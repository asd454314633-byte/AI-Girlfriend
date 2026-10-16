/* ============================================================================
 * preflight_probe.cjs — AI_GIRLFRIEND_UI_DETAIL_COMPOSITION_REPAIR_01 · §1/§3
 *
 * 纯只读现场取证：把"拖动分隔线为什么没有构图价值"变成可复算的数字，
 * 并顺手量出 DETAIL 面板停靠必须避开的每一块主控件的真实矩形。
 *
 * 本文件不改生产文件、不写 localStorage（除用 ui_prefs 快照/还原外）、
 * 不新建 video/WebSocket/Session。setPane() 只是调用页面既有 API。
 * ==========================================================================*/
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const CDP = require('../ui_dual_viewmode_01/cdp.cjs');
const PREFS = require('../ui_dual_viewmode_01/ui_prefs.cjs');

const EV = path.resolve(__dirname, '../../evidence/UI_DETAIL_COMPOSITION_REPAIR_01');
const URL = process.env.UIVM_URL || 'http://127.0.0.1:7860/?probe=composition-preflight';

/* 页面侧测量代码：禁止出现反斜杠（Node 模板字面量会吃掉它 → 假 FAIL，
 * 见 ui_dual_viewmode_01/page_code_hazards.cjs 的既有教训）。 */
