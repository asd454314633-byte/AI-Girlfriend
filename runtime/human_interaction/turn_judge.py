# -*- coding: utf-8 -*-
"""V2 的核心：「本轮互动判断」——正式回复之前，先看一眼这一句到底该怎么接。

V1 为什么没做到逐轮理解，报告里已经量化了：一个「真人自然」档 36 格里只有 2 种
不同的指导文本，带证据行的只有 6 条。根因是结构性的——固定文案 + 「材料在不在」
的门，加上一次模型调用都不许多，于是这一层只会**提醒**模型「请理解语境」，
它自己并没有理解。这一模块就是把「理解」这一步真的做出来。

它只回答两个问题：这一轮是什么互动？她此刻该用什么态度、什么方式接？
它**不许**写她这一句要说的话——写台词是主模型的事。

四条边界（V2 唯一被授权的变化就是「多一次很短的模型调用」，除此之外一律照旧）：

  * 同一个 8090、同一个模型、同一份采样口径（请求体不带采样参数，见 llm_client）。
    不新增模型、不新增服务、不新增端口、不新增库。这一发唯一与主请求不同的地方是
    「不许先想一大段」：实测裸发要 8.47 秒、其中 2423 字是主回复根本用不上的思考，
    关掉后 0.9 秒且六个字段照样填满；这只影响这一次判断，主回复那一发照旧。
  * 只由**每轮的调用方**触发：测试台现在这么调，将来实时链的钩子也这么调。
    装配路径（context_builder → engine.attach）绝不走到这里，所以连线时那次
    ``GET /api/character/prompt`` 不会因为这一层多等一个模型；
  * 两道闸：``feature_enabled`` 关 → 根本走不到；``turn_judge_enabled`` 关 →
    一个字都不发，本层逐字等于 V1。两个默认都是关；
  * 任何失败（超时、连不上、六个字段没解析出来）都回「没有判断」，
    调用方拿到空文本，行为退回 V1。坏了不许把主回复带崩。

输出六个字段的名字**不在代码里**：它们从这一档的简报文案里扫出来（``declared_keys``）。
文案里写几行「键：」，这里就解析几个键。想加一个字段 = 改 txt，不改代码，
也不会出现「代码要三个键、文案教四个键」这种两张皮。
"""
from __future__ import annotations

import re
import time
from typing import Any

from . import config, context as hi_context, llm_client, modes

HEADER = "【本轮互动判断】"

# 简报里「键：」那种行 —— 行尾是个空冒号，说明它在声明一个要填的字段。
_KEY_LINE = re.compile(r"^\s*(?:[-*]\s*)?([^:：]{2,14}?)\s*[:：]\s*$")
# 模型回执里那一行 —— 同一个键，后面跟着值。
_FILLED = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?\s*([^:：]{2,14}?)\s*[:：]\s*(.+?)\s*$")
_QUOTES = "\"“”‘'`"
# V2b：判断结果压成一行，字段之间用竖线/分号隔开。四种写法都收（全角竖线、半角竖线、
# 全角分号、半角分号），因为模型给的分隔符不稳定；换行照旧支持——老的多行形状不改。
_FRAGMENT = re.compile(r"[｜|；;\n]")
JOINER = "｜"



def declared_keys(brief: str) -> tuple[str, ...]:
    """从简报里扫出它自己要求的那几个键，顺序照文案里的出现顺序。"""
    out: list[str] = []
    for line in str(brief or "").splitlines():
        hit = _KEY_LINE.match(line)
        if not hit:
            continue
        key = hit.group(1).strip().strip(_QUOTES).rstrip("*").strip()
        if key and key not in out:
            out.append(key)
    return tuple(out)


def _clean_value(raw: str, limit: int) -> tuple[str, bool]:
    """把一格值收成一行：去壳、压空白、封顶。返回 (值, 是否被截过)。"""
    body = str(raw or "").strip()
    body = re.sub(r"\*\*", "", body).strip(_QUOTES).strip()
    body = re.sub(r"\s+", " ", body).rstrip("。；;，,")
    over = len(body) > limit
    return (body[:limit] if over else body), over


