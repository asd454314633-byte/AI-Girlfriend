"""音色管理路由（AI_GIRLFRIEND_VOICE_LIBRARY_01 · 2026-10-15）。

独立的用户音色库：上传参考音频 → 校验/转码 → 本地 ASR 提取参考文字（可改）
→ 保存为可复用的克隆音色 → 试听 → 切换。全部数据留在本机
``runtime/voices/``，不向任何外部服务发送录音。

各 TTS 后端的真实支持面（不允许假切换，不支持就明说）：
  • higgs  ：Higgs Audio v3 Studio 的 /v1/higgs/voice-clone 每次合成都吃
             reference_audio_path + reference_text，因此支持「上传即克隆、
             无需重启」。激活 = 写 runtime/windows/active_voice_higgs.json，
             由 runtime/s2s_business/higgs_tts_handler.py 在合成时读取。
  • qwen3  ：参考音色烘焙在 8765 管线启动参数（--qwen3_tts_ref_audio / ref_text），
             属进程级。改音色必须重启语音管线，本轮不提供在线切换（明确限制，
             不伪装成功）。原小烟 ref 不受任何影响。
  • fish   ：按命名音色（.s2voice）工作，制作克隆音色需要 fish 工具链，
             本轮不提供（明确限制）。

「小烟」是受保护系统音色：只读入口，永不覆盖/删除；higgs 激活回 xiaoyan
即等价于「恢复原音色」（清空激活状态文件，handler 回退到内置小烟参考）。
删除用户音色走回收站目录，可由管理面恢复；小烟不可删。
"""

from __future__ import annotations

import json
from functools import wraps
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import wave
from pathlib import Path

import httpx
import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

logger = logging.getLogger("s2s.voice_library")

router = APIRouter()

