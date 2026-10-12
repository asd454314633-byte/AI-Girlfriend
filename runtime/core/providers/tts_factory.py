"""Registry-driven construction of the TTS handler.

``s2s_pipeline.get_tts_handler`` used to be an if/elif chain over backend names,
which meant three things: a new provider needed a core edit, the shared turn
context (``cancel_scope`` / ``speculative_turns``) was injected into a hand-written
list that silently omitted two providers, and an unknown name produced
``ValueError("Unsupported TTS backend")`` with no hint about what *is* supported.

This module replaces all three.  It names no provider: the manifest decides which
class implements a ``--tts`` value, which optional arguments dataclass feeds it,
and which install hint to print.

One safety valve sits on top of that: :func:`effective_handler` lets
``manifests/handler_overrides.json`` (a file only this layer writes) re-attach a
project adapter to a row whose ``handler.module`` still names the vendor class, so a
lost write to ``tts.json`` cannot silently drop behaviour the wheel no longer carries.
See ``runtime/audit/system_v2/MIGRATION_REPORT.md`` §7.

``inspect``-based kwarg filtering is deliberate.  Upstream handlers declare
explicit ``setup()`` signatures (kokoro, chatTTS, pocket do not accept extras), so
"pass the whole world and hope for ``**kwargs``" would raise TypeError.  Each
handler receives exactly the shared context it can accept, which is also how a
handler opts *in* to cancellation without any core change.
"""
from __future__ import annotations

import importlib
import inspect
import json
import logging
from queue import Queue
from threading import Event
from typing import Any

from . import registry

logger = logging.getLogger(__name__)

# Shared turn context every pipeline handler would like to have.
SHARED_CONTEXT = ("cancel_scope", "speculative_turns", "provider_id", "provider_spec")


