# -*- coding: utf-8 -*-
"""记忆子系统：长期记忆的存取、检索，以及「哪些话值得记下来」的抽取。

和人物子系统一样：不认得任何模型或语音提供方，不联网，不加载常驻显存的东西。
本包里的 retrieval 是人物侧和记忆侧共用的中文检索内核。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
DATA_DIR = HERE / "data"


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
    contracts = None  # 契约对象缺席时，本包仍可按普通模块使用

__all__ = ["contracts", "service", "extractor", "retrieval", "DATA_DIR"]
