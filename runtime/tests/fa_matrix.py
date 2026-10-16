"""Final-user-acceptance driver. Subcommands print one JSON verdict line.

Run with runtime/core/venv python (has websockets, psutil, numpy).
"""
import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(str(__import__('pathlib').Path(__file__).resolve().parents[2] / ''))
sys.path.insert(0, str(REPO / 'runtime' / 'tests'))
import fa_cdp  # noqa: E402
import wasapi_loopback  # noqa: E402
import psutil  # noqa: E402

CORE_PY = str(REPO / 'runtime' / 'core' / 'venv' / 'Scripts' / 'python.exe')
SWITCH_PS1 = str(REPO / 'runtime' / 'windows' / 'Switch-Girlfriend-Avatar.ps1')
SUP_PS1 = str(REPO / 'runtime' / 'windows' / 'Avatar-Supervisor.ps1')
STATE = REPO / 'runtime' / 'windows' / 'avatar_runtime_state.json'
POINTER = REPO / 'runtime' / 'windows' / 'active_avatar.txt'

JS_MEDIA = r"""
(() => {
  const v = document.getElementById('av-video');
  const a = document.getElementById('av-audio');
  const vt = v && v.srcObject && v.srcObject.getVideoTracks ? v.srcObject.getVideoTracks()[0] : null;
  const at = a && a.srcObject && a.srcObject.getAudioTracks ? a.srcObject.getAudioTracks()[0] : null;
  return { vSrc: !!(v && v.srcObject), vTrack: vt ? vt.readyState : null,
           w: v ? v.videoWidth : 0, h: v ? v.videoHeight : 0, t: v ? v.currentTime : 0,
           aSrc: !!(a && a.srcObject), aTrack: at ? at.readyState : null,
           aPaused: a ? a.paused : true, aT: a ? a.currentTime : 0,
           layerLive: (document.getElementById('av-layer') || {className: ''}).className.includes('live') };
})()
"""

JS_BUBBLES = r"""
(() => [...document.querySelectorAll('.bubble')].map(b => b.textContent.trim()).filter(Boolean))()
"""

JS_ANALYSER_SETUP = r"""
(() => {
  window.__fa = window.__fa || {};
  if (window.__fa.analyser) return 'already';
  const a = document.getElementById('av-audio');
  if (!a) return 'no_audio_el';
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const src = ctx.createMediaElementSource(a);
    const an = ctx.createAnalyser();
    an.fftSize = 512;
    src.connect(an);
    an.connect(ctx.destination);
    window.__fa = { ctx, analyser: an, peak: 0 };
    window.__fa.sample = () => {
      const d = new Float32Array(an.fftSize);
      an.getFloatTimeDomainData(d);
      let s = 0;
      for (const x of d) s += x * x;
      const rms = Math.sqrt(s / d.length);
      if (rms > window.__fa.peak) window.__fa.peak = rms;
      return rms;
    };
    return 'installed';
  } catch (e) { return 'err:' + String(e).slice(0, 80); }
})()
"""

JS_SEND = r"""
(async (text) => {
  const ta = document.getElementById('gf-composer-input');
  if (!ta) return { ok: false, error: 'no_composer' };
  const snapshot = () => {
    const msgs = [...document.querySelectorAll('.hist-msg')];
    const user = msgs.filter(e => e.className.includes('user')).length;
    const asst = msgs.filter(e => e.className.includes('assistant')).length;
    return { user, asst, last: msgs.length ? msgs[msgs.length - 1].textContent.trim().slice(0, 120) : '' };
  };
  const before = snapshot();
  window.__fa.peak = 0;
  ta.value = text;
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
  const t0 = Date.now();
  let sawAudio = false, aPausedTicks = 0;
  while (Date.now() - t0 < 90000) {
    await new Promise(r => setTimeout(r, 700));
    if (window.__fa.sample) window.__fa.sample();
    const a = document.getElementById('av-audio');
    if (a && !a.paused) { sawAudio = true; aPausedTicks = 0; } else { aPausedTicks++; }
    const now = snapshot();
    if (now.asst > before.asst && now.user > before.user) {
      if (aPausedTicks >= 2 || Date.now() - t0 > 45000) {
        await new Promise(r => setTimeout(r, 1000));
        return { ok: true, sawAudio, pagePeakRms: Math.round((window.__fa.peak || 0) * 1e4) / 1e4,
                 before, after: snapshot(), elapsed: (Date.now() - t0) / 1000 };
      }
    }
  }
  return { ok: false, error: 'turn_timeout', sawAudio, before, after: snapshot() };
})("%TEXT%")
"""


