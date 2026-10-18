// @ts-check
/**
 * Minimal voice conversation app, talking to a Hugging Face speech-to-speech
 * backend over **WebSocket** or **WebRTC**.
 *
 * Click the orb -> we ask for the mic, connect (WS dial, or SDP handshake via
 * the /api/calls proxy), push session.update + mic audio, play back the TTS
 * audio. The orb visually reflects the live state (idle, connecting,
 * listening, user-speaking, processing, ai-speaking).
 *
 * One adapter drives the official Agents SDK RealtimeSession over either stock
 * transport. WebRTC is offered only in env-pinned direct mode (the /api/calls
 * proxy forwards exclusively to SPEECH_TO_SPEECH_URL); LB mode and user-typed
 * URLs stay on WebSocket.
 *
 * @typedef {"idle" | "connecting" | "queued" | "your-turn" | "listening" | "user-speaking" | "processing" | "ai-speaking" | "reconnecting" | "error"} AppState
 * @typedef {S2sRealtimeClient} RealtimeClient
 */

import { S2sRealtimeClient } from "./s2s-realtime-client.js?v=session-capacity-20261018";
import { $, truncateError, DEBUG } from "./ui/dom.js";
import { ChatView } from "./ui/chat.js?v=console-v1";
import { Account } from "./ui/account.js";
import { startZhCn } from "./ui/zh_cn.js?v=capability-ui-05";
import { mountGirlfriend, setLive, manualSelection } from "./ui/girlfriend.js?v=voice-refresh-01";
import { mountReading, pauseForUserTurn, resumeAfterAssistantReply,
         stopForModeSwitch, readingInstructionContext } from "./ui/reading.js?v=reading-pipeline-v17-book-selection";
import { ReadingInstructionGuard } from "./ws/reading-instructions.js?v=20261004b";
// 书库管理 + 音色管理（2026-10-15）：独立模块，不动既有面板。
import { mountReadingManager } from "./ui/reading_manager.js?v=ra-manage-02";
import { mountVoiceLibrary } from "./ui/voice_library.js?v=vl-transcribe-01";

const DEFAULT_VOICE = "Aiden";
const LEGACY_DEFAULT_INSTRUCTIONS = "You are a friendly voice assistant.";
const DEFAULT_INSTRUCTIONS = "你是一位友好、自然的中文语音助手。默认用简体中文简洁回答；只有用户明确要求其他语言时才切换语言。";

const STORAGE_KEYS = {
  // Direct s2s server URL, used only when the deploy has no LOAD_BALANCER_URL
  // (in LB mode the browser never learns the LB address — it POSTs /api/session).
  directUrl: "s2s.ws.directUrl",
  voice: "s2s.ws.voice",
  instructions: "s2s.ws.instructions",
  tools: "s2s.ws.tools",
  searchKey: "s2s.ws.searchKey",
  noiseGate: "s2s.ws.noiseGate",
  // "ws" | "webrtc". Not under the historical "s2s.ws." prefix — it selects
  // between the transports rather than configuring the WS one.
  transport: "s2s.transport",
  audioInputId: "s2s.audio.inputId",
  audioOutputId: "s2s.audio.outputId",
};

// ── Noise gate ──────────────────────────────────────────────────────────────
// The Settings cursor sets the gate's open threshold in dBFS. Its leftmost
// position is an OFF detent (gate disabled, pure passthrough); the rest of the
// travel is the active threshold. The cursor shares the meter's dB axis, so the
// handle sits on the level bar — raise it until room noise stops lighting it up.
// The slider range IS the shared axis: the live meter fill and the threshold
// thumb both map across [GATE_OFF_DB, GATE_MAX_DB], so the thumb sits exactly
// where the gate cuts on the same scale as the level bar.
const GATE_OFF_DB = -66; // slider minimum = off / bottom of the meter axis
const GATE_MAX_DB = -3; // slider maximum = most aggressive / top of the meter axis
const GATE_DEFAULT_DB = -50; // first-run default: a gentle gate, enabled

/** @param {number} thresholdDb @returns {import("./s2s-realtime-client.js").NoiseGate} */
function gateParams(thresholdDb) {
  return { enabled: thresholdDb > GATE_OFF_DB, thresholdDb };
}

// ── Tools ─────────────────────────────────────────────────────────────────
// Function tools we declare to the backend. The model decides when to call
// one; the executor below runs it and returns the result (see runTool).
/** @type {Record<string, import("./s2s-realtime-client.js").ToolDef>} */
const TOOL_DEFS = {
  web_search: {
    type: "function",
    name: "web_search",
    description:
      "Search the web for current or factual information you don't already know " +
      "(news, prices, facts, documentation). Returns the top results with titles, " +
      "snippets and URLs.",
    parameters: {
      type: "object",
      properties: { query: { type: "string", description: "The search query." } },
      required: ["query"],
    },
  },
  camera_snapshot: {
    type: "function",
    name: "camera_snapshot",
    description:
      "Capture the current frame from the user's webcam so you can see what they " +
      "are showing you. Use it whenever the user refers to something visual or " +
      "asks you to look.",
    parameters: { type: "object", properties: {}, required: [] },
  },
};

/** Longest edge of the snapshot sent to the VLM, in px (keeps payload sane). */
const SNAPSHOT_MAX_EDGE = 768;
const SNAPSHOT_QUALITY = 0.7;
// Over WebRTC the snapshot travels inside ONE data-channel message, and SCTP
// messages above the negotiated max (64 KiB on the aiortc side) fail to send.
// Budget for the data-URL portion, leaving headroom for the JSON envelope.
const SNAPSHOT_DC_BUDGET_CHARS = 60_000;
// Re-encode ladder walked until the frame fits the budget: quality first
// (cheap wins), then resolution. The last rung is ~15–25 KB for any content,
// so a frame that "fits" is deterministic, not content-dependent luck.
const SNAPSHOT_LADDER = /** @type {[number, number][]} */ ([
  [SNAPSHOT_MAX_EDGE, SNAPSHOT_QUALITY],
  [SNAPSHOT_MAX_EDGE, 0.5],
  [640, 0.45],
  [512, 0.4],
  [448, 0.35],
  [384, 0.3],
]);

function loadSettings() {
  return {
    directUrl: localStorage.getItem(STORAGE_KEYS.directUrl) || "",
    voice: localStorage.getItem(STORAGE_KEYS.voice) || DEFAULT_VOICE,
    instructions: (() => {
      const saved = localStorage.getItem(STORAGE_KEYS.instructions);
      return !saved || saved === LEGACY_DEFAULT_INSTRUCTIONS ? DEFAULT_INSTRUCTIONS : saved;
    })(),
    noiseGate: loadGateThreshold(),
    // Default WebSocket: the proven path stays the first-run experience.
    transport: localStorage.getItem(STORAGE_KEYS.transport) === "webrtc" ? "webrtc" : "ws",
    audioInputId: localStorage.getItem(STORAGE_KEYS.audioInputId) || "",
    audioOutputId: localStorage.getItem(STORAGE_KEYS.audioOutputId) || "",
  };
}

/** Stored gate threshold (dBFS), clamped to the slider range. Defaults to a
 * gentle enabled gate (GATE_DEFAULT_DB) when the user hasn't set one yet. */
function loadGateThreshold() {
  const stored = localStorage.getItem(STORAGE_KEYS.noiseGate);
  // getItem returns null when unset, and Number(null) === 0 (finite!), so guard
  // the missing/empty case explicitly before coercing — otherwise the default
  // never fires and 0 clamps to the slider max.
  if (stored === null || stored === "") return GATE_DEFAULT_DB;
  const raw = Number(stored);
  if (!Number.isFinite(raw)) return GATE_DEFAULT_DB;
  return Math.min(GATE_MAX_DB, Math.max(GATE_OFF_DB, Math.round(raw)));
}

/** @param {ReturnType<typeof loadSettings>} s */
function saveSettings(s) {
  localStorage.setItem(STORAGE_KEYS.directUrl, s.directUrl);
  localStorage.setItem(STORAGE_KEYS.voice, s.voice);
  localStorage.setItem(STORAGE_KEYS.instructions, s.instructions);
  localStorage.setItem(STORAGE_KEYS.noiseGate, String(s.noiseGate));
  localStorage.setItem(STORAGE_KEYS.transport, s.transport);
  localStorage.setItem(STORAGE_KEYS.audioInputId, s.audioInputId || "");
  localStorage.setItem(STORAGE_KEYS.audioOutputId, s.audioOutputId || "");
}

/** @returns {{ web_search: boolean, camera_snapshot: boolean }} */
function loadTools() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEYS.tools) || "{}");
    // Both tools default ON (web search still only activates when a key exists).
    // We never call getUserMedia on page load — the camera only actually starts
    // on a user gesture (conversation start), so a default-on flag doesn't
    // silently resume the webcam; an explicit saved `false` is respected.
    return {
      web_search: raw.web_search ?? true,
      camera_snapshot: raw.camera_snapshot ?? true,
    };
  } catch {
    return { web_search: true, camera_snapshot: true };
  }
}

function saveTools() {
  localStorage.setItem(STORAGE_KEYS.tools, JSON.stringify(toolsEnabled));
}

/** @type {Record<AppState, { caption: string; disabled: boolean }>} */
const STATE_VIEWS = {
  idle:            { caption: "Tap to start",  disabled: false },
  connecting:      { caption: "Connecting",    disabled: true  },
  queued:          { caption: "Finding you a spot…", disabled: true },
  "your-turn":     { caption: "You're up! 🎉", disabled: true  },
  listening:       { caption: "",              disabled: false },
  "user-speaking": { caption: "",              disabled: false },
  processing:      { caption: "",              disabled: false },
  "ai-speaking":   { caption: "",              disabled: false },
  // 传输已断，正在自动重连：不算在线（LIVE_STATES 里没有它）。
  // TASK_02 STEP 1：caption 置空 + 不再复用 state-connecting 动画。
  // 短暂断连（<3s）什么都不显示（见 scheduleReconnectUi 去抖）；
  // 持续断连 ≥3s 后才由 applyReconnectUi 显示右上角静态徽标。
  "reconnecting":  { caption: "", disabled: true },
  error:           { caption: "Tap to retry",  disabled: false },
};

/** @type {Record<AppState, string>} */
const STATE_CLASS = {
  idle: "state-idle",
  connecting: "state-connecting",
  queued: "state-queued",
  "your-turn": "state-your-turn",
  listening: "state-listening",
  "user-speaking": "state-user-speaking",
  processing: "state-processing",
  "ai-speaking": "state-ai-speaking",
  // TASK_02 STEP 1：重连不再借用"正在连接"的黄色脉冲动画（那正是截图里
  // 反复出现消失的大圆环）。state-reconnecting 是纯静态样式（见
  // avatar_layout.css），持续断连 ≥3s 才会进入这个状态。
  reconnecting: "state-reconnecting",
  error: "state-error",
};

/** @type {ReadonlySet<AppState>} */
const LIVE_STATES = new Set(["listening", "user-speaking", "processing", "ai-speaking"]);

/** @type {HTMLButtonElement} */
const circleBtn = $("#main-circle");
/** @type {HTMLParagraphElement} */
const circleCaption = $("#circle-caption");
/** @type {HTMLParagraphElement} */
const circleSubcaption = $("#circle-subcaption");
/** @type {HTMLElement} */
const orbWrap = $(".orb-wrap");
/** @type {HTMLButtonElement} */
const micBtn = $("#mic-btn");
/** @type {HTMLButtonElement} */
const stopBtn = $("#stop-btn");
/** @type {HTMLElement} */
const queueActions = $("#queue-actions");
/** @type {HTMLButtonElement} */
const joinQueueBtn = $("#join-queue-btn");
/** @type {HTMLButtonElement} */
const leaveQueueBtn = $("#leave-queue-btn");

/** @type {HTMLButtonElement} */
const settingsBtn = $("#settings-btn");
/** @type {HTMLDialogElement} */
const settingsModal = $("#settings-modal");

/** @type {HTMLButtonElement} */
const aboutBtn = $("#about-btn");
/** @type {HTMLDialogElement} */
const aboutModal = $("#about-modal");
/** @type {HTMLButtonElement} */
const aboutClose = $("#about-close");

/** @type {HTMLButtonElement} */
const toolsBtn = $("#tools-btn");
/** @type {HTMLDialogElement} */
const toolsModal = $("#tools-modal");
/** @type {HTMLButtonElement} */
const toolsClose = $("#tools-close");
/** @type {HTMLInputElement} */
const toolWebSwitch = $("#tool-web");
/** @type {HTMLInputElement} */
const toolCamSwitch = $("#tool-cam");
/** @type {HTMLElement} */
const toolWebRow = $("#tool-web-row");
/** @type {HTMLElement} */
const toolWebHint = $("#tool-web-hint");
/** @type {HTMLElement} */
const toolCamHint = $("#tool-cam-hint");
/** @type {HTMLInputElement} */
const searchKeyInput = $("#search-key");
/** @type {HTMLElement} */
const camPip = $("#cam-pip");
/** @type {HTMLVideoElement} */
const camVideo = $("#cam-video");

