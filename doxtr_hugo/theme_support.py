"""Theme profiles, config merge, sidecar overlay, and the emitted README.

This module makes *theme configuration* a first-class, user-facing feature of
the ``doxtr_hugo`` builder. The goal (see the design discussion) is that a user
configures their Hugo theme once, entirely inside their Sphinx project, and
``sphinx-build -b hugo`` emits a complete, version-controllable Hugo site under
``build/hugo/`` whose ``hugo.toml`` is correctly configured for that theme.

The extension never shells out to ``hugo``/``npm``/``git`` at build time: it
emits *input* for Hugo plus a generated ``README-hugo.md`` documenting the exact
one-time commands a human/CI must still run (``hugo mod get``, ``npm install``,
installing Dart Sass, ``hugo serve``). Documentation, not execution.

Three layers compose the final ``hugo.toml``, lowest precedence first:

1. **Core defaults** — the theme-agnostic base (:func:`core_hugo_config`):
   ``baseURL``/``title``/``languageCode``, Goldmark ``unsafe`` + extensions.
2. **Theme profile** — the committed, tested baseline for a theme
   (:data:`PROFILES`): the Hugo ``config`` fragment the theme needs to render
   our content (e.g. hugo-book's ``BookSection = "*"``, docsy's ``[[cascade]]``)
   plus declarative acquisition/toolchain metadata (``source``/``requires``).
3. **User overrides** — ``doxtr_hugo_theme_config`` from ``conf.py`` (and the
   raw config fragments a user drops into the sidecar overlay). Merged last, so
   the user always wins.

The sidecar overlay (``<confdir>/.doxtr/hugo/`` by default) is the declarative
escape hatch: *any* Hugo files the user puts there (custom ``layouts/``,
``assets/scss``, ``static/``, extra ``config/`` fragments) are copied verbatim
into the output on top of the emitted site. No extension change is needed for
arbitrary theme tweaks. An opt-in Python hook (``doxtr_hugo_theme_hook``) runs
last for anything the declarative surfaces cannot express; because it is the
user's *own* project code it is no more privileged than ``conf.py`` itself.

Everything here is pure data + pure functions (no Sphinx/builder object), so it
is cheap to unit-test without a build.
"""

from __future__ import annotations

import copy
import os
import shutil
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Tuple

# --------------------------------------------------------------------------- #
# Theme profiles (shipped baseline knowledge — data, not code)
# --------------------------------------------------------------------------- #
#
# A profile is a plain dict with these optional keys:
#   label    : human-readable name (defaults to the profile key)
#   hugo      : "standard" | "extended"  — required Hugo edition (doc note)
#   min_hugo  : minimum Hugo version string (doc note)
#   subpath   : the theme lives in this subdir of its repo (source materializer)
#   source    : default acquisition descriptor (see ``theme_source`` config);
#               a dict {kind, url, version?}. ``kind`` is module|submodule|
#               git|vendor|path|none.
#   requires  : {dart_sass: "x.y.z", npm: [pkg, ...]} — toolchain the site needs
#               (surfaced in README-hugo.md; never executed by the extension)
#   config    : a Hugo config fragment (arbitrary nested tables / array-tables)
#               deep-merged into hugo.toml as layer 2.
#   preview_port : the port the preview launcher serves this theme on (shared
#               source of truth with scripts/preview-themes.sh).
#
# These mirror the knowledge previously hardcoded in scripts/preview-themes.sh.

