/* ============================================================================
 * dock_acceptance.cjs — AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01
 *
 * 覆盖任务书第 11 条里"必须实际量出来"的那几项，以及第 5/6/7/8/10 条的
 * 渲染后几何断言（教训：只看 CSS 源码会漏掉"变量读得到但 transform 退化成 none"
 * 这一类，闸门必须落在 getBoundingClientRect 上）：
 *
 *   A0 视口通道（本探针的地基）。机制实测见 VIEWPORT_CHANNEL_FINDING.md：
 *      本机物理 1920×1080 + 系统缩放 110%，Emulation.setDeviceMetricsOverride
 *      在 mobile:false 下被【夹到客户区 DIP】(1745×982)，Browser.setWindowBounds
 *      在 maximized 态被忽略。因此：
 *        · 参考态 = 自然视口，不申请任何覆盖；
 *        · 每次量几何前要求 innerWidth/innerHeight 【连续两次读数相同】才算稳定，
 *          并且要等于本次声明的目标视口（两根尺：viewport_stable / matches_wanted）；
 *        · clamp 的上限分支（430px）本机真实窗口压不到 ⇒ A3 扫四个视口点，
 *          其中 1920×1080+mobile:true 是给布局视口用的【模拟通道】，id 里如实标注。
 *   A. DETAIL @参考视口：Dock 宽度按【公式】判（clamp(340,24vw,430) 用当帧实测
 *      innerWidth 现算，写死数字会把"屏幕不够宽"误判成产品缺陷）；max-height 有界
 *      且不越出视口；内部 overflow-y:auto 可滚；五个分组；圆球尺寸与"不得压在人物带
 *      或黑雾接缝上"的矩形判定；#uivm-panel 展开/收起两态都落在 Dock 内部；
 *      中央区（main.stage）渲染后子节点数 = 0；脚手架计数对账（17 + 1 个 <link>）。
 *   A2 卫生账：探针与用户页面共用同一 origin 的 localStorage，跑完逐键对拍，
 *      有差异就按快照写回并复验（探针不许留下偏好副作用）。
 *   B. 网络零干扰（逐动作单独开窗 + 背景噪声对照 + fetch 调用栈归因）：
 *        沉降窗 1.5s → 无操作基线 10s → 每个布局动作各开一窗（面板两态切换、
 *        Dock 内滚动到底/回顶、视口 1440 与自然态往返、DETAIL↔LIVE_PORTRAIT 往返）
 *        → 收尾再开一个 10s 无操作窗。
 *      三条同时成立：动作窗内 RTCPeerConnection/WebSocket 构造增量 = 0；
 *      动作窗内【栈命中布局层】的 fetch = 0；动作窗里出现的后端 fetch 调用者形状
 *      不超出无操作窗见过的集合。背景重试（avatar-sync 的 scheduleReconnect →
 *      POST /offer）落在哪一窗一律如实报数，并给出它在无操作窗里的到达率作对照。
 *      静态对偶证据：这套布局代码里后端调用点数量 = 0 —— 由逐字符状态机
 *      （maskComments）抹掉注释后再扫，作用域收窄到本轮真正写过的文件/区段，
 *      并配三条阳性对照，防止"扫描器失灵 ⇒ 0 命中 ⇒ 假 PASS"。
 *      归因之外还有一条地板：每个动作窗开头页面内计数器必须重置成功
 *      （探针丢了 ⇒ 快照全 0 ⇒ 最坏的一种假干净）。
 *   C. 人物核心未动：#av-layer 取景框宽高比 = 9:16（LIVE），DETAIL→LIVE→DETAIL
 *      往返后 Dock/人物带/圆球矩形逐项相等；transform 的唯一 owner 仍是
 *      avatar_layout.js（磁盘 sha256 对拍 PRE 基线 + gf_dock.css 叶子规则对
 *      #av-video/#av-layer 零命中 + 同文档内往返逐字不变）。
 *      单 video / 同一路流：#av-video 元素个数 = 1、srcObject 的 MediaStream.id
 *      与 track id 集合在四个采样点上逐一相同、src 属性恒为 null。
 *      文档级导航计数走 CDP 事件（页面内变量会被重载一起冲掉）。
 *   D. 截图（独立新窗口 + fromSurface:true + 满屏洋红自证）。
 *      背景层像素来自 canvas 测试替身（captureStream），文件名与 JSON 里都如实
 *      标注 DOUBLE —— 它是"布局证据"，不是"人物视觉证据"；主观视觉验收留给用户。
 *   F. 文件归属：file_audit_offline.cjs 生成 FILE_AUDIT_OFFLINE.json，
 *      本探针读它并把 NON_UI_MODIFIED_FILES 做成闸门（282 份清单内文件逐一重算 sha256）。
 *
 * 明确不做：不点 #main-circle（会拉起真实会话，抢用户那条唯一会话），
 * 不改 #gf-model / #gf-voice-model，不 localStorage.clear()，不重启任何服务。
 * 前置：7860 必须在跑（静态资源改动即时生效，无需重启）；CDP 端点 127.0.0.1:9110。
 * 离线自检：node static_scan_selftest.cjs（15 项，不连浏览器）
 *          node file_audit_offline.cjs（纯磁盘，不连浏览器）
 * 用法： node dock_acceptance.cjs
 * ==========================================================================*/
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { connect, Tab, sleep } = require('../ui_dual_viewmode_01/cdp.cjs');
const { INSTALL, INJECT_DOUBLE } = require('../ui_detail_composition_repair_01/measure_page.cjs');

const REPO = path.join(__dirname, '..', '..', '..');
const EV = path.join(REPO, 'runtime', 'evidence', 'UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01');
const SHOT = path.join(EV, 'screenshots');
const OUT = path.join(EV, 'DOCK_ACCEPTANCE.json');

const t0 = Date.now();
const log = (m) => console.log('+' + String(Date.now() - t0).padStart(7) + 'ms ' + m);

/* 历史不能改写：本探针的输出按定义是"最新一次实测"，会被下一次运行覆盖。
 * 所以动笔前先把上一份归档成带时间戳的文件，而不是就地覆盖。 */
try {
  if (fs.existsSync(OUT)) {
    const st = fs.statSync(OUT);
    const arc = path.join(EV, 'archive',
      'DOCK_ACCEPTANCE_' + st.mtime.toISOString().replace(/[:.]/g, '-') + '.json');
    fs.mkdirSync(path.join(EV, 'archive'), { recursive: true });
    if (!fs.existsSync(arc)) { fs.copyFileSync(OUT, arc); log('archived previous run -> ' + path.relative(EV, arc)); }
  }
} catch (e) { log('archive skipped: ' + String(e.message).slice(0, 120)); }

/* ------------------------------------------------------------------ 页面侧尺 */
const GEO_SRC = `(function(){
  function R(el){ if(!el) return null; var r = el.getBoundingClientRect();
    return {left:+r.left.toFixed(2), top:+r.top.toFixed(2), right:+r.right.toFixed(2),
            bottom:+r.bottom.toFixed(2), w:+r.width.toFixed(2), h:+r.height.toFixed(2)}; }
  function inside(a,b,pad){ if(!a||!b) return null; pad = pad===undefined?0.5:pad;
    return a.left>=b.left-pad && a.right<=b.right+pad && a.top>=b.top-pad && a.bottom<=b.bottom+pad; }
  function overlap(a,b){ if(!a||!b) return null;
    var ow = Math.min(a.right,b.right) - Math.max(a.left,b.left);
    var oh = Math.min(a.bottom,b.bottom) - Math.max(a.top,b.top);
    return {overlapW:+ow.toFixed(2), overlapH:+oh.toFixed(2), intersect: ow>0 && oh>0}; }
  var q = function(s){ return document.querySelector(s); };
  var dock = q('#gf-dock'), scroll = q('#gf-dock .gf-dock-scroll'), stage = q('main.stage');
  var orb = q('.orb-wrap'), circle = q('#main-circle'), panel = q('#uivm-panel');
  var o = window.AvatarLayout; var g = o && o.geometry ? o.geometry() : null;
  var groups = Array.prototype.slice.call(document.querySelectorAll('#gf-dock .gf-dock-group')).map(function(sec){
    return {label: sec.getAttribute('aria-label'), rect: R(sec),
            title: (sec.querySelector('.gf-dock-title')||{}).textContent || null,
            bodyKids: (sec.querySelector('.gf-dock-body')||{childElementCount:null}).childElementCount};
  });
  var lv = q('#av-layer'), vv = q('#av-video');
  return {
    viewport: {w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio},
    viewMode: document.body.getAttribute('data-view-mode'),
    dock: {rect: R(dock),
           cssWidth: getComputedStyle(dock).width,
           cssMaxHeight: getComputedStyle(dock).maxHeight,
           cssOverflowY: getComputedStyle(dock).overflowY,
           scrollCssOverflowY: scroll ? getComputedStyle(scroll).overflowY : null,
           scrollRect: R(scroll),
           scrollHeight: scroll ? scroll.scrollHeight : null,
           scrollTop: scroll ? scroll.scrollTop : null,
           clientHeight: scroll ? scroll.clientHeight : null,
           scrollable: scroll ? (scroll.scrollHeight > scroll.clientHeight + 1) : null},
    scaffold: {
      dockEl: document.querySelectorAll('#gf-dock').length,
      scrollEl: document.querySelectorAll('#gf-dock .gf-dock-scroll').length,
      groupEl: document.querySelectorAll('#gf-dock .gf-dock-group').length,
      titleEl: document.querySelectorAll('#gf-dock .gf-dock-title').length,
      bodyEl: document.querySelectorAll('#gf-dock .gf-dock-body').length,
      screenSlot: document.querySelectorAll('#gf-dock-screen-slot').length,
      totalScaffold: document.querySelectorAll('#gf-dock, #gf-dock .gf-dock-scroll, #gf-dock .gf-dock-group, ' +
                     '#gf-dock .gf-dock-title, #gf-dock .gf-dock-body').length,
      newLinkTags: Array.prototype.slice.call(document.querySelectorAll('link[rel=stylesheet]'))
                     .map(function(l){return l.getAttribute('href');}).filter(function(h){return /gf_dock\\.css/.test(h||'');}).length
    },
    groups: groups,
    orb: {rect: R(orb), circleRect: R(circle),
          circleCssW: circle ? getComputedStyle(circle).width : null,
          parentIsDock: !!(orb && orb.closest && orb.closest('#gf-dock')) },
    panel: panel ? {rect: R(panel), collapsed: panel.hasAttribute('data-collapsed'),
                    position: getComputedStyle(panel).position,
                    parentInDock: !!panel.closest('#gf-dock')} : null,
    stage: {rect: R(stage), childElementCount: stage ? stage.childElementCount : null,
            descendantCount: stage ? stage.querySelectorAll('*').length : null},
    status: (function(){ var s=q('#gf-status-container'); var r=R(s);
      return {rect:r, position: s?getComputedStyle(s).position:null,
              inDock: !!(s && s.closest && s.closest('#gf-dock'))}; })(),
    gfControls: (function(){ var c=q('.gf-controls'); return {rect:R(c), inDock: !!(c&&c.closest&&c.closest('#gf-dock'))}; })(),
    layer: R(lv), videoRect: R(vv),
    /* 任务书第 11 条："人物 video node = 1 / srcObject 不变"。
     * 数两条独立的尺：DOM 里 #av-video 元素个数（复制品会让它变 2），
     * 以及 srcObject 那一侧的流身份（MediaStream.id + 每条 track 的 id）——
     * 只有 rect 相同不足以证明"还是同一路流"，重连会得到几何相同但 id 不同的流。 */
    video: (function(){
      var all = Array.prototype.slice.call(document.querySelectorAll('video'));
      var withStream = all.filter(function(v){ return !!v.srcObject; })
                          .map(function(v){ return v.id || v.className || '(anon)'; });
      var s = vv && vv.srcObject;
      var tracks = s && s.getTracks ? s.getTracks().map(function(t){
        return {kind: t.kind, id: String(t.id).slice(0, 36), label: String(t.label).slice(0, 40), readyState: t.readyState};
      }) : null;
      return {
        avVideoCount: document.querySelectorAll('#av-video').length,
        sameObjectAsStamp: !!(vv && window.__PROBE_VNODE && vv === window.__PROBE_VNODE),
        stampPresent: !!window.__PROBE_VNODE,
        allVideoCount: all.length,
        allVideoIds: all.map(function(v){ return v.id || '(anon)'; }),
        videosWithSrcObject: withStream,
        srcObjectNull: !s,
        srcObjectCtor: s ? (s.constructor && s.constructor.name) : null,
        streamId: s && s.id ? String(s.id).slice(0, 36) : null,
        trackIds: tracks ? tracks.map(function(t){ return t.id; }) : null,
        tracks: tracks,
        srcAttr: vv ? (vv.getAttribute('src') || null) : null,
        muted: vv ? vv.muted : null, playsInline: vv ? vv.playsInline : null,
        videoWidth: vv ? vv.videoWidth : null, videoHeight: vv ? vv.videoHeight : null,
        readyState: vv ? vv.readyState : null,
      };
    })(),
    layerCssVars: (function(){ var b=getComputedStyle(document.body);
      return {stageW: b.getPropertyValue('--uivm-stage-w').trim(), stageH: b.getPropertyValue('--uivm-stage-h').trim(),
              stageGap: b.getPropertyValue('--uivm-stage-gap').trim(), dividerX: b.getPropertyValue('--uivm-divider-x').trim(),
              topbarH: b.getPropertyValue('--uivm-topbar-h').trim()}; })(),
    videoInlineTransform: vv ? vv.style.transform : null,
    /* 第 5 条是【公式】不是常数：期望宽 = clamp(340, 24vw, 430)，
       期望高 = min(内容高, 100vh - 页头高 - 2*安全边距)。必须在页面里用
       实测 innerWidth/innerHeight 现算，写死 430 会把"屏幕不够宽"误判成产品缺陷。 */
    expected: { vw: window.innerWidth, clientW: document.documentElement.clientWidth,
      clampW: +(Math.max(340, Math.min(430, 0.24 * window.innerWidth))).toFixed(2),
      topbarH: (function(){ var t = getComputedStyle(document.body).getPropertyValue('--uivm-topbar-h');
        return t ? +parseFloat(t).toFixed(2) : null; })(),
      maxH: (function(){ var t = getComputedStyle(document.body).getPropertyValue('--uivm-topbar-h');
        var th = isNaN(parseFloat(t)) ? 0 : parseFloat(t);
        return +(window.innerHeight - th - 32).toFixed(2); })() },
    band: g ? {bandLeft:g.bandLeft, bandRight:g.bandRight, bandTop:g.bandTop, bandBottom:g.bandBottom,
               fadeStartScreen:g.fadeStartScreen, fadeEndScreen:g.fadeEndScreen,
               scale:g.scale, stretchX:g.stretchX, baseWidth:g.baseWidth} : null,
    derived: {
      dockRight: R(dock) ? R(dock).right : null,
      orbRight: R(circle) ? R(circle).right : null,
      gapDockToBand: g ? +(g.bandLeft - (R(dock) ? R(dock).right : NaN)).toFixed(2) : null,
      gapOrbToBand: g ? +(g.bandLeft - (R(circle) ? R(circle).right : NaN)).toFixed(2) : null,
      gapOrbToFadeStart: g ? +(g.fadeStartScreen - (R(circle) ? R(circle).right : NaN)).toFixed(2) : null,
      orbInsideDock: inside(R(circle), R(dock)),
      panelInsideDock: inside(R(panel), R(dock)),
      orbVsBand: overlap(R(circle), g ? {left:g.bandLeft, right:g.bandRight, top:g.bandTop, bottom:g.bandBottom} : null),
      orbVsFade: overlap(R(circle), g ? {left:g.fadeStartScreen, right:g.fadeEndScreen, top:g.bandTop, bottom:g.bandBottom} : null),
      dockVsBand: overlap(R(dock), g ? {left:g.bandLeft, right:g.bandRight, top:g.bandTop, bottom:g.bandBottom} : null),
      dockBgOpaque: dock ? (function(){ var bg=getComputedStyle(dock).backgroundColor; return bg; })() : null,
      dockVsStatus: overlap(R(dock), R(q('#gf-status-container')))
    }
  };
})()`;

