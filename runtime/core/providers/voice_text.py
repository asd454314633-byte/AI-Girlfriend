"""Clause splitting for speech: a target length, never a raw character cut.

Moved out of ``speech_to_speech/TTS/manual_voice_mode.py`` so that a plain
``pip install`` of the upstream package cannot take project behaviour with it.
The provider-specific prefix/instruction tables that used to sit next to this
function now live in ``providers/manifests/tts.json`` as ``emit`` values and are
applied by :mod:`providers.delivery`.
"""
from __future__ import annotations

def _is_token_interior(text: str, index: int) -> bool:
    """True when ``text[index]`` is ASCII punctuation glued inside one ASCII token.

    ``1,234`` ``12:30`` ``https://example.com/a?x=1`` all carry clause punctuation
    *inside* a single word, and a TTS must never be told to pause there: the number
    would be read as two fragments.  A clause break needs at least one non-ASCII (or
    space) neighbour, which is what every real Chinese/English clause boundary has.
    """
    char = text[index]
    if not char.isascii() or char.isspace():
        return False
    previous = text[index - 1] if index else ""
    following = text[index + 1] if index + 1 < len(text) else ""
    return all(item and item.isascii() and not item.isspace() for item in (previous, following))


def split_voice_text(text: str, max_chars: int = 48) -> list[str]:
    """Split on sentence/clause/phrase boundaries, never at a raw character cap.

    ``max_chars`` is a target, not a guillotine.  A punctuation-free clause
    without a defensible boundary is kept whole rather than cut inside a word.
    """
    remaining = text.strip()
    if not remaining:
        return []
    max_chars = max(8, max_chars)
    # If punctuation is absent, these words introduce or close a natural clause.
    before_words = (
        "但是", "不过", "而且", "然后", "接着", "随后", "于是", "所以", "因此",
        "因为", "虽然", "如果", "即使", "同时", "直到", "并且", "然而", "反而",
        "可是", "只是", "还有", "而我", "而你", "我却", "你却", "她却", "他却",
        "就在这时", "就在此时", "随着", "伴随着", "这时候", "这时", "那时",
        "她望着", "他望着", "我望着", "心中", "那是", "仿佛", "忽然", "突然",
    )
    after_words = ("的时候", "之后", "以后", "那一刻", "这一刻", "与此同时")
    chunks: list[str] = []
    while len(remaining) > max_chars:
        lower = max(1, max_chars // 2)
        upper = min(len(remaining) - 1, max_chars * 2)
        candidates: list[tuple[int, int]] = []
        for i, char in enumerate(remaining[:upper]):
            position = i + 1
            if position < lower:
                continue
            if _is_token_interior(remaining, i):
                # A separator inside a number / clock time / URL: not a boundary.
                continue
            if char in "。！？!?…\n":
                candidates.append((position, 0))
            elif char in "；;：:":
                candidates.append((position, 1))
            elif char in "，,、":
                candidates.append((position, 2))
            elif char.isspace():
                candidates.append((position, 3))
        for word in before_words:
            start = 0
            while (position := remaining.find(word, start, upper)) >= 0:
                if lower <= position <= upper:
                    candidates.append((position, 4))
                start = position + len(word)
        for word in after_words:
            start = 0
            while (position := remaining.find(word, start, upper)) >= 0:
                end = position + len(word)
                if lower <= end <= upper:
                    candidates.append((end, 4))
                start = end
        if not candidates:
            # No safe semantic boundary: preserve the clause intact.  The TTS
            # adapter logs this overlong input so it cannot fail silently.
            break
        within_target = [(pos, rank) for pos, rank in candidates if pos <= max_chars]
        pool = within_target or candidates
        cut, _rank = min(pool, key=lambda value: (value[1], abs(value[0] - max_chars)))
        head = remaining[:cut].strip()
        if head:
            chunks.append(head)
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def pronounceable_voice_segments(text: str, max_chars: int = 48) -> list[str]:
    """Keep internal clause punctuation; close only the actual response end."""
    segments = split_voice_text(text, max_chars)
    for index in range(len(segments) - 1):
        if not segments[index].endswith(("。", "！", "？", "!", "?", ".", "…", "；", ";", "：", ":", "，", ",", "、")):
            # A genuine semantic clause break is a pause, not a new sentence.
            segments[index] += "，"
    if segments and not segments[-1].rstrip('”’"\'').endswith(("。", "！", "？", "!", "?", ".")):
        if segments[-1].endswith(("，", ",", "、", "；", ";", "：", ":")):
            segments[-1] = segments[-1][:-1] + "。"
        else:
            segments[-1] += "。"
    return segments


# Punctuation ignored when measuring whether a segment is "tiny" (strategy B).
_TINY_STRIP_CHARS = "。！？!?…；;：:，,、”“’'\""


def merge_tiny_segments(segments: list[str], min_chars: int = 3) -> list[str]:
    """TTS segmentation strategy B: never let a 1–2 character fragment stand alone.

    Chapter numbers ("一" "二" …) measured against punctuation leave ≤2 real
    characters, and one tiny segment means one whole TTS generation whose
    prosody context starts from zero (measured 2026-10-03: a lone "二。" cost
    1.5–3.0s of audio).  A tiny segment joins the NEXT segment; only a trailing
    tiny segment falls back to the previous one.  Everything else is untouched,
    so this is safe to stack after :func:`pronounceable_voice_segments`.

    Evidence: evidence/MODE_SYSTEM_V1/TTS_GRANULARITY_EXP_20261003/REPORT.md
    (user ruling 2026-10-04: strategy B is the production default).
    """
    if len(segments) <= 1:
        return list(segments)
    merged: list[str] = []
    index = 0
    total = len(segments)
    while index < total:
        segment = segments[index]
        if len(segment.rstrip(_TINY_STRIP_CHARS)) < min_chars:
            if index + 1 < total:
                merged.append(segment + segments[index + 1])
                index += 2
                continue
            if merged:  # trailing tiny segment -> append to previous unit
                merged[-1] = merged[-1] + segment
                index += 1
                continue
        merged.append(segment)
        index += 1
    return merged
