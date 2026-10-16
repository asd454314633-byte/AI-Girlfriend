# -*- coding: utf-8 -*-
"""AI_GIRLFRIEND 主交接回写：§6.11 纯追加 + MASTER_STATE.json 追加镜像键。

硬断言（沿用主交接 §6.10.8 的口径）：
  * 「纯追加 / 零删除」= 换行归一化后，旧内容逐行同序号全等；
  * 字节前缀与行尾计数只作为信息打印，不作硬断言（编辑工具可能整文件规范化行尾）。
本脚本不启动、不重启、不连接任何服务。
"""
import hashlib
import json
import shutil
import sys
from pathlib import Path

LLM = Path(str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'runtime/llm'))
MD = LLM / 'AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_HANDOFF.md'
JS = LLM / 'AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_STATE.json'
SUF = '.bak_longaudio0926'

GATES12 = """ROOT_CAUSE = ROUTER_30S_SILENT_TAIL_DROP
ROOT_CAUSE = CLOSED
30S_CLAMP_REMOVED = PASS
PRODUCTION_AUDIO_OVER_60S = PASS
BROWSER_TO_8010_AUDIO_COVERAGE = PASS
TAIL_AUDIO_PRESERVED = PASS
REAL_AUDIO_PLAYBACK = PASS
SHORT_AUDIO_REGRESSION = PASS
NO_EXISTING_FEATURE_REGRESSION = PASS
LONG_AUDIO_PLAYBACK = PASS
PRODUCTION_LONG_AUDIO_ACCEPTANCE = PASS
LONG_AUDIO_TRUNCATION = CLOSED"""

SECTION = """
## §6.11 追加（2026-09-26 09:4x）— LONG_AUDIO_TRUNCATION 结案：根因 CLOSED，12 键冻结

**一句话**：「文字完整但声音提前结束」的第一丢失层 = 浏览器路由 `audio-router.js` 的 30 s 硬钳位
（`MAX_TURN_BYTES = 24000 * 2 * 30`）**静默**丢弃超限尾音。已修复并经真实生产长回复验收，
`LONG_AUDIO_TRUNCATION = CLOSED`。**本缺陷已结案，禁止重开调查。**

**权威文本是指针，不是本节**（易变数值一律去结案报告 §2 读；那是 2026-09-26 08:2x 的**单轮快照**，
不是现在的服务态）：

```text
runtime/llm/FINAL_REPORT_AI_GIRLFRIEND_LONG_AUDIO_TRUNCATION_02.md  <-- §1 冻结块 / §2 闸门表 / §7 诚实账
runtime/tests/fa_longaudio_final.py                                  <-- 真实生产终验驱动（CDP 独立页，自关并等 pool idle）
runtime/tests/fa_longaudio_final_out.json                            <-- 第二次尝试的字节级对账快照（尾音判据已被取代，见下）
runtime/tests/fa_short_regression_out.json                           <-- 短回复回归回执
runtime/ui/app/test_audio_router_long_turn.cjs                       <-- 三档(55/180/540s)确定性复现 + 尾块存活断言
runtime/ui/app/audio-router.js                                       <-- 唯一被改的生产文件（旧件 .bak_longaudio01；验收快照 .bak_longaudio02_precomment）
```

**冻结裁决（键名可 grep，与结案报告 §1 逐字一致）**：

```text
GATES12_PLACEHOLDER
```

**两条口径收紧（2026-09-26 用户裁决，永久适用）**

1. `8765_TO_BROWSER_AUDIO_COVERAGE` 只许写成 `PASS` + `MEASURED_RATIO ≈ 100.07%`
   + `difference attributable to duration/log rounding`，**不许写成数学意义上的 100%**。
   分子分母不同源：分母 101.85 s 是 8765 六条 `generated X.XXs audio` 日志求和（2 位小数最多解释 0.03 s），
   分子 101.92 s 由浏览器实收 4,892,160 B 推出；**不存在多出来的音频**——8010 落库 2,446,080 样本 × 2 B
   与实收字节逐字节相等，WAV 头恰好 44 B。
2. `SAFETY_GUARD_DURATION = 3600s` 的角色是 **runaway safety guard**，**不是"系统合法最大输出时长"**。
   `-c 8192` 是上下文窗口（实际可用于输出的 token 数还取决于输入上下文占用），
   `2 chars/token`、`4.5 chars/s` 均为估算。旧写法 `NORMAL_MAX_EXPECTED_AUDIO_DURATION …（系统合法上限）` **作废**。
   `ARCH_DEBT_AUDIO_GUARD_DERIVATION = OPEN`（非阻断）：将来支持超长生成时改为从实际 output budget 动态派生；
   在此之前**不要为了把这个数字算得更漂亮而打开 8090 / 8765 / TTS**。

**后续分诊硬规则（DO_NOT_DO）**：再出现"文字完整但声音提前结束"，先按固定顺序只读查
`__audioRouterDiagnostics().last[].state == 'AUDIO_TRUNCATED'` → `counters.guard_truncated_turns > 0`
→ `turn_truncated_at_guard` 事件；三者全否再查 `turn.bytes` vs `playedBytes` 与 sink 侧实收字节。
**禁止**从 LLM / TTS / 8010 / Session 生命周期重新全链路猜。

**本轮登记的三个陷阱（会被复用）**

- `fa_longaudio_final_out.json` 里 `TAIL_AUDIO_ACTUALLY_PLAYED=false` 与 `ok=false` 是**旧判据**产物
  （用 loopback `active_span`=132.59 s 对齐音频时长，把收口前的静默与预热尾音算了进去），
  判据已改为 `last_active - router_done_rel`。**不许据此判"尾音没播到"**。同文件 `ttfa_s=-2618.8` 是坏指标。
- 测试的版本哨兵**不许写死"当前那版的数字"**：`test_audio_router_long_turn.cjs` 曾用
  `/MAX_TURN_BYTES = 24000 \\* 2 \\* 600/` 判版本，guard 换成派生式后哨兵失配，
  对**已修复**的 router 打印 `PRE-FIX` + 3×FAIL —— 假判决的方向恰好与结案结论相反。
  现改为解析 guard 实际秒数（>30s 判 FIXED；解析失败打印 `UNKNOWN（哨兵失配）`而不是默认结论）。
- 正向对照常驻，红了就说明修复丢了（比"新件 PASS"更有信息量）：
  `node runtime/ui/app/test_audio_router_long_turn.cjs runtime/ui/app/audio-router.js.bak_longaudio01`
  必须仍复现 `avatarGot=29.95s / tailAudio7F39=LOST`。

**本轮范围**：只改第一丢失层。未动 Qwen / Fish / 8765 / 8010 / `websocket_router.py` /
`s2s-realtime-client.js` / `avatar-sync.js` / `main.js` / Session 生命周期 / LLM / Persona / Memory；
本轮**未启动或重启任何服务**，全部验证 = 只读日志对账 + 本地 node 测试
（`test_audio_router.cjs` 16/16、长回合三档 3/3）。
"""

