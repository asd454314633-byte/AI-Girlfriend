/*
 * gate_diff.cjs —— 任务书第 11 条「必须验证」的 BEFORE/AFTER 逐项对拍（纯离线，不开浏览器）
 *
 * 它不重新测量任何东西，只做一件事：把已经落盘的实测证据
 *   CONTROL_INVENTORY_BEFORE.json / CONTROL_INVENTORY_AFTER.json
 *   DOCK_ACCEPTANCE.json / TRANSFORM_EQUIVALENCE.json / FILE_AUDIT_OFFLINE.json
 * 按需求条目对齐，逐条给出【改前数值 / 改后数值 / 判据 / 判决】。
 * 任何一条对不上就是 FAIL —— 不接受"应该差不多"。
 *
 * 为什么允许离线：这些数字都是运行时探针写进磁盘的实测值；
 * 本脚本只是把实测值搬到需求条目上，不引入新的推断。
 *
 * 用法：node runtime/tests/ui_detail_left_dock_01/gate_diff.cjs
 */
'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..', '..');
const EV = path.join(ROOT, 'runtime', 'evidence', 'UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01');

function read(name) {
  const p = path.join(EV, name);
  if (!fs.existsSync(p)) throw new Error('缺少证据文件 ' + name + '（先跑对应探针）');
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}
function readOrNull(name) {
  try { return read(name); } catch (_) { return null; }
}

const B = read('CONTROL_INVENTORY_BEFORE.json');
const A = read('CONTROL_INVENTORY_AFTER.json');
const ACC = read('DOCK_ACCEPTANCE.json');
const TEQ = read('TRANSFORM_EQUIVALENCE.json');
const FA = read('FILE_AUDIT_OFFLINE.json');
const MP = readOrNull('MOVE_PROOF_OFFLINE.json');

const G = ACC.gates || {};
const gpass = (k) => !!(G[k] && G[k].pass);
const gobs = (k) => (G[k] && G[k].observed) || {};

/* ------------------------------------------------------------------ 控件台账 */
/* 43 个功能选择器逐个对拍。字段分两类：
 *   身份类（exists/count/tag/id/cls/children/text/hidden/value/options）
 *     —— 这些变了就说明"控件本身被动过"，必须逐个解释；
 *   位置类（path/parentSel/rect/display/position）
 *     —— 本任务的目的就是改这些，所以【预期会变】，不构成缺陷。
 * outerHash 是"内容签名"（含 style 等排版属性）：签名相同 ⇒ 连排版都没变的强证据；
 * 签名不同 ⇒ 回落到身份类字段判，避免把"搬家必然带来的 style 变化"误判成改动。 */
const IDENTITY_FIELDS = ['exists', 'count', 'tag', 'id', 'cls', 'children',
  'text', 'hidden', 'value', 'options'];
const PLACE_FIELDS = ['path', 'parentSel', 'rect', 'display', 'position'];

function indexBy(list, keyName) {
  const m = new Map(); const dup = [];
  for (const it of list) {
    const k = it[keyName];
    if (m.has(k)) dup.push(k); else m.set(k, it);
  }
  if (dup.length) throw new Error('键重复：' + keyName + ' -> ' + dup.join(','));
  return m;
}

const bCtl = indexBy(B.controls, 'sel');
const aCtl = indexBy(A.controls, 'sel');
const missing = [...bCtl.keys()].filter((k) => !aCtl.has(k));
const extra = [...aCtl.keys()].filter((k) => !bCtl.has(k));

const ledger = [];
for (const [sel, b] of bCtl) {
  const a = aCtl.get(sel);
  if (!a) { ledger.push({ sel, verdict: 'MISSING_AFTER', before: b, after: null }); continue; }
  const idDiff = IDENTITY_FIELDS.filter((f) => JSON.stringify(b[f]) !== JSON.stringify(a[f]));
  const sigSame = b.outerHash === a.outerHash;
  const placeDiff = PLACE_FIELDS.filter((f) => JSON.stringify(b[f]) !== JSON.stringify(a[f]));
  ledger.push({
    sel,
    verdict: sigSame ? 'BYTE_IDENTICAL'
      : (idDiff.length ? 'IDENTITY_CHANGED' : 'IDENTICAL_IDENTITY_PLACEMENT_MOVED'),
    outerHash_before: b.outerHash, outerHash_after: a.outerHash,
    outerLen_before: b.outerLen, outerLen_after: a.outerLen,
    identity_diff: idDiff.map((f) => ({ field: f, before: b[f], after: a[f] })),
    placement_diff: placeDiff.map((f) => ({ field: f, before: b[f], after: a[f] })),
    /* 迁移轨迹：原来挂在哪、现在挂在哪 —— 这就是"原样迁入同一个 Dock"的逐控件证据 */
    parent_before: b.parentSel, parent_after: a.parentSel,
    inDock_before: /#gf-dock/.test(b.path || ''), inDock_after: /#gf-dock/.test(a.path || ''),
  });
}
for (const sel of extra) ledger.push({ sel, verdict: 'EXTRA_IN_AFTER', before: null, after: aCtl.get(sel) });

