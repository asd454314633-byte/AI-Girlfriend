# -*- coding: utf-8 -*-
"""把装配引擎现场递来的八项输入归一化成本层能用的形状。

只读消费：这里绝不改写传进来的对象，也不写任何状态。所有字段在构造时复制成
不可变更的形状，所以「本层会不会改坏上游内容」这件事可以直接从代码结构上排除。

八项输入与任务 §5 一一对应：

    当前模式            -> InteractionInput.mode
    人物设定            -> persona_text（档案那几句 + 用户自己写的台词）
    当前临时人物设定     -> temporary_text
    当前场景            -> scene_text（这一场的近况摘要）
    双方关系信息         -> relationship_text
    相关记忆            -> memory_lines
    最近聊天            -> history
    用户本轮输入         -> user_text
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import config


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return str(value).strip()


def _rows(history: Any) -> tuple[tuple[str, str], ...]:
    """历史统一成 (谁, 话) 的元组。认得 role/谁/kind/text/content 几种写法。"""
    out: list[tuple[str, str]] = []
    for item in (history or ()):
        if isinstance(item, Mapping):
            who = _text(item.get("role") or item.get("谁") or "user").lower()
            body = _text(item.get("text") or item.get("content") or item.get("话") or "")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            who, body = _text(item[0]).lower(), _text(item[1])
        else:
            who, body = "user", _text(item)
        if not body:
            continue
        if who not in ("user", "assistant", "system"):
            who = "assistant" if who in ("girlfriend", "her", "ai") else "user"
        out.append((who, body))
    return tuple(out)


def _lines(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(one.strip() for one in value.splitlines() if one.strip())
    if isinstance(value, (list, tuple)):
        out = []
        for one in value:
            if isinstance(one, Mapping):
                out.append(_text(one.get("text") or one.get("content") or ""))
            else:
                out.append(_text(one))
        return tuple(one for one in out if one)
    return ()


@dataclass(frozen=True)
class InteractionInput:
    """本层唯一认得的那一份输入。构造出来就不再变。"""
    mode: str = config.MODE_OFF
    character_id: str = ""
    session_id: str = ""
    persona_text: str = ""
    temporary_text: str = ""
    scene_text: str = ""
    relationship_text: str = ""
    memory_lines: tuple[str, ...] = ()
    history: tuple[tuple[str, str], ...] = ()
    user_text: str = ""
    # 「用户明确点了这一档」的凭据：本轮由配置显式选中即算，界面上线后由界面传
    mode_explicitly_chosen: bool = False

    # --- 只读视图：检测层要的这些切片，都从上面这几项里算，不另外取数据源 ---

    @property
    def assistant_turns(self) -> tuple[str, ...]:
        return tuple(body for who, body in self.history if who == "assistant")

    @property
    def user_turns(self) -> tuple[str, ...]:
        return tuple(body for who, body in self.history if who == "user")

    @property
    def recent_assistant(self) -> tuple[str, ...]:
        look = config.signal_int("recent_turns_scanned", 6)
        body = self.assistant_turns
        return body[-look:] if look > 0 else ()

    @property
    def has_conversation(self) -> bool:
        return bool(self.history)

    @property
    def is_first_turn(self) -> bool:
        return not self.history and not self.user_text

    def material_for(self, *names: str) -> str:
        """按需取那几项材料，拼成一小段供文案落位用。"""
        bag = {"persona": self.persona_text, "temporary": self.temporary_text,
               "scene": self.scene_text, "relationship": self.relationship_text}
        return "\n".join(_text(bag.get(name)) for name in names if _text(bag.get(name)))

    def memory_excerpt(self, limit: int = 3) -> tuple[str, ...]:
        return tuple(self.memory_lines[:max(0, int(limit))])

    def snapshot(self) -> dict[str, Any]:
        """给测试台看的，只含长度与来源，不把用户原话抄进报告。"""
        return {"mode": self.mode, "character_id": self.character_id,
                "persona_chars": len(self.persona_text),
                "temporary_chars": len(self.temporary_text),
                "scene_chars": len(self.scene_text),
                "relationship_chars": len(self.relationship_text),
                "memory_lines": len(self.memory_lines),
                "history_rows": len(self.history),
                "assistant_rows": len(self.assistant_turns),
                "user_chars": len(self.user_text)}


def build(*, mode: str | None = None, character_id: str = "", session_id: str = "",
          persona_text: Any = None, temporary_text: Any = None, scene_text: Any = None,
          relationship_text: Any = None, memory_lines: Any = None,
          history: Any = None, user_text: Any = None,
          mode_explicitly_chosen: bool | None = None) -> InteractionInput:
    """把散乱形状的入参收成一个 InteractionInput。任何一项缺了都不会报错。"""
    chosen = _text(mode) or config.mode()
    explicit = (config.mode() == chosen) if mode_explicitly_chosen is None \
        else bool(mode_explicitly_chosen)
    return InteractionInput(
        mode=chosen,
        character_id=_text(character_id),
        session_id=_text(session_id),
        persona_text=_text(persona_text),
        temporary_text=_text(temporary_text),
        scene_text=_text(scene_text),
        relationship_text=_text(relationship_text),
        memory_lines=_lines(memory_lines),
        history=_rows(history),
        user_text=_text(user_text),
        mode_explicitly_chosen=explicit,
    )


__all__ = ["InteractionInput", "build"]
