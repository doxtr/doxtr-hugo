"""HugoTranslator — docutils NodeVisitor that emits Goldmark Markdown.

M1 scope (this step): the core block and inline constructs — section
headings, paragraphs, inline formatting (emphasis/strong/literal), bullet and
enumerated lists, literal/code blocks, tables (GFM pipe tables with a raw
HTML fallback for spanning cells), and admonitions (shortcode by default, raw
HTML when ``doxtr_hugo_admonitions = "html"``), and links/cross-references
(external links, ``:doc:``/``:ref:`` internal xrefs via ``{{< relref >}}`` or
plain relative links per ``doxtr_hugo_link_style``). Front matter is added in a
later M1 step. Nodes not yet handled fall through
to :meth:`unknown_visit`/:meth:`unknown_departure`: their container markup is
not rendered (text children are still visited) so a build never crashes on an
unmapped construct a third-party extension may emit, but a single Sphinx
warning per unmapped node *type* is logged so the dropped markup is observable
rather than silent.

The translator accumulates Markdown text into ``self.body`` and exposes the
result via :meth:`astext`. It deliberately keeps the output Goldmark-friendly:
ATX headings, fenced code blocks with language hints, and CommonMark inline
markers.
"""

from __future__ import annotations

from docutils import nodes
from sphinx.util import logging
from sphinx.util.docutils import SphinxTranslator

from . import links
from . import slug

logger = logging.getLogger(__name__)


