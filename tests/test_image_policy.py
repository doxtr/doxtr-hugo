"""M3 image-policy tests — SVG-preferred / PNG-fallback decisions (guide §4.2).

Pure-data tests: :mod:`doxtr_hugo.image_policy` takes a backend-capability
description plus config knobs and returns a :class:`FormatDecision`. No Sphinx
build or diagram backend binary is required, so these run everywhere.
"""

from __future__ import annotations

import pytest

from doxtr_hugo.image_policy import (
    BackendCapability,
    FormatDecision,
    choose_format,
)


# Common backends used across cases.
FULL = BackendCapability("plantuml", can_svg=True, can_png=True, can_webp=False)
WEBP = BackendCapability("fancy", can_svg=True, can_png=True, can_webp=True)
PNG_ONLY = BackendCapability("raster-only", can_svg=False, can_png=True, can_webp=False)
SVG_ONLY = BackendCapability("svg-only", can_svg=True, can_png=False, can_webp=False)


# --------------------------------------------------------------------------
# BackendCapability.can_emit
# --------------------------------------------------------------------------

def test_can_emit_reports_declared_capabilities():
    assert FULL.can_emit("svg") is True
    assert FULL.can_emit("png") is True
    assert FULL.can_emit("webp") is False
    assert FULL.can_emit("bogus") is False


# --------------------------------------------------------------------------
# Step 2 — prefer SVG when safe
# --------------------------------------------------------------------------

def test_auto_prefers_svg_when_backend_supports_and_no_processing():
    d = choose_format(FULL)
    assert d.fmt == "svg"
    assert d.reason == "prefer-svg"
    assert d.warning is None


def test_auto_falls_back_to_png_when_svg_unsupported():
    d = choose_format(PNG_ONLY)
    assert d.fmt == "png"
    assert d.reason == "raster:no-svg-backend"
    assert d.warning is None


# --------------------------------------------------------------------------
# Step 2/3 — processing forces raster
# --------------------------------------------------------------------------

def test_process_images_forces_raster_even_when_svg_available():
    d = choose_format(FULL, process_images=True)
    assert d.fmt == "png"
    assert d.reason == "raster:processing-requested"


def test_per_node_wants_processing_forces_raster():
    d = choose_format(FULL, wants_processing=True)
    assert d.fmt == "png"
    assert d.reason == "raster:processing-requested"


def test_raster_fallback_webp_honored_when_backend_can_webp():
    d = choose_format(WEBP, process_images=True, raster_fallback="webp")
    assert d.fmt == "webp"
    assert d.reason == "raster:processing-requested"
    assert d.warning is None


def test_raster_fallback_webp_degrades_to_png_when_unsupported():
    d = choose_format(FULL, process_images=True, raster_fallback="webp")
    assert d.fmt == "png"
    assert d.warning is not None
    assert "cannot emit webp" in d.warning


def test_unknown_raster_fallback_defaults_to_png():
    d = choose_format(FULL, process_images=True, raster_fallback="tiff")
    assert d.fmt == "png"
    assert d.reason == "raster:processing-requested"


# --------------------------------------------------------------------------
# Step 1 — forced formats
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["svg", "png"])
def test_forced_format_honored_when_supported(fmt):
    d = choose_format(FULL, image_format=fmt)
    assert d.fmt == fmt
    assert d.reason == f"forced:{fmt}"
    assert d.warning is None


def test_forced_svg_overrides_processing_request():
    # An explicit force wins over the preference logic (step 1 before step 2/3).
    d = choose_format(FULL, image_format="svg", process_images=True)
    assert d.fmt == "svg"
    assert d.reason == "forced:svg"


def test_forced_webp_honored_when_backend_can_webp():
    d = choose_format(WEBP, image_format="webp")
    assert d.fmt == "webp"
    assert d.reason == "forced:webp"


# --------------------------------------------------------------------------
# Step 4 — forced format unsupported → PNG + warning
# --------------------------------------------------------------------------

def test_forced_svg_unsupported_falls_back_to_png_with_warning():
    d = choose_format(PNG_ONLY, image_format="svg")
    assert d.fmt == "png"
    assert d.reason == "forced:svg:unsupported"
    assert d.warning is not None
    assert "backend raster-only cannot emit svg" in d.warning


def test_forced_webp_unsupported_falls_back_to_png_with_warning():
    d = choose_format(FULL, image_format="webp")
    assert d.fmt == "png"
    assert d.reason == "forced:webp:unsupported"
    assert d.warning is not None
    assert "cannot emit webp" in d.warning


# --------------------------------------------------------------------------
# Degenerate backend — cannot even emit PNG
# --------------------------------------------------------------------------

def test_backend_without_png_still_returns_png_with_warning():
    # Guaranteed target is PNG; if a backend even lacks PNG we still return the
    # contract's PNG reference but warn so the caller can degrade gracefully.
    nothing = BackendCapability("broken", can_svg=False, can_png=False)
    d = choose_format(nothing, process_images=True)
    assert d.fmt == "png"
    assert d.warning is not None
    assert "cannot emit png" in d.warning


def test_svg_only_backend_with_processing_request_warns_and_returns_png():
    d = choose_format(SVG_ONLY, process_images=True)
    assert d.fmt == "png"
    assert d.warning is not None
    assert "cannot emit png" in d.warning


# --------------------------------------------------------------------------
# Decision object shape
# --------------------------------------------------------------------------

def test_decision_is_frozen_and_comparable():
    a = FormatDecision(fmt="svg", reason="prefer-svg")
    b = FormatDecision(fmt="svg", reason="prefer-svg")
    assert a == b
    with pytest.raises(Exception):
        a.fmt = "png"  # frozen dataclass
