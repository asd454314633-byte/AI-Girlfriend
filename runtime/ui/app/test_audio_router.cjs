'use strict';
/* ============================================================================
 * test_audio_router.cjs — runtime/ui/app/audio-router.js 的离线契约测试
 *
 * 被替换掉的故障形状：avatar-sync.js 劫持 window.WebSocket，用
 * stopImmediatePropagation 吞掉 TTS delta，只交给 :8010 发声 —— 8010 挂了
 * 就等于"整个系统静音"。本文件证明新契约：
 *   1. 音频源只有一个，路由层永远保留整轮可播字节（fail-open 时可重播）；
 *   2. 每一轮只有一个"可听" sink，avatar→local 只移交"未被确认"的尾巴；
 *   3. 数字人不可用/无回执/上传失败/抛错，都必须变成"本机有声"，而不是无声；
 *   4. cancel 之后不得有回声，且保留量有界。
 *
 * 手法：直接驱动 router（无 DOM、无 fetch、无 WebSocket），毫秒级完成。
 * 时间用假时钟（vm 里注入 performance.now），只有 §12"保留期有界"需要跨过
 * TURN_RETENTION_MS(120s) 才推进它；真实定时器只用于 grace 窗口本身。
 * ========================================================================== */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

/* 假时钟。基准必须非 0：Turn 用 `endedAt = 0` 当"未结束"哨兵，
 * 而 nowMs() 也用来写 endedAt —— 基准为 0 会让 end() 看起来没发生过。 */
const CLOCK_BASE = 1000;
let fakeNow = CLOCK_BASE;

const sandbox = {
  setTimeout, clearTimeout, setInterval, clearInterval, console,
  performance: { now: () => fakeNow },
};
vm.runInNewContext(fs.readFileSync(`${__dirname}/audio-router.js`, 'utf8'), sandbox);

const AudioRouter = sandbox.S2sAudioRouter;
const SHARED = sandbox.s2sAudioRouter;   // 页面里真正被使用的那个单例
const SINK_LOCAL = SHARED.SINK_LOCAL;    // 'local_browser'
const SINK_AVATAR = SHARED.SINK_AVATAR;  // 'avatar'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const pcm = (samples, fill = 7) => new Int16Array(samples).fill(fill);

/** 两个假 sink。local 侧的记账口径与 s2s-realtime-client.js 的 local sink 一致：
 *  `meta.clear && !samples` 就是"把已经排进 AudioWorklet 的缓冲清掉"。 */
function makeSinks(opts) {
  const local = {
    writes: [], bytes: 0, clears: [], endTurns: [],
    available: () => opts.localAvailable !== false,
    write(samples, meta) {
      if (meta && meta.clear && !samples) { this.clears.push(meta); return; }
      const size = samples ? samples.byteLength : 0;
      this.writes.push({ bytes: size, meta: meta || {} });
      this.bytes += size;
    },
    endTurn(meta) { this.endTurns.push(meta || {}); },
  };
  const avatar = {
    writes: [], bytes: 0,
    available: () => (typeof opts.avatarAvailable === 'function' ? opts.avatarAvailable() : true),
    write(samples, meta) {
      const size = samples ? samples.byteLength : 0;
      this.writes.push({ bytes: size, meta: meta || {} });
      this.bytes += size;
      if (opts.avatarThrows) throw new Error(':8010 upload exploded');
    },
  };
  return { local, avatar };
}

function newRouter(opts = {}) {
  const router = new AudioRouter();
  const sinks = makeSinks(opts);
  router.registerSink(SINK_LOCAL, sinks.local);
  if (opts.noAvatarSink !== true) router.registerSink(SINK_AVATAR, sinks.avatar);
  if (typeof opts.graceMs === 'number') router.graceMs = opts.graceMs;
  return { router, local: sinks.local, avatar: sinks.avatar };
}

const CHUNK = pcm(320);                  // 640 B = 320 个 s16le 采样（24kHz 约 13ms）
const CHUNK_BYTES = CHUNK.byteLength;    // 640

