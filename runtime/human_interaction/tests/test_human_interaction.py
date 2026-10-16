# -*- coding: utf-8 -*-
"""真人互动层单元测试：全部是纯函数检查，不碰网络、不碰模型、不碰任何服务。

跑法（用项目自带的解释器，不需要任何新依赖）：

    runtime/core/venv/Scripts/python.exe runtime/human_interaction/tests/test_human_interaction.py

这份测试刻意包含三类「负控制」：没有重复时必须不报警、关掉时必须逐字节不变、
未知模式时必须不出话。只验「能出话」的测试等于没测。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.human_interaction import config, context as hi_context, engine, guard  # noqa: E402
from runtime.human_interaction import humanizer, modes  # noqa: E402


def use_settings(**over):
    """把配置换成测试想要的样子（不写盘、不碰生产那份文件）。"""
    merged = dict(config._SAFE_DEFAULTS)
    merged.update(over)
    return patch.object(config, "_snapshot", return_value=merged)


def on(mode="human_natural", **over):
    merged = {"feature_enabled": True, "interaction_mode": mode}
    merged.update(over)
    return use_settings(**merged)


def kwargs(**over):
    base = dict(character_id="t1", session_id="s1", persona_text="她说话挺直接。",
                temporary_text="", scene_text="", relationship_text="认识多年的朋友。",
                memory_lines=(), history=(), user_text="今天加班到十点才回来")
    base.update(over)
    return base


class TestOffPath(unittest.TestCase):
    """回滚硬验收：关着的时候，本层必须恒等于不存在。"""

    def test_disabled_returns_empty(self):
        with use_settings(feature_enabled=False):
            self.assertEqual(engine.interaction_instruction(**kwargs()), "")

    def test_attach_returns_input_byte_identical(self):
        base = "【你是谁】\n第一条\n第二条"
        with use_settings(feature_enabled=False):
            self.assertEqual(engine.attach(base, **kwargs()), base)

    def test_disabled_even_if_mode_is_autonomous(self):
        with use_settings(feature_enabled=False, interaction_mode="autonomous"):
            self.assertEqual(engine.interaction_instruction(**kwargs()), "")

    def test_env_var_beats_file(self):
        with use_settings(feature_enabled=True):
            with patch.dict("os.environ", {config.ENV_ENABLED: "false"}):
                self.assertFalse(config.enabled())
                self.assertEqual(engine.interaction_instruction(**kwargs()), "")

    def test_wiring_survives_module_vanishing(self):
        """整目录被删掉时，生产装配必须照旧跑通——这是「彻底移除」那档回滚的承重墙。

        接线写成一个自带 try/except 的小函数，光读代码不算证过：这里让
        `runtime.human_interaction` 的 import 直接抛错，再走一次生产接入口，
        看它是不是原样退回、一个字都不少。
        """
        import builtins

        from runtime.character import context_builder

        base = "【你是谁】\n第一条\n第二条"
        original = builtins.__import__

        def boom(name, *args, **kw):
            if str(name).startswith("runtime.human_interaction"):
                raise ImportError(f"simulated removal of {name}")
            return original(name, *args, **kw)

        builtins.__import__ = boom
        try:
            self.assertEqual(context_builder._attach_interaction(base, **kwargs()), base)
        finally:
            builtins.__import__ = original

    def test_explain_says_why_it_said_nothing(self):
        with use_settings(feature_enabled=False):
            row = engine.explain(**kwargs())
            self.assertIn("DISABLED_BY_FLAG", row["notes"])


class TestModeResolution(unittest.TestCase):
    def test_unknown_mode_is_treated_as_off(self):
        with use_settings(feature_enabled=True, interaction_mode="sassy"):
            self.assertEqual(config.mode(), config.MODE_OFF)

    def test_mode_off_emits_nothing(self):
        with on("off"):
            self.assertEqual(engine.interaction_instruction(**kwargs()), "")

    def test_story_director_blocked_without_explicit_choice(self):
        with on("story_director"):
            row = engine.explain(**kwargs(scene_text="刚才吵了一架", history=(
                ("user", "你别这样"), ("assistant", "我怎么了"))))
            # 显式选中才算被点过：这里配置里就是它，所以应当放行
            self.assertTrue(row["text"], row["notes"])
        with on("human_natural"):
            row = engine.explain(mode="story_director",
                                 **kwargs(scene_text="刚才吵了一架"))
            self.assertEqual(row["text"], "")
            self.assertIn("MODE_NOT_ALLOWED:MODE_NOT_EXPLICITLY_CHOSEN", row["notes"])

    def test_every_mode_has_a_non_empty_prompt_file(self):
        for key, row in modes.MODES.items():
            if key == config.MODE_OFF:
                continue
            body = modes.prompt_text(key)
            self.assertTrue(body, f"{key} 的文案是空的")
            self.assertTrue((modes.PROMPT_DIR / row.prompt_file).exists())

    def test_mode_differences_come_only_from_metadata(self):
        """同一组输入下，不同模式只可能差在文案，不该差在代码分支。"""
        seen = {}
        for key in (config.MODE_HUMAN_NATURAL, config.MODE_AUTONOMOUS):
            with on(key):
                seen[key] = engine.interaction_instruction(**kwargs())
        self.assertNotEqual(seen[config.MODE_HUMAN_NATURAL], seen[config.MODE_AUTONOMOUS])
        for key in seen:
            self.assertIn(modes.label(key), seen[key])


class TestSectionGating(unittest.TestCase):
    """[#turn] / [#chat] 这类声明：材料不在场，那一段就不该出现。"""

    def test_turn_section_absent_without_user_text(self):
        with on():
            row = engine.explain(**kwargs(user_text="", history=()))
            body = row["text"]
            self.assertNotIn("判断的顺序是", body)
            self.assertIn("SECTION_SKIPPED:turn", row["notes"])

    def test_chat_section_absent_without_history(self):
        with on():
            row = engine.explain(**kwargs(history=()))
            self.assertNotIn("她前面说过的话是她自己说过的", row["text"])

    def test_chat_section_present_with_history(self):
        with on():
            row = engine.explain(**kwargs(history=(("user", "还在忙"),
                                                   ("assistant", "忙完了，你呢"))))
            self.assertIn("她前面说过的话是她自己说过的", row["text"])

    def test_tag_applies_only_to_the_paragraph_right_after_it(self):
        """回归：曾经一个标签把它下面本来无条件的段落一起吞掉了。"""
        body = "[#turn]\n这一段要对方开口\n\n这一段无条件\n\n[#chat]\n这一段要聊过几句"
        with on(), patch.object(modes, "prompt_text", return_value=body):
            row = engine.explain(**kwargs(user_text="", history=()))
            self.assertNotIn("这一段要对方开口", row["text"])
            self.assertIn("这一段无条件", row["text"])
            self.assertNotIn("这一段要聊过几句", row["text"])
            # 标签段被跳过时，它下面那段不能跟着一起消失
            self.assertIn("SECTION_SKIPPED:turn", row["notes"])
            self.assertIn("SECTION_SKIPPED:chat", row["notes"])

    def test_unknown_tag_is_skipped_not_emitted(self):
        body = "[#nonsense]\n这一段不该出现\n\n这一段该出现"
        with on(), patch.object(modes, "prompt_text", return_value=body):
            row = engine.explain(**kwargs())
            self.assertNotIn("这一段不该出现", row["text"])
            self.assertIn("这一段该出现", row["text"])
            self.assertTrue(any(one.startswith("UNKNOWN_TAG") for one in row["notes"]))


class TestDeliveryChannel(unittest.TestCase):
    """回归：固定测试里量到过一次——她回出「（拿起那颗橘子）知道了。」。

    这段话最终是被 TTS 原样念出来的，括号里的舞台指示会被读成怪声。根因在提示：
    强角色自主那档明写了「她自己想做的动作，都可以在话里带出来」，而三份文案里
    没有任何一处说过「这话是要念的」。所以修在通道那一句上——它是配置里的一条字符串，
    代码里只有一处「配置给了就出，没给就不出」，没有按模式分支。
    """

    NOTE = "这一段话是要念出来的"

    def test_channel_note_reaches_every_mode(self):
        for mode_key in (config.MODE_HUMAN_NATURAL, config.MODE_AUTONOMOUS,
                         config.MODE_STORY_DIRECTOR):
            with on(mode_key, output_channel_note=self.NOTE,
                    story_director_requires_user_choice=False):
                body = engine.interaction_instruction(**kwargs())
                self.assertIn(self.NOTE, body, mode_key)

    def test_channel_note_comes_from_config_not_from_code(self):
        """配置里留空，这句就必须完全不出现——证明它不是写死在代码里的分支。"""
        with on(output_channel_note=""):
            body = engine.interaction_instruction(**kwargs())
            self.assertNotIn("要念出来", body)
            self.assertTrue(body)

    def test_off_path_emits_nothing_at_all(self):
        with use_settings(feature_enabled=False, output_channel_note=self.NOTE):
            self.assertEqual(engine.interaction_instruction(**kwargs()), "")

    def test_shipped_config_actually_carries_the_note(self):
        """配置文件里那句真的在（不是只有测试假数据在跑）。"""
        settings = config._read_file()
        note = str(settings.get("output_channel_note") or "")
        self.assertTrue(note, "生产配置里通道那句是空的")
        for token in ("念", "括号", "动作"):
            self.assertIn(token, note)


class TestDetection(unittest.TestCase):
    """检测层：正控制要报得出来，负控制必须不报。"""

    def test_repeated_phrase_positive(self):
        turns = ["真的挺想的", "真的累了", "真的没事", "真的不用"]
        with use_settings(**{"signals": {"repeat_min_count": 3, "repeat_min_turns": 2,
                                         "repeat_gram_min": 2, "repeat_gram_max": 4}}):
            found = humanizer.repeated_phrases(turns)
        self.assertTrue(any(one["phrase"].startswith("真的") for one in found), found)

    def test_repeated_phrase_negative_control(self):
        turns = ["外面下雨了", "我把衣服收进来了", "你带伞了吗", "晚上想吃面"]
        with use_settings(**{"signals": {"repeat_min_count": 3, "repeat_min_turns": 2,
                                         "repeat_gram_min": 2, "repeat_gram_max": 4}}):
            self.assertEqual(humanizer.repeated_phrases(turns), [])

    def test_echo_only_when_long_enough(self):
        user = "我今天被领导叫去骂了一顿很难受"
        with use_settings(**{"signals": {"echo_min_chars": 6}}):
            hit = humanizer.echoed_user(user, "你今天被领导叫去骂了一顿，肯定很难受吧")
            self.assertTrue(hit)
            miss = humanizer.echoed_user(user, "那后来呢")
            self.assertEqual(miss, [])

    def test_length_monotony_positive_and_negative(self):
        flat = ["那就这样吧我先去把东西收拾一下", "行啊那就这样吧我去把东西收拾好",
                "嗯嗯那就这样吧我先去把东西收了", "好的那就这样吧我先去收拾东西了"]
        spread = ["那就这样吧我先去把东西收拾一下",
                  "行啊，那你先把材料放在我桌上，我明天一早过去拿，不用专门等我",
                  "嗯嗯",
                  "对了，你上次说那家店搬去地铁那边了，周末要是没事我们就一块过去看看"]
        with use_settings(**{"signals": {"length_monotony_min_turns": 4,
                                         "length_monotony_tolerance_chars": 12,
                                         "length_monotony_min_len": 12}}):
            self.assertTrue(humanizer.length_monotony(flat))
            self.assertEqual(humanizer.length_monotony(spread), [])

    def test_cliche_only_when_actually_said(self):
        with use_settings(**{"cliche_templates": ["我理解你的感受"],
                             "signals": {"cliche_min_count": 1}}):
            self.assertTrue(humanizer.cliche_overuse(["我理解你的感受，你也不容易"]))
            self.assertEqual(humanizer.cliche_overuse(["那家店关门了"]), [])

    def test_detect_outputs_are_positive_goals_not_bans(self):
        turns = ["真的挺开心", "真的有点困", "真的没事", "真的就这样吧总之"]
        inp = hi_context.build(mode="human_natural", history=[("assistant", t) for t in turns],
                               user_text="真的假的")
        with use_settings(feature_enabled=True, **{
                "signals": {"repeat_min_count": 3, "repeat_min_count_short": 4,
                            "repeat_min_turns": 2, "repeat_gram_min": 2,
                            "repeat_gram_max": 4},
                "summary_tone_openers": ["总之"]}):
            evidence, lines = humanizer.detect(inp)
        self.assertTrue(evidence)
        self.assertTrue(lines)
        for one in lines:
            self.assertNotIn("禁止", one)
            self.assertNotIn("不许", one)

    def test_longest_shared(self):
        self.assertEqual(humanizer.longest_shared("今天加班到十点", "加班到十点真累"),
                         "加班到十点")

    def test_determinism_same_input_same_output(self):
        inp = kwargs(history=[("user", "你还在忙"), ("assistant", "忙完了，你呢"),
                              ("user", "真的假的"), ("assistant", "真的有点累")] * 2)
        with on():
            first = engine.interaction_instruction(**inp)
            second = engine.interaction_instruction(**inp)
        self.assertEqual(first, second)


class TestGuard(unittest.TestCase):
    def test_only_five_rules_exist(self):
        self.assertEqual(set(guard.RULES), {"REPEAT", "RESTATE", "STENCIL",
                                            "MISMATCH", "MODE_CLASH"})

    def test_mode_clash_is_exactly_five_rules_plus_structure(self):
        inp = hi_context.build(mode="story_director", user_text="走不走")
        with on("human_natural"):
            row = guard.check(inp, [])
            self.assertTrue(any(one["rule"] == "MODE_CLASH" for one in row), row)

    def test_no_evidence_no_anomaly(self):
        inp = hi_context.build(mode="human_natural", user_text="嗯",
                               scene_text="刚吵完", history=(("user", "你什么意思"),))
        with on():
            self.assertEqual(guard.check(inp, []), [])

    def test_should_revise_respects_severity(self):
        weak = [{"rule": "REPEAT", "severity": 1}]
        strong = [{"rule": "REPEAT", "severity": 9}]
        with on():
            self.assertFalse(guard.should_revise(weak))
            self.assertTrue(guard.should_revise(strong))

    def test_revision_pass_is_off_by_default(self):
        with on():
            self.assertFalse(config.revise_enabled())
            row = engine.revision_directive(**kwargs())
            self.assertFalse(row["needed"])
            self.assertEqual(row["reason"], "REVISE_FLAG_OFF")


class TestSafetyInvariants(unittest.TestCase):
    """任务 §11 / §13 落成的静态检查。"""

    SOURCES = ["engine.py", "config.py", "context.py", "modes.py", "guard.py",
               "humanizer.py", "__init__.py"]
    SRC_DIR = Path(__file__).resolve().parents[1]

    def _all_source(self):
        return "\n".join((self.SRC_DIR / name).read_text(encoding="utf-8")
                         for name in self.SOURCES)

    def test_no_model_name_or_service_port_hardcoded(self):
        body = self._all_source().lower()
        for token in ("gemma", "dark roast", "dark_roast", "llama", "qwen", "fish",
                      "higgs", "8090", "8765", "8010", "7860"):
            self.assertNotIn(token, body, f"主逻辑里出现了 {token}")

    def test_no_network_or_media_client_import(self):
        body = self._all_source()
        for token in ("requests", "urllib", "socket", "http", "websocket", "openai",
                      "subprocess", "sqlite", "json.dump"):
            self.assertNotIn(token, body, f"本层不该有 {token}")

    def test_no_writes_to_disk_from_engine_path(self):
        body = self._all_source()
        for token in ("write_text", "open(", "mkdir", "shutil", "os.replace"):
            self.assertNotIn(token, body, f"本层不该往盘上写 {token}")

    def test_output_carries_no_internal_words(self):
        from runtime.character import rules
        for mode_key in (config.MODE_HUMAN_NATURAL, config.MODE_AUTONOMOUS):
            with on(mode_key):
                row = engine.explain(**kwargs(
                    history=[("user", "真的"), ("assistant", "真的没事"),
                             ("user", "嗯"), ("assistant", "嗯")],
                    memory_lines=("你之前说过她妈妈打电话来")))
                self.assertTrue(row["text"])
                self.assertEqual(rules.find_banned(row["text"]), [], mode_key)

    def test_hygiene_gate_strips_leaked_words(self):
        from runtime.character import rules
        with on():
            kept, notes = engine._hygiene("这句是干净的\n这句里有 prompt 这个词")
            self.assertNotIn("prompt", kept)
            self.assertTrue(rules.find_banned("prompt") )
            self.assertTrue(any(one.startswith("HYGIENE_STRIPPED") for one in notes))

    def test_output_respects_declared_max(self):
        with on("human_natural", instruction_max_chars=120):
            body = engine.interaction_instruction(**kwargs())
            self.assertLessEqual(len(body), 120)

    def test_broken_prompt_file_yields_nothing(self):
        with on(), patch.object(modes, "prompt_text", return_value=""):
            row = engine.explain(**kwargs())
            self.assertEqual(row["text"], "")
            self.assertIn("MODE_PROMPT_EMPTY", row["notes"])

    def test_attach_survives_internal_crash(self):
        base = "原来的那块话"
        with on(), patch.object(engine, "interaction_instruction",
                                side_effect=RuntimeError("炸了")):
            self.assertEqual(engine.attach(base, **kwargs()), base)

    def test_context_never_mutates_inputs(self):
        history = [{"role": "user", "text": "在吗"}, {"role": "assistant", "text": "在"}]
        snapshot_before = [dict(one) for one in history]
        hi_context.build(mode="human_natural", history=history, user_text="在吗")
        self.assertEqual(history, snapshot_before)


class TestInputNormalisation(unittest.TestCase):
    def test_accepts_both_role_spellings_and_plain_strings(self):
        inp = hi_context.build(mode="human_natural", history=[
            {"谁": "girlfriend", "text": "我在这儿"}, ("user", "嗯"), "孤零零一行"])
        self.assertEqual(inp.assistant_turns, ("我在这儿",))
        self.assertEqual(len(inp.user_turns), 2)

    def test_memory_lines_accepts_dicts_and_strings(self):
        inp = hi_context.build(memory_lines=[{"text": "你之前说过加班"}, "裸字符串"])
        self.assertEqual(inp.memory_lines, ("你之前说过加班", "裸字符串"))

    def test_snapshot_has_no_user_verbatim_text(self):
        inp = hi_context.build(user_text="这是一句不该被抄进报告的私房话")
        self.assertNotIn("私房话", repr(inp.snapshot()))

    def test_is_first_turn_and_has_conversation(self):
        empty = hi_context.build()
        self.assertTrue(empty.is_first_turn)
        self.assertFalse(empty.has_conversation)


def row(content: str, **over):
    """假装是 llm_client 的一条回执。"""
    out = {"ok": True, "content": content, "reasoning_chars": 0, "finish": "stop",
           "usage": {}, "timings": {}, "model": "fake", "wall_s": 0.1,
           "url": "http://127.0.0.1:0/v1/chat/completions"}
    out.update(over)
    return out


JUDGE_SIX = ("关系：认识多年｜意图：被接住｜态度：不累但心疼｜"
             "接法：先顺着问｜节奏：短｜推进：否")
# V2 那一份老形状（一行一个键、值写成小句子）。V2b 压成一行之后，这一形**仍须解析得动**：
# 线上模型未必每次都听话给一行，两形走同一条路径才不留两张皮。
JUDGE_SIX_MULTILINE = ("关系：认识多年\n意图：被接住\n态度：不累但心疼\n"
                       "接法：先顺着问\n节奏：短\n推进：否")


class TestTurnJudgeKeys(unittest.TestCase):
    """字段名来自文案，不来自代码——这条必须可检查，否则两张皮迟早分开。"""

    def test_real_brief_declares_the_six_keys_in_order(self):
        from runtime.human_interaction import turn_judge
        keys = turn_judge.declared_keys(modes.judge_prompt_text(config.MODE_HUMAN_NATURAL))
        self.assertEqual(keys, ("关系", "意图", "态度", "接法", "节奏", "推进"))

    def test_six_meanings_survived_the_renaming(self):
        """V2b 只许压排版，不许换判断口径：六个键各自还带着原来那层意思。"""
        brief = modes.judge_prompt_text(config.MODE_HUMAN_NATURAL)
        for pair in (("关系", "你们是什么关系"), ("意图", "想干什么"),
                     ("态度", "什么态度"), ("接法", "怎么接"),
                     ("节奏", "说多长"), ("推进", "推进")):
            self.assertIn(pair[0], brief, brief)
            self.assertIn(pair[1], brief, "键还在，但它对应的那件事被删了")

    def test_brief_exists_only_for_the_mode_that_earned_it(self):
        self.assertEqual(modes.judge_prompt_text(config.MODE_AUTONOMOUS), "")
        self.assertEqual(modes.judge_prompt_text(config.MODE_STORY_DIRECTOR), "")
        self.assertEqual(modes.spec(config.MODE_AUTONOMOUS).judge_prompt_file, "")

    def test_keys_ignore_prose_and_bullets(self):
        from runtime.human_interaction import turn_judge
        keys = turn_judge.declared_keys("说明一行。\n- 状态：\n意图：\n这一句不是键值")
        self.assertEqual(keys, ("状态", "意图"))

    def test_single_character_line_is_not_a_field(self):
        """键至少两个字：省得文案里一句「嗯：」被当成要填的字段。"""
        from runtime.human_interaction import turn_judge
        self.assertEqual(turn_judge.declared_keys("甲：\n状态："), ("状态",))


class TestJudgeRequestShape(unittest.TestCase):
    """判断那一发的形状：关掉思考、给输出封顶，但不碰采样、不猜端口。

    这几个断言是有理由的：「不带思考」是实测把 8.47 秒压到 0.9 秒的那一刀，
    写错一个键名就等于回到八秒；「不碰采样」是因为采样口径属于 8090 的 argv，
    本层一旦带 temperature/top_p，实时链和测试台就不是同一个模型行为的人了。
    """

    SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p", "seed",
                     "repeat_penalty", "repetition_penalty", "dynatemp")

    def test_body_carries_the_reasoning_switch_and_a_cap(self):
        from runtime.human_interaction import llm_client
        body = llm_client.request_body("简报", "材料", max_tokens=200)
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual(body["max_tokens"], 200)
        self.assertFalse(body["stream"])
        self.assertEqual([one["role"] for one in body["messages"]], ["system", "user"])

    def test_body_never_carries_a_sampling_parameter(self):
        from runtime.human_interaction import llm_client
        for kwargs in ({}, {"max_tokens": 200}, {"disable_thinking": False}):
            body = llm_client.request_body("简报", "材料", **kwargs)
            self.assertEqual(sorted(set(body) & set(self.SAMPLING_KEYS)), [], body)

    def test_zero_cap_is_honoured_instead_of_becoming_two_hundred(self):
        from runtime.human_interaction import llm_client
        self.assertNotIn("max_tokens", llm_client.request_body("简报", "材料", max_tokens=0))

    def test_unreadable_registry_fails_without_touching_a_socket(self):
        from runtime.human_interaction import llm_client
        with patch.object(llm_client, "REGISTRY_PATH",
                          ROOT / "runtime" / "human_interaction" / "tests" / "没有这个文件.json"):
            out = llm_client.complete_short("简报", "材料", timeout_s=1)
        self.assertFalse(out["ok"])
        self.assertTrue(out["error"].startswith(("NO_PORT_IN_REGISTRY",
                                                 "REGISTRY_UNREADABLE")), out)
        self.assertEqual(out["wall_s"], 0.0)


