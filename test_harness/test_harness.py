"""Pytest wrappers around the end-to-end harness (``test_runner.py``).

These let ``pytest test_harness`` exercise the real pipeline:

* the **Sphinx stage** always runs (no external binary needed) and asserts the
  emitted built-in-skeleton site is coherent;
* the **Hugo stage** runs only when the ``hugo`` binary is present and is
  ``skip``-ped otherwise (the local/dev path from guide §7.3) so a missing
  binary is a recorded skip, never a silent pass.
"""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from .test_runner import (
    _EXPECTED_CONTENT_FLAT,
    THEMES,
    StageError,
    hugo_available,
    run_hugo_stage,
    run_sphinx_stage,
    run_theme_matrix,
    theme_vendored,
    verify_diagrams_rendered,
    verify_theme_matrix_distinct,
)


@pytest.fixture(scope="module")
def built_site():
    """Run the real Sphinx stage once and yield the emitted site directory."""
    outdir = tempfile.mkdtemp(prefix="doxtr-hugo-harness-")
    try:
        run_sphinx_stage(outdir)  # raises StageError on any failure
        yield outdir
    finally:
        import shutil

        shutil.rmtree(outdir, ignore_errors=True)


def test_sphinx_stage_builds_coherent_skeleton(built_site):
    """The emitted tree is a self-building Hugo site with content + skeleton."""
    assert os.path.isfile(os.path.join(built_site, "hugo.toml"))
    assert os.path.isfile(
        os.path.join(built_site, "layouts", "_default", "baseof.html")
    )
    assert os.path.isfile(os.path.join(built_site, "content", "_index.md"))
    assert os.path.isfile(
        os.path.join(built_site, "content", "guide", "getting-started", "index.md")
    )


@pytest.mark.skipif(not hugo_available(), reason="hugo binary not found on PATH")
def test_hugo_stage_renders_pages(built_site):
    """A real ``hugo`` build renders every fixture page to non-empty HTML."""
    detail = run_hugo_stage(built_site)
    assert "rendered" in detail
    assert os.path.getsize(os.path.join(built_site, "public", "index.html")) > 0


@pytest.mark.skipif(
    shutil.which("dot") is None,
    reason="graphviz 'dot' not on PATH; diagram fixture page excluded",
)
def test_diagram_render_guard_requires_rendered_images(built_site):
    """A diagram-heavy build must record at least one rendered diagram asset.

    Regression guard for the silent-drop bug: with the backend binary present
    the fixture builds a diagram page, so the manifest must carry a
    ``kind == "diagram"`` asset that exists on disk. Zero diagram assets is a
    failure, not a 0-broken-of-0 pass.
    """
    detail = verify_diagrams_rendered(built_site)
    assert "diagram asset" in detail


def test_flat_bundle_mode_emits_flat_files():
    """``doxtr_hugo_bundle_mode=flat`` emits ``content/<path>.md`` leaf pages.

    Proves the M4 flat-bundle layout end to end against a real
    ``sphinx-build -b hugo``: leaf docs become single files (no leaf-bundle
    ``index.md`` directory) while the root/branch sections keep ``_index.md``.
    """
    outdir = tempfile.mkdtemp(prefix="doxtr-hugo-harness-flat-")
    try:
        run_sphinx_stage(
            outdir,
            overrides={"doxtr_hugo_bundle_mode": "flat"},
            expected_content=_EXPECTED_CONTENT_FLAT,
        )
        # Flat leaf pages are single files, not leaf-bundle directories.
        assert os.path.isfile(
            os.path.join(outdir, "content", "guide", "getting-started.md")
        )
        assert not os.path.exists(
            os.path.join(outdir, "content", "guide", "getting-started", "index.md")
        )
        # Root + branch sections still own their section index.
        assert os.path.isfile(os.path.join(outdir, "content", "_index.md"))
    finally:
        import shutil

        shutil.rmtree(outdir, ignore_errors=True)


def test_content_only_mode_omits_skeleton():
    """``doxtr_hugo_emit_skeleton=content-only`` emits ``content/**`` + hooks.

    Proves the M4 content-only drop-in layout end to end: a real
    ``sphinx-build -b hugo`` emits the ``content/`` tree and the markup render
    hook, but **no** ``hugo.toml`` and **no** theme-shadowing page templates
    (``layouts/_default/single.html`` etc.), so consumers drop it into their
    own themed site. The ``layouts/_default/_markup/render-image.html`` hook is
    emitted in this mode on purpose: it overrides a theme's hooks without
    shadowing its page templates and is what makes bundle-local image refs
    (``math-<hash>.png``) resolve when a theme inlines a page's full content on
    a home feed / section list page.
    """
    outdir = tempfile.mkdtemp(prefix="doxtr-hugo-harness-content-only-")
    try:
        run_sphinx_stage(
            outdir,
            overrides={"doxtr_hugo_emit_skeleton": "content-only"},
            expect_skeleton=False,
        )
        assert os.path.isfile(os.path.join(outdir, "content", "_index.md"))
        assert not os.path.exists(os.path.join(outdir, "hugo.toml"))
        # No theme-shadowing page templates.
        assert not os.path.exists(
            os.path.join(outdir, "layouts", "_default", "single.html")
        )
        assert not os.path.exists(
            os.path.join(outdir, "layouts", "_default", "baseof.html")
        )
        assert not os.path.exists(os.path.join(outdir, "layouts", "shortcodes"))
        # The image render hook IS emitted (theme-portable image resolution).
        assert os.path.isfile(
            os.path.join(
                outdir, "layouts", "_default", "_markup", "render-image.html"
            )
        )
    finally:
        import shutil

        shutil.rmtree(outdir, ignore_errors=True)