/* ---------------------------------------------------------------- 测试执行器 */
/* .cjs 是 CommonJS，顶层不能 await —— 用例先登记，再由 runAll() 顺序执行。 */
const cases = [];
const outcomes = [];
function test(name, fn) { cases.push({ name, fn }); }

let lastRouter = null;
function track(ctx) { lastRouter = ctx.router; return ctx; }

async function runOne({ name, fn }) {
  fakeNow = CLOCK_BASE;                  // 假时钟每条用例归零，§12 自己再推进
  try {
    await fn();
    outcomes.push(true);
    console.log(`ok   ${name}`);
  } catch (err) {
    outcomes.push(false);
    const detail = String(err && err.message || err).split('\n').join('\n     ');
    console.log(`FAIL ${name}\n     ${detail}`);
  } finally {
    // 丢掉本条用例留下的回执窗口定时器，别让它拖住退出或串台到下一条。
    if (lastRouter) { lastRouter.reset('teardown'); lastRouter = null; }
  }
}

async function runAll() {
  for (const one of cases) await runOne(one);
  const failed = outcomes.filter((ok) => !ok).length;
  console.log(`\n${outcomes.length - failed}/${outcomes.length} 条用例通过`);
  if (failed) {
    console.log(`FAIL: ${failed} 条 audio-router 契约未通过（见上方 FAIL 详情）`);
    process.exitCode = 1;
  } else {
    console.log('PASS: audio-router keeps one audible sink per turn and never goes silent');
  }
}

/* ============================================================================
 * 1 — 保留音频不变量：路由层必须始终握有整轮可播字节
 * ========================================================================== */
test('1 保留音频不变量：N 段注入后 router 仍握有全部 N 段', async () => {
  const { router, local, avatar } = track(newRouter());
  router.begin('r1');
  for (let i = 0; i < 4; i += 1) router.ingest('r1', pcm(320));

  const open = router.diagnostics().openTurns;
  assert.equal(open.length, 1, '只应有一个在途轮次');
  assert.equal(open[0].responseId, 'r1');
  assert.equal(open[0].sink, SINK_AVATAR, 'avatar 可用时本轮的可听 sink 必须是 avatar');
  assert.equal(open[0].bytes, 4 * CHUNK_BYTES, 'diagnostics().openTurns[0].bytes 必须等于全部 4 段');
  assert.equal(avatar.writes.length, 4, '每段都原样转给 avatar');

  // 迟到接管：此刻才失败，全部 4 段必须能被本地重播（保留的意义就在于此）。
  router.reportAvatarFailure('r1', '8010 down');
  assert.equal(local.writes.length, 1, '降级只移交一次');
  assert.equal(local.bytes, 4 * CHUNK_BYTES, '本地必须拿到 avatar 从未确认的全部字节');
});

/* ============================================================================
 * 2 — 每轮一个可听 sink
 * ========================================================================== */
