/* ============================================================================
 * dock_inventory.cjs — AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01
 *
 * 目的：给「控件数量修改前后相同 / 同一个 DOM 控件，不是复制品 / 原有事件
 * 监听继续工作 / 中央控制区节点数量 = 0 / 人物 video node = 1 / srcObject
 * 不变」这几条验收，提供**同一套探针**在改造前后各跑一次的对照证据。
 *
 * 手法（三把独立的尺，缺一把都可能给出假判决）：
 *   1) 身份尺：对一份逐字冻结的选择器清单，采 tagName / id / className /
 *      childElementCount / outerHTML 长度 + djb2 哈希 / 文本前缀 / 祖先链路径。
 *      「不是复制品」的判据 = 每个选择器 document.querySelectorAll(sel).length
 *      恒为 1，且 outerHash 前后一致（复制品必然出现 count=2 或哈希不同）。
 *      注意：outerHash 只证「子树字节形状一致」，位置变化会体现在 path 上，
 *      而 path 本来就【应该】变 —— 这正是本轮要做的事，所以 path 不参与判等。
 *   2) 监听尺：CDP DOMDebugger.getEventListeners 直接读每个控件上真实注册的
 *      监听器条数与事件类型集合。移动节点（appendChild）不会解绑监听器，
 *      这条尺就是把「不会」变成「实测没掉」。它不依赖点击，因此不会触发
 *      任何后端请求（§「backend requests caused by layout = 0」不被探针自己破坏）。
 *   3) 功能尺：做几个【零网络】的真实交互，看 DOM 是否真的动了 ——
 *      #gf-composer-input 的 input 事件会把 textarea 的 style.height 写成 px，
 *      #gf-layout-open 的 click 会翻 #uivm-panel 的 data-collapsed。
 *      监听条数相同但处理器被换掉的假阳性，只有这一把尺能抓到。
 *
 * 明确不做：不点 #main-circle（那会拉起真实会话 → WebRTC/8010，越界），
 * 不改 #gf-model / #gf-voice-model（那会触发 LLM/TTS 切换，越界），
 * 不 localStorage.clear()（只读快照）。
 *
 * 用法： node dock_inventory.cjs before
 *        node dock_inventory.cjs after
 * ==========================================================================*/
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { connect, Tab, sleep } = require('../ui_dual_viewmode_01/cdp.cjs');
const { INSTALL, INJECT_DOUBLE } = require('../ui_detail_composition_repair_01/measure_page.cjs');

const PHASE = process.argv[2] === 'after' ? 'after' : 'before';
const REPO = path.join(__dirname, '..', '..', '..');
const EV = path.join(REPO, 'runtime', 'evidence', 'UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01');
const OUT = path.join(EV, PHASE === 'before' ? 'CONTROL_INVENTORY_BEFORE.json'
                                             : 'CONTROL_INVENTORY_AFTER.json');

/* ---------------------------------------------------------------- 选择器清单
 * 逐字对应任务书第 3 条要点名的 11 类控件，外加它们的宿主与被移动的兄弟节点。
 * 这份清单在 before/after 两次运行里必须是同一份字节 —— 所以写死在这里。 */
