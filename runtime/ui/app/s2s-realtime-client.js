// @ts-check
/**
 * Demo adapter around the official OpenAI Agents SDK RealtimeSession.
 *
 * Protocol ownership stays with the SDK's stock WebSocket and WebRTC
 * transports. This adapter only preserves demo concerns: the HF queue grant,
 * browser audio graphs, user-audio replay, visualization, and UI events.
 *
 * @typedef {"websocket" | "webrtc"} TransportKind
 * @typedef {Object} NoiseGate
 * @property {boolean} enabled
 * @property {number} thresholdDb
 * @typedef {Object} ToolDef
 * @property {"function"} type
 * @property {string} name
 * @property {string} description
 * @property {object} parameters
 * @typedef {Object} SessionInfo
 * @property {string} sessionId
 * @property {string} connectUrl
 * @property {string} websocketUrl
 * @property {string} sessionToken
 * @property {number} pendingTimeoutS
 * @property {string} [tier]
 * @property {boolean} [limited]
 * @property {number} [heartbeatSec]
 * @property {number} [remainingSec]
 * @typedef {Object} ClientOptions
 * @property {TransportKind} transport
 * @property {string} [sessionUrl]
 * @property {string} [loadBalancerUrl]
 * @property {string} [directUrl]
 * @property {string} [callsUrl]
 * @property {RTCIceServer[]} [iceServers]
 * @property {string} voice
 * @property {string} instructions
 * @property {{tts_backend: string, manual_voice_mode: Record<string, string>}} [manualVoiceSelection]
 * @property {string} [startupGreeting]
 * @property {MediaStream} [micStream]
 * @property {() => Promise<MediaStream>} [acquireMic]
 * @property {AudioContext} [audioContext]
 * @property {ToolDef[]} [tools]
 * @property {NoiseGate} [noiseGate]
 * @property {string} [audioOutputId]
 * @property {(call: {name: string, arguments: string, callId: string}) => Promise<{output: string, image?: string}>} [executeTool]
 * @property {Resilience} [resilience]
 * @typedef {Object} Resilience
 * @property {boolean} [enabled]
 * @property {number} [maxAttempts]
 * @property {number} [baseDelayMs]
 * @property {number} [maxDelayMs]
 * @property {number} [jitterMs]
 * @property {number} [heartbeatMs]
 * @property {number} [missLimit]
 */

import { extractResponseTranscript, trimTrailingSlash } from "./ws/codec.js";
import { OrbVisualiser, VIS_FFT_SIZE } from "./ws/orb-visualizer.js";
import { SentAudioRecorder, pcm16ToWavBlob } from "./ws/user-audio-recorder.js";

const AUDIO_SAMPLE_RATE = 24_000;
const AUDIO_WORKLET_VERSION = "24k-20260924-diag2";
const MIC_CHUNK_MS = 40;
const CAPTURE_CONFIG_TIMEOUT_MS = 2_000;
const SPEAKING_OPEN_DB = -50;
const SPEAKING_HANG_MS = 250;

/**
 * Liveness probe for an idle realtime socket.
 *
 * A browser WebSocket cannot send a ping frame, and a *quiet but healthy*
 * pipeline sends nothing at all until it has something to say — so "no traffic
 * for a while" must never be read as alive, and must never be read as dead
 * either. The probe resolves that: it is an event the endpoint cannot ignore.
 * Every realtime server answers an event type it does not know with a generic
 * `error` event, and receiving *anything* back proves the return path. Nothing
 * here is model-, TTS- or vendor-specific: no capability branch, no backend
 * literal, and a server that later grows a real keepalive handler answers with
 * the same type name and still counts.
 */
const KEEPALIVE_EVENT_TYPE = "realtime.keepalive";
const KEEPALIVE_REPLY_TYPES = new Set([KEEPALIVE_EVENT_TYPE, `${KEEPALIVE_EVENT_TYPE}.ack`, "pong"]);
const STALE_RESPONSE_MEMORY = 200;

/**
 * THE single decider for "cancel the current response" (任务 §7 H 统一 cancel).
 *
 * Why one object: an interrupt has to stop two independent renderers at once —
 * the :8765 realtime pipeline (LLM + TTS still streaming into this socket) and
 * the :8010 avatar (audio already queued for lip-sync, reached only through
 * `avatar-sync.js`'s own WebSocket tap).  Before this existed each file decided
 * for itself on `input_audio_buffer.speech_started`, so the two could name
 * different things "the current response" and neither carried the response id
 * that :8010's segment gate needs to block a reply that never enqueued.
 *
 * Contract, and it is deliberately small:
 *  - `begin(id)` / `end(id)` track the live response id (one at a time);
 *  - `cancel(reason, {responseId})` makes ONE decision and fans it out to the
 *    registered participants; a second cancel for an already-retired id is a
 *    no-op, so the VAD path and a manual stop cannot produce two interrupts;
 *  - an *unscoped* cancel (nothing in flight, so no id) is keyed by the turn
 *    that most recently was in flight, not by "no id": both callers of ONE
 *    `speech_started` message (this client's `_onTransportEvent` and
 *    `avatar-sync.js`'s WebSocket tap see the same frame) would otherwise get a
 *    fresh decision, because whoever ran first already cleared the id;
 *  - each participant owns *its own* drop mechanism (`response.cancel` on the
 *    wire / epoch + `/interrupt_talk`), because only they can;
 *  - a participant that throws is recorded and does not silence the others.
 *
 * `reason` is the same vocabulary :8010 validates
 * (`server/idempotency.py:CANCEL_REASONS`), so one barge-in is comparable in
 * the browser log and in `livetalking.log`.
 */
export class TurnControl {
  constructor() {
    /** @type {Map<string, (decision: any) => any>} */
    this.participants = new Map();
    this.activeId = "";
    /** Most recent id handed to `begin()` — the fallback target of a cancel. */
    this.lastId = "";
    /** @type {Set<string>} ids a cancel already retired (bounded, see _remember) */
    this.cancelled = new Set();
    this.decisions = [];
    this.seq = 0;
  }

  registerParticipant(name, fn) {
    if (typeof name !== "string" || !name || typeof fn !== "function") return false;
    this.participants.set(name, fn);
    return true;
  }

  hasParticipant(name) {
    return this.participants.has(name);
  }

  /** @param {string} responseId @param {any} [owner] */
  begin(responseId, owner) {
    if (!responseId) return;
    this.activeId = responseId;
    this.lastId = responseId;
    this.owner = owner ?? null;
    this.cancelled.delete(responseId);
  }

  end(responseId) {
    if (responseId && this.activeId === responseId) this.activeId = "";
  }

  _remember(id) {
    if (!id) return;
    this.cancelled.add(id);
    while (this.cancelled.size > STALE_RESPONSE_MEMORY) {
      const [oldest] = this.cancelled;
      this.cancelled.delete(oldest);
    }
  }

  /**
   * Make the turn decision and fan it out.
   * @param {"user_interrupt"|"barge_in"|"manual_stop"|"unspecified"} reason
   * @param {{responseId?: string}} [options]
   * @returns {any|null} the decision record, or null when it changed nothing
   */
  cancel(reason, options = {}) {
    // The target is always a response id: the explicit one, else the turn in
    // flight, else the one that was most recently in flight.  Falling back to
    // `lastId` — rather than to "" — is what turns a duplicate trigger into a
    // no-op: `_onTransportEvent` and avatar-sync's WebSocket tap both see the
    // SAME `speech_started` frame, and whoever runs first has already cleared the
    // active id, so an id-less second decision used to name nothing.  A cancel
    // that names nothing cannot block anything on :8010.
    const responseId = String(options.responseId || this.activeId || this.lastId || "");
    if (!responseId || this.cancelled.has(responseId)) return null;   // decided already
    const decision = {
      seq: ++this.seq,
      reason: reason || "unspecified",
      responseId,
      at: Date.now(),
      participants: {},
    };
    this._remember(responseId);
    if (this.activeId === responseId) this.activeId = "";
    for (const [name, fn] of this.participants) {
      try {
        fn(decision);
        decision.participants[name] = "ok";
      } catch (err) {
        // One broken renderer must not silence the other end's cancel.
        decision.participants[name] = `error: ${err?.message || err}`;
      }
    }
    this.decisions.push(decision);
    while (this.decisions.length > 20) this.decisions.shift();
    return decision;
  }

  diagnostics() {
    return {
      activeId: this.activeId,
      lastId: this.lastId,
      cancelled: [...this.cancelled],
      cancels: this.seq,
      participants: [...this.participants.keys()],
    };
  }
}

/** @type {TurnControl} process-wide owner; classic scripts see it on globalThis */
export const s2sTurnControl = new TurnControl();
globalThis.s2sTurnControl = s2sTurnControl;

/** @param {string} message @param {string} code @param {object} [extra] */
function codedError(message, code, extra) {
  const error = /** @type {Error & {code?: string}} */ (new Error(message));
  error.code = code;
  if (extra) Object.assign(error, extra);
  return error;
}

