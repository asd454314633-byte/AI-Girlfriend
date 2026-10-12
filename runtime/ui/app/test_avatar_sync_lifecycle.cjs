'use strict';
/* ============================================================================
 * test_avatar_sync_lifecycle.cjs — avatar-sync.js 的生命周期与隔离测试
 *
 * 覆盖两个契约测试之外的任务书场景：
 *   1. 错误响应拒绝：/offer 200+code:-1 与 HTTP 500 都不得留下半开状态；
 *   2. 重复 response_id：router 侧幂等，avatar-sync 不重复投递、不重复打断；
 *   3. 切换聊天与陪读不串流：模式切换 = 取消当前轮 + 迟到音频永不投递；
 *   4. 多次建立和关闭会话：connect → disconnect 循环，名额 release 互不重复；
 *   5. 浏览器刷新及异常断线：pagehide 交回 + pc failed 单定时器重连。
 *
 * 手法与契约测试一致：vm.runInNewContext 载入真实模块 + 假浏览器。
 * ========================================================================== */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const realSetTimeout = setTimeout;
const tick = () => new Promise((resolve) => realSetTimeout(resolve, 0));
async function settle(rounds = 25) {
  for (let i = 0; i < rounds; i += 1) await tick();
}

class FakeMediaStream {
  constructor() { this.tracks = []; }
  addTrack(track) { this.tracks.push(track); }
  getTracks() { return this.tracks; }
  getVideoTracks() { return this.tracks.filter((t) => t.kind === 'video'); }
}

function makeEl(tag) {
  return {
    tag, id: '', dataset: {},
    classList: { add() {}, remove() {}, contains: () => false },
    children: [], textContent: '', srcObject: null,
    videoWidth: 0, videoHeight: 0, currentTime: Number.NaN,
    appendChild(child) { this.children.push(child); return child; },
    insertBefore(child) { this.children.unshift(child); return child; },
    setAttribute(k, v) { this[k] = v; },
    play: async () => {},
  };
}

const els = { style: makeEl('style'), layer: makeEl('div'), video: makeEl('video'), audio: makeEl('audio') };
els.layer.id = 'av-layer';
els.video.id = 'av-video';
els.audio.id = 'av-audio';
const statusEl = makeEl('div');
statusEl.id = 'gf-avatar-status';
const caption = () => statusEl.textContent;

const listeners = {};
const document = {
  readyState: 'complete',
  head: { appendChild() {} },
  body: { insertBefore() {}, appendChild() {}, firstChild: null },
  createElement: (tag) => els[tag] || makeEl(tag),
  getElementById: (id) => (id in els ? els[id] : (id === 'gf-avatar-status' ? statusEl : null)),
  addEventListener(type, fn) { listeners[type] = fn; },
  removeEventListener(type) { delete listeners[type]; },
};

const videoStream = {
  id: 'lt-video',
  tracks: [{ kind: 'video', readyState: 'live' }],
  getVideoTracks() { return this.tracks; },
  getTracks() { return this.tracks; },
};
const audioStream = {
  id: 'lt-audio',
  tracks: [{ kind: 'audio', readyState: 'live' }],
  getVideoTracks() { return []; },
  getTracks() { return this.tracks; },
};

let rtcBuilt = 0;
let rtcClosed = 0;
let sidSeq = 0;
const offers = [];
const posts = [];
const releases = [];
const interrupts = [];
let offerMode = 'ok';       // ok | http500 | code-1
let humanaudioFails = false;

/* 健康路径的最小可用 offer SDP：两条 recvonly m-line + host 候选。 */
const HEALTHY_OFFER_SDP = [
  'v=0',
  'o=- 1 2 IN IP4 127.0.0.1',
  's=-',
  't=0 0',
  'm=audio 9 UDP/TLS/RTP/SAVPF 111',
  'a=recvonly',
  'a=candidate:1 1 UDP 2013266431 127.0.0.1 5000 typ host',
  'm=video 9 UDP/TLS/RTP/SAVPF 96',
  'a=recvonly',
  'a=candidate:1 1 UDP 2013266431 127.0.0.1 5001 typ host',
  '',
].join('\r\n');

