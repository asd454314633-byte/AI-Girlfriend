"""Registry-driven construction of the STT handler.

This is the stage half of what :mod:`providers.tts_factory` did for the voice.
``speech_to_speech.s2s_pipeline.get_stt_handler`` is now a thin generic adapter into
this module, so the core no longer contains a recognizer-name ``if/elif`` chain.
``providers.bootstrap`` retains an import-time replacement only as reinstall
recovery: if pip restores the upstream wheel bytes, a fresh interpreter still gets
this factory rather than silently returning to vendor dispatch.

What the six pieces of the manifest row are used for, in this file:

``handler.module`` / ``handler.class``
    which class fills the pipeline's STT slot (:func:`load_handler_class`);
``handler.cli_arguments_class``
    which parsed optional-arguments dataclass feeds it (:func:`build`), looked up by
    *class name* so this module imports nothing provider-specific;
``launch.placeholders`` + ``handler.model_path``
    where the weights are, resolved by :func:`providers.registry.model_setup_kwargs`
    with the same law ``Get-ProviderLaunchContext`` applies in PowerShell;
``default_selection``
    which row is live, because a recognizer is a start-up argument of the resident
    :8765 process and therefore has no selection state file (:func:`resolve` takes
    the flag, ``registry.active_id('STT')`` answers with the declared default);
``capabilities``
    what the generic layer may promise the browser -- consumed by
    ``registry.ui_schema``/``validate_selection``, and checked for agreement with the
    injected wheel fixes by ``runtime/tests/test_stt_provider_factory.py``;
``enabled``
    whether a named provider may start (:func:`resolve`'s gate).

One deliberate limit
--------------------
**A declared model path never overwrites a parsed one.**  Rewriting argv-level values
inside the factory would move a running recognizer away from what the operator
started; :func:`_reconcile_model_path` therefore fills only a key that is *absent*
(which the handler's ``setup()`` would otherwise reject) and reports a disagreement
loudly instead of acting on it.
"""
from __future__ import annotations

import importlib
import logging
from queue import Queue
from threading import Event
from typing import Any

from . import registry

# Bound in ``tts_factory`` but keyed by ``provider_type`` + ``id``, so it serves every
# kind: it is what lets a manifest row whose ``handler.module`` still names the wheel be
# re-attached to a project adapter by ``manifests/handler_overrides.json``.
from .tts_factory import effective_handler

logger = logging.getLogger(__name__)

#: The optional-argument dataclasses the upstream pipeline passes positionally, keyed
#: by class *name* -- the same trick ``tts_factory`` uses, so no provider is named here.
ARGUMENT_CLASSES = (
    "WhisperSTTHandlerArguments",
    "FasterWhisperSTTHandlerArguments",
    "ParaformerSTTHandlerArguments",
    "MLXAudioWhisperSTTHandlerArguments",
    "ParakeetTDTSTTHandlerArguments",
)

def resolve(flag: str | None, *, require_enabled: bool = True) -> dict[str, Any]:
    """Manifest spec for a ``--stt`` value, or an error listing what exists.

    ``require_enabled=False`` lets a registered-but-withdrawn row be constructed by a
    contract fixture through the same generic path as a production provider.
    """
    spec = registry.spec_for_flag("STT", flag)
    if spec is None:
        raise ValueError(
            f"Unsupported STT backend: {flag!r}. Registered: {', '.join(registry.flag_values('STT'))}. "
            "Add a row to runtime/core/providers/manifests/stt.json to introduce one."
        )
    if require_enabled and not spec.get("enabled", True):
        raise ValueError(f"STT provider {flag!r} is registered but disabled in the manifest.")
    return spec


def load_handler_class(spec: dict[str, Any]) -> type:
    handler = effective_handler(spec)
    module_name = str(handler.get("module") or "")
    class_name = str(handler.get("class") or "")
    if not module_name or not class_name:
        raise ValueError(f"Provider {spec.get('id')!r} has no handler.module/handler.class in the manifest")
    try:
        return getattr(importlib.import_module(module_name), class_name)
    except (ImportError, RuntimeError) as exc:
        hint = str(handler.get("install_hint") or "")
        logger.error("Error importing %s from %s. %s", class_name, module_name, hint)
        raise


