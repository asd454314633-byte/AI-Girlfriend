# -*- coding: utf-8 -*-
"""拿现成的旧数据验一次 G2 那把尺子本身：它对"明显随场景变"的文本也给不出可分性吗？

为什么必须做这一步：硬门槛 G2（同场景重抽之间的最大距离，必须小于跨场景的最小距离，
66 个场景对里 ≥90% 成立）判了 3/66。FAIL 有两种读法——判断器没在读场景，
或者这把尺子对任何自由生成的中文文本都到不了 0.90。这两种在交付里完全不同，
所以不能只凭 G2 一个数说"V2 不行"。

对照材料不用再造：V1 那三轮跑留下了 12 场景 × 3 抽的**正式回复**（bench_rows.jsonl，
arm=旧基线 / 真人自然）。那些话是回答不同场景里不同那句原话的，"随场景变"这件事
在这份材料上是人眼可证的（场景 1 在聊天气，场景 8 在吵架）。
如果同一条 G2 判在这份材料上也不过 0.90，那 G2 量的就不是"有没有随场景变"，
而是"自由文本的距离分布有没有可能不重叠"——答案是不可能，门槛定错了。

顺便跑 M2（归属测试 + 打乱标签对照）做同一份材料，两条一起看才知道差在哪。
一个 token 都不花：只读磁盘上已有的行。
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from analyze_judge_gate import attribution, attribution_control, separability  # noqa: E402

BENCH = HERE / "out" / "bench_rows.jsonl"
JUDGE = HERE / "out" / "judge_gate_rows.jsonl"


def load(path: Path, text_of, arms: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if arms is not None and str(row.get("arm")) not in arms:
            continue
        out.append({"scenario": int(row["scenario"]), "draw": int(row.get("draw") or 1),
                    "text": str(text_of(row) or "")})
    return out


def report(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_scene: dict[int, list[str]] = defaultdict(list)
    for row in rows:
        by_scene[row["scenario"]].append(row["text"])
    true = attribution(rows, lambda one: one["text"])
    ctrl = [attribution_control(rows, lambda one: one["text"], seed) for seed in (1, 2, 3, 4, 5)]
    first_draw = {row["scenario"]: row["text"] for row in rows if row["draw"] == 1}
    got = {
        "rows": len(rows), "scenes": len(by_scene),
        "distinct_first_draw": len(set(first_draw.values())),
        "G2_strict_separability": separability(by_scene),
        "M2_attribution": {"rate": true["rate"], "accuracy": true["accuracy"],
                           "chance": true["chance"]},
        "M2_control_max": max(one["rate"] for one in ctrl),
        "median_text_chars": int(sorted(len(one["text"]) for one in rows)[len(rows) // 2]),
    }
    print(f"--- {name}")
    print(json.dumps(got, ensure_ascii=False, indent=1))
    return {"name": name, **got}


def main() -> int:
    arms = tuple(sys.argv[1:]) or ("旧基线", "真人自然")
    out = []
    for arm in arms:
        out.append(report(f"V1 正式回复 arm={arm}",
                          load(BENCH, lambda one: one.get("content"), (arm,))))
    out.append(report("V2 本轮互动判断", load(JUDGE, lambda one: one.get("text"))))
    (HERE / "out" / "g2_instrument_control.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("OUT", HERE / "out" / "g2_instrument_control.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
