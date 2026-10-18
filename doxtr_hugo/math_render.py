"""Math node handling: PNG pre-render (default) + mathjax/mathml opt-in (guide §4.3, §5).

Hugo has no built-in math renderer: KaTeX/MathJax require a partial template plus
a CDN script, and MathML/HTML passthrough depends on theme CSS. To keep the
emitted content self-contained and theme-portable, doxtr-hugo **pre-renders math
to static PNG images by default** (guide line 55, §4.3). The ``mathjax`` and
``mathml-passthrough`` modes remain opt-in for operators who wire up client-side
rendering themselves.

Modes (``doxtr_hugo_math``)
---------------------------
``png`` (default)
    Render each ``math`` / ``math_block`` node to a content-hashed PNG image in
    the page bundle and rewrite it to a Markdown image reference. Uses
    matplotlib's ``mathtext`` engine (ships in the reactor image, no extra
    binary, no client-side JS — guide §9 item 3). An optional ``dvipng`` path is
    modeled for full-LaTeX fidelity but the matplotlib path is the guaranteed
    default.
``mathjax``
    Emit the LaTeX verbatim wrapped in MathJax delimiters (``\\(...\\)`` inline,
    ``$$...$$`` display). The operator supplies the MathJax script/partial.
``mathml-passthrough``
    Emit raw ``<math>`` MathML (inline) / block markup passthrough. Requires
    ``markup.goldmark.renderer.unsafe = true`` (already set in the skeleton).

Design goals
------------
Mirrors :mod:`doxtr_hugo.diagrams`: detection is by node *class name* (no import
of optional extensions), filenames are content-addressed for idempotent
rebuilds, and the single rendering choke point is injectable so unit tests run
without invoking matplotlib or any binary.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .assets import HASH_LEN  # single source of truth; exposed as a module attr

#: Accepted values for ``doxtr_hugo_math`` (guide §8 config surface).
MATH_PNG = "png"
MATH_MATHJAX = "mathjax"
MATH_MATHML = "mathml-passthrough"
MATH_MODES = (MATH_PNG, MATH_MATHJAX, MATH_MATHML)

#: Filename prefix for every rendered math asset (parallels diagrams' prefix).
ASSET_PREFIX = "math"

#: Length of the hex digest in content-hashed filenames. Imported from
#: :data:`doxtr_hugo.assets.HASH_LEN` (the single source of truth) so math,
#: diagram, and copied-asset filenames all share one digest length.

#: docutils/Sphinx math node *class names* (lower-cased). ``math`` is the inline
#: role node; ``math_block`` is the ``.. math::`` directive / displayed block.
#: Matching on the class name avoids importing docutils' math nodes at import
#: time and tolerates the Sphinx vs. docutils node-class differences.
INLINE_MATH_NODES = {"math"}
BLOCK_MATH_NODES = {"math_block", "displaymath"}
MATH_NODES = INLINE_MATH_NODES | BLOCK_MATH_NODES


# ---------------------------------------------------------------------------
# Node detection + source extraction
# ---------------------------------------------------------------------------


def is_math_node(node) -> bool:
    """Return whether ``node`` is a docutils/Sphinx math node (inline or block)."""
    return type(node).__name__.lower() in MATH_NODES


def is_block_math(node) -> bool:
    """Return whether ``node`` is a *displayed* math block (vs. inline math)."""
    return type(node).__name__.lower() in BLOCK_MATH_NODES


def math_source(node) -> str:
    """Extract the raw LaTeX source carried by a math ``node``.

    docutils stores the formula under the node's ``latex`` attribute (both the
    inline ``math`` role node and the ``math_block`` directive node). We probe
    the known carriers in order and fall back to the node's text content so an
    unusual node shape still yields *something* rather than silently dropping
    the formula.
    """
    # Mapping-style access (docutils Element supports ``node["latex"]``).
    for key in ("latex", "text"):
        try:
            value = node[key]  # type: ignore[index]
        except (KeyError, TypeError):
            value = None
        if value:
            return str(value)
    # Attribute-style carriers.
    for attr in ("latex", "text"):
        value = getattr(node, attr, None)
        if value:
            return str(value)
    # Last resort: the node's rendered text.
    astext = getattr(node, "astext", None)
    if callable(astext):
        return str(astext())
    return ""


def detect_math_nodes(doctree):
    """Yield ``(node, is_block)`` for every math node in ``doctree``.

    Pure traversal: no rendering, no filesystem. Iterates the doctree once,
    matching nodes by class name via :func:`is_math_node`.
    """
    traverse = getattr(doctree, "findall", None) or getattr(doctree, "traverse")
    for node in traverse():
        if is_math_node(node):
            yield node, is_block_math(node)


# ---------------------------------------------------------------------------
# Content-addressed filenames
# ---------------------------------------------------------------------------


def asset_filename(source: str, *, block: bool, fmt: str = "png") -> str:
    """Return a deterministic ``math-<hash>.<fmt>`` filename.

    The hash digests the LaTeX *source*, whether it is a block vs. inline
    formula (they render at different sizes), and the format, so identical
    formulas de-duplicate to one file and any change yields a new file
    (cache-busting). The result is a bare filename co-located in the page
    bundle by the caller.
    """
    h = hashlib.sha256()
    h.update(b"block" if block else b"inline")
    h.update(b"\x00")
    h.update(fmt.encode("utf-8"))
    h.update(b"\x00")
    h.update(source.encode("utf-8"))
    digest = h.hexdigest()[:HASH_LEN]
    return f"{ASSET_PREFIX}-{digest}.{fmt}"


# ---------------------------------------------------------------------------
# PNG rendering (default mode)
# ---------------------------------------------------------------------------


class MathRenderError(RuntimeError):
    """Raised when a math backend is available but rendering fails."""


@dataclass
class MathResult:
    """The outcome of :func:`render_math_png`.

    Attributes
    ----------
    path:
        Absolute path of the emitted PNG file.
    filename:
        Bare filename a Markdown ``![alt](...)`` reference uses.
    block:
        Whether this was a displayed block (vs. inline) formula.
    reused:
        ``True`` when the file already existed (idempotent rebuild) and no
        renderer was invoked.
    """

    path: str
    filename: str
    block: bool
    reused: bool = False
    extras: dict = field(default_factory=dict, compare=False)


#: amsmath macros that matplotlib's ``mathtext`` engine does not implement,
#: mapped to the semantically-equivalent macro it *does* support. mathtext has
#: no concept of text-style vs. display-style sizing, so ``\tfrac`` (text-style
#: fraction) and ``\dfrac`` (display-style) both collapse to ``\frac``; the
#: ``\t``/``\d`` binomial variants collapse to ``\binom``. Without this the
#: renderer raises ``ValueError`` for perfectly valid LaTeX (e.g. ``\tfrac``),
#: forcing the whole formula into the MathJax passthrough fallback.
_MATHTEXT_MACRO_ALIASES = {
    r"\tfrac": r"\frac",
    r"\dfrac": r"\frac",
    r"\tbinom": r"\binom",
    r"\dbinom": r"\binom",
}

#: Match an unsupported alias as a whole control sequence: the macro name must
#: not be immediately followed by another letter (so ``\tfracky`` — if it ever
#: existed — is left untouched, only the exact token is rewritten).
_MATHTEXT_ALIAS_RE = re.compile(
    r"(" + "|".join(re.escape(m) for m in _MATHTEXT_MACRO_ALIASES) + r")(?![A-Za-z])"
)


def normalize_mathtext_source(source: str) -> str:
    r"""Rewrite amsmath macros mathtext cannot parse into supported equivalents.

    matplotlib's ``mathtext`` engine is not a full LaTeX implementation: common
    amsmath text/display-style macros such as ``\tfrac`` and ``\dfrac`` are not
    recognized and raise a parse error, which would otherwise knock the formula
    out of the default PNG pre-render and into the MathJax passthrough fallback.
    Since mathtext has no text/display sizing distinction, mapping these aliases
    to their base macro (``\frac`` / ``\binom``) renders the mathematically
    identical result. This is a pure string transform with no side effects.
    """
    return _MATHTEXT_ALIAS_RE.sub(
        lambda m: _MATHTEXT_MACRO_ALIASES[m.group(0)], source
    )


def _matplotlib_renderer(source: str, out_path: str, *, block: bool, dpi: int) -> None:
    """Render ``source`` LaTeX to a PNG at ``out_path`` via matplotlib mathtext.

    This is the single rendering choke point tests monkeypatch. It uses the
    ``Agg`` backend and matplotlib's built-in ``mathtext`` engine, so it needs
    no TeX install and no display — it works in the reactor image and in CI with
    zero extra binaries (guide §9 item 3). The formula is wrapped in ``$...$``
    math delimiters as mathtext expects. Unsupported amsmath aliases (``\tfrac``
    etc.) are first normalized so valid LaTeX is not forced into the fallback.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    text = normalize_mathtext_source(source).strip()
    # mathtext needs the expression inside a single pair of ``$`` delimiters.
    if not (text.startswith("$") and text.endswith("$")):
        text = f"${text}$"

    fig = plt.figure(figsize=(0.01, 0.01))
    try:
        fig.text(0, 0, text, fontsize=16)
        # ``bbox_inches="tight"`` crops the canvas to the rendered glyphs so the
        # PNG has no stray whitespace; transparent background blends into any
        # theme.
        fig.savefig(
            out_path,
            dpi=dpi,
            transparent=True,
            bbox_inches="tight",
            pad_inches=0.02 if not block else 0.05,
        )
    finally:
        plt.close(fig)


