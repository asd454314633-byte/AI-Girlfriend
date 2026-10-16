"""Headless Higgs Audio v3 worker backed by the Studio v0.3.0 native engine."""

from __future__ import annotations

import argparse
import ctypes
import io
import json
import logging
import os
import threading
import wave
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
STUDIO = ROOT / "tts_candidates" / "higgs_v3_q4" / "runtime" / "Higgs Audio v3 Studio 0.3.0 Portable"
ENGINE_DIR = Path(os.environ.get("HIGGS_ENGINE_DIR", STUDIO / "resources" / "engine"))
MODEL_ROOT = Path(os.environ.get("HIGGS_MODEL_ROOT", STUDIO / "models" / "higgs-q4_k_m"))
REFERENCE_AUDIO = Path(os.environ.get("HIGGS_REFERENCE_AUDIO", ROOT / "models/native/qwen3/ref.wav"))
REFERENCE_TEXT = os.environ.get('HIGGS_REFERENCE_TEXT') or ((ROOT / 'models/native/qwen3/ref.txt').read_text(encoding='utf-8').strip() if (ROOT / 'models/native/qwen3/ref.txt').exists() else '')
KEY_FILE = Path(__file__).with_name("higgs_api_key.txt")
PORT = 7077
LOG = logging.getLogger("higgs_headless")


class AudioResult(ctypes.Structure):
    _fields_ = [
        ("sample_rate", ctypes.c_int32),
        ("channels", ctypes.c_int32),
        ("sample_count", ctypes.c_size_t),
        ("samples", ctypes.POINTER(ctypes.c_float)),
        ("error", ctypes.c_void_p),
    ]


