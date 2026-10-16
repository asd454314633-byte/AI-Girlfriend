# -*- coding: utf-8 -*-
"""把「Sola V2 Ember 原版移植」这一轮写进主交接事实源（纯追加，幂等）。

写的是**指针**，不是状态快照：开关现在开没开、服务现在活没活，一律让下一个 Agent
自己去读那个文件或那条 HTTP —— 这一节只登记「东西在哪、量法是什么、哪些判决已经冻结、
哪些留开项不许顺手修」。

跑法：
    runtime/core/venv/Scripts/python.exe runtime/tests/fa_append_master_sola_v2_ember_0927.py

四条自我约束（沿用本仓库既有的 fa_append_* 口径）：
  * 先备份（``.bak_solaember_0927``，已存在就不覆盖）；
  * 主文档只在末尾接一节，**不重排版、不改历史小节**：老文本必须是新文本的前缀，
    删掉的行数必须为 0，否则当场放弃并原样回滚；
  * 状态文件按「文本插入最后一个 } 之前」的方式加一个顶层键，不整份重 dump
    （重 dump 会把 15 万字的排版全改一遍，diff 就没法看了）；
  * 幂等：靠 ``<!-- SOLA_V2_EMBER_APPEND_v1 -->`` 标记判断接没接过，接过了就只报告不动手。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "runtime" / "llm" / "AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_HANDOFF.md"
STATE = ROOT / "runtime" / "llm" / "AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_STATE.json"
MARK = "<!-- SOLA_V2_EMBER_APPEND_v1 -->"
KEY = "sola_v2_ember_direct_port_20260927"
SUFFIX = ".bak_solaember_0927"

SECTION = """

---

## §6.14 追加（2026-09-27 20:1x）— 作者官方 Sola V2 **Ember** 原版移植进正式回复链路

@@MARK@@

**任务**：`AI_GIRLFRIEND_SOLA_V2_EMBER_DIRECT_PORT_01` —— 把作者 2026-09-26 发布的
Sola V2 Ember 预设**原版**接进现有单 system 装配链路（Gemma4 12B 正式回复）。
本轮性质 = 原版移植，不是重新设计提示词；做完 §7 那十六键即停：**没做 A/B、没让模型
自评、没继续优化**，也**不允许**当场开 V2.1 / V2.2 补丁。

### 6.14.1 指针（现在是什么状态一律现场读，别引本节）

| 要什么 | 去哪读 |
| --- | --- |
| 这一层开没开 | `runtime/character/sola_v2_ember/sola_v2_ember.json` 的 `sola_v2_ember_enabled`；应急退路 = 环境变量 `SOLA_V2_EMBER_ENABLED`（优先于文件） |
| 给她看的那一整块长什么样 | 界面上的「给她看的」预览，或 `runtime/core/venv/Scripts/python.exe runtime/character/tests/sola_v2_ember_proof.py` |
| 验收十六键 | 同上那支脚本（只读，量在正式库的临时副本上；`PROOF_RAN_READ_ONLY=YES` 是它自己的回拍） |
| V2b 每轮判断器开没开 | `runtime/human_interaction/human_interaction.json` 的 `feature_enabled` / `turn_judge_enabled`（本轮按 §4 关成 `false`，改前整份副本留在 `human_interaction.json.bak_sola_port`） |
| 作者原始件（只读冻结） | `runtime/character/sola_v2_ember/source/Sola V2 Ember.json`，SHA256 `ae5c16deabb7242b1f4035eccaa4c04c0873a71d2b72fbddfb6669acbd35bcfe`，337390 字节；来路与 Ember↔Flame 对拍结论在 `SOURCE_MANIFEST.json` |
| 接入前基线（本仓库不是 git） | `runtime/core/providers/contracts.py.orig_sola_port`、`runtime/character/context_builder.py.orig_sola_port`、`runtime/character/tests/run_all.py.orig_sola_port` |

