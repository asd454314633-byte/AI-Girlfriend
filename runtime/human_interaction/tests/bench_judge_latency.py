# -*- coding: utf-8 -*-
"""把「多挂一段本轮互动判断之后，正式回复慢了一倍」这件事拆开看清楚。

跑门槛时量到的一串数（S11，两抽）：

    旧系统单调          6.79 / 8.99 秒
    判断 + 正式回复     0.99 + 18.57  秒  ← 判断只花 1 秒，正式回复自己从 8 秒变 18 秒

这一条如果属实，V2 的代价就不是"+1 秒"而是"翻倍"，实时链直接不能用。
但先别下结论：多挂 90 个字进系统块，本身也可能是慢的原因，跟"判断有没有读对这一轮"
无关。所以这里做四臂，把三件事分开：

    A 旧系统块                    —— 基准，一次调用
    B V1 块 + 本场景的判断        —— V2 真实形状，两次调用
    C V1 块 + **别的场景的判断**  —— 同样式、同样长、内容与这一轮无关
    D V1 块，**一个字判断都不挂** —— V2b 补的一臂：隔离 V1 外壳本身的成本

C 是对照：如果 B 和 C 一样慢，慢的是"多了一段话"这件事（长度/格式），
判断内容本身没参与；如果 C 明显比 B 快，那才是"判断真的改了她怎么回"。
D 是记账：旧系统块 ~865 字、V1 块 ~1396 字——B 比 A 慢的那一截里，有一笔从 V1 起
就该算在"外壳"头上、不该算在"本轮判断"头上。V2 那轮没有 D，所以「+58%」当时没法归因；
owner 的门槛写的是「整轮总延迟增幅 ≤ 20%」，这一轮就得说清这 20% 由哪几段构成。
每臂都记 completion_tokens 与 reasoning_chars，秒数就能换算成"多想了多少"，
不用靠猜。

跑法（会打 8090，正式回复那一发是**裸发**，形状与实时链一致）：

    runtime/core/venv/Scripts/python.exe runtime/human_interaction/tests/bench_judge_latency.py --tag judge_slim_latency

只写 tests/out/，磁盘配置全程 feature_enabled=false。已有同名产物时**拒绝覆盖**
（V2 那三臂的回执是被复核过的证据，要重跑请换 --tag）。
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.human_interaction import turn_judge                       # noqa: E402
from runtime.human_interaction.tests import (bench_judge_gate, bench_run,  # noqa: E402
                                             bench_scenarios, bench_shadow)

OUT_DIR = HERE / "out"
DEFAULT_TAG = "judge_latency"        # 这一份是 V2 三臂那轮的冻结回执，名字不许被复用


def paths_for(tag: str) -> tuple[Path, Path]:
    return OUT_DIR / f"{tag}_rows.jsonl", OUT_DIR / f"{tag}_summary.json"


# 判断来自哪一个场景、挂到哪一个场景上：(被测场景, 判断来源场景)。
# 来源与被测同一个 = B 臂；不同 = C 臂。
MISMATCHED = {11: 6, 10: 8, 6: 11}
# 3 抽不是"多加两发玩玩"：正式回复这一发是 temp 1.0，单抽下的快慢结论站不住
# （V2 那轮 A 臂均值 7.99s、最差 20.46s，两抽根本分不出"变慢"和"本来就抖"）。
# owner 这条门槛是比值，比值要稳就得每臂每场景都够抽。
DRAWS = 3


def env_snapshot() -> dict[str, Any]:
    """把"这一轮是在什么机器状态下量的"一起落盘。

    延迟数字离开环境就没有意义：8090 现在是新起的（昨天那个 pid 12112 已经不在了），
    卡上还有 ComfyUI 8197 在resident。这些都必须写在产物里，而不是等交付时凭记忆说。
    """
    import subprocess

    import psutil
    out: dict[str, Any] = {}
    try:
        raw = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=15).stdout.strip()
        used, total = [float(one) for one in raw.split(",")[:2]]
        out["gpu_used_mib"], out["gpu_total_mib"] = used, total
        out["gpu_free_mib"] = total - used
    except Exception as exc:
        out["gpu_error"] = f"{type(exc).__name__}:{exc}"
    ram = psutil.virtual_memory()
    out["ram_available_gb"] = round(ram.available / 2 ** 30, 1)
    out["ram_used_percent"] = ram.percent
    for one in psutil.process_iter(["pid", "name", "cmdline"]):
        name = (one.info.get("name") or "").lower()
        if name.startswith("llama-server"):
            out["llama_server_pid"] = one.info["pid"]
            out["llama_server_cmdline"] = " ".join(one.info.get("cmdline") or [])[:300]
        elif "comfy" in " ".join(one.info.get("cmdline") or []).lower()[:200]:
            out.setdefault("other_gpu_tenants", []).append(
                f"{one.info['name']}:{one.info['pid']}")
    return out


def token_of(row: dict[str, Any]) -> int:
    return int(((row.get("usage") or {}).get("completion_tokens")) or 0)


def brief(row: dict[str, Any], label: str, scene_no: int, draw: int, **over: Any) -> dict[str, Any]:
    out = {"arm": label, "scenario": scene_no, "draw": draw,
           "wall_s": float(row.get("wall_s") or 0.0),
           "completion_tokens": token_of(row),
           "reasoning_chars": int(row.get("reasoning_chars") or 0),
           "content_chars": len(str(row.get("content") or "")),
           "content": str(row.get("content") or "")[:160],
           "context_chars": int(over.pop("context_chars", 0))}
    out.update(over)
    return out


def run_scene(scene_no: int) -> list[dict[str, Any]]:
    from runtime.human_interaction import config
    scene = bench_scenarios.find(bench_scenarios.SCENARIOS[scene_no - 1]["key"])
    src_no = MISMATCHED.get(scene_no, scene_no)
    src = scene if src_no == scene_no else bench_scenarios.find(
        bench_scenarios.SCENARIOS[src_no - 1]["key"])
    persona = bench_shadow.SHADOW_PERSONA
    relationship = bench_shadow.SHADOW_PROFILE["relationship"]
    rows: list[dict[str, Any]] = []

    for draw in range(1, DRAWS + 1):
        # A：旧系统块，一次调用
        base_text, _meta, _added = bench_shadow.assemble_as(None, history=scene["history"],
                                                            user_text=scene["user_text"])
        one = bench_run.call(base_text, scene["history"], scene["user_text"])
        rows.append(brief(one, "A_旧系统单调", scene_no, draw, context_chars=len(base_text)))

        # B / C：V1 块 + 判断（本场景 / 别的场景）
        arm_text, _m2, _a2 = bench_shadow.assemble_as("human_natural", history=scene["history"],
                                                      user_text=scene["user_text"])
        for label, material in (("B_V2b本场景判断", scene), ("C_V2b错场景判断", src)):
            with bench_judge_gate.judge_on(config.MODE_HUMAN_NATURAL):
                judged = turn_judge.judge(persona_text=persona, relationship_text=relationship,
                                          history=material["history"],
                                          user_text=material["user_text"])
            block = (arm_text + "\n\n" + judged["text"]).strip() if judged["text"] else arm_text
            two = bench_run.call(block, scene["history"], scene["user_text"])
            rows.append(brief(two, label, scene_no, draw,
                              context_chars=len(block),
                              judge_s=round(float(judged.get("wall_s") or 0.0), 2),
                              judge_ok=bool(judged.get("ok")),
                              judgment_chars=len(str(judged.get("text") or "")),
                              judgment=str(judged.get("text") or ""),
                              judge_source_scene=src_no if label.startswith("C") else scene_no))
            print(f"S{scene_no:02d} d{draw} {label} reply={two.get('wall_s')}s "
                  f"judge={judged.get('wall_s')}s tok={token_of(two)} "
                  f"reason={two.get('reasoning_chars')}")
        # D：V1 块，一个字判断都不挂。**这一臂是这一轮新加的，V2 那三臂没有它。**
        # 加它只有一个理由：B 比 A 慢的那一截里，有多少根本跟"本轮互动判断"无关——
        # V1 那外壳本来就比旧系统块长一截（旧块 ~865 字，V1 块 ~1396 字），这笔账
        # 在 V2 那轮里被算到了判断头上。没有 D，"整轮 +58%" 这句话就没法归因。
        three = bench_run.call(arm_text, scene["history"], scene["user_text"])
        rows.append(brief(three, "D_V1块无判断", scene_no, draw, context_chars=len(arm_text)))
        print(f"S{scene_no:02d} d{draw} A_旧系统单调 reply={one.get('wall_s')}s "
              f"tok={token_of(one)} reason={one.get('reasoning_chars')}")
        print(f"S{scene_no:02d} d{draw} D_V1块无判断 reply={three.get('wall_s')}s "
              f"tok={token_of(three)} reason={three.get('reasoning_chars')}")
    return rows


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    per_arm: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        per_arm.setdefault(row["arm"], []).append(row)
    table = {}
    for arm, items in sorted(per_arm.items()):
        walls = [one["wall_s"] for one in items]
        # 判断那一发是**额外**的时间，实时链上用户等的是「判断 + 正式回复」合计，
        # 所以 total_median_s 才是 owner 那条「整轮总延迟」的口径；reply_* 只到回复结束。
        totals = [one["wall_s"] + float(one.get("judge_s") or 0.0) for one in items]
        table[arm] = {"n": len(items),
                      "reply_median_s": round(statistics.median(walls), 2),
                      "total_median_s": round(statistics.median(totals), 2),
                      "reply_avg_s": round(statistics.mean(walls), 2),
                      "reply_max_s": round(max(walls), 2),
                      "completion_tokens_avg": round(statistics.mean(
                          [one["completion_tokens"] for one in items]), 1),
                      "reasoning_chars_avg": round(statistics.mean(
                          [one["reasoning_chars"] for one in items]), 1),
                      "content_chars_avg": round(statistics.mean(
                          [one["content_chars"] for one in items]), 1),
                      "context_chars_avg": round(statistics.mean(
                          [one["context_chars"] for one in items]), 1),
                      "judgment_chars_avg": round(statistics.mean(
                          [float(one.get("judgment_chars") or 0.0) for one in items]), 1),
                      "judge_s_avg": round(statistics.mean(
                          [float(one.get("judge_s") or 0.0) for one in items]), 2)}
    base = table.get("A_旧系统单调", {})
    for arm in ("B_V2b本场景判断", "C_V2b错场景判断", "D_V1块无判断"):
        if arm in table and base:
            table[arm]["reply_vs_A_median"] = round(
                table[arm]["reply_median_s"] / max(0.01, base.get("reply_median_s", 0.0)), 3)
            table[arm]["total_vs_A_median"] = round(
                table[arm]["total_median_s"] / max(0.01, base.get("reply_median_s", 0.0)), 3)
            table[arm]["tokens_vs_A"] = round(table[arm]["completion_tokens_avg"]
                                              / max(1.0, base.get("completion_tokens_avg", 1.0)), 2)
    b_avg = table.get("B_V2b本场景判断", {}).get("reply_avg_s", 0)
    c_avg = table.get("C_V2b错场景判断", {}).get("reply_avg_s", 0)
    return {"per_arm": table,
            "verdict_hint": {
                "长度还是内容": "多挂一段就慢（与判断内容无关）"
                if abs(b_avg - c_avg) <= 1.5 else "本场景与错场景差得开，判断内容参与了",
                "判断之外还有多少": "D 臂 = V1 外壳本身，B−D 才是这一段判断的净成本"},
            "rows": rows}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default=DEFAULT_TAG)
    ap.add_argument("--allow-overwrite", action="store_true")
    args = ap.parse_args(argv)
    OUT, SUMMARY = paths_for(args.tag)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for one in (OUT, SUMMARY):
        if one.exists() and not args.allow_overwrite:
            print("REFUSING_TO_OVERWRITE", one, "-- 换 --tag，或确认要重跑同一份时加 --allow-overwrite")
            return 3
    rows: list[dict[str, Any]] = []
    started = time.time()
    env_at_start = env_snapshot()
    with OUT.open("w", encoding="utf-8") as sink:
        for scene_no in sorted(MISMATCHED):
            for row in run_scene(scene_no):
                rows.append(row)
                sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                sink.flush()
    summary = summarise(rows)
    summary["env_at_start"] = env_at_start
    summary["env_at_end"] = env_snapshot()
    summary["elapsed_s"] = round(time.time() - started, 1)
    summary["captured_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    summary["draws"] = DRAWS
    summary["scenes"] = sorted(MISMATCHED)
    summary["note"] = ("A=旧系统块一次调用；B=V1块+本场景判断（判断+回复两次）；"
                       "C=同形状但判断来自别的场景；D=V1块不带判断（隔离外壳本身的成本）")
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"per_arm": summary["per_arm"],
                      "verdict_hint": summary["verdict_hint"],
                      "elapsed_s": summary["elapsed_s"]}, ensure_ascii=False, indent=1))
    print("SUMMARY", SUMMARY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