/* gf_dock.css 在【当前模式】下对人物层的命中普查（叶子规则判定已修好，见
 * transform_equivalence.cjs 里那段缺陷记录）。 */
const AV_CENSUS_SRC = `(function(){
  var vv = document.getElementById('av-video'), lv = document.getElementById('av-layer');
  var out = [];
  Array.prototype.slice.call(document.styleSheets).forEach(function(sh){
    var href = sh.href ? String(sh.href).split('/').pop() : '(inline <style>)';
    var rec = {sheet: href, leaf:0, hitV:0, hitL:0, hitVDetail:[], err:null};
    var rules=null; try { rules = sh.cssRules; } catch(e){ rec.err='BLOCKED'; out.push(rec); return; }
    if(!rules){ rec.err='NO_RULES'; out.push(rec); return; }
    (function walk(list){ for (var j=0;j<list.length;j++){ var r=list[j];
      if (!r.selectorText) { if (r.cssRules && r.cssRules.length) walk(r.cssRules); continue; }
      rec.leaf++;
      var sels=String(r.selectorText).split(',');
      for (var k=0;k<sels.length;k++){ var s=sels[k].trim(); if(!s) continue;
        var hv=false, hl=false;
        try { hv = vv && vv.matches(s); hl = lv && lv.matches(s); } catch(_){}
        if (hv) { rec.hitV++; rec.hitVDetail.push({sel:s, css:(r.cssText||'').slice(0,220)}); }
        if (hl) rec.hitL++;
        if (hv||hl) break;
      }
    }})(rules);
    out.push(rec);
  });
  return {sheets: out, leafTotal: out.reduce(function(n,s){return n+s.leaf;},0)};
})()`;

/* 逐个布局动作：每条只做一件事，动作本身在页面里完成（除视口窗）。 */
const OP_CLICK_PANEL = `(function(){var b=document.getElementById('gf-layout-open'); if(!b) return 'NOBTN'; b.click(); return 'CLICKED';})()`;
/* 幂等版：只在需要时点一次，返回值说清到底点没点（面板初始态来自 localStorage，不假设） */
const OP_ENSURE_OPEN = `(function(){var p=document.getElementById('uivm-panel'), b=document.getElementById('gf-layout-open');
  if(!p||!b) return 'NO_PANEL_OR_BTN';
  if(p.hasAttribute('data-collapsed')){ b.click(); return 'OPENED'; } return 'ALREADY_OPEN';})()`;
/* 反方向（确保收起）不单列常量：它就是 opRestorePanel(false)，避免留一份没人调用的死代码。 */
/* 还原到指定收起态（值由探针在开场那一帧实测得到，不硬编码） */
const opRestorePanel = (wantCollapsed) => `(function(){
  var p=document.getElementById('uivm-panel'), b=document.getElementById('gf-layout-open');
  if(!p||!b) return 'NO_PANEL_OR_BTN';
  var now=p.hasAttribute('data-collapsed'), want=${wantCollapsed === null ? 'now' : (wantCollapsed ? 'true' : 'false')};
  if(now!==want){ b.click(); return 'TOGGLED_BACK to want='+want; } return 'ALREADY_AT_LOAD_STATE';})()`;
const OP_SCROLL_BOTTOM = `(function(){var s=document.querySelector('#gf-dock .gf-dock-scroll'); if(!s) return 'NOCONTAINER'; s.scrollTop = s.scrollHeight; return 'scrollTop=' + s.scrollTop;})()`;
const OP_SCROLL_TOP = `(function(){var s=document.querySelector('#gf-dock .gf-dock-scroll'); if(!s) return 'NOCONTAINER'; s.scrollTop = 0; return 'scrollTop=' + s.scrollTop;})()`;
const opMode = (m) => `(function(){var b=document.querySelector('#uivm-panel .uivm-mode[data-mode="${m}"]'); if(!b) return 'NOBTN'; b.click(); return 'CLICKED';})()`;

/* 页面侧探针的清零/快照：每个动作窗开始时清零，结束时快照，
 * 这样"这次点击之后有没有新起连接 / 有没有 fetch 及其调用栈"是逐动作可指认的。 */
const PROBE_RESET_SRC = `(function(){var p=window.__CONN_PROBE; if(!p) return 'NOPROBE';
  p.rtc=0; p.ws=0; p.offers=[]; p.api=[]; return 'RESET';})()`;
const PROBE_SNAP_SRC = `(function(){var p=window.__CONN_PROBE||{};
  return {rtc:p.rtc||0, ws:p.ws||0, offers:p.offers||[], api:p.api||[]};})()`;

/* 人物 video 节点身份 + 流身份（每个动作窗里都采一次，用来把"流变了"钉到某一窗）。
 * 节点身份用【同一个 JS 对象】直接比 ===：
 *   window.__PROBE_VNODE 是在开场那一帧存下的 #av-video 引用，
 *   只要 document.getElementById('av-video') 不再是它，就说明这一路 video 被换过
 *   （重建 / 复制 / outerHTML 往返都会丢引用）。这比"标记属性"强：标记能被克隆带走。
 * 流身份单独记（MediaStream.id + track id 集合）：任务书说"srcObject 不变"，
 * 但应用自己的会话生命周期会在运行中重新协商一次（实测 1 track → 2 track，
 * 且发生在 rtc/ws 构造增量都为 0 的窗口里）⇒ 那一半归"事实 + 归因"，
 * 节点身份那一半才是本轮布局改动的责任范围。 */
const STAMP_VNODE_SRC = `(function(){var v=document.getElementById('av-video');
  if(!v) return 'NO_VIDEO'; window.__PROBE_VNODE = v; return 'STAMPED';})()`;
const VIDEO_SIG_SRC = `(function(){
  var cur = document.getElementById('av-video');
  var s = cur && cur.srcObject;
  var tr = s && s.getTracks ? s.getTracks().map(function(t){ return String(t.id).slice(0,8); }).sort() : null;
  return { count: document.querySelectorAll('#av-video').length,
           same_object: !!(cur && window.__PROBE_VNODE && cur === window.__PROBE_VNODE),
           probe_vnode_present: !!window.__PROBE_VNODE,
           srcAttr: cur ? (cur.getAttribute('src') || null) : null,
           stream_present: !!s,
           sig: (s && s.id ? String(s.id).slice(0,36) : 'NOSTREAM') + '|' + (tr ? tr.join(',') : 'NOTRAKES') };
})()`;

