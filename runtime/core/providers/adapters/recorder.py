"""Process-local sink for what a fake provider was actually asked to do.

A test double is only useful if the assertions can reach inside it.  Handlers run
on their own pipeline thread, so instance state is awkward to reach from a test
that let the factory build the handler; this module is the agreed side channel.
Keys are ``"<kind>:<provider_id>"`` so one process can host several fakes at once.
"""
from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.RLock()
_STORE: dict[str, list[dict[str, Any]]] = {}


def record(kind: str, provider_id: str, **fields: Any) -> dict[str, Any]:
    row = {"kind": kind, "provider_id": provider_id}
    row.update(fields)
    with _LOCK:
        _STORE.setdefault(f"{kind}:{provider_id}", []).append(row)
    return row


def calls(kind: str | None = None, provider_id: str | None = None) -> list[dict[str, Any]]:
    prefix = f"{kind or ''}:{provider_id or ''}"
    with _LOCK:
        rows: list[dict[str, Any]] = []
        for key, bucket in _STORE.items():
            if key.startswith(prefix):
                rows.extend(bucket)
    return rows


def latest(kind: str, provider_id: str) -> dict[str, Any] | None:
    rows = calls(kind, provider_id)
    return rows[-1] if rows else None


def clear() -> int:
    with _LOCK:
        removed = sum(len(bucket) for bucket in _STORE.values())
        _STORE.clear()
    return removed
