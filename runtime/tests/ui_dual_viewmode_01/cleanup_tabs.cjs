/* ============================================================================
 * cleanup_tabs.cjs — 收掉本任务探针遗留的后台标签页
 *
 * 为什么需要：shadow_acceptance 的标签页是靠 tab.close() 关的；如果进程被中断
 * （超时/取消），WebSocket 先死，Target.closeTarget 就永远发不出去，标签页会
 * 留在浏览器里。它不只是脏——它会占住 8010 的单人物会话，让下一轮验收的
 * §10 身份闸门变成"两个会话抢一个形象"的假象。
 *
 *   node cleanup_tabs.cjs            列出 + 关闭所有 7860 页面
 *   node cleanup_tabs.cjs --list     只列不关
 *   node cleanup_tabs.cjs --blank    连 about:blank 一起关（本任务的校验页
 *                                    就是 about:blank；进程被卡住时它不会被关）
 * ==========================================================================*/
'use strict';
const http = require('node:http');

const CDP = process.env.UIVM_CDP || 'http://127.0.0.1:9110';
const ONLY_7860 = true;      // 只碰本项目端口，绝不动用户自己的标签页
const ALSO_BLANK = process.argv.includes('--blank');

function getJson(url, timeoutMs = 6000) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (c) => {
        body += c;
        if (body.length > 2 * 1024 * 1024) { req.destroy(); reject(new Error('TOO_LARGE')); }
      });
      res.on('end', () => {
        try { resolve(JSON.parse(body)); } catch (e) { reject(new Error('PARSE ' + e.message)); }
      });
    });
    req.on('error', reject);
    req.setTimeout(timeoutMs, () => { req.destroy(); reject(new Error('HTTP_TIMEOUT')); });
  });
}

(async () => {
  const v = await getJson(CDP + '/json/version');
  const ws = new WebSocket(v.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    const t = setTimeout(() => rej(new Error('WS_OPEN_TIMEOUT')), 8000);
    ws.onopen = () => { clearTimeout(t); res(); };
    ws.onerror = () => { clearTimeout(t); rej(new Error('WS_OPEN_ERROR')); };
  });

  let nextId = 1;
  const pending = new Map();
  ws.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch (_) { return; }
    if (m.id != null && pending.has(m.id)) {
      const { resolve, reject, timer } = pending.get(m.id);
      pending.delete(m.id);
      clearTimeout(timer);
      if (m.error) reject(new Error('CDP ' + JSON.stringify(m.error).slice(0, 300)));
      else resolve(m.result);
    }
  };
  const send = (method, params) => new Promise((resolve, reject) => {
    const id = nextId++;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error('CDP_TIMEOUT ' + method));
    }, 8000);
    pending.set(id, { resolve, reject, timer });
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });

  const before = await getJson(CDP + '/json/list');
  const isMine = (t) => t.type === 'page'
    && (/127\.0\.0\.1:7860/.test(t.url || '') || (ALSO_BLANK && t.url === 'about:blank'));
  const pages = before.filter(isMine);
  console.log('targets total =', before.length, '| mine(7860', ALSO_BLANK ? '+blank' : '', ') =', pages.length);
  for (const t of before) {
    console.log(`  [${t.type}] ${(t.title || '').slice(0, 44).padEnd(44)} ${String(t.url).slice(0, 62)}`);
  }

  if (process.argv.includes('--list')) { ws.close(); return; }

  for (const t of pages) {
    try {
      await send('Target.closeTarget', { targetId: t.id });
      console.log('CLOSED', t.id, t.url);
    } catch (e) {
      console.log('CLOSE_FAIL', t.id, String(e.message).slice(0, 160));
    }
  }
  await new Promise((r) => setTimeout(r, 1500));
  const after = await getJson(CDP + '/json/list');
  console.log('after: total =', after.length,
    '| 7860 pages =', after.filter((t) => t.type === 'page' && /7860/.test(t.url || '')).length);
  ws.close();
})().catch((e) => { console.error('CLEANUP_FAIL', String(e && e.message).slice(0, 400)); process.exitCode = 1; });
