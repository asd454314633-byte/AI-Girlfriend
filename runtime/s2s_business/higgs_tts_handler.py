"""Higgs v3 Studio local API adapter for the existing 16 kHz audio pipeline."""

from __future__ import annotations

import io
import json
import logging
import os
import wave
import zlib
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
KEY_FILE = Path(str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'runtime/higgs/higgs_api_key.txt'))
# The delivery preset path lives in providers/manifests/tts.json (runtime_controls
# -> state_file / legacy_state_files); a second hard-coded copy is what made the
# UI and this handler disagree.
EVIDENCE = Path(str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'tts_candidates/higgs_v3_q4/evidence'))
LISTENING_TESTS = {
    "你回来啦，我刚刚还在想你今天什么时候到家。": "TEST_1_NORMAL",
    "你终于回来啦！我等你好久了，快过来让我看看！": "TEST_2_EXCITED",
    "你怎么现在才回来！我都等你这么久了！": "TEST_3_ANGRY_SHOUT",
    "过来一点，我想小声跟你说句话。": "TEST_4_WHISPER",
}
_REFERENCE_ROOT = Path(__file__).resolve().parents[2] / 'models/native/qwen3'
REFERENCE_AUDIO = os.environ.get('HIGGS_REFERENCE_AUDIO', str(_REFERENCE_ROOT / 'ref.wav'))
REFERENCE_TEXT = os.environ.get('HIGGS_REFERENCE_TEXT') or ((_REFERENCE_ROOT / 'ref.txt').read_text(encoding='utf-8').strip() if (_REFERENCE_ROOT / 'ref.txt').exists() else '')
PROVIDER_ID = "higgs"
# 音色库激活状态（AI_GIRLFRIEND_VOICE_LIBRARY_01）：7860 的 /api/voices/activate
# 写这里；值为 {"voice_id": "vc_xxxxxxxxxx"}。xiaoyan / 文件不存在 = 内置小烟参考。
# 只读，不写；参考素材本体在 runtime/voices/profiles/<id>/ref.wav。
ACTIVE_VOICE_FILE = Path(str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'runtime/windows/active_voice_higgs.json'))
VOICES_PROFILES = Path(str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'runtime/voices/profiles'))
_active_voice_cache: tuple[float, tuple[str, str]] | None = None


def _active_reference() -> tuple[str, str]:
    """当前生效的 (reference_audio_path, reference_text)，按状态文件 mtime 缓存。

    找不到激活音色 / 素材缺失 / 文件损坏时一律回退内置小烟参考——音色库的任何
    故障都不允许打断合成。"""
    global _active_voice_cache
    try:
        mtime = ACTIVE_VOICE_FILE.stat().st_mtime
    except OSError:
        if _active_voice_cache is not None:
            _active_voice_cache = None
        return REFERENCE_AUDIO, REFERENCE_TEXT
    if _active_voice_cache is not None and _active_voice_cache[0] == mtime:
        return _active_voice_cache[1]
    resolved = (REFERENCE_AUDIO, REFERENCE_TEXT)
    try:
        voice_id = str(json.loads(ACTIVE_VOICE_FILE.read_text(encoding="utf-8")).get("voice_id") or "")
    except (OSError, ValueError):
        voice_id = ""
    if voice_id and not voice_id.startswith("vc_"):
        logger.warning("active voice id %r is not a user profile; using default xiaoyan ref", voice_id)
    elif voice_id:
        ref_wav = VOICES_PROFILES / voice_id / "ref.wav"
        meta_file = VOICES_PROFILES / voice_id / "meta.json"
        try:
            ref_text = str(json.loads(meta_file.read_text(encoding="utf-8")).get("ref_text") or "")
        except (OSError, ValueError):
            ref_text = ""
        if ref_wav.exists() and ref_text:
            resolved = (str(ref_wav), ref_text)
        else:
            logger.warning("voice profile %s incomplete (wav=%s text=%s); fallback to xiaoyan",
                           voice_id, ref_wav.exists(), bool(ref_text))
    _active_voice_cache = (mtime, resolved)
    return resolved


