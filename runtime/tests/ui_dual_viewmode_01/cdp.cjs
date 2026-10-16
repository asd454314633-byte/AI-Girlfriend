/* ============================================================================
 * cdp.cjs — 零依赖 CDP 客户端（只用 Node 内置 fetch + 全局 WebSocket）
 *
 * 为什么自己写：本任务 §0 禁止安装依赖，而 package.json 里的 @playwright/test
 * 并没有 node_modules。BrowserOS 的 loopback DevTools 端点是现成的只读接入面。
 *
 * 安全阀（教训：自写解析器必须有硬上限）：
 *   · 每条响应文本/ base64 长度封顶 MAX_STR / MAX_B64，超限截断并标记
 *   · 每次调用有超时，超时即 reject，不无限等待
 *   · WebSocket 关闭时把所有挂起调用一起 reject，不留僵尸 promise
 * ==========================================================================*/
'use strict';

const http = require('node:http');

const CDP_ENDPOINT = process.env.UIVM_CDP || 'http://127.0.0.1:9110';
const MAX_STR = 20000;
const MAX_B64 = 4 * 1024 * 1024;

function getJson(url, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (c) => {
        body += c;
        if (body.length > 4 * 1024 * 1024) { req.destroy(); reject(new Error('CDP_JSON_TOO_LARGE')); }
      });
      res.on('end', () => {
        try { resolve(JSON.parse(body)); } catch (e) { reject(new Error('CDP_JSON_PARSE_FAIL ' + e.message)); }
      });
    });
    req.on('error', reject);
    req.setTimeout(timeoutMs, () => { req.destroy(); reject(new Error('CDP_HTTP_TIMEOUT ' + url)); });
  });
}

class Conn {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.events = [];        // recorded raw events (bounded)
    this.eventHandlers = new Set();
    this.maxEvents = 20000;
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (msg.id != null && this.pending.has(msg.id)) {
        const { resolve, reject, timer } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        clearTimeout(timer);
        if (msg.error) reject(new Error('CDP_ERR ' + JSON.stringify(msg.error).slice(0, 400)));
        else resolve(msg.result);
        return;
      }
      if (msg.method) {
        if (this.events.length < this.maxEvents) this.events.push(msg);
        for (const h of this.eventHandlers) { try { h(msg); } catch (_) { /* handler must not kill the pipe */ } }
      }
    };
    ws.onclose = () => {
      for (const [, p] of this.pending) { clearTimeout(p.timer); p.reject(new Error('CDP_CLOSED')); }
      this.pending.clear();
    };
  }

  send(method, params = {}, sessionId, timeoutMs = 30000) {
    const id = this.nextId++;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error('CDP_TIMEOUT ' + method + ' after ' + timeoutMs + 'ms'));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      try { this.ws.send(JSON.stringify(payload)); }
      catch (e) { clearTimeout(timer); this.pending.delete(id); reject(e); }
    });
  }

  onEvent(fn) { this.eventHandlers.add(fn); return () => this.eventHandlers.delete(fn); }
  close() { try { this.ws.close(); } catch (_) {} }
}

async function connect() {
  const v = await getJson(CDP_ENDPOINT + '/json/version');
  if (!v || !v.webSocketDebuggerUrl) throw new Error('NO_CDP_ENDPOINT at ' + CDP_ENDPOINT);
  const ws = new WebSocket(v.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('CDP_WS_OPEN_TIMEOUT')), 8000);
    ws.onopen = () => { clearTimeout(t); resolve(); };
    ws.onerror = () => { clearTimeout(t); reject(new Error('CDP_WS_OPEN_ERROR')); };
  });
  return new Conn(ws);
}

/** A single throwaway tab with its own flat CDP session. */
class Tab {
  constructor(conn, targetId, sessionId) {
    this.conn = conn;
    this.targetId = targetId;
    this.sessionId = sessionId;
    this.requests = [];      // Network.requestWillBeSent records
    this.exceptions = [];    // Runtime.exceptionThrown / Log.error records
    this._off = null;
  }