/** @type {HTMLInputElement} */
const inputLbUrl = $("#lb-url");
/** @type {HTMLElement} */
const connField = $("#conn-field");
/** @type {HTMLElement} */
const connHint = $("#conn-hint");
/** @type {HTMLElement} */
const transportField = $("#transport-field");
/** @type {HTMLSelectElement} */
const inputTransport = $("#transport");
/** @type {HTMLElement} */
const transportHint = $("#transport-hint");
/** @type {HTMLElement} */
const gateField = $("#gate-field");
/** @type {HTMLSelectElement} */
const inputVoice = $("#voice");
/** @type {HTMLSelectElement} */
const inputAudioInput = $("#audio-input");
/** @type {HTMLSelectElement} */
const inputAudioOutput = $("#audio-output");
/** @type {HTMLElement} */
const audioOutputHint = $("#audio-output-hint");
/** @type {HTMLTextAreaElement} */
const inputInstructions = $("#instructions");
/** @type {HTMLInputElement} */
const inputNoiseGate = $("#noise-gate");
/** @type {HTMLElement} */
const gateValue = $("#gate-value");
/** @type {HTMLElement} */
const gateMeterFill = $("#gate-meter-fill");
/** @type {HTMLElement} */
const micGate = $("#mic-gate");
const mgaArc = /** @type {SVGSVGElement} */ (document.querySelector("#mic-gate-arc"));
const mgaTrack = /** @type {SVGPathElement} */ (document.querySelector("#mga-track"));
const mgaFill = /** @type {SVGPathElement} */ (document.querySelector("#mga-fill"));
const mgaHit = /** @type {SVGPathElement} */ (document.querySelector("#mga-hit"));
const mgaHandle = /** @type {SVGCircleElement} */ (document.querySelector("#mga-handle"));
/** @type {HTMLButtonElement} */
const restartBtn = $("#restart-conversation");
/** @type {HTMLElement} */
const restartHint = $("#restart-hint");
const settingsForm = /** @type {HTMLFormElement} */ (settingsModal.querySelector("form"));

/** @type {AppState} */
let currentState = "idle";
let settings = loadSettings();

// ── Connection target ────────────────────────────────────────────────────────
// Three modes, decided by the deploy via /api/config:
//   • SPEECH_TO_SPEECH_URL set -> direct mode pinned by the deploy: the browser
//     connects straight to that URL, shown read-only in Settings. Overrides the
//     load balancer entirely.
//   • LOAD_BALANCER_URL set  -> original flow: POST the same-origin /api/session
//     proxy (the server forwards to the LB; the LB address is never sent here).
//   • neither (allowDirect)  -> the user sets a speech-to-speech server URL and
//     the browser connects to it directly (no load balancer, no /session).
let lbMode = false;
// Fail open: direct entry is allowed unless /api/config reports an LB URL. This
// way a missing/unreachable config (e.g. static hosting) leaves the field
// usable rather than locked.
let allowDirect = true;
// Deploy-pinned s2s URL (SPEECH_TO_SPEECH_URL). Non-empty -> locked direct
// mode: the field displays it read-only and the saved user URL is untouched.
let pinnedUrl = "";
// Whether the deploy offers the WebRTC transport (/api/config `rtc`; true
// exactly when the URL is env-pinned, since /api/calls only forwards there).
let rtcAvailable = false;
/** @type {RTCIceServer[]} STUN/TURN servers for the browser peer connection
 * (deploy-provided via RTC_ICE_SERVERS; empty -> host candidates only). */
let iceServers = [];
// Optional hidden user prompt supplied by the deployment. When non-empty, the
// client asks the model to greet once after the initial session configuration.
let startupGreeting = "";
// Transport of the LIVE (or starting) conversation — as opposed to
// `settings.transport`, which is what the NEXT one will use. Drives the
// camera-snapshot size budget while a call is running.
/** @type {"ws" | "webrtc"} */
let activeTransport = "ws";

// ── Tool state ──────────────────────────────────────────────────────────────
let toolsEnabled = loadTools();
// Whether the server holds a Serper key (learned from /api/config on load).
let serverSearchKey = false;
// A user-supplied key (fallback when the deploy has none). localStorage only.
let userSearchKey = localStorage.getItem(STORAGE_KEYS.searchKey) || "";
/** @type {MediaStream | null} */
let cameraStream = null;

/** Search is usable if the server has a key or the user supplied one. */
function searchAvailable() {
  return serverSearchKey || !!userSearchKey;
}

/** Tool definitions for the currently-enabled (and usable) tools. */
function activeToolDefs() {
  const defs = [];
  if (toolsEnabled.web_search && searchAvailable()) defs.push(TOOL_DEFS.web_search);
  if (toolsEnabled.camera_snapshot) defs.push(TOOL_DEFS.camera_snapshot);
  return defs;
}

/** Push the active tool set to a live session so toggles apply mid-call. */
function pushToolsToSession() {
  if (!client || !LIVE_STATES.has(currentState)) return;
  client.setTools(activeToolDefs());
}

// ── Chat view ───────────────────────────────────────────────────────────────
// Owns the history panel, the ephemeral bubbles, and all transcript/tool
// streaming state. The client's events are forwarded to its on* methods.
let userAudioReplaying = false;
const chat = new ChatView({
  onUserAudioPlaybackChange(playing) {
    userAudioReplaying = playing;
    syncMicMuteState();
  },
});

// ── Account / limiter ─────────────────────────────────────────────────────
// Login chip + daily-limit modal (inert unless the deploy is in LB mode). The
// server meters conversation time; the client just heartbeats a live session
// and tears down when the server reports the budget is spent.
const account = new Account();
let limiterOn = false;
let loginRequired = false;
let heartbeatTimer = 0;
let trackedSessionId = "";
let trackedTier = "";
// The waiting-queue ticket id while we're in line (else ""). Used to leave the
// queue on teardown / tab-close so we don't hold a phantom place.
let queuedTicketId = "";

/** @type {RealtimeClient | null} */
let client = null;
/** @type {MediaStream | null} */
let micStream = null;
let micMuted = false;
// 文字模式：这次连接不打开麦克风（打字也能开始对话）。
let textOnlyMode = false;

/** Apply both the user's mute choice and the temporary replay guard. */
function syncMicMuteState() {
  const muted = micMuted || userAudioReplaying;
  for (const track of micStream?.getAudioTracks() ?? []) {
    track.enabled = !muted;
  }
  client?.setMuted(muted);
}

/** @param {AppState} next */
function setState(next) {
  currentState = next;
  const view = STATE_VIEWS[next];
  circleBtn.disabled = view.disabled;
  circleBtn.className = `circle ${STATE_CLASS[next]}`;
  if (next !== "error") setCaption(view.caption);

  const live = LIVE_STATES.has(next);
  orbWrap.classList.toggle("live", live);
  micBtn.setAttribute("aria-hidden", live ? "false" : "true");
  stopBtn.setAttribute("aria-hidden", live ? "false" : "true");
  micBtn.tabIndex = live ? 0 : -1;
  stopBtn.tabIndex = live ? 0 : -1;

  // Queue affordances: "Leave queue" whenever we're in line; "Join now" only once
  // it's our turn (a slot is held for us). Both live under #queue-actions.
  const yourTurn = next === "your-turn";
  const inLine = next === "queued" || yourTurn;
  queueActions.hidden = !inLine;
  joinQueueBtn.hidden = !yourTurn;
  joinQueueBtn.tabIndex = yourTurn ? 0 : -1;
  leaveQueueBtn.hidden = !inLine;
  leaveQueueBtn.tabIndex = inLine ? 0 : -1;
  if (!yourTurn) stopJoinCountdown();

  // Warm reassurance under the terse position, only while waiting in line.
  if (next === "queued") {
    circleSubcaption.textContent =
      "Sorry, we overhugged! 🤗 Every slot is busy, so we saved you a spot. Hang tight, you're moving up.";
    circleSubcaption.hidden = false;
  } else {
    circleSubcaption.hidden = true;
  }

  updateRestartAvailability();
}

function updateRestartAvailability() {
  // Restart works from any settled state — it tears down a live call (if any)
  // and reconnects with the current settings. Only block while mid-connect or
  // while waiting in the queue (restarting from there would just re-queue).
  restartBtn.disabled =
    currentState === "connecting" || currentState === "queued" || currentState === "your-turn";
  restartHint.hidden = false;
  restartHint.textContent = LIVE_STATES.has(currentState)
    ? "Reconnects now with the settings above."
    : "Starts a conversation with the settings above.";
}

/**
 * @param {string} text
 * @param {"" | "error" | "muted"} [kind]
 */
function setCaption(text, kind = "") {
  const trimmed = text.trim();
  circleCaption.textContent = trimmed;
  circleCaption.className = `circle-caption${kind ? ` ${kind}` : ""}${trimmed ? "" : " empty"}`;
}

/* ── TASK_02 STEP 1：重连 UI 去抖 + 静态徽标 ─────────────────────────────
 * WebRTC/WebSocket 的 disconnected 往往只是瞬时抖动，客户端的恢复循环通常
 * 在几秒内就重新 connected（MDN 也明确 disconnected 可自行回到 connected）。
 * 旧行为是状态一变立刻切到"黄色大圆环 + 转圈"，抖动时 UI 反复 show/hide，
 * 即用户看到的"重连圆环一直闪"。
 *
 * 新行为（只改显示，不碰客户端恢复循环）：
 *   disconnected < 3s   → UI 完全不变化，仅 console 记录（含 attempt）
 *   disconnected ≥ 3s   → 右上角静态徽标"正在重连…"（无动画、无脉冲）
 *   恢复 connected      → 徽标消失，去抖计时器作废
 *   恢复失败 → error    → 静态徽标"连接已断开"（页面保留 Tap to retry） */
const RECONNECT_DEBOUNCE_MS = 3000;
/** @type {number|null} */
let reconnectDebounce = null;
let reconnectAttemptShown = 0;
/** @type {HTMLElement|null} */
let connBadge = null;

function ensureConnBadge() {
  if (connBadge && connBadge.isConnected) return connBadge;
  connBadge = document.createElement("div");
  connBadge.id = "conn-badge";
  connBadge.className = "conn-badge";
  connBadge.hidden = true;
  connBadge.setAttribute("role", "status");
  document.body.append(connBadge);
  return connBadge;
}

/** 静态提示；无任何动画。variant="error" 时红色边框。 */
function showConnBadge(text, variant = "") {
  const badge = ensureConnBadge();
  badge.textContent = text;
  badge.className = `conn-badge${variant === "error" ? " conn-badge-error" : ""}`;
  badge.hidden = false;
}

function hideConnBadge() {
  if (connBadge) connBadge.hidden = true;
}

/** @param {number} [attempt] 断连后第几次恢复尝试（-1/undefined = 首次） */
function scheduleReconnectUi(attempt = -1) {
  reconnectAttemptShown = attempt;
  // 已在去抖等待中：只更新计数，不重启计时器（抖动计一次）。
  if (reconnectDebounce) return;
  reconnectDebounce = setTimeout(() => {
    reconnectDebounce = null;
    setLive(false);
    setState("reconnecting");
    showConnBadge(reconnectAttemptShown > 0
      ? `正在重连…（第 ${reconnectAttemptShown} 次）`
      : "正在重连…");
  }, RECONNECT_DEBOUNCE_MS);
}

function cancelReconnectUi() {
  if (reconnectDebounce) {
    clearTimeout(reconnectDebounce);
    reconnectDebounce = null;
  }
  hideConnBadge();
}

function openSettings() {
  syncConnectionUi();
  inputVoice.value = settings.voice;
  inputInstructions.value = settings.instructions;
  syncGateUi();
  updateRestartAvailability();
  void refreshAudioDeviceLists();
  settingsModal.showModal();
}

/** dB position (clamped to the slider axis) as a 0..1 fraction of the track.
 * @param {number} db */
function dbToFraction(db) {
  const clamped = Math.min(GATE_MAX_DB, Math.max(GATE_OFF_DB, db));
  return (clamped - GATE_OFF_DB) / (GATE_MAX_DB - GATE_OFF_DB);
}

/** @param {number} f @returns {number} dB at a 0..1 position on the gate axis. */
function fractionToDb(f) {
  const clamped = Math.min(1, Math.max(0, f));
  return Math.round(GATE_OFF_DB + clamped * (GATE_MAX_DB - GATE_OFF_DB));
}

// ── Radial gate arc (around the mic button, live during a call) ─────────────
// A 270° arc with the gap facing the orb (right). Fraction 0 (=Off) sits at the
// bottom-ish start; 1 (=max) at the top-ish end. The level fill and the
// threshold handle ride this same axis, mirroring the Settings widget.
const ARC_R = 40;
// A ~200° arc centred on the left (180°) so the wide gap faces the orb (right).
const ARC_SPAN_DEG = 200;
const ARC_START_DEG = 180 - ARC_SPAN_DEG / 2; // lower-left start; Off end

/** Point at fraction f (0..1) and radius r, in the 0..100 viewBox.
 * @param {number} f @param {number} [r] */
function arcPoint(f, r = ARC_R) {
  const deg = ARC_START_DEG + f * ARC_SPAN_DEG;
  const rad = (deg * Math.PI) / 180;
  return { x: 50 + r * Math.cos(rad), y: 50 + r * Math.sin(rad) };
}

