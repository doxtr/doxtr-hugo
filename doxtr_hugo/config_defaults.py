"""Canonical configuration defaults for doxtr-hugo.

This module is the single source of truth for the extension's configuration
surface. ``setup(app)`` registers every key here via ``app.add_config_value``,
and the rest of the package reads values through ``app.config`` using these
same names. Keep additions here (not scattered across modules) so the config
surface stays discoverable, matching the ``doxtr-roadmap`` convention.
"""

DEFAULT_CONFIG = {
    # Output shape
    "doxtr_hugo_front_matter": "toml",          # toml | yaml | json
    "doxtr_hugo_bundle_mode": "leaf",           # leaf | flat
    "doxtr_hugo_emit_skeleton": "full",         # full | content-only

    # Images / diagrams
    "doxtr_hugo_image_format": "auto",          # auto | svg | png | webp
    "doxtr_hugo_raster_fallback": "png",        # png | webp
    "doxtr_hugo_process_images": False,         # force raster for Hugo pipeline use
    "doxtr_hugo_math": "png",                   # png | mathjax | mathml-passthrough
    "doxtr_hugo_on_missing_backend": "warn-passthrough",  # warn-passthrough|warn-skip|error

    # Links / rendering
    "doxtr_hugo_link_style": "relref",          # relref | relative
    "doxtr_hugo_admonitions": "shortcode",      # shortcode | html
    "doxtr_hugo_tables": "auto",                # auto(pipe, html fallback) | html
    "doxtr_hugo_title_heading": "frontmatter",  # frontmatter(drop body H1) | body

    # Site skeleton
    "doxtr_hugo_base_url": "/",
    "doxtr_hugo_extra_front_matter": {},        # merged into every page

    # Theme configuration (emit a ready-to-serve, VCS-committable Hugo site)
    "doxtr_hugo_theme": None,                    # shipped profile name | theme name | None
    "doxtr_hugo_theme_config": {},               # deep-merged into hugo.toml (user wins)
    "doxtr_hugo_theme_source": {},               # {kind, url, version, path}; overrides profile
    "doxtr_hugo_theme_overlay_dir": ".doxtr/hugo",  # confdir-relative sidecar overlay
    "doxtr_hugo_theme_hook": None,               # "pkg.mod:callable" opt-in post-emit hook
}
