/*
 * move_proof_offline.cjs —— 「原样迁入 / 是同一个控件、不是复制品」的静态证明（纯离线）
 *
 * 为什么需要这个文件：运行时对拍（dock_inventory / gate_diff）能证明
 * "改后 DOM 里控件数与内容签名一致"，但当某个容器的 children 数真的变了
 * （.gf-controls 6 → 5），只靠运行时数字说不清"少掉的那一个去哪了"。
 * 这里用改动前留在磁盘上的原文副本 index.html.bak_dock01（其 sha256 与
 * PRE_CHANGE_MANIFEST 记录的 index.html 前缀逐位相同）做逐字节对账：
 *
 *   P1  搬移块在现文里恰好出现 1 次  ⇒ 是【移动】，不是复制
 *   P2  每一块落在 Dock 的哪一个分组里（按字节偏移判定，不靠肉眼读注释）
 *   P3  .gf-controls 的每个直接子元素（HTML 层）：改前改后逐字节相同 ⇒ 源文件没删控件
 *   P4  id 多重集：丢失 = ∅；新增只允许 {gf-dock, gf-dock-screen-slot}；无重复
 *   P5  Dock 里【新写的那部分】（把搬移块从 Dock 区间里减掉）零交互标签
 *       ⇒ 交互控件总数的唯一来源就是搬进来的那些，Dock 自己没造新控件
 *   P6  缓存串号：改过的资源必须换串号，没改的（avatar_layout.js）必须【不换】
 *   P7  stage 区 round-trip：head + 各块 + tail 必须逐字节还原原文
 *   P8  运行时快照里 .gf-controls children 6 → 5 的归属：那一个是 JS 创建的
 *       入口按钮，本轮把它的挂载点从 .gf-controls 改成 Dock 的「画面」slot
 *       （HTML 里从来没有这个 id + 单实例守卫 + 零 cloneNode/insertAdjacentHTML）
 *
 * 用法：node runtime/tests/ui_detail_left_dock_01/move_proof_offline.cjs
 */
'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const ROOT = path.resolve(__dirname, '..', '..', '..');
const APP = path.join(ROOT, 'runtime', 'ui', 'app');
const EV = path.join(ROOT, 'runtime', 'evidence', 'UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01');
const PRE_F = path.join(APP, 'index.html.bak_dock01');
const NOW_F = path.join(APP, 'index.html');

const sha16 = (buf) => crypto.createHash('sha256').update(buf).digest('hex').slice(0, 16);
const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
  'link', 'meta', 'param', 'source', 'track', 'wbr']);

function must(cond, msg) { if (!cond) { console.log('ABORT: ' + msg); process.exitCode = 1; throw new Error(msg); } }

const PRE_BUF = fs.readFileSync(PRE_F);
const NOW_BUF = fs.readFileSync(NOW_F);
const PRE = PRE_BUF.toString('utf8');
const NOW = NOW_BUF.toString('utf8');

/* 与磁盘基线对齐：本脚本的所有推论都建立在"这份 .bak 就是改前的 index.html"上 */
let faRows = [];
try { faRows = (JSON.parse(fs.readFileSync(path.join(EV, 'FILE_AUDIT_OFFLINE.json'), 'utf8')).rows) || []; } catch (_) {}
const faIndex = faRows.find((r) => r.file === 'index.html') || {};
const baselines = {
  pre_bak_sha16: sha16(PRE_BUF), pre_manifest_sha16: faIndex.pre_sha || null,
  now_sha16: sha16(NOW_BUF), now_manifest_sha16: faIndex.now_sha || null,
  post_manifest_sha16: faIndex.post_sha || null,
};
must(baselines.pre_bak_sha16 === baselines.pre_manifest_sha16,
  'index.html.bak_dock01 的 sha256 与 PRE 基线不一致，不能用它做改前原文');
must(baselines.now_sha16 === baselines.now_manifest_sha16,
  'index.html 现文 sha256 与 FILE_AUDIT_OFFLINE 的 now 不一致（证据文件已过期，先重跑 file_audit）');