class HiggsTTSHandler(BaseHandler):
    def setup(
        self,
        should_listen: Event,
        cancel_scope=None,
        speculative_turns=None,
        **_kwargs,
    ) -> None:
        self.should_listen = should_listen
        # BaseHandler.should_process_input can only drop a stale generation when the
        # handler holds a cancel scope.  This adapter used to be constructed without
        # one, so an interruption could not stop an in-flight synthesis.
        self.cancel_scope = cancel_scope
        self.speculative_turns = speculative_turns
        self.key = KEY_FILE.read_text(encoding="utf-8").strip()
        if len(self.key) < 8:
            raise RuntimeError("Higgs API key is missing")
        self.client = httpx.Client(timeout=httpx.Timeout(180.0, connect=5.0), trust_env=False)
        status = self.client.get(f"{API}/v1/status", headers=self._headers()).json()
        if not (status.get("engineLoaded") and status.get("modelLoaded")):
            raise RuntimeError("Higgs Studio engine/model must be loaded before starting 8765")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    def _preset(self) -> str:
        """File-scoped delivery preset, resolved through the manifest.

        Used to read one hard-coded path while the UI wrote another, which made the
        picker inert; the state-file list now belongs to the registry.
        """
        from providers import registry

        value = registry.runtime_control_value("TTS", PROVIDER_ID, "delivery_preset", "auto")
        return value if isinstance(value, str) else "auto"

    def process(self, item: TTSInput | EndOfResponse) -> Iterator[np.ndarray | bytes]:
        if isinstance(item, EndOfResponse):
            yield AUDIO_RESPONSE_DONE
            return
        text = item.text.strip()
        if not text:
            return
        preset = self._preset()
        from providers.tts_client import for_handler
        from providers.voice_text import pronounceable_voice_segments

        turn_delivery = for_handler(PROVIDER_ID, item)
        delivery_prefix = turn_delivery.prefix
        segments = pronounceable_voice_segments(text, max_chars=turn_delivery.max_chars)
        seed_key = f"{item.turn_id}:{item.turn_revision}".encode("utf-8")
        response_seed = zlib.crc32(seed_key)
        logger.info("Higgs TTS text length=%d segments=%d seed=%d", len(text), len(segments), response_seed)
        if any(len(segment) > 320 for segment in segments):
            logger.warning("Higgs TTS received an overlong clause without a safe semantic split")
        for segment in segments:
            yield from self._generate_segment(segment, delivery_prefix, preset, response_seed)

    def _generate_segment(self, text: str, delivery_prefix: str, preset: str, seed: int) -> Iterator[np.ndarray]:
        reference_audio, reference_text = _active_reference()
        logger.info("Higgs reference resolved: %s (preset=%s)", os.path.basename(reference_audio), preset)
        payload = {
            "input": delivery_prefix + text,
            "reference_audio_path": reference_audio,
            "reference_text": reference_text,
            "response_format": "wav",
            "temperature": 0.8,
            "top_k": 50,
            "max_tokens": 1024,
            "seed": seed,
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
                "delivery_tag_used": delivery_prefix,
                "output_file": output_file,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(
            "Higgs generation preset=%s delivery_tag=%s ttfa_ms=%d total_generation_ms=%d "
            "audio_duration_ms=%d rtf=%.3f output_sample_rate=%d channels=%d output_file=%s",
            preset, delivery_prefix, generation_ms, generation_ms, duration_ms,
            generation_ms / duration_ms if duration_ms else 0, rate, channels, output_file,
        )
        for offset in range(0, len(samples), 512):
            yield np.pad(samples[offset:offset + 512], (0, max(0, 512 - len(samples[offset:offset + 512]))))

    def cleanup(self) -> None:
        self.client.close()