def parse(text: str, keys: tuple[str, ...], *, value_max_chars: int) -> dict[str, Any]:
    """按简报声明的那几个键取值。取不到就是空串，不猜、不补写。

    V2b 起，模型交回来的是**一行**「键：值｜键：值」，所以这里先把整段按分隔符切成
    碎片再逐片取值。老的多行形状（一行一个「键：值」）切完还是同样几片，照旧能解析——
    两形共用一条路径，不留"测试里能解析、线上形状不同"这种两张皮。
    """
    fields: dict[str, str] = {one: "" for one in keys}
    notes: list[str] = []
    overlong: list[str] = []
    for line in _FRAGMENT.split(str(text or "")):
        hit = _FILLED.match(line)
        if not hit:
            continue
        key = hit.group(1).strip().strip(_QUOTES).rstrip("*").strip()
        if key not in fields or fields[key]:
            continue                      # 不是声明的键，或者这个键已经取过值了
        value, was_cut = _clean_value(hit.group(2), value_max_chars)
        if value:
            fields[key] = value
            if was_cut:
                overlong.append(key)
    missing = [one for one in keys if not fields[one]]
    if missing:
        notes.append("MISSING:" + "、".join(missing))
    if overlong:
        notes.append("OVERLONG:" + "、".join(overlong))
    return {"fields": fields, "missing": missing, "notes": notes,
            "filled": len(keys) - len(missing)}


def render(fields: dict[str, str], keys: tuple[str, ...], *, block_max_chars: int) -> str:
    """把判断渲染成要挂进系统那一块的一小段话。一个字段都没取到 → 整块不出。

    V2b：整块压成**一行**，标题紧跟第一个字段，字段之间用「｜」。
    原因只有一个，而且是量出来的：让正式回复变贵的是"上下文里多挂了一段结构文本"
    （同样长、内容读错了的那一段一样贵，见 V2 门槛报告 §4 的 B/C 两臂），
    所以这段文字的字节数就是延迟账上的本金。判断的六个字段一个没减、含义一个没改，
    改的只有排版。
    """
    parts = [f"{one}：{fields.get(one, '')}" for one in keys if str(fields.get(one) or "")]
    if not parts:
        return ""
    block = HEADER + JOINER.join(parts)
    if block_max_chars > 0 and len(block) > block_max_chars:
        block = block[:block_max_chars].rstrip()
    return block


def materials(inp: hi_context.InteractionInput, *, history_turns: int,
              memory_lines: int, persona_max_chars: int) -> str:
    """递给判断器的那四样材料。形状固定，内容全部来自本层已有的那一份归一化输入。"""
    parts: list[str] = []
    persona = str(inp.persona_text or "").strip()
    if persona_max_chars > 0 and len(persona) > persona_max_chars:
        persona = persona[:persona_max_chars].rstrip() + "…"
    if persona:
        parts.append("她是谁：\n" + persona)
    if inp.relationship_text:
        parts.append("你们的关系：\n" + str(inp.relationship_text).strip())
    if inp.temporary_text:
        parts.append("这一场临时约的事：\n" + str(inp.temporary_text).strip())
    if inp.scene_text:
        parts.append("这一场聊到哪儿了：\n" + str(inp.scene_text).strip())
    rows = list(inp.history)
    if history_turns > 0 and len(rows) > history_turns:
        rows = rows[-history_turns:]
    if rows:
        parts.append("最近几句：\n" + "\n".join(
            f"{'对方' if who == 'user' else '她'}：{body}" for who, body in rows))
    if memory_lines > 0 and inp.memory_lines:
        parts.append("她还记得的几件真事：\n" + "\n".join(
            f"- {one}" for one in inp.memory_lines[:memory_lines]))
    parts.append("对方刚说的这一句：\n" + (str(inp.user_text or "").strip() or "（没说话）"))
    return "\n\n".join(parts)


