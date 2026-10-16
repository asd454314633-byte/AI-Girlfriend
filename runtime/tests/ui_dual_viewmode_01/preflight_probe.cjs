/* preflight_probe.cjs — 只读实测：现有 DOM 冻结清单 + 人物舞台几何真值。
 * 不写任何生产文件；产物 JSON 落到 evidence/UI_DUAL_VIEWMODE_01/<phase>/。
 *   node preflight_probe.cjs prechange
 *   node preflight_probe.cjs postchange
 * ==========================================================================*/
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { connect, Tab, sleep } = require('./cdp.cjs');

const PHASE = process.argv[2] === 'postchange' ? 'postchange' : 'prechange';
const EV = path.join(__dirname, '..', '..', 'evidence', 'UI_DUAL_VIEWMODE_01');
const OUTDIR = path.join(EV, PHASE);

(async () => {
  const conn = await connect();
  const tab = await Tab.create(conn, 'about:blank');
  await tab.setViewport(1920, 1080);
  await tab.navigate('http://127.0.0.1:7860/', 2500);

  // canary 1: JS 注入通道真的作用在这个 document 上
  const canary = 'PROBE_' + Date.now();
  await tab.js(`document.documentElement.dataset.uiProbeCanary='${canary}';1`);
  const canaryBack = await tab.js(`document.documentElement.dataset.uiProbeCanary`);

  const dom = await tab.js(`(() => {
    const rect = (el) => { if(!el) return null; const r=el.getBoundingClientRect();
      return {x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)}; };
    const comp = (el, props) => { if(!el) return null; const s=getComputedStyle(el); const o={};
      props.forEach(p=>o[p]=String(s[p]).slice(0,80)); return o; };
    const q=(s)=>document.querySelectorAll(s).length;
    const v=document.getElementById('av-video'), layer=document.getElementById('av-layer'),
          app=document.getElementById('app'), stage=document.querySelector('main.stage');
    const KEYS = {
      '人物 video #av-video':'#av-video', '相机小窗 #cam-video':'#cam-video',
      '人物层 #av-layer':'#av-layer', '应用壳 #app':'#app', '主区 main.stage':'main.stage',
      '聊天正文 #gf-live-conversation':'#gf-live-conversation', '本轮状态 #gf-turn-status':'#gf-turn-status',
      '输入/麦克风门 #mic-gate':'#mic-gate', '控制区 .gf-controls':'.gf-controls',
      '控制按钮 #gf-layout-open':'#gf-layout-open', '数字人按钮 #gf-avatar-open':'#gf-avatar-open',
      '数字人挂载 #av-panel-mount':'#av-panel-mount', '状态容器 #gf-status-container':'#gf-status-container',
      '页脚 .footer':'.footer', '顶栏 .topbar':'.topbar', '气泡栈 #bubble-stack':'#bubble-stack',
      '聊天面板 #chat-panel':'#chat-panel', '聊天历史 #chat-history':'#chat-history',
      '设置弹窗 #settings-modal':'#settings-modal', '关于弹窗 #about-modal':'#about-modal',
      '工具弹窗 #tools-modal':'#tools-modal', '人物面板 #char-panel':'#char-panel',
      '数字人面板卡 #av-layout-card':'#av-layout-card', '阅读面板 #reading-panel':'#reading-panel',
      '朗读粘贴 #reading-text':'#reading-text', '人物状态条 #av-status':'#av-status',
      '音频元素 #av-audio':'#av-audio', '语音表现 #gf-voice-performance':'#gf-voice-performance',
    };
    const nodes = {};
    for (const [label, sel] of Object.entries(KEYS)) {
      const el = document.querySelector(sel);
      nodes[label] = { sel, count:q(sel), present:!!el, rect:rect(el) };
    }
    return {
      title:document.title, url:location.href,
      viewport:{w:innerWidth,h:innerHeight,dpr:devicePixelRatio},
      counts:{ video:q('video'), avVideo:q('#av-video'), camVideo:q('#cam-video'),
               audio:q('audio'), object:q('object'), canvas:q('canvas'), iframe:q('iframe') },
      avVideoRect:rect(v), avVideoInner:{vw:v&&v.videoWidth,vh:v&&v.videoHeight,srcObject:!!(v&&v.srcObject),currentTime:v&&v.currentTime},
      avVideoComp:comp(v,['position','objectFit','objectPosition','width','height','top','right','left','transform','transformOrigin','display','maskImage','zIndex']),
      avLayerRect:rect(layer), avLayerComp:comp(layer,['position','inset','width','height','overflow','zIndex','opacity','backgroundColor','pointerEvents']),
      appComp:comp(app,['display','gridTemplateRows','gridTemplateColumns','width','position','zIndex']),
      stageComp:comp(stage,['display','alignItems','justifyContent','width','padding','gap']),
      bodyComp:comp(document.body,['backgroundColor','backgroundImage','minHeight','overflowX']),
      htmlComp:comp(document.documentElement,['backgroundColor']),
      bodyChildren:Array.from(document.body.children).map(e=>e.id?'#'+e.id:(e.tagName.toLowerCase()+'.'+String(e.className||'').split(' ')[0])),
      nodes,
      styleSheets:Array.from(document.styleSheets).map(s=>{try{return (s.href||'INLINE').split('/').pop().split('?')[0]+'('+s.cssRules.length+' rules)'}catch(_){return (s.href||'INLINE')+'(locked)'}}),
      scripts:Array.from(document.scripts).map(s=>s.src?s.src.replace(/^https?:\\/\\/[^/]+/,''):s.type), // HAZARD-OK: 双写反斜杠是有意的，模板求值吃掉一层后页面收到 \/，正是正则需要的形状（page_code_hazards.cjs 复核点）
      localStorageKeys:Object.keys(localStorage).sort(),
    };
  })()`);

  console.log(JSON.stringify({ canary_ok: canaryBack === canary, canary }, null, 2));
  console.log(JSON.stringify(dom, null, 2));

  const sz = await tab.screenshot(path.join(OUTDIR, `PREFLIGHT_DEFAULT_1920x1080.png`));
  console.log('screenshot bytes =', sz);

  fs.mkdirSync(OUTDIR, { recursive: true });
  const outFile = path.join(OUTDIR, 'DOM_BASELINE.json');
  fs.writeFileSync(outFile, JSON.stringify(
    { phase: PHASE, captured_at: new Date().toISOString(), canary_ok: canaryBack === canary,
      viewport: [1920, 1080], dom }, null, 2));
  console.log('wrote', outFile);
  await tab.close();
  conn.close();
})().catch((e) => { console.error('PROBE_FAIL', e && e.message); process.exitCode = 1; });