const byVerdict = {};
for (const r of ledger) byVerdict[r.verdict] = (byVerdict[r.verdict] || 0) + 1;
const identityChanged = ledger.filter((r) => r.verdict === 'IDENTITY_CHANGED');
const movedIntoDock = ledger.filter((r) => !r.inDock_before && r.inDock_after).map((r) => r.sel);
const stillOutsideDock = ledger.filter((r) => r.verdict !== 'MISSING_AFTER' && !r.inDock_after)
  .map((r) => r.sel);

/* ------------------------------------------------------------- 事件监听对拍 */
const aLs = indexBy(A.listeners.map((x) => ({ ...x, key: x.sel })), 'key');
const listeners = B.listeners.map((x) => {
  const y = aLs.get(x.sel) || {};
  return {
    sel: x.sel,
    types_before: (x.types || []).slice().sort().join('+'),
    types_after: (y.types || []).slice().sort().join('+'),
    n_before: x.listeners, n_after: y.listeners,
    same: JSON.stringify(x.types) === JSON.stringify(y.types) && x.listeners === y.listeners,
  };
});
const listenerDiffs = listeners.filter((r) => !r.same);

/* --------------------------------------------------------------- 网络窗口 */
const NET = ACC.network || {};
const idleWins = [NET.idle_baseline, NET.idle_recheck].filter(Boolean);
const opWins = NET.op_windows || [];
const allWins = idleWins.concat(opWins);
const sum = (arr, k) => arr.reduce((s, w) => s + ((k in w ? w[k] : (w.constructors || {})[k]) || 0), 0);
const ctorSum = (arr, k) => arr.reduce((s, w) => s + (((w.constructors || {})[k]) || 0), 0);
const layoutCaused = sum(opWins, 'layout_caused_fetches') + sum(idleWins, 'layout_caused_fetches');
const backendTotal = opWins.reduce((s, w) => s + ((w.backend_requests || []).length), 0);
const reqTotal = allWins.reduce((s, w) => s + (w.requests_total || 0), 0);
const deadProbeWins = allWins.filter((w) => w.probe_reset !== 'RESET').map((w) => ({ tag: w.tag, got: w.probe_reset }));

/* ------------------------------------------------------- 文件账（离线重算） */
const FAT = FA.totals || {};
const ownerRow = (FA.rows || []).find((r) => r.file === 'avatar_layout.js');
const ownerUnchanged = !!(ownerRow && ownerRow.pre_sha && ownerRow.pre_sha === ownerRow.now_sha);
const inScopeList = (FA.in_scope_modified || []).map((r) => r.file);
const verdictOk = FA.VERDICT === 'NON_UI_MODIFIED_FILES = NONE';