def render_math_png(
    node_or_source,
    out_dir: str | Path,
    *,
    block: bool | None = None,
    dpi: int = 150,
    renderer: Callable[..., None] = _matplotlib_renderer,
) -> MathResult:
    """Render one math formula to a PNG under ``out_dir``; return a MathResult.

    Parameters
    ----------
    node_or_source:
        Either a math doctree node (source + inline/block auto-detected) or a
        raw LaTeX source string (then ``block`` should be given; defaults to
        inline).
    out_dir:
        Bundle directory to write the asset into (created if needed).
    block:
        Override/supply whether this is a displayed block formula. Required
        semantics when passing raw source; ignored when a node is passed and it
        determines block-ness itself unless explicitly overridden.
    dpi:
        Rendering resolution. Higher is crisper but larger.
    renderer:
        Injection point for tests (the actual matplotlib call).

    Returns
    -------
    MathResult
        The file exists on disk on return.

    Raises
    ------
    MathRenderError
        If the renderer runs but produces no (or empty) output.
    """
    if isinstance(node_or_source, str):
        source = node_or_source
        is_block = bool(block)
    else:
        source = math_source(node_or_source)
        is_block = is_block_math(node_or_source) if block is None else bool(block)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = asset_filename(source, block=is_block, fmt="png")
    out_path = out_dir / filename

    # Idempotent: identical formula already rendered → reuse.
    if out_path.exists() and out_path.stat().st_size > 0:
        return MathResult(
            path=str(out_path), filename=filename, block=is_block, reused=True
        )

    try:
        renderer(source, str(out_path), block=is_block, dpi=dpi)
    except MathRenderError:
        raise
    except Exception as exc:
        # The renderer (matplotlib ``mathtext``) raises ``ValueError`` /
        # ``ParseFatalException`` for LaTeX it cannot parse (e.g. ``\tfrac``).
        # Normalize every renderer failure to :class:`MathRenderError` so
        # callers need only catch that one type and degrade gracefully instead
        # of letting an arbitrary exception abort the whole Sphinx build.
        # Clean up any partial/empty file the renderer may have left behind.
        try:
            if out_path.exists():
                out_path.unlink()
        except OSError:
            pass
        raise MathRenderError(
            f"math renderer failed for {filename!r}: {exc}"
        ) from exc

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise MathRenderError(
            f"math renderer produced no output for {filename!r}"
        )

    return MathResult(path=str(out_path), filename=filename, block=is_block)


