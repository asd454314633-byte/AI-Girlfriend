# -*- coding: utf-8 -*-
"""TTS segmentation strategy tests (A/B/C) - pure text, no GPU, no services.

Run: runtime/core/venv/Scripts/python.exe runtime/tests/test_tts_segmentation_strategy.py
Covers the 2026-10-04 ruling: B is the production default; A is the untouched
baseline; C is whole-text single generation.  Top-level mode system untouched.
"""
LEGACY_TEST_SCOPE = 'INSUFFICIENT'
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "runtime" / "core"))

from providers.adapters.qwen3_tts_adapter import resolve_segmentation_strategy
from providers.voice_text import merge_tiny_segments, pronounceable_voice_segments

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (name, detail))


def stripped_len(segment):
    return len(segment.rstrip("。！？!?…；;：:，,、”“’'\""))


print("== merge_tiny_segments ==")
check("empty list", merge_tiny_segments([]) == [])
check("single tiny segment kept", merge_tiny_segments(["二。"]) == ["二。"])

# trailing tiny segment joins the previous unit
out = merge_tiny_segments(["他说完了。", "二。"])
check("trailing tiny joins previous", out == ["他说完了。二。"], repr(out))

# consecutive tiny segments pair up forward (does not occur in real books)
out = merge_tiny_segments(["一", "二", "正文来了。"])
check("consecutive tiny forward pair", out == ["一二", "正文来了。"], repr(out))

# real production plan: chapter "二" material from chunks.json index 2..8
MATERIAL = [
    "那年秋天，海风比往年来得更早。灯塔下的石板路整日湿着，苔藓一直爬到第三级台阶。老周把最后一箱行李搬上车的时候，雨正好停了。他站了一会儿，回头看了一眼塔顶——那盏灯白天是不亮的，可他还是看了一会儿，像是在确认什么。",
    "二",
    "“真走了？”我问他。",
    "“真走了。”他说得很轻，“医生说，再待下去，这双耳朵就彻底聋了。”",
    "我这才知道，过去三年，他每晚要听的并不是海浪，是机器。那台老旧的雾笛压缩机日夜转着，声音像有人贴在他耳边不停地敲铁。他试过塞棉花，试过把值班室的门关死，最后连收音机里的说话声都听不见了。",
    "“那灯呢？”我问。",
    "“灯没问题。”他笑了笑，“灯不需要耳朵。”",
]
plan_a = []
for chunk in MATERIAL:
    plan_a.extend(pronounceable_voice_segments(chunk, max_chars=96))
check("A plan = 8 segments (matches experiment)", len(plan_a) == 8, "got %d" % len(plan_a))
check("A plan keeps the lone chapter number", any(stripped_len(s) <= 2 for s in plan_a))

plan_b = merge_tiny_segments(plan_a)
check("B plan = 7 units (matches experiment)", len(plan_b) == 7, "got %d" % len(plan_b))
check("B plan unit 3 = chapter+first sentence",
      plan_b[2] == "二。“真走了？”我问他。", repr(plan_b[2]))
check("B plan has no tiny unit left", all(stripped_len(s) >= 3 for s in plan_b))
check("B preserves all text (concat equal)",
      "".join(plan_b) == "".join(plan_a))

print("== resolve_segmentation_strategy ==")
check("missing file -> default B",
      resolve_segmentation_strategy(Path(tempfile.gettempdir()) / "definitely_missing_strategy.txt") == "B")
with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp) / "strategy.txt"
    p.write_text("a\n", encoding="utf-8")
    check("lowercase a -> A", resolve_segmentation_strategy(p) == "A")
    p.write_text("C", encoding="utf-8")
    check("C -> C", resolve_segmentation_strategy(p) == "C")
    p.write_text("X", encoding="utf-8")
    check("invalid -> default B", resolve_segmentation_strategy(p) == "B")
    p.write_text("", encoding="utf-8")
    check("empty -> default B", resolve_segmentation_strategy(p) == "B")
real = resolve_segmentation_strategy()
check("production control file currently B", real == "B", "got %r" % real)

print()
print("TOTAL pass=%d fail=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
