"""Diagram render pass — the builder-side wiring of the §4 pipeline.

:mod:`doxtr_hugo.diagrams` is the pure render-to-file half of the diagram
pipeline (guide §4.1 steps 2–3); this module is the builder-side half that runs
*during* ``write_doc`` (steps 1, 4, 5):

1. **Detect** every diagram node in a page's doctree
   (:func:`doxtr_hugo.diagrams.detect_diagram_nodes`).
2. **Render** each to a content-hashed file co-located in the page's bundle
   directory, applying the SVG-preferred/PNG-fallback format policy (§4.2) and
   the ``doxtr_hugo_on_missing_backend`` graceful-degradation policy (§4.4) when
   a backend binary is unavailable.
3. **Rewrite** the node in place: a successfully rendered diagram becomes a
   docutils ``image`` node pointing at the emitted bare filename (which the
   :class:`~doxtr_hugo.translator.HugoTranslator` renders as a Markdown
   ``![alt](diagram-<hash>.svg)`` reference); a missing-backend passthrough
   becomes a :class:`RawMarkdown` node carrying a fenced code block; a
   missing-backend skip is removed.
4. **Record** every emitted asset + its chosen format in the build
   :class:`~doxtr_hugo.assets.Manifest` for the post-build verifier (§7.2).

Keeping this in its own module keeps the builder lean and the pipeline
unit-testable with a fake builder/doctree (no Sphinx app required).
"""

from __future__ import annotations

from pathlib import Path

from docutils import nodes
from sphinx.util import logging

from . import diagrams
from .assets import Manifest, copy_asset, sniff_format
from . import math_render

logger = logging.getLogger(__name__)


class RawMarkdown(nodes.General, nodes.Element):
    """A doctree node carrying literal Markdown to emit verbatim.

    Used for the missing-backend *passthrough* path (guide §4.4): the diagram
    source is wrapped in a fenced code block so the information survives even
    when the backend binary is unavailable. The
    :class:`~doxtr_hugo.translator.HugoTranslator` emits the node's ``markdown``
    text as-is.
    """


def _node_alt(node) -> str:
    """Best-effort alt text for a diagram node.

    Diagram directives rarely set ``alt``; fall back to the backend/caption so
    the emitted ``![alt](...)`` is still meaningful. Empty when nothing useful
    is available (a bare ``![](...)`` is valid CommonMark).
    """
    for key in ("alt", "caption"):
        try:
            value = node.get(key)  # type: ignore[attr-defined]
        except AttributeError:
            value = None
        if value:
            return str(value)
    return ""


def _node_cwd(node, builder) -> str | None:
    """Working directory for a diagram backend subprocess, or ``None``.

    Some backends resolve relative paths in the diagram source against the
    original document's directory (PlantUML ``!include``, a d2 ``.d2`` file
    argument). ``sphinxcontrib.plantuml`` records that directory on the node as
    ``incdir`` (source-dir-relative); ``doxtr_d2`` records a ``d2_file`` path.
    Resolving the subprocess cwd to that directory lets relative includes work.
    Returns ``None`` when the node carries no directory hint (inline source).
    """
    srcdir = Path(getattr(builder, "srcdir", "") or "")
    try:
        incdir = node.get("incdir")  # type: ignore[attr-defined]
    except AttributeError:
        incdir = None
    if incdir:
        resolved = (srcdir / incdir) if not Path(incdir).is_absolute() else Path(incdir)
        if resolved.is_dir():
            return str(resolved)
    try:
        d2_file = node.get("d2_file")  # type: ignore[attr-defined]
    except AttributeError:
        d2_file = None
    if d2_file:
        parent = Path(d2_file).parent
        if parent.is_dir():
            return str(parent)
    return None


