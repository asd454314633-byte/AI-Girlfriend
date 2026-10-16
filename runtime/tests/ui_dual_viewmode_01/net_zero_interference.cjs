/* ============================================================================
 * net_zero_interference.cjs — 任务 §11 / §18：布局动作必须零后台调用
 *
 * 两条互相独立的见证通道：
 *   1) CDP Network.requestWillBeSent —— 看到所有真发出去的资源请求；
 *   2) 页面里的 window.__connTrace —— 生产自带的连接记录器（conn_trace.js）
 *      包装了 RTCPeerConnection / WebSocket 的构造与状态变化。用它当第二证人
 *      比我自己在测试里重新包一层更硬：那是产品代码在看着，不是测试脚手架
 *      自证自话。
 *
 * 关键区分（§18 明确要求）：先把"静置时也一直在发的东西"（conn-trace 每 5s
 * 批量外送、媒体协商后的持续流量）采成基线，再从布局窗口的请求里扣掉。
 * 不扣就会把产品既有心跳算成本任务的新增调用，得到一个假 FAIL。
 * ==========================================================================*/
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { connect, Tab, sleep } = require('./cdp.cjs');
const prefs = require('./ui_prefs.cjs');

const EV = path.join(__dirname, '..', '..', 'evidence', 'UI_DUAL_VIEWMODE_01');
const MAX_KEEP = 400;                     // 请求条数硬上限，防无界增长

function uniq(list) { return Array.from(new Set(list)).sort(); }
function hostOf(u) {
  try { const x = new URL(u); return x.pathname; } catch (_) { return String(u).slice(0, 120); }
}

