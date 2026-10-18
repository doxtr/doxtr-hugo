"""Site skeleton: ``hugo.toml``, section ``_index.md``, layouts & shortcodes.

``HugoBuilder.finish()`` turns the emitted ``content/**`` into a **self-building
Hugo project** so the output is immediately previewable and the test harness can
run a real ``hugo`` build with **no external theme** (guide §6). This module
owns that skeleton:

* :func:`render_hugo_toml` — a minimal, standard-binary ``hugo.toml`` with
  ``baseURL``/``title`` from the Sphinx project, raw-HTML passthrough
  (``markup.goldmark.renderer.unsafe = true``) so admonition/table HTML
  survives, and the Goldmark extensions the translator relies on (tables,
  footnotes, definition lists, task lists) explicitly enabled.
* :func:`render_section_index` — a branch-bundle ``_index.md`` (front matter
  only) for every toctree section, carrying its ``title``/``weight`` so section
  ordering is deterministic under any theme.
* :func:`iter_layout_files` / :func:`iter_shortcode_files` — the shipped
  fallback ``layouts/`` templates (``baseof``/``single``/``list``) and the
  ``admonition``/``figure`` shortcodes, read from the packaged ``templates/``
  tree so the content renders even when no theme is installed.
* :func:`emit_skeleton` — the single entry point used by the builder: given the
  output directory, the resolved config, and the :class:`~doxtr_hugo.toctree.TocModel`,
  it writes everything the chosen ``doxtr_hugo_emit_skeleton`` mode requires.

Design notes
------------
* Templates are **package data** (``doxtr_hugo/templates/``) rather than inline
  strings, so a consumer can inspect or override them and so the layouts stay
  readable as real Hugo templates. They are discovered with
  :mod:`importlib.resources`, which works both from a source checkout and from
  an installed wheel.
* The module takes no Sphinx/builder object: it is driven by plain values
  (title, base URL, front-matter format) and the pure-data :class:`TocModel`,
  so it is cheap to unit-test without a build.
* ``doxtr_hugo_emit_skeleton`` gates the output: ``"full"`` (default) writes
  ``hugo.toml`` + ``layouts/`` + section indexes; ``"content-only"`` writes only
  the section ``_index.md`` files (the consumer supplies their own site/theme).
"""

from __future__ import annotations

import os
from typing import Iterable, Iterator, Mapping, Tuple

from .frontmatter import FrontMatter, toml_string
from .slug import content_path
from .toctree import TocModel

#: Package-relative directory holding the shipped Hugo templates.
_TEMPLATES_PKG = "doxtr_hugo.templates"

#: Goldmark extensions the translator depends on, enabled in ``hugo.toml`` so a
#: zero-theme build renders tables/footnotes/definition-lists/task-lists.
_GOLDMARK_EXTENSIONS = ("table", "footnote", "definitionList", "taskList")


# --------------------------------------------------------------------------- #
# hugo.toml
# --------------------------------------------------------------------------- #

def render_hugo_toml(
    title: str,
    base_url: str = "/",
    *,
    language_code: str = "en-us",
) -> str:
    """Render a minimal, standard-binary ``hugo.toml``.

    The config intentionally uses only features available in the **standard**
    (non-extended) Hugo binary:

    * ``baseURL`` / ``title`` / ``languageCode`` at the top level;
    * ``markup.goldmark.renderer.unsafe = true`` so the raw-HTML the translator
      emits (HTML admonitions, spanning-cell tables) is not stripped;
    * the Goldmark extensions the translator relies on, enabled explicitly so
      the build does not depend on a theme turning them on.
    """
    exts = "\n".join(
        f"{name} = true" for name in _GOLDMARK_EXTENSIONS
    )
    return (
        f"baseURL = {toml_string(base_url)}\n"
        f"title = {toml_string(title)}\n"
        f"languageCode = {toml_string(language_code)}\n"
        "\n"
        "[markup]\n"
        "  [markup.goldmark]\n"
        "    [markup.goldmark.renderer]\n"
        "      # Raw HTML (HTML admonitions, spanning-cell tables) must survive.\n"
        "      unsafe = true\n"
        "    [markup.goldmark.extensions]\n"
        + "".join(f"      {line}\n" for line in exts.splitlines())
    )


