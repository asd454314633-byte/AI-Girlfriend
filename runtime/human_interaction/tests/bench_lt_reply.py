# -*- coding: utf-8 -*-
"""正式回复「低思考」A/B/C：12 个既有场景 × 每臂 2 抽，只打 8090。

跑之前先把「A = 现在正式回复方式」这件事按盘上事实定义清楚（证据链在
`runtime/human_interaction_low_thinking_baseline.json` 与 `out/lt_knob_probe.json`）：

  * 8765 的冻结命令行里没有 `--responses_api_disable_thinking`，也没有
    `--responses_api_reasoning_effort` ⇒ s2s 吃 dataclass 默认值 ⇒
    `base_openai_compatible_language_model.py` L196-197 那一支命中 ⇒
    **生产今天这一发实际带着 `chat_template_kwargs={"enable_thinking": false}` 发出去**；
  * 本机 b11065 + Gemma4 实测：这一发 `reasoning_content` = **0 字**、0.62~0.87 秒；
    同一 prompt 把该键去掉（裸发）= **1521~1643 字隐藏思考**、5.14~5.81 秒。

⇒ owner 要的「A=现在 / B=关掉隐藏思考」在这台机器上**两者已经是同一发**——
低思考不是待办，是已经在跑的现状。于是三臂改成这样，才答得出 owner 真正要决的那件事
（「低思考正式回复要不要与真人互动层组合」）：

    A 生产低思考 · 旧系统块            —— 现状这一发
    B 生产带思考 · 旧系统块            —— 反事实：如果没关思考（= 关思考这一刀值多少）
    C 生产低思考 · V1 块 + 本轮判断    —— 组合起来是什么样（判断 + 正式回复两次调用）

owner 的判据照抄在 `judge_lt_verdict.py` 里，跑之前写死。
每臂都记 first_content_ms：实时链上用户等的是第一声，不是整段吐完。
`content` 存整段（截 600 字），因为六条质量轴里「理解语境/会不会说错话/被带偏/语气自然/真人感」
最终要 owner 目测，我不能代答。

不改生产任何文件、不改 8090 启动参数、不启动 7860/8765、磁盘配置全程 feature_enabled=false。
"""
from __future__ import annotations

import hashlib
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

from runtime.human_interaction import turn_judge, config              # noqa: E402
from runtime.human_interaction.tests import (bench_judge_gate, bench_judge_latency,  # noqa: E402
                                             bench_run, bench_scenarios, bench_shadow)

OUT_DIR = HERE / "out"
DEFAULT_TAG = "lt1"
DRAWS = 2                     # owner：「每个只跑 2 次就够」
THINKING_OFF = {"chat_template_kwargs": {"enable_thinking": False}}
ARMS = ("A_生产低思考_旧系统块", "B_生产带思考_旧系统块", "C_低思考_挂本轮判断")
ARM_A, ARM_B, ARM_C = ARMS
# 臂序。8090 是 `-np 1` 且没关 prompt cache，A/B/C 三臂里 旧块与 V1 块**前缀逐字相同**，
# 所以固定的 A→B→C 会让后面的臂吃前面焐热的前缀（复核时抓到：C 比 A 多 390 token
# 却 first 376ms < A 452ms）。`--reverse` 只把每抽的臂序倒过来（C→B→A），
# 其余一个字不改，用来量这个前缀效应有多大；不改 lt1 那份已交付的数。
REVERSE = False


def order_for(draw: int) -> tuple[str, ...]:
    return (ARMS[2], ARMS[1], ARMS[0]) if REVERSE else ARMS


def seq_index(arm: str, draw: int) -> int:
    return order_for(draw).index(arm) + 1
BASELINE = ROOT / "runtime" / "human_interaction_low_thinking_baseline.json"


def paths_for(tag: str) -> tuple[Path, Path]:
    return OUT_DIR / f"{tag}_ab_rows.jsonl", OUT_DIR / f"{tag}_ab_summary.json"


def production_model_name() -> str | None:
    """从 8765 的冻结命令行里取 --model_name 的值。

    注意坑：这条命令行是**人肉带引号**写的（`--model_name "Gemma4 12B"`），
    按空格切会切成 `'\"Gemma4'` 和 `'12B\"'` 两段——上一版探针就把这个残缺值发出去了
    （llama-server 不校验 model 字段，所以它照样 200，但那就不是生产发的那个值）。
    这里取到下一个 `--` 之前的所有 token 再剥引号。
    """
    tree = json.loads((ROOT / "HUMAN_INTERACTION_BASELINE.json").read_text(encoding="utf-8"))
    cmd = ((tree.get("voice_chain_baseline") or {}).get("s2s_commandLine")) or ""
    tokens = cmd.split()
    if "--model_name" not in tokens:
        return None
    idx = tokens.index("--model_name") + 1
    picked: list[str] = []
    for one in tokens[idx:]:
        if one.startswith("--"):
            break
        picked.append(one)
    return " ".join(picked).strip('"') or None


