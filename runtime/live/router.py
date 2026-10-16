import time
import uuid
from collections import Counter, OrderedDict
from .dedupe import Deduplicator
from .session import LiveSession
from .adapter import response_request


class LiveInteractionRouter:
    def __init__(self, cfg, normalizer, audit=lambda *args, **kwargs: None, clock=time.time):
        self.cfg, self.normalizer, self.audit, self.clock = cfg, normalizer, audit, clock
        self.enabled = bool(cfg["enabled"])
        self.dedupe, self.session = Deduplicator(cfg), LiveSession(cfg)
        self.queue, self.inflight = [], None
        self.counts = Counter({k: 0 for k in ("comments_received", "comments_valid", "comments_deduplicated", "comments_filtered", "comments_expired", "comments_selected", "llm_requests", "tts_requests", "replies_completed")})
        self.last_user, self.last_room = OrderedDict(), OrderedDict()
        self.last_comment_at = self.last_reply_at = 0
        self.recent_comments, self.recent_replies = [], []

    def set_enabled(self, value):
        self.enabled = bool(value)
        if not self.enabled:
            self.queue.clear()
            self.session.clear()
        self.audit("enabled", enabled=self.enabled)

    def ingest(self, raw, now=None):
        if not self.enabled:
            return
        now = self.clock() if now is None else now
        self.counts["comments_received"] += 1
        try:
            event = self.normalizer.normalize(raw, now)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            self.counts["comments_invalid"] += 1
            self.audit("invalid_payload", raw=raw)
            return
        if not event:
            self.counts["comments_filtered"] += 1
            self.audit("ignored_payload", raw=raw)
            return
        self.counts["comments_valid"] += 1
        self.last_comment_at = now
        self.audit("received", event=event)
        if self.dedupe.duplicate(event, now):
            self.counts["comments_deduplicated"] += 1
            self.audit("duplicate", message_id=event["message_id"])
            return
        text = event["content"].strip()
        cfg, policy = self.cfg, self.cfg["reply_policy"]
        if any(term in "".join(text.split()) for term in cfg.get("blocked_identity_terms", [])):
            self.counts["comments_filtered"] += 1
            self.audit("filtered", message_id=event["message_id"], reason="private_identity_or_memory_request")
            return
        if len(text) > cfg["max_content"] or any(s.lower() in text.lower() for s in cfg["spam_terms"]) or len(set(text)) < 2:
            self.counts["comments_filtered"] += 1
            self.audit("filtered", message_id=event["message_id"], reason="spam_empty_or_long")
            return
        category = "ordinary"
        history = self.session.history(event, now)
        if any(s in text for s in cfg["mention_terms"]):
            category = "mention"
        elif history and any(s in text for s in cfg["followup_terms"]):
            category = "followup"
        elif any(s in text for s in cfg["question_terms"]):
            category = "question"
        elif history:
            category = "continuing"
        event.update(score=policy[category], category=category)
        self.recent_comments = (self.recent_comments + [{k: v for k, v in event.items() if k != "raw_event"}])[-12:]
        if event["score"] < policy["min_score"]:
            self.counts["comments_filtered"] += 1
            self.audit("filtered", message_id=event["message_id"], reason="low_value", category=category)
            return
        if any(x["room_id"] == event["room_id"] and x["user_id"] == event["user_id"] and x["content"] == text for x in self.queue):
            self.counts["comments_filtered"] += 1
            return
        self.queue.append(event)
        if len(self.queue) > cfg["queue_size"]:
            drop = min(self.queue, key=lambda x: (x["score"], x["received_at"]))
            self.queue.remove(drop)
            self.counts["comments_backpressure"] += 1
            self.audit("backpressure", message_id=drop["message_id"])

    def expire(self, now):
        # A closed frontend cannot leave a ticket wedged forever. This expires
        # only its own ticket; it never resubmits an attempted viewer message.
        job = self.inflight
        if job and now - job['timing']['selected'] > self.cfg['dispatch_timeout']:
            self.phase(job['client_id'], job['ticket'], 'failed',
                       {'reason': 'dispatch_timeout_server'}, now=now)
        fresh = []
        for event in self.queue:
            ttl = self.cfg["priority_ttl"] if event["category"] in ("mention", "followup") else self.cfg["comment_ttl"]
            if now - event["received_at"] > ttl:
                self.counts["comments_expired"] += 1
                self.audit("expired", message_id=event["message_id"])
            else:
                fresh.append(event)
        self.queue = fresh

    def claim(self, client_id, persona, now=None):
        now = self.clock() if now is None else now
        self.expire(now)
        if not self.enabled or self.inflight:
            return None
        eligible = [e for e in self.queue if now - e["received_at"] >= self.cfg["selection_delay"]
                    and now - self.last_room.get((e["source"], e["room_id"]), 0) >= self.cfg["room_cooldown"]
                    and now - self.last_user.get(self.session.viewer(e), 0) >= self.cfg["user_cooldown"]]
        if not eligible:
            return None
        event = max(eligible, key=lambda e: (e["score"], e["received_at"]))
        self.queue.remove(event)
        ticket = uuid.uuid4().hex
        request = response_request(event, self.session, self.cfg, persona, ticket, now)
        self.inflight = {"ticket": ticket, "client_id": client_id, "event": event, "timing": {"receive": event["received_at"], "normalized": event["normalized_at"], "selected": now}, "reply": "", "audio_bytes": 0, "started": False, "tts": False}
        self.counts["comments_selected"] += 1
        self.audit("selected", ticket=ticket, event=event)
        return {"ticket": ticket, "event": {k: v for k, v in event.items() if k != "raw_event"}, "request": request}

    def phase(self, client_id, ticket, phase, data=None, now=None):
        now = self.clock() if now is None else now
        data = data or {}
        job = self.inflight
        if not job or job["ticket"] != ticket or job["client_id"] != client_id:
            return False
        job["timing"].setdefault(phase, now)
        if phase == "llm_done":
            job["timing"][phase] = now
        if phase == "llm_start" and not job["started"]:
            job["started"] = True
            self.counts["llm_requests"] += 1
        if phase == "tts_start" and not job["tts"]:
            job["tts"] = True
            self.counts["tts_requests"] += 1
        if "reply" in data:
            job["reply"] = str(data["reply"])[:4000]
        if "audio_bytes" in data:
            job["audio_bytes"] = int(data["audio_bytes"])
        self.audit("phase", ticket=ticket, phase=phase, data=data, timing=job["timing"])
        if phase in ("completed", "failed", "cancelled"):
            event = job["event"]
            if phase == "completed" and job["audio_bytes"] > 0 and data.get("audible"):
                self.counts["replies_completed"] += 1
                if self.enabled:
                    self.session.remember(event, job["reply"], now)
                self.last_reply_at = now
            else:
                self.counts["replies_failed"] += 1
            self.last_room[(event["source"], event["room_id"])] = now
            self.last_user[self.session.viewer(event)] = now
            for mapping in (self.last_user, self.last_room):
                while len(mapping) > self.cfg["max_users"]:
                    mapping.popitem(last=False)
            public_job = {**job, "event": {k: v for k, v in event.items() if k != "raw_event"}}
            self.recent_replies = (self.recent_replies + [{**public_job, "phase": phase, "audible": bool(data.get("audible"))}])[-12:]
            self.inflight = None
        return True

    def status(self):
        return {"LIVE_INTERACTION_ENABLED": "ON" if self.enabled else "OFF", "memory_scope": "live_ephemeral",
                **self.counts, "queue_depth": len(self.queue), "last_comment_at": self.last_comment_at,
                "last_reply_at": self.last_reply_at, "generation_in_flight": bool(self.inflight),
                "recent_comments": self.recent_comments, "recent_replies": self.recent_replies,
                "users": len(self.session.users), "dedupe_size": len(self.dedupe.seen)}
