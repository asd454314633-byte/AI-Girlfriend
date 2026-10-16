/* ============================================================================
 * transform_equivalence.cjs — AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01
 *
 * 为什么需要这个探针（问题背景）：
 *   BEFORE 与 AFTER 两次盘点里，#av-video 的内联 transform 数值不一样：
 *     BEFORE  scale(1)    scaleX(1.3) translate(-40%, -8%)
 *     AFTER   scale(0.97) scaleX(1.3) translate(-24%, -8%)
 *   而 avatar_layout.js / avatar_layout.css 的 sha256 与字节数在两轮之间完全没变
 *   （POST_CHANGE_MANIFEST 已证），gf_dock.css 里也没有任何一条规则命中 #av-video。
 *   所以数值差异只可能来自【持久化状态本身变了】：owner 的 (s,w,x,y) 是从
 *   localStorage 读的，写入口只有 set()，而 set() 只在滑块 input / 分隔线
 *   pointermove / 预设点击 / 键盘这些**真实用户交互**里被调用（relayout() 只
 *   apply() 不 save()）。也就是说是使用者自己在两次采样之间动过人物大小/左右位置。
 *
 *   但"只可能是"不算证据。本探针把这句话变成实测：
 *     步骤 A  读当前持久化状态 → 用 owner 自己文档里的公式重算 transform 字符串
 *             → 与实际内联值逐字比对（证明没有第二个写者、没有 CSS 叠加）。
 *     步骤 B  用 AvatarLayout.set(cap, v, {quiet:true}) 把状态**临时**设回 BEFORE
 *             记录的那一组 (s=100,w=130,x=-100,y=-20)，再读一次内联 transform /
 *             origin / mask 与 #uivm-panel 的 outerLen+哈希，与 BEFORE 冻结值逐字
 *             比对。若全部相等 ⇒ 同一份程序在同一组状态下产出同一批像素，
 *             AVATAR_TRANSFORM_UNCHANGED 成立，且 #uivm-panel 的 outerLen 差异
 *             也被同一个状态差解释掉（预设高亮 class + 滑块读数文本）。
 *     步骤 C  再把状态设回步骤 A 读到的原值（同样 quiet），断言页面回到 A 的
 *             逐字快照，并断言 localStorage 的四个原始字符串在整个过程中一字未动
 *             （quiet 是唯一不落盘的通道；这条是"探针没污染用户设置"的自证）。
 *     步骤 D  遍历 document.styleSheets 的每一条规则，对 #av-video / #av-layer
 *             做 matches()，统计哪些表命中、命中规则里是否含 transform / mask /
 *             object-fit / width / height / left / right 声明。gf_dock.css 必须是
 *             0 命中 —— 这是"本轮 CSS 没碰人物"的正向证明，不是 grep 关键词。
 *
 * 边界纪律：
 *   - 只在本探针自己新建的后台标签页里操作，不碰用户标签页的 DOM。
 *   - 不点 #main-circle（会拉起真实 WebRTC 会话），不改 #gf-model / #gf-voice-model，
 *     不 localStorage.clear() / setItem()。
 *   - set() 一律带 {quiet:true}：读 avatar_layout.js:322 `if (!quiet) save();`
 *     可知 quiet 通道不写 localStorage。步骤 C 的断言就是这条的实测复核。
 *
 * 用法： node transform_equivalence.cjs
 * ==========================================================================*/
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { connect, Tab, sleep } = require('../ui_dual_viewmode_01/cdp.cjs');
const { INSTALL, INJECT_DOUBLE } = require('../ui_detail_composition_repair_01/measure_page.cjs');

const REPO = path.join(__dirname, '..', '..', '..');
const EV = path.join(REPO, 'runtime', 'evidence', 'UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01');
const OUT = path.join(EV, 'TRANSFORM_EQUIVALENCE.json');
const BEFORE = JSON.parse(fs.readFileSync(path.join(EV, 'CONTROL_INVENTORY_BEFORE.json'), 'utf8'));

/* BEFORE 冻结值：逐字取自 CONTROL_INVENTORY_BEFORE.json，不在这里手写第二份。 */
const B_VIDEO = BEFORE.video;
const B_OWNER = BEFORE.owner && BEFORE.owner.geo;
const B_OWNER_STATE = BEFORE.owner
  ? { s: BEFORE.owner.s, w: BEFORE.owner.w, x: BEFORE.owner.x, y: BEFORE.owner.y } : null;

