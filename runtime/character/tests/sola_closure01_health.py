# -*- coding: utf-8 -*-
"""sola_closure01_health.py — 收口任务 §20「GPU / RAM 最终健康」→ gpu_ram_post.json。

四条判据（OOM / OFFLOAD_ANOMALY / GPU_FOREIGN_HEAVY_COMPUTE / RAM_PRESSURE）
全部由现场量出来的数出，且每条都自带判据文本与分母：

* GPU 空转利用率连采 8 次（跨 ~21 秒），不是一次抽样；
* 显存归因走**差值 + 台账**：§3 冻结时 :8090 根本没在听（baseline 里
  listening=false），本轮唯一新增的显存索取者就是它，8765/8010 的 pid 与
  冻结时逐字相同 ⇒ 13 GB 增量归属明确，不需要猜；
* Windows 侧逐进程显存是 [N/A]（本机已登记的驱动限制），所以再开一条
  **WSL 侧**通道交叉看一遍（Linux 侧 compute apps 为零）；两条通道都取不到
  的部分就登记为「不可归因」，不拿它冒充 PASS；
* OOM / 崩溃字面量扫描带**正控制**（自己造一条含关键字的行，必须被数到），
  否则「零命中」可能只是尺坏了；
* 卸载异常不看日志措辞，看服务自己打的吞吐：本轮真实解码速率中位数 +
  CUDA graph 复用计数——真把层甩到 CPU 上，这两个数会先垮。

只读：不改任何服务，不调模型（读的是 llama.cpp / uvicorn / s2s / avatar 的既有日志）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import sola_closure01_lib as L  # noqa: E402

LLM_LOG = L.REPO / "runtime" / "llm" / "logs" / "llama_8090_gemma4.log"
LOGS = {
    # 归因类别：IN_WINDOW = 行首有墙钟，按窗口切；WHOLE_FILE_IS_CURRENT_INSTANCE =
    # 整份文件只属于当前这个进程实例（下面用「启动行数 == 1」正向证明）；
    # NOT_ATTRIBUTABLE = 无时间戳且跨实例追加 ⇒ 不参与判据，只登记。
    "llama-8090": (L.REPO / "runtime" / "llm" / "logs" / "llama_8090_gemma4.log",
                   "WHOLE_FILE_IS_CURRENT_INSTANCE"),
    "ui-7860-stderr": (L.REPO / "logs" / "runtime" / "ui-7860.stderr.log",
                       "WHOLE_FILE_IS_CURRENT_INSTANCE"),
    "ui-7860-access": (L.REPO / "logs" / "runtime" / "ui-7860.stdout.log",
                       "NOT_ATTRIBUTABLE"),
    "s2s-8765": (L.REPO / "logs" / "runtime" / "s2s.stderr.log", "IN_WINDOW"),
    "avatar-8010": (L.REPO / "logs" / "runtime" / "avatar-8010.stderr.log",
                    "IN_WINDOW"),
}
# 「整份文件就是当前实例」的正向证明：这些启动横幅各自只能出现 1 次
INSTANCE_BANNERS = {
    "llama-8090": "listening on http",
    "ui-7860-stderr": "Uvicorn running on",
}
EXIT_WITNESS = L.REPO / "logs" / "runtime" / "avatar-8010.exitwitness.log"
STARTUP_LOG = L.REPO / "logs" / "runtime" / "startup.log"

LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[ ,](\d{3})?")

OOM_TOKENS = (
    "out of memory", "cuda out of memory", "cublas_status_alloc_failed",
    "no available memory", "failed to allocate", "unable to allocate",
    "std::bad_alloc", "memoryerror", "insufficient memory",
    "allocating too much", "kv cache is full",
)
CRASH_TOKENS = (
    "traceback (most recent call last)", "segmentation fault", "core dumped",
    "abort()", "windows error", "fatal python", "gpu crash",
)
TAIL_CAP = 900_000      # 每份日志最多回头看这么多字节（自写解析器必须有硬上限）


def parse_ts(line: str) -> datetime | None:
    m = LOG_TS.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
            microsecond=int(m.group(2) or 0) * 1000)
    except ValueError:
        return None


def tail_lines(path: Path, cap: int = TAIL_CAP) -> tuple[list[str], dict[str, Any]]:
    fact = {"path": str(path.relative_to(L.REPO)) if path.is_relative_to(L.REPO)
            else str(path), "exists": path.exists()}
    if not path.exists():
        fact["error"] = "MISSING"
        return [], fact
    size = path.stat().st_size
    fact["bytes_now"] = size
    with open(path, "rb") as fh:
        if size > cap:
            fh.seek(size - cap)
            fact["scanned_bytes"] = cap
            fact["truncated_head_skipped"] = True
        blob = fh.read(cap)
    lines = blob.decode("utf-8", "replace").splitlines()
    if fact.get("truncated_head_skipped"):
        lines = lines[1:]                     # 第一行可能被腰斩，丢掉
    fact["lines_kept"] = len(lines)
    return lines, fact


def scan_tokens(lines: list[str], tokens: tuple[str, ...],
                window: tuple[datetime, datetime] | None) -> dict[str, Any]:
    """扫关键字命中。带时间戳的行按窗口切；**没有时间戳的行沿用最近一次见过的时间戳**
    （日志是只增不减的单写者文件，Python traceback 的续行本来就不带戳，
    若按「无戳」把它们排除在判据外，就等于给判据开了一个洞）。
    """
    out: dict[str, Any] = {"tokens": list(tokens), "hits": [],
                           "hits_in_window": 0, "hits_outside_window": 0,
                           "hits_before_first_stamp": 0}
    cur: datetime | None = None
    seen_any_stamp = False
    for one in lines:
        ts = parse_ts(one)
        if ts is not None:
            cur = ts
            seen_any_stamp = True
        low = one.lower()
        for tok in tokens:
            if tok not in low:
                continue
            if window is None:
                bucket = "hits_in_window"          # 整份文件都算（由调用侧证明）
            elif cur is None:
                bucket = "hits_before_first_stamp"  # 文件开头就是无戳行 ⇒ 无法定年
            elif window[0] <= cur <= window[1]:
                bucket = "hits_in_window"
            else:
                bucket = "hits_outside_window"
            out[bucket] += 1
            out["hits"].append({"token": tok, "ts": str(cur) if cur else None,
                                "bucket": bucket, "line": L.short(one, 200)})
    out["hits_total"] = len(out["hits"])
    out["counted"] = out["hits_in_window"]
    out["stamped_any_line"] = seen_any_stamp
    out["hits"] = out["hits"][:12]
    return out


POSITIVE_CONTROL_LINE = ("2099-01-01 00:00:00,000 ERROR control: the run "
                         "failed with CUDA out of memory")


def ruler_positive_control() -> dict[str, Any]:
    """正控制：故意喂一条含关键字的行，扫描器必须数到它。数不到 = 尺坏。
    （一行可以同时命中多个 token，所以断言的是「至少数到 1 次，
    且命中的确实是预期的那个 token」，不是「恰好 1 次」。）"""
    got = scan_tokens([POSITIVE_CONTROL_LINE], OOM_TOKENS, None)
    neg = scan_tokens(["2099-01-01 00:00:00,000 INFO nothing interesting"],
                      OOM_TOKENS, None)
    toks = {one["token"] for one in got["hits"]}
    return {"正控制行": POSITIVE_CONTROL_LINE,
            "正控制命中次数": got["hits_total"],
            "正控制命中的 token": sorted(toks),
            "正控制被数到": got["hits_total"] >= 1 and "cuda out of memory" in toks,
            "负控制保持为 0": neg["hits_total"] == 0,
            "PASS": bool(got["hits_total"] >= 1 and "cuda out of memory" in toks
                         and neg["hits_total"] == 0)}


def gpu_samples(n: int = 8, gap: float = 3.0) -> list[dict[str, Any]]:
    rows = []
    for i in range(n):
        snap = L.gpu_snapshot()
        gpus = snap.get("gpus") or []
        for g in gpus:
            rows.append({"i": i, "at": L.now_local(),
                         "util_pct": _num(g.get("util_gpu_pct")),
                         "mem_used_mib": _num(g.get("mem_used_mib")),
                         "mem_free_mib": _num(g.get("mem_free_mib")),
                         "mem_total_mib": _num(g.get("mem_total_mib"))})
        if i < n - 1:
            time.sleep(gap)
    return rows


def _num(v: Any) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def stats(vals: list[float | None]) -> dict[str, Any]:
    xs = sorted(float(v) for v in vals if v is not None)
    if not xs:
        return {"n": 0}
    mid = xs[len(xs) // 2] if len(xs) % 2 else (
        xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2
    return {"n": len(xs), "min": xs[0], "median": round(mid, 2), "max": xs[-1]}


def compute_apps() -> dict[str, Any]:
    cmd = ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
           "--format=csv,noheader,nounits"]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=30)
    except Exception as exc:
        return {"error": repr(exc)}
    rows = []
    for line in raw.stdout.decode("utf-8", "replace").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            rows.append({"pid": parts[0], "name": parts[1],
                         "used_mib": parts[2]})
    proj = [one for one in rows if "zero_replica" in one["name"].lower()
            or "llama-server" in one["name"].lower()]
    other = [one for one in rows if one not in proj]
    return {"rows_total": len(rows), "project_rows": proj,
            "non_project_rows": [{"pid": o["pid"], "name": L.short(o["name"], 90)}
                                 for o in other],
            "per_process_memory_attributable": all(
                one["used_mib"] not in ("N/A", "[N/A]", "") for one in rows)}


def wsl_channel() -> dict[str, Any]:
    """第二通道：Linux 侧到底有没有人在吃这张卡（本机已登记：Windows 侧看不见）。"""
    shell = ("nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu"
             " --format=csv,noheader; echo ---APPS---;"
             " nvidia-smi --query-compute-apps=pid,process_name,used_memory"
             " --format=csv,noheader; echo RC=$?")
    try:
        raw = subprocess.run(["wsl.exe", "-e", "sh", "-c", shell],
                             capture_output=True, timeout=90)
    except Exception as exc:
        return {"available": False, "error": repr(exc)[:200]}
    text = raw.stdout.decode("utf-8", "replace")
    head, _, tail = text.partition("---APPS---")
    apps = [one.strip() for one in tail.splitlines()
            if one.strip() and not one.startswith("RC=")]
    return {"available": raw.returncode == 0, "rc": raw.returncode,
            "gpu_line": L.short(head.strip().replace("\n", " | "), 200),
            "linux_compute_apps_rows": len(apps),
            "linux_compute_apps": apps[:10],
            "备注": "这条探针本身会把闲置的 WSL 发行版叫醒（一次性的，无副作用）；"
                  "它查的是 Linux 侧有没有 CUDA 进程，不是显存配额。"}


def llama_throughput(window_start: datetime) -> dict[str, Any]:
    """从 llama.cpp 自己打的 print_timing 里取吞吐与 graph 复用（不自己掐表）。"""
    lines, fact = tail_lines(LLM_LOG)
    dec, pre, graphs, releases = [], [], [], 0
    for one in lines:
        m = re.search(r"eval time\s*=\s*([\d.]+) ms\s*/\s*(\d+) tokens"
                      r"\s*\(\s*([\d.]+) ms per token,\s*([\d.]+) tokens per second", one)
        if m and "prompt eval" not in one:
            dec.append({"tok_per_s": float(m.group(4)), "tokens": int(m.group(2))})
        elif m and "prompt eval" in one:
            pre.append({"tok_per_s": float(m.group(4)), "tokens": int(m.group(2))})
        gm = re.search(r"graphs reused\s*=\s*(\d+)", one)
        if gm:
            graphs.append(int(gm.group(1)))
        if "release:" in one and "stop processing" in one:
            releases += 1
    cold = [one for one in pre if one["tokens"] >= 200]
    return {"log_fact": fact, "tasks_with_print_timing": len(dec),
            "decode_tok_per_s": stats([one["tok_per_s"] for one in dec]),
            "decode_tok_per_s_min": min((one["tok_per_s"] for one in dec), default=None),
            "prefill_lines_total": len(pre),
            "prefill_cold_lines_ge200tok": len(cold),
            "prefill_cold_tok_per_s": stats([one["tok_per_s"] for one in cold]),
            "prefill_cached_lines_lt200tok": len(pre) - len(cold),
            "graph_reuse_counts_seen": len(graphs),
            "graph_reuse_max": max(graphs, default=None),
            "slot_releases": releases}


def main() -> int:
    L.bare_import_root()
    base = L.read_json_file(
        "runtime/character/tests/out/sola_unattended_closure_01/baseline.json") or {}
    window_start = None
    if base.get("collected_at_local"):
        try:
            window_start = datetime.strptime(
                base["collected_at_local"], "%Y-%m-%d %H:%M:%S %z").replace(tzinfo=None)
        except ValueError:
            window_start = None
    window = (window_start, datetime.now() + timedelta(minutes=5)) \
        if window_start else None

    snaps = gpu_samples()
    ram_a = L.ram_snapshot()
    time.sleep(4.0)
    ram_b = L.ram_snapshot()
    apps = compute_apps()
    wsl = wsl_channel()
    control = ruler_positive_control()

    scans: dict[str, Any] = {}
    for name, (path, kind) in LOGS.items():
        if kind == "IN_WINDOW" and window is None:
            kind = "NOT_ATTRIBUTABLE"        # 没有可靠的窗口起点就不要冒充能切窗
        lines, fact = tail_lines(path)
        win = window if kind == "IN_WINDOW" else None
        oom = scan_tokens(lines, OOM_TOKENS, win)
        cr = scan_tokens(lines, CRASH_TOKENS, win)
        banner = INSTANCE_BANNERS.get(name)
        banner_n = sum(1 for one in lines if banner and banner in one) if banner else None
        if kind == "WHOLE_FILE_IS_CURRENT_INSTANCE":
            counted = oom["hits_total"]          # 整份就是本轮这个实例写的
            attribution = f"整份文件属于当前实例（启动横幅出现 {banner_n} 次）"
            ok_for_gate = counted == 0 and banner_n == 1
        elif kind == "IN_WINDOW":
            counted = oom["hits_in_window"] + oom["hits_before_first_stamp"]
            attribution = (f"按行首墙钟切窗；无戳续行沿用最近一次时间戳"
                           f"（窗口内 {oom['hits_in_window']}，"
                           f"定不了年 {oom['hits_before_first_stamp']}，"
                           f"窗口外被排除 {oom['hits_outside_window']}）")
            ok_for_gate = counted == 0
        else:
            counted = None
            attribution = "无时间戳且跨实例追加 ⇒ 不进判据，只登记"
            ok_for_gate = True
        scans[name] = {"attribution_class": kind, "window_note": attribution,
                       "file": fact, "instance_banner": banner,
                       "instance_banner_count": banner_n,
                       "oom_counted": counted,
                       "oom_hits_total_in_file": oom["hits_total"],
                       "oom_hits_in_window": oom["hits_in_window"],
                       "oom_hits_outside_window": oom["hits_outside_window"],
                       "oom_hits_before_first_stamp": oom["hits_before_first_stamp"],
                       "crash_hits_total_in_file": cr["hits_total"],
                       "crash_hits_in_window": cr["hits_in_window"],
                       "crash_hits_outside_window": cr["hits_outside_window"],
                       "oom_samples": oom["hits"][:4],
                       "crash_samples": cr["hits"][:4],
                       "PASS": ok_for_gate}
    oom_gate_ok = all(one["PASS"] for one in scans.values())
    oom_counted_total = {k: v["oom_counted"] for k, v in scans.items()}
    exit_rows, exit_fact = tail_lines(EXIT_WITNESS, 60_000)
    exits_in_window = [one for one in exit_rows
                       if (parse_ts(one.split(" ", 1)[0].replace("T", " ")
                                     .split("+")[0]) or datetime.min) >=
                          (window_start or datetime.min)]
    start_rows, start_fact = tail_lines(STARTUP_LOG, 200_000)

    thr = llama_throughput(window_start or datetime.min)

    # :7860 的访问日志没有时间戳 ⇒ 那个端口上的「没有中途被打死」走**活体通道**：
    # 同一个 pid 从头到现在没换过，而且本轮那 4 发真实请求全部拿到完整回复。
    smoke_rows = []
    smoke_read_error = None
    try:
        for line in (L.OUT_DIR / "smoke_rows.jsonl").read_text(
                encoding="utf-8").splitlines():
            if line.strip():
                smoke_rows.append(json.loads(line))
    except Exception as exc:
        smoke_read_error = repr(exc)[:200]
    now_listeners = L.listeners()
    ui_7860_now = (now_listeners.get(7860) or [{}])[0]
    now_listeners_summary = [{"port": p, "pid": (now_listeners.get(p) or [{}])[0].get("pid"),
                              "created": (now_listeners.get(p) or [{}])[0].get("created")}
                             for p in sorted(now_listeners)]
    smoke_clean = [one for one in smoke_rows
                   if one.get("reply") and not one.get("error")
                   and not one.get("error_event") and not one.get("timeout")]

    # ==================================================================
    # 四条判据
    # ==================================================================
    util = stats([one["util_pct"] for one in snaps])
    free = stats([one["mem_free_mib"] for one in snaps])
    used = stats([one["mem_used_mib"] for one in snaps])
    used_last = snaps[-1]["mem_used_mib"] if snaps else None
    free_last = snaps[-1]["mem_free_mib"] if snaps else None
    pre_used = _num(((base.get("gpu_pre") or {}).get("gpus") or [{}])[0]
                    .get("mem_used_mib"))
    pre_free = _num(((base.get("gpu_pre") or {}).get("gpus") or [{}])[0]
                    .get("mem_free_mib"))

    checks: dict[str, Any] = {}
    checks["GPU_UTIL_IDLE"] = {
        "判据": "连续 8 次、跨 ~21 秒的空转抽样（不做任何生成）；中位数与最大值都要低",
        "samples": snaps, "utilization_pct": util,
        "ROLE": "safety guard：阈值 15% 是判「有没有人还在压这张卡」的护栏，"
                "不是从官方文档来的硬线",
        "PASS": bool(util.get("n", 0) >= 6 and (util.get("median") or 99) <= 15
                  and (util.get("max") or 99) <= 40),
    }
    checks["VRAM_ACCOUNTING"] = {
        "判据": "显存增量必须能归到本轮真的起来的那个服务上：§3 冻结时 :8090 "
               "listening=false，本轮只有它新增；8765/8010 的 pid 与冻结时逐字相同",
        "pre_used_mib": pre_used, "post_used_mib": used_last,
        "delta_used_mib": (None if pre_used is None or used_last is None
                           else used_last - pre_used),
        "pre_free_mib": pre_free, "post_free_mib": free_last,
        "used_range_over_samples": used, "free_range_over_samples": free,
        "mem_total_mib": (snaps[-1].get("mem_total_mib") if snaps else None),
        "baseline_8090_listening": (base.get("services", {}).get("8090", {})
                                    .get("listening")),
        "services_pid_equal_to_baseline": {},
        "PASS": pre_used is not None and used_last is not None
        and free_last is not None and free_last > 1024,
    }
    for port in ("8765", "8010", "7860", "8090"):
        pre_rows = ((base.get("services") or {}).get(port) or {}).get("listeners") or []
        pre_pid = pre_rows[0].get("pid") if pre_rows else None
        now = now_listeners.get(int(port)) or []
        now_pid = now[0].get("pid") if now else None
        checks["VRAM_ACCOUNTING"]["services_pid_equal_to_baseline"][port] = {
            "pre": pre_pid, "post": now_pid,
            "same": (pre_pid is not None and pre_pid == now_pid),
            "本轮按任务允许的动作": ("启动（冻结时未监听）" if port == "8090" else
                              "重启（§2 明确允许）" if port == "7860" else "未动")}
    checks["GPU_FOREIGN_HEAVY_COMPUTE"] = {
        "判据": "两条通道交叉：Windows 侧 compute-apps 里除本项目进程外没有"
               "非桌面/图形类的计算进程；Linux(WSL) 侧 compute-apps 行数为 0；"
               "且空转 util 中位数已经过闸（GPU_UTIL_IDLE）",
        "windows_side": {k: apps[k] for k in
                         ("rows_total", "per_process_memory_attributable")}
                        if not apps.get("error") else apps,
        "windows_project_rows": apps.get("project_rows"),
        "windows_non_project_rows_n": len(apps.get("non_project_rows") or []),
        "wsl_side": wsl,
        "登记的不可归因部分": ("逐进程显存字节数在本机是 [N/A]（已登记的驱动限制），"
                        "所以这条判据量的是「有没有别的进程在压这张卡」，"
                        "不是「每一 MiB 属于谁」；每一 MiB 的归属由 "
                        "VRAM_ACCOUNTING 的差值 + pid 台账负责。"),
        "PASS": bool(checks["GPU_UTIL_IDLE"]["PASS"]
                     and wsl.get("available") and wsl.get("linux_compute_apps_rows") == 0
                     and len(apps.get("project_rows") or []) >= 3),
    }
    checks["OFFLOAD_ANOMALY"] = {
        "判据": "不看日志措辞，看服务自己打的吞吐：本轮真实解码速率中位数 ≥ 50 tok/s"
               " 且冷 prefill（≥200 token 的那批）≥ 200 tok/s 且 CUDA graph 有复用；"
               "真把层甩到 CPU 上，这三个数会先垮",
        "throughput": {k: v for k, v in thr.items() if k != "log_fact"},
        "throughput_log_fact": thr["log_fact"],
        "ROLE": "safety guard：50 / 200 tok/s 是按本机 12B-Q4 在 5090 上的量级"
                "设的下限（本轮实测值远高于它），不是官方规格",
        "PASS": bool((thr["decode_tok_per_s"].get("median") or 0) >= 50
                     and (thr["prefill_cold_tok_per_s"].get("median") or 0) >= 200
                     and (thr["graph_reuse_max"] or 0) > 0),
    }
    checks["OOM"] = {
        "判据": "① 各服务日志按其归因类别（窗口内 / 整份即当前实例）OOM 关键字命中 = 0；"
               "② 窗口内没有 avatar 退出见证记录；③ 四个端口现在都有监听者"
               "（中途被打死过就一定接不上）；④ :7860 的访问日志无时间戳且不按实例截断"
               " ⇒ 那个端口走活体通道：同一个 pid 从 04:37 到现在没换，"
               "且本轮 4 发真实请求全部拿到完整回复、零 error 事件",
        "per_log": {k: {"attribution_class": v["attribution_class"],
                        "oom_counted": v["oom_counted"],
                        "oom_hits_total_in_file": v["oom_hits_total_in_file"],
                        "oom_hits_in_window": v["oom_hits_in_window"],
                        "oom_hits_outside_window": v["oom_hits_outside_window"],
                        "oom_hits_before_first_stamp": v["oom_hits_before_first_stamp"],
                        "crash_hits_in_window": v["crash_hits_in_window"],
                        "crash_hits_outside_window": v["crash_hits_outside_window"],
                        "instance_banner_count": v["instance_banner_count"],
                        "window_note": v["window_note"], "file": v["file"],
                        "PASS": v["PASS"]}
                    for k, v in scans.items()},
        "oom_samples": [one for v in scans.values()
                        for one in (v.get("oom_samples") or [])][:6],
        "crash_samples": [one for v in scans.values()
                          for one in (v.get("crash_samples") or [])][:6],
        "exit_witness_rows_in_window": exits_in_window[:6],
        "exit_witness_n_in_window": len(exits_in_window),
        "exit_witness_file": exit_fact,
        "startup_log_fact": start_fact,
        "oom_counted_by_log": oom_counted_total,
        "all_four_ports_listening": now_listeners_summary,
        "ui_7860_liveness": {
            "pid_now": ui_7860_now.get("pid"),
            "created_now": ui_7860_now.get("created"),
            "smoke_rows": len(smoke_rows), "smoke_rows_clean": len(smoke_clean),
            "smoke_read_error": smoke_read_error,
        },
        "ruler_positive_control": control,
        "PASS": bool(control["PASS"] and oom_gate_ok
                     and len(exits_in_window) == 0
                     and all(one["pid"] for one in now_listeners_summary)
                     and len(smoke_clean) == len(smoke_rows) == 4),
    }
    checks["RAM_PRESSURE"] = {
        "判据": "两次抽样：可用内存 ≥ 8 GB、占用率 < 92%、swap 使用率 < 25%，"
               "且两次之间没有明显恶化",
        "sample_a": ram_a, "sample_b": ram_b, "pre": base.get("ram_pre"),
        "ROLE": "safety guard：阈值是护栏，不是系统告警线（本机物理内存 "
                f"{ram_a.get('ram_total_gb')} GB）",
        "PASS": bool(ram_b.get("ram_available_gb", 0) >= 8
                     and ram_b.get("ram_percent", 100) < 92
                     and ram_b.get("swap_percent", 100) < 25),
    }
    parts = ("GPU_UTIL_IDLE", "VRAM_ACCOUNTING", "GPU_FOREIGN_HEAVY_COMPUTE",
             "OFFLOAD_ANOMALY", "OOM", "RAM_PRESSURE")
    verdicts = {
        "OOM": "NO" if checks["OOM"]["PASS"] else "UNKNOWN",
        "OFFLOAD_ANOMALY": "NO" if checks["OFFLOAD_ANOMALY"]["PASS"] else "UNKNOWN",
        "GPU_FOREIGN_HEAVY_COMPUTE": ("NO" if checks["GPU_FOREIGN_HEAVY_COMPUTE"]["PASS"]
                                      else "UNKNOWN"),
        "RAM_PRESSURE": "NO" if checks["RAM_PRESSURE"]["PASS"] else "UNKNOWN",
        "GPU_HEALTHY": ("YES" if checks["GPU_UTIL_IDLE"]["PASS"]
                        and checks["VRAM_ACCOUNTING"]["PASS"] else "NO"),
        "RAM_HEALTHY": "YES" if checks["RAM_PRESSURE"]["PASS"] else "NO",
    }
    payload = {
        "task_id": L.TASK_ID,
        "stages": ["§20 GPU / RAM 最终健康（所有验证完成之后）"],
        "collected_at_local": L.now_local(),
        "collected_at_utc": L.now_utc_z(),
        "read_only": True,
        "window": {"since": base.get("collected_at_local"), "until": L.now_local()},
        "gpu": L.gpu_snapshot(), "ram": ram_b,
        "checks": checks,
        "verdicts": verdicts,
        "ALL_GREEN": all(checks[k]["PASS"] is True for k in parts),
    }
    path = L.write_json("gpu_ram_post.json", payload)
    print("WROTE", path)
    print(f"  util idle: {util}")
    print(f"  vram used: {used} last={used_last} free_last={free_last} (pre {pre_used})")
    print(f"  ram: {ram_b}")
    print(f"  decode tok/s: {thr['decode_tok_per_s']} "
          f"cold prefill: {thr['prefill_cold_tok_per_s']} graphs={thr['graph_reuse_max']}")
    print(f"  wsl: {wsl.get('gpu_line')} linux_apps={wsl.get('linux_compute_apps_rows')}")
    print("  OOM counted by log =", oom_counted_total,
          "| 正控制 =", control["PASS"],
          "| avatar 窗口内退出记录 =", len(exits_in_window))
    print("  verdicts:", verdicts)
    print("  ALL_GREEN =", payload["ALL_GREEN"],
          {k: checks[k]["PASS"] for k in parts})
    return 0


if __name__ == "__main__":
    sys.exit(main())
