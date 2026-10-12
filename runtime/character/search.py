# -*- coding: utf-8 -*-
"""给工具层用的三个入口（清单里 runtime/core/providers/manifests/tool.json 已经指着这里）。

清单声明：
    local_document_search -> runtime.character.search : search_documents
    local_document_read   -> runtime.character.search : read_document_window
    character_canon_search-> runtime.character.search : search_canon

只读：这三条都不写任何东西。而且它们只查已经收进来的内容，不打开磁盘上的文件，
所以「不许整盘翻」这条规矩天然满足，不需要额外去挡。

取数上限（最多几条、最多几字）从清单里读，不写死。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import canon, context_builder, store
from runtime.memory import retrieval, service as memory_service

try:
    from . import contracts as _contracts  # type: ignore
except ImportError:  # pragma: no cover
    from runtime.core.providers import contracts as _contracts  # type: ignore

ToolResult = _contracts.ToolResult
ToolCall = _contracts.ToolCall

NO_TEXT_HINT = "还没收过任何文本，可以先导入一份。"
NO_MATCH_HINT = "没有找到对得上的内容。"


def _tool_caps(tool_id: str) -> dict[str, Any]:
    try:
        from runtime.core.providers import registry
        spec = registry.get("TOOL", tool_id) or {}
        return dict(spec.get("capabilities") or {})
    except Exception:
        return {}


def _clamp_limit(tool_id: str, limit: Any, fallback: int) -> int:
    caps = _tool_caps(tool_id)
    top = int(caps.get("max_results") or 0)
    try:
        value = int(limit or fallback)
    except (TypeError, ValueError):
        value = fallback
    value = max(1, value)
    return min(value, top) if top else value


def _clamp_chars(tool_id: str, chars: Any, fallback: int) -> int:
    caps = _tool_caps(tool_id)
    top = int(caps.get("max_chars") or 0)
    try:
        value = int(chars or fallback)
    except (TypeError, ValueError):
        value = fallback
    value = max(1, value)
    return min(value, top) if top else value


def _result(tool_id: str, started: float, *, ok: bool = True, reason: str = "",
            passages: list[Any] | None = None, text: str = "") -> ToolResult:
    rows = list(passages or [])
    body = text or "\n".join(str(row.text) for row in rows)
    return ToolResult(tool_id=tool_id, ok=ok, reason=reason, passages=rows, text=body,
                      elapsed_ms=int((time.perf_counter() - started) * 1000))


# ---------------------------------------------------------------------------
# 1) 查小说正文
# ---------------------------------------------------------------------------

def search_documents(query: str = "", character_id: str | None = None,
                     limit: Any = 5, *, db_path: Any = None,
                     turn: Any = None) -> ToolResult:
    started = time.perf_counter()
    if not str(query or "").strip():
        return _result("local_document_search", started, ok=False,
                       reason="EMPTY_QUERY", text="要先说想查什么。")
    character = character_id or store.get_active_character(db_path=db_path)
    top = _clamp_limit("local_document_search", limit, 5)
    path = Path(db_path) if db_path else store.DB_PATH
    passages = [one for one in canon.canon_passages(query, character, limit=top,
                                                    db_path=path)
                if one.source_kind == "document"]
    if not passages:
        return _result("local_document_search", started, reason=NO_MATCH_HINT,
                       passages=passages)
    return _result("local_document_search", started, passages=passages)


# ---------------------------------------------------------------------------
# 2) 按窗口读某一篇
# ---------------------------------------------------------------------------

def read_document_window(document_id: str = "", offset: Any = 0, chars: Any = 1200,
                         *, db_path: Any = None, turn: Any = None) -> ToolResult:
    started = time.perf_counter()
    if not str(document_id or "").strip():
        return _result("local_document_read", started, ok=False, reason="EMPTY_DOCUMENT",
                       text="要先说是哪一篇。")
    size = _clamp_chars("local_document_read", chars, 1200)
    window = canon.read_window(document_id, int(offset or 0), size,
                               db_path=Path(db_path) if db_path else None)
    if not window.get("ok"):
        return _result("local_document_read", started, ok=False,
                       reason=str(window.get("说明") or "读不到"), text="")
    passage = retrieval.LayeredPassage(
        source_kind="document", document_id=document_id, title=str(window.get("title") or ""),
        chapter=str(window.get("chapter") or ""), text=str(window.get("text") or ""),
        score=1.0, offset=int(window.get("offset") or 0))
    passage.layer = retrieval.LAYER_CANON
    body = str(window.get("text") or "")
    tail = "" if window.get("at_end") else f"\n（后面还有，接着读的话从第 " \
                                          f"{window.get('next_offset')} 字开始）"
    return _result("local_document_read", started, passages=[passage], text=body + tail)


# ---------------------------------------------------------------------------
# 3) 查设定（事实 + 原文，两层都给）
# ---------------------------------------------------------------------------

def search_canon(character_id: str = "", query: str = "", limit: Any = 5, *,
                 db_path: Any = None, turn: Any = None) -> ToolResult:
    started = time.perf_counter()
    if not str(query or "").strip():
        return _result("character_canon_search", started, ok=False, reason="EMPTY_QUERY",
                       text="要先说想查什么。")
    character = character_id or store.get_active_character(db_path=db_path)
    top = _clamp_limit("character_canon_search", limit, 5)
    path = Path(db_path) if db_path else store.DB_PATH
    passages = canon.canon_passages(query, character, limit=top, db_path=path)
    if not passages:
        docs = canon.list_documents(character, db_path=path)
        reason = NO_MATCH_HINT if docs else NO_TEXT_HINT
        return _result("character_canon_search", started, reason=reason, passages=[])
    return _result("character_canon_search", started, passages=passages)


# ---------------------------------------------------------------------------
# 4) 记忆检索的工具化入口（清单里那条指着 memory 服务，这里只是顺手给个同款）
# ---------------------------------------------------------------------------

def search_memories(query: str = "", character_id: str | None = None, limit: Any = 6,
                    *, db_path: Any = None, turn: Any = None) -> ToolResult:
    started = time.perf_counter()
    character = character_id or store.get_active_character()
    top = _clamp_limit("memory_search", limit, 6)
    path = Path(db_path) if db_path else memory_service.DB_PATH
    passages = memory_service.search(query, character, limit=top, db_path=path)
    return _result("memory_search", started, passages=passages,
                   reason="" if passages else NO_MATCH_HINT)


# ---------------------------------------------------------------------------
# 一次查全部（给还没接工具、只想把话接住的场景）
# ---------------------------------------------------------------------------

def search_all(query: str = "", character_id: str | None = None, *, limit: int = 6,
               weights: dict[str, float] | None = None,
               character_db_path: Any = None, memory_db_path: Any = None,
               turn: Any = None) -> list[Any]:
    """设定 + 记忆一次查完，分数已经打通排序。"""
    character = character_id or store.get_active_character(db_path=character_db_path)
    char_db = Path(character_db_path) if character_db_path else store.DB_PATH
    mem_db = Path(memory_db_path) if memory_db_path else memory_service.DB_PATH
    top = max(1, min(int(limit or 6), 12))
    requests = [
        retrieval.LayerRequest(retrieval.LAYER_CANON, char_db,
                               canon.canon_fact_spec(character), top),
        retrieval.LayerRequest(retrieval.LAYER_INTERACTION, mem_db,
                               memory_service.memory_spec(character), top),
    ]
    found = retrieval.search_layers(query, requests,
                                    weights=weights or {retrieval.LAYER_CANON: 1.0,
                                                        retrieval.LAYER_INTERACTION: 1.0},
                                    limit=top)
    # 小说原文那一层单独补进来：它要 JOIN 出处表，通用内核里没有特例 JOIN
    extra = [one for one in canon.canon_passages(query, character, limit=top, db_path=char_db)
             if one.source_kind == "document"]
    merged = {(one.document_id or one.memory_id, one.text[:24]): one
              for one in found}
    for item in extra:
        key = (item.document_id, item.text[:24])
        if key in merged:
            continue
        merged[key] = item
    rows = sorted(merged.values(), key=lambda one: one.score, reverse=True)
    return rows[:top]


__all__ = ["search_documents", "read_document_window", "search_canon", "search_memories",
           "search_all", "context_builder"]
