# -*- coding: utf-8 -*-
"""模式注册表：只有元数据，没有行为分支。

这是任务 §11 的落点。整个文件里没有任何 ``if mode == ...: 做件特殊事``——
模式之间的差别只有两处：读哪个文案文件、以及这份文案允许占多少地方。
新增第四种模式 = 加一行元数据 + 加一个 txt，不改主逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config

HERE = Path(__file__).resolve().parent
PROMPT_DIR = HERE / "prompts"


@dataclass(frozen=True)
class ModeSpec:
    """一个模式的元数据。"""
    key: str
    label_zh: str
    prompt_file: str
    # 这一档是否需要在配置里被显式选中才允许生效（剧情推进默认关）
    requires_explicit_choice: bool = False
    # 这块文案自己声明的上限；0 表示只受全局 instruction_max_chars 管
    max_chars: int = 0
    # 检测信号在这一档是否参与（关掉就不是「不同模式不同逻辑」，而是「这条模式不要检测」）
    use_detected_signals: bool = True
    # V2：这一档的「本轮互动判断」简报。空串 = 这一档没有判断器，永远走 V1 那份行为。
    # 跟 prompt_file 一样只是元数据：新增第四种模式 = 加一行元数据 + 加一个 txt。
    judge_prompt_file: str = ""


MODES: dict[str, ModeSpec] = {
    config.MODE_HUMAN_NATURAL: ModeSpec(
        key=config.MODE_HUMAN_NATURAL, label_zh="真人自然",
        prompt_file="human_natural.txt", use_detected_signals=True,
        judge_prompt_file="turn_judge_human_natural.txt"),
    config.MODE_AUTONOMOUS: ModeSpec(
        key=config.MODE_AUTONOMOUS, label_zh="强角色自主",
        prompt_file="autonomous.txt", use_detected_signals=True),
    config.MODE_STORY_DIRECTOR: ModeSpec(
        key=config.MODE_STORY_DIRECTOR, label_zh="剧情推进",
        prompt_file="story_director.txt", requires_explicit_choice=True,
        use_detected_signals=True),
    config.MODE_OFF: ModeSpec(
        key=config.MODE_OFF, label_zh="关", prompt_file=""),
}

# 文案按 mtime 重读，改话术不用重启（与 rules.py 同一口径）
_TEXT_CACHE: dict[str, tuple[float | None, str]] = {}

# 不属于任何模式、但同样住在这个目录里的文案：第二遍修正用的表达目标。
# 单独列出来，是为了让「模式之外的文案」也只有一个读取口径。
EXTRA_TEXTS: dict[str, str] = {"humanizer": "humanizer.txt"}


def spec(mode: str) -> ModeSpec | None:
    return MODES.get(str(mode or "").strip())


def is_known(mode: str) -> bool:
    return spec(mode) is not None


def label(mode: str) -> str:
    row = spec(mode)
    return row.label_zh if row else ""


def _read_file(path: Path, cache_key: str) -> str:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _TEXT_CACHE[cache_key] = (None, "")
        return ""
    cached_mtime, cached_text = _TEXT_CACHE.get(cache_key, (None, None))
    if cached_mtime == mtime and cached_text is not None:
        return cached_text
    try:
        body = path.read_text(encoding="utf-8")
    except Exception:
        body = ""
    body = "\n".join(line.rstrip() for line in body.splitlines()).strip()
    _TEXT_CACHE[cache_key] = (mtime, body)
    return body


def prompt_text(mode: str) -> str:
    """读这一档的文案原文。文件不见了就返回空串（宁可不出话，也不要拍一句）。"""
    row = spec(mode)
    if row is None or not row.prompt_file:
        return ""
    return _read_file(PROMPT_DIR / row.prompt_file, f"mode:{row.key}")


def judge_prompt_text(mode: str) -> str:
    """这一档「本轮互动判断」的简报。没配文件名或文件不见了 → 空串 = 这一档没有判断器。"""
    row = spec(mode)
    if row is None or not row.judge_prompt_file:
        return ""
    return _read_file(PROMPT_DIR / row.judge_prompt_file, f"judge:{row.key}")


def extra_text(key: str) -> str:
    """模式之外的那几份文案（现在只有第二遍修正用的表达目标）。"""
    name = EXTRA_TEXTS.get(str(key or "").strip())
    if not name:
        return ""
    return _read_file(PROMPT_DIR / name, f"extra:{name}")


def allowed_now(mode: str) -> tuple[bool, str]:
    """这一档现在能不能生效。只查配置许可，不查话、不查人物。"""
    row = spec(mode)
    if row is None:
        return False, "MODE_UNKNOWN"
    if mode == config.MODE_OFF:
        return False, "MODE_OFF"
    if mode not in [str(one) for one in (config.config().get("allowed_modes")
                                         or config.ALLOWED_MODES)]:
        return False, "MODE_NOT_IN_ALLOWED_LIST"
    if row.requires_explicit_choice and mode != config.mode():
        return False, "MODE_NOT_EXPLICITLY_CHOSEN"
    return True, ""


def max_chars(mode: str) -> int:
    row = spec(mode)
    if row is None:
        return 0
    return int(row.max_chars or 0)


def uses_signals(mode: str) -> bool:
    row = spec(mode)
    return bool(row and row.use_detected_signals)


def registry_view() -> dict[str, Any]:
    """给测试台：证明「模式差异只有元数据」这一条是可检查的。"""
    return {key: {"label_zh": row.label_zh, "prompt_file": row.prompt_file,
                  "requires_explicit_choice": row.requires_explicit_choice,
                  "max_chars": row.max_chars,
                  "use_detected_signals": row.use_detected_signals,
                  "judge_prompt_file": row.judge_prompt_file}
            for key, row in MODES.items()}


__all__ = ["ModeSpec", "MODES", "PROMPT_DIR", "EXTRA_TEXTS", "spec", "is_known", "label",
           "prompt_text", "judge_prompt_text", "extra_text", "allowed_now", "max_chars",
           "uses_signals", "registry_view"]
