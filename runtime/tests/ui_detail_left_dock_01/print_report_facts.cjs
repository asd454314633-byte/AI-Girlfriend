/* 一次性只读摘要：把已落盘的实测值打印成 FINAL_REPORT 需要的字符串。
 * 不产生新测量、不写任何文件。运行：node runtime/tests/ui_detail_left_dock_01/print_report_facts.cjs
 */
'use strict';
const fs = require('fs');
const path = require('path');
const P = path.join(__dirname, '..', '..', 'evidence', 'UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01');
const J = (f) => JSON.parse(fs.readFileSync(path.join(P, f), 'utf8'));

const te = J('TRANSFORM_EQUIVALENCE.json');
const txt = fs.readFileSync(path.join(P, 'TRANSFORM_EQUIVALENCE.json'), 'utf8');
const re = /"inlineTransform"\s*:\s*"([^"]+)"/g;
const seen = [];
let m;
while ((m = re.exec(txt))) if (!seen.includes(m[1])) seen.push(m[1]);
console.log('inlineTransform 出现值（去重，按文件顺序）:');
for (const s of seen) console.log('  ' + s);

console.log('\nsteps:');
for (const k of Object.keys(te.steps || {})) {
  const v = te.steps[k];
  console.log('  ' + k + ' = ' + JSON.stringify(v).slice(0, 260));
}
console.log('\ndouble_ready =', JSON.stringify(te.double_ready));
console.log('gates_all_pass =', te.gates_all_pass, 'captured_at =', te.captured_at);

const acc = J('DOCK_ACCEPTANCE.json');
const det = acc.detail;
console.log('\ndetail.video =', JSON.stringify(det.video));
console.log('detail.ids =', JSON.stringify({ avVideoCount: det.avVideoCount, stageChildElements: det.stageChildElements, stageDescendants: det.stageDescendants }));
console.log('localStorage =', JSON.stringify({
  before: (det.ls_before && det.ls_before.key_count) || null,
  after: (det.ls_after && det.ls_after.key_count) || null,
}));
console.log('gates keys =', Object.keys(acc.gates).length, 'all_pass =', acc.gates_all_pass);
console.log('video_node_stamp =', JSON.stringify(acc.video_node_stamp));

const fa = J('FILE_AUDIT_OFFLINE.json');
console.log('\nFA baselines =', JSON.stringify(fa.baselines && Object.keys(fa.baselines)));
console.log('FA verdict =', fa.VERDICT);

const mp = J('MOVE_PROOF_OFFLINE.json');
console.log('\nP1 blocks:');
for (const b of mp.P1_P2_moved_blocks) {
  console.log('  ' + JSON.stringify({ label: b.label || b.name || b.id, bytes: b.bytes, occ: b.occurrences_in_now, inDock: b.inside_dock, group: b.group && (b.group.name || b.group) }));
}
console.log('P3 lost_or_unexplained =', JSON.stringify(mp.P3_gf_controls_children.lost_or_unexplained));
console.log('P6 rules =', JSON.stringify(mp.P6_cache_busters.rules.map((r) => r.file + ':' + (r.satisfied ? 'OK' : 'FAIL') + '(' + r.observed + ')')));
console.log('P8 bridge =', JSON.stringify(mp.P8_runtime_delta_attribution.static_vs_runtime_bridge));
