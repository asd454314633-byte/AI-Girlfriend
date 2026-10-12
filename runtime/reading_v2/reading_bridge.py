"""_RV2_READING_BRIDGE_V1 — direct TTS injection for 陪读 (read-aloud) mode.

Why this exists
---------------
Task 12 requires 原文 → Qwen3-TTS → LiveTalking with *no* LLM rewrite, so the
reader must reach the TTS handler without going through the LLM.  The realtime
pipeline already exposes exactly that seam: ``LMOutputProcessor`` writes
``TTSInput`` items into the TTS handler's ``queue_in`` (which is
``lm_response_queue``), so pushing the same message type there is the native
path rather than a bypass — nothing downstream needs to know the text didn't
come from the model.

Audio then leaves over the ordinary ``response.output_audio.delta`` path
(``encode_audio_chunk`` -> ``_ensure_response`` self-opens the response on the
first chunk), which is what ``avatar-sync.js`` intercepts and uploads to
LiveTalking as one WAV per turn.  No change to the frozen 24 kHz avatar chain.

Injected items carry ``turn_id=None``; ``SpeculativeTurnTracker`` treats a None
turn as always-latest, so the gate does not drop them.

Two properties the seam probe measured and that shape this file
---------------------------------------------------------------
1. ``_ensure_response`` sets ``st.in_response = True`` even on the implicit
   audio path, and only ``AUDIO_RESPONSE_DONE`` (produced by an
   ``EndOfResponse`` reaching the TTS handler) clears it.  So *every* stop path
   here must guarantee one ``EndOfResponse`` survives — dropping it wedges the
   session: text chat would be deferred forever and ``response.create`` would
   answer "Cannot create response while another response is in progress".
2. Cutting only ``queue_in`` leaves already-synthesised frames in
   ``output_queue`` (the probe measured ~5 s of audio arriving after stop).
   ``stop`` therefore replays the send loop's own speech-barge-in teardown,
   which flushes ``output_queue`` while preserving the audio sentinel.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

MARKER = "_RV2_READING_BRIDGE_V1"

# Task 12: only the current chunk plus a small lookahead may ever be queued.
# Enforced server-side instead of trusting the caller -- one queue item per
# TTSInput plus its EndOfResponse, so 4 items == 2 chunks of lookahead.
MAX_PENDING_ITEMS = 4

# Guards against two paragraphs synthesising at once; the TTS handler is a
# single worker, but the coalescing step in _coalesce_pending_tts_input happily
# merges queued items, so we serialise submissions instead.
_submit_lock = threading.RLock()
from providers.reading_strategy import ReadingUnitPlanner, ReadingTTSInput, resolve_segmentation_strategy
_reading_planner = ReadingUnitPlanner()

# MODE_SYSTEM_V1 · 陈旧回调闸门（审计专项 §8）。
# 真实发生过的竞态：/v1/speak 这个 POST 还在飞，用户先点了停止，/v1/speak/stop
# 先落地，随后那一段才姗姗来迟塞进 TTS —— 声音在「已停止」之后又响起来。
# 浏览器侧也一样：回答播完触发的自动续读回调可能晚于用户这次「停止」。
# 所以这里留一个单向递增的 generation：stop() 把它 +1，调用方把自己发出请求时的
# 那个号带回来，号小了就是陈旧请求，直接拒掉，一个字节都不进 TTS。
_generation = 0
_generation_lock = threading.Lock()

_stats = {"submitted": 0, "rejected": 0, "stopped": 0, "stale": 0,
          "last_error": "", "last_submit_at": ""}


def generation() -> int:
    """当前的朗读 generation。调用方发 /v1/speak 时把它带回来做陈旧判定。"""
    with _generation_lock:
        return _generation


def _bump_generation() -> int:
    global _generation
    with _generation_lock:
        _generation += 1
        return _generation


def _tts_handler(unit: Any):
    """Return the pipeline's TTS handler, or None when no unit is attached."""
    for handler in getattr(unit, "handlers", None) or []:
        if "TTS" in type(handler).__name__:
            return handler
    return None


