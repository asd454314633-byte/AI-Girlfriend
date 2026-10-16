"""§7 fault A (REALTIME TRANSPORT DISCONNECTED) — offline regression.

Run::

    python runtime/tests/test_realtime_transport.py

Everything here is in-process and offline: no browser, no network, no GPU, no
``:8765``.  ``runtime/ui/app/s2s-realtime-client.js`` is imported *as shipped*
into a throwaway Node process whose globals are replaced by fakes (WebSocket,
AudioContext, audio worklets, the Agents SDK), so the reconnect loop, the link
watchdog and the response-generation guard are exercised for real rather than
re-implemented in Python.

What each check protects, mapped to the eight §7.A questions::

    handshake_failure_...              页面刷新/8765 未就绪: a failed dial must not
                                       wedge the client, and the caller still gets
                                       the real error.
    dropped_link_reconnects_...        自动重连 + 8765 重启: one close event turns
                                       into exactly one backoff loop that recovers.
    error_callback_never_recurses_...  the undici/Node lesson: tearing the socket
                                       down from inside its own error/close
                                       callback must not become a recursion.
    half_open_link_...                 心跳 / 网络瞬断: no close event and no inbound
                                       traffic, yet the call is dead — the watchdog
                                       has to say so.
    quiet_but_healthy_link_...         the same watchdog must not invent failures
                                       for a silent-but-alive pipeline.
    reconnect_flushes_playback_...     重连后旧 response 是否残留: the core risk —
                                       audio/text blocks of the previous call may
                                       not reach the speakers of the new one.
    keepalive_answer_...               a keepalive echo must not be painted as a
                                       server error.
    unified_cancel_...                 §7 H 统一 cancel: one barge-in makes ONE
                                       decision, which (a) puts exactly one
                                       ``response.cancel`` on the :8765 wire,
                                       (b) reaches the :8010 participant with the
                                       response id it must block, and (c) drops
                                       the interrupted response's stragglers
                                       without touching the next one.
    ui_state_machine_matches_...       UI 状态是否真实: every status the client can
                                       emit is painted by main.js, and none of them
                                       survives as "live" while the socket is down.
    provider_neutrality_...            the recovery path may not branch on a
                                       backend/TTS literal (§7 constraint 2).

Two of the structural checks below (``one_owner_decides_the_interrupt``,
``upload_fields_match_the_avatar_gate``) are the browser↔:8010 contract: they
compare the field names ``avatar-sync.js`` posts against the names
``server/idempotency.py`` reads, so neither end can rename its half of the
``(response_id, segment_index)`` key without failing here.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]
APP = RUNTIME / "ui" / "app"
CLIENT_JS = APP / "s2s-realtime-client.js"
MAIN_JS = APP / "main.js"
AVATAR_JS = APP / "avatar-sync.js"
GATE_PY = RUNTIME / "avatar" / "app" / "server" / "idempotency.py"
ROUTES_PY = RUNTIME / "avatar" / "app" / "server" / "routes.py"

# The idempotency key :8010 dedupes a /humanaudio upload on (task §7 H).
SEGMENT_KEY = frozenset({"response_id", "segment_index"})

# The harness lives in the audit tools folder on purpose: it is test scaffolding,
# not something the shipped UI may import.
HARNESS_JS = RUNTIME / "audit" / "system_v2" / "tools" / "fault_a_transport_harness.mjs"

TIMEOUT_S = 90

HARNESS = r'''
// Offline fake-transit for runtime/ui/app/s2s-realtime-client.js (§7 fault A).
// Written by runtime/tests/test_realtime_transport.py — do not import from the UI.
import { pathToFileURL } from "node:url";
import { readFileSync } from "node:fs";

const CLIENT_PATH = process.argv[2];
const WORK = process.argv[3] || "offline";
if (!/^[A-Za-z0-9_.-]+$/.test(WORK)) throw new Error(`unusable work name: ${WORK}`);
// The caller reads AUDIO_WORKLET_VERSION out of the client and hands it here, so
// the fake worklet always answers with the version the client demands. Hardcoding
// it is how this fixture rotted once already: bump the constant in the client and
// every scenario dies on the mic gate instead of on a real transport regression.
// Standalone runs (no argv[5]) fall back to parsing the same constant.
const WORKLET_VERSION = process.argv[5]
  || (/AUDIO_WORKLET_VERSION\s*=\s*"([^"]+)"/.exec(readFileSync(CLIENT_PATH, "utf8")) || [, "test-worklet"])[1];

// ── browser surface ─────────────────────────────────────────────────────────
const noop = () => {};
const log = { debug: [], info: [], warn: [], error: [], log: [] };
console.debug = (a, ...r) => { log.debug.push([String(a), ...r.map(String)]); };
console.info = (a, ...r) => { log.info.push(String(a)); };
console.warn = (a, ...r) => { log.warn.push(String(a)); };
console.error = (a, ...r) => { log.error.push(String(a)); };
globalThis.requestAnimationFrame = () => 0;
globalThis.cancelAnimationFrame = noop;
globalThis.performance = globalThis.performance || { now: () => Date.now() };
// Only what the orb visualiser touches: it writes CSS custom properties and
// never runs its rAF loop here, because requestAnimationFrame is a no-op above.
globalThis.document = {
  documentElement: { style: { setProperty: noop, removeProperty: noop, getPropertyValue: () => "" } },
};

class FakeTrack {
  constructor() { this.enabled = true; this.readyState = "live"; }
  stop() { this.readyState = "ended"; }
  addEventListener() {}
  removeEventListener() {}
}
class FakeMediaStream {
  constructor() { this._t = [new FakeTrack()]; }
  getAudioTracks() { return this._t; }
}

/** The socket the browser would own: only the two numbers the watchdog reads. */
class FakeSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 1;          // OPEN
    this.bufferedAmount = 0;      // bytes queued for the peer
    this.closed = 0;
    this.sent = [];
  }
  send(text) { this.sent.push(text); }
  close() { this.closed += 1; this.readyState = 3; }
  addEventListener() {}
  removeEventListener() {}
}
globalThis.WebSocket = FakeSocket;

class FakeAnalyser {
  constructor() { this.fftSize = 0; this.smoothingTimeConstant = 0; }
  getByteTimeDomainData(buf) { buf.fill(128); }
  connect() {}
  disconnect() {}
}
class FakePort {
  constructor(node) { this.node = node; }
  postMessage(message) { this.node.parent.messages.push({ kind: message.kind, at: Date.now() }); }
}
class FakeCaptureNode {
  constructor(parent) {
    this.parent = parent;
    this.port = new FakePort(this);
    this.port.onmessage = null;
    // The worklet answers the probe with its real rates, as shipped.
    setTimeout(() => {
      this.port.onmessage?.({ data: { kind: "capture-config", inputRate: 24000, outputRate: 24000, version: WORKLET_VERSION } });
    }, 0);
    // ...and keeps delivering 40 ms mic frames for the rest of the run: the
    // watchdog's stall signal only exists because the microphone is talking.
    this.micTimer = setInterval(() => this.port.onmessage?.({ data: new ArrayBuffer(1_920) }), 5);
    if (this.micTimer.unref) this.micTimer.unref();
  }
  connect() {}
  disconnect() {}
  postMessage() {}
}
class FakePlaybackNode {
  constructor(parent) {
    this.parent = parent;
    this.port = {
      postMessage: (message) => {
        parent.messages.push({
          kind: message.kind,
          samples: message.samples ? message.samples.length : 0,
          at: Date.now(),
        });
      },
    };
  }
  connect() {}
  disconnect() {}
}
class FakeAudioContext {
  constructor(backend) { this.state = "running"; this.backend = backend; this.destination = {}; }
  async resume() { this.state = "running"; }
  createMediaStreamSource() { return new FakeAnalyser(); }
  createAnalyser() { return new FakeAnalyser(); }
  get audioWorklet() {
    const backend = this.backend;
    return { addModule: async () => { backend.modules.push("worklet"); } };
  }
  async close() { this.state = "closed"; }
}

// ── fake Agents SDK ─────────────────────────────────────────────────────────
const world = {
  transports: [],
  sessions: [],
  modules: [],
  dialPlan: [],       // per dial: "ok" | "refused" | "throw"
  dials: 0,
  live: null,
  throwOnClose: false,
  closeEmitsDisconnected: false,
  answerKeepalive: false,
  messages: [],       // every message posted to an audio node (shared history)
  storage: new Map([[WORK, "1"]]),
};

class FakeTransport {
  constructor(options) {
    this.options = options;
    this.handlers = new Map();
    this.events = [];
    this.socket = null;
    world.transports.push(this);
  }
  on(type, fn) {
    if (!this.handlers.has(type)) this.handlers.set(type, []);
    this.handlers.get(type).push(fn);
  }
  emit(type, payload) {
    for (const fn of this.handlers.get(type) || []) fn(type === "*" ? payload || { type } : payload);
  }
  sendEvent(event) {
    this.events.push(event);
    if (!this.socket || this.socket.readyState !== 1) throw new Error("WebSocket is not connected");
    this.socket.send(JSON.stringify(event));
    if (world.answerKeepalive && typeof event?.type === "string" && event.type !== "session.update") {
      // What this endpoint actually does with an event type it does not know
      // (websocket_router.py:354-362): answer with a generic error event.
      this.emit("*", {
        type: "error",
        event_id: "event_probe",
        error: { type: "unknown_or_invalid_event", message: `Unknown or invalid event: ${event.type}` },
      });
    }
  }
  async _dial(opts) {
    this.url = opts.url;
    this.socket = await this.options.createWebSocket({ url: opts.url });
    world.dials += 1;
    const outcome = world.dialPlan.shift() ?? "ok";
    if (outcome === "refused") {
      // The peer is not listening: the socket opens, then dies without ever
      // acknowledging the session — no `close` event to hang a retry on.
      throw new Error(`Realtime WebSocket handshake failed (${opts.url})`);
    }
    if (outcome === "throw") throw new Error("connect() exploded");
    world.live = this;
    this.emit("connection_change", "connected");
  }
}

class FakeSession {
  constructor(agent, options) {
    this.agent = agent;
    this.transport = options.transport;
    this.config = options.config;
    this.handlers = new Map();
    this.closeCalls = 0;
    this.sentMessages = [];
    this.sentAudio = 0;
    world.sessions.push(this);
  }
  on(type, fn) {
    if (!this.handlers.has(type)) this.handlers.set(type, []);
    this.handlers.get(type).push(fn);
  }
  emit(type, payload) {
    for (const fn of this.handlers.get(type) || []) fn(payload);
  }
  async connect(opts) { await this.transport._dial(opts); }
  sendMessage(text) { this.sentMessages.push(text); }
  sendAudio() { this.sentAudio += 1; }
  addImage() {}
  async updateAgent() {}
  close() {
    this.closeCalls += 1;
    if (world.throwOnClose) throw new Error("close() re-fired an error event");
    if (world.closeEmitsDisconnected) this.transport.emit("connection_change", "disconnected");
  }
}

globalThis.OpenAIAgentsRealtime = {
  OpenAIRealtimeWebSocket: FakeTransport,
  OpenAIRealtimeWebRTC: FakeTransport,
  RealtimeSession: FakeSession,
  RealtimeAgent: class { constructor(options) { Object.assign(this, options); } },
  tool: (definition) => definition,
};
globalThis.AudioWorkletNode = function (ctx, name) {
  return name === "mic-capture" ? new FakeCaptureNode(ctx.backend) : new FakePlaybackNode(ctx.backend);
};
globalThis.localStorage = {
  getItem: (k) => (world.storage.has(k) ? world.storage.get(k) : null),
  setItem: (k, v) => world.storage.set(k, v),
  removeItem: (k) => world.storage.delete(k),
};
globalThis.window = globalThis;
globalThis.location = { href: `file://${WORK}/index.html` };

// ── the module under test ───────────────────────────────────────────────────
const { S2sRealtimeClient, s2sTurnControl } = await import(pathToFileURL(CLIENT_PATH).href);

const RESILIENCE = {
  baseDelayMs: 4, maxDelayMs: 16, jitterMs: 1, maxAttempts: 10, heartbeatMs: 25, missLimit: 2,
};

function newClient(extra = {}) {
  return new S2sRealtimeClient({
    transport: "websocket",
    directUrl: "ws://127.0.0.1:8765/v1/realtime",
    voice: "test",
    instructions: "test",
    micStream: new FakeMediaStream(),
    audioContext: new FakeAudioContext(world),
    resilience: { ...RESILIENCE, ...extra },
  });
}

/** Everything the page would see, in order, with the reason it changed. */
function record(client) {
  const events = [];
  for (const type of ["status", "transcript", "error", "server-error", "reconnect", "reconnected", "transport-drop", "response-finished"]) {
    client.addEventListener(type, (e) => {
      const d = e.detail || {};
      events.push({
        type,
        status: d.status,
        reason: d.reason ?? d.error?.message,
        attempt: d.attempt,
        role: d.role,
        text: d.text,
      });
    });
  }
  return events;
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function waitFor(predicate, ms = 1500, label = "condition") {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await wait(3);
  }
  throw new Error(`timeout waiting for ${label}`);
}

/** A drop is processed on a later task than the callback that reported it, so a
 *  test must never read "still connected" as "never dropped": wait for the drop
 *  first, then for the recovery. */
async function waitForDrop(events, ms = 2000) {
  await waitFor(() => events.some((e) => e.type === "transport-drop"), ms, "transport-drop");
}

function pcm(samples) {
  const buffer = new ArrayBuffer(samples * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < samples; i += 1) view.setInt16(i * 2, 1000, true);
  return buffer;
}

function playedBlocks() {
  return world.messages.filter((m) => m.kind === "audio");
}

const steps = {};

// 握手失败要能重试 (and never wedge the client) ──────────────────────────────
steps.handshake_failure = async () => {
  const client = newClient();
  const events = record(client);
  world.dialPlan = ["refused"];
  let thrown = null;
  try { await client.connect(); } catch (err) { thrown = err; }
  assert(thrown, "a failed first dial must reach the caller, not be swallowed");
  assert(/handshake failed/.test(String(thrown.message)), `real cause reported: ${thrown.message}`);
  assert(client.status === "connecting", `UI left on "connecting" -> ${client.status}`);
  assert(client.diagnostics().drops === 1, "the dead dial is retired");
  assert(!events.some((e) => e.type === "error"), "no duplicate error event on top of the rejection");
  assert(world.transports.length === 1, `one dial attempted, got ${world.transports.length}`);
  // The same client can still be dialled once the endpoint is up: nothing wedged.
  world.dialPlan = [];
  await client.connect();
  assert(client.status === "connected", client.status);
  assert(client.diagnostics().generation >= 3, `generation advanced per dial: ${JSON.stringify(client.diagnostics())}`);
  await client.close();
  return { dials: world.dials, generation: client.diagnostics().generation };
};

// 自动重连 + 8765 重启：a drop turns into exactly one recovering backoff loop ──
steps.reconnect_after_drop = async () => {
  const client = newClient({ maxAttempts: 10 });
  const events = record(client);
  await client.connect();
  world.dialPlan = ["refused", "refused"];      // endpoint still booting for 2 tries
  world.live.emit("connection_change", "disconnected");
  await waitForDrop(events);
  await waitFor(() => client.status === "connected", 3000, "recovery");
  const attempts = events.filter((e) => e.type === "reconnect").map((e) => e.attempt);
  assert(attempts.length >= 3, `backoff retried past the failures: ${JSON.stringify(attempts)}`);
  assert(!events.some((e) => e.type === "error"), "a recovered link must never report an error");
  const seen = events.filter((e) => e.type === "status").map((e) => e.status);
  assert(seen.includes("reconnecting"), `UI shown reconnecting: ${JSON.stringify(seen)}`);
  assert(seen.indexOf("reconnecting") < seen.lastIndexOf("connected"), "reconnecting precedes the recovery");
  // no live state may be painted while the socket is down
  const cut = seen.indexOf("reconnecting");
  assert(!seen.slice(cut, seen.lastIndexOf("connected")).some((s) => ["listening", "ai-speaking", "user-speaking"].includes(s)),
    `stale live state during reconnect: ${JSON.stringify(seen)}`);
  assert(world.dials === 4, `dials = first + 2 failures + success, got ${world.dials}`);
  return { attempts, dials: world.dials, seen, generation: client.diagnostics().generation };
};

// error/close 回调里再 close() 造成递归 ───────────────────────────────────────
steps.no_recursive_close = async () => {
  const client = newClient();
  const events = record(client);
  await client.connect();
  const session = world.sessions[0];
  // The worst case this class of bug needs: closing the socket fires the dead
  // transport's own `connection_change` again *and* throws.
  world.throwOnClose = true;
  world.closeEmitsDisconnected = true;
  world.live.emit("connection_change", "disconnected");
  await waitForDrop(events);
  await waitFor(() => client.status === "connected", 2000, "recovery after a hostile close");
  world.throwOnClose = false;
  world.closeEmitsDisconnected = false;
  assert(session.closeCalls <= 2, `close() called ${session.closeCalls}x — recursion is back`);
  assert(events.filter((e) => e.type === "transport-drop").length === 1, "one drop, not a storm");
  assert(events.filter((e) => e.type === "error").length === 0, "no error surfaced");
  assert(!log.error.length, `uncaught noise in console.error: ${JSON.stringify(log.error)}`);
  const drops = events.filter((e) => e.type === "transport-drop");
  assert(drops.length === 1 && drops[0].reason === "Realtime transport disconnected", JSON.stringify(drops));
  return { closeCalls: session.closeCalls, drops: drops.length };
};

// 心跳：半开链路（没有 close 事件、没有入站流量）必须判失联 ──────────────────
steps.watchdog_stall = async () => {
  const client = newClient({ heartbeatMs: 25, missLimit: 2 });
  const events = record(client);
  await client.connect();
  const socket = world.live.socket;
  // Nobody drains the kernel buffer: every tick has more unsent mic audio than
  // the last one, and nothing has come back from the peer.
  const grow = setInterval(() => { socket.bufferedAmount += 20_000; }, 8);
  if (grow.unref) grow.unref();
  try {
    await waitForDrop(events);
    await waitFor(() => client.status === "connected", 3000, "redial after a stalled link");
  } finally {
    clearInterval(grow);
  }
  const drop = events.find((e) => e.type === "transport-drop");
  assert(drop, "the watchdog must declare the link dead");
  assert(/outbound stalled/.test(drop.reason), `stall reported as the cause: ${drop.reason}`);
  assert(client.diagnostics().link.misses >= 1, JSON.stringify(client.diagnostics().link));
  assert(events.filter((e) => e.type === "error").length === 0, "recovered, so no error");
  return { reason: drop.reason, dials: world.dials };
};

// 心跳的反面：安静但健康的链路不得被误判 ────────────────────────────────────
steps.quiet_but_healthy = async () => {
  const client = newClient({ heartbeatMs: 20, missLimit: 2 });
  const events = record(client);
  await client.connect();
  const before = world.dials;
  world.live.socket.bufferedAmount = 0;                 // kernel drains everything
  await wait(300);                                      // ~15 ticks of pure silence
  assert(client.status === "connected", `silent-but-alive link was dropped: ${client.status}`);
  assert(world.dials === before, "no redial for a healthy idle socket");
  assert(!events.some((e) => e.type === "transport-drop"), JSON.stringify(events.filter((e) => e.type === "transport-drop")));
  assert(!events.some((e) => e.type === "server-error"), "idle silence is not an error");
  return { ticksObserved: Math.round(300 / 20), dials: world.dials - before };
};

// keepalive 的回声不得被报成 server error（echoProbe 打开时）──────────────────
steps.keepalive_pong = async () => {
  const client = newClient({ heartbeatMs: 20, missLimit: 2, echoProbe: true });
  const events = record(client);
  world.answerKeepalive = true;
  await client.connect();
  await wait(200);
  world.answerKeepalive = false;
  const d = client.diagnostics();
  assert(d.keepalive.sent >= 1, `probe was sent: ${JSON.stringify(d.keepalive)}`);
  assert(d.keepalive.answered >= 1, `probe answered: ${JSON.stringify(d.keepalive)}`);
  assert(!events.some((e) => e.type === "server-error"), "keepalive echo surfaced as a server error");
  assert(client.status === "connected", `answered probes must not drop the link: ${client.status}`);
  assert(world.live.events.some((e) => e.type === "realtime.keepalive"), "probe is a generic GA event, no vendor branch");
  return d.keepalive;
};

// 重连后旧 response 是否残留（本题核心）─────────────────────────────────────
steps.stale_generation_blocks = async () => {
  const client = newClient();
  const events = record(client);
  await client.connect();
  const old = { transport: world.live, session: world.sessions[world.sessions.length - 1] };
  old.transport.emit("*", { type: "response.created", response: { id: "resp_OLD", status: "in_progress" } });
  old.session.emit("audio", { data: pcm(16), responseId: "resp_OLD" });
  old.transport.emit("*", { type: "response.audio_transcript.delta", response_id: "resp_OLD", delta: "上一通" });
  const playedBefore = playedBlocks().length;
  assert(playedBefore === 1, `the live turn plays: ${JSON.stringify(playedBefore)}`);

  // ---- the link dies mid-answer, then comes back --------------------------
  world.dialPlan = [];
  world.live.emit("connection_change", "disconnected");
  await waitForDrop(events);
  await waitFor(() => client.status === "connected", 3000, "new generation live");
  const d = client.diagnostics();
  assert(d.generation >= 3, `generation advanced across the drop: ${d.generation}`);
  const flushed = world.messages.filter((m) => m.kind === "clear");
  assert(flushed.length >= 1, "the playback worklet is flushed at the dial boundary");

  const playedAfterReconnect = playedBlocks().length;
  // every residue route of the previous call, tried after the new one is up
  old.session.emit("audio", { data: pcm(32), responseId: "resp_OLD" });          // old session object
  old.transport.emit("*", { type: "response.audio_transcript.delta", response_id: "resp_OLD", delta: "残留" });
  old.transport.emit("*", { type: "response.done", response: { id: "resp_OLD", status: "completed" } });
  world.live.emit("*", { type: "response.audio_transcript.delta", response_id: "resp_OLD", delta: "残留2" });
  world.live.emit("*", { type: "conversation.item.input_audio_transcription.completed", item_id: "item_OLD", transcript: "旧问题" });
  // and the way a server bug could actually deliver it: a late delta on the NEW socket
  const before = playedBlocks().length;
  world.sessions[world.sessions.length - 1].emit("audio", { data: pcm(64), responseId: "resp_OLD" });
  assert(playedBlocks().length === before, "stale audio reached the speakers through the new socket");

  const d2 = client.diagnostics();
  assert(d2.staleDropped.audio >= 1, `stale audio counted as dropped: ${JSON.stringify(d2.staleDropped)}`);
  assert(d2.staleDropped.text >= 1, `stale text counted as dropped: ${JSON.stringify(d2.staleDropped)}`);
  assert(playedBlocks().length === playedBefore, "no extra audio played after the drop");
  assert(!events.slice(events.findIndex((e) => e.type === "transport-drop")).some((e) => e.type === "transcript" && e.role === "assistant"),
    "a stale assistant transcript reached the history panel");

  // ---- the new call must still be able to talk ----------------------------
  world.live.emit("*", { type: "response.created", response: { id: "resp_NEW", status: "in_progress" } });
  const fresh = world.sessions[world.sessions.length - 1];
  fresh.emit("audio", { data: pcm(16), responseId: "resp_NEW" });
  world.live.emit("*", { type: "response.audio_transcript.delta", response_id: "resp_NEW", delta: "新一通" });
  assert(playedBlocks().length === playedBefore + 1, "the new generation's audio was wrongly dropped");
  const last = events.filter((e) => e.type === "transcript").pop();
  assert(last.role === "assistant" && last.text.includes("新一通"), JSON.stringify(last));
  await client.close();
  return {
    generation: client.diagnostics().generation,
    staleDropped: client.diagnostics().staleDropped,
    playedBeforeDrop: playedBefore,
    playedAtRecovery: playedAfterReconnect,
    played: playedBlocks().length,
  };
};

// 8765 重启后浏览器只给 readyState，不给 connection_change ────────────────────
steps.restart_socket_closed = async () => {
  const client = newClient({ heartbeatMs: 20, missLimit: 2 });
  const events = record(client);
  await client.connect();
  world.live.socket.readyState = 3;                     // CLOSED, silently
  await waitForDrop(events);
  await waitFor(() => client.status === "connected", 3000, "redial after a silent close");
  const drop = events.find((e) => e.type === "transport-drop");
  assert(/readyState=3/.test(drop.reason), `cause reported from the socket itself: ${drop.reason}`);
  assert(events.filter((e) => e.type === "error").length === 0, "recovered without an error");
  return { reason: drop.reason };
};

// 恢复预算用尽后必须真的报错（UI 不得一直转圈）───────────────────────────────
steps.give_up_after_attempts = async () => {
  const client = newClient({ maxAttempts: 3, baseDelayMs: 2, maxDelayMs: 4, jitterMs: 1 });
  const events = record(client);
  await client.connect();
  world.dialPlan = ["refused", "refused", "refused", "refused", "refused"];
  world.live.emit("connection_change", "disconnected");
  await waitFor(() => client.status === "error", 3000, "giving up");
  const errors = events.filter((e) => e.type === "error");
  assert(errors.length === 1, `exactly one fatal error: ${errors.length}`);
  assert(/gave up after 3 reconnect attempts/.test(errors[0].reason), errors[0].reason);
  assert(events.filter((e) => e.type === "reconnect").length === 3, "every attempt was painted");
  assert(client.diagnostics().attempt === 3, JSON.stringify(client.diagnostics()));
  return { attempts: 3, reason: errors[0].reason };
};

// 用户主动挂断必须取消在飞的恢复循环 ────────────────────────────────────────
steps.close_cancels_retry = async () => {
  const client = newClient({ maxAttempts: 10, baseDelayMs: 40, maxDelayMs: 60, jitterMs: 1 });
  const events = record(client);
  await client.connect();
  world.dialPlan = ["refused", "refused", "refused"];
  world.live.emit("connection_change", "disconnected");
  await waitFor(() => client.status === "reconnecting", 1000, "reconnecting");
  await client.close();
  const dials = world.dials;
  await wait(200);
  assert(world.dials === dials, "a hung-up client must not keep dialling");
  assert(client.status === "closed", client.status);
  assert(!events.some((e) => e.type === "error"), "an intentional close is not an error");
  return { dialsAfterClose: world.dials - dials };
};

// Characterisation probe (no assertion): what a client that has no recovery path
// at all does when the transport dies. Run against the pre-fix backup to record
// the baseline: one dial, an `error` event, and no further attempts.
steps.legacy_baseline = async () => {
  const client = newClient();
  const events = record(client);
  await client.connect();
  world.live.emit("connection_change", "disconnected");
  await wait(400);
  return {
    dials: world.dials,
    statuses: events.filter((e) => e.type === "status").map((e) => e.status),
    errors: events.filter((e) => e.type === "error").length,
  };
};

// ── unified cancel (§7 H): one decision, both ends ---------------------------
// The barge-in is decided ONCE.  This scenario registers a participant that
// stands in for avatar-sync.js's `cancelAvatarTurn` (the :8010 half) and then
// drives the same event stream the page would see, asserting:
//   * exactly one `response.cancel` reaches the :8765 wire, carrying the id;
//   * exactly one decision reaches the :8010 participant, carrying the same id
//     plus the reason, so the two logs describe one event;
//   * local playback is flushed (the worklet gets a `clear`);
//   * stragglers of the interrupted response die at the gate, while the NEXT
//     response still plays — the cancel must not become a global mute.
steps.unified_cancel = async () => {
  const calls = [];
  // `turnControl.seq` is process-wide, so every count below is a delta: the
  // scenario must read the same way whether it runs alone or inside "all".
  const seqBefore = s2sTurnControl.seq;
  s2sTurnControl.registerParticipant("avatar-renderer", (decision) => {
    calls.push({ responseId: decision.responseId, reason: decision.reason, seq: decision.seq });
  });
  const client = newClient();
  const events = record(client);
  await client.connect();
  const session = world.sessions[world.sessions.length - 1];

  world.live.emit("*", { type: "response.created", response: { id: "resp_A", status: "in_progress" } });
  session.emit("audio", { data: pcm(16), responseId: "resp_A" });
  const played = playedBlocks().length;
  assert(played === 1, `resp_A is live and playing: ${played}`);

  // The barge-in.  `speech_started` carries no response_id, so only the shared
  // owner can know which response is in flight.
  world.live.emit("*", { type: "input_audio_buffer.speech_started", item_id: "item_2" });
  const wire = () => world.live.events.filter((e) => e.type === "response.cancel");
  assert(wire().length === 1, `exactly one response.cancel on the wire: ${JSON.stringify(wire())}`);
  assert(wire()[0].event_id === "response_cancel_resp_A", JSON.stringify(wire()[0]));
  assert(calls.length === 1 && calls[0].responseId === "resp_A", `:8010 told which response: ${JSON.stringify(calls)}`);
  assert(calls[0].reason === "user_interrupt", `reason travels too: ${JSON.stringify(calls)}`);
  assert(world.messages.some((m) => m.kind === "clear"), "local playback flushed by the same decision");
  assert(events.some((e) => e.type === "status" && e.status === "user-speaking"), "the user turn still paints");

  // A second trigger — VAD echo plus a manual stop — must not decide twice.
  world.live.emit("*", { type: "input_audio_buffer.speech_started", item_id: "item_3" });
  client.cancelTurn("manual_stop");
  assert(wire().length === 1, `duplicate cancel: ${JSON.stringify(wire())}`);
  assert(calls.length === 1, `duplicate :8010 cancel: ${JSON.stringify(calls)}`);
  assert(client.diagnostics().turn.cancels === seqBefore + 1, JSON.stringify(client.diagnostics().turn));

  // The interrupted response's tail must not bleed into the next turn.
  const droppedBefore = client.diagnostics().staleDropped.audio;
  session.emit("audio", { data: pcm(32), responseId: "resp_A" });
  world.live.emit("*", { type: "response.audio_transcript.delta", response_id: "resp_A", delta: "残留" });
  const d = client.diagnostics();
  assert(d.staleDropped.audio === droppedBefore + 1, JSON.stringify(d.staleDropped));
  assert(playedBlocks().length === played, "the cancelled response still reached the speakers");
  assert(!events.slice(-3).some((e) => e.type === "transcript" && e.role === "assistant"), "stale transcript painted");

  // ...and the new response is unaffected: the cancel is scoped to one id.
  world.live.emit("*", { type: "response.created", response: { id: "resp_B", status: "in_progress" } });
  session.emit("audio", { data: pcm(16), responseId: "resp_B" });
  world.live.emit("*", { type: "response.audio_transcript.delta", response_id: "resp_B", delta: "新一通" });
  assert(playedBlocks().length === played + 1, "the next response was muted by the interrupt");
  const last = events.filter((e) => e.type === "transcript").pop();
  assert(last && last.text.includes("新一通"), JSON.stringify(last));
  await client.close();
  return {
    wireCancels: wire().length,
    avatarCalls: calls,
    cancels: client.diagnostics().turn.cancels,
    participants: client.diagnostics().turn.participants,
    staleDropped: client.diagnostics().staleDropped,
    played: playedBlocks().length,
  };
};

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

// ── run ─────────────────────────────────────────────────────────────────────
const only = process.argv[4] || "all";
const names = only === "all" ? Object.keys(steps) : [only];
const results = {};
for (const name of names) {
  for (const list of [world.transports, world.sessions, world.messages]) list.length = 0;
  world.dials = 0;
  world.live = null;
  world.dialPlan = [];
  world.throwOnClose = false;
  world.closeEmitsDisconnected = false;
  world.answerKeepalive = false;
  try {
    const detail = await steps[name]();
    results[name] = { ok: true, detail: detail || {} };
  } catch (err) {
    results[name] = { ok: false, error: `${err && err.stack ? String(err.stack).split("\n").slice(0, 3).join(" | ") : err}` };
  }
}
console.log(`HARNESS_JSON_BEGIN${JSON.stringify(results)}HARNESS_JSON_END`);
process.exit(Object.values(results).every((r) => r.ok) ? 0 : 1);
'''


# ── helpers ──────────────────────────────────────────────────────────────────

def _node() -> str:
    found = shutil.which("node") or shutil.which("node.exe")
    if not found:
        raise AssertionError("node is not on PATH — this regression needs Node, not a browser")
    return found


def _worklet_version() -> str:
    """The version string the mic gate demands, read from the client itself.

    The fake worklet has to answer with exactly this or every scenario dies on
    `Microphone sample-rate mismatch` instead of on a transport regression.  A
    missing constant is a hard error, not a default: that is how this fixture
    rotted before (client bumped to `24k-20260924-diag2`, fixture still said
    `24k-20260901-1`, 10 of 14 checks failed for an unrelated reason).
    """
    match = re.search(r'AUDIO_WORKLET_VERSION\s*=\s*"([^"]+)"', CLIENT_JS.read_text(encoding="utf-8"))
    if not match:
        raise AssertionError("AUDIO_WORKLET_VERSION no longer exists in s2s-realtime-client.js")
    return match.group(1)


def _write_harness() -> None:
    """(Re)materialise the harness so the on-disk copy can never go stale.

    Writing it only when absent meant a hand-edited or older copy silently won.
    A pinned copy that differs from the embedded text is rewritten, which also
    gives `--emit-harness` a single code path.
    """
    HARNESS_JS.parent.mkdir(parents=True, exist_ok=True)
    if HARNESS_JS.exists() and HARNESS_JS.read_text(encoding="utf-8") == HARNESS:
        return
    HARNESS_JS.write_text(HARNESS, encoding="utf-8", newline="\n")


def _run(scenario: str = "all", work: str = "offline") -> dict:
    _write_harness()
    proc = subprocess.run(
        [_node(), str(HARNESS_JS), str(CLIENT_JS), work, scenario, _worklet_version()],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_S,
    )
    match = re.search(r"HARNESS_JSON_BEGIN(.*)HARNESS_JSON_END", proc.stdout or "", re.S)
    if not match:
        raise AssertionError(
            f"node harness produced no verdict (rc={proc.returncode})\n"
            f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}"
        )
    results = json.loads(match.group(1))
    failed = {name: r for name, r in results.items() if not r.get("ok")}
    if failed:
        detail = "\n".join(f"  {name}: {r['error']}" for name, r in failed.items())
        raise AssertionError(f"harness scenario(s) failed:\n{detail}\nstderr: {proc.stderr[-400:]}")
    return results


def _scenario(name: str) -> dict:
    return _run(name)[name]


# ── transport-behaviour checks (driven through the fakes) ────────────────────

def handshake_failure_is_reported_and_never_wedges_the_client():
    """一个失败的拨号必须把真实原因交给调用方，并把客户端留在可再拨的状态。"""
    out = _scenario("handshake_failure")
    assert out["ok"], out
    assert out["detail"]["dials"] >= 2, out["detail"]


def dropped_link_reconnects_with_backoff_and_without_an_error_event():
    """一次 connection_change('disconnected') 只变成一条恢复循环。"""
    out = _scenario("reconnect_after_drop")
    assert out["ok"], out
    assert out["detail"]["dials"] == 4, out["detail"]


def error_callback_never_recurses_into_close():
    """死掉的 socket 在自己的 close 回调里再次抛错/再次 disconnected。"""
    out = _scenario("no_recursive_close")
    assert out["ok"], out
    assert out["detail"]["closeCalls"] <= 2, out["detail"]
    assert out["detail"]["drops"] == 1, out["detail"]


def half_open_link_is_declared_dead_by_the_watchdog():
    """没有 close 事件、没有入站流量、只有堆积的发送缓冲 —— 仍然要判失联。"""
    out = _scenario("watchdog_stall")
    assert out["ok"], out
    assert "outbound stalled" in out["detail"]["reason"], out["detail"]


def quiet_but_healthy_link_is_not_dropped():
    """哨兵不得为"安静但活着"的链路编造故障（8765 空闲时什么都不发）。"""
    out = _scenario("quiet_but_healthy")
    assert out["ok"], out
    assert out["detail"]["dials"] == 0, out["detail"]


def keepalive_echo_is_not_reported_as_a_server_error():
    """探测的回声是活性证明，不是错误；且探测与厂商无关。"""
    out = _scenario("keepalive_pong")
    assert out["ok"], out
    assert out["detail"]["sent"] >= 1 and out["detail"]["answered"] >= 1, out["detail"]


def reconnect_flushes_playback_and_drops_previous_generations_blocks():
    """核心判据：世代号在重连时推进，上一通的音频块/文本块计数级丢弃。"""
    out = _scenario("stale_generation_blocks")
    assert out["ok"], out
    detail = out["detail"]
    dropped = detail["staleDropped"]
    assert dropped["audio"] >= 1 and dropped["text"] >= 1, detail
    assert detail["generation"] >= 3, detail
    # The new call plays exactly one block: nothing from the previous generation
    # reached the speakers, and the new generation's own audio was not dropped.
    assert detail["playedAtRecovery"] == detail["playedBeforeDrop"], detail
    assert detail["played"] == detail["playedBeforeDrop"] + 1, detail


def restart_of_the_endpoint_recovered_without_a_close_event():
    """只有 readyState 变化的重启（浏览器不给 connection_change）。"""
    out = _scenario("restart_socket_closed")
    assert out["ok"], out
    assert "readyState=3" in out["detail"]["reason"], out["detail"]


def exhausted_retry_budget_reports_the_truth():
    """预算用尽要真的报错，UI 不能一直转圈。"""
    out = _scenario("give_up_after_attempts")
    assert out["ok"], out
    assert "gave up after 3" in out["detail"]["reason"], out["detail"]


def user_close_cancels_an_in_flight_recovery():
    """挂断优先级高于恢复：不得继续拨号、不得报错误。"""
    out = _scenario("close_cancels_retry")
    assert out["ok"], out
    assert out["detail"]["dialsAfterClose"] == 0, out["detail"]


# ── structural checks: the UI may only paint what the transport really does ──

def _statuses_the_client_can_emit() -> set:
    src = CLIENT_JS.read_text(encoding="utf-8")
    return set(re.findall(r'_setStatus\("([a-z-]+)"\)', src)) | {"connected", "error", "reconnecting"}


def _client_status_cases(main_src: str) -> set:
    start = main_src.index("function onClientStatus")
    body = main_src[start: main_src.index("\nasync function teardown", start)]
    found = set(re.findall(r'case "([a-z-]+)":', body))
    # `case "creating-session": case "connecting":` share a branch; both are matched.
    return found


def ui_state_machine_covers_every_real_transport_state():
    main_src = MAIN_JS.read_text(encoding="utf-8")
    emitted = _statuses_the_client_can_emit()
    painted = _client_status_cases(main_src)
    unhandled = emitted - painted - {"closed"}  # "closed" is deliberately owned by teardown()
    assert not unhandled, f"client can emit statuses the page never paints: {sorted(unhandled)}"


def reconnecting_state_is_not_treated_as_live():
    main_src = MAIN_JS.read_text(encoding="utf-8")
    assert '"reconnecting"' in main_src, "no reconnecting state in the page at all"
    block = main_src[main_src.index("const STATE_VIEWS"): main_src.index("const LIVE_STATES")]
    keys = set(re.findall(r'^\s{2}"?([a-z-]+)"?:', block, re.M))
    typedef = re.search(r'@typedef \{([^}]*)\} AppState', main_src)
    assert typedef, "AppState typedef missing"
    app_states = set(re.findall(r'"([a-z-]+)"', typedef.group(1)))
    assert app_states <= keys, f"STATE_VIEWS/STATE_CLASS missing: {sorted(app_states - keys)}"
    live = main_src[main_src.index("const LIVE_STATES"):]
    live = live[: live.index(";")]
    assert "reconnecting" not in live, "a dead socket must not count as a live call"
    assert "connecting" not in live and "error" not in live, "pre-existing invariant broken"


def text_input_cannot_spawn_a_second_generation():
    main_src = MAIN_JS.read_text(encoding="utf-8")
    start = main_src.index("async function startTextConversation")
    body = main_src[start: main_src.index("\n/** @param {number} position", start)]
    assert 'currentState === "reconnecting"' in body, (
        "typing during a recovery would build a second client and mix generations"
    )


def recovery_path_carries_no_provider_literal():
    """§7 约束 2：恢复逻辑不得按厂商/TTS 名字分支，能力只能来自注册表清单。"""
    branchy = re.compile(
        r"""(?:===?|!==?)\s*['"](?:fish|qwen3|qwen3_tts|higgs|cosy|openaudio|sambert|xtts)['"]"""
        r"""|['"](?:fish|qwen3|qwen3_tts|higgs|cosy|openaudio|sambert|xtts)['"]\s*(?:===?|!==?)""",
        re.X,
    )
    for path in (CLIENT_JS, MAIN_JS):
        src = path.read_text(encoding="utf-8")
        code = re.sub(r"//[^\n]*|/\*.*?\*/", "", src, flags=re.S)   # comments may name vendors
        hits = branchy.findall(code)
        assert not hits, f"{path.name}: provider literal branch {hits}"
    assert "setKnownVoiceBackends" in CLIENT_JS.read_text(encoding="utf-8"), (
        "the registry-provided voice-backend list is no longer wired into the client"
    )