def stream_once(url: str, body: dict[str, Any], timeout: int = 180) -> dict[str, Any]:
    """与 probe_lt_knobs 同尺：流式，记首字时间、隐藏思考字数、正文字数、末尾 usage。"""
    import urllib.request

    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    started = time.time()
    first_content_ms = first_delta_ms = None
    reasoning_chars = content_chars = 0
    pieces: list[str] = []
    finish = None
    usage: dict[str, Any] = {}
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}),
                timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                blob = line[5:].strip()
                if blob == "[DONE]":
                    break
                chunk = json.loads(blob)
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    rc = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    ct = delta.get("content") or ""
                    now = time.time()
                    if first_delta_ms is None and (rc or ct):
                        first_delta_ms = round((now - started) * 1000)
                    reasoning_chars += len(str(rc))
                    if ct:
                        content_chars += len(str(ct))
                        pieces.append(str(ct))
                        if first_content_ms is None:
                            first_content_ms = round((now - started) * 1000)
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
    except Exception as exc:                                        # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {str(exc)[:180]}",
                "wall_s": round(time.time() - started, 2), "content": ""}
    return {"wall_s": round(time.time() - started, 2),
            "first_delta_ms": first_delta_ms, "first_content_ms": first_content_ms,
            "reasoning_chars": reasoning_chars, "content_chars": content_chars,
            "content": "".join(pieces), "finish": finish,
            "completion_tokens": usage.get("completion_tokens"),
            "prompt_tokens": usage.get("prompt_tokens")}


def build_body(messages: list[dict[str, str]], model: str | None, with_off: bool) -> dict[str, Any]:
    body: dict[str, Any] = {"messages": messages, "stream": True,
                            "stream_options": {"include_usage": True}}
    if model:
        body["model"] = model
    if with_off:
        body.update(THINKING_OFF)
    return body


def messages_for(system_text: str, history: tuple, user_text: str) -> list[dict[str, str]]:
    out = [{"role": "system", "content": system_text}]
    for one in history:
        role = str(one.get("role") or "user")
        text = str(one.get("text") or one.get("content") or "")
        out.append({"role": role if role in ("user", "assistant") else "user", "content": text})
    out.append({"role": "user", "content": user_text})
    return out


def run_scene(url: str, model: str | None, scene_no: int) -> list[dict[str, Any]]:
    scene = bench_scenarios.find(bench_scenarios.SCENARIOS[scene_no - 1]["key"])
    base_text, _m1, _a1 = bench_shadow.assemble_as(None, history=scene["history"],
                                                   user_text=scene["user_text"])
    v1_text, _m2, _a2 = bench_shadow.assemble_as("human_natural", history=scene["history"],
                                                 user_text=scene["user_text"])
    rows: list[dict[str, Any]] = []
    for draw in range(1, DRAWS + 1):
        # A / B：同一个旧系统块（生产现状的装配），只差那个关思考的键
        msgs = messages_for(base_text, scene["history"], scene["user_text"])

        def ab_row(arm: str, with_off: bool) -> dict[str, Any]:
            got = stream_once(url, build_body(msgs, model, with_off))
            return {"arm": arm, "scenario": scene_no, "draw": draw,
                    "system_sha": hashlib.sha256(base_text.encode("utf-8")).hexdigest()[:12],
                    "messages_sha": hashlib.sha256(
                        json.dumps(msgs, ensure_ascii=False).encode("utf-8")).hexdigest()[:12],
                    "context_chars": len(base_text), "judgment": "",
                    "arm_seq": seq_index(arm, draw), **got}

        def c_row() -> dict[str, Any]:
            # C：低思考 + 挂本轮判断（V2b 那一版的形状），判断那一发的时间要一起算
            with bench_judge_gate.judge_on(config.MODE_HUMAN_NATURAL):
                judged = turn_judge.judge(persona_text=bench_shadow.SHADOW_PERSONA,
                                          relationship_text=bench_shadow.SHADOW_PROFILE["relationship"],
                                          history=scene["history"], user_text=scene["user_text"])
            block = (v1_text + "\n\n" + judged["text"]).strip() if judged["text"] else v1_text
            c_msgs = messages_for(block, scene["history"], scene["user_text"])
            got = stream_once(url, build_body(c_msgs, model, True))
            return {"arm": "C_低思考_挂本轮判断", "scenario": scene_no, "draw": draw,
                    "system_sha": hashlib.sha256(block.encode("utf-8")).hexdigest()[:12],
                    "messages_sha": hashlib.sha256(
                        json.dumps(c_msgs, ensure_ascii=False).encode("utf-8")).hexdigest()[:12],
                    "context_chars": len(block), "judgment": str(judged.get("text") or ""),
                    "judge_s": round(float(judged.get("wall_s") or 0.0), 2),
                    "judge_ok": bool(judged.get("ok")),
                    "arm_seq": seq_index("C_低思考_挂本轮判断", draw), **got}

        steps = {ARM_A: lambda: ab_row(ARM_A, True),
                 ARM_B: lambda: ab_row(ARM_B, False),
                 ARM_C: c_row}
        for name in order_for(draw):
            rows.append(steps[name]())
        for arm in ARMS:
            one = next((r for r in rows if r["arm"] == arm and r["draw"] == draw), {})
            print("S%02d d%d %-22s wall=%ss first=%sms reason=%s tok=%s out=%s"
                  % (scene_no, draw, arm, one.get("wall_s"), one.get("first_content_ms"),
                     one.get("reasoning_chars"), one.get("completion_tokens"),
                     one.get("content_chars")))
    return rows


