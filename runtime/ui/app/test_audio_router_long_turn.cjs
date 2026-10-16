'use strict';
/* ============================================================================
 * test_audio_router_long_turn.cjs — AI_GIRLFRIEND_LONG_AUDIO_TRUNCATION_ROOT_CAUSE_FINAL_01
 *
 * 三档长度确定性复现（任务书 §9）：把"已知总字节"的模拟长回复逐 delta 喂进
 * 真·audio-router.js，对账 avatar sink 实收字节，用 [[TAIL_AUDIO_7F39]] 尾块
 * 断言"最后一句是否活着"。
 *
 * 判据（任务书 §11）：
 *   FULL_TEXT_TAIL / TTS_SEGMENT_TAIL 在 router 层的对应物 =
 *   "最后一个 delta 到达 avatar sink"。
 *   修复前（MAX_TURN_BYTES=30s）：MEDIUM/LONG 档尾块必须丢失 —— 复现生产截断；
 *   修复后：三档 avatar 实收字节 == 全部喂入字节，TAIL 块必须存活。
 *
 * 运行：node runtime/ui/app/test_audio_router_long_turn.cjs [path-to-audio-router.js]
 * ==========================================================================*/
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROUTER_FILE = process.argv[2] || path.join(__dirname, 'audio-router.js');
const SOURCE = fs.readFileSync(path.resolve(ROUTER_FILE), 'utf8');

const CLOCK_BASE = 1000;
let fakeNow = CLOCK_BASE;
const sandbox = {
  setTimeout, clearTimeout, setInterval, clearInterval, console,
  performance: { now: () => fakeNow },
};
vm.runInNewContext(SOURCE, sandbox);

const SHARED = sandbox.s2sAudioRouter;
const SINK_LOCAL = SHARED.SINK_LOCAL;
const SINK_AVATAR = SHARED.SINK_AVATAR;

const SAMPLE_RATE = 24000;
const BYTES_PER_SEC = SAMPLE_RATE * 2;          // s16le mono
const CHUNK_SAMPLES = 2048;                     // 与 8765 真实 batch 粒度同量级
const CHUNK_BYTES = CHUNK_SAMPLES * 2;

/** 一档回复 = { name, seconds, tailLabel }，delta 总字节 = seconds*48000。 */
function feedTurn(router, sinks, { name, seconds }) {
  const totalBytes = Math.round(seconds * BYTES_PER_SEC / CHUNK_BYTES) * CHUNK_BYTES;
  const id = 'resp_' + name + '_' + seconds + 's';
  const DUMMY = new Int16Array(CHUNK_SAMPLES).fill(7);
  router.begin(id, {});
  for (let sent = 0; sent < totalBytes; sent += CHUNK_BYTES) {
    router.ingest(id, DUMMY, { responseId: id, sampleRate: SAMPLE_RATE });
    fakeNow += 20;   // 每个 delta 间隔 20ms，模拟流式到达
  }
  // 尾块：字节填充值 0x7F39 —— [[TAIL_AUDIO_7F39]] 的 router 层对应物。
  const TAIL = new Int16Array(CHUNK_SAMPLES).fill(0x7F39 & 0x7fff);
  const tailStartsAt = sinks.avatar.writes.length;
  router.ingest(id, TAIL, { responseId: id, sampleRate: SAMPLE_RATE });
  router.end(id, {});
  const got = sinks.avatar.bytes + sinks.local.bytes;
  const tailAlive = sinks.avatar.writes.length > tailStartsAt
    || sinks.local.writes.some(w => w.bytes === CHUNK_BYTES);
  return { id, totalBytes: totalBytes + CHUNK_BYTES, got, tailAlive, avatarBytes: sinks.avatar.bytes };
}

function makeSinks() {
  const local = {
    writes: [], bytes: 0,
    available: () => true,
    write(samples, meta) {
      if (meta && meta.clear && !samples) return;
      this.bytes += samples ? samples.byteLength : 0;
      this.writes.push({ bytes: samples ? samples.byteLength : 0 });
    },
    endTurn() {},
  };
  const avatar = {
    writes: [], bytes: 0,
    available: () => true,
    write(samples) { this.bytes += samples ? samples.byteLength : 0; this.writes.push({}); },
    endTurn() {},
  };
  return { local, avatar };
}

function newRouter() {
  const router = new sandbox.S2sAudioRouter();
  const sinks = makeSinks();
  router.registerSink(SINK_LOCAL, sinks.local);
  router.registerSink(SINK_AVATAR, sinks.avatar);
  return { router, sinks };
}

