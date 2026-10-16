# -*- coding: utf-8 -*-
"""从冻结的官方原始件里取出 Ember 那一档，渲染成一个独立规则块。

一句话口径：**作者的规则文本一个字都不改**，这里只做两件事 ——
按 Ember 的 ``prompt_order`` 把「被启用的模块」按原顺序取出来，
再用 :mod:`macros` 把 SillyTavern 模板宏算成静态文本。

为什么必须按顺序共用一张变量表：作者的 ``━━📜 Core Foundations`` 那一格
把 34 个 ``mod_*`` 变量全部清空，后面的模块再各自把自己那一格填上；
Oath / Post-History 这些模块靠 ``{{getvar}}`` 回头取。分开渲染再拼会把这些取用
全变空串，等于把作者的规则删掉了几段。

只读取，不写盘，不联网。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import macros

HERE = Path(__file__).resolve().parent
SOURCE_PATH = HERE / "source" / "Sola V2 Ember.json"
MANIFEST_PATH = HERE / "SOURCE_MANIFEST.json"

# SillyTavern 预设里那些「不属于规则正文」的东西，本模块一律不接进生产。
# 分类口径写在这里，计数在运行时从原始件现算，不手写数字。
_ST_ANCHOR_IDENTIFIERS = frozenset({
    "worldInfoBefore", "worldInfoAfter", "scenario", "personaDescription",
    "charDescription", "charPersonality", "dialogueExamples", "nsfw",
    "chatHistory",
})
_HEADER_MARK = "━"


class SolaSourceIntegrityError(RuntimeError):
    """冻结件和 manifest 对不上：宁可不出这一层，也不能拿一份来路不明的文本当作者原版。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def manifest() -> dict[str, Any]:
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_source(*, verify: bool = True) -> dict[str, Any]:
    """读冻结原始件；默认顺手把 SHA256 和 manifest 对一次。"""
    if not SOURCE_PATH.is_file():
        raise SolaSourceIntegrityError("SOLA_SOURCE_MISSING")
    raw = SOURCE_PATH.read_bytes()
    row = manifest()
    want = str(((row.get("SHA256") or "")))
    if verify and want and _sha256(SOURCE_PATH) != want:
        raise SolaSourceIntegrityError("SOLA_SOURCE_SHA_MISMATCH")
    if verify and row.get("DOWNLOAD_SIZE_BYTES") is not None \
            and int(row["DOWNLOAD_SIZE_BYTES"]) != len(raw):
        raise SolaSourceIntegrityError("SOLA_SOURCE_SIZE_MISMATCH")
    return json.loads(raw.decode("utf-8"))


def enabled_rows(data: dict[str, Any]) -> list[dict[str, str]]:
    """按 Ember 的 prompt_order 原顺序，取 enabled=true 的模块。

    返回 ``[{identifier, name, role, content}, ...]`` —— content 是**作者原始字节**，
    未渲染宏，未做任何改写。
    """
    slot = (data.get("prompt_order") or [{}])[0]
    by_id = {p.get("identifier"): p for p in (data.get("prompts") or [])}
    out: list[dict[str, str]] = []
    for row in slot.get("order") or []:
        if not row.get("enabled"):
            continue
        prompt = by_id.get(row.get("identifier")) or {}
        out.append({
            "identifier": str(row.get("identifier") or ""),
            "name": str(prompt.get("name") or ""),
            "role": str(prompt.get("role") or ""),
            "marker": bool(prompt.get("marker")),
            "content": str(prompt.get("content") or ""),
        })
    return out


def disabled_count(data: dict[str, Any]) -> int:
    slot = (data.get("prompt_order") or [{}])[0]
    rows = slot.get("order") or []
    return sum(1 for r in rows if not r.get("enabled"))


@dataclass(slots=True)
class EmberRender:
    text: str = ""
    modules: list[dict[str, Any]] = field(default_factory=list)
    report: macros.MacroReport | None = None
    skipped: dict[str, Any] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)


def render(*, char_label: str = "", user_label: str = "",
           data: dict[str, Any] | None = None) -> EmberRender:
    """渲染 Ember 的单一规则块。

    ``char_label`` / ``user_label`` 是任务 §3 明确允许的格式适配
    （``{{char}}`` → 当前人物、``{{user}}`` → 当前用户）；除此之外产出的每个字
    都来自作者的模块正文。
    """
    payload = data if data is not None else load_source()
    rows = enabled_rows(payload)
    labels = {"char": char_label, "user": user_label,
              "persona": user_label, "actual_user": user_label}
    variables: dict[str, str] = {}
    report = macros.MacroReport()

    modules: list[dict[str, Any]] = []
    kept: list[str] = []
    for index, row in enumerate(rows):
        body, variables, report = macros.render(
            row["content"], labels=labels, variables=variables, report=report)
        blank = macros.module_is_blank(body)
        modules.append({
            "sequence": index,
            "identifier": row["identifier"],
            "name": row["name"],
            "role": row["role"],
            "original_chars": len(row["content"]),
            "rendered_chars": len(body),
            "contributes_text": not blank,
        })
        if not blank:
            kept.append(body.rstrip("\n"))

    # SillyTavern 把每个启用模块依次接进系统提示；这里同口径，用换行拼，
    # 不重排、不合并、不插自己的话。
    text = "\n\n".join(kept).strip()

    header_rows = [m for m in modules if m["name"].startswith(_HEADER_MARK)]
    anchor_rows = [m for m in modules if m["identifier"] in _ST_ANCHOR_IDENTIFIERS]
    assistant_rows = [m for m in modules if m["role"] == "assistant"]
    skipped = {
        "st_card_data_anchors": [m["identifier"] for m in anchor_rows],
        "st_card_data_anchors_count": len(anchor_rows),
        "st_section_header_rows": [m["name"] for m in header_rows],
        "st_section_header_rows_count": len(header_rows),
        "ember_disabled_modules_not_ported": disabled_count(payload),
        "st_regex_scripts_in_file": len(
            ((payload.get("extensions") or {}).get("regex_scripts") or [])),
        "assistant_role_rows_folded_into_single_system": [m["name"] for m in assistant_rows],
    }
    facts = {
        "SOLA_VERSION": str(payload.get("preset_settings_openai") or ""),
        "ember_enabled_modules": len(modules),
        "ember_modules_contributing_text": sum(1 for m in modules if m["contributes_text"]),
        "ember_original_sum_chars": sum(m["original_chars"] for m in modules),
        "ember_prompt_size_bytes": len(text.encode("utf-8")),
        "ember_prompt_size_chars": len(text),
        "macro_report": report.as_dict(),
    }
    return EmberRender(text=text, modules=modules, report=report,
                       skipped=skipped, facts=facts)


__all__ = ["EmberRender", "SolaSourceIntegrityError", "disabled_count", "enabled_rows",
           "load_source", "manifest", "render"]
