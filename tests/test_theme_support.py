"""Tests for theme_support: profiles, config merge, TOML emit, overlay, README.

Pure-function tests — no Sphinx build. The builder wiring is exercised
end-to-end by the harness/extension-verifier; here we lock the composable units.
"""

from __future__ import annotations

import os

import pytest

from doxtr_hugo import theme_support as ts


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #

def test_get_profile_known_returns_deep_copy():
    a = ts.get_profile("docsy")
    b = ts.get_profile("docsy")
    assert a is not None and a["label"] == "Docsy"
    a["config"]["cascade"][0]["type"] = "mutated"
    # Mutating one copy must not affect another / the registry.
    assert b["config"]["cascade"][0]["type"] == "docs"
    assert ts.PROFILES["docsy"]["config"]["cascade"][0]["type"] == "docs"


def test_get_profile_unknown_is_none():
    assert ts.get_profile("does-not-exist") is None


def test_every_profile_has_a_port_and_source():
    for name, prof in ts.PROFILES.items():
        assert isinstance(prof.get("preview_port"), int), name
        assert prof.get("source", {}).get("kind"), name


# --------------------------------------------------------------------------- #
# deep_merge
# --------------------------------------------------------------------------- #

def test_deep_merge_recurses_mappings():
    base = {"a": {"x": 1, "y": 2}, "k": 1}
    over = {"a": {"y": 20, "z": 3}, "k": 9}
    out = ts.deep_merge(base, over)
    assert out == {"a": {"x": 1, "y": 20, "z": 3}, "k": 9}


def test_deep_merge_list_replaces_not_appends():
    base = {"cascade": [{"type": "docs"}]}
    over = {"cascade": [{"type": "guide"}]}
    assert ts.deep_merge(base, over) == {"cascade": [{"type": "guide"}]}


def test_deep_merge_does_not_mutate_inputs():
    base = {"a": {"x": 1}}
    over = {"a": {"y": 2}}
    ts.deep_merge(base, over)
    assert base == {"a": {"x": 1}} and over == {"a": {"y": 2}}


# --------------------------------------------------------------------------- #
# resolve_theme_config (layer precedence)
# --------------------------------------------------------------------------- #

def test_resolve_layers_core_profile_user():
    profile = ts.get_profile("hugo-book")
    cfg = ts.resolve_theme_config(
        title="Proj",
        base_url="/",
        language_code="en-us",
        profile=profile,
        theme_name="hugo-book",
        user_config={"params": {"BookToC": False, "extra": 1}},
    )
    # core
    assert cfg["baseURL"] == "/" and cfg["title"] == "Proj"
    assert cfg["markup"]["goldmark"]["renderer"]["unsafe"] is True
    # theme key + profile
    assert cfg["theme"] == "hugo-book"
    assert cfg["params"]["BookSection"] == "*"
    # user override wins over profile (BookToC True -> False) and adds keys
    assert cfg["params"]["BookToC"] is False
    assert cfg["params"]["extra"] == 1


def test_resolve_no_theme_is_core_only():
    cfg = ts.resolve_theme_config(
        title="P", base_url="/", language_code="en-us",
        profile=None, theme_name=None, user_config=None,
    )
    assert "theme" not in cfg
    assert cfg["title"] == "P"


# --------------------------------------------------------------------------- #
# dump_toml (general emitter: nested tables + array-of-tables)
# --------------------------------------------------------------------------- #

def test_dump_toml_scalars_and_nested_tables():
    data = {
        "baseURL": "/",
        "title": "T",
        "markup": {"goldmark": {"renderer": {"unsafe": True}}},
    }
    text = ts.dump_toml(data)
    assert 'baseURL = "/"' in text
    assert "[markup.goldmark.renderer]" in text
    assert "unsafe = true" in text


def test_dump_toml_array_of_tables():
    data = {"cascade": [{"type": "docs", "_target": {"path": "/**"}}]}
    text = ts.dump_toml(data)
    assert "[[cascade]]" in text
    assert 'type = "docs"' in text
    assert "[cascade._target]" in text
    assert 'path = "/**"' in text


def test_dump_toml_inline_scalar_array():
    assert 'mainSections = ["a", "b"]' in ts.dump_toml({"mainSections": ["a", "b"]})


def test_dump_toml_skips_redundant_intermediate_headers():
    # A purely-intermediate table (only nested tables) must not emit its own
    # empty header line; the child header carries the dotted path.
    data = {"markup": {"goldmark": {"renderer": {"unsafe": True}}}}
    text = ts.dump_toml(data)
    assert "[markup]\n" not in (text + "\n")
    assert "[markup.goldmark]\n" not in (text + "\n")
    assert "[markup.goldmark.renderer]" in text
    # Still valid/roundtrips.
    tomllib = pytest.importorskip("tomllib")
    assert tomllib.loads(text)["markup"]["goldmark"]["renderer"]["unsafe"] is True


