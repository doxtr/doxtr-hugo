"""M4 math tests — PNG pre-render default + mathjax/mathml opt-in (guide §4.3, §5).

Core paths run without invoking matplotlib: the rendering choke point
(``renderer``) is injected, mirroring how the diagram tests inject ``runner``.
A final, opt-in block renders with the *real* matplotlib mathtext engine when it
is importable, and is skipped (never faked) otherwise.
"""

from __future__ import annotations

import pytest

from doxtr_hugo import math_render
from doxtr_hugo.math_render import (
    MATH_MATHJAX,
    MATH_MATHML,
    MATH_PNG,
    MathRenderError,
    MathResult,
    asset_filename,
    detect_math_nodes,
    is_block_math,
    is_math_node,
    math_markdown,
    math_source,
    mathjax_markdown,
    mathml_markdown,
    normalize_mathtext_source,
    png_markdown,
    render_math_png,
)


# ---------------------------------------------------------------------------
# Fake nodes mimicking docutils/Sphinx math nodes
# ---------------------------------------------------------------------------


class math:  # noqa: N801 - name mirrors the real inline node class
    """Mimics the inline ``math`` role node (``latex`` attribute)."""

    def __init__(self, latex=""):
        self._attrs = {"latex": latex}

    def __getitem__(self, key):
        return self._attrs[key]

    def astext(self):
        return self._attrs.get("latex", "")


class math_block:  # noqa: N801 - name mirrors the real block node class
    """Mimics the ``math_block`` directive node (``latex`` attribute)."""

    def __init__(self, latex=""):
        self._attrs = {"latex": latex}

    def __getitem__(self, key):
        return self._attrs[key]

    def astext(self):
        return self._attrs.get("latex", "")


class paragraph:  # noqa: N801 - a non-math node
    def __init__(self, text=""):
        self.text = text


class FakeDoctree:
    """Minimal doctree exposing ``findall`` for traversal tests."""

    def __init__(self, nodes):
        self._nodes = list(nodes)

    def findall(self):
        return iter(self._nodes)


class FakeDoctreeTraverse:
    """Doctree that only exposes the older ``traverse`` API."""

    def __init__(self, nodes):
        self._nodes = list(nodes)

    def traverse(self):
        return iter(self._nodes)


# ---------------------------------------------------------------------------
# A fake PNG-writing renderer (no matplotlib)
# ---------------------------------------------------------------------------

#: Smallest valid-ish PNG byte signature so sniffers/size checks pass.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def fake_renderer(source, out_path, *, block, dpi):
    with open(out_path, "wb") as fh:
        fh.write(_PNG_BYTES)


def empty_renderer(source, out_path, *, block, dpi):
    # Writes nothing — exercises the empty-output guard.
    open(out_path, "wb").close()


# ---------------------------------------------------------------------------
# Detection + source extraction
# ---------------------------------------------------------------------------


def test_is_math_node_inline_and_block():
    assert is_math_node(math("x^2"))
    assert is_math_node(math_block("x^2"))
    assert not is_math_node(paragraph("hi"))


def test_is_block_math_distinguishes_inline_vs_block():
    assert not is_block_math(math("x"))
    assert is_block_math(math_block("x"))


def test_math_source_mapping_access():
    assert math_source(math(r"\alpha + \beta")) == r"\alpha + \beta"
    assert math_source(math_block("E = mc^2")) == "E = mc^2"


def test_math_source_attribute_fallback():
    class weird:
        latex = "a_b"

    assert math_source(weird()) == "a_b"


def test_math_source_astext_last_resort():
    class only_astext:
        def astext(self):
            return "fallback"

    assert math_source(only_astext()) == "fallback"


def test_math_source_empty():
    class empty:
        pass

    assert math_source(empty()) == ""


def test_detect_math_nodes_findall():
    nodes = [math("a"), paragraph("x"), math_block("b")]
    found = list(detect_math_nodes(FakeDoctree(nodes)))
    assert len(found) == 2
    assert found[0][1] is False  # inline
    assert found[1][1] is True   # block


