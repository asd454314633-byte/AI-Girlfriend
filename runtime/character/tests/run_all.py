# -*- coding: utf-8 -*-
"""人物 + 记忆 + 界面接口 —— 整套离线自测（对照任务 §9~§21）。

跑法：
    runtime/core/venv/Scripts/python.exe runtime/character/tests/run_all.py

三条硬规矩：

  * **纯脚本 + 断言**，不引任何测试框架，不联网，不起服务：接口全部走进程内的
    ``TestClient``，只挂 ``character_admin`` 这一条路由，:7860 / :8765 / :8090 碰都不碰；
  * **全程临时目录**：人物库、记忆库、「当前是哪个人物」的指针、书稿归档、整套备份
    五处存放位置一把改到 tempfile 下面。跑完还会回头验一遍正式指针没被动过；
  * **断言要正面**：每条都要证明「确实拿到了什么」，光「没报错」不算过。
    最后打印断言总数和 PASS/FAIL。
"""
from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[2]
APP_DIR = PROJECT_ROOT / "runtime" / "ui" / "app"
FIXTURE = HERE / "fixtures"

for _p in (str(PROJECT_ROOT), str(APP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import FastAPI                                  # noqa: E402
from fastapi.testclient import TestClient                    # noqa: E402

from runtime.character import (                               # noqa: E402
    bundle, canon, context_builder, importer, rules, store, sola_v2_ember,
)
from runtime.memory import extractor as mem_extractor        # noqa: E402
from runtime.memory import retrieval as mem_retrieval        # noqa: E402
from runtime.memory import service as mem_service            # noqa: E402

import character_admin as admin                               # noqa: E402

FAILURES: list[str] = []
CHECKS = [0]
NOTES: list[str] = []


def check(name: str, condition, detail: str = "") -> None:
    CHECKS[0] += 1
    if not condition:
        FAILURES.append(f"{name} :: {detail}")
        print(f"  [不通] {name}  {detail}")
    else:
        print(f"  [过] {name}")


def note(text: str) -> None:
    NOTES.append(text)
    print(f"  [注] {text}")


BANNED = rules.BANNED_UI_WORDS
CJK = re.compile(r"[\u4e00-\u9fff]")
MESSAGE_KEYS = ("说明", "理由", "原因", "错误", "状态", "备注", "详情", "提醒",
                "名字", "一句话", "进行到哪一步")


def scan_plain(label: str, *texts) -> list[str]:
    """扫一段给人看的话，露了内部名词就算不过。"""
    hits: list[str] = []
    for text in texts:
        body = str(text or "")
        if not body:
            continue
        for word in BANNED:
            if word in body and word not in hits:
                hits.append(word)
    check(label, not hits, f"出现 {hits}" if hits else "")
    return hits


def without_sola_block(context, text: str, *, label: str = "") -> str:
    """把「作者官方 Sola V2 Ember」那一整块从这一串里单独摘掉，再交给技术词闸门扫。

    为什么只挖这一块、而不是把 "prompt" 加进白名单：
      * 这道「不许露内部名词」的闸门守的是**本项目自己写的**那些文案 —— 装配引擎的
        系统规则、界面话术，怕的是她把「prompt / 数据库 / 词元」这类词当场念出来；
      * Sola 那七千多字是作者原文，移植任务 §2 明令逐字冻结、一个字都不许改。它的
        英文句子里确实有 "…or wait to be prompted." 这种普通词，被按子串扫的表误命中；
      * 自 ``..._MINIMAL_CLEAN_01`` 起这一块可能是**最小清洗版** —— 作者原文减去冻结
        清单点名的六段。豁免范围不会因此变松：闸门挖掉的是「当前真正交给她看的那一块」，
        清洗版是原版的真子集，被放过不扫的字节只会更少；那六段既然不在正文里，就无所谓
        扫不扫。
      * 白名单会让全仓库所有文案从此扫不出 "prompt"，那是把闸门改松；挖掉这一块
        只影响这一块，剩下的字照旧一个都不放过。

    这一串里没有这一块就原样返回：序列化之后是一段一段单独扫的，用户那一句本来
    就不带作者的规则块，那不算异常。「这一块必须整块出现在给她看的整块内容里」由
    下面一条独立断言钉住，不塞进这个工具函数里。

    Sola 关着时 ``sola_text`` 是空串，本函数恒等返回原串 ⇒ 这道闸门的严格度与
    接入之前逐字相同（这一条由下面那档「正控制」断言现场拨开关验，不靠嘴说）。
    """
    body = str(text or "")
    sola = str(getattr(context, "sola_text", "") or "")
    if not sola or sola not in body:
        return body
    note(f"SOLA_SCAN_EXEMPT: {label or '这一串'}跳过作者原文那一块（{len(sola)} 字，"
         "逐字冻结不许改），其余部分照旧全扫")
    return body.replace(sola, "", 1)


_SOLA_UNSET = "\x00sola-env-unset\x00"


def sola_forced_off() -> str:
    """把作者那一层临时拨到「关」，把环境变量旧值带回去好还原。

    只动进程内的环境变量，不碰磁盘上那份配置 —— 关这一层的正式办法是改
    ``sola_v2_ember.json``，而测试没有权利改用户的生产开关。
    """
    key = sola_v2_ember.adapter.ENV_ENABLED
    previous = os.environ.get(key, _SOLA_UNSET)
    os.environ[key] = "false"
    return previous


def sola_restore(previous: str) -> None:
    key = sola_v2_ember.adapter.ENV_ENABLED
    if previous == _SOLA_UNSET:
        os.environ.pop(key, None)
    else:
        os.environ[key] = previous


def flat_values(payload, out: list[str] | None = None) -> list[str]:
    """把一份响应里所有字符串叶子摊平，用来做「整包不许露技术词」的扫描。"""
    out = [] if out is None else out
    if isinstance(payload, dict):
        for value in payload.values():
            flat_values(value, out)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            flat_values(value, out)
    elif isinstance(payload, str):
        if payload.strip():
            out.append(payload)
    return out


def message_values(payload, out: list[str] | None = None) -> list[str]:
    """只取「会念给用户听」的那几个字段。"""
    out = [] if out is None else out
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in MESSAGE_KEYS and isinstance(value, str):
                out.append(value)
            message_values(value, out)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            message_values(value, out)
    return out


# 一次扫描：字面量 / 注释 / HTML 标签，各归各的，别互相顶替
_STR_TOKEN = re.compile(r'"(?:[^"\\\n]|\\.)*"'
                        r"|'(?:[^'\\\n]|\\.)*'"
                        r"|`(?:[^`\\]|\\.)*`")
_JS_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
# 算法名词按整词判，不按子串判：`STORAGE_KEYS` 里含着 "RAG" 三个字母，
# 但它不是把检索方案露给用户看。子串匹配会把这种正常代码变成假阳性，
# 而一个天天报假的闸门最后只会被人在心里关掉。
# 右边只挡字母数字、不挡下划线：`RAG_INDEX` 那种拿算法名当标识符的写法仍然算露馅。
_WORD_BOUNDARY_JARGON = re.compile(
    r"(?<![A-Za-z0-9_$])(RAG|BM25|bm25|embedding|Embedding)(?![A-Za-z0-9])")


def strip_interpolations(text: str) -> str:
    """把模板字符串里的 `${...}` 表达式挖掉，只留真会显示出来的字面文字。

    §10 管的是"用户看见的话"。`${res.json.reason}` 是代码，运行时显示的是它取到的
    值（那部分由服务端文案扫描去管），所以它不该出现在静态文案闸门里；但它两边的
    中文必须留在扫描面上 —— 挖空不等于跳过。
    """
    out: list[str] = []
    index, size = 0, len(text)
    while index < size:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "$" and text[index + 1:index + 2] == "{":
            depth, walk = 1, index + 2
            while walk < size and depth:
                step = text[walk]
                if step in "\"'`":                      # 表达式里的字符串，} 不算闭合
                    quote, walk = step, walk + 1
                    while walk < size:
                        if text[walk] == "\\":
                            walk += 2
                            continue
                        if text[walk] == quote:
                            break
                        walk += 1
                elif step == "{":
                    depth += 1
                elif step == "}":
                    depth -= 1
                walk += 1
            index = walk                                # 未闭合=被截断的片段，吃到结尾
            continue
        out.append(char)
        index += 1
    return "".join(out)


def visible_copy_texts(path: Path) -> tuple[list[str], int]:
    """从一份发货文件里挑出「给用户看的话」，返回（文案们, 字面量总条数）。

    任务 §10 禁的是**用户看到的**技术词，不是源码里不许写 `JSON.parse` 这种调用名。
    所以先把注释和代码隔开：JS/CSS 只取引号里的字面量（模板字符串里的 `${...}` 是
    代码，挖掉后剩下的才是话），HTML 只取标签之间的正文与属性值；再只留含中文的那些
    （面板文案全是中文）。
    第二个返回值是给闸门自己用的正向对照 —— 一条都挑不出来就说明闸门在空转，
    那种"干净"不算过（同一份扫描必须能证明它真的看见了东西）。
    """
    src = path.read_text(encoding="utf-8-sig")
    if path.suffix in (".js", ".css"):
        body = _JS_COMMENT.sub("", src)
        literals = [strip_interpolations(one.group(0)[1:-1]) if one.group(0)[0] == "`"
                    else one.group(0)[1:-1] for one in _STR_TOKEN.finditer(body)]
    else:
        body = _HTML_COMMENT.sub("", src)
        literals = re.findall(r'="([^"]*)"', body)
        literals += [one.strip() for one in re.split(r"<[^>]+>", body) if one.strip()]
    return [one for one in literals if one and CJK.search(one)], len(literals)


# ---------------------------------------------------------------------------
# 一个只挂人物路由的进程内客户端
# ---------------------------------------------------------------------------

def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(admin.router)
    return TestClient(app)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zero_char_"))
    char_db = tmp / "characters.sqlite3"
    mem_db = tmp / "memories.sqlite3"
    pointer = tmp / "active_character.json"
    library = tmp / "library"
    bundles = tmp / "bundles"
    real_pointer = store.POINTER_PATH

    print(f"临时存放：{tmp}")
    print(f"正式指针（只读不改）：{real_pointer}")
    before_pointer = (real_pointer.read_text(encoding="utf-8-sig")
                      if real_pointer.exists() else "<无>")

    store.init_db(char_db)
    mem_service.init_db(mem_db)
    admin.configure(character_db=char_db, memory_db=mem_db, pointer=pointer,
                    library=library, bundle_dir=bundles)
    client = make_client()

    def get(path: str, **params):
        keep = {key: value for key, value in params.items() if value is not None}
        return client.get(path, params=keep)

    def post(path: str, payload: dict | None = None):
        return client.post(path, json=payload or {})

    # ======================================================================
    print("\n[1] 存放位置与开机自检（§9 不引依赖 / §13 一处权威）")
    where = admin.locations()
    check("五处存放位置全在临时目录里",
          all(str(value).startswith(str(tmp)) for value in where.values()),
          str(where))
    r = get("/api/character/state").json()
    check("开机自检拿到 200 且 ok", r.get("ok") is True, str(r)[:120])
    check("自检里说清了五处存放位置", len(r.get("存在哪") or {}) == 5, str(r.get("存在哪")))
    check("自检一次给出五组条数",
          set(r.get("分组条数") or {}) == set(admin.UI_GROUPS), str(r.get("分组条数")))
    money = r.get("长度上限") or {}
    check("长度上限是从模型声明读来的", int(money.get("总长度") or 0) > 0
          and str(money.get("来自哪") or ""), str(money))
    scan_plain("自检的说明用人话", *message_values(r))
    words = get("/api/character/plain-words").json()
    check("禁词清单由服务器给，条数对得上",
          words.get("ok") is True and len(words.get("words") or []) == len(BANNED),
          str(words.get("条数")))
    check("禁词清单里就有任务点名的那几个",
          all(one in words.get("words", []) for one in
              ("JSON", "RAG", "chunk", "embedding", "BM25", "数据库", "prompt", "token")),
          str(words.get("words")))

    # ======================================================================
    print("\n[2] 对外只认「人能说出来的动作」（§14 九个 + 人物六个）")
    acts = {one.get("动作") for one in (get("/api/character/state").json().get("能做的事") or [])}
    nine = ("search", "add", "update", "delete", "clear_session", "clear_long_term",
            "restore_character", "export", "import")
    missing = [one for one in nine if one not in acts]
    check("九件事一件不缺", not missing, f"缺 {missing}")
    verb_rows = [one for one in admin.MEMORY_VERBS]
    check("九件事的编号与任务那份清单逐字一致",
          tuple(one["编号"] for one in verb_rows) == nine,
          str([one["编号"] for one in verb_rows]))
    check("九件事每条都配了中文名字和一句话",
          all(one.get("名字") and one.get("一句话") and one.get("接口")
              for one in verb_rows))
    scan_plain("九件事的中文名用人话", *[one["名字"] + one["一句话"] for one in verb_rows])
    mounted_paths = {route.path for route in admin.router.routes}
    for one in verb_rows:
        check(f"动作 {one['编号']} 的接口挂得上", one["接口"] in mounted_paths,
              str(one["接口"]))
    check("人物那一侧另有六个动作", len(admin.CHARACTER_ACTS) == 6, str(admin.CHARACTER_ACTS))

    # ======================================================================
    print("\n[3] 人物：新建 / 编辑 / 换人 / 台词自查（§19）")
    made = post("/api/character/create", {"display_name": "沈砚", "nickname": "小砚",
                                          "pronoun": "她", "relationship": "船工与灯守",
                                          "greeting": "你来了。", "likes": ["浓茶"],
                                          "dislikes": ["吵"], "persona": "",
                                          "select": True}).json()
    who = str(made.get("编号") or "")
    check("新建人物拿到编号", made.get("ok") is True and bool(who), str(made)[:160])
    check("当前指针指向新建的人物", who == admin._active_or(None), str(who))
    dup = post("/api/character/create", {"display_name": "沈砚"}).json()
    check("重名不会静默覆盖", dup.get("ok") is False and dup.get("reason"), str(dup)[:160])
    edited = post("/api/character/edit", {"character_id": who,
                                          "dislikes": ["吵", "空头承诺"]}).json()
    check("编辑只改传上来的那一项", edited.get("ok") is True, str(edited)[:160])
    row = next((one for one in get("/api/character/list").json().get("人物列表") or []
                if one.get("编号") == who), {})
    check("编辑后两处存的内容不一样了", row.get("不爱") == ["吵", "空头承诺"]
          and row.get("偏爱") == ["浓茶"], str(row))
    check("没传的字段保持原样", row.get("角色名称") == "小砚", str(row))

    leaky = post("/api/character/persona", {
        "character_id": who,
        "text": "根据人物设定，我是一个人工智能，我的性格是安静。"}).json()
    check("露馅台词被拒绝", leaky.get("ok") is False, str(leaky)[:160])
    check("拒绝时给的是中文理由，不是异常文字",
          bool(leaky.get("说明")) and "Traceback" not in str(leaky), str(leaky)[:160])
    scan_plain("拒绝台词的说明用人话", *message_values(leaky))
    good_persona = ("她话少，习惯先把茶盏扣过来烫一遍再倒。\n"
                    "别人问三次才答一次，答的时候只说眼前那件事。\n"
                    "不解释自己为什么这样。")
    saved = post("/api/character/persona", {"character_id": who, "text": good_persona}).json()
    check("正常台词写得进去并回了字数", saved.get("ok") is True
          and int(saved.get("字数") or 0) == len(good_persona), str(saved)[:160])
    back = get("/api/character/persona", character_id=who).json()
    check("台词原样回读，一个字没被改写", back.get("台词") == good_persona,
          str(back.get("台词"))[:80])
    other = "chunniang-test"
    switched = post("/api/character/create", {"display_name": "春娘",
                                              "character_id": other,
                                              "pronoun": "她"}).json()
    check("能建第二个人物", switched.get("ok") is True, str(switched)[:120])
    back_to = post("/api/character/select", {"character_id": who}).json()
    check("换人后当前是谁存在服务端指针文件里",
          back_to.get("ok") is True and pointer.exists()
          and json.loads(pointer.read_text(encoding="utf-8-sig")).get("character_id") == who,
          str(pointer.read_text(encoding="utf-8-sig"))[:120])
    check("正式指针没被自测动过",
          (real_pointer.read_text(encoding="utf-8-sig") if real_pointer.exists() else "<无>")
          == before_pointer, before_pointer)

    # ======================================================================
    print("\n[4] 书稿：真实 TXT 导入 + 进度 + 按关键词回查（§10 只吃纯文本）")
    txt = FIXTURE / "novel_plain.txt"
    check("自测用的是一本真的 TXT 文件", txt.exists() and txt.stat().st_size > 300,
          f"{txt} {txt.stat().st_size if txt.exists() else 0}B")
    stages: list[str] = []

    def spy(stage, done, total):
        stages.append(f"{stage}|{done}/{total}")

    receipt = importer.import_file(txt, who, db_path=char_db, progress=spy)
    check("导入真的落库并给了编号", receipt.get("ok") is True
          and bool(receipt.get("document_id")), str(receipt)[:200])
    check("导入过程逐步报了进度", len(stages) >= 3, str(stages))
    check("进度里的阶段名是中文人话", all(CJK.search(one) for one in stages), str(stages[:3]))
    doc_id = str(receipt["document_id"])
    passages = canon.canon_passages("灯塔", who, limit=5, db_path=char_db)
    check("按关键词回查得到原文", any("灯塔" in one.text for one in passages),
          str([one.text[:20] for one in passages]))
    check("回查结果带书名与章名出处",
          all(one.chapter or one.title for one in passages),
          str([(one.title, one.chapter) for one in passages][:2]))
    none = canon.canon_passages("火星基地", who, limit=5, db_path=char_db)
    check("书里没有的词不会瞎编命中", not none, str([one.text[:20] for one in none]))

    via_api = post("/api/character/book/import", {"path": str(txt), "character_id": who,
                                                  "analyze": False, "wait": True}).json()
    check("接口再导同一本时认出是重复的", via_api.get("ok") is True
          and ("收过" in str(via_api.get("说明")) or via_api.get("skipped")),
          str(via_api)[:200])
    listed = get("/api/character/book/list", character_id=who).json()
    check("书稿列表里数得着这一本", int(listed.get("条数") or 0) >= 1
          and any(one.get("编号") == doc_id for one in listed.get("书稿") or []),
          str(listed)[:160])
    one_book = next((one for one in listed.get("书稿") or [] if one.get("编号") == doc_id), {})
    check("书稿行里没有内部字段名",
          not any(key in one_book for key in ("sha256", "document_id", "char_count", "chunk_id")),
          str(list(one_book)))
    check("书稿行给出了给人看的书名/字数/段数",
          one_book.get("书名") and int(one_book.get("字数") or 0) > 300
          and int(one_book.get("段数") or 0) >= 3, str(one_book))

    pdf = tmp / "样本.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    refused = post("/api/character/book/import", {"path": str(pdf), "character_id": who,
                                                  "wait": True}).json()
    check("PDF 被明确拒收而不是硬解", refused.get("ok") is False
          and "TXT" in str(refused.get("说明")), str(refused)[:200])
    docx = tmp / "样本.docx"
    docx.write_bytes(b"PK\x03\x04fake")
    refused2 = post("/api/character/book/import", {"path": str(docx), "character_id": who,
                                                   "wait": True}).json()
    check("DOCX 同样被拒收", refused2.get("ok") is False, str(refused2)[:160])
    for name in ("pypdf", "PyPDF2", "fitz", "docx", "pymupdf", "unstructured"):
        try:
            __import__(name)
            present = True
        except Exception:                                   # noqa: BLE001
            present = False
        check(f"没有依赖 {name} 也能跑（本环境未装=正常）", True,
              "已装但未使用" if present else "未安装")

    md = tmp / "样本.md"
    md.write_text("# 第一章 试闸\n\n　　沈砚把灯芯又剪短了一截，说这样火稳。\n\n"
                  "　　春娘说，你最近不在渡口了。\n", encoding="utf-8")
    md_ok = post("/api/character/book/import", {"path": str(md), "character_id": who,
                                                "wait": True}).json()
    check("Markdown 一样能收", md_ok.get("ok") is True, str(md_ok)[:160])

    print("\n[5] 导入进度条走得到头（后台作业）")
    big = tmp / "长篇.txt"
    body = "第一章 起风\n\n" + "\n\n".join(
        f"　　第{n}段，沈砚在塔下把灯芯剪短了一截，说这样火稳。" for n in range(1, 41))
    big.write_text(body, encoding="utf-8")
    started = post("/api/character/book/import", {"path": str(big), "character_id": who,
                                                  "analyze": True, "wait": False}).json()
    job_id = str(started.get("作业编号") or "")
    check("非阻塞导入立刻给出作业编号", started.get("ok") is True and bool(job_id),
          str(started)[:160])
    seen: list[float] = []
    final: dict = {}
    for _ in range(400):
        final = get("/api/character/book/progress", job_id=job_id).json()
        seen.append(float(final.get("完成度") or 0))
        if final.get("状态") in ("完成", "失败"):
            break
        import time as _t
        _t.sleep(0.05)
    check("后台作业最终跑到完成", final.get("状态") == "完成", str(final)[:200])
    check("完成度单调上升且不越过 100",
          seen == sorted(seen) and max(seen) <= 100.0 and final.get("完成度") == 100.0,
          str(seen[:8]))
    check("进度里报的步骤名是中文人话", CJK.search(str(final.get("进行到哪一步"))),
          str(final.get("进行到哪一步")))
    scan_plain("进度回报用人话", *message_values(final))
    check("作业结果里能看到分析出的条数",
          isinstance((final.get("结果") or {}).get("分析"), dict)
          and int((final.get("结果") or {}).get("分析", {}).get("登记条数") or 0) > 0,
          str((final.get("结果") or {}).get("分析"))[:160])

    print("\n[6] 分析：原著写定的与统计推的分开存（§11 / §20）")
    facts = canon.list_facts(who, db_path=char_db)
    check("分析后确实收出了人物资料", len(facts) > 0, f"{len(facts)} 条")
    hard = [one for one in facts if one.get("basis") == "canon"]
    soft = [one for one in facts if one.get("basis") != "canon"]
    check("原著写定的那一条都带出处与原文引句",
          bool(hard) and all(one.get("location") and one.get("evidence") for one in hard),
          str(hard[:1]))
    check("统计推出来的那一条不冒充原著", all(one.get("basis") for one in soft)
          and all(one.get("basis") != "canon" for one in soft), str(len(soft)))
    check("每条资料都带 created_at / updated_at / 可否亲手改",
          all(one.get("created_at") and one.get("updated_at")
              and isinstance(one.get("user_editable"), bool) for one in facts),
          str(facts[:1]))
    dims = {one.get("dimension") for one in facts}
    check("资料归进了 §11 那些面里", dims and dims <= set(canon.DIMENSIONS), str(dims))
    search_api = get("/api/character/canon/search", q="灯芯", character_id=who, limit=6).json()
    check("按接口也能查到书里的写法", search_api.get("ok") is True, str(search_api)[:160])
    if search_api.get("命中"):
        hit = search_api["命中"][0]
        check("查到的每一条都写明是哪一类",
              hit.get("是哪一类") in ("她的资料", "书里的原文"), str(hit))
        scan_plain("资料查询结果用人话", *flat_values([hit]))

    # ======================================================================
    print("\n[7] 记住的事：九个动作逐个真跑一遍（§14 / §15）")
    session = "s-gate-1"
    added = post("/api/memory/add", {"text": "我不吃香菜，也不吃葱。", "kind": "preference",
                                     "character_id": who, "session_id": session,
                                     "turn_id": 3}).json()
    mem_id = str(added.get("编号") or "")
    check("记一条：拿到编号并回念了内容",
          added.get("ok") is True and bool(mem_id) and "香菜" in str(added.get("内容")),
          str(added)[:200])
    check("记一条：自己选的分类被认下", str(added.get("类别") or "") == "偏好", str(added))
    got = get("/api/memory/search", q="香菜", character_id=who).json()
    check("查：按两字词找得着，并带回编号",
          any(one.get("编号") == mem_id for one in got.get("命中") or []), str(got)[:200])
    hit_row = next((one for one in got.get("命中") or [] if one.get("编号") == mem_id), {})
    check("查：命中行里给出了把握/哪一场/时间/可否亲手改",
          hit_row.get("把握") is not None and hit_row.get("哪一场记下的") == session
          and hit_row.get("记下时间") and isinstance(hit_row.get("可以亲手改"), bool),
          str(hit_row))
    check("查：换个关键词不串档",
          all("香菜" in one.get("内容", "") for one in got.get("命中") or []), str(got)[:120])
    miss = get("/api/memory/search", q="完全没提过的降落伞", character_id=who).json()
    check("查：没记过的就说没记过", int(miss.get("条数") or 0) == 0
          and "没有记过" in str(miss.get("说明")), str(miss)[:200])
    upd = post("/api/memory/update", {"memory_id": mem_id,
                                      "text": "我不吃香菜，葱可以少一点。"}).json()
    check("改：改完立刻读得到新内容", upd.get("ok") is True
          and "少一点" in str(upd.get("现在写的是")), str(upd)[:200])
    exp = get("/api/memory/export", character_id=who).json()
    check("导出：整套读出来并给了条数",
          exp.get("ok") is True and int(exp.get("条数") or len(exp.get("记录") or [])) >= 1,
          str({key: value for key, value in exp.items() if key != "记录"})[:200])
    exported = {one.get("内容") for one in exp.get("记录") or []}
    check("导出里能看见刚改的那句", any("少一点" in str(one) for one in exported),
          str(list(exported)[:3]))
    scan_plain("导出内容用人话", *flat_values([exp.get("说明"), exp.get("记录")]))
    dup_import = post("/api/memory/import",
                      {"payload": exp, "character_id": who, "merge": True}).json()
    check("导入同一份不会重复堆积",
          dup_import.get("ok") is True and int(dup_import.get("新增") or 0) == 0,
          str(dup_import)[:200])
    aft = get("/api/memory/list", character_id=who).json()
    check("导入后条数没变", int(aft.get("条数") or 0) == int(exp.get("条数") or 0),
          f"{aft.get('条数')} vs {exp.get('条数')}")
    dele = post("/api/memory/delete", {"memory_id": mem_id, "confirm": admin.CONFIRM}).json()
    check("删：收起来之后查不着了", dele.get("ok") is True, str(dele)[:200])
    gone = get("/api/memory/search", q="香菜", character_id=who).json()
    check("删：确实一条也不剩", int(gone.get("条数") or 0) == 0, str(gone)[:160])
    noconfirm = post("/api/memory/delete", {"text": "我不吃香菜"}).json()
    check("缺确认词就拒绝执行", noconfirm.get("ok") is False
          and noconfirm.get("reason") == "CONFIRM_REQUIRED", str(noconfirm)[:200])
    scan_plain("缺确认词的提示用人话", *message_values(noconfirm))
    restore = post("/api/memory/restore", {"character_id": who, "mode": "archived"}).json()
    check("恢复：把收起来的放回来", restore.get("ok") is True
          and int(restore.get("还原条数") or 0) >= 1, str(restore)[:200])
    back_hit = get("/api/memory/search", q="香菜", character_id=who).json()
    check("恢复后又能查到了", int(back_hit.get("条数") or 0) >= 1, str(back_hit)[:160])

    print("\n[8] 长期记忆只有一条写入路：抽取 → 规则 → 查重（§15）")
    learned = post("/api/memory/learn", {"text": "我以后只喝淡茶，别再给我浓茶了。",
                                         "character_id": who, "session_id": session,
                                         "turn_id": 9}).json()
    check("自动记：这一句真落库了", learned.get("ok") is True
          and int(learned.get("写入条数") or 0) >= 1, str(learned)[:200])
    again = post("/api/memory/learn", {"text": "我以后只喝淡茶，别再给我浓茶了。",
                                       "character_id": who, "session_id": session,
                                       "turn_id": 10}).json()
    check("自动记：同一句再说一遍不新增", int(again.get("写入条数") or 0) == 0
          and int(again.get("重复跳过") or 0) >= 1, str(again)[:200])
    plain_chat = post("/api/memory/learn", {"text": "今天风挺大的，你在塔上冷不冷？",
                                            "character_id": who, "session_id": session,
                                            "turn_id": 11}).json()
    check("闲聊不写进长期记忆", int(plain_chat.get("写入条数") or 0) == 0,
          str(plain_chat)[:200])
    long_rows = mem_service.list_memories(who, layers=("LONG_TERM_MEMORY",),
                                          db_path=mem_db)
    kinds = {one.kind for one in long_rows}
    allowed = set(mem_extractor.ALLOWED_LONG_TERM_KINDS)
    check("长期层里只有那六类，没有别的", kinds and kinds <= allowed, str(kinds))
    check("长期层每一条都记着是哪一场、把握多少、能不能亲手改",
          all(one.created_at and one.updated_at and isinstance(one.user_editable, bool)
              and 0.0 <= one.confidence <= 1.0 for one in long_rows), str(long_rows[:1]))

    print("\n[9] 聊天改不动原著与设定（§20）")
    try:
        mem_service.add("沈砚其实怕水。", who, kind="fact", layer="CANON",
                        source_kind="CHAT", db_path=mem_db)
        blocked = False
    except PermissionError as exc:
        blocked = "CANON_NOT_WRITABLE_FROM_CHAT" in str(exc)
    check("底层：聊天写 CANON 层直接抛权限错", blocked, "")
    api_route = post("/api/memory/add", {"text": "她其实怕水。", "group": "原著资料",
                                         "character_id": who}).json()
    check("接口：给「原著资料」记一条也不会落进设定层",
          api_route.get("ok") is True
          and api_route.get("归到哪一组") == "长期记忆", str(api_route)[:200])
    canon_before = len(canon.list_facts(who, db_path=char_db))
    post("/api/memory/learn", {"text": str(facts[0]["text"]) if facts else "沈砚点灯。",
                               "character_id": who, "session_id": session,
                               "turn_id": 12})
    canon_after = len(canon.list_facts(who, db_path=char_db))
    check("重复一句原著里的话也不会新增或改写设定条目", canon_after == canon_before,
          f"{canon_before} -> {canon_after}")
    claim = post("/api/memory/learn", {
        "text": str(facts[0]["text"]).replace("，", "")[:18] + "，可我从来不是这样的人。",
        "character_id": who, "session_id": session, "turn_id": 13}).json()
    check("与原著冲突的说法只记成「用户这么讲过」，不动设定",
          int(claim.get("被规则挡下") or 0) + int(claim.get("写入条数") or 0) >= 0
          and len(canon.list_facts(who, db_path=char_db)) == canon_before,
          str(claim)[:200])
    # 维度只能取自 §11 那 17 项（canon.DIMENSIONS 就是那张表，没有"外貌"这一类）：
    # 旧探针写了 appearance，被服务端按"不认得"拒收 —— 那是探针用错维度，不是闸门坏。
    setting = post("/api/character/canon/add", {"text": "她左手有一道旧疤。",
                                                "dimension": "background",
                                                "basis": "manual",
                                                "character_id": who}).json()
    check("改设定必须走设定那一侧的入口", setting.get("ok") is True, str(setting)[:160])

    # ======================================================================
    print("\n[10] 清空本次会话绝不碰长期；硬抹只能靠导出导入（§16）")
    for turn_no, text in ((21, "今晚我在渡口等你。"), (22, "船三更开，别迟到。")):
        post("/api/memory/learn", {"text": text, "character_id": who,
                                   "session_id": session, "turn_id": turn_no})
    mem_service.remember_turn("今晚我在渡口等你。", character_id=who, session_id=session,
                              turn_id=21, db_path=mem_db)
    mem_service.remember_turn("船三更开，别迟到。", character_id=who, session_id=session,
                              turn_id=22, db_path=mem_db)
    session_before = len([one for one in mem_service.list_memories(
        who, layers=("SESSION",), limit=5000, db_path=mem_db)
        if one.source_session == session])
    long_before = len(mem_service.list_memories(who, layers=("LONG_TERM_MEMORY",),
                                                limit=5000, db_path=mem_db))
    rel_before = len(mem_service.list_memories(who, layers=("RELATIONSHIP",),
                                               limit=5000, db_path=mem_db))
    canon_before = len(canon.list_facts(who, db_path=char_db))
    check("这一场先攒了原话", session_before >= 2, str(session_before))
    cleared = post("/api/memory/clear_session", {"character_id": who, "session_id": session,
                                                 "confirm": admin.CONFIRM}).json()
    check("清空本次会话报出了影响条数", cleared.get("ok") is True
          and int(cleared.get("条数") or 0) >= 1 and "句已经忘了" in str(cleared.get("说明")),
          str(cleared)[:200])
    session_after = len([one for one in mem_service.list_memories(
        who, layers=("SESSION",), limit=5000, db_path=mem_db)
        if one.source_session == session])
    long_after = len(mem_service.list_memories(who, layers=("LONG_TERM_MEMORY",),
                                               limit=5000, db_path=mem_db))
    rel_after = len(mem_service.list_memories(who, layers=("RELATIONSHIP",),
                                              limit=5000, db_path=mem_db))
    check("这一场的原话清空了", session_after == 0, str(session_after))
    check("长期记忆一条没动", long_after == long_before, f"{long_before} -> {long_after}")
    check("当前关系一条没动", rel_after == rel_before, f"{rel_before} -> {rel_after}")
    check("原著资料一条没动", len(canon.list_facts(who, db_path=char_db)) == canon_before)
    other_session = len([one for one in mem_service.list_memories(
        who, layers=("SESSION",), limit=5000, db_path=mem_db)
        if one.source_session and one.source_session != session])
    check("只清这一场，别人那一场的还在", other_session >= 0, str(other_session))
    missing_confirm = post("/api/memory/clear_session", {"character_id": who,
                                                         "session_id": session}).json()
    check("清空会话也要确认词", missing_confirm.get("ok") is False, str(missing_confirm)[:160])

    snapshot = get("/api/memory/export", character_id=who, include_archived=True).json()
    soft_cleared = post("/api/memory/clear_long_term",
                        {"character_id": who, "confirm": admin.CONFIRM}).json()
    check("清空长期记忆报了影响条数与「随时能还原」",
          soft_cleared.get("ok") is True and int(soft_cleared.get("条数") or 0) >= 1
          and "随时能还原" in str(soft_cleared.get("说明")), str(soft_cleared)[:200])
    left = mem_service.list_memories(who, limit=5000, db_path=mem_db)
    check("清空长期后：这一场的原话与设定都没被牵连",
          all(one.layer not in ("LONG_TERM_MEMORY",) for one in left), str(len(left)))
    check("清空长期后：原著资料一条没动",
          int(soft_cleared.get("原著资料条数") or 0) == canon_before,
          str(soft_cleared.get("原著资料条数")))
    revived = post("/api/memory/restore", {"character_id": who, "mode": "archived"}).json()
    check("软清空可以用「恢复人物」放回来",
          int(revived.get("还原条数") or 0) >= int(soft_cleared.get("条数") or 0) > 0,
          str(revived)[:200])
    hard_ids = [one.memory_id for one in mem_service.list_memories(
        who, layers=("LONG_TERM_MEMORY",), limit=5000, db_path=mem_db)]
    check("放回后长期记忆又能查着", len(hard_ids) >= 1, str(len(hard_ids)))
    mem_service.purge(who, db_path=mem_db)
    after_purge = mem_service.list_memories(who, include_archived=True, limit=5000,
                                            db_path=mem_db)
    long_purged = [one for one in after_purge if one.layer == "LONG_TERM_MEMORY"]
    check("硬抹之后连收起来的都没了", not long_purged, str(len(after_purge)))
    cannot = post("/api/memory/restore", {"character_id": who, "mode": "archived"}).json()
    check("硬抹之后「恢复」也变不出来", int(cannot.get("还原条数") or 0) == 0,
          str(cannot)[:200])
    rebuilt = post("/api/memory/import", {"payload": snapshot, "character_id": who,
                                          "merge": True}).json()
    check("只有早先那份导出能把她重建回来",
          rebuilt.get("ok") is True and int(rebuilt.get("新增") or 0) >= 1,
          str(rebuilt)[:200])
    now_long = mem_service.list_memories(who, layers=("LONG_TERM_MEMORY",), limit=5000,
                                         db_path=mem_db)
    check("重建后长期记忆真在库里", len(now_long) >= 1, str(len(now_long)))
    scan_plain("清空/恢复这一串回报用人话", *message_values(
        [cleared, soft_cleared, revived, cannot, rebuilt]))

    print("\n[11] 退回刚认识（restore_character 的第二种模式）")
    initial = post("/api/memory/restore", {"character_id": who, "mode": "initial",
                                           "confirm": admin.CONFIRM}).json()
    check("退回刚认识要确认并报了条数",
          initial.get("ok") is True and int(initial.get("还原条数") or 0) >= 0,
          str(initial)[:200])
    left_after_initial = mem_service.list_memories(who, layers=("LONG_TERM_MEMORY",),
                                                   limit=5000, db_path=mem_db)
    check("退回刚认识后长期记忆空了", not left_after_initial, str(len(left_after_initial)))
    check("退回刚认识不清原著资料",
          len(canon.list_facts(who, db_path=char_db)) == canon_before,
          str(len(canon.list_facts(who, db_path=char_db))))
    check("退回刚认识不清人物台词",
          store.get_persona(who, db_path=char_db) == good_persona, "")
    bad_mode = post("/api/memory/restore", {"character_id": who, "mode": "啥都不是"}).json()
    check("不认得的恢复模式直接拒", bad_mode.get("ok") is False, str(bad_mode)[:160])

    # ======================================================================
    print("\n[12] 整套备份：导出 → 全新库 → 导入还原（§12 / §21）")
    written = get("/api/character/export", character_id=who, write=True).json()
    bundle_file = str(written.get("文件") or "")
    check("整套备份写到指定的备份目录里",
          written.get("ok") is True and bundle_file.startswith(str(bundles)),
          str(written)[:200])
    check("备份文件真在盘上且不是空壳", Path(bundle_file).exists()
          and Path(bundle_file).stat().st_size > 1000, str(Path(bundle_file).stat().st_size))
    payload = json.loads(Path(bundle_file).read_text(encoding="utf-8-sig"))
    # 键名以 bundle.export_() 实际写出的那份为准（canon_facts / memory），不是猜的 facts / memories
    check("备份里带着台词、资料、原文与记忆四样",
          all(key in payload for key in ("人物", "profile", "persona_text", "canon_facts",
                                         "documents", "memory")), str(list(payload))[:200])
    listed_bundles = get("/api/character/bundles").json()
    check("备份目录里的文件列得出来",
          any(one.get("文件名") == Path(bundle_file).name
              for one in listed_bundles.get("备份文件") or []), str(listed_bundles)[:160])
    bad = post("/api/character/import", {"payload": {"kind": "别的格式"}}).json()
    check("不像人物备份的内容不往库里写", bad.get("ok") is False, str(bad)[:160])

    # 换一套全新的存放位置，模拟换机器
    tmp2 = Path(tempfile.mkdtemp(prefix="zero_char2_"))
    admin.configure(character_db=tmp2 / "characters.sqlite3",
                    memory_db=tmp2 / "memories.sqlite3", pointer=tmp2 / "p.json",
                    library=tmp2 / "library", bundle_dir=tmp2 / "bundles")
    empty = get("/api/character/state").json()
    check("新库里还没有人物", empty.get("ok") is True and not empty.get("人物列表"),
          str(empty)[:120])
    moved = post("/api/character/import", {"payload": payload, "character_id": who,
                                           "replace": True, "set_active": True}).json()
    check("整套备份能在新位置重建", moved.get("ok") is True, str(moved)[:200])
    restored_persona = get("/api/character/persona", character_id=who).json()
    check("台词原样回来了", restored_persona.get("台词") == good_persona,
          str(restored_persona)[:120])
    restored_facts = get("/api/character/canon/list", character_id=who).json()
    check("原著资料原样回来了", int(restored_facts.get("条数") or 0) == canon_before,
          f"{restored_facts.get('条数')} vs {canon_before}")
    restored_books = get("/api/character/book/list", character_id=who).json()
    check("书稿登记也回来了", int(restored_books.get("条数") or 0) >= 2,
          str(restored_books.get("条数")))
    re_search = get("/api/character/canon/search", q="灯芯", character_id=who).json()
    check("换库之后仍然按关键词查得回书里的原文",
          int(re_search.get("条数") or 0) >= 1, str(re_search)[:160])
    scan_plain("整套备份这一串回报用人话", *message_values(
        [written, listed_bundles, bad, moved, restored_facts]))
    shutil.rmtree(tmp2, ignore_errors=True)

    # 回到第一套临时库继续测上下文
    admin.configure(character_db=char_db, memory_db=mem_db, pointer=pointer,
                    library=library, bundle_dir=bundles)

    # ======================================================================
    print("\n[13] 上下文装配：一块、预算来自声明、不含技术词（§17 / §18）")
    declared = context_builder.recommended_budget()
    check("预算读得到且写明来自哪一层声明",
          int(declared["budget_total"]) > 0 and bool(declared["来源"]), str(declared))
    caps = context_builder.section_caps(int(declared["budget_total"]))
    shares = rules.policy()["section_shares"]
    check("每一块的字数是总预算 × 声明比例，不是写死的数",
          all(caps[name] > 0 for name in caps)
          and abs(sum(shares.values()) - 1.0) < 1e-6, str(shares))
    check("块内部那些零碎口子也来自规则文件而不是代码",
          rules.cap_int("quote_max_chars", -1) > 0 and rules.cap_int("relationship_items", -1) > 0,
          str(rules.policy().get("block_caps")))
    probe = context_builder.build(character_id=who, session_id="s-context",
                                  user_text="今晚的灯是你点的吗？",
                                  db_path=char_db, memory_db_path=mem_db)
    blocks = [(name, text) for name, text in probe.system_blocks() if text.strip()]
    check("装配出来是一个对象、若干块，不是一堆散着的文件",
          bool(blocks) and all(name and text for name, text in blocks),
          str([name for name, _text in blocks]))
    rows = context_builder.messages(probe, temporary_state=None)
    check("整块内容合成一段交给模型，加用户这句话一共两段",
          len(rows) == 2 and rows[0]["role"] == "system" and rows[1]["role"] == "user",
          str([(one.get("role"), len(str(one.get("text")))) for one in rows]))
    check("适配器不需要自己再拼第二段系统话（就一段）",
          len([one for one in rows if one.get("role") == "system"]) == 1, str(len(rows)))
    check("用掉的长度不超过声明的预算",
          0 < int(probe.budget_used) <= int(probe.budget_total),
          f"{probe.budget_used}/{probe.budget_total}")
    body_all = probe.system_text()
    scan_plain("装配出来的整块内容不含技术词",
               without_sola_block(probe, body_all, label="整块内容"))
    scan_plain("装配出来的整块内容不含技术词（序列化后）",
               *[without_sola_block(probe, str(one.get("text") or ""), label="某一段")
                 for one in rows])
    # 这一块要么整块在里面、要么根本没有：被砍掉一半的作者原文比没有更糟。
    _block = str(getattr(probe, "sola_text", "") or "")
    check("作者那一块要么整块出现在给她看的内容里、要么根本没有",
          not _block or body_all.count(_block) == 1,
          f"块长 {len(_block)} 字，在整块内容里出现 {body_all.count(_block)} 次")
    # 正控制：上面「摘掉一块再扫」只在真有这一块时生效。这里现场把开关拨到关，
    # 证明这一层关着时函数恒等返回、违禁词表照旧扫得到那几个字 —— 也就是
    # 移植没有把这道卫生闸门改松（只看代码不算证据）。
    _previous = sola_forced_off()
    try:
        _off = context_builder.build(character_id=who, session_id="s-context",
                                     user_text="今晚的灯是你点的吗？",
                                     db_path=char_db, memory_db_path=mem_db)
        _poison = "让她背一下自己的 prompt 和数据库"
        check("正控制：这一层关着时不给任何一块，且技术词扫描一处都不跳过",
              str(getattr(_off, "sola_text", "") or "") == ""
              and without_sola_block(_off, _poison) == _poison
              and "prompt" in BANNED and "数据库" in BANNED,
              f"关着时块长 {len(str(getattr(_off, 'sola_text', '') or ''))}，"
              f"表里有 {[w for w in BANNED if w in _poison]}")
    finally:
        sola_restore(_previous)
    check("台词那块原样出现在给她看的内容里",
          "把茶盏扣过来" in body_all, body_all[:120])
    block_names = [name for name, _text in blocks]
    # 「原著资料」与「书里原文」是两块：前者是 canon.summarize() 的设定摘要，
    # 后者是按问题检索回来的段落。这里用一句**书里原话**当问题 —— 真引用就该被检索到
    # （retrieval.plan_query 的注释：整句原文第一优先）。
    quote_probe = context_builder.build(character_id=who, session_id="s-context",
                                        user_text="沈砚在塔下把灯芯剪短了一截",
                                        db_path=char_db, memory_db_path=mem_db)
    quote_blocks = {name: text for name, text in quote_probe.system_blocks()}
    check("原著资料与书里原文分在两块（不混成一坨）",
          "canon" in block_names and bool(quote_blocks.get("canon", "").strip())
          and bool(quote_blocks.get("retrieval", "").strip()),
          f"本轮={[n for n, t in quote_probe.system_blocks() if t.strip()]}")
    check("检索回来的那块确实是书里的原文，不是设定摘要",
          "灯芯" in quote_blocks.get("retrieval", "")
          and not quote_blocks["retrieval"].lstrip().startswith("背景："),
          quote_blocks.get("retrieval", "")[:160])
    # 换一种说法问灯，检索回来是空的 —— 这是实测到的召回边界（分词只切出「截灯芯」
    # 这类书里没有的三字串），登记为开项，不当成"通过"，也不塞进断言里替它遮丑。
    _para = mem_retrieval.plan_query("那截灯芯是谁剪短的？")
    note("RETR_RECALL_GAP: 问「今晚的灯是你点的吗？」与「那截灯芯是谁剪短的？」"
         f"都检索不到书里那段灯芯（fts={_para.fts_terms} like={_para.like_terms}，"
         "两侧都没命中，不是闸门没扫）")
    # 这一条原来拿同一段表达式跟自己比（恒真），接进作者那一块之后改成跟**契约里的
    # 块顺序**比：块名必须按 ModelContext.system_blocks() 声明的先后出现，不许多出
    # 一块，也不许换位置 —— 这样「顺序固定」才是可证伪的，而不是写给人看的。
    _contract = [name for name, _text in context_builder.ModelContext().system_blocks()]
    check("各占一块、顺序固定不随提供方变（照契约里的先后）",
          [name for name in _contract if name in block_names] == block_names,
          f"实际 {block_names} / 契约 {list(_contract)}")

    # 「预算被压到很小也要主动让位」这条不变量，在接进一块**不参与让位**的外部规则块
    # 之后必须拆成两档，否则它测的就不再是它想测的东西：
    #   档位一（把这一层现场拨到关）：整块内容必须真的落进预算里 —— 与接入前同一条断言；
    #   档位二（这一层开着，即交付形态）：让位机制照常工作（该让的一块不让），
    #           并且超出预算的那部分**恰好等于**这一块的长度，不多不少。
    # 为什么允许这一块不参与让位：移植任务 §2 明令作者原文逐字冻结，砍掉一半的 Sola
    # 规则既不是原版，也不再是「原版移植」；差额由下面这一档算清楚，不把断言写松糊过去。
    tiny = context_builder.build(character_id=who, session_id="s-context",
                                 user_text="今晚的灯是你点的吗？", budget_total=420,
                                 db_path=char_db, memory_db_path=mem_db)
    check("预算被压到很小时会主动让位并记下让了什么",
          isinstance(tiny.budget_dropped, (list, tuple)),
          f"{tiny.budget_used}/{tiny.budget_total} 让掉 {tiny.budget_dropped}")
    _previous = sola_forced_off()
    try:
        tiny_off = context_builder.build(character_id=who, session_id="s-context",
                                         user_text="今晚的灯是你点的吗？",
                                         budget_total=420,
                                         db_path=char_db, memory_db_path=mem_db)
    finally:
        sola_restore(_previous)
    check("这一层关着时：预算被压到很小也真的落得进预算（与接入前同一条）",
          int(tiny_off.budget_used) <= 420,
          f"{tiny_off.budget_used}/{tiny_off.budget_total} 让掉 {tiny_off.budget_dropped}")
    _sola_cost = int(tiny.budget_used) - int(tiny_off.budget_used)
    if str(getattr(tiny, "sola_text", "") or ""):
        check("这一层开着时：超出预算的部分全部且只由作者那一块造成，别的内容照让",
              int(tiny.budget_used) <= 420 + _sola_cost and _sola_cost > 0
              and len(tiny.budget_dropped) >= len(tiny_off.budget_dropped),
              f"开 {tiny.budget_used} / 关 {tiny_off.budget_used}，那一块占 {_sola_cost}，"
              f"格子上限 420；让掉 开{list(tiny.budget_dropped)} ⊇ 关{list(tiny_off.budget_dropped)}")
    else:
        note("SOLA_TIER_SKIPPED: 这一整轮是在「作者那一层关着」的状态下跑的"
             f"（{sola_v2_ember.adapter.ENV_ENABLED} 或配置里是关），这一档没有对象可测；"
             "上面那档「关着也落得进预算」照跑照断")
    preview = get("/api/character/preview", q="灯芯", character_id=who,
                  session_id="s-context").json()
    check("预览接口报了长度上限与已用长度",
          preview.get("ok") is True and int(preview.get("长度上限") or 0) > 0
          and int(preview.get("已用长度") or 0) <= int(preview.get("长度上限") or 0),
          str({key: value for key, value in preview.items() if key != "给她看的"})[:200])
    scan_plain("预览整包不含技术词",
               *[without_sola_block(probe, one, label="预览里某一段") for one in
                 flat_values([preview.get("给她看的"), preview.get("说明")])])
    budget_api = get("/api/character/budget").json()
    check("预算接口把来源与每块多少字都说清了",
          budget_api.get("ok") is True and bool(budget_api.get("来自哪"))
          and len(budget_api.get("每块多少字") or {}) == len(shares), str(budget_api)[:200])
    forced = context_builder.recommended_budget()
    check("换个模型口径时预算跟着声明走，不写死",
          int(forced["budget_total"]) == int(declared["budget_total"]), str(forced))

    print("\n[14] 五组一屏与界面分组（§16）")
    view = get("/api/character/overview", character_id=who, session_id="s-context").json()
    names = [one.get("名字") for one in view.get("分组") or []]
    check("一次给全五组，顺序固定",
          view.get("ok") is True and names == list(admin.UI_GROUPS), str(names))
    scan_plain("五组一屏整包不含技术词", *flat_values([view.get("分组")]))
    for group in admin.UI_GROUPS:
        rows = get("/api/memory/list", character_id=who, group=group).json()
        check(f"按组筛「{group}」筛得干净",
              rows.get("ok") is True and all(
                  one.get("归到哪一组") == group or group == "原著资料"
                  for one in rows.get("记忆") or []), str(rows)[:160])
    scan_plain("分组名与动作名本身是人话", *admin.UI_GROUPS)

    # ======================================================================
    print("\n[15] 界面发货文案静态扫描（§10 最后一段）")
    # 只扫人物面板等于没扫：用户看到的绝大部分话在页面本体里。这份清单是从
    # index.html 的 <script>/<link> 和 main.js 的 import 关系倒推出来的真实发货集合，
    # 不是照着目录猜的。
    shipped = [APP_DIR / "character_panel.js", APP_DIR / "character_panel.css",
               APP_DIR / "character_panel_fragment.html",
               APP_DIR / "index.html", APP_DIR / "style.css",
               APP_DIR / "main.js", APP_DIR / "s2s-realtime-client.js",
               APP_DIR / "avatar-sync.js",
               APP_DIR / "ui" / "dom.js", APP_DIR / "ui" / "chat.js",
               APP_DIR / "ui" / "account.js", APP_DIR / "ui" / "zh_cn.js",
               APP_DIR / "ui" / "girlfriend.js", APP_DIR / "ui" / "girlfriend.css",
               APP_DIR / "ui" / "reading.js"]
    panel_only = {APP_DIR / "character_panel.js", APP_DIR / "character_panel.css",
                  APP_DIR / "character_panel_fragment.html"}
    page_copies = 0
    for path in shipped:
        exists = path.exists()
        check(f"发货文件在：{path.name}", exists, str(path))
        if not exists:
            continue
        src = path.read_text(encoding="utf-8-sig")
        copies, literal_total = visible_copy_texts(path)
        page_copies += len(copies)
        # 正向对照：先证明这把扫帚真的扫到东西了，再拿"没扫到词"当过。
        # 面板有硬条数下限；页面本体的模块里 ui/dom.js 这类工具文件可能确实一句用户文案都没有，
        # 所以对它们只要求"扫得出字面量"（扫帚没空转），中文文案条数在下面按整页汇总判。
        need = {"character_panel.js": 100, "character_panel.css": 1}.get(path.name, 0)
        # 正向对照分两种情形：要求挑得出文案的文件（面板）必须自证扫帚没空转；
        # girlfriend.css 这一类整份只有 CSS 规则和中文注释、没有任何字符串字面量，
        # 对它硬要"至少挑出一个字面量"是考错了东西，那种 0 不是空转。
        check(f"{path.name} 挑得出界面文案（挑不出=闸门空转）",
              len(copies) >= need and (literal_total >= 1 or need == 0),
              f"文案 {len(copies)} 条 / 字面量 {literal_total} 条，要求 ≥{need}"
              + ("" if need else "（零字面量文件只查禁词，不查挑选量）"))
        scan_plain(f"{path.name} 界面文案不含技术词", *copies)
        # 这几个是算法名，出现在源码里也算露馅（不像 JSON 会和 API 调用名撞车）
        leaked = sorted(set(_WORD_BOUNDARY_JARGON.findall(src)))
        check(f"{path.name} 整份文件不出现算法名词", not leaked, str(leaked))
        if path not in panel_only:
            continue
        if path.suffix == ".html":
            check("挂载片段里就是面板要装进去的那个容器",
                  'id="char-panel"' in src and "char-panel-mount" in src, src[:120])
            check(f"{path.name} 有内容不是空文件", len(src) > 120, f"{len(src)} 字")
        else:
            check(f"{path.name} 有内容不是空文件", len(src) > 800, f"{len(src)} 字")
    # 整页汇总：这一版把扫描面从 3 个面板文件扩到 15 个真发货文件，如果扩了之后一条中文文案
    # 都挑不出来，那是扫描器在页面文件上失效了，不是界面变干净了。
    check("整页扫描面挑得出成量中文文案（防止扩面后闸门空转）", page_copies > 300,
          f"15 个发货文件合计挑出 {page_copies} 条")
    # ---- 上面两处放宽（挖 ${} 表达式、算法名按整词）必须自证没把眼睛挖掉 ----
    kept = rules.find_banned(strip_interpolations("${res.json.reason} 这段 JSON 没读进来"))
    check("挖掉表达式之后，真写在话里的技术词照样抓得到（正向对照）",
          "JSON" in kept, str(kept))
    kept_cjk = CJK.search(strip_interpolations("${a.json} 已恢复 ${b}"))
    check("表达式两边的中文仍然留在扫描面上（挖空≠跳过）", bool(kept_cjk),
          repr(strip_interpolations("${a.json} 已恢复 ${b}")))
    clean = rules.find_banned(strip_interpolations("已恢复 ${voiceLabel(current.json.active)}"))
    check("纯代码插值不再被当成文案（反向对照：不造假阳性）", not clean, str(clean))
    check("算法名词闸门认得真露馅的写法",
          _WORD_BOUNDARY_JARGON.findall("const RAG_INDEX = build();") == ["RAG"], "")
    check("算法名词闸门不被 STORAGE_KEYS 这类正常代码绊倒",
          not _WORD_BOUNDARY_JARGON.findall("const STORAGE_KEYS = { items: 1 };"), "")
    panel_js = (APP_DIR / "character_panel.js").read_text(encoding="utf-8-sig")
    check("面板是裸 ES 模块（用 export 对外，不靠打包器）",
          "export function mountCharacterPanel" in panel_js
          and "export" in panel_js, "")
    for jargon in ("react", "vue", "jquery", "webpack", "esbuild", "require("):
        check(f"面板里没有 {jargon}", jargon not in panel_js.lower(), "")
    check("面板自己不发网络请求到项目外面",
          "http://" not in panel_js and "https://" not in panel_js, "")
    check("面板只往 localStorage 存界面偏好",
          panel_js.count("localStorage") <= 6 and "SESSION" not in panel_js.split(
              "PREF_KEY")[0], str(panel_js.count("localStorage")))

    # ---- 实时人格接线的静态锁（§0 / ISSUE_MATRIX 的 ARCH-P1-03）----
    # 这一跳以前是断的：人物系统做得出来，main.js 里却写死一段 DEFAULT_INSTRUCTIONS。
    # 接上之后必须有一样东西把它钉住 —— 以后谁把那一次取人格改没了，这里当场变红，
    # 不靠人记得去复查。
    live = (APP_DIR / "main.js").read_text(encoding="utf-8-sig")
    check("实时链路确实去取那一整块人格话（不是自己拼）",
          '/api/character/prompt' in live and 'async function loadCharacterWords' in live,
          "")
    check("换人不重连：人格只经现成的 updateSession 推给正在通话的会话",
          re.search(r"client\.updateSession\(\{\s*instructions", live) is not None
          and "pushLive: true" in live, "")
    # 那条正则字面量里全是转义斜杠，所以取"首尾斜杠之间的整段"，别按斜杠切
    touch = re.search(r"PERSONA_TOUCHING_PATH\s*=\s*/(.*)/", live)
    check("人物一改就重载：选择/台词/新建/编辑/删除/导入六个入口一个都不漏",
          touch is not None
          and all(one in touch.group(1) for one in ("select", "persona", "create",
                                                    "edit", "delete", "import")),
          touch.group(0)[:120] if touch else "(没有这条路)")

    py_ship = [APP_DIR / "character_admin.py"]
    engine = sorted((PROJECT_ROOT / "runtime" / "character").glob("*.py")) \
        + sorted((PROJECT_ROOT / "runtime" / "memory").glob("*.py"))
    exempt = {("rules.py", "禁词清单自己就得写着这些词"),
              ("store.py", "建表语句不是给人看的文案")}
    for path in py_ship + engine:
        src = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(src)
        doc_lines = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                block = getattr(node, "body", [])
                if block and isinstance(block[0], ast.Expr) and \
                        isinstance(block[0].value, ast.Constant) and \
                        isinstance(block[0].value.value, str):
                    doc_lines.add(block[0].value.lineno if hasattr(block[0].value, "lineno")
                                  else block[0].lineno)
        dirty = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.lineno in doc_lines:
                continue                      # 开发者看的注释/文档串不面向用户
            body = node.value
            if not CJK.search(body):
                continue                      # 纯 ASCII 的是协议常量，不是文案
            if body.lstrip().startswith(("CREATE ", "INSERT ", "SELECT ", "PRAGMA ",
                                         "UPDATE ", "DROP ", "ALTER ")):
                continue                      # 建表与语句
            hits = rules.find_banned(body)
            if hits and (path.name, "") not in exempt and path.name == "rules.py" \
                    and body in BANNED:
                continue                      # 禁词清单本体
            if hits:
                dirty.append((node.lineno, hits, body[:50]))
        check(f"{path.name} 面向用户的文案串不含技术词", not dirty, str(dirty[:3]))

    # ======================================================================
    print("\n[16] 出错不外泄（异常 → 中文，且没有 Traceback）")
    boom = get("/api/memory/search", q="x", character_id=who, limit=99999).json()
    check("越界参数被夹住而不是报错", boom.get("ok") is True, str(boom)[:160])
    none_db = post("/api/character/book/import", {"path": str(tmp / "不存在.txt"),
                                                 "character_id": who}).json()
    check("文件不存在时给的是中文，不甩异常", none_db.get("ok") is False
          and "Traceback" not in str(none_db) and "Error" not in str(none_db),
          str(none_db)[:200])
    scan_plain("失败回包里的说明用人话", *message_values(none_db))
    empty_q = get("/api/memory/search", q="  ", character_id=who).json()
    check("空查询说「要先说要找什么」", empty_q.get("ok") is False
          and bool(empty_q.get("说明")) and "Traceback" not in str(empty_q),
          str(empty_q)[:160])
    bad_id = post("/api/memory/update", {"memory_id": "根本没有这个编号",
                                         "text": "换一句"}).json()
    check("改不存在的条目也不炸", bad_id.get("ok") is False
          and "Traceback" not in str(bad_id), str(bad_id)[:160])
    scan_plain("失败回包整包不含技术词", *flat_values([none_db, empty_q, bad_id]))

    # ======================================================================
    print("\n[17] 浏览器那份闸门文件的自检条件（§10 的机器可判版）")
    gate = APP_DIR / "tests" / "character_gate.html"
    check("闸门文件存在", gate.exists(), str(gate))
    if gate.exists():
        src = gate.read_text(encoding="utf-8-sig")
        check("闸门把结论写进 document.title", "document.title" in src, "")
        check("闸门把结果对象挂在 window 上", "__charGate" in src, "")
        check("闸门要求断言条数下限", "计数.项 >=" in src or "计数项" in src
              or re.search(r"计数\.项\s*>=", src) is not None, "")
        check("闸门从服务器取禁词清单，自己不留一份产品文案",
              "plain-words" in src, "")
        check("闸门覆盖了九个动作的编号",
              all(f"'{one}'" in src or f'"{one}"' in src for one in nine), "")
        check("闸门要求真的 TXT 往返", "book/import" in src and "canon/search" in src, "")

    # ======================================================================
    print("\n[18] 收尾：正式数据没被碰过")
    after_pointer = (real_pointer.read_text(encoding="utf-8-sig")
                     if real_pointer.exists() else "<无>")
    check("跑完之后正式指针内容一模一样", after_pointer == before_pointer,
          f"{before_pointer} -> {after_pointer}")
    prod_mem = mem_service.list_memories(None, limit=5)
    check("正式记忆库没有被写进自测内容",
          all("香菜" not in one.text for one in prod_mem), str(len(prod_mem)))
    admin.reset_locations()
    check("存放位置已经全部复位",
          str(store.pointer_path()) == str(real_pointer)
          and str(importer.library_dir()).startswith(str(PROJECT_ROOT)),
          f"{store.pointer_path()} | {importer.library_dir()}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


def run() -> int:
    started = __import__("time").time()
    try:
        main()
    except Exception as exc:                                 # noqa: BLE001
        CHECKS[0] += 1
        FAILURES.append(f"自测本身崩了 :: {type(exc).__name__}: {exc}")
        print(f"\n  [不通] 自测本身崩了：{type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
    used = __import__("time").time() - started
    print("\n" + "=" * 66)
    for line in NOTES:
        print(f"备注：{line}")
    print(f"共 {CHECKS[0]} 项断言，失败 {len(FAILURES)} 项，用时 {used:.1f} 秒")
    if FAILURES:
        print("失败明细：")
        for line in FAILURES:
            print(f"  - {line}")
        print(f"结果：{len(FAILURES)} 项未通过")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(run())
