/* ============================================================================
 * avatar-sync.js — 数字人（LiveTalking :8010）前端同步适配层
 *
 * 开源版本说明（来源与授权）：
 *   本文件是面向开源发布的**独立实现**，依据本仓库有权使用的材料重新设计：
 *     · 行为契约来自本仓库自研测试 test_avatar_sync_session(_reuse).cjs；
 *     · sink 形状与事件来自 audio-router.js（Apache-2.0 派生）的接口文档；
 *     · 服务端线上形状来自 runtime/avatar/app/server/（LiveTalking，Apache-2.0）；
 *     · 原生舞台几何常量（right:-4%、maskFade=0.42 及渐变形状）来自
 *       avatar_layout.js 中独立记录的产品行为参数；
 *     · 朗读边界字段与调用时序来自 ui/reading.js 的调用契约，取值全部来自本模块
 *       亲眼看到的事实（/humanaudio 回执与 :8010 /is_speaking 的翻转）。
 *   不声明严格 clean-room：历史排障曾阅读旧实现。2026-10-12 的表达层审查结果为
 *   与锁定上游（f5b4840a…64ed8）精确 token 相似 3.29%、归一化 5.88%，无匹配函数体、
 *   无公共块，未检出来自上游的实质性逐字表达；来源细节见 provenance 目录。
 *   详见 provenance/AVATAR_SYNC_RESTORATION_AUDIT_20261012.md。
 *
 * 工作方式：
 *   1. body 级 #av-layer 全屏层承接 LiveTalking 的 WebRTC 音视频流（视频为唯一
 *      可听输出；#av-audio 仅作为遗留句柄保持静音，绝不启动第二个媒体时钟）；
 *   2. 本模块不建 WebSocket、不劫持 window.WebSocket —— 音频唯一入口是
 *      audio-router 注册进来的 avatar sink（router 持有每一轮的保留缓冲，
 *      本模块只负责"投给 :8010 并如实回报结果"）；
 *   3. MEDIA_READY（真的在出画）才允许接管音频；:8010 拒收 / 没画面都必须把
 *      该轮交回本机（reportAvatarFailure），绝不出现"8010 挂了 = 全场静音"；
 *   4. 会话 single-flight、逐页复用、唯一拆除路径（close → release，
 *      keepalive），pagehide 显式交回名额，退避重连只排一个定时器；
 *   5. 取消 = router.cancel + 恰好一次 /interrupt_talk，同键去重，
 *      已取消轮次的迟到音频永远不得复活上传。
 * ==========================================================================*/
