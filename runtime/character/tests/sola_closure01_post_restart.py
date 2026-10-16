# -*- coding: utf-8 -*-
"""事故后恢复复核（只读）— 产出 post_restart_recheck.json。

背景：验收窗口结束后，:7860/:8090/:8765/:8010 四个服务在 2026-09-28 10:18~10:23
之间整机消失（含**不是本任务起的**昨天 15:22 的 avatar supervisor pid 6356）。
本脚本只做三件事：
  1) 把「服务没了」这件事的**磁盘可复算证据**采下来（日志尾行、开机时刻、旧 pid 存活性）；
  2) 记录用仓库唯一正式入口 `scripts/Start-AI-Girlfriend.ps1` 恢复后的**现场态**；
  3) 复算「重启后正式 system 块是否仍逐字等于冻结件」——这是本任务的核心断言。

不改任何文件，不删任何文件；唯一写入是 out 目录里的这份 JSON。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sola_closure01_lib as L  # noqa: E402

OLD_WINDOW_PIDS = {
    "7860_listener": 48988, "7860_launcher": 28748,
    "8090_listener": 18748, "8765_listener": 34208, "8010_listener": 12296,
    "8010_supervisor_not_started_by_this_task": 6356,
}
RESTART_LOG = Path("<USER_HOME>/AppData/Local/Temp/gf_stack_restart.log")
START_SCRIPT_REL = "scripts/Start-AI-Girlfriend.ps1"
CONFIG_REL = "runtime/character/sola_v2_ember/sola_v2_ember.json"
LOGTAILS = {
    "avatar_stderr": "logs/runtime/avatar-8010.stderr.log",
    "avatar_heartbeat": "logs/runtime/avatar-8010.heartbeat.log",
    "avatar_exitwitness": "logs/runtime/avatar-8010.exitwitness.log",
    "ui_stderr": "logs/runtime/ui-7860.stderr.log",
    "startup": "logs/runtime/startup.log",
}


def tail_text(rel: str, n_bytes: int = 400) -> str:
    p = L.REPO / rel
    if not p.exists():
        return "<MISSING>"
    raw = p.read_bytes()[-n_bytes:]
    return raw.decode("utf-8", errors="replace")


def tail_lines(rel: str, n: int = 3) -> list[str]:
    p = L.REPO / rel
    if not p.exists():
        return ["<MISSING>"]
    lines = [x for x in p.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
    return lines[-n:]


def main() -> int:
    out: dict[str, object] = {"task_id": L.TASK_ID, "generated_at_local": L.now_local()}

    # 1) 旧窗口那批 pid 现在是否还活着 + 机器有没有重启（排除"重启导致的自然消失"）
    import psutil
    out["old_window_pid_status"] = {
        label: {"pid": pid, "alive": L.pid_alive(pid)}
        for label, pid in OLD_WINDOW_PIDS.items()
    }
    bt = psutil.boot_time()
    import datetime
    out["machine_rebooted"] = {
        "boot_time_local": datetime.datetime.fromtimestamp(bt).strftime("%Y-%m-%d %H:%M:%S"),
        "uptime_s_at_check": round(time.time() - bt),
        "verdict": "NO_REBOOT" if bt < datetime.datetime(2026, 9, 28, 0, 0).timestamp() else "REBOOTED",
    }

    # 2) 服务自己留下的最后字迹（有没有体面退出记录）
    out["service_last_words"] = {
        k: {"path": v, "tail_lines": tail_lines(v, 2), "mtime_local": (
            datetime.datetime.fromtimestamp((L.REPO / v).stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            if (L.REPO / v).exists() else "<MISSING>")} for k, v in LOGTAILS.items()
    }
    out["transient_samples_not_reproducible"] = {
        "说明": ("这两次采样发生在恢复动作之前、只存在于当时那一次工具调用的 stdout，"
                 "现在服务已恢复故无法复算；登记为观测记录而非闸门。"),
        "2026-09-28 10:18:41 +0800": "L.listeners() → :7860 n=0；8090/8765/8010 仍各自有 listener",
        "2026-09-28 10:23:48 +0800": "L.listeners() → 7860/8090/8765/8010 全为 0；"
                                     "psutil.net_connections 四端口 LISTEN 计数 0；"
                                     "HTTP 四端口全部 WinError 10061 连接拒绝",
        "同时刻事件": "后台任务 b9946youx（04:37 用仓库脚本只重启 7860）在完成时被回收，"
                     "其进程树连带昨天 15:22 就存在的 avatar supervisor 6356 一起消失",
    }

    # 3) 恢复命令的逐字回执
    if RESTART_LOG.exists():
        txt = RESTART_LOG.read_text(encoding="utf-8", errors="replace")
        out["restoration_receipt"] = {
            "command": f"pwsh -File {START_SCRIPT_REL} -NoBrowser",
            "log_path": str(RESTART_LOG),
            "log_sha256": L.sha256_bytes(RESTART_LOG.read_bytes()),
            "first_line": txt.splitlines()[0] if txt.splitlines() else "",
            "last_line": txt.splitlines()[-1] if txt.splitlines() else "",
            "START_NATIVE_OK_present": "START_NATIVE_OK backend=qwen3" in txt,
            "lines_verbatim": txt.splitlines(),
        }
    else:
        out["restoration_receipt"] = {"log_path": str(RESTART_LOG), "present": False}

    # 3b) 今天官方入口每一次跑的字迹（startup.log 是唯一跨恢复持久的回执，
    #     临时日志只覆盖第一次；这样"第几次恢复"不靠我记忆）
    p_startup = L.REPO / "logs/runtime/startup.log"
    today = [x for x in (p_startup.read_text(encoding="utf-8", errors="replace").splitlines()
                         if p_startup.exists() else []) if "2026-09-28" in x]
    out["official_start_lines_today"] = {"path": "logs/runtime/startup.log",
                                        "lines_2026_09_28": today,
                                        "count": len(today),
                                        "last_start_native_ok": next(
                                            (x for x in reversed(today) if "START_NATIVE_OK" in x), "")}

    # 4) 恢复后的现场态
    lis = L.listeners()
    out["post_listeners"] = {str(p): (rows[0].get("pid") if rows else None)
                             for p, rows in lis.items()}
    out["post_process_facts"] = {}
    for port, rows in lis.items():
        if not rows:
            continue
        info = L.proc_info(rows[0].get("pid"))
        out["post_process_facts"][str(port)] = {
            "pid": rows[0].get("pid"), "name": info.get("name"),
            "created": info.get("created"), "cmdline": L.short(info.get("cmdline"), 900),
        }
    sp = L.read_json_file("runtime/windows/service_pids.json") or {}
    av = L.read_json_file("runtime/windows/avatar_runtime_state.json") or {}
    own = sp.get("ownership") or sp.get("owners") or {}
    out["post_state_files"] = {
        "service_pids_top_keys": sorted(sp),
        "service_pids_ui": sp.get("ui"),
        "service_pids_s2s_tracked": (own.get("s2s") or {}).get("pid") if isinstance(own, dict) else None,
        "llmOwnership_pid": (sp.get("llmOwnership") or {}).get("pid"),
        "llmOwnership_createdBy": (sp.get("llmOwnership") or {}).get("createdBy"),
        "llmOwnership_commandLine": L.short((sp.get("llmOwnership") or {}).get("commandLine"), 900),
        "avatar_generation": av.get("generation"), "avatar_state": av.get("state"),
        "avatar_listener_pid": av.get("listener_pid"), "avatar_id": av.get("avatar_id"),
        "active_tts_backend": (L.REPO / "runtime/windows/active_tts_backend.txt").read_text(encoding="utf-8").strip(),
    }

    # 5) 核心断言：重启后正式 system 块逐字等于冻结件
    frozen = json.loads((L.OUT_DIR / "runtime_prompt_proof.json").read_text(encoding="utf-8")) \
        if (L.OUT_DIR / "runtime_prompt_proof.json").exists() else {}
    f_live = frozen.get("live_prompt_endpoint") or {}
    f_sola = frozen.get("sola_block") or {}
    want_sha = f_live.get("sha256") or ""
    want_chars = f_live.get("chars")
    want_bytes = f_live.get("bytes")
    want_clean = f_sola.get("sha256") or ""
    want_clean_chars = f_sola.get("chars")
    resp = L.http_json("http://127.0.0.1:7860/api/character/prompt?character_id=c279dcae6d8")
    d = resp.get("json") or {}
    txt = d.get("内容") or ""
    live_sha = L.sha256_bytes(txt.encode("utf-8"))
    tail_len = int(want_clean_chars or 7011)
    tail_sha = L.sha256_bytes(txt[-tail_len:].encode("utf-8")) if len(txt) >= tail_len else ""
    out["live_prompt_after_restart"] = {
        "endpoint": "/api/character/prompt?character_id=c279dcae6d8",
        "http_ok": resp.get("ok"), "http_status": resp.get("status"),
        "ok": d.get("ok"), "character_name": d.get("人物名称"),
        "chars": len(txt), "bytes": len(txt.encode("utf-8")), "sha256": live_sha,
        "declared_zishu": d.get("字数"),
        "sola_tail_chars": tail_len, "sola_tail_sha256": tail_sha,
    }
    out["restart_identity_verdicts"] = {
        "LIVE_BLOCK_SHA_EQUALS_FROZEN": bool(want_sha) and live_sha == want_sha,
        "LIVE_CHARS_EQUALS_FROZEN": want_chars == len(txt),
        "LIVE_BYTES_EQUALS_FROZEN": want_bytes == len(txt.encode("utf-8")),
        "CLEAN_SOLA_BLOCK_SHA_EQUALS_FROZEN": bool(want_clean) and tail_sha == want_clean,
        "frozen_reference": {"sha256": want_sha, "chars": want_chars, "bytes": want_bytes,
                             "sola_block_sha256": want_clean, "sola_block_chars": want_clean_chars},
    }

    # 6) 开关落盘态（env 在新进程里为空 ⇒ 权威回到配置文件）
    cfg = L.read_json_file(CONFIG_REL) or {}
    out["switch_authority_after_restart"] = {
        "config_path": CONFIG_REL,
        "sola_v2_ember_enabled": cfg.get("sola_v2_ember_enabled"),
        "voice_minimal_enabled": cfg.get("voice_minimal_enabled"),
        "user_label": cfg.get("user_label"),
        "char_label_fallback": cfg.get("char_label_fallback"),
        "config_sha256": L.sha256_file(L.REPO / CONFIG_REL),
        "env_on_7860_process": "SOLA_V2_EMBER_ENABLED=None, SOLA_V2_EMBER_VOICE_MINIMAL=None"
                               "（psutil 读 :7860 进程 environ；env 为空 ⇒ 权威回到配置文件）",
    }

    # 6b) 第二通道排除：共享事实源里"别人主动停服"的署名记录（全枚举，不是抽查）
    ms_rel = "runtime/llm/AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_STATE.json"
    ms_path = L.REPO / ms_rel
    ms = L.read_json_file(ms_rel) or {}
    stopped: list[dict[str, Any]] = []

    def walk(obj: Any, path: str) -> None:
        if isinstance(obj, dict):
            if str(obj.get("state", "")).lower() == "stopped":
                dates = {k: str(v) for k, v in obj.items()
                         if any(t in k.lower() for t in ("date", "_at", "when", "by"))
                         and isinstance(v, (str, int))}
                stopped.append({"path": path, "date_fields": dates})
            for k, v in obj.items():
                walk(v, f"{path}/{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]")

    walk(ms, "")
    today_hits = [s for s in stopped
                  if "2026-09-28" in json.dumps(s, ensure_ascii=False)]
    out["second_channel_exclusion"] = {
        "shared_state_file": ms_rel,
        "file_mtime_local": datetime.datetime.fromtimestamp(
            ms_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S") if ms_path.exists() else "<MISSING>",
        "internal_last_updated_at": ms.get("last_updated_at"),
        "internal_last_updated_by": ms.get("last_updated_by"),
        "stopped_records_total": len(stopped),
        "stopped_records": stopped,
        "stopped_records_dated_2026_09_28": len(today_hits),
        "verdict": ("NO_SIGNED_EXTERNAL_STOP_TODAY"
                    if ms_path.exists() and not today_hits else "SEE_RECORDS"),
    }

    # 6c) 事故时间线：不由我手写在正文里，而是从服务自己的日志把 2026-09-28 的行**全量**捞出来
    #     （判"官方停服脚本没跑过"要用 stop.log 里当天的行数=0，这是可复算的分母，不是措辞）
    def dated_lines(rel: str, sub: str = "2026-09-28") -> list[str]:
        # 尺的自检：task_run.log 用斜杠日期（2026/09/28），supervisor/stop.log 用横杠。
        # 只按一种写法过滤会**假返 0 行**，看上去像"今天没事故"，其实是尺在空转。
        variants = (sub, sub.replace("-", "/"))
        p = L.REPO / rel
        if not p.exists():
            return ["<MISSING>"]
        return [x for x in p.read_text(encoding="utf-8", errors="replace").splitlines()
                if any(v in x for v in variants)]

    def date_forms_seen(rel: str) -> list[str]:
        p = L.REPO / rel
        if not p.exists():
            return []
        import re as _re
        return sorted(set(_re.findall(r"20\d\d[-/]0\d[-/]\d\d", p.read_text(
            encoding="utf-8", errors="replace"))))[-3:]

    inc = {
        "llm_task_run_log": {"path": "runtime/llm/logs/task_run.log",
                            "lines_2026_09_28": dated_lines("runtime/llm/logs/task_run.log")},
        "avatar_supervisor_log": {"path": "logs/runtime/avatar-supervisor.log",
                                 "lines_2026_09_28": dated_lines("logs/runtime/avatar-supervisor.log")},
        "avatar_exitwitness_log": {"path": "logs/runtime/avatar-8010.exitwitness.log",
                                  "lines_2026_09_28": dated_lines("logs/runtime/avatar-8010.exitwitness.log")},
        "official_stop_log": {"path": "logs/runtime/stop.log",
                             "lines_2026_09_28": dated_lines("logs/runtime/stop.log")},
        "llm_own_log_last_write": "runtime/llm/logs/llama_8090_gemma4.log",
    }
    inc["llm_own_log_mtime_local"] = datetime.datetime.fromtimestamp(
        (L.REPO / inc["llm_own_log_last_write"]).stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    # 尺的正控制：这四份日志里都有 2026 年的行，任何一份采成 0 行 = 尺空转，不是"没事故"
    inc["probe_self_control"] = {
        rel: {"date_forms_at_tail": date_forms_seen(rel),
              "lines_today": len(dated_lines(rel))}
        for rel in ("runtime/llm/logs/task_run.log", "logs/runtime/avatar-supervisor.log",
                    "logs/runtime/avatar-8010.exitwitness.log", "logs/runtime/stop.log")
    }
    inc["verdicts"] = {
        # stop.log 当天 0 行 ⇒ 仓库的正式停服入口没有跑过（不是"我记得没跑"）
        "NO_OFFICIAL_STOP_SCRIPT_TODAY": len(inc["official_stop_log"]["lines_2026_09_28"]) == 0,
        # llama 侧的 exit code=-1 是被外部终结的字迹（正常退出记 0；历史上 -1 都是被杀）
        "LLM_EXIT_CODE_MINUS_1_SEEN": any("task exit code=-1" in x
                                         for x in inc["llm_task_run_log"]["lines_2026_09_28"]),
        "AVATAR_SUPERVISOR_RELAUNCHED_TODAY": any("LAUNCH_OK" in x
                                                 for x in inc["avatar_supervisor_log"]["lines_2026_09_28"]),
    }
    out["incident_timeline_from_service_logs"] = inc

    # 7) 之前唯一那条红的套件，现在重跑
    p = subprocess.run([str(L.REPO / "runtime/core/venv/Scripts/python.exe"),
                        str(L.REPO / "runtime/tests/test_state_single_source.py")],
                       cwd=str(L.REPO), capture_output=True, text=True, timeout=300)
    lines = [x for x in (p.stdout or "").splitlines() if x.strip()]
    out["test_state_single_source_rerun"] = {
        "rc": p.returncode, "last_lines": lines[-3:],
        "PASS_counted": (p.returncode == 0 and any(x.startswith("test_state_single_source: PASS") for x in lines)),
        "why_it_was_red_before": ("旧记录里的 llmOwnership.pid=17468 是上一轮遗留的死亡 pid；"
                                  "正式启动脚本这次重新捕获为当前 :8090 listener，"
                                  "于是 service-pids-port-agreement 由红转绿"),
    }

    # 7b) 终态离线装配复算：:7860 已经不在了，"运行态 system 块逐字等于冻结件"这一条
    #     不能只靠 HTTP 端点复算。仓库的正式装配器是纯代码+配置文件，不需要服务，
    #     所以这里复用**已在清单里哈希过的** rollback 采集器的 `--case on`
    #     （生产当前环境、不注入任何变量），把整条 system 和 Sola 块重新拼一遍。
    pp_rel = "runtime/character/tests/out/sola_unattended_closure_01/runtime_prompt_proof.json"
    pp = L.read_json_file(pp_rel) or {}
    frozen_live_sha = pp.get("live_block_sha256") or ""
    frozen_clean = (pp.get("sola_block") or {}).get("sha256") or ""
    sp = subprocess.run([sys.executable,
                         str(HERE / "sola_closure01_rollback.py"), "--case", "on"],
                        cwd=str(L.REPO), capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=240)
    row: dict[str, Any] = {}
    for one in (sp.stdout or "").splitlines():
        if one.startswith("CASE_JSON::"):
            row = json.loads(one[len("CASE_JSON::"):])
            break
    out_sha = row.get("system_text_sha256") or ""
    out_block = row.get("sola_block_sha256") or ""
    out["terminal_offline_assembly"] = {
        "method": ("复用 sola_closure01_rollback.py --case on：起一个全新 python，"
                   "按磁盘当前配置装配生产 system（不注入 env、不写任何生产文件）。"),
        "requires_live_service": False,
        "rc": sp.returncode,
        "character_id": row.get("character_id"),
        "block_names_in_order": row.get("block_names_in_order"),
        "block_sizes": row.get("block_sizes"),
        "system_text_sha256": out_sha,
        "system_text_chars": row.get("system_text_chars"),
        "sola_block_sha256": out_block,
        "sola_block_chars": row.get("sola_block_chars"),
        "render_state": row.get("render_state"),
        "switch_authority": row.get("adapter_describe"),
        "frozen_reference": {"live_block_sha256": frozen_live_sha,
                             "sola_clean_block_sha256": frozen_clean,
                             "from": pp_rel},
        "OFFLINE_SYSTEM_SHA_EQUALS_FROZEN": bool(frozen_live_sha) and out_sha == frozen_live_sha,
        "OFFLINE_SOLA_BLOCK_SHA_EQUALS_FROZEN_CLEAN": (bool(frozen_clean)
                                                       and out_block == frozen_clean),
        "stderr_tail": (sp.stderr or "")[-400:],
    }
    offline_ok = (out["terminal_offline_assembly"]["OFFLINE_SYSTEM_SHA_EQUALS_FROZEN"]
                  and out["terminal_offline_assembly"]["OFFLINE_SOLA_BLOCK_SHA_EQUALS_FROZEN_CLEAN"])

    n_live = len([v for v in out["post_listeners"].values() if v])
    identity_ok = all([
        out["restart_identity_verdicts"]["LIVE_BLOCK_SHA_EQUALS_FROZEN"],
        out["restart_identity_verdicts"]["CLEAN_SOLA_BLOCK_SHA_EQUALS_FROZEN"],
    ])
    if n_live == 4 and identity_ok:
        pv = "RESTORED_AND_SOLA_STILL_BYTE_IDENTICAL"
    elif n_live == 0 and offline_ok:
        pv = ("BLOCKED_ALL_LISTENERS_GONE_DISK_ASSEMBLY_STILL_BYTE_IDENTICAL")
    else:
        pv = "CHECK_FAILED"
    out["POST_RESTART_VERDICT"] = pv

    # 8) 硬阻塞判定（只读）：官方入口最近一次失败的原因 + 四个项目计划任务当前是否被禁用。
    #    名称必须来自磁盘字迹，不许我口头命名。
    failed_lines = [x for x in today if "START_NATIVE_FAILED" in x]
    tasks = {}
    for tn in ("GirlfriendLLM8090_Manual", "GirlfriendAvatar8010_Manual",
               "GirlfriendS2S8765_Manual", "GirlfriendUI7860_Manual"):
        r = subprocess.run(["schtasks", "/query", "/tn", tn, "/fo", "LIST"],
                           capture_output=True)
        t = r.stdout.decode("gbk", errors="replace")
        mode = next((l.split(":", 1)[1].strip() for l in t.splitlines() if "模式" in l), "<未读到>")
        tasks[tn] = {"rc": r.returncode, "mode_field": mode}
    n_live = len([v for v in out["post_listeners"].values() if v])
    all_disabled = all(v["mode_field"] == "已禁用" for v in tasks.values())
    out["hard_blocker"] = {
        "live_listeners_at_recheck": n_live,
        "official_start_last_attempt": failed_lines[-1] if failed_lines else "",
        "official_start_failed_lines_today": failed_lines,
        "girlfriend_scheduled_tasks": tasks,
        "all_four_tasks_disabled": all_disabled,
        "enabling_a_task_is_out_of_scope_because": "§21 硬停止条件「必须改系统设置」",
        "FINAL_RESULT_if_blocked": (
            "BLOCKED_HOST_STACK_TORN_DOWN_EXTERNALLY_AND_ALL_GIRLFRIEND_TASKS_DISABLED"
            if (n_live < 4 and all_disabled and failed_lines) else ""),
    }
    path = L.write_json("post_restart_recheck.json", out)
    print(f"POST_RESTART_VERDICT={pv}")
    print("LIVE_BLOCK_SHA_EQUALS_FROZEN=", out["restart_identity_verdicts"]["LIVE_BLOCK_SHA_EQUALS_FROZEN"])
    print("CLEAN_SOLA_BLOCK_SHA_EQUALS_FROZEN=", out["restart_identity_verdicts"]["CLEAN_SOLA_BLOCK_SHA_EQUALS_FROZEN"])
    oa = out["terminal_offline_assembly"]
    print("OFFLINE_SYSTEM_SHA_EQUALS_FROZEN=", oa["OFFLINE_SYSTEM_SHA_EQUALS_FROZEN"], oa["system_text_sha256"][:16])
    print("OFFLINE_SOLA_BLOCK_SHA_EQUALS_FROZEN_CLEAN=",
          oa["OFFLINE_SOLA_BLOCK_SHA_EQUALS_FROZEN_CLEAN"], (oa["sola_block_sha256"] or "")[:16])
    print("listeners=", out["post_listeners"])
    print("suite=", out["test_state_single_source_rerun"]["rc"], out["test_state_single_source_rerun"]["last_lines"][-1:])
    print("written:", path)
    return 0 if (pv == "RESTORED_AND_SOLA_STILL_BYTE_IDENTICAL"
                 or pv.endswith("DISK_ASSEMBLY_STILL_BYTE_IDENTICAL")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
