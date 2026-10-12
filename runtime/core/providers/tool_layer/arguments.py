"""Argument validation for tool calls: manifest-declared types, defaults, bounds.

The declaration lives in ``tool.json`` under ``arguments_schema``, e.g.::

    "limit": {"type": "integer", "required": false, "default": 5, "max": 8}
    "path":  {"type": "string",  "required": true,  "format": "path", "access": "read"}

Rules, in one place, for every tool and every model:

* a **hard error** (the call is rejected before the implementation is entered) for a
  missing required argument, a value that cannot be coerced losslessly, a value
  outside ``allowed_values``, and a path the :class:`~.fs_guard.PathGuard` denies;
* a **note** (the call proceeds, and the note is audited) for a numeric value
  clamped to ``min``/``max``, a string truncated to ``max_length``, a lossless
  ``"5"`` -> ``5`` coercion from a chatty model, and an unknown argument, which is
  *dropped* — a dropped argument can never reach an implementation, so dropping is
  as safe as rejecting and keeps a rambling model usable;
* a path argument is replaced by its **resolved absolute form** so the
  implementation cannot be re-tricked by the original string.

Nothing here knows a provider, a transport, or which model wrote the JSON.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .fs_guard import EXPECT_DIR, EXPECT_FILE, Decision, PathGuard
from .spec import PATH_FORMAT, READ_ACCESS, ToolSpec, WRITE_ACCESS

MISSING = object()


@dataclass(slots=True)
class Validation:
    """Outcome of checking one call's arguments."""

    clean: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    paths: dict[str, Decision] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_audit(self) -> dict[str, Any]:
        return {
            "errors": list(self.errors),
            "notes": list(self.notes),
            "paths": {name: row.as_audit() for name, row in self.paths.items()},
            # What the implementation is actually handed: path arguments travel as
            # their resolved absolute form, never as the model's original string.
            "arguments": {
                name: (self.paths[name].resolved if name in self.paths else value)
                for name, value in self.clean.items()
            },
        }

    def text_for_model(self) -> str:
        return "; ".join(self.errors)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _coerce_number(value: Any) -> float | int | None:
    """Accept a *losslessly* parseable string; models stringify numbers constantly."""
    if _is_number(value):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            as_float = float(text)
        except ValueError:
            return None
        if "." not in text and "e" not in text.lower():
            return int(as_float)
        return as_float
    return None


def validate_arguments(
    spec: ToolSpec,
    raw: Any,
    *,
    guard: PathGuard | None = None,
) -> Validation:
    """Check ``raw`` against ``spec.arguments``.  Returns data, never raises.

    ``guard`` is the tool-bound :class:`~.fs_guard.PathGuard`; when a row declares a
    ``path`` argument and no guard is supplied the call is rejected, because a path
    without a judge is a path without a limit.
    """
    result = Validation()
    payload = _as_mapping(raw, result)
    if payload is None:
        return result

    declared = set(spec.arguments)
    for name in sorted(set(payload) - declared):
        result.notes.append(f"DROPPED_ARGUMENT:{name}")

    for name, note in spec.arguments.items():
        rule = dict(note or {})
        type_name = str(rule.get("type") or "string")
        has_value = name in payload and payload[name] is not None
        value = payload.get(name, MISSING)

        if not has_value:
            if rule.get("required"):
                result.errors.append(f"MISSING_ARGUMENT:{name}")
                continue
            if "default" in rule:
                result.clean[name] = rule["default"]
            continue

        coerced = _typed(name, value, type_name, rule, result)
        if coerced is MISSING:
            continue

        if str(rule.get("format") or "") == PATH_FORMAT:
            _path_argument(name, coerced, rule, spec, guard, result)
            continue

        allowed = rule.get("allowed_values") or rule.get("enum")
        if allowed is not None:
            options = [str(item) for item in allowed]
            if str(coerced) not in options:
                result.errors.append(f"ARGUMENT_NOT_ALLOWED:{name}:{_brief(coerced)}")
                continue
            coerced = _restore_type(coerced, type_name, options)

        coerced, bounded = _apply_bounds(name, coerced, type_name, rule, result)
        if not bounded:
            continue
        result.clean[name] = coerced

    # A row that declares ``"allowed_roots": []`` is judged by the guard, which is
    # the single authority on file access; no second opinion here.
    return result


# ------------------------------------------------------------------------ helpers
def _as_mapping(raw: Any, result: Validation) -> dict[str, Any] | None:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            loaded = json.loads(text)
        except ValueError:
            result.errors.append("ARGUMENTS_NOT_JSON")
            return None
        if isinstance(loaded, dict):
            result.notes.append("ARGUMENTS_WERE_JSON_STRING")
            return dict(loaded)
        result.errors.append("ARGUMENTS_NOT_OBJECT")
        return None
    result.errors.append("ARGUMENTS_NOT_OBJECT")
    return None