/** SVG path `d` for the full 0..1 arc (clockwise). */
function fullArcD() {
  const a = arcPoint(0);
  const b = arcPoint(1);
  const largeArc = ARC_SPAN_DEG > 180 ? 1 : 0;
  return `M ${a.x} ${a.y} A ${ARC_R} ${ARC_R} 0 ${largeArc} 1 ${b.x} ${b.y}`;
}

/** One-time geometry: track, fill (dash-revealed) and the transparent hit band. */
function initGateArc() {
  const d = fullArcD();
  mgaTrack.setAttribute("d", d);
  mgaFill.setAttribute("d", d);
  mgaHit.setAttribute("d", d);
  // pathLength 100 lets us reveal the fill by fraction via dashoffset.
  mgaFill.setAttribute("pathLength", "100");
  mgaFill.style.strokeDasharray = "100 100";
  mgaFill.style.strokeDashoffset = "100"; // empty until levels arrive
  renderGateHandle();
}

/** Place the threshold bead on the arc at the stored threshold; flag off state. */
function renderGateHandle() {
  const off = settings.noiseGate <= GATE_OFF_DB;
  const p = arcPoint(dbToFraction(settings.noiseGate));
  mgaHandle.setAttribute("cx", String(p.x));
  mgaHandle.setAttribute("cy", String(p.y));
  micGate.classList.toggle("gate-off", off);
}

/** Paint a 0..1 live level onto the arc fill (and the Settings meter if open).
 * Brightens the tick when the level crosses the threshold — i.e. the gate is
 * actually open — but only when gating is enabled.
 * @param {number} rms */
function paintInputLevel(rms) {
  const db = rms > 0 ? 20 * Math.log10(rms) : GATE_OFF_DB;
  const f = dbToFraction(db);
  mgaFill.style.strokeDashoffset = String(100 * (1 - f));
  if (settingsModal.open) gateMeterFill.style.width = `${f * 100}%`;
  const enabled = settings.noiseGate > GATE_OFF_DB;
  micGate.classList.toggle("gate-open", enabled && f >= dbToFraction(settings.noiseGate));
}

/** The single place that commits a new gate threshold: updates both controls,
 * persists, and applies live to the running session.
 * @param {number} db */
function setGateThreshold(db) {
  settings.noiseGate = Math.min(GATE_MAX_DB, Math.max(GATE_OFF_DB, Math.round(db)));
  const off = settings.noiseGate <= GATE_OFF_DB;
  inputNoiseGate.value = String(settings.noiseGate);
  gateValue.textContent = off ? "Off" : `${settings.noiseGate} dB`;
  renderGateHandle();
  localStorage.setItem(STORAGE_KEYS.noiseGate, String(settings.noiseGate));
  if (client && LIVE_STATES.has(currentState)) {
    client.setNoiseGate(gateParams(settings.noiseGate));
  }
}

/** Reflect the stored gate threshold into the slider, label and arc handle. */
function syncGateUi() {
  inputNoiseGate.value = String(settings.noiseGate);
  const off = settings.noiseGate <= GATE_OFF_DB;
  gateValue.textContent = off ? "Off" : `${settings.noiseGate} dB`;
  renderGateHandle();
}

// Drag along the arc band to set the threshold (a tap on the glyph still mutes).
let gateDragging = false;
/** @param {PointerEvent} e */
function gatePointerToDb(e) {
  const rect = mgaArc.getBoundingClientRect();
  const cx = rect.left + rect.width / 2;
  const cy = rect.top + rect.height / 2;
  let deg = (Math.atan2(e.clientY - cy, e.clientX - cx) * 180) / Math.PI;
  if (deg < 0) deg += 360;
  // Map the on-arc angle to a fraction; angles in the right-side gap fall
  // outside [0,1] and fractionToDb clamps them to the nearest end (just-below
  // start -> Off, just-past end -> max).
  const f = (deg - ARC_START_DEG) / ARC_SPAN_DEG;
  return fractionToDb(f);
}
mgaHit.addEventListener("pointerdown", (e) => {
  gateDragging = true;
  mgaHit.setPointerCapture(e.pointerId);
  setGateThreshold(gatePointerToDb(e));
});
mgaHit.addEventListener("pointermove", (e) => {
  if (gateDragging) setGateThreshold(gatePointerToDb(e));
});
const endGateDrag = (/** @type {PointerEvent} */ e) => {
  if (!gateDragging) return;
  gateDragging = false;
  try { mgaHit.releasePointerCapture(e.pointerId); } catch {}
};
mgaHit.addEventListener("pointerup", endGateDrag);
mgaHit.addEventListener("pointercancel", endGateDrag);

settingsBtn.addEventListener("click", openSettings);

// About panel: native <dialog>, Esc closes for free; also close on the X and
// on a click in the backdrop (a click whose target is the dialog itself).
aboutBtn.addEventListener("click", () => aboutModal.showModal());
// Mobile twin of the (i), living in the right-hand control cluster.
$("#about-btn-m").addEventListener("click", () => aboutModal.showModal());
aboutClose.addEventListener("click", () => aboutModal.close());
aboutModal.addEventListener("click", (e) => {
  if (e.target === aboutModal) aboutModal.close();
});

// ── Tools panel ───────────────────────────────────────────────────────────

/** Reflect the current tool state into the panel controls. */
function syncToolsUi() {
  const avail = searchAvailable();
  toolWebSwitch.checked = toolsEnabled.web_search && avail;
  toolWebSwitch.disabled = !avail;
  toolWebRow.classList.toggle("disabled", !avail);
  toolCamSwitch.checked = toolsEnabled.camera_snapshot;

  if (serverSearchKey) {
    // Key lives server-side: show it as configured, never expose it.
    searchKeyInput.value = "";
    searchKeyInput.placeholder = "••••••••  · provided by the server";
    searchKeyInput.disabled = true;
    toolWebHint.textContent = "Ready. The search key is held server-side and never sent to your browser.";
  } else {
    searchKeyInput.disabled = false;
    searchKeyInput.value = userSearchKey;
    searchKeyInput.placeholder = "Paste a Serper key to enable web search";
    toolWebHint.textContent = userSearchKey
      ? "Using your key — stored in this browser only."
      : "No server key configured. Add your own Serper key to enable web search.";
  }
}

toolsBtn.addEventListener("click", () => { syncToolsUi(); toolsModal.showModal(); });
toolsClose.addEventListener("click", () => toolsModal.close());
toolsModal.addEventListener("click", (e) => {
  if (e.target === toolsModal) toolsModal.close();
});

toolWebSwitch.addEventListener("change", () => {
  if (toolWebSwitch.checked && !searchAvailable()) {
    toolWebSwitch.checked = false; // guard: can't enable without a key
    return;
  }
  toolsEnabled.web_search = toolWebSwitch.checked;
  saveTools();
  pushToolsToSession();
});

toolCamSwitch.addEventListener("change", async () => {
  if (toolCamSwitch.checked) {
    try {
      // Flipping the switch always re-requests the camera, so a permission that
      // was only dismissed earlier is asked again here.
      await enableCamera();
    } catch (err) {
      toolCamSwitch.checked = false;
      const denied = err instanceof Error && (err.name === "NotAllowedError" || err.name === "SecurityError");
      toolCamHint.textContent = denied
        ? "Camera blocked. Allow it from the camera icon in your browser's address bar — it switches on automatically."
        : `Camera unavailable${err instanceof Error ? `: ${err.message}` : ""}`;
      return;
    }
    toolsEnabled.camera_snapshot = true;
    toolCamHint.textContent = "Camera on. The assistant can take a snapshot when it needs to see.";
  } else {
    disableCamera();
    toolsEnabled.camera_snapshot = false;
    toolCamHint.textContent = "Let the assistant see through your webcam.";
  }
  saveTools();
  pushToolsToSession();
});

searchKeyInput.addEventListener("input", () => {
  if (serverSearchKey) return;
  userSearchKey = searchKeyInput.value.trim();
  if (userSearchKey) localStorage.setItem(STORAGE_KEYS.searchKey, userSearchKey);
  else localStorage.removeItem(STORAGE_KEYS.searchKey);

  const avail = searchAvailable();
  toolWebSwitch.disabled = !avail;
  toolWebRow.classList.toggle("disabled", !avail);
  // Losing the key disables a previously-enabled tool.
  if (!avail && toolsEnabled.web_search) {
    toolsEnabled.web_search = false;
    toolWebSwitch.checked = false;
    saveTools();
    pushToolsToSession();
  }
  toolWebHint.textContent = userSearchKey
    ? "Using your key — stored in this browser only."
    : "No server key configured. Add your own Serper key to enable web search.";
});

// ── Camera ──────────────────────────────────────────────────────────────────

async function enableCamera() {
  if (cameraStream) return;
  cameraStream = await navigator.mediaDevices.getUserMedia({
    video: { facingMode: "user" },
    audio: false,
  });
  camVideo.srcObject = cameraStream;
  try { await camVideo.play(); } catch { /* autoplay quirks; muted video is fine */ }
  camPip.classList.add("visible");
  camPip.setAttribute("aria-hidden", "false");
  // Lets the footer reflow to the bottom-right (and hide on mobile) while the
  // webcam preview occupies the bottom of the stage.
  document.body.classList.add("cam-on");
}

function disableCamera() {
  if (cameraStream) {
    for (const t of cameraStream.getTracks()) t.stop();
    cameraStream = null;
  }
  camVideo.srcObject = null;
  camPip.classList.remove("visible");
  camPip.setAttribute("aria-hidden", "true");
  document.body.classList.remove("cam-on");
}

/** Auto-start the webcam on arrival (the camera tool is on by default). If the
 *  user declines the permission, switch the tool off and reflect it in the UI
 *  rather than nagging. */
async function autoStartCamera() {
  if (!toolsEnabled.camera_snapshot || cameraStream) return;
  try {
    await enableCamera();
  } catch (err) {
    console.warn("[main] camera auto-start declined/failed:", err);
    toolsEnabled.camera_snapshot = false;
    saveTools();
    syncToolsUi();
  }
}

/** Track the browser's camera permission so a later re-grant (e.g. the user
 *  unblocks it from the address bar after a denial) turns the camera back on
 *  without another toggle, and a revoke turns it off. Best-effort: the
 *  Permissions API doesn't support "camera" everywhere (e.g. Safari). */
async function watchCameraPermission() {
  try {
    const status = await navigator.permissions?.query?.({ name: /** @type {any} */ ("camera") });
    if (!status) return;
    status.addEventListener("change", () => {
      if (status.state === "granted") {
        if (!toolsEnabled.camera_snapshot) { toolsEnabled.camera_snapshot = true; saveTools(); }
        void autoStartCamera();
        syncToolsUi();
      } else if (status.state === "denied") {
        disableCamera();
        if (toolsEnabled.camera_snapshot) { toolsEnabled.camera_snapshot = false; saveTools(); }
        syncToolsUi();
      }
    });
  } catch {
    // Permissions API unavailable for "camera" — the toggle still re-asks.
  }
}

/**
 * Grab the current webcam frame as a downscaled JPEG data URL. The preview is
 * mirrored in CSS for a natural self-view, but we draw the raw (un-mirrored)
 * video here so the model sees the scene in its true orientation.
 *
 * Over WebRTC the frame must fit one data-channel message, so it's re-encoded
 * down the SNAPSHOT_LADDER until it's under SNAPSHOT_DC_BUDGET_CHARS; over
 * WebSocket the first (full-quality) rung is used as before.
 * @returns {string | null}
 */
function captureSnapshot() {
  if (!cameraStream || !camVideo.videoWidth) return null;
  const vw = camVideo.videoWidth;
  const vh = camVideo.videoHeight;

  /** @param {number} maxEdge @param {number} quality @returns {string | null} */
  const encode = (maxEdge, quality) => {
    const scale = Math.min(1, maxEdge / Math.max(vw, vh));
    const w = Math.max(1, Math.round(vw * scale));
    const h = Math.max(1, Math.round(vh * scale));
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.drawImage(camVideo, 0, 0, w, h);
    return canvas.toDataURL("image/jpeg", quality);
  };

  if (activeTransport !== "webrtc") {
    return encode(SNAPSHOT_MAX_EDGE, SNAPSHOT_QUALITY);
  }
  let dataUrl = null;
  for (const [edge, quality] of SNAPSHOT_LADDER) {
    dataUrl = encode(edge, quality);
    if (!dataUrl) return null;
    if (dataUrl.length <= SNAPSHOT_DC_BUDGET_CHARS) return dataUrl;
  }
  // Even the last rung overflowed (shouldn't happen in practice) — send it
  // anyway; the client logs the failed send rather than killing the session.
  console.warn(`[tool] snapshot exceeds the data-channel budget after the full ladder (${dataUrl?.length} chars)`);
  return dataUrl;
}

/** Brief shutter flash on the preview so the user sees a snapshot was taken. */
function flashPreview() {
  camPip.classList.remove("flash");
  void camPip.offsetWidth; // reflow so the animation restarts
  camPip.classList.add("flash");
}

// ── Tool executor ─────────────────────────────────────────────────────────
// Runs the function the model called and returns the result. RealtimeSession
// owns function output ordering and the follow-up response. Errors come back as
// tool output too, so the model can recover gracefully instead of stalling.

