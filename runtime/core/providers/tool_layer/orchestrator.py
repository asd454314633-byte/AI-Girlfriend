"""Provider-neutral orchestration: how a tool call reaches a model and comes back.

任务 §22 allows two shapes of provider and forbids a third thing (a branch per
model):

* **native** — an LLM whose manifest row declares ``tools.supported`` *and*
  ``enabled_in_production`` gets real schemas through
  :func:`providers.tool_layer.transport.tool_schemas` and its own function-call
  events come back through :func:`~.transport.parse_call`;
* **proxy** — anything else gets a rendered instruction block in
  ``ModelContext.tool_lines`` and its prose is mined by
  :func:`~.transport.extract_text_calls`, which is the Orchestrator doing the calling
  on the model's behalf.

:func:`route_for` reads those two declared fields; it never looks at a provider id.
So the day llama.cpp's tool support is trusted, flipping ``enabled_in_production`` in
``llm.json`` changes the route — no edit to this file, and no edit to the persona,
memory, context-builder, WS or logging layers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .. import registry
from ..contracts import ModelContext, ToolCall, TurnRef
from .executor import ToolLayer, ToolOutcome, default_layer
from .transport import extract_text_calls, instruction_block, normalize_transport, parse_calls

logger = logging.getLogger("providers.tool_layer.orchestrator")

NATIVE = "native"
PROXY = "proxy"


@dataclass(slots=True)
class TurnToolPlan:
    """Everything one turn needs to know about tools, in one portable object."""

    provider_id: str = ""
    route: str = PROXY
    transport: str = "chat-completions"
    schemas: list[dict[str, Any]] = field(default_factory=list)
    instructions: str = ""
    tools: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "route": self.route,
            "transport": self.transport,
            "tools": list(self.tools),
            "schemas": list(self.schemas),
            "instructions": self.instructions,
        }


def _capability_flag(entry: Any) -> bool:
    if isinstance(entry, dict):
        return bool(entry.get("supported"))
    return bool(entry)


def _production_flag(entry: Any) -> bool:
    if isinstance(entry, dict):
        return bool(entry.get("enabled_in_production", False))
    return False


def native_calling_declared(provider_id: str | None) -> bool:
    """Both the transport *and* the model row must say yes, with two flags each.

    ``supported_by_transport`` / ``supported`` mean "the wire could carry a tool
    call"; ``enabled_in_production`` is the deliberate human decision that the
    provider's own tool path is trusted.  llama.cpp marks tools experimental, so
    today every row says false and every route is proxy — flipping
    ``llm.json`` is the only edit that changes that.
    """
    shared = registry.manifest_value("LLM", "shared_capabilities", "tools")
    shared = shared if isinstance(shared, dict) else {}
    row = (registry.get("LLM", provider_id) or {}).get("capabilities") or {}
    row = row.get("tools") if isinstance(row, dict) else None
    row = row if isinstance(row, dict) else {}
    return bool(
        shared.get("supported_by_transport", False)
        and shared.get("enabled_in_production", False)
        and _capability_flag(row.get("supported", False))
        and _production_flag(row),
    )


def route_for(provider_id: str | None = None) -> str:
    return NATIVE if native_calling_declared(provider_id) else PROXY


def transport_for(provider_id: str | None = None) -> str:
    spec = registry.get("LLM", provider_id) or {}
    declared = str(spec.get("tool_transport") or "") or str(
        registry.manifest_value("LLM", "transport", "s2s_llm_backend_flag") or "chat-completions")
    try:
        return normalize_transport(declared)
    except Exception:  # a typo in a manifest must not stop a turn
        logger.warning("unknown tool_transport %r declared for %s; using chat-completions",
                       declared, provider_id)
        return "chat-completions"


def plan_for(provider_id: str | None = None, *, layer: ToolLayer | None = None,
             refresh: bool = False) -> TurnToolPlan:
    """The per-turn tool plan an adapter or the WS layer hands to a provider."""
    tools = layer or default_layer(refresh=refresh)
    transport = transport_for(provider_id)
    route = route_for(provider_id)
    enabled = tools.tools(enabled_only=True)
    if route == NATIVE:
        return TurnToolPlan(
            provider_id=str(provider_id or ""), route=route, transport=transport,
            schemas=list(tools.schemas(transport)["tools"]),
            instructions="", tools=[row.id for row in enabled])
    return TurnToolPlan(
        provider_id=str(provider_id or ""), route=route, transport="text",
        schemas=[], instructions=instruction_block(enabled, "text"),
        tools=[row.id for row in enabled])


def run_calls(calls: Sequence[Any], *, provider_id: str | None = None,
              transport: Any = None, turn: Any = None,
              layer: ToolLayer | None = None) -> list[ToolOutcome]:
    """Execute a batch of model-shaped calls; every element answers with data."""
    tools = layer or default_layer()
    name = transport or transport_for(provider_id)
    return tools.execute_many(list(calls), transport=name, turn=turn)


def plan_from_prose(text: Any) -> list[ToolCall]:
    """Proxy route: turn assistant prose into internal :class:`ToolCall` objects."""
    return [ToolCall(tool_id=row.tool_id, arguments=dict(row.arguments or {}))
            for row in extract_text_calls(text)]


def context_lines(outcomes: Iterable[ToolOutcome]) -> list[str]:
    """What a model is told a tool returned — paths stay inside their root."""
    lines: list[str] = []
    for outcome in outcomes:
        result = outcome.result
        body = str(result.text or result.reason or "").strip()
        head = f"[工具 {result.tool_id} {'ok' if result.ok else '拒绝'}]"
        if not result.ok:
            lines.append(f"{head} {body or result.reason}")
            continue
        lines.append(f"{head} {body}" if body else f"{head} （无结果）")
    return lines


def feed_results(context: ModelContext, outcomes: Sequence[ToolOutcome]) -> ModelContext:
    """Fold tool answers back into the context the *next* model call is built from.

    Append-only, provider-independent ordering, and the budget bookkeeping stays the
    Context Builder's: this function does not trim, it only adds lines.
    """
    for line in context_lines(outcomes):
        context.tool_lines.append(line)
    return context


def turn_ref(session_id: str = "", turn_id: int = 0, response_id: str = "") -> TurnRef:
    return TurnRef(session_id=session_id, turn_id=turn_id, response_id=response_id)


__all__ = ["NATIVE", "PROXY", "TurnToolPlan", "context_lines", "feed_results",
           "native_calling_declared", "plan_for", "plan_from_prose", "route_for",
           "run_calls", "tool_transport_hint", "transport_for", "turn_ref"]


def tool_transport_hint(provider_id: str | None = None) -> dict[str, Any]:
    """Small, JSON-shaped answer the UI/WS layer can show without importing the layer."""
    return {"route": route_for(provider_id), "transport": transport_for(provider_id)}
