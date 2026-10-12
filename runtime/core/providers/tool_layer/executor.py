"""The unified execution entry point: validate → judge → run → structure → log.

One call, one verdict, no exceptions on the wire:

1. resolve the tool by id **from the manifest** (an unknown id is answered with the
   list of what *does* exist, the same convention ``tts_factory.resolve`` uses);
2. honour the layer switch and the row's ``enabled`` flag — a disabled tool answers
   ``TOOL_DISABLED`` together with the manifest's own ``disabled_reason``, so the
   model can say "这项现在不开" instead of inventing an answer;
3. validate arguments (:mod:`.arguments`) and judge every path argument through the
   tool-bound :class:`~.fs_guard.PathGuard`;
4. import the implementation the *row* names (``implementation``/``entrypoint``) and
   call it with exactly the kwargs it accepts, under the row's ``timeout_s``;
5. normalize whatever came back into :class:`~.contracts.ToolResult`, and turn a
   crash, a timeout or a missing implementation into the same shaped answer;
6. append one audit line (``roots.json`` → ``audit_log``) and one log record.

A model name appears nowhere: routing to "does this provider call tools natively or
do we proxy it" reads declared capabilities and lives in :mod:`.orchestrator`.
"""
from __future__ import annotations

import importlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import registry
from ..contracts import RetrievedPassage, ToolCall, ToolResult
from ..tts_factory import accepted_kwargs
from .arguments import Validation, validate_arguments
from .fs_guard import PathGuard, guard_for
from .roots import RootsConfig, load_config
from .spec import ToolSpec, load_payload, parse
from .transport import (
    ToolCallRequest,
    normalize_transport,
    parse_call,
    render_output,
    tool_schemas,
)

logger = logging.getLogger("providers.tool_layer")

#: Injected into an implementation's call when its signature asks for it, which is
#: how a tool gets the live guard instead of re-implementing the permission rules.
SHARED_CONTEXT = ("tool_id", "tool_spec", "turn", "guard", "roots_config", "layer")

MAX_TEXT_CHARS = 4000


class ToolLayerError(RuntimeError):
    pass


@dataclass(slots=True)
class ToolOutcome:
    """A result plus everything needed to explain it (and to log it)."""

    result: ToolResult
    tool_id: str = ""
    transport: str = "chat-completions"
    call_id: str = ""
    audit: dict[str, Any] = field(default_factory=dict)
    arguments: dict[str, Any] = field(default_factory=dict)
    paths: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.result.ok)

    @property
    def reason(self) -> str:
        return str(self.result.reason or "")

    @property
    def code(self) -> str:
        """First token of the reason: a stable, greppable verdict."""
        return self.reason.split(":", 1)[0] if self.reason else ("OK" if self.ok else "FAILED")

    def wire(self, transport: Any = None, *, max_chars: int | None = None) -> Any:
        """The provider-shaped object that feeds this result back to the model."""
        return render_output(transport or self.transport, call_id=self.call_id,
                             tool_id=self.tool_id, result=self.result, max_chars=max_chars)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "call_id": self.call_id,
            "transport": self.transport,
            "result": self.result.to_dict(),
            "arguments": self.arguments,
            "paths": self.paths,
            "audit": self.audit,
        }


