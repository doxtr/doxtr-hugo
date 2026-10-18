"""M1 translator tests: each core construct maps to the expected Goldmark
Markdown. These tests build a real Sphinx ``hugo`` build over small RST fixtures
and read the emitted ``.md`` files. No external binaries are required.
"""

from __future__ import annotations

import textwrap

import pytest
from docutils import nodes


def _build(tmp_path, rst, extra_conf=""):
    """Build a one-document hugo project from ``rst`` and return its Markdown."""
    from sphinx.application import Sphinx

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
    (srcdir / "index.rst").write_text(textwrap.dedent(rst), encoding="utf-8")

    outdir = tmp_path / "out"
    app = Sphinx(
        srcdir=str(srcdir),
        confdir=str(srcdir),
        outdir=str(outdir),
        doctreedir=str(tmp_path / "doctrees"),
        buildername="hugo",
    )
    app.build()
    # The root document is written to ``content/_index.md`` under the Hugo
    # content tree (slug/bundle layout); read that path.
    return (outdir / "content" / "_index.md").read_text(encoding="utf-8")


def test_headings_nest_by_section_depth(tmp_path):
    md = _build(
        tmp_path,
        """
        Top Title
        =========

        Intro paragraph.

        Sub Section
        -----------

        Nested paragraph.
        """,
    )
    # The page's leading H1 is carried by the ``title`` front matter and dropped
    # from the body by default (doxtr_hugo_title_heading="frontmatter"), so a
    # theme/skeleton that renders .Title does not show it twice.
    assert 'title = "Top Title"' in md
    assert "# Top Title" not in md
    # Deeper headings are always kept (as H2, not promoted).
    assert "## Sub Section" in md


def test_title_heading_body_mode_keeps_the_h1(tmp_path):
    md = _build(
        tmp_path,
        """
        Top Title
        =========

        Intro paragraph.

        Sub Section
        -----------
        """,
        extra_conf='doxtr_hugo_title_heading = "body"\n',
    )
    # Opt back in to the legacy behavior: the leading H1 stays in the body.
    assert "# Top Title" in md
    assert "## Sub Section" in md


def test_title_heading_frontmatter_drops_only_the_leading_h1(tmp_path):
    # A second top-level section in the same document keeps its H1 — only the
    # page's first/leading title is the one the front matter duplicates.
    md = _build(
        tmp_path,
        """
        First Title
        ===========

        Body of first.

        Second Title
        ============

        Body of second.
        """,
    )
    assert "# First Title" not in md       # leading title dropped
    assert 'title = "First Title"' in md
    assert "# Second Title" in md          # subsequent top-level title kept


def test_paragraphs_are_separated_by_blank_lines(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        First paragraph.

        Second paragraph.
        """,
    )
    assert "First paragraph.\n\nSecond paragraph." in md


def test_inline_emphasis_strong_literal(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        This is *emph* and **bold** and ``code``.
        """,
    )
    assert "*emph*" in md
    assert "**bold**" in md
    assert "`code`" in md


def test_inline_literal_containing_backticks_uses_longer_fence():
    # An inline literal whose text itself contains backticks (RST
    # ````this text is code````) must be fenced with a backtick run LONGER
    # than any inside it, and space-padded because it starts/ends with a
    # backtick, so the backticks render verbatim inside the inline code.
    lit = nodes.literal()
    lit += nodes.Text("``this text is code``")
    md = _translate_nodes(lit)
    # Content has runs of two backticks -> fence must be three, with padding.
    assert "``` ``this text is code`` ```" in md


def test_inline_literal_plain_uses_single_backtick():
    lit = nodes.literal()
    lit += nodes.Text("plain code")
    md = _translate_nodes(lit)
    assert "`plain code`" in md
    # No over-fencing for content without backticks.
    assert "``plain" not in md


def test_inline_literal_single_backtick_content_is_padded():
    # A single embedded backtick needs a double-backtick fence. Padding is only
    # required when the content starts/ends with a backtick; a mid-string
    # backtick needs no padding, just the longer fence.
    lit = nodes.literal()
    lit += nodes.Text("a ` b")
    md = _translate_nodes(lit)
    assert "``a ` b``" in md


def test_inline_literal_edge_backtick_content_is_padded():
    # Content that ends with a backtick must be space-padded so the fence is
    # not fused with the content's backtick.
    lit = nodes.literal()
    lit += nodes.Text("ends with `")
    md = _translate_nodes(lit)
    assert "`` ends with ` ``" in md