def test_detect_math_nodes_traverse_fallback():
    nodes = [math_block("b")]
    found = list(detect_math_nodes(FakeDoctreeTraverse(nodes)))
    assert len(found) == 1
    assert found[0][1] is True


# ---------------------------------------------------------------------------
# Content-addressed filenames
# ---------------------------------------------------------------------------


def test_asset_filename_shape():
    name = asset_filename("x^2", block=False)
    assert name.startswith("math-")
    assert name.endswith(".png")


def test_asset_filename_deterministic_and_dedup():
    assert asset_filename("x^2", block=False) == asset_filename("x^2", block=False)


def test_asset_filename_block_vs_inline_differ():
    assert asset_filename("x", block=True) != asset_filename("x", block=False)


def test_asset_filename_source_change_busts_cache():
    assert asset_filename("x", block=False) != asset_filename("y", block=False)


# ---------------------------------------------------------------------------
# PNG rendering (default mode)
# ---------------------------------------------------------------------------


def test_render_math_png_writes_file(tmp_path):
    result = render_math_png(math("x^2"), tmp_path, renderer=fake_renderer)
    assert isinstance(result, MathResult)
    assert result.block is False
    assert result.reused is False
    out = tmp_path / result.filename
    assert out.exists() and out.stat().st_size > 0


def test_render_math_png_block_node(tmp_path):
    result = render_math_png(math_block("E=mc^2"), tmp_path, renderer=fake_renderer)
    assert result.block is True


def test_render_math_png_raw_source_block_flag(tmp_path):
    result = render_math_png("a+b", tmp_path, block=True, renderer=fake_renderer)
    assert result.block is True


def test_render_math_png_idempotent_reuse(tmp_path):
    calls = []

    def counting(source, out_path, *, block, dpi):
        calls.append(out_path)
        fake_renderer(source, out_path, block=block, dpi=dpi)

    r1 = render_math_png("x^2", tmp_path, renderer=counting)
    r2 = render_math_png("x^2", tmp_path, renderer=counting)
    assert r1.filename == r2.filename
    assert r1.reused is False
    assert r2.reused is True
    assert len(calls) == 1  # second call reused the existing file


def test_render_math_png_empty_output_raises(tmp_path):
    with pytest.raises(MathRenderError):
        render_math_png("x", tmp_path, renderer=empty_renderer)


# ---------------------------------------------------------------------------
# Markdown emission per mode
# ---------------------------------------------------------------------------


def test_png_markdown_inline():
    result = MathResult(path="/x/math-abc.png", filename="math-abc.png", block=False)
    md = png_markdown(result, "x^2")
    assert md == "![x^2](math-abc.png)"


def test_png_markdown_block():
    result = MathResult(path="/x/math-abc.png", filename="math-abc.png", block=True)
    md = png_markdown(result, "E=mc^2")
    assert "math-abc.png" in md
    assert md.startswith("![")


def test_png_markdown_escapes_alt_brackets_and_backslash():
    result = MathResult(path="/x/m.png", filename="m.png", block=False)
    md = png_markdown(result, r"x_{a]}")
    # The closing bracket is escaped so it cannot truncate the alt text.
    assert r"\]" in md
    assert md.endswith("(m.png)")


def test_mathjax_markdown_inline_and_block():
    assert mathjax_markdown("x^2", block=False) == r"\(x^2\)"
    assert mathjax_markdown("E=mc^2", block=True) == "$$\nE=mc^2\n$$"


def test_mathml_markdown_passthrough_when_available():
    mathml = "<math><mi>x</mi></math>"
    assert mathml_markdown("x", mathml, block=False) == mathml


def test_mathml_markdown_degrades_when_absent():
    md = mathml_markdown("a < b", None, block=True)
    assert md.startswith("<math")
    assert 'display="block"' in md
    assert "&lt;" in md  # XML-escaped


# ---------------------------------------------------------------------------
# math_markdown dispatch
# ---------------------------------------------------------------------------


