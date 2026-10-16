# -*- coding: utf-8 -*-
"""sola_closure01_regression.py — 收口任务 §19 回归测试。

只跑与本轮改动**直接相关**的既有测试，按任务点名的六组分组：
Sola / 人物+装配引擎 / 记忆 / 真人互动 / Provider 契约 / 状态权威。

跑法本身不改任何东西：全部是仓库里既有的自测脚本，各自都用临时目录，
跑完自己回拍正式数据没被碰过（这些脚本自带的口径）。

为什么还要在旁边盯一份 :8090 日志区段：回归组里那几个 proof 脚本会枚举端口、
只读 GET 首页，但不该产生任何推理。「回归这一趟一次模型都没调」必须是自己
量出来的，不是「我以为它们不联网」。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import sola_closure01_lib as L  # noqa: E402

OUT = L.ensure_out_dir()
LLM_LOG = L.REPO / "runtime" / "llm" / "logs" / "llama_8090_gemma4.log"

SUITES: tuple[tuple[str, str, int], ...] = (
    # (组名, 相对路径, 超时秒)
    ("Sola", "runtime/character/tests/sola_v2_ember_proof.py", 600),
    ("Sola", "runtime/character/tests/sola_v2_ember_voice_minimal_proof.py", 600),
    ("人物/装配引擎", "runtime/character/tests/run_all.py", 900),
    ("记忆", "runtime/memory/tests/run_all.py", 900),
    ("真人互动", "runtime/human_interaction/tests/test_human_interaction.py", 900),
    ("Provider 契约", "runtime/tests/test_provider_contract.py", 600),
    ("Provider 契约", "runtime/tests/test_provider_neutrality.py", 600),
    ("状态权威", "runtime/tests/test_tts_switch_transaction.py", 900),
    ("状态权威", "runtime/tests/test_state_single_source.py", 900),
)

# 每个脚本自己收尾打的「几项断言 / 几个键 / PASS / OK」那行，用来把「跑完」和
# 「跑过多少东西」分开报 —— 只看 rc 会把「一条断言都没跑到」也判成过。
SUMMARY_PATTERNS = (
    r"共\s*\d+\s*项断言[^\n]*",
    r"共打出?\s*\d+\s*个键[^\n]*",
    r"^Ran \d+ tests.*$",
    r".*checks: PASS \(\d+\).*$",
    r".*: PASS \(\d+/\d+\).*$",
    r"provider neutrality: .*$",
    r"结果：.*$",
    r".*FAIL \(\d+/\d+ failed[^\n]*",
)
# 断言数为 0 = 尺没跑到东西，必须单独标出来（正向对照失败，不是 PASS）。
# 每个仓库自带的自测脚本收尾话术不一样，所以这里是一条**有序**规则表，
# 命中哪条要一起写进产物（count_rule）——尺子本身得可审计，不能只报一个数。
COUNT_RULES: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("共N项断言", re.compile(r"共\s*(\d+)\s*项断言"), 1),
    ("RanNtests", re.compile(r"Ran\s*(\d+)\s*tests"), 1),
    # 红的那条：分母是它自己报的总项数
    ("FAIL(N_of_M_failed", re.compile(r"FAIL\s*\(\s*(\d+)\s*/\s*(\d+)\s*failed"), 2),
    ("PASS(N_of_M)", re.compile(r"PASS\s*\(\s*(\d+)\s*/\s*(\d+)\s*\)"), 2),
    ("PASS(N)", re.compile(r"PASS\s*\(\s*(\d+)\s*\)"), 1),
    ("共打出N个键", re.compile(r"共打出?\s*(\d+)\s*个键"), 1),
    ("hits_now_vs_baseline",
     re.compile(r"hits now / in baseline\s*:\s*(\d+)\s*/\s*(\d+)"), 1),
)
FAILED_RULES: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("失败N项", re.compile(r"失败\s*(\d+)\s*项"), 1),
    ("红=N条", re.compile(r"红\s*[=＝]\s*(\d+)\s*条"), 1),
    ("FAIL(N_of_M_failed", re.compile(r"FAIL\s*\(\s*(\d+)\s*/\s*(\d+)\s*failed"), 1),
)

# 尺子的正向对照：拿这几条**各脚本真实打过的收尾行**喂给计数规则，
# 每条都必须数出期望值；数不出来就说明尺坏了，而不是脚本坏了。
RULER_CONTROL: tuple[tuple[str, str, int], ...] = (
    ("run_all", "共 277 项断言，失败 0 项，用时 0.9 秒", 277),
    ("unittest", "Ran 82 tests in 0.043s", 82),
    ("provider_contract", "core contract checks: PASS (8)", 8),
    ("tts_switch", "test_tts_switch_transaction: PASS (8/8)", 8),
    ("sola_proof", "共打出 18 个键，用时 0.2 秒，备注 28 条", 18),
    ("voice_minimal_proof", "共打 16 个键；红 = 0 条 → 全绿", 16),
    ("neutrality", "hits now / in baseline  : 41 / 41", 41),
    ("state_single", "test_state_single_source: FAIL (1/19 failed: service-pids-x)", 19),
)


def count_by_rules(text: str) -> tuple[int, str]:
    for name, pat, grp in COUNT_RULES:
        m = pat.search(text)
        if m:
            return int(m.group(grp)), name
    return 0, "NONE"


def failed_by_rules(text: str) -> tuple[int | None, str]:
    for name, pat, grp in FAILED_RULES:
        m = pat.search(text)
        if m:
            return int(m.group(grp)), name
    return None, "NONE"


def check_ruler() -> dict[str, Any]:
    rows = []
    for name, sample, want in RULER_CONTROL:
        got, rule = count_by_rules(sample)
        rows.append({"case": name, "want": want, "got": got, "rule": rule,
                     "ok": got == want})
    return {"all_ok": all(one["ok"] for one in rows), "cases": rows}


def summarize(text: str) -> dict[str, Any]:
    lines = [one.strip() for one in text.splitlines() if one.strip()]
    hits: list[str] = []
    for pat in SUMMARY_PATTERNS:
        for one in re.finditer(pat, text, re.M):
            got = one.group(0).strip()
            if got and got not in hits:
                hits.append(got)
    n, rule = count_by_rules(text)
    fm, fr = failed_by_rules(text)
    return {"summary_lines": hits[-6:], "assertions_or_cases": n,
            "count_rule_used": rule,
            "failed_assertions_reported": fm, "failed_rule_used": fr,
            "tail": lines[-6:]}


def run_suite(rel: str, timeout: int) -> dict[str, Any]:
    path = L.REPO / rel
    if not path.exists():
        return {"suite": rel, "missing": True, "result": "NOT_EVALUABLE"}
    started = datetime.now()
    try:
        proc = subprocess.run([sys.executable, str(path)], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, cwd=str(L.REPO))
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        rc = proc.returncode
        crashed = False
    except subprocess.TimeoutExpired as exc:
        out = ((exc.stdout or b"").decode("utf-8", "replace") if isinstance(
            exc.stdout, bytes) else str(exc.stdout or "")) + "\nTIMEOUT"
        rc = 124
        crashed = True
    except Exception as exc:                       # 启动器本身失败 ≠ 测试失败
        out = f"LAUNCH_ERROR {type(exc).__name__}: {exc}"
        rc = -1
        crashed = True
    row: dict[str, Any] = {"suite": rel, "rc": rc, "elapsed_s": round(
        (datetime.now() - started).total_seconds(), 1),
        "launcher_error": crashed}
    row.update(summarize(out))
    # 「红」的行单独捞出来，交人工判 A/B/C
    row["red_lines"] = [one for one in out.splitlines()
                        if re.match(r"\s*(FAIL|ERROR|失败|✗|\[红\])", one)][:12]
    reported_fail = row["failed_assertions_reported"]
    if rc != 0 or crashed:
        row["result"] = "FAIL"
    elif row["assertions_or_cases"] <= 0:
        row["result"] = "NOT_EVALUABLE"
    elif reported_fail:
        row["result"] = "FAIL"
    else:
        row["result"] = "PASS"
    row["output_tail_full_saved"] = L.short(out, 4000)
    return row


def main() -> int:
    def log_size() -> int:
        try:
            return LLM_LOG.stat().st_size
        except OSError:
            return -1

    off = log_size()
    ruler = check_ruler()
    rows = []
    for group, rel, to in SUITES:
        one = run_suite(rel, to)
        one["group"] = group
        rows.append(one)
    new = log_size() - off
    region = ""
    if off >= 0 and LLM_LOG.exists():
        with open(LLM_LOG, "rb") as fh:
            fh.seek(off)
            region = fh.read(400_000).decode("utf-8", "replace")
    gen_lines = [one for one in region.splitlines() if "processing task" in one]

    # 尺子自己没过正向对照 ⇒ 这批数一个都不作数（宁可 NOT_EVALUABLE，不发假 PASS）
    if not ruler["all_ok"]:
        for one in rows:
            one["result"] = "NOT_EVALUABLE"
    green = sum(1 for one in rows if one["result"] == "PASS")
    red = [one for one in rows if one["result"] != "PASS"]

    payload: dict[str, Any] = {
        "task_id": L.TASK_ID,
        "stages": ["§19 与本轮改动直接相关的既有回归"],
        "collected_at_local": L.now_local(),
        "scope": "只跑任务点名的六组；未扩到整台机器（§19 最后一条）",
        "interpreter": sys.executable,
        "counting_ruler_control": ruler,
        "suites": rows,
        "GREEN": green, "RED": len(red),
        "RED_SUITES": [one["suite"] for one in red],
        "regression_run_made_llm_calls": len(gen_lines),
        "llama_log_bytes_new_during_regression": new,
        "triage": TRIAGE,
    }
    path = L.write_json("regression_summary.json", payload)
    for one in rows:
        print(f"  {one['suite']:60s} {one['result']:14s} rc={one['rc']} "
              f"n={one['assertions_or_cases']} {one.get('red_lines')[:1]}")
    print("GREEN =", green, "| RED =", len(red),
          "| 回归期间模型调用 =", len(gen_lines),
          "| 尺子正向对照 =", "OK" if ruler["all_ok"] else [one for one in
                                                        ruler["cases"] if not one["ok"]],
          "->", path)
    return 0


# 逐条归属：红了不猜，先用「本轮之前就已经是这样」的现场证据分类，
# 再按 §19 的 A/B/C 落话。A 才动接线，B/C 只登记。
# 下面这份是**本轮实际跑出来的那一条**红的归属；新出现的红由报告另行登记。
TRIAGE: list[dict[str, Any]] = [
    {
        "suite": "runtime/tests/test_state_single_source.py",
        "case": "service-pids-port-agreement — llm: ownership pid 17468 vs :8090 owners [18748]",
        "class": "B（已知旧状态问题，非本轮回归）",
        "证据": {
            "本轮基线（04:35:15）:8090 状态": "listening=false，listeners=[]，"
            "而 service_pids.json 的 llmOwnership 当时已写着 pid=17468"
            "（startTimeUtc=2026-09-27T07:22:00Z）",
            "本轮 :8090 实际起来的时候": "pid=18748，created=2026-09-28 04:36:26"
            "（§4 服务恢复，任务明确允许「启动 8090」）",
            "归属": "登记件在**本轮开始之前**就已经指向一个死进程；这条断言只在"
                    ":8090 有监听者时才有机会开口，所以它此前一直沉默。本轮把 :8090 "
                    "听上，只是让这个既成的陈旧读数第一次被看见，不是把它写坏的。",
        },
        "为什么不顺手修": (
            "service_pids.json 的 llmOwnership 由正式启动/接管路径独占书写"
            "（§1「一个状态只能有一个权威 owner」）。从验收脚本里手改它 = 第二个模块"
            "「顺便管」这个状态；而为了这条测试去重启一个健康的 :8090，正是 §1 禁止的"
            "「为了让测试变绿而动生产」。§2 允许的是「清理本项目遗留失效 PID/state」，"
            "指的是走官方入口让它自己重写，不是验收侧代写。"),
        "owner 侧一行处置": "要么走一次正式启动脚本的 8090 接管分支，让官方写者把 "
        "llmOwnership 刷成当前 pid；要么按官方停服路径停掉 :8090 再看这条是否回归沉默。",
        "与本任务目标的关系": "与 Sola 无关：Sola 不写 PID 登记、不认领进程所有权"
        "（sola_v2_ember_proof.py 的 PROOF_RAN_READ_ONLY=YES 与本回收器的 "
        "regression_run_made_llm_calls 两道都在钉这件事）。",
    },
]


if __name__ == "__main__":
    sys.exit(main())
