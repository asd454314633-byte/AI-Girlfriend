"""Tool declarations, read straight out of ``manifests/tool.json`` (任务 §22/§23).

任务 §0 says a tool belongs to the *system*, never to a model: adding, renaming or
deleting a tool must not touch the WebSocket protocol, the state manager, the
logging path or this package.  That is only true if the declaration has exactly one
home, so :func:`load_payload` is the *only* function in the layer that asks where a
tool came from:

* production -> ``registry.load_manifest("TOOL")`` (mtime-cached, i.e. a manifest
  edit is picked up by a running process without a restart), and
* a contract fixture -> the same :func:`parse` fed an in-memory payload that is the
  real manifest plus one extra row, which is how the "add a tool by editing only
  the manifest" test proves the property without touching the production file.

Nothing here names a provider or a transport.  Transport differences are rendered
in :mod:`providers.tool_layer.transport`; file permissions live in
:mod:`providers.tool_layer.fs_guard`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import registry

# The manifest keeps living where the registry put it; this module only points at it.
MANIFEST_PATH = registry.MANIFEST_DIR / "tool.json"

ARGUMENT_TYPES: tuple[str, ...] = ("string", "integer", "number", "boolean", "array", "object")
PATH_FORMAT = "path"
READ_ACCESS = "read"
WRITE_ACCESS = "write"
ARGUMENT_ACCESS = (READ_ACCESS, WRITE_ACCESS)


class ToolManifestError(RuntimeError):
    """A manifest that cannot be honoured is refused, never guessed at."""


@dataclass(slots=True)
class ToolPolicy:
    """Manifest-level defaults (§23): nothing may be readable unless declared."""

    enabled: bool = True
    require_explicit_root: bool = True
    forbidden_globs: tuple[str, ...] = ()
    source: str = "manifest"

    @property
    def forbidden_patterns(self) -> list[str]:
        return [str(item) for item in self.forbidden_globs if str(item).strip()]


@dataclass(slots=True)
class ToolSpec:
    """One row of ``tool.json``, in the shape the whole layer consumes."""

    id: str
    provider_type: str = "TOOL"
    display_name: str = ""
    display_name_zh: str = ""
    version: str = "1"
    enabled: bool = False
    ui_visible: bool = True
    implementation: str | None = None
    entrypoint: str | None = None
    description: str = ""
    #: ``None`` = the row declares nothing -> the system whitelist applies.
    #: ``()``     = the row explicitly declares "no file access at all".
    #: A named root narrows the system whitelist and may be written absolutely or
    #: relative to the project root; either way it can never widen what exists.
    allowed_roots: tuple[str, ...] | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    arguments: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 5.0
    max_results: int | None = None
    max_chars: int | None = None
    writable: bool = False
    whole_disk_read: bool = False
    disabled_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- capability
    @staticmethod
    def _number(entry: Any, default: float | int | None = None) -> float | int | None:
        """A capability may be a bare number (``"timeout_s": 1.5``) or a mapping."""
        if isinstance(entry, dict):
            for key in ("default", "value", "max", "limit"):
                if isinstance(entry.get(key), (int, float)) and not isinstance(entry.get(key), bool):
                    return entry[key]
            return default
        if isinstance(entry, bool):
            return default
        if isinstance(entry, (int, float)):
            return entry
        return default

    @staticmethod
    def _flag(entry: Any) -> bool:
        if isinstance(entry, dict):
            return bool(entry.get("supported"))
        return bool(entry)

    @property
    def label(self) -> str:
        return str(self.display_name_zh or self.display_name or self.id)

    @property
    def has_implementation(self) -> bool:
        return bool(self.implementation and self.entrypoint)

    @property
    def path_arguments(self) -> tuple[str, ...]:
        return tuple(
            name for name, entry in self.arguments.items()
            if isinstance(entry, dict) and str(entry.get("format") or "") == PATH_FORMAT
        )

    @property
    def write_arguments(self) -> tuple[str, ...]:
        return tuple(
            name for name, entry in self.arguments.items()
            if isinstance(entry, dict) and str(entry.get("format") or "") == PATH_FORMAT
            and str(entry.get("access") or READ_ACCESS) == WRITE_ACCESS
        )

    @property
    def file_access(self) -> bool:
        return bool(self.path_arguments)

    def description_for_model(self) -> str:
        """What the model is told the tool does.  Chinese-first, never a provider hint."""
        text = str(self.description or "").strip()
        if text:
            return text
        label = str(self.display_name_zh or self.display_name or "").strip()
        return f"{label}（{self.id}）" if label else self.id

    def note_for(self, name: str) -> dict[str, Any]:
        entry = self.arguments.get(name)
        return dict(entry) if isinstance(entry, dict) else {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider_type": self.provider_type,
            "display_name_zh": self.display_name_zh,
            "version": self.version,
            "enabled": self.enabled,
            "ui_visible": self.ui_visible,
            "implementation": self.implementation,
            "entrypoint": self.entrypoint,
            "allowed_roots": list(self.allowed_roots or ()),
            "declares_roots": self.allowed_roots is not None,
            "file_access": self.file_access,
            "writable": self.writable,
            "whole_disk_read": self.whole_disk_read,
            "timeout_s": self.timeout_s,
            "arguments": dict(self.arguments),
            "disabled_reason": self.disabled_reason,
        }


def load_payload(manifest_path: Path | str | None = None, *, refresh: bool = False) -> dict[str, Any]:
    """The declaration text, from the registry when production, from disk for fixtures."""
    if manifest_path is None:
        return registry.load_manifest("TOOL", refresh=refresh)
    path = Path(manifest_path)
    if not path.exists():
        raise ToolManifestError(f"TOOL_MANIFEST_MISSING:{path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ToolManifestError(f"TOOL_MANIFEST_NOT_OBJECT:{path}")
    return payload


def parse(payload: dict[str, Any], *, source: str = "manifest") -> tuple[list[ToolSpec], ToolPolicy]:
    """Validate + normalize one TOOL manifest payload into :class:`ToolSpec` rows."""
    if not isinstance(payload, dict):
        raise ToolManifestError("TOOL_MANIFEST_NOT_OBJECT")
    defaults = payload.get("defaults") or {}
    policy = ToolPolicy(
        # ``defaults.enabled: false`` in the shipped manifest is a *per-row* default
        # ("no tool exists until its row says enabled"), not a kill switch.  The
        # layer-wide kill switch is its own key, so one never shadows the other.
        enabled=bool(payload.get("layer_enabled", True)),
        require_explicit_root=bool(defaults.get("require_explicit_root", True)),
        forbidden_globs=tuple(str(item) for item in payload.get("forbidden_roots") or ()),
        source=source,
    )
    if not isinstance(payload.get("layer_enabled", True), bool):
        raise ToolManifestError("BAD_LAYER_ENABLED_FLAG")
    specs: list[ToolSpec] = []
    seen: set[str] = set()
    for row in payload.get("providers") or []:
        spec = _spec_from_row(row, defaults)
        if spec.id in seen:
            raise ToolManifestError(f"DUPLICATE_TOOL_ID:{spec.id}")
        seen.add(spec.id)
        specs.append(spec)
    return specs, policy


def _spec_from_row(row: dict[str, Any], defaults: dict[str, Any]) -> ToolSpec:
    if not isinstance(row, dict):
        raise ToolManifestError("TOOL_ROW_NOT_OBJECT")
    tool_id = str(row.get("id") or "").strip()
    if not tool_id:
        raise ToolManifestError("TOOL_WITHOUT_ID")
    capabilities = dict(row.get("capabilities") or {})
    arguments = {str(k): dict(v or {}) for k, v in (row.get("arguments_schema") or {}).items()}
    for name, entry in arguments.items():
        declared = str(entry.get("type") or "string")
        if declared not in ARGUMENT_TYPES:
            raise ToolManifestError(f"BAD_ARGUMENT_TYPE:{tool_id}:{name}:{declared}")
        access = str(entry.get("access") or READ_ACCESS)
        if str(entry.get("format") or "") == PATH_FORMAT and access not in ARGUMENT_ACCESS:
            raise ToolManifestError(f"BAD_PATH_ACCESS:{tool_id}:{name}:{access}")
    enabled = bool(row.get("enabled", defaults.get("enabled", False)))
    implementation = row.get("implementation") or None
    entrypoint = row.get("entrypoint") or None
    if enabled and not (implementation and entrypoint):
        raise ToolManifestError(f"ENABLED_TOOL_WITHOUT_IMPLEMENTATION:{tool_id}")
    raw_roots = row.get("allowed_roots")
    allowed_roots = None if raw_roots is None else tuple(str(item) for item in raw_roots)
    spec = ToolSpec(
        id=tool_id,
        provider_type=str(row.get("provider_type") or "TOOL"),
        display_name=str(row.get("display_name") or ""),
        display_name_zh=str(row.get("display_name_zh") or row.get("display_name") or ""),
        version=str(row.get("version") or "1"),
        enabled=enabled,
        ui_visible=bool(row.get("ui_visible", True)),
        implementation=str(implementation) if implementation else None,
        entrypoint=str(entrypoint) if entrypoint else None,
        description=str(row.get("description") or row.get("description_zh") or row.get("note_zh") or ""),
        allowed_roots=allowed_roots,
        capabilities=capabilities,
        arguments=arguments,
        timeout_s=float(ToolSpec._number(capabilities.get("timeout_s"), 5.0) or 5.0),
        max_results=(int(v) if (v := ToolSpec._number(capabilities.get("max_results"))) is not None else None),
        max_chars=(int(v) if (v := ToolSpec._number(capabilities.get("max_chars"))) is not None else None),
        writable=bool(ToolSpec._flag(capabilities.get("writable"))),
        whole_disk_read=bool(ToolSpec._flag(capabilities.get("whole_disk_read"))),
        disabled_reason=str(row.get("disabled_reason") or ""),
        raw=dict(row),
    )
    if spec.whole_disk_read:
        # §23: "禁止让模型读取整个磁盘" is a system rule, so a row that claims the
        # opposite is a manifest bug, not a capability the layer should honour.
        raise ToolManifestError(f"FORBIDDEN_CAPABILITY_WHOLE_DISK_READ:{tool_id}")
    return spec


__all__ = [
    "ARGUMENT_TYPES",
    "PATH_FORMAT",
    "READ_ACCESS",
    "WRITE_ACCESS",
    "MANIFEST_PATH",
    "ToolManifestError",
    "ToolPolicy",
    "ToolSpec",
    "load_payload",
    "parse",
]
