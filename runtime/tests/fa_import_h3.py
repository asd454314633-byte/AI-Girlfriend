# -*- coding: utf-8 -*-
"""H3 素材导入驱动：走 7860 完整生成链 inspect -> create -> making 轮询监控。

用法:
  python fa_import_h3.py <video_path> <display_name> <monitor_log_path>
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:7860"
AVATAR_8010 = "http://127.0.0.1:8010"


def multipart_upload(path):
    boundary = uuid.uuid4().hex
    data = Path(path).read_bytes()
    name = Path(path).name
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        f"Content-Type: video/mp4\r\n\r\n"
    ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        BASE + "/api/avatar/inspect", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def post_json(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def get_json(url, timeout=30):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:160]}


def main():
    video, display_name, monitor_log = sys.argv[1], sys.argv[2], sys.argv[3]
    log = open(monitor_log, "a", encoding="utf-8")

    def emit(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    emit(f"MATERIAL video={video} sha256_check=skip name={display_name}")

    # Step 1: inspect
    ins = multipart_upload(video)
    emit(f"INSPECT {json.dumps(ins, ensure_ascii=False)[:300]}")
    data = ins
    handle = data.get("句柄") or data.get("handle") or (data.get("数据") or {}).get("句柄")
    if not handle:
        emit("IMPORT_RESULT = FAIL step=INSPECT no_handle")
        return 1

    # Step 2: create
    cr = post_json("/api/avatar/create", {"显示名": display_name, "句柄": handle})
    emit(f"CREATE {json.dumps(cr, ensure_ascii=False)[:300]}")
    cdata = cr
    making_id = (cdata.get("制作编号") or cdata.get("making_id")
                 or (cdata.get("数据") or {}).get("制作编号"))
    if not making_id:
        emit("IMPORT_RESULT = FAIL step=CREATE")
        return 1

    # Step 3: making 轮询（监控主循环）
    result = None
    t0 = time.time()
    last_phase = None
    while time.time() - t0 < 1500:
        time.sleep(5)
        mk = get_json(BASE + "/api/avatar/making")
        line = json.dumps(mk, ensure_ascii=False)
        if line != last_phase:
            emit(f"MAKING {line[:400]}")
            last_phase = line
        blob = json.dumps(mk, ensure_ascii=False)
        if '"done": true' in blob or '"完成": true' in blob or '已生成' in blob or '做完' in blob:
            result = "done"
        if '"failed"' in blob or "失败" in blob:
            result = "failed"
        if result == "done":
            break
        if result == "failed":
            emit("IMPORT_RESULT = FAIL step=MAKING")
            return 1

    # 落盘最终 registry 状态
    lst = get_json(BASE + "/api/avatar/list")
    emit(f"FINAL_LIST {json.dumps(lst, ensure_ascii=False)[:600]}")
    emit("IMPORT_RESULT = " + ("PASS" if result == "done" else "TIMEOUT_OR_UNKNOWN"))
    return 0 if result == "done" else 2


if __name__ == "__main__":
    sys.exit(main())
