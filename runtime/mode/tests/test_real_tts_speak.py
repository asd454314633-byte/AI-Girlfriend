# -*- coding: utf-8 -*-
"""朗读链路的真 TTS 验收（MODE_SYSTEM_V1 审计 §七、§八、§三十五）。

不需要浏览器：直接连 8765 的 /v1/realtime 占一个会话位，然后走和页面完全相同的两个
HTTP 口子（POST /v1/speak、POST /v1/speak/stop），看真实的音频字节流。

能在这里证伪的四件事：
  * 原文真的进了 Qwen3-TTS 并出了音频（不是"接口返回 ok"就算数）；
  * stop 之后音频真的断掉，不再有新的 delta（不是只改了个标志位）；
  * stop 之后到达的旧号 speak 不出声（陈旧回调闸门）；
  * 一次性猛灌 40 段，队列不会无限堆积（背压真的在起作用）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import httpx
import websockets

S2S_HTTP = "http://127.0.0.1:8765"
S2S_WS = "ws://127.0.0.1:8765/v1/realtime"
HERE = Path(__file__).resolve().parent

SAMPLE = "海边的灯塔已经很久没有人看守了。镇上的人说，那塔里的灯从来不灭。"
CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), str(detail)[:240]))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


class AudioTap:
    """数一数这个会话到底收到了多少音频字节。"""

    def __init__(self) -> None:
        self.bytes = 0
        self.deltas = 0
        self.done = 0
        self.errors: list[str] = []
        self.session_ready = asyncio.Event()

    def feed(self, event: dict) -> None:
        kind = str(event.get("type") or "")
        if kind == "session.created":
            self.session_ready.set()
        if kind == "response.output_audio.delta":
            raw = event.get("delta") or ""
            self.deltas += 1
            try:
                self.bytes += len(base64.b64decode(raw))
            except Exception:
                self.bytes += len(str(raw))
        elif kind in ("response.output_audio.done", "response.done"):
            self.done += 1
        elif kind == "error":
            self.errors.append(json.dumps(event, ensure_ascii=False)[:200])


async def _pump(ws, tap: AudioTap) -> None:
    """一直读到连接断开；读不动了就退出，不影响主流程。"""
    try:
        while True:
            raw = await ws.recv()
            if isinstance(raw, bytes):
                continue
            try:
                tap.feed(json.loads(raw))
            except Exception:
                continue
    except Exception:
        return


async def main_async() -> int:
    tap = AudioTap()
    async with websockets.connect(S2S_WS, max_size=None, open_timeout=60) as ws:
        # 音频是靠 WebSocket 推过来的，所以整个测试期间必须一直有人在读；
        # 只在开头读一次的话，后面所有 delta 都还堵在连接里，字节数当然是 0。
        reader = asyncio.create_task(_pump(ws, tap))

        # 等会话建立
        deadline = time.time() + 60
        while time.time() < deadline and not tap.session_ready.is_set():
            await asyncio.sleep(0.2)
        # 给一个最小 instructions，否则部分路径会拒绝开 response
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {"instructions": "你正在给对方念一段文字。照着念，不要改写。",
                        "voice": "alloy"},
        }))
        await asyncio.sleep(1.5)

        async with httpx.AsyncClient(timeout=30) as http:
            st = (await http.get(f"{S2S_HTTP}/v1/speak/status")).json()
            check("8765 上真的挂着朗读桥接", st.get("marker") == "_RV2_READING_BRIDGE_V1",
                  f"tts_handler={st.get('tts_handler')} unit_attached={st.get('unit_attached')}")
            if not st.get("unit_attached"):
                print("ABORT 没有可用的 TTS 单元（会话没占上），后面的音频断言无从谈起")
                return 2

            gen0 = int(st.get("generation") or 0)

            # ---- 1. 真出声 -------------------------------------------------
            base = tap.bytes
            speak = (await http.post(f"{S2S_HTTP}/v1/speak",
                                     json={"text": SAMPLE, "language_code": "zh"})).json()
            check("speak 被接受", bool(speak.get("ok")), json.dumps(speak, ensure_ascii=False)[:200])
            await asyncio.sleep(12)     # Qwen3-TTS 合成 + 推送需要一点时间
            grew = tap.bytes - base
            check("朗读真的产出了音频字节（不是只有 ok=True）", grew > 1000,
                  f"{grew} 字节 / {tap.deltas} 个 delta")

            # ---- 2. stop 之后音频断掉 --------------------------------------
            stop = (await http.post(f"{S2S_HTTP}/v1/speak/stop")).json()
            gen1 = int(stop.get("generation") or 0)
            check("stop 推进了 generation", gen1 > gen0, f"{gen0} → {gen1}")
            await asyncio.sleep(1.0)
            mark = tap.bytes
            await asyncio.sleep(6)
            after_stop = tap.bytes - mark
            check("stop 之后不再有新的朗读音频流出", after_stop == 0,
                  f"停止后又流出 {after_stop} 字节")

            # ---- 3. 陈旧号被拒 ---------------------------------------------
            stale = (await http.post(f"{S2S_HTTP}/v1/speak",
                                     json={"text": SAMPLE, "language_code": "zh",
                                           "generation": gen0})).json()
            check("旧 generation 的 speak 被服务端拒掉",
                  stale.get("ok") is False and stale.get("reason") == "STALE_GENERATION",
                  json.dumps(stale, ensure_ascii=False)[:160])

            # ---- 4. 背压 ---------------------------------------------------
            accepted = rejected = 0
            for i in range(40):
                r = (await http.post(f"{S2S_HTTP}/v1/speak",
                                     json={"text": f"第{i}句压力测试用的很短的一句话。",
                                           "language_code": "zh"})).json()
                if r.get("ok"):
                    accepted += 1
                else:
                    rejected += 1
            pending = (await http.get(f"{S2S_HTTP}/v1/speak/status")).json()
            check("猛灌 40 段时队列有界（背压真的挡住了）",
                  int(pending.get("pending_queue_items") or 0) <= 8 and rejected > 0,
                  f"接受 {accepted} / 拒绝 {rejected}，队列 {pending.get('pending_queue_items')}"
                  f" / 上限 {pending.get('max_pending_items')}")
            await http.post(f"{S2S_HTTP}/v1/speak/stop")
        reader.cancel()

    passed = sum(1 for _n, ok, _d in CHECKS if ok)
    print("=" * 72)
    print(f"REAL_TTS_SPEAK = {passed}/{len(CHECKS)}")
    (HERE / "out_real_tts_speak.json").write_text(json.dumps(
        [{"name": n, "ok": ok, "detail": d} for n, ok, d in CHECKS],
        ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed == len(CHECKS) else 1


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
