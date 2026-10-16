"""CORE CONTRACT layer of the 任务 §25 test matrix: fakes only, no model, no GPU.

Runs with plain ``python`` (no pytest in this venv), the same convention as
``test_manual_voice_mode.py``::

    runtime/core/venv/scripts/python.exe runtime/tests/test_provider_contract.py

What these checks protect is the *contract*, not any vendor's sound quality:

* a provider that declares capabilities gets exactly those emissions, in the
  shape its manifest says, on the wire the adapter would send;
* a selection made for another provider stays inert;
* an undeclared capability is dropped instead of being passed through;
* a turn always terminates in ``AUDIO_RESPONSE_DONE`` through the generic path;
* the language-model stage receives one serialized system block and a test can
  read it back — the boundary the Context Builder depends on.
"""

from __future__ import annotations

import sys
from pathlib import Path
from queue import Queue
from threading import Event

import numpy as np
from openai.types.realtime import SessionUpdateEvent

CORE = Path(__file__).resolve().parents[1] / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from providers import delivery, registry  # noqa: E402
from providers.adapters import fake_llm_adapter, recorder  # noqa: E402
from providers.tts_factory import build  # noqa: E402

from speech_to_speech.LLM.chat import Chat, make_system_message, make_user_message  # noqa: E402
from speech_to_speech.api.openai_realtime.runtime_config import RuntimeConfig  # noqa: E402
from speech_to_speech.pipeline.messages import (  # noqa: E402
    AUDIO_RESPONSE_DONE,
    EndOfResponse,
    GenerateResponseRequest,
    TTSInput,
)


def config_for(backend: str, mode: dict) -> RuntimeConfig:
    """The same session shape the browser sends over ``session.update``."""
    event = SessionUpdateEvent.model_validate({
        "type": "session.update",
        "session": {"type": "realtime", "tts_backend": backend, "manual_voice_mode": mode},
    })
    config = RuntimeConfig()
    config.apply_session_update(event.session)
    return config


def item(text: str, backend: str, mode: dict, turn: str = "turn-1") -> TTSInput:
    return TTSInput(
        text=text,
        turn_id=turn,
        turn_revision=1,
        runtime_config=config_for(backend, mode),
    )


def make_tts(**kwargs):
    """Build the fixture through the *production* registry path, minus the gate."""
    return build("test_fake", Event(), Queue(), Queue(), Event(), require_enabled=False, **kwargs)


def provider_resolves_from_manifest_only():
    spec = registry.spec_for_flag("TTS", "test_fake")
    assert spec is not None and spec["id"] == "test_fake"
    assert spec["handler"]["module"].startswith("providers.adapters.")
    handler = make_tts()
    assert handler.provider_id == "test_fake"
    assert isinstance(handler._tone({}), np.ndarray)


def declared_capabilities_reach_the_wire():
    handler = make_tts()
    blocks = list(handler.process(item("你好呀。", "test_fake", {"emotion": "calm", "pitch": "high"})))
    row = recorder.latest("tts", "test_fake")
    assert "<fake:calm>" in row["spoken"][0], row
    assert "<fake:pitch_high>" in row["spoken"][0], row
    assert row["spoken"][0].endswith("你好呀。"), row
    assert blocks and all(isinstance(b, np.ndarray) and b.dtype == np.int16 for b in blocks)
    assert all(len(b) == 512 for b in blocks)
    assert sum(len(b) for b in blocks) == row["blocks"] * 512


def selection_for_another_provider_stays_inert():
    handler = make_tts()
    list(handler.process(item("你好呀。", "higgs", {"emotion": "elation"})))
    assert recorder.latest("tts", "test_fake")["spoken"] == ["你好呀。"]


def undeclared_capability_is_dropped_not_emitted():
    handler = make_tts()
    # test_fake declares emotion + pitch, and explicitly NOT speed.
    list(handler.process(item("慢一点说。", "test_fake", {"speed": "slow"})))
    row = recorder.latest("tts", "test_fake")
    assert row["spoken"] == ["慢一点说。"], row
    assert row["selection"] == {}, row


def turn_always_closes():
    handler = make_tts()
    assert list(handler.process(EndOfResponse(turn_id="turn-9", turn_revision=1))) == [AUDIO_RESPONSE_DONE]


def long_text_uses_the_declared_split_target():
    handler = make_tts()
    text = "这是一段慢慢讲的长文本，" * 12 + "到这里就结束了。"
    list(handler.process(item(text, "test_fake", {})))
    row = recorder.latest("tts", "test_fake")
    assert row["max_chars"] == delivery.split_target(registry.get("TTS", "test_fake"))
    assert len(row["segments"]) >= 2, row
    assert "".join(row["segments"]) == text


def unknown_flag_lists_registered_providers():
    try:
        build("no_such_backend", Event(), Queue(), Queue(), Event())
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("an unknown --tts value must not build a handler")
    assert "Unsupported TTS backend" in message
    for flag in registry.flag_values("TTS"):
        assert flag in message, f"{flag} missing from the error text: {message}"


def llm_stage_receives_one_serialized_system_block():
    chat = Chat(10)
    try:
        chat.init_chat(make_system_message("你是小满。你说话简短。"))
        chat.add_item(make_user_message("今天有点累。"))
        handler = fake_llm_adapter.FakeLLMHandler(
            Event(), Queue(), Queue(), setup_args=(Event(),), setup_kwargs={},
        )
        out = list(handler.process(GenerateResponseRequest(runtime_config=RuntimeConfig(chat=chat))))
    finally:
        chat.close()
    chunks = [row for row in out if getattr(row, "text", None)]
    assert chunks and out[-1].__class__.__name__ == "EndOfResponse", out
    row = recorder.latest("llm", "test_fake")
    assert "你是小满" in row["system_text"], row
    assert "今天有点累" in row["user_text"], row
    assert row["message_count"] == 2, row


TESTS = [
    provider_resolves_from_manifest_only,
    declared_capabilities_reach_the_wire,
    selection_for_another_provider_stays_inert,
    undeclared_capability_is_dropped_not_emitted,
    turn_always_closes,
    long_text_uses_the_declared_split_target,
    unknown_flag_lists_registered_providers,
    llm_stage_receives_one_serialized_system_block,
]

if __name__ == "__main__":
    recorder.clear()
    for test in TESTS:
        recorder.clear()
        test()
        print(f"  ok  {test.__name__}")
    print(f"core contract checks: PASS ({len(TESTS)})")