def render_page_diagrams(
    builder,
    docname: str,
    doctree: nodes.document,
    bundle_dir: str | Path,
    manifest: Manifest,
) -> None:
    """Render and rewrite every diagram node in ``doctree`` in place.

    Parameters
    ----------
    builder:
        The :class:`~doxtr_hugo.builder.HugoBuilder`; its ``config`` supplies
        the image-format/raster-fallback/process-images/on-missing-backend
        knobs and its ``outdir`` anchors the manifest ``relpath``.
    docname:
        The document being written (recorded on each manifest asset).
    doctree:
        The resolved doctree; diagram nodes are replaced in place.
    bundle_dir:
        The page's bundle directory (where the Markdown file is written);
        rendered diagrams co-locate here so a bare-filename Markdown image
        reference resolves as a Hugo page resource.
    manifest:
        The build manifest; one record is appended per successfully rendered
        diagram.
    """
    config = builder.config
    outdir = Path(builder.outdir)
    bundle_dir = Path(bundle_dir)
    policy = getattr(config, "doxtr_hugo_on_missing_backend", "warn-passthrough")
    image_format = getattr(config, "doxtr_hugo_image_format", "auto")
    raster_fallback = getattr(config, "doxtr_hugo_raster_fallback", "png")
    process_images = bool(getattr(config, "doxtr_hugo_process_images", False))

    # Collect first, rewrite after: mutating the tree while iterating
    # ``findall`` is unsafe.
    found = list(diagrams.detect_diagram_nodes(doctree))
    for node, backend_key in found:
        alt = _node_alt(node)
        cwd = _node_cwd(node, builder)
        # Per-node CLI flags are owned by the backend (e.g. d2 multi-board
        # ``--target``/``--animate-interval``); unknown backends return ().
        backend = diagrams.BACKENDS.get(backend_key)
        extra_args = backend.extra_args_for(node) if backend is not None else ()
        try:
            result = diagrams.render_diagram(
                node,
                bundle_dir,
                backend_key=backend_key,
                image_format=image_format,
                raster_fallback=raster_fallback,
                process_images=process_images,
                cwd=cwd,
                extra_args=extra_args,
            )
        except diagrams.DiagramRenderError as exc:
            # The binary is present but the render failed: this is a real
            # authoring/environment error, not a missing backend. Warn and drop
            # the node (keep the build green) so one broken diagram does not
            # abort the whole site.
            logger.warning(
                "doxtr_hugo: diagram render failed (%s backend): %s",
                backend_key,
                exc,
                location=docname,
                type="doxtr_hugo",
                subtype="diagram_render_failed",
            )
            _replace_with(node, None)
            continue

        if result is None:
            # Backend binary unavailable → apply the graceful-degradation
            # policy (guide §4.4). ``degrade_missing_backend`` never raises for
            # the warn-* policies; ``"error"`` raises MissingBackendError which
            # propagates to fail the build, as configured.
            degrade = diagrams.degrade_missing_backend(
                node, backend_key=backend_key, policy=policy
            )
            logger.warning(
                "%s", degrade.warning,
                location=docname,
                type="doxtr_hugo",
                subtype="missing_backend",
            )
            if degrade.action == "passthrough":
                replacement = RawMarkdown()
                replacement["markdown"] = degrade.markdown
                _replace_with(node, replacement)
            else:  # "skip"
                _replace_with(node, None)
            continue

        # Success: record the asset and rewrite the node to a Markdown image.
        try:
            relpath = str(Path(result.path).resolve().relative_to(outdir.resolve()))
        except ValueError:
            # Rendered outside the output tree (shouldn't happen); fall back to
            # the bare filename so the manifest still has a usable record.
            relpath = result.filename
        manifest.add(
            docname=docname,
            filename=result.filename,
            relpath=relpath.replace("\\", "/"),
            fmt=result.fmt,
            kind="diagram",
            source=result.backend_key,
        )
        if result.decision.warning:
            logger.warning(
                "%s", result.decision.warning,
                location=docname,
                type="doxtr_hugo",
                subtype="format_fallback",
            )
        image = nodes.image(uri=result.filename, alt=alt)
        _replace_with(node, image)


