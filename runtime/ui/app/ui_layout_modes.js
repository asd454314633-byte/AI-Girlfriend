/* ============================================================================
 * ui_layout_modes.js — AI_GIRLFRIEND_UI_DETAIL_COMPOSITION_REPAIR_01 (V2)
 *
 * 纯显示层控制器。V2 之后它【只】做四件事（§21 的职责收紧）：
 *   1. 维护 viewMode 状态（body 的 data 属性）与模式切换
 *   2. 提供【唯一】一块布局控制面板（默认收起）+ 一个「画面布局」入口
 *   3. 驱动 owner：把滑块/预设/分隔线位置转成 AvatarLayout 的 setScale /
 *      setStretch / set / setBoundary 调用
 *   4. 把自己的状态（viewMode + LIVE 参数 + 收起态）存进 localStorage
 *
 * 硬边界（任务 §0/§2/§11/§21/§22）：
 *   · 不写 #av-video 的 transform / mask —— 一个字节都不写。DETAIL 的显示几何
 *     权威是 avatar_layout.js（AVATAR_LAYOUT_TRANSFORM_OWNER），这里再写一份
 *     就是第二个 transform owner。
 *   · 不新建 video/audio，不 cloneNode，不 removeChild/appendChild 任何媒体节点
 *   · 不 fetch、不发 WebSocket、不碰 RTCPeerConnection、不碰 Session
 *   · 无 setInterval、无 requestAnimationFrame 常驻循环；拖动只用 pointermove
 *
 * V1→V2 改了什么（现场实测见 PREFLIGHT_ROOT_CAUSE.md）：
 *   V1 的 DETAIL 用 pane 模型：分隔线 → --uivm-content-w → 同时压 #app 宽度、
 *   改 #av-video 盒子宽度，再被 object-fit:contain 把人物钉在"源高→视口高"
 *   那一档。实测 pane 28%→70% 时盒子 533→1333（2.50×）而人物带只有
 *   533→622（1.17×），711px 是黑；黑雾按盒子百分比画，1009 就走完而人物
 *   1283 才开始 ⇒ 用户看到的"主UI｜独立黑矩形｜人物"。工程 Gate 当时全绿，
 *   因为绿的是【容器行为】而不是【构图行为】。
 *   V2 把 DETAIL 交回 avatar-sync 的原生全屏合成（§4 FULL_VIEWPORT_COMPOSITE）：
 *     · #av-layer 永远是完整 viewport 背景层，人物在同一个全屏层里
 *     · #app 宽度不再随线动（§14 主 UI 不被挤压）
 *     · 分隔线 = "人物视觉边界"：拖它 = 反解 uniform scale 交给 owner（§5/§6）
 *     · 旧键 detail.paneRatio 进入 DEPRECATED / IGNORED，不再影响生产布局（§6）
 *   LIVE_PORTRAIT 的 9:16 舞台模型、zoom/X/Y 语义、捕获区逻辑一字未动（§2）。
 *
 * 回退：?noviewmode=1 或 localStorage.uiViewModeDisabled='1' ⇒ 本模块整体不启动，
 * avatar_layout.js 保留它自己的入口与卡片，页面回到 V1 之前那套原生表现。
 * ==========================================================================*/