# ── §7 H: the browser↔:8010 contract, checked across the language boundary ───

def _strip_js_comments(src: str) -> str:
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", src, flags=re.S)


def _block(src: str, anchor: str, span: int = 2000) -> str:
    """The text from `anchor` through the close of the brace it opens.

    Used instead of a line window so a handler cannot pass by hiding a second
    decision below the cut.
    """
    start = src.index(anchor)
    open_at = src.index("{", start)
    depth = 0
    for pos in range(open_at, min(len(src), open_at + span)):
        if src[pos] == "{":
            depth += 1
        elif src[pos] == "}":
            depth -= 1
            if depth == 0:
                return src[start:pos + 1]
    raise AssertionError(f"unbalanced block after {anchor!r}")


def cancel_owner_violations(client_src: str, avatar_src: str) -> list[str]:
    """Pure predicate: empty list means "one owner decides an interrupt"."""
    client, avatar = _strip_js_comments(client_src), _strip_js_comments(avatar_src)
    bad: list[str] = []
    if "export class TurnControl" not in client:
        bad.append("the shared owner (TurnControl) is gone from the client")
    if "globalThis.s2sTurnControl" not in client:
        bad.append("the owner is no longer visible to the classic avatar script")
    if 'registerParticipant("realtime-transport"' not in client:
        bad.append("the :8765 half no longer registers as a participant")
    if "registerParticipant('avatar-renderer'" not in avatar:
        bad.append("the :8010 half no longer registers as a participant")
    # One wire mechanism per end: a second send site means a second decider.
    needle = 'type: "response.cancel"'
    if client.count(needle) != 1:
        bad.append(f"{client.count(needle)} response.cancel send sites, want 1")
    if avatar.count("/interrupt_talk") != 1:
        bad.append(f'{avatar.count("/interrupt_talk")} /interrupt_talk POST sites, want 1')
    # Both VAD edges must go *through* the owner, and must not keep a private
    # local shortcut that would make the decision twice.
    vad_client = _block(client, 'case "input_audio_buffer.speech_started"')
    if "this.cancelTurn(" not in vad_client:
        bad.append("the client's speech_started path does not ask the owner")
    if "_clearPlayback()" in vad_client:
        bad.append("the client's speech_started path still clears playback itself")
    vad_avatar = _block(avatar, "t === 'input_audio_buffer.speech_started'")
    if "delegateCancel(" not in vad_avatar:
        bad.append("avatar-sync's speech_started path no longer delegates to the owner")
    return bad