test('2 每轮只有一个可听 sink（avatar 可用时本地零写入，不可用时只有本地）', async () => {
  // 2a avatar 可用 → 只有 avatar
  const a = track(newRouter());
  a.router.begin('r1');
  a.router.ingest('r1', CHUNK);
  a.router.ingest('r1', CHUNK);
  assert.equal(a.local.bytes, 0, 'avatar 可用时本地 sink 不得收到任何字节');
  assert.equal(a.local.writes.length, 0, 'local 侧 write 调用次数必须为 0');
  assert.equal(a.avatar.bytes, 2 * CHUNK_BYTES);
  assert.equal(a.router.diagnostics().counters.to_avatar, 1);
  assert.equal(a.router.diagnostics().counters.to_local, 0);

  // 2b 注册了 avatar 但 available() 为假 → 只有 local，avatar 零写入
  const b = track(newRouter({ avatarAvailable: () => false }));
  b.router.begin('r2');
  b.router.ingest('r2', CHUNK);
  assert.equal(b.avatar.writes.length, 0, 'avatar 不可用时不得喂它');
  assert.equal(b.local.bytes, CHUNK_BYTES, '不可用时整段必须本机播（fail-open）');
  assert.equal(b.router.diagnostics().openTurns[0].sink, SINK_LOCAL);

  // 2c 根本没注册 avatar sink → 同样只有 local
  const c = track(newRouter({ noAvatarSink: true }));
  c.router.begin('r3');
  c.router.ingest('r3', CHUNK);
  assert.equal(c.local.bytes, CHUNK_BYTES, '没有 avatar sink 时也必须有声');

  // 2d 关掉总开关 → 永远本地（A/B 与故障隔离用）
  const d = track(newRouter());
  d.router.policy.avatar_enabled = false;
  d.router.begin('r4');
  d.router.ingest('r4', CHUNK);
  assert.equal(d.avatar.writes.length, 0, 'avatar_enabled=false 时数字人不得拿到音频');
  assert.equal(d.local.bytes, CHUNK_BYTES);

  // 2e 一轮之内的唯一接管：sink 选定后 avatar 倒下 → 整轮补播本地，
  //    之后同轮的迟到 delta 只能进本地（已经交给过 avatar 但从未被确认的段
  //    在 :8010 侧还没落库，本地重播不算重复发声 —— 确认才算承担）。
  let up = true;
  const e = track(newRouter({ avatarAvailable: () => up }));
  e.router.begin('r5');
  e.router.ingest('r5', CHUNK);
  e.router.ingest('r5', CHUNK);
  up = false;
  e.router.ingest('r5', CHUNK);          // 触发 avatar_unavailable_mid_turn
  e.router.ingest('r5', CHUNK);
  assert.equal(e.avatar.writes.length, 2, '倒下之后 avatar 不得再收到任何一段');
  assert.equal(e.local.bytes, 4 * CHUNK_BYTES,
    `本地应恰好拿到 4 段共 ${4 * CHUNK_BYTES}B，实际 ${e.local.bytes}B —— `
    + 'fail-open 已补播整轮，ingest 又把当前这一段重播了一遍（同一字节被播两次）');
  assert.equal(e.router.diagnostics().counters.fail_open, 1, '每轮只允许一次降级');
  assert.equal(e.router.diagnostics().openTurns[0].sink, SINK_LOCAL);
});

/* ============================================================================
 * 3 — fail-open on avatar failure
 * ========================================================================== */
test('3 数字人失败即降级：本地恰好收到未被确认的字节，且 fail_open==1', async () => {
  const { router, local, avatar } = track(newRouter());
  router.begin('r1');
  for (let i = 0; i < 3; i += 1) router.ingest('r1', CHUNK);
  router.end('r1');
  assert.equal(router.diagnostics().openTurns[0].ended, true, 'end 之后轮次仍要保留（等回执）');
  assert.equal(local.bytes, 0, '回执窗口内本地还不该出声');

  assert.equal(router.reportAvatarFailure('r1', 'x'), true);
  assert.equal(avatar.writes.length, 3);
  assert.equal(local.writes.length, 1, '只移交一次');
  assert.equal(local.bytes, 3 * CHUNK_BYTES, '本地必须恰好拿到 avatar 没确认的字节');
  assert.equal(local.writes[0].meta.responseId, 'r1');
  assert.equal(router.diagnostics().counters.fail_open, 1);
  assert.equal(router.diagnostics().counters.double_audio_guard_hits, 0);

  // 重复报故障不得再播一遍
  assert.equal(router.reportAvatarFailure('r1', 'again'), false, '已降级的轮次二次报故障应为 no-op');
  assert.equal(local.bytes, 3 * CHUNK_BYTES, 'NO_DUPLICATE_AUDIO');
  assert.equal(router.diagnostics().counters.fail_open, 1);
});

/* ============================================================================
 * 4 — 部分回执后失败：只补播第二段
 * ========================================================================== */
