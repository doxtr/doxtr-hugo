"""Tests for the builder-side diagram render pass (:mod:`doxtr_hugo.render_pass`).

These exercise the detect → render → rewrite → record pipeline with a fake
builder/doctree and an injected diagram renderer, so no backend binary is
required (mirroring the style of ``tests/test_diagrams.py``).
"""

from __future__ import annotations

import types

from docutils import nodes

from doxtr_hugo import diagrams, render_pass
from doxtr_hugo.assets import Manifest
from doxtr_hugo.render_pass import RawMarkdown, render_page_diagrams, copy_page_images


class _Doctree(nodes.Element):
    """A minimal document-like container supporting ``findall`` + child ops."""


def _plantuml_node(uml):
    class plantuml(nodes.General, nodes.Element):  # noqa: N801
        pass

    node = plantuml()
    node["uml"] = uml
    return node


def _fake_builder(tmp_path, **config):
    cfg = {
        "doxtr_hugo_on_missing_backend": "warn-passthrough",
        "doxtr_hugo_image_format": "auto",
        "doxtr_hugo_raster_fallback": "png",
        "doxtr_hugo_process_images": False,
    }
    cfg.update(config)
    return types.SimpleNamespace(
        config=types.SimpleNamespace(**cfg),
        outdir=str(tmp_path),
        srcdir=str(tmp_path / "src"),
    )


def test_render_pass_rewrites_diagram_to_image_and_records_manifest(tmp_path, monkeypatch):
    bundle = tmp_path / "content" / "page"
    bundle.mkdir(parents=True)
    builder = _fake_builder(tmp_path)
    manifest = Manifest()

    doctree = _Doctree()
    para = nodes.paragraph()
    diagram = _plantuml_node("@startuml\nA->B\n@enduml")
    para += diagram
    doctree += para

    def fake_render(node, out_dir, **kwargs):
        # Emulate a successful SVG render writing the file.
        out = __import__("pathlib").Path(out_dir) / "diagram-deadbeef.svg"
        out.write_bytes(b"<svg/>")
        return diagrams.RenderResult(
            path=str(out),
            filename="diagram-deadbeef.svg",
            fmt="svg",
            backend_key="plantuml",
            decision=diagrams.FormatDecision(fmt="svg", reason="prefer-svg"),
        )

    monkeypatch.setattr(render_pass.diagrams, "render_diagram", fake_render)

    render_page_diagrams(builder, "page", doctree, str(bundle), manifest)

    # The diagram node was replaced in place with an image node.
    images = list(doctree.findall(nodes.image))
    assert len(images) == 1
    assert images[0]["uri"] == "diagram-deadbeef.svg"
    # And recorded in the manifest with the chosen format.
    records = manifest.records()
    assert len(records) == 1
    assert records[0].fmt == "svg"
    assert records[0].kind == "diagram"
    assert records[0].relpath == "content/page/diagram-deadbeef.svg"


def test_render_pass_missing_backend_passthrough(tmp_path, monkeypatch):
    bundle = tmp_path / "content" / "page"
    bundle.mkdir(parents=True)
    builder = _fake_builder(tmp_path, doxtr_hugo_on_missing_backend="warn-passthrough")
    manifest = Manifest()

    doctree = _Doctree()
    diagram = _plantuml_node("@startuml\nA->B\n@enduml")
    doctree += diagram

    # Backend unavailable → render returns None → degrade policy applies.
    monkeypatch.setattr(
        render_pass.diagrams, "render_diagram", lambda *a, **k: None
    )

    render_page_diagrams(builder, "page", doctree, str(bundle), manifest)

    raws = [n for n in doctree.findall() if isinstance(n, RawMarkdown)]
    assert len(raws) == 1
    assert "@startuml" in raws[0]["markdown"]
    # No asset recorded for a passthrough (nothing was rendered).
    assert len(manifest) == 0


def test_render_pass_missing_backend_skip(tmp_path, monkeypatch):
    bundle = tmp_path / "content" / "page"
    bundle.mkdir(parents=True)
    builder = _fake_builder(tmp_path, doxtr_hugo_on_missing_backend="warn-skip")
    manifest = Manifest()

    doctree = _Doctree()
    doctree += _plantuml_node("@startuml\nA->B\n@enduml")

    monkeypatch.setattr(
        render_pass.diagrams, "render_diagram", lambda *a, **k: None
    )

    render_page_diagrams(builder, "page", doctree, str(bundle), manifest)

    # The node is removed entirely (no image, no raw markdown).
    assert not list(doctree.findall(nodes.image))
    assert not [n for n in doctree.findall() if isinstance(n, RawMarkdown)]


def test_render_pass_render_failure_drops_node(tmp_path, monkeypatch):
    bundle = tmp_path / "content" / "page"
    bundle.mkdir(parents=True)
    builder = _fake_builder(tmp_path)
    manifest = Manifest()

    doctree = _Doctree()
    doctree += _plantuml_node("@startuml\nbad\n@enduml")

    def boom(*a, **k):
        raise diagrams.DiagramRenderError("backend blew up")

    monkeypatch.setattr(render_pass.diagrams, "render_diagram", boom)

    # Must not raise; the node is dropped and the build continues.
    render_page_diagrams(builder, "page", doctree, str(bundle), manifest)
    assert not list(doctree.findall(nodes.image))
    assert len(manifest) == 0


