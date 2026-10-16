# -*- coding: utf-8 -*-
"""长期记忆的存取与检索。

对外只有一组动作：查 / 记 / 改 / 删 / 清掉这一场 / 清掉长期 / 还原这个人物 / 导出 / 导入。

三条硬规矩：
  1. 删是「收起来」，不是抹掉：active 置 0、留 deleted_at，所以 restore_character 能还原；
  2. 每条都带 active / created_at / updated_at / source_session / confidence / user_editable，
     事后能查清「这条是谁、什么时候、被改过没有」；
  3. 聊天里说的话永远不能改写设定层（CANON），要改设定只能走设定那一侧的入口。

本模块不认得任何模型或语音提供方，不联网。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import retrieval

try:
    from . import contracts as _contracts  # type: ignore
except ImportError:  # pragma: no cover
    _contracts = None  # type: ignore

MemoryRecord = _contracts.MemoryRecord if _contracts is not None else None
ToolResult = _contracts.ToolResult if _contracts is not None else None
RetrievedPassage = _contracts.RetrievedPassage if _contracts is not None else None

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
DB_PATH = DATA_DIR / "memories.sqlite3"
SCHEMA_PATH = HERE / "schema.sql"

# 结构版本：每次加列 / 加表都要 +1，并在 _migrate() 里补一条 ALTER（任务 §13）。
# 存在库自己身上的 memory_meta 表里，不另开一个 JSON 状态文件（任务 §8：一处一个权威源）。
SCHEMA_VERSION = 2

LAYER_CANON = retrieval.LAYER_CANON
LAYER_INTERACTION = retrieval.LAYER_INTERACTION
SESSION_LAYER = "SESSION"
LONG_TERM_LAYERS = ("PROFILE", "PERSONA", "CANON", "RELATIONSHIP", "LONG_TERM_MEMORY", "SOURCE")
KINDS = ("preference", "fact", "relationship", "commitment", "goal", "shared_event", "turn")

_WRITE_LOCK = retrieval.write_lock_for(DB_PATH)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id() -> str:
    return "m" + uuid.uuid4().hex[:12]


def schema_sql() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


def init_db(db_path: Path | str | None = None) -> Path:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(schema_sql())
        _migrate(conn)
        conn.execute("INSERT OR REPLACE INTO memory_meta (key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
        conn.commit()
    finally:
        conn.close()
    return path


def _migrate(conn: sqlite3.Connection) -> None:
    """老库补表：schema.sql 全是 IF NOT EXISTS，这里只处理「加过列」的情况。"""
    have = {str(row["name"]) for row in conn.execute("PRAGMA table_info(memory_record)")}
    for column, ddl in (("deleted_at", "TEXT NOT NULL DEFAULT ''"),
                        ("norm_text", "TEXT NOT NULL DEFAULT ''")):
        if column not in have:
            conn.execute(f"ALTER TABLE memory_record ADD COLUMN {column} {ddl}")


def schema_version(db_path: Path | str | None = None) -> dict[str, Any]:
    """这份存放是第几版结构、能不能读。迁移没做完就别装作兼容。"""
    path = Path(db_path) if db_path else DB_PATH
    if not path.exists():
        return {"schema_version": None, "stored_version": None, "readable": True,
                "位置": str(path), "说明": "还没有建立存放，第一次写入时会自动建。"}
    conn = connect(path)
    try:
        try:
            row = conn.execute("SELECT value FROM memory_meta WHERE key = 'schema_version'"
                               " LIMIT 1").fetchone()
        except sqlite3.Error:
            row = None
        stored = int(row["value"]) if row and str(row["value"]).isdigit() else 1
        return {"schema_version": SCHEMA_VERSION, "stored_version": stored,
                "readable": stored <= SCHEMA_VERSION, "位置": str(path),
                "说明": ("结构版本一致。" if stored == SCHEMA_VERSION else
                         ("这份数据比当前程序更旧，已经就地补齐。"
                          if stored < SCHEMA_VERSION else
                          "这份数据来自更新的程序，请先升级再用，避免写坏它。"))}
    finally:
        conn.close()


def meta_value(key: str, default: str = "", *, db_path: Path | str | None = None) -> str:
    path = Path(db_path) if db_path else DB_PATH
    if not path.exists():
        return default
    conn = connect(path)
    try:
        row = conn.execute("SELECT value FROM memory_meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default
    except sqlite3.Error:
        return default
    finally:
        conn.close()


def connect(db_path: Path | str | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    if read_only and not path.exists():
        raise FileNotFoundError(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri if read_only and path.exists() else str(path),
                           uri=read_only and path.exists(), timeout=10.0,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _writable(db_path: Path | str | None) -> tuple[sqlite3.Connection, Path]:
    if isinstance(db_path, sqlite3.Connection):   # 调用方已经开好库了，别再开一次
        return db_path, DB_PATH
    path = Path(db_path) if db_path else DB_PATH
    init_db(path)
    return connect(path), path


# ---------------------------------------------------------------------------
# 行 <-> 契约
# ---------------------------------------------------------------------------

def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        memory_id=str(row["memory_id"]),
        character_id=str(row["character_id"]),
        layer=str(row["layer"]),
        kind=str(row["kind"]),
        text=str(row["text"]),
        confidence=float(row["confidence"]),
        explicit=bool(row["explicit_flag"]),
        source_session=str(row["source_session"]),
        source_turn=int(row["source_turn"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        user_editable=bool(row["user_editable"]),
        active=bool(row["active"]),
        tags=_loads_list(row["tags"]),
    )


def _loads_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    try:
        loaded = json.loads(str(value or "[]"))
    except ValueError:
        return []
    return [str(item) for item in loaded] if isinstance(loaded, list) else []


def record_to_row(record: MemoryRecord, **extra: Any) -> dict[str, Any]:
    stamp = now_iso()
    return {
        "memory_id": record.memory_id or new_id(),
        "character_id": record.character_id or "default",
        "layer": record.layer or "LONG_TERM_MEMORY",
        "kind": record.kind or "fact",
        "text": (record.text or "").strip(),
        "norm_text": retrieval.normalize_text(record.text),
        "confidence": float(record.confidence),
        "explicit_flag": 1 if record.explicit else 0,
        "source_session": record.source_session or "",
        "source_turn": int(record.source_turn or 0),
        "source_kind": extra.get("source_kind", "CHAT"),
        "user_editable": 1 if record.user_editable else 0,
        "active": 1 if record.active else 0,
        "deleted_at": extra.get("deleted_at", ""),
        "created_at": record.created_at or stamp,
        "updated_at": record.updated_at or stamp,
        "tags": json.dumps(list(record.tags or []), ensure_ascii=False),
    }


def _insert(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    columns = ", ".join(row)
    marks = ", ".join(f":{key}" for key in row)
    conn.execute(f"INSERT INTO memory_record ({columns}) VALUES ({marks})", row)


def _journal(conn: sqlite3.Connection, action: str, memory_id: str, character_id: str,
             detail: str = "") -> None:
    conn.execute("INSERT INTO memory_journal (at, action, memory_id, character_id, detail)"
                 " VALUES (?,?,?,?,?)", (now_iso(), action, memory_id, character_id, detail))


# ---------------------------------------------------------------------------
# 1) 记
# ---------------------------------------------------------------------------

def add(text_or_record: Any = "", character_id: str | None = None, *,
        kind: str = "fact", layer: str = "LONG_TERM_MEMORY", confidence: float = 0.5,
        explicit: bool = False, source_session: str = "", source_turn: int = 0,
        tags: Iterable[str] = (), user_editable: bool = True, source_kind: str = "CHAT",
        memory_id: str | None = None, created_at: str = "",
        db_path: Path | str | None = None) -> MemoryRecord:
    """记一条。layer=CANON 的内容不接受聊天来源，会被挡回来。"""
    if isinstance(text_or_record, MemoryRecord):
        record = text_or_record
    else:
        record = MemoryRecord(text=str(text_or_record or ""))

    body = (record.text or text_or_record or "").strip()
    if not body:
        raise ValueError("EMPTY_TEXT")
    character = (record.character_id if isinstance(text_or_record, MemoryRecord)
                 and record.character_id else (character_id or record.character_id or "default"))
    kind = record.kind if isinstance(text_or_record, MemoryRecord) and record.kind else kind
    layer = record.layer if isinstance(text_or_record, MemoryRecord) and record.layer else layer

    if layer == "CANON" and source_kind == "CHAT":
        # 设定层对聊天只读：想改设定请走设定那一侧的入口
        raise PermissionError("CANON_NOT_WRITABLE_FROM_CHAT")

    record = MemoryRecord(
        memory_id=memory_id or record.memory_id or new_id(),
        character_id=character, layer=layer, kind=kind, text=body,
        confidence=max(0.0, min(1.0, float(confidence if not record.explicit
                                           else max(confidence, record.confidence)))),
        explicit=bool(record.explicit or explicit),
        source_session=record.source_session or source_session,
        source_turn=int(record.source_turn or source_turn or 0),
        created_at=record.created_at or created_at,
        updated_at="", user_editable=bool(record.user_editable and user_editable),
        active=True, tags=record.tags or list(tags or []),
    )
    row = record_to_row(record, source_kind=source_kind)
    conn, path = _writable(db_path)
    try:
        with _WRITE_LOCK, conn:
            _insert(conn, row)
            _journal(conn, "ADD", row["memory_id"], row["character_id"],
                     f"{row['layer']}/{row['kind']}|{source_kind}")
        saved = get(row["memory_id"], db_path=path)
        return saved or record
    finally:
        conn.close()


def remember_turn(text: str, *, role: str = "user", character_id: str | None = None,
                  session_id: str = "", turn_id: int = 0,
                  db_path: Path | str | None = None) -> MemoryRecord:
    """这一场说过的话先落在 SESSION 层，clear_session 一把清掉，不会污染长期。"""
    return add(text, character_id, kind="turn", layer=SESSION_LAYER, confidence=1.0,
               source_session=session_id, source_turn=turn_id,
               user_editable=False, source_kind="SESSION",
               tags=[f"role:{role}"], db_path=db_path)


# ---------------------------------------------------------------------------
# 2) 查
# ---------------------------------------------------------------------------

def memory_spec(character_id: str | None = None, *, include_session: bool = False,
                session_id: str | None = None,
                kinds: Sequence[str] | None = None) -> retrieval.IndexSpec:
    clauses = ["memory_record.active = 1"]
    args: list[Any] = []
    if character_id:
        clauses.append("memory_record.character_id = ?")
        args.append(character_id)
    if not include_session:
        clauses.append("memory_record.layer <> ?")
        args.append(SESSION_LAYER)
    if session_id:
        clauses.append("memory_record.source_session = ?")
        args.append(session_id)
    if kinds:
        clauses.append(f"memory_record.kind IN ({', '.join('?' for _ in kinds)})")
        args.extend(kinds)
    return retrieval.IndexSpec(
        table="memory_record", fts="memory_fts", text_column="text", key_column="memory_id",
        extra_select="memory_record.layer AS layer, memory_record.kind AS kind,"
                     " memory_record.confidence AS confidence,"
                     " memory_record.explicit_flag AS explicit_flag,"
                     " memory_record.source_session AS source_session,"
                     " memory_record.source_turn AS source_turn",
        where=" AND ".join(clauses), params=tuple(args),
    )


def search(query: str, character_id: str | None = None, *, limit: int = 6,
           include_session: bool = False, session_id: str | None = None,
           kinds: Sequence[str] | None = None, min_confidence: float = 0.0,
           recent_terms: Sequence[str] | None = None,
           db_path: Path | str | None = None, as_result: bool = False) -> Any:
    """按中文找记忆。默认只看长期层，这一场的原话要显式带 include_session。

    as_result=True 时返回统一的工具结果，好让工具入口那边一行代码就能接。
    recent_terms 是「最近几轮正在聊的词」（§12 的第三条排序依据）：给了就在层内
    把命中的往上抬，不给就完全退回 bm25 的顺序。
    """
    limit = max(1, min(int(limit or 6), 24))
    started = time.perf_counter()
    path = Path(db_path) if db_path else DB_PATH
    passages = retrieval.search_index(path, memory_spec(character_id,
                                                        include_session=include_session,
                                                        session_id=session_id,
                                                        kinds=kinds),
                                      query, limit=limit)
    picked: list[retrieval.LayeredPassage] = []
    for key, body, score, extra in passages:
        confidence = float(extra.get("confidence") or 0.5)
        if confidence < float(min_confidence or 0.0):
            continue
        # 把握度和「用户明说过」参与排序：同样的字面命中，更可信的排前面
        value = score * (0.7 + 0.3 * confidence)
        if extra.get("explicit_flag"):
            value *= 1.08
        extra = dict(extra)
        extra["layer"] = LAYER_INTERACTION
        extra["memory_id"] = key
        passage = retrieval.to_passage(key, body, min(value, 1.5), extra)
        picked.append(passage)
    picked.sort(key=lambda item: item.score, reverse=True)
    picked = retrieval.boost_by_terms(picked, list(recent_terms or []))[:limit]
    if not as_result:
        return picked
    return ToolResult(tool_id="memory_search", ok=True, passages=picked,
                      text="\n".join(item.text for item in picked),
                      elapsed_ms=int((time.perf_counter() - started) * 1000))


def _read_conn(db_path: Path | str | sqlite3.Connection | None
               ) -> tuple[sqlite3.Connection | None, bool]:
    """读一侧的开库：已经连着就直接用（返回的第二个值是「该不该由我关」）。

    内部几个函数（import_ / export）手里已经攥着连接，再 Path(连接) 一次就会炸，
    所以这里统一收两种传法。
    """
    if isinstance(db_path, sqlite3.Connection):
        return db_path, False
    path = Path(db_path) if db_path else DB_PATH
    if not path.exists():
        return None, False
    return connect(path), True


def get(memory_id: str, *, db_path: Path | str | None = None) -> MemoryRecord | None:
    conn, mine = _read_conn(db_path)
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT * FROM memory_record WHERE memory_id = ?",
                           (memory_id,)).fetchone()
        return _row_to_record(row) if row else None
    finally:
        if mine:
            conn.close()


def list_memories(character_id: str | None = None, *, include_archived: bool = False,
                  layers: Sequence[str] | None = None, limit: int = 200,
                  db_path: Path | str | None = None) -> list[MemoryRecord]:
    conn, mine = _read_conn(db_path)
    if conn is None:
        return []
    try:
        clauses, args = [], []
        if character_id:
            clauses.append("character_id = ?")
            args.append(character_id)
        if not include_archived:
            clauses.append("active = 1")
        if layers:
            clauses.append(f"layer IN ({', '.join('?' for _ in layers)})")
            args.extend(layers)
        sql = "SELECT * FROM memory_record"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY explicit_flag DESC, confidence DESC, updated_at DESC LIMIT ?"
        args.append(max(1, int(limit)))
        return [_row_to_record(row) for row in conn.execute(sql, args)]
    finally:
        if mine:
            conn.close()


# ---------------------------------------------------------------------------
# 3) 改
# ---------------------------------------------------------------------------

_EDITABLE = {"text", "norm_text", "kind", "layer", "confidence", "explicit_flag",
             "user_editable", "active", "deleted_at", "tags", "updated_at",
             "source_session", "source_turn"}


def update(memory_id: str, *, text: str | None = None, kind: str | None = None,
           layer: str | None = None, confidence: float | None = None,
           explicit: bool | None = None, user_editable: bool | None = None,
           active: bool | None = None, tags: Iterable[str] | None = None,
           source_session: str | None = None, source_turn: int | None = None,
           db_path: Path | str | None = None) -> dict[str, Any]:
    """改一条。用户手改过的（user_editable=False 之外的场景）会刷新 updated_at。"""
    conn, _path = _writable(db_path)
    try:
        row = conn.execute("SELECT * FROM memory_record WHERE memory_id = ?",
                           (memory_id,)).fetchone()
        if row is None:
            return {"ok": False, "说明": "没有找到这条记忆。"}
        if not row["user_editable"] and text is not None:
            return {"ok": False,
                    "说明": "这条不允许直接改写，先把它删掉再重新记一条。"}
        sets: list[str] = []
        args: list[Any] = []
        if text is not None:
            body = text.strip()
            if not body:
                return {"ok": False, "说明": "要改成的内容是空的。"}
            sets += ["text = ?", "norm_text = ?"]
            args += [body, retrieval.normalize_text(body)]
        for column, value in (("kind", kind), ("layer", layer), ("confidence", confidence),
                             ("explicit_flag", None if explicit is None else int(explicit)),
                             ("user_editable", None if user_editable is None
                              else int(user_editable)),
                             ("active", None if active is None else int(active)),
                             ("source_session", source_session),
                             ("source_turn", source_turn),
                             ("tags", None if tags is None
                              else json.dumps(list(tags), ensure_ascii=False))):
            if value is None:
                continue
            sets.append(f"{column} = ?")
            args.append(value)
        if not sets:
            return {"ok": True, "memory_id": memory_id, "改动": 0}
        if active is False:
            sets.append("deleted_at = ?")
            args.append(now_iso())
        elif active is True:
            sets.append("deleted_at = ''")
        sets.append("updated_at = ?")
        args.append(now_iso())
        args.append(memory_id)
        with _WRITE_LOCK, conn:
            conn.execute(f"UPDATE memory_record SET {', '.join(sets)} WHERE memory_id = ?",
                         args)
            _journal(conn, "UPDATE", memory_id, str(row["character_id"]),
                     ",".join(sets[:-2]))
        return {"ok": True, "memory_id": memory_id, "改动": len(sets) - 2}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4) 删（收起）与还原
# ---------------------------------------------------------------------------

def delete(memory_id: str | None = None, *, character_id: str | None = None,
           match_text: str | None = None, hard: bool = False,
           db_path: Path | str | None = None) -> dict[str, Any]:
    """三种删法：给编号删一条、给一段话删最像的那条、给人物名删全部。

    默认是「收起来」：内容还在原处，restore_character 能整片还原。hard=True 才是真抹掉。
    """
    conn, _path = _writable(db_path)
    try:
        stamp = now_iso()
        targets: list[str] = []
        if memory_id:
            targets = [memory_id]
        elif match_text:
            targets = _match_ids(conn, match_text, character_id)
            if not targets:
                return {"ok": False, "说明": "没找到跟这句话对得上的记忆。",
                        "候选条数": 0}
        elif character_id:
            rows = conn.execute("SELECT memory_id FROM memory_record WHERE character_id = ?"
                                " AND active = 1", (character_id,)).fetchall()
            targets = [str(row["memory_id"]) for row in rows]
        else:
            return {"ok": False, "说明": "要说清楚删哪一条，或者删谁的。"}
        if not targets:
            return {"ok": True, "条数": 0, "memory_ids": [], "说明": "本来就没有要删的。"}
        marks = ", ".join("?" for _ in targets)
        with _WRITE_LOCK, conn:
            if hard:
                conn.execute(f"DELETE FROM memory_record WHERE memory_id IN ({marks})", targets)
            else:
                conn.execute(f"UPDATE memory_record SET active = 0, deleted_at = ?,"
                             f" updated_at = ? WHERE memory_id IN ({marks})",
                             (stamp, stamp, *targets))
            for one in targets:
                _journal(conn, "PURGE" if hard else "SOFT_DELETE", one,
                         character_id or "", match_text or "")
        return {"ok": True, "条数": len(targets), "memory_ids": targets, "彻底删除": hard}
    finally:
        conn.close()


def _match_ids(conn: sqlite3.Connection, text: str,
               character_id: str | None) -> list[str]:
    """「忘掉刚才那句」这类要求：按归一化文本找最像的一条，阈值给得比较松。"""
    target = retrieval.normalize_text(text)
    if not target:
        return []
    sql = "SELECT memory_id, text, character_id FROM memory_record WHERE active = 1"
    args: tuple[Any, ...] = ()
    if character_id:
        sql += " AND character_id = ?"
        args = (character_id,)
    scored = [(retrieval.similarity(text, str(row["text"])), str(row["memory_id"]),
               bool(text.strip() in str(row["text"])))
              for row in conn.execute(sql, args)]
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return []
    best = scored[0]
    exact = [one for score, one, contained in scored if contained]
    if exact:
        return exact
    return [best[1]] if best[0] >= 0.62 else []


def clear_session(character_id: str | None = None, session_id: str | None = None, *,
                  db_path: Path | str | None = None) -> dict[str, Any]:
    """把这一场的原话收起来，不动长期记忆。"""
    result = _clear_layers(character_id, (SESSION_LAYER,), session_id=session_id,
                           db_path=db_path, action="CLEAR_SESSION")
    # 摘要与这一场的原话是同一层东西：清了聊天却留着摘要，等于留下了半份聊天
    from . import summary as _summary    # 延迟导入：包初始化时互相牵扯

    dropped = _summary.clear(result["character_id"], session_id, db_path=db_path)
    result["摘要条数"] = dropped
    return result


def clear_long_term(character_id: str | None = None, *, kinds: Sequence[str] | None = None,
                    db_path: Path | str | None = None) -> dict[str, Any]:
    """把长期记忆收起来（含关系、承诺、目标），这一场的原话不受影响。"""
    result = _clear_layers(character_id, LONG_TERM_LAYERS, kinds=kinds,
                           db_path=db_path, action="CLEAR_LONG_TERM")
    if result.get("条数"):
        # 设定层对聊天只读：清长期绝不能顺手把设定也清了
        result["设定条数"] = 0
    return result



def _clear_layers(character_id: str | None, layers: Sequence[str], *,
                  session_id: str | None = None, kinds: Sequence[str] | None = None,
                  db_path: Path | str | None = None, action: str = "CLEAR") -> dict[str, Any]:
    conn, _path = _writable(db_path)
    try:
        character = character_id or "default"
        clauses = ["character_id = ?", "layer <> 'CANON'", f"layer IN ({', '.join('?' for _ in layers)})"]
        args: list[Any] = [character, *layers]
        if session_id:
            clauses.append("source_session = ?")
            args.append(session_id)
        if kinds:
            clauses.append(f"kind IN ({', '.join('?' for _ in kinds)})")
            args.extend(kinds)
        rows = conn.execute("SELECT memory_id FROM memory_record WHERE active = 1 AND "
                            + " AND ".join(clauses), args).fetchall()
        targets = [str(row["memory_id"]) for row in rows]
        stamp = now_iso()
        if targets:
            marks = ", ".join("?" for _ in targets)
            with _WRITE_LOCK, conn:
                conn.execute(f"UPDATE memory_record SET active = 0, deleted_at = ?,"
                             f" updated_at = ? WHERE memory_id IN ({marks})",
                             (stamp, stamp, *targets))
                _journal(conn, action, character, character, str(len(targets)))
        return {"ok": True, "条数": len(targets), "character_id": character,
                "说明": f"已经收起来 {len(targets)} 条，随时可以还原。"}
    finally:
        conn.close()


def restore_character(character_id: str, *, mode: str = "archived",
                      db_path: Path | str | None = None) -> dict[str, Any]:
    """两种「恢复」，语义完全不同，别混（任务 §16 要求三个概念分清）。

      mode='archived'（默认）：把之前收起来的记忆全部放回来 —— 对应接口清单里的
          restore_character()，undo 一次清空。
      mode='initial'：回到「刚认识」的初始状态（§26 验收：CANON 保留、互动记忆清除）。
          只清互动层（这一场的原话 + 长期记忆 + 关系），设定事实与小说原文一条都不动，
          同时把设定层里被收起来的东西放回来。
    """
    character = (character_id or "").strip() or "default"
    if mode == "initial":
        return _restore_initial(character, db_path=db_path)
    conn, _path = _writable(db_path)
    try:
        rows = conn.execute("SELECT memory_id FROM memory_record WHERE character_id = ?"
                            " AND active = 0", (character,)).fetchall()
        targets = [str(row["memory_id"]) for row in rows]
        stamp = now_iso()
        if targets:
            marks = ", ".join("?" for _ in targets)
            with _WRITE_LOCK, conn:
                conn.execute(f"UPDATE memory_record SET active = 1, deleted_at = ?,"
                             f" updated_at = ? WHERE memory_id IN ({marks})",
                             ("", stamp, *targets))
                _journal(conn, "RESTORE", character, character, str(len(targets)))
        left = conn.execute("SELECT COUNT(*) FROM memory_record WHERE character_id = ?"
                            " AND active = 1", (character,)).fetchone()[0]
        return {"ok": True, "mode": "archived", "character_id": character,
                "还原条数": len(targets), "在用的条数": int(left),
                "说明": f"已经把 {len(targets)} 条放回来了。"}
    finally:
        conn.close()


def _restore_initial(character: str, *, db_path: Path | str | None = None) -> dict[str, Any]:
    """回到最初：互动记忆清空、设定层原样保留（§20 的分层在这里落地）。"""
    conn, _path = _writable(db_path)
    try:
        stamp = now_iso()
        rows = conn.execute("SELECT memory_id FROM memory_record WHERE character_id = ?"
                            " AND active = 1 AND layer <> 'CANON'", (character,)).fetchall()
        targets = [str(row["memory_id"]) for row in rows]
        if targets:
            marks = ", ".join("?" for _ in targets)
            with _WRITE_LOCK, conn:
                conn.execute(f"UPDATE memory_record SET active = 0, deleted_at = ?,"
                             f" updated_at = ? WHERE memory_id IN ({marks})",
                             (stamp, stamp, *targets))
                _journal(conn, "RESTORE_INITIAL", character, character, str(len(targets)))
        left = conn.execute("SELECT COUNT(*) FROM memory_record WHERE character_id = ?"
                            " AND active = 1 AND layer <> 'CANON'", (character,)).fetchone()[0]
        kept = conn.execute("SELECT COUNT(*) FROM memory_record WHERE character_id = ?"
                            " AND active = 1 AND layer = 'CANON'", (character,)).fetchone()[0]
        return {"ok": True, "mode": "initial", "character_id": character,
                "清除条数": len(targets), "剩余互动条数": int(left),
                "保留的设定条数": int(kept),
                "说明": (f"已经退回刚认识的样子：互动记忆收起来 {len(targets)} 条，"
                         "原著资料和人物设定一条没动。")}
    finally:
        conn.close()


def purge(character_id: str, *, db_path: Path | str | None = None) -> dict[str, Any]:
    """真抹掉某人的历史（含收起来的）。只有明确要清盘时才用。"""
    conn, _path = _writable(db_path)
    try:
        with _WRITE_LOCK, conn:
            cur = conn.execute("DELETE FROM memory_record WHERE character_id = ?",
                               (character_id,))
            _journal(conn, "PURGE_ALL", character_id, character_id, str(cur.rowcount))
        from . import summary as _summary    # 延迟导入
        dropped = _summary.clear(character_id, None, db_path=conn)
        return {"ok": True, "条数": cur.rowcount, "摘要条数": dropped}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 5) 导出 / 导入（换机器、备份、把一份整理好的记忆搬进来）
# ---------------------------------------------------------------------------

def export(character_id: str | None = None, *, include_archived: bool = True,
           db_path: Path | str | None = None) -> dict[str, Any]:
    records = list_memories(character_id, include_archived=include_archived, limit=100000,
                            db_path=db_path)
    return {
        "schema_version": SCHEMA_VERSION,
        "导出时间": now_iso(),
        "人物": character_id or "全部",
        "条数": len(records),
        "records": [item.to_dict() for item in records],
        "说明": f"一共 {len(records)} 条，可以再导入回去。",
    }


def import_(payload: Any, *, character_id: str | None = None, merge: bool = True,
            overwrite_explicit: bool = False,
            db_path: Path | str | None = None) -> dict[str, Any]:
    """把导出的内容搬回来。

    merge=True 时按归一化文本查重：同意思的只抬把握度，不新增重复条目。
    设定层的内容不接受从这里进（要改设定请走设定那一侧）。
    """
    data = payload
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    rows = data.get("records") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return {"ok": False, "说明": "这份要导入的内容读不出来，麻烦重新导出一份。"}
    added = merged = skipped = 0
    conn, _path = _writable(db_path)
    try:
        existing = list_memories(character_id, include_archived=True, limit=100000,
                                 db_path=conn)
        for item in rows:
            if not isinstance(item, dict):
                skipped += 1
                continue
            record = MemoryRecord.from_dict(item)
            body = (record.text or "").strip()
            if not body:
                skipped += 1
                continue
            target_char = character_id or record.character_id or "default"
            if record.layer == "CANON":
                skipped += 1
                continue
            duplicate = None
            if merge:
                for old in existing:
                    if old.character_id != target_char or not old.active:
                        continue
                    if retrieval.similarity(body, old.text) >= 0.9:
                        duplicate = old
                        break
            with _WRITE_LOCK, conn:
                if duplicate is not None and not (overwrite_explicit and record.explicit):
                    conn.execute("UPDATE memory_record SET confidence = ?,"
                                 " updated_at = ?, explicit_flag = MAX(explicit_flag, ?)"
                                 " WHERE memory_id = ?",
                                 (max(duplicate.confidence, record.confidence), now_iso(),
                                  1 if record.explicit else 0, duplicate.memory_id))
                    _journal(conn, "IMPORT_MERGE", duplicate.memory_id, target_char, "")
                    merged += 1
                    continue
                record = MemoryRecord(
                    memory_id=new_id(), character_id=target_char, layer=record.layer,
                    kind=record.kind or "fact", text=body, confidence=record.confidence,
                    explicit=record.explicit, source_session=record.source_session,
                    source_turn=record.source_turn, created_at=record.created_at,
                    updated_at="", user_editable=record.user_editable,
                    active=bool(record.active), tags=record.tags)
                _insert(conn, record_to_row(record, source_kind="IMPORT"))
                _journal(conn, "IMPORT_ADD", record.memory_id, target_char, "")
                added += 1
            if merge:
                existing = [r for r in existing if r.character_id != target_char
                            or r.text != body]
        return {"ok": True, "新增": added, "并入已有": merged, "跳过": skipped,
                "说明": f"新增 {added} 条，并入 {merged} 条，跳过 {skipped} 条。"}
    finally:
        conn.close()


# 工具入口那边是按名字反射的，这里把「import」这个名字也备上
globals()["import"] = import_
