# -*- coding: utf-8 -*-
"""§6.14 的补一节（纯追加、幂等）：接线**要重启 :7860 才算生效**，这是现场量出来的。

跑法：
    runtime/core/venv/Scripts/python.exe runtime/tests/fa_append_master_sola_v2_ember_0927b.py

为什么单独一支而不是回去改 §6.14：那一节已经落盘，回去改就是「改历史小节」，
本仓库的交接纪律是纯追加。

口径跟 fa_append_master_sola_v2_ember_0927.py 一致：先备份 → 前缀 + 零删除双闸门 →
写完读回来对一遍。
"""
from __future__ import annotations

import datetime
import difflib
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "runtime" / "llm" / "AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_HANDOFF.md"
STATE = ROOT / "runtime" / "llm" / "AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_STATE.json"
MARK = "<!-- SOLA_V2_EMBER_APPEND_v1b -->"
SUFFIX = ".bak_solaemberb_0927"
ANCHOR = '"handoff_section": "§6.14",'
NEW_FIELD = ('"effective_now": "还没生效 —— 要重启 :7860 才会把接线后的装配模块装进内存'
             '（本轮按 §7 明令没重启、没动 :8090）",\n      '
             '"restart_entry": "scripts/Start-AI-Girlfriend.ps1（pwsh 7），'
             '成功判据看 logs/runtime/startup.log 的 START_NATIVE_OK",')