class NativeRTC {
  constructor() {
    rtcBuilt += 1;
    this.connectionState = 'new';
    this.iceConnectionState = 'new';
    this.iceGatheringState = 'complete';
    this.listeners = {};
    this.localDescription = null;
    this.torn = false;
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  }
  emit(type, detail) {
    const evt = Object.assign({ type }, detail || {});
    const handler = this[`on${type}`];
    if (typeof handler === 'function') handler(evt);
    for (const fn of this.listeners[type] || []) fn(evt);
  }
  addTransceiver() {}
  // 健康路径：真实形状的 offer（含 m-line 与 host 候选）。
  async createOffer() { return { sdp: HEALTHY_OFFER_SDP, type: 'offer' }; }
  async setLocalDescription(desc) { this.localDescription = desc; }
  async setRemoteDescription() {
    this.emit('track', { track: videoStream.tracks[0], streams: [videoStream] });
    this.emit('track', { track: audioStream.tracks[0], streams: [audioStream] });
    this.connectionState = 'connected';
    this.iceConnectionState = 'connected';
    this.emit('connectionstatechange');
    return undefined;
  }
  close() {
    if (this.torn) return;
    this.torn = true;
    rtcClosed += 1;
    this.connectionState = 'closed';
    queueMicrotask(() => this.emit('connectionstatechange'));
  }
  setState(next) {
    if (this.torn) return;
    this.connectionState = next;
    this.emit('connectionstatechange');
  }
}

const windowListeners = {};
const fakeWindow = {
  RTCPeerConnection: NativeRTC,
  console,
  WebSocket: function WS() { throw new Error('avatar-sync 不得建 WebSocket'); },
  AVATAR_CONFIG: { debug: false, chunkMs: 0, sampleRate: 24000 },
  addEventListener(type, fn) { windowListeners[type] = fn; },
  removeEventListener(type) { delete windowListeners[type]; },
};

function makeFakeRouter() {
  const sinks = new Map();
  const observers = {};
  return {
    SINK_LOCAL: 'local_browser',
    SINK_AVATAR: 'avatar',
    registerCount: 0,
    confirms: [],
    failures: [],
    cancelled: [],
    avatarWrites: [],
    open: new Map(),
    sink: (name) => sinks.get(name),
    registerSink(name, sink) { sinks.set(name, sink); this.registerCount += 1; return true; },
    unregisterSink(name) { sinks.delete(name); },
    hasSink: (name) => sinks.has(name),
    sinkAvailable(name) {
      const entry = sinks.get(name);
      if (!entry) return false;
      return typeof entry.available === 'function' ? Boolean(entry.available()) : true;
    },
    on(event, fn) { (observers[event] = observers[event] || []).push(fn); },
    notify(event, payload) { for (const fn of observers[event] || []) fn(payload); },
    write(name, samples, meta) { const entry = sinks.get(name); if (entry) entry.write(samples, meta); },
    begin(responseId) {
      if (this.open.has(responseId)) return this.open.get(responseId);   // 重复 begin 幂等
      const turn = { responseId, sink: this.sinkAvailable('avatar') ? 'avatar' : 'local_browser', bytes: 0, confirmed: 0 };
      this.open.set(responseId, turn);
      this.notify('begin', { responseId, sink: turn.sink });
      return turn;
    },
    ingest(responseId, samples, meta) {
      const turn = this.open.get(responseId) || this.begin(responseId);
      turn.bytes += samples.byteLength;
      if (turn.sink === 'avatar') {
        this.avatarWrites.push({ responseId, bytes: samples.byteLength });
        this.write('avatar', samples, Object.assign({ responseId }, meta || {}));
      }
      return turn;
    },
    end(responseId) {
      const turn = this.open.get(responseId);
      if (!turn) return null;
      this.open.delete(responseId);
      this.notify('end', { responseId, sink: turn.sink, bytes: turn.bytes });
      return turn;
    },
    cancel(responseId, reason) {
      this.cancelled.push({ responseId, reason });
      this.open.delete(responseId);
      this.notify('cancel', { responseId, reason });
    },
    confirmAvatar(responseId, bytes, receipt) {
      this.confirms.push({ responseId, bytes, receipt });
      const turn = this.open.get(responseId);
      if (turn) {
        turn.confirmed += bytes;
        if (turn.confirmed >= turn.bytes) this.open.delete(responseId);
      }
      return true;
    },
    reportAvatarFailure(responseId, reason) {
      this.failures.push({ responseId, reason });
      this.open.delete(responseId);
      return true;
    },
    diagnostics() { return { openTurns: [...this.open.values()], counters: {} }; },
  };
}

const router = makeFakeRouter();
const mediaTickers = [];

