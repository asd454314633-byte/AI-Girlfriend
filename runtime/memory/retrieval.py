# -*- coding: utf-8 -*-
"""统一检索内核：人物设定（CANON）与聊天记忆（INTERACTION）走同一套中文检索逻辑。

放在 runtime/memory/ 下，但 runtime/character/ 也调用它 —— 中文分词的坑只需要踩一次，
两边各写一份必然写歪。人物侧的工具入口在 runtime/character/search.py，那只是本模块的
一层薄壳。

===============================================================================
分词器实测对拍（数据是真的，复现：python runtime/memory/tests/tokenizer_bench.py）
===============================================================================
语料：本项目自带的两份中文样例（runtime/character/tests/fixtures/），
基础 43 段 -> 放大 240 份 = 10320 段，真值段落合计 12960 条。
查询词 12 个：归字一号、灯芯剪、陶老人、盐栈的旧账、玻璃罩、民国十一年、江面上、
             灯塔、渡口、沈砚、陆青、青螺渡（其中 4 个是两字词，正是人名与地名）。
真值定义：某段落正文里真的出现了该词 => 该段落相关（唯一可自动判定的标准）。
指标：Top-20 截断，召回分母用 min(真值, 20)，所以这里比的是「找不找得到」，
      不是「找全找不全」。落空 = 返回 0 条的查询个数。

    分词方案                        召回      准确     落空     p50ms   p95ms   最大ms
    默认 unicode61（精确短语）      25.0%   100.0%   9/12     0.01     0.09     0.36
        落空：灯芯剪、陶老人、盐栈的旧账、玻璃罩、民国十一年、江面上、灯塔、渡口、青螺渡
    默认 unicode61（前缀 *）        83.3%   100.0%   2/12     0.10     1.23     1.98
        落空：灯芯剪、渡口
    trigram（只用全文索引）         66.7%   100.0%   4/12     0.17     0.30     0.52
        落空：灯塔、渡口、沈砚、陆青     <-- 全部是两字词，结构性失明
    LIKE 全表扫（对照上限）        100.0%   100.0%   0/12     0.04     0.13     0.23
    trigram + 两字以内走 LIKE      100.0%   100.0%   0/12     0.20     0.37     0.49

连跑两次的召回/准确/落空三列完全一致，延迟列抖动在 0.2ms 以内（同一台机器、同一份语料）。

选择：全文索引建 trigram，查询侧按词长分流 —— >=3 字走 trigram 全文索引并用 bm25()
排序，<=2 字走 LIKE 兜底。理由：

  1. 默认 unicode61 不带中文词典，一整句连续汉字被切成「一个词」，所以只能匹配
     恰好落在切分边界上的串。12 个查询里 9 个直接查不到东西（召回 25%）。加 `*`
     前缀只补到 83.3%，而且漏的是「词出现在小句中间」的情况（灯芯剪、渡口）——
     这种漏是位置相关的，用户完全没有规律可循，比查不到更难解释。
  2. trigram 对 >=3 字是真子串匹配，这部分召回 100%，且 bm25() 排序正常。
     它对 1-2 字查询结构性失明（三元组凑不出来），而中文最常查的恰恰是两字的人名、
     地名、称谓（沈砚 / 陆青 / 渡口 / 灯塔）：单用 trigram 会在这类查询上落空 4/12。
  3. LIKE 兜底只承担 <=2 字这一小类，实测与上限对齐（100% 召回）。它是全表扫，
     规模上去后会变成主要开销 —— 注意上表里 LIKE 的 0.04ms 带水分：LIMIT 20 会提前
     收工，而样例语料的命中都排在前面。真到几十万段时，慢的原因是「短词全表扫」这一
     条，不是全文索引；届时该做的是把短词查询限制成「前缀 LIKE（可走索引列的排序键）」
     或者引导用户输入更长的词，而不是砍掉兜底、把落空率藏起来。
  4. 两层索引（人物设定 / 记忆）分别算 bm25，跨表数值不可比，所以先层内归一再乘层权重
     （见 _fuse()），保证 CANON 与 INTERACTION 的相对顺序由调用方显式控制。

本模块不含任何模型/语音提供方的名字，不联网，不加载任何常驻显存的东西。
"""
from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # 包内导入时 contracts 已由 __init__ 绑定
    from . import contracts as _contracts  # type: ignore
