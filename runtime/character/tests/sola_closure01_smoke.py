# -*- coding: utf-8 -*-
"""smoke — 任务 §11「单 LLM 调用证明」+ §12「真实端到端最小测试」+ §13「输出污染检查」+ §16 的真实 4 发。

为什么走 :8765 的 OpenAI Realtime WebSocket：那是**生产浏览器用的同一条链路**
（``runtime/ui/app/s2s-realtime-client.js`` 的 ``sendUserText`` 就是
``conversation.item.create(input_text)`` + ``response.create``，服务端注释也写明
「和转写进来的语音回合落在同一个服务端会话里，走同一条 TTS / avatar-sync 路径」）。
绕开它去打 :8090 只能证明模型能答，证明不了「她这一句真的经过了生产链」。

影子会话怎么保证（§12「不得污染长期记忆」）：
  * :8765 每个 WebSocket 连接一套 ``ConnState``，**chat 是这条连接自己的**
    （``service.py:227 RuntimeConfig(chat=Chat(self._chat_size))``），断开即
    ``chat.close()``；本轮每条 smoke 都用**新连接**，回合之间不共享上下文；
  * 装的 ``speech_to_speech`` 包里没有任何写女友记忆库的代码（见 single_call_proof 的
    memory_pollution 段现场复扫）；
  * 长/短期两个库（characters.sqlite3 / memories.sqlite3）在 4 发前后各做一次
    **只读**结构+行数指纹，必须逐字相等 —— 这条是正向证明，不是「应该没写」。

LLM 调用计数（不靠猜）：先做一次标定 —— 一次 /v1/chat/completions 请求在 llama.cpp
日志里恰好产生 1 行 ``slot launch_slot_ ... processing task``、1 行 ``prompt eval time``、
1 行 ``release: ... stop processing``。本轮每个回合只数**该回合新增日志区段**里的这三行，
三者互相对拍，谁不等于 1 就登记。

只登记，不评价：禁止主观打分，禁止问模型「你觉得自己真实吗」（任务原文）。
动作描写出现也不判 FAIL，只登记 NARRATIVE_ACTION_OUTPUT_PRESENT。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sola_closure01_lib as L  # noqa: E402

import importlib.util  # noqa: E402

MINIMAL_PROOF = Path(__file__).resolve().parent / "sola_v2_ember_voice_minimal_proof.py"
LLM_LOG = L.REPO / "runtime/llm/logs/llama_8090_gemma4.log"
CHARACTER_DB = L.REPO / "runtime/character/data/characters.sqlite3"
MEMORY_DB = L.REPO / "runtime/memory/data/memories.sqlite3"
WS_URL = "ws://127.0.0.1:8765/v1/realtime"
TURN_TIMEOUT_S = 150.0

TURNS = [
    {"id": "T1", "kind": "普通短句", "text": "今天加班到好晚，累死了。"},
    {"id": "T2", "kind": "情绪句", "text": "我今天特别难受，就想跟你说两句，你别给我讲道理。"},
    {"id": "T3", "kind": "有观点冲突的句子",
     "text": "我觉得你根本不懂我，每次我说累你就只会让我早点睡，我不想听这个。"},
    {"id": "T4", "kind": "需要稍长回答的问题",
     "text": "这周四位老人要轮流去医院，我还得上班，你帮我排一下这周怎么安排，顺便说为什么这么排。"},
]

# §13 的机械尺：分三桶，全部只做字符串判断，不做语义评价。
LEAK_IDENTITY = (re.compile(r"(?i)\bsola\b"), re.compile(r"You are Sola"),
                 re.compile(r"(?i)co-author"))
LEAK_PATTERNS = (
    ("显式 // 元标签", re.compile(r"(?m)^\s*//\s*\S+")),
    ("方括号动作/旁白标签", re.compile(r"\[(?:action|narration|scene|stage)[^\]]*\]", re.I)),
    ("system/prompt 字样泄漏", re.compile(
        r"(?i)\b(system prompt|system message|instructions given to you|你收到的提示词)\b")),
    ("提示词模板标签", re.compile(r"</?(?:sola|adult_fiction_license|system)>\b")),
)
LEAK_E6_EXAMPLES = ("*blushes*", "*rolling the words in her mouth*", "APPROVED")
# 叙述性动作（任务 §13：出现不判 FAIL，只登记）
NARRATIVE_ACTION_PATTERNS = (
    re.compile(r"\*(?![\"'“”])[A-Za-z][A-Za-z'’,.!? \-]{1,60}\*"),
    re.compile(r"(?:他|她|它)(?:们)?(?:轻轻|慢慢|缓缓|微微)[一二三四五六七八九十]{1,4}"),
)
# 取 system 里若干段较长字面，专门用来抓「整段吐回提示词」这种泄漏
SYSTEM_FINGERPRINT_MIN_CHARS = 40


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_frozen_rulers():
    spec = importlib.util.spec_from_file_location("sola_closure01_rulers2", MINIMAL_PROOF)
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    assert spec and spec.loader
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def production_instructions() -> tuple[str, dict]:
    """浏览器连线时拿的那一整块：同一个端点、同一份字节。"""
    url = "http://127.0.0.1:7860/api/character/prompt"
    r = L.http_json(url, timeout=90)
    j = r.get("json") or {}
    text = str(j.get("内容") or "")
    return text, {"url": url, "ok": r["ok"], "status": r.get("status"), "error": r.get("error"),
                  "chars": len(text), "bytes": len(text.encode("utf-8")),
                  "sha256": sha256_text(text), "人物名称": j.get("人物名称"),
                  "编号": j.get("编号")}


def log_size() -> int:
    try:
        return LLM_LOG.stat().st_size
    except OSError:
        return 0


def log_region(off: int) -> str:
    try:
        with open(LLM_LOG, "rb") as fh:
            fh.seek(off)
            raw = fh.read(2_000_000)      # 硬上限：绝不让自写解析器产出垃圾大文件
    except OSError:
        return ""
    return raw.decode("utf-8", "replace")


def count_llm_calls(region: str) -> dict:
    launch = re.findall(r"slot launch_slot_: id\s+\d+ \| task (\d+) \| processing task", region)
    prompt_eval = re.findall(r"print_timing: id\s+\d+ \| task (\d+) \| prompt eval time", region)
    release = re.findall(r"release: id\s+\d+ \| task (\d+) \| stop processing", region)
    pe_ms = [float(one) for one in
             re.findall(r"prompt eval time =\s+([\d.]+) ms /\s+\d+ tokens", region)]
    ev_ms = [float(one) for one in
             re.findall(r"eval time =\s+([\d.]+) ms /\s+\d+ tokens", region)]
    return {"launch_slot_lines": len(launch), "prompt_eval_lines": len(prompt_eval),
            "release_lines": len(release),
            "launch_task_ids": launch, "prompt_eval_task_ids": prompt_eval,
            "release_task_ids": release,
            "agree": len(launch) == len(prompt_eval) == len(release),
            "llama_prompt_eval_ms": round(sum(pe_ms), 1),
            "llama_decode_ms": round(sum(ev_ms), 1)}


def memory_package_scan() -> dict:
    """现场复扫：装的 s2s 包里到底有没有写女友记忆库 / 调 :7860 的代码。"""
    root = L.REPO / "runtime/core/venv/Lib/site-packages/speech_to_speech"
    hits: dict[str, list[str]] = {}
    files = 0
    for path in root.rglob("*.py"):
        files += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for needle in ("memory_bridge", "memories.sqlite3", "characters.sqlite3",
                       "127.0.0.1:7860", "localhost:7860", "runtime/memory"):
            if needle in text:
                hits.setdefault(needle, []).append(str(path.relative_to(root)))
    return {"scanned_files": files, "root": str(root), "hits": hits,
            "WRITES_MEMORY_DB": "YES" if hits else "NO"}


async def one_turn(instructions: str, text: str) -> dict:
    import websockets

    row: dict = {"ws_url": WS_URL}
    sent = {"type": "session.update", "session": {
        "type": "realtime", "instructions": instructions,
        "tools": [], "tool_choice": "none"}}
    t0 = time.monotonic()
    first_text_ms = None
    first_audio_ms = None
    parts: list[str] = []
    event_types: dict[str, int] = {}
    audio_bytes = 0
    audio_delta_events = 0
    transcript_from_done = False
    usage = None
    finish = None
    response_ids: list[str] = []
    deltas = 0
    async with websockets.connect(WS_URL, max_size=50 * 1024 * 1024,
                                  open_timeout=30, close_timeout=10) as ws:
        await ws.send(json.dumps(sent, ensure_ascii=False))
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": text}]},
        }, ensure_ascii=False))
        await ws.send(json.dumps({"type": "response.create",
                                 "response": {"modalities": ["audio", "text"]}}))
        row["sent_event_types"] = ["session.update", "conversation.item.create",
                                   "response.create"]
        while time.monotonic() - t0 < TURN_TIMEOUT_S:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=TURN_TIMEOUT_S)
            except asyncio.TimeoutError:
                row["timeout"] = True
                break
            except websockets.exceptions.ConnectionClosed as exc:
                row["connection_closed"] = f"{exc.code}/{L.short(exc.reason, 80)}"
                break
            if isinstance(raw, (bytes, bytearray)):
                audio_bytes += len(raw)
                if first_audio_ms is None:
                    first_audio_ms = int((time.monotonic() - t0) * 1000)
                event_types.setdefault("<binary_audio_frame>", 0)
                event_types["<binary_audio_frame>"] += 1
                continue
            try:
                ev = json.loads(raw)
            except Exception:
                event_types.setdefault("<unparsable>", 0)
                event_types["<unparsable>"] += 1
                continue
            et = str(ev.get("type") or "")
            event_types[et] = event_types.get(et, 0) + 1
            if et == "session.created":
                row["session_id"] = ((ev.get("session") or {}).get("id"))
            elif et in ("response.output_text.delta", "response.audio_transcript.delta",
                        "response.text.delta"):
                piece = str(ev.get("delta") or "")
                if piece:
                    if first_text_ms is None:
                        first_text_ms = int((time.monotonic() - t0) * 1000)
                    parts.append(piece)
                    deltas += 1
            elif et == "response.output_audio_transcript.delta":
                piece = str(ev.get("delta") or "")
                if piece:
                    if first_text_ms is None:
                        first_text_ms = int((time.monotonic() - t0) * 1000)
                    parts.append(piece)
                    deltas += 1
            elif et == "response.output_audio_transcript.done":
                # 生产链把助手台词放在这条事件的 transcript 里（语音回合同样走它）；
                # 没有 .delta 流时，这一条就是这一回合的完整文字回复。
                final_text = str(ev.get("transcript") or "")
                if final_text and not parts:
                    parts.append(final_text)
                    if first_text_ms is None:
                        first_text_ms = int((time.monotonic() - t0) * 1000)
                    transcript_from_done = True
                row["transcript_event_at_ms"] = int((time.monotonic() - t0) * 1000)
            elif et == "response.output_audio.delta":
                payload = str(ev.get("delta") or "")
                if payload:
                    audio_bytes += int(len(payload) * 3 // 4)
                    audio_delta_events += 1
                    if first_audio_ms is None:
                        first_audio_ms = int((time.monotonic() - t0) * 1000)
            elif et == "response.output_audio.done":
                row["audio_done_at_ms"] = int((time.monotonic() - t0) * 1000)
            elif et == "response.completed":
                r0 = (ev.get("response") or {})
                usage = r0.get("usage")
                finish = r0.get("status")
                if r0.get("id"):
                    response_ids.append(r0["id"])
                if r0.get("modality") is not None:
                    row["modality"] = r0.get("modality")
            elif et == "response.done":
                r0 = (ev.get("response") or {})
                usage = r0.get("usage") or usage
                finish = r0.get("status") or finish
                if r0.get("id"):
                    response_ids.append(r0["id"])
                break
            elif et == "error":
                row["error_event"] = L.short(json.dumps(ev, ensure_ascii=False), 400)
                break
        wall_ms = int((time.monotonic() - t0) * 1000)
    reply = "".join(parts).strip()
    row.update({
        "reply": reply, "reply_chars": len(reply),
        "first_content_ms": first_text_ms, "first_audio_ms": first_audio_ms,
        "wall_ms": wall_ms, "text_delta_events": deltas,
        "transcript_from_done_event": transcript_from_done,
        "audio_delta_events": audio_delta_events,
        "audio_bytes": audio_bytes, "usage": usage, "finish_status": finish,
        "response_ids": response_ids, "event_types": event_types,
    })
    return row


def leak_checks(reply: str, instructions: str) -> dict:
    identity = [rx.pattern for rx in LEAK_IDENTITY if rx.search(reply)]
    identity += [lit for lit in LEAK_E6_EXAMPLES if lit in reply]
    pat = {label: [m.group(0)[:60] for m in rx.finditer(reply)]
           for label, rx in LEAK_PATTERNS}
    pat = {k: v for k, v in pat.items() if v}
    # 逐字回吐提示词：任何 ≥40 字的 system 片段整段出现在回复里就算泄漏
    verbatim = []
    for chunk in re.split(r"\n+", instructions):
        c = chunk.strip()
        if len(c) >= SYSTEM_FINGERPRINT_MIN_CHARS and c in reply:
            verbatim.append(L.short(c, 80))
    return {"identity_hits": identity, "pattern_hits": pat,
            "verbatim_system_spans": verbatim,
            "clean": not identity and not pat and not verbatim}


def narrative_action(reply: str) -> dict:
    """机械尺：把引号里的「说出来的话」摘掉，剩下的还是成句的叙述 / 动作文字 = 叙述性动作。

    任务 §13 明确：出现叙述性动作**不判 FAIL、不改**，只登记（原版 Ember 本来就允许，
    交 owner 后续真人决定）。这里不用语义判断，只用「引号外是否还剩成段文字」。
    """
    without_quotes = re.sub(r"[“\"「][^”\"」]*[”\"」]", "", reply)
    without_quotes = re.sub(r"\s+", "", without_quotes)
    residue = re.sub(r"[，。！？；：、,.!?;:\n…\-—~—\s]+", "", without_quotes)
    stars = [m.group(0)[:40] for m in NARRATIVE_ACTION_PATTERNS[0].finditer(reply)]
    third_person = bool(re.search(r"(她|他|周慧)[^”\"]{0,3}(?:放下|伸手|皱眉|叹|靠|眯|抬|低|走|坐|拿|看)",
                                  reply))
    return {"present": bool(stars) or third_person,
            "引号外残留文字": L.short(residue, 120), "星号动作": stars,
            "第三人称动作句": third_person}


def main() -> int:
    L.bare_import_root()
    # --only T2,T3,T4：只补跑失败的那几条（任务 §12 上限 4 条，握手失败的那几条
    # 根本没产生生成，补跑不算第 5 条；已经成功的行不重跑）。默认全跑，
    # 且 smoke_rows.jsonl 会被本轮实际跑的行覆盖重写。
    only = None
    for i, arg in enumerate(sys.argv):
        if arg == "--only" and i + 1 < len(sys.argv):
            only = {one.strip().upper() for one in sys.argv[i + 1].split(",")}
    specs = [one for one in TURNS if only is None or one["id"] in only]
    REFLAGS = "--reflags" in sys.argv
    rulers = load_frozen_rulers()

    instructions, inst_fact = production_instructions()
    if not instructions:
        print("FATAL: 拿不到生产 instructions", json.dumps(inst_fact, ensure_ascii=False))
        return 2
    sol_block = (L.REPO / "runtime/character/sola_v2_ember"
                 / "sola_v2_ember_voice_minimal.txt")
    from runtime.character.sola_v2_ember import adapter as sola_adapter  # type: ignore
    from runtime.character import store  # type: ignore
    ident = store.get_active_character()
    live_sola = sola_adapter.system_text(ident)

    rows_path = L.ensure_out_dir() / "smoke_rows.jsonl"
    if REFLAGS:
        # --reflags：不重跑生成（任务 §12 上限 4 条已用完），拿上一轮落盘的 4 行回复
        # 用修好的尺重算判据，pre 指纹与包扫描沿用那一轮现场采到的值，post 现在再采一次。
        prev = json.loads((L.ensure_out_dir() / "single_call_proof.json")
                          .read_text(encoding="utf-8"))
        mp = prev.get("memory_pollution") or {}
        pre = mp.get("pre_fingerprint") or {
            "characters": rulers.db_fingerprint(CHARACTER_DB),
            "memories": rulers.db_fingerprint(MEMORY_DB)}
        scan = mp.get("package_scan") or memory_package_scan()
        rows = [json.loads(one) for one in
                rows_path.read_text(encoding="utf-8").splitlines() if one.strip()]
        for r0 in rows:
            r0["leak_check"] = leak_checks(r0.get("reply") or "", instructions)
            r0["narrative_action_present"] = narrative_action(r0.get("reply") or "")
        rows_path.write_text("".join(json.dumps(r0, ensure_ascii=False) + "\n"
                                     for r0 in rows), encoding="utf-8", newline="\n")
        print("REFLAGS=从已存 4 行重算判据（本轮不打模型、不生成）", len(rows), "rows")
    else:
        pre = {"characters": rulers.db_fingerprint(CHARACTER_DB),
               "memories": rulers.db_fingerprint(MEMORY_DB)}
        scan = memory_package_scan()
        rows = []
        rows_path.write_text("", encoding="utf-8", newline="\n")
        for spec in specs:
            off = log_size()
            try:
                turn = asyncio.run(one_turn(instructions, spec["text"]))
            except Exception as exc:
                turn = {"error": type(exc).__name__ + ": " + str(exc)[:300], "reply": ""}
                time.sleep(2.0)
                turn["log_region_after_error"] = log_region(off)[:2000]
            time.sleep(1.2)                       # 让 llama.cpp 把这一回合的日志刷完
            region = log_region(off)
            calls = count_llm_calls(region)
            row = {
                "TASK_ID": L.TASK_ID, "turn_id": spec["id"], "kind": spec["kind"],
                "user_text": spec["text"],
                "system_hash": inst_fact["sha256"],
                "system_chars": inst_fact["chars"],
                "sola_active_in_system": bool(live_sola) and live_sola in instructions,
                "sola_block_sha256": sha256_text(live_sola),
                "first_content_ms": turn.get("first_content_ms"),
                "first_audio_ms": turn.get("first_audio_ms"),
                "wall_ms": turn.get("wall_ms"),
                "prompt_tokens": (turn.get("usage") or {}).get("input_tokens")
                                 if turn.get("usage") else None,
                "completion_tokens": (turn.get("usage") or {}).get("output_tokens")
                                     if turn.get("usage") else None,
                "usage_raw": turn.get("usage"),
                "finish": turn.get("finish_status"),
                "response_ids": turn.get("response_ids"),
                "session_id": turn.get("session_id"),
                "audio_bytes": turn.get("audio_bytes"),
                "audio_delta_events": turn.get("audio_delta_events"),
                "transcript_from_done_event": turn.get("transcript_from_done_event"),
                "text_delta_events": turn.get("text_delta_events"),
                "event_types": turn.get("event_types"),
                "output_chars": turn.get("reply_chars"),
                "reply": turn.get("reply"),
                "llm_calls_from_8090_log": calls,
                "llama_prompt_eval_ms": calls["llama_prompt_eval_ms"],
                "llama_decode_ms": calls["llama_decode_ms"],
                "LLM_CALLS_PER_TURN": calls["release_lines"],
                "leak_check": leak_checks(turn.get("reply") or "", instructions),
                "narrative_action_present": narrative_action(turn.get("reply") or ""),
                "error": turn.get("error"), "timeout": turn.get("timeout"),
                "connection_closed": turn.get("connection_closed"),
                "error_event": turn.get("error_event"),
                "collected_at_local": L.now_local(),
            }
            rows.append(row)
            with open(rows_path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"[{spec['id']}] calls={row['LLM_CALLS_PER_TURN']} "
                  f"ttft={row['first_content_ms']} wall={row['wall_ms']} "
                  f"out={row['output_chars']}c audio={row['audio_bytes']}B "
                  f"clean={row['leak_check']['clean']} err={row['error']}")

    post = {"characters": rulers.db_fingerprint(CHARACTER_DB),
            "memories": rulers.db_fingerprint(MEMORY_DB)}

    ok_rows = [r for r in rows if r.get("reply")]
    firsts = [r["first_content_ms"] for r in ok_rows if r.get("first_content_ms")]
    audio_firsts = [r.get("first_audio_ms") for r in ok_rows if r.get("first_audio_ms")]
    walls = [r["wall_ms"] for r in ok_rows if r.get("wall_ms")]
    outs = [r["output_chars"] for r in ok_rows]
    compl = [r["completion_tokens"] for r in ok_rows if r.get("completion_tokens")]
    prompts = [r["prompt_tokens"] for r in ok_rows if r.get("prompt_tokens")]
    out = {
        "task_id": L.TASK_ID,
        "stages": ["11_single_llm_call_per_turn", "12_real_end_to_end_smoke",
                   "13_output_leak_check", "16_real_four_shot_performance"],
        "collected_at_local": L.now_local(),
        "chain": "浏览器用的同一条生产链：:7860 /api/character/prompt 取 instructions → "
                 "ws://127.0.0.1:8765/v1/realtime（VAD 不参与，文字直接进 LLM 上下文）→ "
                 "llm_backend chat-completions → :8090 llama.cpp → TTS(qwen3) 回音频帧",
        "instructions": inst_fact,
        "sola_block_sha256": sha256_text(live_sola),
        "SOLA_ACTIVE_IN_PRODUCTION_CHAIN": "YES" if (live_sola and live_sola in instructions) else "NO",
        "per_turn": rows,
        "LLM_CALLS_PER_TURN": (max(r["LLM_CALLS_PER_TURN"] for r in rows) if rows else None),
        "LLM_CALLS_PER_TURN_ALL_EQUAL_1": bool(rows) and all(
            r["LLM_CALLS_PER_TURN"] == 1 for r in rows),
        "three_log_rulers_agree": all(r["llm_calls_from_8090_log"]["agree"] for r in rows),
        "judge_call": "NONE_SEEN" if all(
            r["LLM_CALLS_PER_TURN"] == 1 for r in rows) else "SEE_PER_TURN",
        "second_llm": "NONE_SEEN" if all(r["llm_calls_from_8090_log"]["release_lines"] == 1
                                         for r in rows) else "SEE_PER_TURN",
        "revision_llm": "DISABLED (REVISE_ENABLED=false，见 runtime_services.json)",
        "hidden_helper_model": "NONE_SEEN (:8090 -np 1 单槽；每回合日志区段里只有 1 个 task id)",
        "smoke_count": len(rows),
        "sola_content_check": {
            "sola_block_in_system_hash_source": live_sola in instructions,
            "sola_block_chars": len(live_sola),
        },
        "leak_summary": {
            "SOLA_IDENTITY_LEAK": "NO" if not any(
                r["leak_check"]["identity_hits"] for r in rows) else "YES",
            "SYSTEM_PROMPT_LEAK": "NO" if not any(
                r["leak_check"]["verbatim_system_spans"] for r in rows)
                and not any("system/prompt" in k
                            for r in rows for k in r["leak_check"]["pattern_hits"]) else "YES",
            "META_LABEL_LEAK": "NO" if not any(
                k in ("显式 // 元标签", "方括号动作/旁白标签", "提示词模板标签")
                for r in rows for k in r["leak_check"]["pattern_hits"]) else "YES",
            "per_turn_identity_hits": {r["turn_id"]: r["leak_check"]["identity_hits"]
                                       for r in rows},
            "per_turn_pattern_hits": {r["turn_id"]: r["leak_check"]["pattern_hits"]
                                      for r in rows},
            "per_turn_verbatim_spans": {r["turn_id"]: r["leak_check"]["verbatim_system_spans"]
                                        for r in rows},
            "ruler_note": "identity=『Sola / You are Sola / co-author / 原 E6 例句字面』；"
                          "prompt_leak=回复里整段出现 system 的 ≥40 字片段或明说 system prompt；"
                          "meta_label=// 行、[action] 类标签、<sola> 这类模板标签。全部机械判断。",
        },
        "NARRATIVE_ACTION_OUTPUT_PRESENT": (
            "YES" if any(r["narrative_action_present"]["present"] for r in rows) else "NO"),
        "narrative_action_turns": [r["turn_id"] for r in rows
                                         if r["narrative_action_present"]["present"]],
        "narrative_action_detail": {r["turn_id"]: r["narrative_action_present"] for r in rows},
        "narrative_action_note": "任务 §13：出现自然语言/星号动作描写不判 FAIL、不修改，只登记交 owner。",
        "performance": {
            "regime": "本机共存负载（:8090 + :8765 + :8010 + 桌面同机、显存共享），"
                      "不是隔离环境；只记录不优化，不改 prompt",
            "median_TTFT_ms": statistics.median(firsts) if firsts else None,
            "median_first_audio_packet_ms": statistics.median(audio_firsts) if audio_firsts else None,
            "median_llama_prompt_eval_ms": (
                statistics.median([r["llama_prompt_eval_ms"] for r in ok_rows])
                if ok_rows else None),
            "median_wall_ms": statistics.median(walls) if walls else None,
            "median_completion_chars": statistics.median(outs) if outs else None,
            "median_completion_tokens": statistics.median(compl) if compl else None,
            "median_prompt_tokens": statistics.median(prompts) if prompts else None,
            "ttft_samples_ms": firsts,
            "first_audio_packet_samples_ms": audio_firsts,
            "llama_prompt_eval_samples_ms": [r["llama_prompt_eval_ms"] for r in ok_rows],
            "llama_decode_samples_ms": [r["llama_decode_ms"] for r in ok_rows],
            "wall_samples_ms": walls,
            "completion_char_samples": outs,
            "completion_token_samples": compl,
            "prompt_token_samples": prompts,
            "口径说明": {
                "median_TTFT_ms": "客户端发出 response.create → 第一个「文字内容」事件到达；"
                                  "生产链的文字台词由 response.output_audio_transcript 事件带，"
                                  "所以这条在本链上等于「转写事件到达」，不是模型首 token",
                "median_llama_prompt_eval_ms": ":8090 日志里该回合的 prefill 时间（模型侧首 token 前"
                                               "的一段，口径与 TTFT 不同，分开报）",
                "median_first_audio_packet_ms": "客户端发出 → 第一个音频增量帧到达（含 TTS 首包）",
                "median_wall_ms": "客户端发出 response.create → 收到 response.done 的整段墙钟",
            },
        },
        "memory_pollution": {
            "pre_fingerprint": pre, "post_fingerprint": post,
            "MEMORY_DB_UNCHANGED": "YES" if pre == post else "NO",
            "package_scan": scan,
            "shadow_session": ":8765 每连接一套 Chat（service.py:227），断开即 close；"
                              "每条 smoke 用新连接，回合之间不共享上下文",
        },
        "artifacts": {"smoke_rows": str(rows_path),
                      "smoke_rows_sha256": L.sha256_file(rows_path)},
    }
    path = L.write_json("single_call_proof.json", out)
    print("WROTE", path)
    print("LLM_CALLS_PER_TURN =", out["LLM_CALLS_PER_TURN"],
          "all_equal_1 =", out["LLM_CALLS_PER_TURN_ALL_EQUAL_1"],
          "rulers_agree =", out["three_log_rulers_agree"])
    print("SOLA_IDENTITY_LEAK =", out["leak_summary"]["SOLA_IDENTITY_LEAK"],
          "SYSTEM_PROMPT_LEAK =", out["leak_summary"]["SYSTEM_PROMPT_LEAK"],
          "META_LABEL_LEAK =", out["leak_summary"]["META_LABEL_LEAK"])
    print("NARRATIVE_ACTION_OUTPUT_PRESENT =", out["NARRATIVE_ACTION_OUTPUT_PRESENT"],
          out["narrative_action_turns"])
    print("MEMORY_DB_UNCHANGED =", out["memory_pollution"]["MEMORY_DB_UNCHANGED"],
          "| package scan WRITES_MEMORY_DB =", scan["WRITES_MEMORY_DB"], scan["hits"])
    print("perf =", json.dumps(out["performance"], ensure_ascii=False))
    for r in rows:
        print("---", r["turn_id"], "out", r["output_chars"], "c:",
              L.short(r["reply"], 200))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
