/* live_probe.cjs — 只读实测：人物真实上线后的舞台几何（不改任何生产文件）。
 * 用一次性后台标签页派发一次真实点击，触发 avatar-sync 既有的 kick→connect。 */
'use strict';
const { connect, Tab, sleep } = require('./cdp.cjs');
const path = require('node:path');
const EV = path.join(__dirname, '..', '..', 'evidence', 'UI_DUAL_VIEWMODE_01', 'prechange');

(async () => {
  const conn = await connect();
  const tab = await Tab.create(conn, 'about:blank');
  await tab.setViewport(1920, 1080);
  await tab.navigate('http://127.0.0.1:7860/', 2500);

  const before = await tab.js(`({srcObject:!!(document.getElementById('av-video')||{}).srcObject,
    layerClass:(document.getElementById('av-layer')||{}).className})`);
  console.log('before click:', JSON.stringify(before));

  // 触发 avatar-sync 自己的既有 kick 监听器（document capture 阶段 click）。
  // 注：后台标签页收不到 CDP 可信输入事件（Chrome background posture 限制），
  //     所以这里派发 DOM 合成事件。这是测试夹具动作，不改任何生产代码路径。
  const kicked = await tab.js(`(() => {
     document.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}));
     return true; })()`);
  console.log('synthetic click dispatched =', kicked);

  const got = await tab.wait(`(() => { const v=document.getElementById('av-video');
     return !!(v && v.srcObject && v.videoWidth>0); })()`, 60000, 1000);
  console.log('srcObject+intrinsic arrived =', got);

  const m = await tab.js(`(() => {
    const v=document.getElementById('av-video'), layer=document.getElementById('av-layer');
    const r=v.getBoundingClientRect(), lr=layer.getBoundingClientRect();
    const s=getComputedStyle(v), ls=getComputedStyle(layer);
    const tracks = v.srcObject ? v.srcObject.getVideoTracks().map(t=>({readyState:t.readyState,label:t.kind})) : null;
    return {
      videoWidth:v.videoWidth, videoHeight:v.videoHeight, intrinsicRatio:v.videoWidth?+(v.videoWidth/v.videoHeight).toFixed(4):null,
      rect:{x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)},
      layerRect:{w:Math.round(lr.width),h:Math.round(lr.height)},
      computed:{objectFit:s.objectFit, objectPosition:s.objectPosition, width:s.width, height:s.height,
                right:s.right, left:s.left, top:s.top, transform:s.transform, transformOrigin:s.transformOrigin},
      layerClass:layer.className, layerOpacity:ls.opacity,
      currentTime:+v.currentTime.toFixed(3), readyState:v.readyState, paused:v.paused,
      srcObjectTag: v.srcObject ? v.srcObject.constructor.name : null,
      trackCount: v.srcObject ? v.srcObject.getTracks().length : 0, tracks,
      avatarStatusText:(document.getElementById('av-status')||{}).textContent||null,
      avatarStatusState:(document.getElementById('av-status')||{}).dataset ? document.getElementById('av-status').dataset.state : null,
    };
  })()`);
  console.log(JSON.stringify(m, null, 2));

  // currentTime 单调性（真实流才会前进）
  const t0 = await tab.js(`document.getElementById('av-video').currentTime`);
  await sleep(3000);
  const t1 = await tab.js(`document.getElementById('av-video').currentTime`);
  console.log('currentTime t0=', t0, 't1=', t1, 'advanced=', (t1 - t0).toFixed(3), 's over 3s wall');

  const sz = await tab.screenshot(path.join(EV, 'PREFLIGHT_AVATAR_LIVE_1920x1080.png'));
  console.log('screenshot bytes =', sz);

  // 记录本次会话产生的网络请求（区分原有持续流量 vs 布局动作）
  const reqs = tab.requests.map(r => r.request.url).filter(u => !u.includes('fonts.g'));
  console.log('requests during LIVE probe (n=' + reqs.length + '):');
  for (const u of reqs.slice(0, 40)) console.log('   ', u.slice(0, 120));

  await tab.close();
  conn.close();
})().catch((e) => { console.error('LIVE_PROBE_FAIL', e && e.message); process.exitCode = 1; });