PROFILES: Dict[str, Dict[str, Any]] = {
    "archie": {
        "label": "Archie",
        "hugo": "standard",
        "source": {"kind": "module", "url": "github.com/athul/archie"},
        "preview_port": 1314,
        "config": {},
    },
    "hugo-terminal": {
        "label": "hugo-terminal",
        "hugo": "standard",
        "source": {"kind": "module", "url": "github.com/panr/hugo-theme-terminal"},
        "preview_port": 1315,
        # terminal lists a 'posts' content type by default; point it at our
        # content so the home feed is not empty. (Overridable by the user.)
        "config": {"params": {"contentTypeName": "docs", "showMenuItems": 5}},
    },
    "hyde": {
        "label": "Hyde",
        "hugo": "standard",
        "source": {"kind": "module", "url": "github.com/spf13/hyde"},
        "preview_port": 1316,
        "config": {},
    },
    "risotto": {
        "label": "Risotto",
        "hugo": "standard",
        "source": {"kind": "module", "url": "github.com/joeroe/risotto"},
        "preview_port": 1317,
        "config": {},
    },
    "notepadium": {
        "label": "Notepadium",
        "hugo": "standard",
        "source": {"kind": "module", "url": "github.com/cntrump/hugo-notepadium"},
        "preview_port": 1318,
        "config": {},
    },
    "hugo-book": {
        "label": "Hugo Book",
        "hugo": "extended",
        "min_hugo": "0.158.0",
        "source": {"kind": "module", "url": "github.com/alex-shpak/hugo-book"},
        "preview_port": 1319,
        # Book keys its sidebar off a single content section (default "docs");
        # "*" uses all root sections so an arbitrary toctree drives the sidebar.
        "config": {"params": {"BookSection": "*", "BookToC": True}},
    },
    "docsy": {
        "label": "Docsy",
        "hugo": "extended",
        "min_hugo": "0.160.1",
        "subpath": "theme",
        "source": {"kind": "module", "url": "github.com/google/docsy"},
        "requires": {
            "dart_sass": "1.105.0",
            "npm": ["bootstrap", "@fortawesome/fontawesome-free"],
        },
        "preview_port": 1320,
        # Docsy renders page bodies only through its layouts/docs/* templates,
        # selected by page *type*. A page's type defaults to its top-level
        # section; docsy ships no templates for arbitrary section names, so the
        # body renders empty. Cascade type="docs" to every page so docsy's docs
        # templates render our content.
        "config": {"cascade": [{"type": "docs", "_target": {"path": "/**"}}]},
    },
}


def get_profile(name: str) -> Optional[Dict[str, Any]]:
    """Return a deep copy of the shipped profile ``name`` (or ``None``)."""
    prof = PROFILES.get(name)
    return copy.deepcopy(prof) if prof is not None else None


# --------------------------------------------------------------------------- #
# Deep merge (layer composition)
# --------------------------------------------------------------------------- #

