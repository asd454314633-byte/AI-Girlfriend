"""Transport shapes: the *only* place a wire format is known (任务 §0 / §22).

The Tool Layer speaks one internal language — :class:`~.spec.ToolSpec` in,
:class:`~.contracts.ToolResult` out.  A model adapter asks for a transport id and
gets back exactly what its own protocol wants:

======================  ================================================  ==============================
transport               schema                                            tool call / result
======================  ================================================  ==============================
``chat-completions``    ``{"type":"function","function":{...}}``           ``message.tool_calls[]`` →
                                                                           ``{"role":"tool","tool_call_id":...}``
``responses``           ``{"type":"function","name":...,"strict":false}``  ``response.function_call_arguments``
                                                                           items → ``function_call_output``
``realtime``            ``{"type":"function","name":...}`` (flat)           ``{"type":"function_call"}`` →
                                                                           ``conversation.item.create``
``text``                a rendered system block (no native calling)         one JSON line → 回灌进上下文
======================  ================================================  ==============================

Adding a tool never touches this module.  Adding a *provider* that speaks a new
dialect adds one renderer function here (or an alias), which is exactly the
"provider-specific logic lives in an adapter" rule — and no other module in the
layer learns that anything changed.  Unknown transport ids are a structured error
listing what exists, mirroring ``tts_factory.resolve``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from .spec import PATH_FORMAT, ToolSpec

TRANSPORTS: tuple[str, ...] = ("chat-completions", "responses", "realtime", "text")

#: Declared spellings a transport may arrive under.  Keys are protocol names, never
#: model names, so an alias is a fact about a wire format and not a branch.
TRANSPORT_ALIASES: dict[str, str] = {
    "chat_completions": "chat-completions",
    "openai-chat": "chat-completions",
    "openai_chat": "chat-completions",
    "chat-completions-api": "chat-completions",
    "responses-api": "responses",
    "responses_api": "responses",
    "openai-responses": "responses",
    "realtime-api": "realtime",
    "realtime": "realtime",
    "openai-realtime": "realtime",
    "prompt": "text",
    "proxy": "text",
    "orchestrator": "text",
    "none": "text",
}


class TransportError(ValueError):
    """Raised only for a *programmer* mistake (unknown transport id)."""


@dataclass(slots=True)
class ToolCallRequest:
    """A model's request to use a tool, already mapped onto the internal shape."""

    tool_id: str = ""
    arguments: Any = None
    call_id: str = ""
    transport: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.tool_id) and not self.error

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "arguments": self.arguments,
            "call_id": self.call_id,
            "transport": self.transport,
            "error": self.error,
        }


def normalize_transport(name: Any, *, default: str = "chat-completions") -> str:
    """Resolve a declared transport spelling; unknown values raise, never guess."""
    text = str(name or "").strip()
    if not text:
        return default
    key = text.casefold()
    if key in TRANSPORTS:
        return key
    if key in TRANSPORT_ALIASES:
        return TRANSPORT_ALIASES[key]
    raise TransportError(
        f"Unsupported tool transport: {name!r}. Registered: {', '.join(TRANSPORTS)}. "
        "Add a renderer to providers/tool_layer/transport.py to introduce one."
    )


def supported_transports() -> list[str]:
    return list(TRANSPORTS)


