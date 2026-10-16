# -*- coding: utf-8 -*-
"""AI_GIRLFRIEND_LONG_AUDIO_TRUNCATION_FINAL_02 — 真实生产长音频端到端终验。

不操作用户页面：使用 9223 CDP 自动化浏览器（fa_profile）新开/复用 7860 标签页。
前置硬条件：8765 pool 必须空闲（in_use=0）才开跑，否则超时退出（绝不抢用户会话）。

链路：7860 → 8765(qwen3 TTS) → browser AudioRouter → /humanaudio → 8010 → WebRTC → 系统扬声器
对账层：8765 stderr 日志 / browser router diagnostics + fetch 账本 / 8010 stderr 日志 / WASAPI per-window loopback。
"""
import asyncio
import json
import re
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fa_cdp  # noqa: E402
import wasapi_loopback as wl  # noqa: E402
import urllib.request  # noqa: E402

S2S_LOG = Path(str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'logs/runtime/s2s.stderr.log'))
AV_LOG = Path(str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'logs/runtime/avatar-8010.stderr.log'))
POOL_URL = 'http://127.0.0.1:8765/v1/pool'

TAIL_SENTENCE = '长语音完整播放验证结束七三九二。'
SENTENCES = [
    '今晚的风把窗台吹得很轻。', '远处的路灯一盏一盏亮起来。', '桌上的茶还冒着细小的热气。',
    '旧照片里的人笑得很安静。', '巷口的猫伸了一个长长的懒腰。', '雨点敲在伞面上像小鼓。',
    '楼下的桂花香飘进了梦。', '火车缓缓驶过夜色的边缘。', '书页翻动时带起一阵微风。',
    '钟摆的声音把房间变得很慢。', '海边的贝壳藏着潮汐的回声。', '萤火虫提着灯笼走过草丛。',
    '面包在烤箱里慢慢膨胀。', '雪落在松枝上积成小小的山。', '风筝在云端写自己的名字。',
    '老屋的木门吱呀一声开了。', '蒲公英乘着风去远方旅行。', '咖啡杯沿留着一道浅浅的印。',
    '琴键上跳出一串透亮的音符。', '沙漏把时间堆成两座小丘。', '灯塔的光扫过沉睡的海面。',
    '竹叶在檐下滴着午后的雨。', '巷子深处传来自行车的铃。', '月亮把影子铺在石板路上。',
    '橘猫踩过键盘留下乱码。', '汽笛声惊起了一群白鸽。', '炉火映红了围着的人脸。',
    '果园里的苹果压弯了枝头。', '清晨的雾还挂在稻田上。', '晚风把晾着的衬衫吹成帆。',
    '信箱里躺着一封没有署名的信。', TAIL_SENTENCE,
]
PROMPT = ('请逐字朗读下面三十二个编号句子：只朗读，不改任何字，不加开头语、结束语或任何评论，'
          '按编号顺序一口气读完。\n' + '\n'.join('%s、%s' % (cn, s) for cn, s in
                                                 zip(['一', '二', '三', '四', '五', '六', '七', '八', '九', '十',
                                                      '十一', '十二', '十三', '十四', '十五', '十六', '十七', '十八',
                                                      '十九', '二十', '二十一', '二十二', '二十三', '二十四', '二十五',
                                                      '二十六', '二十七', '二十八', '二十九', '三十', '三十一', '三十二'],
                                                     SENTENCES)))
SHORT_PROMPT = '你好呀，用一句话介绍一下你自己吧。'

JS_ENV = r"""
(() => {
  const res = (performance.getEntriesByType('resource') || []).map(r => r.name);
  return {
    href: location.href,
    routerVer: res.filter(n => n.includes('audio-router')),
    composer: !!document.getElementById('gf-composer-input'),
    histMsgs: document.querySelectorAll('.hist-msg').length,
    routerReady: typeof window.__audioRouterDiagnostics === 'function',
  };
})()
"""

