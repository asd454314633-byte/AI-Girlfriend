/* ============================================================================
 * manifest.cjs (DOCK 轮) — AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01
 *
 * 与上一轮 manifest.cjs 同一套只读取证手法，但作用域按本轮任务书重划：
 *
 *   本轮允许改（纯 presentation layer）：
 *     index.html            建立唯一左侧 Dock，把现有控件【原样迁入】
 *     gf_dock.css           本轮新建：Dock 排布 / 圆球缩小 / LIVE 适配
 *     ui_layout_modes.js    只改「画面布局」入口与面板的挂载点（改挂进 Dock）
 *     ui_layout_modes.css   只改面板停靠相关规则（面板不再是悬浮框）
 *
 *   本轮【明令不得动】（任务书第 9 条）：
 *     avatar_layout.js / avatar_layout.css
 *     —— 人物 video 的 transform 行为必须逐字保持，唯一 owner 不重写。
 *     所以它从上一轮的 UI_SCOPE 降级为本轮的 MUST_NOT_CHANGE_THIS_ROUND，
 *     POST 阶段若哈希变化即判 NON_UI_MODIFIED_FILES ≠ NONE / 越界。
 *
 *   §1 纪律照旧：线上字节 = 磁盘字节（逐个 7860 GET 回读对账），
 *   证据落在本轮自己的目录，禁止引用上一轮哈希冒充本轮现算。
 *
 * 只读磁盘 + 只发 GET；不改任何文件，不重启任何服务。
 * ==========================================================================*/
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const http = require('node:http');

const APP = path.join(__dirname, '..', '..', 'ui', 'app');   // 7860 静态根
const REPO = path.join(__dirname, '..', '..', '..');
const EV = path.join(REPO, 'runtime', 'evidence', 'UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01');
const BASE = process.env.UIVM_BASE || 'http://127.0.0.1:7860';

const PHASE = process.argv[2] === 'postchange' ? 'postchange' : 'prechange';

const TASK_ID = 'AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01';

/** 本轮允许修改的显示层文件（任务书第 4/5/8 条：只重新布局现有 DOM）。 */
const UI_SCOPE = ['index.html', 'gf_dock.css', 'ui_layout_modes.js', 'ui_layout_modes.css'];

/** 第 9 条点名：人物 transform owner 与其样式，本轮不得重写。 */
const MUST_NOT_CHANGE = ['avatar_layout.js', 'avatar_layout.css'];

/** 其余冻结面：与上一轮一致，加上本轮同样不碰的面板/上游样式。 */
const FROZEN = [
  'audio-router.js', 'avatar-sync.js', 'main.js', 's2s-realtime-client.js',
  'server.py', 'voice_models_admin.py', 'avatar_admin.py', 'character_admin.py',
  'local_api.py', 'models_admin.py', 'auth.py', 'limiter.py', 'pid_liveness.py',
  'ui/girlfriend.js', 'ui/chat.js', 'ui/dom.js', 'ui/account.js', 'ui/reading.js',
  'ui/zh_cn.js', 'ui/girlfriend.css',
  'avatar_panel.js', 'avatar_panel.css', 'character_panel.js', 'character_panel.css',
  'character_panel_fragment.html', 'conn_trace.js', 'style.css',
  'package.json', 'package-lock.json', 'requirements.txt',
];

function sha(p) {
  const b = fs.readFileSync(p);
  return { sha256: crypto.createHash('sha256').update(b).digest('hex'),
           bytes: b.length,
           mtime: fs.statSync(p).mtime.toISOString().replace('T', ' ').slice(0, 19) };
}

function walk(dir, base, acc) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) { if (e.name !== 'node_modules' && e.name !== '__pycache__') walk(full, base, acc); continue; }
    if (!/\.(js|css|html|py|json|txt)$/.test(e.name)) continue;
    acc.push({ file: path.relative(base, full).replace(/\\/g, '/'), ...sha(full) });
  }
  return acc;
}

const all = walk(APP, APP, []);
const rows = all.map((r) => {
  let cls = 'UNCLASSIFIED';
  if (UI_SCOPE.includes(r.file)) cls = 'UI_SCOPE_DOCK';
  else if (MUST_NOT_CHANGE.includes(r.file)) cls = 'MUST_NOT_CHANGE_THIS_ROUND';
  else if (FROZEN.includes(r.file)) cls = 'FROZEN';
  else if (/(\.bak|\.old|\.copy|before_|pre-)/i.test(r.file)) cls = 'LEGACY_BACKUP(pre-existing)';
  else if (/^test_/i.test(r.file)) cls = 'TEST_ASSET';
  else if (r.file.endsWith('.py')) cls = 'BACKEND';
  return { ...r, cls };
});

const BACKEND_TARGETS = [
  { label: 'avatar_8010_src', dir: path.join(REPO, 'runtime', 'avatar', 'app'), re: /\.(py|json|yaml|yml|txt)$/i },
  { label: 's2s_8765_src', dir: path.join(REPO, 'runtime', 'core', 'venv', 'Lib', 'site-packages', 'speech_to_speech'), re: /\.py$/i },
  { label: 'launchers', dir: path.join(REPO, 'runtime', 'windows'), re: /\.(ps1)$/i, flat: true },
  { label: 'launchers', dir: path.join(REPO, 'scripts'), re: /\.(ps1)$/i, flat: true },
];
const backend = [];
for (const t of BACKEND_TARGETS) {
  if (!fs.existsSync(t.dir)) { backend.push({ file: '<MISSING DIR> ' + t.label }); continue; }
  (function rec(p) {
    for (const e of fs.readdirSync(p, { withFileTypes: true })) {
      if (e.name === '__pycache__' || e.name === 'node_modules') continue;
      const full = path.join(p, e.name);
      if (e.isDirectory()) { if (!t.flat) rec(full); continue; }
      if (!t.re.test(e.name)) continue;
      if (/(\.bak|before_|pre-)/i.test(e.name)) continue;
      backend.push({ group: t.label, file: path.relative(REPO, full).replace(/\\/g, '/'), ...sha(full) });
    }
  })(t.dir);
}

