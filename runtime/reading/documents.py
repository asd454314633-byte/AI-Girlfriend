# -*- coding: utf-8 -*-
"""girlfriend_reading.py — 陪读文档层（AI_GIRLFRIEND_PRODUCT_UI_READING_V2 · §9-§12）。

一个模块，两个读法：7860 的 `/api/reading/*` 路由用它建索引、推进进度；8765 的
`girlfriend_memory_bridge` 用它在系统提示词里拼 `READING_STATE`。两边不各写一份，
否则进度和注入的原文迟早对不上。

存放纪律（§10）：小说原文、索引、临时音频全部在项目内
`<项目根>/reading/{library,index,temp}`，不跨机器、不复制进 WSL。

**MODE_SYSTEM_V1 修正（本轮唯一改动的既有缺陷）**：迁移出 site-packages 时这两条
默认值仍是 WSL 年代的字符串（`/mnt/d/…`、`/home/user/…`），在 Windows 原生
运行时上会被解释成 `D:\\mnt\\d\\…`、`D:\\home\\user\\…` —— 目录确实会被建出来，
但那是个谁也不会去看的影子位置：导入的书、朗读进度全落在那里，页面显示「书库还空着」，
`/api/reading/block` 恒为空。现在默认落到项目真实路径上。

数据库这条同时改了归属：原来写的是「复用女友记忆那一个库」，本轮改成阅读自己的
`runtime/reading/data/reading.sqlite3`。理由有两条，都是硬约束：
  1. 本轮任务明令 `NO_MEMORY_SCHEMA_CHANGE` —— 往记忆库里加 `reading_progress`
     这类表就是在改它的 schema；
  2. §32「小说正文不得写进人物长期 Memory」—— 书本进度与用户记忆分库存放，
     隔离是结构上的，不靠调用方自觉。
SQLite 里仍然只有 `document_id / 当前章节 / 段落 / 位置 / 阅读状态 / 章节摘要`。

索引只存偏移量，不复制正文——一整本小说的正文在 library 里只有一份。
文本按 `(document_id, mtime)` 缓存一份，超过缓存上限就退回每次现读，
内存占用有上界。

切块（§11）按 `。！？；` 和自然段边界，禁止固定 N 字符暴力切句；短句并进前一句，
避免 TTS 一句一顿。

上下文预算（§17）：`CTX=8192` 不变，阅读块整体封顶，超预算时先丢远的邻居段落，
再压章节摘要，绝不挤掉当前段和用户这一问。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time

logger = logging.getLogger("s2s.reading")

# 默认位置按本文件所在位置推出来，不再写死任何一台机器的路径：
#   runtime/reading/documents.py -> 上两级 = 项目根
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

READING_ROOT = os.environ.get("READING_ROOT", "").strip() or os.path.join(_PROJECT_ROOT, "reading")
LIBRARY_DIR = os.path.join(READING_ROOT, "library")
INDEX_DIR = os.path.join(READING_ROOT, "index")
TEMP_DIR = os.path.join(READING_ROOT, "temp")

# 阅读进度自己一个库（见模块头 MODE_SYSTEM_V1 那段说明）：不进用户记忆库，
# 既守住 NO_MEMORY_SCHEMA_CHANGE，也把「书本进度」和「用户记忆」在磁盘上分开。
DB_PATH = os.environ.get("GIRLFRIEND_READING_DB", "").strip() or os.path.join(
    _PROJECT_ROOT, "runtime", "reading", "data", "reading.sqlite3")

# ---- hard limits (a pathological file must not blow up the index) -----------
MAX_DOC_CHARS = 8_000_000          # ~16 MB of UTF-8, well above any novel here
MAX_CHUNKS_PER_DOC = 80_000
MAX_CHAPTERS = 2_000
TEXT_CACHE_MAX_CHARS = 3_000_000   # above this, read from disk on demand

# §17: the whole READING_STATE block is capped; neighbours are dropped first.
READING_BLOCK_MAX_CHARS = 1600
NEIGHBOURS_BEFORE = 3
NEIGHBOURS_AFTER = 2

SUPPORTED_SUFFIX = (".txt", ".md", ".markdown")

_SENT_END = "。！？；!?;…"
_SENT_TAIL = "”』」）)”'』"
_MIN_CHUNK_CHARS = 8               # shorter fragments merge into the previous one
MAX_SEGMENT_CHARS = 120            # one spoken unit, broken only at sentence ends

_CN_NUM = "0-9〇零一二两三四五六七八九十百千万"
_HEADING_RES = (
    re.compile(
        r"^[ \t\u3000]*(?:第[ \t]*[" + _CN_NUM + r"]+[ \t]*(?:章|节|卷|回|篇|部|集|幕))"
        r"(?:[ \t]*[·:：、.\-—]*[ \t\u3000]*(?P<title>[^\n]{0,40}))?[ \t]*$"
    ),
    re.compile(
        r"^[ \t\u3000]*(?:Chapter|CHAPTER|chapter|Part|PART|Book|BOOK)[ \t]+"
        r"(?:[0-9]+|[IVXLCDM]+)[ \t]*[.:：\-—]?[ \t]*(?P<title>[^\n]{0,40})[ \t]*$"
    ),
    re.compile(r"^[ \t\u3000]*(?P<title>序章|楔子|引子|尾声|后记|终章|附录)[ \t\u3000]*[^\n]{0,20}$"),
)

_NOT_UTF8 = ("utf-8-sig", "gb18030", "big5", "utf-16", "latin-1")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def ensure_dirs() -> None:
    for d in (LIBRARY_DIR, INDEX_DIR, TEMP_DIR):
        os.makedirs(d, exist_ok=True)


# --------------------------------------------------------------------------
# storage helpers
# --------------------------------------------------------------------------
def _lib_path(document_id: str) -> str:
    return os.path.join(LIBRARY_DIR, f"{_safe_id(document_id)}.txt")


def _index_path(document_id: str) -> str:
    return os.path.join(INDEX_DIR, f"{_safe_id(document_id)}.json")


def _safe_id(document_id: str) -> str:
    """document_id 会进文件名——先证明它像个 id，不接受任意路径片段。"""
    d = re.sub(r"[^A-Za-z0-9_-]", "", str(document_id or ""))[:64]
    if not d:
        raise ValueError("INVALID_DOCUMENT_ID")
    return d


def _read_index(document_id: str) -> dict:
    try:
        with open(_index_path(document_id), "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("index read failed (%s): %r", document_id, exc)
        return {}


def _write_index(meta: dict) -> None:
    ensure_dirs()
    path = _index_path(meta["document_id"])
    # Per-write unique temp name: one lingering handle on a fixed `<index>.json.tmp`
    # (antivirus scan, crashed sibling) is enough to wedge every later import on
    # Windows while the UI still reports success.  Still ends in `.tmp`, so
    # list_documents() keeps ignoring it.
    tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


_text_cache: dict = {"document_id": "", "mtime": 0.0, "text": ""}


def _text(document_id: str) -> str:
    """正文按 mtime 缓存；过长或换文件就现读，不常驻大字符串。"""
    path = _lib_path(document_id)
    mtime = os.path.getmtime(path)
    if _text_cache["document_id"] == document_id and _text_cache["mtime"] == mtime:
        return _text_cache["text"]
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if len(text) <= TEXT_CACHE_MAX_CHARS:
        _text_cache.update(document_id=document_id, mtime=mtime, text=text)
    else:
        _text_cache.update(document_id="", mtime=0.0, text="")
    return text


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
def _normalize_str(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("﻿", "")


def _normalize(raw: bytes) -> tuple[str, str]:
    for enc in _NOT_UTF8:
        try:
            return _normalize_str(raw.decode(enc)), ("utf-8" if enc == "utf-8-sig" else enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise ValueError("UNKNOWN_ENCODING")


def _read_aloud_segments(para: str, base: int) -> list[tuple[int, int]]:
    """自然段 -> 朗读单元，返回全文绝对偏移 [start, end)。

    只在三件事上断：句末标点、自然段边界、单元长到 `MAX_SEGMENT_CHARS`。
    断点永远落在句子结尾，所以既不违反 §11（禁止固定 N 字符暴力切句），也不会
    把一句话读到一半掐断；上限只是为了让"暂停"和背压（§12 一次只处理当前段）
    保持在一句之内的响应速度。太碎的短句并进前一句，避免 TTS 一顿一顿。
    """
    spans: list[list[int]] = []
    start = 0
    for i, ch in enumerate(para):
        if ch not in _SENT_END:
            continue
        j = i + 1
        while j < len(para) and para[j] in _SENT_TAIL:
            j += 1
        spans.append([start, j])
        start = j
    if start < len(para):
        spans.append([start, len(para)])
    if not spans:
        return []

    units: list[list[int]] = [spans[0]]
    for s, e in spans[1:]:
        if e - units[-1][0] <= MAX_SEGMENT_CHARS:
            units[-1][1] = e
        else:
            units.append([s, e])
    merged: list[list[int]] = [units[0]]
    for s, e in units[1:]:
        if e - s < _MIN_CHUNK_CHARS and merged:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(base + s, base + e) for s, e in merged]


def _parse(text: str) -> list[dict]:
    """章节标题优先（第X章/节/卷/回、Chapter N、序章…），否则整篇算一章按自然段走。"""
    chapters: list[dict] = []
    current = {"index": 0, "title": "", "char_start": 0, "paragraphs": []}
    offset = 0
    total_chunks = 0

    for line in text.split("\n"):
        line_end = offset + len(line)
        stripped = line.strip()
        if stripped:
            heading = None
            # A real heading never carries sentence punctuation; without this
            # guard "引子一样的风灌进来。" would open a new chapter.
            if len(stripped) <= 50 and not any(c in stripped for c in "。！？；"):
                for rex in _HEADING_RES:
                    m = rex.match(line)
                    if m:
                        heading = (m.groupdict().get("title") or "").strip()
                        break
            if heading is not None:
                if current["paragraphs"]:
                    chapters.append(current)
                current = {
                    "index": len(chapters),
                    "title": stripped[:50],
                    "char_start": offset,
                    "paragraphs": [],
                }
                offset = line_end + 1
                continue

            p_start = offset + (len(line) - len(line.lstrip()))
            p_end = line_end - (len(line) - len(line.rstrip()))
            chunks = _read_aloud_segments(text[p_start:p_end], p_start)
            if not chunks:
                chunks = [(p_start, p_end)]
            total_chunks += len(chunks)
            if total_chunks > MAX_CHUNKS_PER_DOC:
                raise ValueError("TOO_MANY_CHUNKS")
            current["paragraphs"].append(
                {
                    "index": len(current["paragraphs"]),
                    "char_start": p_start,
                    "char_end": p_end,
                    "chunks": [
                        {"index": k, "char_start": a, "char_end": b}
                        for k, (a, b) in enumerate(chunks)
                    ],
                }
            )
        offset = line_end + 1

    if current["paragraphs"]:
        chapters.append(current)
    if not chapters:
        chapters = [{"index": 0, "title": "", "char_start": 0, "paragraphs": []}]
    if len(chapters) > MAX_CHAPTERS:
        raise ValueError("TOO_MANY_CHAPTERS")
    for i, ch in enumerate(chapters):
        ch["index"] = i
        ch["char_end"] = (
            chapters[i + 1]["char_start"] if i + 1 < len(chapters) else len(text)
        )
    return chapters


def _flatten(chapters: list[dict]) -> list[list[int]]:
    """[[chapter, paragraph, chunk_start, chunk_end, index_within_chapter], ...]."""
    flat: list[list[int]] = []
    base = 0
    for ch in chapters:
        for para in ch["paragraphs"]:
            for ck in para["chunks"]:
                flat.append([ch["index"], para["index"], ck["char_start"], ck["char_end"],
                             len(flat) - base])
        base = len(flat)
    return flat


def _document_id(title: str, text: str) -> str:
    """内容寻址：重复导入同一本书不会多出一份副本、也不会丢掉进度。"""
    return "doc_" + hashlib.sha1(f"{title}\n{text}".encode("utf-8")).hexdigest()[:10]


def _ingest(title: str, text: str, source_path: str, encoding: str) -> dict:
    if len(text) > MAX_DOC_CHARS:
        raise ValueError("DOC_TOO_LARGE")
    if not text.strip():
        raise ValueError("EMPTY_DOCUMENT")
    ensure_dirs()
    document_id = _document_id(title, text)
    existing = _read_index(document_id)
    if existing.get("chapters"):
        return _meta_public(existing)
    with open(_lib_path(document_id), "w", encoding="utf-8") as fh:
        fh.write(text)
    chapters = _parse(text)
    meta = {
        "document_id": document_id,
        "title": (title or "未命名")[:80],
        "source_path": source_path,
        "encoding": encoding,
        "created_at": _now(),
        "char_count": len(text),
        "chunk_count": sum(len(p["chunks"]) for c in chapters for p in c["paragraphs"]),
        "chapters": chapters,
    }
    _write_index(meta)
    return _meta_public(meta)


def import_text(title: str, text: str) -> dict:
    """§9：粘贴长文本也走同一套索引与进度，不落 WSL。"""
    body = _normalize_str(text or "")
    return _ingest((title or "").strip()[:80] or "粘贴文本", body, "paste", "utf-8")


def import_path(win_path: str) -> dict:
    path = (win_path or "").strip()
    posix = _as_posix(path)
    if not posix or not os.path.isfile(posix):
        raise ValueError("FILE_NOT_FOUND")
    suffix = os.path.splitext(posix)[1].lower()
    if suffix not in SUPPORTED_SUFFIX:
        raise ValueError("UNSUPPORTED_FORMAT")
    size = os.path.getsize(posix)
    if size > MAX_DOC_CHARS * 4:
        raise ValueError("FILE_TOO_LARGE")
    with open(posix, "rb") as fh:
        raw = fh.read()
    text, encoding = _normalize(raw)
    title = os.path.splitext(os.path.basename(posix))[0]
    return _ingest(title, text, path, encoding)


def _as_posix(win_path: str) -> str:
    p = (win_path or "").strip()
    if len(p) > 1 and p[1] == ":":
        return f"/mnt/{p[0].lower()}" + p[2:].replace("\\", "/")
    return p


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------
def document(doc_id: str) -> dict:
    meta = _read_index(doc_id)
    if not meta:
        raise ValueError("DOCUMENT_NOT_FOUND")
    out = _meta_public(meta)
    ch_index = int(out["progress"]["chapter_index"])
    chapters = meta["chapters"]
    out["chapter_titles"] = [
        {"index": c["index"], "title": c["title"] or f"第 {c['index'] + 1} 节",
         "paragraph_count": len(c["paragraphs"])}
        for c in chapters
    ]
    if 0 <= ch_index < len(chapters):
        out["current_chapter"] = _chapter_view(meta, chapters[ch_index])
    return out


def chapter_view(doc_id: str, chapter_index: int) -> dict:
    meta = _read_index(doc_id)
    if not meta:
        raise ValueError("DOCUMENT_NOT_FOUND")
    chapters = meta["chapters"]
    if not (0 <= int(chapter_index) < len(chapters)):
        raise ValueError("CHAPTER_OUT_OF_RANGE")
    return _chapter_view(meta, chapters[int(chapter_index)])


def _chapter_view(meta: dict, chapter: dict) -> dict:
    return {
        "index": chapter["index"],
        "title": chapter["title"] or f"第 {chapter['index'] + 1} 节",
        "chunks": _chapter_chunks(_text(meta["document_id"]), chapter),
    }


def _chapter_chunks(text: str, chapter: dict) -> list[dict]:
    """One chapter's read-aloud units, numbered from 0 within the chapter."""
    return [
        {"index": li, "paragraph": para, "char_start": a, "char_end": b, "text": text[a:b]}
        for _c, para, a, b, li in _flatten([chapter])
    ]