/**
 * Run the function the model called. The Agents SDK preserves call order and
 * submits the returned value to the session.
 * @param {string} name @param {string} argsJson @param {string} callId
 * @returns {Promise<{ output: string, image?: string }>}
 */
async function runTool(name, argsJson, callId) {
  if (!client) return { output: "" };
  let args = /** @type {Record<string, unknown>} */ ({});
  try { args = JSON.parse(argsJson || "{}"); } catch { /* keep {} */ }

  if (DEBUG) console.debug(`[tool] run name=${name} callId=${JSON.stringify(callId)} args=${argsJson}`);
  if (!callId) console.warn("[tool] empty call_id — the backend didn't tag the call, can't return a function_call_output");

  /** @type {{ output: string, image?: string }} */
  let result = { output: "" };
  try {
    if (name === "web_search") {
      const query = typeof args.query === "string" ? args.query : "";
      result.output = await execWebSearch(query);
    } else if (name === "camera_snapshot") {
      const dataUrl = captureSnapshot();
      if (dataUrl) {
        if (DEBUG) console.debug(`[tool] camera_snapshot captured frame (${dataUrl.length} chars), sending image + output`);
        result = { output: "Snapshot captured from the webcam and attached as an image.", image: dataUrl };
        flashPreview();
      } else {
        console.warn("[tool] camera_snapshot: no frame — camera off or not ready");
        result.output = "The camera is not available right now.";
      }
    } else {
      result.output = `Unknown tool: ${name}`;
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    result.output = `Tool failed: ${msg}`;
  }
  return result;
}

/** @param {string} query @returns {Promise<string>} */
async function execWebSearch(query) {
  if (!query) return "No query provided.";
  /** @type {Record<string, string>} */
  const body = { query };
  // Only send a user key when there's no server key (server prefers its own).
  if (!serverSearchKey && userSearchKey) body.key = userSearchKey;

  const res = await fetch("api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = String(res.status);
    try { const j = await res.json(); if (j.detail) detail = j.detail; } catch {}
    throw new Error(`search error (${detail})`);
  }
  const json = await res.json();
  // Date-stamp the header so the model treats these as fresh realtime facts
  // rather than its (older) training knowledge.
  const today = new Date().toISOString().slice(0, 10);
  /** @type {string[]} */
  const lines = [`Google search result from ${today}:`];
  if (json.answer) lines.push(`Answer: ${json.answer}`);
  for (const r of json.results || []) {
    lines.push(`- ${r.title}: ${r.snippet} (${r.url})`);
  }
  return lines.length > 1 ? lines.join("\n") : `${lines[0]}\nNo results found.`;
}

/** Learn server config (search key + connection target), then refresh the UI. */
async function fetchConfig() {
  try {
    const res = await fetch("api/config");
    if (res.ok) {
      const json = await res.json();
      serverSearchKey = !!json.search;
      lbMode = !!json.lb;
      // Lock to LB mode only when the deploy reports a load balancer.
      allowDirect = json.allowDirect ?? !lbMode;
      // Deploy-pinned direct URL (overrides the LB server-side already).
      pinnedUrl = (json.s2sUrl || "").trim();
      // WebRTC transport: offered only when the deploy pins the URL (the
      // /api/calls proxy refuses to forward anywhere else).
      rtcAvailable = !!json.rtc;
      iceServers = Array.isArray(json.iceServers) ? json.iceServers : [];
      startupGreeting = typeof json.startupGreeting === "string"
        ? json.startupGreeting.trim()
        : "";
      loginRequired = !!json.requireLogin;
      // The conversation-time limiter rides on the LB being present.
      limiterOn = lbMode;
    }
    // Non-OK response: leave the fail-open default (allowDirect = true).
  } catch {
    // Config endpoint unreachable (e.g. static hosting): keep direct entry.
  }
  if (DEBUG) console.debug(`[ui] config: allowDirect=${allowDirect} lbMode=${lbMode}`);
  // Login chip + remaining-budget (no-op / hidden when the limiter is off).
  await account.refresh();
  if (currentState === "idle" && loginRequired && !account.loggedIn) {
    setCaption("Sign in to start");
  }
  syncToolsUi();
  syncConnectionUi();
}

/**
 * Resolve where to connect, per the deploy's mode:
 *   • LB mode  -> `{ sessionUrl }`, the client POSTs the same-origin /api/session
 *     proxy and the server forwards to the LB (its address stays server-side).
 *   • direct   -> `{ directUrl }`, connect straight to the s2s WebSocket.
 * Throws a user-facing error if direct mode is on but no URL was entered.
 * @returns {{ sessionUrl: string } | { directUrl: string }}
 */
function connectionTarget() {
  if (!allowDirect) {
    return { sessionUrl: "api/session" };
  }
  const directUrl = buildDirectWsUrl(pinnedUrl || settings.directUrl);
  if (!directUrl) {
    throw new Error("Enter a speech-to-speech server URL in Settings.");
  }
  return { directUrl };
}

/**
 * Normalise a user-typed server address into a realtime WebSocket URL.
 * Accepts bare hosts (`localhost:8080`), http(s) URLs, or ws(s) URLs, and adds
 * the `/v1/realtime` path when none is given. A full connect URL (with path
 * and/or query) is preserved as-is.
 * @param {string} raw @returns {string}
 */
function buildDirectWsUrl(raw) {
  let s = (raw || "").trim();
  if (!s) return "";
  if (!/^wss?:\/\//i.test(s)) {
    if (/^https?:\/\//i.test(s)) {
      s = s.replace(/^http/i, "ws"); // http→ws, https→wss
    } else {
      const isLocal = /^(localhost|127\.0\.0\.1|\[::1\])(:|\/|$)/i.test(s);
      s = (isLocal ? "ws://" : "wss://") + s;
    }
  }
  try {
    const u = new URL(s);
    if (u.pathname === "" || u.pathname === "/") u.pathname = "/v1/realtime";
    return u.toString();
  } catch {
    return s;
  }
}

/** Create + resume an AudioContext synchronously (must run inside the user
 *  gesture so iOS lets it start). Returns null if construction fails. */
function createResumedAudioContext() {
  try {
    const Ctx = window.AudioContext || /** @type {any} */ (window).webkitAudioContext;
    const ctx = new Ctx({ latencyHint: "interactive" });
    if (ctx.state === "suspended") void ctx.resume().catch(() => {});
    return /** @type {AudioContext} */ (ctx);
  } catch (err) {
    console.warn("[main] AudioContext init failed:", err);
    return null;
  }
}

/** Read the editable settings out of the form. The URL field is only honoured
 *  in free direct mode — in LB mode it's hidden, and when the deploy pins a
 *  URL it's read-only, so the user's saved URL survives either way. The
 *  transport select is only honoured while selectable, so a saved "webrtc"
 *  survives a visit to a deploy that can't offer it. */
function readSettingsFromForm() {
  return {
    directUrl: allowDirect && !pinnedUrl ? inputLbUrl.value.trim() : settings.directUrl,
    voice: inputVoice.value || DEFAULT_VOICE,
    instructions: inputInstructions.value.trim() || DEFAULT_INSTRUCTIONS,
    noiseGate: readGateThreshold(),
    transport: /** @type {"ws" | "webrtc"} */ (
      transportSelectable()
        ? (inputTransport.value === "webrtc" ? "webrtc" : "ws")
        : settings.transport
    ),
    audioInputId: inputAudioInput.value || "",
    audioOutputId: inputAudioOutput.value || "",
  };
}

/** Gate threshold (dBFS) currently shown on the slider, clamped to range. */
function readGateThreshold() {
  const v = Math.round(Number(inputNoiseGate.value));
  if (!Number.isFinite(v)) return GATE_OFF_DB;
  return Math.min(GATE_MAX_DB, Math.max(GATE_OFF_DB, v));
}

/** Whether the transport picker is live: env-pinned direct mode only. The
 *  /api/calls proxy forwards exclusively to SPEECH_TO_SPEECH_URL, so without
 *  the pin there is nowhere safe to send a WebRTC offer. */
function transportSelectable() {
  return allowDirect && !!pinnedUrl && rtcAvailable;
}

/** The transport the next conversation will actually use. */
function effectiveTransport() {
  return transportSelectable() && settings.transport === "webrtc" ? "webrtc" : "ws";
}

/** Reflect transport availability + selection into Settings, and hide the
 *  noise gate when WebRTC is picked (the gate lives in the WS capture
 *  worklet; the WebRTC mic path sends the raw track). */
function syncTransportUi() {
  // Hidden in LB mode (nothing to choose); visible-but-locked in un-pinned
  // direct mode so the option is discoverable along with what unlocks it.
  transportField.hidden = !allowDirect;
  const selectable = transportSelectable();
  inputTransport.disabled = !selectable;
  inputTransport.value = selectable && settings.transport === "webrtc" ? "webrtc" : "ws";
  transportHint.textContent = selectable
    ? "How audio travels to the server. Applies on the next conversation."
    : "WebRTC needs a server URL pinned by the deployment (SPEECH_TO_SPEECH_URL).";
  gateField.hidden = effectiveTransport() === "webrtc";
}

/** Adapt the connection field to the mode learned from /api/config. */
function syncConnectionUi() {
  syncTransportUi();
  if (pinnedUrl) {
    // Deploy-pinned URL: show it, but locked — the deployment owns it.
    connField.hidden = false;
    inputLbUrl.value = pinnedUrl;
    inputLbUrl.readOnly = true;
    connHint.classList.remove("error");
    connHint.textContent = "Speech-to-speech server URL pinned by this deployment.";
  } else if (allowDirect) {
    // Direct mode: the user sets their own s2s server URL.
    connField.hidden = false;
    inputLbUrl.value = settings.directUrl;
    inputLbUrl.readOnly = false;
    inputLbUrl.placeholder = "http://localhost:port";
    connHint.classList.remove("error");
    connHint.textContent =
      "URL of your speech-to-speech server, e.g. http://localhost:8080 (the app adds /v1/realtime).";
  } else {
    // LB mode: the load balancer URL is deployment-owned — hide it entirely so
    // its address is never exposed in Settings.
    connField.hidden = true;
  }
}

/** True when the user must supply a server URL before connecting (direct mode
 *  with nothing set). */
function missingServerUrl() {
  return allowDirect && !pinnedUrl && !buildDirectWsUrl(settings.directUrl);
}

/** Open Settings and point the user at the empty server-URL field. */
function promptServerUrl() {
  if (settingsModal.open) syncConnectionUi();
  else openSettings();
  connHint.textContent = "Set the speech-to-speech server URL to start.";
  connHint.classList.add("error");
  inputLbUrl.focus();
}

settingsForm.addEventListener("submit", (event) => {
  const submitter = /** @type {HTMLButtonElement | null} */ ((/** @type {SubmitEvent} */ (event)).submitter);
  if (submitter?.value !== "save") return;

  settings = readSettingsFromForm();
  saveSettings(settings);

  // Voice + instructions can apply to a live session without reconnecting; a
  // changed connection URL only takes effect on the next restart. Speaker
  // output can switch live when the browser supports AudioContext.setSinkId;
  // mic device changes need a Restart (new getUserMedia stream).
  if (client && LIVE_STATES.has(currentState)) {
    client.updateSession({ voice: settings.voice, instructions: settings.instructions });
    if (typeof client.setAudioOutputDevice === "function") {
      void client.setAudioOutputDevice(settings.audioOutputId);
    }
  }
});

// The noise gate applies live (worklet param), so tune it without a restart:
// update the label/marker, persist, and push straight to the running client.
inputNoiseGate.addEventListener("input", () => {
  setGateThreshold(readGateThreshold());
});

// ── 人物的话（连线时真正说给她听的那一整块）────────────────────────────────
// 这一页以前只认 localStorage 里那串通用话：人物面板写的台词存进了本机档案，
// 却永远进不了模型 —— 开口还是「你是一位友好、自然的中文语音助手」。现在开机问
// 一次 /api/character/prompt：服务端把台词、说话方式、边界、关系、原著资料、
// 长期记忆按预算装配成一整块话发回来，连线和换人都用它。四条不让步的规矩：
//   1. 你在框里亲手改过，就以你的为准，自动值永远不盖它。这件事只认一个显式
//      标志（敲进框里的那一次 input 才置位），绝不拿字符串比对去猜：上一轮
//      自动填进去的话和「你亲手写的」长得一模一样，猜必错；
//   2. 问不到（服务没起、没选人物、回包是空的）就完全退回今天的老样子：
//      框里是什么就用什么，并且把这件事用人话写在状态行里，不弹窗、不断线；
//   3. 换人、改台词之后只走现成的 updateSession({instructions}) 推给正在通话的
//      会话，绝不重连 WebSocket（重连会把用户正在说的半句丢掉）；
//   4. 长度、砍谁都不在这儿算 —— 那是服务端装配引擎一处的事，这里只搬字。
const INSTRUCTIONS_CUSTOM_KEY = "s2s.ws.instructionsCustom";

/** @type {{内容: string, 人物名称: string, 编号: string, 字数: number, 失败: string}} */
let characterWords = { 内容: "", 人物名称: "", 编号: "", 字数: 0, 失败: "" };
/** @type {HTMLElement | null} 状态行节点，开机插进提示词那一格下面 */
let wordsHint = null;

/** 框里的话是不是你亲手写的（是 → 自动值一律让路）。 */
function wordsAreManual() {
  return localStorage.getItem(INSTRUCTIONS_CUSTOM_KEY) === "1";
}

/** 「现在她说的是谁的话」那一行的措辞：只说人话，不提接口、不提内部名词。 */
function wordsStatusLine() {
  const inBox = (inputInstructions.value || "").trim();
  if (wordsAreManual()) {
    const giveWay = characterWords.人物名称 ? `${characterWords.人物名称}的那一套先给你让开。` : "";
    return `现在她照你亲手写的这套话说，一共 ${inBox.length} 个字。${giveWay}`;
  }
  if (characterWords.内容) {
    return `现在她说的是「${characterWords.人物名称}」的话，一共 ${characterWords.字数} 个字。`;
  }
  const why = characterWords.失败 || "这台机器上还没读到这个人物的话";
  return `${why}，现在用的是框里这几句通用话，一共 ${inBox.length} 个字。`;
}

function renderWordsStatus() {
  if (wordsHint) wordsHint.textContent = wordsStatusLine();
}

/** 状态行 + 「用回人物的话」那颗按钮。页面里本来没有这两样，就在这儿自己插进
 *  提示词那一格后面：index.html 别处也在改，不占它。 */
function mountWordsRow() {
  const anchor = inputInstructions.closest(".field") || inputInstructions.parentElement;
  if (!anchor || !anchor.parentElement) return;
  const row = document.createElement("div");
  row.className = "field";
  const line = document.createElement("small");
  line.id = "character-words-hint";
  const back = document.createElement("button");
  back.type = "button";
  back.className = "btn";
  back.id = "character-words-back";
  back.textContent = "用回人物的话";
  // 一键回到「人物的话说了算」：先撤掉亲手改过这个标志，再去问一次最新装配的
  // 那一块（台词可能刚改过，拿旧的填回去等于没换）。
  back.addEventListener("click", () => {
    localStorage.removeItem(INSTRUCTIONS_CUSTOM_KEY);
    void loadCharacterWords({ pushLive: true });
  });
  row.append(line, back);
  anchor.after(row);
  wordsHint = line;
}

/** 把人物的话填进提示词框并落到设置里（你亲手改过就一个字都不动），
 *  正在通话时顺手推给这一场。 */
function useCharacterWords({ pushLive = false } = {}) {
  if (!wordsAreManual() && characterWords.内容) {
    settings.instructions = characterWords.内容;
    if (inputInstructions.value !== characterWords.内容) {
      inputInstructions.value = characterWords.内容;
    }
    saveSettings(settings);
  }
  renderWordsStatus();
  if (pushLive && !wordsAreManual() && characterWords.内容
    && client && LIVE_STATES.has(currentState)) {
    client.updateSession({ instructions: characterWords.内容 });
  }
}

/** 去本机问一次「现在这个人该怎么说话」。问不到只是状态行换一句话。 */
async function loadCharacterWords({ pushLive = false } = {}) {
  const instructionIdentity = readingInstructions.capture();
  try {
    const res = await fetch("/api/character/prompt", { cache: "no-store" });
    const data = res.ok ? await res.json().catch(() => null) : null;
    const text = String((data && data.内容) || "").trim();
    if (!readingInstructions.unchanged(instructionIdentity)) return characterWords;
    if (data && data.ok && text) {
      characterWords = {
        内容: text,
        人物名称: String(data.人物名称 || "").trim() || "当前人物",
        编号: String(data.编号 || ""),
        字数: Number(data.字数) || text.length,
        失败: "",
      };
    } else {
      characterWords = {
        内容: "", 人物名称: "", 编号: "", 字数: 0,
        失败: String((data && data.说明) || "这台机器上还没读到这个人物的话"),
      };
    }
  } catch {
    if (!readingInstructions.unchanged(instructionIdentity)) return characterWords;
    // 接口不通（比如这台机器上人物服务没起来）：什么都不改，退回老样子。
    characterWords = { 内容: "", 人物名称: "", 编号: "", 字数: 0,
                       失败: "这台机器上的人物服务暂时联系不上" };
  }
  useCharacterWords({ pushLive });
  // V3_WORKSPACE：顶栏人物名按钮跟随真实人物数据（persona 管理入口）。
  const personaName = document.getElementById("gf-persona-name");
  if (personaName) {
    personaName.textContent = characterWords.人物名称 || "小夜";
  }
  return characterWords;
}

/** 最近说过的那几句，只活在页面内存里 —— 本层要看的「最近几句」从这里取。
 *  判断结果不落在这里，也不写设置、不写 localStorage：它只对当轮有效。 */
const RECENT_TURN_ROWS = 8;
const recentTurns = [];
let dynamicEnabled = false;
let dynamicRevision = 0;
let dynamicTurnActive = false;
let recentCharacterId = "";
const dynamicButton = document.getElementById('dynamic-chat-toggle');
function renderDynamic() {
  dynamicButton.textContent = `动态互动：${dynamicEnabled ? '开启' : '关闭'}`;
  dynamicButton.setAttribute('aria-pressed', String(dynamicEnabled));
}
async function loadDynamic() {
  try {
    const r = await fetch('/api/interaction/dynamic', {cache:'no-store'});
    if (!r.ok) throw new Error('unavailable');
    dynamicEnabled = (await r.json()).enabled === true;
    dynamicButton.disabled = false; renderDynamic();
  } catch { document.getElementById('dynamic-chat-note').textContent = '动态互动暂不可用，聊天照常'; }
}
dynamicButton.addEventListener('click', async () => {
  dynamicButton.disabled = true; dynamicRevision++;
  try {
    const r = await fetch('/api/interaction/dynamic', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!dynamicEnabled})});
    if (!r.ok) throw new Error('save failed');
    dynamicEnabled = (await r.json()).enabled === true;
    if (!dynamicEnabled) { await applyTurnInstructions(''); dynamicTurnActive = false; }
    renderDynamic();
  } catch { document.getElementById('dynamic-chat-note').textContent = '保存未成功，请重试'; }
  finally { dynamicButton.disabled = false; }
});
void loadDynamic();

