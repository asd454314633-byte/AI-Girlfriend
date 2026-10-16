"""V2 开工前的现场事实：四服务是否在、人物库里谁还在、指针指向谁。

只读。不写指针、不改库、不发任何 POST。
"""
from __future__ import annotations

import json
import sqlite3
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CHAR_DB = ROOT / "runtime" / "character" / "data" / "characters.sqlite3"
POINTER = ROOT / "runtime" / "character" / "data" / "active_character.json"


def http_get(url: str, timeout: float = 6.0) -> tuple[int | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:300]
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def main() -> None:
    print("POINTER", POINTER.read_text(encoding="utf-8") if POINTER.exists() else "MISSING")
    print("CHAR_DB_EXISTS", CHAR_DB.exists(), CHAR_DB.stat().st_size if CHAR_DB.exists() else 0)

    if CHAR_DB.exists():
        conn = sqlite3.connect(
            f"file:{CHAR_DB.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        tables = [r[0] for r in conn.execute(
            "select name from sqlite_master where type='table' order by name")]
        print("TABLES", tables)
        for table in tables:
            cols = [r[1] for r in conn.execute(f"pragma table_info({table})")]
            n = conn.execute(f"select count(*) from {table}").fetchone()[0]
            print(f"  {table} rows={n} cols={cols}")
        people = []
        if "characters" in tables:
            cols = [r[1] for r in conn.execute("pragma table_info(characters)")]
            rows = conn.execute("select * from characters").fetchall()
            for row in rows:
                one = dict(zip(cols, tuple(row)))
                persona = str(one.get("persona") or one.get("persona_text") or "")
                people.append({k: one.get(k) for k in ("character_id", "display_name", "archived",
                                                        "status", "updated_at") if k in one})
                people[-1]["persona_chars"] = len(persona.strip())
        print("PEOPLE", json.dumps(people, ensure_ascii=False, indent=2))
        conn.close()

    for port, path in ((7860, "/api/character/list"), (8090, "/health"),
                       (7860, "/api/character/prompt"), (8765, "/health")):
        status, head = http_get(f"http://127.0.0.1:{port}{path}")
        print(f"HTTP {port}{path} ->", status, head[:200].replace("\n", " "))

    try:
        import psutil
        live = {}
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == "LISTEN" and conn.laddr.port in (7860, 8090, 8765, 8010):
                live.setdefault(str(conn.laddr.port), set()).add(conn.pid)
        print("LISTENERS", {k: sorted(v) for k, v in live.items()})
    except Exception as exc:  # noqa: BLE001
        print("LISTENERS_ERR", type(exc).__name__, exc)


if __name__ == "__main__":
    main()
