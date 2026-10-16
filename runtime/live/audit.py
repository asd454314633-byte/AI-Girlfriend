"""Bounded non-blocking log queue; raw events retained with credential redaction."""
import json
import logging
import logging.handlers
import queue
import re
import time


SECRET = re.compile(r"token|cookie|sessdata|password|authorization|api.?key", re.I)


def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if SECRET.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        # Parse encoded Data to redact nested credential fields without changing content.
        try:
            parsed = json.loads(value)
            if isinstance(parsed, (dict, list)):
                return json.dumps(redact(parsed), ensure_ascii=False)
        except (ValueError, TypeError):
            pass
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)
        value = re.sub(r'(?i)(authorization|cookie|sessdata|password|api_key|token)\s*[:=]\s*[^\s,}]+', r'\1=[REDACTED]', value)
    return value


class Audit:
    def __init__(self, path, level="INFO"):
        self.queue = queue.Queue(maxsize=8192)
        self.logger = logging.getLogger("live_interaction")
        self.logger.setLevel(str(level).upper())
        self.logger.propagate = False
        self.dropped = 0
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=8_000_000, backupCount=6, encoding="utf-8")
        self.listener = logging.handlers.QueueListener(self.queue, handler)
        self.listener.start()

    def __call__(self, action, **fields):
        level = logging.WARNING if action in ("invalid_payload", "adapter_unready", "attempted_state_invalid") else logging.INFO
        if not self.logger.isEnabledFor(level):
            return
        record = self.logger.makeRecord(self.logger.name, level, "", 0,
                                       json.dumps(redact({"at": time.time(), "action": action, **fields}), ensure_ascii=False), (), None)
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1

    def close(self):
        self.listener.stop()
