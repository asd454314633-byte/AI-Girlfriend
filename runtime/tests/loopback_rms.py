"""WASAPI loopback RMS probe (sounddevice port of a01_loopback.py).
Usage: python loopback_rms.py <seconds> <out_json>
Captures the default OUTPUT device via WASAPI loopback; reports peak/avg RMS.
"""
import sys, json, time
import numpy as np
import sounddevice as sd

dur = float(sys.argv[1])
out = sys.argv[2]
out_dev = sd.default.device[1]
dev_name = sd.query_devices(out_dev)['name']
extra = sd.WasapiSettings(loopback=True)
samples = []
with sd.InputStream(device=out_dev, samplerate=48000, channels=2, dtype='float32',
                    blocksize=4800, extra_settings=extra) as st:
    t0 = time.time()
    while time.time() - t0 < dur:
        data, over = st.read(4800)
        mono = data.mean(axis=1)
        samples.append((round(time.time() - t0, 2), float(np.sqrt((mono ** 2).mean())), int(over)))
peak = max(s[1] for s in samples)
avg = sum(s[1] for s in samples) / len(samples)
# count of 0.1s windows with audible energy
active = sum(1 for s in samples if s[1] > 0.002)
result = {"peakRms": round(peak, 6), "avgRms": round(avg, 6), "activeWindows": active,
          "windows": len(samples), "seconds": dur, "device": str(dev_name)[:60], "ts": time.strftime("%H:%M:%S")}
json.dump(result, open(out, "w"))
print(json.dumps(result))
