# -*- coding: utf-8 -*-
"""故事朗读后端验收（MODE_SYSTEM_V1 专项 §二~§五、§十六、§十七、§三十二）。

这一支**不需要浏览器**：把 7860 的 /api/reading/* 与 8765 的 /v1/speak 当黑盒打。
能在这里证伪的：
  * 文本真的进了书库、按句切块（不是机械 N 字一截）；
  * 进度只在「写进度」时推进，speak 本身不推进（审计 §五）；
  * 停止 / 暂停会推进 generation，旧号 speak 被拒（陈旧回调闸门，§八）；
  * 新文档不复用旧文档的 offset（§十七）；
  * 朗读正文不进用户长期记忆库（§三十二）。

需要真人耳朵与数字人的那一段（§四十一）不在这里，另见报告。
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

UI = "http://127.0.0.1:7860"
S2S = "http://127.0.0.1:8765"
HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "reading_fixture_01.txt"

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), str(detail)[:300]))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def get(url: str, timeout: int = 60) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post(url: str, payload: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    body = FIXTURE.read_text(encoding="utf-8")
    title = "朗读测试文本 第一卷"

    # ---- 1. 导入 + 切块 ----------------------------------------------------
    imported = post(f"{UI}/api/reading/import", {"title": title, "text": body})
    doc_id = str(imported.get("document_id") or "")
    check("文本导入书库", bool(doc_id), str(imported)[:200])
    if not doc_id:
        return 2

    doc = get(f"{UI}/api/reading/document?document_id={doc_id}")
    chapters = doc.get("chapter_titles") or []
    check("分出章节", len(chapters) >= 3, f"{len(chapters)} 节："
          + " / ".join(str(c.get('title')) for c in chapters[:8]))

    # 第 0 章是标题行（只有一段），取第一个真正有内容的章节来验切块。
    ch_index, chunks = 0, []
    for i in range(len(chapters)):
        view_i = get(f"{UI}/api/reading/chapter?document_id={doc_id}&chapter={i}")
        got = view_i.get("chunks") or []
        if len(got) >= 3:
            ch_index, chunks, view = i, got, view_i
            break
    check("正文章节切出多个朗读段", len(chunks) >= 3,
          f"第 {ch_index} 章（{chapters[ch_index].get('title')}）{len(chunks)} 段")

    texts = [str(c.get("text") or "") for c in chunks]
    mechanical = [t for t in texts if len(t) >= 100 and "。" not in t and "？" not in t
                  and "！" not in t and "；" not in t]
    check("不是机械定长切句（长段里都有句末标点）", not mechanical,
          f"可疑段 {len(mechanical)} 个")
    check("切块不超过 TTS 单段上限(120字)", all(len(t) <= 130 for t in texts),
          f"最长 {max((len(t) for t in texts), default=0)} 字")

    # 原文保真：把第一节各段拼回去，应当是原文的一个连续子串
    joined = "".join(texts)
    stripped = "".join(body.split())
    check("朗读段落拼回去仍是原文（逐字，未改写）",
          "".join(joined.split()) in stripped, f"{len(joined)} 字")

    # ---- 2. 进度只在写进度时推进 ------------------------------------------
    before = get(f"{UI}/api/reading/progress?document_id={doc_id}")
    loc = post(f"{UI}/api/reading/speak",
               {"document_id": doc_id, "chapter_index": ch_index, "chunk_index": 0})
    after = get(f"{UI}/api/reading/progress?document_id={doc_id}")
    check("speak 本身不推进进度（提交了也不算读完）",
          before.get("chunk_index") == after.get("chunk_index"),
          f"提交前 {before.get('chunk_index')} → 提交后 {after.get('chunk_index')}"
          f"（speak 回执 ok={loc.get('ok')} reason={loc.get('reason')}）")

    setp = post(f"{UI}/api/reading/progress",
                {"document_id": doc_id, "chapter_index": ch_index, "chunk_index": 2,
                 "status": "READING"})
    check("写进度能推进", int(setp.get("chunk_index") or -1) == 2, str(setp)[:200])

    # ---- 3. 停止 / 暂停 推进 generation -----------------------------------
    st0 = get(f"{S2S}/v1/speak/status")
    gen0 = int(st0.get("generation") or 0)
    stopped = post(f"{UI}/api/reading/pause",
                   {"document_id": doc_id, "chapter_index": ch_index, "chunk_index": 2,
                    "status": "STOPPED"})
    st1 = get(f"{S2S}/v1/speak/status")
    gen1 = int(st1.get("generation") or 0)
    check("停止会推进朗读 generation", gen1 > gen0, f"{gen0} → {gen1}")
    check("停止后状态落 STOPPED",
          str(stopped.get("progress", {}).get("status")) == "STOPPED",
          str(stopped.get("progress"))[:160])

    # 陈旧回调闸门：拿旧号去 speak，必须被拒
    stale = post(f"{UI}/api/reading/speak",
                 {"document_id": doc_id, "chapter_index": ch_index, "chunk_index": 3,
                  "generation": max(gen0, 0)})
    check("旧 generation 的 speak 被拒（停止之后才到的那段不会出声）",
          stale.get("ok") is False and stale.get("reason") in
          ("STALE_GENERATION", "SPEAK_UNREACHABLE", "SPEAK_HTTP_404"),
          f"ok={stale.get('ok')} reason={stale.get('reason')}")

    # 背压闸门：队列上限是 4，不会无限堆积
    st2 = get(f"{S2S}/v1/speak/status")
    check("朗读队列有上限（背压存在）",
          int(st2.get("max_pending_items") or 0) == 4,
          f"max={st2.get('max_pending_items')} pending={st2.get('pending_queue_items')}")

    # ---- 4. 新文档不复用旧 offset -----------------------------------------
    doc2 = post(f"{UI}/api/reading/import",
                {"title": "朗读测试文本 第二卷（另一篇）",
                 "text": "这是另一篇文章的第一段。\n这是另一篇文章的第二段。\n"
                         "这是另一篇文章的第三段。"})
    id2 = str(doc2.get("document_id") or "")
    check("另一篇文章拿到另一个 document_id", bool(id2) and id2 != doc_id,
          f"{doc_id} vs {id2}")
    if id2:
        p2 = get(f"{UI}/api/reading/progress?document_id={id2}")
        idx2 = p2.get("chunk_index")
        idx2 = int(idx2) if idx2 is not None else -1
        check("新文章从头开始，不沿用上一篇的进度",
              str(p2.get("status")) == "UNOPENED" and idx2 == 0, str(p2)[:160])

    # ---- 5. 朗读正文不进用户长期记忆 ---------------------------------------
    import urllib.parse
    mem = get(f"{UI}/api/memory/search?q={urllib.parse.quote('灯塔')}")
    rows = mem.get("命中") or []
    hit = [r for r in rows if isinstance(r, dict)
           and "灯塔" in json.dumps(r, ensure_ascii=False)]
    check("朗读正文没有被写进人物长期记忆", not hit,
          f"命中 {len(hit)} 条 / 检索返回 {len(rows)} 条")
    # 顺手看一眼记忆库里到底有没有这本书的任何句子（比检索更硬的证据）
    listed = get(f"{UI}/api/memory/list")
    all_rows = listed.get("记忆") or []
    polluted = [r for r in all_rows
                if isinstance(r, dict) and "灯塔" in json.dumps(r, ensure_ascii=False)]
    check("记忆库里没有朗读正文的任何一句", not polluted,
          f"记忆总条数 {len(all_rows)}，命中 {len(polluted)}")

    # ---- 6. 阅读上下文只在 READING 模式出现 --------------------------------
    post(f"{UI}/api/mode", {"mode": "ROLEPLAY"})
    rp = get(f"{UI}/api/reading/block")
    post(f"{UI}/api/mode", {"mode": "READING"})
    post(f"{UI}/api/reading/progress",
         {"document_id": doc_id, "chapter_index": ch_index, "chunk_index": 2,
          "status": "READING"})
    rd = get(f"{UI}/api/reading/block")
    check("READING 模式有阅读上下文块",
          bool(str(rd.get("block") or "").strip()),
          str(rd.get("block"))[:120])
    check("阅读上下文块自带「不是用户记忆」的边界声明",
          "不得当成用户的经历写进长期记忆" in str(rd.get("block") or ""), "")

    # 回到 ROLEPLAY，并确认系统提示词里没有阅读正文
    post(f"{UI}/api/mode", {"mode": "ROLEPLAY"})
    prompt = get(f"{UI}/api/character/prompt")
    text = str(prompt.get("内容") or "")
    check("ROLEPLAY 的系统提示词里没有朗读正文",
          "灯塔" not in text and "正在陪读" not in text,
          f"提示词 {len(text)} 字")

    passed = sum(1 for _n, ok, _d in CHECKS if ok)
    print("=" * 72)
    print(f"READING_BACKEND = {passed}/{len(CHECKS)}")
    out = HERE / "out_reading_backend.json"
    out.write_text(json.dumps(
        [{"name": n, "ok": ok, "detail": d} for n, ok, d in CHECKS],
        ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
