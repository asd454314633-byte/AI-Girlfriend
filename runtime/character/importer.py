# -*- coding: utf-8 -*-
"""导入小说正文：只吃 TXT 和 Markdown 两种纯文本。

规则：
  * 按章节走，章节标题一定跟着下面的正文一起存，不会切出「只有标题」的一段；
  * 段落是最小单位，段落不拆开；确实太长时才在句号、问号、感叹号、分号处断开，
    绝不从半句话中间切；
  * 每一段都带着「谁的人物、哪份文件、文件校验和、第几章、第几段」，重复导入按校验和跳过；
  * PDF / Word 这类格式本轮不做，给了就用一句中文讲清楚。

不联网，不装任何东西，不叫模型。
"""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any, Iterator

from . import canon, store

HERE = Path(__file__).resolve().parent
LIBRARY_DIR = HERE / "library"

# 自测 / 第二套实例要把「读回来的原文」放到别处去，不去动项目正式人物资料区。
_LIBRARY_OVERRIDE: Path | None = None


def library_dir() -> Path:
    return _LIBRARY_OVERRIDE or LIBRARY_DIR


def set_library_dir(path: Path | str | None) -> None:
    global _LIBRARY_OVERRIDE
    _LIBRARY_OVERRIDE = Path(path) if path else None

SUFFIX_TEXT = {".txt": "txt", ".md": "md", ".markdown": "md"}
# 明确不做的两类，给用户一句人话，不抛奇怪的错误
SUFFIX_LATER = {".pdf", ".doc", ".docx", ".epub", ".mobi", ".azw3", ".rtf", ".wps"}

ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16")

TARGET_CHARS = 620
MAX_CHARS = 900

_HEADING_MD = re.compile(r"^(#{1,6})\s*(.+?)\s*$")
# 「第X章/回/节/卷/篇/折」和带数字的写法都要能认。尾巴必须写在每个分支里面，
# 否则 (?:A|B)[^\n]{0,40} 只会给 B 加上尾巴，「第一章 茶棚」这种就认不出来了。
_HEADING_CN = re.compile(
    r"^\s*(?:"
    r"第[一二三四五六七八九十百千零〇两0-9]{1,9}[章回节卷篇折][^\n]{0,40}"
    r"|(?:楔子|序章|序言|引子|尾声|后记|番外)[^\n]{0,40}"
    r")$"
)
_BLANK = re.compile(r"\n\s*\n")
_SENTENCE_END = "。！？!?；;\n"
_QUOTE_LINES = re.compile(r"^\s*[>\-\*\+]\s+")


def detect_format(path: Path | str) -> tuple[str | None, str | None]:
    """返回 (格式, 给人看的说明)。说明非空时格式一定为 None。"""
    suffix = Path(path).suffix.lower()
    if suffix in SUFFIX_TEXT:
        return SUFFIX_TEXT[suffix], None
    if suffix in SUFFIX_LATER:
        return None, ("这种文件暂时还读不了。目前只认 TXT 和 Markdown 两种纯文本，"
                      "请先把它另存成其中一种再给我。")
    if not suffix:
        return "txt", None
    return None, (f"不认识 {suffix} 这种文件。目前只认 TXT 和 Markdown 两种纯文本，"
                  "请先把它另存成其中一种再给我。")


def read_text(path: Path | str) -> tuple[str, str]:
    """按最常见的几种中文编码挨个试，返回 (正文, 实际用的编码)。"""
    raw = Path(path).read_bytes()
    if not raw:
        raise ValueError("EMPTY")
    last_error: Exception | None = None
    for encoding in ENCODINGS:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError) as exc:
            last_error = exc
            continue
        if text and len(re.sub(r"\s", "", text)) >= 20:
            return text.replace("\r\n", "\n").replace("\r", "\n"), encoding
    if last_error:
        raise ValueError("ENCODING")
    raise ValueError("ENCODING")


class Part:
    """一个待存段落：正文 + 它属于哪一章 + 在原文里的位置。"""

    __slots__ = ("text", "chapter", "heading_path", "start", "end")

    def __init__(self, text: str, chapter: str, heading_path: str, start: int, end: int):
        self.text = text
        self.chapter = chapter
        self.heading_path = heading_path
        self.start = start
        self.end = end