# ---------------------------------------------------------------------------
# Markdown emission (per mode)
# ---------------------------------------------------------------------------


def _escape_md_alt(text: str) -> str:
    """Escape a LaTeX string for safe use as a Markdown image alt attribute."""
    # Alt text lives inside ``![...]`` — closing brackets would truncate it.
    return text.replace("\\", "\\\\").replace("]", "\\]").replace("\n", " ").strip()


def png_markdown(result: MathResult, source: str) -> str:
    """Return a Markdown image reference for a rendered-PNG math result.

    Returns a bare Markdown image reference. Block-vs-inline layout (the
    surrounding blank lines that make a display equation stand on its own line)
    is the caller's responsibility (block context), not this function's. The
    original LaTeX is preserved in the alt text for accessibility and as a
    fallback when the image fails to load.
    """
    alt = _escape_md_alt(source)
    return f"![{alt}]({result.filename})"


def mathjax_markdown(source: str, *, block: bool) -> str:
    """Return MathJax-delimited markup for the ``mathjax`` mode.

    MathJax's default TeX delimiters are ``\\(...\\)`` for inline and ``$$...$$``
    (or ``\\[...\\]``) for display math. We emit those verbatim; the operator is
    responsible for loading the MathJax script/partial (guide §4.3 note: not a
    default). The source is passed through unescaped because MathJax consumes
    raw LaTeX.
    """
    text = source.strip()
    if block:
        return f"$$\n{text}\n$$"
    return rf"\({text}\)"