def _pick_unit(pool: list):
    """Prefer a unit that currently owns a client session; else the first one."""
    for unit in pool:
        if getattr(unit, "session", None) is not None and _tts_handler(unit) is not None:
            return unit
    for unit in pool:
        if _tts_handler(unit) is not None:
            return unit
    return None


def speak(pool: list, text: str, language_code: str | None = None,
          generation_id: int | None = None, reading_source: dict | None = None) -> dict:
    with _submit_lock:
        return _speak(pool, text, language_code, generation_id, reading_source)

def _speak(pool: list, text: str, language_code: str | None = None,
          generation_id: int | None = None, reading_source: dict | None = None) -> dict:
    """Queue one verbatim chunk for synthesis. Returns a JSON-ready receipt.

    ``generation_id`` 是调用方发出这次请求时看到的号（来自 ``status()``）。
    它比现在的号小，说明中途发生过一次 stop —— 这一段是陈旧请求，拒掉。
    不传 = 不做陈旧判定（老调用方行为不变）。
    """
    from speech_to_speech.pipeline.messages import EndOfResponse, TTSInput

    body = (text or "").strip()
    if not body:
        _stats["rejected"] += 1
        return {"ok": False, "reason": "EMPTY_TEXT"}

    if generation_id is not None:
        try:
            if int(generation_id) < generation():
                _stats["stale"] += 1
                _stats["rejected"] += 1
                return {"ok": False, "reason": "STALE_GENERATION",
                        "sent": int(generation_id), "current": generation()}
        except (TypeError, ValueError):
            pass

    unit = _pick_unit(pool)
    if unit is None:
        _stats["rejected"] += 1
        return {"ok": False, "reason": "NO_TTS_HANDLER"}

    handler = _tts_handler(unit)
    try:
        backlog = handler.queue_in.qsize()
    except Exception:
        backlog = 0
    if backlog >= MAX_PENDING_ITEMS:
        _stats["rejected"] += 1
        return {"ok": False, "reason": "BACKPRESSURE", "queued": backlog}

    strategy = resolve_segmentation_strategy()
    plan = {'ok': True, 'pending': False, 'text': body, 'source_chunks': [], 'strategy': strategy}
    if reading_source is not None:
        if not all(k in reading_source for k in ('document_id', 'chapter_index', 'chunk_index')):
            return {'ok': False, 'reason': 'INVALID_READING_SOURCE'}
        plan = _reading_planner.submit(body, reading_source, strategy, generation())
        if not plan['ok']:
            return plan
        if plan['pending']:
            return dict(plan, generation=generation(), submitted=_stats['submitted'])
        body = plan['text']

    try:
        with _submit_lock:
            handler.queue_in.put(ReadingTTSInput(text=body, language_code=language_code,
                reading_strategy=strategy, source_chunks=plan['source_chunks']))
            handler.queue_in.put(EndOfResponse())
    except Exception as exc:  # a full/broken queue must not take the server down
        _stats["last_error"] = repr(exc)[:200]
        _stats["rejected"] += 1
        logger.warning("reading_bridge: inject failed: %r", exc)
        return {"ok": False, "reason": "INJECT_FAILED", "detail": _stats["last_error"]}

    _stats["submitted"] += 1
    _stats["last_submit_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return {
        **{k: v for k, v in plan.items() if k != 'text'},
        "ok": True,
        "unit": getattr(unit, "index", -1),
        "chars": len(body),
        "submitted": _stats["submitted"],
        "generation": generation(),
    }