function rememberTurnForInteraction(detail) {
  if (!detail || detail.partial) return;
  const text = String(detail.text || "").trim();
  if (!text) return;
  recentTurns.push(`${detail.role === "user" ? "user" : "assistant"}:${text}`);
  while (recentTurns.length > RECENT_TURN_ROWS) recentTurns.shift();
}

/** 这一句要开口了：先问一次「照这一句，她该怎么接」，把拿到的那一块换进这一场。
 *
 * 只借两条现成的路：装配还是 :7860 那一次 build（真人互动层在里面决定这一轮要不要
 * 多问判断器一次），下发还是这个客户端本来就有的 session.update —— 连线后换人物
 * 走的就是它。这里不写 settings、不写 localStorage，所以「用回人物的话」、刷新页面
 * 这些老动作一律照旧，那一行判断也不会活到下一场。
 *
 * 问不到、接口挂了、或者这一轮没出判断：一律把人物那块放回去 —— 上一轮那一行判断
 * 绝不留到这一轮，这句话照常发出去，也就是原样走回那条老回复路。
 */
let turnInstructions = "";

async function applyTurnInstructions(next, { reading = false } = {}) {
  const standing = String(characterWords.内容 || "").trim();
  const target = String(next || "").trim() || standing;
  if (!target || (!reading && target === turnInstructions)) return false;
  const update = client?.updateSession({ instructions: target });
  if (reading && !(await update)) return false;
  turnInstructions = target;
  return true;
}

async function refreshWordsForTurn(text) {
  if (!client || !LIVE_STATES.has(currentState)) return;
  const identity = readingInstructions.capture();
  const reading = identity.mode === 'READING';
  if (reading && modeSwitching) throw new Error('STALE_INSTRUCTION_UPDATE');
  if (!reading && wordsAreManual()) return;     // 普通聊天沿用手写提示词
  const body = String(text || "").trim();
  if (!body) return;
  const params = new URLSearchParams({ turn: body });
  if (recentTurns.length) params.set("history", recentTurns.join("\n"));
  if (characterWords.编号) params.set("character_id", characterWords.编号);
  if (reading) {
    if (!identity.documentId) throw new Error('PROMPT_BUILD_FAILED');
    await readingInstructions.deliver(identity, async () => {
      const verifyDocument = async () => {
        const status = await fetch('/api/reading/status', { cache: 'no-store' });
        const data = status.ok ? await status.json() : null;
        if (data?.reading_state?.document_id !== identity.documentId) {
          throw new Error('STALE_INSTRUCTION_UPDATE');
        }
        readingInstructions.check(identity);
      };
      await verifyDocument();
      const res = await fetch(`/api/character/prompt?${params.toString()}`, { cache: 'no-store' });
      const data = res.ok ? await res.json() : null;
      await verifyDocument();
      return { ok: data?.ok, mode: data?.模式, text: data?.内容 };
    }, (next) => applyTurnInstructions(next, { reading: true }));
    return;
  }
  let next = "";
  try {
    const revision = dynamicRevision;
    const characterId = characterWords.编号;
    if (recentCharacterId !== characterId) { recentTurns.length = 0; recentCharacterId = characterId; }
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), 7000);
    let res;
    try {
      res = dynamicEnabled
        ? await fetch('/api/interaction/turn', {method:'POST',headers:{'Content-Type':'application/json'},signal:controller.signal,
            body:JSON.stringify({text:body, character_id:characterId,history:recentTurns.join('\n')})})
        : await fetch(`/api/character/prompt?${params.toString()}`, {cache:'no-store',signal:controller.signal});
    } finally { clearTimeout(deadline); }
    const data = res.ok ? await res.json().catch(() => null) : null;
    if (revision !== dynamicRevision || characterId !== characterWords.编号) throw new Error('STALE_DYNAMIC_TURN');
    next = String((data && data.ok && data.内容) || "");
    dynamicTurnActive = data?.dynamic?.used === true;
    if (data?.dynamic) window.dispatchEvent(new CustomEvent('dynamic-interaction-result',{detail:data.dynamic}));
  } catch (error) {
    if (error.message === "STALE_DYNAMIC_TURN") throw error;
    dynamicTurnActive = false;
  }
  if (!readingInstructions.unchanged(identity)) throw new Error('STALE_INSTRUCTION_UPDATE');
  applyTurnInstructions(next);
}

/** 你自己动手敲了框 —— 从现在起你的话说了算。 */
inputInstructions.addEventListener("input", () => {
  localStorage.setItem(INSTRUCTIONS_CUSTOM_KEY, "1");
  renderWordsStatus();
});

// ---------------------------------------------------------------------------
// 模式（AI_GIRLFRIEND_MODE_SYSTEM_V1）
//
// 这一块只做三件事，别的一概不管：
//   1. 显示：三个按钮的形状与当前模式**只从后端 /api/mode 来**。页面不存「现在是
//      什么模式」——localStorage 只记一个「上次选了哪一项」的提示，刷新后拿它跟
//      后端对一次，对不上就听后端的。这样 UI 显示 = 后端当前模式 = 装配用的模式，
//      不可能出现「页面显示角色模式、实际后端还在朗读」。
//   2. 请求：切换只发 POST /api/mode。后端把新装配好的提示词一并回在这里，于是
//      「切完立刻 updateSession」是一次请求里完成的事 —— 不靠刷新页面、不靠重启
//      8765、不靠调用方记得再去问一次。
//   3. 安全：从朗读切走之前先把朗读音频停掉、断点存好、自动续读作废；
//      切到朗读之前先把角色这一路不该带的东西清掉（那是后端按模式装配的，前端
//      只负责别让旧声音继续响）。
// 第三模式那一颗恒 disabled：它没实现，点了不进半成品。
// ---------------------------------------------------------------------------

const MODE_ROW = /** @type {HTMLElement|null} */ (document.getElementById("gf-mode-row"));
const MODE_STATUS = /** @type {HTMLElement|null} */ (document.getElementById("gf-mode-status"));
const MODE_HINT_KEY = "s2s.mode.lastChoice";

/** @type {{mode:string, rows:Array<{id:string,label:string,selectable:boolean,hint:string,active:boolean}>}} */
let modeState = { mode: "ROLEPLAY", rows: [] };
let modeSwitching = false;
const readingInstructions = new ReadingInstructionGuard(() => ({
  mode: modeState.mode, client, liveSession: client?._session, ...readingInstructionContext(),
}));

