/* ============================================================================
 * shadow_acceptance.cjs — 任务 §17 A–T 自动化验收（用户不参与）
 *
 * 关键前提：必须有一个"活着且在播放"的人物视频，§10 的身份闸门才有意义
 * （空 video 上 currentTime 恒为 0，切不动也测不出切坏了）。所以脚本先像真实
 * 用户那样派发一次 DOM click —— 命中的是 avatar-sync.js 自己的
 *   document.addEventListener('click', kick, true)   // 首次交互才 /offer
 * 这是【测试夹具动作】，不注入、不改动任何生产代码路径。
 *
 *   node shadow_acceptance.cjs            → 1920x1080 全量 A–T
 * ==========================================================================*/
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { connect, Tab, sleep } = require('./cdp.cjs');
const prefs = require('./ui_prefs.cjs');

const EV = path.join(__dirname, '..', '..', 'evidence', 'UI_DUAL_VIEWMODE_01');
const SHOT = path.join(EV, 'SCREENSHOTS');

const results = [];
const LIVE_LOG = path.join(EV, 'ACCEPTANCE_PROGRESS.jsonl');
function log(line) {
  // Node 的 stdout 一进管道就是全缓冲，前台看不到进度；进度同时逐行落盘，
  // 这样长时间运行也能随时回读，不必等进程结束。
  try { fs.appendFileSync(LIVE_LOG, line + '\n'); } catch (_) { /* 日志不致命 */ }
  console.log(line);
}
function check(id, name, ok, detail) {
  results.push({ id, name, verdict: ok ? 'PASS' : 'FAIL', detail });
  log(`${ok ? 'PASS' : 'FAIL'}  ${id.padEnd(4)} ${name}`);
  if (detail !== undefined) log('        ' + JSON.stringify(detail).slice(0, 900));
  // 每个判定落地后就覆写结果文件：中途崩了也留有到那一步为止的真实进度。
  try {
    fs.writeFileSync(path.join(EV, 'VIEWMODE_TEST.json'), JSON.stringify({
      captured_at: new Date().toISOString(), incremental: true, results,
    }, null, 2));
  } catch (_) { /* 落盘失败由最后的总写入暴露 */ }
  return ok;
}

/* 页面侧探针：一次拿全布局/身份/几何事实，避免多次往返之间的时间差。 */
const PROBE = `(() => {
  const b = document.body, cs = getComputedStyle(b);
  const rv = (el) => { if (!el) return null; const r = el.getBoundingClientRect();
    return { x:+r.x.toFixed(1), y:+r.y.toFixed(1), w:+r.width.toFixed(1), h:+r.height.toFixed(1) }; };
  const v = document.getElementById('av-video');
  const layer = document.getElementById('av-layer');
  const app = document.getElementById('app');
  const dv = document.getElementById('uivm-divider');
  const pn = document.getElementById('uivm-panel');
  const gs = document.getElementById('gf-status-container');
  const controls = document.querySelector('.gf-controls');
  const micGate = document.getElementById('mic-gate');
  const topbarEl = document.querySelector('header.topbar');
  const convEl = document.getElementById('gf-live-conversation');
  const pipEl = document.getElementById('cam-pip');
  /* §13「控件不得压住人物/既有控件」要量的是【遮挡像素】，不是【矩形相交】。
   * 例：WebRTC 通话中 style.css 把 "body.rtc-live .mic-gate-arc" 设成 display:none，
   * 于是 #mic-gate 塌成 2×52 的空壳 —— 拿它的矩形来判"遮挡"，闸门量的是一个
   * 屏幕上根本不存在的东西；反过来若把它硬编进白名单，就是在给闸门放水。
   * 所以这里现场判"这坨到底有没有像素"：自身有背景/边框，或子孙里有看得见的
   * 元素/文本 ⇒ 算；空壳 ⇒ 不算，并且把每个目标的判定结果一并写进 detail。 */
  const ink = (el) => {
    if (!el) return false;
    const c = getComputedStyle(el);
    if (c.display === 'none' || c.visibility === 'hidden' || parseFloat(c.opacity) === 0) return false;
    const bg = c.backgroundColor;
    if (bg && bg !== 'transparent' && bg !== 'rgba(0, 0, 0, 0)') return true;
    if ((parseFloat(c.borderTopWidth) || 0) > 0 && c.borderTopStyle !== 'none') return true;
    /* 媒体/控件类标签本身就是一片像素，不看它有没有背景 */
    const tag = el.tagName;
    if (tag === 'VIDEO' || tag === 'IMG' || tag === 'CANVAS' || tag === 'IFRAME'
      || tag === 'BUTTON' || tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return true;
    if (el.children.length === 0 && el.textContent.trim().length > 0) return true;
    for (let i = 0; i < el.children.length; i++) if (ink(el.children[i])) return true;
    return false;
  };
  const PV = rv(pn);
  const ov = (el) => {                       // 与面板的相交面积（px²）；面板或目标缺席⇒null
    if (!PV || !el || !ink(el)) return 0;
    const r = el.getBoundingClientRect();
    const x = Math.max(0, Math.min(PV.x + PV.w, r.x + r.width) - Math.max(PV.x, r.x));
    const y = Math.max(0, Math.min(PV.y + PV.h, r.y + r.height) - Math.max(PV.y, r.y));
    return +(x * y).toFixed(0);
  };
  const st = window.UiViewModes && window.UiViewModes.state;
  const ident = window.UiViewModes && window.UiViewModes.videoIdentity();
  return {
    viewMode: b.dataset.viewMode || null,
    pane: b.dataset.uivmPane || null,
    vars: { contentW: cs.getPropertyValue('--uivm-content-w').trim(),
            dividerX: cs.getPropertyValue('--uivm-divider-x').trim(),
            lvS: cs.getPropertyValue('--uivm-lv-s').trim(),
            lvX: cs.getPropertyValue('--uivm-lv-x').trim(),
            lvY: cs.getPropertyValue('--uivm-lv-y').trim(),
            stageW: cs.getPropertyValue('--uivm-stage-w').trim(),
            stageH: cs.getPropertyValue('--uivm-stage-h').trim() },
    video: rv(v), videoNat: v ? { w: v.videoWidth, h: v.videoHeight } : null,
    videoSrcObject: !!(v && v.srcObject),
    currentTime: v ? v.currentTime : null,
    layer: rv(layer), app: rv(app), divider: rv(dv), panel: rv(pn),
    stage: rv(document.querySelector('main.stage')),
    conv: rv(document.getElementById('gf-live-conversation')),
    status: rv(gs), controls: rv(controls), micGate: rv(micGate),
    topbar: rv(topbarEl), camPip: rv(pipEl),
    /* §13 遮挡闸门的原始量：面板与每个"确有像素"的目标相交多少 px² */
    ovPanel: { topbar: ov(topbarEl), conv: ov(convEl), controls: ov(controls),
               status: ov(gs), micGate: ov(micGate), camPip: ov(pipEl),
               video: ov(v), layer: ov(layer) },
    ink: { topbar: ink(topbarEl), conv: ink(convEl), controls: ink(controls),
           status: ink(gs), micGate: ink(micGate), camPip: ink(pipEl), video: ink(v) },
    layerClips: (() => { if (!layer) return null;   // 不用正则：见上方 mat 的教训
      const o = getComputedStyle(layer).overflow;
      return o.indexOf('hidden') >= 0 || o.indexOf('clip') >= 0; })(),
    objFit: v ? getComputedStyle(v).objectFit : null,
    objPos: v ? getComputedStyle(v).objectPosition : null,
    xform: v ? getComputedStyle(v).transform.slice(0, 90) : null,
    /* 计算矩阵逐项拆开：只看"transform 变了没有"是假判据 ——
     * translate() 拿到无单位数字时整条声明作废，值从 matrix(1,0,0,1,0,0)
     * 变成 "none"，也是"变了"，但渲染上什么都没发生。J 需要断言到
     * a/d=缩放倍率、e/f=平移像素，才能证明参数真的落到了画面上。
     *
     * 这里刻意【不用正则】：本探针是 Node 侧模板字符串里的页面代码，正则里的
     * 反斜杠会被模板求值吃掉一层 —— 转义过的左括号退化成字面量左括号，捕获组
     * 随之错位，parseFloat 拿到 "(" 得 NaN，NaN 经 JSON 序列化成 null，于是
     * J2 以"看起来是对的"姿势假 FAIL（真实踩过，见 page_code_hazards.cjs）。
     * 改用括号定位切片，对转义免疫。 */
    mat: (() => { if (!v) return null;
      const t = getComputedStyle(v).transform;
      const i = t.indexOf('(');
      if (t === 'none' || i < 0) return { NOT_MATRIX: t.slice(0, 40) };
      const n = t.slice(i + 1, t.lastIndexOf(')')).split(',').map((x) => parseFloat(x));
      return { kind: t.slice(0, i), a: n[0], b: n[1], c: n[2], d: n[3], e: n[4], f: n[5] }; })(),
    /* 未经 transform 的布局盒：translate 的百分比就是按它解析的 */
    videoBox: v ? { w: v.offsetWidth, h: v.offsetHeight } : null,
    dividerVisible: dv ? getComputedStyle(dv).display !== 'none' : false,
    secDetail: (() => { const e = document.querySelector('.uivm-sec-detail');
      return e ? getComputedStyle(e).display !== 'none' : null; })(),
    secLive: (() => { const e = document.querySelector('.uivm-sec-live');
      return e ? getComputedStyle(e).display !== 'none' : null; })(),
    modePressed: Array.from(document.querySelectorAll('.uivm-mode'))
      .map(x => x.dataset.mode + '=' + x.getAttribute('aria-pressed')),
    ident,
    store: (() => { try { return JSON.parse(localStorage.getItem('ai_girlfriend_ui_layout_v1')); }
                    catch (e) { return { UNPARSEABLE: String(e.message).slice(0, 60) }; } })(),
    viewport: { w: innerWidth, h: innerHeight },
  };
})()`;