def render_page_math(
    builder,
    docname: str,
    doctree: nodes.document,
    bundle_dir: str | Path,
    manifest: Manifest,
    *,
    renderer=None,
) -> None:
    """Render and rewrite every ``math``/``math_block`` node in place (guide §4.3).

    Hugo ships no math renderer, so math is handled by
    :mod:`doxtr_hugo.math_render` per the ``doxtr_hugo_math`` mode:

    - ``png`` (default): pre-render each formula to a content-hashed PNG in the
      page bundle (matplotlib ``mathtext``; no client JS, theme-portable),
      rewrite the node to a Markdown image, and record the asset in the
      manifest.
    - ``mathjax`` / ``mathml-passthrough``: rewrite to a :class:`RawMarkdown`
      node carrying the delimited LaTeX / MathML (no asset emitted; the
      operator supplies the client-side renderer).

    A render failure (e.g. a LaTeX construct matplotlib's mathtext cannot
    parse) degrades to a MathJax-delimited passthrough with a one-time warning,
    so one bad formula never aborts the build and the LaTeX is never lost.

    ``renderer`` is an injection seam for tests: when provided it is forwarded
    to :func:`math_render.math_markdown` as the PNG renderer so a fake/failing
    renderer can be exercised without a real matplotlib call. ``None`` (the
    default) lets ``math_markdown`` use its own default matplotlib renderer.
    """
    outdir = Path(builder.outdir)
    bundle_dir = Path(bundle_dir)
    mode = getattr(builder.config, "doxtr_hugo_math", math_render.MATH_PNG)

    # Only pass ``renderer`` through when the caller injected one, so the
    # production path keeps math_markdown's own default.
    render_kwargs = {} if renderer is None else {"renderer": renderer}

    found = [
        n
        for n in doctree.findall()
        if math_render.is_math_node(n)
    ]
    for node in found:
        block = math_render.is_block_math(node)
        try:
            markdown, result = math_render.math_markdown(
                node, bundle_dir, mode=mode, **render_kwargs
            )
        except math_render.MathRenderError as exc:
            # Degrade to a MathJax passthrough so the LaTeX survives.
            source = math_render.math_source(node)
            markdown = math_render.mathjax_markdown(source, block=block)
            result = None
            _warn_math_fallback(builder, docname, exc)

        if result is not None:
            # PNG mode: record the asset and emit a Markdown image node so the
            # translator's image handler renders it (block math on its own
            # paragraph).
            try:
                relpath = str(
                    Path(result.path).resolve().relative_to(outdir.resolve())
                )
            except ValueError:
                relpath = result.filename
            manifest.add(
                docname=docname,
                filename=result.filename,
                relpath=relpath.replace("\\", "/"),
                fmt="png",
                kind="math",
                source="math",
            )
            image = nodes.image(uri=result.filename, alt=math_render.math_source(node))
            _replace_with(node, image)
        else:
            replacement = RawMarkdown()
            replacement["markdown"] = markdown
            _replace_with(node, replacement)


def _warn_math_fallback(builder, docname: str, exc) -> None:
    """Warn once per build that a formula fell back to MathJax passthrough."""
    seen = getattr(builder, "_doxtr_math_fallback_warned", None)
    if seen is None:
        seen = [False]
        try:
            builder._doxtr_math_fallback_warned = seen
        except Exception:  # pragma: no cover - defensive
            pass
    if seen[0]:
        return
    seen[0] = True
    logger.warning(
        "doxtr_hugo: a math formula could not be pre-rendered to PNG (%s); "
        "falling back to MathJax-delimited passthrough for affected formulas. "
        "Set doxtr_hugo_math='mathjax' to silence this and render client-side.",
        exc,
        location=docname,
        type="doxtr_hugo",
        subtype="math_fallback",
    )


def _replace_with(node, replacement) -> None:
    """Replace ``node`` in its parent with ``replacement`` (or remove it).

    ``replacement`` of ``None`` removes the node entirely (the ``skip`` policy
    and render-failure paths). A docutils node replaces it in position.
    """
    parent = node.parent
    if parent is None:
        return
    index = parent.index(node)
    if replacement is None:
        del parent[index]
    else:
        parent[index] = replacement


#: Image URI schemes/shapes that are already final and must not be copied or
#: rewritten: remote URLs, protocol-relative URLs, data URIs, and fragments.
#: A bare filename (no slash, no scheme) is treated as already co-located (a
#: diagram the render pass emitted), so it is left untouched too.
def _is_external_or_inline_uri(uri: str) -> bool:
    return (
        "://" in uri
        or uri.startswith(("//", "data:", "#", "mailto:"))
    )