/* --------------------------------------------------------- 通用小工具 */
function lineStart(s, i) { return s.lastIndexOf('\n', i - 1) + 1; }
function uniqIdx(s, anchor, label) {
  const n = s.split(anchor).length - 1;
  must(n === 1, '锚点 ' + label + ' 出现 ' + n + ' 次（需要恰好 1）');
  return s.indexOf(anchor);
}

/* 找到 openIdx 处那个元素的完整外区间 [openIdx, closeEnd)。
 * 同名标签做深度计数 —— 本文件里 div/section/aside/main/p 都不是自闭合。 */
function elementRange(text, openIdx) {
  const m = /^<\s*([a-zA-Z][\w-]*)/.exec(text.slice(openIdx, openIdx + 30));
  must(m, 'openIdx 处不是元素起始');
  const tag = m[1];
  const openEnd = text.indexOf('>', openIdx);
  must(openEnd > 0, '找不到开标签的 >');
  const openLen = openEnd + 1 - openIdx;
  const openTag = text.slice(openIdx, openEnd + 1);
  if (VOID.has(tag.toLowerCase()) || /\/>\s*$/.test(text.slice(openIdx, openEnd + 1))) {
    return { tag, start: openIdx, end: openEnd + 1, openLen, openTag, closeLen: 0, src: openTag };
  }
  const re = new RegExp('<\\s*' + tag + '(\\s[^>]*)?>|<\\s*/\\s*' + tag + '\\s*>', 'gi');
  re.lastIndex = openIdx;
  let depth = 0, mm;
  while ((mm = re.exec(text))) {
    if (mm[0][1] === '/') { depth -= 1; if (depth === 0) {
      return {
        tag,
        start: openIdx,
        end: re.lastIndex,
        openLen,
        openTag,
        closeLen: re.lastIndex - mm.index,
        src: text.slice(openIdx, re.lastIndex),
      };
    } }
    else { if (/\/>\s*$/.test(mm[0])) continue; depth += 1; }
  }
  throw new Error('元素未闭合：<' + tag + ' @' + openIdx);
}

/* HTML 注释屏蔽：只用于"新写脚手架里不得有交互标签"这类判定，
 * 逐字符扫描，注释内容替换成等长空格以保持偏移不变。 */
function maskHtmlComments(text) {
  const out = text.split('');
  let i = 0;
  while (i < text.length) {
    if (text.startsWith('<!--', i)) {
      const j = text.indexOf('-->', i);
      const end = j < 0 ? text.length : j + 3;
      for (let k = i; k < end; k++) if (out[k] !== '\n') out[k] = ' ';
      i = end;
    } else i++;
  }
  return out.join('');
}

/* --------------------------------------------------- P7 stage round-trip */
const mStart = uniqIdx(PRE, '<main class="stage">', 'main.stage-open');
const mClose = uniqIdx(PRE, '</main>', 'main.stage-close');
const region = PRE.slice(mStart, mClose + '</main>'.length);

const A = [
  ['orb', '<div class="orb-wrap">'],
  ['caps', '<p id="circle-caption"'],
  ['queue', '<div id="queue-actions"'],
  ['gfctl', '<!-- 女友版控件条'],
  ['mount', '<!-- 数字人面板挂在这里'],
  ['perf', '<section id="gf-voice-performance"'],
  ['conv', '<section id="gf-live-conversation"'],
  ['comp', '<!-- 文字聊天'],
];
const pos = A.map(([n, a]) => [n, uniqIdx(region, a, n)]);
pos.push(['end', region.indexOf('</main>')]);
must(pos.map((p) => p[0]).join(',') === A.map((p) => p[0]).concat('end').join(','),
  'stage 锚点顺序与预期不符：' + pos.map((p) => p[0]).join(','));

const blocks = {};
const head = region.slice(0, pos[0][1]);
for (let i = 0; i < pos.length - 1; i++) blocks[A[i][0]] = region.slice(pos[i][1], pos[i + 1][1]);
const tail = region.slice(pos[pos.length - 1][1]);
const roundTrip = head + A.map(([n]) => blocks[n]).join('') + tail;
must(roundTrip === region, 'stage 区 round-trip 不逐字节相等（切片有丢失）');
must(head.length + tail.length + A.reduce((s, [n]) => s + blocks[n].length, 0) === region.length,
  'stage 区加法不闭合：head+tail+8 块 != region');