const context = {
  MediaStream: FakeMediaStream,
  window: fakeWindow,
  document,
  console,
  setTimeout: (cb, ms) => realSetTimeout(cb, ms),
  clearTimeout,
  queueMicrotask,
  Blob,
  FormData,
  AbortController,
  atob,
  setInterval: (fn) => { mediaTickers.push(fn); return mediaTickers.length; },
  clearInterval: () => {},
  fetch: async (url, options) => {
    if (url.endsWith('/offer')) {
      offers.push(JSON.parse(options.body));
      if (offerMode === 'http500') return { ok: false, status: 500, json: async () => ({ msg: 'boom' }) };
      if (offerMode === 'code-1') return { ok: true, status: 200, json: async () => ({ code: -1, msg: 'no capacity' }) };
      sidSeq += 1;
      return { ok: true, status: 200, json: async () => ({ sessionid: 4200 + sidSeq, sdp: 'answer', type: 'answer' }) };
    }
    if (url.endsWith('/humanaudio')) {
      const fd = options.body;
      posts.push({
        sessionid: fd.get('sessionid'),
        response_id: fd.get('response_id'),
        segment_index: fd.get('segment_index'),
        file: fd.get('file'),
      });
      if (humanaudioFails) return { ok: true, status: 200, json: async () => ({ code: 500, msg: 'session not found' }) };
      return { ok: true, status: 200, json: async () => ({ code: 0, data: { segment_gate: { decision: 'accept' } } }) };
    }
    if (url.endsWith('/interrupt_talk') || url.endsWith('/human')) {
      interrupts.push({ url, body: JSON.parse(options.body) });
      await tick();
      return { ok: true, status: 200, json: async () => ({ code: 0 }) };
    }
    if (url.endsWith('/api/session/release')) {
      releases.push({ body: JSON.parse(options.body), keepalive: options.keepalive });
      return { ok: true, status: 200, json: async () => ({ code: 0, data: { released: true } }) };
    }
    throw new Error('Unexpected request: ' + url);
  },
};
context.window.fetch = context.fetch;
context.s2sAudioRouter = router;

vm.runInNewContext(fs.readFileSync(`${__dirname}/avatar-sync.js`, 'utf8'), context);
const AV = context.window.AvatarSync;

const pcmChunk = (samples = 320) => new Int16Array(samples).fill(0x1122);
function pumpMedia() {
  els.video.videoWidth = 512;
  els.video.videoHeight = 896;
  els.video.currentTime = Number(els.video.currentTime || 0) + 0.04;
  videoStream.tracks[0].readyState = 'live';
  for (const fn of mediaTickers) fn();
}
async function driveTurn(responseId) {
  router.begin(responseId);
  router.ingest(responseId, pcmChunk(), { sampleRate: 24000 });
  router.end(responseId);
  await settle();
}