/** Turn browser WebSocket events and SDK error envelopes into useful errors.
 *  @param {unknown} value @returns {Error} */
function connectionError(value) {
  if (value instanceof Error) return value;
  const nested = /** @type {any} */ (value)?.error;
  if (nested instanceof Error) return nested;
  if (typeof nested?.message === "string" && nested.message) return new Error(nested.message);
  if (typeof /** @type {any} */ (value)?.message === "string" && /** @type {any} */ (value).message) {
    return new Error(/** @type {any} */ (value).message);
  }
  if (typeof Event !== "undefined" && value instanceof Event) {
    return new Error("Realtime WebSocket handshake failed");
  }
  const text = String(value);
  return new Error(text === "[object Object]" ? "Realtime connection failed" : text);
}

function sdk() {
  const value = globalThis.OpenAIAgentsRealtime;
  if (!value) throw new Error("OpenAI Agents SDK bundle is not loaded");
  return value;
}

export class S2sRealtimeClient extends EventTarget {
  /** @param {ClientOptions} options */
  constructor(options) {
    super();
    this.options = options;
    this._tools = options.tools ?? [];
    this._directUrl = options.directUrl ?? "";
    this._sessionUrl = options.sessionUrl
      ? options.sessionUrl
      : options.loadBalancerUrl
        ? `${trimTrailingSlash(options.loadBalancerUrl)}/session`
        : "";
    this._acquireMic = options.acquireMic ?? null;
    this._noiseGate = options.noiseGate ?? { enabled: false, thresholdDb: -45 };
    this._status = "idle";
    this._closed = false;
    this._closing = false;
    this._muted = false;
    this._session = null;
    this._transport = null;
    this._agent = null;
    this._queueId = "";
    this._queueWake = null;
    this._queueTimer = 0;
    this._joinResolve = null;
    this._joinReject = null;
    this._joinTimer = 0;
    this._ctx = null;
    this._micSrc = null;
    this._captureNode = null;
    this._playbackNode = null;
    this._micAnalyser = null;
    this._outAnalyser = null;
    this._remoteSrc = null;
    this._audioElement = null;
    this._visualiser = null;
    this._levelTimer = 0;
    this._levelBuf = new Uint8Array(VIS_FFT_SIZE);
    this._lastAudibleAt = 0;
    this._aiSpeaking = false;
    this._activeResponseId = "";
    this._responseRequested = false;
    this._audibleResponses = new Set();
    this._asstTranscriptByResp = new Map();
    this._asstFullByResp = new Map();
    // Input transcription deltas are append-only and may overlap across turns.
    // Keep each unresolved prefix until its authoritative completion arrives.
    this._userTranscriptByItem = new Map();
    this._currentUserItemId = "";
    this._userAudioRecorder = new SentAudioRecorder({ sampleRate: AUDIO_SAMPLE_RATE });
    this._micDiagnostic = new URLSearchParams(globalThis.location?.search || "").has("micdiag") ? {
      startedAt: Date.now(), rawChunks: [], rawSamples: 0, rawPeak: 0,
      rawRms: [], workletChunks: 0, workletSamples: 0, workletFirstAt: 0,
      workletLastAt: 0, sendAudioCount: 0, sendAudioBytes: 0,
      skipped: {}, wsAppendCount: 0, wsAppendBytes: 0,
      wsFirstAt: 0, wsLastAt: 0, wsPacketBytes: [], vadEvents: [], transcripts: [],
      channelPeaks: [0, 0], channelRmsMax: [0, 0],
    } : null;
    // ── §7 fault A: transport resilience state ──────────────────────────────
    // One monotonic dial generation. Every listener built by a dial captures the
    // generation it belongs to, so a frame that arrives late from a socket we
    // already gave up on is dropped instead of bleeding into the new call.
    this._generation = 0;
    this._dialing = false;
    this._dropping = false;
    this._reconnectAttempt = 0;
    this._reconnectTimer = 0;
    this._lastDisconnectReason = "";
    this._staleResponseIds = new Set();
    this._genResponseIds = new Set();
    this._staleDropped = { audio: 0, text: 0, event: 0 };
    this._audioBlocksPlayed = 0;
    this._drops = 0;
    this._handshakes = 0;
    this._heartbeatTimer = 0;
    this._lastInboundAt = 0;
    this._lastSendAt = 0;
    this._lastBuffered = null;
    this._link = { buffered: 0, grew: false, streak: 0 };
    this._linkMisses = 0;
    this._socket = null;
    this._probeSentAt = 0;
    this._probeMisses = 0;
    this._probesSent = 0;
    this._probesAnswered = 0;
    const r = options.resilience ?? {};
    const num = (value, fallback) => (Number.isFinite(value) && value > 0 ? value : fallback);
    this._resilience = {
      enabled: r.enabled !== false,
      maxAttempts: num(r.maxAttempts, 15),
      baseDelayMs: num(r.baseDelayMs, 500),
      maxDelayMs: num(r.maxDelayMs, 8_000),
      jitterMs: num(r.jitterMs, 250),
      heartbeatMs: num(r.heartbeatMs, 5_000),
      missLimit: num(r.missLimit, 3),
      // See _watchdogTick(): an echo probe costs the server a usage error.
      echoProbe: r.echoProbe === true,
    };
    this._debug = (() => {
      try { return localStorage.getItem("s2s.debug") === "1"; } catch { return false; }
    })();
    // §7 H: this client is the :8765 participant of the one turn decider. It
    // registers itself (not the reverse) because only it owns the socket, and
    // `avatar-sync.js` may or may not be on the page.
    this._turnCancelled = 0;
    s2sTurnControl.registerParticipant("realtime-transport", (decision) => this._cancelRealtimeTurn(decision));
  }

  /**
   * What "cancel the current response" costs on the :8765 side, run by
   * :meth:`TurnControl.cancel` and by nothing else:
   *  1. flush the playback worklet (the samples already in its ring buffer),
   *  2. retire the response id, so every straggler delta/text/event of *that*
   *     call is dropped by the existing `_isStaleResponse` route instead of
   *     bleeding into the next turn, and
   *  3. tell the server, so it stops generating (and stops billing) a reply
   *     nobody will hear, and re-enables listening.  `response.cancel` is a
   *     no-op server-side when nothing is in flight, which is why the id guard
   *     above — not a guess about server state — decides whether we send it.
   * @param {{responseId?: string, reason?: string}} decision
   */
  _cancelRealtimeTurn(decision) {
    const responseId = String(decision?.responseId || "");
    if (responseId && this._staleResponseIds.has(responseId) && !this._activeResponseId) return;
    this._clearPlayback();
    // Tell the router before we mark the response stale: its retained buffer and
    // the armed avatar-receipt timer both have to die here, or a cancelled round
    // would come back as audio after the new turn started (§九 + 旧 response 插入
    // 新 response).
    globalThis.s2sAudioRouter?.cancel(responseId || this._activeResponseId, decision?.reason || "cancel");
    if (responseId) {
      this._staleResponseIds.add(responseId);
      this._genResponseIds.delete(responseId);
      while (this._staleResponseIds.size > STALE_RESPONSE_MEMORY) {
        const [oldest] = this._staleResponseIds;
        this._staleResponseIds.delete(oldest);
      }
    }
    if (responseId === this._activeResponseId) this._activeResponseId = "";
    if (responseId && !this._closed && this._transport?.sendEvent) {
      try {
        this._transport.sendEvent({ type: "response.cancel", event_id: `response_cancel_${responseId}` });
        this._turnCancelled += 1;
      } catch (err) {
        // A dead socket is not a reason to leave the avatar talking: the :8010
        // participant runs regardless, and the drop path owns redial.
        if (this._debug) console.debug("[realtime] response.cancel failed:", err);
      }
    }
    this.dispatchEvent(new CustomEvent("turn-cancelled", { detail: decision }));
  }

  get status() { return this._status; }

  /** @param {string} status */
  _setStatus(status) {
    if (this._status === status) return;
    this._status = status;
    this.dispatchEvent(new CustomEvent("status", { detail: { status } }));
  }

  /** Live transport state, so the UI (and a test) can compare what it paints
   *  against what the socket is actually doing instead of trusting a caption. */
  diagnostics() {
    return {
      status: this._status,
      generation: this._generation,
      handshakes: this._handshakes,
      drops: this._drops,
      attempt: this._reconnectAttempt,
      lastDisconnectReason: this._lastDisconnectReason,
      staleDropped: { ...this._staleDropped },
      turn: s2sTurnControl.diagnostics(),
      turnCancelled: this._turnCancelled,
      audioBlocksPlayed: this._audioBlocksPlayed,
      mic: this.micDiagnostics(),
      link: { ...this._link, misses: this._linkMisses, readyState: this._socket?.readyState },
      keepalive: {
        intervalMs: this._resilience.heartbeatMs,
        echoProbe: this._resilience.echoProbe,
        sent: this._probesSent,
        answered: this._probesAnswered,
        outstanding: this._probeSentAt ? 1 : 0,
        misses: this._probeMisses,
      },
    };
  }

