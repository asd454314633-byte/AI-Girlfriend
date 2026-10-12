# -*- coding: utf-8 -*-
"""设定层（CANON）：人物世界观里「不会因为聊天而改变」的那部分。

两类内容：
  * 设定事实 —— 一条条写清楚的来历、关系、地点、时间线；
  * 小说原文 —— 由 importer 切好段落灌进来的正文，检索时给出处。

这一层对聊天是只读的：记忆抽取永远不能改写这里（判定接口 similar_to_canon）。
"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any, Sequence

from . import store
from runtime.memory import retrieval

CANON = retrieval.LAYER_CANON
KIND_LABEL = {
    "setting": "设定", "relationship": "关系", "place": "地点", "timeline": "经过",
    "habit": "习惯", "fact": "事实", "style": "说话方式",
}

# 任务 §11 要求人物分析覆盖的 17 个维度。写成一份表，是为了：
#   * 界面按这一份渲染分组，不在前端复制第二份（§0：一处只有一份权威）；
#   * 摘要按 tier 决定谁一定出场（tier=1 的核心几项），谁只在被问到才拿出来；
#   * analyzer 每产出一条都必须落在这张表里，否则自测直接判不过。
# basis 说明这一项通常能不能拿到原文证据：canon=能引原句，inferred=只能统计推断。
DIMENSIONS: dict[str, dict[str, Any]] = {
    "identity":        {"label_zh": "身份", "tier": 1, "basis": "canon"},
    "background":      {"label_zh": "背景", "tier": 1, "basis": "canon"},
    "relationship":    {"label_zh": "关系", "tier": 1, "basis": "canon"},
    "experience":      {"label_zh": "重大经历", "tier": 1, "basis": "canon"},
    "personality":     {"label_zh": "性格", "tier": 1, "basis": "inferred"},
    "values":          {"label_zh": "价值观", "tier": 2, "basis": "inferred"},
    "desire":          {"label_zh": "欲望", "tier": 2, "basis": "inferred"},
    "fear":            {"label_zh": "恐惧", "tier": 2, "basis": "inferred"},
    "conflict_style":  {"label_zh": "冲突方式", "tier": 2, "basis": "inferred"},
    "thinking_style":  {"label_zh": "思考方式", "tier": 2, "basis": "inferred"},
    "speech_habit":    {"label_zh": "说话习惯", "tier": 1, "basis": "canon"},
    "vocabulary":      {"label_zh": "用词习惯", "tier": 2, "basis": "canon"},
    "sentence_length": {"label_zh": "句子长度", "tier": 2, "basis": "canon"},
    "stance":          {"label_zh": "讽刺/直接/回避倾向", "tier": 2, "basis": "inferred"},
    "addressee_style": {"label_zh": "不同对象下的说话方式", "tier": 3, "basis": "inferred"},
    "growth":          {"label_zh": "人物成长阶段", "tier": 3, "basis": "canon"},
    "quotes":          {"label_zh": "关键原著台词", "tier": 2, "basis": "canon"},
}
DIMENSION_LABELS = {key: str(value["label_zh"]) for key, value in DIMENSIONS.items()}
BASIS_LABELS = {"canon": "原著里写得有", "inferred": "按文本统计推断", "manual": "用户补充"}
SUMMARY_TIERS = (1, 2)      # 摘要里只出场前两层，第三层留给检索（§18：不许把整本塞进去）
SUMMARY_LIMIT_CHARS = 520   # 兜底上限；正式装配时按预算比例现算（context_builder 传进来）


def dimension_label(dimension: str) -> str:
    return DIMENSION_LABELS.get(str(dimension or ""), "事实")


def is_dimension(dimension: str) -> bool:
    return str(dimension or "") in DIMENSIONS


def new_id(prefix: str = "cf") -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# 设定事实
# ---------------------------------------------------------------------------

def add_fact(text: str, *, character_id: str | None = None, kind: str = "setting",
             origin: str = "manual", document_id: str = "", ordinal: int = 0,
             weight: int = 100, confidence: float = 0.9, user_editable: bool = True,
             dimension: str = "", basis: str = "canon", source_id: str = "",
             chunk_id: str = "", location: str = "", evidence: str = "",
             db_path: Path | str | None = None) -> dict[str, Any]:
    body = (text or "").strip()
    if not body:
        return {"ok": False, "说明": "要写的设定是空的。"}
    dimension = str(dimension or "")
    if dimension and not is_dimension(dimension):
        # 维度必须落在 §11 那一张表里，否则界面分组会漏掉这一条
        return {"ok": False, "说明": f"不认得「{dimension}」这一类分析结果。",
                "可选": sorted(DIMENSION_LABELS)}
    basis = str(basis or "canon")
    if basis not in BASIS_LABELS:
        basis = "canon"
    character_id = character_id or store.get_active_character(db_path=db_path)
    path = Path(db_path) if db_path else store.DB_PATH
    near = similar_to_canon(body, character_id=character_id, db_path=path)
    if near and near["相似度"] >= 0.92:
        # 同一条设定再说一遍：只把权重和把握抬一下，不新增；证据缺失时顺手补齐
        conn = store.init_and_connect(path)
        try:
            stamp = store.now_iso()
            with conn:
                conn.execute("UPDATE canon_fact SET weight = MAX(weight, ?), confidence = ?, "
                             "evidence = CASE WHEN evidence = '' THEN ? ELSE evidence END,"
                             " location = CASE WHEN location = '' THEN ? ELSE location END,"
                             " chunk_id = CASE WHEN chunk_id = '' THEN ? ELSE chunk_id END,"
                             " source_id = CASE WHEN source_id = '' THEN ? ELSE source_id END,"
                             " updated_at = ? WHERE fact_id = ?",
                             (max(weight, 100), max(confidence, near["confidence"]),
                              evidence, location, chunk_id, source_id, stamp,
                              near["fact_id"]))
            return {"ok": True, "fact_id": near["fact_id"], "合并了": True}
        finally:
            conn.close()
    fact_id = new_id()
    stamp = store.now_iso()
    conn = store.init_and_connect(db_path)
    try:
        with _write_lock(path), conn:
            conn.execute(
                "INSERT INTO canon_fact (fact_id, character_id, kind, dimension, text,"
                " norm_text, origin, basis, document_id, source_id, chunk_id, location,"
                " evidence, ordinal, weight, confidence, user_editable, active,"
                " created_at, updated_at) VALUES"
                " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                (fact_id, character_id, kind, dimension, body,
                 retrieval.normalize_text(body), origin, basis, document_id,
                 source_id or document_id, chunk_id, location, evidence, int(ordinal),
                 int(weight), float(confidence), 1 if user_editable else 0,
                 stamp, stamp))
        return {"ok": True, "fact_id": fact_id, "合并了": False}
    finally:
        conn.close()


def _write_lock(db_path: Path | str | None = None):
    """按库文件加进程内写锁：锁的键必须是真正要写的那个库。"""
    return retrieval.write_lock_for(Path(db_path) if db_path else store.DB_PATH)


def list_facts(character_id: str | None = None, *, include_archived: bool = False,
               dimension: str | None = None, basis: str | None = None,
               tiers: Sequence[int] | None = None,
               db_path: Path | str | None = None) -> list[dict[str, Any]]:
    character_id = character_id or store.get_active_character(db_path=db_path)
    path = Path(db_path) if db_path else store.DB_PATH
    if not path.exists():
        return []
    conn = store.connect(path)
    try:
        sql = "SELECT * FROM canon_fact WHERE character_id = ?"
        args: list[Any] = [character_id]
        if not include_archived:
            sql += " AND active = 1"
        if dimension:
            sql += " AND dimension = ?"
            args.append(dimension)
        if basis:
            sql += " AND basis = ?"
            args.append(basis)
        if tiers:
            names = [key for key, value in DIMENSIONS.items() if value["tier"] in set(tiers)]
            if not names:
                return []
            sql += f" AND dimension IN ({', '.join('?' for _ in names)})"
            args.extend(names)
        sql += " ORDER BY weight DESC, confidence DESC, created_at ASC"
        return [_fact_row(row) for row in conn.execute(sql, args)]
    finally:
        conn.close()


def _fact_row(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    def _text(name: str) -> str:
        return str(row[name]) if name in keys else ""
    return {
        "fact_id": _text("fact_id"),
        "character_id": _text("character_id"),
        "kind": _text("kind"),
        "dimension": _text("dimension"),
        "维度": dimension_label(_text("dimension")),
        "text": _text("text"),
        "origin": _text("origin"),
        "basis": _text("basis"),
        "依据": BASIS_LABELS.get(_text("basis"), "原著里写得有"),
        "document_id": _text("document_id"),
        "source_id": _text("source_id"),
        "chunk_id": _text("chunk_id"),
        "location": _text("location"),
        "evidence": _text("evidence"),
        "ordinal": int(row["ordinal"] or 0) if "ordinal" in keys else 0,
        "weight": int(row["weight"] or 0) if "weight" in keys else 0,
        "confidence": float(row["confidence"] or 0.0) if "confidence" in keys else 0.0,
        "user_editable": bool(row["user_editable"]) if "user_editable" in keys else True,
        "active": bool(row["active"]) if "active" in keys else True,
        "created_at": _text("created_at"),
        "updated_at": _text("updated_at"),
    }


def update_fact(fact_id: str, *, text: str | None = None, kind: str | None = None,
                weight: int | None = None, dimension: str | None = None,
                basis: str | None = None, evidence: str | None = None,
                location: str | None = None,
                character_id: str | None = None,
                db_path: Path | str | None = None) -> dict[str, Any]:
    path = Path(db_path) if db_path else store.DB_PATH
    conn = store.connect(path)
    try:
        row = conn.execute("SELECT * FROM canon_fact WHERE fact_id = ?", (fact_id,)).fetchone()
        if row is None:
            return {"ok": False, "说明": "没有找到这条设定。"}
        if not row["user_editable"]:
            return {"ok": False, "说明": "这条是原著里写定的，只能另开一条补充说明。"}
        if dimension is not None and dimension and not is_dimension(dimension):
            return {"ok": False, "说明": f"不认得「{dimension}」这一类分析结果。"}
        if basis is not None and basis and basis not in BASIS_LABELS:
            return {"ok": False, "说明": "不认得这种依据类型。"}
        sets: list[str] = []
        args: list[Any] = []
        if text is not None:
            sets += ["text = ?", "norm_text = ?"]
            args += [text.strip(), retrieval.normalize_text(text)]
        for column, value in (("kind", kind), ("dimension", dimension), ("basis", basis),
                              ("evidence", evidence), ("location", location),
                              ("weight", weight)):
            if value is None:
                continue
            sets.append(f"{column} = ?")
            args.append(int(value) if column == "weight" else value)
        if not sets:
            return {"ok": True, "fact_id": fact_id, "改动": 0}
        sets.append("updated_at = ?")
        args.append(store.now_iso())
        args.append(fact_id)
        with conn:
            conn.execute(f"UPDATE canon_fact SET {', '.join(sets)} WHERE fact_id = ?", args)
        return {"ok": True, "fact_id": fact_id, "改动": len(sets) - 1}
    finally:
        conn.close()


def delete_fact(fact_id: str, *, hard: bool = False,
                db_path: Path | str | None = None) -> dict[str, Any]:
    """默认只是收起来（软删），还原这个人物时会一起回来。"""
    conn = store.init_and_connect(db_path)
    try:
        stamp = store.now_iso()
        with conn:
            if hard:
                cur = conn.execute("DELETE FROM canon_fact WHERE fact_id = ?", (fact_id,))
            else:
                cur = conn.execute("UPDATE canon_fact SET active = 0, deleted_at = ?,"
                                   " updated_at = ? WHERE fact_id = ?", (stamp, stamp, fact_id))
            if cur.rowcount:
                conn.execute("INSERT INTO change_journal (at, action, subject_id)"
                             " VALUES (?,?,?)", (stamp, "CANON_DELETE", fact_id))
        return {"ok": bool(cur.rowcount), "fact_id": fact_id}
    finally:
        conn.close()


def delete_character_facts(character_id: str, *, keep_dimensions: Sequence[str] = (),
                           hard: bool = False,
                           db_path: Path | str | None = None) -> dict[str, Any]:
    """整套替换之前先清掉这个人物的设定条目（默认软删，还原人物时会一起回来）。

    只给 bundle.import_bundle(replace=True) 用；界面上「删除人物」走 store 那边，
    两边共用同一张表的 active 标记，不另立一套删除口径。
    """
    conn = store.init_and_connect(db_path)
    try:
        stamp = store.now_iso()
        sql = "SELECT fact_id FROM canon_fact WHERE character_id = ? AND active = 1"
        args: list[Any] = [character_id]
        if keep_dimensions:
            names = [str(one) for one in keep_dimensions if str(one) in DIMENSIONS]
            if names:
                sql += f" AND dimension NOT IN ({', '.join('?' for _ in names)})"
                args.extend(names)
        targets = [str(row["fact_id"]) for row in conn.execute(sql, args)]
        if not targets:
            return {"ok": True, "条数": 0, "说明": "本来就没有要替换的设定条目。"}
        marks = ", ".join("?" for _ in targets)
        with conn:
            if hard:
                cur = conn.execute(f"DELETE FROM canon_fact WHERE fact_id IN ({marks})",
                                   targets)
            else:
                cur = conn.execute(f"UPDATE canon_fact SET active = 0, deleted_at = ?,"
                                   f" updated_at = ? WHERE fact_id IN ({marks})",
                                   [stamp, stamp, *targets])
            conn.execute("INSERT INTO change_journal (at, action, subject_id, character_id,"
                         " detail) VALUES (?,?,?,?,?)",
                         (stamp, "CANON_CLEAR", character_id, character_id,
                          str(cur.rowcount)))
        return {"ok": True, "条数": cur.rowcount,
                "说明": f"先把 {cur.rowcount} 条旧设定收起来了。"}
    finally:
        conn.close()


def similar_to_canon(text: str, *, character_id: str | None = None,
                     db_path: Path | str | None = None,
                     threshold: float = 0.62) -> dict[str, Any]:
    """「聊天里说的话是不是已经在设定里写过了」——抽取器要靠这个闸门守住设定层。

    只比对设定事实：小说原文由 importer 单独登记，聊天里的一句话不可能和整段原文撞车，
    真撞上了也说明那句话本来就是从原文抄来的，那时它同样不该变成新记忆。
    """
    character_id = character_id or store.get_active_character(db_path=db_path)
    best: dict[str, Any] = {"相似度": 0.0, "出处": ""}
    for fact in list_facts(character_id, db_path=db_path):
        score = retrieval.similarity(text, fact["text"])
        if score > best["相似度"]:
            best = {"相似度": score, "fact_id": fact["fact_id"], "text": fact["text"],
                    "kind": fact["kind"], "confidence": fact["confidence"],
                    "出处": "setting"}
        if best["相似度"] >= 0.98:
            break
    if best["相似度"] < threshold:
        return {"相似度": best["相似度"], "出处": ""}
    return best


# ---------------------------------------------------------------------------
# 小说原文的读接口（给检索工具和上下文用）
# ---------------------------------------------------------------------------

def list_documents(character_id: str | None = None, *,
                   db_path: Path | str | None = None) -> list[dict[str, Any]]:
    character_id = character_id or store.get_active_character(db_path=db_path)
    path = Path(db_path) if db_path else store.DB_PATH
    if not path.exists():
        return []
    conn = store.connect(path)
    try:
        rows = conn.execute("SELECT * FROM source_document WHERE character_id = ? "
                            "ORDER BY created_at", (character_id,)).fetchall()
        return [{"document_id": str(r["document_id"]), "title": str(r["title"]),
                 "source_name": str(r["source_name"]), "sha256": str(r["sha256"]),
                 "format": str(r["format"]), "char_count": int(r["char_count"]),
                 "part_count": int(r["part_count"]), "active": bool(r["active"]),
                 "created_at": str(r["created_at"])} for r in rows]
    finally:
        conn.close()


def read_window(document_id: str, offset: int = 0, chars: int = 1200, *,
                db_path: Path | str | None = None) -> dict[str, Any]:
    """按字数窗口读一篇原文，给「按段读取」的工具入口用。"""
    chars = max(1, min(int(chars or 1200), 3000))
    offset = max(0, int(offset or 0))
    path = Path(db_path) if db_path else store.DB_PATH
    if not path.exists():
        return {"ok": False, "说明": "还没有导入过任何文本。"}
    conn = store.connect(path)
    try:
        doc = conn.execute("SELECT * FROM source_document WHERE document_id = ?",
                           (document_id,)).fetchone()
        if doc is None:
            return {"ok": False, "说明": "没有这篇原文。"}
        rows = conn.execute("SELECT ordinal, chapter, text FROM source_part "
                            "WHERE document_id = ? ORDER BY ordinal", (document_id,)).fetchall()
        body = "".join(str(r["text"]) for r in rows)
        window = body[offset:offset + chars]
        chapter = ""
        cursor = 0
        for row in rows:
            nxt = cursor + len(str(row["text"]))
            if offset < nxt:
                chapter = str(row["chapter"])
                break
            cursor = nxt
        return {"ok": True, "document_id": document_id, "title": str(doc["title"]),
                "chapter": chapter, "text": window, "offset": offset,
                "next_offset": offset + len(window),
                "at_end": offset + len(window) >= len(body), "total_chars": len(body)}
    finally:
        conn.close()


def delete_document(document_id: str, *, hard: bool = False,
                    db_path: Path | str | None = None) -> dict[str, Any]:
    conn = store.init_and_connect(db_path)
    try:
        stamp = store.now_iso()
        with conn:
            if hard:
                conn.execute("DELETE FROM source_part WHERE document_id = ?", (document_id,))
                conn.execute("DELETE FROM source_document WHERE document_id = ?", (document_id,))
                conn.execute("DELETE FROM canon_fact WHERE document_id = ?", (document_id,))
            else:
                conn.execute("UPDATE source_document SET active = 0, updated_at = ? "
                             "WHERE document_id = ?", (stamp, document_id))
                conn.execute("UPDATE canon_fact SET active = 0, deleted_at = ?, updated_at = ? "
                             "WHERE document_id = ?", (stamp, stamp, document_id))
        return {"ok": True, "document_id": document_id}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 检索用的表结构 + 摘要
# ---------------------------------------------------------------------------

def canon_fact_spec(character_id: str) -> retrieval.IndexSpec:
    return retrieval.IndexSpec(
        table="canon_fact", fts="canon_fts", text_column="text", key_column="fact_id",
        extra_select="canon_fact.kind AS kind, canon_fact.origin AS origin,"
                     " canon_fact.dimension AS dimension, canon_fact.basis AS basis,"
                     " canon_fact.location AS location, canon_fact.evidence AS evidence,"
                     " canon_fact.source_id AS source_id, canon_fact.chunk_id AS chunk_id,"
                     " canon_fact.document_id AS document_id, 0 AS ordinal,"
                     " '' AS chapter, canon_fact.confidence AS confidence",
        where="canon_fact.active = 1 AND canon_fact.character_id = ?",
        params=(character_id,),
    )


def canon_part_spec(character_id: str) -> retrieval.IndexSpec:
    """原文段落的表结构描述。

    注意：段落要 JOIN 出处表才拿得到书名，通用检索内核不做特例 JOIN，所以实际查询走
    _search_parts()，这个 spec 只留给「只看段落本体」的场景（比如导出、统计）。
    """
    return retrieval.IndexSpec(
        table="source_part", fts="part_fts", text_column="text", key_column="part_id",
        extra_select="source_part.document_id AS document_id,"
                     " source_part.ordinal AS ordinal, source_part.chapter AS chapter",
        where="source_part.character_id = ?",
        params=(character_id,),
    )


def canon_passages(query: str, character_id: str | None = None, *, limit: int = 6,
                   recent_terms: Sequence[str] | None = None,
                   db_path: Path | str | None = None) -> list[Any]:
    """设定层检索：事实优先、原文补充，返回带出处的 passage。

    recent_terms 是「最近几轮正在聊的词」（§12 的第三条排序依据）：给了就在层内加权，
    不给就完全退回原来的 bm25 顺序，不会凭空改变结果。
    """
    character_id = character_id or store.get_active_character(db_path=db_path)
    path = Path(db_path) if db_path else store.DB_PATH
    fact_rows = retrieval.search_index(path, canon_fact_spec(character_id), query, limit=limit)
    part_rows = _search_parts(path, character_id, query, limit)
    if recent_terms:
        picked = [str(one) for one in recent_terms if one]
        fact_rows = retrieval.boost_by_rows(fact_rows, picked)
        part_rows = retrieval.boost_by_rows(part_rows, picked)
    fused = retrieval.fuse([(CANON, fact_rows), (CANON, part_rows)], {CANON: 1.0}, limit)
    return [retrieval.to_passage(k, b, s, e) for k, b, s, e in fused]


def _search_parts(path: Path, character_id: str, query: str | None,
                  limit: int) -> list[tuple[str, str, float, dict[str, Any]]]:
    """原文段落：因为要 JOIN source_document 拿标题，这里单开一条查询。"""
    plan = retrieval.plan_query(query)
    if plan.empty or not path.exists():
        return []
    conn = retrieval.open_db(path, read_only=True)
    if conn is None:
        return []
    try:
        out: dict[str, tuple[str, str, float, dict[str, Any]]] = {}
        expr = plan.match_expr()
        if expr:
            sql = ("SELECT p.part_id AS k, p.text AS body, -bm25(part_fts) AS score,"
                   " p.document_id AS document_id, p.ordinal AS ordinal,"
                   " p.chapter AS chapter, d.title AS title"
                   " FROM part_fts JOIN source_part p ON p.part_id = part_fts.rowid"
                   " JOIN source_document d ON d.document_id = p.document_id"
                   f" WHERE part_fts MATCH ? AND p.character_id = ? AND d.active = 1"
                   f" ORDER BY bm25(part_fts) LIMIT {max(limit * 4, 20)}")
            for row in conn.execute(sql, (expr, character_id)):
                extra = {"document_id": row["document_id"], "ordinal": row["ordinal"],
                         "chapter": row["chapter"], "title": row["title"],
                         "source_kind": "document"}
                out[str(row["k"])] = (str(row["k"]), str(row["body"]),
                                      float(row["score"]), extra)
        if plan.like_terms and len(out) < limit:
            floor = min((v[2] for v in out.values()), default=0.0)
            for index, term in enumerate(plan.like_terms):
                sql = ("SELECT p.part_id AS k, p.text AS body,"
                       " p.document_id AS document_id, p.ordinal AS ordinal,"
                       " p.chapter AS chapter, d.title AS title"
                       " FROM source_part p JOIN source_document d"
                       " ON d.document_id = p.document_id"
                       " WHERE p.text LIKE ? ESCAPE '\\' AND p.character_id = ?"
                       " AND d.active = 1 LIMIT ?")
                for row in conn.execute(sql, (f"%{retrieval._escape_like(term)}%",
                                              character_id, limit * 2)):
                    key = str(row["k"])
                    if key in out:
                        continue
                    extra = {"document_id": row["document_id"], "ordinal": row["ordinal"],
                             "chapter": row["chapter"], "title": row["title"],
                             "source_kind": "document"}
                    out[key] = (key, str(row["body"]),
                                floor - (10.0 - len(term)) - index, extra)
        return sorted(out.values(), key=lambda item: item[2], reverse=True)[:limit]
    except sqlite3.Error as exc:
        # 查不到可以，但必须是「真的没有」而不是「SQL 写错了」：报错留在 DIAGNOSTICS 里。
        retrieval._diagnose("part_fts:local", exc)
        return []
    finally:
        conn.close()


def summarize(character_id: str | None = None, *, max_chars: int = SUMMARY_LIMIT_CHARS,
              tiers: Sequence[int] = SUMMARY_TIERS, db_path: Path | str | None = None) -> str:
    """把设定压成一段能直接给人念的话。只有事实，没有评论，也没有「根据设定」这类话。

    只出场 tiers 里那几层（默认核心 + 次级），成长阶段/逐对象说话方式这类长尾内容留给
    检索按问题取，不参与每轮装配 —— §18 原话：禁止无脑把整本小说塞进去。
    """
    facts = list_facts(character_id, tiers=tiers, db_path=db_path)
    if not facts:
        return ""
    lines: list[str] = []
    used = 0
    for fact in facts:
        label = fact["维度"] or KIND_LABEL.get(fact["kind"], "事实")
        body = fact["text"].strip().rstrip("。")
        line = f"{label}：{body}。"
        if used + len(line) > max_chars:
            continue
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


def by_dimension(character_id: str | None = None, *, include_archived: bool = False,
                 db_path: Path | str | None = None) -> dict[str, list[dict[str, Any]]]:
    """按 §11 的 17 个维度分组返回（界面就照这一份分组，不复制第二份表）。"""
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in DIMENSIONS}
    for fact in list_facts(character_id, include_archived=include_archived, db_path=db_path):
        key = str(fact.get("dimension") or "")
        if key in grouped:
            grouped[key].append(fact)
    return grouped


def coverage(character_id: str | None = None, *,
             db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """哪些维度已经有资料、哪些还空着 —— 界面用它提示「这几项书里没写到」。"""
    grouped = by_dimension(character_id, db_path=db_path)
    out = []
    for key, meta in DIMENSIONS.items():
        rows = grouped.get(key) or []
        out.append({"dimension": key, "label_zh": meta["label_zh"], "tier": meta["tier"],
                    "能拿到原文证据": meta["basis"] == "canon",
                    "条数": len(rows),
                    "其中原著事实": sum(1 for one in rows if one["basis"] == "canon"),
                    "其中推断": sum(1 for one in rows if one["basis"] == "inferred")})
    return out
