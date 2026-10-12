"""Qwen3-TTS with this project's delivery behaviour, as a subclass.

Why a subclass and not an edited wheel file
------------------------------------------
``speech_to_speech/TTS/qwen3_tts_handler.py`` used to carry two project edits
(``PATCH_MANIFEST.json`` -> ``modified``): the per-turn manual-voice instruction and
delimiter-aware segmentation of the coalesced text before synthesis.  Both live here
now, so the wheel file is the vendor's bytes again and ``pip install
--force-reinstall speech-to-speech`` cannot take the feature down with it.

The vendor's ``process()`` asks for the voice-clone iterator exactly once per turn::

    if self.ref_audio:
        audio_iter = self._process_voice_clone(text)

which is a single, narrow seam.  So this subclass only:

1. resolves the turn's :class:`~providers.tts_client.TurnDelivery` while the
   ``TTSInput`` is still in hand (``process``), and
2. replaces the voice-clone leaf (``_process_voice_clone``) with the segment loop that
   needs it, forwarding each segment to the vendor's generation call with the
   delivery instruction attached.

``custom_voice`` / ``voice_design`` are untouched: they inherit the vendor's path,
exactly as before.

The manifest (``providers/manifests/tts.json`` -> ``handler``) points at this class,
so no ``speech_to_speech`` module imports it -- :mod:`providers.tts_factory` does, at
handler-construction time, which is also why importing this file loads torch.

Upstream drift
--------------
``_generate_voice_clone`` below is a *copy* of the vendor's 20-line leaf with one
added kwarg.  A future vendor change to it would be silently shadowed, so
:data:`UPSTREAM_SOURCE_SHA256` records the exact source of the two vendor methods this
class depends on and :func:`_check_upstream_drift` logs a WARNING the moment they no
longer match.  That is a tripwire, not a gate: it never changes behaviour.
"""
from __future__ import annotations

import hashlib
import inspect
import logging
import json
import time
import textwrap
from pathlib import Path
from typing import Any, Iterator, Optional

from providers.tts_client import for_handler
from providers.voice_text import merge_tiny_segments, pronounceable_voice_segments
from providers.reading_strategy import (ReadingTTSInput, STRATEGY_FILE,
    SEGMENTATION_STRATEGIES, DEFAULT_SEGMENTATION_STRATEGY,
    resolve_segmentation_strategy)
from speech_to_speech.TTS.qwen3_tts_handler import Qwen3TTSHandler as _VendorQwen3TTSHandler
from speech_to_speech.pipeline.messages import TTSInput

logger = logging.getLogger(__name__)

# This adapter's own identity in providers/manifests/tts.json (class-A by definition).
PROVIDER_ID = "qwen3"

# An overlong clause has no safe semantic split; the vendor's own limit is what the
# pre-migration code warned about, kept verbatim so the log lines did not move.
OVERLONG_WARNING_CHARS = 192

# READING zero-audio boundary (2026-10-11, doc_bb4c9a14ee chunk3 "结束。"):
# the model returned zero chunks WITHOUT raising (start -> last_delta -> complete
# with audio_chunks=0 and no first_delta), the response never opened and the final
# segment was silently swallowed.  Bounded repair, READING scope only: re-generate
# the SAME original text up to ZERO_AUDIO_MAX_RETRIES extra times.  Never retry
# once any chunk was yielded (no repeated reading), re-check the reading
# generation before every retry (stop semantics preserved), and on exhaustion
# audit 'zero_audio_exhausted' + log an error -- the segment is skipped loudly,
# no fabricated speech, no unbounded loop.
ZERO_AUDIO_MAX_RETRIES = 2

# Audit sink for generation events.  Module-level so tests can redirect it away
# from the production logs/runtime/tts-generation.jsonl.
AUDIT_PATH = Path(__file__).resolve().parents[4] / 'logs/runtime/tts-generation.jsonl'

