# -*- coding: utf-8 -*-
"""书库管理后端单元测试（隔离环境：临时 READING_ROOT / 独立 SQLite，不碰生产书库）。"""
import json
import os
import sys
import tempfile
from pathlib import Path

import fastapi.testclient as _tc  # noqa: E402
from fastapi import FastAPI  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="ra_test_"))
os.environ["READING_ROOT"] = str(tmp / "reading")
os.environ["GIRLFRIEND_READING_DB"] = str(tmp / "reading.sqlite3")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime", "ui", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime"))
import reading.documents as rdocs  # noqa: E402
import reading_admin  # noqa: E402

# Isolation includes the network: never stop the real 8765 during unit tests.
reading_admin._stop_speaking = lambda: True

rdocs.ensure_dirs()

doc_a = rdocs.import_text("粘贴文本", "第一章 开始\n这是第一本书的正文，内容足够长可以分块。" * 20)
doc_b = rdocs.import_text("粘贴文本", "第一章 另一本\n这是第二本书的正文，标题重复但内容不同。" * 20)
doc_c = rdocs.import_text("小说章节", "第一章 开始\n这是第一本书的正文，内容足够长可以分块。" * 20)  # 与 A 内容相同
doc_d = rdocs.import_text("独一本书", "第一章 独白\n孤本内容。" * 5)
rdocs.set_progress(doc_a["document_id"], 0, 0, "READING")
rdocs.set_progress(doc_d["document_id"], 0, 0, "DONE")

app = FastAPI()
app.include_router(reading_admin.router)
client = _tc.TestClient(app)

fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


# 1. 列表 + 重复检测
r = client.get("/api/reading/library/manage")
data = r.json()
check("manage list 4 docs", data["total"] == 4, data)
dup_titles = [d for d in data["documents"] if d["dup_title_group"]]
dup_contents = [d for d in data["documents"] if d["dup_content_group"]]
check("dup title group detected", len(dup_titles) == 2, dup_titles)
check("dup content group detected", len(dup_contents) == 2, dup_contents)
check("active doc surfaced", data["active_document_id"] == doc_a["document_id"], data["active_document_id"])

# 2. 搜索 / 排序 / 筛选
r = client.get("/api/reading/library/manage", params={"q": "独一"})
check("search by title", r.json()["total"] == 1)
r = client.get("/api/reading/library/manage", params={"status": "DONE"})
check("filter DONE", r.json()["total"] == 1 and r.json()["documents"][0]["document_id"] == doc_d["document_id"])
r = client.get("/api/reading/library/manage", params={"sort": "chars"})
check("sort by chars", r.json()["documents"][0]["char_count"] >= r.json()["documents"][-1]["char_count"])

# 3. 重命名
r = client.post("/api/reading/library/rename", json={"document_id": doc_b["document_id"], "title": "粘贴文本·第二批"})
check("rename ok", r.json().get("ok") is True)
check("rename persisted", rdocs._read_index(doc_b["document_id"])["title"] == "粘贴文本·第二批")
r = client.post("/api/reading/library/rename", json={"document_id": doc_b["document_id"], "title": ""})
check("rename empty rejected", r.status_code == 400)

# 4. 正在朗读：未确认 → 拦截；确认 → 停朗读 + 移入回收站
reading_admin._stop_speaking = lambda: False
r = client.post("/api/reading/library/trash", json={"document_ids": [doc_a["document_id"]], "confirm_active": True})
check("failed stop preserves active book", r.json()["moved"] == 0 and
      Path(rdocs._lib_path(doc_a["document_id"])).exists() and
      rdocs.get_progress(doc_a["document_id"])["status"] == "READING", r.json())
reading_admin._stop_speaking = lambda: True
r = client.post("/api/reading/library/trash", json={"document_ids": [doc_a["document_id"]]})
check("active reading blocked without confirm", r.json().get("reason") == "ACTIVE_READING_CONFIRM", r.json())
r = client.post("/api/reading/library/trash", json={"document_ids": [doc_a["document_id"]], "confirm_active": True})
res = r.json()
check("active reading trash with confirm", res.get("moved") == 1, res)
check("text moved to trash", not (Path(os.environ["READING_ROOT"]) / "library" / f"{doc_a['document_id']}.txt").exists())

# 5. 批量删除（按 ID；重复标题互不影响）
r = client.post("/api/reading/library/trash", json={"document_ids": [doc_b["document_id"], doc_c["document_id"], "doc_nonexist00"]})
res = r.json()
check("batch trash moved 2", res.get("moved") == 2, res)
check("batch skips missing id", any(x.get("reason") == "DOCUMENT_NOT_FOUND" for x in res["results"]))

# 6. 回收站列表 / 恢复 / 彻底清除
r = client.get("/api/reading/library/trash")
ops = r.json()["ops"]
ops = [op for op in ops if op["items"]]
check("trash lists ops", len(ops) == 2, len(ops))
first_op = next(op["op_id"] for op in ops
                if any(i["document_id"] == doc_a["document_id"] for i in op["items"]))
check("trash shows progress rows", any(item["progress_rows"] >= 1 for op in ops for item in op["items"] if op["op_id"] == first_op), [(op["op_id"], [i.get("progress_rows") for i in op["items"]]) for op in ops])
collision = Path(rdocs._lib_path(doc_a["document_id"]))
collision.write_text("new imported content", encoding="utf-8")
r = client.post("/api/reading/library/restore", json={"op_ids": [first_op]})
check("restore collision preserves new book and trash", not r.json()["ok"] and
      collision.read_text(encoding="utf-8") == "new imported content" and
      (reading_admin._TRASH_DIR / first_op / "library" / collision.name).exists())
collision.unlink()
real_restore = reading_admin._reinsert_progress
def failed_restore(_):
    raise OSError("injected DB failure")
reading_admin._reinsert_progress = failed_restore
r = client.post("/api/reading/library/restore", json={"op_ids": [first_op]})
check("restore DB failure returns files to trash", not r.json()["ok"] and
      not collision.exists() and
      (reading_admin._TRASH_DIR / first_op / "library" / collision.name).exists())
reading_admin._reinsert_progress = real_restore
r = client.post("/api/reading/library/restore", json={"op_ids": [first_op]})
check("restore ok", r.json()["ok"] is True, r.json())
check("restore brings text back", (Path(os.environ["READING_ROOT"]) / "library" / f"{doc_a['document_id']}.txt").exists())
prog = rdocs.get_progress(doc_a["document_id"])
check("restore brings progress back", prog["status"] in ("READING", "PAUSED"), prog)
check("restored doc listed again", rdocs._read_index(doc_a["document_id"]).get("document_id") == doc_a["document_id"])

batch_op = next(op["op_id"] for op in ops if op["op_id"] != first_op)
r = client.post("/api/reading/library/purge", json={"op_ids": [batch_op]})
check("purge requires confirm", r.status_code == 400)
r = client.post("/api/reading/library/purge", json={"op_ids": [batch_op], "confirm": True})
check("purge ok", r.json()["ok"] is True, r.json())
r = client.post("/api/reading/library/trash", json={"document_ids": [doc_a["document_id"]], "confirm_active": True})
r = client.post("/api/reading/library/purge", json={"op_ids": [r.json()["op_id"]], "confirm": True})
check("re-trash + purge ok", r.json()["ok"] is True)

# 7. 审计
r = client.get("/api/reading/library/audit")
ops_names = {rec["op"] for rec in r.json()["records"]}
check("audit has trash/restore/purge/rename", {"trash", "restore", "purge", "rename"} <= ops_names, ops_names)

print()
print("RESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
