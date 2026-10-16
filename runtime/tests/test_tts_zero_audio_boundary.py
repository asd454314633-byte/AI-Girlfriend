# -*- coding: utf-8 -*-
"""Zero-audio READING boundary regression (2026-10-11 fault, doc_bb4c9a14ee "结束。").

Production evidence: logs/runtime/tts-generation.jsonl showed
start -> last_delta(complete) with audio_chunks=0 and NO first_delta -- the model
returned zero chunks without raising, the response never opened, and the final
segment was silently swallowed.  The repair (qwen3_tts_adapter) retries the SAME
original text up to ZERO_AUDIO_MAX_RETRIES extra times for READING inputs only,
never once any chunk was yielded, honours reading-generation cancellation, and on
exhaustion audits 'zero_audio_exhausted' + logs an error (no fabricated speech,
no unbounded loop).

Run: runtime/core/venv/Scripts/python.exe runtime/tests/test_tts_zero_audio_boundary.py
Pure logic: the real model is never loaded; _generate_voice_clone is faked.
"""
LEGACY_TEST_SCOPE = 'INSUFFICIENT'
import json
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "runtime" / "core"))

from providers.adapters import qwen3_tts_adapter as adapter
from providers.adapters.qwen3_tts_adapter import (
    AUDIT_PATH, Qwen3DeliveryTTSHandler, ZERO_AUDIO_MAX_RETRIES,
)
from providers.reading_strategy import ReadingTTSInput

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (name, detail))


def make_handler(reading_generation=None, strategy='B'):
    handler = Qwen3DeliveryTTSHandler.__new__(Qwen3DeliveryTTSHandler)
    handler._turn_delivery = types.SimpleNamespace(instruction=None, max_chars=96)
    if reading_generation is None:
        handler._reading_input = None
    else:
        handler._reading_input = ReadingTTSInput(
            text='结束。', language_code=None,
            reading_strategy=strategy,
            source_chunks=[{'document_id': 'fixture', 'chapter_index': 0,
                            'chunk_index': 3, 'is_last': True, 'text': '结束。'}],
            reading_generation=reading_generation)
    return handler


def run(handler, fake_generate):
    # Instance-attribute functions are not bound, so bind explicitly: the adapter
    # calls self._generate_voice_clone(segment, instruct).
    handler._generate_voice_clone = (
        lambda segment, instruct=None, _f=fake_generate: _f(handler, segment, instruct))
    return list(handler._process_voice_clone('结束。'))


def read_audit(tmp):
    path = Path(getattr(tmp, 'name', tmp)) / 'audit.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def reset_audit():
    """Audit opens in append mode; drop the file so each section sees only its own events."""
    p = Path(getattr(tmpdir, 'name')) / 'audit.jsonl'
    if p.exists():
        p.unlink()


tmpdir = tempfile.TemporaryDirectory()
adapter.AUDIT_PATH = Path(tmpdir.name) / 'audit.jsonl'

print("== bounded retry on clean zero output (READING) ==")
reset_audit()
h = make_handler(reading_generation=5)
calls = []


def zero_then_audio(self, segment, instruct=None):
    calls.append(segment)
    if len(calls) == 1:
        return iter(())          # the fault: clean zero output, no exception
    return iter([b'A' * 16])


out = run(h, zero_then_audio)
check("retry yields the real audio once", out == [b'A' * 16], repr(out))
check("same original text retried, nothing added", calls == ['结束。', '结束。'], repr(calls))
events = [e['event'] for e in read_audit(tmpdir)]
check("retry audited", 'zero_audio_retry' in events, events)
check("first_delta fired exactly once", events.count('first_delta') == 1, events)
check("complete has audio_chunks=1", any(e['event'] == 'complete' and e['audio_chunks'] == 1
                                         for e in read_audit(tmpdir)))
check("no exhausted flag when retry succeeds",
      'zero_audio_exhausted' not in events, events)

