/* ============================================================================
 * panel_dump.cjs — 只读诊断：把 #uivm-panel 的 outerHTML 在两组 owner 状态下各
 * 采一份，做字符级 diff，用来定位 transform_equivalence 里
 * panel_identical_at_same_state 那一条 FAIL 的确切来源（同长度、不同哈希 ⇒
 * 一定有等长的文本被换掉，必须点名，不能含糊过去）。
 *
 * 纪律同上：set() 一律 {quiet:true}（avatar_layout.js:322 只在 !quiet 时 save()），
 * 不点任何业务控件，不写 localStorage。
 * 用法： node panel_dump.cjs
 * ==========================================================================*/
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { connect, Tab, sleep } = require('../ui_dual_viewmode_01/cdp.cjs');
const { INSTALL, INJECT_DOUBLE } = require('../ui_detail_composition_repair_01/measure_page.cjs');

const REPO = path.join(__dirname, '..', '..', '..');
const EV = path.join(REPO, 'runtime', 'evidence', 'UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01');
const OUT = path.join(EV, 'UIVM_PANEL_DUMP.json');
const BEFORE = JSON.parse(fs.readFileSync(path.join(EV, 'CONTROL_INVENTORY_BEFORE.json'), 'utf8'));
const TARGET = { s: BEFORE.owner.s, w: BEFORE.owner.w, x: BEFORE.owner.x, y: BEFORE.owner.y };

const DUMP = `(function(){
  /* 与 dock_inventory.cjs:81 同款哈希（XOR 变体、倒序）。不同款 = 尺子不同刻度，
   * 比出来的差异是假的 —— 本文件第一版就因此把"面板结构未变"误判成不一致。 */
  function hash(s){ var h = 5381, i = s.length; while (i) { h = (h * 33) ^ s.charCodeAt(--i); } return (h >>> 0).toString(16); }
  var p = document.getElementById('uivm-panel'); if (!p) return null;
  var o = window.AvatarLayout; var g = o.get();
  return {state:{s:g.s,w:g.w,x:g.x,y:g.y}, len:p.outerHTML.length, hash:hash(p.outerHTML), html:p.outerHTML};
})()`;

function setState(tab, st) {
  return tab.js(`(function(){var o=window.AvatarLayout;
    o.set('stretch',${st.w},{quiet:true}); o.set('x',${st.x},{quiet:true});
    o.set('y',${st.y},{quiet:true}); o.set('scale',${st.s},{quiet:true}); return 'SET';})()`);
}

/* 等长字符串的字符级差异定位：给出每一段不同的 [start,end) 与两侧文本。 */
function diffSpans(a, b) {
  const out = [];
  const n = Math.min(a.length, b.length);
  let i = 0;
  while (i < n) {
    if (a[i] !== b[i]) {
      let j = i;
      while (j < n && a[j] !== b[j]) j++;
      out.push({ at: i, a: a.slice(Math.max(0, i - 40), j + 40), b: b.slice(Math.max(0, i - 40), j + 40),
                 aSeg: a.slice(i, j), bSeg: b.slice(i, j) });
      i = j;
    } else i++;
  }
  if (a.length !== b.length) out.push({ at: n, note: 'LENGTH_DIFF', aLen: a.length, bLen: b.length });
  return out;
}

/* 把"两组状态下的面板 HTML 之差"完全归因到状态：只在 uivm-sec-detail 区段内做
 * 三处替换（大小读数 / 左右读数 / 预设高亮 class），其余字节一律不许动。
 * 若替换结果与另一组状态的 HTML 逐字相等 ⇒ 面板结构没有被本轮改过，
 * 14 字节差异 100% 来自 owner 状态。这是可证伪的强判据，不是"看起来差不多"。
 * 区段限定是必须的：LIVE 区段里也有一个 <output>100%</output>（直播竖屏的人物
 * 大小），全文替换会把它一起改掉，从而制造出一个假的"完全解释"。 */
const PRESET_S = [100, 140, 180, 220];
function explainByState(htmlFrom, s0, s1) {
  const i0 = htmlFrom.indexOf('uivm-sec-detail');
  const i1 = htmlFrom.indexOf('uivm-sec-live');
  if (i0 < 0 || i1 < 0 || i1 < i0) return { ok: false, why: 'SECTION_ANCHOR_NOT_FOUND', i0, i1 };
  const head = htmlFrom.slice(0, i0);
  let det = htmlFrom.slice(i0, i1);
  const tail = htmlFrom.slice(i1);
  const subs = [];
  function sub(from, to, label) {
    const n = det.split(from).length - 1;
    if (n !== 1) { subs.push({ label, from, to, occurrences: n, applied: false }); return; }
    det = det.replace(from, to);
    subs.push({ label, from, to, occurrences: 1, applied: true });
  }
  sub('<output>' + s0.s + '%</output>', '<output>' + s1.s + '%</output>', 'detail_scale_output');
  if (s0.w !== s1.w) sub('<output>' + s0.w + '%</output>', '<output>' + s1.w + '%</output>', 'detail_stretch_output');
  sub('<output>' + s0.x + '</output>', '<output>' + s1.x + '</output>', 'detail_offsetX_output');
  if (s0.y !== s1.y) sub('<output>' + s0.y + '</output>', '<output>' + s1.y + '</output>', 'detail_offsetY_output');
  const a0 = PRESET_S.indexOf(s0.s) >= 0, a1 = PRESET_S.indexOf(s1.s) >= 0;
  if (a0 && !a1) sub('class="uivm-btn uivm-active"', 'class="uivm-btn"', 'preset_active_class_removed');
  if (!a0 && a1) sub('data-preset="original">原始', 'data-preset="original">原始', 'preset_active_class_added_NOT_SUPPORTED');
  return { ok: true, html: head + det + tail, subs, sectionRange: [i0, i1] };
}