const SEL = [
  /* 8. 大语音交互圆球 + 它的实时交互兄弟 */
  '.orb-wrap', '#main-circle', '#mic-gate', '#mic-btn', '#stop-btn',
  '#circle-caption', '#circle-subcaption', '#queue-actions',
  '#join-queue-btn', '#leave-queue-btn',
  /* 3. 画面布局（入口由 ui_layout_modes.js 注入，before 阶段可能缺席） */
  '#gf-layout-open', '#uivm-panel', '#uivm-divider',
  /* 3. 模型 / TTS / 自动朗读 / 陪读 / 数字人 */
  '.gf-controls', '#gf-model', '#gf-voice-model', '.gf-switch-row',
  '.gf-speak-state', '#gf-reading-open', '#gf-avatar-open', '#av-panel-mount',
  /* 3. 语音表现 / 情绪语气 / 自定义语气（后两者是 fields 里的动态子控件） */
  '#gf-voice-performance', '#gf-performance-fields', '#gf-performance-model',
  '#gf-performance-reset', '#gf-performance-note',
  /* 3. 回复对话 / 输入区域 */
  '#gf-live-conversation', '#gf-turn-status', '#gf-turn-user', '#gf-turn-assistant',
  '#gf-composer', '#gf-composer-input', '#gf-composer-send',
  /* 系统状态（原本 position:fixed 压在左下角，会被 Dock 挡住 → 一并迁入） */
  '#gf-status-container', '#gf-model-status', '#gf-voice-status',
  '#gf-tts-status', '#gf-avatar-status',
  /* 容器与本轮新建的 Dock 脚手架 */
  '#app', 'main.stage', '#gf-dock', 'header.topbar', '.footer',
];

/* 监听尺只对这些真正挂过处理器的节点采样（其余节点本来就 0 条，采了是噪声）。 */
const LISTENER_SEL = [
  '#main-circle', '#mic-btn', '#stop-btn', '#join-queue-btn', '#leave-queue-btn',
  '#gf-model', '#gf-voice-model', '#gf-reading-open', '#gf-avatar-open',
  '#gf-layout-open', '#gf-performance-reset', '#gf-composer', '#gf-composer-input',
  '#uivm-panel', '#uivm-divider',
];

