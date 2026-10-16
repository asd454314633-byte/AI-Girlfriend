# -*- coding: utf-8 -*-
"""§6.15（纯追加、幂等）：AI_GIRLFRIEND_SOLA_V2_EMBER_MINIMAL_CLEAN_01 的交接登记。

跑法：
    runtime/core/venv/Scripts/python.exe runtime/tests/fa_append_master_sola_v2_ember_0927c.py
    runtime/core/venv/Scripts/python.exe runtime/tests/fa_append_master_sola_v2_ember_0927c.py --probe

这一支**不改 §6.14**（那一节已经落盘，回去改就是改历史小节），只在文末追加一节，
并把本轮判决的指针写进 STATE.json 的 ``sola_v2_ember_voice_minimal_20260927``。

口径跟 fa_append_master_sola_v2_ember_0927b.py 一致：先备份 → 前缀 + 零删除双闸门 →
写完读回来对一遍。所有数字都是**现场量**出来的：尺寸/SHA 从渲染路径现算，进程/端口
从 psutil 现枚举，一个都不引上一节记录里的值。
"""
from __future__ import annotations

import datetime
import difflib
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "runtime" / "llm" / "AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_HANDOFF.md"
STATE = ROOT / "runtime" / "llm" / "AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_STATE.json"
SOLA_DIR = ROOT / "runtime" / "character" / "sola_v2_ember"
MARK = "<!-- SOLA_V2_EMBER_CLEAN01_v1 -->"
SUFFIX = ".bak_solaemberc_0927"
STATE_KEY = "sola_v2_ember_voice_minimal_20260927"
ANCHOR = '  "sola_v2_ember_direct_port_20260927": {'