class ToolLayer:
    """Registry-driven tool framework.  Build one, share it everywhere."""

    def __init__(
        self,
        *,
        payload: dict[str, Any] | None = None,
        manifest_path: Path | str | None = None,
        config: RootsConfig | None = None,
        config_path: Path | str | None = None,
        project_root: Path | None = None,
        refresh: bool = False,
    ) -> None:
        self.project_root = Path(project_root or registry.PROJECT_ROOT)
        self.payload = payload if payload is not None else load_payload(manifest_path, refresh=refresh)
        self.specs, self.policy = parse(self.payload, source=str(manifest_path or "registry:TOOL"))
        self._by_id = {spec.id: spec for spec in self.specs}
        self.config = config or load_config(config_path, project_root=self.project_root,
                                            extra_forbidden=self.policy.forbidden_patterns)
        self._guards: dict[str, PathGuard] = {}
        self._modules: dict[str, Any] = {}
        self.audit: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ discovery
    def tools(self, *, enabled_only: bool = False, ui_only: bool = False) -> list[ToolSpec]:
        rows: Iterable[ToolSpec] = self.specs
        if enabled_only:
            rows = [row for row in rows if row.enabled and row.has_implementation]
        if ui_only:
            rows = [row for row in rows if row.ui_visible]
        return list(rows)

    def get(self, tool_id: Any) -> ToolSpec | None:
        return self._by_id.get(str(tool_id or ""))

    def ids(self, *, enabled_only: bool = False) -> list[str]:
        return [row.id for row in self.tools(enabled_only=enabled_only)]

    def schemas(self, transport: Any = None, *, enabled_only: bool = True) -> dict[str, Any]:
        name = self._transport(transport)
        return tool_schemas(self.tools(enabled_only=enabled_only), name)

    def instructions(self, transport: Any = None) -> str:
        """Text-mode instructions (empty string when the transport uses real schemas)."""
        payload = self.schemas(transport)
        return str(payload.get("instructions") or "")

    def guard(self, spec: ToolSpec | dict[str, Any] | None) -> PathGuard:
        row = spec if isinstance(spec, ToolSpec) else self.get((spec or {}).get("id"))
        key = row.id if row else "<system>"
        cached = self._guards.get(key)
        if cached is None:
            # The manifest's ``forbidden_roots`` are folded in here as well as in the
            # config, so a caller that supplies its own RootsConfig can never drop the
            # declaration-level deny list by accident.
            cached = guard_for(self.config, row,
                               require_explicit_root=self.policy.require_explicit_root,
                               extra_forbidden=self.policy.forbidden_patterns)
            self._guards[key] = cached
        return cached

    # ------------------------------------------------------------------- executing
    def execute(
        self,
        call: Any,
        *,
        transport: Any = None,
        turn: Any = None,
    ) -> ToolOutcome:
        """Run one tool call.  Accepts a :class:`ToolCall`, a wire event, or a dict.

        Every failure path returns a structured :class:`~.contracts.ToolResult`; this
        method does not raise for model-shaped input, only for a programmer error
        such as an unregistered transport id.
        """
        started = time.perf_counter()
        request = self._request(call, transport)
        outcome = ToolOutcome(
            result=ToolResult(tool_id=request.tool_id, ok=False, reason=""),
            tool_id=request.tool_id,
            transport=request.transport or self._transport(transport),
            call_id=request.call_id,
        )
        if request.error:
            return self._finish(outcome, started, reason="CALL_REJECTED", detail=request.error)

        spec = self.get(request.tool_id)
        if spec is None:
            known = ", ".join(self.ids()) or "(none)"
            return self._finish(outcome, started, reason="TOOL_UNKNOWN",
                                detail=f"{request.tool_id}. Registered: {known}")
        outcome.tool_id = spec.id
        if not self.config.enabled:
            return self._finish(outcome, started, reason="TOOL_LAYER_DISABLED")
        if not spec.enabled or not spec.has_implementation:
            reason = "TOOL_DISABLED" + (f":{spec.disabled_reason}" if spec.disabled_reason else "")
            return self._finish(outcome, started, reason=reason, spec=spec)

        guard = self.guard(spec)
        validation = validate_arguments(spec, request.arguments, guard=guard)
        outcome.paths = {name: row.as_audit() for name, row in validation.paths.items()}
        if not validation.ok:
            # The guard's own verdict is the headline: ``PATH_OUTSIDE_ROOT`` says what
            # happened, where an aggregate ``TOOL_PATH_DENIED`` would only say that a
            # judge existed.  The per-argument detail stays in ``result.text``.
            return self._finish(outcome, started, reason=_failure_code(validation.errors),
                                detail="; ".join(validation.errors), spec=spec, validation=validation)
        outcome.arguments = dict(validation.clean)

        callable_, resolution_error = self._resolve_callable(spec)
        if callable_ is None:
            return self._finish(outcome, started, reason=resolution_error or "TOOL_NOT_IMPLEMENTED",
                                spec=spec, validation=validation)

        kwargs = self._call_kwargs(spec, callable_, validation, guard, turn)
        value, crash, timed_out = _with_timeout(callable_, kwargs, spec.timeout_s)
        result = _normalize(spec, value, crash=crash, timed_out=timed_out, started=started)
        outcome.result = result
        outcome.audit.update({
            "notes": list(validation.notes),
            "errors": list(validation.errors),
            "timeout_s": spec.timeout_s,
            "file_access": spec.file_access,
            "roots": [str(row.path) for row in guard.roots],
            "missing_roots": guard.missing_roots,
        })
        if timed_out:
            outcome.audit["thread_leaked_until_it_returns"] = True
        return self._finish(outcome, started, spec=spec, validation=validation)

    def execute_many(self, calls: Sequence[Any], *, transport: Any = None, turn: Any = None) -> list[ToolOutcome]:
        return [self.execute(call, transport=transport, turn=turn) for call in calls]

    # -------------------------------------------------------------------- internals
    def _request(self, call: Any, transport: Any) -> ToolCallRequest:
        name = self._transport(transport, soft=True)
        if isinstance(call, ToolCallRequest):
            call.transport = call.transport or name
            return call
        if isinstance(call, ToolCall):
            return ToolCallRequest(tool_id=call.tool_id, arguments=dict(call.arguments or {}),
                                   transport=name)
        if isinstance(call, dict) and ("tool_id" in call or "name" in call) and "arguments" in call:
            # an already-internal shape: do not run it through a transport parser
            return ToolCallRequest(tool_id=str(call.get("tool_id") or call.get("name") or ""),
                                   arguments=call.get("arguments"),
                                   call_id=str(call.get("call_id") or ""),
                                   transport=name)
        return parse_call(call, name, default_transport=name)

    def _transport(self, transport: Any = None, *, soft: bool = False) -> str:
        if transport:
            return normalize_transport(transport)
        declared = str(registry.manifest_value("LLM", "transport", "s2s_llm_backend_flag") or "")
        try:
            return normalize_transport(declared, default="chat-completions")
        except Exception as exc:  # never break a live turn over a manifest typo
            self._transport_error = f"{type(exc).__name__}:{exc}"
            logger.warning("Tool layer transport fallback: %s", exc)
            if soft:
                return "chat-completions"
            return "chat-completions"

    def _resolve_callable(self, spec: ToolSpec) -> tuple[Any, str]:
        module_name = str(spec.implementation or "")
        attribute = str(spec.entrypoint or "")
        if not module_name or not attribute:
            return None, "TOOL_NOT_IMPLEMENTED"
        module = self._modules.get(module_name)
        if module is None:
            try:
                module = importlib.import_module(module_name)
            except ImportError as exc:
                return None, f"TOOL_IMPORT_FAILED:{module_name}:{type(exc).__name__}"
            self._modules[module_name] = module
        target = getattr(module, attribute, None)
        if not callable(target):
            return None, f"TOOL_ENTRYPOINT_MISSING:{module_name}:{attribute}"
        return target, ""

    def _call_kwargs(self, spec: ToolSpec, callable_: Any, validation: Validation,
                     guard: PathGuard, turn: Any) -> dict[str, Any]:
        shared: dict[str, Any] = {
            "tool_id": spec.id,
            "tool_spec": spec,
            "guard": guard,
            "roots_config": self.config,
            "layer": self,
            "turn": turn,
        }
        candidate = dict(validation.clean)
        candidate.update({k: v for k, v in shared.items() if k not in candidate})
        accepted, skipped = _accepted(callable_, candidate)
        for name in skipped:
            logger.debug("tool %s does not accept %s", spec.id, name)
        return accepted

    def _finish(self, outcome: ToolOutcome, started: float, *, reason: str = "",
                detail: str = "", spec: ToolSpec | None = None,
                validation: Validation | None = None) -> ToolOutcome:
        if reason and not outcome.result.reason:
            if detail and reason in detail:
                # the detail already carries the verdict as its first token; repeating
                # it would only teach the model to read past the useful part
                text = detail
            else:
                text = f"{reason}: {detail}" if detail else reason
            outcome.result = ToolResult(tool_id=outcome.tool_id, ok=False, reason=reason, text=text,
                                        elapsed_ms=int((time.perf_counter() - started) * 1000))
        outcome.result.tool_id = outcome.result.tool_id or outcome.tool_id
        outcome.audit.setdefault("notes", list(validation.notes) if validation else [])
        outcome.audit.setdefault("errors", list(validation.errors) if validation else [])
        if spec is not None:
            outcome.audit.setdefault("file_access", spec.file_access)
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tool_id": outcome.tool_id,
            "call_id": outcome.call_id,
            "transport": outcome.transport,
            "ok": outcome.ok,
            "code": outcome.code,
            "reason": outcome.reason,
            "elapsed_ms": outcome.result.elapsed_ms or int((time.perf_counter() - started) * 1000),
            "arguments": _safe_arguments(outcome),
            "paths": outcome.paths,
            "notes": outcome.audit.get("notes", []),
            "errors": outcome.audit.get("errors", []),
        }
        self.audit.append(record)
        del self.audit[:-200]  # keep the in-process ring small; disk holds everything
        self._write_audit(record)
        line = "tool %s ok=%s code=%s ms=%s"
        if outcome.reason and not outcome.ok:
            line += " reason=%s"
        logger.info(line, *( (record["tool_id"], record["ok"], record["code"], record["elapsed_ms"])
                             + ((record["reason"],) if record["reason"] and not record["ok"] else ()) ))
        return outcome

    def _write_audit(self, record: dict[str, Any]) -> None:
        relative = self.config.audit_log
        if not relative:
            return
        try:
            path = Path(relative)
            if not path.is_absolute():
                path = self.project_root / path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # auditing must never take a voice turn down
            logger.warning("tool audit log unwritable (%s): %s", relative, exc)

    # ---------------------------------------------------------------------- health
    def health(self) -> dict[str, Any]:
        rows = [self.guard(row) for row in self.specs]
        return {
            "enabled": self.config.enabled,
            "manifest_source": self.payload.get("manifest_version"),
            "tools": self.ids(),
            "enabled_tools": self.ids(enabled_only=True),
            "disabled_tools": [row.id for row in self.specs if not row.enabled],
            "transport": self._transport(soft=True),
            "transport_error": getattr(self, "_transport_error", ""),
            "file_access_tools": [row.id for row in self.specs if row.file_access],
            "writable_tools": [row.id for row in self.specs if row.writable],
            "roots": [row.as_dict() for row in self.config.roots],
            "dropped_roots": list(self.config.dropped),
            "missing_roots": sorted({item for guard in rows for item in guard.missing_roots}),
            "confine_to_project": self.config.confine_to_project,
            "allow_write_tools": self.config.allow_write_tools,
            "audit_log": self.config.audit_log,
        }


