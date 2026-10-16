# -*- coding: utf-8 -*-
"""TSSCP01 shadow verification for the switch transaction controller.

TASK_ID = AI_GIRLFRIEND_TTS_SWITCH_CONTROL_PLANE_FINAL_REPAIR_01, task §13.
Standalone runner (no pytest in runtime/core/venv):

    runtime/core/venv/Scripts/python.exe runtime/tests/test_tts_switch_transaction.py

Everything runs OFFLINE in a sandbox: the journal is pointed at a temp file, the
executor is a fake child process (a real OS process, so child identity is real),
and the runtime probes (committed backend / runtime backend / pipeline health) are
stubbed to controllable values.  The live stack is never touched: no service is
started, stopped or probed by these tests.

Tests (task §13):

  TEST1  IDLE -> STARTING -> RUNNING -> COMMITTED        (stdout contract SWITCH_OK)
  TEST2  IDLE -> STARTING -> RUNNING -> FAILED           (rc != 0)
  TEST3  IDLE -> STARTING -> RUNNING -> ROLLED_BACK      (stdout contract ROLLBACK_OK)
  TEST4  journal RUNNING, child pid nonexistent          -> ABORTED_STALE -> IDLE
  TEST5  PID reused, CreationTime differs                -> stale, not SWITCH_ALREADY_RUNNING
  TEST5b child identity fully proven                     -> SWITCH_ALREADY_RUNNING (held)
  TEST6  two concurrent switch requests                  -> exactly one gains execution
  TEST7  journal write anomaly before replace            -> previous valid version survives

Prints ``PASS TESTn`` / ``FAIL TESTn`` per check and exits 0 only when all pass.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "runtime" / "ui" / "app"))

import voice_models_admin as vma  # noqa: E402

results: list[tuple[str, bool, str]] = []

HELPER = r'''
import sys, time
target = sys.argv[sys.argv.index('-Target') + 1] if '-Target' in sys.argv else ''
mode = sys.argv[1]
if mode == 'ok':
    print(f'SWITCH_OK target={target}')
elif mode == 'fail':
    print('SWITCH_FAILED target=%s reason=PROBE_FAILURE' % target, file=sys.stderr)
    sys.exit(1)
elif mode == 'rollback':
    print(f'ROLLBACK_OK previous=other target={target} reason=PROBE_FAILURE')
elif mode == 'sleep-ok':
    time.sleep(float(sys.argv[2]))
    print(f'SWITCH_OK target={target}')
'''


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + ("" if not detail else "  " + detail))


class Sandbox:
    """Point every file/probe of the controller at throwaway stand-ins."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tsscp01_shadow_"))
        self.journal = self.tmp / "tts_switch_state.json"
        self.script = self.tmp / "Switch-Girlfriend-Voice.ps1"  # identity marker only
        self.helper = self.tmp / "fake_executor.py"
        self.helper.write_text(HELPER, encoding="utf-8")
        self._orig = {}
        self.mode = "ok"
        self.sleep = 0.0
        # The shadow tests switch to "higgs"; a healthy runtime that already agrees
        # with that target is what the post-commit verify expects to find.
        self.committed = "higgs"
        self.runtime = "higgs"
        self.healthy = True
        self.executor_calls = 0

    def install(self) -> None:
        self._orig = {
            "SWITCH_STATE_FILE": vma.SWITCH_STATE_FILE,
            "SCRIPT": vma.SCRIPT,
            "_executor_command": vma._executor_command,
            "_committed_backend": vma._committed_backend,
            "_runtime_backend": vma._runtime_backend,
            "_pipeline_healthy": vma._pipeline_healthy,
        }
        vma.SWITCH_STATE_FILE = self.journal
        vma.SCRIPT = self.script
        sandbox = self

        def fake_executor(target: str) -> list[str]:
            sandbox.executor_calls += 1
            argv = [sys.executable, str(sandbox.helper), sandbox.mode]
            if sandbox.sleep:
                argv.append(str(sandbox.sleep))
            return argv + [str(sandbox.script), "-Target", target]

        vma._executor_command = fake_executor
        vma._committed_backend = lambda: sandbox.committed
        vma._runtime_backend = lambda: sandbox.runtime
        vma._pipeline_healthy = lambda: sandbox.healthy
        self.pool_patch = patch.object(vma.httpx, 'get', return_value=SimpleNamespace(json=lambda: {'in_use': 0}))
        self.pool_patch.start()  # offline fixture must never probe production activity

    def restore(self) -> None:
        self.pool_patch.stop()
        for name, value in self._orig.items():
            setattr(vma, name, value)

    def write_journal(self, **fields) -> None:
        payload = {**vma._blank_journal(), **fields}
        self.journal.write_text(json.dumps(payload), encoding="utf-8")

    def read_journal(self) -> dict:
        try:
            return json.loads(self.journal.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}

    def wait_terminal(self, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record_ = self.read_journal()
            if record_.get("phase") in vma.TERMINAL_PHASES:
                return record_
            time.sleep(0.05)
        return self.read_journal()


def fresh_sandbox(mode: str = "ok", **kwargs) -> Sandbox:
    box = Sandbox()
    box.mode = mode
    for key, value in kwargs.items():
        setattr(box, key, value)
    box.install()
    return box


# ------------------------------------------------------------------- TEST 1..3

def test_run_verdicts() -> None:
    cases = [
        ("TEST1", "ok", {}, vma.PHASE_COMMITTED, "SWITCH_OK"),
        ("TEST2", "fail", {"committed": "fish", "runtime": "higgs"},
         vma.PHASE_FAILED, "SWITCH_FAILED"),
        ("TEST3", "rollback", {"committed": "fish", "runtime": "fish"},
         vma.PHASE_ROLLED_BACK, "ROLLBACK_OK"),
    ]
    for name, mode, probes, want_phase, want_result in cases:
        box = fresh_sandbox(mode, **probes)
        try:
            # start_switch normally records the target before the runner thread runs;
            # the direct _run calls below seed the same pre-state.
            box.write_journal(phase=vma.PHASE_IDLE, target="higgs")
            vma._run("higgs")
            final = box.wait_terminal()
            ok = (final.get("phase") == want_phase and final.get("result") == want_result
                  and final.get("target") == "higgs")
            record(f"{name}-{want_phase}", ok, json.dumps(
                {k: final.get(k) for k in ("phase", "result", "reason")}, ensure_ascii=False))
        finally:
            box.restore()


# --------------------------------------------------------------------- TEST 4

def test_stale_reconcile() -> None:
    box = fresh_sandbox()
    try:
        box.write_journal(phase=vma.PHASE_RUNNING, target="higgs", child_pid=999999999,
                          child_start_time="700000.0", switch_id="stale-1")
        receipt = vma._reconcile_stale(box.read_journal())
        final = box.read_journal()
        ok = (receipt.get("ok") is True and final.get("phase") == vma.PHASE_IDLE
              and final.get("reason") == vma.REASON_STALE_RECONCILED
              and final.get("child_pid") == 0)
        record("TEST4-ABORTED_STALE-to-IDLE", ok,
               json.dumps({"receipt": receipt, "phase": final.get("phase"),
                           "reason": final.get("reason")}))
    finally:
        box.restore()


# --------------------------------------------------------------- TEST 5 / TEST5b

def test_pid_reuse_is_stale() -> None:
    box = fresh_sandbox()
    try:
        # A very much alive pid (our own) whose CreationTime does NOT match the
        # recorded one: identity unproven -> stale -> reconcile (stubbed healthy).
        box.write_journal(phase=vma.PHASE_RUNNING, target="higgs", child_pid=__import__("os").getpid(),
                          child_start_time="1.0", switch_id="reuse-1")
        held = vma._child_identity_ok(box.read_journal())
        receipt = vma._reconcile_stale(box.read_journal())
        ok = held is False and receipt.get("ok") is True
        record("TEST5-pid-reuse-stale", ok, f"identity_ok={held} receipt_ok={receipt.get('ok')}")
    finally:
        box.restore()


def test_proven_child_is_held() -> None:
    box = fresh_sandbox(mode="sleep-ok", sleep=6.0)
    try:
        argv = [sys.executable, str(box.helper), "sleep-ok", "6.0", str(box.script), "-Target", "higgs"]
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import psutil
        child_ct = repr(psutil.Process(proc.pid).create_time())
        box.write_journal(phase=vma.PHASE_RUNNING, target="higgs", child_pid=proc.pid,
                          child_start_time=child_ct, switch_id="live-1")
        result = vma.start_switch("higgs")
        ok = (result.get("reason") == "SWITCH_ALREADY_RUNNING"
              and box.read_journal().get("switch_id") == "live-1"
              and box.executor_calls == 0)
        record("TEST5b-proven-child-held", ok,
               f"reason={result.get('reason')} switch_id_preserved="
               f"{box.read_journal().get('switch_id') == 'live-1'}")
        proc.kill()
        proc.wait(timeout=10)
    finally:
        box.restore()


# --------------------------------------------------------------------- TEST 6

def test_concurrent_single_grant() -> None:
    box = fresh_sandbox(mode="sleep-ok", sleep=1.5)
    try:
        box.write_journal(phase=vma.PHASE_IDLE)
        outcomes: list[dict] = []

        def call() -> None:
            outcomes.append(vma.start_switch("higgs"))

        threads = [threading.Thread(target=call) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        granted = [o for o in outcomes if o.get("ok")]
        refused = [o for o in outcomes if o.get("reason") == "SWITCH_ALREADY_RUNNING"]
        final = box.wait_terminal()
        ok = (len(granted) == 1 and len(refused) == 1
              and box.executor_calls == 1
              and final.get("phase") == vma.PHASE_COMMITTED)
        record("TEST6-single-execution-grant", ok,
               f"granted={len(granted)} refused={len(refused)} executor_calls={box.executor_calls}")
    finally:
        box.restore()


# --------------------------------------------------------------------- TEST 7

def test_torn_write_keeps_old_version() -> None:
    box = fresh_sandbox()
    try:
        good = {**vma._blank_journal(), "switch_id": "intact-1", "target": "fish",
                "phase": vma.PHASE_COMMITTED, "result": "SWITCH_OK", "child_pid": 4242}
        box.journal.write_text(json.dumps(good), encoding="utf-8")
        real_replace = __import__("os").replace

        def exploding_replace(*args, **kwargs):
            raise OSError("PROBE_DISK_FAILURE")

        vma.os.replace = exploding_replace
        try:
            raised = False
            try:
                vma._journal_write(phase=vma.PHASE_RUNNING, target="higgs")
            except (vma.JournalWriteError, OSError):
                raised = True
        finally:
            vma.os.replace = real_replace
        after = box.read_journal()
        ok = (raised and after.get("switch_id") == "intact-1"
              and after.get("phase") == vma.PHASE_COMMITTED
              and after.get("child_pid") == 4242)
        record("TEST7-torn-write-preserves-journal", ok,
               f"raised={raised} persisted_switch_id={after.get('switch_id')} "
               f"persisted_phase={after.get('phase')}")
    finally:
        box.restore()


def main() -> int:
    test_run_verdicts()
    test_stale_reconcile()
    test_pid_reuse_is_stale()
    test_proven_child_is_held()
    test_concurrent_single_grant()
    test_torn_write_keeps_old_version()
    failed = [name for name, ok, _ in results if not ok]
    print(f"test_tts_switch_transaction: {'PASS' if not failed else 'FAIL'} "
          f"({len(results) - len(failed)}/{len(results)})")
    for name in failed:
        print(f"  failed: {name}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
