# -*- coding: utf-8 -*-
"""§6.11.1 追加 + 同轮第二条架构债镜像进 MASTER_STATE.json（外科手术式插入）。

断言：MD 仍是纯追加；JSON 除新插入的 3 行外逐行同序号全等；两份文件 reparse/rewrap 一致。
不启动、不重启、不连接任何服务。
"""
import json
import shutil
from pathlib import Path

LLM = Path(str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'runtime/llm'))
MD = LLM / 'AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_HANDOFF.md'
JS = LLM / 'AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_STATE.json'
KEY = 'long_audio_truncation_closure_20260926'
SUF = '.bak_longaudio0926b'

SEC = """
### §6.11.1 追加（2026-09-26 09:5x）— 同轮登记的第三条与一处文案漂移

1. **作废口径的清 sweep 不止注释**：`audio-router.js` 里 guard 命中时那条 `console.error`
   原文自称"单轮音频超过**系统合法上限**哨值"——那是将来某天真的命中 guard 时，
   人在控制台/日志里读到的第一句（也是唯一一句）话，所以一并改成
   `单轮音频超过失控护栏 SAFETY_GUARD_DURATION（<N>s，非模型合法最大时长），疑似上游失控`。
   本轮对 `audio-router.js` 的 `diff` = 3 个 hunk，**全部落在注释与日志文本**，
   常量 / 控制流 / 状态机逐字未动（`node --check` OK；既有套件 16/16、长回合三档 3/3、
   guard 探针 10/10）。改后 SHA `a9038df0910789f1…`，验收快照留在
   `audio-router.js.bak_longaudio02_precomment`（`47bdcaa848d4887…`）。缓存串仍 `?v=longaudio-02`，不必 bump。
2. **第二条非阻断债**：`ARCH_DEBT_AUDIO_GUARD_COUNTER_SEMANTICS = OPEN（非阻断）`
   —— `guard_truncated_turns` 数的是**越界 delta 条数**，不是轮数（同一轮命中后每条后续 delta
   都会再 `+1` 并各打一条 `console.error`；本地探针实测 1 命中 + 5 后续 ⇒ 计数 6、日志 6）。
   生产至今计数为 0，不影响任何已验收结论；但**不许把这个数读成"有几轮被截"**。
   行为已由 `test_audio_router_long_turn.cjs` 新增的 guard 探针段按现状锁定。
3. **一处"红了才对"的测试漂移**：`test_audio_router_long_turn.cjs` 现在对**验收快照**
   `audio-router.js.bak_longaudio02_precomment` 报 exit 1，红的恰好是 4 条"作废口径"断言。
   这是有意的文案门禁，**不要**据此判"验收件坏了"或回滚 `audio-router.js`。
"""


def lines_norm(t):
    return t.replace('\r\n', '\n').replace('\r', '\n').split('\n')


def main():
    rc = 0

    # ---- MD 纯追加 ----
    old = MD.read_text(encoding='utf-8')
    if '§6.11.1' in old:
        print('MD: §6.11.1 already present -> SKIP')
    else:
        assert '## §6.11 追加' in old, 'MD: §6.11 missing'
        sep = '\n' if old.endswith('\n') else '\n\n'
        shutil.copy2(MD, MD.parent / (MD.name + SUF))
        MD.write_text(old + sep + SEC.lstrip('\n') + '\n', encoding='utf-8', newline='\n')
        ol, nl = lines_norm(old), lines_norm(MD.read_text(encoding='utf-8'))
        ok = nl[:len(ol)] == ol
        print('MD: §6.11.1 appended  lines %d -> %d  prefix_identical=%s' % (len(ol), len(nl), ok))
        rc |= 0 if ok else 1

    # ---- JSON: 在我本轮那个 member 的 arch_debt 里插 3 行 ----
    jraw = JS.read_text(encoding='utf-8')
    jold = json.loads(jraw)
    assert KEY in jold, 'JSON: %s missing (run fa_append_master_611_0926.py first)' % KEY
    lines = jraw.replace('\r\n', '\n').split('\n')
    hits = [i for i, l in enumerate(lines) if 'reopening 8090/8765/TTS just to make 3600' in l]
    if len(hits) != 1:
        print('JSON: 锚点命中 %d 次（需恰好 1 次）-> 不动文件，请人工看' % len(hits))
        return 1
    if 'ARCH_DEBT_AUDIO_GUARD_COUNTER_SEMANTICS' in jraw:
        print('JSON: 第二条债已在 -> SKIP')
    else:
        i = hits[0]
        assert not lines[i].rstrip().endswith(','), 'JSON: 锚点已非末键，插入法需重看'
        # 锚点是 arch_debt 的最后一个键（无逗号）：给它补逗号，再追加 3 条（末条不带逗号）
        ins = [
            lines[i].rstrip() + ',',
            '      "ARCH_DEBT_AUDIO_GUARD_COUNTER_SEMANTICS": "OPEN_NON_BLOCKING",',
            '      "counter_semantics_note": "guard_truncated_turns counts over-limit DELTAS, not turns",',
            '      "guard_log_wording_frozen_at": "console.error now says SAFETY_GUARD_DURATION, not 系统合法上限"',
        ]
        new = lines[:i] + ins + lines[i + 1:]
        txt = '\r\n'.join(new) + ('\r\n' if jraw.endswith('\r\n') else '')
        jnew = json.loads(txt)                                 # 语法必须仍成立
        assert jnew[KEY]['arch_debt']['ARCH_DEBT_AUDIO_GUARD_DERIVATION'] \
            == jold[KEY]['arch_debt']['ARCH_DEBT_AUDIO_GUARD_DERIVATION']
        diff = [k for k in jold if jnew[k] != jold[k]]
        assert diff == [KEY], 'JSON: 其它键被改动 %s' % diff
        assert list(jnew[KEY].keys()) == list(jold[KEY].keys()), 'JSON: member 顶层键顺序变了'
        assert set(jnew[KEY]['arch_debt']) - set(jold[KEY]['arch_debt']) == {
            'ARCH_DEBT_AUDIO_GUARD_COUNTER_SEMANTICS', 'counter_semantics_note',
            'guard_log_wording_frozen_at'}
        assert jnew[KEY]['arch_debt']['forbidden'] == jold[KEY]['arch_debt']['forbidden']
        shutil.copy2(JS, JS.parent / (JS.name + SUF))          # 先备份再写
        JS.write_text(txt, encoding='utf-8', newline='')
        ol, nl = lines, lines_norm(JS.read_text(encoding='utf-8'))
        same = nl[:i] == ol[:i] and nl[i + len(ins):] == ol[i + 1:]
        b = JS.read_bytes()
        print('JSON: arch_debt 内追加 3 条（锚点补逗号）；两侧上下文逐行全等=%s；'
              'CR=%d LF=%d；除 %s 外全部键深相等=True；member 顶层键序未变'
              % (same, b.count(b'\r\n'), b.count(b'\n'), KEY))
        rc |= 0 if same else 1
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
