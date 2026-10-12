"""FAULT-S2S-UNIT-LEAK as a project-owned override (任务 P0-1).

The fault
---------
``_release_unit_after_drain`` waits for ``session.drained``, which only the send
loop sets when ``SESSION_END`` travels the whole handler chain.  The vendor's
loop has no exit condition: if a handler thread died (an adapter whose
``setup()`` raised) or is stuck in a long HTTP read, ``SESSION_END`` never
arrives, the function never returns, and ``unit.session`` is never cleared.  With
the production ``--num_pipelines 1`` that is not a degraded pool, it is a dead
service -- every later client is rejected with "all 1 session slots are in use",
which is the other half of "the girlfriend stopped responding".

How the override is built
-------------------------
The vendor's own coroutine is run as a task under ``asyncio.wait(timeout=...)``.
Inside that task the vendor polls ``session.drained`` every 0.05 s and, once the
flag is set, falls out of its loop and performs *its own* release tail
(``service.unregister`` -> ``unit.session = None`` -> the "released" log).  So
this module contains no copied release statement on the normal path: exceeding
the window merely sets the flag the vendor is waiting on.  Two things are cleared
first, matching the old in-place edit:

* ``session.pending_output_item`` -- the send loop's scratch buffer, so a forced
  release cannot hand a chunk of the previous session to whoever claims the unit
  next;
* ``session.drained`` -- the exit signal itself.

The window is read from the *target module's* global on every call, so both
``SESSION_END_FORCE_RELEASE_S`` (installed here when absent) and the vendor's
``SESSION_END_DRAIN_TIMEOUT_S`` stay monkeypatchable; a test that shrinks them
does not have to know this wrapper exists.

If the session carries no signalisable ``drained`` event, the vendor loop is
structurally incapable of exiting.  That cannot happen against the shipped
``PipelineUnit``/``SessionState`` contract, but the fallback branch handles it
without hanging: the task is cancelled and the unit is released here, because a
leaked unit is the failure this fix exists to remove.

任务 AI_GIRLFRIEND_RUNTIME_LIFECYCLE_FINAL_REPAIR_01 §十二 closes the two ways the
window above could still end with the unit claimed, which is the same fault under
a different name:

* the forced ``drained`` signal was set but the vendor's own release tail never
  ran (its task was cancelled along with the request, or its host task died) --
  the tail now gets ``_TAIL_GRACE_S`` and, when that expires, the queue flush plus
  ``service.unregister`` plus ``unit.session = None`` all happen *here*, logged as
  ``reason=drain_timeout``;
* the awaiting caller was cancelled by Starlette right after a disconnect -- a
  detached watcher takes the task over and enforces the same deadline, so the
  release no longer depends on whoever was awaiting it.

Both paths, and the normal one, are idempotent: a defensive release only touches
the unit while it still belongs to the session being released, so a late replay
of an old release can never evict the client that claimed the unit meanwhile.
``/v1/pool`` must therefore reach ``in_use = 0`` after the browser closes, without
restarting :8765.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import UNKNOWN, VendorFix, classify_vendor

logger = logging.getLogger(__name__)

FIX_ID = "FAULT-S2S-UNIT-LEAK"
_MARKER = f"_vendor_fix_{FIX_ID.lower().replace('-', '_')}"

#: Ceiling on how long a unit may stay unavailable *after* the vendor's own
#: "SESSION_END not drained" warning.  Bounded loss of one queue flush beats an
#: unrecoverable pool.
SESSION_END_FORCE_RELEASE_S = 30.0

#: How long to wait for the vendor's release tail once ``drained`` was forced.
#: The vendor loop polls at 0.05 s, so this is many times the needed interval.
_TAIL_GRACE_S = 5.0


def _consume_result(task: "asyncio.Future[Any]") -> None:
    """Retrieve a task's outcome so asyncio does not log 'never retrieved'."""
    try:
        if not task.cancelled():
            task.exception()
    except Exception:  # pragma: no cover - defensive around half-torn-down tasks
        logger.debug("%s: suppressed outcome retrieval error", FIX_ID, exc_info=True)


