/* ============================================================================
 * measure_page.cjs — V2 闸门共用的页面侧探针（唯一一份，测试脚本 require 它）
 *
 * 为什么单独成文件：上一版 smoke_probe 用正则从 shadow_acceptance.cjs 里
 * "抠"这段字符串，正则没匹配上 → 早退 → 但 CDP 的 WebSocket 还开着 →
 * 整个进程挂死 7 分钟，什么都没测到。探针属于被测代码之外的测量工具，
 * 它自己必须可 require、可单测，而不是靠源码扫描。
 *
 * 探针只【读】渲染结果：getBoundingClientRect / getComputedStyle / offset*。
 * 唯一的写操作是 window.__DRAG / __SETD / __SETLV —— 那是"模拟用户操作"，
 * 走产品自己的 pointerdown / input 事件路径，不直接改样式。
 * ==========================================================================*/
'use strict';

const INSTALL = `(() => {
  const A = (r) => ({ x: +r.x.toFixed(1), y: +r.y.toFixed(1), w: +r.width.toFixed(1), h: +r.height.toFixed(1) });

  window.__M = function (tag) {
    const v = document.getElementById('av-video');
    const l = document.getElementById('av-layer');
    const app = document.getElementById('app');
    const d = document.getElementById('uivm-divider');
    const p = document.getElementById('uivm-panel');
    const e = document.getElementById('gf-layout-open');
    if (!v || !l) return null;
    const bcs = getComputedStyle(document.body);
    const rv = v.getBoundingClientRect(), rl = l.getBoundingClientRect();
    const ra = app ? app.getBoundingClientRect() : null;
    const cs = getComputedStyle(v), cls = getComputedStyle(l);
    const inline = [];
    for (let i = 0; i < v.style.length; i++) inline.push(v.style[i]);
    const maskRaw = (cs.maskImage && cs.maskImage !== 'none') ? cs.maskImage
                  : ((cs.webkitMaskImage && cs.webkitMaskImage !== 'none') ? cs.webkitMaskImage : 'none');
    const stops = []; const re = /rgba?\\([^)]*\\)\\s+(-?[\\d.]+)%/g; let m;
    while ((m = re.exec(maskRaw)) !== null) stops.push(+m[1]);
    const fadeStart = stops.length ? rv.x + stops[0] / 100 * rv.width : null;
    const fadeEnd = stops.length ? rv.x + stops[stops.length - 1] / 100 * rv.width : null;
    const o = window.AvatarLayout;
    const rd = d ? d.getBoundingClientRect() : null;
    return {
      tag: tag || null,
      t: Date.now(),
      viewMode: document.body.dataset.viewMode || null,
      paneAttr: document.body.dataset.uivmPane || null,
      boundaryAttr: document.body.dataset.uivmBoundary || null,
      vp: { iw: window.innerWidth, ih: window.innerHeight,
            dw: document.documentElement.clientWidth, dh: document.documentElement.clientHeight },
      video: Object.assign(A(rv), {
        offsetW: v.offsetWidth, offsetH: v.offsetHeight,
        srcW: v.videoWidth, srcH: v.videoHeight,
        maxW: cs.maxWidth, maxH: cs.maxHeight, cssW: cs.width, cssH: cs.height,
        objectFit: cs.objectFit, objectPosition: cs.objectPosition, position: cs.position,
        transform: cs.transform, transformOrigin: cs.transformOrigin,
        inline: inline.sort(), srcObject: !!v.srcObject,
        currentTime: +v.currentTime.toFixed(3),
      }),
      mask: { stops: stops, size: cs.maskSize || cls.maskSize || null, raw: maskRaw.slice(0, 620),
              fadeStart: fadeStart === null ? null : +fadeStart.toFixed(1),
              fadeEnd: fadeEnd === null ? null : +fadeEnd.toFixed(1),
              seamW: (fadeEnd !== null && fadeStart !== null) ? +(fadeEnd - fadeStart).toFixed(1) : null,
              blackGap: fadeEnd === null ? null : +(rv.x - fadeEnd).toFixed(1),
              fadeEndInsidePerson: fadeEnd === null ? null : (fadeEnd < rv.right - 40) },
      layer: Object.assign(A(rl), { overflow: cls.overflow,
             fillsViewport: rl.width >= document.documentElement.clientWidth - 1
               && rl.height >= window.innerHeight - 1 && Math.abs(rl.x) <= 1 && Math.abs(rl.y) <= 1,
             ratio: +(rl.width / rl.height).toFixed(4) }),
      app: ra ? A(ra) : null,
      divider: d ? Object.assign(A(rd), {
             display: getComputedStyle(d).display,
             cssX: bcs.getPropertyValue('--uivm-divider-x').trim(),
             center: +(rd.x + rd.width / 2).toFixed(1) }) : null,
      panel: p ? Object.assign(A(p.getBoundingClientRect()), {
             collapsed: p.dataset.collapsed === '1',
             position: getComputedStyle(p).position,
             collapsedHeadOnly: p.querySelector('.uivm-body')
               ? getComputedStyle(p.querySelector('.uivm-body')).display : null }) : null,
      entry: e ? Object.assign(A(e.getBoundingClientRect()), {
             text: e.textContent, expanded: e.getAttribute('aria-expanded') }) : null,
      entryCount: document.querySelectorAll('#gf-layout-open').length,
      owner: o ? { ver: o.version, disabled: !!o.disabled, s: o.state.s, w: o.state.w,
                   x: o.state.x, y: o.state.y, isDefault: o.isDefault(),
                   geo: o.geometry ? o.geometry() : null } : null,
      uivm: window.UiViewModes ? { ver: window.UiViewModes.version,
              disabled: !!window.UiViewModes.disabled, state: window.UiViewModes.state,
              migrated: window.UiViewModes.migrated || null } : null,
      counts: { avVideo: document.querySelectorAll('#av-video').length,
                videos: document.querySelectorAll('video').length,
                cards: document.querySelectorAll('#av-layout-card').length },
      vars: { lvS: bcs.getPropertyValue('--uivm-lv-s').trim(),
              lvX: bcs.getPropertyValue('--uivm-lv-x').trim(),
              lvY: bcs.getPropertyValue('--uivm-lv-y').trim(),
              topbar: bcs.getPropertyValue('--uivm-topbar-h').trim() },
    };
  };

  window.__UI = function (sels) {
    const out = {};
    for (const s of sels) {
      const el = document.querySelector(s);
      if (!el) { out[s] = null; continue; }
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      if (cs.display === 'none' || cs.visibility === 'hidden' || r.width < 1 || r.height < 1) {
        out[s] = { hidden: true }; continue;
      }
      out[s] = { x: +r.x.toFixed(1), y: +r.y.toFixed(1), w: +r.width.toFixed(1), h: +r.height.toFixed(1),
                 z: cs.zIndex, pos: cs.position };
    }
    return out;
  };

  /** 面板 vs 一批主 UI 的矩形相交（§17：必须实测，不许肉眼）。 */
  window.__COLLIDE = function (sel, sels) {
    const a = document.querySelector(sel);
    if (!a) return { error: 'NO_' + sel };
    const ra = a.getBoundingClientRect();
    const hits = [];
    for (const s of sels) {
      const el = document.querySelector(s);
      if (!el || el === a) continue;
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      if (cs.display === 'none' || r.width < 1 || r.height < 1) continue;
      const w = Math.min(ra.right, r.right) - Math.max(ra.left, r.left);
      const h = Math.min(ra.bottom, r.bottom) - Math.max(ra.top, r.top);
      if (w > 0 && h > 0) hits.push({ sel: s, area: +(w * h).toFixed(1),
                                      panelRect: { x: +ra.x.toFixed(1), y: +ra.y.toFixed(1),
                                                   w: +ra.width.toFixed(1), h: +ra.height.toFixed(1) },
                                      elRect: { x: +r.x.toFixed(1), y: +r.y.toFixed(1),
                                                w: +r.width.toFixed(1), h: +r.height.toFixed(1) } });
    }
    return { panel: { x: +ra.x.toFixed(1), y: +ra.y.toFixed(1), w: +ra.width.toFixed(1),
                      h: +ra.height.toFixed(1) }, count: hits.length, hits: hits };
  };

  /** 真实指针拖动人物边界（走产品自己的 pointerdown/move/up 路径）。 */
  window.__DRAG = function (fromX, toX, steps) {
    const d = document.getElementById('uivm-divider');
    if (!d) return 'NO_DIVIDER';
    const r = d.getBoundingClientRect();
    const start = (fromX === null || fromX === undefined) ? Math.round(r.x + r.width / 2) : Math.round(fromX);
    const mk = (ty, x) => new PointerEvent(ty, { bubbles: true, cancelable: true, view: window,
      pointerId: 7, clientX: Math.round(x), clientY: Math.round(window.innerHeight / 2),
      isPrimary: true, button: 0, buttons: ty === 'pointerup' ? 0 : 1 });
    d.dispatchEvent(mk('pointerdown', start));
    for (let i = 1; i <= steps; i++) d.dispatchEvent(mk('pointermove', start + (toX - start) * i / steps));
    d.dispatchEvent(mk('pointerup', toX));
    return { start: start, end: Math.round(toX), steps: steps };
  };

  window.__SETD = function (key, val) {           // 走面板 DETAIL 滑杆（真实 input 事件）
    const i = document.querySelector('#uivm-panel input[data-uivm-d="' + key + '"]');
    if (!i) return 'NO_INPUT_' + key;
    if (i.disabled) return 'DISABLED_' + key;
    i.value = String(val); i.dispatchEvent(new Event('input', { bubbles: true }));
    return i.value;
  };
  window.__SETLV = function (key, val) {          // 走面板 LIVE 滑杆
    const i = document.querySelector('#uivm-panel input[data-uivm-lv="' + key + '"]');
    if (!i) return 'NO_INPUT_' + key;
    i.value = String(val); i.dispatchEvent(new Event('input', { bubbles: true }));
    return i.value;
  };
  return 1;
})()`;

