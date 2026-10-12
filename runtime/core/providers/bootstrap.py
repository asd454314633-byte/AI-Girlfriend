"""Inject project behaviour into ``speech_to_speech`` *without* editing the wheel.

Why this module exists
----------------------
Five files of the upstream distribution used to carry project edits in place
(``PATCH_MANIFEST.json`` -> ``modified``).  Three of them were migrated first:

* ``arguments_classes/module_arguments.py``  -- ``--tts`` was a hard-coded
  ``Literal[...]`` whitelist, so no provider added to
  ``providers/manifests/tts.json`` (not even ``test_fake``) could be named on the
  command line;
* ``s2s_pipeline.py`` -- ``get_tts_handler`` was an ``if/elif`` chain over backend
  names, so a new provider meant patching the vendor's pipeline file;
* ``TTS/qwen3_tts_handler.py`` -- delivery wording + segmentation were woven into
  the vendor's handler.

and two more carry the two P0 audio-path faults, now owned by
:mod:`providers.vendor_fixes`:

* ``baseHandler.py`` -- FAULT-S2S-TERMINATOR, the guarantee that an accepted
  ``EndOfResponse`` always closes the realtime response;
* ``api/openai_realtime/websocket_router.py`` -- FAULT-S2S-UNIT-LEAK, the bounded
  force-release that stops one dead handler thread from killing the whole pool.

All five are now at the vendor's bytes (verified against
``speech_to_speech-0.2.11.dist-info/RECORD``) and their behaviour is injected here
instead, at import time:

``module_arguments``  -> :func:`tts_choices` rebuilds the ``--tts`` annotation as
``Optional[Literal[<upstream> + <manifest>]]`` right after the vendor module has
executed.  ``HfArgumentParser._add_dataclass_arguments`` re-reads the annotation via
``typing.get_type_hints`` *when the parser is built*, i.e. after this hook, so the
choices the CLI accepts are whatever the manifest lists.  Adding a provider stays a
manifest row.

``s2s_pipeline``      -> ``get_tts_handler`` is replaced by
:func:`providers.tts_factory.get_tts_handler`.  The checked-in
``get_stt_handler`` is already a thin manifest-driven adapter; the hook verifies that
marker and replaces only an upstream function restored by a wheel reinstall.

``baseHandler`` /
``websocket_router``  -> the two :mod:`providers.vendor_fixes` wrappers, installed
through :data:`VENDOR_FIX_TARGETS`.  Neither copies a vendor code body: they wrap
``BaseHandler.run`` and ``_release_unit_after_drain`` and delegate, so they survive
an upstream rewrite of the surrounding lines, and a
``pip install --force-reinstall speech-to-speech`` restores exactly the pristine
state they were written against -- the fix re-applies itself with no human step.

``qwen3_tts_handler`` -> **not patched at all.**  The manifest points ``handler.module``
at :mod:`providers.adapters.qwen3_tts_adapter`, a subclass of the vendor handler.
Subclass beats monkey-patch here because the vendor file stays untouched *and* the
override is visible in ``.__mro__``.

How it gets loaded
------------------
``site-packages/zz_providers_bootstrap.pth`` (written by
``runtime/audit/system_v2/tools/migrate_out_of_site_packages.py``) calls
:func:`install` at interpreter start-up.  That is the only way this reaches every
entry point -- the console script, ``python -m``, the offline tests -- without
touching ``scripts/**`` or ``runtime/windows/**``, which are owned by the lifecycle
layer.  The .pth only registers two import hooks: no torch, no file reads, and a
raised error inside :func:`install` is swallowed on purpose so that ``pip`` itself
can never be bricked by this file.

Idempotent: calling :func:`install` twice is a no-op.
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Literal, Optional

CORE_DIR = Path(__file__).resolve().parents[1]
RUNTIME_DIR = CORE_DIR.parent

logger = logging.getLogger(__name__)

_INSTALLED = False
_PATCHES_APPLIED: list[str] = []


# ── the two injections ───────────────────────────────────────────────


def _literal_of(annotation: Any) -> tuple:
    """Unwrap ``Optional[Literal[...]]`` (or a bare ``Literal[...]``) to its args."""
    args = getattr(annotation, "__args__", None)
    if not args:
        return ()
    for arg in args:
        if getattr(arg, "__origin__", None) is Literal:
            return tuple(getattr(arg, "__args__", ()) or ())
        nested = _literal_of(arg)
        if nested:
            return nested
    return ()


def manifest_tts_tokens() -> list[str]:
    """Every ``--tts`` token the manifest declares: id, cli_flag_value, aliases.

    Disabled rows are included on purpose: *naming* a provider on the command line
    is the registry's question, *building* it is the factory's (:func:`
    providers.tts_factory.resolve` still refuses a disabled one, with a message that
    says why).  Keeping the gate in exactly one place is why ``test_fake`` can be
    driven by the contract tests without becoming selectable for a user.
    """
    from . import registry

    tokens: list[str] = []
    for row in registry.providers("TTS"):
        handler = row.get("handler") or {}
        for token in (row.get("id"), handler.get("cli_flag_value"), *(handler.get("cli_flag_aliases") or [])):
            text = str(token or "")
            if text and text not in tokens:
                tokens.append(text)
    return tokens


def manifest_stt_tokens() -> list[str]:
    """Every CLI token owned by an enabled STT registry row."""
    from . import registry

    tokens: list[str] = []
    for row in registry.providers("STT", enabled_only=True):
        handler = row.get("handler") or {}
        for token in (row.get("id"), handler.get("cli_flag_value"), *(handler.get("cli_flag_aliases") or [])):
            text = str(token or "")
            if text and text not in tokens:
                tokens.append(text)
    return tokens


def tts_choices(upstream_tokens: tuple = ()) -> list[str]:
    """Upstream's own list first (so ``--help`` reads like upstream), then the manifest's."""
    out: list[str] = []
    for token in [*upstream_tokens, *manifest_tts_tokens()]:
        text = str(token)
        if text and text not in out:
            out.append(text)
    return out


def _patch_module_arguments(module: Any) -> str:
    cls = getattr(module, "ModuleArguments", None)
    if cls is None:
        return "skipped: no ModuleArguments"
    fields = getattr(cls, "__dataclass_fields__", {})
    outcomes: list[str] = []

    tts_field = fields.get("tts")
    if tts_field is not None:
        choices = tts_choices(_literal_of(tts_field.type))
        annotation = Optional[Literal[tuple(choices)]]  # type: ignore[valid-type]
        cls.__annotations__["tts"] = annotation
        tts_field.type = annotation
        metadata = tts_field.metadata if isinstance(tts_field.metadata, dict) else {}
        metadata["help"] = (
            "The TTS to use. Accepted values come from providers/manifests/tts.json "
            f"(id + cli aliases), so a new provider needs no code change: {', '.join(choices)}. "
            f"Default is '{tts_field.default}'."
        )
        tts_field.metadata = metadata
        outcomes.append("tts choices -> " + ",".join(choices))

    stt_field = fields.get("stt")
    if stt_field is not None:
        from . import registry

        choices = manifest_stt_tokens()
        default_id = registry.default_selection("STT")
        default_spec = registry.get("STT", default_id) or {}
        default_handler = default_spec.get("handler") or {}
        default_flag = str(default_handler.get("cli_flag_value") or default_id or "")
        if not choices or default_flag not in choices:
            raise RuntimeError(
                f"STT_DEFAULT_NOT_SELECTABLE default={default_id!r} flag={default_flag!r} choices={choices!r}"
            )
        annotation = Optional[Literal[tuple(choices)]]  # type: ignore[valid-type]
        cls.__annotations__["stt"] = annotation
        stt_field.type = annotation
        stt_field.default = default_flag
        metadata = stt_field.metadata if isinstance(stt_field.metadata, dict) else {}
        metadata["help"] = (
            "The STT to use. Accepted values and the default come from "
            f"providers/manifests/stt.json: {', '.join(choices)}. Default is '{default_flag}'."
        )
        stt_field.metadata = metadata
        outcomes.append("stt choices/default -> " + ",".join(choices) + f" / {default_flag}")

    return "; ".join(outcomes) if outcomes else "skipped: no tts/stt fields"


def _patch_s2s_pipeline(module: Any) -> str:
    from . import stt_factory, tts_factory

    if not (hasattr(module, "get_tts_handler") and hasattr(module, "parse_arguments")):
        return "skipped: not a speech-to-speech pipeline module"
    # Both slots are handled independently and reported together: the second one was
    # added later, and an early "already applied" return on the first would have hidden
    # the fact that ``get_stt_handler`` was never replaced in an already-imported
    # process (``python -m``, or two module objects under ``__main__``).
    outcomes: list[str] = []
    if module.get_tts_handler is tts_factory.get_tts_handler:
        outcomes.append("already applied: get_tts_handler")
    else:
        module.get_tts_handler = tts_factory.get_tts_handler
        outcomes.append("get_tts_handler -> providers.tts_factory.get_tts_handler")
    current_stt = getattr(module, "get_stt_handler", None)
    if current_stt is None:
        outcomes.append("no get_stt_handler in this module")
    elif current_stt is stt_factory.get_stt_handler or getattr(current_stt, "_stt_registry_factory", False):
        outcomes.append("already registry-driven: get_stt_handler")
    else:
        # Recovery path after a wheel reinstall: restore the generic dispatcher in
        # memory.  Unknown providers then fail closed until they have a validated row.
        module.get_stt_handler = stt_factory.get_stt_handler
        outcomes.append("recovered get_stt_handler -> providers.stt_factory.get_stt_handler")
    return "; ".join(outcomes)


def _vendor_fix_hook(fix_id: str) -> Callable[[Any], str]:
    """Bind a :mod:`providers.vendor_fixes` entry to an after-import hook.

    The fix id is resolved *inside* the closure so importing this module stays
    cheap (``install`` runs from a ``.pth`` in every interpreter, including pip's)
    and so an unknown id degrades to a logged skip rather than a broken pipeline.
    """

    def hook(module: Any) -> str:
        from .vendor_fixes import by_id

        return by_id(fix_id).apply(module)

    hook.__name__ = "patch_" + fix_id.lower().replace("-", "_")
    hook.__qualname__ = hook.__name__
    return hook


def _patch_faster_whisper_final_only(module: Any) -> str:
    """Do not commit a progressive audio prefix as the user's final message.

    The upstream FasterWhisper handler emits ``Transcription`` for every
    ``VADAudio``, including ``mode='progressive'``.  The first prefix then marks
    that turn/revision final, so the actual speech-stop audio is discarded.
    This wrapper leaves the installed wheel pristine and delegates final and
    legacy inputs to the original handler unchanged.
    """
    handler = getattr(module, "FasterWhisperSTTHandler", None)
    if handler is None:
        return "skipped: no FasterWhisperSTTHandler"
    original = handler.process
    if getattr(original, "_mic_final_only_fix", False):
        return "already applied: faster-whisper final-only gate"

    def final_only(self: Any, vad_audio: Any):
        if getattr(vad_audio, "mode", None) == "progressive":
            logger.debug(
                "Skipping progressive FasterWhisper prefix turn=%s revision=%s",
                getattr(vad_audio, "turn_id", None),
                getattr(vad_audio, "turn_revision", None),
            )
            return
        yield from original(self, vad_audio)

    final_only._mic_final_only_fix = True
    handler.process = final_only
    return "FasterWhisperSTTHandler.process -> final-only gate"


#: Vendor files whose project edits became replayable overrides.
#: ``baseHandler.py`` keeps its own name under ``python -m`` because only
#: ``s2s_pipeline`` is ever executed as ``__main__``.
VENDOR_FIX_TARGETS: dict[str, str] = {
    "speech_to_speech.baseHandler": "FAULT-S2S-TERMINATOR",
    "speech_to_speech.api.openai_realtime.websocket_router": "FAULT-S2S-UNIT-LEAK",
}


TARGETS: dict[str, Callable[[Any], str]] = {
    "speech_to_speech.arguments_classes.module_arguments": _patch_module_arguments,
    "speech_to_speech.s2s_pipeline": _patch_s2s_pipeline,
    "speech_to_speech.STT.faster_whisper_handler": _patch_faster_whisper_final_only,
    **{name: _vendor_fix_hook(fix_id) for name, fix_id in VENDOR_FIX_TARGETS.items()},
    # ``python -m speech_to_speech.s2s_pipeline`` runs the file as __main__, i.e. as a
    # *second* module object the named hook never sees.  Both patchers sniff the
    # module's shape before touching it, so unrelated __main__ scripts are ignored.
    "__main__": _patch_s2s_pipeline,
}


# ── the import hook ──────────────────────────────────────────────────


class _AfterImportLoader(importlib.abc.Loader):
    """Run the real loader, then the project's patch for that module."""

    def __init__(self, inner: Any, after: Callable[[Any], str]) -> None:
        self._inner = inner
        self._after = after

    def create_module(self, spec):
        inner = self._inner
        if hasattr(inner, "create_module"):
            return inner.create_module(spec)
        return None

    def exec_module(self, module) -> None:
        inner = self._inner
        inner.exec_module(module)
        try:
            outcome = self._after(module)
        except Exception:  # a failed injection must never make the pipeline unimportable
            logger.exception("providers.bootstrap: patch for %s failed", module.__name__)
            return
        _PATCHES_APPLIED.append(f"{module.__name__}: {outcome}")
        logger.debug("providers.bootstrap: %s: %s", module.__name__, outcome)

    def __getattr__(self, name):  # delegate get_source / is_package / get_filename ...
        return getattr(self._inner, name)


class _AfterImportFinder(importlib.abc.MetaPathFinder):
    """Wrap the loader of exactly the modules named in ``TARGETS``."""

    def __init__(self) -> None:
        self._resolving: set[str] = set()

    def find_spec(self, fullname, path=None, target=None):
        after = TARGETS.get(fullname)
        if after is None or target is not None or fullname in self._resolving:
            return None
        self._resolving.add(fullname)
        try:
            spec = importlib.util.find_spec(fullname)
        except Exception:
            spec = None
        finally:
            self._resolving.discard(fullname)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _AfterImportLoader(spec.loader, after)
        return spec


def install() -> bool:
    """Register the import hooks.  Safe to call from a .pth in any process."""
    global _INSTALLED
    if _INSTALLED:
        return True
    _INSTALLED = True
    try:
        if str(RUNTIME_DIR) not in sys.path:
            sys.path.insert(0, str(RUNTIME_DIR))  # makes ``s2s_business`` importable
        if str(CORE_DIR) not in sys.path:
            sys.path.insert(0, str(CORE_DIR))
        if not any(isinstance(item, _AfterImportFinder) for item in sys.meta_path):
            sys.meta_path.insert(0, _AfterImportFinder())
    except Exception:  # pragma: no cover - interpreter start-up must not break
        print("providers.bootstrap: install failed", file=sys.stderr)
        return False
    return True


def applied() -> list[str]:
    """Which injections actually ran in this process (used by the offline checks)."""
    return list(_PATCHES_APPLIED)


def patch_now(module_name: str) -> str:
    """Apply ``TARGETS[module_name]`` to an already-imported module (test helper)."""
    after = TARGETS.get(module_name)
    if after is None:
        raise KeyError(module_name)
    module = importlib.import_module(module_name)
    outcome = after(module)
    _PATCHES_APPLIED.append(f"{module.__name__}: {outcome}")
    return outcome