/* status aside（在 </main> 之后、<footer> 之前的那个独立 aside） */
const sStart = uniqIdx(PRE, '<aside id="gf-status-container"', 'status-aside');
const sStartLine = lineStart(PRE, sStart);
const ftr = uniqIdx(PRE, '<footer class="footer">', 'footer');
const statusBlock = PRE.slice(sStartLine, ftr).replace(/\s+$/, '') + '\n';
must(statusBlock.includes('gf-avatar-status'), 'status aside 切片看起来不对');
blocks.status = statusBlock;

/* -------------------------------------------- Dock 区间与五个分组的位置 */
const dockOpen = uniqIdx(NOW, '<aside id="gf-dock"', 'aside#gf-dock');
const dock = elementRange(NOW, dockOpen);
const groupMarkers = ['gf-dock-g-live', 'gf-dock-g-screen', 'gf-dock-g-io', 'gf-dock-g-perf', 'gf-dock-g-chat']
  .map((cls) => ({ cls, at: NOW.indexOf(cls, dock.start) }))
  .filter((g) => g.at > 0);
must(groupMarkers.length === 5, 'Dock 里找不到 5 个分组标记，只找到 ' + groupMarkers.length);
groupMarkers.sort((x, y) => x.at - y.at);
must(groupMarkers[0].at >= dock.start && groupMarkers[4].at < dock.end,
  '分组标记不在 Dock 元素区间内');

function groupOf(offset) {
  let name = null;
  for (const g of groupMarkers) if (offset >= g.at) name = g.cls;
  return name;
}
function tagLabel(range) {
  /* 只用【开标签本身】取 id/class：拿整段 src 匹配会把后代的 id 当成这个元素的 id
   * （第一轮就出过这种假祖先：div#mic-gate.orb-wrap） */
  const cls = /\bclass="([^"]*)"/.exec(range.openTag);
  const id = /\bid="([^"]*)"/.exec(range.openTag);
  return range.tag.toLowerCase() + (id ? '#' + id[1] : '') + (cls ? '.' + cls[1].trim().replace(/\s+/g, '.') : '');
}
function ancestorChain(offset) {
  const found = [];
  try {
    for (const tag of ['aside', 'section', 'div']) {
      const re = new RegExp('<\\s*' + tag + '\\b', 'g');
      let m2;
      while ((m2 = re.exec(NOW))) {
        if (m2.index > offset) break;
        if (m2.index < dock.start) continue;
        const range = elementRange(NOW, m2.index);
        if (range.start <= offset && offset < range.end) found.push({ range, span: range.end - range.start });
      }
    }
  } catch (_) { found.push({ bogus: true }); }
  found.sort((a, b) => b.span - a.span); /* 外 → 内 */
  return found.slice(-3).map((f) => (f.range ? tagLabel(f.range) : '<ERR>'));
}

/* ------------------------------------------------------- P1 + P2 搬移块 */
const moved = [];
for (const [name, blk] of Object.entries(blocks)) {
  const probe = blk.replace(/\n+$/, '');
  const count = NOW.split(probe).length - 1;
  must(count === 1, '块 ' + name + ' 在现文里出现 ' + count + ' 次（需要 1 次 ⇒ 移动而非复制）');
  const at = NOW.indexOf(probe);
  moved.push({
    name, bytes: probe.length, occurrences_in_now: count,
    inside_dock: at >= dock.start && at < dock.end,
    group: groupOf(at), ancestors: ancestorChain(at),
    sha16_of_block: sha16(Buffer.from(probe, 'utf8')),
  });
}
must(moved.every((m) => m.inside_dock), '有搬移块不在 #gf-dock 区间内：'
  + moved.filter((m) => !m.inside_dock).map((m) => m.name).join(','));