let renderedBusinessMode = null;
function renderMode() {
  // 挂在 window 上给别的模块读（live_interaction.js 据此在朗读模式下不去抢话）。
  // 这是**广播**，不是第二份事实源：能写的仍然只有 /api/mode 那一处。
  window.__aiGirlfriendMode = modeState.mode;
  if (!MODE_ROW) return;
  const buttons = /** @type {NodeListOf<HTMLButtonElement>} */ (
    MODE_ROW.querySelectorAll("button[data-mode]"));
  buttons.forEach((btn) => {
    const id = String(btn.dataset.mode || "");
    const row = modeState.rows.find((one) => one.id === id);
    const selectable = Boolean(row && row.selectable);
    const active = id === modeState.mode;
    btn.disabled = !selectable || modeSwitching;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
    btn.textContent = selectable ? String(row.label || id) : `${String(row?.label || id)}（待开发）`;
  });
  const active = modeState.rows.find((one) => one.id === modeState.mode);
  if (MODE_STATUS) {
    MODE_STATUS.textContent = modeSwitching
      ? "正在切换模式…"
      : `现在在「${String(active?.label || modeState.mode)}」。`;
  }
  // V3_WORKSPACE：右侧工作区按模式切换视图。互动聊天=ROLEPLAY 视图，
  // 陪读朗读=READING 视图；互斥显示，hidden 由这里唯一驱动。
  const chatView = document.getElementById("gf-view-chat");
  const readingView = document.getElementById("gf-view-reading");
  if (chatView) chatView.hidden = modeState.mode !== "ROLEPLAY";
  if (readingView) readingView.hidden = modeState.mode !== "READING";
  // 切到朗读时刷新书库与断点（reading.js 挂的钩子；未挂就跳过）。
  // renderedBusinessMode 只防同模式的重复刷新，不是第二份事实源。
  if (modeState.mode === "READING" && !modeSwitching && renderedBusinessMode !== "READING") {
    window.__gfReadingRefresh?.();
  }
  if (!modeSwitching) renderedBusinessMode = modeState.mode;
}

/** 把刚拿到的那一整块提示词推给正在通话的这一场（换人走的就是这条路）。 */
async function pushInstructionsLive(text, { force = false } = {}) {
  const body = String(text || "").trim();
  if (!body || !client || !LIVE_STATES.has(currentState)) return false;
  if (!force && wordsAreManual()) return false;   // 你亲手改过框里的话：以你的为准
  characterWords = { 内容: body, 人物名称: characterWords.人物名称,
                     编号: characterWords.编号, 字数: body.length, 失败: "" };
  if (inputInstructions.value !== body) inputInstructions.value = body;
  settings.instructions = body;
  saveSettings(settings);
  turnInstructions = "";
  const applied = client.updateSession({ instructions: body });
  if (modeState.mode === 'READING' && !(await applied)) return false;
  renderWordsStatus();
  return true;
}

async function loadMode({ pushLive = false } = {}) {
  try {
    const res = await fetch("/api/mode", { cache: "no-store" });
    const data = res.ok ? await res.json().catch(() => null) : null;
    if (data && data.ok) {
      modeState = { mode: String(data.模式 || "ROLEPLAY"),
                    rows: Array.isArray(data.模式列表) ? data.模式列表 : [] };
    } else {
      modeState = { mode: modeState.mode, rows: modeState.rows };
    }
  } catch {
    // 问不到就保持现状，不猜：页面上一次显示什么还显示什么，绝不假装切换成功。
  }
  renderMode();
  if (pushLive) await loadCharacterWords({ pushLive: true });
  return modeState;
}

async function switchMode(target) {
  if (modeSwitching) return;
  const id = String(target || "").trim().toUpperCase();
  if (!id || id === modeState.mode) return;
  const row = modeState.rows.find((one) => one.id === id);
  if (row && !row.selectable) {
    // 第三模式：后端也会拒，这里连请求都不发（不发就没有半途而废的状态）。
    if (MODE_STATUS) MODE_STATUS.textContent = `「${row.label}」还没做，先放着。`;
    return;
  }
  modeSwitching = true;
  readingInstructions.invalidate();
  renderMode();
  try {
    // 离开朗读：先把声音停掉、断点存好、自动续读作废，再切模式 ——
    // 不许出现「故事还在后台念、角色又开始讲话」。
    if (modeState.mode === "READING" && id !== "READING") await stopForModeSwitch();
    const res = await fetch("/api/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: id }),
    });
    const data = res.ok ? await res.json().catch(() => null) : null;
    if (!data || !data.ok) {
      if (MODE_STATUS) {
        MODE_STATUS.textContent = String((data && data.说明) || "这次没切成，还是原来那个模式。");
      }
      await loadMode();
      return;
    }
    modeState.mode = String(data.模式 || id);
    try { localStorage.setItem(MODE_HINT_KEY, modeState.mode); } catch { /* 无痕模式 */ }
    // 后端在这一次回包里就把新模式的提示词装配好了，直接推给当前这场：
    // 不刷新页面、不重启 8765、不等下一次连线。
    const pushed = await pushInstructionsLive(String(data.提示词 || ""), { force: true });
    if (MODE_STATUS) {
      MODE_STATUS.textContent = pushed
        ? `已经切到「${describeModeLabel(modeState.mode)}」，这一场立刻照新的来。`
        : `已经切到「${describeModeLabel(modeState.mode)}」，下次开口就是新的。`;
    }
    await loadMode();
  } catch {
    if (MODE_STATUS) MODE_STATUS.textContent = "这次没切成，还是原来那个模式。";
    await loadMode();
  } finally {
    modeSwitching = false;
    renderMode();
  }
}

function describeModeLabel(id) {
  const row = modeState.rows.find((one) => one.id === id);
  return String((row && row.label) || id);
}

if (MODE_ROW) {
  MODE_ROW.addEventListener("click", (event) => {
    const btn = /** @type {HTMLElement|null} */ (
      /** @type {HTMLElement} */ (event.target).closest?.("button[data-mode]") || null);
    if (!btn) return;
    void switchMode(btn.dataset.mode);
  });
}

// V3_WORKSPACE：给 reading.js 用——陪读视图的「关闭」按钮 = 切回互动聊天。
window.__gfRequestMode = (target) => { void switchMode(target); };

// V3_WORKSPACE：设置弹层六分组标签页。只切换 hidden，不动任何控件。
const SETTINGS_TABS = document.getElementById("settings-tabs");
if (SETTINGS_TABS) {
  SETTINGS_TABS.addEventListener("click", (event) => {
    const tab = /** @type {HTMLElement|null} */ (
      /** @type {HTMLElement} */ (event.target).closest?.("button[data-tab]") || null);
    if (!tab) return;
    const target = String(tab.dataset.tab || "");
    SETTINGS_TABS.querySelectorAll("button[data-tab]").forEach((b) => {
      b.classList.toggle("is-active", b === tab);
    });
    const panels = /** @type {HTMLElement|null} */ (
      document.querySelector("#settings-modal .tab-panels"));
    panels?.querySelectorAll(".tab-panel").forEach((p) => {
      p.classList.toggle("active", p instanceof HTMLElement && p.dataset.panel === target);
    });
  });
}

// 换人、改台词这些请求都是人物面板自己发的（那个模块不动），所以在最外面
// 套一层「听见就算一次」：见到这些回执成功，就去问一次新装配的那一块，
// 正在通话就直接换口吻，不重连。
const PERSONA_TOUCHING_PATH = /\/api\/character\/(select|persona|create|edit|delete|import)(\?|$)/;
const plainFetch = window.fetch.bind(window);
window.fetch = (input, init) => {
  const promise = plainFetch(input, init);
  try {
    const url = typeof input === "string" ? input : ((input && input.url) || "");
    const method = String((init && init.method) || (input && input.method) || "GET").toUpperCase();
    if (method === "POST" && PERSONA_TOUCHING_PATH.test(url)) {
      promise.then((res) => {
        if (res && res.ok) void loadCharacterWords({ pushLive: true });
      }).catch(() => {});
    }
  } catch { /* 这层观察出错也不能影响真正的请求 */ }
  return promise;
};

/** 连线前把「用谁的话」定下来：你改过就用框里的，否则以人物的话为准；
 *  开机那一次没问到的（服务当时没起），这里补问一次。 */
async function settleWordsForSession() {
  if (wordsAreManual()) {
    renderWordsStatus();
    return;
  }
  if (characterWords.内容) useCharacterWords();
  else await loadCharacterWords();
}

// Transport persists on change (like the gate) and takes effect on the next
// conversation; the gate field previews its WS-only availability right away.
inputTransport.addEventListener("change", () => {
  if (!transportSelectable()) return;
  settings.transport = inputTransport.value === "webrtc" ? "webrtc" : "ws";
  localStorage.setItem(STORAGE_KEYS.transport, settings.transport);
  syncTransportUi();
});

restartBtn.addEventListener("click", async () => {
  if (currentState === "connecting") return; // a connect is already underway
  if (loginRequired && !account.loggedIn) {
    account.showLoginRequired();
    return;
  }
  settings = readSettingsFromForm();
  saveSettings(settings);
  if (missingServerUrl()) { promptServerUrl(); return; } // keep settings open
  settingsModal.close();
  // Grab the AudioContext NOW, inside the click gesture — teardown() awaits, and
  // creating it afterwards would fall outside the gesture (silent on iOS).
  const audioContext = createResumedAudioContext();
  try {
    if (client) await teardown();
    await doStart(audioContext);
  } catch (err) {
    await handleStartError(err);
  }
});

circleBtn.addEventListener("click", async () => {
  try {
    if (currentState === "idle" || currentState === "error") {
      if (loginRequired && !account.loggedIn) {
        account.showLoginRequired();
        return;
      }
      if (missingServerUrl()) { promptServerUrl(); return; }
      await doStart();
    }
  } catch (err) {
    await handleStartError(err);
  }
});

/** A failed start is either the daily limit (show the modal, return to idle) or
 *  a real fault (surface it). doStart already closed any orphan AudioContext.
 *  @param {any} err */
async function handleStartError(err) {
  if (err && err.code === "login-required") {
    await teardown();
    setState("error");
    setCaption("Sign in again to continue.", "error");
    account.showLoginRequired(err.loginUrl);
    return;
  }
  if (err && err.code === "limit") {
    await teardown();
    account.showLimit(err.tier);
    return;
  }
  // The user left the queue (close() aborted the wait): teardown already reset
  // the UI to idle, so there's nothing to report.
  if (err && err.code === "aborted") return;
  // The whole waiting line is full: a warm, reassuring modal rather than an error.
  if (err && err.code === "queue-full") {
    await teardown();
    account.showBusy();
    return;
  }
  // Our place lapsed (ticket reaped, or the join window ran out). Recoverable, not
  // a fault: land on the retry state with a kind, plain-language reason.
  if (err && (err.code === "queue-expired" || err.code === "join-expired")) {
    await teardown();
    setState("error");
    setCaption(
      err.code === "join-expired"
        ? "Your spot expired. Tap to rejoin."
        : "That took a while. Tap to rejoin.",
      "error",
    );
    return;
  }
  await onFatalError(err);
}

micBtn.addEventListener("click", () => {
  if (!micStream || !client) return;
  micMuted = !micMuted;
  syncMicMuteState();
  micBtn.classList.toggle("muted", micMuted);
  micBtn.setAttribute("aria-label", micMuted ? "Unmute" : "Mute");
  micBtn.title = micMuted ? "Unmute" : "Mute";
});

stopBtn.addEventListener("click", async () => {
  await teardown();
});

// "Leave queue": tear down the pending connect (aborts the poll wait) and drop
// our place in line. Same teardown path as stopping a live call.
leaveQueueBtn.addEventListener("click", async () => {
  await teardown();
});

// "Join now": accept the held slot. The click is a user gesture, so the client
// re-resumes the AudioContext here (iOS) before dialing.
joinQueueBtn.addEventListener("click", () => {
  stopJoinCountdown();
  if (client) client.join();
});

const MIC_CONSTRAINTS_BASE = {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
};

/** @returns {MediaStreamConstraints} */
function micConstraints() {
  /** @type {MediaTrackConstraints} */
  const audio = { ...MIC_CONSTRAINTS_BASE };
  // Diagnostic A/B only: compare USB mic PCM before/after browser DSP without
  // changing normal capture or any server-side VAD/STT setting.
  if (new URLSearchParams(location.search).get("micdiag") === "raw") {
    audio.echoCancellation = false;
    audio.noiseSuppression = false;
    audio.autoGainControl = false;
  }
  if (settings.audioInputId) {
    // ideal (not exact): if the saved device was unplugged, fall back quietly.
    audio.deviceId = { ideal: settings.audioInputId };
  }
  return { audio };
}

/** True when Web Audio can route playback to a chosen output device. */
function supportsAudioOutputSelection() {
  const Ctx = window.AudioContext || /** @type {any} */ (window).webkitAudioContext;
  return typeof Ctx?.prototype?.setSinkId === "function";
}

/**
 * Rebuild the mic/speaker <select>s from enumerateDevices. Labels are blank
 * until mic permission has been granted at least once.
 */
async function refreshAudioDeviceLists() {
  const canPickOutput = supportsAudioOutputSelection();
  inputAudioOutput.disabled = !canPickOutput;
  audioOutputHint.textContent = canPickOutput
    ? "Where assistant audio plays. Can change live while connected."
    : "Speaker selection needs a browser with AudioContext.setSinkId (Chrome/Edge).";

  /** @type {MediaDeviceInfo[]} */
  let devices = [];
  try {
    devices = await navigator.mediaDevices.enumerateDevices();
  } catch (err) {
    console.warn("[main] enumerateDevices failed:", err);
  }

  const inputs = devices.filter((d) => d.kind === "audioinput");
  const outputs = devices.filter((d) => d.kind === "audiooutput");
  const labelsReady = devices.some((d) => d.label);

  fillDeviceSelect(inputAudioInput, inputs, settings.audioInputId, "Microphone");
  fillDeviceSelect(inputAudioOutput, outputs, settings.audioOutputId, "Speaker");

  if (!labelsReady) {
    // Permission unlocks real device names; keep it quiet — user can tap Start
    // or we unlock when they already connected once this session.
    const hint = inputAudioInput.parentElement?.querySelector("small");
    if (hint) {
      hint.textContent =
        "Allow microphone access (tap Start once) to see device names. Mic changes apply on Restart.";
    }
  } else {
    const hint = inputAudioInput.parentElement?.querySelector("small");
    if (hint) hint.textContent = "Applies on the next conversation (or Restart).";
  }
}

