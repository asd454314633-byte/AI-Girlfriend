# -*- coding: utf-8 -*-
"""检测层：从「她最近说过的话」里量出重复、复述、套话、长度单调。

设计纪律（对应任务 §8）：

  * 这里是 **检测**，不是禁词表。每个函数只做一件事——数出最近真的犯了几次，
    犯了才产出一句 **正向改写目标**（「这轮换掉这个说法」），没犯就不出声；
  * 纯函数：不调模型、不联网、不读写任何状态、不引入任何分词依赖
    （所以中文按 n-gram 量，按字面出现的连续片段算，可复算、可解释）；
  * 阈值全部来自 config.signals，代码里不埋数字。

为什么用 n-gram 而不是分词：多一个依赖就多一处会坏的地方，而「连着用了四次同一个
说法」这件事，用连续片段完全量得出来，还能把证据原样摊给人看。
"""
from __future__ import annotations

import re
from typing import Iterable, Sequence

from . import config

# 标点与空白不参与片段统计：它们会让「真的，真的，真的」这种重复被切开而漏检
_CLEAN_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff\u3400-\u4dbf]+")


def clean(text: str) -> str:
    return _CLEAN_RE.sub("", str(text or ""))


def _grams(body: str, size: int) -> list[str]:
    if size <= 0 or len(body) < size:
        return []
    return [body[i:i + size] for i in range(len(body) - size + 1)]


