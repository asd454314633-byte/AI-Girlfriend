# -*- coding: utf-8 -*-
"""sola_closure01_media.py — 收口任务 §14（TTS 链）与 §15（数字人链）只读采集。

只读：两路 GET（:7860 控制面 / :8010 管理面）、日志与状态文件读取、进程枚举、
两次心跳抽样。**不发起切换、不 POST /offer、不重启、不写任何生产状态。**

产两件：
  * tts_smoke.json      —— §14
  * avatar_smoke.json   —— §15

为什么要「正向对照 + 无意义对照」都跑：判据是「日志里找不到 TTS 行」时，
到底是没有 TTS，还是我这把尺（时间窗解析 / 正则）本身坏了 —— 两者在裸计数
上长得一模一样。本机有过这类假干净判决的事故，所以每条计数都要配一个
「一定该数到」和一个「一定数不到」的对照。
"""
from __future__ import annotations

import json
import re
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROBLEM_DIR = Path(__file__).resolve().parent
if str(PROBLEM_DIR) not in sys.path:
    sys.path.insert(0, str(PROBLEM_DIR))
import sola_closure01_lib as L  # noqa: E402

OUT = L.ensure_out_dir()
S2S_LOG = L.REPO / "logs" / "runtime" / "s2s.stderr.log"
HB_LOG = L.REPO / "logs" / "runtime" / "avatar-8010.heartbeat.log"
AV_LOG = L.REPO / "logs" / "runtime" / "avatar-8010.stderr.log"
AVATAR_STATE = L.REPO / "runtime" / "windows" / "avatar_runtime_state.json"
TTS_BACKEND = L.REPO / "runtime" / "windows" / "active_tts_backend.txt"
TTS_JOURNAL = L.REPO / "runtime" / "windows" / "tts_switch_state.json"

# 日志行首的时间戳口径：logging 默认「2026-09-28 05:02:05,693」
LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")
TTS_TEXT_LINE = "Qwen TTS text length="
TTS_TTFA = "Qwen3-TTS TTFA:"
TTS_DONE = "Qwen3-TTS generated"
RESP_DONE = "Response done (status=completed)"
BAD_WORDS = ("Traceback (most recent call last)", "CRITICAL", "ERROR -",
             "  File \"", "Exception:")
# 无意义对照：这串在本链任何日志里都不可能出现。数到 >0 = 尺坏（判据必须报错，
# 不能把「匹配到垃圾」当成 PASS。
NULL_PATTERN = "ZZZ_QODER_NO_SUCH_LOG_PATTERN_ZZZ"


def parse_log_ts(line: str) -> datetime | None:
    m = LOG_TS.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") + \
        timedelta(milliseconds=int(m.group(2)))


def local_ts_to_dt(text: str) -> datetime | None:
    """把证据里的 `collected_at_local`（"%Y-%m-%d %H:%M:%S %z"）转成本机墙钟。"""
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def tail_lines(path: Path, n: int) -> list[str]:
    """从文件尾读 n 行（数字人日志有 2.3MB+，不能整读）。"""
    if not path.exists():
        return []
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        block = min(size, max(4096, n * 260))
        fh.seek(size - block)
        raw = fh.read().decode("utf-8", "replace")
    lines = [one for one in raw.splitlines() if one.strip()]
    if size - block > 0 and lines:
        lines = lines[1:]            # 被切断的首行丢掉，不算一行证据
    return lines[-n:]