const INV_SRC = `
window.__INV_SEL = ${JSON.stringify(SEL)};
window.__INV = function () {
  var SEL = window.__INV_SEL;
  function hash(s) { var h = 5381, i = s.length; while (i) { h = (h * 33) ^ s.charCodeAt(--i); } return (h >>> 0).toString(16); }
  function chain(n) {
    var out = [], c = n;
    while (c && c.nodeType === 1) {
      var t = c.tagName.toLowerCase();
      var id = c.id ? '#' + c.id : '';
      var cl = (typeof c.className === 'string' && c.className.trim())
        ? '.' + c.className.trim().split(/\\s+/).slice(0, 2).join('.') : '';
      out.unshift(t + id + cl);
      c = c.parentElement;
    }
    return out.join(' > ');
  }
  function desc(sel) {
    var nodes = document.querySelectorAll(sel);
    var n = nodes[0];
    if (!n) return { sel: sel, exists: false, count: nodes.length };
    var oh = n.outerHTML;
    var cs = getComputedStyle(n);
    var r = n.getBoundingClientRect();
    var v = null;
    try { if ('value' in n && n.tagName !== 'DIV' && n.tagName !== 'SECTION') v = String(n.value).slice(0, 32); } catch (_) {}
    return {
      sel: sel, exists: true, count: nodes.length,
      tag: n.tagName.toLowerCase(), id: n.id || null,
      cls: (typeof n.className === 'string' ? n.className : '') || null,
      children: n.childElementCount,
      outerLen: oh.length, outerHash: hash(oh),
      text: (n.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 48),
      path: chain(n),
      hidden: n.hidden === true,
      value: v,
      options: n.options ? n.options.length : null,
      rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
      display: cs.display, position: cs.position,
      parentSel: n.parentElement ? (n.parentElement.tagName.toLowerCase() +
                 (n.parentElement.id ? '#' + n.parentElement.id : '') +
                 (typeof n.parentElement.className === 'string' && n.parentElement.className.trim()
                   ? '.' + n.parentElement.className.trim().split(/\\s+/)[0] : '')) : null
    };
  }
  var perf = document.getElementById('gf-performance-fields');
  var perfKids = [];
  if (perf) {
    for (var i = 0; i < perf.children.length; i++) {
      var c = perf.children[i];
      perfKids.push({
        tag: c.tagName.toLowerCase(), cls: String(c.className || ''),
        text: (c.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 44),
        inputs: c.querySelectorAll('input,select,textarea').length,
        outerLen: c.outerHTML.length, outerHash: hash(c.outerHTML)
      });
    }
  }
  var v = document.getElementById('av-video');
  var ls = null;
  try { ls = Object.keys(localStorage).sort(); } catch (_) { ls = 'LS_BLOCKED'; }
  return {
    controls: SEL.map(desc),
    perfFields: perfKids,
    counts: {
      docElements: document.querySelectorAll('*').length,
      appElements: document.querySelectorAll('#app *').length,
      appChildren: document.getElementById('app') ? document.getElementById('app').childElementCount : null,
      stageChildElements: (function () { var s = document.querySelector('main.stage'); return s ? s.childElementCount : null; })(),
      stageDescendants: (function () { var s = document.querySelector('main.stage'); return s ? s.querySelectorAll('*').length : null; })(),
      dockExists: !!document.getElementById('gf-dock'),
      dockGroups: document.querySelectorAll('#gf-dock .gf-dock-group').length,
      dockGroupNames: Array.prototype.map.call(
        document.querySelectorAll('#gf-dock .gf-dock-title'), function (t) { return t.textContent.trim(); }),
      interactive: document.querySelectorAll('button,input,select,textarea,label,a,[role="button"]').length,
      avVideo: document.querySelectorAll('#av-video').length,
      videos: document.querySelectorAll('video').length,
      uivmPanels: document.querySelectorAll('#uivm-panel').length,
      layoutEntries: document.querySelectorAll('#gf-layout-open').length,
      gfControls: document.querySelectorAll('.gf-controls').length
    },
    body: { viewMode: document.body.getAttribute('data-view-mode'),
            boundary: document.body.getAttribute('data-uivm-boundary'),
            bodyClasses: document.body.className },
    video: v ? {
      inlineTransform: v.style.transform || '',
      inlineOrigin: v.style.transformOrigin || '',
      inlineMask: String(v.style.maskImage || v.style.webkitMaskImage || '').slice(0, 140),
      computedTransform: getComputedStyle(v).transform,
      computedOrigin: getComputedStyle(v).transformOrigin,
      computedMask: String(getComputedStyle(v).maskImage || '').slice(0, 140),
      srcW: v.videoWidth, srcH: v.videoHeight,
      hasSrcObject: !!v.srcObject, currentTime: +v.currentTime.toFixed(3)
    } : null,
    owner: window.AvatarLayout ? {
      ver: window.AvatarLayout.version,
      s: window.AvatarLayout.state.s, w: window.AvatarLayout.state.w,
      x: window.AvatarLayout.state.x, y: window.AvatarLayout.state.y,
      isDefault: window.AvatarLayout.isDefault(),
      suppressed: window.AvatarLayout.isSuppressed(),
      geo: window.AvatarLayout.geometry()
    } : null,
    uivm: window.UiViewModes ? {
      ver: window.UiViewModes.version, disabled: window.UiViewModes.disabled,
      collapsed: window.UiViewModes.panel.isCollapsed(), state: window.UiViewModes.state
    } : null,
    connTrace: (function () {
      try { var r = window.__connTrace || []; var c = {};
        for (var i = 0; i < r.length; i++) { var k = r[i].ev || r[i].kind || '?'; c[k] = (c[k] || 0) + 1; }
        return { entries: r.length, byEvent: c }; } catch (e) { return { error: String(e.message).slice(0, 80) }; }
    })(),
    localStorageKeys: ls,
    viewport: [window.innerWidth, window.innerHeight]
  };
};
'INV_INSTALLED';
`;

