"""Model-free pipeline handlers used as contract fixtures (任务 §25 CORE CONTRACT).

The whole point of the registry work is that a *new* provider is one manifest row
plus one adapter.  These two adapters are the falsification test for that claim:
they were added without touching the persona, memory, tool, context-builder, main
UI or audio-queue code, and the ``test_provider_contract.py`` suite drives them
through the real pipeline message shapes.

Nothing in this package names a production provider.
"""
from __future__ import annotations

from .recorder import calls, clear, record

__all__ = ["calls", "clear", "record"]
