# -*- coding: utf-8 -*-
"""§6.11.2 追加（2026-09-26 用户终审）+ 同一轮的两块机器镜像键写进 MASTER_STATE.json。

写入内容：
  1) MD §6.11.2 —— 终版冻结键（含 SAFETY_GUARD_ROLE / IS_MODEL_MAXIMUM / 两条 OPEN_NON_BLOCKING）
     + 快照分类门禁（ARTIFACT_CLASS / EXPECTED_RESULT / PRODUCTION_GATE）。
  2) JSON —— 在 long_audio_truncation_closure_20260926 这个 member 末尾追加 3 个键。

断言：MD 仍是纯追加（旧行逐行同序号全等、零删除）；JSON 除插入点外两侧逐行全等、
除本 member 外全部键深相等、member 顶层键序是旧的超序延伸、CRLF 计数只按新增行数增长。
**不启动、不重启、不连接任何服务。** 幂等：已含 §6.11.2 / 已含新键时打印 SKIP。
"""
import json
import shutil
from pathlib import Path

LLM = Path(str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'runtime/llm'))
MD = LLM / 'AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_HANDOFF.md'
JS = LLM / 'AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_STATE.json'
KEY = 'long_audio_truncation_closure_20260926'
SUF = '.bak_longaudio0926c'

SEC = """
### §6.11.2 追加（2026-09-26 10:3x）— 用户终审：LONG_AUDIO_TRUNCATION 接受为磁盘事实基线

**终版冻结键（逐字，可直接 grep；细节判据在结案报告 §9）**：

```text
ROOT_CAUSE = ROUTER_30S_SILENT_TAIL_DROP
ROOT_CAUSE = CLOSED
LONG_AUDIO_PLAYBACK = PASS
TAIL_AUDIO_PRESERVED = PASS
PRODUCTION_LONG_AUDIO_ACCEPTANCE = PASS
LONG_AUDIO_TRUNCATION = CLOSED
SAFETY_GUARD_DURATION = 3600s
SAFETY_GUARD_ROLE = RUNAWAY_PROTECTION
SAFETY_GUARD_IS_MODEL_MAXIMUM = NO
ARCH_DEBT_AUDIO_GUARD_DERIVATION = OPEN_NON_BLOCKING
ARCH_DEBT_AUDIO_GUARD_COUNTER_SEMANTICS = OPEN_NON_BLOCKING
```

与 §6.11 的 12 键块**不冲突**：那 12 键是逐闸门明细，本节是终审汇总并新增 4 个键
（`SAFETY_GUARD_ROLE` / `SAFETY_GUARD_IS_MODEL_MAXIMUM` = §6.11 里 §3.2 中文口径的机器形式）。
两条架构债的**终值词表是 `OPEN_NON_BLOCKING`**（§6.11 里写的 `OPEN（非阻断）` 同义，grep 以此为准）。
本轮接受意见同时确认：运行逻辑没有因"修措辞"再变；100.07% 已降回"测量比值"；3600s 已改为失控保护栏。

**快照分类门禁（永久，防"看到红就回滚正确代码"）**：任何全测试扫描**不得**把备份快照
纳入当前生产验收集合。`.bak_longaudio02_precomment` 上长回合测试 exit 1 是**有意结果**：

```text
ARTIFACT_CLASS = HISTORICAL_PRECOMMENT_SNAPSHOT
EXPECTED_RESULT = FAIL_OLD_WORDING_ASSERTIONS
PRODUCTION_GATE = NO
```

同族第二件：`ARTIFACT_CLASS = HISTORICAL_PREFIX_CONTROL` /
`EXPECTED_RESULT = PASS_REPRODUCING_30S_TRUNCATION` / `PRODUCTION_GATE = NO`
（`.bak_longaudio01`，故意跑修复前旧件证明测试不空转）。
这条已**落进测试输出**而不只是文档：`test_audio_router_long_turn.cjs` 现在首行打印
`artifact class: … PRODUCTION_GATE=YES|NO`，VERDICT 把"口径/文案类红"与"行为类红"分开计，
`FIX_INCOMPLETE` 一词只在**行为类真红**时才出现（旧措辞快照现在打
`OLD_WORDING_ONLY（红的全是口径/文案断言，截断行为零失败）`，退出码仍是 1，红不会被吞）。
规则：**长回合测试只允许无参运行计入生产验收；带 `.bak` 参数的运行一律记为对照/文案门禁。**
顺带记事实：现有扫描器 `runtime/tests/probes/run_offline_suites_01.ps1` 的 node 段是显式白名单
（3 个套件），既不 glob `*.bak*` 也不含长回合测试 ⇒ 今天的红只可能来自人工指错文件。

**收口边界（用户重申，永久）**：长音频问题至此彻底结束。不要再碰 8090 / 8765 / TTS / 8010 /
AudioRouter 正常路径；`guard_truncated_turns` 保持"越界 delta 条数"现状，不改名、不改计数语义。
本轮只动了两件验证资产（`test_audio_router_long_turn.cjs` 分类打印 + 本文件与配对 JSON 回写），
`audio-router.js` 逐字未动（仍 `a9038df0910789f1…` / 30,062 B），**未启动或重启任何服务**。
"""