const MEASURE = `(() => {
  const r2 = (n) => Math.round(n * 10) / 10;
  const box = (sel) => {
    const e = document.querySelector(sel);
    if (!e) return null;
    const r = e.getBoundingClientRect();
    return { sel, x: r2(r.left), y: r2(r.top), w: r2(r.width), h: r2(r.height),
             right: r2(r.right), bottom: r2(r.bottom),
             visible: !!(r.width > 0 && r.height > 0),
             display: getComputedStyle(e).display,
             overflow: getComputedStyle(e).overflow };
  };
  const v = document.getElementById('av-video');
  const layer = document.getElementById('av-layer');
  const cs = v ? getComputedStyle(v) : null;
  const lcs = layer ? getComputedStyle(layer) : null;
  const maskFull = cs ? (cs.maskImage && cs.maskImage !== 'none' ? cs.maskImage : (cs.webkitMaskImage || 'none')) : 'NO_VIDEO';
  let chain = [];
  for (let e = v; e && chain.length < 6; e = e.parentElement) {
    chain.push(e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') +
               (e.className && typeof e.className === 'string' ? '.' + e.className.trim().split(/\\s+/).join('.') : ''));
  }
  const lcs2 = layer ? getComputedStyle(layer) : null;
  return {
    viewport: { iw: innerWidth, ih: innerHeight, dpr: devicePixelRatio,
                docW: document.documentElement.clientWidth,
                docH: document.documentElement.clientHeight },
    viewMode: document.body.dataset.viewMode || null,
    paneAttr: document.body.dataset.uivmPane || null,
    bodyVars: ['--uivm-content-w', '--uivm-divider-x', '--uivm-stage-w', '--uivm-stage-h',
               '--uivm-topbar-h', '--uivm-lv-s'].map((k) => k + '=' +
               (document.body.style.getPropertyValue(k) || '(unset)')),
    layer: { rect: box('#av-layer'), position: lcs && lcs.position, zIndex: lcs && lcs.zIndex,
             overflow: lcs && lcs.overflow, cls: layer ? layer.className : null,
             children: layer ? Array.prototype.map.call(layer.children, (c) => c.tagName.toLowerCase() + (c.id ? '#' + c.id : '')) : [],
             fillsViewport: lcs2 ? (Math.abs(layer.getBoundingClientRect().left) < 1 &&
               Math.abs(layer.getBoundingClientRect().top) < 1 &&
               Math.abs(layer.getBoundingClientRect().width - innerWidth) < 2 &&
               Math.abs(layer.getBoundingClientRect().height - innerHeight) < 2) : null },
    video: { rect: v ? box('#av-video') : null, offsetW: v ? v.offsetWidth : null,
             offsetH: v ? v.offsetHeight : null,
             videoW: v ? v.videoWidth : null, videoH: v ? v.videoHeight : null,
             srcObject: v ? !!v.srcObject : null, currentTime: v ? r2(v.currentTime || 0) : null,
             objectFit: cs && cs.objectFit, objectPosition: cs && cs.objectPosition,
             cssWidth: cs && cs.width, cssHeight: cs && cs.height,
             maxWidth: cs && cs.maxWidth, maxHeight: cs && cs.maxHeight,
             position: cs && cs.position, top: cs && cs.top, right: cs && cs.right,
             left: cs && cs.left, bottom: cs && cs.bottom,
             transform: cs && cs.transform, transformOrigin: cs && cs.transformOrigin,
             maskLen: maskFull ? maskFull.length : 0,
             maskHead: maskFull ? maskFull.slice(0, 120) : null,
             maskFull: maskFull ? maskFull.slice(0, 900) : null,
             chain },
    videoNodeCount: document.querySelectorAll('video').length,
    avVideoCount: document.querySelectorAll('#av-video').length,
    ui: ['#app', 'header.topbar', 'main.stage', '.orb-wrap', '#main-circle', '.gf-controls',
         '#gf-model', '#gf-voice-model', '#gf-reading-open', '#gf-avatar-open', '#gf-layout-open',
         '#av-panel-mount', '#gf-voice-performance', '#gf-live-conversation', '#gf-composer',
         '#gf-composer-input', '#gf-composer-send', '#gf-status-container', '#cam-pip',
         '#uivm-panel', '#uivm-divider', '#av-layout-card'].map(box),
    panel: (() => {
      const p = document.getElementById('uivm-panel');
      if (!p) return null;
      const r = p.getBoundingClientRect();
      const st = getComputedStyle(p);
      return { x: r2(r.left), y: r2(r.top), w: r2(r.width), h: r2(r.height),
               position: st.position, left: st.left, top: st.top, transform: st.transform,
               collapsed: p.dataset.collapsed || '(unset)', zIndex: st.zIndex };
    })(),
    owner: window.AvatarLayout ? { disabled: !!window.AvatarLayout.disabled,
             state: JSON.parse(JSON.stringify(window.AvatarLayout.state || {})),
             isDefault: window.AvatarLayout.isDefault ? window.AvatarLayout.isDefault() : null,
             api: Object.keys(window.AvatarLayout) } : 'NO_WINDOW',
    modes: window.UiViewModes ? { disabled: !!window.UiViewModes.disabled,
             state: JSON.parse(JSON.stringify(window.UiViewModes.state || {})),
             api: Object.keys(window.UiViewModes) } : 'NO_WINDOW',
    storageKeys: (() => { try { return Object.keys(localStorage).sort(); } catch (e) { return ['ERR']; } })(),
  };
})()`;

