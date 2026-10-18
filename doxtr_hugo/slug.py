"""Docname → Hugo path/slug mapping.

Sphinx identifies every document by a **docname**: a forward-slash separated,
suffix-less path (e.g. ``guide/install``). Hugo organizes content by file path
under ``content/`` and derives a page's URL from that path (plus any ``slug``
front-matter override). This module is the single place that translates a
Sphinx docname into:

* the **content-relative output path** of the Markdown file to write
  (:func:`content_path`), honoring the configured bundle mode; and
* the per-segment **slug** Hugo would compute for a title or path component
  (:func:`slugify`), so front matter and ``:ref:`` anchors agree with Hugo's
  own URL arithmetic.

Bundle modes (``doxtr_hugo_bundle_mode``)
-----------------------------------------
* ``"leaf"`` (default): each document becomes a Hugo **leaf bundle** so its
  rendered diagrams/images co-locate with the page. The root document maps to
  ``content/_index.md`` (the site's branch root); a **branch** docname (a
  toctree section) maps to ``content/<path>/_index.md``; every other document
  maps to ``content/<path>/index.md``. This matches the layout emitted by
  :mod:`doxtr_hugo.site_skeleton` (§3.2 of the guide).
* ``"flat"``: each non-root document becomes a single file
  ``content/<path>.md`` (assets go to a shared ``static/``), for consumers whose
  theme expects a flat content tree. The root still maps to
  ``content/_index.md`` and branch sections still own an ``_index.md`` so Hugo
  section ordering keeps working.

Slugification
-------------
:func:`slugify` reproduces Hugo's default ``urlize`` behavior: lowercase, runs
of non-alphanumeric characters collapse to a single hyphen, and leading/trailing
hyphens are stripped. It is deterministic and dependency-free so front matter
and anchor fragments are stable across builds and match what Hugo computes for
the same input.

The module is intentionally free of Sphinx/builder objects — it takes plain
strings and a :class:`~doxtr_hugo.toctree.TocModel` — so it is cheap to reuse
from the builder, front matter, and links layers and trivial to unit-test.
"""

from __future__ import annotations

import posixpath
import re

from .toctree import TocModel

#: Markdown suffix for emitted pages.
_SUFFIX = ".md"

#: Leaf-bundle branch index filename (a Hugo *section*).
_BRANCH_INDEX = "_index"

#: Leaf-bundle page index filename (a Hugo *leaf bundle*).
_LEAF_INDEX = "index"

#: Matches one or more characters that are not ASCII alphanumerics. Hugo's
#: default slugifier collapses every such run to a single hyphen.
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

#: A docname whose final path segment is exactly this is the *directory index*
#: for its parent folder (the Sphinx ``foo/index.rst`` convention), so the
#: segment is dropped from the Hugo output path/URL: ``foo/index`` becomes the
#: page *for* ``foo/`` rather than a phantom ``foo/index/`` subsection. This
#: mirrors how Sphinx's HTML builder serves ``foo/index.html`` as ``foo/`` and
#: keeps the toctree children of ``foo/index`` nested under ``foo/`` in Hugo.
_INDEX_SEGMENT = "index"


def slugify(text: str) -> str:
    """Return the Hugo default slug for ``text``.

    Mirrors Hugo's built-in ``urlize``/``anchorize`` default: the input is
    lowercased, every run of non-alphanumeric characters becomes a single
    hyphen, and leading/trailing hyphens are stripped. Empty or
    all-punctuation input yields an empty string.

    >>> slugify("Installing the Tool")
    'installing-the-tool'
    >>> slugify("API & CLI reference!")
    'api-cli-reference'
    >>> slugify("  spaced  out  ")
    'spaced-out'
    """
    return _NON_ALNUM.sub("-", text.lower()).strip("-")


def slug_for_docname(docname: str) -> str:
    """Return the slug of a docname's **last** path segment.

    Only the final segment is slugified because Hugo derives a page's own slug
    from its file/dir name, not the whole path. The parent segments contribute
    to the URL separately (see :func:`url_for_docname`).

    >>> slug_for_docname("guide/Getting Started")
    'getting-started'
    >>> slug_for_docname("index")
    'index'
    """
    last = docname.split("/")[-1]
    return slugify(last)


