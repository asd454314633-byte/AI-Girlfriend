/* ============================================================================
 * file_audit_offline.cjs — 纯磁盘的文件级归属审计（不需要 7860，不重启任何东西）
 *
 * 任务书第 11 条最后一项：NON_UI_MODIFIED_FILES = NONE。
 * 这一条不能靠"我记得只改了三个文件"来结 —— 必须把 PRE/POST 清单里点过名的
 * 每一份文件（46 份 UI 静态资源 + 235 份后端/相邻系统源文件）重新哈希，
 * 再与两份清单对拍，任何一处【超出 Dock 作用域】的变化都要单独点名。
 *
 * 三份基准同时比，因为本轮 gf_dock.css 在 POST 之后又为了"轨道宽度=元素盒宽"
 * 修过一次（那是产品修复，仍在作用域内，但要看得见它发生在 POST 之后）：
 *   PRE  = 动手之前冻结的基线
 *   POST = 第一次改完后的清单
 *   NOW  = 本次运行时磁盘
 *
 * 用法： node file_audit_offline.cjs
 * ==========================================================================*/
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const REPO = path.join(__dirname, '..', '..', '..');
const EV = path.join(REPO, 'runtime', 'evidence', 'UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01');
const OUT = path.join(EV, 'FILE_AUDIT_OFFLINE.json');

const PRE = JSON.parse(fs.readFileSync(path.join(EV, 'PRE_CHANGE_MANIFEST.json'), 'utf8'));
const POST = JSON.parse(fs.readFileSync(path.join(EV, 'POST_CHANGE_MANIFEST.json'), 'utf8'));
const STATIC_ROOT = PRE.staticRoot;

const sha = (p) => {
  const b = fs.readFileSync(p);
  return { sha256: crypto.createHash('sha256').update(b).digest('hex'), bytes: b.length };
};
const diskPathOf = (rec) => rec.file
  ? (rec.group ? path.join(REPO, rec.file) : path.join(STATIC_ROOT, rec.file))
  : null;

/* 作用域：本轮【允许】改动的文件。三个 UI 呈现层文件 + 测试资产（tests/ 下的
 * 探针不属于生产静态根，本来就不在这两张清单的 uiApp 里）。 */
const IN_SCOPE = new Set(['gf_dock.css', 'index.html', 'ui_layout_modes.js', 'gf_dock.css.bak_dock01']);

const rows = [];
const push = (kind, rec, preMap, postMap) => {
  const dp = diskPathOf(rec);
  let now = null, readErr = null;
  if (dp && fs.existsSync(dp)) { try { now = sha(dp); } catch (e) { readErr = String(e.message).slice(0, 120); } }
  else readErr = dp ? 'MISSING_ON_DISK' : 'NO_PATH';
  const preSha = preMap.get(rec.file), postSha = postMap.get(rec.file);
  const status = !now ? (readErr === 'MISSING_ON_DISK' ? 'DELETED_SINCE_MANIFEST' : 'UNREADABLE:' + readErr)
    : (preSha === undefined ? 'NOT_IN_PRE' : (now.sha256 !== preSha ? 'MODIFIED_SINCE_PRE' : 'UNCHANGED_SINCE_PRE'));
  const afterPost = !!(now && postSha && now.sha256 !== postSha);
  rows.push({
    kind, group: rec.group || 'uiApp', file: rec.file, cls: rec.cls || (rec.group ? 'BACKEND_OUTSIDE_UI' : 'UNCLASSIFIED'),
    pre_sha: preSha ? preSha.slice(0, 16) : null, post_sha: postSha ? postSha.slice(0, 16) : null,
    now_sha: now ? now.sha256.slice(0, 16) : null, now_bytes: now ? now.bytes : null,
    status, changed_after_post_manifest: afterPost,
    in_scope: IN_SCOPE.has(rec.file),
  });
};

const mapOf = (m) => new Map(m.uiApp.filter((r) => r.sha256).map((r) => [r.file, r.sha256]));
const preUi = mapOf(PRE), postUi = mapOf(POST);
for (const r of PRE.uiApp) push('uiApp', r, preUi, postUi);
/* 本轮 POST 里新增的磁盘文件（PRE 没有）也要进账，否则"新增了谁"看不见 */
for (const r of POST.uiApp) if (!preUi.get(r.file)) push('uiApp_new_in_post', r, preUi, postUi);
const preBe = new Map(PRE.backend.filter((r) => r.sha256).map((r) => [r.file, r.sha256]));
const postBe = new Map(POST.backend.filter((r) => r.sha256).map((r) => [r.file, r.sha256]));
for (const r of PRE.backend) push('backend', r, preBe, postBe);

const modified = rows.filter((r) => r.status === 'MODIFIED_SINCE_PRE' || r.status === 'NOT_IN_PRE');
const violations = modified.filter((r) => !r.in_scope);
const deletions = rows.filter((r) => /DELETED|UNREADABLE/.test(r.status));

const verdict = violations.length === 0 && deletions.length === 0
  ? 'NON_UI_MODIFIED_FILES = NONE'
  : 'NON_UI_MODIFIED_FILES = VIOLATION';

const out = {
  task_id: 'AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01',
  kind: 'offline disk-only file audit (no HTTP, no restart)',
  generated_at: new Date().toISOString(),
  baselines: { pre: PRE.generatedAt, post: POST.generatedAt },
  totals: {
    files_hashed_now: rows.filter((r) => r.now_sha).length,
    listed: rows.length,
    uiapp: rows.filter((r) => r.kind !== 'backend').length,
    backend_or_adjacent: rows.filter((r) => r.kind === 'backend').length,
    modified_since_pre: modified.length,
    out_of_scope: violations.length,
    unreadable_or_deleted: deletions.length,
    changed_after_post_manifest: rows.filter((r) => r.changed_after_post_manifest).length,
  },
  VERDICT: verdict,
  in_scope_modified: modified.filter((r) => r.in_scope),
  out_of_scope_modified: violations,
  deletions_or_unreadable: deletions,
  post_manifest_followups: rows.filter((r) => r.changed_after_post_manifest),
  rows,
};
fs.writeFileSync(OUT, JSON.stringify(out, null, 2));
console.log('wrote ' + OUT);
console.log('VERDICT: ' + verdict);
console.log('hashed_now=' + out.totals.files_hashed_now + '/' + out.totals.listed +
            '  modified_since_pre=' + out.totals.modified_since_pre +
            '  out_of_scope=' + out.totals.out_of_scope +
            '  deleted_or_unreadable=' + out.totals.unreadable_or_deleted +
            '  changed_after_post=' + out.totals.changed_after_post_manifest);
for (const r of out.in_scope_modified) console.log('  IN_SCOPE  ' + r.file.padEnd(24) + ' ' + (r.pre_sha || '-') + ' -> ' + r.now_sha + (r.changed_after_post_manifest ? '  (再改于 POST 之后)' : ''));
for (const r of out.out_of_scope_modified) console.log('  VIOLATION ' + r.file + ' [' + r.cls + '] ' + r.pre_sha + ' -> ' + r.now_sha);
for (const r of out.deletions_or_unreadable) console.log('  DELETED?  ' + r.file + ' ' + r.status);
process.exit(violations.length === 0 && deletions.length === 0 ? 0 : 1);
