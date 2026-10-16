# -*- coding: utf-8 -*-
"""Temporary, per-Chat role state. No database, model call, or TTS side effect."""

import logging
import re

logger = logging.getLogger(__name__)

_VALUES = ("affection", "flirt", "intimacy", "initiative", "assertiveness",
           "playfulness", "shyness", "emotionality")


def default_state():
    state = {
        "active": False, "role": "", "role_directive": "", "relationship_mode": "",
        "affection": 50, "flirt": 0, "intimacy": 0, "initiative": 50,
        "assertiveness": 50, "playfulness": 50, "shyness": 50,
        "emotionality": 50, "temporary": True, "minor_role": False,
        "voice_style": {"emotion": "", "intensity": 0.0},
    }
    return state


_CLEAR = re.compile(r"恢复正常|退出角色|别演了|回到原来的你")
_ENTER = (
    re.compile(r"(?:现在)?换成\s*(.+)"),
    re.compile(r"(?:现在|从现在开始)?你(?:现在)?是\s*(.+)"),
    re.compile(r"(?:你现在|现在你|从现在开始|现在)?扮演\s*(.+)"),
    re.compile(r"(?:从现在开始|现在)?当(?:一个|个)?\s*(.+)"),
    re.compile(r"进入\s*(.+?)模式"),
)
_SWITCH_UNSPECIFIED = re.compile(r"换一个身份|换个身份|换一个角色|换个角色")
_ROLE_NOUN = re.compile(r"女上司|护士|秘书|老师|御姐|陌生女生|女生|女人|上司|医生|同事|朋友")
_MINOR = re.compile(r"未成年|未满(?:八|8)岁?|(?<![0-8])(?:[0-8]|[0-7])岁")
# One small table for all explicit style deltas. Negative patterns precede positives.
_STYLE_RULES = (
    (re.compile(r"别(?:这么|那么|再)?强势|不要(?:这么|那么|再)?强势"), {"assertiveness": -15}),
    (re.compile(r"别害羞|不要害羞"), {"shyness": -20, "initiative": 10}),
    (re.compile(r"主动一点|更主动些?"), {"initiative": 15}),
    (re.compile(r"大胆一点|更大胆些?"), {"initiative": 15, "assertiveness": 10, "intimacy": 10}),
    (re.compile(r"温柔一点|更温柔些?"), {"affection": 15, "assertiveness": -10}),
    (re.compile(r"暧昧一点|更暧昧些?"), {"flirt": 15, "intimacy": 10}),
    (re.compile(r"克制一点|更克制些?"), {"flirt": -15, "intimacy": -15}),
    (re.compile(r"害羞一点|更害羞些?"), {"shyness": 15, "assertiveness": -10}),
    (re.compile(r"活泼一点|更活泼些?"), {"playfulness": 15}),
    (re.compile(r"认真一点|更认真些?"), {"playfulness": -15}),
)
_ENTER_TRAITS = (
    ("强势", {"assertiveness": 20}),
    ("害羞", {"shyness": 15}),
    ("温柔", {"affection": 15}),
)


def _clamp(value):
    return max(0, min(100, int(value)))


def _change(state, deltas):
    for key, delta in deltas.items():
        state[key] = _clamp(state[key] + delta)


def _descriptor(text):
    for pattern in _ENTER:
        match = pattern.search(text)
        if match:
            value = match.group(1).strip(" 。！!？?，,：:")
            if re.match(r"什么|谁|哪|如何|怎么|是否", value):
                return None
            return value[:240] if value else None
    if _SWITCH_UNSPECIFIED.search(text):
        return "换一个不同的身份"
    return None


def _role_name(directive):
    match = _ROLE_NOUN.search(directive)
    if match:
        return match.group(0)
    head = re.split(r"[，,。；;]|(?:说话|讲话|而且)", directive, maxsplit=1)[0]
    return head.strip()[:24] or "临时角色"


def is_role_control(text):
    text = (text or "").strip()
    return bool(_CLEAR.search(text) or _descriptor(text) or
                any(pattern.search(text) for pattern, _ in _STYLE_RULES))


def current_state(chat):
    """Read the state on the original Chat, invalidating it after Chat.reset()."""
    if getattr(chat, "_role_state_generation", None) != getattr(chat, "_gen_counter", 0):
        return default_state()
    state = getattr(chat, "_role_state", None)
    return dict(state) if state else default_state()


def update_chat(chat, text, turn_id=None):
    """Apply a user turn before memory extraction and before context assembly.

    A final STT revision with the same item id replaces the speculative parse,
    rather than applying a style delta twice.
    """
    text = (text or "").strip()
    generation = getattr(chat, "_gen_counter", 0)
    state = current_state(chat)
    last = getattr(chat, "_role_last_event", None)
    if getattr(chat, "_role_state_generation", None) != generation:
        last = None
    if last and turn_id and last[0] == turn_id:
        if last[1] == text:
            return state
        state = dict(last[2])
    before = dict(state)
    event = None

    if _CLEAR.search(text):
        if state["active"]:
            state = default_state()
            event = "ROLE_STATE_CLEAR"
    else:
        directive = _descriptor(text)
        if directive:
            event = "ROLE_STATE_SWITCH" if state["active"] else "ROLE_STATE_ENTER"
            state = default_state()
            state.update(active=True, role=_role_name(directive), role_directive=directive)
            state["minor_role"] = bool(_MINOR.search(directive))
            if state["minor_role"]:
                state["relationship_mode"] = "non_intimate_minor_role"
            for word, deltas in _ENTER_TRAITS:
                if word in directive:
                    _change(state, deltas)

        applied = False
        for pattern, deltas in _STYLE_RULES:
            if pattern.search(text):
                if not state["active"]:
                    state["active"] = True
                    event = "ROLE_STATE_ENTER"
                _change(state, deltas)
                applied = True
        if applied and not event:
            event = "ROLE_STATE_UPDATE"
        if state["minor_role"]:
            state["flirt"] = state["intimacy"] = 0

    chat._role_state = state
    chat._role_state_generation = generation
    chat._role_last_event = (turn_id, text, before) if turn_id else None
    if event:
        logger.info("%s role=%r initiative=%d assertiveness=%d flirt=%d intimacy=%d",
                    event, state["role"], state["initiative"], state["assertiveness"],
                    state["flirt"], state["intimacy"])
    return dict(state)


def context_block(state):
    if not state or not state.get("active"):
        return ""
    values = " ".join("%s=%d" % (key, _clamp(state[key])) for key in _VALUES)
    age_rule = ("\nExplicitly minor role: no adult intimacy or sexual roleplay."
                if state.get("minor_role") else "")
    return ("[CURRENT_TEMPORARY_ROLE]\nRole: %s\nDirective: %s\n"
            "Current behavioral state: %s\n"
            "Temporary session roleplay. Follow the latest role/style instruction now; "
            "keep it until changed or cleared. Do not treat it as permanent identity "
            "or turn fictional events into real user facts.%s" %
            (state["role"], state["role_directive"], values, age_rule))
