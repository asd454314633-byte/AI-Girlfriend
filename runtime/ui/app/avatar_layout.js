/* ============================================================================
 * avatar_layout.js — AI_GIRLFRIEND_UI_DETAIL_COMPOSITION_REPAIR_01 (V2)
 *
 * 本文件是 DETAIL 下 #av-video 显示几何的【唯一 owner】（§21/§35）。
 * 它只写这个元素的四个内联显示属性：transform / transform-origin /
 * mask-image / -webkit-mask-image。不碰 srcObject、不碰属性节点、
 * 不新建元素、不复制媒体对象、不发任何请求。
 *
 * V1→V2 为什么必须动这个文件（现场实测，见 PREFLIGHT_ROOT_CAUSE.md）：
 *   V1 的 DETAIL 拖动杠杆是 pane 宽度（--uivm-content-w），人物被
 *   ui_layout_modes.css 的 `height:100% + object-fit:contain` 钉死在
 *   "源高 1056 → 视口高 1080"这一档：pane 从 28% 拖到 70%，盒子宽 533→1333
 *   （2.50×），人物渲染带只有 533→622（1.17×），多出来的 711px 全是黑；
 *   而 avatar-sync 的 mask 按【盒子】百分比画，导致黑雾在 x=1009 就走完、
 *   人物 x=1283 才开始 —— 中间 274px 纯黑带就是用户拒掉的"独立黑色矩形区域"。
 *   V2 把 DETAIL 交回 avatar-sync 的原生全屏几何（V2 §4 FULL_VIEWPORT_COMPOSITE），
 *   杠杆换成 owner 的 uniform scale：实测 setScale(180) 时人物带 622→1119.6、
 *   左缘 1359.4→1110.7（真的向页面内部扩展），mask 随 transform 一起缩放。
 *
 * 四能力解耦（V2 §9，互不覆盖）：
 *   人物大小  70%~240%  默认 100% → uniform scale(s)        avatarVideoScale   （复用旧键）
 *   人物宽度  80%~160%  默认 100% → 追加 scaleX(w)          avatarVideoStretchX（本轮新增，唯一新键）
 *   左右位置  -100~+100           → translateX(x*0.4%)      avatarVideoOffsetX （复用旧键）
 *   上下位置  -100~+100           → translateY(y*0.4%)      avatarVideoOffsetY （复用旧键）
 *   合成：scale(s) scaleX(w) translate(tx,ty) —— translate 放在最里层，
 *   于是平移量随缩放一起长大（放大后需要平移的距离本来就更长），
 *   而 s=1 时与 V1 逐像素相同 ⇒ 旧参数不会被这次改动重新解释。
 *   （V2 §20：已有 scale/x/y 的 owner 必须继续复用，只新增 stretchX，
 *     绝不复制一份 scale/x/y 状态 —— 复制了就会有第二权威。）
 *
 * 锚点从 V1 的 `50% 100%`（底部）换成 `100% 0%`（右上）：实测底部锚定时
 * scale=1.8 会把盒子上缘推到 y=-864，也就是【放大把头顶出画面】，与 §5
 * "脸部明显变大"正好相反。右上锚定 = 人物右缘与顶缘钉住，放大只向页面内部
 * 和下方生长，超出视口的部分交给 #av-layer{overflow:hidden} 裁切（§7）。
 *
 * 黑雾联动（V2 §11/§12）：接管后按【实测人物矩形】重画 mask —— 渐变覆盖
 * 屏幕等宽的一段接缝（宽 = 0.42×baseWidth，即原生那一档的雾宽），起点钉在
 * 人物带左缘。于是"人物左缘"与"黑雾边界"永远同向移动，不会再出现
 * VIDEO_MOVED=TRUE 而 FADE_MOVED=FALSE。默认态（从未接管）【零覆盖】，
 * 继续用 avatar-sync 注入的那份 mask，首屏与改造前逐像素一致。
 * 更新时机只有：slider input / 分隔线 pointermove / 模式切换 / resize /
 * loadedmetadata —— 无常驻 rAF、无定时器（§11）。
 *
 * 与 ui_layout_modes.js 的分工（§21）：那个文件只 切模式 / 存预设 /
 * 【驱动本 owner】（调 setScale/setStretch/setOffset/apply），
 * 它自己一行都不写 video.style.transform。
 *
 * A/B 回退：localStorage.avatarLayoutDisabled='1' 或 URL 带 ?nolayout=1 时
 * 本模块完全不启动（页面回到 avatar-sync.js 的原生表现）。
 * ==========================================================================*/
