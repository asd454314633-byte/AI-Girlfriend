# -*- coding: utf-8 -*-
"""§17 逐项判断用的「内容级」计数台。

为什么还要这一个：句长、跨抽反复这些数值指标，在 temp=1.0 三抽下面根本分不出真假
（格内噪声底中位数就有 4 个字）。但「她到底有没有接住对方给的那颗橘子」「她有没有
把决定权原样还回去」这种是**内容事实**，一眼数得出来，可复算。

每一条判据都写成「哪些字出现 = 这一抽算命中」，词表原样摊在这里，
谁觉得判据不对都可以改这张表重跑——不是藏在某段代码里的印象分。

口径：只有 3/3 对 0/3 这种形状才算可分辨的差异；2/3 对 1/3 一律记 NOT_RESOLVED。
"""
from __future__ import annotations

import collections
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROWS = HERE / "out" / "bench_rows.jsonl"
ARMS = ("旧基线", "真人自然", "强角色自主")

# 场景 -> 判据名 -> 命中词表（任一出现即算命中这一抽）
CHECKS: dict[int, dict[str, tuple[str, ...]]] = {
    # 对方把橘子放在桌上了：她这一轮接没接这个实物（接了才算听见了这句话）
    9: {"接住对方放下的那颗橘子": ("吃", "橘子", "甜")},
    # 对方澄清「报表不是我做的」：她有没有认出这件事本身
    7: {"认出『不是他的错』": ("不是你的", "明明不是", "不是你")},
    # 对方拿证据把她说赢了：她接不接受这个新证据
    2: {"接受对方拿出的新证据": ("难怪", "怪不得", "没白说", "没说错", "换一家",
                                   "换家", "别去", "没必要再去")},
    # 争执：她是自己承担一个动作，还是把决定权原样踢回去
    8: {"自己承担一个动作": ("我去", "我就去", "带个", "那就去", "那我去"),
        "把决定权踢回给对方": ("那你要我", "那你想让我", "那你想让我", "你要我")},
    # 不合理的要求：她驳的话里有没有自己的理由，还是只把他那句反问回去
    3: {"驳的话里有自己的理由": ("好看", "我修", "我乐意", "总得有人", "我在这儿",
                                   "白干", "发呆"),
        "只是把他那句反问回去": ("没必要", "那我不干", "闲着")},
    # 反复检查：三抽是不是全落进同一个「那你想……」推回模板
    12: {"落进『那你想 / 那你让』模板": ("那你想", "那你让", "那你要")},
}


def main() -> int:
    rows = [json.loads(line) for line in ROWS.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    grid = collections.defaultdict(list)
    for one in rows:
        grid[(int(one["scenario"]), one["arm"])].append(str(one["content"]))

    print("| 场景 | 判据 | " + " | ".join(ARMS) + " | 形状 |")
    print("|---|---|---|---|---|")
    for scene, checks in CHECKS.items():
        for name, needles in checks.items():
            cells = []
            for arm in ARMS:
                replies = grid[(scene, arm)]
                hits = sum(1 for text in replies if any(k in text for k in needles))
                cells.append("%d/%d" % (hits, len(replies)))
            counts = [sum(1 for text in grid[(scene, arm)]
                          if any(k in text for k in needles)) for arm in ARMS]
            top, bottom = max(counts), min(counts)
            shape = ("可分辨" if (top == 3 and bottom == 0) or (top == 0 and bottom == 3)
                     else ("三档一致" if len(set(counts)) == 1 else "NOT_RESOLVED"))
            print("| S%02d | %s | %s | %s |" % (scene, name, " | ".join(cells), shape))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
