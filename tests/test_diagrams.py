"""M3 diagram tests — detection + render-to-file (guide §4.1, §4.3).

These tests exercise :mod:`doxtr_hugo.diagrams` without requiring any backend
binary: binary discovery (``which``) and the subprocess call (``runner``) are
injected, mirroring how the roadmap tests monkeypatch ``plantuml``. A final,
opt-in block renders with the *real* ``plantuml``/``dot`` binaries when they
happen to be installed, and is skipped (never faked) otherwise.
"""

from __future__ import annotations

import importlib.util
import shutil

import pytest

from doxtr_hugo import diagrams
from doxtr_hugo.diagrams import (
    BACKENDS,
    DegradeResult,
    DiagramRenderError,
    MissingBackendError,
    RenderResult,
    asset_filename,
    degrade_missing_backend,
    detect_backend,
    detect_diagram_nodes,
    diagram_source,
    node_backend_key,
    passthrough_markdown,
    render_diagram,
)


# ---------------------------------------------------------------------------
# Fake nodes mimicking sphinxcontrib.plantuml / sphinx.ext.graphviz
# ---------------------------------------------------------------------------


class plantuml:  # noqa: N801 - name mirrors the real node class
    """Mimics ``sphinxcontrib.plantuml.plantuml`` (``uml`` attribute)."""

    def __init__(self, uml):
        self.uml = uml

    def __getitem__(self, key):
        return {"uml": self.uml}[key]


class graphviz:  # noqa: N801 - name mirrors the real node class
    """Mimics ``sphinx.ext.graphviz.graphviz`` (``code`` attr key)."""

    def __init__(self, code):
        self._attrs = {"code": code}

    def __getitem__(self, key):
        return self._attrs[key]


class mermaid:  # noqa: N801 - name mirrors the real node class
    """Mimics ``sphinxcontrib.mermaid.mermaid`` (``code`` attr key)."""

    def __init__(self, code):
        self._attrs = {"code": code}

    def __getitem__(self, key):
        return self._attrs[key]


class d2node:  # noqa: N801 - name mirrors doxtr_d2.nodes.d2node
    """Mimics ``doxtr_d2.nodes.d2node`` (``d2_script`` inline / ``d2_file``)."""

    def __init__(self, d2_script=None, d2_file=None, source="inline", options=None):
        self._attrs = {
            "d2_script": d2_script,
            "d2_file": d2_file,
            "source": source,
            "options": options or {},
        }

    def __getitem__(self, key):
        value = self._attrs.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def get(self, key, default=None):
        return self._attrs.get(key, default)


class diagrams_node:  # noqa: N801 - mirrors sphinx_diagrams.diagrams
    """Mimics ``sphinx_diagrams.diagrams`` (``code`` attr key, Python source)."""

    def __init__(self, code):
        self._attrs = {"code": code}

    def __getitem__(self, key):
        return self._attrs[key]

    def get(self, key, default=None):
        return self._attrs.get(key, default)


# The ``diagrams`` backend is keyed on the node *class name* being ``diagrams``;
# alias the test node so ``node_backend_key`` recognizes it.
diagrams_node.__name__ = "diagrams"


class paragraph:  # noqa: N801 - a non-diagram node
    def __getitem__(self, key):
        raise KeyError(key)


class FakeDoctree:
    def __init__(self, nodes):
        self._nodes = nodes

    def findall(self, *a, **k):
        return iter(self._nodes)


PRESENT = lambda name: f"/usr/bin/{name}"  # noqa: E731 - test stub
ABSENT = lambda name: None  # noqa: E731 - test stub


# ---------------------------------------------------------------------------
# node_backend_key / diagram_source
# ---------------------------------------------------------------------------


def test_node_backend_key_maps_known_nodes():
    assert node_backend_key(plantuml("@startuml\n@enduml")) == "plantuml"
    assert node_backend_key(graphviz("digraph{}")) == "graphviz"
    assert node_backend_key(paragraph()) is None


def test_diagram_source_extracts_uml_and_code():
    assert diagram_source(plantuml("@startuml\nA->B\n@enduml")).startswith("@startuml")
    assert diagram_source(graphviz("digraph G { a -> b }")) == "digraph G { a -> b }"


class AttrNode:
    """Carries source via attribute (no mapping access) + list content."""

    def __init__(self, content):
        self.content = content

    def __getitem__(self, key):
        raise TypeError("not subscriptable")


