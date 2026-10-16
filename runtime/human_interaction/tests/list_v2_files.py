# -*- coding: utf-8 -*-
"""交付清单生成器：某一轮到底加了哪些文件、改了哪些、层外一个都没动。

口径与 V1 那份 `file_manifest.json` 分开，是为了**不改写 V1 已归档的证据**——
V1 报告里引用的那份清单一旦被重跑，哈希就跟着 V2 一起变了，V1 的结论再也对不回盘上。
所以本文件只做一件事：**从指定那一轮的开工基线起算 diff**（默认 V2）。

V2b 那一轮（只压判断块形状与注入成本）加不进 V2 那份口径：它的 diff 起点是
`runtime/human_interaction_v2b_baseline.json`，交付件与产物也不是同一批。
所以这里改成 `--tag` 选档案（v2 / v2b），**默认值与不带参数时的行为一字未动**，
V2 那份清单仍可原样重跑。每份清单写到自己名字里，谁也不覆盖谁。

判据都是盘上现算，零模型调用、零网络。四类：
  * 本层新增（冻结时不在、现在在）
  * 本层修改（两边都在但哈希不同）
  * 本层删除（冻结时在、现在没了）
  * 层外 watched（V1 那 19 个生产文件，逐个对**冻结时记下的 now**，不是对 V1 改前）
最后一条是保护闸门：本轮只许动本层，层外若有一个变了就打印 LAYER_OUTSIDE_DRIFT 并非零退出。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
LAYER = ROOT / "runtime" / "human_interaction"

# 点名要的交付件与产物，逐个列；不在名单里的不算这一轮的交付。
V2_DELIVERABLES = ("HUMAN_INTERACTION_V2_GATE_REPORT.md",
                   "runtime/human_interaction_v2_baseline.json")
V2_ARTIFACTS = ("tests/out/judge_gate_rows.jsonl", "tests/out/judge_gate_summary.json",
                "tests/out/judge_gate_analysis.json", "tests/out/judge_gate_run.log",
                "tests/out/g2_instrument_control.json", "tests/out/judge_language_check.json",
                "tests/out/judge_latency_rows.jsonl", "tests/out/judge_latency_summary.json",
                "tests/out/judge_thinking_probe.json",
                "tests/shadow/characters.sqlite3", "tests/shadow/memories.sqlite3")

# V2b：报告 + 冻结件是交付。判断块那一份提示词、`turn_judge.py` 的解析/渲染、
# `engine.py` 的剔除粒度、`config.py` + `human_interaction.json` 那两条封顶字节数，
# 一起构成"形状"这一侧的全部改动面（判断规则那侧 `guard.py` 一字未动，见 §口径）。
# 点名列进来是为了磁盘现算哈希，不靠"我记得我只改了它"。
V2B_DELIVERABLES = ("HUMAN_INTERACTION_V2B_GATE_REPORT.md",
                    "runtime/human_interaction_v2b_baseline.json",
                    "runtime/human_interaction/prompts/turn_judge_human_natural.txt",
                    "runtime/human_interaction/turn_judge.py",
                    "runtime/human_interaction/engine.py",
                    "runtime/human_interaction/config.py",
                    "runtime/human_interaction/human_interaction.json",
                    "runtime/human_interaction/tests/judge_v2b_verdict.py",
                    # 本轮往 V2 报告末尾追加了一节指针 ⇒ 那份报告的哈希必然变了。
                    # 把它点名列进来，是为了让"追加之后的哈希"有一处承接，不靠口头保证。
                    "HUMAN_INTERACTION_V2_GATE_REPORT.md")
V2B_ARTIFACTS = ("tests/out/judge_slim_rows.jsonl", "tests/out/judge_slim_summary.json",
                 "tests/out/judge_slim_analysis.json", "tests/out/judge_slim_run.log",
                 "tests/out/judge_slim_latency_rows.jsonl", "tests/out/judge_slim_latency_summary.json",
                 "tests/out/judge_slim_latency.log", "tests/out/judge_slim_verdict.json",
                 "tests/out/protection_check.json", "tests/out/off_path_check.json",
                 "tests/out/judge_language_check.json")

# LT 轮（正式回复低思考 A/B）：本轮**一个字层代码都没改**，改的只有 tests/ 下的
# 新生成器与这份报告。所以这一档要证的是两件别的事：
#   ① 回复路径那 22 个文件（含 s2s 包内 5 个）与冻结时逐字节相同 —— 没接线、没改参数；
#   ② 层外 19 个 V1 watched 仍然零漂移 —— 本轮同样没碰生产主链。
# 冻结件用的键名是 `reply_path_watched_now`（不是 v1_watched_now），
# 所以 watched 键做成档案字段，不在循环里硬编码。
LT_DELIVERABLES = ("LOW_THINKING_REPLY_AB_REPORT.md",
                   "runtime/human_interaction_low_thinking_baseline.json",
                   "runtime/human_interaction/tests/freeze_lt_baseline.py",
                   "runtime/human_interaction/tests/probe_lt_knobs.py",
                   "runtime/human_interaction/tests/bench_lt_reply.py",
                   "runtime/human_interaction/tests/judge_lt_verdict.py",
                   # 本清单生成器自己也是本轮改过的层文件（加 lt 档案 + watched 键改成档案字段）：
                   # 点名它，是为了让"改完之后的哈希"有一处承接，而不是只报"它出现在 modified 里"。
                   "runtime/human_interaction/tests/list_v2_files.py")
LT_ARTIFACTS = ("tests/out/lt_knob_probe.json", "tests/out/lt_b11065_help_raw.txt",
                "tests/out/lt1_ab_rows.jsonl", "tests/out/lt1_ab_summary.json",
                "tests/out/lt1_ab.log", "tests/out/lt_verdict.json",
                "tests/out/lt_ab_table.md",
                # 臂序反转对照（§5.1）与本轮单测回执：都是本轮的判决依据，必须在名单里，
                # 否则"清单干净"只覆盖了我挑出来那几个文件。
                "tests/out/ltrev_ab_rows.jsonl", "tests/out/ltrev_ab_summary.json",
                "tests/out/ltrev.log", "tests/out/_lt_unittest.log",
                # 本轮（13:26）还**重跑**了上一轮的两个自查脚本，它们各自写产物、
                # 并改写了自己名下的 shadow 测试库。不列进名单，"清单干净"就只覆盖
                # 我挑出来那几个文件——本轮的实际写入面以此处为准。
                "tests/out/protection_check.json", "tests/out/off_path_check.json",
                "tests/shadow/characters.sqlite3", "tests/shadow/memories.sqlite3")

PROFILES = {
    "v2": {"baseline": ROOT / "runtime" / "human_interaction_v2_baseline.json",
           "out": HERE / "out" / "v2_file_manifest.json",
           "task": "HUMAN_INTERACTION_LAYER_01/V2_MANIFEST",
           "deliverables": V2_DELIVERABLES, "artifacts": V2_ARTIFACTS},
    "v2b": {"baseline": ROOT / "runtime" / "human_interaction_v2b_baseline.json",
            "out": HERE / "out" / "v2b_file_manifest.json",
            "task": "HUMAN_INTERACTION_LAYER_01/V2B_MANIFEST",
            "deliverables": V2B_DELIVERABLES, "artifacts": V2B_ARTIFACTS},
    "lt": {"baseline": ROOT / "runtime" / "human_interaction_low_thinking_baseline.json",
           "out": HERE / "out" / "lt_file_manifest.json",
           "task": "HUMAN_INTERACTION_LAYER_01/LT_MANIFEST",
           "deliverables": LT_DELIVERABLES, "artifacts": LT_ARTIFACTS,
           "watched_key": "reply_path_watched_now",
           # 本轮冻结件只记了回复路径 22 个文件；owner 的"不接生产"要靠层外 19 个
           # V1 watched 也零漂移才能收口，所以**再接一份上一轮的冻结清单**当第二把尺。
           "extra_watched": [{"baseline": ROOT / "runtime" / "human_interaction_v2b_baseline.json",
                              "key": "v1_watched_now", "label": "v1_watched@v2b"}]},
}
# 上一轮的清单名字要**现算存在与否**再报，不写死成"它肯定没被动过"。
PREDECESSOR = {"v2": HERE / "out" / "file_manifest.json",
               "v2b": HERE / "out" / "v2_file_manifest.json",
               "lt": HERE / "out" / "v2b_file_manifest.json"}
SOURCE_EXT = {".py", ".txt", ".json"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def is_artifact(name: str) -> bool:
    return any(token in name for token in ("tests/out", "tests/shadow", "__pycache__"))


def scan_layer() -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(LAYER.rglob("*")):
        if not path.is_file():
            continue
        name = rel(path)
        if is_artifact(name) or path.suffix not in SOURCE_EXT:
            continue
        rows[name] = {"sha256": sha(path), "bytes": path.stat().st_size}
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag", default="v2", choices=sorted(PROFILES),
                    help="选哪一轮的档案（默认 v2，与不带参数时一字不差）")
    ap.add_argument("--baseline", default=None, help="覆盖档案里的 diff 起点")
    ap.add_argument("--out", default=None, help="覆盖档案里的清单输出路径")
    args = ap.parse_args()

    profile = PROFILES[args.tag]
    baseline_path = Path(args.baseline) if args.baseline else profile["baseline"]
    out = Path(args.out) if args.out else profile["out"]
    if not out.is_absolute():
        out = HERE / out

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    before = baseline["layer_source_files"]
    now = scan_layer()

    added = {name: row for name, row in now.items() if name not in before}
    removed = {name: before[name] for name in before if name not in now}
    modified = {name: {"before": before[name]["sha256"], "after": row["sha256"],
                       "before_bytes": before[name]["bytes"], "after_bytes": row["bytes"]}
                for name, row in now.items()
                if name in before and before[name]["sha256"] != row["sha256"]}
    unchanged = sorted(name for name, row in now.items()
                       if name in before and before[name]["sha256"] == row["sha256"])

    # 层外/watched：对冻结时那一份哈希，逐字节。
    # 两类基线的键名与字段名都不一样（v1_watched_now 记 `now`，
    # reply_path_watched_now 记 `sha256`），所以都从档案字段取，不在这里猜。
    watched_key = profile.get("watched_key", "v1_watched_now")
    watched = baseline.get(watched_key)
    if not isinstance(watched, dict) or not watched:
        print("BASELINE_MISSING_WATCHED_KEY", watched_key, "in", rel(baseline_path))
        return 2

    def frozen_sha(row: Any) -> str:
        return str(row.get("now") or row.get("sha256") or "")

    outside_drift: dict[str, Any] = {}
    outside_ok = 0
    outside_missing: list[str] = []
    for name, row in watched.items():
        path = ROOT / str(name).replace("\\", "/")
        if not path.exists():
            outside_missing.append(str(name))
            continue
        current = sha(path)
        if current != frozen_sha(row):
            outside_drift[str(name)] = {"at_freeze": frozen_sha(row), "now": current}
        else:
            outside_ok += 1

    # 第二把尺：档案里点名要一并核的"别人家的冻结清单"（LT 轮用它核层外 19 个 V1 文件）。
    extra_report: dict[str, Any] = {}
    for spec in profile.get("extra_watched", []):
        ebase = json.loads(Path(spec["baseline"]).read_text(encoding="utf-8"))
        ewatch = ebase.get(spec["key"]) or {}
        good = miss = 0
        drift: dict[str, Any] = {}
        for name, row in ewatch.items():
            path = ROOT / str(name).replace("\\", "/")
            if not path.exists():
                miss += 1
                outside_missing.append("EXTRA:" + str(name))
                continue
            current = sha(path)
            if current != frozen_sha(row):
                drift[str(name)] = {"at_freeze": frozen_sha(row), "now": current}
                outside_drift.setdefault("EXTRA:" + str(name), drift[str(name)])
            else:
                good += 1
        extra_report[spec["label"]] = {"baseline": rel(Path(spec["baseline"])),
                                       "key": spec["key"], "total": len(ewatch),
                                       "unchanged": good, "missing": miss, "drift": drift}

    deliverables: dict[str, Any] = {}
    for name in profile["deliverables"]:
        path = ROOT / name
        deliverables[name] = ({"sha256": sha(path), "bytes": path.stat().st_size}
                              if path.exists() else "MISSING")
    artifacts: dict[str, Any] = {}
    for name in profile["artifacts"]:
        path = LAYER / name
        artifacts["runtime/human_interaction/" + name] = (
            {"sha256": sha(path), "bytes": path.stat().st_size}
            if path.exists() else "MISSING")

    predecessor = PREDECESSOR.get(args.tag)
    payload = {
        "task": profile["task"],
        "tag": args.tag,
        "diff_from": rel(baseline_path),
        "diff_from_captured_at": baseline.get("captured_at"),
        "layer_source_count_now": len(now),
        "layer_source_count_at_freeze": len(before),
        "added": added,
        "modified": modified,
        "removed": removed,
        "unchanged": unchanged,
        "outside_layer_watched_key": watched_key,
        "outside_layer_watched_unchanged": outside_ok,
        "outside_layer_watched_total": len(watched),
        "outside_layer_drift": outside_drift,
        "extra_watched_report": extra_report,
        "outside_layer_missing": outside_missing,
        "deliverables": deliverables,
        "run_artifacts": artifacts,
        "predecessor_manifest_left_untouched": (
            rel(predecessor) if predecessor and predecessor.exists()
            else ("ABSENT:" + rel(predecessor) if predecessor else "n/a")),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print("本轮 tag = %s，diff 起点 %s（%s 冻结）" % (args.tag, rel(baseline_path),
                                                    baseline.get("captured_at")))
    print("本层新增 %d / 修改 %d / 删除 %d / 未变 %d"
          % (len(added), len(modified), len(removed), len(unchanged)))
    for name in added:
        print("  + %s" % name)
    for name, row in modified.items():
        print("  ~ %s  %s -> %s  (%d -> %d 字)"
              % (name, row["before"][:12], row["after"][:12],
                 row["before_bytes"], row["after_bytes"]))
    print("watched(%s) 未变 %d / %d" % (watched_key, outside_ok, len(watched)))
    for name, row in outside_drift.items():
        print("  ! 层外漂移 %s  %s -> %s" % (name, str(row["at_freeze"])[:12], row["now"][:12]))
    verdict = ("LAYER_OUTSIDE_DRIFT" if outside_drift or outside_missing
               else "%s_MANIFEST_OUTSIDE_CLEAN" % args.tag.upper())
    print(verdict)
    missing = [name for name, row in list(deliverables.items()) + list(artifacts.items())
               if row == "MISSING"]
    if missing:
        print("点名要的交付件缺失：%s" % ", ".join(missing))
    print("MANIFEST", out)
    return 1 if outside_drift or outside_missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