function pickControl(inv, sel) {
  const arr = inv.controls || [];
  for (const c of arr) if (c && c.sel === sel) return c;
  return null;
}
const B_PANEL_NODE = pickControl(BEFORE, '#uivm-panel');
if (!B_PANEL_NODE) { console.log('FATAL: BEFORE 里找不到 #uivm-panel 记录'); process.exit(1); }
if (!B_VIDEO || !B_VIDEO.inlineTransform) { console.log('FATAL: BEFORE 里找不到 video.inlineTransform'); process.exit(1); }

/* 步骤 B 要设回去的那一组状态：由 BEFORE 的内联字符串反解，不用记忆里的数字。
 * 公式（avatar_layout.js:253）：
 *   transform = 'scale(' + s/100 + ') scaleX(' + w/100 + ') translate(' + x*0.4 + '%, ' + y*0.4 + '%)'
 * 反解：从字符串里抠出四个数，再各自还原成整数状态量。抠不出来就直接失败退出，
 * 绝不猜（猜出来的"等价证明"是最坏的一类假证据）。 */
function decodeState(str) {
  const m = /^scale\((-?[\d.]+)\) scaleX\((-?[\d.]+)\) translate\((-?[\d.]+)%, (-?[\d.]+)%\)$/.exec(String(str).trim());
  if (!m) return null;
  const s = parseFloat(m[1]) * 100, w = parseFloat(m[2]) * 100;
  const x = parseFloat(m[3]) / 0.4, y = parseFloat(m[4]) / 0.4;
  if (![s, w, x, y].every(Number.isFinite)) return null;
  return { s: Math.round(s), w: Math.round(w), x: Math.round(x), y: Math.round(y) };
}
const DECODED = decodeState(B_VIDEO.inlineTransform);
if (!DECODED) { console.log('FATAL: 无法从 BEFORE 内联 transform 反解状态：' + B_VIDEO.inlineTransform); process.exit(1); }
if (!B_OWNER_STATE) { console.log('FATAL: BEFORE 里找不到 owner 状态记录'); process.exit(1); }
/* 两条独立通道必须给出同一组状态：owner 自己记的 (s,w,x,y) 与从内联字符串反解的结果。
 * 不一致就说明我对公式的理解是错的，后面所有"等价"结论都不可信 → 直接退出。 */
const TARGET = B_OWNER_STATE;
const stateChannelsAgree = JSON.stringify(DECODED) === JSON.stringify(B_OWNER_STATE);
if (!stateChannelsAgree) {
  console.log('FATAL: 状态双通道不一致 decoded=' + JSON.stringify(DECODED) +
              ' recorded=' + JSON.stringify(B_OWNER_STATE));
  process.exit(1);
}