**代码形状**：作者那一块经 `context_builder._sola_block()` 这**一个**带保险的门进
`ModelContext.sola_text`，在 `system_blocks()` 里永远排最后；默认空串 ⇒ 关着时整块
内容的字节与接入前逐字相同。除这两处接线（contracts.py +10/-0 行、
context_builder.py +35/-3 行，删掉的三行都是块顺序注释与列表项）之外，
生产面没动过第三个文件。

### 6.14.2 已经冻结的判决（不随重跑改变）

```text
SOLA_SOURCE_VERIFIED=YES  SOLA_VERSION=Sola V2 Ember  SOLA_ORIGINAL_SIZE_BYTES=337390
EMBER_PROMPT_SIZE_BYTES=7469（7363 字）  EMBER_PROMPT_TOKEN_COUNT=2090（本仓库估算器口径，不是分词器）
EMBER_CONTENT_MODIFIED=NO（6186/6186 字块可在原始件解码文本里原样找到；其中 19 块是撤销
  {char}/{user} 称呼替换后找到的，撤销后仍找不到的 = 0）
ST_SPECIFIC_MODULES_SKIPPED=角色卡数据锚 9 + 分区装饰行 9 + Ember 自己关着的模块 64
  + regex_scripts 149 + assistant 角色行折进单 system 共 2
SOLA_IN_SYSTEM_PROMPT=YES（系统段恰好 1 段，块顺序 …tools → sola）
V2B_TURN_JUDGE_ENABLED=false  LLM_CALLS_PER_TURN=1
  （正控制：进程内把三把开关拨成全开时判断器确实被打 1 次 ⇒ 报 0 是开关的结果不是计数器坏了）
THINKING_CHANGED=NO  PERSONA_UNCHANGED=YES  MEMORY_UNCHANGED=YES
TTS_UNCHANGED=YES  AVATAR_UNCHANGED=YES  OFF_PATH_IDENTICAL=YES
READY_FOR_MANUAL_CHAT_TEST=YES
```

回归现状（同一台机器、同一份配置）：`runtime/character/tests/run_all.py` 开着这一层
277/277 通过、关着 276/276 通过（少的那一条是「超出部分只由那一块造成」那一档，
关着时没有对象可测，脚本自己登记 `SOLA_TIER_SKIPPED`）；
记忆 123/123、真人互动层 82 例、提供方契约 8 项、提供方中立 41/41、
状态单一权威 19 项，全通过。

### 6.14.3 移植带来的**已知后果**（登记，不在本轮处理）

这几条都是「原样搬作者规则」的必然结果，改哪一条都违反 §2「不许改正文」，
所以只有主人自己文字聊过之后才有权决定：

1. `[Core] Unfiltered Register & Consent` 那一整块带着 `<adult_fiction_license>`
   原文进来，里面逐条列了非自愿/乱伦/血腥等范围 —— 它是**独立可识别**的作者文本，
   且落在正式回复链路上。
2. 第一句是 `You are Sola — a deeply observant storyteller and co-author`，
   与人物档案里「你叫周慧」那一段身份并存，两个身份谁压谁本轮没测、也不许测。
3. 作者写死了 `~400–500 词 / 4–5 段`的长度规则，与"语音陪伴回话通常很短"这个
   使用习惯有张力。
4. 规则里有大量 `*动作*` 星号写法与少量 XML 标签名（如 `<tempo>`、`<sola>`、`<core>`），
   这些字有可能被 TTS 念出来 —— 本轮没做读后处理（§3 禁接 regex 后处理）。
5. §3 与 §4 的口径冲突留档：作者的 `Post-History` 锚点在 SillyTavern 里是**历史之后**
   的第二段 system，本仓库禁止多 system，所以那一块被放到唯一 system 段的**最末尾**。
6. Ember 那 25 个启用模块里只有 6 个真的给出正文：`[Core] User: Agency-Lite` 整块是
   `{{setvar}}`，它的正文靠 `{{getvar::sola_narrative}}` 进 `Oath` 那一块 —— 这是作者
   自己的宏接力，不是丢了模块（共用一张变量表，见 `sola_v2_ember/__init__.py`）。