_RUNTIME_ROOT = Path(__file__).resolve().parents[2]         # .../runtime（app -> ui -> runtime）
_PROJECT_ROOT = _RUNTIME_ROOT.parent                       # .../zero_replica
VOICES_ROOT = _RUNTIME_ROOT / "voices"
PROFILES_DIR = VOICES_ROOT / "profiles"
TRASH_DIR = VOICES_ROOT / "trash"
XIAOYAN_META = VOICES_ROOT / "xiaoyan.json"
# higgs 激活状态文件：handler 与本模块共同认定的唯一事实源。
HIGGS_ACTIVE_FILE = _RUNTIME_ROOT / "windows" / "active_voice_higgs.json"
HIGGS_KEY_FILE = _RUNTIME_ROOT / "higgs" / "higgs_api_key.txt"
HIGGS_API = "http://127.0.0.1:7077"
WHISPER_MODEL_DIR = _PROJECT_ROOT / "models" / "native" / "whisper-large-v3"
FFMPEG = os.environ.get("VOICE_LIB_FFMPEG", "ffmpeg")

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MIN_DURATION_S = 3.0
MAX_DURATION_S = 120.0
SUPPORTED_SUFFIX = (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus", ".webm")

_VOICE_ID_RE = re.compile(r"^vc_[0-9a-f]{10}$")
_TRASH_ID_RE = re.compile(r"^(vc_[0-9a-f]{10})_[0-9]{8}_[0-9]{6}(?:_[0-9a-f]{6})?$")
_asr_lock = threading.Lock()
_profile_lock = threading.RLock()


def _serialized(function):
    @wraps(function)
    def call(*args, **kwargs):
        with _profile_lock:
            return function(*args, **kwargs)
    return call


def _voice_mutation_idle():
    """No reference changes midway through a live response or preview."""
    try:
        response = httpx.get('http://127.0.0.1:8765/v1/speak/status', timeout=2, trust_env=False)
        response.raise_for_status()
        state = response.json()
        if state.get('response_open') or state.get('pending_queue_items', 0):
            return False
    except httpx.ConnectError:
        pass
    except Exception:
        raise HTTPException(status_code=503, detail={"reason": "TTS_ACTIVITY_UNKNOWN"})
    try:
        key = HIGGS_KEY_FILE.read_text(encoding='utf-8').strip()
        response = httpx.get(f'{HIGGS_API}/v1/status',headers={'Authorization':f'Bearer {key}'},timeout=2,trust_env=False)
        response.raise_for_status()
        return not response.json().get('generating', True)
    except (httpx.ConnectError, FileNotFoundError):
        return True
    except Exception:
        raise HTTPException(status_code=503, detail={"reason": "HIGGS_ACTIVITY_UNKNOWN"})


def _active_voice_id():
    try:
        return json.loads(HIGGS_ACTIVE_FILE.read_text(encoding='utf-8')).get('voice_id') or 'xiaoyan'
    except (OSError, ValueError):
        return 'xiaoyan'


class RefTextRequest(BaseModel):
    ref_text: str


class RenameRequest(BaseModel):
    name: str


class ActivateRequest(BaseModel):
    voice_id: str
    backend: str = "higgs"


class PreviewRequest(BaseModel):
    text: str = ""


# ---------------------------------------------------------------- storage

def _profile_dir(voice_id: str) -> Path:
    if not _VOICE_ID_RE.match(voice_id):
        raise ValueError("INVALID_VOICE_ID")
    return PROFILES_DIR / voice_id


def _read_meta(voice_id: str) -> dict:
    if not _VOICE_ID_RE.fullmatch(voice_id):
        raise HTTPException(status_code=400, detail={"reason": "INVALID_VOICE_ID"})
    meta_file = _profile_dir(voice_id) / "meta.json"
    try:
        return json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_meta(voice_id: str, meta: dict) -> None:
    target = _profile_dir(voice_id) / "meta.json"
    _atomic_json(target, meta)


def _atomic_json(target: Path, data: dict) -> None:
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _seed_xiaoyan() -> None:
    """系统音色「小烟」：指向两份历史参考素材，只读。文件缺失照样登记并标注。"""
    if XIAOYAN_META.exists():
        return
    VOICES_ROOT.mkdir(parents=True, exist_ok=True)
    meta = {
        "voice_id": "xiaoyan",
        "name": "默认参考音（自行提供）",
        "protected": True,
        "created_at": "2026-09-21T00:00:00",
        "higgs": {
            "ref_audio": os.environ.get('HIGGS_REFERENCE_AUDIO', str(_PROJECT_ROOT / 'models/native/qwen3/ref.wav')),
            "ref_text": os.environ.get('HIGGS_REFERENCE_TEXT') or ((_PROJECT_ROOT / 'models/native/qwen3/ref.txt').read_text(encoding='utf-8').strip() if (_PROJECT_ROOT / 'models/native/qwen3/ref.txt').exists() else ''),
        },
        "qwen3": {
            "ref_audio": str(__import__('pathlib').Path(__file__).resolve().parents[3] / 'models/native/qwen3/ref.wav'),
            "ref_text": (_PROJECT_ROOT / 'models/native/qwen3/ref.txt').read_text(encoding='utf-8').strip() if (_PROJECT_ROOT / 'models/native/qwen3/ref.txt').exists() else '',
        },
        "note": "系统音色：对应生产启动参数里的参考音，不可删除或覆盖。",
    }
    XIAOYAN_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _higgs_worker_up() -> bool:
    try:
        headers = {}
        try:
            headers["Authorization"] = f"Bearer {HIGGS_KEY_FILE.read_text(encoding='utf-8').strip()}"
        except OSError:
            pass
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            status = client.get(f"{HIGGS_API}/v1/status", headers=headers).json()
        return bool(status.get("engineLoaded") and status.get("modelLoaded"))
    except Exception:
        return False


def backend_support() -> dict:
    """每个后端能对上传音色做什么——UI 据此渲染，绝不假装支持。"""
    return {
        "higgs": {
            "clone": True,
            "switch": True,
            "requires_restart": False,
            "worker_ready": _higgs_worker_up(),
            "note_zh": "Higgs Audio v3：参考音频逐句克隆，支持上传音色在线切换，无需重启。",
        },
        "qwen3": {
            "clone": False,
            "switch": False,
            "requires_restart": True,
            "worker_ready": True,
            "note_zh": "千问 TTS：参考音色固定在语音管线启动参数中，切换音色需重启管线，本版本不提供在线切换。",
        },
        "fish": {
            "clone": False,
            "switch": False,
            "requires_restart": True,
            "worker_ready": False,
            "note_zh": "Fish S2 Pro：按命名音色库工作，制作克隆音色需要独立工具链，本版本不提供。",
        },
    }


# ---------------------------------------------------------------- list / get

@router.get("/api/voices")
def voices_list():
    _seed_xiaoyan()
    rows = []
    xiaoyan = json.loads(XIAOYAN_META.read_text(encoding="utf-8"))
    rows.append({
        **{k: v for k, v in xiaoyan.items() if k not in ("higgs", "qwen3")},
        "ref_text": xiaoyan.get("higgs", {}).get("ref_text", ""),
        "higgs_ready": os.path.exists(xiaoyan.get("higgs", {}).get("ref_audio", "")),
        "qwen3_ready": os.path.exists(xiaoyan.get("qwen3", {}).get("ref_audio", "")),
        "kind": "system",
    })
    if PROFILES_DIR.exists():
        for name in sorted(os.listdir(PROFILES_DIR)):
            if not _VOICE_ID_RE.match(name):
                continue
            meta = _read_meta(name)
            if not meta:
                continue
            rows.append({
                "voice_id": meta.get("voice_id") or name,
                "name": meta.get("name") or name,
                "created_at": meta.get("created_at") or "",
                "duration_s": meta.get("duration_s"),
                "sample_rate": meta.get("sample_rate"),
                "warnings": meta.get("warnings") or [],
                "ref_text": meta.get("ref_text") or "",
                "higgs_ready": (PROFILES_DIR / name / "ref.wav").exists(),
                "qwen3_ready": False,
                "kind": "user",
            })
    active = ""
    try:
        active = json.loads(HIGGS_ACTIVE_FILE.read_text(encoding="utf-8")).get("voice_id", "")
    except (OSError, ValueError):
        pass
    return {"voices": rows, "active_higgs": active or "xiaoyan", "trash_supported": True,
            "backend_support": backend_support()}


@router.get("/api/voices/trash")
def voices_trash():
    rows = []
    if TRASH_DIR.exists():
        for folder in sorted(TRASH_DIR.iterdir(), reverse=True):
            if not _TRASH_ID_RE.fullmatch(folder.name) or not folder.is_dir() or folder.is_symlink():
                continue
            try:
                meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows.append({"trash_id": folder.name, "voice_id": meta.get("voice_id"),
                         "name": meta.get("name") or folder.name})
    return {"voices": rows}


@router.post("/api/voices/trash/{trash_id}/restore")
@_serialized
def voice_restore(trash_id: str):
    match = _TRASH_ID_RE.fullmatch(trash_id)
    if not match:
        raise HTTPException(status_code=400, detail={"reason": "INVALID_TRASH_ID"})
    folder = TRASH_DIR / trash_id
    if not folder.is_dir() or folder.is_symlink():
        raise HTTPException(status_code=404, detail={"reason": "TRASH_NOT_FOUND"})
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail={"reason": "TRASH_METADATA_INVALID"})
    voice_id = match.group(1)
    if meta.get("voice_id") != voice_id or not (folder / "ref.wav").is_file():
        raise HTTPException(status_code=400, detail={"reason": "TRASH_INCOMPLETE"})
    dest = _profile_dir(voice_id)
    if dest.exists():
        raise HTTPException(status_code=409, detail={"reason": "RESTORE_CONFLICT"})
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(folder), str(dest))
    return {"ok": True, "voice_id": voice_id}