(async () => {
  /* --- 1. 错误响应拒绝：/offer 200+code:-1 -------------------------------- */
  offerMode = 'code-1';
  await assert.rejects(() => AV.connect(), /no capacity/, 'code:-1 的服务端话要原样透出');
  assert.equal(rtcBuilt, 1, '失败的尝试也要把半成品连接关掉');
  assert.equal(rtcClosed, 1, '无泄漏');
  assert.equal(AV.sessionid, '0', '从没拿到会话号就不可能有本地账本');
  assert.equal(releases.length, 0, '从没拿到过会话号的尝试不 release');

  offerMode = 'http500';
  await assert.rejects(() => AV.connect(), /offer HTTP 500/, 'HTTP 500 原样透出');
  assert.equal(rtcClosed, rtcBuilt, '仍然无泄漏');

  /* --- 2. 正常建立 + 服务端拒收 ------------------------------------------- */
  offerMode = 'ok';
  await AV.connect();
  await settle();
  pumpMedia();
  assert.equal(AV.mediaReady(), true, '正常建立并出画');

  humanaudioFails = true;
  await driveTurn('err-1');
  assert.equal(posts.filter((p) => p.response_id === 'err-1').length, 1, '投递恰好一次');
  assert.deepEqual(router.failures[router.failures.length - 1],
    { responseId: 'err-1', reason: 'avatar:humanaudio_failed' },
    '服务端明确拒收 → 必须把这一轮交回本机');
  assert.equal(caption(), '数字人未接收音频，已改用本机声音');
  humanaudioFails = false;

  /* --- 3. 重复 response_id：幂等，不重复投递 ------------------------------ */
  router.begin('dup-1');
  router.ingest('dup-1', pcmChunk(), { sampleRate: 24000 });
  const turnsBefore = router.open.size;
  router.begin('dup-1');                     // 重复 created：幂等
  assert.equal(router.open.size, turnsBefore, '重复 begin 不产生第二个轮');
  router.end('dup-1');
  await settle();
  router.end('dup-1');                       // 重复 end：轮已删除，不应再投
  await settle();
  assert.equal(posts.filter((p) => p.response_id === 'dup-1').length, 1,
    '重复 end 不得造成第二次投递');
  assert.equal(router.confirms.filter((c) => c.responseId === 'dup-1').length, 1);

  /* --- 4. 模式切换（聊天 → 陪读）不串流 ----------------------------------- */
  await driveTurn('mode-a');                 // 聊天模式正常一轮
  assert.equal(posts.filter((p) => p.response_id === 'mode-a').length, 1);
  const interruptsBefore = interrupts.length;
  // 进入陪读：取消在途轮 + 挂起自动重连（换会话），聊天侧迟到音频不得跟进陪读会话。
  router.begin('mode-b');
  router.ingest('mode-b', pcmChunk(), { sampleRate: 24000 });
  const cancelled = await AV.cancelAvatarTurn({ responseId: 'mode-b', reason: 'mode_switch' });
  assert.equal(cancelled, true, '取消真实发生');
  await settle();
  assert.equal(interrupts.length, interruptsBefore + 1, '恰好一次线上打断');
  assert.equal(interrupts[interrupts.length - 1].body.response_id, 'mode-b');
  assert.equal(interrupts[interrupts.length - 1].body.sessionid, String(AV.sessionid));
  router.ingest('mode-b', pcmChunk(), { sampleRate: 24000 });   // 迟到的尾巴
  router.end('mode-b');                                         // 迟到的 done
  await settle();
  assert.equal(posts.filter((p) => p.response_id === 'mode-b').length, 0,
    '已取消轮次的迟到音频不得投递（聊天侧不得串进陪读）');

  /* --- 5. 多次建立和关闭会话 ---------------------------------------------- */
  const sidsSeen = new Set();
  for (let i = 0; i < 3; i++) {
    const sid = await AV.disconnect().then(() => AV.reconnect());
    assert.ok(sid, '每次 reconnect 都拿到会话');
    sidsSeen.add(String(sid));
    pumpMedia();
    await driveTurn(`cycle-${i}`);
    assert.equal(posts[posts.length - 1].sessionid, String(sid), '新会话上出声');
  }
  assert.equal(sidsSeen.size, 3, '三次循环三个不同会话号（不复用已交回的名额）');
  const releaseSids = releases.map((r) => r.body.sessionid);
  assert.equal(new Set(releaseSids).size, releaseSids.length, 'release 的会话号互不重复');
  assert.ok(releaseSids.every((s) => s !== '0'), '从不 release 空会话号');
  assert.equal(rtcClosed, rtcBuilt - 1, '除当前在用的一条，全部关闭：无 PeerConnection 泄漏');

  /* --- 6. 异常断线：单定时器重连 + 旧名额释放 ------------------------------ */
  const offersBefore = offers.length;
  const peer = AV.peerConnection;
  peer.setState('failed');
  await settle();
  assert.equal(releases[releases.length - 1].body.reason, 'pc_failed', '异常断线也要点名释放');
  assert.equal(AV.sessionid, '0', '本地账本已归零');
  await tick();    // 给 3s 退避一点真时间（这里退避是真实 setTimeout）
  await new Promise((r) => realSetTimeout(r, 3100));
  assert.equal(offers.length, offersBefore + 1, '退避到点：恰好一次重连');
  assert.ok(String(AV.sessionid) !== '0', '重连后重新持有会话');
  assert.equal(rtcBuilt, rtcClosed + 1, '一条在用，其余全关');

  /* --- 7. 刷新：pagehide 显式交回 ----------------------------------------- */
  const liveSid = String(AV.sessionid);
  windowListeners.pagehide();
  await settle();
  assert.equal(releases[releases.length - 1].body.sessionid, liveSid, 'pagehide 点名交回');
  assert.equal(releases[releases.length - 1].body.reason, 'pagehide');
  assert.equal(AV.sessionid, '0', '账本立刻归零，不会二次 release');
  windowListeners.pagehide();
  await settle();
  assert.equal(releases.filter((r) => r.body.sessionid === liveSid).length, 1, '同一会话只 release 一次');

  console.log('PASS: lifecycle — error rejection, idempotent duplicate ids, mode-switch isolation, session cycles, crash reconnect, pagehide release');
})().catch((err) => {
  console.error('FAIL: avatar-sync 生命周期契约未通过');
  console.error(err && err.stack ? err.stack : err);
  process.exitCode = 1;
});
