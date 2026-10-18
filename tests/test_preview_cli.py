"""Tests for the _preview_cli shell bridge (single-source-of-truth for preview).

The preview launcher (scripts/preview-themes.sh) reads all theme data through
this CLI so the shipped profiles remain the only place theme knowledge lives.
These tests lock the CLI's contract (stdout shape + merge behavior).
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

from doxtr_hugo import _preview_cli as cli
from doxtr_hugo import theme_support as ts


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, buf.getvalue()


def test_themes_lists_every_profile_ordered_by_port():
    rc, out = _run(["themes"])
    assert rc == 0
    names = out.split()
    assert set(names) == set(ts.PROFILES)
    # ordered by preview_port
    ports = [ts.PROFILES[n]["preview_port"] for n in names]
    assert ports == sorted(ports)


@pytest.mark.parametrize(
    "theme, key, expected",
    [
        ("docsy", "port", "1320"),
        ("docsy", "hugo", "extended"),
        ("docsy", "subpath", "theme"),
        ("docsy", "dart_sass", "1.105.0"),
        ("docsy", "npm", "bootstrap @fortawesome/fontawesome-free"),
        ("archie", "hugo", "standard"),
        ("archie", "subpath", ""),       # no subpath
        ("archie", "dart_sass", ""),     # no toolchain requirement
        ("archie", "npm", ""),
        ("hugo-book", "port", "1319"),
    ],
)
def test_field(theme, key, expected):
    rc, out = _run(["field", theme, key])
    assert rc == 0
    assert out.strip() == expected


def test_field_unknown_theme_errors():
    rc, _ = _run(["field", "nope", "port"])
    assert rc == 2


def test_hugo_toml_merges_profile_config_over_preview_defaults():
    rc, out = _run(["hugo-toml", "docsy", "--sections", "basics,extensions"])
    assert rc == 0
    tomllib = pytest.importorskip("tomllib")
    cfg = tomllib.loads(out)
    # preview-level navigation derived from sections
    assert cfg["theme"] == "docsy"
    assert cfg["mainSections"] == ["basics", "extensions"]
    assert [m["name"] for m in cfg["menu"]["main"]] == ["basics", "extensions"]
    assert cfg["markup"]["goldmark"]["renderer"]["unsafe"] is True
    # profile config fragment wins/augments: docsy's docs cascade
    assert cfg["cascade"][0]["type"] == "docs"
    assert cfg["cascade"][0]["_target"]["path"] == "/**"


def test_hugo_toml_profile_overrides_preview_default():
    # hugo-terminal's profile sets contentTypeName="docs"; it must win over the
    # preview default (first section name).
    rc, out = _run(["hugo-toml", "hugo-terminal", "--sections", "basics,extensions"])
    tomllib = pytest.importorskip("tomllib")
    cfg = tomllib.loads(out)
    assert cfg["params"]["contentTypeName"] == "docs"


def test_hugo_toml_hugo_book_booksection():
    rc, out = _run(["hugo-toml", "hugo-book", "--sections", "basics"])
    tomllib = pytest.importorskip("tomllib")
    cfg = tomllib.loads(out)
    assert cfg["params"]["BookSection"] == "*"
    assert cfg["params"]["BookToC"] is True


def test_hugo_toml_no_sections_still_valid():
    rc, out = _run(["hugo-toml", "archie", "--sections", ""])
    assert rc == 0
    tomllib = pytest.importorskip("tomllib")
    cfg = tomllib.loads(out)
    assert cfg["theme"] == "archie"
    assert "mainSections" not in cfg  # nothing derived without sections


def test_hugo_toml_unknown_theme_errors():
    rc, _ = _run(["hugo-toml", "nope", "--sections", "a"])
    assert rc == 2