(function (global) {
  'use strict';

  var disabled = false;
  try {
    disabled = localStorage.getItem('avatarLayoutDisabled') === '1'
      || /[?&]nolayout=1/.test(location.search);
  } catch (_) { /* ignore */ }
  if (disabled) {
    global.AvatarLayout = { disabled: true };
    return;
  }

  var KEYS = {
    s: 'avatarVideoScale',
    w: 'avatarVideoStretchX',
    x: 'avatarVideoOffsetX',
    y: 'avatarVideoOffsetY',
  };
  var LIMITS = {
    sMin: 70,  sMax: 240,      // V2 §10：人物大小 70%~240%，默认 100%
    wMin: 80,  wMax: 160,      // V2 §8/§10：人物宽度 80%~160%，默认 100%（原始比例）
    xMin: -100, xMax: 100,
    yMin: -100, yMax: 100,
  };
  var DEFAULTS = { s: 100, w: 100, x: 0, y: 0 };
  // 滑块 ±100 映射为 ±40% 自身尺寸的平移。
  var OFFSET_FACTOR = 0.4;
  // 黑雾参照：avatar-sync.js 的 maskFade=0.42 作用在【盒子】上；接管后把它
  // 变成作用在【屏幕等宽的一段接缝】上的 0.42×baseWidth —— s=1 时两者重合，
  // 放大/拉宽后雾宽不再跟着人物一起变粗（否则雾会爬上脸）。
  var FADE_REF = 0.42;
  var FADE_SHAPE = [0, 0.45, 0.781, 1];   // = avatar-sync 的 0% / 18.9% / 32.8% / 42%
  var PRESETS = [
    { name: '原始', s: 100 },
    { name: '拉近', s: 140 },
    { name: '特写', s: 180 },
    { name: '大特写', s: 220 },
  ];

  var state = { s: DEFAULTS.s, w: DEFAULTS.w, x: DEFAULTS.x, y: DEFAULTS.y };
  // true = 尚未接管（视频保持 avatar-sync.js 原生舞台几何与它自己的 mask）。
  var defaultMode = true;
  var card = null;
  var inputs = {};
  var suppressed = false;      // 控制面板由 ui_layout_modes.js 统一提供
  var subs = [];

  function clamp(v, min, max) { return Math.min(max, Math.max(min, v)); }
  function num(v, dflt) {
    var n = typeof v === 'number' ? v : parseFloat(v);
    return Number.isFinite(n) ? n : dflt;
  }

  function load() {
    try {
      var s = parseFloat(localStorage.getItem(KEYS.s));
      var w = parseFloat(localStorage.getItem(KEYS.w));
      var x = parseFloat(localStorage.getItem(KEYS.x));
      var y = parseFloat(localStorage.getItem(KEYS.y));
      if (Number.isFinite(s)) { state.s = clamp(s, LIMITS.sMin, LIMITS.sMax); defaultMode = false; }
      if (Number.isFinite(w)) { state.w = clamp(w, LIMITS.wMin, LIMITS.wMax); defaultMode = false; }
      if (Number.isFinite(x)) { state.x = clamp(x, LIMITS.xMin, LIMITS.xMax); defaultMode = false; }
      if (Number.isFinite(y)) { state.y = clamp(y, LIMITS.yMin, LIMITS.yMax); defaultMode = false; }
      // 历史遗留键：窗口化比例（v1）与 pane 模型（V1 DETAIL）都不再由本文件持有。
      localStorage.removeItem('avatarPanelRatio');
    } catch (_) { /* 隐私模式等场景下读不到就算了 */ }
  }

  function save() {
    try {
      localStorage.setItem(KEYS.s, String(state.s));
      localStorage.setItem(KEYS.w, String(state.w));
      localStorage.setItem(KEYS.x, String(state.x));
      localStorage.setItem(KEYS.y, String(state.y));
    } catch (_) { /* 存不进去不致命，本次会话仍然生效 */ }
  }

  function clearSaved() {
    try {
      Object.keys(KEYS).forEach(function (k) { localStorage.removeItem(KEYS[k]); });
      localStorage.removeItem('avatarPanelRatio');
    } catch (_) { /* ignore */ }
  }

  function videoEl() { return document.getElementById('av-video'); }
  function layerEl() { return document.getElementById('av-layer'); }
  function positionFrame() {
    var stage = document.querySelector('#app > .stage');
    if (isDetail() && window.innerWidth > 860 && stage) {
      var sr = stage.getBoundingClientRect();
      if (sr.width > 0) return sr;
    }
    var l = layerEl();
    return l ? l.getBoundingClientRect() : { left: 0, width: window.innerWidth };
  }

  /* ------------------------------------------------------------ 几何内核 */

  /** 量【布局盒】—— 必须是 offsetLeft/offsetWidth，不是 getBoundingClientRect：
   *  后者把本文件自己写的 scale 也算进去，用它反解 scale 就会自我放大。
   *  （V1 的像素审计已经踩过这个坑，见 ui_layout_modes.js naturalPanePx 注释。）
   *  offsetParent 是 #av-layer（position:fixed;inset:0、无 padding/border），
   *  所以布局左缘的屏幕坐标 = layer 的 rect.left + video 的 offsetLeft。 */
  function layoutBox() {
    var v = videoEl();
    if (!v) return null;
    var l = layerEl();
    var lr = l ? l.getBoundingClientRect() : { left: 0, top: 0 };
    var bw = v.offsetWidth, bh = v.offsetHeight;
    if (!(bw > 0) || !(bh > 0)) return null;          // 流还没来，盒子塌成 0
    return {
      baseWidth: bw,
      baseHeight: bh,
      boxLeft: lr.left + v.offsetLeft,
      boxRight: lr.left + v.offsetLeft + bw,
      boxTop: lr.top + v.offsetTop,
    };
  }

  /** 当前 (s,w,x,y) 下人物带在屏幕上的真实矩形。
   *  translate 在矩阵最里层 ⇒ 平移量随缩放长大（见文件头的合成说明）。 */
  function geometry() {
    var b = layoutBox();
    if (!b) return null;
    var s = defaultMode ? 1 : state.s / 100;
    var w = defaultMode ? 1 : state.w / 100;
    var tyPx = defaultMode ? 0 : (state.y * OFFSET_FACTOR / 100) * b.baseHeight;
    var bandW = b.baseWidth * w * s;
    var bandH = b.baseHeight * s;
    // 锚点 = 盒右上（transform-origin:100% 0%）：右缘/顶缘是不动点，再叠平移。
    var fr = positionFrame();
    // 以可见画布而非视频自身百分比定位，缩放后仍能到达两端。
    var left = fr.left + ((defaultMode ? 0 : state.x) + 100) / 200 * (fr.width - bandW);
    var txPx = (left - (b.boxRight - bandW)) / (w * s);
    var top = b.boxTop + tyPx * s;
    var fadePx = FADE_REF * b.baseWidth;             // 屏幕等宽接缝
    return {
      defaultMode: defaultMode,
      baseWidth: b.baseWidth, baseHeight: b.baseHeight,
      boxRight: b.boxRight, boxTop: b.boxTop,
      scale: s, stretchX: w, translateXpx: txPx * w * s, translateYpx: tyPx * s,
      bandWidth: bandW, bandHeight: bandH,
      bandLeft: left, bandRight: left + bandW, bandTop: top, bandBottom: top + bandH,
      fadeWidth: fadePx,
      fadeStartScreen: left,
      fadeEndScreen: left + fadePx,
      viewport: { w: window.innerWidth || 0, h: window.innerHeight || 0 },
    };
  }

  /** 拖动反解：要让人物带左缘落在 pointerX，uniform scale 该是多少。
   *
   *  V2 §6 原式是 desiredVisualWidth = viewportWidth - pointerX。这里把
   *  viewportWidth 换成【人物带右缘】—— 实测原生右缘在 1981.2 而视口只有 1920
   *  （avatar-sync 的 right:-4% 让 61.2px 溢出屏外）。照字面用 1920 的话，
   *  第一次"抓住边界"就会把人物缩小 3%，破坏 §5"左拖=放大/右拖=缩小"的
   *  可逆性。两者几何同形，差别就是这段溢出；反解仍写成 §6 的形状：
   *      scale = (带右缘 - pointerX) / (baseWidth * stretchX * (1 - x*0.4/100))
   *  分母里的 x 项来自 translate 随缩放放大后对左缘的贡献。 */
  function scaleForBoundary(pointerX) {
    var b = layoutBox();
    if (!b) return null;
    var x = defaultMode ? 0 : state.x;
    var w = defaultMode ? 1 : state.w / 100;
    var fr = positionFrame();
    var t = (x + 100) / 200;
    // 左对齐时左边界固定，无法用该边界反解大小。
    if (t < 0.001) return state.s;
    var s = (fr.width - (pointerX - fr.left) / t) / (b.baseWidth * w);
    if (!Number.isFinite(s)) return null;
    return clamp(Math.round(s * 100) / 1, LIMITS.sMin, LIMITS.sMax);
  }

  /* --------------------------------------------------------------- mask */

  function isDetail() {
    return (document.body && document.body.dataset.viewMode) !== 'live-portrait';
  }

  /** 把"屏幕等宽接缝"换算回元素局部坐标（mask 在局部盒上绘制，再随 transform
   *  一起变换，所以这里除以屏幕带宽就是局部百分比）。 */
  function maskCss(g) {
    if (!g) return '';
    var pct = FADE_SHAPE.map(function (f) {
      return clamp((f * g.fadeWidth) / g.bandWidth * 100, 0, 100).toFixed(2);
    });
    // The original left fog lives on the video itself. Free positioning also
    // exposes its right/bottom edges: feather them locally, never a fixed overlay.
    return 'linear-gradient(90deg,' +
      ' rgba(0,0,0,0) ' + pct[0] + '%,' +
      ' rgba(0,0,0,.30) ' + pct[1] + '%,' +
      ' rgba(0,0,0,.85) ' + pct[2] + '%,' +
      ' #000 ' + pct[3] + '%, #000 88%, rgba(0,0,0,0) 100%),' +
      'linear-gradient(180deg,transparent 0%,#000 2%,#000 94%,transparent 100%)';
  }

  /* --------------------------------------------------------------- 应用 */

  var CLEARED = ['transform', 'transformOrigin', 'objectPosition', 'objectFit',
                 'left', 'width', 'height', 'right', 'maxWidth', 'maxHeight',
                   'maskImage', 'webkitMaskImage', 'maskComposite', 'webkitMaskComposite'];

  /** 把当前 state 落到 #av-video 的内联样式（实时预览，无需保存按钮）。
   *  只写 transform / transform-origin / mask —— 原生舞台几何与 fit 全部留给
   *  avatar-sync.js，本文件从不写 width/height/max-*（V2 §7：靠 transform 生长，
   *  超出交给 #av-layer overflow:hidden 裁切，禁止再加尺寸上限）。 */
  function apply() {
    var v = videoEl();
    if (!v) return;
    if (defaultMode) {
      CLEARED.forEach(function (p) { v.style[p] = ''; });
      if (!isDetail() || window.innerWidth <= 860) { notify(); return; }
    }
    var g = geometry();
    if (!g) return;
    v.style.transform = 'translate(' + g.translateXpx + 'px, ' + g.translateYpx + 'px)'
      + ' scale(' + g.scale + ') scaleX(' + g.stretchX + ')';
    v.style.transformOrigin = '100% 0%';   // 右上锚定：头顶不掉出画面，放大向内生长
    if (isDetail()) {
      var m = maskCss(geometry());
      v.style.maskImage = m;
      v.style.webkitMaskImage = m;
      v.style.maskComposite = 'intersect';
      v.style.webkitMaskComposite = 'source-in';
    } else {
      // LIVE：取景框里不允许渐隐（那是 ui_layout_modes.css 的 !important 负责的
      // 第二道保险）。内联写了 mask 会盖过非 !important 的规则，所以离开 DETAIL
      // 时必须把这两条收回来 —— V2 §22"DETAIL 参数不得污染 LIVE"。
      v.style.maskImage = '';
      v.style.webkitMaskImage = '';
      v.style.maskComposite = '';
      v.style.webkitMaskComposite = '';
    }
    notify();
  }

  function notify() {
    var g = geometry();
    for (var i = 0; i < subs.length; i++) {
      try { subs[i](state, g, defaultMode); } catch (_) { /* 订阅者不许拖垮 owner */ }
    }
  }

  function syncInputs() {
    if (inputs.s) inputs.s.value = String(state.s);
    if (inputs.w) inputs.w.value = String(state.w);
    if (inputs.x) inputs.x.value = String(state.x);
    if (inputs.y) inputs.y.value = String(state.y);
    syncOutputs();
    syncPresets();
  }

  /** 单个滑块的读数显示。§8：宽度这一档必须写成"100%（原始）"这种用户语言，
   *  不能露出 matrix X / scaleX / aspect correction 之类的实现词。 */
  function syncOutput(key) {
    var inp = inputs[key];
    if (!inp) return;
    var o = inp.parentElement && inp.parentElement.querySelector('output');
    if (!o) return;
    if (key === 's') o.textContent = defaultMode ? '默认' : state.s + '%';
    else if (key === 'w') o.textContent = state.w + '%' + (state.w === 100 ? '（原始）' : '');
    else o.textContent = (state[key] > 0 ? '+' : '') + state[key];
  }

  function syncOutputs() {
    ['s', 'w', 'x', 'y'].forEach(syncOutput);
  }

  function syncPresets() {
    if (!card) return;
    card.querySelectorAll('.av-layout-preset').forEach(function (b) {
      b.classList.toggle('active', !defaultMode && Number(b.dataset.scale) === state.s);
    });
  }

  /** 唯一的写入口：cap ∈ 'scale' | 'stretch' | 'x' | 'y'。
   *  ui_layout_modes.js 只通过它改状态，自己一律不碰 video.style。 */
  function set(cap, value, opts) {
    var quiet = !!(opts && opts.quiet);
    var v = num(value, NaN);
    if (!Number.isFinite(v)) return state;
    if (cap === 'scale') state.s = clamp(Math.round(v), LIMITS.sMin, LIMITS.sMax);
    else if (cap === 'stretch' || cap === 'w') state.w = clamp(Math.round(v), LIMITS.wMin, LIMITS.wMax);
    else if (cap === 'x' || cap === 'offsetX') state.x = clamp(Math.round(v), LIMITS.xMin, LIMITS.xMax);
    else if (cap === 'y' || cap === 'offsetY') state.y = clamp(Math.round(v), LIMITS.yMin, LIMITS.yMax);
    else return state;
    defaultMode = false;
    apply();
    syncInputs();
    if (!quiet) save();
    return state;
  }

  function setScale(s, opts) { return set('scale', s, opts); }
  function setStretch(w, opts) { return set('stretch', w, opts); }

  /** 拖动分隔线：把它换算成人物大小（V2 §5/§6 的唯一杠杆）。 */
  function setBoundary(pointerX, opts) {
    var s = scaleForBoundary(pointerX);
    if (s === null) return null;
    set('scale', s, opts);
    return s;
  }

  function resetToDefault() {
    defaultMode = true;
    clearSaved();
    apply();
    state.s = DEFAULTS.s; state.w = DEFAULTS.w; state.x = DEFAULTS.x; state.y = DEFAULTS.y;
    syncInputs();
  }

  /* ------------------------------------------------------------ UI 构建 */

  var FIELDS = [
    { key: 's', label: '人物大小', min: LIMITS.sMin, max: LIMITS.sMax, step: 5 },
    { key: 'w', label: '人物宽度', min: LIMITS.wMin, max: LIMITS.wMax, step: 5 },
    { key: 'x', label: '左右位置', min: LIMITS.xMin, max: LIMITS.xMax, step: 5 },
    { key: 'y', label: '上下位置', min: LIMITS.yMin, max: LIMITS.yMax, step: 5 },
  ];

  function buildCard() {
    if (suppressed || card) return;
    card = document.createElement('div');
    card.className = 'av-layout-card';
    card.id = 'av-layout-card';
    card.hidden = true;

    var head = document.createElement('div');
    head.className = 'av-layout-head';
    var title = document.createElement('span');
    title.className = 'av-layout-title';
    title.textContent = '画面布局';
    var close = document.createElement('button');
    close.className = 'av-layout-close';
    close.type = 'button';
    close.setAttribute('aria-label', '关闭画面布局');
    close.textContent = '✕';
    close.addEventListener('click', function () { card.hidden = true; });
    head.append(title, close);
    card.append(head);

    FIELDS.forEach(function (f) {
      var wrap = document.createElement('label');
      wrap.className = 'av-layout-field';
      var lab = document.createElement('span');
      lab.className = 'av-layout-field-label';
      lab.append(Object.assign(document.createElement('span'), { textContent: f.label }));
      lab.append(Object.assign(document.createElement('output'), { textContent: '' }));
      var input = document.createElement('input');
      input.type = 'range';
      input.min = String(f.min);
      input.max = String(f.max);
      input.step = String(f.step);
      input.dataset.key = f.key;
      input.setAttribute('aria-label', f.label);
      input.addEventListener('input', function () {
        // 四能力各自独立写自己那一个通道（§9：大小与宽度不得互相覆盖）。
        if (f.key === 's') setScale(Number(input.value));
        else if (f.key === 'w') setStretch(Number(input.value));
        else set(f.key, Number(input.value));
      });
      wrap.append(lab, input);
      card.append(wrap);
      inputs[f.key] = input;
    });

    var presets = document.createElement('div');
    presets.className = 'av-layout-presets';
    PRESETS.forEach(function (p) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'av-layout-preset';
      b.dataset.scale = String(p.s);
      b.textContent = p.name;
      b.addEventListener('click', function () { setScale(p.s); });
      presets.append(b);
    });
    card.append(presets);

    var reset = document.createElement('button');
    reset.type = 'button';
    reset.className = 'av-layout-reset';
    reset.textContent = '恢复默认布局';
    reset.addEventListener('click', resetToDefault);
    card.append(reset);

    var hint = document.createElement('p');
    hint.className = 'av-layout-hint';
    hint.textContent = '拖动即生效、自动保存。人物宽度 100% 就是原始比例。';
    card.append(hint);

    document.body.append(card);
  }

  function toggleCard() {
    if (suppressed) {
      // 面板由 ui_layout_modes.js 提供时，入口语义交给它（V2 §15/§16：只有一个入口）。
      var m = global.UiViewModes;
      if (m && m.toggle) m.toggle();
      return;
    }
    if (!card) buildCard();
    card.hidden = !card.hidden;
    if (!card.hidden) syncInputs();
  }

  function installEntry() {
    if (suppressed) return;
    var controls = document.querySelector('.gf-controls');
    if (!controls || document.getElementById('gf-layout-open')) return;
    var btn = document.createElement('button');
    btn.id = 'gf-layout-open';
    btn.type = 'button';
    btn.className = 'btn ghost gf-layout-open';
    btn.textContent = '画面布局';
    btn.addEventListener('click', toggleCard);
    // 「数字人」按钮之后，同一排。
    var avatarBtn = document.getElementById('gf-avatar-open');
    if (avatarBtn && avatarBtn.parentElement === controls) {
      avatarBtn.after(btn);
    } else {
      controls.append(btn);
    }
  }

  /** 由 ui_layout_modes.js 调用：合并成【单一】布局控制面板，本文件退成纯 owner。
   *  ?noviewmode=1（那个文件不存在/被关）时不会被调用，本文件维持 V1 自带的
   *  入口 + 卡片，回退路径逐字节不变。 */
  function suppressOwnControls() {
    suppressed = true;
    try {
      var b = document.getElementById('gf-layout-open');
      if (b && b.parentElement && !b.closest('.topbar')) b.parentElement.removeChild(b);
      if (card && card.parentElement) card.parentElement.removeChild(card);
    } catch (_) { /* ignore */ }
    card = null;
    inputs = {};
    return true;
  }

  /* ------------------------------------------------------------ 事件与启动 */

  function relayout() {
    // resize / 模式切换 / 流就绪 → 重画内联 mask（雾必须跟着人物，§11）。
    // 刻意同步调用：不引入 rAF、不引入定时器。
    if (defaultMode && (!isDetail() || window.innerWidth <= 860)) { notify(); return; }
    apply();
  }

  function boot() {
    load();
    installEntry();
    buildCard();
    apply();
    syncInputs();

    var v = videoEl();
    if (v) {
      // 流是晚到的：没流的时候 offsetWidth=300（UA 默认盒），量出来的几何全是错的。
      v.addEventListener('loadedmetadata', relayout);
    }
    // V2 §11：更新时机只有 slider input / pointermove / 模式切换 / resize /
    // loadedmetadata。这里只挂一次性监听，不起 rAF、不起定时器。
    window.addEventListener('resize', relayout);
    // body 上的 data-view-mode 由 ui_layout_modes.js 写；它切换时会显式调
    // onModeChange()，本文件不在 DETAIL/LIVE 之间做任何自动猜测。
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  // 调试 / 验收挂点（V1 的键全部保留，V2 的新能力是增量）
  global.AvatarLayout = {
    disabled: false,
    version: 2,
    state: state,
    limits: LIMITS,
    keys: KEYS,
    presets: PRESETS,
    isDefault: function () { return defaultMode; },
    isSuppressed: function () { return suppressed; },
    set: set,
    setScale: setScale,
    setStretch: setStretch,
    setBoundary: setBoundary,
    scaleForBoundary: scaleForBoundary,
    geometry: geometry,
    layoutBox: layoutBox,
    get: function () {
      return { s: state.s, w: state.w, x: state.x, y: state.y,
               defaultMode: defaultMode, geometry: geometry() };
    },
    subscribe: function (fn) {
      if (typeof fn === 'function') { subs.push(fn); return function () {
        var i = subs.indexOf(fn); if (i >= 0) subs.splice(i, 1); }; }
      return function () {};
    },
    onModeChange: relayout,
    persist: save,
    suppressOwnControls: suppressOwnControls,
    apply: apply,
    resetToDefault: resetToDefault,
    toggleCard: toggleCard,
  };
})(typeof window !== 'undefined' ? window : globalThis);