const byGroup = {};
for (const m of moved) if (m.group) (byGroup[m.group] = byGroup[m.group] || []).push(m.name);
const expectedGroup = {
  'gf-dock-g-live': ['orb', 'caps', 'queue'],
  'gf-dock-g-io': ['gfctl', 'mount', 'status'],
  'gf-dock-g-perf': ['perf'],
  'gf-dock-g-chat': ['conv', 'comp'],
};
const groupCheck = Object.entries(expectedGroup).map(([g, names]) => ({
  group: g, expected: names, observed: (byGroup[g] || []).slice().sort(),
  ok: JSON.stringify((byGroup[g] || []).slice().sort()) === JSON.stringify(names.slice().sort()),
}));
must(groupCheck.every((c) => c.ok), '分组归属与预期不符：' + JSON.stringify(groupCheck));
must((byGroup['gf-dock-g-screen'] || []).length === 0,
  '「画面」组在 HTML 里应当是空的（入口与面板由 ui_layout_modes.js 运行时挂进 slot）');

/* ------------------------------- P3 .gf-controls 直接子元素逐一对账 */
function directChildren(text, containerStart) {
  const r = elementRange(text, containerStart);
  const closeStart = r.end - r.closeLen;
  const kids = [];
  let i = r.start + r.openLen;
  while (i < closeStart) {
    const lt = text.indexOf('<', i);
    if (lt < 0 || lt >= closeStart) break;
    if (text[lt + 1] === '/') { i = lt + 1; continue; }
    if (!/^[a-zA-Z]/.test(text[lt + 1] || '')) { i = lt + 1; continue; }
    const range = elementRange(text, lt);
    kids.push(range);
    i = Math.max(range.end, lt + 1);
  }
  return kids.map((range) => {
    const id = /\bid="([^"]*)"/.exec(range.openTag);
    return {
      key: tagLabel(range),
      tag: range.tag, id: id ? id[1] : null,
      bytes: range.src.length, sha16: sha16(Buffer.from(range.src, 'utf8')), src: range.src,
    };
  });
}
const gcPreIdx = uniqIdx(PRE, '<div class="gf-controls">', 'pre gf-controls');
const gcNowIdx = NOW.indexOf('<div class="gf-controls">');
must(gcNowIdx > 0, '现文里找不到 .gf-controls');
const preKids = directChildren(PRE, gcPreIdx);
const nowKids = directChildren(NOW, gcNowIdx);
const preShaSet = new Set(nowKids.map((k) => k.sha16));
const childReconcile = preKids.map((k) => {
  const stillThere = preShaSet.has(k.sha16);
  const elsewhere = stillThere ? null : NOW.split(k.src).length - 1;
  const at = elsewhere === 1 ? NOW.indexOf(k.src) : -1;
  return {
    key: k.key, bytes: k.bytes, sha16_pre: k.sha16,
    still_child_of_gf_controls: stillThere,
    occurrences_in_whole_doc: stillThere ? nowKids.filter((x) => x.sha16 === k.sha16).length : elsewhere,
    relocated_to: stillThere ? null : (at >= 0 ? { group: groupOf(at), ancestors: ancestorChain(at), inside_dock: at >= dock.start && at < dock.end } : null),
  };
});
const lostChildren = childReconcile.filter((c) => !c.still_child_of_gf_controls
  && !(c.relocated_to && c.relocated_to.inside_dock && c.occurrences_in_whole_doc === 1));
must(lostChildren.length === 0, '.gf-controls 有子元素对不上账：' + JSON.stringify(lostChildren.map((c) => c.key)));
/* 不要求数量相等：数量可以变（本轮就变了 6→5），但差集必须逐字节在 Dock 里
 * 出现恰好 1 次（上面的 lostChildren 已经把"对不上账"的情况拦下），
 * 并且反向也要查：现文里多出来的子元素必须是 0 个。 */
const nowShaSet = new Set(preKids.map((k) => k.sha16));
const newOnlyChildren = nowKids.filter((k) => !nowShaSet.has(k.sha16)
  && !(k.sha16 && childReconcile.some((c) => c.sha16_pre === k.sha16)));
must(newOnlyChildren.length === 0, '.gf-controls 里凭空多出子元素：'
  + newOnlyChildren.map((k) => k.key).join(','));

/* ------------------- P8 运行时 6→5 的归属：入口按钮是 JS 建的，挂载点变了 */
/* 复用上场的那套注释感知扫描器（与 static_scan_selftest 同一抽取方式），
 * 不复用就等于第二套实现 —— 那正是任务书要避免的事。 */
