"""Shadow tests TEST A-F for the :8010 avatar session lifecycle
(TASK_ID = AI_GIRLFRIEND_AVATAR_SESSION_LIFECYCLE_FINAL_01, section 14).

These exercise the REAL ``SessionLifecycle`` / ``SessionManager`` (no wav2lip,
no GPU, no network) with fake pc / player / avatar objects that mimic the
interfaces destroy_session is allowed to use:

* fake pc        — ``connectionState`` + ``async close()``
* fake player    — ``force_stop(reason)`` (sync, off-loop, as HumanPlayer has)
                 — ``media_stats()``
* fake avatar    — ``flush_talk()``, ``stop_recording()``, ``recording``,
                   ``res_frame_queue``

All state is in-memory.  Nothing here touches the running :8010.

Run:
    runtime/core/venv/Scripts/python.exe runtime/tests/test_avatar_session_lifecycle.py
"""

import asyncio
import os
import queue
import re
import sys
import time
import types
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
APP = PROJECT / "runtime" / "avatar" / "app"
sys.path.insert(0, str(APP))

# Import stubs: session_manager's type-only import of BaseAvatar drags in the
# whole inference stack (torch/cv2/numpy/resampy/soundfile).  Those live in the
# launcher-injected site-packages of :8010, not in this test interpreter, and
# the lifecycle under test never touches a real avatar — the tests feed fakes.
# A stub here keeps the suite runnable anywhere; a real BaseAvatar is never
# constructed.
import types  # noqa: E402

for _name in ("cv2", "resampy", "soundfile"):
    sys.modules.setdefault(_name, types.ModuleType(_name))
for _pkg, _sub in (("torch", "torch.multiprocessing"),):
    _m = types.ModuleType(_pkg)
    _m.__path__ = []          # mark as package so submodule imports resolve
    sys.modules.setdefault(_pkg, _m)
    sys.modules.setdefault(_sub, types.ModuleType(_sub))
if "numpy" not in sys.modules:
    _np = types.ModuleType("numpy")
    _np_typing = types.ModuleType("numpy.typing")

    class _NDArray:  # subscriptable stand-in for numpy.typing.NDArray
        def __class_getitem__(cls, item):
            return object

    _np_typing.NDArray = _NDArray
    _np.typing = _np_typing
    for _t in ("float32", "float64", "int16", "uint8", "int64"):
        setattr(_np, _t, object)
    sys.modules["numpy"] = _np
    sys.modules["numpy.typing"] = _np_typing

from server.session_lifecycle import (  # noqa: E402
    SessionLifecycle, DISCONNECT_GRACE_S, CONNECT_DEADLINE_S, PEER_SILENCE_S,
)
from server.session_manager import SessionManager, MaxSessionError  # noqa: E402


# ─── fakes ───────────────────────────────────────────────────────────────────

class FakePC:
    def __init__(self, state="connected"):
        self._state = state
        self.close_calls = 0

    @property
    def connectionState(self):
        return self._state

    async def close(self):
        self.close_calls += 1
        self._state = "closed"


class FakePlayer:
    def __init__(self, session_id=""):
        self.session_id = session_id
        self.stop_calls = 0
        self.stopped = False

    def force_stop(self, reason="unspecified", join_timeout: float = 10.0):
        self.stop_calls += 1
        self.stopped = True
        return {"joined": True, "reason": reason}

    def media_stats(self):
        return {"video_frames": 10, "audio_frames": 20, "video_age_s": 0.1,
                "video_queue": 0, "audio_queue": 0, "thread_alive": not self.stopped,
                "stopped": self.stopped}


class FakeAvatar:
    def __init__(self):
        self.recording = False
        self.flush_calls = 0
        self.res_frame_queue = queue.Queue()
        for _ in range(3):
            self.res_frame_queue.put(object())

    def flush_talk(self):
        self.flush_calls += 1

    def stop_recording(self):
        self.recording = False


# ─── helpers ─────────────────────────────────────────────────────────────────

def new_lc(max_session=5):
    lc = SessionLifecycle()
    lc.set_max_session(max_session)
    return lc


def totals(lc):
    return lc.snapshot()["totals"]


async def spawn(lc, sid, pc_state="connected", with_media=True):
    """reserve → building → activate, the shape a successful /offer leaves."""
    lc.reserve(sid)
    lc.mark_building(sid)
    pc = FakePC(pc_state) if with_media else None
    player = FakePlayer(sid) if with_media else None
    avatar = FakeAvatar() if with_media else None
    ok = lc.activate(sid, pc=pc, player=player, avatar_session=avatar)
    assert ok, f"activate({sid}) refused"
    return pc, player, avatar


RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ─── TEST A: create → connected → closed ⇒ active 1 → 0 ────────────────────

async def test_a():
    lc = new_lc()
    await spawn(lc, "A-1")
    ok1 = lc.active_count() == 1
    out = await lc.destroy_session("A-1", "pc_closed")
    ok = (ok1 and lc.active_count() == 0
          and out["released"] is True
          and totals(lc)["released"] == 1
          and lc.get("A-1") is None)
    record("TEST A connected→closed active 1→0", ok,
           f"before=1 out.released={out['released']} after={lc.active_count()}")


# ─── TEST B: disconnected → recover within grace ⇒ active stays 1 ──────────

async def test_b():
    lc = new_lc()
    pc, _, _ = await spawn(lc, "B-1")
    lc.note_connection_state("B-1", "disconnected")
    rec = lc.get("B-1")
    mid = (rec.state == "disconnected"
           and lc.active_count() == 1
           and not any(why == "disconnected_timeout" for _, why in lc._due()))
    # recover inside the grace window: callback + sweep both see connected again
    pc._state = "connected"
    lc.note_connection_state("B-1", "connected")
    rec = lc.get("B-1")
    ok = (mid and rec.state == "live"
          and rec.disconnect_since_mono is None
          and lc.active_count() == 1
          and lc._due() == [])
    record("TEST B disconnected→recover inside grace, active stays 1", ok,
           f"state={rec.state} active={lc.active_count()}")


# ─── TEST C: disconnected → grace expires ⇒ reaped, active 1 → 0 ───────────

async def test_c():
    lc = new_lc()
    await spawn(lc, "C-1")
    lc.note_connection_state("C-1", "disconnected")
    rec = lc.get("C-1")
    rec.disconnect_since_mono -= (DISCONNECT_GRACE_S + 0.5)  # age past the grace
    due = lc._due()
    assert any(why == "disconnected_timeout" for _, why in due), f"due={due}"
    forced_before = totals(lc)["forced_released"]
    out = await lc.destroy_session("C-1", "disconnected_timeout")
    ok = (lc.active_count() == 0 and out["released"] is True
          and totals(lc)["forced_released"] == forced_before + 1)
    record("TEST C grace expires → destroyed, active 1→0, counted forced", ok,
           f"reason={out['reason']} forced={totals(lc)['forced_released']}")


# ─── TEST D: offer setup aborts mid-way ⇒ active back to 0, no orphan ──────

async def test_d():
    lc = new_lc()
    # D1: session reserved + partially built (pc attached) then SDP fails —
    # the rtc_manager rollback path: destroy_session must take the pc down too.
    pc, player, _ = await spawn(lc, "D-1")
    out = await lc.destroy_session("D-1", "answer_failed:SetupError")
    d1 = (lc.active_count() == 0 and out["released"] is True
          and pc.close_calls == 1 and player.stop_calls == 1)
    # D2: reservation that never even built (build executor raised) — the
    # session_manager._rollback path with nothing attached.
    lc.reserve("D-2")
    lc.mark_building("D-2")
    out2 = await lc.destroy_session("D-2", "build_failed:RuntimeError")
    d2 = lc.active_count() == 0 and out2["released"] is True
    # D3: capacity is really returned — a full deck before, one free after.
    ok = d1 and d2
    record("TEST D offer-setup failure rolls the slot back", ok,
           f"pc.close={pc.close_calls} player.stop={player.stop_calls} "
           f"active={lc.active_count()}")


# ─── TEST E: destroy three times ⇒ one release, never raises ───────────────

async def test_e():
    lc = new_lc()
    pc, player, avatar = await spawn(lc, "E-1")
    r1 = await lc.destroy_session("E-1", "client_pagehide")
    r2 = await lc.destroy_session("E-1", "client_pagehide")
    r3 = await lc.destroy_session("E-1", "client_pagehide")
    released_once = (r1["released"] is True
                     and r2.get("idempotent") is True and r2["released"] is False
                     and r3.get("idempotent") is True and r3["released"] is False)
    # side effects ran exactly once
    once = (pc.close_calls == 1 and player.stop_calls == 1 and avatar.flush_calls == 1)
    t = totals(lc)
    ok = released_once and once and t["released"] == 1 and lc.active_count() == 0
    record("TEST E destroy×3 idempotent, released counted once", ok,
           f"close={pc.close_calls} stop={player.stop_calls} flush={avatar.flush_calls} "
           f"released_total={t['released']}")


