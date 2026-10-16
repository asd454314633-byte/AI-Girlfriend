/* ============================================================================
 * viewport_recipe.cjs — 只回答一个问题：这台机器上，用什么 CDP 通道能把
 * 页面视口【稳定地】设成我要的尺寸？
 *
 * 起因：dock_acceptance 第一轮 mode_roundtrip 假 FAIL ——
 *   Emulation.setDeviceMetricsOverride 前几次生效（innerWidth=1920），
 *   在 1440→1920 这一对之后【永久失效】，innerWidth 退回真实窗口 1745。
 *   闸门是几何级的，视口不可控 = 所有对照作废，所以先把"哪条通道可靠"
 *   单独测清楚，再回头改验收探针。
 *
 * 每一步都：发命令 → 读 window.innerWidth/innerHeight + visualViewport →
 * 打成一行表格。纯测量，不改任何生产文件。
 * 用法： node viewport_recipe.cjs
 * ==========================================================================*/
'use strict';
const { connect, Tab, sleep, CDP_ENDPOINT } = require('../ui_dual_viewmode_01/cdp.cjs');

const READ = `(function(){return {iw: window.innerWidth, ih: window.innerHeight,
  vv: (window.visualViewport||{}).width + 'x' + (window.visualViewport||{}).height,
  ow: window.outerWidth, oh: window.outerHeight,
  dpr: window.devicePixelRatio, vscale: (window.visualViewport||{}).scale};})()`;

(async function main() {
  console.log('CDP_ENDPOINT = ' + CDP_ENDPOINT);
  const conn = await connect();
  const tab = await Tab.create(conn, 'about:blank');
  await tab.navigate('http://127.0.0.1:7860/?probe=viewport-recipe', 4000);
  await sleep(2000);
  const rows = [];
  const step = async (label, fn) => {
    let err = null;
    try { await fn(); } catch (e) { err = String(e.message || e).slice(0, 120); }
    const v = await tab.js(READ);
    rows.push({ label, err, ...v });
    console.log(String(rows.length).padStart(2) + ' ' + label.padEnd(46) +
      ' inner=' + (v.iw + 'x' + v.ih).padEnd(11) + ' outer=' + (v.ow + 'x' + v.oh).padEnd(11) +
      ' vv=' + v.vv + (err ? '  ERR=' + err : ''));
  };

  const setM = (w, h, dsf = 1, mobile = false) => () =>
    tab.send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: dsf, mobile });

  await step('baseline (no override)', async () => {});
  await step('A1 set 1920x1080', setM(1920, 1080));
  await step('A2 set 1440x900', setM(1440, 900));
  await step('A3 set 1920x1080 again', setM(1920, 1080));
  await step('B1 clear', () => tab.send('Emulation.clearDeviceMetricsOverride', {}));
  await step('B2 set 1920x1080 after clear', setM(1920, 1080));
  await step('B3 set 1440x900', setM(1440, 900));
  await step('B4 set 1920x1080 (dsf=0)', setM(1920, 1080, 0));
  await step('C1 clear + Page.setDeviceMetricsOverride?', async () => {});
  await step('C2 clear again', () => tab.send('Emulation.clearDeviceMetricsOverride', {}));

  /* D 组：改真实窗口尺寸（对本 tab 所属窗口） */
  let winId = null;
  try {
    const r = await tab.send('Browser.getWindowForTarget', {});
    winId = r.windowId;
    console.log('   windowForTarget -> id=' + winId + ' bounds=' + JSON.stringify(r.bounds));
    rows.push({ windowId: winId, bounds: r.bounds });
  } catch (e) { console.log('   getWindowForTarget ERR ' + e.message); }
  if (winId !== null) {
    await step('D1 setWindowBounds 2000x1150', () =>
      conn.send('Browser.setWindowBounds', { windowId: winId, bounds: { width: 2000, height: 1150 } }));
    await sleep(600);
    rows.push({ label: 'D1b re-read', err: null, ...(await tab.js(READ)) });
    console.log('   D1b inner=' + (rows[rows.length - 1].iw + 'x' + rows[rows.length - 1].ih));
    await step('D2 setWindowBounds 1500x950', () =>
      conn.send('Browser.setWindowBounds', { windowId: winId, bounds: { width: 1500, height: 950 } }));
    await sleep(600);
    rows.push({ label: 'D2b re-read', err: null, ...(await tab.js(READ)) });
    console.log('   D2b inner=' + (rows[rows.length - 1].iw + 'x' + rows[rows.length - 1].ih));
    await step('D3 override 1920x1080 on small window', setM(1920, 1080));
    await step('D4 clear', () => tab.send('Emulation.clearDeviceMetricsOverride', {}));
  }

  /* E 组：Emulation 之前是否必须先 Page.enable / 是否有 metric override 冲突 */
  await step('E1 set 1920x1080 with mobile:true', setM(1920, 1080, 1, true));
  await step('E2 set 1920x1080 mobile:false after mobile:true', setM(1920, 1080, 1, false));
  await step('E3 clear', () => tab.send('Emulation.clearDeviceMetricsOverride', {}));

  /* F 组：重复同一大小 6 次，看是否"第 N 次之后失效" */
  for (let i = 1; i <= 6; i++) await step('F' + i + ' set 1920x1080 (repeat)', setM(1920, 1080));
  /* G 组：交替 1440/1920 共 8 次，找失效的临界次数 */
  for (let i = 1; i <= 8; i++) {
    await step('G' + i + 'a set 1440x900', setM(1440, 900));
    await step('G' + i + 'b set 1920x1080', setM(1920, 1080));
  }
  try { await tab.close(); } catch (_) {}
  console.log('\nVERDICT_TABLE ' + JSON.stringify(rows.map((r) => r.label ? {
    label: r.label, inner: r.iw + 'x' + r.ih, outer: r.ow + 'x' + r.oh, err: r.err } : r), null, 1));
  conn.close();
  process.exit(0);
})().catch((e) => { console.log('VIEWPORT_RECIPE_CRASH ' + String((e && e.stack) || e).slice(0, 1500)); process.exit(1); });
