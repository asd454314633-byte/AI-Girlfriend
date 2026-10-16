# -*- coding: utf-8 -*-
"""§20 生产文件 Diff 审计：把"我只改了显示层"从口头承诺变成可复算的算术。

三道判据（缺一不可）：
1) 冻结面逐个 SHA256 对账：32 个 §0 点名禁改文件 + 235 个后端源文件，
   PRE/POST 两次台账必须一字不差；并且【现场再算一遍】，不信任台账抄的哈希
   （PRE 抄错 + POST 抄成同一个错值，也能让 PRE/POST 相减通过）。
2) index.html 的改动要能【逐行还原成改造前】：把我加的行删掉之后，
   剩下的字节流必须与 PRE 台账记录的 sha256 完全相同。
   这条比"我看了一下，只加了标签"强得多 —— 它排除了同文件内的任何顺手改动。
3) 进程世代：PID + 创建时间 + 命令行 + 监听端口，PRE/POST 必须同一批进程
   （§0"不得重启 8010/8090/8765"用文件哈希证不了，只有进程世代能证）。

另外把静态根里"本任务时间窗内新建的文件"列出来，用来兑现
"禁止在生产 UI 目录制造 *.bak / *.old / *.copy"这一条。
"""
import datetime
import hashlib
import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2].parent
APP = REPO / "runtime" / "ui" / "app"
EV = REPO / "runtime" / "evidence" / "UI_DUAL_VIEWMODE_01"
# 本任务的时间起点：PRE 证据目录创建于 2026-09-26 11:03（现场 ls 得到）。
# 早于它的备份属于别的任务（longaudio / layout02 / tsscp01 …），只登记不处置。
TASK_WINDOW_START = "2026-09-26 10:30:00"
UI_ALLOW = {"index.html", "ui_layout_modes.css", "ui_layout_modes.js"}
ANCHORS = (b"AI_GIRLFRIEND_UI_DUAL_VIEWMODE_01", b"UI_DUAL_VIEWMODE_01")
TAG = b"ui_layout_modes"


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def load(phase):
    f = EV / ("PRE_CHANGE_MANIFEST.json" if phase == "prechange"
              else "POST_CHANGE_MANIFEST.json")
    return json.loads(f.read_text(encoding="utf-8"))


def rows(m, key):
    return {r["file"]: r for r in m[key]}