/* 三档（任务书 §9）：按 qwen3 实测 ~5.6 字/秒换算
 *   SHORT ≈  300 字 →  55s（跨过旧 30s 上限）
 *   MEDIUM ≈ 1000 字 → 180s
 *   LONG  ≈ 3000 字 → 540s
 * （SHORT 档同场对照：<30s 的短回复在任何版本都必须完整。） */
const CASES = [
  { name: 'SHORT_300ZI',  seconds: 55 },
  { name: 'MEDIUM_1000ZI', seconds: 180 },
  { name: 'LONG_3000ZI',  seconds: 540 },
];

/* 版本判定：只认"旧 30s 硬钳位"这一缺陷指纹，不认当前 guard 的字面量。
 * FINAL_02 起 guard 由上游能力派生
 *   MAX_TURN_SECONDS = ceil(LLM_MAX_OUTPUT_TOKENS * CHARS_PER_TOKEN / TTS_CHARS_PER_SECOND)
 * 所以任何写死的 `MAX_TURN_BYTES = 24000 * 2 * <N>` 哨兵都会在数字变化时假红
 * （历史上就假红过一次：中间态 600s → 终态 3600s 派生式，令本测试对**已修复**的
 *  router 报 "PRE-FIX" + 3×FAIL，而它自己打印的 coverage=100%/tail=PRESENT 才是事实）。 */
function guardSeconds(src) {
  const num = (name) => {
    const m = new RegExp(name + '\\s*=\\s*([0-9.]+)').exec(src);
    return m ? Number(m[1]) : NaN;
  };
  const tok = num('LLM_MAX_OUTPUT_TOKENS');
  const cpt = num('CHARS_PER_TOKEN');
  const cps = num('TTS_CHARS_PER_SECOND');
  if (tok > 0 && cpt > 0 && cps > 0) return Math.ceil(tok * cpt / cps);
  const literal = /MAX_TURN_BYTES\s*=\s*24000\s*\*\s*2\s*\*\s*(\d+)/.exec(src);
  return literal ? Number(literal[1]) : NaN;
}

const GUARD_S = guardSeconds(SOURCE);
const fixed = Number.isFinite(GUARD_S) && GUARD_S > 30;

/* ---------------------------------------------------------------------------
 * 快照分类（2026-09-26 用户门禁，权威文本 = 结案报告 §10）
 * 本测试允许被指向**历史备份件**（正向对照 / 旧口径快照）。那些件不是生产件：
 * 它们红了**不代表** audio-router.js 有问题，绝不能据此回滚。所以每次运行都先把
 * 归属打在输出第一行，让"红色 exit 1"自带解释，而不靠人去翻文档。
 * 生产验收集合 = 无参运行（指向 audio-router.js 本身）。
 * -------------------------------------------------------------------------= */
function artifactClass(file) {
  const base = path.basename(file);
  if (/\.bak_longaudio02_precomment$/.test(base)) return {
    cls: 'HISTORICAL_PRECOMMENT_SNAPSHOT',
    expect: 'FAIL_OLD_WORDING_ASSERTIONS（4 条口径断言红，行为全 PASS）',
    prod: 'NO',
    note: '旧措辞快照：exit 1 是有意结果，勿判"验收件坏了"、勿回滚 audio-router.js',
  };
  if (/\.bak_longaudio01$/.test(base)) return {
    cls: 'HISTORICAL_PREFIX_CONTROL',
    expect: 'PASS_REPRODUCING_30S_TRUNCATION（三档 avatarGot=29.95s / tailAudio7F39=LOST）',
    prod: 'NO',
    note: '正向对照：故意跑修复前旧件，证明测试不空转',
  };
  if (/\.bak/.test(base)) return {
    cls: 'HISTORICAL_BACKUP_SNAPSHOT（未登记）',
    expect: 'UNCLASSIFIED —— 先查结案报告 §10 再据结果下判断',
    prod: 'NO',
    note: '任何 .bak 快照都不在生产验收集合内',
  };
  return { cls: 'PRODUCTION', expect: 'ALL_PASS', prod: 'YES', note: '无参运行 = 当前生产验收' };
}
const ARTIFACT = artifactClass(ROUTER_FILE);

console.log(`artifact class: ${ARTIFACT.cls}   PRODUCTION_GATE=${ARTIFACT.prod}   ${ARTIFACT.note}`);
console.log(`expected result: ${ARTIFACT.expect}`);
console.log(`router file: ${ROUTER_FILE}`);
console.log(`guard: ${Number.isFinite(GUARD_S) ? GUARD_S + 's' : 'UNKNOWN（哨兵失配，别照抄本行结论）'}`
  + (fixed ? ' = runaway safety guard（非"合法最大时长"，见 audio-router.js 注释）' : ''));