def copy_page_images(
    builder,
    docname: str,
    doctree: nodes.document,
    bundle_dir: str | Path,
    manifest: Manifest,
) -> None:
    """Co-locate authored ``image``/``figure`` targets into the page bundle.

    Authored images (and images other extensions leave on the node, e.g. a
    pre-rendered drawio SVG) carry a ``uri`` that is source-dir-relative or an
    absolute path into the Sphinx working tree — neither resolves under Hugo's
    ``content/``/``public/`` layout. For each such image this copies the file
    next to the page's Markdown with a content-hashed filename
    (:func:`doxtr_hugo.assets.copy_asset`), records it in the ``manifest``, and
    rewrites ``uri`` to the bare co-located filename so the
    :class:`~doxtr_hugo.translator.HugoTranslator` emits a working
    ``![alt](<file>)`` page-resource reference (guide §4.1 step 1 plain-image
    row, §5.1 images row).

    Images already rewritten by the diagram render pass (a bare filename),
    remote/inline URIs, and files that cannot be found on disk are left alone;
    an unresolvable target warns once per build so the gap is observable rather
    than a silent broken link.
    """
    outdir = Path(builder.outdir)
    srcdir = Path(getattr(builder, "srcdir", "") or "")
    bundle_dir = Path(bundle_dir)

    for image in list(doctree.findall(nodes.image)):
        uri = image.get("uri", "")
        if not uri or _is_external_or_inline_uri(uri):
            continue
        # A bare filename with no directory is an already-co-located asset
        # (emitted by the diagram render pass); leave it untouched.
        if "/" not in uri and "\\" not in uri and not Path(uri).is_absolute():
            continue
        src = _resolve_image_path(uri, srcdir, builder)
        if src is None:
            _warn_missing_image(builder, docname, uri)
            continue
        try:
            _dest, filename = copy_asset(src, bundle_dir)
        except OSError:
            _warn_missing_image(builder, docname, uri)
            continue
        try:
            relpath = str((bundle_dir / filename).resolve().relative_to(outdir.resolve()))
        except ValueError:
            relpath = filename
        fmt = sniff_format(Path(src).read_bytes()[:4096]) or ""
        manifest.add(
            docname=docname,
            filename=filename,
            relpath=relpath.replace("\\", "/"),
            fmt=fmt,
            kind="image",
            source=uri,
        )
        image["uri"] = filename


def _resolve_image_path(uri: str, srcdir: Path, builder) -> Path | None:
    """Resolve an image ``uri`` to an existing file on disk, or ``None``.

    Probes, in order: an absolute path as-is; the Sphinx source dir
    (``uri`` is typically source-dir-relative); and the Sphinx output image dir
    (``builder.outdir`` / ``_images``) that some extensions render into. Returns
    the first existing path, else ``None``.
    """
    candidate = Path(uri)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    probes = [srcdir / uri]
    outdir = Path(getattr(builder, "outdir", "") or "")
    probes.append(outdir / uri)
    probes.append(outdir / "_images" / Path(uri).name)
    for p in probes:
        if p.is_file():
            return p
    return None


def _warn_missing_image(builder, docname: str, uri: str) -> None:
    """Warn once per build that an authored image could not be co-located.

    Tracked on the builder so a repeated missing asset warns once, consistent
    with the translator's unmapped-node warn-once behavior.
    """
    seen = getattr(builder, "_doxtr_missing_images", None)
    if seen is None:
        seen = set()
        try:
            builder._doxtr_missing_images = seen
        except Exception:  # pragma: no cover - defensive, builder may be a stub
            pass
    if uri in seen:
        return
    seen.add(uri)
    logger.warning(
        "doxtr_hugo: could not locate image %r to co-locate into the page "
        "bundle; the Markdown reference will be left as-is and may be broken",
        uri,
        location=docname,
        type="doxtr_hugo",
        subtype="missing_image",
    )
