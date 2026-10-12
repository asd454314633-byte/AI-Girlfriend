'use strict';
/* ============================================================================
 * test_avatar_sync_session_reuse.cjs — avatar-sync.js 的会话复用与单一所有权测试
 *
 * 这一支守的是 :8010 只有 5 个会话槽位时的三条命门：
 *   1. single-flight：并发 connect() 只允许打出一个 /offer（connectGeneration
 *      + connectPromise），一个页面一条 RTCPeerConnection；
 *   2. 会话复用：一个已 connected 的会话必须被后续每一轮复用，绝不再开第二条；
 *   3. 唯一拆除路径 retire()：close → 等到 closed → POST /api/session/release
 *      （keepalive:true），所以旧会话不会占着槽位等新 /offer 撞上来；
 *   4. 恰好一个重连定时器：抖动/连续失败只能排下一次尝试，不能排出一场风暴；
 *   5. 换人挂起（disconnect/reconnect）与 pagehide 显式交回会话；
 *   6. 仍然不劫持 window.WebSocket，音频只从 router 的 avatar sink 进来。
 *
 * 手法：真实模块 + 假浏览器；定时器换成虚拟时钟，所以 3s/5s 退避不用真等。
 * ========================================================================== */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class FakeMediaStream {
 constructor(){this.tracks=[];}
 addTrack(track){this.tracks.push(track);}
 getTracks(){return this.tracks;}
 getVideoTracks(){return this.tracks.filter(t=>t.kind==='video');}
 getAudioTracks(){return this.tracks.filter(t=>t.kind==='audio');}
}


const realSetTimeout = setTimeout;
const tick = () => new Promise((resolve) => realSetTimeout(resolve, 0));
async function settle(rounds = 25) {
  for (let i = 0; i < rounds; i += 1) await tick();
}

/* -------------------------------------------------- 虚拟时钟（重连不用真等） */
let clock = 0;
let timerSeq = 0;
const timers = [];
function setTimeoutV(cb, ms) {
  const t = { id: ++timerSeq, cb, ms: ms || 0, at: clock + (ms || 0), state: 'pending' };
  timers.push(t);
  return t.id;
}
function clearTimeoutV(id) {
  const t = timers.find((x) => x.id === id && x.state === 'pending');
  if (t) t.state = 'cleared';
}
async function advance(ms) {
  const target = clock + ms;
  for (;;) {
    const due = timers
      .filter((t) => t.state === 'pending' && t.at <= target)
      .sort((a, b) => a.at - b.at)[0];
    if (!due) break;
    due.state = 'fired';
    clock = Math.max(clock, due.at);
    due.cb();
    await settle(4);
  }
  clock = target;
  await settle();
}
// 3000 = pc failed/closed 的退避，5000 = connect 失败的退避（生产常量，故意点名）
const pendingReconnectTimers = () => timers
  .filter((t) => t.state === 'pending' && (t.ms === 3000 || t.ms === 5000));

/* ------------------------------------------------------------ 假浏览器 DOM */
function makeEl(tag) {
  return {
    tag,
    id: '',
    dataset: {},
    classList: { add() {}, remove() {}, contains: () => false },
    children: [],
    textContent: '',
    srcObject: null,
    videoWidth: 0,
    videoHeight: 0,
    currentTime: Number.NaN,
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
const caption = () => statusEl.textContent;

/* --------------------------------------------------------- 假 WebRTC 与流量 */
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
let offerFails = false;
const offerAttempts = [];
const posts = [];
const releases = [];
const controlPosts = [];

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
    this.bornAt = clock;
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
  // 健康路径走真实形状：含 m-line 与 host 候选（退化分支另由 ICE 单测覆盖）。
  async createOffer() {
    return { sdp: HEALTHY_OFFER_SDP.replace(/5000|5001/, (m) => String(Number(m) + rtcBuilt)), type: 'offer' };
  }
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
    // 规范里 close() 的 connectionstatechange 是**排队任务**，不是同步派发。
    // 同步派发会让 retire() 里的 close 反过来重入 onconnectionstatechange
    // （那时 pc 还指着同一把 peer），把一个会话 release 两次。
    queueMicrotask(() => this.emit('connectionstatechange'));
  }

  setState(next) {
    if (this.torn) return;          // 已 close 的连接不可能再回到 failed：UA 不变量
    this.connectionState = next;
    this.emit('connectionstatechange');
  }
}

