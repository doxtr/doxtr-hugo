"""Cross-reference and link resolution for the Hugo translator.

Sphinx resolves ``:doc:``/``:ref:``/internal xrefs *before* the builder writes
output, so by the time :class:`~doxtr_hugo.translator.HugoTranslator` walks a
document every reference is a :class:`docutils.nodes.reference` carrying a final
``refuri``:

* **External** links have an absolute ``refuri`` (``http:``, ``https:``,
  ``mailto:``, ...) and no ``internal`` flag. They become plain CommonMark
  links ``[text](url)``.
* **Internal** links have ``internal = True`` and a ``refuri`` that is a path
  relative to the *current* document (produced by the builder's
  ``get_relative_uri``), optionally suffixed with a ``#fragment`` anchor.

Two output styles are supported, selected by ``doxtr_hugo_link_style``:

``relref`` (default)
    Emit ``[text]({{< relref "guide/install" >}})`` (or ``...#anchor``). Hugo
    validates the target at build time, so a dangling cross-reference *fails*
    the Hugo build — the desirable behaviour for CI (guide §5.2).

``relative``
    Emit a plain relative Markdown link ``[text](../guide/)`` for
    theme-agnostic portability.

Recovering the **target docname** from a relative ``refuri`` is done
authoritatively: the builder knows every docname and can compute the relative
URI from the current document to each one, so the translator inverts that exact
mapping rather than guessing from path arithmetic. This correctly handles the
root document (which maps to the empty site-root URI under Hugo pretty URLs) and
nested sections.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from .slug import relref_target


def is_external_uri(refuri: str) -> bool:
    """Return ``True`` when ``refuri`` points outside the generated site.

    An absolute URI with a scheme (``https:``, ``mailto:`` ...) or a
    scheme-relative/host-qualified URI (``//host/path``) is treated as
    external. Pure fragment links (``#anchor``) and relative paths are internal.
    """
    if not refuri:
        return False
    parts = urlsplit(refuri)
    return bool(parts.scheme) or bool(parts.netloc)


def split_fragment(refuri: str) -> tuple[str, str]:
    """Split ``refuri`` into ``(path, fragment)`` where fragment keeps its ``#``.

    ``"guide#install"`` → ``("guide", "#install")``; ``"guide"`` →
    ``("guide", "")``; ``"#install"`` → ``("", "#install")``.
    """
    base, sep, frag = refuri.partition("#")
    return base, (sep + frag) if sep else ""


def build_docname_index(builder, from_docname: str) -> dict[str, str]:
    """Map each ``get_relative_uri`` result back to its docname.

    The map is computed from the authoritative set of documents
    (``builder.env.found_docs``) by asking the builder for the relative URI from
    ``from_docname`` to every known docname. Inverting this exact mapping is
    reliable where raw path arithmetic is not, because it mirrors the same
    URI scheme the resolved ``refuri`` values were produced with.
    """
    index: dict[str, str] = {}
    for docname in builder.env.found_docs:
        uri = builder.get_relative_uri(from_docname, docname)
        # First writer wins on collisions; docnames are unique so the only
        # possible duplicate key is the current document mapping to ".".
        index.setdefault(uri, docname)
    return index


def resolve_target_docname(
    builder, from_docname: str, uri_path: str
) -> str | None:
    """Return the docname a relative ``uri_path`` refers to, or ``None``.

    ``uri_path`` is the fragment-stripped, relative ``refuri`` of an internal
    reference. An empty path denotes the current document.
    """
    if uri_path == "":
        return from_docname
    index = build_docname_index(builder, from_docname)
    return index.get(uri_path)


def internal_link_markdown(
    builder,
    from_docname: str,
    refuri: str,
    text: str,
    style: str,
) -> str:
    """Render an internal reference to Markdown in the requested ``style``.

    For ``relref`` the target docname is recovered and emitted inside a
    ``{{< relref "..." >}}`` shortcode so Hugo validates it. If the docname
    cannot be recovered (e.g. a reference to a non-document target) the relative
    URI is emitted directly as a safe fallback. For ``relative`` the resolved
    relative ``refuri`` is used verbatim.
    """
    uri_path, fragment = split_fragment(refuri)

    if style == "relative":
        return f"[{text}]({refuri})"

    # relref (default)
    docname = resolve_target_docname(builder, from_docname, uri_path)
    if docname is None:
        # Could not map back to a document (bare anchor or non-doc target):
        # fall back to the plain relative link so output stays valid.
        return f"[{text}]({refuri})"
    ref_arg = _relref_arg(builder, docname) + fragment
    return f'[{text}]({{{{< relref "{ref_arg}" >}}}})'


def _relref_arg(builder, docname: str) -> str:
    """Return the Hugo ``relref`` target for ``docname``.

    Prefers the toctree model's slugged **logical path** (see
    :func:`doxtr_hugo.slug.relref_target`) so the shortcode resolves to the
    page that was actually emitted — in particular after a trailing ``index``
    segment is collapsed into its parent directory. Falls back to the raw
    docname if the builder has no toctree model yet (e.g. a stub in tests).
    """
    model = getattr(builder, "toc_model", None)
    if model is None:
        return docname
    config = getattr(builder, "config", None)
    root_doc = getattr(config, "root_doc", None)
    return relref_target(docname, model, root_doc=root_doc)


def external_link_markdown(refuri: str, text: str) -> str:
    """Render an external reference as a CommonMark link.

    When the visible text equals the URL (an autolinked bare URL), the plain
    URL is emitted so round-tripped autolinks stay clean.
    """
    if text == refuri:
        return refuri
    return f"[{text}]({refuri})"
