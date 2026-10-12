"""Project-level capability/provider registry for the AI girlfriend runtime.

Layering rule (任务 §0): this package is *system* code.  It knows how to enumerate
providers, validate a capability selection, resolve a health plan and hand the
browser a render schema.  It never contains ``if provider_id == "..."``.
"""
from __future__ import annotations

import sys
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = CORE_DIR.parent
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

__all__ = [
    "registry",
    "capabilities",
    "contracts",
    "delivery",
    "tts_client",
    "tts_factory",
    "voice_text",
    "adapters",
]
