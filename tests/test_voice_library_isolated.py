# -*- coding: utf-8 -*-
"""音色库后端单元测试（隔离环境：临时 VOICES_ROOT / 状态文件，不碰生产音色库与 higgs）。"""
import io
import json
import os
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
import fastapi.testclient as _tc
from fastapi import FastAPI

tmp = Path(tempfile.mkdtemp(prefix="vl_test_"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime", "ui", "app"))

import voice_library_admin as vla  # noqa: E402
vla._higgs_worker_up = lambda: False  # never probe production in isolated tests
vla._voice_mutation_idle = lambda: True

# 重定向全部落盘位置到临时目录（必须在调用任何路由之前）
vla.VOICES_ROOT = tmp / "voices"
vla.PROFILES_DIR = vla.VOICES_ROOT / "profiles"
vla.TRASH_DIR = vla.VOICES_ROOT / "trash"
vla.XIAOYAN_META = vla.VOICES_ROOT / "xiaoyan.json"
vla.HIGGS_ACTIVE_FILE = tmp / "windows" / "active_voice_higgs.json"

app = FastAPI()
app.include_router(vla.router)
client = _tc.TestClient(app)

fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def make_wav(seconds=5.0, rate=24000, freq=220.0, clip=False):
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    amp = 32767.0 if clip else 8000.0
    pcm = (amp * np.sin(2 * np.pi * freq * t)).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())
    return buf.getvalue()


# 1. 小烟系统音色自动登记 + 受保护
r = client.get("/api/voices")
data = r.json()
check("xiaoyan seeded", any(v["voice_id"] == "xiaoyan" for v in data["voices"]))
check("active defaults to xiaoyan", data["active_higgs"] == "xiaoyan")
support = data["backend_support"]
check("higgs clone true / qwen3 fish false", support["higgs"]["clone"] and not support["qwen3"]["clone"] and not support["fish"]["clone"])
r = client.post("/api/voices/xiaoyan/ref_text", json={"ref_text": "改写小烟"})
check("xiaoyan ref_text protected", r.status_code == 400)
r = client.delete("/api/voices/xiaoyan")
check("xiaoyan delete protected", r.status_code == 400)

# 2. 上传：合法 WAV
r = client.post("/api/voices/upload", files={"file": ("test.wav", make_wav(5.0), "audio/wav")},
                data={"name": "测试音色A", "transcribe": "false"})
check("upload ok", r.json().get("ok") is True, r.text[:200])
vid = r.json()["voice"]["voice_id"]
meta = r.json()["voice"]
check("duration probed", 4.5 <= (meta.get("duration_s") or 0) <= 5.5, meta.get("duration_s"))
check("multipart name persisted", meta["name"] == "测试音色A")
check("converted 24k mono", meta.get("sample_rate") == 24000, meta.get("sample_rate"))
ref = vla.PROFILES_DIR / vid / "ref.wav"
check("ref.wav saved", ref.exists())

# 3. 上传：太短 / 静音 / 坏格式被拒
r = client.post("/api/voices/upload", files={"file": ("short.wav", make_wav(1.0), "audio/wav")}, data={})
check("too short rejected", r.status_code == 400 and "短" in r.json()["detail"]["reason"])
silent = np.zeros(24000 * 5, dtype="<i2")
buf = io.BytesIO()
with wave.open(buf, "wb") as wav:
    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
    wav.writeframes(silent.tobytes())
r = client.post("/api/voices/upload", files={"file": ("silent.wav", buf.getvalue(), "audio/wav")}, data={})
check("silence rejected", r.status_code == 400 and "静音" in r.json()["detail"]["reason"])
r = client.post("/api/voices/upload", files={"file": ("bad.wav", b"not audio at all", "audio/wav")}, data={})
check("corrupt rejected", r.status_code == 400)

# 4. 参考文字编辑 + 重命名
r = client.post("/api/voices/activate", json={"voice_id": vid, "backend": "higgs"})
check("activation rejects missing reference text", r.status_code == 400 and not vla.HIGGS_ACTIVE_FILE.exists())
r = client.post(f"/api/voices/{vid}/ref_text", json={"ref_text": "这是测试参考文字"})
check("ref_text saved", r.json().get("ok") is True and r.json()["ref_text"] == "这是测试参考文字")
r = client.post(f"/api/voices/{vid}/rename", json={"name": "改名音色A"})
check("rename ok", r.json().get("ok") is True)

# 5. 激活：higgs 允许 / qwen3 fish 明确限制 / 恢复小烟 = 清状态文件
vla._voice_mutation_idle = lambda: False
r = client.post('/api/voices/activate',json={'voice_id':vid,'backend':'higgs'})
check('busy activation preserves state',r.status_code == 409 and not vla.HIGGS_ACTIVE_FILE.exists())
vla._voice_mutation_idle = lambda: True
saved_ref = ref.with_name('ref.saved.wav')
ref.rename(saved_ref)  # isolated generated fixture only; restored immediately
r = client.post('/api/voices/activate',json={'voice_id':vid,'backend':'higgs'})
check('missing reference audio rejected',r.status_code == 400 and r.json()['detail']['reason']=='REF_AUDIO_MISSING')
saved_ref.rename(ref)
r = client.post("/api/voices/activate", json={"voice_id": vid, "backend": "higgs"})
check("activate higgs ok", r.json().get("ok") is True, r.text[:200])
check("state file written", json.loads(vla.HIGGS_ACTIVE_FILE.read_text(encoding="utf-8"))["voice_id"] == vid)
r = client.get("/api/voices")
check("active reflects switch", r.json()["active_higgs"] == vid)
r = client.post("/api/voices/activate", json={"voice_id": vid, "backend": "qwen3"})
check("qwen3 switch refused with reason", r.json().get("ok") is False and r.json().get("reason") == "BACKEND_SWITCH_UNSUPPORTED")
r = client.post("/api/voices/activate", json={"voice_id": vid, "backend": "fish"})
check("fish switch refused", r.json().get("ok") is False)
r = client.post("/api/voices/activate", json={"voice_id": "xiaoyan", "backend": "higgs"})
check("restore xiaoyan ok", r.json().get("ok") is True and r.json()["active_voice"] == "xiaoyan")
check("state file cleared", not vla.HIGGS_ACTIVE_FILE.exists())

# 6. handler 解析逻辑：写入状态文件后 _active_reference 指向 profile（直接测实现）
vla.HIGGS_ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
vla.HIGGS_ACTIVE_FILE.write_text(json.dumps({"voice_id": vid}), encoding="utf-8")

# 动态加载 higgs handler 的 _active_reference（绕开 torch 重依赖：只执行该函数源码）
import ast as _ast
handler_src = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime", "s2s_business", "higgs_tts_handler.py")).read_text(encoding="utf-8")
tree = _ast.parse(handler_src)
ns = {"json": json, "Path": Path, "logger": type("L", (), {"warning": staticmethod(lambda *a, **k: None)})()}
for node in tree.body:
    if isinstance(node, _ast.FunctionDef) and node.name == "_active_reference":
        code = compile(_ast.Module(body=[node], type_ignores=[]), "<handler>", "exec")
        exec(code, ns)
        break
ns["ACTIVE_VOICE_FILE"] = vla.HIGGS_ACTIVE_FILE
ns["VOICES_PROFILES"] = vla.PROFILES_DIR
ns["REFERENCE_AUDIO"] = "xiaoyan_ref.wav"
ns["REFERENCE_TEXT"] = "小烟参考文字"
ns["_active_voice_cache"] = None
audio_path, text = ns["_active_reference"]()
check("handler resolves cloned voice", audio_path.endswith("ref.wav") and text == "这是测试参考文字", (audio_path, text))
vla.HIGGS_ACTIVE_FILE.unlink()
ns["_active_voice_cache"] = None
audio_path, text = ns["_active_reference"]()
check("handler falls back to xiaoyan", audio_path == "xiaoyan_ref.wav")

# 7. 试听：higgs worker 不在 → 明确 503
r = client.post(f"/api/voices/{vid}/preview", json={"text": ""})
check("preview 503 without higgs studio", r.status_code == 503, r.status_code)

# 8. 删除：进回收站；若正在使用则自动退回小烟
r = client.post("/api/voices/not-a-voice/ref_text", json={"ref_text": "test"})
check("invalid voice id returns 400", r.status_code == 400)
vla.HIGGS_ACTIVE_FILE.write_text(json.dumps({"voice_id": vid}), encoding="utf-8")
r_guard = None
vla._voice_mutation_idle = lambda: False
r_guard = client.delete(f'/api/voices/{vid}')
check('busy delete retains reference and activation',r_guard.status_code == 409 and ref.exists() and json.loads(vla.HIGGS_ACTIVE_FILE.read_text())['voice_id']==vid)
vla._voice_mutation_idle = lambda: True
r = client.delete(f"/api/voices/{vid}")
check("delete ok -> trash", r.json().get("ok") is True and "trash" in r.json().get("trash_path", ""))
check("active reset after delete", not vla.HIGGS_ACTIVE_FILE.exists())
r = client.get("/api/voices")
check("deleted voice not listed", all(v["voice_id"] != vid for v in r.json()["voices"]))
trash = client.get('/api/voices/trash').json()['voices']
check('voice trash listed', len(trash) == 1 and trash[0]['voice_id'] == vid)
trash_id = trash[0]['trash_id']
vla._profile_dir(vid).mkdir()
r = client.post(f'/api/voices/trash/{trash_id}/restore')
check('voice restore collision rejected', r.status_code == 409)
vla._profile_dir(vid).rmdir()  # only an empty isolated fixture directory
r = client.post(f'/api/voices/trash/{trash_id}/restore')
check('voice restore succeeds', r.status_code == 200 and r.json()['voice_id'] == vid)
check('restored audio exists', (vla._profile_dir(vid)/'ref.wav').is_file())
check('restored metadata preserved', vla._read_meta(vid)['name'] == '改名音色A' and vla._read_meta(vid)['ref_text'] == '这是测试参考文字')
check('restore does not change active voice', not vla.HIGGS_ACTIVE_FILE.exists())
r = client.post('/api/voices/trash/invalid/restore')
check('invalid trash id rejected', r.status_code == 400)

# A long CPU transcription must not occupy the ASGI event loop.
import asyncio
import httpx
import threading
started = threading.Event()
released = threading.Event()
real_transcribe = vla._transcribe
def slow_transcribe(_):
    started.set()
    released.wait(4)
    return "isolated transcription"
vla._transcribe = slow_transcribe
@app.get("/isolated-health")
async def isolated_health():
    return {"ok": True}
async def concurrent_upload():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        pending = asyncio.create_task(c.post("/api/voices/upload",
            files={"file": ("test.wav", make_wav(), "audio/wav")}))
        await asyncio.to_thread(started.wait, 5)
        try:
            health = await asyncio.wait_for(c.get("/isolated-health"), 1)
            check("ASR does not block async health", health.status_code == 200 and
                  not pending.done() and not released.is_set())
        finally:
            released.set()
        check("concurrent upload completes", (await pending).status_code == 200)
asyncio.run(concurrent_upload())
vla._transcribe = real_transcribe

print()
print("RESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