  static async create(conn, url) {
    const t = await conn.send('Target.createTarget', { url, background: true });
    const a = await conn.send('Target.attachToTarget', { targetId: t.targetId, flatten: true });
    const tab = new Tab(conn, t.targetId, a.sessionId);
    tab._off = conn.onEvent((msg) => {
      if (msg.sessionId !== tab.sessionId) return;
      const p = msg.params || {};
      if (msg.method === 'Network.requestWillBeSent') {
        p._status = null;
        tab.requests.push(p);
      } else if (msg.method === 'Network.responseReceived') {
        // 把状态码回填到那条请求上：判"布局动作有没有招来 4xx/5xx"需要成对事实，
        // 只有 requestWillBeSent 的话只能看到"发了"，看不到"被怎么拒了"。
        const hit = tab.requests.find((r) => r.requestId === p.requestId);
        if (hit) hit._status = (p.response && p.response.status) || null;
      } else if (msg.method === 'Network.loadingFailed') {
        const hit = tab.requests.find((r) => r.requestId === p.requestId);
        if (hit && !hit._status) hit._status = 'FAILED:' + String(p.errorText || '').slice(0, 60);
      }
      // 未捕获异常必须靠 CDP 事件收：页面里的 try/catch 看不见别处抛的东西，
      // 而且每次 navigate 都会把页面侧的收集器冲掉。
      // 两类要分开存：JS 抛错才是"页面崩了"；资源 404/405 是网络层事实，
      // 混在一起会让 §21 的"无 JS error"判定被不相干的资源状态码污染。
      else if (msg.method === 'Runtime.exceptionThrown') {
        const d = p.exceptionDetails || {};
        const txt = (d.exception && (d.exception.description || d.exception.value)) || d.text || 'unknown';
        tab.exceptions.push({ kind: 'JS_EXCEPTION', at: Date.now(),
          url: String(d.url || '').slice(0, 200), text: String(txt).slice(0, 400) });
      } else if (msg.method === 'Log.entryAdded' && p.entry && p.entry.level === 'error') {
        const e = p.entry;
        tab.exceptions.push({ kind: 'LOG_ERROR', source: String(e.source || '').slice(0, 40),
          url: String(e.url || '').slice(0, 220), text: String(e.text || '').slice(0, 300),
          at: Date.now() });
      }
    });
    await tab.send('Page.enable');
    await tab.send('Network.enable', { maxTotalBufferSize: 5 * 1024 * 1024 });
    await tab.send('Runtime.enable');
    await tab.send('Log.enable').catch(() => {});
    // keep the renderer alive & laid out even though the tab is in background
    try { await conn.send('Target.sendMessageToTarget', {}, tab.targetId); } catch (_) {}
    try { await tab.send('Page.setWebLifecycleState', { state: 'active' }); } catch (_) {}
    return tab;
  }

  /** 【截图专用】开一个独立新窗口里的标签页。
   *
   * 为什么不能用 Tab.create 的后台标签页截图（capture_debug_oneshot 实测）：
   *   · 后台标签页 + fromSurface:true  → Page.captureScreenshot 25s 超时（没有帧）
   *   · 后台标签页 + fromSurface:false → 秒回一张【别的 surface】：注入满屏洋红后
   *     洋红占比仍是 0，平均亮度 90（本页是 5），即拍到的根本不是这个 document。
   *     上一批 LIVE_PORTRAIT_*.png 就是这么来的——能看、但是错的。
   *   · 独立新窗口 + fromSurface:true  → 干净图 154KB/亮度 5.2，注入洋红后占比
   *     0.99583，撤掉复原 ⇒ 唯一可信通道。
   * 代价：会短暂抢一次前台焦点；脚本结束必须 closeTarget 把窗口带走。
   */
  static async createInNewWindow(conn, url, w = 1920, h = 1080) {
    const t = await conn.send('Target.createTarget', { url, newWindow: true, background: false });
    const a = await conn.send('Target.attachToTarget', { targetId: t.targetId, flatten: true });
    const tab = new Tab(conn, t.targetId, a.sessionId);
    tab._off = conn.onEvent((msg) => {
      if (msg.sessionId !== tab.sessionId) return;
      if (msg.method === 'Network.requestWillBeSent') {
        const p = msg.params;
        p._status = null;
        tab.requests.push(p);
      } else if (msg.method === 'Network.responseReceived') {
        const hit = tab.requests.find((r) => r.requestId === msg.params.requestId);
        if (hit) hit._status = (msg.params.response && msg.params.response.status) || null;
      } else if (msg.method === 'Runtime.exceptionThrown') {
        const d = msg.params.exceptionDetails || {};
        const txt = (d.exception && (d.exception.description || d.exception.value)) || d.text || 'unknown';
        tab.exceptions.push({ kind: 'JS_EXCEPTION', at: Date.now(),
          url: String(d.url || '').slice(0, 200), text: String(txt).slice(0, 400) });
      } else if (msg.method === 'Log.entryAdded' && msg.params.entry && msg.params.entry.level === 'error') {
        const e = msg.params.entry;
        tab.exceptions.push({ kind: 'LOG_ERROR', source: String(e.source || '').slice(0, 40),
          url: String(e.url || '').slice(0, 220), text: String(e.text || '').slice(0, 300),
          at: Date.now() });
      }
    });
    await tab.send('Page.enable');
    await tab.send('Network.enable', { maxTotalBufferSize: 5 * 1024 * 1024 });
    await tab.send('Runtime.enable');
    await tab.send('Log.enable').catch(() => {});
    await tab.setViewport(w, h);
    return tab;
  }