JS_INSTALL = r"""
(() => {
  if (window.__humLedger) return 'already';
  const of = window.fetch;
  window.__humLedger = [];
  window.fetch = async function (input, init) {
    const url = (typeof input === 'string') ? input : (input && input.url) || '';
    if (init && init.method === 'POST' && /\/humanaudio/.test(url)) {
      const rec = { url, at: Date.now(), wavBytes: 0, responseId: '', segmentIndex: null, code: null, ok: false };
      try {
        const fd = init.body;
        if (typeof FormData !== 'undefined' && fd instanceof FormData) {
          for (const [k, v] of fd.entries()) {
            if (k === 'file' && v && v.size != null) rec.wavBytes = v.size;
            else if (k === 'response_id') rec.responseId = v;
            else if (k === 'segment_index') rec.segmentIndex = Number(v);
          }
        }
      } catch (e) {}
      window.__humLedger.push(rec);
      const r = await of.call(this, input, init);
      rec.ok = r.ok;
      try { const j = await r.clone().json(); rec.code = j && j.code; } catch (e) {}
      rec.at2 = Date.now();
      return r;
    }
    return of.call(this, input, init);
  };
  return 'installed';
})()
"""

JS_SEND = r"""
((text) => {
  const ta = document.getElementById('gf-composer-input');
  if (!ta) return { ok: false, error: 'no_composer' };
  ta.value = text;
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  window.__laSendPerf = performance.now();
  ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
  return { ok: true, sendPerf: window.__laSendPerf };
})(%TEXT%)
"""

JS_DIAG = r"""
(() => {
  if (typeof window.__audioRouterDiagnostics !== 'function') return null;
  const d = window.__audioRouterDiagnostics();
  return {
    counters: d.counters,
    sinks: d.sinks,
    active: d.activeResponseId,
    open: d.openTurns.map(t => ({ id: t.responseId.slice(-8), bytes: t.bytes, confirmed: t.confirmedBytes,
                                  state: t.state, ended: t.ended })),
    last: d.lastTurns.map(t => ({ id: t.responseId, bytes: t.bytes, played: t.playedBytes, state: t.state,
                                  audible: t.audible, trunc: !!t.truncatedAtGuard })),
    firstPlayingAt: (() => {
      const evs = (d.recent || []).filter(e => e.what === 'turn_state' && e.state === 'AUDIO_PLAYING');
      return evs.length ? evs[0].at : null;
    })(),
  };
})()
"""

JS_TEXT_TAIL = r"""
(() => {
  const msgs = [...document.querySelectorAll('.hist-msg')].filter(e => e.className.includes('assistant'));
  const last = msgs.length ? msgs[msgs.length - 1].textContent.trim() : '';
  return { n: msgs.length, len: last.length, tail: last.slice(-60) };
})()
"""


def pool_state():
    with urllib.request.urlopen(POOL_URL, timeout=5) as r:
        return json.load(r)


def wait_pool_idle(timeout_s, poll_s=15):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        p = pool_state()
        if p.get('in_use', 1) == 0:
            return True, time.time() - t0
        time.sleep(poll_s)
    return False, timeout_s


