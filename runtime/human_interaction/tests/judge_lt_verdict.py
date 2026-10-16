# -*- coding: utf-8 -*-
"""LT 轮判决：低思考正式回复该不该与真人互动层组合。**只读已写下的行，不重跑任何测量。**

owner 的裁决标准（原话）：
```text
如果：速度提升 ≥ 50% 质量基本不下降 那就继续，把"低思考正式回复"与真人互动层组合。
如果速度很快但真人感、理解力明显下降：立即停，不继续补。
```
门槛在**看到本轮 72 行之前**写死在这里（V2b 那轮的教训：口径跑之前定，跑完不换说法）。

先说清一件本轮才发现的事实，否则数字会被读成别的意思：owner 这条 A/B 的前提是
「A=现在正式回复方式、B=正式回复关闭隐藏思考」。盘上证据（s2s 的
`--responses_api_disable_thinking` 默认 True → `chat_template_kwargs.enable_thinking=false`）
+ 本机 b11065 实测（`out/lt_knob_probe.json`）显示**生产今天这一发本来就带着那个键，
隐藏思考已经是 0 字**。所以 A/B 在生产里是同一发，"关掉思考"不是待办收益、是既成事实。
本轮真正的对照是 bench 里的三臂：A=现状（低思考）、B=反事实（把那个键去掉、带思考）、
C=低思考 + 挂本轮判断（=owner 说的"组合"）。于是：
- 「速度提升 ≥ 50%」机械地量在 **B→A**（=把思考关掉值多少，反事实）；
  同一份数字必须并列报「在生产里还能不能再省」= 0，因为已经在省了；
- 「组合值不值」量在 **C/A**（挂上互动判断后整轮比现状慢多少）；
- 「质量基本不下降」用机械闸门 Q1–Q5 兜住"说错话/塌成嗯哦/撞句/还原话"，
  owner 列的另外几条（理解语境、被带偏、语气自然、真人感）是**主观轴**，
  脚本只把 72 行原文并排摆成表（`out/lt_ab_table.md`），**判读由 owner 给，我不代答**。

判尺自校验（本轮加的，因为上一轮吃过"尺坏报物坏"的亏）：Q4/Q5 除了 PASS/FAIL，
还把**实测分布**打出来（同场景两抽的不同正文数、每场景公共片段最长长度），
这样"0 命中"能区分"确实没有"和"阈值根本到不了"。
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for one in (str(ROOT), str(HERE)):
    if one not in sys.path:
        sys.path.insert(0, one)

from runtime.human_interaction.tests import bench_run, bench_scenarios  # noqa: E402

OUT_DIR = HERE / "out"
ROWS = OUT_DIR / "lt1_ab_rows.jsonl"
SUMMARY = OUT_DIR / "lt1_ab_summary.json"
PROBE = OUT_DIR / "lt_knob_probe.json"
VERDICT = OUT_DIR / "lt_verdict.json"
TABLE = OUT_DIR / "lt_ab_table.md"

ARM_A, ARM_B = "A_生产低思考_旧系统块", "B_生产带思考_旧系统块"
ARM_C = "C_低思考_挂本轮判断"

# —— 跑之前就写死的判据（LT1 + Q1–Q5 是判决；C/A 只报不判）——
LT1_SPEEDUP_MIN_PCT = 50.0      # owner：速度提升 ≥ 50%
Q1_USABLE_MIN = 24              # A 臂 24 行必须全部有正文、零错发
Q2_AI_SELF_MAX = 0              # 说错话的可测代理：不许自称 AI / 语言模型
Q3_LEN_MIN_CHARS = 4            # 长度不许塌成"嗯/哦"
Q3_LEN_RATIO_MIN = 0.4          # A 正文中位不得低于 B 的 40%
Q4_DUP_MAX = 2                  # 同场景两抽正文完全相同，最多 2 个场景
Q5_COPY_MIN_CHARS = 8           # 与用户原话的最长公共片段阈值
Q5_COPY_SCENE_MAX = 2           # 命中该阈值的场景最多 2 个
Q6_SAME_PROMPT_CONTROL = True   # A/B 必须是同一发：messages_sha 逐行相同，只差那个键


def med(values: list[float]) -> float:
    return round(statistics.median(values), 2) if values else 0.0


def main() -> int:
    for one in (ROWS, SUMMARY, PROBE):
        if not one.exists():
            print("MISSING_INPUT", one)
            return 2
    rows = [json.loads(line) for line in ROWS.read_text(encoding="utf-8").splitlines() if line.strip()]
    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
    probe = json.loads(PROBE.read_text(encoding="utf-8"))
    by_arm: dict[str, list[dict[str, Any]]] = {}
    for one in rows:
        by_arm.setdefault(one["arm"], []).append(one)
    a, b, c = by_arm.get(ARM_A, []), by_arm.get(ARM_B, []), by_arm.get(ARM_C, [])

    def text_of(row: dict[str, Any]) -> str:
        return str(row.get("content") or "").strip()

    # 内容按 (场景, 抽) 对齐，A/B 才能逐行对拍
    key = lambda one: (one["scenario"], one["draw"])  # noqa: E731
    a_map = {key(one): one for one in a}
    b_map = {key(one): one for one in b}

    usable = [one for one in a if not one.get("error") and text_of(one)]
    ai_self = sum(1 for one in a if any(hit in text_of(one) for hit in
                  ("作为AI", "作为一个人工智能", "我是AI", "语言模型", "人工智能助手")))
    servile_a = sum(1 for one in a for hit in bench_run.SERVILE if hit in text_of(one))
    servile_b = sum(1 for one in b for hit in bench_run.SERVILE if hit in text_of(one))
    len_a = med([float(one.get("content_chars") or 0) for one in a])
    len_b = med([float(one.get("content_chars") or 0) for one in b])

    # Q4 分布：每个场景两抽的"不同正文数"（1=两抽撞句）
    distinct_per_scene = {scene_no: len({text_of(one) for one in a if one["scenario"] == scene_no})
                          for scene_no in range(1, 13)}
    dup_scenes = [scene_no for scene_no, count in distinct_per_scene.items() if count == 1]

    # 判尺对照（不是改判据，是给 Q4 一个参照系）：同一条尺子量 B、C 两臂。
    # 没有这一栏，"A 撞句 3/12" 会被读成"尺子本来就紧"或者"抖而已"，两种都没法排除。
    def dup_of(items: list[dict[str, Any]]) -> list[int]:
        return [scene_no for scene_no in range(1, 13)
                if len({text_of(one) for one in items if one["scenario"] == scene_no}) == 1]

    dup_control = {"A_低思考": dup_scenes, "B_带思考": dup_of(b), "C_低思考+判断": dup_of(c)}
    dup_b, dup_c = dup_control["B_带思考"], dup_control["C_低思考+判断"]

    # Q5 分布：每场景 A 正文与用户原话的最长公共片段
    copy_detail = {}
    for scene_no in range(1, 13):
        joined = " ".join(text_of(one) for one in a if one["scenario"] == scene_no)
        copy_detail[scene_no] = len(bench_run.longest_shared(
            joined, str(bench_scenarios.SCENARIOS[scene_no - 1]["user_text"])))
    copied_scenes = [scene_no for scene_no, n in copy_detail.items() if n >= Q5_COPY_MIN_CHARS]

    # Q6 对照：A/B 的 messages_sha 必须逐行相等（差的那一个键在 extra_body 里，不进 messages）
    sha_pairs = [(one, b_map.get(key(one))) for one in a]
    sha_mismatch = [key(one) for one, mate in sha_pairs
                    if mate is None or one.get("messages_sha") != mate.get("messages_sha")]

    wall_a = med([float(one["wall_s"]) for one in a])
    wall_b = med([float(one["wall_s"]) for one in b])
    speedup = round((1.0 - wall_a / max(0.01, wall_b)) * 100, 1)
    first_a = med([float(one.get("first_content_ms") or 0) for one in a])
    first_b = med([float(one.get("first_content_ms") or 0) for one in b])

    # C/A：整轮（判断那一发 + 回复那一发）对生产的真实现状
    c_rows = [float(one["wall_s"]) + float(one.get("judge_s") or 0.0) for one in c]
    c_total = med(c_rows)
    judge_med = med([float(one.get("judge_s") or 0.0) for one in c])
    combine_ratio = round(c_total / max(0.01, wall_a), 3)

    gates = {
        "Q1 A臂可用行数": {"measured": "%d/%d" % (len(usable), len(a)),
                         "PASS": len(usable) >= Q1_USABLE_MIN},
        "Q2 A臂自称AI/语言模型": {"measured": ai_self, "PASS": ai_self <= Q2_AI_SELF_MAX},
        "Q2b A臂客服腔命中不高於B臂": {"measured": "A=%d B=%d" % (servile_a, servile_b),
                                   "PASS": servile_a <= servile_b},
        "Q3 A臂正文长度不塌": {"measured": "A中位=%s B中位=%s 比=%s" % (
            len_a, len_b, round(len_a / max(0.01, len_b), 3)),
            "PASS": len_a >= Q3_LEN_MIN_CHARS and len_a >= Q3_LEN_RATIO_MIN * len_b},
        "Q4 同场景两抽撞句场景数": {"measured": "%d/12 %s" % (len(dup_scenes), dup_scenes),
                               "distribution_每场景不同正文数": distinct_per_scene,
                               "control_同尺量另两臂": {"B_带思考": dup_b, "C_低思考挂判断": dup_c},
                               "PASS": len(dup_scenes) <= Q4_DUP_MAX},
        "Q5 把原话成句还回去的场景数": {"measured": "%d/12 %s" % (len(copied_scenes), copied_scenes),
                                  "distribution_最长公共片段": copy_detail,
                                  "阈值_判尺本身": "公共片段 ≥ %d 字" % Q5_COPY_MIN_CHARS,
                                  "PASS": len(copied_scenes) <= Q5_COPY_SCENE_MAX},
        "Q6 A/B 逐行同 prompt 对照": {"measured": "%d 行不一致" % len(sha_mismatch),
                                "PASS": not sha_mismatch},
    }
    quality_pass = all(one["PASS"] for one in gates.values())
    lt1 = {"criterion": "LT1 把隐藏思考关掉的提速 ≥ %d%%（B→A，回复那一发中位）" % int(LT1_SPEEDUP_MIN_PCT),
           "B_median_s": wall_b, "A_median_s": wall_a, "speedup_pct": speedup,
           "first_content_ms": {"A": first_a, "B": first_b,
                                "speedup_pct": round((1 - first_a / max(0.01, first_b)) * 100, 1)},
           "reasoning_chars_median": {"A": med([float(one.get("reasoning_chars") or 0) for one in a]),
                                      "B": med([float(one.get("reasoning_chars") or 0) for one in b])},
           "completion_tokens_median": {"A": med([float(one.get("completion_tokens") or 0) for one in a]),
                                        "B": med([float(one.get("completion_tokens") or 0) for one in b])},
           "PASS": speedup >= LT1_SPEEDUP_MIN_PCT,
           "前提修正": ("这一 %d%% 是【反事实】收益：B 臂=把生产已经在带的那个键去掉。"
                        "生产今天这一发本来就是低思考（见 probe 与 freeze 基线），"
                        "所以『还能不能再降 ≥50%%』的答案是 0——没有这笔待领的钱。" % speedup)}

    combine = {
        "ROLE": "本轮真正给 owner 决策的那个数（组合值不值）；owner 未对 C/A 立门槛，故只报不判",
        "A_reply_median_s": wall_a,
        "C_reply_median_s": med([float(one["wall_s"]) for one in c]),
        "C_judge_median_s": judge_med,
        "C_total_median_s": c_total,
        "ratio_C_over_A": combine_ratio,
        "delta_pct_C_over_A": round((combine_ratio - 1) * 100, 1),
        "delta_abs_s_C_over_A": round(c_total - wall_a, 2),
        "口径说明": ("分子=判断那一发+回复那一发（每行求和后取中位，不是中位相加）；"
                    "分母=生产现状那一发；绝对增量=同一跑内相减，禁止跨跑取数。"
                    "上一轮 V2b 的比值（+34.7% 汇合中位 / +51.4% 逐行配对后取中位 "
                    "=它产物里的 paired_ratio_median 1.514；均值口径 mean_basis_ratio 1.507=+50.7%）"
                    "分母是**裸发带思考**那一臂（V2b 量到 6.49s 中位；本轮同形状的 B 臂量到 "
                    + str(wall_b) + "s），比生产真实形状慢一个数量级，所以那批比值天然偏小；"
                    "本轮分母换成生产真实形状（低思考 " + str(wall_a) + "s）后比值必然变大。"
                    "两批都别单看——本轮绝对增量是 +" + str(round(c_total - wall_a, 2)) + " 秒。"),
        "v2b_frozen_ratio_vs_thinking_on_baseline": 1.347,
    }

    if not lt1["PASS"]:
        gate = "NO_SPEED_WIN"
    elif not quality_pass:
        gate = "STOP_MECHANICAL_QUALITY"
    else:
        gate = "COMBINE_ALLOWED_PENDING_OWNER_SUBJECTIVE_READ"

    # 臂序反转对照（§5.1）：另一批已写下的行（ltrev），同样是**只读重算**，不重跑测量。
    # 8090 开着 prompt cache，固定臂序会把公共前缀焐热给后面的臂，所以 C/A 这个比值
    # 的真实宽度要靠两跑一起给；这条不改判据，只是把已交付的数加宽。
    rev_rows_path = OUT_DIR / "ltrev_ab_rows.jsonl"
    cache_control = {"present": False}
    if rev_rows_path.exists():
        rr = [json.loads(line) for line in
              rev_rows_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        ra = [one for one in rr if one["arm"] == ARM_A]
        rb = [one for one in rr if one["arm"] == ARM_B]
        rc = [one for one in rr if one["arm"] == ARM_C]
        # 比值一律从**未取整**的中位算出来再取整：med() 取到 2 位小数，
        # 0.425 会变成 0.43 之类，拿它当分母会让 2.412 变成 2.429（本文件第一版就错在这）。
        def raw(items: list[dict[str, Any]], key: str = "wall_s") -> float:
            vals = [float(one.get(key) or 0.0) for one in items]
            return statistics.median(vals) if vals else 0.0

        r_a = raw(ra)
        r_b = raw(rb)
        r_total = raw([{"wall_s": float(one["wall_s"]) + float(one.get("judge_s") or 0.0)}
                       for one in rc])
        r_ratio = round(r_total / max(0.01, r_a), 3)
        cache_control = {
            "present": True, "tag": "ltrev", "rows": len(rr),
            "arm_order": "C→B→A", "errors": sum(1 for one in rr if one.get("error")),
            "A_reply_median_s": round(r_a, 3), "B_reply_median_s": round(r_b, 3),
            "speedup_pct_reverse": round((1 - r_a / max(0.01, r_b)) * 100, 1),
            "C_total_median_s": round(r_total, 3), "ratio_C_over_A_reverse": r_ratio,
            "fwd_ratio": combine_ratio,
            "读法": ("两跑一起给宽度，不取好看的那个：C/A ∈ [%.3f, %.3f]，绝对增量 +%.2f~+%.2f 秒；"
                     "B→A 提速两跑都 >90%%，对臂序稳健。反序里 A 反而更快（%.3f < %.3f），"
                     "因为 C 的 V1 块在它前面焐热了公共前缀——这条正是固定臂序的污染方向。"
                     % (min(combine_ratio, r_ratio), max(combine_ratio, r_ratio),
                        min(r_total - r_a, c_total - wall_a), max(r_total - r_a, c_total - wall_a),
                        r_a, wall_a)),
        }

    payload = {
        "task": "HUMAN_INTERACTION_LAYER_01/LT_VERDICT",
        "gate": gate,
        "owner_ruling_verbatim": ("如果：速度提升 ≥ 50% 质量基本不下降 那就继续，"
                                  "把\"低思考正式回复\"与真人互动层组合。"
                                  "如果速度很快但真人感、理解力明显下降：立即停，不继续补。"),
        "premise_correction": {
            "生产这一发已带关思考键": "chat_template_kwargs.enable_thinking=false（s2s 默认值）",
            "本机实测": probe.get("per_variant"),
            "判决口径": ("owner 的 A/B 在同一发上，A/B 差=0；本轮按三臂重排：A=现状低思考、"
                         "B=反事实带思考、C=低思考+挂本轮判断。LT1 的 %d%% 属反事实收益，"
                         "生产可领收益 0%%。" % speedup),
        },
        "LT1": lt1,
        "quality_mechanical": gates,
        "quality_mech_pass": quality_pass,
        "quality_control": {
            "同尺量三臂的撞句场景": dup_control,
            "每臂正文字数_min_中位_max": {
                "A_低思考": [min(float(one.get("content_chars") or 0) for one in a), len_a,
                             max(float(one.get("content_chars") or 0) for one in a)],
                "B_带思考": [min(float(one.get("content_chars") or 0) for one in b), len_b,
                             max(float(one.get("content_chars") or 0) for one in b)],
                "C_低思考挂判断": [min(float(one.get("content_chars") or 0) for one in c),
                                  med([float(one.get("content_chars") or 0) for one in c]),
                                  max(float(one.get("content_chars") or 0) for one in c)]},
            "为什么这一栏只是对照": ("Q4 的判据在跑之前钉在 A 臂上（=提议保留的那一臂），"
                                     "B/C 的数字不参与判决；抽数每臂每场景只有 2（owner：「每个只跑 2 次就够」），"
                                     "temp 1.0 下 2 抽不足以把「真会变笨」和「本来就抖」分开——"
                                     "按既有纪律，这一栏只报「看到的现象」，不升格成结论。"),
            "A臂就是生产今天的行为": ("所以 Q4 报出来的重复，不是本轮实验引进的新风险，"
                                      "而是生产现有形状（低思考）本来的样子；"
                                      "把它当成「低思考让人变笨」的证据需要更多抽，"
                                      "把它当成「别把互动层压在 0.5s 基线上」的理由则不需要。"),
        },
        "combine": combine,
        "cache_control": cache_control,
        "subjective_axes_left_to_owner": [
            "是否理解人物和语境", "是否更容易说错话（措辞层面）", "是否更容易被带偏",
            "情绪/语气是否自然", "真人感有没有明显下降"],
        "table_for_owner": TABLE.name,
        "summary_from_bench": summary.get("per_arm"),
        "env_at_start": summary.get("env_at_start"),
        "counts": {"rows": len(rows), "A": len(a), "B": len(b), "C": len(c),
                   "draws": summary.get("draws"), "model_sent": summary.get("model_sent")},
        "thresholds_frozen_before_rows": {
            "LT1_SPEEDUP_MIN_PCT": LT1_SPEEDUP_MIN_PCT, "Q1_USABLE_MIN": Q1_USABLE_MIN,
            "Q2_AI_SELF_MAX": Q2_AI_SELF_MAX, "Q3_LEN_MIN_CHARS": Q3_LEN_MIN_CHARS,
            "Q3_LEN_RATIO_MIN": Q3_LEN_RATIO_MIN, "Q4_DUP_MAX": Q4_DUP_MAX,
            "Q5_COPY_MIN_CHARS": Q5_COPY_MIN_CHARS, "Q5_COPY_SCENE_MAX": Q5_COPY_SCENE_MAX},
        "inputs": {"rows": ROWS.name, "summary": SUMMARY.name, "probe": PROBE.name,
                   "cache_control_rows": rev_rows_path.name if rev_rows_path.exists() else "ABSENT"},
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    VERDICT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = ["| 场景 | 用户原话 | A 低思考＝生产现状（中位 %.2fs） | B 带思考＝反事实（中位 %.2fs） | C 低思考+本轮判断（整轮中位 %.2fs） |" % (
             wall_a, wall_b, c_total),
             "|---|---|---|---|---|"]
    for scene_no in range(1, 13):
        scene = bench_scenarios.SCENARIOS[scene_no - 1]

        def cell(items: list[dict[str, Any]]) -> str:
            # 正文里有换行（S11 那种长回答）会把表格行撑断，这里折成一行；
            # 逐字原文仍然完整存在 lt1_ab_rows.jsonl 的 content 字段里，表只是索引。
            got = [" ".join(text_of(one).split()).replace("|", "／") or "（空）"
                   for one in items if one["scenario"] == scene_no]
            return "<br>".join(got) if got else "—"

        lines.append("| S%02d %s | %s | %s | %s | %s |" % (
            scene_no, scene["key"], str(scene["user_text"]).replace("|", "／"),
            cell(a), cell(b), cell(c)))
    TABLE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("LT1", json.dumps({"speedup_pct": speedup, "PASS": lt1["PASS"],
                             "A": wall_a, "B": wall_b,
                             "first_ms_A": first_a, "first_ms_B": first_b}, ensure_ascii=False))
    for name, one in gates.items():
        print("%-26s %-22s PASS=%s" % (name, json.dumps(one["measured"], ensure_ascii=False), one["PASS"]))
    print("机械质量闸门", "ALL_PASS" if quality_pass else "SOME_FAIL")
    print("Q4对照", json.dumps(dup_control, ensure_ascii=False))
    print("COMBINE", json.dumps({k: v for k, v in combine.items()
                                 if not isinstance(v, str)}, ensure_ascii=False))
    print("CACHE_CONTROL", json.dumps({k: v for k, v in cache_control.items()
                                       if not isinstance(v, str)}, ensure_ascii=False))
    print("GATE", gate)
    print("VERDICT", VERDICT)
    print("TABLE", TABLE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
