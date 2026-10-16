# -*- coding: utf-8 -*-
"""从一句日常对话里挑出「值得长期记下来的话」。

只有六类内容可以变成长期记忆：稳定的偏好、长期事实、重要关系、承诺、长期目标、
共同经历过的大事。其余一律不写。

三条闸门，顺序固定：
  1. 规则闸门 —— 问句、临时角色扮演、命令、寒暄、太短的话，直接不收；
  2. 设定闸门 —— 和设定层（CANON）说的是同一件事，绝不写入，更不覆盖；
  3. 查重闸门 —— 和已有记忆是同一件事，只抬把握度，不再新增一条。

用户明说「记住…」「以后…」「从现在开始…」的那句，优先级和把握度都要高一档。

纯规则实现：不叫模型，不联网。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import retrieval, service
from runtime.character import canon

HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "extractor_rules.json"

# 只有这几类可以变成长期记忆
ALLOWED_LONG_TERM_KINDS = ("preference", "fact", "relationship", "commitment",
                           "goal", "shared_event")

MIN_TEXT_CHARS = 4          # 归一化之后短于这个长度，说明没说事
DEDUPE_THRESHOLD = 0.82     # 和已有记忆像到这个程度就合并
CANON_THRESHOLD = 0.62      # 和设定像到这个程度就不许写入
EXPLICIT_BOOST = 0.15
EXPLICIT_PRIORITY = 1

# 用户自己点名要记的说法
EXPLICIT_PATTERNS = (
    r"记住", r"记下来", r"别忘了", r"以后(都|就|别|不要|记得)?", r"从现在开始", r"从今天起",
    r"从今以后", r"一直(记得|记住)", r"你一定要", r"我跟你说过", r"帮我记",
)
_EXPLICIT_RE = re.compile("|".join(EXPLICIT_PATTERNS))

# 规则闸门：这些句子不是「关于用户的长期事实」。
# 顺序有意义：先判「这话是不是在跟系统提要求」（临时角色、收回、当下指令、寒暄），
# 再判句式是不是问句。「恢复正常吧」尾字是「吧」，先判问句就会把它错标成问句，
# 后面按标签做统计和处理就全错了。
REJECT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(扮演|你现在是|换成.{0,8}(角色|身份|模式)|进入.{0,8}模式|恢复正常|退出角色)",
     "临时角色要求"),
    (r"(^别.{0,4}(记|说|提)|忘掉|忘记|把.{0,6}删了|不要保存|别录)", "收回要求"),
    (r"^(你好|在吗|在干嘛|干嘛呢|吃了没|晚安|早安|哈哈+|嗯+|哦+|谢谢|再见|早上好|晚上好)",
     "寒暄"),
    (r"(闭嘴|安静|停下|继续说|再说一遍|大声点|慢点说)", "当下指令"),
    (r"[Pp][Rr][Oo][Mm][Pp][Tt]|系统提示|你是谁开发的|模型叫什么", "关于系统本身"),
    (r"[?？]\s*$|^[?？]|\s[?？]", "问句"),
    (r"^(什么|为什么|为啥|怎么|怎样|哪|谁|是否|能不能|可不可以)", "问句"),
    (r"(吗|呢)[?？]?\s*$", "问句"),
)

# 六类长期内容的判定规则，顺序即优先级：先具体后宽泛
KIND_RULES: tuple[tuple[str, str, float], ...] = (
    # kind, 正则, 基础把握度
    ("commitment", r"我(会|一定|保证|承诺|答应|说到做到)|以后我再也不|下次我", 0.80),
    ("goal", r"我想(成为|当|考|学|做)|我的目标是|打算|迟早要|以后要(做|当|考)", 0.72),
    ("relationship", r"我是你的|你是我|我们(是|已经认识|在一起)|以后叫我|叫我|我称呼你", 0.78),
    ("preference", r"最(喜欢|讨厌|不爱)|不喜欢|不爱|受不了|习惯|偏好|更喜欢|不喜欢被", 0.75),
    ("preference", r"喜欢|爱吃|爱喝|爱看|爱听|讨厌|烦", 0.68),
    # 「不吃香菜」这类忌口是最典型的稳定偏好，不带「喜欢/不喜欢」这几个字，
    # 光靠上面两条抓不到，所以单列一条（放在宽泛那条之后，避免抢走「不喜欢吃」）。
    ("preference", r"不吃|不喝|不碰|忌口|过敏", 0.72),
    # §15 点名「以后……」「从现在开始……」要**优先写入**，可这类句子常常不带
    # 上面任何字样：「我以后只喝淡茶」「从现在开始叫我小张」。
    # 前面几条一条都不命中，于是这句当场蒸发 —— 记忆写进去是 0 条，
    # 用户那边表现为"说了没用"。所以给"往后一直这样"这个措辞本身一条规则；
    # 放在忌口条之后，不抢「以后我再也不」（那条例更具体，且排在最前）。
    ("preference", r"(以后|从现在开始|从现在起|往后|今后)[^。！？」』]{0,8}"
                   r"(只喝|只吃|只用|只要|只穿|喜欢|不爱|不喝|不吃|别再|不要|改成|换成)", 0.78),
    ("shared_event", r"(昨天|上周|上个月|去年|那天|前阵子|之前)(我们|我)", 0.68),
    ("shared_event", r"我们一起|我们那次|发生过", 0.66),
    ("fact", r"我叫|我的名字|我今年|我住在|我在.{0,10}(上班|读书|工作)|我的生日|我这边", 0.80),
    ("fact", r"我的(工作|职业|公司|学校|家|城市)是", 0.76),
)

# 句子切分：中文里能自然断开的地方
_CLAUSE_SPLIT = re.compile(r"[。！？!?；;\n]")
_SOFT_SPLIT = re.compile(r"[，,]")
_TRAILING = " 　。\t"


# ---------------------------------------------------------------------------
# 原著 vs 互动的分层闸门（任务 §20）
#
# 与设定撞车时不再一刀切拒收：如果这句话是**在改她的过去**（「你以前其实很喜欢甲」），
# 它就不该进设定层，但也确实发生过 —— 发生过的是「用户这么说」这件事。
# 于是记一条互动层的说法，把握度压低、打上标记，界面上看得见它和原著不是一回事。
# 词表在 extractor_rules.json，不写死在这里。
# ---------------------------------------------------------------------------

_CLAIM_FALLBACK: dict[str, Any] = {
    "claim_patterns": [],
    "claim_confidence": 0.55,
    "claim_tag": "canon_conflict",
    "claim_layer": "RELATIONSHIP",
    "claim_label_zh": "与原著资料不同，只当作互动里的说法",
    "render_user_claim_zh": "他跟你说过：{text}（原著资料里不是这样，别改口）",
    "render_plain_zh": "你们之间约定过：{text}。",
    "max_claims_per_turn": 2,
}
_claim_cache: dict[str, Any] | None = None


def claim_rules(*, refresh: bool = False) -> dict[str, Any]:
    """读分层规则；缺文件就用内置的一份，字段缺一个补一个。"""
    global _claim_cache
    if _claim_cache is not None and not refresh:
        return _claim_cache
    merged = dict(_CLAIM_FALLBACK)
    if RULES_PATH.exists():
        try:
            loaded = json.loads(RULES_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                merged.update(loaded)
        except (OSError, ValueError):
            pass
    _claim_cache = merged
    return merged


def _character_names(character_id: str | None, character_db_path: Any) -> list[str]:
    """这个人物在聊天里会被怎么称呼：显示名 + 小名，没有就只靠「你/她」。"""
    try:
        from runtime.character import store     # 延迟导入，避开包初始化顺序
        profile = store.get_profile(character_id, db_path=character_db_path)
    except Exception:
        return []
    if profile is None:
        return []
    return [one for one in ((profile.display_name or "").strip(),
                            (profile.nickname or "").strip()) if len(one) >= 2]


def is_character_claim(text: str, names: Sequence[str] = ()) -> bool:
    """这句话是不是在说**她**的过去/本性（而不是说话人自己的事）。"""
    body = (text or "").strip()
    if not body:
        return False
    for template in claim_rules()["claim_patterns"]:
        pattern = template.replace("{name}", "|".join(names) if names else "(?!x)x")
        try:
            if re.search(pattern, body):
                return True
        except re.error:
            continue
    return False


def record_as_interaction_claim(item: Candidate, character: str, *,
                                db_path: Any) -> Any:
    """把一句「用户对她的说法」落到互动层，绝不碰设定层。"""
    settings = claim_rules()
    return service.add(
        item.text, character, kind="fact",
        layer=str(settings["claim_layer"]),
        confidence=float(settings["claim_confidence"]),
        explicit=False, source_session=item.source_session, source_turn=item.source_turn,
        tags=[f"kind:fact", str(settings["claim_tag"])],
        source_kind="CHAT", db_path=db_path)


@dataclass(slots=True)
class Candidate:
    """一条被规则挑出来的候选记忆，带着它凭什么入选。"""

    text: str
    kind: str = "fact"
    confidence: float = 0.5
    explicit: bool = False
    priority: int = 0
    reason: str = ""
    source_session: str = ""
    source_turn: int = 0
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "kind": self.kind, "confidence": round(self.confidence, 3),
                "explicit": self.explicit, "priority": self.priority,
                "入选原因": self.reason}


def reject_reason(text: str) -> str:
    """这句话该不该直接不收；返回空串表示可以继续往下判。

    先过规则再看长度：「你好」只有两个字，先量长度的话会被标成「没说事」，
    而它真正该被记成「寒暄」——两类后面的处理方式不一样，标签不能给错。
    """
    body = (text or "").strip()
    if not body:
        return "空"
    for pattern, label in REJECT_PATTERNS:
        if re.search(pattern, body):
            return label
    if len(retrieval.normalize_text(body)) < MIN_TEXT_CHARS:
        return "没说事"
    return ""


def classify(text: str) -> tuple[str, float, str]:
    """判类型 + 基础把握度；判不出来就不是长期内容。"""
    body = (text or "").strip()
    for kind, pattern, base in KIND_RULES:
        hit = re.search(pattern, body)
        if hit:
            return kind, base, f"命中长期内容规则：{hit.group(0)}"
    return "", 0.0, ""


def split_sentences(text: str) -> list[str]:
    parts = [chunk.strip(_TRAILING) for chunk in _CLAUSE_SPLIT.split(text or "")]
    out = [part for part in parts if len(part) >= 3]
    if not out and (text or "").strip():
        out = [(text or "").strip()]
    return out


def extract_candidates(user_text: str, *, session_id: str = "", turn_id: int = 0,
                       allow_soft_split: bool = True) -> list[Candidate]:
    """第一步：从一句话里挑出候选，只判「算不算长期内容」，还不落盘。"""
    body = (user_text or "").strip()
    if not body:
        return []
    candidates: list[Candidate] = []
    seen: set[str] = set()
    sentences = split_sentences(body)
    pieces: list[str] = []
    for sentence in sentences:
        pieces.append(sentence)
        if allow_soft_split and len(sentence) > 24:
            # 「我喜欢吃辣，讨厌香菜」这种并列，拆开各记一条才有用
            for chunk in _SOFT_SPLIT.split(sentence):
                chunk = chunk.strip(_TRAILING)
                if len(chunk) >= 6 and chunk != sentence:
                    pieces.append(chunk)
    explicit_whole = bool(_EXPLICIT_RE.search(body))

    for piece in pieces:
        if reject_reason(piece):
            continue
        kind, base, why = classify(piece)
        if not kind:
            continue
        normalized = retrieval.normalize_text(piece)
        if normalized in seen:
            continue
        seen.add(normalized)
        explicit = explicit_whole or bool(_EXPLICIT_RE.search(piece))
        confidence = base
        if explicit:
            confidence = min(0.97, base + EXPLICIT_BOOST)
        candidates.append(Candidate(
            text=piece.strip(_TRAILING), kind=kind, confidence=confidence,
            explicit=explicit, priority=EXPLICIT_PRIORITY if explicit else 0,
            reason=why + ("；用户明说要记，把握度抬一档" if explicit else ""),
            source_session=session_id, source_turn=turn_id,
            tags=[f"kind:{kind}"] + (["explicit"] if explicit else []),
        ))
    return candidates


def _drop_inner_duplicates(candidates: Sequence[Candidate]) -> list[Candidate]:
    """同一句话里说了两遍同一件事：留把握度最高的那条。"""
    kept: list[Candidate] = []
    for item in candidates:
        merged = False
        for index, other in enumerate(kept):
            if retrieval.similarity(item.text, other.text) >= DEDUPE_THRESHOLD:
                if (item.priority, item.confidence) > (other.priority, other.confidence):
                    kept[index] = item
                merged = True
                break
        if not merged:
            kept.append(item)
    kept.sort(key=lambda one: (one.priority, one.confidence), reverse=True)
    return kept


def apply_candidates(candidates: Sequence[Candidate], character_id: str | None = None, *,
                     db_path: Any = None, canon_db_path: Any = None,
                     max_writes: int = 4) -> dict[str, list[dict[str, Any]]]:
    """第二、三道闸门 + 落盘。返回一份能直接给人看的处理结果。"""
    character = character_id or "default"
    settings = claim_rules()
    tag = str(settings["claim_tag"])
    names = _character_names(character, canon_db_path)
    claims_left = max(0, int(settings["max_claims_per_turn"] or 0))
    report: dict[str, list[dict[str, Any]]] = {"写入": [], "合并": [], "拒收（设定）": [],
                                               "跳过（重复）": [], "记为互动说法": []}
    written = 0
    for item in _drop_inner_duplicates(candidates):
        entry = {"text": item.text, "kind": item.kind,
                 "confidence": round(item.confidence, 3), "explicit": item.explicit}
        if item.kind not in ALLOWED_LONG_TERM_KINDS:
            entry["原因"] = "不在允许变成长期记忆的六类里"
            report["拒收（设定）"].append(entry)
            continue
        near_canon = canon.similar_to_canon(item.text, character_id=character,
                                           db_path=canon_db_path, threshold=CANON_THRESHOLD)
        if near_canon.get("出处") and near_canon["相似度"] >= CANON_THRESHOLD:
            entry["撞上的设定"] = near_canon["text"][:60]
            if is_character_claim(item.text, names) and claims_left > 0:
                # §20：不许改写原著，但「用户这么说」这件事本身要留得下来
                record = record_as_interaction_claim(item, character, db_path=db_path)
                entry["memory_id"] = record.memory_id
                entry["标签"] = tag
                entry["说明"] = str(settings["claim_label_zh"])
                report["记为互动说法"].append(entry)
                claims_left -= 1
                continue
            entry["原因"] = "设定层已经写了这件事，聊天里的话不许改动它"
            report["拒收（设定）"].append(entry)
            continue
        existing = service.list_memories(character, limit=400, db_path=db_path)
        duplicate = None
        for old in existing:
            if old.layer == "CANON":
                continue      # 双保险：任何情况下都不拿聊天去覆盖设定
            if old.layer == service.SESSION_LAYER:
                # 这一场的原话是先落库的（remember_turn=True），它和刚抽出来的候选
                # 说的是同一句话，相似度必然是 1。把它当"以前记过"看，
                # 每一句都会被自己这一场的回放挡掉，长期记忆就永远写不进去。
                continue
            if retrieval.similarity(item.text, old.text) >= DEDUPE_THRESHOLD:
                duplicate = old
                break
        if duplicate is not None:
            better = (item.priority, item.confidence) > (int(duplicate.explicit),
                                                         duplicate.confidence)
            if better:
                service.update(duplicate.memory_id,
                               confidence=min(0.99, max(duplicate.confidence, item.confidence)),
                               explicit=item.explicit or duplicate.explicit,
                               db_path=db_path)
                entry["并到"] = duplicate.memory_id
            else:
                entry["原因"] = "已经记过了，这句没有新增信息"
                entry["并到"] = duplicate.memory_id
                report["跳过（重复）"].append(entry)
                continue
            report["合并"].append(entry)
            continue
        if written >= max_writes:
            entry["原因"] = f"一轮最多写 {max_writes} 条，剩下的下一轮再说"
            report["跳过（重复）"].append(entry)
            continue
        record = service.add(item.text, character, kind=item.kind,
                             layer="LONG_TERM_MEMORY", confidence=item.confidence,
                             explicit=item.explicit, source_session=item.source_session,
                             source_turn=item.source_turn, tags=item.tags,
                             source_kind="CHAT", db_path=db_path)
        entry["memory_id"] = record.memory_id
        report["写入"].append(entry)
        written += 1
    return report


def learn_from_turn(user_text: str, character_id: str | None = None, *,
                    session_id: str = "", turn_id: int = 0,
                    db_path: Any = None, canon_db_path: Any = None,
                    remember_turn: bool = False) -> dict[str, Any]:
    """一整轮的入口：抽候选 -> 过闸门 -> 落盘，返回处理结果。

    remember_turn=True 时顺便把这一句原文放进 SESSION 层（这一场的原话，clear_session 清）。
    """
    character = character_id or "default"
    if remember_turn and (user_text or "").strip():
        service.remember_turn(user_text, character_id=character, session_id=session_id,
                              turn_id=turn_id, db_path=db_path)
    candidates = extract_candidates(user_text, session_id=session_id, turn_id=turn_id)
    report = apply_candidates(candidates, character, db_path=db_path,
                              canon_db_path=canon_db_path)
    return {"候选": [one.to_dict() for one in candidates], **report,
            "说明": f"这轮记了 {len(report['写入'])} 条，合并 {len(report['合并'])} 条。"}


def forget_request(text: str) -> str:
    """用户说「忘掉刚才那句」时，把要删的那句话交出来；不是这种要求就返回空串。"""
    body = (text or "").strip()
    match = re.search(r"(?:忘掉|忘记|别记|删了)\s*(?:吧|了)?(.{2,60})", body)
    if match:
        return match.group(1).strip(_TRAILING)
    if re.search(r"^(忘掉|忘记)(刚才|之前|那句)", body):
        return ""
    return ""