/* ============================================================================
 * 媒体测试替身（INJECT_DOUBLE）—— 为什么必须有它，以及它【不能】顶替什么
 *
 * 现场事实（本轮实测）：8010 只服务一路会话，用户自己浏览器里的
 * 「小夜 · 实时语音女友」正占着它（netstat: 127.0.0.1:8010 只有 1 条 ESTABLISHED，
 * 属主 pid 39796 ↔ 15004）。探针标签页如果为了"拿到画面"去点【开始会话】，
 * 就可能把用户那一路顶掉/引发重连 —— 那是 §30 明令禁止的干扰，
 * 比"这一轮量不到几何"严重得多。
 *
 * 所以几何闸门改用替身：608×1056 的 canvas.captureStream 喂给【探针自己】的
 * #av-video。608×1056 不是随手挑的 —— 它是 §1 preflight 在真会话上量到的
 * 源尺寸，于是 intrinsic ratio 与生产一致：
 *   height:100% + width:auto ⇒ baseWidth = 1080 × 608/1056 = 621.8px
 * 与 §1 基线里那批 px 数字直接可比（真会话下的 621.8 / 1359.4 / 1981.2）。
 *
 * 它顶替的是【像素源】，不顶替【会话】：探针全程不点开始、不建 PC/WS；
 * connTrace 计数与 8010 established 数一并留证。
 * ==========================================================================*/
