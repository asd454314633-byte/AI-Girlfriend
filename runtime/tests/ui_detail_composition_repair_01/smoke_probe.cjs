/* smoke_probe.cjs — V2 代码在真浏览器里装载后，探针本身是否可用、
 * owner/面板/几何是否成形状。不判闸门、不落证据文件（那是 shadow_acceptance 的活）。
 *
 * 刻意【不点开始会话】：8010 只服务一路会话，用户自己的标签页正占着它
 * （netstat 实测只有 1 条 :8010 ESTABLISHED，属主是 7860 后端）。
 * 为了"拿到画面"去点启动 = 可能把用户那一路顶掉/重连，属 §30 禁止的干扰。
 * 像素源改用 measure_page.INJECT_DOUBLE（608×1056 canvas，与真会话源同 ratio）。 */
'use strict';
const { connect, Tab, sleep } = require('../ui_dual_viewmode_01/cdp.cjs');
const { INSTALL, INJECT_DOUBLE } = require('./measure_page.cjs');

const CONN_COUNTS = `(() => { const a = window.__connTrace || [];
  const c = { pc: 0, ws: 0, total: a.length };
  for (const e of a) if (e.event === 'construct') { if (e.kind === 'pc') c.pc++; else if (e.kind === 'ws') c.ws++; }
  return c; })()`;

(async () => {
  setTimeout(() => { console.log('WATCHDOG_FORCE_EXIT'); process.exit(3); }, 200000);
  const T0 = Date.now();
  const log = (...a) => console.log('+' + String(Date.now() - T0).padStart(6) + 'ms', ...a);
  const conn = await connect();
  const tab = await Tab.create(conn, 'about:blank');
  await tab.setViewport(1920, 1080);
  await tab.navigate('http://127.0.0.1:7860/', 2500);
  log('navigated; INSTALL len', INSTALL.length);
  try { console.log('install ret =', await tab.js(INSTALL)); }
  catch (e) { console.log('INSTALL_THROW', String(e.message).slice(0, 700)); }

  log('conn counters @load:', JSON.stringify(await tab.js(CONN_COUNTS)));
  log('inject double ->', JSON.stringify(await tab.js(INJECT_DOUBLE)));
  for (let i = 0; i < 24; i++) {
    const ok = await tab.js(`(() => { const v=document.getElementById('av-video');
      return !!(v && v.videoWidth > 0 && v.readyState >= 2); })()`);
    if (ok) break;
    await sleep(500);
  }
  let M;
  try { M = await tab.js(`window.__M('smoke')`); }
  catch (e) { console.log('MEASURE_THROW', String(e.message).slice(0, 800)); }
  if (M) {
    console.log(JSON.stringify({
      viewMode: M.viewMode, paneAttr: M.paneAttr, boundary: M.boundaryAttr, vp: M.vp,
      src: [M.video.srcW, M.video.srcH], inline: M.video.inline, transform: M.video.transform,
      maxW: M.video.maxW, objectFit: M.video.objectFit, ct: M.video.currentTime,
      video: { x: M.video.x, y: M.video.y, w: M.video.w, h: M.video.h, off: [M.video.offsetW, M.video.offsetH] },
      maskStops: M.mask.stops, maskHead: M.mask.raw.slice(0, 180),
      fade: { s: M.mask.fadeStart, e: M.mask.fadeEnd, seam: M.mask.seamW, gap: M.mask.blackGap },
      layer: M.layer, app: M.app, divider: M.divider, panel: M.panel, entry: M.entry,
      geo: M.owner && M.owner.geo, counts: M.counts, vars: M.vars,
      uivm: M.uivm && { ver: M.uivm.ver, migrated: M.uivm.migrated },
    }, null, 1));
  }
  console.log('exceptions', JSON.stringify(tab.exceptions.slice(0, 5)));
  try {
    console.log('--- drag -> 1150 ---', JSON.stringify(await tab.js(`window.__DRAG(null, 1150, 6)`)));
    const m2 = await tab.js(`window.__M('smoke_drag')`);
    console.log(JSON.stringify({ scale: m2.owner.s, left: m2.video.x, w: m2.video.w, h: m2.video.h,
      fadeStart: m2.mask.fadeStart, fadeEnd: m2.mask.fadeEnd, gap: m2.mask.blackGap,
      divider: m2.divider && m2.divider.center, inline: m2.video.inline, ct: m2.video.currentTime }));
    console.log('collide', JSON.stringify(await tab.js(
      `window.__COLLIDE('#uivm-panel', ['#gf-composer-input','#gf-model','#gf-voice-model','#gf-layout-open','#gf-composer-send'])`)));
    await tab.js(`document.getElementById('gf-layout-open').click(); 1`); await sleep(300);
    const mo = await tab.js(`window.__M('smoke_panel_open')`);
    console.log('panelOpen', JSON.stringify({ rect: mo.panel, body: mo.panel && mo.panel.collapsedHeadOnly }));
    console.log('collideOpen', JSON.stringify(await tab.js(
      `window.__COLLIDE('#uivm-panel', ['#gf-composer-input','#gf-model','#gf-voice-model','#gf-avatar-open','#gf-layout-open','#gf-voice-performance','#gf-live-conversation','.orb-wrap','#gf-reading-open','#gf-composer-send'])`)));
    await tab.js(`window.UiViewModes.set('live-portrait'); 1`); await sleep(400);
    const m3 = await tab.js(`window.__M('smoke_live')`);
    console.log('live', JSON.stringify({ transform: m3.video.transform, mask: m3.mask.raw.slice(0, 70),
      layer: m3.layer, app: m3.app, panel: m3.panel, vars: m3.vars, inline: m3.video.inline }));
    console.log('liveCollide', JSON.stringify(await tab.js(
      `window.__COLLIDE('#uivm-panel', ['#gf-composer-input','#gf-composer-send','#gf-model','#gf-voice-model','#gf-live-conversation'])`)));
    await tab.js(`window.UiViewModes.set('detail'); window.AvatarLayout.resetToDefault(); 1`); await sleep(300);
    const m4 = await tab.js(`window.__M('smoke_reset')`);
    console.log('afterReset', JSON.stringify({ inline: m4.video.inline, left: m4.video.x, w: m4.video.w,
      band: m4.owner.geo && m4.owner.geo.bandWidth }));
    console.log('storedKeys', JSON.stringify(await tab.js(
      `Object.keys(localStorage).filter(k => /avatarVideo|ui_layout|uivm|viewMode/i.test(k)).sort()`)));
    console.log('conn counters @end:', JSON.stringify(await tab.js(CONN_COUNTS)));
  } catch (e) { console.log('DRAG_THROW', String(e.message).slice(0, 600)); }
  await tab.js(`(() => { if (window.__DOUBLE_TIMER) clearInterval(window.__DOUBLE_TIMER); return 1; })()`);
  await tab.close(); conn.close();
  console.log('done');
  process.exit(0);
})().catch((e) => { console.error('SMOKE_CRASH', String(e && e.stack || e).slice(0, 900)); process.exit(1); });
