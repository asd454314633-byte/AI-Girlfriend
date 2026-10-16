"""Fish S2 Pro adapter for the local s2.cpp HTTP worker."""

from __future__ import annotations

import json
import logging
import socket
import time
from threading import Event
from typing import Iterator

import httpx
import numpy as np
from scipy.signal import resample_poly

from speech_to_speech.baseHandler import BaseHandler
from speech_to_speech.pipeline.messages import AUDIO_RESPONSE_DONE, EndOfResponse, TTSInput

logger = logging.getLogger(__name__)
API = "http://127.0.0.1:3030/generate"
VOICE = "default"
VOICE_DIR = str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'external/fish-s2-pro/voices')
PIPELINE_SR = 16000
BLOCKSIZE = 512
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# This adapter's own identity in providers/manifests/tts.json (class-A by definition).
PROVIDER_ID = "fish"


class FishTTSHandler(BaseHandler):
    def setup(
        self,
        should_listen: Event,
        cancel_scope=None,
        speculative_turns=None,
        **_kwargs,
    ) -> None:
        self.should_listen = should_listen
        # See HiggsTTSHandler.setup: without a cancel scope an interrupted
        # generation keeps synthesising.  The registry factory now hands one to
        # every handler instead of a hand-written subset.
        self.cancel_scope = cancel_scope
        self.speculative_turns = speculative_turns
        self.client = httpx.Client(timeout=httpx.Timeout(900.0, connect=5.0), trust_env=False)
        try:
            with socket.create_connection(("127.0.0.1", 3030), timeout=2.0):
                pass
        except OSError as exc:
            self.client.close()
            raise RuntimeError("Fish S2 Pro worker is not listening on 127.0.0.1:3030") from exc

    def process(self, item: TTSInput | EndOfResponse) -> Iterator[np.ndarray | bytes]:
        if isinstance(item, EndOfResponse):
            yield AUDIO_RESPONSE_DONE
            return
        text = item.text.strip()
        if not text:
            return

        from providers.tts_client import for_handler
        from providers.voice_text import pronounceable_voice_segments

        turn_delivery = for_handler(PROVIDER_ID, item)
        segments = pronounceable_voice_segments(text, max_chars=turn_delivery.max_chars)
        logger.info("Fish TTS text length=%d segments=%d manual=%s", len(text), len(segments), turn_delivery.active)
        if any(len(segment) > 320 for segment in segments):
            logger.warning("Fish TTS received an overlong clause without a safe semantic split")
        # One native request reuses the resolved .s2voice prompt codes for all
        # semantic segments.  The worker does not expose acoustic KV continuity.
        prepared = [turn_delivery.apply(segment) for segment in segments]
        token_cap = min(4096, max(400, 10 * max(len(segment) for segment in segments)))
        logger.info("Fish TTS token budget=%d for longest segment=%d characters", token_cap, max(map(len, segments)))
        yield from self._generate_segment("\n".join(prepared), segmented=len(prepared) > 1, token_cap=token_cap)

    def _generate_segment(self, text: str, segmented: bool = False, token_cap: int = 400) -> Iterator[np.ndarray]:
        params = {
            "max_new_tokens": token_cap,
            "stream": True,
            "chunked": True,
            "output_format": "pcm_s16le",
            "low_latency": True,
        }
        if segmented:
            params["segment_sentences"] = True
            params["segment_max_chars"] = 0
            params["sentence_pause_ms"] = 80
        fields = {
            "text": (None, text),
            "voice": (None, VOICE),
            "voice_dir": (None, VOICE_DIR),
            "params": (None, json.dumps(params, ensure_ascii=False)),
        }
        started = time.perf_counter()
        # The s2.cpp chunked endpoint can finish sending its last audio chunk
        # just before its synthesis thread releases server_busy.  A following
        # segment must wait for that release instead of silently losing the
        # remainder of the reply to a transient 503.
        busy_deadline = time.monotonic() + 30.0
        while True:
            response = self.client.post(API, files=fields)
            if response.status_code != 503 or "Server busy processing another synthesis request" not in response.text:
                response.raise_for_status()
                break
            if time.monotonic() >= busy_deadline:
                raise RuntimeError("Fish worker remained busy for 30 seconds between speech segments")
            logger.info("Fish worker still releasing previous segment; retrying shortly")
            time.sleep(0.25)
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ValueError(f"Fish response exceeded {MAX_RESPONSE_BYTES} bytes")

        rate = int(response.headers.get("x-audio-sample-rate", "0"))
        encoding = response.headers.get("x-audio-encoding", "").lower()
        channels = int(response.headers.get("x-audio-channels", "1"))
        if rate <= 0 or channels != 1 or encoding not in {"pcm_s16le", ""}:
            raise ValueError(
                f"Unexpected Fish PCM format: rate={rate}, channels={channels}, encoding={encoding}"
            )
        pcm_bytes = response.content[: len(response.content) & ~1]
        if not pcm_bytes:
            raise ValueError("Fish returned empty audio")
        audio = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32)
        if rate != PIPELINE_SR:
            audio = resample_poly(audio, PIPELINE_SR, rate)
        audio = np.clip(audio, -32768, 32767).astype(np.int16)

        for offset in range(0, len(audio), BLOCKSIZE):
            chunk = audio[offset : offset + BLOCKSIZE]
            if len(chunk) < BLOCKSIZE:
                chunk = np.pad(chunk, (0, BLOCKSIZE - len(chunk)))
            yield chunk

        logger.info(
            "Fish S2 Pro generated samples=%d sample_rate=%d generation_s=%.3f",
            len(audio), PIPELINE_SR, time.perf_counter() - started,
        )

    def cleanup(self) -> None:
        self.client.close()