def mathml_markdown(source: str, mathml: str | None, *, block: bool) -> str:
    """Return MathML passthrough markup for the ``mathml-passthrough`` mode.

    When Sphinx/docutils has already produced a MathML serialization (passed as
    ``mathml``), we emit it verbatim (requires ``unsafe = true`` in the skeleton
    ``hugo.toml``, which is set). When no MathML is available we fall back to a
    ``<math>`` element wrapping the raw LaTeX as its text so the information is
    not lost — a renderer-agnostic, non-silent degradation.
    """
    if mathml:
        return mathml.strip()
    # Degrade: wrap the LaTeX in a minimal MathML shell so content is preserved.
    display = ' display="block"' if block else ""
    latex = source.strip()
    # Escape for XML text content.
    latex = (
        latex.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return f'<math xmlns="http://www.w3.org/1998/Math/MathML"{display}><mtext>{latex}</mtext></math>'


def math_markdown(
    node_or_source,
    out_dir: str | Path,
    *,
    mode: str = MATH_PNG,
    block: bool | None = None,
    mathml: str | None = None,
    dpi: int = 150,
    renderer: Callable[..., None] = _matplotlib_renderer,
) -> tuple[str, MathResult | None]:
    """Produce the Markdown for one math node under the given ``mode``.

    This is the single entry point the builder/translator calls. It dispatches
    on ``doxtr_hugo_math``:

    - ``png`` (default): render a PNG (via :func:`render_math_png`) and return a
      Markdown image reference. The returned :class:`MathResult` lets the caller
      record the asset in the build manifest (guide §4.1 step 5).
    - ``mathjax``: return MathJax-delimited markup; no asset is emitted.
    - ``mathml-passthrough``: return MathML markup; no asset is emitted.

    An unrecognized ``mode`` safely defaults to ``png`` so a misconfiguration
    never silently drops a formula.

    Returns
    -------
    (markdown, result)
        ``markdown`` is the string to insert into the page; ``result`` is the
        :class:`MathResult` for the ``png`` mode (for manifest recording), else
        ``None``.
    """
    if isinstance(node_or_source, str):
        source = node_or_source
        is_block = bool(block)
    else:
        source = math_source(node_or_source)
        is_block = is_block_math(node_or_source) if block is None else bool(block)

    if mode == MATH_MATHJAX:
        return mathjax_markdown(source, block=is_block), None
    if mode == MATH_MATHML:
        return mathml_markdown(source, mathml, block=is_block), None

    # Default / unrecognized -> png.
    result = render_math_png(
        source, out_dir, block=is_block, dpi=dpi, renderer=renderer
    )
    return png_markdown(result, source), result