def http(url, timeout=8):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except Exception as e:
        return 'ERR:' + str(e)[:80]


def state():
    return json.load(open(STATE, encoding='utf-8'))


def pointer():
    return POINTER.read_text(encoding='utf-8').strip() if POINTER.exists() else ''


def side_pids():
    """Pids of services that must NOT restart during switches."""
    out = {}
    for port, name in ((7860, 'ui'), (8090, 'llm'), (8765, 's2s')):
        for c in psutil.net_connections(kind='inet'):
            if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN:
                out[name] = c.pid
    return out


def cmd_baseline():
    snap = {'pids': side_pids(), 'pointer': pointer(),
            'gen': state().get('generation'), 'avatar': state().get('avatar_id'),
            'tts': (REPO / 'runtime' / 'windows' / 'active_tts_backend.txt').read_text().strip()
            if (REPO / 'runtime' / 'windows' / 'active_tts_backend.txt').exists() else None}
    (REPO / 'runtime' / 'tests' / '_fa_baseline.json').write_text(json.dumps(snap))
    print(json.dumps({'ok': True, **snap}))


def cmd_services():
    base = json.loads((REPO / 'runtime' / 'tests' / '_fa_baseline.json').read_text())
    now = side_pids()
    same = {k: (base['pids'].get(k) == now.get(k)) for k in ('ui', 'llm', 's2s')}
    print(json.dumps({'ok': all(same.values()), 'same': same, 'now': now}))


def run_switch(target):
    out = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
                          '-ExecutionPolicy', 'Bypass', '-File', SWITCH_PS1, '-Target', target],
                         capture_output=True, text=True, timeout=300, errors='replace')
    txt = out.stdout + out.stderr
    return {'ok': 'AVATAR_SWITCH_OK' in txt, 'died': 'TASK_CHILD_DIED' in txt,
            'failed': 'AVATAR_SWITCH_FAILED' in txt, 'raw_tail': txt.strip().splitlines()[-4:]}


def cmd_switch(target):
    r = run_switch(target)
    time.sleep(2)
    st = state()
    r['pointer'] = pointer()
    r['gen'] = st.get('generation')
    r['avatar_id'] = st.get('avatar_id')
    r['state'] = st.get('state')
    r['http8010'] = http('http://127.0.0.1:8010/')
    r['pointer_ok'] = r['pointer'] == target
    r['state_ok'] = st.get('state') == 'RUNNING' and st.get('avatar_id') == target
    r['ok'] = bool(r['ok'] and r['pointer_ok'] and r['state_ok'] and r['http8010'] == 200 and not r['died'])
    print(json.dumps(r, ensure_ascii=False))


def cmd_media(timeout=120):
    async def go():
        p = await fa_cdp.Page.attach()
        try:
            t0 = time.time()
            last = None
            while time.time() - t0 < timeout:
                m = await p.eval_js(JS_MEDIA)
                last = m
                if m['vSrc'] and m['vTrack'] == 'live' and m['w'] > 0 and m['h'] > 0 and m['aSrc'] and m['aTrack'] == 'live':
                    t1 = m['t']
                    await asyncio.sleep(2.5)
                    m2 = await p.eval_js(JS_MEDIA)
                    growing = m2['t'] > t1
                    return {'ok': growing, 'media': m2, 'currentTimeGrowing': growing,
                            'waited': round(time.time() - t0, 1)}
                await asyncio.sleep(2)
            return {'ok': False, 'media': last, 'error': 'media_timeout'}
        finally:
            await p.close()
    print(json.dumps(fa_cdp.run(go()), ensure_ascii=False))


def cmd_turn(text):
    text = text or '你好呀，用一句话介绍一下你自己吧。'
    result = {'ok': False}

    async def go():
        p = await fa_cdp.Page.attach()
        try:
            setup = await p.eval_js(JS_ANALYSER_SETUP)
            stop_ev = threading.Event()
            rec = {}

            def recorder():
                try:
                    rec['r'] = wasapi_loopback.record_loopback(75, stop_event=stop_ev)
                except Exception as e:
                    rec['r'] = {'error': str(e)[:120]}

            th = threading.Thread(target=recorder)
            th.start()
            expr = JS_SEND.replace('%TEXT%', text.replace('"', '\\"'))
            turn = await p.eval_js(expr)
            stop_ev.set()
            th.join(timeout=80)
            turn['analyserSetup'] = setup
            turn['loopback'] = rec.get('r')
            sys_rms = rec.get('r', {}).get('peakRms', 0)
            page_rms = turn.get('pagePeakRms', 0)
            turn['audioOk'] = bool(turn.get('sawAudio')) and (page_rms > 0.005 or sys_rms > 0.002)
            turn['ok'] = bool(turn.get('ok') and turn['audioOk'])
            return turn
        finally:
            await p.close()
    print(json.dumps(fa_cdp.run(go()), ensure_ascii=False))


