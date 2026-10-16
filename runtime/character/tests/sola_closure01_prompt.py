# -*- coding: utf-8 -*-
"""prompt proof — 任务 §9「正式 system 运行态证明」+ §10「最小清洗运行态验证」+ §16 的 prompt token 账。

三条不许妥协的口径：
  1. 「正式 system」取自**在听的那个 :7860 进程**的在线装配端点（生产 Context Builder），
     并与我在进程内用同一套原语现算的那一份逐字对拍（两端 SHA 相同才算「运行态」）；
  2. 保留项 / 那两类内容的尺**沿用上一轮那份冻结验收脚本**（PROTECTED / IDENTITY_PATTERNS /
     ACTION_PATTERNS / ASTERISK_KEPT / action_verdict / classify_lines / delete_spans /
     account_diff），不新造第二套判据；但把「在位」的证据位置从「清洗块」
     升级到「最终进模型的那一整条 system」，并给每把尺配正控制（扫原版必须命中）；
  3. 「两类之外零改动」用**三条独立复算**证明：
       a) 模块级：冻结原版正文 → ``apply_deletions`` → ``reinsert`` 必须逐字回到原文；
       b) 整块级：拿原版整块按登记的渲染字面逐段抠字（纯删除）→ 必须逐字等于现场清洗块；
       c) 行级：diff 的每条 −/+ 行与两版每一行都要能归属到登记区间（``account_diff`` +
          ``classify_lines``），归不出来 = 有改写或误删。

只读；除 :8090 的 /tokenize（只分词、不生成、不占 slot）与 :7860 的两个只读 GET 外，
不打任何生产端点，不写任何生产状态。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sola_closure01_lib as L  # noqa: E402

PROOF_DIR = Path(__file__).resolve().parent
MINIMAL_PROOF = PROOF_DIR / "sola_v2_ember_voice_minimal_proof.py"

# §10 点名要查的六个「不该在」的字面（E1..E6 那六个被删项的编号本身也一并查）
FORBIDDEN_LITERALS = [
    "You are Sola",
    "Sola is",
    "This is Sola's narrative POV",
    "E1", "E2", "E3", "E4", "E5", "E6",
    "<sola>", "</sola>",
    "co-author",
    "APPROVED",
]

# 上一轮 Direct Port 冻结下来的原版整块尺寸（交接 §6.14.2）与本轮清洗块尺寸（§6.15）。
RAW_BLOCK_BYTES = 7469
RAW_BLOCK_CHARS = 7363
CLEAN_BLOCK_BYTES = 7093
CLEAN_BLOCK_CHARS = 7011

# /api/character/preview 的 history 参数形状是「一行一条、谁:话」（character_admin
# ._history_rows），不是 JSON。这里按它真实认得的形状传，并把我进程内那一份历史
# 写成同一批行 —— 两端才是同一件事。
HISTORY_ROWS = [{"role": "user", "text": "妈，我今晚想早点回来。"},
                {"role": "assistant", "text": "回来就好，路上慢点。"}]
HISTORY_FOR_PREVIEW = "\n".join(f"{one['role']}:{one['text']}" for one in HISTORY_ROWS)

USER_TEXT = "今天加班到好晚，累死了"


def load_frozen_rulers():
    """把上一轮那份验收脚本按模块装起来，直接沿用它的尺（只装常量与函数，不跑 main）。"""
    spec = importlib.util.spec_from_file_location("sola_minimal_proof_rulers", MINIMAL_PROOF)
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    assert spec and spec.loader
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def tokenize_via_8090(text: str) -> dict:
    """用模型自己的分词器数 token（llama.cpp /tokenize 只分词，不生成、不进 slot）。"""
    r = L.http_json("http://127.0.0.1:8090/tokenize",
                    payload={"content": text, "add_special": False}, timeout=30)
    toks = r.get("json") or {}
    return {"ok": r["ok"], "status": r.get("status"), "error": r.get("error"),
            "count": (len(toks.get("tokens")) if isinstance(toks, dict)
                      and toks.get("tokens") is not None else None)}


def live_endpoints(character_id: str) -> dict:
    """在听的那个 :7860 进程自己拼出来的东西 —— 两路只读 GET。"""
    qs = urllib.parse.quote(USER_TEXT)
    url = ("http://127.0.0.1:7860/api/character/preview?q=" + qs
           + "&character_id=" + urllib.parse.quote(character_id or "")
           + "&history=" + urllib.parse.quote(HISTORY_FOR_PREVIEW))
    r = L.http_json(url, timeout=60)
    j = r.get("json") or {}
    rows = j.get("给她看的") or []
    systems = [i for i, one in enumerate(rows) if str(one.get("谁")) == "system"]
    live_system_joined = "\n\n".join(
        str(rows[i].get("内容") or "").strip() for i in systems).strip()
    preview = {
        "url": url, "ok": r["ok"], "status": r.get("status"), "error": r.get("error"),
        "段数": j.get("段数"), "已用长度": j.get("已用长度"), "长度上限": j.get("长度上限"),
        "让出去了什么": j.get("让出去了什么"),
        "roles_in_order": [str(one.get("谁")) for one in rows],
        "system_row_indexes": systems,
        "SYSTEM_MESSAGE_COUNT_live": len(systems),
        "system_chars_live": sum(len(str(one.get("内容") or "")) for one in rows
                                 if str(one.get("谁")) == "system"),
        "rows_head_preview": [{"谁": one.get("谁"), "字数": len(str(one.get("内容") or "")),
                               "头": L.short(one.get("内容"), 60)} for one in rows],
        "system_text_live_chars": len(live_system_joined),
        "system_text_live_sha256": L.sha256_bytes(live_system_joined.encode("utf-8")),
    }
    # /api/character/prompt 把系统段原样接成一整块（浏览器连线吃的那一份）
    purl = ("http://127.0.0.1:7860/api/character/prompt?character_id="
            + urllib.parse.quote(character_id or ""))
    pr = L.http_json(purl, timeout=60)
    pj = pr.get("json") or {}
    live_block = str(pj.get("内容") or "")
    preview["prompt_endpoint"] = {
        "url": purl, "ok": pr["ok"], "status": pr.get("status"), "error": pr.get("error"),
        "字数": pj.get("字数"), "chars": len(live_block),
        "bytes": len(live_block.encode("utf-8")),
        "sha256": L.sha256_bytes(live_block.encode("utf-8")) if live_block else "",
        "已用长度": pj.get("已用长度"), "长度上限": pj.get("长度上限"),
        "人物名称": pj.get("人物名称"),
    }
    return preview


def main() -> int:
    L.bare_import_root()
    rulers = load_frozen_rulers()
    from runtime.character import context_builder  # type: ignore
    from runtime.character import store  # type: ignore
    from runtime.character.sola_v2_ember import adapter as sola_adapter  # type: ignore
    from runtime.character.sola_v2_ember import ember as ember_mod  # type: ignore
    from runtime.character.sola_v2_ember import voice_minimal as vm  # type: ignore

    ident = store.get_active_character()
    labels = sola_adapter.labels_for(ident)

    # ---------- 现场装配（进程内，与 :7860 同一套原语） ----------
    ctx = context_builder.build(character_id=ident, session_id="",
                                user_text=USER_TEXT,
                                history=[{"role": "user", "text": "妈，我今晚想早点回来。"},
                                         {"role": "assistant", "text": "回来就好，路上慢点。"}])
    msgs = context_builder.messages(ctx, temporary_state=None)
    system_text = ctx.system_text()
    blocks = [(name, len(text.strip())) for name, text in ctx.system_blocks()
              if text.strip()]
    sola_text = ctx.sola_text or ""
    base_text = "\n\n".join(t.strip() for n, t in ctx.system_blocks()
                            if t.strip() and n != "sola")

    live_preview = live_endpoints(ident)
    prompt_ep = live_preview["prompt_endpoint"]
    profile = store.get_profile(ident)
    char_name = (profile.display_name or profile.nickname or "") if profile else ""

    # 镜像 :7860 /api/character/prompt 那一档的入参（q 空、没有历史、没有本轮判断），
    # 这样「在线那一整块」与「我在这里现算的那一整块」才是同一件事，能逐字对拍。
    ctx_prompt = context_builder.build(character_id=ident, session_id="",
                                       user_text="", history=(), query="")
    msgs_prompt = context_builder.messages(ctx_prompt, temporary_state=None)
    block_prompt = "\n\n".join(
        str(m.get("text") or "").strip() for m in msgs_prompt
        if m.get("role") == "system").strip()
    sha_block_prompt = L.sha256_bytes(block_prompt.encode("utf-8"))

    # ---------- §10 的三条独立复算 ----------
    spec_path = L.REPO / "runtime/character/sola_v2_ember/voice_minimal_edits.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    r = vm.render(char_label=labels["char"], user_label=labels["user"])
    raw_block = r.raw_text.strip()
    clean_block = r.text.strip()

    # a) 模块级：冻结原版正文 → 纯删除 → 按记录偏移插回，必须逐字等于原文
    source = ember_mod.load_source()
    bodies = {str(p.get("identifier") or ""): str(p.get("content") or "")
              for p in source.get("prompts") or []}
    module_roundtrip = []
    for mod_id in sorted({one.module_identifier for one in r.applied}):
        mine = [one for one in spec["edits"] if str(one["module_identifier"]) == mod_id]
        original = bodies.get(mod_id, "")
        cleaned_body, spanned = vm.apply_deletions(original, mine)
        module_roundtrip.append({
            "module_identifier": mod_id,
            "module_name": spanned[0].module_name if spanned else "",
            "original_chars": len(original),
            "cleaned_chars": len(cleaned_body),
            "deleted_chars": len(original) - len(cleaned_body),
            "ids": [one.id for one in spanned],
            "reinsert_equals_original": vm.reinsert(cleaned_body, spanned) == original,
            "only_touched_region_shrank": cleaned_body in original or all(
                one.deleted in original for one in spanned),
        })

    # b) 整块级：原版整块 → 按登记的渲染字面逐段抠（纯删除）→ 必须等于现场清洗块
    recomputed_block, complaints = rulers.delete_spans(r.raw_text, r.applied)
    # 块级偏移不在 Applied 里，块级还原走 c) 的行级归属（每一行都要能对上）

    # c) 行级：diff 归属 + 两版逐行归属
    diff_text = rulers.build_diff(r.raw_text, r.text)
    minus, plus = rulers.split_diff(diff_text)
    spans = [one.rendered_deleted for one in r.applied]
    unexplained_plus, unexplained_minus = rulers.account_diff(minus, plus, spans)
    raw_lines = r.raw_text.splitlines()
    clean_lines = r.text.splitlines()
    rewrote, removed = rulers.classify_lines(raw_lines, clean_lines, spans,
                                             "".join(spans))

    # ---------- 尺自身的正控制（同一把尺扫原版必须命中，否则「命中 0」不算通过） ----------
    identity_clean = rulers.sweep(system_text, rulers.IDENTITY_PATTERNS)
    identity_raw = rulers.sweep(raw_block, rulers.IDENTITY_PATTERNS)
    action_clean_fmt, action_clean_unowned, action_clean_endorsed = \
        rulers.action_verdict(system_text)
    action_raw_fmt, action_raw_unowned, action_raw_endorsed = rulers.action_verdict(raw_block)

    # ---------- §10 点名字面：在最终 system 上扫 ----------
    forbidden_hits = {}
    for lit in FORBIDDEN_LITERALS:
        idx = [m.start() for m in re.finditer(re.escape(lit), system_text)]
        forbidden_hits[lit] = {
            "count": len(idx),
            "contexts": [L.short(system_text[max(0, i - 40):i + 60], 160) for i in idx[:3]],
        }

    # ---------- 保留项：在最终 system 上逐条回读 + 正控制 ----------
    protected_report = []
    for key, title, literals in rulers.PROTECTED:
        rows = [{"literal": lit,
                 "in_final_system": lit in system_text,
                 "count": system_text.count(lit),
                 "positive_control_in_original": lit in raw_block} for lit in literals]
        protected_report.append({
            "KEY": key, "说明": title, "items": rows,
            "all_present": all(r0["in_final_system"] for r0 in rows),
            "positive_controls_hit": all(r0["positive_control_in_original"] for r0 in rows),
            "verdict": "YES" if all(r0["in_final_system"] and r0["positive_control_in_original"]
                                    for r0 in rows) else "NO",
        })

    # ---------- 仍然带成对星号的那几行：逐条点名归属 ----------
    kept_rows = []
    for needle, why in rulers.ASTERISK_KEPT:
        kept_rows.append({"literal": needle, "为什么留着": why,
                          "in_final_system": needle in system_text})

    tok_final = tokenize_via_8090(system_text)
    tok_sola = tokenize_via_8090(sola_text)
    tok_base = tokenize_via_8090(base_text)

    spec_declared = {str(e["id"]): int(e.get("deleted_chars") or len(str(e.get("anchor") or "")))
                     for e in spec["edits"]}
    out = {
        "task_id": L.TASK_ID,
        "stages": ["9_official_system_runtime", "10_minimal_clean_runtime",
                   "16_prompt_token_ledger"],
        "collected_at_local": L.now_local(),
        "character_id": ident,
        "character_name": char_name,
        "labels": labels,
        "assembly_origin": "进程内用生产 context_builder.build + messages（与 :7860 同一套原语），"
                           "并与在线 /api/character/prompt 那一整块逐字对拍（见 live_block_equals_"
                           "inprocess）；两路都是只读。",
        "live_preview": live_preview,
        "live_system_text_equals_inprocess": (
            live_preview.get("system_text_live_sha256")
            == L.sha256_bytes(system_text.encode("utf-8"))),
        "inprocess_system_text_sha256": L.sha256_bytes(system_text.encode("utf-8")),
        "live_prompt_endpoint": prompt_ep,
        "live_block_equals_inprocess": bool(prompt_ep.get("sha256"))
                                       and prompt_ep.get("sha256") == sha_block_prompt,
        "live_block_sha256": prompt_ep.get("sha256"),
        "inprocess_mirror_of_live_block_sha256": sha_block_prompt,
        "inprocess_mirror_block_chars": len(block_prompt),
        "mirror_block_ends_with_sola_clean_block": block_prompt.endswith(clean_block),
        "SYSTEM_MESSAGE_COUNT": sum(1 for m in msgs if m.get("role") == "system"),
        "message_roles": [str(m.get("role")) for m in msgs],
        "system_blocks_present_in_order": [n for n, _ in blocks],
        "system_block_sizes": dict(blocks),
        "sola_is_last_block": bool(blocks) and blocks[-1][0] == "sola",
        "SOLA_RUNTIME_ACTIVE": "YES" if (sola_text.strip()
                                         and sola_text.strip() in system_text) else "NO",
        "VOICE_MINIMAL_ACTIVE": "YES" if sola_adapter.voice_minimal_enabled() else "NO",
        "sola_block": {
            "chars": len(sola_text), "bytes": len(sola_text.encode("utf-8")),
            "sha256": L.sha256_bytes(sola_text.encode("utf-8")),
            "equals_independent_recompute": sola_text == clean_block,
            "head": L.short(sola_text[:160], 200),
        },
        "block_sizes_vs_frozen": {
            "原版块_实测": {"chars": len(raw_block), "bytes": len(raw_block.encode("utf-8")),
                            "sha256": L.sha256_bytes(raw_block.encode("utf-8"))},
            "原版块_上一轮冻结": {"chars": RAW_BLOCK_CHARS, "bytes": RAW_BLOCK_BYTES},
            "原版尺寸一致": (len(raw_block) == RAW_BLOCK_CHARS
                             and len(raw_block.encode("utf-8")) == RAW_BLOCK_BYTES),
            "清洗块_实测": {"chars": len(clean_block), "bytes": len(clean_block.encode("utf-8")),
                            "sha256": L.sha256_bytes(clean_block.encode("utf-8"))},
            "清洗块_上一轮冻结": {"chars": CLEAN_BLOCK_CHARS, "bytes": CLEAN_BLOCK_BYTES},
            "清洗尺寸一致": (len(clean_block) == CLEAN_BLOCK_CHARS
                             and len(clean_block.encode("utf-8")) == CLEAN_BLOCK_BYTES),
            "渲染口径一致性": "ember.render(不清洗) 与 voice_minimal.render 的 raw_text 同一函数同一口径",
        },
        "character_block_present": {
            "人物名在 system 里": bool(char_name) and char_name in system_text,
            "profile_block_chars": len(ctx.profile_text or ""),
            "persona_block_chars": len(ctx.persona_text or ""),
            "policy_block_chars": len(ctx.policy_text or ""),
            "memory_lines": len(ctx.memory_lines), "passage_lines": len(ctx.passage_lines),
            "session_summary_chars": len(ctx.summary_text or ""),
            "history_rows": len([m for m in msgs if m.get("role") != "system"]),
        },
        "forbidden_literals_scan_on_final_system": forbidden_hits,
        "forbidden_literals_hit_total": sum(v["count"] for v in forbidden_hits.values()),
        "identity_patterns_sweep_on_final_system": [
            {"pattern": p, "hits": len(h), "sample": h[:2]} for p, h in identity_clean],
        "identity_positive_control_on_original": [
            {"pattern": p, "hits_on_original": len(h)} for p, h in identity_raw],
        "identity_ruler_alive": any(len(h) for _p, h in identity_raw),
        "action_ruler_final_system": {
            "格式尺命中": action_clean_fmt,
            "归不进名单的星号行": action_clean_unowned,
            "仍在位的盖章例句": action_clean_endorsed},
        "action_ruler_positive_control_original": {
            "格式尺命中": action_raw_fmt,
            "归不进名单的星号行": action_raw_unowned,
            "仍在位的盖章例句": action_raw_endorsed},
        "action_ruler_alive": bool(action_raw_endorsed) or bool(action_raw_unowned),
        "asterisk_lines_explicitly_kept": kept_rows,
        "protected_preserved_on_final_system": protected_report,
        "clean_block_recomputation": {
            "path_a_module_roundtrip": module_roundtrip,
            "path_a_all_exact": all(m["reinsert_equals_original"] for m in module_roundtrip),
            "path_b_delete_spans_from_original_equals_live_clean":
                recomputed_block.strip() == clean_block,
            "path_b_complaints": complaints,
            "path_c_diff_minus_lines": len(minus), "path_c_diff_plus_lines": len(plus),
            "path_c_unexplained_plus": unexplained_plus,
            "path_c_unexplained_minus": unexplained_minus,
            "path_c_unattributable_clean_lines": rewrote,
            "path_c_unattributable_raw_lines": removed,
            "original_chars": len(raw_block), "live_clean_chars": len(clean_block),
            "deleted_chars_total": len(raw_block) - len(clean_block),
            "spec_declared_deleted_total": sum(spec_declared.values()),
            "categories_in_spec": sorted({str(e.get("category")) for e in spec["edits"]}),
            "ids_in_spec": sorted(str(e.get("id")) for e in spec["edits"]),
            "deleted_by_category": r.facts.get("voice_minimal_deleted_by_category"),
            "OTHER_EMBER_CONTENT_REWRITTEN": (
                "NO" if (all(m["reinsert_equals_original"] for m in module_roundtrip)
                         and recomputed_block.strip() == clean_block and not complaints
                         and not unexplained_plus and not unexplained_minus
                         and not rewrote and not removed) else "YES_OR_UNPROVEN"),
        },
        "span_report": [one.as_dict() for one in r.applied],
        "spec_sha256": L.sha256_file(spec_path),
        "ember_source_sha256": L.file_fact(
            "runtime/character/sola_v2_ember/source/Sola V2 Ember.json"),
        "skipped_st_only_pieces": r.skipped,
        "token_ledger": {
            "estimator": "本仓库 CJK/ASCII 比例口径（context_builder.estimate_tokens）",
            "real_tokenizer": "llama.cpp /tokenize（Gemma4 自己的分词器，只分词不生成）",
            "BASE_SYSTEM_PROMPT_TOKENS_est": context_builder.estimate_tokens(base_text),
            "SOLA_PROMPT_TOKENS_est": context_builder.estimate_tokens(sola_text),
            "FINAL_PROMPT_TOKENS_est": context_builder.estimate_tokens(system_text),
            "BASE_SYSTEM_PROMPT_TOKENS": tok_base.get("count"),
            "SOLA_PROMPT_TOKENS": tok_sola.get("count"),
            "FINAL_PROMPT_TOKENS": tok_final.get("count"),
            "tokenizer_probe_ok": [tok_base.get("ok"), tok_sola.get("ok"), tok_final.get("ok")],
            "note": "FINAL 的实测 token 不等于单次请求的 prompt_tokens（后者还要加对话模板与"
                    "这一轮的历史）；真实单次数值见 single_call_proof.json。",
        },
        "block_order_note": (
            "生产块序是 policy→profile→persona→canon→retrieval→relationship→memory→"
            "session_summary→tools→sola：Sola 永远在最后一条 system 的末尾，"
            "且全程只有一条 system message。任务 §9 字面写的『人物→记忆→最近上下文→正常规则→Sola』"
            "把正常规则排在最近上下文之后，与本仓库既有装配序（正常规则在最前）不同；"
            "本轮按 §1『不修改范围外事物』沿用现网顺序，只登记不改。"),
    }
    path = L.write_json("runtime_prompt_proof.json", out)
    print("WROTE", path)
    print("SYSTEM_MESSAGE_COUNT =", out["SYSTEM_MESSAGE_COUNT"],
          "live =", live_preview.get("SYSTEM_MESSAGE_COUNT_live"))
    print("blocks =", out["system_blocks_present_in_order"])
    print("sola_last =", out["sola_is_last_block"],
          "SOLA_RUNTIME_ACTIVE =", out["SOLA_RUNTIME_ACTIVE"],
          "live_equals_inprocess =", out["live_block_equals_inprocess"])
    print("sola_block =", out["sola_block"]["chars"], "chars",
          out["sola_block"]["bytes"], "bytes",
          "==recompute", out["sola_block"]["equals_independent_recompute"])
    print("sizes_vs_frozen:",
          "raw_ok" if out["block_sizes_vs_frozen"]["原版尺寸一致"] else "RAW_MISMATCH",
          "clean_ok" if out["block_sizes_vs_frozen"]["清洗尺寸一致"] else "CLEAN_MISMATCH")
    print("forbidden hits =", {k: v["count"] for k, v in forbidden_hits.items() if v["count"]} or "NONE")
    print("identity ruler alive =", out["identity_ruler_alive"],
          "hits_on_final =", sum(len(x["sample"]) and x["hits"]
                                 for x in out["identity_patterns_sweep_on_final_system"]))
    print("identity residual =",
          [(x["pattern"], x["hits"], x["sample"]) for x in
           out["identity_patterns_sweep_on_final_system"] if x["hits"]])
    print("action final =", out["action_ruler_final_system"],
          "| control(raw) =", out["action_ruler_positive_control_original"])
    print("protected =", [(x["KEY"], x["verdict"]) for x in protected_report])
    print("OTHER_EMBER_CONTENT_REWRITTEN =",
          out["clean_block_recomputation"]["OTHER_EMBER_CONTENT_REWRITTEN"],
          "unexplained(+,-) =", len(unexplained_plus), len(unexplained_minus),
          "unattributed(clean,raw) =", len(rewrote), len(removed), "complaints =", complaints)
    print("tokens =", json.dumps(out["token_ledger"], ensure_ascii=False)[:400])
    print("live_preview rows =", live_preview.get("roles_in_order"),
          "prompt_ep_sha =", (prompt_ep.get("sha256") or "")[:16])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
