"""Generic TTS delivery encoding.

The whole point of this module is that it never mentions a provider.  Every
provider-specific string (``<|emotion:anger|>``, ``请用温柔的语气说话。``,
``[soft and gentle voice]``) lives in the manifest as an ``emit`` value, and the
shape of the emission (prefix vs. instruction field, bracket wrapping, joiner,
split length) lives in one ``delivery`` block per provider.

Adding a TTS provider therefore means adding a manifest entry — never a new
``if backend == ...`` branch here or in the frontend.

Manifest contract consumed here (``providers[].delivery``)::

    {
      "placement": "text_prefix" | "instruction_field",
      "separator": "<joiner between emitted fragments>",
      "attach":    "<joiner between the fragment block and the spoken text>",
      "value_wrap": "[{value}]" | "{value}",
      "max_chars": <target clause length for the text splitter>,
      "text_field": "custom_style",          # which field emits its raw value
      "sanitize":  "[\\[\\]\\r\\n]+"          # regex removed from free text
    }
"""
from __future__ import annotations

import re
from typing import Any

DEFAULTS: dict[str, Any] = {
    "placement": "none",
    "separator": "",
    "attach": "",
    "value_wrap": "{value}",
    "max_chars": 160,
    "text_field": "custom_style",
    "sanitize": r"[\[\]\r\n]+",
}

_WRAP_RE = re.compile(r"\{value\}")


def delivery_spec(spec: dict[str, Any] | None) -> dict[str, Any]:
    """Merge the provider's ``delivery`` block over the generic defaults."""
    row = dict(DEFAULTS)
    raw = (spec or {}).get("delivery")
    if isinstance(raw, dict):
        row.update({key: value for key, value in raw.items() if value is not None})
    if not _WRAP_RE.search(str(row["value_wrap"])):
        row["value_wrap"] = "{value}"
    return row


def _order(spec: dict[str, Any]) -> list[str]:
    """Capability emission order, taken from ``ui_order``.

    Falls back to manifest declaration order, which is the same list in practice;
    keeping one source of truth means a UI reorder cannot silently change audio.
    """
    capabilities = (spec or {}).get("capabilities") or {}
    rows = []
    for index, (name, entry) in enumerate(capabilities.items()):
        if not isinstance(entry, dict) or not entry.get("supported"):
            continue
        rows.append((int(entry.get("ui_order", 1000 + index)), index, name))
    return [name for _order_key, _index, name in sorted(rows)]


def _emit_for(spec: dict[str, Any], field: str, value: Any) -> str:
    entry = ((spec.get("capabilities") or {}).get(field)) or {}
    if not isinstance(entry, dict):
        return ""
    if entry.get("type") == "text" or field == delivery_spec(spec)["text_field"]:
        text = str(value).strip()
        if not text:
            return ""
        pattern = str(delivery_spec(spec)["sanitize"] or "")
        return re.sub(pattern, " ", text).strip() if pattern else text
    for item in entry.get("allowed_values") or []:
        if isinstance(item, dict) and str(item.get("id")) == str(value):
            emitted = item.get("emit", "")
            return emitted if isinstance(emitted, str) else ""
    return ""


def fragments(spec: dict[str, Any], selection: dict[str, Any] | None) -> list[str]:
    """Ordered, wrapped emit strings for one validated selection."""
    if not isinstance(selection, dict):
        return []
    plan = delivery_spec(spec)
    wrap = str(plan["value_wrap"])
    out: list[str] = []
    for field in _order(spec):
        if field not in selection:
            continue
        emitted = _emit_for(spec, field, selection[field])
        if not emitted:
            continue
        out.append(wrap.replace("{value}", emitted))
    return out


def plan(spec: dict[str, Any] | None, selection: dict[str, Any] | None) -> dict[str, Any]:
    provider = spec or {}
    plan_values = delivery_spec(provider)
    parts = fragments(provider, selection)
    joined = str(plan_values["separator"]).join(parts)
    return {
        "placement": plan_values["placement"],
        "prefix": joined,
        "attach": plan_values["attach"] if joined else "",
        "max_chars": int(plan_values["max_chars"]),
        "fragments": parts,
    }


def emit_for_runtime_control(spec: dict[str, Any] | None, control: str, value: Any) -> str:
    """The emit string behind a file/process-scoped control value (e.g. a preset).

    This is what lets an adapter say "no manual choice, use the preset" without
    owning a second, provider-private prefix table.
    """
    entry = ((spec or {}).get("runtime_controls") or {}).get(control)
    if not isinstance(entry, dict) or value in (None, ""):
        return ""
    for item in entry.get("allowed_values") or []:
        if isinstance(item, dict) and str(item.get("id")) == str(value):
            emitted = item.get("emit", "")
            return emitted if isinstance(emitted, str) else ""
    return ""


def _fallback_control(spec: dict[str, Any] | None) -> str:
    row = (spec or {}).get("delivery")
    if not isinstance(row, dict):
        return ""
    value = row.get("fallback_to_runtime_control")
    return str(value) if value else ""


def prefix(
    spec: dict[str, Any] | None,
    selection: dict[str, Any] | None,
    runtime_controls: dict[str, Any] | None = None,
) -> str:
    """The resolved delivery prefix for a text-placed provider ('' otherwise).

    A manual selection always wins; only an empty manual choice falls back to the
    declared runtime control, which mirrors the pre-registry behaviour
    (``manual_prefix or PREFIX.get(emotion)``).
    """
    current = plan(spec, selection)
    if current["placement"] != "text_prefix":
        return ""
    if current["prefix"]:
        return current["prefix"]
    control = _fallback_control(spec)
    if not control:
        return ""
    return emit_for_runtime_control(spec, control, (runtime_controls or {}).get(control))


def apply_to_text(
    spec: dict[str, Any] | None,
    selection: dict[str, Any] | None,
    text: str,
    runtime_controls: dict[str, Any] | None = None,
) -> str:
    current = plan(spec, selection)
    head = prefix(spec, selection, runtime_controls)
    if not head:
        return text
    return f"{head}{current['attach']}{text}"


def instruction(spec: dict[str, Any] | None, selection: dict[str, Any] | None) -> str | None:
    """Return the out-of-band instruction string, or ``None`` for 'say nothing'."""
    current = plan(spec, selection)
    if current["placement"] != "instruction_field":
        return None
    return current["prefix"] or None


def split_target(spec: dict[str, Any] | None, fallback: int = 160) -> int:
    value = delivery_spec(spec)["max_chars"]
    try:
        return max(8, int(value))
    except (TypeError, ValueError):
        return max(8, int(fallback))


def is_supported(spec: dict[str, Any] | None, field: str) -> bool:
    entry = ((spec or {}).get("capabilities") or {}).get(field)
    return bool(isinstance(entry, dict) and entry.get("supported"))
