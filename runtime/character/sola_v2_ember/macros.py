# -*- coding: utf-8 -*-
"""SillyTavern 宏的最小解析器（只解析，不改写）。

为什么要这一份：作者官方 Sola V2 是 SillyTavern 预设，它的模块正文里混着两类东西 ——
一类是作者写给模型看的规则句子，一类是 SillyTavern 的模板宏
（``{{setvar}}`` / ``{{getvar}}`` / ``{{trim}}`` / ``{{//注释}}`` / ``{{char}}`` / ``{{user}}``）。
本项目的装配引擎没有 SillyTavern 的模板引擎，所以宏必须在「移植」这一步就地算完，
算完剩下的每个字都必须是作者原文。

口径（三条硬规矩）：
  * 只做 SillyTavern 语义要求做的替换，一个字的规则文案都不动：不压缩、不润色、
    不翻译、不加「针对某模型」的私货；
  * 宏要按花括号**深度**配对，不能按「第一个 }}」收尾 —— 作者确实在 setvar 的值里
    写了 ``{{user}}``，也在注释里写了 ``{{mod_pov}}``，非深度配对会把正文截断；
  * 认不出来的宏一律**原样留着**并登记，绝不静默删除：静默删宏就是在改作者的规则。

支持的宏（与 Ember 实际用到的完全对齐，逐个对照过作者的 25 个启用模块）：

  ====================  ==================================================
  ``{{setvar::名::值}}``  先把值算完再存进变量表；本位置产出空串
  ``{{getvar::名}}``     取回存过的值；没存过 = 空串（并登记）
  ``{{名}}``              同上，SillyTavern 允许省略 getvar:: 的简写
  ``{{trim}}``           删掉自己前后的空白（SillyTavern 原义）
  ``{{// 说明}}``         作者写给使用者看的注释，SillyTavern 不渲染给模型
  ``{{char}}``           换成当前人物名字（配置项 char_label）
  ``{{user}}``           换成当前对话对象称呼（配置项 user_label）
  ====================  ==================================================
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# 占位符：先把 {{trim}} 换成它，等所有宏展开完再统一吃空白。
# 用不可能出现在正文里的字节序列，避免和作者的文本撞车。
_TRIM_TOKEN = "\x00SOLA_TRIM\x00"
_TRIM_RE = re.compile(r"\s*" + re.escape(_TRIM_TOKEN) + r"\s*")

_NAME_RE = re.compile(r"^[A-Za-z0-9_@]+$")
_LABEL_RE = re.compile(r"^(char|user|persona|actual_user)$", re.I)


class MacroReport:
    """一次渲染里所有「不是作者规则正文」的处理记录，给验收清单用。"""

    def __init__(self) -> None:
        self.setvars: list[str] = []
        self.getvars: list[str] = []
        self.undefined_getvars: list[str] = []
        self.comments_dropped: int = 0
        self.trims: int = 0
        self.labels: dict[str, int] = {}
        self.unknown_macros: list[str] = []

    def as_dict(self) -> dict[str, Any]:
        return {
            "setvars_executed": len(self.setvars),
            "setvar_names": list(self.setvars),
            "getvars_expanded": len(self.getvars),
            "getvar_names": list(self.getvars),
            "undefined_getvars": list(self.undefined_getvars),
            "author_comments_dropped": self.comments_dropped,
            "trims_applied": self.trims,
            "label_substitutions": dict(self.labels),
            "unknown_macros_left_verbatim": list(self.unknown_macros),
        }


def _match_end(text: str, open_index: int) -> int:
    """从 ``{{`` 开始，按深度找到配对的 ``}}``，返回其结束后的下标。

    找不到就返回 -1 —— 调用侧当成普通文本，不猜。
    """
    depth = 0
    i = open_index
    n = len(text)
    while i < n - 1:
        two = text[i:i + 2]
        if two == "{{":
            depth += 1
            i += 2
        elif two == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return -1


def _split_setvar(body: str) -> tuple[str, str] | None:
    """``setvar::名::值`` → (名, 值)；名里不含 ':'，值按第一个 '::' 切。"""
    rest = body[len("setvar::"):]
    index = rest.find("::")
    if index <= 0:
        return None
    name = rest[:index].strip()
    if not _NAME_RE.match(name):
        return None
    return name, rest[index + 2:]


def _strip_prefix(body: str, token: str) -> str:
    """容错处理 SillyTavern 允许的空白（``{{ getvar::x }}``）。"""
    return body[len(token):].strip()


def render(content: str, *,
           labels: Mapping[str, str],
           variables: dict[str, str] | None = None,
           report: MacroReport | None = None) -> tuple[str, dict[str, str], MacroReport]:
    """把一段 SillyTavern 模块正文渲染成「作者真正发给模型的那些字」。

    ``variables`` 是跨模块共享的变量表 —— Sola 就是靠它在「Core Foundations」里清空、
    在某个模块里赋值、再到另一个模块里取用，所以整份预设必须按顺序共用一张表，
    不能一个模块一张表，否则 ``{{getvar}}`` 全变空串。
    """
    table = variables if variables is not None else {}
    rep = report if report is not None else MacroReport()
    text = content or ""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i:i + 2] != "{{":
            out.append(text[i])
            i += 1
            continue
        end = _match_end(text, i)
        if end < 0:                       # 配不上对：原样当文本，不猜
            out.append(text[i])
            i += 1
            continue
        body = text[i + 2:end - 2]
        lowered = body.strip().lower()

        if lowered.startswith("//"):               # 作者注释：SillyTavern 不渲染
            rep.comments_dropped += 1
        elif lowered.startswith("setvar::"):
            split = _split_setvar(body.strip())
            if split is None:
                rep.unknown_macros.append(text[i:end])
                out.append(text[i:end])
            else:
                name, raw = split
                value, _t, rep = render(raw, labels=labels, variables=table, report=rep)
                table[name] = value
                if name not in rep.setvars:
                    rep.setvars.append(name)
        elif lowered.startswith("getvar::"):
            name = _strip_prefix(body.strip(), "getvar::")
            rep.getvars.append(name)
            if name in table:
                out.append(table[name])
            else:
                if name not in rep.undefined_getvars:
                    rep.undefined_getvars.append(name)
        elif _NAME_RE.match(lowered) and lowered in table:   # {{mod_pov}} 简写
            rep.getvars.append(lowered)
            out.append(table[lowered])
        elif lowered == "trim":
            rep.trims += 1
            out.append(_TRIM_TOKEN)
        elif _LABEL_RE.match(lowered):
            value = str(labels.get(lowered) or labels.get(lowered.replace("actual_user", "user")) or "")
            if value:
                rep.labels[lowered] = rep.labels.get(lowered, 0) + 1
                out.append(value)
            else:                       # 没给称呼就留着，不悄悄吞掉
                rep.unknown_macros.append(text[i:end])
                out.append(text[i:end])
        else:
            rep.unknown_macros.append(text[i:end])
            out.append(text[i:end])
        i = end

    rendered = "".join(out)
    # 先吃 {{trim}} 及其两侧空白，再把没配对的宏名留下的空行收一收（只收 trim 造成的）
    rendered = _TRIM_RE.sub("", rendered)
    return rendered, table, rep


def module_is_blank(rendered: str) -> bool:
    """渲染后只剩空白 = 这个模块在本版里不产出任何字（SillyTavern 也会丢掉它）。"""
    return not rendered.strip()


__all__: Iterable[str] = ("MacroReport", "render", "module_is_blank")
