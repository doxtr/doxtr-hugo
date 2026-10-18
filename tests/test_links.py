"""M1 link/cross-reference tests.

Each test builds a small multi-document hugo project over RST fixtures and
reads the emitted Markdown, asserting that ``:doc:``/``:ref:``/external links
map to the expected Goldmark output. No external binaries are required.
"""

from __future__ import annotations

import textwrap

from sphinx.application import Sphinx


def _build_project(tmp_path, files, extra_conf=""):
    """Build a hugo project from ``files`` (docname -> rst) and return outdir.

    ``docname`` keys use forward slashes without the ``.rst`` suffix; nested
    directories are created as needed.
    """
    srcdir = tmp_path / "src"
    srcdir.mkdir()
    conf = (
        'project = "T"\n'
        'author = "A"\n'
        'extensions = ["doxtr_hugo"]\n'
        'root_doc = "index"\n'
        'exclude_patterns = ["_build"]\n'
        + extra_conf
    )
    (srcdir / "conf.py").write_text(conf, encoding="utf-8")
    for docname, rst in files.items():
        path = srcdir / (docname + ".rst")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(rst), encoding="utf-8")

    outdir = tmp_path / "out"
    app = Sphinx(
        srcdir=str(srcdir),
        confdir=str(srcdir),
        outdir=str(outdir),
        doctreedir=str(tmp_path / "doctrees"),
        buildername="hugo",
    )
    app.build()
    return outdir


def _read(outdir, docname):
    """Read the Markdown emitted for ``docname`` under ``content/``.

    The builder writes pages under ``content/`` using the slug/bundle layout:
    the root doc → ``content/_index.md``; a branch (toctree parent) →
    ``content/<path>/_index.md``; a leaf → ``content/<path>/index.md``. The
    exact kind is not known to the test, so probe the candidate paths in order.
    """
    if docname == "index":
        candidates = [outdir / "content" / "_index.md"]
    else:
        parts = docname.split("/")
        candidates = [
            outdir.joinpath("content", *parts, "index.md"),
            outdir.joinpath("content", *parts, "_index.md"),
        ]
    for cand in candidates:
        if cand.is_file():
            return cand.read_text(encoding="utf-8")
    raise FileNotFoundError(
        f"no emitted content page for {docname!r}; tried: "
        + ", ".join(str(c) for c in candidates)
    )


# -- external links ---------------------------------------------------------


def test_external_named_link_is_commonmark(tmp_path):
    out = _build_project(
        tmp_path,
        {
            "index": """
            Index
            =====

            See the `Example site <https://example.com/path>`_ here.
            """,
        },
    )
    md = _read(out, "index")
    assert "[Example site](https://example.com/path)" in md


def test_bare_external_url_is_emitted_plain(tmp_path):
    out = _build_project(
        tmp_path,
        {
            "index": """
            Index
            =====

            Visit https://example.org for details.
            """,
        },
    )
    md = _read(out, "index")
    assert "https://example.org" in md
    # An autolinked bare URL should not be wrapped as [url](url).
    assert "[https://example.org]" not in md


def test_mailto_link_is_external(tmp_path):
    out = _build_project(
        tmp_path,
        {
            "index": """
            Index
            =====

            Mail `us <mailto:dev@example.com>`_ now.
            """,
        },
    )
    md = _read(out, "index")
    assert "[us](mailto:dev@example.com)" in md


# -- :doc: cross-references (relref default) --------------------------------


def test_doc_xref_emits_relref(tmp_path):
    out = _build_project(
        tmp_path,
        {
            "index": """
            Index
            =====

            Read the :doc:`guide`.
            """,
            "guide": """
            Guide Title
            ===========

            Body.
            """,
        },
    )
    md = _read(out, "index")
    assert '[Guide Title]({{< relref "guide" >}})' in md


