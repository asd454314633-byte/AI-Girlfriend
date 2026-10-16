# -*- coding: utf-8 -*-
"""把「判断文本里混进外语词」这件事从一次性的肉眼发现，变成一把可重跑的尺子。

零模型调用：只读盘上的判断回执 —— 门槛那 36 发（`out/judge_gate_rows.jsonl` 的 `text`）
和三臂/四臂对照里挂在正式回复上的那一份（`..._latency_rows.jsonl` 的 `judgment`）。
V2 与 V2b 各算各的分母（见下面 SOURCES 第四项）。

为什么判据设在**字符层面**而不是"查英文词表"：本轮实际漏出来的三种里，
一个是英文（irritability）、一个是韩文谚文（딱）、一个是葡文（defesa）。
任何按语种名单写的检查都会漏掉后两个。

写法上的三个坑（都在这里避掉，别改回简单版）：
  1. 判据不能是"某种外语的词表"，也不能是"非白名单字符"——本轮实际漏出来的是英文
     （irritability）、韩文谚文（딱）、葡文（defesa）三种，按语种名单必漏后两种；
     而按"非白名单字符"数，第一版就把全角逗号「，」「；」这类正常中文标点算成了脏
     （虚报 14/48）。定稿判据：**按 Unicode 字母类**——凡"是字母、又不是汉字"的字符都算脏，
     标点/数字/符号一律放过。
  2. 「扫到 0 行」不等于「干净」——也可能是读不到文件或者字段名写错。
     所以必须先打印分母（UNIVERSE），分母为 0 直接判 NOT_EVALUABLE 而不是 PASS。
  3. 只读 `text` 一个字段会漏掉三臂对照那 12 发（它们的字段叫 `judgment`）。两份都要扫。
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"

# 汉字（统一表意文字 + 扩展 A）——正文只允许这些
HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
# 字段名 → 文件，四条来源都要扫，漏一条分母就少一截。
# 第四项是 V2b（瘦身）那轮的判产。为什么**分开记账不合并成一个数**：
# V2 报告里写死的是「脏 3 / 共 48 = 6.2%」，那一句已经交付并被复核过；把 V2b 的 36+18
# 段并进同一个分母，那句 6.2% 就悄悄换了含义。所以每个 run 各报自己的分母与比率，
# 只有"这一把尺子中不中"是合并判的。
SOURCES = (("judge_gate_rows.jsonl", "text", "V2 门槛 36 发", "V2"),
           ("judge_latency_rows.jsonl", "judgment", "V2 三臂对照 12 发", "V2"),
           ("judge_slim_rows.jsonl", "text", "V2b 门槛 36 发", "V2b"),
           ("judge_slim_latency_rows.jsonl", "judgment", "V2b 四臂对照", "V2b"))


def foreign_runs(text: str) -> list[str]:
    """返回文本里所有「是字母、但不是汉字」的连续片段（标点数字符号不算）。"""
    runs: list[str] = []
    current: list[str] = []
    for ch in text:
        if unicodedata.category(ch).startswith("L") and not HAN.match(ch):
            current.append(ch)
            continue
        if current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return runs



def rows(file_name: str, field: str) -> list[dict[str, Any]]:
    path = OUT / file_name
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        body = str(row.get(field) or "")
        if not body:
            continue
        out.append({"source": file_name, "scenario": row.get("scenario"),
                    "draw": row.get("draw"), "arm": row.get("arm", ""), "text": body})
    return out


def self_control() -> bool:
    """尺子自证：一段脏的必须抓到，一段干净的必须放过。缺这一步，下面的 0 就没有意义。"""
    positive = foreign_runs("人物态度：略带微 irritability 的亲密")      # 应抓到
    negative = foreign_runs("人物态度：略带心疼的包容，回复节奏：短促有力（3 句）")  # 应放过
    ok = positive == ["irritability"] and negative == []
    print("尺子自证 SELF_CONTROL = %s（正例抓到 %s / 负例抓到 %s）"
          % ("OK" if ok else "BROKEN", positive or "无", negative or "无"))
    return ok


def main() -> int:
    if not self_control():
        print("NOT_EVALUABLE：尺子本身不可信，下面的数字一律不作数")
        return 2
    scanned: list[dict[str, Any]] = []

    missing: list[str] = []
    for file_name, field, label, run in SOURCES:
        found = rows(file_name, field)
        if not found:
            missing.append(file_name)
        for one in found:
            one["label"] = label
            one["run"] = run
        scanned.extend(found)

    dirty = [{**one, "foreign": foreign_runs(one["text"])}
             for one in scanned if foreign_runs(one["text"])]

    print("分母 UNIVERSE = %d 段判断文本（%s）"
          % (len(scanned), " + ".join("%s:%d" % (label, sum(1 for o in scanned if o["label"] == label))
                                      for _f, _k, label, _r in SOURCES)))
    if missing:
        print("NOT_EVALUABLE，读不到：%s" % ", ".join(missing))
        print("（分母不齐时禁止把剩下的当成干净）")
        return 2
    if not scanned:
        print("NOT_EVALUABLE：一段判断文本都没读到 —— 这是尺子坏了，不是内容干净")
        return 2

    for one in dirty:
        where = "S%02d 第 %s 抽" % (int(one["scenario"] or 0), one["draw"])
        if one["arm"]:
            where += " · %s" % one["arm"]
        print("  脏 %s [%s] %s" % (one["source"].replace(".jsonl", ""), where, "、".join(one["foreign"])))
    verdict = "DIRTY" if dirty else "LANGUAGE_CLEAN"
    per_run: dict[str, dict[str, Any]] = {}
    for run in sorted({one["run"] for one in scanned}):
        got = [one for one in scanned if one["run"] == run]
        bad = [one for one in dirty if one["run"] == run]
        per_run[run] = {"universe": len(got), "dirty": len(bad),
                        "rate": round(len(bad) / len(got), 4) if got else 0.0,
                        "words": sorted({word for one in bad for word in one["foreign"]})}
        print("  %s 本 run：脏 %d / 共 %d = %.1f%%  %s"
              % (run, len(bad), len(got), 100.0 * len(bad) / len(got),
                 "、".join(per_run[run]["words"]) or "无"))
    print("JUDGE_LANGUAGE = %s（脏 %d / 共 %d = %.1f%%）"
          % (verdict, len(dirty), len(scanned), 100.0 * len(dirty) / len(scanned)))
    (OUT / "judge_language_check.json").write_text(
        json.dumps({"universe": len(scanned), "per_source": {label: sum(1 for o in scanned if o["label"] == label)
                                                            for _f, _k, label, _r in SOURCES},
                  "per_run": per_run,
                  "dirty_count": len(dirty),
                  "dirty_rate": round(len(dirty) / len(scanned), 4),
                  "verdict": verdict, "missing_sources": missing,
                  "dirty_rows": [{k: v for k, v in one.items() if k != "text"} for one in dirty],
                  "note": "判据=Unicode 字母类且非汉字；标点/数字/符号放过。只判不改内容。"
                          "分 run 记账：V2 那句 3/48 的含义不许被 V2b 的样本悄悄改掉"},
                  ensure_ascii=False, indent=1), encoding="utf-8")
    print("EVIDENCE_OUT", OUT / "judge_language_check.json")
    return 1 if dirty else 0


if __name__ == "__main__":
    raise SystemExit(main())
