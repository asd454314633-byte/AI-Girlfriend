"""WASAPI loopback recorder — capture actual speaker output energy.
Usage: python a01_loopback.py <seconds> <out_json>
"""
import sys, json, time
import numpy as np
import soundcard as sc

dur = float(sys.argv[1])
out = sys.argv[2]
# loopback mics = outputs of the audio renderer, captured at the system level
mics = [m for m in sc.all_microphones(include_loopback=True) if m.isloopback]
if not mics:
    json.dump({"error": "no_loopback_mic"}, open(out, "w")); sys.exit(1)
# prefer the default device's loopback
mic = mics[0]
try:
    if sc.default_speaker().name in mic.name:
        pass
except Exception:
    pass
with mic.recorder(samplerate=48000) as rec:
    t0 = time.time(); peak = 0.0; total = 0.0; n = 0
    while time.time() - t0 < dur:
        data = rec.record(numframes=4800)  # 0.1 s
        mono = data.mean(axis=1)
        rms = float(np.sqrt((mono ** 2).mean()))
        peak = max(peak, rms)
        total += rms; n += 1
result = {"peakRms": peak, "avgRms": total / max(n, 1), "seconds": dur,
          "device": str(mic.name)[:60], "isloopback": True, "ts": time.strftime("%H:%M:%S")}
json.dump(result, open(out, "w"))
print(json.dumps(result))