class HiggsEngine:
    def __init__(self) -> None:
        self._dll_directory = os.add_dll_directory(str(ENGINE_DIR))
        self.dll = ctypes.WinDLL(str(ENGINE_DIR / "audiocpp_engine.dll"))
        self._bind()
        self.handle = self.dll.audiocpp_create()
        if not self.handle:
            raise RuntimeError("audiocpp_create failed")
        status = self.dll.audiocpp_load_model(
            self.handle, os.fsencode(MODEL_ROOT), 2, 0, 0, b"higgs_tts", None
        )
        if status != 0:
            message = self.error()
            self.close()
            raise RuntimeError(f"audiocpp_load_model status={status}: {message}")
        if not self.dll.audiocpp_is_model_loaded(self.handle):
            self.close()
            raise RuntimeError("native engine returned success but model is not loaded")
        LOG.info("Higgs model loaded: %s", MODEL_ROOT)

    def _bind(self) -> None:
        d = self.dll
        d.audiocpp_create.argtypes = []
        d.audiocpp_create.restype = ctypes.c_void_p
        d.audiocpp_destroy.argtypes = [ctypes.c_void_p]
        d.audiocpp_destroy.restype = None
        d.audiocpp_load_model.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int32,
            ctypes.c_int32, ctypes.c_char_p, ctypes.c_char_p,
        ]
        d.audiocpp_load_model.restype = ctypes.c_int
        d.audiocpp_unload_model.argtypes = [ctypes.c_void_p]
        d.audiocpp_unload_model.restype = None
        d.audiocpp_is_model_loaded.argtypes = [ctypes.c_void_p]
        d.audiocpp_is_model_loaded.restype = ctypes.c_bool
        d.audiocpp_is_generating.argtypes = [ctypes.c_void_p]
        d.audiocpp_is_generating.restype = ctypes.c_bool
        d.audiocpp_last_error.argtypes = [ctypes.c_void_p]
        d.audiocpp_last_error.restype = ctypes.c_char_p
        d.audiocpp_generate_voice_clone.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(AudioResult),
        ]
        d.audiocpp_generate_voice_clone.restype = ctypes.c_int
        d.audiocpp_free_result.argtypes = [ctypes.POINTER(AudioResult)]
        d.audiocpp_free_result.restype = None

    def error(self) -> str:
        value = self.dll.audiocpp_last_error(self.handle) if self.handle else None
        return value.decode("utf-8", "replace") if value else "unknown native error"

    def loaded(self) -> bool:
        return bool(self.handle and self.dll.audiocpp_is_model_loaded(self.handle))

    def generate(self, text: str, options: dict[str, Any],
                 reference_audio: Path = REFERENCE_AUDIO,
                 reference_text: str = REFERENCE_TEXT) -> bytes:
        if not self.loaded():
            raise RuntimeError("Higgs model is not loaded")
        result = AudioResult()
        encoded_options = json.dumps(options, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        status = self.dll.audiocpp_generate_voice_clone(
            self.handle,
            text.encode("utf-8"),
            os.fsencode(reference_audio),
            reference_text.encode("utf-8"),
            encoded_options,
            None,
            None,
            ctypes.byref(result),
        )
        try:
            if status != 0:
                details = ctypes.string_at(result.error).decode("utf-8", "replace") if result.error else self.error()
                raise RuntimeError(f"Higgs generation status={status}: {details}")
            if result.sample_count <= 0 or not result.samples or result.channels != 1:
                raise RuntimeError("native Higgs engine returned invalid audio")
            samples = np.ctypeslib.as_array(result.samples, shape=(result.sample_count,)).copy()
            pcm = np.rint(np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setnchannels(result.channels)
                wav.setsampwidth(2)
                wav.setframerate(result.sample_rate)
                wav.writeframes(pcm.tobytes())
            return output.getvalue()
        finally:
            self.dll.audiocpp_free_result(ctypes.byref(result))

    def close(self) -> None:
        if getattr(self, "handle", None):
            try:
                self.dll.audiocpp_unload_model(self.handle)
            finally:
                self.dll.audiocpp_destroy(self.handle)
                self.handle = None
        if getattr(self, "_dll_directory", None):
            self._dll_directory.close()
            self._dll_directory = None


app = FastAPI(title="Higgs Audio v3 Headless Worker", docs_url=None, redoc_url=None)
_engine: HiggsEngine | None = None
_generation_lock = threading.Lock()


@app.on_event("startup")
def load_higgs() -> None:
    global _engine
    for path in (ENGINE_DIR / "audiocpp_engine.dll", MODEL_ROOT / "q4_k_m.gguf", REFERENCE_AUDIO, KEY_FILE):
        if not path.is_file():
            raise RuntimeError(f"required Higgs asset missing: {path}")
    if len(KEY_FILE.read_text(encoding="ascii").strip()) < 8:
        raise RuntimeError("Higgs API key missing or invalid")
    _engine = HiggsEngine()


@app.on_event("shutdown")
def unload_higgs() -> None:
    global _engine
    if _engine is not None:
        _engine.close()
        _engine = None
    LOG.info("Higgs model unloaded; worker exiting")


def require_auth(authorization: str | None) -> None:
    expected = KEY_FILE.read_text(encoding="ascii").strip()
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": bool(_engine and _engine.loaded())}


@app.get("/v1/status")
def status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_auth(authorization)
    loaded = bool(_engine and _engine.loaded())
    return {"engineLoaded": loaded, "modelLoaded": loaded, "generating": bool(_engine and _engine.dll.audiocpp_is_generating(_engine.handle))}


@app.get("/v1/models")
def models(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    require_auth(authorization)
    return {"data": [{"id": "higgs-q4_k_m", "name": "Higgs Audio v3 Q4_K_M", "path": str(MODEL_ROOT)}]}


@app.post("/v1/higgs/voice-clone")
def voice_clone(payload: dict[str, Any], authorization: str | None = Header(default=None)) -> Response:
    require_auth(authorization)
    text = str(payload.get("input", "")).strip()
    if not text:
        raise HTTPException(status_code=422, detail="input is required")
    if _engine is None or not _engine.loaded():
        raise HTTPException(status_code=503, detail="Higgs model is not loaded")
    reference_audio = Path(str(payload.get("reference_audio_path") or REFERENCE_AUDIO)).resolve()
    reference_text = str(payload.get("reference_text", REFERENCE_TEXT)).strip()
    if not reference_audio.is_file():
        raise HTTPException(status_code=422, detail="reference audio missing")
    if not reference_text:
        raise HTTPException(status_code=422, detail="reference text missing")
    options = {
        "temperature": float(payload.get("temperature", 0.8)),
        "top_p": float(payload.get("top_p", 0.8)),
        "top_k": int(payload.get("top_k", 50)),
        "max_tokens": int(payload.get("max_tokens", 1024)),
    }
    if payload.get("seed") is not None:
        options["seed"] = int(payload["seed"])
    try:
        with _generation_lock:
            LOG.info("voice-clone reference=%s text_chars=%d", reference_audio, len(reference_text))
            audio = _engine.generate(text, options, reference_audio, reference_text)
    except Exception as exc:
        LOG.exception("Higgs voice clone failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return Response(content=audio, media_type="audio/wav")


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the headless Higgs Audio v3 worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