def test_bullet_list(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        - alpha
        - beta
        - gamma
        """,
    )
    assert "- alpha" in md
    assert "- beta" in md
    assert "- gamma" in md


def test_enumerated_list(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        #. first
        #. second
        #. third
        """,
    )
    assert "1. first" in md
    assert "2. second" in md
    assert "3. third" in md


def test_nested_bullet_list_is_indented(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        - outer

          - inner
        """,
    )
    assert "- outer" in md
    assert "  - inner" in md


def test_three_level_nested_list_aligns_to_marker_width(tmp_path):
    # A third nesting level under an *ordered* item must be indented to the
    # parent item's CONTENT column (past its ``N. `` marker), not a fixed two
    # spaces per depth. Otherwise CommonMark/Goldmark reads the deepest items
    # as siblings of the parent list and the third level collapses into the
    # second (the "Nested Lists" rendering bug).
    md = _build(
        tmp_path,
        """
        Title
        =====

        * level one

           a. level two
           b. level two again

              i. level three
              ii. level three again
        """,
    )
    # Level 1 bullet at column 0.
    assert "\n- level one" in md
    # Level 2 ordered under the bullet: 2-space indent (``- `` marker width).
    assert "\n  1. level two" in md
    # Level 3 ordered under ``  1. ``/``  2. ``: aligned to the parent content
    # column = 2 (indent) + 3 (``2. `` marker) = 5 spaces, NOT 4.
    assert "\n     1. level three" in md
    # Guard against the old 4-space (collapsed) output.
    assert "\n    1. level three" not in md


def test_code_block_is_fenced_with_language(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        .. code-block:: python

           def f():
               return 1
        """,
    )
    assert "```python" in md
    assert "def f():" in md
    assert "    return 1" in md
    # fence closes
    assert md.count("```") >= 2


def test_literal_block_without_language(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        A code sample::

            plain text block
        """,
    )
    # Opening fence has no language tag.
    assert "```\n" in md
    assert "plain text block" in md


def test_no_triple_blank_lines(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        Para one.

        Para two.
        """,
    )
    assert "\n\n\n" not in md


def test_document_ends_with_single_newline(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        Body.
        """,
    )
    assert md.endswith("\n")
    assert not md.endswith("\n\n")


def test_simple_table_is_gfm_pipe(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        ===== =====
        A     B
        ===== =====
        1     2
        3     4
        ===== =====
        """,
    )
    assert "| A | B |" in md
    assert "| --- | --- |" in md
    assert "| 1 | 2 |" in md
    assert "| 3 | 4 |" in md
    # No raw HTML table for the span-free case.
    assert "<table>" not in md


def test_grid_table_with_span_falls_back_to_html(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        +---------+---------+
        | Header1 | Header2 |
        +=========+=========+
        | spans both cols   |
        +---------+---------+
        | a       | b       |
        +---------+---------+
        """,
    )
    assert "<table>" in md
    assert 'colspan="2"' in md
    assert "spans both cols" in md


def test_tables_html_mode_forces_raw_html(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        ===== =====
        A     B
        ===== =====
        1     2
        ===== =====
        """,
        extra_conf='doxtr_hugo_tables = "html"\n',
    )
    assert "<table>" in md
    assert "<th>A</th>" in md
    assert "| A | B |" not in md


def test_table_cell_inline_formatting_preserved(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        ======== ========
        Name     Note
        ======== ========
        ``code`` *emph*
        ======== ========
        """,
    )
    assert "`code`" in md
    assert "*emph*" in md


# -- admonitions ------------------------------------------------------------


def test_note_admonition_shortcode(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        .. note::

           Be careful here.
        """,
    )
    assert "{{< admonition note >}}" in md
    assert "Be careful here." in md
    assert "{{< /admonition >}}" in md


def test_warning_admonition_shortcode(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        .. warning::

           Danger ahead.
        """,
    )
    assert "{{< admonition warning >}}" in md
    assert "Danger ahead." in md


def test_generic_admonition_carries_custom_title(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        .. admonition:: Read Me First

           Important preamble.
        """,
    )
    assert '{{< admonition note "Read Me First" >}}' in md
    assert "Important preamble." in md
    # The custom title must not leak into the body as an ATX heading.
    assert "# Read Me First" not in md


