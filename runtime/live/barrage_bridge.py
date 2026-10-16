#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Barrage Bridge (LIVE_BARRAGE_BRIDGE_01, DRY-RUN).

Fan-in from BarrageGrab ws://127.0.0.1:8888/ (Type==1, ProcessName=='live-companion'),
second-parse Data, dedupe by MsgId, basic filter, normalize to unified event,
then the legacy entry logs DRY-RUN only (no ComfyUI/8193/H3).
The reusable async reader uses the Girlfriend runtime's existing websockets.
Paths embedded as base64 (evidence/config dirs are Chinese).
"""
import base64
import json
import os
import socket
import struct
import sys
import time

B64_BASE = ""
B64_RUNTIME = ""
B64_EVIDENCE = ""
WS_HOST = "127.0.0.1"
WS_PORT = 8888
WS_PATH = "/"
SOURCE_PROC_B64 = "55u05pKt5Ly05L6j"   # live-companion proc name as base64 (pure-ASCII source)
DEDUPE_STATE = "dedupe_state.json"
CONFIG_FILE = "config.json"
RECONNECT_DELAY = 3.0


def U(s):
    return base64.b64decode(s).decode("utf-8")


SOURCE_PROC = U(SOURCE_PROC_B64)


BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RDIR = os.path.dirname(os.path.abspath(__file__))
EVDIR = os.path.join(BASE, 'logs', 'barrage')
CFG_PATH = os.path.join(RDIR, CONFIG_FILE)
DEDUPE_PATH = os.path.join(RDIR, DEDUPE_STATE)


def nowts():
    return time.time()


class JsonlW(object):
    def __init__(self, path):
        self.path = path

    def write(self, obj):
        f = open(self.path, "a", encoding="utf-8", newline="\n")
        try:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        finally:
            f.close()


class StatusLog(object):
    def __init__(self, path):
        self.path = path

    def line(self, s):
        f = open(self.path, "a", encoding="utf-8", newline="\n")
        try:
            f.write(s + "\n")
        finally:
            f.close()


# ---------- load config ----------
def load_config():
    cfg = {"dry_run": True, "live_execution": False, "dedupe_persist": True}
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            cfg.update({k: raw.get(k, v) for k, v in cfg.items()})
    except Exception:
        pass
    return cfg


def load_dedupe():
    try:
        with open(DEDUPE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return set(data.get("seen", []))
    except Exception:
        return set()


def save_dedupe(seen):
    try:
        lst = list(seen)
        lst.sort()
        folder = os.path.dirname(DEDUPE_PATH) or "."
        tmp = os.path.join(folder, ".dedupe.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"seen": lst[-200000:]}, fh, ensure_ascii=False)
        os.replace(tmp, DEDUPE_PATH)
    except Exception:
        pass


# ---------- ws client (stdlib) ----------
def ws_connect(timeout=8):
    s = socket.create_connection((WS_HOST, WS_PORT), timeout)
    s.settimeout(timeout)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = ("GET %s HTTP/1.1\r\n"
           "Host: %s:%d\r\n"
           "Upgrade: websocket\r\n"
           "Connection: Upgrade\r\n"
           "Sec-WebSocket-Key: %s\r\n"
           "Sec-WebSocket-Version: 13\r\n"
           "Origin: http://%s:%d\r\n"
           "\r\n" % (WS_PATH, WS_HOST, WS_PORT, key, WS_HOST, WS_PORT))
    s.sendall(req.encode("ascii"))
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = s.recv(4096)
        if not d:
            raise RuntimeError("handshake eof")
        buf += d
    head, rest = buf.split(b"\r\n\r\n", 1)
    if "101" not in head.split(b"\r\n")[0].decode("ascii", "replace"):
        raise RuntimeError("handshake failed: " + head.split(b"\r\n")[0][:80])
    return s, rest


class WSReader(object):
    def __init__(self, sock, buf=b""):
        self.s = sock
        self.buf = buf

    def _need(self, n):
        while len(self.buf) < n:
            d = self.s.recv(65536)
            if not d:
                raise EOFError("closed")
            self.buf += d

    def read_frame(self):
        self._need(2)
        b0 = self.buf[0]
        b1 = self.buf[1]
        op = b0 & 0x0F
        masked = b1 & 0x80
        ln = b1 & 0x7F
        idx = 2
        if ln == 126:
            self._need(4)
            ln = struct.unpack(">H", self.buf[2:4])[0]
            idx = 4
        elif ln == 127:
            self._need(10)
            ln = struct.unpack(">Q", self.buf[2:10])[0]
            idx = 10
        m = None
        if masked:
            self._need(idx + 4)
            m = self.buf[idx:idx + 4]
            idx += 4
        self._need(idx + ln)
        payload = bytearray(self.buf[idx:idx + ln])
        if m:
            for i in range(len(payload)):
                payload[i] ^= m[i % 4]
        self.buf = self.buf[idx + ln:]
        return op, bytes(payload)


def parse_inner(data):
    for _ in range(4):
        if isinstance(data, dict):
            return data
        if not isinstance(data, str):
            return None
        try:
            data = json.loads(data)
        except (ValueError, TypeError, RecursionError):
            return None
    return None


def norm_comment(pack, received_at):
    """Normalize a Type==1 pack into unified event dict; None if must drop."""
    inner = parse_inner(pack.get("Data"))
    if not isinstance(inner, dict):
        return None
    msg_id = str(inner.get("MsgId") or "").strip()
    content = inner.get("Content") or ""
    if isinstance(content, str):
        content = content
    else:
        content = str(content)
    if not content.strip():
        return None
    user = inner.get("User") or {}
    if not isinstance(user, dict):
        user = {}
    owner = inner.get("Owner") or inner.get("Onwer")
    room_id = str(inner.get("RoomId") or "")
    web_room_id = str(inner.get("WebRoomId") or "")
    room_title = inner.get("RoomTitle") or ""
    ev = {
        "source": "douyin_barrage",
        "type": "comment",
        "msg_id": msg_id,
        "content": content,
        "user": {
            "id": str(user.get("Id") or ""),
            "display_id": str(user.get("DisplayId") or ""),
            "nickname": user.get("Nickname") or "",
        },
        "room": {
            "room_id": room_id,
            "web_room_id": web_room_id,
            "title": room_title,
        },
        "owner": owner,
        "received_at": received_at,
    }
    return ev


async def stream_comments(ws_url, on_packet, on_connection, stop_event,
                          auto_reconnect=True, reconnect_min=1, reconnect_max=20):
    """Production consumer upgrade; same BarrageGrab source, no second collector.

    websockets handles fragmentation/ping/close and bounds frame size. The legacy
    DRY-RUN entry below remains available; run only ONE entry at a time.
    """
    import asyncio
    import websockets
    delay = reconnect_min
    while not stop_event.is_set():
        try:
            async with websockets.connect(ws_url, proxy=None, open_timeout=5, close_timeout=2,
                                          max_size=262144, max_queue=32,
                                          ping_interval=None) as ws:
                delay = reconnect_min
                on_connection(True, "connected")
                while not stop_event.is_set():
                    recv = asyncio.create_task(ws.recv())
                    stopped = asyncio.create_task(stop_event.wait())
                    try:
                        done, _ = await asyncio.wait((recv, stopped), return_when=asyncio.FIRST_COMPLETED)
                        if stopped in done:
                            break
                        on_packet(recv.result())
                    finally:
                        for task in (recv, stopped):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(recv, stopped, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            on_connection(False, type(exc).__name__)
        finally:
            on_connection(False, "disconnected")
        if not auto_reconnect or stop_event.is_set():
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        delay = min(reconnect_max, delay * 2)


def main():
    cfg = load_config()
    dry_run = bool(cfg.get("dry_run", True))
    live_exec = bool(cfg.get("live_execution", False))
    persist = bool(cfg.get("dedupe_persist", True))
    if not os.path.isdir(RDIR):
        try:
            os.makedirs(RDIR, exist_ok=True)
        except Exception:
            pass
    if not os.path.isdir(EVDIR):
        try:
            os.makedirs(EVDIR, exist_ok=True)
        except Exception:
            pass
    seen = load_dedupe()
    w_raw = JsonlW(os.path.join(EVDIR, "04_real_comments.jsonl"))
    w_ev = JsonlW(os.path.join(EVDIR, "05_normalized_events.jsonl"))
    st = StatusLog(os.path.join(EVDIR, "bridge_status.log"))
    recv = 0
    valid_t1 = 0
    parse_fail = 0
    dup_delivered = 0
    accepted = 0
    t0 = time.time()
    st.line("==BRIDGE_START== ts=%s dry_run=%s live_exec=%s ws=ws://%s:%d%s" %
            (time.strftime("%Y-%m-%d %H:%M:%S"), dry_run, live_exec, WS_HOST, WS_PORT, WS_PATH))
    conn = 0
    while True:
        conn += 1
        try:
            sock, rest = ws_connect(8)
        except Exception as e:
            st.line("CONNECT_FAIL conn=%d err=%s sleep=%s" % (conn, e, RECONNECT_DELAY))
            time.sleep(RECONNECT_DELAY)
            continue
        sock.settimeout(1.0)
        rd = WSReader(sock, rest)
        st.line("CONNECTED conn=%d" % conn)
        last_ping = time.time()
        closed = False
        while not closed:
            try:
                opcode, payload = rd.read_frame()
            except socket.timeout:
                continue
            except EOFError:
                st.line("EOF conn=%d" % conn)
                closed = True
                break
            except Exception as e:
                st.line("READ_ERR conn=%d err=%s" % (conn, e))
                closed = True
                break
            if opcode == 0x9:
                continue
            if opcode == 0x8:
                st.line("CLOSE conn=%d" % conn)
                closed = True
                break
            if opcode not in (0x1, 0x2):
                continue
            text = payload.decode("utf-8", "replace")
            recv += 1
            try:
                pack = json.loads(text)
            except Exception:
                parse_fail += 1
                continue
            if not isinstance(pack, dict):
                continue
            ptype = pack.get("Type")
            proc = pack.get("ProcessName") or pack.get("processName") or ""
            if ptype != 1:
                continue
            if proc != SOURCE_PROC:
                # ProcessName may come through with the literal Chinese name; accept if matches decoded.
                continue
            valid_t1 += 1
            w_raw.write({"recv_ts": time.strftime("%Y-%m-%d %H:%M:%S"), "type": 1, "proc": proc, "raw": text})
            rev = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            ev = norm_comment(pack, rev)
            if ev is None:
                continue
            mid = ev["msg_id"]
            if mid in seen:
                dup_delivered += 1
                st.line("DUP_DROP msg_id=%s content=%s" % (mid, ev["content"]))
                continue
            seen.add(mid)
            if persist and len(seen) % 50 == 0:
                save_dedupe(seen)
            accepted += 1
            w_ev.write(ev)
            if dry_run:
                st.line("BARRAGE_ACCEPTED CONTENT=%s MSG_ID=%s TARGET=%s DRY_RUN=TRUE" %
                        (ev["content"], mid, "runtime/barrage (dry-run gate)"))
                print("BARRAGE_ACCEPTED CONTENT=%s MSG_ID=%s DRY_RUN=TRUE" % (ev["content"], mid), flush=True)
            elif live_exec:
                # NOT reached this round. Placeholder: hand off to existing entry.
                print("BARRAGE_ACCEPTED CONTENT=%s MSG_ID=%s LIVE_EXEC_GATE=ON(UNUSED)" % (ev["content"], mid), flush=True)
        try:
            sock.close()
        except Exception:
            pass
        time.sleep(RECONNECT_DELAY)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        try:
            f = open(os.path.join(EVDIR, "bridge_crash.txt"), "a", encoding="utf-8", newline="\n")
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + traceback.format_exc() + "\n")
            f.close()
        except Exception:
            pass
        sys.exit(1)
