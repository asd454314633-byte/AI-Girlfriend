"""Reuse LIVE_BARRAGE_BRIDGE_01's parser, preserving the original packet."""
import hashlib
import importlib.util
import json
import time
import math


def load_bridge(path):
    spec = importlib.util.spec_from_file_location("existing_barrage_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EventNormalizer:
    def __init__(self, bridge):
        self.bridge = bridge

    def normalize(self, raw, received_at=None):
        now = time.time() if received_at is None else received_at
        pack = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if not isinstance(pack, dict) or str(pack.get("Type")) != "1":
            return None
        parsed = self.bridge.norm_comment(pack, now)
        if not parsed:
            return None
        inner = self.bridge.parse_inner(pack.get("Data"))
        user = parsed["user"]
        room = parsed["room"]
        timestamp = inner.get("Timestamp") or inner.get("CreateTime") or now
        try:
            timestamp = float(timestamp)
            if not math.isfinite(timestamp) or timestamp <= 0:
                timestamp = now
            if timestamp > 1e11:
                timestamp /= 1000
        except (ValueError, TypeError):
            timestamp = now
        # Anonymous users never share a named viewer's context.
        uid = user["id"] or user["display_id"]
        if not uid:
            # A nickname is not a stable identity; missing-ID events get no
            # shared viewer history. Identical retries still deduplicate.
            identity = [user["nickname"], parsed["msg_id"], parsed["content"], timestamp]
            uid = "anonymous:" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:16]
        return {"source": "simulation" if pack.get("_test_fixture") else "douyin", "room_id": room["room_id"] or room["web_room_id"] or "unknown",
                "event_type": "comment", "message_id": parsed["msg_id"], "user_id": uid,
                "username": str(user["nickname"] or "观众"), "content": parsed["content"],
                "timestamp": timestamp, "received_at": now, "normalized_at": time.time(),
                "raw_type": 1, "raw_event": pack}
