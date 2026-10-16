"""Fault triage for the P0 "the girlfriend stopped responding" class (任务 §7 G/H).

Read this file as the ISSUE evidence for ``FAULT-S2S-TERMINATOR`` and
``FAULT-S2S-UNIT-LEAK`` in the §31 report.  Everything here is in-process and
offline: no server is contacted, no model is loaded, no TTS/LLM generation runs.

The contract under test
-----------------------
``AUDIO_RESPONSE_DONE`` is the *only* thing on the audio path that closes a
realtime response.  ``_send_loop_for`` (websocket_router.py:524-542) is the sole
consumer: it calls ``service.finish_response()``, clears ``response_playing`` and
re-arms ``should_listen``.  ``finish_response`` (handlers/response.py:206-259) is
gated on ``st.in_response``, so if the sentinel never arrives the response stays
open forever and every later ``response.create`` is refused with
"Cannot create response while another response is in progress."

Today that sentinel is emitted by exactly one hand-written branch inside each TTS
adapter (the ``EndOfResponse`` case).  Nothing in the framework guarantees it
runs, and three shipped code paths already defeat it:

  1. ``baseHandler.run()``:136-137 swallows every exception raised by
     ``process()`` and puts *nothing* on the output queue as compensation;
  2. ``qwen3_tts_handler.py``:696-701 returns early for a stale turn and drops
     the sentinel;
  3. ``providers/adapters/fake_tts_adapter.py``:82-86 does the same on purpose,
     which is how the project's own contract fixture encodes the bug.

The fix is deliberately provider-neutral: the *role* "this handler owns the audio
turn terminator" is a pipeline-topology fact, so ``s2s_pipeline.get_tts_handler``
marks whichever adapter the registry resolved, and ``BaseHandler.run()`` honors
the mark by guaranteeing exactly one terminator per ``EndOfResponse``.  A new TTS
provider therefore still needs zero edits outside its own adapter.

Run::

    runtime/core/venv/scripts/python.exe runtime/tests/test_fault_turn_terminator.py
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from threading import Event
from types import SimpleNamespace

import numpy as np

RUNTIME = Path(__file__).resolve().parents[1]
for _p in (str(RUNTIME / "core"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from speech_to_speech.baseHandler import BaseHandler  # noqa: E402
from speech_to_speech.pipeline.messages import (  # noqa: E402
    AUDIO_RESPONSE_DONE,
    EndOfResponse,
    PIPELINE_END,
    TTSInput,
)

BLOCKSIZE = 512


def _payload(item):
    """Unwrap what BaseHandler.output_for_queue may have tagged as AudioOutput."""
    return item.audio if hasattr(item, "audio") else item


def is_terminator(item) -> bool:
    payload = _payload(item)
    return isinstance(payload, bytes) and payload == AUDIO_RESPONSE_DONE


class _StubTTS(BaseHandler):
    """A minimal stand-in for any TTS adapter: audio out, one terminator per turn.

    ``mode`` picks which of the shipped failure shapes to reproduce, so each test
    names the real code path it is standing in for.  The role mark is set on this
    fixture exactly the way ``s2s_pipeline.get_tts_handler`` sets it on whichever
    adapter the registry resolved in production.
    """

    emits_turn_terminator = True

    def setup(self, mode: str = "ok", should_listen=None, **_kw) -> None:
        self.mode = mode
        self.should_listen = should_listen or Event()
        self.cancel_scope = None
        self.speculative_turns = None

    def process(self, item):
        if isinstance(item, EndOfResponse):
            if self.mode == "raises_on_end":
                # baseHandler.run():136-137 shape — e.g. FishTTSHandler
                # "Fish returned empty audio" / httpx raise_for_status().
                raise RuntimeError("synth worker failed while closing the turn")
            if self.mode == "drops_on_stale":
                # qwen3_tts_handler.py:696-701 and fake_tts_adapter.py:82-86.
                return
            yield AUDIO_RESPONSE_DONE
            return
        if self.mode == "raises_on_text":
            raise RuntimeError("synth worker failed mid-speech")
        yield np.zeros(BLOCKSIZE, dtype=np.int16)


def _drive(handler_cls, *, mode, items, timeout=6.0):
    """Run a real BaseHandler.run() loop in a thread; return its output queue."""
    stop_event = Event()
    queue_in: Queue = Queue()
    queue_out: Queue = Queue()
    handler = handler_cls(stop_event, queue_in, queue_out, (), {"mode": mode})
    thread = threading.Thread(target=handler.run, daemon=True)
    thread.start()
    collected: list = []
    try:
        for item in items:
            queue_in.put(item)
        deadline = time.monotonic() + timeout
        saw_end = False
        while time.monotonic() < deadline:
            try:
                got = queue_out.get(timeout=0.1)
            except Empty:
                continue
            collected.append(got)
            if isinstance(got, EndOfResponse):
                continue
            if _payload(got) is None:
                continue
            # Once the terminator for the EndOfResponse we fed in has landed, the
            # contract is satisfied; stop early so the test stays fast.
            if is_terminator(got):
                saw_end = True
                break
        if not saw_end:
            # Give a handler that intends to stay silent a moment to prove it.
            time.sleep(0.3)
            while True:
                try:
                    collected.append(queue_out.get_nowait())
                except Empty:
                    break
    finally:
        stop_event.set()
        queue_in.put(PIPELINE_END)
        thread.join(timeout=5.0)
    return collected


def _terminators(outputs) -> int:
    return sum(1 for item in outputs if is_terminator(item))


# ── FAULT-S2S-TERMINATOR ─────────────────────────────────────────────


def tts_exception_still_terminates_the_turn():
    """A TTS adapter that raises while closing a turn must not leave it open."""
    outputs = _drive(_StubTTS, mode="raises_on_end", items=[EndOfResponse(turn_id="turn_1")])
    assert _terminators(outputs) == 1, (
        "AUDIO_RESPONSE_DONE was lost to BaseHandler.run()'s blanket except; "
        f"the realtime response would never finish. Got {outputs!r}"
    )


def stale_turn_dropped_sentinel_is_replaced():
    """An adapter that swallows the EndOfResponse (qwen3:696-701) still terminates."""
    outputs = _drive(_StubTTS, mode="drops_on_stale", items=[EndOfResponse(turn_id="turn_1")])
    assert _terminators(outputs) == 1, (
        "a stale-turn early return dropped the terminator, so CancelScope.discarding "
        f"would linger (websocket_router.py:212-214). Got {outputs!r}"
    )


def mid_speech_exception_still_terminates_the_turn():
    """Failure on the text item is fine on its own; the EndOfResponse that follows
    must still close the turn exactly once."""
    outputs = _drive(
        _StubTTS,
        mode="raises_on_text",
        items=[TTSInput(text="你好呀。", turn_id="turn_1"), EndOfResponse(turn_id="turn_1")],
    )
    assert _terminators(outputs) == 1, f"expected exactly one terminator, got {outputs!r}"


def healthy_turn_emits_exactly_one_terminator():
    """The guarantee must not double-send: two per turn breaks the send loop."""
    outputs = _drive(
        _StubTTS,
        mode="ok",
        items=[
            TTSInput(text="第一句。", turn_id="turn_1"),
            TTSInput(text="第二句。", turn_id="turn_1"),
            EndOfResponse(turn_id="turn_1"),
        ],
    )
    assert _terminators(outputs) == 1, f"expected exactly one terminator, got {_terminators(outputs)}"
    audio = [i for i in outputs if not is_terminator(i)]
    assert audio, "a healthy turn must still carry audio"


def terminator_carries_the_cancel_generation():
    """The fallback sentinel must be generation-tagged like real audio, or the send
    loop's stale branch (websocket_router.py:526) cannot tell whose turn closed."""
    outputs = _drive(
        _StubTTS,
        mode="drops_on_stale",
        items=[EndOfResponse(turn_id="turn_1", turn_revision=2, cancel_generation=7)],
    )
    assert _terminators(outputs) == 1, outputs
    tagged = [i for i in outputs if is_terminator(i)][0]
    assert getattr(tagged, "cancel_generation", None) == 7, (
        f"terminator reached the queue untagged ({tagged!r}); a superseded turn would "
        "clear the discard guard of the *live* generation"
    )


