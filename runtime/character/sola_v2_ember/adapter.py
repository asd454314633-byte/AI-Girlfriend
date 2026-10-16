# -*- coding: utf-8 -*-
"""Sola V2 Ember 的唯一对外接口：读开关 → 取当前人物称呼 → 给装配引擎一块文本。

三件事说清楚：

  * **开关是现场读的**（mtime 变了就重读，沿用 rules.py / human_interaction.config.py 的
    口径），所以关掉它不用重启服务、更不用删文件；环境变量 ``SOLA_V2_EMBER_ENABLED``
    优先于文件，留一条「先别改磁盘」的应急退路。第二把开关 ``voice_minimal_enabled``
    （应急环境变量 ``SOLA_V2_EMBER_VOICE_MINIMAL``）只管「要不要做最小清洗」：
    开着 = 交出去的是删掉 Sola 自我身份与星号动作格式那两类之后的版本，
    拨回 ``false`` = 交出去的是作者逐字原版 —— 两条路都不需要删文件、不需要重启；
  * **任何一步失败都退回「没有这一块」**：冻结件坏了、SHA 对不上、人物档案读不到、
    渲染抛错、清洗清单与冻结件对不上，一律产出空串并留下原因。这一层挂了不能把装配带崩，
    也不能把一份来路不明的文本当成作者的规则发出去；
  * **渲染结果按 (配置 mtime, 原始件 mtime, 清单 mtime, 清洗档, 称呼) 缓存**：每轮重新解析
    33 万字的 JSON 是没必要的开销，而缓存键里带了这些 mtime，改任何一项都会自动失效。

本模块不认识模型、不认识 TTS、不联网，也不写盘。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from . import ember, voice_minimal

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "sola_v2_ember.json"
ENV_ENABLED = "SOLA_V2_EMBER_ENABLED"
# 第二把开关：只切「最小清洗」这一档，不动上面那把总开关。
ENV_VOICE_MINIMAL = "SOLA_V2_EMBER_VOICE_MINIMAL"

# 配置文件整个不见了也走这份，而这份的第一条就是「关」——和真人互动层同一个口径。
_SAFE_DEFAULTS: dict[str, Any] = {
    "schema": "sola-v2-ember/1",
    "sola_v2_ember_enabled": False,
    "user_label": "",
    # 清洗档默认开着：总开关都已经被人拨开了，就没有把「模型自称 Sola」发出去的理由。
    "voice_minimal_enabled": True,
    # 称呼只从人物档案取；取不到就留空，空称呼会让整块旁路（见 system_text），
    # 而不是拿一个我们编的名字顶上 —— 那正是本轮 §A 要失效掉的东西。
    "char_label_fallback": "",
}

_LOCK = threading.Lock()
_CFG_CACHE: dict[str, Any] = {"mtime": None, "raw": None, "origin": ""}
_RENDER_CACHE: dict[str, Any] = {"key": None, "text": "", "reason": "", "mode": ""}


def _truthy(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n"):
        return False
    return default


def _read_file() -> dict[str, Any]:
    try:
        stat = CONFIG_PATH.stat()
    except OSError:
        return dict(_SAFE_DEFAULTS)
    with _LOCK:
        if _CFG_CACHE["mtime"] == stat.st_mtime_ns:
            return dict(_CFG_CACHE["raw"] or {})
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config not a mapping")
    except Exception:
        raw = dict(_SAFE_DEFAULTS)
        origin = "安全默认（配置文件读坏了或不是字典）"
    else:
        origin = "配置文件"
    merged = dict(_SAFE_DEFAULTS)
    merged.update(raw)
    with _LOCK:
        _CFG_CACHE.update({"mtime": stat.st_mtime_ns, "raw": merged, "origin": origin})
    return dict(merged)


def config() -> dict[str, Any]:
    return _read_file()


def enabled() -> bool:
    """这一层到底开没开 —— 环境变量优先，读不到就退回「关」。"""
    env = os.environ.get(ENV_ENABLED)
    if env is not None and str(env).strip() != "":
        return _truthy(env.strip(), default=False)
    row = config()
    return _truthy(row.get("sola_v2_ember_enabled"), default=False)


def voice_minimal_enabled() -> bool:
    """要不要做「最小清洗」（只删 Sola 自我身份 + 星号动作格式那两类）。

    总开关没开时这一把没有意义（整块都不进 system）。环境变量优先，跟总开关同一口径，
    留一条「不改磁盘就临时拿回逐字原版」的退路。
    """
    env = os.environ.get(ENV_VOICE_MINIMAL)
    if env is not None and str(env).strip() != "":
        return _truthy(env.strip(), default=True)
    return _truthy(config().get("voice_minimal_enabled"), default=True)


def labels_for(character_id: str | None = None, *, db_path: Any = None) -> dict[str, str]:
    """当前人物 / 当前对话对象的称呼：{{char}} 与 {{user}} 的落地值。"""
    row = config()
    user_label = str(row.get("user_label") or "")
    char_label = ""
    if character_id:
        try:
            from runtime.character import store
            profile = store.get_profile(character_id, db_path=db_path)
            if profile is not None:
                char_label = (profile.display_name or profile.nickname or "").strip()
        except Exception:
            char_label = ""
    if not char_label:
        char_label = str(row.get("char_label_fallback") or "")
    return {"char": char_label, "user": user_label}


def _source_mtime() -> int:
    try:
        return ember.SOURCE_PATH.stat().st_mtime_ns
    except OSError:
        return -1


def _spec_mtime() -> int:
    """清洗清单落了盘就进缓存键 —— 改清单下一次装配就会重算，不用重启。"""
    return voice_minimal.spec_mtime()


def _config_mtime() -> int:
    try:
        return CONFIG_PATH.stat().st_mtime_ns
    except OSError:
        return -1


def system_text(character_id: str | None = None, *, db_path: Any = None) -> str:
    """给装配引擎的那一块文本；关掉 / 坏了 = 空串（空串就不会进 system）。"""
    if not enabled():
        _RENDER_CACHE.update({"key": None, "text": "", "reason": "disabled", "mode": ""})
        return ""
    picked = labels_for(character_id, db_path=db_path)
    if not picked["char"]:
        # 称呼取不到 = 这一层整块旁路。宁可少一层规则，也不拿一个编出来的名字当她是谁。
        _RENDER_CACHE.update({"key": None, "text": "", "reason": "no_char_label", "mode": ""})
        return ""
    cleaned = voice_minimal_enabled()
    key = (_config_mtime(), _source_mtime(), _spec_mtime() if cleaned else 0,
           cleaned, picked["char"], picked["user"])
    if _RENDER_CACHE.get("key") == key:
        return str(_RENDER_CACHE.get("text") or "")
    try:
        if cleaned:
            rendered = voice_minimal.render(char_label=picked["char"],
                                            user_label=picked["user"])
            text, mode, reason = rendered.text.strip(), "voice_minimal", ""
        else:
            rendered = ember.render(char_label=picked["char"], user_label=picked["user"])
            text, mode, reason = rendered.text.strip(), "raw_ember", ""
    except voice_minimal.VoiceMinimalSpecError:
        # 清单与冻结件对不上：这是「删不干净」的处境，宁可不给这一层。
        text, mode, reason = "", "spec_mismatch", "VOICE_MINIMAL_SPEC_MISMATCH"
    except Exception:
        # 冻结件损坏 / SHA 对不上 / 宏解析抛错：这一层整块旁路，绝不带崩装配。
        text, mode, reason = "", "error", "ERROR"
    _RENDER_CACHE.update({"key": key, "text": text, "reason": reason, "mode": mode})
    return text


def render_state() -> dict[str, Any]:
    """缓存里那一次渲染的口径（走没走清洗、为什么旁路），只读。"""
    return {"mode": str(_RENDER_CACHE.get("mode") or ""),
            "reason": str(_RENDER_CACHE.get("reason") or "")}


def describe(character_id: str | None = None, *, db_path: Any = None) -> dict[str, Any]:
    """给验收/自检用的一次性快照（只读，不改任何状态）。"""
    row = config()
    env = os.environ.get(ENV_ENABLED)
    env_min = os.environ.get(ENV_VOICE_MINIMAL)
    picked = labels_for(character_id, db_path=db_path)
    out: dict[str, Any] = {
        "sola_v2_ember_enabled": enabled(),
        "voice_minimal_enabled": voice_minimal_enabled(),
        "开关来源": ("环境变量 " + ENV_ENABLED) if (env or "").strip() else "配置文件",
        "清洗开关来源": ("环境变量 " + ENV_VOICE_MINIMAL) if (env_min or "").strip() else "配置文件",
        "配置文件": str(CONFIG_PATH),
        "user_label": picked["user"],
        "char_label": picked["char"],
    }
    if not out["sola_v2_ember_enabled"]:
        out["块字数"] = 0
        return out
    try:
        rendered = (voice_minimal.render(char_label=picked["char"], user_label=picked["user"])
                    if out["voice_minimal_enabled"]
                    else ember.render(char_label=picked["char"], user_label=picked["user"]))
    except Exception as exc:
        out["渲染失败"] = type(exc).__name__ + ": " + str(exc)[:120]
        out["块字数"] = 0
        return out
    out["块字数"] = len(rendered.text)
    out.update(rendered.facts)
    out["未搬进来的 ST 专用件"] = rendered.skipped
    if out["voice_minimal_enabled"]:
        out["清洗前的原版块字数"] = out.get("voice_minimal_raw_chars")
        out["本轮删掉的六段原文"] = [one.as_dict() for one in getattr(rendered, "applied", [])]
    return out


__all__ = ["CONFIG_PATH", "ENV_ENABLED", "ENV_VOICE_MINIMAL", "config", "describe",
           "enabled", "labels_for", "render_state", "system_text", "voice_minimal_enabled"]
