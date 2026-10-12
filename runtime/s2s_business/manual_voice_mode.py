"""Compatibility shim.

The project code that used to live in this module moved to
``runtime/core/providers/`` so that a plain ``pip install``/``pip install -U`` of
``speech-to-speech`` cannot take it with it.  ``import providers`` works because
``site-packages/zero_replica_providers.pth`` puts ``runtime/core`` on the path.

Provider-specific tables (``QWEN_INSTRUCTIONS`` / ``HIGGS_*`` / ``FISH_TAGS``)
are gone as Python source: the same strings now live in
``providers/manifests/tts.json`` as per-value ``emit`` fields and are applied by
the generic encoder in ``providers.delivery``.  Equivalence against the old
tables is asserted by ``runtime/audit/system_v2/tools/apply_delivery_emit.py``.

The old function signatures are kept so an un-migrated caller keeps working; they
normalize the legacy keys (``effect``/``custom``) exactly like the client path.
"""
from __future__ import annotations

from typing import Any

from providers import delivery, registry
from providers import voice_text
from providers.tts_client import client_selection

split_voice_text = voice_text.split_voice_text
pronounceable_voice_segments = voice_text.pronounceable_voice_segments

__all__ = [
    "client_selection",
    "FISH_TAGS",
    "fish_text",
    "HIGGS_EFFECTS",
    "HIGGS_EMOTIONS",
    "HIGGS_EXPRESSIVENESS",
    "HIGGS_PITCH",
    "HIGGS_SPEED",
    "HIGGS_STYLES",
    "higgs_prefix",
    "pronounceable_voice_segments",
    "QWEN_INSTRUCTIONS",
    "qwen_instruct",
    "selection",
    "split_voice_text",
]


def _spec(provider_id: str) -> dict[str, Any]:
    return registry.get("TTS", provider_id) or {}


def _clean(provider_id: str, mode: Any) -> dict[str, Any]:
    return registry.normalize_selection("TTS", provider_id, mode)


def _tagging(provider_id: str, field: str) -> dict[str, str]:
    """{value id: emitted tag} for the values of one capability that emit anything.

    Derived from the manifest instead of restated here: the long-standing
    "every Higgs tag must exist in the installed tokenizer" test now guards the
    manifest data rather than a Python copy of it.
    """
    entry = (_spec(provider_id).get("capabilities") or {}).get(field) or {}
    return {
        str(item["id"]): str(item.get("emit") or "")
        for item in entry.get("allowed_values") or []
        if isinstance(item, dict) and item.get("emit")
    }


# Legacy views kept for callers and regression tests; the manifest is the only writer.
QWEN_INSTRUCTIONS = _tagging("qwen3", "emotion")
FISH_TAGS = _tagging("fish", "emotion")
HIGGS_EMOTIONS = frozenset(_tagging("higgs", "emotion"))
HIGGS_STYLES = _tagging("higgs", "style")
HIGGS_EFFECTS = _tagging("higgs", "sound_effect")
HIGGS_SPEED = _tagging("higgs", "speed")
HIGGS_PITCH = _tagging("higgs", "pitch")
HIGGS_EXPRESSIVENESS = _tagging("higgs", "expressiveness")


def selection(item: Any, backend: str) -> dict[str, Any]:
    """Legacy entry point: this backend's own validated delivery selection."""
    mode, _dropped = client_selection(_spec(backend), item)
    return mode


def qwen_instruct(mode: Any) -> str | None:
    return delivery.instruction(_spec("qwen3"), _clean("qwen3", mode))


def higgs_prefix(mode: Any) -> str:
    return delivery.plan(_spec("higgs"), _clean("higgs", mode))["prefix"]


def fish_text(text: str, mode: Any) -> str:
    return delivery.apply_to_text(_spec("fish"), _clean("fish", mode), text)