/* 把 paneRatio 换算成期望人物带宽度的辅助：只用于报告，不参与判定。 */
async function probe(tab) { return tab.js(PROBE); }

/* 真实拖动：CDP 可信输入在后台标签页会被 Chrome posture 限制吃掉（pre-flight
 * 已实测），所以这里用页面内 PointerEvent 序列驱动被测代码自己的 handler。
 * 被测代码只认 e.clientX，这与真实鼠标进到手上的字段一致。
 *
 * 每一"帧"是一次独立的 CDP 往返，由 Node 侧驱动 —— 刻意【不在页面里 await
 * requestAnimationFrame】：后台标签页的 rAF 根本不触发，页面内 await 会把整段
 * 求值吊到 CDP 超时（上一轮就是这么卡死在 B 的）。跨往返之间浏览器必然已经
 * 重新算过布局，所以量到的是真实的逐步响应，不是终态。 */
async function dragDivider(tab, toX) {
  const from = await tab.js(`(() => {
    const d = document.getElementById('uivm-divider');
    if (!d) return null;
    const r = d.getBoundingClientRect();
    window.__uivmDrag = { from: Math.round(r.x + r.width / 2) };
    const mk = (type, x) => new PointerEvent(type, {
      bubbles: true, cancelable: true, view: window, pointerId: 1,
      clientX: x, clientY: Math.round(innerHeight / 2), isPrimary: true, button: 0 });
    d.dispatchEvent(mk('pointerdown', window.__uivmDrag.from));
    return window.__uivmDrag.from;
  })()`);
  if (from === null) return 'NO_DIVIDER';
  const trace = [];
  for (let i = 1; i <= 8; i++) {
    const x = Math.round(from + (toX - from) * i / 8);
    trace.push(await tab.js(`(() => {
      const d = document.getElementById('uivm-divider');
      d.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, cancelable: true,
        view: window, pointerId: 1, clientX: ${x}, clientY: Math.round(innerHeight/2),
        isPrimary: true, button: 0 }));
      const v = document.getElementById('av-video').getBoundingClientRect();
      const a = document.getElementById('app').getBoundingClientRect();
      return { x: ${x}, videoW: +v.width.toFixed(1), videoX: +v.x.toFixed(1), appW: +a.width.toFixed(1) };
    })()`));
  }
  await tab.js(`(() => {
    const d = document.getElementById('uivm-divider');
    const mk = (type) => new PointerEvent(type, { bubbles: true, cancelable: true,
      view: window, pointerId: 1, clientX: ${toX}, clientY: Math.round(innerHeight/2),
      isPrimary: true, button: 0 });
    d.dispatchEvent(mk('pointerup'));
    window.dispatchEvent(mk('pointerup'));
    return 1;
  })()`);
  return trace;
}

async function setSlider(tab, sel, value) {
  return tab.js(`(() => {
    const i = document.querySelector('${sel}');
    if (!i) return 'NO_SLIDER';
    i.value = '${value}';
    i.dispatchEvent(new Event('input', { bubbles: true }));
    return i.value;
  })()`);
}

async function clickSel(tab, sel) {
  return tab.js(`(() => { const e = document.querySelector('${sel}');
    if (!e) return 'NO_ELEMENT'; e.click(); return 'OK'; })()`);
}