def test_math_markdown_png_mode_emits_image_and_result(tmp_path):
    md, result = math_markdown(
        math("x^2"), tmp_path, mode=MATH_PNG, renderer=fake_renderer
    )
    assert result is not None
    assert result.filename in md
    assert (tmp_path / result.filename).exists()


def test_math_markdown_unknown_mode_defaults_to_png(tmp_path):
    md, result = math_markdown(
        math("x^2"), tmp_path, mode="bogus", renderer=fake_renderer
    )
    assert result is not None
    assert result.filename in md


def test_math_markdown_mathjax_mode_no_asset(tmp_path):
    md, result = math_markdown(math("x^2"), tmp_path, mode=MATH_MATHJAX)
    assert result is None
    assert md == r"\(x^2\)"
    assert not list(tmp_path.iterdir())  # no file emitted


def test_math_markdown_mathml_mode_no_asset(tmp_path):
    md, result = math_markdown(
        math_block("x"), tmp_path, mode=MATH_MATHML, mathml="<math/>"
    )
    assert result is None
    assert md == "<math/>"
    assert not list(tmp_path.iterdir())


def test_math_markdown_block_detection_from_node(tmp_path):
    md, result = math_markdown(
        math_block("E=mc^2"), tmp_path, mode=MATH_MATHJAX
    )
    assert md == "$$\nE=mc^2\n$$"


def test_math_markdown_raw_source_string(tmp_path):
    # Passing a raw LaTeX string (not a node) with an explicit block flag.
    md, result = math_markdown(
        "a+b", tmp_path, mode=MATH_PNG, block=True, renderer=fake_renderer
    )
    assert result is not None
    assert result.block is True
    assert result.filename in md


# ---------------------------------------------------------------------------
# Opt-in: real matplotlib render (skipped if unavailable; never faked)
# ---------------------------------------------------------------------------


def _matplotlib_available() -> bool:
    try:
        import matplotlib  # noqa: F401
    except Exception:
        return False
    return True


@pytest.mark.skipif(
    not _matplotlib_available(), reason="matplotlib not importable"
)
def test_render_math_png_real_matplotlib(tmp_path):
    # Uses the module's real default renderer (matplotlib mathtext, Agg).
    result = render_math_png(math_block(r"\int_0^1 x^2 \, dx"), tmp_path)
    out = tmp_path / result.filename
    assert out.exists() and out.stat().st_size > 0
    # Real output must be a PNG (magic bytes) for the §7 format verifier.
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# amsmath alias normalization (mathtext cannot parse \tfrac etc.)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source, expected",
    [
        (r"\tfrac{1}{2}", r"\frac{1}{2}"),
        (r"\dfrac{a}{b}", r"\frac{a}{b}"),
        (r"\tbinom{n}{k}", r"\binom{n}{k}"),
        (r"\dbinom{n}{k}", r"\binom{n}{k}"),
        # Multiple occurrences in one formula are all rewritten.
        (r"\tfrac{1}{2} + \dfrac{3}{4}", r"\frac{1}{2} + \frac{3}{4}"),
        # Already-supported macros and surrounding LaTeX are untouched.
        (r"s = ut + \frac{1}{2}at^2", r"s = ut + \frac{1}{2}at^2"),
        (r"x^2 + y^2", r"x^2 + y^2"),
    ],
)
def test_normalize_mathtext_source(source, expected):
    assert normalize_mathtext_source(source) == expected


def test_normalize_mathtext_source_requires_whole_token():
    # A longer macro that merely starts with an alias name is not rewritten.
    assert normalize_mathtext_source(r"\tfracky") == r"\tfracky"


@pytest.mark.skipif(
    not _matplotlib_available(), reason="matplotlib not importable"
)
def test_render_math_png_tfrac_real_matplotlib(tmp_path):
    # Regression: \tfrac is valid LaTeX but unsupported by mathtext; it must be
    # normalized to \frac and render to a real PNG instead of raising and
    # falling back to MathJax passthrough.
    result = render_math_png(math_block(r"s = ut + \tfrac{1}{2}at^2"), tmp_path)
    out = tmp_path / result.filename
    assert out.exists() and out.stat().st_size > 0
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