def chunk_span(meta: dict, chapter_index: int, chunk_index: int) -> tuple[int, int]:
    """(char_start, char_end) of one chunk, addressed by chapter-local index."""
    chapters = meta["chapters"]
    if not (0 <= chapter_index < len(chapters)):
        raise ValueError("CHAPTER_OUT_OF_RANGE")
    chunks = _flatten([chapters[chapter_index]])
    if not (0 <= chunk_index < len(chunks)):
        raise ValueError("CHUNK_OUT_OF_RANGE")
    return chunks[chunk_index][2], chunks[chunk_index][3]


def chunk_location(doc_id: str, chapter_index: int, chunk_index: int) -> dict:
    meta = _read_index(doc_id)
    if not meta:
        raise ValueError("DOCUMENT_NOT_FOUND")
    a, b = chunk_span(meta, chapter_index, chunk_index)
    return {
        "document_id": doc_id,
        "chapter_index": int(chapter_index),
        "chunk_index": int(chunk_index),
        "char_start": a,
        "char_end": b,
        "text": _text(doc_id)[a:b],
    }


def _meta_public(meta: dict) -> dict:
    chapters = meta.get("chapters") or []
    return {
        "document_id": meta["document_id"],
        "title": meta["title"],
        "source_path": meta.get("source_path", ""),
        "encoding": meta.get("encoding", ""),
        "created_at": meta.get("created_at", ""),
        "char_count": meta.get("char_count", 0),
        "chunk_count": meta.get("chunk_count", 0),
        "chapter_count": len(chapters),
        "progress": get_progress(meta["document_id"]),
    }