7. 当前活动人物的台词那一格实测 0 字（本来就是空的）。本轮没代写一个字，
   所以「她的语气」目前完全由人物档案那几句 + 作者这一块决定。

### 6.14.4 本轮**没量到**的东西（下一个 Agent 别当成已通过）

`:8090`（正式回复链路）现场探针 = 没在听（URLError）。所以本轮**没有**任何真实推理、
时延、字数、朗读行为的实测证据：十六键全部是「装配形状 + 静态审计 + 进程内计数」量出来的。
文字对话验收只能由主人自己开一轮（§8），开之前请先确认 8090 起来了。
另：`runtime/llm/test_input_v4.py` 需要 WSL 里的解释器路径，从 Windows 解释器跑必然
`FileNotFoundError`（预先存在的环境耦合，与本轮无关，只登记不修）。
"""
SECTION = SECTION.replace("@@MARK@@", MARK)


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def backup(path: Path) -> Path:
    copy = Path(str(path) + SUFFIX)
    if copy.exists():
        print(f"  备份已存在，不覆盖：{copy.name}（{copy.stat().st_size} 字节）")
        return copy
    shutil.copy2(path, copy)
    print(f"  已备份 → {copy.name}")
    return copy


def main() -> int:
    for path in (DOC, STATE):
        if not path.exists():
            print(f"找不到 {path}")
            return 2
    print("=" * 70)
    print("把本轮写进主交接事实源（纯追加 + 幂等）")
    print("=" * 70)
    backup(DOC)
    backup(STATE)

    doc_before = DOC.read_bytes()
    doc_text = doc_before.decode("utf-8")
    if MARK in doc_text:
        print("  主文档：已经接过这一节（看见标记了），不动")
        doc_result = doc_text
        doc_appended = False
    else:
        last = doc_text.rstrip().splitlines()[-1]
        doc_result = doc_text.rstrip("\n") + "\n" + SECTION
        doc_appended = True
        print(f"  主文档：接在最后一行之后 → {last[:56]}")

    # 硬闸门：老文本必须是新文本的前缀 ⇒ 历史一个字都没被改、没被删
    if not doc_result.startswith(doc_text):
        print("  主文档：前缀检查失败，放弃写入并回滚")
        DOC.write_bytes(doc_before)
        return 1
    removed = [one for one in difflib.unified_diff(doc_text.splitlines(),
                                                   doc_result.splitlines(), lineterm="")
               if one.startswith("-") and not one.startswith("---")]
    added = [one for one in difflib.unified_diff(doc_text.splitlines(),
                                                 doc_result.splitlines(), lineterm="")
             if one.startswith("+") and not one.startswith("+++")]
    if removed:
        print(f"  主文档：出现 {len(removed)} 行删除，放弃写入并回滚：{removed[:3]}")
        DOC.write_bytes(doc_before)
        return 1
    if doc_appended:
        DOC.write_text(doc_result, encoding="utf-8", newline="\n")
    print(f"  主文档：{len(doc_text.splitlines())} 行 → {len(doc_result.splitlines())} 行"
          f"（新增 {len(added)} 行，删除 0 行）")

    state_before = STATE.read_bytes()
    state_text = state_before.decode("utf-8")
    parsed = json.loads(state_text)
    if KEY in parsed:
        print("  状态文件：顶层键已存在，不动")
    else:
        block = json.dumps({
            KEY: {
                "task": "AI_GIRLFRIEND_SOLA_V2_EMBER_DIRECT_PORT_01",
                "recorded_at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
                "nature": "原版移植（不是重新设计提示词）；做完 §7 十六键即停，未做 A/B / 自评 / 优化",
                "switch_pointer": "runtime/character/sola_v2_ember/sola_v2_ember.json:sola_v2_ember_enabled",
                "kill_switch_env": "SOLA_V2_EMBER_ENABLED",
                "proof_command": "runtime/core/venv/Scripts/python.exe runtime/character/tests/sola_v2_ember_proof.py",
                "frozen_source": {
                    "path": "runtime/character/sola_v2_ember/source/Sola V2 Ember.json",
                    "sha256": "ae5c16deabb7242b1f4035eccaa4c04c0873a71d2b72fbddfb6669acbd35bcfe",
                    "bytes": 337390,
                    "source_url": "https://pyrxpia.github.io/sola-hub/presets/Sola%20V2%20Ember.json",
                    "secondary_source_used": "NO",
                },
                "verdicts_frozen": {
                    "SOLA_SOURCE_VERIFIED": "YES",
                    "EMBER_PROMPT_SIZE_BYTES": 7469,
                    "EMBER_PROMPT_TOKEN_COUNT_estimator_only": 2090,
                    "EMBER_CONTENT_MODIFIED": "NO",
                    "SOLA_IN_SYSTEM_PROMPT": "YES",
                    "V2B_TURN_JUDGE_ENABLED": "false",
                    "LLM_CALLS_PER_TURN": 1,
                    "THINKING_CHANGED": "NO",
                    "PERSONA_UNCHANGED": "YES",
                    "MEMORY_UNCHANGED": "YES",
                    "TTS_UNCHANGED": "YES",
                    "AVATAR_UNCHANGED": "YES",
                    "OFF_PATH_IDENTICAL": "YES",
                    "READY_FOR_MANUAL_CHAT_TEST": "YES",
                },
                "not_measured": "8090 回复链路现场没在听 ⇒ 无真实推理/时延/朗读实测；"
                                "runtime/llm/test_input_v4.py 需 WSL 解释器（预先存在的环境耦合）",
                "handoff_section": "§6.14",
                "authoritative_doc": DOC.name,
            }
        }, ensure_ascii=False, indent=2)
        insert_at = state_text.rstrip().rfind("}")
        inner = block.strip()[1:-1].strip()          # 去掉最外层那对花括号
        candidate = (state_text.rstrip()[:insert_at].rstrip() + ",\n  "
                     + inner + "\n}\n")
        try:
            check = json.loads(candidate)
        except Exception as exc:
            print(f"  状态文件：插入之后解析不了（{exc}），放弃写入")
            STATE.write_bytes(state_before)
            return 1
        if set(check) - set(parsed) != {KEY} or len(check) != len(parsed) + 1:
            print("  状态文件：新键之外的内容变了，放弃写入并回滚")
            STATE.write_bytes(state_before)
            return 1
        STATE.write_text(candidate, encoding="utf-8", newline="\n")
        removed_state = [one for one in difflib.unified_diff(state_text.splitlines(),
                                                             candidate.splitlines(),
                                                             lineterm="")
                         if one.startswith("-") and not one.startswith("---")]
        print(f"  状态文件：加了 1 个顶层键（{len(candidate.splitlines())} 行，"
              f"删除 {len(removed_state)} 行）")
        if removed_state:
            print(f"  状态文件：出现删除行 {removed_state[:2]} ⇒ 回滚")
            STATE.write_bytes(state_before)
            return 1

    # 写完读回来对一遍（"已做完 X" 必须回读，不能信自己刚才那一步）
    doc_after = DOC.read_text(encoding="utf-8")
    head_line = next((index for index, line in enumerate(doc_after.splitlines(), 1)
                      if line.startswith("## §6.14")), 0)
    state_after = json.loads(STATE.read_text(encoding="utf-8-sig"))
    print("-" * 70)
    print(f"  读回：§6.14 在主文档第 {head_line} 行；标记在位={MARK in doc_after}")
    print(f"  读回：状态文件顶层键数 {len(parsed)} → {len(state_after)}，"
          f"{KEY} 在位={KEY in state_after}")
    print(f"  读回：主文档 SHA {sha256(DOC)[:16]}…，状态文件 SHA {sha256(STATE)[:16]}…")
    print("结果：" + ("已追加" if head_line and KEY in state_after else "没写成，去看上面"))
    return 0 if head_line and KEY in state_after else 1


if __name__ == "__main__":
    sys.exit(main())
