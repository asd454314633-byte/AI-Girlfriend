"""Where the file-access whitelist lives, and what its safe defaults are.

Two whitelists exist on purpose (任务 §23):

1. **this file** (:file:`roots.json`) is the *system* whitelist: the only directories
   any tool may ever touch, each with an explicit ``read``/``write`` flag.  It is
   configuration, so widening or narrowing file access never needs a code edit.
2. each ``tool.json`` row's ``allowed_roots`` is a *per-tool narrowing*.  A row can
   only shrink what the system already allows; a row root that is not inside a
   system root is dropped with an audit note instead of being honoured.

If :file:`roots.json` is missing or unreadable the layer falls back to
:func:`safe_defaults` — an *empty* whitelist, read-only, writes off — because a
broken config must never open a door.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .. import registry

CONFIG_PATH = Path(__file__).with_name("roots.json")
CONFIG_ENV = "TOOL_LAYER_ROOTS_CONFIG"


@dataclass(slots=True)
class Root:
    """One whitelisted directory, already resolved."""

    declared: str
    path: Path
    read: bool = True
    write: bool = False
    note: str = ""

    @property
    def exists(self) -> bool:
        return self.path.is_dir()

    @property
    def folded(self) -> str:
        return fold(self.path)

    def as_dict(self) -> dict[str, Any]:
        return {
            "declared": self.declared,
            "path": str(self.path),
            "read": self.read,
            "write": self.write,
            "exists": self.exists,
            "note": self.note,
        }


@dataclass(slots=True)
class RootsConfig:
    """Everything :mod:`.fs_guard` needs, in one immutable-ish bag."""

    project_root: Path
    enabled: bool = True
    confine_to_project: bool = True
    allow_write_tools: bool = False
    read_only_by_default: bool = True
    follow_symlinks: bool = False
    max_read_bytes: int = 200_000
    max_list_entries: int = 200
    roots: tuple[Root, ...] = ()
    forbidden_globs: tuple[str, ...] = ()
    audit_log: str | None = None
    source: str = "defaults"
    dropped: tuple[dict[str, Any], ...] = ()

    @property
    def readable_roots(self) -> list[Root]:
        return [row for row in self.roots if row.read and row.exists]

    @property
    def writable_roots(self) -> list[Root]:
        if not self.allow_write_tools:
            return []
        return [row for row in self.roots if row.write and row.exists]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "enabled": self.enabled,
            "confine_to_project": self.confine_to_project,
            "allow_write_tools": self.allow_write_tools,
            "read_only_by_default": self.read_only_by_default,
            "max_read_bytes": self.max_read_bytes,
            "project_root": str(self.project_root),
            "roots": [row.as_dict() for row in self.roots],
            "forbidden_globs": list(self.forbidden_globs),
            "audit_log": self.audit_log,
            "dropped": list(self.dropped),
        }


def fold(path: Path | str) -> str:
    """Windows-safe comparison key: normalized separators, case-insensitive."""
    text = os.path.normcase(str(path))
    return text.casefold().rstrip("\\/") or text


def project_relative(value: str, project_root: Path) -> Path:
    """A config path may be absolute or project-relative; relative never escapes."""
    candidate = Path(str(value).strip().strip('"'))
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return Path(os.path.normpath(str(candidate)))


def safe_defaults(project_root: Path | None = None) -> RootsConfig:
    return RootsConfig(
        project_root=Path(project_root or registry.PROJECT_ROOT),
        enabled=True,
        confine_to_project=True,
        allow_write_tools=False,
        read_only_by_default=True,
        roots=(),
        forbidden_globs=(),
        audit_log=None,
        source="safe_defaults",
    )


def load_config(
    path: Path | str | None = None,
    *,
    project_root: Path | None = None,
    extra_forbidden: Iterable[str] = (),
) -> RootsConfig:
    """Read the whitelist.  Never raises: a bad config tightens, it does not open."""
    root = Path(project_root or registry.PROJECT_ROOT)
    target = Path(path or os.environ.get(CONFIG_ENV) or CONFIG_PATH)
    if not target.exists():
        config = safe_defaults(root)
        config.forbidden_globs = tuple(str(item) for item in extra_forbidden)
        return config
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # An unreadable whitelist is a security-relevant event: fall back to "no
        # roots at all" and say so loudly rather than using the last good copy.
        config = safe_defaults(root)
        config.source = f"unreadable:{target}"
        config.forbidden_globs = tuple(str(item) for item in extra_forbidden)
        return config
    rows: list[Root] = []
    dropped: list[dict[str, Any]] = []
    confine = bool(payload.get("confine_to_project", True))
    # Compare like with like: the project root itself may sit behind a symlink or a
    # mapped drive, so containment is judged against its *resolved* form too.
    try:
        real_project = Path(os.path.realpath(str(root), strict=False))
    except OSError:
        real_project = root
    for item in payload.get("roots") or []:
        declared = str((item or {}).get("path") or "").strip()
        if not declared:
            dropped.append({"path": "", "reason": "ROOT_WITHOUT_PATH"})
            continue
        resolved = project_relative(declared, root)
        try:
            real = Path(os.path.realpath(str(resolved), strict=False))
        except OSError:
            dropped.append({"path": declared, "reason": "ROOT_UNRESOLVABLE"})
            continue
        if confine and not _inside(real, real_project):
            dropped.append({"path": declared, "reason": "ROOT_OUTSIDE_PROJECT"})
            continue
        rows.append(Root(
            declared=declared,
            path=real,
            read=bool((item or {}).get("read", True)),
            write=bool((item or {}).get("write", False)),
            note=str((item or {}).get("note_zh") or (item or {}).get("note") or ""),
        ))
    audit = payload.get("audit_log")
    return RootsConfig(
        project_root=root,
        enabled=bool(payload.get("enabled", True)),
        confine_to_project=confine,
        allow_write_tools=bool(payload.get("allow_write_tools", False)),
        read_only_by_default=bool(payload.get("read_only_by_default", True)),
        follow_symlinks=bool(payload.get("follow_symlinks", False)),
        max_read_bytes=int(payload.get("max_read_bytes", 200_000) or 200_000),
        max_list_entries=int(payload.get("max_list_entries", 200) or 200),
        roots=tuple(rows),
        forbidden_globs=tuple([str(x) for x in payload.get("forbidden_globs") or []]
                              + [str(x) for x in extra_forbidden]),
        audit_log=str(audit) if audit else None,
        source=str(target),
        dropped=tuple(dropped),
    )


def _inside(candidate: Path | str, parent: Path | str) -> bool:
    """Containment on folded, normalized paths (Windows case + separator safe)."""
    left = fold(candidate)
    right = fold(parent)
    if left == right:
        return True
    return left.startswith(right.rstrip("\\/") + "\\") or left.startswith(right.rstrip("\\/") + "/")


__all__ = ["CONFIG_ENV", "CONFIG_PATH", "Root", "RootsConfig", "fold",
           "load_config", "project_relative", "safe_defaults"]
