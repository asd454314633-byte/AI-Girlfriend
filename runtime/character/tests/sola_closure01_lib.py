# -*- coding: utf-8 -*-
"""sola_closure01_lib.py — 无人值守收口（AI_GIRLFRIEND_SOLA_V2_EMBER_UNATTENDED_CLOSURE_01）共用工具。

**只读工具库**：这里没有任何写生产状态的函数，只有
  * 路径常量 / 输出目录
  * SHA256 / 字节数
  * 进程枚举（只用查询权限，绝不 os.kill）
  * HTTP 探针（GET / 只读 POST，带超时与截断）

为什么单独建一个库：本轮要产出十几件证据，每件都要同一把尺（同一时间格式、
同一哈希算法、同一进程身份口径），复制粘贴必然量出第二套口径。

进程身份口径 = PID + 创建时间 + 命令行 三重匹配；判活只用
``kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION)``，
**绝不用 ``os.kill(pid, 0)``**（本机它等价于 TerminateProcess，见
``runtime/ui/app/pid_liveness.py`` 的实测说明）。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parents[3]
OUT_DIR = Path(__file__).resolve().parent / "out" / "sola_unattended_closure_01"
TASK_ID = "AI_GIRLFRIEND_SOLA_V2_EMBER_UNATTENDED_CLOSURE_01"

PORTS = (7860, 8090, 8765, 8010)

# 快照时间戳（本机墙钟），所有证据统一用它格式化
TS_FMT = "%Y-%m-%d %H:%M:%S %z"


def now_local() -> str:
    return datetime.now().astimezone().strftime(TS_FMT)


def now_utc_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_out_dir() -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR


def write_json(name: str, payload: Any) -> Path:
    """写本轮证据目录里的一个 JSON 文件（目录在 tests/out 下，属本轮产物）。"""
    ensure_out_dir()
    path = OUT_DIR / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
                    encoding="utf-8", newline="\n")
    return path


def sha256_file(path: os.PathLike | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def file_fact(rel: str) -> dict[str, Any]:
    """一个文件的可核对事实：字节 / mtime / sha256。缺文件要显式登记，不许静默跳过。"""
    path = REPO / rel
    if not path.exists():
        return {"path": rel, "exists": False}
    st = path.stat()
    return {
        "path": rel,
        "exists": True,
        "bytes": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime).strftime(TS_FMT),
        "mtime_epoch": round(st.st_mtime, 3),
        "sha256": sha256_file(path),
    }


# ---------------------------------------------------------------------------
# 进程与端口
# ---------------------------------------------------------------------------

_SYNCHRONIZE = 0x00100000
_QUERY_LIMITED = 0x1000
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87


def pid_alive(pid: int) -> bool:
    """只申请查询权限的判活。ACCESS_DENIED 表示对象存在。"""
    if pid <= 0:
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_SYNCHRONIZE | _QUERY_LIMITED, False, int(pid))
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.windll.kernel32.GetLastError() == _ERROR_ACCESS_DENIED
    except Exception:
        return False


def listeners() -> dict[int, list[dict[str, Any]]]:
    """port -> [{pid, name, created, cmdline}]，用 psutil 一次拿全（同一把尺）。"""
    import psutil
    out: dict[int, list[dict[str, Any]]] = {port: [] for port in PORTS}
    seen: set[tuple[int, int]] = set()
    for conn in psutil.net_connections(kind="tcp"):
        if not conn.laddr or conn.status != psutil.CONN_LISTEN:
            continue
        port = int(conn.laddr.port)
        if port not in out:
            continue
        pid = int(conn.pid or 0)
        key = (port, pid)
        if key in seen:
            continue
        seen.add(key)
        row: dict[str, Any] = {"port": port, "ip": str(conn.laddr.ip), "pid": pid}
        try:
            proc = psutil.Process(pid)
            row["name"] = proc.name()
            row["created"] = datetime.fromtimestamp(proc.create_time()).strftime(TS_FMT)
            row["created_epoch"] = round(proc.create_time(), 3)
            row["cmdline"] = " ".join(proc.cmdline())
            row["exe"] = proc.exe()
            row["cwd"] = proc.cwd()
        except Exception as exc:  # 权限/退出
            row["name"] = f"<unavailable:{type(exc).__name__}>"
        out[port].append(row)
    return out


def proc_info(pid: int) -> dict[str, Any]:
    import psutil
    try:
        proc = psutil.Process(pid)
        return {
            "pid": pid,
            "name": proc.name(),
            "created": datetime.fromtimestamp(proc.create_time()).strftime(TS_FMT),
            "created_epoch": round(proc.create_time(), 3),
            "cmdline": " ".join(proc.cmdline()),
            "exe": proc.exe(),
        }
    except Exception as exc:
        return {"pid": pid, "error": repr(exc)}


# ---------------------------------------------------------------------------
# GPU / RAM
# ---------------------------------------------------------------------------

def gpu_snapshot() -> dict[str, Any]:
    """nvidia-smi 一次；拿不到就如实登记不可用，不当成 0。"""
    cmd = ["nvidia-smi",
           "--query-gpu=name,utilization.gpu,memory.used,memory.total,memory.free",
           "--format=csv,noheader,nounits"]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=25)
    except Exception as exc:
        return {"available": False, "error": repr(exc)}
    text = raw.stdout.decode("utf-8", "replace")
    gpus = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5:
            gpus.append({"name": parts[0], "util_gpu_pct": parts[1],
                         "mem_used_mib": parts[2], "mem_total_mib": parts[3],
                         "mem_free_mib": parts[4]})
    out: dict[str, Any] = {"available": bool(gpus), "rc": raw.returncode, "gpus": gpus}
    if not gpus:
        out["stderr"] = raw.stderr.decode("utf-8", "replace")[:2000]
    return out


def ram_snapshot() -> dict[str, Any]:
    import psutil
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return {
        "ram_total_gb": round(vm.total / 2**30, 2),
        "ram_available_gb": round(vm.available / 2**30, 2),
        "ram_used_gb": round((vm.total - vm.available) / 2**30, 2),
        "ram_percent": vm.percent,
        "swap_total_gb": round(sw.total / 2**30, 2),
        "swap_used_gb": round(sw.used / 2**30, 2),
        "swap_percent": sw.percent,
    }


# ---------------------------------------------------------------------------
# HTTP 探针
# ---------------------------------------------------------------------------

def http_json(url: str, *, payload: dict | None = None, timeout: float = 20,
              headers: dict | None = None, max_bytes: int = 400_000) -> dict[str, Any]:
    """GET（payload=None）或 POST json。永不抛异常，一律回 {ok,status,body|error}。

    max_bytes 是硬上限：自写解析器不设上限会产出垃圾大文件（本机有过 3×10GB 的事故）。
    """
    data = None
    req_headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers,
                                method="POST" if data is not None else "GET")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(max_bytes)
            status = int(resp.status)
    except urllib.error.HTTPError as exc:
        body = exc.read(min(4000, max_bytes))
        return {"ok": False, "status": int(exc.code), "url": url,
                "error": f"HTTPError {exc.code}",
                "body_text": body.decode("utf-8", "replace"),
                "elapsed_ms": int((time.time() - started) * 1000)}
    except Exception as exc:
        return {"ok": False, "status": 0, "url": url, "error": repr(exc)[:400],
                "elapsed_ms": int((time.time() - started) * 1000)}
    elapsed_ms = int((time.time() - started) * 1000)
    try:
        parsed: Any = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        parsed = None
    return {"ok": 200 <= status < 300, "status": status, "url": url,
            "bytes": len(body), "json": parsed,
            "body_text": None if parsed is not None else body.decode("utf-8", "replace")[:4000],
            "elapsed_ms": elapsed_ms}


def port_open(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 项目配置只读读取
# ---------------------------------------------------------------------------

def read_json_file(rel: str) -> Any:
    try:
        return json.loads((REPO / rel).read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_read_error": repr(exc)[:300]}


def env_override(name: str) -> str:
    """运行期环境变量优先级说明用：这里读的是**本进程**的环境，不是 7860 那个进程的。"""
    return os.environ.get(name, "")


def bare_import_root() -> None:
    """让 runtime.character 在脚本里可用（与 tests/ 下其它证同一口径）。"""
    for p in (str(REPO), str(REPO / "runtime")):
        if p not in sys.path:
            sys.path.insert(0, p)


def fact_line(label: str, value: Any) -> str:
    return f"{label}={value}"


def short(v: Any, n: int = 120) -> str:
    text = str(v).replace("\n", "\\n")
    return text if len(text) <= n else text[:n] + f"…(+{len(text) - n})"