def _slugged_segments(docname: str) -> list[str]:
    """Slugify every segment of a docname, dropping any that slug to empty.

    A trailing ``index`` segment is collapsed (``foo/bar/index`` →
    ``["foo", "bar"]``) so a ``foo/bar/index.rst`` section document maps to the
    ``foo/bar/`` directory itself, not a nested ``foo/bar/index/`` folder. This
    keeps the Hugo section nesting identical to the Sphinx toctree hierarchy
    (and to the Sphinx HTML builder, which serves ``foo/bar/index.html`` at the
    ``foo/bar/`` URL). The root document never reaches this function (it is
    special-cased by the callers), so collapsing ``index`` here cannot swallow
    the site root.

    This is the *path-based* fallback used when no toctree model is available
    (e.g. a stub builder in unit tests). When a :class:`TocModel` is supplied,
    :func:`_toctree_segments` is preferred because it reflects the authored
    toctree hierarchy rather than the source directory layout.
    """
    raw = docname.split("/")
    # Collapse a trailing ``index`` path segment (case-insensitive, matching
    # Sphinx's docname normalisation) before slugifying the rest.
    if len(raw) > 1 and raw[-1].lower() == _INDEX_SEGMENT:
        raw = raw[:-1]
    segments = [slugify(seg) for seg in raw]
    return [seg for seg in segments if seg]


def _collapsed_parts(docname: str) -> list[str]:
    """Return the directory parts a document *represents*, un-slugged.

    Applies the Sphinx directory-index conventions so a document maps to the
    folder it is the index of, rather than a nested folder named after the file:

    * ``foo/index`` → ``["foo"]`` (the ``index.rst`` directory-index);
    * ``foo/foo``   → ``["foo"]`` (the ``foo/foo.rst`` landing-page convention);
    * ``dir/page``  → ``["dir", "page"]`` (an ordinary page);
    * ``guide``     → ``["guide"]``.

    The segments are returned verbatim (not slugified) so callers can compare
    prefixes against other docnames; slugification happens later.
    """
    parts = docname.split("/")
    if len(parts) >= 2 and parts[-1].lower() == _INDEX_SEGMENT:
        return parts[:-1]
    if len(parts) >= 2 and parts[-1] == parts[-2]:
        return parts[:-1]
    return parts


def _own_segments(docname: str, parent_docname: str | None) -> list[str]:
    """Return a node's own slugged path segment(s) relative to its parent.

    Hugo derives nesting from the directory tree, so a child's output path must
    extend its *toctree parent's* path. A node's "represented directory" is its
    docname with the Sphinx directory-index conventions collapsed (see
    :func:`_collapsed_parts`): ``foo/index`` and ``foo/foo`` both represent
    ``foo``, a plain ``guide`` represents ``guide``. This returns the node's
    represented directory with the *parent's* represented directory prefix
    removed, so each ancestor contributes only the level(s) it adds:

    * child ``guide/install`` under parent ``guide`` → ``["install"]``;
    * section ``studies/index`` under parent ``doto/doto`` → ``["studies"]``
      (parent represents ``doto``; ``studies/index`` represents
      ``doto/studies`` only when docnames share that prefix — see below);
    * ``Guide/Install Steps`` under root → ``["guide", "install-steps"]``.

    When the parent's represented directory is *not* a prefix of this node's
    (the toctree crosses directories, e.g. a landing page in one folder
    including a document in a sibling folder), the node's full represented
    directory is used so no component is dropped; Hugo still nests it under the
    parent because the ancestor chain contributes the parent's segments first.
    """
    own = _collapsed_parts(docname)
    parent_repr = _collapsed_parts(parent_docname) if parent_docname else []

    remainder = own
    if parent_repr and own[: len(parent_repr)] == parent_repr:
        remainder = own[len(parent_repr) :]

    segments = [slugify(seg) for seg in remainder]
    segments = [seg for seg in segments if seg]
    if segments:
        return segments

    # Degenerate remainder (parent repr == own repr, or all-punctuation): fall
    # back to the last meaningful docname segment so the node keeps a stable,
    # non-empty directory name distinct from its parent.
    non_index = [p for p in docname.split("/") if p.lower() != _INDEX_SEGMENT]
    fallback = slugify(non_index[-1]) if non_index else slugify(docname.split("/")[-1])
    return [fallback] if fallback else [docname.replace("/", "-")]


def _toctree_segments(
    docname: str, model: TocModel, root_doc: str
) -> list[str] | None:
    """Return the slugged section segments for ``docname`` from the toctree.

    Walks the node's toctree ancestor chain (root excluded) and collects each
    ancestor's :func:`_own_segments`, so the output path mirrors the authored
    ``.. toctree::`` hierarchy rather than the source directory layout. This is
    what makes, e.g., a ``studies/index`` section that a ``doto/doto`` page
    includes nest *under* Doto in Hugo, exactly as it reads in Sphinx.

    Returns ``None`` when ``docname`` is not in the model (the caller then
    falls back to the path-based :func:`_slugged_segments`).
    """
    node = model.get(docname)
    if node is None:
        return None

    # Collect the ancestor chain from this node up to (but excluding) the root.
    chain: list = []
    cur = node
    guard = 0
    limit = len(model.nodes) + 1
    while cur is not None and cur.docname != root_doc:
        chain.append(cur)
        parent = cur.parent
        cur = model.get(parent) if parent is not None else None
        guard += 1
        if guard > limit:  # pragma: no cover - cyclic-toctree guard
            break
    chain.reverse()

    segments: list[str] = []
    for n in chain:
        segments.extend(_own_segments(n.docname, n.parent))
    return segments


