"""谁在人物库里、谁被归档了、以及指针是怎么掉到 default 的——全部只读。"""
from __future__ import annotations

import json
import sqlite3
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CHAR_DB = ROOT / "runtime" / "character" / "data" / "characters.sqlite3"


def main() -> None:
    conn = sqlite3.connect(f"file:{CHAR_DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    print("== character_profile ==")
    for row in conn.execute("select * from character_profile order by updated_at"):
        one = dict(row)
        print(json.dumps({
            "id": one.get("character_id"),
            "name": one.get("display_name"),
            "role": one.get("nickname"),
            "avatar": one.get("avatar_id"),
            "active": one.get("active"),
            "persona_chars": len(str(one.get("persona_text") or "").strip()),
            "relationship": str(one.get("relationship") or "")[:40],
            "created": one.get("created_at"),
            "updated": one.get("updated_at"),
        }, ensure_ascii=False))

    print("== change_journal tail 30 ==")
    for row in conn.execute("select * from change_journal order by seq desc limit 30"):
        one = dict(row)
        print(one.get("seq"), one.get("at"), one.get("action"),
              one.get("subject_id"), one.get("character_id"),
              str(one.get("detail") or "")[:120])

    print("== character_meta ==")
    for row in conn.execute("select * from character_meta"):
        print(dict(row))
    conn.close()

    with urllib.request.urlopen("http://127.0.0.1:7860/api/character/list", timeout=10) as r:
        print("== /api/character/list ==")
        print(json.dumps(json.loads(r.read()), ensure_ascii=False)[:2000])


if __name__ == "__main__":
    main()