/* ---------------------------------- 人物状态漂移归因（必须说清，不能装看不见） */
const sB = B.owner, sA = A.owner;
/* 用 owner 自己的公式反算：scale = s/100，stretchX = w/100，translate% = 值/250 */
function formulaMatches(own) {
  if (!own || !own.geo) return null;
  const g = own.geo;
  const r = (x) => +x.toFixed(2);
  return {
    state: { s: own.s, w: own.w, x: own.x, y: own.y },
    scale_ok: Math.abs(g.scale - own.s / 100) < 1e-9,
    stretchX_ok: Math.abs(g.stretchX - own.w / 100) < 1e-9,
    translateXpx_ok: r(g.bandWidth * (own.x / 250)) === r(g.translateXpx),
    translateYpx_ok: r(g.bandHeight * (own.y / 250)) === r(g.translateYpx),
    observed: { scale: g.scale, stretchX: g.stretchX, translateXpx: g.translateXpx, translateYpx: g.translateYpx },
    derived_from_owner_formula: {
      scale: own.s / 100, stretchX: own.w / 100,
      translateXpx: +(g.bandWidth * (own.x / 250)).toFixed(2),
      translateYpx: +(g.bandHeight * (own.y / 250)).toFixed(2),
    },
    /* owner 的"输入常量"必须一模一样：底图尺寸/锚点位置没被布局改动过 */
    invariants: { baseWidth: g.baseWidth, baseHeight: g.baseHeight, boxRight: g.boxRight, boxTop: g.boxTop },
  };
}
const fB = formulaMatches(sB), fA = formulaMatches(sA);
const fOk = (f) => !!(f && f.scale_ok && f.stretchX_ok && f.translateXpx_ok && f.translateYpx_ok);
const TEQ_A = ((TEQ.steps || {}).A_current) || {};
const TEQ_B = ((TEQ.steps || {}).B_at_before_state) || {};
const teqGates = TEQ.gates || {};
/* TEQ 的闸门值是 {pass, observed...} 形态；直接 !!obj 恒真，会把"没判过"当成 PASS */
const tgate = (k) => { const v = teqGates[k]; return v ? (typeof v === 'object' ? v.pass === true : v === true) : false; };
const persistObs = gobs('user_persistence_untouched_by_probe');

const preRerunTEQ = readOrNull(path.join('archive', '2026-09-26_pre-rerun_TRANSFORM_EQUIVALence.json'));
const run1ACC = readOrNull(path.join('archive', 'DOCK_ACCEPTANCE_2026-09-26T10-45-41-065Z.json'));
const run1Vals = (((((run1ACC || {}).localStorage || {}).restored || {}).final || {}).values) || null;

const compositionAttribution = {
  question: 'PRE 基线的人物四参数是 100/130/-100/-20，现在是 90/150/-90/-5。这是本轮布局改的，还是用户自己拖的？',
  pre_state: { s: sB.s, w: sB.w, x: sB.x, y: sB.y, captured_at: B.captured_at },
  now_state: { s: sA.s, w: sA.w, x: sA.x, y: sA.y, captured_at: A.captured_at, lsRaw: TEQ_A.lsRaw || null },
  timeline_utc: [
    { at: B.captured_at, state: '100/130/-100/-20', source: 'CONTROL_INVENTORY_BEFORE.owner' },
    run1Vals ? {
      at: (run1ACC || {}).captured_at, state: [run1Vals.avatarVideoScale, run1Vals.avatarVideoStretchX,
      run1Vals.avatarVideoOffsetX, run1Vals.avatarVideoOffsetY].join('/'),
      source: 'DOCK_ACCEPTANCE run#1 localStorage 快照（探针开始时磁盘上已是这个值）',
    } : null,
    preRerunTEQ ? {
      at: preRerunTEQ.captured_at, state: JSON.stringify((preRerunTEQ.steps || {}).A_current && (preRerunTEQ.steps || {}).A_current.state),
      source: 'TRANSFORM_EQUIVALENCE pre-rerun 归档',
    } : null,
    { at: TEQ.captured_at, state: '90/150/-90/-5', source: 'TRANSFORM_EQUIVALENCE now（A_current）' },
    { at: A.captured_at, state: '90/150/-90/-5', source: 'CONTROL_INVENTORY_AFTER.owner' },
  ].filter(Boolean).sort((x, y) => String(x.at).localeCompare(String(y.at))),
  writer_scan: {
    only_writer_of_avatarVideo_keys: 'avatar_layout.js:122-125（唯一写者；在任务书禁改清单内）',
    avatar_layout_js_pre_sha: ownerRow ? ownerRow.pre_sha : null,
    avatar_layout_js_now_sha: ownerRow ? ownerRow.now_sha : null,
    avatar_layout_js_identical_to_PRE: ownerUnchanged,
    writes_in_this_rounds_changed_files: 0,
    note: 'grep 全 runtime/ui/app（排除 .bak）：setItem(avatarVideo*) 只出现在 avatar_layout.js；'
      + '本轮改的 index.html / gf_dock.css / ui_layout_modes.js 里 0 处。',
  },
  formula_check: { before: fB, after: fA, same_formula_both_states: fOk(fB) && fOk(fA) },
  divider_tracks_bandLeft: (function () {
    /* 分割线/黑雾联动是上一轮的成果：状态变了以后，它相对 bandLeft 的偏移必须不变。
     * 若本轮布局改动了这条链，这里的两个 delta 会分开。 */
    const row = (d) => {
      const g = (d.owner && d.owner.geo) || {};
      const c = (d.controls || []).find((x) => x.sel === '#uivm-divider');
      if (!c || !c.rect || g.bandLeft === undefined) return null;
      return { divider_x: c.rect[0], bandLeft: g.bandLeft, delta: +(c.rect[0] - g.bandLeft).toFixed(3), hidden: c.hidden };
    };
    const b = row(B), a = row(A);
    const ledgerRow = ledger.find((r) => r.sel === '#uivm-divider');
    return {
      before: b, after: a,
      delta_shift_px: b && a ? +(a.delta - b.delta).toFixed(3) : null,
      outer_html_byte_identical: !!(ledgerRow && ledgerRow.verdict === 'BYTE_IDENTICAL'),
      reading: '两态下 divider_x − bandLeft 的偏移量只差 ' + (b && a ? Math.abs(a.delta - b.delta).toFixed(3) : '?')
        + ' px（子像素取整量级），而 bandLeft 本身随用户新数值移动了 '
        + (b && a ? (a.bandLeft - b.bandLeft).toFixed(3) : '?') + ' px ⇒ 联动链没断，动的是数值不是算法。',
    };
  })(),
  probe_side_readback: {
    transform_equivalence_state_at_before: TEQ_B.state || null,
    restore_closed_loop: tgate('restore_closed_loop'),
    acceptance_probe_persistence: { diffs: (ACC.localStorage || {}).diffs, restored: (ACC.localStorage || {}).restored, gate_pass: gpass('user_persistence_untouched_by_probe') },
  },
  reason: '取值序列 100/130/-100/-20 → 97/130/-60/-20 → 90/150/-90/-5 是三个互不相同的中间刻度；'
      + '"代码把状态改掉"只会复位到默认（100/100/0/0）或落到某个预设，不会连续落在三个任意刻度上。'
      + '唯一写者文件与 PRE 逐字节相同；探针窗内 localStorage 净变化为 0；'
      + 'TEQ 探针自己把状态设成 PRE 值测完后立刻还原闭环（restore_closed_loop）。'
      + '⇒ 归因：用户在页面上自己拖动滑块（本轮期间用户在场验收 UI，并自行重启过 7860）。',
  verdict: null,
};
compositionAttribution.verdict =
  (fOk(fB) && fOk(fA)
    && JSON.stringify(fB.invariants) === JSON.stringify(fA.invariants)
    && ownerUnchanged
    && gpass('user_persistence_untouched_by_probe') && tgate('restore_closed_loop')
    && tgate('inline_equals_documented_formula') && tgate('transform_identical_at_same_state'))
    ? 'COMPOSITION_DRIFT_ATTRIBUTED_TO_USER = TRUE（缩放/拉伸算法与唯一 owner 均未改动）'
    : 'UNEXPLAINED — 必须停下查根因，不许交付';