test('4 部分回执后失败：本地只收到未被确认的第二段，字节数精确', async () => {
  const { router, local } = track(newRouter());
  router.begin('r1');
  router.ingest('r1', CHUNK);
  router.ingest('r1', CHUNK);
  router.end('r1');

  assert.equal(router.confirmAvatar('r1', CHUNK_BYTES), true, '第一段已在 :8010 落库');
  assert.equal(router.diagnostics().openTurns[0].confirmedBytes, CHUNK_BYTES);
  assert.equal(local.bytes, 0, '还有未确认尾巴时不该出声');

  router.reportAvatarFailure('r1', 'tail lost');
  assert.equal(local.writes.length, 1);
  assert.equal(local.bytes, CHUNK_BYTES, '只移交第二段：40B 尾巴，不重播第一段');
  assert.equal(router.diagnostics().counters.fail_open, 1);
  assert.equal(router.diagnostics().counters.double_audio_guard_hits, 0);

  router.end('r1');                      // 降级后再收尾，走本地分支
  assert.equal(router.diagnostics().openTurns.length, 0, '轮次应被结清');
});

/* ============================================================================
 * 5 — 回执路径 = 本地不出声
 * ========================================================================== */
test('5 全回执路径：本地零写入且轮次结清', async () => {
  const { router, local, avatar } = track(newRouter());
  router.begin('r1');
  router.ingest('r1', CHUNK);
  router.ingest('r1', CHUNK);
  router.end('r1');
  router.confirmAvatar('r1', CHUNK_BYTES);
  router.confirmAvatar('r1', CHUNK_BYTES);

  assert.equal(local.writes.length, 0, 'avatar 确认了一切，本地不得出声');
  assert.equal(local.bytes, 0);
  assert.equal(router.diagnostics().openTurns.length, 0, '全回执后轮次必须结清');
  assert.equal(router.diagnostics().counters.fail_open, 0);
  assert.equal(avatar.bytes, 2 * CHUNK_BYTES);
  assert.equal(router.diagnostics().activeResponseId, '');
});

/* ============================================================================
 * 6 — 有界 grace：无回执自动兜底，有回执永不兜底（真实定时器）
 * ========================================================================== */
test('6 有界 grace：无回执在窗口内自动本机播，全回执永不回落', async () => {
  const { router, local } = track(newRouter({ graceMs: 30 }));
  router.begin('r1');
  router.ingest('r1', CHUNK);
  router.ingest('r1', CHUNK);
  router.end('r1');                      //  armed 一个 30ms 的回执窗口
  assert.equal(local.bytes, 0, 'grace 内不该提前出声');

  await sleep(150);
  assert.equal(local.bytes, 2 * CHUNK_BYTES, '超过 grace 仍无回执 → 本机必须播（否则就是老故障）');
  assert.equal(router.diagnostics().counters.fail_open, 1);
  const reasons = router.diagnostics().recent.filter((d) => d.what === 'fail_open');
  assert.equal(reasons.length, 1);
  assert.match(reasons[0].reason, /avatar_no_receipt/);

  // 反例：拿到完整回执的轮次永远不得回落
  router.begin('r2');
  router.ingest('r2', CHUNK);
  router.end('r2');
  router.confirmAvatar('r2', CHUNK_BYTES);
  await sleep(150);
  assert.equal(local.bytes, 2 * CHUNK_BYTES, '已全回执的轮次不得在 grace 之后重复出声');
  assert.equal(router.diagnostics().counters.fail_open, 1, 'fail_open 仍只计第一轮');
  const stillOpen = router.diagnostics().openTurns.map((t) => t.responseId);
  assert.ok(!stillOpen.includes('r2'), '拿到完整回执的轮次必须结清，永不回落');
});

/* ============================================================================
 * 7 — cancel 必须掐掉兜底定时器，并向本地 sink 发 clear
 * ========================================================================== */
