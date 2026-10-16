"""判断请求的四种发法，逐一实测：延迟、token、思考有没有关掉、六行还合不合格式。

动机：裸请求实测 7.01s / completion_tokens 713 / 正文只有 78 字 ——
钱几乎全花在 reasoning_content 上。这一轮要回答的是「关掉思考能不能把
一次判断压到 1 秒级」，而不是靠猜。只读，不改服务、不改 8090 argv。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.human_interaction import llm_client, modes  # noqa: E402
from runtime.human_interaction.turn_judge import declared_keys, parse  # noqa: E402

BRIEF = modes.judge_prompt_text("human_natural")
BODY = """她是谁：
你叫苏晚，二十八岁，在一家旧书店做装帧修复。
你跟他在一起两年了，不是客服，也不是助理。
你句子短，喜欢反问，不喜欢那种安排好的体贴。

你们的关系：
在一起两年

最近几句：
对方：跟你说个事
她：说

对方刚说的这一句：
我寻思养只猫替代你算了，猫不嫌我穷还蹭我
"""

VARIANTS: list[tuple[str, dict]] = [
    ("A_裸发（生产同形状）", {}),
    ("B_chat_template_kwargs.enable_thinking=false",
     {"chat_template_kwargs": {"enable_thinking": False}}),
    ("C_reasoning_effort=none", {"reasoning_effort": "none"}),
    ("D_both_off_plus_max_tokens_120",
     {"chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "none",
      "max_tokens": 120}),
]


def one(label: str, extra: dict, timeout: float) -> dict:
    url = llm_client.endpoint()["url"]
    body = {"messages": [{"role": "system", "content": BRIEF},
                         {"role": "user", "content": BODY}], "stream": False}
    body.update(extra)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    started = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}),
                timeout=timeout) as resp:
            data = json.loads(resp.read(400_000).decode("utf-8", "replace"))
    except Exception as exc:                                   # noqa: BLE001
        return {"variant": label, "error": f"{type(exc).__name__}: {str(exc)[:150]}",
                "wall_s": round(time.time() - started, 2)}
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = str(message.get("content") or "")
    keys = declared_keys(BRIEF)
    parsed = parse(content, keys, value_max_chars=24)
    usage = data.get("usage") or {}
    return {"variant": label, "wall_s": round(time.time() - started, 2),
            "completion_tokens": usage.get("completion_tokens"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
            "reasoning_chars": len(str(message.get("reasoning_content") or "")),
            "content_chars": len(content), "finish": choice.get("finish_reason"),
            "fields_filled": f"{parsed['filled']}/{len(keys)}",
            "content": content[:400]}


def main() -> int:
    timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    print("BRIEF_CHARS", len(BRIEF), "KEYS", declared_keys(BRIEF))
    rows = []
    for label, extra in VARIANTS:
        row = one(label, extra, timeout)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False, indent=1))
    out = Path(__file__).resolve().parent / "out"
    out.mkdir(exist_ok=True)
    (out / "judge_thinking_probe.json").write_text(
        json.dumps({"variants": rows, "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print("OUT", out / "judge_thinking_probe.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