def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` onto ``base`` and return a new dict.

    Mappings merge key-by-key; a mapping on both sides recurses. Any
    non-mapping value in ``override`` (including lists) **replaces** the base
    value wholesale — list concatenation is intentionally *not* done, so a user
    override of an array-table (e.g. ``cascade``) is a full, predictable
    replacement rather than an append that could duplicate entries. Inputs are
    not mutated.
    """
    out: Dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in override.items():
        if (
            key in out
            and isinstance(out[key], Mapping)
            and isinstance(value, Mapping)
        ):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


# --------------------------------------------------------------------------- #
# Core (layer 1) Hugo config
# --------------------------------------------------------------------------- #

#: Goldmark extensions the translator depends on (tables/footnotes/etc.).
_GOLDMARK_EXTENSIONS = ("table", "footnote", "definitionList", "taskList")


def core_hugo_config(
    title: str,
    base_url: str = "/",
    *,
    language_code: str = "en-us",
) -> Dict[str, Any]:
    """Return the theme-agnostic base Hugo config as a plain dict (layer 1).

    Mirrors the minimal ``hugo.toml`` the builder has always emitted: top-level
    ``baseURL``/``title``/``languageCode`` and the Goldmark ``unsafe`` +
    extensions the translator relies on. Returned as structured data so theme
    profiles and user overrides can be deep-merged on top before serialization.
    """
    return {
        "baseURL": base_url,
        "title": title,
        "languageCode": language_code,
        "markup": {
            "goldmark": {
                "renderer": {"unsafe": True},
                "extensions": {name: True for name in _GOLDMARK_EXTENSIONS},
            }
        },
    }


def resolve_theme_config(
    *,
    title: str,
    base_url: str,
    language_code: str,
    profile: Optional[Mapping[str, Any]],
    theme_name: Optional[str],
    user_config: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Compose the final Hugo config dict from the three precedence layers.

    1. core defaults, 2. the theme ``theme = "<name>"`` key + the profile's
    ``config`` fragment, 3. the user's ``doxtr_hugo_theme_config``. Returns the
    merged mapping ready for :func:`dump_toml`.
    """
    cfg = core_hugo_config(title, base_url, language_code=language_code)
    if theme_name:
        cfg["theme"] = theme_name
    if profile and profile.get("config"):
        cfg = deep_merge(cfg, profile["config"])
    if user_config:
        cfg = deep_merge(cfg, user_config)
    return cfg


# --------------------------------------------------------------------------- #
# General TOML emitter (handles nested tables + array-of-tables)
# --------------------------------------------------------------------------- #
#
# The front-matter emitter only handles one level of nested table; a theme's
# merged Hugo config needs arbitrary nesting and array-of-tables (docsy's
# ``[[cascade]]``). This is a small, deterministic, dependency-free writer for
# the JSON-like value space (dict / list / str / int / float / bool). Key order
# follows insertion order so output is stable and diff-friendly.


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    # Reuse the front-matter string quoter for identical escaping.
    from .frontmatter import toml_string

    return toml_string(str(value))


def _is_table(v: Any) -> bool:
    return isinstance(v, Mapping)


def _is_table_array(v: Any) -> bool:
    return (
        isinstance(v, (list, tuple))
        and len(v) > 0
        and all(isinstance(i, Mapping) for i in v)
    )


def _toml_inline_array(value) -> str:
    """Render a list of scalars as an inline TOML array."""
    return "[" + ", ".join(_toml_scalar(v) for v in value) + "]"


def _dump_table(data: Mapping[str, Any], prefix: str, out: List[str]) -> None:
    """Emit ``data`` as TOML, recursing into nested tables/array-tables.

    ``prefix`` is the dotted header path of the current table ("" at the root).
    Scalars and scalar-arrays are written first (they belong to the current
    table header), then nested tables and array-of-tables as their own headers,
    as TOML requires. A table header is **not** emitted for an intermediate
    table that has no scalar keys of its own (only nested tables) — the nested
    headers already carry the full dotted path, so an empty ``[a.b]`` line would
    be redundant noise in the diff.
    """
    scalars = []
    tables = []
    for key, value in data.items():
        if _is_table(value):
            tables.append((key, value, False))
        elif _is_table_array(value):
            tables.append((key, value, True))
        else:
            scalars.append((key, value))

    for key, value in scalars:
        if isinstance(value, (list, tuple)):
            out.append(f"{key} = {_toml_inline_array(value)}")
        else:
            out.append(f"{key} = {_toml_scalar(value)}")

    for key, value, is_array in tables:
        header_path = f"{prefix}.{key}" if prefix else key
        if is_array:
            for item in value:
                if out and out[-1] != "":
                    out.append("")
                out.append(f"[[{header_path}]]")
                _dump_table(item, header_path, out)
        else:
            # Skip the header line for a purely-intermediate table (only nested
            # tables, no scalars): the child headers carry the dotted path.
            only_nested = value and all(
                _is_table(v) or _is_table_array(v) for v in value.values()
            )
            if not only_nested:
                if out and out[-1] != "":
                    out.append("")
                out.append(f"[{header_path}]")
            _dump_table(value, header_path, out)


def dump_toml(data: Mapping[str, Any]) -> str:
    """Serialize a nested config mapping to a deterministic TOML document."""
    out: List[str] = []
    _dump_table(data, "", out)
    text = "\n".join(out).strip("\n")
    return text + "\n" if text else ""


# --------------------------------------------------------------------------- #
# Sidecar overlay (declarative escape hatch)
# --------------------------------------------------------------------------- #

def overlay_dir(confdir: str, configured: str) -> str:
    """Resolve the overlay directory, confdir-relative unless absolute."""
    if os.path.isabs(configured):
        return configured
    return os.path.join(confdir, configured)


def copy_overlay(src_dir: str, out_dir: str) -> List[str]:
    """Copy the user's sidecar overlay tree verbatim into the output.

    Every file under ``src_dir`` is copied to the same relative path under
    ``out_dir``, overwriting the emitted site (user files win — this is the
    escape hatch). Returns the list of output-relative paths written. A missing
    or empty overlay dir is a no-op (returns ``[]``). ``.git`` and typical junk
    are skipped so a user who keeps the overlay under version control does not
    leak VCS metadata into the Hugo input.
    """
    written: List[str] = []
    if not src_dir or not os.path.isdir(src_dir):
        return written
    skip_names = {".git", ".DS_Store", "__pycache__"}
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in skip_names]
        for name in files:
            if name in skip_names:
                continue
            abs_src = os.path.join(root, name)
            rel = os.path.relpath(abs_src, src_dir)
            abs_dst = os.path.join(out_dir, rel)
            os.makedirs(os.path.dirname(abs_dst), exist_ok=True)
            shutil.copy2(abs_src, abs_dst)
            written.append(rel.replace(os.sep, "/"))
    return sorted(written)


# --------------------------------------------------------------------------- #
# Theme source materialization scaffolding
# --------------------------------------------------------------------------- #

def resolve_source(
    profile: Optional[Mapping[str, Any]],
    user_source: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Resolve the effective theme-source descriptor.

    The profile supplies a default (e.g. a Hugo-module URL); the user's
    ``doxtr_hugo_theme_source`` overrides/augments it. Defaults to
    ``{"kind": "none"}`` when neither names a source, meaning the extension
    scaffolds nothing and the user manages the theme themselves.
    """
    src: Dict[str, Any] = {}
    if profile and isinstance(profile.get("source"), Mapping):
        src.update(copy.deepcopy(dict(profile["source"])))
    if user_source:
        src.update(dict(user_source))
    if not src.get("kind"):
        src["kind"] = "none"
    return src


def module_import_path(source: Mapping[str, Any], profile: Optional[Mapping[str, Any]]) -> str:
    """Return the Hugo-module import path for a ``module`` source.

    Appends the profile ``subpath`` (e.g. docsy's ``theme``) so the module
    import points at the actual theme directory inside the repo.
    """
    url = str(source.get("url", "")).rstrip("/")
    sub = (profile or {}).get("subpath")
    if sub:
        url = f"{url}/{str(sub).strip('/')}"
    return url


def render_module_config(
    theme_name: str,
    source: Mapping[str, Any],
    profile: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return the ``module.imports`` config fragment for a Hugo-module theme.

    Returning config (not a go.mod) keeps everything in ``hugo.toml``; Hugo
    resolves and caches the module on first build after ``hugo mod get``.
    """
    path = module_import_path(source, profile)
    frag: Dict[str, Any] = {"module": {"imports": [{"path": path}]}}
    return frag


# --------------------------------------------------------------------------- #
# README-hugo.md (documentation, not execution)
# --------------------------------------------------------------------------- #

def _acquisition_steps(
    theme_name: Optional[str],
    source: Mapping[str, Any],
    profile: Optional[Mapping[str, Any]],
) -> List[str]:
    """Human steps to make the configured theme available to Hugo."""
    kind = source.get("kind", "none")
    if kind == "module":
        path = module_import_path(source, profile)
        version = source.get("version")
        get = "hugo mod get -u" if not version else f"hugo mod get {path}@{version}"
        return [
            f"This site uses the **{theme_name}** theme as a Hugo Module "
            f"(`{path}`).",
            "Fetch it once (requires network + a Go toolchain for Hugo Modules):",
            "",
            "```bash",
            "hugo mod get -u",
            "```" if not version else f"# pinned: {get}",
        ]
    if kind == "submodule":
        url = source.get("url", "")
        return [
            f"This site uses the **{theme_name}** theme as a git submodule.",
            "",
            "```bash",
            f"git submodule add {url} themes/{theme_name}",
            "git submodule update --init --recursive",
            "```",
        ]
    if kind == "vendor":
        return [
            f"The **{theme_name}** theme is vendored under `themes/{theme_name}/` "
            "in this directory — no acquisition step is needed."
        ]
    if kind == "path":
        return [
            f"The **{theme_name}** theme is expected at the local path "
            f"`{source.get('path', '<path>')}` (configured via "
            "`doxtr_hugo_theme_source`)."
        ]
    if theme_name:
        return [
            f"A theme (`theme = \"{theme_name}\"`) is configured but no source "
            "was declared. Install it under `themes/` yourself, or set "
            "`doxtr_hugo_theme_source` in your Sphinx `conf.py`."
        ]
    return [
        "No theme is configured. This site uses the built-in doxtr-hugo "
        "fallback layouts and serves as-is."
    ]


