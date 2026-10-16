"""evidence_readout.py — 把本轮证据文件里的关键数字摊成一页，供人工复核。

为什么单独写文件而不是 python -c：跨 shell 传参里嵌套引号/箭头会被静默改写，
一旦改成"看起来跑了其实跑歪"的语句，读出来的数字就不再是磁盘上的数字。
"""
import json
import sys
from pathlib import Path

# 本文件在 zero_replica/runtime/tests/ui_dual_viewmode_01/ 下：
# parents[0]=本目录 [1]=tests [2]=runtime [3]=zero_replica —— 取 [3] 再拼 runtime
EV = Path(__file__).resolve().parents[3] / "runtime" / "evidence" / "UI_DUAL_VIEWMODE_01"

m = json.loads((EV / "SCREENSHOT_MANIFEST.json").read_text(encoding="utf-8"))
print("generated_at      ", m["generated_at"])
print("capture_channel   ", m["capture_channel"])
print("live_session      ", m.get("live_session_established"))
for k in ("capture_gate_before", "capture_gate_after"):
    g = m.get(k) or {}
    print(f"{k:18}", g.get("VERDICT"),
          "magenta=", g.get("magenta_ratio"),
          "dims=", g.get("dims"), "bytes=", g.get("bytes"),
          "notblack_before=", (g.get("notblack_ratio") or {}).get("before"))
for n, s in m["shots"].items():
    g, a = s["geom"], s["pixel_audit"]
    print(f"\n{n}")
    print("   viewMode   ", g["viewMode"], " pane=", g["pane"], " stageRatio=", g["stageRatio"],
          " objectFit=", g["objectFit"])
    print("   video      ", g["video"], " layer=", g["layer"])
    print("   app        ", g["app"], " panel=", g["panel"])
    print("   currentTime", g["currentTime"], " srcObject=", g["srcObject"], " lv=", g["lv"])
    print("   store      ", json.dumps(g["store"], ensure_ascii=False))
    print("   pixel_audit", "sha=", a["sha16"], "bytes=", a["bytes"],
          "magenta_residue=", a["magenta_residue"],
          "inVideo_notblack=", a["notblack_in_video"]["ratio"],
          "panel_bright=", a["bright_in_panel"]["ratio"],
          "lum=", a["luminance"], a["VERDICT"])
    print("   checks     ", json.dumps(a["checks"], ensure_ascii=False))
print("\nmedia_continuity ", json.dumps(m.get("media_continuity"), ensure_ascii=False))
print("negative_control ", json.dumps(m.get("negative_control_module_disabled"), ensure_ascii=False)[:420])
print("VISUAL_GATE      ", (m.get("VISUAL_GATE") or {}).get("VERDICT"))
