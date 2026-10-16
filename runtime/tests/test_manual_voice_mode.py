"""Manual voice controls must stay backend-scoped and inert by default."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

from openai.types.realtime import SessionUpdateEvent
from speech_to_speech.api.openai_realtime.runtime_config import RuntimeConfig

from speech_to_speech.TTS.manual_voice_mode import (
    fish_text,
    HIGGS_EMOTIONS,
    HIGGS_EFFECTS,
    HIGGS_EXPRESSIVENESS,
    HIGGS_PITCH,
    HIGGS_SPEED,
    HIGGS_STYLES,
    higgs_prefix,
    qwen_instruct,
    pronounceable_voice_segments,
    selection,
    split_voice_text,
)
from s2s_business.higgs_tts_handler import HiggsTTSHandler
from s2s_business.fish_tts_handler import FishTTSHandler
from speech_to_speech.pipeline.messages import TTSInput


def item(backend="qwen3", mode=None):
    return SimpleNamespace(
        response=None,
        runtime_config=SimpleNamespace(session=SimpleNamespace(model_extra={
            "tts_backend": backend,
            "manual_voice_mode": mode or {},
        })),
    )


def test_backend_isolation_and_defaults():
    source = item("fish", {"emotion": "angry"})
    assert selection(source, "qwen3") == {}
    assert selection(source, "higgs") == {}
    assert selection(source, "fish") == {"emotion": "angry"}
    assert qwen_instruct({}) is None
    assert higgs_prefix({}) == ""
    assert fish_text("你好", {}) == "你好"


def test_realtime_session_update_carries_manual_selection():
    event = SessionUpdateEvent.model_validate({
        "type": "session.update",
        "session": {"type": "realtime", "tts_backend": "higgs", "manual_voice_mode": {"emotion": "elation"}},
    })
    config = RuntimeConfig()
    config.apply_session_update(event.session)
    source = SimpleNamespace(response=None, runtime_config=config)
    assert selection(source, "higgs") == {"emotion": "elation"}
    assert selection(source, "fish") == {}


def test_qwen_and_higgs_real_interfaces():
    assert "严厉" in qwen_instruct({"emotion": "strict", "custom": "严厉、高冷、克制"})
    assert higgs_prefix({
        "emotion": "elation", "style": "whispering", "effect": "sigh",
        "speed": "slow", "pitch": "low", "expressiveness": "strong",
    }) == (
        "<|emotion:elation|><|style:whispering|><|sfx:sigh|>"
        "<|prosody:speed_slow|><|prosody:pitch_low|>"
        "<|prosody:expressive_high|>"
    )


def test_higgs_mapped_tags_exist_in_installed_tokenizer():
    tokenizer = Path(str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'runtime/higgs/source/desktop/src-tauri/resources/higgs-assets/higgs-audio-v3-tts-4b/tokenizer_config.json'))
    tokens = set(json.loads(tokenizer.read_text(encoding="utf-8"))["extra_special_tokens"])
    modes = [{"emotion": value} for value in HIGGS_EMOTIONS]
    for field, values in (
        ("style", HIGGS_STYLES), ("effect", HIGGS_EFFECTS),
        ("speed", HIGGS_SPEED), ("pitch", HIGGS_PITCH),
        ("expressiveness", HIGGS_EXPRESSIVENESS),
    ):
        modes.extend({field: value} for value in values)
    for mode in modes:
        generated = re.findall(r"<\|[^|]+\|>", higgs_prefix(mode))
        assert generated and set(generated).issubset(tokens), mode


def test_fish_text_and_custom():
    assert fish_text("你好", {"emotion": "angry", "custom": "生气但不要喊叫"}) == (
        "[angry] [生气但不要喊叫] 你好"
    )
    assert fish_text("你好", {"emotion": "gentle"}) == "[soft tone] 你好"


def test_long_qwen_text_is_fully_segmented():
    text = "既然你想听长一点的，" + "今天的故事会慢慢讲给你听，" * 30 + "结尾也必须读出来。"
    parts = split_voice_text(text)
    assert len(parts) >= 5
    assert all(part.endswith(("，", "。")) for part in parts)
    assert "".join(parts) == text
    assert split_voice_text("短句") == ["短句"]


def test_every_synthesis_segment_has_a_terminal_boundary():
    text = "你不能这样，我们不合适。"
    assert split_voice_text(text, 5) == ["你不能这样，", "我们不合适。"]
    assert pronounceable_voice_segments(text, 5) == ["你不能这样，", "我们不合适。"]
    text = "这是一段完全没有标点的中文叙述" * 12 + "最后四个字"
    raw = split_voice_text(text, 40)
    spoken = pronounceable_voice_segments(text, 40)
    assert len(spoken) == len(raw)
    assert raw == [text]  # no defensible boundary: never invent a fixed-width cut
    assert spoken == [text + "。"]
    assert pronounceable_voice_segments("我想帮你，但是现在不合适", 8) == ["我想帮你，", "但是现在不合适。"]
    assert pronounceable_voice_segments("我们先认真考虑但是不要着急答应", 8)[0].endswith("，")
    assert pronounceable_voice_segments("已经结束。") == ["已经结束。"]
    assert pronounceable_voice_segments("请继续，") == ["请继续。"]


def test_higgs_same_seed_and_fish_one_request():
    text = ("温暖的阳光照进小屋，" * 22) + "蓝色风铃已经响起。"
    higgs = object.__new__(HiggsTTSHandler)
    higgs._preset = lambda: "normal"
    higgs_calls = []
    higgs._generate_segment = lambda segment, prefix, preset, seed: (
        higgs_calls.append((segment, prefix, preset, seed)) or iter(())
    )
    list(higgs.process(TTSInput(text=text, turn_id="same-response", turn_revision=1)))
    assert len(higgs_calls) >= 2
    assert len({call[3] for call in higgs_calls}) == 1

    fish = object.__new__(FishTTSHandler)
    fish_calls = []
    fish._generate_segment = lambda spoken, segmented=False, token_cap=400: (
        fish_calls.append((spoken, segmented, token_cap)) or iter(())
    )
    list(fish.process(TTSInput(text=text, turn_id="same-response", turn_revision=1)))
    assert len(fish_calls) == 1
    assert fish_calls[0][1] is True
    assert "\n" in fish_calls[0][0]
    assert fish_calls[0][2] > 400


if __name__ == "__main__":
    test_backend_isolation_and_defaults()
    test_realtime_session_update_carries_manual_selection()
    test_qwen_and_higgs_real_interfaces()
    test_higgs_mapped_tags_exist_in_installed_tokenizer()
    test_fish_text_and_custom()
    test_long_qwen_text_is_fully_segmented()
    test_every_synthesis_segment_has_a_terminal_boundary()
    test_higgs_same_seed_and_fish_one_request()
    print("manual voice unit checks: PASS")