def record_loopback_pw(seconds, stop_event):
    """Per-window RMS loopback（wasapi_loopback 的累计 RMS 无法定位停止时刻，故自建）。"""
    ole32 = wl.ole32
    PVOID = wl.PVOID
    ole32.CoInitializeEx(None, 0)
    enumerator = PVOID()
    cls = (__import__('ctypes').c_ubyte * 16).from_buffer_copy(wl.CLSID_MMDeviceEnumerator)
    iid = (__import__('ctypes').c_ubyte * 16).from_buffer_copy(wl.IID_IMMDeviceEnumerator)
    hr = ole32.CoCreateInstance(__import__('ctypes').byref(cls), None, 1, __import__('ctypes').byref(iid),
                                __import__('ctypes').byref(enumerator))
    assert hr == 0, 'CoCreateInstance 0x%08X' % (hr & 0xFFFFFFFF)
    dev = PVOID()
    hr = wl.vtbl_fn(enumerator, 4, wl._GetDefaultAudioEndpoint)(enumerator, 0, 0, __import__('ctypes').byref(dev))
    assert hr == 0, 'GetDefaultAudioEndpoint 0x%08X' % (hr & 0xFFFFFFFF)
    client = PVOID()
    iid_ac = (__import__('ctypes').c_ubyte * 16).from_buffer_copy(wl.IID_IAudioClient)
    hr = wl.vtbl_fn(dev, 3, wl._Activate)(dev, iid_ac, 4, None, __import__('ctypes').byref(client))
    assert hr == 0, 'Activate 0x%08X' % (hr & 0xFFFFFFFF)
    fmt_ptr = PVOID()
    hr = wl.vtbl_fn(client, 8, wl._GetMixFormat)(client, __import__('ctypes').byref(fmt_ptr))
    assert hr == 0, 'GetMixFormat 0x%08X' % (hr & 0xFFFFFFFF)
    fmt = __import__('ctypes').cast(fmt_ptr, __import__('ctypes').POINTER(wl.WAVEFORMATEX))[0]
    is_float = (fmt.wFormatTag == 3) or (fmt.wFormatTag == 0xFFFE and fmt.wBitsPerSample == 32)
    hr = wl.vtbl_fn(client, 3, wl._Initialize)(client, 0, 0x00020000, 10000000, 0, fmt_ptr, None)
    assert hr == 0, 'Initialize 0x%08X' % (hr & 0xFFFFFFFF)
    capture = PVOID()
    iid_cc = (__import__('ctypes').c_ubyte * 16).from_buffer_copy(wl.IID_IAudioCaptureClient)
    hr = wl.vtbl_fn(client, 14, wl._GetService)(client, iid_cc, __import__('ctypes').byref(capture))
    assert hr == 0, 'GetService 0x%08X' % (hr & 0xFFFFFFFF)
    hr = wl.vtbl_fn(client, 10, wl._Start)(client)
    assert hr == 0, 'Start 0x%08X' % (hr & 0xFFFFFFFF)

    import numpy as np
    windows = []          # (t_rel, per_window_rms)
    buf = bytearray()
    consumed = 0
    t0 = time.time()
    next_window = 0.0
    try:
        while time.time() - t0 < seconds and not stop_event.is_set():
            time.sleep(0.02)
            packets = __import__('ctypes').c_uint32(0)
            while True:
                hr = wl.vtbl_fn(capture, 5, wl._GetNextPacketSize)(capture, __import__('ctypes').byref(packets))
                if hr != 0 or packets.value == 0:
                    break
                data_ptr = PVOID(); nframes = __import__('ctypes').c_uint32(0); flags = __import__('ctypes').c_uint32(0)
                dev_pos = __import__('ctypes').c_uint64(0); qpc = __import__('ctypes').c_uint32(0)
                hr = wl.vtbl_fn(capture, 3, wl._GetBuffer)(capture, __import__('ctypes').byref(data_ptr),
                                                           __import__('ctypes').byref(nframes), __import__('ctypes').byref(flags),
                                                           __import__('ctypes').byref(dev_pos), __import__('ctypes').byref(qpc))
                if hr != 0 or not data_ptr.value:
                    break
                buf += __import__('ctypes').string_at(data_ptr.value, nframes.value * fmt.nBlockAlign)
                wl.vtbl_fn(capture, 4, wl._ReleaseBuffer)(capture, nframes.value)
            now = time.time() - t0
            if now >= next_window:
                delta = bytes(buf[consumed:])
                consumed = len(buf)
                rms = 0.0
                if delta:
                    arr = np.frombuffer(delta, dtype=np.float32 if is_float else np.int16)
                    if not is_float:
                        arr = arr.astype(np.float32) / 32768.0
                    mono = arr.reshape(-1, fmt.nChannels).mean(axis=1)
                    rms = float(np.sqrt((mono ** 2).mean()))
                windows.append((round(now, 2), round(rms, 5)))
                next_window += 0.1
    finally:
        wl.vtbl_fn(client, 11, wl._Stop)(client)
    return {'windows': windows, 'rate': fmt.nSamplesPerSec, 'channels': fmt.nChannels,
            'fmt': 'float32' if is_float else 'int%d' % fmt.wBitsPerSample, 't0_wall': t0}