let wsBuilt = 0;
const NativeWS = function WS() { wsBuilt += 1; };
const windowListeners = {};
const fakeWindow = {
  RTCPeerConnection: NativeRTC,
  WebSocket: NativeWS,
  console,
  AVATAR_CONFIG: { debug: false, chunkMs: 0, sampleRate: 24000 },
  addEventListener(type, fn) { windowListeners[type] = fn; },
  removeEventListener(type) { delete windowListeners[type]; },
};

/* -------------------------------------------------------- 假 AudioRouter 替身 */
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
      const sink = this.sinkAvailable('avatar') ? 'avatar' : 'local_browser';
      const turn = { responseId, sink, bytes: 0, confirmed: 0 };
      this.open.set(responseId, turn);
      this.notify('begin', { responseId, sink });
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
      this.notify('end', { responseId, sink: turn.sink, bytes: turn.bytes });
      return turn;
    },
    cancel(responseId, reason) {
      this.cancelled.push({ responseId, reason });
      this.open.delete(responseId);
      this.notify('cancel', { responseId, reason });
    },
    confirmAvatar(responseId, bytes) {
      this.confirms.push({ responseId, bytes });
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

/* ---------------------------------------------------- 真实模块 + 假浏览器环境 */
const context = {
  MediaStream:FakeMediaStream,
  window: fakeWindow,
  document,
  console,
  setTimeout: setTimeoutV,
  clearTimeout: clearTimeoutV,
  queueMicrotask,
  Blob,
  FormData,
  AbortController,
  atob,
  setInterval: (fn) => { mediaTickers.push(fn); return mediaTickers.length; },
  clearInterval: () => {},
  fetch: async (url, options) => {
    if (url.endsWith('/offer')) {
      offerAttempts.push(JSON.parse(options.body));
      await tick();                      // 并发抢跑必须在这里被 single-flight 挡住
      if (offerFails) return { ok: false, status: 503, json: async () => ({ msg: 'no capacity' }) };
      sidSeq += 1;
      return { ok: true, status: 200, json: async () => ({ sessionid: 4200 + sidSeq, sdp: 'answer', type: 'answer' }) };
    }
    if (url.endsWith('/humanaudio')) {
      const fd = options.body;
      posts.push({
        sessionid: fd.get('sessionid'),
        response_id: fd.get('response_id'),
        segment_index: fd.get('segment_index'),
        bytes: fd.get('file').size,
      });
      return { ok: true, status: 200, json: async () => ({ code: 0, data: { segment_gate: { decision: 'accept' } } }) };
    }
    if (url.endsWith('/api/session/release')) {
      releases.push({ url, body: JSON.parse(options.body), keepalive: options.keepalive });
      return { ok: true, status: 200, json: async () => ({ code: 0, data: { released: true } }) };
    }
    if (url.endsWith('/interrupt_talk') || url.endsWith('/human')) {
      controlPosts.push({ url, body: JSON.parse(options.body) });
      await tick();
      return { ok: true, status: 200, json: async () => ({ code: 0 }) };
    }
    throw new Error('Unexpected request: ' + url);
  },
};
context.window.fetch = context.fetch;
context.s2sAudioRouter = router;

vm.runInNewContext(fs.readFileSync(`${__dirname}/avatar-sync.js`, 'utf8'), context);
const AV = context.window.AvatarSync;

const pcmChunk = (samples = 320) => new Int16Array(samples).fill(0x1122);
function pumpMedia({ live = true, frames = true, advancing = true } = {}) {
  els.video.videoWidth = frames ? 512 : 0;
  els.video.videoHeight = frames ? 896 : 0;
  if (advancing) els.video.currentTime = Number(els.video.currentTime || 0) + 0.04;
  videoStream.tracks[0].readyState = live ? 'live' : 'ended';
  for (const fn of mediaTickers) fn();
}
/** 走一轮真实形状：begin → ingest → end → 投递 → 回执。 */
async function driveTurn(responseId, chunks = 1) {
  router.begin(responseId);
  for (let i = 0; i < chunks; i += 1) router.ingest(responseId, pcmChunk(), { sampleRate: 24000 });
  router.end(responseId);
  await settle();
}

(async () => {
  /* --- 0. 不劫持 WebSocket，只注册一个 avatar sink ------------------------ */
  assert.equal(wsBuilt, 0, '不得再建 WebSocket');
  assert.equal(fakeWindow.WebSocket, NativeWS, 'window.WebSocket 必须保持原样');
  assert.equal(router.registerCount, 1, 'boot() 注册唯一 avatar sink');
  assert.equal(router.sinkAvailable('avatar'), false, '没建会话前不可用');
  assert.equal(AV.suspended, false);

  /* --- 1. single-flight：并发 connect 只打一个 /offer -------------------- */
  const sids = await Promise.all([AV.connect(), AV.connect(), AV.connect()]);
  await settle();
  assert.deepEqual(sids, [4201, 4201, 4201], '三路并发必须拿回同一个 sessionid');
  assert.equal(offerAttempts.length, 1, '三次并发 connect 只允许一个 /offer');
  assert.equal(rtcBuilt, 1, '一个页面一条 PeerConnection');
  assert.equal(AV.connectionState, 'connected');
  assert.equal(offerAttempts[0].type, 'offer', '/offer 带的是本地 SDP');

  /* --- 2. 复用：连打三轮，会话号始终一个 -------------------------------- */
  pumpMedia();
  await settle(1);
  assert.equal(AV.mediaReady(), true, '出画后 MEDIA_READY 成立');
  await driveTurn('a1');
  await driveTurn('a2');
  await driveTurn('a3', 2);
  assert.equal(offerAttempts.length, 1, 'SESSION_REUSED：三轮对话不开第二个会话');
  assert.equal(rtcBuilt, 1, '三轮对话零新增 PeerConnection');
  assert.equal(posts.length, 3, '每轮恰好一次投递，NO_DUPLICATE_AUDIO');
  assert.deepEqual(posts.map((p) => p.sessionid), ['4201', '4201', '4201'], '全部复用同一会话');
  assert.deepEqual(posts.map((p) => p.response_id), ['a1', 'a2', 'a3']);
  assert.deepEqual(posts.map((p) => p.segment_index), ['0', '0', '0'], 'chunkMs=0 时每轮只有一个分段');
  assert.equal(posts[2].bytes, 44 + 1280, 'a3 两段 PCM 合成一个 WAV');
  assert.deepEqual(router.confirms.map((c) => c.responseId), ['a1', 'a2', 'a3']);
  assert.equal(router.confirms[2].bytes, 1280, '回执的是 PCM 字节数');
  assert.equal(router.failures.length, 0);

  /* --- 3. 抖动风暴：恰好一个重连定时器 + 旧会话被 release ---------------- */
  const peerA = AV.peerConnection;
  peerA.setState('disconnected');
  peerA.setState('disconnected');
  peerA.setState('failed');
  peerA.setState('failed');
  await settle();
  assert.equal(releases.length, 1, '一个死会话只被点名释放一次');
  assert.equal(releases[0].url.endsWith('/api/session/release'), true);
  assert.equal(releases[0].body.sessionid, '4201', '释放点名的就是那个已死的会话');
  assert.equal(releases[0].body.reason, 'pc_failed');
  assert.equal(releases[0].keepalive, true, 'pagehide 也要能带走 → keepalive:true');
  assert.equal(peerA.connectionState, 'closed', 'close 先于 release（§七 顺序契约）');
  assert.equal(rtcClosed, 1, '旧连接真的被关掉了，没留尸');
  assert.equal(pendingReconnectTimers().length, 1, '恰好一个重连定时器，不排风暴');
  assert.equal(offerAttempts.length, 1, '释放确认之前不得抢跑新 /offer');

  await advance(3000);
  assert.equal(offerAttempts.length, 2, '退避到点：恰好一次重连');
  assert.equal(rtcBuilt, 2, '重连换一条新连接，旧的已 closed');
  assert.equal(String(AV.sessionid), '4202', '新会话生效');
  assert.equal(AV.connectionState, 'connected');
  pumpMedia();
  assert.equal(AV.mediaReady(), true, '重连后重新判定画面');

  /* --- 4. 新会话上继续出声：复用，不再增会话 ---------------------------- */
  await driveTurn('b1');
  assert.equal(posts.length, 4);
  assert.equal(posts[3].sessionid, '4202', '新轮次用新会话');
  assert.equal(offerAttempts.length, 2, '这一轮没有再开会话');
  assert.equal(router.confirms.length, 4);

  /* --- 5. 连续失败也只排一次重试（generation + 单一定时器）------------- */
  const peerB = AV.peerConnection;
  offerFails = true;
  peerB.setState('failed');                     // 先把在用的会话打断，否则 connect() 直接复用返回
  await settle();
  assert.equal(releases.length, 2, '一次死会话一次 release');
  assert.equal(releases[1].body.sessionid, '4202');
  assert.equal(pendingReconnectTimers().length, 1, 'failed 之后排了一次 3s');
  await assert.rejects(() => AV.connect(), /offer HTTP 503/, '容量满时把服务端的话原样抛出，不压成一句"连接失败"');
  await assert.rejects(() => AV.connect(), /offer HTTP 503/);
  await settle();
  assert.equal(pendingReconnectTimers().length, 1,
    '三个排队来源（1×failed + 2×connect 失败）之后仍然只有一个待的重试');
  assert.equal(offerAttempts.length, 4, '两次手动 connect 各打了一次 /offer');
  assert.equal(rtcBuilt, 4);
  assert.equal(rtcClosed, rtcBuilt, '此刻没有在飞的 PeerConnection：半成品也被关掉，无泄漏');
  offerFails = false;
  await advance(5000);
  assert.equal(offerAttempts.length, 5, '退避到点只补一次 /offer');
  assert.equal(String(AV.sessionid), '4203');
  assert.equal(releases.length, 2, '从没拿到过会话号的尝试不该 release');
  pumpMedia();

  /* --- 6. 分块模式（chunkMs>0）：同一条 response 的第二段会怎样 ---------- */
  await driveTurn('c0');                 // 先证明默认路径正常
  const beforeChunked = posts.length;
  AV.config.chunkMs = 1200;              // 等价于 AVATAR_CONFIG.chunkMs=1200
  // 注意算术：24kHz s16le 的 1200ms = 28800 采样 = 57600 字节。
  router.begin('chunked');
  router.ingest('chunked', pcmChunk(28800), { sampleRate: 24000 });   // 攒够 1200ms → 自动投第一段
  await settle();
  assert.equal(posts.length, beforeChunked + 1, '第一段按阈值先行投递（降低首响延迟）');
  assert.equal(posts[posts.length - 1].bytes, 44 + 57600);
  assert.equal(posts[posts.length - 1].segment_index, '0');
  router.ingest('chunked', pcmChunk(28800), { sampleRate: 24000 });   // 第二段：本该再投一次
  router.end('chunked');
  await settle();
  AV.config.chunkMs = 0;
  const chunkedPosts = posts.filter((p) => p.response_id === 'chunked');
  // 老故障：submittedResponseIds 按 response 记账，第二段永远被"这条已经交过了"挡掉，
  // 于是 1200ms 阈值形同虚设、尾巴整段静音。现在的契约是同一条 response 的每一段都如实投递。
  assert.equal(chunkedPosts.length, 2, '同一条 response 的两段都要投到 /humanaudio，第一段之后不吞尾巴');
  assert.deepEqual(chunkedPosts.map((p) => p.segment_index), ['0', '1'],
    '两段各自拿到递增的 segment_index（:8010 靠它去重，不是靠客户端少投）');
  assert.deepEqual(chunkedPosts.map((p) => p.bytes), [44 + 57600, 44 + 57600],
    '每段都是完整的 1200ms（57600 字节 PCM + 44 字节 WAV 头）');
  assert.deepEqual(router.confirms.filter((c) => c.responseId === 'chunked').map((c) => c.bytes),
    [57600, 57600], '两段都拿到 :8010 的 accept 回执并各自确认了自己的字节');
  assert.equal(router.open.has('chunked'), false, '全部字节确认后这一轮在 router 侧必须收口，不留悬空轮');
  assert.equal(router.failures.filter((f) => f.responseId === 'chunked').length, 0,
    '两段都被接收的轮次不该触发 fail-open');

  /* --- 7. 换人：disconnect 挂起 → connect 被拒 → reconnect 恢复 ---------- */
  const sidBeforeSwitch = String(AV.sessionid);
  AV.disconnect();
  await settle();
  assert.equal(AV.suspended, true, '换人期间挂起自动重连');
  assert.equal(releases.length, 3, '旧会话必须点名交回，否则新服务一起来就撞旧账本');
  assert.equal(releases[2].body.reason, 'avatar_switch');
  assert.equal(releases[2].body.sessionid, sidBeforeSwitch);
  assert.equal(releases[2].keepalive, true);
  await assert.rejects(() => AV.connect(), /挂起/, '挂起期间 connect() 必须明确拒绝');
  assert.equal(router.sinkAvailable('avatar'), false, '挂起时 router 不会把音频交给数字人');
  const writesBeforeSwitch = router.avatarWrites.length;
  const confirmsBeforeSwitch = router.confirms.length;
  await driveTurn('during-switch');
  assert.equal(router.open.get('during-switch').sink, 'local_browser', '挂起期间这一轮判给本机 sink');
  assert.equal(router.avatarWrites.length, writesBeforeSwitch, '挂起期间一个字节都不该交给 avatar sink');
  assert.equal(router.confirms.length, confirmsBeforeSwitch, '判给本机就不该有 avatar 回执');
  assert.equal(posts.length, beforeChunked + 2,
    '挂起期间零投递（+2 是上面 chunked 那条 response 的两段，不是这里的）');

  await AV.reconnect();
  await settle();
  assert.equal(AV.suspended, false);
  assert.equal(String(AV.sessionid), '4204', 'reconnect() 之后拿到新会话');
  await driveTurn('after-switch');
  assert.equal(posts[posts.length - 1].sessionid, '4204', '换人后正常出声');

  /* --- 8. 页面消失：pagehide 必须把会话交回 ------------------------------ */
  const liveSid = String(AV.sessionid);
  windowListeners.pagehide();
  await settle();
  assert.equal(releases.length, 4, 'pagehide 也要显式 release');
  assert.equal(releases[3].body.sessionid, liveSid);
  assert.equal(releases[3].body.reason, 'pagehide');
  assert.equal(releases[3].keepalive, true);
  assert.equal(String(AV.sessionid), '0', '交回后本地账本立刻归零，不会二次 release');

  /* --- 9. 总量：无泄漏、无风暴 ------------------------------------------ */
  assert.equal(offerAttempts.length, 6, `/offer 总量必须等于会话数（实际 ${offerAttempts.length}）`);
  assert.equal(rtcBuilt, 6, 'PeerConnection 总量与 /offer 一一对应');
  assert.equal(rtcClosed, rtcBuilt - 1, '除当前在用的一条，全部关闭：无 PeerConnection 泄漏');
  assert.equal(releases.length, 4, '每一次真正拿到过会话号的拆除都 release 了');
  assert.equal(new Set(releases.map((r) => r.body.sessionid)).size, 4, 'release 的会话号互不重复');
  assert.deepEqual(releases.map((r) => r.keepalive), [true, true, true, true], '全部 keepalive:true');

  /* --- 10. 唯一还开着的一条：分块模式下同一轮的第二段（放在最后，别遮挡上文）*/
  // DEFAULTS.chunkMs 的注释建议"再试 1200"，但一旦启用：pushAudio 见
  // submittedResponseIds 命中就 return（avatar-sync.js:600），flush 同样早退
  // （:625），于是同一条 response 只剩第一段能到 :8010，后半段只能由 router
  // 的 grace 兜底在本机播 —— 一句话被劈成"数字人 + 本机"两半。
  assert.deepEqual(chunkedPosts.map((p) => p.segment_index), ['0', '1'],
    'chunkMs=1200 时两段 1200ms 音频应各投一次，实际只投了 ' + chunkedPosts.length + ' 次');

  console.log('PASS: one reused session per page, single-flight connect, one reconnect timer, release on every teardown');
})().catch((err) => {
  console.error('FAIL: avatar-sync 会话复用契约未通过');
  console.error(err && err.stack ? err.stack : err);
  process.exitCode = 1;
});
