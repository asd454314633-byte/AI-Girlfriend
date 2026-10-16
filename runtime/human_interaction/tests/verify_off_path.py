# -*- coding: utf-8 -*-
"""§22 那句 OFF_PATH_IDENTICAL_TO_BASELINE 的可重跑证明。

上一轮这个数是临时命令算出来的，没落成脚本，等于「说过就信了」。这里补成
以后任何时候都能重算的一道闸。它证两件不同的事，缺一件都不算：

  A. **接线本身在关掉时不贡献任何一个字。**
     把 context_builder._attach_interaction 换成「原样退回」——这就是「这一层从来没接进来」
     的形状——然后和「接进来了但整层关着」逐字节对拍。
     只比"关掉 vs 打开"证不了这一条：那种比法默认接线是干净的。

  B. **摘掉中间插入那段，剩下的必须逐字等于关掉那份。**
     这条由 bench_shadow.assemble_as 的前后缀对拍负责，它不等就抛 AssertionError。
     这里顺手把 12 个场景在三种档下都跑一遍，让它必须不抛。

对照一律在影子库上做：生产库、生产指针、生产配置一概不写。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.character import context_builder  # noqa: E402
from runtime.human_interaction.tests import bench_scenarios, bench_shadow  # noqa: E402

OUT_PATH = HERE / "out" / "off_path_check.json"


def system_block(*, neutralized: bool, history: Any = (), user_text: str = "") -> str:
    """走生产那一条装配路径拿系统块；neutralized=True 时假装这一层从没接进来。"""
    original = context_builder._attach_interaction
    if neutralized:
        context_builder._attach_interaction = lambda base, **kw: base
    try:
        text, _meta = bench_shadow.assemble(history=history, user_text=user_text)
    finally:
        context_builder._attach_interaction = original
    return text


def main() -> int:
    bench_shadow.build_shadow(rebuild=True)
    from runtime.human_interaction import config
    print("磁盘开关状态（必须关着）:", config.enabled(), config.mode())

    rows = []
    all_same = True
    for scene in bench_scenarios.SCENARIOS:
        history = scene.get("history") or ()
        user_text = str(scene.get("user_text") or "")
        wired_off = system_block(neutralized=False, history=history, user_text=user_text)
        never_wired = system_block(neutralized=True, history=history, user_text=user_text)
        same = wired_off == never_wired
        all_same = all_same and same
        # B 项：三种档都跑一遍，前后缀对拍不等就会在这里抛
        for arm in (None, "human_natural", "autonomous", "story_director"):
            bench_shadow.assemble_as(arm, history=history, user_text=user_text)
        rows.append({
            "scenario": scene.get("no"),
            "requirement": scene.get("requirement"),
            "sha_wired_off": hashlib.sha256(wired_off.encode("utf-8")).hexdigest(),
            "sha_never_wired": hashlib.sha256(never_wired.encode("utf-8")).hexdigest(),
            "chars": len(wired_off),
            "identical": same,
        })
        print("S%02d %-30s %4d字  %s  %s" % (
            scene.get("no"), str(scene.get("requirement"))[:30], len(wired_off),
            "IDENTICAL" if same else "DIFFER", rows[-1]["sha_wired_off"][:12]))

    verdict = "YES" if all_same else "NO"
    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps(
        {"checked_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
         "scenarios": len(rows), "identical_count": sum(1 for one in rows if one["identical"]),
         "splice_check": "assemble_as 对 12 场景 × 4 档逐字对拍，未抛 AssertionError",
         "OFF_PATH_IDENTICAL_TO_BASELINE": verdict, "rows": rows},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print("OFF_PATH_IDENTICAL_TO_BASELINE = %s (%d/%d)"
          % (verdict, sum(1 for one in rows if one["identical"]), len(rows)))
    print("EVIDENCE_OUT", OUT_PATH)
    return 0 if all_same else 1


if __name__ == "__main__":
    raise SystemExit(main())
