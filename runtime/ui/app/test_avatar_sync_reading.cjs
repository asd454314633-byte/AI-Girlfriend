'use strict';
/* ============================================================================
 * test_avatar_sync_session.cjs — avatar-sync.js 的会话有效性与音频交接测试
 *
 * 这一支守的是"数字人这一侧只在事实成立时才接管声音"：
 *   1. 不再劫持 window.WebSocket（那正是老故障的机制本体）；音频唯一入口是
 *      router 注册进来的 avatar sink；
 *   2. 必须有真实有效的 :8010 会话（/offer 返回非 0 的 sessionid）才允许投递；
 *   3. MEDIA_READY 才是硬事实：协商成功但一帧未出时 available() 必须为假，
 *      状态条也绝不允许写"数字人已连接"；
 *   4. 上传成功 **且** 真的在出画，才 router.confirmAvatar(bytes)；
 *   5. :8010 拒收音频 / 收了但没画面 → router.reportAvatarFailure(...)，
 *      这一条正是老故障"8010 挂了 = 全场静音"的反面；
 *   6. 打断 = router.cancel + 恰好一次 /interrupt_talk，迟到的 delta 不得复活该轮。
 *
 * 手法：vm.runInNewContext 载入真实的 avatar-sync.js，喂假浏览器
 * （document / RTCPeerConnection / fetch / 虚拟 setInterval）+ 一个假 router，
 * 走真实的 registerAvatarSink() 路径。投递用 gate 卡住在途窗口，保证确定性。
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
    currentTime: Number.NaN,          // NaN = 协商成功但一帧都没出（aiortc 的真实形状）
    appendChild(child) { this.children.push(child); return child; },
    insertBefore(child) { this.children.unshift(child); return child; },
    setAttribute(k, v) { this[k] = v; },
    play: async () => {},
  };
}

const els = {
  style: makeEl('style'),
  layer: makeEl('div'),
  video: makeEl('video'),
  audio: makeEl('audio'),
};
els.layer.id = 'av-layer';
els.video.id = 'av-video';
els.audio.id = 'av-audio';
const statusEl = makeEl('div');       // 页面里已存在的状态条：getElementById 命中它
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
let offerCount = 0;
let humanaudioFails = false;
const offers = [];
const posts = [];
const controlPosts = [];

/* 在途投递闸门：让测试能在"HTTP 还没回来"的窗口里改变画面事实。 */
let uploadGate = null;
function armUploadGate() {
  let release;
  const promise = new Promise((resolve) => { release = resolve; });
  uploadGate = { promise, release };
}
function releaseUploadGate() {
  if (!uploadGate) return;
  const open = uploadGate;
  uploadGate = null;
  open.release();
}

/* 健康路径的最小可用 offer SDP：两条 recvonly m-line + 至少一个 host 候选。
 * 真实浏览器在 ICE gathering 完成之后给出的 localDescription.sdp 就是这个形状。 */
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
  // 健康浏览器 + 已用具收集 completes：localDescription.sdp 必含 m-line 与
  // candidate（真实 SDP 用 CRLF 换行）。"无候选"与"收集超时"两个退化分支由
  // test_avatar_sync_ice.cjs 单独覆盖，这里走契约测试的正常路径。
  async createOffer() {
    return { sdp: HEALTHY_OFFER_SDP, type: 'offer' };
  }
  async setLocalDescription(desc) { this.localDescription = desc; }

  async setRemoteDescription() {
    // 协商成功后 aiortc 立刻推流：先 track，再 connected。
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
    this.emit('connectionstatechange');
  }
}

/* 老故障的守门人：本模块绝不允许替换 window.WebSocket，也不许建任何 WS。 */
let wsBuilt = 0;
const NativeWS = function WS() { wsBuilt += 1; };

/* window 上的生命周期挂点（pagehide / beforeunload） */
const windowListeners = {};
const fakeWindow = {
  RTCPeerConnection: NativeRTC,
  WebSocket: NativeWS,
  console,                                 // 真实 window 有 console：改造前会吞掉模块报错
  addEventListener(type, fn) { windowListeners[type] = fn; },
  removeEventListener(type) { delete windowListeners[type]; },
};