def test_diagram_source_falls_back_to_attribute_and_joins_lists():
    assert diagram_source(AttrNode("single")) == "single"
    assert diagram_source(AttrNode(["a", "b"])) == "a\nb"


def test_diagram_source_last_resort_astext():
    class AstextNode:
        def __getitem__(self, key):
            raise TypeError

        def astext(self):
            return "rendered"

    assert diagram_source(AstextNode()) == "rendered"


# ---------------------------------------------------------------------------
# detect_backend (graceful-degradation signal)
# ---------------------------------------------------------------------------


def test_detect_backend_returns_backend_when_binary_present():
    b = detect_backend("plantuml", which=PRESENT)
    assert b is not None and b.binary == "plantuml"
    g = detect_backend("graphviz", which=PRESENT)
    assert g is not None and g.binary == "dot"


def test_detect_backend_none_when_binary_absent():
    assert detect_backend("plantuml", which=ABSENT) is None


def test_detect_backend_none_for_unknown_key():
    assert detect_backend("nope", which=PRESENT) is None


# ---------------------------------------------------------------------------
# Mermaid backend is gated (guide §4.3/§5): present only when `mmdc` is on PATH
# ---------------------------------------------------------------------------


def test_mermaid_node_detected_and_source_extracted():
    node = mermaid("graph TD; A-->B")
    assert node_backend_key(node) == "mermaid"
    assert diagram_source(node) == "graph TD; A-->B"


def test_detect_backend_mermaid_present_when_mmdc_on_path():
    b = detect_backend("mermaid", which=PRESENT)
    assert b is not None and b.binary == "mmdc"
    assert b.stdin_source is False  # file-input backend


def test_detect_backend_mermaid_none_when_mmdc_absent():
    # Gated: absent mmdc -> None, so the missing-backend policy degrades.
    assert detect_backend("mermaid", which=ABSENT) is None


def test_render_mermaid_missing_backend_returns_none(tmp_path):
    res = render_diagram(mermaid("graph TD; A-->B"), tmp_path, which=ABSENT)
    assert res is None


def test_render_mermaid_file_input_backend(tmp_path):
    """mmdc reads an input file and writes `-o <out>`; source written to temp."""
    captured = {}

    def fake_runner(argv, source, stdin_source):
        captured["argv"] = list(argv)
        captured["stdin_source"] = stdin_source
        out = argv[argv.index("-o") + 1]
        with open(out, "wb") as f:
            f.write(b"<svg>mermaid</svg>")
        return b""

    res = render_diagram(
        mermaid("graph TD; A-->B"),
        tmp_path,
        which=PRESENT,
        runner=fake_runner,
    )
    assert res is not None
    assert res.backend_key == "mermaid"
    assert res.fmt == "svg"  # prefer-svg default
    assert captured["stdin_source"] is False
    # Build args carry the input placeholder and the chosen format.
    assert captured["argv"][0] == "mmdc"
    assert diagrams.INPUT_PLACEHOLDER in captured["argv"]
    assert "-e" in captured["argv"] and "svg" in captured["argv"]
    assert (tmp_path / res.filename).read_bytes() == b"<svg>mermaid</svg>"


def test_mermaid_missing_backend_degrades_passthrough():
    res = degrade_missing_backend(mermaid("graph TD; A-->B"))
    assert res.action == "passthrough"
    assert res.backend_key == "mermaid"
    assert "```mermaid" in res.markdown


def test_default_runner_file_input_substitutes_temp_path(tmp_path):
    """stdin_source=False writes source to a temp file and substitutes it."""
    from doxtr_hugo.diagrams import _default_runner, INPUT_PLACEHOLDER

    out = tmp_path / "out.svg"
    # `cp <input> <out>` proves the temp input file exists and holds the source.
    if shutil.which("cp") is None:
        pytest.skip("cp not available")
    _default_runner(["cp", INPUT_PLACEHOLDER, str(out)], "graph TD; X-->Y", False)
    assert out.read_text() == "graph TD; X-->Y"


# ---------------------------------------------------------------------------
# asset_filename (deterministic, content-addressed)
# ---------------------------------------------------------------------------


def test_asset_filename_is_deterministic_and_prefixed():
    a = asset_filename("src", "svg", backend_key="plantuml")
    b = asset_filename("src", "svg", backend_key="plantuml")
    assert a == b
    assert a.startswith("diagram-") and a.endswith(".svg")


