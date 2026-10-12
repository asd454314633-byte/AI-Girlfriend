# -*- coding: utf-8 -*-
"""人物子系统：档案、设定、小说正文、上下文装配。

这一层不认得任何模型或语音提供方，也不联网。它只做四件事：
把人物存下来、把设定与原文收下来、按中文把相关内容找出来、把这些拼成一份
「可以直接交给模型」的上下文对象。

导入放在包初始化里做，是为了让 runtime/character/ 和 runtime/memory/ 两边的模块
都能用同一套契约对象，不必各自再去拼搜索路径。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]


def _bootstrap() -> None:
    for candidate in (PROJECT_ROOT, PROJECT_ROOT / "runtime" / "core",
                      PROJECT_ROOT / "runtime"):
        text = str(candidate)
        if candidate.exists() and text not in sys.path:
            sys.path.insert(0, text)


_bootstrap()

try:
    from runtime.core.providers import contracts  # type: ignore
except ImportError:  # pragma: no cover
    import providers.contracts as contracts  # type: ignore  # noqa: F401

CONTRACT_SCHEMA_VERSION = getattr(contracts, "SCHEMA_VERSION", 1)

__all__ = ["contracts", "store", "importer", "canon", "analyzer", "rules",
           "context_builder", "bundle", "role_state_bridge", "search",
           "CONTRACT_SCHEMA_VERSION"]