  send(method, params = {}, timeoutMs = 30000) {
    return this.conn.send(method, params, this.sessionId, timeoutMs);
  }

  async navigate(url, settleMs = 1500) {
    await this.send('Page.navigate', { url });
    await new Promise((r) => setTimeout(r, settleMs));
    for (let i = 0; i < 40; i++) {
      const st = await this.js('document.readyState');
      if (st === 'complete') break;
      await new Promise((r) => setTimeout(r, 250));
    }
    await new Promise((r) => setTimeout(r, settleMs));
  }

  async setViewport(w, h) {
    await this.send('Emulation.setDeviceMetricsOverride',
      { width: w, height: h, deviceScaleFactor: 1, mobile: false });
    await new Promise((r) => setTimeout(r, 400));
  }

  /** Evaluate JS and return the by-value result. Throws on page-side throw. */
  async js(expression, timeoutMs = 20000) {
    const r = await this.send('Runtime.evaluate', {
      expression, returnByValue: true, awaitPromise: true, allowUnsafeEvalBlocklist: false,
    }, timeoutMs);
    if (r.exceptionDetails) {
      const d = r.exceptionDetails;
      const txt = (d.exception && (d.exception.description || d.exception.value)) || d.text || 'unknown';
      throw new Error('PAGE_JS_THROW ' + String(txt).slice(0, 600));
    }
    let v = r.result && r.result.value;
    if (typeof v === 'string' && v.length > MAX_STR) v = v.slice(0, MAX_STR) + '…[TRUNC]';
    return v;
  }

  async screenshot(path, opts = {}) {
    const fs = require('node:fs');
    /* 必须能强制 fromSurface:false。实测教训：后台标签页上 fromSurface:true
     * 会"成功返回一张 PNG"，但拍的是别的 surface —— 上一版就拿到了 ComfyUI
     * 窗口的像素，还自以为是本页截图，属于最坏的一类假证据（能看、但是错的）。
     * opts.fromSurface 显式给出时一律照办，不再自动降级试探。 */
    const params = { format: 'png' };
    if (opts.beyond !== undefined) params.captureBeyondViewport = !!opts.beyond;
    else params.captureBeyondViewport = false;
    if (opts.fromSurface !== undefined) params.fromSurface = !!opts.fromSurface;
    let r;
    try {
      r = await this.send('Page.captureScreenshot', params, opts.timeout || 30000);
    } catch (e) {
      if (opts.fromSurface === undefined) {
        r = await this.send('Page.captureScreenshot',
          { format: 'png', fromSurface: false });
      } else throw e;
    }
    const b64 = r && r.data;
    if (!b64) throw new Error('SCREENSHOT_EMPTY ' + path);
    if (b64.length > MAX_B64) throw new Error('SCREENSHOT_TOO_LARGE ' + b64.length);
    const buf = Buffer.from(b64, 'base64');
    if (path === ':buffer') return buf;
    fs.writeFileSync(path, buf);
    return buf.length;
  }