def _force_release(session: Any, unit: Any, session_id: str, timeout: float) -> bool:
    """Signal the vendor loop so its own release tail runs.

    Returns True when the vendor loop can finish, False when the session has no
    signal to set (contract drift) and the caller must release defensively.
    """
    logger.error(
        "Pipeline %s: SESSION_END still not drained after %.1fs - force-releasing the "
        "unit (session %s); a dead handler thread is the usual cause",
        getattr(unit, "index", "?"), timeout, session_id,
    )
    try:
        # Drop the send loop's scratch for this session before releasing, so a
        # force-release cannot hand a buffered chunk of the old session to the
        # next one.
        session.pending_output_item = None
    except Exception:  # pragma: no cover - attribute may be gone after teardown
        logger.debug("%s: pending_output_item could not be cleared", FIX_ID, exc_info=True)
    event = getattr(session, "drained", None)
    setter = getattr(event, "set", None)
    if callable(setter):
        setter()
        return True
    return False


def _release_defensively(unit: Any, session_id: str, session: Any = None) -> bool:
    """Only reachable if the vendor loop cannot finish its own release tail.

    Guarded on ownership: if ``session`` is given and the unit already belongs to
    someone else, this release must not run.  ``_release_unit_after_drain`` is
    spawned as a detached task, so a late instance of it may fire *after* a new
    client claimed the unit; blindly clearing ``unit.session`` there would evict
    the live session and re-create the fault under a different name.

    Returns True when this call actually returned the unit to the pool.
    """
    if session is not None and getattr(unit, "session", None) is not session:
        logger.warning(
            "%s: skipping defensive release of session %s - the unit now belongs to %r",
            FIX_ID, session_id, getattr(unit.session, "session_id", unit.session),
        )
        return False
    service = getattr(unit, "service", None)
    unregister = getattr(service, "unregister", None)
    if callable(unregister):
        try:
            unregister(session_id)
        except Exception:  # noqa: BLE001 - the release below is what matters
            logger.exception("%s: service.unregister failed during defensive release", FIX_ID)
    unit.session = None
    return True


def _watch_tail(unit: Any, session: Any, session_id: str, task: "asyncio.Future[Any]",
                grace: float, cleaner: Any = None) -> None:
    """Finish a release whose awaiting caller is gone, or force it within `grace`.

    The route handler's ``finally`` can be cancelled by Starlette right after a
    disconnect.  Cancelling the vendor task there would re-create the leak, so the
    task is handed to this detached watcher instead: it lets the vendor's own tail
    run, and if that never happens the unit still returns to the pool on time.
    """
    async def guard() -> None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=grace)
            return
        except asyncio.CancelledError:
            # ``shield`` keeps the task alive; this guard itself was cancelled
            # because the loop is going down, so nothing is left to protect.
            if not task.done():
                task.cancel()
            return
        except (TimeoutError, asyncio.TimeoutError):
            pass
        _force_finish(unit, session, session_id, task, grace, "cancelled_caller", cleaner)

    try:
        asyncio.get_running_loop().create_task(guard())
    except RuntimeError:  # pragma: no cover - no loop left to schedule on
        logger.warning("%s: no loop to watch the release tail of session %s", FIX_ID, session_id)


def _force_finish(unit: Any, session: Any, session_id: str, task: "asyncio.Future[Any]",
                  grace: float, stage: str, cleaner: Any = None) -> bool:
    """Cancel a release that cannot complete and return the unit to the pool."""
    if not task.done():
        task.cancel()
    _consume_result(task)
    if callable(cleaner):
        # §十二 要求"取消 + 清队列 + 强制释放"是一步：四个 pipeline 队列里还压着
        # 死会话的输出时，把 unit 交回池子等于把上一段声音放给下一个客户端。
        try:
            cleaner(unit)
        except Exception:  # noqa: BLE001 - a broken flush must not block the release
            logger.exception("%s: queue flush failed while force-releasing %s", FIX_ID, session_id)
    released = _release_defensively(unit, session_id, session)
    logger.error(
        "%s: session %s never completed its own release tail (%s after %.1fs) - "
        "released the unit here; reason=drain_timeout, unit_cleared=%s",
        FIX_ID, session_id, stage, grace, released,
    )
    return released