(function (global) {
  'use strict';

  var win = (typeof window !== 'undefined') ? window : global;
  var doc = (typeof document !== 'undefined') ? document : null;
  if (!doc || typeof win.RTCPeerConnection !== 'function') return; // 非浏览器环境不启动

  /* ------------------------------------------------------------ 配置 */
  function defaultLivetalkingBase() {
    try {
      if (typeof location !== 'undefined' && location.hostname) {
        return location.protocol + '//' + location.hostname + ':8010';
      }
    } catch (_) { /* 沙箱/离线环境没有 location：留空，由调用方配置 */ }
    return '';
  }

  var DEFAULTS = {
    livetalkingBase: defaultLivetalkingBase(),
    sampleRate: 24000,          // 与 s2s 输出一致的 24kHz 单声道 s16le
    chunkMs: 0,                 // 0 = 整轮一个分段；>0 按该毫秒数切段投递
    debug: false,
    maskFade: 0.42,             // 左侧渐隐占人物盒宽度的比例
    reconnectBackoffPcMs: 3000, // pc failed/disconnected 后的退避
    reconnectBackoffConnectMs: 5000, // connect 失败后的退避
    uploadRetryMax: 2,          // 网络层失败的重试次数（段号不变，服务端靠幂等键去重）
    readingPollMs: 500,         // 朗读期间 /is_speaking 的观察周期（也是时间的确定性边界）
    iceGatheringTimeoutMs: 10000,
    iceConnectionTimeoutMs: 20000,
    offerTimeoutMs: 15000,
  };
  var cfg = {};
  var key;
  for (key in DEFAULTS) cfg[key] = DEFAULTS[key];
  try {
    var over = win.AVATAR_CONFIG;
    if (over) for (key in over) if (over[key] !== undefined) cfg[key] = over[key];
  } catch (_) { /* 配置读不到就用默认 */ }

  var STATUS = {
    standby: '数字人待命',
    waiting: '已接通，等待画面…',
    live: '数字人已连接',
    speaking: '数字人正在说话',
    stopped: '数字人已停止',
    fallback: '数字人未接收音频，已改用本机声音',
  };
  var SINK_AVATAR = 'avatar';

  /* ------------------------------------------------------------ 状态 */
  var layerEl = null;
  var videoEl = null;
  var audioEl = null;
  var statusEl = null;

  var ownedSid = null;            // 本页当前持有的 :8010 会话号（字符串）；无会话为 null
  var rawSessionId = null;        // 服务端 /offer 原样返回的会话号（保持原始类型）
  var currentPc = null;           // 当前 PeerConnection（一个页面只有一条）
  var connectionState = 'idle';
  var suspended = false;          // 换人挂起：不做自动重连，connect() 明确拒绝
  var pageGone = false;           // pagehide 之后不再重连
  var sinkRegistered = false;

  var connectPromise = null;      // single-flight：并发 connect 共享同一次 /offer
  var connectGeneration = 0;
  var reconnectTimer = null;      // 全页唯一一个待触发的重连定时器
  var lastNegotiation = null;

  // 完整 SDP 交换，不使用 trickle ICE。超时是失败，绝不发送半成品。
  function awaitPeerState(pc, events, ready, timeoutMs, phase) {
    timeoutMs = Math.min(60000, Math.max(1, Number(timeoutMs) || 10000));
    return new Promise(function (resolve, reject) {
      var timer;
      var finished = false;
      function finish(err) {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        events.forEach(function (event) { pc.removeEventListener(event, check); });
        if (err) reject(err); else resolve();
      }
      function check() {
        if (pc !== currentPc || suspended || pageGone
          || pc.connectionState === 'closed' || pc.connectionState === 'failed'
          || pc.iceConnectionState === 'failed') return finish(new Error(phase + ' aborted'));
        if (ready()) finish();
      }
      events.forEach(function (event) { pc.addEventListener(event, check); });
      timer = setTimeout(function () { finish(new Error(phase + ' timeout')); }, timeoutMs);
      check();
    });
  }

  var remoteVideoTrack = null;
  var remoteAudioTrack = null;
  var lastFrameTime = -1;
  var frameAdvancing = false;

  var turnBufs = new Map();       // responseId -> {chunks:[], bytes, sampleRate, nextSegment}
  var uploadQueue = [];           // 串行 lease 队列；排队中的任务在 diagnostics 可见
  var draining = false;
  var deadRids = new Set();       // 已取消 / fail_open 交回的轮次：迟到音频永不投递
  var interruptKeys = new Set();  // (response_id|reason) 去重，保证线上打断恰好一次

  function routerRef() {
    if (win.s2sAudioRouter) return win.s2sAudioRouter;
    try { if (typeof s2sAudioRouter !== 'undefined') return s2sAudioRouter; } catch (_) {}
    return null;
  }

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text;
  }

  /* ------------------------------------------------------------ DOM 注入
   * 只注入"舞台"：全屏层 + 原生几何 + 自带渐隐。transform / mask 的后续
   * 所有权属于 avatar_layout.js（默认态零覆盖，接管前与本文件的原生表现一致）。
   */
  var STAGE_CSS = [
    '#av-layer{position:fixed;top:0;left:0;right:0;bottom:0;overflow:hidden;',
    'pointer-events:none;z-index:0;background:transparent}',
    '#av-video{position:absolute;top:0;right:-4%;height:100%;width:auto;max-width:none;object-fit:cover;',
    '-webkit-mask-image:linear-gradient(90deg,rgba(0,0,0,0) 0%,rgba(0,0,0,.30) 18.9%,rgba(0,0,0,.85) 32.8%,#000 42%),',
    'linear-gradient(180deg,transparent 0%,#000 2%,#000 94%,transparent 100%);',
    '-webkit-mask-composite:source-in;',
    'mask-image:linear-gradient(90deg,rgba(0,0,0,0) 0%,rgba(0,0,0,.30) 18.9%,rgba(0,0,0,.85) 32.8%,#000 42%),',
    'linear-gradient(180deg,transparent 0%,#000 2%,#000 94%,transparent 100%);',
    'mask-composite:intersect}',
    '#av-audio{display:none}',
  ].join('');

  function ensureDom() {
    var style = doc.createElement('style');
    style.textContent = STAGE_CSS;
    (doc.head || doc.documentElement || doc.body).appendChild(style);

    layerEl = doc.getElementById('av-layer');
    if (!layerEl) {
      layerEl = doc.createElement('div');
      layerEl.id = 'av-layer';
      (doc.body || doc.documentElement).insertBefore(layerEl, doc.body ? doc.body.firstChild : null);
    }
    videoEl = doc.getElementById('av-video');
    if (!videoEl) {
      videoEl = doc.createElement('video');
      videoEl.id = 'av-video';
      layerEl.appendChild(videoEl);
    }
    audioEl = doc.getElementById('av-audio');
    if (!audioEl) {
      audioEl = doc.createElement('audio');
      audioEl.id = 'av-audio';
      layerEl.appendChild(audioEl);
    }
    // 唯一可听输出是视频元素；遗留 audio 句柄永远静音、永不自启媒体时钟。
    videoEl.muted = false;
    try { videoEl.autoplay = true; videoEl.playsInline = true; } catch (_) {}
    audioEl.muted = true;
    audioEl.autoplay = false;

    statusEl = doc.getElementById('gf-avatar-status');
  }

  /* ------------------------------------------------------------ 会话有效性 */
  function hasLiveSession() {
    return Boolean(currentPc && ownedSid && String(ownedSid) !== '0'
      && currentPc.connectionState === 'connected');
  }

  function mediaReady() {
    if (suspended || !hasLiveSession() || !videoEl || !videoEl.srcObject) return false;
    var vt = videoEl.srcObject.getVideoTracks ? videoEl.srcObject.getVideoTracks()[0] : null;
    if (!vt || vt.readyState !== 'live') return false;
    if (!(videoEl.videoWidth > 0 && videoEl.videoHeight > 0)) return false;
    return frameAdvancing;    // currentTime 不前进 = 一帧都没出
  }

  function onMediaTick() {
    if (videoEl && videoEl.srcObject) {
      var t = Number(videoEl.currentTime);
      var grew = isFinite(t) && t > lastFrameTime;
      frameAdvancing = grew;
      if (grew) lastFrameTime = t;
    } else {
      frameAdvancing = false;
    }
    if (connectionState === 'connected' && !suspended
      && (statusEl.textContent === STATUS.waiting) && mediaReady()) {
      setStatus(STATUS.live);
    }
  }

  /* ------------------------------------------------------------ 会话建立 */
  function connect() {
    if (suspended) {
      return Promise.reject(new Error('数字人已挂起（正在切换人物），暂不能建立会话'));
    }
    if (hasLiveSession()) return Promise.resolve(rawSessionId);   // 会话复用
    if (connectPromise) return connectPromise;                        // single-flight
    var gen = ++connectGeneration;
    connectPromise = doConnect(gen).finally(function () {
      if (gen === connectGeneration) connectPromise = null;
    });
    return connectPromise;
  }

  async function doConnect(gen) {
    lastNegotiation = { localType: null, remoteType: null, candidateCount: 0,
      gatheringState: 'new', iceState: 'new', peerState: 'new', error: null };
    var pc = new win.RTCPeerConnection();
    currentPc = pc;
    api.peerConnection = pc;
    connectionState = 'connecting';
    remoteVideoTrack = null;
    remoteAudioTrack = null;
    pc.addEventListener('track', function (evt) { onTrack(pc, evt); });
    pc.addEventListener('connectionstatechange', function () { onPcState(pc); });
    // 只收不发：显式声明两条 recvonly m-line（先视频后音频，与 :8010 的
    // 编解码偏好约定一致），否则 offer 是空 SDP，aiortc 无法协商。
    pc.addTransceiver('video', { direction: 'recvonly' });
    pc.addTransceiver('audio', { direction: 'recvonly' });

    try {
      var offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      // LiveTalking 约定：等 ICE gathering 完成再发 /offer（aiortc 是完整 ICE
      // 代理，不是 ice-lite —— offer 里没有候选它就永远停在 checking）。
      await awaitPeerState(pc, ['icegatheringstatechange', 'connectionstatechange'],
        function () { return pc.iceGatheringState === 'complete'; },
        cfg.iceGatheringTimeoutMs, 'ICE gathering');
      var local = pc.localDescription;
      if (!local || local.type !== 'offer' || !local.sdp) throw new Error('Invalid local offer');
      var candidates = local.sdp.match(/^a=candidate:.+$/gm) || [];
      lastNegotiation = { localType: local.type, candidateCount: candidates.length,
        gatheringState: pc.iceGatheringState, iceState: pc.iceConnectionState,
        peerState: pc.connectionState, error: null };
      if (!candidates.length) throw new Error('Complete offer has no ICE candidates');
      if (!/^m=audio /m.test(local.sdp) || !/^m=video /m.test(local.sdp)) {
        throw new Error('Offer requires audio and video');
      }
      var offerAbort = new AbortController();
      var offerTimer = setTimeout(function () { offerAbort.abort(); },
        Math.min(60000, Math.max(1, Number(cfg.offerTimeoutMs) || 15000)));
      var answer;
      try {
        var res = await fetch(cfg.livetalkingBase + '/offer', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sdp: local.sdp, type: local.type }),
          signal: offerAbort.signal,
        });
        if (!res.ok) throw new Error('offer HTTP ' + res.status);
        answer = await res.json();
      } catch (offerError) {
        if (offerAbort.signal.aborted) throw new Error('offer request timeout');
        throw offerError;
      } finally { clearTimeout(offerTimer); }
      if (answer && typeof answer.code === 'number' && answer.code !== 0 && !answer.sessionid) {
        throw new Error(answer.msg || 'offer rejected');
      }
      if (!answer || answer.sessionid === undefined || answer.sessionid === null
        || String(answer.sessionid) === '0') {
        throw new Error('offer returned no valid sessionid');
      }
      var sid = String(answer.sessionid);
      // 服务端已经记账：从这一刻起本页持有这个名额，任何失败都要走 retire 交回。
      ownedSid = sid;
      rawSessionId = answer.sessionid;
      if (gen !== connectGeneration || suspended || pageGone) {
        // 排队的旧尝试 / 换人打断：拿到的名额立即交回，不占用。
        await retire('connect_failed');
        throw new Error('connect superseded');
      }
      if (answer.type !== 'answer' || !answer.sdp) throw new Error('Invalid remote answer');
      lastNegotiation.remoteType = answer.type;
      await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
      await awaitPeerState(pc, ['connectionstatechange', 'iceconnectionstatechange'],
        function () { return pc.connectionState === 'connected'
          && (pc.iceConnectionState === 'connected' || pc.iceConnectionState === 'completed'); },
        cfg.iceConnectionTimeoutMs, 'ICE connection');
      lastNegotiation.iceState = pc.iceConnectionState;
      lastNegotiation.peerState = pc.connectionState;
      connectionState = 'connected';
      setStatus(mediaReady() ? STATUS.live : STATUS.waiting);
      return rawSessionId;   // /offer 原样交回（保持服务端给的类型）
    } catch (err) {
      lastNegotiation = Object.assign({}, lastNegotiation, {
        localType: pc.localDescription && pc.localDescription.type,
        remoteType: pc.remoteDescription && pc.remoteDescription.type,
        gatheringState: pc.iceGatheringState, iceState: pc.iceConnectionState,
        peerState: pc.connectionState, error: String(err && err.message || err),
      });
      if (win.console) win.console.error('Avatar negotiation failed', lastNegotiation);
      if (pc === currentPc) {
        currentPc = null;
        api.peerConnection = null;
        connectionState = 'failed';
        try { if (pc.connectionState !== 'closed') pc.close(); } catch (_) {}
        if (ownedSid) {
          var sid = ownedSid;
          ownedSid = null;
          releaseSession(sid, 'connect_failed');
        }
        scheduleReconnect(cfg.reconnectBackoffConnectMs);
      }
      throw err;
    }
  }

  function onTrack(pc, evt) {
    if (pc !== currentPc || !evt || !evt.track) return;
    if (evt.track.kind === 'video') remoteVideoTrack = evt.track;
    else if (evt.track.kind === 'audio') remoteAudioTrack = evt.track;
    if (!(remoteVideoTrack && remoteAudioTrack)) return;
    // 两条入站轨必须合成同一条流：音画共用一个媒体时钟，video 是唯一可听输出。
    var combined = new MediaStream();
    combined.addTrack(remoteVideoTrack);
    combined.addTrack(remoteAudioTrack);
    videoEl.srcObject = combined;
    audioEl.srcObject = combined;
    try {
      var p = videoEl.play();
      if (p && typeof p.catch === 'function') p.catch(function () {});
    } catch (_) { /* 自动播放被拦：等下一次用户手势 */ }
  }

  function onPcState(pc) {
    var st = pc.connectionState;
    if (pc === currentPc && st === 'connected') connectionState = 'connected';
    if (pc !== currentPc) return;                       // 旧连接的状态变化一律忽略
    if (st === 'failed' || st === 'disconnected' || st === 'closed') {
      connectionState = 'failed';
      void retire('pc_failed').then(function () {
        scheduleReconnect(cfg.reconnectBackoffPcMs);
      });
    }
  }

  /* ------------------------------------------------------------ 拆除与释放
   * 唯一拆除路径：close → 等到 closed → POST /api/session/release（keepalive）。
   * 服务端不必等 ICE 超时就能腾出名额；幂等，永不报错。
   */
  function releaseSession(sid, reason) {
    if (!sid || String(sid) === '0') return;
    try {
      void fetch(cfg.livetalkingBase + '/api/session/release', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionid: String(sid), reason: reason }),
        keepalive: true,                                  // pagehide 也要能带走
      }).catch(function () {});
    } catch (_) { /* 释放失败不致命：服务端看门狗仍会回收 */ }
  }

  async function retire(reason) {
    var sid = ownedSid;
    var pc = currentPc;
    abandonUnfinishedReading();           // 会话没了：未播完的段一律作废，重连不得复活
    currentPc = null;
    ownedSid = null;
    api.peerConnection = null;
    connectionState = 'idle';
    if (pc && typeof pc.close === 'function' && pc.connectionState !== 'closed') {
      try { pc.close(); } catch (_) {}
    }
    // close() 的 connectionstatechange 是排队任务：等到真的 closed 再 release，
    // 顺序契约（§七）——并且防止 close 的回调重入 onPcState 造成二次 release。
    var spins = 0;
    while (pc && pc.connectionState !== 'closed' && spins++ < 100) {
      await Promise.resolve();
    }
    if (sid && String(sid) !== '0') releaseSession(sid, reason);
    setStatus(STATUS.standby);
  }

  function scheduleReconnect(ms) {
    if (suspended || pageGone) return;
    if (reconnectTimer) return;               // 已有排队的重试：不叠加，不排风暴
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      if (!suspended && !pageGone && !hasLiveSession()) {
        connect().catch(function () {});
      }
    }, ms);
  }

  function disconnect() {
    suspended = true;                         // 换人挂起：connect() 明确拒绝
    // 朗读账本由 retire() 统一作废旧段（保留记录以便诊断），此处不再重复清理。
    return retire('avatar_switch');
  }

  function reconnect() {
    suspended = false;
    return connect();
  }

  function onPageHide() {
    pageGone = true;
    stopSpeakingPoll();                       // 页面走了就别再问了
    abandonUnfinishedReading();
    var sid = ownedSid;
    ownedSid = null;
    if (sid && String(sid) !== '0') {
      releaseSession(sid, 'pagehide');
    }
  }

  /* ------------------------------------------------------------ 音频交接
   * router 的 avatar sink：write 只攒缓冲；真正的投递在轮次 end（或 chunkMs
   * 阈值）之后，串行 lease，一至 :8010 一次一段。
   */
  var sink = {
    available: function () {
      return !suspended && hasLiveSession() && mediaReady();
    },
    write: function (samples, meta) { onSinkWrite(samples, meta); },
  };

  function markDead(rid) {
    deadRids.add(rid);
    if (deadRids.size > 500) {
      deadRids.delete(deadRids.values().next().value);
    }
    turnBufs.delete(rid);
    abandonReadingByResponseId(rid);      // 已取消的轮次不得再被算作朗读在播
    for (var i = uploadQueue.length - 1; i >= 0; i--) {
      if (uploadQueue[i].responseId === rid) uploadQueue.splice(i, 1);
    }
  }

  function onSinkWrite(samples, meta) {
    if (!samples || !samples.byteLength) return;
    var rid = meta && meta.responseId;
    if (!rid || deadRids.has(rid)) return;
    if (suspended || !hasLiveSession()) return;      // 没有有效会话就不收
    var sr = Number(meta && meta.sampleRate) || cfg.sampleRate || 24000;
    var buf = turnBufs.get(rid);
    if (!buf) {
      buf = { chunks: [], bytes: 0, sampleRate: sr, nextSegment: 0 };
      turnBufs.set(rid, buf);
    }
    buf.chunks.push(new Uint8Array(samples.buffer, samples.byteOffset, samples.byteLength));
    buf.bytes += samples.byteLength;
    if (cfg.chunkMs > 0) {
      var threshold = Math.max(1, Math.round(buf.sampleRate * cfg.chunkMs / 1000)) * 2;
      while (buf.bytes >= threshold) {
        var pcm = takeBytes(buf, threshold);
        enqueueJob(rid, buf.nextSegment++, pcm, buf.sampleRate);
      }
    }
  }

  function takeBytes(buf, count) {
    var out = new Uint8Array(count);
    var filled = 0;
    while (filled < count && buf.chunks.length) {
      var chunk = buf.chunks[0];
      var view = new Uint8Array(chunk.buffer, chunk.byteOffset, chunk.byteLength);
      var want = count - filled;
      if (view.byteLength <= want) {
        out.set(view, filled);
        filled += view.byteLength;
        buf.chunks.shift();
      } else {
        out.set(view.subarray(0, want), filled);
        buf.chunks[0] = view.subarray(want);   // 余下的字节留给下一段
        filled = count;
      }
    }
    buf.bytes -= filled;
    return out;
  }

  function onTurnEnd(payload) {
    var rid = payload && payload.responseId;
    if (!rid) return;
    var buf = turnBufs.get(rid);
    if (!buf) return;
    if (payload.sink && payload.sink !== SINK_AVATAR) { turnBufs.delete(rid); return; }
    if (deadRids.has(rid)) { turnBufs.delete(rid); return; }
    if (buf.bytes > 0) {
      var pcm = takeBytes(buf, buf.bytes);
      enqueueJob(rid, buf.nextSegment++, pcm, buf.sampleRate);
    }
    turnBufs.delete(rid);
  }

  /* ------------------------------------------------------------ 上传与回执 */
  function enqueueJob(rid, segmentIndex, pcm, sampleRate) {
    if (deadRids.has(rid)) return;
    uploadQueue.push({
      responseId: rid,
      segmentIndex: segmentIndex,
      pcm: pcm,
      bytes: pcm.byteLength,
      sampleRate: sampleRate,
      sessionid: String(ownedSid || '0'),   // 入队即固定归属，防会话更换后错投
    });
    void drainUploads();
  }

  async function drainUploads() {
    if (draining) return;                    // 串行 lease：同一时刻只有一段在飞
    draining = true;
    try {
      while (uploadQueue.length) {
        var job = uploadQueue[0];
        if (deadRids.has(job.responseId)) {
          removeQueued(job);
          continue;
        }
        var outPromise = postHumanaudio(job);
        // 请求此刻已在途（服务端已收到）：真的在出画，才敢声称"数字人正在说话"。
        // 落在下一拍，让 end 观察者先把状态条落回"已连接"。
        await Promise.resolve();
        if (mediaReady()) setStatus(STATUS.speaking);
        var out = await outPromise;
        removeQueued(job);                   // 在飞期间可能被取消/交回：只删自己这一条
        if (deadRids.has(job.responseId)) continue;   // 结果作废，不惊动 router
        if (out.ok) {
          markReadingInputConsumed(job.responseId);   // 输入已被服务端收下（=会上 queue）
          if (mediaReady()) {
            // 服务端收了 且 我们真的在出画，才允许回执"数字人承担了"。
            routerRef().confirmAvatar(job.responseId, job.bytes,
              { segmentIndex: job.segmentIndex, decision: out.decision || 'accept' });
          } else {
            // 收了但画面死了：本轮已经交回本机发声，状态条不许再谎报发声方。
            abandonReadingByResponseId(job.responseId);
            failTurn(job.responseId, 'avatar:accepted_but_not_rendering');
            setStatus(STATUS.live);
          }
        } else {
          abandonReadingByResponseId(job.responseId);
          failTurn(job.responseId, 'avatar:humanaudio_failed');
          setStatus(STATUS.fallback);
        }
      }
    } finally {
      draining = false;
    }
  }

  function removeQueued(job) {
    var idx = uploadQueue.indexOf(job);
    if (idx >= 0) uploadQueue.splice(idx, 1);
  }

  function failTurn(rid, reason) {
    var router = routerRef();
    if (router && typeof router.reportAvatarFailure === 'function') {
      router.reportAvatarFailure(rid, reason);
    }
  }

  async function postHumanaudio(job) {
    var attempts = Math.max(1, cfg.uploadRetryMax || 1);
    var lastMsg = '';
    for (var i = 0; i < attempts; i++) {
      try {
        var fd = new FormData();
        fd.append('sessionid', job.sessionid);
        fd.append('response_id', job.responseId);
        fd.append('segment_index', String(job.segmentIndex));  // 重试时段号不变
        fd.append('file', new Blob([makeWav(job.pcm, job.sampleRate)], { type: 'audio/wav' }), 'segment.wav');
        var res = await fetch(cfg.livetalkingBase + '/humanaudio', { method: 'POST', body: fd });
        if (!res.ok) throw new Error('humanaudio HTTP ' + res.status);
        var body = await res.json();
        if (body && body.code === 0) {
          var decision = body.data && body.data.segment_gate && body.data.segment_gate.decision;
          return { ok: true, decision: decision };
        }
        return { ok: false, msg: (body && body.msg) || 'rejected' };  // 服务端明确拒收：不重试
      } catch (err) {
        lastMsg = String((err && err.message) || err);
      }
    }
    return { ok: false, msg: lastMsg || 'upload failed' };
  }

  function makeWav(pcm, sampleRate) {
    var buf = new Uint8Array(44 + pcm.byteLength);
    var dv = new DataView(buf.buffer);
    writeTag(dv, 0, 'RIFF');
    dv.setUint32(4, 36 + pcm.byteLength, true);
    writeTag(dv, 8, 'WAVE');
    writeTag(dv, 12, 'fmt ');
    dv.setUint32(16, 16, true);
    dv.setUint16(20, 1, true);                       // PCM
    dv.setUint16(22, 1, true);                       // 单声道
    dv.setUint32(24, sampleRate, true);
    dv.setUint32(28, sampleRate * 2, true);          // byte rate
    dv.setUint16(32, 2, true);                       // block align
    dv.setUint16(34, 16, true);                      // bits per sample
    writeTag(dv, 36, 'data');
    dv.setUint32(40, pcm.byteLength, true);
    buf.set(pcm, 44);
    return buf;
  }

  function writeTag(dv, offset, s) {
    for (var i = 0; i < s.length; i++) dv.setUint8(offset + i, s.charCodeAt(i));
  }

  /* ------------------------------------------------------------ 取消与打断 */
  async function postInterrupt(sid, rid, reason) {
    try {
      await fetch(cfg.livetalkingBase + '/interrupt_talk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionid: sid, response_id: rid, reason: reason }),
      });
      return true;
    } catch (_) {
      return false;
    }
  }

  /**
   * 取消一轮：先告诉 router（它的保留缓冲与回执窗口在这里死掉），再向 :8010
   * 恰好发一次打断；同 (response_id, reason) 并发去重。之后这一轮的迟到
   * ingest / end 永远不得再上传。
   */
  async function cancelAvatarTurn(opts) {
    var rid = opts && opts.responseId;
    if (!rid) return false;
    var reason = (opts && opts.reason) || 'user_interrupt';
    var key = rid + '|' + reason;
    markDead(rid);
    if (interruptKeys.has(key)) return false;
    interruptKeys.add(key);
    if (interruptKeys.size > 500) interruptKeys.delete(interruptKeys.values().next().value);
    var router = routerRef();
    if (router && typeof router.cancel === 'function') {
      try { router.cancel(rid, reason); } catch (_) {}
    }
    var sid = String(ownedSid || '0');
    if (sid === '0') return false;
    var sent = await postInterrupt(sid, rid, reason);
    setStatus(STATUS.stopped);
    return sent;
  }

  function onRouterCancel(payload) {
    var rid = payload && payload.responseId;
    if (!rid) return;
    var reason = payload.reason || 'user_interrupt';
    markDead(rid);
    var key = rid + '|' + reason;
    if (interruptKeys.has(key)) return;              // cancelAvatarTurn 已发过
    interruptKeys.add(key);
    var sid = String(ownedSid || '0');
    if (sid === '0') return;
    void postInterrupt(sid, rid, reason);
    setStatus(STATUS.stopped);
  }

  function onRouterFailOpen(payload) {
    var rid = payload && payload.responseId;
    if (!rid) return;
    markDead(rid);   // 所有权永久交回本机：迟到的 delta/end 也不得再上传
  }

  /* ------------------------------------------------------------ sink 注册 */
  function registerAvatarSink() {
    var router = routerRef();
    if (!router) return false;
    if (sinkRegistered) return true;
    if (typeof router.hasSink === 'function' && router.hasSink(SINK_AVATAR)) {
      sinkRegistered = true;
      return true;
    }
    sinkRegistered = router.registerSink(SINK_AVATAR, sink) !== false;
    return sinkRegistered;
  }

  /* ------------------------------------------------------------ 朗读播放账本
   * 阅读模式专属：把 :8010 的播放真值按 readingKey 逐段排成一列事实，供
   * ui/reading.js 的流水线推进断点。只记录本地能亲眼看到的事实：
   *   inputConsumed = /humanaudio 已被服务端收下并回了 code 0（输入已被消费）；
   *   started / finished = 会话级 /is_speaking 的翻转，配合 FIFO 归属落到具体段。
   * 问不到就什么都不写 —— 没有"猜"出来的完成信号，宁可让上层按超时停下，
   * 也不谎报"这一段念完了"。
   * 只在确实存在活跃朗读段时才轮询，聊天模式下零网络开销。
   */
  var readingOrder = [];                  // readingKey 列表，按 expectReading 顺序
  var readingRecords = new Map();         // readingKey -> record
  var readingPending = [];                // 尚未绑定到 responseId 的 key（FIFO）
  var readingSpeakingKey = null;          // 当前正在出声的段
  var readingPollTimer = null;

  var READING_BIND_WINDOW_MS = 120000;    // 登记后多久还没等到音频就判定这一轮不属于它
  var READING_RETENTION_MS = 180000;      // 已播完/已作废的段在此之后移出账本
  var READING_MAX_RECORDS = 64;           // 账本上限：防长时间朗读无界增长
  function readingPollMs() {
    return Math.min(2000, Math.max(200, Number(cfg.readingPollMs) || 500));
  }


  function newReadingRecord(k) {
    return {
      key: k,
      order: readingOrder.length,
      createdAt: Date.now(),
      responseId: null,
      inputConsumed: false,
      inputEnd: null,
      started: false,
      start: null,
      finished: false,
      end: null,
      playoutDelayMs: null,
      clockUncertaintyMs: null,
      abandoned: false,
    };
  }

  function dropArrayItem(arr, value) {
    var i = arr.indexOf(value);
    if (i >= 0) arr.splice(i, 1);
  }

  function activeReadingRecords() {
    var out = [];
    for (var i = 0; i < readingOrder.length; i++) {
      var rec = readingRecords.get(readingOrder[i]);
      if (rec && !rec.abandoned && !rec.finished) out.push(rec);
    }
    return out;
  }

  function recordByResponseId(rid) {
    if (!rid) return null;
    var id = String(rid);
    for (var i = 0; i < readingOrder.length; i++) {
      var rec = readingRecords.get(readingOrder[i]);
      if (rec && rec.responseId === id) return rec;
    }
    return null;
  }

  /** 回收：已播完/已作废且过了保留窗的段移出账本；总数超限时优先丢最老的终态段。 */
  function pruneReading() {
    var now = Date.now();
    var i, k, rec;
    for (i = readingOrder.length - 1; i >= 0; i--) {
      k = readingOrder[i];
      rec = readingRecords.get(k);
      if (!rec) { readingOrder.splice(i, 1); dropArrayItem(readingPending, k); continue; }
      if (!(rec.finished || rec.abandoned)) continue;
      var stamp = rec.end || rec.createdAt;
      if (now - stamp <= READING_RETENTION_MS) continue;
      readingRecords.delete(k);
      dropArrayItem(readingPending, k);
      readingOrder.splice(i, 1);
    }
    // 上限：先丢最老的已终段落，全都没终态时丢最老的（极罕见，宁丢记载不丢新事实）
    for (var pass = 0; pass < 2 && readingOrder.length > READING_MAX_RECORDS; pass++) {
      var strict = pass === 0;
      for (i = 0; i < readingOrder.length && readingOrder.length > READING_MAX_RECORDS; i++) {
        k = readingOrder[i];
        rec = readingRecords.get(k);
        if (!rec) { readingOrder.splice(i--, 1); continue; }
        if (strict && !(rec.finished || rec.abandoned)) continue;
        readingRecords.delete(k);
        dropArrayItem(readingPending, k);
        readingOrder.splice(i--, 1);
      }
    }
  }

  function startSpeakingPoll() {
    if (readingPollTimer || pageGone) return;
    readingPollTimer = setInterval(function () { void pollSpeaking(); }, readingPollMs());
  }

  function stopSpeakingPoll() {
    if (!readingPollTimer) return;
    clearInterval(readingPollTimer);
    readingPollTimer = null;
  }

  function stopSpeakingPollIfIdle() {
    if (!readingPollTimer) return;
    if (!activeReadingRecords().length) stopSpeakingPoll();
  }

  /** @param {string} reason 记录为什么作废：cancel / failure / session_retired 等 */
  function abandonReadingRecord(rec, reason) {
    if (!rec) return;
    if (rec.abandoned || rec.finished) return;
    rec.abandoned = true;
    if (readingSpeakingKey === rec.key) readingSpeakingKey = null;
    void reason;
  }

  function abandonReadingByResponseId(rid) {
    var rec = recordByResponseId(rid);
    abandonReadingRecord(rec, 'dead_turn');
  }

  /** 会话没了/退了：所有还没念完的段一律作废，重连后不得复活。 */
  function abandonUnfinishedReading() {
    for (var i = 0; i < readingOrder.length; i++) {
      var rec = readingRecords.get(readingOrder[i]);
      if (rec && !rec.finished) abandonReadingRecord(rec, 'session_retired');
    }
    readingSpeakingKey = null;
    stopSpeakingPoll();
  }

  /**
   * 登记一个等待播放的朗读段。key 由调用方构造，是本套事实的唯一关联键。
   * @param {string} key
   */
  function expectReading(key) {
    if (!key) return undefined;
    var k = String(key);
    if (!readingRecords.has(k)) {
      readingRecords.set(k, newReadingRecord(k));
      readingOrder.push(k);
      readingPending.push(k);
    }
    startSpeakingPoll();
    return k;
  }

  /**
   * 作废朗读登记：传 key 只作废旧段，不传则整体清空（切模式/换人走这个）。
   * 连同尚未投递的上传一起丢弃，避免"停止之后旧音频继续播报"。
   */
  function forgetReading(key) {
    var keys = key ? [String(key)] : readingOrder.slice();
    for (var i = 0; i < keys.length; i++) {
      var rec = readingRecords.get(keys[i]);
      if (!rec) continue;
      if (rec.responseId) markDead(rec.responseId);   // 连带掐掉在队列里的旧段
      rec.abandoned = true;
      readingRecords.delete(keys[i]);
      dropArrayItem(readingOrder, keys[i]);
      dropArrayItem(readingPending, keys[i]);
      if (readingSpeakingKey === keys[i]) readingSpeakingKey = null;
    }
    stopSpeakingPollIfIdle();
    return true;
  }

  /**
   * 停止朗读播出：掐掉正在出声的一段，并作废所有尚未播完的段与其在途上传。
   * 之后登记的新段不受影响（下一次 expectReading 会重新开轮询）。
   * @returns {Promise<boolean>}
   */
  async function interruptReading(opts) {
    var reason = (opts && opts.reason) || 'reading_interrupt';
    var targets = [];
    if (opts && opts.responseId) targets.push(String(opts.responseId));
    for (var i = 0; i < readingOrder.length; i++) {
      var rec = readingRecords.get(readingOrder[i]);
      if (!rec || rec.finished || rec.abandoned || !rec.responseId) continue;
      if (targets.indexOf(rec.responseId) < 0) targets.push(rec.responseId);
    }
    for (var j = 0; j < targets.length; j++) {
      await cancelAvatarTurn({ responseId: targets[j], reason: reason });
    }
    for (var m = 0; m < readingOrder.length; m++) {
      abandonReadingRecord(readingRecords.get(readingOrder[m]), reason);
    }
    readingPending.length = 0;
    readingSpeakingKey = null;
    stopSpeakingPoll();
    return true;
  }

  /** router.begin：把最早一个未绑定的段挂到这一轮音频上（FIFO = 服务端处理顺序）。 */
  function onRouterBegin(payload) {
    var rid = payload && payload.responseId;
    if (!rid) return;
    if (payload.sink && payload.sink !== SINK_AVATAR) return;   // 本地播的轮次不占朗读位
    while (readingPending.length) {
      var k = readingPending.shift();
      var rec = readingRecords.get(k);
      if (!rec) continue;
      if (Date.now() - rec.createdAt > READING_BIND_WINDOW_MS) {
        abandonReadingRecord(rec, 'bind_stale');   // 太久没等来音频：不算阅读串扰的借口
        continue;
      }
      rec.responseId = String(rid);
      return;
    }
  }

  function markReadingInputConsumed(rid) {
    var rec = recordByResponseId(rid);
    if (!rec || rec.inputConsumed) return;
    rec.inputConsumed = true;
    rec.inputEnd = Date.now();
  }

  function pollSpeaking() {
    if (pageGone || suspended) return Promise.resolve();
    var sid = String(ownedSid || '0');
    if (sid === '0' || !mediaReady()) return Promise.resolve();   // 没会话/没出画：不敢推断播放
    return fetch(cfg.livetalkingBase + '/is_speaking', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sessionid: sid }),
    }).then(function (res) {
      if (!res.ok) return null;
      return res.json();
    }).then(function (body) {
      if (body && body.code === 0) applySpeakingObservation(Boolean(body.data));
    })['catch'](function () { /* 问不到就什么都不写，下一次再问 */ });
  }

  /**
   * 一次会话级观察：说话开始时挂在"最早一个已投进去但还没出声"的段上，
   * 说话结束时把它标记为播完。这是这台机器能看到的全部粒度，因此
   * clockUncertaintyMs 如实写上轮询周期。
   */
  function applySpeakingObservation(speaking) {
    var i, rec;
    if (speaking) {
      for (i = 0; i < readingOrder.length; i++) {
        rec = readingRecords.get(readingOrder[i]);
        if (!rec || rec.abandoned || rec.finished) continue;
        if (rec.inputConsumed && !rec.started) {
          rec.started = true;
          rec.start = Date.now();
          rec.playoutDelayMs = Math.max(0, rec.start - (rec.inputEnd || rec.start));
          rec.clockUncertaintyMs = readingPollMs();
          readingSpeakingKey = rec.key;
          return;
        }
      }
      return;   // 会话在说话，但不属于任何已登记的朗读段（聊天轮次）：不记账
    }
    if (!readingSpeakingKey) return;
    rec = readingRecords.get(readingSpeakingKey);
    readingSpeakingKey = null;
    if (!rec || !rec.started || rec.finished) return;
    rec.finished = true;
    rec.end = Date.now();
    rec.playoutDelayMs = Math.max(0, rec.end - (rec.start || rec.end));
    rec.clockUncertaintyMs = readingPollMs();
  }

  /** @returns {Array<object>} 逐段播放边界；任何情况下都不抛异常。 */
  function readingBoundaries() {
    var out = [];
    try {
      pruneReading();
      for (var i = 0; i < readingOrder.length; i++) {
        var rec = readingRecords.get(readingOrder[i]);
        if (!rec) continue;
        out.push({
          key: rec.key,
          responseId: rec.responseId,
          inputConsumed: rec.inputConsumed,
          inputEnd: rec.inputEnd,
          started: rec.started,
          start: rec.start,
          finished: rec.finished,
          end: rec.end,
          playoutDelayMs: rec.playoutDelayMs,
          clockUncertaintyMs: rec.clockUncertaintyMs,
          abandoned: rec.abandoned,
        });
      }
    } catch (_) { /* 读不出来就返回已攒出来的部分，绝不把异常抛给流水线 */ }
    return out;
  }

  async function readingPlayback() {
    return readingBoundaries();
  }

  /* ------------------------------------------------------------ 对外 API */
  var api = {
    config: cfg,
    registerAvatarSink: registerAvatarSink,
    connect: function () { return connect(); },
    reconnect: function () { return reconnect(); },
    disconnect: function () { return disconnect(); },
    cancelAvatarTurn: function (opts) { return cancelAvatarTurn(opts); },
    expectReading: function (key) { return expectReading(key); },
    forgetReading: function (key) { return forgetReading(key); },
    readingPlayback: function () { return readingPlayback(); },
    interrupt: function (opts) { return interruptReading(opts); },
    mediaReady: function () { return mediaReady(); },
    diagnostics: function () {
      return {
        sessionid: String(ownedSid || '0'),
        connectionState: connectionState,
        suspended: suspended,
        mediaReady: mediaReady(),
        pendingUploads: uploadQueue.map(function (j) {
          return {
            responseId: j.responseId,
            segmentIndex: j.segmentIndex,
            bytes: j.bytes,
            sessionid: j.sessionid,
          };
        }),
        deadResponseIds: deadRids.size,
        negotiation: lastNegotiation,
        reading: readingBoundaries(),
      };
    },
  };
  Object.defineProperty(api, 'sessionid', {
    get: function () { return String(ownedSid || '0'); },
  });
  Object.defineProperty(api, 'connectionState', {
    get: function () { return connectionState; },
  });
  Object.defineProperty(api, 'suspended', {
    get: function () { return suspended; },
  });
  Object.defineProperty(api, 'peerConnection', {
    get: function () { return currentPc; },
    set: function (pc) { currentPc = pc; },
  });

  /* ------------------------------------------------------------ 事件接线 */
  function wireRouterEvents() {
    var router = routerRef();
    if (!router || typeof router.on !== 'function' || wired) return;
    wired = true;
    router.on('begin', onRouterBegin);
    router.on('end', onTurnEnd);
    router.on('cancel', onRouterCancel);
    router.on('fail_open', onRouterFailOpen);
  }
  var wired = false;

  /* ------------------------------------------------------------ boot */
  ensureDom();
  setStatus(STATUS.standby);
  registerAvatarSink();
  wireRouterEvents();
  setInterval(onMediaTick, 1000);          // 唯一的媒体打点：1Hz 判定 MEDIA_READY
  doc.addEventListener('click', function () {
    wireRouterEvents();
    registerAvatarSink();
    if (connectionState === 'connected' || connectPromise || suspended || pageGone) return;
    connect().catch(function () {});
  });
  win.addEventListener('pagehide', onPageHide);
  win.addEventListener('beforeunload', onPageHide);

  win.AvatarSync = api;
})(typeof window !== 'undefined' ? window : this);