# ---------------------------------------------------------------------------
# TTS segmentation strategy (READING-internal generation strategy; NOT a
# top-level mode — the ROLEPLAY/READING/THIRD mode system is untouched).
#
#   A = production segmentation as-is (compatibility / baseline)
#   B = merge 1–2 char fragments (chapter numbers) into the adjacent sentence
#       【production default, user ruling 2026-10-04】
#   C = whole text as ONE generation (continuous narration; opt-in only)
#
# Control surface: runtime/windows/tts_segmentation_strategy.txt, one letter per
# turn (same pattern as active_tts_backend.txt).  Missing/invalid file -> B.
# Evidence: evidence/MODE_SYSTEM_V1/TTS_GRANULARITY_EXP_20261003/REPORT.md §4.5.
# ---------------------------------------------------------------------------
#: dedented source of the two vendor methods this class shadows, as they are in the
#: 0.2.11 wheel (RECORD-verified).  Recompute with ``runtime/core/venv/Scripts/python.exe
#: -c "import ...; print(_method_sha256('process'))"`` after an intentional upgrade.
UPSTREAM_SOURCE_SHA256 = {
    "process": "792ce5acb2e0f12c38be5da9f1ebb892fa3dcc21fceccd57153260ca906418f7",
    "_process_voice_clone": "9c7012d6e9d474b14b32f8e615a5d9c950c75168f31e669a4c66b74db019953c",
}


def _method_sha256(name: str) -> str:
    try:
        source = inspect.getsource(getattr(_VendorQwen3TTSHandler, name))
    except (OSError, TypeError):  # built / exec'd class
        return ""
    return hashlib.sha256(textwrap.dedent(source).encode("utf-8")).hexdigest()


def _check_upstream_drift() -> list[str]:
    """Names of vendor methods whose source changed under this subclass."""
    drifted: list[str] = []
    for name, expected in UPSTREAM_SOURCE_SHA256.items():
        actual = _method_sha256(name)
        if actual and not actual.startswith(expected[:16]):
            drifted.append(name)
    if drifted:
        logger.warning(
            "providers.adapters.qwen3_tts_adapter: upstream Qwen3TTSHandler.%s changed; "
            "this subclass shadows it, so re-diff against the wheel before trusting "
            "delivery behaviour (speech-to-speech installed version).",
            ",".join(drifted),
        )
    return drifted