def _read_toml(path):
    """Parse a TOML file with stdlib tomllib (3.11+); None if unavailable."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        return None
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def test_theme_config_emits_ready_to_serve_site():
    """``doxtr_hugo_theme=<profile>`` emits a theme-configured, serve-ready site.

    End-to-end proof of the theme-configuration feature: a real
    ``sphinx-build -b hugo`` with a shipped theme profile selected writes a
    ``hugo.toml`` composed of core defaults + the profile's config fragment +
    the module import for the theme source, plus a ``README-hugo.md`` that
    documents the remaining one-time human steps. The emitted tree is the
    version-controllable Hugo *input*; the README is the build recipe.
    """
    outdir = tempfile.mkdtemp(prefix="doxtr-hugo-harness-theme-")
    try:
        run_sphinx_stage(
            outdir,
            overrides={"doxtr_hugo_theme": "hugo-book"},
        )
        hugo_toml = os.path.join(outdir, "hugo.toml")
        assert os.path.isfile(hugo_toml)
        parsed = _read_toml(hugo_toml)
        if parsed is not None:  # tomllib available -> assert the merged shape
            # core defaults survive
            assert parsed["markup"]["goldmark"]["renderer"]["unsafe"] is True
            # theme selected
            assert parsed["theme"] == "hugo-book"
            # profile config fragment merged in
            assert parsed["params"]["BookSection"] == "*"
            # module source scaffolded into hugo.toml
            assert (
                parsed["module"]["imports"][0]["path"]
                == "github.com/alex-shpak/hugo-book"
            )
        else:  # fallback substring checks on <3.11
            text = open(hugo_toml, encoding="utf-8").read()
            assert 'theme = "hugo-book"' in text
            assert 'BookSection = "*"' in text

        # README-hugo.md documents the remaining human steps.
        readme = os.path.join(outdir, "README-hugo.md")
        assert os.path.isfile(readme)
        rtext = open(readme, encoding="utf-8").read()
        assert "hugo mod get" in rtext
        assert "hugo server" in rtext
        assert "extended" in rtext  # hugo-book needs extended edition
    finally:
        import shutil

        shutil.rmtree(outdir, ignore_errors=True)


def test_theme_matrix_reports_every_theme(built_site):
    """The multi-theme matrix (guide §7.2a) returns one row per pinned theme.

    Every row is PASS, SKIP, or FAIL — never a silent omission. When ``hugo``
    or a theme is not vendored the row is a recorded ``SKIP`` with a reason, so
    this assertion holds binary-free (the local/dev path) and under CI (pinned
    hugo + vendored themes).
    """
    rows = run_theme_matrix(built_site)
    assert len(rows) == len(THEMES)
    labels = {row[0] for row in rows}
    assert labels == {f"theme:{t.name}" for t in THEMES}
    for stage, status, detail in rows:
        assert status in {"PASS", "SKIP", "FAIL"}, (stage, status)
        assert detail, f"{stage} produced an empty detail string"
        if status == "SKIP":
            # A skip is never silent: it carries a human-readable reason.
            assert detail.strip()


def test_theme_matrix_skips_are_explained(built_site):
    """Binary-free run: with no hugo (local/dev) every theme is an explained SKIP."""
    if hugo_available():
        pytest.skip("hugo present: matrix attempts real builds, not the skip path")
    rows = run_theme_matrix(built_site)
    for _stage, status, detail in rows:
        assert status == "SKIP"
        assert "hugo binary not found" in detail


@pytest.mark.skipif(not hugo_available(), reason="hugo binary not found on PATH")
def test_theme_matrix_builds_vendored_themes(built_site):
    """When hugo + at least one theme are vendored, that theme builds cleanly."""
    if not any(theme_vendored(t) for t in THEMES):
        pytest.skip("no matrix theme vendored under themes/ (pin as submodules)")
    rows = run_theme_matrix(built_site)
    # At least one vendored, non-extended theme must have actually built.
    assert any(status == "PASS" for _s, status, _d in rows), rows


@pytest.mark.skipif(not hugo_available(), reason="hugo binary not found on PATH")
def test_theme_matrix_renders_distinct_output(built_site):
    """Theme-staged builds must render DISTINCT output per theme (guide §7.2a).

    Guards the exact false positive found in manual preview: staging the full
    skeleton (``layouts/_default/``) lets the skeleton shadow every ``--theme``
    so all themes render byte-identical output. The theme-portable staging path
    copies only ``content/`` + assets (never ``layouts/``), so a correct build
    produces a different rendered ``index.html`` md5 per theme. Analogous to the
    zero-images diagram guard: the real-hugo assertion SKIPs (with a reason)
    when fewer than two binary-compatible themes are vendored, never a silent
    pass.
    """
    try:
        detail = verify_theme_matrix_distinct(built_site)
    except StageError as exc:
        if "fewer than two" in str(exc):
            pytest.skip(str(exc))
        raise
    # The detail enumerates per-theme md5 prefixes; a PASS proves they differ.
    assert "rendered distinctly" in detail, detail