print("== deterministic zero output exhausts boundedly (READING) ==")
reset_audit()
h = make_handler(reading_generation=5)
calls = []


def always_zero(self, segment, instruct=None):
    calls.append(segment)
    return iter(())


out = run(h, always_zero)
check("nothing yielded (no fabricated speech)", out == [], repr(out))
check("attempts bounded to 1 + ZERO_AUDIO_MAX_RETRIES",
      len(calls) == 1 + ZERO_AUDIO_MAX_RETRIES, len(calls))
events = [e['event'] for e in read_audit(tmpdir)]
check("exhaustion audited", 'zero_audio_exhausted' in events, events)
check("exhaustion records attempt count", any(
    e['event'] == 'zero_audio_exhausted' and e['attempts'] == 1 + ZERO_AUDIO_MAX_RETRIES
    for e in read_audit(tmpdir)))
check("complete still closes the audit shape", 'complete' in events, events)

print("== no retry once audio was produced (no repeated reading) ==")
reset_audit()
h = make_handler(reading_generation=5)
calls = []


def one_chunk(self, segment, instruct=None):
    calls.append(segment)
    return iter([b'A' * 16])


out = run(h, one_chunk)
check("single attempt", len(calls) == 1, len(calls))
check("audio yielded once", out == [b'A' * 16], repr(out))

print("== cancellation stops the retry (stop semantics preserved) ==")
reset_audit()
h = make_handler(reading_generation=4)
# Simulate a stop: reading_bridge.generation() has moved past 4. Patch the bridge
# module the adapter imports, without touching the real 8765 process.
import speech_to_speech.api.openai_realtime as realtime_api
real_gen = realtime_api.reading_bridge.generation
realtime_api.reading_bridge.generation = lambda: 9
try:
    calls = []
    out = run(h, always_zero)
finally:
    realtime_api.reading_bridge.generation = real_gen
check("cancelled zero attempt does not re-generate", len(calls) == 1, len(calls))
check("no audio after cancellation", out == [], repr(out))
events = [e['event'] for e in read_audit(tmpdir)]
check("cancellation audited", 'zero_audio_cancelled' in events, events)
check("no exhaustion on cancellation", 'zero_audio_exhausted' not in events, events)

print("== scope boundary: ROLEPLAY (no reading input) keeps the baseline path ==")
reset_audit()
h = make_handler(reading_generation=None)
calls = []
out = run(h, always_zero)
check("ROLEPLAY zero output: exactly one attempt, no retry",
      len(calls) == 1, len(calls))
events = [e['event'] for e in read_audit(tmpdir)]
check("no retry/exhausted events outside READING",
      'zero_audio_retry' not in events and 'zero_audio_exhausted' not in events, events)

print("== exceptions are not masked by the retry ==")
reset_audit()
h = make_handler(reading_generation=5)


def boom(self, segment, instruct=None):
    raise RuntimeError('GPU OOM')


try:
    run(h, boom)
    raised = False
except RuntimeError:
    raised = True
check("exception propagates", raised)
events = [e['event'] for e in read_audit(tmpdir)]
check("failure audited", any(e['event'] == 'failed' and e['error'] == 'RuntimeError'
                             for e in read_audit(tmpdir)), events)

print("== B/C/A strategies still segment identically through the loop ==")
reset_audit()
for strat, segs in (('A', ['结束。']), ('C', ['结束。'])):
    h = make_handler(reading_generation=5, strategy=strat)
    seen = []
    out = run(h, lambda self, s, instruct=None: (seen.append(s), iter([b'A' * 16]))[1])
    check(f"strategy {strat} keeps single segment", seen == segs, seen)
h = make_handler(reading_generation=5, strategy='B')
seen = []
out = run(h, lambda self, s, instruct=None: (seen.append(s), iter([b'A' * 16]))[1])
check("strategy B merge keeps '结束。' segment", seen == ['结束。'], seen)

tmpdir.cleanup()
print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
