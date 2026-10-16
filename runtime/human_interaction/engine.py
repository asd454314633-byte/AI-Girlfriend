# -*- coding: utf-8 -*-
"""本层的唯一出口：把八项输入算成一段「这一轮怎么接」的补充话。

对外只有两个函数：

    interaction_instruction(...) -> str     任务 §5 里那个唯一输出；空串代表什么都不加
    attach(policy_text, ...) -> str          给装配引擎用的那一个薄入口

四条硬规矩：

  1. 总闸关着 → 恒返回空串，旧链一个字都不多；
  2. 里面任何一步出异常 → 返回原样传回的那块话（新层坏了绝不能带坏旧链）；
  3. 出口前过一遍现成的「内部名词不许露出来」口径，露了就当场把那几句剔掉；
  4. 不写模型名、不写人物名、不写具体话术分支——差异只来自 modes 的文案与配置。

文案里的 ``[#turn]`` 这类标签是**声明**「这一段要对方这句在场才成立」，不是逻辑分支：
标签到生效条件的那张表在 ``_EMIT`` 里，一共几行，一眼数得完。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Sequence

from . import config, context as hi_context, guard, humanizer, modes

_SECTION_RE = re.compile(r"^\[#([a-zA-Z_]+)\]\s*$")

# 标签 -> 这一段现在成不成立。全部是「材料在不在」这种结构事实。
_EMIT: dict[str, Callable[[Any], bool]] = {
    "": lambda inp: True,
    "turn": lambda inp: bool(inp.user_text),
    "chat": lambda inp: bool(inp.history),
    "first": lambda inp: not inp.history,
    "scene": lambda inp: bool(inp.scene_text) or bool(inp.history),
    "memory": lambda inp: bool(inp.memory_lines),
}

_SENTENCE_END = ("。", "！", "？", "；", "\n")


def _sections(text: str) -> list[tuple[str, str]]:
    """把文案切成 (标签, 正文) 几段。

    口径：**标签只管紧跟它的那一个自然段**。空行分隔段落；段首若是 ``[#xxx]``
    就把这一段挂在 xxx 上，其余段落标签为空串（无条件出）。
    这样 [#turn] 不会把它下面本来无条件的段落一起吃掉——那正好是最容易犯的错。
    """
    body = str(text or "").replace("\r\n", "\n")
    rows: list[tuple[str, str]] = []
    for paragraph in re.split(r"\n[ \t]*\n", body):
        lines = [one for one in paragraph.splitlines() if one.strip()]
        if not lines:
            continue
        tag = ""
        hit = _SECTION_RE.match(lines[0].strip())
        if hit:
            tag = hit.group(1).strip().lower()
            lines = lines[1:]
        text_body = "\n".join(lines).strip()
        if text_body:
            rows.append((tag, text_body))
    return rows


def _fit(text: str, max_chars: int) -> str:
    """压进它那一格：整句砍，不拦腰切。

    不复用装配引擎那个同名函数，是因为它在那一层、而这一层被那一层调用——
    反向引用会绕成一个导入环。行为对齐，实现独立。
    """
    body = str(text or "")
    if max_chars <= 0 or len(body) <= max_chars:
        return body
    cut = body[:max_chars]
    best = -1
    for mark in _SENTENCE_END:
        best = max(best, cut.rfind(mark))
    return (cut[:best + 1] if best >= max_chars * 0.4 else cut).rstrip()


# 判断块压成一行之后，"一格"和"一行"不是一回事了：一格之间用这几个符号隔开。
# 与 turn_judge 那边保持同一套符号（那边负责解析模型回执，这边负责出口体检），
# 不 import 过来是为了让装配这一层不因为一个分隔符常量去依赖发请求那一模块。
_PIECE = re.compile(r"[｜|；;]")
_JOIN = "｜"


def _hygiene(text: str) -> tuple[str, list[str]]:
    """出口前拿现成的内部名词口径扫一遍，露馅的那几句当场剔掉。

    V2b 起多了一层形状上的现实：本轮判断压成**一行**之后，"按行剔"等于"整块剔"——
    一个漏词会把六个字段一起带走。所以剔的粒度跟着形状走：整行脏了先试着按字段拆
    （「｜」/分号），脏的那一格丢掉，干净的那几格留下；拆完什么都不剩才整行不要。
    这只影响"含露馅词的那一行"，正常文本一个字节都不动（OFF 路径逐字节相同的证明里就含它）。
    """
    try:
        from runtime.character import rules
    except Exception:
        return text, ["HYGIENE_GATE_UNAVAILABLE"]
    try:
        leaked = rules.find_banned(text)
    except Exception:
        return text, ["HYGIENE_GATE_ERROR"]
    if not leaked:
        return text, []
    kept: list[str] = []
    for one in text.splitlines():
        if not rules.find_banned(one):
            kept.append(one)
            continue
        pieces = [frag for frag in _PIECE.split(one) if frag.strip()]
        if len(pieces) <= 1:
            continue                                  # 不是多字段那一行，整行照旧剔掉
        clean = [frag.strip() for frag in pieces if not rules.find_banned(frag)]
        if clean:
            kept.append(_JOIN.join(clean))
    return "\n".join(kept).strip(), [f"HYGIENE_STRIPPED:{'、'.join(leaked)}"]



def interaction_instruction(*, mode: str | None = None, character_id: str = "",
                            session_id: str = "", persona_text: Any = None,
                            temporary_text: Any = None, scene_text: Any = None,
                            relationship_text: Any = None, memory_lines: Any = None,
                            history: Any = None, user_text: Any = None,
                            mode_explicitly_chosen: bool | None = None,
                            turn_judgment: Any = None) -> str:
    """任务 §5 里那个唯一输出。"""
    return explain(mode=mode, character_id=character_id, session_id=session_id,
                   persona_text=persona_text, temporary_text=temporary_text,
                   scene_text=scene_text, relationship_text=relationship_text,
                   memory_lines=memory_lines, history=history, user_text=user_text,
                   mode_explicitly_chosen=mode_explicitly_chosen,
                   turn_judgment=turn_judgment)["text"]


def _judgment_block(turn_judgment: Any) -> tuple[str, str]:
    """把判断器那份回执收成一段话。返回 (那段话, 没拿到话的原因)。

    只认两种形状：``judge()`` 的整个回执 dict，或者已经渲染好的那段文本。
    形状不对就当没有——本层绝不因为「判断器坏了」而少出原来那几行。
    """
    if turn_judgment is None:
        return "", "NOT_PROVIDED"
    if isinstance(turn_judgment, str):
        text = turn_judgment.strip()
        return (text, "") if text else ("", "EMPTY_TEXT")
    if isinstance(turn_judgment, dict):
        text = str(turn_judgment.get("text") or "").strip()
        if text:
            return text, ""
        return "", f"ABSENT:{turn_judgment.get('reason') or 'NO_TEXT'}"
    return "", "UNKNOWN_SHAPE"


def explain(*, mode: str | None = None, character_id: str = "", session_id: str = "",
            persona_text: Any = None, temporary_text: Any = None, scene_text: Any = None,
            relationship_text: Any = None, memory_lines: Any = None,
            history: Any = None, user_text: Any = None,
            mode_explicitly_chosen: bool | None = None,
            turn_judgment: Any = None) -> dict[str, Any]:
    """同一个算法，但把「为什么出这几句」一并摊开，供测试台与验收报告复算。

    ``turn_judgment`` 是 V2 加的那一格，默认不传。**不传时这里输出的每个字节都跟
    V1 一致**——传进来的是「本轮互动判断」那段话，位置在标题之后、一般说法之前，
    所以长度预算真要用到让位时，先让位的是后面那篇固定话术，不是这一轮的判断。
    """
    result = {"text": "", "mode": "", "label": "", "sections_used": [], "signals": [],
              "evidence": [], "anomalies": [], "notes": [], "chars": 0,
              "judgment": {"provided": turn_judgment is not None, "used": False,
                           "chars": 0, "fields": {}}}
    if not config.enabled():
        result["notes"].append("DISABLED_BY_FLAG")
        return result

    inp = hi_context.build(mode=mode, character_id=character_id, session_id=session_id,
                           persona_text=persona_text, temporary_text=temporary_text,
                           scene_text=scene_text, relationship_text=relationship_text,
                           memory_lines=memory_lines, history=history, user_text=user_text,
                           mode_explicitly_chosen=mode_explicitly_chosen)
    result["mode"] = inp.mode
    if inp.mode == config.MODE_OFF or not modes.is_known(inp.mode):
        result["notes"].append("MODE_OFF_OR_UNKNOWN")
        return result

    ok, why = modes.allowed_now(inp.mode)
    if not ok:
        result["notes"].append(f"MODE_NOT_ALLOWED:{why}")
        return result

    spec = modes.spec(inp.mode)
    result["label"] = modes.label(inp.mode)

    parts: list[str] = []
    header = f"【这一轮怎么接（{result['label']}）】"
    parts.append(header)

    block, why_absent = _judgment_block(turn_judgment)
    if block:
        parts.append(block)
        result["judgment"]["used"] = True
        result["judgment"]["chars"] = len(block)
        if isinstance(turn_judgment, dict):
            result["judgment"]["fields"] = dict(turn_judgment.get("fields") or {})
        result["sections_used"].append("judgment")
    elif turn_judgment is not None:
        result["notes"].append(why_absent)

    body_text = modes.prompt_text(inp.mode)
    if not body_text:
        result["notes"].append("MODE_PROMPT_EMPTY")
        return result
    for tag, block in _sections(body_text):
        gate = _EMIT.get(tag)
        if gate is None:
            result["notes"].append(f"UNKNOWN_TAG:{tag}")
            continue
        if not gate(inp):
            result["notes"].append(f"SECTION_SKIPPED:{tag or 'plain'}")
            continue
        result["sections_used"].append(tag or "plain")
        parts.append(block)

    # 材料这一轮在场就顺手指一句，全是指向性事实，不评价、不规定姿态
    anchors = []
    if inp.relationship_text:
        anchors.append("你们之间是什么关系，前面已经写过了，照那个来。")
    if inp.temporary_text:
        anchors.append("这一场还临时约了一件事，那段话优先级高于下面这些一般说法。")
    if inp.scene_text:
        anchors.append("这一场已经聊到哪儿了，也在前面。")
    if inp.memory_lines:
        anchors.append(f"她前面真实说过的话有 {len(inp.memory_lines)} 条，别当成刚编的。")
    if anchors:
        parts.append("\n".join(anchors))

    # 交付通道那句：三个模式共用，写在配置里，代码里没有「哪个模式该怎么说」的分支。
    # 空串就是不出这句——它就是这一层所有行为差异的唯一出处（配置 + 文案）。
    channel = str(config.channel_note() or "").strip()
    if channel:
        parts.append(channel)

    evidence: list[dict] = []
    if spec is not None and spec.use_detected_signals and modes.uses_signals(inp.mode):
        evidence, signal_lines = humanizer.detect(inp)
        if signal_lines:
            parts.append("这一场她最近说过的话里，量到这几处：\n" +
                         "\n".join(f"- {one}" for one in signal_lines))
        result["signals"] = signal_lines
    result["evidence"] = evidence

    anomalies: list[dict] = []
    try:
        anomalies = guard.check(inp, evidence)
    except Exception as exc:                       # 检查层坏了不影响主输出
        result["notes"].append(f"GUARD_ERROR:{type(exc).__name__}")
    result["anomalies"] = list(anomalies)

    block, notes = _hygiene("\n\n".join(one for one in parts if one).strip())
    result["notes"].extend(notes)
    block = _fit(block, config.instruction_max_chars())
    if not block:
        result["notes"].append("EMPTY_AFTER_TRIM")
    result["text"] = block
    result["chars"] = len(block)
    result["input_snapshot"] = inp.snapshot()
    return result


def revision_directive(**inputs: Any) -> dict[str, Any]:
    """第二遍修正用的那句话术目标。

    本层**自己不发模型请求**：谁要跑第二遍，谁拿这段目标去调自家的模型。
    生产默认 ``revise_enabled = false``，所以这条路在旧链上根本不会被走到。
    """
    row = explain(**inputs)
    if not config.revise_enabled():
        return {"needed": False, "reason": "REVISE_FLAG_OFF", "anomalies": row["anomalies"]}
    needed = guard.should_revise(row["anomalies"])
    return {"needed": bool(needed), "reason": "" if needed else "BELOW_SEVERITY",
            "directive": modes.extra_text("humanizer") if needed else "",
            "targets": row["signals"], "anomalies": row["anomalies"]}


def attach(policy_text: Any, *, character_id: str = "", session_id: str = "",
           persona_text: Any = None, temporary_text: Any = None, scene_text: Any = None,
           relationship_text: Any = None, memory_lines: Any = None,
           history: Any = None, user_text: Any = None,
           turn_judgment: Any = None) -> str:
    """装配引擎里那唯一的薄入口：关着 / 出错 / 没话可说，都原样退回。

    ``turn_judgment`` 不经过这里也能生效：实时链那一轮是从钩子直接取
    ``turn_judge.judge()`` 的 ``text`` 拼进本轮 instructions 的。这个参数是为了让
    测试台能走**同一条**装配口径，而不是在测试里另拼一份形状。
    """
    base = str(policy_text or "")
    try:
        if not config.enabled():
            return base
        block = interaction_instruction(character_id=character_id, session_id=session_id,
                                        persona_text=persona_text,
                                        temporary_text=temporary_text, scene_text=scene_text,
                                        relationship_text=relationship_text,
                                        memory_lines=memory_lines, history=history,
                                        user_text=user_text, turn_judgment=turn_judgment)
    except Exception:
        return base
    if not block:
        return base
    return (base + "\n\n" + block) if base else block


__all__ = ["interaction_instruction", "explain", "attach", "revision_directive"]
