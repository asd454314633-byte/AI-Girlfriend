"""V2-0：用**产品自己的**人物选择路径把当前人物指针接回去。

只调一次 POST /api/character/select（与 character_panel.js:272 同一个处理器、同一个请求体形状）。
不写代码、不改 store、不手改 SQLite、不碰归档位。
"""
from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
POINTER = ROOT / "runtime" / "character" / "data" / "active_character.json"
TARGET = "cbae1a1a425"          # 验收乙：在库、未归档、有台词、4 条长期记忆
BASELINE = ROOT / "runtime" / "human_interaction_baseline_prompt.json"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:7860{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def get_prompt(q: str = "") -> dict:
    """装配回执：键名以 character_admin.character_prompt 源码为准（内容/字数/编号）。"""
    url = f"http://127.0.0.1:7860/api/character/prompt?q={urllib.parse.quote(q)}"
    with urllib.request.urlopen(url, timeout=25) as r:
        return json.loads(r.read())


def main() -> int:
    before = POINTER.read_text(encoding="utf-8") if POINTER.exists() else "MISSING"
    print("POINTER_BEFORE", before)

    reply = post("/api/character/select", {"character_id": TARGET})
    print("SELECT_REPLY", json.dumps(reply, ensure_ascii=False)[:400])

    print("POINTER_AFTER", POINTER.read_text(encoding="utf-8"))

    got = get_prompt("")
    text = str(got.get("内容") or "").strip()
    print("PROMPT_OK", bool(got.get("ok")), "编号", got.get("编号"), "人物", got.get("人物名称"),
          "chars", got.get("字数"), "sha", sha(text)[:16] if text else "-",
          "台词chars", len(str(got.get("台词") or "")),
          "让出去", got.get("让出去了什么"))
    if not text:
        print("PROMPT_REPLY", json.dumps(got, ensure_ascii=False)[:400])

    rec = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    print("V1_BASELINE_CHARACTER", rec.get("active_character"),
          "(四格判据按显式人物号重算，不受指针影响)")
    return 0 if text else 1


if __name__ == "__main__":
    raise SystemExit(main())
