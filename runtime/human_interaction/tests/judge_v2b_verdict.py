# -*- coding: utf-8 -*-
"""V2b 的纯性能门槛：一份**跑之前就写死**的判据，和一个不花 token 的装配器。

owner 那句原话（2026-09-27 裁决乙，范围限定「只做结构性提速」）：

    判断器成功率 = 100%
    动态判断能力 = 保持
    打乱标签对照 = 仍明显低于真实标签
    额外判断耗时 ≤ 1.2 秒左右
    整轮总延迟增幅 ≤ 20%
    生产主链保护项 = 10/10 PASS

    以及：如果压完以后仍然接近 +50%，停止，不再继续优化这条实现。

这一轮 owner 明令「先做纯性能门槛，不做大规模质量 A/B」，所以这里**不**再判 V2 那套
G1–G4（G2 已经被证明对短文本结构上不可满足，见 _g2_instrument_control.py），
只判上面这六条。六条的口径全部写死在下面，跑完不许回头改：

  S1 判断器成功率 = 100%      36 发判断 0 失败。失败一发都不算 100%。
  S2 动态判断能力 = 保持      整块归属率 ≥ 0.45 且 只留值归属率 ≥ 0.40。
                             V2 实测分别是 0.556 / 0.528（门槛 36 发，同一把尺子
                             analyze_judge_gate.attribution）。压格式压掉的必须是废话，
                             不是场景信息——这两条数字就是用来抓「压过头了」。
  S3 打乱标签对照             五份打乱标签的归属率里最大的那份 ≤ 真标签 − 0.20
                             且 ≤ 0.25（瞎猜是 0.083）。V2 是 0.111 / 0.167。
                             这条掉到真标签附近 = 尺子量的不是场景，S2 跟着作废。
  S4 额外判断耗时 ≤ 1.2 秒    36 发判断的 p95 ≤ 1.20s（p50 与最差一起报）。
                             取 p95 不取均值：实时链上用户碰到的是尾巴，不是平均。
  S5 整轮总延迟增幅 ≤ 20%     中位(判断 + 正式回复) / 中位(旧系统单调) ≤ 1.20。
                             **用中位数不用均值**：8090 单调时长在这个模型上是重尾的
                             （V2 那份 A 臂均值 7.99s、最差 20.46s），均值会被一发热车
                             带跑，中位数不会。
                             用配对同一抽、同一场景的样本，不跨场景比大小。
  S6 生产主链保护项 10/10     读 verify_protection.py 落的那份 flags，十条全 YES。

外加两条**过程量**（不是 owner 的门槛，只用来解释 S5 为什么会过或不过）：
  P1 判断块字数中位（V2 是 90 字，这一轮目标 ≤ 60）——ROLE=diagnostic，不参与判决。
  P2 三段拆分：判断那一发 / 注入那一段 / V1 外壳本身，各占旧系统单调的百分之几。
     V2 那份三臂没带 D 臂（V1 块不带判断），所以「+58% 里有多少跟判断无关」当时
     回答不了。这一轮补上 D 臂，就是为了让 S5 的账能落到具体哪一段上。

停线口径（owner 明令，写死在这里）：
  S5 ≤ 1.20                → PASS，可以进接线裁决
  1.20 < S5 < 1.40         → FAIL，回报 owner，不在本轮继续优化
  S5 ≥ 1.40                → STOP，这条实现到此为止，不再花 token

跑法（零调用，只读别人落好的产物）：

    runtime/core/venv/Scripts/python.exe runtime/human_interaction/tests/judge_v2b_verdict.py
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"

# ---- 门槛（跑之前写死，跑完不许回头改） ----
S1_SUCCESS_RATE_MIN = 1.00
S2_ATTRIBUTION_FULL_MIN = 0.45          # V2 实测 0.556
S2_ATTRIBUTION_VALUES_MIN = 0.40        # V2 实测 0.528
S3_CONTROL_MARGIN_MIN = 0.20            # 真标签 - 对照，至少差这么多
S3_CONTROL_ABS_MAX = 0.25               # 绝对上限（瞎猜 0.083）
S4_JUDGE_P95_MAX_S = 1.20
S5_TOTAL_RATIO_MAX = 1.20
S5_STOP_RATIO = 1.40                    # ≥ 这个数 = 停线，不再优化这条实现
S6_PROTECTION_ALL_YES = 10
P1_BLOCK_CHARS_MAX = 60                 # ROLE=diagnostic，不参与判决
STATIONARITY_TOLERANCE = 0.25           # A 臂前 3 抽 vs 后 3 抽中位差超过 25% = 环境不平稳
ENV_SHIFT_FLAG = 2.0                    # 今天 A 臂中位 / V2 那份 A 臂均值 超过 2 倍 = 登记环境漂移

V2_FROZEN_ATTR_FULL = 0.556
V2_FROZEN_ATTR_VALUES = 0.528
V2_FROZEN_A_AVG_S = 7.99

GATE_ROWS = OUT_DIR / "judge_slim_rows.jsonl"
GATE_SUMMARY = OUT_DIR / "judge_slim_summary.json"
ANALYSIS = OUT_DIR / "judge_slim_analysis.json"
LATENCY_SUMMARY = OUT_DIR / "judge_slim_latency_summary.json"
PROTECTION = OUT_DIR / "protection_check.json"
VERDICT = OUT_DIR / "judge_slim_verdict.json"


def median(values: list[float]) -> float:
    return round(statistics.median(values), 2) if values else 0.0


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return round(ordered[index], 2)


def missing(names: list[str]) -> dict[str, Any]:
    """少哪一份产物就直说少哪一份。缺文件绝不能被读成「这条没过」，更不能被读成过了。"""
    return {"GATE": "NOT_EVALUABLE", "missing_inputs": names}


def main() -> int:
    need = [one for one in (GATE_SUMMARY, ANALYSIS, LATENCY_SUMMARY, PROTECTION) if not one.exists()]
    if need:
        print(json.dumps(missing([one.name for one in need]), ensure_ascii=False, indent=1))
        return 2

    gate = json.loads(GATE_SUMMARY.read_text(encoding="utf-8"))
    ana = json.loads(ANALYSIS.read_text(encoding="utf-8"))
    lat = json.loads(LATENCY_SUMMARY.read_text(encoding="utf-8"))
    prot = json.loads(PROTECTION.read_text(encoding="utf-8"))

    metrics, cost = gate["metrics"], gate["cost"]
    rows = [json.loads(one) for one in GATE_ROWS.read_text(encoding="utf-8").splitlines()
            if one.strip()] if GATE_ROWS.exists() else []

    # ---- S1 判断器成功率 ----
    calls = int(cost.get("calls") or 0)
    errs = int(cost.get("client_errors") or 0)
    s1_rate = round((calls - errs) / calls, 4) if calls else 0.0
    s1 = {"criterion": "S1 判断器成功率 = 100%",
          "measured": f"{calls - errs}/{calls}", "rate": s1_rate,
          "scenes_no_usable_row": metrics.get("scenes_with_no_usable_row", []),
          "PASS": calls > 0 and errs == 0 and s1_rate >= S1_SUCCESS_RATE_MIN}

    # ---- S2 动态判断能力 = 保持 ----
    attr_full = ana.get("M2_attribution_full_block") or {}
    attr_vals = ana.get("M2b_attribution_values_only") or {}
    s2 = {"criterion": f"S2 归属率 ≥ {S2_ATTRIBUTION_FULL_MIN}（整块）/ "
                       f"≥ {S2_ATTRIBUTION_VALUES_MIN}（只留值）",
          "full_block": attr_full.get("accuracy"), "full_rate": attr_full.get("rate"),
          "values_only": attr_vals.get("accuracy"), "values_rate": attr_vals.get("rate"),
          "v2_frozen": {"full": V2_FROZEN_ATTR_FULL, "values": V2_FROZEN_ATTR_VALUES},
          "distinct_first_draw": f"{metrics.get('distinct_first_draw')}/"
                                 f"{metrics.get('expected_scenarios', 12)}",
          "PASS": (float(attr_full.get("rate") or 0) >= S2_ATTRIBUTION_FULL_MIN
                   and float(attr_vals.get("rate") or 0) >= S2_ATTRIBUTION_VALUES_MIN)}

    # ---- S3 打乱标签对照 ----
    ctrl_full = (ana.get("M2_control_shuffled_labels_full_block") or {}).get("max_rate")
    ctrl_vals = (ana.get("M2b_control_shuffled_labels_values_only") or {}).get("max_rate")
    s3 = {"criterion": f"S3 打乱标签 ≤ 真标签-{S3_CONTROL_MARGIN_MIN} 且 ≤ {S3_CONTROL_ABS_MAX}",
          "control_max_full": ctrl_full, "control_max_values": ctrl_vals,
          "true_full": attr_full.get("rate"), "true_values": attr_vals.get("rate"),
          "chance": attr_full.get("chance"),
          "PASS": (ctrl_full is not None and ctrl_vals is not None
                   and float(ctrl_full) <= float(attr_full.get("rate") or 0) - S3_CONTROL_MARGIN_MIN
                   and float(ctrl_vals) <= float(attr_vals.get("rate") or 0) - S3_CONTROL_MARGIN_MIN
                   and max(float(ctrl_full), float(ctrl_vals)) <= S3_CONTROL_ABS_MAX)}

    # ---- S4 额外判断耗时 ----
    judge_walls = [float(one.get("wall_s") or 0.0) for one in rows] or \
                  [float(cost.get("p50_s") or 0.0)]
    s4 = {"criterion": f"S4 判断那一发 p95 ≤ {S4_JUDGE_P95_MAX_S}s",
          "p50_s": cost.get("p50_s"), "p95_s": cost.get("p95_s"), "max_s": cost.get("max_s"),
          "n": calls,
          "PASS": float(cost.get("p95_s") or 99) <= S4_JUDGE_P95_MAX_S}

    # ---- S5 整轮总延迟增幅 ----
    per_arm = lat.get("per_arm") or {}
    arm_rows = lat.get("rows") or []
    a_rows = [one for one in arm_rows if one["arm"].startswith("A_")]
    b_rows = [one for one in arm_rows if one["arm"].startswith("B_")]
    a_med = median([float(one.get("wall_s") or 0.0) for one in a_rows])
    b_total_med = median([float(one.get("wall_s") or 0.0) + float(one.get("judge_s") or 0.0)
                          for one in b_rows])
    ratio = round(b_total_med / a_med, 3) if a_med else 0.0
    # 另外两个口径**只报不改判**：门槛按上面那条中位数（跑之前写死的），这两条摆出来是
    # 因为「+58%」那句话当年是**均值口径、每臂 6 抽**量的。换口径不换说法就是自欺，
    # 所以这里把同一批 V2b 行的均值口径也算出来，让两句话能对上账。
    paired = {}
    by_key = {(one["scenario"], one["draw"]): one for one in a_rows}
    for one in b_rows:
        base = by_key.get((one["scenario"], one["draw"]))
        if base and float(base.get("wall_s") or 0) > 0:
            paired[f"S{one['scenario']:02d}d{one['draw']}"] = round(
                (float(one.get("wall_s") or 0) + float(one.get("judge_s") or 0))
                / float(base["wall_s"]), 3)
    a_avg = round(statistics.mean([float(one.get("wall_s") or 0.0) for one in a_rows]), 2) if a_rows else 0.0
    b_avg_total = round(statistics.mean(
        [float(one.get("wall_s") or 0.0) + float(one.get("judge_s") or 0.0)
         for one in b_rows]), 2) if b_rows else 0.0
    early = median([float(one.get("wall_s") or 0.0) for one in a_rows[:3]])
    late = median([float(one.get("wall_s") or 0.0) for one in a_rows[-3:]])
    drift = round(abs(late - early) / early, 3) if early else 0.0
    stationary = drift <= STATIONARITY_TOLERANCE
    s5 = {"criterion": f"S5 中位(判断+正式回复)/中位(旧系统单调) ≤ {S5_TOTAL_RATIO_MAX}",
          "A_median_s": a_med, "A_n": len(a_rows),
          "B_total_median_s": b_total_med, "B_n": len(b_rows),
          "ratio": ratio, "delta_pct": round((ratio - 1) * 100, 1),
          "v2_frozen_ratio": 1.58,
          "reported_not_gating": {
              "paired_ratio_median": median([float(v) for v in paired.values()]),
              "paired_per_sample": paired,
              "mean_basis_ratio": round(b_avg_total / a_avg, 3) if a_avg else 0.0,
              "mean_basis_note": "V2 那句「+58%」就是这个口径（每臂 6 抽的均值）。"
                                 "中位数抗单发长答案，均值不抗——两个都摆出来。"},
          "stationarity": {"first3_median_s": early, "last3_median_s": late,
                           "drift_pct": round(drift * 100, 1),
                           "tolerance_pct": round(STATIONARITY_TOLERANCE * 100),
                           "STATIONARY": stationary},
          "PASS": stationary and ratio <= S5_TOTAL_RATIO_MAX,
          "STOP": ratio >= S5_STOP_RATIO}

    # ---- S6 生产主链保护项 ----
    flags = prot.get("flags") or {}
    yes = [k for k, v in flags.items() if str(v).strip().upper() == "YES"]
    s6 = {"criterion": f"S6 保护项 {S6_PROTECTION_ALL_YES}/{S6_PROTECTION_ALL_YES} YES",
          "checked_at": (prot.get("raw") or {}).get("checked_at"),
          "measured": f"{len(yes)}/{len(flags)}", "no": sorted(set(flags) - set(yes)),
          "PASS": len(flags) == S6_PROTECTION_ALL_YES and len(yes) == S6_PROTECTION_ALL_YES}

    # ---- P1 / P2 过程量 ----
    blocks = sorted(len(one.get("text") or "") for one in rows if one.get("text"))
    d_rows = [one for one in arm_rows if one["arm"].startswith("D_")]
    c_rows = [one for one in arm_rows if one["arm"].startswith("C_")]
    d_med = median([float(one.get("wall_s") or 0.0) for one in d_rows])
    c_med = median([float(one.get("wall_s") or 0.0) for one in c_rows])
    judge_med = median([float(one.get("judge_s") or 0.0) for one in b_rows])
    p1 = {"median_block_chars": median([float(one) for one in blocks]),
          "v2_frozen_avg_block_chars": cost.get("judgment_chars_avg"),
          "max_block_chars": blocks[-1] if blocks else 0,
          "ROLE": "diagnostic（不参与判决）", "target_max": P1_BLOCK_CHARS_MAX}
    p2 = {
        "A_旧系统单调_median_s": a_med,
        "D_V1块无判断_median_s": d_med,
        "B_V2b判断+正式_median_reply_s": median([float(one.get("wall_s") or 0.0) for one in b_rows]),
        "B_V2b_total_median_s": b_total_med,
        "C_V2b错场景_total_median_s": round(c_med + judge_med, 2),
        "拆解_pct_of_A": {
            "V1外壳本身": round((d_med - a_med) / a_med * 100, 1) if a_med else 0.0,
            "注入这一段(同形状换内容另算)": round((median(
                [float(one.get("wall_s") or 0.0) for one in b_rows]) - d_med)
                / a_med * 100, 1) if a_med and b_rows else 0.0,
            "判断那一发": round(judge_med / a_med * 100, 1) if a_med else 0.0},
        "C臂含义": "C 与 B 同样长同样式，只有判断内容来自别的场景；两者接近 → 慢的是「多挂一段」",
        # 分场景拆一遍：如果增幅只出现在「本来就该长答」的那一格（S11 需要较长解释），
        # 那它记在"这一轮答得更多"上，不该记在"判断器拖累"上。合在一起看会被平均掉。
        "per_scene_median_s": {
            f"S{scene:02d}": {
                arm: median([float(one.get("wall_s") or 0.0)
                             for one in arm_rows
                             if one["scenario"] == scene and one["arm"].startswith(arm_key)])
                for arm, arm_key in (("A", "A"), ("B_reply", "B"), ("C_reply", "C"), ("D", "D"))}
            for scene in sorted({one["scenario"] for one in arm_rows})},
    }

    six = {"S1": s1["PASS"], "S2": s2["PASS"], "S3": s3["PASS"],
           "S4": s4["PASS"], "S5": s5["PASS"], "S6": s6["PASS"]}
    passed = sum(1 for one in six.values() if one)
    if not s5["stationarity"]["STATIONARY"]:
        gate_verdict = "NOT_EVALUABLE"       # 环境不平稳 = 没量准，不是没过
    elif s5["STOP"]:
        gate_verdict = "STOP"                # owner：压完还接近 +50% 就停，不再优化
    elif all(six.values()):
        gate_verdict = "PASS"
    else:
        gate_verdict = "FAIL"
    env_shift = round(a_med / V2_FROZEN_A_AVG_S, 2) if a_med else 0.0

    summary = {
        "GATE": gate_verdict, "six": six, "passed": f"{passed}/6",
        "criteria": {"S1": s1, "S2": s2, "S3": s3, "S4": s4, "S5": s5, "S6": s6},
        "process": {"P1_judgment_block": p1, "P2_latency_breakdown": p2,
                    "env_shift_A_vs_v2": {"today_median_s": a_med,
                                          "v2_frozen_avg_s": V2_FROZEN_A_AVG_S,
                                          "ratio": env_shift,
                                          "FLAG": "ENV_SHIFT" if env_shift > ENV_SHIFT_FLAG
                                                  else "COMPARABLE",
                                          "note": "S5 是同一窗口内的比值，绝对值漂移不改判决，"
                                                  "但必须报出来，因为它说明今天的 8090 不是昨天那个"}},
        "thresholds_frozen_at": "2026-09-27（跑之前写死在本文件头部）",
        "inputs": {"gate_summary": GATE_SUMMARY.name, "analysis": ANALYSIS.name,
                   "latency": LATENCY_SUMMARY.name, "protection": PROTECTION.name,
                   "protection_checked_at": (prot.get("raw") or {}).get("checked_at")},
    }
    VERDICT.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"GATE": gate_verdict, "passed": summary["passed"], "six": six},
                     ensure_ascii=False))
    for key in ("S1", "S2", "S3", "S4", "S5", "S6"):
        print(json.dumps(summary["criteria"][key], ensure_ascii=False))
    print(json.dumps(summary["process"], ensure_ascii=False, indent=1))
    print("VERDICT", VERDICT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
