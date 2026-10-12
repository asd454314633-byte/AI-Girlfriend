"""Same-origin API for the local (non-HF-Space) deploy.

Kept separate from `server.py` so the upstream demo file stays almost untouched
and everything this fork adds for the desktop girlfriend — model switching,
陪读 — has one home. Mounted with `app.include_router(router)`; it has no state
of its own beyond what `models_admin` and `girlfriend_reading` track.

The 陪读 routes are a thin shell: document parsing, progress and the
READING_STATE block all live in `girlfriend_reading` (site-packages), because
8765's memory bridge needs the same implementation. Speaking text is *not*
done here — these routes forward the verbatim chunk to 8765's `/v1/speak`,
which is the only place that owns the TTS queue (task 12: 原文 → TTS, no LLM
rewrite).

Only ever reachable on the local deploy, where the whole box is the user's own
machine: these endpoints can stop and start processes, so they must not be
exposed on a public Space. `server.py` includes them only when the LB/Space
metering is off.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from threading import RLock

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import girlfriend_reading as reading
import models_admin
import voice_models_admin

logger = logging.getLogger("s2s.local_api")

router = APIRouter()

# The realtime server owns the TTS handler, and the page cannot call it
# directly (cross-origin), so 7865 relays. Same host, one hop, no new process.
S2S_BASE = os.environ.get("S2S_SPEAK_BASE", "http://127.0.0.1:8765").strip().rstrip("/")
READING_STRATEGY_FILE = Path(__file__).resolve().parents[2] / "windows/tts_segmentation_strategy.txt"
_reading_strategy_lock = RLock()


class ReadingStrategyRequest(BaseModel):
    strategy: str


@router.get("/api/reading/strategy")
def reading_strategy_get():
    with _reading_strategy_lock:
        try:
            requested = READING_STRATEGY_FILE.read_text(encoding="utf8").strip().upper()
        except OSError:
            requested = ""
        return {"ok": True, "strategy": requested if requested in ("A", "B", "C") else "B",
                "default": "B", "authority": "runtime/windows/tts_segmentation_strategy.txt"}


@router.post("/api/reading/strategy")
def reading_strategy_set(req: ReadingStrategyRequest):
    value = req.strategy.strip().upper()
    if value not in ("A", "B", "C"):
        raise HTTPException(status_code=400, detail="INVALID_READING_STRATEGY")
    # Serialize against the same-origin submission seam. Stop invalidates old
    # typed inputs/deltas and pending source units before publishing the choice.
    # The page separately cancels its Avatar response ledger, preserving checkpoint.
    with _reading_strategy_lock:
        receipt = _forward("post", "/v1/speak/stop")
        if not receipt.get("ok"):
            raise HTTPException(status_code=503, detail=receipt)
        pending = READING_STRATEGY_FILE.with_suffix(".txt.pending")
        try:
            pending.write_text(value + "\n", encoding="utf8")
            pending.replace(READING_STRATEGY_FILE)
        except OSError as exc:
            raise HTTPException(status_code=500, detail="STRATEGY_SAVE_FAILED") from exc
        return {**receipt, **reading_strategy_get()}


class SwitchRequest(BaseModel):
    target: str


class EmotionRequest(BaseModel):
    emotion: str


class ImportRequest(BaseModel):
    path: str | None = None
    title: str | None = None
    text: str | None = None
    document_id: str | None = None


class ProgressRequest(BaseModel):
    document_id: str
    chapter_index: int = 0
    chunk_index: int = 0
    status: str = "PAUSED"


class SpeakRequest(BaseModel):
    document_id: str
    chapter_index: int = 0
    chunk_index: int = 0
    generation: int | None = None


class PauseRequest(BaseModel):
    document_id: str
    chapter_index: int = 0
    chunk_index: int = 0
    status: str = "PAUSED"


class SummaryRequest(BaseModel):
    document_id: str
    chapter_index: int
    summary: str


@router.get("/api/models")
def models_state():
    """Registry, active model, live 8090 health and any in-flight switch."""
    return models_admin.models()


@router.post("/api/models/switch")
def models_switch(req: SwitchRequest):
    """Kick off Switch-Girlfriend-LLM.ps1 and return immediately.

    A switch means hashing a multi-gigabyte GGUF and loading a new model, so it
    is polled through GET /api/models rather than held open as one request.
    """
    result = models_admin.start_switch((req.target or "").strip())
    if not result.get("ok"):
        reason = str(result.get("reason") or "")
        status = 409 if reason in {"BUSY", "SWITCH_ALREADY_RUNNING"} else 400
        logger.info("switch refused: %s", result)
        raise HTTPException(status_code=status, detail=result)
    return result


@router.get("/api/voice-models")
def voice_models_state():
    return voice_models_admin.state()


@router.post("/api/voice-models/switch")
def voice_models_switch(req: SwitchRequest):
    result = voice_models_admin.start_switch((req.target or "").strip())
    if not result.get("ok"):
        reason = result.get("reason")
        raise HTTPException(status_code=409 if reason in {"BUSY", "SWITCH_ALREADY_RUNNING"} else 400, detail=result)
    return result


@router.post("/api/voice-models/emotion")
def voice_emotion(req: EmotionRequest):
    """Historical alias for the preset control; kept so old bookmarks keep working."""
    result = voice_models_admin.set_emotion((req.emotion or "").strip())
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


class RuntimeControlRequest(BaseModel):
    provider: str | None = None
    control: str
    value: str


@router.post("/api/voice-models/runtime-control")
def voice_runtime_control(req: RuntimeControlRequest):
    """Write any manifest-declared file/process scoped control of any provider.

    The frontend's generic control renderer posts here, so a new provider that
    declares its own ``runtime_controls`` row gets a working widget without a route,
    a validator or a frontend branch being added anywhere.
    """
    result = voice_models_admin.set_runtime_control(req.provider, req.control, req.value)
    if not result.get("ok"):
        reason = str(result.get("reason") or "")
        raise HTTPException(status_code=404 if reason == "UNKNOWN_PROVIDER" else 400, detail=result)
    return result


@router.get("/api/capabilities")
def capabilities_state(kind: str = Query("TTS")):
    """Schema-only view of one provider kind: what the browser must render, ever.

    Deliberately independent of live state — a page can fetch this once at boot and
    draw any provider, including ones that did not exist when it was written.  The
    kinds come from the registry, so adding a kind is a manifest file, not a route.
    """
    result = voice_models_admin.capabilities(kind)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


# ---- 陪读 ------------------------------------------------------------------

def _bad_request(exc: Exception):
    return HTTPException(status_code=400, detail={"reason": str(exc)[:120]})


def _forward(method: str, path: str, json_body: dict | None = None) -> dict:
    """One-shot call to 8765; nothing here stays resident between requests."""
    try:
        resp = httpx.request(method.upper(), f"{S2S_BASE}{path}", timeout=8.0, json=json_body, trust_env=False)
        body = resp.json()
    except Exception as exc:
        return {"ok": False, "reason": "SPEAK_UNREACHABLE", "detail": type(exc).__name__}
    if not isinstance(body, dict):
        return {"ok": False, "reason": "SPEAK_BAD_RESPONSE"}
    if resp.status_code >= 400 and "ok" not in body:
        return {"ok": False, "reason": f"SPEAK_HTTP_{resp.status_code}"}
    return body


@router.get("/api/reading/documents")
def reading_documents():
    try:
        return {"documents": reading.list_documents()}
    except Exception as exc:
        raise _bad_request(exc)


@router.post("/api/reading/import")
def reading_import(req: ImportRequest):
    """TXT / MD 路径，或页面粘贴的长文本（§9：第一版就这两种来源）。"""
    try:
        if (req.text or "").strip():
            return reading.import_text(req.title or "", req.text or "")
        if (req.path or "").strip():
            return reading.import_path(req.path or "")
        raise ValueError("EMPTY_IMPORT")
    except Exception as exc:
        raise _bad_request(exc)


@router.post("/api/reading/delete")
def reading_delete(req: ImportRequest):
    """删除一本书（V3_CLEANUP_API_01）：document_id 走 body。
    索引 / 原文 / 进度与摘要三处一起收；只影响这一本，不碰其他书与断点。"""
    try:
        if not (req.document_id or "").strip():
            raise ValueError("EMPTY_DOCUMENT_ID")
        return reading.delete_document(req.document_id.strip())
    except Exception as exc:
        raise _bad_request(exc)


@router.get("/api/reading/document")
def reading_document(document_id: str = Query(...)):
    try:
        return reading.document(document_id)
    except Exception as exc:
        raise _bad_request(exc)


@router.get("/api/reading/chapter")
def reading_chapter(document_id: str = Query(...), chapter: int = Query(0)):
    try:
        return reading.chapter_view(document_id, chapter)
    except Exception as exc:
        raise _bad_request(exc)


@router.post("/api/reading/progress")
def reading_progress(req: ProgressRequest):
    """暂停 / 上一段 / 下一段 / 从头继续：位置是唯一事实，朗读只是它的副作用。"""
    try:
        return reading.set_progress(
            req.document_id, req.chapter_index, req.chunk_index, req.status
        )
    except Exception as exc:
        raise _bad_request(exc)


@router.get("/api/reading/progress")
def reading_progress_get(document_id: str = Query(...)):
    try:
        return reading.get_progress(document_id)
    except Exception as exc:
        raise _bad_request(exc)


@router.post("/api/reading/speak")
def reading_speak(req: SpeakRequest):
    """把一段原文逐字交给 TTS。文本从 D 盘按偏移量取，不经改写、不进 LLM。

    这里刻意不写进度：预取下一段时页面还在读上一段，进度必须停在"正在出声的那
    一段"，否则一被打断就会跳过一句。写进度是 `/api/reading/progress` 的事。
    """
    try:
        loc = reading.chunk_location(
            req.document_id, req.chapter_index, req.chunk_index
        )
    except Exception as exc:
        raise _bad_request(exc)
    body = {"text": loc["text"], "language_code": "zh"}
    view = reading.chapter_view(req.document_id, req.chapter_index)
    body['reading_source'] = dict(document_id=req.document_id,
        chapter_index=req.chapter_index, chunk_index=req.chunk_index,
        char_start=loc.get('char_start'), char_end=loc.get('char_end'),
        is_last=req.chunk_index == len(view['chunks']) - 1)
    # Read-only source lookahead: the original B planner may join a short final
    # tail before this unit enters TTS. A/C ignore it. No extra speech submission,
    # queue slot, checkpoint write, or second audio route is introduced.
    remaining = view['chunks'][req.chunk_index + 1:]
    if 0 < len(remaining) <= 12:
        from providers.reading_strategy import short_final_tail
        following = [dict(document_id=req.document_id, chapter_index=req.chapter_index,
            chunk_index=chunk['index'], char_start=chunk['char_start'],
            char_end=chunk['char_end'], text=chunk['text'],
            is_last=chunk['index'] == len(view['chunks']) - 1) for chunk in remaining]
        tail = short_final_tail(body['reading_source'], following)
        if tail:
            body['reading_source']['following_sources'] = tail
    if req.generation is not None:
        body["generation"] = int(req.generation)   # 陈旧回调闸门（审计 §8）
    with _reading_strategy_lock:
        return {**_forward("post", "/v1/speak", body), "location": loc}


@router.post("/api/reading/pause")
def reading_pause(req: PauseRequest):
    """停掉朗读：8765 侧丢掉未合成的片段并掐掉在飞的音频，不碰转写与提交时机。

    ``status`` 区分两种停法（MODE_SYSTEM_V1）：
      * ``PAUSED``  —— 用户点暂停，或开口插话。进度留着，可以再继续；
      * ``STOPPED`` —— 用户点停止 / 说「别读了」。自动续读一律作废，
        回答播完也不许自己接着念。
    """
    receipt = _forward("post", "/v1/speak/stop")
    try:
        progress = reading.set_progress(req.document_id, req.chapter_index,
                                        req.chunk_index, (req.status or "PAUSED")[:16])
    except Exception as exc:
        raise _bad_request(exc)
    return {**receipt, "progress": progress}


@router.get("/api/reading/status")
def reading_status():
    """8765 的朗读闸门 + 本地阅读状态，供页面轮询自动续读。"""
    status = _forward("get", "/v1/speak/status")
    try:
        status["reading_state"] = reading.active_reading_state()
    except Exception as exc:
        status["reading_state_error"] = str(exc)[:120]
    return status


@router.get("/api/reading/search")
def reading_search(document_id: str = Query(...), q: str = Query(...), limit: int = Query(3)):
    """早期情节问答的兜底通道：D 盘原文关键词检索，无 embedding、无新模型。"""
    try:
        return {"hits": reading.search(document_id, q, limit)}
    except Exception as exc:
        raise _bad_request(exc)


@router.get("/api/reading/block")
def reading_block():
    """当前会注入进系统提示词的那段阅读上下文（也是 UI 的自检视图）。"""
    return {"block": reading.reading_state_block()}


@router.post("/api/reading/summary")
def reading_summary(req: SummaryRequest):
    """章节摘要——DOCUMENT_SUMMARY，不是 USER_MEMORY（§19 隔离）。"""
    try:
        reading.set_chapter_summary(req.document_id, req.chapter_index, req.summary)
    except Exception as exc:
        raise _bad_request(exc)
    return {"ok": True}
