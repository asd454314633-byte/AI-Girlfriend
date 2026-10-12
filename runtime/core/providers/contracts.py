"""Unified cross-layer contracts (任务 §0: 先定系统能力 → 再定统一契约).

Every layer speaks these shapes instead of inventing its own:

* the Context Builder produces :class:`ModelContext`; a model adapter may only
  *serialize* it, never compose persona text of its own;
* persona/memory/document services exchange :class:`MemoryRecord` /
  :class:`RetrievedPassage`;
* turn/segment identity travels as :class:`TurnRef` / :class:`SegmentRef` so any
  provider can be held to the same continuity and dedupe rules.

All classes are stdlib-only and JSON round-trippable, because PowerShell and the
browser consume the same dictionaries.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v not in (None, "", [], {})}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


@dataclass(slots=True)
class Contract:
    """Base: JSON round-trip with empty fields dropped, plus a stable type tag."""

    def to_dict(self) -> dict[str, Any]:
        payload = _clean(asdict(self))
        payload.setdefault("schema_version", SCHEMA_VERSION)
        payload["kind"] = type(self).__name__
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Contract":
        known = {f for f in getattr(cls, "__dataclass_fields__", {})}
        return cls(**{k: v for k, v in (payload or {}).items() if k in known})


@dataclass(slots=True)
class TurnRef(Contract):
    """Identity of one user turn, valid across every provider."""

    session_id: str = ""
    turn_id: int = 0
    turn_revision: int = 0
    response_id: str = ""
    item_id: str = ""


@dataclass(slots=True)
class SegmentRef(Contract):
    """Identity of one synthesized segment inside a turn.

    ``segment_index`` is monotonic inside a response and, together with
    ``response_id``, is the idempotency key every downstream consumer (avatar
    renderer, TTS worker) must be able to dedupe on.
    """

    turn: TurnRef = field(default_factory=TurnRef)
    segment_index: int = 0
    segment_count: int = 1
    provider_id: str = ""
    is_final: bool = False


@dataclass(slots=True)
class CapabilitySelection(Contract):
    """A provider-scoped delivery selection, already normalized to canonical ids."""

    provider_type: str = "TTS"
    provider_id: str = ""
    values: dict[str, Any] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MemoryRecord(Contract):
    """One long-term memory row.  User-visible fields are text, never JSON blobs."""

    memory_id: str = ""
    character_id: str = "default"
    layer: str = "LONG_TERM_MEMORY"          # PROFILE|PERSONA|CANON|RELATIONSHIP|LONG_TERM_MEMORY|SESSION|SOURCE
    kind: str = "fact"                        # preference|fact|relationship|commitment|goal|shared_event
    text: str = ""
    confidence: float = 0.5
    explicit: bool = False                    # the user said "记住…/以后…"
    source_session: str = ""
    source_turn: int = 0
    created_at: str = ""
    updated_at: str = ""
    user_editable: bool = True
    active: bool = True
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RetrievedPassage(Contract):
    """A hit from the novel/canon/memory index, with the provenance the UI shows."""

    source_kind: str = "document"             # document|canon|memory
    document_id: str = ""
    title: str = ""
    chapter: str = ""
    text: str = ""
    score: float = 0.0
    offset: int = 0
    memory_id: str = ""


@dataclass(slots=True)
class CharacterProfile(Contract):
    """What the user edits.  Persona *rules* live in CharacterSpec, facts in CANON."""

    character_id: str = "default"
    display_name: str = ""
    nickname: str = ""
    pronoun: str = ""
    speaking_style: str = ""
    relationship: str = ""
    boundary: str = ""
    greeting: str = ""
    likes: list[str] = field(default_factory=list)
    dislikes: list[str] = field(default_factory=list)
    avatar_id: str = ""
    preferred_providers: dict[str, str] = field(default_factory=dict)
    #: The row has always carried this, but the contract dropped it on the way out,
    #: so anything that read it back (bundle export) hit an AttributeError.
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class ModelContext(Contract):
    """The single object a model adapter is allowed to turn into a request.

    Ordering is fixed and provider-independent: system policy, identity, persona
    rules, canon, retrieved canon, relationships, memories, session summary,
    tools, recent turns.  Adapters may reformat, never reorder or re-author.

    Field history (任务 §17 的统一输入清单逐项对得上):
      system policy        -> policy_text          (V1 补齐，之前缺这一路)
      character persona    -> persona_text / profile_text
      relationship         -> relationship_text
      session summary      -> summary_text          (V1 补齐，之前只有原始 history)
      relevant canon       -> canon_text / passage_lines
      relevant long-term   -> memory_lines
      current conversation -> history
      current user message -> user_text

    ``sola_text`` 是「作者官方 Sola V2 Ember 预设」那一块独立规则文本的落点
    （移植任务 AI_GIRLFRIEND_SOLA_V2_EMBER_DIRECT_PORT_01）。它不参与块顺序里任何
    既有环节的换位：只在最后追加，且默认为空串，空串会被 :meth:`system_blocks` 的
    调用侧筛掉，因此关掉开关时本契约对象与接入前逐字节等价。

    ``budget_total`` deliberately has *no* default: a context assembled without a
    number read from the LLM capability manifest is a bug, and letting 6144 leak in
    as a Python default would hide it (任务 §18).

    ``mode_text`` / ``reading_text``（AI_GIRLFRIEND_MODE_SYSTEM_V1）：模式自己的
    输出契约，与「正在朗读的附近原文」这两块。两者都默认为空串，空串会被
    :meth:`system_blocks` 的调用侧筛掉 —— 不在 READING 模式时拼出来的 system 与
    没有模式体系时逐字节相同。它们排在 tools 之后、sola 之前：越靠后的是越具体的
    「这一场怎么说」的约束，而作者那一块是外部资产，永远垫底。
    """

    character_id: str = "default"
    session_id: str = ""
    turn: TurnRef = field(default_factory=TurnRef)
    policy_text: str = ""
    profile_text: str = ""
    persona_text: str = ""
    canon_text: str = ""
    relationship_text: str = ""
    summary_text: str = ""
    memory_lines: list[str] = field(default_factory=list)
    passage_lines: list[str] = field(default_factory=list)
    tool_lines: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    user_text: str = ""
    budget_total: int = 0
    budget_used: int = 0
    budget_dropped: list[str] = field(default_factory=list)
    provider_id: str = ""
    mode_text: str = ""
    reading_text: str = ""
    sola_text: str = ""

    def system_blocks(self) -> list[tuple[str, str]]:
        return [
            ("policy", self.policy_text),
            ("profile", self.profile_text),
            ("persona", self.persona_text),
            ("canon", self.canon_text),
            ("retrieval", "\n".join(self.passage_lines)),
            ("relationship", self.relationship_text),
            ("memory", "\n".join(self.memory_lines)),
            ("session_summary", self.summary_text),
            ("tools", "\n".join(self.tool_lines)),
            # 模式自己的输出契约（ROLEPLAY 的第一人称直接对白 / READING 的朗读纪律）
            ("mode", self.mode_text),
            # 正在朗读的附近原文：只有 READING 模式有，且是文档上下文、不是用户记忆
            ("reading", self.reading_text),
            # 作者官方 Sola V2 Ember 规则块：永远排在已有那些块之后。
            # 默认空串 -> 下面 `if text.strip()` 直接把它筛掉，
            # 所以「没开这一层」时 system_text() 的字节与接入前逐字相同。
            ("sola", self.sola_text),
        ]

    def system_text(self) -> str:
        return "\n\n".join(text.strip() for _name, text in self.system_blocks() if text.strip())


@dataclass(slots=True)
class ToolCall(Contract):
    tool_id: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    turn: TurnRef = field(default_factory=TurnRef)


@dataclass(slots=True)
class ToolResult(Contract):
    tool_id: str = ""
    ok: bool = False
    reason: str = ""
    passages: list[RetrievedPassage] = field(default_factory=list)
    text: str = ""
    elapsed_ms: int = 0


@dataclass(slots=True)
class ProviderHealth(Contract):
    """One readiness verdict, with a *specific* reason per failed conjunct.

    The old code collapsed every probe failure into ``ready=False``, which made a
    slow cold load indistinguishable from a wrong backend and produced false
    "切换器报告完成但未通过就绪检查" messages.
    """

    provider_type: str = "TTS"
    provider_id: str = ""
    ready: bool = False
    checks: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def failed(self) -> list[str]:
        return [str(row.get("name")) for row in self.checks if not row.get("ok")]


def coerce(payload: Any, cls: type[Contract]) -> Any:
    """Best-effort dict -> contract, tolerant of partial PowerShell/JS payloads."""
    if isinstance(payload, cls):
        return payload
    if not isinstance(payload, dict):
        return cls()  # type: ignore[call-arg]
    return cls.from_dict(payload)  # type: ignore[return-value]
