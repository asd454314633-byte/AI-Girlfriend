# -*- coding: utf-8 -*-
"""gpu/ram 快照 — 任务 §3 与 §20 的两把尺：`--phase pre` / `--phase post`。

只读。附带列出「谁在吃显存」的候选进程（按名字与命令行归属，不按名字猜结论）；
本机显存归因有已登记的坑（WSL 里的 Linux CUDA 进程 Windows 侧看不见，
见主交接与 MEMORY），所以这里同时探一次 WSL 的 /dev/dxg 使用者，取不到就登记不可见。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sola_closure01_lib as L  # noqa: E402

HEAVY_HINTS = ("python", "llama-server", "pwsh", "ffmpeg", "LiveTalking", "comfy", "pt_main")


def top_gpu_processes() -> list[dict]:
    cmd = ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
           "--format=csv,noheader,nounits"]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=25)
    except Exception as exc:
        return [{"error": repr(exc)}]
    rows = []
    for line in raw.stdout.decode("utf-8", "replace").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            rows.append({"pid": parts[0], "name": parts[1], "used_mib": parts[2]})
    if not rows:
        rows.append({"note": "nvidia-smi 无 compute-apps 行（本机已登记：共享 GPU 下该表可能为空/N-A）",
                     "stderr": raw.stderr.decode("utf-8", "replace")[:500]})
    return rows


def project_processes() -> list[dict]:
    import psutil
    rows = []
    me = psutil.Process().pid
    for proc in psutil.process_iter(["pid", "name", "createTime", "cmdline"]):
        try:
            name = str(proc.info.get("name") or "")
            cmd = " ".join(proc.info.get("cmdline") or [])
            if proc.pid == me:
                continue
            if not any(h.lower() in name.lower() or h.lower() in cmd.lower() for h in HEAVY_HINTS):
                continue
            if "zero_replica" not in cmd and "llama-server" not in name.lower():
                continue
            rows.append({
                "pid": proc.pid, "name": name,
                "created": L.proc_info(proc.pid).get("created", "-"),
                "cmdline": L.short(cmd, 260),
            })
        except Exception:
            continue
    return rows


def pre_from_baseline() -> int:
    """把 §3 冻结时**已经量过**的那一份前置快照落成交付件要求的文件名。

    这不是「现在再量一次当 pre」——数字与时间戳全部沿用 baseline.json 里
    collected_at_local 那一刻的读数，只是换个文件放好并写清来路（§22 点名要
    gpu_ram_pre.json，而 pre 只有一次机会：必须在动手之前量）。
    """
    base = L.read_json_file(
        "runtime/character/tests/out/sola_unattended_closure_01/baseline.json") or {}
    if not base.get("gpu_pre") or not base.get("ram_pre"):
        print("baseline.json 里没有 gpu_pre/ram_pre —— 不写，避免造一份假 pre")
        return 3
    payload = {
        "task_id": L.TASK_ID,
        "phase": "pre",
        "collected_at_local": base.get("collected_at_local"),
        "collected_at_utc": base.get("collected_at_utc"),
        "gpu": base["gpu_pre"],
        "ram": base["ram_pre"],
        "provenance": {
            "来源": "baseline.json（§3 冻结现场那一次读数）",
            "为什么不是现场重测": "pre 的语义就是「动手之前」；本轮动手前只有那一次读数。"
                              "事后重测会把它变成 post，所以这里只做搬家，不动数值。",
            "baseline_sha256": L.sha256_file(L.OUT_DIR / "baseline.json"),
        },
        "services_at_pre": base.get("services"),
    }
    path = L.write_json("gpu_ram_pre.json", payload)
    print("WROTE", path, "(搬家自 baseline.json，未重测)")
    print("gpu_pre:", payload["gpu"].get("gpus") or payload["gpu"])
    print("ram_pre:", payload["ram"])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=("pre", "post", "pre-from-baseline"),
                    required=True)
    args = ap.parse_args()
    if args.phase == "pre-from-baseline":
        return pre_from_baseline()
    snap_services = {str(p): rows for p, rows in L.listeners().items()}
    snap = {
        "task_id": L.TASK_ID,
        "phase": args.phase,
        "collected_at_local": L.now_local(),
        "collected_at_utc": L.now_utc_z(),
        "gpu": L.gpu_snapshot(),
        "ram": L.ram_snapshot(),
        "nvidia_compute_apps": top_gpu_processes(),
        "zero_replica_processes": project_processes(),
        "services": snap_services,
    }
    path = L.write_json(f"gpu_ram_{args.phase}.json", snap)
    print(f"WROTE {path}")
    print("gpu:", snap["gpu"].get("gpus") or snap["gpu"])
    print("ram:", snap["ram"])
    print("compute_apps:", snap["nvidia_compute_apps"])
    for port, rows in snap_services.items():
        print(f":{port} " + ("; ".join(
            f"pid={r['pid']} created={r.get('created', '-')}" for r in rows) or "no listener"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
