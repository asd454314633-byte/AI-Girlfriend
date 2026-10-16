""":8010 边界上的幂等 / 统一 cancel —— 用**真实 routes.py** 跑的离线回归 (任务 §7 H).

Run with the **avatar** venv (it is the one that has aiohttp)::

    runtime/avatar/venv/Scripts/python.exe runtime/tests/test_avatar_turn_transport.py

Why this file exists
--------------------
``test_continuity_and_idempotency.py`` proves the gate's *logic*.  It cannot
prove the thing that actually matters to the user, which is:

  1. that ``POST /humanaudio`` — the real handler, with the real multipart
     parsing, on a real socket — refuses to render the same
     ``(response_id, segment_index)`` twice,
  2. that ``POST /interrupt_talk`` with a ``response_id`` blocks a reply that
     never reached the queue, and
  3. that the gate's two modes differ **exactly** where the docstring says they
     do (``observe`` = verdict only, ``enforce`` = verdict enforced).

Those are HTTP-boundary claims, so they are tested over HTTP.  The live :8010 is
launched in ``observe`` and must not be restarted by this task, so the enforce
half is exercised here, against the shipped handler code, on a throwaway aiohttp
app in a daemon thread bound to 127.0.0.1 on an ephemeral port.

What is real and what is faked
------------------------------
* real: ``server/routes.py`` (every handler + ``setup_routes``), ``aiohttp``,
  multipart encoding, ``server/idempotency.py`` and its ``Verdict``.
* faked: ``server.session_manager`` / ``server.avatar_routes`` are replaced by
  stubs **before** ``server.routes`` is imported.  The first pulls in
  ``avatars.base_avatar`` (torch, whisper, a model load); the second registers
  avatar-generation routes.  Neither is on the path under test, and a test that
  loaded them would need GPU work this task is not allowed to start.  The fake
  session counts ``put_audio_file`` calls, which is the definition of "the
  renderer got the audio" — ``routes.humanaudio`` calls it exactly once per
  accepted segment.

Every check has a negative control in the same run: the same bytes, posted to
the same handler with the same key, in ``observe`` mode, must produce **two**
renders.  If that ever stops being true, the enforce numbers mean nothing.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]
AVATAR_APP = RUNTIME / "avatar" / "app"
if str(AVATAR_APP) not in sys.path:
    sys.path.insert(0, str(AVATAR_APP))

try:
    from aiohttp import web
except Exception as exc:  # noqa: BLE001
    print(
        "FAIL aiohttp is required — run this file with the avatar venv:\n"
        f"      runtime/avatar/venv/Scripts/python.exe runtime/tests/{Path(__file__).name}\n"
        f"  ({type(exc).__name__}: {exc})"
    )
    raise SystemExit(1)

import urllib.error
import urllib.request

STUB_SESSIONS: dict[str, object] = {}


# ── stubs for the two imports routes.py needs but does not exercise ───────────

class FakeAvatarSession:
    """Stand-in for ``BaseAvatar``: records what reached the renderer."""

    def __init__(self, sessionid: str):
        self.sessionid = sessionid
        self.opt = None
        self.recording = False
        self.audio: list[tuple[str, int]] = []       # (uploaded bytes, byte count)
        self.texts: list[str] = []
        self.flushes = 0

    def put_audio_file(self, filebyte, datainfo=None):
        # The tag is read back out of the bytes: `datainfo` is deliberately empty
        # in routes.humanaudio (per-frame eventpoints would emit ~50 SSE/s), so
        # the WAV itself is the only thing that can say which segment rendered.
        self.audio.append((filebyte.decode("utf-8", "replace"), len(filebyte)))

    def put_msg_txt(self, msg, datainfo=None):
        self.texts.append(msg)

    def flush_talk(self):
        self.flushes += 1

    def is_speaking(self):
        return False


class _FakeSessionManager:
    def get_session(self, sessionid):
        return STUB_SESSIONS.get(str(sessionid))

    @property
    def sessions(self):
        return STUB_SESSIONS


def _install_stubs() -> None:
    """Put the two stand-in modules into sys.modules *before* importing routes."""
    import types

    manager = types.ModuleType("server.session_manager")
    manager.session_manager = _FakeSessionManager()       # routes.py: `import ... session_manager`
    manager.MaxSessionError = type("MaxSessionError", (Exception,), {})
    sys.modules.setdefault("server.session_manager", manager)

    avatar_routes = types.ModuleType("server.avatar_routes")
    avatar_routes.setup_avatar_routes = lambda app: None
    sys.modules.setdefault("server.avatar_routes", avatar_routes)


_install_stubs()

from server import routes as live_routes                       # noqa: E402
from server.idempotency import (                                # noqa: E402
    ACCEPT,
    DUPLICATE,
    ENFORCE,
    OBSERVE,
    STALE_RESPONSE,
    SegmentGate,
)

SESSION = "p1c-session"


def _new_gate(mode: str) -> SegmentGate:
    """Swap the process-wide gate the shipped handlers close over."""
    gate = SegmentGate(mode=mode)
    live_routes.segment_gate = gate
    STUB_SESSIONS.clear()
    STUB_SESSIONS[SESSION] = FakeAvatarSession(SESSION)
    return gate


# ── one throwaway :8010 on an ephemeral port ─────────────────────────────────

class _Server:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.base = None
        self.runner = None
        self.ready = threading.Event()
        self.error = None

    def _main(self):
        import asyncio

        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            app = web.Application()
            live_routes.setup_routes(app)
            runner = web.AppRunner(app)
            self.loop.run_until_complete(runner.setup())
            self.runner = runner
            # ``BaseSite.start()`` returns None; the aiohttp Server hangs off the
            # site, and the ephemeral port only exists as a socket address.
            site = web.TCPSite(runner, "127.0.0.1", 0)
            self.loop.run_until_complete(site.start())
            port = site._server.sockets[0].getsockname()[1]
            self.base = f"http://127.0.0.1:{port}"
        except BaseException as exc:            # noqa: BLE001 — a dead thread would
            self.error = exc                    # otherwise read as "never came up"
        finally:
            self.ready.set()
        if self.error is None:
            self.loop.run_forever()

    def start(self):
        # ``setup_routes`` ends with ``add_static('/', path='web')`` — the shipped
        # ``app.py`` relies on being launched from ``runtime/avatar/app``, so that
        # one relative path is the only thing in the handler stack that cares what
        # CWD the suite runs from (this file is invoked from the repo root).  Hold
        # the launch CWD only while the app is being *built*; ``ready`` is set after
        # ``add_static`` has resolved, so nothing below needs the directory anymore.
        previous = os.getcwd()
        os.chdir(AVATAR_APP)
        try:
            self.thread = threading.Thread(target=self._main, daemon=True)
            self.thread.start()
            if not self.ready.wait(20):
                raise AssertionError("the :8010 handler stack never came up")
            if self.error is not None:
                raise AssertionError(
                    f"the :8010 handler stack failed to come up: {self.error!r}"
                ) from self.error
        finally:
            os.chdir(previous)

    def stop(self):
        # Stop by the app's own teardown door first: a bare loop.stop() leaves the
        # proactor's in-flight accept task pending, and the interpreter then prints
        # a genuine-looking "Task was destroyed" traceback that a reader could
        # mistake for a failing check.
        if self.loop is None:
            return
        import asyncio

        try:
            if self.runner is not None:
                asyncio.run_coroutine_threadsafe(
                    self.runner.cleanup(), self.loop
                ).result(10)
        except Exception as exc:  # noqa: BLE001 — teardown must not fail a pass
            print(f"  note: throwaway :8010 teardown reported {type(exc).__name__}: {exc}")
        self.loop.call_soon_threadsafe(self.loop.stop)


SERVER = _Server()


def _multipart(fields: dict, filename="seg.wav", blob=b"RIFFfake-wav") -> tuple[str, bytes]:
    boundary = f"----p1c{uuid.uuid4().hex}"
    out = io.BytesIO()
    for name, value in fields.items():
        out.write(f"--{boundary}\r\n".encode())
        out.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.write(f"{value}\r\n".encode())
    out.write(f"--{boundary}\r\n".encode())
    out.write(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    out.write(blob)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return boundary, out.getvalue()


def post_json(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        SERVER.base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode())


def post_humanaudio(**fields) -> dict:
    # The renderer is only ever handed WAV bytes, so the blob has to carry the
    # segment's identity or no check below can say *which* segments got through:
    # same (response_id, segment_index) -> byte-identical upload (exactly what a
    # lost-ack retry looks like), different ones -> different bytes.
    blob = "RIFF{}#{}".format(
        fields.get("response_id", "-"), fields.get("segment_index", "-")
    ).encode()
    boundary, body = _multipart({"sessionid": SESSION, **fields}, blob=blob)
    request = urllib.request.Request(
        SERVER.base + "/humanaudio",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return {"code": exc.code, "msg": exc.reason, "body": exc.read()[:400]}


def get_json(path: str) -> dict:
    with urllib.request.urlopen(SERVER.base + path, timeout=20) as response:
        return json.loads(response.read().decode())


def rendered(session=None):
    session = session or STUB_SESSIONS[SESSION]
    return list(session.audio)


# ── checks ───────────────────────────────────────────────────────────────────

def enforce_mode_renders_one_play_for_a_doubly_posted_segment():
    """The literal §H 要求 2, over a real socket and the shipped handler."""
    gate = _new_gate(ENFORCE)
    key = {"response_id": "resp_enf", "segment_index": "0"}
    first = post_humanaudio(**key)
    retry = post_humanaudio(**key)
    assert first["code"] == 0 and first["data"]["enqueued"] is True, first
    assert first["data"]["segment_gate"]["decision"] == ACCEPT, first
    assert retry["code"] == 0, f"a rejected retry must still answer 0: {retry}"
    assert retry["data"]["enqueued"] is False, retry
    assert retry["data"]["segment_gate"]["decision"] == DUPLICATE, retry
    assert len(rendered(gate and STUB_SESSIONS[SESSION])) == 1, STUB_SESSIONS[SESSION].audio
    # the segment counter of the same response still advances normally
    assert post_humanaudio(response_id="resp_enf", segment_index="1")["data"]["enqueued"] is True
    assert len(STUB_SESSIONS[SESSION].audio) == 2, STUB_SESSIONS[SESSION].audio


def observe_mode_is_the_negative_control_and_renders_twice():
    """Same bytes, same handler, same key — ``observe`` must NOT deduplicate.

    This is the check that makes the previous one a proof instead of a tautology:
    if the route never reached the renderer, or the client's own loop did the
    deduping, both modes would report one play and the enforce number would mean
    nothing.
    """
    _new_gate(OBSERVE)
    key = {"response_id": "resp_obs", "segment_index": "0"}
    first, retry = post_humanaudio(**key), post_humanaudio(**key)
    assert len(STUB_SESSIONS[SESSION].audio) == 2, (
        f"observe mode dropped audio: {STUB_SESSIONS[SESSION].audio}"
    )
    assert first["data"]["segment_gate"]["decision"] == ACCEPT, first
    assert retry["data"]["segment_gate"]["decision"] == DUPLICATE, retry
    assert retry["data"]["enqueued"] is True, "observe mode enforced a verdict"


def untagged_upload_is_untouched_by_the_gate():
    """A caller that adopted neither field keeps working verbatim."""
    _new_gate(ENFORCE)
    response = post_humanaudio()                       # sessionid + file, nothing else
    assert response["code"] == 0 and "data" not in response, response
    assert len(STUB_SESSIONS[SESSION].audio) == 1, STUB_SESSIONS[SESSION].audio
    again = post_humanaudio()
    assert len(STUB_SESSIONS[SESSION].audio) == 2, "untagged audio was deduped"


def a_named_cancel_blocks_a_reply_that_never_reached_the_queue():
    """§H 要求 3+4 at the HTTP door: the barge-in names the response, and the
    interrupted reply's *first* segment is then refused."""
    _new_gate(ENFORCE)
    cancel = post_json("/interrupt_talk", {
        "sessionid": SESSION, "response_id": "resp_late", "reason": "user_interrupt",
    })
    assert cancel["code"] == 0, cancel
    decision = cancel["data"]["turn_cancel"]
    assert decision["cancelled_response_id"] == "resp_late", decision
    assert decision["watermark"] == -1, decision          # nothing had ever enqueued
    assert decision["mode"] == ENFORCE and decision["will_drop_late_segments"] is True, decision
    assert STUB_SESSIONS[SESSION].flushes == 1, "flush_talk must still run"

    late = post_humanaudio(response_id="resp_late", segment_index="0")
    assert late["data"]["enqueued"] is False, late
    assert late["data"]["segment_gate"]["decision"] == STALE_RESPONSE, late
    assert late["data"]["segment_gate"]["reason"] == "RESPONSE_CANCELLED", late
    assert STUB_SESSIONS[SESSION].audio == [], STUB_SESSIONS[SESSION].audio
    # and the next turn is not collateral damage
    assert post_humanaudio(response_id="resp_next", segment_index="0")["data"]["enqueued"] is True
    assert len(STUB_SESSIONS[SESSION].audio) == 1


