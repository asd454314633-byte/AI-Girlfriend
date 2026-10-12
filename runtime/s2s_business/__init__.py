"""Project-owned code that is *loaded from* site-packages, migrated per 任务 §6.

What lives here
---------------
``higgs_tts_handler`` / ``fish_tts_handler``
    The two TTS adapters this project wrote for the upstream
    ``speech-to-speech`` pipeline.  They were dropped *inside* the wheel's own
    ``speech_to_speech/TTS/`` directory, which is why they used to look like
    vendor code in every audit.
``manual_voice_mode``
    The compatibility facade over ``providers.delivery`` / ``providers.registry``
    that keeps the pre-registry call signatures (``selection`` / ``qwen_instruct``
    / ``higgs_prefix`` / ``fish_text``) working.

The import paths under ``speech_to_speech.TTS.*`` still resolve — they are now
three-line shims that re-export from this package, because
``providers/manifests/tts.json`` binds ``handler.module`` to those dotted paths:

    "module": "speech_to_speech.TTS.higgs_tts_handler", "class": "HiggsTTSHandler"
    "module": "speech_to_speech.TTS.fish_tts_handler",  "class": "FishTTSHandler"

Repointing the manifest at ``s2s_business.*`` and deleting the shims is the
follow-up change request in the §31 report; until then a ``pip install
--force-reinstall speech-to-speech`` loses the shims (recoverable from
``runtime/audit/system_v2/PATCH_MANIFEST.json``) but can no longer lose the
implementation, which is the point of §6.

Provider-neutrality note: nothing in this package is consulted by name from the
pipeline.  Which adapter gets built is decided by the manifest through
``providers.tts_factory``; which one owns the audio turn terminator is decided by
pipeline topology in ``s2s_pipeline.get_tts_handler``.
"""

from __future__ import annotations

__all__ = ["fish_tts_handler", "higgs_tts_handler", "manual_voice_mode"]