def analyze_loopback(lb, router_done_rel):
    wins = lb['windows']
    active = [(t, r) for t, r in wins if r > 0.002]
    first_active = active[0][0] if active else None
    last_active = active[-1][0] if active else None
    span = (last_active - first_active) if (first_active is not None and last_active is not None) else None
    # router done 之后是否还有活动（=真正的扬声器回放在 router 收口之后继续）
    active_after_done = [t for t, r in active if t > router_done_rel - 0.5]
    return {'first_active': first_active, 'last_active': last_active, 'active_span': span,
            'active_windows': len(active), 'total_windows': len(wins),
            'active_after_router_done': len(active_after_done),
            'last_active_after_router_done': (active_after_done[-1] if active_after_done else None)}


def tail_log(path, start_offset):
    if not path.exists():
        return ''
    with open(path, 'rb') as f:
        f.seek(start_offset)
        return f.read().decode('utf-8', errors='replace')


def parse_s2s_block(text, expect_len=None):
    """按 text length 匹配目标 response 块的账本（expect_len≈456 为长回复）。"""
    done_positions = [m.start() for m in re.finditer(r'Response done \(status=', text)]
    if not done_positions:
        return None
    blocks = []
    for i, end in enumerate(done_positions):
        start = done_positions[i - 1] if i > 0 else 0
        block = text[start:end + 400]
        gen = [float(x) for x in re.findall(r'generated ([\d.]+)s audio', block)]
        lens = [(int(a), int(b)) for a, b in re.findall(r'text length=(\d+) segments=(\d+)', block)]
        m = re.search(r'Response done \(status=(\w+)\).*?output_tokens=(\d+)', block, re.S)
        blocks.append({'gen_s': round(sum(gen), 2), 'gen_chunks': len(gen),
                       'text_len': lens[-1][0] if lens else None,
                       'segments': lens[-1][1] if lens else None,
                       'status': m.group(1) if m else None,
                       'output_tokens': int(m.group(2)) if m else None})
    if expect_len:
        for b in blocks:
            if b['text_len'] and abs(b['text_len'] - expect_len) <= 60:
                return b
    return blocks[-1]


def parse_av_block(text):
    puts = [(int(a), int(b)) for a, b in re.findall(r'put audio stream (\d+): \((\d+),\)', text)]
    gates = re.findall(r"'response_id': '(\w+)'", text)
    return {'puts': puts[-4:], 'last_gate_ids': [g[-8:] for g in gates[-4:]]}