  micDiagnostics() {
    const d = this._micDiagnostic;
    if (!d) return null;
    return {
      startedAt: d.startedAt, rawChunkCount: d.rawChunks.length,
      rawSamples: d.rawSamples, rawDurationSec: d.rawSamples / AUDIO_SAMPLE_RATE,
      rawPeak: d.rawPeak, rawRms: [...d.rawRms],
      workletChunks: d.workletChunks, workletSamples: d.workletSamples,
      workletFirstAt: d.workletFirstAt, workletLastAt: d.workletLastAt,
      sendAudioCount: d.sendAudioCount, sendAudioBytes: d.sendAudioBytes,
      skipped: { ...d.skipped }, wsAppendCount: d.wsAppendCount,
      wsAppendBytes: d.wsAppendBytes, wsFirstAt: d.wsFirstAt,
      wsLastAt: d.wsLastAt, wsPacketBytes: [...d.wsPacketBytes],
      vadEvents: [...d.vadEvents], transcripts: [...d.transcripts],
      channelPeaks: [...d.channelPeaks], channelRmsMax: [...d.channelRmsMax],
      micTracks: this.options.micStream?.getAudioTracks().map((track) => ({
        label: track.label, enabled: track.enabled, muted: track.muted,
        readyState: track.readyState, settings: track.getSettings(),
      })) || [],
    };
  }

  micDiagnosticWav() {
    const d = this._micDiagnostic;
    if (!d?.rawSamples) return null;
    const pcm = new Uint8Array(d.rawSamples * 2);
    let offset = 0;
    for (const chunk of d.rawChunks) {
      pcm.set(new Uint8Array(chunk), offset);
      offset += chunk.byteLength;
    }
    return pcm16ToWavBlob(pcm, AUDIO_SAMPLE_RATE);
  }

  startMicDiagnosticCapture() {
    const d = this._micDiagnostic;
    if (!d || !this._captureNode) return false;
    d.rawChunks = [];
    d.rawSamples = 0;
    d.rawPeak = 0;
    d.rawRms = [];
    d.channelPeaks = [0, 0];
    d.channelRmsMax = [0, 0];
    this._captureNode.port.postMessage({ kind: "diagnostic-capture", enabled: true });
    return true;
  }

  /** @returns {boolean} does a callback captured at `gen` still own this client? */
  _alive(gen) {
    return !this._closed && gen === this._generation && this._session !== null;
  }

  /** Any inbound frame proves the return path, which is all a keepalive needs. */
  _noteInbound() {
    this._lastInboundAt = Date.now();
    if (this._probeSentAt) {
      this._probeSentAt = 0;
      this._probeMisses = 0;
      this._probesAnswered += 1;
    }
  }

  /** Is this `error` event just the endpoint's "unknown event type" answer to our
   *  keepalive probe? (Never model/TTS specific — it keys off our own marker.) */
  _isKeepaliveAnswer(error) {
    const type = typeof error?.type === "string" ? error.type : "";
    if (KEEPALIVE_REPLY_TYPES.has(type)) return true;
    const message = typeof error?.message === "string" ? error.message : "";
    return message.includes(KEEPALIVE_EVENT_TYPE);
  }

  /** Resolve (or re-resolve, after a drop) the URL a dial targets.
   *  @param {{recovery?: boolean}} [options] recovery keeps the "reconnecting"
   *  caption on screen instead of pretending this is a fresh user-initiated call. */
  async _resolveUrl(options = {}) {
    const recovery = options.recovery === true;
    if (this.options.transport !== "websocket") {
      if (!recovery) this._setStatus("connecting");
      return new URL(this.options.callsUrl || "api/calls", globalThis.location?.href).href;
    }
    if (this._directUrl) {
      if (!recovery) this._setStatus("connecting");
      return this._directUrl;
    }
    if (!this._sessionUrl) throw new Error("No session endpoint or direct URL configured");
    this._setStatus("creating-session");
    const { grant, waited } = await this._createSessionOrQueue();
    if (this._closed) throw codedError("connect aborted", "aborted");
    if (waited) await this._awaitJoin(grant);
    if (this._closed) throw codedError("connect aborted", "aborted");
    this.dispatchEvent(new CustomEvent("session", { detail: { info: grant } }));
    if (!recovery) this._setStatus("connecting");
    return grant.connectUrl;
  }

  /** Build a fresh transport + session pair and dial it. Listeners are bound to
   *  the generation they were created in, and a handshake failure is reported
   *  only through the awaited rejection below — reacting to the socket's own
   *  error/close from inside a callback is how one drop turns into a recursion.
   *  @returns {Promise<number>} the generation now live */
  async _openGeneration() {
    const gen = this._generation + 1;
    this._generation = gen;
    const { OpenAIRealtimeWebRTC, OpenAIRealtimeWebSocket, RealtimeSession } = sdk();
    let transport;
    if (this.options.transport === "websocket") {
      transport = new OpenAIRealtimeWebSocket({
        useInsecureApiKey: true,
        // The stock browser transport advertises OpenAI's WebSocket
        // subprotocols. The HF realtime endpoint accepts the same GA events but
        // does not negotiate a Sec-WebSocket-Protocol response, which browsers
        // reject. Use the SDK's supported socket factory hook to open the same
        // connection without an OpenAI-specific subprotocol (the signed session
        // token is already carried by the connect URL).
        createWebSocket: async ({ url: socketUrl }) => {
          const socket = new globalThis.WebSocket(socketUrl);
          if (this._micDiagnostic) {
            const send = socket.send.bind(socket);
            socket.send = (data) => {
              const result = send(data);
              if (typeof data === "string" && data.includes('"input_audio_buffer.append"')) {
                try {
                  const event = JSON.parse(data);
                  if (event.type === "input_audio_buffer.append" && typeof event.audio === "string") {
                    const d = this._micDiagnostic;
                    const bytes = Math.floor(event.audio.length * 3 / 4)
                      - (event.audio.endsWith("==") ? 2 : event.audio.endsWith("=") ? 1 : 0);
                    const now = Date.now();
                    d.wsAppendCount += 1;
                    d.wsAppendBytes += bytes;
                    d.wsFirstAt ||= now;
                    d.wsLastAt = now;
                    if (d.wsPacketBytes.length < 300) d.wsPacketBytes.push(bytes);
                  }
                } catch { /* Diagnostics must never affect the production send path. */ }
              }
              return result;
            };
          }
          // Kept per generation: the watchdog reads readyState/bufferedAmount off
          // this very socket, and a retired socket must never be read again.
          if (gen === this._generation) this._socket = socket;
          return socket;
        },
      });
    } else {
      // Keep startup noise from racing the initial config and greeting. The
      // prior demo client held this same gate until its data channel was ready.
      this.options.micStream.getAudioTracks()[0].enabled = false;
      const audioElement = new Audio();
      audioElement.autoplay = true;
      this._audioElement = audioElement;
      transport = new OpenAIRealtimeWebRTC({
        mediaStream: this.options.micStream,
        audioElement,
        useInsecureApiKey: true,
        changePeerConnection: (pc) => {
          if (this.options.iceServers?.length) {
            pc.setConfiguration({ ...pc.getConfiguration(), iceServers: this.options.iceServers });
          }
          return pc;
        },
      });
    }
    const agent = this._buildAgent();
    const session = new RealtimeSession(agent, {
      transport,
      model: "s2s-local",
      config: this._sessionConfig(),
      tracingDisabled: true,
    });
    this._transport = transport;
    this._session = session;

    transport.on("*", (event) => {
      if (!this._alive(gen)) { this._staleDropped.event += 1; return; }
      this._noteInbound();
      this._onTransportEvent(event);
    });
    transport.on("connection_change", (state) => {
      if (!this._alive(gen)) { this._staleDropped.event += 1; return; }
      if (state !== "disconnected") return;
      // A dial that never finished reporting itself: `connect()` below owns it.
      if (this._dialing || this._closing || this._closed) return;
      this._scheduleDrop("Realtime transport disconnected");
    });
    session.on("audio", (event) => {
      if (!this._alive(gen)) { this._staleDropped.audio += 1; return; }
      this._onAudio(event);
    });
    session.on("audio_interrupted", () => {
      if (this._alive(gen)) this._clearPlayback();
    });

    this._dialing = true;
    try {
      await session.connect({ apiKey: "s2s-local", url: this._url });
    } catch (error) {
      this._dialing = false;
      await this._retireGeneration(gen, "handshake failed", session);
      throw connectionError(error);
    }
    this._dialing = false;
    if (!this._alive(gen)) {
      // Teardown or a drop won the race while we were dialling: close what we
      // built so nothing leaks behind the retry loop.
      await this._retireGeneration(gen, "abandoned", session);
      throw codedError("connect aborted", "aborted");
    }
    if (this.options.transport === "webrtc") this._attachRtcOutput();
    this._manualTransportReady = true;
    this._handshakes += 1;
    return gen;
  }

