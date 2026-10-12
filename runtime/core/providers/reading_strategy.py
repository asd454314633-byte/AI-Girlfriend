"""READING generation authority and sequential source-unit planning (no model imports)."""
from pathlib import Path
import logging
import time
import unicodedata
from speech_to_speech.pipeline.messages import TTSInput
from .voice_text import pronounceable_voice_segments

logger = logging.getLogger(__name__)
STRATEGY_FILE = Path(__file__).resolve().parents[2] / 'windows/tts_segmentation_strategy.txt'
DEFAULT_SEGMENTATION_STRATEGY = 'B'
SEGMENTATION_STRATEGIES = ('A', 'B', 'C')

def resolve_segmentation_strategy(path=STRATEGY_FILE):
    try:
        value = path.read_text(encoding='utf-8').strip().upper()
    except OSError as exc:
        logger.warning('READING_STRATEGY_FALLBACK requested=unavailable runtime=B path=%s reason=%s', path, type(exc).__name__)
        return 'B'
    if value not in SEGMENTATION_STRATEGIES:
        logger.warning('READING_STRATEGY_FALLBACK requested=%r runtime=B path=%s reason=invalid', value, path)
        return 'B'
    return value

class ReadingTTSInput(TTSInput):
    reading_strategy: str = 'A'
    source_chunks: list[dict] = []
    reading_generation: int | None = None

def tiny(text):
    return sum(not c.isspace() and not unicodedata.category(c).startswith('P') for c in text) <= 2

def short_reading_unit(sources):
    # Sub-second standalone phrases exhaust the Avatar render lookahead before
    # the next long WAV finishes synthesis. Merge sequential original sources,
    # preserving all source locations; final sources still flush exactly once.
    return sum(sum(not c.isspace() and not unicodedata.category(c).startswith('P')
                   for c in s['text']) for s in sources) < 12

def short_final_tail(source, following):
    """Only same-chapter, sequential, terminal short sources can join upstream.

    This is source planning before synthesis, not another pending/audio queue.
    A resume starting at the final source has no predecessor to replay.
    """
    if source.get('is_last') or not following or len(following) > 12:
        return []
    index = source['chunk_index']
    for offset, item in enumerate(following, 1):
        if (item.get('document_id') != source['document_id']
                or item.get('chapter_index') != source['chapter_index']
                or item.get('chunk_index') != index + offset
                or not isinstance(item.get('text'), str) or not item['text'].strip()
                or bool(item.get('is_last')) != (offset == len(following))):
            return []
    return [dict(item) for item in following] if short_reading_unit(following) else []

class ReadingUnitPlanner:
    """One serialized reading stream; checkpoints remain outside this planner.

    Pending units have no audio and no checkpoint commit. Stop clears them.
    A final short source flushes once; abandoned pending sources expire and must
    be resubmitted from the unchanged checkpoint, never silently skipped.
    """
    def __init__(self):
        self.reset()

    def reset(self):
        self.pending = []
        self.key = None
        self.since = 0.0

    def expire(self):
        if self.pending and time.monotonic() - self.since > 30:
            logger.warning('READING_PENDING_EXPIRED source_chunks=%s', self.pending)
            self.reset()
            return True
        return False

    def submit(self, text, source, strategy, generation):
        source = dict(source)
        following = source.pop('following_sources', [])
        expired = self.expire()
        key = (source['document_id'], source['chapter_index'], generation, strategy)
        index = source['chunk_index']
        if expired:
            return {'ok': False, 'reason': 'PENDING_EXPIRED'}
        if self.pending and self.key[:3] == key[:3] and self.key[3] != strategy:
            self.reset()
            return {'ok': False, 'reason': 'PENDING_STRATEGY_CHANGED'}
        if self.pending and self.key != key:
            self.reset()
        if self.pending and index != self.pending[-1]['chunk_index'] + 1:
            self.reset()
            return {'ok': False, 'reason': 'NON_SEQUENTIAL_SOURCE'}
        sources = self.pending + [dict(source, text=text)]
        if strategy == 'B':
            sources += short_final_tail(source, following)
        index = sources[-1]['chunk_index']
        if strategy == 'B' and short_reading_unit(sources) and not sources[-1].get('is_last'):
            self.pending = sources
            self.key = key
            self.since = time.monotonic()
            logger.info('READING_PENDING strategy=B source_chunks=%s', [s['chunk_index'] for s in sources])
            return {'ok': True, 'pending': True, 'source_chunks': sources,
                    'next_chunk_index': index + 1, 'strategy': strategy}
        # Keep all original source text and locations in metadata. Only the TTS
        # pronunciation form adds the same terminal punctuation as baseline A.
        text = ''.join(''.join(pronounceable_voice_segments(s['text'], 96))
                       if tiny(s['text']) else s['text'] for s in sources)
        if strategy != 'B':
            text = sources[0]['text']
        self.reset()
        return {'ok': True, 'pending': False, 'text': text, 'source_chunks': sources,
                'strategy': strategy, 'completed_chunk_index': index}