def _toolchain_steps(profile: Optional[Mapping[str, Any]]) -> List[str]:
    """Human steps for the toolchain a theme needs (Dart Sass, npm)."""
    lines: List[str] = []
    requires = (profile or {}).get("requires") or {}
    edition = (profile or {}).get("hugo")
    if edition == "extended":
        min_hugo = (profile or {}).get("min_hugo")
        note = "Hugo **extended** edition is required"
        if min_hugo:
            note += f" (\u2265 {min_hugo})"
        lines.append(note + ".")
    dart = requires.get("dart_sass")
    if dart:
        lines += [
            "",
            f"Install **Dart Sass** {dart} (on your `PATH`), which Hugo uses to "
            "transpile the theme's SCSS:",
            "",
            "```bash",
            "# see https://sass-lang.com/dart-sass/ for your platform",
            f"# pinned version: {dart}",
            "```",
        ]
    npm = requires.get("npm")
    if npm:
        sub = (profile or {}).get("subpath")
        where = f"themes/<theme>/{sub}" if sub else "the theme directory"
        lines += [
            "",
            f"Install the theme's npm dependencies ({', '.join(npm)}) in "
            f"{where} (the theme mounts them as Hugo assets):",
            "",
            "```bash",
            "npm install",
            "```",
        ]
    return lines