def delete_document(doc_id: str) -> dict:
    """删除一本书（V3_CLEANUP_API_01）：索引 + 原文 + 进度/摘要三处一起收。

    只接受本库自己的 document_id（_safe_id 挡路径片段）；正文文件只动
    `library/<id>.txt`，绝不越界。进度与摘要走同一条 SQLite 连接
    （WAL、busy_timeout 5s），与 7860 在线写入互不冲突。
    不存在时返回 ok=False / DOCUMENT_NOT_FOUND，不抛异常炸路由。
    """
    d = _safe_id(doc_id)
    meta = _read_index(d)
    removed = {"index": False, "text": False, "progress": 0, "summary": 0}
    if not meta:
        return {"ok": False, "reason": "DOCUMENT_NOT_FOUND", "removed": removed}
    try:
        os.unlink(_index_path(d))
        removed["index"] = True
    except FileNotFoundError:
        removed["index"] = True      # 索引本来就没有：视为已清
    try:
        os.unlink(_lib_path(d))
        removed["text"] = True
    except FileNotFoundError:
        removed["text"] = True
    try:
        conn = _db()
        with conn:
            cur = conn.execute(
                "DELETE FROM reading_progress WHERE document_id = ?", (d,))
            removed["progress"] = cur.rowcount
            cur = conn.execute(
                "DELETE FROM reading_chapter_summary WHERE document_id = ?", (d,))
            removed["summary"] = cur.rowcount
    except Exception as exc:
        logger.error("delete progress rows failed (%s): %r", d, exc)
        return {"ok": False, "reason": "PROGRESS_DELETE_FAILED", "removed": removed}
    return {"ok": True, "removed": removed}


