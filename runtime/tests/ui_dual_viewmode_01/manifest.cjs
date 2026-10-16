/* ============================================================================
 * manifest.cjs — 生产文件 SHA256/字节/mtime 台账（PRE / POST 同一份代码）
 *   node manifest.cjs prechange
 *   node manifest.cjs postchange
 * 只读磁盘，不改任何文件。
 * ==========================================================================*/
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const APP = path.join(__dirname, '..', '..', 'ui', 'app');           // 7860 静态根
const REPO = path.join(__dirname, '..', '..', '..');
const EV = path.join(REPO, 'runtime', 'evidence', 'UI_DUAL_VIEWMODE_01');

const PHASE = process.argv[2] === 'postchange' ? 'postchange' : 'prechange';
const OUTDIR = path.join(EV, PHASE);

/** 冻结面：任务 §0 明令禁止改动的东西，逐个点名核算。 */
const FROZEN = [
  'audio-router.js', 'avatar-sync.js', 'main.js', 's2s-realtime-client.js',
  'server.py', 'voice_models_admin.py', 'avatar_admin.py', 'character_admin.py',
  'local_api.py', 'models_admin.py', 'auth.py', 'limiter.py', 'pid_liveness.py',
  'ui/girlfriend.js', 'ui/chat.js', 'ui/dom.js', 'ui/account.js', 'ui/reading.js',
  'ui/zh_cn.js', 'ui/girlfriend.css',
  'avatar_panel.js', 'avatar_panel.css', 'character_panel.js', 'character_panel.css',
  'character_panel_fragment.html', 'conn_trace.js', 'style.css',
  'avatar_layout.js', 'avatar_layout.css', 'package.json', 'package-lock.json',
  'requirements.txt',
];
/** 本任务允许新增/修改的显示层文件。 */
const UI_SCOPE = ['index.html', 'ui_layout_modes.js', 'ui_layout_modes.css'];

function sha(p) {
  const b = fs.readFileSync(p);
  return { sha256: crypto.createHash('sha256').update(b).digest('hex'),
           bytes: b.length, mtime: fs.statSync(p).mtime.toISOString().replace('T', ' ').slice(0, 19) };
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
  if (FROZEN.includes(r.file)) cls = 'FROZEN';
  else if (UI_SCOPE.includes(r.file)) cls = 'UI_SCOPE';
  else if (/(\.bak|\.old|\.copy|before_|pre-)/i.test(r.file)) cls = 'LEGACY_BACKUP(pre-existing)';
  else if (/^test_/i.test(r.file)) cls = 'TEST_ASSET';
  else if (r.file.endsWith('.py')) cls = 'BACKEND';
  return { ...r, cls };
});

// 后端冻结面：按"谁在服务这个端口"精确取源，不扫 venv 全树（那是 2 万个噪声文件）。
//   8010 → runtime/avatar/app          (app.py --listenport 8010, cwd 已实测)
//   8765 → site-packages/speech_to_speech
//   8090 → 外部 llama-server.exe（非仓库源码，用 PID+create_time 锁）
//   7860 → runtime/ui/app（已在上面按 UI/FROZEN 分类）
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
      if (/(\.bak|before_|pre-)/i.test(e.name)) continue;   // 既有备份不参与门禁
      backend.push({ group: t.label, file: path.relative(REPO, full).replace(/\\/g, '/'), ...sha(full) });
    }
  })(t.dir);
}

/* 进程身份：PID + 创建时间 + 命令行 + 监听端口。任务 §0 的"不得重启 8010/8090/8765"
 * 用文件哈希证不了，只有进程世代能证。由 proc_identity.py（项目自带 python+psutil
 * 通道，避开沙箱 PowerShell 枚举盲区）单独落盘，这里只读它的产物。 */
let processIdentity = { available: false };
try {
  const p = path.join(EV, PHASE, 'PROC_IDENTITY.json');
  processIdentity = { available: true, file: p, data: JSON.parse(fs.readFileSync(p, 'utf8')) };
} catch (e) {
  processIdentity = { available: false, why: String(e.message).slice(0, 200) };
}

const report = {
  phase: PHASE,
  generatedAt: new Date().toISOString(),
  staticRoot: APP,
  counts: {
    uiAppFiles: rows.length,
    frozen: rows.filter(r => r.cls === 'FROZEN').length,
    uiScope: rows.filter(r => r.cls === 'UI_SCOPE').length,
    backendFiles: backend.length,
  },
  uiApp: rows.sort((a, b) => a.file.localeCompare(b.file)),
  backend: backend.sort((a, b) => a.file.localeCompare(b.file)),
  processIdentity,
};

fs.mkdirSync(OUTDIR, { recursive: true });
const outFile = path.join(EV, PHASE === 'prechange' ? 'PRE_CHANGE_MANIFEST.json' : 'POST_CHANGE_MANIFEST.json');
fs.writeFileSync(outFile, JSON.stringify(report, null, 2));
console.log('wrote', outFile);
console.log('uiAppFiles=%d frozen=%d uiScope=%d backendFiles=%d',
  report.counts.uiAppFiles, report.counts.frozen, report.counts.uiScope, report.counts.backendFiles);

// 摘要：本任务作用域文件 + 冻结文件的哈希，直接打印便于比对
for (const r of report.uiApp.filter(r => r.cls === 'UI_SCOPE' || r.cls === 'FROZEN')) {
  console.log(`  ${r.file.padEnd(36)} ${r.cls.padEnd(9)} ${r.sha256.slice(0, 16)} ${r.bytes}`);
}
