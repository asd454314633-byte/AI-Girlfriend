# -*- coding: utf-8 -*-
"""SHORT_AUDIO_REGRESSION 补测：短回复走完整 avatar 链并等完整收口。

前置：8765 pool 空闲。链路：7860 → 8765 → qwen3 TTS → router → /humanaudio → 8010。
采集：TTFA（send → 首字节被 8010 确认）、完整 AUDIO_DONE、关页后 pool 释放。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fa_cdp  # noqa: E402
from fa_longaudio_final import pool_state, wait_pool_idle  # noqa: E402

SHORT_TEXT = '你好呀，用一句话介绍一下你自己吧。'

JS_ENV = r"""
(() => ({
  href: location.href,
  composer: !!document.getElementById('gf-composer-input'),
  routerReady: typeof window.__audioRouterDiagnostics === 'function',
}))()
"""

JS_SEND = r"""
((text) => {
  const ta = document.getElementById('gf-composer-input');
  if (!ta) return { ok: false, error: 'no_composer' };
  ta.value = text;
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  window.__srSendPerf = performance.now();
  ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
  return { ok: true, sendPerf: window.__srSendPerf };
})(%TEXT%)
"""

JS_POLL = r"""
(() => {
  if (typeof window.__audioRouterDiagnostics !== 'function') return {};
  const d = window.__audioRouterDiagnostics();
  const known = window.__srKnownIds || (window.__srKnownIds = new Set());
  const newlyDone = [];
  for (const t of d.lastTurns) {
    if (!known.has(t.responseId)) {
      known.add(t.responseId);
      if (t.state === 'AUDIO_DONE' && t.bytes > 0) newlyDone.push(t);
    }
  }
  return {
    newlyDone,
    open: d.openTurns.map(t => ({ id: t.responseId.slice(-8), bytes: t.bytes, confirmed: t.confirmedBytes, state: t.state })),
    sinks: d.sinks,
    counters: d.counters,
    recent: d.recent || [],
  };
})()
"""


async def drive():
    out = {}
    idle, waited = wait_pool_idle(600, poll_s=10)
    if not idle:
        return {'ok': False, 'error': 'POOL_BUSY'}
    p = await fa_cdp.Page.attach()
    try:
        await p.navigate('http://127.0.0.1:7860/')
        await asyncio.sleep(6)
        env = await p.eval_js(JS_ENV)
        if not env.get('composer') or not env.get('routerReady'):
            return {'ok': False, 'error': 'PAGE_NOT_READY', 'env': env}
        # 预热：等 avatar sink available（首轮 WebRTC 建立）
        send1 = await p.eval_js(JS_SEND.replace('%TEXT%', '"在吗？"'))
        avatar_ready = False
        t0 = time.time()
        while time.time() - t0 < 90:
            await asyncio.sleep(2)
            d = await p.eval_js(JS_POLL)
            if any(s.get('name') == 'avatar' and s.get('available') for s in (d.get('sinks') or [])):
                avatar_ready = True
                break
        out['avatar_ready'] = avatar_ready
        if not avatar_ready:
            return {'ok': False, 'error': 'AVATAR_NOT_READY', **out}
        # 等预热回合完全收口（open 清空），保证此后首个 newlyDone = 正式短回复
        t0 = time.time()
        while time.time() - t0 < 60:
            await asyncio.sleep(1)
            d = await p.eval_js(JS_POLL)
            if not d.get('open'):
                break

        # 正式短回复
        send = await p.eval_js(JS_SEND.replace('%TEXT%', json.dumps(SHORT_TEXT, ensure_ascii=False)))
        out['send'] = send
        send_perf = send['sendPerf']
        ttfa = None
        done = None
        t0 = time.time()
        while time.time() - t0 < 120:
            await asyncio.sleep(0.4)
            d = await p.eval_js(JS_POLL)
            if d.get('newlyDone'):
                done = d['newlyDone'][-1]
                # TTFA：decisions 里该回合第一条 AUDIO_PLAYING 的时刻
                for ev in d.get('recent', []):
                    if ev.get('what') == 'turn_state' and ev.get('state') == 'AUDIO_PLAYING' \
                       and ev.get('responseId') == done.get('responseId'):
                        ttfa = round((ev['at'] - send_perf) / 1000, 2)
                        break
                break
        out['ttfa_s'] = ttfa
        out['done'] = done
        out['counters_after'] = (await p.eval_js(JS_POLL)).get('counters')
        # 关页 → 会话释放
        try:
            await p.eval_js('window.close(); 1', await_promise=False)
        except Exception:
            pass
        return out
    finally:
        await p.close()


def main():
    out = asyncio.run(drive())
    if 'error' in out:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 1
    idle, waited = wait_pool_idle(180, poll_s=6)
    out['pool_released'] = idle
    out['pool_release_wait_s'] = round(waited, 1)
    out['pool_after'] = pool_state()
    out['ok'] = bool(out.get('done') and out['done'].get('state') == 'AUDIO_DONE'
                     and out['done'].get('audible') and not out['done'].get('trunc')
                     and out.get('ttfa_s') is not None and out.get('pool_released'))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
