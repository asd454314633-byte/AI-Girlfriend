"""Exercise cancellation before the first delta without loading model weights."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))
from providers.reading_strategy import ReadingTTSInput
from providers.adapters import qwen3_tts_adapter as adapter
from speech_to_speech.api.openai_realtime import reading_bridge as bridge
from speech_to_speech.pipeline.messages import TTSInput, EndOfResponse


class CancelGeneration(unittest.TestCase):
    def setUp(self):
        self.handler = adapter.Qwen3DeliveryTTSHandler.__new__(adapter.Qwen3DeliveryTTSHandler)
        self.gen = bridge.generation()

    def run_input(self, msg, parent):
        with patch.object(adapter, 'for_handler', return_value=None), patch.object(
                adapter._VendorQwen3TTSHandler, 'process', parent):
            return list(self.handler.process(msg))

    def test_stop_during_inference_before_first_delta(self):
        def parent(_self, msg):
            bridge._bump_generation()
            yield b'late speech'
        self.assertEqual(self.run_input(ReadingTTSInput(text='test', reading_generation=self.gen), parent), [])

    def test_already_stale_does_not_start_inference(self):
        bridge._bump_generation()
        def parent(_self, msg):
            self.fail('stale inference started')
            yield b'never'
        self.assertEqual(self.run_input(ReadingTTSInput(text='test', reading_generation=self.gen), parent), [])

    def test_current_reading_audio_is_preserved(self):
        self.assertEqual(self.run_input(ReadingTTSInput(text='test', reading_generation=self.gen),
            lambda *_: iter([b'first', b'last'])), [b'first', b'last'])

    def test_roleplay_and_closure_are_preserved(self):
        for msg in [TTSInput(text='test'), EndOfResponse()]:
            self.assertEqual(self.run_input(msg, lambda *_: iter([b'unchanged'])), [b'unchanged'])


if __name__ == '__main__':
    unittest.main()
