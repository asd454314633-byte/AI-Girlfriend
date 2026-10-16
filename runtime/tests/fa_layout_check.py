# -*- coding: utf-8 -*-
"""AI_GIRLFRIEND_UI_AVATAR_LAYOUT_ADJUSTER_01 自动验收 A-F（CDP 只读+滑块驱动）。

用法: python fa_layout_check.py <step>
  A  人物区域大小滑块 → 实时变化
  B  人物放大滑块     → 人物真正变大
  C  左右位置滑块     → 位置实时变化
  D  刷新保持
  E  恢复默认
  F  视频仍正常播放
"""
import json
import sys
import time

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'runtime/tests'))
import fa_cdp  # noqa: E402

JS_RECT = r"""
(() => {
  const v = document.getElementById('av-video');
  if (!v) return JSON.stringify({ ok: false, error: 'no_video' });
  const r = v.getBoundingClientRect();
  return JSON.stringify({
    ok: true,
    left: +r.left.toFixed(1), width: +r.width.toFixed(1), height: +r.height.toFixed(1),
    inline: {
      left: v.style.left || null, width: v.style.width || null,
      objectFit: v.style.objectFit || null, transform: v.style.transform || null,
    },
    t: +v.currentTime.toFixed(2), vw: window.innerWidth,
    ls: {
      r: localStorage.getItem('avatarPanelRatio'),
      s: localStorage.getItem('avatarVideoScale'),
      x: localStorage.getItem('avatarVideoOffsetX'),
    },
    slider: {
      r: (document.querySelector('.av-layout-card input[data-key="r"]') || {}).value || null,
    },
    entry: Boolean(document.getElementById('gf-layout-open')),
    divider: (() => { const d = document.getElementById('av-layout-divider'); return d ? { hidden: d.hidden, left: d.style.left } : null; })(),
  });
})()
"""

JS_SET = r"""
(([key, value]) => {
  const input = document.querySelector(`.av-layout-card input[data-key="${key}"]`);
  if (!input) return JSON.stringify({ ok: false, error: 'no_slider_' + key });
  input.value = String(value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
  return JSON.stringify({ ok: true, key, value });
})(%PAYLOAD%)
"""

JS_RESET = r"""
(() => {
  const b = document.querySelector('.av-layout-reset');
  if (!b) return JSON.stringify({ ok: false, error: 'no_reset_btn' });
  b.click();
  return JSON.stringify({ ok: true });
})()
"""


async def rect():
    p = await fa_cdp.Page.attach()
    try:
        return json.loads(await p.eval_js(JS_RECT))
    finally:
        await p.close()


async def set_slider(key, value):
    p = await fa_cdp.Page.attach()
    try:
        expr = JS_SET.replace('%PAYLOAD%', json.dumps([key, value]))
        return json.loads(await p.eval_js(expr))
    finally:
        await p.close()


async def click_reset():
    p = await fa_cdp.Page.attach()
    try:
        return json.loads(await p.eval_js(JS_RESET))
    finally:
        await p.close()


async def reload():
    p = await fa_cdp.Page.attach()
    try:
        await p.eval_js("location.reload(); 1", await_promise=False)
    finally:
        await p.close()


def step_a():
    r1 = fa_cdp.run(rect())
    fa_cdp.run(set_slider("r", 55))
    time.sleep(0.4)
    r2 = fa_cdp.run(rect())
    fa_cdp.run(set_slider("r", 30))
    time.sleep(0.4)
    r3 = fa_cdp.run(rect())
    grew = r2['width'] > r1['width']
    shrunk = r3['width'] < r2['width']
    print('A: width %s -> %s -> %s entry=%s divider=%s' %
          (r1['width'], r2['width'], r3['width'], r1['entry'], r2['divider']))
    print('A_VERDICT =', 'PASS' if (grew and shrunk and r2['inline']['width'] == '55%') else 'FAIL')


def step_b():
    fa_cdp.run(set_slider("r", 40))
    time.sleep(0.3)
    r1 = fa_cdp.run(rect())
    fa_cdp.run(set_slider("s", 160))
    time.sleep(0.4)
    r2 = fa_cdp.run(rect())
    bigger = r2['width'] > r1['width'] * 1.3 and r2['height'] > r1['height'] * 1.3
    print('B: w %s->%s h %s->%s transform=%s' %
          (r1['width'], r2['width'], r1['height'], r2['height'], r2['inline']['transform']))
    fa_cdp.run(set_slider("s", 100))
    print('B_VERDICT =', 'PASS' if bigger else 'FAIL')


def step_c():
    fa_cdp.run(set_slider("r", 40))
    fa_cdp.run(set_slider("s", 100))
    time.sleep(0.3)
    r1 = fa_cdp.run(rect())
    fa_cdp.run(set_slider("x", 80))
    time.sleep(0.4)
    r2 = fa_cdp.run(rect())
    fa_cdp.run(set_slider('x', -80))
    time.sleep(0.4)
    r3 = fa_cdp.run(rect())
    moved_right = r2['left'] > r1['left']
    moved_left = r3['left'] < r1['left']
    print('C: left %s -> %s -> %s' % (r1['left'], r2['left'], r3['left']))
    fa_cdp.run(set_slider("x", 0))
    print('C_VERDICT =', 'PASS' if (moved_right and moved_left) else 'FAIL')


def step_d():
    fa_cdp.run(set_slider("r", 48))
    fa_cdp.run(set_slider("s", 120))
    fa_cdp.run(set_slider("x", 40))
    time.sleep(0.4)
    before = fa_cdp.run(rect())
    fa_cdp.run(reload())
    time.sleep(6)
    after = fa_cdp.run(rect())
    same = (abs(before['width'] - after['width']) < 30
            and abs(before['left'] - after['left']) < 30
            and after['ls'] == {'r': '48', 's': '120', 'x': '40'})
    print('D: ls_after=%s w %s->%s left %s->%s' %
          (after['ls'], before['width'], after['width'], before['left'], after['left']))
    print('D_VERDICT =', 'PASS' if same else 'FAIL')


def step_e():
    b1 = fa_cdp.run(click_reset())
    time.sleep(0.5)
    r = fa_cdp.run(rect())
    cleared = all(v is None for v in r['inline'].values())
    ls = r['ls']
    print('E: reset=%s inline=%s ls=%s' % (b1, r['inline'], ls))
    print('E_VERDICT =', 'PASS' if (b1['ok'] and cleared and ls == {'r': None, 's': None, 'x': None}) else 'FAIL')


def step_f():
    r1 = fa_cdp.run(rect())
    time.sleep(3)
    r2 = fa_cdp.run(rect())
    playing = r2['t'] > r1['t'] and r2['width'] > 0 and r2['height'] > 0
    print('F: t %s -> %s w=%s' % (r1['t'], r2['t'], r2['width']))
    print('F_VERDICT =', 'PASS' if playing else 'FAIL')


STEPS = {'A': step_a, 'B': step_b, 'C': step_c, 'D': step_d, 'E': step_e, 'F': step_f}

if __name__ == '__main__':
    which = (sys.argv[1] if len(sys.argv) > 1 else 'ABCDEF').upper()
    for ch in which:
        print(f'---- {ch} ----')
        try:
            STEPS[ch]()
        except Exception as e:  # noqa: BLE001
            print(f'{ch}_VERDICT = FAIL exception={type(e).__name__}: {str(e)[:150]}')