def non_tts_handler_is_not_given_a_terminator():
    """AUDIO_RESPONSE_DONE may only come from a TTS handler: the guarantee must stay
    scoped to the role, not to every stage in the chain."""

    class _StubLM(_StubTTS):
        emits_turn_terminator = False

    outputs = _drive(_StubLM, mode="drops_on_stale", items=[EndOfResponse(turn_id="turn_1")])
    assert _terminators(outputs) == 0, (
        f"a non-TTS stage emitted the audio terminator: {outputs!r}"
    )


def tts_slot_is_marked_by_pipeline_assembly():
    """The mark must come from topology, so a brand-new provider needs no edit.

    ``providers.tts_factory.build`` is patched so no adapter is constructed and
    no manifest ``enabled`` gate applies — this asserts only that the single TTS
    slot of the pipeline is the one that owns the terminator.
    """
    import providers.tts_factory as tts_factory
    from speech_to_speech.s2s_pipeline import get_tts_handler

    stub = SimpleNamespace(emits_turn_terminator=False, name="stub-tts")
    seen: dict = {}

    def fake_build(flag, *args, **kwargs):
        seen["flag"] = flag
        return stub

    original = tts_factory.build
    tts_factory.build = fake_build
    try:
        module_kwargs = SimpleNamespace(tts="test_fake")
        out = get_tts_handler(
            module_kwargs,
            Event(),
            Queue(),
            Queue(),
            Event(),
            None,
            None,
            None,
            None,
            None,
            cancel_scope=None,
            speculative_turns=None,
        )
    finally:
        tts_factory.build = original
    assert out is stub, "get_tts_handler must return what the registry built"
    assert seen["flag"] == "test_fake"
    assert getattr(stub, "emits_turn_terminator", False) is True, (
        "get_tts_handler did not mark the TTS slot, so BaseHandler's terminator "
        "guarantee would be dead code for every real provider"
    )