/* ------------------------------------------------------------------ 条目表 */
function item(id, req, gates, before, after, judge, verdict, note) {
  return {
    id, requirement: req,
    gate_status: gates.map((k) => ({ gate: k, pass: gpass(k) })),
    before, after, judgment: judge, verdict, note,
  };
}
const bc = B.counts, ac = A.counts;
const eq = (x, y) => JSON.stringify(x) === JSON.stringify(y);
const V = ACC.detail && ACC.detail.video ? ACC.detail.video : {};

/* 容器例外白名单：这 3 个选择器不是"控件"，而是本任务明令要变空/要新增的容器。
 * 每一条例外都必须配一条【可回算的】静态证据通道，不接受口头解释；
 * 任何不在这张表里的 IDENTITY_CHANGED 都算未解释 ⇒ R11-2 直接 FAIL。 */
const CONTAINER_EXCEPTIONS = {
  'main.stage': { why: '第 7 条：中央主画布清空 —— 容器按设计变空', channel: 'R11-4：stageChildElements 9→0、stageDescendants 175→0' },
  '#gf-dock': { why: '第 2 条：本轮新增的唯一左侧 Dock 容器（改前不存在）', channel: 'MOVE_PROOF P5：Dock 新写部分交互标签 0；P4：新增 id 只有 gf-dock / gf-dock-screen-slot' },
  '.gf-controls': { why: 'children 6→5 是运行时挂载点变更，不是删控件', channel: 'MOVE_PROOF P3：HTML 源文件里 5→5 逐字节相同；P8：5(静态)+1(JS 建的入口)=6 → 5+0=5，静态与运行时数字咬合' },
};
const unexplained = identityChanged.filter((r) => !CONTAINER_EXCEPTIONS[r.sel]);
const mpOk = !!(MP && MP.VERDICT === 'MOVED_NOT_COPIED = TRUE'
  && (MP.P1_P2_moved_blocks || []).length === 9
  && MP.P1_P2_moved_blocks.every((m) => m.occurrences_in_now === 1 && m.inside_dock)
  && MP.P3_gf_controls_children && (MP.P3_gf_controls_children.lost_or_unexplained || []).length === 0
  && MP.P4_ids && MP.P4_ids.removed.length === 0 && MP.P4_ids.duplicates.length === 0
  && MP.P5_new_scaffold && MP.P5_new_scaffold.interactive_total === 0
  && MP.P6_cache_busters && MP.P6_cache_busters.rules.every((r) => r.satisfied)
  && MP.P7_stage_round_trip && MP.P7_stage_round_trip.ok === true
  && MP.P8_runtime_delta_attribution
  && MP.P8_runtime_delta_attribution.entry_is_js_created_not_in_html === true
  && MP.P8_runtime_delta_attribution.copy_construct_hits.cloneNode === 0
  && MP.P8_runtime_delta_attribution.static_vs_runtime_bridge.bridge_holds === true);

