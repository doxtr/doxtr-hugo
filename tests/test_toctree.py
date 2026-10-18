"""M2 toctree-model tests.

Two layers:

* **Pure-data** tests exercise :meth:`TocModel.from_includes` directly from a
  ``parent -> [children]`` mapping, so the section/weight/menu logic is tested
  without a Sphinx build (fast, no binaries).
* **Live-env** tests build a small multi-document hugo project and assert
  :meth:`TocModel.from_env` derives the same structure from the real
  ``env.toctree_includes``/``env.titles`` recorded by Sphinx.
"""

from __future__ import annotations

import textwrap

from sphinx.application import Sphinx

from doxtr_hugo.toctree import WEIGHT_STEP, TocModel


# --------------------------------------------------------------------------
# pure-data model
# --------------------------------------------------------------------------

def test_root_is_always_a_branch():
    model = TocModel.from_includes("index", {})
    root = model.get("index")
    assert root is not None
    assert root.is_branch is True
    assert root.parent is None
    assert root.depth == 0
    assert root.weight == 0


def test_flat_children_get_spaced_weights_in_order():
    model = TocModel.from_includes(
        "index",
        {"index": ["intro", "install", "usage"]},
    )
    assert model.weight_of("intro") == WEIGHT_STEP
    assert model.weight_of("install") == 2 * WEIGHT_STEP
    assert model.weight_of("usage") == 3 * WEIGHT_STEP
    # All children of the root are depth-1 leaves here.
    for dn in ("intro", "install", "usage"):
        node = model.get(dn)
        assert node.parent == "index"
        assert node.depth == 1
        assert node.is_branch is False


def test_nested_toctree_marks_parent_as_branch():
    model = TocModel.from_includes(
        "index",
        {
            "index": ["guide/overview"],
            "guide/overview": ["guide/install", "guide/usage"],
        },
    )
    assert model.is_branch("index") is True
    assert model.is_branch("guide/overview") is True  # has children
    assert model.is_branch("guide/install") is False
    overview = model.get("guide/overview")
    assert overview.depth == 1
    assert overview.parent == "index"
    install = model.get("guide/install")
    assert install.depth == 2
    assert install.parent == "guide/overview"
    assert install.weight == WEIGHT_STEP
    assert model.get("guide/usage").weight == 2 * WEIGHT_STEP


def test_titles_are_resolved_with_docname_fallback():
    model = TocModel.from_includes(
        "index",
        {"index": ["intro"]},
        titles={"index": "Welcome", "intro": "Getting Started"},
    )
    assert model.get("index").title == "Welcome"
    assert model.get("intro").title == "Getting Started"
    # Missing title falls back to the docname.
    model2 = TocModel.from_includes("index", {"index": ["x"]})
    assert model2.get("x").title == "x"


def test_orphans_attach_as_top_level_leaves():
    model = TocModel.from_includes(
        "index",
        {"index": ["intro"]},
        all_docs={"index", "intro", "changelog", "about"},
    )
    # Orphans appended after explicit children, in sorted order.
    about = model.get("about")
    changelog = model.get("changelog")
    assert about is not None and changelog is not None
    assert about.parent == "index"
    assert changelog.parent == "index"
    assert about.depth == 1
    # intro is weight 10; orphans start after it (sorted: about, changelog).
    assert model.weight_of("intro") == WEIGHT_STEP
    assert model.weight_of("about") == 2 * WEIGHT_STEP
    assert model.weight_of("changelog") == 3 * WEIGHT_STEP


def test_cyclic_toctree_does_not_loop_forever():
    # Malformed: a child points back at the root. The guard set keeps each
    # node placed once; the back-edge is recorded but not re-parented.
    model = TocModel.from_includes(
        "index",
        {"index": ["a"], "a": ["index"]},
    )
    assert model.get("a").parent == "index"
    assert model.get("index").parent is None
    # No duplicate nodes, no crash.
    assert set(model.nodes) == {"index", "a"}


def test_branches_and_leaves_are_deterministically_ordered():
    model = TocModel.from_includes(
        "index",
        {
            "index": ["sec/overview", "page"],
            "sec/overview": ["sec/child"],
        },
    )
    assert model.branches() == ["index", "sec/overview"]
    assert model.leaves() == ["page", "sec/child"]
    # ordered() is stable by (depth, weight, docname).
    ordered = [n.docname for n in model.ordered()]
    assert ordered[0] == "index"


def test_membership_and_unknown_lookups():
    model = TocModel.from_includes("index", {"index": ["a"]})
    assert "a" in model
    assert "missing" not in model
    assert model.get("missing") is None
    assert model.weight_of("missing") is None
    assert model.is_branch("missing") is False


# --------------------------------------------------------------------------
# live Sphinx environment
# --------------------------------------------------------------------------

def _build(tmp_path, files, root_doc="index"):
    srcdir = tmp_path / "src"
    srcdir.mkdir()
    conf = (
        'project = "T"\n'
        'author = "A"\n'
        'extensions = ["doxtr_hugo"]\n'
        f'root_doc = "{root_doc}"\n'
        'exclude_patterns = ["_build"]\n'
    )
    (srcdir / "conf.py").write_text(conf, encoding="utf-8")
    for docname, rst in files.items():
        p = srcdir / (docname + ".rst")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(rst), encoding="utf-8")
    app = Sphinx(
        srcdir=str(srcdir),
        confdir=str(srcdir),
        outdir=str(tmp_path / "out"),
        doctreedir=str(tmp_path / "dt"),
        buildername="hugo",
    )
    app.build()
    return app


def test_from_env_matches_resolved_toctree(tmp_path):
    files = {
        "index": """\
            Root
            ====

            .. toctree::
               :maxdepth: 2

               guide/intro
               guide/install
        """,
        "guide/intro": "Intro\n=====\n\ntext\n",
        "guide/install": "Install\n=======\n\ntext\n",
    }
    app = _build(tmp_path, files)
    model = TocModel.from_env(app.env, app.config.root_doc)

    assert model.root == "index"
    assert model.is_branch("index") is True
    assert model.weight_of("guide/intro") == WEIGHT_STEP
    assert model.weight_of("guide/install") == 2 * WEIGHT_STEP
    assert model.get("guide/intro").title == "Intro"
    assert model.get("guide/install").title == "Install"
    # Both are leaves (no further toctrees).
    assert model.leaves() == ["guide/intro", "guide/install"]


def test_from_env_handles_orphan_pages(tmp_path):
    files = {
        "index": """\
            Root
            ====

            .. toctree::

               intro
        """,
        "intro": "Intro\n=====\n\ntext\n",
        # 'extra' is never referenced by a toctree -> orphan.
        "extra": "Extra\n=====\n\ntext\n",
    }
    app = _build(tmp_path, files)
    model = TocModel.from_env(app.env, app.config.root_doc)
    extra = model.get("extra")
    assert extra is not None
    assert extra.parent == "index"
    assert extra.depth == 1
    assert extra.is_branch is False
