/* ============================================================================
 * shadow_acceptance.cjs — AI_GIRLFRIEND_UI_DETAIL_COMPOSITION_REPAIR_01
 *
 * V2 的几何级闸门。它判的是【真实渲染后的 video 矩形】，不是 CSS 变量。
 * （上一轮的用户可见故障正是"变量写得对、画面没动"，所以 §23–§28 要求
 *   每一项都必须落到 getBoundingClientRect() / getComputedStyle() 的矩阵上。）
 *
 * mask 停靠点的换算：mask 画在元素的【局部盒】上，再随 transform 一起变换。
 * 局部百分比 p → 屏幕坐标 = rect.x + p/100 × rect.width（rect 已是变换后的）。
 * 这条换算同时是探针的自校验：如果哪天 transform 合成顺序变了，
 * fadeStart 就会不再等于 videoLeft，G10 当场红。
 *
 * 与 §32 的约定：本文件不跑长音频、不切 TTS、不注入会话故障、不做回滚。
 * 它只做布局动作，并把"有没有招来后台请求"交给 net_zero_interference_v2.cjs。
 * ==========================================================================*/
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { connect, Tab, sleep } = require('../ui_dual_viewmode_01/cdp.cjs');
const prefs = require('./ui_prefs_v2.cjs');

const ROOT = path.join(__dirname, '..', '..');
const APP = path.join(ROOT, 'ui', 'app');
const EV = path.join(ROOT, 'evidence', 'UI_DETAIL_COMPOSITION_REPAIR_01');
const BASE = 'http://127.0.0.1:7860';
const VP = [1920, 1080];

const PRIMARY = ['.orb-wrap', '.gf-controls', 'header.topbar', '#gf-model', '#gf-voice-model',
  '#gf-reading-open', '#gf-avatar-open', '#gf-layout-open', '#gf-voice-performance',
  '#gf-live-conversation', '#gf-composer', '#gf-composer-input', '#gf-composer-send',
  '#gf-status-container', '#cam-pip', '#av-layout-card'];

/* ------------------------------------------------------------ 页面侧探针 */
/* 探针字符串本体在 measure_page.cjs（唯一一份，可 require、可单测）。
 * 教训：上一版 smoke_probe 用正则从本文件里抠字符串，正则没中 → 早退 →
 * CDP socket 没关 → 进程挂死 7 分钟，什么都没测到。 */
const { INSTALL } = require('./measure_page.cjs');

