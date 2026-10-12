/* ============================================================================
 * conn_trace.js — AI_GIRLFRIEND_UI_AVATAR_LAYOUT_ADJUSTER_02_FIX · STEP 4
 *
 * 真实页面连接状态无侵入记录器（TASK 02 STEP 4）：
 *   · 只记录状态【变化】，不高频轮询
 *   · 零改动 s2s-realtime-client.js / WebRTC / TTS —— 全部通过包装
 *     RTCPeerConnection / WebSocket / dispatchEvent 在外部观察
 *   · 环形缓冲 + 5s 批量 sendBeacon 到 /api/conn-trace（server.py 落盘 JSONL）
 *
 * 记录字段：
 *   t              ISO 时间戳
 *   kind           pc | ws | status | reconnect | reconnected | transport-drop
 *                  | media | client-error
 *   pc/ws 状态     connectionState / iceConnectionState / signalingState /
 *                  ws readyState（仅变化时）
 *   reconnect      attempt / reason（抖动 flap 计数= reconnect 事件次数）
 *   media          video readyState / paused / currentTime（waiting/stalled/
 *                  playing/pause 事件驱动）
 *
 * 开关：localStorage.connTraceDisabled = '1' 可整体停用（A/B 期间保持常开）。
 * 查看页内缓冲：window.__connTrace（数组，最新在尾部）。
 * ==========================================================================*/
(function () {
  'use strict';
  try {
    if (localStorage.getItem('connTraceDisabled') === '1') return;
  } catch (_) { /* ignore */ }

  var MAX_RING = 4000;
  var ring = [];
  var pending = [];
  var pcSeq = 0;
  var wsSeq = 0;

  function rec(entry) {
    entry.t = new Date().toISOString();
    ring.push(entry);
    if (ring.length > MAX_RING) ring.shift();
    pending.push(entry);
  }

  function flush() {
    if (!pending.length) return;
    var batch = pending.splice(0, pending.length);
    try {
      var blob = new Blob([JSON.stringify(batch)], { type: 'text/plain' });
      if (navigator.sendBeacon) navigator.sendBeacon('/api/conn-trace', blob);
    } catch (_) { /* 网络/页面卸载期失败不致命：ring 里仍有 */ }
  }

  // 5s 批量落盘（事件驱动记录，定时器只做批量外送，不做状态轮询）
  setInterval(flush, 5000);
  window.addEventListener('pagehide', flush);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') flush();
  });

  /* ---- 包装 RTCPeerConnection：connection/ice/signaling 状态变化 ---- */
  var OrigPC = window.RTCPeerConnection;
  if (typeof OrigPC === 'function') {
    function TracedPC(config) {
      var pc = new OrigPC(config);
      var id = ++pcSeq;
      var last = { conn: '', ice: '', sig: '' };
      function snap(prop, evName) {
        var cur = null;
        try { cur = pc[prop]; } catch (_) { return; }
        if (cur === last[evName]) return;
        last[evName] = cur;
        rec({
          kind: 'pc', pcid: id, field: evName, value: cur,
        });
      }
      pc.addEventListener('connectionstatechange', function () { snap('connectionState', 'conn'); });
      pc.addEventListener('iceconnectionstatechange', function () { snap('iceConnectionState', 'ice'); });
      pc.addEventListener('signalingstatechange', function () { snap('signalingState', 'sig'); });
      return pc;
    }
    TracedPC.prototype = OrigPC.prototype;
    // 让 pc instanceof RTCPeerConnection 依旧成立；静态属性透传
    Object.setPrototypeOf(TracedPC, OrigPC);
    window.RTCPeerConnection = TracedPC;
  }

  /* ---- 包装 WebSocket：open/close/error（readyState 仅在这些事件上记录） ---- */
  var OrigWS = window.WebSocket;
  if (typeof OrigWS === 'function') {
    function TracedWS(url, protocols) {
      var ws = protocols === undefined ? new OrigWS(url) : new OrigWS(url, protocols);
      var id = ++wsSeq;
      rec({ kind: 'ws', wsid: id, event: 'construct', url: String(url), readyState: ws.readyState });
      ws.addEventListener('open', function () {
        rec({ kind: 'ws', wsid: id, event: 'open', url: String(url), readyState: ws.readyState });
      });
      ws.addEventListener('close', function (ev) {
        rec({ kind: 'ws', wsid: id, event: 'close', url: String(url), readyState: ws.readyState, code: ev.code, reason: ev.reason || undefined });
      });
      ws.addEventListener('error', function () {
        rec({ kind: 'ws', wsid: id, event: 'error', url: String(url), readyState: ws.readyState });
      });
      return ws;
    }
    TracedWS.prototype = OrigWS.prototype;
    TracedWS.CONNECTING = OrigWS.CONNECTING;
    TracedWS.OPEN = OrigWS.OPEN;
    TracedWS.CLOSING = OrigWS.CLOSING;
    TracedWS.CLOSED = OrigWS.CLOSED;
    window.WebSocket = TracedWS;
  }

  /* ---- 观察 client 事件（status / reconnect / reconnected / transport-drop）：
   *      不改 client 源码，在 dispatchEvent 外层做只读旁路 ---- */
  var WATCH_TYPES = {
    status: true, reconnect: true, reconnected: true,
    'transport-drop': true, error: true,
  };
  var origDispatch = EventTarget.prototype.dispatchEvent;
  EventTarget.prototype.dispatchEvent = function (ev) {
    try {
      if (ev && WATCH_TYPES[ev.type]) {
        var d = ev.detail || {};
        var entry = { kind: ev.type === 'error' ? 'client-error' : ev.type };
        if (d.status !== undefined) entry.status = d.status;
        if (d.attempt !== undefined) entry.attempt = d.attempt;
        if (d.reason !== undefined) entry.reason = d.reason;
        if (d.generation !== undefined) entry.generation = d.generation;
        rec(entry);
      }
    } catch (_) { /* 记录失败绝不影响业务事件分发 */ }
    return origDispatch.call(this, ev);
  };

  /* ---- 视频/媒体中断：waiting / stalled / playing / pause（事件驱动） ---- */
  function mediaEntry(ev) {
    var v = ev.target;
    if (!v || v.tagName !== 'VIDEO') return;
    rec({
      kind: 'media', event: ev.type,
      readyState: v.readyState, paused: v.paused,
      currentTime: Number.isFinite(v.currentTime) ? Math.round(v.currentTime * 1000) / 1000 : -1,
      videoWidth: v.videoWidth || 0, videoHeight: v.videoHeight || 0,
    });
  }
  ['waiting', 'stalled', 'playing', 'pause'].forEach(function (t) {
    document.addEventListener(t, mediaEntry, true);
  });

  window.__connTrace = ring;
  window.__connTraceFlush = flush;
})();
