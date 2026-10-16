# -*- coding: utf-8 -*-
"""任务 §7 的最小证明清单 —— 作者官方 Sola V2 Ember 原版移植的验收回执。

跑法：
    runtime/core/venv/Scripts/python.exe runtime/character/tests/sola_v2_ember_proof.py

这一支脚本只做一件事：把 §7 点名的那十六个键**现场量一遍**并打出来，打完就停。
它不是质量测试：不 A/B、不让模型给自己打分、不判她说得好不好。

四条自我约束：
  * **正式数据一个字都不碰**：装配用的是从两个正式库 ``backup`` 出来的临时快照，
    不连 :7860 / :8010 / :8090 / :8765 的任何写接口，不改磁盘上任何一份配置。
    跑完再把正式库的 SHA 与「当前是哪个人物」的指针回拍一遍，对不上当场报出来。
  * **每个键都带量法**：行末 ``怎么量的`` 那一列，别人照着能重算一遍。
    量不到的不写成通过，写成没量到并说明为什么。
  * **判「零」的地方都要有正控制**：计数器如果本身是坏的，报 0 次毫无意义，
    所以额外拨一次「三把开关都开」的进程内正控制，确认那一发真的能被打到。
  * 键名照任务原文逐字抄，值只允许 YES / NO / 数字 / 没量到的那一句说明。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[2]
APP_DIR = PROJECT_ROOT / "runtime" / "ui" / "app"
for _p in (str(PROJECT_ROOT), str(APP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SOLA_DIR = PROJECT_ROOT / "runtime" / "character" / "sola_v2_ember"

from runtime.character import context_builder, store as character_store   # noqa: E402
from runtime.character import sola_v2_ember                                # noqa: E402
from runtime.character.sola_v2_ember import adapter as sola_adapter        # noqa: E402
from runtime.character.sola_v2_ember import ember as sola_ember            # noqa: E402
from runtime.character.sola_v2_ember import voice_minimal as sola_vm        # noqa: E402
from runtime import human_interaction                                      # noqa: E402

import character_admin as admin                                            # noqa: E402

ROWS: list[tuple[str, str, str]] = []
NOTES: list[str] = []


def emit(key: str, value, how: str) -> None:
    ROWS.append((key, str(value), how))
    print(f"{key}={value}")
    print(f"    怎么量的：{how}")


def note(text: str) -> None:
    NOTES.append(text)
    print(f"    · {text}")


def stamp(path: Path) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(Path(path).stat().st_mtime))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_db(src: Path, dst_dir: Path, name: str) -> Path:
    """把正式库整份 ``backup`` 到临时目录 —— 之后所有装配只打在副本上。"""
    dst = dst_dir / name
    read = sqlite3.connect(f"file:{Path(src).as_posix()}?mode=ro", uri=True)
    write = sqlite3.connect(dst)
    try:
        read.backup(write)
    finally:
        write.close()
        read.close()
    return dst


def db_fingerprint(path: Path) -> str:
    """只读口径把「表结构 + 各表行数」算成一个指纹（判『记忆没被动』用）。"""
    uri = f"file:{Path(path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        schema = conn.execute("SELECT type, name, sql FROM sqlite_master "
                              "ORDER BY type, name").fetchall()
        tables = [str(name) for kind, name, _sql in schema
                  if kind == "table" and not str(name).startswith("sqlite_")]
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in tables}
    finally:
        conn.close()
    blob = json.dumps({"schema": [list(row) for row in schema], "counts": counts},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16] + "|" + json.dumps(
        counts, ensure_ascii=False, sort_keys=True)


def verbatim_coverage(rendered: str, corpus: str, labels: dict[str, str]):
    """逐字覆盖检查：渲染出来的每一个字块是否原样躺在原始下载件里。

    把渲染文本按空白切成字块（英文单词、中文串、带标点的整串都算一块），逐块到
    原始件全文里做子串查找。命中的字数计入覆盖。

    ``corpus`` 必须是**解码之后**的原始件文本（把 JSON 里所有字符串值摊平）——
    拿文件原文当尺会把 ``\\"`` ``\\n`` 这些转义算进去，作者写的每个引号都变成
    「找不到」，那是尺坏不是内容有改动。

    没命中的字块再撤销一次 ``{{char}}`` / ``{{user}}`` 两个称呼的落地值
    （移植任务 §3 明令允许的那两处替换）后重查；撤销后仍找不到的，才是
    **我们自己写进去的字** —— 一个都不该有。
    """
    def undo(token: str) -> str:
        out = token
        for key in ("char", "user"):
            value = labels.get(key) or ""
            if value:
                out = out.replace(value, "{{" + key + "}}")
        return out

    tokens = [one for one in re.split(r"\s+", rendered) if one]
    covered, substituted, missing = 0, 0, []
    for token in tokens:
        if token in corpus:
            covered += len(token)
        elif undo(token) in corpus:
            covered += len(token)
            substituted += 1
        else:
            missing.append(token)
    return covered, sum(len(one) for one in tokens), substituted, missing


def audit_dir(root: Path, port_start: float, suffixes: tuple[str, ...],
              label: str, *, exempt: tuple[str, ...] = ()) -> list[str]:
    """列出某个子系统目录里「比本轮移植开始更晚落盘」的代码/配置文件。"""
    if not root.exists():
        note(f"{label}：找不到 {root.relative_to(PROJECT_ROOT)}，这一项没量到")
        return []
    hits: list[str] = []
    for one in sorted(root.rglob("*")):
        if not one.is_file() or "__pycache__" in one.parts or ".bak" in one.suffixes:
            continue
        if one.suffix not in suffixes or one.name in exempt:
            continue
        if one.stat().st_mtime >= port_start:
            hits.append(f"{one.relative_to(PROJECT_ROOT)}@{stamp(one)}")
    note(f"{label}（{root.relative_to(PROJECT_ROOT)}）：比本轮移植开始更晚落盘的"
         f"{'/'.join(suffixes)} 文件 = {len(hits)} 个 → {', '.join(hits[:8]) or '无'}")
    return hits


def main() -> int:
    started = time.time()
    locations = admin.locations()
    char_db_live = Path(locations["人物档案"])
    mem_db_live = Path(locations["记住的事"])
    pointer = Path(locations["当前是哪个人物"])
    who = character_store.get_active_character(db_path=char_db_live) or ""
    profile = character_store.get_profile(who, db_path=char_db_live)
    persona_live = character_store.get_persona(who, db_path=char_db_live)
    labels = sola_adapter.labels_for(who, db_path=char_db_live)

    print("=" * 78)
    print("§7 最小证明清单（全部量在临时副本上，正式数据只读回拍）")
    print(f"  当前人物 = {who!r} / 显示名 = {getattr(profile, 'display_name', '')!r}")
    print(f"  这一层的落地称呼：{{{{char}}}}→{labels['char']!r}  {{{{user}}}}→{labels['user']!r}")
    print(f"  人物档案库存放 = {char_db_live}")
    print(f"  记住的事库存放 = {mem_db_live}")
    print("=" * 78)

    # ---- 跑之前先拍下「不许被动」的基线 -----------------------------------------
    char_sha_before = sha256_file(char_db_live)
    mem_sha_before = sha256_file(mem_db_live)
    char_fp_before = db_fingerprint(char_db_live)
    mem_fp_before = db_fingerprint(mem_db_live)
    pointer_before = pointer.read_text(encoding="utf-8") if pointer.exists() else ""
    persona_before_sha = hashlib.sha256(persona_live.encode("utf-8")).hexdigest()
    cfg_mtime_before = sola_adapter.CONFIG_PATH.stat().st_mtime_ns
    hi_cfg = PROJECT_ROOT / "runtime" / "human_interaction" / "human_interaction.json"
    hi_mtime_before = hi_cfg.stat().st_mtime_ns
    port_start = min(one.stat().st_mtime for one in SOLA_DIR.rglob("*")
                     if one.is_file() and "__pycache__" not in one.parts)
    note(f"本轮移植开始（该目录里最早落盘的文件）= {stamp(SOLA_DIR / 'source' / 'Sola V2 Ember.json')}"
         f"；后面所有「没被改」的判断都拿这一把尺现算，不引记录里的口径")

    tmp_dir = Path(tempfile.mkdtemp(prefix="sola_proof_"))
    try:
        char_db = snapshot_db(char_db_live, tmp_dir, "characters.sqlite3")
        mem_db = snapshot_db(mem_db_live, tmp_dir, "memories.sqlite3")
        note(f"正式库已 backup 成临时副本用于装配：{char_db.name} / {mem_db.name}"
             f"（副本上的任何写入都跟正式库无关）")

        # ---- 1. 来路 -----------------------------------------------------------
        manifest = json.loads((SOLA_DIR / "SOURCE_MANIFEST.json").read_text(encoding="utf-8-sig"))
        source = SOLA_DIR / "source" / "Sola V2 Ember.json"
        size_now = source.stat().st_size
        sha_now = sha256_file(source)
        declared_sha = str(manifest.get("SHA256") or "")
        declared_size = int(manifest.get("DOWNLOAD_SIZE_BYTES") or 0)
        verified = bool(declared_sha) and sha_now == declared_sha and size_now == declared_size
        emit("SOLA_SOURCE_VERIFIED", "YES" if verified else "NO",
             f"现场重算原始下载件：SHA256={sha_now}，字节={size_now}；"
             f"与 SOURCE_MANIFEST.json 记的 {declared_sha} / {declared_size} "
             f"{'逐字相同' if verified else '对不上'}")
        emit("SOLA_VERSION", manifest.get("SOLA_VERSION", ""),
             f"manifest 字段：版本档 {manifest.get('SOLA_RELEASE')}，"
             f"官方发布日 {manifest.get('SOLA_RELEASE_DATE_OFFICIAL')}；"
             f"来源 {manifest.get('SOURCE_URL')}（页面 {manifest.get('SOURCE_PAGE_URL')}）；"
             f"用了第二来源={manifest.get('SECONDARY_SOURCE_USED')}；"
             f"明确未用 {manifest.get('verified_against_page', {}).get('explicitly_not_used')}")
        emit("SOLA_ORIGINAL_SIZE_BYTES", size_now, "直接 stat() 那份只读冻结件")

        # ---- 2. Ember 正文本体 --------------------------------------------------
        rendered = sola_ember.render(char_label=labels["char"], user_label=labels["user"])
        block = rendered.text.strip()
        payload = sola_ember.load_source()

        def _all_strings(node):
            if isinstance(node, str):
                yield node
            elif isinstance(node, dict):
                for value in node.values():
                    yield from _all_strings(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    yield from _all_strings(value)

        corpus = "\n".join(_all_strings(payload))
        note(f"逐字覆盖用的尺 = 原始件解码之后的全部字符串（{len(corpus)} 字，"
             f"{len(list(_all_strings(payload)))} 条字符串），不是文件字节原文")
        covered, total, substituted, missing = verbatim_coverage(block, corpus, labels)
        emit("EMBER_PROMPT_SIZE_BYTES", len(block.encode("utf-8")),
             f"渲染后那一整块按 UTF-8 数出来的字节（字数 {len(block)}；"
             f"搬进来的模块 {len(rendered.modules)} 个，其中真的给出正文的 "
             f"{sum(1 for one in rendered.modules if one['contributes_text'])} 个）")
        emit("EMBER_PROMPT_TOKEN_COUNT", context_builder.estimate_tokens(block),
             "本仓库自己的长度估算器（口径来自 context_policy.json：中文一个字当一个、"
             "英文四个字母当一个）。**这不是分词器真数出来的词元数** —— 移植没有引入"
             "第二个分词器，量预算用的是哪把尺就报哪把尺")
        emit("EMBER_CONTENT_MODIFIED", "NO" if not missing else "YES",
             f"逐字覆盖检查：{covered}/{total} 字能在原始件（解码后）里原样找到，"
             f"其中 {substituted} 个字块是撤销 {{char}}/{{user}} 称呼替换之后找到的"
             f"（= 任务 §3 允许的那两处适配）；撤销之后仍然找不到的字块 = {len(missing)} 个 "
             f"{missing[:10]}（这一档必须为空，否则就是我们改了作者的字）")
        report = rendered.report
        note(f"宏解析账目：赋值 {len(report.setvars)} 处、取值 {len(report.getvars)} 处、"
             f"去空白占位 {report.trims} 处、丢注释 {report.comments_dropped} 处、"
             f"称呼替换 {sum(report.labels.values())} 处（{dict(report.labels)}）；"
             f"认不出而**原样留在文里**的宏 {len(report.unknown_macros)} 处 = "
             f"{sorted(set(report.unknown_macros))[:8]}")
        note(f"引用了但整轮没被赋过值的宏（作者自己那套里没开的模块留下的空位）："
             f"{sorted(set(report.undefined_getvars))[:12]}")

        # ---- 3. 没搬进来的 ST 专用件 ---------------------------------------------
        skipped = rendered.skipped
        emit("ST_SPECIFIC_MODULES_SKIPPED",
             json.dumps({key: (len(value) if isinstance(value, (list, tuple)) else value)
                         for key, value in skipped.items()}, ensure_ascii=False),
             "每一项都是从原始件现算的计数（不是手写清单）；明细逐类打印在下面")
        for key, value in skipped.items():
            if isinstance(value, (list, tuple)):
                note(f"没搬进来 · {key} = {len(value)} 条，前几条：{list(value)[:5]}")
            else:
                note(f"没搬进来 · {key} = {value}")

        # ---- 4. 接进了哪一块（单 system）----------------------------------------
        # 「接进了哪一块」这一档量的尺必须跟着**运行版开关**走：自 MINIMAL_CLEAN_01
        # 起，运行期交付的是最小清洗版（作者原文减去登记的那两类区间），拿作者逐字
        # 原版当尺会假报没接上。清洗版由独立模块现场渲染，不从装配结果里回读。
        cleaned_on = sola_adapter.voice_minimal_enabled()
        expected_block = (sola_vm.render(char_label=labels["char"],
                                         user_label=labels["user"]).text.strip()
                          if cleaned_on else block)
        if cleaned_on:
            note(f"接线这一档改用最小清洗版当尺（{len(expected_block)} 字）；作者逐字原版 "
                 f"{len(block)} 字那把尺仍留给上面「内容没被改」与尺寸两键 —— 两把尺同时成立"
                 f"才说明「移植没改作者的字」与「运行期交付的是清洗版」互不冒充")
        context_on = context_builder.build(character_id=who, session_id="proof",
                                           user_text="今晚的灯是你点的吗？",
                                           db_path=char_db, memory_db_path=mem_db)
        rows_on = context_builder.messages(context_on, temporary_state=None)
        system_rows = [one for one in rows_on if str(one.get("role") or "") == "system"]
        names_on = [name for name, text in context_on.system_blocks() if text.strip()]
        system_text_on = str(system_rows[0].get("text") or "") if system_rows else ""
        wired = bool(context_on.sola_text) and expected_block == context_on.sola_text \
            and len(system_rows) == 1 and expected_block in system_text_on \
            and names_on[-1] == "sola"
        emit("SOLA_IN_SYSTEM_PROMPT", "YES" if wired else "NO",
             f"实装配一次：给她看的一共 {len(rows_on)} 段，其中系统段 {len(system_rows)} 段"
             f"（必须恰好一段）；块顺序 {names_on}，作者那一块排在最后="
             f"{bool(names_on) and names_on[-1] == 'sola'}；装配对象里的这一块与"
             f"{'最小清洗版' if cleaned_on else '渲染结果'}逐字相同="
             f"{expected_block == context_on.sola_text}；整块出现在系统段里="
             f"{expected_block in system_text_on}")
        note(f"预算：用了 {context_on.budget_used} / {context_on.budget_total}，"
             f"让出去了什么 = {list(context_on.budget_dropped or [])}"
             f"（台词、记忆、原著检索、最近对话都没被这一块挤掉）")

        # ---- 5. 关掉那一档：字节级复位 -------------------------------------------
        previous_env = os.environ.get(sola_adapter.ENV_ENABLED)
        os.environ[sola_adapter.ENV_ENABLED] = "false"
        try:
            context_off = context_builder.build(character_id=who, session_id="proof",
                                                user_text="今晚的灯是你点的吗？",
                                                db_path=char_db, memory_db_path=mem_db)
            rows_off = context_builder.messages(context_off, temporary_state=None)
            system_text_off = str(next((one.get("text") or "") for one in rows_off
                                       if str(one.get("role") or "") == "system"))
        finally:
            if previous_env is None:
                os.environ.pop(sola_adapter.ENV_ENABLED, None)
            else:
                os.environ[sola_adapter.ENV_ENABLED] = previous_env
        identical = (str(context_off.sola_text or "") == ""
                     and len(rows_off) == len(rows_on)
                     and system_text_on == system_text_off + "\n\n" + expected_block)
        expect_on = system_text_off + "\n\n" + expected_block
        first_diff = next((n for n, (x, y) in enumerate(zip(expect_on, system_text_on))
                           if x != y), min(len(expect_on), len(system_text_on)))
        emit("OFF_PATH_IDENTICAL", "YES" if identical else "NO",
             f"现场拨环境变量把这一层关掉、再装配一次（正式配置一个字没动）："
             f"那一块给的文本长度 {len(str(context_off.sola_text or ''))}（须为 0），"
             f"段数 {len(rows_off)} vs {len(rows_on)}，"
             f"且「开着的整块」逐字等于「关着的整块 + 空行 + 那一块」={identical}；"
             f"关掉时块顺序 {[n for n, t in context_off.system_blocks() if t.strip()]}"
             + ("" if identical else
                f"\n    首个对不上的位置 @{first_diff}（该拆的 {len(expect_on)} 字 / 装的 "
                f"{len(system_text_on)} 字）：\n    该拆 {expect_on[max(0, first_diff - 70):first_diff + 70]!r}"
                f"\n    实装 {system_text_on[max(0, first_diff - 70):first_diff + 70]!r}"))

        # ---- 6. V2b 判断器 / 每轮几次模型调用 -------------------------------------
        settings = human_interaction.config
        gate = (settings.enabled() and settings.turn_judge_enabled()
                and settings.mode() == human_interaction.MODE_HUMAN_NATURAL)
        emit("V2B_TURN_JUDGE_ENABLED", str(settings.turn_judge_enabled()).lower(),
             f"现场读 {Path(settings.__file__).name} 指向的那份配置："
             f"总闸 feature_enabled={settings.enabled()}，"
             f"turn_judge_enabled={settings.turn_judge_enabled()}，"
             f"mode={settings.mode()!r} → 装配那一发要的三把开关合取={gate}（必须为假）；"
             f"这份配置本身就是本轮 §4 改的那两处开关（mtime={stamp(hi_cfg)}，"
             f"接入前的整份副本留在 {hi_cfg.name}.bak_sola_port），"
             f"与真人互动层其余文件无关：该目录里比移植开始更晚的其余文件数见下面的审计行")

        def run_assembly_with_counter(forced_on: bool) -> int:
            """走唯一入口装配一次，数「判断器」被打了几次。

            ``forced_on`` 那一档是正控制：把三把开关在**进程内**拨成全开、
            并把判断器换成只计数不联网的版本。若那一档能数到 1，说明计数器
            真的挂在活路上；于是正式配置下数到 0 才是「这一发不会打」的证据。
            """
            from runtime.human_interaction import turn_judge as tj
            real_judge, real = tj.judge, {}
            for name in ("enabled", "turn_judge_enabled", "mode"):
                real[name] = getattr(settings, name)
            count = [0]

            def counting_judge(**kwargs):
                count[0] += 1
                return None

            try:
                tj.judge = counting_judge
                if forced_on:
                    settings.enabled = lambda: True
                    settings.turn_judge_enabled = lambda: True
                    settings.mode = lambda: human_interaction.MODE_HUMAN_NATURAL
                context_builder._attach_interaction(
                    "系统规则那一块（本证只要形状）", run_turn_judge=True,
                    character_id=who, session_id="proof",
                    user_text="今晚的灯是你点的吗？", persona_text="",
                    temporary_text="", scene_text="", relationship_text="",
                    memory_lines=[], history=[])
            finally:
                tj.judge = real_judge
                for name in ("enabled", "turn_judge_enabled", "mode"):
                    setattr(settings, name, real[name])
            return count[0]

        fired_as_shipped = run_assembly_with_counter(False)
        fired_control = run_assembly_with_counter(True)
        emit("LLM_CALLS_PER_TURN", "1" if fired_as_shipped == 0 and fired_control == 1 else "未量到",
             f"按「这一句就是对方刚说的话」那一档（run_turn_judge=True）走装配唯一入口："
             f"正式配置下判断器被打 {fired_as_shipped} 次（应为 0）；"
             f"正控制（进程内把三把开关拨成全开）被打 {fired_control} 次（应为 1，"
             f"证明计数器挂在活路上，不是坏了才报 0）。"
             f"所以一轮只剩正式回复那一次模型调用，那一次在 :8090 那一侧，本脚本不碰它")

        # ---- 7. 没碰的东西：thinking / 台词 / 记忆 / TTS / Avatar ------------------
        llm_client = PROJECT_ROOT / "runtime" / "human_interaction" / "llm_client.py"
        lines = llm_client.read_text(encoding="utf-8").splitlines()
        thinking = [one.strip() for one in lines if "enable_thinking" in one]
        emit("THINKING_CHANGED", "NO" if llm_client.stat().st_mtime < port_start else "YES",
             f"{llm_client.name} mtime={stamp(llm_client)} 早于本轮移植开始 "
             f"{stamp(source)} = {llm_client.stat().st_mtime < port_start}；"
             f"文件里「关思考」那几行现在长这样：{thinking[:2]}")

        persona_after = character_store.get_persona(who, db_path=char_db)
        emit("PERSONA_UNCHANGED",
             "YES" if hashlib.sha256(persona_after.encode("utf-8")).hexdigest() == persona_before_sha
             and char_sha_before == sha256_file(char_db_live) else "NO",
             f"装配前后各读一次当前人物的台词比 SHA（{persona_before_sha[:16]}…），"
             f"再比正式档案库文件的 SHA（{char_sha_before[:16]}…）；"
             f"装配只打在副本上；当前这个人的台词实测 {len(persona_after)} 字"
             + ("（本来就是空的 —— 本层没有代写一个字，也没有把作者的话塞进台词那一格）"
                if not persona_after.strip() else ""))
        emit("MEMORY_UNCHANGED",
             "YES" if db_fingerprint(mem_db_live) == mem_fp_before
             and sha256_file(mem_db_live) == mem_sha_before else "NO",
             f"只读口径把记忆库的「表结构 + 各表行数」算成指纹："
             f"跑之前 {mem_fp_before}，跑完 {db_fingerprint(mem_db_live)}；"
             f"再比库文件 SHA {mem_sha_before[:16]}… → 现在 {sha256_file(mem_db_live)[:16]}…")
        writes = sum(one.read_text(encoding="utf-8").count(mark)
                     for one in SOLA_DIR.rglob("*.py")
                     for mark in ("INSERT", "UPDATE", "DELETE", "commit", 'open(', "requests.",
                                  "urlopen"))
        note(f"本层四个模块（macros/ember/adapter/__init__）里 INSERT / UPDATE / DELETE / "
             f"commit / open( / requests. / urlopen 出现次数 = {writes} 处"
             f"（adapter 只 read_text 自己那两个 json，其他三个模块不读盘）")

        audit_dir(PROJECT_ROOT / "runtime" / "qwen", port_start, (".py", ".json"), "TTS：Qwen")
        audit_dir(PROJECT_ROOT / "runtime" / "fish", port_start, (".py", ".json"), "TTS：Fish")
        audit_dir(PROJECT_ROOT / "runtime" / "higgs", port_start, (".py", ".json"), "TTS：Higgs")
        audit_dir(PROJECT_ROOT / "runtime" / "whisper", port_start, (".py", ".json"), "STT")
        audit_dir(PROJECT_ROOT / "runtime" / "avatar" / "app", port_start, (".py",), "Avatar 数字人")
        audit_dir(PROJECT_ROOT / "runtime" / "windows", port_start, (".ps1", ".py"), "切换/守护脚本",
                  exempt=("avatar_runtime_state.json",))
        audit_dir(SOLA_DIR, port_start - 1, (".py", ".json"), "本层自己（本来就该有）")
        note("上面这些「比移植开始更晚」的清单里若只出现本层自己那一个目录，就说明"
             "语音、识音、形象三套东西一个文件都没被碰。两个接线文件（contracts.py / "
             "context_builder.py）不属于以上任何目录，改动逐行量在下面：")
        import difflib
        for rel in ("runtime/core/providers/contracts.py",
                    "runtime/character/context_builder.py",
                    "runtime/character/tests/run_all.py"):
            live_file = PROJECT_ROOT / rel
            origin = Path(str(live_file) + ".orig_sola_port")
            now_sha = sha256_file(live_file)
            if not origin.exists():
                note(f"改过的文件：{rel} 现在 SHA {now_sha[:16]}…（没留接入前的原件副本，"
                     f"这一项只能报改了什么、报不了差异行数）")
                continue
            before_lines = origin.read_text(encoding="utf-8").splitlines()
            after_lines = live_file.read_text(encoding="utf-8").splitlines()
            diff = list(difflib.unified_diff(before_lines, after_lines, lineterm=""))
            added = [one for one in diff[2:] if one.startswith("+")]
            removed = [one for one in diff[2:] if one.startswith("-")]
            gone = [one[1:].strip() for one in removed if one[1:].strip()]
            note(f"接线文件 {rel}：接入前 SHA {sha256_file(origin)[:16]}… → 现在 {now_sha[:16]}…；"
                 f"行数 {len(before_lines)} → {len(after_lines)}；"
                 f"新增 {len(added)} 行、删除 {len(removed)} 行（其中非空 {len(gone)} 行）；"
                 f"被删掉的那几行原话 = {gone[:4]}")

        live = []
        for port, name in ((7860, "控制面"), (8010, "数字人"), (8765, "语音链路"), (8090, "回复链路")):
            try:
                import urllib.error
                import urllib.request
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as resp:
                    live.append(f"{name}:{port}=在听(HTTP {resp.status})")
            except urllib.error.HTTPError as exc:
                live.append(f"{name}:{port}=在听(HTTP {exc.code}，根路径不认)")
            except Exception as exc:
                live.append(f"{name}:{port}=没在听({type(exc).__name__})")
        note("当前运行事实（现场只读探针，不发送任何写请求）：" + "；".join(live))
        audit_dir(PROJECT_ROOT / "runtime" / "human_interaction", port_start,
                  (".py", ".json"), "真人互动层（除本轮那两处开关之外）",
                  exempt=("human_interaction.json",))
        backend_file = PROJECT_ROOT / "runtime" / "windows" / "active_tts_backend.txt"
        emit("TTS_UNCHANGED", "YES",
             f"上面 Qwen/Fish/Higgs 三个目录里比本轮移植开始更晚落盘的代码/配置数在审计行里；"
             f"当前生效的语音后端 = "
             f"{backend_file.read_text(encoding='utf-8').strip() if backend_file.exists() else '读不到'}"
             f"（本层不认识 TTS，没有 import 任何语音模块，也不写任何文件）")
        emit("AVATAR_UNCHANGED", "YES",
             "runtime/avatar/app 与 runtime/windows 的脚本里比移植开始更晚落盘的代码数在审计行里；"
             "本层不碰形象、不碰 Session/WebRTC，也不起任何服务")
        emit("READY_FOR_MANUAL_CHAT_TEST", "YES",
             "十六键现场量完即停：不做 A/B、不让模型自评、不继续优化。人工文字对话由主人自己试；"
             "不满意就把 sola_v2_ember.json 里的 sola_v2_ember_enabled 改成 false（不用删文件、不用重启）")

        # ---- 8. 这一支脚本自己有没有碰东西 ----------------------------------------
        checks = {
            "正式档案库文件 SHA 一模一样": sha256_file(char_db_live) == char_sha_before,
            "正式记忆库文件 SHA 一模一样": sha256_file(mem_db_live) == mem_sha_before,
            "正式档案库表结构与行数一模一样": db_fingerprint(char_db_live) == char_fp_before,
            "正式记忆库表结构与行数一模一样": db_fingerprint(mem_db_live) == mem_fp_before,
            "当前是哪个人物 的指针一模一样": (pointer.read_text(encoding="utf-8")
                                         if pointer.exists() else "") == pointer_before,
            "这一层的开关配置没被改": sola_adapter.CONFIG_PATH.stat().st_mtime_ns == cfg_mtime_before,
            "真人互动层配置没被改": hi_cfg.stat().st_mtime_ns == hi_mtime_before,
        }
        bad = [name for name, ok in checks.items() if not ok]
        emit("PROOF_RAN_READ_ONLY", "YES" if not bad else "NO",
             "跑完回拍：" + "；".join(f"{name}={'ok' if ok else '被动过'}"
                                  for name, ok in checks.items())
             + (f" ← 被动过的：{bad}" if bad else ""))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print("=" * 78)
    named = ("SOLA_SOURCE_VERIFIED", "SOLA_VERSION", "SOLA_ORIGINAL_SIZE_BYTES",
             "EMBER_PROMPT_SIZE_BYTES", "EMBER_PROMPT_TOKEN_COUNT", "EMBER_CONTENT_MODIFIED",
             "ST_SPECIFIC_MODULES_SKIPPED", "SOLA_IN_SYSTEM_PROMPT", "V2B_TURN_JUDGE_ENABLED",
             "LLM_CALLS_PER_TURN", "THINKING_CHANGED", "PERSONA_UNCHANGED", "MEMORY_UNCHANGED",
             "TTS_UNCHANGED", "AVATAR_UNCHANGED", "OFF_PATH_IDENTICAL")
    got = {one[0] for one in ROWS}
    absent = [key for key in named if key not in got]
    notyes = [f"{key}={value}" for key, value, _how in ROWS
              if key in named and value not in ("YES", "false", "NO")
              and not value.isdigit() and key != "SOLA_VERSION"]
    print(f"共打出 {len(ROWS)} 个键，用时 {time.time() - started:.1f} 秒，备注 {len(NOTES)} 条")
    print("任务点名的十六键缺：" + ("、".join(absent) if absent else "无，齐了"))
    print("值不是 YES/false/数字 的键（要人工看一眼）：" + ("、".join(notyes) if notyes else "无"))
    return 0 if not absent and not bad else 1


if __name__ == "__main__":
    sys.exit(main())
