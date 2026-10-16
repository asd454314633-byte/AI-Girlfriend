"""一次裸请求：看 8090 对判断简报到底回什么、回多久、是不是在啰嗦。

不经过本层任何代码——直接把 turn_judge 会发的那份 messages 打出去。
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
from runtime.human_interaction.tests import bench_scenarios  # noqa: E402

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


def main() -> int:
    url = llm_client.endpoint()["url"]
    timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0
    payload = json.dumps({"messages": [{"role": "system", "content": BRIEF},
                                       {"role": "user", "content": BODY}],
                          "stream": False}, ensure_ascii=False).encode("utf-8")
    print("URL", url, "payload_chars", len(payload.decode("utf-8")))
    started = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}),
                timeout=timeout) as resp:
            raw = resp.read(400_000)
    except Exception as exc:                                   # noqa: BLE001
        print("RAW_FAIL", type(exc).__name__, str(exc)[:200], "after",
              round(time.time() - started, 2), "s")
        return 1
    data = json.loads(raw.decode("utf-8", "replace"))
    wall = round(time.time() - started, 2)
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    print("WALL_S", wall)
    print("FINISH", choice.get("finish_reason"))
    print("USAGE", json.dumps(data.get("usage") or {}, ensure_ascii=False))
    print("TIMINGS", json.dumps(data.get("timings") or {}, ensure_ascii=False))
    print("MODEL", data.get("model"))
    print("REASONING_CHARS", len(str(message.get("reasoning_content") or "")))
    content = str(message.get("content") or "")
    print("CONTENT_CHARS", len(content))
    print("---CONTENT---")
    print(content[:2500])
    print("---END---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