def list_documents() -> list[dict]:
    ensure_dirs()
    out = []
    try:
        names = sorted(os.listdir(INDEX_DIR))
    except FileNotFoundError:
        return out
    for name in names:
        if not name.endswith(".json") or name.endswith(".tmp"):
            continue
        meta = _read_index(name[: -len(".json")])
        if meta.get("document_id"):
            out.append(_meta_public(meta))
    out.sort(key=lambda d: str(d.get("created_at") or ""), reverse=True)
    return out


# --------------------------------------------------------------------------
# progress + document summaries (the only reading rows in SQLite, §10)
# --------------------------------------------------------------------------
_conn = None


def _db():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reading_progress (
                document_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                chapter_index INTEGER NOT NULL DEFAULT 0,
                chunk_index INTEGER NOT NULL DEFAULT 0,
                char_start INTEGER NOT NULL DEFAULT 0,
                char_end INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'IDLE',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reading_chapter_summary (
                document_id TEXT NOT NULL,
                chapter_index INTEGER NOT NULL,
                summary TEXT NOT NULL,
                provenance TEXT NOT NULL DEFAULT 'DOCUMENT_SUMMARY',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (document_id, chapter_index)
            );
            """
        )
        conn.commit()
        _conn = conn
    return _conn


def set_progress(doc_id: str, chapter_index: int, chunk_index: int, status: str = "PAUSED") -> dict:
    meta = _read_index(doc_id)
    if not meta:
        raise ValueError("DOCUMENT_NOT_FOUND")
    a, b = chunk_span(meta, int(chapter_index), int(chunk_index))
    conn = _db()
    conn.execute(
        "INSERT INTO reading_progress(document_id, title, chapter_index, chunk_index,"
        " char_start, char_end, status, updated_at) VALUES(?,?,?,?,?,?,?,?)"
        " ON CONFLICT(document_id) DO UPDATE SET title=excluded.title,"
        " chapter_index=excluded.chapter_index, chunk_index=excluded.chunk_index,"
        " char_start=excluded.char_start, char_end=excluded.char_end,"
        " status=excluded.status, updated_at=excluded.updated_at",
        (doc_id, meta["title"], int(chapter_index), int(chunk_index), a, b,
         (status or "PAUSED")[:16], _now()),
    )
    conn.commit()
    return get_progress(doc_id)


def get_progress(doc_id: str) -> dict:
    row = _db().execute(
        "SELECT chapter_index, chunk_index, char_start, char_end, status, updated_at"
        " FROM reading_progress WHERE document_id=?",
        (doc_id,),
    ).fetchone()
    if not row:
        return {"chapter_index": 0, "chunk_index": 0, "char_start": 0, "char_end": 0,
                "status": "UNOPENED", "progress_percent": 0, "updated_at": ""}
    meta = _read_index(doc_id)
    flat = _flatten(meta["chapters"]) if meta else []
    current = next((i for i, ck in enumerate(flat)
                    if ck[0] == int(row[0]) and ck[4] == int(row[1])), 0)
    percent = 100 if row[4] == "DONE" else int(100 * current / max(len(flat), 1))
    return {
        "chapter_index": int(row[0]),
        "chunk_index": int(row[1]),
        "char_start": int(row[2]),
        "char_end": int(row[3]),
        "status": str(row[4]),
        "progress_percent": percent,
        "updated_at": str(row[5]),
    }


def chapter_summary(doc_id: str, chapter_index: int) -> str:
    row = _db().execute(
        "SELECT summary FROM reading_chapter_summary WHERE document_id=? AND chapter_index=?",
        (doc_id, int(chapter_index)),
    ).fetchone()
    return str(row[0]) if row else ""


def set_chapter_summary(doc_id: str, chapter_index: int, summary: str) -> None:
    body = (summary or "").strip()[:600]
    if not body:
        return
    _db().execute(
        "INSERT INTO reading_chapter_summary(document_id, chapter_index, summary, updated_at)"
        " VALUES(?,?,?,?) ON CONFLICT(document_id, chapter_index) DO UPDATE SET"
        " summary=excluded.summary, updated_at=excluded.updated_at",
        (doc_id, int(chapter_index), body, _now()),
    )
    _db().commit()


def previous_summaries(doc_id: str, before_chapter: int, limit: int = 2) -> list[dict]:
    rows = _db().execute(
        "SELECT chapter_index, summary FROM reading_chapter_summary"
        " WHERE document_id=? AND chapter_index<? ORDER BY chapter_index DESC LIMIT ?",
        (doc_id, int(before_chapter), int(limit)),
    ).fetchall()
    return [{"chapter_index": int(c), "summary": s} for c, s in reversed(rows)]


# --------------------------------------------------------------------------
# READING_STATE injection (§15-§17) — DOCUMENT context, never USER memory
# --------------------------------------------------------------------------
def active_reading_state() -> dict:
    rows = _db().execute(
        "SELECT document_id, chapter_index, chunk_index, status, updated_at"
        " FROM reading_progress WHERE status IN ('READING','PAUSED')"
        " ORDER BY updated_at DESC LIMIT 1"
    ).fetchall()
    if not rows:
        return {}
    doc_id, ch, ck, status, updated = rows[0]
    if not _read_index(doc_id):
        return {}
    return {"document_id": doc_id, "chapter_index": int(ch), "chunk_index": int(ck),
            "status": str(status), "updated_at": str(updated)}


def reading_state_block() -> str:
    """拼给模型的 `[正在陪读…]` 块；没有正在读的书就返回空串。

    预算内优先级（§17）：当前问题 > 当前段 > 前后邻居 > 本章摘要 > 前几章摘要。
    超预算先从尾部丢邻居，再丢旧章节摘要。
    """
    try:
        st = active_reading_state()
        if not st:
            return ""
        meta = _read_index(st["document_id"])
        text = _text(st["document_id"])
        chapters = meta["chapters"]
        ci = min(max(st["chapter_index"], 0), len(chapters) - 1)
        # Neighbours are taken from the whole book, so "前 2~3 段" still works at
        # the top of a chapter — that's exactly where a reader asks "接上文是什么".
        flat = _flatten(chapters)
        anchor = next((i for i, row in enumerate(flat) if row[0] == ci), -1)
        k = min(max(st["chunk_index"], 0), max(len(flat) - 1 - max(anchor, 0), 0))
        idx = max(anchor, 0) + k

        def span(i: int) -> str:
            if i < 0 or i >= len(flat):
                return ""
            a, b = flat[i][2], flat[i][3]
            return text[a:b].strip()

        head = "[正在陪读 · 这是文档内容，不是用户说过的话]"
        lines = [
            head,
            "书名：%s；章节：%s；朗读状态：%s"
            % (meta["title"], chapters[ci]["title"] or f"第 {ci + 1} 节", st["status"]),
        ]
        before = [span(idx - j) for j in range(NEIGHBOURS_BEFORE, 0, -1)]
        before = [b for b in before if b]
        if before:
            lines.append("前文：" + " ".join(before[-2:]))
        lines.append("当前正在读的句子：" + (span(idx) or "（本章已读完）"))
        after = [span(idx + j) for j in range(1, NEIGHBOURS_AFTER + 1)]
        after = [a for a in after if a]
        if after:
            lines.append("后文：" + " ".join(after))
        cur_sum = chapter_summary(st["document_id"], ci)
        if cur_sum:
            lines.append("本章摘要：" + cur_sum)
        older = previous_summaries(st["document_id"], ci)
        if older:
            lines.append("前面章节：" + " / ".join(o["summary"] for o in older))
        block = "\n".join(lines)
        if len(block) > READING_BLOCK_MAX_CHARS:
            block = block[:READING_BLOCK_MAX_CHARS].rsplit("\n", 1)[0]
        return block + "\n[以上是书本原文与进度，只能用来回答用户在读的内容；不得当成用户的经历写进长期记忆]"
    except Exception as exc:
        logger.info("reading_state_block unavailable: %r", exc)
        return ""


# --------------------------------------------------------------------------
# §18 lightweight keyword search (no embedding, no vector store)
# --------------------------------------------------------------------------
def search(doc_id: str, query: str, limit: int = 3) -> list[dict]:
    meta = _read_index(doc_id)
    if not meta:
        raise ValueError("DOCUMENT_NOT_FOUND")
    terms = [t for t in re.split(r"[\s,，。、；;：:！？!?()（）\"“”]+", (query or "").strip()) if len(t) >= 2]
    if not terms:
        terms = [query.strip()] if len((query or "").strip()) >= 2 else []
    if not terms:
        return []
    text = _text(doc_id)
    scored: list[tuple[int, int, dict]] = []
    for ci, _p, a, b, li in _flatten(meta["chapters"]):
        body = text[a:b]
        if not body.strip():
            continue
        hits = sum(body.count(t) for t in terms)
        if hits:
            scored.append((hits, a, {"chapter_index": ci, "chunk_index": li,
                                     "text": body[:160], "score": hits}))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [s[2] for s in scored[: max(1, min(int(limit), 10))]]
