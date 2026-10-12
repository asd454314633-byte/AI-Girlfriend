"""FAULT-S2S-TERMINATOR as a project-owned override (任务 P0-1).

The fault
---------
``AUDIO_RESPONSE_DONE`` is the only thing on the audio path that closes a
realtime response.  If the TTS adapter that received an ``EndOfResponse`` fails
to emit it, ``finish_response`` never runs (it is gated on ``st.in_response``),
so every later ``response.create`` is refused with "Cannot create response while
another response is in progress" -- the P0 "the girlfriend stopped responding"
class.  Two shipped code paths lose the sentinel on purpose or by accident: an
adapter that raises inside ``process()`` (``BaseHandler.run`` swallows it and
puts nothing on the output queue as compensation) and an adapter that returns
early for a stale turn (``qwen3_tts_handler.py`` / ``fake_tts_adapter.py``).

How the override is built
-------------------------
It does **not** copy the vendor's ``run()`` body.  Two pieces only:

1. a class-level ``emits_turn_terminator = False`` default, which
   ``s2s_pipeline.get_tts_handler`` (already project-owned via
   :mod:`providers.tts_factory`) flips to ``True`` on whichever adapter the
   registry resolved -- the role is pipeline topology, so a new provider still
   needs zero edits outside its own adapter;
2. a ``run()`` wrapper that, immediately before delegating to the vendor's own
   ``run``, installs an *instance-level* guard around ``self.process``.  The
   guard watches the stream the vendor's loop consumes and, when an accepted
   ``EndOfResponse`` produced no terminator, yields exactly one
   ``AUDIO_RESPONSE_DONE`` itself.

Patching ``BaseHandler.process`` at class level would never fire: every subclass
defines its own ``process``, so the guard has to sit between the vendor's
``run()`` and the *bound* method of the instance that owns the turn.

Why the guard is written as an explicit loop rather than ``try/finally`` around
``yield from``: a generator cannot emit from a ``finally`` that is running
because an exception propagated out of it, and the vendor's own ``run`` is the
thing that pulls the exception.  So the failure is *captured*, the sentinel is
yielded while the vendor loop is still consuming, and the exception is
re-raised afterwards, which keeps the vendor's own error log intact.

Tagging is delegated to the vendor's ``output_for_queue(output, typed_item)``,
so the fallback sentinel carries the same ``cancel_generation`` as real audio and
a superseded turn's terminator lands in the send loop's stale branch instead of
clearing the live turn's discard guard.

Coexistence: while the vendor file still carries the old in-place edit, that edit
clears its ``_terminator_due`` flag as soon as it sees a terminator pass through
``run`` -- which is exactly what this guard yields -- so the two together still
produce one terminator per turn.  The migration can therefore install the
override before restoring the pristine bytes, and the same holds in reverse.
"""
from __future__ import annotations

import logging
from typing import Any, Iterator

from . import UNKNOWN, VendorFix, classify_vendor

logger = logging.getLogger(__name__)

FIX_ID = "FAULT-S2S-TERMINATOR"
_MARKER = f"_vendor_fix_{FIX_ID.lower().replace('-', '_')}"


def _messages() -> tuple[Any, Any]:
    """``(EndOfResponse, AUDIO_RESPONSE_DONE)`` from the vendor's message module.

    Read lazily and by import, not from the module being patched: pristine
    ``baseHandler.py`` does not import ``AUDIO_RESPONSE_DONE`` at all, so a
    hook that read it off the target module would break on the state this fix
    was written for.
    """
    from speech_to_speech.pipeline.messages import AUDIO_RESPONSE_DONE, EndOfResponse

    return EndOfResponse, AUDIO_RESPONSE_DONE


def is_turn_terminator(output: Any, sentinel: bytes) -> bool:
    """True for the audio turn sentinel whether bare or already wrapped.

    ``output_for_queue`` tags raw bytes as ``AudioOutput``, so one sentinel
    reaches the output queue in either shape.
    """
    if isinstance(output, bytes):
        return output == sentinel
    audio = getattr(output, "audio", None)
    return isinstance(audio, bytes) and audio == sentinel


