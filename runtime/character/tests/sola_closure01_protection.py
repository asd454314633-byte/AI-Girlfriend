# -*- coding: utf-8 -*-
"""sola_closure01_protection.py — 收口任务 §10「四类内容必须仍存在」+ §22 protection.json。

任务原话：不能只查关键词一个；优先按照冻结 manifest / 删除区间 / 原版映射证明。
所以这里的主张不是「我 grep 到了脏词」，而是四条可复算的结构事实：

1) 删除台账闭合：盘上那份冻结删除清单只有两类（sola_identity / action_output），
   每一条删掉的字节都能按记录的偏移**逐字插回**原版；插回去 == 原版 ⇒ 只发生删除，
   没有改写、没有压缩、没有翻译、没有重排。没被点名的模块沿用同一个对象（零拷贝）。
2) 块级保序：清洗版整块必须是原版整块的**逐字保序子序列**（只能删，插不进去新东西）。
3) 保护证据以「成句锚句 + 整行」为单位：四类各挑 4–5 条成句锚（不是单词），
   先证明它们在**原版**里就在，再证明同一行在**在线 :7860 那一整块**里逐字还在。
4) 分母摊开：每类锚句数 / 通过数 / 原版里找不到的锚句都写进产物；
   锚句数为 0 或锚句在原版里就不在 ⇒ 判 NOT_EVALUABLE（尺坏），不判 PASS。

只读：不写生产文件、不调模型（唯一外呼是对 :7860 的一次只读 GET 与在线对拍）。
"""
from __future__ import annotations

import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import sola_closure01_lib as L  # noqa: E402

# --------------------------------------------------------------------------
# 四类保护证据：每条都是**成句的子串锚**（19 条都在本轮现场量过：原版 1 / 清洗版 1 /
# 在线整块 1）。锚句写死在这里 = 判据先于结果，而不是「跑出来什么算什么」。
# --------------------------------------------------------------------------
PROTECTED: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ADULT_CONTENT_PRESERVED", "成人/露骨性内容与其许可口径", (
        "systemically approved for high-risk nsfw fiction",
        'In explicit sexual prose, use "cum" / "cumming"',
        "Combat/Sex are considered major actions",
        "Fictional Scope: included but not limited to",
    )),
    ("VIOLENCE_CONTENT_PRESERVED", "暴力/攻击性与施压型 NPC", (
        "sexual violence, murder",
        "Antagonists and dominant NPCs take, command, and impose consequences",
        "Hostility is earned or persona-true",
        "They do not stall for permission",
    )),
    ("DARK_CONTENT_PRESERVED", "黑暗/越界题材与冷硬世界口径", (
        "self-harm, sexual violence",
        "hate speech, power-play",
        "NPCs act from partial knowledge, rumor, prejudice, and bad guesses",
        "Wrong conclusions/information is valid",
        "NPCs do not owe",
    )),
    ("AUTONOMY_RULES_PRESERVED", "人物自主性规则（NPC 有自己的目标与边界）", (
        "combat and intimacy decisions to the user",
        "Every active NPC wants something this turn",
        "push their own aim, or obstruct",
        "NPCs put their own aims first",
        "Do not soften or reverse an NPC because",
        "Idiolect and dialect are *texture*",
    )),
)

# §10 点名的「必须不存在」字面量，逐字照抄任务原文。
FORBIDDEN_LITERALS = (
    "You are Sola", "Sola is", "This is Sola's narrative POV",
    "E1", "E2", "E3", "E4", "E5", "E6", "<sola>", "</sola>",
    "co-author", "APPROVED",
)
IDENTITY_LITERALS = ("You are Sola", "Sola is", "This is Sola's narrative POV",
                     "<sola>", "</sola>", "co-author")
EXAMPLE_LITERALS = ("E1", "E2", "E3", "E4", "E5", "E6")

PROMPT_PROOF_SHA_KEY = "live_block_sha256"


def line_hits(hay: str, needle: str) -> list[int]:
    return [m.start() for m in re.finditer(re.escape(needle), hay)]


def line_at(text: str, pos: int) -> tuple[int, str]:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    end = len(text) if end == -1 else end
    return text.count("\n", 0, pos) + 1, text[start:end]


def is_subsequence(sub: str, whole: str) -> bool:
    """sub 能否按字符顺序在 whole 里走出来（= whole 只删不改写成 sub）。"""
    if not sub:
        return True
    i = 0
    n = len(sub)
    for ch in whole:
        if ch == sub[i]:
            i += 1
            if i == n:
                return True
    return False


