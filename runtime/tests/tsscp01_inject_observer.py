"""TSSCP01 fault-injection observer (TEST 16).

Triggers one real switch, watches the transaction journal at high frequency,
and kills the executor child as soon as the journal records its pid.
Every observation is logged with a timestamp so the timeline is auditable.

Run with the project venv python:
    runtime/core/venv/Scripts/python.exe runtime/tests/tsscp01_inject_observer.py <target>
"""
import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(str(__import__('pathlib').Path(__file__).resolve().parents[2] / ''))
JOURNAL = PROJECT / "runtime" / "windows" / "tts_switch_state.json"
SWITCH_URL = "http://127.0.0.1:7860/api/voice-models/switch"
STATE_URL = "http://127.0.0.1:7860/api/voice-models"
TERMINAL = {"COMMITTED", "ROLLED_BACK", "FAILED", "ABORTED_STALE", "IDLE"}

target = sys.argv[1] if len(sys.argv) > 1 else "qwen3"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d}] {msg}", flush=True)


def read_journal():
    try:
        return json.loads(JOURNAL.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}


import urllib.request

log(f"target={target} journal_before={read_journal().get('phase')} "
    f"child={read_journal().get('child_pid')}")

req = urllib.request.Request(SWITCH_URL, data=json.dumps({"target": target}).encode(),
                             headers={"Content-Type": "application/json"}, method="POST")
t0 = time.time()
with urllib.request.urlopen(req, timeout=15) as resp:
    log(f"POST -> {resp.read().decode()} (returned after {time.time()-t0:.2f}s)")

killed = False
deadline = time.time() + 60
last = {}
while time.time() < deadline:
    s = read_journal()
    if "_error" in s:
        log(f"READ_ERROR {s['_error']}")
        time.sleep(0.15)
        continue
    ph, cp = s.get("phase"), int(s.get("child_pid") or 0)
    key = (ph, cp)
    if key != last.get("key"):
        log(f"journal phase={ph} child_pid={cp}")
        last["key"] = key
    if cp > 0 and ph in ("STARTING", "RUNNING", "VERIFYING") and not killed:
        log(f"KILLING child tree pid={cp} (phase={ph})")
        rc = subprocess.run(["taskkill", "/F", "/T", "/PID", str(cp)],
                            capture_output=True, text=True)
        log(f"taskkill rc={rc.returncode} out={rc.stdout.strip()} err={rc.stderr.strip()}")
        killed = True
        inject_t = time.time()
        break
    time.sleep(0.15)

if not killed:
    log(f"NO-KILL: transaction reached phase={last.get('key')} without a live child window")
    sys.exit(2)

# wait for the controller to terminalize
deadline = time.time() + 120
final = None
while time.time() < deadline:
    s = read_journal()
    if "_error" not in s and s.get("phase") in TERMINAL and s.get("child_pid") in (0, cp):
        final = s
        if s.get("phase") == last.get("key", ("",))[0]:
            time.sleep(1.0)
            s2 = read_journal()
            if s2.get("phase") != s.get("phase"):
                continue
        break
    time.sleep(0.3)

log(f"FINAL phase={final.get('phase')} result={final.get('result')} "
    f"reason={final.get('reason')} switch_id={final.get('switch_id')}")
log(f"terminalize_latency={time.time()-inject_t:.2f}s since injection")
with urllib.request.urlopen(STATE_URL, timeout=10) as resp:
    st = json.loads(resp.read().decode())
log(f"HTTP ready={st.get('ready')} active={st.get('active')} "
    f"switch.phase={st['switch'].get('phase')} switch.busy={st['switch'].get('busy')}")
