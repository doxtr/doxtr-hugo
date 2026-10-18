"""HugoBuilder — the Sphinx builder registered as ``-b hugo``.

The builder runs end to end: it translates each resolved doctree to Markdown
(:class:`~doxtr_hugo.translator.HugoTranslator`), pre-renders diagrams and math
to static assets (:mod:`doxtr_hugo.render_pass`), writes one content file per
document under the Hugo ``content/`` layout, and emits the site skeleton plus
asset manifest in :meth:`~HugoBuilder.finish`. It subclasses
``sphinx.builders.Builder`` directly (not the text/HTML builders) so it has full
control of the output layout and asset handling.
"""

from __future__ import annotations

from os import path

from docutils import nodes
from sphinx import addnodes
from sphinx.builders import Builder
from sphinx.util import logging
from sphinx.util.osutil import ensuredir, os_path

from .frontmatter import FrontMatter
from .site_skeleton import emit_skeleton
from .slug import content_path, url_for_docname
from .toctree import TocModel
from .translator import HugoTranslator
from .assets import Manifest
from .render_pass import render_page_diagrams, copy_page_images, render_page_math
from . import theme_support

logger = logging.getLogger(__name__)


class HugoBuilder(Builder):
    """Emit Hugo-ready Markdown content from a resolved Sphinx doctree.

    The builder initializes, reports all documents as outdated, maps docnames to
    Hugo-relative URIs, and in :meth:`write_doc` pre-renders diagrams/math and
    translates each doctree to a Markdown content file. :meth:`finish` emits the
    site skeleton and serializes the asset manifest.
    """

    name = "hugo"
    format = "markdown"
    epilog = "The Hugo site is in %(outdir)s."
    out_suffix = ".md"
    default_translator_class = HugoTranslator

    #: Image formats Hugo renders natively; SVG is preferred, PNG is the
    #: guaranteed-everywhere raster fallback (see guide §4).
    supported_image_types = [
        "image/svg+xml",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    ]

    def init(self) -> None:
        """Resolve config and prepare the output directory."""
        self.front_matter = self.config.doxtr_hugo_front_matter
        self.bundle_mode = self.config.doxtr_hugo_bundle_mode
        self.emit_skeleton_mode = self.config.doxtr_hugo_emit_skeleton
        # Theme-portability default: a ``content-only`` emit is meant to be
        # dropped into a consumer's own Hugo site/theme, which does not ship
        # doxtr-hugo's ``admonition`` shortcode (only the ``full`` skeleton
        # does). Shortcode admonitions would then fail to build
        # ("template for shortcode 'admonition' not found"). So when the emit
        # mode is not ``full`` and the admonition style is still the registered
        # default (``"shortcode"``), switch to portable raw HTML so the emitted
        # ``content/**`` builds cleanly under any theme. An explicit
        # ``doxtr_hugo_admonitions = "html"`` is already portable and left
        # untouched; a consumer who ships their own ``admonition`` shortcode can
        # re-assert ``"shortcode"`` — but since that is indistinguishable from
        # the default here, the switch is logged so the behavior is visible.
        if (
            self.emit_skeleton_mode != "full"
            and self.config.doxtr_hugo_admonitions == "shortcode"
        ):
            self.config.doxtr_hugo_admonitions = "html"
            logger.info(
                "doxtr_hugo: emit_skeleton=%r drops in to a consumer theme that "
                "does not ship the 'admonition' shortcode; defaulting "
                "doxtr_hugo_admonitions to 'html' (portable raw HTML). Set "
                "doxtr_hugo_admonitions explicitly to override.",
                self.emit_skeleton_mode,
            )
        #: Built in :meth:`prepare_writing`; consumed by :meth:`finish`.
        self.toc_model: TocModel | None = None
        #: Build manifest of every emitted asset (rendered diagrams and copied
        #: images); populated during :meth:`write_doc` and serialized in
        #: :meth:`finish` for the post-build verifier (guide §7.2).
        self.manifest = Manifest()
        ensuredir(self.outdir)

    def get_outdated_docs(self):
        """Return docnames whose emitted output is older than the source.

        Rebuilds every found document: the Hugo output is a whole-site artifact
        (skeleton, section indexes, and cross-reference arithmetic all depend on
        the full toctree model), so a partial rebuild is not currently
        supported. Incremental mtime comparison is a possible later refinement.
        """
        return self.env.found_docs

    def get_target_uri(self, docname: str, typ: str | None = None) -> str:
        """Map a docname to a Hugo-relative URI for cross-references.

        Uses the slugged Hugo pretty URL (``/slugged/path/``) when the toctree
        model is available, so cross-reference arithmetic matches the on-disk
        ``content/`` layout. The root document maps to the site root (``""`` in
        Sphinx's relative-URI convention).
        """
        if docname == self.config.root_doc:
            return ""
        model = self.toc_model
        if model is None:
            return docname + "/"
        # ``url_for_docname`` returns a leading-slash site-relative URL; strip
        # the leading slash so Sphinx's relative-URI math treats it as a path
        # under the site root rather than an absolute filesystem path.
        return url_for_docname(docname, model, root_doc=self.config.root_doc).lstrip(
            "/"
        )

    def prepare_writing(self, docnames) -> None:
        """Build the toctree → section/weight model once before writing.

        The model (:class:`~doxtr_hugo.toctree.TocModel`) is derived from the
        resolved ``env.toctree_includes``/``env.titles`` recorded by Sphinx and
        is reused by :meth:`finish` to emit section ``_index.md`` files with
        deterministic weights.
        """
        self.toc_model = TocModel.from_env(self.env, self.config.root_doc)

    def get_outfilename(self, docname: str) -> str:
        """Absolute path of the Markdown file emitted for ``docname``.

        Pages land under ``content/`` at the path chosen by
        :func:`doxtr_hugo.slug.content_path` (honoring the bundle mode and the
        branch/leaf distinction from the toctree model), so the emitted tree is
        a coherent, Hugo-buildable site alongside the skeleton's ``_index.md``
        section files.
        """
        model = self.toc_model
        if model is None:
            model = TocModel.from_env(self.env, self.config.root_doc)
        rel = content_path(
            docname,
            model,
            bundle_mode=self.bundle_mode,
            root_doc=self.config.root_doc,
        )
        return path.join(self.outdir, os_path(rel))

    def write_doc(self, docname: str, doctree: nodes.document) -> None:
        """Emit the Hugo Markdown (front matter + body) for a single document.

        The page is written under ``content/`` at the path chosen by
        :func:`doxtr_hugo.slug.content_path`. Every page carries at least a
        ``title`` front-matter field; branch sections additionally carry their
        deterministic ``weight`` so Hugo section ordering is stable. The
        translated Markdown body follows the front matter. Because branch/root
        pages own their ``_index.md`` here, :func:`emit_skeleton` will not
        overwrite them (it only fills in section indexes that do not yet exist).
        """
        self.current_docname = docname
        outfilename = self.get_outfilename(docname)
        ensuredir(path.dirname(outfilename))
        # Render + rewrite diagram nodes before translation so the translator
        # sees plain ``image``/raw-Markdown nodes (guide §4.1 steps 1–5). The
        # page's bundle directory (where the Markdown file lands) is the
        # co-location target so a bare-filename image reference resolves as a
        # Hugo page resource.
        render_page_diagrams(
            self, docname, doctree, path.dirname(outfilename), self.manifest
        )
        # Pre-render math (guide §4.3): PNG by default, or MathJax/MathML
        # passthrough per ``doxtr_hugo_math``. Runs before the image-copy pass
        # so math-emitted images (bare filenames) are left untouched.
        render_page_math(
            self, docname, doctree, path.dirname(outfilename), self.manifest
        )
        # Co-locate any remaining authored image/figure targets into the
        # bundle (diagram-emitted bare-filename images are left untouched).
        copy_page_images(
            self, docname, doctree, path.dirname(outfilename), self.manifest
        )
        translator = self.create_translator(doctree, self)
        doctree.walkabout(translator)
        body = translator.astext()

        # A page whose body already lists its toctree children must tell
        # section-listing themes NOT to append their own auto-generated child
        # index, or the listing renders twice (e.g. docsy's ``section-index``
        # partial). ``no_list`` is the convention such themes honour; themes
        # that ignore it (hugo-book, the bare skeleton) are unaffected.
        #
        # By the time ``write_doc`` runs, Sphinx has *resolved* every
        # non-hidden ``.. toctree::`` into a ``compact_paragraph`` carrying a
        # ``toctree=True`` attribute (the raw ``addnodes.toctree`` node only
        # survives for hidden toctrees, which the translator renders in-page
        # too). Detect either form so both the resolved list and a translator-
        # emitted hidden-toctree list suppress the theme's duplicate index.
        has_toctree = bool(doctree.next_node(addnodes.toctree)) or any(
            node.get("toctree")
            for node in doctree.findall(addnodes.compact_paragraph)
        )
        front = self._front_matter_for(docname, suppress_auto_list=has_toctree)
        text = front.render(self.front_matter) + "\n" + body

        with open(outfilename, "w", encoding="utf-8") as f:
            f.write(text)

    def _front_matter_for(
        self, docname: str, *, suppress_auto_list: bool = False
    ) -> FrontMatter:
        """Build the page front matter for ``docname``.

        Uses the toctree model for the resolved title and the deterministic
        ordering ``weight``. The weight is set for **every** non-root page
        (leaf *and* branch) so Hugo orders siblings by the authored toctree
        sequence rather than its default (title/filename) order; without it a
        section's leaf pages sort alphabetically, losing the authoring order.
        The root document carries no weight (it has no siblings).

        When ``suppress_auto_list`` is true (the page body already renders its
        toctree children as an in-page list), ``no_list = true`` is emitted so
        a section-listing theme does not *also* append its own child index and
        render the listing twice. Pure skeleton section indexes (empty body)
        are written elsewhere without this flag, so they still get the theme's
        auto-list.

        Falls back to the docname when no title is known. Extra front matter
        from ``doxtr_hugo_extra_front_matter`` is merged last.
        """
        model = self.toc_model
        node = model.get(docname) if model is not None else None
        title = node.title if node is not None else docname
        fm = FrontMatter({"title": title, "draft": False})
        if node is not None and docname != self.config.root_doc:
            fm.set("weight", node.weight)
        if suppress_auto_list:
            fm.set("no_list", True)
        extra = self.config.doxtr_hugo_extra_front_matter
        if extra:
            fm.update(extra)
        return fm

    def finish(self) -> None:
        """Emit the site skeleton (and later, global assets).

        M2: write a self-building Hugo site — ``hugo.toml`` + fallback
        ``layouts/`` + section ``_index.md`` files — driven by the
        :class:`~doxtr_hugo.toctree.TocModel` built in :meth:`prepare_writing`.
        The output lands directly in ``self.outdir`` so leaf pages written by
        :meth:`write_doc` and the emitted skeleton form one coherent site.
        """
        model = self.toc_model
        if model is None:
            model = TocModel.from_env(self.env, self.config.root_doc)
        title = getattr(self.config, "project", "") or ""
        # Branch sections that are real Sphinx documents were already written
        # (body + front matter) by ``write_doc``; exclude their section index
        # so ``emit_skeleton`` does not overwrite the page body with a
        # front-matter-only stub.
        found = set(getattr(self.env, "found_docs", []) or [])
        owned_branches = [d for d in model.branches() if d in found]
        written = emit_skeleton(
            self.outdir,
            model,
            title=title,
            base_url=self.config.doxtr_hugo_base_url,
            front_matter=self.front_matter,
            emit_mode=self.emit_skeleton_mode,
            extra_section_front_matter=self.config.doxtr_hugo_extra_front_matter
            or None,
            skip_section_docnames=owned_branches,
        )
        logger.info("doxtr_hugo: emitted %d skeleton file(s)", len(written))

        # Theme configuration: compose the final hugo.toml from core defaults +
        # the resolved theme profile + the user's conf.py overrides, copy the
        # user's sidecar overlay verbatim, scaffold the theme source, write
        # README-hugo.md, and run the opt-in user hook. This turns the output
        # into a ready-to-serve, version-controllable Hugo site configured for
        # the user's theme.
        self._emit_theme(title)

        # Serialize the asset manifest (rendered diagrams + copied images) so
        # the post-build verifier (guide §7.2) can assert, per asset, that the
        # emitted file exists with the declared format/extension/magic bytes.
        # Only written when at least one asset was emitted, so a diagram-free
        # build leaves no stray manifest.
        if len(self.manifest):
            self.manifest.write(self.outdir)
            logger.info(
                "doxtr_hugo: recorded %d asset(s) in the build manifest",
                len(self.manifest),
            )

    def _emit_theme(self, title: str) -> None:
        """Configure the emitted site for the user's theme (guide: theme config).

        Composes the final ``hugo.toml`` from three precedence layers (core <
        theme profile < user ``doxtr_hugo_theme_config``), copies the user's
        sidecar overlay verbatim, scaffolds the theme source (e.g. a Hugo-module
        import), writes ``README-hugo.md`` with the one-time commands a human
        must still run, then runs the opt-in user Python hook. A no-op unless a
        theme/overlay/source/hook is configured, so default builds are
        unchanged. Only runs for the ``full`` emit mode: a ``content-only`` drop
        -in has no ``hugo.toml`` of its own and inherits the host site's config.
        """
        cfg = self.config
        theme_name = getattr(cfg, "doxtr_hugo_theme", None)
        user_cfg = getattr(cfg, "doxtr_hugo_theme_config", {}) or {}
        user_source = getattr(cfg, "doxtr_hugo_theme_source", {}) or {}
        overlay_cfg = getattr(cfg, "doxtr_hugo_theme_overlay_dir", "") or ""
        hook_spec = getattr(cfg, "doxtr_hugo_theme_hook", None)

        # content-only emit owns no hugo.toml; leave theme wiring to the host.
        if self.emit_skeleton_mode != "full":
            if theme_name or user_cfg or user_source or hook_spec:
                logger.info(
                    "doxtr_hugo: theme configuration is ignored for "
                    "emit_skeleton=%r (content-only drop-in inherits the host "
                    "site's hugo.toml).",
                    self.emit_skeleton_mode,
                )
            # The overlay is still useful content-only (custom layouts/assets),
            # so copy it even in this mode.
            self._copy_theme_overlay(overlay_cfg)
            return

        profile = theme_support.get_profile(theme_name) if theme_name else None
        if theme_name and profile is None:
            logger.info(
                "doxtr_hugo: no shipped profile for theme %r; emitting "
                "theme=%r with core defaults + your doxtr_hugo_theme_config "
                "only. Provide the theme and any extra config yourself.",
                theme_name,
                theme_name,
            )

        # Layer the Hugo config: core < profile.config < user_config.
        merged = theme_support.resolve_theme_config(
            title=title,
            base_url=cfg.doxtr_hugo_base_url,
            language_code=getattr(cfg, "language", "en") or "en-us",
            profile=profile,
            theme_name=theme_name,
            user_config=user_cfg,
        )

        # Theme-source scaffolding (module import goes into hugo.toml).
        source = theme_support.resolve_source(profile, user_source)
        if theme_name and source.get("kind") == "module":
            merged = theme_support.deep_merge(
                merged, theme_support.render_module_config(theme_name, source, profile)
            )

        # Only overwrite hugo.toml when the user actually configured a theme or
        # extra config; otherwise leave the skeleton's minimal hugo.toml intact.
        if theme_name or user_cfg or user_source:
            toml_text = theme_support.dump_toml(merged)
            with open(path.join(self.outdir, "hugo.toml"), "w", encoding="utf-8") as f:
                f.write(toml_text)
            logger.info(
                "doxtr_hugo: wrote theme-configured hugo.toml%s",
                f" (theme={theme_name!r})" if theme_name else "",
            )

        # Sidecar overlay: user's verbatim Hugo files win over the emitted site.
        self._copy_theme_overlay(overlay_cfg)

        # README-hugo.md documents the remaining one-time human steps.
        readme = theme_support.render_readme(
            theme_name=theme_name,
            source=source,
            profile=profile,
            project=getattr(cfg, "project", "") or "",
        )
        with open(path.join(self.outdir, "README-hugo.md"), "w", encoding="utf-8") as f:
            f.write(readme)

        # Opt-in Python hook (user-project code) runs last, after all files are
        # on disk, for anything the declarative surfaces cannot express.
        if hook_spec:
            self._run_theme_hook(hook_spec)

    def _copy_theme_overlay(self, overlay_cfg: str) -> None:
        """Copy the confdir-relative sidecar overlay into the output verbatim."""
        if not overlay_cfg:
            return
        confdir = getattr(self, "confdir", None) or getattr(self.env, "srcdir", "")
        src = theme_support.overlay_dir(confdir, overlay_cfg)
        copied = theme_support.copy_overlay(src, self.outdir)
        if copied:
            logger.info(
                "doxtr_hugo: applied %d file(s) from theme overlay %s",
                len(copied),
                src,
            )

    def _run_theme_hook(self, hook_spec: str) -> None:
        """Import and call the opt-in ``"pkg.mod:callable"`` post-emit hook.

        The hook is the user's own project code (declared in their ``conf.py``),
        so it is no more privileged than ``conf.py`` itself. It is called with
        keyword arguments ``outdir``, ``config``, and ``app`` so it can do
        arbitrary post-processing of the emitted Hugo site. Any exception is
        logged as a warning and does not abort the build (the site is already
        written); set the hook carefully.
        """
        try:
            module_name, _, attr = str(hook_spec).partition(":")
            if not module_name or not attr:
                raise ValueError(
                    f"doxtr_hugo_theme_hook must be 'package.module:callable', "
                    f"got {hook_spec!r}"
                )
            import importlib

            mod = importlib.import_module(module_name)
            hook = getattr(mod, attr)
            hook(outdir=self.outdir, config=self.config, app=self.app)
            logger.info("doxtr_hugo: ran theme hook %s", hook_spec)
        except Exception as exc:  # pragma: no cover - user code
            logger.warning(
                "doxtr_hugo: theme hook %r failed: %s", hook_spec, exc
            )

    def get_relative_uri(self, from_: str, to: str, typ: str | None = None) -> str:
        """Relative URI between two docnames (used by cross-references).

        ``get_target_uri`` returns ``""`` for the root document (it maps to the
        site root under Hugo pretty URLs). ``os.path.relpath`` rejects an empty
        path, so both endpoints are normalized to ``"."`` before the relative
        path is computed.
        """
        to_uri = self.get_target_uri(to, typ) or "."
        from_uri = self.get_target_uri(from_) or "."
        return path.relpath(to_uri, from_uri)
