"""Shared pytest fixtures for the doxtr-hugo unit tests.

These fixtures build a minimal, self-contained Sphinx project on disk so tests
can exercise the ``hugo`` builder end to end without any external binaries
(no ``hugo``/``plantuml``/``dot`` required). Later milestones add fixture
documents that exercise individual constructs; M0 only needs a buildable
empty-ish project.
"""

from __future__ import annotations

import pytest

CONF_PY = """\
project = "Test Project"
author = "Test Author"
extensions = ["doxtr_hugo"]
root_doc = "index"
exclude_patterns = ["_build"]
"""

INDEX_RST = """\
Test Project
============

A minimal fixture document.
"""


@pytest.fixture
def minimal_srcdir(tmp_path):
    """Create a minimal Sphinx source tree and return its path."""
    srcdir = tmp_path / "src"
    srcdir.mkdir()
    (srcdir / "conf.py").write_text(CONF_PY, encoding="utf-8")
    (srcdir / "index.rst").write_text(INDEX_RST, encoding="utf-8")
    return srcdir


@pytest.fixture
def hugo_app(minimal_srcdir, tmp_path):
    """Return a Sphinx application configured for the ``hugo`` builder.

    The build is not run here; tests call ``app.build()`` so they can inspect
    both the app and the resulting output directory.
    """
    from sphinx.application import Sphinx

    outdir = tmp_path / "out"
    doctreedir = tmp_path / "doctrees"
    app = Sphinx(
        srcdir=str(minimal_srcdir),
        confdir=str(minimal_srcdir),
        outdir=str(outdir),
        doctreedir=str(doctreedir),
        buildername="hugo",
    )
    return app
