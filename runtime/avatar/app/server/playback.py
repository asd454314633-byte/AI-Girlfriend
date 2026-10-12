"""Bounded per-segment observations; RTP output is distinct from client playout."""
from collections import deque
from threading import Lock
from pathlib import Path
import json
import time

_events = deque(maxlen=4096)
_lock = Lock()
_log = Path(__file__).resolve().parents[4] / 'logs/runtime/reading-playback.jsonl'

def record(event, fields, **extra):
    if not fields.get('response_id'):
        return
    row = dict(event=event, mono_ns=time.perf_counter_ns(), at_ms=time.time_ns()/1e6,
               sessionid=str(fields.get('sessionid', '')),
               response_id=fields['response_id'], segment_index=fields.get('segment_index'),
               reading_key=fields.get('reading_key', ''), **extra)
    with _lock:
        _events.append(row)
        try:
            with _log.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(row) + '\n')
        except OSError:
            pass

def snapshot(sessionid):
    with _lock:
        rows = [dict(e) for e in _events if e['sessionid'] == str(sessionid)]
    return dict(events=rows, mono_ns=time.perf_counter_ns(), at_ms=time.time_ns()/1e6)
