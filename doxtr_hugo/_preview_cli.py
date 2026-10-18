"""Shell-facing CLI bridge for ``scripts/preview-themes.sh``.

The preview launcher must not re-encode theme knowledge (ports, Hugo edition,
subdir layout, toolchain requirements, per-theme Hugo config) that already lives
in :data:`doxtr_hugo.theme_support.PROFILES`. This module is the single bridge
the shell script calls so the profiles stay the one source of truth shared by
the real ``sphinx-build`` emit and the preview.

Subcommands (all print to stdout; exit non-zero on unknown theme):

``themes``
    Print every profile's preview dirname, one per line (ordered by port).

``field <theme> <key>``
    Print one scalar field for a theme: ``port``, ``hugo`` (edition),
    ``subpath`` (empty if none), ``dart_sass`` (version or empty), ``npm``
    (space-separated packages or empty).

``hugo-toml <theme>``
    Print a complete preview ``hugo.toml`` for the theme: the profile's Hugo
    ``config`` fragment deep-merged over preview-level defaults, with the
    content-derived menu/sections appended. The caller passes the discovered
    top-level sections via ``--sections a,b,c``; everything else comes from the
    profile, so adding/removing a theme or changing its config needs no shell
    edit.

This is deliberately tiny and dependency-free so it runs under the same Python
the extension targets (``>= 3.8``).
"""

from __future__ import annotations

import argparse
import sys
from typing import List

from . import theme_support as ts


def _ordered_theme_names() -> List[str]:
    return sorted(ts.PROFILES, key=lambda n: ts.PROFILES[n].get("preview_port", 0))


def _cmd_themes(_args) -> int:
    for name in _ordered_theme_names():
        print(name)
    return 0


def _cmd_field(args) -> int:
    prof = ts.get_profile(args.theme)
    if prof is None:
        print(f"unknown theme: {args.theme}", file=sys.stderr)
        return 2
    key = args.key
    if key == "port":
        print(prof.get("preview_port", ""))
    elif key == "hugo":
        print(prof.get("hugo", "standard"))
    elif key == "subpath":
        print(prof.get("subpath", "") or "")
    elif key == "dart_sass":
        print((prof.get("requires") or {}).get("dart_sass", "") or "")
    elif key == "npm":
        print(" ".join((prof.get("requires") or {}).get("npm", []) or []))
    else:
        print(f"unknown field: {key}", file=sys.stderr)
        return 2
    return 0


def _cmd_hugo_toml(args) -> int:
    prof = ts.get_profile(args.theme)
    if prof is None:
        print(f"unknown theme: {args.theme}", file=sys.stderr)
        return 2
    sections = [s for s in (args.sections or "").split(",") if s]

    # Preview-level base config (NOT part of a theme profile — it is specific to
    # previewing the dox reference site: give every theme working navigation and
    # keep newer-Hugo deprecations non-fatal). The profile's own config fragment
    # (e.g. docsy's cascade, hugo-book's BookSection) merges on top.
    cfg = {
        "baseURL": "/",
        "title": "Doxamples",
        "theme": args.theme,
        "pagination": {"pagerSize": 10},
        "ignoreLogs": ["warning-deprecated"],
        "markup": {"goldmark": {"renderer": {"unsafe": True}}},
    }
    if sections:
        cfg["mainSections"] = list(sections)
        # contentTypeName lets section-driven themes (hugo-terminal) list our
        # content instead of a non-existent 'posts' type; first section is fine.
        cfg.setdefault("params", {})
        cfg["params"]["contentTypeName"] = sections[0]
        cfg["params"]["showMenuItems"] = 5
        cfg["menu"] = {
            "main": [
                {"name": s, "url": f"/{s}/", "weight": (i + 1) * 10}
                for i, s in enumerate(sections)
            ]
        }

    # Deep-merge the profile's Hugo config fragment last so theme-specific keys
    # (docsy cascade, hugo-book BookSection, hugo-terminal contentTypeName) win
    # over the preview defaults.
    if prof.get("config"):
        cfg = ts.deep_merge(cfg, prof["config"])

    sys.stdout.write(ts.dump_toml(cfg))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m doxtr_hugo._preview_cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("themes").set_defaults(func=_cmd_themes)

    p_field = sub.add_parser("field")
    p_field.add_argument("theme")
    p_field.add_argument("key")
    p_field.set_defaults(func=_cmd_field)

    p_toml = sub.add_parser("hugo-toml")
    p_toml.add_argument("theme")
    p_toml.add_argument("--sections", default="")
    p_toml.set_defaults(func=_cmd_hugo_toml)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
