# -*- coding: utf-8 -*-
"""每一场的滚动摘要（任务 §17 的 session summary，也是 §19「不许每轮复述」的替代方案）。

为什么需要它：
  * 上下文里只有原始对话时，模型最容易犯的毛病就是把刚才说过的话再总结一遍给用户听；
  * 原始对话又贵又占位置。所以这里做一份「系统自己看的」滚动纪要：只留最近几轮的
    要点，更早的折成一行。模型看到的是纪要，不需要每轮复述。

三条规矩：
  1. 摘要是**规则抽取**，不叫模型、不联网、不占显存；将来要换成模型摘要，只需要把
     :func:`_condense` 换成走统一契约的适配器调用，本模块的对外接口一个字都不变；
  2. 摘要与「长期记忆」是两回事：它随 clear_session 一起消失，永远不会被 search()
     当成事实召回；
  3. 条数、每行字数、折叠后总长度都写在同目录 ``summary_rules.json`` 里（数据），
     不散在 if 里。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import retrieval, service

HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "summary_rules.json"

_CLAUSE = re.compile(r"[。！？!?；;\n]")
_TRAILING = " 　。\t"

_FALLBACK_RULES: dict[str, Any] = {
    "keep_recent_turns": 8,
    "line_max_chars": 46,
    "folded_max_chars": 260,
    "total_max_chars": 520,
    "drop_shorter_than_chars": 4,
    "speaker_labels": {"user": "他说", "assistant": "她说"},
}

_rules_cache: dict[str, Any] | None = None


def rules(*, refresh: bool = False) -> dict[str, Any]:
    """读规则；文件缺字段就逐字段回落到内置默认，不会半套。"""
    global _rules_cache
    if _rules_cache is not None and not refresh:
        return _rules_cache
    merged = dict(_FALLBACK_RULES)
    if RULES_PATH.exists():
        try:
            loaded = json.loads(RULES_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                merged.update(loaded)
        except (OSError, ValueError):
            pass
    _rules_cache = merged
    return merged


# ---------------------------------------------------------------------------
# 抽取
# ---------------------------------------------------------------------------

def condense(text: str, *, role: str = "user", max_chars: int | None = None) -> str:
    """把一句话压成一行要点：取第一个能独立成句的片段，不重写成别的意思。"""
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    if not body:
        return ""
    limit = int(max_chars if max_chars is not None else rules()["line_max_chars"])
    pieces = [p.strip(_TRAILING) for p in _CLAUSE.split(body)]
    pieces = [p for p in pieces if len(p) >= 3]
    line = pieces[0] if pieces else body[:limit]
    if len(line) > limit:
        cut = line[:limit]
        for mark in ("，", "、", " "):
            index = cut.rfind(mark)
            if index >= limit * 0.5:
                cut = cut[:index]
                break
        line = cut.strip(_TRAILING) + "…"
    label = str(rules()["speaker_labels"].get(role) or ("他说" if role == "user" else "她说"))
    return f"{label}{line}；" if line else ""


def _clause_is_small_talk(text: str) -> bool:
    """寒暄与当下指令不进摘要：它们既不是事实，也不该被下一轮当成事实。"""
    body = (text or "").strip()
    if len(retrieval.normalize_text(body)) < int(rules()["drop_shorter_than_chars"]):
        return True
    from . import extractor      # 延迟导入：两者互相引用

    label = extractor.reject_reason(body)
    return label in {"寒暄", "没说事", "当下指令", "空"}


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------

def current(character_id: str | None, session_id: str, *,
            db_path: Any = None) -> dict[str, Any]:
    conn, mine = service._read_conn(db_path)
    try:
        if conn is None:
            return {"text": "", "turn_count": 0, "from_turn": 0, "to_turn": 0,
                    "updated_at": "", "session_id": session_id}
        row = conn.execute("SELECT * FROM session_summary WHERE character_id = ?"
                           " AND session_id = ?",
                           (character_id or "default", session_id or "")).fetchone()
        if row is None:
            return {"text": "", "turn_count": 0, "from_turn": 0, "to_turn": 0,
                    "updated_at": "", "session_id": session_id}
        return {"text": str(row["text"]), "turn_count": int(row["turn_count"]),
                "from_turn": int(row["from_turn"]), "to_turn": int(row["to_turn"]),
                "updated_at": str(row["updated_at"]), "session_id": session_id}
    finally:
        if mine:
            conn.close()


def observe(character_id: str | None, session_id: str, turn_id: int, *,
            user_text: str = "", reply_text: str = "",
            db_path: Any = None) -> dict[str, Any]:
    """一轮结束后调用：把这轮的要点并进这一场的滚动摘要。"""
    character = character_id or "default"
    session = session_id or ""
    settings = rules()
    lines: list[str] = []
    for text, role in ((user_text, "user"), (reply_text, "assistant")):
        if not (text or "").strip():
            continue
        if _clause_is_small_talk(text):
            continue
        line = condense(text, role=role)
        if line:
            lines.append(line)
    if not lines:
        return {"ok": True, "跳过": True, "text": current(character, session,
                                                          db_path=db_path)["text"]}
    conn, path = service._writable(db_path)
    try:
        previous = conn.execute("SELECT text, from_turn, to_turn, turn_count"
                                " FROM session_summary WHERE character_id = ?"
                                " AND session_id = ?", (character, session)).fetchone()
        old_lines = [one for one in str(previous["text"] or "").split("\n") if one] \
            if previous is not None else []
        keep = int(settings["keep_recent_turns"])
        folded = ""
        if len(old_lines) + len(lines) > keep:
            overflow = old_lines[:max(len(old_lines) + len(lines) - keep, 0)]
            old_lines = old_lines[len(overflow):]
            folded = _fold(overflow, int(settings["folded_max_chars"]))
        merged = ([folded] if folded else []) + old_lines + lines
        text = _fit("\n".join(merged), int(settings["total_max_chars"]))
        stamp = service.now_iso()
        from_turn = int(previous["from_turn"]) if previous is not None and previous["from_turn"] \
            else int(turn_id or 0)
        turn_count = (int(previous["turn_count"]) if previous is not None else 0) + 1
        with conn:
            conn.execute(
                "INSERT INTO session_summary (character_id, session_id, text, from_turn,"
                " to_turn, turn_count, updated_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(character_id, session_id) DO UPDATE SET"
                " text=excluded.text, from_turn=excluded.from_turn,"
                " to_turn=MAX(to_turn, excluded.to_turn),"
                " turn_count=excluded.turn_count, updated_at=excluded.updated_at",
                (character, session, text, from_turn, int(turn_id or 0), turn_count, stamp))
        return {"ok": True, "text": text, "turn_count": turn_count,
                "from_turn": from_turn, "to_turn": int(turn_id or 0),
                "updated_at": stamp, "字符数": len(text),
                "说明": f"这一场已经聊到第 {int(turn_id or 0)} 轮，纪要 {len(text)} 字。"}
    finally:
        conn.close()


def _fold(lines: list[str], max_chars: int) -> str:
    """更早的轮次折成一行，只保留一个总长度上限，越旧的越短。"""
    body = " ".join(one.rstrip("；;") for one in lines if one)
    body = re.sub(r"\s+", " ", body).strip()
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "…"
    return f"更早：{body}；" if body else ""


def _fit(text: str, max_chars: int) -> str:
    lines = [one for one in text.split("\n") if one]
    while len("\n".join(lines)) > max_chars and len(lines) > 1:
        lines.pop(0)     # 折不下的先丢最老的，永远保住最近几轮
    body = "\n".join(lines)
    return body if len(body) <= max_chars else body[-max_chars:]


def list_sessions(character_id: str | None = None, *, limit: int = 20,
                  db_path: Any = None) -> list[dict[str, Any]]:
    conn, mine = service._read_conn(db_path)
    try:
        if conn is None:
            return []
        clauses, args = [], []
        if character_id:
            clauses.append("character_id = ?")
            args.append(character_id)
        sql = "SELECT * FROM session_summary"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(max(1, int(limit)))
        return [{"character_id": str(r["character_id"]), "session_id": str(r["session_id"]),
                 "text": str(r["text"]), "turn_count": int(r["turn_count"]),
                 "from_turn": int(r["from_turn"]), "to_turn": int(r["to_turn"]),
                 "updated_at": str(r["updated_at"])} for r in conn.execute(sql, args)]
    finally:
        if mine:
            conn.close()


def clear(character_id: str | None, session_id: str | None = None, *,
          db_path: Any = None) -> int:
    """清掉这一场的纪要。session_id 为 None 时清这个人所有场的。"""
    conn, _path = service._writable(db_path)
    try:
        character = character_id or "default"
        clauses = ["character_id = ?"]
        args: list[Any] = [character]
        if session_id is not None:
            clauses.append("session_id = ?")
            args.append(session_id)
        with conn:
            cur = conn.execute("DELETE FROM session_summary WHERE "
                               + " AND ".join(clauses), args)
        return int(cur.rowcount or 0)
    finally:
        conn.close()


__all__ = ["rules", "condense", "observe", "current", "list_sessions", "clear"]
