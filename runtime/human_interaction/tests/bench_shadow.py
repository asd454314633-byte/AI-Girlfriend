# -*- coding: utf-8 -*-
"""搭一份**影子**上下文，给 12 场景测试台用。

为什么要影子：任务 §16 要「同一个人物、同一模型、同一上下文」，但生产当前选的人物
（c8b57248624）档案写着「张兰／妈妈／母子」，台词却写着「你叫阿满，是一只在书店工作
的猫」——两头对不上。拿它跑质量对比，测的是档案冲突不是互动层。本任务 §2 明令不许动
现有人物设定，所以这里**不去改生产数据**，而是用装配引擎自带的测试位（build 的
db_path / memory_db_path）另开一份只待在这个目录里的档案。

四条界线：
  * 写只写在本目录（shadow/）：生产的 characters.sqlite3、memories.sqlite3、
    active_character.json 指针，一个字节都不改（指针位置也被挪到本目录）；
  * 影子人物的台词必须过生产那套自然度校验（strict），过不了就报错，不许为跑测试放宽；
  * 三档之间的差别**只有**互动层的开关与档位，人物／模型／上下文／历史全部同一份；
  * 这里不含任何模型地址，也不联网。
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Sequence

HERE = Path(__file__).resolve().parent
SHADOW_DIR = HERE / "shadow"
CHARACTER_DB = SHADOW_DIR / "characters.sqlite3"
MEMORY_DB = SHADOW_DIR / "memories.sqlite3"
POINTER_PATH = SHADOW_DIR / "active_character.json"

PROJECT_ROOT = HERE.parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.character import context_builder, store  # noqa: E402

# 影子人物：一段自然语言台词（没有任何数值人格，§7）
SHADOW_CHARACTER_ID = "hi_bench_suwan"
SHADOW_PROFILE = {
    "display_name": "苏晚",
    "nickname": "晚晚",
    "pronoun": "她",
    "speaking_style": "短句，爱反问，不喜欢被安排",
    "relationship": "在一起两年",
    "boundary": "不替对方做决定",
    "greeting": "嗯，我在。",
}
SHADOW_PERSONA = """你叫苏晚，二十八岁，在一家旧书店做装帧修复。
你跟他在一起两年了，不是客服，也不是助理。
你句子短，喜欢反问，不喜欢那种安排好的体贴。
你脾气急，缓得也快，气头上话会少。
他连着几天弄到很晚你会心疼，但你不说心疼，只问他吃没吃。
你受不了「辛苦了」「注意休息」这类客气话从自己嘴里出来。"""

_BASE_SETTINGS: dict[str, Any] | None = None



def _redirect() -> None:
    """把「现在是谁在说话」那颗指针挪进本目录，生产的一概不碰。"""
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    store.set_pointer_path(POINTER_PATH)


def build_shadow(rebuild: bool = True) -> dict[str, Any]:
    """把影子档案落到本目录，返回用到的位置。不写活动人物指针。"""
    _redirect()
    from runtime.memory import service as memory_service

    for path in (CHARACTER_DB, MEMORY_DB):
        if rebuild and path.exists():
            path.unlink()
    store.init_db(CHARACTER_DB)
    memory_service.init_db(MEMORY_DB)
    profile = store.CharacterProfile(character_id=SHADOW_CHARACTER_ID, **SHADOW_PROFILE)
    store.save_profile(profile, db_path=CHARACTER_DB)
    store.set_persona(SHADOW_PERSONA, character_id=SHADOW_CHARACTER_ID, db_path=CHARACTER_DB)
    issues = store.persona_issues(SHADOW_PERSONA)
    if issues:
        raise RuntimeError("SHADOW_PERSONA_REJECTED: " + json.dumps(list(issues), ensure_ascii=False))
    return {"character_db": str(CHARACTER_DB), "memory_db": str(MEMORY_DB),
            "pointer": str(POINTER_PATH), "character_id": SHADOW_CHARACTER_ID}


def assemble(history: Sequence[Any] = (), user_text: str = "",
             session_id: str = "hi_bench") -> tuple[str, dict[str, Any]]:
    """走**生产那一条**装配路径：build 一次、messages 一次、只取系统段接成一块。

    与 character_admin 给浏览器的形状一致（同一个 build + messages 口径）。
    """
    context = context_builder.build(
        character_id=SHADOW_CHARACTER_ID, session_id=session_id, user_text=user_text,
        history=list(history or ()), query="",
        db_path=CHARACTER_DB, memory_db_path=MEMORY_DB)
    rows = context_builder.messages(context, temporary_state=None)
    parts = [str(one.get("text") or one.get("content") or "").strip()
             for one in rows if str(one.get("role") or "") == "system"]
    text = "\n\n".join(one for one in parts if one).strip()
    return text, {"chars": len(text), "budget_used": context.budget_used,
                  "budget_total": context.budget_total,
                  "dropped": list(context.budget_dropped or [])}


def settings_for(mode: str | None) -> dict[str, Any]:
    """在内存里造一份档位设置：None = 旧基线（整层关着）。绝不写磁盘。"""
    global _BASE_SETTINGS
    from runtime.human_interaction import config
    if _BASE_SETTINGS is None:
        _BASE_SETTINGS = copy.deepcopy(config._snapshot())
    settings = copy.deepcopy(_BASE_SETTINGS)
    if mode is None:
        settings["feature_enabled"] = False
    else:
        settings["feature_enabled"] = True
        settings["interaction_mode"] = mode
    return settings


def _assemble_with(settings: dict[str, Any], *, history: Sequence[Any],
                   user_text: str) -> tuple[str, dict[str, Any]]:
    """把内存里造好的档位临时换成 config 的读档结果，装配一次，再还原。"""
    from runtime.human_interaction import config
    original = config._snapshot
    config._snapshot = lambda: settings
    try:
        return assemble(history=history, user_text=user_text)
    finally:
        config._snapshot = original


def assemble_as(mode: str | None, *, history: Sequence[Any] = (),
                 user_text: str = "") -> tuple[str, dict[str, Any], str]:
    """按某一档装配一次，返回（系统块, 记账, 这一层加进去的那一小段）。

    档位只在调用里生效，磁盘上那份 human_interaction.json 全程保持
    feature_enabled=false，所以生产侧始终是无这一层的状态。

    取「加进去的那段」用的是最长公共前缀 + 最长公共后缀对拍：
    这一层是往系统规则那块**尾巴**上接话，那块后面还有人物、台词、关系、记忆等块，
    所以加完不是「整块末尾多一段」，而是中间插进去一段。用前后缀对拍正好能把这点
    证干净——两边拼回去必须逐字等于关掉那份，多一个字少一个字都算接线坏了。
    """
    off_text, off_meta = _assemble_with(settings_for(None), history=history,
                                        user_text=user_text)
    if mode is None:
        return off_text, off_meta, ""
    text, meta = _assemble_with(settings_for(mode), history=history, user_text=user_text)
    head = 0
    limit = min(len(off_text), len(text))
    while head < limit and off_text[head] == text[head]:
        head += 1
    tail = 0
    while tail < (limit - head) and off_text[len(off_text) - 1 - tail] == text[len(text) - 1 - tail]:
        tail += 1
    added = text[head:len(text) - tail]
    # 唯一要证的：把中间插进去那段摘掉，剩下的必须逐字等于关掉这一层时那份。
    if head + tail + len(added) != len(text) or text[:head] + text[len(text) - tail:] != off_text:
        raise AssertionError(f"SPLICE_MISMATCH head={head} tail={tail} added={len(added)} "
                             f"off={len(off_text)} on={len(text)}")
    return text, meta, added.strip()


if __name__ == "__main__":
    print(json.dumps(build_shadow(), ensure_ascii=False, indent=2))
    his = ({"role": "user", "text": "还在忙"},
           {"role": "assistant", "text": "忙着呢，你今天回来挺早"})
    for arm in (None, "human_natural", "autonomous", "story_director"):
        text, meta, added = assemble_as(arm, history=his, user_text="随便跟你说说，今天啥也没干成")
        print("=" * 70)
        print(f"ARM={arm} chars={meta['chars']} added={len(added)}")
        print(added if added else "(这一层什么都没加)")
