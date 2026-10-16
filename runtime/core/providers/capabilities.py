"""Capability vocabulary and pure capability logic.

Everything a provider is allowed to declare lives here, and the only rules that
decide what the browser renders.  No provider names appear in this module: it is
generic by construction, which is what makes the ``test_fake`` acceptance gate
possible without touching the frontend.
"""
from __future__ import annotations

from typing import Any

# Per-kind capability vocabularies.  A provider may declare any subset of its own
# kind; anything not listed is rejected at load time so a new provider cannot
# smuggle a private control into the generic layer.  Nothing in this module names a
# provider: that is what makes the ``test_fake`` acceptance gate possible without
# touching the frontend.
TTS_DELIVERY_CAPABILITIES: tuple[str, ...] = (
    "emotion",
    "style",
    "sound_effect",
    "speed",
    "pitch",
    "expressiveness",
    "custom_style",
    "voice_clone",
    "streaming",
    "continuation",
    "seed",
    "reference_cache",
)

CAPABILITY_VOCABULARY: dict[str, tuple[str, ...]] = {
    "TTS": TTS_DELIVERY_CAPABILITIES,
    "LLM": (
        "context_window",
        "recommended_context_budget",
        "tools",
        "json_mode",
        "streaming",
        "system_message",
        "multiple_system_messages",
        "language_zh",
        "persona_following",
    ),
    "STT": (
        "language_zh",
        "partial_transcripts",
        "timestamps",
        "runtime_switch",
    ),
    "AVATAR": (
        "interrupt",
        "speaking_probe",
        "per_turn_attribution",
        "segment_dedupe",
        "runtime_switch",
    ),
    "TOOL": (
        "whole_disk_read",
        "writable",
        "max_results",
        "max_chars",
        "timeout_s",
    ),
}

# Runtime controls are *not* per-turn delivery capabilities; they are process- or
# file-scoped switches (e.g. the preset written to a state file).
RUNTIME_CONTROL_VOCABULARY: tuple[str, ...] = ("delivery_preset",)

SUPPORTED_TYPES = ("enum", "text", "bool", "number")


def capability_ids(kind: str = "TTS") -> list[str]:
    return list(CAPABILITY_VOCABULARY.get(kind.upper(), ()))


def is_known(name: str, kind: str | None = None) -> bool:
    if kind is None:
        return any(name in row for row in CAPABILITY_VOCABULARY.values())
    return name in CAPABILITY_VOCABULARY.get(kind.upper(), ())


def supported(capabilities: dict[str, Any], name: str) -> bool:
    entry = capabilities.get(name)
    return bool(isinstance(entry, dict) and entry.get("supported"))


def enum_values(capabilities: dict[str, Any], name: str) -> list[dict[str, str]]:
    entry = capabilities.get(name) or {}
    values = entry.get("allowed_values") if isinstance(entry, dict) else None
    return [dict(item) for item in values or [] if isinstance(item, dict)]