def test_admonition_html_mode(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        .. tip::

           Handy hint.
        """,
        extra_conf='doxtr_hugo_admonitions = "html"\n',
    )
    assert '<div class="admonition tip">' in md
    assert "Handy hint." in md
    assert "</div>" in md
    assert "{{< admonition" not in md


def test_generic_admonition_html_mode_has_title(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        .. admonition:: Heads Up

           Body text.
        """,
        extra_conf='doxtr_hugo_admonitions = "html"\n',
    )
    assert '<div class="admonition note">' in md
    assert '<p class="admonition-title">Heads Up</p>' in md
    assert "Body text." in md


def test_admonition_body_inline_formatting_preserved(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        .. note::

           Use ``code`` and *emphasis*.
        """,
    )
    assert "{{< admonition note >}}" in md
    assert "`code`" in md
    assert "*emphasis*" in md


# ---------------------------------------------------------------------------
# Extensibility: unmapped nodes are observable, not silently dropped
# ---------------------------------------------------------------------------


class _CustomContainer(nodes.General, nodes.Element):
    """A synthetic node type the translator has no handler for.

    Stands in for a construct a third-party Sphinx extension might emit.
    """


class _StubBuilder:
    """Minimal builder surface the translator touches during traversal."""

    def __init__(self):
        import types

        self.config = types.SimpleNamespace()
        self.env = types.SimpleNamespace(domains={})


def _run_unknown_node(monkeypatch, *, count=1):
    """Drive the translator over ``count`` custom nodes; return warning messages."""
    from docutils import frontend, utils
    from docutils.parsers.rst import Parser  # noqa: F401 (ensures settings populate)

    from doxtr_hugo import translator as translator_mod

    messages: list[str] = []

    def _capture(msg, *args, **kwargs):
        messages.append(msg % args if args else msg)

    monkeypatch.setattr(translator_mod.logger, "warning", _capture)

    settings = frontend.OptionParser(components=()).get_default_values()
    document = utils.new_document("<test>", settings)

    builder = _StubBuilder()
    trans = translator_mod.HugoTranslator(document, builder)

    for _ in range(count):
        node = _CustomContainer()
        node += nodes.Text("inner text")
        document += node

    document.walkabout(trans)
    return trans, builder, messages


def test_unmapped_node_warns_once_and_keeps_text(monkeypatch):
    trans, builder, messages = _run_unknown_node(monkeypatch, count=1)
    # The container markup is dropped but its text child is preserved.
    assert "inner text" in trans.astext()
    # Exactly one warning, naming the node type and the handler to add.
    assert len(messages) == 1
    assert "_CustomContainer" in messages[0]
    assert "visit__CustomContainer" in messages[0]


def test_unmapped_node_warning_dedupes_per_type_across_the_build(monkeypatch):
    # Three instances of the same unmapped type -> a single warning, because the
    # warned-type set lives on the builder (shared across documents/translators).
    trans, builder, messages = _run_unknown_node(monkeypatch, count=3)
    assert len(messages) == 1
    assert "_CustomContainer" in getattr(builder, "_doxtr_unmapped_node_types")


# ---------------------------------------------------------------------------
# images / figures / raw-Markdown (diagram render-pass output)
# ---------------------------------------------------------------------------


def _translate_nodes(*child_nodes):
    """Run a bare translator over ``child_nodes`` and return the Markdown."""
    from docutils import frontend, utils

    from doxtr_hugo import translator as translator_mod

    settings = frontend.OptionParser(components=()).get_default_values()
    document = utils.new_document("<test>", settings)
    builder = _StubBuilder()
    trans = translator_mod.HugoTranslator(document, builder)
    for node in child_nodes:
        document += node
    document.walkabout(trans)
    return trans.astext()


def test_image_node_emits_markdown_image():
    md = _translate_nodes(nodes.image(uri="diagram-abc123.svg", alt="a flow"))
    assert "![a flow](diagram-abc123.svg)" in md


def test_image_node_without_alt_emits_bare_reference():
    md = _translate_nodes(nodes.image(uri="diagram-abc123.png"))
    assert "![](diagram-abc123.png)" in md


def test_image_node_with_title_quotes_it():
    img = nodes.image(uri="x.svg", alt="alt")
    img["title"] = 'a "quoted" title'
    md = _translate_nodes(img)
    assert '![alt](x.svg "a \\"quoted\\" title")' in md


def test_image_node_without_uri_is_skipped():
    # A degenerate image with no uri must not emit a broken ![](...) reference.
    md = _translate_nodes(nodes.image())
    assert "![" not in md


def test_figure_node_emits_image_then_italic_caption():
    figure = nodes.figure()
    figure += nodes.image(uri="pic.svg", alt="pic")
    caption = nodes.caption()
    caption += nodes.Text("The caption")
    figure += caption
    md = _translate_nodes(figure)
    assert "![pic](pic.svg)" in md
    assert "*The caption*" in md


def test_rawmarkdown_node_emits_literal_markdown():
    from doxtr_hugo.render_pass import RawMarkdown

    node = RawMarkdown()
    node["markdown"] = "```d2\na -> b\n```"
    md = _translate_nodes(node)
    assert "```d2\na -> b\n```" in md


# ---------------------------------------------------------------------------
# invisible / build-time-only nodes must not leak their text into the output
# ---------------------------------------------------------------------------


def test_comment_node_is_not_rendered():
    # An RST comment (``.. this is a comment``) carries its text as a child.
    # It must produce no output, matching the docutils HTML writer.
    md = _translate_nodes(nodes.comment("", "this is a secret comment"))
    assert "secret comment" not in md
    assert md.strip() == ""


def test_comment_between_paragraphs_is_dropped():
    para_before = nodes.paragraph("", "", nodes.Text("before"))
    comment = nodes.comment("", "hidden note")
    para_after = nodes.paragraph("", "", nodes.Text("after"))
    md = _translate_nodes(para_before, comment, para_after)
    assert "before" in md
    assert "after" in md
    assert "hidden note" not in md


def test_substitution_definition_is_not_rendered():
    node = nodes.substitution_definition("", nodes.Text("replacement body"))
    md = _translate_nodes(node)
    assert "replacement body" not in md


def test_system_message_is_not_rendered():
    msg = nodes.system_message("", nodes.paragraph("", "", nodes.Text("parse error")))
    md = _translate_nodes(msg)
    assert "parse error" not in md


def test_meta_node_is_not_rendered():
    node = nodes.meta()
    node["content"] = "noindex"
    assert _translate_nodes(node).strip() == ""


def test_pending_node_drops_staged_children():
    # ``pending`` is the one suppressed type that can hold staged children; the
    # SkipNode handler must drop them too, not just the container.
    node = nodes.pending(None)
    node += nodes.Text("staged text")
    assert "staged text" not in _translate_nodes(node)


def test_rst_comment_directive_not_in_output(tmp_path):
    # End-to-end: a real ``.. comment`` directive in RST must parse to a
    # ``comment`` node that reaches the handler and leaves no trace, while
    # surrounding paragraphs survive. Closes the parser->handler gap that the
    # synthetic-node tests above cannot cover.
    md = _build(
        tmp_path,
        """
        Title
        =====

        Intro.

        .. this is a secret comment
           spanning two lines

        Outro.
        """,
    )
    assert "secret comment" not in md
    assert "spanning two lines" not in md
    assert "Intro." in md
    assert "Outro." in md


# ---------------------------------------------------------------------------
# structural / semantic node handlers (MEDIUM/HIGH priority mappings)
# ---------------------------------------------------------------------------


def test_raw_html_passthrough(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        .. raw:: html

           <div class="custom">hi</div>
        """,
    )
    assert '<div class="custom">hi</div>' in md