# ---- 要插进 JSON member 末尾的 3 个键（4 空格缩进，与该 member 内其它键同级）----
INS = [
    '    "final_freeze_user_acceptance_20260926": {',
    '      "accepted_as": "DISK_FACT_BASELINE_LONG_AUDIO_TRUNCATION_CLOSED",',
    '      "keys": {',
    '        "ROOT_CAUSE": "CLOSED",',
    '        "LONG_AUDIO_PLAYBACK": "PASS",',
    '        "TAIL_AUDIO_PRESERVED": "PASS",',
    '        "PRODUCTION_LONG_AUDIO_ACCEPTANCE": "PASS",',
    '        "LONG_AUDIO_TRUNCATION": "CLOSED",',
    '        "SAFETY_GUARD_DURATION": "3600s",',
    '        "SAFETY_GUARD_ROLE": "RUNAWAY_PROTECTION",',
    '        "SAFETY_GUARD_IS_MODEL_MAXIMUM": "NO",',
    '        "ARCH_DEBT_AUDIO_GUARD_DERIVATION": "OPEN_NON_BLOCKING",',
    '        "ARCH_DEBT_AUDIO_GUARD_COUNTER_SEMANTICS": "OPEN_NON_BLOCKING"',
    '      },',
    '      "root_cause_first_line": "ROOT_CAUSE = ROUTER_30S_SILENT_TAIL_DROP",',
    '      "do_not_touch_again": ["8090", "8765", "TTS", "8010", "AudioRouter normal path"],',
    '      "authoritative_section": "FINAL_REPORT_AI_GIRLFRIEND_LONG_AUDIO_TRUNCATION_02.md §9"',
    '    },',
    '    "test_artifact_classes": {',
    '      "HISTORICAL_PRECOMMENT_SNAPSHOT": {',
    '        "file": "runtime/ui/app/audio-router.js.bak_longaudio02_precomment",',
    '        "expected_result": "FAIL_OLD_WORDING_ASSERTIONS",',
    '        "production_gate": "NO",',
    '        "meaning": "exit 1 是有意结果（旧口径断言）；禁止据此判验收件坏了或回滚 audio-router.js"',
    '      },',
    '      "HISTORICAL_PREFIX_CONTROL": {',
    '        "file": "runtime/ui/app/audio-router.js.bak_longaudio01",',
    '        "expected_result": "PASS_REPRODUCING_30S_TRUNCATION",',
    '        "production_gate": "NO",',
    '        "meaning": "正向对照：故意跑修复前旧件，红了才说明修复丢了"',
    '      },',
    '      "PRODUCTION": {',
    '        "file": "runtime/ui/app/audio-router.js",',
    '        "expected_result": "ALL_PASS",',
    '        "production_gate": "YES",',
    '        "how": "test_audio_router_long_turn.cjs 无参运行",',
    '        "measured": "13 PASS / 0 FAIL, exit 0"',
    '      },',
    '      "sweep_rule": "长回合测试只允许无参运行计入生产验收；带 .bak 参数的运行一律记为对照/文案门禁，不参与 Gate",',
    '      "existing_sweep_is_allowlist": "runtime/tests/probes/run_offline_suites_01.ps1 的 node 段是显式白名单，不 glob .bak、不含长回合测试"',
    '    },',
    '    "long_turn_test_final_sha256_prefix": "544040568b9268a0",',
    '    "router_untouched_this_round_sha256_prefix": "a9038df0910789f1"',
]
NEW_KEYS = ['final_freeze_user_acceptance_20260926', 'test_artifact_classes',
            'long_turn_test_final_sha256_prefix', 'router_untouched_this_round_sha256_prefix']


def lines_norm(t):
    return t.replace('\r\n', '\n').replace('\r', '\n').split('\n')


