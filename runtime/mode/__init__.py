# -*- coding: utf-8 -*-
"""模式体系（AI_GIRLFRIEND_MODE_SYSTEM_V1）。

这一层只回答一个问题：**现在 AI 女友处在哪个模式，这个模式各自加载什么**。

它存在的唯一理由是历史上发生过一次真实的模式污染：一个任务打开了 Sola V2 Ember
（作者官方叙事预设，正文要求第三人称叙事散文），于是整个 AI 女友所有场景都变成小说
——真人聊天里开始出现「她微微垂下眼帘」。根因不是模型、不是采样、不是 TTS，而是
**本应属于不同使用场景的规则同时进了一条生产 Prompt**。

所以这一层立三条硬规矩：

  1. **唯一事实源**：当前模式只在 ``mode_state.json`` 里，由本模块写、由本模块读。
     前端只显示和请求修改；``localStorage`` 最多记住 UI 上次选了哪一项，绝不参与
     生产判定；后端任何一处想知道当前模式，只能问这里。
  2. **模式自己的配置**：Ember、真人互动层、阅读上下文这些块的开关写在
     ``mode_config.json`` 的**模式条目里**，不再有全局开关。以后任何一个任务打开
     某一块，必须指名它属于哪个模式，不能让全场一起生效。
  3. **失败退回最安全的一档**：读不到、读坏、值不认识 → 回 ``ROLEPLAY``（那个
     真人聊天的模式），并且 Ember 一律不加载。宁可少一层规则，也不发互相矛盾的规则。

目录里有什么：
  ``mode_authority.py``     唯一权威：读/写当前模式 + 取该模式的开关与文案
  ``mode_config.json``      每个模式的开关（Ember / 真人互动层 / 阅读上下文 / 是否实现）
  ``mode_state.json``       当前模式（唯一事实源，原子写）
  ``prompts/``              每个模式各自那一段输出契约文案（正文只在这里改）

禁止：
  * 任何其它模块自己存一份「当前模式」；
  * 任何模式开关散进代码（``if 某模型: 特殊修复`` 那一类）；
  * 第三模式在本轮被实现——它只有 ``implemented: false``，前端按下它得不到任何东西。
"""
from __future__ import annotations

from .mode_authority import (  # noqa: F401
    DEFAULT_MODE,
    MODES,
    THIRD_MODE,
    available,
    current,
    describe,
    flags,
    human_interaction_enabled,
    include_reading_context,
    mode_prompt_text,
    set_mode,
    sola_v2_ember_enabled,
    state,
)

__all__ = [
    "MODES", "DEFAULT_MODE", "THIRD_MODE",
    "available", "current", "describe", "flags", "set_mode", "state",
    "sola_v2_ember_enabled", "human_interaction_enabled",
    "include_reading_context", "mode_prompt_text",
]
