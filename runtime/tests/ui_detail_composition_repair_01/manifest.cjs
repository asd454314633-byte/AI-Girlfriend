/* ============================================================================
 * manifest.cjs (V2) — AI_GIRLFRIEND_UI_DETAIL_COMPOSITION_REPAIR_01 · §1
 *
 * 与 V1 的 manifest.cjs 同一套只读取证，但有三处必须按本轮任务重定义：
 *   1) 作用域不同：本轮 §0 明文允许动 avatar_layout.js / avatar_layout.css
 *      （纯显示层接口/参数），它们在 V1 里属 FROZEN。分类若照抄 V1，
 *      POST 阶段会把"本轮合法修改"误判成越界。
 *   2) §1 要求"线上字节 = 磁盘字节"：所以清单里除了磁盘 SHA256，还必须
 *      对每个作用域文件做一次 7860 HTTP 回读并逐个对账 —— 只有磁盘哈希
 *      证不了用户看到的就是这份代码。
 *   3) 证据落在本轮自己的目录，禁止引用上一轮哈希冒充本轮现算。
 *
 * 只读磁盘 + 只发 GET 请求；不改任何文件，不重启任何服务。
 * ==========================================================================*/
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const http = require('node:http');

const APP = path.join(__dirname, '..', '..', 'ui', 'app');   // 7860 静态根
const REPO = path.join(__dirname, '..', '..', '..');
const EV = path.join(REPO, 'runtime', 'evidence', 'UI_DETAIL_COMPOSITION_REPAIR_01');
const BASE = process.env.UIVM_BASE || 'http://127.0.0.1:7860';

const PHASE = process.argv[2] === 'postchange' ? 'postchange' : 'prechange';
const OUTDIR = path.join(EV, PHASE);

/** 本轮允许修改的显示层文件（§0 逐条点名）。 */
const UI_SCOPE = ['index.html', 'ui_layout_modes.js', 'ui_layout_modes.css',
                  'avatar_layout.js', 'avatar_layout.css'];

/** 冻结面：V1 冻结清单减去本轮被 §0 纳入范围的 avatar_layout.*。 */
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
  if (UI_SCOPE.includes(r.file)) cls = 'UI_SCOPE_V2';
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

/* ---- 线上回读：GET 每个作用域文件，字节级对账 ---- */
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
  for (const f of UI_SCOPE) {
    const disk = path.join(APP, f);
    if (!fs.existsSync(disk)) { out.push({ file: f, disk: 'MISSING' }); continue; }
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
const report = {
  task_id: 'AI_GIRLFRIEND_UI_DETAIL_COMPOSITION_REPAIR_01',
  phase: PHASE,
  generatedAt: new Date().toISOString(),
  staticRoot: APP,
  base: BASE,
  counts: {
    uiAppFiles: rows.length,
    uiScopeV2: rows.filter(r => r.cls === 'UI_SCOPE_V2').length,
    frozen: rows.filter(r => r.cls === 'FROZEN').length,
    backendFiles: backend.length,
  },
  served_vs_disk: served,
  served_all_match: served.every((s) => s.match === true),
  uiApp: rows.sort((a, b) => a.file.localeCompare(b.file)),
  backend: backend.sort((a, b) => a.file.localeCompare(b.file)),
  processIdentity,
};

fs.mkdirSync(OUTDIR, { recursive: true });
const outFile = path.join(EV, PHASE === 'prechange' ? 'PRE_CHANGE_MANIFEST.json' : 'POST_CHANGE_MANIFEST.json');
fs.writeFileSync(outFile, JSON.stringify(report, null, 2));
console.log('wrote', outFile);
console.log('uiAppFiles=%d uiScopeV2=%d frozen=%d backendFiles=%d servedAllMatch=%s',
  report.counts.uiAppFiles, report.counts.uiScopeV2, report.counts.frozen,
  report.counts.backendFiles, report.served_all_match);
for (const s of served) {
  console.log('  SERVED ' + String(s.file).padEnd(22) + ' ' + s.http_status + ' ' +
    (s.match ? 'MATCH' : 'MISMATCH') + '  disk=' + String((s.disk_sha256 || '').slice(0, 16)) +
    '/' + s.disk_bytes + ' served=' + String(s.served_sha256 || '').slice(0, 16) + '/' + s.served_bytes +
    (s.error ? ' ERR=' + s.error : ''));
}
for (const r of report.uiApp.filter(r => r.cls === 'UI_SCOPE_V2' || r.cls === 'FROZEN')) {
  console.log(`  ${r.file.padEnd(36)} ${r.cls.padEnd(11)} ${r.sha256.slice(0, 16)} ${r.bytes}`);
}
}

main().catch((e) => { console.log('TOPLEVEL_FAIL', String(e && e.message || e).slice(0, 300)); process.exitCode = 2; });