def an_unnamed_cancel_is_the_control_that_still_lets_it_through():
    """Without a name, :8010 cannot block a reply it has never seen.

    Exactly the case the browser used to be in — ``/interrupt_talk`` carried only
    a sessionid.  Kept as a live check so nobody 'fixes' the route back to that.
    """
    _new_gate(ENFORCE)
    response = post_json("/interrupt_talk", {"sessionid": SESSION})
    assert response["data"]["turn_cancel"]["cancelled_response_id"] == "", response
    assert post_humanaudio(response_id="resp_unnamed", segment_index="0")["data"]["enqueued"] is True
    assert len(STUB_SESSIONS[SESSION].audio) == 1, (
        "an unnamed cancel blocked a never-seen response — the named control above "
        "would no longer be proving the difference"
    )


def the_text_door_shares_the_audio_door_one_rule():
    """``/human?type=echo`` is the second enqueue door; it must not invent a second key."""
    _new_gate(ENFORCE)
    payload = {"sessionid": SESSION, "type": "echo", "text": "在的在的",
               "response_id": "resp_txt", "segment_index": "0"}
    first, retry = post_json("/human", payload), post_json("/human", payload)
    assert first["code"] == 0 and "data" not in first, first
    assert retry["data"] == {"segment_gate": {
        "decision": DUPLICATE, "response_id": "resp_txt", "segment_index": 0,
        "session_id": SESSION, "reason": "SEGMENT_0_ALREADY_ADMITTED"}, "enqueued": False}, retry
    assert STUB_SESSIONS[SESSION].texts == ["在的在的"], STUB_SESSIONS[SESSION].texts


