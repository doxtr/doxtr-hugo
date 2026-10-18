"""M1 front-matter tests: TOML/YAML/JSON serialization, reserved-key routing,
and deterministic key order. No external binaries required.
"""

from __future__ import annotations

import json

import pytest

from doxtr_hugo.frontmatter import (
    PARAMS_KEY,
    RESERVED_KEYS,
    VALID_FORMATS,
    FrontMatter,
)


def test_reserved_keys_stay_top_level_others_go_to_params():
    fm = FrontMatter(
        {
            "title": "Installing",
            "weight": 20,
            "slug": "install",
            "doxtr_source": "guide/install.rst",
            "doxtr_hugo_version": "0.1.0",
        }
    )
    ordered = fm.as_ordered_dict()
    assert ordered["title"] == "Installing"
    assert ordered["weight"] == 20
    assert ordered["slug"] == "install"
    # Non-reserved keys are namespaced.
    assert ordered[PARAMS_KEY] == {
        "doxtr_hugo_version": "0.1.0",
        "doxtr_source": "guide/install.rst",
    }
    assert "doxtr_source" not in ordered


def test_explicit_params_mapping_is_merged():
    fm = FrontMatter(
        {"title": "T", PARAMS_KEY: {"a": 1}, "b": 2}
    )
    assert fm.as_ordered_dict()[PARAMS_KEY] == {"a": 1, "b": 2}


def test_reserved_key_order_is_canonical_not_insertion_order():
    # Provide keys out of canonical order; output must follow RESERVED_KEYS.
    fm = FrontMatter({"weight": 1, "title": "X", "draft": False})
    keys = list(fm.as_ordered_dict().keys())
    assert keys == ["title", "weight", "draft"]
    # And that order matches the declared canonical ordering.
    assert keys == [k for k in RESERVED_KEYS if k in {"title", "weight", "draft"}]


def test_params_keys_are_sorted_for_stability():
    fm = FrontMatter({"z_key": 1, "a_key": 2, "m_key": 3})
    assert list(fm.as_ordered_dict()[PARAMS_KEY].keys()) == [
        "a_key",
        "m_key",
        "z_key",
    ]


def test_none_values_are_dropped():
    fm = FrontMatter({"title": "T", "description": None, "extra": None})
    ordered = fm.as_ordered_dict()
    assert "description" not in ordered
    assert PARAMS_KEY not in ordered  # extra was None -> params empty -> omitted


def test_toml_render_default_delimiters_and_params_table():
    fm = FrontMatter(
        {
            "title": "Installing",
            "weight": 20,
            "slug": "install",
            "draft": False,
            "doxtr_source": "guide/install.rst",
        }
    )
    out = fm.render("toml")
    assert out.startswith("+++\n")
    assert out.endswith("+++\n")
    assert 'title = "Installing"' in out
    assert "weight = 20" in out
    assert "draft = false" in out
    assert "[params]" in out
    assert 'doxtr_source = "guide/install.rst"' in out
    # Scalars precede the table (TOML requirement).
    assert out.index("title = ") < out.index("[params]")


def test_toml_is_deterministic_across_calls():
    data = {"title": "T", "weight": 2, "b": 1, "a": 2}
    assert FrontMatter(data).render("toml") == FrontMatter(data).render("toml")


def test_toml_string_escaping():
    fm = FrontMatter({"title": 'a "quote" and \\ slash'})
    out = fm.render("toml")
    assert '\\"quote\\"' in out
    assert "\\\\ slash" in out


def test_yaml_render_delimiters_and_content():
    fm = FrontMatter({"title": "Installing", "weight": 20, "custom": "v"})
    out = fm.render("yaml")
    assert out.startswith("---\n")
    assert out.rstrip("\n").endswith("---")
    assert "title: Installing" in out
    assert "weight: 20" in out
    # Namespaced under params.
    assert PARAMS_KEY in out
    assert "custom: v" in out


def test_json_render_is_bare_object_and_parses():
    fm = FrontMatter({"title": "Installing", "weight": 20, "custom": "v"})
    out = fm.render("json")
    parsed = json.loads(out)
    assert parsed["title"] == "Installing"
    assert parsed["weight"] == 20
    assert parsed[PARAMS_KEY] == {"custom": "v"}
    # Canonical order preserved (reserved first, then params).
    assert list(parsed.keys()) == ["title", "weight", PARAMS_KEY]


def test_unknown_format_raises():
    with pytest.raises(ValueError):
        FrontMatter({"title": "T"}).render("xml")


def test_valid_formats_round_trip_via_render():
    fm = FrontMatter({"title": "T", "weight": 1})
    for fmt in VALID_FORMATS:
        rendered = fm.render(fmt)
        assert "T" in rendered


def test_yaml_fallback_matches_without_pyyaml(monkeypatch):
    # Force the ImportError path so the hand-rolled emitter is exercised.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("forced for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    fm = FrontMatter({"title": "Installing", "weight": 20, "custom": "v"})
    out = fm.render("yaml")
    assert out.startswith("---\n")
    assert "title: Installing" in out
    assert "weight: 20" in out
    assert "custom: v" in out
