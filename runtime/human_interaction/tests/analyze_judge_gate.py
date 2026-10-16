# -*- coding: utf-8 -*-
"""把已经跑完的 36 行判断回执再读一遍：变化到底是「随场景」还是「随重抽」。

不碰 8090，一个 token 都不再花——只读 judge_gate_rows.jsonl。
这份分析存在的理由：G2（严格可分性）判了 FAIL，而 FAIL 有两种可能——
判断器真的没在读场景，或者我的尺子对自由文本不适用。这两种在交付里必须分开写，
所以这里量的不是"能不能过门槛"，而是"信号有多少"：门槛已经判过了，不回头改。

三个量：

  M1 面距离（预登记那条的复算）：整块 与 只留值 两种口径下的
     跨场景最小距离 vs 同场景最大距离。看头部与六个键名有没有把比值压死。
  M2 归属准确率：每一行拿它自己场景的质心去跟另外 11 个质心比距离，
     自己的质心最近才算对。质心**不含自己这一行**（留一法），所以不是自己找自己。
     瞎猜是 1/12 = 8.3%。这条只报数，不设门槛。
  M3 六个字段各场景实际填了什么：人读的那张表。判断器是不是真读出了不同，
     这张表比任何比值都直白。
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"
# 默认读 V2 那一份冻结回执。V2b 那一轮跑完要读自己那份——**同一条尺子的口径不能换**，
# 换的只能是输入文件，不然「保持」这两个字就没有可比的对象了。
ROWS = OUT_DIR / "judge_gate_rows.jsonl"
OUT = OUT_DIR / "judge_gate_analysis.json"

PUNCT = " \t\n\r、，。：:；;！？—…\"'（）()【】|*-"


def grams(text: str, n: int = 3) -> set[str]:
    cleaned = "".join(ch for ch in (text or "") if ch not in PUNCT)
    if len(cleaned) < n:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + n] for i in range(len(cleaned) - n + 1)}


def dist(a: str, b: str) -> float:
    ga, gb = grams(a), grams(b)
    union = len(ga | gb)
    return 1.0 - len(ga & gb) / union if union else 1.0


def values_only(row: dict[str, Any]) -> str:
    return "、".join(str(one or "") for one in (row.get("fields") or {}).values())


def attribution(rows: list[dict[str, Any]], getter, assign: dict[int, int] | None = None,
                seed: int = 7) -> dict[str, Any]:
    """归属测试：这一行能不能被认出来是哪一个场景生出来的。

    打分用的是**同类两两相似度的均值**，不是质心集合：用集合并集当质心的话，
    自己那格（留一后只剩 2 行）的并集天然比别格（3 行）小，Jaccard 白捡一个便宜，
    归属率会被虚高。均值没有这个尺寸差。
    ``assign`` 给一份"行号 -> 换一个场景归属"的映射就是打乱对照（负对照）：
    把每行的场景标签随机重新发一遍，格子尺寸一模一样，只有对应关系是假的。
    对照必须掉回瞎猜，否则这条尺子量到的不是场景，是"同一格文本天然更像"这类
    结构性偏好本身。
    """
    bodies = [(index, getter(row)) for index, row in enumerate(rows)]
    bodies = [one for one in bodies if one[1]]
    scene_of = {index: (assign or {one: rows[one]["scenario"] for one in range(len(rows))})[index]
                for index, _body in bodies}
    grams_of = {index: grams(body) for index, body in bodies}
    by_scene: dict[int, list[int]] = defaultdict(list)
    for index in grams_of:
        by_scene[scene_of[index]].append(index)

    correct = 0
    margins: list[float] = []
    for index in grams_of:
        mine = grams_of[index]
        scored = []
        for scene, members in by_scene.items():
            sims = [jaccard(mine, grams_of[other]) for other in members if other != index]
            scored.append((scene, statistics.mean(sims) if sims else 0.0))
        scored.sort(key=lambda one: -one[1])
        own = scene_of[index]
        correct += 1 if scored and scored[0][0] == own else 0
        own_score = next((score for scene, score in scored if scene == own), 0.0)
        best_other = max((score for scene, score in scored if scene != own), default=0.0)
        margins.append(own_score - best_other)
    total = len(grams_of)
    return {"accuracy": f"{correct}/{total}", "rate": round(correct / total, 3) if total else 0.0,
            "chance": round(1.0 / max(1, len(by_scene)), 3),
            "median_margin": round(statistics.median(margins), 4) if margins else 0.0,
            "positive_margin_share": round(
                sum(1 for one in margins if one > 0) / total, 3) if total else 0.0,
            "shuffled": assign is not None, "seed": seed}


def jaccard(ga: set[str], gb: set[str]) -> float:
    union = len(ga | gb)
    return len(ga & gb) / union if union else 0.0


def separability(by_scene: dict[int, list[str]]) -> dict[str, Any]:
    """复算 G2：同场景抽间的最大距离 vs 跨场景的最小距离。"""
    texts = {scene: [one for one in vals if one] for scene, vals in by_scene.items()}
    texts = {k: v for k, v in texts.items() if v}
    within = {scene: max((dist(a, b) for i, a in enumerate(val) for b in val[i + 1:]), default=0.0)
              for scene, val in texts.items()}
    rows = []
    for left in sorted(texts):
        for right in sorted(texts):
            if left >= right:
                continue
            across = min(dist(a, b) for a in texts[left] for b in texts[right])
            rows.append({"pair": f"S{left:02d}-S{right:02d}", "across": round(across, 3),
                         "ceiling": round(max(within[left], within[right]), 3),
                         "separable": across > max(within[left], within[right])})
    acrosses = [one["across"] for one in rows]
    withins = [dist(a, b) for val in texts.values() for i, a in enumerate(val) for b in val[i + 1:]]
    return {"pairs_separable": f"{sum(1 for one in rows if one['separable'])}/{len(rows)}",
            "median_across": round(statistics.median(acrosses), 3) if acrosses else 0.0,
            "median_within": round(statistics.median(withins), 3) if withins else 0.0,
            "mean_within": round(statistics.mean(withins), 3) if withins else 0.0,
            "overlap": round(statistics.median(withins), 3) >= round(statistics.median(acrosses), 3),
            "worst_within": round(max(within.values()), 3) if within else 0.0}


def attribution_control(rows: list[dict[str, Any]], getter, seed: int) -> dict[str, Any]:
    """负对照：把每行的场景归属随机重发一遍（每格还是 3 行），再跑同一个归属测试。
    这一份要是也高，说明这条尺子量的是"同类天然相近"这件事本身，不是场景。"""
    rng = random.Random(seed)
    scenes = [row["scenario"] for row in rows]
    rng.shuffle(scenes)
    return attribution(rows, getter, assign=dict(enumerate(scenes)), seed=seed)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", default=str(ROWS), help="要复算的那一份判断回执")
    ap.add_argument("--out", default=str(OUT), help="分析结果写到哪")
    ap.add_argument("--baseline", default="",
                    help="另一份同口径分析（V2b 传 V2 那一份），用来算「保持」的差值")
    args = ap.parse_args(argv)
    rows_path, out_path = Path(args.rows), Path(args.out)
    if not rows_path.exists():
        print("MISSING_INPUT", rows_path)
        return 2
    rows = [json.loads(one) for one in rows_path.read_text(encoding="utf-8").splitlines()
            if one.strip()]
    by_scene_full: dict[int, list[str]] = defaultdict(list)
    by_scene_vals: dict[int, list[str]] = defaultdict(list)
    for row in rows:
        by_scene_full[row["scenario"]].append(row.get("text") or "")
        by_scene_vals[row["scenario"]].append(values_only(row))

    fields = [one for one in (rows[0].get("fields") or {}) if one]
    table: dict[str, dict[str, list[str]]] = {}
    for field in fields:
        per_scene: dict[str, list[str]] = {}
        for scene in sorted(by_scene_full):
            picked = [str((one.get("fields") or {}).get(field) or "")
                      for one in rows if one["scenario"] == scene]
            per_scene[f"S{scene:02d}"] = picked
        table[field] = per_scene

    true_full = attribution(rows, lambda one: one.get("text") or "")
    ctrl_full = [attribution_control(rows, lambda one: one.get("text") or "", seed)
                 for seed in (1, 2, 3, 4, 5)]
    true_vals = attribution(rows, values_only)
    ctrl_vals = [attribution_control(rows, values_only, seed) for seed in (1, 2, 3, 4, 5)]

    summary = {
        "rows": len(rows),
        "M1_separability_full_block": separability(by_scene_full),
        "M1b_separability_values_only": separability(by_scene_vals),
        "M2_attribution_full_block": true_full,
        "M2_control_shuffled_labels_full_block":
            {"max_rate": max(one["rate"] for one in ctrl_full),
             "rates": [one["rate"] for one in ctrl_full],
             "chance": true_full["chance"]},
        "M2b_attribution_values_only": true_vals,
        "M2b_control_shuffled_labels_values_only":
            {"max_rate": max(one["rate"] for one in ctrl_vals),
             "rates": [one["rate"] for one in ctrl_vals],
             "chance": true_vals["chance"]},
        "M2c_attribution_single_field": {
            field: attribution(rows, lambda one, f=field: str((one.get("fields") or {}).get(f) or ""))
            ["rate"] for field in fields},
        "M2d_control_single_field": {
            field: max(attribution_control(
                rows, lambda one, f=field: str((one.get("fields") or {}).get(f) or ""), seed)["rate"]
                for seed in (1, 2, 3)) for field in fields},
        "M3_field_table": table,
        "distinct_values_per_scene": {
            f"S{scene:02d}": len({values_only(one) for one in rows if one["scenario"] == scene})
            for scene in sorted(by_scene_full)},
    }
    if args.baseline:
        base_path = Path(args.baseline)
        if base_path.exists():
            base = json.loads(base_path.read_text(encoding="utf-8"))
            def rate_of(doc: str, key: str) -> float:
                return float((base.get(key) or {}).get("rate") or 0.0)
            summary["reference"] = {
                "file": base_path.name,
                "rows_in_reference": base.get("rows"),
                "full_block": {"baseline": rate_of(base, "M2_attribution_full_block"),
                               "now": true_full["rate"],
                               "delta": round(true_full["rate"]
                                             - rate_of(base, "M2_attribution_full_block"), 3)},
                "values_only": {"baseline": rate_of(base, "M2b_attribution_values_only"),
                                "now": true_vals["rate"],
                                "delta": round(true_vals["rate"]
                                              - rate_of(base, "M2b_attribution_values_only"), 3)},
                "control_full_max": {"baseline": (
                    (base.get("M2_control_shuffled_labels_full_block") or {}).get("max_rate")),
                    "now": max(one["rate"] for one in ctrl_full)},
                "control_values_max": {"baseline": (
                    (base.get("M2b_control_shuffled_labels_values_only") or {}).get("max_rate")),
                    "now": max(one["rate"] for one in ctrl_vals)},
            }
        else:
            summary["reference"] = {"file": base_path.name, "MISSING": True}
    summary["inputs"] = {"rows": rows_path.name}
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    print("M1 整块   :", json.dumps(summary["M1_separability_full_block"], ensure_ascii=False))
    print("M1b 只留值:", json.dumps(summary["M1b_separability_values_only"], ensure_ascii=False))
    print("M2 整块真标签:", json.dumps(true_full, ensure_ascii=False))
    print("M2 整块打乱对照:", json.dumps(summary["M2_control_shuffled_labels_full_block"],
                                         ensure_ascii=False))
    print("M2b 只留值真标签:", json.dumps(true_vals, ensure_ascii=False))
    print("M2b 只留值打乱对照:", json.dumps(summary["M2b_control_shuffled_labels_values_only"],
                                           ensure_ascii=False))
    print("M2c 单字段归属率:", json.dumps(summary["M2c_attribution_single_field"], ensure_ascii=False))
    print("M2d 单字段打乱对照:", json.dumps(summary["M2d_control_single_field"], ensure_ascii=False))
    print("每场景三种抽的取值互不相同数:",
          json.dumps(summary["distinct_values_per_scene"], ensure_ascii=False))
    print("参考对照:", json.dumps(summary.get("reference"), ensure_ascii=False))
    print("OUT", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