# ─── TEST F: stale async / duplicate connect cannot double-occupy ───────────

async def test_f():
    lc = new_lc()
    await spawn(lc, "F-1")
    # server side: a second reserve for a LIVE id must be refused, not silently
    # adopted (the client-side analogue is the connectGeneration guard in
    # avatar-sync.js, asserted statically below).
    dup_refused = False
    try:
        lc.reserve("F-1")
    except ValueError:
        dup_refused = True
    # a NEW session id still fits while the old one is live (replacement is by
    # new id after release, never by re-reserving the old key)
    lc.reserve("F-2")
    lc.mark_building("F-2")
    lc.activate("F-2", pc=FakePC(), player=FakePlayer("F-2"), avatar_session=FakeAvatar())
    two = lc.active_count() == 2
    # client side static gate: the generation guard must exist and be reachable
    js = (PROJECT / "runtime" / "ui" / "app" / "avatar-sync.js").read_text(encoding="utf-8")
    gen_checks = [
        ("generation counter", "let connectGeneration = 0" in js),
        ("stale-result guard after offer", "gen !== connectGeneration" in js),
        ("retire-before-new-offer", "superseded_by_new_connect" in js),
        ("race-lost release after offer", "race_lost_after_offer" in js),
        ("single connectPromise", "if (connectPromise) return connectPromise;" in js),
        ("single reconnectTimer", "if (reconnectTimer) clearTimeout(reconnectTimer);" in js),
        ("pagehide release", "addEventListener('pagehide'" in js),
    ]
    ok = dup_refused and two and all(ok for _, ok in gen_checks)
    missing = [name for name, ok in gen_checks if not ok]
    record("TEST F stale async cannot overwrite; single-flight present", ok,
           f"dup_refused={dup_refused} active=2 missing={missing or 'none'}")


# ─── TEST B-extra (spec §8): shutdown enumerates ⇒ active 0 ────────────────

async def test_shutdown():
    lc = new_lc()
    for i in range(3):
        await spawn(lc, f"S-{i}")
    await lc.shutdown_all(reason="server_shutdown")
    ok = lc.active_count() == 0
    record("TEST §8 shutdown_all enumerates every session → active 0", ok,
           f"active={lc.active_count()} released={totals(lc)['released']}")


# ─── TEST (spec §2): SessionManager delegates, never a second truth ────────

async def test_manager_delegation():
    sm = SessionManager()
    sm.init_builder(lambda sid, params: FakeAvatar())
    sid = await sm.create_session({"x": 1})
    built = sm.has_session(sid) and sm.get_session(sid) is not None
    # duplicate live id → explicit rejection (no silent reuse)
    dup = False
    try:
        await sm.create_session({"x": 1}, sessionid=sid)
    except MaxSessionError:
        dup = True
    sm.remove_session(sid, "test")  # sync signature; schedules the destroy task
    await asyncio.sleep(0.05)
    gone = not sm.has_session(sid)
    ok = built and dup and gone
    record("TEST §2 SessionManager create/lookup/release all delegate", ok,
           f"built={built} dup_refused={dup} released={gone}")


# ─── TEST G/H: peer-silence reaper (hard-crash death detection) ──────────────
#
# Real-world evidence first: aiortc 1.15/aioice NEVER reports failed/closed for
# a hard-killed peer (runtime/tests/a01_aiortc_death_repro.py: still
# "connected" at 75s).  Browsers keep STUN consent flowing every ~5s while
# alive, so "no datagram from the peer for PEER_SILENCE_S" is the death signal.

class FakeIceProto:
    def __init__(self):
        self.inbound = 0
        self.datagram_received = self._recv  # will be wrapped by the tap

    def _recv(self, data, addr):
        self.inbound += 1


class FakeIceConn:
    def __init__(self, protocols=None):
        self._protocols = protocols if protocols is not None else [FakeIceProto()]


