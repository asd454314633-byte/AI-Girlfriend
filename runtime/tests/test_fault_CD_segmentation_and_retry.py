"""任务 §7 fault C (Fish 503 分段竞态) + fault D (长文本切分) — offline regression.

Runs with plain ``python`` (no pytest in this venv), the same convention as
``test_provider_contract.py``::

    runtime/core/venv/scripts/python.exe runtime/tests/test_fault_CD_segmentation_and_retry.py

Fakes only: no model weights, no GPU, no network.  The Fish worker is replaced by
``httpx.MockTransport``, which speaks exactly the two responses ``s2.cpp`` can give
on ``/generate`` (200 + PCM, or the single documented 503 "Server busy"), so the
retry loop is exercised on its real code path.

Fault C — what is pinned here (the three claims of 任务 §7.C):
  * a retry after a busy 503 cannot duplicate audio for a segment: nothing is
    yielded before a non-503 answer, and every attempt is one POST (no hidden
    transport resend);
  * the retried request carries the same form fields byte-for-byte, so a retry can
    neither lose nor reorder a segment;
  * segments are ordered by the single handler thread, so the emitted stream equals
    the concatenation of the successful responses in request order.

Fault D — what is pinned here: 禁止词内切断 / 禁止句意中间硬砍, and the boundary
priority  。！？……  >  ；  >  ：  >  ，  >  natural phrase edge, with a fixed-width
cut allowed only where no boundary exists at all (and never invented).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from threading import Event

import numpy as np

RUNTIME = Path(__file__).resolve().parents[1]
CORE = RUNTIME / "core"
for entry in (str(RUNTIME), str(CORE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import httpx  # noqa: E402

from providers.voice_text import pronounceable_voice_segments, split_voice_text  # noqa: E402
from s2s_business.fish_tts_handler import FishTTSHandler  # noqa: E402
from speech_to_speech.pipeline.messages import AUDIO_RESPONSE_DONE, EndOfResponse, TTSInput  # noqa: E402

# The only 503 the worker can produce (s2.cpp/src/s2_server.cpp:715-718, and the
# same text in its openapi/s2-openapi.yaml '503' example).
BUSY_BODY = "Server busy processing another synthesis request."

# A maximal run of printable ASCII without a space is one pronounceable "word" for
# a TTS: 1,234 / 12:30 / https://example.com/a?x=1 all carry clause punctuation
# *inside* them, so a cut there is 词内切断.
ASCII_TOKEN = re.compile(r"[!-~]{2,}")


def token_spans(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in ASCII_TOKEN.finditer(text)]


def assert_no_word_internal_cut(text: str, segments: list[str], label: str) -> None:
    """Every internal segment edge must sit outside any ASCII token."""
    spans = token_spans(text)
    cursor = 0
    for segment in segments[:-1]:
        body = segment.rstrip("，")
        if not body:
            continue
        start = text.find(body[:16], cursor)
        if start < 0:
            raise AssertionError(f"{label}: segment {body[:16]!r} is not a slice of the source")
        cursor = start + len(body)
        edge = start + len(body)
        for span_start, span_stop in spans:
            if span_start < edge < span_stop:
                offending = text[span_start:span_stop]
                raise AssertionError(
                    f"{label}: cut at {edge} splits {offending!r} into "
                    f"{text[span_start:edge]!r} + {text[edge:span_stop]!r}"
                )


def assert_no_content_lost(text: str, segments: list[str], label: str) -> None:
    """Whitespace and the injected 句读 aside, the splitter may not eat characters."""
    normalise = lambda value: re.sub(r"[\s，。]", "", value)  # noqa: E731
    if normalise("".join(segments)) != normalise(text):
        raise AssertionError(f"{label}: text changed across segmentation")


# ─────────────────────────────────────────────────────────────────────────────
# FAULT D — 长文本切分
# ─────────────────────────────────────────────────────────────────────────────

CORPUS = {
    "thousands": "这个月的收入是一千二百三十四元，折合 1,234,567.89 元，另有支出 98,765.43 元，"
                 "所以余额只有 1,000,000 元啦，我们得省着点花才行呢。",
    "clock": "我们约在 12:30 见面，然后 13:45 去看电影，晚上 19:00 再一起吃晚饭，你说好不好呀。",
    "decimal": "今天的温度是 23.5 度，明天会降到 18.2 度，后天还会继续下降到 15.7 度左右哦。",
    "money_list": "一共是三笔钱：1,200 元、2,400 元和 3,600 元，合计 7,200 元，你要不要听我念一遍。",
    "url": "详情去看 https://example.com/a/b/c?x=1&y=2 这里面有说明，还有另一个链接 "
           "http://test.org/p:8080/x 也一起看一下吧，别忘记啦。",
    "english": "The quick brown fox jumps over the lazy dog again and again while the sun "
               "goes down and the wind picks up. Then it stops.",
    "long_word": "supercalifragilisticexpialidociousandthensome " * 12,
    "emoji": "好呀 😊 我们出发吧 🎉🎉🎉 今天天气真的很好 ☀️☀️☀️ 我们去公园散步好不好 😄😄",
    "quotes": "她说：“你今天怎么这么晚才回来呀，我等了你好久好久呢。”然后她又说：“下次记得提前讲一声。”",
    "pre_split": "第一句话已经说完了。\n第二句话也说完了。\n第三句还是这样的一句话啊。",
    "ellipsis": "我其实……有点想你……但是不好意思讲出来……只好在这里等你回来啦。",
    "ascii_no_space": "hello,world this is a test hello,world this is a test hello,world",
}


def numeric_and_time_separators_are_never_boundaries():
    """The reproduced fault-D defect: 1,234 / 12:30 used to be cut in half."""
    for key in ("thousands", "clock", "money_list"):
        text = CORPUS[key]
        for cap in (16, 24, 48, 96, 160):
            segments = pronounceable_voice_segments(text, max_chars=cap)
            assert_no_word_internal_cut(text, segments, f"{key}@{cap}")
            assert_no_content_lost(text, segments, key)
    # explicit shape of the old failure: 2,400 / 7,200 used to be cut after the comma
    segments = pronounceable_voice_segments(CORPUS["money_list"], max_chars=24)
    joined = "".join(segments)
    for token in ("1,200", "2,400", "3,600", "7,200"):
        assert token in joined, (token, segments)
    assert_no_word_internal_cut(CORPUS["money_list"], segments, "money_list@24")


def urls_survive_a_small_split_target():
    """'?' and ':' rank above a space, so a URL used to be cut at 'c?'."""
    text = CORPUS["url"]
    for cap in (20, 30, 48, 160):
        segments = pronounceable_voice_segments(text, max_chars=cap)
        assert_no_word_internal_cut(text, segments, f"url@{cap}")
        joined = "".join(segments)
        assert "https://example.com/a/b/c?x=1&y=2" in joined, joined
        assert "http://test.org/p:8080/x" in joined, joined


def mixed_language_emoji_and_whitespace_keep_every_word():
    for key in ("english", "long_word", "emoji", "quotes", "ascii_no_space", "decimal"):
        text = CORPUS[key]
        for cap in (12, 48, 160):
            segments = pronounceable_voice_segments(text, max_chars=cap)
            assert_no_word_internal_cut(text, segments, f"{key}@{cap}")
            assert_no_content_lost(text, segments, key)


def boundary_priority_is_sentence_then_semicolon_then_colon_then_comma():
    """Each stronger class wins when both candidates are inside the split window."""
    # ：(rank 1) beats a ， that sits closer to the target (任务 §7-D order)
    text = "我先讲一下今天的安排：还有两件小事，你别急着走好吗。"
    assert pronounceable_voice_segments(text, max_chars=20)[0] == "我先讲一下今天的安排：", text
    # 。(rank 0) beats a ， closer to the target
    text = "你今天回来得有点晚了。外面还在下雨呢，先擦擦头发吧。"
    assert pronounceable_voice_segments(text, max_chars=20)[0] == "你今天回来得有点晚了。", text
    # ；(rank 1) beats ，(rank 2)
    text = "先听我说一句完整的话；后面还有两件小事，你别急着答复我呀。"
    assert pronounceable_voice_segments(text, max_chars=20)[0] == "先听我说一句完整的话；", text
    # an ASCII sentence end outranks a mere space (rank 3)
    text = "Stop right there! Then we can talk about something else for a while."
    assert pronounceable_voice_segments(text, max_chars=20)[0] == "Stop right there!", text
    # ，(rank 2) beats a space (rank 3)
    text = "a b c d e f g h 这是一句中文的话，后面还有一句在等着说。"
    assert pronounceable_voice_segments(text, max_chars=20)[0].endswith("，"), text


def a_boundary_in_the_first_half_of_the_target_is_not_a_cut_point():
    """Pinned deliberately: the splitter floors its search at ``max_chars // 2`` so a
    three-character sentence never becomes its own audio block.  The short sentence
    stays glued to what follows instead of fragmenting the prosody."""
    text = "好的。我们现在出发去做别的事情啦，回头再讲给你听呀。"
    first = pronounceable_voice_segments(text, max_chars=20)[0]
    assert first.startswith("好的。") and not first.endswith("。"), first


def no_boundary_means_no_cut():
    """A fixed-width guillotine is the last resort and is deliberately not used."""
    text = "这是一段完全没有标点的中文叙述" * 30
    assert split_voice_text(text, 48) == [text]
    assert pronounceable_voice_segments(text, 48) == [text + "。"]
    solid = "A" * 400
    assert split_voice_text(solid, 48) == [solid]
    # ...but a space-only difference in shape never cuts inside a latin word
    text = "word " * 60
    segments = pronounceable_voice_segments(text, max_chars=24)
    assert len(segments) > 1, segments
    for segment in segments:
        assert segment.strip("，。 ").replace(" ", "").isalnum(), segment
    assert_no_word_internal_cut(text, segments, "spaces")


def already_delimited_and_short_text_is_left_alone():
    assert pronounceable_voice_segments("已经结束。") == ["已经结束。"]
    assert pronounceable_voice_segments("请继续，") == ["请继续。"]
    assert pronounceable_voice_segments("") == []
    assert pronounceable_voice_segments("   \n  ") == []
    # text that already carries its own 句读 is sliced on it, never re-punctuated
    text = "第一句话已经说完了。\n第二句话也已经说完了。\n第三句话还是这样的一句话。"
    segments = pronounceable_voice_segments(text, max_chars=20)
    assert segments == ["第一句话已经说完了。", "第二句话也已经说完了。", "第三句话还是这样的一句话。"], segments
    assert_no_word_internal_cut(text, segments, "pre_split")
    assert_no_content_lost(text, segments, "pre_split")


def every_provider_split_target_uses_the_same_rules():
    """The manifest targets (48/96/160) are the ones the adapters pass in."""
    from providers import registry

    targets = set()
    for provider_id in ("fish", "higgs", "qwen3", "test_fake"):
        spec = registry.get("TTS", provider_id)
        targets.add(int(((spec or {}).get("delivery") or {}).get("max_chars") or 160))
    assert targets, "no delivery target found in the manifest"
    for target in sorted(targets):
        for text in CORPUS.values():
            segments = pronounceable_voice_segments(text, max_chars=target)
            assert_no_word_internal_cut(text, segments, f"target{target}")
            assert_no_content_lost(text, segments, f"target{target}")


# ─────────────────────────────────────────────────────────────────────────────
# FAULT C — Fish 503 分段竞态
# ─────────────────────────────────────────────────────────────────────────────


def pcm(samples: int, value: int) -> bytes:
    return np.full(samples, value, dtype="<i2").tobytes()


class FakeFishTransport:
    """The s2.cpp /generate contract: a scripted answer per attempt."""

    def __init__(self, script):
        self.script = list(script)
        self.bodies: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(request.content)
        status, payload = self.script[min(len(self.bodies) - 1, len(self.script) - 1)]
        if status == 200:
            return httpx.Response(
                200,
                content=payload,
                headers={
                    "x-audio-sample-rate": "16000",
                    "x-audio-encoding": "pcm_s16le",
                    "x-audio-channels": "1",
                },
            )
        return httpx.Response(status, text=payload)

    @property
    def attempts(self) -> int:
        return len(self.bodies)


def fish_handler(script):
    """The real adapter, with only its HTTP client replaced."""
    handler = object.__new__(FishTTSHandler)
    handler.should_listen = Event()
    handler.cancel_scope = None
    handler.speculative_turns = None
    transport = FakeFishTransport(script)
    handler.client = httpx.Client(transport=httpx.MockTransport(transport))
    return handler, transport


def form_fields(raw: bytes) -> dict[str, bytes]:
    """Decode a multipart body back into its named fields."""
    out: dict[str, bytes] = {}
    for chunk in raw.split(b'Content-Disposition: form-data; name="')[1:]:
        head, _, body = chunk.partition(b"\r\n\r\n")
        out[head.split(b'"')[0].decode()] = body.rsplit(b"\r\n--", 1)[0]
    return out


def samples_of(blocks) -> bytes:
    if not blocks:
        return b""
    return np.concatenate(list(blocks)).astype("<i2").tobytes()


def busy_503_retries_emit_audio_exactly_once():
    """(a) 重试不会重复生成 — one 200 in, one copy of its PCM out."""
    body = pcm(1024, 7)
    handler, transport = fish_handler([(503, BUSY_BODY), (503, BUSY_BODY), (200, body)])
    blocks = list(handler._generate_segment("你好呀。", segmented=False, token_cap=400))
    assert transport.attempts == 3, transport.attempts
    assert samples_of(blocks) == body, (
        f"emitted {sum(len(b) for b in blocks) * 2} bytes for a {len(body)}-byte answer "
        "-> duplicated or dropped audio"
    )
    assert all(len(block) == 512 for block in blocks), [len(b) for b in blocks]


def nothing_is_emitted_before_a_successful_answer():
    """(a) again, from the other side: the busy attempts cannot produce blocks."""
    body = pcm(1024, 9)
    handler, transport = fish_handler([(503, BUSY_BODY), (503, BUSY_BODY), (200, body)])
    generated = handler._generate_segment("你好呀。", segmented=False, token_cap=400)
    first = next(generated)
    assert transport.attempts == 3, (
        f"the adapter yielded audio after only {transport.attempts} attempts"
    )
    assert np.all(first == np.frombuffer(body, dtype="<i2")[:512]), first


def every_retry_reposts_the_identical_request():
    """(c) 不会漏 chunk — a retry replays the whole segment set, never part of it."""
    handler, transport = fish_handler([(503, BUSY_BODY)] * 3 + [(200, pcm(256, 4))])
    text = ("今天过得怎么样，" * 40) + "就这样啦。"
    list(handler.process(TTSInput(text=text, turn_id="turn-1", turn_revision=1)))
    assert transport.attempts == 4, transport.attempts
    parsed = [form_fields(raw) for raw in transport.bodies]
    assert all(row == parsed[0] for row in parsed), "a retry changed the request it resends"
    on_wire = parsed[0]["text"].decode().split("\n")
    local = pronounceable_voice_segments(text, max_chars=160)
    assert len(local) > 1, "corpus too short to prove multi-segment replay"
    assert on_wire == local, (on_wire, local)
    assert parsed[0]["params"].decode().count("segment_sentences") == 1


def busy_retry_keeps_segments_in_request_order():
    """(b) 不会乱序 — three turns, the middle one retried, come out A+B+C."""
    first, second, third = pcm(512, 1), pcm(512, 2), pcm(512, 3)
    handler, transport = fish_handler([
        (200, first), (503, BUSY_BODY), (503, BUSY_BODY), (200, second), (200, third),
    ])
    stream = bytearray()
    for text in ("第一句", "第二句", "第三句"):
        for block in handler._generate_segment(text, segmented=False, token_cap=400):
            stream += block.astype("<i2").tobytes()
    assert transport.attempts == 5, transport.attempts
    assert bytes(stream) == first + second + third, "segment order changed across a retry"


def a_second_concurrent_request_cannot_interleave_audio():
    """(b) structural: the busy answer carries no audio, so nothing can be spliced in.

    The worker serialises synthesis behind one CAS gate, so an overlapping turn is
    answered by 503 -- the adapter's only possible stream is its own audio.
    """
    mine = pcm(512, 11)
    handler, transport = fish_handler([(503, BUSY_BODY), (200, mine)])
    blocks = list(handler._generate_segment("只属于本 turn 的一句话。", segmented=True, token_cap=400))
    assert samples_of(blocks) == mine
    assert not any(np.any(block != np.full(len(block), 11, dtype="<i2")) for block in blocks), (
        "audio from another request leaked into this turn's stream"
    )


def non_busy_errors_are_not_retried_and_fail_loudly():
    """Scope guard: only the documented busy 503 is retried; a real failure raises."""
    handler, transport = fish_handler([(500, "Synthesis failed."), (200, pcm(512, 1))])
    try:
        list(handler._generate_segment("你好。", segmented=False, token_cap=400))
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 500, exc
    else:
        raise AssertionError("a 500 must not be swallowed as a busy answer")
    assert transport.attempts == 1, transport.attempts


def busy_wait_that_never_clears_is_reported_not_truncated():
    """The one bounded exit: a stuck worker raises instead of emitting half a turn.

    ``time.monotonic`` is advanced on purpose so the 30 s budget expires after two
    attempts; the real 250 ms sleeps stay, which keeps the test offline and quick.
    """
    handler, transport = fish_handler([(503, BUSY_BODY)] * 400)
    import time as real_time

    # The budget baseline is read before the first POST, so driving the fake clock off
    # the attempt count makes the sequence independent of any other monotonic() user.
    base = real_time.monotonic()
    real_monotonic = real_time.monotonic

    def fake_monotonic():
        if transport.attempts >= 2:
            return base + 40.0
        if transport.attempts == 1:
            return base + 1.0
        return base

    real_time.monotonic = fake_monotonic
    try:
        try:
            list(handler._generate_segment("你好。", segmented=False, token_cap=400))
        except RuntimeError as exc:
            assert "remained busy" in str(exc), exc
        else:
            raise AssertionError("an endless busy storm must raise, not emit truncated audio")
    finally:
        real_time.monotonic = real_monotonic
    assert transport.attempts == 2, transport.attempts


def fish_turn_terminator_is_untouched():
    """§7.F guard: this suite must not move the audio turn terminator."""
    handler, _transport = fish_handler([(200, pcm(512, 1))])
    assert list(handler.process(EndOfResponse(turn_id="turn-1", turn_revision=1))) == [
        AUDIO_RESPONSE_DONE
    ]


TESTS = [
    numeric_and_time_separators_are_never_boundaries,
    urls_survive_a_small_split_target,
    mixed_language_emoji_and_whitespace_keep_every_word,
    boundary_priority_is_sentence_then_semicolon_then_colon_then_comma,
    no_boundary_means_no_cut,
    already_delimited_and_short_text_is_left_alone,
    every_provider_split_target_uses_the_same_rules,
    busy_503_retries_emit_audio_exactly_once,
    nothing_is_emitted_before_a_successful_answer,
    every_retry_reposts_the_identical_request,
    busy_retry_keeps_segments_in_request_order,
    a_second_concurrent_request_cannot_interleave_audio,
    non_busy_errors_are_not_retried_and_fail_loudly,
    busy_wait_that_never_clears_is_reported_not_truncated,
    fish_turn_terminator_is_untouched,
]

if __name__ == "__main__":
    failures = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report and keep going
            failures += 1
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"  ok   {test.__name__}")
    if failures:
        print(f"fault C/D checks: FAIL ({failures} of {len(TESTS)})")
        raise SystemExit(1)
    print(f"fault C/D checks: PASS ({len(TESTS)})")
