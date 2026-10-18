"""M2 site-skeleton tests.

Pure-data tests for :mod:`doxtr_hugo.site_skeleton`: the ``hugo.toml`` config,
section ``_index.md`` front matter, shipped layout/shortcode templates, and the
``emit_skeleton`` entry point (full vs. content-only). No Sphinx build or Hugo
binary is required.
"""

from __future__ import annotations

import os

import pytest

from doxtr_hugo import site_skeleton as sk
from doxtr_hugo.toctree import TocModel


# --------------------------------------------------------------------------
# hugo.toml
# --------------------------------------------------------------------------

def test_hugo_toml_has_title_baseurl_and_unsafe():
    text = sk.render_hugo_toml("My Project", "https://example.com/")
    assert 'title = "My Project"' in text
    assert 'baseURL = "https://example.com/"' in text
    # Raw-HTML passthrough is required for HTML admonitions / spanning tables.
    assert "unsafe = true" in text
    assert "[markup.goldmark.renderer]" in text


def test_hugo_toml_enables_translator_goldmark_extensions():
    text = sk.render_hugo_toml("P")
    for ext in ("table", "footnote", "definitionList", "taskList"):
        assert f"{ext} = true" in text


def test_hugo_toml_quotes_special_chars_in_title():
    text = sk.render_hugo_toml('A "quoted" title')
    assert 'title = "A \\"quoted\\" title"' in text


# --------------------------------------------------------------------------
# section _index.md
# --------------------------------------------------------------------------

def test_section_index_toml_front_matter_only():
    text = sk.render_section_index("Guide", 20)
    assert text.startswith("+++")
    assert 'title = "Guide"' in text
    assert "weight = 20" in text
    assert "draft = false" in text


def test_section_index_omits_weight_when_none():
    text = sk.render_section_index("Root", None)
    assert "weight" not in text


def test_section_index_yaml_format():
    text = sk.render_section_index("Guide", 10, fmt="yaml")
    assert text.startswith("---")
    assert "title: Guide" in text


def test_section_index_extra_front_matter_namespaced():
    text = sk.render_section_index("Guide", 10, extra={"source": "x.rst"})
    # Non-reserved keys land under [params].
    assert "[params]" in text
    assert 'source = "x.rst"' in text


# --------------------------------------------------------------------------
# iter_section_indexes
# --------------------------------------------------------------------------

def test_iter_section_indexes_root_and_branches():
    model = TocModel.from_includes(
        "index",
        {"index": ["guide"], "guide": ["install"]},
        titles={"index": "Home", "guide": "Guide", "install": "Install"},
    )
    out = dict(sk.iter_section_indexes(model))
    root_rel = os.path.join("content", "_index.md")
    guide_rel = os.path.join("content", "guide", "_index.md")
    assert root_rel in out
    assert guide_rel in out
    # "install" is a leaf (no children) -> no section index for it.
    install_rel = os.path.join("content", "install", "_index.md")
    assert install_rel not in out
    assert 'title = "Home"' in out[root_rel]
    assert 'title = "Guide"' in out[guide_rel]
    # Branch sections carry their sibling weight; the root does not.
    assert "weight" in out[guide_rel]
    assert "weight" not in out[root_rel]


def test_iter_section_indexes_collapses_index_segment():
    # A ``foo/index`` branch section must land at content/foo/_index.md, not a
    # phantom content/foo/index/_index.md, so the skeleton matches write_doc
    # and the Hugo section nesting mirrors the Sphinx hierarchy.
    model = TocModel.from_includes(
        "index",
        {"index": ["section/index"], "section/index": ["section/child"]},
        titles={"section/index": "Section", "section/child": "Child"},
    )
    out = dict(sk.iter_section_indexes(model))
    collapsed = os.path.join("content", "section", "_index.md")
    phantom = os.path.join("content", "section", "index", "_index.md")
    assert collapsed in out
    assert phantom not in out
    assert 'title = "Section"' in out[collapsed]


# --------------------------------------------------------------------------
# shipped templates
# --------------------------------------------------------------------------