def patch_websocket_router(module: Any) -> str:
    """Bound ``module._release_unit_after_drain`` with a force-release window."""
    original = getattr(module, "_release_unit_after_drain", None)
    if original is None or not callable(original):
        return "skipped: not the openai_realtime websocket_router module"
    if getattr(module, "SESSION_END_FORCE_RELEASE_S", None) is None:
        module.SESSION_END_FORCE_RELEASE_S = SESSION_END_FORCE_RELEASE_S
    if getattr(original, _MARKER, None) == FIX_ID:
        return "already applied: _release_unit_after_drain window"

    async def released(unit: Any, session: Any, session_id: str) -> None:
        timeout = float(getattr(module, "SESSION_END_FORCE_RELEASE_S", SESSION_END_FORCE_RELEASE_S))
        # The vendor's own "make this unit safe for the next claimant" routine, if
        # the wheel has one.  Force-releasing without it would hand a buffered
        # chunk of the dead session to whoever connects next.
        cleaner = getattr(module, "_clean_unit", None)
        task = asyncio.ensure_future(original(unit, session, session_id))
        pending: BaseException | None = None
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError as exc:
            # The router spawns this as a detached task precisely because its own
            # await can be cancelled by Starlette after a disconnect.  Cancelling
            # ``task`` there would re-create the leak, so a detached watcher takes
            # over: the vendor tail still gets its chance, and the unit returns to
            # the pool either way (§十二: /v1/pool must fall back to in_use=0).
            done, pending = set(), exc
            _watch_tail(unit, session, session_id, task, timeout + _TAIL_GRACE_S, cleaner)
        finally:
            if pending is not None:
                task.add_done_callback(_consume_result)
        if task in done:
            task.result()  # propagate a real vendor failure the same way a direct call would
            return
        if not _force_release(session, unit, session_id, timeout):
            # No signal the vendor loop polls: it can never exit on its own.
            _force_finish(unit, session, session_id, task, timeout, "no_drain_signal", cleaner)
        else:
            # Give the vendor's own release tail its poll interval plus slack, then
            # finish the job here.  §十二 forbids ending in "warned and left the unit
            # claimed": with --num_pipelines 1 that is the whole service.
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=_TAIL_GRACE_S)
            except (TimeoutError, asyncio.TimeoutError):
                _force_finish(unit, session, session_id, task, _TAIL_GRACE_S, "drain_tail", cleaner)
            except asyncio.CancelledError:
                _watch_tail(unit, session, session_id, task, _TAIL_GRACE_S, cleaner)
                raise
        if pending is not None:
            raise pending

    setattr(released, _MARKER, FIX_ID)
    module._release_unit_after_drain = released
    return "_release_unit_after_drain force-release window"


def apply_fix(module: Any) -> str:
    """``providers.bootstrap`` hook body, with the byte-state tripwire prefixed."""
    outcome = patch_websocket_router(module)
    state = classify_vendor(
        getattr(module, "__file__", None), FIX.upstream_sha256, FIX.in_place_sha256
    )
    if state == UNKNOWN:
        logger.error(
            "%s: %s target hash matches neither upstream nor the recorded in-place edit (%s)",
            FIX_ID, getattr(module, "__name__", "?"), getattr(module, "__file__", ""),
        )
    return f"{outcome}; vendor_state={state}"


FIX = VendorFix(
    fix_id=FIX_ID,
    symptom=(
        "one wedged or dead handler thread holds the only pipeline unit forever: "
        "'all 1 session slots are in use' for every later client"
    ),
    target_module="speech_to_speech.api.openai_realtime.websocket_router",
    # from speech_to_speech-0.2.11.dist-info/RECORD
    upstream_sha256="dd4657a3ce6d9b415f26ff729aff75108fdd2cacaaaef3f82e1e6fe93a28bd70",
    # the 2026-09-24 hand-edit inside site-packages (backup: patches/websocket_router.py.pre-fault-terminator-20260924)
    in_place_sha256="fd0378aab37f362525bd55d63e142f918594e0b52db9bd21b6a6d16f983ba361",
    impl_file="unit_drain_release.py",
    apply=apply_fix,
)

__all__ = ["FIX", "FIX_ID", "SESSION_END_FORCE_RELEASE_S", "apply_fix", "patch_websocket_router"]
