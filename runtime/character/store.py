# -*- coding: utf-8 -*-
"""人物档案的本地存放：增删改查 + 「当前是哪个人物」的指针。

一处存放：runtime/character/data/characters.sqlite3（单文件）。
指针：runtime/character/data/active_character.json（很小，就是当前人物是谁）。

这里没有任何模型或语音相关的东西，也不联网。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import contracts as _contracts  # type: ignore
except ImportError:  # pragma: no cover - 被当成脚本直接跑时的退路
    import sys
    _ROOT = Path(__file__).resolve().parents[2]
    for _p in (str(_ROOT), str(_ROOT / "runtime" / "core")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    from runtime.core.providers import contracts as _contracts  # type: ignore

CharacterProfile = _contracts.CharacterProfile

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
DB_PATH = DATA_DIR / "characters.sqlite3"
POINTER_PATH = DATA_DIR / "active_character.json"
DEFAULT_CHARACTER_ID = "default"

# 「当前是哪个人物」这个指针默认在项目里。自测和并行的第二套实例要能把它改到临时
# 目录去，否则跑一次自测就把正式环境正在用的人物换了。
_POINTER_OVERRIDE: Path | None = None


def pointer_path() -> Path:
    """指针文件的真实位置（可能被 set_pointer_path 改到别处）。"""
    return _POINTER_OVERRIDE or POINTER_PATH


def set_pointer_path(path: Path | str | None) -> None:
    global _POINTER_OVERRIDE
    _POINTER_OVERRIDE = Path(path) if path else None

# 结构版本：加列 / 加表都要 +1，并在 _migrate() 里补一条 ALTER（任务 §13）。
SCHEMA_VERSION = 2

_lock = threading.RLock()

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS character_profile (
    character_id        TEXT PRIMARY KEY,
    display_name        TEXT NOT NULL DEFAULT '',
    nickname            TEXT NOT NULL DEFAULT '',
    pronoun             TEXT NOT NULL DEFAULT '',
    speaking_style      TEXT NOT NULL DEFAULT '',
    relationship        TEXT NOT NULL DEFAULT '',
    boundary            TEXT NOT NULL DEFAULT '',
    greeting            TEXT NOT NULL DEFAULT '',
    likes               TEXT NOT NULL DEFAULT '[]',
    dislikes            TEXT NOT NULL DEFAULT '[]',
    avatar_id           TEXT NOT NULL DEFAULT '',
    preferred_providers TEXT NOT NULL DEFAULT '{}',
    persona_text        TEXT NOT NULL DEFAULT '',
    active              INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT '',
    updated_at          TEXT NOT NULL DEFAULT ''
);

-- 设定事实：人物世界观里「不能被聊天改动」的那部分。
-- basis 把 §11 要的「原著事实 / 模型或规则推测」分开：
--   canon    = 原文里真写得有，必须同时带 source_id + chunk_id + location + evidence
--   inferred = 由统计或规则推出来的倾向，带依据但不是原文句子
--   manual   = 用户自己补的
CREATE TABLE IF NOT EXISTS canon_fact (
    fact_id       TEXT PRIMARY KEY,
    character_id  TEXT NOT NULL DEFAULT 'default',
    kind          TEXT NOT NULL DEFAULT 'setting',
    dimension     TEXT NOT NULL DEFAULT '',   -- §11 的 17 个分析维度之一，见 canon.DIMENSIONS
    text          TEXT NOT NULL,
    norm_text     TEXT NOT NULL DEFAULT '',
    origin        TEXT NOT NULL DEFAULT 'manual',   -- manual|novel|setting
    basis         TEXT NOT NULL DEFAULT 'canon',    -- canon|inferred|manual
    document_id   TEXT NOT NULL DEFAULT '',
    source_id     TEXT NOT NULL DEFAULT '',         -- 来源编号（书名/文件级）
    chunk_id      TEXT NOT NULL DEFAULT '',         -- 落在哪一段（source_part.part_id）
    location      TEXT NOT NULL DEFAULT '',         -- 给人看的出处：第几章·第几段·第几字
    evidence      TEXT NOT NULL DEFAULT '',         -- 原文引句本身
    ordinal       INTEGER NOT NULL DEFAULT 0,
    weight        INTEGER NOT NULL DEFAULT 100,     -- 摘要里谁先说，数大的先
    confidence    REAL NOT NULL DEFAULT 0.9,
    user_editable INTEGER NOT NULL DEFAULT 1,
    active        INTEGER NOT NULL DEFAULT 1,
    deleted_at    TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS canon_active_idx
    ON canon_fact (character_id, active, weight DESC);
CREATE INDEX IF NOT EXISTS canon_dimension_idx
    ON canon_fact (character_id, dimension, active);

CREATE VIRTUAL TABLE IF NOT EXISTS canon_fts USING fts5(
    text, content='canon_fact', content_rowid='rowid', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS canon_fts_ai AFTER INSERT ON canon_fact BEGIN
    INSERT INTO canon_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS canon_fts_ad AFTER DELETE ON canon_fact BEGIN
    INSERT INTO canon_fts(canon_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS canon_fts_au AFTER UPDATE OF text ON canon_fact BEGIN
    INSERT INTO canon_fts(canon_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO canon_fts(rowid, text) VALUES (new.rowid, new.text);
END;

-- 导入的小说原文：一篇小说 -> 若干段，段落带章节、序号、来源与校验和
CREATE TABLE IF NOT EXISTS source_document (
    document_id  TEXT PRIMARY KEY,
    character_id TEXT NOT NULL DEFAULT 'default',
    title        TEXT NOT NULL DEFAULT '',
    source_path  TEXT NOT NULL DEFAULT '',
    source_name  TEXT NOT NULL DEFAULT '',
    sha256       TEXT NOT NULL DEFAULT '',
    format       TEXT NOT NULL DEFAULT '',        -- txt|md
    char_count   INTEGER NOT NULL DEFAULT 0,
    part_count   INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS document_sha_idx
    ON source_document (character_id, sha256);

CREATE TABLE IF NOT EXISTS source_part (
    part_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  TEXT NOT NULL,
    character_id TEXT NOT NULL DEFAULT 'default',
    ordinal      INTEGER NOT NULL,
    chapter      TEXT NOT NULL DEFAULT '',
    heading_path TEXT NOT NULL DEFAULT '',
    text         TEXT NOT NULL,
    char_start   INTEGER NOT NULL DEFAULT 0,
    char_end     INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS part_ordinal_idx
    ON source_part (document_id, ordinal);
CREATE INDEX IF NOT EXISTS part_character_idx ON source_part (character_id);

CREATE VIRTUAL TABLE IF NOT EXISTS part_fts USING fts5(
    text, content='source_part', content_rowid='part_id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS part_fts_ai AFTER INSERT ON source_part BEGIN
    INSERT INTO part_fts(rowid, text) VALUES (new.part_id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS part_fts_ad AFTER DELETE ON source_part BEGIN
    INSERT INTO part_fts(part_fts, rowid, text) VALUES ('delete', old.part_id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS part_fts_au AFTER UPDATE OF text ON source_part BEGIN
    INSERT INTO part_fts(part_fts, rowid, text) VALUES ('delete', old.part_id, old.text);
    INSERT INTO part_fts(rowid, text) VALUES (new.part_id, new.text);
END;

CREATE TABLE IF NOT EXISTS change_journal (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    action       TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    character_id TEXT NOT NULL DEFAULT '',
    detail       TEXT NOT NULL DEFAULT ''
);

-- 结构版本与迁移水位（任务 §13：必须有 schema_version，未来可以迁移）
CREATE TABLE IF NOT EXISTS character_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

# 老库补列：只处理「加过列」的情况；CREATE TABLE 里的新列对新建库已经生效。
_NEW_COLUMNS: dict[str, dict[str, str]] = {
    "canon_fact": {
        "dimension": "TEXT NOT NULL DEFAULT ''",
        "basis": "TEXT NOT NULL DEFAULT 'canon'",
        "source_id": "TEXT NOT NULL DEFAULT ''",
        "chunk_id": "TEXT NOT NULL DEFAULT ''",
        "location": "TEXT NOT NULL DEFAULT ''",
        "evidence": "TEXT NOT NULL DEFAULT ''",
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize(text: str | None) -> str:
    """查重用的骨架文本，与记忆侧同一套规则（同一个实现）。"""
    from runtime.memory import retrieval  # 延迟导入：避免包初始化时互相牵扯

    return retrieval.normalize_text(text)


def connect(db_path: Path | str | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    if read_only and not path.exists():
        raise FileNotFoundError(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str | None = None) -> Path:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA_SQL)
        _migrate(conn)
        conn.execute("INSERT OR REPLACE INTO character_meta (key, value) VALUES"
                     " ('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.commit()
    finally:
        conn.close()
    return path


def _migrate(conn: sqlite3.Connection) -> None:
    """老库里补上后加的列（列已存在时 sqlite 会报错，所以先看 PRAGMA）。"""
    for table, columns in _NEW_COLUMNS.items():
        have = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue
        for column, ddl in columns.items():
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def schema_version(db_path: Path | str | None = None) -> dict[str, Any]:
    """这份人物存放是第几版结构（§13）。"""
    path = Path(db_path) if db_path else DB_PATH
    if not path.exists():
        return {"schema_version": None, "stored_version": None, "readable": True,
                "位置": str(path), "说明": "还没有建立存放，第一次写入时会自动建。"}
    conn = connect(path)
    try:
        try:
            row = conn.execute("SELECT value FROM character_meta WHERE key = 'schema_version'"
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


# ---------------------------------------------------------------------------
# 档案：增 / 改 / 查 / 删（软删） / 还原
# ---------------------------------------------------------------------------

_TEXT_FIELDS = ("display_name", "nickname", "pronoun", "speaking_style", "relationship",
                "boundary", "greeting", "avatar_id")


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [part for part in value.replace("、", ",").split(",") if part.strip()]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
            return dict(loaded) if isinstance(loaded, dict) else {}
        except ValueError:
            return {}
    return {}


def row_to_profile(row: sqlite3.Row | None) -> CharacterProfile | None:
    if row is None:
        return None
    return CharacterProfile(
        character_id=str(row["character_id"]),
        display_name=str(row["display_name"]),
        nickname=str(row["nickname"]),
        pronoun=str(row["pronoun"]),
        speaking_style=str(row["speaking_style"]),
        relationship=str(row["relationship"]),
        boundary=str(row["boundary"]),
        greeting=str(row["greeting"]),
        likes=_as_list(row["likes"]),
        dislikes=_as_list(row["dislikes"]),
        avatar_id=str(row["avatar_id"]),
        preferred_providers=_as_dict(row["preferred_providers"]),
        # The table has always had created_at; the read side just never carried it out,
        # so export/backup code that asks for it dies on an AttributeError.  The key
        # check is for a profile DB that predates the column being queried explicitly.
        created_at=str(row["created_at"]) if "created_at" in row.keys() else "",
        updated_at=str(row["updated_at"]),
    )


def save_profile(profile: CharacterProfile, *, db_path: Path | str | None = None,
                 persona: str | None = None) -> CharacterProfile:
    """存一份档案；没给 character_id 就当作 default。写入后回读，返回带时间的副本。

    persona 单独传：不传表示「这一轮不动人物台词」，省得每次存档案都把台词本清空。
    """
    character_id = (profile.character_id or DEFAULT_CHARACTER_ID).strip()
    stamp = now_iso()
    conn = init_and_connect(db_path)
    try:
        old = conn.execute("SELECT created_at, persona_text FROM character_profile "
                           "WHERE character_id = ?", (character_id,)).fetchone()
        created = (str(old["created_at"]) or stamp) if old is not None else stamp
        keep = str(old["persona_text"]) if old is not None else ""
        text = keep if persona is None else persona.strip()
        with _lock, conn:
            conn.execute(
                """
                INSERT INTO character_profile (character_id, display_name, nickname, pronoun,
                    speaking_style, relationship, boundary, greeting, likes, dislikes,
                    avatar_id, preferred_providers, persona_text, active, created_at, updated_at)
                VALUES (:cid,:display,:nickname,:pronoun,:style,:relation,:boundary,:greeting,
                        :likes,:dislikes,:avatar,:providers,:persona,1,:created,:updated)
                ON CONFLICT(character_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    nickname=excluded.nickname,
                    pronoun=excluded.pronoun,
                    speaking_style=excluded.speaking_style,
                    relationship=excluded.relationship,
                    boundary=excluded.boundary,
                    greeting=excluded.greeting,
                    likes=excluded.likes,
                    dislikes=excluded.dislikes,
                    avatar_id=excluded.avatar_id,
                    preferred_providers=excluded.preferred_providers,
                    persona_text=excluded.persona_text,
                    active=1,
                    updated_at=excluded.updated_at
                """,
                {"cid": character_id, "display": profile.display_name,
                 "nickname": profile.nickname, "pronoun": profile.pronoun,
                 "style": profile.speaking_style, "relation": profile.relationship,
                 "boundary": profile.boundary, "greeting": profile.greeting,
                 "likes": json.dumps(list(profile.likes or []), ensure_ascii=False),
                 "dislikes": json.dumps(list(profile.dislikes or []), ensure_ascii=False),
                 "avatar": profile.avatar_id,
                 "providers": json.dumps(dict(profile.preferred_providers or {}),
                                         ensure_ascii=False),
                 "persona": text, "created": created, "updated": stamp},
            )
            conn.execute("INSERT INTO change_journal (at, action, subject_id, character_id)"
                         " VALUES (?,?,?,?)",
                         (stamp, "PROFILE_SAVE", character_id, character_id))
        row = conn.execute("SELECT * FROM character_profile WHERE character_id = ?",
                           (character_id,)).fetchone()
        return row_to_profile(row) or CharacterProfile(character_id=character_id,
                                                      updated_at=stamp)
    finally:
        conn.close()


def init_and_connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """保证表结构在，然后给一个可写连接。"""
    path = Path(db_path) if db_path else DB_PATH
    init_db(path)
    return connect(path)


def get_profile(character_id: str | None = None, *,
                db_path: Path | str | None = None) -> CharacterProfile | None:
    conn = None
    if isinstance(db_path, sqlite3.Connection):
        conn = db_path
    else:
        path = Path(db_path) if db_path else DB_PATH
        if not path.exists():
            return None
        conn = connect(path)
    try:
        if not character_id:
            character_id = get_active_character(db_path=conn)
        row = conn.execute("SELECT * FROM character_profile WHERE character_id = ? AND active = 1",
                           (character_id,)).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM character_profile WHERE character_id = ?",
                               (character_id,)).fetchone()
        return row_to_profile(row)
    finally:
        if conn is not None and db_path is not conn:
            conn.close()


def list_characters(*, db_path: Path | str | None = None,
                    include_archived: bool = False) -> list[CharacterProfile]:
    path = Path(db_path) if db_path else DB_PATH
    if not path.exists():
        return []
    conn = connect(path)
    try:
        sql = "SELECT * FROM character_profile"
        if not include_archived:
            sql += " WHERE active = 1"
        sql += " ORDER BY character_id"
        return [row_to_profile(row) for row in conn.execute(sql)]  # type: ignore[misc]
    finally:
        conn.close()


def set_persona(text: str, character_id: str | None = None, *,
                db_path: Path | str | None = None) -> dict[str, Any]:
    """写人物台词本。这里只做校验，不生成、不改写用户的文字。"""
    issues = persona_issues(text)
    if issues:
        return {"ok": False, "issues": issues,
                "说明": "这些话不要写在人物台词里，说出来的时候会露出设定痕迹。"}
    character_id = (character_id or get_active_character(db_path=db_path)).strip() or DEFAULT_CHARACTER_ID
    stamp = now_iso()
    conn = init_and_connect(db_path)
    try:
        with _lock, conn:
            conn.execute("UPDATE character_profile SET persona_text = ?, updated_at = ? "
                         "WHERE character_id = ?", (text.strip(), stamp, character_id))
            conn.execute("INSERT INTO change_journal (at, action, subject_id, character_id)"
                         " VALUES (?,?,?,?)", (stamp, "PERSONA_SAVE", character_id, character_id))
        return {"ok": True, "character_id": character_id, "chars": len(text.strip())}
    finally:
        conn.close()


def get_persona(character_id: str | None = None, *,
                db_path: Path | str | None = None) -> str:
    path = Path(db_path) if db_path else DB_PATH
    if not path.exists():
        return ""
    conn = connect(path)
    try:
        character_id = (character_id or get_active_character(db_path=conn))
        row = conn.execute("SELECT persona_text FROM character_profile WHERE character_id = ?",
                           (character_id,)).fetchone()
        return str(row["persona_text"]) if row else ""
    finally:
        conn.close()


# 「露出设定痕迹」的说法：模型一旦这样开口，用户立刻出戏。
# 规则本体在同目录 style_rules.json（任务 §19 要求：这些约束是数据，不是散在代码里的 if）。
# 这个名字保留下来只是为了老调用方不炸，内容一律从规则文件读。
def _banned_patterns() -> tuple[str, ...]:
    from . import rules
    entries = rules.style()["persona_validation"].get("banned_self_statements") or []
    return tuple(str(item.get("pattern") or "") for item in entries if item.get("pattern"))


PERSONA_BANNED: tuple[str, ...] = ()      # 兼容占位；真实条目见 _banned_patterns()


def persona_issues(text: str | None) -> list[str]:
    """人物台词自查：露设定痕迹的话、每轮复述式的总结、说教、模板句凑数。

    只指出问题，不改用户的字：这一段是人自己写的，程序没有权改写它。
    """
    import re as _re
    from . import rules

    validation = rules.style()["persona_validation"]
    problems: list[str] = []
    body = (text or "").strip()
    if not body:
        return [str(validation["empty_persona"])]
    for item in validation.get("banned_self_statements") or []:
        pattern = str(item.get("pattern") or "")
        if not pattern:
            continue
        hit = _re.search(pattern, body)
        if hit:
            problems.append(str(item.get("why") or "出现了「{hit}」这类说法。")
                            .replace("{hit}", hit.group(0)))
    template = validation.get("template_repetition") or {}
    times_at_least = int(template.get("min_times") or 3)
    line_max = int(template.get("max_line_chars") or 24)
    counted: dict[str, int] = {}
    for line in [one.strip() for one in body.splitlines() if one.strip()]:
        counted[line] = counted.get(line, 0) + 1
    for line, times in counted.items():
        if times >= times_at_least and len(line) <= line_max:
            problems.append(str(template.get("why") or "「{line}」重复了 {times} 次。")
                            .replace("{line}", line).replace("{times}", str(times)))
    preaching = validation.get("preaching") or {}
    hits = _re.findall(str(preaching.get("pattern") or ""), body)
    if len(hits) > int(preaching.get("max_count") or 6):
        problems.append(str(preaching.get("why") or "说教太多。").replace("{count}", str(len(hits))))
    recap = validation.get("recap_demand") or {}
    if _re.search(str(recap.get("pattern") or ""), body):
        problems.append(str(recap.get("why") or "要求每轮复述。"))
    return problems


_TEMPLATE_STUFFING = 3  # 兼容旧引用；真实阈值已经进 style_rules.json


def delete_character(character_id: str, *, db_path: Path | str | None = None,
                     hard: bool = False) -> dict[str, Any]:
    """默认只是收起这个人（软删），restore_character 可以还原。"""
    character_id = (character_id or "").strip()
    if not character_id:
        return {"ok": False, "说明": "要先说清楚是哪一个。"}
    stamp = now_iso()
    conn = init_and_connect(db_path)
    try:
        with _lock, conn:
            if hard:
                conn.execute("DELETE FROM character_profile WHERE character_id = ?", (character_id,))
                conn.execute("DELETE FROM canon_fact WHERE character_id = ?", (character_id,))
                conn.execute("DELETE FROM source_part WHERE character_id = ?", (character_id,))
                conn.execute("DELETE FROM source_document WHERE character_id = ?", (character_id,))
                action = "CHARACTER_PURGE"
            else:
                conn.execute("UPDATE character_profile SET active = 0, updated_at = ? "
                             "WHERE character_id = ?", (stamp, character_id))
                conn.execute("UPDATE canon_fact SET active = 0, deleted_at = ?, updated_at = ? "
                             "WHERE character_id = ?", (stamp, stamp, character_id))
                action = "CHARACTER_ARCHIVE"
            conn.execute("INSERT INTO change_journal (at, action, subject_id, character_id)"
                         " VALUES (?,?,?,?)", (stamp, action, character_id, character_id))
        if get_active_character(db_path=conn) == character_id and not hard:
            set_active_character(DEFAULT_CHARACTER_ID, db_path=conn, allow_missing=True)
        return {"ok": True, "character_id": character_id, "hard": hard}
    finally:
        conn.close()


def restore_character(character_id: str, *, db_path: Path | str | None = None) -> dict[str, Any]:
    """把这个人还原：档案、设定事实全部回到在用状态。"""
    character_id = (character_id or "").strip() or DEFAULT_CHARACTER_ID
    stamp = now_iso()
    conn = init_and_connect(db_path)
    try:
        with _lock, conn:
            conn.execute("UPDATE character_profile SET active = 1, updated_at = ? "
                         "WHERE character_id = ?", (stamp, character_id))
            conn.execute("UPDATE canon_fact SET active = 1, deleted_at = '', updated_at = ? "
                         "WHERE character_id = ?", (stamp, character_id))
            conn.execute("UPDATE source_document SET active = 1, updated_at = ? "
                         "WHERE character_id = ?", (stamp, character_id))
            conn.execute("INSERT INTO change_journal (at, action, subject_id, character_id)"
                         " VALUES (?,?,?,?)", (stamp, "CHARACTER_RESTORE", character_id, character_id))
            left = conn.execute("SELECT COUNT(*) FROM character_profile WHERE character_id = ?",
                                (character_id,)).fetchone()[0]
        return {"ok": bool(left), "character_id": character_id, "存在": bool(left)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 当前人物指针（一个小文本文件，不依赖任何外部配置）
# ---------------------------------------------------------------------------

def set_active_character(character_id: str, *, db_path: Path | str | None = None,
                         allow_missing: bool = False) -> dict[str, Any]:
    character_id = (character_id or "").strip() or DEFAULT_CHARACTER_ID
    if not allow_missing:
        known = {row.character_id for row in list_characters(db_path=db_path,
                                                            include_archived=True)}
        if character_id not in known:
            return {"ok": False, "说明": "还没有这个人，先把档案建起来。",
                    "character_id": character_id}
    target = pointer_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"character_id": character_id, "at": now_iso()}
    # Unique temp name: a fixed `.tmp` partner is enough for one lingering handle to
    # make every later write fail for good on Windows, and this pointer is the single
    # source of "who is talking" for the whole persona/memory stack.
    tmp = target.with_name(
        f"{target.name}.{threading.get_ident()}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True, "character_id": character_id}


def get_active_character(*, db_path: Path | str | None = None) -> str:
    # 连接被传进来时（比如 save_profile 内部），指针文件依然是唯一真源
    target = pointer_path()
    if target.exists():
        try:
            payload = json.loads(target.read_text(encoding="utf-8-sig"))
            value = str(payload.get("character_id") or "").strip()
            if value:
                return value
        except (OSError, ValueError):
            pass
    return DEFAULT_CHARACTER_ID