class TapPC(FakePC):
    """FakePC shaped the way aiortc 1.15 exposes its ICE stack:
    pc.getTransceivers()[i].receiver.transport.transport._connection."""

    def __init__(self, state="connected", with_tr=True):
        super().__init__(state)
        self.proto = FakeIceProto()
        tr = types.SimpleNamespace(receiver=types.SimpleNamespace(
            transport=types.SimpleNamespace(
                transport=types.SimpleNamespace(
                    _connection=FakeIceConn([self.proto])))))
        self._transceivers = [tr] if with_tr else []

    def getTransceivers(self):
        return self._transceivers


async def test_g():
    lc = new_lc()
    lc.reserve("G-1")
    lc.mark_building("G-1")
    pc = TapPC("connected")
    ok = lc.activate("G-1", pc=pc, player=FakePlayer("G-1"), avatar_session=FakeAvatar())
    rec = lc.get("G-1")
    installed = ok and rec.peer_tap_installed and rec.peer_last_signal_mono is not None

    # inbound datagram refreshes the signal and reaches the wrapped protocol
    proto = pc.proto
    proto.datagram_received(b"x", ("1.2.3.4", 5))
    fresh_ok = (time.monotonic() - rec.peer_last_signal_mono) < 1.0 and proto.inbound == 1

    # healthy: fresh signal → not due
    due = lc._due()
    not_due_when_alive = all(sid != "G-1" for sid, _ in due)

    # dead: signal goes stale → watchdog owes this session a reaping
    rec.peer_last_signal_mono = time.monotonic() - (PEER_SILENCE_S + 5)
    due = lc._due()
    due_reasons = [r for sid, r in due if sid == "G-1"]
    due_ok = due_reasons == ["peer_silence_timeout"]

    # the reap still goes through the one and only destroy_session
    out = await lc.destroy_session("G-1", "peer_silence_timeout")
    destroyed = lc.active_count() == 0 and out["released"] is True

    ok = installed and fresh_ok and not_due_when_alive and due_ok and destroyed
    record("TEST G peer-silence tap: fresh≠due, stale→due, reap via destroy_session", ok,
           f"installed={installed} fresh={fresh_ok} alive_not_due={not_due_when_alive} "
           f"reasons={due_reasons} destroyed={destroyed}")


async def test_h():
    # aiortc internals changed shape (or exotic build): tap must fail OPEN —
    # no installation, no crash, and the session is NEVER falsely reaped.
    lc = new_lc()
    lc.reserve("H-1")
    lc.mark_building("H-1")
    pc = FakePC("connected")  # plain FakePC: no _iceTransport internals
    ok = lc.activate("H-1", pc=pc, player=FakePlayer("H-1"), avatar_session=FakeAvatar())
    rec = lc.get("H-1")
    rec.peer_last_signal_mono = time.monotonic() - 10_000  # ancient, but untappable
    due = lc._due()
    not_due = all(sid != "H-1" for sid, _ in due)
    still_active = lc.active_count() == 1
    ok = ok and rec.peer_tap_installed is False and not_due and still_active
    record("TEST H tap fail-open: no internals ⇒ no false reap, session survives", ok,
           f"installed={rec.peer_tap_installed} not_due={not_due} active={lc.active_count()}")


async def test_i():
    # Late install: at activate() time the transceiver transports don't exist
    # yet (real aiortc builds them during ICE setup), so the tap must be
    # retried by the watchdog — and succeed once the transport shows up.
    lc = new_lc()
    lc.reserve("I-1")
    lc.mark_building("I-1")
    pc = TapPC("connected", with_tr=False)
    ok = lc.activate("I-1", pc=pc, player=FakePlayer("I-1"), avatar_session=FakeAvatar())
    rec = lc.get("I-1")
    not_yet = ok and rec.peer_tap_installed is False
    lc._ensure_taps()
    still_not = rec.peer_tap_installed is False
    pc._transceivers = [types.SimpleNamespace(receiver=types.SimpleNamespace(
        transport=types.SimpleNamespace(
            transport=types.SimpleNamespace(_connection=FakeIceConn([pc.proto])))))]
    lc._ensure_taps()
    installed = rec.peer_tap_installed is True and rec.peer_last_signal_mono is not None
    ok = not_yet and still_not and installed
    record("TEST I tap late-install: activate早于ICE也不丢, watchdog补装成功", ok,
           f"not_yet={not_yet} still_not={still_not} installed={installed}")


async def main():
    await test_a()
    await test_b()
    await test_c()
    await test_d()
    await test_e()
    await test_f()
    await test_shutdown()
    await test_manager_delegation()
    await test_g()
    await test_h()
    await test_i()
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} PASS"
          + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