test('7 cancel 掐掉回落：等 2×grace 后本地仍无字节，且发过 {clear:true}', async () => {
  const { router, local } = track(newRouter({ graceMs: 30 }));
  router.begin('r1');
  router.ingest('r1', CHUNK);
  router.end('r1');                      // 回执窗口已开启
  router.cancel('r1', 'user_barge_in');

  assert.equal(local.clears.length, 1, 'cancel 必须向本地 sink 发一次 clear 写（停掉已排进 worklet 的缓冲）');
  assert.equal(local.clears[0].clear, true, 'clear 写的形状要和 client 的 local sink 约定一致');
  assert.equal(local.clears[0].responseId, 'r1');
  assert.equal(local.writes.length, 0, 'clear 不是音频写入');
  assert.equal(router.diagnostics().openTurns.length, 0, '取消即结清');
  assert.equal(router.diagnostics().activeResponseId, '');

  await sleep(150);                      // 2×grace 以上
  assert.equal(local.bytes, 0, '被取消的音频不得在 grace 之后从本机补播');
  assert.equal(local.writes.length, 0);
  assert.equal(router.diagnostics().counters.fail_open, 0, '取消不算降级，更不能算两次');
});

/* ============================================================================
 * 8 — 取消后的迟到 delta（预期暴露 router 侧的"轮次复活"缺陷）
 * ========================================================================== */
test('8 取消后同 id 的迟到 delta 必须被丢弃并计入 dropped_after_cancel', async () => {
  const { router, local, avatar } = track(newRouter({ graceMs: 30 }));
  router.begin('r1');
  router.ingest('r1', CHUNK);
  router.end('r1');
  router.cancel('r1', 'user_barge_in');
  const heardBefore = local.bytes + avatar.bytes;

  router.ingest('r1', pcm(320));         // 同一轮的迟到尾巴

  assert.equal(router.counters.dropped_after_cancel, 1,
    'dropped_after_cancel 应为 1，实际 ' + router.counters.dropped_after_cancel
    + '：cancel 已把 Turn 从表里删掉，计数器永不可达');
  assert.equal(router.diagnostics().openTurns.length, 0, '迟到的 delta 不得复活已取消的轮次');
  assert.equal(local.bytes + avatar.bytes, heardBefore, '已取消轮次的音频不得再被任何 sink 播出');
});

/* ============================================================================
 * 9 — 轮次顺序性：sink 在 begin 定，之后不重算；任何乱序都不抛错
 * ========================================================================== */
test('9 begin/end/confirm 乱序一律不抛错，且 begin 定的 sink 不被重算', async () => {
  const { router, local, avatar } = track(newRouter());

  // delta 先于 begin：ingest 自己补开轮次
  assert.doesNotThrow(() => router.ingest('rx', CHUNK));
  assert.equal(router.diagnostics().openTurns[0].sink, SINK_AVATAR);
  assert.equal(router.diagnostics().openTurns[0].bytes, CHUNK_BYTES);

  // begin 两次：幂等，不重置已记账字节
  const first = router.begin('rx');
  assert.equal(first, router.begin('rx'), '重复 begin 必须返回同一个 Turn');
  assert.equal(router.diagnostics().counters.turns, 1, '重复 created 不得再计一轮');
  assert.equal(router.diagnostics().openTurns[0].bytes, CHUNK_BYTES, '重复 begin 不得清零');

  // end 两次 + end 未知 id + confirm/failure/cancel 未知 id
  assert.doesNotThrow(() => { router.end('rx'); router.end('rx'); });
  assert.doesNotThrow(() => { router.end('nope'); router.confirmAvatar('nope', 10); });
  assert.doesNotThrow(() => { router.reportAvatarFailure('nope', 'x'); router.cancel('nothing-matches'); });
  assert.equal(router.confirmAvatar('nope', 10), false);
  assert.equal(router.reportAvatarFailure('nope', 'x'), false);

  // begin 定的 sink 不因后续决策而重算：avatar 一直"可用"，
  // 则 end / confirm / 再 ingest 都必须继续走 avatar，本地保持静默。
  router.begin('ry');
  router.ingest('ry', CHUNK);
  router.end('ry');
  router.confirmAvatar('ry', 0);
  router.ingest('ry', CHUNK);
  assert.equal(router.diagnostics().openTurns.find((t) => t.responseId === 'ry').sink, SINK_AVATAR);
  assert.equal(local.bytes, 0, '整条乱序链路上本地一次都不该出声');
  assert.equal(avatar.bytes, 3 * CHUNK_BYTES);

  // 空 id / 空采样 / 缺 write 的 sink
  assert.equal(router.begin(''), null, '空 responseId 不开轮次');
  assert.doesNotThrow(() => router.ingest('rz', new Int16Array(0)));
  assert.doesNotThrow(() => router.ingest('rz', null));
  assert.equal(router.diagnostics().openTurns.find((t) => t.responseId === 'rz'), undefined);
  assert.throws(() => router.registerSink('broken', {}), /write/);
});

