"""书库管理路由（AI_GIRLFRIEND_READING_LIBRARY_MANAGE_01 · 2026-10-15）。

只做 7860 侧的「整理」能力，不碰朗读管线本身：
  • 列表增强：进度状态 / 字数 / 内容哈希 / 重复标题与重复内容分组；
  • 重命名：只改索引里的 title，document_id（内容哈希 id）不变；
  • 删除：唯一 ID 批量软删除 → 回收站目录，可恢复 / 可彻底清除；
  • 断点与章节摘要行随条目一起进回收站，恢复时原样放回 SQLite；
  • 正在朗读的条目删除需要二次确认，确认后先停 8765 朗读再删；
  • 全部批量操作写审计 JSONL，任何一步失败都不动下一本。

存储事实（runtime/reading/documents.py 是唯一实现，这里不复制）：
  reading/library/<id>.txt   正文
  reading/index/<id>.json    索引（title / chapters / char_count ...）
  reading/data/reading.sqlite3   reading_progress + reading_chapter_summary
回收站目录：reading/trash/<op_id>/{library,index,progress.json,meta.json}

绝不触碰：用户原始小说文件（source_path 只读记录）、其他书籍数据、
角色记忆、聊天历史。删除范围 = 所选 document_id 的索引 + 正文 + 进度/摘要行。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import reading.documents as rdocs

logger = logging.getLogger("s2s.reading_admin")

router = APIRouter()

# 与 documents.py 同一棵根：reading/
_ROOT = Path(rdocs.READING_ROOT)
_TRASH_DIR = _ROOT / "trash"
_AUDIT_PATH = Path(rdocs.DB_PATH).parent / "library_audit.jsonl"
# 8765 的朗读闸门与 local_api.py 同源；停朗读只此一处，不再发明第二个入口。
S2S_BASE = os.environ.get("S2S_SPEAK_BASE", "http://127.0.0.1:8765").strip().rstrip("/")

_OP_ID_RE = re.compile(r"^op_[0-9]{8}_[0-9]{6}_[a-z0-9]{6}$")


class RenameRequest(BaseModel):
    document_id: str
    title: str


class TrashRequest(BaseModel):
    document_ids: list[str]
    confirm_active: bool = False


class RestoreRequest(BaseModel):
    op_ids: list[str]


class PurgeRequest(BaseModel):
    op_ids: list[str]
    confirm: bool = False


def _bad(exc: Exception):
    return HTTPException(status_code=400, detail={"reason": str(exc)[:160]})


def _audit(record: dict) -> None:
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_AUDIT_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record},
                                ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("library audit write failed: %r", exc)


# ---------------------------------------------------------------- list / enrich

def _content_hash(doc_id: str) -> str:
    path = Path(rdocs._lib_path(doc_id))
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _entries() -> list[dict]:
    metas = rdocs.list_documents()
    rows: list[dict] = []
    for meta in metas:
        doc_id = str(meta.get("document_id") or "")
        if not doc_id:
            continue
        progress = {}
        try:
            progress = rdocs.get_progress(doc_id)
        except Exception as exc:
            logger.warning("progress read failed for %s: %r", doc_id, exc)
        rows.append({
            "document_id": doc_id,
            "title": str(meta.get("title") or ""),
            "created_at": str(meta.get("created_at") or ""),
            "char_count": int(meta.get("char_count") or 0),
            "chunk_count": int(meta.get("chunk_count") or 0),
            "chapter_count": int(meta.get("chapter_count") or 0),
            "source_path": str(meta.get("source_path") or ""),
            "read_status": str(progress.get("status") or "UNOPENED"),
            "progress_percent": int(progress.get("progress_percent") or 0),
            "progress_updated_at": str(progress.get("updated_at") or ""),
            "content_hash": _content_hash(doc_id),
        })
    # 重复标记：同名标题 / 同内容。只在展示层打组号，不做任何自动清理。
    title_counts: dict[str, int] = {}
    hash_counts: dict[str, int] = {}
    for row in rows:
        title_counts[row["title"].strip()] = title_counts.get(row["title"].strip(), 0) + 1
        if row["content_hash"]:
            hash_counts[row["content_hash"]] = hash_counts.get(row["content_hash"], 0) + 1
    title_group: dict[str, int] = {}
    hash_group: dict[str, int] = {}
    for row in rows:
        title = row["title"].strip()
        row["dup_title_group"] = 0
        row["dup_content_group"] = 0
        if title and title_counts.get(title, 0) > 1:
            row["dup_title_group"] = title_group.setdefault(title, len(title_group) + 1)
        if row["content_hash"] and hash_counts.get(row["content_hash"], 0) > 1:
            row["dup_content_group"] = hash_group.setdefault(row["content_hash"], len(hash_group) + 1)
    return rows


@router.get("/api/reading/library/manage")
def library_manage(q: str = "", sort: str = "created", status: str = "all"):
    """管理视图：搜索 / 排序 / 状态筛选都在服务端做，前端只渲染。"""
    try:
        rows = _entries()
    except Exception as exc:
        raise _bad(exc)
    needle = (q or "").strip().lower()
    if needle:
        rows = [r for r in rows if needle in r["title"].lower()]
    if status in ("UNOPENED", "READING", "DONE"):
        rows = [r for r in rows if r["read_status"] == status]
    keys = {
        "created": lambda r: r["created_at"],
        "read": lambda r: r["progress_updated_at"] or r["created_at"],
        "status": lambda r: r["read_status"],
        "title": lambda r: r["title"],
        "chars": lambda r: r["char_count"],
    }
    rows.sort(key=keys.get(sort, keys["created"]), reverse=sort in ("created", "read", "chars"))
    active = ""
    try:
        state = rdocs.active_reading_state() or {}
        active = str(state.get("document_id") or "")
    except Exception:
        pass
    return {
        "documents": rows,
        "total": len(rows),
        "active_document_id": active,
        "duplicate_title_count": len({r["dup_title_group"] for r in rows if r["dup_title_group"]}),
        "duplicate_content_count": len({r["dup_content_group"] for r in rows if r["dup_content_group"]}),
    }


# ---------------------------------------------------------------- rename

@router.post("/api/reading/library/rename")
def library_rename(req: RenameRequest):
    doc_id = rdocs._safe_id(req.document_id)
    title = (req.title or "").strip()
    if not doc_id:
        raise HTTPException(status_code=400, detail={"reason": "EMPTY_DOCUMENT_ID"})
    if not title:
        raise HTTPException(status_code=400, detail={"reason": "EMPTY_TITLE"})
    if len(title) > 200:
        raise HTTPException(status_code=400, detail={"reason": "TITLE_TOO_LONG"})
    meta = rdocs._read_index(doc_id)
    if not meta:
        raise HTTPException(status_code=404, detail={"reason": "DOCUMENT_NOT_FOUND"})
    old_title = str(meta.get("title") or "")
    meta["title"] = title
    try:
        rdocs._write_index(meta)
    except Exception as exc:
        raise _bad(exc)
    # 进度表里的 title 冗余一份展示名，一起改；主键仍是 document_id。
    try:
        conn = rdocs._db()
        with conn:
            conn.execute("UPDATE reading_progress SET title=? WHERE document_id=?",
                         (title, doc_id))
    except Exception as exc:
        logger.warning("progress title sync failed (%s): %r", doc_id, exc)
    _audit({"op": "rename", "document_id": doc_id, "old_title": old_title, "new_title": title})
    return {"ok": True, "document_id": doc_id, "title": title}


# ---------------------------------------------------------------- trash helpers

def _stop_speaking() -> bool:
    try:
        resp = httpx.post(f"{S2S_BASE}/v1/speak/stop", timeout=8.0, trust_env=False)
        return resp.is_success and resp.json().get("ok") is True
    except Exception as exc:
        logger.warning("speak stop failed: %r", exc)
        return False


def _progress_rows(doc_id: str) -> list[dict]:
    conn = rdocs._db()
    rows = []
    for table, cols in (("reading_progress",
                         "title, chapter_index, chunk_index, char_start, char_end, status, updated_at"),
                        ("reading_chapter_summary", "chapter_index, summary")):
        cur = conn.execute(f"SELECT {cols} FROM {table} WHERE document_id=?", (doc_id,))
        for row in cur.fetchall():
            rows.append({"table": table,
                         **dict(zip(cols.replace(" ", "").split(","), row))})
    return rows


def _reinsert_progress(op_dir: Path) -> int:
    payload = op_dir / "progress.json"
    if not payload.exists():
        return 0
    data = json.loads(payload.read_text(encoding="utf-8"))
    conn = rdocs._db()
    n = 0
    with conn:
        for row in data:
            table = row.pop("table")
            doc_id = row.pop("document_id")
            if table == "reading_progress":
                conn.execute(
                    "INSERT OR REPLACE INTO reading_progress(document_id, title, chapter_index,"
                    " chunk_index, char_start, char_end, status, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (doc_id, row.get("title") or "", row["chapter_index"], row["chunk_index"],
                     row["char_start"], row["char_end"], row["status"], row["updated_at"]))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO reading_chapter_summary(document_id, chapter_index, summary)"
                    " VALUES(?,?,?)",
                    (doc_id, row["chapter_index"], row["summary"]))
            n += 1
    return n


# ---------------------------------------------------------------- trash / restore / purge

@router.post("/api/reading/library/trash")
def library_trash(req: TrashRequest):
    """批量软删除：唯一 ID 逐本处理，逐本回报；任何一本失败不影响已完成的书。"""
    ids, seen = [], set()
    for raw in req.document_ids or []:
        d = rdocs._safe_id(raw)
        if d and d not in seen:
            seen.add(d)
            ids.append(d)
    if not ids:
        raise HTTPException(status_code=400, detail={"reason": "EMPTY_SELECTION"})

    active = ""
    try:
        state = rdocs.active_reading_state() or {}
        active = str(state.get("document_id") or "")
    except Exception:
        pass
    hits_active = [d for d in ids if d == active]
    if hits_active and not req.confirm_active:
        titles = []
        for d in hits_active:
            meta = rdocs._read_index(d) or {}
            titles.append(str(meta.get("title") or d))
        return {"ok": False, "reason": "ACTIVE_READING_CONFIRM",
                "active_document_ids": hits_active, "active_titles": titles}

    op_id = "op_" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    op_dir = _TRASH_DIR / op_id
    (op_dir / "library").mkdir(parents=True, exist_ok=True)
    (op_dir / "index").mkdir(parents=True, exist_ok=True)

    stopped = False
    results = []
    for doc_id in ids:
        meta = rdocs._read_index(doc_id)
        if not meta:
            results.append({"document_id": doc_id, "ok": False, "reason": "DOCUMENT_NOT_FOUND"})
            continue
        item: dict = {"document_id": doc_id, "ok": True, "title": str(meta.get("title") or "")}
        try:
            src_txt = Path(rdocs._lib_path(doc_id))
            src_idx = Path(rdocs._index_path(doc_id))
            if doc_id == active and not stopped:
                if not _stop_speaking():
                    raise RuntimeError("STOP_SPEAKING_FAILED")
                stopped = True
                # 8765 侧停下后把断点写成 PAUSED 终态，随条目一起进回收站。
                try:
                    prog = rdocs.get_progress(doc_id)
                    rdocs.set_progress(doc_id, int(prog.get("chapter_index", 0)),
                                       int(prog.get("chunk_index", 0)), "PAUSED")
                except Exception:
                    pass
            item["progress_rows"] = _progress_rows(doc_id)
            (op_dir / "meta.json").write_text(json.dumps({
                "op_id": op_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "results": results + [item], "stopped_speaking": stopped,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            if src_txt.exists():
                shutil.move(str(src_txt), str(op_dir / "library" / src_txt.name))
            if src_idx.exists():
                shutil.move(str(src_idx), str(op_dir / "index" / src_idx.name))
            conn = rdocs._db()
            with conn:
                cur = conn.execute("DELETE FROM reading_progress WHERE document_id=?", (doc_id,))
                item["progress_deleted"] = cur.rowcount
                cur = conn.execute("DELETE FROM reading_chapter_summary WHERE document_id=?", (doc_id,))
                item["summary_deleted"] = cur.rowcount
        except Exception as exc:
            # SQLite rolls back on transaction failure; return moved files too.
            for original in (Path(rdocs._lib_path(doc_id)), Path(rdocs._index_path(doc_id))):
                saved = op_dir / ("library" if original.suffix == ".txt" else "index") / original.name
                if saved.exists() and not original.exists():
                    shutil.move(str(saved), str(original))
            item.update(ok=False, reason=str(exc)[:160])
        results.append(item)

    (op_dir / "meta.json").write_text(json.dumps({
        "op_id": op_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "results": results, "stopped_speaking": stopped,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    ok_count = sum(1 for r in results if r.get("ok"))
    _audit({"op": "trash", "op_id": op_id, "requested": len(ids), "moved": ok_count,
            "document_ids": [r["document_id"] for r in results if r.get("ok")]})
    return {"ok": ok_count > 0, "op_id": op_id, "moved": ok_count, "results": results}


@router.get("/api/reading/library/trash")
def library_trash_list():
    out = []
    try:
        names = sorted(os.listdir(_TRASH_DIR)) if _TRASH_DIR.exists() else []
    except OSError:
        names = []
    for name in reversed(names):
        meta_file = _TRASH_DIR / name / "meta.json"
        if not meta_file.exists() or not _OP_ID_RE.match(name):
            continue
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({
            "op_id": name,
            "created_at": data.get("created_at") or "",
            "items": [
                {"document_id": r.get("document_id"), "title": r.get("title") or "",
                 "progress_rows": len(r.get("progress_rows") or [])}
                for r in data.get("results") or [] if r.get("ok")
            ],
        })
    return {"ops": out, "total": len(out)}


@router.post("/api/reading/library/restore")
def library_restore(req: RestoreRequest):
    """回收站整单恢复：文件搬回 + 进度/摘要行原样写回 SQLite。"""
    restored, errors = [], []
    for op_id in req.op_ids or []:
        op_id = str(op_id or "").strip()
        if not _OP_ID_RE.match(op_id):
            errors.append({"op_id": op_id, "reason": "INVALID_OP_ID"})
            continue
        op_dir = _TRASH_DIR / op_id
        meta_file = op_dir / "meta.json"
        if not meta_file.exists():
            errors.append({"op_id": op_id, "reason": "OP_NOT_FOUND"})
            continue
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            # Preflight the whole operation before moving any file. Never replace
            # a book re-imported since it was moved to trash.
            for result in data.get("results") or []:
                if not result.get("ok") or result.get("restored"):
                    continue
                safe = rdocs._safe_id(str(result.get("document_id") or ""))
                if not safe:
                    raise ValueError("INVALID_DOCUMENT_ID")
                if (Path(rdocs._index_path(safe)).exists() or
                        Path(rdocs._lib_path(safe)).exists()):
                    raise ValueError("RESTORE_CONFLICT: " + safe)
                if not ((op_dir / "index" / f"{safe}.json").exists() and
                        (op_dir / "library" / f"{safe}.txt").exists()):
                    raise ValueError("TRASH_FILES_INCOMPLETE: " + safe)
            moved = []
            for result in data.get("results") or []:
                doc_id = str(result.get("document_id") or "")
                safe = rdocs._safe_id(doc_id)
                if not safe or not result.get("ok") or result.get("restored"):
                    continue
                src_txt = op_dir / "library" / f"{safe}.txt"
                src_idx = op_dir / "index" / f"{safe}.json"
                rows = [dict(r, document_id=safe) for r in result.get("progress_rows") or []]
                (op_dir / "progress.json").write_text(
                    json.dumps(rows, ensure_ascii=False), encoding="utf-8")
                try:
                    shutil.move(str(src_idx), str(rdocs._index_path(safe)))
                    shutil.move(str(src_txt), str(rdocs._lib_path(safe)))
                    _reinsert_progress(op_dir)
                except Exception:
                    for saved, original in ((src_idx, Path(rdocs._index_path(safe))),
                                            (src_txt, Path(rdocs._lib_path(safe)))):
                        if original.exists() and not saved.exists():
                            shutil.move(str(original), str(saved))
                    raise
                (op_dir / "progress.json").unlink(missing_ok=True)
                result["restored"] = True
                meta_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                moved.append(safe)
            if not any((op_dir / "index").iterdir()) and not any((op_dir / "library").iterdir()):
                shutil.rmtree(op_dir, ignore_errors=True)
            restored.append({"op_id": op_id, "document_ids": moved})
            _audit({"op": "restore", "op_id": op_id, "document_ids": moved})
        except Exception as exc:
            errors.append({"op_id": op_id, "reason": str(exc)[:160]})
    return {"ok": not errors, "restored": restored, "errors": errors}


@router.post("/api/reading/library/purge")
def library_purge(req: PurgeRequest):
    """彻底清除回收站整单（不可恢复），必须显式 confirm=true。"""
    if not req.confirm:
        raise HTTPException(status_code=400, detail={"reason": "CONFIRM_REQUIRED"})
    purged, errors = [], []
    for op_id in req.op_ids or []:
        op_id = str(op_id or "").strip()
        if not _OP_ID_RE.match(op_id):
            errors.append({"op_id": op_id, "reason": "INVALID_OP_ID"})
            continue
        op_dir = _TRASH_DIR / op_id
        if not op_dir.exists():
            errors.append({"op_id": op_id, "reason": "OP_NOT_FOUND"})
            continue
        try:
            shutil.rmtree(op_dir)
            purged.append(op_id)
            _audit({"op": "purge", "op_id": op_id})
        except OSError as exc:
            errors.append({"op_id": op_id, "reason": str(exc)[:160]})
    return {"ok": not errors, "purged": purged, "errors": errors}


@router.get("/api/reading/library/audit")
def library_audit(limit: int = 50):
    rows = []
    try:
        with open(_AUDIT_PATH, "r", encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh.readlines()[-min(max(limit, 1), 200):] if line.strip()]
    except (OSError, ValueError):
        pass
    return {"records": rows}