# --------------------------------------------------------------------------- #
# section _index.md
# --------------------------------------------------------------------------- #

def render_section_index(
    title: str,
    weight: int | None = None,
    *,
    fmt: str = "toml",
    extra: Mapping[str, object] | None = None,
) -> str:
    """Render a branch-bundle ``_index.md`` (front matter only).

    A Hugo section is a directory with an ``_index.md``; its front matter
    carries the section ``title`` and ``weight`` so ordering is deterministic
    regardless of the theme. The body is intentionally empty — the section's
    page list is produced by the theme's ``list`` template (or the shipped
    fallback) from its child pages.
    """
    fm = FrontMatter({"title": title, "draft": False})
    if weight is not None:
        fm.set("weight", weight)
    if extra:
        fm.update(extra)
    return fm.render(fmt) + "\n"


def iter_section_indexes(
    model: TocModel,
    *,
    fmt: str = "toml",
    extra: Mapping[str, object] | None = None,
    skip_docnames: Iterable[str] = (),
) -> Iterator[Tuple[str, str]]:
    """Yield ``(content_relpath, text)`` for every branch section's ``_index.md``.

    The content path mirrors the leaf-bundle layout the builder emits: the root
    branch is ``content/_index.md``; every other branch section lives at
    ``content/<docname>/_index.md``. Leaf pages are **not** emitted here — they
    are written by :meth:`HugoBuilder.write_doc`.

    ``skip_docnames`` lists branch docnames that are **also real Sphinx
    documents** already written (body + front matter) by ``write_doc``; their
    section ``_index.md`` is skipped here so the page body is not clobbered. A
    standalone skeleton emit (no real pages) passes an empty set and gets a
    section index for every branch, keeping that path idempotent.

    The content path is computed by :func:`doxtr_hugo.slug.content_path` (the
    single source of truth also used by ``write_doc``), so a trailing ``index``
    docname segment is collapsed into its parent directory identically here and
    there — a ``foo/bar/index`` section lands at ``content/foo/bar/_index.md``,
    never a phantom ``content/foo/bar/index/_index.md``.
    """
    skip = set(skip_docnames)
    for docname in model.branches():
        if docname in skip:
            continue
        node = model.get(docname)
        if node is None:
            continue
        if docname == model.root:
            weight = None  # the root section has no meaningful sibling weight
        else:
            weight = node.weight
        # ``content_path`` returns a forward-slash, ``content/``-relative path
        # (always ``_index.md`` for a branch); normalise to the OS separator.
        rel = content_path(docname, model, root_doc=model.root)
        relpath = os.path.join(*rel.split("/"))
        yield relpath, render_section_index(
            node.title, weight, fmt=fmt, extra=extra
        )


# --------------------------------------------------------------------------- #
# shipped templates (package data)
# --------------------------------------------------------------------------- #

def _read_template(relpath: str) -> str:
    """Read a packaged template file as text.

    ``relpath`` is forward-slash separated and relative to the ``templates/``
    package (e.g. ``"layouts/_default/baseof.html"``). Uses
    :mod:`importlib.resources` so it works from a source tree or a wheel.
    """
    parts = relpath.split("/")
    try:
        from importlib.resources import files  # Python 3.9+

        resource = files(_TEMPLATES_PKG)
        for part in parts:
            resource = resource / part
        return resource.read_text(encoding="utf-8")
    except Exception:
        # Fallback for very old importlib.resources: resolve via package dir.
        here = os.path.dirname(os.path.abspath(__file__))
        full = os.path.join(here, "templates", *parts)
        with open(full, encoding="utf-8") as fh:
            return fh.read()


#: Shipped fallback layout templates, as output-relative paths.
_LAYOUT_FILES = (
    "layouts/_default/baseof.html",
    "layouts/_default/single.html",
    "layouts/_default/list.html",
)

