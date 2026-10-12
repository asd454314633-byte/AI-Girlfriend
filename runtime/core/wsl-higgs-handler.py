"""Higgs v3 Studio local API adapter for the existing 16 kHz audio pipeline."""

from __future__ import annotations

import io
import json
import logging
import wave
from pathlib import Path
from threading import Event
from time import perf_counter
from typing import Iterator

import httpx
import numpy as np
from scipy.signal import resample_poly

from speech_to_speech.baseHandler import BaseHandler
from speech_to_speech.pipeline.messages import AUDIO_RESPONSE_DONE, EndOfResponse, TTSInput

logger = logging.getLogger(__name__)
API = "http://127.0.0.1:7077"
KEY_FILE = Path(os.environ.get("HIGGS_API_KEY_FILE", str(Path.home() / ".higgs_api_key")))
EMOTION_FILE = Path(os.environ.get("HIGGS_EMOTION_FILE", "runtime/llm/higgs_emotion.json"))
EVIDENCE = Path(os.environ.get("HIGGS_EVIDENCE_DIR", "tts_candidates/higgs_v3_q4/evidence"))
LISTENING_TESTS = {} # No private historical listening-test transcripts distributed.
REFERENCE_AUDIO = os.environ.get("HIGGS_REFERENCE_AUDIO", "external/higgs/reference.wav")
REFERENCE_TEXT = ""
PREFIX = {
    "normal": "",
    "excited": "<|emotion:enthusiasm|>",
    "angry": "<|emotion:anger|>",
    "shout": "<|emotion:anger|><|style:shouting|>",
    "whisper": "<|style:whispering|>",
}


class HiggsTTSHandler(BaseHandler):
    def setup(self, should_listen: Event, **_kwargs) -> None:
        self.should_listen = should_listen
        self.key = KEY_FILE.read_text(encoding="utf-8").strip()
        if len(self.key) < 8:
            raise RuntimeError("Higgs API key is missing")
        self.client = httpx.Client(timeout=httpx.Timeout(180.0, connect=5.0), trust_env=False)
        status = self.client.get(f"{API}/v1/status", headers=self._headers()).json()
        if not (status.get("engineLoaded") and status.get("modelLoaded")):
            raise RuntimeError("Higgs Studio engine/model must be loaded before starting 8765")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    def _emotion(self) -> str:
        try:
            choice = json.loads(EMOTION_FILE.read_text(encoding="utf-8-sig")).get("emotion", "auto")
        except (OSError, ValueError):
            choice = "auto"
        # Auto has no available cross-process role_state consumer; preserve normal delivery.
        return choice if choice in PREFIX else "normal"

    def process(self, item: TTSInput | EndOfResponse) -> Iterator[np.ndarray | bytes]:
        if isinstance(item, EndOfResponse):
            yield AUDIO_RESPONSE_DONE
            return
        text = item.text.strip()
        if not text:
            return
        emotion = self._emotion()
        payload = {
            "input": PREFIX.get(emotion, "") + text,
            "reference_audio_path": REFERENCE_AUDIO,
            "reference_text": REFERENCE_TEXT,
            "response_format": "wav",
            "temperature": 0.8,
            "top_k": 50,
            "max_tokens": 1024,
        }
        start = perf_counter()
        response = self.client.post(f"{API}/v1/higgs/voice-clone", headers=self._headers(), json=payload)
        response.raise_for_status()
        generation_ms = round((perf_counter() - start) * 1000)
        with wave.open(io.BytesIO(response.content), "rb") as wav:
            rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
            if rate != 24000 or channels != 1 or width != 2:
                raise ValueError(f"Unexpected Higgs WAV format: {rate} Hz, {channels} ch, {width * 8} bit")
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        if not pcm.size:
            raise ValueError("Higgs returned empty audio")
        audio = resample_poly(pcm.astype(np.float32), 2, 3)
        samples = np.clip(audio, -32768, 32767).astype(np.int16)
        duration_ms = round(len(pcm) * 1000 / rate)
        output_file = ""
        test_id = LISTENING_TESTS.get(text)
        if test_id:
            EVIDENCE.mkdir(parents=True, exist_ok=True)
            audio_file = EVIDENCE / f"{test_id}.wav"
            audio_file.write_bytes(response.content)
            output_file = str(audio_file)
            (EVIDENCE / f"{test_id}.json").write_text(json.dumps({
                "test_id": test_id,
                "ttfa_ms": generation_ms,
                "total_generation_ms": generation_ms,
                "audio_duration_ms": duration_ms,
                "rtf": generation_ms / duration_ms if duration_ms else None,
                "output_sample_rate": rate,
                "channels": channels,
                "delivery_tag_used": PREFIX.get(emotion, ""),
                "output_file": output_file,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(
            "Higgs generation emotion=%s delivery_tag=%s ttfa_ms=%d total_generation_ms=%d "
            "audio_duration_ms=%d rtf=%.3f output_sample_rate=%d channels=%d output_file=%s",
            emotion, PREFIX.get(emotion, ""), generation_ms, generation_ms, duration_ms,
            generation_ms / duration_ms if duration_ms else 0, rate, channels, output_file,
        )
        for offset in range(0, len(samples), 512):
            yield np.pad(samples[offset:offset + 512], (0, max(0, 512 - len(samples[offset:offset + 512]))))

    def cleanup(self) -> None:
        self.client.close()