def test_copy_page_images_colocates_authored_image(tmp_path):
    srcdir = tmp_path / "src"
    (srcdir / "img").mkdir(parents=True)
    png = srcdir / "img" / "logo.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"rest")

    bundle = tmp_path / "content" / "page"
    bundle.mkdir(parents=True)
    builder = _fake_builder(tmp_path)
    manifest = Manifest()

    doctree = _Doctree()
    img = nodes.image(uri="img/logo.png", alt="logo")
    doctree += img

    copy_page_images(builder, "page", doctree, str(bundle), manifest)

    # URI rewritten to a bare co-located hashed filename.
    assert "/" not in img["uri"]
    assert img["uri"].endswith(".png")
    assert (bundle / img["uri"]).is_file()
    records = manifest.records()
    assert len(records) == 1
    assert records[0].kind == "image"
    assert records[0].fmt == "png"


def test_copy_page_images_skips_bare_and_remote_uris(tmp_path):
    bundle = tmp_path / "content" / "page"
    bundle.mkdir(parents=True)
    builder = _fake_builder(tmp_path)
    manifest = Manifest()

    doctree = _Doctree()
    bare = nodes.image(uri="diagram-abc.svg")  # already co-located (render pass)
    remote = nodes.image(uri="https://example.com/x.png")
    doctree += bare
    doctree += remote

    copy_page_images(builder, "page", doctree, str(bundle), manifest)

    assert bare["uri"] == "diagram-abc.svg"  # untouched
    assert remote["uri"] == "https://example.com/x.png"  # untouched
    assert len(manifest) == 0


def test_copy_page_images_warns_once_for_missing(tmp_path, monkeypatch):
    bundle = tmp_path / "content" / "page"
    bundle.mkdir(parents=True)
    builder = _fake_builder(tmp_path)
    manifest = Manifest()

    doctree = _Doctree()
    doctree += nodes.image(uri="img/does-not-exist.png")
    doctree += nodes.image(uri="img/does-not-exist.png")

    warnings: list[str] = []
    monkeypatch.setattr(
        render_pass.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
    )

    copy_page_images(builder, "page", doctree, str(bundle), manifest)
    # Warn once per distinct missing uri; nothing recorded.
    assert len(warnings) == 1
    assert len(manifest) == 0


# ---------------------------------------------------------------------------
# math render pass (guide §4.3)
# ---------------------------------------------------------------------------


def _math_node(latex, block=False):
    from docutils import nodes as _n

    cls = _n.math_block if block else _n.math
    node = cls()
    node += _n.Text(latex)
    # math_block carries latex in astext(); math carries it too.
    return node


def test_render_page_math_png_mode_emits_image_and_records(tmp_path):
    bundle = tmp_path / "content" / "page"
    bundle.mkdir(parents=True)
    builder = _fake_builder(tmp_path, doxtr_hugo_math="png")
    manifest = Manifest()

    doctree = _Doctree()
    doctree += _math_node("E = mc^2", block=True)

    called: list[tuple] = []

    def fake_renderer(source, out_path, *, block, dpi):
        called.append((source, block))
        __import__("pathlib").Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n")

    # Inject the renderer through the render_page_math seam so no matplotlib
    # call happens and we can assert the fake was actually invoked.
    render_pass.render_page_math(
        builder, "page", doctree, str(bundle), manifest, renderer=fake_renderer
    )

    # The injected renderer must have been exercised (not a no-op monkeypatch).
    assert called == [("E = mc^2", True)]
    images = list(doctree.findall(nodes.image))
    assert len(images) == 1
    assert images[0]["uri"].startswith("math-") and images[0]["uri"].endswith(".png")
    records = manifest.records()
    assert len(records) == 1 and records[0].kind == "math" and records[0].fmt == "png"


def test_render_page_math_mathjax_mode_emits_rawmarkdown(tmp_path):
    bundle = tmp_path / "content" / "page"
    bundle.mkdir(parents=True)
    builder = _fake_builder(tmp_path, doxtr_hugo_math="mathjax")
    manifest = Manifest()

    doctree = _Doctree()
    doctree += _math_node("E = mc^2", block=True)

    render_pass.render_page_math(builder, "page", doctree, str(bundle), manifest)

    raws = [n for n in doctree.findall() if isinstance(n, RawMarkdown)]
    assert len(raws) == 1
    assert "$$" in raws[0]["markdown"] and "E = mc^2" in raws[0]["markdown"]
    assert len(manifest) == 0  # no asset emitted in mathjax mode


def test_render_page_math_render_failure_falls_back_to_mathjax(tmp_path, monkeypatch):
    bundle = tmp_path / "content" / "page"
    bundle.mkdir(parents=True)
    builder = _fake_builder(tmp_path, doxtr_hugo_math="png")
    manifest = Manifest()

    doctree = _Doctree()
    doctree += _math_node(r"\tfrac{1}{2}", block=False)

    boom_called: list[tuple] = []

    def boom(source, out_path, *, block, dpi):
        boom_called.append((source, block))
        raise ValueError("Unknown symbol: \\tfrac")

    warnings: list[str] = []
    monkeypatch.setattr(
        render_pass.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
    )

    # Inject the failing renderer through the render_page_math seam. Must not
    # raise; a render failure degrades to MathJax passthrough.
    render_pass.render_page_math(
        builder, "page", doctree, str(bundle), manifest, renderer=boom
    )

    # The injected (failing) renderer must have actually been called, and the
    # failure must have produced a MathJax RawMarkdown fallback node.
    assert boom_called == [(r"\tfrac{1}{2}", False)]
    raws = [n for n in doctree.findall() if isinstance(n, RawMarkdown)]
    assert len(raws) == 1
    assert r"\tfrac" in raws[0]["markdown"]
    assert raws[0]["markdown"].startswith(r"\(")  # inline MathJax-delimited
    assert len(warnings) == 1
    assert len(manifest) == 0
