# -*- coding: utf-8 -*-
"""§16 的 12 场景三档对比台（只跑文字层，不碰语音、不碰数字人）。

跑法：

    python -m runtime.human_interaction.tests.bench_run --draws 3

三条口径要先说清楚：

  * **同一人物、同一模型、同一上下文**：三档只差互动层的开关与档位，人物走影子档案，
    上下文走生产那一条 build + messages；模型地址从 runtime/llm/models.json 读，
    不写死；
  * **不加任何采样参数**：请求体里没有 temperature/top_p，让 8090 用自己冻结的那份
    （实测 --temp 1.0 --top-p 0.95）。既然是 1.0，一次抽样不能说明任何问题，
    所以每格固定抽 draws 次（默认 3），并留一列同档内部的差异作为「噪声底」；
  * **判据比生产自己那份检测器独立**：这里的重复/复读/套话是另写一遍算出来的，
    故意不用被测模块的函数，免得「用嫌疑人的尺子量嫌疑人」。

写只写在 tests/out/ 下面，生产的配置与数据库一律不碰。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.human_interaction.tests import bench_scenarios, bench_shadow  # noqa: E402

OUT_DIR = HERE / "out"
ROWS_PATH = OUT_DIR / "bench_rows.jsonl"
SUMMARY_PATH = OUT_DIR / "bench_summary.json"

ARMS: tuple[str | None, ...] = (None, "human_natural", "autonomous")
ARM_NAME = {None: "旧基线", "human_natural": "真人自然", "autonomous": "强角色自主",
            "story_director": "剧情推进"}

# 客服腔/交代式说法的候选标记。只是候选，不当判决——最终逐项判断在报告里人工给。
SERVILE = ("辛苦了", "注意休息", "我理解你的感受", "有任何", "随时跟我", "作为AI", "作为一个人工智能",
           "希望对你有帮助", "首先需要", "总之", "值得一提的是", "让我们一起")
DECIDE_FOR_USER = ("我帮你决定", "你就听我的", "你必须", "你应该", "我替你把")


def llm_url() -> str:
    """模型地址从注册表读，不写死端口（§11）。"""
    registry = json.loads((PROJECT_ROOT / "runtime" / "llm" / "models.json").read_text(encoding="utf-8"))
    listen = registry.get("listen") or {}
    return f"http://127.0.0.1:{int(listen.get('port') or 0)}/v1/chat/completions"


def grams(text: str, n: int) -> list[str]:
    cleaned = re.sub(r"\s+", "", text or "")
    return [cleaned[i:i + n] for i in range(0, max(0, len(cleaned) - n + 1))] if n > 1 else []


def longest_shared(a: str, b: str) -> str:
    """两段话里最长的共同片段（独立写一遍，用于测「把他那句原样还回去」）。"""
    if not a or not b:
        return ""
    best, prev = "", [""] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [""] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + a[i - 1]
                if len(cur[j]) > len(best):
                    best = cur[j]
        prev = cur
    return best


def repeated_bits(replies: list[str], min_count: int = 2, min_len: int = 3) -> list[str]:
    """几抽之间反复出现的片段（>=2 字，跨抽出现 >=min_count 次）。"""
    counts: Counter[str] = Counter()
    for n in range(2, 9):
        for reply in replies:
            seen: set[str] = set()
            for g in grams(reply, n):
                if g not in seen:
                    seen.add(g)
                    counts[g] += 1
    hits = [g for g, c in counts.items() if c >= min_count and len(g) >= min_len]
    keep: list[str] = []
    for g in sorted(hits, key=len, reverse=True):
        if not any(g in one for one in keep):
            keep.append(g)
    return keep[:6]


def call(system_text: str, history: tuple, user_text: str, timeout: int = 300) -> dict[str, Any]:
    """一次生成。请求体里没有任何采样参数——用 8090 自己冻结的那份。"""
    messages = [{"role": "system", "content": system_text}]
    for one in history:
        role = str(one.get("role") or "user")
        text = str(one.get("text") or one.get("content") or "")
        messages.append({"role": role if role in ("user", "assistant") else "user", "content": text})
    messages.append({"role": "user", "content": user_text})
    payload = json.dumps({"messages": messages, "stream": False}).encode("utf-8")
    started = time.time()
    with urllib.request.urlopen(urllib.request.Request(llm_url(), data=payload,
                                                       headers={"Content-Type": "application/json"}),
                                timeout=timeout) as resp:
        data = json.loads(resp.read())
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return {"content": str(message.get("content") or "").strip(),
            "reasoning_chars": len(str(message.get("reasoning_content") or "")),
            "finish": str(choice.get("finish_reason") or ""),
            "usage": data.get("usage") or {},
            "timings": data.get("timings") or {},
            "wall_s": round(time.time() - started, 2),
            "models": (data.get("model") or "")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=3)
    ap.add_argument("--arms", default=",".join("" if one is None else one for one in ARMS))
    ap.add_argument("--scenarios", default="", help="逗号分隔的场景编号，默认全 12 个")
    ap.add_argument("--append", action="store_true", help="不清空已有 rows")
    args = ap.parse_args(argv)

    wanted_arms = []
    for raw in (one.strip() for one in args.arms.split(",")):
        wanted_arms.append(None if raw in ("", "baseline", "off") else raw)
    wanted_nos = {int(one) for one in args.scenarios.replace(" ", ",").split(",") if one.strip().isdigit()}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if ROWS_PATH.exists() and not args.append:
        ROWS_PATH.unlink()

    print(json.dumps(bench_shadow.build_shadow(), ensure_ascii=False))
    print("LLM_URL", llm_url())
    from runtime.human_interaction import config
    cliches = config.string_list("cliche_templates")

    total = len(wanted_arms) * len([s for s in bench_scenarios.SCENARIOS
                                    if not wanted_nos or s["no"] in wanted_nos]) * args.draws
    done = 0
    with ROWS_PATH.open("a", encoding="utf-8") as sink:
        for scene in bench_scenarios.SCENARIOS:
            if wanted_nos and scene["no"] not in wanted_nos:
                continue
            for arm in wanted_arms:
                # 上下文每格只装配一次：三抽取的是同一个 system 文本，抽的是模型
                system_text, meta, added = bench_shadow.assemble_as(
                    arm, history=scene["history"], user_text=scene["user_text"])
                for draw in range(1, args.draws + 1):
                    row = {"scenario": scene["no"], "key": scene["key"],
                           "requirement": scene["requirement"], "arm": ARM_NAME[arm], "arm_key": arm or "baseline",
                           "draw": draw, "context_chars": meta["chars"],
                           "added_chars": len(added), "added": added,
                           "system_sha": _sha(system_text)}
                    try:
                        row.update(call(system_text, scene["history"], scene["user_text"]))
                    except Exception as exc:
                        row["error"] = f"{type(exc).__name__}: {exc}"
                    row["reply_chars"] = len(row.get("content") or "")
                    row["servile_hits"] = [one for one in SERVILE if one in (row.get("content") or "")]
                    row["decide_hits"] = [one for one in DECIDE_FOR_USER if one in (row.get("content") or "")]
                    row["cliche_hits"] = [one for one in (cliches or ()) if one and one in (row.get("content") or "")]
                    echo = longest_shared(scene["user_text"], row.get("content") or "")
                    row["echo_shared"] = echo if len(echo) >= 6 else ""
                    sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                    sink.flush()
                    done += 1
                    print(f"[{done}/{total}] S{scene['no']:02d} {ARM_NAME[arm]} "
                          f"d{draw} {row['reply_chars']}字 {row.get('wall_s','?')}s "
                          f"{'ERR ' + row['error'][:60] if 'error' in row else ''}", flush=True)
    print("ROWS", ROWS_PATH)
    return 0


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


if __name__ == "__main__":
    sys.exit(main())