/* ============================================================================
 * 10 — 匿名音频（无 id、无在途轮次）不得被静音
 * ========================================================================== */
test('10 匿名音频：ingest("", samples) 无在途轮次时必须本机播而不是丢弃', async () => {
  const { router, local, avatar } = track(newRouter());
  router.ingest('', CHUNK);
  assert.equal(local.bytes, CHUNK_BYTES, '缺 id 也不能静音：宁可本地播（fail-open）');
  assert.equal(local.writes[0].meta.anonymous, true, '匿名写入要带可辨识的 meta');
  assert.equal(avatar.bytes, 0, '匿名音频不该喂给数字人');
  assert.equal(router.diagnostics().openTurns.length, 0, '匿名音频不建立轮次');
});

/* ============================================================================
 * 11 — sink 抛错不得打断音频链路
 * ========================================================================== */
test('11 avatar sink 抛错：ingest 不外抛，且该轮仍能降级到本地', async () => {
  const { router, local } = track(newRouter({ avatarThrows: true, graceMs: 30 }));
  router.begin('r1');
  assert.doesNotThrow(() => router.ingest('r1', CHUNK));
  assert.doesNotThrow(() => router.ingest('r1', CHUNK));
  assert.equal(router.diagnostics().counters.turns, 1);
  const failed = router.diagnostics().recent.filter((d) => d.what === 'sink_write_failed');
  assert.equal(failed.length, 2, 'sink 抛错必须留痕');
  assert.equal(local.bytes, 0, '抛错本身不触发降级，但保留字节仍在');

  router.end('r1');
  await sleep(150);                       // 无回执 → grace 到点自动本机播
  assert.equal(local.bytes, 2 * CHUNK_BYTES, '抛掉的音频必须被 grace 兜回来');

  // 本地 sink 抛错同样不得外抛（路由层不能因为 sink 而崩）
  const b = track(newRouter());
  b.local.write = () => { throw new Error('worklet gone'); };
  b.router.policy.avatar_enabled = false;
  b.router.begin('r1');
  assert.doesNotThrow(() => b.router.ingest('r1', CHUNK));
});

/* ============================================================================
 * 12 — 保留有界；但回收未确认轮次不得变成静音
 * ========================================================================== */
test('12 保留有界：>MAX_TURNS 会回收，且回收不得吞掉未确认的音频', async () => {
  const { router, local, avatar } = track(newRouter());
  for (let i = 0; i < 10; i += 1) {
    const id = `old${i}`;
    router.begin(id);
    router.ingest(id, CHUNK);            // 全程 avatar 可用，一字节都没被确认
  }
  assert.equal(router.diagnostics().openTurns.length, 10, '未过期时不得回收');
  const heardBefore = local.bytes;

  fakeNow += 120001;                      // 越过 TURN_RETENTION_MS(120000)
  router.begin('fresh');
  router.ingest('fresh', CHUNK);

  const d = router.diagnostics();
  assert.equal(d.openTurns.length, 8, '并发轮次必须被压回 MAX_TURNS(8)');
  const pruned = d.recent.filter((x) => x.what === 'turn_pruned');
  assert.equal(pruned.length, 3, '每次回收都要留痕（old0、old1、old2 被压回 8 个）');
  assert.equal(pruned[0].bytes, CHUNK_BYTES, '被回收的轮次其实仍有 640B 未确认音频');

  // 上一条的"有界"是好事；这一条是它的代价：这些字节既没被 avatar 确认、
  // 也没被 grace 兜底（那两轮从未 end()，所以从未开启回执窗口），
  // 现在直接从保留缓冲里消失了 —— 违反 §九"router 永远保留该轮可播音频"。
  assert.ok(local.bytes > heardBefore,
    'AUDIO LOSS：被回收的未确认轮次必须像 fail-open 一样交给本地 sink，而不是静默丢弃');
  assert.equal(avatar.bytes, 11 * CHUNK_BYTES, '（对照）avatar 侧只收到过投递，从未确认');
});

