# -*- coding: utf-8 -*-
"""ADJUSTER_01 收尾：等 8765 pool 空闲后自动跑 G/H（对话+声音）与 I（switch 回归）。

用法: python fa_layout_tail.py [max_wait_minutes]
"""
import json
import subprocess
import sys
import time
import urllib.request

MAX_WAIT = int(sys.argv[1]) * 60 if len(sys.argv) > 1 else 1800
PY = str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'runtime/core/venv/Scripts/python.exe')
TESTS = str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'runtime/tests')


def pool():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/v1/pool", timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:100]}


def run(cmd, timeout):
    p = subprocess.run([PY] + cmd, capture_output=True, text=True,
                       timeout=timeout, errors="replace")
    return (p.stdout + p.stderr).strip().splitlines()[-1] if (p.stdout or p.stderr) else "(empty)"


def main():
    t0 = time.time()
    idle_streak = 0
    while time.time() - t0 < MAX_WAIT:
        pj = pool()
        busy = pj.get("in_use", 1) != 0 or "error" in pj
        idle_streak = 0 if busy else idle_streak + 1
        print(f"[{time.strftime('%H:%M:%S')}] pool={json.dumps(pj)[:120]} idle_streak={idle_streak}", flush=True)
        if idle_streak >= 4:   # 连续 4 次（60s）空闲才动手
            break
        time.sleep(15)
    else:
        print("TAIL_RESULT = BUSY_TIMEOUT（用户会话持续占用，未做 switch 打断）", flush=True)
        return 0

    print("== G/H 对话+声音 ==", flush=True)
    print(run([TESTS + r"\fa_matrix.py", "turn", "布局验收：正常聊一句。"], 240), flush=True)
    print("== I switch 回归（备用） ==", flush=True)
    print(run([TESTS + r"\fa_matrix.py", "switch", "wav2lip256_zero01"], 420), flush=True)
    print("== I switch 回归（默认） ==", flush=True)
    print(run([TESTS + r"\fa_matrix.py", "switch", "wav2lip256_avatar1"], 420), flush=True)
    print("== 收尾媒体 ==", flush=True)
    print(run([TESTS + r"\fa_matrix.py", "media", "120"], 180), flush=True)
    print("TAIL_RESULT = DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