/**
 * @param {HTMLSelectElement} select
 * @param {MediaDeviceInfo[]} devices
 * @param {string} selectedId
 * @param {string} fallbackLabel
 */
function fillDeviceSelect(select, devices, selectedId, fallbackLabel) {
  const prev = selectedId || select.value || "";
  select.replaceChildren();
  const def = document.createElement("option");
  def.value = "";
  def.textContent = "System default";
  select.appendChild(def);
  devices.forEach((d, i) => {
    const opt = document.createElement("option");
    opt.value = d.deviceId;
    opt.textContent = d.label || `${fallbackLabel} ${i + 1}`;
    select.appendChild(opt);
  });
  // Keep a saved id even if it isn't currently listed (unplugged); browser
  // will fall back via ideal constraints / setSinkId errors.
  if (prev && ![...select.options].some((o) => o.value === prev)) {
    const missing = document.createElement("option");
    missing.value = prev;
    missing.textContent = `${fallbackLabel} (saved, not found)`;
    select.appendChild(missing);
  }
  select.value = prev;
  if (select.value !== prev) select.value = "";
}

if (navigator.mediaDevices?.addEventListener) {
  navigator.mediaDevices.addEventListener("devicechange", () => {
    if (settingsModal.open) void refreshAudioDeviceLists();
  });
}

/** Prompt for mic permission up front, then immediately release the tracks so no
 *  recording indicator lingers during a queue wait. Throws a friendly error if the
 *  user denies. */
async function primeMicPermission() {
  try {
    const s = await navigator.mediaDevices.getUserMedia(micConstraints());
    for (const track of s.getTracks()) track.stop();
  } catch (err) {
    throw new Error(
      `Microphone access denied${err instanceof Error ? `: ${err.message}` : ""}`,
    );
  }
}

/** Acquire the live capture stream once a slot is granted. Permission was primed
 *  in the tap gesture, so this is silent. Stored module-side for mute + teardown. */
async function acquireMicStream() {
  micStream = await navigator.mediaDevices.getUserMedia(micConstraints());
  return micStream;
}

/** 打字也能开一场对话：管线需要一个活动音轨来挂采集 worklet，但文字模式不许
 *  求麦克风权限 —— 用 AudioContext 生成的静音流顶替，服务端 VAD 对它永远不触发。 */
async function acquireSilentStream() {
  const ctx = new AudioContext();
  micStream = ctx.createMediaStreamDestination().stream;
  return micStream;
}

/**
 * 文字输入框在"还没连上"时点发送的入口。复用 orb 的整条启动路径，只是把
 * 麦克风换成静音流。
 * @returns {Promise<boolean>} true 表示已经可以给服务端发消息。
 */
async function startTextConversation() {
  if (client && LIVE_STATES.has(currentState)) return true;
  // 重连中不能再开一路：那会造出第二个 client，把旧一代的回调和新一代混在
  // 一起播（正是"上一通的回复漏进这一通"的成因）。让已在跑的恢复循环说完。
  if (client && currentState === "reconnecting") return false;
  if (currentState === "connecting" || currentState === "queued") return false;
  if (missingServerUrl()) { promptServerUrl(); return false; }
  textOnlyMode = true;
  try {
    await doStart(createResumedAudioContext());
    return Boolean(client);
  } catch (err) {
    await handleStartError(err);
    return false;
  } finally {
    if (!client) textOnlyMode = false;
  }
}

/** @param {number} position Update the queued caption ("You're #N in line"). */
function onQueuePosition(position) {
  const n = Number(position) || 0;
  setCaption(n > 0 ? `You're #${n} in line` : "Finding you a spot…", "muted");
}

// ── "Your turn" join countdown ──────────────────────────────────────────────
// While a slot is held for us, show how long is left to accept it. The client's
// join gate expires just before the load balancer reclaims the slot.
let joinCountdownTimer = 0;

/** @param {number} sec */
function startJoinCountdown(sec) {
  stopJoinCountdown();
  let left = Math.max(0, Math.floor(sec));
  const paint = () => {
    joinQueueBtn.textContent = left > 0 ? `Join now (${left}s)` : "Join now";
  };
  paint();
  joinCountdownTimer = window.setInterval(() => {
    left -= 1;
    if (left <= 0) {
      stopJoinCountdown();
      joinQueueBtn.textContent = "Join now";
      return;
    }
    paint();
  }, 1000);
}

function stopJoinCountdown() {
  if (joinCountdownTimer) {
    clearInterval(joinCountdownTimer);
    joinCountdownTimer = 0;
  }
}

/**
 * Start a conversation. Pass a pre-created AudioContext when the caller already
 * made one inside the tap/click gesture (required on iOS); otherwise one is
 * created here, which is still inside the gesture for a direct orb tap.
 * @param {AudioContext | null} [audioContext]
 */
/** Wait briefly for the avatar WebRTC media (bounded); never blocks the call. */
async function waitForAvatarMedia() {
  const avatar = window.AvatarSync;
  if (!avatar || avatar.suspended || avatar.mediaReady() ||
      globalThis.s2sAudioRouter?.diagnostics().policy.avatar_enabled === false) return;
  const started = Date.now();
  void avatar.connect().catch(error => console.warn('[avatar startup]', error));
  await new Promise(resolve => {
    const check = () => {
      if (avatar.mediaReady() || avatar.suspended || Date.now() - started >= 8000) { resolve(); return; }
      setTimeout(check, 100);
    };
    check();
  });
}

async function doStart(audioContext = null) {
  const transport = effectiveTransport();
  // Give the avatar WebRTC media a head start so the first reply is audible
  // through the digital human instead of falling back to local playback.
  await waitForAvatarMedia();
  // Resolve the target before touching mic/audio so a misconfiguration (e.g.
  // direct mode with no URL) fails fast with a clear message. Over WebRTC the
  // browser never dials the s2s server itself — the offer goes to the
  // same-origin /api/calls proxy — so there is no target to resolve.
  const target = transport === "webrtc" ? null : connectionTarget();
  activeTransport = transport;
  // The radial gate arc (threshold handle around the mic button) is a WS
  // feature; over WebRTC only the mute button remains.
  document.body.classList.toggle("rtc-live", transport === "webrtc");

  chat.clear();
  chat.reset();
  setState("connecting");
  setCaption(textOnlyMode ? "Connecting…" : "Asking for mic…", "muted");

  // Create + resume the AudioContext SYNCHRONOUSLY, still inside the gesture.
  // iOS Safari only starts an AudioContext from a user gesture; if we waited
  // until after the getUserMedia / session-creation awaits below, it would stay
  // suspended and the whole pipeline would be silent.
  if (!audioContext) audioContext = createResumedAudioContext();

  // Prime the mic permission now (get the prompt out of the way up front), then
  // release it. The real capture stream is acquired only once a slot is granted
  // (see acquireMicStream), so the mic 'in use' indicator never lights while we
  // sit in the queue. Permission persists, so the later acquire is silent.
  // 文字模式跳过：那条路径不碰麦克风。
  if (!textOnlyMode) {
    try {
      await primeMicPermission();
    } catch (err) {
      if (audioContext) void audioContext.close().catch(() => {});
      throw err;
    }
  }

  // The webcam is started on arrival (autoStartCamera), so nothing to do here;
  // a still-pending grant just means the snapshot tool isn't ready yet.

  // 连线前定稿这一次用谁的话：必须排在读 settings 之前，否则这一次开口
  // 用的还是上一次留在框里的那一套。
  await settleWordsForSession();

  const common = {
    voice: settings.voice,
    manualVoiceSelection: manualSelection(),
    instructions: settings.instructions,
    startupGreeting,
    acquireMic: textOnlyMode ? acquireSilentStream : acquireMicStream,
    tools: activeToolDefs(),
    audioOutputId: settings.audioOutputId || "",
    executeTool: async ({ name, arguments: args, callId }) => {
      chat.onToolCall(name);
      const result = await runTool(name, args, callId);
      if (client === c) chat.onToolResult(name, args, result.output, result.image);
      return result;
    },
    ...(audioContext ? { audioContext } : {}),
    // 夜间无人值守：宁可多试一会儿再报告失败。上限/退避都在客户端里，
    // 这里只声明本机部署的预算（与厂商、模型、TTS 无关）。
    resilience: { maxAttempts: 60, maxDelayMs: 10_000, heartbeatMs: 5_000, missLimit: 3 },
  };
  const c = target === null
    ? new S2sRealtimeClient({
        transport: "webrtc",
        callsUrl: "api/calls",
        iceServers,
        ...common,
      })
    : new S2sRealtimeClient({
        transport: "websocket",
        ...target,
        noiseGate: gateParams(settings.noiseGate),
        ...common,
      });
  client = c;
  if (new URLSearchParams(location.search).has("micdiag")) {
    // Read-only, opt-in microphone trace. The WAV is pre-gate and never enters
    // the realtime path; it stays in this tab until explicitly downloaded.
    window.__micDiag = {
      snapshot: () => c.micDiagnostics(),
      startCapture: () => c.startMicDiagnosticCapture(),
      downloadRawWav: () => {
        const wav = c.micDiagnosticWav();
        if (!wav) return false;
        const url = URL.createObjectURL(wav);
        const link = document.createElement("a");
        link.href = url;
        link.download = `mic-raw-${Date.now()}.wav`;
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 10_000);
        return true;
      },
    };
  }
  c.setMuted(micMuted || userAudioReplaying);

  c.addEventListener("queue", (e) => {
    const { position, queueId } = /** @type {CustomEvent<{ position: number; queueId: string }>} */ (e).detail;
    if (queueId) queuedTicketId = queueId;
    onQueuePosition(position);
  });

  c.addEventListener("ready-to-join", (e) => {
    const { info, expiresSec } = /** @type {CustomEvent<{ info: import("./s2s-realtime-client.js").SessionInfo; expiresSec: number }>} */ (e).detail;
    // A slot is held for us. We're out of the queue now, so drop the ticket ref.
    // Track the granted session id already so that leaving (or letting the timer
    // lapse) refunds the budget the server reserved at claim, even before we dial.
    queuedTicketId = "";
    if (info?.sessionId) {
      trackedSessionId = info.sessionId;
      trackedTier = info.tier || "anon";
    }
    startJoinCountdown(expiresSec);
  });

  c.addEventListener("status", (e) => {
    const detail = /** @type {CustomEvent<{ status: string }>} */ (e).detail;
    onClientStatus(detail.status);
    chat.onTransportStatus(detail.status);
    if (detail.status === "ai-speaking") chat.onAssistantActivity();
  });
  c.addEventListener("transcript", (e) => {
    const d = /** @type {CustomEvent<{ role: "user" | "assistant"; text: string; partial: boolean; itemId?: string; responseId?: string; source?: string }>} */ (e).detail;
    if (window.liveInteraction?.isLiveResponse(d?.responseId)) return;
    chat.onTranscript(d);
    if (recentCharacterId !== characterWords.编号) { recentTurns.length = 0; recentCharacterId = characterWords.编号; }
    rememberTurnForInteraction(d);
  });
  c.addEventListener("user-turn-started", (e) => {
    const detail = /** @type {CustomEvent<{ itemId?: string }>} */ (e).detail;
    chat.onUserTurnStarted(detail);
    // 陪读时用户一开口就先停掉朗读（§14）：只停"念"，转写与提交时机一律不动。
    pauseForUserTurn();
  });
  c.addEventListener("user-turn-stopped", (e) => {
    const detail = /** @type {CustomEvent<{ itemId?: string }>} */ (e).detail;
    chat.onUserTurnStopped(detail);
  });
  c.addEventListener("user-audio", (e) => {
    const detail = /** @type {CustomEvent<{ itemId?: string; audio: Blob; durationMs?: number; truncated?: boolean }>} */ (e).detail;
    chat.onUserAudio(detail);
  });

  c.addEventListener("response-finished", (e) => {
    const detail = /** @type {CustomEvent<{ responseId: string; status: string; audible?: boolean; audioSink?: string; transcript?: string }>} */ (e).detail;
    // A per-turn judgment must not persist into a later raw-microphone reply.
    if (dynamicTurnActive && modeState.mode === 'ROLEPLAY') {
      dynamicTurnActive = false; void applyTurnInstructions('');
    }
    chat.onResponseFinished(detail);
    chat.onResponseAudioFinished(detail);
    void resumeAfterAssistantReply(detail);
  });
  // §十一：音频分级（TTS_READY / AUDIO_GENERATED / AUDIO_PLAYING / …）由路由层
  // 直接推给面板。router 是 audio-router.js 挂到 globalThis 的单例，观察者只挂一次；
  // 挂了它，面板说的"正在播放"就一定是某个活 sink 真的拿到了字节，而不是猜的。
  const audioRouter = globalThis.s2sAudioRouter;
  if (audioRouter && !audioRouter._chatStateAttached) {
    audioRouter._chatStateAttached = true;
    audioRouter.on("state", (d) => chat.onAudioState(d));
  }
  c.addEventListener("error", (e) => {
    const detail = /** @type {CustomEvent<{ error: unknown }>} */ (e).detail;
    void onFatalError(detail.error);
  });
  // §7 fault A — the recovery loop lives in the client; the page only reports
  // what it actually observes, including which attempt we are on, so a caption
  // can never claim "connected" while the socket is still down.
  c.addEventListener("transport-drop", (e) => {
    const detail = /** @type {CustomEvent<{ reason: string }>} */ (e).detail;
    console.warn("[main] realtime transport dropped:", detail.reason);
  });
  c.addEventListener("reconnect", (e) => {
    const detail = /** @type {CustomEvent<{ attempt: number; reason: string; diagnostics: any }>} */ (e).detail;
    // TASK_02 STEP 1：短暂断连不改变 UI，只记录；持续 ≥3s 才显示静态徽标。
    scheduleReconnectUi(detail.attempt);
    if (DEBUG) console.debug("[ui] reconnect attempt:", detail);
  });
  c.addEventListener("reconnected", (e) => {
    const detail = /** @type {CustomEvent<{ attempt: number; generation: number }>} */ (e).detail;
    console.warn(`[main] realtime recovered on attempt ${detail.attempt} (generation ${detail.generation})`);
    cancelReconnectUi();
    setLive(true);
  });
  c.addEventListener("server-error", (e) => {
    // Non-fatal: the backend reported an error mid-session. Log it, keep the
    // socket and the conversation alive (the model can recover on its own).
    const detail = /** @type {CustomEvent<{ error: unknown }>} */ (e).detail;
    const msg = detail.error instanceof Error ? detail.error.message : String(detail.error);
    console.warn("[main] server error (non-fatal):", msg);
  });
  c.addEventListener("session", (e) => {
    const info = /** @type {CustomEvent<{ info: import("./s2s-realtime-client.js").SessionInfo }>} */ (e).detail.info;
    console.log("[ws] session created:", info.sessionId);
    // A slot was granted — we're out of the queue; drop the ticket reference so
    // teardown doesn't try to leave a line we already left.
    queuedTicketId = "";
    // A metered tier (anon / free): heartbeat so the server can extend the
    // reservation and tell us when the daily budget runs out. PRO isn't limited.
    if (info.limited && info.sessionId) {
      trackedSessionId = info.sessionId;
      trackedTier = info.tier || "anon";
      startHeartbeat(info.heartbeatSec || 5);
    }
  });
  c.addEventListener("input-level", (e) => {
    const { rms } = /** @type {CustomEvent<{ rms: number }>} */ (e).detail;
    paintInputLevel(rms);
  });

  try {
    await c.connect();
  } catch (err) {
    // The grant can be refused (402 → limit) or the dial can fail. In LB mode
    // the AudioContext hasn't been adopted by the client yet (the session POST
    // runs first), so close the one we created here to avoid leaking it.
    if (audioContext) void audioContext.close().catch(() => {});
    throw err;
  }
  markLiveIntent(true);
  setLive(true);
}

