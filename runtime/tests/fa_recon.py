"""FA recon: inspect the live 7860 page through CDP."""
import json
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'runtime/tests'))
import fa_cdp

JS = r"""
(() => {
  const vid = document.getElementById('av-video');
  const aud = document.getElementById('av-audio');
  const layer = document.getElementById('av-layer');
  let v = null, a = null;
  if (vid) { const t = vid.srcObject && vid.srcObject.getVideoTracks ? vid.srcObject.getVideoTracks()[0] : null;
    v = { srcObject: !!vid.srcObject, track: t ? t.readyState : null, w: vid.videoWidth, h: vid.videoHeight, t: vid.currentTime, muted: vid.muted }; }
  if (aud) { const t = aud.srcObject && aud.srcObject.getAudioTracks ? aud.srcObject.getAudioTracks()[0] : null;
    a = { srcObject: !!aud.srcObject, track: t ? t.readyState : null, muted: aud.muted, paused: aud.paused, vol: aud.volume }; }
  const els = [...document.querySelectorAll('textarea, input, [contenteditable="true"]')].map(e => ({
    tag: e.tagName, id: e.id || '', cls: String(e.className).slice(0, 50), type: e.type || '', ph: String(e.placeholder || '').slice(0, 30) })).slice(0, 12);
  const btns = [...document.querySelectorAll('button')].map(b => ({ id: b.id || '', txt: b.textContent.trim().slice(0, 24) })).slice(0, 20);
  return { title: document.title, url: location.href, readyState: document.readyState,
    layerClass: layer ? layer.className : null, video: v, audio: a, inputs: els, buttons: btns };
})()
"""

async def main():
    p = await fa_cdp.Page.attach()
    out = await p.eval_js(JS)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    await p.close()

fa_cdp.run(main())
