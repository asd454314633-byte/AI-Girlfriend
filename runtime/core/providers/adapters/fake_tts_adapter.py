"""FakeTTSHandler: a deterministic, model-free TTS pipeline handler.

It exists to answer one question without loading 4 GB of weights: *does a
capability selection configured in the system layer actually reach the provider
request, and does the audio/turn bookkeeping contract hold?*  It therefore walks
the exact same generic path the real adapters use —

    manifest spec -> providers.tts_client.for_turn -> delivery fragments
                  -> providers.voice_text.pronounceable_voice_segments

— and records, per call, the string it would have synthesized.  Tests assert on
that record instead of on a waveform, while the waveform it yields is a real
int16 / 16 kHz / 512-sample block stream, so the downstream audio queue,
resampler and avatar feed stay under genuine load.

Nothing here is provider-specific knowledge beyond this fixture's own identity,
which is exactly the §0 rule: provider logic lives only inside an adapter.
"""
from __future__ import annotations

import logging
from threading import Event
from typing import Any, Iterator

import numpy as np

from speech_to_speech.baseHandler import BaseHandler
from speech_to_speech.pipeline.messages import (
    AUDIO_RESPONSE_DONE,
    EndOfResponse,
    TTSInput,
)

from .. import registry
from ..tts_client import for_handler
from ..voice_text import pronounceable_voice_segments
from .recorder import record

logger = logging.getLogger(__name__)

# Same shape the real adapters emit: mono int16 at the pipeline rate, in 512-sample
# blocks (see FishTTSHandler / Qwen3TTSHandler).
PIPELINE_SR = 16000
BLOCKSIZE = 512
SAMPLES_PER_SEGMENT = 1920          # 0.12 s, long enough to be 4 real blocks
PROVIDER_ID = "test_fake"
_TONE_BY_PITCH = {"low": 180.0, "high": 720.0}


class FakeTTSHandler(BaseHandler):
    """Manifest-bound stub handler.  No model, no network, no GPU."""

    def setup(
        self,
        should_listen: Event,
        provider_id: str | None = None,
        provider_spec: dict[str, Any] | None = None,
        cancel_scope=None,
        speculative_turns=None,
        **_kwargs: Any,
    ) -> None:
        self.should_listen = should_listen
        self.cancel_scope = cancel_scope
        self.speculative_turns = speculative_turns
        # Identity arrives from the manifest through the registry factory; the
        # module constant is only the fallback for direct instantiation in tests.
        self.provider_id = str(provider_id or (provider_spec or {}).get("id") or PROVIDER_ID)
        self.spec = provider_spec or registry.get("TTS", self.provider_id) or {}
        logger.info("FakeTTSHandler ready (provider_id=%s)", self.provider_id)

    def _tone(self, selection: dict[str, Any]) -> np.ndarray:
        base = _TONE_BY_PITCH.get(str(selection.get("pitch")), 360.0)
        if str(selection.get("emotion")) == "loud":
            base *= 1.5
        t = np.arange(SAMPLES_PER_SEGMENT, dtype=np.float32) / PIPELINE_SR
        wave = (np.sin(2 * np.pi * base * t) * 12000.0).astype(np.int16)
        return wave

    def process(self, item: TTSInput | EndOfResponse) -> Iterator[np.ndarray | bytes]:
        if isinstance(item, EndOfResponse):
            tracker = getattr(self, "speculative_turns", None)
            if tracker is not None and not tracker.is_latest_after_reopen_grace(
                item.turn_id, item.turn_revision,
            ):
                logger.debug("FakeTTS drops AUDIO_RESPONSE_DONE for stale turn %s", item.turn_id)
                return
            yield AUDIO_RESPONSE_DONE
            return

        text = (item.text or "").strip()
        if not text:
            return

        turn_delivery = for_handler(self.provider_id, item)
        segments = pronounceable_voice_segments(text, max_chars=turn_delivery.max_chars)
        spoken = [turn_delivery.apply(segment) for segment in segments]
        record(
            "tts",
            self.provider_id,
            raw=text,
            segments=segments,
            spoken=spoken,
            turn_id=item.turn_id,
            turn_revision=item.turn_revision,
            delivery_active=turn_delivery.active,
            max_chars=turn_delivery.max_chars,
            instruction=turn_delivery.instruction,
            prefix=turn_delivery.prefix,
            selection=dict(turn_delivery.mode),
            blocks=len(spoken) * -(-SAMPLES_PER_SEGMENT // BLOCKSIZE),
        )
        tone = self._tone(turn_delivery.mode)
        # The recorded strings are the assertion surface; the audio only has to be
        # shaped like a real provider's, so every segment gets the same tone.
        for _segment in spoken:
            for offset in range(0, len(tone), BLOCKSIZE):
                chunk = tone[offset: offset + BLOCKSIZE]
                if len(chunk) < BLOCKSIZE:
                    chunk = np.pad(chunk, (0, BLOCKSIZE - len(chunk)))
                yield chunk