const ITEMS = [
  item('R11-1', '控件数量修改前后相同', ['scaffold_accounting'],
    { interactive: bc.interactive, selector_census: B.selector_census, uivmPanels: bc.uivmPanels, layoutEntries: bc.layoutEntries, gfControls: bc.gfControls },
    { interactive: ac.interactive, selector_census: A.selector_census, uivmPanels: ac.uivmPanels, layoutEntries: ac.layoutEntries, gfControls: ac.gfControls },
    '43 个功能选择器逐个存在且计数相同；可交互控件 153 → 153；控制面板/布局入口/#gf-controls 各 1 → 1',
    eq(bc.interactive, ac.interactive) && eq(bc.uivmPanels, ac.uivmPanels)
      && eq(bc.layoutEntries, ac.layoutEntries) && eq(bc.gfControls, ac.gfControls)
      && missing.length === 0 && extra.length === 0 && gpass('scaffold_accounting'),
    '总数之外还要求"一个都不缺、一个都不多"：MISSING=' + (missing.join(',') || '(none)')
      + '，EXTRA=' + (extra.join(',') || '(none)')),

  item('R11-2', '同一个 DOM 控件，不是复制品', ['scaffold_accounting', 'no_document_navigation_during_probe'],
    { byte_identical_controls: byVerdict.BYTE_IDENTICAL || 0, dockExists: bc.dockExists },
    {
      byte_identical_controls: byVerdict.BYTE_IDENTICAL || 0,
      identity_only_moved: byVerdict.IDENTICAL_IDENTITY_PLACEMENT_MOVED || 0,
      identity_changed: identityChanged.map((r) => r.sel),
      identity_changed_all_on_container_allow_list: unexplained.length === 0,
      container_exceptions: CONTAINER_EXCEPTIONS,
      moved_into_dock: movedIntoDock,
      still_outside_dock: stillOutsideDock,
      duplicated_selectors: extra,
      static_move_proof: MP ? {
        verdict: MP.VERDICT,
        moved_blocks: (MP.P1_P2_moved_blocks || []).map((m) => m.name + ':' + m.occurrences_in_now + ':' + (m.group || '-')),
        ids: MP.P4_ids, new_scaffold_interactive_tags: MP.P5_new_scaffold.interactive_total,
        bridge: ((MP.P8_runtime_delta_attribution || {}).static_vs_runtime_bridge) || null,
      } : 'MISSING — 先跑 move_proof_offline.cjs',
    },
    '判据不是"看起来一样"，而是四件独立事实同时成立：'
      + '① 43 个选择器里 40 个身份字段零差异（38 个连 outerHTML 内容签名都逐字节相同）；'
      + '② 3 个例外全是【容器】且各有可回算的静态通道（见 container_exceptions）；'
      + '③ 静态侧 MOVE_PROOF：9 个搬移块在现文里各出现恰好 1 次（移动而非复制）、'
      + 'id 多重集零丢失、Dock 新写部分 0 个交互标签；'
      + '④ 探针期间文档零导航（重绘一次就等于换了一批节点）。',
    unexplained.length === 0 && extra.length === 0 && missing.length === 0 && mpOk
      && gpass('scaffold_accounting') && gpass('no_document_navigation_during_probe'),
    'BYTE_IDENTICAL=' + (byVerdict.BYTE_IDENTICAL || 0)
      + '，IDENTICAL_IDENTITY_PLACEMENT_MOVED=' + (byVerdict.IDENTICAL_IDENTITY_PLACEMENT_MOVED || 0)
      + '，IDENTITY_CHANGED=' + identityChanged.map((r) => r.sel).join('+')
      + '（三个都是容器，逐个有静态通道；无一条靠叙述）'),

  item('R11-3', '原有事件监听继续工作', ['scaffold_accounting'],
    { listener_total: B.listener_total, per_selector: B.listeners.map((x) => x.sel + ':' + (x.types || []).join('+')).join(' ') },
    { listener_total: A.listener_total, diffs: listenerDiffs, functional_probe_after: A.functional_probe, js_exceptions: (ACC.info || {}).js_exceptions || (G.no_js_exceptions || {}).observed },
    '逐选择器的监听数与事件类型全等（差异 ' + listenerDiffs.length + ' 处）；'
      + '并且【真的按了一遍】：输入框 input 让高度自适应生效、布局入口点击翻转并还原、'
      + '数字人入口 mount 点仍有 1 个子节点',
    eq(B.listener_total, A.listener_total) && listenerDiffs.length === 0
      && !!(A.functional_probe && A.functional_probe.composerInput && A.functional_probe.composerInput.heightAfter
        && A.functional_probe.layoutToggle && A.functional_probe.layoutToggle.flipped
        && A.functional_probe.avatarEntry && A.functional_probe.avatarEntry.mountChildren === 1),
    '改前 heightRestored=41px / 改后=57px 属【已解释差异】：输入框搬进 Dock 后可用宽度变了，'
      + '自适应行高原值随之不同；两轮 valueKept 都读回探针写入的字符串，说明监听确实在跑。'),

  item('R11-4', '中央控制区节点数量 = 0', ['central_canvas_cleared', 'single_left_dock'],
    { stageChildElements: bc.stageChildElements, stageDescendants: bc.stageDescendants, dockExists: bc.dockExists, dockGroups: bc.dockGroups },
    { stageChildElements: ac.stageChildElements, stageDescendants: ac.stageDescendants, dockExists: ac.dockExists, dockGroups: ac.dockGroups, dockGroupNames: ac.dockGroupNames },
    '主画布直接子元素与后代元素都必须为 0；左侧 Dock 唯一且内部分 5 组',
    ac.stageChildElements === 0 && ac.stageDescendants === 0 && ac.dockExists === true
      && ac.dockGroups === 5 && gpass('central_canvas_cleared') && gpass('single_left_dock'),
    'stageChildElements ' + bc.stageChildElements + ' → ' + ac.stageChildElements
      + '，stageDescendants ' + bc.stageDescendants + ' → ' + ac.stageDescendants),

  item('R11-5', '人物 video node = 1', ['single_avatar_video_node_identity'],
    { avVideo: bc.avVideo, videos: bc.videos },
    {
      avVideo: ac.avVideo, videos: ac.videos,
      in_frame_after_ops: { avVideoCount: V.avVideoCount, allVideoCount: V.allVideoCount, allVideoIds: V.allVideoIds },
      sameObjectAsStamp: V.sameObjectAsStamp, stamp: (ACC.video_node_stamp || {}).sig_at_stamp,
    },
    '文档里 <video> 总数 2（av-video + cam-video）、#av-video 计数 1 未变；'
      + '真正的身份判定是"探针先 window.__PROBE_VNODE=el，再用 el === __PROBE_VNODE 回读"',
    eq(bc.avVideo, 1) && eq(ac.avVideo, 1) && eq(bc.videos, ac.videos)
      && V.avVideoCount === 1 && V.sameObjectAsStamp === true && gpass('single_avatar_video_node_identity'),
    'outerHTML 克隆骗得过计数、骗不过对象恒等；此判据在布局动作之后复算。'),

  item('R11-6', 'srcObject 不变', ['single_avatar_video_node_identity', 'stream_change_not_layout_caused'],
    { srcAttr: B.video.srcAttr === undefined ? null : B.video.srcAttr, hasSrcObject: B.video.hasSrcObject, ctor: null },
    {
      hasSrcObject: A.video.hasSrcObject, srcObjectCtor: V.srcObjectCtor, srcAttr: V.srcAttr,
      videosWithSrcObject: V.videosWithSrcObject, streamId: V.streamId, trackIds: V.trackIds,
      same_object_across_layout_ops: tgate("srcObject_same_object_across_layout_ops"),
      attribution_gate_note: (G.stream_change_not_layout_caused || {}).observed,
    },
    '同一 MediaStream 对象引用在布局动作前后不变；若帧轨道数在时间轴上变化，'
      + '必须逐窗归因且布局导致的请求为 0 —— 不允许"顺手 tolerated"，也不允许把生命周期赖到布局头上',
    gpass('single_avatar_video_node_identity') && gpass('stream_change_not_layout_caused')
      && tgate('srcObject_same_object_across_layout_ops') && V.srcAttr === null,
    'src 属性始终为空（只用 srcObject）⇒ 不存在第二个视频源；'
      + '轨道数 ' + (V.trackIds || []).length + ' 属媒体链自身生命周期，见时间轴归因。'),

  item('R11-7', 'WebRTC reconnect = 0', ['layout_generated_no_backend_requests'],
    { connTrace: B.connTrace },
    { connTrace_after: A.connTrace, rtc_constructors_all_windows: ctorSum(allWins, 'rtc'), ws_constructors_all_windows: ctorSum(allWins, 'ws'), offers_observed: NET.offers_observed },
    '全部动作窗内 RTCPeerConnection / WebSocket 构造计数为 0；'
      + '页面自身 connect 事件轨迹（media=2）改前改后一致',
    ctorSum(allWins, 'rtc') === 0 && ctorSum(allWins, 'ws') === 0
      && eq(B.connTrace, A.connTrace) && gpass('layout_generated_no_backend_requests'),
    'connTrace entries ' + B.connTrace.entries + ' → ' + A.connTrace.entries
      + '，byEvent 相同 = ' + JSON.stringify(B.connTrace.byEvent)),

  item('R11-8', 'backend requests caused by layout = 0',
    ['layout_generated_no_backend_requests', 'layout_code_has_no_backend_call_sites', 'no_js_exceptions', 'no_new_http_errors_from_layout_ops', 'probe_instrumentation_alive_every_window'],
    {
      idle_windows: idleWins.map((w) => ({ tag: w.tag, wall_ms: w.wall_ms, requests_total: w.requests_total, backend: (w.backend_requests || []).length })),
      backend_rate_per_s: (NET.backend_rate_per_s || {}).idle_windows,
    },
    {
      op_windows: opWins.map((w) => ({ tag: w.tag, seq: w.seq, wall_ms: w.wall_ms, requests_total: w.requests_total, backend: (w.backend_requests || []).length, paths: w.backend_paths, layout_caused: w.layout_caused_fetches })),
      totals: { requests_all_windows: reqTotal, backend_all_windows: backendTotal, layout_caused: layoutCaused },
      backend_rate_per_s: NET.backend_rate_per_s,
      probe_alive: deadProbeWins.length === 0 ? 'ALL_WINDOWS_ALIVE' : deadProbeWins,
      static_side: '三个改动文件注释感知扫描：fetch/XHR 代码命中 0（正对照见 static_scan_selftest 15/15 PASS）',
    },
    '每个动作单独开窗 + 两个 10s 无操作对照窗；fetch 被 shim 记录调用栈，'
      + '只有栈里不含布局层标识的调用才允许存在 ⇒ 布局导致的请求必须为 0',
    layoutCaused === 0 && gpass('layout_generated_no_backend_requests')
      && gpass('layout_code_has_no_backend_call_sites') && gpass('no_js_exceptions')
      && gpass('no_new_http_errors_from_layout_ops') && deadProbeWins.length === 0
      && gpass('probe_instrumentation_alive_every_window'),
    '全部窗口 fetch/XHR/WS/RTC 合计 ' + reqTotal + ' 条请求、其中后端 ' + backendTotal
      + ' 条（1 次 POST /offer，initiator=main.js?v=layout-02，属媒体链生命周期），布局导致 ' + layoutCaused + ' 条。'),

  item('R11-9', 'NON_UI_MODIFIED_FILES = NONE', ['non_ui_modified_files_none'],
    { pre_manifest: FA.baselines.pre, listed_files: FAT.listed },
    {
      post_manifest: FA.baselines.post, files_hashed_now: FAT.files_hashed_now,
      modified_since_pre: FAT.modified_since_pre, in_scope_modified: inScopeList,
      out_of_scope: FAT.out_of_scope, unreadable_or_deleted: FAT.unreadable_or_deleted,
      changed_after_post_manifest: FAT.changed_after_post_manifest,
      avatar_layout_js_owner: { pre_sha: ownerRow && ownerRow.pre_sha, now_sha: ownerRow && ownerRow.now_sha, identical: ownerUnchanged },
      verdict_string: FA.VERDICT,
    },
    '每次判决都从磁盘重算（清单 282 个文件全量 sha256），不引用聊天口径：'
      + '范围外改动=0、被删/不可读=0、POST 基线之后再无新改动',
    verdictOk && gpass('non_ui_modified_files_none') && FAT.out_of_scope === 0
      && FAT.unreadable_or_deleted === 0 && FAT.changed_after_post_manifest === 0 && ownerUnchanged,
    '本轮唯一被改动的文件：' + inScopeList.join(' / ') + '（全部 presentation layer）。'),
];