def render_readme(
    *,
    theme_name: Optional[str],
    source: Mapping[str, Any],
    profile: Optional[Mapping[str, Any]],
    project: str,
) -> str:
    """Render ``README-hugo.md`` documenting how to build/serve this output.

    The emitted ``build/hugo/`` is complete Hugo *input*, ready for version
    control. This README records the exact one-time commands a human/CI must
    still run for the configured theme (module fetch, Dart Sass, npm) before
    ``hugo serve`` works — documentation, never executed by the extension.
    """
    lines: List[str] = []
    title = project or "This site"
    lines.append(f"# {title} — Hugo output")
    lines.append("")
    lines.append(
        "This directory was generated by **doxtr-hugo** "
        "(`sphinx-build -b hugo`). It is a complete, self-contained Hugo site "
        "source tree: commit it to version control and build it with Hugo."
    )
    lines.append("")
    lines.append("## 1. Make the theme available")
    lines.append("")
    lines += _acquisition_steps(theme_name, source, profile)

    tool = _toolchain_steps(profile)
    if tool:
        lines.append("")
        lines.append("## 2. Install the theme toolchain")
        lines += tool

    lines.append("")
    lines.append("## Serve")
    lines.append("")
    lines.append("```bash")
    lines.append("hugo server")
    lines.append("```")
    lines.append("")
    lines.append(
        "Then open the printed URL. For a production build run `hugo` and "
        "publish the generated `public/` directory."
    )
    lines.append("")
    lines.append(
        "> Generated by doxtr-hugo. Re-running `sphinx-build -b hugo` "
        "regenerates this file; edit your Sphinx `conf.py` "
        "(`doxtr_hugo_theme*`) or the `.doxtr/hugo/` overlay, not this file."
    )
    lines.append("")
    return "\n".join(lines)
