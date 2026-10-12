"""What a TTS adapter needs from one turn: the client's own delivery selection.

The three handlers used to each import a provider-named helper
(``qwen_instruct`` / ``higgs_prefix`` / ``fish_text``) and a provider-named
lookup (``selection(item, "fish")``).  Both are replaced by this single facade:
it identifies "which provider am I" from the manifest spec, validates the client
payload against that provider's declared capabilities, and hands the emission
shape to :mod:`providers.delivery`.

A new adapter therefore calls :func:`for_turn` and writes no branch anywhere.
"""
from __future__ import annotations

import logging
from typing import Any

from . import delivery, registry

logger = logging.getLogger(__name__)


def client_selection(spec: dict[str, Any] | None, item: Any) -> tuple[dict[str, Any], list[str]]:
    """Read *this* provider's manual delivery selection off a pipeline message.

    The browser sends ``{"tts_backend": <id>, "manual_voice_mode": {...}}`` either
    as per-response metadata or as a session field.  A payload tagged for another
    backend is inert by construction — that check used to be a string compare in
    each handler.
    """
    if not isinstance(spec, dict):
        return {}, ["NO_SPEC"]
    my_id = str((spec.get("handler") or {}).get("cli_flag_value") or spec.get("id") or "")
    response = getattr(item, "response", None)
    metadata = getattr(response, "metadata", None) if response is not None else None
    config = getattr(item, "runtime_config", None)
    session = getattr(config, "session", None) if config is not None else None
    extra = getattr(session, "model_extra", None) if session is not None else None
    source = metadata if isinstance(metadata, dict) and "manual_voice_mode" in metadata else extra
    if not isinstance(source, dict):
        return {}, []
    if str(source.get("tts_backend") or "") != my_id:
        return {}, []
    raw = source.get("manual_voice_mode")
    if not isinstance(raw, dict):
        return {}, []
    clean, dropped = registry.validate_selection("TTS", spec.get("id"), raw)
    if dropped:
        logger.info("Delivery selection dropped for %s: %s", my_id, ",".join(dropped))
    return clean, dropped


def runtime_controls(spec: dict[str, Any] | None) -> dict[str, Any]:
    """File/process-scoped control values this provider declares, read from disk."""
    out: dict[str, Any] = {}
    for name in (spec or {}).get("runtime_controls") or {}:
        out[name] = registry.runtime_control_value("TTS", (spec or {}).get("id"), name)
    return out


class TurnDelivery:
    """One turn's resolved delivery decision for one provider."""

    __slots__ = ("spec", "mode", "_controls")

    def __init__(self, spec: dict[str, Any] | None, mode: dict[str, Any]) -> None:
        self.spec = spec
        self.mode = mode
        self._controls: dict[str, Any] | None = None

    @property
    def active(self) -> bool:
        return bool(self.mode)

    @property
    def max_chars(self) -> int:
        return delivery.split_target(self.spec)

    @property
    def instruction(self) -> str | None:
        return delivery.instruction(self.spec, self.mode)

    @property
    def controls(self) -> dict[str, Any]:
        """Read lazily: only a turn that emits nothing needs the file-scoped preset."""
        if self._controls is None:
            self._controls = runtime_controls(self.spec)
        return self._controls

    @property
    def prefix(self) -> str:
        """Resolved text prefix, manual selection first and preset as fallback."""
        return delivery.prefix(self.spec, self.mode, self.controls)

    def apply(self, text: str) -> str:
        return delivery.apply_to_text(self.spec, self.mode, text, self.controls)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TurnDelivery({(self.spec or {}).get('id')}, {self.mode})"


def for_turn(spec: dict[str, Any] | None, item: Any) -> TurnDelivery:
    mode, _dropped = client_selection(spec, item)
    return TurnDelivery(spec, mode)


def for_handler(provider_id: str, item: Any) -> TurnDelivery:
    """The adapter-side entry point: 'I am this provider, resolve my turn'.

    ``provider_id`` is only ever the adapter's own identity — which is class-A
    coupling by definition.  Passing the active selection instead would let a
    stale state file make one provider emit another provider's tags.
    """
    return for_turn(registry.get("TTS", provider_id), item)
