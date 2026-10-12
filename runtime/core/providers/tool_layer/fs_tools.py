"""Built-in local-file tool implementations, all of them ``allowed_roots`` bound.

A ``tool.json`` row points here with::

    "implementation": "providers.tool_layer.fs_tools",
    "entrypoint":     "read_file"

so adding another file tool is a manifest edit, not a code edit.

These functions *re-judge* every path they receive through the guard the executor
injects, even though :mod:`.arguments` already checked it.  That duplication is
deliberate: the guard is a capability handed to the implementation, so the
implementation never has to trust that somebody else validated its arguments, and a
tool called from anywhere else (a script, a future worker) still cannot read outside
the whitelist.

All of them return plain dicts; :func:`providers.tool_layer.executor._normalize`
turns that into the shared :class:`~.contracts.ToolResult`.  None of them raises for
a hostile or malformed path.
"""
from __future__ import annotations

import logging
import os
from itertools import count
from pathlib import Path
from typing import Any

from .fs_guard import EXPECT_DIR, EXPECT_FILE, PathGuard
from .roots import RootsConfig, load_config
from .spec import ToolSpec

logger = logging.getLogger("providers.tool_layer.fs")

DEFAULT_READ_BYTES = 200_000
_TEMP_SEQUENCE = count(1)


def _guard(guard: PathGuard | None, tool_id: str, config: RootsConfig | None = None) -> PathGuard:
    if guard is not None:
        return guard
    # Called outside the layer (a script, a test): rebuild the same policy from disk
    # rather than running path-free.
    spec = ToolSpec(id=tool_id)
    return PathGuard(config or load_config(), spec=spec)


def read_file(
    path: Any = "",
    *,
    max_bytes: Any = None,
    tool_id: str = "local_file_read",
    guard: PathGuard | None = None,
    roots_config: RootsConfig | None = None,
    tool_spec: ToolSpec | None = None,
    **_ignored: Any,
) -> dict[str, Any]:
    """Read one whitelisted text file, capped, never past the root."""
    limit = int(max_bytes or (tool_spec.max_chars if tool_spec else None) or DEFAULT_READ_BYTES)
    decision = _guard(guard, tool_id, roots_config).check(path, write=False, expect=EXPECT_FILE)
    if not decision.ok:
        return {"ok": False, "reason": f"PATH_DENIED:{decision.reason}", "text": decision.as_model_hint()}
    target = decision.resolved
    try:
        size = os.path.getsize(target)
        with open(target, "rb") as handle:
            blob = handle.read(max(0, limit) + 1)
    except OSError as exc:
        return {"ok": False, "reason": f"FILE_UNREADABLE:{type(exc).__name__}", "text": ""}
    if os.path.normpath(os.path.realpath(target, strict=False)) != os.path.normpath(target):
        # The file moved out from under us between the verdict and the read.
        return {"ok": False, "reason": "PATH_REPARSE_ESCAPE", "text": ""}
    truncated = size > len(blob) or len(blob) > limit
    blob = blob[:limit]
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "reason": "FILE_NOT_TEXT", "text": "该文件不是 UTF-8 文本，不送入模型。"}
    return {
        "ok": True,
        "reason": "" if not truncated else f"TRUNCATED:{limit}",
        "text": text,
        "path": decision.relative,
        "bytes": len(blob),
        "total_bytes": size,
        "passages": [{
            "source_kind": "document",
            "document_id": decision.relative,
            "title": decision.relative,
            "text": text,
            "offset": 0,
        }],
    }


def list_dir(
    path: Any = ".",
    *,
    limit: Any = None,
    tool_id: str = "local_file_list",
    guard: PathGuard | None = None,
    roots_config: RootsConfig | None = None,
    **_ignored: Any,
) -> dict[str, Any]:
    """Name the entries of one whitelisted directory (no content, no recursion)."""
    cap = int(limit or (guard.config.max_list_entries if guard else 200))
    decision = _guard(guard, tool_id, roots_config).check(path, write=False, expect=EXPECT_DIR)
    if not decision.ok:
        return {"ok": False, "reason": f"PATH_DENIED:{decision.reason}", "text": decision.as_model_hint()}
    try:
        names = sorted(os.listdir(decision.resolved))
    except OSError as exc:
        return {"ok": False, "reason": f"DIR_UNREADABLE:{type(exc).__name__}", "text": ""}
    rows: list[dict[str, Any]] = []
    for name in names[:max(0, cap)]:
        full = Path(decision.resolved) / name
        if name.startswith(".") or full.name.lower() in {"__pycache__"}:
            continue
        rows.append({
            "name": name,
            "is_dir": full.is_dir(),
            "bytes": full.stat().st_size if full.is_file() else 0,
        })
    text = "\n".join(f"{'dir ' if row['is_dir'] else 'file'}\t{row['name']}" for row in rows)
    return {
        "ok": True,
        "reason": "" if len(names) <= cap else f"TRUNCATED:{cap}",
        "text": text,
        "path": decision.relative,
        "entries": rows,
    }


def write_file(
    path: Any = "",
    text: Any = "",
    *,
    overwrite: bool = False,
    tool_id: str = "local_file_write",
    guard: PathGuard | None = None,
    roots_config: RootsConfig | None = None,
    **_ignored: Any,
) -> dict[str, Any]:
    """Write inside a whitelisted **writable** root, if the layer allows writes at all.

    Three independent switches must all say yes (tool ``writable``, config
    ``allow_write_tools``, root ``write``), all enforced by the guard, so a manifest
    edit alone can never open a write path.
    """
    decision = _guard(guard, tool_id, roots_config).check(path, write=True, expect=EXPECT_FILE)
    if not decision.ok:
        return {"ok": False, "reason": f"PATH_DENIED:{decision.reason}", "text": decision.as_model_hint()}
    body = str(text if text is not None else "")
    cap = int((guard.config.max_read_bytes if guard else DEFAULT_READ_BYTES))
    if len(body.encode("utf-8")) > cap:
        return {"ok": False, "reason": f"WRITE_TOO_LARGE:{cap}", "text": ""}
    target = Path(decision.resolved)
    if target.exists() and not overwrite:
        return {"ok": False, "reason": "PATH_WRITE_TARGET_EXISTS", "text": ""}
    # A unique temporary name, never a fixed one: on Windows a fixed ".tmp" partner
    # for a state file is a known way to wedge atomic writes permanently.
    temporary = target.with_name(f"{target.name}.{os.getpid()}.{next(_TEMP_SEQUENCE)}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(temporary, "xb") as handle:
            handle.write(body.encode("utf-8"))
        os.replace(temporary, target)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - nothing left to do
            pass
        return {"ok": False, "reason": f"WRITE_FAILED:{type(exc).__name__}", "text": ""}
    return {"ok": True, "reason": "", "text": f"已写入 {decision.relative}（{len(body)} 字）",
            "path": decision.relative, "bytes": len(body)}


__all__ = ["DEFAULT_READ_BYTES", "list_dir", "read_file", "write_file"]
