# Vendored Hugo theme matrix (guide §7.2a)

This directory holds the **pinned** Hugo themes the harness builds the emitted
`content/**` against, in addition to the built-in skeleton. A single `THEMES`
table in `../test_runner.py` drives the matrix; each entry expects its theme at
`themes/<dirname>`.

Themes are vendored as **pinned git submodules** (reproducible; no floating
`main` fetched at CI time). When a theme directory is absent the matrix records
a `SKIP` with a reason — never a silent pass — so the local/dev run stays green
without network access while CI pins the submodules below.

The five lighter themes build with the **standard** Hugo 0.140.2 binary (no
extended edition, no Go modules). Two heavier, popular docs themes
(**hugo-book** and **docsy**) were added to the matrix; they need the
**extended** Hugo binary (both transpile SCSS) and, for docsy, npm deps
(Bootstrap + Font Awesome) plus a pinned **Dart Sass**. The preview launcher
`scripts/preview-themes.sh` therefore installs **extended** Hugo 0.168.0 (which
also builds the five lighter themes) and provisions docsy's toolchain on demand.
(Ananke and PaperMod were dropped: Ananke references the removed
`_internal/google_analytics_async.html` and PaperMod uses the removed
`.Site.Social`, so neither builds under the pinned Hugo.)

## Pinned matrix

| `dirname`        | Theme         | Repo                                         | Pinned commit                              | `unsafe` default | Hugo edition | Extra toolchain |
|------------------|---------------|----------------------------------------------|--------------------------------------------|------------------|--------------|-----------------|
| `archie`         | Archie        | https://github.com/athul/archie              | `cbc1c3aaa2615fa348b85ef39eb4570cd3ac6414` | on               | standard     | — |
| `hugo-terminal`  | hugo-terminal | https://github.com/panr/hugo-theme-terminal  | `8ef994c0e0303861b1204d1248541af06c9bfea6` (v4.2.6) | on      | standard     | — |
| `hyde`           | Hyde          | https://github.com/spf13/hyde                | `2a3f431a852b2fddade17352c1544e12ff7d703f` (v1.4.1) | on      | standard     | — |
| `risotto`        | Risotto       | https://github.com/joeroe/risotto            | `ab6d589f24c1dd3ee4b14eb7c6bf788e99b64356` | on               | standard     | — |
| `notepadium`     | Notepadium    | https://github.com/cntrump/hugo-notepadium   | `c3f5116e08982b32d5a15021b59c132e9a256643` | on               | standard     | — |
| `hugo-book`      | Hugo Book     | https://github.com/alex-shpak/hugo-book      | `79af35c876870c6c9f9edede15ef093f4a7bbf8a` | on               | **extended** | needs `BookSection="*"` to use our root sections |
| `docsy`          | Docsy         | https://github.com/google/docsy              | `6d6e98b7abea19bd6d0e1b818dbf12f4901ce7ba` | on               | **extended** | theme in `docsy/theme`; npm (Bootstrap + Font Awesome) + Dart Sass 1.105.0 |

- The harness builds each theme against the real dox site (109–115 pages, all
  78 diagram/image refs intact).
- Images are made theme-portable by the emitted
  `layouts/_default/_markup/render-image.html` render hook, which rewrites
  bundle-local image refs to the authoring page's `.RelPermalink` so math and
  diagram images resolve even when a theme inlines full page content on a home
  feed / section list (previously a 404 on list pages).
- `unsafe` is each theme's documented default. If a theme ships raw HTML
  **off**, the matrix generically exercises the shortcode → raw-HTML admonition
  downgrade path (the harness detects a missing shortcode and re-emits with
  `doxtr_hugo_admonitions=html`). All current themes ship `unsafe` on.
- **hugo-book** stores content under a single section by default
  (`BookSection="docs"`); the preview sets `BookSection="*"` so our
  `basics`/`extensions` tree drives the sidebar.
- **docsy** keeps the Hugo theme in a `theme/` subdirectory and mounts
  `node_modules/bootstrap` + `node_modules/@fortawesome/*` as Hugo assets, so
  its SCSS needs those npm packages present and a Dart Sass binary on PATH
  (pinned to docsy's `sass-embedded` devDependency). A run where npm or Dart
  Sass is unavailable records docsy as a `SKIP`, not a failure.

## Adding / pinning a theme (CI)

```bash
# From the repo root, pin each theme at a specific commit:
git submodule add https://github.com/athul/archie test_harness/themes/archie
git -C test_harness/themes/archie checkout cbc1c3aaa2615fa348b85ef39eb4570cd3ac6414
git submodule add https://github.com/panr/hugo-theme-terminal test_harness/themes/hugo-terminal
git -C test_harness/themes/hugo-terminal checkout 8ef994c0e0303861b1204d1248541af06c9bfea6
git submodule add https://github.com/spf13/hyde test_harness/themes/hyde
git -C test_harness/themes/hyde checkout 2a3f431a852b2fddade17352c1544e12ff7d703f
git submodule add https://github.com/joeroe/risotto test_harness/themes/risotto
git -C test_harness/themes/risotto checkout ab6d589f24c1dd3ee4b14eb7c6bf788e99b64356
git submodule add https://github.com/cntrump/hugo-notepadium test_harness/themes/notepadium
git -C test_harness/themes/notepadium checkout c3f5116e08982b32d5a15021b59c132e9a256643
git submodule add https://github.com/alex-shpak/hugo-book test_harness/themes/hugo-book
git -C test_harness/themes/hugo-book checkout 79af35c876870c6c9f9edede15ef093f4a7bbf8a
git submodule add https://github.com/google/docsy test_harness/themes/docsy
git -C test_harness/themes/docsy checkout 6d6e98b7abea19bd6d0e1b818dbf12f4901ce7ba
git submodule update --init --recursive
```

Keep the submodule commit pinned (not tracking a branch) so the matrix is
reproducible. Update the table above whenever a pin changes.