def test_dump_toml_roundtrips_with_tomllib_when_available():
    tomllib = pytest.importorskip("tomllib")
    data = ts.resolve_theme_config(
        title="Proj", base_url="/", language_code="en-us",
        profile=ts.get_profile("docsy"), theme_name="docsy",
        user_config={"params": {"github_repo": "https://x/y"}},
    )
    data = ts.deep_merge(
        data,
        ts.render_module_config("docsy", {"kind": "module", "url": "github.com/google/docsy"}, ts.get_profile("docsy")),
    )
    parsed = tomllib.loads(ts.dump_toml(data))
    assert parsed["theme"] == "docsy"
    assert parsed["cascade"][0]["type"] == "docs"
    assert parsed["params"]["github_repo"] == "https://x/y"
    # module import points at docsy's theme subdir.
    assert parsed["module"]["imports"][0]["path"] == "github.com/google/docsy/theme"


# --------------------------------------------------------------------------- #
# source resolution + module import path
# --------------------------------------------------------------------------- #

def test_resolve_source_profile_default_then_user_override():
    prof = ts.get_profile("archie")
    src = ts.resolve_source(prof, {"kind": "vendor"})
    assert src["kind"] == "vendor"  # user wins
    src2 = ts.resolve_source(prof, {})
    assert src2["kind"] == "module"  # profile default


def test_resolve_source_defaults_to_none():
    assert ts.resolve_source(None, {})["kind"] == "none"


def test_module_import_path_appends_subpath():
    prof = ts.get_profile("docsy")
    assert ts.module_import_path(
        {"kind": "module", "url": "github.com/google/docsy"}, prof
    ) == "github.com/google/docsy/theme"


# --------------------------------------------------------------------------- #
# overlay copy
# --------------------------------------------------------------------------- #

def test_copy_overlay_copies_tree_and_skips_git(tmp_path):
    src = tmp_path / "overlay"
    (src / "layouts" / "partials").mkdir(parents=True)
    (src / "layouts" / "partials" / "head.html").write_text("<meta>")
    (src / "config").mkdir()
    (src / "config" / "overlay.toml").write_text("x = 1")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref: refs/heads/main")
    out = tmp_path / "out"
    out.mkdir()
    written = ts.copy_overlay(str(src), str(out))
    assert "layouts/partials/head.html" in written
    assert "config/overlay.toml" in written
    assert not any(".git" in w for w in written)
    assert (out / "layouts" / "partials" / "head.html").read_text() == "<meta>"


def test_copy_overlay_missing_dir_is_noop(tmp_path):
    assert ts.copy_overlay(str(tmp_path / "nope"), str(tmp_path)) == []


def test_overlay_dir_confdir_relative_vs_absolute(tmp_path):
    assert ts.overlay_dir("/conf", ".doxtr/hugo") == os.path.join("/conf", ".doxtr/hugo")
    assert ts.overlay_dir("/conf", "/abs/over") == "/abs/over"


# --------------------------------------------------------------------------- #
# README generation
# --------------------------------------------------------------------------- #

def test_readme_module_theme_documents_hugo_mod_get():
    prof = ts.get_profile("archie")
    src = ts.resolve_source(prof, {})
    md = ts.render_readme(theme_name="archie", source=src, profile=prof, project="Docs")
    assert "# Docs — Hugo output" in md
    assert "hugo mod get" in md
    assert "hugo server" in md


def test_readme_docsy_documents_dart_sass_and_npm():
    prof = ts.get_profile("docsy")
    src = ts.resolve_source(prof, {})
    md = ts.render_readme(theme_name="docsy", source=src, profile=prof, project="Docs")
    assert "extended" in md
    assert "Dart Sass" in md and "1.105.0" in md
    assert "npm install" in md


def test_readme_no_theme_mentions_fallback():
    md = ts.render_readme(
        theme_name=None, source={"kind": "none"}, profile=None, project="Docs"
    )
    assert "No theme is configured" in md
    assert "hugo server" in md


def test_readme_vendor_source_needs_no_acquisition():
    prof = ts.get_profile("hyde")
    src = ts.resolve_source(prof, {"kind": "vendor"})
    md = ts.render_readme(theme_name="hyde", source=src, profile=prof, project="D")
    assert "vendored" in md
