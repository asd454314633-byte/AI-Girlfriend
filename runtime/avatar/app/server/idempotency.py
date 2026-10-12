"""(response_id, segment_index) gate for the :8010 audio/text enqueue boundary (任务 §7 H).

Why this module exists
----------------------
``POST /humanaudio`` is the only way assistant audio reaches the avatar renderer
(``runtime/ui/app/avatar-sync.js`` -> ``routes.humanaudio`` -> ``BaseAvatar
.put_audio_file`` -> ``BaseASR.queue``).  The browser tags every upload with
``response_id`` and, since 任务 §7 H 收口, with ``segment_index`` too (see
``avatar-sync.js``'s ``drainUploads`` and ``runtime/tests/test_realtime_transport.py
::upload_fields_match_the_avatar_gate``), and ``providers/contracts.py:62-75``
already *documents* ``(response_id, segment_index)`` as the idempotency key that
"every downstream consumer must dedupe on" — but nothing on this boundary read
either field.  ``routes.humanaudio`` took ``sessionid`` + ``file`` and dropped the
rest, so the service physically cannot tell a retry from new speech, nor an old
response's late audio from the current one.

This module is that missing key.  It is deliberately pure-stdlib and side-effect
free (no aiohttp, no torch, no clock): the only thing it does is answer
"accept / duplicate / stale-response / out-of-order / untagged" for one proposal,
so the accept path can be unit-tested against a fake receiver without a GPU, a
server, or the live :8010.

Three faults, three verdicts
----------------------------
``DUPLICATE``      same ``(response_id, segment_index)`` twice  -> 重复入队
``STALE_RESPONSE`` a response whose successor already enqueued  -> 旧 response 插入新 response
                   (and, as a consequence, 跨 response 串音: once B starts, A
                   can no longer interleave frames into B)
``OUT_OF_ORDER``   a segment at or below the high-water mark of its own response

Mode
----
``observe`` (default) computes and logs the verdict but never drops, so shipping
this changes no audio for callers that only send ``response_id``.  ``enforce``
drops.  ``AVATAR_SEGMENT_GATE=enforce`` is the flip; a caller that sends
``segment_index`` is what makes the *key* exact per segment — the mode still
decides whether a verdict is enforced, and nothing else does.

One owner for "cancel the current response"
-------------------------------------------
:func:`SegmentGate.cancel_turn` is the *only* place on this service that decides
what an interrupt (插话) means.  Both HTTP interrupt doors
(``/interrupt_talk``, ``/human?type=echo&interrupt=true``) call it, and it is
deliberately able to name the response being cancelled: raising the watermark
alone cannot stop a response that had not enqueued *anything* yet (a reply
interrupted before ``response.done``), because such an id has no sequence number
and would be handed a fresh one above the watermark.  Naming it puts the id on a
blocklist, which is what actually closes 旧 response 插入新 response for the
common barge-in case.

``response_id`` is the same scalar the browser put on ``/humanaudio``, so one
cancel covers ":8765 stopped meaning it" and ":8010 must not render it".

Note on ``datainfo``: verdict metadata is *not* passed downstream into
``put_audio_file``'s ``datainfo``.  That dict is merged into every 20 ms frame's
eventpoint (``base_avatar.py:135-149``) and each non-empty eventpoint triggers a
``notify()`` SSE broadcast + log line (``base_avatar.py:224-227``,
``server/webrtc.py:153-154``), so tagging frames there would emit ~50 SSE
messages per second per utterance.  Attribution belongs in the route's log until
the frame path grows a cheap tag channel (see FAULT_EH.md 留开项).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# The logger's NAME is load-bearing, not cosmetic.  This app configures exactly
# one handler -- ``utils/logger.py`` attaches a FileHandler to the logger named
# ``utils.logger`` and never touches the root logger -- so a record only reaches
# ``livetalking.log`` if its logger is ``utils.logger`` or a **child** of it
# (propagation walks up to the ancestor's handler).  ``logging.getLogger(__name__)``
# here would be named ``server.idempotency``, whose ancestors are ``server`` and
# root, both handler-less; ``logging.lastResort`` then swallows anything below
# WARNING.  That is what happened from the P1-C refactor until 2026-09-24: the two
# ``… segment gate mode=…`` lines that used to be emitted from ``routes.py`` (on the
# project logger) moved into :meth:`SegmentGate.admit_turn` and went silent, so the
# gate kept enforcing while producing no production evidence -- and the 11-item
# browser acceptance could not see a single verdict.  Measured on this machine:
# ``root_handlers=[]``, ``avatar.segment_gate`` -> dropped,
# ``utils.logger.segment_gate`` -> written.  Being a child of the project logger
# also keeps this module importable on its own (a bare ``from utils.logger import
# logger`` would create that FileHandler, and its path is relative to the CWD, so an
# offline test run would litter a stray ``livetalking.log`` outside the avatar app).
logger = logging.getLogger("utils.logger.segment_gate")

ACCEPT = "accept"
DUPLICATE = "duplicate"
STALE_RESPONSE = "stale_response"
OUT_OF_ORDER = "out_of_order"
UNTAGGED = "untagged"

#: Verdicts that mean "this audio must not reach the renderer".
REJECTS = (DUPLICATE, STALE_RESPONSE, OUT_OF_ORDER)

OBSERVE = "observe"
ENFORCE = "enforce"

#: The one reason vocabulary a cancel may carry; keeps the log line comparable
#: across the two HTTP doors and across the browser's own decision record.
CANCEL_REASONS = ("user_interrupt", "barge_in", "manual_stop", "unspecified")

MAX_TRACKED_RESPONSES = 64


def normalize_mode(raw: Any, default: str = OBSERVE) -> str:
    value = str(raw or "").strip().lower()
    return value if value in (OBSERVE, ENFORCE) else default


@dataclass
class _ResponseState:
    """One response's progress inside one session."""

    seq: int = 0
    seen: set[int] = field(default_factory=set)
    high_water: int = -1