def split_parts(text: str) -> list[Part]:
    """章节 / 段落感知的切分。

    Markdown 用 # 号当标题，纯文本用「第X章」这类中文标题；两种都认。
    标题不单独成段，永远粘在紧随其后的正文前面一起存。
    """
    parts: list[Part] = []
    stack: list[tuple[int, str]] = []      # (层级, 标题)
    chapter = ""
    buffer: list[str] = []
    buffer_start = 0
    pending_heading = ""
    offset = 0

    def heading_path() -> str:
        return " / ".join(title for _level, title in stack)

    def flush(force_chapter: str) -> None:
        nonlocal buffer, buffer_start, pending_heading
        body = "\n\n".join(buffer).strip()
        if not body:
            pending_heading = ""
            buffer_start = 0
            return
        if pending_heading:
            body = pending_heading + "\n" + body
        for piece, piece_start, piece_end in _pack(body, buffer_start):
            parts.append(Part(piece, force_chapter or chapter, heading_path(),
                              piece_start, piece_end))
        buffer = []
        buffer_start = 0
        pending_heading = ""

    for block in _iter_blocks(text):
        block_text, block_start, block_end = block
        offset = block_start
        heading = _as_heading(block_text)
        if heading:
            level, title = heading
            flush(chapter or title if not chapter else chapter)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            chapter = title
            pending_heading = title
            buffer_start = block_start
            continue
        clean = _QUOTE_LINES.sub("", block_text).strip()
        if not clean:
            continue
        if not buffer:
            buffer_start = block_start
        buffer.append(clean)
    flush(chapter)
    return parts


def _iter_blocks(text: str) -> Iterator[tuple[str, int, int]]:
    """按空行切成块，并把每块在原文里的字符位置带出来（出处要用）。"""
    cursor = 0
    for match in _BLANK.finditer(text + "\n\n"):
        chunk = text[cursor:match.start()]
        stripped = chunk.strip()
        if stripped:
            yield stripped, cursor + chunk.index(stripped), cursor + len(chunk)
        cursor = match.end()


def _as_heading(block: str) -> tuple[int, str] | None:
    lines = [line for line in block.splitlines() if line.strip()]
    if not lines:
        return None
    first = lines[0].strip()
    if len(first) > 60:
        return None
    match = _HEADING_MD.match(first)
    if match:
        return len(match.group(1)), match.group(2).strip()
    if _HEADING_CN.match(first):
        # 纯文本里的章节标题一般独立成块；整块只有一行才算标题
        if len(lines) == 1:
            return 2, first
        return 1, first
    return None


def _pack(body: str, start: int) -> Iterator[tuple[str, int, int]]:
    """把一段一段的正文打包成不超过 MAX_CHARS 的块，只在句子边界断开。"""
    current = ""
    cursor = start
    probe = cursor
    for paragraph in body.split("\n\n"):
        piece = paragraph.strip()
        if not piece:
            continue
        if not current:
            candidate = piece
            cand_start = body.index(piece, max(probe - start, 0)) + start
        else:
            candidate = current + "\n\n" + piece
            cand_start = cursor
        if len(candidate) <= MAX_CHARS:
            current = candidate
            cursor = cand_start
            probe = cand_start + len(piece)
            continue
        if current:
            yield current, cursor, cursor + len(current)
            probe = cursor + len(current)
            current = ""
        # 单段就超长：在句子边界上切
        for chunk, chunk_start in _split_long(piece, body.index(piece,
                                                               max(probe - start, 0)) + start):
            yield chunk, chunk_start, chunk_start + len(chunk)
            probe = chunk_start + len(chunk)
        current = ""
    if current:
        yield current, cursor, cursor + len(current)


def _split_long(piece: str, start: int) -> Iterator[tuple[str, int]]:
    """超长段落：优先在句末断，退一步在顿号/逗号断，最后才硬切（并保留已输出位置）。"""
    chunks: list[tuple[str, int]] = []
    cursor = 0
    length = len(piece)
    while cursor < length:
        window = piece[cursor:cursor + TARGET_CHARS]
        if len(piece) - cursor <= MAX_CHARS:
            chunks.append((piece[cursor:].strip(), start + cursor))
            break
        cut = _best_break(window)
        if cut <= 0:
            cut = _soft_break(window)
        if cut <= 0:
            cut = TARGET_CHARS
        chunks.append((window[:cut].strip(), start + cursor))
        cursor += cut
    return iter([chunk for chunk in chunks if chunk[0]])


