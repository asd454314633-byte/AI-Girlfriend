# -*- coding: utf-8 -*-
"""V2 开工基线：把「动手之前」这一秒的状态一次落盘。

为什么还要再冻一次：V1 那份基线（HUMAN_INTERACTION_BASELINE.json）记的是
「真人互动层存在之前」的世界。V2 要改的是本层内部，diff 必须从**现在**算起，
否则每一处 V1 已经验收过的改动都会被 V2 重复算成新改动。

只读盘 + 只读 HTTP GET + 只读进程枚举。不发模型请求，不写任何生产状态。
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
LAYER = ROOT / "runtime" / "human_interaction"
OUT = ROOT / "runtime" / "human_interaction_v2_baseline.json"
# 一轮新的窄授权开工前要另落一份（V2b＝"只压判断块形状"那一轮）。默认名字不改，
# 老那一轮的 diff 起点不能被后来者覆盖，所以这里只加一个 --out 参数。
DEFAULT_OUT = OUT
V1_BASELINE = ROOT / "HUMAN_INTERACTION_BASELINE.json"
POINTER = ROOT / "runtime" / "character" / "data" / "active_character.json"
SOURCE_EXT = {".py", ".txt", ".json"}
ARTIFACT_TOKENS = ("tests/out", "tests/shadow", "__pycache__")
PORTS = (7860, 8090, 8765, 8010)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def layer_files() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path in sorted(LAYER.rglob("*")):
        if not path.is_file():
            continue
        name = rel(path)
        if path.suffix not in SOURCE_EXT or any(t in name for t in ARTIFACT_TOKENS):
            continue
        out[name] = {"sha256": sha(path), "bytes": path.stat().st_size}
    return out


def v1_watched_now() -> dict[str, Any]:
    """V1 那份 19 个 watched 文件此刻的哈希——用来证明 V2 起点仍然只有
    context_builder.py 一处生产改动。"""
    if not V1_BASELINE.exists():
        return {"available": False}
    watched = (json.loads(V1_BASELINE.read_text(encoding="utf-8"))
               .get("file_inventory") or {}).get("watched") or {}
    out: dict[str, Any] = {}
    for name, before in watched.items():
        key = str(name).replace("\\", "/")
        path = ROOT / key
        now = sha(path) if path.exists() else "MISSING"
        out[key] = {"v1_before": before, "now": now, "same_as_v1_before": now == before}
    return out


def listeners() -> dict[str, Any]:
    try:
        import psutil
    except Exception as exc:                                   # noqa: BLE001
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    found: dict[str, Any] = {}
    for conn in psutil.net_connections(kind="inet"):
        port = int(getattr(conn.laddr, "port", 0) or 0)
        if conn.status == "LISTEN" and port in PORTS and conn.pid:
            found.setdefault(str(port), set()).add(int(conn.pid))
    out: dict[str, Any] = {"available": True}
    for port, pids in sorted(found.items()):
        rows = []
        for pid in sorted(pids):
            try:
                proc = psutil.Process(pid)
                rows.append({"pid": pid,
                             "created": time.strftime(
                                 "%Y-%m-%dT%H:%M:%S%z", time.localtime(proc.create_time())),
                             "argv_sha256": sha_text("\0".join(proc.cmdline())),
                             "argv_head": " ".join(proc.cmdline())[:400]})
            except Exception as exc:                           # noqa: BLE001
                rows.append({"pid": pid, "error": f"{type(exc).__name__}: {exc}"})
        out[port] = rows
    return out


def http_get_json(url: str) -> dict[str, Any]:
    started = time.time()
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            raw = resp.read()
        return {"status": resp.status, "wall_s": round(time.time() - started, 2),
                "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    except Exception as exc:                                   # noqa: BLE001
        return {"status": None, "error": f"{type(exc).__name__}: {exc}"}


def prompt_cell() -> dict[str, Any]:
    """当前人物此刻的整块提示词：字符数与哈希（V2 之后必须还能一模一样地拿回来）。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:7860/api/character/prompt?q=",
                                    timeout=20) as resp:
            row = json.loads(resp.read())
    except Exception as exc:                                   # noqa: BLE001
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    text = str(row.get("内容") or "")
    return {"available": True, "ok": bool(row.get("ok")), "编号": row.get("编号"),
            "人物名称": row.get("人物名称"), "chars": len(text),
            "sha256": sha_text(text), "reason": row.get("reason", "")}


def main() -> int:
    from runtime.human_interaction import config
    tag, out = "V2_FREEZE", DEFAULT_OUT
    args = sys.argv[1:]
    if "--out" in args:
        at = args.index("--out")
        if at + 1 >= len(args):
            print("用法：--out <文件名>（不带它仍写默认那份，别改默认名）")
            return 2
        raw = args[at + 1]
        out = (ROOT / raw) if not Path(raw).is_absolute() else Path(raw)
        tag = "V2B_FREEZE" if "v2b" in Path(raw).name.lower() else "V2_FREEZE"
    payload = {
        "task": "HUMAN_INTERACTION_LAYER_01/" + tag,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "why": ("V2b＝只压判断块的形状与注入成本这一轮的 diff 起点；"
                if tag == "V2B_FREEZE" else
                "V2 只改本层内部；diff 从这份起算，V1 基线继续负责『层存在之前』那一段。"),
        "layer_source_files": layer_files(),
        "layer_source_count": len(layer_files()),
        "v1_watched_now": v1_watched_now(),
        "listeners": listeners(),
        "prompt_cell": prompt_cell(),
        "http_probe_8090_models": http_get_json("http://127.0.0.1:8090/v1/models"),
        "active_character_pointer": (POINTER.read_text(encoding="utf-8")
                                     if POINTER.exists() else "MISSING"),
        "layer_effective_config": config.describe_config(),
        "layer_config_file_sha": (sha(config.CONFIG_PATH) if config.CONFIG_PATH.exists()
                                  else "MISSING"),
    }
    if out.exists():
        # 基线件是"动手之前那一秒"的记录，覆盖等于把上一轮的 diff 起点抹掉。
        print("REFUSING_TO_OVERWRITE", out, "——已存在的冻结件不许重刷，换名字")
        return 3
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("LAYER_FILES", payload["layer_source_count"])
    print("POINTER", payload["active_character_pointer"])
    print("PROMPT_CELL", json.dumps(payload["prompt_cell"], ensure_ascii=False))
    print("LISTENERS", {k: [r.get("pid") for r in v] for k, v in
                        payload["listeners"].items() if isinstance(v, list)})
    changed = [k for k, v in payload["v1_watched_now"].items()
               if isinstance(v, dict) and not v.get("same_as_v1_before")]
    print("PROD_FILES_ALREADY_DIFFERENT_FROM_V1_PREWORK", changed)
    print("EFFECTIVE_ENABLED", payload["layer_effective_config"]["effective_enabled"],
          "MODE", payload["layer_effective_config"]["effective_mode"])
    print("OUT", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