def test_asset_filename_changes_with_source_format_or_backend():
    base = asset_filename("src", "svg", backend_key="plantuml")
    assert base != asset_filename("src2", "svg", backend_key="plantuml")
    assert base != asset_filename("src", "png", backend_key="plantuml")
    assert base != asset_filename("src", "svg", backend_key="graphviz")


# ---------------------------------------------------------------------------
# detect_diagram_nodes
# ---------------------------------------------------------------------------


def test_detect_diagram_nodes_yields_only_diagrams():
    tree = FakeDoctree([plantuml("@startuml\n@enduml"), paragraph(), graphviz("digraph{}")])
    found = list(detect_diagram_nodes(tree))
    assert [k for _, k in found] == ["plantuml", "graphviz"]


def test_detect_diagram_nodes_respects_backend_filter():
    tree = FakeDoctree([plantuml("@startuml\n@enduml"), graphviz("digraph{}")])
    found = list(detect_diagram_nodes(tree, backends={"graphviz"}))
    assert [k for _, k in found] == ["graphviz"]


# ---------------------------------------------------------------------------
# render_diagram — stdout-emitting backend (PlantUML -pipe), injected runner
# ---------------------------------------------------------------------------


def test_render_stdout_backend_writes_bytes(tmp_path):
    calls = {}

    def fake_runner(argv, source, stdin_source):
        calls["argv"] = list(argv)
        calls["source"] = source
        calls["stdin_source"] = stdin_source
        return b"<svg>fake</svg>"

    res = render_diagram(
        plantuml("@startuml\nA->B\n@enduml"),
        tmp_path,
        which=PRESENT,
        runner=fake_runner,
    )
    assert isinstance(res, RenderResult)
    assert res.fmt == "svg"  # prefer-svg default
    assert res.backend_key == "plantuml"
    assert res.reused is False
    # Argv uses -pipe and the chosen format.
    assert calls["argv"][0] == "plantuml"
    assert "-tsvg" in calls["argv"] and "-pipe" in calls["argv"]
    assert calls["stdin_source"] is True
    # File written with the runner's bytes.
    on_disk = tmp_path / res.filename
    assert on_disk.read_bytes() == b"<svg>fake</svg>"


def test_render_file_writing_backend(tmp_path):
    """Graphviz-style backend writes its own file via -o; runner returns b''."""

    def fake_runner(argv, source, stdin_source):
        # emulate `dot -o <out>` writing the file itself
        out = argv[argv.index("-o") + 1]
        with open(out, "wb") as f:
            f.write(b"<svg>dot</svg>")
        return b""

    res = render_diagram(
        graphviz("digraph G { a -> b }"),
        tmp_path,
        which=PRESENT,
        runner=fake_runner,
    )
    assert res is not None
    assert res.backend_key == "graphviz"
    assert (tmp_path / res.filename).read_bytes() == b"<svg>dot</svg>"


def test_render_missing_backend_returns_none(tmp_path):
    res = render_diagram(plantuml("@startuml\n@enduml"), tmp_path, which=ABSENT)
    assert res is None


def test_render_forced_png_format(tmp_path):
    captured = {}

    def fake_runner(argv, source, stdin_source):
        captured["argv"] = list(argv)
        return b"\x89PNG\r\n\x1a\n"

    res = render_diagram(
        plantuml("@startuml\n@enduml"),
        tmp_path,
        image_format="png",
        which=PRESENT,
        runner=fake_runner,
    )
    assert res.fmt == "png"
    assert "-tpng" in captured["argv"]
    assert res.filename.endswith(".png")


def test_render_is_idempotent_and_reuses_existing(tmp_path):
    n = 0

    def fake_runner(argv, source, stdin_source):
        nonlocal n
        n += 1
        return b"<svg/>"

    node = plantuml("@startuml\nA->B\n@enduml")
    first = render_diagram(node, tmp_path, which=PRESENT, runner=fake_runner)
    second = render_diagram(node, tmp_path, which=PRESENT, runner=fake_runner)
    assert n == 1  # binary invoked once
    assert first.reused is False and second.reused is True
    assert first.filename == second.filename