def probed() -> dict[str, object]:
    """现场量一次「在听的那个进程是不是比接线早」，写完还能重跑复核。"""
    import psutil
    out: dict[str, object] = {"pids": [], "结论": "读不到监听进程"}
    ports = {7860: "控制面/装配所在的进程", 8090: "正式回复链路"}
    for port, label in ports.items():
        pids = sorted({c.pid for c in psutil.net_connections(kind="tcp")
                       if c.laddr and c.laddr.port == port and c.status == "LISTEN"
                       and c.pid})
        rows = []
        for pid in pids:
            try:
                proc = psutil.Process(pid)
                rows.append({"pid": pid,
                             "起于": datetime.datetime.fromtimestamp(
                                 proc.create_time()).strftime("%Y-%m-%d %H:%M:%S"),
                             "命令行": " ".join(proc.cmdline())[:200],
                             "带 --reload": "--reload" in " ".join(proc.cmdline())})
            except Exception as exc:
                rows.append({"pid": pid, "读不到": type(exc).__name__})
        out["pids"] = out["pids"] if pids else out["pids"]
        out[f":{port} {label}"] = rows or "没在听"
    files = {}
    for rel in ("runtime/character/context_builder.py",
                "runtime/core/providers/contracts.py",
                "runtime/character/sola_v2_ember/adapter.py"):
        files[rel] = datetime.datetime.fromtimestamp(
            (ROOT / rel).stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    out["接线文件落盘时刻"] = files
    main_rows = out.get(":7860 控制面/装配所在的进程") or []
    if isinstance(main_rows, list) and main_rows and isinstance(main_rows[0], dict):
        newest = min(files.values())
        out["结论"] = ("进程起于 {}，接线文件最早落盘于 {} ⇒ {}".format(
            main_rows[0].get("起于"), newest,
            "进程比接线早 ⇒ 重启之前用不到新装配" if str(main_rows[0].get("起于")) < newest
            else "进程已经带着接线在跑"))
    return out


SECTION = """

### 6.14.5 追加（同一轮，量完才看见）— 接线**要重启 :7860 才算生效**

@@MARK@@

这一条是「本轮没量到」里最要紧的一件，单独立一节免得被当成已经在跑：

* 控制面 `:7860` 的那个进程 **不带 ``--reload``**（Python 直接跑 ``uvicorn``），
  所以它内存里的 ``context_builder`` / ``contracts`` 还是接线之前那一份。
  **重启之前，界面上和实时链路上给她看的那一块里不会出现作者的规则块** ——
  开关拨到 `true` 只改磁盘上那份配置，改不了别人已经导入的模块。
* 生效办法只用合规入口：`scripts/Start-AI-Girlfriend.ps1`（pwsh 7）。
  成功判据看 `logs/runtime/startup.log` 里的 `START_NATIVE_OK backend=<x>`，
  **不看调用侧管道有没有结束**。本轮按 §7「量完即停」没有重启，也没有去动 `:8090`。
* 另外 `:8090`（正式回复链路）现场探针是**没在听**，所以 §8 的人工文字对话验收
  要先把回复链路起来，否则发消息不会有人回。

复算办法（只读，随时可重跑）：
`runtime/core/venv/Scripts/python.exe runtime/tests/fa_append_master_sola_v2_ember_0927b.py --probe`
—— 它用 psutil 枚举监听进程的「起于/命令行/是否 --reload」，与三个接线文件的 mtime 比先后。

本轮量到的结果：

```text
@@PROBE@@
```

所以下一个 Agent 读到 §6.14 时，**别把「十六键全绿」理解成「她已经在按 Sola 说话」**：
那十六键量的是装配引擎这一层的形状与来路，进程里跑的是哪一份代码要现场再量一次。
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def render_section() -> str:
    measured = probed()
    pretty = json.dumps(measured, ensure_ascii=False, indent=2)
    return SECTION.replace("@@MARK@@", MARK).replace("@@PROBE@@", pretty)


def main() -> int:
    if "--probe" in sys.argv[1:]:
        print(json.dumps(probed(), ensure_ascii=False, indent=2))
        return 0
    print("=" * 70)
    print("补 §6.14.5（重启才算生效）—— 纯追加 + 幂等")
    print("=" * 70)
    doc_before = DOC.read_bytes()
    for original in (DOC, STATE):
        copy = Path(str(original) + SUFFIX)
        if copy.exists():
            print(f"  备份已存在，不覆盖：{copy.name}")
        else:
            shutil.copy2(original, copy)
            print(f"  已备份 → {copy.name}")
    doc_text = doc_before.decode("utf-8")
    if MARK in doc_text:
        print("  主文档：已经补过这一节（看见标记了），不动")
        result = doc_text
    else:
        result = doc_text.rstrip("\n") + "\n" + render_section()
    if not result.startswith(doc_text):
        print("  前缀检查失败 ⇒ 回滚")
        DOC.write_bytes(doc_before)
        return 1
    removed = [one for one in difflib.unified_diff(doc_text.splitlines(),
                                                   result.splitlines(), lineterm="")
               if one.startswith("-") and not one.startswith("---")]
    if removed:
        print(f"  出现 {len(removed)} 行删除 ⇒ 回滚：{removed[:3]}")
        DOC.write_bytes(doc_before)
        return 1
    added = [one for one in difflib.unified_diff(doc_text.splitlines(),
                                                 result.splitlines(), lineterm="")
             if one.startswith("+") and not one.startswith("+++")]
    DOC.write_text(result, encoding="utf-8", newline="\n")
    print(f"  主文档：{len(doc_text.splitlines())} 行 → {len(result.splitlines())} 行"
          f"（新增 {len(added)} 行，删除 0 行）")

    state_before = STATE.read_bytes()
    state_text = state_before.decode("utf-8")
    parsed = json.loads(state_text)
    key = "sola_v2_ember_direct_port_20260927"
    if "effective_now" in state_text:
        print("  状态文件：那条字段已经在了，不动")
    elif ANCHOR not in state_text:
        print("  状态文件：找不到插入锚点（§6.14 那一节还没写？），这一项没做")
    else:
        candidate = state_text.replace(ANCHOR, ANCHOR + "\n      " + NEW_FIELD, 1)
        try:
            after = json.loads(candidate)
        except Exception as exc:
            print(f"  状态文件：插入后解析不了（{exc}）⇒ 回滚")
            STATE.write_bytes(state_before)
            return 1
        if set(after) != set(parsed) or len(json.dumps(after, sort_keys=True)) <= \
                len(json.dumps(parsed, sort_keys=True)):
            print("  状态文件：键集合或内容不对 ⇒ 回滚")
            STATE.write_bytes(state_before)
            return 1
        gone = [one for one in difflib.unified_diff(state_text.splitlines(),
                                                    candidate.splitlines(), lineterm="")
                if one.startswith("-") and not one.startswith("---")]
        if gone:
            print(f"  状态文件：出现删除行 {gone[:2]} ⇒ 回滚")
            STATE.write_bytes(state_before)
            return 1
        STATE.write_text(candidate, encoding="utf-8", newline="\n")
        print(f"  状态文件：就地加了 2 行字段（{key}.effective_now / restart_entry），删除 0 行")

    doc_after = DOC.read_text(encoding="utf-8")
    line = next((index for index, one in enumerate(doc_after.splitlines(), 1)
                 if one.startswith("### 6.14.5")), 0)
    state_after = json.loads(STATE.read_text(encoding="utf-8-sig"))
    ok = bool(line) and "effective_now" in json.dumps(state_after, ensure_ascii=False)
    print("-" * 70)
    print(f"  读回：6.14.5 在主文档第 {line} 行；标记在位={MARK in doc_after}")
    print(f"  读回：状态文件里 effective_now 在位="
          f"{'effective_now' in json.dumps(state_after, ensure_ascii=False)}")
    print(f"  读回：主文档 SHA {sha256(DOC)[:16]}…，状态文件 SHA {sha256(STATE)[:16]}…")
    print("结果：" + ("已补上" if ok else "没写成"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
