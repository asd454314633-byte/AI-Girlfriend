# -*- coding: utf-8 -*-
"""两份规则数据 + 一份「给人看的话不许出现技术名词」的口径。

  * ``style_rules.json``     —— §19 人设自然度：校验人物台词的规则、系统规则那一块的文案；
  * ``context_policy.json``  —— §18 上下文装配：每块能占预算的多少、超了先丢谁。

为什么单独一个模块：这两份东西被 store / canon / context_builder / 界面接口共用。
放在任何一边都会逼出第二次复制，复制一次就已经违反 §0 的「同一件事只有一处」。

本模块不认识任何模型或语音提供方，也不联网。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

HERE = Path(__file__).resolve().parent
STYLE_PATH = HERE / "style_rules.json"
POLICY_PATH = HERE / "context_policy.json"

# ---------------------------------------------------------------------------
# 界面/文案里禁止出现的内部名词（任务 §10 最后那一段）
# ---------------------------------------------------------------------------

BANNED_UI_WORDS: tuple[str, ...] = (
    "JSON", "json", "RAG", "chunk", "Chunk", "CHUNK", "embedding", "Embedding",
    "BM25", "bm25", "数据库", "prompt", "Prompt", "PROMPT", "token", "Token", "TOKEN",
    "sqlite", "SQL", "索引", "向量", "语料", "分词",
)

_BANNED_RE = re.compile("|".join(re.escape(word) for word in BANNED_UI_WORDS))


def find_banned(*texts: Any) -> list[str]:
    """把给人看的若干段话扫一遍，返回露出的内部词（空表 = 干净）。"""
    hits: list[str] = []
    for text in texts:
        body = str(text or "")
        for word in _BANNED_RE.findall(body):
            if word not in hits:
                hits.append(word)
    return hits


def is_plain(*texts: Any) -> bool:
    return not find_banned(*texts)


# ---------------------------------------------------------------------------
# 规则装载（mtime 变了就重读，改规则不用重启服务）
# ---------------------------------------------------------------------------

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

_STYLE_FALLBACK: dict[str, Any] = {
    "persona_validation": {
        "banned_self_statements": [],
        "empty_persona": "人物台词还是空的，先写一段她怎么说话。",
        "template_repetition": {"min_times": 3, "max_line_chars": 24,
                                 "why": "「{line}」重复了 {times} 次。"},
        "preaching": {"pattern": r"(禁止|不要|不得|不可以|应该|必须)[^。\n]{6,}",
                       "max_count": 6, "why": "说教太多。"},
        "recap_demand": {"pattern": r"(每(一)?(轮|次)都?(要|需).{0,8}(总结|复述|概括))",
                         "why": "要求每轮复述。"},
    },
    "policy_sections": [],
    "immutable_defaults": [],
}

_POLICY_FALLBACK: dict[str, Any] = {
    "token_estimate": {"cjk_per_token": 1.0, "ascii_per_token": 0.28},
    "section_shares": {},
    "drop_order": ["history", "tool_note", "memory_lines", "canon_passages"],
    "history_kept_on_trim": 2,
    "passage_limit": 3,
    "memory_limit": 5,
    "tool_note_cap": 6,
    "min_chars_per_section": 40,
    "greeting_note_zh": "她在等你回话，一句就够。",
    # 每一块内部那些零碎的长度/条数口子，以前散在 Python 里写死，现在都进这份文件。
    "block_caps": {
        "quote_max_chars": 190,
        "quote_cut_min_ratio": 0.55,
        "fit_cut_min_ratio": 0.5,
        "relationship_items": 3,
        "profile_list_items": 4,
        "retrieval_overfetch": 2,
        "trim_guard_steps": 64,
        "history_row_overhead": 2,
    },
}


def _load(path: Path, fallback: dict[str, Any], key: str) -> dict[str, Any]:
    stamp = path.stat().st_mtime_ns if path.exists() else 0.0
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    payload: dict[str, Any] = dict(fallback)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = _merge(fallback, loaded)
        except (OSError, ValueError):
            payload = dict(fallback)
    _CACHE[key] = (stamp, payload)
    return payload


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """逐字段合并：文件里少写一个键也不会让另一半规则失效。"""
    out = dict(base)
    for name, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(name), dict):
            out[name] = _merge(out[name], value)
        else:
            out[name] = value
    return out


def style(*, refresh: bool = False) -> dict[str, Any]:
    if refresh:
        _CACHE.pop("style", None)
    return _load(STYLE_PATH, _STYLE_FALLBACK, "style")


def policy(*, refresh: bool = False) -> dict[str, Any]:
    if refresh:
        _CACHE.pop("policy", None)
    return _load(POLICY_PATH, _POLICY_FALLBACK, "policy")


def rule_sources() -> dict[str, Any]:
    """规则到底从哪两份文件读的（自测和界面自检都要能看见这个）。"""
    return {"style_rules": str(STYLE_PATH), "context_policy": str(POLICY_PATH),
            "自然度条目": len(style()["persona_validation"].get("banned_self_statements") or []),
            "系统规则条目": len(style().get("policy_sections") or []),
            "预算分格数": len(policy().get("section_shares") or {})}


# ---------------------------------------------------------------------------
# 装配用的小工具
# ---------------------------------------------------------------------------

def cap(name: str, default: float = 1.0) -> float:
    """块内部那些零碎口子（一句原文最长多少字、关系取几条…）的唯一读法。

    以前这些数字散在 Python 里写死，改一处要翻三个文件；现在一律从
    context_policy.json 的 block_caps 读，读不到才用这行兜底值。
    """
    table = policy().get("block_caps") or {}
    raw = table.get(name)
    if raw is None:
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def cap_int(name: str, default: int) -> int:
    return int(round(cap(name, default)))


def chars_for(budget_total: int, section: str, *, per_token: float = 1.0) -> int:
    """这一块能占多少字 = 总预算 × 声明的比例（§18：不写死数字）。

    中文一个字约等于一个词元，所以默认 per_token=1.0；要按英文多的内容算，
    调用方把 estimate 的反数传进来。
    """
    shares = policy()["section_shares"]
    share = float(shares.get(section) or 0.0)
    floor = int(policy()["min_chars_per_section"])
    value = int(round(max(0, int(budget_total)) * share / max(per_token, 0.1)))
    return value if value >= floor or share == 0.0 else floor


def render_policy(character_name: str = "", *, extra_lines: Sequence[str] = ()) -> str:
    """把 style_rules.json 的 policy_sections 渲染成上下文里的『系统规则』一块。"""
    sections = style().get("policy_sections") or []
    immutable = "、".join(str(one) for one in style().get("immutable_defaults") or [])
    lines: list[str] = []
    for section in sections:
        title = str(section.get("title_zh") or "").strip()
        if title:
            lines.append(f"【{title}】")
        for raw in section.get("lines") or []:
            text = str(raw)
            if "{immutable}" in text:
                text = text.replace("{immutable}", immutable or "不装成另一个人")
            lines.append(text)
    lines.extend(str(one) for one in (extra_lines or ()) if str(one).strip())
    return "\n".join(lines)
