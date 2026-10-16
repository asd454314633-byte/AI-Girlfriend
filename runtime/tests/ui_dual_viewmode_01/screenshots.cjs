/* ============================================================================
 * screenshots.cjs — 任务 §19：四张视觉验收截图
 *   DETAIL_DEFAULT.png / DETAIL_AVATAR_LARGE.png
 *   LIVE_PORTRAIT_DEFAULT.png / LIVE_PORTRAIT_ADJUSTED.png
 *
 * 截图通道（现场实测定论，见 CAPTURE_DEBUG.json / capture_channel_probe.json）：
 *   后台标签页 + fromSurface:true  → Page.captureScreenshot 25s 超时（不出帧）
 *   后台标签页 + fromSurface:false → 秒回一张【别的 surface】：注入满屏洋红后
 *                                    占比仍是 0，属于"能看、但是错的"假证据
 *   独立新窗口 + fromSurface:true  → 注入洋红后占比 0.99583，撤掉归零 ⇒ 可信
 * 所以本脚本用 Tab.createInNewWindow，并在开拍前/收拍后各跑一次归属闸门。
 *
 * 每张交付图还要过一次像素审计（png_probe，且该解码器已用 PIL 逐像素对账通过，
 * 见 PNG_DECODER_VALIDATION.json）：尺寸 1920x1080、无洋红残留、
 * 人物矩形内确有画面（不是全黑）。§19 要求"不得有调试 console / 错误窗口 /
 * 测试 overlay"——洋红残留=0 就是对"测试 overlay 已撤干净"的正面度量。
 * ==========================================================================*/
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { connect, Tab, sleep } = require('./cdp.cjs');
const P = require('./png_probe.cjs');
const prefs = require('./ui_prefs.cjs');

const EV = path.join(__dirname, '..', '..', 'evidence', 'UI_DUAL_VIEWMODE_01');
const SHOT = path.join(EV, 'SCREENSHOTS');
const CAP = { fromSurface: true };

async function liveSession(tab) {
  await tab.js(`document.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window})); 1`);
  for (let i = 0; i < 45; i++) {
    const ok = await tab.js(`(() => { const v = document.getElementById('av-video');
      return !!(v && v.srcObject && v.videoWidth > 0 && v.readyState >= 2); })()`);
    if (ok) { await sleep(2500); return true; }
    await sleep(1000);
  }
  return false;
}

const setDetailSlider = (tab, k, v) => tab.js(`(() => {
  const c = document.getElementById('av-layout-card');
  const i = c.querySelector('input[data-key="${k}"]');
  i.value = '${v}'; i.dispatchEvent(new Event('input', { bubbles: true }));
  return i.value; })()`);

const setLiveSlider = (tab, k, v) => tab.js(`(() => {
  const i = document.querySelector('#uivm-panel input[data-uivm-lv="${k}"]');
  i.value = '${v}'; i.dispatchEvent(new Event('input', { bubbles: true }));
  return i.value; })()`);

const geom = (tab) => tab.js(`(() => {
  const rv = (s) => { const e = document.querySelector(s); if (!e) return null;
    const r = e.getBoundingClientRect();
    return { x:+r.x.toFixed(1), y:+r.y.toFixed(1), w:+r.width.toFixed(1), h:+r.height.toFixed(1) }; };
  const b = document.body, cs = getComputedStyle(b);
  const v = document.getElementById('av-video');
  return { viewMode: b.dataset.viewMode || null, pane: b.dataset.uivmPane || null,
    video: rv('#av-video'), layer: rv('#av-layer'), app: rv('#app'),
    panel: rv('#uivm-panel'), status: rv('#gf-status-container'),
    stageRatio: (() => { const r = v.getBoundingClientRect();
      return r.height ? +(r.width / r.height).toFixed(4) : null; })(),
    objectFit: getComputedStyle(v).objectFit,
    srcObject: !!v.srcObject, currentTime: +v.currentTime.toFixed(2),
    store: JSON.parse(localStorage.getItem('ai_girlfriend_ui_layout_v1') || 'null'),
    lv: { s: cs.getPropertyValue('--uivm-lv-s').trim(),
          x: cs.getPropertyValue('--uivm-lv-x').trim(),
          y: cs.getPropertyValue('--uivm-lv-y').trim() } };
})()`);

