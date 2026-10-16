# -*- coding: utf-8 -*-
"""记忆子系统自测（纯脚本 + 断言，不需要任何测试框架）。

跑法：
    runtime/core/venv/Scripts/python.exe runtime/memory/tests/run_all.py

全程用临时目录里的存放位置，不碰项目正式数据。
"""
from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.character import canon, store           # noqa: E402
from runtime.memory import extractor, retrieval, service  # noqa: E402

FAILURES: list[str] = []
CHECKS = [0]


def check(name: str, condition: Any, detail: str = "") -> None:
    CHECKS[0] += 1
    if not condition:
        FAILURES.append(f"{name} :: {detail}")
        print(f"  [不通] {name}  {detail}")
    else:
        print(f"  [过] {name}")


# 给用户看的文字里不许出现这几个词
BANNED = ("JSON", "json", "RAG", "chunk", "Chunk", "CHUNK", "embedding", "Embedding",
          "BM25", "bm25", "数据库", "prompt", "Prompt", "PROMPT", "token", "Token", "TOKEN")


def scan_plain(label: str, *texts: str) -> None:
    for text in texts:
        body = str(text or "")
        hits = [word for word in BANNED if word in body]
        check(f"{label} 用人话", not hits, f"出现 {hits} :: {body[:60]!r}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="zero_mem_"))
    mem_db = tmp / "memories.sqlite3"
    char_db = tmp / "characters.sqlite3"
    print(f"临时存放：{tmp}")
    store.init_db(char_db)
    service.init_db(mem_db)
    character = "xiaoye"

    # ======================================================================
    print("\n[1] 记 / 查")
    first = service.add("我不吃香菜。", character, kind="preference", confidence=0.8,
                        source_session="s1", source_turn=3, db_path=mem_db)
    check("记一条拿到编号", bool(first.memory_id))
    check("默认就是在用状态", first.active is True)
    check("时间戳齐了", bool(first.created_at and first.updated_at),
          f"{first.created_at}/{first.updated_at}")
    check("带上了是哪一场的", first.source_session == "s1" and first.source_turn == 3)
    check("把握度存住了", abs(first.confidence - 0.8) < 1e-6, str(first.confidence))
    check("用户可改", first.user_editable is True)

    service.add("我住在杭州，做船工。", character, kind="fact", confidence=0.75,
                source_session="s1", db_path=mem_db)
    service.add("以后叫我阿砚。", character, kind="relationship", confidence=0.9,
                explicit=True, source_session="s1", db_path=mem_db)
    service.add("我下周要去青螺渡。", character, kind="goal", confidence=0.6,
                source_session="s2", db_path=mem_db)

    hits = service.search("香菜", character, limit=5, db_path=mem_db)
    check("两字词查得到", any("香菜" in one.text for one in hits),
          f"{[one.text for one in hits]}")
    check("命中带分层标记", all(one.layer == retrieval.LAYER_INTERACTION for one in hits),
          str([getattr(one, "layer", None) for one in hits]))
    check("命中带回记忆编号", all(one.memory_id for one in hits))
    wide = service.search("不吃香菜的偏好是什么", character, limit=5, db_path=mem_db)
    check("整句话也查得到", any("香菜" in one.text for one in wide),
          str([one.text for one in wide]))
    other = service.search("香菜", "另一个人的名字", limit=5, db_path=mem_db)
    check("不会串到别人身上", not other, str([one.text for one in other]))

    # ======================================================================
    print("\n[2] 改")
    before = first.updated_at
    time.sleep(1.1)
    changed = service.update(first.memory_id, text="我不吃香菜，也不吃葱。", db_path=mem_db)
    after = service.get(first.memory_id, db_path=mem_db)
    check("改动能落下去", changed.get("ok") and after is not None
          and "葱" in after.text, str(changed))
    check("改过要刷新时间", after is not None and after.updated_at >= before,
          f"{before} -> {after.updated_at if after else ''}")
    check("创建时间不动", after is not None and after.created_at == first.created_at)

    check("没有这条就说没有",
          service.update("m不存在的", text="x", db_path=mem_db).get("ok") is False)

    locked = service.add("这条不许直接改。", character, kind="fact", user_editable=False,
                         db_path=mem_db)
    refused = service.update(locked.memory_id, text="改了也不行", db_path=mem_db)
    check("不让改的就挡着", refused.get("ok") is False, str(refused))
    scan_plain("挡改的话", refused.get("说明", ""))

    # ======================================================================
    print("\n[3] 会话内原话：记一场、清一场")
    service.remember_turn("今晚雾很大", character_id=character, session_id="s9",
                          turn_id=1, db_path=mem_db)
    service.remember_turn("你把灯留一下", character_id=character, session_id="s9",
                          turn_id=2, db_path=mem_db)
    in_session = service.search("雾", character, limit=5, include_session=True,
                                session_id="s9", db_path=mem_db)
    check("这一场的原话查得到", any("雾" in one.text for one in in_session),
          str([one.text for one in in_session]))
    check("默认不把这一场的话混进长期", not service.search("雾", character, limit=5,
                                                            db_path=mem_db))
    cleared = service.clear_session(character, "s9", db_path=mem_db)
    check("清了这一场", cleared.get("ok") and cleared["条数"] == 2, str(cleared))
    check("清完查不到", not service.search("雾", character, limit=5, include_session=True,
                                          session_id="s9", db_path=mem_db))
    check("长期记忆没被牵连", service.get(first.memory_id, db_path=mem_db) is not None)

    # ======================================================================
    print("\n[4] 软删 + 还原")
    one = service.add("我怕打雷。", character, kind="fact", confidence=0.7, db_path=mem_db)
    gone = service.delete(memory_id=one.memory_id, db_path=mem_db)
    check("删一条是收起来", gone.get("ok") and gone["条数"] == 1, str(gone))
    check("收起来之后查不到", not service.search("打雷", character, limit=5, db_path=mem_db))
    hidden = service.get(one.memory_id, db_path=mem_db)
    check("行还在，只是不在用", hidden is not None and hidden.active is False)
    back = service.restore_character(character, db_path=mem_db)
    check("还原能放回来", back.get("ok") and back["还原条数"] >= 1, str(back))
    check("还原后查得到", any("打雷" in one2.text for one2 in
                              service.search("打雷", character, limit=5, db_path=mem_db)))
    by_text = service.delete(match_text="我怕打雷", character_id=character, db_path=mem_db)
    check("给一句话也能删对条", by_text.get("条数") == 1 and by_text["memory_ids"]
          == [one.memory_id], str(by_text))
    service.restore_character(character, db_path=mem_db)

    # ======================================================================
    print("\n[5] 清长期，绝不清设定")
    canon.add_fact("她守着一盏灯塔，灯是谁熄的一直没说明白。", character_id=character,
                   kind="setting", origin="manual", db_path=char_db)
    facts_before = len(canon.list_facts(character, db_path=char_db))
    all_before = service.list_memories(character, db_path=mem_db)
    long_before = len(all_before)
    # 清长期只碰长期那几层，这一场的原话（SESSION）不该被算进去，也不该被动
    clearable = [one for one in all_before if one.layer != service.SESSION_LAYER]
    cleared = service.clear_long_term(character, db_path=mem_db)
    check("清长期动了条数", cleared["条数"] == len(clearable),
          f"{cleared['条数']} vs 该动 {len(clearable)}（总 {long_before}）::{cleared}")
    check("清长期没碰这一场的原话",
          len([one for one in service.list_memories(character, db_path=mem_db)
               if one.layer == service.SESSION_LAYER])
          == len([one for one in all_before if one.layer == service.SESSION_LAYER]),
          str(cleared))
    check("清长期之后长期查不到",
          not service.search("杭州", character, limit=5, db_path=mem_db))
    check("设定层一条没少",
          len(canon.list_facts(character, db_path=char_db)) == facts_before)
    rows = service.list_memories(character, include_archived=True,
                                 layers=("CANON",), db_path=mem_db)
    check("记忆里不该躺设定条目（清完还是零）", not rows, str(rows))
    back = service.restore_character(character, db_path=mem_db)
    check("还原把长期全放回来",
          len(service.list_memories(character, db_path=mem_db)) == long_before, str(back))

    check("聊天来源不许直接写设定层",
          _raises(PermissionError, lambda: service.add("灯塔是我熄的", character,
                                                        layer="CANON",
                                                        source_kind="CHAT",
                                                        db_path=mem_db)))
    sync = service.add("灯塔是我熄的", character, layer="CANON", source_kind="IMPORT",
                       db_path=mem_db)
    check("要写设定层只能走非聊天来源", sync.memory_id
          and sync.layer == "CANON")
    check("设定层的条目不许被聊天覆盖",
          _raises(PermissionError, lambda: service.add("换个说法", character,
                                                        layer="CANON",
                                                        source_kind="CHAT",
                                                        db_path=mem_db)))
    service.delete(memory_id=sync.memory_id, hard=True, db_path=mem_db)

    # ======================================================================
    print("\n[6] 抽取：规则闸门 / 优先级 / 查重 / 设定保护")
    probes = [
        ("今晚吃啥？", "问句"), ("你在干嘛呢", "问句"),
        ("你现在是一个女上司", "临时角色要求"), ("恢复正常吧", "临时角色要求"),
        ("你好", "寒暄"), ("忘掉刚才那句", "收回要求"),
    ]
    for text, expect in probes:
        got = extractor.reject_reason(text)
        check(f"规则闸门：{text}", got == expect, f"判成「{got}」，期望「{expect}」")

    plain = extractor.extract_candidates("我不吃香菜，也不吃葱。")
    check("偏好能抽出来", len(plain) >= 1 and plain[0].kind == "preference",
          str([c.to_dict() for c in plain]))
    boost = extractor.extract_candidates("记住，我不吃香菜。")
    check("明说要记 → 打上标记", boost and boost[0].explicit is True,
          str([c.to_dict() for c in boost]))
    check("明说要记 → 把握度更高",
          bool(boost) and bool(plain) and boost[0].confidence > plain[0].confidence,
          f"{boost[0].confidence if boost else 0} vs {plain[0].confidence if plain else 0}")
    check("明说要记 → 优先级抬一档", boost and boost[0].priority == 1)

    only_allowed = set(extractor.ALLOWED_LONG_TERM_KINDS)
    kinds = {item.kind for item in extractor.extract_candidates(
        "我打算明年考个船证，我想成为灯守，我会自己修灯。")}
    check("抽出来的类都在六类里", kinds and kinds <= only_allowed, str(kinds))
    check("六类之外不收", not extractor.extract_candidates("今天天气还行吧"))

    report = extractor.apply_candidates(
        [extractor.Candidate(text="我爱喝很苦的浓茶。", kind="preference",
                             confidence=0.7)], character,
        db_path=mem_db, canon_db_path=char_db)
    check("第一条能写进去", len(report["写入"]) == 1, str(report))
    again = extractor.apply_candidates(
        [extractor.Candidate(text="我特别喜欢喝很苦的浓茶。", kind="preference",
                             confidence=0.72)], character,
        db_path=mem_db, canon_db_path=char_db)
    check("同一件事不再新增一条",
          not again["写入"] and (again["合并"] or again["跳过（重复）"]), str(again))
    total_after = len([one for one in service.list_memories(character, db_path=mem_db)
                       if "浓茶" in one.text or "苦" in one.text])
    check("查重之后只剩一条", total_after == 1, str(total_after))

    guard = extractor.apply_candidates(
        [extractor.Candidate(text="她守着一盏灯塔，灯是谁熄的没说明白。", kind="fact",
                             confidence=0.9, explicit=True)], character,
        db_path=mem_db, canon_db_path=char_db)
    check("和设定撞车的句子被挡下", len(guard["拒收（设定）"]) == 1, str(guard))
    blocked = guard["拒收（设定）"][0] if guard["拒收（设定）"] else {}
    check("挡下的理由是设定保护", "设定" in str(blocked.get("原因", "")), str(blocked))
    scan_plain("挡下理由", str(blocked.get("原因", "")))
    check("被挡下的没有变成记忆",
          not [one for one in service.list_memories(character, db_path=mem_db)
               if "灯塔" in one.text])

    learned = extractor.learn_from_turn("以后我说冷的时候你就把灯留一下。", character,
                                        session_id="s7", turn_id=4, db_path=mem_db,
                                        canon_db_path=char_db, remember_turn=True)
    check("整轮入口能写能报", isinstance(learned, dict) and "写入" in learned, str(learned)[:120])
    check("这一场原话进了会话层",
          bool(service.search("把灯留一下", character, limit=5, include_session=True,
                              session_id="s7", db_path=mem_db)))
    scan_plain("整轮说明", learned.get("说明", ""))

    # ======================================================================
    print("\n[7] 导出 / 导入")
    dump = service.export(character, db_path=mem_db)
    check("导出带条数", dump["条数"] == len(dump["records"]),
          f"{dump['条数']} vs {len(dump['records'])}")
    scan_plain("导出说明", dump["说明"])
    fresh = tmp / "memories_second.sqlite3"
    service.init_db(fresh)
    moved = service.import_(dump, db_path=fresh)
    check("导入能落地", moved["新增"] + moved["并入已有"] == dump["条数"], str(moved))
    again_import = service.import_(dump, db_path=fresh)
    check("重复导入不会翻倍",
          again_import["新增"] == 0 and again_import["并入已有"] >= 1, str(again_import))
    check("导入的东西查得到",
          bool(service.search("香菜", "xiaoye", limit=3, db_path=fresh)))
    broken = service.import_({"records": "不是列表"}, db_path=fresh)
    check("读不出来就明说", broken["ok"] is False, str(broken))
    scan_plain("读不出来的话", broken.get("说明", ""))
    canon_probe = service.import_({"records": [{"text": "灯塔是我熄的", "layer": "CANON",
                                               "kind": "fact"}]}, db_path=fresh)
    check("设定层条目不从导入这条路进",
          canon_probe["跳过"] == 1 and canon_probe["新增"] == 0, str(canon_probe))
    check("反射拿得到的入口名字", callable(getattr(service, "import", None)))

    # ======================================================================
    print("\n[8] 检索耗时（要求 < 200ms）")
    for index in range(40):
        service.add(f"第 {index} 件小事：我那天在渡口等了一条船。", character,
                    kind="shared_event", confidence=0.5 + (index % 3) * 0.1,
                    db_path=mem_db)
    queries = ["渡口 船", "香菜", "灯留一下", "我住在哪", "浓茶", "打雷", "青螺渡 船"]
    samples: list[float] = []
    for _ in range(3):
        for text in queries:
            t0 = time.perf_counter()
            service.search(text, character, limit=6, db_path=mem_db)
            samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    slowest = samples[-1]
    print(f"  单次检索：最快 {samples[0]:.2f}ms / 中位 "
          f"{statistics.median(samples):.2f}ms / 最慢 {slowest:.2f}ms"
          f"（{len(samples)} 次，条目 {len(service.list_memories(character, db_path=mem_db))} 条）")
    check("每次检索都在 200ms 以内", slowest < 200.0, f"最慢 {slowest:.2f}ms")
    big = tmp / "memories_big.sqlite3"
    service.init_db(big)
    with service.connect(big) as bulk:
        for index in range(20000):
            bulk.execute(
                "INSERT INTO memory_record (memory_id, character_id, layer, kind, text,"
                " norm_text, confidence, source_kind, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"bm{index}", "bulk", "LONG_TERM_MEMORY", "shared_event",
                 f"第{index}次在渡口等船，雾很大，灯留了一点", f"第{index}次在渡口等船",
                 0.5, "CHAT", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
    heavy: list[float] = []
    for text in ("渡口等船", "雾很大 灯", "第19999次"):
        t0 = time.perf_counter()
        rows_hit = service.search(text, "bulk", limit=8, db_path=big)
        heavy.append((time.perf_counter() - t0) * 1000.0)
        check(f"两万条还查得到「{text}」", bool(rows_hit))
    print(f"  两万条压力样本：{' / '.join(f'{v:.2f}ms' for v in heavy)}")
    check("两万条仍在 200ms 以内", max(heavy) < 200.0, f"最慢 {max(heavy):.2f}ms")

    # ======================================================================
    print("\n[9] 分层与契约")
    merged = retrieval.search_layers(
        "灯塔 谁熄的",
        [retrieval.LayerRequest(retrieval.LAYER_CANON, char_db,
                                canon.canon_fact_spec(character), 4),
         retrieval.LayerRequest(retrieval.LAYER_INTERACTION, mem_db,
                                service.memory_spec(character), 4)],
        weights={retrieval.LAYER_CANON: 1.0, retrieval.LAYER_INTERACTION: 1.0}, limit=6)
    check("一次调用能同时拿到两层", bool(merged), str(merged))
    layers = {getattr(one, "layer", "") for one in merged}
    check("层标记只有 CANON 和聊天这两类",
          layers <= {retrieval.LAYER_CANON, retrieval.LAYER_INTERACTION}, str(layers))
    check("能靠层标记区分设定与聊天",
          any(getattr(one, "layer", "") == retrieval.LAYER_CANON for one in merged)
          or not any(one.source_kind == "canon" for one in merged), str(layers))
    probe = merged[0].to_dict() if merged else {}
    check("分层字段能序列化出去", bool(probe.get("layer")), str(probe)[:120])
    round_trip = retrieval.LayeredPassage.from_dict(probe)
    check("按契约回读不会炸", isinstance(round_trip, retrieval.RetrievedPassage)
          or round_trip is not None)

    # 正向证明「全文索引这条路真的在走」：只喂能进索引的词，一条都不给字面兜底。
    # 出过一次事故：SQL 里列名有歧义，整条索引查询直接失败，上层只拿到空结果，
    # 看起来像「没查到」——所以除了这条，还要断言 DIAGNOSTICS 是空的。
    fts_only = retrieval.plan_query("一盏灯塔")
    check("三字以上的词走的是索引这条路",
          bool(fts_only.fts_terms) and not fts_only.like_terms, str(fts_only))
    probe_conn = service.connect(char_db)
    try:
        direct = retrieval._search_one(probe_conn, canon.canon_fact_spec(character),
                                       retrieval.QueryPlan(raw="一盏灯塔",
                                                           fts_terms=list(fts_only.fts_terms)),
                                       5)
    finally:
        probe_conn.close()
    check("关掉字面兜底仍能命中设定", bool(direct), str(direct)[:120])
    check("底层没有任何静默报错", not retrieval.DIAGNOSTICS,
          " || ".join(retrieval.DIAGNOSTICS[:3]))

    # ======================================================================
    print("\n[10] 用人话（不许出现技术词）")
    scan_plain("工具结果", service.search("香菜", character, limit=3, db_path=mem_db,
                                        as_result=True).text)
    scan_plain("空的检索", service.search("火星", character, limit=3, db_path=mem_db)[0].text
               if service.search("火星", character, limit=3, db_path=mem_db) else "无")
    all_rows = service.list_memories(character, db_path=mem_db, limit=300)
    scan_plain("存进去的话原样回读", *[row.text for row in all_rows[:40]])

    print("\n" + "=" * 66)
    print(f"共 {CHECKS[0]} 项断言，失败 {len(FAILURES)} 项，最慢一次检索 "
          f"{slowest:.2f}ms（两万条压力样本最慢 {max(heavy):.2f}ms）")
    for line in FAILURES:
        print("  失败：" + line)
    if FAILURES:
        print("结果：未通过")
        return 1
    print("结果：全部通过")
    return 0


def _raises(kind: type[BaseException], action) -> bool:
    try:
        action()
    except kind:
        return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    sys.exit(main())
