/* ============================================================================
 * capture_channel_probe.cjs — 找出"唯一可信的截图通道"，按侵入度从低到高试
 *
 * 现场事实：上一轮 fromSurface:true 在后台标签页上拍到了 ComfyUI(8196) 窗口，
 * 而几何/存储数据全是对的 ⇒ 数据与像素不同源。§19 要的是【视觉】验收，
 * 所以在确定通道之前，四张 PNG 一张都不能算数。
 *
 * 判据（见 cdp.cjs 的 verifyCaptureTargetsThisDocument）：往本 document 注入
 * 满屏纯洋红，解码捕获结果数占比，必须 ≥0.95；撤掉后必须 ≤0.005。
 *
 * 阶梯按侵入度排序，取第一个通过者：
 *   1 后台标签页 + fromSurface:false
 *   2 后台标签页 + fromSurface:true
 *   3 后台标签页 + fromSurface:false + captureBeyondViewport:true
 *   4 Page.bringToFront 之后 + fromSurface:false     ← 会切换用户当前标签页
 *   5 Page.bringToFront 之后 + fromSurface:true
 * 只有 1~3 全部失败才允许走到 4/5；4/5 结束时必须把焦点状态写进证据里。
 * ==========================================================================*/
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { connect, Tab, sleep } = require('./cdp.cjs');

const EV = path.join(__dirname, '..', '..', 'evidence', 'UI_DUAL_VIEWMODE_01');

const LADDER = [
  { n: 'bg_fromSurface_false', opts: { fromSurface: false }, front: false },
  { n: 'bg_fromSurface_true', opts: { fromSurface: true }, front: false },
  { n: 'bg_false_beyond', opts: { fromSurface: false, beyond: true }, front: false },
  { n: 'front_fromSurface_false', opts: { fromSurface: false }, front: true },
  { n: 'front_fromSurface_true', opts: { fromSurface: true }, front: true },
];

(async () => {
  const conn = await connect();
  const tab = await Tab.create(conn, 'about:blank');
  await tab.setViewport(1920, 1080);
  await tab.navigate('http://127.0.0.1:7860/', 2500);
  const out = {
    probed_at: new Date().toISOString(),
    url: 'http://127.0.0.1:7860/',
    note: '不需要活的数字人会话：判据只看"本 document 的像素有没有被拍到"',
    scenarios: [],
  };
  let chosen = null;
  for (const s of LADDER) {
    if (chosen) { out.scenarios.push({ name: s.n, skipped: '已有更低侵入度的通道通过' }); continue; }
    if (s.front) {
      try { await tab.send('Page.bringToFront'); } catch (e) { out.focus_error = String(e.message).slice(0, 200); }
      await sleep(1500);
      out.scenarios.push({ name: s.n + '_NOTE', focus_stolen: true });
    }
    let r;
    try { r = await tab.verifyCaptureTargetsThisDocument(s.opts); }
    catch (e) { r = { error: String(e && e.message || e).slice(0, 300) }; }
    out.scenarios.push({ name: s.n, fromSurface: s.opts.fromSurface, beyond: !!s.opts.beyond, ...r });
    if (r && r.reflects_this_document && r.marker_cleaned_up) chosen = s;
  }
  out.VERDICT_CHANNEL = chosen ? chosen.n : 'NONE_TRUSTWORTHY';
  out.screenshots_may_use = chosen ? chosen.opts : null;
  await tab.close();
  conn.close();
  fs.writeFileSync(path.join(EV, 'CAPTURE_CHANNEL_PROBE.json'), JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out, null, 2));
  process.exitCode = chosen ? 0 : 1;
})().catch((e) => { console.error('LADDER_CRASH', String(e && e.stack || e).slice(0, 800)); process.exitCode = 1; });