def test_raw_non_html_is_dropped_with_warning(tmp_path):
    # A raw latex block has no Hugo mapping; it must not leak into the page.
    md = _build(
        tmp_path,
        r"""
        Title
        =====

        .. raw:: latex

           \somelatex
        """,
    )
    assert "somelatex" not in md


def test_rubric_renders_as_bold_line(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        .. rubric:: Footnotes
        """,
    )
    assert "**Footnotes**" in md


def test_field_list_renders_as_bold_labelled_bullets(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        :Author: Jane
        :Version: 1.0
        """,
    )
    assert "- **Author:** Jane" in md
    assert "- **Version:** 1.0" in md


def test_definition_list_renders_goldmark_syntax(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        term one
            definition of term one
        """,
    )
    assert "term one" in md
    assert ": definition of term one" in md


def test_definition_body_code_block_is_not_broken_by_marker(tmp_path):
    # A definition whose body contains a block construct (here a literal/code
    # block) must emit the Goldmark ``: `` marker only on the FIRST line and
    # indent the continuation by four spaces. Prefixing every line with ``: ``
    # corrupts the fenced code block (each line becomes its own one-line
    # definition and the fences render empty), which is the "Literal Blocks"
    # rendering bug under Basics -> Text Formatting.
    md = _build(
        tmp_path,
        """
        Title
        =====

        Literal Blocks
            Intro sentence.

            ::

               code line one
               code line two
        """,
    )
    # The term and the single definition marker are present.
    assert "Literal Blocks" in md
    assert ": Intro sentence." in md
    # The fenced code block survives intact: a fence line and the code lines
    # appear, and NO code line is turned into its own ``: `` definition.
    assert "```" in md
    assert "code line one" in md
    assert ": code line one" not in md
    assert ": ```" not in md
    # Continuation lines (the code block) are indented four spaces, not marked.
    assert "    ```" in md or "    code line one" in md


def test_title_reference_renders_as_emphasis():
    ref = nodes.title_reference()
    ref += nodes.Text("some title")
    md = _translate_nodes(ref)
    assert "*some title*" in md


# ---------------------------------------------------------------------------
# footnotes (Goldmark GFM footnote extension)
# ---------------------------------------------------------------------------


def _footnote_reference(refid, label_text):
    """Build a docutils ``footnote_reference`` like the RST parser emits.

    The single text child is the rendered label (``1``); the translator must
    use ``refid`` for the GFM ``[^label]`` and must not leak the label text.
    """
    ref = nodes.footnote_reference(refid=refid)
    ref += nodes.Text(label_text)
    return ref


def _footnote(fid, label_text, *body_nodes):
    """Build a docutils ``footnote`` (``label`` + body) like the parser emits."""
    fn = nodes.footnote(ids=[fid], backrefs=[f"footnote-reference-{label_text}"])
    label = nodes.label()
    label += nodes.Text(label_text)
    fn += label
    body = nodes.paragraph()
    for b in body_nodes:
        body += b
    fn += body
    return fn


def test_footnote_reference_emits_gfm_caret_reference():
    md = _translate_nodes(_footnote_reference("1", "1"))
    assert "[^1]" in md
    # The rendered label text must not leak into the body as a bare "1".
    assert md.strip() == "[^1]"


def test_footnote_definition_emits_gfm_definition_line():
    md = _translate_nodes(_footnote("1", "1", nodes.Text("First footnote text.")))
    assert "[^1]: First footnote text." in md
    # The docutils-generated label ("1") must not leak as a standalone token.
    assert "\n1\n" not in md


def test_footnote_reference_and_definition_share_the_same_label():
    para = nodes.paragraph()
    para += nodes.Text("See ")
    para += _footnote_reference("note", "1")
    para += nodes.Text(".")
    fn = _footnote("note", "1", nodes.Text("A named footnote."))
    md = _translate_nodes(para, fn)
    assert "See [^note]." in md
    assert "[^note]: A named footnote." in md


def test_footnote_definition_preserves_inline_markup():
    fn = nodes.footnote(ids=["f1"], backrefs=["footnote-reference-1"])
    label = nodes.label()
    label += nodes.Text("1")
    fn += label
    body = nodes.paragraph()
    body += nodes.Text("With ")
    em = nodes.emphasis()
    em += nodes.Text("emphasis")
    body += em
    body += nodes.Text(".")
    fn += body
    md = _translate_nodes(fn)
    assert "[^f1]: With *emphasis*." in md


def test_footnotes_end_to_end_build(tmp_path):
    md = _build(
        tmp_path,
        """
        Title
        =====

        A fact [#fact]_ and a note [1]_.

        .. [#fact] The fact detail.
        .. [1] Numbered detail.
        """,
    )
    import re

    # Named auto-footnote: reference and definition agree on the id-derived label.
    assert "[^fact]" in md
    assert "[^fact]: The fact detail." in md
    # Numbered footnote: Sphinx assigns an opaque id; both sides must agree on it,
    # and the definition must carry the body text.
    ref_labels = set(re.findall(r"\[\^([\w-]+)\](?!:)", md))
    def_labels = set(re.findall(r"\[\^([\w-]+)\]:", md))
    assert "fact" in ref_labels and "fact" in def_labels
    # The numbered footnote produced a matching reference/definition pair that is
    # not the named one, proving reference and definition share a label.
    numbered = (ref_labels & def_labels) - {"fact"}
    assert len(numbered) == 1
    (num_label,) = numbered
    assert f"[^{num_label}]: Numbered detail." in md
    # No raw reStructuredText footnote syntax leaked into the body.
    assert "[1]_" not in md
    assert "[#fact]_" not in md
