# -*- coding: utf-8 -*-
"""§18 那十个 UNCHANGED 位：全部现场重算，不引用谁写过的话。

每面位只跟两样东西比：**HUMAN_INTERACTION_BASELINE.json**（动手之前抓的那份）和
**runtime/human_interaction_baseline_files.json**（4088 个代码/配置文件的 SHA256）。

两个必须讲清楚的方法问题：

  1. 清单里有两个文件字节确实变了 —— `runtime/windows/avatar_runtime_state.json`、
     `runtime/windows/service_pids.json`。它们是运行时状态，数字人 supervisor 和启停
     脚本本来就会反复重写，字节变了不等于行为变了。所以这两个不按「变了就 NO」处理，
     而是**取里面的值**逐项对基线（generation / state / listener_pid / avatar_id）；
     值对不上才算真变了。
  2. 证据项一律写成布尔。端口号、sha 这类原始数值单独放，不参与定档——
     否则「HTTP 200」或者「HTTP -1」都会被当成一条真话混进判决里。
  3. 判据不许拿**易变的生产数据**当代理指标。这一版改掉了两处：
       * UI 那面位原来读 `GET /api/character/prompt?q=`，也就是「指针指向的那个人」的装配
         结果。用户在人物面板里连删三次人物 ⇒ 指针被写成库里不存在的 "default" ⇒ 接口回
         `NOT_THERE` 0 字 ⇒ 假报「UI 基线行为变了」。现在改成显式锚定基线抓的那人物，
         重算基线那四格；指针状态单列为归属登记，不参与定档。
       * MEMORY 那面位原来断「基线之后这文件没被写过」（mtime）。清一次记忆就打成 NO，
         可 §18 问的是 **schema**，不是字节。现在换成正向证明：用当前定结构的代码新建一个
         库，和生产库逐条对 `sqlite_master`，一致才算——再加「这段代码自基线逐字节没变」。

定档词：YES / NO / NOT_EVALUABLE（缺依据就明说缺，不猜）。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASELINE_PATH = ROOT / "HUMAN_INTERACTION_BASELINE.json"
FILES_PATH = ROOT / "runtime" / "human_interaction_baseline_files.json"
OUT_PATH = HERE / "out" / "protection_check.json"

# 允许字节变化的运行时状态文件（值必须另算）
STATE_ONLY = {"runtime/windows/avatar_runtime_state.json", "runtime/windows/service_pids.json"}
REF_AUDIO = ROOT / "models/native/qwen3/ref.wav"


def sha_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "UNREADABLE"


def http(url: str, timeout: int = 12) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except Exception as exc:
        return -1, f"{type(exc).__name__}: {exc}".encode("utf-8")


def ports_and_cmds() -> dict[int, tuple[list[int], str, list[str]]]:
    """端口 → (在听的 pid, 命令行原文, argv 令牌表)。

    一定要拿 psutil 的 argv **列表**而不是拼好的字符串：`--model_name "Gemma4 12B"` 这种
    带空格的值，拼成字符串再切就会裂成两枚令牌，跟基线（WMI 原文，带引号）一比就成了
    「参数被改过」——那是我这边的记法错，不是进程真的变了。
    判活用 psutil，不用 os.kill(pid,0)（那是终结者）。
    """
    import psutil
    found: dict[int, list[int]] = {}
    for conn in psutil.net_connections(kind="tcp"):
        if conn.status == psutil.CONN_LISTEN and conn.laddr:
            bucket = found.setdefault(int(conn.laddr.port), [])
            if conn.pid and conn.pid not in bucket:
                bucket.append(int(conn.pid))
    out: dict[int, tuple[list[int], str, list[str]]] = {}
    for port, pids in found.items():
        line, tokens = "", []
        if pids:
            try:
                tokens = [str(one).replace("\\", "/") for one in psutil.Process(pids[0]).cmdline()]
                line = " ".join(tokens)
            except Exception as exc:
                line = f"UNREADABLE {type(exc).__name__}: {exc}"
        out[port] = (pids, line, tokens)
    return out


def argv_tokens(raw: str, since_flag: str | None = None) -> list[str]:
    """命令行原文 → 参数令牌（基线那份是 WMI 字符串，只能这么切）。"""
    import shlex
    try:
        tokens = [one for one in shlex.split(str(raw or "").replace("\\", "/")) if one.strip()]
    except ValueError:
        tokens = [one for one in str(raw or "").replace("\\", "/").split() if one.strip()]
    if since_flag and since_flag in tokens:
        tokens = tokens[tokens.index(since_flag):]
    return tokens


def tail_from(tokens: list[str], since_flag: str) -> list[str]:
    return tokens[tokens.index(since_flag):] if since_flag in tokens else list(tokens)


def first_diff(now: list[str], base: list[str]) -> dict[str, Any]:
    """两串参数第一次对不上的位置——定档失败时要能一眼看出是哪枚令牌，不靠猜。"""
    for index in range(min(len(now), len(base))):
        if now[index] != base[index]:
            return {"index": index, "now": now[index], "base": base[index],
                    "len_now": len(now), "len_base": len(base)}
    if len(now) != len(base):
        extra = now[len(base):] if len(now) > len(base) else base[len(now):]
        return {"index": min(len(now), len(base)), "extra": extra,
                "len_now": len(now), "len_base": len(base)}
    return {"index": -1, "len_now": len(now), "len_base": len(base)}


def file_mtime(path: Path) -> str:
    try:
        return _dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return "MISSING"


def inventory_diff(old: dict[str, Any]) -> dict[str, list[str]]:
    changed, deleted = [], []
    for rel, meta in old.items():
        path = ROOT / rel
        if not path.exists():
            deleted.append(rel)
        elif sha_file(path) != meta["sha256"]:
            changed.append(rel)
    return {"changed": sorted(changed), "deleted": sorted(deleted)}


def schema_fingerprint(path: Path) -> str:
    """库的结构（表/列/索引/视图）摘成一个 sha，只读打开，不写任何东西。"""
    if not path.exists():
        return "MISSING"
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rows = [list(map(str, one)) for one in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")]
    finally:
        conn.close()
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def schema_built_by_current_code(live_db: Path, kind: str) -> str:
    """正向证明用的另一半：**当前**这份定结构的代码在新位置建出来的库长什么样。

    §18 问的是「长期记忆 DB schema 没被改」，不是「这个 .sqlite3 文件的字节没变」——
    用户在界面里删一个人物、清一次记忆就会改字节，那是数据生命周期，不是结构变更。
    所以这里不拿 mtime 当判据，而是把生产库的 sqlite_master 和「代码新建库」的
    sqlite_master 逐条对拍：一致 ⇒ 生产结构就是代码声明的结构；
    再加上「这段定结构的代码自基线逐字节没变」（inventory 判），结构没动过才成立。
    """
    import shutil
    import tempfile

    from runtime.character import store as character_store
    from runtime.memory import service as memory_service

    tmp = Path(tempfile.mkdtemp(prefix="hi_schema_"))
    try:
        if kind == "memory":
            built = Path(memory_service.init_db(tmp / "mem.sqlite3"))
        else:
            conn = character_store.init_and_connect(tmp / "char.sqlite3")
            conn.close()
            built = tmp / "char.sqlite3"
        return schema_fingerprint(built)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def reanchor_prompt_cells() -> dict[str, Any]:
    """把基线那四格**按显式人物号**重算，不让它跟着 active_character 指针走。

    原来这一位读的是 `GET /api/character/prompt?q=`，也就是「当前这个人」的装配结果。
    用户 23:59 在人物面板里连删三次人物，`store.delete_character()` 发现被删的正是
    当前人物，就把指针写成 DEFAULT_CHARACTER_ID（`allow_missing=True`），而库里没有
    "default" 这个人 ⇒ 接口回 `NOT_THERE`、内容 0 字 ⇒ 判成「UI 基线行为变了」。
    那是**数据被改**，不是**装配逻辑被改**，所以判据必须锚死基线抓的是哪个人。
    """
    path = ROOT / "runtime" / "human_interaction_baseline_prompt.json"
    if not path.exists():
        return {"available": False}
    record = json.loads(path.read_text(encoding="utf-8"))
    ident = str(record.get("active_character") or "")
    from runtime.character import context_builder

    turns = {"no_user_text": "",
             "casual": "今天加班到十点才回来，累死了",
             "short": "嗯",
             "long": "我跟你说个事，我妈今天又打电话来问我什么时候结婚，我说我现在工作忙她不信，"
                     "然后我们就吵起来了，我觉得她根本不理解我"}
    history = [{"role": "user", "text": "你还在生我气啊"},
               {"role": "assistant", "text": "没有，就是有点不高兴。"}]
    cells: dict[str, bool] = {}
    shas: dict[str, str] = {}
    for label, user_text in turns.items():
        was = (record.get("prompt_baseline") or {}).get(label) or {}
        try:
            ctx = context_builder.build(character_id=ident, user_text=user_text,
                                        history=history if label == "long" else ())
            rows = context_builder.messages(ctx, temporary_state=None)
            text = "\n\n".join(str(one.get("text") or "").strip() for one in rows
                               if str(one.get("role")) == "system").strip()
            digest = sha_bytes(text.encode("utf-8")) if text else ""
            shas[label] = digest
            cells[label] = bool(digest) and digest == was.get("sha256")
        except Exception:
            cells[label] = False
            shas[label] = "ERROR"
    return {"available": True, "character_id": ident, "cells": cells, "shas": shas,
            "all_identical": bool(cells) and all(cells.values())}


def pointer_health() -> dict[str, Any]:
    """当前人物指针指向谁、那个人在不在库里——登记事实，不参与定档。"""
    path = ROOT / "runtime" / "character" / "data" / "active_character.json"
    try:
        ident = str(json.loads(path.read_text(encoding="utf-8")).get("character_id") or "")
    except Exception:
        return {"pointer_readable": False, "character_id": "", "profile_present": False}
    from runtime.character import store
    return {"pointer_readable": True, "character_id": ident,
            "profile_present": store.get_profile(ident, db_path=character_db_path()) is not None}


def character_db_path() -> Path:
    return ROOT / "runtime" / "character" / "data" / "characters.sqlite3"


def main() -> int:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    files = {str(k).replace("\\", "/"): v
             for k, v in json.loads(FILES_PATH.read_text(encoding="utf-8"))["files"].items()}
    voice = baseline.get("voice_chain_baseline") or {}
    avatar_base = voice.get("avatar_runtime_state") or {}
    llm_base = json.loads(baseline.get("llm_8090_process") or "{}")

    net = ports_and_cmds()
    inv = inventory_diff(files)
    code_changed = [one for one in inv["changed"] if one not in STATE_ONLY]

    def changed_under(prefixes: tuple[str, ...]) -> list[str]:
        return [one for one in code_changed if one.startswith(prefixes)]

    def live(port: int) -> tuple[list[int], str, list[str]]:
        return net.get(port, ([], "", []))

    status_models, body_models = http("http://127.0.0.1:8090/v1/models")
    status_ui, _ = http("http://127.0.0.1:7860/api/config")
    status_s2s, _ = http("http://127.0.0.1:8765/v1/pool")
    status_avatar, _ = http("http://127.0.0.1:8010/", timeout=8)
    status_prompt, body_prompt = http("http://127.0.0.1:7860/api/character/prompt?q=")
    try:
        prompt_text = str(json.loads(body_prompt.decode("utf-8")).get("内容") or "")
    except Exception:
        prompt_text = ""

    avatar_state = json.loads((ROOT / "runtime/windows/avatar_runtime_state.json")
                              .read_text(encoding="utf-8"))
    switch_state = json.loads((ROOT / "runtime/windows/tts_switch_state.json")
                              .read_text(encoding="utf-8"))
    active_model = json.loads((ROOT / "runtime/llm/active_model.json").read_text(encoding="utf-8"))
    memory_db = ROOT / "runtime/memory/data/memories.sqlite3"
    character_db = ROOT / "runtime/character/data/characters.sqlite3"

    # 8090 的命令行：基线是 WMI 记法（一整串，带引号），现场是 psutil 记法（argv 列表）。
    # 现场直接用列表，**不要**把列表拼回字符串再切——`--model_name "Gemma4 12B"` 那种
    # 带空格的值会在拼接时丢掉引号，再切就裂成两枚令牌，一比就假报「参数被改过」。
    llm_tokens_now = live(8090)[2]
    llm_tokens_base = argv_tokens(llm_base.get("CommandLine") or "")
    # 8765 的进程由 venv launcher 转手，psutil 那份会多出 python.exe + 脚本两枚前缀，
    # 所以只比 --mode 往后的参数部分。
    s2s_tail_now = tail_from(live(8765)[2], "--mode")
    s2s_tail_base = argv_tokens(voice.get("s2s_commandLine") or "", since_flag="--mode")
    s2s_exe_now = str(live(8765)[1]).replace("\\", "/")
    s2s_exe_base = str(voice.get("s2s_commandLine") or "").replace("\\", "/")

    try:
        model_ids_now = sorted(str(one.get("id") or one.get("name") or "")
                               for one in (json.loads(body_models.decode("utf-8")).get("data") or []))
    except Exception:
        model_ids_now = []
    model_ids_base = sorted(str(one) for one in baseline.get("llm_8090_health", {}).get("model_ids") or [])

    memory_db_mtime = file_mtime(memory_db)
    # 基线那份把 pre_existing_memory_db_sha256 记成了空对象：没有可对照的哈希，
    # 所以这一面位不能用「字节相同」来断，改走「结构和当前代码建出来的库逐条一致」。
    memory_baseline_usable = isinstance(baseline.get("pre_existing_memory_db_sha256"), str)
    anchored = reanchor_prompt_cells()
    memory_schema_live = schema_fingerprint(memory_db)
    memory_schema_match = (memory_schema_live != "MISSING"
                           and memory_schema_live == schema_built_by_current_code(memory_db, "memory"))
    character_schema_live = schema_fingerprint(character_db)
    character_schema_match = (character_schema_live != "MISSING"
                              and character_schema_live
                              == schema_built_by_current_code(character_db, "character"))
    pointer = pointer_health()

    evidence: dict[str, dict[str, Any]] = {
        "LLM_8090_UNCHANGED": {
            "listener_present": len(live(8090)[0]) > 0,
            "argv_tokens_identical_to_baseline": bool(llm_tokens_now) and llm_tokens_now == llm_tokens_base,
            "health_200": bool(status_models == 200),
            "loaded_model_ids_identical": bool(model_ids_now) and model_ids_now == model_ids_base,
            "no_code_changed": not changed_under(("runtime/llm/", "runtime/core/providers")),
        },
        "TTS_8765_UNCHANGED": {
            "listener_present": len(live(8765)[0]) > 0,
            "argv_tokens_identical_to_baseline": bool(s2s_tail_now) and s2s_tail_now == s2s_tail_base,
            "executable_path_present_in_both": ("speech-to-speech.exe" in s2s_exe_now
                                                 and "speech-to-speech.exe" in s2s_exe_base),
            "committed_backend_identical": bool(
                (ROOT / "runtime/windows/active_tts_backend.txt").read_text(encoding="utf-8").strip()

                == voice.get("active_tts_backend")),
            "pool_200": bool(status_s2s == 200),
            "no_code_changed": not changed_under(("runtime/core/providers",)),
        },
        "AVATAR_8010_UNCHANGED": {
            "listener_present": len(live(8010)[0]) > 0,
            "http_ok": bool(status_avatar == 200),
            "generation_identical": bool(avatar_state.get("generation") == avatar_base.get("generation")),
            "state_identical": bool(avatar_state.get("state") == avatar_base.get("state")),
            "listener_pid_identical": bool(avatar_state.get("listener_pid") == avatar_base.get("listener_pid")),
            "avatar_id_identical": bool(avatar_state.get("avatar_id") == avatar_base.get("avatar_id")),
            "no_code_changed": not changed_under(("runtime/avatar",)),
        },
        "UI_7860_BASELINE_BEHAVIOR_UNCHANGED": {
            "config_200": bool(status_ui == 200),
            "prompt_endpoint_200": bool(status_prompt == 200),
            # 判据锚在「基线那四格的装配结果」上，显式指定人物号，不跟指针走：
            # 指针会被用户在人物面板里删人物带进悬空态，那是数据生命周期，不是装配行为。
            "four_baseline_cells_byte_identical": bool(anchored.get("all_identical")),
            # 同一份装配结果另有一份基线（动手之前直接从 HTTP 面位抓的那条 599 字），
            # 两边都要对上，不能只跟自己写的那份基线自证。
            "matches_http_capture_baseline": bool(
                (anchored.get("shas") or {}).get("no_user_text")
                == (baseline.get("text_chat_baseline_7860") or {}).get("sha256")),
            "endpoint_still_serves_http_200": bool(status_prompt == 200),
            "no_code_changed": not changed_under(("runtime/ui",)),
        },
        "LLM_SWITCH_UNCHANGED": {
            "switcher_in_inventory_and_unchanged": bool(
                "runtime/llm/Switch-Girlfriend-LLM.ps1" in files
                and "runtime/llm/Switch-Girlfriend-LLM.ps1" not in code_changed),
            "registry_unchanged": bool("runtime/llm/models.json" not in code_changed),
            "active_model_value_identical": bool(active_model.get("active_model")
                                              == (voice.get("active_model") or {}).get("active_model")),
            "models_admin_unchanged": bool("runtime/ui/app/models_admin.py" not in code_changed),
        },
        "TTS_SWITCH_UNCHANGED": {
            "controller_unchanged": bool("runtime/ui/app/voice_models_admin.py" not in code_changed),
            "executor_unchanged": bool("runtime/windows/Switch-Girlfriend-Voice.ps1" not in code_changed),
            "journal_terminal_or_idle": bool(str(switch_state.get("phase") or "") in
                                          {"IDLE", "COMMITTED", "ROLLED_BACK", "FAILED", "ABORTED_STALE"}),
        },
        "MEMORY_SCHEMA_UNCHANGED": {
            # §18 问的是**结构**没被动过。原先这里第二条拿 mtime 当代理指标
            # （「基线之后这文件没被写过」），用户在界面里清一次记忆就能把它打成 NO——
            # 那是数据生命周期，不是 schema 变更，代理指标指错了地方。
            # 现在换成正向证明：用**当前**定结构的代码新建一个库，和生产库逐条对 sqlite_master。
            "schema_defining_code_unchanged": (
                "runtime/memory/service.py" not in code_changed
                and "runtime/character/store.py" not in code_changed),
            "memory_schema_equals_what_current_code_builds": memory_schema_match,
            "character_schema_equals_what_current_code_builds": character_schema_match,
            "memory_db_exists": memory_db.exists(),
        },
        "ROLE_STATE_COMPATIBLE": {
            "role_state_unchanged": bool("runtime/llm/role_state.py" not in code_changed),
            "bridge_unchanged": bool("runtime/character/role_state_bridge.py" not in code_changed),
            "temporary_text_still_reaches_context": bool(_temporary_state_still_builds()),
        },
        "VOICE_CHAIN_UNCHANGED": {
            "ref_audio_sha_identical": bool(sha_file(REF_AUDIO) == voice.get("ref_audio_sha256")),
            "s2s_reachable": bool(status_s2s == 200),
            "memory_bridge_unchanged": bool("runtime/llm/memory_bridge.py" not in code_changed),
            "start_script_unchanged": bool("scripts/Start-AI-Girlfriend.ps1" not in code_changed),
        },
        "DIGITAL_HUMAN_UNCHANGED": {
            "active_avatar_identical": bool(avatar_state.get("avatar_id") == voice.get("active_avatar")),
            "http_ok": bool(status_avatar == 200),
            "avatar_code_unchanged": not changed_under(("runtime/avatar",)),
        },
    }

    raw = {
        "checked_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "listeners": {str(port): {"pids": live(port)[0], "cmdline_head": live(port)[1][:220]}
                      for port in (8090, 8765, 8010, 7860)},
        "inventory_universe": len(files),
        "inventory_changed_all": inv["changed"],
        "inventory_changed_code": code_changed,
        "inventory_deleted": inv["deleted"],
        "state_only_changed_allowed": [one for one in inv["changed"] if one in STATE_ONLY],
        "avatar_state_values": {key: avatar_state.get(key) for key in
                                ("generation", "state", "listener_pid", "avatar_id")},
        "journal": {key: switch_state.get(key) for key in ("phase", "result", "target", "updated_at")},
        "prompt_chars_live": len(prompt_text),
        "memory_db_schema_sha": memory_schema_live,
        "character_db_schema_sha": character_schema_live,
        "memory_db_schema_built_by_current_code": schema_built_by_current_code(memory_db, "memory"),
        "character_db_schema_built_by_current_code": schema_built_by_current_code(
            character_db, "character"),
        "memory_db_mtime": memory_db_mtime,
        "memory_db_sha_now": sha_file(memory_db),
        "baseline_memory_db_field_was_empty": not memory_baseline_usable,
        "memory_db_captured_at_baseline": baseline.get("captured_at"),
        # ---- 生产易变状态的归属登记（不参与定档）----
        # UI 那一面位原先跟着 active_character 指针走，指针在 23:59 被人物面板的删除
        # 带进悬空态，于是判成「行为变了」。这里把当时发生了什么原样记下来。
        "production_state_attribution": {
            "active_pointer": pointer,
            "prompt_endpoint_follows_pointer_chars": len(prompt_text),
            "baseline_prompt_character_id": anchored.get("character_id"),
            "reanchored_cell_shas": anchored.get("shas"),
            "reanchored_cells_identical": anchored.get("cells"),
            "ui_write_endpoints_issued_by_this_task": 0,
            "note": ("7860 访问日志里 23:58:59–23:59:18 有 POST /api/memory/clear_session、"
                     "/api/memory/delete、/api/memory/restore、/api/character/restore、"
                     "/api/character/delete ×3（人物 c8b57248624 张兰），与 change_journal 的 "
                     "CHARACTER_RESTORE@15:58:59Z、CHARACTER_ARCHIVE@15:59:06/15/18Z 一一对应。"
                     "本任务对 7860 只发 GET，对 8090 发 chat/completions，写接口条数实测 0。"
                     "delete_character() 见被删的是当前人物，就把指针写成 DEFAULT_CHARACTER_ID"
                     "（store.py:551-552，allow_missing=True），库里没有 default ⇒ 指针悬空。"),
        },
        "argv_8090_now": llm_tokens_now,
        "argv_8090_baseline": llm_tokens_base,
        "argv_8765_tail_now": s2s_tail_now,
        "argv_8765_tail_baseline": s2s_tail_base,
        "argv_first_diff": {
            "8090": first_diff(llm_tokens_now, llm_tokens_base),
            "8765_tail": first_diff(s2s_tail_now, s2s_tail_base),
        },
        "model_ids_now": model_ids_now,
        "model_ids_baseline": model_ids_base,
        "ref_audio_path": str(REF_AUDIO),
        "ref_audio_sha_now": sha_file(REF_AUDIO),
        "ref_audio_sha_baseline": voice.get("ref_audio_sha256"),
    }

    verdict = {}
    for name, item in evidence.items():
        values = list(item.values())
        verdict[name] = "NO" if any(one is False for one in values) else (
            "NOT_EVALUABLE" if any(one is None for one in values) else "YES")

    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps({"flags": verdict, "raw": raw, "evidence": evidence},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"flags": verdict,
                      "code_changed": code_changed,
                      "deleted": inv["deleted"],
                      "state_only_changed": raw["state_only_changed_allowed"],
                      "journal_phase": raw["journal"]["phase"]}, ensure_ascii=False, indent=2))
    print("EVIDENCE_OUT", OUT_PATH)
    return 0


def sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _temporary_state_still_builds() -> bool:
    """带临时身份状态装配一次，确认那条老能力没被接线打断（只读，不落盘）。"""
    try:
        from runtime.character import context_builder, role_state_bridge
        state = role_state_bridge.default_state()
        state.update({"active": True, "role": "试演", "role_directive": "就演一句"})
        context = context_builder.build(user_text="你就试一句", temporary_state=state,
                                        db_path=ROOT / "runtime/human_interaction/tests/shadow/characters.sqlite3",
                                        memory_db_path=ROOT / "runtime/human_interaction/tests/shadow/memories.sqlite3",
                                        character_id="hi_bench_suwan")
        text = "\n\n".join(str(one.get("text") or one.get("content") or "")
                           for one in context_builder.messages(context, temporary_state=state))
        return "试演" in text
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