  /** Re-apply the per-call configuration that has to survive a redial.
   *  @param {{greeting?: boolean}} [options] */
  _applyLiveConfiguration(options = {}) {
    this.setManualVoiceMode(this.options.manualVoiceSelection);
    if (options.greeting !== false) {
      const greeting = this.options.startupGreeting?.trim();
      if (greeting) {
        this._responseRequested = true;
        this._session?.sendMessage(greeting);
      }
    }
    this.setMuted(this._muted);
    this._startHeartbeat();
  }

  async connect() {
    if (this._session) throw new Error("Already connected");
    this._closed = false;
    this._closing = false;
    this._url = await this._resolveUrl();

    if (!this.options.micStream && this._acquireMic) {
      this.options.micStream = await this._acquireMic();
    }
    if (this._closed) throw codedError("connect aborted", "aborted");
    if (!this.options.micStream?.getAudioTracks()[0]) throw new Error("No microphone track available");

    await this._setupAudio();
    await this._openGeneration();
    this._applyLiveConfiguration({ greeting: true });
    this._setStatus("connected");
  }

  // ── §7 fault A: drop → reconnect → generation reset ───────────────────────

  /** Retire `gen`: bump the epoch first so callbacks queued from that socket die,
   *  flush everything the old call could still play, then close it off this
   *  callback turn. No-op when the generation has already been retired.
   *  @param {number} gen @param {string} why @param {any} [session] */
  async _retireGeneration(gen, why, session) {
    const isCurrent = gen === this._generation;
    const dying = session ?? (isCurrent ? this._session : null);
    const dyingTransport = isCurrent ? this._transport : null;
    if (isCurrent) {
      this._generation += 1;
      this._lastDisconnectReason = why;
      this._stopHeartbeat();
      this._session = null;
      this._transport = null;
      this._socket = null;
      this._manualTransportReady = false;
      this._resetTurnState();
      this._drops += 1;
      this.dispatchEvent(new CustomEvent("transport-drop", {
        detail: { generation: gen, reason: why, diagnostics: this.diagnostics() },
      }));
    }
    this._closePair(dying, dyingTransport);
    return isCurrent;
  }

  /** @param {any} session @param {any} transport */
  _closePair(session, transport) {
    // Guarded on purpose: an `error`/`close` callback that unconditionally tears
    // the socket down again is the recursion this class keeps hitting.
    try {
      const target = session ?? transport;
      const done = target?.close?.();
      if (done && typeof done.catch === "function") done.catch(() => {});
    } catch (err) {
      if (this._debug) console.debug("[realtime] close after drop:", err);
    }
  }

  /** Forget everything the previous call could still surface. The playback worklet
   *  keeps a buffer across a socket drop, so flushing it here is what stops the
   *  first seconds of the old answer playing on top of the new call. */
  _resetTurnState() {
    this._responseRequested = false;
    // A retired generation can never deliver `response.done` for its last call,
    // so the owner must stop believing that id is live.
    s2sTurnControl.end(this._activeResponseId);
    this._activeResponseId = "";
    this._aiSpeaking = false;
    for (const id of this._genResponseIds) this._staleResponseIds.add(id);
    for (const id of this._audibleResponses) this._staleResponseIds.add(id);
    this._audibleResponses.clear();
    this._genResponseIds.clear();
    while (this._staleResponseIds.size > STALE_RESPONSE_MEMORY) {
      const [oldest] = this._staleResponseIds;
      this._staleResponseIds.delete(oldest);
    }
    this._asstTranscriptByResp.clear();
    this._asstFullByResp.clear();
    this._userTranscriptByItem.clear();
    this._currentUserItemId = "";
    this._userAudioRecorder.reset();
    this._clearPlayback();
  }

  /** @param {string} reason */
  _scheduleDrop(reason) {
    if (this._dropping || this._closed || this._dialing || !this._session) return;
    this._dropping = true;
    const gen = this._generation;
    // One task later: we are inside the dying socket's own event dispatch, and
    // re-entering it synchronously is what previously turned a single disconnect
    // into an event storm.
    setTimeout(() => {
      void Promise.resolve()
        .then(() => this._retireGeneration(gen, reason))
        .catch((err) => { if (this._debug) console.debug("[realtime] drop teardown:", err); })
        .finally(() => {
          this._dropping = false;
          this._beginReconnect(reason);
        });
    }, 0);
  }

  /** @param {string} reason */
  _beginReconnect(reason) {
    if (this._closed || this._session || this._dialing) return;
    if (!this._resilience.enabled) {
      this._setStatus("error");
      this.dispatchEvent(new CustomEvent("error", {
        detail: { error: codedError(reason, "transport-disconnected") },
      }));
      return;
    }
    if (this._reconnectTimer) return;
    this._reconnectAttempt = 0;
    this._setStatus("reconnecting");
    this.dispatchEvent(new CustomEvent("reconnect", {
      detail: { attempt: 0, reason, diagnostics: this.diagnostics() },
    }));
    this._scheduleReconnect(0);
  }