class Qwen3DeliveryTTSHandler(_VendorQwen3TTSHandler):
    """Vendor Qwen3-TTS handler + manifest-declared delivery per turn."""

    def process(self, tts_input: TTSInput) -> Iterator[Any]:
        # The delivery selection lives on the pipeline message (per-response metadata
        # or the session's runtime config), so this is the last place it can be read
        # before the vendor's own coalescing turns several items into one string.
        self._turn_delivery = for_handler(PROVIDER_ID, tts_input) if isinstance(tts_input, TTSInput) else None
        self._reading_input = tts_input if isinstance(tts_input, ReadingTTSInput) else None
        try:
            for audio in super().process(tts_input):
                # A response opens only on its first delta. Stop may arrive while
                # inference runs, before any response_id exists to cancel.
                if self._reading_cancelled():
                    logger.info('READING_GENERATION_CANCELLED generation=%s',
                                self._reading_input.reading_generation)
                    return
                yield audio
        finally:
            self._turn_delivery = None
            self._reading_input = None

    def _reading_cancelled(self) -> bool:
        """True when this turn's reading generation has been superseded by a stop."""
        reading_input = getattr(self, '_reading_input', None)
        if reading_input is None or reading_input.reading_generation is None:
            return False
        from speech_to_speech.api.openai_realtime import reading_bridge
        return reading_input.reading_generation < reading_bridge.generation()

    def _process_voice_clone(self, text: str, manual_instruct: Optional[str] = None):
        delivery = getattr(self, "_turn_delivery", None)
        if delivery is None:
            # Entered outside process() (a direct call, or a vendor path that has no
            # turn): behave exactly like the vendor's handler.
            yield from super()._process_voice_clone(text)
            return

        instruct = manual_instruct or delivery.instruction
        logger.info("Qwen manual voice instruct enabled=%s", bool(instruct))
        reading_input = getattr(self, '_reading_input', None)
        # An explicit bridge-owned message is required. ROLEPLAY never reads
        # the reading authority and retains the pre-strategy production path.
        strategy = reading_input.reading_strategy if reading_input is not None else 'A'
        segments = pronounceable_voice_segments(text, max_chars=delivery.max_chars)
        if strategy == "C":
            # Continuous narration: the whole text is ONE generation (internal
            # codec streaming unchanged).  Reading checkpoints stay fine-grained.
            segments = [text]
        elif strategy == "B":
            segments = merge_tiny_segments(segments)
        # strategy "A": production segmentation as-is (compatibility/baseline)
        logger.info(
            "Qwen TTS text length=%d segments=%d strategy=%s",
            len(text), len(segments), strategy,
        )
        if any(len(segment) > OVERLONG_WARNING_CHARS for segment in segments):
            logger.warning("Qwen TTS received an overlong clause without a safe semantic split")
        for segment in segments:
            logger.info('TTS_GENERATION scope=%s strategy=%s source_chunks=%s text=%r',
                        'READING' if reading_input is not None else 'ROLEPLAY', strategy,
                        reading_input.source_chunks if reading_input is not None else [], segment)
            record = dict(timestamp=time.time(), scope='READING' if reading_input is not None else 'ROLEPLAY',
                strategy=strategy, source_chunks=reading_input.source_chunks if reading_input is not None else [], text=segment)
            audit_path = AUDIT_PATH
            def audit(event, **extra):
                try:
                    with audit_path.open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(dict(record, event=event,
                            event_mono_ns=time.perf_counter_ns(), event_at_ms=time.time_ns()/1e6,
                            **extra), ensure_ascii=False) + '\n')
                except OSError:
                    logger.exception('TTS_GENERATION_AUDIT_WRITE_FAILED')
            audit('start')
            audio_chunks = 0
            attempts = 0
            zero_cancelled = False
            max_attempts = 1 + (ZERO_AUDIO_MAX_RETRIES if reading_input is not None else 0)
            try:
                while True:
                    attempts += 1
                    generated = 0
                    for audio in self._generate_voice_clone(segment, instruct):
                        generated += 1
                        audio_chunks += 1
                        if audio_chunks == 1:
                            audit('first_delta')
                        yield audio
                    # Retry only a CLEAN zero-output attempt: once any chunk was
                    # yielded the response is open and re-generating would repeat
                    # reading.  A stop that landed while nothing was produced ends
                    # the turn here instead of re-generating stale audio.
                    if generated or attempts >= max_attempts:
                        break
                    if self._reading_cancelled():
                        zero_cancelled = True
                        audit('zero_audio_cancelled', attempts=attempts)
                        break
                    audit('zero_audio_retry', attempts=attempts)
            except Exception as exc:
                audit('failed', error=type(exc).__name__, audio_chunks=audio_chunks)
                raise
            else:
                audit('last_delta', audio_chunks=audio_chunks)
                audit('complete', audio_chunks=audio_chunks)
                if reading_input is not None and audio_chunks == 0 and not zero_cancelled:
                    audit('zero_audio_exhausted', attempts=attempts)
                    logger.error(
                        'Qwen TTS returned zero audio for a READING segment after %d '
                        'attempt(s); segment skipped without fabricated speech: %r',
                        attempts, segment)

    def _generate_voice_clone(self, text: str, instruct: Optional[str] = None):
        """The vendor's ``_process_voice_clone`` with one added kwarg on the CUDA path.

        ``instruct`` is a real parameter of ``generate_voice_clone_streaming`` in the
        pinned faster-qwen3-tts stack; the mlx path has no instruction channel (the
        vendor's own docstring says the mlx branch ignores CUDA-only modes), so it is
        left exactly as upstream wrote it.
        """
        utterance_max_new_tokens = self._estimate_max_new_tokens(text)
        if self.backend == "mlx":
            if self.xvec_only:
                logger.warning("mlx-audio Qwen3-TTS does not support xvec_only; ignoring it")
            if self.parity_mode:
                logger.info("Qwen3-TTS parity mode is CUDA-specific and is ignored on mlx-audio")

            yield from self._stream_mlx_generation(
                self.model.generate,
                label="voice_clone_mlx",
                max_tokens=utterance_max_new_tokens,
                text=text,
                ref_audio=self._prepare_mlx_ref_audio(self.ref_audio),
                ref_text=self.ref_text,
                lang_code=self.language,
            )
            return

        yield from self._stream(
            self.model.generate_voice_clone_streaming(
                text=text,
                language=self.language,
                ref_audio=self.ref_audio,
                ref_text=self.ref_text,
                xvec_only=self.xvec_only,
                chunk_size=self.streaming_chunk_size,
                max_new_tokens=utterance_max_new_tokens,
                parity_mode=self.parity_mode,
                non_streaming_mode=self.non_streaming_mode,
                instruct=instruct,
            ),
            label="voice_clone_parity" if self.parity_mode else "voice_clone",
        )


_check_upstream_drift()

__all__ = ["PROVIDER_ID", "Qwen3DeliveryTTSHandler", "UPSTREAM_SOURCE_SHA256"]