def cmd_refresh():
    async def go():
        p = await fa_cdp.Page.attach()
        try:
            before_ptr = pointer()
            await p.eval_js("location.reload(); 1", await_promise=False)
            await asyncio.sleep(6)
            t0 = time.time()
            while time.time() - t0 < 120:
                m = await p.eval_js(JS_MEDIA)
                if m['vSrc'] and m['vTrack'] == 'live' and m['w'] > 0 and m['h'] > 0:
                    break
                await asyncio.sleep(3)
            st = state()
            return {'ok': m['vSrc'] and m['w'] > 0 and pointer() == before_ptr and st.get('avatar_id') == before_ptr,
                    'pointerBefore': before_ptr, 'pointerAfter': pointer(),
                    'stateAvatar': st.get('avatar_id'), 'media': m, 'http8010': http('http://127.0.0.1:8010/')}
        finally:
            await p.close()
    print(json.dumps(fa_cdp.run(go()), ensure_ascii=False))


def cmd_restart8010():
    def sup(action, extra=()):
        return subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
                               '-ExecutionPolicy', 'Bypass', '-File', SUP_PS1,
                               '-Action', action, '-Json', *extra],
                              capture_output=True, text=True, timeout=180, errors='replace')
    stop_r = sup('Stop')
    time.sleep(3)
    stopped = state().get('state')
    ensure_r = sup('Ensure', ('-AvatarId', '', '-OwnerMode', 'Task'))
    deadline = time.time() + 90
    while time.time() < deadline:
        s = state()
        if s.get('state') == 'RUNNING' and http('http://127.0.0.1:8010/') == 200:
            break
        time.sleep(3)
    s = state()
    print(json.dumps({'pointer': pointer(), 'avatarAfter': s.get('avatar_id'),
                      'genAfter': s.get('generation'), 'stateAfter': s.get('state'),
                      'http8010': http('http://127.0.0.1:8010/'),
                      'stateWhenStopped': stopped,
                      'stopOut': (stop_r.stdout or stop_r.stderr).strip()[:200],
                      'ensureOut': (ensure_r.stdout or ensure_r.stderr).strip()[:200]}))


JS_DEEP = r"""
(() => {
  const cap = document.querySelector('.gf-caption, .caption, #gf-caption');
  return { bubbles: document.querySelectorAll('.bubble').length,
    hist: document.querySelectorAll('.hist-msg').length,
    caption: cap ? cap.textContent.trim().slice(0, 80) : null,
    composerCls: (document.getElementById('gf-composer-input') || {}).className,
    statusBadge: (document.getElementById('av-status') || {}).textContent,
    playing: [...document.querySelectorAll('audio,video')].filter(x => !x.paused).map(x => ({ id: x.id, t: Math.round((x.currentTime || 0) * 100) / 100 })),
    bodyTail: document.body.innerText.replace(/\s+/g, ' ').slice(-260) };
})()
"""


def cmd_deep():
    async def go():
        p = await fa_cdp.Page.attach()
        try:
            return await p.eval_js(JS_DEEP)
        finally:
            await p.close()
    print(json.dumps(fa_cdp.run(go()), ensure_ascii=False))


COMMANDS = {'baseline': cmd_baseline, 'services': cmd_services, 'switch': cmd_switch,
            'media': cmd_media, 'turn': cmd_turn, 'refresh': cmd_refresh,
            'restart8010': cmd_restart8010, 'deep': cmd_deep}

if __name__ == '__main__':
    cmd = sys.argv[1]
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd in ('switch',):
        COMMANDS[cmd](arg)
    elif cmd in ('turn',):
        COMMANDS[cmd](arg)
    elif cmd in ('media',):
        COMMANDS[cmd](int(arg) if arg else 120)
    else:
        COMMANDS[cmd]()