  /** @param {number} delay */
  _scheduleReconnect(delay) {
    if (this._reconnectTimer) clearTimeout(this._reconnectTimer);
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = 0;
      void this._reconnectOnce();
    }, Math.max(0, delay));
  }

  async _reconnectOnce() {
    if (this._closed || this._session || this._dialing) return;
    const policy = this._resilience;
    this._reconnectAttempt += 1;
    const attempt = this._reconnectAttempt;
    this._setStatus("reconnecting");
    try {
      this._url = await this._resolveUrl({ recovery: true });
      await this._openGeneration();
      if (this._closed) return;
      // No greeting on a recovery: the conversation is still the same one, and a
      // fresh hello block would talk over the answer and re-open a turn.
      this._applyLiveConfiguration({ greeting: false });
      this._reconnectAttempt = 0;
      this._setStatus("connected");
      this.dispatchEvent(new CustomEvent("reconnected", {
        detail: { attempt, generation: this._generation, diagnostics: this.diagnostics() },
      }));
      return;
    } catch (err) {
      if (this._closed) return;
      this._lastDisconnectReason = err instanceof Error ? err.message : String(err);
    }
    if (attempt >= policy.maxAttempts) {
      this._stopHeartbeat();
      this._setStatus("error");
      this.dispatchEvent(new CustomEvent("error", {
        detail: {
          error: codedError(
            `${this._lastDisconnectReason} — gave up after ${attempt} reconnect attempts`,
            "transport-unreachable",
            { attempts: attempt },
          ),
          diagnostics: this.diagnostics(),
        },
      }));
      return;
    }
    const backoff = Math.min(policy.maxDelayMs, policy.baseDelayMs * 2 ** (attempt - 1));
    const nextDelayMs = Math.round(backoff + Math.random() * policy.jitterMs);
    this.dispatchEvent(new CustomEvent("reconnect", {
      detail: { attempt, nextDelayMs, reason: this._lastDisconnectReason, diagnostics: this.diagnostics() },
    }));
    this._scheduleReconnect(nextDelayMs);
  }

  // ── link watchdog (the heartbeat this transport never had) ────────────────

  _startHeartbeat() {
    this._stopHeartbeat();
    const { heartbeatMs } = this._resilience;
    this._lastInboundAt = Date.now();
    this._lastSendAt = 0;
    this._lastBuffered = null;
    this._link.streak = 0;
    this._probeSentAt = 0;
    this._probeMisses = 0;
    if (!heartbeatMs) return;
    this._heartbeatTimer = setInterval(() => this._watchdogTick(), heartbeatMs);
  }

  _stopHeartbeat() {
    if (this._heartbeatTimer) clearInterval(this._heartbeatTimer);
    this._heartbeatTimer = 0;
    this._probeSentAt = 0;
    this._probeMisses = 0;
    this._link.streak = 0;
    this._lastBuffered = null;
  }

  /** @param {string} reason */
  _failLink(reason) {
    this._lastDisconnectReason = reason;
    this._linkMisses += 1;
    this._stopHeartbeat();
    // `_retireGeneration` is the single place a drop is announced, so one link
    // failure can never look like two.
    this._scheduleDrop(reason);
  }

  /**
   * Liveness, decided from evidence the socket itself gives — never from "we
   * have not heard anything", because a quiet pipeline is a healthy pipeline:
   *
   *   1. `readyState` left OPEN  → the peer is gone (an :8765 restart lands
   *      here, and so does a browser that got a close frame nobody told us about);
   *   2. `bufferedAmount` climbing tick after tick while we keep sending and
   *      nothing comes back → the link is half-open (network blip, stale NAT):
   *      the kernel is still retransmitting our mic frames and the window has
   *      closed. That is a dead call whatever the socket "says".
   *
   * A deliberate echo probe (`resilience.echoProbe`) is available for the
   * muted/no-sends case, but it is OFF by default on purpose: this endpoint has
   * no keepalive event, so an unknown-type probe is booked into the server's
   * usage error counters (api/openai_realtime/service.py:493-495
   * `total_usage.record_error`) and would turn an idle night into thousands of
   * recorded errors.
   */
  _watchdogTick() {
    if (this._closed || this._dialing || !this._session) return;
    const { heartbeatMs, missLimit, echoProbe } = this._resilience;
    const now = Date.now();
    const socket = this._socket;
    const OPEN = 1;
    if (socket && typeof socket.readyState === "number" && socket.readyState !== OPEN) {
      this._failLink(`socket no longer open (readyState=${socket.readyState})`);
      return;
    }
    const buffered = Number(socket?.bufferedAmount);
    if (Number.isFinite(buffered)) {
      const silent = now - this._lastInboundAt > heartbeatMs;
      const unsent = this._lastSendAt > this._lastInboundAt;
      const grew = this._lastBuffered !== null && buffered > Math.max(this._lastBuffered + 8_192, 32_768);
      this._lastBuffered = buffered;
      this._link = { buffered, grew: grew && silent && unsent, streak: grew && silent && unsent ? this._link.streak + 1 : 0 };
      if (this._link.streak >= missLimit) {
        this._failLink(
          `outbound stalled: ${Math.round(buffered / 1024)} KiB unsent with no inbound for ${Math.round((now - this._lastInboundAt) / 100) / 10}s`,
        );
        return;
      }
    }
    if (!echoProbe) return;
    if (this._probeSentAt) {
      this._probeSentAt = 0;
      this._probeMisses += 1;
      if (this._probeMisses >= missLimit) {
        this._failLink(`keepalive unanswered for ${missLimit * heartbeatMs} ms`);
        return;
      }
    } else if (now - this._lastInboundAt < heartbeatMs * 4) {
      return;
    }
    this._probeSentAt = now;
    this._probesSent += 1;
    try {
      this._transport?.sendEvent({ type: KEEPALIVE_EVENT_TYPE, event_id: `${KEEPALIVE_EVENT_TYPE}_${now}` });
    } catch (err) {
      if (this._debug) console.debug("[realtime] keepalive send failed:", err);
      this._probeMisses += 1;
    }
  }

  _sessionConfig() {
    return {
      outputModalities: ["audio"],
      audio: {
        input: {
          format: { type: "audio/pcm", rate: AUDIO_SAMPLE_RATE },
          transcription: { model: "whisper-1" },
          turnDetection: { type: "server_vad", interruptResponse: true },
          noiseReduction: null,
        },
        output: {
          format: { type: "audio/pcm", rate: AUDIO_SAMPLE_RATE },
          speed: 1,
        },
      },
    };
  }

  _buildAgent() {
    const { RealtimeAgent, tool } = sdk();
    const tools = this._tools.map((definition) => tool({
      name: definition.name,
      description: definition.description,
      parameters: definition.parameters,
      strict: false,
      execute: async (args, _context, details) => {
        const callId = details?.toolCall?.callId || "";
        const argumentsJson = JSON.stringify(args ?? {});
        if (!this.options.executeTool) throw new Error(`No executor for ${definition.name}`);
        const result = await this.options.executeTool({
          name: definition.name,
          arguments: argumentsJson,
          callId,
        });
        if (result.image) this._session?.addImage(result.image, { triggerResponse: false });
        return result.output;
      },
    }));
    return new RealtimeAgent({
      name: "speech-to-speech-demo",
      instructions: this.options.instructions,
      voice: this.options.voice,
      tools,
    });
  }

  async _setupAudio() {
    const ctx = this.options.audioContext ?? new AudioContext({ latencyHint: "interactive" });
    this._ctx = ctx;
    if (ctx.state === "suspended") await ctx.resume().catch(() => {});
    const micSrc = ctx.createMediaStreamSource(this.options.micStream);
    this._micSrc = micSrc;
    const micAnalyser = ctx.createAnalyser();
    micAnalyser.fftSize = VIS_FFT_SIZE;
    micAnalyser.smoothingTimeConstant = 0;
    micSrc.connect(micAnalyser);
    this._micAnalyser = micAnalyser;

    if (this.options.transport === "websocket") {
      const base = new URL("./worklets/", import.meta.url);
      const captureWorkletUrl = new URL("mic-capture.js", base);
      captureWorkletUrl.searchParams.set("v", AUDIO_WORKLET_VERSION);
      const playbackWorkletUrl = new URL("audio-playback.js", base);
      playbackWorkletUrl.searchParams.set("v", AUDIO_WORKLET_VERSION);
      await ctx.audioWorklet.addModule(captureWorkletUrl.href);
      await ctx.audioWorklet.addModule(playbackWorkletUrl.href);
      const capture = new AudioWorkletNode(ctx, "mic-capture", {
        numberOfInputs: 1,
        numberOfOutputs: 0,
        processorOptions: {
          chunkMs: MIC_CHUNK_MS,
          targetRate: AUDIO_SAMPLE_RATE,
          version: AUDIO_WORKLET_VERSION,
        },
      });
      const captureConfigured = new Promise((resolve, reject) => {
        const timeout = window.setTimeout(() => {
          reject(new Error(
            "The microphone audio processor did not report its sample rate. "
            + "Close every tab for this Space, reopen it, and try again.",
          ));
        }, CAPTURE_CONFIG_TIMEOUT_MS);
        capture.port.onmessage = (event) => {
          if (event.data instanceof ArrayBuffer) {
            this._onMicChunk(event.data);
          } else if (event.data?.kind === "diagnostic-raw" && this._micDiagnostic) {
            const d = this._micDiagnostic;
            d.rawChunks.push(event.data.pcm);
            d.rawSamples += event.data.samples;
            d.rawPeak = Math.max(d.rawPeak, event.data.peak);
            d.rawRms.push(event.data.rms);
          } else if (event.data?.kind === "diagnostic-channels" && this._micDiagnostic) {
            const d = this._micDiagnostic;
            for (let i = 0; i < Math.min(2, event.data.levels.length); i++) {
              d.channelPeaks[i] = Math.max(d.channelPeaks[i], event.data.levels[i].peak);
              d.channelRmsMax[i] = Math.max(d.channelRmsMax[i], event.data.levels[i].rms);
            }
          } else if (event.data?.kind === "level") {
            this.dispatchEvent(new CustomEvent("input-level", { detail: { rms: event.data.rms } }));
          } else if (event.data?.kind === "capture-config") {
            window.clearTimeout(timeout);
            const inputRate = Number(event.data.inputRate);
            const outputRate = Number(event.data.outputRate);
            const version = String(event.data.version || "unknown");
            console.info(
              `[audio] capture worklet ${version}: ${inputRate} Hz -> ${outputRate} Hz`,
            );
            if (outputRate !== AUDIO_SAMPLE_RATE || version !== AUDIO_WORKLET_VERSION) {
              reject(new Error(
                `Microphone sample-rate mismatch: processor ${version} outputs ${outputRate} Hz; `
                + `the client expects ${AUDIO_SAMPLE_RATE} Hz.`,
              ));
              return;
            }
            resolve(undefined);
          }
        };
      });
      capture.port.postMessage({ kind: "probe" });
      capture.port.postMessage({ kind: "gate", ...this._noiseGate });
      micSrc.connect(capture);
      this._captureNode = capture;
      await captureConfigured;

      const playback = new AudioWorkletNode(ctx, "audio-playback", {
        numberOfInputs: 0,
        numberOfOutputs: 1,
        outputChannelCount: [1],
      });
      playback.port.postMessage({ kind: "config", inputRate: AUDIO_SAMPLE_RATE });
      const output = ctx.createAnalyser();
      output.fftSize = VIS_FFT_SIZE;
      output.smoothingTimeConstant = 0.3;
      playback.connect(output);
      output.connect(ctx.destination);
      this._playbackNode = playback;
      this._outAnalyser = output;
      this._visualiser = new OrbVisualiser(micAnalyser, output, () => this._aiSpeaking);
      this._visualiser.start();
    }
    this._registerAudioSink();
    await this.setAudioOutputDevice(this.options.audioOutputId || "");
  }

  _attachRtcOutput() {
    if (this._remoteSrc) return;
    const stream = this._audioElement?.srcObject;
    if (!(stream instanceof MediaStream) || !this._ctx || !this._micAnalyser) {
      this._audioElement?.addEventListener("loadedmetadata", () => this._attachRtcOutput(), { once: true });
      return;
    }
    this._audioElement.muted = true;
    const source = this._ctx.createMediaStreamSource(stream);
    const output = this._ctx.createAnalyser();
    output.fftSize = VIS_FFT_SIZE;
    output.smoothingTimeConstant = 0.3;
    source.connect(output);
    output.connect(this._ctx.destination);
    this._remoteSrc = source;
    this._outAnalyser = output;
    this._visualiser = new OrbVisualiser(this._micAnalyser, output, () => this._aiSpeaking);
    this._visualiser.start();
    this._levelTimer = window.setInterval(() => this._pollRtcLevels(), 50);
  }

  _pollRtcLevels() {
    if (!this._session) return;
    const input = this._rms(this._micAnalyser);
    this.dispatchEvent(new CustomEvent("input-level", { detail: { rms: input } }));
    const output = this._rms(this._outAnalyser);
    const now = performance.now();
    if (output > Math.pow(10, SPEAKING_OPEN_DB / 20)) {
      this._lastAudibleAt = now;
      if (this._activeResponseId) this._audibleResponses.add(this._activeResponseId);
      this._aiSpeaking = true;
      this._markAudible();
    } else if (this._aiSpeaking && now - this._lastAudibleAt > SPEAKING_HANG_MS) {
      this._aiSpeaking = false;
    }
  }

  /** @param {AnalyserNode | null} analyser */
  _rms(analyser) {
    if (!analyser) return 0;
    analyser.getByteTimeDomainData(this._levelBuf);
    let sum = 0;
    for (const value of this._levelBuf) {
      const sample = (value - 128) / 128;
      sum += sample * sample;
    }
    return Math.sqrt(sum / this._levelBuf.length);
  }

  /** @param {{data: ArrayBuffer, responseId?: string}} event */
  _onAudio(event) {
    if (this.options.transport !== "websocket" || !this._playbackNode) return;
    const responseId = typeof event?.responseId === "string" ? event.responseId : "";
    if (this._isStaleResponse(responseId)) {
      // A delta from a call that already ended: it must not reach the speakers.
      this._staleDropped.audio += 1;
      return;
    }
    if (responseId) this._rememberCurrentResponse(responseId);
    const view = new DataView(event.data);
    // Int16 stays Int16 all the way to the router: the avatar sink has to write
    // the same s16le bytes into a WAV, and a float round trip would only add a
    // quantisation step nobody asked for.  The byte order is read explicitly
    // because the WAV header we later emit claims PCM/1 — LE — regardless of the
    // platform we are running on.
    const samples = new Int16Array(event.data.byteLength / 2);
    for (let i = 0; i < samples.length; i += 1) {
      samples[i] = view.getInt16(i * 2, true);
    }
    const router = globalThis.s2sAudioRouter;
    if (!router) {
      // audio-router.js not loaded (an older page, or a node harness): keep the
      // pre-router behaviour verbatim instead of going silent.
      this._playLocal(responseId, samples);
      return;
    }
    if (responseId) this._audibleResponses.add(responseId);
    this._aiSpeaking = true;
    this._markAudible();
    this._audioBlocksPlayed += 1;
    // The router is now the only thing that decides whether this page's
    // speakers, :8010's stream, or both-in-sequence produce the sound (§九).
    router.ingest(responseId, samples, { sampleRate: AUDIO_SAMPLE_RATE });
  }

  /**
   * The `local_browser` sink.  Registered by :8765's own audio path because this
   * class owns the AudioWorklet; the router never touches `port` directly.
   * @param {string} responseId
   * @param {Int16Array} samples
   */
  _playLocal(responseId, samples) {
    if (!this._playbackNode) return;
    if (!samples || !samples.length) return;
    const floats = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i += 1) {
      const sample = samples[i];
      floats[i] = sample < 0 ? sample / 0x8000 : sample / 0x7fff;
    }
    this._playbackNode.port.postMessage({ kind: "audio", samples: floats }, [floats.buffer]);
  }

  /** Idempotent: re-running `_setupAudio` must not stack a second sink. */
  _registerAudioSink() {
    const router = globalThis.s2sAudioRouter;
    if (!router || !this._playbackNode) return;
    router.registerSink(router.SINK_LOCAL || "local_browser", {
      write: (samples, meta) => {
        if (meta && meta.clear && !samples) {
          this._clearPlayback();
          return;
        }
        this._playLocal(meta?.responseId || "", samples);
      },
      // This page can only be audible once the worklet graph exists and the
      // AudioContext is not suspended — the fail-open target must be honest,
      // or "fall back to local" would mean "fall back to silence".
      available: () => Boolean(this._playbackNode)
        && (!this._ctx || this._ctx.state !== "suspended"),
      priority: 10,
    });
  }

  _clearPlayback() {
    this._playbackNode?.port.postMessage({ kind: "clear" });
    this._aiSpeaking = false;
  }

  /** @param {ArrayBuffer} buffer */
  _onMicChunk(buffer) {
    const d = this._micDiagnostic;
    if (d) {
      const now = Date.now();
      d.workletChunks += 1;
      d.workletSamples += buffer.byteLength / 2;
      d.workletFirstAt ||= now;
      d.workletLastAt = now;
    }
    // While the link is down there is nobody to receive the audio; queueing it
    // locally would only mean talking over the recovered call.
    if (!this._session || this._muted) {
      if (d) d.skipped[this._muted ? "muted" : "no-session"] = (d.skipped[this._muted ? "muted" : "no-session"] || 0) + 1;
      return;
    }
    if (this._status === "connecting" || this._status === "reconnecting") {
      if (d) d.skipped[this._status] = (d.skipped[this._status] || 0) + 1;
      return;
    }
    this._lastSendAt = Date.now();
    this._session.sendAudio(buffer);
    if (d) {
      d.sendAudioCount += 1;
      d.sendAudioBytes += buffer.byteLength;
    }
    this._userAudioRecorder.append(buffer);
  }

  /** @param {string} responseId */
  _isStaleResponse(responseId) {
    return Boolean(responseId) && this._staleResponseIds.has(responseId);
  }

  /** Track which response ids belong to the live generation; they become the
   *  blocklist the moment that generation is retired. Server ids are
   *  `resp_<uuid4>` (utils.utils._generate_id), so they never repeat.
   *  @param {string} responseId */
  _rememberCurrentResponse(responseId) {
    this._genResponseIds.add(responseId);
    if (this._genResponseIds.size <= STALE_RESPONSE_MEMORY) return;
    const [oldest] = this._genResponseIds;
    this._genResponseIds.delete(oldest);
  }

  /** The response an inbound GA event belongs to, whichever field carries it.
   *  @param {any} event */
  _eventResponseId(event) {
    if (typeof event?.response_id === "string") return event.response_id;
    if (typeof event?.response?.id === "string") return event.response.id;
    return "";
  }

  /** @param {any} event */
  _onTransportEvent(event) {
    const type = event?.type;
    if (typeof type !== "string") return;
    const responseId = this._eventResponseId(event);
    if (this._isStaleResponse(responseId)) {
      this._staleDropped.text += 1;
      return;
    }
    if (responseId) this._rememberCurrentResponse(responseId);
    if (this._debug) console.debug(`[${this.options.transport}]`, event);
    switch (type) {
      case "input_audio_buffer.speech_started": {
        if (this._micDiagnostic) this._micDiagnostic.vadEvents.push({ type, at: Date.now(), itemId: event.item_id, audioMs: event.audio_start_ms });
        // §7 H: the barge-in cancel is *decided* here and nowhere else. This
        // used to be `this._clearPlayback()`, which only stopped what this page
        // plays locally; the avatar on :8010 was interrupted by a second,
        // independent decider reading the same event off a hijacked WebSocket.
        this.cancelTurn("user_interrupt");
        const itemId = typeof event.item_id === "string" ? event.item_id : "";
        this._currentUserItemId = itemId;
        if (this.options.transport === "websocket") {
          this._userAudioRecorder.speechStarted({
            itemId,
            audioStartMs: Number(event.audio_start_ms),
          });
        }
        this.dispatchEvent(new CustomEvent("user-turn-started", { detail: { itemId } }));
        this._setStatus("user-speaking");
        break;
      }
      case "input_audio_buffer.speech_stopped": {
        if (this._micDiagnostic) this._micDiagnostic.vadEvents.push({ type, at: Date.now(), itemId: event.item_id, audioMs: event.audio_end_ms });
        const itemId = typeof event.item_id === "string" ? event.item_id : "";
        if (this.options.transport === "websocket") {
          const recording = this._userAudioRecorder.speechStopped({
            itemId,
            audioEndMs: Number(event.audio_end_ms),
          });
          if (recording) this.dispatchEvent(new CustomEvent("user-audio", { detail: recording }));
        }
        this.dispatchEvent(new CustomEvent("user-turn-stopped", { detail: { itemId } }));
        if (this._status === "user-speaking") this._setStatus("processing");
        break;
      }
      case "response.created":
        this._responseRequested = false;
        this._activeResponseId = event.response?.id ?? "";
        s2sTurnControl.begin(this._activeResponseId, this);
        // Open the router's turn record with the same key the audio deltas will
        // carry, so the sink choice is made before the first byte arrives and
        // never recomputed mid-turn.
        globalThis.s2sAudioRouter?.begin(this._activeResponseId, { sampleRate: AUDIO_SAMPLE_RATE });
        if (this._status === "connected" || this._status === "user-speaking") this._setStatus("processing");
        break;
      case "response.content_part.added":
        if (event.part?.type === "audio" || event.part?.type === "output_audio") this._markAudible();
        break;
      case "conversation.item.input_audio_transcription.delta": {
        const delta = typeof event.delta === "string" ? event.delta : "";
        if (delta) {
          const itemId = typeof event.item_id === "string" ? event.item_id : "";
          const text = (this._userTranscriptByItem.get(itemId) || "") + delta;
          this._userTranscriptByItem.set(itemId, text);
          this.dispatchEvent(new CustomEvent("transcript", { detail: {
            role: "user", text, partial: true, itemId,
          } }));
        }
        break;
      }
      case "conversation.item.input_audio_transcription.completed": {
        const text = typeof event.transcript === "string" ? event.transcript : "";
        if (this._micDiagnostic) this._micDiagnostic.transcripts.push({ at: Date.now(), itemId: event.item_id, text });
        const itemId = typeof event.item_id === "string" ? event.item_id : "";
        const hadPartial = Boolean(this._userTranscriptByItem.get(itemId));
        this._userTranscriptByItem.delete(itemId);
        const isCurrentUserItem = this._currentUserItemId
          ? Boolean(itemId) && itemId === this._currentUserItemId
          : true;
        if (text || hadPartial) this.dispatchEvent(new CustomEvent("transcript", { detail: {
          role: "user", text, partial: false, itemId,
        } }));
        if (
          !text
          && isCurrentUserItem
          && this._status === "processing"
          && !this._activeResponseId
          && !this._responseRequested
        ) {
          this._setStatus("connected");
        }
        if (itemId && itemId === this._currentUserItemId) this._currentUserItemId = "";
        break;
      }
      case "conversation.item.input_audio_transcription.failed": {
        const itemId = typeof event.item_id === "string" ? event.item_id : "";
        const isCurrentUserItem = this._currentUserItemId
          ? Boolean(itemId) && itemId === this._currentUserItemId
          : true;
        this._userTranscriptByItem.delete(itemId);
        if (itemId && itemId === this._currentUserItemId) this._currentUserItemId = "";
        if (
          isCurrentUserItem
          && this._status === "processing"
          && !this._activeResponseId
          && !this._responseRequested
        ) {
          this._setStatus("connected");
        }
        const message = event.error?.message ?? "Input audio transcription failed";
        this.dispatchEvent(new CustomEvent("server-error", { detail: { error: new Error(message) } }));
        break;
      }
      case "response.audio_transcript.delta":
      case "response.output_audio_transcript.delta":
      case "response.output_text.delta": {
        const responseId = typeof event.response_id === "string" ? event.response_id : "";
        const delta = typeof event.delta === "string" ? event.delta : "";
        if (delta) {
          this._asstTranscriptByResp.set(responseId, (this._asstTranscriptByResp.get(responseId) || "") + delta);
          this.dispatchEvent(new CustomEvent("transcript", { detail: {
            role: "assistant", text: this._asstDisplay(responseId), partial: true, responseId,
          } }));
        }
        break;
      }
      case "response.audio_transcript.done":
      case "response.output_audio_transcript.done":
      // Some providers carry only a text segment alongside generated audio;
      // its field is `text`, not `transcript`.
      case "response.output_text.done": {
        const responseId = typeof event.response_id === "string" ? event.response_id : "";
        const segment = event.transcript || event.text
          || this._asstTranscriptByResp.get(responseId) || "";
        this._asstTranscriptByResp.delete(responseId);
        if (segment) {
          const previous = this._asstFullByResp.get(responseId) || "";
          this._asstFullByResp.set(responseId, previous ? `${previous} ${segment}` : segment);
          this.dispatchEvent(new CustomEvent("transcript", { detail: {
            role: "assistant", text: this._asstFullByResp.get(responseId), partial: false, responseId,
          } }));
        }
        break;
      }
      case "response.done": {
        // `response.done` carries the id in the GA protocol, but a frame without
        // one must not leave the router's turn open (that turn's audio would sit
        // in the retained buffer until the pruner ate it).  Fall back to the id
        // we last saw active.
        const responseId = event.response?.id || this._activeResponseId || "";
        this._responseRequested = false;
        this._activeResponseId = "";
        s2sTurnControl.end(responseId);
        // Close the router's turn: this is what arms the avatar receipt window,
        // and therefore what makes "8010 never answered" become audible audio
        // instead of a silent round (§九).
        globalThis.s2sAudioRouter?.end(responseId, {
          status: event.response?.status ?? "completed",
        });
        this._aiSpeaking = false;
        if (this._status === "ai-speaking" || this._status === "processing") this._setStatus("connected");
        const transcript = extractResponseTranscript(event.response) || this._asstDisplay(responseId) || "";
        // §十一：这一轮到底有没有出声、从哪个 sink 出的声，由路由层裁决 ——
        // 数字人发声时本机 RMS 本来就是平的，用 `_audibleResponses`（"TTS 产出过字节"）
        // 报 audible 会把"没声音"和"声音在对方嘴里"混成一回事。
        const verdict = globalThis.s2sAudioRouter?.turnAudibility?.(responseId) || null;
        this.dispatchEvent(new CustomEvent("response-finished", { detail: {
          responseId,
          status: event.response?.status ?? "completed",
          audible: verdict ? verdict.audible : this._audibleResponses.has(responseId),
          audioSink: verdict ? verdict.sink : "",
          audioState: verdict ? verdict.state : "",
          transcript,
        } }));
        this._audibleResponses.delete(responseId);
        this._asstTranscriptByResp.delete(responseId);
        this._asstFullByResp.delete(responseId);
        break;
      }
      case "error": {
        if (this._isKeepaliveAnswer(event.error)) {
          // Our own probe's echo (the endpoint has no dedicated keepalive event,
          // so it answers "unknown event type"). `_noteInbound()` already scored
          // it; surfacing it as a server error would be UI noise, not truth.
          break;
        }
        const message = event.error?.message ?? "Server error";
        this.dispatchEvent(new CustomEvent("server-error", { detail: { error: new Error(message) } }));
        break;
      }
    }
  }

  /** @param {string} responseId */
  _asstDisplay(responseId) {
    const full = this._asstFullByResp.get(responseId) || "";
    const partial = this._asstTranscriptByResp.get(responseId) || "";
    return full && partial ? `${full} ${partial}` : full || partial;
  }

  _markAudible() {
    if (this._status === "closed" || this._status === "error" || this._status === "reconnecting") return;
    this._setStatus("ai-speaking");
  }

  /** @param {{voice?: string, instructions?: string}} patch */
  updateSession(patch) {
    if (patch.voice) this.options.voice = patch.voice;
    if (patch.instructions) this.options.instructions = patch.instructions;
    if (!this._session) return Promise.resolve(false);
    this._agent = this._buildAgent();
    const session = this._session;
    const agent = this._agent;
    // The SDK promise resolves after session.update is sent, not a server ACK.
    // Serialize live updates so a slower older agent cannot overwrite a newer one.
    const update = (this._instructionUpdate || Promise.resolve()).catch(() => {}).then(async () => {
      if (session !== this._session) return false;
      await session.updateAgent(agent);
      return session === this._session;
    });
    this._instructionUpdate = update;
    // Existing fire-and-forget callers still report failures without an unhandled rejection.
    void update.catch((error) => console.warn('[instructions] update failed', error));
    return update;
  }

  /** @param {ToolDef[]} tools */
  setTools(tools) {
    this._tools = tools;
    if (!this._session) return;
    this._agent = this._buildAgent();
    const clearTools = tools.length === 0;
    void this._session.updateAgent(this._agent).then(() => {
      // @openai/agents 0.14.3 omits the `tools` field when its list is empty.
      // Send the explicit GA clear through the stock transport hook so the
      // server does not retain tools from its deep-merged session config.
      if (clearTools && this._tools.length === 0) {
        this._transport?.sendEvent({
          type: "session.update",
          session: { type: "realtime", tools: [], tool_choice: "none" },
        });
      }
    });
  }

  /** @param {{image?: string}} [options] */
  requestResponse(options = {}) {
    if (options.image) this._session?.addImage(options.image, { triggerResponse: false });
    this._responseRequested = true;
    this._sendManualVoice();
    this._transport?.requestResponse();
  }

  /**
   * The set of backends the app is allowed to name comes from the provider
   * registry (GET /api/voice-models -> capability rows), never from a literal here:
   * a hardcoded list silently dropped every new provider's selection (D-18).
   * @param {string[]} ids
   */
  setKnownVoiceBackends(ids) {
    this._knownVoiceBackends = new Set((ids || []).filter((id) => typeof id === 'string' && id));
  }

  /** @param {{tts_backend: string, manual_voice_mode: Record<string, string>} | undefined} selection */
  setManualVoiceMode(selection) {
    if (!selection || typeof selection.tts_backend !== 'string' || !selection.tts_backend) return;
    const known = this._knownVoiceBackends;
    if (known && known.size && !known.has(selection.tts_backend)) return;
    this._manualVoiceSelection = {
      tts_backend: selection.tts_backend,
      manual_voice_mode: selection.manual_voice_mode && typeof selection.manual_voice_mode === 'object'
        ? { ...selection.manual_voice_mode } : {},
    };
    this._sendManualVoice();
  }

  _sendManualVoice() {
    if (!this._manualTransportReady || !this._manualVoiceSelection) return;
    this._transport?.sendEvent({
      type: 'session.update',
      session: { type: 'realtime', ...this._manualVoiceSelection },
    });
  }

  /** @param {string} dataUrl */
  sendUserImage(dataUrl) {
    this._session?.addImage(dataUrl, { triggerResponse: false });
  }

  /**
   * Cancel the in-flight response on BOTH ends with one decision.
   * Public so the page (and a test) never re-implements what an interrupt means.
   * @param {"user_interrupt"|"barge_in"|"manual_stop"|"unspecified"} [reason]
   */
  cancelTurn(reason = "manual_stop") {
    return s2sTurnControl.cancel(reason, { responseId: this._activeResponseId });
  }

  /**
   * Type instead of speak: one user message in, one reply out.
   *
   * `conversation.item.create` with `input_text` lands in the *same* server-side
   * conversation as a transcribed voice turn (the LLM handler reads it through
   * the identical path), so text and voice keep sharing one memory without this
   * client ever reconfiguring the session.
   *
   * Every typed turn requests the default audio response, so it follows the
   * same active TTS and avatar-sync path as a voice turn.
   *
   * @param {string} text
   * @returns {boolean} false when there is no live transport to write to.
   */
  sendUserText(text) {
    const body = (text || "").trim();
    const transport = /** @type {any} */ (this._transport);
    if (!body || !transport?.sendMessage) return false;
    this._lastSendAt = Date.now();
    // triggerResponse:false keeps item creation and generation separate, which
    // is the only way to attach a per-response modality override.
    transport.sendMessage(body, {}, { triggerResponse: false });
    this._responseRequested = true;
    this._sendManualVoice();
    transport.requestResponse();
    // No STT runs for typed input, so nothing else will ever surface this turn
    // in the history panel; echo it the way a transcript would.
    const itemId = `_t${Date.now().toString(36)}`;
    this.dispatchEvent(new CustomEvent("transcript", {
      detail: { role: "user", text: body, partial: false, itemId, source: "text" },
    }));
    return true;
  }

  /** @param {boolean} muted */
  setMuted(muted) {
    this._muted = muted;
    if (this.options.transport === "webrtc") this._session?.mute(muted);
  }

  /** @param {NoiseGate} gate */
  setNoiseGate(gate) {
    this._noiseGate = gate;
    this._captureNode?.port.postMessage({ kind: "gate", ...gate });
  }

  /** @param {string} [deviceId] */
  async setAudioOutputDevice(deviceId = "") {
    if (!this._ctx || typeof /** @type {any} */ (this._ctx).setSinkId !== "function") return false;
    try {
      await /** @type {any} */ (this._ctx).setSinkId(deviceId || "");
      return true;
    } catch (error) {
      console.warn("[audio] setSinkId failed:", error);
      return false;
    }
  }

  async _createSessionOrQueue() {
    const first = await this._postSession();
    if (first.state !== "queued") return { grant: first.grant, waited: false };
    this._setStatus("queued");
    return { grant: await this._pollQueue(first), waited: true };
  }

  /** @param {SessionInfo} grant */
  _awaitJoin(grant) {
    const windowSeconds = Math.max(3, (grant.pendingTimeoutS || 60) - 3);
    this._setStatus("your-turn");
    this.dispatchEvent(new CustomEvent("ready-to-join", {
      detail: { info: grant, expiresSec: windowSeconds },
    }));
    return new Promise((resolve, reject) => {
      this._joinResolve = resolve;
      this._joinReject = reject;
      this._joinTimer = setTimeout(() => {
        this._joinResolve = null;
        this._joinReject = null;
        reject(codedError("Your spot expired", "join-expired"));
      }, windowSeconds * 1000);
    });
  }

  join() {
    if (this._joinTimer) clearTimeout(this._joinTimer);
    this._joinTimer = 0;
    void this.options.audioContext?.resume().catch(() => {});
    const resolve = this._joinResolve;
    this._joinResolve = null;
    this._joinReject = null;
    resolve?.();
  }

  async _postSession() {
    const response = await fetch(this._sessionUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (response.status === 402) {
      const body = await response.json().catch(() => ({}));
      throw codedError("Daily conversation limit reached", "limit", { tier: body?.tier });
    }
    if (response.status === 401) {
      const body = await response.json().catch(() => ({}));
      throw codedError("Sign in again to continue", "login-required", { loginUrl: body?.loginUrl });
    }
    if (response.status === 503) {
      const body = await response.json().catch(() => ({}));
      if (body?.state === "at_capacity") throw codedError("The queue is full — try again shortly.", "queue-full");
    }
    if (!response.ok) throw new Error(`/session failed (${response.status}): ${await response.text()}`);
    const json = await response.json();
    if (json.state === "queued") return {
      state: "queued", queueId: json.queue_id, position: json.position, pollIntervalS: json.poll_interval_s,
    };
    return { state: "granted", grant: this._parseGrant(json) };
  }

  /** @param {{queueId: string, position: number, pollIntervalS: number}} ticket */
  async _pollQueue(ticket) {
    const interval = Math.max(1, ticket.pollIntervalS || 2) * 1000;
    this._queueId = ticket.queueId;
    this._emitQueue(ticket.position);
    while (true) {
      await this._queueSleep(interval);
      if (this._closed) throw codedError("queue wait aborted", "aborted");
      let response;
      try {
        response = await fetch(`api/queue/${encodeURIComponent(this._queueId)}`);
      } catch { continue; }
      if (response.status === 402) {
        const body = await response.json().catch(() => ({}));
        throw codedError("Daily conversation limit reached", "limit", { tier: body?.tier });
      }
      if (response.status === 401) {
        const body = await response.json().catch(() => ({}));
        throw codedError("Sign in again to continue", "login-required", { loginUrl: body?.loginUrl });
      }
      if (response.status === 404) throw codedError("Queue timed out", "queue-expired");
      if (!response.ok) continue;
      const json = await response.json().catch(() => null);
      if (!json) continue;
      if (json.state === "queued") {
        this._emitQueue(json.position);
        continue;
      }
      this._queueId = "";
      return this._parseGrant(json);
    }
  }

  /** @param {number} position */
  _emitQueue(position) {
    this.dispatchEvent(new CustomEvent("queue", { detail: { position, queueId: this._queueId } }));
  }

  /** @param {number} ms */
  _queueSleep(ms) {
    return new Promise((resolve) => {
      this._queueWake = resolve;
      this._queueTimer = setTimeout(() => {
        this._queueWake = null;
        resolve();
      }, ms);
    });
  }

  /** @param {any} json @returns {SessionInfo} */
  _parseGrant(json) {
    return {
      sessionId: json.session_id,
      connectUrl: json.connect_url,
      websocketUrl: json.websocket_url,
      sessionToken: json.session_token,
      pendingTimeoutS: json.pending_timeout_s,
      tier: json.tier,
      limited: json.limited,
      heartbeatSec: json.heartbeatSec,
      remainingSec: json.remainingSec,
    };
  }

  async close() {
    this._closed = true;
    this._closing = true;
    // A user- or app-initiated close outranks the recovery loop: cancel a
    // scheduled retry before it can dial over the top of the teardown.
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = 0;
    }
    this._reconnectAttempt = 0;
    this._dropping = false;
    this._stopHeartbeat();
    await this._retireGeneration(this._generation, "closed");
    this._userAudioRecorder.reset();
    this._userTranscriptByItem.clear();
    this._currentUserItemId = "";
    if (this._queueWake) {
      clearTimeout(this._queueTimer);
      this._queueWake();
      this._queueWake = null;
    }
    if (this._joinTimer) clearTimeout(this._joinTimer);
    this._joinTimer = 0;
    if (this._joinReject) this._joinReject(codedError("join aborted", "aborted"));
    this._joinResolve = null;
    this._joinReject = null;
    if (this._levelTimer) clearInterval(this._levelTimer);
    this._levelTimer = 0;
    this._visualiser?.stop();
    this._visualiser = null;
    this._session?.close();
    this._session = null;
    this._transport = null;
    for (const node of [this._captureNode, this._playbackNode, this._micSrc, this._micAnalyser, this._remoteSrc, this._outAnalyser]) {
      try { node?.disconnect(); } catch { /* ignored */ }
    }
    try { await this._ctx?.close(); } catch { /* ignored */ }
    this._ctx = null;
    this._setStatus("closed");
  }
}