MEMBER = {
    "long_audio_truncation_closure_20260926": {
        "handoff_section": "6.11",
        "authoritative_doc": "runtime/llm/FINAL_REPORT_AI_GIRLFRIEND_LONG_AUDIO_TRUNCATION_02.md",
        "frozen_at": "2026-09-26T09:4x+08:00 (single production round, 08:2x snapshot)",
        "state_is": "point_in_time_snapshot_not_live",
        "root_cause": "ROUTER_30S_SILENT_TAIL_DROP",
        "root_cause_status": "CLOSED",
        "fix_layer": "runtime/ui/app/audio-router.js (first loss layer only)",
        "gates": {
            "30S_CLAMP_REMOVED": "PASS",
            "PRODUCTION_AUDIO_OVER_60S": "PASS",
            "8765_TO_BROWSER_AUDIO_COVERAGE": "PASS",
            "8765_TO_BROWSER_MEASURED_RATIO": 1.0007,
            "BROWSER_TO_8010_AUDIO_COVERAGE": "PASS",
            "TAIL_AUDIO_PRESERVED": "PASS",
            "REAL_AUDIO_PLAYBACK": "PASS",
            "TAIL_AUDIO_USER_CONFIRMED": True,
            "SHORT_AUDIO_REGRESSION": "PASS",
            "NO_EXISTING_FEATURE_REGRESSION": "PASS",
            "LONG_AUDIO_PLAYBACK": "PASS",
            "PRODUCTION_LONG_AUDIO_ACCEPTANCE": "PASS",
            "LONG_AUDIO_TRUNCATION": "CLOSED",
        },
        "measured": {
            "tts_gen_s_logged_sum_8765": 101.85,
            "browser_received_bytes": 4892160,
            "browser_implied_duration_s": 101.92,
            "avatar8010_samples": 2446080,
            "wav_post_bytes_incl_44B_header": 4892204,
            "short_reply_bytes": 221184,
            "old_clamp_avatar_seconds": 29.95,
        },
        "wording_rules": {
            "8765_TO_BROWSER_AUDIO_COVERAGE": "PASS + MEASURED_RATIO ~= 100.07% + difference attributable to duration/log rounding; never state mathematical 100%",
            "SAFETY_GUARD_DURATION": "3600s; ROLE = runaway safety guard; NOT the model's exact maximum legal audio duration",
            "superseded_phrases": [
                "NORMAL_MAX_EXPECTED_AUDIO_DURATION = ... (系统合法上限)",
                "SAFETY_GUARD_DURATION = 3600s (= 系统合法上限)",
            ],
        },
        "arch_debt": {
            "ARCH_DEBT_AUDIO_GUARD_DERIVATION": "OPEN_NON_BLOCKING",
            "derive_guard_from": "actual output budget (remaining context / max_tokens / measured TTS rate)",
            "trigger": "a legitimate long reply actually hits the guard",
            "forbidden": "reopening 8090/8765/TTS just to make 3600 look prettier",
        },
        "triage_rule": [
            "state == 'AUDIO_TRUNCATED'?",
            "counters.guard_truncated_turns > 0?",
            "'turn_truncated_at_guard' event?",
            "all no -> compare turn.bytes vs playedBytes and sink-side received bytes; do NOT re-guess LLM/TTS/8010/Session",
        ],
        "stale_receipts_do_not_trust": {
            "runtime/tests/fa_longaudio_final_out.json:TAIL_AUDIO_ACTUALLY_PLAYED/ok": "old span-based predicate (attempt 2); superseded",
            "runtime/tests/fa_longaudio_final_out.json:ttfa_s": "bad metric (-2618.8, mixed performance.now epochs); unused",
        },
        "positive_control_command": "node runtime/ui/app/test_audio_router_long_turn.cjs runtime/ui/app/audio-router.js.bak_longaudio01  # must still show 29.95s / tail LOST",
        "services_touched_this_round": [],
        "out_of_scope_confirmed": [
            "Qwen/Fish/Higgs TTS handler", "8765", "8010 server", "websocket_router.py",
            "s2s-realtime-client.js", "avatar-sync.js", "main.js", "Session lifecycle",
            "LLM", "Persona", "Memory",
        ],
    }
}


