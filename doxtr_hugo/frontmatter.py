"""Per-page front-matter model and serialization.

Hugo reads a block of metadata at the top of each content file. The delimiters
and syntax depend on the chosen format:

* **TOML** (default, the Hugo-ecosystem preference) delimited by ``+++``
* **YAML** delimited by ``---``
* **JSON** as a bare ``{ ... }`` object (Hugo detects a leading ``{``)

Reserved Hugo keys (``title``, ``weight``, ``slug``, ``date``,
``description``, ``draft``, ``menus``, ``type``, ``layout``) are written at the
top level in a deterministic order; everything else is namespaced under a
``params`` table/object so page-specific provenance cannot collide with Hugo's
own reserved keys.

Design notes
------------
* The value space of front matter is intentionally small — strings, numbers,
  booleans, and a single nested ``params`` mapping — so a compact deterministic
  TOML emitter is shipped here rather than taking a runtime dependency on a
  third-party TOML *writer* (the stdlib has a reader, ``tomllib``, only on
  3.11+, and no writer at all). This keeps ``requires-python >= 3.8`` honest.
* YAML serialization uses :mod:`PyYAML` when available and otherwise falls back
  to the same hand-rolled emitter restricted to YAML syntax, so a page can
  always be written even if the optional dependency is absent.
* Key order is fully deterministic (reserved keys in a fixed canonical order,
  then ``params`` with its own keys sorted) so snapshot tests are stable.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping

#: Hugo-reserved keys emitted at the top level, in canonical output order.
#: Order is fixed (not alphabetical) so the most-read keys come first and
#: snapshot tests stay stable across runs and Python versions.
RESERVED_KEYS = (
    "title",
    "weight",
    "slug",
    "date",
    "description",
    "draft",
    "menus",
    "type",
    "layout",
)

#: Table/object key under which all non-reserved keys are namespaced.
PARAMS_KEY = "params"

#: Accepted format identifiers (``doxtr_hugo_front_matter``).
VALID_FORMATS = ("toml", "yaml", "json")


class FrontMatter:
    """A page's front matter: reserved top-level keys plus ``params``.

    Construct with a flat mapping of fields. Any key in :data:`RESERVED_KEYS`
    is kept at the top level; every other key is routed into the ``params``
    mapping. An explicit ``params`` mapping in the input is merged in (its keys
    do not need to avoid the reserved set — they are already namespaced).
    """

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        """Create front matter, optionally seeding it from ``data``.

        ``reserved`` holds top-level keys (those in :data:`RESERVED_KEYS`) and
        ``params`` holds the namespaced remainder. When ``data`` is given it is
        routed through :meth:`update`; otherwise both tables start empty.
        """
        self.reserved: Dict[str, Any] = {}
        self.params: Dict[str, Any] = {}
        if data:
            self.update(data)

    def update(self, data: Mapping[str, Any]) -> "FrontMatter":
        """Merge ``data`` into this front matter, routing keys by reserved-ness.

        A nested ``params`` mapping in ``data`` is merged into the params table
        wholesale; all other keys are split between reserved top-level keys and
        the params namespace.
        """
        for key, value in data.items():
            if key == PARAMS_KEY:
                if value:
                    self.params.update(value)
            elif key in RESERVED_KEYS:
                self.reserved[key] = value
            else:
                self.params[key] = value
        return self

    def set(self, key: str, value: Any) -> "FrontMatter":
        """Set a single field, routing it by reserved-ness."""
        return self.update({key: value})

    def as_ordered_dict(self) -> "Dict[str, Any]":
        """Return the full mapping in canonical, deterministic order.

        Reserved keys appear first in :data:`RESERVED_KEYS` order (only those
        actually set), followed by a ``params`` mapping whose keys are sorted.
        ``None`` values are dropped so callers can pass optional fields
        unconditionally.
        """
        out: Dict[str, Any] = {}
        for key in RESERVED_KEYS:
            if key in self.reserved and self.reserved[key] is not None:
                out[key] = self.reserved[key]
        params = {
            k: v for k, v in sorted(self.params.items()) if v is not None
        }
        if params:
            out[PARAMS_KEY] = params
        return out

    def render(self, fmt: str = "toml") -> str:
        """Serialize to a delimited front-matter block for ``fmt``.

        Returns the complete block including delimiters and a trailing newline,
        ready to prepend to a page body.
        """
        fmt = (fmt or "toml").lower()
        if fmt not in VALID_FORMATS:
            raise ValueError(
                f"unknown front-matter format {fmt!r}; "
                f"expected one of {', '.join(VALID_FORMATS)}"
            )
        data = self.as_ordered_dict()
        if fmt == "toml":
            return _render_toml(data)
        if fmt == "yaml":
            return _render_yaml(data)
        return _render_json(data)


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #

def _render_json(data: Mapping[str, Any]) -> str:
    """JSON front matter: a bare object. Hugo detects the leading ``{``.

    ``sort_keys`` is intentionally off: ``data`` already arrives in canonical
    order from :meth:`FrontMatter.as_ordered_dict`, and JSON object insertion
    order is preserved by :func:`json.dumps`.
    """
    body = json.dumps(data, indent=2, ensure_ascii=False)
    return body + "\n"


def _render_yaml(data: Mapping[str, Any]) -> str:
    """YAML front matter delimited by ``---``.

    Prefers :mod:`PyYAML` (an optional/dev dependency); when it is unavailable
    the hand-rolled emitter produces equivalent output for the small value
    space used here, so a page is always writable.
    """
    try:
        import yaml  # type: ignore
    except Exception:  # pragma: no cover - exercised only without PyYAML
        body = _emit_yaml_fallback(data)
    else:
        body = yaml.safe_dump(
            dict(data),
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        ).rstrip("\n")
    return f"---\n{body}\n---\n"


def _render_toml(data: Mapping[str, Any]) -> str:
    """TOML front matter delimited by ``+++``.

    A compact deterministic emitter handles the front-matter value space:
    scalars at the top level, then a single ``[params]`` table. Nested tables
    are emitted after all top-level scalars, as TOML requires.
    """
    lines = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, Mapping)}
    tables = {k: v for k, v in data.items() if isinstance(v, Mapping)}
    for key, value in scalars.items():
        lines.append(f"{key} = {_toml_value(value)}")
    for table, mapping in tables.items():
        lines.append(f"[{table}]")
        for key, value in mapping.items():
            lines.append(f"{key} = {_toml_value(value)}")
    body = "\n".join(lines)
    return f"+++\n{body}\n+++\n"


# --------------------------------------------------------------------------- #
# Scalar emitters (shared helpers)
# --------------------------------------------------------------------------- #

def _toml_value(value: Any) -> str:
    """Render a single scalar as a TOML value literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return toml_string(str(value))


def toml_string(text: str) -> str:
    """Quote a string as a TOML basic string with the required escapes.

    Public helper: other modules (e.g. :mod:`doxtr_hugo.site_skeleton`) need
    identical TOML string quoting when emitting ``hugo.toml``/front matter, so
    this is part of the module's supported surface rather than a private detail.
    """
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _emit_yaml_fallback(data: Mapping[str, Any], indent: int = 0) -> str:
    """Minimal YAML emitter for the front-matter value space.

    Only used when :mod:`PyYAML` is not installed. Handles scalars, one level
    of nested mapping (``params``), and sequences of scalars — which is the
    complete shape produced by :class:`FrontMatter`.
    """
    pad = "  " * indent
    lines = []
    for key, value in data.items():
        if isinstance(value, Mapping):
            lines.append(f"{pad}{key}:")
            lines.append(_emit_yaml_fallback(value, indent + 1))
        elif isinstance(value, (list, tuple)):
            lines.append(f"{pad}{key}:")
            for item in value:
                lines.append(f"{pad}  - {_yaml_scalar(item)}")
        else:
            lines.append(f"{pad}{key}: {_yaml_scalar(value)}")
    return "\n".join(line for line in lines if line != "")


def _yaml_scalar(value: Any) -> str:
    """Render a single scalar for the YAML fallback emitter."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    # Quote when the value could otherwise be misread as a non-string.
    if text == "" or text != text.strip() or text[0] in "!&*?|>%@`\"'#,[]{}":
        return toml_string(text)
    return text
