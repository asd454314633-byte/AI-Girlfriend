"""Shadow repro: does aiortc 1.15 / aioice detect a hard-killed remote peer?

Two PCs in one process, separate event loops (threads).  After media flows,
the "browser" side DIES WITHOUT close(): its loop is stopped cold, leaving
the socket abandoned.  The "server" side must then report, within the consent
window (~30-50s), either connectionState failed/disconnected — or never.

Verdict is printed as JSON to stdout.
"""
import asyncio
import json
import threading
import time

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import AudioStreamTrack


class Peer:
    def __init__(self, name):
        self.name = name
        self.loop = asyncio.new_event_loop()
        self.pc = RTCPeerConnection()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(60)

    def stop_cold(self):
        # Simulate process death: abort the loop WITHOUT closing pc/sockets.
        self.loop.call_soon_threadsafe(self.loop.stop)


async def wire(a: Peer, b: Peer):
    a.pc.addTrack(SilentAudio())
    offer = await a.pc.createOffer()
    await a.pc.setLocalDescription(offer)
    await b.pc.setRemoteDescription(a.pc.localDescription)
    answer = await b.pc.createAnswer()
    await b.pc.setLocalDescription(answer)
    await a.pc.setRemoteDescription(b.pc.localDescription)


class SilentAudio(AudioStreamTrack):
    pass


def main():
    log = []

    a = Peer("browser")
    b = Peer("server8010")

    states = []
    b.pc.on("connectionstatechange", lambda: states.append((time.monotonic(), b.pc.connectionState)) or log.append({"ev": "server_state", "state": b.pc.connectionState}))
    b.pc.on("iceconnectionstatechange", lambda: log.append({"ev": "server_ice", "state": b.pc.iceConnectionState}))

    a.run(wire(a, b))
    # let media flow for 5s
    time.sleep(5)
    t_kill = time.monotonic()
    log.append({"ev": "kill", "t": 0})
    a.stop_cold()
    a.thread.join(timeout=5)

    # watch server side for up to 75s
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        s = b.pc.connectionState
        if s in ("failed", "closed", "disconnected"):
            break
        time.sleep(1)

    detected = b.pc.connectionState
    elapsed = round(time.monotonic() - t_kill, 1)
    log.append({"ev": "final", "state": detected, "elapsed_s": elapsed,
                "states_timeline": [(round(t - t_kill, 1), s) for t, s in states]})
    verdict = {
        "DETECTED": detected in ("failed", "closed", "disconnected"),
        "final_state": detected,
        "elapsed_s": elapsed,
        "expected_window_s": 40,
    }
    log.append({"ev": "verdict", **verdict})
    print(json.dumps(log, indent=1))


if __name__ == "__main__":
    main()