# --------------------------------------------------------------------- module glue
def _failure_code(errors: Sequence[str]) -> str:
    """The most informative reason code for a rejected call.

    A path verdict is already an explanation (``PATH_OUTSIDE_ROOT``,
    ``PATH_FORBIDDEN_PATTERN``, ``PATH_WRITE_FORBIDDEN_BY_LAYER``…) and the guard is
    the single authority on file access, so its code is promoted to the top level.
    Anything else — a missing argument, a bad enum, a type that will not coerce —
    stays under the one generic ``TOOL_INVALID_ARGUMENTS`` heading, with the exact
    list in ``result.text``.
    """
    for row in errors:
        parts = str(row).split(":", 2)
        head = parts[0]
        if head == "PATH_DENIED" and len(parts) == 3:
            return parts[2].split(":", 1)[0] or "TOOL_PATH_DENIED"
        if head.startswith("PATH_WRITE_FORBIDDEN") or head == "PATH_GUARD_UNAVAILABLE":
            return head
    return "TOOL_INVALID_ARGUMENTS"


def _accepted(callable_: Any, candidate: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Same inspect-based kwarg filtering ``tts_factory`` uses for handlers."""
    import inspect

    try:
        parameters = inspect.signature(callable_).parameters.values()
    except (TypeError, ValueError):
        return dict(candidate), []
    if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters):
        return dict(candidate), []
    names = {item.name for item in parameters
             if item.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)}
    accepted = {key: value for key, value in candidate.items() if key in names}
    return accepted, sorted(set(candidate) - set(accepted))


def _with_timeout(callable_: Any, kwargs: dict[str, Any], timeout_s: float) -> tuple[Any, Any, bool]:
    """Run ``callable_`` in a daemon thread so a hung tool cannot hang a turn.

    A thread cannot be killed from Python, so on timeout the worker keeps running
    until its own code returns and the *caller* is handed ``TOOL_TIMEOUT`` right
    away.  Implementations that block forever are a manifest problem (they should
    declare a smaller ``timeout_s`` or not be enabled), and the audit line records
    the leak so it is visible instead of mysterious.
    """
    box: dict[str, Any] = {}
    done = threading.Event()

    def run() -> None:
        try:
            box["value"] = callable_(**kwargs)
        except BaseException as exc:  # noqa: BLE001 - the turn must survive anything
            box["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=run, name=f"tool-{kwargs.get('tool_id', '?')}", daemon=True)
    worker.start()
    if not done.wait(max(0.05, float(timeout_s))):
        return None, None, True
    return box.get("value"), box.get("error"), False


def _normalize(spec: ToolSpec, value: Any, *, crash: Any, timed_out: bool, started: float) -> ToolResult:
    elapsed = int((time.perf_counter() - started) * 1000)
    if timed_out:
        return ToolResult(tool_id=spec.id, ok=False, reason="TOOL_TIMEOUT",
                          text=f"工具超时（>{spec.timeout_s}s）", elapsed_ms=elapsed)
    if crash is not None:
        return ToolResult(tool_id=spec.id, ok=False, reason=f"TOOL_ERROR:{type(crash).__name__}",
                          text=str(crash)[:500], elapsed_ms=elapsed)

    passages: list[Any] = []
    text = ""
    reason = ""
    ok = True

    if isinstance(value, ToolResult):
        return _clip_result(value, spec)
    if value is None:
        text, reason = "", "EMPTY_RESULT"
    elif isinstance(value, str):
        text = value
    elif isinstance(value, bool):
        ok = bool(value)
        reason = "" if value else "TOOL_FAILED"
    elif isinstance(value, dict):
        ok = bool(value.get("ok", True))
        reason = str(value.get("reason") or value.get("说明") or "")
        text = str(value.get("text") or value.get("结果") or "")
        rows = value.get("passages") or value.get("结果条目") or []
        passages = _passages(rows, value)
        if not text and not passages and value:
            text = json.dumps({k: v for k, v in value.items()
                               if k not in ("ok", "reason", "passages")},
                              ensure_ascii=False)[:MAX_TEXT_CHARS]
    elif isinstance(value, (list, tuple)):
        passages = _passages(value, {})
        text = "\n".join(str(getattr(row, "text", row)) for row in passages)
        if not passages:
            text = str(value)[:MAX_TEXT_CHARS]
        if not text:
            ok = False
            reason = "NO_MATCH"
    else:
        text = str(value)[:MAX_TEXT_CHARS]

    if not text and passages:
        text = "\n".join(str(getattr(row, "text", "")) for row in passages)
    if spec.max_chars:
        text = _clip(text, int(spec.max_chars))
    return _clip_result(ToolResult(tool_id=spec.id, ok=ok, reason=reason, passages=passages,
                                   text=text, elapsed_ms=elapsed), spec)


def _passages(rows: Any, meta: dict[str, Any]) -> list[Any]:
    out: list[Any] = []
    for row in rows if isinstance(rows, (list, tuple)) else []:
        if isinstance(row, RetrievedPassage):
            out.append(row)
        elif isinstance(row, dict):
            merged = {key: meta[key] for key in ("title", "document_id", "source_kind")
                      if key in meta and key not in row}
            merged.update(row)
            out.append(RetrievedPassage.from_dict(merged))
        elif hasattr(row, "to_dict"):
            out.append(RetrievedPassage.from_dict(row.to_dict()))
        elif hasattr(row, "text"):
            out.append(RetrievedPassage(source_kind=str(getattr(row, "source_kind", "document")),
                                        document_id=str(getattr(row, "document_id", "")),
                                        title=str(getattr(row, "title", "")),
                                        text=str(row.text),
                                        offset=int(getattr(row, "offset", 0) or 0)))
        else:
            out.append(RetrievedPassage(text=str(row)))
    return out


def _clip_result(result: ToolResult, spec: ToolSpec) -> ToolResult:
    if spec.max_results and len(result.passages) > int(spec.max_results):
        result.passages = list(result.passages)[:int(spec.max_results)]
        result.text = result.text or "\n".join(str(row.text) for row in result.passages)
    if spec.max_chars:
        result.text = _clip(str(result.text or ""), int(spec.max_chars))
    result.tool_id = result.tool_id or spec.id
    return result


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:max(0, limit - 1)] + "…"


def _safe_arguments(outcome: ToolOutcome) -> dict[str, Any]:
    """Never log a resolved absolute path or a model's raw prose into the audit line."""
    out: dict[str, Any] = {}
    for name, value in outcome.arguments.items():
        if name in outcome.paths:
            out[name] = "<in-root>"
        elif isinstance(value, str) and len(value) > 120:
            out[name] = value[:117] + "..."
        else:
            out[name] = value
    return out


_DEFAULT: ToolLayer | None = None
_DEFAULT_LOCK = threading.RLock()


def default_layer(*, refresh: bool = False, **kwargs: Any) -> ToolLayer:
    """Process-wide layer bound to ``manifests/tool.json`` + ``roots.json``.

    The registry caches the manifest by mtime, so editing a tool row changes what a
    *running* process sees the next time :func:`reload` is called — no restart, no
    code change.
    """
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None or refresh:
            _DEFAULT = ToolLayer(refresh=refresh, **kwargs)
        return _DEFAULT


def reload() -> ToolLayer:  # noqa: A003 - the name is the API
    return default_layer(refresh=True)


__all__ = ["MAX_TEXT_CHARS", "SHARED_CONTEXT", "ToolLayer", "ToolLayerError",
           "ToolOutcome", "default_layer", "reload"]
