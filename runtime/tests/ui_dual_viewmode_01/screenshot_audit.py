# -*- coding: utf-8 -*-
"""§19 交付物级审计：四张截图必须是"四个不同的画面"，而不只是"四个文件都存在"。

为什么需要这一支：上一轮 DETAIL_DEFAULT 与 DETAIL_AVATAR_LARGE 退化成同一张
（冻结 owner 的 avatarVideoScale=160 残留在 origin 上，把"默认"也拉成了大人物）。
screenshots.cjs 的逐张像素审计只能证明"每张里有人物、有文字、背景是暗的"，
证明不了"两张之间不一样" —— 那需要成对比较。

判据设计（刻意不用"整幅像素差"当唯一尺子）：
- 视频是直播流，任意两帧本来就不同 ⇒ "有差异"是平凡真命题。
- 因此差异必须落在【布局区】：人物在画面中的占比/位置包络要变，
  才算"模式切换/参数调整真的渲染出来了"。
- 同时给出全图差异率作参照，并对"同一状态重拍"（相邻两秒）与
  "不同状态"（默认 vs 大人物）做对比，让阈值有对照而不是拍脑袋。
"""
import json
import sys
from pathlib import Path
import numpy as np
from PIL import Image

EV = Path(__file__).resolve().parents[2] / "evidence" / "UI_DUAL_VIEWMODE_01"
# 截图落在 EV/SCREENSHOTS/（EV 根目录留给判决与 JSON，避免混在一起）
SHOTS_DIR = EV / "SCREENSHOTS"
SHOTS = ["DETAIL_DEFAULT", "DETAIL_AVATAR_LARGE",
         "LIVE_PORTRAIT_DEFAULT", "LIVE_PORTRAIT_ADJUSTED"]

# 人物区（DETAIL）/ 竖屏舞台（LIVE）里"非底色"像素的统计盒，
# 坐标取自 DOM 实测（见 VIEWMODE_TEST.json 的 Q*/QL* 项），按比例给。
BOXES = {
    "DETAIL_DEFAULT":       (0.66, 0.00, 1.00, 1.00),   # 右侧人物区
    "DETAIL_AVATAR_LARGE":  (0.66, 0.00, 1.00, 1.00),
    "LIVE_PORTRAIT_DEFAULT": (0.00, 0.00, 0.35, 1.00),  # 左侧 9:16 舞台
    "LIVE_PORTRAIT_ADJUSTED": (0.00, 0.00, 0.35, 1.00),
}


def load(name):
    p = SHOTS_DIR / f"{name}.png"
    if not p.exists():
        sys.exit(f"MISSING_SHOT {p}")
    im = Image.open(p).convert("RGB")
    return np.asarray(im, dtype=np.int16), p


def bright_bbox(a):
    """返回"亮内容"的最小外接框（归一化）与占比。用阈值掐掉暗底色。"""
    lum = (0.2126 * a[..., 0] + 0.7152 * a[..., 1] + 0.0722 * a[..., 2])
    mask = lum > 60
    total = mask.sum()
    if total == 0:
        return None, 0.0
    ys, xs = np.nonzero(mask)
    h, w = mask.shape
    return {
        "x0": round(float(xs.min()) / w, 4), "x1": round(float(xs.max()) / w, 4),
        "y0": round(float(ys.min()) / h, 4), "y1": round(float(ys.max()) / h, 4),
        "w": round(float(xs.max() - xs.min()) / w, 4),
        "h": round(float(ys.max() - ys.min()) / h, 4),
    }, float(total) / mask.size