  /** 截图通道的归属自检 —— 数像素，不是比哈希。
   *
   * 为什么换掉哈希法：本页面里有一个正在播放的真实数字人视频，任意两次捕获
   * 的像素【本来就该不同】，所以"哈希变了"证明不了任何事 —— 拍到别的窗口、
   * 拍到整块桌面、甚至拍到一张全黑的 surface，哈希一样会变。那是假闸门。
   *
   * 现在往【本 target】注入满屏纯洋红，再解码捕获结果数洋红占比：
   *   ratio ≈ 1        ⇒ 像素确实来自这个 document（正向证明）
   *   ratio ≈ 0        ⇒ 拍的是别的 surface（上一版的故障形态）
   *   0 < ratio < 0.95 ⇒ 拍的是整块桌面/含别的窗口 ⇒ 同样不可用作视觉验收
   */
  async verifyCaptureTargetsThisDocument(opts = { fromSurface: true }) {
    const P = require('./png_probe.cjs');
    const dec = (b) => { try { return P.decode(b); } catch (e) { return { error: e.message }; } };
    const mag = (d) => (d.error ? null : P.fraction(d, P.isMagenta, 8).ratio);
    const dark = (d) => (d.error ? null : P.fraction(d, P.isNotBlack, 8).ratio);
    /* 覆盖方式：往 body 末尾插一个满屏纯洋红 div（z-index 顶格）。
     * 不用"改 body 背景"那种更轻的写法，是因为本页面有 inset:0 的不透明层会把它
     * 完全盖住 —— 那样占比恒为 0，证明会变成自我否证。插 div 是一次 childList
     * 变更，可能触发页面里既有的 MutationObserver；本文件把它记进证据，
     * 循环/异常归属仍以 shadow_acceptance 的 R/T 两项为准。 */
    const without = await this.screenshot(':buffer', opts);
    await this.js(`(() => { const d = document.createElement('div');
      d.id='__cap_proof'; d.style.cssText='position:fixed;inset:0;z-index:2147483647;background:rgb(255,0,255)';
      document.body.appendChild(d); return 1; })()`);
    const withMarker = await this.screenshot(':buffer', opts);
    await this.js(`(() => { const d = document.getElementById('__cap_proof'); if (d) d.remove(); return 1; })()`);
    const after = await this.screenshot(':buffer', opts);
    const dw = dec(without), dm = dec(withMarker), da = dec(after);
    const mBefore = mag(dw), mMark = mag(dm), mAfter = mag(da);
    return {
      capture_params: opts,
      proof: 'inject full-screen #ff00ff overlay -> capture -> remove -> capture',
      bytes: [without.length, withMarker.length, after.length],
      decode_error: dw.error || dm.error || da.error || null,
      dims: dw.error ? null : [dw.width, dw.height],
      sha: [P.sha(without), P.sha(withMarker), P.sha(after)],
      magenta_ratio: { before: mBefore, with_marker: mMark, after: mAfter },
      notblack_ratio: { before: dark(dw), with_marker: dark(dm) },
      reflects_this_document: mMark !== null && mMark >= 0.95 && mBefore !== null && mBefore <= 0.005,
      marker_cleaned_up: mAfter !== null && mAfter <= 0.005,
    };
  }

  clearRequests() { this.requests.length = 0; }

  async wait(expr, timeoutMs = 15000, stepMs = 300) {
    const t0 = Date.now();
    for (;;) {
      let v = null;
      try { v = await this.js(expr, 8000); } catch (_) { v = null; }
      if (v) return v;
      if (Date.now() - t0 > timeoutMs) return false;
      await new Promise((r) => setTimeout(r, stepMs));
    }
  }

  async close() {
    if (this._off) { try { this._off(); } catch (_) {} }
    try { await this.conn.send('Target.closeTarget', { targetId: this.targetId }); } catch (_) {}
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

module.exports = { connect, Tab, sleep, CDP_ENDPOINT, MAX_STR };
