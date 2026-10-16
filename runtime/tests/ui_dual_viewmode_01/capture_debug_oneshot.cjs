/* ============================================================================
 * capture_debug_oneshot.cjs — 把每种捕获组合的像素真正落盘，人眼看 + 数像素
 *
 * 现场事实（capture_channel_probe.json）：
 *   · 后台标签页下 fromSurface:true / false / beyond 三种组合全部 30s 超时；
 *   · Page.bringToFront 之后 fromSurface:true "成功"，但三次捕获是【同一张】
 *     9023 字节、99.58% 纯洋红的图 —— 注入色块对它没有任何影响，
 *     说明那是 compositor 的色键/陈旧 surface，不是本 document。
 *
 * 本脚本做决定性 A/B：先注入满屏洋红再拍、再撤掉拍，把每一张都存成文件，
 * 这样即便自动判据出错，我也能【亲眼看见】拍到的到底是什么。
 * 结束后不关标签页，把 targetId 打出来继续用它调试。
 * ==========================================================================*/
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { connect, Tab, sleep } = require('./cdp.cjs');
const P = require('./png_probe.cjs');

const EV = path.join(__dirname, '..', '..', 'evidence', 'UI_DUAL_VIEWMODE_01');
const DBG = path.join(EV, 'CAPTURE_DEBUG');

async function shot(tab, tag, params) {
  const t0 = Date.now();
  try {
    const buf = await tab.send('Page.captureScreenshot',
      { format: 'png', ...params }, 25000);
    const b = Buffer.from(buf.data, 'base64');
    const f = path.join(DBG, tag + '.png');
    fs.writeFileSync(f, b);
    let info = { tag, params, ms: Date.now() - t0, bytes: b.length, file: path.basename(f) };
    try {
      const img = P.decode(b);
      info.dims = [img.width, img.height];
      info.magenta = P.fraction(img, P.isMagenta, 8);
      info.notblack = P.fraction(img, P.isNotBlack, 8);
      info.lum = P.luminanceSpread(img);
      info.corner = P.pixel(img, 3, 3);
      info.center = P.pixel(img, img.width >> 1, img.height >> 1);
    } catch (e) { info.decode_error = e.message; }
    console.log(JSON.stringify(info));
    return info;
  } catch (e) {
    const info = { tag, params, ms: Date.now() - t0, error: String(e.message).slice(0, 160) };
    console.log(JSON.stringify(info));
    return info;
  }
}

