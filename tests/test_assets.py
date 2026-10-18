"""M3 asset tests — content-hashed co-location + build manifest (guide §4, §7).

These tests exercise :mod:`doxtr_hugo.assets` without any backend binaries:
they only read/copy bytes and serialize JSON. They cover content-hashed
filenames (de-dup + cache-bust), format sniffing by magic bytes (the verifier's
contract), the manifest model (idempotent recording, deterministic order,
round-trip load), and pass-through asset copying.
"""

from __future__ import annotations

import json

import pytest

from doxtr_hugo import assets
from doxtr_hugo.assets import (
    MANIFEST_NAME,
    AssetRecord,
    Manifest,
    content_hash,
    copy_asset,
    hashed_filename,
    iter_relpaths,
    sniff_format,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 16
GIF_BYTES = b"GIF89a" + b"\x00" * 16
WEBP_BYTES = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8
SVG_BYTES = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"></svg>'
SVG_NO_DECL = b"<svg width='10'></svg>"


# ---------------------------------------------------------------------------
# content_hash / hashed_filename
# ---------------------------------------------------------------------------


def test_content_hash_is_stable_and_length():
    h = content_hash(PNG_BYTES)
    assert h == content_hash(PNG_BYTES)
    assert len(h) == assets.HASH_LEN
    assert all(c in "0123456789abcdef" for c in h)


def test_hashed_filename_preserves_stem_and_ext():
    name = hashed_filename("Logo.PNG", PNG_BYTES)
    assert name.startswith("Logo-")
    assert name.endswith(".png")  # extension lower-cased


def test_hashed_filename_dedups_identical_bytes():
    assert hashed_filename("a.png", PNG_BYTES) == hashed_filename("a.png", PNG_BYTES)


def test_hashed_filename_cache_busts_on_changed_bytes():
    a = hashed_filename("a.png", PNG_BYTES)
    b = hashed_filename("a.png", PNG_BYTES + b"x")
    assert a != b


def test_hashed_filename_defaults_stem_when_empty():
    name = hashed_filename(".png", PNG_BYTES)
    assert name.startswith("asset-")


# ---------------------------------------------------------------------------
# sniff_format
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data,expected",
    [
        (PNG_BYTES, "png"),
        (JPEG_BYTES, "jpeg"),
        (GIF_BYTES, "gif"),
        (WEBP_BYTES, "webp"),
        (SVG_BYTES, "svg"),
        (SVG_NO_DECL, "svg"),
        (b"not an image at all", None),
    ],
)
def test_sniff_format(data, expected):
    assert sniff_format(data) == expected


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_add_and_records():
    m = Manifest()
    m.add(
        docname="guide/install",
        filename="diagram-abc.svg",
        relpath="content/guide/install/diagram-abc.svg",
        fmt="svg",
        kind="diagram",
        source="plantuml",
    )
    assert len(m) == 1
    rec = m.records()[0]
    assert isinstance(rec, AssetRecord)
    assert rec.fmt == "svg" and rec.kind == "diagram"


def test_manifest_relpath_normalized_to_forward_slashes():
    m = Manifest()
    rec = m.add(
        docname="d",
        filename="x.png",
        relpath="content\\d\\x.png",
        fmt="png",
        kind="image",
    )
    assert rec.relpath == "content/d/x.png"


def test_manifest_idempotent_on_relpath():
    m = Manifest()
    for _ in range(3):
        m.add(
            docname="d",
            filename="diagram-1.png",
            relpath="content/d/diagram-1.png",
            fmt="png",
            kind="diagram",
        )
    assert len(m) == 1


def test_manifest_records_are_sorted_by_relpath():
    m = Manifest()
    m.add(docname="d", filename="z.png", relpath="content/z.png", fmt="png", kind="image")
    m.add(docname="d", filename="a.png", relpath="content/a.png", fmt="png", kind="image")
    rels = [r.relpath for r in m.records()]
    assert rels == sorted(rels)


def test_manifest_write_and_load_roundtrip(tmp_path):
    m = Manifest()
    m.add(
        docname="guide/install",
        filename="diagram-abc.svg",
        relpath="content/guide/install/diagram-abc.svg",
        fmt="svg",
        kind="diagram",
        source="plantuml",
    )
    path = m.write(tmp_path)
    assert path.name == MANIFEST_NAME
    assert path.exists()

    reloaded = Manifest.load(tmp_path)
    assert len(reloaded) == 1
    rec = reloaded.records()[0]
    assert rec.fmt == "svg"
    assert rec.source == "plantuml"
    assert rec.docname == "guide/install"


def test_manifest_write_is_idempotent_bytes(tmp_path):
    m = Manifest()
    m.add(docname="d", filename="x.png", relpath="content/x.png", fmt="png", kind="image")
    first = m.write(tmp_path).read_bytes()
    second = m.write(tmp_path).read_bytes()
    assert first == second


def test_manifest_json_is_versioned_and_parseable(tmp_path):
    m = Manifest()
    m.add(docname="d", filename="x.png", relpath="content/x.png", fmt="png", kind="image")
    data = json.loads(m.to_json())
    assert data["version"] == 1
    assert data["assets"][0]["fmt"] == "png"


def test_manifest_dotfile_name_is_ignored_by_hugo():
    # Manifest must be dot-prefixed so Hugo doesn't treat it as content and the
    # §7 format-only assertion doesn't trip on it.
    assert MANIFEST_NAME.startswith(".")
    assert MANIFEST_NAME.endswith(".json")


# ---------------------------------------------------------------------------
# copy_asset
# ---------------------------------------------------------------------------


def test_copy_asset_content_hashed(tmp_path):
    src = tmp_path / "logo.png"
    src.write_bytes(PNG_BYTES)
    bundle = tmp_path / "bundle"
    dest, filename = copy_asset(src, bundle)
    assert dest.exists()
    assert dest.read_bytes() == PNG_BYTES
    assert filename.startswith("logo-") and filename.endswith(".png")
    assert dest.name == filename


def test_copy_asset_idempotent(tmp_path):
    src = tmp_path / "logo.png"
    src.write_bytes(PNG_BYTES)
    bundle = tmp_path / "bundle"
    d1, f1 = copy_asset(src, bundle)
    mtime1 = d1.stat().st_mtime_ns
    d2, f2 = copy_asset(src, bundle)
    assert (d1, f1) == (d2, f2)
    # Second copy skipped (same content already present).
    assert d2.stat().st_mtime_ns == mtime1


def test_copy_asset_dedups_identical_bytes_different_names(tmp_path):
    (tmp_path / "a.png").write_bytes(PNG_BYTES)
    (tmp_path / "b.png").write_bytes(PNG_BYTES)
    bundle = tmp_path / "bundle"
    _, fa = copy_asset(tmp_path / "a.png", bundle)
    _, fb = copy_asset(tmp_path / "b.png", bundle)
    # Same hash because identical bytes, but different stems keep names distinct.
    assert fa.split("-", 1)[1] == fb.split("-", 1)[1]
    assert fa.startswith("a-") and fb.startswith("b-")


def test_copy_asset_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        copy_asset(tmp_path / "nope.png", tmp_path / "bundle")


# ---------------------------------------------------------------------------
# iter_relpaths
# ---------------------------------------------------------------------------


def test_iter_relpaths(tmp_path):
    outdir = tmp_path / "out"
    (outdir / "content" / "g").mkdir(parents=True)
    f = outdir / "content" / "g" / "x.png"
    f.write_bytes(PNG_BYTES)
    rels = iter_relpaths(outdir, [f])
    assert rels == ["content/g/x.png"]