def main():
    pre, post = load("prechange"), load("postchange")
    pre_ui, post_ui = rows(pre, "uiApp"), rows(post, "uiApp")
    pre_be, post_be = rows(pre, "backend"), rows(post, "backend")

    # ---- 1) 冻结面 ----
    frozen_files = sorted(f for f, r in pre_ui.items() if r["cls"] == "FROZEN")
    frozen_bad = [f for f in frozen_files if pre_ui[f]["sha256"] != post_ui[f]["sha256"]]
    frozen_drift = [f for f in frozen_files if sha(APP / f) != pre_ui[f]["sha256"]]
    backend_bad = [f for f in sorted(pre_be) if pre_be[f]["sha256"] != post_be[f]["sha256"]]
    backend_drift = [f for f in sorted(pre_be) if sha(REPO / f) != pre_be[f]["sha256"]]

    # ---- 2) index.html 逐行还原 ----
    raw = (APP / "index.html").read_bytes().split(b"\n")
    drop = set()
    i = 0
    while i < len(raw):
        line = raw[i]
        mine = any(a in line for a in ANCHORS) or TAG in line
        if mine and b"<!--" in line:
            j = i
            while j < len(raw) and b"-->" not in raw[j]:
                j += 1
            end = j if j < len(raw) else i
            if end + 1 < len(raw) and TAG in raw[end + 1]:
                end += 1                      # 注释块后面紧跟的本任务标签行
            drop.update(range(i, end + 1))
            i = end + 1
            continue
        if mine:
            drop.add(i)
        i += 1
    recon = b"\n".join(l for k, l in enumerate(raw) if k not in drop)
    pre_hash = pre_ui["index.html"]["sha256"]
    index_reconstructs = hashlib.sha256(recon).hexdigest() == pre_hash

    added = sorted(set(post_ui) - set(pre_ui))
    removed = sorted(set(pre_ui) - set(post_ui))
    changed = sorted(f for f in set(pre_ui) & set(post_ui)
                     if pre_ui[f]["sha256"] != post_ui[f]["sha256"])
    ui_scope = sorted(f for f, r in pre_ui.items() if r["cls"] == "UI_SCOPE")

    # ---- 3) 时间窗内新建的文件 ----
    created_in_window = []
    for root, dirs, files in os.walk(APP):
        dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__")]
        for fn in files:
            p = Path(root) / fn
            rel = str(p.relative_to(APP)).replace("\\", "/")
            ct = datetime.datetime.fromtimestamp(p.stat().st_ctime).strftime("%Y-%m-%d %H:%M:%S")
            if ct >= TASK_WINDOW_START:
                created_in_window.append({"file": rel, "created": ct,
                                          "in_allow_list": rel in UI_ALLOW})
    created_in_window.sort(key=lambda r: r["file"])
    forbidden = [r["file"] for r in created_in_window
                 if any(s in r["file"] for s in (".bak", ".old", ".copy"))]
    unexpected = [r["file"] for r in created_in_window if not r["in_allow_list"]]

    # ---- 4) 进程世代 ----
    # 只比 listeners（PID + 创建时间 + 命令行 + cwd）；established_counts 是
    # "当前有几条 TCP 连接"的时间态观测值，本来就会变，拿它做恒等判据必假 FAIL。
    def pidmap(m):
        d = (m.get("processIdentity") or {}).get("data") or {}
        return {str(k): v for k, v in (d.get("listeners") or {}).items()}

    ppre, ppost = pidmap(pre), pidmap(post)
    proc_detail, proc_same = {}, True
    for port in sorted(set(ppre) | set(ppost)):
        x, y = ppre.get(port), ppost.get(port)
        same = json.dumps(x, sort_keys=True) == json.dumps(y, sort_keys=True)
        proc_detail[port] = {"pre": x, "post": y, "identical": same}
        proc_same = proc_same and same

    # 分类判据：不能用"创建时间落在任务窗口内"当"这是新文件"的依据 ——
    # 本机编辑器写文件走"临时文件 + rename"，把 Windows 的【创建时间】一起刷新了
    # （实测 index.html 的 ctime 变成了今天 13:07，而它显然不是新文件）。
    # 所以"改动 vs 新建"按下面的事实切：
    #   · index.html 属于【改动】：删掉我加的 8 行后，字节流与 PRE 台账里那份
    #     46166B / cc0866a3…（PRE 采样时 mtime=2026-09-25 18:07）逐字节相同，
    #     这直接证明它改造前就存在、且我只往里加了东西。
    #   · ui_layout_modes.{css,js} 属于【新建】：整个静态根里除了 index.html 那两行
    #     标签以外，没有任何既有文件引用它们（下面 referenced_by 现场扫出来的）。
    pre_existing_by_proof = ["index.html"] if index_reconstructs else []
    new_files = [f for f in changed if f not in pre_existing_by_proof]
    modified_ui = [f for f in changed if f in pre_existing_by_proof]
    non_ui_modified = [f for f in changed if f not in ui_scope] + frozen_bad + backend_bad

    # 反向引用扫描：谁在引用这两个新文件（只允许 index.html + 本任务测试目录）
    referrers = {}
    for target in new_files:
        hits = []
        for root, dirs, files in os.walk(REPO / "runtime"):
            dirs[:] = [d for d in dirs if d not in
                       ("node_modules", "__pycache__", "venv", "evidence")]
            for fn in files:
                if not fn.endswith((".html", ".js", ".css", ".py", ".json")):
                    continue
                p = Path(root) / fn
                try:
                    if target in p.read_text(encoding="utf-8", errors="ignore"):
                        hits.append(str(p.relative_to(REPO)).replace("\\", "/"))
                except OSError:
                    pass
        referrers[target] = sorted(hits)
    # 静态根内部（= 会被 7860 真正发出去的那批文件）里，除了它们自己，
    # 只允许 index.html 引用它们；测试目录里的引用不算生产耦合，单独列。
    new_paths = {f"runtime/ui/app/{t}" for t in new_files}
    refs_in_app = {t: [h for h in hs if h.startswith("runtime/ui/app/")
                       and h not in new_paths]
                   for t, hs in referrers.items()}
    refs_outside = {t: [h for h in hs if not h.startswith("runtime/ui/app/")]
                    for t, hs in referrers.items()}
    only_index_refs = all(v == ["runtime/ui/app/index.html"] for v in refs_in_app.values())

    gate = (not frozen_bad and not frozen_drift and not backend_bad and not backend_drift
            and not removed and not unexpected and not forbidden
            and index_reconstructs and proc_same and len(frozen_files) == 32
            and len(pre_be) == 235
            and new_files == ["ui_layout_modes.css", "ui_layout_modes.js"]
            and modified_ui == ["index.html"] and not non_ui_modified
            and only_index_refs)

    report = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "counts": {"frozen": len(frozen_files), "backend": len(pre_be),
                   "uiApp_files": len(pre_ui)},
        "FROZEN_SHA_MISMATCH": frozen_bad,
        "FROZEN_DRIFT_VS_DISK_NOW": frozen_drift,
        "BACKEND_SHA_MISMATCH": backend_bad,
        "BACKEND_DRIFT_VS_DISK_NOW": backend_drift,
        "uiApp_added_since_PRE_snapshot": added,
        "uiApp_removed": removed,
        "uiApp_changed": changed,
        "NEW_UI_FILES": new_files,
        "MODIFIED_UI_FILES": modified_ui,
        "NON_UI_MODIFIED_FILES": non_ui_modified or "NONE",
        "NEW_UI_FILES_referenced_by": {
            "inside_static_root": refs_in_app,
            "outside_static_root_tests_only": refs_outside,
            "ONLY_INDEX_HTML_REFERENCES_THEM": only_index_refs,
        },
        "NEW_UI_FILES_evidence": {
            "created_times": created_in_window,
            "ctime_caveat": "本机编辑器以 临时文件+rename 落盘，会刷新 Windows 创建时间；"
                            "实测 index.html 的 ctime 也是今天 ⇒ 创建时间只能用于"
                            "『任务窗口内有没有多出别的文件』，不能用于区分新建/改动。",
            "note": "PRE 台账生成于本任务写好这两个文件之后，所以 PRE/POST 相减证不了"
                    "『它们是新文件』；新文件身份由【除 index.html 那两行标签外无人引用】"
                    "与【?noviewmode=1 负对照下页面行为如旧】两件事共同支撑。",
        },
        "index_html_pre_sha256": pre_hash,
        "index_html_post_sha256": post_ui["index.html"]["sha256"],
        "index_html_lines_dropped_to_reconstruct_PRE": sorted(drop),
        "INDEX_HTML_CHANGE_IS_TAGS_ONLY": index_reconstructs,
        "forbidden_backup_artifacts_created_by_this_task": forbidden,
        "unexpected_files_created_in_window": unexpected,
        "process_identity": {"all_identical": proc_same, "per_port": proc_detail},
        "VERDICT": "PASS" if gate else "FAIL",
    }
    (EV / "DIFF_AUDIT.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for k in ("counts", "FROZEN_SHA_MISMATCH", "FROZEN_DRIFT_VS_DISK_NOW",
              "BACKEND_SHA_MISMATCH", "BACKEND_DRIFT_VS_DISK_NOW",
              "uiApp_changed", "uiApp_removed", "NEW_UI_FILES", "MODIFIED_UI_FILES",
              "INDEX_HTML_CHANGE_IS_TAGS_ONLY", "NON_UI_MODIFIED_FILES",
              "forbidden_backup_artifacts_created_by_this_task",
              "unexpected_files_created_in_window"):
        print(f"{k} = {json.dumps(report[k], ensure_ascii=False)}")
    print("created_in_window =", json.dumps(report["NEW_UI_FILES_evidence"]["created_times"],
                                            ensure_ascii=False))
    print("lines_dropped =", sorted(drop), "(0-based)",
          "| bytes recon", len(recon), "vs PRE", pre_ui["index.html"]["bytes"])
    print("process_identity.all_identical =", proc_same, "|",
          {p: [r.get("pid") for r in (proc_detail[p]["post"] or [])] for p in proc_detail})
    print("VERDICT =", report["VERDICT"])


if __name__ == "__main__":
    main()