def test_layout_files_are_readable_and_nonempty():
    files = dict(sk.iter_layout_files())
    assert os.path.join("layouts", "_default", "baseof.html") in files or any(
        p.endswith("baseof.html") for p in files
    )
    for relpath, text in files.items():
        assert text.strip(), f"{relpath} is empty"


def test_baseof_defines_block_and_shortcodes_match_contract():
    layouts = dict(sk.iter_layout_files())
    baseof = next(t for p, t in layouts.items() if p.endswith("baseof.html"))
    assert '{{ block "main" . }}' in baseof

    shortcodes = dict(sk.iter_shortcode_files())
    admonition = next(
        t for p, t in shortcodes.items() if p.endswith("admonition.html")
    )
    # The translator emits positional params: {{< admonition type "title" >}}.
    assert ".Get 0" in admonition
    assert ".Inner" in admonition
    figure = next(t for p, t in shortcodes.items() if p.endswith("figure.html"))
    assert '.Get "src"' in figure


def test_render_image_hook_resolves_bundle_resource():
    hooks = dict(sk.iter_render_hook_files())
    hook = next(
        t for p, t in hooks.items() if p.endswith("render-image.html")
    )
    assert hook.strip(), "render-image hook is empty"
    # The hook must resolve a bundle-local ref to the authoring page's resource
    # permalink (so images survive being inlined on home/list pages) and emit
    # an <img>.
    assert ".Page.Resources.GetMatch" in hook
    assert ".RelPermalink" in hook
    assert "<img" in hook


# --------------------------------------------------------------------------
# emit_skeleton (full vs content-only)
# --------------------------------------------------------------------------

def _model():
    return TocModel.from_includes(
        "index",
        {"index": ["guide"], "guide": ["install"]},
        titles={"index": "Home", "guide": "Guide", "install": "Install"},
    )


def test_emit_full_writes_config_layouts_and_sections(tmp_path):
    written = sk.emit_skeleton(
        str(tmp_path), _model(), title="Proj", emit_mode="full"
    )
    assert written
    assert (tmp_path / "hugo.toml").is_file()
    assert (tmp_path / "layouts" / "_default" / "baseof.html").is_file()
    assert (tmp_path / "layouts" / "shortcodes" / "admonition.html").is_file()
    assert (tmp_path / "layouts" / "_default" / "_markup" / "render-image.html").is_file()
    assert (tmp_path / "content" / "_index.md").is_file()
    assert (tmp_path / "content" / "guide" / "_index.md").is_file()


def test_emit_content_only_skips_config_and_page_layouts(tmp_path):
    sk.emit_skeleton(
        str(tmp_path), _model(), title="Proj", emit_mode="content-only"
    )
    assert not (tmp_path / "hugo.toml").exists()
    # The theme-shadowing page templates must NOT be emitted in content-only
    # mode (they would shadow the theme's own _default/ templates).
    assert not (tmp_path / "layouts" / "_default" / "single.html").exists()
    assert not (tmp_path / "layouts" / "_default" / "baseof.html").exists()
    assert not (tmp_path / "layouts" / "shortcodes").exists()
    # The markup render hook IS emitted in content-only mode: it overrides a
    # theme's hooks without shadowing its page templates, and is what makes
    # bundle-local image refs resolve when a theme inlines full page content.
    assert (tmp_path / "layouts" / "_default" / "_markup" / "render-image.html").is_file()
    # Section bundles are still content, so they are still written.
    assert (tmp_path / "content" / "_index.md").is_file()
    assert (tmp_path / "content" / "guide" / "_index.md").is_file()


def test_emit_is_idempotent(tmp_path):
    a = sk.emit_skeleton(str(tmp_path), _model(), title="Proj")
    b = sk.emit_skeleton(str(tmp_path), _model(), title="Proj")
    assert sorted(a) == sorted(b)
    # Re-running does not duplicate or corrupt the config.
    text = (tmp_path / "hugo.toml").read_text()
    assert text.count("baseURL") == 1


def test_emit_uses_configured_front_matter_format(tmp_path):
    sk.emit_skeleton(
        str(tmp_path), _model(), title="Proj", front_matter="yaml"
    )
    idx = (tmp_path / "content" / "_index.md").read_text()
    assert idx.startswith("---")
