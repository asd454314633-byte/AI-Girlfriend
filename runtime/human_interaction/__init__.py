# -*- coding: utf-8 -*-
"""真人互动层（human_interaction）。

一个只做一件事的隔离模块：把「这一轮到底该怎么理解、怎么接」算成一小段补充话，
交给装配引擎并到系统那一块的尾巴上。

    from runtime.human_interaction import interaction_instruction

关着的时候它恒返回空串；本目录之外，生产代码里只有 context_builder.build() 一处调用。

设计、边界与留开项见仓库根目录的 HUMAN_INTERACTION_DESIGN.md。
"""
from .config import (ALLOWED_MODES, CONFIG_PATH, ENV_ENABLED, MODE_AUTONOMOUS,
                     MODE_HUMAN_NATURAL, MODE_OFF, MODE_STORY_DIRECTOR, describe_config,
                     enabled, mode)
from .engine import attach, explain, interaction_instruction, revision_directive
from .modes import MODES, label, prompt_text, registry_view

__all__ = ["interaction_instruction", "explain", "attach", "revision_directive",
           "enabled", "mode", "describe_config", "registry_view", "prompt_text", "label",
           "MODES", "ALLOWED_MODES", "CONFIG_PATH", "ENV_ENABLED", "MODE_HUMAN_NATURAL",
           "MODE_AUTONOMOUS", "MODE_STORY_DIRECTOR", "MODE_OFF"]
