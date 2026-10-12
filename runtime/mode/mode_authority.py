# -*- coding: utf-8 -*-
"""唯一模式权威（Mode Authority）。

为什么必须有这一层，以及它凭什么算「权威」，写在 ``runtime/mode/__init__.py``
的顶层说明里。这里只讲实现上的三条：

  * **状态文件原子写**：先写同目录临时文件再 ``os.replace``。Windows 上直接覆写
    会被杀软/索引服务抢到半截文件，读到半截 JSON 等于模式凭空变成别的。
  * **配置按 mtime 重读**：改 ``mode_config.json`` 不用重启任何服务，和
    ``rules.py`` / ``human_interaction.config`` 同一套口径。
  * **一切解析失败都退回 ``ROLEPLAY``**：它是真人聊天那一档，也是唯一一个
    「少给规则也不会说错话」的档。宁可退化，不许发矛盾规则。

对外没有类，只有几个纯函数。调用方拿不到可写句柄，改模式只能走 ``set_mode``，
这样「谁改的」永远有迹可循（``updated_by`` 落在状态文件里）。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "mode_config.json"
STATE_PATH = HERE / "mode_state.json"
PROMPT_DIR = HERE / "prompts"

SCHEMA = "ai-girlfriend-mode/1"

ROLEPLAY = "ROLEPLAY"
READING = "READING"
THIRD_MODE = "THIRD"
MODES = (ROLEPLAY, READING, THIRD_MODE)
DEFAULT_MODE = ROLEPLAY

# 状态文件整个不见/读坏时用的兜底。这一档必须是「少规则也不会说错话」的那一档。
_SAFE_STATE: dict[str, Any] = {"schema": SCHEMA, "mode": DEFAULT_MODE,
                               "updated_at": "", "updated_by": "fallback", "revision": 0}

# 配置整个不见时的兜底：两个已实现的模式都关掉 Ember（Ember 正文要求第三人称叙事，
# 与两个模式都冲突），第三模式不实现。
_SAFE_CONFIG: dict[str, Any] = {
    "schema": SCHEMA,
    "modes": {
        ROLEPLAY: {"label": "角色扮演", "implemented": True, "order": 1,
                   "sola_v2_ember": False, "human_interaction": True,
                   "include_reading_context": False, "prompt_file": "roleplay_output_format.txt"},
        READING: {"label": "故事朗读", "implemented": True, "order": 2,
                  "sola_v2_ember": False, "human_interaction": False,
                  "include_reading_context": True, "prompt_file": "reading_answer_mode.txt"},
        THIRD_MODE: {"label": "第三个模式", "implemented": False, "order": 3,
                     "sola_v2_ember": False, "human_interaction": False,
                     "include_reading_context": False, "prompt_file": ""},
    },
}

_LOCK = threading.RLock()
_STATE_CACHE: dict[str, Any] = {"mtime": None, "raw": None}
_CONFIG_CACHE: dict[str, Any] = {"mtime": None, "raw": None}


# --------------------------------------------------------------------------- 读

def _read_json(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _cached(cache: dict[str, Any], path: Path) -> dict[str, Any]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    with _LOCK:
        if cache["mtime"] != mtime or cache["raw"] is None:
            cache["mtime"] = mtime
            cache["raw"] = _read_json(path)
    return dict(cache["raw"] or {})


def config(*, refresh: bool = False) -> dict[str, Any]:
    """模式配置。mtime 变了就重读，改配置不用重启服务。"""
    if refresh:
        with _LOCK:
            _CONFIG_CACHE["mtime"] = None
    raw = _cached(_CONFIG_CACHE, CONFIG_PATH)
    modes = raw.get("modes")
    if not isinstance(modes, dict) or not modes:
        return dict(_SAFE_CONFIG)
    merged = dict(_SAFE_CONFIG)
    merged.update({k: v for k, v in raw.items() if k != "modes"})
    table = dict(_SAFE_CONFIG["modes"])
    for name, row in modes.items():
        if isinstance(row, dict):
            base = dict(table.get(name) or {})
            base.update(row)
            table[name] = base
    merged["modes"] = table
    return merged


def state() -> dict[str, Any]:
    """权威模式状态。这是全系统唯一的事实源。"""
    raw = _cached(_STATE_CACHE, STATE_PATH)
    mode = str(raw.get("mode") or "").strip().upper()
    if mode not in MODES:
        return dict(_SAFE_STATE)
    out = dict(_SAFE_STATE)
    out.update(raw)
    out["mode"] = mode
    out["schema"] = SCHEMA
    return out


def current() -> str:
    """当前模式 ID。永远返回三个合法值之一。"""
    return str(state().get("mode") or DEFAULT_MODE).upper()


def describe(mode: str | None = None) -> dict[str, Any]:
    """一个模式对外可见的描述（给 UI 用，不暴露内部路径）。"""
    name = str(mode or current()).strip().upper()
    if name not in MODES:
        name = DEFAULT_MODE
    row = dict(config()["modes"].get(name) or {})
    implemented = bool(row.get("implemented"))
    label = str(row.get("label") or name)
    hint = "" if implemented else "待开发"
    return {"id": name, "label": label, "implemented": implemented,
            "hint": hint, "selectable": implemented,
            "order": int(row.get("order") or 99),
            "active": name == current()}


def available() -> list[dict[str, Any]]:
    """UI 那一行模式按钮。第三模式会带 selectable:false，前端据此 disabled。"""
    rows = [describe(name) for name in MODES]
    rows.sort(key=lambda one: one["order"])
    return rows


def flags(mode: str | None = None) -> dict[str, Any]:
    """一个模式的全部开关。缺的键按「关」处理，绝不按「开」。"""
    name = str(mode or current()).strip().upper()
    row = config()["modes"].get(name)
    if not isinstance(row, dict):
        return {"sola_v2_ember": False, "human_interaction": False,
                "include_reading_context": False, "implemented": False}
    return {
        "sola_v2_ember": bool(row.get("sola_v2_ember")),
        "human_interaction": bool(row.get("human_interaction")),
        "include_reading_context": bool(row.get("include_reading_context")),
        "implemented": bool(row.get("implemented")),
        "prompt_file": str(row.get("prompt_file") or ""),
    }


# --------------------------------------------------------------------------- 块开关

def sola_v2_ember_enabled(mode: str | None = None) -> bool:
    """Sola V2 Ember 叙事块在这个模式里要不要加载。

    ROLEPLAY / READING 都要求「人物本人直接说话」或「忠实朗读」，而 Ember 正文
    明确要求第三人称叙事散文——两者根本冲突，所以两个已实现的模式都是 False。
    """
    return bool(flags(mode).get("sola_v2_ember"))


def human_interaction_enabled(mode: str | None = None) -> bool:
    """真人互动层在这个模式里要不要加载。

    这一层自带的那句通道事实（「她这一轮的话是要被人念出来的：别写括号里的动作、
    神情、旁白」）正是 ROLEPLAY 需要的输出约束，所以 ROLEPLAY 为 True。
    """
    return bool(flags(mode).get("human_interaction"))


def include_reading_context(mode: str | None = None) -> bool:
    """阅读上下文（正在朗读的附近原文）要不要进 Prompt。只有 READING 需要。"""
    return bool(flags(mode).get("include_reading_context"))


def mode_prompt_text(mode: str | None = None) -> str:
    """这个模式自己的那段输出契约文案。没有就返回空串（空串不参与拼接）。"""
    name = str(mode or current()).strip().upper()
    file_name = str(flags(name).get("prompt_file") or "")
    if not file_name:
        return ""
    path = PROMPT_DIR / file_name
    try:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


# --------------------------------------------------------------------------- 写

def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".mode_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_mode(mode: str, *, updated_by: str = "ui") -> dict[str, Any]:
    """切换模式。这是全系统唯一能改模式的入口。

    返回 ``{ok:True, mode, previous, revision}`` 或 ``{ok:False, reason, 说明}``。
    第三模式会在这里被挡住——它 ``implemented:false``，按下它不允许进入半成品。
    """
    name = str(mode or "").strip().upper()
    if name not in MODES:
        return {"ok": False, "reason": "UNKNOWN_MODE",
                "说明": f"没有这个模式（{mode}），只有 角色扮演 / 故事朗读 / 第三个模式。",
                "mode": current()}
    row = flags(name)
    if not row.get("implemented"):
        return {"ok": False, "reason": "MODE_NOT_IMPLEMENTED",
                "说明": f"「{describe(name)['label']}」还没做，先放着，以后单独开任务。",
                "mode": current()}
    with _LOCK:
        previous_state = state()
        previous = str(previous_state.get("mode") or DEFAULT_MODE).upper()
        revision = int(previous_state.get("revision") or 0) + 1
        payload = {
            "schema": SCHEMA,
            "mode": name,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "updated_by": str(updated_by or "ui")[:32],
            "previous": previous,
            "revision": revision,
        }
        _atomic_write(STATE_PATH, payload)
        _STATE_CACHE["mtime"] = None
        _STATE_CACHE["raw"] = None
    return {"ok": True, "mode": name, "previous": previous, "revision": revision,
            "说明": f"现在在「{describe(name)['label']}」。"}


__all__ = [
    "MODES", "DEFAULT_MODE", "THIRD_MODE", "ROLEPLAY", "READING",
    "CONFIG_PATH", "STATE_PATH", "PROMPT_DIR",
    "config", "state", "current", "describe", "available", "flags",
    "sola_v2_ember_enabled", "human_interaction_enabled", "include_reading_context",
    "mode_prompt_text", "set_mode",
]