(function (global) {
  'use strict';

  var disabled = false;
  try {
    disabled = localStorage.getItem('uiViewModeDisabled') === '1'
      || /[?&]noviewmode=1/.test(location.search);
  } catch (_) { /* 隐私模式：当作没关 */ }
  if (disabled) { global.UiViewModes = { disabled: true }; return; }

  var STORE_KEY = 'ai_girlfriend_ui_layout_v1';       // V1 同名键：只换内容，不换名字
  var PANEL_KEY = 'ai_girlfriend_uivm_panel_v2';      // V2 新增：面板收起态
  var LEGACY_PANEL_KEY = 'uiViewModePanelCollapsed';  // V1 遗留：忽略但【不删】（用户数据）

  /* ------------------------------------------------------------ 边界表 */
  // V1 的 PANE = {min:28,max:70} 是【容器宽度】的边界，V2 不再用它驱动布局；
  // 留着只为把旧值 clamp 干净并让旧 API 有个可解释的落点（§6 DEPRECATED）。
  var PANE = { min: 28, max: 70, dflt: null, balanced: 45, avatarFirst: 62 };
  var LV = {
    scale: { min: 100, max: 200, dflt: 100 },
    x:     { min: -100, max: 100, dflt: 0 },
    y:     { min: -100, max: 100, dflt: 0 },
  };
  /* LIVE 的 ±100 → 人物自身盒 ±25% 平移，这个 0.25 只在 CSS 里落地
   * （ui_layout_modes.css 的 translate(calc(var(--uivm-lv-x) * 0.25%)…)），
   * JS 侧刻意不复制同一份系数——复制了就会有"改了 CSS 忘了改 JS"的两权威漂移。 */
  var MODES = { detail: 1, 'live-portrait': 1 };
  var MODE_LABEL = { detail: '详情模式', 'live-portrait': '直播竖屏' };
  /* DETAIL 预设 = 人物大小档位（owner 的 scale），不再是左右分配比例。 */
  var D_PRESETS = [
    { id: 'original', label: '原始', s: 100 },
    { id: 'close', label: '拉近', s: 140 },
    { id: 'closeup', label: '特写', s: 180 },
    { id: 'big', label: '大特写', s: 220 },
  ];

  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
  function num(v, lo, hi, dflt) {
    var n = typeof v === 'number' ? v : parseFloat(v);
    if (!isFinite(n)) return dflt;          // 非数字 → 默认，不抛
    return clamp(n, lo, hi);
  }

  /* ------------------------------------------------------- 不可信输入 */
  var state = blank();
  var storeDirty = false;        // 本次 load 是否动过手（缺字段/被 clamp/整份作废）
  var migrated = { paneRatioDropped: null, legacyPanelKeyIgnored: null };

  function blank() {
    return {
      viewMode: 'detail',
      // DETAIL 的人物大小/宽度/左右/上下【不在这里】—— 权威 owner 是
      // avatar_layout.js 的 avatarVideoScale/StretchX/OffsetX/OffsetY。
      // V2 §20 明文："已有 scale/x/y owner 必须继续复用，只新增 stretchX，
      // 不得复制 scale/x/y 状态"。这里存一份就必然漂移。
      detail: { paneRatio: null, deprecated: 'DEPRECATED_IGNORED' },
      livePortrait: { scale: LV.scale.dflt, offsetX: LV.x.dflt, offsetY: LV.y.dflt },
    };
  }

  /** 取一个不可信数值：缺失/非数字/越界一律换成合法值，并标记"动过手"。 */
  function take(v, lo, hi, dflt) {
    var out = (v === null || v === undefined) ? dflt : num(v, lo, hi, dflt);
    if (out !== v) storeDirty = true;
    return out;
  }

  function load() {
    var raw = null;
    try { raw = localStorage.getItem(STORE_KEY); } catch (_) { return; }
    if (!raw) { storeDirty = true; return; }   // 第一次进：把默认值落下去
    var p;
    try {
      p = JSON.parse(raw);
      if (!p || typeof p !== 'object' || Array.isArray(p)) throw new Error('shape');
    } catch (_) {
      // JSON 损坏 / 形状不对 → 整份作废，回到默认
      try { localStorage.removeItem(STORE_KEY); } catch (e) {}
      storeDirty = true;
      return;
    }
    if (p.viewMode && MODES[p.viewMode]) {
      state.viewMode = p.viewMode;
    } else {
      storeDirty = true;                        // 非法/缺失 viewMode → 落回 detail
    }
    if (!p.livePortrait || typeof p.livePortrait !== 'object') storeDirty = true;
    var l = (p.livePortrait && typeof p.livePortrait === 'object') ? p.livePortrait : {};
    state.livePortrait.scale   = take(l.scale,   LV.scale.min, LV.scale.max, LV.scale.dflt);
    state.livePortrait.offsetX = take(l.offsetX, LV.x.min,     LV.x.max,     LV.x.dflt);
    state.livePortrait.offsetY = take(l.offsetY, LV.y.min,     LV.y.max,     LV.y.dflt);
    /* V2 §6/§20：旧 detail.paneRatio 只登记，不再参与任何布局计算。
     * 实测用户 origin 上是 {"detail":{"paneRatio":70}} —— 那正是被拒掉的构图，
     * 继续读它等于把这次纠偏原地作废。save() 会把它从对象里彻底摘掉。 */
    var d = (p.detail && typeof p.detail === 'object') ? p.detail : {};
    if (d.paneRatio !== undefined && d.paneRatio !== null) {
      migrated.paneRatioDropped = d.paneRatio;
      storeDirty = true;
    }
    state.detail.paneRatio = null;             // 永远 null = 不再有 pane 覆盖
    try {
      if (localStorage.getItem(LEGACY_PANEL_KEY) !== null) {
        migrated.legacyPanelKeyIgnored = localStorage.getItem(LEGACY_PANEL_KEY);
      }
    } catch (_) {}
  }

  function save() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify({
        version: 2,
        viewMode: state.viewMode,
        detail: { paneRatio: null, deprecated: 'DEPRECATED_IGNORED' },
        livePortrait: state.livePortrait,
      }));
      storeDirty = false;                        // 只有真写进去才算"已经干净"
    } catch (_) { /* 存不进去不致命，本次会话仍生效 */ }
  }

  /* ---------------------------------------------------------- owner 句柄 */
  // DETAIL 的唯一 transform owner。本文件任何时候都只通过下面这几个方法驱动它。
  function owner() {
    var a = global.AvatarLayout;
    if (!a || a.disabled || typeof a.set !== 'function') return null;
    return a;
  }
  function ownerLimits() {
    var o = owner();
    return (o && o.limits) || { sMin: 70, sMax: 240, wMin: 80, wMax: 160,
                                xMin: -100, xMax: 100, yMin: -100, yMax: 100 };
  }

  /* ------------------------------------------------------------ 取几何 */
  function videoEl() { return document.getElementById('av-video'); }
  function layerEl() { return document.getElementById('av-layer'); }

  /** 百分比参照系 = #av-layer 的宽度（它 inset:0，与 body 内容盒同宽）。 */
  function frameW() {
    var l = layerEl();
    var w = l ? l.getBoundingClientRect().width : 0;
    if (!(w > 0)) w = document.documentElement.clientWidth || window.innerWidth || 1280;
    return w;
  }

  /** 既有页头（header.topbar）的实际高度 —— 面板停靠要贴着它下面。
   *  标题行数会随窗口宽度换行，写死一个 px 迟早对不上，所以现场量。
   *  只在 apply() 里读，无常驻任务（§11/§22）。 */
  function topbarH() {
    var t = document.querySelector('header.topbar');
    if (!t) return 0;
    var h = t.getBoundingClientRect().height;
    return (isFinite(h) && h > 0) ? h : 0;
  }

  /* --------------------------------------------------------- 写 CSS 变量 */
  function apply() {
    var b = document.body;
    b.dataset.viewMode = state.viewMode;
    delete b.dataset.uivmPane;                 // V2：pane 属性不再存在（旧覆盖一并失效）

    var th = topbarH();
    var cap = Math.max(0, (window.innerHeight || 800) * 0.4);
    b.style.setProperty('--uivm-topbar-h', Math.round(Math.min(th, cap)) + 'px');

    if (state.viewMode === 'detail') {
      // V2 §4：DETAIL 不做任何左右空间分配。#app 全宽，人物是它【底下】的
      // 全屏背景层；唯一的线是"人物视觉边界"，位置由 owner 的实测矩形给出。
      var o = owner();
      var g = o ? o.geometry() : null;
      if (g && Number.isFinite(g.bandLeft)) {
        var frame = frameW();
        b.style.setProperty('--uivm-divider-x', Math.round(clamp(g.bandLeft, 0, frame)) + 'px');
        b.dataset.uivmBoundary = g.bandLeft > 4 ? 'on' : 'off';
      } else {
        b.style.setProperty('--uivm-divider-x', Math.round(frameW()) + 'px');
        b.dataset.uivmBoundary = 'off';        // 量不到人物（还没流）就别露钩子
      }
    } else {
      delete b.dataset.uivmBoundary;
      var s = state.livePortrait;
      b.style.setProperty('--uivm-lv-s', String(s.scale / 100));
      b.style.setProperty('--uivm-lv-x', String(s.offsetX));
      b.style.setProperty('--uivm-lv-y', String(s.offsetY));
    }
    syncPanel();
  }

  /* ---------------------------------------------------------------- API */
  function setViewMode(mode) {
    if (!MODES[mode] || mode === state.viewMode) return;
    state.viewMode = mode;
    // 让 owner 重算一次：离开 DETAIL 时收回它写的内联 mask，回到 DETAIL 时
    // 再按实测矩形画回去（§22 DETAIL 参数不得污染 LIVE）。
    var o = owner();
    apply();
    if (o && o.onModeChange) o.onModeChange();
    save();
  }

  /** V1 的 API 兼容壳：接受调用但不再产生任何布局影响（§6 DEPRECATED/IGNORED）。 */
  function setPane(ratio) {
    void ratio;
    return 'DEPRECATED_IGNORED';
  }

  /* ------------------------------------------------------------ 分隔线 */
  // V2 §5/§13：这条线代表【人物视觉边界】，不是应用分栏框。
  //   左拖 → 人物真实等比例放大、向页面内部扩展、黑雾边界同步左移
  //   右拖 → 反之
  // 反解公式在 owner 里（scaleForBoundary），本文件只负责把 clientX 递过去。
  function buildDivider() {
    if (document.getElementById('uivm-divider')) return;
    var d = document.createElement('div');
    d.id = 'uivm-divider';
    d.className = 'uivm-divider';
    d.setAttribute('role', 'separator');
    d.setAttribute('aria-orientation', 'vertical');
    d.setAttribute('tabindex', '0');
    d.setAttribute('aria-label', '拖动调整人物在画面里的占比');
    d.title = '左右拖动：人物变大 / 变小';

    var dragging = false;

    function push(clientX, quiet) {
      var o = owner();
      if (!o) return false;
      o.setBoundary(clientX, { quiet: quiet });   // 拖动中不落盘，松手才落
      return true;
    }

    d.addEventListener('pointerdown', function (ev) {
      if (!owner()) return;
      dragging = true;
      d.classList.add('uivm-dragging');
      try { d.setPointerCapture(ev.pointerId); } catch (_) {}
      push(ev.clientX, true);
      ev.preventDefault();
    });
    d.addEventListener('pointermove', function (ev) {
      if (!dragging) return;                       // 只在按住时算，无常驻任务
      push(ev.clientX, true);
    });
    function stop(ev) {
      if (!dragging) return;
      dragging = false;
      d.classList.remove('uivm-dragging');
      try { d.releasePointerCapture(ev.pointerId); } catch (_) {}
      var o = owner();
      if (o) o.persist();                          // §13：松手后保存
    }
    d.addEventListener('pointerup', stop);
    d.addEventListener('pointercancel', stop);
    d.addEventListener('dblclick', function () {
      var o = owner();
      if (o) o.resetToDefault();                   // 双击 = 回到原生构图
    });
    d.addEventListener('keydown', function (ev) {
      var o = owner();
      if (!o) return;
      var g = o.geometry();
      if (!g) return;
      var step = ev.shiftKey ? 48 : 16;            // px：键盘也按"边界"走
      if (ev.key === 'ArrowLeft') { o.setBoundary(g.bandLeft - step); ev.preventDefault(); }
      else if (ev.key === 'ArrowRight') { o.setBoundary(g.bandLeft + step); ev.preventDefault(); }
      else if (ev.key === 'Home') { o.resetToDefault(); ev.preventDefault(); }
    });

    document.body.appendChild(d);
  }

  /* ---------------------------------------------------------- 控制面板 */
  var panel = null, entry = null, dInputs = {}, lvInputs = {};
  var collapsed = true;            // V2 §16：默认收起

  /** AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01：
   *  左侧控制 Dock 的「画面」分组里有一个静态 slot（index.html 里写死）。
   *  本轮把「画面布局」入口按钮与这块调节面板都挂进去，废弃上一轮
   *  "body 上的独立悬浮层"。
   *
   *  这里只改【挂载点】，不改面板里的任何控件、不改收起/展开语义、
   *  不改 mode 切换与 owner 驱动逻辑（第 4 条：不重新实现控件逻辑、
   *  不复制控件、不建立第二份状态）。
   *
   *  slot 不存在时（index.html 被回滚、或 gf_dock 那套没装载）逐字回退到
   *  上一轮的行为：面板挂 body、入口挂 .gf-controls，回退路径不变。 */
  function dockSlot() {
    return document.getElementById('gf-dock-screen-slot');
  }

  function buildPanel() {
    if (panel) return;
    panel = document.createElement('section');
    panel.id = 'uivm-panel';
    panel.className = 'uivm-panel';
    panel.setAttribute('aria-label', '画面布局');

    var head = el('div', 'uivm-head');
    var title = el('span', 'uivm-title', '画面布局');
    var modeTag = el('span', 'uivm-mode-tag', '');
    modeTag.id = 'uivm-mode-tag';
    var col = el('button', 'uivm-collapse');
    col.type = 'button';
    col.textContent = '收起调整';
    col.setAttribute('aria-label', '收起调整');
    col.addEventListener('click', function () { setCollapsed(!collapsed); });
    head.append(title, modeTag, col);
    panel.appendChild(head);

    var bodyWrap = el('div', 'uivm-body');

    /* 模式入口：面板内一级按钮，当前模式有明确选中态 */
    var modes = el('div', 'uivm-modes');
    modes.appendChild(modeBtn('detail', MODE_LABEL.detail));
    modes.appendChild(modeBtn('live-portrait', MODE_LABEL['live-portrait']));
    bodyWrap.appendChild(modes);

    /* ---- DETAIL 分区：四个能力各一条，全部走 owner ---- */
    var secD = el('div', 'uivm-section uivm-sec-detail');
    secD.appendChild(el('p', 'uivm-note',
      '大小为等比例缩放，宽度为横向拉伸。左右：-100 左对齐、0 居中、100 右对齐。' +
      '调整立即生效并自动保存。'));
    var OL = ownerLimits();
    var lim = function (lo, hi, dflt) { return { min: lo, max: hi, dflt: dflt }; };
    secD.appendChild(dslider('s', '人物大小', lim(OL.sMin, OL.sMax, 100), '%', function (v) {
      var o = owner(); if (o) o.set('scale', v);
    }));
    secD.appendChild(dslider('w', '人物宽度', lim(OL.wMin, OL.wMax, 100), '%', function (v) {
      var o = owner(); if (o) o.set('stretch', v);
    }));
    secD.appendChild(dslider('x', '左右位置', lim(OL.xMin, OL.xMax, 0), '', function (v) {
      var o = owner(); if (o) o.set('x', v);
    }));
    secD.appendChild(dslider('y', '上下位置', lim(OL.yMin, OL.yMax, 0), '', function (v) {
      var o = owner(); if (o) o.set('y', v);
    }));
    var rowD = el('div', 'uivm-row');
    D_PRESETS.forEach(function (p) {
      var b = el('button', 'uivm-btn');
      b.type = 'button';
      b.textContent = p.label;
      b.dataset.preset = p.id;
      b.addEventListener('click', function () {
        var o = owner(); if (o) o.setScale(p.s);
      });
      rowD.appendChild(b);
    });
    secD.appendChild(rowD);
    var rstD = el('button', 'uivm-btn uivm-reset');
    rstD.type = 'button';
    rstD.textContent = '恢复默认';
    rstD.addEventListener('click', function () {                    // 只碰 DETAIL 的 owner
      var o = owner(); if (o) o.resetToDefault();
    });
    secD.appendChild(rstD);
    bodyWrap.appendChild(secD);

    /* ---- LIVE 分区（§2：这套参数与语义保持 KEEP，不做任何重新实现） ---- */
    var secL = el('div', 'uivm-section uivm-sec-live');
    secL.appendChild(el('p', 'uivm-note',
      '左侧 9:16 取景框就是直播伴侣要捕捉的区域。以下参数只作用于直播竖屏。'));
    secL.appendChild(slider('scale', '人物大小', LV.scale, function (v) {
      state.livePortrait.scale = v;
    }));
    secL.appendChild(slider('offsetX', '左右位置', LV.x, function (v) {
      state.livePortrait.offsetX = v;
    }));
    secL.appendChild(slider('offsetY', '上下位置', LV.y, function (v) {
      state.livePortrait.offsetY = v;
    }));
    var rstL = el('button', 'uivm-btn uivm-reset');
    rstL.type = 'button';
    rstL.textContent = '恢复直播竖屏默认';
    rstL.addEventListener('click', function () {                       // 只碰 live
      state.livePortrait = { scale: LV.scale.dflt, offsetX: LV.x.dflt, offsetY: LV.y.dflt };
      apply(); save();
    });
    secL.appendChild(rstL);
    bodyWrap.appendChild(secL);

    panel.appendChild(bodyWrap);
    /* 挂进 Dock 的「画面」分组；没有 slot 才退回 body 悬浮层（上一轮行为）。 */
    var slot = document.getElementById('gf-workspace');
    if (slot) { panel.classList.add('uivm-preview-controls'); slot.prepend(panel); }
    else document.body.appendChild(panel);

    try {
      var k = localStorage.getItem(PANEL_KEY);
      // 只有用户明确说"我要展开"才展开；其余（含 V1 遗留键）一律默认收起（§16）。
      collapsed = k !== '0';
    } catch (_) { collapsed = true; }
    renderCollapsed();
  }

  /** 主页面唯一入口（§16：只保留一个「画面布局」小入口，点击展开、再点收起）。
   *  Dock 轮：入口按钮也搬进「画面」分组，和它控制的那块面板待在一起。
   *  boot() 的顺序是 buildPanel() → buildEntry()，所以面板已经在 slot 里了，
   *  这里用 prepend 把按钮放到面板上面（prepend 是插入新节点，不是复制面板）。
   *  没有 slot 时逐字走上一轮的路径：挂在 .gf-controls 里「数字人」之后。 */
  function buildEntry() {
    if (entry) return;
    var front = document.getElementById('gf-layout-open');
    if (front) {
      entry = front;
      front.addEventListener('click', togglePanel);
      var shortcut = document.getElementById('settings-preview-open');
      if (shortcut) shortcut.addEventListener('click', function () {
        var dialog = document.getElementById('settings-modal');
        if (dialog && dialog.open) dialog.close();
        setCollapsed(false);
      });
      renderCollapsed();
      return;
    }
    var slot = dockSlot();
    if (slot) return; // 设置分组本身就是入口，直接显示控件。
    var controls = slot || document.querySelector('.gf-controls');
    if (!controls) return;
    var btn = document.createElement('button');
    btn.id = 'gf-layout-open';
    btn.type = 'button';
    btn.className = 'btn ghost gf-layout-open';
    btn.textContent = '画面布局';
    btn.setAttribute('aria-controls', 'uivm-panel');
    btn.setAttribute('aria-expanded', String(!collapsed));
    btn.addEventListener('click', function () { setCollapsed(collapsed ? false : true); });
    if (slot) {
      slot.prepend(btn);
    } else {
      var avatarBtn = document.getElementById('gf-avatar-open');
      if (avatarBtn && avatarBtn.parentElement === controls) avatarBtn.after(btn);
      else controls.append(btn);
    }
    entry = btn;
  }

  function setCollapsed(v) {
    collapsed = !!v;
    renderCollapsed();
    try { localStorage.setItem(PANEL_KEY, collapsed ? '1' : '0'); } catch (_) {}
  }

  function renderCollapsed() {
    if (!panel) return;
    if (collapsed) panel.setAttribute('data-collapsed', '1');
    else panel.removeAttribute('data-collapsed');
    var head = panel.querySelector('.uivm-collapse');
    if (head) {
      head.textContent = '收起调整';
      head.setAttribute('aria-label', '收起调整');
    }
    if (entry) entry.setAttribute('aria-expanded', String(!collapsed));
    if (document.body) document.body.dataset.previewAdjusting = collapsed ? 'false' : 'true';
    // 收起态只留一条标题；展开态才需要重新量一次边界（一次性，非常驻）。
    if (!collapsed) apply();
  }

  function togglePanel() { setCollapsed(!collapsed); }

  function modeBtn(mode, label) {
    var b = el('button', 'uivm-mode');
    b.type = 'button';
    b.textContent = label;
    b.dataset.mode = mode;
    b.addEventListener('click', function () { setViewMode(mode); });
    return b;
  }

  /** LIVE 滑块：值存在本模块的 state.livePortrait 里（LIVE 没有 owner）。 */
  function slider(key, label, lim, setter) {
    var wrap = el('label', 'uivm-field');
    var lab = el('span', 'uivm-field-label');
    lab.appendChild(el('span', '', label));
    var out = document.createElement('output');
    lab.appendChild(out);
    var input = document.createElement('input');
    input.type = 'range';
    input.min = String(lim.min);
    input.max = String(lim.max);
    input.step = '5';
    input.dataset.uivmLv = key;
    input.setAttribute('aria-label', label);
    input.addEventListener('input', function () {
      setter(num(input.value, lim.min, lim.max, lim.dflt));
      apply();
      save();
    });
    wrap.append(lab, input);
    lvInputs[key] = { input: input, out: out, lim: lim, suffix: key === 'scale' ? '%' : '' };
    return wrap;
  }

  /** DETAIL 滑块：值【不在本模块存】—— 现值向 owner 取，写入向 owner 交。
   *  这是 §21 唯一 owner 约束在 UI 层的落点：面板只是遥控器。 */
  function dslider(key, label, lim, suffix, setter) {
    var wrap = el('label', 'uivm-field');
    var lab = el('span', 'uivm-field-label');
    lab.appendChild(el('span', '', label));
    var out = document.createElement('output');
    lab.appendChild(out);
    var input = document.createElement('input');
    input.type = 'range';
    input.min = String(lim.min);
    input.max = String(lim.max);
    input.step = '5';
    input.dataset.uivmD = key;
    input.setAttribute('aria-label', label);
    input.addEventListener('input', function () {
      setter(num(input.value, lim.min, lim.max, lim.dflt));
    });
    wrap.append(lab, input);
    dInputs[key] = { input: input, out: out, lim: lim, suffix: suffix };
    return wrap;
  }

  function syncPanel() {
    if (!panel) return;
    var tag = document.getElementById('uivm-mode-tag');
    if (tag) tag.textContent = MODE_LABEL[state.viewMode] || '';
    panel.querySelectorAll('.uivm-mode').forEach(function (b) {
      b.setAttribute('aria-pressed', b.dataset.mode === state.viewMode ? 'true' : 'false');
    });
    var o = owner();
    if (!o) {
      // owner 被 ?nolayout=1 / avatarLayoutDisabled 关掉：滑杆禁用并说明原因，
      // 不去发明第二条写 transform 的路（那会直接违反 §21）。
      Object.keys(dInputs).forEach(function (k) { dInputs[k].input.disabled = true; });
      panel.querySelectorAll('.uivm-btn[data-preset]').forEach(function (b) { b.disabled = true; });
      panel.querySelectorAll('.uivm-sec-detail .uivm-reset').forEach(function (b) { b.disabled = true; });
      return;
    }
    Object.keys(dInputs).forEach(function (k) { dInputs[k].input.disabled = false; });
    panel.querySelectorAll('.uivm-btn[data-preset]').forEach(function (b) { b.disabled = false; });
    panel.querySelectorAll('.uivm-sec-detail .uivm-reset').forEach(function (b) { b.disabled = false; });
    var vals = { s: o.state.s, w: o.state.w, x: o.state.x, y: o.state.y };
    Object.keys(dInputs).forEach(function (k) {
      var ref = dInputs[k], v = vals[k];
      if (document.activeElement !== ref.input) ref.input.value = String(v);
      if (k === 's') ref.out.textContent = o.isDefault() ? '默认' : v + '%';
      else if (k === 'w') ref.out.textContent = v + '%' + (v === (ref.lim.dflt) ? '（原始）' : '');
      else ref.out.textContent = (v > 0 ? '+' : '') + v;
    });
    panel.querySelectorAll('.uivm-btn[data-preset]').forEach(function (b) {
      var want = null;
      D_PRESETS.forEach(function (p) { if (p.id === b.dataset.preset) want = p.s; });
      b.classList.toggle('uivm-active', !o.isDefault() && o.state.s === want);
    });
    Object.keys(lvInputs).forEach(function (k) {
      var ref = lvInputs[k], v = state.livePortrait[k];
      if (document.activeElement !== ref.input) ref.input.value = String(v);
      ref.out.textContent = (k === 'scale' ? v + '%' : (v > 0 ? '+' : '') + v);
    });
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  /* -------------------------------------------------------------- 启动 */
  function boot() {
    load();
    if (storeDirty) save();

    // 布局控制面合并成【一个】：请 owner 收回它自己的入口与卡片。
    // 反过来（owner 不存在 / 被关）时不调用，V1 的 ?nolayout=1 回退路径保持原样。
    var o = owner();
    if (o && typeof o.suppressOwnControls === 'function') o.suppressOwnControls();

    buildDivider();
    buildPanel();
    buildEntry();
    apply();

    if (o && typeof o.subscribe === 'function') {
      // owner 的值一变（拖动 / 预设 / 键盘 / resize）这边就重新落一次边界与读数。
      o.subscribe(function () { apply(); });
    }

    // 首帧之后人物才有真实尺寸，量一次边界（一次性，不是循环）。
    var v = videoEl();
    if (v) v.addEventListener('loadedmetadata', function () { apply(); });
    // 窗口尺寸变化后重算：边界线跟着人物走，不越界（§11 允许的更新时机之一）。
    // 刻意同步 apply()：不引入 rAF / 定时器。
    window.addEventListener('resize', function () { apply(); });
  }

  if (document.readyState === 'loading') {
    // 属性在解析期就落好，避免首帧闪一下旧布局
    try { document.body.dataset.viewMode = state.viewMode; } catch (_) {}
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  /* 调试 / 验收挂点 */
  global.UiViewModes = {
    disabled: false,
    version: 2,
    state: state,
    limits: { pane: PANE, live: LV, detail: ownerLimits() },
    migrated: migrated,
    set: setViewMode,
    setPane: setPane,                 // DEPRECATED 兼容壳
    apply: apply,
    read: function () { try { return localStorage.getItem(STORE_KEY); } catch (_) { return null; } },
    panel: {
      isCollapsed: function () { return collapsed; },
      setCollapsed: setCollapsed,
      toggle: togglePanel,
      rect: function () {
        var p = document.getElementById('uivm-panel');
        if (!p) return null;
        var r = p.getBoundingClientRect();
        return { x: r.left, y: r.top, w: r.width, h: r.height,
                 right: r.right, bottom: r.bottom,
                 visible: r.width > 0 && r.height > 0 && !p.hasAttribute('data-collapsed') };
      },
    },
    boundary: {
      x: function () {
        var o = owner(); var g = o ? o.geometry() : null;
        return g ? g.bandLeft : null;
      },
      set: function (px) { var o = owner(); return o ? o.setBoundary(px) : null; },
    },
    ownerGeometry: function () {
      var o = owner();
      return o ? o.geometry() : null;
    },
    toggle: togglePanel,
    videoIdentity: function () {
      var v = videoEl();
      return { present: !!v, count: document.querySelectorAll('#av-video').length,
               allVideos: document.querySelectorAll('video').length,
               hasSrcObject: !!(v && v.srcObject), currentTime: v ? v.currentTime : null };
    },
  };
})(typeof window !== 'undefined' ? window : globalThis);