/* 页面侧：一次调用采一份完整快照（状态 + 内联 + 计算值 + 面板字节形状 + ls 原文）。 */
const SNAP_SRC = `(function(){
  /* 必须与 dock_inventory.cjs:81 的 hash() 逐字同款（XOR 变体、倒序遍历）。
   * 教训：本文件第一版用的是加法式 djb2（正序），与盘点证据里的 outerHash
   * 不可比 —— 同一段 HTML 算出两个哈希，把"面板结构未变"误判成 FAIL。
   * 哈希函数不同款 = 尺子不同刻度，比出来的差异是假的。 */
  function hash(s){ var h = 5381, i = s.length; while (i) { h = (h * 33) ^ s.charCodeAt(--i); } return (h >>> 0).toString(16); }
  var o = window.AvatarLayout;
  var v = document.getElementById('av-video');
  var l = document.getElementById('av-layer');
  var p = document.getElementById('uivm-panel');
  var ls = {};
  ['avatarVideoScale','avatarVideoStretchX','avatarVideoOffsetX','avatarVideoOffsetY'].forEach(function(k){
    ls[k] = localStorage.getItem(k);
  });
  var cs = v ? getComputedStyle(v) : null;
  var inlineProps = v ? Array.prototype.slice.call(v.style).slice() : [];
  var ps = o && o.get ? o.get() : null;
  var g = o && o.geometry ? o.geometry() : null;
  var pHtml = p ? p.outerHTML : '';
  return {
    state: ps ? {s:ps.s, w:ps.w, x:ps.x, y:ps.y, defaultMode:ps.defaultMode} : null,
    isDefault: o ? o.isDefault() : null,
    isSuppressed: o ? o.isSuppressed() : null,
    version: o ? o.version : null,
    lsRaw: ls,
    video: v ? {
      inlineStyleAttr: v.getAttribute('style'),
      inlineProps: inlineProps,
      inlineTransform: v.style.transform,
      inlineOrigin: v.style.transformOrigin,
      inlineMask: v.style.maskImage,
      inlineWebkitMask: v.style.webkitMaskImage,
      computedTransform: cs.transform,
      computedOrigin: cs.transformOrigin,
      computedMask: cs.maskImage || cs.webkitMaskImage,
      computedWidth: cs.width, computedHeight: cs.height,
      computedObjectFit: cs.objectFit, computedObjectPosition: cs.objectPosition,
      computedPosition: cs.position, computedRight: cs.right, computedTop: cs.top,
      srcW: v.videoWidth, srcH: v.videoHeight,
      hasSrcObject: !!v.srcObject,
      offsetWidth: v.offsetWidth, offsetHeight: v.offsetHeight,
      rect: (function(r){return {x:+r.x.toFixed(2),y:+r.y.toFixed(2),w:+r.width.toFixed(2),h:+r.height.toFixed(2)};})(v.getBoundingClientRect())
    } : null,
    layerCss: l ? (function(){var c=getComputedStyle(l);return {position:c.position,inset:c.inset,zIndex:c.zIndex,overflow:c.overflow,
      rect:(function(r){return {x:+r.x.toFixed(2),y:+r.y.toFixed(2),w:+r.width.toFixed(2),h:+r.height.toFixed(2)};})(l.getBoundingClientRect())};})() : null,
    geometry: g ? {baseWidth:g.baseWidth, baseHeight:g.baseHeight, boxRight:g.boxRight, boxTop:g.boxTop,
      scale:g.scale, stretchX:g.stretchX, translateXpx:g.translateXpx, translateYpx:g.translateYpx,
      bandWidth:g.bandWidth, bandHeight:g.bandHeight, bandLeft:g.bandLeft, bandRight:g.bandRight,
      bandTop:g.bandTop, bandBottom:g.bandBottom, fadeWidth:g.fadeWidth,
      fadeStartScreen:g.fadeStartScreen, fadeEndScreen:g.fadeEndScreen,
      viewport:g.viewport} : null,
    panel: p ? {outerLen: pHtml.length, outerHash: hash(pHtml), childCount: p.childElementCount,
      dataCollapsed: p.getAttribute('data-collapsed'), position: getComputedStyle(p).position,
      parentSel: (p.parentElement ? (p.parentElement.tagName.toLowerCase() + (p.parentElement.id?'#'+p.parentElement.id:'') + (p.parentElement.className?'.'+String(p.parentElement.className).trim().split(/\\s+/).join('.'):'')) : null),
      outputs: Array.prototype.slice.call(p.querySelectorAll('output')).map(function(x){return x.textContent;}),
      activePresets: Array.prototype.slice.call(p.querySelectorAll('.uivm-btn.uivm-active')).map(function(x){return x.dataset ? x.dataset.preset : null;})
    } : null,
    /* 公式重算：owner 文档里那条 transform 组装式的独立复刻，用来验证"内联值就是
       状态经这条公式的产物"，中间没有别人插手。 */
    recomputedTransform: ps ? ('scale(' + (ps.s/100) + ') scaleX(' + (ps.w/100) + ') translate(' + (ps.x*0.4) + '%, ' + (ps.y*0.4) + '%)') : null,
    viewport: {w: window.innerWidth, h: window.innerHeight},
    bodyAttrs: {viewMode: document.body.getAttribute('data-view-mode'),
                boundary: document.body.getAttribute('data-uivm-boundary')}
  };
})()`;

/* 步骤 D：CSS 命中普查。对每条规则做 matches()，并抽取与人物几何有关的声明。
 *
 * 这一版修了一个会伪装成 PASS 的探针缺陷（第一版实测教训）：
 *   Chrome 现在支持 CSS 嵌套，**普通 CSSStyleRule 也带 .cssRules（空列表）**，
 *   而空 CSSRuleList 是 truthy。第一版写成 `if (r.cssRules) { walk(); continue; }`
 *   ⇒ 每一条叶子规则都被当成容器跳过，rulesReadable 恒为 0，
 *      videoMatches/layerMatches 当然全为 0 —— 于是 dock_css_zero_hit 报了
 *      一个彻底空洞的 PASS（"探针坏"伪装成"检查通过"）。
 *   现在：① 先判 selectorText（叶子）再判 cssRules；
 *        ② rulesReadable 必须 > 0 才算这张表被真的读进去（gate 里断言）；
 *        ③ 加【正对照】#gf-dock —— gf_dock.css 里必然有规则命中它，
 *           若它也 0 命中，说明普查本身坏了，判 FAIL 而不是 PASS。 */