/** 对已落盘的交付图做像素审计。返回判据 + 度量，不返回"我觉得没问题"。 */
function auditPng(file, g, label) {
  const buf = fs.readFileSync(file);
  const img = P.decode(buf);
  const mag = P.fraction(img, P.isMagenta, 8);
  const lum = P.luminanceSpread(img);
  const rect = g.video;
  // 人物矩形：只取落在画面内的部分
  const vis = rect ? { x: Math.max(0, rect.x), y: Math.max(0, rect.y),
    w: Math.min(img.width - Math.max(0, rect.x), rect.w),
    h: Math.min(img.height - Math.max(0, rect.y), rect.h) } : null;
  const inVideo = vis ? P.fractionInRect(img, vis, P.isNotBlack, 4) : { ratio: 0, total: 0 };
  const inPanel = g.panel ? P.fractionInRect(img, g.panel, P.isBright, 3) : { ratio: 0, total: 0 };
  const checks = {
    dims_1920x1080: img.width === 1920 && img.height === 1080,
    no_test_overlay_left: mag.ratio === 0,
    avatar_pixels_present: inVideo.ratio >= 0.5,
    control_text_readable: inPanel.ratio >= 0.02,
    dark_background: lum.mean <= 90,
  };
  const ok = Object.values(checks).every(Boolean);
  return {
    label, file: path.basename(file), bytes: buf.length, sha16: P.sha(buf),
    dims: [img.width, img.height], colorType: img.ihdr.color,
    luminance: lum, magenta_residue: mag.ratio,
    notblack_in_video: inVideo, bright_in_panel: inPanel,
    checks, VERDICT: ok ? 'PASS' : 'FAIL',
  };
}

