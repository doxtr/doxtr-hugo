"""Asset collection, content-hashed co-location, and build manifest (guide §4, §7).

This module is the bookkeeping half of the diagram/image pipeline described in
guide §4.1. :mod:`doxtr_hugo.diagrams` renders a diagram *source* to a file;
this module handles two complementary concerns:

1. **Co-locating pass-through assets** — plain ``image``/``figure`` targets the
   author already has on disk (PNG/SVG/JPEG/…) are copied next to the page that
   references them with a **content-hashed filename** (``<stem>-<hash>.<ext>``).
   Hashing means (a) identical bytes de-duplicate to one file and (b) changing
   the source bytes yields a new filename, so stale caches and Hugo's fingerprint
   pipeline never serve the wrong image. This mirrors the content-addressed
   naming :func:`doxtr_hugo.diagrams.asset_filename` uses for rendered diagrams.

2. **Recording the build manifest** — every emitted asset (rendered diagram or
   copied image) plus its chosen format is appended to a :class:`Manifest`.
   :meth:`Manifest.write` serializes it to ``<outdir>/.doxtr-hugo-manifest.json``
   so the post-build verifier (guide §7.2 step 2) can assert, per asset, that the
   file on disk exists, has the expected extension, and carries the expected
   magic bytes.

The module is deliberately free of Sphinx imports and never shells out to a
backend: it only reads/copies bytes and serializes JSON, so it is cheap to
unit-test without any binaries.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

#: Filename of the build manifest written into the builder output directory.
#: Dot-prefixed so Hugo ignores it as a content file (and the §7 format-only
#: assertion does not trip over it).
MANIFEST_NAME = ".doxtr-hugo-manifest.json"

#: Length of the hex digest used in content-hashed asset filenames. This is the
#: single source of truth: :mod:`doxtr_hugo.diagrams` and
#: :mod:`doxtr_hugo.math_render` import it so rendered and copied assets share one
#: consistent, short, collision-safe naming scheme. 16 hex chars (64 bits) is
#: ample to avoid collisions within a single site while staying readable.
HASH_LEN = 16

#: Magic-byte signatures the verifier (guide §7.2) checks per format. SVG is
#: text/XML so it has no single fixed prefix; the verifier matches ``<svg`` or an
#: XML declaration anywhere near the top instead, so it is intentionally absent
#: here and handled as a special case by :func:`sniff_format`.
MAGIC_BYTES = {
    "png": b"\x89PNG\r\n\x1a\n",
    "webp": b"RIFF",  # followed by size + "WEBP"; prefix check is sufficient.
    "jpeg": b"\xff\xd8\xff",
    "gif": b"GIF8",
}


def content_hash(data: bytes) -> str:
    """Return the short hex digest used in content-hashed filenames."""
    return hashlib.sha256(data).hexdigest()[:HASH_LEN]


def hashed_filename(original: str, data: bytes) -> str:
    """Return ``<stem>-<hash><ext>`` for ``original`` given its ``data`` bytes.

    The extension is preserved (lower-cased) so downstream format checks and
    Hugo's media-type detection keep working. The stem is slug-safe already for
    typical image names; it is left intact so the human-readable origin stays
    visible in the bundle (e.g. ``logo-1a2b3c4d5e6f7a8b.png``).
    """
    p = Path(original)
    # ``Path(".png")`` has an empty suffix and a ".png" stem; treat a leading-dot
    # only name as an extension with no usable stem.
    if not p.suffix and p.name.startswith("."):
        stem, ext = "", p.name
    else:
        stem, ext = p.stem, p.suffix
    stem = stem or "asset"
    ext = ext.lower()
    digest = content_hash(data)
    return f"{stem}-{digest}{ext}"


def sniff_format(data: bytes) -> str | None:
    """Best-effort format detection from leading bytes (guide §7.2).

    Returns a lower-case format token (``"png"``/``"svg"``/``"webp"``/``"jpeg"``/
    ``"gif"``) or ``None`` when unrecognized. Used by the verifier to assert the
    emitted file's magic bytes match the format the manifest claims.
    """
    if data.startswith(MAGIC_BYTES["png"]):
        return "png"
    if data.startswith(MAGIC_BYTES["jpeg"]):
        return "jpeg"
    if data.startswith(MAGIC_BYTES["gif"]):
        return "gif"
    # WebP: "RIFF" .... "WEBP"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    # SVG is XML/text: look for an <svg tag or XML declaration near the top.
    head = data[:512].lstrip()
    if head.startswith(b"<?xml") or head.startswith(b"<svg") or b"<svg" in data[:512]:
        return "svg"
    return None


@dataclass(frozen=True)
class AssetRecord:
    """One emitted asset in the build manifest.

    Attributes
    ----------
    docname:
        The Sphinx document that references the asset (for diagnostics).
    filename:
        The emitted bare filename (co-located in the page bundle).
    relpath:
        Path of the emitted file relative to the builder output directory,
        using forward slashes (what the verifier stats under ``public/``).
    fmt:
        The format the asset was emitted in (``"svg"``/``"png"``/…). This is the
        value the verifier asserts against the file's extension + magic bytes.
    kind:
        ``"diagram"`` for a rendered diagram, ``"image"`` for a copied
        pass-through asset.
    source:
        Origin description — the original image path for copied assets, or the
        backend key for rendered diagrams.
    """

    docname: str
    filename: str
    relpath: str
    fmt: str
    kind: str
    source: str = ""
    extras: dict = field(default_factory=dict, compare=False)


class Manifest:
    """An append-only record of every emitted asset for post-build verification.

    The builder creates one :class:`Manifest`, records each asset as it is
    emitted (during ``write_doc``), and calls :meth:`write` in ``finish()`` so
    the harness verifier (guide §7.2) can read it back. Recording is idempotent
    on ``relpath``: re-adding the same emitted file (e.g. a de-duplicated
    content-hashed diagram shared by two pages) updates rather than duplicates.
    """

    def __init__(self) -> None:
        """Create an empty manifest.

        Records are kept in a dict keyed by ``relpath`` so repeated emits of the
        same content-hashed file collapse to a single record (idempotent
        rebuilds / de-duplication of shared assets).
        """
        self._records: dict[str, AssetRecord] = {}

    def record(self, record: AssetRecord) -> None:
        """Add or replace one :class:`AssetRecord` (keyed on ``relpath``)."""
        self._records[record.relpath] = record

    def add(
        self,
        *,
        docname: str,
        filename: str,
        relpath: str,
        fmt: str,
        kind: str,
        source: str = "",
    ) -> AssetRecord:
        """Convenience constructor that records and returns an :class:`AssetRecord`."""
        rec = AssetRecord(
            docname=docname,
            filename=filename,
            relpath=relpath.replace("\\", "/"),
            fmt=fmt,
            kind=kind,
            source=source,
        )
        self.record(rec)
        return rec

    def records(self) -> list[AssetRecord]:
        """Return the records in a deterministic (relpath-sorted) order."""
        return [self._records[k] for k in sorted(self._records)]

    def __len__(self) -> int:
        """Return the number of distinct recorded assets (unique ``relpath``s)."""
        return len(self._records)

    def to_dict(self) -> dict:
        """Return a JSON-serializable mapping of the manifest."""
        return {
            "version": 1,
            "assets": [
                {k: v for k, v in asdict(r).items() if k != "extras" or v}
                for r in self.records()
            ],
        }

    def to_json(self) -> str:
        """Serialize the manifest to a stable, pretty-printed JSON string."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    def write(self, outdir: str | Path) -> Path:
        """Write the manifest into ``outdir`` and return the written path.

        Idempotent: writing the same manifest twice yields identical bytes.
        """
        path = Path(outdir) / MANIFEST_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, outdir: str | Path) -> "Manifest":
        """Read a manifest back from ``outdir`` (used by the verifier)."""
        path = Path(outdir) / MANIFEST_NAME
        data = json.loads(path.read_text(encoding="utf-8"))
        manifest = cls()
        for item in data.get("assets", []):
            manifest.record(
                AssetRecord(
                    docname=item.get("docname", ""),
                    filename=item.get("filename", ""),
                    relpath=item.get("relpath", ""),
                    fmt=item.get("fmt", ""),
                    kind=item.get("kind", ""),
                    source=item.get("source", ""),
                )
            )
        return manifest


def copy_asset(src: str | Path, bundle_dir: str | Path) -> tuple[Path, str]:
    """Copy ``src`` into ``bundle_dir`` under a content-hashed filename.

    Returns ``(dest_path, filename)``. Idempotent: because the filename is a
    hash of the bytes, re-copying identical content writes the same path (and is
    skipped if it already exists with non-zero size). Raises ``FileNotFoundError``
    if ``src`` does not exist.
    """
    src = Path(src)
    data = src.read_bytes()
    filename = hashed_filename(src.name, data)
    bundle_dir = Path(bundle_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    dest = bundle_dir / filename
    if not (dest.exists() and dest.stat().st_size == len(data)):
        shutil.copyfile(src, dest)
    return dest, filename


def iter_relpaths(outdir: str | Path, paths: Iterable[str | Path]) -> list[str]:
    """Return each path in ``paths`` relative to ``outdir`` (forward slashes).

    Small helper so callers can turn absolute emitted-file paths into the
    ``relpath`` the manifest stores.
    """
    outdir = Path(outdir)
    rels: list[str] = []
    for p in paths:
        rels.append(str(Path(p).resolve().relative_to(outdir.resolve())).replace("\\", "/"))
    return rels