@router.get("/api/voices/{voice_id}/audio")
def voice_audio(voice_id: str):
    """试听参考音频本体（小烟返回 higgs 侧参考文件）。"""
    if voice_id == "xiaoyan":
        _seed_xiaoyan()
        meta = json.loads(XIAOYAN_META.read_text(encoding="utf-8"))
        path = meta.get("higgs", {}).get("ref_audio") or ""
        if path and os.path.exists(path):
            return FileResponse(path, media_type="audio/wav")
        raise HTTPException(status_code=404, detail={"reason": "REF_AUDIO_MISSING"})
    try:
        path = _profile_dir(voice_id) / "ref.wav"
    except ValueError:
        raise HTTPException(status_code=400, detail={"reason": "INVALID_VOICE_ID"})
    if not path.exists():
        raise HTTPException(status_code=404, detail={"reason": "VOICE_NOT_FOUND"})
    return FileResponse(str(path), media_type="audio/wav")


# ---------------------------------------------------------------- upload / validate

def _probe_and_convert(src: Path, dst_wav: Path) -> dict:
    """ffmpeg 统一转 24 kHz / 单声道 / 16-bit WAV（Higgs 合成侧的交付采样率），
    同时做时长 / 静音 / 削波体检。失败抛 ValueError，消息直接给用户看。"""
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
           "-ac", "1", "-ar", "24000", "-sample_fmt", "s16", str(dst_wav)]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise ValueError("音频转码失败：不是受支持的音频文件？(" +
                         proc.stderr.decode("utf-8", "replace")[-160:] + ")")
    with wave.open(str(dst_wav), "rb") as wav:
        rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
        frames = wav.getnframes()
        pcm = np.frombuffer(wav.readframes(frames), dtype="<i2").astype(np.float32)
    duration = frames / float(rate)
    if duration < MIN_DURATION_S:
        raise ValueError(f"音频太短（{duration:.1f} 秒），至少需要 {MIN_DURATION_S:.0f} 秒")
    if duration > MAX_DURATION_S:
        raise ValueError(f"音频太长（{duration:.1f} 秒），请控制在 {MAX_DURATION_S:.0f} 秒以内")
    warnings = []
    rms = float(np.sqrt(np.mean(np.square(pcm)))) if pcm.size else 0.0
    peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
    if rms < 120.0:
        raise ValueError("音频几乎全是静音，请换一段有效语音")
    if peak >= 32700.0 and float(np.mean(np.abs(pcm) >= 32700.0)) > 0.01:
        warnings.append("检测到明显削波（爆音），克隆效果可能受影响")
    return {"duration_s": round(duration, 2), "sample_rate": rate,
            "channels": channels, "warnings": warnings}


