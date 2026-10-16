# -*- coding: utf-8 -*-
"""配置装载：本层所有「行为差异」的唯一来源之一（另一半是 prompts 里的文案）。

为什么单独一个模块：任务 §11 要求所有差异来自「模式配置」，不允许长成
``if 某模型: 特殊修复A`` 那样。要能审计，就得先让所有可调的东西集中在一份文件里。

三条口径：
  * 环境变量 ``HUMAN_INTERACTION_ENABLED`` 优先于文件里的 ``feature_enabled``
     ——回滚时不用改文件就能整体旁路；
  * 文件 mtime 变了就重读（沿用 rules.py 的口径，改配置不用重启服务）；
  * 读不到、读坏了、值不认识，一律退回「关」——本层的失败绝不能影响旧链。
"""
from __future__ import annotations

import json
import os
import threading
from contextvars import ContextVar
from contextlib import contextmanager
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "human_interaction.json"
ENV_ENABLED = "HUMAN_INTERACTION_ENABLED"
# V2 那道单独的闸：总闸开着、但判断器没通过硬门槛时，本层行为必须逐字等于 V1。
ENV_TURN_JUDGE = "HUMAN_INTERACTION_TURN_JUDGE"

MODE_HUMAN_NATURAL = "human_natural"
MODE_AUTONOMOUS = "autonomous"
MODE_STORY_DIRECTOR = "story_director"
MODE_OFF = "off"
ALLOWED_MODES = (MODE_HUMAN_NATURAL, MODE_AUTONOMOUS, MODE_STORY_DIRECTOR, MODE_OFF)

# 兜底用的最小默认：配置文件整个不见了也走这份，而这份的第一条就是「关」。
_SAFE_DEFAULTS: dict[str, Any] = {
    "schema": "human-interaction/1",
    "feature_enabled": False,
    "interaction_mode": MODE_HUMAN_NATURAL,
    "allowed_modes": list(ALLOWED_MODES),
    "story_director_requires_user_choice": True,
    "instruction_max_chars": 900,
    "revise_enabled": False,
    "output_channel_note": "",
    # V2「本轮互动判断」。这一组默认值的第一条是「关」：判断器要多花一次模型调用，
    # 没被实测证明随场景变化之前，它不许在任何人不知情的情况下发生。
    "turn_judge_enabled": False,
    # 超时给到 5 秒：关掉思考之后实测 0.72~0.93 秒，留五倍余量；再长就是在等一个
    # 已经不发生的思考过程。上限由 turn_judge_max_tokens 兜，不靠超时兜。
    "turn_judge_timeout_s": 5,
    "turn_judge_max_tokens": 200,
    "turn_judge_history_turns": 8,
    "turn_judge_memory_lines": 0,
    "turn_judge_persona_max_chars": 700,
    # V2b：判断结果压成一行。这两条是"注入成本"的封顶值，不是判断能力的口径——
    # 让正式回复变贵的是"多挂了一段结构文本"（V2 门槛报告 §4：挂一段读错了的同样长
    # 的判断，一样贵），所以段的字节数要能被一个数管住。值 8 字对应简报里"2~4 个字"
    # 的两倍余量；块 120 字对应紧凑一行实测中位的两倍余量（老形状下中位是 90+ 字）。
    "turn_judge_value_max_chars": 8,
    "turn_judge_block_max_chars": 120,
    # 这份清单的唯一作用是「常用词不算重复」，不是禁词表；放配置里是为了让人体检测
    # 只有一个读取口径（见 humanizer.repeated_phrases）。
    "signals": {"ignored_phrases": ["什么", "这个", "那个", "自己", "现在", "一下", "有点",
                                    "不是", "我们", "你们", "怎么", "时候", "一个", "没有",
                                    "就是", "还是"]},
    "cliche_templates": [],
    "summary_tone_openers": [],
}

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"mtime": None, "raw": None, "origin": ""}

