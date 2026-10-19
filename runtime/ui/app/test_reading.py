# -*- coding: utf-8 -*-
"""陪读文档层离线校验（AI_GIRLFRIEND_PRODUCT_UI_READING_V2 · §9-§11、§17、§18）。

跑法：`/home/user/s2s/.venv/bin/python test_reading.py`
用 /tmp 下的独立 SQLite，不碰生产记忆库，也不写 D 盘（文件用例落在 /tmp）。
"""

import os
import sys

os.environ["GIRLFRIEND_MEMORY_DB"] = "/tmp/reading_test.sqlite3"
os.environ["READING_ROOT"] = "/tmp/reading_test_root"

import girlfriend_reading as R  # noqa: E402

SAMPLE = """第一章 初见

她推开门，雨还在下。他没有回头。
“你来了。”他说。

第二章 长谈

聊了很久，从天气聊到香菜。她笑了。
他也笑了！
然后沉默。

尾声

书读完了。
"""


def check(cond, label, extra=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        raise SystemExit(1)


def main():
    meta = R.import_text("测试书", SAMPLE)
    doc = meta["document_id"]
    text = SAMPLE.replace("\r\n", "\n")

    print("[章节识别]")
    check(meta["chapter_count"] == 3, "3 章", str(meta["chapter_count"]))
    titles = [c["title"] for c in R._read_index(doc)["chapters"]]
    check(titles == ["第一章 初见", "第二章 长谈", "尾声"], "标题逐字对齐", str(titles))

    print("[朗读切块：按 。！？； 与自然段，不是固定字数]")
    view = R.chapter_view(doc, 1)
    got = [c["text"] for c in view["chunks"]]
    want = ["聊了很久，从天气聊到香菜。她笑了。", "他也笑了！", "然后沉默。"]
    check(got == want, "第二章切块（短句并进前一句）", str(got))
    check(all(text[a:b] == c["text"] for c in view["chunks"]
              for a, b in [(c["char_start"], c["char_end"])]), "偏移量能还原原文")
    joined = "".join(c["text"] for c in R.chapter_view(doc, 0)["chunks"])
    check(joined == "她推开门，雨还在下。他没有回头。“你来了。”他说。", "段内无丢字")
    check(all(c["index"] == n for n, c in enumerate(view["chunks"])), "段号在章内从 0 连续")

    print("[标题识别不误伤叙述句]")
    m4 = R.import_text("易混", "第一章 起\n\n引子一样的风灌进来。她后退了一步。\n\n后记很短的一段话里没有句号")
    t4 = [c["title"] for c in R._read_index(m4["document_id"])["chapters"]]
    check(t4 == ["第一章 起"], "以「引子」开头的叙述句没被当成章节标题", str(t4))

    print("[长自然段：只在句末断，不按固定字数硬切]")
    long_para = "。".join(f"第{i}句说到了一半就被继续延长以凑出足够长的段落" for i in range(1, 9)) + "。"
    m5 = R.import_text("长段", "第一章 长\n\n" + long_para)
    segs = [c["text"] for c in R.chapter_view(m5["document_id"], 0)["chunks"]]
    check(len(segs) > 1, "长段被切成多块", f"{len(segs)} 块 / {len(long_para)} 字")
    check(all(s[-1] in "。！？；" for s in segs), "每一块都以句末标点收尾（没有半句）", str(segs[:2]))
    check("".join(segs) == long_para, "切完不丢字")
    check(max(len(s) for s in segs) <= R.MAX_SEGMENT_CHARS + 20, "单块长度有上限")

    print("[无标题文本 → 整篇一章，按自然段]")
    m2 = R.import_text("无章节", "第一行。\n第二行！\n\n第三行？")
    v2 = R.chapter_view(m2["document_id"], 0)
    check(m2["chapter_count"] == 1, "1 章")
    check([c["text"] for c in v2["chunks"]] == ["第一行。", "第二行！", "第三行？"], "三个自然段=三块")

    print("[进度持久化 + 断点]")
    p = R.set_progress(doc, 1, 1, "READING")
    check((p["chapter_index"], p["chunk_index"], p["status"]) == (1, 1, "READING"), "写入读回")
    p2 = R.set_progress(doc, 2, 0, "PAUSED")
    check((p2["chapter_index"], p2["chunk_index"]) == (2, 0), "改段号生效")
    loc = R.chunk_location(doc, 1, 1)
    check(loc["text"] == "他也笑了！", "chunk_location 按（章, 章内段号）定位", loc["text"])
    check(R.chunk_location(doc, 0, 1)["chapter_index"] == 0, "跨章不串号")

    print("[READING_STATE 注入块 §15-§17]")
    R.set_progress(doc, 1, 0, "READING")
    R.set_chapter_summary(doc, 0, "两人在雨里第一次说话。")
    R.set_chapter_summary(doc, 1, "从天气聊到香菜，关系变近。")
    block = R.reading_state_block()
    print("  ----")
    for line in block.split("\n"):
        print("  " + line)
    print("  ----")
    check(block.startswith("[正在陪读"), "有块")
    check("测试书" in block and "第二章 长谈" in block, "含书名与章节")
    check("她推开门" in block, "含前 2~3 段（前一句在第一章）")
    check("然后沉默" in block, "含后 1~2 段")
    check("从天气聊到香菜，关系变近。" in block, "含本章摘要")
    check("两人在雨里第一次说话。" in block, "含前章摘要")
    check("不得当成用户的经历写进长期记忆" in block, "带隔离声明")
    check(len(block) <= R.READING_BLOCK_MAX_CHARS + 200, "块封顶", f"{len(block)} chars")

    print("[§18 关键词检索：无 embedding]")
    hits = R.search(doc, "香菜")
    check(hits and "香菜" in hits[0]["text"], "命中含关键词", str(hits[:1]))
    check(R.search(doc, "火星探测器") == [], "查不到就返回空（允许说检索不到，不编造）")

    print("[编码 / 文件导入]")
    with open("/tmp/reading_gbk.txt", "wb") as fh:
        fh.write("第一章 试探\n\n这句话是 GBK 存的。".encode("gb18030"))
    m3 = R.import_path("/tmp/reading_gbk.txt")
    check(m3["encoding"] == "gb18030", "识别非 UTF-8", m3["encoding"])
    check(R.chapter_view(m3["document_id"], 0)["chunks"][0]["text"] == "这句话是 GBK 存的。", "解码正确")
    with open("/tmp/reading_test.pdf", "wb") as fh:
        fh.write(b"%PDF-1.4")
    try:
        R.import_path("/tmp/reading_test.pdf")
        check(False, "不支持的格式应被拒绝")
    except ValueError as exc:
        check(str(exc) == "UNSUPPORTED_FORMAT", "拒绝 PDF（§9：第一版不装解析依赖）", str(exc))

    print("[重复导入幂等]")
    again = R.import_text("测试书", SAMPLE)
    check(again["document_id"] == doc, "同一本书不产生第二份")
    check(R.get_progress(doc)["status"] == "READING", "进度没被重置")
    check(len([d for d in R.list_documents() if d["document_id"] == doc]) == 1, "书库不重复")

    print("[硬上限：解析产出不设界会灌爆磁盘]")
    try:
        R.import_text("超大", "句子。" * (R.MAX_DOC_CHARS + 10))
        check(False, "超长文本应被拒绝")
    except ValueError as exc:
        check("TOO_LARGE" in str(exc), "MAX_DOC_CHARS 生效", str(exc))
    check("library" in os.listdir("/tmp/reading_test_root"), "文件写在 READING_ROOT 下")
    sizes = sum(os.path.getsize(os.path.join(dp, f))
                for dp, _d, fs in os.walk("/tmp/reading_test_root") for f in fs)
    check(sizes < 200_000, "本次测试落盘字节数有界", f"{sizes} B")

    print("\nREADING_LAYER_PASS")


if __name__ == "__main__":
    sys.exit(main())