def _transcribe(wav_path: Path) -> str:
    """本地 faster-whisper（whisper-large-v3，CPU int8）：不占 GPU、不出网。"""
    if not WHISPER_MODEL_DIR.exists():
        raise ValueError("本地 whisper 模型目录缺失，无法自动提取参考文字")
    with _asr_lock:
        from faster_whisper import WhisperModel
        model = WhisperModel(str(WHISPER_MODEL_DIR), device="cpu", compute_type="int8")
        segments, _info = model.transcribe(str(wav_path), language="zh", beam_size=1,
                                           vad_filter=True)
        return "".join(seg.text for seg in segments).strip()


@router.post("/api/voices/upload")
async def voices_upload(file: UploadFile = File(...), name: str = Form(""),
                        ref_text: str = Form(""), transcribe: str = Form("true")):
    _seed_xiaoyan()
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in SUPPORTED_SUFFIX:
        raise HTTPException(status_code=400, detail={
            "reason": f"不支持的格式 {suffix}；支持：{' '.join(SUPPORTED_SUFFIX)}"})
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail={"reason": "文件超过 50MB 上限"})
    if not raw:
        raise HTTPException(status_code=400, detail={"reason": "空文件"})

    voice_id = "vc_" + uuid.uuid4().hex[:10]
    profile = _profile_dir(voice_id)
    profile.mkdir(parents=True, exist_ok=True)
    src = profile / f"upload{suffix}"
    wav_path = profile / "ref.wav"
    try:
        src.write_bytes(raw)
        probe = await run_in_threadpool(_probe_and_convert, src, wav_path)
    except ValueError as exc:
        shutil.rmtree(profile, ignore_errors=True)
        raise HTTPException(status_code=400, detail={"reason": str(exc)})
    except Exception as exc:
        shutil.rmtree(profile, ignore_errors=True)
        raise HTTPException(status_code=500, detail={"reason": f"处理失败：{type(exc).__name__}"})
    finally:
        src.unlink(missing_ok=True)

    text = (ref_text or "").strip()
    asr_used = False
    if not text and transcribe.lower() != "false":
        try:
            text = await run_in_threadpool(_transcribe, wav_path)
            asr_used = True
        except Exception as exc:
            logger.warning("ASR failed: %r", exc)
    meta = {
        "voice_id": voice_id,
        "name": (name or "").strip() or f"音色 {time.strftime('%m-%d %H:%M')}",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ref_text": text,
        "asr_transcribed": asr_used,
        "original_filename": file.filename or "",
        "local_only": True,
        **probe,
    }
    _write_meta(voice_id, meta)
    return {"ok": True, "voice": {**meta, "kind": "user"}}


