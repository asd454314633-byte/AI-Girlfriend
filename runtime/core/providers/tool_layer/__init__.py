"""Tool Layer — the system's tools, not any model's (任务 §22 + §23).

Why this package exists
-----------------------
``manifests/tool.json`` already declared six tools and an ``allowed_roots`` guardrail,
and nothing consumed it: the browser had its own private tool list and the manifest's
permissions had zero execution points.  This package is that execution point.  It is
deliberately *not* wired to a model, a transport, or a port, so 任务 §0's rule holds:
adding or deleting an LLM/TTS must not force an edit here, and adding or deleting a
tool must not force an edit anywhere else.

The eight rules this package enforces
-------------------------------------
1. **declaration** — :mod:`.spec` reads ``manifests/tool.json`` through
   :mod:`providers.registry` (mtime-cached: a manifest edit is live after
   :func:`reload`, no restart).
2. **schemas** — :mod:`.transport` renders ``chat-completions`` / ``responses`` /
   ``realtime`` / ``text``; those wire differences exist nowhere else.
3. **validation** — :mod:`.arguments` checks types, defaults, ``allowed_values``,
   bounds; a bad call is a structured verdict, never a crash.
4. **permissions** — :mod:`.fs_guard` is the only judge of ``allowed_roots``;
   :file:`roots.json` is the whitelist, and it is read-only by default.
5. **execution** — :mod:`.executor` imports the row's own implementation under the
   row's ``timeout_s`` and normalizes anything it returns.
6. **feedback** — :mod:`.orchestrator` folds results into ``ModelContext.tool_lines``
   (proxy route) or hands back a native ``function_call_output`` (native route).
7. **evidence** — one structured log record plus one JSONL audit line per call.
8. **neutrality** — no provider, model, backend or port name appears in this package;
   ``runtime/tests/test_tool_layer.py`` fails the build if one ever does.

Typical use
-----------
::

    from providers.tool_layer import default_layer, plan_for, feed_results

    layer = default_layer()
    plan = plan_for(active_llm_id)              # native or proxy, from the manifest
    outcome = layer.execute(model_event, transport=plan.transport, turn=turn_ref)
    context = feed_results(context, [outcome])  # 回灌，与模型无关
"""
from __future__ import annotations

from .arguments import Validation, validate_arguments
from .executor import (
    SHARED_CONTEXT,
    ToolLayer,
    ToolLayerError,
    ToolOutcome,
    default_layer,
    reload,
)
from .fs_guard import Decision, PathGuard, guard_for
from .orchestrator import (
    NATIVE,
    PROXY,
    TurnToolPlan,
    context_lines,
    feed_results,
    native_calling_declared,
    plan_for,
    plan_from_prose,
    route_for,
    run_calls,
    tool_transport_hint,
    transport_for,
)
from .roots import RootsConfig, load_config
from .spec import MANIFEST_PATH, ToolManifestError, ToolPolicy, ToolSpec, load_payload, parse
from .transport import (
    RENDERERS,
    TRANSPORTS,
    ToolCallRequest,
    TransportError,
    extract_text_calls,
    instruction_block,
    normalize_transport,
    parse_call,
    parse_calls,
    render_call_event,
    render_output,
    supported_transports,
    tool_schemas,
)

__all__ = [
    "MANIFEST_PATH",
    "NATIVE",
    "PROXY",
    "RENDERERS",
    "SHARED_CONTEXT",
    "TRANSPORTS",
    "Decision",
    "PathGuard",
    "RootsConfig",
    "ToolCallRequest",
    "ToolLayer",
    "ToolLayerError",
    "ToolManifestError",
    "ToolOutcome",
    "ToolPolicy",
    "ToolSpec",
    "TransportError",
    "TurnToolPlan",
    "Validation",
    "context_lines",
    "default_layer",
    "extract_text_calls",
    "feed_results",
    "guard_for",
    "instruction_block",
    "load_config",
    "load_payload",
    "native_calling_declared",
    "normalize_transport",
    "parse",
    "parse_call",
    "parse_calls",
    "plan_for",
    "plan_from_prose",
    "reload",
    "render_call_event",
    "render_output",
    "route_for",
    "run_calls",
    "supported_transports",
    "tool_schemas",
    "tool_transport_hint",
    "transport_for",
    "validate_arguments",
]