def accepted_kwargs(handler_class: type, candidate: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Split ``candidate`` into what ``handler_class.setup`` accepts and what it doesn't."""
    try:
        parameters = inspect.signature(handler_class.setup).parameters.values()
    except (TypeError, ValueError):  # builtins / C extensions / *args-only
        return dict(candidate), []
    if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters):
        return dict(candidate), []
    names = {
        item.name
        for item in parameters
        if item.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    accepted = {key: value for key, value in candidate.items() if key in names}
    return accepted, sorted(set(candidate) - set(accepted))


def resolve(flag: str | None, *, require_enabled: bool = True) -> dict[str, Any]:
    """Manifest spec for a ``--tts`` value, or a raised error listing what exists.

    ``require_enabled=False`` lets a contract fixture (registered, but switched off
    for users) be constructed by the same generic path as a production provider.
    """
    spec = registry.spec_for_flag("TTS", flag)
    if spec is None:
        raise ValueError(
            f"Unsupported TTS backend: {flag!r}. Registered: {', '.join(registry.flag_values('TTS'))}. "
            "Add a row to runtime/core/providers/manifests/tts.json to introduce one."
        )
    if require_enabled and not spec.get("enabled", True):
        raise ValueError(f"TTS provider {flag!r} is registered but disabled in the manifest.")
    return spec


OVERRIDE_FILE = registry.MANIFEST_DIR / "handler_overrides.json"

# Import roots that belong to a wheel rather than to this project.  A manifest row
# naming one of them is a *vendor* binding, which is what an override may replace.
VENDOR_ROOTS = ("speech_to_speech.", "vllm.", "diffusers.")

_override_cache: tuple[int, dict[str, Any]] | None = None


def handler_overrides(*, refresh: bool = False) -> dict[str, Any]:
    """``{"TTS": {"<provider id>": {"module": …, "class": …, "reason": …}}}``.

    Cached on the file's mtime like the manifests themselves; a missing file simply
    means nothing is overridden, which keeps the project usable without it.
    """
    global _override_cache
    if not OVERRIDE_FILE.is_file():
        _override_cache = None
        return {}
    stamp = OVERRIDE_FILE.stat().st_mtime_ns
    if not refresh and _override_cache is not None and _override_cache[0] == stamp:
        return _override_cache[1]
    payload = json.loads(OVERRIDE_FILE.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        payload = {}
    _override_cache = (stamp, payload)
    return payload


def effective_handler(spec: dict[str, Any]) -> dict[str, Any]:
    """The ``handler`` block to load, i.e. the manifest's unless it still points at a
    vendor class that this project ships an adapter for.

    Deliberately narrow, so the manifest remains the source of truth:

    * a row whose ``handler.module`` already names project code is never touched —
      that is the normal, repaired state;
    * only rows naming a wheel-internal module may be replaced, and only by an entry
      of :func:`handler_overrides` under the same provider id;
    * the substitution is logged, because it means ``tts.json`` lost an update and
      ``runtime/audit/system_v2/tools/migrate_out_of_site_packages.py`` should re-run.

    No provider is named here: the id comes from the row, the adapter from the JSON.
    """
    handler = spec.get("handler") or {}
    module_name = str(handler.get("module") or "")
    if module_name.startswith(VENDOR_ROOTS):
        entry = (handler_overrides().get(str(spec.get("provider_type") or "TTS")) or {}).get(str(spec.get("id")))
        if isinstance(entry, dict) and entry.get("module") and entry.get("class"):
            logger.warning(
                "Provider %s is bound in %s to the vendor class %s.%s; using the project "
                "adapter %s.%s from %s instead (%s). Repair the manifest by re-running "
                "runtime/audit/system_v2/tools/migrate_out_of_site_packages.py.",
                spec.get("id"), OVERRIDE_FILE.parent.name, module_name, handler.get("class"),
                entry["module"], entry["class"], OVERRIDE_FILE.name, entry.get("reason") or "no reason given",
            )
            merged = dict(handler)
            merged["module"] = str(entry["module"])
            merged["class"] = str(entry["class"])
            return merged
    return handler


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


def build(
    flag: str | None,
    stop_event: Event,
    queue_in: Queue,
    queue_out: Queue,
    should_listen: Event,
    arguments_by_class: dict[str, Any] | None = None,
    require_enabled: bool = True,
    **shared: Any,
):
    """Instantiate the handler the manifest binds to ``flag``.

    ``arguments_by_class`` maps a parsed optional-arguments dataclass by its class
    name (``Qwen3TTSHandlerArguments`` -> instance), which is how a provider keeps
    its own CLI surface without the core enumerating providers.
    """
    spec = resolve(flag, require_enabled=require_enabled)
    handler_class = load_handler_class(spec)
    binding = effective_handler(spec)
    args_class = str((spec.get("handler") or {}).get("cli_arguments_class") or "")
    args_instance = (arguments_by_class or {}).get(args_class) if args_class else None
    if args_class and args_instance is None:
        logger.warning("Provider %s declares %s but no parsed instance was supplied", spec.get("id"), args_class)
    setup_kwargs: dict[str, Any] = dict(vars(args_instance)) if args_instance is not None else {}
    # Identity is part of the shared context: an adapter asks "which provider am I"
    # from the manifest instead of a module constant being the only source, and a
    # fixture such as providers.adapters.fake_tts_adapter needs no constant at all.
    shared.setdefault("provider_id", spec.get("id"))
    shared.setdefault("provider_spec", spec)
    for name in SHARED_CONTEXT:
        if name in shared and shared[name] is not None:
            setup_kwargs[name] = shared[name]
    accepted, skipped = accepted_kwargs(handler_class, setup_kwargs)
    for name in skipped:
        logger.debug("Handler %s does not accept setup kwarg %s", handler_class.__name__, name)
    logger.info("TTS provider %s -> %s.%s (setup kwargs: %s)",
                spec.get("id"), binding.get("module"), binding.get("class"),
                ",".join(sorted(accepted)) or "-")
    return handler_class(
        stop_event,
        queue_in=queue_in,
        queue_out=queue_out,
        setup_args=(should_listen,),
        setup_kwargs=accepted,
    )


# The five optional-argument dataclasses the upstream pipeline hands over
# positionally, keyed by the class *name* so this module never imports them.
ARGUMENT_CLASSES = (
    "ChatTTSHandlerArguments",
    "FacebookMMSTTSHandlerArguments",
    "PocketTTSHandlerArguments",
    "KokoroTTSHandlerArguments",
    "Qwen3TTSHandlerArguments",
)


def _shared_context_from_arguments(rows: dict[str, Any]) -> dict[str, Any]:
    """Recover ``cancel_scope`` / ``speculative_turns`` from the parsed arguments.

    The pristine ``initialize_pipeline`` injects both into every handler's
    ``*_handler_kwargs`` object (``vars(kw)["cancel_scope"] = …``), which is how the
    vendor's own five backends receive them.  Higgs / fish / the contract fixture have
    no upstream dataclass, so the factory reads the same values off whichever row does
    carry them instead of asking the pipeline to pass them down — that is what lets
    ``s2s_pipeline.py`` stay at the vendor's bytes.
    """
    out: dict[str, Any] = {}
    for name in ("cancel_scope", "speculative_turns"):
        for row in rows.values():
            value = getattr(row, name, None)
            if value is not None:
                out[name] = value
                break
    return out


def get_tts_handler(
    module_kwargs,
    stop_event,
    lm_response_queue,
    send_audio_chunks_queue,
    should_listen,
    chat_tts_handler_kwargs=None,
    facebook_mms_tts_handler_kwargs=None,
    pocket_tts_handler_kwargs=None,
    kokoro_tts_handler_kwargs=None,
    qwen3_tts_handler_kwargs=None,
    cancel_scope=None,
    speculative_turns=None,
):
    """Drop-in replacement for the upstream ``s2s_pipeline.get_tts_handler``.

    Same name, same positional order, same return; the ``if/elif`` over backend names
    is replaced by one :func:`build` call, and the two turn-context parameters are
    optional so the *unpatched* call site (the vendor's own
    ``get_pipeline_chain``, which never mentions them) keeps working.  Installed on
    the vendor module by :mod:`providers.bootstrap`; see the migration report.

    The audio turn terminator is marked here rather than inside each adapter: which
    stage owns ``AUDIO_RESPONSE_DONE`` is a fact about pipeline topology, so marking
    the slot — not the vendor — is what makes the guarantee hold for a provider that
    does not exist yet (``BaseHandler.run()`` honours the mark).
    """
    arguments_by_class = {
        name: row
        for row in (
            chat_tts_handler_kwargs,
            facebook_mms_tts_handler_kwargs,
            pocket_tts_handler_kwargs,
            kokoro_tts_handler_kwargs,
            qwen3_tts_handler_kwargs,
        )
        if row is not None
        for name in (type(row).__name__,)
    }
    shared = _shared_context_from_arguments(arguments_by_class)
    if cancel_scope is not None:
        shared["cancel_scope"] = cancel_scope
    if speculative_turns is not None:
        shared["speculative_turns"] = speculative_turns

    handler = build(
        module_kwargs.tts,
        stop_event,
        queue_in=lm_response_queue,
        queue_out=send_audio_chunks_queue,
        should_listen=should_listen,
        arguments_by_class=arguments_by_class,
        cancel_scope=shared.get("cancel_scope"),
        speculative_turns=shared.get("speculative_turns"),
    )
    handler.emits_turn_terminator = True
    return handler