def _drop_queued_chunks(handler: Any) -> tuple[int, bool]:
    """Remove not-yet-synthesised text, always leaving one EndOfResponse behind.

    EndOfResponse items and control messages are re-queued untouched; only
    ``TTSInput`` items are dropped.  When nothing left can close the response
    that is currently open, a fresh EndOfResponse is appended, which is what
    keeps ``st.in_response`` from sticking.
    """
    from speech_to_speech.pipeline.messages import EndOfResponse, TTSInput

    kept: list[Any] = []
    dropped = 0
    try:
        while True:
            item = handler.queue_in.get_nowait()
            if isinstance(item, TTSInput):
                dropped += 1
                continue
            kept.append(item)
    except queue.Empty:
        pass
    except Exception as exc:
        _stats["last_error"] = repr(exc)[:200]
        return dropped, False

    closure = any(isinstance(i, EndOfResponse) for i in kept)
    if not closure:
        kept.append(EndOfResponse())
    for item in kept:
        handler.queue_in.put(item)
    return dropped, True


def _cancel_open_response(unit: Any) -> bool:
    """Silence audio already on its way out, mirroring the speech-start path.

    Deliberately the same four steps the send loop runs when the user starts
    talking over a response: bump the cancel generation (so in-flight output is
    recognised as stale), drop the queued frames while preserving the audio
    sentinel, drop stale text events, and clear response_playing.
    """
    from speech_to_speech.api.openai_realtime.websocket_router import (
        _flush_queue,
        _keep_audio_sentinel,
        _keep_user_text_event,
    )

    sess = getattr(unit, "session", None)
    session_id = getattr(sess, "session_id", "") if sess is not None else ""
    if not session_id:
        return False
    st = unit.service._state(session_id)
    if st is None or not (st.in_response or st.response_pending):
        return False

    unit.cancel_scope.cancel()
    st.response_pending = False
    _flush_queue(unit.output_queue, preserve=_keep_audio_sentinel)
    _flush_queue(unit.text_output_queue, preserve=_keep_user_text_event)
    if unit.response_playing.is_set():
        unit.response_playing.clear()
    return True


def stop(pool: list) -> dict:
    with _submit_lock:
        _reading_planner.reset()
        return _stop(pool)

def _stop(pool: list) -> dict:
    """Stop reading: no further chunks start, current audio stops immediately.

    generation 在这里 +1（清掉音频**之后**，保证顺序可证）：此后到达的、带着旧号的
    /v1/speak 一律被当成陈旧请求拒掉，声音不会在停止之后又冒出来。
    """
    unit = _pick_unit(pool)
    if unit is None:
        # 没有可停的处理器，也要把号推进去 —— 否则在飞的那一次 speak 仍会被放行。
        new_gen = _bump_generation()
        return {"ok": False, "reason": "NO_TTS_HANDLER", "generation": new_gen}
    handler = _tts_handler(unit)
    dropped, closure = _drop_queued_chunks(handler)
    cancelled = False
    try:
        cancelled = _cancel_open_response(unit)
    except Exception as exc:  # never leave the caller hanging on a best-effort step
        _stats["last_error"] = repr(exc)[:200]
        logger.warning("reading_bridge: cancel failed: %r", exc)
    _stats["stopped"] += 1
    new_gen = _bump_generation()
    return {
        "ok": True,
        "dropped_chunks": dropped,
        "closure_queued": closure,
        "audio_flushed": cancelled,
        "generation": new_gen,
    }


def status(pool: list) -> dict:
    with _submit_lock:
        pending_expired = _reading_planner.expire()
    unit = _pick_unit(pool)
    handler = _tts_handler(unit) if unit is not None else None
    pending = 0
    if handler is not None:
        try:
            pending = handler.queue_in.qsize()
        except Exception:
            pending = -1
    sess = getattr(unit, "session", None) if unit is not None else None
    session_id = getattr(sess, "session_id", "") if sess is not None else ""
    in_response = None
    if session_id:
        try:
            st = unit.service._state(session_id)
            in_response = bool(st and (st.in_response or st.response_pending))
        except Exception:
            in_response = None
    return {
        'pending_expired': pending_expired,
        "marker": MARKER,
        "unit_attached": bool(session_id),
        "response_open": in_response,
        "tts_handler": type(handler).__name__ if handler is not None else "",
        "pending_queue_items": pending,
        "max_pending_items": MAX_PENDING_ITEMS,
        "generation": generation(),
        **_stats,
    }