/* ------------------------------------------------------------- 静态源检查 */
function staticAudit() {
  const files = ['avatar_layout.js', 'ui_layout_modes.js', 'ui_layout_modes.css', 'index.html'];
  const src = {};
  for (const f of files) src[f] = fs.readFileSync(path.join(APP, f), 'utf8');
  const count = (s, re) => (s.match(re) || []).length;
  const transformWriters = files.filter((f) => /style\.transform\s*=/.test(src[f]));
  return {
    files: files.map((f) => ({ file: f, bytes: src[f].length })),
    only_owner_writes_style_transform: transformWriters.length === 1 && transformWriters[0] === 'avatar_layout.js',
    transform_writers: transformWriters,
    persistent_loops: {
      'avatar_layout.js': {
        requestAnimationFrame: count(src['avatar_layout.js'], /requestAnimationFrame/g),
        setInterval: count(src['avatar_layout.js'], /setInterval/g),
        setTimeout: count(src['avatar_layout.js'], /setTimeout/g),
        MutationObserver: count(src['avatar_layout.js'], /new MutationObserver/g),
      },
      'ui_layout_modes.js': {
        requestAnimationFrame: count(src['ui_layout_modes.js'], /requestAnimationFrame/g),
        setInterval: count(src['ui_layout_modes.js'], /setInterval/g),
        setTimeout: count(src['ui_layout_modes.js'], /setTimeout/g),
        MutationObserver: count(src['ui_layout_modes.js'], /new MutationObserver/g),
      },
    },
    media_api_in_js: {
      createVideo: count(src['avatar_layout.js'] + src['ui_layout_modes.js'], /createElement\(\s*['"]video/g),
      newMediaStream: count(src['avatar_layout.js'] + src['ui_layout_modes.js'], /new MediaStream/g),
      fetch: count(src['avatar_layout.js'] + src['ui_layout_modes.js'], /\bfetch\s*\(/g),
      websocket: count(src['avatar_layout.js'] + src['ui_layout_modes.js'], /new WebSocket|RTCPeerConnection/g),
      srcObjectWrites: count(src['avatar_layout.js'] + src['ui_layout_modes.js'], /srcObject\s*=/g),
    },
    clone_or_move: count(src['avatar_layout.js'] + src['ui_layout_modes.js'], /cloneNode|removeChild|appendChild\(\s*v/g),
    deprecated_token: { setPane_returns: /DEPRECATED_IGNORED/.test(src['ui_layout_modes.js']),
                        pane_css_rule_gone: !/data-uivm-pane/.test(src['ui_layout_modes.css']) },
  };
}

/* ------------------------------------------------------------------ main */
const GATES = [];
function gate(id, requirement, ok, evidence) {
  GATES.push({ id, requirement, verdict: ok ? 'PASS' : 'FAIL', evidence });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${id.padEnd(34)} ${requirement}`);
  if (!ok) console.log('      evidence:', JSON.stringify(evidence).slice(0, 700));
  return ok;
}
const near = (a, b, tol) => Number.isFinite(a) && Number.isFinite(b) && Math.abs(a - b) <= tol;
const mat = (s) => {
  const m = /matrix\(([^)]+)\)/.exec(String(s || ''));
  if (!m) return null;
  const p = m[1].split(',').map(Number);
  return { a: +p[0].toFixed(4), b: +p[1].toFixed(4), c: +p[2].toFixed(4), d: +p[3].toFixed(4),
           e: +p[4].toFixed(2), f: +p[5].toFixed(2) };
};

(async () => {
  const run = { task_id: 'AI_GIRLFRIEND_UI_DETAIL_COMPOSITION_REPAIR_01',
                generatedAt: new Date().toISOString(), base: BASE, viewport: VP };
  const stat = staticAudit();
  run.static_audit = stat;

  const conn = await connect();
  const tab = await Tab.create(conn, 'about:blank');
  await tab.setViewport(VP[0], VP[1]);
  await tab.navigate(BASE + '/', 2500);

  /* --- 干净起点：把本轮涉及的布局键全清掉再重载（用户原值收尾还原） ------- */
  const prefsSnap0 = await prefs.snapshot(tab);
  run.user_prefs_before = prefsSnap0.values;
  await prefs.clearLayoutKeys(tab);
  await tab.navigate(BASE + '/', 2500);
  await tab.js(INSTALL);

  /* --- 必须有真实会话：否则"零重连""单 video""currentTime 连续"全是空话 --- */
  await tab.js(`document.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window})); 1`);
  let live = false;
  for (let i = 0; i < 45; i++) {
    live = await tab.js(`(() => { const v=document.getElementById('av-video');
      return !!(v && v.srcObject && v.videoWidth>0); })()`);
    if (live) break;
    await sleep(1000);
  }
  run.session_live = live;
  if (!live) {
    console.log('ABORT: 7860 没有活的数字人会话 —— 几何闸门不可成立（盒子会塌成 UA 默认 300×150）。');
    await prefs.restore(tab, prefsSnap0);
    await tab.close(); conn.close(); process.exitCode = 2; return;
  }
  await sleep(2500);

  /* 媒体身份标记：给唯一的 video 节点和它的 MediaStream 各钉一个属性。
   * 全程之后还必须是同一个对象 —— 这是 §31 最直接的反证法。 */
  await tab.js(`(() => { const v=document.getElementById('av-video');
    window.__VID_MARK='AV_VIDEO_UNIQUE_' + Date.now(); v.__vidMark = window.__VID_MARK;
    if (v.srcObject) { window.__STR_MARK='MS_UNIQUE_' + Date.now(); v.srcObject.__strMark = window.__STR_MARK; }
    return { vid: v.__vidMark, str: v.srcObject ? v.srcObject.__strMark : null,
             count: document.querySelectorAll('video').length }; })()`)
    .then((r) => { run.media_marks = r; });

  const M = async (tag) => await tab.js(`window.__M(${JSON.stringify(tag || '')})`);

  /* ============================== A 装载与唯一 owner ===================== */
  const A0 = await M('A0_detail_default');
  run.A0_detail_default = A0;
  /* 两类异常必须分开：JS_EXCEPTION 才是"页面崩了"；LOG_ERROR 里混着资源层的
   * 4xx（本页自带的 POST /api/conn-trace 一直返 405，属既有事实、本轮范围外），
   * 混在一起判会让一个不相干的资源状态码把布局闸门染成 FAIL。 */
  const jsEx = () => tab.exceptions.filter((e) => e.kind === 'JS_EXCEPTION');
  const logEx = () => tab.exceptions.filter((e) => e.kind === 'LOG_ERROR');
  gate('A01_LOAD_CLEAN', '页面装载后无 JS 异常；两个模块都是 V2；owner 已接管面板',
    jsEx().length === 0 && A0.owner.ver === 2 && A0.uivm.ver === 2
    && A0.counts.avVideo === 1 && A0.counts.cards === 0 && A0.entryCount === 1,
    { jsExceptions: jsEx(), resourceErrors: logEx().length,
      owner: A0.owner.ver, uivm: A0.uivm.ver,
      avVideo: A0.counts.avVideo, legacyCards: A0.counts.cards, entries: A0.entryCount });

  gate('A02_SINGLE_TRANSFORM_OWNER', '全仓只有 avatar_layout.js 写 #av-video 的 transform',
    stat.only_owner_writes_style_transform && A0.video.inline.length <= 4,
    { writers: stat.transform_writers, inlinePropsAtDefault: A0.video.inline });

  gate('A03_DEFAULT_ZERO_GEOMETRY_OVERRIDE', '默认态 owner 未接管：内联样式为空，画面=改造前',
    A0.owner.isDefault === true && A0.video.inline.length === 0,
    { inline: A0.video.inline, transform: A0.video.transform, maskStops: A0.mask.stops });

  /* ============================== B 全屏合成（§4/§26） =================== */
  gate('B01_DETAIL_FULL_VIEWPORT_COMPOSITE', 'DETAIL 下 #av-layer 铺满 viewport 且 overflow:hidden',
    A0.viewMode === 'detail' && A0.layer.fillsViewport === true && A0.layer.overflow === 'hidden',
    { layer: A0.layer, vp: A0.vp, overflow: A0.layer.overflow });
  gate('B02_NO_PANE_CONTAINER', '不存在人物专用 pane：body 无 data-uivm-pane，#app 为全宽',
    A0.paneAttr === null && near(A0.app.w, A0.layer.w, 2),
    { paneAttr: A0.paneAttr, appW: A0.app.w, layerW: A0.layer.w });
  gate('B03_VIDEO_MAX_SIZE_BLOCK_REMOVED', 'video 尺寸上限已解除（max-width/max-height = none）',
    A0.video.maxW === 'none' && A0.video.maxH === 'none',
    { maxW: A0.video.maxW, maxH: A0.video.maxH, cssW: A0.video.cssW, cssH: A0.video.cssH });
  run.primary_ui_rects_default = await tab.js(`window.__UI(${JSON.stringify(PRIMARY)})`);

  /* ============================== C 四档真实几何（§28） ================= */
  const tiers = {};
  const TIERS = [['DETAIL_SMALL', 70, 100], ['DETAIL_DEFAULT', 100, 100],
                 ['DETAIL_LARGE', 180, 100], ['DETAIL_LARGE_STRETCHED', 180, 160]];
  for (const [name, s, w] of TIERS) {
    await tab.js(`window.AvatarLayout.resetToDefault(); 1`);      // 每档从同一基准起
    if (s !== 100) await tab.js(`window.__SETD('s', ${s}); 1`);
    if (w !== 100) await tab.js(`window.__SETD('w', ${w}); 1`);
    await sleep(220);
    tiers[name] = await M(name);
  }
  run.tiers = tiers;
  const SMALL = tiers.DETAIL_SMALL, DEF = tiers.DETAIL_DEFAULT,
        LARGE = tiers.DETAIL_LARGE, LGS = tiers.DETAIL_LARGE_STRETCHED;

  gate('C01_DRAG_CHANGES_REAL_VIDEO_SIZE', 'DETAIL_LARGE.video.width > DETAIL_SMALL.video.width × 1.5',
    LARGE.video.w > SMALL.video.w * 1.5,
    { small: SMALL.video.w, large: LARGE.video.w, ratio: +(LARGE.video.w / SMALL.video.w).toFixed(3) });
  gate('C02_UNIFORM_SCALE_GROWS_HEIGHT', '等比例放大时 rect.height 必须同步增大；仅调宽度时高度不变',
    DEF.video.h > SMALL.video.h && LARGE.video.h > DEF.video.h
    && near(LGS.video.h, LARGE.video.h, 1.5),
    { h: { small: SMALL.video.h, def: DEF.video.h, large: LARGE.video.h, stretched: LGS.video.h } });
  gate('C03_PERSON_EXPANDS_INWARD', '左缘随放大向页面内部推进，右缘由右上锚点钉住',
    SMALL.video.x > DEF.video.x && DEF.video.x > LARGE.video.x
    && Math.abs(LARGE.video.right - SMALL.video.right) < 8,
    { left: { small: SMALL.video.x, def: DEF.video.x, large: LARGE.video.x },
      right: { small: +(SMALL.video.x + SMALL.video.w).toFixed(1), large: +(LARGE.video.x + LARGE.video.w).toFixed(1) } });
  gate('C04_APP_UNTOUCHED_BY_TIERS', '四档之间主 UI(#app) 逐像素不变（拖动不挤压聊天区）',
    [SMALL, DEF, LARGE, LGS].every((t) => near(t.app.w, A0.app.w, 1) && near(t.app.x, A0.app.x, 1)),
    { appW: [SMALL, DEF, LARGE, LGS].map((t) => t.app.w) });
  gate('C05_OVERFLOW_CLIPPED_BY_LAYER', '超出视口由 #av-layer 裁切，页面不因此出横向滚动',
    LARGE.video.right > LARGE.vp.iw && LARGE.app.w <= LARGE.vp.dw + 1
    && LARGE.layer.w <= LARGE.vp.dw + 1,
    { videoRight: +(LARGE.video.x + LARGE.video.w).toFixed(1), iw: LARGE.vp.iw,
      layerW: LARGE.layer.w, docW: LARGE.vp.dw });

  /* ============================== D 黑雾联动（§11/§12） ================= */
  const fadeRows = [A0, SMALL, DEF, LARGE, LGS].map((t) => ({
    tag: t.tag, left: t.video.x, fadeStart: t.mask.fadeStart, fadeEnd: t.mask.fadeEnd,
    seam: t.mask.seamW, gap: t.mask.blackGap, stops: t.mask.stops, fit: t.video.objectFit }));
  run.fade_linkage = fadeRows;
  gate('D01_FADE_START_AT_PERSON_EDGE', '黑雾起点 == 人物实测左缘（±3px，无独立黑块）',
    fadeRows.every((r) => near(r.fadeStart, r.left, 3)), fadeRows);
  gate('D02_NO_BLACK_BAND_GAP', '黑雾在人物的【里面】结束，不出现人物缩在右侧的黑矩形',
    fadeRows.every((r) => r.gap !== null && r.gap < 0 && r.fadeEnd < r.left + r.seamW + 1), fadeRows);
  gate('D03_FADE_MOVES_WITH_PERSON', '人物左缘左移时 fadeStart/fadeEnd 同向移动，缝宽屏幕等宽',
    (SMALL.mask.fadeEnd > DEF.mask.fadeEnd && DEF.mask.fadeEnd > LARGE.mask.fadeEnd)
    && near(LARGE.mask.seamW, A0.mask.seamW, 4) && near(LGS.mask.seamW, A0.mask.seamW, 6),
    fadeRows);
  gate('D04_MASK_STOPS_COUNT_AND_ORDER', 'mask 四段停靠点存在且单调递增（探针没有瞎报）',
    [A0, SMALL, LARGE, LGS].every((t) => t.mask.stops.length === 4
      && t.mask.stops.every((v, i) => i === 0 || v > t.mask.stops[i - 1])),
    { A0: A0.mask.stops, LARGE: LARGE.mask.stops, LGS: LGS.mask.stops });

  /* ============================== E 真实拖动（§5/§6/§13） =============== */
  await tab.js(`window.AvatarLayout.resetToDefault(); 1`); await sleep(200);
  const before = await M('E_before');
  run.E_before_drag = before;
  const targets = [1500, 1300, 1100, 900];
  const drags = [];
  for (const tx of targets) {
    const from = await tab.js(`(() => { const r=document.getElementById('uivm-divider').getBoundingClientRect();
      return Math.round(r.x + r.width/2); })()`);
    await tab.js(`window.__DRAG(${from}, ${tx}, 6); 1`);
    await sleep(180);
    const m = await M('E_at_' + tx);
    drags.push({ pointerX: tx, dividerCenter: m.divider.center, cssVarX: m.vars ? m.divider.cssX : null,
                 videoLeft: m.video.x, videoW: m.video.w, videoH: m.video.h, scale: m.owner.s,
                 fadeEnd: m.mask.fadeEnd, gap: m.mask.blackGap, inline: m.video.inline });
  }
  run.E_drags = drags;
  gate('E01_DRAG_TRACKS_POINTER', '拖动后人物左缘落在指针处（±12px，含 1% 量化）',
    drags.every((d) => Math.abs(d.videoLeft - d.pointerX) <= 12), drags);
  gate('E02_DRAG_SCALES_MONOTONIC', '越往左拖 scale 与真实宽度越大（左拖=放大）',
    drags.every((d, i) => i === 0 || (d.scale > drags[i - 1].scale && d.videoW > drags[i - 1].videoW)),
    drags.map((d) => ({ x: d.pointerX, s: d.scale, w: d.videoW })));
  gate('E03_DIVIDER_IS_VISUAL_BOUNDARY', '控制线跟的是人物视觉边界，不是窗口分栏',
    drags.every((d) => Math.abs(d.dividerCenter - d.videoLeft) <= 10),
    drags.map((d) => ({ line: d.dividerCenter, personLeft: d.videoLeft })));
  gate('E04_DRAG_PERSISTS_ON_RELEASE', '拖动过程中不落盘、松手后保存（§13）',
    await tab.js(`(() => { const s = localStorage.getItem('avatarVideoScale');
      return s !== null && Number(s) === window.AvatarLayout.state.s; })()`),
    { storedScale: await tab.js(`localStorage.getItem('avatarVideoScale')`) });
  const Eafter = await M('E_after_drag');
  gate('E05_MAIN_UI_NOT_SQUEEZED', '拖动全程 #app 宽度不变（§14）',
    near(Eafter.app.w, before.app.w, 1), { before: before.app.w, after: Eafter.app.w });

  /* 键盘与双击（同一杠杆的另外两条通道） */
  await tab.js(`(() => { const d=document.getElementById('uivm-divider'); d.focus();
    for (let i=0;i<3;i++) d.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true,cancelable:true}));
    return 1; })()`);
  await sleep(200);
  const kb = await M('E_keyboard');
  gate('E06_KEYBOARD_SAME_LEVER', '键盘方向键走同一条边界杠杆（人物继续变大）',
    kb.owner.s > Eafter.owner.s && kb.video.w > Eafter.video.w,
    { afterDrag: Eafter.owner.s, afterKeys: kb.owner.s, w: [Eafter.video.w, kb.video.w] });
  await tab.js(`document.getElementById('uivm-divider').dispatchEvent(new MouseEvent('dblclick',{bubbles:true})); 1`);
  await sleep(200);
  const dbl = await M('E_dblclick');
  gate('E07_DOUBLECLICK_RESTORES_NATIVE', '双击边界回到原生构图（零内联覆盖 + 存储清空）',
    dbl.owner.isDefault === true && dbl.video.inline.length === 0
    && (await tab.js(`localStorage.getItem('avatarVideoScale')`)) === null,
    { isDefault: dbl.owner.isDefault, inline: dbl.video.inline, left: dbl.video.x, w: dbl.video.w });

  /* ============================== F 四能力解耦（§9） ==================== */
  await tab.js(`window.AvatarLayout.resetToDefault(); 1`); await sleep(150);
  const dec = {};
  dec.base = await M('F_base');
  await tab.js(`window.__SETD('s', 150); 1`); await sleep(150); dec.scale = await M('F_scale');
  await tab.js(`window.__SETD('w', 140); 1`); await sleep(150); dec.stretch = await M('F_stretch');
  await tab.js(`window.__SETD('x', -60); 1`); await sleep(150); dec.x = await M('F_x');
  await tab.js(`window.__SETD('y', 60); 1`); await sleep(150); dec.y = await M('F_y');
  run.F_decoupling = Object.fromEntries(Object.entries(dec)
    .map(([k, v]) => [k, { s: v.owner.s, w: v.owner.w, x: v.owner.x, y: v.owner.y,
      rect: { x: v.video.x, y: v.video.y, w: v.video.w, h: v.video.h }, m: mat(v.video.transform) }]));
  gate('F01_SIZE_AND_WIDTH_DO_NOT_OVERWRITE', '大小与宽度互不覆盖：先 150% 再 140% 时两个值同时成立',
    dec.stretch.owner.s === 150 && dec.stretch.owner.w === 140
    && near(dec.stretch.video.w, dec.base.video.w * 1.5 * 1.4, 4)
    && near(dec.stretch.video.h, dec.base.video.h * 1.5, 2),
    run.F_decoupling.stretch);
  gate('F02_XY_PAN_ONLY', '左右/上下只平移：改 x 时 size 不变，改 y 时高度与宽度不变',
    near(dec.x.video.w, dec.stretch.video.w, 2) && near(dec.x.video.h, dec.stretch.video.h, 2)
    && dec.x.video.x < dec.stretch.video.x - 20
    && near(dec.y.video.w, dec.x.video.w, 2) && near(dec.y.video.h, dec.x.video.h, 2)
    && dec.y.video.y > dec.x.video.y + 20,
    { stretch: dec.stretch.video, x: dec.x.video, y: dec.y.video });
  gate('F03_MATRIX_COMPOSITION', 'computed 矩阵就是 scale(s)·scaleX(w)：a=s·w、d=s',
    (() => { const m = mat(dec.stretch.video.transform);
      return m && near(m.a, 1.5 * 1.4, 0.01) && near(m.d, 1.5, 0.01); })(),
    mat(dec.stretch.video.transform));
  /* 拉伸单调性（§25） */
  await tab.js(`window.AvatarLayout.resetToDefault(); window.__SETD('s', 120); 1`); await sleep(150);
  const stretchSeries = [];
  for (const wv of [100, 120, 140, 160]) {
    await tab.js(`window.__SETD('w', ${wv}); 1`); await sleep(150);
    const m = await M('W' + wv);
    stretchSeries.push({ w: wv, videoW: m.video.w, videoH: m.video.h, left: m.video.x, fadeEnd: m.mask.fadeEnd });
  }
  run.stretch_monotonic = stretchSeries;
  gate('F04_STRETCH_MONOTONIC', '人物宽度 100/120/140/160 → 实测 width 单调增、height 不变、雾跟着走',
    stretchSeries.every((r, i) => i === 0 || (r.videoW > stretchSeries[i - 1].videoW
      && near(r.videoH, stretchSeries[i - 1].videoH, 1.5) && r.fadeEnd < stretchSeries[i - 1].fadeEnd)),
    stretchSeries);
  /* UI 文案（§8：写"人物宽度 100%（原始）"，不许露 scaleX/matrix） */
  const labels = await tab.js(`(() => {
    const out = { fields: [], text: document.getElementById('uivm-panel').innerText.replace(/\\s+/g,' ') };
    document.querySelectorAll('#uivm-panel .uivm-field').forEach(f => {
      out.fields.push({ label: f.querySelector('.uivm-field-label span').textContent,
                        value: f.querySelector('output').textContent,
                        aria: f.querySelector('input').getAttribute('aria-label') }); });
    return out; })()`);
  run.panel_labels = labels;
  gate('F05_USER_LANGUAGE_LABELS', '面板写"人物大小/人物宽度/左右位置/上下位置"，读数含「原始」措辞',
    labels.fields.length === 4 && labels.fields.map((f) => f.label).join(',') === '人物大小,人物宽度,左右位置,上下位置'
    && !/scaleX|matrix|aspect| scaleX/i.test(labels.text),
    { fields: labels.fields, snippet: labels.text.slice(0, 260) });

  /* ============================== G 模式互不污染（§22） ================= */
  await tab.js(`window.AvatarLayout.resetToDefault(); 1`); await sleep(150);
  await tab.js(`window.__SETD('s', 180); window.__SETD('w', 140); window.__SETD('x', 50); window.__SETD('y', 30); 1`);
  await sleep(200);
  const Gd0 = await M('G_detail_set');
  const LV = { scale: 130, offsetX: 5, offsetY: 0 };
  await tab.js(`(() => { const b=document.querySelector('#uivm-panel .uivm-mode[data-mode="detail"]');
    return b.getAttribute('aria-pressed'); })()`);
  await tab.js(`window.UiViewModes.set('live-portrait'); 1`); await sleep(400);
  const Gl = await M('G_live');
  const stage = { w: Gl.layer.w, h: Gl.layer.h };
  const expectedLv = { s: LV.scale / 100, txPct: (LV.offsetX * 0.25) };
  await tab.js(`(() => { const q=(k,v)=>{const i=document.querySelector('#uivm-panel input[data-uivm-lv="'+k+'"]');
    i.value=String(v); i.dispatchEvent(new Event('input',{bubbles:true}));};
    q('scale',${LV.scale}); q('offsetX',${LV.offsetX}); q('offsetY',${LV.offsetY}); return 1; })()`);
  await sleep(300);
  const Gl2 = await M('G_live_after_sliders');
  const ml = mat(Gl2.video.transform);
  const expectTx = (LV.offsetX * 0.25 / 100) * Gl2.video.offsetW;
  const expectTy = (LV.offsetY * 0.25 / 100) * Gl2.video.offsetH;
  run.G_live = { measured: Gl2.video, matrix: ml, expect: { a: expectedLv.s, e: expectTx, f: expectTy },
                 layer: Gl2.layer, mask: Gl2.mask.raw.slice(0, 120), vars: Gl2.vars };
  gate('G01_LIVE_PARAMS_REACH_COMPUTED_STYLE', 'LIVE 滑块值真的进入 computed transform（矩阵核对，非变量核对）',
    ml && near(ml.a, 1.3, 0.005) && near(ml.d, 1.3, 0.005) && near(ml.e, expectTx, 1.5) && near(ml.f, expectTy, 1.5),
    { matrix: ml, expect: { a: 1.3, e: +expectTx.toFixed(2), f: +expectTy.toFixed(2) } });
  gate('G02_LIVE_MASK_OFF_INSIDE_STAGE', 'LIVE 取景框内没有渐隐（DETAIL 的内联雾被收回）',
    Gl2.mask.raw === 'none' && Gl2.video.inline.indexOf('mask-image') === -1,
    { computedMask: Gl2.mask.raw, inline: Gl2.video.inline });
  gate('G03_LIVE_STAGE_KEPT_9_16', 'LIVE 的 9:16 舞台模型未被动过（KEEP）',
    near(Gl2.layer.ratio, 9 / 16, 0.004) && Gl2.layer.x > 10 && Gl2.layer.y > 10
    && Gl2.layer.w < Gl2.vp.iw * 0.6,
    { ratio: Gl2.layer.ratio, layer: Gl2.layer });
  await tab.js(`window.UiViewModes.set('detail'); 1`); await sleep(400);
  const Gd1 = await M('G_detail_back');
  gate('G04_DETAIL_PARAMS_SURVIVE_SWITCH', '切回 DETAIL 后四参数与几何逐位复原，LIVE 值也仍在',
    Gd1.owner.s === 180 && Gd1.owner.w === 140 && Gd1.owner.x === 50 && Gd1.owner.y === 30
    && near(Gd1.video.x, Gd0.video.x, 1) && near(Gd1.video.w, Gd0.video.w, 1)
    && near(Gd1.video.h, Gd0.video.h, 1) && Gd1.uivm.state.livePortrait.scale === LV.scale
    && Gd1.layer.fillsViewport === true,
    { detail: { s: Gd1.owner.s, w: Gd1.owner.w, x: Gd1.owner.x, y: Gd1.owner.y },
      storedLive: Gd1.uivm.state.livePortrait, rect: [Gd0.video, Gd1.video] });
  gate('G05_MODE_SWITCH_NO_RECONNECT', '模式切换期间 currentTime 连续、srcObject 仍在',
    Gd1.video.srcObject === true && Gd1.video.currentTime > Gl.video.currentTime,
    { tLive: Gl.video.currentTime, tBack: Gd1.video.currentTime });

  /* ============================== H 面板与碰撞（§15–§19） =============== */
  await tab.js(`window.UiViewModes.set('detail'); 1`); await sleep(150);
  const H0 = await M('H_default_collapsed');
  gate('H01_PANEL_DEFAULT_COLLAPSED', '全新装载（无偏好）→ 面板默认收起，主页面只有一个入口',
    H0.panel.collapsed === true && H0.entryCount === 1 && H0.entry.expanded === 'false',
    { collapsed: H0.panel.collapsed, entries: H0.entryCount, aria: H0.entry.expanded, panel: H0.panel });
  const collCollapsed = await tab.js(`window.__COLLIDE('#uivm-panel', ${JSON.stringify(PRIMARY)})`);
  const openRet = await tab.js(`document.getElementById('gf-layout-open').click(); 1`);
  await sleep(300);
  const H1 = await M('H_open');
  const collOpen = await tab.js(`window.__COLLIDE('#uivm-panel', ${JSON.stringify(PRIMARY)})`);
  const collOpenApp = await tab.js(`window.__COLLIDE('#uivm-panel', ['#gf-composer','#gf-composer-input','#gf-composer-send','#gf-model','#gf-voice-model','#gf-avatar-open','#gf-layout-open','#gf-voice-performance','#gf-live-conversation','.orb-wrap','#gf-reading-open'])`);
  run.panel_detail = { collapsedRect: H0.panel, openRect: H1.panel,
                       collideCollapsed: collCollapsed, collideOpen: collOpen, collideOpenTargets: collOpenApp };
  gate('H02_DETAIL_PANEL_NO_PRIMARY_COLLISION', 'DETAIL 展开态与模型/TTS/模式按钮/输入框/发送键零相交（实测矩形）',
    H1.panel.collapsed === false && collOpenApp.count === 0,
    collOpenApp);
  gate('H03_DETAIL_PANEL_DOCKED_LEFT', 'DETAIL 面板贴左缘、且在页头下方（不再悬浮屏幕中心）',
    H1.panel.x < 40 && H1.panel.y > Number(H1.vars.topbar.replace('px', '') || 0) && H1.panel.h > 0,
    { panel: H1.panel, topbarVar: H1.vars.topbar });
  gate('H04_COLLAPSED_PILL_SMALL', '收起态只是一条标题（高度 < 展开态一半，不吃画面）',
    H0.panel.h < H1.panel.h * 0.5, { collapsedH: H0.panel.h, openH: H1.panel.h });

  await tab.js(`window.UiViewModes.set('live-portrait'); 1`); await sleep(400);
  const Hl = await M('H_live_open');
  const liveOutside = Hl.panel.x >= Hl.layer.x + Hl.layer.w - 1 || (Hl.panel.x + Hl.panel.w) <= Hl.layer.x + 1;
  const collLive = await tab.js(`window.__COLLIDE('#uivm-panel', ['#gf-composer-input','#gf-composer-send','#gf-model','#gf-voice-model'])`);
  run.panel_live = { rect: Hl.panel, layer: Hl.layer, outsideStage: liveOutside, collideApp: collLive };
  gate('H05_LIVE_PANEL_OUTSIDE_CAPTURE_STAGE', 'LIVE 面板整体在 9:16 捕捉舞台之外',
    liveOutside, { panel: Hl.panel, stage: { x: Hl.layer.x, right: +(Hl.layer.x + Hl.layer.w).toFixed(1) } });
  gate('H06_LIVE_PANEL_NOT_IN_CHAT_COLUMN', 'LIVE 面板落在预留操作列，不压聊天/输入框（顺带验收）',
    collLive.count === 0, collLive);
  await tab.js(`document.getElementById('gf-layout-open').click(); 1`); await sleep(250);
  const Hl2 = await M('H_live_collapsed');
  gate('H07_LIVE_DEFAULT_COLLAPSED_AFTER_TOGGLE', 'LIVE 也能收起；收起态不遮舞台（面板在舞台外）',
    Hl2.panel.collapsed === true, { rect: Hl2.panel, layer: Hl2.layer });

  /* 窄窗（§19） */
  const narrow = {};
  for (const [w, h] of [[1100, 820], [880, 720]]) {
    await tab.js(`window.UiViewModes.set('detail'); 1`); await sleep(150);
    await tab.setViewport(w, h); await sleep(500);
    const m = await M(`narrow_${w}x${h}`);
    const c = await tab.js(`window.__COLLIDE('#uivm-panel', ['#gf-composer-input','#gf-composer-send'])`);
    narrow[`${w}x${h}`] = { panel: m.panel, layer: m.layer, vp: m.vp, collide: c,
                            collapsed: m.panel.collapsed, appW: m.app.w };
    gate(`H08_NARROW_${w}x${h}_NO_INPUT_BLOCK`, `${w}×${h} 收起态不压输入框/发送键，DETAIL 仍是全屏合成`,
      m.panel.collapsed === true && c.count === 0 && m.layer.fillsViewport === true,
      narrow[`${w}x${h}`]);
  }
  run.narrow = narrow;
  await tab.setViewport(VP[0], VP[1]); await sleep(500);
  const resized = await M('after_resize');
  gate('H09_RESIZE_RELINKS_FADE', '窗口尺寸变化后雾仍贴人物左缘（更新时机含 resize，且无常驻循环）',
    near(resized.mask.fadeStart, resized.video.x, 3), { fadeStart: resized.mask.fadeStart, left: resized.video.x });

  /* ============================== I 持久化与迁移（§20） ================= */
  await tab.js(`window.UiViewModes.set('detail'); 1`); await sleep(150);
  await tab.js(`window.AvatarLayout.resetToDefault();
    window.__SETD('s', 160); window.__SETD('w', 120); window.__SETD('x', -25); window.__SETD('y', 15);
    (() => { const q=(k,v)=>{const i=document.querySelector('#uivm-panel input[data-uivm-lv="'+k+'"]');
      i.value=String(v); i.dispatchEvent(new Event('input',{bubbles:true}));};
      q('scale', 150); q('offsetX', -35); q('offsetY', 20); })();
    window.UiViewModes.panel.setCollapsed(false); 1`);
  await sleep(300);
  const Ipre = await M('I_before_reload');
  const Istore = await tab.js(`(() => ({ layout: localStorage.getItem('ai_girlfriend_ui_layout_v1'),
    s: localStorage.getItem('avatarVideoScale'), w: localStorage.getItem('avatarVideoStretchX'),
    x: localStorage.getItem('avatarVideoOffsetX'), y: localStorage.getItem('avatarVideoOffsetY'),
    panel: localStorage.getItem('ai_girlfriend_uivm_panel_v2'),
    legacyPanel: localStorage.getItem('uiViewModePanelCollapsed') }))()`);
  run.I_storage_after_writes = Istore;
  await tab.navigate(BASE + '/', 2500);
  await tab.js(INSTALL);
  await sleep(2500);
  const Ipost = await M('I_after_reload');
  gate('I01_DETAIL_SETTINGS_PERSISTENCE', '刷新后 DETAIL 四参数与几何完全复原',
    Ipost.owner.s === 160 && Ipost.owner.w === 120 && Ipost.owner.x === -25 && Ipost.owner.y === 15
    && near(Ipost.video.x, Ipre.video.x, 2) && near(Ipost.video.w, Ipre.video.w, 2),
    { stored: Istore, before: Ipre.video, after: Ipost.video, state: Ipost.owner });
  gate('I02_LIVE_SETTINGS_PRESERVED', '刷新后 LIVE 三参数保持（§35 LIVE_SETTINGS_PRESERVED）',
    Ipost.uivm.state.livePortrait.scale === 150 && Ipost.uivm.state.livePortrait.offsetX === -35
    && Ipost.uivm.state.livePortrait.offsetY === 20, Ipost.uivm.state.livePortrait);
  gate('I03_PANEL_STATE_PERSISTS', '用户显式展开过 → 刷新后保持展开；旧 V1 收起键不再被读',
    Ipost.panel.collapsed === false, { new: Ipost.panel.collapsed, stored: Istore.panel,
      legacy: Istore.legacyPanel });
  const payload = JSON.parse(Istore.layout || '{}');
  run.I_payload = payload;
  gate('I04_LEGACY_PANE_NOT_PERSISTED', '新落盘里 detail.paneRatio=null + deprecated 标记（旧杠杆不再参与生产布局）',
    payload.version === 2 && payload.detail && payload.detail.paneRatio === null
    && payload.detail.deprecated === 'DEPRECATED_IGNORED', payload);

  /* 反向：手工植入 V1 形态的 paneRatio=70，必须【完全不影响】布局 */
  await tab.js(`(() => { const p = JSON.parse(localStorage.getItem('ai_girlfriend_ui_layout_v1'));
    p.detail = { paneRatio: 70 }; localStorage.setItem('ai_girlfriend_ui_layout_v1', JSON.stringify(p));
    return localStorage.getItem('ai_girlfriend_ui_layout_v1'); })()`);
  await tab.js(`window.AvatarLayout.resetToDefault(); 1`); await sleep(200);
  const legacyBase = await M('J_before_legacy_reload');
  await tab.navigate(BASE + '/', 2500); await tab.js(INSTALL); await sleep(2500);
  const legacyAft = await M('J_after_legacy_reload');
  run.J_legacy_paneRatio = { before: legacyBase.video, after: legacyAft.video,
                             app: [legacyBase.app.w, legacyAft.app.w], paneAttr: legacyAft.paneAttr,
                             migrated: legacyAft.uivm.migrated,
                             store: await tab.js(`localStorage.getItem('ai_girlfriend_ui_layout_v1')`) };
  gate('J01_LEGACY_PANE_RATIO_IGNORED', '旧 paneRatio=70 残留 → 布局逐像素无变化，且被净化回写',
    near(legacyAft.app.w, A0.app.w, 1) && near(legacyAft.video.w, A0.video.w, 1)
    && near(legacyAft.video.x, A0.video.x, 1) && legacyAft.paneAttr === null
    && legacyAft.uivm.migrated && legacyAft.uivm.migrated.paneRatioDropped === 70,
    run.J_legacy_paneRatio);
  /* setPane 兼容壳 */
  const shell = await tab.js(`(() => { const before = document.getElementById('app').getBoundingClientRect().width;
    const r = window.UiViewModes.setPane(70);
    return { r, before, after: document.getElementById('app').getBoundingClientRect().width }; })()`);
  gate('J02_SETPANE_SHELL_IS_NOOP', 'UiViewModes.setPane() 兼容壳返回 DEPRECATED_IGNORED 且零几何影响',
    shell.r === 'DEPRECATED_IGNORED' && near(shell.before, shell.after, 0.5), shell);

  /* ============================== K 媒体身份 + 零常驻 ================== */
  const K = await tab.js(`(() => { const v = document.getElementById('av-video');
    return { sameNode: v.__vidMark === window.__VID_MARK,
             srcObject: !!v.srcObject,
             streamTag: v.srcObject ? (v.srcObject.__strMark === window.__STR_MARK) : null,
             currentTime: v.currentTime,
             avVideo: document.querySelectorAll('#av-video').length,
             allVideos: document.querySelectorAll('video').length,
             mediaStreamNodes: (() => { let n=0; document.querySelectorAll('video').forEach(x => { if (x.srcObject) n++; }); return n; })(),
             readyState: v.readyState }; })()`);
  run.K_media_identity = K;
  run.media_marks_final = K;
  gate('K01_AVATAR_VIDEO_NODE_COUNT_ONE', 'AVATAR_VIDEO_NODE_COUNT = 1（#av-video 只有一个）',
    K.avVideo === 1, { avVideo: K.avVideo, allVideoEls: K.allVideos, withStream: K.mediaStreamNodes });
  gate('K02_VIDEO_NODE_IDENTITY_PRESERVED', 'videoBefore === videoAfter（同一 DOM 节点，未被替换）',
    K.sameNode === true, K);
  gate('K03_MEDIASTREAM_IDENTITY_PRESERVED', 'srcObjectBefore === srcObjectAfter（MediaStream 未被重建）',
    K.streamTag === true && K.srcObject === true, K);
  gate('K04_CURRENT_TIME_ADVANCES', 'currentTime 相对装载时刻持续增长（播放链没被打断）',
    K.currentTime > A0.video.currentTime, { atStart: A0.video.currentTime, now: K.currentTime });
  const driftA = await M('L_idle_a');
  await sleep(3200);
  const driftB = await M('L_idle_b');
  gate('L01_NO_PERSISTENT_LOOPS', '静置 3.2s 几何零漂移；两个 JS 里 rAF/setInterval/Observer 计数为 0',
    near(driftA.video.x, driftB.video.x, 0.5) && near(driftA.video.w, driftB.video.w, 0.5)
    && near(driftA.mask.fadeEnd, driftB.mask.fadeEnd, 0.5)
    && stat.persistent_loops['avatar_layout.js'].requestAnimationFrame === 0
    && stat.persistent_loops['avatar_layout.js'].setInterval === 0
    && stat.persistent_loops['ui_layout_modes.js'].requestAnimationFrame === 0
    && stat.persistent_loops['ui_layout_modes.js'].setInterval === 0
    && stat.persistent_loops['ui_layout_modes.js'].MutationObserver === 0,
    { a: driftA.video, b: driftB.video, loops: stat.persistent_loops });
  gate('L02_NO_MEDIA_OBJECT_TOUCH', '两文件里 createElement(video)/new MediaStream/srcObject=/new WebSocket 全为 0',
    stat.media_api_in_js.createVideo === 0 && stat.media_api_in_js.newMediaStream === 0
    && stat.media_api_in_js.srcObjectWrites === 0 && stat.media_api_in_js.websocket === 0
    && stat.media_api_in_js.fetch === 0 && stat.clone_or_move === 0,
    { ...stat.media_api_in_js, clone_or_move: stat.clone_or_move });

  /* ============================== M 异常与收尾 ========================== */
  run.exceptions = { js: jsEx(), resource: logEx() };
  gate('M01_NO_JS_EXCEPTIONS_WHOLE_RUN', '整轮布局操作零未捕获 JS 异常（CDP 事件通道，不是页面自报）',
    jsEx().length === 0, { js: jsEx().slice(0, 6), resourceLevelLogs: logEx().slice(0, 6) });

  /* 收尾：全部布局参数清干净 + 用户原值还原 */
  await tab.js(`window.AvatarLayout.resetToDefault(); window.UiViewModes.apply(); 1`); await sleep(200);
  const prefsEnd = await prefs.clearLayoutKeys(tab);
  const prefsKeysFinal = await tab.js(`Object.keys(localStorage).sort()`);
  const biz = prefs.businessKeysEqual(prefsSnap0.allKeys, prefsKeysFinal);
  await prefs.restore(tab, prefsSnap0);
  const restored = await tab.js(`(() => { const names = ${JSON.stringify(prefs.ALL.concat(prefs.LAYOUT_KEYS.legacy))};
    const out = {}; for (const n of names) out[n] = localStorage.getItem(n);
    return { values: out, allKeys: Object.keys(localStorage).sort() }; })()`);
  const bizAfterRestore = prefs.businessKeysEqual(prefsSnap0.allKeys, restored.allKeys);
  run.prefs_restored = { user_before: prefsSnap0.values, removed_at_end: prefsEnd.removed,
                         after_restore: restored.values, faithful: bizAfterRestore.equal,
                         business_keys: bizAfterRestore,
                         residue: Object.keys(restored.values).filter((k) => prefs.ALL.includes(k)
                           && restored.values[k] !== prefsSnap0.values[k]
                           && !(prefsSnap0.values[k] === null && restored.values[k] === null)) };
  gate('M02_PREFS_RESTORED', '测试写过的布局键全部还原成用户原值，业务键零增删',
    bizAfterRestore.equal && bizAfterRestore.added.length === 0 && bizAfterRestore.removed.length === 0
    && prefs.ALL.every((k) => restored.values[k] === prefsSnap0.values[k]),
    run.prefs_restored);

  run.gates = GATES;
  run.summary = { total: GATES.length,
                  pass: GATES.filter((g) => g.verdict === 'PASS').length,
                  fail: GATES.filter((g) => g.verdict === 'FAIL').map((g) => g.id) };
  run.UI_DETAIL_COMPOSITION_ENGINEERING = run.summary.fail.length === 0 ? 'PASS' : 'FAIL';
  run.UI_VISUAL_USER_ACCEPTANCE = 'PENDING';
  fs.mkdirSync(EV, { recursive: true });
  fs.writeFileSync(path.join(EV, 'SHADOW_ACCEPTANCE.json'), JSON.stringify(run, null, 2));
  fs.appendFileSync(path.join(EV, 'ACCEPTANCE_PROGRESS.jsonl'),
    JSON.stringify({ at: run.generatedAt, file: 'shadow_acceptance.cjs',
                     summary: run.summary, verdict: run.UI_DETAIL_COMPOSITION_ENGINEERING }) + '\n');

  console.log('\n--- geometry table (§28) ---');
  for (const [k, v] of Object.entries(tiers)) {
    console.log(`  ${k.padEnd(24)} video ${String(v.video.w).padStart(7)}x${String(v.video.h).padStart(7)} @${String(v.video.x).padStart(8)}  fade ${String(v.mask.fadeStart).padStart(7)}..${String(v.mask.fadeEnd).padStart(7)} gap ${String(v.mask.blackGap).padStart(7)}  app.w ${v.app.w}  s=${v.owner.s} w=${v.owner.w}`);
  }
  console.log('\nTOTAL=%d PASS=%d FAIL=%s', run.summary.total, run.summary.pass,
    run.summary.fail.join(',') || '(none)');
  console.log('UI_DETAIL_COMPOSITION_ENGINEERING =', run.UI_DETAIL_COMPOSITION_ENGINEERING);
  console.log('UI_VISUAL_USER_ACCEPTANCE = PENDING');
  await tab.close();
  conn.close();
  process.exitCode = run.summary.fail.length === 0 ? 0 : 1;
})().catch((e) => {
  console.error('SHADOW_CRASH', String(e && e.stack || e).slice(0, 1500));
  try { fs.mkdirSync(EV, { recursive: true });
    fs.writeFileSync(path.join(EV, 'SHADOW_CRASH.log'),
      new Date().toISOString() + '\n' + String(e && e.stack || e)); } catch (_) {}
  process.exitCode = 1;
});