def normalize(alias_map: dict[str, str], raw: Any) -> dict[str, Any]:
    """Map legacy browser storage keys onto the canonical vocabulary.

    ``effect`` -> ``sound_effect`` and ``custom`` -> ``custom_style`` existed before
    the registry did, and users still have them in localStorage.  Canonical keys win
    over aliases when both are present, and unknown keys are dropped.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {str(k): v for k, v in raw.items()}
    for legacy, canonical in alias_map.items():
        if canonical not in out and legacy in out:
            out[canonical] = out[legacy]
    cleaned: dict[str, Any] = {}
    for key, value in out.items():
        if key in alias_map:
            continue
        if isinstance(value, str) and value.strip():
            cleaned[key] = value.strip()
        elif isinstance(value, (int, float, bool)):
            cleaned[key] = value
    return cleaned


def ui_controls(spec: dict[str, Any], alias_map: dict[str, str]) -> list[dict[str, Any]]:
    """Ordered, provider-agnostic render instructions for the frontend.

    A control is rendered only when the provider declares it supported *and* gives
    it a display label.  ``bool``/``number`` capabilities are metadata (streaming,
    seed, …) and never become widgets unless they carry ``display_label_zh``.
    """
    capabilities = spec.get("capabilities") or {}
    kind = str(spec.get("provider_type") or "TTS").upper()
    vocabulary = CAPABILITY_VOCABULARY.get(kind, TTS_DELIVERY_CAPABILITIES)
    rows: list[dict[str, Any]] = []
    for name in vocabulary:
        entry = capabilities.get(name)
        if not isinstance(entry, dict) or not entry.get("supported"):
            continue
        label = entry.get("display_label_zh")
        if not label:
            continue
        kind_of_field = entry.get("type") or ("enum" if entry.get("allowed_values") else "text")
        control: dict[str, Any] = {
            "field": name,
            "label_zh": label,
            "type": kind_of_field,
            "default": entry.get("default", ""),
            "order": entry.get("ui_order", 100 + vocabulary.index(name)),
            "runtime_change_requires_reload": bool(entry.get("runtime_change_requires_reload")),
        }
        if kind_of_field == "enum":
            control["values"] = [
                {"id": str(item.get("id", "")), "label_zh": str(item.get("label_zh", item.get("id", "")))}
                for item in enum_values(capabilities, name)
                if item.get("ui_visible", True)
            ]
        else:
            control["max_length"] = int(entry.get("max_length", 160))
            control["placeholder_zh"] = entry.get("placeholder_zh", "")
        rows.append(control)
    rows.sort(key=lambda row: (row["order"], row["field"]))
    return rows


def runtime_control_specs(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Generic descriptors for file/process scoped switches (delivery presets)."""
    out: list[dict[str, Any]] = []
    for name, entry in (spec.get("runtime_controls") or {}).items():
        if not isinstance(entry, dict):
            continue
        out.append({
            "field": name,
            "label_zh": entry.get("display_label_zh", name),
            "type": "enum",
            "default": entry.get("default", ""),
            "values": [dict(item) for item in entry.get("allowed_values") or [] if isinstance(item, dict)],
            "runtime_change_requires_reload": bool(entry.get("runtime_change_requires_reload")),
        })
    out.sort(key=lambda row: row["field"])
    return out


def validate_selection(
    spec: dict[str, Any],
    raw: Any,
    alias_map: dict[str, str],
) -> tuple[dict[str, Any], list[str]]:
    """Return the selection a provider may actually honour, plus rejection reasons.

    Unknown fields, unsupported capabilities and out-of-vocabulary enum values are
    dropped instead of being passed through, so a stale browser preference can never
    change another provider's behaviour.
    """
    selection = normalize(alias_map, raw)
    kind = str(spec.get("provider_type") or "TTS").upper()
    capabilities = spec.get("capabilities") or {}
    clean: dict[str, Any] = {}
    dropped: list[str] = []
    for name, value in selection.items():
        if not is_known(name, kind):
            dropped.append(f"UNKNOWN_CAPABILITY:{name}")
            continue
        entry = capabilities.get(name)
        if not isinstance(entry, dict) or not entry.get("supported"):
            dropped.append(f"NOT_SUPPORTED:{name}")
            continue
        field_type = entry.get("type") or "enum"
        if field_type == "enum":
            allowed = {str(item.get("id")) for item in enum_values(capabilities, name)}
            if str(value) not in allowed:
                dropped.append(f"INVALID_VALUE:{name}")
                continue
            if str(value) == "":
                continue
        elif field_type == "text":
            text = str(value).strip()
            limit = int(entry.get("max_length", 160))
            if not text:
                continue
            clean[name] = text[:limit]
            continue
        clean[name] = value if field_type != "enum" else str(value)
    return clean, dropped


def defaults(spec: dict[str, Any]) -> dict[str, Any]:
    """Explicit 'default' markers, i.e. the no-op selection."""
    out: dict[str, Any] = {}
    for name, entry in (spec.get("capabilities") or {}).items():
        if isinstance(entry, dict) and entry.get("supported") and "default" in entry:
            value = entry["default"]
            if value not in ("", None):
                out[name] = value
    return out