def test_render_raises_on_empty_output(tmp_path):
    def empty_runner(argv, source, stdin_source):
        return b""  # stdout backend produced nothing and wrote no file

    with pytest.raises(DiagramRenderError):
        render_diagram(
            plantuml("@startuml\n@enduml"),
            tmp_path,
            which=PRESENT,
            runner=empty_runner,
        )


def test_render_raw_source_requires_backend_key(tmp_path):
    with pytest.raises(ValueError):
        render_diagram("@startuml\n@enduml", tmp_path, which=PRESENT)


def test_render_raw_source_with_backend_key(tmp_path):
    res = render_diagram(
        "@startuml\nA->B\n@enduml",
        tmp_path,
        backend_key="plantuml",
        which=PRESENT,
        runner=lambda *a: b"<svg/>",
    )
    assert res is not None and res.backend_key == "plantuml"


def test_default_runner_raises_on_nonzero_exit(tmp_path):
    from doxtr_hugo.diagrams import _default_runner

    # `false` exits non-zero; the runner must raise DiagramRenderError.
    with pytest.raises(DiagramRenderError):
        _default_runner(["false"], "src", True)


# ---------------------------------------------------------------------------
# Graceful degradation on missing backend (guide §4.4)
# ---------------------------------------------------------------------------


def test_passthrough_markdown_fences_with_backend_language():
    md = passthrough_markdown("@startuml\nA->B\n@enduml", "plantuml")
    assert md.startswith("```plantuml\n")
    assert md.endswith("\n```")
    assert "@startuml\nA->B\n@enduml" in md
    # graphviz maps to the `dot` language tag
    assert passthrough_markdown("digraph{}", "graphviz").startswith("```dot\n")
    # unknown backend -> no language hint
    assert passthrough_markdown("x", "mystery").startswith("```\n")


def test_passthrough_markdown_grows_fence_past_embedded_backticks():
    # Source containing a ```-fence must be wrapped in a longer fence so the
    # outer block does not terminate early.
    source = "line\n```\ninner\n```\nend"
    md = passthrough_markdown(source, "plantuml")
    fence = "`" * 4
    assert md.startswith(f"{fence}plantuml\n")
    assert md.endswith(f"\n{fence}")
    assert source in md


def test_degrade_default_is_warn_passthrough():
    res = degrade_missing_backend(plantuml("@startuml\nA->B\n@enduml"))
    assert isinstance(res, DegradeResult)
    assert res.action == "passthrough"
    assert res.backend_key == "plantuml"
    assert res.warning and "plantuml" in res.warning
    assert res.markdown.startswith("```plantuml\n")
    assert "@startuml" in res.markdown


def test_degrade_warn_skip_drops_node():
    res = degrade_missing_backend(graphviz("digraph{}"), policy="warn-skip")
    assert res.action == "skip"
    assert res.markdown == ""
    assert res.warning and "skipping" in res.warning
    assert res.backend_key == "graphviz"


def test_degrade_error_policy_raises():
    with pytest.raises(MissingBackendError):
        degrade_missing_backend(plantuml("@startuml\n@enduml"), policy="error")


def test_degrade_unknown_policy_falls_back_to_passthrough():
    res = degrade_missing_backend(graphviz("digraph{}"), policy="nonsense")
    assert res.action == "passthrough"
    assert res.markdown.startswith("```dot\n")


def test_degrade_raw_source_requires_backend_key():
    with pytest.raises(ValueError):
        degrade_missing_backend("@startuml\n@enduml")


def test_degrade_raw_source_with_backend_key():
    res = degrade_missing_backend("digraph{}", backend_key="graphviz")
    assert res.action == "passthrough"
    assert res.backend_key == "graphviz"
    # warning mentions the resolved binary name (dot), not the key
    assert "dot" in res.warning


def test_degrade_end_to_end_when_render_reports_missing(tmp_path):
    """render_diagram -> None (missing binary) then degrade applies policy."""
    node = plantuml("@startuml\nAlice -> Bob\n@enduml")
    assert render_diagram(node, tmp_path, which=ABSENT) is None
    res = degrade_missing_backend(node, policy="warn-passthrough")
    assert res.action == "passthrough"
    assert "Alice -> Bob" in res.markdown