async function main() {
  const conn = await CDP.connect();
  const tab = await CDP.Tab.create(conn, 'about:blank');
  const out = { task_id: 'AI_GIRLFRIEND_UI_DETAIL_COMPOSITION_REPAIR_01',
                phase: 'PREFLIGHT_PROBE', at: new Date().toISOString(), url: URL };
  try {
    /* about:blank 是 opaque origin，在它上面读 localStorage 会直接抛
     * SecurityError —— 快照必须在导航到 7860 之后做。 */
    await tab.setViewport(1920, 1080);
    await tab.navigate(URL);
    await tab.wait('!!document.getElementById("av-video") && !!window.UiViewModes', 20000);
    await CDP.sleep(1200);

    /* 页面里的 avatar-sync 是"点击任意处才 connect"（avatar-sync.js:887–895），
     * 不点就没有人物像素 —— 上一版探针就是因为没点，量到的是 UA 默认的
     * 300×150 空盒，那组数字不能用来谈构图。 */
    await tab.js(`document.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window})); 1`);
    let live = false;
    for (let i = 0; i < 30 && !live; i++) {
      live = await tab.js(`(() => { const v = document.getElementById('av-video');
        return !!(v && v.srcObject && v.videoWidth > 0 && v.readyState >= 2); })()`);
      if (!live) await CDP.sleep(1000);
    }
    out.LIVE_SESSION = live;
    if (live) await CDP.sleep(2500);

    const snap = await PREFS.snapshot(tab);
    out.prefs_snapshot = snap && snap.values;
    out.prefs_snapshot_allKeys_count = snap && snap.allKeys && snap.allKeys.length;
    out.prefs_snapshot_allKeys = snap && snap.allKeys;
    tab.clearRequests();   /* 从这里起才是"布局动作"产生的请求 */

    /* A0：用户 origin 上【当前持久化】的状态 —— 这就是用户真实看到的那一屏，
     *      也是"DETAIL 被拒"的现场证据，不能拿默认态顶替它。 */
    out.A0_user_persisted = await tab.js(MEASURE);

    /* A1：调用既有 API 把 pane 置回 null（= 不做任何 pane 覆盖，回到
     *      avatar-sync.js 原生舞台几何）。刻意【不 reload】：reload 会再握手一次
     *      WebRTC，而 §3 要的只是几何，pane 的生效路径是纯 CSS 变量/属性。 */
    out.A1_setPaneNull = await tab.js('window.UiViewModes.setPane(null); String(document.body.dataset.uivmPane)');
    await CDP.sleep(400);
    out.A1_native_default = await tab.js(MEASURE);

    /* B：把分隔线拖到 70%（人区最宽） —— 证明"变的是容器不是人物" */
    out.B_pane70_call = await tab.js('window.UiViewModes.setPane(70); String(window.UiViewModes.state.detail.paneRatio)');
    await CDP.sleep(400);
    out.B_pane70 = await tab.js(MEASURE);

    /* C：再拖到 28%（最窄），取三档对照 */
    await tab.js('window.UiViewModes.setPane(28)');
    await CDP.sleep(400);
    out.C_pane28 = await tab.js(MEASURE);

    /* D：owner 自己的放大（对照：这条路是真的在改像素，只是被 pane 的封顶卡住） */
    await tab.js('window.UiViewModes.setPane(null)');
    await CDP.sleep(200);
    out.D_ownerScale_call = await tab.js('window.AvatarLayout.setScale(180); String(window.AvatarLayout.state.s)');
    await CDP.sleep(300);
    out.D_ownerScale180 = await tab.js(MEASURE);
    await tab.js('window.AvatarLayout.resetToDefault()');
    await CDP.sleep(200);
    out.E_afterReset = await tab.js(MEASURE);

    /* 三档对照的判决。
     * 关键：#av-video 的【盒子宽度】不等于【人物渲染带宽度】。object-fit:contain
     * 把源按 min(boxW/srcW, boxH/srcH) 缩放后按 object-position 摆放 —— 盒子变宽
     * 完全可能对应"人物一寸没大"，多出来的宽度全是黑。更糟的是 avatar-sync 的
     * mask 是按【盒子】百分比画的黑→透明渐变（mask-size:100% 100%），于是渐变
     * 落在这段黑里，人物左缘与"黑雾结束处"脱钩 —— 这就是用户看到的
     * "主UI | 一块纯黑矩形 | 人物"。下面把这三件事全算成数字。 */
    const r1 = (n) => Math.round(n * 10) / 10;
    const matrix = (t) => {
      if (!t || t === 'none') return { a: 1, b: 0, c: 0, d: 1, e: 0, f: 0 };
      const m = /matrix\(([^)]+)\)/.exec(String(t));
      if (!m) return { a: 1, b: 0, c: 0, d: 1, e: 0, f: 0 };
      const p = m[1].split(',').map((s) => parseFloat(s));
      return { a: p[0], b: p[1], c: p[2], d: p[3], e: p[4], f: p[5] };
    };
    const fadePct = (maskFull) => {
      const stops = [];
      const re = /(rgba\([^)]*\)|#[0-9a-fA-F]{3,8}|\b(?:transparent|black|white)\b)\s+(-?[\d.]+)%/g;
      let m;
      while ((m = re.exec(String(maskFull || ''))) !== null) stops.push({ color: m[1], pct: parseFloat(m[2]) });
      return stops;
    };
    const band = (o) => {
      const rect = (o.video && o.video.rect) || {};
      const M = matrix(o.video && o.video.transform);
      const sw = (o.video && o.video.videoW) || 0, sh = (o.video && o.video.videoH) || 0;
      const boxW = (o.video && o.video.offsetW) || 0, boxH = (o.video && o.video.offsetH) || 0;
      let k = null, bandLayout = null, blackLayout = null;
      if (sw > 0 && sh > 0 && boxW > 0 && boxH > 0) {
        k = o.video.objectFit === 'contain' ? Math.min(boxW / sw, boxH / sh)
          : Math.max(boxW / sw, boxH / sh);
        bandLayout = sw * k;
        blackLayout = boxW - bandLayout;
      }
      const stops = fadePct(o.video && o.video.maskFull);
      /* 渐隐最后一个不透明停靠点（local 盒坐标 %）→ 变换后落到的屏幕 x */
      let fadeEndLocalPct = null, fadeEndScreen = null, personLeftScreen = null, blackGap = null;
      if (stops.length) fadeEndLocalPct = stops[stops.length - 1].pct;
      if (rect.w && boxW > 0) {
        if (fadeEndLocalPct !== null) fadeEndScreen = rect.x + (fadeEndLocalPct / 100) * rect.w;
        if (bandLayout !== null) {
          const bandScreen = bandLayout * M.a;
          /* object-position:100% ⇒ 带右对齐；否则居中 */
          const posRight = String(o.video.objectPosition || '').indexOf('100%') === 0
            || String(o.video.objectPosition || '').indexOf('right') === 0;
          const leftPad = posRight ? (boxW - bandLayout) : (boxW - bandLayout) / 2;
          personLeftScreen = rect.x + leftPad * M.a;
          if (fadeEndScreen !== null) blackGap = r1(personLeftScreen - fadeEndScreen);
        }
      }
      return {
        pane: o.paneAttr,
        box_layout: [boxW, boxH],
        box_screen: [r1(rect.w || 0), r1(rect.h || 0)],
        box_left_screen: r1(rect.x || 0), box_right_screen: r1(rect.right || 0),
        person_source: [sw, sh],
        objectFit: o.video && o.video.objectFit,
        objectPosition: o.video && o.video.objectPosition,
        transform: o.video && o.video.transform,
        transform_screen_scale: [r1(M.a), r1(M.d)],
        renderedBandW_screen: bandLayout !== null ? r1(bandLayout * M.a) : null,
        blackInsideBoxW_screen: blackLayout !== null ? r1(blackLayout * M.a) : null,
        fadeEndPct: fadeEndLocalPct, fadeStops: stops,
        fadeEndScreen_x: fadeEndScreen === null ? null : r1(fadeEndScreen),
        personLeftScreen_x: personLeftScreen === null ? null : r1(personLeftScreen),
        black_band_gap_px: blackGap,
        appW: (o.ui.find((x) => x && x.sel === '#app') || {}).w,
        layerFillsViewport: o.layer && o.layer.fillsViewport,
        dividerX: (o.ui.find((x) => x && x.sel === '#uivm-divider') || {}).x,
        panelRect: o.panel && [o.panel.x, o.panel.y, o.panel.w, o.panel.h],
        currentTime: o.video && o.video.currentTime,
      };
    };
    out.COMPARISON = {
      A0_user_persisted: band(out.A0_user_persisted),
      A1_native_default: band(out.A1_native_default),
      B_pane_70: band(out.B_pane70),
      C_pane_28: band(out.C_pane28),
      D_owner_scale_180: band(out.D_ownerScale180),
      E_after_reset: band(out.E_afterReset),
    };
    const boxOf = (x) => (x && x.box_screen && x.box_screen[0]) || 0;
    const bandOf = (x) => (x && x.renderedBandW_screen) || 0;
    out.ROOT_CAUSE_MEASURED = {
      pane_box_width_ratio_70_vs_28: r1(boxOf(out.COMPARISON.B_pane_70) / boxOf(out.COMPARISON.C_pane_28)),
      pane_band_width_ratio_70_vs_28: r1(bandOf(out.COMPARISON.B_pane_70) / bandOf(out.COMPARISON.C_pane_28)),
      pane_box_delta_70_vs_28: [boxOf(out.COMPARISON.B_pane_70) - boxOf(out.COMPARISON.C_pane_28),
        bandOf(out.COMPARISON.B_pane_70) - bandOf(out.COMPARISON.C_pane_28)],
      note: '盒子宽度随 pane 大幅变化而【人物渲染带】几乎不变 ⇒ 拖动只是容器变化（contain 盒内留黑）；' +
            '若同时 black_band_gap_px > 0 ⇒ 黑雾结束点与人物左缘之间有纯黑带（用户拒的"独立黑区"）',
    };
    out.VIEWPORT = { w: out.A1_native_default.viewport.iw, h: out.A1_native_default.viewport.ih };
    out.HAS_LIVE_AVATAR = !!(out.A1_native_default.video.srcObject && out.A1_native_default.video.videoW > 0);
    out.avatarVideoSourceSize = [out.A1_native_default.video.videoW, out.A1_native_default.video.videoH];
    out.PRIMARY_CONTROL_RECTS = out.A1_native_default.ui;
    out.PANEL_AND_LAYER = { panel: out.A1_native_default.panel, layer: out.A1_native_default.layer,
                            avCard: (out.A1_native_default.ui.find((x) => x && x.sel === '#av-layout-card') || null),
                            uivmPanel: (out.A1_native_default.ui.find((x) => x && x.sel === '#uivm-panel') || null) };

    out.prefs_restore = await PREFS.restore(tab, snap);
    out.prefs_restored = !!(out.prefs_restore && out.prefs_restore.faithful);
    out.prefs_business_keys = PREFS.businessKeysEqual(
      snap.allKeys, ((await PREFS.snapshot(tab)) || {}).allKeys);

    /* 布局动作期间的网络事实（只读记录，不判定）：setPane/scale/reset 应当
     * 一条后台请求都不招。WebSocket/WebRTC 握手发生在 clearRequests 之前。 */
    const seen = tab.requests.map((r) => ({
      url: String((r.request && r.request.url) || '').slice(0, 160),
      method: (r.request && r.request.method) || null,
      type: (r.type || null), status: r._status === undefined ? null : r._status,
    }));
    out.REQUESTS_DURING_LAYOUT_ACTIONS = {
      total: seen.length,
      by_origin: seen.reduce((acc, s) => {
        const o = (/^https?:\/\/[^/]+/i.exec(s.url) || [s.url || '?'])[0];
        acc[o] = (acc[o] || 0) + 1; return acc;
      }, {}),
      non_static: seen.filter((s) => !/\/(api\/)?(voice-models|models|health)/i.test(s.url)
        && /\.(js|css|png|jpg|svg|woff2?|ico|json)(\?|$)/i.test(s.url) === false).slice(0, 30),
    };
  } catch (e) {
    out.FATAL = String(e && e.message || e).slice(0, 500);
    process.exitCode = 1;
  } finally {
    fs.mkdirSync(EV, { recursive: true });
    fs.writeFileSync(path.join(EV, 'PREFLIGHT_COMPOSITION_PROBE.json'), JSON.stringify(out, null, 2));
    await tab.close();
    conn.close();
  }
  const k = (o) => o;
  console.log('HAS_LIVE_AVATAR =', out.HAS_LIVE_AVATAR, '| src', JSON.stringify(out.avatarVideoSourceSize));
  console.log('viewport', JSON.stringify(out.VIEWPORT));
  console.log(JSON.stringify(out.COMPARISON, null, 1));
  console.log('FATAL =', out.FATAL || 'none');
  console.log('Wrote', path.join(EV, 'PREFLIGHT_COMPOSITION_PROBE.json'));
}

main().catch((e) => { console.log('TOPLEVEL_FAIL', e.message); process.exitCode = 2; });