def _segments_for(
    docname: str, model: TocModel, root_doc: str
) -> list[str]:
    """Section segments for ``docname``: toctree-driven, path-based fallback."""
    toc = _toctree_segments(docname, model, root_doc)
    if toc is not None:
        return toc or [docname.replace("/", "-")]
    return _slugged_segments(docname) or [docname.replace("/", "-")]


def content_path(
    docname: str,
    model: TocModel,
    *,
    bundle_mode: str = "leaf",
    root_doc: str | None = None,
) -> str:
    """Return the ``content/``-relative output path for ``docname``.

    The path is forward-slash separated (Hugo/portable), suffixed with ``.md``.
    ``model`` decides whether a docname is a branch section (``_index.md``) or a
    leaf page; ``bundle_mode`` selects the leaf vs. flat file layout.

    * root document → ``content/_index.md`` (both modes).
    * branch section → ``content/<path>/_index.md`` (both modes) so Hugo treats
      it as a section regardless of layout style.
    * leaf page, ``bundle_mode="leaf"`` → ``content/<path>/index.md``.
    * leaf page, ``bundle_mode="flat"`` → ``content/<path>.md``.

    ``root_doc`` defaults to ``model.root``. Path segments follow the authored
    **toctree** hierarchy (via :func:`_segments_for`), so a document nests under
    its toctree parent even when the source directory layout differs (e.g. a
    ``foo/foo.rst`` landing page that includes ``bar/index`` in its toctree
    places ``bar`` *under* ``foo`` in the emitted tree).
    """
    root = root_doc if root_doc is not None else model.root
    if docname == root:
        return posixpath.join("content", _BRANCH_INDEX + _SUFFIX)

    segments = _segments_for(docname, model, root)

    if model.is_branch(docname):
        return posixpath.join("content", *segments, _BRANCH_INDEX + _SUFFIX)

    if bundle_mode == "flat":
        *parents, leaf = segments
        return posixpath.join("content", *parents, leaf + _SUFFIX)

    # Default leaf-bundle layout: a directory with an ``index.md``.
    return posixpath.join("content", *segments, _LEAF_INDEX + _SUFFIX)


def url_for_docname(
    docname: str,
    model: TocModel,
    *,
    root_doc: str | None = None,
    trailing_slash: bool = True,
) -> str:
    """Return the Hugo pretty URL (site-relative) for ``docname``.

    Hugo pretty URLs drop the ``index``/``_index`` filename and keep the
    directory path, so both leaf and branch bundles share the same URL shape:
    ``/<slugged/path>/``. The root document maps to ``/``. This is what
    cross-references resolve against (see :mod:`doxtr_hugo.links`).

    ``trailing_slash`` controls whether a trailing ``/`` is appended (Hugo's
    default pretty-URL style keeps it); the root is always ``/``.
    """
    root = root_doc if root_doc is not None else model.root
    if docname == root:
        return "/"
    segments = _segments_for(docname, model, root)
    path = "/" + "/".join(segments)
    if trailing_slash:
        path += "/"
    return path


def relref_target(
    docname: str,
    model: TocModel,
    *,
    root_doc: str | None = None,
) -> str:
    """Return the argument to pass to Hugo's ``{{< relref >}}`` for ``docname``.

    Hugo resolves a ``relref`` against a page's **logical path** (its slugged
    location under ``content/`` with the ``index``/``_index`` filename and
    suffix dropped). The raw Sphinx docname is *not* a safe ``relref`` argument
    once a trailing ``index`` segment is collapsed (``foo/bar/index`` is
    written to ``content/foo/bar/_index.md``, so ``relref "foo/bar/index"``
    would dangle and fail the Hugo build). This returns the collapsed, slugged
    logical path (e.g. ``foo/bar``) so the shortcode resolves to the page that
    was actually emitted. The root document maps to ``/``.

    >>> # doctest uses a tiny model; see tests for realistic cases.
    """
    root = root_doc if root_doc is not None else model.root
    if docname == root:
        return "/"
    segments = _segments_for(docname, model, root)
    return "/".join(segments)