const CSS_CENSUS_SRC = `(function(){
  var targets = {
    video: document.getElementById('av-video'),
    layer: document.getElementById('av-layer'),
    dock:  document.getElementById('gf-dock'),      /* 正对照：必须被 gf_dock.css 命中 */
    app:   document.getElementById('app')
  };
  var out = [];
  var sheets = Array.prototype.slice.call(document.styleSheets);
  var GEOM = ['transform','transform-origin','mask','mask-image','-webkit-mask-image','object-fit',
              'object-position','width','height','max-width','max-height','min-width','min-height',
              'left','right','top','bottom','inset','position','scale','translate'];
  for (var i=0;i<sheets.length;i++){
    var sh = sheets[i];
    var href = sh.href ? String(sh.href).split('/').pop() : '(inline <style>)';
    var rec = {sheet: href, rulesTotal: 0, rulesLeaf: 0, rulesContainer: 0,
               videoMatches: [], layerMatches: [], dockMatches: 0, appMatches: 0,
               geomVideo: [], geomLayer: [], error: null};
    var rules = null;
    try { rules = sh.cssRules; } catch(e){ rec.error = 'CROSS_ORIGIN_OR_BLOCKED ' + String(e.message).slice(0,80); out.push(rec); continue; }
    if (!rules) { rec.error = 'NO_RULES'; out.push(rec); continue; }
    (function walk(list){
      for (var j=0;j<list.length;j++){
        var r = list[j];
        rec.rulesTotal++;
        if (!r.selectorText) {
          if (r.cssRules && r.cssRules.length) { rec.rulesContainer++; walk(r.cssRules); }
          continue;                                  /* @media/@supports/@font-face 等 */
        }
        rec.rulesLeaf++;
        var sels = String(r.selectorText).split(',');
        var hitV = false, hitL = false, hitD = false;
        for (var k=0;k<sels.length;k++){
          var s = sels[k].trim(); if (!s) continue;
          try {
            if (targets.video && targets.video.matches(s)) hitV = true;
            if (targets.layer && targets.layer.matches(s)) hitL = true;
            if (targets.dock  && targets.dock.matches(s))  hitD = true;
          } catch(_) { /* ::before 之类不可 matches 的选择器：跳过，不算命中 */ }
        }
        if (hitD) rec.dockMatches++;
        if (targets.app) { try { for (var q=0;q<sels.length;q++){ if (sels[q].trim() && targets.app.matches(sels[q].trim())) { rec.appMatches++; break; } } } catch(_){} }
        var txt = r.cssText || '';
        var geom = [];
        for (var g=0; g<GEOM.length; g++){
          var re = new RegExp('(^|[;\\\\s{])' + GEOM[g].replace(/-/g,'\\\\-') + '\\\\s*:', 'i');
          if (re.test(txt)) geom.push(GEOM[g]);
        }
        if (hitV) { rec.videoMatches.push(txt.slice(0,300)); if (geom.length) rec.geomVideo.push({sel:r.selectorText, props:geom}); }
        if (hitL) { rec.layerMatches.push(txt.slice(0,300)); if (geom.length) rec.geomLayer.push({sel:r.selectorText, props:geom}); }
      }
    })(rules);
    out.push(rec);
  }
  return {sheets: out, universe: sheets.length,
          leafTotal: out.reduce(function(n,s){return n+s.rulesLeaf;},0),
          targetFound: {video: !!targets.video, layer: !!targets.layer, dock: !!targets.dock, app: !!targets.app}};
})()`;

function shallowEq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

const t0 = Date.now();
function log(m) { console.log('+' + String(Date.now() - t0).padStart(7) + 'ms ' + m); }