def test_doc_xref_from_nested_doc_recovers_docname(tmp_path):
    out = _build_project(
        tmp_path,
        {
            "index": "Index\n=====\n",
            "sub/a": """
            A
            =

            Back to :doc:`/index` and over to :doc:`/guide`.
            """,
            "guide": "Guide\n=====\n",
        },
    )
    md = _read(out, "sub/a")
    # relref targets the page's logical path. A reference to the root document
    # resolves to the site home ("/"); other docs use their slugged logical
    # path (here unchanged because they are flat top-level docs).
    assert '{{< relref "/" >}}' in md
    assert '{{< relref "guide" >}}' in md


def test_doc_xref_to_index_section_collapses_segment(tmp_path):
    # A ``foo/index`` document is the section page for ``foo/`` (Sphinx's
    # directory-index convention). Its relref target must be the collapsed
    # logical path ``foo`` -- NOT ``foo/index`` -- so the shortcode resolves to
    # the ``content/foo/_index.md`` page that is actually emitted, and the Hugo
    # section nesting matches the Sphinx hierarchy.
    out = _build_project(
        tmp_path,
        {
            "index": """
            Index
            =====

            .. toctree::

               section/index
            """,
            "section/index": """
            Section
            =======

            .. toctree::

               child
            """,
            "section/child": """
            Child
            =====

            See :doc:`/section/index`.
            """,
        },
    )
    md = _read(out, "section/child")
    assert '{{< relref "section" >}}' in md
    assert '{{< relref "section/index" >}}' not in md
    # The section page is emitted at content/section/_index.md (no phantom
    # content/section/index/ directory).
    assert (out / "content" / "section" / "_index.md").is_file()
    assert not (out / "content" / "section" / "index").exists()


def test_toctree_hierarchy_nests_studies_under_doto(tmp_path):
    # End-to-end regression for the dox ``Extensions/Doto/Studies`` hierarchy:
    # a ``foo/foo.rst`` landing page (``ext/doto/doto``) whose toctree includes
    # a ``bar/index`` section (``ext/doto/studies/index``) must nest Studies
    # UNDER Doto, mirroring the Sphinx reading hierarchy. The emitted tree,
    # not the source directory layout, is what Hugo turns into navigation.
    out = _build_project(
        tmp_path,
        {
            "index": """
            Root
            ====

            .. toctree::

               ext/ext
            """,
            "ext/ext": """
            Extensions
            ==========

            .. toctree::

               doto/doto
            """,
            "ext/doto/doto": """
            Doto
            ====

            .. toctree::

               studies/index
            """,
            "ext/doto/studies/index": """
            Studies
            =======

            .. toctree::

               highschool/index
            """,
            "ext/doto/studies/highschool/index": """
            High School
            ===========

            Notes.
            """,
        },
    )
    content = out / "content"
    # Doto owns ext/doto/ (foo/foo landing page collapsed), not ext/doto/doto/.
    assert (content / "ext" / "doto" / "_index.md").is_file()
    assert not (content / "ext" / "doto" / "doto").exists()
    # Studies nests directly under Doto (it is a branch -> owns _index.md).
    assert (content / "ext" / "doto" / "studies" / "_index.md").is_file()
    # High School nests under Studies. It is a leaf here (no toctree children),
    # so it is a leaf bundle collapsed onto the highschool/ directory.
    assert (
        content / "ext" / "doto" / "studies" / "highschool" / "index.md"
    ).is_file()
    # No phantom ``index/`` directories anywhere in the emitted tree.
    assert not any(p.name == "index" and p.is_dir() for p in content.rglob("*"))