def _terminator_guard(handler: Any, inner_process: Any) -> Any:
    """Wrap a bound ``process`` so an accepted EndOfResponse always terminates."""
    end_of_response, sentinel = _messages()

    def guarded(item: Any) -> Iterator[Any]:
        if not (getattr(handler, "emits_turn_terminator", False) and isinstance(item, end_of_response)):
            yield from inner_process(item)
            return
        due = True
        failure: BaseException | None = None
        stream: Iterator[Any] | None = None
        while True:
            try:
                if stream is None:  # a call-time raise counts as "produced nothing"
                    stream = iter(inner_process(item))
                output = next(stream)
            except StopIteration:
                break
            except Exception as exc:  # noqa: BLE001 - re-raised below, vendor still logs
                failure = exc
                break
            if due and is_turn_terminator(output, sentinel):
                due = False
            yield output
        if due:
            logger.warning(
                "%s: EndOfResponse produced no turn terminator; emitting the fallback "
                "AUDIO_RESPONSE_DONE so the realtime response can finish",
                type(handler).__name__,
            )
            yield sentinel
        if failure is not None:
            raise failure

    setattr(guarded, _MARKER, FIX_ID)
    return guarded


def patch_base_handler(module: Any) -> str:
    """Install the terminator guard on ``module.BaseHandler``.  Idempotent."""
    cls = getattr(module, "BaseHandler", None)
    if cls is None or not hasattr(cls, "run"):
        return "skipped: not the speech_to_speech baseHandler module"
    original_run = cls.run
    if getattr(original_run, _MARKER, None) == FIX_ID:
        return "already applied: BaseHandler.run guard"

    def run(self: Any) -> None:
        # Install per instance, at thread start, over whatever the class (or an
        # instance attribute) currently binds as ``process``.
        current = self.__dict__.get("process", getattr(type(self), "process", None))
        if getattr(current, _MARKER, None) != FIX_ID:
            self.process = _terminator_guard(self, self.process)  # type: ignore[method-assign]
        original_run(self)

    setattr(run, _MARKER, FIX_ID)
    cls.run = run  # type: ignore[method-assign]
    if "emits_turn_terminator" not in cls.__dict__:
        # The role is opt-in by topology; a non-TTS stage must never claim it.
        cls.emits_turn_terminator = False
    return "BaseHandler.run guard + emits_turn_terminator default"


def apply_fix(module: Any) -> str:
    """``providers.bootstrap`` hook body, with the byte-state tripwire prefixed."""
    outcome = patch_base_handler(module)
    state = classify_vendor(
        getattr(module, "__file__", None), FIX.upstream_sha256, FIX.in_place_sha256
    )
    if state == UNKNOWN:
        # Loud, but still installed: this wrapper copies no vendor code, so it is
        # the shape least likely to be broken by an upstream rewrite -- and a
        # refused install would silently bring the P0 symptom back after a
        # version bump.  Drift is a red gate in verify_vendor_fixes.py instead.
        logger.error(
            "%s: %s target hash matches neither upstream nor the recorded in-place edit (%s)",
            FIX_ID, getattr(module, "__name__", "?"), getattr(module, "__file__", ""),
        )
    return f"{outcome}; vendor_state={state}"


FIX = VendorFix(
    fix_id=FIX_ID,
    symptom=(
        "realtime response never finishes after a TTS adapter raises or drops a stale "
        "turn: 'Cannot create response while another response is in progress'"
    ),
    target_module="speech_to_speech.baseHandler",
    # from speech_to_speech-0.2.11.dist-info/RECORD
    upstream_sha256="0bb6481089822bd695b3d9d2324ae1bd34e596faaee8e23d0a5fbde36cfd89d2",
    # the 2026-09-24 hand-edit inside site-packages (backup: patches/baseHandler.py.pre-fault-terminator-20260924)
    in_place_sha256="556539a3de48bb82b3fdc87d6ebbd7242cc5eb4169de7828e7cccd2bff39f3ba",
    impl_file="turn_terminator.py",
    apply=apply_fix,
)

__all__ = ["FIX", "FIX_ID", "apply_fix", "is_turn_terminator", "patch_base_handler"]