class HugoTranslator(SphinxTranslator):
    """Translate a resolved doctree into Hugo-ready Markdown.

    Block constructs append their own surrounding blank lines so the emitted
    document has stable, deterministic spacing (important for snapshot tests).
    Inline constructs wrap their content in CommonMark markers.
    """

    def __init__(self, document: nodes.document, builder) -> None:
        """Initialise the visitor's output buffer and traversal state.

        The attributes below form the translator's small state machine driven
        by the ``visit_*`` / ``depart_*`` handlers: ``body`` accumulates output
        fragments, ``section_level`` tracks heading depth, and the two stacks
        track nested list and admonition context.
        """
        super().__init__(document, builder)
        self.body: list[str] = []
        #: Current heading depth. ``section`` increments on entry and
        #: decrements on exit; the title handler uses it to choose ``#`` count.
        self.section_level = 0
        #: Whether the page's leading top-level (H1) title heading has been
        #: seen yet. Under the default ``doxtr_hugo_title_heading="frontmatter"``
        #: that first H1 is dropped from the body because the ``title`` front
        #: matter already carries it and every Hugo theme (and the built-in
        #: skeleton) renders the title from front matter — emitting it again in
        #: the body produces a duplicated heading. Deeper headings are always
        #: emitted.
        self._page_title_emitted = False
        #: Stack of list context dicts: ``{"type": "bullet"|"enum", "counter": int}``.
        self._list_stack: list[dict] = []
        #: Stack of pending admonition titles (generic ``admonition`` only).
        self._admonition_title: list[str] = []

    # -- public API ---------------------------------------------------------

    def astext(self) -> str:
        """Return the accumulated Markdown body with a single trailing newline."""
        text = "".join(self.body)
        # Collapse 3+ consecutive newlines to exactly two (one blank line) and
        # ensure the document ends with exactly one newline.
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")
        return text.strip("\n") + "\n" if text.strip("\n") else ""

    # -- helpers ------------------------------------------------------------

    def _add(self, text: str) -> None:
        self.body.append(text)

    @property
    def _in_list(self) -> bool:
        return bool(self._list_stack)

    # -- document -----------------------------------------------------------

    def visit_document(self, node: nodes.Element) -> None:
        pass

    def depart_document(self, node: nodes.Element) -> None:
        pass

    # -- sections & titles --------------------------------------------------

    def visit_section(self, node: nodes.Element) -> None:
        self.section_level += 1

    def depart_section(self, node: nodes.Element) -> None:
        self.section_level -= 1

    def visit_title(self, node: nodes.Element) -> None:
        # Headings are only emitted for section titles. Titles on other nodes
        # (e.g. tables, admonitions) are handled by their own visitors.
        if not isinstance(node.parent, nodes.section):
            raise nodes.SkipNode
        # Drop the page's leading top-level (H1) title from the body: the Hugo
        # ``title`` front matter already carries it and themes/the skeleton
        # render it, so emitting it here duplicates the heading. Controlled by
        # ``doxtr_hugo_title_heading`` ("frontmatter" = drop, the default;
        # "body" = keep the old behavior). Only the first H1 is dropped; deeper
        # headings and any subsequent top-level section titles are kept.
        if (
            self.section_level == 1
            and not self._page_title_emitted
            and self._title_heading_mode == "frontmatter"
        ):
            self._page_title_emitted = True
            raise nodes.SkipNode
        if self.section_level == 1:
            self._page_title_emitted = True
        level = max(1, min(self.section_level, 6))
        self._add("\n" + "#" * level + " ")

    @property
    def _title_heading_mode(self) -> str:
        """Resolved ``doxtr_hugo_title_heading`` (defaults to ``frontmatter``)."""
        cfg = getattr(self.builder, "config", None)
        return getattr(cfg, "doxtr_hugo_title_heading", "frontmatter") or "frontmatter"

    def depart_title(self, node: nodes.Element) -> None:
        self._add("\n\n")

    # -- paragraphs ---------------------------------------------------------

    def visit_paragraph(self, node: nodes.Element) -> None:
        if self._in_list:
            # Inside a list item the marker/indentation is emitted by the list
            # item handler; just add the text, no leading blank line.
            return
        self._add("\n")

    def depart_paragraph(self, node: nodes.Element) -> None:
        if self._in_list:
            return
        self._add("\n\n")

    # -- inline text --------------------------------------------------------

    def visit_Text(self, node: nodes.Text) -> None:
        text = node.astext()
        # Inside a fenced code block the ``literal_block`` handler raises
        # ``SkipNode`` (verbatim), and ``visit_literal`` renders its own content
        # and skips, so by the time we reach here ``text`` is ordinary inline
        # prose. A literal backtick in prose (e.g. from an escaped RST ``\```` or
        # a ``:literal:`` that resolved to text) must be backslash-escaped, or
        # Goldmark reads it as the start of an inline code span and swallows the
        # following text up to the next backtick. Backslash-escaping is the
        # CommonMark-correct way to emit a verbatim backtick in running text.
        self._add(self._escape_inline_text(text))

    @staticmethod
    def _escape_inline_text(text: str) -> str:
        r"""Escape Markdown-structural characters in inline prose text.

        Only characters that would otherwise change the *structure* of the
        rendered output are escaped, so ordinary prose stays readable:

        * a backtick (`` ` ``) would open an inline code span;
        * a backslash (``\``) must be escaped first so it cannot consume a
          following character (and so an authored ``\``` stays a backslash).

        Emphasis/link punctuation (``*``, ``_``, ``[`` …) is intentionally left
        alone: docutils already delivers emphasis/links as dedicated nodes, so
        any such character surviving into a Text node is literal prose that
        Goldmark renders verbatim anyway, and escaping it would add stray
        backslashes to normal sentences.
        """
        return text.replace("\\", "\\\\").replace("`", "\\`")

    def depart_Text(self, node: nodes.Text) -> None:
        pass

    def visit_emphasis(self, node: nodes.Element) -> None:
        self._add("*")

    def depart_emphasis(self, node: nodes.Element) -> None:
        self._add("*")

    def visit_strong(self, node: nodes.Element) -> None:
        self._add("**")

    def depart_strong(self, node: nodes.Element) -> None:
        self._add("**")

    def visit_literal(self, node: nodes.Element) -> None:
        """Emit inline code, choosing a backtick fence that survives backticks.

        CommonMark inline code is delimited by a run of backticks; to embed
        literal backticks the delimiter must be *longer* than any backtick run
        in the content, and the span must be padded with one space on each side
        when the content itself begins or ends with a backtick (or is only
        spaces). A fixed single-backtick wrapper corrupts content that contains
        backticks — e.g. the RST ```` ````this text is code```` ```` parses to a
        literal whose text is ``` ``this text is code`` ```, which a lone ``` `
        ``` ``` wrapper would mis-close. Rendering the child text and computing
        the fence here keeps such content verbatim.
        """
        # The literal's text is its concatenated descendants. Inline literals
        # carry plain text (any role markup is already resolved), so astext()
        # is the faithful content.
        text = node.astext()

        # Longest run of consecutive backticks in the content; the fence must
        # be at least one backtick longer so it cannot be closed early.
        longest = 0
        run = 0
        for ch in text:
            if ch == "`":
                run += 1
                longest = max(longest, run)
            else:
                run = 0
        fence = "`" * (longest + 1)

        # Pad with a single space when the content starts/ends with a backtick
        # (CommonMark strips exactly one leading and trailing space), or when
        # it is all spaces, so the delimiters are not fused with the content.
        needs_pad = (
            text.startswith("`")
            or text.endswith("`")
            or (text and text.strip(" ") == "")
        )
        pad = " " if needs_pad else ""
        self._add(f"{fence}{pad}{text}{pad}{fence}")
        raise nodes.SkipNode

    def depart_literal(self, node: nodes.Element) -> None:
        # ``visit_literal`` renders the whole node and raises ``SkipNode``; this
        # departure is only reached for a degenerate empty literal that somehow
        # bypasses the visitor, so it is intentionally a no-op.
        pass

    # -- references / links -------------------------------------------------

    def _reference_text(self, node: nodes.Element) -> str:
        """Render a reference node's inline children to Markdown.

        A fresh child translator renders the link label so nested inline
        markup (``*emph*``, ``` ``code`` ```) is preserved. Newlines are
        flattened to spaces because a Markdown link label is single-line.
        """
        child = type(self)(self.document, self.builder)
        for sub in node.children:
            sub.walkabout(child)
        text = child.astext().strip()
        return " ".join(text.split("\n"))

    def visit_reference(self, node: nodes.Element) -> None:
        """Emit a cross-reference or external link as CommonMark/relref.

        References are fully resolved by Sphinx before the builder writes, so
        ``refuri`` is final here. Nodes without a ``refuri`` (e.g. internal
        target anchors) are left to the generic traversal.
        """
        refuri = node.get("refuri")
        if not refuri:
            # No URI to link to (named anchors, unresolved targets): render
            # the label text inline without a link wrapper.
            return
        text = self._reference_text(node)
        if links.is_external_uri(refuri):
            self._add(links.external_link_markdown(refuri, text))
        else:
            style = getattr(self.config, "doxtr_hugo_link_style", "relref")
            from_docname = getattr(self.builder, "current_docname", None) or ""
            self._add(
                links.internal_link_markdown(
                    self.builder, from_docname, refuri, text, style
                )
            )
        raise nodes.SkipNode

    def depart_reference(self, node: nodes.Element) -> None:
        pass

    # -- line breaks --------------------------------------------------------

    def visit_line_block(self, node: nodes.Element) -> None:
        self._add("\n")

    def depart_line_block(self, node: nodes.Element) -> None:
        self._add("\n")

    def visit_line(self, node: nodes.Element) -> None:
        pass

    def depart_line(self, node: nodes.Element) -> None:
        self._add("  \n")  # trailing two spaces = hard line break

    # -- images & figures ---------------------------------------------------

    def visit_image(self, node: nodes.Element) -> None:
        """Emit a Markdown image reference ``![alt](uri "title")``.

        ``image`` nodes reach the translator either from an authored
        ``.. image::``/``.. figure::`` directive or from the diagram render pass
        (:mod:`doxtr_hugo.render_pass`), which rewrites each rendered diagram to
        an ``image`` node whose ``uri`` is the co-located content-hashed
        filename. The reference is emitted verbatim so a bare filename resolves
        as a Hugo page resource and an absolute/relative URI is passed through
        unchanged (guide §4.1 step 4, §5.1 images row).
        """
        uri = node.get("uri", "")
        if not uri:
            raise nodes.SkipNode
        alt = node.get("alt", "") or ""
        # A Markdown image label is single-line; flatten any newlines.
        alt = " ".join(str(alt).split())
        title = node.get("title")
        if title:
            safe = str(title).replace('"', '\\"')
            self._add(f'\n![{alt}]({uri} "{safe}")\n\n')
        else:
            self._add(f"\n![{alt}]({uri})\n\n")
        raise nodes.SkipNode

    def visit_figure(self, node: nodes.Element) -> None:
        """Render a ``figure`` as its image plus an italicized caption line.

        Goldmark has no native figure block, so a figure is emitted as the
        child image reference followed by the caption text in emphasis on its
        own paragraph — theme-agnostic and valid CommonMark. The generic
        traversal of the figure's children is suppressed; the image and caption
        are rendered explicitly here.
        """
        image = next(iter(node.findall(nodes.image)), None)
        if image is not None:
            image.walkabout(self)
        caption = next(iter(node.findall(nodes.caption)), None)
        if caption is not None:
            child = type(self)(self.document, self.builder)
            for sub in caption.children:
                sub.walkabout(child)
            text = " ".join(child.astext().split())
            if text:
                self._add(f"\n*{text}*\n\n")
        raise nodes.SkipNode

    def visit_RawMarkdown(self, node: nodes.Element) -> None:
        """Emit literal Markdown carried by a :class:`~doxtr_hugo.render_pass.RawMarkdown` node.

        The diagram render pass uses this for the missing-backend *passthrough*
        path (guide §4.4): the diagram source is wrapped in a fenced code block
        so the information survives even when the backend binary is absent.
        """
        markdown = node.get("markdown", "")
        if markdown:
            self._add("\n" + markdown + "\n\n")
        raise nodes.SkipNode

    def depart_RawMarkdown(self, node: nodes.Element) -> None:
        pass

    # -- lists --------------------------------------------------------------

    def visit_bullet_list(self, node: nodes.Element) -> None:
        if not self._in_list:
            self._add("\n")
        else:
            # A nested list must begin on its own line, after the parent
            # item's inline text.
            self._add("\n")
        self._list_stack.append(
            {"type": "bullet", "counter": 0, "indent": self._nested_list_indent()}
        )

    def depart_bullet_list(self, node: nodes.Element) -> None:
        self._list_stack.pop()
        if not self._in_list:
            self._add("\n")

    def visit_enumerated_list(self, node: nodes.Element) -> None:
        if not self._in_list:
            self._add("\n")
        else:
            # Nested enumerated list starts on its own line.
            self._add("\n")
        start = node.get("start", 1)
        self._list_stack.append(
            {
                "type": "enum",
                "counter": start - 1,
                "indent": self._nested_list_indent(),
            }
        )

    def depart_enumerated_list(self, node: nodes.Element) -> None:
        self._list_stack.pop()
        if not self._in_list:
            self._add("\n")

    def _nested_list_indent(self) -> str:
        """Base indent for a list that is about to open at the current depth.

        A top-level list has no indent. A *nested* list must align with the
        **content column** of the enclosing list item — i.e. past that item's
        marker — or CommonMark/Goldmark treats its items as siblings of the
        parent list instead of a child list. The enclosing item recorded that
        content column in ``child_indent`` when it emitted its marker.
        """
        if not self._list_stack:
            return ""
        return self._list_stack[-1].get("child_indent", "  ")

    def visit_list_item(self, node: nodes.Element) -> None:
        ctx = self._list_stack[-1]
        indent = ctx["indent"]
        if ctx["type"] == "bullet":
            marker = "- "
        else:
            ctx["counter"] += 1
            marker = f"{ctx['counter']}. "
        # Record the content column for this item so a nested list opened while
        # rendering it aligns past this marker (CommonMark nesting rule). The
        # width depends on the marker (``- `` is 2, ``1. `` is 3, ``10. `` is 4).
        ctx["child_indent"] = indent + " " * len(marker)
        self._add(f"{indent}{marker}")

    def depart_list_item(self, node: nodes.Element) -> None:
        self._add("\n")

    # -- code / literal blocks ---------------------------------------------

    def visit_literal_block(self, node: nodes.Element) -> None:
        lang = ""
        # Sphinx ``code-block`` stores the language in the "language" attr;
        # highlighting via classes is a fallback.
        if node.get("language"):
            lang = node["language"]
            if lang in ("default", "none"):
                lang = ""
        self._add(f"\n```{lang}\n")
        self._add(node.astext())
        if not node.astext().endswith("\n"):
            self._add("\n")
        self._add("```\n\n")
        raise nodes.SkipNode

    def visit_doctest_block(self, node: nodes.Element) -> None:
        self._add("\n```python\n")
        self._add(node.astext())
        if not node.astext().endswith("\n"):
            self._add("\n")
        self._add("```\n\n")
        raise nodes.SkipNode

    # -- tables -------------------------------------------------------------

    def visit_table(self, node: nodes.Element) -> None:
        """Render a table as a GFM pipe table, or raw HTML when needed.

        Goldmark pipe tables cannot express row/column spans, so a table that
        contains a spanning cell (``morecols``/``morerows``) is emitted as raw
        ``<table>`` HTML. The ``doxtr_hugo_tables = "html"`` option forces HTML
        for every table. Raw HTML passthrough requires
        ``markup.goldmark.renderer.unsafe = true`` in the emitted ``hugo.toml``
        (set by the site skeleton).

        The whole table subtree is consumed here (``SkipNode``) so the generic
        row/entry traversal never runs.
        """
        mode = getattr(self.config, "doxtr_hugo_tables", "auto")
        if mode == "html" or self._table_has_spans(node):
            self._add("\n" + self._render_table_html(node) + "\n\n")
        else:
            self._add("\n" + self._render_table_pipe(node) + "\n")
        raise nodes.SkipNode

    @staticmethod
    def _iter_entries(node: nodes.Element):
        for entry in node.findall(nodes.entry):
            yield entry

    def _table_has_spans(self, node: nodes.Element) -> bool:
        for entry in self._iter_entries(node):
            if entry.get("morecols", 0) or entry.get("morerows", 0):
                return True
        return False

    def _cell_markdown(self, entry: nodes.Element) -> str:
        """Render a cell's inline content to single-line Markdown.

        A fresh child translator renders the entry's children, then the result
        is flattened: pipe tables are one line per row, so newlines collapse to
        ``<br>`` and literal pipes are escaped.
        """
        child = type(self)(self.document, self.builder)
        for sub in entry.children:
            sub.walkabout(child)
        text = child.astext().strip()
        text = text.replace("\n", "<br>")
        text = text.replace("|", "\\|")
        return text

    def _cell_html(self, entry: nodes.Element) -> str:
        child = type(self)(self.document, self.builder)
        for sub in entry.children:
            sub.walkabout(child)
        return child.astext().strip()

    def _render_table_pipe(self, node: nodes.Element) -> str:
        tgroup = next(iter(node.findall(nodes.tgroup)), None)
        if tgroup is None:
            return ""
        ncols = len(list(tgroup.findall(nodes.colspec)))
        thead = next(iter(tgroup.findall(nodes.thead)), None)
        tbodies = list(tgroup.findall(nodes.tbody))

        def row_cells(row: nodes.Element) -> list[str]:
            return [self._cell_markdown(e) for e in row.findall(nodes.entry)]

        header_rows = list(thead.findall(nodes.row)) if thead is not None else []
        body_rows = []
        for tbody in tbodies:
            body_rows.extend(tbody.findall(nodes.row))

        # GFM requires a header row. If the table has no thead, synthesize an
        # empty header so the pipe table is still valid.
        if header_rows:
            header = row_cells(header_rows[0])
            extra_header = header_rows[1:]
        else:
            header = [""] * ncols
            extra_header = []
        if not ncols:
            ncols = len(header)

        def fmt(cells: list[str]) -> str:
            cells = (cells + [""] * ncols)[:ncols]
            return "| " + " | ".join(cells) + " |"

        lines = [fmt(header), "| " + " | ".join(["---"] * ncols) + " |"]
        # Any additional header rows become ordinary body rows (pipe tables
        # support only one header row).
        for row in list(extra_header) + body_rows:
            lines.append(fmt(row_cells(row)))
        return "\n".join(lines) + "\n"

    def _render_table_html(self, node: nodes.Element) -> str:
        tgroup = next(iter(node.findall(nodes.tgroup)), None)
        if tgroup is None:
            return "<table></table>"
        thead = next(iter(tgroup.findall(nodes.thead)), None)
        tbodies = list(tgroup.findall(nodes.tbody))
        out = ["<table>"]

        def emit_row(row: nodes.Element, tag: str) -> None:
            out.append("<tr>")
            for entry in row.findall(nodes.entry):
                attrs = ""
                morecols = entry.get("morecols", 0)
                morerows = entry.get("morerows", 0)
                if morecols:
                    attrs += f' colspan="{morecols + 1}"'
                if morerows:
                    attrs += f' rowspan="{morerows + 1}"'
                out.append(f"<{tag}{attrs}>{self._cell_html(entry)}</{tag}>")
            out.append("</tr>")

        if thead is not None:
            out.append("<thead>")
            for row in thead.findall(nodes.row):
                emit_row(row, "th")
            out.append("</thead>")
        out.append("<tbody>")
        for tbody in tbodies:
            for row in tbody.findall(nodes.row):
                emit_row(row, "td")
        out.append("</tbody>")
        out.append("</table>")
        return "".join(out)

    # -- admonitions --------------------------------------------------------

    #: docutils admonition node classes keyed by their Hugo/shortcode type.
    #: The generic ``admonition`` node (user-titled) is handled separately.
    _ADMONITION_TYPES = (
        "note",
        "warning",
        "tip",
        "caution",
        "danger",
        "error",
        "hint",
        "important",
        "attention",
        "seealso",
    )

    def _admonition_mode(self) -> str:
        return getattr(self.config, "doxtr_hugo_admonitions", "shortcode")

    def _render_admonition_body(self, node: nodes.Element, skip_title: bool) -> str:
        """Render an admonition's children to Markdown via a child translator.

        When ``skip_title`` is set, a leading ``title`` child (present on the
        generic ``admonition`` directive) is excluded from the body so it can
        be used as the admonition heading instead.
        """
        child = type(self)(self.document, self.builder)
        for sub in node.children:
            if skip_title and isinstance(sub, nodes.title):
                continue
            sub.walkabout(child)
        return child.astext().strip()

    def _emit_admonition(self, node: nodes.Element, atype: str, title: str) -> None:
        """Emit one admonition as a shortcode or raw-HTML div.

        ``atype`` is the semantic type (``note``/``warning``/...). ``title`` is
        the display title for the generic ``admonition`` directive (empty for
        the typed directives, which the theme titles from the type).
        """
        body = self._render_admonition_body(node, skip_title=bool(title))
        if self._admonition_mode() == "html":
            classes = f"admonition {atype}"
            self._add(f'\n<div class="{classes}">\n')
            if title:
                self._add(f'<p class="admonition-title">{title}</p>\n')
            if body:
                self._add(body + "\n")
            self._add("</div>\n\n")
        else:
            # Shortcode form: {{< admonition type "title" >}}...{{< /admonition >}}
            if title:
                # Escape embedded double quotes in the title argument.
                safe = title.replace('"', '\\"')
                open_tag = f'{{{{< admonition {atype} "{safe}" >}}}}'
            else:
                open_tag = f"{{{{< admonition {atype} >}}}}"
            self._add("\n" + open_tag + "\n")
            if body:
                self._add(body + "\n")
            self._add("{{< /admonition >}}\n\n")
        raise nodes.SkipNode

    def visit_admonition(self, node: nodes.Element) -> None:
        """Generic ``.. admonition:: Title`` directive (custom title)."""
        title = ""
        for sub in node.children:
            if isinstance(sub, nodes.title):
                title = sub.astext()
                break
        self._emit_admonition(node, "note", title)

    # Typed admonition directives (note/warning/tip/...): each uses its node
    # class name as the semantic type. Bound below the class body.

    # -- block quotes & transitions ----------------------------------------

    def visit_block_quote(self, node: nodes.Element) -> None:
        # Minimal support: emit the inner content as-is. A dedicated blockquote
        # renderer (``> ``-prefixed) can replace this in a later step.
        self._add("\n")

    def depart_block_quote(self, node: nodes.Element) -> None:
        self._add("\n")

    def visit_transition(self, node: nodes.Element) -> None:
        self._add("\n---\n\n")
        raise nodes.SkipNode

    # -- toctree (section navigation) --------------------------------------

    def _toc_model(self):
        """Return the builder's toctree model, or ``None`` for a bare translator."""
        return getattr(self.builder, "toc_model", None)

    def _relref_arg(self, docname: str) -> str:
        """Hugo ``relref`` target (slugged logical path) for a toctree entry.

        Delegates to :func:`doxtr_hugo.slug.relref_target` so a trailing
        ``index`` segment is collapsed into its parent directory, matching the
        page actually emitted. Falls back to the raw docname when no model is
        available (e.g. a unit-test stub builder).
        """
        model = self._toc_model()
        if model is None:
            return docname
        root_doc = getattr(self.config, "root_doc", None)
        return slug.relref_target(docname, model, root_doc=root_doc)

    def _relative_target(self, docname: str) -> str:
        """Site-relative pretty URL for a toctree entry under the relative style.

        Uses :func:`doxtr_hugo.slug.url_for_docname` (same ``index``-collapsing
        rules) so a plain relative link points at the directory Hugo actually
        serves. Falls back to the raw docname when no model is available.
        """
        model = self._toc_model()
        if model is None:
            return docname
        root_doc = getattr(self.config, "root_doc", None)
        return slug.url_for_docname(docname, model, root_doc=root_doc)

    def visit_toctree(self, node: nodes.Element) -> None:
        """Render a ``toctree`` as a Hugo-native list of child section links.

        The emitted site skeleton already derives section ``_index.md`` nav
        (weights/menus) from the resolved toctree (guide §5), so a toctree in
        the page body becomes an in-page list of links to its children — a
        Hugo-friendly "section index" — rather than being dropped (which would
        leave a visible gap on a landing page that is *only* a toctree). Each
        entry's ``docname`` is emitted through the ``relref`` shortcode so Hugo
        validates the link at build time (a dangling entry fails the Hugo
        build, as for every other internal xref); the ``relative`` link style
        emits a plain relative link instead.

        ``toctree`` ``entries`` are ``(title_or_None, docname)`` pairs in
        authoring order. A ``None`` title falls back to the environment's
        resolved title for that docname, else the docname itself.
        """
        entries = node.get("entries") or []
        if not entries:
            raise nodes.SkipNode
        style = getattr(self.config, "doxtr_hugo_link_style", "relref")
        env = getattr(self.builder, "env", None)
        titles = getattr(env, "titles", {}) or {}
        self._add("\n")
        for title, docname in entries:
            if not docname:
                continue
            # External toctree entries carry a URL in the title position
            # (docname is a URL); skip the relref machinery for those.
            label = title
            if not label:
                title_node = titles.get(docname)
                label = title_node.astext() if title_node is not None else docname
            label = " ".join(str(label).split())
            if links.is_external_uri(docname):
                self._add(f"- {links.external_link_markdown(docname, label)}\n")
            elif style == "relative":
                self._add(f"- [{label}]({self._relative_target(docname)})\n")
            else:
                self._add(
                    f'- [{label}]({{{{< relref "{self._relref_arg(docname)}" >}}}})\n'
                )
        self._add("\n")
        raise nodes.SkipNode

    # -- raw passthrough ----------------------------------------------------

    def visit_raw(self, node: nodes.Element) -> None:
        """Honor ``.. raw:: html`` by passing the HTML through verbatim.

        Goldmark renders inline/block HTML when
        ``markup.goldmark.renderer.unsafe = true`` (set by the site skeleton),
        so an ``html`` raw block is emitted as-is (guide §5.1 raw-html row).
        Other raw formats (``latex``/``man``/…) have no Hugo rendering and are
        dropped with a one-per-type warning (guide §5.1 "raw latex/other →
        dropped with warning"), reusing the unmapped-node channel so operators
        can mute it via ``suppress_warnings``.
        """
        fmt = (node.get("format") or "").lower()
        if fmt == "html":
            text = node.astext()
            self._add("\n" + text + "\n\n")
        else:
            # No sensible Hugo mapping for non-HTML raw content; warn once.
            self._warn_unmapped_node(node)
        raise nodes.SkipNode

    # -- footnotes (Goldmark GFM footnote extension) -----------------------

    @staticmethod
    def _footnote_label(node: nodes.Element) -> str:
        """Return a stable GFM footnote label for a reference or definition.

        docutils links a ``footnote_reference`` to its ``footnote`` by id: the
        reference carries ``refid`` and the definition carries that same id as
        its first entry in ``ids``. Using that id as the GFM ``[^label]`` keeps
        the reference (``visit_footnote_reference``) and the definition
        (``visit_footnote``) in agreement without relying on the rendered label
        text (``1``/``*``), which would leak numbering into the Markdown.
        """
        refid = node.get("refid")
        if refid:
            return refid
        ids = node.get("ids") or []
        if ids:
            return ids[0]
        # Last resort: a named footnote with neither refid nor ids.
        names = node.get("names") or []
        return names[0] if names else "fn"

    def visit_footnote_reference(self, node: nodes.Element) -> None:
        """Emit a GFM footnote reference ``[^label]``.

        The node's single text child is the rendered label (``1``, ``*``); it
        must not leak into the body, so the reference is emitted from the
        ``refid`` and the subtree is skipped.
        """
        self._add(f"[^{self._footnote_label(node)}]")
        raise nodes.SkipNode

    def visit_footnote(self, node: nodes.Element) -> None:
        """Emit a GFM footnote definition ``[^label]: <text>``.

        Goldmark's footnote extension (enabled in the skeleton ``hugo.toml``)
        collects definitions regardless of position, so the definition is
        emitted where docutils places it. The ``label`` and ``backrefs`` that
        docutils generates carry no Markdown meaning and would leak the
        numbering, so the body is rendered through a child translator that
        visits only the content children (paragraphs), and the result is
        folded into a single ``[^label]: ...`` line.
        """
        label = self._footnote_label(node)
        child = type(self)(self.document, self.builder)
        for sub in node.children:
            if isinstance(sub, (nodes.label,)):
                continue
            sub.walkabout(child)
        body = " ".join(child.astext().split())
        self._add(f"\n[^{label}]: {body}\n\n")
        raise nodes.SkipNode

    def visit_label(self, node: nodes.Element) -> None:
        """Drop a footnote/citation ``label`` node.

        The label's rendered number is implied by the GFM ``[^label]`` syntax,
        so emitting it would duplicate the numbering into the body.
        """
        raise nodes.SkipNode

    # -- inline references / misc inline -----------------------------------

    def visit_title_reference(self, node: nodes.Element) -> None:
        r"""Render an interpreted ``\`title\``` reference as emphasized text.

        A bare ``\`text\``` role (docutils default ``title_reference``) has no
        link target; reStructuredText renders it as a citation-style emphasis,
        so emphasis is the faithful Markdown mapping.
        """
        self._add("*")

    def depart_title_reference(self, node: nodes.Element) -> None:
        self._add("*")

    def visit_rubric(self, node: nodes.Element) -> None:
        """Render a ``.. rubric::`` as a bold stand-alone line.

        A rubric is an informal, unnumbered heading that is *not* part of the
        section structure, so it maps to a bold paragraph rather than an ATX
        heading (which would perturb the section/weight model).
        """
        self._add("\n**")

    def depart_rubric(self, node: nodes.Element) -> None:
        self._add("**\n\n")

    def visit_caption(self, node: nodes.Element) -> None:
        """Render a stand-alone caption (e.g. on a code-block) as italic text.

        Captions inside a ``figure`` are consumed by :meth:`visit_figure`; a
        caption reached here belongs to another container (literal-block
        ``:caption:``, ``container``) and renders as an italic line.
        """
        self._add("\n*")

    def depart_caption(self, node: nodes.Element) -> None:
        self._add("*\n\n")

    # -- definition lists (Goldmark DefinitionList extension) --------------

    def visit_definition_list(self, node: nodes.Element) -> None:
        self._add("\n")

    def depart_definition_list(self, node: nodes.Element) -> None:
        self._add("\n")

    def visit_definition_list_item(self, node: nodes.Element) -> None:
        pass

    def depart_definition_list_item(self, node: nodes.Element) -> None:
        self._add("\n")

    def visit_term(self, node: nodes.Element) -> None:
        """Emit a definition-list term line (Goldmark DefinitionList syntax)."""
        self._add("")

    def depart_term(self, node: nodes.Element) -> None:
        self._add("\n")

    def visit_definition(self, node: nodes.Element) -> None:
        """Emit a Goldmark definition (``: `` marker + indented continuation).

        The definition body is rendered through a child translator so nested
        inline *and block* markup survives. Goldmark's definition-list syntax
        marks a definition with ``: `` on its **first** line only; every
        following line is *continuation* content that must be indented by four
        spaces, exactly like a list-item body. Prefixing every line with ``: ``
        (the naive approach) corrupts multi-line block content — a fenced code
        block, for instance, has its fences and indented code lines turned into
        separate one-line definitions, so the code renders as stray ``<dd>``
        entries instead of a ``<pre>`` block. Emitting ``: `` once and indenting
        the remainder keeps paragraphs, code blocks, lists, etc. intact inside
        a single ``<dd>``.
        """
        child = type(self)(self.document, self.builder)
        for sub in node.children:
            sub.walkabout(child)
        body = child.astext().strip()
        lines = body.split("\n")
        for i, line in enumerate(lines):
            if i == 0:
                # First line carries the definition marker.
                self._add(f": {line}\n" if line else ": \n")
            elif line:
                # Continuation lines are indented four spaces so block content
                # (code fences, nested lists, extra paragraphs) stays attached
                # to this definition rather than ending it.
                self._add(f"    {line}\n")
            else:
                # Preserve blank lines verbatim; an indented blank line would
                # introduce trailing whitespace and is unnecessary for the
                # lazy-continuation rules.
                self._add("\n")
        raise nodes.SkipNode

    # -- field lists (``:name: body`` metadata) ----------------------------

    def visit_field_list(self, node: nodes.Element) -> None:
        """Render a field list as a bullet list of ``**name:** body`` items.

        Field lists (``:Query Parameters:``, autodoc ``:param:`` groups, …)
        have no native Goldmark construct; a bullet list of bold-labelled
        entries is a faithful, theme-agnostic rendering (guide §5.1 MEDIUM
        field-list row).
        """
        self._add("\n")

    def depart_field_list(self, node: nodes.Element) -> None:
        self._add("\n")

    def visit_field(self, node: nodes.Element) -> None:
        name_node = next(iter(node.findall(nodes.field_name)), None)
        body_node = next(iter(node.findall(nodes.field_body)), None)
        name = name_node.astext().strip() if name_node is not None else ""
        body = ""
        if body_node is not None:
            child = type(self)(self.document, self.builder)
            for sub in body_node.children:
                sub.walkabout(child)
            body = " ".join(child.astext().split())
        if name and body:
            self._add(f"- **{name}:** {body}\n")
        elif name:
            self._add(f"- **{name}**\n")
        elif body:
            self._add(f"- {body}\n")
        raise nodes.SkipNode

    # -- autodoc descriptions (``desc`` / signatures) ----------------------

    def visit_desc(self, node: nodes.Element) -> None:
        """Open an autodoc object description (signature(s) + content).

        The ``desc`` family (``desc_signature``/``desc_name``/
        ``desc_parameterlist``/``desc_parameter``/``desc_annotation``/
        ``desc_content``) models an API signature. Goldmark has no signature
        construct, so a signature renders as an inline-code line and the content
        follows as ordinary Markdown (guide §5.1 MEDIUM desc rows).
        """
        self._add("\n")

    def depart_desc(self, node: nodes.Element) -> None:
        self._add("\n")

    def visit_desc_signature(self, node: nodes.Element) -> None:
        """Render one signature line as inline code (flattened to one line)."""
        child = type(self)(self.document, self.builder)
        for sub in node.children:
            sub.walkabout(child)
        sig = " ".join(child.astext().split())
        if sig:
            # Escape backticks so the inline-code span never terminates early.
            sig = sig.replace("`", "\u02cb")
            self._add(f"`{sig}`\n\n")
        raise nodes.SkipNode

    def visit_desc_content(self, node: nodes.Element) -> None:
        """Render the description body as ordinary Markdown (children visited)."""
        self._add("\n")

    def depart_desc_content(self, node: nodes.Element) -> None:
        self._add("\n")

    # -- icons (sphinxcontrib.icon ``icon_node``) --------------------------

    def visit_icon_node(self, node: nodes.Element) -> None:
        """Render an inline icon as its Font Awesome ``<i>`` element.

        ``sphinxcontrib.icon`` carries the icon class string (e.g.
        ``fa-solid fa-folder``) on the node; HTML passthrough (unsafe=true, set
        by the skeleton) lets Hugo render it. Themes that ship the Font Awesome
        CSS display the glyph; others degrade to nothing visible-broken. Empty
        icon value emits nothing.
        """
        icon = node.get("icon") or ""
        if icon:
            self._add(f'<i class="{icon}"></i>')
        raise nodes.SkipNode

    # -- doto tasks (doxtr_doto ``doto_*`` nodes) --------------------------

    def visit_doto_node(self, node: nodes.Element) -> None:
        """Render a single ``doto`` task as a bold title + metadata line.

        ``doxtr_doto`` carries the task ``title``/``assignee``/``priority`` as
        node attributes and the task body as children. Rendered as a bold title
        with an italic metadata suffix, followed by the body — theme-agnostic
        and losslessly observable (guide §5.1 MEDIUM doto rows).
        """
        title = node.get("title") or ""
        meta_bits = []
        assignee = node.get("assignee")
        priority = node.get("priority")
        if assignee:
            meta_bits.append(f"@{assignee}")
        if priority:
            meta_bits.append(str(priority))
        self._add("\n")
        if title:
            self._add(f"**{title}**")
        if meta_bits:
            self._add(f" *({', '.join(meta_bits)})*")
        self._add("\n\n")
        # Children (task body) are rendered by the generic traversal.

    def depart_doto_node(self, node: nodes.Element) -> None:
        self._add("\n")

    def visit_doto_list_node(self, node: nodes.Element) -> None:
        # The list container is transparent; its resolved child task nodes carry
        # the rendered content.
        self._add("\n")

    def depart_doto_list_node(self, node: nodes.Element) -> None:
        self._add("\n")

    def visit_doto_summary_node(self, node: nodes.Element) -> None:
        # A summary placeholder resolved elsewhere; render any children it
        # carries and emit nothing of its own.
        pass

    def depart_doto_summary_node(self, node: nodes.Element) -> None:
        pass

    # -- sphinx.ext.todo (``todo_node``) -----------------------------------

    def visit_todo_node(self, node: nodes.Element) -> None:
        """Render a ``sphinx.ext.todo`` ``todo_node`` as a note admonition.

        A ``.. todo::`` directive is a highlighted aside; mapping it to the
        existing ``note`` admonition path keeps it visible and theme-consistent
        (shortcode or raw HTML per ``doxtr_hugo_admonitions``) rather than
        dropping it. Reuses :meth:`_emit_admonition` so the ``admonitions=html``
        downgrade applies uniformly.
        """
        self._emit_admonition(node, "note", "TODO")

    # -- invisible / build-time-only nodes ---------------------------------

    def visit_comment(self, node: nodes.Element) -> None:
        """Drop an RST comment (``.. comment text``) and its text entirely.

        A ``comment`` node carries its text as a ``Text`` child. Merely listing
        it in ``_TRANSPARENT_NODE_TYPES`` only silences the unmapped-node
        warning -- the generic traversal would still visit the child and emit
        the comment body into the Markdown output. ``SkipNode`` suppresses the
        whole subtree so comments produce no rendered content, matching the
        docutils/Sphinx HTML writer behaviour.
        """
        raise nodes.SkipNode

    def visit_substitution_definition(self, node: nodes.Element) -> None:
        """Drop a ``.. |name| replace::`` definition and its text.

        Substitution *definitions* are build-time only -- the resolved
        references are substituted inline elsewhere -- so their body must not
        be emitted. Without ``SkipNode`` the replacement text would leak into
        the output.
        """
        raise nodes.SkipNode

    def visit_system_message(self, node: nodes.Element) -> None:
        """Drop a docutils ``system_message`` (parser diagnostic) subtree.

        System messages are reporter diagnostics, not authored content; their
        paragraph children would otherwise render into the page. ``SkipNode``
        keeps them out of the output.
        """
        raise nodes.SkipNode

    def visit_meta(self, node: nodes.Element) -> None:
        """Drop a ``meta`` node (HTML ``<meta>`` metadata, no body text)."""
        raise nodes.SkipNode

    def visit_pending(self, node: nodes.Element) -> None:
        """Drop a transform ``pending`` placeholder and any staged children."""
        raise nodes.SkipNode

    # -- fallthrough --------------------------------------------------------

    #: docutils/Sphinx structural nodes that are *deliberately* transparent:
    #: they carry no Markdown markup of their own (their children supply the
    #: content, or they are invisible anchors/plumbing), so passing them
    #: through without a dedicated handler is correct and must **not** trigger
    #: the unmapped-node warning. Keyed by lower-cased class name.
    #:
    #: - ``inline`` / ``generated`` / ``problematic``: generic spans; children
    #:   carry the text.
    #: - ``compound``: wraps e.g. a resolved ``toctree`` — the toctree itself
    #:   drives the site skeleton, so its container is intentionally dropped.
    #: - ``target``: an invisible internal hyperlink-target anchor with no
    #:   rendered output (its children, when present, are the link text and are
    #:   carried through). ``comment`` / ``substitution_definition`` /
    #:   ``system_message`` / ``pending`` / ``meta`` are *not* listed here:
    #:   they carry build-time-only text that must be suppressed, so each has a
    #:   dedicated ``visit_*`` handler raising ``SkipNode`` instead of being
    #:   passed through transparently.
    #: - ``container`` / ``topic`` / ``sidebar``: generic wrappers whose
    #:   children carry the content; a plain block pass-through is the right
    #:   Hugo mapping (no special shortcode).
    #: - ``subtitle`` / ``attribution``: inline-ish text carried by children.
    #: - ``index``: an invisible index-entry anchor (no rendered output).
    #: - ``field_name`` / ``field_body``: consumed by :meth:`visit_field` via a
    #:   child translator; if reached standalone their children carry the text.
    #: - ``desc_name`` / ``desc_parameterlist`` / ``desc_parameter`` /
    #:   ``desc_annotation`` / ``desc_addname`` / ``desc_returns`` /
    #:   ``desc_sig_name`` / ``desc_sig_space`` / ``desc_sig_punctuation`` /
    #:   ``desc_sig_keyword`` / ``desc_sig_operator`` / ``desc_optional`` /
    #:   ``desc_type`` / ``desc_inline``: the inner signature spans rendered
    #:   (flattened) by :meth:`visit_desc_signature`'s child translator; their
    #:   text is the signature, so they are transparent rather than unmapped.
    _TRANSPARENT_NODE_TYPES = frozenset(
        {
            "inline",
            "generated",
            "problematic",
            "compound",
            "target",
            "container",
            "topic",
            "sidebar",
            "subtitle",
            "attribution",
            "index",
            "field_name",
            "field_body",
            "desc_name",
            "desc_addname",
            "desc_parameterlist",
            "desc_parameter",
            "desc_annotation",
            "desc_returns",
            "desc_sig_name",
            "desc_sig_space",
            "desc_sig_punctuation",
            "desc_sig_keyword",
            "desc_sig_keyword_type",
            "desc_sig_operator",
            "desc_sig_literal_number",
            "desc_sig_literal_string",
            "desc_sig_literal_char",
            "desc_sig_element",
            "desc_optional",
            "desc_type",
            "desc_type_parameter_list",
            "desc_type_parameter",
            "desc_inline",
        }
    )

    def unknown_visit(self, node: nodes.Node) -> None:
        # Unhandled nodes are ignored (their text children are still visited)
        # so the build stays green against constructs a third-party Sphinx
        # extension may emit that this builder does not map. Dropping content
        # *silently* is the real extensibility hazard, though: an extender has
        # no signal that their directive's output vanished. So emit a single
        # Sphinx warning per unmapped node *type* for the whole build — enough
        # to make the gap observable (and to tell an extender exactly which
        # ``visit_<type>`` handler to add) without flooding the log or failing
        # the build. Text children are still visited, so textual content is
        # preserved even for unmapped containers. Deliberately-transparent
        # structural/plumbing nodes (see ``_TRANSPARENT_NODE_TYPES``) are
        # passed through silently — warning about them would be noise and, under
        # ``-W``, would break otherwise-valid builds.
        if type(node).__name__.lower() not in self._TRANSPARENT_NODE_TYPES:
            self._warn_unmapped_node(node)

    def _warn_unmapped_node(self, node: nodes.Node) -> None:
        """Warn once per unmapped node type that content may be unstyled.

        The set of already-warned type names is tracked on the *builder* (not
        the per-document translator instance) so the warning fires exactly once
        across the whole build, no matter how many documents or child
        translators encounter the node. ``type="doxtr_hugo"`` /
        ``subtype="unmapped_node"`` let operators mute it via Sphinx's
        ``suppress_warnings`` if a given construct is intentionally dropped.
        """
        type_name = type(node).__name__
        builder = getattr(self, "builder", None)
        seen = getattr(builder, "_doxtr_unmapped_node_types", None)
        if seen is None:
            seen = set()
            if builder is not None:
                try:
                    builder._doxtr_unmapped_node_types = seen
                except Exception:  # pragma: no cover - defensive, builder may be a stub
                    pass
        if type_name in seen:
            return
        seen.add(type_name)
        logger.warning(
            "doxtr_hugo: no Markdown handler for node type %r; its container "
            "markup is dropped (text children are kept). Add a "
            "visit_%s/depart_%s handler to the translator to render it.",
            type_name,
            type_name,
            type_name,
            type="doxtr_hugo",
            subtype="unmapped_node",
        )

    def unknown_departure(self, node: nodes.Node) -> None:
        pass


def _make_typed_admonition_visitor(atype: str):
    """Build a ``visit_<type>`` method for a typed admonition node.

    The typed directives (``note``, ``warning``, ...) carry no custom title;
    the theme/shortcode derives the display title from the type, so the title
    argument is empty.
    """

    def _visit(self: HugoTranslator, node: nodes.Element) -> None:
        self._emit_admonition(node, atype, "")

    _visit.__name__ = f"visit_{atype}"
    _visit.__doc__ = f"Emit the ``{atype}`` admonition (typed directive)."
    return _visit


# Bind one visitor per typed admonition. ``seealso`` is a Sphinx node; it is
# only present when Sphinx registered it, but binding a visitor is harmless
# otherwise (docutils dispatches by node class name).
for _atype in HugoTranslator._ADMONITION_TYPES:
    setattr(HugoTranslator, f"visit_{_atype}", _make_typed_admonition_visitor(_atype))
del _atype