def window_lines(path: Path, start: datetime, end: datetime, cap: int = 6000) -> \
        tuple[list[str], dict[str, Any]]:
    """按时间戳取一段日志。缺时间戳的行归给上一条有戳的行（多行 traceback）。"""
    facts: dict[str, Any] = {"path": str(path.relative_to(L.REPO)),
                             "exists": path.exists()}
    if not path.exists():
        return [], facts
    kept: list[str] = []
    cur: datetime | None = None
    total = stamped = unstamped = dropped = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            total += 1
            line = line.rstrip("\n")
            ts = parse_log_ts(line)
            if ts is not None:
                stamped += 1
                cur = ts
            else:
                unstamped += 1
            if cur is None:
                continue
            if start <= cur <= end:
                if len(kept) < cap:
                    kept.append(line)
                else:
                    dropped += 1
    facts.update({"lines_total": total, "lines_stamped": stamped,
                  "lines_unstamped": unstamped, "window": [
                      start.strftime(L.TS_FMT), end.strftime(L.TS_FMT)],
                  "kept": len(kept), "dropped_over_cap": dropped,
                  "window_unparsable": stamped == 0})
    return kept, facts


def count(pattern: str, lines: list[str]) -> list[str]:
    return [one for one in lines if pattern in one]


# ---------------------------------------------------------------------------
# §14 TTS
# ---------------------------------------------------------------------------

