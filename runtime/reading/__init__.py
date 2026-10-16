"""Project-owned reading companion (陪读) layer — migrated out of site-packages.

Why this package exists
-----------------------
``任务.txt`` §6: *「第三方 package 可以 patch，但项目业务逻辑不能长期只存在
site-packages」*.  Until this move, the whole reading domain — document import,
chapter/chunk indexing, progress, chapter summaries, the ``READING_STATE`` prompt
block — lived only in
``runtime/core/venv/Lib/site-packages/girlfriend_reading.py``, i.e. inside the
third-party ``speech-to-speech`` wheel's own directory, where a reinstall or a
fresh venv silently takes it away.

The real code is now :mod:`reading.documents`, byte-identical to what was in
site-packages.  The old import path is kept alive by a thin shim so that nothing
outside this package has to change yet::

    runtime/ui/app/local_api.py        import girlfriend_reading as reading
    runtime/ui/app/test_reading.py     import girlfriend_reading as R
    runtime/llm/_reading_closure_probe.py

Provenance / rollback
---------------------
Byte-exact pre-migration copies:
``runtime/audit/system_v2/patches/girlfriend_reading.py.pre-migration-20260924``
and the frozen tree under ``runtime/audit/system_v2/patches/speech_to_speech/``,
catalogued by ``runtime/audit/system_v2/PATCH_MANIFEST.json``.

Known, deliberately unfixed defect (kept out of this migration on purpose — see
the §31 report): ``documents.READING_ROOT`` and ``documents.DB_PATH`` still default
to WSL-era paths (``/mnt/d/…``, ``/home/user/…``) that do not exist on the
Windows native runtime, and production never sets ``READING_ROOT`` /
``GIRLFRIEND_MEMORY_DB``.  Fixing that is a behaviour change, not a relocation, so
it was left for the owner.  Importing this package creates no directories.
"""

from __future__ import annotations

from . import documents  # noqa: F401  re-exported so `reading.documents` is a real attribute

__all__ = ["documents"]