def _reconcile_model_path(spec: dict[str, Any], setup_kwargs: dict[str, Any]) -> None:
    """Fill a missing model path from the manifest; report a clash, never act on it."""
    declared = registry.model_setup_kwargs(str(spec.get("provider_type") or "STT"), str(spec.get("id")))
    for field, value in declared.items():
        got = setup_kwargs.get(field)
        if got is None:
            setup_kwargs[field] = value
            logger.info("STT provider %s had no %s; using the manifest-declared %s",
                        spec.get("id"), field, value)
        elif str(got) != str(value):
            logger.warning(
                "STT provider %s is loading %r while %s declares %r. The parsed command line "
                "wins on purpose -- rewriting it here would change which weights a running "
                "pipeline uses without the operator asking. Reconcile the launcher "
                "(runtime/windows/Switch-Girlfriend-Voice.ps1) or stt.json's launch.cli_args.",
                spec.get("id"), got, "stt.json", value,
            )


def build(
    flag: str | None,
    stop_event: Event,
    queue_in: Queue,
    queue_out: Queue,
    speculative_turns: Any = None,
    arguments_by_class: dict[str, Any] | None = None,
    require_enabled: bool = True,
):
    """Instantiate the handler the manifest binds to ``flag``.

    The constructor call keeps the wheel's exact shape (``stop_event`` positional, the
    two queues and ``setup_kwargs`` by keyword, no ``setup_args``) and ``speculative_turns``
    is assigned *after* construction rather than passed in, because on the STT side it
    is a class attribute of ``BaseSTTHandler`` and not a ``setup()`` parameter -- an
    "improvement" to either would change which positional slots a handler receives.
    """
    spec = resolve(flag, require_enabled=require_enabled)
    handler_class = load_handler_class(spec)
    binding = effective_handler(spec)
    args_class = str((spec.get("handler") or {}).get("cli_arguments_class") or "")
    args_instance = (arguments_by_class or {}).get(args_class) if args_class else None
    if args_class and args_instance is None:
        logger.warning("Provider %s declares %s but no parsed instance was supplied", spec.get("id"), args_class)
    setup_kwargs: dict[str, Any] = dict(vars(args_instance)) if args_instance is not None else {}
    _reconcile_model_path(spec, setup_kwargs)
    logger.info("STT provider %s -> %s.%s (setup kwargs: %s)",
                spec.get("id"), binding.get("module"), binding.get("class"),
                ",".join(sorted(setup_kwargs)) or "-")
    handler = handler_class(
        stop_event,
        queue_in=queue_in,
        queue_out=queue_out,
        setup_kwargs=setup_kwargs,
    )
    if speculative_turns is not None:
        handler.speculative_turns = speculative_turns
    # Identity comes from the row, not from a module constant: the same reason the TTS
    # side passes it, and what lets a log line say which recognizer answered.
    handler.provider_id = spec.get("id")
    return handler


def get_stt_handler(
    module_kwargs,
    stop_event,
    spoken_prompt_queue,
    text_prompt_queue,
    speculative_turns=None,
    whisper_stt_handler_kwargs=None,
    faster_whisper_stt_handler_kwargs=None,
    paraformer_stt_handler_kwargs=None,
    mlx_audio_whisper_stt_handler_kwargs=None,
    parakeet_tdt_stt_handler_kwargs=None,
):
    """Drop-in replacement for the upstream ``s2s_pipeline.get_stt_handler``.

    Same name, same positional order (the pipeline call site passes all ten
    positionally), same return.  The old recognizer-name ``if/elif`` becomes one
    manifest lookup and one :func:`build` call.  Unregistered values fail closed;
    the CLI choices are populated from the same registry by ``providers.bootstrap``.
    """
    rows = (
        whisper_stt_handler_kwargs,
        faster_whisper_stt_handler_kwargs,
        paraformer_stt_handler_kwargs,
        mlx_audio_whisper_stt_handler_kwargs,
        parakeet_tdt_stt_handler_kwargs,
    )
    flag = getattr(module_kwargs, "stt", None)
    arguments_by_class = {
        type(row).__name__: row for row in rows if row is not None
    }
    return build(
        flag,
        stop_event,
        queue_in=spoken_prompt_queue,
        queue_out=text_prompt_queue,
        speculative_turns=speculative_turns,
        arguments_by_class=arguments_by_class,
    )


# Lets the bootstrap distinguish this generic dispatcher from an upstream vendor
# chain after a wheel reinstall, without inspecting source text at import time.
get_stt_handler._stt_registry_factory = True