console.log(`expected behaviour: ${fixed ? 'FIXED (clamp removed)' : 'PRE-FIX (30s cap)'}\n`);

// 口径/文案类断言：只有 audio-router.js 的**日志措辞**变旧才会红；与截断行为无关。
// 判"修复丢了"只看行为类失败（behaviouralFailures），不看这一组。
const WORDING_ASSERTIONS = new Set([
  'console.error 每条越界 delta 各一次（绝不静默）',
  '首条日志含 SAFETY_GUARD_DURATION',
  '日志不再自称「系统合法上限」',
  '源码不再自称「系统合法上限」',
]);

let failures = 0;
let behaviouralFailures = 0;
for (const c of CASES) {
  const { router, sinks } = newRouter();
  const r = feedTurn(router, sinks, c);
  const coverage = r.got / r.totalBytes;
  const ok = fixed ? (r.got === r.totalBytes && r.tailAlive)
                   : (c.seconds <= 30 ? (r.got === r.totalBytes && r.tailAlive)
                                      : (r.got < r.totalBytes && !r.tailAlive));
  const tag = ok ? 'PASS' : 'FAIL';
  if (!ok) { failures++; behaviouralFailures++; }
  console.log(`[${tag}] ${c.name.padEnd(14)} fed=${(r.totalBytes / BYTES_PER_SEC).toFixed(1)}s`
    + ` avatarGot=${(r.avatarBytes / BYTES_PER_SEC).toFixed(2)}s`
    + ` coverage=${(coverage * 100).toFixed(1)}% tailAudio7F39=${r.tailAlive ? 'PRESENT' : 'LOST'}`);
}

/* ---------------------------------------------------------------------------
 * 第二段：guard 命中路径必须 fail-loud（LONG_AUDIO_TRUNCATION_FINAL_02 的可观测性）
 * 真实 guard 是 3600s ⇒ 走完要吃 172.8 MB，所以这里把 LLM_MAX_OUTPUT_TOKENS 换成 100
 * （→ 45s / 2.16 MB）跑**同一段代码路径**；真实秒数由上面的 guardSeconds() 单独断言。
 * -------------------------------------------------------------------------= */
function guardProbe() {
  const PROBE_TOKENS = 100;
  const re = /LLM_MAX_OUTPUT_TOKENS\s*=\s*[\d.]+/;
  if (!re.test(SOURCE)) return Promise.resolve({ checks: { '可缩小 guard（需 LLM_MAX_OUTPUT_TOKENS 常量）': false }, probeGuard: 0, kept: 0, state: undefined, err: '' });
  const src = SOURCE.replace(re, `LLM_MAX_OUTPUT_TOKENS = ${PROBE_TOKENS}`);
  if (src === SOURCE) return Promise.resolve({ checks: { 'guard 缩放替换未生效': false }, probeGuard: 0, kept: 0, state: undefined, err: '' });
  const probeGuard = Math.ceil(PROBE_TOKENS * 2.0 / 4.5);          // 45s
  const errors = [];
  const pbase = Date.now();
  const pbox = { setTimeout, clearTimeout, setInterval, clearInterval,
    // 收口走的是真实 setTimeout（grace），所以探针用**活钟**，不用手写 fakeNow
    performance: { now: () => Date.now() - pbase },
    console: { log() {}, warn() {}, error: (m) => errors.push(String(m)) } };
  vm.runInNewContext(src, pbox);
  const S = pbox.s2sAudioRouter;
  const router = new pbox.S2sAudioRouter();
  const written = { bytes: 0 };
  const mk = (take) => ({ available: () => true,
    write(s) { if (take) written.bytes += s ? s.byteLength : 0; }, endTurn() {} });
  router.registerSink(S.SINK_LOCAL, mk(false));
  router.registerSink(S.SINK_AVATAR, mk(true));
  const id = 'resp_guard_probe';
  router.begin(id, {});
  const chunk = new Int16Array(CHUNK_SAMPLES).fill(7);
  const limit = probeGuard * BYTES_PER_SEC;
  for (let i = 0; i < Math.ceil(limit / CHUNK_BYTES) + 10; i++) {
    router.ingest(id, chunk, { responseId: id, sampleRate: SAMPLE_RATE });
    if (router._turns.get(id) && router._turns.get(id).truncatedAtGuard) break;
  }
  const t = router._turns.get(id);
  const keptWhenFired = t.bytes;
  for (let i = 0; i < 5; i++) router.ingest(id, chunk, { responseId: id, sampleRate: SAMPLE_RATE });
  router.end(id, {});
  router.confirmAvatar(id, keptWhenFired, { ok: true });
  return new Promise((resolve) => setTimeout(() => {
    const d = router.diagnostics();
    const row = d.lastTurns.find(x => x.responseId === id) || {};
    resolve({
      probeGuard, kept: keptWhenFired, state: row.state, err: errors[0] || '',
      truncEvents: d.counters.guard_truncated_turns, errCount: errors.length,
      checks: {
        'guard 在推算上限处停手': keptWhenFired === Math.floor(limit / CHUNK_BYTES) * CHUNK_BYTES,
        '命中后继续 ingest 不再涨字节': t.bytes === keptWhenFired,
        'sink 实收 == guard 保留（无额外尾）': written.bytes === keptWhenFired,
        'truncatedAtGuard 标记为真': t.truncatedAtGuard === true,
        '最终状态 = AUDIO_TRUNCATED': row.state === 'AUDIO_TRUNCATED',
        // 计数语义（2026-09-26 实测登记）：guard_truncated_turns 数的是**越界 delta 条数**，
        // 不是轮数 —— 同一轮命中后每来一条 delta 都会再加一次、再各打一条 console.error。
        // 生产至今 0 命中，所以这条只在此断言"行为如登记"，别把它读成"每轮一次"。
        'guard_truncated_turns = 越界 delta 条数（事件数≠轮数）': d.counters.guard_truncated_turns === 6,
        'console.error 每条越界 delta 各一次（绝不静默）': errors.length === 6
          && errors.every(m => /SAFETY_GUARD_DURATION/.test(m)),
        '首条日志含 SAFETY_GUARD_DURATION': errors.length === 6 && /SAFETY_GUARD_DURATION/.test(errors[0]),
        '日志不再自称「系统合法上限」': errors.length === 6 && errors.every(m => !/系统合法上限/.test(m)),
        '源码不再自称「系统合法上限」': !/系统合法上限/.test(SOURCE),
      },
    });
  }, 300));
}