# ------------------------------------------------------------------------ schemas
def parameters_schema(spec: ToolSpec) -> dict[str, Any]:
    """JSON Schema for one row's ``arguments_schema``, generated not hand-written."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, raw in spec.arguments.items():
        rule = dict(raw or {})
        entry: dict[str, Any] = {"type": str(rule.get("type") or "string")}
        described = str(rule.get("description_zh") or rule.get("description") or "")
        if str(rule.get("format") or "") == PATH_FORMAT:
            described = (described + " " if described else "") + (
                "只能写系统白名单目录内（allowed_roots）的相对路径或其中文件的绝对路径；"
                "越界、上级目录、符号链接会被拒绝。"
            )
            entry["format"] = "uri-path"
        if described:
            entry["description"] = described
        options = rule.get("allowed_values") or rule.get("enum")
        if options:
            entry["enum"] = [str(item) for item in options]
        low = rule.get("min", rule.get("minimum"))
        high = rule.get("max", rule.get("maximum"))
        if low is not None:
            entry["minimum"] = low
        if high is not None:
            entry["maximum"] = high
        if rule.get("max_length"):
            entry["maxLength"] = int(rule["max_length"])
        if "default" in rule:
            entry["default"] = rule["default"]
        if rule.get("required"):
            required.append(name)
        properties[name] = entry
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _schema_chat_completions(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.id,
            "description": spec.description_for_model(),
            "parameters": parameters_schema(spec),
        },
    }


def _schema_responses(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "name": spec.id,
        "description": spec.description_for_model(),
        "parameters": parameters_schema(spec),
        "strict": False,
    }


def _schema_realtime(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "name": spec.id,
        "description": spec.description_for_model(),
        "parameters": parameters_schema(spec),
    }


TEXT_HEADER_ZH = (
    "你可以请求下面这些系统工具。需要资料时，只输出一行 JSON（不要同时输出别的文字），"
    "格式：{\"tool_id\":\"工具名\",\"arguments\":{...}}；不需要工具时正常回答。"
)


def _schema_text(spec: ToolSpec) -> dict[str, Any]:
    return {"type": "prompt", "name": spec.id, "text": render_instruction(spec)}


RENDERERS: dict[str, Callable[[ToolSpec], dict[str, Any]]] = {
    "chat-completions": _schema_chat_completions,
    "responses": _schema_responses,
    "realtime": _schema_realtime,
    "text": _schema_text,
}


def render_instruction(spec: ToolSpec) -> str:
    """One tool, described in text for a model that has no native tool calling."""
    lines = [f"- {spec.id}: {spec.description_for_model()}"]
    for name, raw in spec.arguments.items():
        rule = dict(raw or {})
        shape = str(rule.get("type") or "string")
        marks = []
        if rule.get("required"):
            marks.append("必填")
        if "default" in rule:
            marks.append(f"默认 {rule['default']!r}")
        if rule.get("max") is not None:
            marks.append(f"最大 {rule['max']}")
        if rule.get("allowed_values") or rule.get("enum"):
            marks.append("可选值 " + "/".join(str(item) for item in (rule.get("allowed_values") or rule.get("enum"))))
        described = str(rule.get("description_zh") or rule.get("description") or "")
        lines.append(f"    {name}: {shape} {'，'.join(marks)}{'，' if marks and described else ''}{described}")
    return "\n".join(lines)


def tool_schemas(specs: Iterable[ToolSpec], transport: Any = None, *,
                 with_instructions: bool = True) -> dict[str, Any]:
    """The transport-tagged payload a model adapter sends to its provider."""
    name = normalize_transport(transport)
    renderer = RENDERERS[name]
    rows = [renderer(spec) for spec in specs]
    out: dict[str, Any] = {"transport": name, "tools": rows}
    if with_instructions and name == "text":
        out["instructions"] = "\n\n".join([TEXT_HEADER_ZH] + [row["text"] for row in rows])
    return out


def instruction_block(specs: Sequence[ToolSpec], transport: Any = None) -> str:
    """What goes into ``ModelContext.tool_lines`` when the model cannot call tools."""
    name = normalize_transport(transport)
    rows = list(specs)
    if name != "text":
        return ""
    return "\n\n".join([TEXT_HEADER_ZH] + [render_instruction(row) for row in rows])


# ------------------------------------------------------------------- call parsing
def _json_objects(text: str) -> list[Any]:
    """Every balanced JSON object inside free-form prose, strings respected.

    A regex would either miss ``{"a":{"b":1}}`` (nested) or split it in the wrong
    place, and prose is exactly where the proxy route has to find a call.
    """
    out: list[Any] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    out.append(json.loads(text[start:index + 1]))
                except ValueError:
                    pass
                start = -1
    return out


def parse_call(event: Any, transport: Any = None, *, default_transport: str = "chat-completions") -> ToolCallRequest:
    """Map one provider's "I want to use a tool" object onto the internal shape.

    Accepts a whole provider event or the bare call object; a JSON *string* is parsed
    first, because both chat-completions and the realtime wire carry arguments that
    way.  Never raises: a shape it cannot read comes back as ``error=``.
    """
    name = normalize_transport(transport, default=default_transport)
    payload = event
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return ToolCallRequest(transport=name, error="CALL_NOT_JSON")
    if isinstance(payload, dict):
        # providers wrap the call to different depths: chat-completions nests it under
        # "function", the realtime API nests it under "item" (sometimes under "delta")
        for key in ("function", "tool_call", "call", "item", "delta", "response"):
            nested = payload.get(key)
            if isinstance(nested, dict) and (nested.get("name") or nested.get("tool_id")):
                payload = nested
                break
        container = payload.get("tool_calls")
        if isinstance(container, list) and container:
            return parse_call(container[0], name, default_transport=name)
        tool_id = str(payload.get("name") or payload.get("tool_id") or payload.get("function_name") or "")
        arguments = payload.get("arguments", payload.get("args", payload.get("input")))
        call_id = str(payload.get("call_id") or payload.get("id") or payload.get("item_id") or "")
        if not tool_id:
            return ToolCallRequest(transport=name, error="CALL_SHAPE_UNKNOWN")
        return ToolCallRequest(tool_id=tool_id, arguments=arguments, call_id=call_id, transport=name)
    return ToolCallRequest(transport=name, error="CALL_SHAPE_NOT_OBJECT")


def parse_calls(event: Any, transport: Any = None, *,
                default_transport: str = "chat-completions") -> list[ToolCallRequest]:
    name = normalize_transport(transport, default=default_transport)
    if isinstance(event, list):
        return [parse_call(row, name, default_transport=name) for row in event]
    if isinstance(event, dict) and isinstance(event.get("tool_calls"), list):
        return [parse_call(row, name, default_transport=name) for row in event["tool_calls"]]
    return [parse_call(event, name, default_transport=name)]


def render_call_event(transport: Any, *, tool_id: str, arguments: Any = None,
                      call_id: str = "call-1") -> dict[str, Any]:
    """The inverse of :func:`parse_call`: what a provider would have sent us.

    Tests and the offline planner use it so a fixture can speak a real wire shape
    instead of an internal convenience object; a provider adapter never needs it.
    """
    name = normalize_transport(transport)
    body = arguments if isinstance(arguments, str) else json.dumps(arguments or {}, ensure_ascii=False)
    if name == "chat-completions":
        return {"id": call_id, "type": "function", "function": {"name": tool_id, "arguments": body}}
    if name == "responses":
        return {"type": "function_call", "call_id": call_id, "name": tool_id, "arguments": body}
    if name == "realtime":
        return {"type": "function_call", "call_id": call_id, "name": tool_id, "arguments": body}
    return {"tool_id": tool_id, "arguments": arguments or {}, "call_id": call_id}


def extract_text_calls(text: Any, *, transport: str = "text") -> list[ToolCallRequest]:
    """The proxy route (§22): pull ``{"tool_id":...}`` objects out of model prose."""
    out: list[ToolCallRequest] = []
    if not isinstance(text, str) or not text.strip():
        return out
    for payload in _json_objects(text):
        if isinstance(payload, dict) and str(payload.get("tool_id") or "").strip():
            out.append(ToolCallRequest(
                tool_id=str(payload["tool_id"]).strip(),
                arguments=payload.get("arguments"),
                call_id=str(payload.get("call_id") or f"text-{len(out) + 1}"),
                transport=transport,
            ))
    return out


# ----------------------------------------------------------------- result writing
def payload_for_result(result: Any, *, max_chars: int | None = None) -> dict[str, Any]:
    """The JSON a model is allowed to see: no absolute paths, no private fields."""
    data = result.to_dict() if hasattr(result, "to_dict") else dict(result or {})
    rows: list[dict[str, Any]] = []
    for passage in data.get("passages") or []:
        if not isinstance(passage, dict):
            continue
        rows.append({
            "source_kind": passage.get("source_kind", ""),
            "title": passage.get("title", ""),
            "chapter": passage.get("chapter", ""),
            "offset": passage.get("offset", 0),
            "document_id": passage.get("document_id", ""),
            "memory_id": passage.get("memory_id", ""),
            "text": _clip(str(passage.get("text") or ""), max_chars),
        })
    out = {
        "ok": bool(data.get("ok")),
        "tool_id": str(data.get("tool_id") or ""),
        "reason": str(data.get("reason") or ""),
        "text": _clip(str(data.get("text") or ""), max_chars),
        "passages": rows,
        "elapsed_ms": data.get("elapsed_ms", 0),
    }
    return out


def render_output(transport: Any, *, call_id: str, tool_id: str, result: Any,
                  max_chars: int | None = None) -> Any:
    """The object a provider must be sent next to feed a tool result back."""
    name = normalize_transport(transport)
    body = json.dumps(payload_for_result(result, max_chars=max_chars), ensure_ascii=False)
    if name == "chat-completions":
        return {"role": "tool", "tool_call_id": call_id, "name": tool_id, "content": body}
    if name == "responses":
        return {"type": "function_call_output", "call_id": call_id, "output": body}
    if name == "realtime":
        return {
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id, "output": body},
        }
    return {"channel": "context", "tool_id": tool_id, "text": body}


def _clip(text: str, limit: int | None) -> str:
    if not limit or len(text) <= limit:
        return text
    return text[:limit] + "…"


__all__ = [
    "RENDERERS",
    "TEXT_HEADER_ZH",
    "TRANSPORTS",
    "TRANSPORT_ALIASES",
    "ToolCallRequest",
    "TransportError",
    "extract_text_calls",
    "instruction_block",
    "normalize_transport",
    "parameters_schema",
    "parse_call",
    "parse_calls",
    "payload_for_result",
    "render_call_event",
    "render_instruction",
    "render_output",
    "supported_transports",
    "tool_schemas",
]