# MODE_SYSTEM_V1：本次装配所属的模式。由 context_builder 在调用本层之前绑定，
# 只影响紧接着的那一次装配；None = 非模式化调用，完全退回原有口径。
_MODE_BINDING = ContextVar("interaction_mode_binding", default=None)
_REQUEST_JUDGE = ContextVar("interaction_request_judge", default=None)

@contextmanager
def request_judge_scope():
    """V3 opt-in scope; never changes global legacy flags or another request."""
    result = {}
    token = _REQUEST_JUDGE.set(result)
    try:
        yield result
    finally:
        _REQUEST_JUDGE.reset(token)

def record_judgment(value):
    result = _REQUEST_JUDGE.get()
    if result is not None:
        result["judgment"] = value



def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in ("1", "true", "yes", "on", "y")


def _read_file() -> dict[str, Any]:
    """读配置；读不到或读坏返回 None（调用侧退到安全默认）。"""
    try:
        if not CONFIG_PATH.exists():
            return {}
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _snapshot() -> dict[str, Any]:
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        mtime = None
    with _LOCK:
        if _CACHE["mtime"] != mtime or _CACHE["raw"] is None:
            _CACHE["mtime"] = mtime
            _CACHE["raw"] = _read_file()
            _CACHE["origin"] = str(CONFIG_PATH) if mtime is not None else "MISSING"
        raw = _CACHE["raw"] or {}
    merged = dict(_SAFE_DEFAULTS)
    merged.update(raw)
    return merged


def config(*, refresh: bool = False) -> dict[str, Any]:
    if refresh:
        with _LOCK:
            _CACHE["mtime"] = None
    return _snapshot()


def bind_mode(mode: Any) -> None:
    """MODE_SYSTEM_V1：把「这次装配属于哪个模式」告诉本层，只在这一次装配里有效。

    为什么不是一个普通入参：本层的开关散在 ``enabled()`` / ``mode()`` /
    ``turn_judge_enabled()`` 这几处，装配引擎是通过它们现场读的，把它们全改一遍签名
    等于把整层重写一遍——那是这一轮明令禁止的（§23「不要重新发明真人自然层」）。
    所以留一个**装配入口处绑定、本次调用内有效**的槽位，改动只有这一处。

    ``None`` = 没有模式信息（测试、预览等非模式化调用）→ 退回文件里那把总闸的原值，
    与接入模式体系之前逐字相同。
    """
    text = str(mode or "").strip().upper()
    _MODE_BINDING.set(text or None)


def _mode_allows() -> bool | None:
    """模式对本层的裁决。``None`` = 模式层不在场/没绑定，别插手。"""
    if _MODE_BINDING.get() is None:
        return None
    try:
        from runtime.mode import mode_authority
        return bool(mode_authority.human_interaction_enabled(_MODE_BINDING.get()))
    except Exception:
        return None


def enabled() -> bool:
    """总闸。优先级从高到低：环境变量 > 当前模式 > 文件里那把。

    环境变量仍然最高：回滚不该依赖改文件，这条口径不能因为加了模式就失效。
    模式这一档是 MODE_SYSTEM_V1 加的：本层现在归模式管，ROLEPLAY 开、READING 关，
    不再由文件里那把全局开关一句话定死全场。
    """
    override = os.environ.get(ENV_ENABLED)
    if override is not None and str(override).strip() != "":
        return _truthy(override)
    allowed = _mode_allows()
    if _REQUEST_JUDGE.get() is not None:
        return allowed is not False
    if allowed is not None:
        return allowed
    return _truthy(config().get("feature_enabled"))


def mode() -> str:
    """当前模式；值不认识一律当「关」。"""
    settings = config()
    raw = str(settings.get("interaction_mode") or "").strip()
    allowed = [str(one) for one in (settings.get("allowed_modes") or ALLOWED_MODES)]
    if raw not in ALLOWED_MODES or raw not in allowed:
        return MODE_OFF
    return raw


