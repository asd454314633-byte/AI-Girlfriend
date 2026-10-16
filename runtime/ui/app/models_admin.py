"""Model registry bridge for the local 7860 deploy.

Task 5 asks for a model picker on the page *without* a second model-management
system, so everything here is a thin reader/launcher over what already exists:

  models.json / active_model.json   the registry and the current choice
  Switch-Girlfriend-LLM.ps1         the only thing allowed to touch port 8090

The Windows Native switch is fired as a one-shot `pwsh.exe` child; on the
legacy WSL deploy the environment may override that path. Nothing here stays
running between requests. The script is authoritative for stopping,
starting, health-checking and rolling back; we only watch its stdout, enforce a
timeout, and record its PID (a runaway background task is exactly what task 23
forbids).

Refs are written as Windows paths because that is how the Windows script and
llama receive them; `_as_posix` remains for legacy WSL-side file checks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import shutil
import subprocess
import threading
import time

import httpx

logger = logging.getLogger("s2s.models")

_RUNTIME_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_NATIVE_WINDOWS = os.name == "nt"
REGISTRY_PATH = os.environ.get(
    "LLM_MODELS_JSON",
    os.path.join(_RUNTIME_ROOT, "llm", "models.json")
    if _NATIVE_WINDOWS
    else str(__import__('pathlib').Path(__file__).resolve().parents[3] / 'runtime/llm/models.json'),
).strip()
ACTIVE_PATH = os.environ.get(
    "LLM_ACTIVE_JSON",
    os.path.join(_RUNTIME_ROOT, "llm", "active_model.json")
    if _NATIVE_WINDOWS
    else str(__import__('pathlib').Path(__file__).resolve().parents[3] / 'runtime/llm/active_model.json'),
).strip()
SWITCH_SCRIPT_WIN = os.environ.get(
    "LLM_SWITCH_PS1",
    os.path.join(_RUNTIME_ROOT, "llm", "Switch-Girlfriend-LLM.ps1")
    if _NATIVE_WINDOWS
    else str(__import__('pathlib').Path(__file__).resolve().parents[3] / 'runtime/llm/Switch-Girlfriend-LLM.ps1'),
).strip()
POWERSHELL = os.environ.get(
    "LLM_POWERSHELL",
    shutil.which("pwsh.exe") or "pwsh.exe"
    if _NATIVE_WINDOWS
    else "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
).strip()
LLM_BASE = os.environ.get("LLM_HEALTH_URL", "http://127.0.0.1:8090").strip().rstrip("/")
# 8765's reading bridge is the authority on "a turn is in flight right now".
S2S_SPEAK_STATUS = os.environ.get("S2S_SPEAK_STATUS", "http://127.0.0.1:8765/v1/pool").strip()

# Hashing a ~9 GB GGUF plus model load plus the script's own 180 s health
# budget. Past this the child is killed rather than left writing to the log.
SWITCH_TIMEOUT_S = int(os.environ.get("LLM_SWITCH_TIMEOUT_S", "900"))

_RESULT_RE = re.compile(r"(SWITCH_OK|SWITCH_FAILED|ROLLBACK_OK|ROLLBACK_FAIL|ALREADY_ACTIVE)")


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh) or {}
    except Exception as exc:
        logger.warning("registry read failed (%s): %r", path, exc)
        return {}


def _as_posix(win_path: str) -> str:
    """Convert Windows model paths only under WSL; preserve native Windows paths."""
    p = (win_path or "").strip()
    if os.name == "nt":
        return p.replace("/", "\\")
    if len(p) > 1 and p[1] == ":":
        return f"/mnt/{p[0].lower()}{p[2:].replace(chr(92), '/')}"
    return p


def _active_key() -> str:
    data = _read_json(ACTIVE_PATH)
    key = str(data.get("active_model") or "").strip()
    if key:
        return key
    try:
        with open(os.path.join(os.path.dirname(ACTIVE_PATH), "active_model_label.txt"), encoding="utf-8") as fh:
            return fh.read().strip()
    except Exception:
        return ""


def health() -> dict:
    """What 8765 will actually see when it next calls the LLM."""
    try:
        resp = httpx.get(f"{LLM_BASE}/health", timeout=3.0, trust_env=False)
        ids = []
        model_path = None
        try:
            models = httpx.get(f"{LLM_BASE}/v1/models", timeout=3.0, trust_env=False).json().get("data", [])
            ids = [str(m.get("id", "")) for m in models if m.get("id")]
        except Exception:
            pass
        try:
            props = httpx.get(f"{LLM_BASE}/props", timeout=3.0, trust_env=False).json()
            model_path = str(props.get("model_path") or "") or None
        except Exception:
            pass
        if not model_path and len(ids) == 1:
            model_path = ids[0]
        return {"reachable": resp.status_code == 200 and bool(ids), "served": ids, "modelPath": model_path}
    except Exception as exc:
        return {"reachable": False, "served": [], "modelPath": None, "error": type(exc).__name__}


def models() -> dict:
    """Registry + which entry is live + whether the file is actually on disk."""
    registry = _read_json(REGISTRY_PATH)
    entries = registry.get("models") or {}
    active = _active_key()
    active_spec = entries.get(active) if isinstance(entries, dict) else None
    expected_path = str(active_spec.get("path") or "") if isinstance(active_spec, dict) else ""
    live = health()
    normalize = lambda p: os.path.normcase(os.path.normpath(_as_posix(str(p or ""))))
    selection_applied = bool(live.get("reachable") and expected_path and live.get("modelPath") and
                             normalize(expected_path) == normalize(live.get("modelPath")))
    out = []
    for key, spec in entries.items():
        if not isinstance(spec, dict):
            continue
        role = str(spec.get("role") or "")
        out.append(
            {
                "key": key,
                "label": str(spec.get("label") or key),
                "hint": str(spec.get("hint") or ""),
                "role": role,
                "sizeBytes": int(spec.get("size_bytes") or 0),
                # 14B stays a back-door rollback, never a menu item (task 5).
                "uiVisible": bool(spec.get("ui_visible", role in ("realtime", "quality"))),
                "present": os.path.exists(_as_posix(str(spec.get("path") or ""))),
                "active": key == active,
            }
        )
    return {
        "active": active,
        "params": str(registry.get("launch_params_frozen") or ""),
        "listen": registry.get("listen") or {},
        "models": out,
        "health": live,
        "selectionApplied": selection_applied,
        "selectionError": None if active_spec else "ACTIVE_MODEL_STATE_INVALID",
        "switch": _state_snapshot(),
    }


# ---- switch orchestration -------------------------------------------------

_lock = threading.Lock()
_state = {
    "busy": False,
    "target": "",
    "pid": None,
    "startedAt": "",
    "finishedAt": "",
    "result": "",
    "detail": "",
    "tail": [],
}


def _state_snapshot() -> dict:
    with _lock:
        return json.loads(json.dumps(_state))


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def _gate_reason() -> str:
    """Refuse to pull the LLM out from under a live turn (task 5 step 1-2)."""
    try:
        body = httpx.get(S2S_SPEAK_STATUS, timeout=3.0, trust_env=False).json()
    except Exception:
        try:
            with socket.create_connection(("127.0.0.1", 8765), timeout=0.3):
                return "8765 正在监听但状态不可读取；为避免中断本轮回复，拒绝切换。"
        except OSError:
            return ""  # 8765 is down: there can be no in-flight voice turn
    if body.get("in_use", 0):
        return "正在生成回复，请等本轮结束后再切换。"
    return ""


def _run_switch(target: str) -> None:
    cmd = [
        POWERSHELL,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        SWITCH_SCRIPT_WIN,
        "-Target",
        target,
    ]
    lines: list[str] = []
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except Exception as exc:
        _set(busy=False, finishedAt=_now(), result="LAUNCH_FAILED", detail=repr(exc)[:300], tail=lines)
        return

    _set(pid=proc.pid)
    deadline = time.time() + SWITCH_TIMEOUT_S
    timed_out = False
    try:
        while proc.poll() is None:
            if time.time() > deadline:
                timed_out = True
                _kill(proc.pid)
                break
            line = proc.stdout.readline() if proc.stdout else ""
            if line:
                lines.append(line.rstrip())
                if len(lines) > 400:
                    del lines[:200]
    except Exception as exc:
        logger.warning("switch watcher failed: %r", exc)
    finally:
        try:
            proc.wait(timeout=30)
        except Exception:
            pass

    verdict = ""
    for line in reversed(lines):
        found = _RESULT_RE.search(line)
        if found:
            verdict = found.group(1)
            break
    if timed_out:
        verdict = "TIMEOUT_KILLED"
    _set(
        busy=False,
        finishedAt=_now(),
        result=verdict or ("OK" if proc.returncode == 0 else "FAILED"),
        detail=f"exit={proc.returncode} timed_out={timed_out}",
        tail=lines[-25:],
        pid=None,
    )
    print(f"[models_admin] switch {target} -> {verdict or 'UNKNOWN'} ({proc.returncode})", flush=True)


def _kill(pid: int) -> None:
    """Hard-stop the PowerShell child; a switch must never outlive its request."""
    for argv in (
        ["/mnt/c/Windows/System32/taskkill.exe", "/PID", str(pid), "/T", "/F"],
        ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
    ):
        try:
            subprocess.run(argv, capture_output=True, timeout=20)
            return
        except Exception:
            continue
    logger.warning("could not taskkill pid=%s", pid)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def start_switch(target: str) -> dict:
    registry = (_read_json(REGISTRY_PATH).get("models") or {})
    if target not in registry:
        return {"ok": False, "reason": "UNKNOWN_TARGET"}
    path = _as_posix(str(registry[target].get("path") or ""))
    if not path or not os.path.exists(path):
        return {"ok": False, "reason": "GGUF_MISSING", "path": path}

    gate = _gate_reason()
    if gate:
        return {"ok": False, "reason": "BUSY", "detail": gate}

    with _lock:
        if _state["busy"]:
            return {"ok": False, "reason": "SWITCH_ALREADY_RUNNING", "target": _state["target"]}
        _state.update(
            busy=True, target=target, pid=None, startedAt=_now(), finishedAt="", result="", detail="", tail=[]
        )

    threading.Thread(target=_run_switch, args=(target,), daemon=True).start()
    return {"ok": True, "started": True, "target": target}