def lines_norm(t):
    return t.replace('\r\n', '\n').replace('\r', '\n').split('\n')


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    rc = 0

    # ---------------- MD: pure append ----------------
    old_raw = MD.read_text(encoding='utf-8')
    if '## §6.11' in old_raw:
        print('MD: §6.11 already present -> SKIP')
    else:
        assert '## §6.10' in old_raw, 'MD: §6.10 missing; wrong file?'
        body = SECTION.replace('GATES12_PLACEHOLDER', GATES12)
        sep = '' if old_raw.endswith('\n\n') else ('\n' if old_raw.endswith('\n') else '\n\n')
        new_raw = old_raw + sep + body.lstrip('\n') + ('\n' if new_raw_needs_nl(old_raw) else '')
        shutil.copy2(MD, MD.parent / (MD.name + SUF))
        MD.write_text(new_raw, encoding='utf-8', newline='\n')
        ol, nl = lines_norm(old_raw), lines_norm(MD.read_text(encoding='utf-8'))
        prefix_ok = nl[:len(ol)] == ol
        print('MD  append: removed_lines=%d  lines %d -> %d  prefix_identical=%s  CRLF_in_new_tail=%d'
              % (0 if prefix_ok else -1, len(ol), len(nl), prefix_ok,
                 sum(1 for l in MD.read_bytes().split(b'\n') if l.endswith(b'\r'))))
        if not prefix_ok:
            rc = 1

    # ---------------- JSON: append one member ----------------
    jraw = JS.read_text(encoding='utf-8')
    jold = json.loads(jraw)
    KEY = next(iter(MEMBER))
    if KEY in jold:
        print('JSON: %s already present -> SKIP' % KEY)
    else:
        raw_bytes = JS.read_bytes()
        eol = '\r\n' if raw_bytes.count(b'\r\n') else '\n'   # 本 JSON 实测为 CRLF，保持原样
        tail_nl = jraw.endswith('\n')
        head = jraw.rstrip()
        assert head.endswith('}'), 'JSON: unexpected tail'
        cut = head.rfind('\n}')
        assert cut > 0, 'JSON: cannot locate closing brace line'
        assert json.loads(head[:cut] + '\n}') == jold, 'JSON: splice point wrong'
        val = json.dumps(MEMBER[KEY], indent=2, ensure_ascii=False).split('\n')
        mem_line = ('  ' + json.dumps(KEY, ensure_ascii=False) + ': '
                    + '\n'.join([val[0]] + ['  ' + ln for ln in val[1:]]))
        new = head[:cut] + ',\n' + mem_line + '\n}' + ('\n' if tail_nl else '')
        jnew = json.loads(new)
        assert list(jnew.keys()) == list(jold.keys()) + [KEY], 'JSON: key order/append violated'
        for k in jold:
            assert jnew[k] == jold[k], 'JSON: existing key %s changed' % k
        shutil.copy2(JS, JS.parent / (JS.name + SUF))
        JS.write_text(new.replace('\n', eol) if eol == '\r\n' else new,
                      encoding='utf-8', newline='')
        chk = JS.read_bytes()
        ol = jraw.replace('\r\n', '\n').split('\n')
        nl = chk.decode('utf-8').replace('\r\n', '\n').split('\n')
        print('JSON: keys %d -> %d | existing keys deep-equal=True | CRLF kept=%d/%d | removed_lines=%d'
              % (len(jold), len(jnew), chk.count(b'\r\n'), chk.count(b'\n'),
                 0 if nl[:len(ol)] == ol else -1))
        if nl[:len(ol)] != ol:
            rc = 1
    return rc


def new_raw_needs_nl(old_raw):
    return True


if __name__ == '__main__':
    raise SystemExit(main())