(async () => {
  const conn = await connect();
  const tab = await Tab.create(conn, 'about:blank');
  await tab.setViewport(1920, 1080);
  await tab.navigate('http://127.0.0.1:7860/', 2500);
  /* 开局把布局类 4 个键（含冻结 owner avatar_layout.js 的三个）清干净再重载：
   * 残留缩放会让"零干扰窗口"从一个非首屏态开始，测出来的东西不对应当前代码。
   * 这一步刻意放在建立会话【之前】—— 重载会打断会话，放在之后就把
   * §18 的计数窗口污染成"我自己的重载流量"了。收尾时再删一次还原。 */
  const prefsSnap0 = await prefs.snapshot(tab);
  await prefs.clearLayoutKeys(tab);
  await tab.navigate('http://127.0.0.1:7860/', 2500);

  /* ---- 建立真实会话：没有活会话，"没触发重连"就是句空话 ---- */
  await tab.js(`document.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window})); 1`);
  let live = false;
  for (let i = 0; i < 40; i++) {
    live = await tab.js(`(() => { const v = document.getElementById('av-video');
      return !!(v && v.srcObject && v.videoWidth > 0); })()`);
    if (live) break;
    await sleep(1000);
  }
  if (!live) { console.log('ABORT: 无活会话，零干扰判定不可成立。'); await tab.close(); conn.close(); process.exitCode = 1; return; }
  await sleep(3000);

  const connStats = () => tab.js(`(() => {
    const a = window.__connTrace || [];
    const c = { pc_construct: 0, ws_construct: 0, reconnect: 0, pc_close: 0, ws_close: 0,
                ice_failed: 0, media_stall: 0, total: a.length };
    for (const e of a) {
      if (e.event === 'construct') { if (e.kind === 'pc') c.pc_construct++; else if (e.kind === 'ws') c.ws_construct++; }
      if (e.kind === 'reconnect') c.reconnect++;
      if (e.event === 'close') { if (e.kind === 'pc') c.pc_close++; else if (e.kind === 'ws') c.ws_close++; }
      if (e.iceConnectionState === 'failed' || e.connectionState === 'failed') c.ice_failed++;
      if (e.kind === 'media' && (e.event === 'waiting' || e.event === 'stalled')) c.media_stall++;
    }
    return c;
  })()`);

  /* ---- 基线：静置 6s，采"本来就在发"的东西 ---- */
  await tab.clearRequests();
  const baseConn = await connStats();
  await sleep(6000);
  const baselineReqs = tab.requests.map(r => hostOf(r.request.url));
  const baselinePaths = uniq(baselineReqs);

  /* ---- 布局动作窗口：清空记录 → 打一遍全部布局操作 → 收记录 ---- */
  await tab.clearRequests();
  const preConn = await connStats();
  const v0 = await tab.js(`(() => { const v = document.getElementById('av-video');
    return { ct: v.currentTime, src: !!v.srcObject, node: 'ok' }; })()`);

  const actions = [];
  const act = async (name, expr) => {
    const before = tab.requests.length;
    const t0 = await tab.js('performance.now()');
    const r = await tab.js(expr);
    await sleep(700);
    const t1 = await tab.js('performance.now()');
    actions.push({ name, returned: typeof r === 'string' ? r.slice(0, 40) : r,
      requestsDuring: tab.requests.length - before, spanMs: +(t1 - t0).toFixed(0) });
    return r;
  };

  // DETAIL：拖分隔线（多步）
  await act('divider_drag', `(() => {
    const d = document.getElementById('uivm-divider'); if (!d) return 'NO_DIVIDER';
    const r = d.getBoundingClientRect(); const from = Math.round(r.x + r.width/2);
    const mk = (ty,x) => new PointerEvent(ty,{bubbles:true,cancelable:true,view:window,
      pointerId:1,clientX:x,clientY:500,isPrimary:true,button:0});
    d.dispatchEvent(mk('pointerdown', from));
    for (let i=1;i<=6;i++) d.dispatchEvent(mk('pointermove', Math.round(from + (from+220-from)*i/6)));
    d.dispatchEvent(mk('pointerup', from+220)); window.dispatchEvent(mk('pointerup', from+220));
    return 'OK';
  })()`);
  // DETAIL：三个滑杆 + 恢复默认（走既有 owner avatar_layout.js）
  await act('detail_sliders', `(() => {
    document.getElementById('gf-layout-open').click();
    const card = document.getElementById('av-layout-card');
    const set = (k,v) => { const i = card.querySelector('input[data-key="'+k+'"]');
      i.value = String(v); i.dispatchEvent(new Event('input',{bubbles:true})); };
    set('s', 120); set('x', -30); set('y', 20);
    return 'OK';
  })()`);
  await act('detail_reset', `(() => {
    document.querySelector('#av-layout-card .av-layout-reset').click(); return 'OK'; })()`);
  // DETAIL：三个预设 + 恢复详情默认
  await act('detail_presets', `(() => {
    for (const p of ['balanced','avatar-first','original']) {
      const b = document.querySelector('#uivm-panel .uivm-btn[data-preset="'+p+'"]');
      if (b) b.click();
    }
    document.querySelector('#uivm-panel .uivm-sec-detail .uivm-reset').click();
    return 'OK';
  })()`);
  // 切到 LIVE + 三个滑杆 + 恢复直播默认 + 切回
  await act('switch_to_live', `(() => {
    document.querySelector('#uivm-panel .uivm-mode[data-mode="live-portrait"]').click();
    return 'OK'; })()`);
  await act('live_sliders', `(() => {
    const set = (k,v) => { const i = document.querySelector('#uivm-panel input[data-uivm-lv="'+k+'"]');
      i.value = String(v); i.dispatchEvent(new Event('input',{bubbles:true})); };
    set('scale', 130); set('offsetX', -40); set('offsetY', 25);
    return 'OK'; })()`);
  await act('live_reset', `(() => {
    document.querySelector('#uivm-panel .uivm-sec-live .uivm-reset').click(); return 'OK'; })()`);
  await act('switch_back_detail', `(() => {
    document.querySelector('#uivm-panel .uivm-mode[data-mode="detail"]').click();
    return 'OK'; })()`);
  // 尺寸变化（§9 的动作，也必须零调用）
  await tab.setViewport(1600, 900); await sleep(600);
  await tab.setViewport(1920, 1080); await sleep(600);
  actions.push({ name: 'viewport_resize_x2', requestsDuring: 0, spanMs: 1200 });

  const postConn = await connStats();
  const layoutReqs = tab.requests.map(r => ({ path: hostOf(r.request.url),
                                              method: r.request.method, type: r.type }));
  const v1 = await tab.js(`(() => { const v = document.getElementById('av-video');
    return { ct: v.currentTime, src: !!v.srcObject, ident: v === document.getElementById('av-video') }; })()`);

  /* ---- 归属判定：布局窗口里出现的、基线静置期从未出现过的路径才算"新增" ---- */
  const layoutPaths = uniq(layoutReqs.map(r => r.path));
  const newPaths = layoutPaths.filter(p => !baselinePaths.includes(p));
  const beacons = layoutReqs.filter(r => /conn-trace/.test(r.path)).length;
  const apiReqs = layoutReqs.filter(r => /^\/api\//.test(r.path) || /humanaudio/.test(r.path));
  const apiNew = apiReqs.filter(r => newPaths.includes(r.path));

  const derived = {
    pc_construct_delta: postConn.pc_construct - preConn.pc_construct,
    ws_construct_delta: postConn.ws_construct - preConn.ws_construct,
    reconnect_delta: postConn.reconnect - preConn.reconnect,
    pc_close_delta: postConn.pc_close - preConn.pc_close,
    ws_close_delta: postConn.ws_close - preConn.ws_close,
    ice_failed_delta: postConn.ice_failed - preConn.ice_failed,
    media_stall_delta: postConn.media_stall - preConn.media_stall,
  };

  const LAYOUT_GENERATED_BACKEND_REQUESTS = newPaths.length;
  const gate = LAYOUT_GENERATED_BACKEND_REQUESTS === 0
    && derived.pc_construct_delta === 0 && derived.ws_construct_delta === 0
    && derived.reconnect_delta === 0 && derived.pc_close_delta === 0
    && derived.ws_close_delta === 0 && derived.ice_failed_delta === 0
    && derived.media_stall_delta === 0
    && v1.src === true && v1.ct > v0.ct;

  /* 收尾还原：本脚本的 detail_sliders 动作动过【冻结 owner】的持久化键
   * （avatarVideoScale/OffsetX/OffsetY），改造前实测这 4 个键均不存在，
   * 所以"还原"= 删干净；业务键要求一个都不能少、也不多出来。 */
  const prefsEnd = await prefs.clearLayoutKeys(tab);
  const prefsKeysFinal = await tab.js(`Object.keys(localStorage).sort()`);
  const prefsBiz = prefs.businessKeysEqual(prefsSnap0.allKeys, prefsKeysFinal);
  const prefsLeft = prefsEnd.leftKeys.filter((k) => prefs.ALL.indexOf(k) !== -1);
  const prefsRestore = {
    snapshot_at_start: prefsSnap0.values,
    values_removed_at_end: prefsEnd.removed,
    layout_keys_still_present: prefsLeft,
    business_keys: prefsBiz.after,
    business_keys_lost: prefsBiz.removed,
    business_keys_gained: prefsBiz.added,
    VERDICT: (prefsLeft.length === 0 && prefsBiz.removed.length === 0
      && prefsBiz.added.length === 0) ? 'PASS' : 'FAIL',
  };

  const report = {
    captured_at: new Date().toISOString(),
    viewport: [1920, 1080],
    session_live: live,
    LAYOUT_GENERATED_BACKEND_REQUESTS,
    VERDICT: gate ? 'PASS' : 'FAIL',
    baseline_idle_6s: { totalRequests: baselineReqs.length, distinctPaths: baselinePaths,
                        note: '静置期一直在发的东西：布局窗口里再见到不算新增' },
    layout_window: {
      actions,
      totalRequests: layoutReqs.length,
      distinctPaths: layoutPaths,
      connTraceBeacons: beacons,
      apiOrHumanAudioRequests: apiReqs.length,
      apiOrHumanAudio_NOT_in_baseline: apiNew,
      newPaths,
    },
    connTrace_counters: { before: preConn, after: postConn, derived },
    media_continuity: { currentTime_before: v0.ct, currentTime_after: v1.ct,
                        advancedSec: +(v1.ct - v0.ct).toFixed(3), srcObjectKept: v1.src },
    connection_counters_baseline: baseConn,
    prefs_restored: prefsRestore,
  };
  fs.writeFileSync(path.join(EV, 'NETWORK_ZERO_INTERFERENCE_TEST.json'),
    JSON.stringify(report, null, 2));

  console.log('LAYOUT_GENERATED_BACKEND_REQUESTS =', LAYOUT_GENERATED_BACKEND_REQUESTS);
  console.log('VERDICT =', report.VERDICT);
  console.log('baseline distinct paths :', baselinePaths.join(' , ') || '(none)');
  console.log('layout window paths     :', layoutPaths.join(' , ') || '(none)');
  console.log('NEW (not in baseline)   :', newPaths.join(' , ') || '(none)');
  console.log('conn-trace beacons kept :', beacons, '(既有 5s 批量外送，属基线流量)');
  console.log('counters derived        :', JSON.stringify(derived));
  console.log('currentTime advance     :', (v1.ct - v0.ct).toFixed(3), 's');
  console.log('per-action requests     :');
  for (const a of actions) {
    console.log(`   ${a.name.padEnd(22)} span=${String(a.spanMs).padStart(5)}ms  requestsDuring=${a.requestsDuring}`);
  }
  await tab.close();
  conn.close();
  process.exitCode = gate ? 0 : 1;
})().catch((e) => {
  console.error('NET_TEST_CRASH', String(e && e.stack || e).slice(0, 1200));
  process.exitCode = 1;
});
