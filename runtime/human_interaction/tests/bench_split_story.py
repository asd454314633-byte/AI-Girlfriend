# -*- coding: utf-8 -*-
"""把剧情推进那一档的行从三档对比件里分出去。

为什么要分：任务 §16 明确「剧情推进模式只做功能正确性验证，不加入质量排名」。
混在同一份 rows 文件里，下一步的统计就会把它算进三档对比——那是把这行字当没有。
所以这份小工具做的事只有拆分，不改任何一行内容，拆完两边各自留一份可复算的件。
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
MAIN = OUT / "bench_rows.jsonl"
STORY = OUT / "bench_rows_story.jsonl"
KEY = "剧情推进"


def main() -> int:
    rows = [json.loads(line) for line in MAIN.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    keep = [one for one in rows if one.get("arm") != KEY]
    story = [one for one in rows if one.get("arm") == KEY]
    if len(keep) + len(story) != len(rows):
        print("SPLIT_COUNT_MISMATCH")
        return 1
    MAIN.write_text("".join(json.dumps(one, ensure_ascii=False) + "\n" for one in keep),
                    encoding="utf-8")
    STORY.write_text("".join(json.dumps(one, ensure_ascii=False) + "\n" for one in story),
                     encoding="utf-8")
    print("三档对比 rows =", len(keep), "->", MAIN.name)
    print("剧情推进 rows =", len(story), "->", STORY.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