# ---------------------------------------------------------------- edit / delete

@router.post("/api/voices/{voice_id}/ref_text")
@_serialized
def voice_set_ref_text(voice_id: str, req: RefTextRequest):
    text = (req.ref_text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail={"reason": "EMPTY_REF_TEXT"})
    if voice_id == "xiaoyan":
        raise HTTPException(status_code=400, detail={"reason": "PROTECTED_VOICE"})
    meta = _read_meta(voice_id)
    if not meta:
        raise HTTPException(status_code=404, detail={"reason": "VOICE_NOT_FOUND"})
    if voice_id == _active_voice_id() and not _voice_mutation_idle():
        raise HTTPException(status_code=409, detail={"reason": "VOICE_BUSY"})
    meta["ref_text"] = text[:600]
    meta["asr_transcribed"] = False
    _write_meta(voice_id, meta)
    return {"ok": True, "voice_id": voice_id, "ref_text": meta["ref_text"]}


@router.post("/api/voices/{voice_id}/rename")
@_serialized
def voice_rename(voice_id: str, req: RenameRequest):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail={"reason": "EMPTY_NAME"})
    if voice_id == "xiaoyan":
        raise HTTPException(status_code=400, detail={"reason": "PROTECTED_VOICE"})
    meta = _read_meta(voice_id)
    if not meta:
        raise HTTPException(status_code=404, detail={"reason": "VOICE_NOT_FOUND"})
    meta["name"] = name[:60]
    _write_meta(voice_id, meta)
    return {"ok": True, "voice_id": voice_id, "name": meta["name"]}


@router.delete("/api/voices/{voice_id}")
@_serialized
def voice_delete(voice_id: str):
    if voice_id == "xiaoyan":
        raise HTTPException(status_code=400, detail={"reason": "PROTECTED_VOICE"})
    try:
        profile = _profile_dir(voice_id)
    except ValueError:
        raise HTTPException(status_code=400, detail={"reason": "INVALID_VOICE_ID"})
    if not profile.exists():
        raise HTTPException(status_code=404, detail={"reason": "VOICE_NOT_FOUND"})
    if voice_id == _active_voice_id() and not _voice_mutation_idle():
        raise HTTPException(status_code=409, detail={"reason": "VOICE_BUSY"})
    # 软删除：整个目录移入回收站；若它正被 higgs 使用，先退回小烟。
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    dest = TRASH_DIR / f"{voice_id}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    shutil.move(str(profile), str(dest))
    try:
        active = json.loads(HIGGS_ACTIVE_FILE.read_text(encoding="utf-8")).get("voice_id", "")
    except (OSError, ValueError):
        active = ""
    if active == voice_id:
        HIGGS_ACTIVE_FILE.unlink(missing_ok=True)
    return {"ok": True, "voice_id": voice_id, "trash_path": str(dest)}


# ---------------------------------------------------------------- activate / preview

