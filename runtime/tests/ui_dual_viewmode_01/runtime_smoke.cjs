/* ============================================================================
 * runtime_smoke.cjs — 任务 §21：最小运行时冒烟（生产页面，7860）
 *
 * §21 只要求"页面活着、人物在动、模式切得回去、既有功能还在"。
 * 明令【不做】的：TTS 长音频、会话故障注入、人物回滚、模型切换压力测试
 * —— 那些属冻结链的既有验收，不在本任务范围（§0/§23）。
 *
 *   node runtime_smoke.cjs        → evidence/UI_DUAL_VIEWMODE_01/RUNTIME_SMOKE.json
 * ==========================================================================*/
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { connect, Tab, sleep } = require('./cdp.cjs');
const prefs = require('./ui_prefs.cjs');

const EV = path.join(__dirname, '..', '..', 'evidence', 'UI_DUAL_VIEWMODE_01');
const SNAP = (tab) => tab.js(`(() => { const v = document.getElementById('av-video');
  return { t: v ? +v.currentTime.toFixed(3) : null, nodes: document.querySelectorAll('#av-video').length,
           stream: !!(v && v.srcObject), w: v ? Math.round(v.getBoundingClientRect().width) : 0,
           h: v ? Math.round(v.getBoundingClientRect().height) : 0,
           paused: v ? v.paused : null, readyState: v ? v.readyState : null,
           mode: document.body.dataset.viewMode || null,
           visible: v ? getComputedStyle(v).visibility !== 'hidden' && getComputedStyle(v).display !== 'none' : false };
})()`);

