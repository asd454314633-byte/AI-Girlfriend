# -*- coding: utf-8 -*-
"""交付物清单：新增文件 / 修改文件 / 改前改后 SHA256（任务 §21 要的那几张表）。

只读盘上现状 + 基线里那份 watched 清单，不发任何模型请求、不碰任何服务。
判口径说明：
  * 「新增」= 位于 runtime/human_interaction/ 下、或本轮四份交付文档；
  * 「修改」= 基线 watched 清单里有、且现在哈希变了的那些；
  * __pycache__ / tests/out / tests/shadow 属于跑出来的产物，单列，不混进源码清单。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
LAYER = ROOT / "runtime" / "human_interaction"
BASELINE = ROOT / "HUMAN_INTERACTION_BASELINE.json"
OUT = HERE / "out" / "file_manifest.json"

DELIVERABLES = ("HUMAN_INTERACTION_DESIGN.md", "HUMAN_INTERACTION_BASELINE.json",
                "HUMAN_INTERACTION_TEST_REPORT.md", "HUMAN_INTERACTION_ROLLBACK.md")
SOURCE_EXT = {".py", ".txt", ".json"}
ARTIFACT_DIRS = ("tests/out", "tests/shadow", "__pycache__")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def is_artifact(name: str) -> bool:
    return any(token in name for token in ARTIFACT_DIRS)


def main() -> int:
    watched = json.loads(BASELINE.read_text(encoding="utf-8"))["file_inventory"]["watched"]

    layer_source: dict[str, Any] = {}
    layer_artifacts: dict[str, Any] = {}
    for path in sorted(LAYER.rglob("*")):
        if not path.is_file():
            continue
        name = rel(path)
        row = {"sha256": sha(path), "bytes": path.stat().st_size}
        if is_artifact(name) or path.suffix not in SOURCE_EXT:
            layer_artifacts[name] = row
        else:
            layer_source[name] = row

    deliverables = {}
    for name in DELIVERABLES:
        path = ROOT / name
        if path.exists():
            deliverables[name] = {"sha256": sha(path), "bytes": path.stat().st_size}

    modified = {}
    unchanged_watched = 0
    for name, before in watched.items():
        key = str(name).replace("\\", "/")
        path = ROOT / key
        if not path.exists():
            continue
        now = sha(path)
        if now != before:
            modified[key] = {"before": before, "after": now}
        else:
            unchanged_watched += 1

    payload = {
        "new_layer_source_files": layer_source,
        "new_layer_file_count": len(layer_source),
        "run_artifacts": sorted(layer_artifacts),
        "deliverables": deliverables,
        "modified_files": modified,
        "modified_file_count": len(modified),
        "watched_unchanged_count": unchanged_watched,
        "watched_total": len(watched),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print("新增（本层源码/文案/配置）%d 个" % len(layer_source))
    for name, row in layer_source.items():
        print("  %s  %s" % (row["sha256"][:16], name))
    print("修改 %d 个" % len(modified))
    for name, row in modified.items():
        print("  %s\n    改前 %s\n    改后 %s" % (name, row["before"][:16], row["after"][:16]))
    print("基线 watched 清单里未变 %d / %d" % (unchanged_watched, len(watched)))
    print("MANIFEST", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