def _upload_fields(gate_src: str, avatar_src: str) -> tuple[set, set]:
    """(names the gate reads off the upload, names avatar-sync actually posts)."""
    reads = set(re.findall(r'fields\.get\("([a-z_]+)"\)', gate_src))
    posts = set(re.findall(r"fd\.append\('([a-z_]+)'", avatar_src))
    return reads, posts


def missing_upload_keys(gate_src: str, avatar_src: str) -> set:
    """Key halves the :8010 gate reads that the browser never posts."""
    reads, posts = _upload_fields(gate_src, avatar_src)
    return SEGMENT_KEY - posts


def unified_cancel_makes_exactly_one_decision():
    """§7 H 核心：一次插话 = 一个判决，两端各执行一次，迟到音频只丢被取消那一通。"""
    out = _scenario("unified_cancel")
    assert out["ok"], out
    detail = out["detail"]
    assert detail["wireCancels"] == 1, detail
    assert len(detail["avatarCalls"]) == 1, detail
    assert detail["avatarCalls"][0]["responseId"] == "resp_A", detail
    assert detail["avatarCalls"][0]["reason"] == "user_interrupt", detail
    assert len({c["seq"] for c in detail["avatarCalls"]}) == 1, detail
    assert {"realtime-transport", "avatar-renderer"} <= set(detail["participants"]), detail
    assert detail["staleDropped"]["audio"] >= 1, detail
    assert detail["played"] == 2, f"one played before the interrupt, one after: {detail}"


