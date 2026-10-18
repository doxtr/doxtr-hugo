"""Unit tests for the harness format verifier (guide §7.2 steps 2–3).

These are binary-free: they synthesize an emitted output directory (a build
manifest plus on-disk asset files) and exercise
:func:`test_harness.test_runner.verify_formats` directly — both the pass path
(extension + magic bytes match the declared format) and the mismatch-detection
paths, including the **forced-PNG negative test**: an asset forced to PNG that
is actually PNG passes, while a format/extension/magic-byte mismatch fails.

No ``hugo``/``plantuml``/``dot`` binary is required.
"""

from __future__ import annotations

import pytest

from doxtr_hugo.assets import MANIFEST_NAME, Manifest
from test_harness.test_runner import StageError, verify_formats

# Minimal, valid magic-byte payloads for each format the verifier recognizes.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SVG_BYTES = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"></svg>\n'
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 16
GIF_BYTES = b"GIF89a" + b"\x00" * 16
WEBP_BYTES = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8


def _write_asset(outdir, relpath: str, data: bytes) -> None:
    target = outdir / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _manifest_with(outdir, records) -> None:
    """Write a manifest describing ``records`` (list of add() kwargs)."""
    manifest = Manifest()
    for kw in records:
        manifest.add(**kw)
    manifest.write(outdir)


def test_no_manifest_is_reported_not_failed(tmp_path):
    """A diagram-free build (no manifest) does not fail the verifier."""
    detail = verify_formats(str(tmp_path))
    assert "no asset manifest" in detail


def test_empty_manifest_is_reported(tmp_path):
    """A present-but-empty manifest is reported, not failed."""
    Manifest().write(tmp_path)
    detail = verify_formats(str(tmp_path))
    assert "empty" in detail


def test_svg_and_png_assets_pass(tmp_path):
    """Matching extension + magic bytes for SVG and PNG assets verify clean."""
    _write_asset(tmp_path, "guide/diagram-abc.svg", SVG_BYTES)
    _write_asset(tmp_path, "guide/diagram-def.png", PNG_BYTES)
    _manifest_with(
        tmp_path,
        [
            dict(
                docname="guide/arch",
                filename="diagram-abc.svg",
                relpath="guide/diagram-abc.svg",
                fmt="svg",
                kind="diagram",
                source="plantuml",
            ),
            dict(
                docname="guide/arch",
                filename="diagram-def.png",
                relpath="guide/diagram-def.png",
                fmt="png",
                kind="diagram",
                source="graphviz",
            ),
        ],
    )
    detail = verify_formats(str(tmp_path))
    assert "verified 2 asset(s)" in detail


def test_all_recognized_formats_pass(tmp_path):
    """Every format token the verifier knows round-trips (ext + magic bytes)."""
    cases = [
        ("a.svg", SVG_BYTES, "svg"),
        ("b.png", PNG_BYTES, "png"),
        ("c.jpg", JPEG_BYTES, "jpeg"),
        ("d.jpeg", JPEG_BYTES, "jpeg"),
        ("e.gif", GIF_BYTES, "gif"),
        ("f.webp", WEBP_BYTES, "webp"),
    ]
    records = []
    for name, data, fmt in cases:
        _write_asset(tmp_path, name, data)
        records.append(
            dict(
                docname="d",
                filename=name,
                relpath=name,
                fmt=fmt,
                kind="image",
            )
        )
    _manifest_with(tmp_path, records)
    detail = verify_formats(str(tmp_path))
    assert "verified 6 asset(s)" in detail


def test_forced_png_asset_passes(tmp_path):
    """Forced-PNG path: a diagram forced to PNG that *is* PNG verifies clean.

    This is the positive half of the forced-fallback negative test (guide §7.2
    step 3): a diagram emitted as ``.png`` with PNG magic bytes passes.
    """
    _write_asset(tmp_path, "guide/forced-123.png", PNG_BYTES)
    _manifest_with(
        tmp_path,
        [
            dict(
                docname="guide/forced",
                filename="forced-123.png",
                relpath="guide/forced-123.png",
                fmt="png",
                kind="diagram",
                source="plantuml",
            )
        ],
    )
    detail = verify_formats(str(tmp_path))
    assert "verified 1 asset(s)" in detail


def test_forced_png_with_svg_bytes_fails(tmp_path):
    """Negative: manifest claims PNG but the file holds SVG bytes → StageError.

    This proves the verifier catches a forced-PNG fallback that silently emitted
    the wrong payload (the exact regression guide §7.2 step 3 guards against).
    """
    # Extension matches the declared PNG format, but the bytes are SVG.
    _write_asset(tmp_path, "guide/forced-123.png", SVG_BYTES)
    _manifest_with(
        tmp_path,
        [
            dict(
                docname="guide/forced",
                filename="forced-123.png",
                relpath="guide/forced-123.png",
                fmt="png",
                kind="diagram",
            )
        ],
    )
    with pytest.raises(StageError, match="magic-byte/format mismatch"):
        verify_formats(str(tmp_path))


def test_extension_format_mismatch_fails(tmp_path):
    """Negative: PNG bytes under an ``.svg`` extension for a PNG record fails."""
    _write_asset(tmp_path, "img.svg", PNG_BYTES)
    _manifest_with(
        tmp_path,
        [dict(docname="d", filename="img.svg", relpath="img.svg", fmt="png", kind="image")],
    )
    with pytest.raises(StageError, match="extension/format mismatch"):
        verify_formats(str(tmp_path))


def test_missing_file_fails(tmp_path):
    """Negative: a manifested asset with no file on disk fails."""
    _manifest_with(
        tmp_path,
        [dict(docname="d", filename="gone.png", relpath="gone.png", fmt="png", kind="image")],
    )
    with pytest.raises(StageError, match="missing on disk"):
        verify_formats(str(tmp_path))


def test_empty_file_fails(tmp_path):
    """Negative: a zero-byte manifested asset fails."""
    _write_asset(tmp_path, "empty.png", b"")
    _manifest_with(
        tmp_path,
        [dict(docname="d", filename="empty.png", relpath="empty.png", fmt="png", kind="image")],
    )
    with pytest.raises(StageError, match="empty"):
        verify_formats(str(tmp_path))


def test_unknown_declared_format_fails(tmp_path):
    """Negative: a manifest declaring an unsupported format token fails."""
    _write_asset(tmp_path, "weird.tiff", b"II*\x00")
    _manifest_with(
        tmp_path,
        [dict(docname="d", filename="weird.tiff", relpath="weird.tiff", fmt="tiff", kind="image")],
    )
    with pytest.raises(StageError, match="unknown format"):
        verify_formats(str(tmp_path))


def test_manifest_name_constant_used(tmp_path):
    """The verifier reads the canonical manifest filename."""
    assert MANIFEST_NAME.endswith(".json")
    # Writing under the canonical name is what verify_formats reads back.
    _write_asset(tmp_path, "x.png", PNG_BYTES)
    _manifest_with(
        tmp_path,
        [dict(docname="d", filename="x.png", relpath="x.png", fmt="png", kind="image")],
    )
    assert (tmp_path / MANIFEST_NAME).is_file()
    assert "verified 1 asset(s)" in verify_formats(str(tmp_path))