/* 功能尺：零网络的真实交互，验证监听器不只是"还在"，而是"还在干活"。 */
const FUNCTIONAL_SRC = `
(function () {
  var out = {};
  try {
    var ta = document.getElementById('gf-composer-input');
    if (!ta) { out.composerInput = 'NO_NODE'; }
    else {
      var before = ta.style.height || '(empty)';
      ta.value = 'dock-listener-probe';
      ta.dispatchEvent(new Event('input', { bubbles: true }));
      out.composerInput = { heightBefore: before, heightAfter: ta.style.height || '(empty)',
                            valueKept: ta.value };
      /* 收尾：把探针写进去的字和高度还原，别把用户输入区留在脏状态 */
      ta.value = '';
      ta.dispatchEvent(new Event('input', { bubbles: true }));
      out.composerInput.heightRestored = ta.style.height || '(empty)';
    }
  } catch (e) { out.composerInput = 'THROW ' + String(e.message).slice(0, 120); }
  try {
    var btn = document.getElementById('gf-layout-open');
    var panel = document.getElementById('uivm-panel');
    if (!btn || !panel) { out.layoutToggle = 'NO_NODE btn=' + !!btn + ' panel=' + !!panel; }
    else {
      var c0 = panel.getAttribute('data-collapsed');
      btn.click();
      var c1 = panel.getAttribute('data-collapsed');
      btn.click();
      var c2 = panel.getAttribute('data-collapsed');
      out.layoutToggle = { collapsed0: c0, afterClick: c1, afterSecondClick: c2,
                           flipped: c0 !== c1, restored: c0 === c2 };
    }
  } catch (e) { out.layoutToggle = 'THROW ' + String(e.message).slice(0, 120); }
  try {
    var av = document.getElementById('gf-avatar-open');
    var mount = document.getElementById('av-panel-mount');
    out.avatarEntry = { btnExists: !!av, mountExists: !!mount,
                        mountChildren: mount ? mount.childElementCount : null };
  } catch (e) { out.avatarEntry = 'THROW ' + String(e.message).slice(0, 120); }
  return out;
})();
`;

async function listenerCensus(tab) {
  const out = [];
  for (const sel of LISTENER_SEL) {
    try {
      const r = await tab.send('Runtime.evaluate', {
        expression: `document.querySelector(${JSON.stringify(sel)})`,
        returnByValue: false,
      });
      const oid = r && r.result && r.result.objectId;
      if (!oid) { out.push({ sel, node: false, listeners: null }); continue; }
      const l = await tab.send('DOMDebugger.getEventListeners', { objectId: oid, depth: 0 });
      const arr = (l && l.listeners) || [];
      out.push({ sel, node: true, listeners: arr.length,
                 types: [...new Set(arr.map((x) => x.type))].sort() });
    } catch (e) {
      out.push({ sel, node: null, listeners: null, error: String(e.message).slice(0, 120) });
    }
  }
  return out;
}