def med(values: list[float]) -> float:
    return round(statistics.median(values), 2) if values else 0.0


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    table: dict[str, Any] = {}
    for arm in ARMS:
        items = [one for one in rows if one["arm"] == arm]
        walls = [float(one["wall_s"]) for one in items]
        totals = [float(one["wall_s"]) + float(one.get("judge_s") or 0.0) for one in items]
        table[arm] = {
            "n": len(items),
            "reply_median_s": med(walls), "reply_max_s": round(max(walls or [0]), 2),
            "total_median_s": med(totals),
            "first_content_median_ms": med([float(one.get("first_content_ms") or 0) for one in items]),
            "reasoning_chars_median": med([float(one.get("reasoning_chars") or 0) for one in items]),
            "completion_tokens_median": med([float(one.get("completion_tokens") or 0) for one in items]),
            "content_chars_median": med([float(one.get("content_chars") or 0) for one in items]),
            "context_chars_median": med([float(one.get("context_chars") or 0) for one in items]),
            "errors": sum(1 for one in items if one.get("error")),
            "servile_hits": sum(1 for one in items
                                for hit in bench_run.SERVILE if hit in str(one.get("content") or "")),
            "ai_self_mentions": sum(1 for one in items
                                    if any(k in str(one.get("content") or "")
                                           for k in ("作为AI", "作为一个人工智能", "我是AI", "语言模型"))),
        }
    a, b, c = table.get(ARMS[0], {}), table.get(ARMS[1], {}), table.get(ARMS[2], {})
    return {"per_arm": table,
            "speedup_thinking_off_pct": round(
                (1.0 - a["reply_median_s"] / max(0.01, b["reply_median_s"])) * 100, 1) if a and b else None,
            "combine_total_vs_A": round(c["total_median_s"] / max(0.01, a["reply_median_s"]), 3)
                                  if a and c else None,
            "rows": rows}


def main(argv: list[str] | None = None) -> int:
    import argparse

    global REVERSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default=DEFAULT_TAG)
    ap.add_argument("--reverse", action="store_true",
                    help="每抽臂序倒过来跑（C→B→A），量 prompt cache 前缀焐热对倍数的影响")
    ap.add_argument("--allow-overwrite", action="store_true")
    args = ap.parse_args(argv)
    REVERSE = bool(args.reverse)
    OUT, SUMMARY = paths_for(args.tag)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for one in (OUT, SUMMARY):
        if one.exists() and not args.allow_overwrite:
            print("REFUSING_TO_OVERWRITE", one, "-- 换 --tag，或确认要重跑同一份时加 --allow-overwrite")
            return 3
    url = bench_run.llm_url()
    model = production_model_name()
    print("URL", url, "MODEL", repr(model))
    started = time.time()
    env_at_start = bench_judge_latency.env_snapshot()
    rows: list[dict[str, Any]] = []
    with OUT.open("w", encoding="utf-8") as sink:
        for scene_no in range(1, 13):
            for row in run_scene(url, model, scene_no):
                rows.append(row)
                sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                sink.flush()
    summary = summarise(rows)
    summary["env_at_start"] = env_at_start
    summary["env_at_end"] = bench_judge_latency.env_snapshot()
    summary["captured_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    summary["elapsed_s"] = round(time.time() - started, 1)
    summary["draws"] = DRAWS
    summary["model_sent"] = model
    summary["thinking_off_key"] = THINKING_OFF
    summary["arms"] = list(ARMS)
    summary["arm_order_per_draw"] = list(order_for(1))
    summary["note"] = ("A=生产现状这一发（带 enable_thinking=false）；B=同一 prompt 去掉该键（带思考，反事实）；"
                       "C=低思考 + V1 块 + 本轮判断（组合形状，total 含判断那一发）")
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, ensure_ascii=False, indent=1))
    print("SUMMARY", SUMMARY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
