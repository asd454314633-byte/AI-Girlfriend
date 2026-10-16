from collections import OrderedDict, deque


class LiveSession:
    memory_scope = "live_ephemeral"

    def __init__(self, cfg):
        self.cfg = cfg
        self.users, self.rooms = OrderedDict(), OrderedDict()

    def prune(self, now):
        for mapping in (self.users, self.rooms):
            for key, row in list(mapping.items()):
                if now - row["at"] > self.cfg["context_ttl"]:
                    del mapping[key]

    def viewer(self, event):
        return (event["source"], event["room_id"], event["user_id"])

    def history(self, event, now):
        self.prune(now)
        return list(self.users.get(self.viewer(event), {}).get("rows", []))

    def remember(self, event, reply, now):
        key = self.viewer(event)
        row = self.users.setdefault(key, {"at": now, "rows": deque(maxlen=self.cfg["max_user_context"] * 2)})
        row["at"] = now
        row["rows"].extend([{"role": "user", "content": event["content"]}, {"role": "assistant", "content": reply}])
        self.users.move_to_end(key)
        while len(self.users) > self.cfg["max_users"]:
            self.users.popitem(last=False)
        rk = (event["source"], event["room_id"])
        room = self.rooms.setdefault(rk, {"at": now, "rows": deque(maxlen=self.cfg["max_room_context"])})
        room["at"] = now
        room["rows"].append({"username": event["username"], "user_id": event["user_id"], "content": event["content"], "reply": reply})
        self.rooms.move_to_end(rk)
        while len(self.rooms) > self.cfg["max_users"]:
            self.rooms.popitem(last=False)

    def clear(self):
        self.users.clear()
        self.rooms.clear()
