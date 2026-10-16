import hashlib
from collections import OrderedDict


class Deduplicator:
    def __init__(self, cfg):
        self.cfg, self.seen = cfg, OrderedDict()

    def key(self, event):
        prefix = (event["source"], event["room_id"])
        if event["message_id"]:
            return prefix + (event["message_id"],)
        bucket = int(event["timestamp"] // self.cfg["fingerprint_bucket"])
        return prefix + (hashlib.sha256(repr((event["user_id"], event["content"], bucket)).encode()).hexdigest(),)

    def duplicate(self, event, now):
        while self.seen and (now - next(iter(self.seen.values())) > self.cfg["dedupe_ttl"]):
            self.seen.popitem(last=False)
        key = self.key(event)
        if key in self.seen:
            return True
        self.seen[key] = now
        while len(self.seen) > self.cfg["dedupe_size"]:
            self.seen.popitem(last=False)
        return False