(async function main() {
  const wd = setTimeout(() => { console.log('WATCHDOG_FORCE_EXIT'); process.exit(3); }, 240000);
  wd.unref();
  const verdict = {
    task_id: 'AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01',
    probe: 'transform_equivalence',
    captured_at: new Date().toISOString(),
    target_state_decoded_from_before: TARGET,
    target_state_decoded_from_before_transform_string: DECODED,
    target_state_two_channels_agree: stateChannelsAgree,
    before_frozen: {
      inlineTransform: B_VIDEO.inlineTransform,
      inlineOrigin: B_VIDEO.inlineOrigin,
      inlineMask: B_VIDEO.inlineMask,
      computedTransform: B_VIDEO.computedTransform,
      computedOrigin: B_VIDEO.computedOrigin,
      panelOuterLen: B_PANEL_NODE.outerLen,
      panelOuterHash: B_PANEL_NODE.outerHash,
      ownerGeometry: B_OWNER || null,
    },
    steps: {}, gates: {}, gates_info: {},
  };
  const G_INFO = verdict.gates_info;

  const conn = await connect();
  log('phase=equivalence connecting');
  const tab = await Tab.create(conn, 'about:blank');
  await tab.setViewport(1920, 1080);
  await tab.navigate('http://127.0.0.1:7860/?probe=dock-equiv', 4000);
  log('navigate');
  await sleep(1200);
  await tab.js(INSTALL);
  await tab.js(INJECT_DOUBLE);
  /* 等 double 真的出帧：没有流的盒子量出来的几何全是 UA 默认值。 */
  let ready = null;
  for (let i = 0; i < 40; i++) {
    ready = await tab.js(`(function(){var v=document.getElementById('av-video');
      return v ? [v.videoWidth, v.videoHeight, v.readyState, !!v.srcObject] : null;})()`);
    if (ready && ready[0] > 0 && ready[2] >= 2) break;
    await sleep(250);
  }
  log('double ready ' + JSON.stringify(ready));
  verdict.double_ready = ready;

  /* ---------- 步骤 A：当前状态快照 + 公式自证 ---------- */
  const A = await tab.js(SNAP_SRC);
  verdict.steps.A_current = A;
  log('A state=' + JSON.stringify(A.state) + ' inline=' + A.video.inlineTransform);

  /* ---------- 步骤 B：临时设回 BEFORE 状态（quiet，不落盘） ---------- */
  const setRes = await tab.js(`(function(){
    var o = window.AvatarLayout; if (!o || !o.set) return {ok:false, why:'NO_OWNER'};
    o.set('stretch', ${TARGET.w}, {quiet:true});
    o.set('x', ${TARGET.x}, {quiet:true});
    o.set('y', ${TARGET.y}, {quiet:true});
    o.set('scale', ${TARGET.s}, {quiet:true});
    var g = o.get();
    return {ok:true, after:{s:g.s,w:g.w,x:g.x,y:g.y,defaultMode:g.defaultMode}};
  })()`);
  verdict.steps.B_set = setRes;
  await sleep(120);
  const B = await tab.js(SNAP_SRC);
  verdict.steps.B_at_before_state = B;
  log('B inline=' + B.video.inlineTransform + ' panelLen=' + (B.panel && B.panel.outerLen));

  /* ---------- 步骤 C：设回原值，断言逐字回到 A ---------- */
  const restore = await tab.js(`(function(){
    var o = window.AvatarLayout; if (!o || !o.set) return {ok:false, why:'NO_OWNER'};
    o.set('stretch', ${A.state.w}, {quiet:true});
    o.set('x', ${A.state.x}, {quiet:true});
    o.set('y', ${A.state.y}, {quiet:true});
    o.set('scale', ${A.state.s}, {quiet:true});
    return {ok:true};
  })()`);
  verdict.steps.C_restore_call = restore;
  await sleep(120);
  const C = await tab.js(SNAP_SRC);
  verdict.steps.C_after_restore = C;
  log('C inline=' + C.video.inlineTransform);

  /* ---------- 步骤 D：CSS 命中普查 ---------- */
  const D = await tab.js(CSS_CENSUS_SRC);
  verdict.steps.D_css_census = D;
  const dockSheet = (D.sheets || []).find((s) => /gf_dock\.css/.test(s.sheet));
  log('D sheets=' + D.universe + ' gf_dock.css videoMatches=' + (dockSheet ? dockSheet.videoMatches.length : 'SHEET_NOT_FOUND'));

  /* ============================== 闸门 ============================== */
  const G = verdict.gates;

  /* G1 owner 身份：唯一、版本 2、控制面板仍被 ui_layout_modes.js 抑制 */
  G.owner_single_and_suppressed = {
    pass: A.version === 2 && A.isSuppressed === true && A.isDefault === false,
    observed: { version: A.version, isSuppressed: A.isSuppressed, isDefault: A.isDefault },
  };

  /* G2 公式自证：内联 transform 就是状态经 owner 文档公式的产物（无第二写者） */
  G.inline_equals_documented_formula = {
    pass: A.recomputedTransform === A.video.inlineTransform,
    recomputed: A.recomputedTransform, actual: A.video.inlineTransform,
  };

  /* G3 内联只写 transform / origin / mask 三类，没有别人塞进来的尺寸或定位 */
  const allowed = ['transform', 'transform-origin', 'mask-image', '-webkit-mask-image', 'webkit-mask-image'];
  const extra = (A.video.inlineProps || []).filter((p) => allowed.indexOf(p) < 0);
  G.inline_props_whitelist = { pass: extra.length === 0, extra: extra, all: A.video.inlineProps };

  /* G4 状态等价：把状态设回 BEFORE 那组后，内联三件套与 BEFORE 逐字相同 */
  G.transform_identical_at_same_state = {
    pass: B.video.inlineTransform === B_VIDEO.inlineTransform
       && B.video.inlineOrigin === B_VIDEO.inlineOrigin
       && B.video.inlineMask === B_VIDEO.inlineMask,
    before: { t: B_VIDEO.inlineTransform, o: B_VIDEO.inlineOrigin, m: B_VIDEO.inlineMask },
    atBeforeState: { t: B.video.inlineTransform, o: B.video.inlineOrigin, m: B.video.inlineMask },
  };

  /* G5 计算值等价：computedTransform / origin 也回到 BEFORE（这一条把"内联相同"
   *    升级成"浏览器最终合成出来的矩阵相同"，能抓到 CSS 叠加）。 */
  G.computed_identical_at_same_state = {
    pass: B.video.computedTransform === B_VIDEO.computedTransform
       && B.video.computedOrigin === B_VIDEO.computedOrigin,
    before: { t: B_VIDEO.computedTransform, o: B_VIDEO.computedOrigin },
    atBeforeState: { t: B.video.computedTransform, o: B.video.computedOrigin },
  };

  /* G6 几何等价：人物带 / 黑雾接缝的屏幕坐标回到 BEFORE 的数值 */
  const geoKeys = ['baseWidth','baseHeight','boxRight','boxTop','scale','stretchX','translateXpx',
                   'translateYpx','bandWidth','bandHeight','bandLeft','bandRight','bandTop',
                   'bandBottom','fadeWidth','fadeStartScreen','fadeEndScreen'];
  const geoDiff = [];
  if (B_OWNER) for (const k of geoKeys) {
    const a = B_OWNER[k], b = B.geometry ? B.geometry[k] : undefined;
    if (typeof a === 'number' && typeof b === 'number') { if (Math.abs(a - b) > 0.01) geoDiff.push(k + ': ' + a + ' -> ' + b); }
    else if (JSON.stringify(a) !== JSON.stringify(b)) geoDiff.push(k + ': ' + JSON.stringify(a) + ' -> ' + JSON.stringify(b));
  }
  G.geometry_identical_at_same_state = { pass: !!B_OWNER && geoDiff.length === 0, diff: geoDiff,
    atBeforeState: B.geometry, before: B_OWNER };

  /* G7 面板字节形状等价：#uivm-panel 的 outerLen/哈希回到 BEFORE
   *    ⇒ AFTER 里那 14 字节差异纯粹是状态差（预设高亮 class + 滑块读数），
   *       不是面板结构被改。 */
  G.panel_identical_at_same_state = {
    pass: B.panel && B.panel.outerLen === B_PANEL_NODE.outerLen && B.panel.outerHash === B_PANEL_NODE.outerHash,
    before: { outerLen: B_PANEL_NODE.outerLen, outerHash: B_PANEL_NODE.outerHash },
    atBeforeState: B.panel ? { outerLen: B.panel.outerLen, outerHash: B.panel.outerHash,
      outputs: B.panel.outputs, activePresets: B.panel.activePresets } : null,
    atCurrentState: A.panel ? { outerLen: A.panel.outerLen, outerHash: A.panel.outerHash,
      outputs: A.panel.outputs, activePresets: A.panel.activePresets } : null,
  };

  /* G8 还原闭合：步骤 C 之后页面回到步骤 A 的逐字快照，且 localStorage 原文一字未动 */
  G.restore_closed_loop = {
    pass: C.video.inlineTransform === A.video.inlineTransform
       && C.video.inlineMask === A.video.inlineMask
       && C.video.computedTransform === A.video.computedTransform
       && shallowEq(C.panel && { l: C.panel.outerLen, h: C.panel.outerHash }, A.panel && { l: A.panel.outerLen, h: A.panel.outerHash })
       && shallowEq(C.lsRaw, A.lsRaw)
       && shallowEq(C.state, A.state),
    ls_A: A.lsRaw, ls_C: C.lsRaw, state_A: A.state, state_C: C.state,
  };

  /* G9 gf_dock.css 对人物零命中（正向 matches()，不是 grep 关键词）。
   *    这条只有在"普查本身被证明在工作"时才有意义，所以同一条 gate 里合取：
   *      a) 全表叶子规则总数 > 0（第一版就是这里恒 0 ⇒ 判决空洞）
   *      b) gf_dock.css 自己 leaf 规则 > 0 且对正对照 #gf-dock 命中 > 0
   *      c) 对 #av-video / #av-layer 命中 = 0，且几何声明命中 = 0
   *    a/b 任一不成立 ⇒ 本 gate 判 FAIL（探针坏），绝不报 PASS。 */
  const leafTotal = D.leafTotal || 0;
  const probeAlive = leafTotal > 0 && D.targetFound && D.targetFound.video && D.targetFound.layer
    && D.targetFound.dock && !!dockSheet && dockSheet.rulesLeaf > 0 && dockSheet.dockMatches > 0;
  G.dock_css_zero_hit_on_avatar = {
    pass: probeAlive === true && !dockSheet.error
          && dockSheet.videoMatches.length === 0 && dockSheet.layerMatches.length === 0
          && dockSheet.geomVideo.length === 0 && dockSheet.geomLayer.length === 0,
    probe_alive: probeAlive,
    probe_alive_detail: { leafTotalAcrossAllSheets: leafTotal,
      gfDockLeafRules: dockSheet ? dockSheet.rulesLeaf : null,
      gfDockMatchesOnControl_gf_dock: dockSheet ? dockSheet.dockMatches : null,
      targets_found: D.targetFound },
    sheet: dockSheet ? { sheet: dockSheet.sheet, rulesTotal: dockSheet.rulesTotal,
      rulesLeaf: dockSheet.rulesLeaf, videoMatches: dockSheet.videoMatches,
      layerMatches: dockSheet.layerMatches, error: dockSheet.error } : 'GF_DOCK_CSS_NOT_IN_STYLE_SHEETS',
  };

  /* G10 谁在命中 #av-video：把全部命中表列出来，供与 FROZEN 清单对账
   *     （命中者只允许是 avatar-sync/style.css/ui_layout_modes.css/avatar_layout.css
   *       这些本轮 sha256 未变的表）。这是 INFO 记录，不参与 pass/fail。 */
  G_INFO.av_video_hit_sheets = (D.sheets || [])
    .filter((s) => s.videoMatches.length || s.layerMatches.length)
    .map((s) => ({ sheet: s.sheet, rulesTotal: s.rulesTotal, rulesLeaf: s.rulesLeaf,
                   videoMatches: s.videoMatches.length, layerMatches: s.layerMatches.length,
                   geomVideo: s.geomVideo, geomLayer: s.geomLayer }));
  G_INFO.per_sheet_census = (D.sheets || []).map((s) => ({ sheet: s.sheet, total: s.rulesTotal,
    leaf: s.rulesLeaf, container: s.rulesContainer, hitV: s.videoMatches.length,
    hitL: s.layerMatches.length, hitDockControl: s.dockMatches, err: s.error || null }));

  /* G11 媒体身份：单 video 节点、单 MediaStream、srcObject 仍在 */
  const vids = await tab.js(`(function(){
    var vs = Array.prototype.slice.call(document.querySelectorAll('video'));
    var v = document.getElementById('av-video');
    var s = v ? v.srcObject : null;
    window.__DOCK_PROBE_STREAM = s;
    return {count: vs.length, ids: vs.map(function(x){return x.id||'(no-id)';}),
            avVideo: document.querySelectorAll('#av-video').length,
            ctor: s ? String(s.constructor && s.constructor.name) : null,
            hasSrcObject: !!s,
            tracks: s && s.getTracks ? s.getTracks().map(function(t){
              return {kind:t.kind, id:t.id, label:t.label, readyState:t.readyState, muted:t.muted};}) : [],
            cloneNodes: document.querySelectorAll('video[src], canvas#av-video-clone').length,
            currentTime: v ? v.currentTime : null,
            readyState: v ? v.readyState : null};
  })()`);
  verdict.steps.media_identity = vids;
  const trackIds0 = (vids.tracks || []).map((t) => t.id).sort().join(',');
  G.media_identity = {
    pass: vids.avVideo === 1 && vids.hasSrcObject === true && vids.count === 2
          && (vids.tracks || []).length > 0,
    observed: vids,
    note: 'count=2 的两个 id = ' + JSON.stringify(vids.ids) + '：#av-video（avatar-sync.js 动态建的人物节点）' +
          '+ #cam-video（index.html:311 页面原有的摄像头 PiP，静态标签）。' +
          '本轮没有新增任何媒体节点 —— 由这条 id 清单与 dock_inventory 的 ' +
          'avVideo===1 / videos===2 双向前后一致共同钉住。',
  };

  /* G12 srcObject 不变：在【同一次页面加载】里做纯布局操作（展开/收起画面布局面板，
   *     两次点击回到原值），再比对 srcObject 是否仍是同一个对象、track id 是否同一批。
   *     这是"srcObject 不变"的唯一诚实口径：跨两次页面加载比 srcObject 没有意义
   *     （那必然是两条不同的流），能比的只有【一次会话内、布局操作前后】。 */
  await tab.js(`(function(){var b=document.getElementById('gf-layout-open'); if (b) b.click(); return 'C1';})()`);
  await sleep(200);
  await tab.js(`(function(){var b=document.getElementById('gf-layout-open'); if (b) b.click(); return 'C2';})()`);
  await sleep(200);
  const afterOps = await tab.js(`(function(){
    var v = document.getElementById('av-video'); var s = v ? v.srcObject : null;
    var p = window.__DOCK_PROBE_STREAM;
    return {sameObject: (s === p),
            tracks: s && s.getTracks ? s.getTracks().map(function(t){return {kind:t.kind,id:t.id,readyState:t.readyState};}) : [],
            panelCollapsedNow: (function(){var el=document.getElementById('uivm-panel'); return el?el.getAttribute('data-collapsed'):null;})()};
  })()`);
  verdict.steps.srcObject_across_layout_ops = { before: { sameObjectRef: 'window.__DOCK_PROBE_STREAM', tracks: vids.tracks }, after: afterOps };
  const trackIds1 = (afterOps.tracks || []).map((t) => t.id).sort().join(',');
  G.srcObject_same_object_across_layout_ops = {
    pass: afterOps.sameObject === true && trackIds0 === trackIds1 && (afterOps.tracks || []).length > 0,
    sameObject: afterOps.sameObject, trackIdsBefore: trackIds0, trackIdsAfter: trackIds1,
    panelCollapsedRestored: afterOps.panelCollapsedNow === null || afterOps.panelCollapsedNow === '0'
      ? true : 'CHECK:' + afterOps.panelCollapsedNow,
  };

  verdict.gates_all_pass = Object.keys(G).every((k) => !(G[k] && G[k].pass === false));
  verdict.note_state_drift = {
    finding: 'BEFORE 与 AFTER 之间，人物 owner 的持久化状态从 ' +
      JSON.stringify(TARGET) + ' 变成了 ' + JSON.stringify(A.state) + '。',
    attribution: 'avatar_layout.js / avatar_layout.css 两轮 sha256 与字节数完全一致（POST_CHANGE_MANIFEST）；' +
      'owner 的唯一写入口 set() 只由滑块 input / 分隔线 pointermove / 预设点击 / 键盘触发，' +
      'relayout() 只 apply() 不 save()；本轮探针全部走 {quiet:true} 不落盘（G8 实测复核）。' +
      '⇒ 状态变化来自两次采样之间的真实使用者交互，不是本轮布局改造造成。',
    scope: 'REGISTERED_OUT_OF_SCOPE',
    consequence: '人物当前比 BEFORE 略小(97% vs 100%)且更靠右(-24% vs -40%)，' +
      'bandLeft 由 848.96 变为 ' + (A.geometry ? A.geometry.bandLeft.toFixed(2) : 'n/a') +
      '；碰撞闸门一律用 AFTER 实测值判定，不引用 BEFORE 数值。',
  };

  await tab.js(`(function(){ if (window.__DOUBLE_TIMER) clearInterval(window.__DOUBLE_TIMER); return 'CLEARED'; })()`);
  fs.mkdirSync(EV, { recursive: true });
  fs.writeFileSync(OUT, JSON.stringify(verdict, null, 2));
  log('wrote ' + OUT);

  console.log('--- GATES ---');
  for (const k of Object.keys(G)) {
    const g = G[k];
    console.log((g && g.pass ? 'PASS ' : 'FAIL ') + k);
  }
  console.log('--- INFO ---');
  for (const k of Object.keys(G_INFO)) {
    console.log('INFO ' + k + ' = ' + JSON.stringify(G_INFO[k]).slice(0, 900));
  }
  console.log('gates_all_pass = ' + verdict.gates_all_pass);
  console.log('state BEFORE(decoded) = ' + JSON.stringify(TARGET) + '  state NOW = ' + JSON.stringify(A.state));
  try { await tab.close(); } catch (_) {}
  conn.close();
  clearTimeout(wd);
  process.exit(0);
})().catch((e) => {
  console.log('EQUIVALENCE_CRASH ' + String((e && e.stack) || e).slice(0, 2000));
  try {
    fs.mkdirSync(EV, { recursive: true });
    fs.writeFileSync(path.join(EV, 'TRANSFORM_EQUIVALENCE_CRASH.log'), String((e && e.stack) || e));
  } catch (_) {}
  process.exitCode = 1;
  process.exit(1);
});