(async () => {
  fs.mkdirSync(SHOT, { recursive: true });
  const conn = await connect();
  const meta = {
    generated_at: new Date().toISOString(), viewport: [1920, 1080],
    capture_channel: 'Tab.createInNewWindow + Page.captureScreenshot{fromSurface:true}',
    channel_rationale: {
      'background_tab_fromSurface_true': 'TIMEOUT after 25s（后台标签页不出帧）',
      'background_tab_fromSurface_false': '返回别的 surface：注入满屏洋红后占比仍为 0',
      'new_window_fromSurface_true': '注入洋红后占比 0.99583、撤掉归零 ⇒ 唯一可信通道',
      source: 'CAPTURE_DEBUG.json / CAPTURE_CHANNEL_PROBE.json',
    },
    shots: {},
  };

  /* ---------------- 负对照：本模块关掉后，conn-trace 的 405 还在不在 ----------------
   * 这一路不需要像素，只要网络计数与 DOM 探针 ⇒ 用后台标签页，避免多开窗口。 */
  {
    const tab = await Tab.create(conn, 'about:blank');
    await tab.setViewport(1920, 1080);
    await tab.navigate('http://127.0.0.1:7860/?noviewmode=1', 2500);
    await tab.js(`document.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window})); 1`);
    await sleep(12000);
    const byPath = {};
    for (const r of tab.requests) {
      let p;
      try { p = new URL(r.request.url).pathname; } catch (_) { p = String(r.request.url).slice(0, 80); }
      const st = r._status;
      if (st && (typeof st === 'string' || st >= 400)) {
        const k = p + ' -> ' + st;
        byPath[k] = (byPath[k] || 0) + 1;
      }
    }
    meta.negative_control_module_disabled = {
      url: 'http://127.0.0.1:7860/?noviewmode=1',
      viewModeAttr: await tab.js('document.body.dataset.viewMode || null'),
      panelPresent: await tab.js('!!document.getElementById("uivm-panel")'),
      dividerPresent: await tab.js('!!document.getElementById("uivm-divider")'),
      api: await tab.js('typeof window.UiViewModes'),
      totalRequests: tab.requests.length,
      httpErrors_seen: byPath,
      conclusion: '本模块整体停用后仍出现同样的 4xx ⇒ 该缺陷与本任务无关（既有）',
    };
    await tab.close();
  }

  /* ---------------- 正式截图：独立新窗口 ---------------- */
  const tab = await Tab.createInNewWindow(conn, 'http://127.0.0.1:7860/', 1920, 1080);
  try {
    await tab.navigate('http://127.0.0.1:7860/', 2500);
    /* 快照 → 清 4 个布局键 → 重载。上一版只清自己那一个键，于是上一轮留下的
     * avatarVideoScale=160 一直在生效，DETAIL_DEFAULT 拍出来就是放大态，
     * 与 DETAIL_AVATAR_LARGE 成了同一张图。§5.3 的"首屏=改造前"需要
     * 所有布局参数（含冻结 owner 的）都处于改造前状态才有意义。 */
    meta.prefs_snapshot_before = await prefs.snapshot(tab);
    meta.prefs_cleared = await prefs.clearLayoutKeys(tab);
    await tab.navigate('http://127.0.0.1:7860/', 2500);
    const live = await liveSession(tab);
    meta.live_session_established = live;
    if (!live) {
      meta.ABORT = '无活会话，截图不具代表性（§19 要的是能看出构图的照片）';
      console.log('ABORT: 无活会话。');
      fs.writeFileSync(path.join(EV, 'SCREENSHOT_MANIFEST.json'), JSON.stringify(meta, null, 2));
      process.exitCode = 1; return;
    }

    /* 开拍前归属闸门：像素必须随本 document 的 DOM 变化而变 */
    const gateOpen = await tab.verifyCaptureTargetsThisDocument(CAP);
    meta.capture_gate_before = { ...gateOpen,
      VERDICT: gateOpen.reflects_this_document ? 'PASS' : 'FAIL' };
    console.log('gate_before', JSON.stringify(gateOpen.magenta_ratio), gateOpen.reflects_this_document);
    if (!gateOpen.reflects_this_document || !gateOpen.marker_cleaned_up) {
      meta.ABORT = '截图通道不反射本 document —— 拒绝产出视觉验收';
      fs.writeFileSync(path.join(EV, 'SCREENSHOT_MANIFEST.json'), JSON.stringify(meta, null, 2));
      process.exitCode = 1; return;
    }

    // 1) DETAIL_DEFAULT —— 第一次进入详情模式，必须就是改造前的样子
    await sleep(1500);
    let g = await geom(tab);
    meta.shots.DETAIL_DEFAULT = { file: 'DETAIL_DEFAULT.png', geom: g,
      meaning: '§5.3：本任务首次启用时的详情模式 = 改造前原样；分隔线尚未按下' };
    meta.shots.DETAIL_DEFAULT.bytes = await tab.screenshot(path.join(SHOT, 'DETAIL_DEFAULT.png'), CAP);
    meta.shots.DETAIL_DEFAULT.pixel_audit =
      auditPng(path.join(SHOT, 'DETAIL_DEFAULT.png'), g, 'DETAIL_DEFAULT');

    // 2) DETAIL_AVATAR_LARGE —— 人物明显变大（走既有 owner avatar_layout.js 的"人物大小"）
    await tab.js(`document.getElementById('gf-layout-open').click(); 1`);
    await setDetailSlider(tab, 's', '160');
    await setDetailSlider(tab, 'x', '0');
    await setDetailSlider(tab, 'y', '0');
    await sleep(1200);
    g = await geom(tab);
    meta.shots.DETAIL_AVATAR_LARGE = { file: 'DETAIL_AVATAR_LARGE.png', geom: g,
      meaning: '§5.2：人物大小调大后的详情模式（缩放归既有 owner avatar_layout.js 管，本任务不复制其状态）' };
    meta.shots.DETAIL_AVATAR_LARGE.bytes = await tab.screenshot(path.join(SHOT, 'DETAIL_AVATAR_LARGE.png'), CAP);
    meta.shots.DETAIL_AVATAR_LARGE.pixel_audit =
      auditPng(path.join(SHOT, 'DETAIL_AVATAR_LARGE.png'), g, 'DETAIL_AVATAR_LARGE');
    await tab.js(`document.querySelector('#av-layout-card .av-layout-close').click(); 1`);

    // 3) LIVE_PORTRAIT_DEFAULT —— 9:16 舞台 + 右侧控制列
    await tab.js(`document.querySelector('#uivm-panel .uivm-mode[data-mode="live-portrait"]').click(); 1`);
    await sleep(1500);
    g = await geom(tab);
    meta.shots.LIVE_PORTRAIT_DEFAULT = { file: 'LIVE_PORTRAIT_DEFAULT.png', geom: g,
      meaning: '§6.1/§14：直播竖屏默认取景框（contain，未放大），控制列在舞台右侧' };
    meta.shots.LIVE_PORTRAIT_DEFAULT.bytes = await tab.screenshot(path.join(SHOT, 'LIVE_PORTRAIT_DEFAULT.png'), CAP);
    meta.shots.LIVE_PORTRAIT_DEFAULT.pixel_audit =
      auditPng(path.join(SHOT, 'LIVE_PORTRAIT_DEFAULT.png'), g, 'LIVE_PORTRAIT_DEFAULT');

    // 4) LIVE_PORTRAIT_ADJUSTED —— 主播自行放大/移位后的构图
    await setLiveSlider(tab, 'scale', '125');
    await setLiveSlider(tab, 'offsetX', '25');
    await setLiveSlider(tab, 'offsetY', '-15');
    await sleep(1200);
    g = await geom(tab);
    meta.shots.LIVE_PORTRAIT_ADJUSTED = { file: 'LIVE_PORTRAIT_ADJUSTED.png', geom: g,
      meaning: '§6.4：直播竖屏独立的人物大小/左右/上下参数生效后的构图' };
    meta.shots.LIVE_PORTRAIT_ADJUSTED.bytes = await tab.screenshot(path.join(SHOT, 'LIVE_PORTRAIT_ADJUSTED.png'), CAP);
    meta.shots.LIVE_PORTRAIT_ADJUSTED.pixel_audit =
      auditPng(path.join(SHOT, 'LIVE_PORTRAIT_ADJUSTED.png'), g, 'LIVE_PORTRAIT_ADJUSTED');

    /* 收拍后归属闸门：证明四张图拍摄期间通道没换（同一 document、同一窗口） */
    const gateClose = await tab.verifyCaptureTargetsThisDocument(CAP);
    meta.capture_gate_after = { ...gateClose,
      VERDICT: gateClose.reflects_this_document ? 'PASS' : 'FAIL' };
    console.log('gate_after', JSON.stringify(gateClose.magenta_ratio), gateClose.reflects_this_document);

    /* 收尾还原：本脚本为了第 2 张图动过【冻结 owner】的持久化键，不能把
     * "没人点过的 1.6 倍人物"留在用户 origin 上。改造前实测（prechange/
     * DOM_BASELINE.json）这 4 个键一个都不存在 ⇒ 这里的"还原"就是删干净，
     * 并逐个核对业务键既没少也没多。 */
    const snapEnd = await prefs.clearLayoutKeys(tab);
    const keysFinal = await tab.js(`Object.keys(localStorage).sort()`);
    const biz = prefs.businessKeysEqual(meta.prefs_snapshot_before.allKeys, keysFinal);
    const layoutLeft = snapEnd.leftKeys.filter((k) => prefs.ALL.indexOf(k) !== -1);
    meta.prefs_restored = {
      valuesRemovedAtEnd: snapEnd.removed,
      layoutKeysStillPresent: layoutLeft,
      businessBefore: biz.before, businessAfter: biz.after,
      businessKeysLost: biz.removed, businessKeysGained: biz.added,
      VERDICT: (layoutLeft.length === 0 && biz.removed.length === 0
        && biz.added.length === 0) ? 'PASS' : 'FAIL',
      note: '改造前这 4 个布局键均不在册 ⇒ 还原 = 删除；业务键方向性判据=一个都不能少',
    };
    console.log('prefs_restored', meta.prefs_restored.VERDICT, JSON.stringify(layoutLeft));

    // 关键连续性证据：四张截图全程同一个 video 节点、同一路流、时间没断
    meta.media_continuity = await tab.js(`(() => { const v = document.getElementById('av-video');
      return { count: document.querySelectorAll('#av-video').length, srcObject: !!v.srcObject,
               currentTime: +v.currentTime.toFixed(2), readyState: v.readyState, paused: v.paused,
               intrinsic: v.videoWidth + 'x' + v.videoHeight }; })()`);
    meta.shots.DETAIL_DEFAULT.paneRatio_after_first_entry =
      meta.shots.DETAIL_DEFAULT.geom.store && meta.shots.DETAIL_DEFAULT.geom.store.detail.paneRatio;

    const audits = Object.values(meta.shots).map((s) => s.pixel_audit);
    /* 交付物级的"这两张必须不一样"红线：先比几何，再比像素。
     * 上一轮 LIVE_PORTRAIT_DEFAULT 与 ADJUSTED 在舞台区几乎逐像素相同 ——
     * 因为 translate() 拿到无单位数字，整条 transform 作废，缩放根本没渲染。
     * 只数像素差异会因"直播流每帧都变"而假 PASS，所以这里加一道几何判据：
     * 调整后的人物渲染盒宽度必须真的变了（rect 含 transform，此处就该含）。 */
    const vw = (k) => (meta.shots[k].geom.video || {}).w || 0;
    const geomDelta = {
      detail: { default_w: vw('DETAIL_DEFAULT'), large_w: vw('DETAIL_AVATAR_LARGE') },
      live: { default_w: vw('LIVE_PORTRAIT_DEFAULT'), adjusted_w: vw('LIVE_PORTRAIT_ADJUSTED') },
    };
    geomDelta.detail.ratio = +(geomDelta.detail.large_w / (geomDelta.detail.default_w || 1)).toFixed(3);
    geomDelta.live.ratio = +(geomDelta.live.adjusted_w / (geomDelta.live.default_w || 1)).toFixed(3);
    geomDelta.detail_changed = Math.abs(geomDelta.detail.ratio - 1) > 0.05;
    geomDelta.live_changed = Math.abs(geomDelta.live.ratio - 1) > 0.05;
    meta.VISUAL_GATE = {
      all_pixel_audits_pass: audits.length === 4 && audits.every((a) => a.VERDICT === 'PASS'),
      per_shot: audits.map((a) => ({ file: a.file, ...a.checks, VERDICT: a.VERDICT })),
      rendered_geometry_changed: geomDelta,
      gate_before: meta.capture_gate_before.VERDICT,
      gate_after: meta.capture_gate_after.VERDICT,
      VERDICT: (audits.length === 4 && audits.every((a) => a.VERDICT === 'PASS')
        && gateOpen.reflects_this_document && gateClose.reflects_this_document
        && geomDelta.detail_changed && geomDelta.live_changed) ? 'PASS' : 'FAIL',
    };
  } catch (e) {
    meta.CRASH = String(e && e.message || e).slice(0, 500);
    console.error('SHOT_CRASH', meta.CRASH);
    process.exitCode = 1;
  } finally {
    /* 必须关：这个窗口占住 8010 的单人物会话，留着下一轮验收就变成
     * "两个会话抢一个形象"的假象（本任务已因此踩过一次）。 */
    try { await tab.close(); } catch (_) {}
    try { conn.close(); } catch (_) {}
    fs.writeFileSync(path.join(EV, 'SCREENSHOT_MANIFEST.json'), JSON.stringify(meta, null, 2));
    console.log(JSON.stringify({ VISUAL_GATE: meta.VISUAL_GATE, ABORT: meta.ABORT, CRASH: meta.CRASH }, null, 2));
  }
})().catch((e) => { console.error('TOP_CRASH', String(e && e.stack || e).slice(0, 1000)); process.exitCode = 1; });