class TestTurnJudge(unittest.TestCase):
    """本轮互动判断：闸、降级、解析。全程不发真请求（client 是假的）。"""

    def judge(self, content=JUDGE_SIX, **over):
        from runtime.human_interaction import turn_judge

        def fake(system_text, user_text, *, timeout_s, max_tokens):
            fake.seen = (system_text, user_text, timeout_s, max_tokens)
            return row(content) if not isinstance(content, dict) else content

        kw = dict(persona_text="她说话挺直接。", relationship_text="认识多年的朋友。",
                  history=({"role": "user", "text": "在忙吗"},), user_text="出来走走？")
        kw.update(over)
        return turn_judge.judge(client=fake, **kw), fake

    def test_master_flag_off_means_no_request_at_all(self):
        with use_settings(feature_enabled=False, turn_judge_enabled=True):
            out, fake = self.judge()
        self.assertEqual(out["reason"], "DISABLED_BY_FLAG")
        self.assertFalse(out["requested"])
        self.assertFalse(hasattr(fake, "seen"))

    def test_judge_flag_off_is_byte_equal_to_v1_nothing_emitted(self):
        with use_settings(feature_enabled=True, turn_judge_enabled=False):
            out, fake = self.judge()
        self.assertEqual(out["reason"], "DISABLED_BY_JUDGE_FLAG")
        self.assertFalse(out["requested"])
        self.assertEqual(out["text"], "")

    def test_env_var_beats_file_for_the_judge_gate(self):
        with use_settings(feature_enabled=True, turn_judge_enabled=True):
            self.assertTrue(config.turn_judge_enabled())
            with patch.dict("os.environ", {config.ENV_TURN_JUDGE: "false"}):
                self.assertFalse(config.turn_judge_enabled())
                out, _fake = self.judge()
            self.assertEqual(out["reason"], "DISABLED_BY_JUDGE_FLAG")

    def test_happy_path_renders_one_line_and_asks_once(self):
        with on("human_natural", turn_judge_enabled=True):
            out, fake = self.judge()
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["requested"])
        self.assertEqual(len(out["text"].splitlines()), 1)      # V2b：整块就一行
        self.assertTrue(out["text"].startswith("【本轮互动判断】"))
        self.assertLessEqual(len(out["text"]),
                             int(config.judge_int("turn_judge_block_max_chars", 120)))
        self.assertEqual(out["fields"]["接法"], "先顺着问")
        # 简报 + 材料都递到了，且材料里有对方那句原话
        brief, body, timeout_s, max_tokens = fake.seen
        self.assertIn("六个字段用", brief)
        self.assertIn("出来走走？", body)
        self.assertIn("在忙吗", body)
        self.assertEqual(timeout_s, float(config.judge_int("turn_judge_timeout_s", 5)))
        self.assertEqual(max_tokens, int(config.judge_int("turn_judge_max_tokens", 200)))

    def test_one_line_shape_and_multi_line_shape_render_the_same(self):
        """压形状不许把解析口径压窄：模型给一行、或者给惯常的六行，结果必须一字不差。"""
        with on("human_natural", turn_judge_enabled=True):
            one, _f1 = self.judge(JUDGE_SIX)
            many, _f2 = self.judge(JUDGE_SIX_MULTILINE)
        self.assertEqual(one["fields"], many["fields"], (one["fields"], many["fields"]))
        self.assertEqual(one["text"], many["text"])
        self.assertTrue(one["ok"] and many["ok"])

    def test_mixed_separators_are_all_accepted(self):
        """分隔符不稳：全角竖线、半角竖线、分号混着给，也要六个键全落地。"""
        with on("human_natural", turn_judge_enabled=True):
            out, _fake = self.judge("关系：认识多年; 意图：被接住｜态度：不累但心疼 | "
                                    "接法：先顺着问；节奏：短｜推进：否")
        self.assertEqual(sorted(out["fields"].values()),
                         sorted(["认识多年", "被接住", "不累但心疼", "先顺着问", "短", "否"]),
                         out["fields"])

    def test_slim_block_is_shorter_than_the_v2_prose_block(self):
        """这一轮唯一的目标就是短。老形状那一份中位数是 90 字，压完不许反弹。"""
        with on("human_natural", turn_judge_enabled=True):
            out, _fake = self.judge()
        self.assertLess(len(out["text"]), 70, out["text"])

    def test_no_user_text_means_no_judgement_and_no_request(self):
        with on("human_natural", turn_judge_enabled=True):
            out, fake = self.judge(user_text="   ")
        self.assertEqual(out["reason"], "NO_USER_TEXT_TO_JUDGE")
        self.assertFalse(hasattr(fake, "seen"))

    def test_mode_without_a_brief_does_not_call_the_model(self):
        """只有「真人自然」挣到了判断器。别档没配简报 = 一个字都不发。"""
        with on("human_natural", turn_judge_enabled=True):
            out, fake = self.judge(mode="autonomous")
        self.assertEqual(out["reason"], "NO_BRIEF_FOR_MODE")
        self.assertFalse(out["requested"])
        self.assertFalse(hasattr(fake, "seen"))

    def test_unknown_mode_is_refused_before_any_request(self):
        with on("human_natural", turn_judge_enabled=True):
            out, _fake = self.judge(mode="sassy")
        self.assertTrue(out["reason"].startswith("MODE_NOT_ALLOWED"))
        self.assertFalse(out["requested"])

    def test_partial_parse_below_half_is_not_used(self):
        with on("human_natural", turn_judge_enabled=True):
            out, _fake = self.judge("关系：熟\n意图：闲扯")
        self.assertFalse(out["ok"])
        self.assertTrue(out["reason"].startswith("PARSE_FAILED"))
        self.assertEqual(out["text"], "")

    def test_four_of_six_is_enough_and_missing_ones_are_noted(self):
        with on("human_natural", turn_judge_enabled=True):
            out, _fake = self.judge("关系：熟\n接法：接梗\n节奏：短\n推进：否")
        self.assertTrue(out["ok"], out)
        self.assertIn("MISSING:意图、态度", out["notes"][0])
        self.assertNotIn("意图", out["text"])

    def test_truncated_reply_is_marked_but_still_used_when_enough_landed(self):
        """被封顶切断要说出来：不然六字段只回来四个，看不出是模型没想说还是切早了。"""
        with on("human_natural", turn_judge_enabled=True):
            out, _fake = self.judge(row("关系：熟\n接法：接梗\n节奏：短\n推进：否",
                                        finish="length"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["finish"], "length")
        self.assertIn("TRUNCATED_BY_MAX_TOKENS", out["notes"])

    def test_overlong_value_is_capped_and_marked(self):
        long = "关系：" + "很" * 40 + "｜" + JUDGE_SIX.split("关系：认识多年｜")[1]
        with use_settings(feature_enabled=True, turn_judge_enabled=True,
                          turn_judge_value_max_chars=12):
            out, _fake = self.judge(long)
        self.assertEqual(len(out["fields"]["关系"]), 12)
        self.assertTrue(any(one.startswith("OVERLONG") for one in out["notes"]), out["notes"])

    def test_default_value_cap_is_the_eight_char_one(self):
        """封顶值从 24 收到 8 是这一轮的口径，别让它悄悄回到"值可以写成一句话"。"""
        self.assertEqual(int(config.judge_int("turn_judge_value_max_chars", 8)), 8)
        self.assertEqual(int(config.judge_int("turn_judge_block_max_chars", 120)), 120)

    def test_client_failure_degrades_to_no_judgement(self):
        with on("human_natural", turn_judge_enabled=True):
            out, _fake = self.judge({"ok": False, "error": "TimeoutError: timed out",
                                     "wall_s": 8.0})
        self.assertFalse(out["ok"])
        self.assertTrue(out["reason"].startswith("CLIENT_FAILED"))
        self.assertEqual(out["text"], "")
        self.assertTrue(out["requested"])

    def test_garbage_reply_is_not_used_but_never_raises(self):
        with on("human_natural", turn_judge_enabled=True):
            out, _fake = self.judge("我真的累了，你别这样说")
        self.assertFalse(out["ok"])
        self.assertEqual(out["text"], "")

    def test_history_is_cropped_to_configured_rows(self):
        from runtime.human_interaction import turn_judge
        rows = [{"role": "user", "text": f"第{index}句"} for index in range(1, 13)]
        body = turn_judge.materials(hi_context.build(history=rows, user_text="最后一句"),
                                    history_turns=4, memory_lines=0, persona_max_chars=700)
        self.assertIn("第12句", body)
        self.assertNotIn("第8句", body)
        self.assertEqual(body.count("对方："), 4)

    def test_materials_do_not_leak_memory_when_switched_off(self):
        from runtime.human_interaction import turn_judge
        body = turn_judge.materials(hi_context.build(memory_lines=["她说过加班"],
                                                     user_text="嗯"),
                                    history_turns=8, memory_lines=0, persona_max_chars=700)
        self.assertNotIn("她说过加班", body)


class TestJudgementReachesAssembly(unittest.TestCase):
    """判断结果要能进那一块话，而且不能把 V1 的形状改掉。"""

    def test_none_judgement_is_byte_identical_to_v1(self):
        with on("human_natural"):
            plain = engine.interaction_instruction(**kwargs())
            explicit = engine.interaction_instruction(**kwargs(), turn_judgment=None)
        self.assertEqual(plain, explicit)

    def test_judgement_lands_right_after_the_header(self):
        block = "【本轮互动判断】接法：接梗｜节奏：短"     # V2b 的真实形状：标题后一行到底
        with on("human_natural"):
            text = engine.interaction_instruction(**kwargs(), turn_judgment={"text": block})
        head = "【这一轮怎么接（真人自然）】\n\n"
        self.assertTrue(text.startswith(head + block), text[:120])

    def test_absent_judgement_says_why_in_notes(self):
        with on("human_natural"):
            row = engine.explain(**kwargs(), turn_judgment={"ok": False,
                                                            "reason": "CLIENT_FAILED:x",
                                                            "text": ""})
        self.assertIn("ABSENT:CLIENT_FAILED:x", row["notes"])
        self.assertFalse(row["judgment"]["used"])

    def test_judgement_goes_through_the_same_hygiene_gate(self):
        """判断里混进内部名词时，出口那一关必须照旧把它剔掉，不能因为它新就放过。"""
        banned = "数据库"          # rules.BANNED_UI_WORDS 里那一个，用来当探针
        block = "【本轮互动判断】接法：接梗｜态度：想提" + banned + "一下"
        with on("human_natural"):
            text = engine.interaction_instruction(**kwargs(), turn_judgment={"text": block})
        self.assertNotIn(banned, text)
        self.assertIn("接法：接梗", text)

    def test_hygiene_drops_the_dirty_field_not_the_whole_one_line_block(self):
        """一行形状带来的真实副作用，写成闸：露馅词只能带走它那一格。

        V2 那一份是一行一个字段，"按行剔"恰好等于"按格剔"。V2b 压成一行之后，
        如果还按行剔，一个漏词会把六个判断一起带走（等于这轮白判断）。
        负控制同条内：整行都是脏话（没有分隔符可拆）时，照旧整行不许留。
        """
        banned = "数据库"
        block = ("【本轮互动判断】关系：认识多年｜意图：想查" + banned +
                 "｜态度：接梗｜接法：顺着问｜节奏：短｜推进：否")
        with on("human_natural"):
            text = engine.interaction_instruction(**kwargs(), turn_judgment={"text": block})
            notes = " ".join(engine.explain(**kwargs(), turn_judgment={"text": block})["notes"])
        self.assertNotIn(banned, text)
        for keep in ("关系：认识多年", "态度：接梗", "推进：否"):
            self.assertIn(keep, text, text)
        self.assertIn("HYGIENE_STRIPPED", notes)
        # 负控制：这一行拆不出干净格子，整行必须消失
        all_dirty = "【本轮互动判断】意图：" + banned + "词｜态度：" + banned + "库"
        with on("human_natural"):
            dirty = engine.interaction_instruction(**kwargs(),
                                                   turn_judgment={"text": all_dirty})
        self.assertNotIn(banned, dirty)
        self.assertNotIn("【本轮互动判断】", dirty)

    def test_assembly_path_itself_never_opens_the_socket(self):
        """承重墙：装配（context_builder → engine）里不许出现模型请求。

        这一条不是为了好看：连线时那次 GET /api/character/prompt 走的就是装配，
        一旦那里能发请求，实时链就会在没人预料的地方多等一次模型。
        """
        for module in (engine, modes, hi_context, guard, humanizer):
            body = Path(module.__file__).read_text(encoding="utf-8")
            self.assertNotIn("llm_client", body, module.__name__)
            self.assertNotIn("complete_short", body, module.__name__)
            self.assertNotIn("urlopen", body, module.__name__)
        turn_judge_body = (Path(engine.__file__).resolve().parent / "turn_judge.py"
                           ).read_text(encoding="utf-8")
        self.assertIn("llm_client", turn_judge_body)


class TestNoPatchPile(unittest.TestCase):
    """反补丁堆积的可检查形式：主逻辑里不许出现按话/按人/按模型的分支。"""

    def test_no_per_character_or_per_model_branch_in_engine(self):
        body = (engine.__file__ and Path(engine.__file__).read_text(encoding="utf-8"))
        self.assertIsNone(re.search(r"(gemma|Gemma|dark|ROAST|character_id\s*==)", body))
        self.assertIsNone(re.search(r"if\s+.*user_text\s*(==|in\s)", body))

    def test_engine_has_no_regular_expression_matching_user_words(self):
        """反补丁堆积的真实口径：engine 里的式子只许认**排版结构**，不许认**话**。

        原来这条是数 `re.compile` 的个数（写死 1）。个数会因正当理由变（V2b 为了
        把判断块按字段剔脏加了第二个，认的是分隔符不是话），所以改成按本意查：
        每个式子的模式体里不许出现汉字——按中文话头写的正则，才是"第三个第四个规则"。
        外加一条正控制，防止这条检查自己空转。
        """
        body = Path(engine.__file__).read_text(encoding="utf-8")
        pats = re.findall(r"^[A-Z_]+ = re\.compile\((.*)\)\s*$", body, re.M)
        self.assertTrue(pats, "engine 里一个结构式都没扫到 = 这条检查在空转")
        for one in pats:
            self.assertIsNone(re.search(r"[\u4e00-\u9fff]", one),
                              "engine 里出现了按中文话头匹配的式子：" + one)
        self.assertEqual(len(pats), 2, pats)          # 现状：文案标签式 + 字段分隔式
        # 正控制：真按话头写的式子必须被上面那段抓到
        self.assertIsNotNone(re.search(r"[\u4e00-\u9fff]", r'^[#].*(?:在忙吗|吃了吗)$'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