/* ============================================================================
 * 13 — §十一 音频状态分级：TTS_READY → AUDIO_GENERATED → AUDIO_PLAYING → AUDIO_DONE
 *      并且"在发声"必须由 sink 的真实写入/回执决定，不看 TTS 有没有产出字节。
 * ========================================================================== */
test('13 音频状态分级按真实投递推进，且只发状态变化那几条事件', async () => {
  const { router, local } = track(newRouter({ avatarAvailable: () => false }));
  const seen = [];
  router.on('state', (d) => seen.push(`${d.state}@${d.sink}`));

  router.begin('s1');
  assert.equal(seen.join('|'), 'TTS_READY@local_browser', 'begin 只宣布就绪，不宣布有声');
  router.ingest('s1', CHUNK);
  assert.equal(seen.join('|'), 'TTS_READY@local_browser|AUDIO_GENERATED@local_browser|AUDIO_PLAYING@local_browser',
    '字节先算"已生成"，真的写进活 sink 才算"在播放"');
  router.ingest('s1', CHUNK);
  assert.equal(seen.length, 3, '同状态不得重复通知（不按 delta 打爆观察者）');
  router.end('s1');
  assert.equal(seen[seen.length - 1], 'AUDIO_DONE@local_browser', '收口必须是 DONE');
  assert.equal(local.bytes, 2 * CHUNK_BYTES, '（对照）本机确实拿到了整轮字节');
  assert.equal(router.diagnostics().counters.silent_turns, 0, '出声的轮次不得记成静音');
});

test('14 本机 sink 写失败 → AUDIO_SILENT 并计入 silent_turns，不谎报 DONE', async () => {
  const { router } = track(newRouter({ avatarAvailable: () => false }));
  // 顶掉 harness 的良性本机 sink，换成"活着但一写就炸"的 AudioWorklet。
  router.registerSink(SINK_LOCAL, { available: () => true, write() { throw new Error('worklet gone'); } });
  const seen = [];
  router.on('state', (d) => seen.push(d.state));
  router.begin('s2');
  router.ingest('s2', CHUNK);
  router.end('s2');
  assert.ok(!seen.includes('AUDIO_PLAYING'), '写入失败时不得谎称"正在播放"');
  assert.ok(seen.includes('AUDIO_GENERATED'), '字节确实生成过，只是没能出声');
  assert.equal(seen[seen.length - 1], 'AUDIO_SILENT', '这就是"本轮未收到语音输出"的唯一真源');
  assert.equal(router.diagnostics().counters.silent_turns, 1, '真静音必须被计数，不能只靠人耳发现');
});

/* ============================================================================
 * 15 — 收口账本：response.done 之后仍要能回答"这一轮到底是谁出的声音"
 * ========================================================================== */