const accSrc = fs.readFileSync(path.join(__dirname, 'dock_acceptance.cjs'), 'utf8');
const sf = accSrc.indexOf('const NET_CALLSITE_RE');
const st = accSrc.indexOf('const LAYOUT_STACK_RE');
must(sf >= 0 && st > sf, '抽不出 dock_acceptance.cjs 里的扫描器');
// eslint-disable-next-line no-new-func
const scanner = new Function('fs', 'path',
  accSrc.slice(sf, st) + '\nreturn {maskComments, codeHitsIn};')(fs, path);
const JS_F = path.join(APP, 'ui_layout_modes.js');
const JS = fs.readFileSync(JS_F, 'utf8');
const jsMasked = scanner.maskComments(JS, 'js').masked;
/* 正对照：同一把尺子必须数得出来真的脏文件（否则 0 命中只是探针坏了） */
const positiveControl = scanner.codeHitsIn(
  fs.readFileSync(path.join(APP, 'avatar-sync.js'), 'utf8'), 'js');
must(positiveControl > 0, '正对照失效：avatar-sync.js 应当有命中，却数到 ' + positiveControl);
const hits = (re) => (jsMasked.match(new RegExp(re.source, 'g')) || []).length;
const codeOnly = (re) => (jsMasked.match(new RegExp(re.source, 'gm')) || []);
const entryIsJsCreated = !/id="gf-layout-open"/.test(PRE) && !/id="gf-layout-open"/.test(NOW);
/* 把静态数字和运行时数字钉在一起（不靠叙述）：从两份运行时清单里读 .gf-controls 的 children */
let rtKids = { before: null, after: null };
try {
  const rb = JSON.parse(fs.readFileSync(path.join(EV, 'CONTROL_INVENTORY_BEFORE.json'), 'utf8'));
  const ra = JSON.parse(fs.readFileSync(path.join(EV, 'CONTROL_INVENTORY_AFTER.json'), 'utf8'));
  const pick = (o) => (o.controls || []).find((c) => c.sel === '.gf-controls');
  rtKids = { before: pick(rb) && pick(rb).children, after: pick(ra) && pick(ra).children };
} catch (_) { rtKids.note = '读不到运行时清单，桥接数字未核对'; }
const bridgeOk = rtKids.note ? null
  : (preKids.length + 1 === rtKids.before && nowKids.length + 0 === rtKids.after);
const mountAnchors = [
  { name: '入口挂 Dock 的「画面」slot', re: /slot\.prepend\(btn\)/, must: true },
  { name: '面板同一实例改挂 slot', re: /slot\.appendChild\(panel\)/, must: true },
  { name: '无 slot 时逐字回退到旧路径', re: /document\.body\.appendChild\(panel\)/, must: true },
  { name: '入口单实例守卫', re: /if \(entry \|\| document\.getElementById\('gf-layout-open'\)\) return;/, must: true },
  { name: '面板单实例守卫', re: /if \(panel\) return;/, must: true },
].map((a) => ({ ...a, found: codeOnly(a.re).length }));
must(mountAnchors.every((a) => !a.must || a.found === 1),
  '挂载锚点异常：' + JSON.stringify(mountAnchors.map((a) => [a.name, a.found])));
