#!/usr/bin/env python3
"""End-to-end test harness entry point for doxtr-hugo.

Runs the real build pipeline against the shipped fixture
(:mod:`test_harness.docs`):

1. **Sphinx stage:** a real ``python -m sphinx -b hugo`` on the fixture docs.
   Asserts exit code 0, no ``ERROR`` lines, and that the emitted tree is a
   coherent Hugo site against the **built-in skeleton** — ``hugo.toml``,
   fallback ``layouts/``, and a ``content/`` page (with front matter) for every
   fixture document. This is the "first real build against the skeleton" proof
   (guide §7.2).
2. **Hugo stage:** when the ``hugo`` binary is present, a real
   ``hugo --source <out> --destination <out>/public`` build. Asserts exit code
   0 and a non-empty ``public/**/index.html`` for each fixture page. When
   ``hugo`` is absent the stage records a ``SKIP`` with a clear reason (the
   local/dev path from guide §7.3); CI installs a pinned Hugo so the stage
   always runs there.

The harness returns non-zero on any ``FAIL`` (same contract as the roadmap
harness); a ``SKIP`` never fails the harness. It prints a structured report so a
missing binary is visible, never a silent pass.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

from doxtr_hugo.assets import MANIFEST_NAME, Manifest, sniff_format

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_DOCS = os.path.join(HERE, "docs")

#: Map a manifest format token to the set of file extensions that are
#: acceptable on disk for it (guide §7.2 step 2: "the file on disk has that
#: extension"). ``jpeg`` accepts both ``.jpg`` and ``.jpeg``.
_FORMAT_EXTENSIONS = {
    "svg": {".svg"},
    "png": {".png"},
    "webp": {".webp"},
    "jpeg": {".jpg", ".jpeg"},
    "gif": {".gif"},
}

#: Fixture docnames (sans suffix) that must each produce a content page and,
#: when Hugo runs, a rendered HTML page. ``index`` is the root/branch section.
FIXTURE_DOCNAMES = ("index", "guide/getting-started", "guide/reference")

#: ``content/``-relative Markdown path expected for each fixture docname under
#: the default (leaf-bundle) layout.
_EXPECTED_CONTENT = {
    "index": "content/_index.md",
    "guide/getting-started": "content/guide/getting-started/index.md",
    "guide/reference": "content/guide/reference/index.md",
}

#: ``content/``-relative Markdown path expected for each fixture docname under
#: the **flat** bundle layout (``doxtr_hugo_bundle_mode="flat"``): leaf pages
#: become ``content/<path>.md`` (no leaf-bundle directory), while the root and
#: branch sections keep their ``_index.md`` so Hugo section ordering survives.
_EXPECTED_CONTENT_FLAT = {
    "index": "content/_index.md",
    "guide/getting-started": "content/guide/getting-started.md",
    "guide/reference": "content/guide/reference.md",
}

#: Hugo pretty-URL path (under ``public/``) for each fixture page's HTML.
_EXPECTED_HTML = {
    "index": "public/index.html",
    "guide/getting-started": "public/guide/getting-started/index.html",
    "guide/reference": "public/guide/reference/index.html",
}

_ERROR_LINE = re.compile(r"^.*\bERROR\b", re.MULTILINE)

#: Hugo logs a missing shortcode (theme lacks ``admonition``/``figure``) with
#: one of these phrases. The matrix uses this to re-run the theme with the
#: raw-HTML admonition path (guide §7.2a: downgrade shortcode → HTML), which is
#: how we surface theme-portability bugs instead of silently passing.
_MISSING_SHORTCODE = re.compile(
    r"failed to extract shortcode|unknown shortcode|"
    r"shortcode .* not found|template for shortcode",
    re.IGNORECASE,
)

#: Directory where matrix themes are vendored (pinned Hugo modules / git
#: submodules per guide §7.2a). Each theme is expected at
#: ``themes/<Theme.dirname>``; when absent the matrix entry records a ``SKIP``
#: with a reason (never a silent pass).
THEMES_DIR = os.path.join(HERE, "themes")


class Theme:
    """One matrix entry describing a vendored Hugo theme (guide §7.2a).

    The matrix is driven by a single :data:`THEMES` table. Each theme is
    vendored (pinned) under :data:`THEMES_DIR` as ``themes/<dirname>``; the
    harness never fetches a floating ``main`` at CI time. Attributes:

    * ``name`` — report label.
    * ``dirname`` — directory under :data:`THEMES_DIR` holding the pinned
      theme (its Hugo ``theme`` name, used verbatim in ``hugo.toml``).
    * ``unsafe`` — the value to write for
      ``markup.goldmark.renderer.unsafe``. Left at the theme's documented
      default so a theme that does **not** enable raw HTML (e.g. Geekdoc)
      flushes out HTML-passthrough assumptions.
    * ``extended`` — True when the theme needs the Hugo **extended** binary
      (SCSS pipeline); when the available ``hugo`` is not extended the entry
      is ``SKIP``-ped with a recorded reason rather than failed.
    """

    __slots__ = ("name", "dirname", "unsafe", "extended")

    def __init__(
        self,
        name: str,
        dirname: str,
        *,
        unsafe: bool,
        extended: bool = False,
    ) -> None:
        self.name = name
        self.dirname = dirname
        self.unsafe = unsafe
        self.extended = extended


#: The pinned multi-theme matrix (guide §7.2a). The built-in skeleton is the
#: baseline (handled separately in :func:`run_hugo_stage`); these are the
#: external themes a real consumer would drop the emitted ``content/**`` into.
#: All five build with the **standard** Hugo 0.140.2 binary (no Hugo-extended,
#: no Go modules). ``unsafe`` is each theme's documented default so portability
#: gaps surface: if a theme ships raw HTML **off** the matrix exercises the
#: shortcode-vs-HTML admonition downgrade path generically (keyed off
#: ``Theme.unsafe``). Themes are vendored (pinned) under ``themes/<dirname>``; a
#: missing vendor dir is a recorded ``SKIP`` so CI pins them as submodules.
THEMES = (
    Theme("Archie", "archie", unsafe=True),
    Theme("hugo-terminal", "hugo-terminal", unsafe=True),
    Theme("Hyde", "hyde", unsafe=True),
    Theme("Risotto", "risotto", unsafe=True),
    Theme("Notepadium", "notepadium", unsafe=True),
)


def hugo_available() -> bool:
    """Return True when a ``hugo`` binary is on PATH."""
    return shutil.which("hugo") is not None


class StageError(Exception):
    """Raised to fail a harness stage with a human-readable reason."""


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run ``cmd`` capturing combined output as text (never raises on nonzero)."""
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **kwargs,
    )