# ---------------------------------------------------------------------------
# Opt-in: real binaries when present (never faked; skipped otherwise)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("dot") is None, reason="graphviz 'dot' not installed")
def test_real_graphviz_render_svg(tmp_path):
    res = render_diagram(graphviz("digraph G { a -> b }"), tmp_path)
    assert res is not None and res.fmt == "svg"
    data = (tmp_path / res.filename).read_bytes()
    assert b"<svg" in data[:4096]


@pytest.mark.skipif(shutil.which("plantuml") is None, reason="plantuml not installed")
def test_real_plantuml_render_png(tmp_path):
    res = render_diagram(
        plantuml("@startuml\nAlice -> Bob: hi\n@enduml"),
        tmp_path,
        image_format="png",
    )
    assert res is not None and res.fmt == "png"
    data = (tmp_path / res.filename).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# d2 backend (doxtr_d2 'd2node') and diagrams backend (sphinx_diagrams)
# ---------------------------------------------------------------------------


def test_node_backend_key_maps_d2_and_diagrams():
    assert node_backend_key(d2node(d2_script="a -> b")) == "d2"
    assert node_backend_key(diagrams_node("from diagrams import Diagram")) == "diagrams"


def test_diagram_source_extracts_d2_inline_script():
    assert diagram_source(d2node(d2_script="x -> y")) == "x -> y"


def test_diagram_source_reads_d2_file(tmp_path):
    f = tmp_path / "graph.d2"
    f.write_text("a -> b -> c")
    node = d2node(d2_file=str(f), source="file")
    assert diagram_source(node) == "a -> b -> c"


def test_diagram_source_extracts_diagrams_python_code():
    code = "from diagrams import Diagram\n"
    assert diagram_source(diagrams_node(code)) == code


def test_render_d2_prefers_svg_and_writes_output(tmp_path):
    captured = {}

    def fake_runner(argv, source, stdin_source):
        captured["argv"] = list(argv)
        captured["source"] = source
        return b"<svg>d2</svg>"

    res = render_diagram(
        d2node(d2_script="a -> b"),
        tmp_path,
        which=PRESENT,
        runner=fake_runner,
    )
    assert res is not None
    assert res.backend_key == "d2" and res.fmt == "svg"  # prefer-svg (d2 can_svg)
    # argv is `d2 - <out_path>`; output path carries the chosen .svg extension.
    assert captured["argv"][0] == "d2"
    assert captured["argv"][1] == "-"
    assert captured["argv"][2].endswith(".svg")
    assert captured["source"] == "a -> b"
    assert (tmp_path / res.filename).read_bytes() == b"<svg>d2</svg>"


def test_render_d2_extra_args_change_filename_and_argv(tmp_path):
    captured = []

    def fake_runner(argv, source, stdin_source):
        captured.append(list(argv))
        return b"<svg>d2</svg>"

    base = render_diagram(
        d2node(d2_script="a -> b"),
        tmp_path,
        which=PRESENT,
        runner=fake_runner,
    )
    targeted = render_diagram(
        d2node(d2_script="a -> b"),
        tmp_path,
        which=PRESENT,
        runner=fake_runner,
        extra_args=("--target=layers.detail",),
    )
    # Extra args are spliced right after the binary.
    assert captured[-1][1] == "--target=layers.detail"
    # Folding the extra args into the hash yields a distinct filename, so a
    # targeted render does not collide with the plain one.
    assert base.filename != targeted.filename


def test_render_passes_cwd_to_default_runner(tmp_path):
    # PlantUML's `!include` resolves against the document dir; the builder
    # passes that as cwd. Use the real default runner through a tiny stub
    # binary is overkill — instead assert cwd reaches subprocess via a spy on
    # subprocess.run through the public render path with a captured runner is
    # not possible (cwd only flows to our own runners), so exercise
    # _default_runner directly below.
    from doxtr_hugo.diagrams import _default_runner, INPUT_PLACEHOLDER  # noqa: F401
    out = tmp_path / "copied"
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "seed.txt").write_text("seed")
    # `cp seed.txt <out>` only succeeds if cwd is `sub` (relative input).
    _default_runner(["cp", "seed.txt", str(out)], "", True, str(sub))
    assert out.read_text() == "seed"


def test_detect_backend_diagrams_requires_library(monkeypatch):
    import doxtr_hugo.diagrams as dmod

    # Present when importable.
    monkeypatch.setattr(
        dmod.importlib.util, "find_spec", lambda name: object()
    )
    assert detect_backend("diagrams", which=ABSENT) is not None
    # Absent when the library cannot be imported.
    monkeypatch.setattr(dmod.importlib.util, "find_spec", lambda name: None)
    assert detect_backend("diagrams", which=PRESENT) is None


