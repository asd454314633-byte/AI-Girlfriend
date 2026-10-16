# -*- coding: utf-8 -*-
"""sola_closure01_rollback.py — 收口任务 §17（OFF 路径回退）+ §18（V2b 判断器保持关闭）。

**一个模型调用都不发。** 开关的 OFF/ON 全部在**子进程内**用环境变量重现
（§17 原文允许「在测试环境/进程内或安全热开关方式」），不重启 :7860、不改
配置文件、不删任何文件。

本脚本有两种角色：
  * `--case <名字>`：子进程模式，按指定环境装配一次正式 system，把事实打成
    一行 JSON 到 stdout（父进程解析）。
  * 无参数：父进程模式，依次起 4 个子进程（on / vm_off / vm_on / ember_off），
    做 OFF↔ON 对拍、读运行时判断器闸口、复核 §12 那 4 发的调用计数，
    落 `rollback_proof.json`。

为什么每种环境都要另起进程：渲染缓存按 mtime 键、开关按环境读，同一进程里
翻环境会吃到上一份缓存 —— 那正好是「假 OFF」的成因，不能让它藏在证据里。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import sola_closure01_lib as L  # noqa: E402

OUT = L.ensure_out_dir()
LLM_LOG = L.REPO / "runtime" / "llm" / "logs" / "llama_8090_gemma4.log"
S2S_LOG = L.REPO / "logs" / "runtime" / "s2s.stderr.log"
USER_TEXT = "今天加班到好晚，累死了"
HISTORY_ROWS = [{"role": "user", "text": "妈，我今晚想早点回来。"},
                {"role": "assistant", "text": "回来就好，路上慢点。"}]

CASE_ENV: dict[str, dict[str, str]] = {
    "on": {},                                        # 生产当前环境，什么都不注入
    "vm_off": {"SOLA_V2_EMBER_VOICE_MINIMAL": "false"},
    "vm_on": {"SOLA_V2_EMBER_VOICE_MINIMAL": "true"},
    "ember_off": {"SOLA_V2_EMBER_ENABLED": "false"},
}


# ---------------------------------------------------------------------------
# 子进程：按环境装配一份正式 system，只回事实
# ---------------------------------------------------------------------------

def run_case(case: str) -> int:
    L.bare_import_root()
    from runtime.character import context_builder  # type: ignore
    from runtime.character import store  # type: ignore
    from runtime.character.sola_v2_ember import adapter as sola_adapter  # type: ignore
    from runtime.character.sola_v2_ember import ember as ember_mod  # type: ignore
    from runtime.character.sola_v2_ember import voice_minimal as vm  # type: ignore

    ident = store.get_active_character()
    labels = sola_adapter.labels_for(ident)
    ctx = context_builder.build(character_id=ident, session_id="",
                                user_text=USER_TEXT, history=HISTORY_ROWS)
    blocks = [(name, text.strip()) for name, text in ctx.system_blocks() if text.strip()]
    r = vm.render(char_label=labels["char"], user_label=labels["user"])
    state = sola_adapter.render_state()          # 上一次渲染走的哪条路（只读缓存）
    desc = sola_adapter.describe(ident)
    payload = {
        "case": case,
        "env_injected": dict(CASE_ENV.get(case, {})),
        "env_effective": {k: os.environ.get(k, "<unset>") for k in
                          ("SOLA_V2_EMBER_ENABLED", "SOLA_V2_EMBER_VOICE_MINIMAL")},
        "character_id": ident,
        "render_state": state,
        "adapter_describe": {k: desc.get(k) for k in
                             ("sola_v2_ember_enabled", "voice_minimal_enabled",
                              "开关来源", "清洗开关来源", "mode", "reason")
                             if isinstance(desc, dict)},
        "system_text_sha256": L.sha256_bytes(ctx.system_text().encode("utf-8")),
        "system_text_chars": len(ctx.system_text()),
        "block_names_in_order": [name for name, _ in blocks],
        "block_sizes": {name: len(text) for name, text in blocks},
        "sola_block_present": any(name == "sola" for name, _ in blocks),
        "sola_block_sha256": next((L.sha256_bytes(t.encode("utf-8"))
                                   for name, t in blocks if name == "sola"), None),
        "sola_block_chars": next((len(t) for name, t in blocks if name == "sola"), 0),
        "non_sola_joined_sha256": L.sha256_bytes(
            "\n\n".join(t for name, t in blocks if name != "sola").encode("utf-8")),
        "msg_roles": [str(m.get("role")) for m in
                      context_builder.messages(ctx, temporary_state=None)],
        # 注意：voice_minimal.render() 自己**永远**给清洗版（清洗是纯函数），
        # 真正的开关在 adapter —— 它按 voice_minimal_enabled() 决定用
        # voice_minimal.render().text 还是 ember.render().text。所以下面这两行
        # 在四个 case 里都一样，用来证明「盘上的两份都还在」，不参与开关判据。
        "vm_raw_sha256": L.sha256_bytes(r.raw_text.strip().encode("utf-8")),
        "vm_raw_chars": len(r.raw_text.strip()),
        "vm_clean_sha256": L.sha256_bytes(r.text.strip().encode("utf-8")),
        "vm_clean_chars": len(r.text.strip()),
        "vm_applied_count": len(getattr(r, "applied", ()) or ()),
        # 控制项：这条**不读环境**（吃盘上冻结件 + 盘上清单），四个 case 里都该是 6。
        # 它的作用是「环境没生效」与「清洗清单坏了」不会长得一样。
        "vm_spec_edits_count_on_disk": len(
            json.loads(Path(vm.SPEC_PATH).read_text(encoding="utf-8")).get("edits") or []),
        "vm_clean_payload_applied_live": len(
            vm.clean_payload(ember_mod.load_source())[1]),
    }
    sys.stdout.write("CASE_JSON::" + json.dumps(payload, ensure_ascii=False) + "\n")
    return 0


# ---------------------------------------------------------------------------
# 父进程
# ---------------------------------------------------------------------------

def spawn(case: str) -> dict[str, Any]:
    env = dict(os.environ)
    env.update(CASE_ENV[case])
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--case", case],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=240, cwd=str(L.REPO))
    line = ""
    for one in (proc.stdout or "").splitlines():
        if one.startswith("CASE_JSON::"):
            line = one
            break
    if not line:
        return {"case": case, "error": "子进程没回 CASE_JSON",
                "rc": proc.returncode,
                "stdout_tail": L.short(proc.stdout, 1500),
                "stderr_tail": L.short(proc.stderr, 1500)}
    row = json.loads(line[len("CASE_JSON::"):])
    row["rc"] = proc.returncode
    row["stderr_tail"] = L.short(proc.stderr, 600) if proc.stderr else ""
    return row


def log_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except Exception:
        return -1


def tail_region(path: Path, off: int) -> str:
    if off < 0 or not path.exists():
        return ""
    with open(path, "rb") as fh:
        fh.seek(off)
        return fh.read(400_000).decode("utf-8", "replace")


def judge_state() -> dict[str, Any]:
    """运行期两道闸的现状（子进程读，避免本进程的缓存/环境干扰）。"""
    code = (
        "import json,sys\n"
        "sys.path.insert(0, %r)\n"
        "from runtime.human_interaction import config as hi\n"
        "cfg = hi.config()\n"
        "out={'turn_judge_enabled': hi.turn_judge_enabled(),"
        "     'human_interaction_enabled_effective': hi.enabled(),\n"
        "     'feature_enabled_in_file': cfg.get('feature_enabled'),\n"
        "     'revise_enabled': hi.revise_enabled(),\n"
        "     'env_HUMAN_INTERACTION_ENABLED': __import__('os').environ.get("
        "'HUMAN_INTERACTION_ENABLED', '<unset>'),\n"
        "     'config_enabled_keys': {k: v for k, v in sorted(cfg.items())\n"
        "                             if k.endswith('_enabled')}\n"
        "}\n"
        "print('JUDGE_JSON::'+json.dumps(out,ensure_ascii=False,default=str))\n"
        % str(L.REPO))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=120, cwd=str(L.REPO))
    for one in (proc.stdout or "").splitlines():
        if one.startswith("JUDGE_JSON::"):
            row = json.loads(one[len("JUDGE_JSON::"):])
            row["rc"] = proc.returncode
            return row
    return {"error": "没读到判断器配置", "rc": proc.returncode,
            "stdout": L.short(proc.stdout, 800), "stderr": L.short(proc.stderr, 800)}


# 服务自己会写的运行态登记件：§17 禁止的是**删文件**，不是「服务活着时不许更新自己的
# PID 登记表」。把这些和「冻结件/源码/配置」分开钉，两边各用自己那条判据 ——
# 混在一把尺里要么把合法生命周期写成回归，要么把真的删改放过。
SERVICE_OWNED_STATE = {"runtime/windows/service_pids.json",
                       "runtime/windows/avatar_runtime_state.json"}


def file_untouched_vs_baseline() -> dict[str, Any]:
    """§17「不得删除文件」：基线登记过的 17 个文件逐个复算。

    * 冻结件 / 源码 / 配置：**必须逐字节相同**（哈希相等）。
    * 服务自己写的运行态登记件：必须**还在**；哈希漂了要能归因到本轮 §2 允许的
      那次重启（拿 :7860 新进程的创建时间和该文件 mtime 对拍），归不了因就判红。
    """
    base = json.loads((OUT / "baseline.json").read_text(encoding="utf-8"))
    services = json.loads((OUT / "runtime_services.json").read_text(encoding="utf-8"))
    ui_created = str((((services.get("ui_new_code") or {}).get("ui_process") or {})
                      .get("created")) or "")
    rows = []
    bad: list[str] = []
    drift: list[dict[str, Any]] = []
    for fact in base.get("file_sha256") or []:
        rel = str(fact.get("path"))
        now = L.file_fact(rel)
        volatile = rel in SERVICE_OWNED_STATE
        if not now.get("exists"):
            bad.append(rel + "（文件已不存在）")
        elif volatile:
            if now.get("sha256") != fact.get("sha256"):
                mt = str(now.get("mtime") or "")[:19]
                explained = bool(ui_created) and mt == ui_created[:19]
                drift.append({"path": rel, "mtime_now": now.get("mtime"),
                              "ui_process_created": ui_created,
                              "mtime_equals_7860_restart": explained,
                              "note": "本轮 §2 允许的重启 :7860 由项目脚本改写自己的 PID 登记表"
                              if explained else "漂移归因不到本轮允许的操作，需人工判"})
                if not explained:
                    bad.append(rel + "（运行态漂移但归因不了）")
        elif now.get("sha256") != fact.get("sha256"):
            bad.append(rel + "（冻结件/源码/配置被改过）")
        rows.append({"path": rel, "group": "服务运行态" if volatile else "冻结件/源码/配置",
                     "existed_at_baseline": fact.get("exists"),
                     "exists_now": now.get("exists"),
                     "sha256_at_baseline": L.short(fact.get("sha256"), 70),
                     "sha256_now": L.short(now.get("sha256"), 70),
                     "bytes_now": now.get("bytes"), "mtime_now": now.get("mtime"),
                     "identical": bool(now.get("exists"))
                                  and now.get("sha256") == fact.get("sha256")})
    return {"files_checked": len(rows),
            "frozen_source_config_checked": sum(1 for one in rows
                                                if one["group"].startswith("冻结件")),
            "frozen_source_config_identical": sum(1 for one in rows
                                                  if one["group"].startswith("冻结件")
                                                  and one["identical"]),
            "service_owned_state": [one["path"] for one in rows
                                    if one["group"] == "服务运行态"],
            "service_state_drift": drift,
            "changed_or_missing": bad,
            "PASS": not bad,
            "NO_FILE_DELETED_OR_REWRITTEN": not bad, "rows": rows}


def production_final_state() -> dict[str, Any]:
    """最终生产必须停在 ON：只读 GET :7860 那一整块，与 §9/§12 的哈希对拍。"""
    cfg = L.read_json_file("runtime/character/sola_v2_ember/sola_v2_ember.json")
    url = ("http://127.0.0.1:7860/api/character/prompt?character_id="
           + urllib.parse.quote(_active_character() or ""))
    r = L.http_json(url, timeout=60)
    j = r.get("json") or {}
    block = str(j.get("内容") or "")
    proof = json.loads((OUT / "runtime_prompt_proof.json").read_text(encoding="utf-8"))
    smoke = json.loads((OUT / "single_call_proof.json").read_text(encoding="utf-8"))
    frozen_clean = ((proof.get("block_sizes_vs_frozen") or {})
                    .get("清洗块_实测") or {}).get("sha256")
    instructions_sha = ((smoke.get("instructions") or {}).get("sha256")
                        if isinstance(smoke.get("instructions"), dict) else None)
    return {
        "config_file": {"path": "runtime/character/sola_v2_ember/sola_v2_ember.json",
                        "sola_v2_ember_enabled": (cfg or {}).get("sola_v2_ember_enabled")
                        if isinstance(cfg, dict) else None,
                        "voice_minimal_enabled": (cfg or {}).get("voice_minimal_enabled")
                        if isinstance(cfg, dict) else None,
                        "mtime": L.file_fact(
                            "runtime/character/sola_v2_ember/sola_v2_ember.json"
                        ).get("mtime"),
                        "本回收器进程的环境变量": {
                            k: os.environ.get(k, "<unset>") for k in
                            ("SOLA_V2_EMBER_ENABLED", "SOLA_V2_EMBER_VOICE_MINIMAL")}},
        "live_endpoint": {"url": url, "ok": r.get("ok"), "status": r.get("status"),
                          "chars": len(block),
                          "sha256": L.sha256_bytes(block.encode("utf-8")) if block else ""},
        "must_match_clean_frozen_sha256": frozen_clean,
        "must_match_smoke_instructions_sha256": instructions_sha,
    }


def _active_character() -> str:
    try:
        p = L.REPO / "runtime" / "character" / "data" / "active_character.json"
        return str(json.loads(p.read_text(encoding="utf-8")).get("character_id")
                   or json.loads(p.read_text(encoding="utf-8")).get("id") or "")
    except Exception:
        return ""


def main() -> int:
    if "--case" in sys.argv:
        return run_case(sys.argv[sys.argv.index("--case") + 1])

    llm_off_before = log_size(LLM_LOG)
    s2s_off_before = log_size(S2S_LOG)
    started = L.now_local()

    cases = {case: spawn(case) for case in CASE_ENV}

    # §9/§10 已冻结的两个块哈希，作为「哪个块该出现」的基准（不硬编码，读上一件证据）
    proof = json.loads((OUT / "runtime_prompt_proof.json").read_text(encoding="utf-8"))
    bs = proof.get("block_sizes_vs_frozen") or {}
    frozen_raw = (bs.get("原版块_实测") or {})
    frozen_clean = (bs.get("清洗块_实测") or {})

    on = cases.get("on", {})
    vm_off = cases.get("vm_off", {})
    vm_on = cases.get("vm_on", {})
    em_off = cases.get("ember_off", {})

    checks: dict[str, Any] = {}
    checks["OFF_1_voice_minimal_disappears"] = {
        "判据": ("SOLA_V2_EMBER_VOICE_MINIMAL=false 时，sola 块必须逐字退回**原版 Ember**"
                 "（= 冻结的 7363 字 / sha 1b7f9ef2…），清洗版那一整块消失"),
        "sola块在场": vm_off.get("sola_block_present"),
        "该回合块sha256": vm_off.get("sola_block_sha256"),
        "冻结原版sha256": frozen_raw.get("sha256"),
        "冻结清洗版sha256": frozen_clean.get("sha256"),
        "退回原版且≠清洗版": (vm_off.get("sola_block_sha256") == frozen_raw.get("sha256")
                             and vm_off.get("sola_block_sha256")
                             != frozen_clean.get("sha256")),
        "清洗版仍在盘上": vm_off.get("vm_clean_chars"),
        "PASS": (vm_off.get("sola_block_sha256") == frozen_raw.get("sha256")
                 and vm_off.get("vm_clean_chars", 0) > 0),
    }
    checks["OFF_2_voice_minimal_returns"] = {
        "判据": "恢复 SOLA_V2_EMBER_VOICE_MINIMAL=true 后，块必须逐字回到清洗版，"
               "且与生产当前那一块（case=on）完全相同",
        "恢复后sha256": vm_on.get("sola_block_sha256"),
        "生产当前sha256": on.get("sola_block_sha256"),
        "等于冻结清洗版": vm_on.get("sola_block_sha256") == frozen_clean.get("sha256"),
        "PASS": (vm_on.get("sola_block_sha256") == frozen_clean.get("sha256")
                 and vm_on.get("sola_block_sha256") == on.get("sola_block_sha256")),
    }
    checks["OFF_3_ember_off_is_byte_identical_pre_sola"] = {
        "判据": "SOLA_V2_EMBER_ENABLED=false 时整条 system 里不能有 sola 块，"
               "且**其余块的拼接**必须与开着 Sola 时逐字节相同（Sola 只做加法，不动别的段）",
        "sola块在场": em_off.get("sola_block_present"),
        "块序_ON": on.get("block_names_in_order"),
        "块序_OFF": em_off.get("block_names_in_order"),
        "其余段拼接sha_ON": on.get("non_sola_joined_sha256"),
        "其余段拼接sha_OFF": em_off.get("non_sola_joined_sha256"),
        "OFF少掉的字节数": int(on.get("system_text_chars") or 0)
                       - int(em_off.get("system_text_chars") or 0),
        "PASS": (em_off.get("sola_block_present") is False
                 and on.get("non_sola_joined_sha256") == em_off.get("non_sola_joined_sha256")
                 and "sola" in (on.get("block_names_in_order") or [])),
    }
    checks["files_not_deleted"] = file_untouched_vs_baseline()

    # ---- §18 V2b 判断器：运行期闸关着 + 那 4 发真实文字请求每发只 1 次调用 ----
    judge = judge_state()
    smoke = json.loads((OUT / "single_call_proof.json").read_text(encoding="utf-8"))
    # §11 那三把尺的原始行数是字典；这里取「launch_slot 处理任务行数」当作该回合
    # 进 :8090 的请求数（三把尺是否互相一致，§11 自己已经证过并记在 agree 里）。
    per_turn_calls: list[dict[str, Any]] = []
    for one in smoke["per_turn"]:
        ruler = one.get("llm_calls_from_8090_log") or {}
        per_turn_calls.append({"turn_id": one.get("turn_id"),
                               "launch_slot_lines": ruler.get("launch_slot_lines"),
                               "prompt_eval_lines": ruler.get("prompt_eval_lines"),
                               "release_lines": ruler.get("release_lines"),
                               "三把尺一致": ruler.get("agree")})
    calls = [int(one.get("launch_slot_lines") or 0) for one in per_turn_calls]
    # :8090 侧「这一回合只进来一个请求」的第二把尺：s2s 自己的 httpx 请求行
    s2s_lines_for_posts: list[str] = []
    if S2S_LOG.exists():
        with open(S2S_LOG, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "POST http://127.0.0.1:8090/v1/chat/completions" in line:
                    s2s_lines_for_posts.append(line[:60])
    posts_total_in_log = len(s2s_lines_for_posts)
    checks["v2b_turn_judge_disabled"] = {
        "判据": "运行期不能调用（代码可以保留）：两道闸的**运行期读数**都必须 False，"
               "且那 4 发真实文字请求每发的 :8090 调用数必须 ==1（多发一次就是判断器偷跑）",
        "turn_judge_enabled": judge.get("turn_judge_enabled"),
        "human_interaction_enabled_effective": judge.get(
            "human_interaction_enabled_effective"),
        "feature_enabled_in_file": judge.get("feature_enabled_in_file"),
        "revise_enabled": judge.get("revise_enabled"),
        "env_HUMAN_INTERACTION_ENABLED": judge.get("env_HUMAN_INTERACTION_ENABLED"),
        "config_enabled_keys": judge.get("config_enabled_keys"),
        "读数都是真布尔_尺没瞎": all(isinstance(judge.get(k), bool) for k in
                                     ("turn_judge_enabled",
                                      "human_interaction_enabled_effective",
                                      "revise_enabled")),
        "per_turn_llm_calls_from_8090_log": calls,
        "逐回合三把尺": per_turn_calls,
        "每发恰好一次": bool(calls) and all(one == 1 for one in calls),
        "口径": (":8090 没开 --metrics，所以数的是**该回合日志区段内**的处理任务行数；"
                 "它是区段级计数，谁在这个区段多打一发都会进分母 —— 判断器偷跑必然显形。"
                 "§11 已证三把尺（launch_slot / prompt eval / release）逐回合互相一致。"),
        "s2s侧对8090的chat请求行数_整份日志": posts_total_in_log,
        "PASS": (judge.get("turn_judge_enabled") is False
                 and judge.get("human_interaction_enabled_effective") is False
                 and judge.get("revise_enabled") is False
                 and all(isinstance(judge.get(k), bool) for k in
                         ("turn_judge_enabled", "human_interaction_enabled_effective",
                          "revise_enabled"))
                 and bool(calls) and all(one == 1 for one in calls)
                 and judge.get("error") is None),
    }
    checks["TURN_JUDGE_CALLS"] = 0 if checks["v2b_turn_judge_disabled"]["PASS"] \
        else "UNKNOWN"

    # ---- 本回收器自己有没有偷发模型调用（OFF/ON 全在进程内渲染）----
    new_llm = log_size(LLM_LOG) - llm_off_before
    new_s2s = log_size(S2S_LOG) - s2s_off_before
    region = tail_region(LLM_LOG, llm_off_before)
    llm_lines_here = [one for one in region.splitlines() if "processing task" in one]
    checks["rollback_proof_made_no_llm_calls"] = {
        "判据": "本脚本只渲染、不生成：:8090 日志在本段新增里不得出现 processing task",
        "llama日志新增字节": new_llm, "s2s日志新增字节": new_s2s,
        "processing_task新增行数": len(llm_lines_here),
        "PASS": new_llm >= 0 and not llm_lines_here,
    }

    prod = production_final_state()
    live_sha = (prod.get("live_endpoint") or {}).get("sha256") or ""
    # 生产最终态必须还端出 §9/§12 那一份整块：三路哈希同时相等才算 ON
    prod["PRODUCTION_LEFT_ON"] = bool(live_sha) and live_sha == on.get(
        "system_text_sha256") == (proof.get("live_block_sha256") or "") == \
        (smoke.get("instructions") or {}).get("sha256")
    prod["对拍基准"] = {"§9_在线整块": proof.get("live_block_sha256"),
                       "§12_实际喂给模型的instructions": (
                           (smoke.get("instructions") or {}).get("sha256")),
                       "本轮case=on": on.get("system_text_sha256")}

    all_pass = all(one["PASS"] for one in checks.values() if isinstance(one, dict)
                   and "PASS" in one)
    payload: dict[str, Any] = {
        "task_id": L.TASK_ID,
        "stages": ["§17 OFF 路径回退验证", "§18 V2b 判断器保持关闭"],
        "collected_at_local": L.now_local(),
        "started_at_local": started,
        "read_only": True,
        "method": ("子进程注入环境变量重现 OFF 态；:7860 未重启、配置未改、文件未删、"
                   "模型一次都没调"),
        "cases": cases,
        "checks": checks,
        "human_interaction_judge_readback": judge,
        "production_final_state": prod,
        "ROLLBACK_REVERSIBLE": "YES" if all_pass else "NO",
        "PRODUCTION_LEFT_ON": prod["PRODUCTION_LEFT_ON"],
    }
    path = L.write_json("rollback_proof.json", payload)
    for name, one in checks.items():
        if isinstance(one, dict):
            print(f"  {name:52s} PASS={one.get('PASS')}")
    print("ROLLBACK_REVERSIBLE =", payload["ROLLBACK_REVERSIBLE"],
          "| PRODUCTION_LEFT_ON =", payload["PRODUCTION_LEFT_ON"],
          "| TURN_JUDGE_CALLS =", payload["checks"]["TURN_JUDGE_CALLS"], "->", path)
    return 0 if payload["ROLLBACK_REVERSIBLE"] == "YES" else 3


if __name__ == "__main__":
    sys.exit(main())