def _count(grams: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for one in grams:
        out[one] = out.get(one, 0) + 1
    return out


def repeated_phrases(turns: Sequence[str]) -> list[dict]:
    """连着几轮里反复出现的连续片段（优先报最长的那一段）。"""
    min_n = config.signal_int("repeat_gram_min", 2)
    max_n = max(min_n, config.signal_int("repeat_gram_max", 6))
    base = config.signal_int("repeat_min_count", 3)
    # 两字片段最容易和常用词撞车，所以它单独一道更高的门槛；数字也在配置里，不埋在这
    short = config.signal_int("repeat_min_count_short", base + 1)
    need_turns = max(1, config.signal_int("repeat_min_turns", 2))
    # 这份「常用词不算重复」的清单只有一个来源：配置文件。
    # 原来这里还写死了一份和配置一样的表，等于同一个东西有三处口径（其中一处读的还是
    # 根本不存在的顶层键）——那是 §11 说的那种会长成规则堆的东西，现在只留配置一处，
    # 配置文件整个不见了时退到 config._SAFE_DEFAULTS 里那一份。
    ignore = set(config.signal_list("ignored_phrases"))
    scored: list[tuple[int, int, str]] = []          # (-turns, -count, gram)
    for size in range(max_n, min_n - 1, -1):
        per_turn: dict[str, int] = {}
        total: dict[str, int] = {}
        for one in turns:
            counts = _count(_grams(clean(one), size))
            for gram, value in counts.items():
                total[gram] = total.get(gram, 0) + value
                if value:
                    per_turn[gram] = per_turn.get(gram, 0) + 1
        threshold = base if size > 2 else short
        for gram, value in total.items():
            if gram in ignore or value < threshold:
                continue
            if per_turn.get(gram, 0) < need_turns:
                continue
            scored.append((-per_turn[gram], -value, gram))
    scored.sort()
    picked: list[str] = []
    for _neg_turns, _neg_count, gram in scored:
        # 已经报了长的，就不再报它的子串，免得一段话被拆成三句提醒
        if any(gram in one or one in gram for one in picked):
            continue
        picked.append(gram)
        if len(picked) >= config.signal_int("repeat_max_report", 3):
            break
    turns_map = {gram: sum(1 for one in turns if gram in clean(one)) for gram in picked}
    return [{"kind": "repeat_phrase", "phrase": gram,
             "turns": turns_map.get(gram, 0)} for gram in picked]


def longest_shared(a: str, b: str) -> str:
    """两段话里最长的公共连续片段（复述与换词重复都用它来量）。"""
    left, right = clean(a), clean(b)
    if not left or not right:
        return ""
    prev = [""] * (len(right) + 1)
    best = ""
    for i in range(1, len(left) + 1):
        cur = [""] * (len(right) + 1)
        for j in range(1, len(right) + 1):
            if left[i - 1] == right[j - 1]:
                cur[j] = prev[j - 1] + left[i - 1]
                if len(cur[j]) > len(best):
                    best = cur[j]
        prev = cur
    return best


def echoed_user(user_text: str, last_reply: str) -> list[dict]:
    """她上一轮把对方刚说的话原样还回去。"""
    need = config.signal_int("echo_min_chars", 6)
    if need <= 0 or not user_text or not last_reply:
        return []
    shared = longest_shared(user_text, last_reply)
    if len(shared) < need:
        return []
    return [{"kind": "echo_user", "phrase": shared, "chars": len(shared)}]


def restated_views(turns: Sequence[str]) -> list[dict]:
    """同一个意思换个说法又讲一遍：相邻两轮有大段重合，但又不是一字不差地抄。"""
    need = config.signal_int("restated_min_shared", 8)
    out: list[dict] = []
    if need <= 0:
        return out
    for older, newer in zip(list(turns)[:-1], list(turns)[1:]):
        shared = longest_shared(older, newer)
        if len(shared) < need:
            continue
        if clean(shared) == clean(older) or clean(shared) == clean(newer):
            continue                      # 整句照搬由 echo/repeat 负责，这里不重复报
        out.append({"kind": "restated_view", "phrase": shared, "chars": len(shared)})
        break
    return out


def repeated_openers(turns: Sequence[str]) -> list[dict]:
    """每轮开头都用同一个句式。"""
    size = config.signal_int("opener_prefix_chars", 5)
    need = config.signal_int("opener_min_repeat", 2)
    if size <= 0 or need <= 0 or len(turns) < need:
        return []
    heads = [clean(one)[:size] for one in turns if clean(one)]
    counts = _count(heads)
    out = [{"kind": "repeat_opener", "phrase": gram, "count": value}
           for gram, value in counts.items() if value >= need and len(gram) >= max(2, size - 1)]
    return out[:config.signal_int("opener_max_report", 2)]


def cliche_overuse(turns: Sequence[str]) -> list[dict]:
    """客服腔/万能安慰话：只有她最近真的说过才提，说了几次就提几次（清单在配置里）。"""
    templates = config.string_list("cliche_templates")
    need = config.signal_int("cliche_min_count", 1)
    if need <= 0 or not templates:
        return []
    joined = "".join(clean(one) for one in turns)
    out = []
    for one in templates:
        body = clean(one)
        if not body:
            continue
        value = joined.count(body)
        if value >= need:
            out.append({"kind": "cliche", "phrase": one, "count": value})
    return out[:config.signal_int("cliche_max_report", 2)]


def summary_tone_repeats(turns: Sequence[str]) -> list[dict]:
    """每轮都拿总结腔收尾。"""
    openers = config.string_list("summary_tone_openers")
    need = config.signal_int("summary_min_count", 2)
    if need <= 0 or not openers or len(turns) < need:
        return []
    hits = 0
    last_seen = ""
    for one in turns:
        tail = clean(one)[-14:]
        for opener in openers:
            if clean(opener) and clean(opener) in tail:
                hits += 1
                last_seen = opener
                break
    if hits >= need:
        return [{"kind": "summary_tone", "phrase": last_seen, "count": hits}]
    return []


def length_monotony(turns: Sequence[str]) -> list[dict]:
    """每轮长短几乎一样：真人说话有长有短，这一点能直接量出来。"""
    need_turns = config.signal_int("length_monotony_min_turns", 4)
    tolerance = config.signal_int("length_monotony_tolerance_chars", 12)
    floor = config.signal_int("length_monotony_min_len", 20)
    if need_turns <= 0 or len(turns) < need_turns:
        return []
    body = [len(clean(one)) for one in turns][-need_turns:]
    if min(body) < floor:
        return []                         # 都是短句时长短接近是正常现象，不算毛病
    spread = max(body) - min(body)
    if spread > tolerance:
        return []
    return [{"kind": "length_monotony", "turns": len(body),
             "avg": int(sum(body) / len(body)), "spread": spread}]


# ---------------------------------------------------------------------------
# 命中之后要说什么：全部是「这一轮怎么改」的正向目标，不是「禁止做什么」
# ---------------------------------------------------------------------------

_GOALS = {
    "repeat_phrase": "这几句里她连着用了「{phrase}」好几位：这一轮要么换个说法，"
                     "要么干脆不用它。",
    "echo_user": "对方刚说的是「{phrase}」：不要把他这句话原样还给他，接到它后面去。",
    "restated_view": "「{phrase}」这层意思上一轮已经讲过了：这一轮要么换个角度，"
                     "要么就别再讲它。",
    "repeat_opener": "有几轮的开头都长成「{phrase}」这个样子：这一轮换个起法。",
    "cliche": "「{phrase}」这类万能安慰话她已经说过 {count} 次：这一轮按她自己的说法讲，"
              "别拿这句顶。",
    "summary_tone": "她连着拿「{phrase}」这种总结腔收尾：这一轮该说完就说完，"
                    "不用给每段话都收个尾巴。",
    "length_monotony": "最近 {turns} 轮都稳定在 {avg} 个字上下：这一轮按内容该多长就多长，"
                       "不必维持同一个长度。",
}


def detect(inp) -> tuple[list[dict], list[str]]:
    """跑一遍所有检测，返回「证据」和「对应的正向目标句」。"""
    turns = list(inp.recent_assistant)
    last_reply = turns[-1] if turns else ""
    found: list[dict] = []
    found += repeated_phrases(turns)
    found += echoed_user(inp.user_text, last_reply)
    found += restated_views(turns)
    found += repeated_openers(turns)
    found += cliche_overuse(turns)
    found += summary_tone_repeats(turns)
    found += length_monotony(turns)
    lines: list[str] = []
    seen: set[str] = set()
    for one in found:
        template = _GOALS.get(one.get("kind") or "")
        if not template:
            continue
        try:
            line = template.format(**one)
        except (KeyError, IndexError, ValueError):
            continue
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
    cap = config.signal_int("max_signal_lines", 4)
    return found, lines[:cap] if cap > 0 else lines


__all__ = ["clean", "repeated_phrases", "longest_shared", "echoed_user", "restated_views",
           "repeated_openers", "cliche_overuse", "summary_tone_repeats", "length_monotony",
           "detect"]