const out = { started_at: new Date().toISOString(), viewport: [1920, 1080], checks: [] };
function chk(id, name, ok, detail) {
  out.checks.push({ id, name, verdict: ok ? 'PASS' : 'FAIL', detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${id.padEnd(5)} ${name}`);
  if (detail !== undefined) console.log('        ' + JSON.stringify(detail).slice(0, 500));
}

(async () => {
  const conn = await connect();
  const tab = await Tab.create(conn, 'about:blank');
  await tab.setViewport(1920, 1080);
  await tab.navigate('http://127.0.0.1:7860/', 2500);
  out.prefs_snapshot = await prefs.snapshot(tab);
  /* 为了"从改造前的默认态起测"，把布局类 4 键（含冻结 owner 的）清掉再重载；
   * 收尾时再删一次 —— 本模块一加载就会写回默认值，这是它的正常生命周期。 */
  await prefs.clearLayoutKeys(tab);
  await tab.navigate('http://127.0.0.1:7860/', 2500);
  await tab.js(`document.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window})); 1`);

  /* 1) 人物可见（真实会话，不是空 video） */
  const live = await tab.wait(`(() => { const v = document.getElementById('av-video');
    return !!(v && v.srcObject && v.videoWidth > 0 && v.readyState >= 2); })()`, 60000, 1000);
  const s0 = await SNAP(tab);
  chk('SMOKE_SESSION', '§21 人物视频已上线（srcObject + 内在分辨率 + readyState≥2）', live && s0.stream, s0);

  /* 2) 反复切换两种模式 6 次：节点/流身份不变，时间持续增长 */
  const timeline = [];
  for (let i = 0; i < 6; i++) {
    const mode = i % 2 === 0 ? 'live-portrait' : 'detail';
    await tab.js(`document.querySelector('#uivm-panel .uivm-mode[data-mode="${mode}"]').click(); 1`);
    await sleep(900);
    timeline.push({ i, want: mode, ...(await SNAP(tab)) });
  }
  const t0 = timeline[0].t, t1 = timeline[timeline.length - 1].t;
  chk('SMOKE_SWITCH', '§21 反复切换 6 次：模式确实跟着切、人物始终在动',
    timeline.every((x) => x.nodes === 1 && x.stream && !x.paused && x.visible && x.w > 100
      && x.mode === x.want)
      && t1 > t0 + 3,
    { first: timeline[0], last: timeline[timeline.length - 1],
      modes: timeline.map((x) => x.mode).join(','), widths: timeline.map((x) => x.w).join(','),
      currentTime: [t0, t1] });
  chk('SMOKE_IDENTITY', '§2 全程 #av-video 节点数 = 1（没有第二个视频）',
    timeline.every((x) => x.nodes === 1), { nodes_per_step: timeline.map((x) => x.nodes).join(',') });

  /* 3) 既有功能仍在原位可用：聊天输入 + 控制区 + 模型/语音下拉 + 画面布局入口 */
  const feats = await tab.js(`(() => {
    const q = (s) => { const e = document.querySelector(s); if (!e) return null;
      const r = e.getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height) }; };
    return { chatInput: q('#gf-composer-input'), send: q('#gf-composer-send'),
      controls: q('.gf-controls'), modelSel: q('#gf-model'), voiceSel: q('#gf-voice-model'),
      layoutBtn: q('#gf-layout-open'), avatarBtn: q('#gf-avatar-open'), readingBtn: q('#gf-reading-open'),
      settings: q('#settings-btn'), status: q('#gf-status-container'), conv: q('#gf-live-conversation') };
  })()`);
  const nonEmpty = (x) => !!x && x.w > 20 && x.h > 8;
  chk('SMOKE_FEATURES', '§2 既有聊天输入/发送/控制区/模型与语音下拉/画面布局入口全部在册',
    nonEmpty(feats.chatInput) && nonEmpty(feats.send) && nonEmpty(feats.controls)
      && nonEmpty(feats.modelSel) && nonEmpty(feats.voiceSel) && nonEmpty(feats.layoutBtn)
      && nonEmpty(feats.status) && nonEmpty(feats.conv),
    feats);

  /* 4) 无未捕获 JS 异常（CDP 事件全程收集） */
  const jsEx = tab.exceptions.filter((e) => e.kind === 'JS_EXCEPTION');
  chk('SMOKE_NO_JS_ERROR', '§21 冒烟全程无未捕获 JS 异常', jsEx.length === 0,
    { jsExceptions: jsEx.length, sample: jsEx.slice(0, 3) });

  /* 收尾：本轮为"从默认态起测"清过布局键，模块加载时又会写回默认值（正常生命周期），
   * 所以这里再删一次，让 origin 回到改造前实测状态；业务键一根手指不碰。 */
  const snapEnd = await prefs.clearLayoutKeys(tab);
  const endKeys = await tab.js(`Object.keys(localStorage).sort()`);
  const biz = prefs.businessKeysEqual(out.prefs_snapshot.allKeys, endKeys);
  const layoutLeft = endKeys.filter((k) => prefs.ALL.indexOf(k) !== -1);
  chk('SMOKE_PREFS', '收尾：布局键回到改造前状态，业务键零增删',
    layoutLeft.length === 0 && snapEnd.leftKeys.every((k) => prefs.ALL.indexOf(k) === -1)
      && biz.removed.length === 0 && biz.added.length === 0,
    { layoutLeft, removedThisRun: Object.keys(snapEnd.removed || {}),
      lost: biz.removed, gained: biz.added });

  out.VERDICT = out.checks.every((c) => c.verdict === 'PASS') ? 'PASS' : 'FAIL';
  out.finished_at = new Date().toISOString();
  fs.writeFileSync(path.join(EV, 'RUNTIME_SMOKE.json'), JSON.stringify(out, null, 2));
  console.log('\nRUNTIME_SMOKE = ' + out.VERDICT);
  await tab.close();
  conn.close();
})().catch((e) => {
  out.CRASH = String(e && e.stack || e).slice(0, 800);
  console.error('SMOKE_CRASH', out.CRASH);
  try { fs.writeFileSync(path.join(EV, 'RUNTIME_SMOKE.json'), JSON.stringify(out, null, 2)); } catch (_) {}
  process.exitCode = 1;
});
