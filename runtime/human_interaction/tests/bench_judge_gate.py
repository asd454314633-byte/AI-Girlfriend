# -*- coding: utf-8 -*-
"""V2 的硬门槛：「本轮互动判断」到底有没有随场景变。

跑法（会打 8090，只发判断请求；正式回复的 A/B 不在这一份里）：

    runtime/core/venv/Scripts/python.exe runtime/human_interaction/tests/bench_judge_gate.py

判据在**跑之前**写死在这里，跑完不许回头改门槛：

  G1 DISTINCT_SCENARIOS >= 10/12
      12 个场景各取第 1 抽，判断文本互不相同的组数。V1 的失败形状就是这一项：
      「真人自然」36 格里只有 2 种不同文本。
  G2 SEPARABLE_PAIR_RATIO >= 0.90
      66 个场景对里，跨场景的最小距离 > 两边各自同场景抽间最大距离 的比例。
      这条才是「真的随场景变」而不是「同一句话随机抖动」：同场景重抽之间的差
      是噪声底，跨场景的差必须压过它。距离 = 1 - 字符三元组 Jaccard（本文件另写一遍，
      不复用被测模块的度量）。
  G3 CLIENT_ERRORS <= 1
      36 次里失败超过一次 = 这一轮量不出来，直接判 NOT_EVALUABLE（不是判不过，是没测到）。
  G4 ANCHOR_HITS >= 4/6
      六条按 §16 场景要求写的功能抽查。只查「有没有把这句话读错」，
      不查「有没有按某个姿态回」——预设姿态是任务 §6 明令禁止的。
  G5 成本照实报：判断请求的 p50/p95/最差、提示词字数、补全 token 数，
      以及「判断 + 正式回复」两调合计相对 V1 那条 7.23s 基线的增幅。
      这一项不设门槛，只决定值不值得接线。

任何一条 G1–G4 不达标 → GATE=STOP：不接线、不追加规则、不再花 token（任务 §17/§20）。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import unittest.mock as mock
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.human_interaction.tests import bench_scenarios, bench_shadow  # noqa: E402

OUT_DIR = HERE / "out"
DEFAULT_TAG = "judge_gate"          # 这一份名字是 V2 那次判决的证据，不许被复用覆盖
# ---- 产物名字按 tag 分（V2b 用 --tag judge_slim）----
# 为什么必须分：门槛跑完的判决是**绑在那一份行文件上的**。V2b 再跑一遍如果写回同名，
# 报告里「55.6% 归属率」那一条的回执就没了，等于自己把自己被复核的依据删掉。
def paths_for(tag: str) -> tuple[Path, Path]:
    return OUT_DIR / f"{tag}_rows.jsonl", OUT_DIR / f"{tag}_summary.json"

# ---- 门槛（跑之前写死） ----
G1_DISTINCT_MIN = 10
G2_SEPARABLE_MIN = 0.90
G3_CLIENT_ERRORS_MAX = 1
G4_ANCHORS_MIN = 4

# 功能抽查：键 -> (在哪个字段里找, 允许的同词集合)。none = 六个字段里任一个命中即可。
# V2b 把键名从「用户意图/人物态度/互动方式/回复节奏」压成了「意图/态度/接法/节奏」，
# 这里跟着改键名**只是让探针重新指向存在的字段**——不改探针要判的事，也不放宽词表。
# 注意最后那条「复读被抓出来」：压成一行之后六个字段里没有一个格子是留给「发现重复」
# 这种观察的（2～4 个字的值写不下），所以它在 V2b 里预期会掉。这一项**不在 owner 那六条
# 门槛里**，本轮只照实报，不因为它难看了去动格式，也不偷偷删探针。
ANCHORS: list[tuple[str, str, tuple[str, ...]]] = [
    ("S6_玩笑没被读成冲突", "意图", ("玩笑", "调侃", "逗", "打趣", "贫")),
    ("S7_倾诉没被读成吵架", "接法", ("安慰", "接住", "陪", "听", "顺", "心疼", "软")),
    ("S10_极短问句判成短话", "节奏", ("短", "一句", "快")),
    ("S11_要解释的判成长话", "节奏", ("长", "细", "展开", "多说", "分步")),
    ("S12_复读被抓出来", "", ("重复", "换句", "一样", "来回", "同句", "腻")),
    ("S8_争执没被读成甜腻", "态度", ("不高兴", "有气", "堵", "硬", "火", "冷", "不满")),
]


def judge_on(mode: str = "human_natural"):
    """只在内存里把两道闸打开：磁盘上那份配置始终是 feature_enabled=false。"""
    from runtime.human_interaction import config
    settings = dict(config._SAFE_DEFAULTS)
    settings.update(bench_shadow.settings_for(mode))
    settings["turn_judge_enabled"] = True
    return mock.patch.object(config, "_snapshot", return_value=settings)


def grams(text: str, n: int = 3) -> set[str]:
    cleaned = "".join(ch for ch in (text or "") if ch not in " \t\n\r、，。：:；;！？—…\"'（）()")
    return {cleaned[i:i + n] for i in range(0, max(0, len(cleaned) - n + 1))} if len(cleaned) >= n \
        else ({cleaned} if cleaned else set())


def distance(a: str, b: str) -> float:
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 1.0
    union = len(ga | gb)
    return 1.0 - len(ga & gb) / union if union else 1.0


def measure(rows: list[dict[str, Any]], expected: Any = None) -> dict[str, Any]:
    wanted = sorted(int(one) for one in (expected or list(range(1, 13))))
    expected_scenes = len(wanted)
    by_scene: dict[int, list[str]] = {}
    for row in rows:
        by_scene.setdefault(row["scenario"], []).append(row["text"])
    ok_by_scene = {k: [one for one in v if one] for k, v in by_scene.items()}
    # 一个场景三抽全挂 = 这一格没测到，不是这一格没过。空集喂进 min() 会当场崩，
    # 更糟的是崩之前会被当成「距离很大/很小」混进比值。所以先点名，再判 NOT_EVALUABLE。
    empty_scenes = sorted(k for k, val in ok_by_scene.items() if not val)
    missing_scenes = sorted(set(wanted) - set(by_scene))
    ok_by_scene = {k: val for k, val in ok_by_scene.items() if val}

    within: dict[int, float] = {}
    for scene, texts in ok_by_scene.items():
        pairs = [distance(a, b) for i, a in enumerate(texts) for b in texts[i + 1:]]
        within[scene] = max(pairs) if pairs else 0.0

    scenes = sorted(ok_by_scene)
    pair_results: list[dict[str, Any]] = []
    for i, left in enumerate(scenes):
        for right in scenes[i + 1:]:
            across = min(distance(a, b) for a in ok_by_scene[left] for b in ok_by_scene[right])
            ceiling = max(within[left], within[right])
            pair_results.append({"pair": f"S{left:02d}-S{right:02d}", "across": round(across, 4),
                                 "within_ceiling": round(ceiling, 4),
                                 "separable": across > ceiling})
    separable = sum(1 for one in pair_results if one["separable"])
    total_pairs = len(pair_results) or 1

    first_draw = [row for row in rows if row["draw"] == 1 and row["text"]]
    distinct = len({row["text"] for row in first_draw})
    distinct_all = len({row["text"] for row in rows if row["text"]})

    anchors = []
    for name, field, words in ANCHORS:
        scene_no = int(name.split("_")[0][1:])
        row = next((one for one in rows if one["scenario"] == scene_no and one["draw"] == 1), {})
        if field:
            hay = str((row.get("fields") or {}).get(field) or "")
        else:
            hay = "、".join(str(one or "") for one in (row.get("fields") or {}).values())
        anchors.append({"anchor": name, "field": field or "任意字段",
                        "value": hay, "hit": any(word in hay for word in words),
                        "measured": bool(row)})
    return {
        "distinct_first_draw": distinct, "distinct_all_rows": distinct_all,
        "expected_scenes": expected_scenes, "scenes_covered": len(by_scene),
        "scenes_with_no_usable_row": [f"S{k:02d}" for k in empty_scenes],
        "scenes_not_run": [f"S{k:02d}" for k in missing_scenes],
        "evaluable": not empty_scenes and not missing_scenes and len(ok_by_scene) >= 2,
        "separable_pairs": separable, "total_pairs": total_pairs,
        "separable_ratio": round(separable / total_pairs, 3),
        "within_noise_floor": {f"S{key:02d}": round(val, 3) for key, val in sorted(within.items())},
        "worst_within": round(max(within.values()) if within else 0.0, 3),
        "median_across": round(statistics.median([one["across"] for one in pair_results]), 3)
        if pair_results else 0.0,
        "pairs": pair_results, "anchors": anchors,
        "anchor_hits": sum(1 for one in anchors if one["hit"]),
        "anchor_unmeasured": sum(1 for one in anchors if not one["measured"]),
    }


def cost(rows: list[dict[str, Any]]) -> dict[str, Any]:
    walls = [float(row.get("wall_s") or 0.0) for row in rows]
    usages = [(row.get("usage") or {}) for row in rows]
    return {
        "calls": len(rows),
        "client_errors": sum(1 for row in rows if not row.get("ok")),
        "p50_s": round(statistics.median(walls), 2) if walls else 0.0,
        "p95_s": round(sorted(walls)[max(0, int(len(walls) * 0.95) - 1)], 2) if walls else 0.0,
        "max_s": round(max(walls), 2) if walls else 0.0,
        "prompt_chars_avg": round(statistics.mean([row.get("prompt_chars") or 0 for row in rows]), 0)
        if rows else 0,
        "completion_tokens_avg": round(statistics.mean([float(one.get("completion_tokens") or 0)
                                                       for one in usages]), 1) if usages else 0,
        "judgment_chars_avg": round(statistics.mean([len(row.get("text") or "") for row in rows]), 0)
        if rows else 0,
    }


def two_call_cost(scenario_no: int = 11, draws: int = 2) -> dict[str, Any]:
    """判断 + 正式回复 两调合计，跟只跑正式回复一比。V1 报告里旧基线单调 7.23s。

    V2 那份系统块的拼法**照将来钩子那一条路**：拿装配好的整块，把判断那段接在尾巴上。
    钩子拿到的是 session 的 instructions 整块，它不会重跑装配，所以拼接位置在这里
    就得按那一条来，不能在测试里另摆一个更好看的位置。
    """
    from runtime.human_interaction import turn_judge
    from runtime.human_interaction.tests import bench_run

    scene = bench_scenarios.find(bench_scenarios.SCENARIOS[scenario_no - 1]["key"])
    persona, relationship = bench_shadow.SHADOW_PERSONA, bench_shadow.SHADOW_PROFILE["relationship"]
    single, paired = [], []
    for _draw in range(draws):
        base_text, _meta, _added0 = bench_shadow.assemble_as(None, history=scene["history"],
                                                             user_text=scene["user_text"])
        one = bench_run.call(base_text, scene["history"], scene["user_text"])
        single.append(float(one.get("wall_s") or 0.0))
        with judge_on("human_natural"):
            judged = turn_judge.judge(persona_text=persona, relationship_text=relationship,
                                      history=scene["history"], user_text=scene["user_text"])
        arm_text, _meta2, _added = bench_shadow.assemble_as(
            "human_natural", history=scene["history"], user_text=scene["user_text"])
        v2_text = (arm_text + "\n\n" + judged["text"]).strip() if judged["text"] else arm_text
        two = bench_run.call(v2_text, scene["history"], scene["user_text"])
        paired.append({"judge_s": round(float(judged.get("wall_s") or 0.0), 2),
                       "reply_s": float(two.get("wall_s") or 0.0),
                       "total_s": round(float(judged.get("wall_s") or 0.0)
                                        + float(two.get("wall_s") or 0.0), 2),
                       "context_chars": len(v2_text), "judgment_chars": len(judged["text"]),
                       "judged": judged["ok"]})
    return {"scenario": scenario_no, "baseline_single_call_s": single,
            "v2_two_calls": paired,
            "baseline_avg_s": round(statistics.mean(single), 2) if single else 0.0,
            "v2_avg_total_s": round(statistics.mean([one["total_s"] for one in paired]), 2)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=3)
    ap.add_argument("--scenarios", default="", help="逗号分隔的场景编号，默认全 12 个")
    ap.add_argument("--skip-cost", action="store_true")
    ap.add_argument("--tag", default=DEFAULT_TAG,
                    help="产物名字前缀。V2b 那一轮用 judge_slim，别写回 V2 那一份")
    ap.add_argument("--allow-overwrite", action="store_true",
                    help="只有明确要重跑同一份时才给；默认拒绝覆盖已有回执")
    args = ap.parse_args(argv)

    wanted = {int(one) for one in args.scenarios.replace(" ", ",").split(",")
              if one.strip().isdigit()}
    ROWS_PATH, SUMMARY_PATH = paths_for(args.tag)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for one in (ROWS_PATH, SUMMARY_PATH):
        if one.exists() and not args.allow_overwrite:
            # 已存在的回执 = 某一轮判决的依据。删它等于把自己被复核的依据删掉。
            print("REFUSING_TO_OVERWRITE", one,
                  "-- 换 --tag，或确认要重跑同一份时加 --allow-overwrite")
            return 3
    if ROWS_PATH.exists():
        ROWS_PATH.unlink()
    if SUMMARY_PATH.exists():
        SUMMARY_PATH.unlink()

    from runtime.human_interaction import turn_judge
    print(json.dumps(bench_shadow.build_shadow(rebuild=True), ensure_ascii=False))
    persona = bench_shadow.SHADOW_PERSONA
    relationship = bench_shadow.SHADOW_PROFILE["relationship"]

    rows: list[dict[str, Any]] = []
    total = len([s for s in bench_scenarios.SCENARIOS if not wanted or s["no"] in wanted]) * args.draws
    with ROWS_PATH.open("a", encoding="utf-8") as sink:
        for scene in bench_scenarios.SCENARIOS:
            if wanted and scene["no"] not in wanted:
                continue
            for draw in range(1, args.draws + 1):
                started = time.time()
                with judge_on("human_natural"):
                    out = turn_judge.judge(persona_text=persona, relationship_text=relationship,
                                           history=scene["history"], user_text=scene["user_text"])
                row = {"scenario": scene["no"], "key": scene["key"],
                       "requirement": scene["requirement"], "draw": draw,
                       "user_text": scene["user_text"], "ok": out["ok"],
                       "reason": out["reason"], "text": out["text"],
                       "fields": out["fields"], "keys": out["keys"], "notes": out["notes"],
                       "prompt_chars": out["prompt_chars"], "usage": out["usage"],
                       "timings": out.get("timings") or {},
                       "reasoning_chars": out.get("reasoning_chars"),
                       "finish": out.get("finish"), "sent": out.get("sent") or [],
                       "wall_s": out.get("wall_s") or round(time.time() - started, 2)}
                rows.append(row)
                sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                sink.flush()
                print(f"[{len(rows)}/{total}] S{scene['no']:02d} d{draw} "
                      f"{row['wall_s']}s {'OK' if row['ok'] else 'FAIL ' + row['reason']}")
                if row["ok"]:
                    print("    " + row["text"].replace("\n", " | "))

    expected = sorted(wanted) if wanted else list(range(1, 13))
    m = measure(rows, expected=expected)
    c = cost(rows)
    if not args.skip_cost:
        c["two_call"] = two_call_cost()
    full_run = m["evaluable"] and len(expected) == 12
    verdict = {
        "G1_distinct_scenarios": f"{m['distinct_first_draw']}/{len(expected)} "
                                 f"(need >={G1_DISTINCT_MIN}，门槛按 12 格算)",
        "G1_PASS": m["distinct_first_draw"] >= G1_DISTINCT_MIN,
        "G2_separable_pairs": f"{m['separable_pairs']}/{m['total_pairs']} "
                              f"ratio={m['separable_ratio']} (need >={G2_SEPARABLE_MIN})",
        "G2_PASS": m["separable_ratio"] >= G2_SEPARABLE_MIN,
        "G3_client_errors": f"{c['client_errors']}/{c['calls']} (max {G3_CLIENT_ERRORS_MAX})",
        "G3_EVALUABLE": c["client_errors"] <= G3_CLIENT_ERRORS_MAX and full_run,
        "G4_anchor_hits": f"{m['anchor_hits']}/{len(ANCHORS)} (need >={G4_ANCHORS_MIN})",
        "G4_PASS": m["anchor_hits"] >= G4_ANCHORS_MIN,
        "partial_run": len(expected) != 12,
    }
    # NOT_EVALUABLE = 这一轮没量出来（连接/超时/有一格三抽全挂）。它既不是 PASS 也不是
    # STOP，不许被读成「门槛没过所以停」，也不许被读成「过了」。
    gate = ("NOT_EVALUABLE" if not verdict["G3_EVALUABLE"]
            else "PASS" if all([verdict["G1_PASS"], verdict["G2_PASS"], verdict["G4_PASS"]])
            else "STOP")
    summary = {"verdict": verdict, "GATE": gate, "metrics": m, "cost": c,
               "rows": len(rows), "draws": args.draws,
               "scenes_covered": m["scenes_covered"],
               "not_measured": {"scenes_not_run": m["scenes_not_run"],
                                "scenes_all_failed": m["scenes_with_no_usable_row"]},
               "config_note": "磁盘配置始终 feature_enabled=false；档位只在内存里改",
               "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"verdict": verdict, "GATE": gate, "cost": c}, ensure_ascii=False, indent=1))
    print("SUMMARY", SUMMARY_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
