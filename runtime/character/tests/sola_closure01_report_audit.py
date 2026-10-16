# -*- coding: utf-8 -*-
"""sola_closure01_report_audit.py — 收口报告的独立自检（§22 交付件之一）。

做两件事，都不写任何生产/证据内容：
  1) `eq`：拿产物自己复算一遍报告里引用的数字（中位数、字节求和、台账闭合）；
  2) `in_report`：断言报告正文里确实写着那个字面 —— 两半合起来才是
     「报告里的这个数字既和产物一致、又真的是报告里的那个数」。

任何一条不过线就 exit 1。本脚本只读产物，不调模型、不改文件。
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / "sola_unattended_closure_01"
REPORT = OUT / "SOLA_V2_EMBER_UNATTENDED_CLOSURE_REPORT.md"
TEXT = REPORT.read_text(encoding="utf-8")

FAILS: list[str] = []
PASS_N = 0


def j(name: str) -> dict:
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def eq(label: str, a: object, b: object) -> None:
    global PASS_N
    ok = str(a) == str(b)
    if not ok:
        # 数值型对拍走绝对容差（产物里存的是未 round 的浮点，如 149.10000000000002）
        try:
            ok = abs(float(a) - float(b)) < 1e-6  # type: ignore[arg-type]
        except (TypeError, ValueError):
            ok = False
    print(("PASS " if ok else "FAIL ") + f"[对拍] {label}: {a!r} vs {b!r}")
    PASS_N += 1
    if not ok:
        FAILS.append(f"eq:{label}")


def in_report(label: str, literal: str) -> None:
    global PASS_N
    ok = literal in TEXT
    print(("PASS " if ok else "FAIL ") + f"[文本] {label}: 「{literal}」")
    PASS_N += 1
    if not ok:
        FAILS.append(f"text:{label}")


R = [json.loads(l) for l in (OUT / "smoke_rows.jsonl").read_text(encoding="utf-8")
     .splitlines() if l.strip()]
A = j("avatar_smoke.json")
P = j("protection.json")
G = j("gpu_ram_post.json")
S = j("single_call_proof.json")
T = j("tts_smoke.json")
B = j("baseline.json")
RP = j("runtime_prompt_proof.json")
RB = j("rollback_proof.json")
RG = j("regression_summary.json")
K = j("key_screen.json")

print("=== 1. 四发原始回复：报告逐字抄的那 4 段 ===")
for r in R:
    body = r["reply"]
    head = body.strip().splitlines()[0][:24]
    in_report(f"{r['turn_id']} 回复首行逐字出现", head)
    eq(f"{r['turn_id']} 回复字符数（含段间换行）== 产物 output_chars",
       len(body), r["output_chars"])
    eq(f"{r['turn_id']} 换行数（产物 output_chars 含换行这一说法成立）",
       r["output_chars"] - len(body.replace("\n", "")), body.count("\n"))
    if r["turn_id"] == "T1":
        # 报告正文里那句「T1=58 即 56 字 + 2 个换行」必须与实物相符
        eq("T1 去换行字数 = 56", len(body.replace("\n", "")), 56)
        eq("T1 换行数 = 2", body.count("\n"), 2)
        in_report("报告写了 T1 的含换行口径", "T1=58 即 56 字 + 2 个换行")
    eq(f"{r['turn_id']} 无 error/超时", f"{r['error']}/{r['timeout']}", "None/None")
    eq(f"{r['turn_id']} finish", r["finish"], "completed")

print("\n=== 2. 性能：每个中位数都从 4 发现场重算 ===")
ttft = [r["first_content_ms"] for r in R]
fa = [r["first_audio_ms"] for r in R]
wall = [r["wall_ms"] for r in R]
pre = [r["llama_prompt_eval_ms"] for r in R]
dec = [r["llama_decode_ms"] for r in R]
eq("TTFT 逐发", " / ".join(str(v) for v in ttft), "875 / 906 / 750 / 1703")
eq("首个音频包逐发", " / ".join(str(v) for v in fa), "1141 / 1156 / 1015 / 1922")
eq("墙钟逐发", " / ".join(str(v) for v in wall), "3797 / 5375 / 4188 / 11500")
eq("prefill 逐发", " / ".join(str(v) for v in pre), "312.6 / 148.3 / 141.2 / 149.9")
eq("decode 逐发", " / ".join(str(v) for v in dec), "812.3 / 877.2 / 711.1 / 1636.7")
eq("prompt_tokens 逐发", " / ".join(str(r["prompt_tokens"]) for r in R),
   "2410 / 2420 / 2422 / 2433")
eq("出词 tokens 逐发", "/".join(str(r["completion_tokens"]) for r in R), "57/83/64/166")
eq("出词字数逐发", "/".join(str(r["output_chars"]) for r in R), "58/98/75/207")
for name, lit, val, art_key in (
        ("TTFT 中位", "890.5", statistics.median(ttft), "median_TTFT_ms"),
        ("首个音频包中位", "1148.5", statistics.median(fa),
         "median_first_audio_packet_ms"),
        ("墙钟中位", "4781.5", statistics.median(wall), "median_wall_ms"),
        ("prefill 中位", "149.1", round(statistics.median(pre), 1),
         "median_llama_prompt_eval_ms"),
        ("出词字数中位", "86.5", statistics.median([r["output_chars"] for r in R]),
         "median_completion_chars"),
        ("出词 tokens 中位", "73.5", statistics.median([r["completion_tokens"] for r in R]),
         "median_completion_tokens"),
        ("prompt_tokens 中位", "2421", statistics.median([r["prompt_tokens"] for r in R]),
         "median_prompt_tokens")):
    in_report(f"报告写了 {name}", lit)
    eq(f"{name} 重算 == 产物 {art_key}", val, S["performance"][art_key])

print("\n=== 3. 语音侧：字节↔秒 跨通道闭合 ===")
total_bytes = sum(r["audio_bytes"] for r in R)
eq("客户端音频总字节", total_bytes, 2690181)
in_report("报告写了总字节数", "2,690,181")
eq("折算秒（16kHz 单声道 PCM16）", round(total_bytes / 32000, 2), 84.07)
in_report("报告写了折算秒", "84.07")
alllines = "\n".join(l for one in T["per_turn"] for l in one["tts_audio_done_lines"])
uniq = {}
for m in re.finditer(r"generated ([\d.]+)s audio in ([\d.]+)s \(RTF: ([\d.]+)", alllines):
    uniq[m.group(0)] = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
eq("日志里的生成段数（去重）", len(uniq), 7)
in_report("报告写了 7 段", "7 段")
dur = sum(d for d, _g, _r in uniq.values())
gen = sum(g for _d, g, _r in uniq.values())
eq("音频秒求和", round(dur, 2), 83.95)
in_report("报告写了音频总时长", "83.95")
eq("生成耗时求和", round(gen, 2), 20.60)
in_report("报告 wrote 生成总耗时", "20.60")
eq("整体倍率 = 83.95/20.60", round(dur / gen, 2), 4.08)
in_report("报告写了整体倍率", "4.08")
ratios = sorted(r for _d, _g, r in uniq.values())
eq("逐段倍率区间", f"{min(ratios)}–{max(ratios)}", "3.59–4.2")
in_report("报告写了逐段区间", "3.59–4.20")
ttfas = sorted(float(m.group(1)) for m in re.finditer(r"TTFA: ([\d.]+)s",
               "\n".join(l for one in T["per_turn"] for l in one["tts_ttfa_lines"])))
eq("TTFA 区间", f"{min(ttfas):.2f}–{max(ttfas):.2f}", "0.20–0.24")
in_report("报告写了 TTFA 区间", "0.20–0.24")
eq("两路时长差 %", round(abs(dur - total_bytes / 32000) / dur * 100, 2), 0.14)
in_report("报告写了差值", "0.14%")

print("\n=== 4. 只删不改的台账 ===")
bs = P["checks"]["block_subsequence"]
for lit, val in ((str(bs["original_bytes"]), "7469"), (str(bs["original_chars"]), "7363"),
                 (str(bs["cleaned_bytes"]), "7093"), (str(bs["cleaned_chars"]), "7011"),
                 (str(bs["delta_chars"]), "352")):
    eq("台账字面 == 产物", lit, val)
    in_report("报告写了这个数", val)
eq("算术闭合 7363-7011=352", bs["original_chars"] - bs["cleaned_chars"],
   bs["delta_chars"])
sl = P["checks"]["spec_ledger"]
eq("声明条数 == 应用条数", sl["edits_declared"], sl["edits_applied"])
in_report("报告写了 6 条", "6 条")
eq("类别白名单之外零命中", sorted(sl["categories_seen"]),
   ["action_output", "sola_identity"])
pd = P["checks"]["pure_deletion_reinsert"]
eq("未点名模块全部共用同一对象",
   pd["modules_total"] - pd["modules_touched"], pd["modules_untouched_share_same_object"])
in_report("报告写了 89/3/86", "89 个模块里只有 3 个被动过，其余 86 个")
cats = P["checks"]["protected_categories"]["categories"]
order = ("ADULT_CONTENT_PRESERVED", "VIOLENCE_CONTENT_PRESERVED",
         "DARK_CONTENT_PRESERVED", "AUTONOMY_RULES_PRESERVED")
eq("四类锚点数", "/".join(str(cats[k]["锚句数"]) for k in order), "4/4/5/6")
in_report("报告写了 4/4/5/6", "（4/4/5/6）")
eq("锚点合计", sum(cats[k]["锚句数"] for k in order), 19)
in_report("报告写了 19 条", "19 条成句锚点")
eq("四类全部通过", all(cats[k]["通过锚句数"] == cats[k]["锚句数"] for k in order), True)
eq("PRODUCTION_PROTECTION", P["checks"]["PRODUCTION_PROTECTION"]["PASS"], True)
in_report("报告写了保留结论", "PRODUCTION_PROTECTION")

print("\n=== 5. token 台账与在线块 ===")
tl = RP["token_ledger"]
eq("基底实测", tl["BASE_SYSTEM_PROMPT_TOKENS"], 402)
eq("Sola 实测", tl["SOLA_PROMPT_TOKENS"], 1684)
eq("合计实测", tl["FINAL_PROMPT_TOKENS"], 2087)
in_report("报告写了 402/1684/2087", "402 / Sola 1684 / 合计 2087")
lp = RP["live_prompt_endpoint"]
eq("在线整块字数", lp["chars"], 7574)
eq("在线整块字节", lp["bytes"], 8742)
in_report("报告写了在线块 7574/8742", "7574 字 / 8742 字节")
eq("在线块 SHA 前缀", lp["sha256"][:12], "280a4f7df875")
in_report("报告写了在线块 SHA", "`280a4f7df875…`")
eq("sola 块 SHA 前缀", RP["sola_block"]["sha256"][:12], "90c52e33c567")
in_report("报告写了清洗块 SHA", "`90c52e33c567…`")
sz = RP["system_block_sizes"]
lit = "policy(%d字)→profile(%d字)→tools(%d字)→sola(%d字)" % (
    sz["policy"], sz["profile"], sz["tools"], sz["sola"])
in_report("报告写了块序与字数", lit)
eq("块序 == 产物", "".join(RP["system_blocks_present_in_order"]), "policyprofiletoolssola")
eq("Sola 在最后一块", RP["system_blocks_present_in_order"][-1], "sola")
eq("产物里 sola_is_last_block 为真", RP["sola_is_last_block"], True)
eq("只有一条 system message", RP["SYSTEM_MESSAGE_COUNT"], 1)
eq("禁用字面命中 0", RP["forbidden_literals_hit_total"], 0)
in_report("报告写了 forbidden=0", "`forbidden_literals_hit_total=0`")

print("\n=== 6. 服务/配置现场读数 ===")
key = K["keys"]
PR = j("post_restart_recheck.json")
# 3A = 验收窗口那批 pid（冻结事实，仍逐条存在复核产物里）；
# 3D = 第三次事故后的终态：四路全空，键屏读的就是这个空。
WINDOW_PIDS = {"7860": 48988, "8090": 18748, "8765": 34208, "8010": 12296}
for port, lit in WINDOW_PIDS.items():
    tag = f"{port}_listener"
    rec = PR["old_window_pid_status"][tag]
    eq(f"§3A :{port} pid 与复核产物一致", str(rec["pid"]), str(lit))
    eq(f"§3A 那个旧 pid 现在确实不在了（{tag}）", rec["alive"], False)
    in_report(f"报告 §3A 写了窗口 pid :{port}", str(lit))
    now_pid = PR["post_listeners"][port]
    eq(f"键屏 {port}_PID == 复核产物 post_listeners", key[f"{port}_PID"]["value"], now_pid)
    eq(f":{port} 终态无 listener（第三次事故后，键屏不许写假 pid）", now_pid, None)
    in_report(f"报告 §3D 写了键屏 {port}_PID=None", f"`{port}_PID=None`")
row = [x for x in B["services"][port]["listeners"]]
eq("基线时 :8090 未监听", B["services"]["8090"]["listening"], False)
eq("基线时 :7860 是老 pid", B["services"]["7860"]["listeners"][0]["pid"], 9152)
in_report("报告写了 :7860 从 9152 换到 48988", "9152（09-27 16:36:53）→ 48988")
eq("机器没重启（事故不是 reboot 造成的）", PR["machine_rebooted"]["verdict"], "NO_REBOOT")
in_report("报告写了不是重启", "`NO_REBOOT`")
# 6a) 终态"读不到"与"读到了别的"必须分开：端点不可达 ⇒ 四把在线尺为 false，
#     而它们 false 的原因是采到 0 字节（空输入的 SHA256 就是这一事实的字迹）。
LP = PR["live_prompt_after_restart"]
eq("终态 :7860 端点不可达", LP["http_ok"], False)
eq("终态端点 HTTP 状态为 0（连状态码都没拿到）", LP["http_status"], 0)
eq("终态端点采到 0 字节", LP["bytes"], 0)
eq("采到的 0 字节 == 空输入的 SHA256（证明是'没读到'而非'读到了不同内容'）",
   LP["sha256"], hashlib.sha256(b"").hexdigest())
for _rk in ("LIVE_BLOCK_SHA_EQUALS_FROZEN", "LIVE_CHARS_EQUALS_FROZEN",
            "LIVE_BYTES_EQUALS_FROZEN", "CLEAN_SOLA_BLOCK_SHA_EQUALS_FROZEN"):
    eq(f"端点不可达 ⇒ 在线尺 {_rk} 为 false", PR["restart_identity_verdicts"][_rk], False)
eq("冻结参照仍写在产物里（块 SHA / 字数 / 字节）",
   (PR["restart_identity_verdicts"]["frozen_reference"]["sha256"],
    PR["restart_identity_verdicts"]["frozen_reference"]["chars"],
    PR["restart_identity_verdicts"]["frozen_reference"]["bytes"]),
   (RP["live_block_sha256"], RP["inprocess_mirror_block_chars"],
    RP["live_prompt_endpoint"]["bytes"]))
in_report("报告 §3D 写了空输入 SHA 这一事实", LP["sha256"])
# 6b) 不依赖服务的那一半：离线装配复算，必须与冻结件逐字相同
OA = PR["terminal_offline_assembly"]
eq("离线复算 rc=0", OA["rc"], 0)
eq("离线整块 SHA == 冻结在线块 SHA", OA["system_text_sha256"], RP["live_block_sha256"])
eq("离线清洗块 SHA == 冻结清洗块 SHA", OA["sola_block_sha256"], RP["sola_block"]["sha256"])
eq("离线整块字数 == 冻结字数", OA["system_text_chars"], RP["inprocess_mirror_block_chars"])
eq("离线清洗块字数 == 冻结清洗块字数", OA["sola_block_chars"], RP["sola_block"]["chars"])
eq("离线块序 == 冻结块序", OA["block_names_in_order"], RP["system_blocks_present_in_order"])
eq("离线块尺寸 == 冻结块尺寸", OA["block_sizes"], RP["system_block_sizes"])
eq("离线 render_state.mode == voice_minimal", OA["render_state"]["mode"], "voice_minimal")
eq("离线开关来源 == 配置文件", OA["switch_authority"]["开关来源"], "配置文件")
eq("离线两枚判决", (OA["OFFLINE_SYSTEM_SHA_EQUALS_FROZEN"],
                   OA["OFFLINE_SOLA_BLOCK_SHA_EQUALS_FROZEN_CLEAN"]), (True, True))
eq("离线复算的冻结参照来源就是那份冻结产物", OA["frozen_reference"]["live_block_sha256"],
   RP["live_block_sha256"])
in_report("报告 §3D 写了离线整块完整 SHA", OA["system_text_sha256"])
in_report("报告 §3D 写了离线清洗块完整 SHA", OA["sola_block_sha256"])
in_report("报告 §3D 写了离线判决键名值对", "`OFFLINE_SYSTEM_SHA_EQUALS_FROZEN=true`")
in_report("报告 §3D 写了第二枚离线判决", "`OFFLINE_SOLA_BLOCK_SHA_EQUALS_FROZEN_CLEAN=true`")
eq("§3B/§3C 那两个 SHA 的权威落盘处仍是未动的冻结件",
   (RP["live_block_sha256"], RP["sola_block"]["sha256"]),
   (PR["restart_identity_verdicts"]["frozen_reference"]["sha256"],
    PR["restart_identity_verdicts"]["frozen_reference"]["sola_block_sha256"]))
in_report("报告 §3C 写了完整在线块 SHA", RP["live_block_sha256"])
in_report("报告 §3C 写了完整清洗块 SHA", RP["sola_block"]["sha256"])
in_report("报告 §3B 写了完整在线块 SHA", RP["live_block_sha256"])
in_report("报告 §3B 写了完整清洗块 SHA", RP["sola_block"]["sha256"])
in_report("报告登记了复核产物被覆写这件事", "已被 12:23 的复采覆写")
eq("恢复回执末行", PR["restoration_receipt"]["last_line"].endswith("START_NATIVE_OK backend=qwen3"), True)
in_report("报告写了恢复回执末行", "START_NATIVE_OK backend=qwen3")
SC = PR["second_channel_exclusion"]
eq("第二通道：共享状态文件里 stopped 记录全枚举分母", SC["stopped_records_total"], 6)
in_report("报告写了全枚举分母 = 6 条", "全枚举分母 = 6 条")
eq("第二通道：今天没有署名的外部停服记录", SC["stopped_records_dated_2026_09_28"], 0)
eq("第二通道判决", SC["verdict"], "NO_SIGNED_EXTERNAL_STOP_TODAY")
in_report("报告写了第二通道的否定结论", "没有一条属于今天")
eq("第二通道文件 mtime 早于今日", SC["file_mtime_local"] < "2026-09-28", True)
GEN = PR["post_state_files"]["avatar_generation"]
eq("最后一次恢复时 generation 比 3A 窗口高（走过重启周期）", isinstance(GEN, int) and GEN > 72, True)
in_report(f"报告写了恢复后 generation={GEN}", f"generation={GEN}")
# 陈旧台账登记：状态文件还说 RUNNING，而那一路的 listener 已经空了
eq("状态文件仍写 RUNNING 而 :8010 无 listener（陈旧，报告必须登记而不是据此判活）",
   (PR["post_state_files"]["avatar_state"], PR["post_listeners"]["8010"]), ("RUNNING", None))
in_report("报告 §3D 写了这条陈旧台账", "generation=74 state=RUNNING listener_pid=8140")
in_report("报告 §3D 登记了 service_pids 的陈旧 pid 台账", "s2s tracked=24668")
# 这一条不许我手写数字：generation 与 supervisor 日志里最后一次 LAUNCH_OK 的 gen 必须同源
last_launch_ok = [x for x in PR["incident_timeline_from_service_logs"]["avatar_supervisor_log"]["lines_2026_09_28"]
                  if "LAUNCH_OK" in x][-1]
eq("supervisor 日志最后一次 LAUNCH_OK 的 gen == 状态文件 generation",
   f"gen={GEN}" in last_launch_ok, True)

eq("恢复后 avatar_id 未变", PR["post_state_files"]["avatar_id"],
   A["lifecycle_state_file"]["avatar_id"])
eq("恢复后 committed backend 未变", PR["post_state_files"]["active_tts_backend"],
   T["committed_backend_file"]["value"])
eq("恢复后开关仍 true/true",
   (PR["switch_authority_after_restart"]["sola_v2_ember_enabled"],
    PR["switch_authority_after_restart"]["voice_minimal_enabled"]), (True, True))
eq("红过的套件在 0/4 监听下仍判绿（口径是 tracked dead OK，报告必须写出来）",
   PR["test_state_single_source_rerun"]["PASS_counted"], True)
in_report("报告写了套件重跑结果", "test_state_single_source: PASS (19)")
in_report("报告写了这条 PASS 的判定口径", "no listener; tracked dead OK")
eq("重跑 rc", PR["test_state_single_source_rerun"]["rc"], 0)
eq("POST_RESTART_VERDICT 终态值", PR["POST_RESTART_VERDICT"],
   "BLOCKED_ALL_LISTENERS_GONE_DISK_ASSEMBLY_STILL_BYTE_IDENTICAL")
in_report("报告写了终态复核判决", PR["POST_RESTART_VERDICT"])
in_report("报告写了复核判决文件名", "`post_restart_recheck.json`")
# 6b) 事故时间线：正文里那些"几点几分被终结""官方停服没跑过"必须由服务自己的日志行供养，
#     而不是我手写。判"没跑过官方停服"用的是 stop.log 当天行数=0 这个可复算分母。
import re as _re
IT = PR["incident_timeline_from_service_logs"]
eq("官方停服入口今天没跑过（stop.log 当天行数=0）",
   len(IT["official_stop_log"]["lines_2026_09_28"]), 0)
eq("事故判决：NO_OFFICIAL_STOP_SCRIPT_TODAY", IT["verdicts"]["NO_OFFICIAL_STOP_SCRIPT_TODAY"], True)
EXIT_LINES = [x for x in IT["llm_task_run_log"]["lines_2026_09_28"] if "task exit code=-1" in x]
START_LINES = [x for x in IT["llm_task_run_log"]["lines_2026_09_28"] if "task start" in x]
eq("llama 侧今天被外部终结的书写次数", len(EXIT_LINES), 1)
eq("llama 侧今天 task start 次数（04:36 / 10:29 / 10:55 三次）", len(START_LINES), 3)
EXIT_TIMES = [_re.search(r"(\d\d:\d\d:\d\d)", x).group(1) for x in EXIT_LINES
              if _re.search(r"(\d\d:\d\d:\d\d)", x)]
eq("唯一一条 exit 字迹的时刻 = 10:49:33（第二次）", EXIT_TIMES, ["10:49:33"])
eq("第三次事件（11:2x–12:1x）没留下 exit 字迹 ⇒ 与第一次同形、连书写 wrapper 一起没了",
   [t for t in EXIT_TIMES if t >= "11:00:00"], [])

# 尺的正控制：这三份日志当天都采到了行；stop.log 单独是 0，那是"没跑过"而不是"没采到"
SC_TODAY = {k: v["lines_today"] for k, v in IT["probe_self_control"].items()}
eq("尺正控制：三份事故日志当天非空",
   all(SC_TODAY[k] > 0 for k in SC_TODAY if "stop.log" not in k), True)
eq("尺正控制：stop.log 当天为 0 且文件本身有历史行",
   (SC_TODAY["logs/runtime/stop.log"], bool(IT["official_stop_log"]["lines_2026_09_28"]) is False),
   (0, True))
eq("第一次整机消失没留下 exit 行（连书写 wrapper 一起没了）",
   any(f"10:{mm}" in x for x in EXIT_LINES for mm in ("0", "1", "2")), False)
eq("事故判决：LLM_EXIT_CODE_MINUS_1_SEEN", IT["verdicts"]["LLM_EXIT_CODE_MINUS_1_SEEN"], True)
in_report("报告逐字写了 llama 被终结的字迹", "task exit code=-1")
for _x in EXIT_LINES:
    _m = _re.search(r"(\d\d:\d\d:\d\d)\.\d+", _x)
    if _m:
        in_report(f"报告写了 llama 被终结的时刻 {_m.group(1)}", _m.group(1))
        eq(f"该时刻确实落在今天的行里（{_m.group(1)}）",
           "2026-09-28" in _x.replace("/", "-"), True)
SUP_TODAY = IT["avatar_supervisor_log"]["lines_2026_09_28"]
eq("supervisor 今天的书写条数", len(SUP_TODAY), 10)
in_report("报告写了 supervisor 今天的书写条数", "今天 10 行")
eq("第三次事件里 supervisor 没有新的 LAUNCH_OK（今天仍只有 gen=73/gen=74 两次重拉）",
   sum(1 for x in SUP_TODAY if "LAUNCH_OK" in x), 3)
eq("今天的 LAUNCH_OK 涉及两代（73、74）",
   sorted({x.split("gen=")[1].split()[0] for x in SUP_TODAY if "LAUNCH_OK" in x}), ["73", "74"])
EXW = IT["avatar_exitwitness_log"]["lines_2026_09_28"]
eq("exitwitness 今天的行数", len(EXW), 4)
eq("exitwitness 今天的时间全部落在两次拉起（10:31/10:49），第三次消失没留下任何退出字迹",
   [t for t in (_re.search(r"T(\d\d:\d\d:\d\d)", x).group(1) for x in EXW) if t >= "11:00:00"], [])
eq("事故判决：AVATAR_SUPERVISOR_RELAUNCHED_TODAY", IT["verdicts"]["AVATAR_SUPERVISOR_RELAUNCHED_TODAY"], True)
in_report("报告写了 supervisor 自己把 :8010 重拉过", "`LAUNCH_OK`")
in_report("报告写了官方停服日志当天零行", "当天的行数 = **0**")
in_report("报告写了第三次事件里停服日志仍 0 行", "今天的行数仍然 = **0**")
OSL = PR["official_start_lines_today"]
eq("startup.log 今天的官方入口书写条数（两次成功各 4 行 + 第三次拒绝 2 行）", OSL["count"], 10)
eq("最后一次**成功**回执仍是第二次的 START_NATIVE_OK",
   OSL["last_start_native_ok"].endswith("START_NATIVE_OK backend=qwen3"), True)
eq("最后一次成功回执的时刻落在今天", "2026-09-28" in OSL["last_start_native_ok"], True)
in_report("报告写了第二次恢复的时刻与回执",
          OSL["last_start_native_ok"].split("]")[0][12:])
# 6c) 阻塞本体：官方入口的失败字迹 + 四个任务的当前"模式"字段，逐条来自产物
HB = PR["hard_blocker"]
eq("今天的 START_NATIVE_FAILED 行数 = 1（第三次那一次）",
   len(HB["official_start_failed_lines_today"]), 1)
eq("失败发生在 :8090 阶段且原因写字面是『任务被禁用』",
   ("stage=8090" in HB["official_start_last_attempt"], "任务被禁用" in HB["official_start_last_attempt"]),
   (True, True))
eq("hard_blocker 记的终态监听数", HB["live_listeners_at_recheck"], 0)
eq("四个项目计划任务全部已禁用", HB["all_four_tasks_disabled"], True)
eq("根因名由产物生成（不是手打）", HB["FINAL_RESULT_if_blocked"],
   "BLOCKED_HOST_STACK_TORN_DOWN_EXTERNALLY_AND_ALL_GIRLFRIEND_TASKS_DISABLED")
eq("解禁动作的越界理由写在产物里", HB["enabling_a_task_is_out_of_scope_because"],
   "§21 硬停止条件「必须改系统设置」")
for _tn, _tv in HB["girlfriend_scheduled_tasks"].items():
    eq(f"{_tn} 逐条 rc=0 且模式字段读到（读到『已禁用』而非『未读到』）",
       (_tv["rc"], _tv["mode_field"]), (0, "已禁用"))
    in_report(f"报告写了 {_tn}", _tn)
in_report("报告逐字写了官方入口的失败字迹", HB["official_start_last_attempt"])
in_report("报告写了 §21 的边界用词", "「必须改系统设置」")
in_report("报告写了第三次尝试不绕开官方入口", "没有自造启动方式")

eq("模型名", key["LLM_MODEL"]["value"],
   "Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M.gguf")
eq("THINKING_OFF", key["THINKING_OFF"]["value"], "YES")
eq("UI 新代码已加载", key["UI_NEW_CODE_LOADED"]["value"], "YES")
eq("人物指针有效", key["CHARACTER_POINTER_VALID"]["value"], "YES")
eq("通用助手回落", key["GENERIC_ASSISTANT_FALLBACK"]["value"], "NO")
eq("运行态 Sola", key["SOLA_RUNTIME_ACTIVE"]["value"], "YES")
eq("运行态 Voice Minimal", key["VOICE_MINIMAL_ACTIVE"]["value"], "YES")
eq("数字人 generation", A["lifecycle_state_file"]["generation"], 72)
in_report("报告写了 generation=72", "`generation=72`")
eq("数字人 avatar_id", A["lifecycle_state_file"]["avatar_id"], "av_ab077e4abe")
eq("数字人 fps 抽样区间",
   f"{min(A['fps_values']):.4f}–{max(A['fps_values']):.3f}", "24.9983–25.088")
in_report("报告写了 fps 区间", "24.9983–25.088")
eq("committed backend", T["committed_backend_file"]["value"], "qwen3")
eq("切换事务 phase", T["control_plane"]["switch_phase"], "COMMITTED")
eq("TTS 逐回合文本行 == 回合数", T["tts_line_attribution"]["smoke覆盖段内_TTS文本行数"],
   T["tts_line_attribution"]["回合数"])
eq("TTS 一一配对", T["tts_line_attribution"]["ONE_TO_ONE"], True)
in_report("报告写了一一配对", "命中 **4 行 == 4 回合**")

print("\n=== 7. §20 判决与 §19 回归 ===")
for k, want in (("GPU_HEALTHY", "YES"), ("RAM_HEALTHY", "YES"), ("OOM", "NO"),
                ("OFFLOAD_ANOMALY", "NO"), ("GPU_FOREIGN_HEAVY_COMPUTE", "NO"),
                ("RAM_PRESSURE", "NO")):
    eq(f"产物 verdicts.{k}", G["verdicts"][k], want)
    in_report(f"报告写了 {k}={want}", f"`{k}={want}`")
eq("ALL_GREEN", G["ALL_GREEN"], True)
eq("回归绿/红", f"{RG['GREEN']}/{RG['RED']}", "8/1")
in_report("报告写了回归结论", "8/9_SUITES_GREEN_1_PREEXISTING_B_CLASS_REGISTERED")
eq("回归红的就是那一组", RG["RED_SUITES"], ["runtime/tests/test_state_single_source.py"])
eq("回归期间模型调用 0", RG["regression_run_made_llm_calls"], 0)
in_report("报告写了回归零调用", "processing task` 新增 0 行")
eq("OFF_3 其余段拼接 ON==OFF",
   RB["checks"]["OFF_3_ember_off_is_byte_identical_pre_sola"]["其余段拼接sha_ON"],
   RB["checks"]["OFF_3_ember_off_is_byte_identical_pre_sola"]["其余段拼接sha_OFF"])
in_report("报告写了 OFF_3 那对 SHA", "`99ddcd2610d9…` == `99ddcd2610d9…`")
eq("OFF_1 退回原版 SHA", RB["checks"]["OFF_1_voice_minimal_disappears"]["该回合块sha256"],
   RB["checks"]["OFF_1_voice_minimal_disappears"]["冻结原版sha256"])
eq("ROLLBACK_REVERSIBLE", RB["ROLLBACK_REVERSIBLE"], "YES")
eq("终态仍开着", RB["PRODUCTION_LEFT_ON"], True)
eq("判断器调用 0", RB["checks"]["TURN_JUDGE_CALLS"], 0)

print("\n=== 8. 结构与判决 ===")
M = j("manifest.json")
eq("清单里的采集器脚本数", len(M["collector_scripts"]), 14)
in_report("报告写了 14 个采集器脚本", "14 行，含逐脚本 SHA")
eq("采集器无缺失", M["missing_collectors"], [])
eq("冻结输入无缺失", M["missing_frozen_inputs"], [])
eq("本目录实际文件数 == 报告写的 21",
   len([p for p in OUT.iterdir() if p.is_file()]), 21)
in_report("报告写了 21 个文件", "= 21 个文件")
disk_bad = []
rows = 0
for sec in ("artifacts", "extra_files_in_dir", "collector_scripts", "frozen_inputs"):
    for one in M[sec]:
        rows += 1
        if one["name"] == "report_audit.log":
            # 本脚本自己的 stdout：清单只能是"这一跑之前"的那份日志的哈希，
            # 属自指，交给冻结之后的外部复算（第 8 节写了怎么复算）。
            continue
        p = Path(one["path"])
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != one["sha256"]:
            disk_bad.append(one["name"])
eq("清单逐行哈希 == 磁盘态（38 行，排除本日志的自指）", disk_bad, [])
eq("清单行数（12 产物 + 8 额外文件 + 14 脚本 + 5 冻结输入）", rows, 39)
disk = sorted(p.name for p in OUT.iterdir() if p.is_file())
covered = {Path(one["path"]).name
           for sec in ("artifacts", "extra_files_in_dir") for one in M[sec]}
eq("本目录里没被清单覆盖的文件只有 manifest 自己（不能自指）",
   [x for x in disk if x not in covered], ["manifest.json"])
eq("报告里的 §3A 窗口 pid 全部由复核产物背书",
   sorted(str(v["pid"]) for k, v in PR["old_window_pid_status"].items() if k.endswith("_listener")),
   sorted(["48988", "18748", "34208", "12296"]))
heads = re.findall(r"^## (\d)\. ", TEXT, re.M)
eq("§22 点名的九节齐全有序", "".join(heads), "123456789")
in_report("报告写了最终判决",
          "FINAL_RESULT=BLOCKED_HOST_STACK_TORN_DOWN_EXTERNALLY_AND_ALL_GIRLFRIEND_TASKS_DISABLED")
eq("判决 == 键屏", key["FINAL_RESULT"]["value"], HB["FINAL_RESULT_if_blocked"])
eq("判决 == 复核产物生成的根因名（不是占位）", key["FINAL_RESULT"]["value"],
   "BLOCKED_HOST_STACK_TORN_DOWN_EXTERNALLY_AND_ALL_GIRLFRIEND_TASKS_DISABLED")
eq("判决不是 BLOCKED_SEE_KEY_SCREEN 这种占位名",
   key["FINAL_RESULT"]["value"].endswith("SEE_KEY_SCREEN"), False)
eq("READY_FOR_OWNER_USE", key["READY_FOR_OWNER_USE"]["value"], "NO")
in_report("报告写了 READY_FOR_OWNER_USE=NO", "`READY_FOR_OWNER_USE=NO`")
eq("SERVICES_HEALTHY 键屏值 == 终态监听数派生（0/4 ⇒ NO）",
   key["SERVICES_HEALTHY"]["value"], "NO")
in_report("报告写了 SERVICES_HEALTHY=NO", "`SERVICES_HEALTHY=NO`")
eq("键屏合取失败恰好只有 SERVICES_HEALTHY 这一条",
   [x.split("=")[0] for x in K["conjunction_failures"]], ["SERVICES_HEALTHY"])
eq("合取失败条数", len(K["conjunction_failures"]), 1)

bad_tables = []
for block in re.findall(r"(?:^\|.*\|\s*$\n?)+", TEXT, re.M):
    cols = [line.count("|") for line in block.strip().splitlines()]
    if len(set(cols)) != 1:
        bad_tables.append(cols)
eq("markdown 表列数自洽", bad_tables, [])
# 报告里出现的每个 `XXX=YYY` 判决 token 都要能在键屏里找到（防写错键名）
stuck = []
for m in re.finditer(r"`([A-Z][A-Z0-9_]{3,})=([A-Za-z0-9_./,]+)`", TEXT):
    kk, vv = m.group(1), m.group(2)
    if kk in key and str(key[kk]["value"]) != vv:
        stuck.append((kk, vv, key[kk]["value"]))
eq("报告里的键值与键屏无冲突", stuck, [])
print("  note 报告引用的键 token 数 =",
      len(re.findall(r"`([A-Z][A-Z0-9_]{3,})=", TEXT)),
      "｜键屏有值的 =", len(key))

# 自指那一条也要有证据：报告正文写的断言总条数 == 本脚本实际打出来的断言行数
# （+2 = 下面这一条 in_report，和紧跟着断言"失败条数为 0"的那一条 eq；
#  落盘之后可以用 `grep -c '^PASS' report_audit.log` 从外部复算这个数）
TOTAL = PASS_N + 2
in_report("报告写了本自检的断言条数与零失败",
          f"{TOTAL} 条断言、0 条失败")
eq("本脚本失败条数（报告同一句里声称的）", len(FAILS), 0)

print()
print("REPORT bytes", REPORT.stat().st_size,
      "sha256", hashlib.sha256(REPORT.read_bytes()).hexdigest()[:16])
print("AUDIT FAILS =", len(FAILS))
for one in FAILS:
    print("   -", one)
sys.exit(1 if FAILS else 0)
