"""Discriminating audio test — is the loopback channel dead, or is playback silent?
Records from ALL loopback devices while a known-audible WebRTC turn plays.
Usage: python a01_loopback_all.py <seconds> <out_json>
"""
import sys, json, time, threading
import numpy as np
import soundcard as sc

dur = float(sys.argv[1]); out = sys.argv[2]
mics = [m for m in sc.all_microphones(include_loopback=True) if m.isloopback]
results = {}

def record(mic):
    try:
        with mic.recorder(samplerate=48000) as rec:
            t0 = time.time(); peak = 0.0
            while time.time() - t0 < dur:
                data = rec.record(numframes=4800)
                mono = data.mean(axis=1)
                peak = max(peak, float(np.sqrt((mono ** 2).mean())))
        results[str(mic.name)[:50]] = peak
    except Exception as e:
        results[str(mic.name)[:50]] = f"ERR {str(e)[:60]}"

threads = [threading.Thread(target=record, args=(m,)) for m in mics]
for t in threads: t.start()
print(json.dumps({"devices": [str(m.name)[:50] for m in mics]}))
for t in threads: t.join()
json.dump({"peaks": results, "seconds": dur}, open(out, "w"))
print(json.dumps({"peaks": results}))