def sha_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def measured() -> dict[str, object]:
    """现场量一遍：清洗前后整块尺寸/SHA、六段区间、开关、盘上三件产物、进程与端口。"""
    for _p in (str(ROOT), str(ROOT / "runtime" / "ui" / "app")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    from runtime.character import context_builder
    from runtime.character import store as character_store
    from runtime.character.sola_v2_ember import adapter as A, ember as E, voice_minimal as VM
    import character_admin as admin

    spec = VM.load_spec()
    who = character_store.get_active_character() or ""
    labels = A.labels_for(who)
    plain = E.render(char_label=labels["char"], user_label=labels["user"])
    clean = VM.render(char_label=labels["char"], user_label=labels["user"])
    raw_block = plain.text.strip()
    clean_block = clean.text.strip()
    out: dict[str, object] = {
        "当前人物": who,
        "称呼落地": dict(labels),
        "两把开关现场读回": {"sola_v2_ember_enabled": A.enabled(),
                             "voice_minimal_enabled": A.voice_minimal_enabled()},
        "作者原版那一块": {"bytes": len(raw_block.encode("utf-8")),
                           "chars": len(raw_block),
                           "tokens_估算": context_builder.estimate_tokens(raw_block),
                           "sha256_前16": sha_bytes(raw_block.encode("utf-8"))[:16]},
        "清洗运行版那一块": {"bytes": len(clean_block.encode("utf-8")),
                             "chars": len(clean_block),
                             "tokens_估算": context_builder.estimate_tokens(clean_block),
                             "sha256_前16": sha_bytes(clean_block.encode("utf-8"))[:16]},
        "本轮删掉的作者正文合计字数": sum(int(one["deleted_chars"]) for one in spec["edits"]),
        "删除清单": [{"id": one["id"], "category": one["category"],
                      "module": one["module_name"], "offset": one["offset_in_module_raw"],
                      "chars": one["deleted_chars"], "anchor_head": one["anchor"][:46]}
                     for one in spec["edits"]],
        "spec_sha256_前16": sha_bytes(VM.SPEC_PATH.read_bytes())[:16],
        "冻结源": {"path": "runtime/character/sola_v2_ember/source/Sola V2 Ember.json",
                   "sha256_前16": sha_bytes(
                       (SOLA_DIR / "source" / "Sola V2 Ember.json").read_bytes())[:16]},
    }
    arts = {}
    for path in (VM.ARTIFACT_TXT, VM.ARTIFACT_DIFF, VM.ARTIFACT_META):
        arts[path.name] = ({"bytes": path.stat().st_size,
                            "sha256_前16": sha_bytes(path.read_bytes())[:16],
                            "mtime": datetime.datetime.fromtimestamp(
                                path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")}
                           if path.is_file() else "盘上没有")
    out["派生产物"] = arts
    out["盘上txt等于现场清洗版"] = (arts.get(VM.ARTIFACT_TXT.name, {}).get("sha256_前16")
                                    == sha_bytes((clean_block + "\n").encode("utf-8"))[:16]) \
        if isinstance(arts.get(VM.ARTIFACT_TXT.name), dict) else "没落盘"

    import psutil
    ports = {}
    for port in (7860, 8090, 8010, 8765):
        pids = sorted({c.pid for c in psutil.net_connections(kind="tcp")
                       if c.laddr and c.laddr.port == port and c.status == "LISTEN"
                       and c.pid})
        rows = []
        for pid in pids:
            try:
                proc = psutil.Process(pid)
                cmd = " ".join(proc.cmdline())
                rows.append({"pid": pid,
                             "起于": datetime.datetime.fromtimestamp(
                                 proc.create_time()).strftime("%Y-%m-%d %H:%M:%S"),
                             "带 --reload": "--reload" in cmd})
            except Exception as exc:
                rows.append({"pid": pid, "读不到": type(exc).__name__})
        ports[f":{port}"] = rows or "没在听"
    out["端口现场"] = ports
    out["本轮改过的文件落盘时刻"] = {
        rel: datetime.datetime.fromtimestamp((ROOT / rel).stat().st_mtime)
            .strftime("%Y-%m-%d %H:%M:%S")
        for rel in ("runtime/character/sola_v2_ember/voice_minimal.py",
                    "runtime/character/sola_v2_ember/voice_minimal_edits.json",
                    "runtime/character/sola_v2_ember/adapter.py",
                    "runtime/character/sola_v2_ember/sola_v2_ember.json")}
    main = ports.get(":7860")
    if isinstance(main, list) and main and isinstance(main[0], dict):
        newest = max(out["本轮改过的文件落盘时刻"].values())
        out["生效状态"] = (f"在听的 :7860 起于 {main[0]['起于']}，本轮代码最新落盘于 "
                          f"{newest} ⇒（两把都是 %Y-%m-%d %H:%M:%S 同一把尺）" +
                          ("进程比本轮代码早：重启 :7860 之前运行期还用不到清洗版"
                           if str(main[0]["起于"]) < newest else "进程已经带着本轮代码在跑"))
    return out


SECTION = """

---

## §6.15 追加（2026-09-27 20:3x）— Ember 之上的**最小清洗运行版**（两类删除，其余逐字不动）

@@MARK@@

任务：``AI_GIRLFRIEND_SOLA_V2_EMBER_MINIMAL_CLEAN_01``。主人原话的边界是
「只删除两类内容：1. 删除 Sola 自我身份 2. 删除/禁用会被 TTS 直接念出的动作格式；
除此之外其他 Ember 内容全部保留，不压缩、不重写、不润色、不优化」，
并且「即使认为不适合语音，本轮也不许改……owner 真人实测后再决定是否逐项删除」。
做完就停：**没有 A/B、没有继续优化、没有第三处修改**。

### 6.15.1 指针（现在是什么状态一律现场读，别引本节）

| 东西 | 在哪儿 |
| --- | --- |
| 冻结的删除清单（六段：锚 + 模块 + 偏移 + 字数 + 为什么） | `runtime/character/sola_v2_ember/voice_minimal_edits.json` |
| 只做纯删除、删完能逐字插回去的执行器 | `runtime/character/sola_v2_ember/voice_minimal.py` |
| 第二把开关 | `sola_v2_ember.json:voice_minimal_enabled`，环境变量 `SOLA_V2_EMBER_VOICE_MINIMAL` 优先 |
| 给人看的三件派生产物 | `sola_v2_ember_voice_minimal.txt` / `.diff` / `.meta.json`（**运行期不读它们**） |
| 验收回执（本轮那十三个键） | `runtime/character/tests/sola_v2_ember_voice_minimal_proof.py`（`--probe` 只把盘上产物跟现场量出的对拍） |
| 上一轮移植那十六个键 | `runtime/character/tests/sola_v2_ember_proof.py`（仍全绿；接线那一档的尺改成跟着清洗开关走，见 6.15.5） |

### 6.15.2 已经冻结的判决（不随重跑改变）

* **删了什么**：只有两类，共六段作者正文，合计 @@DELCOUNT@@ 字。
  类别一「Sola 自我身份」五条：`<sola>` 与 `</sola>` 两行外壳、
  `You are Sola — a deeply observant storyteller and co-author. ` 这一句、
  Embodied Reality 里 `NPCs, the user, {{user}}, and Sola do not share the same knowledge.`
  这一条、POV 块末尾 `- This is Sola's narrative POV.`。
  类别二「会被念出来的动作格式」一条：Anti-Echo 那块里的
  `Example of Anti-Echo: … APPROVED` 三行（含 `*blushes*` /
  `*rolling the words in her mouth*`）—— 这是全份 Ember 里**唯一还活着**的
  「星号包动作 = 被认可的可输出样式」背书。
* **同一条里剩下的两条规则一个字没动**：Anti-Echo 那四条 bullet、
  `- NPCs react to the meaning of words. NPCs NEVER repeat, quote, summarize…` 都原样在位。
* **没删的（按明令保留）**：400–500 words / 4–5 paragraphs 长度规则、
  `<adult_fiction_license>` 整块（含 non-con / incest / gore 那份许可）、
  暴力与战斗条、黑暗题材与「NPC 不因你求情就改立场」、自主性 12 条、
  反谄媚 / 反助手腔、文风禁忌（Avoided Constructions 等）。
  回执里把这四组按**作者原文逐条**回读，共 29 条全部在位。
* **清洗版里 `sola` 这五个字母大小写都不剩**（原版 5 处 + 标签 4 处）。
* 其余全部模块**同一个 Python 对象**直接沿用（零拷贝模块数 86），
  所以「其他内容没被动」是**构造上成立**，不是比出来的。

### 6.15.3 本轮量到的数（现场重算口径）

```text
@@MEASURED@@
```

验收键逐条：`ORIGINAL_EMBER_UNCHANGED=YES`、`SOLA_IDENTITY_REMOVED=YES`、
`TTS_ACTION_OUTPUT_REMOVED=YES`、`OTHER_EMBER_CONTENT_REMOVED=NO`、
`OTHER_EMBER_CONTENT_REWRITTEN=NO`、`ADULT_CONTENT_PRESERVED=YES`、
`VIOLENCE_CONTENT_PRESERVED=YES`、`DARK_CONTENT_PRESERVED=YES`、
`AUTONOMY_RULES_PRESERVED=YES`、`FINAL_PROMPT_SIZE_BYTES` / `_CHARS` /
`_TOKEN_ESTIMATE`（值见上面的表，估算器是本仓库那个 CJK/ASCII 比例口径，**不是分词器**）、
`DIFF_ONLY_ALLOWED_TWO_CATEGORIES=YES`、`THIRD_CHANGE_OUTSIDE_TWO_CATEGORIES=NO`、
`PROOF_RAN_READ_ONLY=YES`。跑法：
`runtime/core/venv/Scripts/python.exe runtime/character/tests/sola_v2_ember_voice_minimal_proof.py`。

### 6.15.4 三条**留开项**（本轮按明令没动，等主人真人实测裁决）

1. **「正式回复默认只输出人物真正说出口的话」这一条，光删第二类格式达不到。**
   保留下来的作者规则里，`Run the world and every NPC. Narrate in a naturalistic,
   lived-in voice…`、`Limited to ~400–500 words / ~4–5 paragraphs of narrative prose`、
   `30–50% Dialogue to monologue and actions`、Physiology 整块、POV 第三人称现在时，
   以及 Narrative Authority 里「只控制 ephemeral actions/reactions」那条授权，
   **本身就在要求她输出叙述性的动作/身体反应文字**。这些全在「严格禁止删除或修改」
   名单里，所以本轮一个字没碰。要真做到「只念说出口的话」，得动的是这些规则 ——
   那是下一轮主人实测之后的决定，不是本轮的权限。
   换句话说：本轮交付的是「**没有星号动作格式**」，不是「**没有动作内容**」。
2. **`*noun*` 与 `*texture*` 两处成对星号留着**（分别在 Avoided Constructions 和
   Authentic NPCs 行里）。它们是排版强调 / **被明令避免的写法本身**，不是动作标记，
   且所在行受本轮禁改清单保护。回执里逐条点名并给了负对照。
3. **`char_label_fallback` 由 `"Sola"` 改成空串** —— 取不到档案显示名时，
   adapter 现在整块旁路（干净空块），不再编一个「Sola」当她的名字。
   这条记在 `THIRD_CHANGE_OUTSIDE_TWO_CATEGORIES=NO` 的说明里：它属于类别一的
   同一件事（身份只能来自人物档案），不是第三种改动。

### 6.15.5 本轮**没量到**的东西（下一个 Agent 别当成已通过）

* `:8090` 正式回复链路现场**没在听** ⇒ 本轮依旧**没有任何真实推理 / 朗读行为实测**。
  「TTS 到底会不会把动作念出来」只能等主人开一轮文字对话看回执。
* `:7860` 那个进程不带 `--reload`（详见 §6.14.5 同一件事）⇒ 清洗版**要重启 :7860
  才进得了运行期**。拨开关只改磁盘上那份配置。
* 上一轮那十六键脚本里的 `EMBER_PROMPT_SIZE_BYTES=7469` / `EMBER_PROMPT_TOKEN_COUNT=2090`
  量的是**作者原版那一块**（移植保真用的尺）；本轮 `FINAL_PROMPT_*` 量的是**运行版**。
  两个数同时成立才对：前者证明「移植没改作者的字」，后者证明「交付的是清洗版」。
  `sola_v2_ember_proof.py` 里 `SOLA_IN_SYSTEM_PROMPT` / `OFF_PATH_IDENTICAL` 两键的
  尺已改为跟随清洗开关（否则拿原版当尺会假报没接上），本轮复跑两键都是 YES。
* 没做：A/B、质量自评、任何额外优化、任何第三处修改。按任务原话**然后停止**。

### 6.15.6 一句话回退

把 `SOLA_V2_EMBER_VOICE_MINIMAL=false` 拨进环境（或把 `sola_v2_ember.json` 里
`voice_minimal_enabled` 改回 `false`）⇒ 下一发就逐字拿回作者原版那一块（7363 字），
不删文件、不改代码；整层都不要就拨 `SOLA_V2_EMBER_ENABLED=false`。
"""


def render_section() -> str:
    data = measured()
    pretty = json.dumps(data, ensure_ascii=False, indent=2)
    return (SECTION.replace("@@MARK@@", MARK).replace("@@MEASURED@@", pretty)
            .replace("@@DELCOUNT@@", str(data["本轮删掉的作者正文合计字数"])))


def diff_lines(before: str, after: str, want: str) -> list[str]:
    return [one for one in difflib.unified_diff(before.splitlines(), after.splitlines(),
                                                lineterm="")
            if one.startswith(want) and not one.startswith(want * 2)]


def main() -> int:
    if "--probe" in sys.argv[1:]:
        print(json.dumps(measured(), ensure_ascii=False, indent=2))
        return 0
    print("=" * 70)
    print("§6.15 最小清洗运行版交接登记 —— 纯追加 + 幂等")
    print("=" * 70)
    doc_before = DOC.read_bytes()
    state_before = STATE.read_bytes()
    for original in (DOC, STATE):
        copy = Path(str(original) + SUFFIX)
        if copy.exists():
            print(f"  备份已存在，不覆盖：{copy.name}")
        else:
            shutil.copy2(original, copy)
            print(f"  已备份 → {copy.name}")

    doc_text = doc_before.decode("utf-8")
    if MARK in doc_text:
        print("  主文档：已经登记过（看见标记了），不动")
        result = doc_text
    else:
        result = doc_text.rstrip("\n") + "\n" + render_section()
    problems: list[str] = []
    if not result.startswith(doc_text):
        problems.append("前缀检查失败")
    if diff_lines(doc_text, result, "-"):
        problems.append(f"出现 {len(diff_lines(doc_text, result, '-'))} 行删除")
    added = diff_lines(doc_text, result, "+")
    if not problems and result != doc_text:
        DOC.write_text(result, encoding="utf-8", newline="\n")
    print(f"  主文档：{len(doc_text.splitlines())} 行 → {len(DOC.read_text(encoding='utf-8').splitlines())} 行"
          f"（新增 {len(added)} 行，删除 0 行）{'；'.join(problems)}")
    if problems:
        DOC.write_bytes(doc_before)
        STATE.write_bytes(state_before)
        return 1

    state_text = state_before.decode("utf-8")
    parsed = json.loads(state_text)
    if STATE_KEY in parsed:
        print(f"  状态文件：{STATE_KEY} 已存在，不动")
    elif ANCHOR not in state_text:
        print("  状态文件：找不到插入锚点，这一项没做（请手工核对）")
        return 1
    else:
        payload = {
            "task": "AI_GIRLFRIEND_SOLA_V2_EMBER_MINIMAL_CLEAN_01",
            "recorded_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "nature": "在已移植的 Ember 之上只做两类纯删除；不做 A/B、不优化、无第三处修改",
            "switch_pointer": "runtime/character/sola_v2_ember/sola_v2_ember.json:voice_minimal_enabled",
            "kill_switch_env": "SOLA_V2_EMBER_VOICE_MINIMAL",
            "deletion_spec": "runtime/character/sola_v2_ember/voice_minimal_edits.json",
            "proof_command": "runtime/core/venv/Scripts/python.exe runtime/character/tests/sola_v2_ember_voice_minimal_proof.py",
            "handoff_section": "§6.15",
            "rollback": "拨 voice_minimal_enabled=false（或同名环境变量）即逐字拿回作者原版那一块，不删文件不重启",
        }
        block = json.dumps({STATE_KEY: payload}, ensure_ascii=False, indent=2)
        lines = block.splitlines()
        candidate = state_text.replace(ANCHOR, "\n".join(lines[1:-1]) + ",\n" + ANCHOR, 1)
        try:
            after = json.loads(candidate)
        except Exception as exc:
            print(f"  状态文件：插入后解析不了（{exc}）⇒ 回滚")
            return 1
        if set(after) - set(parsed) != {STATE_KEY} or STATE_KEY not in after:
            print("  状态文件：键集合变化不符合预期 ⇒ 回滚")
            return 1
        if diff_lines(state_text, candidate, "-"):
            print("  状态文件：出现删除行 ⇒ 回滚")
            return 1
        STATE.write_text(candidate, encoding="utf-8", newline="\n")
        print(f"  状态文件：加了顶层键 {STATE_KEY}（{len(lines) - 2} 行），删除 0 行")

    doc_after = DOC.read_text(encoding="utf-8")
    head = next((index for index, one in enumerate(doc_after.splitlines(), 1)
                 if one.startswith("## §6.15")), 0)
    state_after = json.loads(STATE.read_text(encoding="utf-8-sig"))
    ok = bool(head) and STATE_KEY in state_after
    print("-" * 70)
    print(f"  读回：§6.15 在主文档第 {head} 行；标记在位={MARK in doc_after}")
    print(f"  读回：状态文件 {STATE_KEY} 在位={STATE_KEY in state_after}")
    print(f"  读回：主文档 SHA {sha_bytes(DOC.read_bytes())[:16]}…，"
          f"状态文件 SHA {sha_bytes(STATE.read_bytes())[:16]}…")
    print("结果：" + ("已登记" if ok else "没写成"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