/* ---- 线上回读：GET 每个作用域文件，字节级对账 ----
 * gf_dock.css 在 prechange 阶段还不存在（本轮新建），所以缺失要区分成
 * "本轮新建、尚未落盘"，不能当成取证失败。 */
function get(url) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
      const chunks = [];
      let n = 0;
      res.on('data', (c) => { chunks.push(c); n += c.length;
        if (n > 16 * 1024 * 1024) { req.destroy(); resolve({ error: 'SERVED_TOO_LARGE', status: res.statusCode }); } });
      res.on('end', () => resolve({ status: res.statusCode, body: Buffer.concat(chunks) }));
    });
    req.on('error', (e) => resolve({ error: String(e.message).slice(0, 160) }));
    req.setTimeout(8000, () => { req.destroy(); resolve({ error: 'HTTP_TIMEOUT ' + url }); });
  });
}

async function servedReadback() {
  const out = [];
  for (const f of [...UI_SCOPE, ...MUST_NOT_CHANGE]) {
    const disk = path.join(APP, f);
    if (!fs.existsSync(disk)) {
      out.push({ file: f, disk: 'MISSING',
                 note: f === 'gf_dock.css' ? 'NEW_THIS_ROUND_NOT_YET_CREATED' : 'UNEXPECTED_MISSING' });
      continue;
    }
    const d = sha(disk);
    const r = await get(BASE + '/' + f + '?probe=' + PHASE);
    const sBody = r.body || Buffer.alloc(0);
    const sSha = r.body ? crypto.createHash('sha256').update(sBody).digest('hex') : null;
    out.push({ file: f, url: BASE + '/' + f, http_status: r.status || null,
               served_bytes: r.body ? sBody.length : null, served_sha256: sSha,
               disk_bytes: d.bytes, disk_sha256: d.sha256,
               match: !!(sSha && sSha === d.sha256 && sBody.length === d.bytes),
               error: r.error || null });
  }
  return out;
}

let processIdentity = { available: false };
try {
  const p = path.join(EV, PHASE, 'PROC_IDENTITY.json');
  processIdentity = { available: true, file: p, data: JSON.parse(fs.readFileSync(p, 'utf8')) };
} catch (e) {
  processIdentity = { available: false, why: String(e.message).slice(0, 200) };
}

async function main() {
const served = await servedReadback();
/* 只有"已存在的作用域文件"参与 match 门禁；本轮新建文件在 prechange 缺席是预期。 */
const servedRelevant = served.filter((s) => s.disk !== 'MISSING');
const report = {
  task_id: TASK_ID,
  phase: PHASE,
  generatedAt: new Date().toISOString(),
  staticRoot: APP,
  base: BASE,
  counts: {
    uiAppFiles: rows.length,
    uiScopeDock: rows.filter(r => r.cls === 'UI_SCOPE_DOCK').length,
    mustNotChange: rows.filter(r => r.cls === 'MUST_NOT_CHANGE_THIS_ROUND').length,
    frozen: rows.filter(r => r.cls === 'FROZEN').length,
    backendFiles: backend.length,
  },
  served_vs_disk: served,
  served_all_match: servedRelevant.length > 0 && servedRelevant.every((s) => s.match === true),
  uiApp: rows.sort((a, b) => a.file.localeCompare(b.file)),
  backend: backend.sort((a, b) => a.file.localeCompare(b.file)),
  processIdentity,
};

fs.mkdirSync(EV, { recursive: true });
const outFile = path.join(EV, PHASE === 'prechange' ? 'PRE_CHANGE_MANIFEST.json' : 'POST_CHANGE_MANIFEST.json');
fs.writeFileSync(outFile, JSON.stringify(report, null, 2));
console.log('wrote', outFile);
console.log('uiAppFiles=%d uiScopeDock=%d mustNotChange=%d frozen=%d backendFiles=%d servedAllMatch=%s',
  report.counts.uiAppFiles, report.counts.uiScopeDock, report.counts.mustNotChange,
  report.counts.frozen, report.counts.backendFiles, report.served_all_match);
for (const s of served) {
  console.log('  SERVED ' + String(s.file).padEnd(24) + ' ' + String(s.http_status) + ' ' +
    (s.disk === 'MISSING' ? (s.note || 'MISSING') : (s.match ? 'MATCH' : 'MISMATCH')) +
    '  disk=' + String((s.disk_sha256 || '').slice(0, 16)) + '/' + s.disk_bytes +
    ' served=' + String((s.served_sha256 || '').slice(0, 16)) + '/' + s.served_bytes +
    (s.error ? ' ERR=' + s.error : ''));
}
for (const r of report.uiApp.filter(r => r.cls === 'UI_SCOPE_DOCK' || r.cls === 'MUST_NOT_CHANGE_THIS_ROUND' || r.cls === 'FROZEN')) {
  console.log('  ' + r.file.padEnd(36) + ' ' + r.cls.padEnd(26) + ' ' + r.sha256.slice(0, 16) + ' ' + r.bytes);
}
}

main().catch((e) => { console.log('TOPLEVEL_FAIL', String(e && e.message || e).slice(0, 300)); process.exitCode = 2; });
