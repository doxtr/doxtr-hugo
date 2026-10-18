"""M2 slug tests — docname → Hugo path/slug mapping.

Pure-data tests only: :mod:`doxtr_hugo.slug` takes plain strings and a
:class:`~doxtr_hugo.toctree.TocModel` built from an includes mapping, so no
Sphinx build or external binary is needed.
"""

from __future__ import annotations

import pytest

from doxtr_hugo.slug import (
    content_path,
    relref_target,
    slug_for_docname,
    slugify,
    url_for_docname,
)
from doxtr_hugo.toctree import TocModel


# --------------------------------------------------------------------------
# slugify — Hugo default urlize rules
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Installing the Tool", "installing-the-tool"),
        ("API & CLI reference!", "api-cli-reference"),
        ("  spaced  out  ", "spaced-out"),
        ("already-slugged", "already-slugged"),
        ("MixedCASE", "mixedcase"),
        ("under_score", "under-score"),
        ("dots.and.dots", "dots-and-dots"),
        ("trailing---hyphens---", "trailing-hyphens"),
        ("123 numbers 456", "123-numbers-456"),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_slugify_matches_hugo_defaults(text, expected):
    assert slugify(text) == expected


def test_slug_for_docname_uses_last_segment():
    assert slug_for_docname("guide/Getting Started") == "getting-started"
    assert slug_for_docname("index") == "index"
    assert slug_for_docname("a/b/C D") == "c-d"


# --------------------------------------------------------------------------
# content_path — output file layout
# --------------------------------------------------------------------------

def _model():
    # root(index) -> guide(branch) -> {install(leaf), usage(leaf)}
    return TocModel.from_includes(
        "index",
        {"index": ["guide"], "guide": ["guide/install", "guide/usage"]},
    )


def test_root_maps_to_content_index():
    model = _model()
    assert content_path("index", model) == "content/_index.md"
    # root is identical in flat mode.
    assert content_path("index", model, bundle_mode="flat") == "content/_index.md"


def test_branch_section_maps_to_branch_index_both_modes():
    model = _model()
    assert content_path("guide", model) == "content/guide/_index.md"
    assert (
        content_path("guide", model, bundle_mode="flat")
        == "content/guide/_index.md"
    )


def test_leaf_bundle_default_uses_index_md():
    model = _model()
    assert content_path("guide/install", model) == "content/guide/install/index.md"


def test_flat_mode_uses_flat_file():
    model = _model()
    assert (
        content_path("guide/install", model, bundle_mode="flat")
        == "content/guide/install.md"
    )


def test_content_path_slugifies_segments():
    model = TocModel.from_includes(
        "index", {"index": ["Guide/Install Steps"]}
    )
    assert (
        content_path("Guide/Install Steps", model)
        == "content/guide/install-steps/index.md"
    )


def test_content_path_is_forward_slashed():
    model = _model()
    # No OS-specific separators regardless of platform.
    assert "\\" not in content_path("guide/install", model)


def test_degenerate_docname_still_maps_somewhere():
    # A docname whose segments all slug to empty must not vanish.
    model = TocModel.from_includes("index", {"index": ["!!!"]})
    path = content_path("!!!", model)
    assert path.startswith("content/")
    assert path.endswith(".md")


# --------------------------------------------------------------------------
# trailing ``index`` segment collapse (Sphinx foo/index.rst == foo/ section)
# --------------------------------------------------------------------------

def _index_model():
    # root(index) -> section(branch, authored as section/index) ->
    #   {section/child (leaf), section/sub (branch, authored as section/sub/index)}
    return TocModel.from_includes(
        "index",
        {
            "index": ["section/index"],
            "section/index": ["section/child", "section/sub/index"],
            "section/sub/index": ["section/sub/leaf"],
        },
    )


def test_index_branch_collapses_to_parent_dir():
    # A ``foo/index`` branch is the section page for ``foo/``; it must land at
    # content/foo/_index.md, NOT content/foo/index/_index.md.
    model = _index_model()
    assert content_path("section/index", model) == "content/section/_index.md"
    assert (
        content_path("section/sub/index", model)
        == "content/section/sub/_index.md"
    )


def test_index_leaf_collapses_to_parent_dir():
    # A non-branch ``foo/index`` is the leaf bundle for ``foo/``.
    model = TocModel.from_includes("index", {"index": ["guide/index"]})
    assert content_path("guide/index", model) == "content/guide/index.md"
    assert (
        content_path("guide/index", model, bundle_mode="flat")
        == "content/guide.md"
    )


def test_index_children_nest_under_collapsed_section():
    # Children of a ``foo/index`` section keep their own paths, so they nest
    # directly under content/foo/ (siblings of foo/_index.md), matching Sphinx.
    model = _index_model()
    assert (
        content_path("section/child", model)
        == "content/section/child/index.md"
    )


