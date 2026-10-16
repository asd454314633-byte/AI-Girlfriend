"""Regression gate: progressive FasterWhisper prefixes never become LLM input.

This suite uses the production handler class with a recording model stub.  It does
not load Whisper or open a microphone, but it exercises the exact ``process`` method
that sits between VAD and the LLM queue.  Run directly (no pytest dependency):

    runtime/core/venv/Scripts/python.exe runtime/tests/test_mic_final_only.py
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from speech_to_speech.STT.faster_whisper_handler import FasterWhisperSTTHandler
from speech_to_speech.pipeline.messages import VADAudio


FINAL_TEXT = "今天天气很好，我准备出去散步。"


class MicTurnHarness:
    """Minimal VAD -> STT -> LLM-queue seam used by all three assertions."""

    def __init__(self) -> None:
        self.handler = object.__new__(FasterWhisperSTTHandler)
        self.handler.gen_kwargs = {}
        self.model_calls: list[int] = []
        self.llm_inputs: list[str] = []
        self.speech_stopped = False

        def transcribe(audio: np.ndarray, **_kwargs: object):
            self.model_calls.append(len(audio))
            return [SimpleNamespace(text=FINAL_TEXT, start=0, end=len(audio) / 16000)], None

        self.handler.model = SimpleNamespace(transcribe=transcribe)

    def _deliver(self, audio: VADAudio) -> None:
        for transcription in self.handler.process(audio):
            self.llm_inputs.append(transcription.text)

    def progressive(self, seconds: int) -> None:
        assert not self.speech_stopped
        self._deliver(VADAudio(
            audio=np.zeros(16000 * seconds, dtype=np.float32),
            mode="progressive",
            turn_id="turn-final-only",
            turn_revision=0,
        ))

    def speech_stopped_final(self, seconds: int) -> None:
        self.speech_stopped = True
        self._deliver(VADAudio(
            audio=np.zeros(16000 * seconds, dtype=np.float32),
            mode="final",
            turn_id="turn-final-only",
            turn_revision=0,
        ))


def progressive_prefix_does_not_enter_llm() -> None:
    turn = MicTurnHarness()
    assert getattr(FasterWhisperSTTHandler.process, "_mic_final_only_fix", False), (
        "providers.bootstrap lost the final-only wrapper"
    )
    turn.progressive(1)
    assert turn.model_calls == [], "a progressive prefix reached Whisper"
    assert turn.llm_inputs == [], "a progressive prefix entered the LLM queue"


def final_enters_llm_only_after_speech_stopped() -> None:
    turn = MicTurnHarness()
    turn.progressive(1)
    assert not turn.speech_stopped and turn.llm_inputs == []
    turn.speech_stopped_final(3)
    assert turn.speech_stopped
    assert turn.model_calls == [48000]
    assert turn.llm_inputs == [FINAL_TEXT]


def ten_seconds_of_continuous_speech_does_not_commit_early() -> None:
    turn = MicTurnHarness()
    for elapsed_seconds in range(1, 11):
        turn.progressive(elapsed_seconds)
        assert turn.llm_inputs == [], f"LLM received input at t={elapsed_seconds}s"
        assert turn.model_calls == [], f"Whisper final transcription ran at t={elapsed_seconds}s"
    turn.speech_stopped_final(10)
    assert turn.model_calls == [160000]
    assert turn.llm_inputs == [FINAL_TEXT]


TESTS = [
    progressive_prefix_does_not_enter_llm,
    final_enters_llm_only_after_speech_stopped,
    ten_seconds_of_continuous_speech_does_not_commit_early,
]


if __name__ == "__main__":
    for test in TESTS:
        test()
        print(f"  ok  {test.__name__}")
    print(f"mic final-only checks: PASS ({len(TESTS)})")