(async () => {
  fs.mkdirSync(DBG, { recursive: true });
  const conn = await connect();
  const results = [];

  /* ---------- A. 后台标签页（当前验收用的形态） ---------- */
  const tab = await Tab.create(conn, 'about:blank');
  await tab.setViewport(1920, 1080);
  await tab.navigate('http://127.0.0.1:7860/', 2500);
  results.push({ scenario: 'A_bg_tab', dom: await tab.js(`({
    href: location.href, title: document.title,
    marker: !!document.getElementById('__cap_proof'),
    bodyBg: getComputedStyle(document.body).backgroundColor,
    vmmode: document.body.dataset.viewMode || null })`), targetId: tab.targetId });

  await tab.js(`(() => { const d = document.createElement('div');
    d.id='__cap_proof'; d.style.cssText='position:fixed;inset:0;z-index:2147483647;background:rgb(255,0,255)';
    document.body.appendChild(d); return !!document.getElementById('__cap_proof'); })()`);
  results.push({ scenario: 'A_with_marker_injected',
    present: await tab.js('!!document.getElementById("__cap_proof")') });
  await sleep(600);
  results.push(await shot(tab, 'A_bg_marker_true', { fromSurface: true }));
  results.push(await shot(tab, 'A_bg_marker_false', { fromSurface: false }));
  await tab.js(`(() => { const d = document.getElementById('__cap_proof'); if (d) d.remove(); return 1; })()`);
  await sleep(600);
  results.push(await shot(tab, 'A_bg_after_remove', { fromSurface: true }));

  /* ---------- B. 把本标签页切到窗口最前（会短暂改变用户看到的标签页） ---------- */
  try { await tab.send('Page.bringToFront'); results.push({ scenario: 'B_bringToFront', ok: true }); }
  catch (e) { results.push({ scenario: 'B_bringToFront', error: String(e.message).slice(0, 160) }); }
  await sleep(1500);
  results.push(await shot(tab, 'B_front_clean', { fromSurface: true }));
  await tab.js(`(() => { const d = document.createElement('div');
    d.id='__cap_proof'; d.style.cssText='position:fixed;inset:0;z-index:2147483647;background:rgb(255,0,255)';
    document.body.appendChild(d); return 1; })()`);
  await sleep(600);
  results.push(await shot(tab, 'B_front_marker', { fromSurface: true }));
  await tab.js(`(() => { const d = document.getElementById('__cap_proof'); if (d) d.remove(); return 1; })()`);
  await sleep(600);
  results.push(await shot(tab, 'B_front_clean_again', { fromSurface: true }));

  /* ---------- C. 独立新窗口（不动用户当前标签页） ---------- */
  let tab2 = null;
  try {
    const t = await conn.send('Target.createTarget',
      { url: 'http://127.0.0.1:7860/', background: false, newWindow: true });
    const a = await conn.send('Target.attachToTarget', { targetId: t.targetId, flatten: true });
    tab2 = new Tab(conn, t.targetId, a.sessionId);
    await tab2.send('Page.enable');
    await tab2.send('Emulation.setDeviceMetricsOverride',
      { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false });
    await sleep(3000);
    const w = await conn.send('Browser.getWindowForTarget', { targetId: t.targetId });
    results.push({ scenario: 'C_new_window', targetId: t.targetId, windowId: w.windowId,
      bounds: w.bounds, dom: await tab2.js('({href:location.href,title:document.title,ready:document.readyState})') });
    results.push(await shot(tab2, 'C_win_clean', { fromSurface: true }));
    await tab2.js(`(() => { const d = document.createElement('div');
      d.id='__cap_proof'; d.style.cssText='position:fixed;inset:0;z-index:2147483647;background:rgb(255,0,255)';
      document.body.appendChild(d); return 1; })()`);
    await sleep(600);
    results.push(await shot(tab2, 'C_win_marker', { fromSurface: true }));
    await tab2.js(`(() => { const d = document.getElementById('__cap_proof'); if (d) d.remove(); return 1; })()`);
  } catch (e) {
    results.push({ scenario: 'C_new_window', error: String(e.message).slice(0, 200) });
  }

  /* ---------- D. 前台 + 取消设备度量覆写（怀疑覆写让 surface 尺寸对不上） ---------- */
  try {
    await conn.send('Browser.getWindowForTarget', { targetId: tab.targetId });
    await tab.send('Emulation.clearDeviceMetricsOverride');
    results.push({ scenario: 'D_clear_metrics_override', ok: true });
  } catch (e) { results.push({ scenario: 'D_clear_metrics_override', error: String(e.message).slice(0, 160) }); }
  await sleep(1200);
  results.push(await shot(tab, 'D_nofront_clean', { fromSurface: true }));
  try { await tab.send('Page.bringToFront'); } catch (_) {}
  await sleep(1500);
  results.push(await shot(tab, 'D_front_clean', { fromSurface: true }));
  await tab.js(`(() => { const d = document.createElement('div');
    d.id='__cap_proof'; d.style.cssText='position:fixed;inset:0;z-index:2147483647;background:rgb(255,0,255)';
    document.body.appendChild(d); return 1; })()`);
  await sleep(700);
  results.push(await shot(tab, 'D_front_marker', { fromSurface: true }));
  await tab.js(`(() => { const d = document.getElementById('__cap_proof'); if (d) d.remove(); return 1; })()`);
  await sleep(700);
  results.push(await shot(tab, 'D_front_after', { fromSurface: true }));
  results.push(await shot(tab, 'D_front_after_noSurface', { fromSurface: false }));
  results.push({ scenario: 'D_viewport_now',
    vp: await tab.js('({iw:innerWidth, ih:innerHeight, dpr:devicePixelRatio})'),
    domMode: await tab.js('document.body.dataset.viewMode || null') });

  fs.writeFileSync(path.join(DBG, 'CAPTURE_DEBUG.json'),
    JSON.stringify({ at: new Date().toISOString(), results }, null, 2));

  /* 收尾必须关掉自己开的标签页：后台标签页被 close 时 Chrome 会把窗口切回
   * 用户原先看的那个标签页 —— 这比"猜哪个是前台再 activate"可靠得多，
   * 不需要知道 z-order 也不会有猜错带来的第二次切换。 */
  try { await tab.close(); } catch (_) {}
  if (tab2) { try { await conn.send('Target.closeTarget', { targetId: tab2.targetId }); } catch (_) {} }
  console.log('ALL_DONE');
  conn.close();
})().catch((e) => { console.error('DBG_CRASH', String(e && e.stack || e).slice(0, 600)); process.exitCode = 1; });
