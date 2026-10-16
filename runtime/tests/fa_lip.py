# -*- coding: utf-8 -*-
"""口型专项验证：发送一轮真实对话，同时页内采样 <video> 帧，
量化说话期间的帧间差分（嘴部活动度）。

用法: python fa_lip.py "文本"
"""
import json
import sys
import numpy as np

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'runtime/tests'))
import fa_cdp  # noqa: E402

TEXT = sys.argv[1] if len(sys.argv) > 1 else "多说几句话，让我看看你的口型。"

JS = r"""
(async (text) => {
  const v = document.getElementById('av-video');
  if (!v || !v.srcObject) return JSON.stringify({ ok: false, error: 'no_video' });
  const cw = 48, ch = 84;
  const c = document.createElement('canvas');
  c.width = cw; c.height = ch;
  const ctx = c.getContext('2d', { willReadFrequently: true });
  const frames = [];
  const times = [];
  let stop = false;
  const sampler = setInterval(() => {
    if (v.readyState >= 2 && v.videoWidth > 0) {
      ctx.drawImage(v, 0, 0, cw, ch);
      const d = ctx.getImageData(0, 0, cw, ch).data;
      const g = new Float32Array(cw * ch);
      for (let i = 0; i < cw * ch; i++) g[i] = 0.299*d[i*4] + 0.587*d[i*4+1] + 0.114*d[i*4+2];
      frames.push(Array.from(g));
      times.push(v.currentTime);
    }
  }, 400);
  const ta = document.getElementById('gf-composer-input');
  const histCount = () => document.querySelectorAll('.hist-msg').length;
  const before = histCount();
  ta.value = text;
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
  const t0 = Date.now();
  while (Date.now() - t0 < 70000) {
    await new Promise(r => setTimeout(r, 800));
    const added = [...document.querySelectorAll('.hist-msg')].slice(before.length);
    const a = document.getElementById('av-audio');
    const speaking = a && !a.paused;
    if (added.some(x => x.textContent.trim() && x.textContent.trim() !== text) && !speaking && Date.now() - t0 > 6000) {
      await new Promise(r => setTimeout(r, 1500));
      break;
    }
  }
  stop = true;
  clearInterval(sampler);
  return JSON.stringify({ ok: true, frames, times,
    hist: [...document.querySelectorAll('.hist-msg')].slice(before.length).map(e => e.textContent.trim().slice(0, 60)) });
})(%TEXT%)
""".replace("%TEXT%", json.dumps(TEXT))


def main():
    async def go():
        p = await fa_cdp.Page.attach()
        try:
            raw = await p.eval_js(JS)
            return json.loads(raw) if isinstance(raw, str) else raw
        finally:
            await p.close()

    res = fa_cdp.run(go())
    if not res.get("ok"):
        print(json.dumps(res, ensure_ascii=False))
        return 1
    F = np.array(res["frames"], dtype=np.float32).reshape(len(res["frames"]), 84, 48)
    T = np.array(res["times"], dtype=np.float32)
    print("frames sampled:", len(F), "video t range: %.2f..%.2f" % (T.min(), T.max()))
    if len(F) < 6:
        print("LIP_CHECK = FAIL reason=TOO_FEW_FRAMES")
        return 1
    # 帧间差分（整帧 = 头部+嘴部区域；视频是半身像，上半 60% 为头部）
    diffs = np.abs(np.diff(F, axis=0)).mean(axis=(1, 2))
    head = np.abs(np.diff(F[:, :int(F.shape[1]*0.6), :], axis=0)).mean(axis=(1, 2))
    video_advancing = float(np.abs(np.diff(T)).mean()) > 0.05
    active = diffs > 0.8
    print("full-frame diff: mean=%.3f p90=%.3f active_ratio=%.2f" %
          (diffs.mean(), np.percentile(diffs, 90), active.mean()))
    print("head-zone diff: mean=%.3f p90=%.3f" % (head.mean(), np.percentile(head, 90)))
    print("video advancing:", video_advancing)
    print("replies:", json.dumps(res.get("hist", []), ensure_ascii=False))
    ok = head.mean() > 0.5 and active.mean() > 0.2 and video_advancing
    print("LIP_CHECK = " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
