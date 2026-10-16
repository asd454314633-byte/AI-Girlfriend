/* connect_diag.cjs — 只读诊断：为什么点击后没有 /offer。读 AVATAR_CONFIG 与 console。 */
'use strict';
const { connect, Tab, sleep } = require('./cdp.cjs');

const URL_BASE = process.argv[2] || 'http://localhost:7860/';

(async () => {
  const conn = await connect();
  const tab = await Tab.create(conn, 'about:blank');
  const logs = [];
  tab.conn.onEvent((m) => {
    if (m.sessionId !== tab.sessionId || m.method !== 'Runtime.consoleAPICalled') return;
    const txt = (m.params.args || []).map(a => a.value !== undefined ? String(a.value) : (a.description || a.type)).join(' ');
    if (logs.length < 400) logs.push((m.params.type || '') + ': ' + txt.slice(0, 220));
  });
  await tab.send('Runtime.enable');
  await tab.setViewport(1920, 1080);
  await tab.navigate(URL_BASE, 2500);

  console.log('AVATAR_CONFIG =', JSON.stringify(await tab.js(`window.AVATAR_CONFIG || null`)));
  console.log('page origin   =', await tab.js(`location.origin`));

  await tab.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: 960, y: 200, button: 'left', clickCount: 1, buttons: 1 });
  await tab.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: 960, y: 200, button: 'left', clickCount: 1, buttons: 0 });
  await sleep(6000);

  const st = await tab.js(`(() => { const v=document.getElementById('av-video');
    return {srcObject:!!(v&&v.srcObject), vw:v&&v.videoWidth, currentTime:v&&+v.currentTime.toFixed(2),
            layerClass:document.getElementById('av-layer').className,
            statusText:(document.getElementById('av-status')||{}).textContent||null,
            statusState:(document.getElementById('av-status')||{}).dataset?.state||null}; })()`);
  console.log('after click   =', JSON.stringify(st));
  console.log('avatar-related requests:');
  for (const r of tab.requests) {
    const u = r.request.url;
    if (u.includes('8010') || u.includes('offer') || u.includes('humanaudio') || u.includes('ws:') || u.includes(':8765'))
      console.log('   ', r.type, u.slice(0, 130));
  }
  console.log('--- console (avatar/status lines) ---');
  for (const l of logs) if (/avatar|数字人|offer|RTC|error|Error|fail/i.test(l)) console.log('   ', l);
  await tab.close();
  conn.close();
})().catch((e) => { console.error('DIAG_FAIL', e && e.message); process.exitCode = 1; });