def interrupt_via_the_text_door_also_names_the_response():
    """``/human?interrupt=true`` goes through the same cancel owner."""
    _new_gate(ENFORCE)
    response = post_json("/human", {"sessionid": SESSION, "type": "echo", "text": "",
                                   "interrupt": True, "response_id": "resp_via_human",
                                   "reason": "manual_stop"})
    assert response["code"] == 0, response
    assert STUB_SESSIONS[SESSION].flushes == 1
    assert post_humanaudio(response_id="resp_via_human", segment_index="0")["data"]["enqueued"] is False


def the_read_only_admin_door_reports_the_running_mode():
    """:8010's mode must be confirmable without restarting anything."""
    gate = _new_gate(ENFORCE)
    post_json("/interrupt_talk", {"sessionid": SESSION, "response_id": "resp_admin"})
    payload = get_json("/api/admin/segment_gate?sessionid=" + SESSION)
    data = payload["data"]
    assert data["mode"] == ENFORCE and data["env"] == "AVATAR_SEGMENT_GATE", data
    assert data["session"]["cancelled"] == ["resp_admin"], data
    assert data["session"]["cancels"] == 1, data
    assert get_json("/api/admin/segment_gate")["data"]["mode"] == ENFORCE
    assert gate.mode == ENFORCE