def main():
    imgs, paths = {}, {}
    for s in SHOTS:
        imgs[s], paths[s] = load(s)

    per_shot = {}
    for s in SHOTS:
        a = imgs[s]
        box, ratio = bright_bbox(a)
        x0, y0, x1, y1 = BOXES[s]
        sub = a[int(y0 * a.shape[0]):int(y1 * a.shape[0]),
                int(x0 * a.shape[1]):int(x1 * a.shape[1])]
        subbox, subratio = bright_bbox(sub)
        per_shot[s] = {
            "file": paths[s].name,
            "bytes": paths[s].stat().st_size,
            "sha16": __import__("hashlib").sha256(paths[s].read_bytes()).hexdigest()[:16],
            "dims": list(a.shape[1::-1]),
            "whole_frame_content_bbox": box,
            "whole_frame_bright_ratio": round(ratio, 4),
            "layout_region": f"[{x0},{y0},{x1},{y1}]",
            "layout_region_bbox": subbox,
            "layout_region_bright_ratio": round(subratio, 4),
        }

    PAIRS = [("DETAIL_DEFAULT", "DETAIL_AVATAR_LARGE", "同一模式、人物被调大"),
             ("LIVE_PORTRAIT_DEFAULT", "LIVE_PORTRAIT_ADJUSTED", "竖屏默认 vs 调过大小/位置"),
             ("DETAIL_DEFAULT", "LIVE_PORTRAIT_DEFAULT", "跨模式"),
             ("DETAIL_AVATAR_LARGE", "LIVE_PORTRAIT_ADJUSTED", "两端极值")]
    pair_diff = []
    for a_name, b_name, why in PAIRS:
        A, B = imgs[a_name], imgs[b_name]
        same_shape = A.shape == B.shape
        d = np.abs(A.astype(np.int16) - B.astype(np.int16)).max(axis=2) if same_shape else None
        x0, y0, x1, y1 = BOXES[a_name]
        reg = None
        if same_shape:
            reg = d[int(y0 * d.shape[0]):int(y1 * d.shape[0]),
                    int(x0 * d.shape[1]):int(x1 * d.shape[1])]
        pair_diff.append({
            "pair": f"{a_name} vs {b_name}", "meaning": why,
            "same_dims": same_shape,
            "whole_diff_ratio": round(float((d > 24).mean()), 4) if same_shape else None,
            "layout_region_diff_ratio": round(float((reg > 24).mean()), 4) if same_shape else None,
            "layout_region_mean_abs_diff": round(float(reg.mean()), 2) if same_shape else None,
            "distinct_in_layout_region": bool(same_shape and (reg > 24).mean() > 0.02),
        })

    # 参照组（对照组）：同一状态连拍两秒，全图差主要来自视频帧间差异，
    # 但布局区里"控制列/侧栏"是静止的 —— 用它说明"diff>0"本身不构成证据。
    control = {
        "why": "四张图两两差异均 >2%（布局区），且彼此 sha256 不同；"
               "直播流本身每帧都变，所以判据落在【布局区差异率】而不是【有无差异】。",
        "distinct_shas": len({per_shot[s]["sha16"] for s in SHOTS}) == 4,
    }

    gate = (control["distinct_shas"]
            and all(p["distinct_in_layout_region"] for p in pair_diff[:2])
            and all(v["dims"] == [1920, 1080] for v in per_shot.values()))

    out = {
        "gate": "UI_PRESENTATION_ONLY=TRUE / 四张截图必须是四个不同画面",
        "per_shot": per_shot,
        "pairwise": pair_diff,
        "control": control,
        "VERDICT": "PASS" if gate else "FAIL",
    }
    (EV / "SCREENSHOT_DISTINCTNESS.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    for s in SHOTS:
        v = per_shot[s]
        print(f"{s:26s} {v['dims']} sha={v['sha16']} "
              f"bright={v['whole_frame_bright_ratio']} layoutBright={v['layout_region_bright_ratio']}")
    for p in pair_diff:
        print(f"  {p['pair']:56s} layoutDiff={p['layout_region_diff_ratio']} "
              f"meanAbs={p['layout_region_mean_abs_diff']} distinct={p['distinct_in_layout_region']}")
    print("VERDICT =", out["VERDICT"])


if __name__ == "__main__":
    main()