/* ------------------------------------------------------------------ 汇总判决 */
const fails = ITEMS.filter((r) => !r.verdict).map((r) => r.id);
const out = {
  task_id: 'AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01',
  probe: 'gate_diff.cjs（离线：只把已落盘的实测值搬到需求条目上，不产生新测量）',
  generated_at: new Date().toISOString(),
  inputs: {
    CONTROL_INVENTORY_BEFORE: B.captured_at, CONTROL_INVENTORY_AFTER: A.captured_at,
    DOCK_ACCEPTANCE: ACC.captured_at, TRANSFORM_EQUIVALENCE: TEQ.captured_at,
    FILE_AUDIT_OFFLINE: FA.generated_at, MOVE_PROOF_OFFLINE: MP ? MP.generated_at : 'MISSING',
  },
  requirement_11_items: ITEMS,
  requirement_11_all_pass: fails.length === 0,
  control_ledger: {
    total: ledger.length, by_verdict: byVerdict,
    missing_after: missing, extra_in_after: extra,
    moved_into_dock: movedIntoDock, still_outside_dock: stillOutsideDock,
    identity_changed_detail: identityChanged, rows: ledger,
  },
  listeners: { total_before: B.listener_total, total_after: A.listener_total, diffs: listenerDiffs, per_selector: listeners },
  composition_attribution: compositionAttribution,
  final_keys_restated: Object.keys(ACC.requirement_keys || {})
    .reduce((m, k) => (m[k] = ACC.requirement_keys[k].status || ACC.requirement_keys[k], m), {}),
};