def one_owner_decides_the_interrupt():
    bad = cancel_owner_violations(CLIENT_JS.read_text(encoding="utf-8"), AVATAR_JS.read_text(encoding="utf-8"))
    assert not bad, "; ".join(bad)


def upload_fields_match_the_avatar_gate():
    """The two halves of :8010's idempotency key must be sent by the browser."""
    gate_src = GATE_PY.read_text(encoding="utf-8")
    avatar_src = AVATAR_JS.read_text(encoding="utf-8")
    reads, posts = _upload_fields(gate_src, avatar_src)
    assert SEGMENT_KEY <= reads, f"the gate stopped reading its own key halves: {sorted(SEGMENT_KEY - reads)}"
    assert SEGMENT_KEY <= posts, f"avatar-sync.js no longer posts {sorted(SEGMENT_KEY - posts)}"
    assert "admit_turn(" in ROUTES_PY.read_text(encoding="utf-8"), "/humanaudio no longer asks the gate"
    assert "cancel_turn(" in ROUTES_PY.read_text(encoding="utf-8"), "the interrupt routes no longer name the cancelled response"


# ── runner ───────────────────────────────────────────────────────────────────

TESTS = [
    handshake_failure_is_reported_and_never_wedges_the_client,
    dropped_link_reconnects_with_backoff_and_without_an_error_event,
    error_callback_never_recurses_into_close,
    half_open_link_is_declared_dead_by_the_watchdog,
    quiet_but_healthy_link_is_not_dropped,
    keepalive_echo_is_not_reported_as_a_server_error,
    reconnect_flushes_playback_and_drops_previous_generations_blocks,
    unified_cancel_makes_exactly_one_decision,
    one_owner_decides_the_interrupt,
    upload_fields_match_the_avatar_gate,
    restart_of_the_endpoint_recovered_without_a_close_event,
    exhausted_retry_budget_reports_the_truth,
    user_close_cancels_an_in_flight_recovery,
    ui_state_machine_covers_every_real_transport_state,
    reconnecting_state_is_not_treated_as_live,
    text_input_cannot_spawn_a_second_generation,
    recovery_path_carries_no_provider_literal,
]

if __name__ == "__main__":
    if "--emit-harness" in sys.argv:
        HARNESS_JS.parent.mkdir(parents=True, exist_ok=True)
        HARNESS_JS.write_text(HARNESS, encoding="utf-8", newline="\n")
        print(f"harness written: {HARNESS_JS}")
        raise SystemExit(0)
    started = time.monotonic()
    failures = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"  ok   {test.__name__}")
    elapsed = time.monotonic() - started
    if failures:
        print(f"realtime transport checks: FAIL ({failures} of {len(TESTS)}) in {elapsed:.1f}s")
        raise SystemExit(1)
    print(f"realtime transport checks: PASS ({len(TESTS)}) in {elapsed:.1f}s")
