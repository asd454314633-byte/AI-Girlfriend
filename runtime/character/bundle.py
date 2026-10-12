# -*- coding: utf-8 -*-
"""把一个人物整套搬走 / 整套搬回来（任务 §14 的「导出 / 导入」）。

一个文件装得下：档案 + 人物台词 + 设定层（含出处与原文引句）+ 导入过的书与其段落
+ 长期记忆。带 schema_version，导入时版本对不上会如实说，不会装作成功。

三条边界：
  * 这一模块不认得任何模型或语音提供方，也不联网（§0）；
  * 写盘只写在项目目录里（默认 ``runtime/character/data/bundles/``），
    不往用户主目录或系统位置放东西；
  * 导入默认「合并不覆盖」：同一条设定/同一条记忆按归一化文本认，认出来就只抬
    把握度，不会越搬越多；要整本替换得明说 ``replace=True``。

给用户看的字段一律中文句子，不出现内部名词（导出文件名也算给人看的东西）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import canon, importer, store
from runtime.memory import service as memory_service

HERE = Path(__file__).resolve().parent
BUNDLE_DIR = HERE / "data" / "bundles"
BUNDLE_KIND = "character_bundle"

_SAFE = re.compile(r"[^\w\u4e00-\u9fff.\-]+")


def bundle_format_ok(name: str) -> bool:
    """文件名只能是普通字符，防止把路径写到外面去。"""
    body = str(name or "").strip()
    return bool(body) and "/" not in body and "\\" not in body and ".." not in body


def safe_filename(character_id: str, *, suffix: str = "") -> str:
    body = _SAFE.sub("", (character_id or "default")).strip(".") or "default"
    tail = f"-{_SAFE.sub('', suffix).strip('.')}" if suffix else ""
    return f"character-{body}{tail}.json"


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

def export_character(character_id: str | None = None, *,
                     include_memories: bool = True,
                     include_sources: bool = True,
                     include_archived: bool = True,
                     character_db_path: Path | str | None = None,
                     memory_db_path: Path | str | None = None) -> dict[str, Any]:
    """把一个人物整套读成一个可序列化字典（不写盘）。"""
    cdb = Path(character_db_path) if character_db_path else store.DB_PATH
    mdb = Path(memory_db_path) if memory_db_path else memory_service.DB_PATH
    character = character_id or store.get_active_character(db_path=cdb)
    profile = store.get_profile(character, db_path=cdb)
    facts = canon.list_facts(character, include_archived=include_archived, db_path=cdb)
    payload: dict[str, Any] = {
        "kind": BUNDLE_KIND,
        "schema_version": store.SCHEMA_VERSION,
        "契约版本": getattr(store, "SCHEMA_VERSION", 1),
        "导出时间": store.now_iso(),
        "人物": character,
        "profile": _profile_dict(profile),
        "persona_text": store.get_persona(character, db_path=cdb),
        "canon_facts": facts,
    }
    if include_sources:
        payload["documents"] = _documents(character, cdb)
    if include_memories:
        memory_service.init_db(mdb)
        payload["memory"] = memory_service.export(character, include_archived=include_archived,
                                                  db_path=mdb)
    counts = {
        "设定条数": len(facts),
        "原著条数": sum(1 for one in facts if one.get("basis") == "canon"),
        "推断条数": sum(1 for one in facts if one.get("basis") == "inferred"),
        "书本数": len(payload.get("documents") or []),
        "记忆条数": int((payload.get("memory") or {}).get("条数") or 0),
    }
    payload["条数"] = counts
    payload["说明"] = _export_note(profile, counts)
    return payload


def _profile_dict(profile: Any) -> dict[str, Any]:
    if profile is None:
        return {}
    return {
        "character_id": profile.character_id,
        "display_name": profile.display_name,
        "nickname": profile.nickname,
        "pronoun": profile.pronoun,
        "speaking_style": profile.speaking_style,
        "relationship": profile.relationship,
        "boundary": profile.boundary,
        "greeting": profile.greeting,
        "likes": list(profile.likes or []),
        "dislikes": list(profile.dislikes or []),
        "avatar_id": profile.avatar_id,
        "preferred_providers": dict(profile.preferred_providers or {}),
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


def _documents(character: str, cdb: Path) -> list[dict[str, Any]]:
    conn = store.connect(cdb)
    try:
        rows = conn.execute("SELECT * FROM source_document WHERE character_id = ?"
                            " ORDER BY created_at", (character,)).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            parts = conn.execute("SELECT ordinal, chapter, heading_path, text,"
                                 " char_start, char_end FROM source_part"
                                 " WHERE document_id = ? ORDER BY ordinal",
                                 (str(row["document_id"]),)).fetchall()
            one = {key: str(row[key]) for key in row.keys() if key != "document_id"}
            one["document_id"] = str(row["document_id"])
            one["parts"] = [dict(item) for item in parts]
            out.append(one)
        return out
    finally:
        conn.close()


def _export_note(profile: Any, counts: dict[str, int]) -> str:
    who = getattr(profile, "display_name", "") or getattr(profile, "character_id", "") or "这个人"
    return (f"{who} 整套都在这儿了：{counts['设定条数']} 条资料"
            f"（{counts['原著条数']} 条带原文出处、{counts['推断条数']} 条是按说法统计推的）、"
            f"{counts['书本数']} 本书、{counts['记忆条数']} 条记得的话。")


def write_bundle(character_id: str | None = None, *, file_name: str = "",
                 directory: Path | str | None = None, **kwargs: Any) -> dict[str, Any]:
    """导出并落盘成一个文件，返回路径和给用户看的那句话。"""
    payload = export_character(character_id, **kwargs)
    folder = Path(directory) if directory else BUNDLE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    name = file_name or safe_filename(str(payload["人物"]))
    if not bundle_format_ok(name):
        return {"ok": False, "说明": "这个文件名我写不了，换一个试试。"}
    target = folder / name
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["文件"] = str(target)
    payload["说明"] = str(payload["说明"]) + f" 已写到「{target.name}」。"
    return {"ok": True, "path": str(target), "说明": payload["说明"], "条数": payload["条数"]}


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------

def read_bundle(payload: Any, *, path: Path | str = "") -> dict[str, Any]:
    """把各种传法（字典 / JSON 文本 / 文件路径）统一成一个字典。"""
    data = payload
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8")
    if isinstance(data, (str, Path)):
        body = Path(data)
        text = body.read_text(encoding="utf-8") if str(data) and body.exists() else str(data)
        try:
            data = json.loads(text)
        except ValueError:
            return {"_坏": "这份内容读不出来，像是被改坏了。"}
    if not isinstance(data, dict):
        return {"_坏": "这份内容不像一个人物的整套备份。"}
    return data


def import_bundle(payload: Any, *, character_id: str | None = None,
                  replace: bool = False, set_active: bool = True,
                  include_memories: bool = True, include_sources: bool = True,
                  character_db_path: Path | str | None = None,
                  memory_db_path: Path | str | None = None) -> dict[str, Any]:
    """把一份整套备份搬回来。返回的「说明」是可以直接念给用户听的话。"""
    data = read_bundle(payload)
    if data.get("_坏"):
        return {"ok": False, "说明": str(data["_坏"])}
    if str(data.get("kind") or "") != BUNDLE_KIND:
        return {"ok": False, "说明": "这份内容不是人物的整套备份，不敢往库里写。"}
    cdb = Path(character_db_path) if character_db_path else store.DB_PATH
    mdb = Path(memory_db_path) if memory_db_path else memory_service.DB_PATH
    store.init_db(cdb)
    target = (character_id or str(data.get("人物") or "")).strip() or store.DEFAULT_CHARACTER_ID
    version = int(data.get("schema_version") or 0)
    notes: list[str] = []
    if version and version > store.SCHEMA_VERSION:
        return {"ok": False, "说明": "这份备份是新版本程序导出的，当前版本读不了它。"}
    if version and version < store.SCHEMA_VERSION:
        notes.append("这份备份是旧版本存的，缺的项目按空处理")

    # 1) 档案 + 人物台词
    profile = _as_profile(data.get("profile"), target)
    store.save_profile(profile, db_path=cdb, persona=str(data.get("persona_text") or ""))
    if replace:
        canon.delete_character_facts(target, db_path=cdb)
    facts_in = list(data.get("canon_facts") or [])
    fact_report = _restore_facts(facts_in, target, cdb, replace=replace)

    # 2) 书与段落（按校验和认，同一本书不会导两次）
    docs = 0
    if include_sources:
        docs = _restore_documents(list(data.get("documents") or []), target, cdb)

    # 3) 长期记忆（走记忆模块自己的导入，查重规则只有一处）
    memories = {"新增": 0, "合并": 0}
    if include_memories and data.get("memory"):
        result = memory_service.import_(data["memory"], character_id=target, merge=not replace,
                                        db_path=mdb)
        memories = {"新增": int(result.get("新增") or 0),
                    "合并": int(result.get("并入已有") or 0)}
        if not result.get("ok"):
            notes.append("记忆那一半没搬回来：" + str(result.get("说明") or ""))

    if set_active:
        store.set_active_character(target, db_path=cdb)
    # 检索索引由库上的触发器自己跟着写，不需要（也不该）在这里重建一遍

    counts = {
        "设定条数": fact_report["新增"] + fact_report["合并"],
        "其中新加": fact_report["新增"],
        "原著条数": sum(1 for one in facts_in if str(one.get("basis")) == "canon"),
        "推断条数": sum(1 for one in facts_in if str(one.get("basis")) == "inferred"),
        "书本数": docs,
        "记忆条数": memories["新增"] + memories["合并"],
    }
    who = profile.display_name or target
    note = (f"{who} 已经搬回来了：{counts['设定条数']} 条资料（{counts['原著条数']} 条带原文出处、"
            f"{counts['推断条数']} 条是推断）、{counts['书本数']} 本书、"
            f"{counts['记忆条数']} 条记得的话。")
    if notes:
        note += "另外：" + "；".join(notes) + "。"
    banned = rules_banned(note)
    if banned:
        note = "人物已经搬回来了，界面文案要检查一下。"
    return {"ok": True, "character_id": target, "条数": counts, "说明": note,
            "提示": banned, "版本": {"备份": version, "当前": store.SCHEMA_VERSION}}


def rules_banned(*texts: Any) -> list[str]:
    """界面/文案口径自检（同一个函数在 rules 里，这里只是给调用方少写一次导入）。"""
    from . import rules as _rules
    return _rules.find_banned(*texts)


def _as_profile(raw: Any, character_id: str) -> Any:
    from .store import CharacterProfile
    body = raw if isinstance(raw, dict) else {}
    return CharacterProfile(
        character_id=character_id,
        display_name=str(body.get("display_name") or ""),
        nickname=str(body.get("nickname") or ""),
        pronoun=str(body.get("pronoun") or ""),
        speaking_style=str(body.get("speaking_style") or ""),
        relationship=str(body.get("relationship") or ""),
        boundary=str(body.get("boundary") or ""),
        greeting=str(body.get("greeting") or ""),
        likes=list(body.get("likes") or []),
        dislikes=list(body.get("dislikes") or []),
        avatar_id=str(body.get("avatar_id") or ""),
        preferred_providers=dict(body.get("preferred_providers") or {}),
    )


def _restore_facts(rows: list[dict[str, Any]], character_id: str, cdb: Path, *,
                   replace: bool) -> dict[str, int]:
    added = merged = skipped = 0
    for one in rows:
        text = str(one.get("text") or "").strip()
        if not text:
            skipped += 1
            continue
        dimension = str(one.get("dimension") or "")
        if dimension and not canon.is_dimension(dimension):
            dimension = ""            # 老备份里的维度名不认了就留空，不整条丢掉
        result = canon.add_fact(
            text, character_id=character_id, kind=str(one.get("kind") or "setting"),
            origin=str(one.get("origin") or "manual"),
            dimension=dimension, basis=str(one.get("basis") or "canon"),
            document_id=str(one.get("document_id") or ""),
            source_id=str(one.get("source_id") or one.get("document_id") or ""),
            chunk_id=str(one.get("chunk_id") or ""), location=str(one.get("location") or ""),
            evidence=str(one.get("evidence") or ""),
            ordinal=int(one.get("ordinal") or 0), weight=int(one.get("weight") or 100),
            confidence=float(one.get("confidence") or 0.9),
            user_editable=bool(one.get("user_editable", True)), db_path=cdb)
        if not result.get("ok"):
            skipped += 1
            continue
        if result.get("合并了"):
            merged += 1
        else:
            added += 1
    return {"新增": added, "合并": merged, "跳过": skipped}


def _restore_documents(rows: list[dict[str, Any]], character_id: str, cdb: Path) -> int:
    """书和段落：正文逐段重新登记（importer 自己按校验和去重），不直接 INSERT。"""
    conn = store.connect(cdb)
    count = 0
    try:
        for doc in rows:
            document_id = str(doc.get("document_id") or "")
            if not document_id:
                continue
            seen = conn.execute("SELECT 1 FROM source_document WHERE document_id = ?",
                                (document_id,)).fetchone()
            if seen is not None:
                count += 1
                continue
            title = str(doc.get("title") or "导入的正文")
            body = "\n\n".join(str(part.get("text") or "")
                               for part in list(doc.get("parts") or []))
            if not body.strip():
                continue
            result = importer.import_text(body, title=title, character_id=character_id,
                                          db_path=cdb)
            if result.get("ok"):
                count += 1
    finally:
        conn.close()
    return count


__all__ = ["BUNDLE_DIR", "BUNDLE_KIND", "safe_filename", "bundle_format_ok",
           "export_character", "write_bundle", "read_bundle", "import_bundle"]
