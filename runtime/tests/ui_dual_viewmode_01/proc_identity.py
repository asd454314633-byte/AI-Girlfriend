"""proc_identity.py — 只读采集四个端口的进程世代与运行时状态指针。

用途：任务 §0 明令"不得重启 8010/8090/8765"。文件哈希证不了这件事，只有
PID + creation_time + cmdline 能证。走项目自带 python+psutil 通道，因为
沙箱 PowerShell 通道历史上对服务子进程枚举存在盲区（不得只凭它判"进程不存在"）。

    python proc_identity.py prechange
    python proc_identity.py postchange
"""
import json
import sys
import time
from pathlib import Path

import psutil

REPO = Path(__file__).resolve().parents[3]          # zero_replica（parents[2] 是 runtime/）
EV = REPO / "runtime" / "evidence" / "UI_DUAL_VIEWMODE_01"
PORTS = [7860, 8010, 8090, 8765]

# 这些是运行态文件，正常操作本来就会变；只记指针，不参与哈希门禁。
STATE_POINTERS = [
    "runtime/windows/avatar_runtime_state.json",
    "runtime/windows/active_tts_backend.txt",
    "runtime/windows/tts_switch_state.json",
    "runtime/windows/active_avatar.txt",
]


def listeners():
    out = {}
    for c in psutil.net_connections(kind="tcp"):
        if not c.laddr or c.status != psutil.CONN_LISTEN:
            continue
        if c.laddr.port in PORTS:
            out.setdefault(c.laddr.port, set()).add(c.pid)
    res = {}
    for port, pids in sorted(out.items()):
        rows = []
        for pid in sorted(pids):
            try:
                p = psutil.Process(pid)
                rows.append({
                    "pid": pid,
                    "name": p.name(),
                    "create_time_iso": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(p.create_time())),
                    "create_time": p.create_time(),
                    "cmdline": " ".join(p.cmdline())[:600],
                    "cwd": str(p.cwd()),
                })
            except Exception as e:  # noqa: BLE001 - 采集失败要显式记下来
                rows.append({"pid": pid, "error": str(e)[:200]})
        res[port] = rows
    return res


def established():
    """当前连到 8010/7860 的对端数量 —— 用来证明测试收尾没留会话。"""
    n = {p: 0 for p in PORTS}
    for c in psutil.net_connections(kind="tcp"):
        if c.status == psutil.CONN_ESTABLISHED and c.laddr and c.laddr.port in n:
            n[c.laddr.port] += 1
    return n


def pointers():
    res = {}
    for rel in STATE_POINTERS:
        p = REPO / rel
        try:
            txt = p.read_text(encoding="utf-8", errors="replace").strip()
        except Exception as e:  # noqa: BLE001
            res[rel] = {"unreadable": str(e)[:160]}
            continue
        if rel.endswith(".json"):
            try:
                j = json.loads(txt)
                res[rel] = {k: j.get(k) for k in
                            ("generation", "state", "listener_pid", "owner_pid",
                             "avatar_id", "phase", "result", "updated_at")}
            except Exception as e:  # noqa: BLE001
                res[rel] = {"raw_head": txt[:160], "parse_error": str(e)[:160]}
        else:
            res[rel] = {"value": txt[:160]}
    return res


def main():
    phase = sys.argv[1] if len(sys.argv) > 1 else "prechange"
    if phase not in ("prechange", "postchange"):
        print("usage: proc_identity.py [prechange|postchange]", file=sys.stderr)
        return 2
    d = EV / phase
    d.mkdir(parents=True, exist_ok=True)
    snap = {
        "phase": phase,
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "listeners": listeners(),
        "established_counts": established(),
        "state_pointers": pointers(),
    }
    out = d / "PROC_IDENTITY.json"
    out.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", out)
    for port, rows in snap["listeners"].items():
        for r in rows:
            print("  :%s pid=%s created=%s %s" % (port, r.get("pid"),
                  r.get("create_time_iso"), r.get("cmdline", r.get("error", ""))[:90]))
    print("  established:", snap["established_counts"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