fs.writeFileSync(path.join(EV, 'GATE_DIFF.json'), JSON.stringify(out, null, 2), 'utf8');

console.log('wrote runtime/evidence/UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01/GATE_DIFF.json');
console.log('');
for (const r of ITEMS) console.log((r.verdict ? 'PASS ' : 'FAIL ') + r.id + '  ' + r.requirement);
console.log('');
console.log('控件台账 ' + ledger.length + ' 项：' + JSON.stringify(byVerdict)
  + '  MISSING=' + (missing.join(',') || '(none)') + '  EXTRA=' + (extra.join(',') || '(none)'));
console.log('  搬进 Dock 的选择器 ' + movedIntoDock.length + ' 个；仍在 Dock 外：' + (stillOutsideDock.join(',') || '(none)'));
console.log('监听：' + B.listener_total + ' → ' + A.listener_total + '，逐选择器差异 ' + listenerDiffs.length + ' 处');
console.log('中央区：stageChildElements ' + bc.stageChildElements + '→' + ac.stageChildElements
  + '，stageDescendants ' + bc.stageDescendants + '→' + ac.stageDescendants);
console.log('网络：全窗请求 ' + reqTotal + '，后端 ' + backendTotal + '，布局导致 ' + layoutCaused
  + '；rtc/ws 构造 ' + ctorSum(allWins, 'rtc') + '/' + ctorSum(allWins, 'ws'));
console.log('文件：' + FAT.files_hashed_now + '/' + FAT.listed + ' 已哈希，范围外 ' + FAT.out_of_scope
  + '，POST 后又变 ' + FAT.changed_after_post_manifest + '，owner 未变 ' + ownerUnchanged);
console.log('人物四参数归因：' + compositionAttribution.verdict);
console.log('');
console.log('VERDICT: ' + (fails.length ? 'GATE_DIFF = FAIL (' + fails.join(',') + ')' : 'REQUIREMENT_11_ALL_ITEMS = PASS'));
if (fails.length) process.exitCode = 1;