test('15 turnAudibility 在轮次收口后仍可查：avatar 承担 vs 本机接管 vs 未发声', async () => {
  const { router } = track(newRouter());
  // (a) 全程由数字人承担：本机零写入，但这一轮确实是"有声"的。
  router.begin('a');
  router.ingest('a', CHUNK);
  router.end('a');
  router.confirmAvatar('a', CHUNK_BYTES);
  // 展开一份：router 的对象是 vm context 里造的，跨 realm 的 prototype 会让
  // deepStrictEqual 报"结构相同但不相等"，那不是我们要断言的东西。
  assert.deepEqual({ ...router.turnAudibility('a') },
    { audible: true, sink: 'avatar', state: 'AUDIO_DONE' },
    '数字人发声时本机本来就是静的，判据只能是回执，不能是本机 RMS');

  // (b) avatar 收了但没确认 → 本机接管，sink 必须翻成 local_browser。
  router.begin('b');
  router.ingest('b', CHUNK);
  router.reportAvatarFailure('b', 'accepted_but_not_rendering');
  router.end('b');
  router.confirmAvatar('b', CHUNK_BYTES);   // 迟到的回执不得改判已降级的轮次
  assert.equal(router.turnAudibility('b').audible, true, '降级后必须有声音');
  assert.equal(router.turnAudibility('b').sink, SINK_LOCAL, '声音来自本机');

  // (c) 取消的一轮不得算作"出声"。
  router.begin('c');
  router.ingest('c', CHUNK);
  router.cancel('c', 'vad_barge_in');
  assert.equal(router.turnAudibility('c').audible, false, '打断的轮次不贡献可听性');
  assert.equal(router.turnAudibility('c').state, 'AUDIO_CANCELLED');
  assert.equal(router.turnAudibility('nope'), null, '从没见过的 id 要回答"不知道"，不能假称有声');
});

/* ============================================================================
 * 16 — 跨文件一致性：路由层发出的状态名必须被面板逐字接住
 *      （chat.js 的 onAudioState 是纯文案映射，键名对不上就是静默不显示）
 * ========================================================================== */
test('16 音频状态名与 ui/chat.js 的映射表逐字对齐，且顺序单调', async () => {
  const routerSrc = fs.readFileSync(`${__dirname}/audio-router.js`, 'utf8');
  const chatSrc = fs.readFileSync(`${__dirname}/ui/chat.js`, 'utf8');
  const rankBlock = routerSrc.match(/const STATE_RANK = \{([\s\S]*?)\};/);
  assert.ok(rankBlock, 'audio-router.js 里必须能找到 STATE_RANK 定义');
  const emitted = [...rankBlock[1].matchAll(/^\s*([A-Z][A-Z_]+):\s*(\d+),/gm)]
    .map((m) => ({ state: m[1], rank: Number(m[2]) }));
  assert.ok(emitted.length >= 5, `状态表不该缩水（实际 ${emitted.length} 项）`);

  const mapBlock = chatSrc.match(/onAudioState\(detail\)[\s\S]*?const text = \{([\s\S]*?)\}\[detail\.state\];/);
  assert.ok(mapBlock, 'chat.js 的 onAudioState 必须保留可静态解析的映射表');
  const mapped = [...mapBlock[1].matchAll(/^\s*([A-Z][A-Z_]+):/gm)].map((m) => m[1]);

  assert.deepEqual(mapped.slice().sort(), emitted.map((e) => e.state).slice().sort(),
    '路由层能发的每一个状态，面板都必须有一句人话；多出来的键名一律算错');
  // 单调性：秩必须随"更晚的真相"递增，否则迟到的 delta 会把文案往回退。
  const order = ['TTS_READY', 'AUDIO_GENERATED', 'AUDIO_PLAYING', 'AUDIO_DONE'];
  const rankOf = (s) => emitted.find((e) => e.state === s).rank;
  for (let i = 1; i < order.length; i += 1) {
    assert.ok(rankOf(order[i]) > rankOf(order[i - 1]), `${order[i]} 的秩必须高于 ${order[i - 1]}`);
  }
  assert.ok(rankOf('AUDIO_DONE') === rankOf('AUDIO_SILENT'), 'DONE/SILENT 同为终态，互不覆盖');
});

/* -------------------------------------------------------------------- 汇总 */
runAll().catch((err) => {
  console.error('runner crashed:', err);
  process.exitCode = 1;
});