def collect_tts() -> dict[str, Any]:
    proof = json.loads((OUT / "single_call_proof.json").read_text(encoding="utf-8"))
    turns = proof["per_turn"]

    # 1) 8765 监听者身份 + argv 里点名的 --tts
    ls = L.listeners()
    cur_8765 = (ls.get(8765) or [{}])[0]
    base = json.loads((OUT / "baseline.json").read_text(encoding="utf-8"))
    base_8765 = ((base.get("services") or {}).get("8765") or {}).get("listeners") or [{}]
    argv = str(cur_8765.get("cmdline") or "")
    tts_flag = ""
    m = re.search(r"--tts\s+(\S+)", argv)
    if m:
        tts_flag = m.group(1)

    # 2) 控制面（只读 GET，前端兼容面 busy/message/result 三个键要在）
    vm = L.http_json("http://127.0.0.1:7860/api/voice-models", timeout=45)
    vmj = vm.get("json") or {}
    sw = vmj.get("switch") or {}
    control_plane = {
        "url": "http://127.0.0.1:7860/api/voice-models",
        "ok": vm.get("ok"), "status": vm.get("status"), "error": vm.get("error"),
        "elapsed_ms": vm.get("elapsed_ms"),
        "ready": vmj.get("ready"), "active": vmj.get("active"),
        "activeError": vmj.get("activeError"),
        "activeMainTtsCount": vmj.get("activeMainTtsCount"),
        "switch_phase": sw.get("phase"), "switch_busy": sw.get("busy"),
        "switch_message": sw.get("message"), "switch_result": sw.get("result"),
        "switch_target": sw.get("target"),
        "frontend_keys_present": {k: (k in sw) for k in ("busy", "message", "result")},
        "provider_ids": [str(one.get("id")) for one in (vmj.get("providers") or [])
                         if isinstance(one, dict)],
        "health_detail": L.short(vmj.get("health_detail"), 600),
    }

    # 3) committed backend / 事务日志（只读，不改）
    committed = ""
    if TTS_BACKEND.exists():
        committed = TTS_BACKEND.read_text(encoding="utf-8").strip()
    journal = L.read_json_file("runtime/windows/tts_switch_state.json")
    journal_row = ({k: journal.get(k) for k in
                    ("phase", "result", "target", "updated_at", "switch_id")}
                   if isinstance(journal, dict) else {"_read_error": L.short(journal)})

    # 4) 每一回合的 TTS 日志行归属。
    #
    #    第一版这里用「每回合 ±8 秒宽窗 + 长度出现在窗里即算相等」，跑出来
    #    T1 的窗里躺着 [58,98,75] 三条 —— 窗与窗互相重叠，那条判据退化成
    #    「这四个数里有一个对」，等于没判。所以改成两步：
    #      步一 整个 smoke 覆盖段里把 TTS 文本行全捞出来（带时间戳）；
    #      步二 要求「行数 == 回合数」且每回合在自己那扇紧窗
    #           [结束墙钟 - 本回合耗时 - 2s, 结束 + 3s] 里**恰好命中一行**，
    #           并且那一行的 length 逐字节等于本回合回复字数。
    #    这样才是「第 i 回合的那一行」，而不是「这一段里某一行的字数碰巧一样」。
    dts = [local_ts_to_dt(str(t.get("collected_at_local") or "")) for t in turns]
    walls = [float(t.get("wall_ms") or 0) / 1000.0 for t in turns]
    span_start = min(one - timedelta(seconds=wall + 12)
                     for one, wall in zip(dts, walls) if one is not None) \
        if any(dts) else None
    span_end = max(one for one in dts if one is not None) + timedelta(seconds=12) \
        if any(dts) else None
    slines: list[str] = []
    span_facts: dict[str, Any] = {"usable": bool(span_start and span_end)}
    if span_start and span_end:
        slines, span_facts = window_lines(S2S_LOG, span_start, span_end)
        span_facts["window_lines_kept"] = len(slines)

    def stamped_events(pattern: str, num_re: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for one in slines:
            if pattern not in one:
                continue
            mm = re.search(num_re, one)
            out.append({"ts": parse_log_ts(one), "value": mm.group(1) if mm else None,
                        "line": L.short(one, 170)})
        return out

    tts_ev = stamped_events(TTS_TEXT_LINE, r"Qwen TTS text length=(\d+)")
    done_ev = stamped_events(RESP_DONE, r"audio=([0-9]+)")
    ttfa_ev = stamped_events(TTS_TTFA, r"TTFA:\s*([0-9.]+)s")
    gen_ev = stamped_events(TTS_DONE, r"generated\s+([0-9.]+)s\s+audio")
    rtf_ev = stamped_events(TTS_DONE, r"\(RTF:\s*([0-9.]+)")

    #    紧窗认领实测会「一窗两行」（相邻回合的 TTS 行只隔 5~6 秒，而
    #    `collected_at_local` 只到秒），所以判据换成**保序双射**：段内行数 ==
    #    回合数（4==4，多一行/少一行都直接判 NO）、行时间戳严格递增、第 i 行配
    #    第 i 回合，再逐回合比 length。附带一扇宽容紧窗（回合开始 ~ 结束后 8 秒）
    #    只做「没配到离谱的地方」的安全检查，不承担归属。
    monotone = all(a["ts"] is not None and b["ts"] is not None and a["ts"] < b["ts"]
                   for a, b in zip(tts_ev, tts_ev[1:]))
    tts_bijection = len(tts_ev) == len(turns) and monotone
    per_turn: list[dict[str, Any]] = []
    for i, (t, dt, wall_s) in enumerate(zip(turns, dts, walls)):
        row: dict[str, Any] = {
            "turn_id": t.get("turn_id"), "kind": t.get("kind"),
            "collected_at_local": t.get("collected_at_local"),
            "reply_chars": len(str(t.get("reply") or "")),
            "transcript_output_chars": t.get("output_chars"),
            "audio_delta_events": t.get("audio_delta_events"),
            "audio_bytes_estimate": t.get("audio_bytes"),
            "first_audio_packet_ms": t.get("first_audio_ms"),
            "error": t.get("error"), "error_event": t.get("error_event"),
            "pair_index": i,
        }
        if dt is None:
            row["window_unparsable"] = True
            row["attribution"] = "COLLECTED_AT_UNPARSABLE"
            row["TTS_INPUT_EQUALS_REPLY"] = False
            per_turn.append(row)
            continue
        lo = dt - timedelta(seconds=wall_s + 4)
        hi = dt + timedelta(seconds=8)
        row["loose_window"] = [lo.strftime("%H:%M:%S"), hi.strftime("%H:%M:%S")]
        ev = tts_ev[i] if i < len(tts_ev) else None
        row["attribution"] = "ORDER_PAIR" if ev is not None else "NO_LINE_TO_PAIR"
        row["tts_text_lines"] = [ev["line"]] if ev else []
        row["tts_text_lengths_seen"] = ([int(ev["value"])] if ev and
                                        ev["value"] is not None else [])
        row["ts_inside_loose_window"] = bool(ev and ev["ts"] is not None and
                                             lo <= ev["ts"] <= hi)
        row["TTS_INPUT_EQUALS_REPLY"] = bool(tts_bijection and ev is not None and
                                             ev["value"] is not None and
                                             int(ev["value"]) == row["reply_chars"]
                                             and row["ts_inside_loose_window"])
        row["tts_ttfa_lines"] = [one["line"] for one in ttfa_ev
                                 if one["ts"] and lo <= one["ts"] <= hi]
        row["tts_audio_done_lines"] = [one["line"] for one in gen_ev
                                       if one["ts"] and lo <= one["ts"] <= hi]
        row["response_done_lines"] = [one["line"] for one in done_ev
                                      if one["ts"] and lo <= one["ts"] <= hi]
        row["AUDIO_PRODUCED"] = bool(t.get("audio_delta_events")) and \
            int(t.get("audio_bytes") or 0) > 0
        per_turn.append(row)

    unassigned_tts_lines: list[str] = []      # 保序配对：行数与回合数已在上面强制相等

    # 5) 整窗异常扫描（smoke 覆盖的那一段）
    bad: list[str] = []
    for w in BAD_WORDS:
        hit = count(w, slines)
        if hit:
            bad.append(f"{w} -> {len(hit)} 行，例：{L.short(hit[0], 160)}")

    resp_done_total = len(done_ev)

    # 6) 尺本身活着没（正/负对照）
    ruler_alive = len(tts_ev) > 0 and all(one["ts"] is not None for one in tts_ev)
    null_hits = len(count(NULL_PATTERN, slines))
    null_ok = null_hits == 0

    def nums(ev: list[dict[str, Any]]) -> list[float]:
        out: list[float] = []
        for one in ev:
            try:
                out.append(float(one["value"]))
            except (TypeError, ValueError):
                continue
        return out

    ttfa_nums = nums(ttfa_ev)
    gen_secs = nums(gen_ev)
    rtfs = nums(rtf_ev)

    tts_ok = (len(turns) == 4 and ruler_alive and null_ok and tts_bijection
              and all(one.get("AUDIO_PRODUCED") for one in per_turn)
              and all(one.get("TTS_INPUT_EQUALS_REPLY") for one in per_turn)
              and not bad
              and int(cur_8765.get("pid") or 0) == int(base_8765[0].get("pid") or -1)
              and str(control_plane["switch_phase"]) == "COMMITTED"
              and control_plane["ready"] is True
              and committed == tts_flag == str(control_plane["active"]))

    # 7) §14 要点：动作/旁白有没有被念出来 —— 只登记，不修（§1 禁止再动 Sola 内容，
    #    也禁止为让测试变绿改验收规则）
    narr = []
    for row in per_turn:
        detail = (proof.get("narrative_action_detail") or {}).get(row["turn_id"]) or {}
        narr.append({
            "turn_id": row["turn_id"],
            "动作输出在场": bool(detail.get("present")),
            "星号动作": detail.get("星号动作"),
            "第三人称动作句": detail.get("第三人称动作句"),
            "TTS输入长度=回复长度": row.get("TTS_INPUT_EQUALS_REPLY"),
        })

    payload: dict[str, Any] = {
        "task_id": L.TASK_ID,
        "stages": ["§14 TTS 链健康（8765 / qwen3 / 音频产出）"],
        "collected_at_local": L.now_local(),
        "read_only": True,
        "s2s_process": {
            "port": 8765, "pid": cur_8765.get("pid"), "name": cur_8765.get("name"),
            "created": cur_8765.get("created"),
            "exe": cur_8765.get("exe"),
            "tts_flag_in_argv": tts_flag,
            "argv": L.short(argv, 900),
            "identical_to_baseline": (int(cur_8765.get("pid") or 0) ==
                                     int(base_8765[0].get("pid") or -1)),
            "alive_now": L.pid_alive(int(cur_8765.get("pid") or 0)),
        },
        "committed_backend_file": {
            "path": "runtime/windows/active_tts_backend.txt",
            "value": committed,
            "sha256": L.sha256_bytes(committed.encode("utf-8")),
            "fact": L.file_fact("runtime/windows/active_tts_backend.txt"),
        },
        "switch_journal_readonly": journal_row,
        "control_plane": control_plane,
        "per_turn": per_turn,
        "tts_line_attribution": {
            "smoke覆盖段内_TTS文本行数": len(tts_ev),
            "回合数": len(turns),
            "行时间戳严格递增": monotone,
            "配不上的余行": unassigned_tts_lines,
            "response_done_lines": resp_done_total,
            "ONE_TO_ONE": tts_bijection,
            "口径": ("先整段捞行（段内行数必须 == 回合数，多一行少一行都判 NO）、按时间严格递增、"
                     "第 i 行配第 i 回合，再逐回合比该行 length 与本回合回复字数；"
                     "另配一扇宽容紧窗（回合开始~结束后 8 秒）做「没配到离谱处」的安全检查"),
        },
        "anomaly_scan_in_smoke_span": {
            "span": [span_start.strftime(L.TS_FMT) if span_start else None,
                     span_end.strftime(L.TS_FMT) if span_end else None],
            "span_facts": span_facts,
            "patterns": list(BAD_WORDS),
            "hits": bad,
            "CLEAN": not bad,
        },
        "measurement_rulers": {
            "正对照_该数到": {"pattern": TTS_TEXT_LINE,
                              "整段命中行数": len(tts_ev),
                              "行内时间戳全部可解析": ruler_alive,
                              "RULER_ALIVE": ruler_alive},
            "负对照_该数不到": {"pattern": NULL_PATTERN, "命中总数": null_hits,
                                "NULL_CLEAN": null_ok},
            "时间戳可解析行数": span_facts.get("lines_stamped"),
        },
        "tts_numbers": {
            "regime": "本机共存负载（:8090 + :8765 + :8010 同机、显存共享），只记录不优化",
            "口径": "TTFA / RTF 数字来自 :8765 自己打的日志行，不是本脚本掐的表",
            "ttfa_s_samples_from_log": ttfa_nums,
            "rtf_samples_from_log": rtfs,
            "audio_generated_s_samples": gen_secs,
            "client_audio_bytes_by_turn": {
                str(row["turn_id"]): row["audio_bytes_estimate"] for row in per_turn},
        },
        "narrative_action_spoken_check": {
            "rows": narr,
            "verdict": "REGISTERED_NOT_FIXED",
            "说明": ("Qwen TTS 的输入长度与回复长度逐回合相等 ⇒ 回复里那串第三人称动作"
                     "叙述是整段交给 TTS 的（本链没有动作/旁白剥离层）。这是 Sola 风格"
                     "经语音链放大出来的现象，按 §1/§21 只登记不修：修它要么改 Sola 内容、"
                     "要么改 TTS/音频路由，两样都越界。"),
        },
        "TTS_OK": "YES" if tts_ok else "NO",
    }
    payload["TTS_NO_CRASH"] = "YES" if not bad and payload["s2s_process"]["alive_now"] \
        else "NO"
    return payload


# ---------------------------------------------------------------------------
# §15 Avatar
# ---------------------------------------------------------------------------

AVATAR_GLOBS: tuple[tuple[str, str], ...] = (
    ("数字人服务代码", "runtime/avatar/app"),
    ("UI 音频路由/WebRTC 客户端", "runtime/ui/app"),
    ("启停脚本", "runtime/windows"),
)
# 「本轮没改代码」这把尺只看**源码**扩展名。runtime/windows 下还躺着服务自己
# 每几秒写一次的状态 JSON（avatar_runtime_state.json / tts_switch_state.json），
# 拿易变状态当不变性判据会必然假红（本机踩过），那些文件另列信息项。
SOURCE_SUFFIXES = (".py", ".js", ".cjs", ".mjs", ".ps1", ".psm1",
                   ".html", ".htm", ".css")
NOISE_SUBSTRINGS = ("__pycache__", "\\logs\\", "\\avatars\\", "\\models\\",
                    "\\assets\\", "\\web\\", "\\data\\", "\\venv\\",
                    "/logs/", "/avatars/", "/models/", "/assets/", "/web/",
                    "/data/", "/venv/")


def mtime_audit(round_start: datetime) -> dict[str, Any]:
    """证明本轮没碰数字人 / WebRTC / 音频路由代码：只登记 mtime 落在本轮窗口内的源码文件。

    分母 = 三组目录里的源码文件（排除运行产物/依赖/缓存）。本轮自己写证据的
    目录（tests/out、tests/sola_closure01_*.py）单列，避免把「我自己的证据」
    算成「改了数字人」。
    """
    groups: dict[str, Any] = {}
    for label, rel in AVATAR_GLOBS:
        root = L.REPO / rel
        touched: list[dict[str, Any]] = []
        non_source_touched: list[dict[str, Any]] = []
        n = 0
        full_universe = 0
        if not root.exists():
            groups[label] = {"root": rel, "exists": False, "files_scanned": 0,
                             "modified_this_round": []}
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            s = str(path)
            if any(part in s for part in NOISE_SUBSTRINGS):
                continue
            full_universe += 1
            mt = datetime.fromtimestamp(path.stat().st_mtime)
            if path.suffix.lower() not in SOURCE_SUFFIXES:
                if mt >= round_start:
                    non_source_touched.append(
                        {"path": str(path.relative_to(L.REPO)),
                         "mtime": mt.strftime(L.TS_FMT),
                         "bytes": path.stat().st_size})
                continue
            n += 1
            if mt >= round_start:
                touched.append({"path": str(path.relative_to(L.REPO)),
                                "mtime": mt.strftime(L.TS_FMT),
                                "bytes": path.stat().st_size})
        groups[label] = {"root": rel, "exists": True, "files_scanned": n,
                         "universe_all_filetypes": full_universe,
                         "non_source_touched_this_round": sorted(
                             non_source_touched, key=lambda x: x["path"]),
                         "modified_this_round": sorted(touched, key=lambda x: x["path"])}
    return groups


def collect_avatar() -> dict[str, Any]:
    proof = json.loads((OUT / "single_call_proof.json").read_text(encoding="utf-8"))
    turns = proof["per_turn"]
    dts = [local_ts_to_dt(str(t.get("collected_at_local") or "")) for t in turns]
    dts = [one for one in dts if one is not None]
    walls = [float(t.get("wall_ms") or 0) / 1000.0 for t in turns]
    span_start = min(dts) - timedelta(seconds=max(walls) + 12) if dts else None
    span_end = max(dts) + timedelta(seconds=12) if dts else None

    ls = L.listeners()
    cur = (ls.get(8010) or [{}])[0]
    state = L.read_json_file("runtime/windows/avatar_runtime_state.json")
    state_row = ({k: state.get(k) for k in
                  ("avatar_id", "state", "generation", "listener_pid", "listener_pids",
                   "owner_pid", "supervisor_pid", "exit_code", "exit_reason",
                   "last_error", "started_at", "port", "task_name")}
                 if isinstance(state, dict) else {"_read_error": L.short(state)})

    hb1 = tail_lines(HB_LOG, 3)
    time.sleep(17.0)                       # 心跳周期实测 15s，取 17s 保证跨一拍
    hb2 = tail_lines(HB_LOG, 3)
    beats = [int(x) for x in re.findall(r"beats=(\d+)", "\n".join(hb1 + hb2))]
    sessions = [int(x) for x in re.findall(r"active_sessions=(\d+)",
                                           "\n".join(hb1 + hb2))]
    pids_hb = sorted({str(x) for x in re.findall(r"pid=(\d+)", "\n".join(hb1 + hb2))})

    index = L.http_json("http://127.0.0.1:8010/", timeout=20, max_bytes=20_000)
    admin_rt = L.http_json("http://127.0.0.1:8010/api/admin/runtime", timeout=20)
    admin_ss = L.http_json("http://127.0.0.1:8010/api/admin/sessions", timeout=20)
    media_ready = L.http_json("http://127.0.0.1:8010/api/media_ready", timeout=20)

    def summarize(r: dict[str, Any]) -> dict[str, Any]:
        j = r.get("json")
        return {"url": r.get("url"), "ok": r.get("ok"), "status": r.get("status"),
                "error": r.get("error"), "elapsed_ms": r.get("elapsed_ms"),
                "bytes": r.get("bytes"),
                "json": L.short(j, 900) if j is not None else L.short(
                    r.get("body_text"), 400)}

    fps_lines = [one for one in tail_lines(AV_LOG, 400) if "avg final fps" in one]
    fps_vals = [float(x) for x in re.findall(r"avg final fps:([0-9.]+)",
                                             "\n".join(fps_lines))]
    a_lines: list[str] = []
    a_facts: dict[str, Any] = {"usable": bool(span_start and span_end)}
    if span_start and span_end:
        a_lines, a_facts = window_lines(AV_LOG, span_start, span_end)
    a_bad = []
    for w in ("Traceback (most recent call last)", "CRITICAL", "ERROR:", "ERROR -",
              "Exception:"):
        hit = count(w, a_lines)
        if hit:
            a_bad.append(f"{w} -> {len(hit)} 行，例：{L.short(hit[0], 170)}")

    # 路由存在性 = 读源码里真实注册的那几行（不 POST /offer：那会真起一路会话）
    routes_src = (L.REPO / "runtime" / "avatar" / "app" / "app.py").read_text(
        encoding="utf-8", errors="replace")
    routes_py = (L.REPO / "runtime" / "avatar" / "app" / "server" / "routes.py"
                 ).read_text(encoding="utf-8", errors="replace")
    route_list = sorted(set(re.findall(r'add_(?:get|post|route)\(\s*[\'"]([^\'"]+)',
                                        routes_src + "\n" + routes_py)))

    baseline = json.loads((OUT / "baseline.json").read_text(encoding="utf-8"))
    round_start = local_ts_to_dt(str(baseline.get("collected_at_local") or "")) \
        or datetime.now() - timedelta(days=1)
    audit = mtime_audit(round_start)
    own_evidence = sorted(str(p.relative_to(L.REPO)) for p in
                          (L.REPO / "runtime" / "character" / "tests").glob(
                              "sola_closure01_*.py"))

    pid_match = (int(cur.get("pid") or 0) == int(state_row.get("listener_pid") or -1)
                 and bool(cur.get("pid")))
    beats_advanced = len(beats) >= 2 and beats[-1] > beats[0]
    av_healthy = (pid_match and L.pid_alive(int(cur.get("pid") or 0))
                  and str(state_row.get("state")) == "RUNNING"
                  and beats_advanced and not a_bad
                  and index.get("ok") is True
                  and admin_rt.get("ok") is True and admin_ss.get("ok") is True
                  and media_ready.get("ok") is True
                  and all(not one["modified_this_round"]
                          for one in audit.values() if one.get("exists", True))
                  and "/offer" in route_list)

    return {
        "task_id": L.TASK_ID,
        "stages": ["§15 数字人 / WebRTC / 音频路由链未被 Sola 接入破坏"],
        "collected_at_local": L.now_local(),
        "read_only": True,
        "process": {"port": 8010, "pid": cur.get("pid"), "name": cur.get("name"),
                    "created": cur.get("created"), "exe": cur.get("exe"),
                    "cmdline": L.short(cur.get("cmdline"), 700),
                    "alive": L.pid_alive(int(cur.get("pid") or 0)),
                    "pid_equals_state_listener_pid": pid_match},
        "lifecycle_state_file": state_row,
        "heartbeat": {"path": "logs/runtime/avatar-8010.heartbeat.log",
                      "sample_1": hb1, "sample_2_after_17s": hb2,
                      "beats": beats, "beats_advanced": beats_advanced,
                      "active_sessions": sessions,
                      "pid_in_heartbeat": pids_hb},
        "http_probes": {"root": summarize(index), "admin_runtime": summarize(admin_rt),
                        "admin_sessions": summarize(admin_ss),
                        "media_ready": summarize(media_ready)},
        "routes_registered_in_source": route_list,
        "webrtc_offer_policy": ("未 POST /offer —— 发起真实 WebRTC 会话属生产动作，"
                                "不在本轮允许范围内；改用路由注册 + 管理面只读探针。"),
        "fps_lines_tail": [L.short(one, 120) for one in fps_lines[-5:]],
        "fps_values": fps_vals[-5:],
        "avatar_log_window": {"facts": a_facts, "anomaly_hits": a_bad,
                              "positive_control_有日志行": a_facts.get("kept"),
                              "negative_control": {"pattern": NULL_PATTERN,
                                                   "hits": len(count(NULL_PATTERN, a_lines))}},
        "code_untouched_mtime_audit": {
            "round_start": round_start.strftime(L.TS_FMT),
            "groups": audit,
            "本轮自己写的文件（不属于数字人/WebRTC/音频路由）": own_evidence,
        },
        "AVATAR_CHAIN_HEALTHY": "YES" if av_healthy else "NO",
    }


def main() -> int:
    tts = collect_tts()
    p1 = L.write_json("tts_smoke.json", tts)
    print("TTS_OK =", tts["TTS_OK"], "| TTS_NO_CRASH =", tts["TTS_NO_CRASH"], "->", p1)
    for row in tts["per_turn"]:
        print("  ", row["turn_id"], "reply_chars", row["reply_chars"],
              "attribution", row.get("attribution"),
              "tts_lengths", row.get("tts_text_lengths_seen"),
              "audio_deltas", row.get("audio_delta_events"),
              "audio_bytes", row.get("audio_bytes_estimate"),
              "eq", row.get("TTS_INPUT_EQUALS_REPLY"))
    print("  1-to-1 =", tts["tts_line_attribution"]["ONE_TO_ONE"],
          "| TTS 行数", tts["tts_line_attribution"]["smoke覆盖段内_TTS文本行数"],
          "| 递增", tts["tts_line_attribution"]["行时间戳严格递增"],
          "| response.done", tts["tts_line_attribution"]["response_done_lines"])
    print("  anomalies:", tts["anomaly_scan_in_smoke_span"]["hits"] or "none")

    av = collect_avatar()
    p2 = L.write_json("avatar_smoke.json", av)
    print("AVATAR_CHAIN_HEALTHY =", av["AVATAR_CHAIN_HEALTHY"], "->", p2)
    print("  pid", av["process"]["pid"], "== state",
          av["lifecycle_state_file"].get("listener_pid"),
          "| beats", av["heartbeat"]["beats"], "| sessions",
          av["heartbeat"]["active_sessions"])
    print("  mtime touched:",
          {k: len(v.get("modified_this_round", [])) for k, v in
           av["code_untouched_mtime_audit"]["groups"].items()})
    return 0 if (tts["TTS_OK"] == "YES" and av["AVATAR_CHAIN_HEALTHY"] == "YES") else 3


if __name__ == "__main__":
    sys.exit(main())
