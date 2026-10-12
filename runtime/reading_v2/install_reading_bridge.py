#!/usr/bin/env python3
"""_RV2_READING_BRIDGE_V1 installer.

Registers three routes inside ``create_app`` in
``speech_to_speech/api/openai_realtime/websocket_router.py`` so the 陪读
read-aloud client can inject verbatim text straight into the TTS handler:

    POST /v1/speak        {text, language_code?}  -> synthesis receipt
    POST /v1/speak/stop   drain queued chunks
    GET  /v1/speak/status bridge + queue stats

Patch discipline (lesson carried over from INPUT_MEMORY_HARDENING_01): the
insertion is a *single unique-anchor string replacement*, never an index
slice.  The anchor is verified to occur exactly once before anything is
written, both target files are backed up with a timestamp, and re-running is
a no-op (idempotent via the marker).

Nothing here touches VAD parameters, the Whisper model, Qwen3-TTS voice
parameters, ref.wav or REF_TEXT.
"""

from __future__ import annotations

import datetime as _dt
import os
import pathlib
import shutil
import sys

# MODE_SYSTEM_V1：这两条原本写死的是 WSL 年代的路径（/home/<user>/s2s/…、
# /mnt/d/…），而 Windows 原生运行时会把它们解释成 D:\home\…、D:\mnt\… 这种谁也不
# 会去看的影子位置 —— 于是这个安装器一直「成功」地装到了一个不存在的地方，8765 上
# 从来就没有 /v1/speak（实测：GET /v1/speak/status -> 404 Not Found）。
# 现在按本文件自身位置推出项目根，并允许环境变量覆盖；同时保留旧路径作为兜底，
# 万一哪天真的回到 WSL 上跑也不会装错。
_HERE = pathlib.Path(__file__).resolve()
_PROJECT_ROOT = _HERE.parents[2]

_CANDIDATES = [
    pathlib.Path(os.environ.get("S2S_SITE_PACKAGES", "").strip())
    if os.environ.get("S2S_SITE_PACKAGES", "").strip() else None,
    _PROJECT_ROOT / "runtime" / "core" / "venv" / "Lib" / "site-packages" / "speech_to_speech",
    pathlib.Path(os.environ.get("S2S_HOME", str(Path.home() / "s2s"))) / ".venv/lib/python3.12/site-packages/speech_to_speech",
]
SITE_PACKAGES = next((p for p in _CANDIDATES if p is not None and p.is_dir()),
                     _CANDIDATES[1])
ROUTER = SITE_PACKAGES / "api" / "openai_realtime" / "websocket_router.py"
BRIDGE_SRC = _HERE.parent / "reading_bridge.py"
BRIDGE_DST = SITE_PACKAGES / "api" / "openai_realtime" / "reading_bridge.py"

MARKER = "_RV2_READING_BRIDGE_V1"
ANCHOR = '    @app.websocket("/v1/realtime")'

ROUTES = f'''    # ---- {MARKER} : 陪读 (read-aloud) injection routes --------------------
    # Registered inside create_app so the handlers close over `pool`.  These are
    # the only server-side entry points 陪读 needs: text goes straight to the
    # TTS handler's queue_in -- the same queue LMOutputProcessor writes to -- so
    # the novel is spoken verbatim and never rewritten by the LLM (task 12).
    @app.post("/v1/speak")
    async def reading_speak(payload: dict) -> dict:
        from speech_to_speech.api.openai_realtime import reading_bridge

        body = payload if isinstance(payload, dict) else {{}}
        text = body.get("text")
        lang = body.get("language_code")
        gen = body.get("generation")          # 陈旧回调闸门（MODE_SYSTEM_V1 审计 §8）
        return await asyncio.to_thread(reading_bridge.speak, pool, str(text or ""),
                                       lang or None, gen, body.get("reading_source"))

    @app.post("/v1/speak/stop")
    async def reading_speak_stop() -> dict:
        from speech_to_speech.api.openai_realtime import reading_bridge

        return await asyncio.to_thread(reading_bridge.stop, pool)

    @app.get("/v1/speak/status")
    async def reading_speak_status() -> dict:
        from speech_to_speech.api.openai_realtime import reading_bridge

        return await asyncio.to_thread(reading_bridge.status, pool)
    # ---- end {MARKER} ------------------------------------------------------

'''


def backup(path: pathlib.Path, stamp: str) -> pathlib.Path:
    dst = path.with_name(path.name + f".bak_rv2_{stamp}")
    shutil.copy2(path, dst)
    return dst


def main() -> int:
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report: list[str] = []

    if not ROUTER.exists():
        print(f"ABORT missing router: {ROUTER}")
        return 2
    if not BRIDGE_SRC.exists():
        print(f"ABORT missing bridge source: {BRIDGE_SRC}")
        return 2

    original = BRIDGE_DST.read_text(encoding="utf-8") if BRIDGE_DST.exists() else ""
    if original != BRIDGE_SRC.read_text(encoding="utf-8"):
        if BRIDGE_DST.exists():
            backup(BRIDGE_DST, stamp)
        BRIDGE_DST.write_text(BRIDGE_SRC.read_text(encoding="utf-8"), encoding="utf-8")
        report.append(f"bridge_written={BRIDGE_DST}")
    else:
        report.append("bridge_unchanged")

    text = ROUTER.read_text(encoding="utf-8")
    if MARKER in text:
        report.append("routes=ALREADY_INSTALLED")
    else:
        n = text.count(ANCHOR)
        if n != 1:
            print(f"ABORT anchor occurrences={n} (need exactly 1) in {ROUTER}")
            return 3
        bak = backup(ROUTER, stamp)
        patched = text.replace(ANCHOR, ROUTES + ANCHOR, 1)
        if MARKER not in patched or patched.count(ANCHOR) != 1:
            print("ABORT post-patch assertion failed; nothing written")
            return 4
        ROUTER.write_text(patched, encoding="utf-8")
        report.append(f"routes=INSERTED backup={bak.name}")
        report.append(f"router_delta_bytes={len(patched) - len(text)}")

    # Compile-check both files so a syntax error fails here, not at 8765 boot.
    import py_compile

    for p in (ROUTER, BRIDGE_DST):
        py_compile.compile(str(p), doraise=True)
        report.append(f"compiles={p.name}")

    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