def _best_break(window: str) -> int:
    for mark in ("。", "！", "？", "；", "…", "\n"):
        index = window.rfind(mark)
        if index >= TARGET_CHARS * 0.45:
            return index + 1
    return 0


def _soft_break(window: str) -> int:
    index = window.rfind("，")
    if index >= TARGET_CHARS * 0.6:
        return index + 1
    index = window.rfind("、")
    if index >= TARGET_CHARS * 0.6:
        return index + 1
    return 0


def sha256_of(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def import_file(path: Path | str, character_id: str | None = None, *,
                db_path: Path | str | None = None, title: str | None = None,
                copy_source: bool = False,
                progress: Any = None) -> dict[str, Any]:
    """导入一份纯文本。返回的「说明」字段是可以直接给用户看的话。

    progress 可选，签名 ``progress(阶段名, 已完成, 总数)``：给界面那条进度条用，
    不传就是原来的一把跑完，行为完全不变。
    """
    def _tick(stage: str, done: int = 0, total: int = 0) -> None:
        if progress is None:
            return
        try:
            progress(stage, int(done), int(total))
        except Exception:      # 进度回调不许把导入本身搞挂
            pass

    source = Path(path)
    fmt, complaint = detect_format(source)
    if fmt is None:
        return {"ok": False, "reason": "UNSUPPORTED_FORMAT", "说明": complaint,
                "文件": source.name}
    if not source.exists():
        return {"ok": False, "reason": "NOT_FOUND",
                "说明": "没找到这个文件，麻烦再确认一下位置。", "文件": source.name}
    character_id = character_id or store.get_active_character(db_path=db_path)
    _tick("正在读这份文本")
    try:
        text, encoding = read_text(source)
    except ValueError as exc:
        code = str(exc)
        message = ("这个文件是空的。" if code == "EMPTY"
                   else "这个文件的文字编码我认不出来，请另存成 UTF-8 的 TXT 再试一次。")
        return {"ok": False, "reason": code, "说明": message, "文件": source.name}

    digest = sha256_of(source)
    conn = store.init_and_connect(db_path)
    try:
        seen = conn.execute("SELECT document_id, title FROM source_document "
                            "WHERE character_id = ? AND sha256 = ?",
                            (character_id, digest)).fetchone()
        if seen is not None:
            return {"ok": True, "skipped": True, "document_id": str(seen["document_id"]),
                    "说明": "这份文本之前已经导入过了，内容一模一样，就不再重复收一遍。",
                    "校验和": digest}
        parts = split_parts(text)
        _tick("正在按章节和段落切开", len(parts), len(parts))
        if not parts:
            return {"ok": False, "reason": "NO_TEXT",
                    "说明": "这个文件里没有读到可以收的正文。"}
        document_id = "doc" + digest[:16]
        stamp = store.now_iso()
        doc_title = (title or source.stem).strip() or source.stem
        if copy_source:
            target = library_dir() / character_id / f"{digest[:12]}_{source.name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
        with conn:
            conn.execute(
                "INSERT INTO source_document (document_id, character_id, title, source_path,"
                " source_name, sha256, format, char_count, part_count, active,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
                (document_id, character_id, doc_title, str(source.resolve()), source.name,
                 digest, fmt, len(text), len(parts), stamp, stamp))
            for ordinal, part in enumerate(parts):
                _tick("正在把原文登记好", ordinal + 1, len(parts))
                conn.execute(
                    "INSERT INTO source_part (document_id, character_id, ordinal, chapter,"
                    " heading_path, text, char_start, char_end) VALUES (?,?,?,?,?,?,?,?)",
                    (document_id, character_id, ordinal, part.chapter, part.heading_path,
                     part.text, part.start, part.end))
            conn.execute("INSERT INTO change_journal (at, action, subject_id, character_id,"
                         " detail) VALUES (?,?,?,?,?)",
                         (stamp, "DOCUMENT_IMPORT", document_id, character_id,
                          f"{source.name}|{digest[:12]}|{len(parts)}"))
        return {"ok": True, "document_id": document_id, "title": doc_title,
                "章节数": len({p.chapter for p in parts if p.chapter}),
                "段落数": len(parts), "字数": len(text), "编码": encoding,
                "校验和": digest, "说明": f"已经收好了，一共 {len(parts)} 段。"}
    finally:
        conn.close()


def remove_file(path: Path | str, character_id: str | None = None, *,
                db_path: Path | str | None = None, hard: bool = False) -> dict[str, Any]:
    source = Path(path)
    if not source.exists():
        return {"ok": False, "说明": "没找到这个文件。"}
    digest = sha256_of(source)
    character_id = character_id or store.get_active_character(db_path=db_path)
    conn = store.init_and_connect(db_path)
    try:
        row = conn.execute("SELECT document_id FROM source_document "
                           "WHERE character_id = ? AND sha256 = ?",
                           (character_id, digest)).fetchone()
        if row is None:
            return {"ok": False, "说明": "这个人物的文本里没有这一份。"}
        document_id = str(row["document_id"])
    finally:
        conn.close()
    return canon.delete_document(document_id, hard=hard, db_path=db_path)


def parts_of(document_id: str, *, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """一篇小说的所有段落（带 part_id，出处要用它当 chunk_id）。"""
    path = Path(db_path) if db_path else store.DB_PATH
    if not path.exists():
        return []
    conn = store.connect(path)
    try:
        rows = conn.execute("SELECT part_id, ordinal, chapter, heading_path, text,"
                            " char_start, char_end FROM source_part WHERE document_id = ?"
                            " ORDER BY ordinal", (document_id,)).fetchall()
        return [{"part_id": int(r["part_id"]), "chunk_id": f"p{int(r['part_id'])}",
                 "ordinal": int(r["ordinal"]), "chapter": str(r["chapter"]),
                 "heading_path": str(r["heading_path"]), "text": str(r["text"]),
                 "char_start": int(r["char_start"]), "char_end": int(r["char_end"])}
                for r in rows]
    finally:
        conn.close()


def location_of(part: dict[str, Any], *, title: str = "") -> str:
    """给人看的出处：书名 · 第几章 · 第几段 · 第几字（§11 的 location）。"""
    bits: list[str] = []
    if title:
        bits.append(str(title))
    chapter = str(part.get("chapter") or "").strip()
    bits.append(chapter or f"第 {int(part.get('ordinal') or 0) + 1} 段")
    bits.append(f"第 {int(part.get('ordinal') or 0) + 1} 段")
    bits.append(f"第 {int(part.get('char_start') or 0)}—{int(part.get('char_end') or 0)} 字")
    return " · ".join(str(one) for one in bits if one)


def import_text(text: str, title: str = "", character_id: str | None = None, *,
                db_path: Path | str | None = None,
                progress: Any = None) -> dict[str, Any]:
    """用户在页面上直接粘贴的正文：先落到项目内的人物资料区，再走同一个导入入口。

    只写进 runtime/character/library/，绝不去翻用户磁盘上的别的目录（§23）。
    """
    body = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(re.sub(r"\s", "", body)) < 20:
        return {"ok": False, "reason": "EMPTY", "说明": "粘贴的内容太短，没有可以收的正文。"}
    character_id = character_id or store.get_active_character(db_path=db_path)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    name = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", (title or "粘贴的文本").strip())[:60] or "粘贴的文本"
    target = library_dir() / character_id / f"{digest[:12]}_{name}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(body, encoding="utf-8")
    result = import_file(target, character_id, db_path=db_path, title=(title or name),
                         progress=progress)
    if result.get("ok"):
        result["来源"] = str(target.relative_to(HERE)) if target.is_relative_to(HERE) else str(target)
    return result


def register_setting_facts(document_id: str, facts: list[dict[str, Any]], *,
                           character_id: str | None = None,
                           db_path: Path | str | None = None) -> dict[str, Any]:
    """把「从这本书里读出来的设定」一条条登记进设定层。

    facts 里每条至少要有 text，可带 kind / weight / ordinal。这一步是人工或工具整理好
    的结果，导入器自己不会去猜设定。
    """
    character_id = character_id or store.get_active_character(db_path=db_path)
    added = merged = 0
    for fact in facts:
        result = canon.add_fact(str(fact.get("text") or ""), character_id=character_id,
                                kind=str(fact.get("kind") or "setting"),
                                origin="novel", document_id=document_id,
                                ordinal=int(fact.get("ordinal") or 0),
                                weight=int(fact.get("weight") or 120),
                                confidence=float(fact.get("confidence") or 0.95),
                                db_path=db_path)
        if not result.get("ok"):
            continue
        merged += 1 if result.get("合并了") else 0
        added += 1
    return {"ok": True, "登记条数": added, "其中并入已有": merged}
