"""Diagram detection + render-to-file for the supported backends (guide §4).

This module turns the diagram *source* carried on a doctree node into a static
image file on disk, chosen per the format policy in
:mod:`doxtr_hugo.image_policy`. It is the render half of the pipeline described
in guide §4.1; node rewriting (step 4) and manifest recording (step 5) live in
the builder/translator and :mod:`doxtr_hugo.assets` respectively.

Scope
-----
Five backends are implemented here — **PlantUML** (``plantuml``), **Graphviz**
(``dot``), **Mermaid** (``mmdc``), **d2** (``d2``, from ``doxtr_d2``), and
**sphinx_diagrams** (the ``diagrams`` Python library, PNG-only). PlantUML,
Graphviz and d2 ship in the reactor/CI image (guide §7.3) so they work with
zero extra install; Mermaid is **gated** (guide §4.3/§5): ``mmdc`` is only
present if the operator installs ``@mermaid-js/mermaid-cli``, and
sphinx_diagrams is gated on the ``diagrams`` library being importable. A gated
backend's absence degrades gracefully via the missing-backend policy. Each
backend is modeled as a small, pure description (:class:`DiagramBackend`) plus a
render function that shells out to the binary.
Additional backends register via :func:`register_backend` (re-exported as
``doxtr_hugo.register_backend``), which installs the :class:`DiagramBackend`,
maps its node class names, and optionally supplies a custom runner — so the
detection/render contract here is deliberately backend-agnostic and no caller
has to mutate the module tables directly.

Design goals
------------
- **Testable without binaries.** The subprocess call is funneled through a
  single ``runner`` hook that tests monkeypatch (mirroring how the roadmap
  tests monkeypatch ``plantuml``). Detection, capability modeling, filename
  hashing, and argument construction are all pure and unit-testable.
- **Deterministic, content-addressed filenames.** The emitted file name is a
  stable hash of the diagram source + format + backend, so rebuilds are
  idempotent and identical diagrams de-duplicate.
- **Graceful degradation is representable.** :func:`detect_backend` returns
  ``None`` when a backend binary is absent; callers (builder) apply the
  ``doxtr_hugo_on_missing_backend`` policy. This module never raises for a
  missing binary during *detection*; it only raises if an explicitly requested
  render actually fails.
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .image_policy import BackendCapability, FormatDecision, choose_format
from .assets import HASH_LEN  # single source of truth; re-exported below

#: Filename prefix for every rendered diagram asset (guide §4.1 step 4 shows
#: ``diagram-<hash>.svg``).
ASSET_PREFIX = "diagram"

#: Policy values accepted by ``doxtr_hugo_on_missing_backend`` (guide §4.4).
ON_MISSING_PASSTHROUGH = "warn-passthrough"
ON_MISSING_SKIP = "warn-skip"
ON_MISSING_ERROR = "error"
ON_MISSING_BACKEND_CHOICES = (
    ON_MISSING_PASSTHROUGH,
    ON_MISSING_SKIP,
    ON_MISSING_ERROR,
)

#: Markdown fenced-code-block language tag used when passing a diagram's source
#: through verbatim (``warn-passthrough``). Keyed on backend key so the source
#: still reads sensibly in the rendered page; unknown backends fall back to no
#: language hint.
PASSTHROUGH_FENCE_LANG = {
    "plantuml": "plantuml",
    "graphviz": "dot",
    "mermaid": "mermaid",
    "d2": "d2",
    # sphinx_diagrams' source is a Python script (the ``diagrams`` library DSL).
    "diagrams": "python",
}

#: Sentinel token a backend's ``build_args`` emits where the diagram *source*
#: must be supplied as an input-file path. File-input backends (Mermaid's
#: ``mmdc``, which has no stdin/stdout pipe mode) use this; :func:`_default_runner`
#: writes the source to a temp file and substitutes the real path before exec.
INPUT_PLACEHOLDER = "__DOXTR_INPUT__"

#: Length of the hex digest in content-hashed filenames. Imported from
#: :data:`doxtr_hugo.assets.HASH_LEN` (the single source of truth) so diagram,
#: math, and copied-asset filenames all share one digest length. Exposed as
#: ``diagrams.HASH_LEN`` for existing callers/tests via the import above.


# ---------------------------------------------------------------------------
# Node source extraction
# ---------------------------------------------------------------------------

#: docutils/Sphinx node *class names* (lower-cased) we recognize as diagrams,
#: mapped to the backend key that renders them. Matching on the class name
#: avoids importing optional extensions (``sphinxcontrib.plantuml`` /
#: ``sphinx.ext.graphviz``) at module import time — they may not be installed.
NODE_BACKEND = {
    "plantuml": "plantuml",
    "graphviz": "graphviz",
    "graph": "graphviz",
    "digraph": "graphviz",
    "mermaid": "mermaid",
    # doxtr_d2's ``d2node`` (``d2`` binary) and sphinx_diagrams' ``diagrams``
    # node (the ``diagrams`` Python library DSL, rendered via the Python
    # interpreter). Both are matched by class name so importing the optional
    # extension at module load time is never required.
    "d2node": "d2",
    "diagrams": "diagrams",
}


def node_backend_key(node) -> str | None:
    """Return the backend key for a doctree ``node``, or ``None``.

    Detection is by the node's class name (case-insensitive) so this works
    whether or not the diagram extension is importable in the current
    environment. Both ``sphinxcontrib.plantuml``'s ``plantuml`` node and
    ``sphinx.ext.graphviz``'s ``graphviz`` node are recognized.
    """
    name = type(node).__name__.lower()
    return NODE_BACKEND.get(name)


def diagram_source(node) -> str:
    """Extract the raw diagram source text carried by a diagram ``node``.

    ``sphinxcontrib.plantuml`` stores the UML text on the node's
    ``uml``/``content`` attributes; ``sphinx.ext.graphviz`` stores the DOT text
    under the ``code`` key of the node's attribute dict;
    ``sphinxcontrib.mermaid`` and ``sphinx_diagrams`` store the diagram text
    under the ``code`` key; ``doxtr_d2``'s ``d2node`` stores the inline script
    under ``d2_script`` (or a path under ``d2_file`` when ``source == "file"``).
    We probe the known carriers in order and fall back to the node's text
    content.
    """
    # doxtr_d2: inline script, or an external file referenced by path.
    try:
        if node.get("source") == "file" and node.get("d2_file"):  # type: ignore[attr-defined]
            try:
                with open(node["d2_file"], encoding="utf-8") as fh:  # type: ignore[index]
                    return fh.read()
            except OSError:
                pass
    except (AttributeError, TypeError):
        pass
    for key in ("uml", "d2_script", "code"):
        try:
            value = node[key]  # type: ignore[index]
        except (KeyError, TypeError):
            value = None
        if value:
            return str(value)
    # Attribute-style carriers.
    for attr in ("uml", "code", "content"):
        value = getattr(node, attr, None)
        if value:
            if isinstance(value, (list, tuple)):
                return "\n".join(str(v) for v in value)
            return str(value)
    # Last resort: the node's rendered text.
    astext = getattr(node, "astext", None)
    if callable(astext):
        return str(astext())
    return ""


# ---------------------------------------------------------------------------
# Backend model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiagramBackend:
    """A diagram renderer that shells out to a single binary.

    Attributes
    ----------
    key:
        Stable backend key (``"plantuml"`` / ``"graphviz"``) used for node
        mapping, hashing, and config.
    binary:
        The executable name resolved on ``PATH`` (``"plantuml"`` / ``"dot"``).
    capability:
        What formats the backend can emit (:class:`BackendCapability`), fed to
        :func:`doxtr_hugo.image_policy.choose_format`.
    build_args:
        Pure function ``(binary, fmt, out_path) -> argv`` producing the exact
        command line. Kept as a field so each backend's CLI quirks stay local
        and unit-testable without running the binary.
    stdin_source:
        When ``True`` the diagram source is written to the process stdin;
        otherwise it is passed via a temp input file path appended to argv.
    node_extra_args:
        Optional pure function ``(node) -> tuple[str, ...]`` producing per-node
        extra CLI flags derived from the diagram directive's options (e.g. d2's
        multi-board ``--target`` / ``--animate-interval``). Keeping this on the
        backend means the render pass has no backend-specific branching: it
        calls ``backend.node_extra_args(node)`` for every backend and gets an
        empty tuple for those that need none.
    """

    key: str
    binary: str
    capability: BackendCapability
    build_args: Callable[[str, str, str], Sequence[str]]
    stdin_source: bool = True
    node_extra_args: Callable[[object], tuple[str, ...]] | None = None

    def extra_args_for(self, node) -> tuple[str, ...]:
        """Return per-node CLI flags for ``node``, or an empty tuple.

        Wraps the optional :attr:`node_extra_args` hook so callers never need to
        null-check it or branch on the backend key.
        """
        if self.node_extra_args is None:
            return ()
        return self.node_extra_args(node)


def _plantuml_args(binary: str, fmt: str, out_path: str) -> list[str]:
    """``plantuml -t<fmt> -pipe`` → emits the chosen format on stdout.

    PlantUML's ``-pipe`` mode reads source on stdin and writes the rendered
    image to stdout, which keeps us free of temp input files. The caller
    redirects stdout to ``out_path``.
    """
    return [binary, f"-t{fmt}", "-pipe"]


def _graphviz_args(binary: str, fmt: str, out_path: str) -> list[str]:
    """``dot -T<fmt> -o <out>`` → reads DOT on stdin, writes to ``out_path``."""
    return [binary, f"-T{fmt}", "-o", out_path]


def _mermaid_args(binary: str, fmt: str, out_path: str) -> list[str]:
    """``mmdc -i <input> -o <out> -e <fmt>`` → reads a Mermaid source file.

    ``mmdc`` (``@mermaid-js/mermaid-cli``) has no stdin/stdout pipe mode: it
    reads an input file and writes an output file, inferring the format from
    the output extension (``-e`` makes it explicit). We emit
    :data:`INPUT_PLACEHOLDER` where the input path belongs; the runner writes
    the diagram source to a temp file and substitutes the real path.
    """
    return [binary, "-i", INPUT_PLACEHOLDER, "-o", out_path, "-e", fmt]


def _d2_args(binary: str, fmt: str, out_path: str) -> list[str]:
    """``d2 - <out_path>`` → reads the d2 script on stdin, writes ``out_path``.

    ``d2`` infers the output format from the output file's extension, so no
    explicit format flag is needed (``out_path`` already carries the chosen
    ``.svg``/``.png`` suffix via the content-hashed filename). The default
    layout/theme are used; the richer directive options handled by
    ``doxtr_d2``'s own HTML/LaTeX visitors (dark mode, multi-board, link
    rewriting) are intentionally out of scope for the Hugo static-image path —
    a plain, deterministic render is what a Markdown ``![](...)`` reference
    needs.
    """
    return [binary, "-", out_path]


def _d2_node_extra_args(node) -> tuple[str, ...]:
    """Per-node d2 flags for multi-board diagrams (``:target:`` / ``:animate_interval:``).

    A d2 composition (``layers``/``scenarios``/``steps``) is multi-board. d2
    renders such a source to *multiple* files unless it is told to collapse to a
    single file — either ``--target`` to select one board or
    ``--animate-interval`` to emit a single animated SVG. ``doxtr_d2`` records
    these as directive ``options``; without the matching flag d2 writes into an
    ``out/`` directory and our single-file output path stays empty (the render
    reports "no output"). Threading just these two flags keeps multi-board d2
    diagrams rendering while leaving the rest of ``doxtr_d2``'s rich option set
    (dark mode, fonts, layout) to its own HTML/LaTeX visitors — out of scope for
    the static Hugo image path. Exposed on the d2 :class:`DiagramBackend` via
    ``node_extra_args`` so the render pass stays backend-agnostic.
    """
    try:
        opts = node.get("options", {}) or {}
    except AttributeError:
        return ()
    args: list[str] = []
    target = opts.get("target")
    if target:
        args.append(f"--target={target}")
    animate = opts.get("animate_interval")
    if animate:
        args.append(f"--animate-interval={int(animate)}")
    return tuple(args)


#: Argument the ``diagrams`` (``sphinx_diagrams``) backend passes to the Python
#: interpreter: the diagram's ``diagrams``-library script is read on stdin
#: (``-``) and executed. The script's ``Diagram(..., filename=<stem>)`` call
#: writes ``<stem>.png`` into the process working directory. :func:`_diagrams_runner`
#: supplies a private temp stem + cwd and reads the PNG bytes back, so the
#: result flows through the same content-hashed/manifest path as every other
#: backend. The ``show=False`` second argument is the convention
#: ``sphinx_diagrams`` itself uses to stop the library opening a viewer.
def _diagrams_args(binary: str, fmt: str, out_path: str) -> list[str]:
    """``python - <stem> false`` → runs a ``diagrams``-library script on stdin.

    ``binary`` is the Python interpreter. ``out_path`` is the final content-
    hashed PNG path; its stem (minus the ``.png``) is handed to the script as
    ``argv[1]`` so the ``diagrams`` library writes next to it. The actual temp
    stem/cwd juggling lives in :func:`_diagrams_runner` (this backend's custom
    runner) because the library writes a file it names itself rather than to
    stdout or a ``-o`` path.
    """
    stem = Path(out_path).stem
    return [binary, "-", stem, "false"]


#: The built-in backend table. Both binaries ship in the reactor/CI image
#: (guide §7.3), so no extra install is required for these two.
BACKENDS: dict[str, DiagramBackend] = {
    "plantuml": DiagramBackend(
        key="plantuml",
        binary="plantuml",
        capability=BackendCapability("plantuml", can_svg=True, can_png=True),
        build_args=_plantuml_args,
        stdin_source=True,
    ),
    "graphviz": DiagramBackend(
        key="graphviz",
        binary="dot",
        capability=BackendCapability("graphviz", can_svg=True, can_png=True),
        build_args=_graphviz_args,
        stdin_source=True,
    ),
    # Mermaid is **gated** (guide §4.3, §5): ``mmdc`` is not installed by
    # default, so ``detect_backend("mermaid")`` returns ``None`` unless the
    # operator installs ``@mermaid-js/mermaid-cli``. When absent, the missing-
    # backend policy (guide §4.4) degrades gracefully. ``mmdc`` is a file-input
    # backend (no pipe mode), so ``stdin_source=False``.
    "mermaid": DiagramBackend(
        key="mermaid",
        binary="mmdc",
        capability=BackendCapability("mermaid", can_svg=True, can_png=True),
        build_args=_mermaid_args,
        stdin_source=False,
    ),
    # doxtr_d2 (``d2node``): the ``d2`` binary renders a d2 script read on stdin
    # to a file whose format is chosen by the output extension. It ships in the
    # reactor/CI image; when absent the missing-backend policy degrades
    # gracefully. d2 can emit both SVG and PNG, so the format policy (§4.2)
    # prefers SVG unless Hugo image processing is requested.
    "d2": DiagramBackend(
        key="d2",
        binary="d2",
        capability=BackendCapability("d2", can_svg=True, can_png=True),
        build_args=_d2_args,
        stdin_source=True,
        node_extra_args=_d2_node_extra_args,
    ),
    # sphinx_diagrams (``diagrams`` node): the diagram source is a Python script
    # using the ``diagrams`` library DSL, which writes a **PNG only** (the
    # library has no vector output), so the capability is PNG-only and the
    # format policy resolves to png. ``binary`` is the running interpreter and
    # the custom :func:`_diagrams_runner` handles the library's
    # writes-its-own-file invocation model. Detection checks the ``diagrams``
    # library is importable (see :func:`detect_backend`) rather than a binary on
    # PATH.
    "diagrams": DiagramBackend(
        key="diagrams",
        binary=sys.executable or "python3",
        capability=BackendCapability(
            "diagrams", can_svg=False, can_png=True, can_webp=False
        ),
        build_args=_diagrams_args,
        stdin_source=True,
    ),
}


def detect_backend(
    key: str,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> DiagramBackend | None:
    """Return the :class:`DiagramBackend` for ``key`` if its binary is on PATH.

    ``which`` is injectable so tests can simulate a present/absent binary
    without touching the real environment. Returns ``None`` when the backend is
    unknown or its binary cannot be found — the signal callers use to apply the
    ``doxtr_hugo_on_missing_backend`` graceful-degradation policy.

    The ``diagrams`` (``sphinx_diagrams``) backend is special-cased: its
    "binary" is the running Python interpreter, so availability is determined by
    whether the ``diagrams`` library is importable rather than by a PATH lookup.
    """
    backend = BACKENDS.get(key)
    if backend is None:
        return None
    if key == "diagrams":
        # Availability = the ``diagrams`` library is importable. The interpreter
        # itself is always present (it is running this code).
        if importlib.util.find_spec("diagrams") is None:
            return None
        return backend
    if which(backend.binary) is None:
        return None
    return backend


# ---------------------------------------------------------------------------
# Content-addressed filenames
# ---------------------------------------------------------------------------


def asset_filename(source: str, fmt: str, *, backend_key: str = "") -> str:
    """Return a deterministic ``diagram-<hash>.<fmt>`` filename.

    The hash digests the diagram *source*, the chosen ``fmt``, and the backend
    key so that (a) identical diagrams rendered the same way de-duplicate to one
    file and (b) changing the source or the target format yields a new file
    (cache-busting). The result is a bare filename (no directory), co-located in
    the page bundle by the caller.
    """
    h = hashlib.sha256()
    h.update(backend_key.encode("utf-8"))
    h.update(b"\x00")
    h.update(fmt.encode("utf-8"))
    h.update(b"\x00")
    h.update(source.encode("utf-8"))
    digest = h.hexdigest()[:HASH_LEN]
    return f"{ASSET_PREFIX}-{digest}.{fmt}"


# ---------------------------------------------------------------------------
# Render result + rendering
# ---------------------------------------------------------------------------


@dataclass
class RenderResult:
    """The outcome of :func:`render_diagram`.

    Attributes
    ----------
    path:
        Absolute path of the emitted image file.
    filename:
        Bare filename (what a Markdown ``![alt](...)`` reference uses).
    fmt:
        The format actually emitted.
    backend_key:
        Which backend produced it.
    decision:
        The :class:`FormatDecision` that selected ``fmt`` (carries any warning).
    reused:
        ``True`` when the file already existed (idempotent rebuild) and no
        binary was invoked.
    """

    path: str
    filename: str
    fmt: str
    backend_key: str
    decision: FormatDecision
    reused: bool = False
    extras: dict = field(default_factory=dict, compare=False)


def _default_runner(
    argv: Sequence[str],
    source: str,
    stdin_source: bool,
    cwd: str | None = None,
) -> bytes:
    """Invoke a backend binary and return its stdout bytes.

    This is the single subprocess choke point tests monkeypatch. For
    stdin-reading backends, source text is fed on stdin and ``stdout`` is
    captured (stdout-emitting backends like PlantUML ``-pipe`` return their
    image bytes; file-writing backends like Graphviz ``-o`` return empty stdout
    and write the file themselves).

    For file-input backends (``stdin_source=False``, e.g. Mermaid's ``mmdc``),
    the source is written to a temporary input file and the
    :data:`INPUT_PLACEHOLDER` token in ``argv`` is replaced with its path. The
    temp file is removed afterwards; these backends write their own output file.
    """
    argv = list(argv)
    tmp_input: str | None = None
    try:
        if not stdin_source:
            fd, tmp_input = tempfile.mkstemp(suffix=".mmd", prefix="doxtr-diagram-")
            with open(fd, "w", encoding="utf-8") as fh:
                fh.write(source)
            argv = [tmp_input if a == INPUT_PLACEHOLDER else a for a in argv]
        proc = subprocess.run(
            argv,
            input=source.encode("utf-8") if stdin_source else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            check=False,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace").strip()
            raise DiagramRenderError(
                f"{argv[0]} exited {proc.returncode}: {stderr or '(no stderr)'}"
            )
        return proc.stdout
    finally:
        if tmp_input is not None:
            try:
                Path(tmp_input).unlink()
            except OSError:
                pass


class DiagramRenderError(RuntimeError):
    """Raised when a backend binary is present but rendering fails."""


def _diagrams_runner(
    argv: Sequence[str],
    source: str,
    stdin_source: bool,
    cwd: str | None = None,
) -> bytes:
    """Runner for the ``sphinx_diagrams`` ``diagrams`` backend.

    The ``diagrams`` library writes a PNG it names itself (``<stem>.png``) into
    the process working directory rather than to stdout or a ``-o`` path, so the
    stdout/``-o`` contract of :func:`_default_runner` does not apply. This
    runner executes the script (``python - <stem> false`` on stdin) inside a
    private temp directory, then reads the ``<stem>.png`` the library produced
    and returns its bytes — letting the result flow through the same content-
    hashed-filename + manifest path as every other backend.

    ``argv`` is ``[python, "-", <stem>, "false"]`` as built by
    :func:`_diagrams_args`; the stem is ``argv[2]``.
    """
    argv = list(argv)
    stem = argv[2] if len(argv) > 2 else "diagram"
    work = tempfile.mkdtemp(prefix="doxtr-diagrams-")
    # ``cwd`` is accepted for signature parity with :func:`_default_runner` but
    # ignored: the ``diagrams`` library must write into the private ``work`` dir
    # so we can read the PNG back, so that directory is always the subprocess
    # cwd regardless of any caller-supplied value.
    try:
        proc = subprocess.run(
            argv,
            input=source.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=work,
            check=False,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace").strip()
            raise DiagramRenderError(
                f"{argv[0]} (diagrams) exited {proc.returncode}: "
                f"{stderr or '(no stderr)'}"
            )
        produced = Path(work) / f"{stem}.png"
        if not produced.exists() or produced.stat().st_size == 0:
            raise DiagramRenderError(
                f"diagrams script did not produce {stem}.png (ensure the "
                f"Diagram(...) filename matches, as sphinx_diagrams requires)"
            )
        return produced.read_bytes()
    finally:
        shutil.rmtree(work, ignore_errors=True)


#: Per-backend runner overrides for backends whose invocation model does not
#: fit the stdout/``-o`` contract of :func:`_default_runner`. Keyed by backend
#: key; a backend absent here uses :func:`_default_runner`. ``sphinx_diagrams``
#: writes a PNG it names itself in its working directory, so it needs the
#: dedicated :func:`_diagrams_runner`.
_BACKEND_RUNNERS: dict[str, Callable[[Sequence[str], str, bool], bytes]] = {
    "diagrams": _diagrams_runner,
}


def register_backend(
    backend: DiagramBackend,
    node_names: Sequence[str],
    *,
    runner: Callable[[Sequence[str], str, bool], bytes] | None = None,
    node_extra_args: Callable[[object], tuple[str, ...]] | None = None,
) -> DiagramBackend:
    """Register a diagram backend so the render pass can detect and render it.

    This is the public consumer-registration seam (re-exported as
    ``doxtr_hugo.register_backend``). It installs ``backend`` under its
    :attr:`~DiagramBackend.key`, maps each doctree node class name in
    ``node_names`` (matched case-insensitively, as
    :func:`node_backend_key` does) to that key, and optionally installs a custom
    subprocess ``runner`` for backends whose binary does not follow the
    stdin/stdout or ``-o`` convention (as ``sphinx_diagrams`` does).

    Parameters
    ----------
    backend:
        The :class:`DiagramBackend` describing the renderer. Its ``key`` is the
        table key used for mapping, hashing, and config.
    node_names:
        Doctree node class names this backend handles (e.g. ``["plantuml"]``).
        Stored lower-cased so detection matches regardless of class casing.
    runner:
        Optional ``(argv, source, stdin_source) -> bytes`` subprocess hook for a
        backend with a non-standard invocation model. When omitted, the default
        stdout/``-o`` runner is used.
    node_extra_args:
        Optional ``(node) -> tuple[str, ...]`` hook for per-node CLI flags. When
        given it overrides any ``node_extra_args`` already on ``backend`` so the
        hook can be supplied at registration time.

    Returns
    -------
    DiagramBackend
        The registered backend (possibly with ``node_extra_args`` applied), for
        convenient chaining.
    """
    if node_extra_args is not None:
        backend = replace(backend, node_extra_args=node_extra_args)
    BACKENDS[backend.key] = backend
    for name in node_names:
        NODE_BACKEND[name.lower()] = backend.key
    if runner is not None:
        _BACKEND_RUNNERS[backend.key] = runner
    return backend


class MissingBackendError(RuntimeError):
    """Raised by :func:`degrade_missing_backend` under the ``"error"`` policy.

    Signals that a diagram backend binary was unavailable and the configured
    ``doxtr_hugo_on_missing_backend`` policy is ``"error"`` (fail the build),
    as opposed to the warn-and-continue policies.
    """


# ---------------------------------------------------------------------------
# Graceful degradation (guide §4.4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DegradeResult:
    """The outcome of applying the missing-backend policy to one diagram.

    This module stays free of the Sphinx logger (keeping it pure and unit
    testable); the builder is responsible for actually emitting ``warning`` via
    its logger and for inserting ``markdown`` into the page. The two
    warn-and-continue policies never raise, so a single diagram with a missing
    backend cannot abort a build unless the operator opted into ``"error"``.

    Attributes
    ----------
    action:
        What the builder should do: ``"passthrough"`` (insert ``markdown``),
        ``"skip"`` (drop the node, insert nothing), or ``"error"`` (never
        returned — the ``"error"`` policy raises instead).
    markdown:
        For ``"passthrough"``, a fenced code block carrying the diagram source
        verbatim so the information is not lost. Empty for ``"skip"``.
    warning:
        A Sphinx-style warning message the builder should log. Always set for
        the warn-* policies.
    backend_key:
        The backend whose binary was missing (for diagnostics/manifest).
    """

    action: str
    markdown: str
    warning: str
    backend_key: str
    extras: dict = field(default_factory=dict, compare=False)


def passthrough_markdown(source: str, backend_key: str) -> str:
    """Return ``source`` wrapped in a fenced code block for passthrough.

    The fence language is chosen from :data:`PASSTHROUGH_FENCE_LANG` so the
    diagram text keeps syntax context in the rendered page; unknown backends
    get a plain fence. A fence long enough to contain any backticks already in
    ``source`` is used so the block never terminates early.
    """
    lang = PASSTHROUGH_FENCE_LANG.get(backend_key, "")
    # Grow the fence past the longest backtick run in the source so embedded
    # backticks cannot prematurely close the block (CommonMark rule).
    longest = 0
    run = 0
    for ch in source:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    fence = "`" * max(3, longest + 1)
    body = source.rstrip("\n")
    return f"{fence}{lang}\n{body}\n{fence}"


def degrade_missing_backend(
    node_or_source,
    *,
    backend_key: str | None = None,
    policy: str = ON_MISSING_PASSTHROUGH,
) -> DegradeResult:
    """Apply the ``doxtr_hugo_on_missing_backend`` policy to a missing backend.

    Call this when :func:`render_diagram` / :func:`detect_backend` reports a
    backend is unavailable. It implements guide §4.4:

    - ``"warn-passthrough"`` (default): return ``action="passthrough"`` with the
      diagram source wrapped in a fenced code block and a warning message.
    - ``"warn-skip"``: return ``action="skip"`` with empty markdown and a
      warning message.
    - ``"error"``: raise :class:`MissingBackendError`.

    Parameters
    ----------
    node_or_source:
        Either a diagram doctree node (source + backend auto-detected) or a raw
        source string (then ``backend_key`` must be given).
    backend_key:
        Override/supply the backend key. Required when passing raw source.
    policy:
        One of :data:`ON_MISSING_BACKEND_CHOICES`. An unrecognized value is
        treated as the default (``"warn-passthrough"``) so a misconfiguration
        never silently drops content.

    Returns
    -------
    DegradeResult
        For the two warn-* policies. The ``"error"`` policy never returns.

    Raises
    ------
    MissingBackendError
        When ``policy == "error"``.
    ValueError
        When a raw source string is passed without ``backend_key``.
    """
    if isinstance(node_or_source, str):
        source = node_or_source
        if backend_key is None:
            raise ValueError("backend_key is required when passing raw source")
        key = backend_key
    else:
        source = diagram_source(node_or_source)
        key = backend_key or node_backend_key(node_or_source) or "diagram"

    binary = BACKENDS[key].binary if key in BACKENDS else key

    if policy == ON_MISSING_ERROR:
        raise MissingBackendError(
            f"doxtr_hugo: diagram backend {key!r} is unavailable "
            f"(binary {binary!r} not found on PATH) and "
            f"doxtr_hugo_on_missing_backend={ON_MISSING_ERROR!r}"
        )

    if policy == ON_MISSING_SKIP:
        warning = (
            f"doxtr_hugo: diagram backend {key!r} unavailable "
            f"(binary {binary!r} not found on PATH); skipping diagram"
        )
        return DegradeResult(
            action="skip", markdown="", warning=warning, backend_key=key
        )

    # Default / unrecognized -> warn-passthrough.
    warning = (
        f"doxtr_hugo: diagram backend {key!r} unavailable "
        f"(binary {binary!r} not found on PATH); passing diagram source "
        f"through as a code block"
    )
    return DegradeResult(
        action="passthrough",
        markdown=passthrough_markdown(source, key),
        warning=warning,
        backend_key=key,
    )


def render_diagram(
    node_or_source,
    out_dir: str | Path,
    *,
    backend_key: str | None = None,
    image_format: str = "auto",
    raster_fallback: str = "png",
    process_images: bool = False,
    wants_processing: bool = False,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[[Sequence[str], str, bool], bytes] | None = None,
    cwd: str | None = None,
    extra_args: Sequence[str] = (),
) -> RenderResult | None:
    """Render one diagram to a file under ``out_dir``; return a RenderResult.

    Parameters
    ----------
    node_or_source:
        Either a diagram doctree node (the backend/source are auto-detected) or
        a raw source string (then ``backend_key`` must be given).
    out_dir:
        Bundle directory to write the asset into (created if needed).
    backend_key:
        Override/supply the backend key. Required when passing raw source.
    image_format, raster_fallback, process_images, wants_processing:
        Passed straight to :func:`doxtr_hugo.image_policy.choose_format`.
    which, runner:
        Injection points for tests (binary discovery and subprocess). When
        ``runner`` is ``None`` (the default) the backend's own runner is used:
        :func:`_default_runner` for stdout/``-o`` backends, or the dedicated
        runner from :data:`_BACKEND_RUNNERS` (e.g. ``sphinx_diagrams``).

    Returns
    -------
    RenderResult | None
        ``None`` when the backend cannot be detected (missing binary or unknown
        node) so the caller can apply the graceful-degradation policy. Otherwise
        a populated result; the file exists on disk on return.

    Raises
    ------
    DiagramRenderError
        If the backend binary is present but the render command fails.
    """
    # Resolve source + backend key.
    if isinstance(node_or_source, str):
        source = node_or_source
        if backend_key is None:
            raise ValueError("backend_key is required when passing raw source")
        key = backend_key
    else:
        source = diagram_source(node_or_source)
        key = backend_key or node_backend_key(node_or_source)
        if key is None:
            return None

    backend = detect_backend(key, which=which)
    if backend is None:
        return None

    # Choose the output format via the shared policy.
    decision = choose_format(
        backend.capability,
        image_format=image_format,
        raster_fallback=raster_fallback,
        process_images=process_images,
        wants_processing=wants_processing,
    )
    fmt = decision.fmt

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Fold any per-node extra backend flags (e.g. d2 ``--target`` /
    # ``--animate-interval`` for multi-board diagrams) into the content hash so
    # two renders of the same source with different flags do not collide on one
    # filename.
    hash_source = source if not extra_args else source + "\x00" + "\x00".join(extra_args)
    filename = asset_filename(hash_source, fmt, backend_key=backend.key)
    out_path = out_dir / filename

    # Idempotent: identical source+format already rendered → reuse.
    if out_path.exists() and out_path.stat().st_size > 0:
        return RenderResult(
            path=str(out_path),
            filename=filename,
            fmt=fmt,
            backend_key=backend.key,
            decision=decision,
            reused=True,
        )

    argv = list(backend.build_args(backend.binary, fmt, str(out_path)))
    if extra_args:
        # Insert extra flags after the binary (argv[0]) and before the
        # stdin/output positional args, matching each backend's CLI shape.
        argv[1:1] = list(extra_args)
    effective_runner = runner or _BACKEND_RUNNERS.get(key, _default_runner)
    # Our own runners accept an optional ``cwd`` (for backends like PlantUML
    # whose source uses relative ``!include`` directives resolved against the
    # original document's directory). A test-injected 3-arg runner is called
    # without it to preserve the documented ``(argv, source, stdin_source)``
    # monkeypatch signature.
    if cwd is not None and effective_runner in (_default_runner, _diagrams_runner):
        stdout = effective_runner(argv, source, backend.stdin_source, cwd)
    else:
        stdout = effective_runner(argv, source, backend.stdin_source)

    # Stdout-emitting backends (PlantUML -pipe) hand us the image bytes; write
    # them. File-writing backends (Graphviz -o) already wrote out_path.
    if stdout:
        out_path.write_bytes(stdout)

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise DiagramRenderError(
            f"{backend.binary} produced no output for {filename!r}"
        )

    return RenderResult(
        path=str(out_path),
        filename=filename,
        fmt=fmt,
        backend_key=backend.key,
        decision=decision,
        reused=False,
    )


def detect_diagram_nodes(doctree, *, backends: Iterable[str] | None = None):
    """Yield ``(node, backend_key)`` for every diagram node in ``doctree``.

    Iterates the doctree once, matching nodes by class name via
    :func:`node_backend_key`. When ``backends`` is given, only nodes for those
    backend keys are yielded (lets the builder restrict to detected binaries).
    Pure traversal: no rendering, no filesystem, no binaries.
    """
    allowed = set(backends) if backends is not None else None
    traverse = getattr(doctree, "findall", None) or getattr(doctree, "traverse")
    for node in traverse():
        key = node_backend_key(node)
        if key is None:
            continue
        if allowed is not None and key not in allowed:
            continue
        yield node, key
