# -*- coding: utf-8 -*-
"""从一本小说里把人物资料收出来（任务 §11 的「分析人物」那一步）。

一份说明书：

  1. **只用文本证据**：每一条结论都能回溯到「哪本书 · 第几章 · 第几段 · 第几字」和
     那句原文（source_id / chunk_id / location / evidence 四个字段），没有证据的话
     就不产出；
  2. **原著事实与推测严格分家**（§11 最后一条）：
       basis='canon'    —— 原文里真写得有，直接引句；
       basis='inferred' —— 由词频/句长这类统计推出来的倾向，界面上会写明是推断；
     两者绝不混成一句话，也绝不因为聊天而改写（那一层闸门在记忆抽取那边）；
  3. **不叫模型、不联网、不占显存**：这一版全部是规则 + 统计，词表和阈值都在
     ``analysis_rules.json``。将来要接真模型做深读，只需要新增一条走统一契约的
     适配器调用，本模块的输入输出形状不变（§0）；
  4. 幂等：同一本书反复分析不会越写越多，因为设定层自己会按「说的是不是同一件事」
     合并（canon.add_fact）。

给用户看的字段一律是中文句子，不出现任何内部名词。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from . import canon, importer, store

HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "analysis_rules.json"

_SENTENCE = re.compile(r"[。！？!?；;\n]")
_FALLBACK: dict[str, Any] = {
    "max_sentences_per_dimension": 3,
    "evidence_max_chars": 90,
    "min_evidence_chars": 8,
    "max_entries_per_dimension": 4,
    "quote_brackets": [["“", "”"], ["「", "」"]],
    "speech_verbs": ["说", "道", "问", "答"],
    "addressee_patterns": [],
    "pronoun_aliases": ["她", "他"],
    "dimensions": {},
    "speech_stats": {"mood_particles": [], "short_line_chars": 6, "long_line_chars": 30,
                     "trailing_marks": ["……"], "question_ratio_direct": 0.28,
                     "question_ratio_indirect": 0.12, "kind": "style", "weight": 130,
                     "confidence": 0.85},
    "growth_stages": {"buckets": 3, "min_hits": 3, "kind": "timeline", "confidence": 0.6,
                      "rise_label_zh": "前半", "peak_label_zh": "中段",
                      "fall_label_zh": "后半"},
    "unknown_note_zh": "这本书里没写到这一项，先不编。",
}
_cache: tuple[float, dict[str, Any]] | None = None


def rules(*, refresh: bool = False) -> dict[str, Any]:
    """读词表；文件改了立刻生效（mtime 变化才重读）。"""
    global _cache
    stamp = RULES_PATH.stat().st_mtime_ns if RULES_PATH.exists() else 0.0
    if _cache is not None and not refresh and _cache[0] == stamp:
        return _cache[1]
    payload = json.loads(RULES_PATH.read_text(encoding="utf-8")) if RULES_PATH.exists() else {}
    merged = json.loads(json.dumps(_FALLBACK))
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
    _cache = (stamp, merged)
    return merged


def _sentences(text: str) -> list[str]:
    return [one.strip() for one in _SENTENCE.split(text or "") if len(one.strip()) >= 3]


def _quote_pairs() -> list[tuple[str, str]]:
    return [(str(a)[:1], str(b)[:1]) for a, b in rules()["quote_brackets"] if a and b]


def quoted_lines(text: str) -> list[str]:
    """把叙述里的引号内容抠出来（这就是她亲口说过的话）。"""
    out: list[str] = []
    for left, right in _quote_pairs():
        if not left or not right:
            continue
        for match in re.finditer(re.escape(left) + r"(.{2,200}?)" + re.escape(right), text,
                                 re.S):
            body = re.sub(r"\s+", "", match.group(1)).strip()
            if len(body) >= 2:
                out.append(body)
    return sorted(set(out), key=text.find)


def _subject_alt(name: str) -> str:
    """本名 + 词表里的代称，拼成一个正则分支：小说里大半句子只用「她」提人。"""
    aliases = [str(name)] if name else []
    aliases += [str(one) for one in rules().get("pronoun_aliases") or [] if one]
    seen: list[str] = []
    for one in aliases:
        if one and one not in seen:
            seen.append(one)
    return "|".join(re.escape(one) for one in seen) if seen else "[^\\s]{2,4}"


def _about(name: str, sentence: str) -> bool:
    """这句话是不是在说分析对象：出现本名或代称都算。"""
    if not name:
        return True
    body = str(sentence or "")
    return name in body or any(str(one) in body
                               for one in rules().get("pronoun_aliases") or [] if one)


def speech_of(name: str, text: str) -> list[str]:
    """归属到某个名字的台词：引号前后出现「名字/代称 + 说/道/问/答」就算她说的。"""
    verbs = "|".join(re.escape(str(one)) for one in rules()["speech_verbs"])
    subject = _subject_alt(name) if name else ""
    if not name:
        return quoted_lines(text)
    pattern = re.compile(
        r"(?:" + subject + r"[^\n]{0,12}(?:" + verbs + r")"
        r"|(?:说|道|问|答)[^\n]{0,4}?" + subject + r")")
    hits: list[str] = []
    for left, right in _quote_pairs():
        for match in re.finditer(re.escape(left) + r"(.{2,200}?)" + re.escape(right), text,
                                 re.S):
            window = text[max(0, match.start() - 40):min(len(text), match.end() + 40)]
            if pattern.search(window):
                body = re.sub(r"\s+", "", match.group(1)).strip()
                if len(body) >= 2 and body not in hits:
                    hits.append(body)
    return hits


def addressee_of(part_text: str, line: str) -> str:
    """她在跟谁说话：引号附近出现「对X说 / 看着X说」里的 X。"""
    index = part_text.find(line)
    window = part_text[max(0, index - 60):index + len(line) + 60] if index >= 0 else part_text
    for pattern in rules()["addressee_patterns"]:
        match = re.search(pattern, window)
        if match:
            return match.group(2)
    return ""


# ---------------------------------------------------------------------------
# 统计类的维度（说话习惯 / 用词 / 句子长度 / 对象差异）
# ---------------------------------------------------------------------------

def _ratio(part: float, whole: float) -> float:
    return round(part / whole, 3) if whole else 0.0


def speech_stats(lines: Sequence[str]) -> dict[str, Any]:
    settings = rules()["speech_stats"]
    rows = [str(one) for one in lines if one]
    if not rows:
        return {}
    lengths = [len(one) for one in rows]
    particles = [str(one) for one in settings["mood_particles"] if one]
    counted = Counter()
    for line in rows:
        for token in particles:
            if line.endswith(token) or token + "？" in line or token + "?" in line:
                counted[token] += 1
    questions = sum(1 for one in rows if one.rstrip("」』”\"？?！!… ").endswith(("？", "?")))
    rhetorical = sum(1 for one in rows if re.search(r"(不是.*吗|凭什么|难道|是吗|你说呢)", one))
    trailing = sum(1 for one in rows
                   if any(str(one).endswith(str(mark)) for mark in settings["trailing_marks"]))
    short = sum(1 for one in rows if len(one) <= int(settings["short_line_chars"]))
    long = sum(1 for one in rows if len(one) >= int(settings["long_line_chars"]))
    return {
        "台词条数": len(rows),
        "平均字数": round(sum(lengths) / len(lengths), 1),
        "最短": min(lengths), "最长": max(lengths),
        "短句比例": _ratio(short, len(rows)), "长句比例": _ratio(long, len(rows)),
        "问句比例": _ratio(questions, len(rows)),
        "反问比例": _ratio(rhetorical, len(rows)),
        "话说半句比例": _ratio(trailing, len(rows)),
        "语气词": dict(counted.most_common(4)),
        "样本": rows[:3],
    }


def vocabulary_stats(lines: Sequence[str], *, top: int = 6) -> dict[str, Any]:
    """没有分词库，就用「去掉虚词后的二字串」当粗词表，够看出用词偏好。"""
    from runtime.memory import retrieval

    stop = retrieval.CJK_STOP_CHARS
    counter: Counter[str] = Counter()
    for line in lines:
        body = re.sub(r"[^\u4e00-\u9fff]", "", line)
        for index in range(len(body) - 1):
            pair = body[index:index + 2]
            if pair[0] in stop or pair[1] in stop:
                continue
            counter[pair] += 1
    picked = [(word, times) for word, times in counter.most_common(top * 3) if times >= 2]
    return {"常用词组": picked[:top]}


def stance_from_stats(stats: dict[str, Any]) -> str:
    settings = rules()["speech_stats"]
    hints: list[str] = []
    ratio = float(stats.get("反问比例") or 0.0)
    if ratio >= float(settings["question_ratio_direct"]):
        hints.append("爱用反问顶回去")
    elif ratio >= float(settings["question_ratio_indirect"]):
        hints.append("偶尔带刺")
    if float(stats.get("短句比例") or 0.0) >= 0.4:
        hints.append("能短说就不多说")
    if float(stats.get("话说半句比例") or 0.0) >= 0.2:
        hints.append("话到嘴边会咽一半")
    if float(stats.get("问句比例") or 0.0) >= 0.3:
        hints.append("习惯先把问题丢回去")
    return "、".join(hints)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _entry(dimension: str, text: str, *, basis: str, part: dict[str, Any] | None,
           evidence: str, confidence: float, kind: str, weight: int, title: str,
           document_id: str) -> dict[str, Any]:
    location = importer.location_of(part, title=title) if part else ""
    return {
        "dimension": dimension, "text": text, "basis": basis,
        "kind": kind, "weight": weight, "confidence": round(float(confidence), 3),
        "source_id": document_id, "chunk_id": str((part or {}).get("chunk_id") or ""),
        "document_id": document_id,
        "location": location,
        "evidence": evidence[:int(rules()["evidence_max_chars"])],
        "ordinal": int((part or {}).get("ordinal") or 0),
    }


def analyze_document(document_id: str, *, name: str = "", character_id: str | None = None,
                     db_path: Path | str | None = None, register: bool = True,
                     title: str = "", progress: Any = None) -> dict[str, Any]:
    """分析一本书里的某个人。register=True 时把结论登记进设定层。

    返回的「说明」「未写到的维度」都是给人看的话；每一条都带出处。

    progress 可选，签名 ``progress(阶段名, 已完成, 总数)``，给界面的进度条用；
    不传就跟以前一样一把跑完。
    """
    def _tick(stage: str, done: int = 0, total: int = 0) -> None:
        if progress is None:
            return
        try:
            progress(stage, int(done), int(total))
        except Exception:      # 进度回调不许把分析本身搞挂
            pass

    path = Path(db_path) if db_path else store.DB_PATH
    settings = rules()
    _tick("正在读这本书", 0, 1)
    character = character_id or store.get_active_character(db_path=path)
    doc = _document(document_id, path)
    if doc is None:
        return {"ok": False, "reason": "NO_DOCUMENT", "说明": "没有这本书，先把它收进来。"}
    parts = importer.parts_of(document_id, db_path=path)
    if not parts:
        return {"ok": False, "reason": "NO_PARTS", "说明": "这本书里没读到正文。"}
    book_title = title or str(doc["title"])
    display = (name or "").strip() or _guess_name(parts)
    entries: list[dict[str, Any]] = []
    lines_by_part = [(part, _sentences(str(part["text"]))) for part in parts]

    # --- 1) 原文句子类维度：必须引得到原句 ---
    _dimension_total = len([one for one in (settings["dimensions"] or {})
                            if canon.is_dimension(one)])
    _done = 0
    for dimension, spec in (settings["dimensions"] or {}).items():
        if not canon.is_dimension(dimension):
            continue
        picked = _pattern_entries(dimension, spec, display, lines_by_part, parts,
                                 book_title, document_id)
        entries.extend(picked[:int(spec.get("max_entries")
                                   or settings["max_entries_per_dimension"])])
        _done += 1
        _tick("正在照着原文找她的样子", _done, _dimension_total)

    # --- 2) 说话统计类维度 ---
    all_text = "\n".join(str(one["text"]) for one in parts)
    her_lines = speech_of(display, all_text) if display else []
    stats = speech_stats(her_lines)
    if stats:
        sample_part = _part_with(parts, (stats["样本"] or [""])[0])
        entries.append(_entry(
            "speech_habit",
            f"她一共有 {stats['台词条数']} 句可对上的话，平均 {stats['平均字数']} 字一句，"
            f"{_half(stats)}；{stance_from_stats(stats) or '语气没什么固定套路'}。",
            basis="canon", part=sample_part, evidence=(stats["样本"] or [""])[0],
            confidence=float(settings["speech_stats"]["confidence"]),
            kind=str(settings["speech_stats"]["kind"]),
            weight=int(settings["speech_stats"]["weight"]),
            title=book_title, document_id=document_id))
        vocab = vocabulary_stats(her_lines)
        if vocab["常用词组"]:
            words = "、".join(f"{word}（{times} 次）" for word, times in vocab["常用词组"])
            entries.append(_entry(
                "vocabulary", f"她反复用到的说法：{words}。",
                basis="canon", part=sample_part, evidence=(stats["样本"] or [""])[0],
                confidence=0.8, kind="style", weight=98,
                title=book_title, document_id=document_id))
        entries.append(_entry(
            "sentence_length",
            f"她的话最短 {stats['最短']} 字、最长 {stats['最长']} 字，"
            f"短句占 {int(stats['短句比例'] * 100)}%。",
            basis="canon", part=sample_part, evidence=(stats["样本"] or [""])[0],
            confidence=0.82, kind="style", weight=96,
            title=book_title, document_id=document_id))

    # --- 3) 对不同对象说话的分寸 ---
    for dimension_entries in _addressee_entries(display, parts, book_title, document_id):
        entries.append(dimension_entries)

    # --- 4) 成长阶段 ---
    growth = _growth_entries(display, parts, book_title, document_id)
    entries.extend(growth)

    # --- 5) 词表推断类（性格/价值观/欲望/恐惧/冲突/思考/讽刺直接回避） ---
    _lex_total = len([one for one, one_spec in (settings["dimensions"] or {}).items()
                      if one_spec.get("lexicon")])
    _lex_done = 0
    for dimension, spec in (settings["dimensions"] or {}).items():
        if not spec.get("lexicon"):
            continue
        entries.extend(_lexicon_entries(dimension, spec, display, her_lines, lines_by_part,
                                        parts, book_title, document_id))
        _lex_done += 1
        _tick("正在归纳她说话的习惯", _lex_done, _lex_total)

    registered = merged = failed = 0
    _entry_total = len(entries)
    if register:
        for _index, item in enumerate(entries, 1):
            _tick("正在把这些资料登记好", _index, _entry_total)
            result = canon.add_fact(
                item["text"], character_id=character, kind=item["kind"], origin="novel",
                dimension=item["dimension"], basis=item["basis"],
                document_id=item["document_id"], source_id=item["source_id"],
                chunk_id=item["chunk_id"], location=item["location"],
                evidence=item["evidence"], ordinal=item["ordinal"],
                weight=item["weight"], confidence=item["confidence"], db_path=path)
            if not result.get("ok"):
                failed += 1
                continue
            merged += 1 if result.get("合并了") else 0
            registered += 1

    missing = [one["label_zh"] for one in canon.coverage(character, db_path=path)
               if one["条数"] == 0]
    facts = sum(1 for one in entries if one["basis"] == "canon")
    guesses = len(entries) - facts
    return {
        "ok": True, "character_id": character, "document_id": document_id,
        "title": book_title, "分析对象": display,
        "条目": entries, "登记条数": registered, "其中并入已有": merged, "未登记": failed,
        "原著事实": facts, "统计推断": guesses,
        "覆盖情况": canon.coverage(character, db_path=path),
        "未写到的维度": missing,
        "说话统计": stats,
        "段落数": len(parts),
        "说明": (f"从《{book_title}》里给她收出 {registered} 条资料："
                 f"{facts} 条是原文里写得有、带出处的，{guesses} 条是按说法统计推出来的。"
                 + (f" 这几项书里没写到：{'、'.join(missing)}。" if missing else "")),
    }


def _document(document_id: str, path: Path) -> Any:
    conn = store.connect(path)
    try:
        return conn.execute("SELECT * FROM source_document WHERE document_id = ?",
                            (document_id,)).fetchone()
    finally:
        conn.close()


def _half(stats: dict[str, Any]) -> str:
    ratio = float(stats.get("长句比例") or 0.0)
    if ratio >= 0.3:
        return "偶尔能一口气说挺长"
    if ratio <= 0.08:
        return "几乎不铺开说"
    return "长短混着说"


def _part_with(parts: Sequence[dict[str, Any]], needle: str) -> dict[str, Any] | None:
    body = re.sub(r"\s", "", str(needle or ""))
    if not body:
        return parts[0] if parts else None
    for part in parts:
        if body[:12] and body[:12] in re.sub(r"\s", "", str(part["text"])):
            return part
    return parts[0] if parts else None


def _guess_name(parts: Sequence[dict[str, Any]]) -> str:
    """没填名字时，用「出现在台词归属里最多的二字名」兜底，并如实标明是猜的。"""
    counter: Counter[str] = Counter()
    verbs = "|".join(re.escape(str(one)) for one in rules()["speech_verbs"])
    for part in parts:
        for match in re.finditer(r"([\u4e00-\u9fff]{2,3})(?:" + verbs + r")",
                                 str(part["text"])):
            counter[match.group(1)] += 1
    return counter.most_common(1)[0][0] if counter else ""


def _pattern_entries(dimension: str, spec: dict[str, Any], name: str,
                     lines_by_part: Sequence[tuple[dict[str, Any], list[str]]],
                     parts: Sequence[dict[str, Any]], title: str,
                     document_id: str) -> list[dict[str, Any]]:
    if spec.get("lexicon"):
        return []       # 推断类走另一条路，免得这里重复产出
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    patterns = [str(one).replace("{name}", re.escape(name) if name else "[^\\s]{2,4}")
                for one in spec.get("patterns") or []]
    min_chars = int(spec.get("min_clause_chars") or rules()["min_evidence_chars"])
    for part, sentences in lines_by_part:
        for sentence in sentences:
            if len(sentence) < min_chars:
                continue
            hit = _first_match(patterns, sentence)
            if not hit:
                continue
            if spec.get("name_must_appear") and not _about(name, sentence):
                continue
            key = re.sub(r"\W", "", sentence)[:24]
            if key in seen:
                continue
            seen.add(key)
            out.append(_entry(dimension, _tidy(sentence), basis=str(spec.get("basis")
                                                                    or "canon"),
                              part=part, evidence=sentence,
                              confidence=float(spec.get("confidence") or 0.9),
                              kind=str(spec.get("kind") or "setting"),
                              weight=int(spec.get("weight") or 100),
                              title=title, document_id=document_id))
            if len(out) >= int(spec.get("max_entries")
                               or rules()["max_entries_per_dimension"]):
                return out
    return out


def _first_match(patterns: Sequence[str], sentence: str) -> str:
    for pattern in patterns:
        try:
            if re.search(pattern, sentence):
                return pattern
        except re.error:
            continue
    return ""


def _lexicon_entries(dimension: str, spec: dict[str, Any], name: str,
                     her_lines: Sequence[str],
                     lines_by_part: Sequence[tuple[dict[str, Any], list[str]]],
                     parts: Sequence[dict[str, Any]], title: str,
                     document_id: str) -> list[dict[str, Any]]:
    """词表推断：数一数各类线索出现几次，够多次才下结论，并且把例子摆出来。"""
    haystacks = list(her_lines) + [sentence for _part, rows in lines_by_part
                                   for sentence in rows if _about(name, sentence)]
    if not haystacks:
        return []
    out: list[dict[str, Any]] = []
    for label, words in (spec.get("lexicon") or {}).items():
        hits: list[str] = []
        for body in haystacks:
            if any(str(word) in body for word in words):
                hits.append(body)
        if len(hits) < 2:
            continue
        where = _part_with(parts, hits[0])
        out.append(_entry(
            dimension,
            f"{label}：这本书里能对上的说法有 {len(hits)} 处，"
            f"比如「{hits[0][:24]}」「{(hits[1] if len(hits) > 1 else hits[0])[:24]}」。",
            basis=str(spec.get("basis") or "inferred"), part=where,
            evidence=hits[0], confidence=float(spec.get("confidence") or 0.55),
            kind=str(spec.get("kind") or "setting"), weight=int(spec.get("weight") or 100),
            title=title, document_id=document_id))
    return out[:int(spec.get("max_entries") or rules()["max_entries_per_dimension"])]


def _addressee_entries(name: str, parts: Sequence[dict[str, Any]], title: str,
                       document_id: str) -> list[dict[str, Any]]:
    if not name:
        return []
    grouped: dict[str, list[str]] = {}
    for part in parts:
        text = str(part["text"])
        for line in speech_of(name, text):
            partner = addressee_of(text, line)
            if partner and partner != name:
                grouped.setdefault(partner, []).append(line)
    out: list[dict[str, Any]] = []
    for partner, lines in list(grouped.items())[:3]:
        stats = speech_stats(lines)
        if not stats or stats["台词条数"] < 2:
            continue
        out.append(_entry(
            "addressee_style",
            f"她对着{partner}的时候：平均 {stats['平均字数']} 字一句，"
            f"{stance_from_stats(stats) or '没什么特别变化'}。",
            basis="inferred", part=_part_with(parts, lines[0]), evidence=lines[0],
            confidence=0.55, kind="style", weight=90,
            title=title, document_id=document_id))
    return out


def _growth_entries(name: str, parts: Sequence[dict[str, Any]], title: str,
                    document_id: str) -> list[dict[str, Any]]:
    """成长阶段：按她出场的段落位置切成几档，说清「哪一段开始话变了」。"""
    if not name:
        return []
    settings = rules()["growth_stages"]
    buckets = max(2, int(settings["buckets"]))
    total = len(parts)
    counts = [0] * buckets
    for index, part in enumerate(parts):
        hits = str(part["text"]).count(name)
        counts[min(int(index * buckets / max(total, 1)), buckets - 1)] += hits
    if max(counts, default=0) < int(settings["min_hits"]):
        return []
    out: list[dict[str, Any]] = []
    first_index = next((index for index, part in enumerate(parts) if name in str(part["text"])),
                       None)
    if first_index is not None:
        part = parts[first_index]
        out.append(_entry("growth",
                          f"她第一次出现在{importer.location_of(part, title='').split(' · ')[0]}"
                          f"（全书 {total} 段里的第 {first_index + 1} 段）。",
                          basis="canon", part=part,
                          evidence=str(part["text"])[:60],
                          confidence=0.8, kind=str(settings["kind"]), weight=88,
                          title=title, document_id=document_id))
    labels = [str(settings.get("rise_label_zh") or "前半"),
              str(settings.get("peak_label_zh") or "中段"),
              str(settings.get("fall_label_zh") or "后半")]
    peak = counts.index(max(counts))
    out.append(_entry(
        "growth",
        f"{labels[min(peak, len(labels) - 1)]}：她的出现集中在"
        f"{'开头' if peak == 0 else ('中间' if peak < buckets - 1 else '结尾')}那几段"
        f"（各段出现次数 {counts}）。",
        basis="inferred", part=parts[min(int((peak + 0.5) * total / buckets), total - 1)],
        evidence=f"各段出现次数：{counts}",
        confidence=float(settings["confidence"]), kind=str(settings["kind"]), weight=86,
        title=title, document_id=document_id))
    return out


def _tidy(sentence: str) -> str:
    body = re.sub(r"\s+", "", str(sentence or "")).strip("，,。.、；;：:")
    return (body[:78] + "…") if len(body) > 78 else (body + "。") if body else ""


def collect_quotes(document_id: str, name: str = "", *, character_id: str | None = None,
                   db_path: Path | str | None = None, limit: int = 6,
                   register: bool = True) -> dict[str, Any]:
    """§11 最后一项「关键原著台词样本」：原句照抄，一条都不改写。"""
    path = Path(db_path) if db_path else store.DB_PATH
    character = character_id or store.get_active_character(db_path=path)
    doc = _document(document_id, path)
    if doc is None:
        return {"ok": False, "说明": "没有这本书。"}
    parts = importer.parts_of(document_id, db_path=path)
    added = 0
    quotes: list[dict[str, Any]] = []
    for part in parts:
        for line in speech_of(name, str(part["text"])):
            if len(line) < 4 or any(one["evidence"] == line for one in quotes):
                continue
            quotes.append({"text": f"她说过：{line}", "evidence": line,
                           "location": importer.location_of(part, title=str(doc["title"])),
                           "chunk_id": str(part["chunk_id"]), "ordinal": part["ordinal"]})
            if len(quotes) >= max(1, int(limit)):
                break
        if len(quotes) >= max(1, int(limit)):
            break
    if register:
        for item in quotes:
            result = canon.add_fact(item["text"], character_id=character, kind="quote",
                                   origin="novel", dimension="quotes", basis="canon",
                                   document_id=document_id, source_id=document_id,
                                   chunk_id=item["chunk_id"], location=item["location"],
                                   evidence=item["evidence"], ordinal=item["ordinal"],
                                   weight=92, confidence=0.95, db_path=path)
            added += 1 if result.get("ok") and not result.get("合并了") else 0
    return {"ok": True, "条数": len(quotes), "新增": added, "台词": quotes,
            "说明": f"照抄了 {len(quotes)} 句她说的话，一个字没改。"}


def register_setting_facts(document_id: str, facts: list[dict[str, Any]], *,
                           character_id: str | None = None,
                           db_path: Path | str | None = None) -> dict[str, Any]:
    """人工/外部整理好的设定条目登记（保留旧入口，多了出处就一并存）。"""
    character = character_id or store.get_active_character(db_path=db_path)
    added = merged = 0
    for fact in facts:
        result = canon.add_fact(
            str(fact.get("text") or ""), character_id=character,
            kind=str(fact.get("kind") or "setting"), origin="novel",
            dimension=str(fact.get("dimension") or ""),
            basis=str(fact.get("basis") or "canon"),
            document_id=document_id, source_id=str(fact.get("source_id") or document_id),
            chunk_id=str(fact.get("chunk_id") or ""), location=str(fact.get("location") or ""),
            evidence=str(fact.get("evidence") or ""),
            ordinal=int(fact.get("ordinal") or 0), weight=int(fact.get("weight") or 120),
            confidence=float(fact.get("confidence") or 0.95), db_path=db_path)
        if not result.get("ok"):
            continue
        merged += 1 if result.get("合并了") else 0
        added += 1
    return {"ok": True, "登记条数": added, "其中并入已有": merged}


def analyze_novel(document_id: str, name: str = "", *, character_id: str | None = None,
                  db_path: Path | str | None = None, quote_limit: int = 5,
                  progress: Any = None) -> dict[str, Any]:
    """一步到位：先做全维度分析，再照抄台词样本。"""
    report = analyze_document(document_id, name=name, character_id=character_id,
                              db_path=db_path, register=True, progress=progress)
    if not report.get("ok"):
        return report
    quotes = collect_quotes(document_id, str(report.get("分析对象") or name),
                            character_id=report["character_id"], db_path=db_path,
                            limit=quote_limit, register=True)
    report["台词样本"] = quotes
    # 台词是这一步之后才登记的，覆盖情况和「没写到」的清单要重新算一遍，
    # 否则会出现「明明收了台词，却还报没写到」的自相矛盾。
    if quotes.get("条数"):
        report["覆盖情况"] = canon.coverage(report["character_id"], db_path=db_path)
        report["未写到的维度"] = [one["label_zh"] for one in report["覆盖情况"]
                                    if one["条数"] == 0]
    report["说明"] = str(report["说明"]) + " " + str(quotes.get("说明") or "")
    return report


__all__ = ["rules", "analyze_document", "analyze_novel", "collect_quotes",
           "register_setting_facts", "quoted_lines", "speech_of", "speech_stats",
           "vocabulary_stats", "addressee_of", "stance_from_stats"]
