"""M0 scaffold tests: the extension loads, registers the builder and config,
and ``sphinx-build -b hugo`` runs to completion emitting an output tree.

These tests require no external binaries (no hugo/plantuml/dot).
"""

from __future__ import annotations

import os

import doxtr_hugo
from doxtr_hugo.builder import HugoBuilder
from doxtr_hugo.config_defaults import DEFAULT_CONFIG


def test_version_is_a_string():
    assert isinstance(doxtr_hugo.__version__, str)
    assert doxtr_hugo.__version__


def test_setup_returns_metadata():
    """setup(app) returns the expected parallel-safe extension metadata."""

    class _FakeApp:
        def __init__(self):
            self.builders = []
            self.config_values = {}

        def add_builder(self, builder):
            self.builders.append(builder)

        def add_config_value(self, name, default, rebuild):
            self.config_values[name] = (default, rebuild)

        def connect(self, event, callback):
            # setup() wires a config-inited handler to exclude the theme
            # overlay from Sphinx's doc scan; record it so the stub is complete.
            self.connected = getattr(self, "connected", [])
            self.connected.append((event, callback))

    app = _FakeApp()
    meta = doxtr_hugo.setup(app)

    assert HugoBuilder in app.builders
    assert set(app.config_values) == set(DEFAULT_CONFIG)
    for name, (default, rebuild) in app.config_values.items():
        assert default == DEFAULT_CONFIG[name]
        assert rebuild == "env"
    assert meta["version"] == doxtr_hugo.__version__
    assert meta["parallel_read_safe"] is True
    assert meta["parallel_write_safe"] is True


def test_default_config_keys_are_namespaced():
    for key in DEFAULT_CONFIG:
        assert key.startswith("doxtr_hugo_")


def test_builder_identity():
    assert HugoBuilder.name == "hugo"
    assert HugoBuilder.format == "markdown"
    assert HugoBuilder.out_suffix == ".md"


def test_hugo_build_runs_and_emits_tree(hugo_app):
    """The full build runs green and produces an output directory."""
    hugo_app.build()
    assert hugo_app.builder.name == "hugo"
    assert os.path.isdir(hugo_app.outdir)


def test_get_target_uri_maps_docnames(hugo_app):
    builder = hugo_app.builder
    # root doc maps to the site root
    assert builder.get_target_uri(hugo_app.config.root_doc) == ""
    # other docs map to a pretty-URL path
    assert builder.get_target_uri("guide/install") == "guide/install/"


def _hugo_app_with_overrides(minimal_srcdir, tmp_path, overrides):
    """Build (without running) a ``hugo`` Sphinx app with config overrides."""
    from sphinx.application import Sphinx

    return Sphinx(
        srcdir=str(minimal_srcdir),
        confdir=str(minimal_srcdir),
        outdir=str(tmp_path / "out"),
        doctreedir=str(tmp_path / "doctrees"),
        buildername="hugo",
        confoverrides=overrides,
    )


def test_content_only_defaults_admonitions_to_html(minimal_srcdir, tmp_path):
    """content-only emit flips the default shortcode admonitions to raw HTML.

    A content-only drop-in lands in a consumer theme that does not ship the
    ``admonition`` shortcode, so the portable raw-HTML form is the correct
    default (otherwise the theme build fails on a missing shortcode).
    """
    app = _hugo_app_with_overrides(
        minimal_srcdir, tmp_path, {"doxtr_hugo_emit_skeleton": "content-only"}
    )
    assert app.config.doxtr_hugo_admonitions == "html"


def test_full_emit_keeps_shortcode_admonitions_default(minimal_srcdir, tmp_path):
    """The default ``full`` emit ships the shortcode, so it stays the default."""
    app = _hugo_app_with_overrides(minimal_srcdir, tmp_path, {})
    assert app.config.doxtr_hugo_admonitions == "shortcode"


def test_content_only_respects_explicit_html(minimal_srcdir, tmp_path):
    """An explicit ``html`` is already portable and is left untouched."""
    app = _hugo_app_with_overrides(
        minimal_srcdir,
        tmp_path,
        {
            "doxtr_hugo_emit_skeleton": "content-only",
            "doxtr_hugo_admonitions": "html",
        },
    )
    assert app.config.doxtr_hugo_admonitions == "html"