def test_leaf_pages_carry_toctree_weight_in_authoring_order(tmp_path):
    # Sibling leaf pages must carry a ``weight`` matching their ``.. toctree::``
    # order so Hugo renders them in the authored sequence rather than its
    # default alphabetical (title/filename) order. Regression: leaf pages
    # previously got no weight, so e.g. "Colored roadmap" sorted before
    # "Getting started".
    out = _build_project(
        tmp_path,
        {
            "index": """
            Root
            ====

            .. toctree::

               getting-started
               csv-files
               structure
               colored-roadmap
            """,
            "getting-started": "Getting started\n===============\n",
            "csv-files": "CSV files and format\n====================\n",
            "structure": "Sections, tasks and rows\n========================\n",
            "colored-roadmap": "Colored roadmap\n===============\n",
        },
    )

    def _weight(docname):
        text = _read(out, docname)
        for line in text.splitlines():
            if line.startswith("weight"):
                # ``weight = 10`` (TOML)
                return int(line.split("=", 1)[1].strip())
        raise AssertionError(f"no weight in front matter for {docname!r}:\n{text}")

    # Weights increase in authoring order, so the authored-first page sorts
    # first and the authored-last page sorts last (not alphabetically).
    w = [
        _weight("getting-started"),
        _weight("csv-files"),
        _weight("structure"),
        _weight("colored-roadmap"),
    ]
    assert w == sorted(w), w
    assert w[0] < w[-1]
    # "colored-roadmap" is authored last, so despite sorting first
    # alphabetically it must have the largest weight of the four.
    assert _weight("colored-roadmap") == max(w)


def test_section_with_toctree_sets_no_list_but_leaf_does_not(tmp_path):
    # A branch section whose body already lists its toctree children emits
    # ``no_list = true`` so section-listing themes (docsy) do not append a
    # duplicate auto-generated child index. Leaf pages (no toctree) must NOT
    # get the flag, and pure content pages keep their theme's default.
    out = _build_project(
        tmp_path,
        {
            "index": """
            Root
            ====

            .. toctree::

               section
            """,
            "section": """
            Section
            =======

            Intro.

            .. toctree::

               leaf
            """,
            "leaf": "Leaf\n====\n\nJust content.\n",
        },
    )
    section_md = _read(out, "section")
    # ``leaf`` nests under ``section`` in the toctree, so it is emitted at
    # content/section/leaf/index.md (toctree-driven path), not content/leaf/.
    leaf_md = (out / "content" / "section" / "leaf" / "index.md").read_text()
    # The section body renders the resolved toctree list, so suppress the
    # theme's duplicate auto-index.
    assert "no_list = true" in section_md
    # The leaf has no toctree -> no suppression flag.
    assert "no_list" not in leaf_md


def test_ref_xref_includes_anchor_fragment(tmp_path):
    out = _build_project(
        tmp_path,
        {
            "index": """
            Index
            =====

            Jump to :ref:`mylabel`.
            """,
            "guide": """
            .. _mylabel:

            Guide Title
            ===========

            Body.
            """,
        },
    )
    md = _read(out, "index")
    assert '{{< relref "guide#mylabel" >}}' in md


# -- relative link style ----------------------------------------------------


def test_doc_xref_relative_style(tmp_path):
    out = _build_project(
        tmp_path,
        {
            "index": """
            Index
            =====

            Read the :doc:`sub/a`.
            """,
            "sub/a": "A\n=\n",
        },
        extra_conf='doxtr_hugo_link_style = "relative"\n',
    )
    md = _read(out, "index")
    # No relref shortcode in relative mode; a plain relative link is used.
    assert "relref" not in md
    assert "[A](sub/a)" in md


def test_relative_style_still_uses_commonmark_for_external(tmp_path):
    out = _build_project(
        tmp_path,
        {
            "index": """
            Index
            =====

            External `Example <https://example.com>`_ link.
            """,
        },
        extra_conf='doxtr_hugo_link_style = "relative"\n',
    )
    md = _read(out, "index")
    assert "[Example](https://example.com)" in md


# -- link label inline markup ----------------------------------------------


def test_internal_link_label_preserves_inline_markup(tmp_path):
    out = _build_project(
        tmp_path,
        {
            "index": """
            Index
            =====

            See :ref:`the guide <styled>`.
            """,
            "guide": """
            .. _styled:

            Guide
            =====

            Body.
            """,
        },
    )
    md = _read(out, "index")
    # Custom xref title renders as the link label.
    assert '[the guide]({{< relref "guide#styled" >}})' in md