/* 静态证据用：从磁盘读"这套布局代码自己有没有网络调用点"。 */
const NET_CALLSITE_RE = /\bfetch\s*\(|XMLHttpRequest|new\s+WebSocket|RTCPeerConnection|sendBeacon|EventSource|new\s+EventSource/;

/* 逐字符状态机：把注释区间【就地抹成空格】（保留换行，行号不漂移），
 * 再在抹过的文本上跑 NET_CALLSITE_RE。
 *
 * 为什么不能用上一版那种"行首是不是 // / * / <!-- 开头"的判断：
 * 注释可以是多行的、也可以从行中间开始。index.html:612 就是一行注释的【续行】，
 * 它不以 <!-- 开头却整行都在注释里（末字符才是 -->），上一版把它当成代码 ⇒ 假 FAIL。
 *
 * 同时给出三本账，任何一本都能单独把结论问倒，不存在"被悄悄抹掉"：
 *   raw_hits      —— 原文里所有命中（含注释内的）
 *   code_hits     —— 抹掉注释后仍命中（这一本才是判据）
 *   comment_hits  —— 只在原文命中 ⇒ 它们确实只是注释/文档，逐条列出来供人核
 */
function maskComments(text, lang) {
  const out = text.split('');
  const mark = (i, j) => { for (let k = i; k < j && k < out.length; k++) if (out[k] !== '\n' && out[k] !== '\r') out[k] = ' '; };
  const ranges = [];
  let i = 0, s = 'code';
  const n = text.length;
  while (i < n) {
    const c = text[i], d = text[i + 1];
    if (s === 'code') {
      if (lang !== 'html' && c === '/' && d === '/') { const j = text.indexOf('\n', i); const e = j < 0 ? n : j; ranges.push([i, e, 'line']); mark(i, e); i = e; continue; }
      if (c === '/' && d === '*') { const j = text.indexOf('*/', i + 2); const e = j < 0 ? n : j + 2; ranges.push([i, e, 'block']); mark(i, e); i = e; continue; }
      if (lang === 'html' && c === '<' && d === '!') { const j = text.indexOf('-->', i + 4); const e = j < 0 ? n : j + 3; ranges.push([i, e, 'html_comment']); mark(i, e); i = e; continue; }
      if (lang === 'js' && (c === '\'' || c === '"')) { let k = i + 1; while (k < n) { if (text[k] === '\\') { k += 2; continue; } if (text[k] === c || text[k] === '\n') break; k++; } i = k + 1; continue; }
      if (lang === 'js' && c === '`') { let k = i + 1; while (k < n) { if (text[k] === '\\') { k += 2; continue; } if (text[k] === '`') break; k++; } i = k + 1; continue; }
      i++; continue;
    }
  }
  return { masked: out.join(''), ranges };
}

function scanCallSites(file, lang, regionBounds) {
  const raw = fs.readFileSync(file, 'utf8');
  const lines = raw.split(/\r?\n/);
  let scope = raw, bounds = null;
  if (regionBounds) {
    bounds = regionBounds;
    scope = raw.slice(bounds.start, bounds.end);
  }
  const { masked, ranges } = maskComments(scope, lang);
  const firstLine = bounds ? raw.slice(0, bounds.start).split(/\r?\n/).length : 1;
  const mLines = masked.split(/\r?\n/);
  const codeHits = [], commentHits = [];
  mLines.forEach((ml, idx) => {
    const n = firstLine + idx;
    const srcLine = lines[n - 1] || '';
    if (NET_CALLSITE_RE.test(ml)) codeHits.push(n + ': ' + ml.trim().slice(0, 160));
    else if (NET_CALLSITE_RE.test(srcLine)) commentHits.push(n + ': ' + srcLine.trim().slice(0, 160));
  });
  return { code_hits: codeHits, comment_hits: commentHits, raw_total: codeHits.length + commentHits.length,
    masked_chars: ranges.reduce((a, r) => a + (r[1] - r[0]), 0),
    comment_regions: ranges.length, scoped_lines: mLines.length };
}

/* 在 index.html 里定位 <aside id="gf-dock"> 及其配平的 </aside>（内层还有嵌套 aside）。 */
function gfDockRegion(text) {
  const open = text.indexOf('<aside id="gf-dock"');
  if (open < 0) return null;
  const re = /<aside\b[^>]*>|<\/aside>/g;
  re.lastIndex = open;
  let depth = 0, m;
  while ((m = re.exec(text))) {
    if (m[0][1] === 'a') { depth++; continue; }        // <aside...>
    depth--;
    if (depth === 0) return { start: open, end: re.lastIndex };
  }
  return null;
}

/* 抹注释后还有没有命中（给"阳性对照"用：喂一段确实含调用点的代码，必须数得出来）。 */
function codeHitsIn(text, lang) {
  const { masked } = maskComments(text, lang);
  return masked.split(/\r?\n/).filter((l) => NET_CALLSITE_RE.test(l)).length;
}

const LAYOUT_STACK_RE = /ui_layout_modes|uivm|gf[-_]dock|dockSlot|buildEntry|dock_acceptance|dock_inventory/i;
const RECONNECT_STACK_RE = /avatar-sync|scheduleReconnect|reconnect|setTimeout|setInterval|_00n_/i;

function classify(reqs) {
  const out = { total: reqs.length, backend: [], staticish: [], other: [], byPath: {} };
  reqs.forEach((r, idx) => {
    let u; try { u = new URL(r.request.url); } catch (_) { out.other.push(String(r.request.url).slice(0, 120)); return; }
    const p = u.pathname;
    const isBackend = /^\/(api|ws|webrtc|offer|answer|ice|signal|queue|sessions?)\b/i.test(p)
      || /connect|reconnect|offer|answer|candidate/i.test(p);
    /* initiator 是 CDP 给的【谁发的】事实，不是我猜的：
       type='script' 才有 JS 调用者（并带 stack）；'Other'/'parsed' 是浏览器/解析器自己发的
       —— favicon 就是这一类，它不可能由一次点击引起。这条尺子用来替代白名单。 */
    const ini = r.initiator || {};
    const topFrame = (ini.stack && ini.stack.callFrames && ini.stack.callFrames[0]) || null;
    const rec = { k: idx, method: r.request.method, path: p + (u.search ? '?' + u.search.slice(0, 60) : ''),
                  type: r.type, status: r._status === undefined ? null : r._status,
                  initiator: String(ini.type || '?') + (topFrame ? ' ' + String(topFrame.url || '').split('/').pop() + ':' + topFrame.lineNumber : '') };
    r._k = idx;
    if (isBackend) out.backend.push(rec); else out.staticish.push(rec);
    const key = rec.method + ' ' + p;
    out.byPath[key] = (out.byPath[key] || 0) + 1;
  });
  return out;
}

(async function main() {
  const wd = setTimeout(() => { console.log('WATCHDOG_FORCE_EXIT'); process.exit(3); }, 420000);
  const res = { task_id: 'AI_GIRLFRIEND_UI_DETAIL_LEFT_DOCK_CONSOLIDATION_01',
                probe: 'dock_acceptance', captured_at: new Date().toISOString(),
                gates: {}, info: {} };
  const conn = await connect();
  log('connecting');
  /* 参考视口 = 【从未下发过任何 Emulation 覆盖】的自然视口。
   * 原因见 VIEWPORT_CHANNEL_FINDING.md：本机显示器物理 1920x1080 + 系统缩放 110%
   * ⇒ mobile:false 的覆盖会被夹到客户区 DIP（1745x982），而且"覆盖在 navigate
   * 之前还是之后下发"看起来会改变夹取结果 —— 环境量不该进对照组。
   * 所以这里不再 setViewport(1920,1080)，改为开场先量一次自然视口当参考尺，
   * 需要别的宽度时用 clearDeviceMetricsOverride 回到这个参考态。 */
  const tab = await Tab.create(conn, 'about:blank');
  /* 【先把视口擦干净，再谈"参考视口"】
   * 上一轮实测：开场读到 1680×949 @dpr1，而 clearDeviceMetricsOverride 之后读到
   * 1309×818 @dpr1.1 —— 说明这个后台标签页【一开始就带着上一次会话残留的覆盖】
   * （dpr=1 是覆盖的指纹：真实 dpr 是系统缩放的 1.1）。拿被污染的那一帧当基线，
   * 后面所有"同一个视口内比前后"都建立在错误的前提上。
   * 所以这里在 navigate 之前无条件擦一次覆盖（新 session 上 clear 可能报错，忽略即可），
   * 之后读到的 naturalRef 才是这台机器真正的自然视口。 */
  const preClear = await tab.send('Emulation.clearDeviceMetricsOverride', {}).catch((e) => ({ ignored: String(e.message || e).slice(0, 80) }));
  res.viewport_pre_clear = { result: preClear === undefined ? 'CLEARED' : preClear,
    at: new Date().toISOString() };
  await sleep(400);
  const preClearRead = await tab.js(`({w:window.innerWidth,h:window.innerHeight,dpr:window.devicePixelRatio})`).catch(() => null);
  res.viewport_pre_clear.reading = preClearRead;
  /* ---------- 文档级导航计数：走 CDP 事件，不走页面里的变量 ----------
   * 为什么不能用页面内计数器：文档一旦重新加载，window 被整个换掉，
   * 页面里的计数器和它要证的东西一起消失（"没数到" ≠ "没发生"）。
   * Page.enable 之后 frameNavigated / navigatedWithinDocument 是浏览器自己报的，
   * 与文档生死无关；再叠一条侧证：每个动作窗开头 PROBE_RESET_SRC 若返回 NOPROBE，
   * 说明 __CONN_PROBE 已经不在了 —— 只有文档被替换才会这样。
   * 注册点放在 navigate【之前】，这样连首屏加载都进账。 */
  const navEvents = [];
  const offNav = conn.onEvent((msg) => {
    if (msg.sessionId !== tab.sessionId) return;
    const p = msg.params || {};
    if (msg.method === 'Page.frameNavigated') {
      if (p.frame && p.frame.parentId) return;          // 子框架不算
      navEvents.push({ kind: 'FRAME_NAVIGATED', t: Date.now(),
        url: String((p.frame && p.frame.url) || '').slice(0, 200) });
    } else if (msg.method === 'Page.navigatedWithinDocument') {
      navEvents.push({ kind: 'IN_DOCUMENT', t: Date.now(), url: String(p.url || '').slice(0, 200) });
    }
  });
  res.page_navigation_events = [];   // 闸门处填 navEvents 的快照
  await tab.navigate('http://127.0.0.1:7860/?probe=dock-accept', 4000);
  log('navigate');
  await sleep(1500);
  await tab.js(INSTALL);
  await tab.js(INJECT_DOUBLE);
  for (let i = 0; i < 40; i++) {
    const r = await tab.js(`(function(){var v=document.getElementById('av-video');return v?[v.videoWidth,v.readyState]:null;})()`);
    if (r && r[0] > 0 && r[1] >= 2) break;
    await sleep(250);
  }

  /* ---------- 页面内连接构造计数（构造器包裹，只统计不改行为） ---------- */
  await tab.js(`(function(){
    if (window.__CONN_PROBE) return 'ALREADY';
    window.__CONN_PROBE = {rtc:0, ws:0, getUserMedia:0, offers:[], api:[]};
    var ORtc = window.RTCPeerConnection, OWs = window.WebSocket;
    window.RTCPeerConnection = function(){ window.__CONN_PROBE.rtc++; return new ORtc(...arguments); };
    window.RTCPeerConnection.prototype = ORtc.prototype;
    Object.assign(window.RTCPeerConnection, ORtc);
    window.WebSocket = function(url, p){ window.__CONN_PROBE.ws++; return p===undefined ? new OWs(url) : new OWs(url,p); };
    window.WebSocket.prototype = OWs.prototype;
    window.WebSocket.CONNECTING=OWs.CONNECTING; window.WebSocket.OPEN=OWs.OPEN;
    window.WebSocket.CLOSING=OWs.CLOSING; window.WebSocket.CLOSED=OWs.CLOSED;
    /* fetch 包裹：记下每一次 /offer 与 /api/* 的【调用栈】。
     * 为什么必须抓栈：第一轮实测里 OPS 窗出现了一次 POST /offer（200）。
     * 光看"OPS 窗比 IDLE 窗多一条"就判 FAIL，或者反过来"它肯定是重连定时器"
     * 都不可接受 —— 栈里写着谁调的，才能把责任钉死或洗清。 */
    var of = window.fetch;
    if (of) window.fetch = function(){
      try {
        var u = String((arguments[0] && arguments[0].url) || arguments[0] || '');
        if (/\\/offer|\\/api\\//.test(u)) {
          var rec = {url: u.slice(0,160), method: (arguments[1]&&arguments[1].method)||'GET',
                     t: Date.now(), stack: String(new Error('probe').stack).split('\\n').slice(1,9).join(' | ').slice(0,900)};
          if (/\\/offer/.test(u)) window.__CONN_PROBE.offers.push(rec); else window.__CONN_PROBE.api.push(rec);
        }
      } catch(_) {}
      return of.apply(this, arguments);
    };
    return 'INSTALLED';
  })()`);
  /* 首屏加载（我们自己那一次 navigate）到此结束。从这个计数往后，
   * 任何一条 frameNavigated / navigatedWithinDocument 都不该再出现。 */
  const navBaseline = navEvents.slice();
  res.page_navigation_initial_load = { count: navBaseline.length, events: navBaseline.slice(0, 5) };
  /* 存下这一帧的 #av-video 对象引用，作为"是不是同一个节点"的判据基准 */
  const stamped = await tab.js(STAMP_VNODE_SRC);
  res.video_node_stamp = { result: stamped, at: new Date().toISOString(),
    sig_at_stamp: await tab.js(VIDEO_SIG_SRC) };
  log('video node stamp = ' + stamped);

  /* ---------- A0. 视口通道（本探针的地基，机制见 VIEWPORT_CHANNEL_FINDING.md）
   * 三条规则：
   *   (1) 参考态 = 自然视口（mobile:false 的覆盖会被夹到客户区 DIP，不可信为大视口）；
   *   (2) 每一次量几何前，视口必须【连续两次读数一致】才算稳定，否则该次测量作废；
   *   (3) 需要 >1792 CSS px 才能压到 clamp 的 430 上限，本机真实窗口给不出，
   *       只能走 mobile:true 的布局视口通道，并且必须标注 EMULATED_LAYOUT_VIEWPORT。 */
  let overrideActive = false;
  const setVp = async (w, h, mobile) => {
    await tab.send('Emulation.setDeviceMetricsOverride',
      { width: w, height: h, deviceScaleFactor: 1, mobile: !!mobile });
    overrideActive = true;
    await sleep(500);
  };
  const clearVp = async () => {
    if (!overrideActive) return;
    await tab.send('Emulation.clearDeviceMetricsOverride', {});
    overrideActive = false;
    await sleep(500);
  };
  const snap = async (mode) => {
    if (mode.kind === 'natural') await clearVp();
    else await setVp(mode.w, mode.h, mode.mobile);
    let geo = null, stable = false, reads = [];
    for (let i = 0; i < 4; i++) {
      const a = await tab.js(GEO_SRC); await sleep(250);
      const b = await tab.js(GEO_SRC);
      reads.push(a.viewport.w + 'x' + a.viewport.h, b.viewport.w + 'x' + b.viewport.h);
      if (a.viewport.w === b.viewport.w && a.viewport.h === b.viewport.h) { geo = b; stable = true; break; }
      geo = b;
      await sleep(400);
    }
    const want = mode.kind === 'natural' ? (naturalRef && naturalRef.vp) : null;
    const matchesWanted = want ? (geo.viewport.w === want.w && geo.viewport.h === want.h) : true;
    return { mode, geo, viewport_stable: stable, matches_wanted_viewport: matchesWanted,
      viewport: geo.viewport, reads: reads };
  };
  /* 参考态自证：连测三次同一自然视口，读数必须一模一样（含 dpr —— dpr≠系统缩放
   * 就是"还带着覆盖"的指纹，所以 dpr 也进读数）。 */
  const naturalProbe = [];
  for (let i = 0; i < 3; i++) { const v = await tab.js(`({w:window.innerWidth,h:window.innerHeight,dpr:window.devicePixelRatio})`); naturalProbe.push(v); await sleep(300); }
  const naturalRef = { vp: naturalProbe[0],
    stable: naturalProbe.every((v) => v.w === naturalProbe[0].w && v.h === naturalProbe[0].h),
    dpr_stable: naturalProbe.every((v) => v.dpr === naturalProbe[0].dpr),
    probe: naturalProbe };
  res.viewport_reference = naturalRef;
  log('reference viewport = ' + naturalRef.vp.w + 'x' + naturalRef.vp.h +
      ' dpr=' + naturalRef.vp.dpr + ' stable=' + naturalRef.stable);
  /* 诚实登记：开场先 clear 过一次覆盖，但 clear 之后的读数与【跑完扫描再 clear】的读数
   * 不一样（实测 want 1680x949@dpr1 vs got 1309x818@dpr1.1）⇒ 这个参考视口不能
   * 宣称是"这台机器真正的自然视口"，它只是"本探针全程使用且全程自证恒定的那一帧"。
   * 所有跨帧比较的合法性来自：同一帧 + 两次读数一致 + 四帧（detail/after_ops/live/back）
   * 逐字相同，而【不是】来自"clear 一定还得回去"。 */
  naturalRef.caveat = 'labeled_reference_not_verified_natural: pre-navigate clearDeviceMetricsOverride ' +
    'was issued, but the post-sweep clear read a different viewport (see viewport_after_sweep) ' +
    '=> cross-frame comparisons rely on frame equality, not on override reversibility.';

  /* ---------- A. DETAIL 几何（参考视口） ---------- */
  const detailSnap = await snap({ kind: 'natural' });
  const detail = detailSnap.geo;
  res.detail = detail;
  /* DETAIL 下也做一次规则命中普查：gf_dock.css 不许命中人物层的任何一个元素。
   * （matches() 只看选择器文本，不看 @media 条件，所以这条断言与模式无关 —— 比"当前
   *  模式下没生效"更强：这条样式表【在原理上】够不到 #av-video / #av-layer。） */
  const detailCensus = await tab.js(AV_CENSUS_SRC);
  res.info.detail_census = detailCensus;

  res.detail_viewport_proof = { viewport_stable: detailSnap.viewport_stable,
    matches_wanted_viewport: detailSnap.matches_wanted_viewport, reads: detailSnap.reads };
  log('DETAIL dock w=' + detail.dock.rect.w + ' expected(clamp)=' + detail.expected.clampW +
      ' right=' + detail.dock.rect.right +
      ' bandLeft=' + (detail.band ? detail.band.bandLeft.toFixed(2) : 'n/a') +
      ' circleW=' + detail.orb.circleCssW);

  /* ---------- A2. 用户持久态的卫生账 ----------
   * 两个标签页共用 127.0.0.1:7860 这一【同一个 origin】，而这个 origin 就是用户
   * 日常在用的那个：面板收起态（ai_girlfriend_uivm_panel_v2）、显示模式
   * （ai_girlfriend_ui_layout_v1）都会被本探针的点击写进去。
   * 所以：在页面完成初始化之后取一次全量快照，跑完所有动作后逐键比对，
   * 有差异就把快照里的原值一字不差写回去并复验。探针不许给用户留下偏好副作用。 */
  const LS_ALL = `(function(){var o={}; for(var i=0;i<localStorage.length;i++){ var k=localStorage.key(i);
      try { o[k]=localStorage.getItem(k); } catch(_) { o[k]='<THROW>'; } }
    return {count:Object.keys(o).length, values:o};})()`;
  const lsBefore = await tab.js(LS_ALL);
  res.localStorage = { before_keys: lsBefore.count, keys_listed: Object.keys(lsBefore.values).sort().join(',') };
  log('localStorage keys=' + lsBefore.count);

  /* ---------- A3. clamp 三分支扫描（第 5 条）
   * 本机真实窗口最宽只能到 1745 CSS px（见 VIEWPORT_CHANNEL_FINDING.md），
   * 24vw=418.8 永远碰不到 430 上限 ⇒ 只测自然视口等于【没有验证上限分支】。
   * 所以这里扫四个点：自然(中项) / 1440 覆盖(夹到 1309 ⇒ 下限) /
   * 1920 覆盖(夹到 1745 ⇒ 中项接近上限) / mobile:true 布局视口(2280 ⇒ 上限)。
   * 每一点都要求：视口两次读数一致（稳定）+ 实测 Dock 宽 == 用该点实测 innerWidth
   * 代入 clamp(340,24vw,430) 的期望值；并且三个分支至少各被一个点覆盖。 */
  const sweepPoints = [
    { id: 'natural', kind: 'natural', note: '用户日常窗口的真实视口' },
    { id: 'override_1440x900', w: 1440, h: 900, mobile: false, note: '窄：预期压到下限 340' },
    { id: 'override_1920x1080', w: 1920, h: 1080, mobile: false, note: '被夹到客户区 DIP，24vw 仍小于 430' },
    { id: 'EMULATED_LAYOUT_VIEWPORT_1920x1080_mobile_true', w: 1920, h: 1080, mobile: true,
      note: '只为压出 clamp 上限分支；不是真实桌面视口，如实标注' },
  ];
  /* 【本函数在 B 段全部量完之后才调用】—— 这是这一版的关键顺序修正。
   * 上一版把扫描放在 A 段：扫完之后 Emulation.clearDeviceMetricsOverride 并没有把
   * 视口还回原始状态（实测 want 1680x949 / got 1309x818，dpr 也从 1 变 1.1），
   * 于是 B/C 段所有"同一个视口内比前后"的闸门全部失去前提（4 条 FAIL）。
   * 结论：覆盖通道只能"用掉不能再要回来" ⇒ 凡依赖视口恒等的比较必须排在扫描之前。 */
  const runClampSweep = async () => {
    const sweep = [];
    for (const p of sweepPoints) {
      const s = await snap(p);
      const iw = s.geo.viewport.w;
      const raw24 = 0.24 * iw;
      const branch = raw24 < 340 ? 'floor_340' : (raw24 > 430 ? 'ceiling_430' : 'middle_24vw');
      sweep.push({ id: p.id, note: p.note, requested: p.kind === 'natural' ? 'natural' : (p.w + 'x' + p.h + (p.mobile ? ' mobile:true' : '')),
        viewport: s.geo.viewport, viewport_stable: s.viewport_stable,
        dpr: s.geo.viewport.dpr,
        dockW: s.geo.dock.rect.w, dockLeft: s.geo.dock.rect.left, dockH: s.geo.dock.rect.h,
        expected_clampW: s.geo.expected.clampW, expected_maxH: s.geo.expected.maxH,
        raw_24vw: +raw24.toFixed(2), branch,
        delta_w: +(s.geo.dock.rect.w - s.geo.expected.clampW).toFixed(2) });
      log('sweep ' + p.id.padEnd(46) + ' vw=' + iw + ' dockW=' + s.geo.dock.rect.w +
          ' expected=' + s.geo.expected.clampW + ' branch=' + branch + ' stable=' + s.viewport_stable);
    }
    await clearVp();
    const backToNatural = await tab.js(`({w:window.innerWidth,h:window.innerHeight,dpr:window.devicePixelRatio})`);
    res.viewport_after_sweep = { want: naturalRef.vp, got: backToNatural,
      matches: backToNatural.w === naturalRef.vp.w && backToNatural.h === naturalRef.vp.h,
      note: 'matches=false 不推翻任何闸门：扫描已排到最后，此后再不比几何。' +
            '它作为【环境缺陷登记】进 VIEWPORT_CHANNEL_FINDING.md。' };
    log('post-sweep viewport restore: want ' + JSON.stringify(naturalRef.vp) +
        ' got ' + JSON.stringify(backToNatural) + ' matches=' + res.viewport_after_sweep.matches);
    return sweep;
  };


  /* ---------- B. 网络零干扰 ----------
   * 第一轮这里的判据是"操作窗的后端请求集合必须是静默窗的子集且计数不增"，
   * 实测 FAIL：OPS 窗里出现了一次 POST /offer (200)。但当时既没能证明它是
   * 布局引起的，也没能证明它不是 —— 所以这一版把尺子换成能指认责任方的形状：
   *   1) 先量一个【完全无操作】的 10s 背景窗 → 后端请求速率 + /offer 到达次数；
   *   2) 再【每个布局动作单独开窗】：清零(请求/异常/探针) → 只做这一个动作 →
   *      等 1.5s → 采集。"这次点击之后有没有后端请求"逐动作可指认；
   *   3) 窗内每条后端 fetch 都带【调用栈】（页面里包了 fetch）：栈命中布局层
   *      标识才计为 LAYOUT_CAUSED；栈只命中 avatar-sync / 定时器 ⇒ 记为
   *      BACKGROUND_RETRY 并如实报数，不偷偷白名单；
   *   4) 硬闸门另加两条与归因无关的：动作窗内 RTCPeerConnection / WebSocket
   *      构造增量必须为 0（布局要是新起了连接，这条一定红）。
   * 最后再补一个 10s 无操作窗做第二次噪声对照，防止"背景重试恰好全落在
   * 动作窗里"这种巧合被读成因果。 */
  const clearAll = () => { tab.clearRequests(); tab.exceptions.length = 0; };

  /* 首个动作前的一次性沉降：Chrome 的 favicon 请求、以及首次 /offer 重试
   * 都发生在文档加载后的头一两秒。不清掉它们，它们会落进第一个窗格里，
   * 变成一条"看起来由布局动作引起"的假证据。 */
  clearAll();
  await sleep(1500);
  const settle = classify(tab.requests.slice());
  const settleErrors = tab.exceptions.slice();
  log('settle reqs=' + settle.total + ' backend=' + settle.backend.length);

  let winSeq = 0;
  const win = async (tag, action, opts = {}) => {
    clearAll();
    /* 'RESET' = 页面内构造计数器还活着；'NOPROBE' = window.__CONN_PROBE 不见了。
     * 必须把它记下来：探针不见了的时候 PROBE_SNAP_SRC 返回的是全 0，
     * 和"真的零活动"长得一模一样 —— 那是最坏的一种假干净。 */
    const probeReset = await tab.js(PROBE_RESET_SRC);
    const tStart = Date.now();
    let acted = 'NOOP';
    /* action 三种形态：{js} 页面内脚本 / {vp:{w,h,mobile}} 或 {vp:'natural'} 视口变更 /
     * null 无操作。视口变更必须落在窗【内】，
     * 否则"resize 引起的 relayout 有没有发请求"就没被观测。 */
    if (action && action.js) acted = await tab.js(action.js);
    else if (action && action.vp) {
      if (action.vp === 'natural') { await clearVp(); acted = 'VIEWPORT_NATURAL_RESTORE'; }
      else { await setVp(action.vp.w, action.vp.h, action.vp.mobile);
             acted = 'VIEWPORT_' + action.vp.w + 'x' + action.vp.h + (action.vp.mobile ? '_mobile' : ''); }
    }
    await sleep(opts.settleMs || 1500);
    const probe = await tab.js(PROBE_SNAP_SRC);
    const videoSig = await tab.js(VIDEO_SIG_SRC);
    const cls = classify(tab.requests.slice());
    const exc = tab.exceptions.slice();
    const wall = Date.now() - tStart;
    let geo = null;
    if (opts.geo) geo = await tab.js(GEO_SRC);
    const be = cls.backend;
    const stackHits = (probe.offers || []).concat(probe.api || []);
    const layoutCaused = stackHits.filter((r) => LAYOUT_STACK_RE.test(r.stack || ''));
    const rec = { seq: ++winSeq, tag, wall_ms: wall, action_result: acted, probe_reset: probeReset,
      requests_total: cls.total, backend_requests: be,
      backend_paths: Object.keys(cls.byPath).filter((k) => /^(\w+ )?\/(api|ws|webrtc|offer|answer|ice|signal|queue|session)/i.test(k)).sort(),
      static_requests: cls.staticish.length,
      http_errors: be.concat(cls.staticish).filter((r) => typeof r.status === 'number' && r.status >= 400)
        .map((r) => r.method + ' ' + r.path + ' -> ' + r.status),
      error_records: be.concat(cls.staticish)
        .filter((r) => typeof r.status === 'number' && r.status >= 400)
        .map((r) => ({ sig: r.method + ' ' + r.path + ' -> ' + r.status,
                       initiator: r.initiator, from_script: /^script\b/.test(r.initiator || '') })),
      constructors: { rtc: probe.rtc, ws: probe.ws },
      fetch_with_stack: stackHits.map((r) => ({ url: r.url, method: r.method,
        stack_is_layout: LAYOUT_STACK_RE.test(r.stack || ''),
        stack_is_reconnect: RECONNECT_STACK_RE.test(r.stack || ''),
        stack: (r.stack || '').slice(0, 420) })),
      layout_caused_fetches: layoutCaused.length,
      js_exceptions: exc.filter((e) => e.kind === 'JS_EXCEPTION'),
      log_errors: exc.filter((e) => e.kind !== 'JS_EXCEPTION').map((e) => e.kind + '/' + (e.source || '') + ' ' +
        String(e.url || '').slice(0, 120) + ' ' + String(e.text || '').slice(0, 120)) };
    if (opts.geo) rec.geo = geo;
    rec.video_sig_at_window_end = videoSig;
    log('win ' + tag.padEnd(22) + ' reqs=' + rec.requests_total +
        ' backend=' + be.length + ' rtc/ws=' + rec.constructors.rtc + '/' + rec.constructors.ws +
        ' layoutCaused=' + rec.layout_caused_fetches);
    return rec;
  };

  /* B0 背景噪声窗（完全无操作） */
  const idleWin = await win('idle_baseline_10s', null, { settleMs: 10000 });

  /* B1 逐个布局动作开窗。
   * 【顺序是判据的一部分】：所有"要和开场那一帧比几何"的动作必须排在任何视口覆盖
   * 之前 —— clearDeviceMetricsOverride 实测不保证还得回原视口（见 runClampSweep 上方
   * 那段），一旦排前面，panel_inside_dock / mode_roundtrip / live_portrait 的
   * "同一个视口"前提就静默失效。所以这里：几何类动作 → 无操作对照窗 →
   * 视口类动作（放最尾）→ clamp 扫描（更尾）。 */
  const opWindows = [];
  opWindows.push(await win('panel_toggle_1', { js: OP_CLICK_PANEL }, { settleMs: 1400, geo: true }));
  opWindows.push(await win('panel_toggle_2_back_to_start', { js: OP_CLICK_PANEL }, { settleMs: 1400, geo: true }));
  opWindows.push(await win('dock_scroll_bottom', { js: OP_SCROLL_BOTTOM }, { geo: true }));
  opWindows.push(await win('dock_scroll_top', { js: OP_SCROLL_TOP }));
  /* 模式按钮在 #uivm-panel 内部；面板处于收起态时它们仍在 DOM 里（只是被 CSS 隐藏），
   * 程序化 .click() 照样派发 —— 但为了让"点了没点到"这件事不靠推断，
   * 这里显式开一个【幂等】的展开窗，再单独开模式切换窗，一个窗一个动作。 */
  opWindows.push(await win('panel_open_for_mode', { js: OP_ENSURE_OPEN }, { settleMs: 900 }));
  opWindows.push(await win('mode_to_live', { js: opMode('live-portrait') }, { settleMs: 1600 }));
  const liveSnap = await snap({ kind: 'natural' });
  const live = liveSnap.geo;
  const liveCensus = await tab.js(AV_CENSUS_SRC);
  opWindows.push(await win('mode_to_detail', { js: opMode('detail') }, { settleMs: 1600 }));
  /* 面板要还原到【开场那一帧的状态】，不是硬写"收起"—— 开场展开态来自用户自己的
   * localStorage（ai_girlfriend_uivm_panel_v2），探针没资格替用户改它。 */
  const panelAtLoad = detail.panel ? detail.panel.collapsed : null;
  opWindows.push(await win('panel_restore_to_load_state',
    { js: opRestorePanel(panelAtLoad) }, { settleMs: 900, geo: true }));
  const backSnap = await snap({ kind: 'natural' });
  const backToDetail = backSnap.geo;
  const afterOpsSnap = await snap({ kind: 'natural' });
  const detailAfterOps = afterOpsSnap.geo;

  /* B2 第二个无操作对照窗（排在视口类动作之前，保证它是【干净无操作】的噪声基线） */
  const tailWin = await win('idle_recheck_10s', null, { settleMs: 10000 });

  /* B3 视口类动作：它们是"布局动作"，也要进网络账；但它们会下覆盖，
   * 所以只能排在所有几何比较之后。恢复不回原视口时，只登记、不推翻任何闸门。 */
  opWindows.push(await win('viewport_narrow_1440x900', { vp: { w: 1440, h: 900 } }, { settleMs: 900 }));
  opWindows.push(await win('viewport_back_to_natural', { vp: 'natural' }, { settleMs: 900 }));

  /* B4 clamp 三分支扫描（最后执行，理由同上） */
  const sweep = await runClampSweep();
  res.clamp_sweep = sweep;


  const allOpsBackend = opWindows.reduce((n, w) => n + w.backend_requests.length, 0);
  const allOpsLayoutCaused = opWindows.reduce((n, w) => n + w.layout_caused_fetches, 0);
  const allOpsRtc = opWindows.reduce((n, w) => n + w.constructors.rtc, 0);
  const allOpsWs = opWindows.reduce((n, w) => n + w.constructors.ws, 0);
  const idleOpsMs = idleWin.wall_ms + tailWin.wall_ms;
  const opsMs = opWindows.reduce((n, w) => n + w.wall_ms, 0);
  const rate = (n, ms) => +(n / (ms / 1000)).toFixed(5);
  log('backend: idle windows=' + idleWin.backend_requests.length + '+' + tailWin.backend_requests.length +
      '/' + idleOpsMs + 'ms  ops windows=' + allOpsBackend + '/' + opsMs + 'ms  layoutCaused=' + allOpsLayoutCaused);

  res.network = {
    method: '逐动作单独开窗 + 背景噪声对照 + fetch 调用栈归因（见文件头注释）',
    pre_window_settle: { requests: settle.total, backend: settle.backend.length,
      http_errors: settleErrors.map((e) => e.kind + ' ' + (e.url || '') + ' ' + (e.text || '')).slice(0, 8) },
    idle_baseline: idleWin, idle_recheck: tailWin, op_windows: opWindows,
    backend_rate_per_s: { idle_windows: rate(idleWin.backend_requests.length + tailWin.backend_requests.length, idleOpsMs),
      op_windows: rate(allOpsBackend, opsMs) },
    offers_observed: { idle_windows: idleWin.fetch_with_stack.length + tailWin.fetch_with_stack.length,
      op_windows: opWindows.reduce((n, w) => n + w.fetch_with_stack.length, 0) },
  };

  /* ---------- 闸门 ---------- */
  const G = res.gates;
  const dr = detail.dock.rect, band = detail.band, orb = detail.orb;

  /* 第 5 条 · 判据一：宽度公式在四个视口点上逐项成立，且 clamp 三个分支都被覆盖。
   * （写死"@1920 必须 430"在本机不成立 —— 真实窗口给不出 innerWidth≥1792，
   *   那属于环境事实，已归档进 VIEWPORT_CHANNEL_FINDING.md，不许拿它冒充产品缺陷，
   *   也不许因为"量不到"就免检上限分支。） */
  const branchCovered = {};
  sweep.forEach((s) => { branchCovered[s.branch] = (branchCovered[s.branch] || []).concat([s.id]); });
  G.dock_width_matches_clamp_formula = {
    pass: sweep.length === sweepPoints.length
          && sweep.every((s) => s.viewport_stable === true && Math.abs(s.delta_w) <= 1.0)
          && ['floor_340', 'middle_24vw', 'ceiling_430'].every((b) => (branchCovered[b] || []).length > 0),
    observed: { formula: 'clamp(340px, 24vw, 430px)', points: sweep,
      branches_covered: branchCovered,
      machine_fact: '本机物理 1920x1080 + 系统缩放 110% ⇒ mobile:false 覆盖被夹到客户区 DIP 1745x982，' +
                    '真实视口的 24vw 最大 418.8px；上限分支只能由 mobile:true 的布局视口点压出，已在 id 里标注。' },
  };

  /* 第 5 条 · 判据二：参考视口下的 Dock 盒本身 —— max-height 有界且不越出视口、
   * 内部 overflow-y:auto 可滚（超高不许把页面向下顶）、左边留有安全边距。 */
  G.dock_box_and_scroll_at_reference_viewport = {
    pass: detailSnap.viewport_stable === true && detailSnap.matches_wanted_viewport === true &&
          Math.abs(dr.h - Math.min(dr.h, detail.expected.maxH)) < 0.01 && dr.h <= detail.expected.maxH + 1 &&
          dr.bottom <= detail.viewport.h + 0.5 && dr.left >= 14 && dr.left <= 18 &&
          detail.dock.scrollCssOverflowY === 'auto' &&
          detail.dock.scrollHeight > detail.dock.clientHeight,
    observed: { viewport: detail.viewport, viewport_stable: detailSnap.viewport_stable,
      matches_wanted_viewport: detailSnap.matches_wanted_viewport, wanted: naturalRef.vp,
      dockW: dr.w, expectedClampW: detail.expected.clampW,
      dockLeft: dr.left, dockTop: dr.top, dockBottom: dr.bottom, dockH: dr.h,
      expectedMaxH: detail.expected.maxH, topbarH: detail.expected.topbarH,
      cssMaxHeight: detail.dock.cssMaxHeight,
      scrollOverflowY: detail.dock.scrollCssOverflowY,
      scrollHeight: detail.dock.scrollHeight, clientHeight: detail.dock.clientHeight },
  };

  /* 第 6 条：Dock 内分组 = 实时/画面/对话与声音/语音表现/对话（任务书四个必含组齐全） */
  const names = detail.groups.map((g2) => g2.label);
  G.dock_groups = {
    pass: ['画面', '对话与声音', '语音表现', '对话'].every((n) => names.indexOf(n) >= 0)
          && names.indexOf('实时') >= 0 && detail.groups.length === 5,
    observed: names,
  };

  /* 第 2 条：唯一左侧控制 Dock（除 #gf-dock 外没有第二个控制容器） */
  G.single_left_dock = {
    pass: detail.scaffold.dockEl === 1 && detail.scaffold.screenSlot === 1,
    observed: detail.scaffold,
  };

  /* 第 7 条：中间主画布清空（渲染后 DOM 断言，不只看源码） */
  G.central_canvas_cleared = {
    pass: detail.stage.childElementCount === 0 && detail.stage.descendantCount === 0,
    observed: detail.stage,
  };

  /* 第 8 条：圆球缩小、落在 Dock 内、且不压人物带/黑雾交界 */
  G.orb_shrunk_and_clear_of_person = {
    pass: orb.parentIsDock === true && orb.circleRect.w <= 130 &&
          detail.derived.orbInsideDock === true &&
          detail.derived.orbVsBand && detail.derived.orbVsBand.intersect === false &&
          detail.derived.orbVsFade && detail.derived.orbVsFade.intersect === false &&
          detail.derived.gapOrbToBand > 0,
    observed: { circleCssW: orb.circleCssW, circleRect: orb.circleRect, orbRect: orb.rect,
      insideDock: detail.derived.orbInsideDock, vsBand: detail.derived.orbVsBand,
      vsFade: detail.derived.orbVsFade, gapOrbToBand: detail.derived.gapOrbToBand,
      gapOrbToFadeStart: detail.derived.gapOrbToFadeStart, band: detail.band },
  };

  /* Dock 整体不压人物带（背景不透明 ⇒ 圆球也不可能透过去盖人） */
  G.dock_clear_of_person_band = {
    pass: detail.derived.dockVsBand && detail.derived.dockVsBand.intersect === false
          && detail.derived.gapDockToBand > 0,
    observed: { vs: detail.derived.dockVsBand, gap: detail.derived.gapDockToBand,
      dockRight: dr.right, bandLeft: band ? band.bandLeft : null,
      dockBackground: detail.derived.dockBgOpaque },
  };

  /* 画面布局面板：两态（展开 / 收起）都必须在 Dock 内部、都不是悬浮层。
   * 两态分别来自 panel_toggle_1 / panel_toggle_2 这两个动作窗内的实测几何。
   * "同一个视口"这条前提现在由【顺序】保证（这两窗排在任何视口覆盖之前），
   * 并且这里再逐字量一遍：三帧（开场 / 切换1 / 切换2）的 innerWidth×innerHeight
   * 必须完全相同 —— 不再依赖"扫描后能不能还原"那种环境运气。 */
  const wOpen = opWindows[0].geo, wClosed = opWindows[1].geo;
  const sameVp = (a, b) => !!a && !!b && a.viewport.w === b.viewport.w && a.viewport.h === b.viewport.h;
  G.panel_inside_dock = {
    pass: detail.panel && detail.panel.parentInDock === true && detail.panel.position === 'static'
          && wOpen && wOpen.panel && wOpen.panel.parentInDock === true && wOpen.panel.position === 'static'
          && wClosed && wClosed.panel && wClosed.panel.parentInDock === true && wClosed.panel.position === 'static'
          && wOpen.panel.collapsed !== wClosed.panel.collapsed
          && sameVp(wOpen, detail) && sameVp(wClosed, detail),
    observed: { viewport_after_sweep: res.viewport_after_sweep,
      viewport_at_load: detail.viewport,
      viewport_at_toggle_1: wOpen && wOpen.viewport, viewport_at_toggle_2: wClosed && wClosed.viewport,
      at_load: detail.panel,
      after_toggle_1: wOpen && wOpen.panel, inside_1: wOpen && wOpen.derived.panelInsideDock,
      after_toggle_2: wClosed && wClosed.panel, inside_2: wClosed && wClosed.derived.panelInsideDock },
  };

  /* 状态条：从 position:fixed 迁成 Dock 内的 static 流。
   * 上一版这条闸门叫 status_not_overlapped，判据是"状态条矩形不与 Dock 矩形相交"——
   * 它 PASS 了，但 PASS 的理由是【状态条在滚动区之外（下方）看不见】，
   * 与"没被 Dock 盖住"这个本意正好相反。改名并重写成两根真实的尺：
   *   (a) 归属与排版：在 #gf-dock 里、position:static、水平方向落在 Dock 内容区；
   *   (b) 可达性：它是 Dock 滚动内容的一部分（scrollHeight > clientHeight），
   *       并且把 Dock 滚到底之后它【真的进入可视框】——见下一条闸门用实测几何证明。 */
  const statusVis = detail.status.rect && detail.dock.rect ? {
    insideVisibleBox: detail.status.rect.top >= detail.dock.rect.top - 1 &&
                      detail.status.rect.bottom <= detail.dock.rect.bottom + 1,
    px_to_scroll_down: +(detail.status.rect.bottom - detail.dock.rect.bottom).toFixed(2),
  } : null;
  G.status_in_dock_scroll_flow = {
    pass: detail.status.inDock === true && detail.status.position === 'static' &&
          detail.status.rect.w > 0 && detail.status.rect.h > 0 &&
          detail.status.rect.left >= detail.dock.rect.left - 0.5 &&
          detail.status.rect.right <= detail.dock.rect.right + 0.5 &&
          detail.dock.scrollable === true && !!statusVis,
    observed: { inDock: detail.status.inDock, position: detail.status.position,
      statusRect: detail.status.rect, dockRect: detail.dock.rect,
      scrollable: detail.dock.scrollable, scrollHeight: detail.dock.scrollHeight,
      clientHeight: detail.dock.clientHeight, visibility_at_load: statusVis,
      note: 'px_to_scroll_down > 0 不是缺陷：Dock 有 max-height + overflow-y:auto（任务书第 5 条），' +
            '内容高于盒子时靠滚动到达。真正的缺陷是"滚到底也见不到"，由下一条闸门证伪。' },
  };

  /* 滚到底必须能把最后一组和状态条带进可视框（否则"迁进 Dock"等于把控件藏起来）。
   * 几何取自 dock_scroll_bottom 这个动作窗内实测（scrollTop 已被设为 scrollHeight）。 */
  const wScrollBottom = opWindows.find((w) => w.tag === 'dock_scroll_bottom');
  const sb = wScrollBottom && wScrollBottom.geo;
  const sbLastGroup = sb && sb.groups.length ? sb.groups[sb.groups.length - 1].rect : null;
  G.scrolled_dock_reveals_last_group_and_status = {
    pass: !!sb && sb.status && sb.status.rect &&
          sb.status.rect.bottom <= sb.dock.rect.bottom + 2 &&
          sb.status.rect.top >= sb.dock.rect.top - 1 &&
          !!sbLastGroup && sbLastGroup.bottom <= sb.dock.rect.bottom + 2 &&
          sbLastGroup.top >= sb.dock.rect.top - 1,
    observed: { scroll_top_op_result: wScrollBottom && wScrollBottom.action_result,
      dockRect: sb && sb.dock.rect,
      statusRect: sb && sb.status.rect, last_group: sbLastGroup && { label: sb.groups[sb.groups.length - 1].label, rect: sbLastGroup },
      scroll_after: sb && { scrollTop: sb.dock.scrollTop, scrollHeight: sb.dock.scrollHeight,
        clientHeight: sb.dock.clientHeight, max_scrollable: sb.dock.scrollHeight - sb.dock.clientHeight },
      all_group_rects: sb && sb.groups.map((g2) => ({ label: g2.label, top: g2.rect.top, bottom: g2.rect.bottom, bodyKids: g2.bodyKids })) },
  };


  /* 第 5 条脚手架账：docElements +18 = Dock 脚手架 + 一个新 <link> */
  G.scaffold_accounting = {
    pass: detail.scaffold.totalScaffold === 17 && detail.scaffold.newLinkTags === 1,
    observed: { scaffoldElements: detail.scaffold.totalScaffold, newLinkTags: detail.scaffold.newLinkTags,
      note: '1 aside + 1 .gf-dock-scroll + 5 section + 5 h2 + 5 .gf-dock-body = 17；' +
            '加 gf_dock.css 的一个 <link rel=stylesheet> = 18，与盘点的 623→641 完全对上。' },
  };

  /* 第 10 条：LIVE 取景框 9:16 保持，且人物层在 LIVE 下也没被 gf_dock.css 命中 */
  const lv = live.layer, ratio = lv && lv.h > 0 ? lv.w / lv.h : null;
  const gfSheet = (liveCensus.sheets || []).find((s) => /gf_dock\.css/.test(s.sheet));
  G.live_portrait_core_kept = {
    pass: liveSnap.viewport_stable === true && liveSnap.matches_wanted_viewport === true
          && live.viewMode === 'live-portrait'
          && ratio !== null && Math.abs(ratio - 9 / 16) < 0.01
          && !!gfSheet && gfSheet.leaf > 0 && gfSheet.hitV === 0 && gfSheet.hitL === 0
          && liveCensus.leafTotal > 0,
    observed: { viewMode: live.viewMode, viewport: live.viewport,
      viewport_stable: liveSnap.viewport_stable, matches_wanted_viewport: liveSnap.matches_wanted_viewport,
      layerRect: lv, ratio: ratio ? +ratio.toFixed(4) : null,
      cssVars: live.layerCssVars, gf_dock_leafRules: gfSheet ? gfSheet.leaf : null,
      gf_dock_hitVideo: gfSheet ? gfSheet.hitV : null, gf_dock_hitLayer: gfSheet ? gfSheet.hitL : null,
      leafTotalAcrossSheets: liveCensus.leafTotal },
  };

  /* 模式往返闭合：回到 DETAIL 后 Dock/人物几何与来之前一致。
   * 前置条件：两次测量都必须在【同一个且已稳定】的视口下做 —— 上一轮这里 FAIL 是
   * 跨了 1920 与 1745 两种视口去比几何造成的假故障，所以视口自证本身是闸门的一部分。 */
  G.mode_roundtrip_restores_detail = {
    pass: backToDetail.viewMode === 'detail'
          && detailSnap.viewport_stable === true && backSnap.viewport_stable === true
          && detailSnap.matches_wanted_viewport === true && backSnap.matches_wanted_viewport === true
          && JSON.stringify(backToDetail.viewport) === JSON.stringify(detail.viewport)
          && JSON.stringify(backToDetail.dock.rect) === JSON.stringify(detail.dock.rect)
          && JSON.stringify(backToDetail.band) === JSON.stringify(detail.band)
          && JSON.stringify(backToDetail.orb.circleRect) === JSON.stringify(detail.orb.circleRect),
    observed: { viewMode: backToDetail.viewMode,
      viewport_before: detail.viewport, viewport_after: backToDetail.viewport,
      viewport_stable: { before: detailSnap.viewport_stable, after: backSnap.viewport_stable },
      dockBefore: detail.dock.rect, dockAfterRoundtrip: backToDetail.dock.rect,
      bandBefore: detail.band, bandAfterRoundtrip: backToDetail.band,
      circleBefore: detail.orb.circleRect, circleAfter: backToDetail.orb.circleRect,
      panelStateAtLoad: panelAtLoad,
      panelStateAfterRestore: backToDetail.panel ? backToDetail.panel.collapsed : null,
      statusBefore: detail.status.rect, statusAfter: backToDetail.status.rect },
  };

  /* 第 11 条：布局操作不招后端请求、不新起连接。
   * 三条独立判据（任何一条红就 FAIL）：
   *   (a) 每个动作窗内 RTCPeerConnection / WebSocket 构造增量 = 0；
   *   (b) 每个动作窗内【调用栈命中布局层】的 fetch = 0；
   *   (c) 所有动作窗里出现的后端 fetch 栈，与两个无操作窗里出现过的栈【同一形状】
   *       （即：动作窗没有引入任何新的调用者）。
   * 背景重试（avatar-sync 的 scheduleReconnect）在动作窗里落到几条，如实写进
   * observed，不改判据 —— 它在无操作窗里同样出现，所以不是布局引起的。 */
  const stackShape = (f) => (f.stack || '').replace(/:\d+(:\d+)?/g, ':N').replace(/_00n_/g, '')
    .split('|').slice(0, 4).join('|').slice(0, 220);
  const idleShapes = new Set(idleWin.fetch_with_stack.concat(tailWin.fetch_with_stack).map(stackShape));
  const unknownShapeWindows = opWindows.filter((w) => w.fetch_with_stack.some((f) => !idleShapes.has(stackShape(f))
    && LAYOUT_STACK_RE.test(f.stack || '')));
  G.layout_generated_no_backend_requests = {
    pass: allOpsRtc === 0 && allOpsWs === 0 && allOpsLayoutCaused === 0 && unknownShapeWindows.length === 0,
    observed: {
      op_window_count: opWindows.length,
      op_windows_total_ms: opsMs,
      layout_caused_fetches: allOpsLayoutCaused,
      rtcpctor_deltas: opWindows.map((w) => ({ tag: w.tag, rtc: w.constructors.rtc, ws: w.constructors.ws })),
      backend_requests_per_op_window: opWindows.map((w) => ({ tag: w.tag, n: w.backend_requests.length,
        ms: w.wall_ms, paths: w.backend_requests.map((r) => r.method + ' ' + r.path + ' -> ' + r.status) })),
      backend_requests_idle_windows: { idle: idleWin.backend_requests.length,
        recheck: tailWin.backend_requests.length, total_ms: idleOpsMs },
      backend_rate_per_s: res.network.backend_rate_per_s,
      fetch_callers_seen: { idle: [...idleShapes],
        ops: [...new Set(opWindows.flatMap((w) => w.fetch_with_stack.map(stackShape)))] },
      fetches_in_ops_with_non_layout_stack: opWindows.reduce((n, w) => n + w.fetch_with_stack.filter((f) => !f.stack_is_layout).length, 0),
    },
  };

  /* HTTP 错误：只判"动作窗里出现的错误签名，是否在【与本布局无关的既有基线】里也出现过"。
   * 上一轮这里的 FAIL 来自 GET /favicon.ico -> 404 —— 那是本探针新开文档才有的产物，
   * Chrome 在加载后一两秒才发它，所以落进了第一个窗格。处置不是白名单，而是两件事：
   *   (1) 量几何/开窗之前先跑一个沉降窗，把这类"文档加载尾巴"吸收并如实记录；
   *   (2) 每个签名必须有明确【来源】：本轮无操作窗、沉降窗，或本 origin 上
   *       更早、且已被用户验收的那一轮冻结证据（V1 网络零干扰证据里
   *       baseline_idle_6s.distinctPaths 就把 /api/conn-trace 记为静置噪声）。
   * 没有来源的新错误 ⇒ FAIL。 */
  /* 每一条 ≥400 都要有【出处】，两条合法出处二选一：
   *   (1) baseline：同一条签名在本轮的沉降窗/无操作窗里出现过，
   *       或在本 origin 更早、且已被用户验收的那一轮冻结证据里被记为静置噪声
   *       （V1 网络零干扰证据的 baseline_idle_6s.distinctPaths 里有 /api/conn-trace）；
   *   (2) 非脚本发起：CDP 的 initiator.type 不是 'script'（favicon 是 'Other'，
   *       由浏览器自己按文档发，一次点击/一次 DOM 变更在原理上发不出它）。
   * 两条都没有 ⇒ FAIL 并原文报出。不做字符串白名单。 */
  const baselineErr = new Map();
  const addErr = (sig, src) => { if (!baselineErr.has(sig)) baselineErr.set(sig, src); };
  const sigsOfCls = (c) => [].concat(c.backend, c.staticish)
    .filter((r) => typeof r.status === 'number' && r.status >= 400)
    .map((r) => r.method + ' ' + r.path + ' -> ' + r.status);
  sigsOfCls(settle).forEach((s) => addErr(s, 'this_run:pre_window_settle'));
  [idleWin, tailWin].forEach((w) => w.error_records.forEach((e) => addErr(e.sig, 'this_run:idle_window:' + w.tag)));
  /* 冻结的上一轮证据（只读磁盘，不发请求） */
  const PRIOR_NET_EVIDENCE = path.join(REPO, 'runtime', 'evidence', 'UI_DUAL_VIEWMODE_01',
                                        'NETWORK_ZERO_INTERFERENCE_TEST.json');
  let priorBaseline = null;
  try {
    const j = JSON.parse(fs.readFileSync(PRIOR_NET_EVIDENCE, 'utf8'));
    priorBaseline = { file: path.relative(REPO, PRIOR_NET_EVIDENCE).replace(/\\/g, '/'),
                      verdict: j.VERDICT, layout_generated_backend_requests: j.LAYOUT_GENERATED_BACKEND_REQUESTS,
                      distinctPaths: (j.baseline_idle_6s || {}).distinctPaths || [],
                      note: (j.baseline_idle_6s || {}).note || null };
    (priorBaseline.distinctPaths || []).forEach((p) => addErr('ANY ' + p, 'frozen_prior_round:' + priorBaseline.file));
  } catch (e) { priorBaseline = { file: PRIOR_NET_EVIDENCE, read_error: String(e.message).slice(0, 200) }; }
  const pathOfSig = (sig) => { const m = /\s(\/[^ ]*)\s/.exec(sig); return m ? m[1] : null; };
  const justify = (e) => {
    if (baselineErr.has(e.sig)) return { ok: true, because: baselineErr.get(e.sig) };
    if (baselineErr.has('ANY ' + pathOfSig(e.sig))) return { ok: true, because: baselineErr.get('ANY ' + pathOfSig(e.sig)) };
    if (!e.from_script) return { ok: true, because: 'not_script_initiated:initiator=' + e.initiator };
    return { ok: false, because: 'NO_BASELINE_AND_SCRIPT_INITIATED' };
  };
  const errVerdicts = opWindows.map((w) => ({ window: w.tag,
    errors: w.error_records.map((e) => ({ sig: e.sig, initiator: e.initiator,
      from_script: e.from_script, ok: justify(e).ok, because: justify(e).because })) }))
    .filter((x) => x.errors.length);
  const unjustified = errVerdicts.flatMap((w) => w.errors.filter((e) => !e.ok).map((e) => ({ window: w.window, ...e })));
  G.no_new_http_errors_from_layout_ops = {
    pass: unjustified.length === 0,
    observed: { unjustified: unjustified, per_op_window: errVerdicts,
      baseline_sources: [...baselineErr.entries()].map(([sig, src]) => ({ signature: sig, source: src })),
      frozen_prior_round: priorBaseline,
      pre_window_settle_log_errors: settleErrors.map((e) => e.kind + ' ' + (e.source || '') + ' ' +
        (e.url || '') + ' ' + (e.text || '')).slice(0, 10) },
  };

  /* 零异常：只数 Runtime.exceptionThrown（页面真的抛了 JS 错）。
   * Log.entryAdded level=error 是【网络层】事实（404/405 一类），上一轮四条全是它，
   * 把它算进"JS 崩了"是把两类尺子混成一根 —— 它归上面那条闸门管。 */
  const jsExc = opWindows.concat([idleWin, tailWin]).flatMap((w) => w.js_exceptions)
    .filter((e) => e.kind === 'JS_EXCEPTION');
  G.no_js_exceptions = {
    pass: jsExc.length === 0,
    observed: { js_exception_count: jsExc.length, js_exceptions: jsExc.slice(0, 8),
      note: '网络层 Log.error（404/405）不计入本闸门，见 no_new_http_errors_from_layout_ops',
      network_log_errors_per_window: opWindows.concat([idleWin, tailWin])
        .map((w) => ({ window: w.tag, n: w.log_errors.length,
          first: (w.log_errors[0] || {}).text || null })) },
  };

  /* 探针自证：每个动作窗开头都必须能重置到页面内计数器。
   * 计数器一旦丢了，后面所有"rtc=0 / ws=0"都会变成【假的干净】——
   * 快照返回的是 {rtc:0,ws:0,offers:[],api:[]}，和真的零活动一模一样。
   * 所以这条不是装饰，它是上面两条网络闸门的地板。 */
  const allWins = opWindows.concat([idleWin, tailWin]);
  const deadProbeWins = allWins.filter((w) => w.probe_reset !== 'RESET').map((w) => ({ tag: w.tag, got: w.probe_reset }));
  G.probe_instrumentation_alive_every_window = {
    pass: deadProbeWins.length === 0 && allWins.length >= 12,
    observed: { windows: allWins.length, dead_probe_windows: deadProbeWins,
      per_window_reset: allWins.map((w) => w.tag + ':' + w.probe_reset) },
  };

  /* 文档级导航：走 CDP 事件，不走页面变量（页面变量会被重载一起冲掉）。
   * 布局动作如果把页面 reload 了，"控件还在、监听还在"就全是重新加载出来的假象。
   * 计数只算 navBaseline 之后（那里面是探针自己那一次首屏 navigate，属预期）。 */
  const navAfterBaseline = navEvents.slice(navBaseline.length);
  res.page_navigation_events = navEvents.slice();
  G.no_document_navigation_during_probe = {
    pass: navAfterBaseline.length === 0 && navBaseline.length >= 1,
    observed: { events_after_baseline: navAfterBaseline, count: navAfterBaseline.length,
      baseline: { count: navBaseline.length, note: '探针自己的首屏 navigate，计 1 次' },
      windows: allWins.length,
      note: 'Page.frameNavigated(主框架) + Page.navigatedWithinDocument 两类；' +
            '截图标签页属另一个 sessionId，已被 sessionId 过滤掉，不混进这条账。' },
  };

  /* 任务书第 11 条："人物 video node = 1 / srcObject 不变"。拆成两根各自成立的尺：
   *   闸门①（本轮的责任范围）这一路人物的【DOM 节点身份】：#av-video 只有一个、
   *     始终是我们开场那一帧存下的那个 JS 对象（=== 比较，克隆/重建都会露馅）、
   *     没有任何 src 属性、全场只有它在承载 MediaStream。
   *   闸门②（应用自己的会话生命周期）流身份如果在跑动中变了，必须能钉到【某一扇窗】，
   *     并且那一窗内 rtc/ws 构造增量 = 0、栈命中布局层的 fetch = 0。
   * 为什么闸门②不是"流必须一辈子不变"：实测开场 1 条 track，几十秒后变 2 条
   * （avatar-sync 在【我们这一个】标签页里自己完成的一次再协商），
   * 而这期间没有任何新的 RTCPeerConnection/WebSocket 被构造 —— 那是被冻结的媒体链
   * 自身的生命周期，不是布局改出来的第二个流。硬要求"一辈子不变"只会逼我去
   * 谎报或者拆掉真判据。 */
  const vids = { detail: detail.video, after_ops: detailAfterOps.video,
    live: live.video, back_to_detail: backToDetail.video };
  const vpKeys = Object.keys(vids);
  G.single_avatar_video_node_identity = {
    pass: vpKeys.every((k) => vids[k]) &&
          vpKeys.every((k) => vids[k].avVideoCount === 1 && vids[k].sameObjectAsStamp === true &&
                              vids[k].stampPresent === true && vids[k].srcAttr === null) &&
          allWins.every((w) => !w.video_sig_at_window_end ||
              (w.video_sig_at_window_end.count === 1 && w.video_sig_at_window_end.same_object === true &&
               w.video_sig_at_window_end.srcAttr === null)) &&
          vids.detail.allVideoCount === 2 &&
          vids.detail.videosWithSrcObject.length === 1 && vids.detail.videosWithSrcObject[0] === 'av-video' &&
          vpKeys.every((k) => vids[k].srcObjectCtor === 'MediaStream' && vids[k].trackIds && vids[k].trackIds.length >= 1),
    observed: { samples: vpKeys.map((k) => ({ k, count: vids[k].avVideoCount,
        sameObjectAsStamp: vids[k].sameObjectAsStamp, srcAttr: vids[k].srcAttr,
        srcObjectCtor: vids[k].srcObjectCtor, withSrcObject: vids[k].videosWithSrcObject,
        allVideoIds: vids[k].allVideoIds, streamId: vids[k].streamId, trackIds: vids[k].trackIds })),
      per_window_sig: allWins.map((w) => ({ tag: w.tag, sig: w.video_sig_at_window_end })),
      note: 'allVideoCount=2 的另一只是 #cam-video（相机预览，无流）；' +
            'srcAttr 必须恒为 null：这一路人物流的唯一入口是 srcObject（WebRTC track）。' },
  };

  /* 闸门②：流身份逐窗时间线 + 变化归因。按 win() 的自增序号排成【真实时间序】
   * （opWindows 里最后两扇视口窗是在 tailWin 之后才 push 的，数组序≠时序）。 */
  const winsChrono = allWins.slice().sort((a, b) => a.seq - b.seq);
  const sigSeq = [{ where: 'A:load_stamp', seq: 0, sig: res.video_node_stamp.sig_at_stamp && res.video_node_stamp.sig_at_stamp.sig,
                    rtc: null, ws: null, layoutCaused: null, backend: null }];
  winsChrono.forEach((w) => sigSeq.push({ where: 'B#' + w.seq + ':' + w.tag, seq: w.seq,
    sig: w.video_sig_at_window_end && w.video_sig_at_window_end.sig,
    rtc: w.constructors.rtc, ws: w.constructors.ws, layoutCaused: w.layout_caused_fetches,
    backend: w.backend_requests.length,
    fetch_stacks: w.fetch_with_stack.map((f) => (f.stack || '').slice(0, 160)) }));
  const sigChanges = [];
  for (let i = 1; i < sigSeq.length; i++) {
    if (sigSeq[i].sig !== sigSeq[i - 1].sig)
      sigChanges.push({ in_window: sigSeq[i].where, from: sigSeq[i - 1].sig, to: sigSeq[i].sig,
        rtc: sigSeq[i].rtc, ws: sigSeq[i].ws, layoutCaused: sigSeq[i].layoutCaused,
        backend: sigSeq[i].backend, fetch_stacks: sigSeq[i].fetch_stacks });
  }
  G.stream_change_not_layout_caused = {
    pass: sigSeq.every((s) => !!s.sig) &&
          sigChanges.every((c) => c.rtc === 0 && c.ws === 0 && c.layoutCaused === 0),
    observed: { distinct_sigs: [...new Set(sigSeq.map((s) => s.sig))],
      change_count: sigChanges.length, changes: sigChanges, timeline: sigSeq.map((s) => s.where + ' = ' + s.sig),
      note: '变化数 = 0 也 PASS（那种情况这条闸门是空真）；变化数 > 0 时要求发生窗内' +
            ' rtc/ws 构造 = 0 且布局栈命中 = 0，并把 fetch 栈原样列出供回查。' },
  };


  /* 人物 transform 逐字未改（第 9/10 条）。三级证据合取：
   *   (1) 磁盘：avatar_layout.js 的 sha256 与 PRE 基线逐字相同 ⇒ owner 没被重写；
   *   (2) 选择器：gf_dock.css 的叶子规则对 #av-video / #av-layer 命中数 = 0（DETAIL 与
   *       LIVE 各普查一次），且同一份普查里【正对照】必须 >0 —— 否则"0 命中"
   *       只是扫描器失灵；
   *   (3) 渲染后：内联 transform 非空、含 scale，且 DETAIL→LIVE→DETAIL 往返逐字不变。
   * 注意不拿"今天的字符串"去比"上周冻结的字符串"：中间用户自己动过滑杆
   * （TRANSFORM_EQUIVALENCE.json 的 note_state_drift 已记录 s100→s97），
   * 跨会话比字面值会把用户操作误判成本轮改动。同一文档内比往返才是本轮的责任范围。 */
  const gfSheetOf = (c) => ((c && c.sheets) || []).find((s) => /gf_dock\.css/.test(s.sheet)) || null;
  const gfD = gfSheetOf(detailCensus), gfL = gfSheetOf(liveCensus);
  /* 正对照：上游 avatar_layout.css 应当【能】命中人物层（若它命中为 0，说明普查失灵） */
  const ctrlSheet = (c) => ((c && c.sheets) || []).filter((s) => /avatar_layout\.css|main\.css|style\.css/.test(s.sheet))
    .map((s) => ({ sheet: s.sheet, leaf: s.leaf, hitV: s.hitV, hitL: s.hitL }));
  const tfD = detail.videoInlineTransform, tfB = backToDetail.videoInlineTransform;
  const ownerFile = path.join(REPO, 'runtime/ui/app/avatar_layout.js');
  const ownerDisk = { sha256: crypto.createHash('sha256').update(fs.readFileSync(ownerFile)).digest('hex'),
    bytes: fs.statSync(ownerFile).size };
  const preManifestForOwner = (() => {
    try {
      const pre = JSON.parse(fs.readFileSync(path.join(EV, 'PRE_CHANGE_MANIFEST.json'), 'utf8'));
      return (pre.uiApp || []).find((r) => r.file === 'avatar_layout.js') || null;
    } catch (e) { return { read_error: String(e.message).slice(0, 160) }; }
  })();
  const ownerHashMatch = !!(preManifestForOwner && preManifestForOwner.sha256 &&
                            preManifestForOwner.sha256 === ownerDisk.sha256);
  G.avatar_transform_owner_unchanged = {
    pass: ownerHashMatch && !!gfD && !!gfL && gfD.leaf > 0 && gfL.leaf > 0 &&
          gfD.hitV === 0 && gfD.hitL === 0 && gfL.hitV === 0 && gfL.hitL === 0 &&
          ctrlSheet(detailCensus).some((s) => s.hitV > 0 || s.hitL > 0) &&
          typeof tfD === 'string' && /scale/.test(tfD) && tfD === tfB,
    observed: { disk: { expected_from_pre_manifest: preManifestForOwner && preManifestForOwner.sha256,
        actual_now: ownerDisk.sha256, bytes: ownerDisk.bytes, match: ownerHashMatch },
      gf_dock_css_detail: gfD && { leaf: gfD.leaf, hitAvVideo: gfD.hitV, hitAvLayer: gfD.hitL },
      gf_dock_css_live: gfL && { leaf: gfL.leaf, hitAvVideo: gfL.hitV, hitAvLayer: gfL.hitL },
      positive_control_sheets: ctrlSheet(detailCensus),
      transform_detail: tfD, transform_back_to_detail: tfB,
      transform_live: live.videoInlineTransform,
      band_detail: detail.band, band_back: backToDetail.band,
      note: '跨会话字面值不比对，理由见上（用户中途调过滑杆，属正当生命周期）。' +
            '逐字等价证据在 TRANSFORM_EQUIVALENCE.json：同一状态下 BEFORE/AFTER 输出字符串相同。' },
  };

  /* 第 11 条最后一项：文件级归属。每次都【现跑一遍】再读，不缓存旧 JSON ——
   * 磁盘在这轮里被改过多次（POST 清单之后 index.html/gf_dock.css 又动过一次），
   * 读一份过期审计报告就是拿记忆冒充事实。子进程退出码 1 = 有越界改动，
   * 但它已经先把 FILE_AUDIT_OFFLINE.json 写盘了，所以照常读内容做判据。 */
  let fileAudit = null, fileAuditRun = { attempted: false };
  try {
    const { execFileSync } = require('node:child_process');
    try {
      const so = execFileSync(process.execPath,
        [path.join(__dirname, 'file_audit_offline.cjs')], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
      fileAuditRun = { attempted: true, exit_code: 0, stdout_tail: so.split(/\r?\n/).slice(0, 6) };
    } catch (e) {
      fileAuditRun = { attempted: true, exit_code: typeof e.status === 'number' ? e.status : null,
        stdout_tail: String(e.stdout || '').split(/\r?\n/).slice(0, 6),
        stderr_tail_head: String(e.stderr || '').slice(0, 200) };
    }
    fileAudit = JSON.parse(fs.readFileSync(path.join(EV, 'FILE_AUDIT_OFFLINE.json'), 'utf8'));
  } catch (e) { fileAudit = { read_error: String(e.message).slice(0, 160) }; }
  G.non_ui_modified_files_none = {
    pass: fileAuditRun.attempted === true &&
          !!fileAudit && fileAudit.VERDICT === 'NON_UI_MODIFIED_FILES = NONE' &&
          (fileAudit.totals || {}).out_of_scope === 0 &&
          (fileAudit.totals || {}).unreadable_or_deleted === 0 &&
          (fileAudit.rows || []).every((r) => r.kind !== 'backend' || r.status === 'UNCHANGED_SINCE_PRE'),
    observed: { verdict: fileAudit && fileAudit.VERDICT, totals: fileAudit && fileAudit.totals,
      audit_run: fileAuditRun,
      in_scope_modified: fileAudit && (fileAudit.in_scope_modified || []).map((r) => r.file + ' ' + r.status),
      out_of_scope_modified: fileAudit && fileAudit.out_of_scope_modified,
      generated_at: fileAudit && fileAudit.generated_at,
      note: '由 file_audit_offline.cjs 现跑生成：282 份清单内文件（46 UI + 235 后端/相邻系统）逐一重算 sha256。' },
  };

  /* 静态证据：这套布局代码自己有没有任何网络调用点。
   * 判据只数【抹掉注释之后仍然成立】的命中，并且作用域收窄到本轮真正写过的东西：
   *   · gf_dock.css        —— 整份文件（本轮新增的样式，里面根本没有 JS）
   *   · ui_layout_modes.js —— 整份文件（唯一被改过的布局脚本；里面那句匹配是注释）
   *   · index.html         —— 只取 <aside id="gf-dock">…</aside> 这一段（配平嵌套 aside）
   * index.html 其余部分（含 <script src=…>、上游内联注释）是【没动过的上游代码】，
   * 单独报账，不进判据 —— 上一版把整份 index.html 一起扫，并把 HTML 注释的续行
   * （612 行，末尾才是 -->）当成代码，制造了一次假 FAIL。
   *
   * 三条阳性对照，防止"扫描器坏了 ⇒ 0 命中 ⇒ 假 PASS"（环境闸门必须正向证明）：
   *   (1) 现成的脏文件 runtime/ui/app/avatar-sync.js 必须数得出 >0；
   *   (2) 真代码 fetch(...) 必须数得出 1；
   *   (3) 多行 HTML 注释里的 RTCPeerConnection 必须数得出 0，
   *       而同一个字符串去掉注释外壳后必须数得出 1（证明是"注释在挡"，不是"扫不到"）。 */
  const ctrl = {
    dirty_file_avatar_sync: (() => {
      const t = fs.readFileSync(path.join(REPO, 'runtime/ui/app/avatar-sync.js'), 'utf8');
      return { lang: 'js', code_hits: codeHitsIn(t, 'js'),
        raw_hits: t.split(/\r?\n/).filter((l) => NET_CALLSITE_RE.test(l)).length };
    })(),
    plain_call: { code_hits: codeHitsIn("var a = fetch('/api/x');", 'js'), expect: 1 },
    multi_line_html_comment: {
      masked_code_hits: codeHitsIn('<!-- line one\n     包装到 RTCPeerConnection 的构造\n     第二行 -->', 'html'),
      unmasked_code_hits: codeHitsIn('   包装到 RTCPeerConnection 的构造   ', 'html'),
      expect: [0, 1],
    },
  };
  const htmlText = fs.readFileSync(path.join(REPO, 'runtime/ui/app/index.html'), 'utf8');
  const dockRegion = gfDockRegion(htmlText);
  const callSites = {
    'runtime/ui/app/gf_dock.css (whole file)': scanCallSites(path.join(REPO, 'runtime/ui/app/gf_dock.css'), 'css'),
    'runtime/ui/app/ui_layout_modes.js (whole file)': scanCallSites(path.join(REPO, 'runtime/ui/app/ui_layout_modes.js'), 'js'),
    'runtime/ui/app/index.html (<aside id=gf-dock> region only)':
      dockRegion ? scanCallSites(path.join(REPO, 'runtime/ui/app/index.html'), 'html', dockRegion)
                 : { code_hits: ['<aside id="gf-dock" NOT FOUND — 作用域无法建立'], comment_hits: [], raw_total: 0 },
  };
  const htmlWhole = scanCallSites(path.join(REPO, 'runtime/ui/app/index.html'), 'html');
  const probeAlive = ctrl.dirty_file_avatar_sync.code_hits > 0 &&
    ctrl.plain_call.code_hits === 1 &&
    ctrl.multi_line_html_comment.masked_code_hits === 0 &&
    ctrl.multi_line_html_comment.unmasked_code_hits === 1;
  G.layout_code_has_no_backend_call_sites = {
    pass: probeAlive && !!dockRegion &&
          Object.values(callSites).every((v) => v.code_hits.length === 0),
    observed: { probe_alive: probeAlive, positive_controls: ctrl,
      dock_region_char_bounds: dockRegion,
      html_line_range: dockRegion ? { first: htmlText.slice(0, dockRegion.start).split(/\r?\n/).length,
        last: htmlText.slice(0, dockRegion.end).split(/\r?\n/).length } : null,
      scanned: callSites,
      note: 'code_hits = 抹掉注释后仍命中的行（判据）；raw_total = 含注释的原文命中。' +
            '静态"调用点=0"与动态"每个动作窗 rtc/ws 构造增量=0"互为对偶。' },
  };
  res.info.static_scan_outside_scope = {
    'runtime/ui/app/index.html (whole file, untouched upstream included)': htmlWhole,
    note: '仅登记，不进判据：整份 index.html 的命中全部位于 HTML 注释里（见 comment_hits），' +
          '且位于 Dock 区段之外 ⇒ 属未改动的上游 markup/注释。',
  };


  /* 卫生账：探针有没有给用户留下偏好副作用（localStorage 逐键对拍） */
  G.user_persistence_untouched_by_probe = { pass: null, observed: {} }; // 末尾填，见 RESTORE

  res.info.live_census = liveCensus;
  res.info.live_snapshot = live;
  res.info.back_to_detail_snapshot = backToDetail;
  res.info.detail_after_ops_snapshot = detailAfterOps;
  /* 每一次"量几何"的视口自证。旧字段名 viewport_ok 指的是已经被否定的那条判据
   * （见 VIEWPORT_CHANNEL_FINDING.md：mobile:false 的覆盖会被夹到客户区 DIP），
   * 现在换成两根真实的尺：stable = 连续两次 innerWidth/innerHeight 读数相同；
   * matches_wanted = 稳定下来的那一组，确实等于本探针声明的参考视口。 */
  res.info.viewport_proof = {
    natural_reference: naturalRef.vp, natural_reference_stable: naturalRef.stable,
    detail: { stable: detailSnap.viewport_stable, matches_wanted: detailSnap.matches_wanted_viewport, viewport: detailSnap.viewport },
    after_ops: { stable: afterOpsSnap.viewport_stable, matches_wanted: afterOpsSnap.matches_wanted_viewport, viewport: afterOpsSnap.viewport },
    live: { stable: liveSnap.viewport_stable, matches_wanted: liveSnap.matches_wanted_viewport, viewport: liveSnap.viewport },
    back_to_detail: { stable: backSnap.viewport_stable, matches_wanted: backSnap.matches_wanted_viewport, viewport: backSnap.viewport },
  };
  /* （gates_all_pass 在 E 段之后统一重算，因为 user_persistence 那条要等还原结果） */

  /* ---------- D. 截图：独立新窗口 + 洋红自证 ---------- */
  fs.mkdirSync(SHOT, { recursive: true });
  const shotTab = await Tab.createInNewWindow(conn, 'about:blank', 1920, 1080);
  await shotTab.navigate('http://127.0.0.1:7860/?probe=dock-shot', 4000);
  await sleep(1500);
  await shotTab.js(INSTALL);
  await shotTab.js(INJECT_DOUBLE);
  for (let i = 0; i < 40; i++) {
    const r = await shotTab.js(`(function(){var v=document.getElementById('av-video');return v?[v.videoWidth,v.readyState]:null;})()`);
    if (r && r[0] > 0 && r[1] >= 2) break;
    await sleep(250);
  }
  const proof = await shotTab.verifyCaptureTargetsThisDocument({ fromSurface: true });
  res.screenshot_channel_proof = proof;
  log('screenshot proof reflects_this_document=' + proof.reflects_this_document +
      ' magenta with_marker=' + proof.magenta_ratio.with_marker);
  const shots = [];
  async function shoot(tag) {
    const f = path.join(SHOT, tag + '_DOUBLE_' + Date.now() + '.png');
    const n = await shotTab.screenshot(f, { fromSurface: true });
    shots.push({ tag, file: path.relative(REPO, f).replace(/\\/g, '/'), bytes: n });
    log('shot ' + tag + ' ' + n + 'B');
  }
  await shoot('DOCK_DETAIL_1920x1080_collapsed');
  await shotTab.js(`(function(){var b=document.getElementById('gf-layout-open'); if(b) b.click(); return 'OPEN';})()`);
  await sleep(500);
  await shoot('DOCK_DETAIL_1920x1080_panel_open');
  await shotTab.js(`(function(){var b=document.querySelector('#uivm-panel .uivm-mode[data-mode="live-portrait"]'); if(b) b.click(); return b?'CLICKED':'NOBTN';})()`);
  await sleep(800);
  await shoot('DOCK_LIVE_PORTRAIT_1920x1080');
  await shotTab.js(`(function(){var b=document.querySelector('#uivm-panel .uivm-mode[data-mode="detail"]'); if(b) b.click(); if(b){var p=document.getElementById('gf-layout-open'); if(p) p.click();} return 'BACK';})()`);
  await sleep(600);
  await shotTab.setViewport(1440, 900); await sleep(700);
  await shoot('DOCK_DETAIL_1440x900');
  res.screenshots = { pixel_source: 'canvas.captureStream 测试替身（608x1056），非真人物流', shots };
  try { await shotTab.close(); } catch (_) {}

  /* ---------- E. 卫生账收尾：把探针写进用户 origin 的偏好还原回快照 ----------
   * 必须在截图标签页也关掉【之后】再做，否则它后写的那一份会覆盖还原。 */
  const LS_RESTORE = (vals) => `(function(){var v=${JSON.stringify(vals)},n=0;
    Object.keys(v).forEach(function(k){ try { if(v[k]===null) localStorage.removeItem(k); else { localStorage.setItem(k, v[k]); } n++; } catch(_){} });
    return n;})()`;
  const lsAfter = await tab.js(LS_ALL);
  const diffs = [];
  const allKeys = new Set([...Object.keys(lsBefore.values), ...Object.keys(lsAfter.values)]);
  for (const k of allKeys) {
    if (lsBefore.values[k] !== lsAfter.values[k]) {
      diffs.push({ key: k, before: lsBefore.values[k] === undefined ? '<ABSENT>' : String(lsBefore.values[k]).slice(0, 200),
                   after: lsAfter.values[k] === undefined ? '<ABSENT>' : String(lsAfter.values[k]).slice(0, 200) });
    }
  }
  let restored = null;
  if (diffs.length) {
    await tab.js(LS_RESTORE(lsBefore.values));
    const lsFinal = await tab.js(LS_ALL);
    const stillDiff = Object.keys(lsFinal.values).filter((k) => lsFinal.values[k] !== lsBefore.values[k])
      .concat(Object.keys(lsBefore.values).filter((k) => !(k in lsFinal.values)));
    restored = { rewrites: diffs.length, remaining_diffs: stillDiff, final: lsFinal };
  }
  res.localStorage.after_keys = lsAfter.count;
  res.localStorage.diffs = diffs;
  res.localStorage.restored = restored;
  G.user_persistence_untouched_by_probe = {
    pass: diffs.length === 0 || (restored && restored.remaining_diffs.length === 0),
    observed: { keys_before: lsBefore.count, keys_after: lsAfter.count, changed: diffs,
      restore: restored,
      note: '两个标签页与用户日常页面共用同一 origin 的 localStorage；探针的点击会写面板/模式偏好。' +
            '判据是【最终净效果为零】：若有差异则按快照逐键写回并复验。' },
  };

  try { await tab.close(); } catch (_) {}
  try { if (typeof offNav === 'function') offNav(); } catch (_) {}

  /* 任务书最终键 ← 闸门集合的机械映射。这一层只做"哪些闸门归哪个键"，
   * 不新增判断；任何一个归属闸门不为 PASS，对应键就是 INCOMPLETE。
   * UI_VISUAL_USER_ACCEPTANCE 永远不由探针给出 —— 那是用户的主观验收。 */
  const ok = (k) => G[k] && G[k].pass === true;
  const REQ = {
    LEFT_CONTROL_DOCK: ['single_left_dock', 'dock_groups', 'dock_width_matches_clamp_formula',
      'dock_box_and_scroll_at_reference_viewport', 'panel_inside_dock',
      'status_in_dock_scroll_flow', 'scrolled_dock_reveals_last_group_and_status'],
    CENTRAL_CANVAS_CLEARED: ['central_canvas_cleared', 'orb_shrunk_and_clear_of_person',
      'dock_clear_of_person_band'],
    EXISTING_CONTROLS_REUSED: ['scaffold_accounting', 'panel_inside_dock',
      'mode_roundtrip_restores_detail', 'no_document_navigation_during_probe'],
    AVATAR_TRANSFORM_UNCHANGED: ['avatar_transform_owner_unchanged', 'mode_roundtrip_restores_detail'],
    LIVE_PORTRAIT_CORE_MODEL: ['live_portrait_core_kept'],
    UI_PRESENTATION_ONLY_CHANGE: ['non_ui_modified_files_none', 'layout_generated_no_backend_requests',
      'single_avatar_video_node_identity', 'stream_change_not_layout_caused',
      'no_document_navigation_during_probe', 'probe_instrumentation_alive_every_window',
      'layout_code_has_no_backend_call_sites',
      'no_new_http_errors_from_layout_ops', 'no_js_exceptions',
      'user_persistence_untouched_by_probe'],
  };
  res.requirement_keys = {};
  for (const k of Object.keys(REQ)) {
    const missing = REQ[k].filter((g2) => !G[g2]);
    const failing = REQ[k].filter((g2) => G[g2] && G[g2].pass !== true);
    res.requirement_keys[k] = missing.length ? { status: 'INCOMPLETE_MISSING_GATE', missing }
      : failing.length ? { status: 'INCOMPLETE', failing }
      : { status: 'PASS', gates: REQ[k] };
  }
  res.requirement_keys.UI_VISUAL_USER_ACCEPTANCE = {
    status: 'PENDING', note: '用户主观视觉验收，探针不得代答（任务书 §33 同源纪律）。' };

  res.gates_all_pass = Object.keys(G).every((k) => G[k] && G[k].pass === true);
  fs.writeFileSync(OUT, JSON.stringify(res, null, 2));
  log('wrote ' + OUT);
  console.log('--- GATES ---');
  for (const k of Object.keys(G)) console.log((G[k].pass === true ? 'PASS ' : (G[k].pass === false ? 'FAIL ' : 'NULL ')) + k);
  console.log('gates_all_pass = ' + res.gates_all_pass);
  console.log('--- REQUIREMENT KEYS (mechanical map from gates) ---');
  for (const k of Object.keys(res.requirement_keys)) {
    const v = res.requirement_keys[k];
    console.log((v.status === 'PASS' ? 'PASS ' : (v.status === 'PENDING' ? 'PEND ' : 'NO   ')) + k +
      (v.failing ? ' failing=' + JSON.stringify(v.failing) : '') +
      (v.missing ? ' missing=' + JSON.stringify(v.missing) : ''));
  }
  console.log('band=' + JSON.stringify(detail.band));
  console.log('dock=' + JSON.stringify(detail.dock.rect) + ' circle=' + JSON.stringify(detail.orb.circleRect));
  console.log('viewport_proof=' + JSON.stringify(res.info.viewport_proof));
  console.log('backend rate idle/ops per s = ' + JSON.stringify(res.network.backend_rate_per_s));
  console.log('op windows backend requests = ' + JSON.stringify(opWindows.map((w) => w.tag + ':' + w.backend_requests.length + '/' + w.constructors.rtc + 'rtc' + w.constructors.ws + 'ws')));
  console.log('idle windows backend requests = ' + idleWin.backend_requests.length + ' + ' + tailWin.backend_requests.length);
  console.log('fetch callers idle shapes = ' + JSON.stringify([...idleShapes].slice(0, 4)));
  console.log('fetch callers ops shapes  = ' + JSON.stringify([...new Set(opWindows.flatMap((w) => w.fetch_with_stack.map(stackShape)))].slice(0, 4)));
  console.log('localStorage diffs = ' + JSON.stringify(res.localStorage.diffs));
  conn.close();
  clearTimeout(wd);
  process.exit(0);
})().catch((e) => {
  console.log('DOCK_ACCEPTANCE_CRASH ' + String((e && e.stack) || e).slice(0, 2500));
  try { fs.writeFileSync(path.join(EV, 'DOCK_ACCEPTANCE_CRASH.log'), String((e && e.stack) || e)); } catch (_) {}
  process.exit(1);
});