(async () => {
  fs.mkdirSync(SHOT, { recursive: true });
  fs.writeFileSync(LIVE_LOG, `# started ${new Date().toISOString()}\n`);
  const conn = await connect();
  const tab = await Tab.create(conn, 'about:blank');
  await tab.setViewport(1920, 1080);
  await tab.navigate('http://127.0.0.1:7860/', 2500);

  /* ---------- 0. 先确认新代码装载了 ----------
   * 这一项在【清键之前】采样，所以 viewMode 允许是两个合法值之一：
   * 持久化的 viewMode 本来就该在刷新后被恢复（§7），"空存储时默认 detail"
   * 由下面的 A0/A 在清键之后专门判。上一版在这里硬写 === 'detail'，
   * 于是把"按 §7 正确恢复了上一轮的模式"误判成装载失败 —— 那是断言前提
   * 写错，不是产品缺陷。 */
  const loaded = await tab.js(`({
    api: typeof window.UiViewModes,
    sheet: Array.from(document.styleSheets).some(s => (s.href||'').includes('ui_layout_modes')),
    viewMode: document.body.dataset.viewMode || null,
    errors: (window.__UIVM_BOOT_ERRORS || null),
  })`);
  check('LOAD', '新增 CSS/JS 已装载 + 控制器已注册（刷新后 viewMode 必为两个合法值之一）',
    loaded.api === 'object' && loaded.sheet === true
      && ['detail', 'live-portrait'].indexOf(loaded.viewMode) !== -1 && !loaded.errors,
    { ...loaded, legalModes: ['detail', 'live-portrait'],
      note: '空存储时的默认值 = detail 由 A0/A 在清键之后判定' });

  /* ---------- A0 见下方 CLEAR 之后（必须先清掉上一轮遗留的持久化值） ---------- */

  /* ---------- 清"布局类"全部键，然后重载 = 真实"第一次进入" ----------
   * 为什么不止清本任务那一个键：#av-video 的内联 transform 由【冻结 owner】
   * avatar_layout.js 持久化（avatarVideoScale/OffsetX/OffsetY）。上一轮把它留在
   * origin 上，本轮的"首屏"就带 1.6 倍缩放 —— 截图里 DETAIL_DEFAULT 与
   * DETAIL_AVATAR_LARGE 因此退化成同一张（见 screenshots.cjs 的像素审计）。
   * §5.3"首屏=改造前"这个命题，前提是所有布局参数都处于改造前状态。
   * 改造前实测（prechange/DOM_BASELINE.json）这 4 个键【一个都不存在】，
   * 所以"回到改造前"= 全部删除；收尾时再删一次并核对业务键未变。
   * 绝不 localStorage.clear()：同一 origin 上还有 s2s.* 等业务键，
   * 清了就是 §16 明令禁止的那种破坏。 */
  const snap0 = await prefs.snapshot(tab);
  const keysBefore = snap0.allKeys;
  /* "改造前这 4 个键不存在"这条前提，依据只能来自【改造前那次采样】的落盘记录，
   * 不能来自 keysBefore —— keysBefore 是本轮第一次 navigate 时读到的，
   * 里面带着上一轮测试的遗留值。拿它当"改造前"证据，会把事实说反。 */
  const preTaskRecord = JSON.parse(
    fs.readFileSync(path.join(EV, 'prechange', 'DOM_BASELINE.json'), 'utf8'));
  const preTaskKeys = (preTaskRecord.dom && preTaskRecord.dom.localStorageKeys) || [];
  const mineAbsentPreTask = prefs.ALL.filter(k => preTaskKeys.indexOf(k) !== -1);
  const mine = await prefs.clearLayoutKeys(tab);
  await tab.navigate('http://127.0.0.1:7860/', 2000);
  const keysAfter = await tab.js(`Object.keys(localStorage).sort()`);
  /* 比的是"除这 4 个布局键之外，键集合逐字相同" —— 不能比总数：净化后的默认值
   * 会被 boot 回写，我的键在重载后又出现了，比总数会假 FAIL。 */
  const bizCmp = prefs.businessKeysEqual(keysBefore, keysAfter);
  const mineReappeared = keysAfter.includes(prefs.LAYOUT_KEYS.mine);
  check('CLEAR', '测试前置：只移除布局类 4 个键（含冻结 owner 的），业务键一根手指都没碰',
    bizCmp.equal && bizCmp.added.length === 0 && bizCmp.removed.length === 0
      && mine.leftKeys.indexOf(prefs.LAYOUT_KEYS.mine) === -1 && mineReappeared === true
      && mineAbsentPreTask.length === 0,
    { removed: mine.removed, businessKeysBefore: bizCmp.before.length,
      businessKeysAfter: bizCmp.after.length,
      ownKeyRewrittenByBoot: mineReappeared,
      // 依据 prechange/DOM_BASELINE.json（改造前那次采样），不是本轮残留快照
      layoutKeysAbsentInPreTaskRecord: mineAbsentPreTask.length === 0,
      preTaskLocalStorageKeys: preTaskKeys,
      residueThisRunRemoved: prefs.ALL.filter(k => keysBefore.indexOf(k) !== -1) });

  /* ---------- A0. §5.3 DETAIL 首屏必须等于改造前（拿 PRE 基线对几何） ----------
   * 只比"与视频流无关"的壳层节点：#av-video 的宽度取决于有没有流（PRE 采样时
   * 还没连上，videoWidth=0），拿它做逐像素对比会得到假 FAIL。 */
  const base = preTaskRecord.dom;
  const a0 = await probe(tab);
  const near = (x, y, tol) => !!(x && y) &&
    Math.abs(x.w - y.w) <= tol && Math.abs(x.h - y.h) <= tol &&
    Math.abs(x.x - y.x) <= tol && Math.abs(x.y - y.y) <= tol;
  const PAIRS = [
    ['app', a0.app, base.nodes['应用壳 #app'].rect],
    ['layer', a0.layer, base.nodes['人物层 #av-layer'].rect],
    ['stage', a0.stage, base.nodes['主区 main.stage'].rect],
    ['status', a0.status, base.nodes['状态容器 #gf-status-container'].rect],
    ['controls', a0.controls, base.nodes['控制区 .gf-controls'].rect],
    ['conv', a0.conv, base.nodes['聊天正文 #gf-live-conversation'].rect],
  ];
  const cmp = {};
  for (const [name, now, pre] of PAIRS) cmp[name] = { now, pre, same: near(now, pre, 2) };
  check('A0', '§5.3 DETAIL 第一次启用 = 改造前布局（零几何覆盖，未凭空改主页）',
    a0.pane === null && cmp.app.same && cmp.layer.same && cmp.status.same
      && cmp.controls.same && cmp.conv.same && cmp.stage.same,
    { pane_at_first_entry: a0.pane, viewMode: a0.viewMode, cmp });

  /* ---------- 1. 建立真实播放会话（人物必须是活的） ---------- */
  await tab.js(`document.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window})); 1`);
  let live = false;
  for (let i = 0; i < 40; i++) {
    const t = await tab.js(`(() => { const v = document.getElementById('av-video');
      return !!(v && v.srcObject && v.videoWidth > 0); })()`);
    if (t) { live = true; break; }
    await sleep(1000);
  }
  check('SESSION', '真实人物会话已连接且在解码（§10 前提）', live,
    { srcObjectAndFrames: live });
  if (!live) {
    log('\nABORT: 没有活的视频，§10 身份/连续性闸门不可判定。');
    await tab.close(); conn.close();
    process.exitCode = 1; return;
  }
  await sleep(2500);

  /* ---------- A. 默认进 DETAIL，且首屏等于改造前 ---------- */
  const a = await probe(tab);
  check('A', 'DETAIL：默认模式 = detail', a.viewMode === 'detail',
    { viewMode: a.viewMode, pane: a.pane });

  /* ---------- B/C. 拖 divider → 人物区实时变化 ---------- */
  const beforePane = a;
  const frameW = a.layer.w;
  const targetX = Math.round(frameW * 0.55);       // 想要 55% 给聊天区
  const trace = await dragDivider(tab, targetX);
  await sleep(400);
  const b = await probe(tab);
  const paneDelta = Math.abs(b.video.w - beforePane.video.w);
  const monotonic = Array.isArray(trace) && trace.length >= 6 &&
    (() => { const ws = trace.map(t => t.videoW); let ups = 0, downs = 0;
      for (let i = 1; i < ws.length; i++) { if (ws[i] > ws[i - 1] + 0.5) ups++; else if (ws[i] < ws[i - 1] - 0.5) downs++; }
      return (ups === 0 || downs === 0) && (ups + downs) >= 4; })();
  check('B', 'DETAIL：拖 divider 后 paneRatio 生效且已持久化',
    b.pane === 'custom' && b.store && typeof b.store.detail.paneRatio === 'number',
    { pane: b.pane, store: b.store && b.store.detail, dividerX: b.vars.dividerX });
  check('C', 'DETAIL：人物区宽度随拖动逐帧实时变化（未刷新、未提交后台）',
    paneDelta > 60 && monotonic && b.currentTime >= beforePane.currentTime,
    { videoW_before: beforePane.video.w, videoW_after: b.video.w,
      delta: +paneDelta.toFixed(1), perFrame: Array.isArray(trace) && trace.map(t => t.videoW),
      currentTime_before: beforePane.currentTime, currentTime_after: b.currentTime });
  /* 拖动前的人物区几何样本。刻意不叫 GEOMETRY_BEFORE、也不写进 prechange/：
   * 它是【已装本模块之后】的样本，而改造前的真基线在
   * prechange/DOM_BASELINE.json（avVideoRect / avLayerRect，11:28 采集，早于任何
   * 生产文件改动）。名字含糊的样本放在 prechange/ 里，早晚被人当成冻结基线。 */
  fs.writeFileSync(path.join(EV, 'ACCEPTANCE_PANE_SAMPLE.json'),
    JSON.stringify({ meaning: 'C 项拖动前的人物区几何 + 该刻的完整探针（非 §1 改造前基线）',
      captured_at: new Date().toISOString(), beforePane }, null, 2));

  /* ---------- D. DETAIL 的人物大小/左右/上下（owner = avatar_layout.js） ---------- */
  await clickSel(tab, '#gf-layout-open');
  const dOpen = await tab.js(`!document.getElementById('av-layout-card').hidden`);
  const d0 = await probe(tab);
  await setSlider(tab, '#av-layout-card input[data-key="s"]', '130');
  await setSlider(tab, '#av-layout-card input[data-key="x"]', '-40');
  await setSlider(tab, '#av-layout-card input[data-key="y"]', '25');
  await sleep(300);
  const d1 = await probe(tab);
  check('D', 'DETAIL：大小/左右/上下三滑杆实时生效（沿用既有 owner）',
    dOpen === true && d1.xform !== d0.xform && /matrix|matrix3d/.test(d1.xform || ''),
    { cardOpened: dOpen, transform_before: d0.xform, transform_after: d1.xform });

  /* ---------- B2. 分隔线 × 既有 owner 缩放残留（naturalPanePx 的口径闸门） ----------
   * 这一项由【像素审计】牵出：#av-video 的内联 transform 是冻结 owner 写的，
   * getBoundingClientRect().width 会把 scale 算进去，offsetWidth 不会。
   * 若拿 rect 宽当 custom 模式的【布局】宽度写回去，transform 仍在 ⇒
   * 按下分隔线那一帧渲染尺寸被放大两次（实测 1.6 倍时 622→995→1592）。
   * 判据：pane 先复位成"未分配"，在 scale=130 确实生效的前提下【只按下、不移动】，
   * 渲染宽度必须与按下前一致（±2px）；并且要正向证明缩放真的在效果里
   * （rectW 明显大于 offsetW），否则这一项会在缩放=100 时空过。 */
  await tab.js(`(() => { const d = document.getElementById('uivm-divider');
    const r = d.getBoundingClientRect(); const cx = Math.round(r.x + r.width / 2);
    d.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, cancelable: true,
      view: window, clientX: cx, clientY: Math.round(innerHeight / 2) })); return 1; })()`);
  await sleep(350);
  const b2pre = await tab.js(`(() => { const v = document.getElementById('av-video');
    const r = v.getBoundingClientRect();
    return { pane: document.body.dataset.uivmPane || null, rectW: +r.width.toFixed(1),
             offsetW: v.offsetWidth, xform: getComputedStyle(v).transform.slice(0, 46),
             dividerX: getComputedStyle(document.body).getPropertyValue('--uivm-divider-x').trim() }; })()`);
  const b2press = await tab.js(`(() => {
    const d = document.getElementById('uivm-divider');
    const r = d.getBoundingClientRect(); const cx = Math.round(r.x + r.width / 2);
    const mk = (t) => new PointerEvent(t, { bubbles: true, cancelable: true, view: window,
      pointerId: 1, clientX: cx, clientY: Math.round(innerHeight / 2), isPrimary: true, button: 0 });
    d.dispatchEvent(mk('pointerdown'));
    const v = document.getElementById('av-video'); const vr = v.getBoundingClientRect();
    const out = { rectW: +vr.width.toFixed(1), offsetW: v.offsetWidth,
                  pane: document.body.dataset.uivmPane || null };
    d.dispatchEvent(mk('pointerup')); window.dispatchEvent(mk('pointerup'));
    return out; })()`);
  check('B2', 'DETAIL：owner 缩放残留下按下分隔线，人物渲染尺寸不跳（布局宽 vs 变换宽）',
    b2pre.pane === null && b2press.pane === 'custom'
      && b2pre.rectW > b2pre.offsetW + 20
      && Math.abs(b2press.rectW - b2pre.rectW) <= 2
      && Math.abs(b2press.offsetW - b2pre.offsetW) <= 2,
    { scaleActuallyInEffect: b2pre.rectW > b2pre.offsetW + 20,
      before: b2pre, afterPress: b2press,
      renderedDelta: +(b2press.rectW - b2pre.rectW).toFixed(1),
      wouldHaveBeenOldBehaviour: 'rect.width 当布局宽写回 ⇒ 渲染 ' +
        Math.round(b2pre.rectW * (b2pre.rectW / b2pre.offsetW)) + 'px（放大两次）' });

  await clickSel(tab, '#av-layout-card .av-layout-reset');
  await sleep(200);

  /* ---------- E. 切到 LIVE_PORTRAIT：9:16 舞台 ---------- */
  await clickSel(tab, '#uivm-panel .uivm-mode[data-mode="live-portrait"]');
  await sleep(500);
  const e = await probe(tab);
  const ratio = e.video ? e.video.w / e.video.h : 0;
  check('E', 'LIVE_PORTRAIT：舞台宽高比 ≈ 9/16（含 -0.5%~+0.5% 容差）',
    e.viewMode === 'live-portrait' && Math.abs(ratio - 9 / 16) < 0.005,
    { viewMode: e.viewMode, stageRect: e.video, ratio: +ratio.toFixed(5),
      expected: +(9 / 16).toFixed(5), cssVars: { w: e.vars.stageW, h: e.vars.stageH } });

  /* ---------- F. 舞台内不出现后台控制 UI ---------- */
  const f = await tab.js(`(() => {
    const stage = document.getElementById('av-layer').getBoundingClientRect();
    const inside = (el) => { const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return false;
      return !(r.right <= stage.left || r.left >= stage.right ||
               r.bottom <= stage.top || r.top >= stage.bottom); };
    const SELS = ['#gf-model-status','#gf-voice-status','#gf-tts-status','#gf-avatar-status',
      '#gf-layout-open','#gf-avatar-open','#av-layout-card','#char-panel','#settings-modal',
      '#chat-panel','#uivm-panel','.gf-controls','#bubble-stack','#cam-pip','#gf-live-conversation',
      '#mic-gate','.topbar','.footer'];
    const hits = [];
    for (const s of SELS) document.querySelectorAll(s).forEach(el => { if (inside(el)) hits.push(s); });
    return { stage, hits, videoCount: document.querySelectorAll('video').length,
             avVideoCount: document.querySelectorAll('#av-video').length,
             audioCount: document.querySelectorAll('audio').length,
             canvasCount: document.querySelectorAll('canvas').length };
  })()`);
  check('F', 'LIVE_PORTRAIT：9:16 舞台内只有人物（无下拉/设置/调试/日志）',
    f.hits.length === 0 && f.avVideoCount === 1,
    { insideStage: f.hits, stage: f.stage });

  /* ---------- G/H/I. §10 硬闸门：同一节点 / 同一 MediaStream / 时间连续 ---------- */
  const g = await tab.js(`(() => {
    const v = document.getElementById('av-video');
    window.__uivm_t0 = { node: v, stream: v.srcObject, ct: v.currentTime,
                         t: performance.now() };
    return { sameCount: document.querySelectorAll('#av-video').length,
             hasStream: !!v.srcObject, ct: v.currentTime };
  })()`);
  await clickSel(tab, '#uivm-panel .uivm-mode[data-mode="detail"]');
  await sleep(1200);
  await clickSel(tab, '#uivm-panel .uivm-mode[data-mode="live-portrait"]');
  await sleep(1200);
  const h = await tab.js(`(() => {
    const v = document.getElementById('av-video'), a = window.__uivm_t0;
    return { nodeIdentical: v === a.node, streamIdentical: v.srcObject === a.stream,
             ctBefore: a.ct, ctAfter: v.currentTime, advanced: v.currentTime - a.ct,
             wallMs: performance.now() - a.t, ready: v.readyState, paused: v.paused,
             srcObjectPresent: !!v.srcObject, count: document.querySelectorAll('#av-video').length };
  })()`);
  check('G', '§10 硬闸门：videoBefore === videoAfter（同一 DOM 节点）',
    h.nodeIdentical === true && h.count === 1, { nodeIdentical: h.nodeIdentical, count: h.count });
  check('H', '§10 硬闸门：srcObjectBefore === srcObjectAfter（同一 MediaStream）',
    h.streamIdentical === true && h.srcObjectPresent === true,
    { streamIdentical: h.streamIdentical, srcObjectPresent: h.srcObjectPresent });
  check('I', '§10 硬闸门：切换期间 currentTime 持续增长（没黑屏重载/重启）',
    h.advanced > 1.0 && h.ready >= 2 && h.paused === false,
    { ctBefore: h.ctBefore, ctAfter: h.ctAfter, advancedSec: +h.advanced.toFixed(3),
      wallMs: +h.wallMs.toFixed(0), readyState: h.ready, paused: h.paused });

  /* ---------- J. LIVE 参数独立可调 ----------
   * 这一项上一版放过一个真缺陷：判据写成 `xform !== xform_before`，
   * 而 translate(var(--uivm-lv-x)…) 里塞无单位数字会让【整条 transform 作废】，
   * 值从 matrix(1,0,0,1,0,0) 变成 none —— "变了"成立，画面却一点没动。
   * 现在按矩阵逐项判：a/d 必须等于缩放倍率，e/f 必须等于按盒宽解析出的平移像素，
   * 并且变换后的 rect 宽高要跟着变。截图成对差异（screenshot_audit.py）是第二道。 */
  const OFFSET_FACTOR = 0.25;   // 设计承诺：滑块 ±100 ⇒ 人物自身盒 ±25% 平移
  /* 源头红线（静态）：CSS 里必须用 calc() 把无单位数值换算成百分比。
   * 这一条不是冗余 —— 行为判据（J2）要活会话才跑得动，静态这条任何时候都能挡住
   * "有人把 calc() 删了"的回归，并把陷阱写进可检索的位置。 */
  const cssSrc = fs.readFileSync(
    path.join(__dirname, '..', '..', 'ui', 'app', 'ui_layout_modes.css'), 'utf8');
  const translateLine = (cssSrc.match(/transform:[^\n]*--uivm-lv-x[^\n]*/) || ['<未找到>'])[0];
  check('J2s', 'CSS 源头红线：--uivm-lv-x/y 必须经 calc() 变成长度/百分比',
    translateLine.includes('calc(var(--uivm-lv-x)') && translateLine.includes('calc(var(--uivm-lv-y)')
      && !/translate\(var\(--uivm-lv-x\)/.test(translateLine),
    { rule: translateLine.slice(0, 160) });
  const j0 = await probe(tab);
  await setSlider(tab, '#uivm-panel input[data-uivm-lv="scale"]', '125');
  await setSlider(tab, '#uivm-panel input[data-uivm-lv="offsetX"]', '-35');
  await setSlider(tab, '#uivm-panel input[data-uivm-lv="offsetY"]', '20');
  await sleep(300);
  const j1 = await probe(tab);
  const expectTx = -35 * OFFSET_FACTOR / 100 * j0.videoBox.w;
  const expectTy = 20 * OFFSET_FACTOR / 100 * j0.videoBox.h;
  const isMat = !!(j1.mat && j1.mat.kind === 'matrix' && Number.isFinite(j1.mat.a));
  check('J', 'LIVE_PORTRAIT：大小/左右/上下可调且写进 CSS 变量',
    j1.vars.lvS === '1.25' && j1.vars.lvX === '-35' && j1.vars.lvY === '20',
    { lvS: j1.vars.lvS, lvX: j1.vars.lvX, lvY: j1.vars.lvY,
      transform_before: j0.xform, transform_after: j1.xform });
  check('J2', '§6.4 LIVE 参数确实被渲染：transform 矩阵 a/d=1.25、e/f=期望平移像素',
    isMat && Math.abs(j1.mat.a - 1.25) <= 0.01 && Math.abs(j1.mat.d - 1.25) <= 0.01
      && Math.abs(j1.mat.e - expectTx) <= 1.5 && Math.abs(j1.mat.f - expectTy) <= 1.5,
    { matrix: j1.mat, expected: { a: 1.25, d: 1.25, e: +expectTx.toFixed(2), f: +expectTy.toFixed(2) },
      box_untransformed: j0.videoBox,
      why: 'translate() 只接受 <length-percentage>；无单位数字会让整条声明作废 ⇒ none' });
  check('J3', '§6.4 缩放后人物【渲染盒】真的变大（rect 含 transform，这里就该含）',
    Math.abs(j1.video.w - j0.video.w * 1.25) <= 2 && Math.abs(j1.video.h - j0.video.h * 1.25) <= 2,
    { rect_before: { w: j0.video.w, h: j0.video.h }, rect_after: { w: j1.video.w, h: j1.video.h },
      expected_w: +(j0.video.w * 1.25).toFixed(1), expected_h: +(j0.video.h * 1.25).toFixed(1) });
  check('JOBJ', 'LIVE_PORTRAIT：object-fit = contain（禁止拉伸）',
    j1.objFit === 'contain', { objectFit: j1.objFit, objectPosition: j1.objPos });

  /* ---------- K/L. 切回 DETAIL：DETAIL 参数恢复自己此前的值 ---------- */
  const liveStore = (await probe(tab)).store;
  await clickSel(tab, '#uivm-panel .uivm-mode[data-mode="detail"]');
  await sleep(600);
  const k = await probe(tab);
  check('K', '切回 DETAIL：paneRatio 恢复此前值，未被 LIVE 污染',
    k.viewMode === 'detail' && k.store.detail.paneRatio === liveStore.detail.paneRatio
      && k.pane === 'custom',
    { paneRatio_restored: k.store.detail.paneRatio, expected: liveStore.detail.paneRatio,
      dividerVisible: k.dividerVisible });
  check('K2', '§12/§7：DETAIL 下本模块不写 transform（人物几何仍归 avatar_layout.js）',
    !/1\.25/.test(k.xform || '') && k.dividerVisible === true &&
      k.store.livePortrait.scale === 125,
    { detailTransform: k.xform, liveScaleKept: k.store.livePortrait.scale });

  /* ---------- M. 再切 LIVE：LIVE 参数恢复自己的值 ---------- */
  await clickSel(tab, '#uivm-panel .uivm-mode[data-mode="live-portrait"]');
  await sleep(600);
  const m = await probe(tab);
  check('M', '再切 LIVE：LIVE 三参数原样恢复（独立持久化）',
    m.store.livePortrait.scale === 125 && m.store.livePortrait.offsetX === -35
      && m.store.livePortrait.offsetY === 20 && m.vars.lvS === '1.25',
    { livePortrait: m.store.livePortrait, vars: { s: m.vars.lvS, x: m.vars.lvX, y: m.vars.lvY } });

  /* ---------- N. 刷新页面：viewMode + 两套参数全部恢复 ---------- */
  await tab.js(`localStorage.setItem('ai_girlfriend_ui_layout_v1', JSON.stringify(window.UiViewModes.state)); 1`);
  await tab.navigate('http://127.0.0.1:7860/', 2500);
  for (let i = 0; i < 40; i++) {
    const t = await tab.js(`(() => { const v = document.getElementById('av-video');
      return !!(v && v.srcObject && v.videoWidth > 0); })()`);
    if (t) break;
    if (i === 0) await tab.js(`document.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window})); 1`);
    await sleep(1000);
  }
  await sleep(2000);
  const n = await probe(tab);
  check('N', '刷新后：viewMode 与两模式参数按 localStorage 正确恢复',
    n.viewMode === 'live-portrait' && n.store.livePortrait.scale === 125
      && n.store.detail.paneRatio === liveStore.detail.paneRatio && n.vars.lvS === '1.25',
    { viewMode: n.viewMode, store: n.store, lvS: n.vars.lvS });
  await tab.js(`(() => { const v=document.getElementById('av-video');
    window.__uivm_r0={node:v,stream:v.srcObject,ct:v.currentTime}; return 1; })()`);

  /* ---------- O. 恢复默认分别有效（§16：互不清除、不碰业务） ---------- */
  const beforeReset = await tab.js(`({
    store: JSON.parse(localStorage.getItem('ai_girlfriend_ui_layout_v1')),
    avatarScale: localStorage.getItem('avatarVideoScale'),
    gfKeys: Object.keys(localStorage).filter(k => /model|voice|tts|avatar|character|persona|memory|chat/i.test(k)).sort(),
  })`);
  await clickSel(tab, '#uivm-panel .uivm-mode[data-mode="detail"]');
  await sleep(400);
  await clickSel(tab, '#uivm-panel .uivm-sec-detail .uivm-reset');
  await sleep(400);
  const afterResetD = await tab.js(`({
    store: JSON.parse(localStorage.getItem('ai_girlfriend_ui_layout_v1')),
    gfKeys: Object.keys(localStorage).filter(k => /model|voice|tts|avatar|character|persona|memory|chat/i.test(k)).sort(),
  })`);
  check('O', '§16「恢复详情默认」只清 detail，LIVE 参数与业务键零变化',
    afterResetD.store.detail.paneRatio === null
      && afterResetD.store.livePortrait.scale === 125
      && JSON.stringify(afterResetD.gfKeys) === JSON.stringify(beforeReset.gfKeys),
    { detail_after: afterResetD.store.detail, live_kept: afterResetD.store.livePortrait,
      businessKeys_before: beforeReset.gfKeys.length,
      businessKeys_after: afterResetD.gfKeys.length });

  /* ---------- O2. 「恢复直播竖屏默认」必须把变换真的清回 identity ----------
   * 只核对 localStorage 里的数字回到 100/0/0 是不够的（J 的教训就是这么来的：
   * 数值对、渲染错）。这里要求计算矩阵 a=1、e=f=0，且 rect 等于未变换布局盒。 */
  await clickSel(tab, '#uivm-panel .uivm-mode[data-mode="live-portrait"]');
  await sleep(500);
  const o2before = await probe(tab);
  await clickSel(tab, '#uivm-panel .uivm-sec-live .uivm-reset');
  await sleep(400);
  const o2 = await probe(tab);
  check('O2', '§16「恢复直播竖屏默认」：变换矩阵回到 identity，DETAIL 参数未被牵连',
    !!o2.mat && Number.isFinite(o2.mat.a) && Math.abs(o2.mat.a - 1) <= 0.001
      && Math.abs(o2.mat.d - 1) <= 0.001 && Math.abs(o2.mat.e) <= 0.5 && Math.abs(o2.mat.f) <= 0.5
      && o2.store.livePortrait.scale === 100 && o2.store.livePortrait.offsetX === 0
      && o2.store.livePortrait.offsetY === 0 && o2.store.detail.paneRatio === null
      && Math.abs(o2.video.w - o2.videoBox.w) <= 2,
    { matrix_before: o2before.mat, matrix_after: o2.mat,
      store: o2.store, rect: o2.video, untransformed_box: o2.videoBox });
  await clickSel(tab, '#uivm-panel .uivm-mode[data-mode="detail"]');
  await sleep(400);

  /* ---------- P. 非法 localStorage → 自动 clamp / fallback，不崩 ---------- */
  await tab.setViewport(1920, 1080);
  const badCases = [
    { name: 'paneRatio 超上限', json: { viewMode: 'detail', detail: { paneRatio: 9999 },
        livePortrait: { scale: 100, offsetX: 0, offsetY: 0 } } },
    { name: 'scale 非数字', json: { viewMode: 'live-portrait', detail: { paneRatio: 45 },
        livePortrait: { scale: 'abc', offsetX: {}, offsetY: null } } },
    { name: 'offset 无限大', json: { viewMode: 'live-portrait', detail: { paneRatio: 45 },
        livePortrait: { scale: 10, offsetX: 1e9, offsetY: -1e9 } } },
    { name: 'viewMode 非法值', json: { viewMode: 'teleport', detail: { paneRatio: 45 },
        livePortrait: { scale: 100, offsetX: 0, offsetY: 0 } } },
    { name: 'JSON 损坏', raw: '{not json at all,,,' },
    { name: '整份是数组', raw: '[1,2,3]' },
  ];
  for (const c of badCases) {
    await tab.js(`localStorage.setItem('ai_girlfriend_ui_layout_v1', ${
      JSON.stringify(c.raw !== undefined ? c.raw : JSON.stringify(c.json))}); 1`);
    await tab.navigate('http://127.0.0.1:7860/', 2000);
    await tab.js(`document.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window})); 1`);
    await sleep(2500);
    const p = await tab.js(`(() => {
      const v = document.getElementById('av-video'), b = document.body;
      const r = v && v.getBoundingClientRect();
      const cs = getComputedStyle(b);
      return { alive: !!window.UiViewModes, viewMode: b.dataset.viewMode || null,
        videoPresent: !!v, count: document.querySelectorAll('#av-video').length,
        rect: r ? { x:+r.x.toFixed(1), w:+r.width.toFixed(1), h:+r.height.toFixed(1) } : null,
        visible: !!(r && r.width > 40 && r.height > 200 && r.x < innerWidth && r.x + r.width > 0),
        store: (() => { try { return JSON.parse(localStorage.getItem('ai_girlfriend_ui_layout_v1')); }
                        catch (e) { return 'STILL_CORRUPT'; } })(),
        panel: !!document.getElementById('uivm-panel'),
        /* 真输入框，不是"控制区里随便有个东西"：§8 兜底必须证明坏数据没有把
         * 既有聊天输入一起带走（选择器与 runtime_smoke.cjs 用同一个）。 */
        input: !!document.querySelector('#gf-composer-input'),
        lvS: cs.getPropertyValue('--uivm-lv-s').trim(),
        lvX: cs.getPropertyValue('--uivm-lv-x').trim(),
        contentW: cs.getPropertyValue('--uivm-content-w').trim() };
    })()`);
    const limOk = p.store && (
      (typeof p.store.detail.paneRatio !== 'number') ||
      (p.store.detail.paneRatio >= 28 && p.store.detail.paneRatio <= 70)) &&
      p.store.livePortrait.scale >= 100 && p.store.livePortrait.scale <= 200 &&
      p.store.livePortrait.offsetX >= -100 && p.store.livePortrait.offsetX <= 100 &&
      p.store.livePortrait.offsetY >= -100 && p.store.livePortrait.offsetY <= 100;
    check('P', `§8 非法输入兜底：${c.name}`,
      p.alive === true && p.viewMode !== null && p.count === 1 && p.visible === true
        && p.panel === true && p.input === true && limOk,
      { viewMode: p.viewMode, videoRect: p.rect, visible: p.visible,
        clampedStore: p.store, cdpExceptionsSoFar: tab.exceptions.length });
  }

  /* ---------- Q. 三档视口（含 §13 遮挡闸门） ----------
   * FORBID_* 的取值口径：
   *   DETAIL —— 人物像素就在 #av-video 自己的矩形里（该模式不缩放，无裁剪问题）；
   *   LIVE   —— 人物被 #av-layer 裁进 9:16 取景框，放大后的 video 矩形是【裁剪前】
   *             的变换盒，拿它判遮挡会假 FAIL，故改用 layer（=舞台），同时硬断言
   *             layer 确实在裁剪（layerClips），否则"舞台外的溢出人物"看不见但存在。
   *   mic-gate 由 ink() 现场裁决：有像素才计入，空壳不计（判定值一并落进 detail）。 */
  const FORBID_D = ['topbar', 'conv', 'controls', 'status', 'camPip', 'video'];
  const FORBID_L = ['topbar', 'conv', 'controls', 'status', 'camPip', 'layer'];
  const badOv = (o, keys) => keys.filter((k) => ((o || {})[k] || 0) > 0).map((k) => k + '=' + o[k]);
  const inVp = (r, w, h) => !!r && r.x >= -0.5 && r.y >= -0.5 && r.x + r.w <= w + 1 && r.y + r.h <= h + 1;

  for (const [w, h] of [[1920, 1080], [1600, 900], [1366, 768]]) {
    await tab.setViewport(w, h);
    await clickSel(tab, '#uivm-panel .uivm-mode[data-mode="detail"]');
    await setSlider(tab, '#uivm-panel input[data-uivm-lv="scale"]', '100');
    await sleep(500);
    const q = await probe(tab);
    check(`Q${w}`, `§9 ${w}x${h} DETAIL：输入区/控制区/divider 全部在屏内`,
      q.controls && q.controls.w > 200 && q.controls.x >= 0 && q.controls.x + q.controls.w <= w
        && q.micGate && q.micGate.w > 0 && q.micGate.y + q.micGate.h <= h + 1
        && q.dividerVisible && q.divider && q.divider.x >= 0 && q.divider.x + q.divider.w <= w
        && q.video.w > 100,
      { controls: q.controls, micGate: q.micGate, divider: q.divider, video: q.video,
        app: q.app, contentW: q.vars.contentW });
    check(`OV${w}`, `§13 ${w}x${h} DETAIL：本任务面板与既有可见元素零遮挡、整块在屏内`,
      badOv(q.ovPanel, FORBID_D).length === 0 && q.panel.w > 150 && q.panel.h > 80 && inVp(q.panel, w, h),
      { overlaps_px2: q.ovPanel, offenders: badOv(q.ovPanel, FORBID_D), ink: q.ink,
        panel: q.panel, viewport: [w, h] });
    await clickSel(tab, '#uivm-panel .uivm-mode[data-mode="live-portrait"]');
    await sleep(500);
    const ql = await probe(tab);
    const lr = ql.video ? ql.video.w / ql.video.h : 0;
    check(`QL${w}`, `§9 ${w}x${h} LIVE：舞台存在、9:16 成立、控制列未被挤出`,
      Math.abs(lr - 9 / 16) < 0.01 && ql.panel.x + ql.panel.w <= w && ql.panel.w > 100,
      { stage: ql.video, ratio: +lr.toFixed(4), panel: ql.panel, status: ql.status });
    check(`OVL${w}`, `§13 ${w}x${h} LIVE：面板不压页头/控制区/舞台，且舞台确实裁住人物`,
      badOv(ql.ovPanel, FORBID_L).length === 0 && ql.layerClips === true
        && ql.panel.w > 150 && ql.panel.h > 80 && inVp(ql.panel, w, h),
      { overlaps_px2: ql.ovPanel, offenders: badOv(ql.ovPanel, FORBID_L), layerClips: ql.layerClips,
        ink: ql.ink, panel: ql.panel, stage: ql.layer, videoRaw: ql.video, viewport: [w, h] });
    await clickSel(tab, '#uivm-panel .uivm-mode[data-mode="detail"]');
    await sleep(300);
  }

  /* ---------- R. §22 无常驻任务 ---------- */
  const r = await tab.js(`(async () => {
    let raf = 0, to = 0, iv = 0; const stacks = [];
    const _raf = requestAnimationFrame, _to = setTimeout, _iv = setInterval;
    window.requestAnimationFrame = function () {
      raf++;
      if (stacks.length < 6) { try { stacks.push('RAF#' + raf + ' ' + String(new Error().stack).slice(0, 300)); } catch (_) {} }
      return _raf.apply(window, arguments);
    };
    window.setTimeout = function () { to++; return _to.apply(window, arguments); };
    window.setInterval = function () { iv++; return _iv.apply(window, arguments); };
    const t0 = performance.now();
    await new Promise(res => _to(res, 3000));      // 静置 3s，不交互（用原函数，别自计数）
    const idle = { raf, to, iv };
    raf = to = iv = 0; stacks.length = 0;
    const d = document.getElementById('uivm-divider');
    if (d) {
      const rect = d.getBoundingClientRect();
      const mk = (type, x) => new PointerEvent(type, { bubbles: true, cancelable: true,
        view: window, pointerId: 1, clientX: x, clientY: 400, isPrimary: true, button: 0 });
      d.dispatchEvent(mk('pointerdown', rect.x + 6));
      for (let i = 0; i < 30; i++) { window.dispatchEvent(mk('pointermove', rect.x + 6 + i * 3)); }
      d.dispatchEvent(mk('pointerup', rect.x + 6 + 90));
      window.dispatchEvent(mk('pointerup', rect.x + 6 + 90));
    }
    const drag = { raf, to, iv, stacks: stacks.slice(0, 3) };
    raf = to = iv = 0; stacks.length = 0;
    await new Promise(res => _to(res, 1500));      // 松手后再看 1.5s
    const after = { raf, to, iv, stacks: stacks.slice() };
    window.requestAnimationFrame = _raf; window.setTimeout = _to; window.setInterval = _iv;
    return { idle3s: idle, duringDrag: drag, afterDrag1_5s: after,
             elapsed: +(performance.now() - t0).toFixed(0) };
  })()`, 30000);
  /* 判据只锁"本模块引入的常驻任务"：rAF 必须一次都不调用；setInterval 不能因为
   * 拖动而新增（idle 与 drag 后计数相等）。业务链自己本来就有 setInterval，
   * 那不是本任务的回归，硬要求全局为 0 会造假 FAIL。 */
  /* §22 禁的是"本任务把布局做成常驻循环"，不是"全站永不调用 rAF"。
   * 上一轮这条判 FAIL，堆栈抓出那一次 rAF 来自 ui/zh_cn.js:430 的 schedule()
   * ——既有冻结翻译层的 MutationObserver 批量重译，一次性的、不是布局循环。
   * 所以判据改成按【归属】切：静置期必须 0 次（证明没有自转循环），
   * 且任何一次调用的栈里都不许出现 ui_layout_modes.js。 */
  const ownerOf = (s) => {
    const m = String(s).match(/\/([\w.-]+\.(?:js|css))(?:\?[^:\s]*)?:(\d+)/g) || [];
    return m.map(x => x.replace(/^\//, '').split(':')[0]).filter(f => !f.startsWith('cdp'));
  };
  const allStacks = [...(r.duringDrag.stacks || []), ...(r.afterDrag1_5s.stacks || [])];
  const owners = {};
  for (const s of allStacks) {
    const files = ownerOf(s).filter(f => f !== '<anonymous>');
    const key = (files[files.length - 1] || files[0] || 'ANONYMOUS');
    owners[key] = (owners[key] || 0) + 1;
  }
  const mineRaf = (owners['ui_layout_modes.js'] || 0)
    + allStacks.filter(s => /ui_layout_modes/.test(s)).length;
  check('R', '§22 无布局常驻任务：静置 3s rAF=0，且任何 rAF 调用都不来自本模块',
    r.idle3s.raf === 0 && mineRaf === 0 && r.afterDrag1_5s.iv <= r.idle3s.iv,
    { idle3s: r.idle3s, duringDrag: { ...r.duringDrag, stacks: r.duringDrag.stacks.length },
      afterDrag1_5s: { ...r.afterDrag1_5s, stacks: r.afterDrag1_5s.stacks.length },
      rafCalls_attributedByFile: owners, fromThisModule: mineRaf,
      elapsedMs: r.elapsed });

  /* ---------- S. 现有功能全部还在原位（§2 冻结清单回归） ---------- */
  const s = await tab.js(`(() => {
    const KEYS = ['#av-video','#cam-video','#av-layer','#app','main.stage',
      '#gf-live-conversation','#mic-gate','.gf-controls','#gf-layout-open','#gf-avatar-open',
      '#av-panel-mount','#gf-status-container','#gf-model-status','#gf-voice-status',
      '#gf-tts-status','#gf-avatar-status','.footer','.topbar','#bubble-stack','#chat-panel',
      '#chat-history','#settings-modal','#about-modal','#tools-modal','#char-panel',
      '#av-layout-card','#reading-panel','#av-audio'];
    const out = {}, missing = [];
    for (const k of KEYS) { const n = document.querySelectorAll(k).length;
      out[k] = n; if (n === 0) missing.push(k); }
    return { counts: out, missing, totalDom: document.getElementsByTagName('*').length,
             videoNodes: document.querySelectorAll('video').length,
             avVideoNodes: document.querySelectorAll('#av-video').length };
  })()`);
  check('S', '§2 现有功能节点零丢失，人物 video 节点数仍 = 1',
    s.missing.length === 0 && s.avVideoNodes === 1,
    { missing: s.missing, videoNodes: s.videoNodes, avVideoNodes: s.avVideoNodes,
      totalDomElements: s.totalDom });

  /* ---------- T. 页面无 JS error（由 CDP 事件全程收集，跨 navigate 不丢） ----------
   * 只把 JS_EXCEPTION 当硬闸门：资源 404/405 是网络层事实，必须列出来看归属，
   * 但不等于"页面崩了"，混进判据会造假 FAIL。 */
  const jsEx = tab.exceptions.filter(e => e.kind === 'JS_EXCEPTION');
  const logErr = tab.exceptions.filter(e => e.kind === 'LOG_ERROR');
  const logByUrl = {};
  for (const e of logErr) logByUrl[e.url + ' [' + e.text.slice(0, 40) + ']'] =
    (logByUrl[e.url + ' [' + e.text.slice(0, 40) + ']'] || 0) + 1;
  const logErrMine = logErr.filter(e => /ui_layout_modes|uivm/i.test(e.url + e.text));
  check('T', '全程无未捕获 JS 异常', jsEx.length === 0,
    { jsExceptions: jsEx.length, sample: jsEx.slice(0, 5) });
  check('T2', '资源级 error 不指向本任务新增文件', logErrMine.length === 0,
    { logErrors_total: logErr.length, attributableToNewFiles: logErrMine.length, byUrl: logByUrl });

  /* ---------- 收尾：布局键回到改造前状态，并核对业务键 ----------
   * 改造前实测这 4 个键不存在 ⇒ "还原"就是再删一次。测试确实动过冻结 owner
   * 的持久化键（D 项要设滑杆），所以必须在这里收尾干净，不能给用户 origin
   * 留下"1.6 倍人物"这种他们没点过的偏好。
   * 业务键方向性判据：开局在册的键【一个都不能少】（§16 的实质违规），
   * 结束时多出来的键也必须逐个交代归属，不许含糊通过。 */
  const snapEnd = await prefs.clearLayoutKeys(tab);
  const keysFinal = await tab.js(`Object.keys(localStorage).sort()`);
  const bizEnd = prefs.businessKeysEqual(keysBefore, keysFinal);
  check('PREFS', '收尾：布局类 4 键回到改造前状态（当时实测不存在），业务键零丢失',
    prefs.ALL.every(k => snapEnd.leftKeys.indexOf(k) === -1)
      && bizEnd.removed.length === 0 && bizEnd.added.length === 0,
    { layoutKeysRemoved: snapEnd.removed, layoutKeysLeft: snapEnd.leftKeys,
      businessKeysBefore: bizEnd.before, businessKeysAfter: bizEnd.after,
      businessKeysLost: bizEnd.removed, businessKeysGained: bizEnd.added });

  /* ---------- 落盘 ---------- */
  fs.writeFileSync(path.join(EV, 'VIEWMODE_TEST.json'), JSON.stringify({
    captured_at: new Date().toISOString(), results,
  }, null, 2));
  const fails = results.filter(x => x.verdict === 'FAIL');
  console.log(`\n=== ${results.length - fails.length}/${results.length} PASS ===`);
  if (fails.length) console.log('FAILED:', fails.map(f => f.id + ' ' + f.name).join(' | '));

  await tab.close();
  conn.close();
})().catch((e) => {
  console.error('ACCEPTANCE_CRASH', e && e.stack ? e.stack.slice(0, 1500) : e);
  process.exitCode = 1;
});
