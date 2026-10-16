# -*- coding: utf-8 -*-
"""分词器实测对拍：默认 unicode61 vs trigram vs 「trigram + 短词兜底」。

只依赖标准库。跑法：
    python runtime/runtime/memory/tests/tokenizer_bench.py [--copies 240]

输出表格里每一格都是真跑出来的，runtime/memory/retrieval.py 顶部的注释直接抄这里，
所以不许改数字，只能重跑。
"""
from __future__ import annotations

import re
import sqlite3
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[2]
FIXTURES = PROJECT_ROOT / "runtime" / "character" / "tests" / "fixtures"

_SPLIT_BLANK = re.compile(r"\n\s*\n")

# 查询词 + 真值定义：一条查询的相关段落 = 「正文里真的出现了这个词的段落」。
# 这是中文使用者对「找一段话」最朴素的期待，也是唯一可以自动判定的真值。
QUERIES = [
    "归字一号", "灯芯剪", "陶老人", "盐栈的旧账", "玻璃罩", "民国十一年",
    "江面上", "灯塔", "渡口", "沈砚", "陆青", "青螺渡",
]


def load_corpus() -> list[str]:
    """把两份样例按空行切成段落，段落就是这里的最小检索单元。"""
    paras: list[str] = []
    for name in ("novel_sample.md", "novel_plain.txt"):
        raw = (FIXTURES / name).read_text(encoding="utf-8")
        for block in _SPLIT_BLANK.split(raw):
            text = re.sub(r"[#>]+\s*", "", block).strip()
            if len(text) >= 8:
                paras.append(text)
    return paras


def scale(paras: list[str], copies: int) -> list[str]:
    """放大语料，让延迟数字有意义（每份加一个不同的小尾巴，避免完全重复）。"""
    out: list[str] = []
    for n in range(copies):
        for p in paras:
            out.append(f"{p}（卷{n + 1}）")
    return out


def build_db(path: Path, docs: list[str], tokenize: str | None) -> None:
    """建一张普通表 + 一张全文表；普通表留着给 LIKE 兜底用。"""
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    opt = f", tokenize='{tokenize}'" if tokenize else ""
    conn.execute("CREATE TABLE doc(id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
    conn.execute(f"CREATE VIRTUAL TABLE doc_fts USING fts5(body{opt})")
    with conn:
        for i, body in enumerate(docs):
            conn.execute("INSERT INTO doc(id, body) VALUES(?, ?)", (i + 1, body))
            conn.execute("INSERT INTO doc_fts(rowid, body) VALUES(?, ?)", (i + 1, body))
    conn.execute("INSERT INTO doc_fts(doc_fts) VALUES('optimize')")
    conn.commit()
    conn.close()


def fts_rows(conn: sqlite3.Connection, term: str, prefix: bool) -> list[int]:
    expr = f'"{term}"*' if prefix else f'"{term}"'
    sql = ("SELECT rowid FROM doc_fts WHERE doc_fts MATCH ? "
           "ORDER BY bm25(doc_fts) LIMIT 20")
    return [r[0] for r in conn.execute(sql, (expr,)).fetchall()]


def like_rows(conn: sqlite3.Connection, term: str) -> list[int]:
    sql = "SELECT id FROM doc WHERE body LIKE ? ESCAPE '\\' LIMIT 20"
    esc = term.replace("%", r"\%").replace("_", r"\_")
    return [r[0] for r in conn.execute(sql, (f"%{esc}%",)).fetchall()]


# 每种方案怎么把一条查询变成候选行；None 表示这条查询该走 LIKE
STRATEGIES = [
    ("默认 unicode61（精确短语）", "", "fts_exact", False),
    ("默认 unicode61（前缀 *）", "", "fts_prefix", False),
    ("trigram（只用全文索引）", "trigram", "fts_exact", False),
    ("LIKE 全表扫（对照上限）", "trigram", "like_only", False),
    ("trigram + 两字以内走 LIKE", "trigram", "hybrid", True),
]


def query_once(conn: sqlite3.Connection, how: str, term: str) -> list[int]:
    if how == "fts_exact":
        return fts_rows(conn, term, prefix=False)
    if how == "fts_prefix":
        return fts_rows(conn, term, prefix=True)
    if how == "like_only":
        return like_rows(conn, term)
    if how == "hybrid":
        return fts_rows(conn, term, prefix=False) if len(term) >= 3 else like_rows(conn, term)
    raise ValueError(how)


def run(db: Path, how: str, repeat: int = 5) -> dict:
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA mmap_size=268435456")
    per_query: dict[str, list[int]] = {}
    latencies: list[float] = []
    with conn:
        for term in QUERIES:
            rows: list[list[int]] = []
            for _ in range(repeat):
                t0 = time.perf_counter()
                got = query_once(conn, how, term)
                latencies.append((time.perf_counter() - t0) * 1000.0)
                rows.append(got)
            per_query[term] = max(rows, key=len)
    conn.close()
    lat = sorted(latencies)
    return {"per_query": per_query, "p50": statistics.median(lat),
            "p95": lat[max(0, int(len(lat) * 0.95) - 1)], "max": lat[-1]}


def main(argv: list[str]) -> int:
    copies = 240
    if "--copies" in argv:
        copies = int(argv[argv.index("--copies") + 1])
    out_dir = HERE / "_tokenizer_bench"
    out_dir.mkdir(exist_ok=True)

    base = load_corpus()
    docs = scale(base, copies)
    gold = {term: {i + 1 for i, d in enumerate(docs) if term in d} for term in QUERIES}
    print(f"语料：基础 {len(base)} 段，放大 {copies} 份 -> 共 {len(docs)} 段，"
          f"真值段落合计 {sum(len(g) for g in gold.values())} 条")
    print(f"查询词 {len(QUERIES)} 个：" + "、".join(QUERIES))
    print("说明：Top-20 截断，所以召回率上限 = min(真值, 20)/真值；"
          "两字词的真值都远大于 20，这里比的是「找不找得到」，不是「找全找不全」。")
    print()
    print(f"{'分词方案':<26}{'召回':>7}{'准确':>7}{'落空':>7}{'p50ms':>9}"
          f"{'p95ms':>9}{'最大ms':>9}")
    for label, tok, how, _ in STRATEGIES:
        db = out_dir / f"{tok}_{how}.sqlite3"
        build_db(db, docs, tok)
        res = run(db, how)
        got = hit = 0
        zero: list[str] = []
        for term in QUERIES:
            rows = set(res["per_query"][term])
            got += len(rows & gold[term])
            hit += len(rows)
            if not rows:
                zero.append(term)
        want = sum(min(len(g), 20) for g in gold.values())
        lat = ("{:.2f}".format(res["p50"]), "{:.2f}".format(res["p95"]),
               "{:.2f}".format(res["max"]))
        print(f"{label:<26}{got / want * 100:>6.1f}%"
              f"{(got / hit * 100) if hit else 0.0:>6.1f}%{f'{len(zero)}/{len(QUERIES)}':>7}"
              f"{lat[0]:>9}{lat[1]:>9}{lat[2]:>9}")
        if zero:
            print(f"{'':<26}落空：{'、'.join(zero)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