def a_lost_ack_retry_of_the_same_wav_is_not_a_second_segment():
    """The client's own retry loop, replayed through the real route.

    ``avatar-sync.js`` freezes ``(response_id, segment_index)`` onto the job, so
    a resend after a lost ACK arrives with the same pair — here, twice, over two
    real sockets, and :8010 renders it once.
    """
    _new_gate(ENFORCE)
    posts = [post_humanaudio(response_id="resp_retry", segment_index="0") for _ in range(3)]
    assert [p["data"]["segment_gate"]["decision"] for p in posts] == [ACCEPT, DUPLICATE, DUPLICATE], posts
    assert len(STUB_SESSIONS[SESSION].audio) == 1, STUB_SESSIONS[SESSION].audio


def a_stale_response_never_queues_behind_the_new_one():
    """§H 要求 4 end-to-end at the boundary: A0, cancel(A), B0, then A1 — A must
    not slot in after B, and B keeps rendering."""
    _new_gate(ENFORCE)
    assert post_humanaudio(response_id="respA", segment_index="0")["data"]["enqueued"] is True
    post_json("/interrupt_talk", {"sessionid": SESSION, "response_id": "respA"})
    assert post_humanaudio(response_id="respB", segment_index="0")["data"]["enqueued"] is True
    late = post_humanaudio(response_id="respA", segment_index="1")
    assert late["data"]["enqueued"] is False, late
    order = [row[0] for row in rendered()]
    # the tagged upload says who actually rendered, in order: A's first segment,
    # then B's — and A's late second segment never slots in behind B.
    assert order == ["RIFFrespA#0", "RIFFrespB#0"], order