def real_project_adapters_inherit_the_guarantee():
    """The shipped adapters need no edit: they inherit the role hook from BaseHandler,
    and the pipeline marks them.  (Class default stays False so a non-TTS stage can
    never claim the terminator by accident.)"""
    from s2s_business.fish_tts_handler import FishTTSHandler
    from s2s_business.higgs_tts_handler import HiggsTTSHandler
    from speech_to_speech.TTS.qwen3_tts_handler import Qwen3TTSHandler

    assert BaseHandler.emits_turn_terminator is False, "the role must be opt-in by topology"
    for cls in (Qwen3TTSHandler, HiggsTTSHandler, FishTTSHandler):
        assert issubclass(cls, BaseHandler), cls
        assert cls.emits_turn_terminator is False, (
            f"{cls.__name__} hard-codes the TTS role; it must come from "
            "s2s_pipeline.get_tts_handler so every provider is treated alike"
        )


# ── FAULT-S2S-UNIT-LEAK ──────────────────────────────────────────────


def _make_unit():
    import speech_to_speech.api.openai_realtime.websocket_router as wr

    released: list = []

    class _Service:
        def unregister(self, session_id):
            released.append(session_id)

    unit = SimpleNamespace(index=0, session=None, service=_Service())

    class _Session:
        drained = asyncio.Event()
        released_at = 0.0
        pending_output_item = b"stale-audio"

    return wr, unit, _Session(), released


def stuck_unit_is_force_released_within_a_bounded_window():
    """With --num_pipelines 1 a unit held forever means the girlfriend is dead for
    the next caller.  A SESSION_END that never drains must therefore be survivable.
    """
    wr, unit, session, released = _make_unit()
    original_timeout = wr.SESSION_END_DRAIN_TIMEOUT_S
    original_force = getattr(wr, "SESSION_END_FORCE_RELEASE_S", None)
    # Keep the wall-clock cost of the test independent of the shipped values.
    wr.SESSION_END_DRAIN_TIMEOUT_S = 0.1
    if original_force is not None:
        wr.SESSION_END_FORCE_RELEASE_S = 0.4

    async def scenario():
        await asyncio.wait_for(
            wr._release_unit_after_drain(unit, session, "sess-1"), timeout=6.0
        )

    started = time.monotonic()
    try:
        asyncio.run(scenario())
    except asyncio.TimeoutError:
        raise AssertionError(
            "_release_unit_after_drain never returns while SESSION_END is undrained: "
            "the only pipeline unit stays unavailable forever (num_pipelines=1)."
        )
    finally:
        wr.SESSION_END_DRAIN_TIMEOUT_S = original_timeout
        if original_force is not None:
            wr.SESSION_END_FORCE_RELEASE_S = original_force
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"force release took {elapsed:.1f}s — the fallback window is too long"
    assert unit.session is None, "unit was not released"
    assert released == ["sess-1"], f"service.unregister was not called: {released}"


def drained_unit_is_released_normally():
    """Control: the happy path must keep working and must not wait for the fallback."""
    wr, unit, session, released = _make_unit()
    original_timeout = wr.SESSION_END_DRAIN_TIMEOUT_S
    wr.SESSION_END_DRAIN_TIMEOUT_S = 30.0

    async def scenario():
        async def setter():
            await asyncio.sleep(0.15)
            session.drained.set()

        task = asyncio.create_task(setter())
        await asyncio.wait_for(wr._release_unit_after_drain(unit, session, "sess-2"), timeout=5.0)
        await task

    try:
        asyncio.run(scenario())
    finally:
        wr.SESSION_END_DRAIN_TIMEOUT_S = original_timeout
    assert unit.session is None
    assert released == ["sess-2"]


TESTS = [
    tts_exception_still_terminates_the_turn,
    stale_turn_dropped_sentinel_is_replaced,
    mid_speech_exception_still_terminates_the_turn,
    healthy_turn_emits_exactly_one_terminator,
    terminator_carries_the_cancel_generation,
    non_tts_handler_is_not_given_a_terminator,
    tts_slot_is_marked_by_pipeline_assembly,
    real_project_adapters_inherit_the_guarantee,
    stuck_unit_is_force_released_within_a_bounded_window,
    drained_unit_is_released_normally,
]

if __name__ == "__main__":
    failures = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"  ok   {test.__name__}")
    if failures:
        print(f"fault turn-terminator checks: FAIL ({failures} of {len(TESTS)})")
        raise SystemExit(1)
    print(f"fault turn-terminator checks: PASS ({len(TESTS)})")
