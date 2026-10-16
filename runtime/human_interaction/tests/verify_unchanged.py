# -*- coding: utf-8 -*-
"""对照 §2 的基线清单，把「这次到底动了哪些文件」算出来。

主证明（闭合、可下判决）：基线清单里那 4088 个路径，今天重新算 SHA256。
这个全集就是基线文件自己定义的那一份，不掺外面的东西，所以
「改动 = 预期那几个、删除 = 0」是一句能负责的话。

副证明（只能降档）：整棵树里「新增」了什么，用不了。基线那份产物
没有把它的排除规则写进去（实测它漏掉了 runtime/core/venv_native 那 4980 个文件，
也没走根目录的 evidence/、logs/）。所以这一栏只报「在本任务写过的目录下」出现了什么，
其余一律标 NOT_ATTRIBUTABLE，不假装是干净。

排除集合是从基线的键集反推出来的，见 UNDER_SCAN；跑之前先把它印出来，
这样以后基线换了口径，这里对不上会当场暴露，而不是静默把别人家的文件算成新增。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
BASELINE = ROOT / "runtime" / "human_interaction_baseline_files.json"

# 与基线同一口径：这些扩展名、这些目录不进
EXTENSIONS = {".py", ".json", ".ps1", ".js", ".sh", ".html", ".mjs", ".css", ".cmd"}
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "venv_native"}
# 本任务有权新建文件的目录（副证明只在这里下结论）
OWNED_PREFIXES = ("runtime/human_interaction",)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for block in iter(lambda: src.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def scan(hash_only: set[str] | None = None) -> dict[str, str]:
    """走一遍树。hash_only 给了就只算那份的名字，其余只记「存在」——
    主证明要的是基线那 4088 个的哈希，没必要把一万个文件全算一遍。"""
    found: dict[str, str] = {}
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [one for one in dirs if one not in SKIP_DIRS and not one.startswith(".git")]
        for name in files:
            path = Path(base) / name
            if path.suffix.lower() not in EXTENSIONS:
                continue
            try:
                rel = path.relative_to(ROOT).as_posix()
            except ValueError:
                continue
            if hash_only is not None and rel not in hash_only:
                found[rel] = "EXISTS"
                continue
            try:
                found[rel] = sha256(path)
            except OSError:
                found[rel] = "UNREADABLE"
    return found


def compare(baseline: dict[str, Any], current: dict[str, str]) -> dict[str, Any]:
    # 键一律转正斜杠：基线里若混着反斜杠，直接比会把每个文件都判成「删除」
    old = {str(key).replace("\\", "/"): value for key, value in baseline["files"].items()}
    changed = [{"path": rel, "before": old[rel]["sha256"], "after": current[rel]}
               for rel in old if rel in current and current[rel] != old[rel]["sha256"]]
    deleted = sorted(rel for rel in old if rel not in current)
    added_all = sorted(rel for rel in current if rel not in old)
    added_owned = [one for one in added_all if one.startswith(OWNED_PREFIXES)]
    return {
        "universe_primary": len(old),
        "universe_rescan": len(current),
        "scope_note": ("主证明只对基线那 %d 个路径下判决；副证明的新增只在 %s 里下结论，"
                       "其余 %d 个新增文件属于基线口径没覆盖的目录，标 NOT_ATTRIBUTABLE，"
                       "不据此说『什么都没多出来』。")
                      % (len(old), "/".join(OWNED_PREFIXES), len(added_all) - len(added_owned)),
        "changed": sorted(changed, key=lambda one: one["path"]),
        "deleted": deleted,
        "added_in_owned_dirs": added_owned,
        "added_outside_owned_count": len(added_all) - len(added_owned),
    }


def main() -> int:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    wanted = {str(one).replace("\\", "/") for one in baseline["files"]}
    result = compare(baseline, scan(hash_only=wanted))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    verdict = "PASS" if not result["deleted"] else "FAIL"
    print("DELETED_COUNT=%d VERDICT=%s" % (len(result["deleted"]), verdict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
