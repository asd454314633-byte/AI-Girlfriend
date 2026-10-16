"""fa_trace_report.py — TASK_02 STEP 4/5 取证汇总。

汇总 conn_trace.js → /api/conn-trace 落盘的 JSONL（runtime/tests/conn_trace_live.jsonl），
输出 A/B 对比所需的四项计数 + flap 时间线：

    python fa_trace_report.py                 # 全量汇总
    python fa_trace_report.py --last 10m      # 只看最近 10 分钟
    python fa_trace_report.py --csv out.csv   # 附带逐事件 CSV

计数定义（A/B 比较字段）：
  disconnect_count   status=reconnecting 事件数（= transport 掉线次数）
  reconnect_count    reconnect 事件数（恢复尝试次数，含 attempt）
  reconnected_count  reconnected 事件数（成功恢复次数）
  failed_count       pc connectionState=failed + client-error + status=error
  ws_close_count     WebSocket close 事件数
  video_stall_count  media waiting/stalled 事件数
  flap               连续 reconnecting↔connected 对（|Δt| < 10s 记一次 flap）
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOG = Path(__file__).resolve().parent / "conn_trace_live.jsonl"


def parse_t(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", default="", help="只统计最近时长，如 10m / 30m / 1h")
    ap.add_argument("--csv", default="", help="可选：把事件导出为 CSV")
    args = ap.parse_args()

    if not LOG.exists():
        print(f"NO_LOG: {LOG} 尚未生成（确认 7860 UI 服务已重启加载 /api/conn-trace，"
              f"且真实页面已刷新装载 conn_trace.js）")
        return 2

    cutoff = None
    if args.last:
        unit = args.last[-1]
        n = int(args.last[:-1])
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n),
                 "s": timedelta(seconds=n)}[unit]
        cutoff = datetime.now(timezone.utc) - delta

    events = []
    with LOG.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            t = parse_t(ev.get("t") or ev.get("_server_t") or "")
            if cutoff and t and t < cutoff:
                continue
            events.append((t, ev))

    c = dict(disconnect_count=0, reconnect_count=0, reconnected_count=0,
             failed_count=0, ws_close_count=0, video_stall_count=0, flap=0)
    timeline = []
    last_disc = None
    for t, ev in events:
        kind = ev.get("kind")
        if kind == "reconnect":
            c["reconnect_count"] += 1
            timeline.append((t, f"reconnect attempt={ev.get('attempt')}"))
        elif kind == "reconnected":
            c["reconnected_count"] += 1
            timeline.append((t, "reconnected OK"))
        elif kind == "status":
            st = ev.get("status")
            if st == "reconnecting":
                c["disconnect_count"] += 1
                last_disc = t
                timeline.append((t, "DISCONNECT"))
            elif st == "error":
                c["failed_count"] += 1
                timeline.append((t, "STATUS_ERROR"))
        elif kind == "transport-drop":
            timeline.append((t, f"transport-drop reason={ev.get('reason')}"))
        elif kind == "client-error":
            c["failed_count"] += 1
            timeline.append((t, "client-error"))
        elif kind == "pc":
            if ev.get("field") == "conn":
                v = ev.get("value")
                if v == "failed":
                    c["failed_count"] += 1
                    timeline.append((t, "pc FAILED"))
                elif v in ("connected", "disconnected"):
                    timeline.append((t, f"pc {v}"))
        elif kind == "ws" and ev.get("event") == "close":
            c["ws_close_count"] += 1
            timeline.append((t, f"ws close code={ev.get('code')}"))
        elif kind == "media" and ev.get("event") in ("waiting", "stalled"):
            c["video_stall_count"] += 1
            timeline.append((t, f"video {ev.get('event')} t={ev.get('currentTime')}"))

    # flap：disconnect 与下一次成功恢复间隔 <10s 记一次抖动
    for i, (t, label) in enumerate(timeline):
        if label == "DISCONNECT":
            for t2, label2 in timeline[i + 1:i + 6]:
                if label2 in ("reconnected OK", "pc connected") and t and t2:
                    if (t2 - t).total_seconds() < 10:
                        c["flap"] += 1
                    break

    print(f"LOG={LOG}")
    print(f"events={len(events)}  window={args.last or 'ALL'}")
    print("-" * 48)
    for k, v in c.items():
        print(f"{k:>18} = {v}")
    print("-" * 48)
    verdict = ("STABLE（无可观测掉线）" if c["disconnect_count"] == 0 and c["failed_count"] == 0
               else f"有 {c['disconnect_count']} 次掉线 / {c['flap']} 次 flap / "
                    f"{c['failed_count']} 次 failed")
    print("SUMMARY:", verdict)

    if timeline:
        print("\n最近 30 条状态变化：")
        for t, label in timeline[-30:]:
            print(f"  {t.strftime('%H:%M:%S') if t else '??':>8}  {label}")

    if args.csv:
        import csv
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t", "kind", "detail"])
            for t, ev in events:
                w.writerow([t.isoformat() if t else "", ev.get("kind"),
                            json.dumps({k: v for k, v in ev.items()
                                        if k not in ("t", "_server_t", "kind")},
                                       ensure_ascii=False)])
        print(f"\nCSV -> {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