def the_route_and_this_test_share_one_ledger():
    """``_new_gate`` swaps a module attribute, so the handler has to read the gate
    per request.  If it ever captured a copy at import time, every mode-dependent
    check above would silently be testing one gate while the route used another."""
    gate = _new_gate(ENFORCE)
    assert live_routes.segment_gate is gate
    assert post_humanaudio(response_id="resp_ledger", segment_index="0")["data"]["enqueued"] is True
    seen = gate.snapshot(SESSION)["responses"]["resp_ledger"]["segments"]
    assert seen == 1, f"route wrote a different ledger object than {gate!r}: {gate.snapshot(SESSION)}"


def the_verdict_still_reaches_the_app_log():
    """The gate must stay *observable*, not merely correct.

    P2-B reads the live ``humanaudio segment gate mode=…`` line as the only
    production evidence that a real browser turn carried a
    ``(response_id, segment_index)`` pair — and this app configures exactly one
    sink: ``utils/logger.py`` attaches a FileHandler to the logger named
    ``utils.logger`` and never touches the root logger.  A verdict logged under a
    namespace that is not ``utils.logger`` or one of its children therefore
    disappears in silence.  That is precisely what happened after the refactor
    that moved these two ``logger.info`` calls out of ``routes.py`` (project
    logger) and into ``SegmentGate.admit_turn``, whose logger was a
    handler-less standalone name: the 2026-09-24 13:19 browser turn rendered,
    spoke, and left no gate line anywhere.

    Non-vacuity: the assertion counts records that actually arrive at
    ``utils.logger``'s handler chain.  Renaming the gate's logger back to a
    name outside that tree makes it fail, because the collector then sees
    nothing — a line nobody consumes cannot satisfy it.
    """
    import logging

    gate = _new_gate(ENFORCE)
    sink = logging.getLogger("utils.logger")
    assert any(isinstance(h, logging.FileHandler) for h in sink.handlers), (
        "utils.logger carries no FileHandler in this process, so this check would "
        "prove nothing about the production log -- investigate utils/logger.py "
        "rather than relaxing this assertion"
    )

    records: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    collector = _Collect()
    sink.addHandler(collector)
    try:
        post_humanaudio(response_id="resp_log", segment_index="0")
        post_humanaudio(response_id="resp_log", segment_index="0")
    finally:
        sink.removeHandler(collector)

    verdicts = [line for line in records if "segment gate" in line]
    assert len(verdicts) == 2, f"the gate ran but wrote nothing observable: {records}"
    assert ACCEPT in verdicts[0] and "mode=enforce" in verdicts[0], verdicts
    assert DUPLICATE in verdicts[1] and "rejected by segment gate" in verdicts[1], verdicts
    assert gate.snapshot(SESSION)["responses"]["resp_log"]["segments"] == 1


TESTS = [
    enforce_mode_renders_one_play_for_a_doubly_posted_segment,
    observe_mode_is_the_negative_control_and_renders_twice,
    untagged_upload_is_untouched_by_the_gate,
    a_named_cancel_blocks_a_reply_that_never_reached_the_queue,
    an_unnamed_cancel_is_the_control_that_still_lets_it_through,
    the_text_door_shares_the_audio_door_one_rule,
    interrupt_via_the_text_door_also_names_the_response,
    the_read_only_admin_door_reports_the_running_mode,
    a_lost_ack_retry_of_the_same_wav_is_not_a_second_segment,
    a_stale_response_never_queues_behind_the_new_one,
    the_route_and_this_test_share_one_ledger,
    the_verdict_still_reaches_the_app_log,
]

if __name__ == "__main__":
    SERVER.start()
    time.sleep(0.2)
    failures = 0
    print(f"real routes.py served read-only at {SERVER.base} (ephemeral, 127.0.0.1)")
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"  ok   {test.__name__}")
    SERVER.stop()
    if failures:
        print(f"avatar turn transport checks: FAIL ({failures} of {len(TESTS)})")
        raise SystemExit(1)
    print(f"avatar turn transport checks: PASS ({len(TESTS)})")
