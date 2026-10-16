# -*- coding: utf-8 -*-
"""把 bench_rows.jsonl 汇成可判的每格数值。

口径上分两层，别混：

  * **能自动算的**：句长分布、同一格三抽的开头重复、跨抽反复片段、把他原句还回来的
    最长公共片段、客服腔候选命中。这些是数值，可以当场重算。
  * **不能自动算的**：回应方向合不合理、是不是被说动了、语气对不对。这些只能逐格读
    原文判，本脚本只把「同一格三抽 + 对应那格加了什么」摆在一起，方便对着看。

同一格内部三抽的差异当**噪声底**：跨档的差小于噪声底就不能声称是收益。
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"

ARM_ORDER = ("baseline", "human_natural", "autonomous", "story_director")
ARM_LABEL = {"baseline": "旧基线", "human_natural": "真人自然", "autonomous": "强角色自主",
             "story_director": "剧情推进"}


def repeated_bits(replies: list[str], min_count: int = 2) -> list[str]:
    counts: dict[str, int] = {}
    for n in range(3, 9):
        for reply in replies:
            cleaned = "".join(str(reply or "").split())
            seen: set[str] = set()
            for i in range(0, max(0, len(cleaned) - n + 1)):
                gram = cleaned[i:i + n]
                if gram not in seen:
                    seen.add(gram)
                    counts[gram] = counts.get(gram, 0) + 1
    hits = [g for g, c in counts.items() if c >= min_count]
    keep: list[str] = []
    for g in sorted(hits, key=len, reverse=True):
        if not any(g in one for one in keep):
            keep.append(g)
    return keep[:5]


def cell(rows: list[dict[str, Any]]) -> dict[str, Any]:
    replies = [str(one.get("content") or "") for one in rows]
    lengths = [len(one) for one in replies]
    openers = ["".join(one[:6].split()) for one in replies if one]
    fails = [one for one in rows if one.get("error")]
    return {
        "draws": len(rows),
        "failures": len(fails),
        "replies": replies,
        "lengths": lengths,
        "length_min": min(lengths) if lengths else 0,
        "length_max": max(lengths) if lengths else 0,
        "length_median": statistics.median(lengths) if lengths else 0,
        # 长短是否随场景自然变化：同一格里三抽的极差，和跨格的极差是两件事
        "length_spread_within": (max(lengths) - min(lengths)) if lengths else 0,
        "distinct_openers": len(set(openers)),
        "repeated_across_draws": repeated_bits(replies),
        "echo_any": sorted({one.get("echo_shared") or "" for one in rows} - {""}),
        "servile_any": sorted({hit for one in rows for hit in (one.get("servile_hits") or [])}),
        "cliche_any": sorted({hit for one in rows for hit in (one.get("cliche_hits") or [])}),
        "decide_any": sorted({hit for one in rows for hit in (one.get("decide_hits") or [])}),
        "added_chars": rows[0].get("added_chars") if rows else 0,
        "context_chars": rows[0].get("context_chars") if rows else 0,
        "wall_s_mean": round(statistics.mean([one["wall_s"] for one in rows
                                              if isinstance(one.get("wall_s"), (int, float))]), 2)
        if rows else 0,
    }


def load(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as src:
        for line in src:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def build(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for one in rows:
        grouped[(int(one["scenario"]), str(one["arm_key"]))].append(one)
    scenarios: dict[str, Any] = {}
    for (no, arm), bucket in sorted(grouped.items()):
        bucket.sort(key=lambda one: int(one.get("draw") or 0))
        scenarios.setdefault(str(no), {"requirement": bucket[0].get("requirement"),
                                       "user_text": "", "arms": {}})["arms"][arm] = cell(bucket)
    # 噪声底 = 三档各自「格内极差」的中位数；跨格比较时低于这个数不算差异
    floors = [one["length_spread_within"] for s in scenarios.values() for one in s["arms"].values()]
    summary = {
        "rows": len(rows),
        "cells": sum(len(s["arms"]) for s in scenarios.values()),
        "length_spread_noise_floor_median": statistics.median(floors) if floors else 0,
        "servile_by_arm": {arm: sum(1 for s in scenarios.values() for one in [s["arms"].get(arm)]
                                    if one and one["servile_any"]) for arm in ARM_ORDER},
        "echo_by_arm": {arm: sum(1 for s in scenarios.values() for one in [s["arms"].get(arm)]
                                 if one and one["echo_any"]) for arm in ARM_ORDER},
        "repeated_by_arm": {arm: sum(1 for s in scenarios.values() for one in [s["arms"].get(arm)]
                                     if one and one["repeated_across_draws"]) for arm in ARM_ORDER},
        "length_by_arm_median": {arm: (statistics.median([s["arms"][arm]["length_median"]
                                                          for s in scenarios.values()
                                                          if arm in s["arms"]])
                                       if any(arm in s["arms"] for s in scenarios.values()) else 0)
                                 for arm in ARM_ORDER},
        "scenarios": scenarios,
    }
    return summary


def markdown(summary: dict[str, Any]) -> str:
    lines = ["| 场景 | 档 | 三抽句长 | 格内极差 | 开头几种 | 跨抽反复 | 客服腔候选 | 还原句 |",
             "|---|---|---|---|---|---|---|---|"]
    for no in sorted(summary["scenarios"], key=int):
        scene = summary["scenarios"][no]
        for arm in ARM_ORDER:
            one = scene["arms"].get(arm)
            if not one:
                continue
            lines.append(f"| S{int(no):02d} {scene['requirement']} | {ARM_LABEL[arm]} | "
                         f"{'/'.join(str(x) for x in one['lengths'])} | {one['length_spread_within']} | "
                         f"{one['distinct_openers']}/{one['draws']} | "
                         f"{'、'.join(one['repeated_across_draws']) or '—'} | "
                         f"{'、'.join(one['servile_any']) or '—'} | "
                         f"{'、'.join(one['echo_any']) or '—'} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", default=str(OUT_DIR / "bench_rows.jsonl"))
    args = ap.parse_args(argv)
    summary = build(load(Path(args.rows)))
    (OUT_DIR / "bench_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                                encoding="utf-8")
    (OUT_DIR / "bench_table.md").write_text(markdown(summary) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "scenarios"}, ensure_ascii=False, indent=2))
    print(markdown(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