(async () => {
  const T0 = Date.now();
  const log = (...a) => console.log('+' + String(Date.now() - T0).padStart(6) + 'ms', ...a);
  const wd = setTimeout(() => { console.log('WATCHDOG_FORCE_EXIT'); process.exit(3); }, 240000);
  wd.unref && wd.unref();

  log('phase=' + PHASE + ' connecting');
  const conn = await connect();
  const tab = await Tab.create(conn, 'about:blank');
  await tab.setViewport(1920, 1080);
  log('navigate');
  await tab.navigate('http://127.0.0.1:7860/', 4000);
  await sleep(1200);
  log('installing inventory probe');
  await tab.js(INSTALL);            /* 复用上一轮的 __M（黑雾/几何尺），本轮只做交叉核对 */
  await tab.js(INV_SRC);
  const installed = await tab.js(`typeof window.__INV`);
  log('probe installed =', installed);

  /* 人物几何要有真实源才量得准：注入 608×1056 的 canvas 替身（顶替像素源，
   * 不顶替会话——不点开始、不碰 8010）。before/after 两次都注，口径才可比。 */
  log('injecting canvas test double');
  await tab.js(INJECT_DOUBLE);
  let ready = false;
  for (let i = 0; i < 40; i++) {
    const s = await tab.js(`(() => { const v = document.getElementById('av-video');
      return v ? [v.videoWidth, v.videoHeight, v.readyState, !!v.srcObject] : [0,0,0,false]; })()`);
    if (s && s[0] > 0 && s[2] >= 2) { ready = true; log('double ready', JSON.stringify(s)); break; }
    await sleep(250);
  }
  if (!ready) log('DOUBLE_NOT_READY —— 几何尺降级为「无流口径」，前后仍可比');

  const inv = await tab.js(`window.__INV()`);
  const listeners = await listenerCensus(tab);
  const functional = await tab.js(FUNCTIONAL_SRC);

  /* 只读快照：本轮不写 localStorage，所以键集合必须原样 */
  const lsKeys = await tab.js(`Object.keys(localStorage).sort()`);

  const report = {
    task_id: 'AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01',
    phase: PHASE,
    captured_at: new Date().toISOString(),
    viewport: [1920, 1080],
    double_ready: ready,
    selector_census: SEL.length,
    controls: inv.controls,
    perf_fields: inv.perfFields,
    counts: inv.counts,
    body: inv.body,
    video: inv.video,
    owner: inv.owner,
    uivm: inv.uivm,
    listeners: listeners,
    listener_total: listeners.reduce((a, x) => a + (x.listeners || 0), 0),
    functional_probe: functional,
    connTrace: inv.connTrace,
    localStorageKeys: lsKeys,
    exceptions: (tab.exceptions || []).slice(0, 8),
    inventory_src_sha: require('node:crypto').createHash('sha256').update(INV_SRC).digest('hex'),
  };

  fs.mkdirSync(EV, { recursive: true });
  fs.writeFileSync(OUT, JSON.stringify(report, null, 2));
  log('wrote', OUT);

  const miss = inv.controls.filter((c) => !c.exists).map((c) => c.sel);
  const dup = inv.controls.filter((c) => c.exists && c.count !== 1).map((c) => c.sel + '×' + c.count);
  console.log('--- SUMMARY ' + PHASE + ' ---');
  console.log('docElements=%d appElements=%d interactive=%d', inv.counts.docElements, inv.counts.appElements, inv.counts.interactive);
  console.log('stageChildElements=%s stageDescendants=%s', inv.counts.stageChildElements, inv.counts.stageDescendants);
  console.log('dockExists=%s dockGroups=%d', inv.counts.dockExists, inv.counts.dockGroups);
  console.log('avVideo=%d videos=%d uivmPanels=%d layoutEntries=%d gfControls=%d',
    inv.counts.avVideo, inv.counts.videos, inv.counts.uivmPanels, inv.counts.layoutEntries, inv.counts.gfControls);
  console.log('MISSING selectors :', miss.join(', ') || '(none)');
  console.log('DUPLICATED (count!=1):', dup.join(', ') || '(none)');
  console.log('listener_total =', report.listener_total);
  for (const l of listeners) console.log('   LST ' + String(l.sel).padEnd(24) + ' node=' + l.node + ' n=' + l.listeners + ' [' + (l.types || []).join(',') + ']');
  console.log('functional =', JSON.stringify(functional));
  console.log('video =', JSON.stringify(inv.video));
  console.log('owner.geo =', JSON.stringify(inv.owner && inv.owner.geo));
  console.log('perfFields =', inv.perfFields.map((p) => p.text).join(' | ') || '(none)');
  console.log('exceptions =', JSON.stringify(report.exceptions));

  try { await tab.js(`clearInterval(window.__DOUBLE_TIMER)`); } catch (_) {}
  await tab.close();
  conn.close();
  clearTimeout(wd);
  console.log('done');
  process.exit(0);
})().catch((e) => {
  console.error('INVENTORY_CRASH', String((e && e.stack) || e).slice(0, 1200));
  try { fs.mkdirSync(EV, { recursive: true }); } catch (_) {}
  try { fs.writeFileSync(path.join(EV, 'INVENTORY_CRASH_' + PHASE + '.log'), String((e && e.stack) || e)); } catch (_) {}
  process.exitCode = 1;
  process.exit(1);
});