const SRC = { w: 608, h: 1056 };
const INJECT_DOUBLE = '(function (SW, SH) {'
  + 'var v = document.getElementById("av-video");'
  + 'if (!v) return { ok: false, why: "NO_VIDEO" };'
  + 'var c = document.createElement("canvas"); c.width = SW; c.height = SH;'
  + 'var g = c.getContext("2d");'
  + 'function draw(t) {'
  + '  g.fillStyle = "#101014"; g.fillRect(0, 0, c.width, c.height);'
  + '  g.fillStyle = "#dfe6e9"; g.fillRect(c.width * 0.34, c.height * 0.10, c.width * 0.32, c.height * 0.80);'
  + '  g.beginPath(); g.arc(c.width * 0.5, c.height * 0.22, c.width * 0.13, 0, 6.29);'
  + '  g.fillStyle = "#f3d9c8"; g.fill();'
  + '  g.fillStyle = "#4ec9b0"; g.fillRect(6, c.height - 30 - (t % 40), 140, 8);'
  + '}'
  + 'draw(0);'
  + 'var st = c.captureStream(25);'
  + 'window.__DOUBLE_STREAM = st; window.__DOUBLE_CANVAS = c;'
  + 'window.__DOUBLE_TIMER = setInterval(function () { try { draw(performance.now()); } catch (e) {} }, 80);'
  + 'v.srcObject = st; v.muted = true;'
  + 'var p = v.play(); if (p && p.catch) p.catch(function () {});'
  + 'return { ok: true, srcW: v.videoWidth, srcH: v.videoHeight, tracks: st.getTracks().length };'
  +'})(' + SRC.w + ', ' + SRC.h + ')';

module.exports = { INSTALL, INJECT_DOUBLE, SRC };