except ImportError:  # pragma: no cover - 直接被当脚本跑时的退路
    import sys
    _ROOT = Path(__file__).resolve().parents[2]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from runtime.core.providers import contracts as _contracts  # type: ignore

RetrievedPassage = _contracts.RetrievedPassage

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]

# 两个存放位置；调用方可以整体换掉（测试用临时目录）
CHARACTER_DB_DEFAULT = PROJECT_ROOT / "runtime" / "character" / "data" / "characters.sqlite3"
MEMORY_DB_DEFAULT = HERE / "data" / "memories.sqlite3"

LAYER_CANON = "CANON"            # 小说原文 / 设定事实
LAYER_INTERACTION = "INTERACTION"  # 聊天里长出来的记忆

# trigram 凑不出三元组的长度，走 LIKE
TRIGRAM_MIN_LEN = 3
# 兜底用的最长查询串，防止把一个长句整个塞进 LIKE
MAX_QUERY_CHARS = 96

# 出过一次「静默失败」的教训：SQL 写错时上层拿到空结果，看起来像「没查到」。
# 所以这里把最后一次底层报错留下来，自测会断言它必须是空的。
DIAGNOSTICS: list[str] = []


def _diagnose(where: str, exc: BaseException) -> None:
    DIAGNOSTICS.append(f"{where}:{type(exc).__name__}:{exc}")
    if len(DIAGNOSTICS) > 20:
        del DIAGNOSTICS[:-20]

_RUN_RE = re.compile(r"[0-9A-Za-z_\u4e00-\u9fff]+")
_STRIP_RE = re.compile(r"[\"'*()^:~`\\/]")
_LIKE_ESCAPES = {"%": r"\%", "_": r"\_"}
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()

# 用户会整句话丢进来（「灯塔是谁熄的」），这种串当整体查一定查不到。
# 中文的虚词几乎不承载查询意图，用它当切刀，能稳定切出「灯塔」「熄」这种实词。
CJK_STOP_CHARS = set("的了是在和与就都很也还不有把被给对从以之于且或若因着过吗呢啊呀吧呗"
                     "咱你他她它这那个些们要会能没又只更最太多少")
LONG_RUN = 5          # 超过这个长度才做虚词切分，短词不切（免得把「归字一号」切碎）
MAX_TERMS = 8         # 一条查询最多带几个词，防止长句把检索拖慢


@dataclass(slots=True)
class LayeredPassage(RetrievedPassage):
    """契约的 RetrievedPassage 没有 layer 槽位，用子类补一个。

    仍然是 isinstance(passage, RetrievedPassage)，所以任何按契约办事的下游都不用改；
    带 @dataclass(slots=True) 是为了 layer 能进 to_dict()（契约基类是用 asdict 序列化的，
    不加装饰器就只会输出父类那几个字段，分层信息在往外传的时候静默丢掉）。
    唯一的失分点：换成基类 ReceivedPassage.from_dict 回读时 layer 会丢，因为基类只认
    自己那几栏，已记录在审计文档的留开项里。
    """

    layer: str = LAYER_CANON


@dataclass(slots=True)
class QueryPlan:
    """一条中文查询拆成「能走全文索引的」和「只能走 LIKE 的」两堆。"""

    raw: str = ""
    fts_terms: list[str] = field(default_factory=list)
    like_terms: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.fts_terms or self.like_terms)

    def match_expr(self) -> str:
        """多个词按 OR 组，交给 bm25() 去奖励同时命中的段落。"""
        return " OR ".join(f'"{t}"' for t in self.fts_terms)

    def like_clauses(self) -> list[str]:
        return list(self.like_terms)


def _split_on_stops(run: str) -> list[str]:
    """按中文虚词切一刀，只留下长度 >=2 的实词片段。"""
    segments: list[str] = []
    current: list[str] = []
    for char in run:
        if char in CJK_STOP_CHARS:
            if len(current) >= 2:
                segments.append("".join(current))
            current = []
        else:
            current.append(char)
    if len(current) >= 2:
        segments.append("".join(current))
    return segments