@dataclass
class Verdict:
    """The gate's answer plus the key it judged, so callers never re-derive it."""

    decision: str
    response_id: str = ""
    segment_index: int | None = None
    session_id: str = ""
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.decision not in REJECTS

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "response_id": self.response_id,
            "segment_index": self.segment_index,
            "session_id": self.session_id,
            "reason": self.reason,
        }


def parse_segment_index(raw: Any) -> int | None:
    """Strict-ish int coercion: absent/garbage means 'no key', never 0.

    Coercing a missing field to 0 would make the *first* segment of every legacy
    response share one key, so a legitimate second chunk of the same response
    would be judged a duplicate.  ``None`` keeps "unknown" distinct from "zero".
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value >= 0 else None


def parse_response_id(raw: Any) -> str:
    if raw is None:
        return ""
    text = str(raw).strip()
    return text[:128]


class SegmentGate:
    """Per-session ``(response_id, segment_index)`` ledger.

    Not thread-safe by design: :class:`aiohttp` serialises the request handlers
    on one event loop, so a plain dict is enough.  If a second (non-async)
    producer is ever added, guard :meth:`admit` with a lock — the class keeps no
    derived caches, so that is a one-line change.
    """

    def __init__(self, *, mode: str = OBSERVE, max_tracked: int = MAX_TRACKED_RESPONSES) -> None:
        self.mode = normalize_mode(mode)
        self.max_tracked = max(8, int(max_tracked))
        self._responses: dict[str, dict[str, _ResponseState]] = {}
        self._order: dict[str, list[str]] = {}
        self._seq: dict[str, int] = {}
        self._watermark: dict[str, int] = {}
        #: response_ids retired by :meth:`cancel_turn`; bounded like everything else.
        self._blocked: dict[str, set[str]] = {}
        self._blocked_order: dict[str, list[str]] = {}
        self._cancels = 0

    # ------------------------------------------------------------------ state

    def _state(self, session_id: str, response_id: str) -> _ResponseState:
        book = self._responses.setdefault(session_id, {})
        state = book.get(response_id)
        if state is None:
            seq = self._seq.get(session_id, 0)
            self._seq[session_id] = seq + 1
            state = _ResponseState(seq=seq)
            book[response_id] = state
            order = self._order.setdefault(session_id, [])
            order.append(response_id)
            while len(order) > self.max_tracked:
                evicted = order.pop(0)
                if evicted == response_id:
                    order.insert(0, evicted)
                    break
                book.pop(evicted, None)
        return state

    # ---------------------------------------------------------------- public

    def _block(self, session: str, response_id: Any) -> str:
        """Retire one response id for one session (bounded, LRU like the ledger)."""
        rid = parse_response_id(response_id)
        if not rid:
            return ""
        blocked = self._blocked.setdefault(session, set())
        order = self._blocked_order.setdefault(session, [])
        if rid not in blocked:
            blocked.add(rid)
            order.append(rid)
        while len(order) > self.max_tracked:
            blocked.discard(order.pop(0))
        return rid

    def is_cancelled(self, session_id: str, response_id: Any) -> bool:
        return parse_response_id(response_id) in self._blocked.get(str(session_id or ""), set())

    def cancel_turn(
        self,
        session_id: str,
        *,
        response_id: Any = None,
        reason: str = "user_interrupt",
    ) -> dict[str, Any]:
        """THE single decision point for "cancel the current response" on :8010.

        Every interrupt door funnels here, and here only, so the two HTTP routes
        can never drift apart and so the browser's own cancel record and this
        one agree on what was cancelled.  It does two things, in this order:

        1. retire the named ``response_id`` (the barge-in case where the
           interrupted reply had not enqueued anything yet), and
        2. raise the watermark past every response this session has enqueued,
           which retires the un-named ones too.

        ``reason`` is validated against :data:`CANCEL_REASONS` so the log stays
        machine-comparable; an unknown reason becomes ``unspecified`` rather than
        whatever string a new caller invented.
        """
        reason = reason if reason in CANCEL_REASONS else "unspecified"
        session = str(session_id or "")
        rid = self._block(session, response_id)
        watermark = self.mark_interrupted(session)
        self._cancels += 1
        decision = {
            "session_id": session,
            "cancelled_response_id": rid,
            "watermark": watermark,
            "reason": reason,
            "mode": self.mode,
            # What will happen to a late segment of the cancelled response: under
            # ``observe`` nothing is dropped yet, which is the whole point of the
            # two-mode design — the verdict is provable before it is enforced.
            "will_drop_late_segments": self.mode == ENFORCE,
        }
        logger.info("turn cancel %s", decision)
        return decision

    def admit(
        self,
        session_id: str,
        response_id: Any,
        segment_index: Any,
        *,
        total_segments: Any = None,
    ) -> Verdict:
        """Judge one proposed enqueue; record it when the verdict is ACCEPT.

        ``segment_index=None`` means the caller never adopted the contract.  The
        gate then falls back to the response-level key: one *first* segment per
        response, which is exactly what the current browser sends, and still
        rejects a byte-identical retry of that response.
        """
        rid = parse_response_id(response_id)
        index = parse_segment_index(segment_index)
        session = str(session_id or "")

        if not rid:
            # Nothing to key on.  Accept — dropping untagged audio would be a
            # product regression, and the manifest already declares
            # per_turn_attribution unsupported for this provider.
            return Verdict(UNTAGGED, "", index, session, "NO_RESPONSE_ID")

        if rid in self._blocked.get(session, ()):
            # Retired by an interrupt before it ever enqueued: it has no sequence
            # number, so the watermark test below could not have caught it.
            return Verdict(STALE_RESPONSE, rid, index, session, "RESPONSE_CANCELLED")

        state = self._state(session, rid)
        key = 0 if index is None else index

        if key in state.seen:
            return Verdict(DUPLICATE, rid, index, session, f"SEGMENT_{key}_ALREADY_ADMITTED")
        if key <= state.high_water:
            # Not literally seen, but its response has already rendered past it.
            return Verdict(OUT_OF_ORDER, rid, index, session, "SEGMENT_BELOW_HIGH_WATER")

        watermark = self._watermark.get(session, -1)
        if state.seq < watermark:
            return Verdict(
                STALE_RESPONSE, rid, index, session,
                f"RESPONSE_SEQ_{state.seq}_BELOW_WATERMARK_{watermark}",
            )

        # Accept path: mutate only after every rejection has been returned.
        state.seen.add(key)
        state.high_water = max(state.high_water, key)
        if state.seq > watermark:
            self._watermark[session] = state.seq
        return Verdict(ACCEPT, rid, index, session, "")

    def admit_and_apply(
        self,
        session_id: str,
        response_id: Any,
        segment_index: Any,
        enqueue,
        *,
        total_segments: Any = None,
    ) -> tuple[Verdict, bool]:
        """Drive a fake-or-real ``enqueue()`` callable under the gate's mode.

        Returns ``(verdict, ran)``.  ``observe`` always runs the enqueue (the
        verdict is recorded, not enforced); ``enforce`` skips rejected audio.
        Exceptions from ``enqueue`` are not swallowed — the caller's own error
        handling (``routes.humanaudio``'s ``json_error``) must keep working.
        """
        verdict = self.admit(session_id, response_id, segment_index, total_segments=total_segments)
        # ``accepted`` covers ACCEPT and UNTAGGED: audio the gate has no key for
        # must keep flowing, or adopting this module would itself be a regression.
        if verdict.accepted or self.mode == OBSERVE:
            enqueue()
            return verdict, True
        logger.info(
            "segment gate rejected enqueue mode=enforce %s", verdict.to_dict()
        )
        return verdict, False

    def admit_turn(self, session_id: str, fields: Mapping, *, kind: str = "audio") -> tuple[Verdict, bool]:
        """The one read-the-request form of :meth:`admit_and_apply`.

        Both enqueue doors (:meth:`server.routes.humanaudio` for audio and
        ``/human?type=echo`` for text) hand over the same four fields, so both
        go through here: one place decides what ``(response_id, segment_index)``
        means for this service, and a new enqueue route cannot accidentally
        invent a second rule.

        Returns ``(verdict, allowed)``.  ``allowed`` is what the route must obey
        — in ``observe`` mode it is always True, so shipping this changes nothing
        for a caller that adopted the key halfway.
        """
        verdict = self.admit(
            session_id,
            fields.get("response_id"),
            fields.get("segment_index"),
            total_segments=fields.get("segment_count"),
        )
        allowed = verdict.accepted or self.mode == OBSERVE
        if not allowed:
            logger.info("%s rejected by segment gate mode=enforce %s", kind, verdict.to_dict())
        elif verdict.decision != UNTAGGED:
            logger.info("%s segment gate mode=%s %s", kind, self.mode, verdict.to_dict())
        return verdict, allowed

    def mark_interrupted(self, session_id: str, response_id: Any = None) -> int:
        """Advance the session watermark past every response seen so far.

        Called through :meth:`cancel_turn` on an interrupt: after a user barges
        in, the responses that were already in flight are *by definition* old, so
        their late audio must not slip into whatever the next turn renders.  The
        watermark therefore moves one *past* the newest known response — a replay
        of an already-known ``response_id`` is rejected, while the next genuinely
        new response (which gets the next sequence number) still plays.  Returns
        the new watermark (``-1`` when the session never enqueued anything).

        Pass ``response_id`` to *also* retire a response that never enqueued
        anything here — see :meth:`cancel_turn`.
        """
        session = str(session_id or "")
        self._block(session, response_id)
        book = self._responses.get(session, {})
        highest = max((state.seq for state in book.values()), default=None)
        if highest is None:
            return self._watermark.get(session, -1)
        candidate = highest + 1
        if candidate > self._watermark.get(session, -1):
            self._watermark[session] = candidate
        return self._watermark[session]

    def reset(self, session_id: str | None = None) -> None:
        """Drop bookkeeping for one session (session teardown) or all of them."""
        if session_id is None:
            self._responses.clear()
            self._order.clear()
            self._seq.clear()
            self._watermark.clear()
            self._blocked.clear()
            self._blocked_order.clear()
            return
        session = str(session_id)
        self._responses.pop(session, None)
        self._order.pop(session, None)
        self._seq.pop(session, None)
        self._watermark.pop(session, None)
        self._blocked.pop(session, None)
        self._blocked_order.pop(session, None)

    def snapshot(self, session_id: str) -> dict[str, Any]:
        """Read-only view for logs/tests: what the gate believes about a session."""
        session = str(session_id or "")
        book = self._responses.get(session, {})
        return {
            "mode": self.mode,
            "watermark": self._watermark.get(session, -1),
            "cancelled": sorted(self._blocked.get(session, set())),
            "cancels": self._cancels,
            "responses": {
                rid: {"seq": state.seq, "high_water": state.high_water, "segments": len(state.seen)}
                for rid, state in sorted(book.items(), key=lambda row: row[1].seq)
            },
        }


def replay(orders: Iterable[tuple[str, Any]], *, mode: str = ENFORCE, session_id: str = "s1") -> list[str]:
    """Convenience for probes: feed ``(response_id, segment_index)`` pairs back in."""
    gate = SegmentGate(mode=mode)
    return [gate.admit(session_id, rid, idx).decision for rid, idx in orders]


#: One process-wide gate: :8010 serves one resident avatar process per session id,
#: and the ledger must survive across requests.  ``AVATAR_SEGMENT_GATE=enforce``
#: is read once at import, exactly like the service's other launch-time switches.
default_gate = SegmentGate(mode=normalize_mode(os.environ.get("AVATAR_SEGMENT_GATE")))