def test_index_segment_url_and_relref_collapse():
    model = _index_model()
    # URL drops the ``index`` segment too.
    assert url_for_docname("section/index", model) == "/section/"
    assert url_for_docname("section/sub/index", model) == "/section/sub/"
    # relref target is the collapsed logical path (resolves to the emitted page).
    assert relref_target("section/index", model) == "section"
    assert relref_target("section/sub/index", model) == "section/sub"
    assert relref_target("section/child", model) == "section/child"


def test_relref_target_root_is_home():
    model = _index_model()
    assert relref_target("index", model) == "/"


def test_root_named_index_is_not_collapsed():
    # The root document is special-cased by callers, so even though it is named
    # ``index`` it maps to the site root, never to an empty path.
    model = _index_model()
    assert content_path("index", model) == "content/_index.md"
    assert url_for_docname("index", model) == "/"


# --------------------------------------------------------------------------
# toctree-driven hierarchy: output path follows the authored toctree, not the
# source directory layout (``foo/foo.rst`` landing pages, cross-dir nesting)
# --------------------------------------------------------------------------

def test_foo_foo_landing_page_owns_its_directory():
    # The ``foo/foo.rst`` convention (a landing page named after its folder):
    # ``doto/doto`` is the section page for ``doto/`` and must land at
    # content/.../doto/_index.md, NOT content/.../doto/doto/_index.md.
    model = TocModel.from_includes(
        "index",
        {
            "index": ["ext/ext"],
            "ext/ext": ["ext/doto/doto"],
            "ext/doto/doto": ["ext/doto/getting-started"],
        },
    )
    assert content_path("ext/ext", model) == "content/ext/_index.md"
    assert content_path("ext/doto/doto", model) == "content/ext/doto/_index.md"
    # A plain child nests directly under the landing page's directory.
    assert (
        content_path("ext/doto/getting-started", model)
        == "content/ext/doto/getting-started/index.md"
    )
    assert url_for_docname("ext/doto/doto", model) == "/ext/doto/"
    assert relref_target("ext/doto/doto", model) == "ext/doto"


def test_toctree_child_nests_under_parent_not_source_dir():
    # The headline case from the dox project: a ``studies/index`` section that a
    # ``doto/doto`` landing page includes in its toctree must nest UNDER Doto,
    # mirroring the Sphinx reading hierarchy Extensions/Doto/Studies, even
    # though both are ``.rst`` files in the same source directory tree.
    model = TocModel.from_includes(
        "index",
        {
            "index": ["ext/ext"],
            "ext/ext": ["ext/doto/doto"],
            "ext/doto/doto": ["ext/doto/studies/index"],
            "ext/doto/studies/index": ["ext/doto/studies/highschool/index"],
            "ext/doto/studies/highschool/index": [
                "ext/doto/studies/highschool/chemistry/index"
            ],
        },
    )
    # Studies is a child of Doto -> content/ext/doto/studies/_index.md.
    assert (
        content_path("ext/doto/studies/index", model)
        == "content/ext/doto/studies/_index.md"
    )
    # High School nests one level deeper, under Studies (it is a branch because
    # it has a child, so it owns an ``_index.md`` section).
    assert (
        content_path("ext/doto/studies/highschool/index", model)
        == "content/ext/doto/studies/highschool/_index.md"
    )
    # Every node's URL is a child-path of its toctree parent's URL.
    for dn in model.nodes:
        node = model.get(dn)
        if node.parent is None:
            continue
        child_url = url_for_docname(dn, model)
        parent_url = url_for_docname(node.parent, model)
        assert child_url.startswith(parent_url), (dn, child_url, parent_url)


def test_multi_segment_child_keeps_all_levels():
    # A child docname that extends the root by more than one segment keeps all
    # of its path components (regression: only the last segment was kept).
    model = TocModel.from_includes("index", {"index": ["Guide/Install Steps"]})
    assert (
        content_path("Guide/Install Steps", model)
        == "content/guide/install-steps/index.md"
    )
    assert url_for_docname("Guide/Install Steps", model) == (
        "/guide/install-steps/"
    )


# --------------------------------------------------------------------------
# url_for_docname — Hugo pretty URLs
# --------------------------------------------------------------------------

def test_root_url_is_site_root():
    model = _model()
    assert url_for_docname("index", model) == "/"


def test_leaf_and_branch_share_url_shape():
    model = _model()
    assert url_for_docname("guide", model) == "/guide/"
    assert url_for_docname("guide/install", model) == "/guide/install/"


def test_url_slugifies_segments():
    model = TocModel.from_includes("index", {"index": ["Guide/Install Steps"]})
    assert url_for_docname("Guide/Install Steps", model) == "/guide/install-steps/"


def test_url_without_trailing_slash():
    model = _model()
    assert url_for_docname("guide/install", model, trailing_slash=False) == (
        "/guide/install"
    )