def _typed(name: str, value: Any, type_name: str, rule: dict[str, Any], result: Validation) -> Any:
    if type_name == "string":
        if isinstance(value, str):
            text = value
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            result.errors.append(f"ARGUMENT_TYPE:{name}:string")
            return MISSING
        else:
            text = str(value)
            result.notes.append(f"COERCED_TO_STRING:{name}")
        limit = rule.get("max_length") or rule.get("max_chars")
        if limit and len(text) > int(limit):
            text = text[:int(limit)]
            result.notes.append(f"TRUNCATED:{name}:{limit}")
        stripped = text.strip()
        if rule.get("required") and not stripped:
            result.errors.append(f"EMPTY_ARGUMENT:{name}")
            return MISSING
        return stripped if rule.get("trim", True) else text

    if type_name in ("integer", "number"):
        number = _coerce_number(value)
        if number is None:
            result.errors.append(f"ARGUMENT_TYPE:{name}:{type_name}")
            return MISSING
        if isinstance(value, str):
            result.notes.append(f"COERCED_TO_{type_name.upper()}:{name}")
        if type_name == "integer":
            if float(number) != int(number) and isinstance(number, float):
                result.errors.append(f"ARGUMENT_NOT_INTEGER:{name}")
                return MISSING
            return int(number)
        return float(number)

    if type_name == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            result.notes.append(f"COERCED_TO_BOOLEAN:{name}")
            return value.strip().lower() == "true"
        if value in (0, 1):
            result.notes.append(f"COERCED_TO_BOOLEAN:{name}")
            return bool(value)
        result.errors.append(f"ARGUMENT_TYPE:{name}:boolean")
        return MISSING

    if type_name in ("array", "object"):
        key = "items" if type_name == "array" else "properties"
        if isinstance(value, list if type_name == "array" else dict):
            if rule.get(key) and not value and rule.get("required"):
                result.errors.append(f"EMPTY_ARGUMENT:{name}")
                return MISSING
            return value
        if isinstance(value, str) and value.strip():
            try:
                loaded = json.loads(value)
            except ValueError:
                result.errors.append(f"ARGUMENT_TYPE:{name}:{type_name}")
                return MISSING
            if isinstance(loaded, list if type_name == "array" else dict):
                result.notes.append(f"COERCED_TO_{type_name.upper()}:{name}")
                return loaded
        result.errors.append(f"ARGUMENT_TYPE:{name}:{type_name}")
        return MISSING

    result.errors.append(f"UNSUPPORTED_ARGUMENT_TYPE:{name}:{type_name}")
    return MISSING


def _apply_bounds(name: str, value: Any, type_name: str, rule: dict[str, Any],
                  result: Validation) -> tuple[Any, bool]:
    if not _is_number(value):
        return value, True
    low = rule.get("min", rule.get("minimum"))
    high = rule.get("max", rule.get("maximum"))
    if low is not None and value < float(low):
        result.errors.append(f"ARGUMENT_TOO_SMALL:{name}:<{_brief(low)}")
        return value, False
    if high is not None and value > float(high):
        # ``max`` on a retrieval cap is a *ceiling the system owns* (mirrors
        # capabilities.max_results / max_chars), so clamping beats rejecting: the
        # model still gets an answer, and the clamp is audited.
        result.notes.append(f"CLAMPED:{name}:{_brief(value)}->{_brief(high)}")
        return (int(high) if type_name == "integer" else float(high)), True
    return value, True


def _restore_type(value: Any, type_name: str, options: list[str]) -> Any:
    for option in options:
        if str(option) == str(value):
            if type_name in ("integer", "number") and option.lstrip("-").replace(".", "", 1).isdigit():
                return int(option) if type_name == "integer" else float(option)
            return option
    return value


def _path_argument(name: str, value: Any, rule: dict[str, Any], spec: ToolSpec,
                   guard: PathGuard | None, result: Validation) -> None:
    access = str(rule.get("access") or READ_ACCESS)
    write = access == WRITE_ACCESS
    expect = EXPECT_DIR if str(rule.get("expect") or "") == EXPECT_DIR else EXPECT_FILE
    if guard is None:
        result.errors.append(f"PATH_GUARD_UNAVAILABLE:{name}")
        return
    decision = guard.check(value, write=write, expect=expect)
    result.paths[name] = decision
    if not decision.ok:
        result.errors.append(f"PATH_DENIED:{name}:{decision.reason}")
        for note in decision.notes:
            result.notes.append(f"PATH_NOTE:{name}:{note}")
        return
    for note in decision.notes:
        result.notes.append(f"PATH_NOTE:{name}:{note}")
    if write and not spec.writable:
        result.errors.append(f"PATH_WRITE_FORBIDDEN_BY_TOOL:{name}")
        return
    result.clean[name] = decision.resolved


def _brief(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 48 else text[:45] + "..."


__all__ = ["Validation", "validate_arguments"]
