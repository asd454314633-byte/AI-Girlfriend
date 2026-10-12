"""SESSION OWNERSHIP — the single place a :8010 avatar session is born and dies.

Why this module exists
----------------------
Before it, one session's identity was smeared across three containers that each
had their own idea of "live":

* ``SessionManager.sessions``      — ``sessionid -> BaseAvatar`` (plus ``None`` placeholders)
* ``RTCManager.pcs``               — an unordered ``set`` of PeerConnections, *not keyed by sessionid*
* ``SegmentGate``                  — per-session response ledgers
* ``HumanPlayer`` + its worker thread, the avatar's inference / process_frames /
  process_tts threads, the ffmpeg recording subprocess, four audio/video queues

There was no ``sessionid -> pc/player/threads`` map, so there was no function any
exit path *could* call; each path cleaned up whatever it happened to hold.  The
measured consequence in ``livetalking.log``: 71 ``Creating sessionid`` against 35
``Removing session``, ``Connection state is failed`` **zero** times, and 27 + 21
rejections reading ``Maximum session limit reached (5/5)`` / ``(6/5)``.  Leaked
sessions were invisible from the admin endpoint, because ``/api/admin/sessions``
skips the ``None`` placeholders that do not count toward ``max_session`` either —
which is also how the cap got pierced to 6/5.

The contract this module freezes
--------------------------------
1. :meth:`SessionLifecycle.reserve` is the *only* way in.  A reservation counts
   toward ``max_session`` from the instant it is taken, so a build that dies
   mid-flight still occupies — and therefore still gets reaped — as a session.
2. :meth:`SessionLifecycle.destroy_session` is the *only* way out, and it is
   idempotent: called twice or five times it performs the release once and never
   raises.  It owns closing the PeerConnection, stopping media tracks, stopping
   the ``HumanPlayer`` thread, flushing the avatar audio queues, dropping the
   segment-gate ledger, removing the registry entry, and logging the reason.
3. Every exit path in :mod:`server.rtc_manager` / :mod:`server.routes` funnels
   into that one function with a ``reason`` string.  ``reason`` is also how the
   operator learns *which* path fired.
4. A watchdog (:meth:`SessionLifecycle.start_watchdog`) covers what aiortc does
   not signal: a PeerConnection that was built, answered, and then never
   connected — a browser tab that died between ``/offer`` and ICE — reports
   neither ``failed`` nor ``closed``, so it needs a deadline, not a callback.
   ``disconnected`` gets a bounded grace timer instead of an immediate kill, so a
   momentary network hiccup is not mistaken for an abandoned page.

Deliberately *not* here
-----------------------
Nothing about which avatar to load, how to answer SDP, or how to render frames —
this module only owns the lifetime of what it is handed.  That keeps it free of
model/GPU state, so it can be unit-tested without loading wav2lip.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from utils.logger import logger

# ─── Tunables ────────────────────────────────────────────────────────────────
# All of them are env-overridable so a regression run can shorten them without a
# code change; the defaults are the production contract.

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


#: A reservation whose build never completed is a leak, not a pending session.
BUILD_DEADLINE_S = _env_float("AVATAR_SESSION_BUILD_DEADLINE_S", 120.0)
#: /offer answered 200 but ICE never came up: the tab is gone.  This is the
#: deadline whose absence produced the 36 unreclaimed sessions.
CONNECT_DEADLINE_S = _env_float("AVATAR_SESSION_CONNECT_DEADLINE_S", 30.0)
#: ``disconnected`` is transient by nature — give it a bounded grace, then reap.
DISCONNECT_GRACE_S = _env_float("AVATAR_SESSION_DISCONNECT_GRACE_S", 10.0)
#: Upper bound on one destroy pass, so a wedged pc.close() cannot freeze the loop.
DESTROY_TIMEOUT_S = _env_float("AVATAR_SESSION_DESTROY_TIMEOUT_S", 20.0)
#: Watchdog sweep period.
SWEEP_INTERVAL_S = _env_float("AVATAR_SESSION_SWEEP_INTERVAL_S", 1.0)

RESERVED, BUILDING, LIVE, DISCONNECTED, DESTROYING, DESTROYED = (
    "reserved", "building", "live", "disconnected", "destroying", "destroyed",
)

#: ``kind`` values that are permanent infrastructure rather than browser
#: sessions.  ``virtualcam``/``rtmp`` pre-seed session ``'0'`` and own their
#: render thread outside of HumanPlayer, so the reaper must never touch them —
#  see the note in app.py's transport branch.
RESIDENT_KINDS = frozenset({"resident", "rtcpush"})

#: States from which a destroy is already underway or complete.  The guard is
#: what makes destroy_session idempotent.
_INFLIGHT = frozenset({DESTROYING, DESTROYED})

#: Reasons that mean "we gave up waiting", as opposed to a clean client exit.
#: Counted separately so ``forced_released`` in the totals answers the question
#: section 14 asks: how often did the server have to reap something itself?
_FORCED_REASONS = frozenset({
    "build_timeout", "never_connected", "disconnected_timeout",
    "answer_failed", "shutdown", "server_error", "watchdog",
    "peer_silence_timeout",
})

#: A hard-dead browser (crash, kill -9, network pull) never sends aiortc the
#: ``disconnected``/``failed`` event our grace path waits for — measured on
#: aiortc 1.15/aioice: the pc still reports ``connected`` 75s after the peer
#: stopped existing (see runtime/tests/a01_aiortc_death_repro.py).  Browsers do
#: the opposite of silence: RFC 7675 consent keeps STUN binding requests
#: arriving roughly every 5s for as long as the page lives.  So "the peer sent
#: us nothing for this long" is the only honest death signal, and one we own.
#: 25s ≈ 5 missed consent intervals, still below aioice's own 6×5s expiry —
#: we reap a little earlier than the transport would, if it ever did.
PEER_SILENCE_S = 25.0


@dataclass
class SessionRecord:
    """Everything one :8010 session owns, under one key."""

    session_id: str
    kind: str = "offer"
    state: str = RESERVED
    generation: int = 0

    pc: Any = None
    player: Any = None
    avatar_session: Any = None

    created_wall: float = field(default_factory=time.time)
    created_mono: float = field(default_factory=time.monotonic)
    connected_mono: Optional[float] = None
    last_activity_mono: float = field(default_factory=time.monotonic)
    last_activity_wall: float = field(default_factory=time.time)

    #: Frame counters, bumped by the HumanPlayer pump.  ``video_frames`` standing
    #: still while ``state == live`` is what separates "connected" from
    #: "MEDIA_READY" — the distinction section 11 of the brief demands.
    video_frames: int = 0
    audio_frames: int = 0
    last_video_frame_mono: Optional[float] = None

    disconnect_since_mono: Optional[float] = None
    destroy_reason: str = ""
    destroyed_wall: Optional[float] = None

    #: Peer-liveness tap (see :meth:`SessionLifecycle._install_peer_tap`).
    #: ``peer_last_signal_mono`` is bumped by the datagram hook itself, on the
    #: event loop thread, so it needs no lock.
    peer_tap_installed: bool = False
    peer_last_signal_mono: Optional[float] = None

    @property
    def age_s(self) -> float:
        return round(time.monotonic() - self.created_mono, 2)

    @property
    def idle_s(self) -> float:
        return round(time.monotonic() - self.last_activity_mono, 2)

    def connection_state(self) -> str:
        return str(getattr(self.pc, "connectionState", "") or "")

    def ice_connection_state(self) -> str:
        return str(getattr(self.pc, "iceConnectionState", "") or "")


class SessionLifecycle:
    """The single registry and the single releaser.

    Instantiated once per process and reached through the module-level
    ``session_lifecycle``.  Bookkeeping mutations happen under a plain
    ``threading.Lock`` (never ``await`` inside it) because ``create_session``
    completes from a worker-thread callback; everything slow — ``pc.close()``,
    the player thread join — happens outside the lock, on the event loop, in an
    executor where it would block.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: Dict[str, SessionRecord] = {}
        self._max_session = 1
        self._pending_gens: Dict[str, int] = {}
        self._destroying_tasks: Dict[str, "asyncio.Future[Any]"] = {}

        # ── observability counters (process-wide; see totals()) ──────────
        self._total_created = 0
        self._total_released = 0
        self._total_forced = 0
        self._total_rejected_offers = 0
        self._total_destroy_errors = 0
        self._reason_hist: Dict[str, int] = {}

        self._watchdog_task: Optional["asyncio.Future[Any]"] = None
        self._closing = False

    # ── configuration ────────────────────────────────────────────────────
    def set_max_session(self, n: int) -> None:
        with self._lock:
            self._max_session = max(1, int(n))

    @property
    def max_session(self) -> int:
        with self._lock:
            return self._max_session

    # ── way in ───────────────────────────────────────────────────────────
    def reserve(self, session_id: str, kind: str = "offer",
                counted: bool = True) -> SessionRecord:
        """Take a slot.  Raises :class:`MaxSessionError` past the cap.

        The count that the cap is measured against includes reservations and
        in-flight builds, not just finished sessions — that is the difference
        between "5/5" meaning something and the old code answering 6/5.

        ``counted=False`` is for permanent infrastructure (the ``virtualcam`` /
        ``rtmp`` pre-seeded session ``'0'``): it is registered so it is visible
        and releasable, but it must never consume a browser slot.
        """
        max_session = self.max_session
        with self._lock:
            # An idempotent re-reserve of a live key must not double-count.
            existing = self._records.get(session_id)
            if existing is not None and existing.state not in (DESTROYED, DESTROYING):
                raise ValueError(
                    f"session {session_id} already reserved (state={existing.state})"
                )
            occupied = self._occupied_locked()
            if counted and occupied >= max_session:
                self._total_rejected_offers += 1
                raise MaxSessionError(
                    f"Maximum session limit reached ({occupied}/{max_session})"
                )
            gen = self._pending_gens.get(session_id, 0) + 1
            self._pending_gens[session_id] = gen
            rec = SessionRecord(session_id=session_id, kind=kind, generation=gen)
            self._records[session_id] = rec
            self._total_created += 1
            logger.info(
                "session reserved id=%s kind=%s gen=%d occupied=%d/%d",
                session_id, kind, gen, occupied + 1, max_session,
            )
            return rec

    def mark_building(self, session_id: str) -> None:
        with self._lock:
            rec = self._records.get(session_id)
            if rec is not None and rec.state == RESERVED:
                rec.state = BUILDING

    def activate(self, session_id: str, *, pc: Any = None, player: Any = None,
                 avatar_session: Any = None) -> bool:
        """Attach what was built and move the record to ``live``.

        Returns False (without raising) when the record already went away — a
        slow build racing a watchdog reap is a legitimate outcome, and the
        caller must then tear down what it built rather than adopt it.
        """
        now = time.monotonic()
        with self._lock:
            rec = self._records.get(session_id)
            if rec is None or rec.state in _INFLIGHT:
                return False
            if pc is not None:
                rec.pc = pc
            if player is not None:
                rec.player = player
            if avatar_session is not None:
                rec.avatar_session = avatar_session
            rec.state = LIVE
            rec.last_activity_mono = now
            rec.last_activity_wall = time.time()
            self._install_peer_tap(rec)
            logger.info("session active id=%s gen=%d kind=%s", session_id, rec.generation, rec.kind)
            return True

    @staticmethod
    def _install_peer_tap(rec: SessionRecord) -> None:
        """Watch the wire for signs of life the event layer never reports.

        Wraps every aioice ``StunProtocol.datagram_received`` belonging to this
        pc so that *any* inbound datagram — consent STUN, RTCP, RTP — refreshes
        ``rec.peer_last_signal_mono``.  Read-only instrumentation of the
        transport we already own: no site-packages edits, no extra thread, no
        second teardown path — the watchdog only *decides* with this number,
        and still exits through the one and only ``destroy_session``.

        Fail-open by design: if aiortc's internals ever change shape, the tap
        simply doesn't install and that session keeps the pre-tap behavior
        (relying on pc state events), never a false kill.
        """
        try:
            # aiortc 1.15: pc.getTransceivers()[i].receiver.transport is the
            # RTCDtlsTransport, whose .transport is the RTCIceTransport, which
            # wraps the aioice Connection in ._connection (list ._protocols of
            # StunProtocol).  Walk the public path first; mangled attrs only
            # as a fallback.
            protocols: list = []
            seen: set[int] = set()
            for tr in rec.pc.getTransceivers():
                ice = getattr(getattr(getattr(tr, "receiver", None), "transport", None),
                              "transport", None)
                conn = getattr(ice, "_connection", None)
                for proto in getattr(conn, "_protocols", []):
                    if id(proto) not in seen:
                        seen.add(id(proto))
                        protocols.append(proto)
            if not protocols:
                return
            rec.peer_last_signal_mono = time.monotonic()
            for proto in protocols:
                original = proto.datagram_received
                if getattr(original, "_peer_tap_for", None) == rec.session_id:
                    continue
                def wrapped(data, addr, _orig=original, _rec=rec):
                    _rec.peer_last_signal_mono = time.monotonic()
                    _orig(data, addr)
                wrapped._peer_tap_for = rec.session_id
                proto.datagram_received = wrapped
            rec.peer_tap_installed = True
        except Exception:  # pragma: no cover - internals moved; fail open
            rec.peer_tap_installed = False

    def touch(self, session_id: str) -> None:
        """Record client activity (any HTTP call naming this session)."""
        with self._lock:
            rec = self._records.get(session_id)
            if rec is not None:
                rec.last_activity_mono = time.monotonic()
                rec.last_activity_wall = time.time()

    def note_connection_state(self, session_id: str, state: str) -> None:
        """Called from the aiortc ``connectionstatechange`` closure."""
        now = time.monotonic()
        with self._lock:
            rec = self._records.get(session_id)
            if rec is None:
                return
            rec.last_activity_mono = now
            rec.last_activity_wall = time.time()
            if state == "connected":
                rec.connected_mono = rec.connected_mono or now
                if rec.state in (LIVE, DISCONNECTED, BUILDING):
                    rec.state = LIVE
                rec.disconnect_since_mono = None
            elif state == "disconnected":
                if rec.state == LIVE:
                    rec.state = DISCONNECTED
                    rec.disconnect_since_mono = now
            # ``failed`` / ``closed`` are deliberately not acted on here: aiortc
            # has no ``disconnected`` state at all, and a callback that only
            # logged "Connection state is failed" is exactly the mechanism that
            # left 36 sessions unreclaimed.  The reaper in _due() reads
            # pc.connectionState directly instead of trusting this signal, so a
            # missed or never-fired callback still gets the slot back.

    @staticmethod
    def _media(rec: SessionRecord) -> Dict[str, Any]:
        """Frame counts, read from the pump that actually produces them.

        ``HumanPlayer`` owns the counters (it is the only object on the per-frame
        hot path, and it must not pay for a registry lookup 75 times a second),
        so the record is a fallback rather than a second source of truth.
        """
        stats = getattr(rec.player, "media_stats", None)
        if callable(stats):
            try:
                return dict(stats())
            except Exception:  # pragma: no cover - monitoring must never break a read
                pass
        return {
            "video_frames": rec.video_frames,
            "audio_frames": rec.audio_frames,
            "video_age_s": (None if rec.last_video_frame_mono is None
                            else round(time.monotonic() - rec.last_video_frame_mono, 3)),
        }

    # ── the single way out ───────────────────────────────────────────────
    async def destroy_session(self, session_id: str, reason: str = "unspecified") -> Dict[str, Any]:
        """Release everything attached to ``session_id``.  Idempotent.

        Safe to call from inside a ``connectionstatechange`` handler, from the
        watchdog, from a route, and from shutdown — concurrently and repeatedly.
        The first caller performs the release; everyone else waits for it and
        reports the same outcome.  Never raises: a teardown step that fails is
        counted and logged, because a partially-released session is worth more
        than a teardown that aborts halfway and leaves the slot burned.
        """
        waiter = self._destroying_tasks.get(str(session_id))
        if waiter is not None:
            try:
                return await asyncio.shield(waiter)
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception:  # pragma: no cover - the coroutine itself swallows
                return {"sessionid": str(session_id), "released": False,
                        "reason": reason, "idempotent": True}

        rec, already = self._claim(session_id, reason)
        if already is not None:
            return already
        if rec is None:
            # Nothing to release.  This is the normal result of the second call,
            # of a release for a session the watchdog already reaped, and of a
            # browser naming a session id from a previous process lifetime.  It
            # is *not* an error, and the client is told so with released=False.
            logger.info("session release no-op id=%s reason=%s (not registered)", session_id, reason)
            return {"sessionid": str(session_id), "released": False,
                    "reason": reason, "idempotent": True}

        loop = asyncio.get_event_loop()
        task = loop.create_task(self._release(rec, reason))
        sid_key = str(session_id)
        self._destroying_tasks[sid_key] = task
        try:
            outcome = await asyncio.shield(task)
        except asyncio.CancelledError:
            # The *caller* went away (browser hung up mid-release).  The release
            # must still finish, so the task is deliberately left running.
            logger.info("destroy_session caller cancelled; release continues id=%s", sid_key)
            raise
        finally:
            if self._destroying_tasks.get(sid_key) is task:
                self._destroying_tasks.pop(sid_key, None)
        return outcome

    def _claim(self, session_id: str, reason: str):
        """Atomically move a record to ``destroying``; return (rec, shortcut)."""
        sid = str(session_id)
        with self._lock:
            rec = self._records.get(sid)
            if rec is None:
                return None, None
            if rec.state in _INFLIGHT:
                return None, {"sessionid": sid, "released": False, "reason": reason,
                              "idempotent": True, "note": f"already {rec.state}"}
            rec.state = DESTROYING
            rec.destroy_reason = reason
            if reason in _FORCED_REASONS:
                self._total_forced += 1
            self._reason_hist[reason] = self._reason_hist.get(reason, 0) + 1
            return rec, None

    async def _release(self, rec: SessionRecord, reason: str) -> Dict[str, Any]:
        sid = rec.session_id
        steps_failed: List[str] = []
        t0 = time.monotonic()
        media0 = self._media(rec)
        logger.info(
            "session release BEGIN id=%s gen=%d kind=%s reason=%s state=%s age=%.1fs "
            "pc=%s ice=%s vframes=%d aframes=%d",
            sid, rec.generation, rec.kind, reason, rec.state, rec.age_s,
            rec.connection_state(), rec.ice_connection_state(),
            media0.get("video_frames", 0), media0.get("audio_frames", 0),
        )

        # 1/6 — stop the avatar producing audio *before* anything downstream goes
        # away, so a late /humanaudio cannot enqueue into a half-torn pipeline.
        await self._step(steps_failed, "flush_talk", rec.avatar_session, reason,
                         lambda: self._flush_avatar(rec.avatar_session))

        # 2/6 — recording owns an ffmpeg subprocess; it outlives everything else
        # if left behind.
        await self._step(steps_failed, "stop_recording", rec.avatar_session, reason,
                         lambda: self._stop_recording(rec.avatar_session))

        # 3/6 — the player thread and its four queues.  Its stop() path joins a
        # thread, so this runs in an executor: joining on the event loop is how
        # the whole :8010 service would wedge.
        await self._step(steps_failed, "stop_player", rec.player, reason,
                         lambda: self._stop_player(rec))

        # 4/6 — the PeerConnection and its tracks.
        await self._step(steps_failed, "close_pc", rec.pc, reason,
                         lambda: self._close_pc(rec.pc))

        # 5/6 — the segment / idempotency ledger, which is keyed by sessionid and
        # previously never deleted (SegmentGate.reset had zero callers).
        await self._step(steps_failed, "reset_gate", None, reason,
                         lambda: self._reset_gate(sid))

        # 6/6 — drop from the registry.  Inside the lock with the counters, so no
        # reader can observe a destroyed record as an occupied slot.
        with self._lock:
            self._records.pop(sid, None)
            self._pending_gens.pop(sid, None)
            self._total_released += 1
            rec.state = DESTROYED
            rec.destroyed_wall = time.time()
            remaining = self._occupied_locked()

        logger.info(
            "session release DONE id=%s gen=%d reason=%s in %.2fs active_left=%d%s",
            sid, rec.generation, reason, time.monotonic() - t0, remaining,
            f" steps_failed={steps_failed}" if steps_failed else "",
        )
        if steps_failed:
            with self._lock:
                self._total_destroy_errors += 1
        return {
            "sessionid": sid, "released": True, "reason": reason,
            "age_s": rec.age_s, "steps_failed": steps_failed,
            "active_sessions": remaining,
        }

    async def _step(self, failures: List[str], name: str, subject: Any,
                    reason: str, fn) -> None:
        """Run one teardown step, bounded and non-fatal.

        ``subject is None`` means the session never got that far — nothing to do,
        which is a *success*, not a failure: the paths that abort during SDP
        exchange legitimately have no player to stop.
        """
        if subject is None and name != "reset_gate":
            return
        # Both the begin and the done line are logged, at DEBUG (which this service runs
        # with): a release that takes the whole process down leaves no traceback, so the
        # last step that printed 'begin' without 'done' IS the diagnosis. Without these
        # two lines the death looked like an outside kill, because 'release BEGIN' and
        # 'Connection state is closed' bracket it but name no step.
        began = time.monotonic()
        logger.debug("session release step %s begin (reason=%s)", name, reason)
        try:
            result = fn()
            # ``run_in_executor`` hands back a Future, not a coroutine, and
            # ``iscoroutine`` answers False for it — checking awaitable-ness is
            # what keeps the player-join step from being silently skipped.
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=DESTROY_TIMEOUT_S)
        except (asyncio.TimeoutError, TimeoutError):
            failures.append(name)
            logger.error("session release step %s TIMED OUT after %.1fs (reason=%s)",
                         name, DESTROY_TIMEOUT_S, reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failing step must not abort the rest
            failures.append(name)
            logger.warning("session release step %s raised: %s (reason=%s)", name, exc, reason)
        logger.debug("session release step %s done in %.2fs (reason=%s)",
                     name, time.monotonic() - began, reason)

    # ── step implementations ─────────────────────────────────────────────
    @staticmethod
    def _flush_avatar(avatar_session: Any) -> None:
        """Drain talk state *and* the frame queue behind it.

        ``flush_talk()`` only clears ``tts.msgqueue`` and ``asr.queue``; frames
        already past them keep rendering for the dead session, so they are
        dropped here too (the queue is the one ``base_avatar`` builds per session).
        """
        flush = getattr(avatar_session, "flush_talk", None)
        if callable(flush):
            flush()
        for attr in ("res_frame_queue", "frame_queue"):
            q = getattr(avatar_session, attr, None)
            if q is None:
                continue
            try:
                while not q.empty():
                    q.get_nowait()
            except Exception:  # pragma: no cover - a foreign queue shape is fine
                pass

    @staticmethod
    def _stop_recording(avatar_session: Any) -> None:
        if not getattr(avatar_session, "recording", False):
            return
        stop = getattr(avatar_session, "stop_recording", None)
        if callable(stop):
            stop()

    @staticmethod
    def _stop_player(rec: SessionRecord) -> Any:
        """Hand the blocking join to a worker thread; return the awaitable."""
        player, sid = rec.player, rec.session_id
        stop = getattr(player, "force_stop", None)
        if not callable(stop):  # pragma: no cover - HumanPlayer always has it
            return None
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(None, stop, f"session_destroy:{sid}:{rec.generation}")

    @staticmethod
    async def _close_pc(pc: Any) -> None:
        close = getattr(pc, "close", None)
        if callable(close):
            await close()

    @staticmethod
    def _reset_gate(session_id: str) -> None:
        from server.idempotency import default_gate as gate
        reset = getattr(gate, "reset", None)
        if callable(reset):
            reset(session_id)

    # ── queries ──────────────────────────────────────────────────────────
    def get(self, session_id: str) -> Optional[SessionRecord]:
        with self._lock:
            return self._records.get(str(session_id))

    def active_count(self) -> int:
        """Occupied *browser* slots — reservations and in-flight builds included.

        Resident sessions (virtualcam/rtmp infrastructure) are excluded, so this
        is the number ``max_session`` gates and the number the acceptance
        criterion "one page ⇒ steady state == 1" is measured against.
        """
        with self._lock:
            return self._occupied_locked()

    def _occupied_locked(self) -> int:
        return sum(1 for r in self._records.values()
                   if r.state != DESTROYED and r.kind not in RESIDENT_KINDS)

    def session_ids(self) -> List[str]:
        with self._lock:
            return [sid for sid, r in self._records.items() if r.state != DESTROYED]

    def media_ready(self, session_id: str, window_s: float = 1.5) -> Dict[str, Any]:
        """"Connected" is a signalling fact; this is a media fact.

        Section 11 of the brief: the UI may only claim the digital human is
        connected once frames are actually moving.  Server-side, that means the
        HumanPlayer pump is pushing video, which is the only evidence :8010 has
        that the encoder and the render loop are alive.  The browser adds its own
        half (``videoWidth > 0`` and ``currentTime`` advancing).
        """
        with self._lock:
            rec = self._records.get(str(session_id))
            if rec is None:
                return {"sessionid": str(session_id), "ready": False, "reason": "no_such_session"}
            media = self._media(rec)
        fresh = media.get("video_age_s")
        frames = int(media.get("video_frames") or 0)
        ready = bool(rec.state == LIVE and frames > 0
                     and fresh is not None and fresh <= window_s)
        reasons = []
        if rec.state not in (LIVE, DISCONNECTED):
            reasons.append(f"state={rec.state}")
        if frames == 0:
            reasons.append("no_video_frames_pushed")
        if fresh is not None and fresh > window_s:
            reasons.append(f"video_stalled_{fresh:.1f}s")
        return {
            "sessionid": str(session_id), "ready": ready,
            "reason": "+".join(reasons) if reasons else "media_flowing",
            "state": rec.state, "video_frames": frames,
            "audio_frames": media.get("audio_frames", 0),
            "video_age_s": fresh, "pc_connection_state": rec.connection_state(),
        }

    def snapshot(self) -> Dict[str, Any]:
        """Everything the operator needs in one read — see section 14."""
        with self._lock:
            recs = list(self._records.values())
        sessions = []
        for rec in recs:
            if rec.state == DESTROYED:
                continue
            media = self._media(rec)
            sessions.append({
                "sessionid": rec.session_id,
                "kind": rec.kind,
                "state": rec.state,
                "generation": rec.generation,
                "pc_connection_state": rec.connection_state(),
                "ice_connection_state": rec.ice_connection_state(),
                "age_s": rec.age_s,
                "created_at": round(rec.created_wall, 3),
                "last_activity": round(rec.last_activity_wall, 3),
                "idle_s": rec.idle_s,
                "peer_idle_s": (
                    None if rec.peer_last_signal_mono is None
                    else round(time.monotonic() - rec.peer_last_signal_mono, 1)
                ),
                "media": {
                    "video_frames": media.get("video_frames", 0),
                    "audio_frames": media.get("audio_frames", 0),
                    "video_age_s": media.get("video_age_s"),
                    "video_queue": media.get("video_queue"),
                    "audio_queue": media.get("audio_queue"),
                    "pump_thread_alive": media.get("thread_alive"),
                },
                "media_ready": self.media_ready(rec.session_id)["ready"],
            })
        with self._lock:
            return {
                "active_sessions": self._occupied_locked(),
                "resident_sessions": sum(1 for r in recs
                                         if r.state != DESTROYED and r.kind in RESIDENT_KINDS),
                "max_session": self._max_session,
                "sessions": sessions,
                "totals": {
                    "created": self._total_created,
                    "released": self._total_released,
                    "forced_released": self._total_forced,
                    "rejected_offers": self._total_rejected_offers,
                    "destroy_errors": self._total_destroy_errors,
                },
                "reason_histogram": dict(self._reason_hist),
                "tunables": {
                    "build_deadline_s": BUILD_DEADLINE_S,
                    "connect_deadline_s": CONNECT_DEADLINE_S,
                    "disconnect_grace_s": DISCONNECT_GRACE_S,
                    "destroy_timeout_s": DESTROY_TIMEOUT_S,
                    "sweep_interval_s": SWEEP_INTERVAL_S,
                    "peer_silence_s": PEER_SILENCE_S,
                },
            }

    # ── the reaper ───────────────────────────────────────────────────────
    def start_watchdog(self) -> None:
        """Begin sweeping.  Idempotent; safe to call from the aiohttp startup hook."""
        if self._watchdog_task is not None:
            return
        self._closing = False
        self._watchdog_task = asyncio.ensure_future(self._watchdog())
        logger.info(
            "session watchdog started (build<=%.0fs connect<=%.0fs disconnect grace=%.0fs sweep=%.1fs)",
            BUILD_DEADLINE_S, CONNECT_DEADLINE_S, DISCONNECT_GRACE_S, SWEEP_INTERVAL_S,
        )

    def stop_watchdog(self) -> None:
        task, self._watchdog_task = self._watchdog_task, None
        if task is not None:
            task.cancel()

    async def _watchdog(self) -> None:
        while not self._closing:
            try:
                await asyncio.sleep(SWEEP_INTERVAL_S)
                # A pc built after activate() (or whose transports didn't exist
                # yet at activation time) gets its tap on a later sweep — the
                # wire keeps flowing consent bindings for the life of the pc,
                # so a late install still sees liveness within seconds.
                self._ensure_taps()
                for sid, why in self._due():
                    try:
                        await self.destroy_session(sid, why)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # pragma: no cover - the sweep must survive one bad reap
                        logger.exception("watchdog reap failed id=%s reason=%s", sid, why)
            except asyncio.CancelledError:
                return
            except Exception:  # pragma: no cover
                logger.exception("session watchdog iteration failed; continuing")

    def _ensure_taps(self) -> None:
        """(Re)attempt the peer tap for live sessions that don't have one yet.

        Cheap by construction: only untapped records are touched, and
        :meth:`_install_peer_tap` is fail-open.  Runs on the event loop, same
        thread the datagram hook fires on.
        """
        with self._lock:
            pending = [rec for rec in self._records.values()
                       if rec.state == LIVE and rec.pc is not None
                       and not rec.peer_tap_installed]
        for rec in pending:
            self._install_peer_tap(rec)

    def _due(self) -> List[tuple]:
        """Pure decision function — the whole reaping policy, separately testable."""
        now = time.monotonic()
        due: List[tuple] = []
        with self._lock:
            for sid, rec in self._records.items():
                if rec.state in _INFLIGHT or rec.kind in RESIDENT_KINDS:
                    continue
                pc_state = str(getattr(rec.pc, "connectionState", "") or "")
                if rec.state in (RESERVED, BUILDING):
                    # Only meaningful once the build call is in flight; a
                    # reservation that never got picked up is equally dead.
                    if now - rec.created_mono > BUILD_DEADLINE_S:
                        due.append((sid, "build_timeout"))
                    continue
                # ``pc is None`` happens between reserve() and activate() on the
                # answer-failed path; the build deadline above already owns it.
                if rec.pc is not None and pc_state in ("failed", "closed"):
                    due.append((sid, f"pc_{pc_state}"))
                    continue
                # The hard-death reaper: a crashed browser keeps pc_state
                # "connected" forever (aiortc 1.15 has no working consent
                # expiry — proven by a01_aiortc_death_repro.py), but it also
                # stops sending us so much as a STUN consent binding.  Silence
                # on the wire is the death signal the event layer can't give.
                if rec.state == LIVE and rec.peer_tap_installed:
                    last = rec.peer_last_signal_mono
                    if last is not None and now - last > PEER_SILENCE_S:
                        due.append((sid, "peer_silence_timeout"))
                        continue
                if rec.state == DISCONNECTED:
                    since = rec.disconnect_since_mono or now
                    if now - since > DISCONNECT_GRACE_S:
                        due.append((sid, "disconnected_timeout"))
                    elif pc_state == "connected":
                        rec.state = LIVE  # recovered inside the grace window
                        rec.disconnect_since_mono = None
                    continue
                if rec.connected_mono is None and pc_state != "connected":
                    if now - rec.created_mono > CONNECT_DEADLINE_S:
                        # The load-bearing reap: an answered offer whose ICE never
                        # came up reports nothing, forever, without this.
                        due.append((sid, "never_connected"))
        return due

    async def shutdown_all(self, reason: str = "shutdown") -> None:
        """Every session, concurrently bounded — the :8010 exit path."""
        self._closing = True
        self.stop_watchdog()
        for sid in self.session_ids():
            try:
                await self.destroy_session(sid, reason)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                logger.exception("shutdown_all failed id=%s", sid)

    def release_all_sync(self, reason: str = "shutdown") -> None:
        """Best-effort release for a path with no loop (interpreter teardown).

        Only clears bookkeeping; the threads die with the process.  This exists
        so an abrupt exit at least stops *reporting* live sessions.
        """
        with self._lock:
            for rec in self._records.values():
                if rec.state != DESTROYED:
                    rec.state = DESTROYED
                    rec.destroy_reason = reason


class MaxSessionError(Exception):
    """Raised by :meth:`SessionLifecycle.reserve` when every slot is occupied.

    Re-exported by :mod:`server.session_manager` so existing importers keep
    working against one class object rather than two look-alikes.
    """


#: The process-wide singleton.
session_lifecycle = SessionLifecycle()