/* ------------------------------------------------------- 假 AudioRouter 替身
 * 只镜像真实 router 的两条决策：begin 时按 available() 定唯一可听 sink；
 * avatar 的两种回报（confirmAvatar / reportAvatarFailure）。
 * 路由本身的行为由 test_audio_router.cjs 直接对真 router 取证，这里只测
 * avatar-sync 这一侧是否遵守契约。
 */
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

/* ---------------------------------------------------- 真实模块 + 假浏览器环境 */
let speakingFlag = false;
const context = {
  MediaStream:FakeMediaStream,
  window: fakeWindow,
  document,
  console,
  setTimeout: (cb, ms) => realSetTimeout(cb, ms),
  clearTimeout,
  queueMicrotask,
  Blob,
  FormData,
  AbortController,                         // 真实浏览器都有：/offer 用它做超时中断
  atob,
  // 1Hz 的媒体打点在这里变成"可手动泵"的，测试才能确定性地决定何时出画。
  setInterval: (fn) => { mediaTickers.push(fn); return mediaTickers.length; },
  clearInterval: () => {},
  fetch: async (url, options) => {
    if (url.endsWith('/offer')) {
      offers.push(JSON.parse(options.body));
      offerCount += 1;
      const sid = 4200 + offerCount;        // 每次 /offer 都换一个新会话号，便于追踪归属
      await tick();                         // 网络调用：给并发抢跑留出暴露窗口
      return {
        ok: true,
        status: 200,
        json: async () => ({ sessionid: sid, sdp: 'fake-answer', type: 'answer' }),
      };
    }
    if (url.endsWith('/humanaudio')) {
      const fd = options.body;
      posts.push({
        sessionid: fd.get('sessionid'),
        response_id: fd.get('response_id'),
        segment_index: fd.get('segment_index'),
        file: fd.get('file'),
      });
      if (uploadGate) await uploadGate.promise;   // 卡住在途，供测试改变画面事实
      if (humanaudioFails) {
        return { ok: true, status: 200, json: async () => ({ code: 500, msg: 'session not found' }) };
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({ code: 0, data: { segment_gate: { decision: 'accept' } } }),
      };
    }
    if (url.endsWith('/is_speaking')) {
      return { ok: true, status: 200, json: async () => ({ code: 0, data: speakingFlag }) };
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
context.s2sAudioRouter = router;        // avatar-sync 的 routerRef() 找的就是它

vm.runInNewContext(fs.readFileSync(`${__dirname}/avatar-sync.js`, 'utf8'), context);

const AV = context.window.AvatarSync;
const pcmChunk = (samples = 320) => new Int16Array(samples).fill(0x1122);  // 640 B s16le
const caption = () => statusEl.textContent;
const interrupts = () => controlPosts.filter((p) => p.url.endsWith('/interrupt_talk'));

/** 把画面推到（或推出）MEDIA_READY：四个条件逐个可判。 */
function pumpMedia({ live = true, frames = true, advancing = true } = {}) {
  els.video.videoWidth = frames ? 512 : 0;
  els.video.videoHeight = frames ? 896 : 0;
  if (advancing) els.video.currentTime = Number(els.video.currentTime || 0) + 0.04;
  videoStream.tracks[0].readyState = live ? 'live' : 'ended';
  for (const fn of mediaTickers) fn();
}

/* ============================================================================
 * test_avatar_sync_reading.cjs — 阅读链路四个接口的离线契约测试
 *
 * 守的是"朗读断点只能被亲眼看到的播放真值推进"这同一条线：
 *   1. expectReading 登记的 key 必须绑到真正承载它的那一轮音频上；
 *   2. inputConsumed 只能由服务端收下音频的事实点亮；
 *   3. started / finished 只能由 :8010 会话级 /is_speaking 的翻转点亮，
 *      问不到就什么都不写（不许猜）；
 *   4. 停止、切换、断线之后，旧音频一律不得再投递、不得复活，
 *      曾经登记过的段必须留在作废状态，重连也不得把它救回来。
 * ==========================================================================*/
async function driveReading(responseId) {
  // 投递与回执期间画面必须仍在出帧：否则走的是"收了但没画面"这条分支。
  pumpMedia();
  router.begin(responseId);
  router.ingest(responseId, pcmChunk(), { sampleRate: 24000 });
  router.end(responseId);
  await settle();
}

/** 逐个 await 替身登记的所有周期任务（含朗读轮询），让异步轮询真的走完。 */
async function pumpPoll() {
  pumpMedia();                 // 画面继续出帧：MEDIA_READY 判的是"真的在出画"
  for (const fn of mediaTickers) await fn();
  await settle(30);            // 轮询内部的 fetch 是异步的：给足轮次让它真的走完
}

const boundary = (list, key) => list.find((b) => b.key === key);

(async () => {
  try {
    /* --- 0. 未连接时四个接口都不得抛异常 ------------------------------- */
    const early = await AV.readingPlayback();
    assert.ok(Array.isArray(early) && early.length === 0, '没有会话时朗读边界是空数组');
    assert.equal(AV.forgetReading(), true, '空账本也能安全作废');
    assert.equal(typeof AV.expectReading, 'function', 'expectReading 必须存在');
    assert.equal(typeof AV.interrupt, 'function', 'interrupt 必须存在');

    /* --- 1. 建立会话并出画 -------------------------------------------- */
    listeners.click();
    await settle();
    pumpMedia();
    await settle(2);
    assert.equal(AV.mediaReady(), true, 'MEDIA_READY 之后才谈得上播放');

    /* --- 2. key 必须绑到真正承载它的那一轮 ---------------------------- */
    AV.expectReading('gen1:doc:0:0:1');
    await driveReading('resp-reading-1');
    let list = await AV.readingPlayback();
    const seg1 = boundary(list, 'gen1:doc:0:0:1');
    assert.ok(seg1, '登记的段必须出现在边界里');
    assert.equal(seg1.responseId, 'resp-reading-1', 'key 与承载它的音频轮次一一对应');
    assert.equal(seg1.inputConsumed, true, '服务端收下音频 = 输入已消费');
    assert.ok(typeof seg1.inputEnd === 'number', '记下输入消费时刻');
    assert.equal(seg1.started, false, '还没真的出声就不许算出过声');
    assert.equal(seg1.finished, false);

    /* --- 3. 出声 / 播完只由 /is_speaking 的翻转点亮 ------------------- */
    speakingFlag = true;
    await pumpPoll();
    list = await AV.readingPlayback();
    assert.equal(boundary(list, 'gen1:doc:0:0:1').started, true, '会话真的在发声就该点亮 started');
    assert.ok(typeof boundary(list, 'gen1:doc:0:0:1').start === 'number');
    assert.ok(boundary(list, 'gen1:doc:0:0:1').clockUncertaintyMs > 0, '如实给出时间不确定性');

    speakingFlag = false;
    await pumpPoll();
    list = await AV.readingPlayback();
    assert.equal(boundary(list, 'gen1:doc:0:0:1').finished, true, '发声结束 → 这一段播完');
    assert.ok(typeof boundary(list, 'gen1:doc:0:0:1').end === 'number');

    /* --- 4. 多段按序推进：完成信号必须落在正确的段上 ------------------- */
    AV.expectReading('k-a');
    AV.expectReading('k-b');
    await driveReading('resp-a');
    speakingFlag = true; await pumpPoll();
    speakingFlag = false; await pumpPoll();
    list = await AV.readingPlayback();
    assert.equal(boundary(list, 'k-a').finished, true, '先登记先播完');
    assert.equal(boundary(list, 'k-b').started, false, '后一段没轮到就不许抢先完成');
    await driveReading('resp-b');
    speakingFlag = true; await pumpPoll();
    speakingFlag = false; await pumpPoll();
    list = await AV.readingPlayback();
    assert.equal(boundary(list, 'k-b').finished, true, '第二轮结束后第二段落完成');
    assert.ok(boundary(list, 'k-a').end <= boundary(list, 'k-b').end, '顺序不得颠倒');

    /* --- 5. 聊天轮次不得占用朗读位；作废之后更不得关联 ----------------- */
    AV.expectReading('k-chat-guard');
    AV.forgetReading('k-chat-guard');
    await driveReading('resp-chat');
    list = await AV.readingPlayback();
    assert.equal(boundary(list, 'k-chat-guard'), undefined, '作废的段不得留在账本里');
    assert.equal(list.some((b) => b.responseId === 'resp-chat'), false, '聊天轮次不进朗读账本');

    /* --- 6. 停止：旧音频不再投递，迟到到达也不得复活 ------------------- */
    AV.expectReading('k-stop');
    await driveReading('resp-stop');
    list = await AV.readingPlayback();
    assert.equal(boundary(list, 'k-stop').inputConsumed, true, '停止前确实投递过一次');
    const postsBeforeStop = posts.length;
    const interruptsBefore = controlPosts.filter((p) => p.url.endsWith('/interrupt_talk')).length;
    await AV.interrupt({ reason: 'user_stop' });
    await settle(2);
    list = await AV.readingPlayback();
    assert.equal(boundary(list, 'k-stop').abandoned, true, '停止后这一段作废');
    assert.ok(controlPosts.filter((p) => p.url.endsWith('/interrupt_talk')).length > interruptsBefore,
      '停止必须真的去 :8010 打断');
    router.ingest('resp-stop', pcmChunk(), { sampleRate: 24000 });
    router.end('resp-stop');
    await settle(4);
    assert.equal(posts.length, postsBeforeStop, '停止之后旧音频一律不得再投递');
    list = await AV.readingPlayback();
    assert.equal(boundary(list, 'k-stop').started, false, '不得用迟到音频伪造出声');

    /* --- 7. 断线：未播完的段作废，重连后不得复活 ----------------------- */
    AV.expectReading('k-drop');
    await driveReading('resp-drop');
    await AV.disconnect();
    await settle(4);
    list = await AV.readingPlayback();
    assert.equal(boundary(list, 'k-drop').abandoned, true, '会话断了这一段就作废');
    const postsAfterDrop = posts.length;
    router.ingest('resp-drop', pcmChunk(), { sampleRate: 24000 });
    router.end('resp-drop');
    await settle(4);
    assert.equal(posts.length, postsAfterDrop, '断线后的迟到音频不得投递');
    assert.equal(AV.mediaReady(), false, '没有会话就不许声称在出画');

    /* --- 8. 服务端拒收 = 所有权交回本机，账本不得假装播过 -------------- */
    await AV.reconnect();
    await settle();
    pumpMedia();
    await settle(2);
    humanaudioFails = true;
    AV.expectReading('k-fail');
    await driveReading('resp-fail');
    list = await AV.readingPlayback();
    assert.equal(boundary(list, 'k-fail').inputConsumed, false, '被拒收就没有"输入已消费"');
    assert.equal(boundary(list, 'k-fail').abandoned, true, '拒收即作废，不得再用于推进断点');
    assert.deepEqual(router.failures[router.failures.length - 1],
      { responseId: 'resp-fail', reason: 'avatar:humanaudio_failed' },
      '服务端拒收必须把这一轮交回本机，避免本机与数字人同时出声');

    /* --- 9. 异常安全：会话异常时任何接口都不得抛出 --------------------- */
    await AV.disconnect();
    await settle(2);
    await AV.readingPlayback();
    await AV.interrupt();
    await AV.forgetReading();
    AV.expectReading('k-after');
    await AV.readingPlayback();

    console.log('PASS: reading contract — key binding, sequential boundaries, stop/disconnect safety, ownership handback');
  } catch (err) {
    console.log('FAIL: 阅读链路契约未通过');
    console.log(err && err.message);
    if (err && err.actual !== undefined) console.log(JSON.stringify({ actual: err.actual, expected: err.expected }));
    process.exitCode = 1;
  }
})();
