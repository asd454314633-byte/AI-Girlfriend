"""Compatibility shim -- the manual-voice compatibility facade moved to ``s2s_business.manual_voice_mode`` (任务 §6).

Why this file still exists
--------------------------
``speech_to_speech.TTS.manual_voice_mode`` is referenced from outside this package -- ``runtime/tests/test_manual_voice_mode.py`` and
``runtime/audit/system_v2/tools/apply_delivery_emit.py`` -- and by
``providers/manifests/tts.json`` (``handler.module``) for the TTS adapters.  The
task forbids editing those callers in this pass, so the old import path is kept
alive instead.  The *implementation* is no longer here: it lives in the project
tree, so a ``pip install --force-reinstall speech-to-speech`` can no longer take
project business logic down with it.

Byte-exact pre-migration copy:
``runtime/audit/system_v2/patches/manual_voice_mode.py.pre-migration-20260924``; the facade it replaces (it was itself already a shim over providers/).

This shim replaces itself with the implementation module in ``sys.modules``, so
``girlfriend_reading``/``…TTS.higgs_tts_handler`` and the moved module are *one*
module object: module-level state (caches, ``READING_ROOT``, monkeypatched
globals) is shared, not copied.
"""
from __future__ import annotations

import os
import sys
from importlib import import_module
from pathlib import Path


def _runtime_dir() -> Path:
    """Locate ``…/zero_replica/runtime`` without depending on sys.path.

    Deliberately self-contained: a compatibility shim must not need a helper it
    cannot yet import, and it must keep working even if
    ``zero_replica_providers.pth`` is gone.
    """
    override = os.environ.get("ZERO_REPLICA_RUNTIME")
    if override:
        candidate = Path(override).resolve()
    else:
        candidate = None
        for ancestor in Path(__file__).resolve().parents:
            if (ancestor / "core" / "providers").is_dir():
                candidate = ancestor
                break
    if candidate is None or not candidate.is_dir():
        raise ImportError(
            f"{__name__}: cannot find the zero_replica runtime directory from "
            f"{__file__}; set ZERO_REPLICA_RUNTIME to …/zero_replica/runtime"
        )
    return candidate


_RUN = _runtime_dir()
if str(_RUN) not in sys.path:
    sys.path.insert(0, str(_RUN))

_target = 's2s_business.manual_voice_mode'
_impl = import_module(_target)
sys.modules[__name__] = _impl
_parent, _, _child = __name__.rpartition(".")
if _parent:
    _pkg = sys.modules.get(_parent)
    if _pkg is not None:
        setattr(_pkg, _child, _impl)
