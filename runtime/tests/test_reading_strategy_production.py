"""Sequential bridge -> typed TTS input -> actual adapter seam regression.

Only model/audio generation is captured. Inputs are submitted separately, not
pre-concatenated. The unique production authority is temporarily changed and
restored in finally. Run before deployment with services idle.
"""
import sys
import queue
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'runtime/core'))
from providers import reading_strategy as rs
from providers.adapters import qwen3_tts_adapter as adapter
from providers.voice_text import pronounceable_voice_segments
from speech_to_speech.api.openai_realtime import reading_bridge as bridge
from speech_to_speech.pipeline.messages import TTSInput

def vendor_process(self, item):
    if isinstance(item, TTSInput):
        yield from self._process_voice_clone(item.text)

class ProductionSequence(unittest.TestCase):
    def setUp(self):
        self.saved = rs.STRATEGY_FILE.read_bytes()
        self.handler = object.__new__(adapter.Qwen3DeliveryTTSHandler)
        self.handler.queue_in = queue.Queue()
        self.calls = []
        def capture(text, instruct=None):
            message = getattr(self.handler, '_reading_input', None)
            self.calls.append(dict(text=text, strategy=message.reading_strategy if message else 'A',
                                   sources=message.source_chunks if message else []))
            # One successful audio chunk: a clean zero-output generation now has
            # bounded-retry semantics (test_tts_zero_audio_boundary.py), so the
            # "normal generation" fake must not model the fault.
            return iter((b'\x00' * 32,))
        self.handler._generate_voice_clone = capture
        self.pool = [SimpleNamespace(handlers=[self.handler], session=None, index=0)]
        bridge._reading_planner.reset()
        self.vendor = patch.object(adapter._VendorQwen3TTSHandler, 'process', vendor_process)
        self.vendor.start()
    def tearDown(self):
        self.vendor.stop()
        rs.STRATEGY_FILE.write_bytes(self.saved)
        bridge._reading_planner.reset()
    def strategy(self, value):
        rs.STRATEGY_FILE.write_text(value, encoding='utf-8')
    def submit(self, index, text, last=False, document='fixture'):
        result = bridge.speak(self.pool, text, 'zh', bridge.generation(), dict(
            document_id=document, chapter_index=0, chunk_index=index,
            char_start=index*10, char_end=index*10+len(text), is_last=last))
        self.assertTrue(result['ok'], result)
        while not self.handler.queue_in.empty():
            list(self.handler.process(self.handler.queue_in.get_nowait()))
        return result
    def test_unique_default_path_ABC(self):
        self.assertEqual(rs.STRATEGY_FILE, ROOT / 'runtime/windows/tts_segmentation_strategy.txt')
        for value in 'ABC':
            self.strategy(value)
            self.assertEqual(adapter.resolve_segmentation_strategy(), value)
            self.submit(0, '这是一段真实来源的朗读正文。', True)
            self.assertEqual(self.calls[-1]['strategy'], value)
    def test_roleplay_ABC_original_no_reading_authority(self):
        text = '海风经过长长的走廊，窗边的灯一直亮着。' * 5 + '。二'
        expected = pronounceable_voice_segments(text, 96)
        for value in 'ABC':
            self.strategy(value)
            self.calls.clear()
            with patch.object(bridge, 'resolve_segmentation_strategy', side_effect=AssertionError('roleplay read authority')), patch.object(adapter, 'resolve_segmentation_strategy', side_effect=AssertionError('roleplay read authority')):
                list(self.handler.process(TTSInput(text=text)))
            self.assertEqual([c['text'] for c in self.calls], expected)
            self.assertTrue(all(c['sources'] == [] for c in self.calls))
    def test_B_separate_sources_mapping(self):
        self.strategy('B')
        pending = self.submit(7, '二')
        self.assertTrue(pending['pending'])
        self.assertEqual(self.calls, [])
        receipt = self.submit(8, '第二天早晨，雨仍然没有停。', True)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['text'], '二。第二天早晨，雨仍然没有停。')
        self.assertEqual([s['chunk_index'] for s in self.calls[0]['sources']], [7, 8])
        self.assertEqual(receipt['completed_chunk_index'], 8)
        self.assertEqual(''.join(s['text'] for s in receipt['source_chunks']), '二第二天早晨，雨仍然没有停。')
    def test_consecutive_short_then_normal(self):
        self.strategy('B')
        self.submit(0, '一。')
        self.submit(1, '二')
        self.assertEqual(self.calls, [])
        self.submit(2, '正文仍然按原顺序朗读。', True)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([s['chunk_index'] for s in self.calls[0]['sources']], [0, 1, 2])
    def test_final_short_flush(self):
        self.strategy('B')
        receipt = self.submit(0, '十', True)
        self.assertFalse(receipt['pending'])
        self.assertEqual(self.calls[0]['text'], '十。')
        self.assertFalse(bridge._reading_planner.pending)
    def test_stop_pending_stale_resume(self):
        self.strategy('B')
        self.submit(0, '三')
        gen = bridge.generation()
        bridge.stop(self.pool)
        self.assertFalse(bridge._reading_planner.pending)
        result = bridge.speak(self.pool, '不应出声。', 'zh', gen)
        self.assertEqual(result['reason'], 'STALE_GENERATION')
        self.submit(0, '三')
        self.submit(1, '恢复后从原章节号开始。', True)
        self.assertEqual([s['chunk_index'] for s in self.calls[0]['sources']], [0, 1])
    def test_new_document_discards_old_pending(self):
        self.strategy('B')
        self.submit(0, '四')
        self.submit(0, '新文档正文。', True, 'new')
        self.assertEqual(self.calls[0]['text'], '新文档正文。')
    def test_A_C_real_sequence(self):
        text = '海风经过长长的走廊，窗边的灯一直亮着。' * 12
        self.strategy('A')
        self.submit(0, '二')
        self.submit(1, text, True)
        self.assertEqual(self.calls[0]['text'], '二。')
        self.assertGreater(len(self.calls), 2)
        self.calls.clear()
        self.strategy('C')
        self.submit(0, text, True)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['text'], text)
    def test_ten_chapters_sequential_zero_short_generations(self):
        self.strategy('B')
        original = []
        for n, chapter in enumerate('一二三四五六七八九十'):
            original.extend([chapter, '第二天早晨，雨仍然没有停。'])
            self.submit(2*n, chapter)
            self.submit(2*n+1, original[-1], n == 9)
        self.assertEqual(len(self.calls), 10)
        self.assertTrue(all(not rs.tiny(c['text']) for c in self.calls))
        sources = [s for c in self.calls for s in c['sources']]
        self.assertEqual([s['chunk_index'] for s in sources], list(range(20)))
        self.assertEqual([s['text'] for s in sources], original)
    def test_pending_expiry_and_strategy_change_fail_closed(self):
        self.strategy('B')
        self.submit(0, '五')
        bridge._reading_planner.since -= 31
        result = bridge.speak(self.pool, '不能跳过章节号。', 'zh', bridge.generation(),
            dict(document_id='fixture', chapter_index=0, chunk_index=1, is_last=True))
        self.assertEqual(result['reason'], 'PENDING_EXPIRED')
        self.submit(0, '五')
        self.strategy('C')
        result = bridge.speak(self.pool, '不能跳过章节号。', 'zh', bridge.generation(),
            dict(document_id='fixture', chapter_index=0, chunk_index=1, is_last=True))
        self.assertEqual(result['reason'], 'PENDING_STRATEGY_CHANGED')

if __name__ == '__main__':
    unittest.main(verbosity=2)
