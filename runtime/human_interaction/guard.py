# -*- coding: utf-8 -*-
"""轻量检查：只有任务 §9 许可的那五条可解释规则，不许长成几十个判断器。

五条规则各回答一个问题，且每条都必须是「拿当前输入能复算出来」的判断：

    REPEAT      明显重复——她最近几轮真的反复用同一个说法/同一个开头
    RESTATE     明显复述——她把对方的话原样还回去，或换个词把同一意思又讲一遍
    STENCIL     明显套话——客服腔那句她这阵子真的说过，或每轮都拿总结腔收尾
    MISMATCH    明显场景错位——这一档要参照的材料这轮根本不在（没人开口、没发生过事）
    MODE_CLASH  明显模式冲突——选中的这一档现在并不该生效（配置不许/没被明确点过）

新增第六条之前，先按任务 §20 回设计层重审，不要在这里往 if 链上再加一支。
"""
from __future__ import annotations

from typing import Any

from . import config, modes

RULES = ("REPEAT", "RESTATE", "STENCIL", "MISMATCH", "MODE_CLASH")

# 每条规则触发「值得再过一遍表达」的最低严重度。都在配置里，代码里没有藏数字。
_DEFAULT_MIN_SEVERITY = {"REPEAT": 2, "RESTATE": 2, "STENCIL": 2, "MISMATCH": 1,
                         "MODE_CLASH": 3}


def _severity(kind: str, hits: list[dict]) -> int:
    """严重度 = 这条规则实际量到的最大次数（没命中就是 0）。"""
    values = []
    for one in hits:
        for key in ("count", "turns", "chars"):
            value = one.get(key)
            if isinstance(value, int):
                values.append(value)
                break
    return max(values) if values else 0


def check(inp, evidence: list[dict]) -> list[dict[str, Any]]:
    """跑这五条，返回异常清单。空表 = 这轮不必进第二遍。"""
    out: list[dict[str, Any]] = []

    def add(rule: str, hits: list[dict], note: str) -> None:
        if not hits:
            return
        out.append({"rule": rule, "severity": _severity(rule, hits),
                    "evidence": hits, "note": note})

    add("REPEAT", [one for one in evidence
                   if one.get("kind") in ("repeat_phrase", "repeat_opener")],
        "最近几轮有说法在反复出现")
    add("RESTATE", [one for one in evidence
                    if one.get("kind") in ("echo_user", "restated_view")],
        "有把对方的话或自己刚说过的意思再讲一遍的迹象")
    add("STENCIL", [one for one in evidence
                    if one.get("kind") in ("cliche", "summary_tone")],
        "有万能安慰话或总结腔反复收尾的迹象")

    # MISMATCH 只判「材料在不在」这种结构事实，不猜这句话是什么语气
    mismatch: list[dict] = []
    if inp.mode == config.MODE_STORY_DIRECTOR and not inp.scene_text and not inp.history:
        mismatch.append({"what": "story_without_material",
                         "detail": "这一档要接着已经发生过的事往下走，但这轮既没有近况也没有历史"})
    if not inp.user_text and not inp.history and not inp.scene_text:
        mismatch.append({"what": "nothing_to_read",
                         "detail": "对方还没开口，也没有这一场的材料可参照"})
    add("MISMATCH", mismatch, "这一档要参照的东西这一轮并不存在")

    clash: list[dict] = []
    if not modes.is_known(inp.mode):
        clash.append({"what": "mode_unknown", "detail": f"认不出这一档：{inp.mode}"})
    else:
        ok, why = modes.allowed_now(inp.mode)
        if not ok and inp.mode != config.MODE_OFF:
            clash.append({"what": "mode_not_allowed", "detail": why})
    add("MODE_CLASH", clash, "选中的档位现在不该生效")

    order = {name: index for index, name in enumerate(RULES)}
    out.sort(key=lambda one: (order.get(one["rule"], 99), -one["severity"]))
    return out


def min_severity(rule: str) -> int:
    row = config.config().get("guard_min_severity") or {}
    if isinstance(row, dict) and rule in row:
        try:
            return int(row[rule])
        except (TypeError, ValueError):
            pass
    return int(_DEFAULT_MIN_SEVERITY.get(rule, 2))


def should_revise(anomalies: list[dict]) -> bool:
    """五条里有一条量到「明显」的程度，才允许进第二遍修正。"""
    for one in anomalies:
        if int(one.get("severity") or 0) >= min_severity(str(one.get("rule"))):
            return True
    return False


def blocking(anomalies: list[dict]) -> bool:
    """模式冲突是结构问题：这一档本来就不该出话，整块直接不出。"""
    return any(one.get("rule") == "MODE_CLASH" for one in anomalies)


def explain(anomalies: list[dict]) -> list[str]:
    return [f"{one['rule']}(严重程度 {one.get('severity', 0)})：{one.get('note', '')}"
            for one in anomalies]


__all__ = ["RULES", "check", "should_revise", "blocking", "min_severity", "explain"]
