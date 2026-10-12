# -*- coding: utf-8 -*-
"""临时状态只读适配层。

今天这些状态存在 runtime/llm/role_state.py 里（挂在会话对象上的那份「这一场临时
换的身份 / 语气档位」）。本模块只做一件事：把它**读**出来，变成人物上下文能用的
一段中文。

三条界线：
  1. 只读：不改、不写、不持久化，也不碰 role_state.py 那个文件本身；
  2. 只暴露动态临时状态：任何长期身份、设定、记忆都不从这里走；
  3. 读不到原模块时退到内置的同构实现，并且明说自己在退路（source 字段），
     绝不假装自己读到了真的那份状态。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
ROLE_STATE_FILE = PROJECT_ROOT / "runtime" / "llm" / "role_state.py"

# 数值档位与取值顺序，与原模块保持一致；读不到原模块时用这份兜底
VALUE_KEYS = ("affection", "flirt", "intimacy", "initiative", "assertiveness",
              "playfulness", "shyness", "emotionality")
_LABELS = {
    "affection": "在意你的程度", "flirt": "撩的分寸", "intimacy": "亲近的分寸",
    "initiative": "谁先开口", "assertiveness": "主见", "playfulness": "玩心",
    "shyness": "害羞", "emotionality": "情绪上得快不快",
}
_DEFAULTS: dict[str, Any] = {
    "active": False, "role": "", "role_directive": "", "relationship_mode": "",
    "affection": 50, "flirt": 0, "intimacy": 0, "initiative": 50,
    "assertiveness": 50, "playfulness": 50, "shyness": 50, "emotionality": 50,
    "temporary": True, "minor_role": False, "voice_style": {"emotion": "", "intensity": 0.0},
}

_SOURCE = None
_LOAD_ERROR = ""


def _load_source() -> Any:
    """按文件路径只读地把原模块装进来（它只依赖标准库，加载没有副作用）。"""
    global _SOURCE, _LOAD_ERROR
    if _SOURCE is not None or _LOAD_ERROR:
        return _SOURCE
    try:
        if not ROLE_STATE_FILE.exists():
            raise FileNotFoundError(str(ROLE_STATE_FILE))
        spec = importlib.util.spec_from_file_location("_zero_role_state_readonly",
                                                     ROLE_STATE_FILE)
        if spec is None or spec.loader is None:
            raise ImportError("SPEC_UNAVAILABLE")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        for name in ("default_state", "current_state"):
            if not callable(getattr(module, name, None)):
                raise ImportError(f"MISSING:{name}")
        _SOURCE = module
    except Exception as exc:  # 读不到就用内置同构实现，并且把原因留着
        _SOURCE = None
        _LOAD_ERROR = f"{type(exc).__name__}:{exc}"
    return _SOURCE


def source() -> dict[str, Any]:
    """这份状态到底从哪来的。退路模式必须能被看见，不许悄悄假装有状态。"""
    module = _load_source()
    if module is not None:
        return {"mode": "原模块", "位置": str(ROLE_STATE_FILE)}
    return {"mode": "内置只读实现", "原因": _LOAD_ERROR or "还没试着加载"}


def default_state() -> dict[str, Any]:
    module = _load_source()
    if module is not None:
        return dict(module.default_state())
    return dict(_DEFAULTS)


def state(chat: Any) -> dict[str, Any]:
    """读一个会话对象上当前的临时状态；对象没有状态时给默认值。"""
    module = _load_source()
    if module is not None:
        try:
            return dict(module.current_state(chat))
        except Exception:
            return default_state()
    stored = getattr(chat, "_role_state", None)
    if isinstance(stored, dict) and getattr(chat, "_role_state_generation", None) == \
            getattr(chat, "_gen_counter", 0):
        return dict(stored)
    return default_state()


def is_control(text: str) -> bool:
    """这句话是不是在临时改身份/语气（只判断，不应用）。"""
    module = _load_source()
    checker = getattr(module, "is_role_control", None) if module is not None else None
    if callable(checker):
        try:
            return bool(checker(text))
        except Exception:
            return False
    return False


def is_active(chat_or_state: Any) -> bool:
    value = chat_or_state if isinstance(chat_or_state, Mapping) else state(chat_or_state)
    return bool(value.get("active"))


def levels(chat_or_state: Any) -> dict[str, int]:
    value = chat_or_state if isinstance(chat_or_state, Mapping) else state(chat_or_state)
    return {key: _clamp(value.get(key)) for key in VALUE_KEYS}


def _clamp(value: Any) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


def _phrase(key: str, value: int) -> str:
    label = _LABELS[key]
    if value >= 75:
        degree = "很高"
    elif value >= 60:
        degree = "偏高"
    elif value >= 40:
        degree = "中等"
    elif value >= 25:
        degree = "偏低"
    else:
        degree = "很低"
    return f"{label}{degree}"


def describe(chat_or_state: Any) -> str:
    """把临时状态翻成一句人话，放进上下文最后面。

    必须点明「这是这一场的，说了就改，不算她的长期身份，也不要把演过的事当成真实发生
    过的经历」——这条约束丢了，演完一次就再也回不来了。
    """
    value = chat_or_state if isinstance(chat_or_state, Mapping) else state(chat_or_state)
    if not value or not value.get("active"):
        return ""
    parts: list[str] = []
    role = str(value.get("role") or "").strip()
    directive = str(value.get("role_directive") or "").strip()
    if role:
        parts.append(f"这一场你临时以「{role}」的身份说话")
    if directive and directive != role:
        parts.append(f"用户给你的说法是：{directive}")
    words = [_phrase(key, _clamp(value.get(key))) for key in VALUE_KEYS]
    parts.append("当下的分寸是：" + "，".join(words))
    parts.append("这是这一场临时约定的，不算你的长期身份；用户随时可以改，"
                 "演过的情节不要当成真实发生过的事。")
    if value.get("minor_role"):
        parts.append("涉及未成年身份时，只保留普通相处的部分，不往亲密或成人的方向走。")
    return "\n".join(parts)


__all__ = ["source", "default_state", "state", "levels", "describe", "is_active",
           "is_control", "VALUE_KEYS"]
