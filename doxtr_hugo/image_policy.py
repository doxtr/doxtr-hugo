"""SVG-preferred / PNG-fallback format decision logic (guide §4.2).

This module answers a single question for every diagram the builder renders:
**which image format should the file be written in — SVG, PNG, or WebP?**

It is intentionally a pure-data module: it takes plain values (a backend
capability description, the relevant config knobs, and per-node hints) and
returns a small :class:`FormatDecision`. It performs no rendering, touches no
filesystem, and imports nothing from Sphinx, so it is trivial to unit-test and
cheap to reuse from :mod:`doxtr_hugo.diagrams`.

Rationale (verified against Hugo, guide §4.2)
---------------------------------------------
SVG renders in Hugo out of the box as a plain ``<img>``, so it is the preferred
default for crisp, scalable diagrams. But Hugo's image *pipeline* reports
``IsImageResourceProcessable = false`` for SVG, so any diagram that needs
Hugo-side processing (resize / thumbnail / ``Fill``) must be a **raster**
format. PNG is the guaranteed-everywhere raster format (WebP output may require
a Hugo *extended* build), so PNG is the safe default fallback.

Decision order (:func:`choose_format`)
--------------------------------------
1. If ``doxtr_hugo_image_format`` forces a concrete format (``"svg"`` /
   ``"png"`` / ``"webp"``), honor it — subject to backend capability.
2. Else prefer **SVG** when the backend can emit SVG *and* the diagram does not
   request Hugo image processing.
3. Else fall back to ``doxtr_hugo_raster_fallback`` (default ``"png"``).
4. If the chosen backend cannot emit the chosen format, rasterize to PNG and
   flag a warning on the decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The format used when nothing else is possible. PNG is processable by Hugo's
#: image pipeline and renders universally, so it is the guaranteed-everywhere
#: default (guide §4.2).
GUARANTEED_FORMAT = "png"

#: The preferred-when-safe format: scalable and small, but not processable by
#: Hugo's image pipeline (guide §4.2).
PREFERRED_FORMAT = "svg"

#: Formats a user may force via ``doxtr_hugo_image_format``.
FORCEABLE_FORMATS = ("svg", "png", "webp")

#: Raster formats allowed as a fallback via ``doxtr_hugo_raster_fallback``.
RASTER_FORMATS = ("png", "webp")


@dataclass(frozen=True)
class BackendCapability:
    """What a diagram backend can emit.

    Attributes
    ----------
    name:
        Human-readable backend name used in warnings (e.g. ``"plantuml"``).
    can_svg:
        Whether the backend can emit SVG.
    can_png:
        Whether the backend can emit PNG. Treated as always-true in practice
        (PNG is the rasterization target), but modeled explicitly so a backend
        that genuinely cannot raster is representable.
    can_webp:
        Whether the backend can emit WebP directly.
    """

    name: str
    can_svg: bool = True
    can_png: bool = True
    can_webp: bool = False

    def can_emit(self, fmt: str) -> bool:
        """Return whether this backend can emit ``fmt`` directly."""
        return {
            "svg": self.can_svg,
            "png": self.can_png,
            "webp": self.can_webp,
        }.get(fmt, False)


@dataclass(frozen=True)
class FormatDecision:
    """The outcome of :func:`choose_format`.

    Attributes
    ----------
    fmt:
        The chosen output format (``"svg"`` / ``"png"`` / ``"webp"``).
    reason:
        A short, stable, machine-and-human readable reason code explaining the
        decision (useful for the build manifest and for tests).
    warning:
        A Sphinx-style warning message when a requested format could not be
        honored (e.g. backend cannot emit SVG), else ``None``.
    """

    fmt: str
    reason: str
    warning: str | None = None
    #: Reserved for future use; keeps the dataclass extensible without breaking
    #: callers that construct it positionally in tests.
    extras: dict = field(default_factory=dict, compare=False)


def _rasterize_to_png(backend: BackendCapability, wanted: str, why: str) -> FormatDecision:
    """Fall back to PNG when ``wanted`` cannot be emitted (guide §4.2 step 4)."""
    warning = (
        f"doxtr_hugo: backend {backend.name} cannot emit {wanted}; falling back to png"
    )
    return FormatDecision(fmt=GUARANTEED_FORMAT, reason=why, warning=warning)


def choose_format(
    backend: BackendCapability,
    *,
    image_format: str = "auto",
    raster_fallback: str = "png",
    process_images: bool = False,
    wants_processing: bool = False,
) -> FormatDecision:
    """Decide the output format for one diagram (guide §4.2).

    Parameters
    ----------
    backend:
        What the rendering backend can emit (:class:`BackendCapability`).
    image_format:
        ``doxtr_hugo_image_format``: ``"auto"`` (default) applies the preference
        logic; ``"svg"`` / ``"png"`` / ``"webp"`` force a concrete format
        subject to backend capability.
    raster_fallback:
        ``doxtr_hugo_raster_fallback``: raster format to use when SVG is not
        chosen (default ``"png"``; ``"webp"`` allowed).
    process_images:
        ``doxtr_hugo_process_images``: when ``True``, force a processable
        (raster) format site-wide because the theme uses Hugo's image pipeline.
    wants_processing:
        Per-node hint that *this* diagram requests Hugo image processing (e.g. a
        ``:scale:`` beyond intrinsic size). Forces a raster format.

    Returns
    -------
    FormatDecision
        The chosen format plus a reason code and optional warning.
    """
    needs_raster = bool(process_images or wants_processing)

    # Step 1 — forced format wins (subject to backend capability, step 4).
    if image_format in FORCEABLE_FORMATS:
        wanted = image_format
        if backend.can_emit(wanted):
            return FormatDecision(fmt=wanted, reason=f"forced:{wanted}")
        return _rasterize_to_png(backend, wanted, why=f"forced:{wanted}:unsupported")

    # From here on image_format is "auto" (or anything unrecognized → auto).

    # Step 2 — prefer SVG when safe: backend can emit it and no processing needed.
    if not needs_raster and backend.can_emit(PREFERRED_FORMAT):
        return FormatDecision(fmt=PREFERRED_FORMAT, reason="prefer-svg")

    # Step 3 — fall back to the configured raster format.
    fallback = raster_fallback if raster_fallback in RASTER_FORMATS else GUARANTEED_FORMAT
    why = "raster:processing-requested" if needs_raster else "raster:no-svg-backend"
    if backend.can_emit(fallback):
        return FormatDecision(fmt=fallback, reason=why)

    # Step 4 — chosen raster unsupported by backend → PNG (guaranteed).
    if fallback != GUARANTEED_FORMAT:
        return _rasterize_to_png(backend, fallback, why=why)

    # Backend claims it cannot even emit PNG: still return PNG as the contract's
    # guaranteed target, but surface a warning so the caller can degrade.
    return FormatDecision(
        fmt=GUARANTEED_FORMAT,
        reason=why,
        warning=(
            f"doxtr_hugo: backend {backend.name} reports it cannot emit png; "
            "emitting png reference anyway"
        ),
    )