def main():
    rc = 0

    # ---------------- MD 纯追加 ----------------
    old = MD.read_text(encoding='utf-8')
    if '§6.11.2' in old:
        print('MD: §6.11.2 already present -> SKIP')
    else:
        assert '### §6.11.1 追加' in old, 'MD: §6.11.1 missing（先跑 fa_append_master_611_1_0926.py）'
        sep = '\n' if old.endswith('\n') else '\n\n'
        shutil.copy2(MD, MD.parent / (MD.name + SUF))
        MD.write_text(old + sep + SEC.lstrip('\n') + '\n', encoding='utf-8', newline='\n')
        ol, nl = lines_norm(old), lines_norm(MD.read_text(encoding='utf-8'))
        ok = nl[:len(ol)] == ol
        print('MD: §6.11.2 appended  lines(wc -l 口径) %d -> %d  prefix_identical=%s  bytes=%d'
              % (len(ol) - 1, len(nl) - 1, ok, MD.stat().st_size))
        rc |= 0 if ok else 1

    # ---------------- JSON：member 末尾追加 4 个键 ----------------
    jraw = JS.read_text(encoding='utf-8')
    jold = json.loads(jraw)
    assert KEY in jold, 'JSON: %s missing（先跑 fa_append_master_611_0926.py）' % KEY
    lines = lines_norm(jraw)
    if set(NEW_KEYS) <= set(jold[KEY]):
        print('JSON: 终审镜像键已在 -> SKIP（幂等复跑：不检查收尾锚点、不动文件）')
    else:
        # 锚点 = 文档收尾三连：'    ]' / '  }' / '}'（本 member 是最后追加的那个，位置确定）
        assert lines[-1] == '}' and lines[-2] == '  }' and lines[-3] == '    ]', \
            'JSON: 收尾锚点不再是 ["]", "}", "}"]，插入法需人工重看：%r' % (lines[-3:],)
        i = len(lines) - 3                                    # '    ]' 这一行
        assert 'out_of_scope_confirmed' in '\n'.join(lines[max(0, i - 20):i]), \
            'JSON: 锚点前 20 行内找不到 out_of_scope_confirmed，插入位置存疑'
        assert not lines[i].rstrip().endswith(','), 'JSON: 锚点已带逗号，非末键，插入法需重看'
        new = lines[:i] + [lines[i] + ','] + INS + lines[i + 1:]
        txt = '\r\n'.join(new) + ('\r\n' if jraw.endswith('\r\n') else '')
        jnew = json.loads(txt)                                # 语法必须仍成立
        diff = [k for k in jold if jnew[k] != jold[k]]
        assert diff == [KEY], 'JSON: 其它顶层键被改动 %s' % diff
        assert list(jnew[KEY].keys())[:len(jold[KEY])] == list(jold[KEY].keys()), \
            'JSON: member 原有键序被改动'
        assert set(jnew[KEY]) - set(jold[KEY]) == set(NEW_KEYS), 'JSON: 新键集合不符'
        assert jnew[KEY]['test_artifact_classes']['HISTORICAL_PRECOMMENT_SNAPSHOT']['production_gate'] == 'NO'
        assert jnew[KEY]['final_freeze_user_acceptance_20260926']['keys']['SAFETY_GUARD_IS_MODEL_MAXIMUM'] == 'NO'
        assert jnew[KEY]['arch_debt'] == jold[KEY]['arch_debt'], 'JSON: arch_debt 被动了'
        assert jnew[KEY]['gates'] == jold[KEY]['gates'], 'JSON: gates 被动了'
        shutil.copy2(JS, JS.parent / (JS.name + SUF))         # 先备份再写
        JS.write_text(txt, encoding='utf-8', newline='')
        ol, nl = lines, lines_norm(JS.read_text(encoding='utf-8'))
        same = ol[:i] == nl[:i] and ol[i + 1:] == nl[i + 1 + len(INS):]
        b = JS.read_bytes()
        old_lf = jraw.replace('\r\n', '\n').count('\n')      # 旧文件行尾计数（本文件末行无行尾符）
        print('JSON: member 末尾追加 %d 行（锚点 "]"+","）；两侧上下文逐行全等=%s；'
              'CRLF %d -> %d（应 +%d）；末行仍无行尾符=%s；除 %s 外全部键深相等；member 原键序未变'
              % (len(INS), same, jraw.replace('\r\n', '\n').count('\n'), b.count(b'\r\n'),
                 len(INS), not b.endswith(b'\r\n'), KEY))
        rc |= 0 if (same and b.count(b'\r\n') == old_lf + len(INS)) else 1
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