@router.post("/api/voices/activate")
@_serialized
def voice_activate(req: ActivateRequest):
    backend = (req.backend or "higgs").strip()
    support = backend_support().get(backend)
    if not support or not support.get("switch"):
        return {"ok": False, "reason": "BACKEND_SWITCH_UNSUPPORTED",
                "detail": (support or {}).get("note_zh", "该后端不支持在线音色切换")}
    voice_id = (req.voice_id or "").strip()
    if voice_id != "xiaoyan" and not _VOICE_ID_RE.match(voice_id):
        raise HTTPException(status_code=400, detail={"reason": "INVALID_VOICE_ID"})
    if voice_id != "xiaoyan":
        meta = _read_meta(voice_id)
        if not meta:
            raise HTTPException(status_code=404, detail={"reason": "VOICE_NOT_FOUND"})
        if not (_profile_dir(voice_id) / "ref.wav").is_file():
            raise HTTPException(status_code=400, detail={"reason": "REF_AUDIO_MISSING"})
        if not str(meta.get("ref_text") or "").strip():
            raise HTTPException(status_code=400, detail={"reason": "EMPTY_REF_TEXT"})
    if voice_id == "xiaoyan":
        # 恢复原音色 = 清空激活文件，handler 回退到内置小烟参考（不覆盖任何素材）。
        if not _voice_mutation_idle():
            raise HTTPException(status_code=409, detail={"reason": "VOICE_BUSY"})
        HIGGS_ACTIVE_FILE.unlink(missing_ok=True)
    else:
        if not _voice_mutation_idle():
            raise HTTPException(status_code=409, detail={"reason": "VOICE_BUSY"})
        HIGGS_ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(HIGGS_ACTIVE_FILE, {"voice_id": voice_id, "backend": backend})
    return {"ok": True, "backend": backend, "active_voice": voice_id or "xiaoyan"}


@router.post("/api/voices/{voice_id}/preview")
def voice_preview(voice_id: str, req: PreviewRequest):
    """短句试听：走 Higgs 的 voice-clone 接口用该音色合成一句。仅 higgs 支持。"""
    if not _higgs_worker_up():
        raise HTTPException(status_code=503, detail={
            "reason": "Higgs Studio 未运行（127.0.0.1:7077）。请先把语音模型切到 Higgs，"
                      "或启动 Higgs Studio 后再试听。"})
    if voice_id == "xiaoyan":
        _seed_xiaoyan()
        meta = json.loads(XIAOYAN_META.read_text(encoding="utf-8"))
        ref_audio = meta.get("higgs", {}).get("ref_audio") or ""
        ref_text = meta.get("higgs", {}).get("ref_text") or ""
    else:
        meta = _read_meta(voice_id)
        if not meta:
            raise HTTPException(status_code=404, detail={"reason": "VOICE_NOT_FOUND"})
        ref_audio = str(_profile_dir(voice_id) / "ref.wav")
        ref_text = meta.get("ref_text") or ""
    if not ref_audio or not os.path.exists(ref_audio):
        raise HTTPException(status_code=404, detail={"reason": "REF_AUDIO_MISSING"})
    if not ref_text:
        raise HTTPException(status_code=400, detail={
            "reason": "该音色还没有参考文字，请先填写参考文字再试听"})
    text = (req.text or "").strip() or "你好，这是我用新音色说的第一句话，很高兴见到你。"
    key = HIGGS_KEY_FILE.read_text(encoding="utf-8").strip()
    payload = {
        "input": text,
        "reference_audio_path": ref_audio,
        "reference_text": ref_text,
        "response_format": "wav",
        "temperature": 0.8,
        "top_k": 50,
        "max_tokens": 1024,
    }
    try:
        with httpx.Client(timeout=httpx.Timeout(180.0, connect=5.0), trust_env=False) as client:
            resp = client.post(f"{HIGGS_API}/v1/higgs/voice-clone",
                               headers={"Authorization": f"Bearer {key}"}, json=payload)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail={"reason": f"Higgs 合成失败：HTTP {exc.response.status_code}"})
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"reason": f"Higgs 不可达：{type(exc).__name__}"})
    from fastapi.responses import Response
    return Response(content=resp.content, media_type="audio/wav")