(async function main() {
  const conn = await connect();
  const tab = await Tab.create(conn, 'about:blank');
  await tab.setViewport(1920, 1080);
  await tab.navigate('http://127.0.0.1:7860/?probe=panel-dump', 4000);
  await sleep(1200);
  await tab.js(INSTALL);
  await tab.js(INJECT_DOUBLE);
  for (let i = 0; i < 40; i++) {
    const r = await tab.js(`(function(){var v=document.getElementById('av-video');return v?[v.videoWidth,v.readyState]:null;})()`);
    if (r && r[0] > 0 && r[1] >= 2) break;
    await sleep(250);
  }
  const now = await tab.js(DUMP);
  await setState(tab, TARGET);
  await sleep(150);
  const atTarget = await tab.js(DUMP);
  /* 还原到页面加载时的持久化状态（quiet，不落盘） */
  await setState(tab, now.state);
  await sleep(150);
  const restored = await tab.js(DUMP);
  await tab.js(`(function(){ if (window.__DOUBLE_TIMER) clearInterval(window.__DOUBLE_TIMER); return 'C'; })()`);

  const explained = explainByState(atTarget.html, atTarget.state, now.state);
  const bNode = BEFORE.controls.find((c) => c.sel === '#uivm-panel') || {};
  const B_LEN = bNode.outerLen, B_HASH = bNode.outerHash;
  const res = {
    task_id: 'AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01',
    probe: 'panel_dump', captured_at: new Date().toISOString(),
    hash_function: 'h=5381; while(i){h=(h*33)^s.charCodeAt(--i)} — 与 dock_inventory.cjs:81 同款',
    before_frozen: { outerLen: B_LEN, outerHash: B_HASH,
                     owner_state: { s: BEFORE.owner.s, w: BEFORE.owner.w, x: BEFORE.owner.x, y: BEFORE.owner.y } },
    at_current_state: { state: now.state, len: now.len, hash: now.hash },
    at_before_state: { state: atTarget.state, len: atTarget.len, hash: atTarget.hash },
    restored: { state: restored.state, len: restored.len, hash: restored.hash,
                identical_to_current: restored.html === now.html },
    gates: {
      /* 面板在 BEFORE 那组状态下的字节形状必须与 BEFORE 冻结值逐字相同 */
      panel_bytes_identical_at_before_state: {
        pass: atTarget.len === B_LEN && atTarget.hash === B_HASH,
        before: { len: B_LEN, hash: B_HASH },
        observed: { len: atTarget.len, hash: atTarget.hash },
      },
      /* 两组状态之间的全部差异，必须能被"状态读数 + 预设高亮"完全解释 */
      panel_delta_fully_explained_by_state: {
        pass: explained.ok === true && explained.html === now.html,
        substitutions: explained.subs,
        reconstructed_equals_current: explained.ok === true && explained.html === now.html,
        byte_delta: now.len - atTarget.len,
      },
      /* 探针自证：设回原值后页面逐字复原（quiet 通道没有留下任何痕迹） */
      restore_closed_loop: { pass: restored.html === now.html },
    },
    diff_atBefore_vs_current: diffSpans(atTarget.html, now.html),
    html_at_before_state: atTarget.html,
    html_at_current_state: now.html,
  };
  fs.writeFileSync(OUT, JSON.stringify(res, null, 2));
  console.log('wrote ' + OUT);
  console.log('BEFORE frozen len/hash = ' + B_LEN + ' / ' + B_HASH);
  console.log('at BEFORE state  len/hash = ' + atTarget.len + ' / ' + atTarget.hash +
              '  match=' + (atTarget.len === B_LEN && atTarget.hash === B_HASH));
  console.log('at current state len/hash = ' + now.len + ' / ' + now.hash);
  console.log('restored == current : ' + res.restored.identical_to_current);
  console.log('--- gates ---');
  for (const k of Object.keys(res.gates)) {
    console.log((res.gates[k].pass ? 'PASS ' : 'FAIL ') + k);
  }
  console.log('substitutions = ' + JSON.stringify(res.gates.panel_delta_fully_explained_by_state.substitutions));
  console.log('reconstructed(before-state html + state subs) === current html : ' +
              res.gates.panel_delta_fully_explained_by_state.reconstructed_equals_current);
  console.log('byte_delta = ' + res.gates.panel_delta_fully_explained_by_state.byte_delta);
  console.log('--- spans differing between the two states ---');
  for (const d of res.diff_atBefore_vs_current) console.log(JSON.stringify(d).slice(0, 400));
  try { await tab.close(); } catch (_) {}
  conn.close();
  process.exit(0);
})().catch((e) => { console.log('PANEL_DUMP_CRASH ' + String((e && e.stack) || e).slice(0, 1500)); process.exit(1); });
