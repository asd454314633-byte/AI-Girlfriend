# -*- coding: utf-8 -*-
"""最小清洗（voice minimal）：**只删不改**，而且只许删两类东西。

任务 ``AI_GIRLFRIEND_SOLA_V2_EMBER_MINIMAL_CLEAN_01`` 的口径：

  1. ``sola_identity`` —— 把模型自身定义成 Sola / co-author / 一个独立知情主体的语句，
     以及给它套的具名外壳 ``<sola>`` / ``</sola>``；
  2. ``action_output`` —— 要求或示范「正式回复里用星号把动作包起来输出」的文本
     （整块里唯一一处，就是 Anti-Echo 那段盖章 ``APPROVED ✓`` 的例句）。

除这两类之外，作者的规则一个字节都不许动：400–500 词 / 4–5 段那条长度规则、
成人 / 暴力 / 黑暗题材、人物自主性、anti-sycophancy、anti-assistant、自然对话、
允许冲突与拒绝、文风要求 —— 全部原样。任务里写死了：即使觉得「不适合语音」也不许改。

冻结源 ``source/Sola V2 Ember.json`` 不参与本模块的任何写操作，一个字都不改；
本模块只是「渲染时少喂几段作者正文」。

三件事保证这是「删」而不是「改」：

  * 删除区间全部写在 :data:`SPEC_PATH` 那份冻结清单里。每条锚点必须在其所属模块的
    **原始正文**（宏未渲染、称呼未落地的那一份字节）里出现恰好一次，并且落在冻结时
    记录的偏移上 —— 抄错一个字节、或者作者原文与冻结时不一致，当场抛
    :class:`VoiceMinimalSpecError`；
  * 没被点名的模块用的是**同一个 dict 对象**（连拷贝都没有），所以「其他模块没动」
    是构造层面成立的，不依赖事后比对；
  * :func:`reinsert` 把删掉的字节按记录的偏移插回去，必须与该模块清洗前的正文
    **逐字相等** —— 这条是 ``OTHER_EMBER_CONTENT_REWRITTEN=NO`` 的硬依据，
    不靠人眼看 diff。

清洗失败时本层宁可不给规则：适配器会整块旁路（返回空串并记下原因），绝不把带着
被禁身份声明的文本发出去。想拿回逐字原版，把 ``voice_minimal_enabled`` 拨回 ``false``
就行，不用删任何文件。

本模块不认识模型、不认识 TTS、不联网；只有 :func:`render` 读盘（冻结件 + 清单）。
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ember, macros

HERE = Path(__file__).resolve().parent
SPEC_PATH = HERE / "voice_minimal_edits.json"
# 给人看的三件派生产物：清洗后的整块、逐字 diff、来路回执。
# **运行期不读它们**（运行期永远从冻结件现场量），所以它们旧了不会污染生产，
# 而验收脚本会再量一次并断言「盘上这份 == 现场量出来那份」。
ARTIFACT_TXT = HERE / "sola_v2_ember_voice_minimal.txt"
ARTIFACT_DIFF = HERE / "sola_v2_ember_voice_minimal.diff"
ARTIFACT_META = HERE / "sola_v2_ember_voice_minimal.meta.json"

CATEGORIES = ("sola_identity", "action_output")
_EDIT_FIELDS = ("id", "category", "module_identifier", "anchor", "offset_in_module_raw")


class VoiceMinimalSpecError(RuntimeError):
    """删除清单与冻结原始件对不上：锚点找不到 / 不止一处 / 偏移不符 / 类别不在白名单。"""


_LOCK = threading.Lock()
_SPEC_CACHE: dict[str, Any] = {"mtime": None, "raw": None}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def spec_mtime() -> int:
    try:
        return SPEC_PATH.stat().st_mtime_ns
    except OSError:
        return -1


def load_spec(*, force: bool = False) -> dict[str, Any]:
    """读那份冻结删除清单；mtime 变了就重读（与适配器同一口径，不锁死在内存里）。

    顺手把清单自己的 SHA 算出来带走，交付件里能回指到「是哪一份清单删的」。
    """
    stat = spec_mtime()
    if stat == -1:
        raise VoiceMinimalSpecError("VOICE_MINIMAL_SPEC_MISSING")
    with _LOCK:
        if not force and _SPEC_CACHE["mtime"] == stat and _SPEC_CACHE["raw"] is not None:
            return dict(_SPEC_CACHE["raw"])
    raw_bytes = SPEC_PATH.read_bytes()
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("edits"), list) or not raw["edits"]:
            raise ValueError("spec shape")
    except VoiceMinimalSpecError:
        raise
    except Exception as exc:
        raise VoiceMinimalSpecError(
            f"VOICE_MINIMAL_SPEC_UNREADABLE: {type(exc).__name__}") from exc
    seen: set[str] = set()
    for edit in raw["edits"]:
        if not isinstance(edit, dict):
            raise VoiceMinimalSpecError("VOICE_MINIMAL_SPEC_EDIT_NOT_MAPPING")
        for key in _EDIT_FIELDS:
            if key not in edit:
                raise VoiceMinimalSpecError(f"VOICE_MINIMAL_SPEC_EDIT_FIELD_MISSING: {key}")
        if edit["category"] not in CATEGORIES:
            raise VoiceMinimalSpecError(
                f"VOICE_MINIMAL_SPEC_CATEGORY_FORBIDDEN: {edit['category']}")
        if str(edit["id"]) in seen:
            raise VoiceMinimalSpecError(f"VOICE_MINIMAL_SPEC_DUPLICATE_ID: {edit['id']}")
        seen.add(str(edit["id"]))
        if not str(edit["anchor"]):
            raise VoiceMinimalSpecError("VOICE_MINIMAL_SPEC_EMPTY_ANCHOR")
    raw["spec_sha256"] = hashlib.sha256(raw_bytes).hexdigest()
    with _LOCK:
        _SPEC_CACHE.update({"mtime": stat, "raw": raw})
    return dict(raw)


@dataclass(slots=True)
class Applied:
    """一条真的被抠掉的区间：在哪一节、第几个字节起、删了什么、属于哪一类。"""

    id: str
    category: str
    module_identifier: str
    module_name: str
    offset: int
    deleted: str
    rendered_deleted: str = ""

    @property
    def deleted_chars(self) -> int:
        return len(self.deleted)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "module": self.module_name,
            "module_identifier": self.module_identifier,
            "offset_in_module_raw": self.offset,
            "deleted_chars": self.deleted_chars,
            "deleted_sha8": sha256_text(self.deleted)[:8],
            "deleted_literal": self.deleted,
            "rendered_literal": self.rendered_deleted,
        }


def reinsert(cleaned: str, applied: list[Applied]) -> str:
    """把删掉的字节按记录的偏移插回去 —— 用来证明「只发生删除，没有改写也没有重排」。"""
    out = cleaned
    for one in sorted(applied, key=lambda item: item.offset):
        out = out[:one.offset] + one.deleted + out[one.offset:]
    return out


def apply_deletions(text: str, edits: list[dict[str, Any]]) -> tuple[str, list[Applied]]:
    """对一段作者正文应用一组删除。

    偏移**从大到小**应用：先删后面的区间，前面那些偏移在剩余文本里依然成立，
    于是 :func:`reinsert` 能用同一批偏移逐字还原。任何一条对不上，整段作废并抛错。
    """
    out = text
    applied: list[Applied] = []
    for edit in sorted(edits, key=lambda one: int(one["offset_in_module_raw"]), reverse=True):
        anchor = str(edit["anchor"])
        want = int(edit["offset_in_module_raw"])
        need = int(edit.get("occurrences_required", 1))
        hits = out.count(anchor)
        if hits != need:
            raise VoiceMinimalSpecError(
                f"VOICE_MINIMAL_ANCHOR_COUNT[{edit['id']}]={hits} 应为 {need}")
        at = out.find(anchor)
        if at != want:
            raise VoiceMinimalSpecError(
                f"VOICE_MINIMAL_ANCHOR_OFFSET[{edit['id']}]={at}，冻结时记录的是 {want}")
        out = out[:at] + out[at + len(anchor):]
        applied.append(Applied(
            id=str(edit["id"]), category=str(edit["category"]),
            module_identifier=str(edit["module_identifier"]),
            module_name=str(edit.get("module_name") or ""),
            offset=at, deleted=anchor))
    if reinsert(out, applied) != text:
        raise VoiceMinimalSpecError("VOICE_MINIMAL_RECONSTRUCT_MISMATCH")
    applied.sort(key=lambda one: (one.module_identifier, one.offset))
    return out, applied


def clean_payload(data: dict[str, Any]) -> tuple[dict[str, Any], list[Applied]]:
    """整份原始件 → 「少喂几段作者正文」的同一份结构。

    没被点名的模块沿用**同一个对象**（不是副本），所以「其他内容一个字没动」
    在构造层面就成立。
    """
    row = load_spec()
    edits = list(row["edits"])
    wanted = {str(one["module_identifier"]) for one in edits}
    prompts: list[Any] = []
    applied: list[Applied] = []
    touched: set[str] = set()
    for prompt in data.get("prompts") or []:
        identifier = str(prompt.get("identifier") or "")
        if identifier not in wanted:
            prompts.append(prompt)                       # 原对象，零拷贝
            continue
        body = str(prompt.get("content") or "")
        mine = [one for one in edits if str(one["module_identifier"]) == identifier]
        new_body, spanned = apply_deletions(body, mine)
        if new_body == body:
            raise VoiceMinimalSpecError(f"VOICE_MINIMAL_NO_EFFECT[{identifier}]")
        copy = dict(prompt)
        copy["content"] = new_body
        prompts.append(copy)
        applied.extend(spanned)
        touched.add(identifier)
    if touched != wanted:
        raise VoiceMinimalSpecError(
            "VOICE_MINIMAL_MODULE_NOT_FOUND: " + ",".join(sorted(wanted - touched)))
    out = dict(data)
    out["prompts"] = prompts
    return out, applied


@dataclass(slots=True)
class VoiceMinimalRender:
    """一次现场量出来的「原版 / 清洗版」对照，diff 与那些键都从这一份出。"""

    text: str = ""                       # 交给装配引擎的那一块（清洗版）
    raw_text: str = ""                   # 同一渲染口径下不清洗的原版
    applied: list[Applied] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    modules: list[dict[str, Any]] = field(default_factory=list)
    skipped: dict[str, Any] = field(default_factory=dict)
    report: Any = None


def _render_span(span: str, *, char_label: str, user_label: str) -> str:
    """删掉的那段作者正文在渲染后长什么样（只为把 diff 对到渲染文本的行上）。"""
    try:
        body, _variables, _report = macros.render(
            span, labels={"char": char_label, "user": user_label,
                          "persona": user_label, "actual_user": user_label},
            variables={}, report=macros.MacroReport())
    except Exception:
        return span
    return body


def render(*, char_label: str = "", user_label: str = "",
           data: dict[str, Any] | None = None) -> VoiceMinimalRender:
    """渲染清洗版；同时把不清洗的原版按同一次口径再量一遍，两版一起交出去。"""
    payload = data if data is not None else ember.load_source()
    plain = ember.render(char_label=char_label, user_label=user_label, data=payload)
    cleaned_payload, applied = clean_payload(payload)
    cleaned = ember.render(char_label=char_label, user_label=user_label, data=cleaned_payload)
    for one in applied:
        one.rendered_deleted = _render_span(one.deleted, char_label=char_label,
                                            user_label=user_label)
    row = load_spec()
    facts = {
        **cleaned.facts,
        "voice_minimal_spec": str(row.get("spec") or ""),
        "voice_minimal_spec_sha256": str(row.get("spec_sha256") or ""),
        "voice_minimal_edits": len(applied),
        "voice_minimal_raw_chars": len(plain.text),
        "voice_minimal_raw_bytes": len(plain.text.encode("utf-8")),
        "voice_minimal_clean_chars": len(cleaned.text),
        "voice_minimal_clean_bytes": len(cleaned.text.encode("utf-8")),
        "voice_minimal_deleted_chars_raw": sum(one.deleted_chars for one in applied),
        "voice_minimal_deleted_chars_rendered": sum(
            len(one.rendered_deleted) for one in applied),
        "voice_minimal_deleted_by_category": {
            cat: sum(one.deleted_chars for one in applied if one.category == cat)
            for cat in CATEGORIES},
        "voice_minimal_categories_used": sorted({one.category for one in applied}),
        "voice_minimal_applied": [one.as_dict() for one in applied],
    }
    return VoiceMinimalRender(text=cleaned.text, raw_text=plain.text, applied=applied,
                              modules=cleaned.modules, skipped=cleaned.skipped,
                              report=cleaned.report, facts=facts)


__all__ = ["ARTIFACT_DIFF", "ARTIFACT_META", "ARTIFACT_TXT", "Applied", "CATEGORIES",
           "SPEC_PATH", "VoiceMinimalRender", "VoiceMinimalSpecError", "apply_deletions",
           "clean_payload", "load_spec", "reinsert", "render", "sha256_text", "spec_mtime"]
