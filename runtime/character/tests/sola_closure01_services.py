# -*- coding: utf-8 -*-
"""services + llm + ui-code + character — 任务 §4 §5 §6 §8 的现场证据。

产出：
  runtime_services.json      四服务现场态 + 8090 LLM 证据（model/argv/thinking）+ 7860 新代码 + 人物
  （§9/§10 的 system 证明在 sola_closure01_prompt.py）

全部只读，唯一例外：§5 的 thinking 探针会向 8090 发 2 次 max_tokens=1 的最小请求
（不动任何设置、不产正文），并在证据里逐字记下它们的形状与用途。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sola_closure01_lib as L  # noqa: E402

# 生产代码里决定「正式请求带不带 enable_thinking=false」的那几处，逐字引用行号
S2S_BASE = ("runtime/core/venv/Lib/site-packages/speech_to_speech/LLM/"
            "base_openai_compatible_language_model.py")
S2S_ARGS = ("runtime/core/venv/Lib/site-packages/speech_to_speech/arguments_classes/"
            "responses_api_language_model_arguments.py")

# 通用 assistant 兜底话术的特征串（出现即说明没在用人物的话）
GENERIC_ASSISTANT_MARKERS = [
    "你是一位友好、自然的中文语音助手",
    "You are a helpful assistant",
    "友好的语音助手",
]

# 生产链路的代码文件：7860 必须比这些更「新」
PRODUCTION_CODE_FILES = [
    "runtime/character/context_builder.py",
    "runtime/core/providers/contracts.py",
    "runtime/character/sola_v2_ember/adapter.py",
    "runtime/character/sola_v2_ember/__init__.py",
    "runtime/character/sola_v2_ember/voice_minimal.py",
    "runtime/character/sola_v2_ember/ember.py",
    "runtime/character/sola_v2_ember/macros.py",
    "runtime/ui/app/server.py",
    "runtime/ui/app/character_admin.py",
]


def s2s_thinking_source_evidence() -> dict:
    """从 8765 真正加载的那份源码里，抠出「生产请求带什么 extra_body」的逐字证据。"""
    out: dict = {}
    try:
        base = (L.REPO / S2S_BASE).read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return {"error": repr(exc)}
    def grab(pattern: str, span: int = 3) -> list[str]:
        rows = []
        for i, line in enumerate(base, 1):
            if re.search(pattern, line):
                rows.append(f"{i}: " + "\n        ".join(base[i - 1:i - 1 + span]))
        return rows[:6]
    out["base_file"] = S2S_BASE
    out["base_sha256"] = L.sha256_file(L.REPO / S2S_BASE)
    out["setup_disable_thinking_default"] = grab(r"disable_thinking: bool =")
    out["build_extra_body"] = grab(r"if disable_thinking:", 2)
    out["returns_chat_template_kwargs"] = grab(r'chat_template_kwargs"', 1) or grab(
        r"chat_template_kwargs.*enable_thinking.*False", 1)
    out["official_openai_short_circuit"] = grab(r"def _is_official_openai", 1)
    try:
        args_txt = (L.REPO / S2S_ARGS).read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        out["args_error"] = repr(exc)
        return out
    out["args_file"] = S2S_ARGS
    out["args_sha256"] = L.sha256_file(L.REPO / S2S_ARGS)
    for i, line in enumerate(args_txt, 1):
        if "responses_api_disable_thinking" in line:
            out["args_disable_thinking_default"] = "\n        ".join(
                f"{j}: {t}" for j, t in zip(range(i, i + 3), args_txt[i - 1:i + 2]))
            break
    return out


def llm_thinking_probes() -> dict:
    """两发最小请求对拍：不带 kwargs（模板默认）vs 带 enable_thinking=false（生产形状）。

    判据是 usage.prompt_tokens 与 message.reasoning_content —— 不发正文、不设温度，
    所以这两发不进入 §12 那四条 smoke 的计数。
    """
    msgs = [{"role": "user", "content": "只回一个字：好"}]
    out: dict = {}
    variants = {
        "no_kwargs": {"model": "gemma4", "messages": msgs, "max_tokens": 1,
                      "temperature": 0.0},
        "enable_thinking_false": {"model": "gemma4", "messages": msgs, "max_tokens": 1,
                                  "temperature": 0.0,
                                  "chat_template_kwargs": {"enable_thinking": False}},
    }
    for name, body in variants.items():
        r = L.http_json("http://127.0.0.1:8090/v1/chat/completions", payload=body,
                        timeout=120)
        j = r.get("json") or {}
        choice = (j.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        out[name] = {
            "request_body": body,
            "ok": r["ok"], "status": r.get("status"), "elapsed_ms": r.get("elapsed_ms"),
            "id": j.get("id"),
            "usage": j.get("usage"),
            "content": message.get("content"),
            "reasoning_content": message.get("reasoning_content"),
            "finish_reason": choice.get("finish_reason"),
            "raw_error": j.get("error") if not r["ok"] else None,
        }
    a, b = out["no_kwargs"], out["enable_thinking_false"]
    pa = (a.get("usage") or {}).get("prompt_tokens")
    pb = (b.get("usage") or {}).get("prompt_tokens")
    out["prompt_tokens_delta_false_minus_default"] = (None if pa is None or pb is None
                                                     else pb - pa)
    out["kwargs_accepted_by_server"] = bool(b["ok"])
    out["reasoning_present_default"] = bool((a.get("reasoning_content") or "").strip())
    out["reasoning_present_with_false"] = bool((b.get("reasoning_content") or "").strip())
    return out


def ui_new_code_proof(listeners: dict) -> dict:
    rows = listeners.get(7860) or []
    if not rows:
        return {"UI_NEW_CODE_LOADED": "NO_LISTENER"}
    ui = rows[0]
    code = [L.file_fact(rel) for rel in PRODUCTION_CODE_FILES]
    newest = max((c["mtime_epoch"] for c in code if c.get("exists")), default=None)
    newest_row = next((c for c in code
                       if c.get("mtime_epoch") == newest), None)
    created = float(ui.get("created_epoch") or 0)
    # 在线运行路径：让**在听的那个进程**自己回答「现在这个人该怎么说话」，
    # 再和在别的解释器里现算出来的那块对拍 —— 老进程里没有 sola_text 这个字段，
    # 也就绝不可能吐出这一块，所以这一步同时证明了「加载的是新代码」。
    live = L.http_json("http://127.0.0.1:7860/api/character/prompt", timeout=60)
    live_text = str(((live.get("json") or {}).get("内容")) or "")
    expected = inprocess_expected_prompt()
    return {
        "ui_process": {k: ui.get(k) for k in ("pid", "name", "created", "created_epoch",
                                              "cmdline", "cwd")},
        "ui_has_reload_flag": "--reload" in str(ui.get("cmdline") or ""),
        "production_code_files": code,
        "newest_production_code": {"path": (newest_row or {}).get("path"),
                                   "mtime": (newest_row or {}).get("mtime"),
                                   "mtime_epoch": newest},
        "process_created_after_newest_code": (None if newest is None
                                              else bool(created > newest)),
        "delta_seconds_created_minus_newest_mtime": (None if newest is None
                                                     else round(created - newest, 1)),
        "live_endpoint": {
            "url": "http://127.0.0.1:7860/api/character/prompt",
            "ok": live["ok"], "status": live.get("status"),
            "chars": len(live_text),
            "sha256": L.sha256_bytes(live_text.encode("utf-8")),
        },
        "inprocess_expected": expected,
        "live_equals_inprocess": (live_text == (expected or {}).get("system_text")),
    }


def inprocess_expected_prompt() -> dict | None:
    """在**独立解释器**里用同一套生产原语现算一份（不是 7860 那份内存，用来对拍）。"""
    try:
        L.bare_import_root()
        from runtime.character import context_builder  # type: ignore
        from runtime.character import store  # type: ignore
    except Exception as exc:
        return {"error": repr(exc)[:300]}
    try:
        cdb = L.REPO / "runtime" / "character" / "data" / "characters.sqlite3"
        ident = store.get_active_character(db_path=cdb)
        # 不传任何 db_path：让 store / memory_service 各用自己的默认位置 ——
        # 这与 :7860 完全一致（server.py 从不调用 configure()，见
        # character_admin.configure 的「只在自测里用」）。
        ctx = context_builder.build(character_id=ident, session_id="", user_text="",
                                    query="")
        rows = context_builder.messages(ctx, temporary_state=None)
        system_text = ctx.system_text()
        return {
            "character_id": ident,
            "db_paths_used": "默认（characters.sqlite3 / memories.sqlite3）",
            "chars": len(system_text),
            "sha256": L.sha256_bytes(system_text.encode("utf-8")),
            "system_text": system_text,
            "message_roles": [str(r.get("role")) for r in rows],
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}


def character_proof() -> dict:
    pointer = L.read_json_file("runtime/character/data/active_character.json")
    ident = (pointer or {}).get("character_id") if isinstance(pointer, dict) else None
    cdb = L.REPO / "runtime" / "character" / "data" / "characters.sqlite3"
    in_db = None
    name = None
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{cdb.as_posix()}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            hits = []
            for tbl in tables:
                cols = [r[1] for r in con.execute(f"PRAGMA table_info({tbl})").fetchall()]
                key = next((c for c in ("character_id", "id", "subject_id") if c in cols), None)
                if not key:
                    continue
                try:
                    n = con.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {key}=?",
                                    (ident,)).fetchone()[0]
                except Exception:
                    continue
                if n:
                    hits.append({"table": tbl, "key": key, "rows": int(n)})
            in_db = {"tables": tables, "tables_holding_active_id": hits,
                     "active_id_found_anywhere": bool(hits)}
        finally:
            con.close()
    except Exception as exc:
        in_db = {"error": repr(exc)[:300]}
    live = L.http_json("http://127.0.0.1:7860/api/character/prompt", timeout=60)
    j = live.get("json") or {}
    text = str(j.get("内容") or "")
    state = L.http_json("http://127.0.0.1:7860/api/character/state", timeout=30)
    sj = state.get("json") or {}
    generic = [m for m in GENERIC_ASSISTANT_MARKERS if m in text]
    return {
        "pointer": pointer,
        "pointer_character_id": ident,
        "pointer_is_default_placeholder": (ident in (None, "", "default")),
        "row_in_sqlite_ro": in_db,
        "live_prompt_endpoint": {
            "ok": live["ok"], "status": live.get("status"),
            "编号": j.get("编号"), "人物名称": j.get("人物名称"),
            "字数": j.get("字数"), "已用长度": j.get("已用长度"),
            "长度上限": j.get("长度上限"),
            "content_sha256": L.sha256_bytes(text.encode("utf-8")) if text else None,
        },
        "state_endpoint": {
            "ok": state["ok"], "当前人物": sj.get("当前人物"),
            "人物名称": sj.get("人物名称"),
            "人物列表条数": (len(sj.get("人物列表") or []) if isinstance(sj.get("人物列表"), list)
                          else None),
        },
        "generic_assistant_fallback_markers_found": generic,
        "character_id_matches_pointer": (str(j.get("编号") or "") == str(ident or "")),
    }


def service_health() -> dict:
    probes = {
        "7860_config": "http://127.0.0.1:7860/api/config",
        "7860_capabilities_LLM": "http://127.0.0.1:7860/api/capabilities?kind=LLM",
        "7860_voice_models": "http://127.0.0.1:7860/api/voice-models",
        "8090_models": "http://127.0.0.1:8090/v1/models",
        "8090_props": "http://127.0.0.1:8090/props",
        "8765_pool": "http://127.0.0.1:8765/v1/pool",
        "8010_health": "http://127.0.0.1:8010/health",
        "8010_root": "http://127.0.0.1:8010/",
    }
    out = {}
    for key, url in probes.items():
        r = L.http_json(url, timeout=20)
        out[key] = {"url": url, "ok": r["ok"], "status": r.get("status"),
                    "elapsed_ms": r.get("elapsed_ms"),
                    "error": r.get("error"),
                    "preview": L.short(json.dumps(r.get("json"), ensure_ascii=False)
                                       if r.get("json") is not None
                                       else (r.get("body_text") or ""), 400)}
    return out


def main() -> int:
    listeners = L.listeners()
    services = {
        str(port): {"listening": bool(rows), "listeners": rows,
                    "tcp_connect": L.port_open(port)}
        for port, rows in listeners.items()
    }
    props = L.http_json("http://127.0.0.1:8090/props", timeout=20).get("json") or {}
    models = L.http_json("http://127.0.0.1:8090/v1/models", timeout=20).get("json") or {}
    rows8090 = listeners.get(8090) or []
    llm = rows8090[0] if rows8090 else {}
    active_model = L.read_json_file("runtime/llm/active_model.json")
    sola_cfg = L.read_json_file("runtime/character/sola_v2_ember/sola_v2_ember.json")
    hi = L.read_json_file("runtime/human_interaction/human_interaction.json")
    tts_backend_file = L.REPO / "runtime/windows/active_tts_backend.txt"
    avatar_state = L.read_json_file("runtime/windows/avatar_runtime_state.json")
    tts_switch = L.read_json_file("runtime/windows/tts_switch_state.json")

    out = {
        "task_id": L.TASK_ID,
        "stages": ["4_service_recovery", "5_llm_confirmation",
                   "6_ui_new_code", "7_final_switch_state", "8_active_character"],
        "collected_at_local": L.now_local(),
        "collected_at_utc": L.now_utc_z(),
        "services": services,
        "health_probes": service_health(),
        "llm_8090": {
            "LLM_PID": llm.get("pid"),
            "LLM_CREATED": llm.get("created"),
            "LLM_ARGV": llm.get("cmdline"),
            "LLM_EXE": llm.get("exe"),
            "active_model_pointer": active_model,
            "props_model_path": props.get("model_path"),
            "props_model_alias": props.get("model_alias"),
            "props_ftype": props.get("model_ftype"),
            "props_build": props.get("build_info"),
            "props_total_slots": props.get("total_slots"),
            "props_ctx_note": "n_ctx_slot=8192 记录在启动日志里；/props 不含该键",
            "v1_models": models,
            "thinking_source_evidence": s2s_thinking_source_evidence(),
            "thinking_runtime_probes": llm_thinking_probes(),
            "argv_contains_port_8090": bool(re.search(r"--port\s+8090", str(llm.get("cmdline")))),
        },
        "switch_final_state": {
            "SOLA_V2_EMBER_ENABLED": (sola_cfg or {}).get("sola_v2_ember_enabled"),
            "SOLA_V2_EMBER_VOICE_MINIMAL": (sola_cfg or {}).get("voice_minimal_enabled"),
            "TURN_JUDGE_ENABLED": (hi or {}).get("turn_judge_enabled"),
            "HUMAN_INTERACTION_FEATURE_ENABLED": (hi or {}).get("feature_enabled"),
            "REVISE_ENABLED": (hi or {}).get("revise_enabled"),
            "user_label": (sola_cfg or {}).get("user_label"),
            "char_label_fallback": (sola_cfg or {}).get("char_label_fallback"),
        },
        "tts_and_avatar": {
            "active_tts_backend": (tts_backend_file.read_text(encoding="utf-8").strip()
                                   if tts_backend_file.exists() else None),
            "tts_switch_state": {k: (tts_switch or {}).get(k)
                                 for k in ("phase", "result", "updated_at")}
            if isinstance(tts_switch, dict) else tts_switch,
            "avatar_runtime_state": {k: (avatar_state or {}).get(k) for k in
                                     ("generation", "state", "listener_pid", "avatar_id")}
            if isinstance(avatar_state, dict) else avatar_state,
        },
        "ui_new_code": ui_new_code_proof(listeners),
        "character": character_proof(),
    }
    path = L.write_json("runtime_services.json", out)
    print("WROTE", path)
    print("services:", {k: (v["listeners"] and [r["pid"] for r in v["listeners"]])
                        for k, v in out["services"].items()})
    print("LLM_PID", out["llm_8090"]["LLM_PID"], "created", out["llm_8090"]["LLM_CREATED"])
    tp = out["llm_8090"]["thinking_runtime_probes"]
    print("thinking probes:", {k: (tp[k].get("usage"), bool((tp[k].get("reasoning_content") or "").strip()))
                               for k in ("no_kwargs", "enable_thinking_false")})
    print("delta", tp.get("prompt_tokens_delta_false_minus_default"),
          "accepted", tp.get("kwargs_accepted_by_server"))
    unc = out["ui_new_code"]
    print("UI_NEW_CODE: created_after_code=", unc["process_created_after_newest_code"],
          "delta_s=", unc["delta_seconds_created_minus_newest_mtime"],
          "live_eq_inproc=", unc["live_equals_inprocess"])
    ch = out["character"]
    print("character:", ch["live_prompt_endpoint"].get("人物名称"),
          ch["live_prompt_endpoint"].get("字数"),
          "fallback_markers=", ch["generic_assistant_fallback_markers_found"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
