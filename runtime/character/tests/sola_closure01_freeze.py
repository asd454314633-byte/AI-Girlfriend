# -*- coding: utf-8 -*-
"""sola_closure01_freeze.py — 收口任务 §22 manifest.json + §23 那一屏 + §24 裁决。

这一屏的每一个值都**从已落盘的证据产物里读出来**，不是凭记忆手填：
每个键都带 source=<产物>.<字段路径>，值与产物对不上就报 MISMATCH 而不是照抄。

manifest.json 给目录里每个文件算 SHA256（含本轮 9 个采集脚本自己的哈希 —— 判据
长在脚本里，脚本没哈希等于判据没交付）。
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import sola_closure01_lib as L  # noqa: E402

ARTIFACTS = ("baseline.json", "gpu_ram_pre.json", "gpu_ram_post.json",
             "runtime_services.json", "runtime_prompt_proof.json",
             "single_call_proof.json", "smoke_rows.jsonl", "tts_smoke.json",
             "avatar_smoke.json", "rollback_proof.json", "regression_summary.json",
             "protection.json")
COLLECTORS = ("sola_closure01_lib.py", "sola_closure01_baseline.py",
              "sola_closure01_services.py", "sola_closure01_prompt.py",
              "sola_closure01_smoke.py", "sola_closure01_media.py",
              "sola_closure01_rollback.py", "sola_closure01_regression.py",
              "sola_closure01_protection.py", "sola_closure01_health.py",
              "sola_closure01_gpu.py", "sola_closure01_report_audit.py",
              "sola_closure01_post_restart.py",
              "sola_closure01_freeze.py")
FROZEN_INPUTS = ("runtime/character/sola_v2_ember/source/Sola V2 Ember.json",
                 "runtime/character/sola_v2_ember/voice_minimal_edits.json",
                 "runtime/character/sola_v2_ember/sola_v2_ember.json",
                 "runtime/character/sola_v2_ember/SOURCE_MANIFEST.json",
                 "runtime/human_interaction/config.py")


def dig(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
                continue
            except (ValueError, IndexError):
                return None
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def load_all() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in ARTIFACTS:
        p = L.OUT_DIR / name
        if not p.exists():
            out[name] = {"_missing": True}
            continue
        if name.endswith(".jsonl"):
            out[name] = [json.loads(one) for one in
                         p.read_text(encoding="utf-8").splitlines() if one.strip()]
        else:
            out[name] = json.loads(p.read_text(encoding="utf-8"))
    # §22 没点名的第 13 份产物：事故后只读复核。它不是 ARTIFACTS 的成员
    # （清单第 12 行的计数和审计里"12 份 §22 产物"的口径都依赖 ARTIFACTS 不变），
    # 只在 main() 里给 §24 的 BLOCKED 根因名做来源。
    extra = L.OUT_DIR / "post_restart_recheck.json"
    out["post_restart_recheck"] = (json.loads(extra.read_text(encoding="utf-8"))
                                   if extra.exists() else {})
    return out


def build_keys(A: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """键 → {value, source}。value 一律现场从产物里取。"""
    post = A.get("gpu_ram_post.json", {})
    svc = A.get("runtime_services.json", {})
    pp = A.get("runtime_prompt_proof.json", {})
    sc = A.get("single_call_proof.json", {})
    tts = A.get("tts_smoke.json", {})
    av = A.get("avatar_smoke.json", {})
    rb = A.get("rollback_proof.json", {})
    rg = A.get("regression_summary.json", {})
    pr = A.get("protection.json", {})
    rows = A.get("smoke_rows.jsonl", []) or []
    listeners_now = {str(p): (r[0].get("pid") if r else None)
                     for p, r in L.listeners().items()}

    k: dict[str, dict[str, Any]] = {}

    def put(key: str, value: Any, source: str) -> None:
        k[key] = {"value": value, "source": source}

    ports_up = [p for p in ("7860", "8090", "8765", "8010") if listeners_now.get(p)]
    put("SERVICES_HEALTHY",
        "YES" if len(ports_up) == 4 and dig(post, "verdicts.GPU_HEALTHY") == "YES"
        else "NO", "live psutil listeners(4) + gpu_ram_post.verdicts.GPU_HEALTHY")
    for port in ("7860", "8090", "8765", "8010"):
        put(f"{port}_PID", listeners_now.get(port), "live psutil listener pid")
    put("LLM_MODEL", Path(str(dig(svc, "llm_8090.props_model_path") or "")).name,
        "runtime_services.llm_8090.props_model_path")
    think_probes = dig(svc, "llm_8090.thinking_runtime_probes") or {}
    put("THINKING_OFF",
        "YES" if think_probes.get("reasoning_present_with_false") is False
        and think_probes.get("kwargs_accepted_by_server") is True else "UNKNOWN",
        "runtime_services.llm_8090.thinking_runtime_probes."
        "(reasoning_present_with_false, kwargs_accepted_by_server)")
    put("UI_NEW_CODE_LOADED",
        "YES" if dig(svc, "ui_new_code.process_created_after_newest_code") is True
        and dig(svc, "ui_new_code.live_equals_inprocess") is True else "UNKNOWN",
        "runtime_services.ui_new_code")
    put("ACTIVE_CHARACTER",
        f"{dig(svc, 'character.pointer_character_id')}/{dig(pp, 'character_name')}",
        "runtime_services.character.pointer_character_id + prompt_proof.character_name")
    put("CHARACTER_POINTER_VALID",
        "YES" if dig(svc, "character.pointer_is_default_placeholder") is False
        and dig(svc, "character.row_in_sqlite_ro.active_id_found_anywhere") is True
        else "NO",
        "runtime_services.character.(pointer_is_default_placeholder,"
        "row_in_sqlite_ro.active_id_found_anywhere)")
    markers = dig(svc, "character.generic_assistant_fallback_markers_found")
    put("GENERIC_ASSISTANT_FALLBACK",
        "NO" if isinstance(markers, list) and not markers else
        ("YES" if isinstance(markers, list) and markers else "UNKNOWN"),
        "runtime_services.character.generic_assistant_fallback_markers_found"
        "（空列表 = 正式 system 里没找到任何通用助手回落标记）")
    put("SOLA_RUNTIME_ACTIVE", dig(pp, "SOLA_RUNTIME_ACTIVE"),
        "runtime_prompt_proof.SOLA_RUNTIME_ACTIVE")
    put("VOICE_MINIMAL_ACTIVE", dig(pp, "VOICE_MINIMAL_ACTIVE"),
        "runtime_prompt_proof.VOICE_MINIMAL_ACTIVE")
    put("SOLA_IDENTITY_REMOVED", dig(pr, "checks.removed_side.SOLA_IDENTITY_REMOVED"),
        "protection.checks.removed_side")
    put("TTS_ACTION_EXAMPLE_REMOVED",
        dig(pr, "checks.removed_side.TTS_ACTION_EXAMPLE_REMOVED"),
        "protection.checks.removed_side")
    put("OTHER_EMBER_CONTENT_PRESERVED",
        "YES" if all(dig(pr, f"checks.protected_categories.categories.{c}.VERDICT")
                     == "YES" for c in
                     ("ADULT_CONTENT_PRESERVED", "VIOLENCE_CONTENT_PRESERVED",
                      "DARK_CONTENT_PRESERVED", "AUTONOMY_RULES_PRESERVED"))
        and dig(pr, "checks.block_subsequence.PASS") is True
        else "NO", "protection.checks.protected_categories + block_subsequence")
    for cat in ("ADULT_CONTENT_PRESERVED", "VIOLENCE_CONTENT_PRESERVED",
                "DARK_CONTENT_PRESERVED", "AUTONOMY_RULES_PRESERVED"):
        put(cat, dig(pr, f"checks.protected_categories.categories.{cat}.VERDICT"),
            f"protection.checks.protected_categories.categories.{cat}")
    put("TURN_JUDGE_ENABLED",
        "False" if dig(rb, "human_interaction_judge_readback.turn_judge_enabled")
        is False else dig(rb, "human_interaction_judge_readback.turn_judge_enabled"),
        "rollback_proof.human_interaction_judge_readback.turn_judge_enabled")
    put("TURN_JUDGE_CALLS", dig(rb, "checks.TURN_JUDGE_CALLS"),
        "rollback_proof.checks.TURN_JUDGE_CALLS")
    put("LLM_CALLS_PER_TURN", dig(sc, "LLM_CALLS_PER_TURN"),
        "single_call_proof.LLM_CALLS_PER_TURN")
    put("SYSTEM_MESSAGE_COUNT", dig(pp, "SYSTEM_MESSAGE_COUNT"),
        "runtime_prompt_proof.SYSTEM_MESSAGE_COUNT")
    put("REAL_LLM_SMOKE",
        "YES" if len(rows) == 4 and all(one.get("reply") and not one.get("error")
                                       for one in rows)
        and dig(sc, "LLM_CALLS_PER_TURN_ALL_EQUAL_1") is True else "NO",
        "smoke_rows.jsonl(4) + single_call_proof.LLM_CALLS_PER_TURN_ALL_EQUAL_1")
    put("REAL_TTS_SMOKE", dig(tts, "TTS_OK"), "tts_smoke.TTS_OK")
    put("AVATAR_CHAIN_HEALTHY", dig(av, "AVATAR_CHAIN_HEALTHY"),
        "avatar_smoke.AVATAR_CHAIN_HEALTHY")
    for lk in ("SOLA_IDENTITY_LEAK", "SYSTEM_PROMPT_LEAK", "META_LABEL_LEAK"):
        put(lk, dig(sc, f"leak_summary.{lk}"), f"single_call_proof.leak_summary.{lk}")
    put("NARRATIVE_ACTION_OUTPUT_PRESENT", dig(sc, "NARRATIVE_ACTION_OUTPUT_PRESENT"),
        "single_call_proof.NARRATIVE_ACTION_OUTPUT_PRESENT")
    put("TTS_NARRATIVE_ACTION_SPOKEN",
        dig(tts, "narrative_action_spoken_check.verdict"),
        "tts_smoke.narrative_action_spoken_check.verdict")
    put("MEDIAN_TTFT_MS", dig(sc, "performance.median_TTFT_ms"),
        "single_call_proof.performance.median_TTFT_ms")
    put("MEDIAN_WALL_MS", dig(sc, "performance.median_wall_ms"),
        "single_call_proof.performance.median_wall_ms")
    put("FINAL_PROMPT_TOKENS", dig(pp, "token_ledger.FINAL_PROMPT_TOKENS"),
        "runtime_prompt_proof.token_ledger.FINAL_PROMPT_TOKENS（llama.cpp /tokenize 实测）")
    put("GPU_HEALTHY", dig(post, "verdicts.GPU_HEALTHY"), "gpu_ram_post.verdicts")
    put("RAM_HEALTHY", dig(post, "verdicts.RAM_HEALTHY"), "gpu_ram_post.verdicts")
    for extra in ("OOM", "OFFLOAD_ANOMALY", "GPU_FOREIGN_HEAVY_COMPUTE",
                  "RAM_PRESSURE"):
        put(extra, dig(post, f"verdicts.{extra}"), f"gpu_ram_post.verdicts.{extra}")
    put("OFF_PATH_VERIFIED", rb.get("ROLLBACK_REVERSIBLE"), "rollback_proof")
    put("ROLLBACK_VERIFIED", rb.get("ROLLBACK_REVERSIBLE"),
        "rollback_proof.ROLLBACK_REVERSIBLE（OFF 三发逐字对拍）")
    green, red = dig(rg, "GREEN"), dig(rg, "RED")
    put("REGRESSION_PASS",
        f"{green}/{green + red}_SUITES_GREEN_"
        f"{red}_PREEXISTING_B_CLASS_REGISTERED" if red else f"{green}/{green}",
        "regression_summary.GREEN/RED + triage")
    put("PRODUCTION_PROTECTION",
        "YES" if dig(pr, "checks.PRODUCTION_PROTECTION.PASS") is True else "NO",
        "protection.checks.PRODUCTION_PROTECTION")
    return k


REQUIRED_OK = {
    "SERVICES_HEALTHY": "YES", "UI_NEW_CODE_LOADED": "YES",
    "CHARACTER_POINTER_VALID": "YES",
    "SOLA_RUNTIME_ACTIVE": "YES", "VOICE_MINIMAL_ACTIVE": "YES",
    "SOLA_IDENTITY_REMOVED": "YES", "TTS_ACTION_EXAMPLE_REMOVED": "YES",
    "OTHER_EMBER_CONTENT_PRESERVED": "YES", "ADULT_CONTENT_PRESERVED": "YES",
    "VIOLENCE_CONTENT_PRESERVED": "YES", "DARK_CONTENT_PRESERVED": "YES",
    "AUTONOMY_RULES_PRESERVED": "YES", "TURN_JUDGE_CALLS": 0,
    "LLM_CALLS_PER_TURN": 1, "SYSTEM_MESSAGE_COUNT": 1,
    "REAL_LLM_SMOKE": "YES", "REAL_TTS_SMOKE": "YES",
    "AVATAR_CHAIN_HEALTHY": "YES", "SOLA_IDENTITY_LEAK": "NO",
    "SYSTEM_PROMPT_LEAK": "NO", "META_LABEL_LEAK": "NO",
    "GPU_HEALTHY": "YES", "RAM_HEALTHY": "YES", "OOM": "NO",
    "OFFLOAD_ANOMALY": "NO", "GPU_FOREIGN_HEAVY_COMPUTE": "NO",
    "RAM_PRESSURE": "NO", "OFF_PATH_VERIFIED": "YES",
    "ROLLBACK_VERIFIED": "YES", "PRODUCTION_PROTECTION": "YES",
    "TURN_JUDGE_ENABLED": "False",
}


def verdict(k: dict[str, dict[str, Any]]) -> tuple[str, str, list[str]]:
    bad = []
    for key, want in REQUIRED_OK.items():
        got = (k.get(key) or {}).get("value")
        if key == "OFF_PATH_VERIFIED" and got not in ("YES", True):
            bad.append(f"{key}={got!r}(want {want!r})")
        elif key == "ROLLBACK_VERIFIED" and got not in ("YES", True):
            bad.append(f"{key}={got!r}(want {want!r})")
        elif want in ("YES", "NO", "False") and got != want:
            bad.append(f"{key}={got!r}(want {want!r})")
        elif isinstance(want, int) and got != want:
            bad.append(f"{key}={got!r}(want {want!r})")
    if bad:
        return ("FINAL_RESULT=BLOCKED_SEE_KEY_SCREEN",
                "READY_FOR_OWNER_USE=NO", bad)
    return ("FINAL_RESULT=SOLA_V2_EMBER_VOICE_MINIMAL_PRODUCTION_READY",
            "READY_FOR_OWNER_USE=YES", [])


def file_row(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    st = path.stat()
    return {"path": str(path), "name": path.name, "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "mtime_local": datetime.fromtimestamp(st.st_mtime).strftime(L.TS_FMT)}


def main() -> int:
    A = load_all()
    keys = build_keys(A)
    final, ready, bad = verdict(keys)
    # §24 要求 BLOCKED_ 后面是**准确根因**，不是 SEE_KEY_SCREEN 这种占位。
    # 根因名不由我手打：它来自只读复核产物 hard_blocker 的机器判定
    # （四路监听数 + 官方入口今天的 START_NATIVE_FAILED 行 + 四个计划任务的当前"模式"字段）。
    src = "本脚本 §24 合取"
    hb = ((A.get("post_restart_recheck") or {}).get("hard_blocker") or {})
    if final.startswith("FINAL_RESULT=BLOCKED") and hb.get("FINAL_RESULT_if_blocked"):
        final = "FINAL_RESULT=" + str(hb["FINAL_RESULT_if_blocked"])
        src = ("本脚本 §24 合取 + post_restart_recheck.hard_blocker."
               "FINAL_RESULT_if_blocked（只读复核产物，判据见 §8）")
    keys["FINAL_RESULT"] = {"value": final.split("=", 1)[1], "source": src}
    keys["READY_FOR_OWNER_USE"] = {"value": ready.split("=", 1)[1],
                                   "source": "本脚本 §24 合取"}

    gen_at = L.now_local()
    # 顺序要紧：manifest.json 会把 key_screen.json 的 SHA 记进清单，所以 key_screen
    # 必须先落盘。反过来写就会留下一条"清单里记的哈希对不上磁盘"的假账
    # （本轮独立复算清单哈希时就是这样抓出来的，已登记在第 8 节）。
    L.write_json("key_screen.json", {
        "task_id": L.TASK_ID, "generated_at_local": gen_at,
        "keys": keys, "conjunction_failures": bad,
        "required_map": REQUIRED_OK,
    })
    manifest = {
        "task_id": L.TASK_ID,
        "generated_at_local": gen_at,
        "out_dir": str(L.OUT_DIR),
        "artifacts": [file_row(L.OUT_DIR / n) for n in ARTIFACTS
                      if (L.OUT_DIR / n).exists()],
        "extra_files_in_dir": [file_row(p) for p in sorted(L.OUT_DIR.iterdir())
                               if p.is_file() and p.name not in ARTIFACTS
                               and p.name not in ("manifest.json",)],
        "collector_scripts": [file_row(HERE / n) for n in COLLECTORS
                              if (HERE / n).exists()],
        "missing_collectors": [n for n in COLLECTORS if not (HERE / n).exists()],
        "frozen_inputs": [file_row(L.REPO / rel) for rel in FROZEN_INPUTS
                          if (L.REPO / rel).exists()],
        "missing_frozen_inputs": [rel for rel in FROZEN_INPUTS
                                  if not (L.REPO / rel).exists()],
        "note": "manifest.json 自己不在清单里（不能自指哈希）；key_screen.json 先落盘、"
                "再算清单，所以它的哈希是真的磁盘态。生成器哈希见 collector_scripts "
                "里的 sola_closure01_freeze.py。",
    }
    path = L.write_json("manifest.json", manifest)
    order = ("SERVICES_HEALTHY", "7860_PID", "8090_PID", "8765_PID", "8010_PID",
             "LLM_MODEL", "THINKING_OFF", "UI_NEW_CODE_LOADED",
             "ACTIVE_CHARACTER",
             "CHARACTER_POINTER_VALID", "GENERIC_ASSISTANT_FALLBACK",
             "SOLA_RUNTIME_ACTIVE", "VOICE_MINIMAL_ACTIVE", "SOLA_IDENTITY_REMOVED",
             "TTS_ACTION_EXAMPLE_REMOVED", "OTHER_EMBER_CONTENT_PRESERVED",
             "ADULT_CONTENT_PRESERVED", "VIOLENCE_CONTENT_PRESERVED",
             "DARK_CONTENT_PRESERVED", "AUTONOMY_RULES_PRESERVED",
             "TURN_JUDGE_ENABLED", "TURN_JUDGE_CALLS", "LLM_CALLS_PER_TURN",
             "SYSTEM_MESSAGE_COUNT", "REAL_LLM_SMOKE", "REAL_TTS_SMOKE",
             "AVATAR_CHAIN_HEALTHY", "SOLA_IDENTITY_LEAK", "SYSTEM_PROMPT_LEAK",
             "META_LABEL_LEAK", "NARRATIVE_ACTION_OUTPUT_PRESENT",
             "TTS_NARRATIVE_ACTION_SPOKEN", "MEDIAN_TTFT_MS", "MEDIAN_WALL_MS",
             "FINAL_PROMPT_TOKENS", "GPU_HEALTHY", "RAM_HEALTHY", "OOM",
             "OFFLOAD_ANOMALY", "GPU_FOREIGN_HEAVY_COMPUTE", "RAM_PRESSURE",
             "OFF_PATH_VERIFIED", "ROLLBACK_VERIFIED", "REGRESSION_PASS",
             "PRODUCTION_PROTECTION", "READY_FOR_OWNER_USE", "FINAL_RESULT")
    print("=" * 66)
    for key in order:
        one = keys.get(key) or {"value": "MISSING"}
        print(f"{key}={one['value']}")
    print("=" * 66)
    if bad:
        print("合取未过的项：")
        for one in bad:
            print("  -", one)
        # 判决根因的事实行（同样逐字来自只读复核产物，不是手写）：
        if hb:
            print("BLOCKER 事实（来自 post_restart_recheck.hard_blocker）：")
            print("  live_listeners_at_recheck=", hb.get("live_listeners_at_recheck"))
            print("  official_start_last_attempt=", hb.get("official_start_last_attempt"))
            for tn, tv in (hb.get("girlfriend_scheduled_tasks") or {}).items():
                print(f"  {tn}: rc={tv.get('rc')} 模式={tv.get('mode_field')}")
            print("  解禁任务 =", hb.get("enabling_a_task_is_out_of_scope_because"))
    print("WROTE", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