@pytest.mark.skipif(shutil.which("d2") is None, reason="d2 binary not installed")
def test_real_d2_render_svg(tmp_path):
    res = render_diagram(d2node(d2_script="a -> b -> c"), tmp_path)
    assert res is not None and res.fmt == "svg"
    data = (tmp_path / res.filename).read_bytes()
    assert b"<svg" in data[:4096]


@pytest.mark.skipif(
    importlib.util.find_spec("diagrams") is None,
    reason="python 'diagrams' library not installed",
)
@pytest.mark.skipif(
    shutil.which("dot") is None,
    reason="graphviz 'dot' binary not installed",
)
def test_real_diagrams_render_png(tmp_path):
    code = (
        "from diagrams import Diagram\n"
        "from diagrams.generic.blank import Blank\n"
        "import sys\n"
        "with Diagram(sys.argv[1], show=False, filename=sys.argv[1]):\n"
        "    Blank('x')\n"
    )
    res = render_diagram(diagrams_node(code), tmp_path, backend_key="diagrams")
    assert res is not None and res.fmt == "png"
    data = (tmp_path / res.filename).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# Per-node extra args (owned by the backend, not the render pass)
# ---------------------------------------------------------------------------


def test_d2_backend_node_extra_args_for_multiboard():
    """The d2 backend derives ``--target``/``--animate-interval`` from options."""
    backend = BACKENDS["d2"]
    node = d2node(
        d2_script="a -> b", options={"target": "x", "animate_interval": "500"}
    )
    assert backend.extra_args_for(node) == (
        "--target=x",
        "--animate-interval=500",
    )


def test_d2_backend_node_extra_args_empty_without_options():
    backend = BACKENDS["d2"]
    assert backend.extra_args_for(d2node(d2_script="a -> b")) == ()


def test_backend_without_hook_returns_empty_extra_args():
    """Backends with no ``node_extra_args`` hook yield () with no branching."""
    assert BACKENDS["plantuml"].extra_args_for(plantuml("A -> B")) == ()


# ---------------------------------------------------------------------------
# Public register_backend seam
# ---------------------------------------------------------------------------


def test_register_backend_installs_table_and_node_mapping(monkeypatch):
    import doxtr_hugo
    from doxtr_hugo.diagrams import DiagramBackend
    from doxtr_hugo.image_policy import BackendCapability

    # Snapshot and restore the module tables so the test is isolated.
    saved_backends = dict(diagrams.BACKENDS)
    saved_nodes = dict(diagrams.NODE_BACKEND)
    saved_runners = dict(diagrams._BACKEND_RUNNERS)
    try:
        calls = []

        def fake_runner(argv, source, stdin_source):
            calls.append((tuple(argv), source, stdin_source))
            return b"<svg>ok</svg>"

        def extra(node):
            return ("--flag",)

        backend = DiagramBackend(
            key="fakeviz",
            binary="fakeviz",
            capability=BackendCapability("fakeviz", can_svg=True, can_png=True),
            build_args=lambda b, fmt, out: [b, out],
        )
        returned = doxtr_hugo.register_backend(
            backend,
            ["fakeviznode", "FakeVizAlias"],
            runner=fake_runner,
            node_extra_args=extra,
        )

        # Installed in all three tables.
        assert diagrams.BACKENDS["fakeviz"].key == "fakeviz"
        assert diagrams.NODE_BACKEND["fakeviznode"] == "fakeviz"
        # Node names are lower-cased for case-insensitive detection.
        assert diagrams.NODE_BACKEND["fakevizalias"] == "fakeviz"
        assert diagrams._BACKEND_RUNNERS["fakeviz"] is fake_runner
        # node_extra_args applied via register_backend overrides the dataclass.
        assert returned.extra_args_for(object()) == ("--flag",)
    finally:
        diagrams.BACKENDS.clear()
        diagrams.BACKENDS.update(saved_backends)
        diagrams.NODE_BACKEND.clear()
        diagrams.NODE_BACKEND.update(saved_nodes)
        diagrams._BACKEND_RUNNERS.clear()
        diagrams._BACKEND_RUNNERS.update(saved_runners)