/** 刷新/关掉标签会带走整个通话（音频图与 socket 都在页面里）。刷新后诚实的
 *  状态是"未连接"——所以这里只留下"刚才被打断了"的说明，绝不自动装作在线：
 *  浏览器只在手势里才允许 AudioContext 出声，静默自动重连只会产出一个
 *  "显示已连接但音频不回来"的假状态，正是 §7.A 禁止的那种。 */
const LIVE_INTENT_KEY = "s2s.liveIntent";
function markLiveIntent(on) {
  try {
    if (on) sessionStorage.setItem(LIVE_INTENT_KEY, "1");
    else sessionStorage.removeItem(LIVE_INTENT_KEY);
  } catch { /* storage disabled — nothing to recover */ }
}

function takeLiveIntentHint() {
  try {
    if (sessionStorage.getItem(LIVE_INTENT_KEY) !== "1") return false;
    sessionStorage.removeItem(LIVE_INTENT_KEY);
    return true;
  } catch { return false; }
}

// ── Conversation-time heartbeat ─────────────────────────────────────────────

/** Ping the server every `sec` seconds so it can meter the live session; when
 *  it reports the daily budget is spent, cut the call and show the limit modal.
 *  @param {number} sec */
function startHeartbeat(sec) {
  stopHeartbeat();
  heartbeatTimer = window.setInterval(async () => {
    if (!trackedSessionId) return;
    try {
      const res = await fetch("api/session/heartbeat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId: trackedSessionId }),
        keepalive: true,
      });
      const json = await res.json().catch(() => ({}));
      if (json.expired) await onLimitReached();
    } catch (err) {
      // A transient network blip shouldn't kill the call; the next tick retries.
      if (DEBUG) console.debug("[ui] heartbeat failed:", err);
    }
  }, Math.max(1, sec) * 1000);
}

function stopHeartbeat() {
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = 0;
  }
}

/** The server cut the live session: tear down and explain why. */
async function onLimitReached() {
  const tier = trackedTier;
  stopHeartbeat();
  await teardown();
  account.showLimit(tier);
}

/** Tell the server a session ended so it reconciles + refunds the unused chunk.
 *  Uses sendBeacon so it still fires when the tab is closing. */
function endTrackedSession() {
  if (!trackedSessionId) return;
  const body = JSON.stringify({ sessionId: trackedSessionId });
  try {
    const blob = new Blob([body], { type: "application/json" });
    if (!navigator.sendBeacon("api/session/end", blob)) {
      void fetch("api/session/end", {
        method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true,
      }).catch(() => {});
    }
  } catch {
    // Best-effort; the server sweep reaps the session anyway.
  }
  trackedSessionId = "";
  trackedTier = "";
}

/** Leave the waiting queue so the LB frees our place. sendBeacon so it still
 *  fires on tab close; the LB also reaps the ticket on TTL as a backstop. */
function endQueueTicket() {
  if (!queuedTicketId) return;
  const body = JSON.stringify({ queueId: queuedTicketId });
  try {
    const blob = new Blob([body], { type: "application/json" });
    if (!navigator.sendBeacon("api/queue/end", blob)) {
      void fetch("api/queue/end", {
        method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true,
      }).catch(() => {});
    }
  } catch {
    // Best-effort; the LB reaps the ticket on TTL anyway.
  }
  queuedTicketId = "";
}

/** @param {string} status */
function onClientStatus(status) {
  switch (status) {
    case "creating-session":
    case "connecting":
      setState("connecting");
      break;
    case "queued":
      setState("queued");
      break;
    case "your-turn":
      setState("your-turn");
      break;
    case "connected":
      // The same status value closes both cases: the first handshake and a
      // recovery. Either way the transport is live now, so the page may say so.
      cancelReconnectUi();
      setLive(true);
      setState("listening");
      break;
    case "user-speaking":
      setState("user-speaking");
      break;
    case "processing":
      setState("processing");
      break;
    case "ai-speaking":
      setState("ai-speaking");
      break;
    case "reconnecting":
      // Truth first: the socket is down. Nothing may keep painting a live call.
      // TASK_02 STEP 1：短暂断连先去抖（UI 不变化），≥3s 才显示。
      scheduleReconnectUi(-1);
      break;
    case "closed":
      // teardown() will move us to idle
      cancelReconnectUi();
      break;
    case "error":
      // 恢复循环已放弃：静态错误提示（无动画）。页面仍提供 Tap to retry。
      cancelReconnectUi();
      setLive(false);
      setState("error");
      showConnBadge("连接已断开", "error");
      break;
  }
}

async function teardown() {
  stopHeartbeat();
  stopJoinCountdown();
  cancelReconnectUi();
  markLiveIntent(false);
  endTrackedSession();
  endQueueTicket();
  chat.reset({ dismiss: true });
  setLive(false);
  textOnlyMode = false;
  if (client) {
    try {
      await client.close();
    } catch (err) {
      console.warn("[main] error closing client:", err);
    }
    client = null;
  }
  if (micStream) {
    for (const track of micStream.getTracks()) track.stop();
    micStream = null;
  }
  // The webcam is independent of the call lifecycle (it runs while the user is
  // on the page), so we leave it on here — only the camera toggle stops it.
  micMuted = false;
  micBtn.classList.remove("muted");
  document.body.classList.remove("rtc-live");
  setState("idle");
  // Refresh the chip's remaining-today after the budget moved.
  if (limiterOn) void account.refresh();
}

/** @param {unknown} err */
async function onFatalError(err) {
  console.error("[main] fatal:", err);
  const message = err instanceof Error ? err.message : String(err);
  try {
    await teardown();
  } catch (teardownError) {
    console.warn("[main] error during fatal teardown:", teardownError);
  } finally {
    setState("error");
    setCaption(truncateError(message), "error");
  }
}

setState("idle");
chat.renderEmptyState();

// TASK_02 验收挂点（只读/显示层，无逻辑作用）：CDP 验收驱动用它模拟
// 持续断连徽标的显示与隐藏，不必为验证 UI 而真把链路拉断。
window.__connUi = {
  show: (text, variant) => showConnBadge(text, variant),
  hide: () => cancelReconnectUi(),
  debounceMs: RECONNECT_DEBOUNCE_MS,
};
initGateArc();
startZhCn();
// Move the existing nodes and listeners into the chat workspace; there is still
// one registry and one voice library (the settings tabs become shortcuts).
for (const [panelId, selector] of [['inline-voice', '[data-panel="voice"]'], ['inline-library', '[data-panel="voice-library"]']]) {
  const body = document.querySelector(selector);
  const destination = document.getElementById(panelId);
  if (body && destination) { body.classList.remove('tab-panel', 'active'); body.hidden = false; destination.append(body); }
}
document.querySelectorAll('#settings-tabs [data-tab="voice"],#settings-tabs [data-tab="voice-library"]').forEach(button => {
  button.addEventListener('click', event => {
    if (!event.isTrusted) return;
    settingsModal.close();
    const target = document.getElementById(button.dataset.tab === 'voice' ? 'voice-fold' : 'library-fold');
    target.open = true; target.scrollIntoView({ block: 'nearest' });
  });
});
let conversationGrew = false;
new MutationObserver(() => {
  const grown = document.querySelectorAll('.hist-msg').length >= 6;
  if (grown && !conversationGrew && !document.getElementById('voice-tools')?.contains(document.activeElement)) {
    document.querySelectorAll('#voice-tools details').forEach(d => d.open = false);
  }
  conversationGrew = grown;
}).observe(document.getElementById('gf-view-chat'), { childList: true, subtree: true });
mountGirlfriend({
  sendText: async (text) => {
    // 打字也算插话：先停朗读，再让这一句走 LLM（顺序反了会两段音频叠在一起）。
    await pauseForUserTurn();
    // 本轮互动判断：先照这一句问一次「该怎么接」，拿到就把这一块换进这一场。
    // 这一步不许把话憋住 —— 里面任何不顺利都会退回人物那块，再往下发。
    try {
      await refreshWordsForTurn(text);
    } catch (error) {
      console.warn('[reading instructions] turn blocked', error);
      setCaption(error.message === 'STALE_DYNAMIC_TURN' ? '互动状态已变更，请重新发送。' : '这次阅读上下文没有准备好，请稍后再试。', 'error');
      return false;
    }
    return client?.sendUserText(text) ?? false;
  },
  ensureLive: startTextConversation,
  setManualVoiceMode: (selection) => client?.setManualVoiceMode(selection),
  setKnownVoiceBackends: (ids) => client?.setKnownVoiceBackends?.(ids),
});
mountReading();
// 书库管理入口与音色管理面板（都在设置弹窗 / 陪读视图内，零冲突挂载）。
mountReadingManager();
mountVoiceLibrary();
// 开机先问一次权威模式：UI 显示什么完全由后端说了算。
// localStorage 里那个只是「上次选了哪一项」的提示，只用来跟后端对一次账，
// 对不上就听后端的 —— 它永远不是事实源。
void (async () => {
  await loadMode();
  let hint = "";
  try { hint = String(localStorage.getItem(MODE_HINT_KEY) || ""); } catch { /* 无痕模式 */ }
  if (hint && hint !== modeState.mode && MODE_STATUS) {
    MODE_STATUS.textContent = `上次选的是「${describeModeLabel(hint)}」，`
      + `但这台机器上现在是「${describeModeLabel(modeState.mode)}」，以后端为准。`;
  }
})();
setLive(false);
// 刷新过一次且刷新时正在通话：说明原因，状态仍是诚实的 idle（未连接）。
if (takeLiveIntentHint()) {
  setCaption("点击继续语音对话。", "muted");
}
void fetchConfig();
// 开机先插状态行（不插好，第一次回包就没地方说话），再去问一次人物的话。
mountWordsRow();
void loadCharacterWords();
// Start the webcam as soon as the user lands (camera tool defaults on), and
// react to later permission changes (re-grant after a denial re-enables it).
void autoStartCamera();
void watchCameraPermission();

// Reconcile a live session if the tab is closed/hidden mid-call (no teardown).
window.addEventListener("pagehide", () => { endTrackedSession(); endQueueTicket(); });

requestAnimationFrame(() => {
  document.body.classList.remove("booting");
});