def plan_query(raw: str | None) -> QueryPlan:
    """把一条中文查询拆成「能走全文索引的」和「只能走 LIKE 兜底的」两堆。

    整句原文仍然是第一优先（真引用它就该排最前），长句额外再按虚词切出的实词兜底。
    """
    plan = QueryPlan(raw=(raw or "").strip()[:MAX_QUERY_CHARS])
    candidates: list[str] = []
    for piece in _STRIP_RE.split(plan.raw):
        for run in _RUN_RE.findall(piece):
            term = run.strip()
            if not term or term in candidates:
                continue
            candidates.append(term)
            if len(term) > LONG_RUN and re.search(r"[\u4e00-\u9fff]", term):
                for segment in _split_on_stops(term):
                    if segment not in candidates:
                        candidates.append(segment)
    for term in candidates:
        if len(plan.fts_terms) + len(plan.like_terms) >= MAX_TERMS:
            plan.skipped.append(term)
            continue
        if len(term) >= TRIGRAM_MIN_LEN:
            plan.fts_terms.append(term)
        elif len(term) == 2 or (len(term) == 1 and len(candidates) == 1):
            plan.like_terms.append(term)
        else:
            plan.skipped.append(term)
    if plan.empty:
        plan.skipped.append("EMPTY")
    return plan


def write_lock_for(path: Path | str) -> threading.RLock:
    key = str(Path(path))
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