const copyConstructs = {
  cloneNode: hits(/cloneNode\s*\(/),
  innerHTML_assignment: hits(/\.innerHTML\s*=/),
  insertAdjacentHTML: hits(/insertAdjacentHTML\s*\(/),
};
must(copyConstructs.cloneNode === 0 && copyConstructs.insertAdjacentHTML === 0,
  'ui_layout_modes.js 出现复制节点的写法：' + JSON.stringify(copyConstructs));
must(bridgeOk !== false, '静态/运行时 children 数字不咬合：' + JSON.stringify(rtKids)
  + ' vs html ' + preKids.length + '→' + nowKids.length);

/* ------------------------------------------------------ P4 id 多重集 */
const ids = (t) => (t.match(/\bid="([^"]+)"/g) || []).map((s) => s.slice(4, -1));
const idsPre = ids(PRE), idsNow = ids(NOW);
const cnt = (arr, x) => arr.filter((v) => v === x).length;
const removedIds = [...new Set(idsPre)].filter((i) => cnt(idsNow, i) < cnt(idsPre, i));
const addedIds = [...new Set(idsNow)].filter((i) => cnt(idsNow, i) > cnt(idsPre, i));
const dupIds = [...new Set(idsNow)].filter((i) => cnt(idsNow, i) !== 1);
must(removedIds.length === 0, 'id 丢失：' + removedIds.join(','));
must(JSON.stringify(addedIds.slice().sort()) === JSON.stringify(['gf-dock', 'gf-dock-screen-slot']),
  '新增 id 超出允许集合：' + addedIds.join(','));
must(dupIds.length === 0, 'id 重复：' + dupIds.join(','));

/* ------------------------------- P5 Dock 里"新写的那部分"零交互标签 */
let scaffold = NOW.slice(dock.start, dock.end);
for (const m of moved) {
  const blk = blocks[m.name].replace(/\n+$/, '');
  scaffold = scaffold.split(blk).join('');
}
const scaffoldMasked = maskHtmlComments(scaffold);
const interactiveTags = {};
for (const t of ['button', 'input', 'select', 'textarea', 'a', 'label', 'option']) {
  const n = (scaffoldMasked.match(new RegExp('<\\s*' + t + '\\b', 'gi')) || []).length;
  if (n) interactiveTags[t] = n;
}
const interactiveTotal = Object.values(interactiveTags).reduce((a, b) => a + b, 0);
const contentEditable = (scaffoldMasked.match(/contenteditable/gi) || []).length;
const newJsHandlers = (scaffoldMasked.match(/\bon(click|input|change|keydown|submit)\s*=/gi) || []).length;
must(interactiveTotal === 0 && contentEditable === 0 && newJsHandlers === 0,
  'Dock 新写部分含交互标签/内联事件：' + JSON.stringify({ interactiveTags, contentEditable, newJsHandlers }));
const containerTags = {};
for (const t of ['aside', 'div', 'section', 'h2']) {
  const n = (scaffoldMasked.match(new RegExp('<\\s*' + t + '\\b', 'gi')) || []).length;
  if (n) containerTags[t] = n;
}

/* --------------------------------------------------- P6 缓存串号 */
function linkVersions(text) {
  const out = {};
  /* 路径部分必须排除 ? 与 &，否则贪婪匹配会把串号吞进文件名里（第一轮就是这么错的） */
  for (const m of text.matchAll(/<(?:link[^>]*href|script[^>]*src)="([^"?&]+)(?:\?v=([^"&]+))?"[^>]*>/g)) {
    const file = m[1].replace(/^\/+/, '');
    if (!/^(ui_layout_modes\.css|gf_dock\.css|ui_layout_modes\.js|avatar_layout\.js|index\.html|main\.js|style\.css|avatar-sync\.js)$/.test(file)) continue;
    out[file] = { v: m[2] || null, tag: m[0][1] === 'l' ? 'link' : 'script' };
  }
  return out;
}
const vPre = linkVersions(PRE), vNow = linkVersions(NOW);
const cacheBuster = Object.keys(vNow).map((f) => {
  const before = vPre[f] ? vPre[f].v : '<ABSENT>';
  const exists = fs.existsSync(path.join(APP, f));
  const sha = exists ? sha16(fs.readFileSync(path.join(APP, f))) : null;
  return { file: f, v_before: before, v_now: vNow[f].v, changed: before !== vNow[f].v, on_disk: exists, sha16_now: sha };
});
/* 改过的三个文件里，被 ?v= 引用的那两个必须换串号；没改的必须不换 */
const CB = Object.fromEntries(cacheBuster.map((c) => [c.file, c]));
const rules = [
  { file: 'gf_dock.css', expect_changed: true, why: '本轮新增文件，必须被引用且带串号' },
  { file: 'ui_layout_modes.js', expect_changed: true, why: '本轮改过 ⇒ 串号必须换，否则浏览器还在用旧的' },
  { file: 'ui_layout_modes.css', expect_changed: true, why: '本轮改过 ⇒ 串号必须换' },
  { file: 'avatar_layout.js', expect_changed: false, why: '第 9 条：文件逐字未动 ⇒ 串号【刻意不动】，换了反而误导' },
  { file: 'main.js', expect_changed: false, why: '范围外文件未动 ⇒ 串号不得动' },
  { file: 'avatar-sync.js', expect_changed: false, why: '范围外文件未动 ⇒ 串号不得动' },
  { file: 'style.css', expect_changed: false, why: '范围外文件未动 ⇒ 串号不得动' },
].map((r) => ({
  ...r,
  observed: CB[r.file] ? CB[r.file].v_now : '<NOT REFERENCED IN index.html>',
  /* 要求"变"的必须被引用；要求"不变"的若压根没被引用，也不算违反（它本来就不由 index.html 装载） */
  satisfied: r.expect_changed
    ? !!(CB[r.file] && CB[r.file].changed === true)
    : (!CB[r.file] || CB[r.file].changed === false),
  referenced: !!CB[r.file],
}));
must(rules.every((r) => r.satisfied), '缓存串号规则不满足：' + JSON.stringify(rules.filter((r) => !r.satisfied)));

/* ------------------------------------------------------------------ 输出 */
const out = {
  task_id: 'AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01',
  probe: 'move_proof_offline.cjs（静态：改前原文副本 vs 现文，逐字节对账；不读 HTTP、不开浏览器）',
  generated_at: new Date().toISOString(),
  inputs: { pre_file: 'index.html.bak_dock01', now_file: 'index.html', baselines },
  P7_stage_round_trip: {
    ok: true, region_bytes: region.length,
    /* 分解必须自己算给自己看：head+tail + 【stage 区内】那 8 个锚点块 == region。
     * 第 9 个搬移块（status aside）来自 </main> 之后，不在这一区里 ⇒ 单列，不混计数。
     * 上一版把这里写成 blocks: 9，读起来像"stage 区由 9 块拼成"，属表述缺陷。 */
    blocks_in_stage_region: A.length,
    blocks_outside_stage_region: 1,
    head_tail_bytes: head.length + tail.length,
    stage_block_bytes_sum: A.reduce((s, [n]) => s + blocks[n].length, 0),
    arithmetic_holds: head.length + tail.length
      + A.reduce((s, [n]) => s + blocks[n].length, 0) === region.length,
    status_aside_bytes_with_trailing_newline: blocks.status.length,
    block_bytes: Object.fromEntries(Object.entries(blocks).map(([k, v]) => [k, v.length])),
    byte_convention_note: 'P1 的 bytes 用【去尾部空白的探针文本】长度，本节的 block_bytes 用'
      + '【region 原样切片】长度，二者只在行尾换行/尾随空白上差 0–1 字节（status：371 vs 372）。'
      + '两个口径都对，只是量的对象不同；本节参与加法的是 block_bytes。',
  },
  P1_P2_moved_blocks: moved,
  P3_gf_controls_children: {
    before: preKids.length, after: nowKids.length,
    reconciliation: childReconcile.map((c) => ({ ...c, src: undefined })),
    lost_or_unexplained: lostChildren.map((c) => c.key),
    html_only_note: '本节是【HTML 源文件】层面的对账：改前/改后 .gf-controls 的直接子元素'
      + '各 ' + preKids.length + ' / ' + nowKids.length + ' 个且逐字节相同。'
      + '运行时快照里它从 6 变 5 与 HTML 无关，归属见 P8。',
    arithmetic_bridge: 'HTML 静态子元素 ' + preKids.length + ' 个 + JS 现场创建的入口 1 个 = 运行时改前 6 个；'
      + '改后入口不再挂这里 ⇒ ' + nowKids.length + ' + 0 = 运行时 5 个。数字闭合。',
  },
  P8_runtime_delta_attribution: {
    question: '运行时 .gf-controls 的 children 6 → 5，是谁少了一个？',
    answer_sel: '#gf-layout-open（「画面布局」入口按钮）',
    entry_is_js_created_not_in_html: entryIsJsCreated,
    evidence: 'index.html 改前/改后都不含 id="gf-layout-open"（'
      + (!/id="gf-layout-open"/.test(PRE) ? '改前无' : '改前有') + ' / '
      + (!/id="gf-layout-open"/.test(NOW) ? '改后无' : '改后有') + '）；'
      + '它由 ui_layout_modes.js 的 buildEntry() 创建，本轮把挂载点从 .gf-controls 改成 Dock 的「画面」slot。',
    mount_anchors: mountAnchors,
    single_instance_guards: mountAnchors.filter((a) => a.name.indexOf('守卫') >= 0)
      .map((a) => ({ name: a.name, occurrences: a.found })),
    copy_construct_hits: copyConstructs,
    scanner_positive_control_hits: positiveControl,
    static_vs_runtime_bridge: {
      html_children_before: preKids.length, html_children_after: nowKids.length,
      runtime_children_before: rtKids.before, runtime_children_after: rtKids.after,
      js_created_entry_children: 1, bridge_holds: bridgeOk,
      note: rtKids.note || '静态 children + 那 1 个 JS 创建的入口 = 运行时 children（两侧数字互相咬合）',
    },
    cross_check: '运行时侧对应事实：dock_inventory 的 #gf-layout-open 行 = BYTE_IDENTICAL 且 '
      + 'parentSel div.gf-controls → div#gf-dock-screen-slot.gf-dock-body，监听数与类型不变（gate_diff.R11-3）。',
  },
  P4_ids: { total_before: idsPre.length, total_after: idsNow.length, removed: removedIds, added: addedIds, duplicates: dupIds },
  P5_new_scaffold: {
    bytes_after_removing_moved_blocks: scaffold.length,
    interactive_tags: interactiveTags, interactive_total: interactiveTotal,
    contenteditable: contentEditable, inline_on_handlers: newJsHandlers,
    container_tags_only: containerTags,
    masking_preserves_length: scaffold.length === scaffoldMasked.length,
  },
  P6_cache_busters: { per_file: cacheBuster, rules },
  VERDICT: 'MOVED_NOT_COPIED = TRUE',
};
fs.writeFileSync(path.join(EV, 'MOVE_PROOF_OFFLINE.json'), JSON.stringify(out, null, 2), 'utf8');

console.log('wrote runtime/evidence/UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01/MOVE_PROOF_OFFLINE.json');
console.log('P7 stage 区逐字节还原：' + region.length + ' B = head+tail ' + (head.length + tail.length)
  + ' B + 区内 ' + A.length + ' 块 ' + A.reduce((s, [n]) => s + blocks[n].length, 0)
  + ' B（第 9 块 status aside ' + blocks.status.length + ' B 来自 </main> 之后，不在这一区）');
for (const m of moved) console.log('  P1/P2 ' + m.name.padEnd(7) + String(m.bytes).padStart(5) + ' B  出现 ' + m.occurrences_in_now + ' 次  组 ' + (m.group || '-') + '  ' + m.ancestors.join(' > '));
console.log('P3 .gf-controls 子元素（HTML 层）' + preKids.length + ' → ' + nowKids.length
  + '；未对上的 ' + lostChildren.length + ' 个（' + (lostChildren.map((c) => c.key).join(',') || '-') + '）');
for (const c of childReconcile.filter((x) => !x.still_child_of_gf_controls)) {
  console.log('    relocated: ' + c.key + ' (' + c.bytes + ' B, sha ' + c.sha16_pre + ') → '
    + (c.relocated_to ? c.relocated_to.group + ' / ' + c.relocated_to.ancestors.join(' > ') : 'NOT FOUND'));
}
console.log('P4 id ' + idsPre.length + ' → ' + idsNow.length + '；removed=' + (removedIds.join(',') || '(none)')
  + '  added=' + addedIds.join(',') + '  dup=' + (dupIds.join(',') || '(none)'));
console.log('P5 Dock 新写部分 ' + scaffold.length + ' B：交互标签 ' + interactiveTotal
  + '，contenteditable ' + contentEditable + '，内联 on* ' + newJsHandlers + '；容器 ' + JSON.stringify(containerTags));
console.log('P6 串号规则：' + rules.map((r) => r.file + (r.satisfied ? '=OK' : '=BAD') + '(' + (r.observed || '-') + ')').join('  '));
console.log('P8 运行时 6→5 归属：' + out.P8_runtime_delta_attribution.answer_sel
  + '（HTML 里没有这个 id = ' + entryIsJsCreated + '；cloneNode ' + copyConstructs.cloneNode
  + ' / insertAdjacentHTML ' + copyConstructs.insertAdjacentHTML + '；正对照命中 ' + positiveControl + '）');
console.log('');
console.log('VERDICT: ' + out.VERDICT);