def judge(*, mode: str | None = None, character_id: str = "", session_id: str = "",
          persona_text: Any = None, temporary_text: Any = None, scene_text: Any = None,
          relationship_text: Any = None, memory_lines: Any = None,
          history: Any = None, user_text: Any = None,
          client: Any = None) -> dict[str, Any]:
    """跑一次本轮判断。返回里 ``text`` 为空 = 这一轮没有判断（等价于 V1）。

    ``client`` 只给单测注入假回执用；生产与测试台都不传，走 ``llm_client.complete_short``。
    """
    out: dict[str, Any] = {"ok": False, "reason": "", "text": "", "fields": {},
                           "keys": [], "notes": [], "wall_s": 0.0,
                           "prompt_chars": 0, "usage": {}, "timings": {},
                           "raw": "", "reasoning_chars": 0, "requested": False,
                           "finish": "", "sent": []}
    started = time.time()
    try:
        if not config.enabled():
            out["reason"] = "DISABLED_BY_FLAG"
            return out
        if not config.turn_judge_enabled():
            out["reason"] = "DISABLED_BY_JUDGE_FLAG"
            return out

        inp = hi_context.build(mode=mode, character_id=character_id, session_id=session_id,
                               persona_text=persona_text, temporary_text=temporary_text,
                               scene_text=scene_text, relationship_text=relationship_text,
                               memory_lines=memory_lines, history=history,
                               user_text=user_text)
        out["mode"] = inp.mode
        ok, why = modes.allowed_now(inp.mode)
        if not ok:
            out["reason"] = f"MODE_NOT_ALLOWED:{why}"
            return out

        brief = modes.judge_prompt_text(inp.mode)
        if not brief:
            out["reason"] = "NO_BRIEF_FOR_MODE"
            return out
        keys = declared_keys(brief)
        out["keys"] = list(keys)
        if len(keys) < 2:
            out["reason"] = "BRIEF_DECLARES_NO_FIELDS"
            return out
        if not str(inp.user_text or "").strip():
            out["reason"] = "NO_USER_TEXT_TO_JUDGE"
            return out

        body = materials(inp,
                         history_turns=config.judge_int("turn_judge_history_turns", 8),
                         memory_lines=config.judge_int("turn_judge_memory_lines", 0),
                         persona_max_chars=config.judge_int("turn_judge_persona_max_chars", 700))
        out["prompt_chars"] = len(brief) + len(body)
        send = client or llm_client.complete_short
        row = send(brief, body, timeout_s=float(config.judge_int("turn_judge_timeout_s", 5)),
                   max_tokens=int(config.judge_int("turn_judge_max_tokens", 200)))
        out["wall_s"] = round(time.time() - started, 2)
        out["requested"] = True
        if not isinstance(row, dict) or not row.get("ok"):
            out["reason"] = f"CLIENT_FAILED:{str((row or {}).get('error'))[:120]}" \
                if isinstance(row, dict) else "CLIENT_RETURNED_NON_DICT"
            return out
        raw = str(row.get("content") or "")
        out["raw"] = raw[:1200]
        out["usage"] = row.get("usage") or {}
        out["timings"] = row.get("timings") or {}
        out["reasoning_chars"] = int(row.get("reasoning_chars") or 0)
        out["client_wall_s"] = row.get("wall_s")
        out["finish"] = str(row.get("finish") or "")
        out["sent"] = row.get("sent") or []
        if out["finish"] == "length":
            out["notes"].append("TRUNCATED_BY_MAX_TOKENS")

        parsed = parse(raw, keys, value_max_chars=config.judge_int("turn_judge_value_max_chars", 24))
        out["fields"] = parsed["fields"]
        out["notes"].extend(parsed["notes"])
        need = max(2, (len(keys) + 1) // 2)
        if parsed["filled"] < need:
            out["reason"] = f"PARSE_FAILED:{parsed['filled']}/{len(keys)}"
            return out
        out["text"] = render(parsed["fields"], keys,
                             block_max_chars=config.judge_int("turn_judge_block_max_chars", 260))
        if not out["text"]:
            out["reason"] = "EMPTY_AFTER_RENDER"
            return out
        out["ok"] = True
        return out
    except Exception as exc:                                   # noqa: BLE001
        out["reason"] = f"JUDGE_ERROR:{type(exc).__name__}"
        out["wall_s"] = round(time.time() - started, 2)
        out["text"] = ""
        out["ok"] = False
        return out


__all__ = ["HEADER", "declared_keys", "parse", "render", "materials", "judge"]