(async () => {
  if (fixed) {
    console.log('—— guard fail-loud 路径（把 guard 缩到 45s 跑同一段代码；不启服务、不占显存）——');
    const g = await guardProbe();
    const failed = [];
    for (const [k, v] of Object.entries(g.checks)) {
      if (!v) { failures++; failed.push(k); if (!WORDING_ASSERTIONS.has(k)) behaviouralFailures++; }
      console.log(`[${v ? 'PASS' : 'FAIL'}] ${k}`);
    }
    if (failed.length) {
      const w = failed.filter(k => WORDING_ASSERTIONS.has(k)).length;
      const b = failed.length - w;
      console.log(`  红的断言 = 口径/文案类 ${w} 条 + 行为类 ${b} 条`
        + (b === 0 ? '  <<< 行为零失败：这不是截断回归，是措辞门禁' : '  <<< 行为类失败，才是真回归'));
    }
    console.log(`  probe guard=${g.probeGuard}s  kept=${g.kept}B  final_state=${g.state}`
      + `  guard_truncated_turns=${g.truncEvents}（越界 delta 条数）  console.error=${g.errCount} 条`);
    console.log(`  真实 guard=${Number.isFinite(GUARD_S) ? GUARD_S + 's' : '?'}（runaway safety guard，非合法最大时长）`);
    console.log(`  文案: ${g.err}\n`);
  }
  let verdict;
  if (!fixed) {
    verdict = behaviouralFailures === 0
      ? 'PRE_FIX_TRUNCATION_REPRODUCED（这正是生产 30s 截断）'
      : 'PRE_FIX_PROBE_INCONCLUSIVE';
  } else if (failures === 0) {
    verdict = 'AFTER_FIX_ALL_TAILS_PRESERVED + GUARD_FAIL_LOUD_OK';
  } else if (behaviouralFailures === 0) {
    verdict = 'OLD_WORDING_ONLY（红的全是口径/文案断言，截断行为零失败）'
      + `  ARTIFACT_CLASS=${ARTIFACT.cls}  PRODUCTION_GATE=${ARTIFACT.prod}`
      + '  —— 不要据此回滚 audio-router.js，判据见结案报告 §10';
  } else {
    verdict = `FIX_INCOMPLETE（行为类失败 ${behaviouralFailures} 条 —— 只有这种才需要动手）`;
  }
  console.log('\nVERDICT: ' + verdict);
  process.exit(failures === 0 ? 0 : 1);          // 退出码语义未变：任何红仍非 0
})();
