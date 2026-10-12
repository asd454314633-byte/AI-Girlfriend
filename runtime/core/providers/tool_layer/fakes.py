"""Offline fixtures: a tool implementation and a "model" that asks for tools.

任务 §25 splits the test matrix into CORE CONTRACT tests (fakes only, no GPU, no
network, no real model) and REAL PROVIDER REGRESSION.  Everything in this module
serves the first half:

* :func:`echo`, :func:`slow`, :func:`boom`, :func:`spy` are minimal implementations a
  manifest row can point at, which is how a test proves "a new tool needs nothing
  but a manifest row" without touching any real service;
* :class:`ScriptedPlanner` stands in for a language model that wants to call a tool.
  It emits *wire-shaped* events per transport, so the end-to-end test drives the same
  parse → validate → judge → execute → feed-back path a live provider uses.

Never imported by production code paths; only ``tool.json`` rows with
``implementation: "providers.tool_layer.fakes"`` (i.e. fixtures) and tests reach it.
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Sequence

from .transport import render_call_event


def echo(text: Any = "", *, tool_id: str = "echo", **_ignored: Any) -> dict[str, Any]:
    """Return what it was given: proves argument plumbing and result shaping."""
    return {"ok": True, "reason": "", "text": str(text), "tool_id": tool_id}


def spy(**incoming: Any) -> dict[str, Any]:
    """Report what the layer injected — the contract an implementation can rely on."""
    return {
        "ok": True,
        "reason": "",
        "text": ",".join(sorted(incoming)),
        "tool_id": str(incoming.get("tool_id") or "spy"),
        "context": {name: type(value).__name__ for name, value in sorted(incoming.items())},
        "arguments": {
            key: value for key, value in sorted(incoming.items())
            if key not in {"tool_id", "tool_spec", "turn", "guard", "roots_config", "layer"}
        },
    }


def slow(delay_s: Any = 5.0, **_ignored: Any) -> dict[str, Any]:
    """Blocks longer than any sane ``timeout_s``: proves the timeout verdict wins."""
    time.sleep(float(delay_s))
    return {"ok": True, "text": "slept"}


def boom(**_ignored: Any) -> dict[str, Any]:
    """Raises on purpose: an implementation crash must stay a structured answer."""
    raise RuntimeError("fixture implementation failed on purpose")


class ScriptedPlanner:
    """A deterministic stand-in for a model that decides to use tools.

    ``script`` rows are ``(tool_id, arguments)`` pairs, or a ``str`` which is treated
    as prose the ``text`` transport has to mine, or a ``dict`` which is passed through
    untouched (so a test can inject a deliberately broken call shape).
    """

    def __init__(self, script: Iterable[Any] | Sequence[Any] = ()) -> None:
        self.script: list[Any] = list(script)
        self.asked: list[dict[str, Any]] = []

    def events(self, transport: str = "chat-completions") -> list[dict[str, Any]]:
        """The raw provider events for this script, in that transport's shape."""
        out: list[dict[str, Any]] = []
        for index, row in enumerate(self.script):
            call_id = f"call-{index + 1}"
            if isinstance(row, str):
                # prose: only the ``text`` transport can do anything with it
                out.append({"role": "assistant", "content": row})
                continue
            if isinstance(row, tuple):
                row = {"tool_id": row[0], "arguments": row[1]}
            if isinstance(row, dict) and "tool_id" not in row:
                out.append(dict(row))  # verbatim, so a test can inject a broken shape
                continue
            payload = row if isinstance(row, dict) else {}
            out.append(render_call_event(
                transport,
                tool_id=str(payload.get("tool_id") or ""),
                arguments=dict(payload.get("arguments") or {}),
                call_id=call_id,
            ))
        self.asked = out
        return out


__all__ = ["ScriptedPlanner", "boom", "echo", "slow", "spy"]