async def drive():
    out = {'ok': False}
    # 1) pool 必须空闲。用户页面关闭/刷新才会释放（8765 无服务端 idle timeout，
    #    实测 2026-09-26 00:25 会话持续占用中）——长待命：最长等 60 分钟，
    #    槽位一释放立即开跑；超时如实报 BLOCKED，绝不 drain 用户的活跃会话。
    idle, waited = wait_pool_idle(43200, poll_s=20)
    out['pool_wait_s'] = round(waited, 1)
    if not idle:
        out['error'] = 'POOL_BUSY_TIMEOUT_STANDBY'
        return out

    log_off = {p.name: p.stat().st_size for p in (S2S_LOG, AV_LOG) if p.exists()}
    out['log_offsets'] = log_off

    p = await fa_cdp.Page.attach()
    try:
        # 2) 新开/复用 CDP 页面，强制刷新加载 longaudio-02
        await p.navigate('http://127.0.0.1:7860/')
        await asyncio.sleep(6)
        env = await p.eval_js(JS_ENV)
        out['env'] = env
        if not env.get('composer') or not env.get('routerReady'):
            out['error'] = 'PAGE_NOT_READY'
            return out
        out['router_version_loaded'] = env['routerVer']
        out['install'] = await p.eval_js(JS_INSTALL)

        # 2b) avatar 预热：发一条短消息触发 WebRTC 连接，等 avatar sink 变为
        #     available（MEDIA_READY）。否则长回复按冻结的 fail-open 设计走本地
        #     sink，/humanaudio→8010 链路测不到（首轮实测复现了这一点）。
        warm = await p.eval_js(JS_SEND.replace('%TEXT%', '"你好，请用一句话打个招呼。"'))
        out['warmup_send'] = warm
        avatar_ready = False
        t0 = time.time()
        while time.time() - t0 < 90:
            await asyncio.sleep(2)
            d = await p.eval_js(JS_DIAG)
            sinks = (d or {}).get('sinks') or []
            if any(s.get('name') == 'avatar' and s.get('available') for s in sinks):
                avatar_ready = True
                break
        out['avatar_ready'] = avatar_ready
        # 清空预热回合的账本，快照日志 offset——此后才是正式对账窗口
        await p.eval_js('window.__humLedger = []; "cleared"')
        out['log_offsets'] = {p_.name: p_.stat().st_size for p_ in (S2S_LOG, AV_LOG) if p_.exists()}
        if not avatar_ready:
            out['error'] = 'AVATAR_NOT_READY_90S'
            return out

        # 3) loopback 录音线程（180s 上限，事件停止）
        stop_ev = threading.Event()
        lb_holder = {}

        def rec():
            try:
                lb_holder['r'] = record_loopback_pw(180, stop_ev)
            except Exception as e:
                lb_holder['r'] = {'error': str(e)[:200]}

        th = threading.Thread(target=rec)
        th.start()
        time.sleep(0.8)
        lb_t0 = lb_holder.get('r', {}).get('t0_wall')  # 尚未返回；用本地时间近似
        lb_started = time.time()

        # 4) 发送长文本
        send = await p.eval_js(JS_SEND.replace('%TEXT%', json.dumps(PROMPT, ensure_ascii=False)))
        out['send'] = send
        if not send.get('ok'):
            stop_ev.set(); th.join(timeout=200)
            out['error'] = 'SEND_FAILED'
            return out
        send_perf = send['sendPerf']

        # 5) 等 router 收口（AUDIO_DONE / AUDIO_TRUNCATED）
        diag_final = None
        t0 = time.time()
        while time.time() - t0 < 420:
            await asyncio.sleep(3)
            d = await p.eval_js(JS_DIAG)
            fin = [t for t in (d or {}).get('last', []) if t['bytes'] > 0 and t['state'] in
                   ('AUDIO_DONE', 'AUDIO_TRUNCATED', 'AUDIO_SILENT')]
            # 只要出现了比 send 晚收口的、字节量大的 turn（避免命中上一轮的记录）
            if fin and max(t['bytes'] for t in fin) > 200000:
                diag_final = d
                break
        router_done_wall = time.time()
        router_done_rel = router_done_wall - lb_started
        out['router_done_rel'] = round(router_done_rel, 2)
        out['router_done_after_send_s'] = round(router_done_wall - (lb_started + 0.8), 1)
        if not diag_final:
            stop_ev.set(); th.join(timeout=200)
            out['error'] = 'TURN_TIMEOUT_420S'
            out['last_diag'] = d
            return out
        out['router_final'] = diag_final
        ttfa = None
        if diag_final.get('firstPlayingAt'):
            ttfa = round(diag_final['firstPlayingAt'] - send_perf, 2)
        out['ttfa_s'] = ttfa

        # 6) 等 loopback 静音尾部（router 收口后播放仍在继续）。
        #    必须先看到收口之后真的有声音活动，才允许用"连续 2.5s 静音"收尾，
        #    防止在收口与开播之间的空档提前截停录音。
        router_done_rel = router_done_wall - lb_started
        deadline = time.time() + 150
        while time.time() < deadline:
            await asyncio.sleep(2)
            r = lb_holder.get('r') or {}
            wins = r.get('windows', [])
            if wins:
                after = [(t, rms) for t, rms in wins if t > router_done_rel]
                after_active = [t for t, rms in after if rms > 0.002]
                if len(after_active) > 3 and after \
                   and all(rms <= 0.002 for t, rms in after if t > after[-1][0] - 2.5) \
                   and (time.time() - lb_started) - after[-1][0] > 2.5:
                    break
        stop_ev.set()
        th.join(timeout=200)
        lb = lb_holder.get('r', {})
        if 'error' in lb:
            out['error'] = 'LOOPBACK_FAIL: ' + lb['error']
            return out
        out['loopback'] = analyze_loopback(lb, router_done_rel)

        # 7) 长回合收口后的日志快照（此后短回合的日志不进入长回合对账）
        out['log_offsets_mid'] = {p.name: p.stat().st_size
                                  for p in (S2S_LOG, AV_LOG) if p.exists()}

        # 7b) 浏览器账本
        out['hum_ledger'] = await p.eval_js('JSON.stringify(window.__humLedger || [])')
        out['hum_ledger'] = json.loads(out['hum_ledger'])
        out['assistant_text'] = await p.eval_js(JS_TEXT_TAIL)

        # 8) 短回复回归
        send2_wall = time.time()
        send2 = await p.eval_js(JS_SEND.replace('%TEXT%', json.dumps(SHORT_PROMPT, ensure_ascii=False)))
        t0 = time.time()
        short_diag = None
        ttfa2 = None
        while time.time() - t0 < 180:
            await asyncio.sleep(1.0)
            d = await p.eval_js(JS_DIAG)
            if ttfa2 is None and d:
                for t in d.get('open', []):
                    if t['confirmed'] > 0:
                        ttfa2 = round(time.time() - send2_wall, 2)
            fin = [t for t in (d or {}).get('last', []) if t['bytes'] > 0 and t['state'] in
                   ('AUDIO_DONE', 'AUDIO_TRUNCATED', 'AUDIO_SILENT')]
            if fin and len(fin) >= 2:
                short_diag = d
                break
        out['short_regression'] = {'send_ok': send2.get('ok'), 'ttfa_s': ttfa2,
                                   'done_after_send_s': round((time.time() - send2_wall), 1),
                                   'diag': short_diag}

        # 9) 关闭测试标签页（释放会话）
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

    # 10) 服务端日志对账（用预热后快照的 offset；按 text length=456 与最大 put 匹配，
    #     免受预热/短回合污染，也不依赖 Response done 落盘与 offset 快照的竞态）
    base = out.get('log_offsets') or {}
    s2s_tail = tail_log(S2S_LOG, base.get(S2S_LOG.name, 0))
    av_tail = tail_log(AV_LOG, base.get(AV_LOG.name, 0))
    out['s2s_ledger'] = parse_s2s_block(s2s_tail, expect_len=456)
    out['av8010_ledger'] = parse_av_block(av_tail)

    # 11) 关页后会话异步释放 → 轮询 pool 回 idle（最终态记录）
    idle_after, release_wait = wait_pool_idle(180, poll_s=6)
    out['pool_released'] = idle_after
    out['pool_release_wait_s'] = round(release_wait, 1)
    out['pool_after'] = pool_state()

    # ---- Gate 计算 ----
    g = {}
    # 取字节最大的已完成回合（长回复），避免预热/短回合干扰
    fin_all = (out.get('router_final') or {}).get('last', [])
    rf = max(fin_all, key=lambda t: t.get('bytes', 0)) if fin_all else {}
    router_bytes = rf.get('bytes', 0)
    tts_gen_s = (out.get('s2s_ledger') or {}).get('gen_s') or 0
    puts = (out.get('av8010_ledger') or {}).get('puts') or []
    # 长回合的 put 是窗口内最大的（短回合只有 ~3 万样本）
    av_samples = max((b for _, b in puts), default=0)
    hum = [h for h in out.get('hum_ledger', []) if h.get('wavBytes')]
    wav_bytes = sum(h['wavBytes'] for h in hum)
    lb = out.get('loopback') or {}

    g['PRODUCTION_AUDIO_OVER_60S'] = tts_gen_s >= 60
    g['30S_CLAMP_REMOVED'] = bool(router_bytes > 1_440_000 and av_samples > 720_000)
    cov_8765_browser = round(router_bytes / (tts_gen_s * 48000), 4) if tts_gen_s else None
    cov_browser_8010 = round((av_samples * 2) / router_bytes, 4) if router_bytes else None
    g['8765_TO_BROWSER_AUDIO_COVERAGE'] = cov_8765_browser
    g['BROWSER_TO_8010_AUDIO_COVERAGE'] = cov_browser_8010
    g['coverage_ok'] = bool(cov_8765_browser and 0.97 <= cov_8765_browser <= 1.03
                            and cov_browser_8010 and 0.97 <= cov_browser_8010 <= 1.03)
    tail_in_text = bool(out.get('assistant_text', {}).get('tail', '').endswith(TAIL_SENTENCE.rstrip('。')))
    lb = out.get('loopback') or {}
    span = lb.get('active_span')
    last_active = lb.get('last_active')
    # TAIL 判据（播放终点对齐）：avatar 路径下播放从上传完成（≈router 收口）开始，
    # 因此 last_active 应 ≈ router_done_rel + 音频时长。span 会把收口前的静默与
    # 预热尾音算进去（attempt 2 实测 span 132.6s vs 音频 101.9s，正是此因），不能作判据。
    rdd = out.get('router_done_rel')
    tol = max(3.0, (tts_gen_s or 0) * 0.06)
    tail_tail_ok = bool(last_active and rdd and tts_gen_s
                        and (last_active - rdd) >= tts_gen_s - tol
                        and (last_active - rdd) <= tts_gen_s + tol + 5
                        and (lb.get('active_after_router_done', 0) > 3))
    g['TAIL_AUDIO_ACTUALLY_PLAYED'] = bool(tail_in_text and tail_tail_ok)
    out['tail_analysis'] = {'router_done_rel': rdd, 'last_active': last_active,
                            'play_span_est': (last_active - rdd) if (last_active and rdd) else None,
                            'tol': tol}
    g['WAV_BYTES_MATCH'] = bool(wav_bytes and abs(wav_bytes - 44 - router_bytes) < 4096)
    g['ROUTER_STATE'] = rf.get('state')
    g['ROUTER_TRUNCATED'] = rf.get('trunc')
    out['gates'] = g
    out['summary'] = {
        'tts_gen_s': tts_gen_s, 'router_bytes': router_bytes,
        'wav_bytes': wav_bytes, 'av_samples': av_samples,
        'av_duration_s': round(av_samples / 24000, 2),
        'loopback_span_s': span, 'ttfa_s': out.get('ttfa_s'),
    }
    out['ok'] = all(v for k, v in g.items() if k in ('PRODUCTION_AUDIO_OVER_60S', '30S_CLAMP_REMOVED',
                                                     'coverage_ok', 'TAIL_AUDIO_ACTUALLY_PLAYED', 'WAV_BYTES_MATCH')) \
        and not g['ROUTER_TRUNCATED'] and out['short_regression'].get('send_ok') and out.get('pool_released')
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    return 0 if out['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
