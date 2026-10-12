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

(async () => {
  /* --- 0. 加载即成立的事实 ----------------------------------------------- */
  assert.equal(wsBuilt, 0, 'avatar-sync 不得再建 WebSocket：音频唯一入口是 router 的 avatar sink');
  assert.equal(context.window.WebSocket, NativeWS, 'window.WebSocket 必须保持原样，不许被劫持');
  assert.equal(router.registerCount, 1, 'boot() 必须把 avatar sink 注册进 router');
  assert.ok(router.hasSink('avatar'));
  assert.equal(AV.registerAvatarSink(), true, 'registerAvatarSink 可重复调用');
  assert.equal(router.registerCount, 1, '重复注册必须幂等（不得叠第二个 sink）');
  assert.equal(mediaTickers.length, 1, '媒体打点只有一个定时器');
  assert.equal(caption(), '数字人待命', '初始状态条');
  assert.equal(router.sinkAvailable('avatar'), false, '会话都没建起来时 avatar 侧必须不可用');

  /* --- 1. 点击建会话，但"没出画就不算连接" -------------------------------- */
  listeners.click();
  await settle();
  assert.equal(offers.length, 1, '一次点击只允许一个 /offer');
  assert.equal(rtcBuilt, 1, '一个页面一条 PeerConnection');
  assert.equal(AV.connectionState, 'connected', 'WebRTC 协商完成');
  assert.equal(String(AV.sessionid), '4201', '/offer 必须交回一个非 0 的有效 sessionid');
  assert.equal(AV.mediaReady(), false, 'currentTime 未前进 = 一帧未出 → MEDIA_READY 为假');
  assert.equal(router.sinkAvailable('avatar'), false,
    '连上但没出画时，router 不得把音频交给数字人');
  assert.equal(caption(), '已接通，等待画面…', '§十一：没出画绝不允许写"数字人已连接"');

  /* --- 2. 画面真的活了 --------------------------------------------------- */
  pumpMedia();
  await settle(1);
  assert.equal(AV.mediaReady(), true, 'srcObject + live track + 宽高 + currentTime 前进');
  assert.equal(router.sinkAvailable('avatar'), true, 'MEDIA_READY 之后才允许路由到数字人');
  assert.equal(els.video.srcObject,els.audio.srcObject,'audio/video must use one combined stream');
  assert.equal(els.video.srcObject.getTracks().length,2,'both separate incoming track streams must be combined');
  assert.equal(els.video.muted,false,'video owns audible output and video presentation');
  assert.equal(els.audio.muted,true,'legacy audio handle must be silent');
  assert.equal(els.audio.autoplay,false,'legacy audio handle must never start a second media clock');


  /* --- 3. 完整一轮：只有"服务端收了且我们在出画"才算承担 ------------------ */
  router.begin('resp-1');
  router.ingest('resp-1', pcmChunk(), { sampleRate: 24000 });
  assert.deepEqual(router.avatarWrites, [{ responseId: 'resp-1', bytes: 640 }],
    '本轮音频全部交给 avatar，且字节数=注入字节数');
  armUploadGate();
  router.end('resp-1');
  // 'end' 观察者先落状态条，drain 之后才把状态推进到"正在说话"。
  assert.equal(caption(), '数字人已连接', '出画之后才允许这句话');
  await settle(6);
  assert.equal(posts.length, 1, 'end 之后才投递，且恰好一次 /humanaudio');
  assert.equal(caption(), '数字人正在说话', '在途且真的在出画，才敢声称数字人在说话');
  assert.equal(router.confirms.length, 0, 'HTTP 未回来之前绝不允许回执"数字人承担了"');
  releaseUploadGate();
  await settle();

  assert.equal(offers.length, 1, '上传复用已建好的会话，不开第二条');
  assert.equal(posts[0].sessionid, '4201', '投递用的就是那个有效 sessionid');
  assert.equal(posts[0].response_id, 'resp-1');
  assert.equal(posts[0].segment_index, '0', '幂等键的另一半必须带上');
  assert.equal(posts[0].file.size, 684, '640B PCM + 44B WAV 头');

  const wav = Buffer.from(await posts[0].file.arrayBuffer());
  assert.equal(wav.subarray(0, 4).toString(), 'RIFF');
  assert.equal(wav.subarray(8, 12).toString(), 'WAVE');
  assert.equal(wav.readUInt32LE(24), 24000, 'WAV 头声明的必须是 delta 带下来的采样率');
  assert.equal(wav.readUInt16LE(34), 16, 'PCM/16');
  assert.equal(wav.readUInt32LE(40), 640, 'data 段长度 = 收到的字节数');
  // 0x1122 按小端落成 22 11 —— 与 WAV 头声称的 PCM/1(LE) 一致，能抓住字节序回归。
  assert.deepEqual([...wav.subarray(44, 48)], [0x22, 0x11, 0x22, 0x11], 'PCM 原样按小端落进 WAV');
  assert.equal(wav.length, 44 + 640);

  // 注意：receipt 是沙箱里造的对象，跨 realm 的 deepStrictEqual 会因原型不同而失败，
  // 所以逐字段比。
  assert.equal(router.confirms.length, 1, '恰好一次 confirmAvatar');
  assert.equal(router.confirms[0].responseId, 'resp-1');
  assert.equal(router.confirms[0].bytes, 640, '交回的是 PCM 字节数，不是 WAV 大小');
  assert.equal(router.confirms[0].receipt.segmentIndex, 0, '幂等键要随回执交回');
  assert.equal(router.confirms[0].receipt.decision, 'accept');
  assert.deepEqual([...router.open.keys()], [], '全回执后该轮在 router 侧结清');
  assert.equal(router.failures.length, 0);
  assert.equal(caption(), '数字人正在说话', '数字人确实在发声（本机侧一次写入都没有）');

  /* --- 4. :8010 拒收音频 → 必须把这一轮交回本机（老故障的反面） ----------- */
  humanaudioFails = true;
  router.begin('resp-2');
  router.ingest('resp-2', pcmChunk(), { sampleRate: 24000 });
  router.end('resp-2');
  await settle();
  assert.equal(posts.length, 2, '第二轮确实去投递了');
  assert.equal(router.confirms.length, 1, '被拒收的一轮绝不允许伪装成"数字人承担了"');
  assert.deepEqual(router.failures, [{ responseId: 'resp-2', reason: 'avatar:humanaudio_failed' }],
    'router.reportAvatarFailure 必须被调用 —— 这就是"8010 挂了也有声音"的那一步');
  assert.equal(caption(), '数字人未接收音频，已改用本机声音');
  humanaudioFails = false;

  /* --- 5. 服务端收了、但画面在途期间死了 → 同样不算承担 ------------------- */
  router.begin('resp-3');
  router.ingest('resp-3', pcmChunk(), { sampleRate: 24000 });
  armUploadGate();
  router.end('resp-3');
  await settle(6);
  assert.equal(posts.length, 3, '第三轮已投递，正等在途');
  pumpMedia({ live: false });            // HTTP 还挂着时把画面掐掉
  releaseUploadGate();
  await settle();
  assert.equal(router.confirms.length, 1, '没有画面就没有"数字人发声"，收了也不算');
  assert.deepEqual(router.failures[1], {
    responseId: 'resp-3', reason: 'avatar:accepted_but_not_rendering',
  });
  const captionAfterHandback = caption();  // 记下此刻状态条，断言放在文件最后（见 §十二）
  pumpMedia();
  assert.equal(AV.mediaReady(), true, '画面恢复');

  /* --- 6. 打断：router.cancel + 一次 /interrupt_talk + 迟到不复活 --------- */
  router.begin('resp-4');
  router.ingest('resp-4', pcmChunk(), { sampleRate: 24000 });
  await AV.cancelAvatarTurn({ responseId: 'resp-4', reason: 'user_barge_in' });
  await settle();
  assert.deepEqual(router.cancelled, [{ responseId: 'resp-4', reason: 'user_barge_in' }],
    '取消必须先告诉 router：它的保留缓冲和回执定时器都要在这里死掉');
  assert.equal(interrupts().length, 1, ':8010 侧恰好一次打断');
  assert.equal(interrupts()[0].body.response_id, 'resp-4');
  assert.equal(caption(), '数字人已停止');

  const postsBefore = posts.length;
  router.ingest('resp-4', pcmChunk(), { sampleRate: 24000 });  // 迟到的尾巴
  router.end('resp-4');                                        // 迟到的 done
  await settle();
  assert.equal(posts.length, postsBefore,
    '已取消轮次的迟到音频不得再上传（旧 response 插进新 response）—— avatar-sync 自己的 cancelledResponseIds 兜住 router 的复活缺陷');
  assert.equal(router.confirms.length, 1);

  // 同一 (response_id, reason) 同时到两次（VAD + 手动停止）只允许一次线上打断。
  const both = await Promise.all([
    AV.cancelAvatarTurn({ responseId: 'resp-5', reason: 'vad_barge_in' }),
    AV.cancelAvatarTurn({ responseId: 'resp-5', reason: 'vad_barge_in' }),
  ]);
  await settle();
  assert.equal(interrupts().length, 2, '上一轮的打断 + 这一轮的打断，各一次');
  assert.equal(both.filter(Boolean).length, 1, '两次同键取消只有一次真的走到线上');

  /* --- 7. 收尾：不泄漏、不静默 ------------------------------------------- */
  assert.equal(rtcBuilt, 1, '整个过程只有一条 PeerConnection');
  assert.equal(rtcClosed, 0, '没有会话被无谓关闭');
  assert.equal(AV.suspended, false);
  assert.equal(AV.diagnostics().pendingUploads.length, 0, '没有卡在队列里的上传');
  assert.equal(offers.length, 1, '全程一个会话，无 /offer 风暴');

  /* --- 8. 唯一还开着的一条：降级路径的状态条必须说实话（§十二）------------ */
  // :8010 收了音频但没在出画时，本轮已经交回本机发声；此时状态条若继续写
  // "数字人正在说话"，就是在谎报发声方。humanaudio_failed 路径会 setStatus
  // ('failed_audio_submit')，而 accepted_but_not_rendering 路径只 failTurn()。
  assert.notEqual(captionAfterHandback, '数字人正在说话',
    'accepted_but_not_rendering 之后状态条仍是「' + captionAfterHandback + '」：'
    + 'avatar-sync.js:681 的 failTurn 漏了 setStatus，与 :686 的 humanaudio_failed 路径不一致');


  // Hold the first POST; a second job must stay queued until ownership changes.
  armUploadGate();
  router.begin('lease-first');router.ingest('lease-first',pcmChunk(),{sampleRate:24000});router.end('lease-first');await settle(3);
  router.begin('lease-second');router.ingest('lease-second',pcmChunk(),{sampleRate:24000});router.end('lease-second');
  assert.ok(AV.diagnostics().pendingUploads.some(j=>j.responseId==='lease-second'));
  router.notify('fail_open',{responseId:'lease-second',reason:'avatar_no_receipt'});
  assert.ok(!AV.diagnostics().pendingUploads.some(j=>j.responseId==='lease-second'));
  releaseUploadGate();await settle();
  router.ingest('lease-second',pcmChunk(),{sampleRate:24000});router.end('lease-second');await settle();
  assert.ok(!posts.some(p=>p.response_id==='lease-second'),'handed-back queued turn must never upload, even after late delta/end');
  console.log('PASS: queued fallback relinquishes Avatar ownership permanently');
  console.log('PASS: audio reached only a valid MEDIA_READY session, and every non-render path reported back to the router');
})().catch((err) => {
  console.error('FAIL: avatar-sync 会话有效性契约未通过');
  console.error(err && err.stack ? err.stack : err);
  process.exitCode = 1;
});