def open_db(path: Path | str, *, schema_sql: str | None = None,
            read_only: bool = False) -> sqlite3.Connection | None:
    """打开一个存放位置。不存在时：读侧返回 None（这层就是空的），写侧建目录再建库。"""
    path = Path(path)
    if read_only and not path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    uri = f"file:{path.as_posix()}?mode=ro" if read_only else None
    conn = sqlite3.connect(uri or str(path), uri=bool(uri), timeout=10.0,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    if schema_sql:
        conn.executescript(schema_sql)
        conn.commit()
    return conn


def _escape_like(term: str) -> str:
    for k, v in _LIKE_ESCAPES.items():
        term = term.replace(k, v)
    return term


@dataclass(slots=True)
class IndexSpec:
    """一张可检索的表：正名列、主键列、全文表名、额外的过滤条件。"""

    table: str
    fts: str
    text_column: str
    key_column: str
    extra_select: str = ""
    where: str = ""          # 已经拼好的、不含用户输入的 SQL 片段
    params: tuple[Any, ...] = ()


def _search_one(conn: sqlite3.Connection, spec: IndexSpec, plan: QueryPlan,
                limit: int) -> list[tuple[str, str, float, dict[str, Any]]]:
    """一个索引内部：先走全文索引，凑不够再走 LIKE 兜底，合并后按分数从高到低。

    分数只是「层内可比」的相对值，跨层要在 _fuse() 里再归一。
    """
    out: dict[str, tuple[str, str, float, dict[str, Any]]] = {}
    extra = (", " + spec.extra_select) if spec.extra_select else ""
    fetch = max(limit * 4, 20)
    # 列名必须带表名前缀：全文索引表和正文表都有同名的正文字段，裸写会被判成「列名有歧义」，
    # 整条查询直接失败。这里踩过一次，所以统一加前缀，并把失败留在 DIAGNOSTICS 里。
    key_col = f"{spec.table}.{spec.key_column}"
    body_col = f"{spec.table}.{spec.text_column}"

    def pack(key: str, body: str, score: float, row: sqlite3.Row) -> None:
        misc = {k: row[k] for k in row.keys() if k not in ("k", "body", "score")}
        out[key] = (key, body, score, misc)

    expr = plan.match_expr()
    if expr:
        sql = (
            f"SELECT {key_col} AS k, {body_col} AS body, "
            f"-bm25({spec.fts}) AS score{extra} "
            f"FROM {spec.fts} JOIN {spec.table} ON {spec.table}.rowid = {spec.fts}.rowid "
            f"WHERE {spec.fts} MATCH ?"
        )
        if spec.where:
            sql += f" AND {spec.where}"
        sql += f" ORDER BY bm25({spec.fts}) LIMIT {fetch}"
        try:
            rows = list(conn.execute(sql, (expr,) + spec.params))
        except sqlite3.Error as exc:
            _diagnose(f"{spec.fts}:match", exc)
            raise
        for row in rows:
            pack(str(row["k"]), str(row["body"]), float(row["score"]), row)

    if plan.like_terms and len(out) < limit:
        fts_floor = min((v[2] for v in out.values()), default=0.0)
        for index, term in enumerate(plan.like_terms):
            sql = (f"SELECT {key_col} AS k, {body_col} AS body{extra} "
                   f"FROM {spec.table} WHERE {body_col} LIKE ? ESCAPE '\\'")
            if spec.where:
                sql += f" AND {spec.where}"
            sql += f" LIMIT {limit * 2}"
            try:
                rows = list(conn.execute(sql, (f"%{_escape_like(term)}%",) + spec.params))
            except sqlite3.Error as exc:
                _diagnose(f"{spec.table}:like", exc)
                raise
            for row in rows:
                key = str(row["k"])
                if key in out:
                    continue
                # 兜底命中排在整层最低分之下，短词之间再按长度给一点区分
                pack(key, str(row["body"]), fts_floor - (10.0 - len(term)) - index, row)
    return sorted(out.values(), key=lambda item: item[2], reverse=True)[:limit]


def search_index(db_path: Path | str, spec: IndexSpec, query: str | None,
                 *, limit: int = 6) -> list[tuple[str, str, float, dict[str, Any]]]:
    plan = plan_query(query)
    if plan.empty:
        return []
    conn = open_db(db_path, read_only=True)
    if conn is None:
        return []
    try:
        return _search_one(conn, spec, plan, max(1, int(limit)))
    except sqlite3.Error:
        # 索引还没建（首次导入前）-> 当成查不到，绝不让检索把上层打断。
        # 真正的 SQL 写错也走到这里，但 _search_one 已经把它记进 DIAGNOSTICS 了，
        # 自测会断言那份记录是空的，所以不会被当成「就是没查到」。
        return []
    finally:
        conn.close()


def _normalize(rows: Sequence[tuple[str, str, float, dict[str, Any]]]) -> list[float]:
    """把一层的分数压到 0.05..1.0：只保留层内相对次序，不假装跨层可比。

    全层同分时给满分 1.0（比如整层都是两字兜底命中）；分数全非正时按名次衰减。
    """
    if not rows:
        return []
    scores = [row[2] for row in rows]
    lo, hi = min(scores), max(scores)
    if hi <= 0:
        return [1.0 / (index + 1) for index in range(len(scores))]
    if hi - lo < 1e-9:
        return [1.0 for _ in scores]
    return [0.05 + 0.95 * ((max(score, 0.0) - lo) / (hi - lo)) for score in scores]


def _fuse(groups: Iterable[tuple[str, Sequence[tuple[str, str, float, dict[str, Any]]]]],
          weights: dict[str, float], limit: int) -> list[tuple[str, str, float,
                                                               dict[str, Any]]]:
    """跨层合并：层内归一 -> 乘层权重 -> 全局排序；同分时用原始分数破平。"""
    merged: list[tuple[str, str, float, dict[str, Any]]] = []
    for name, rows in groups:
        scaled = _normalize(list(rows))
        weight = float(weights.get(name, 1.0))
        for row, value in zip(rows, scaled):
            extra = dict(row[3])
            extra["layer"] = name
            extra["raw_score"] = row[2]
            merged.append((row[0], row[1], value * weight, extra))
    merged.sort(key=lambda item: (item[2], item[3].get("raw_score", 0.0)), reverse=True)
    return merged[:limit]


def fuse(groups: Iterable[tuple[str, Sequence[tuple[str, str, float, dict[str, Any]]]]],
         weights: dict[str, float], limit: int) -> list[tuple[str, str, float,
                                                              dict[str, Any]]]:
    """跨层合并的公开入口（同一套归一化规则，只有一份实现）。"""
    return _fuse(groups, weights, limit)


# ---------------------------------------------------------------------------
# 对外：一次调用把 CANON 与 INTERACTION 两层都查一遍
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class LayerRequest:
    """一层检索请求：层名 + 存放位置 + 表结构 + 取多少条。"""

    layer: str
    db_path: Path | str
    spec: IndexSpec
    limit: int = 6


_MIN_SCORE = 0.05


def search_layers(query: str | None, requests: Sequence[LayerRequest],
                  *, weights: dict[str, float] | None = None,
                  limit: int = 6) -> list[LayeredPassage]:
    """查一层或多层，返回按分数排好的 passage。

    任何一层出错（文件还没建、索引还没灌）都只让那一层为空，不影响其它层，也不抛到
    语音链路上去。
    """
    groups: list[tuple[str, list[tuple[str, str, float, dict[str, Any]]]]] = []
    for request in requests:
        try:
            rows = search_index(request.db_path, request.spec, query, limit=request.limit)
        except sqlite3.Error as exc:      # 打开存放位置这一层就失败：这层空掉，别牵连其它层
            _diagnose(f"layer:{request.layer}", exc)
            rows = []
        groups.append((request.layer, rows))
    fused = _fuse(groups, weights or {}, limit)
    passages: list[LayeredPassage] = []
    for key, body, score, extra in fused:
        if score < _MIN_SCORE:
            continue
        passages.append(to_passage(key, body, score, extra))
    return passages


def to_passage(key: str, body: str, score: float, extra: dict[str, Any]) -> LayeredPassage:
    """把一行命中变成契约对象；extra 里带 title / chapter / offset 这些出处。"""
    layer = str(extra.get("layer") or LAYER_CANON)
    passage = LayeredPassage()
    passage.memory_id = str(extra.get("memory_id") or (key if layer == LAYER_INTERACTION else ""))
    passage.document_id = str(extra.get("document_id") or ("" if layer == LAYER_INTERACTION else key))
    passage.title = str(extra.get("title") or "")
    passage.chapter = str(extra.get("chapter") or "")
    passage.text = body
    passage.score = round(float(score), 6)
    try:
        passage.offset = int(extra.get("ordinal") or extra.get("offset") or 0)
    except (TypeError, ValueError):
        passage.offset = 0
    passage.source_kind = ("memory" if layer == LAYER_INTERACTION
                           else str(extra.get("source_kind") or "canon"))
    passage.layer = layer
    return passage


# ---------------------------------------------------------------------------
# 最近上下文加权（任务 §12 的第三条排序依据：FTS/BM25 + 字段权重 + 最近上下文关键词）
# ---------------------------------------------------------------------------

CONTEXT_LOOKBACK = 3         # 往回看几轮
CONTEXT_MAX_TERMS = 6        # 最多带几个最近关键词
CONTEXT_BONUS = 1.15         # 命中最近关键词的抬多少（只改排序，不改相关性判定）
CONTEXT_MIN_TERM_CHARS = 2


def context_terms(history: Sequence[Any] | None, *, lookback: int = CONTEXT_LOOKBACK,
                  max_terms: int = CONTEXT_MAX_TERMS) -> list[str]:
    """从最近几轮对话里取出「现在正聊着的东西」。

    只用最朴素的词长切分（plan_query 那一套），不引分词库：这一层要的是「刚刚出现过
    的实词」，不是语义理解。
    """
    rows = [dict(one) for one in (history or []) if isinstance(one, dict)][-max(1, lookback):]
    terms: list[str] = []
    for row in rows:
        body = str(row.get("text") or row.get("content") or "")
        plan = plan_query(body)
        for term in plan.fts_terms + plan.like_terms:
            if len(term) < CONTEXT_MIN_TERM_CHARS or term in terms:
                continue
            terms.append(term)
            if len(terms) >= max_terms:
                return terms
    return terms


def boost_by_terms(passages: Sequence[Any], terms: Sequence[str], *,
                   factor: float = CONTEXT_BONUS) -> list[Any]:
    """命中最近关键词的排前面；没给关键词就原样返回（不产生任何副作用）。"""
    picked = [one for one in (terms or []) if len(str(one)) >= CONTEXT_MIN_TERM_CHARS]
    rows = list(passages or [])
    if not picked or not rows:
        return rows
    for row in rows:
        body = normalize_text(getattr(row, "text", "") or "")
        if any(normalize_text(str(term)) in body for term in picked):
            row.score = round(float(row.score) * float(factor), 6)
    rows.sort(key=lambda one: one.score, reverse=True)
    return rows


def boost_by_rows(rows: Sequence[tuple[str, str, float, dict[str, Any]]],
                  terms: Sequence[str], *, factor: float = CONTEXT_BONUS
                  ) -> list[tuple[str, str, float, dict[str, Any]]]:
    """同一套加权，作用在「还没变成契约对象」的原始行上（层内融合之前）。"""
    picked = [one for one in (terms or []) if len(str(one)) >= CONTEXT_MIN_TERM_CHARS]
    source = list(rows or [])
    if not picked or not source:
        return source
    out: list[tuple[str, str, float, dict[str, Any]]] = []
    for key, body, score, extra in source:
        text = normalize_text(body)
        value = float(score)
        if any(normalize_text(str(term)) in text for term in picked):
            value = round(value * float(factor), 6)
        out.append((key, body, value, extra))
    out.sort(key=lambda item: item[2], reverse=True)
    return out


# ---------------------------------------------------------------------------
# 文本归一化：查重、比对都用这一份，别处不许再各写一套
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[\s，。、；：！？…—－·\"'“”‘’「」『』（）()【】\[\]{},.;:!?'\"<>=~`|/\\@#$%^&*()+_-]")
_FULL_WIDTH = {ord(c): ord(c) - 0xFEE0 for c in "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
                                            "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ０１２３４５６７８９"}
_FULL_WIDTH[ord("　")] = ord(" ")


def normalize_text(text: str | None) -> str:
    """去空白与标点、全角转半角、统一小写：只留下用来比「说的是不是同一件事」的骨架。"""
    if not text:
        return ""
    cleaned = _PUNCT_RE.sub("", str(text).translate(_FULL_WIDTH)).lower()
    return cleaned


OVERLAP_MIN_CHARS = 6     # 两边都至少这么长才用实词重合度判重复，理由见 similarity()


def _content_chars(text: str) -> set[str]:
    """一条文本里「承载意思的字」：丢掉中文虚词和零碎字母数字后的单字集合。"""
    return {c for c in text if c not in CJK_STOP_CHARS and not c.isdigit()}


def similarity(left: str, right: str) -> float:
    """归一化后的相似度：包含关系直接算满，其余取两种粗比的较大值。

    不引第三方库，也不是语义相似度（那一档在留开项里写明要等向量检索）。两种粗比：

    1. 二元组重合度（Jaccard）：对语序敏感，改一个字往往掉一大截；
    2. 实词字重合度（overlap coefficient）：按「去掉虚词后剩下的字」算交集除以较短一方，
       专门补二元组的短板——同一件事换了个说法（「我爱喝很苦的浓茶」/
       「我特别喜欢喝很苦的浓茶」）二元组只有 0.48，会被判成两条新记忆，
       而实词重合度给 0.83，能合并。

    第 2 种只在两边都够长（>= OVERLAP_MIN_CHARS 字）时才用：短句子里证据字太少，
    「我不吃葱」和「我不吃香菜也不吃葱」会因为交集占满短的一方被判成同一条，
    那是丢信息而不是合并。
    """
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        return 0.72 + 0.28 * (len(shorter) / max(len(longer), 1))

    # 二元组重合度：对中文足够稳，又不需要分词器
    def bigrams(text: str) -> set[str]:
        return {text[i:i + 2] for i in range(len(text) - 1)} or {text}

    ga, gb = bigrams(a), bigrams(b)
    overlap = len(ga & gb) / max(len(ga | gb), 1)
    best = min(0.99, overlap * 1.15)
    if len(a) >= OVERLAP_MIN_CHARS and len(b) >= OVERLAP_MIN_CHARS:
        ca, cb = _content_chars(a), _content_chars(b)
        if ca and cb:
            ratio = len(ca & cb) / min(len(ca), len(cb))
            best = max(best, min(0.98, ratio))
    return best