def turn_judge_enabled() -> bool:
    """V2 判断器单独的闸。总闸管「这一层存不存在」，这一句管「要不要多跑一次模型」。

    两层是串联的：``enabled()`` 关 → 判断器根本不会被走到；
    ``enabled()`` 开、这里关 → 本层行为逐字等于 V1（不发任何模型请求）。
    环境变量优先，理由与 ``enabled()`` 一样：回滚不该依赖改文件。
    """
    override = os.environ.get(ENV_TURN_JUDGE)
    if override is not None and str(override).strip() != "":
        return _truthy(override)
    if _REQUEST_JUDGE.get() is not None:
        return True
    return _truthy(config().get("turn_judge_enabled"))


def judge_int(name: str, default: int) -> int:
    try:
        return max(0, int(config().get(name)))
    except (TypeError, ValueError):
        return max(0, int(default))


def signal(name: str, default: Any) -> Any:
    row = config().get("signals") or {}
    value = row.get(name)
    return default if value is None else value


def signal_int(name: str, default: int) -> int:
    try:
        return max(0, int(signal(name, default)))
    except (TypeError, ValueError):
        return max(0, int(default))


def string_list(name: str) -> list[str]:
    value = config().get(name)
    if not isinstance(value, list):
        return []
    return [str(one).strip() for one in value if str(one or "").strip()]


def signal_list(name: str) -> list[str]:
    """signals 那一格里的字符串表。清单只在这一个地方读，代码里不留副本。

    生效的 signals 里**没有这个键**时，退回 `_SAFE_DEFAULTS` 里那份——
    配置文件整体读不到、或者某一份测试只覆盖了 signals 的几个键，都不该让这份
    清单凭空变成空的。要显式清空就在配置里写 ``"ignored_phrases": []``。
    """
    row = config().get("signals") or {}
    value = row.get(name)
    if value is None:
        value = (_SAFE_DEFAULTS.get("signals") or {}).get(name)
    if not isinstance(value, list):
        return []
    return [str(one).strip() for one in value if str(one or "").strip()]


def instruction_max_chars() -> int:
    try:
        return max(0, int(config().get("instruction_max_chars") or 0))
    except (TypeError, ValueError):
        return 0


def revise_enabled() -> bool:
    return _truthy(config().get("revise_enabled"))


def channel_note() -> str:
    """交付通道那一句话。三档共用，所以放在配置里而不是三份文案里——
    各写一遍迟早写成三个不一样。空串 = 这一句不出。"""
    return str(config().get("output_channel_note") or "").strip()


def story_director_requires_choice() -> bool:
    return bool(config().get("story_director_requires_user_choice", True))


def describe_config() -> dict[str, Any]:
    """给测试台和验收报告看的：本层现在的真实生效状态（含环境变量覆盖结果）。"""
    settings = config()
    return {
        "config_path": str(CONFIG_PATH),
        "config_file_present": CONFIG_PATH.exists(),
        "origin": _CACHE.get("origin", ""),
        "env_var": ENV_ENABLED,
        "env_value": os.environ.get(ENV_ENABLED),
        "feature_enabled_in_file": settings.get("feature_enabled"),
        "effective_enabled": enabled(),
        "effective_mode": mode(),
        "revise_enabled": revise_enabled(),
        "instruction_max_chars": instruction_max_chars(),
        "turn_judge_enabled": turn_judge_enabled(),
        "turn_judge_env_value": os.environ.get(ENV_TURN_JUDGE),
        "turn_judge_timeout_s": judge_int("turn_judge_timeout_s", 5),
        "turn_judge_max_tokens": judge_int("turn_judge_max_tokens", 200),
    }


__all__ = ["CONFIG_PATH", "ENV_ENABLED", "ENV_TURN_JUDGE", "ALLOWED_MODES",
           "MODE_HUMAN_NATURAL", "MODE_AUTONOMOUS", "MODE_STORY_DIRECTOR", "MODE_OFF",
           "config", "enabled", "mode", "signal", "signal_int", "signal_list",
           "string_list", "instruction_max_chars", "revise_enabled", "channel_note",
           "story_director_requires_choice", "describe_config", "turn_judge_enabled",
           "judge_int"]