#: Shipped markup render hooks. Unlike the ``_default/`` page templates above,
#: a site-level markup render hook takes precedence over a theme's own hooks
#: WITHOUT shadowing the theme's page templates, so it is emitted in BOTH
#: ``full`` and ``content-only`` modes. (Hugo resolves image render hooks at
#: ``layouts/_default/_markup/render-image.html``; the enclosing ``_default/``
#: dir holding only ``_markup/`` does not shadow a theme's ``_default/single``
#: etc.) The image hook rewrites bundle-local image references
#: (``math-<hash>.png`` etc.) to the authoring page's resource ``.RelPermalink``
#: so they resolve even when a theme inlines the page's full content on a
#: different URL (home feed / section list).
_RENDER_HOOK_FILES = (
    "layouts/_default/_markup/render-image.html",
)

#: Shipped shortcodes matching the translator's emitted shortcode contract.
_SHORTCODE_FILES = (
    "layouts/shortcodes/admonition.html",
    "layouts/shortcodes/figure.html",
)


def iter_layout_files() -> Iterator[Tuple[str, str]]:
    """Yield ``(output_relpath, text)`` for every shipped fallback layout."""
    for relpath in _LAYOUT_FILES:
        yield relpath, _read_template(relpath)


def iter_render_hook_files() -> Iterator[Tuple[str, str]]:
    """Yield ``(output_relpath, text)`` for every shipped markup render hook."""
    for relpath in _RENDER_HOOK_FILES:
        yield relpath, _read_template(relpath)


def iter_shortcode_files() -> Iterator[Tuple[str, str]]:
    """Yield ``(output_relpath, text)`` for every shipped shortcode."""
    for relpath in _SHORTCODE_FILES:
        yield relpath, _read_template(relpath)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def emit_skeleton(
    outdir: str,
    model: TocModel,
    *,
    title: str,
    base_url: str = "/",
    front_matter: str = "toml",
    emit_mode: str = "full",
    extra_section_front_matter: Mapping[str, object] | None = None,
    skip_section_docnames: Iterable[str] = (),
) -> list[str]:
    """Write the site skeleton under ``outdir`` and return the paths written.

    ``emit_mode`` selects how much is written:

    * ``"full"`` (default): ``hugo.toml`` + ``layouts/`` (fallback templates and
      shortcodes) + every section ``_index.md``. The result is a self-building
      Hugo site.
    * ``"content-only"``: only the section ``_index.md`` files, for dropping the
      emitted ``content/**`` into an existing Hugo site/theme.

    ``skip_section_docnames`` lists branch docnames whose section ``_index.md``
    was already written by ``HugoBuilder.write_doc`` (branch pages that are real
    documents); those are not re-emitted here so their page body survives. It
    defaults to empty, so a standalone skeleton emit stays fully idempotent and
    writes a section index for every branch.

    All writes are idempotent (plain overwrites), so re-running a half-finished
    build is safe. Returns the list of absolute paths written, for logging and
    manifests.
    """
    written: list[str] = []

    def _write(relpath: str, text: str) -> None:
        full = os.path.join(outdir, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
        written.append(full)

    if emit_mode == "full":
        _write("hugo.toml", render_hugo_toml(title, base_url))
        for relpath, text in iter_layout_files():
            _write(relpath, text)
        for relpath, text in iter_shortcode_files():
            _write(relpath, text)

    # Markup render hooks are emitted in BOTH modes: a ``_markup/`` hook merges
    # with (and overrides) a theme's own hooks without shadowing its page
    # templates, and the image hook is what makes bundle-local image refs
    # resolve when a theme inlines full page content elsewhere (home/list).
    for relpath, text in iter_render_hook_files():
        _write(relpath, text)

    # Section indexes are emitted in both modes: they are *content*, and a
    # content-only drop-in still needs its section bundles. Branch sections
    # that are also real documents are already written by ``write_doc`` and are
    # excluded via ``skip_section_docnames`` so their body is not clobbered.
    for relpath, text in iter_section_indexes(
        model,
        fmt=front_matter,
        extra=extra_section_front_matter,
        skip_docnames=skip_section_docnames,
    ):
        _write(relpath, text)

    return written