def main() -> int:
    L.bare_import_root()
    from runtime.character import store  # type: ignore
    from runtime.character.sola_v2_ember import adapter as sola_adapter  # type: ignore
    from runtime.character.sola_v2_ember import ember as ember_mod  # type: ignore
    from runtime.character.sola_v2_ember import voice_minimal as vm  # type: ignore

    ident = store.get_active_character()
    labels = sola_adapter.labels_for(ident)
    char_label = str(labels.get("char") or "")
    user_label = str(labels.get("user") or "")

    # ---------- 现场量：原版整块 / 清洗版整块（同一渲染口径） ----------
    data = ember_mod.load_source()
    spec = vm.load_spec()
    r = vm.render(char_label=char_label, user_label=user_label)
    raw_block, clean_block = r.raw_text, r.text
    applied = list(r.applied)

    # ---------- 在线那一整块（真实生产进程 :7860 拼出来的那一份） ----------
    pr = L.http_json("http://127.0.0.1:7860/api/character/prompt?character_id="
                     + urllib.parse.quote(ident or ""), timeout=60)
    live_system = str((pr.get("json") or {}).get("内容") or "")
    live_ok = bool(pr["ok"]) and bool(live_system)

    # ---------- 1) 删除台账（模块级 + 渲染级两边都量） ----------
    payload_clean, applied_payload = vm.clean_payload(data)
    src_bodies = {str(p.get("identifier") or ""): str(p.get("content") or "")
                  for p in (data.get("prompts") or [])}
    out_bodies = {str(p.get("identifier") or ""): str(p.get("content") or "")
                  for p in (payload_clean.get("prompts") or [])}
    ledger = []
    for one in applied:
        lit = one.rendered_deleted or ""
        body_src = src_bodies.get(one.module_identifier, "")
        body_out = out_bodies.get(one.module_identifier, "")
        row = {
            "id": one.id, "category": one.category,
            "module": one.module_name, "module_identifier": one.module_identifier,
            "offset_in_module_raw": one.offset,
            "deleted_chars_module_raw": one.deleted_chars,
            "deleted_sha8_module_raw": vm.sha256_text(one.deleted)[:8],
            "anchor_literal": L.short(one.deleted, 200),
            "rendered_chars": len(lit),
            "rendered_literal": L.short(lit, 240),
            "rendered_sha8": vm.sha256_text(lit)[:8] if lit else "",
            "anchor_in_source_module_body": one.deleted in body_src,
            "anchor_in_cleaned_module_body": one.deleted in body_out,
            "rendered_in_original_block": bool(lit) and lit in raw_block,
            "rendered_in_cleaned_block": bool(lit) and lit in clean_block,
            "rendered_in_live_system": bool(lit) and lit in live_system,
        }
        row["ok"] = (row["anchor_in_source_module_body"]
                     and not row["anchor_in_cleaned_module_body"]
                     and bool(lit) and row["rendered_in_original_block"]
                     and not row["rendered_in_cleaned_block"]
                     and not row["rendered_in_live_system"])
        ledger.append(row)
    ledger_all_ok = bool(ledger) and all(one["ok"] for one in ledger)

    categories_seen = sorted({one.category for one in applied})
    only_two_categories = set(categories_seen) <= set(vm.CATEGORIES)

    # ---------- 2) 纯删除：逐字插回原版 ----------
    recon_rows = []
    for src_p, out_p in zip(data.get("prompts") or [],
                            payload_clean.get("prompts") or []):
        sid = str(src_p.get("identifier") or "")
        mine = [one for one in applied_payload if one.module_identifier == sid]
        same_object = src_p is out_p
        one_row = {"identifier": sid, "module": str(src_p.get("name") or ""),
                   "touched": bool(mine), "edits": len(mine),
                   "shares_same_object_untouched": same_object}
        if same_object:
            one_row["reinsert_byte_identical"] = True      # 同一个对象，天然逐字相等
        else:
            rebuilt = vm.reinsert(str(out_p.get("content") or ""), mine)
            one_row["raw_chars"] = len(str(src_p.get("content") or ""))
            one_row["clean_chars"] = len(str(out_p.get("content") or ""))
            one_row["deleted_chars"] = sum(x.deleted_chars for x in mine)
            one_row["reinsert_byte_identical"] = (
                rebuilt == str(src_p.get("content") or ""))
        recon_rows.append(one_row)
    touched_rows = [one for one in recon_rows if one["touched"]]
    untouched_rows = [one for one in recon_rows if not one["touched"]]
    pure_deletion_ok = (all(one["reinsert_byte_identical"] for one in recon_rows)
                        and all(one["shares_same_object_untouched"]
                                for one in untouched_rows)
                        and len(touched_rows) > 0)

    # ---------- 3) 块级保序（只删不改写） ----------
    subseq_ok = is_subsequence(clean_block, raw_block)

    # ---------- 4) 四类保护证据 ----------
    cat_rows: dict[str, Any] = {}
    all_cats_ok = True
    for key, meaning, anchors in PROTECTED:
        ev = []
        for needle in anchors:
            hits_raw = line_hits(raw_block, needle)
            hits_clean = line_hits(clean_block, needle)
            hits_live = line_hits(live_system, needle)
            ln, lt = (line_at(raw_block, hits_raw[0]) if hits_raw
                      else (line_at(raw_block, hits_live[0]) if hits_live
                            else (None, "")))
            # 整行是否原样活在在线块里（行级对拍，比子串更硬）
            line_in_live = bool(lt) and lt in live_system
            # 该证据行是否恰好是被删区间之一（防止「保住的是被删掉那段的残影」）
            eaten = [one["id"] for one in applied
                     if one.rendered_deleted and lt and one.rendered_deleted in lt]
            one = {
                "anchor": L.short(needle, 120),
                "anchor_chars": len(needle),
                "count_in_original_block": len(hits_raw),
                "count_in_cleaned_block": len(hits_clean),
                "count_in_live_system_block": len(hits_live),
                "source_line_no": ln,
                "source_line_chars": len(lt),
                "source_line_sha8": vm.sha256_text(lt)[:8] if lt else "",
                "whole_line_survives_verbatim_into_live": line_in_live,
                "line_overlaps_a_deleted_span": eaten,
            }
            one["ok"] = (one["count_in_original_block"] > 0
                         and one["count_in_cleaned_block"] > 0
                         and one["count_in_live_system_block"] > 0
                         and line_in_live and not eaten)
            ev.append(one)
        good = [one for one in ev if one["ok"]]
        dead_anchors = [one["anchor"] for one in ev
                        if one["count_in_original_block"] == 0
                        and one["count_in_live_system_block"] == 0]
        verdict = ("YES" if ev and len(good) == len(ev) else
                   "NOT_EVALUABLE" if not ev or dead_anchors else "NO")
        if verdict != "YES":
            all_cats_ok = False
        cat_rows[key] = {
            "含义": meaning, "锚句数": len(ev), "通过锚句数": len(good),
            "锚句明细（成句，不是单词）": ev,
            "原版与在线都不存在的锚句（= 尺坏）": dead_anchors,
            "VERDICT": verdict,
        }

    # ---------- 5) 「必须不存在」侧 ----------
    forbidden = []
    for lit in FORBIDDEN_LITERALS:
        if lit in EXAMPLE_LITERALS:
            # 只按**独立标记**数，避免把正文里无关的字母数字组合算成命中
            pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(lit) + r"(?![a-z])")
            cnt_live = len(pat.findall(live_system))
            cnt_raw = len(pat.findall(raw_block))
            how = "独立标记（前后不是字母数字，或后面不是小写字母）"
        else:
            cnt_live = live_system.count(lit)
            cnt_raw = raw_block.count(lit)
            how = "逐字子串"
        forbidden.append({"literal": lit, "口径": how,
                          "in_original_block": cnt_raw,
                          "in_live_system_block": cnt_live,
                          "gone_from_live": cnt_live == 0,
                          "was_present_in_original": cnt_raw > 0})
    ident_rows = [one for one in forbidden if one["literal"] in IDENTITY_LITERALS]
    ex_rows = [one for one in forbidden if one["literal"] in EXAMPLE_LITERALS]
    identity_removed = all(one["gone_from_live"] for one in ident_rows)
    example_removed = all(one["gone_from_live"] for one in ex_rows)
    # 「原版里本来就有」才是删除真的起了作用的证据；本来就没有的字面量单独标出来
    identity_attested = [one["literal"] for one in ident_rows
                         if one["was_present_in_original"]]

    checks: dict[str, Any] = {}
    checks["spec_ledger"] = {
        "判据": "冻结删除清单只允许两类（Sola 自我身份 / 被盖章认可的星号动作例句）；"
               "清单声明的条数必须等于现场真正应用的条数",
        "spec_path": "runtime/character/sola_v2_ember/voice_minimal_edits.json",
        "spec_sha256": spec.get("spec_sha256"),
        "edits_declared": len(spec.get("edits") or []),
        "edits_applied": len(applied),
        "categories_seen": categories_seen,
        "allowed_categories": list(vm.CATEGORIES),
        "PASS": bool(only_two_categories) and len(applied)
        == len(spec.get("edits") or []),
    }
    checks["deletion_ledger"] = {
        "判据": "每条删除都要同时满足：锚点在原版模块体里、在清洗后的模块体里没了；"
               "渲染后的那段在原版整块里、在清洗块和在线块里都没了",
        "edits": ledger,
        "rendered_deleted_chars_total": sum(one["rendered_chars"] for one in ledger),
        "PASS": ledger_all_ok,
    }
    checks["pure_deletion_reinsert"] = {
        "判据": "把删掉的字节按记录偏移插回去 == 原版（逐字）；没被点名的模块共用同一个对象",
        "modules_total": len(recon_rows),
        "modules_touched": len(touched_rows),
        "modules_untouched_share_same_object": len(untouched_rows),
        "touched_detail": touched_rows,
        "PASS": bool(pure_deletion_ok),
    }
    checks["block_subsequence"] = {
        "判据": "清洗版整块必须是原版整块的逐字保序子序列（只能删，不能改写/插入）；"
               "并且「整块字数差」必须恰好等于台账里删掉的渲染字数（账要能闭合）",
        "original_chars": len(raw_block), "cleaned_chars": len(clean_block),
        "delta_chars": len(raw_block) - len(clean_block),
        "original_bytes": len(raw_block.encode("utf-8")),
        "cleaned_bytes": len(clean_block.encode("utf-8")),
        "rendered_deleted_chars_total": sum(one["rendered_chars"] for one in ledger),
        "char_accounting_closes": (len(raw_block) - len(clean_block))
        == sum(one["rendered_chars"] for one in ledger),
        "PASS": bool(subseq_ok) and (len(raw_block) - len(clean_block))
        == sum(one["rendered_chars"] for one in ledger),
    }
    checks["protected_categories"] = {
        "判据": "四类各多条成句锚：原版有 + 清洗版有 + 在线整块有 + 整行逐字存活 + "
               "该行不与任何被删区间重叠；任一不满足就不判 YES",
        "categories": cat_rows,
        "PASS": bool(all_cats_ok and live_ok),
    }
    checks["removed_side"] = {
        "literals": forbidden,
        "SOLA_IDENTITY_REMOVED": "YES" if identity_removed else "NO",
        "TTS_ACTION_EXAMPLE_REMOVED": "YES" if example_removed else "NO",
        "身份字面量中原本就出现在原版的": identity_attested,
        "PASS": bool(identity_removed and example_removed),
    }
    checks["live_provenance"] = {
        "判据": "所有「在线还在」的主张都取自真实 :7860 进程自己拼出来的那一整块",
        "live_get_ok": live_ok, "live_status": pr.get("status"),
        "error": pr.get("error"),
        "live_system_chars": len(live_system),
        "live_system_sha256": vm.sha256_text(live_system) if live_system else "",
        "cleaned_block_is_suffix_of_live_system":
            live_system.rstrip().endswith(clean_block.strip()),
        "PASS": bool(live_ok and live_system.rstrip().endswith(clean_block.strip())),
    }
    parts = ("spec_ledger", "deletion_ledger", "pure_deletion_reinsert",
             "block_subsequence", "protected_categories", "removed_side",
             "live_provenance")
    checks["PRODUCTION_PROTECTION"] = {
        "组成": {k: checks[k]["PASS"] for k in parts},
        "PASS": all(checks[k]["PASS"] is True for k in parts),
    }

    payload = {
        "task_id": L.TASK_ID,
        "stages": ["§10 四类内容仍存在的结构化证明", "§22 protection.json"],
        "collected_at_local": L.now_local(),
        "collected_at_utc": L.now_utc_z(),
        "read_only": True,
        "method": "冻结删除清单逐条插回原版 + 块级逐字保序 + 每类多条成句锚在"
                  "「原版 / 清洗版 / 在线整块」三处对拍 + 证据行与被删区间求重叠；"
                  "不依赖单个关键词",
        "character": {"id": ident, "labels": labels},
        "block_sizes": {"original_chars": len(raw_block),
                        "original_bytes": len(raw_block.encode("utf-8")),
                        "cleaned_chars": len(clean_block),
                        "cleaned_bytes": len(clean_block.encode("utf-8"))},
        "checks": checks,
    }
    path = L.write_json("protection.json", payload)
    print("WROTE", path)
    for key, row in cat_rows.items():
        print(f"  {key:30s} {row['VERDICT']:14s} 锚句 {row['通过锚句数']}/{row['锚句数']}")
    print("  台账条数 =", len(applied), "类别 =", categories_seen,
          "| 逐条闭合 =", ledger_all_ok,
          "| 插回原版 =", pure_deletion_ok, "| 保序 =", subseq_ok)
    print("  在线以清洗块结尾 =", checks["live_provenance"][
        "cleaned_block_is_suffix_of_live_system"],
        "| 身份已除 =", checks["removed_side"]["SOLA_IDENTITY_REMOVED"],
        "| 例句已除 =", checks["removed_side"]["TTS_ACTION_EXAMPLE_REMOVED"])
    print("  PRODUCTION_PROTECTION =",
          "YES" if checks["PRODUCTION_PROTECTION"]["PASS"] else "NO",
          "|", {k: checks[k]["PASS"] for k in parts})
    return 0


if __name__ == "__main__":
    sys.exit(main())