def run_sphinx_stage(
    outdir: str,
    *,
    overrides: dict[str, str] | None = None,
    expected_content: dict[str, str] | None = None,
    expect_skeleton: bool = True,
) -> str:
    """Run the real ``sphinx-build -b hugo`` and assert a coherent site.

    By default this builds the fixture with its own ``conf.py`` settings (the
    leaf-bundle, full-skeleton layout) and asserts the built-in skeleton plus a
    co-located ``index.md`` per page. The keyword arguments let the harness
    exercise the alternate M4 layouts without a second fixture:

    * ``overrides`` — ``{config_key: value}`` pairs passed to ``sphinx-build``
      as ``-D key=value`` (e.g. ``doxtr_hugo_bundle_mode=flat`` or
      ``doxtr_hugo_emit_skeleton=content-only``), so one fixture proves every
      bundle/skeleton mode end to end.
    * ``expected_content`` — the ``content/``-relative Markdown path expected
      for each fixture docname under the chosen layout (defaults to the
      leaf-bundle map :data:`_EXPECTED_CONTENT`).
    * ``expect_skeleton`` — when False (``content-only`` mode), assert the
      skeleton files (``hugo.toml``/``layouts/``) are **absent** instead of
      present, proving the drop-in layout emits only ``content/**``.

    Returns a short detail string on success; raises :class:`StageError` with a
    descriptive message on any failure.
    """
    if expected_content is None:
        expected_content = _EXPECTED_CONTENT
    cmd = [
        sys.executable,
        "-m",
        "sphinx",
        "-b",
        "hugo",
        "-W",  # warnings are errors (strict, as in the roadmap harness)
        "--keep-going",
    ]
    for key, value in (overrides or {}).items():
        cmd += ["-D", f"{key}={value}"]
    cmd += [FIXTURE_DOCS, outdir]
    proc = _run(cmd)
    if proc.returncode != 0:
        raise StageError(
            f"sphinx-build exited {proc.returncode}\n{proc.stdout.strip()}"
        )
    if _ERROR_LINE.search(proc.stdout):
        raise StageError(f"ERROR line(s) in sphinx output:\n{proc.stdout.strip()}")

    skeleton_files = ("hugo.toml", "layouts/_default/baseof.html")
    if expect_skeleton:
        # Built-in skeleton: a self-building site.
        for required in skeleton_files:
            full = os.path.join(outdir, required.replace("/", os.sep))
            if not os.path.isfile(full):
                raise StageError(f"missing skeleton file: {required}")
    else:
        # content-only drop-in: no hugo.toml / layouts/ — only content/**.
        for forbidden in skeleton_files:
            full = os.path.join(outdir, forbidden.replace("/", os.sep))
            if os.path.exists(full):
                raise StageError(
                    f"content-only build leaked a skeleton file: {forbidden}"
                )

    # Every fixture document must produce a non-empty content page with front
    # matter at the layout-specific path under content/.
    for docname in FIXTURE_DOCNAMES:
        rel = expected_content[docname]
        full = os.path.join(outdir, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            raise StageError(f"missing content page for {docname!r}: {rel}")
        text = open(full, encoding="utf-8").read()
        if not text.strip():
            raise StageError(f"empty content page for {docname!r}: {rel}")
        if not text.lstrip().startswith("+++"):
            raise StageError(f"content page {rel} is missing TOML front matter")

    # Format-only assertion (guide §7.2 step 4): content is Markdown only — no
    # stray source formats leaked into the emitted tree.
    for root, _dirs, files in os.walk(os.path.join(outdir, "content")):
        for name in files:
            if name.endswith((".rst", ".adoc", ".txt")):
                rel = os.path.relpath(os.path.join(root, name), outdir)
                raise StageError(f"non-Markdown content file emitted: {rel}")

    return f"built {len(FIXTURE_DOCNAMES)} pages + skeleton under {outdir}"


def verify_formats(outdir: str) -> str:
    """Assert every manifested asset's extension + magic bytes match its format.

    Implements guide §7.2 step 2's format-match contract: the build manifest
    (:data:`doxtr_hugo.assets.MANIFEST_NAME`) lists the chosen format per emitted
    asset; for each record this asserts the file exists, its on-disk extension is
    one the format permits, and its leading bytes sniff to that same format
    (SVG = XML/``<svg``, PNG = ``\x89PNG``, etc.).

    Returns a short detail string. Raises :class:`StageError` on any mismatch,
    missing file, or unrecognized/empty payload. When no manifest is present the
    build emitted no assets, which is reported (not failed) so a diagram-free
    fixture does not spuriously fail the harness.
    """
    manifest_path = os.path.join(outdir, MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        return "no asset manifest (build emitted no diagrams/images)"

    manifest = Manifest.load(outdir)
    records = manifest.records()
    if not records:
        return "asset manifest present but empty"

    for rec in records:
        full = os.path.join(outdir, rec.relpath.replace("/", os.sep))
        if not os.path.isfile(full):
            raise StageError(
                f"manifested asset missing on disk: {rec.relpath} "
                f"(docname={rec.docname!r}, fmt={rec.fmt!r})"
            )
        allowed_exts = _FORMAT_EXTENSIONS.get(rec.fmt)
        if allowed_exts is None:
            raise StageError(
                f"manifest declares unknown format {rec.fmt!r} for {rec.relpath}"
            )
        ext = os.path.splitext(full)[1].lower()
        if ext not in allowed_exts:
            raise StageError(
                f"extension/format mismatch: {rec.relpath} has extension {ext!r} "
                f"but manifest declares format {rec.fmt!r} "
                f"(expected one of {sorted(allowed_exts)})"
            )
        with open(full, "rb") as fh:
            data = fh.read(4096)
        if not data:
            raise StageError(f"manifested asset is empty: {rec.relpath}")
        sniffed = sniff_format(data)
        if sniffed != rec.fmt:
            raise StageError(
                f"magic-byte/format mismatch: {rec.relpath} sniffs as "
                f"{sniffed!r} but manifest declares {rec.fmt!r}"
            )

    return f"verified {len(records)} asset(s) match declared formats (ext + magic bytes)"


def run_hugo_stage(outdir: str) -> str:
    """Run the real ``hugo`` build against the emitted site and assert output.

    Returns a short detail string on success; raises :class:`StageError` on
    failure. The caller only invokes this when :func:`hugo_available` is true.
    """
    public = os.path.join(outdir, "public")
    proc = _run(
        [
            "hugo",
            "--source",
            outdir,
            "--destination",
            public,
            "--logLevel",
            "info",
        ]
    )
    if proc.returncode != 0:
        raise StageError(f"hugo exited {proc.returncode}\n{proc.stdout.strip()}")

    for docname in FIXTURE_DOCNAMES:
        rel = _EXPECTED_HTML[docname]
        full = os.path.join(outdir, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            raise StageError(f"missing rendered HTML for {docname!r}: {rel}")
        if os.path.getsize(full) == 0:
            raise StageError(f"empty rendered HTML for {docname!r}: {rel}")

    # Guide §7.2 step 2: emitted asset formats must match the build manifest
    # (extension + magic bytes). No manifest means the fixture emitted no
    # diagrams/images, which is reported, not failed.
    fmt_detail = verify_formats(outdir)

    return f"rendered {len(FIXTURE_DOCNAMES)} pages to {public}; {fmt_detail}"


def hugo_is_extended() -> bool:
    """Return True when the on-PATH ``hugo`` is the **extended** edition.

    ``hugo version`` prints e.g. ``hugo v0.XXX.0 ... extended`` for the
    extended build. Themes that need a SCSS pipeline (e.g. Docsy) require it;
    when it is absent the matrix records a ``SKIP`` for that entry rather than a
    spurious ``FAIL`` (guide §7.2a). Returns False if ``hugo`` is missing.
    """
    if not hugo_available():
        return False
    proc = _run(["hugo", "version"])
    return "extended" in proc.stdout.lower()


def theme_vendored(theme: "Theme") -> bool:
    """Return True when ``theme`` is vendored under :data:`THEMES_DIR`.

    A vendored theme is a non-empty ``themes/<dirname>`` directory (a pinned
    Hugo module / git submodule, per guide §7.2a). An unvendored theme is a
    recorded ``SKIP`` — never a silent pass — so CI pins the matrix themes
    before the Hugo stage runs.
    """
    path = os.path.join(THEMES_DIR, theme.dirname)
    return os.path.isdir(path) and bool(os.listdir(path))


def _render_theme_hugo_toml(theme: "Theme", *, admonitions_html: bool) -> str:
    """Build a theme-specific ``hugo.toml`` for a matrix entry (guide §7.2a).

    The config sets ``theme`` and ``themesDir`` (pointing at the vendored
    :data:`THEMES_DIR`) and leaves ``markup.goldmark.renderer.unsafe`` at the
    theme's documented default (:attr:`Theme.unsafe`) so a theme that ships raw
    HTML **off** surfaces passthrough gaps. The Goldmark extensions the
    translator relies on are enabled explicitly, exactly as the built-in
    skeleton does, so section/table/footnote rendering does not depend on the
    theme turning them on.

    ``admonitions_html`` is informational only (recorded in a comment): when the
    first build reports a missing shortcode the matrix re-runs Sphinx with
    ``doxtr_hugo_admonitions=html`` so the raw-HTML downgrade path is what Hugo
    renders on that theme.
    """
    from doxtr_hugo.frontmatter import toml_string
    from doxtr_hugo.site_skeleton import _GOLDMARK_EXTENSIONS

    exts = "".join(
        f"      {name} = true\n" for name in _GOLDMARK_EXTENSIONS
    )
    return (
        f"baseURL = {toml_string('/')}\n"
        f"title = {toml_string('doxtr-hugo theme matrix')}\n"
        f"languageCode = {toml_string('en-us')}\n"
        f"theme = {toml_string(theme.dirname)}\n"
        f"themesDir = {toml_string(THEMES_DIR)}\n"
        "\n"
        f"# admonitions rendered as {'raw HTML' if admonitions_html else 'shortcodes'}\n"
        "[markup]\n"
        "  [markup.goldmark]\n"
        "    [markup.goldmark.renderer]\n"
        f"      unsafe = {'true' if theme.unsafe else 'false'}\n"
        "    [markup.goldmark.extensions]\n"
        + exts
    )


def _assert_theme_pages(outdir: str, public: str) -> None:
    """Apply the §7.2 page/image assertions for a single theme build.

    A theme owns its own ``list``/``single`` templates and pretty-URL layout,
    so rather than asserting the skeleton's exact HTML paths this checks that
    **every** fixture page rendered to some non-empty ``index.html`` under
    ``public/`` and that no referenced image is a broken link.
    """
    html_files = []
    for root, _dirs, files in os.walk(public):
        for name in files:
            if name.endswith(".html"):
                html_files.append(os.path.join(root, name))
    if not html_files:
        raise StageError(f"theme build produced no HTML under {public}")
    nonempty = [p for p in html_files if os.path.getsize(p) > 0]
    if not nonempty:
        raise StageError(f"theme build produced only empty HTML under {public}")

    # Every referenced local image must resolve to a file under public/
    # (guide §7.2 step: no broken diagram links).
    img_ref = re.compile(r'(?:<img[^>]+src|<object[^>]+data)="([^"]+)"')
    for html_path in nonempty:
        text = open(html_path, encoding="utf-8", errors="replace").read()
        for ref in img_ref.findall(text):
            if "://" in ref or ref.startswith(("data:", "#", "mailto:")):
                continue  # external / inline / anchor — not a local file
            rel = ref.split("?", 1)[0].split("#", 1)[0].lstrip("/")
            if not rel:
                continue
            target = os.path.join(public, rel.replace("/", os.sep))
            if not os.path.isfile(target):
                raise StageError(
                    f"broken image reference {ref!r} in "
                    f"{os.path.relpath(html_path, public)}"
                )


def run_theme_matrix(
    outdir: str,
) -> list[tuple[str, str, str]]:
    """Build the emitted content against each matrix theme (guide §7.2a).

    ``outdir`` is a site already produced by the Sphinx stage. For each theme
    in :data:`THEMES` this:

    1. ``SKIP``s when ``hugo`` is missing, the theme is not vendored, or the
       theme needs the extended binary and only the standard one is present —
       each with a recorded reason, never a silent pass (guide §7.2a/§7.3);
    2. otherwise copies ``outdir`` into an isolated per-theme build dir, writes
       a theme-specific ``hugo.toml`` (theme set, ``unsafe`` at the theme's
       default), runs ``hugo``, and applies the §7.2 assertions (exit 0, every
       page rendered to non-empty HTML, no broken image references, declared
       formats match the manifest);
    3. when the first build reports a **missing shortcode** (the theme lacks the
       ``admonition``/``figure`` shortcode), re-runs the Sphinx stage with
       ``doxtr_hugo_admonitions=html`` and asserts the raw-HTML downgrade path
       renders cleanly — this is how theme-portability bugs are surfaced.

    Returns a list of ``(stage, status, detail)`` rows, one per theme.
    """
    rows: list[tuple[str, str, str]] = []
    hugo_ok = hugo_available()
    extended_ok = hugo_is_extended()

    for theme in THEMES:
        label = f"theme:{theme.name}"
        if not hugo_ok:
            rows.append((label, "SKIP", "hugo binary not found on PATH (local/dev)"))
            continue
        if not theme_vendored(theme):
            rows.append(
                (
                    label,
                    "SKIP",
                    f"theme not vendored at themes/{theme.dirname} "
                    f"(pin as a Hugo module/submodule per guide §7.2a)",
                )
            )
            continue
        if theme.extended and not extended_ok:
            rows.append(
                (
                    label,
                    "SKIP",
                    "theme requires the Hugo extended edition (SCSS); "
                    "only the standard binary is present",
                )
            )
            continue
        try:
            detail = _build_one_theme(outdir, theme)
            rows.append((label, "PASS", detail))
        except StageError as exc:
            rows.append((label, "FAIL", str(exc)))
    return rows


def _stage_and_build_theme(outdir: str, theme: "Theme") -> tuple[str, str, bool]:
    """Stage the emitted content against ``theme`` and run a real ``hugo`` build.

    Mirrors the consumer path for third-party themes: only ``content/`` + the
    asset manifest + co-located assets are staged (never the skeleton
    ``layouts/``, which would otherwise shadow the theme), and a theme-specific
    ``hugo.toml`` is written. When the first build reports a missing shortcode
    (the theme lacks an ``admonition``/``figure`` shortcode) the content is
    re-emitted with ``doxtr_hugo_admonitions=html`` and rebuilt.

    Returns ``(build_dir, public, admonitions_html)``. The caller owns
    ``build_dir`` and must ``shutil.rmtree`` it. Raises :class:`StageError` on a
    non-zero ``hugo`` exit.
    """
    admonitions_html = False
    build_dir = tempfile.mkdtemp(prefix=f"doxtr-hugo-theme-{theme.dirname}-")
    # First pass: use the content already emitted by the Sphinx stage
    # (shortcode admonitions, the out-of-the-box default).
    _stage_theme_content(outdir, build_dir, theme, admonitions_html=False)
    public = os.path.join(build_dir, "public")
    proc = _run(
        ["hugo", "--source", build_dir, "--destination", public, "--logLevel", "info"]
    )
    if proc.returncode != 0 and _MISSING_SHORTCODE.search(proc.stdout):
        # Downgrade path: re-emit with raw-HTML admonitions and rebuild.
        admonitions_html = True
        shutil.rmtree(build_dir, ignore_errors=True)
        build_dir = tempfile.mkdtemp(prefix=f"doxtr-hugo-theme-{theme.dirname}-html-")
        html_src = tempfile.mkdtemp(prefix="doxtr-hugo-html-content-")
        try:
            run_sphinx_stage(html_src, overrides={"doxtr_hugo_admonitions": "html"})
            _stage_theme_content(html_src, build_dir, theme, admonitions_html=True)
        finally:
            shutil.rmtree(html_src, ignore_errors=True)
        public = os.path.join(build_dir, "public")
        proc = _run(
            [
                "hugo",
                "--source",
                build_dir,
                "--destination",
                public,
                "--logLevel",
                "info",
            ]
        )
    if proc.returncode != 0:
        shutil.rmtree(build_dir, ignore_errors=True)
        raise StageError(
            f"hugo exited {proc.returncode} building theme "
            f"{theme.name!r}\n{proc.stdout.strip()}"
        )
    return build_dir, public, admonitions_html


def _build_one_theme(outdir: str, theme: "Theme") -> str:
    """Run a single vendored theme build + assertions; return a detail string.

    Raises :class:`StageError` on any failure. Handles the shortcode → raw-HTML
    downgrade (guide §7.2a) by re-emitting the content with
    ``doxtr_hugo_admonitions=html`` and rebuilding when the first pass reports a
    missing shortcode.
    """
    build_dir, public, admonitions_html = _stage_and_build_theme(outdir, theme)
    try:
        _assert_theme_pages(build_dir, public)
        verify_formats(build_dir)
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)
    mode = "raw-HTML admonitions" if admonitions_html else "shortcode admonitions"
    return f"{theme.name}: pages + images ok ({mode}, unsafe={theme.unsafe})"


def _theme_output_fingerprint(public: str) -> tuple[str, frozenset[str]]:
    """Return ``(index_md5, css_refs)`` identifying a theme's rendered output.

    ``index_md5`` is the md5 of the rendered ``public/index.html``; ``css_refs``
    is the set of stylesheet ``href`` values referenced from it. Two different
    Hugo themes render the *same* emitted content with different templates and
    different CSS, so a correct theme-staged build yields a distinct fingerprint
    per theme. Byte-identical fingerprints across themes mean the themes were
    shadowed by the skeleton ``layouts/`` — the false positive this guards.
    """
    index = os.path.join(public, "index.html")
    if not os.path.isfile(index):
        raise StageError(f"theme build produced no public/index.html at {public}")
    data = open(index, "rb").read()
    text = data.decode("utf-8", errors="replace")
    css_refs = frozenset(
        re.findall(r'<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"', text)
        + re.findall(r'<link[^>]+href="([^"]+\.css[^"]*)"', text)
    )
    return hashlib.md5(data).hexdigest(), css_refs


def verify_theme_matrix_distinct(outdir: str) -> str:
    """Assert theme-staged builds render DISTINCT output per theme (guide §7.2a).

    The regression this guards against: a preview that stages the full skeleton
    (including ``layouts/_default/``) lets the skeleton SHADOW every ``--theme``
    (Hugo template precedence), so all themes render byte-identical output — a
    silent false positive that looks like "every theme works" while actually no
    theme is exercised. The theme-portable staging path copies only
    ``content/`` + assets (never ``layouts/``), so each theme's own templates
    run and the rendered ``index.html`` differs per theme.

    Builds every vendored, binary-compatible theme and asserts the rendered
    ``index.html`` fingerprints (md5 + referenced CSS) are not all identical.
    Requires at least two buildable themes to make a meaningful comparison;
    raises :class:`StageError` with a reason when fewer are available (the
    caller turns that into a recorded ``SKIP``, never a silent pass) or when the
    outputs collapse to a single fingerprint.
    """
    if not hugo_available():
        raise StageError("hugo binary not found on PATH (local/dev)")
    extended_ok = hugo_is_extended()
    buildable = [
        t
        for t in THEMES
        if theme_vendored(t) and not (t.extended and not extended_ok)
    ]
    if len(buildable) < 2:
        raise StageError(
            "fewer than two vendored, binary-compatible themes available; "
            "cannot compare per-theme output for distinctness "
            "(pin matrix themes as submodules per guide §7.2a)"
        )
    fingerprints: dict[str, tuple[str, frozenset[str]]] = {}
    for theme in buildable:
        build_dir, public, _mode = _stage_and_build_theme(outdir, theme)
        try:
            fingerprints[theme.name] = _theme_output_fingerprint(public)
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)
    md5s = {name: fp[0] for name, fp in fingerprints.items()}
    distinct_md5 = set(md5s.values())
    if len(distinct_md5) < 2:
        raise StageError(
            "theme-staged builds produced IDENTICAL index.html across "
            f"{len(buildable)} themes (md5={next(iter(distinct_md5))}) — the "
            "skeleton layouts/ shadowed every theme; this is the "
            "identical-skeleton-for-every-theme false positive the guard catches"
        )
    return (
        f"{len(buildable)} themes rendered distinctly: "
        + ", ".join(f"{name}={md5[:8]}" for name, md5 in sorted(md5s.items()))
    )


def _stage_theme_content(
    src: str, build_dir: str, theme: "Theme", *, admonitions_html: bool
) -> None:
    """Copy emitted ``content/``/assets from ``src`` into ``build_dir``.

    Only the drop-in parts of the Sphinx output (``content/``, the asset
    manifest + any co-located assets, and the ``layouts/_default/_markup/``
    render hooks) are copied; the built-in ``layouts/_default/`` *page*
    templates and ``layouts/shortcodes/`` are intentionally **not** copied so
    the theme's own templates are exercised. The ``_markup/`` render hook is
    copied because it overrides a theme's hooks without shadowing its page
    templates and is what rewrites bundle-local image refs (``math-<hash>.png``)
    to the authoring page's ``.RelPermalink`` so they resolve when a theme
    inlines full page content on a home/list page. A theme-specific
    ``hugo.toml`` is written on top.
    """
    content_src = os.path.join(src, "content")
    if os.path.isdir(content_src):
        shutil.copytree(content_src, os.path.join(build_dir, "content"))
    markup_src = os.path.join(src, "layouts", "_default", "_markup")
    if os.path.isdir(markup_src):
        shutil.copytree(
            markup_src, os.path.join(build_dir, "layouts", "_default", "_markup")
        )
    manifest_src = os.path.join(src, MANIFEST_NAME)
    if os.path.isfile(manifest_src):
        shutil.copy2(manifest_src, os.path.join(build_dir, MANIFEST_NAME))
    with open(os.path.join(build_dir, "hugo.toml"), "w", encoding="utf-8") as fh:
        fh.write(_render_theme_hugo_toml(theme, admonitions_html=admonitions_html))


def verify_diagrams_rendered(outdir: str) -> str:
    """Assert a diagram-heavy build actually rendered diagram images.

    The regression this guards against: the diagram nodes were silently dropped
    *before* the render stage, so a diagram-rich fixture emitted zero images
    while every other check still passed (0-broken-of-0 wrongly looked green).
    When a diagram backend binary is available (Graphviz ``dot`` ships in the
    reactor/CI image and the fixture's diagram page is built then), this asserts
    the build manifest records at least one ``kind == "diagram"`` asset. When no
    backend binary is present the fixture excludes its diagram page
    (graceful degradation), so this is a recorded ``SKIP`` with a reason — never
    a silent pass.

    Returns a short detail string. Raises :class:`StageError` when a backend is
    present but no diagram asset was emitted.
    """
    manifest_path = os.path.join(outdir, MANIFEST_NAME)
    diagram_assets = []
    if os.path.isfile(manifest_path):
        diagram_assets = [
            r for r in Manifest.load(outdir).records() if r.kind == "diagram"
        ]
    if not diagram_assets:
        raise StageError(
            "diagram backend ('dot') is available and the diagram fixture page "
            "was built, but the build manifest recorded ZERO diagram assets — "
            "the diagram nodes were dropped before the render stage (the "
            "silent-drop regression this guard catches)"
        )
    # Each recorded diagram must exist on disk with non-zero size.
    for rec in diagram_assets:
        full = os.path.join(outdir, rec.relpath.replace("/", os.sep))
        if not os.path.isfile(full) or os.path.getsize(full) == 0:
            raise StageError(
                f"recorded diagram asset missing/empty on disk: {rec.relpath}"
            )
    return f"{len(diagram_assets)} diagram asset(s) rendered and recorded"


def run_harness(outdir: str | None = None) -> list[tuple[str, str, str]]:
    """Run every stage and return a list of ``(stage, status, detail)`` rows.

    ``outdir`` lets a test reuse a known directory; when omitted a temporary
    directory is created and cleaned up.
    """
    results: list[tuple[str, str, str]] = []
    cleanup = False
    if outdir is None:
        outdir = tempfile.mkdtemp(prefix="doxtr-hugo-harness-")
        cleanup = True
    try:
        # --- Sphinx stage -------------------------------------------------
        try:
            detail = run_sphinx_stage(outdir)
            results.append(("sphinx-build -b hugo", "PASS", detail))
            sphinx_ok = True
        except StageError as exc:
            results.append(("sphinx-build -b hugo", "FAIL", str(exc)))
            sphinx_ok = False

        # --- Hugo stage ---------------------------------------------------
        if not sphinx_ok:
            results.append(
                ("hugo build", "SKIP", "skipped: Sphinx stage did not produce a site")
            )
        elif not hugo_available():
            results.append(
                ("hugo build", "SKIP", "hugo binary not found on PATH (local/dev)")
            )
        else:
            try:
                detail = run_hugo_stage(outdir)
                results.append(("hugo build", "PASS", detail))
            except StageError as exc:
                results.append(("hugo build", "FAIL", str(exc)))

        # --- Diagram regression guard ------------------------------------
        # A diagram-heavy build that yields ZERO images is a FAIL (not a
        # 0-broken-of-0 pass). Only asserted when the backend binary is present
        # (the fixture builds its diagram page then); otherwise a recorded SKIP.
        if not sphinx_ok:
            results.append(
                (
                    "diagram render guard",
                    "SKIP",
                    "skipped: Sphinx stage did not produce a site",
                )
            )
        elif shutil.which("dot") is None:
            results.append(
                (
                    "diagram render guard",
                    "SKIP",
                    "graphviz 'dot' not on PATH; diagram fixture page excluded "
                    "(graceful degradation)",
                )
            )
        else:
            try:
                detail = verify_diagrams_rendered(outdir)
                results.append(("diagram render guard", "PASS", detail))
            except StageError as exc:
                results.append(("diagram render guard", "FAIL", str(exc)))

        # --- Multi-theme matrix (guide §7.2a) ----------------------------
        # Build the same emitted content against each pinned theme. Every
        # entry reports PASS/SKIP/FAIL; a missing hugo/theme/extended binary is
        # a recorded SKIP, never a silent pass.
        if sphinx_ok:
            results.extend(run_theme_matrix(outdir))
            # Distinctness guard: the themes must render DIFFERENTLY from each
            # other (and from the skeleton). A missing hugo / too-few vendored
            # themes is a recorded SKIP with the StageError reason, never a
            # silent pass; a collapse to a single fingerprint is a FAIL.
            if not hugo_available():
                results.append(
                    (
                        "theme matrix distinct",
                        "SKIP",
                        "hugo binary not found on PATH (local/dev)",
                    )
                )
            else:
                try:
                    detail = verify_theme_matrix_distinct(outdir)
                    results.append(("theme matrix distinct", "PASS", detail))
                except StageError as exc:
                    msg = str(exc)
                    status = (
                        "SKIP"
                        if "fewer than two" in msg or "not found on PATH" in msg
                        else "FAIL"
                    )
                    results.append(("theme matrix distinct", status, msg))
        else:
            for theme in THEMES:
                results.append(
                    (
                        f"theme:{theme.name}",
                        "SKIP",
                        "skipped: Sphinx stage did not produce a site",
                    )
                )
    finally:
        if cleanup:
            shutil.rmtree(outdir, ignore_errors=True)
    return results


def main() -> int:
    results = run_harness()
    print("=== doxtr-hugo test harness ===")
    failed = False
    for stage, status, detail in results:
        print(f"[{status:4}] {stage} - {detail}")
        if status == "FAIL":
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
