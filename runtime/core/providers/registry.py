"""Project-level provider registry: one manifest, four consumers.

PowerShell lifecycle scripts, the :7860 admin modules, the browser and the
``speech_to_speech`` adapters all read the *same* files under ``manifests/``.
Nothing in this module knows what a "qwen3" or a "higgs" is: it iterates.

Two authorities are deliberately *not* duplicated here:

* which provider is currently selected -> the runtime state files
  (``active_tts_backend.txt`` / ``active_model.json``), and
* which GGUFs exist / their hashes -> ``runtime/llm/models.json``.

The manifest only says what a provider *can* do and how to reach it.  One kind is
the exception, and deliberately: a start-up-only provider (STT: the recognizer lives
inside the resident :8765 process and cannot be swapped at runtime) has no selection
file to be authoritative, so ``default_selection`` in its manifest *is* the answer to
"which one is active" -- see :func:`active_id`.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from . import capabilities as cap

HERE = Path(__file__).resolve().parent
MANIFEST_DIR = HERE / "manifests"
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")

# runtime/core/providers -> runtime/core -> runtime -> zero_replica
PROJECT_ROOT = Path(os.environ.get("ZERO_REPLICA_ROOT") or HERE.parents[2])

KINDS = ("TTS", "LLM", "STT", "AVATAR", "TOOL")

# Authoritative "who is selected right now" sources.  Values are paths relative to
# the project root; the file content is the provider id (LLM uses a small JSON).
# Kinds absent from this map have no selection state by design: STT is a start-up
# argument of the resident :8765 process and AVATAR a single wedged-in producer, so
# their `default_selection` is the authority (`active_id`), not a fallback.
SELECTION_SOURCES = {
    "TTS": "runtime/windows/active_tts_backend.txt",
    "LLM": "runtime/llm/active_model.json",
}

_lock = threading.RLock()
_cache: dict[str, dict] = {}
_mtime: dict[str, float] = {}


class RegistryError(RuntimeError):
    pass


def _manifest_path(kind: str) -> Path:
    return MANIFEST_DIR / f"{kind.lower()}.json"


def load_manifest(kind: str, *, refresh: bool = False) -> dict:
    """Read one manifest, caching by mtime so edits are picked up without a restart."""
    kind = kind.upper()
    if kind not in KINDS:
        raise RegistryError(f"UNKNOWN_PROVIDER_KIND:{kind}")
    path = _manifest_path(kind)
    if not path.exists():
        raise RegistryError(f"MANIFEST_MISSING:{path}")
    with _lock:
        stamp = path.stat().st_mtime_ns
        if not refresh and _cache.get(kind) is not None and _mtime.get(kind) == stamp:
            return _cache[kind]
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        _validate(kind, payload)
        for spec in payload.get("providers") or []:
            spec.setdefault("provider_type", kind)
        _cache[kind] = payload
        _mtime[kind] = stamp
        return payload


def _validate(kind: str, payload: dict) -> None:
    if not isinstance(payload, dict):
        raise RegistryError(f"MANIFEST_NOT_OBJECT:{kind}")
    seen: set[str] = set()
    seen_cli_flags: set[str] = set()
    for spec in payload.get("providers") or []:
        pid = str(spec.get("id") or "")
        if not pid:
            raise RegistryError(f"PROVIDER_WITHOUT_ID:{kind}")
        if pid in seen:
            raise RegistryError(f"DUPLICATE_PROVIDER_ID:{kind}:{pid}")
        seen.add(pid)
        for name in spec.get("capabilities") or {}:
            if not cap.is_known(name, kind):
                raise RegistryError(f"UNKNOWN_CAPABILITY:{kind}:{pid}:{name}")
        for name in spec.get("runtime_controls") or {}:
            if name not in cap.RUNTIME_CONTROL_VOCABULARY:
                raise RegistryError(f"UNKNOWN_RUNTIME_CONTROL:{kind}:{pid}:{name}")
        if kind.upper() == "STT":
            handler = spec.get("handler")
            if not isinstance(handler, dict):
                raise RegistryError(f"STT_HANDLER_MISSING:{pid}")
            for field in ("module", "class", "cli_flag_value", "cli_arguments_class"):
                if not str(handler.get(field) or ""):
                    raise RegistryError(f"STT_HANDLER_FIELD_MISSING:{pid}:{field}")
            tokens = [str(handler.get("cli_flag_value"))]
            tokens.extend(str(value) for value in handler.get("cli_flag_aliases") or [])
            for token in tokens:
                if token in seen_cli_flags:
                    raise RegistryError(f"DUPLICATE_STT_CLI_FLAG:{token}")
                seen_cli_flags.add(token)
            model_path = handler.get("model_path")
            if model_path is not None:
                if not isinstance(model_path, dict):
                    raise RegistryError(f"STT_MODEL_PATH_NOT_OBJECT:{pid}")
                required = ("placeholder", "cli_flag", "setup_kwarg")
                for field in required:
                    if not str(model_path.get(field) or ""):
                        raise RegistryError(f"STT_MODEL_PATH_FIELD_MISSING:{pid}:{field}")
                launch = spec.get("launch") or {}
                cli_args = list(launch.get("cli_args") or [])
                flag = str(model_path["cli_flag"])
                placeholder = str(model_path["placeholder"])
                expected = "{" + placeholder + "}"
                if flag not in cli_args or cli_args.index(flag) + 1 >= len(cli_args) or cli_args[cli_args.index(flag) + 1] != expected:
                    raise RegistryError(f"STT_MODEL_PATH_LAUNCH_MISMATCH:{pid}:{flag}:{expected}")
                placeholders = launch.get("placeholders") or {}
                if not str(placeholders.get(placeholder) or ""):
                    raise RegistryError(f"STT_MODEL_PLACEHOLDER_UNDECLARED:{pid}:{placeholder}")
    # A kind whose "who is active" lives in a state file must also say who to use
    # when that file does not exist yet.  Keeping that answer in Python was D-37:
    # a fresh install silently assumed a vendor, and the assumption existed in
    # four more places (the admin BFF, the browser, and two PowerShell scripts).
    #
    # The second half of the condition is the STT case: a recognizer is a *start-up*
    # argument of the resident :8765 process (`resident: in_proc_8765`,
    # `runtime_switch.supported:false`), so there is deliberately no selection state
    # file for it and `default_selection` is not a fallback there -- it *is* the
    # authority.  A row the factory can build must therefore declare it, otherwise
    # "which STT is live" is answered by manifest row order (ISSUE_MATRIX
    # STT-FACTORY-MISSING; runtime/windows/Switch-Girlfriend-Voice.ps1 already refuses
    # that on the launch side with S2S_STT_DEFAULT_UNDECLARED).
    factory_buildable = any(isinstance(spec.get("handler"), dict)
                            for spec in payload.get("providers") or [])
    if kind.upper() in SELECTION_SOURCES or factory_buildable:
        default = str(payload.get("default_selection") or "")
        if not default:
            raise RegistryError(f"MISSING_DEFAULT_SELECTION:{kind}")
        if default not in seen:
            raise RegistryError(f"DEFAULT_SELECTION_UNKNOWN:{kind}:{default}")


def default_selection(kind: str) -> str | None:
    """The provider a fresh install uses, declared by the manifest (never by code)."""
    return str(load_manifest(kind).get("default_selection") or "") or None


def manifest_value(kind: str, *keys: str, default: Any = None) -> Any:
    """Walk an arbitrary top-level manifest field, e.g. ``('argument_class_by_backend')``."""
    node: Any = load_manifest(kind)
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def alias_map(kind: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for manifest_kind in KINDS:
        path = _manifest_path(manifest_kind)
        if path.exists():
            out.update(json.loads(path.read_text(encoding="utf-8-sig")).get("field_aliases") or {})
    return out


def providers(kind: str, *, enabled_only: bool = False, ui_only: bool = False) -> list[dict]:
    payload = load_manifest(kind)
    rows = list(payload.get("providers") or [])
    if enabled_only:
        rows = [row for row in rows if row.get("enabled")]
    if ui_only:
        rows = [row for row in rows if row.get("ui_visible", True)]
    return rows


def provider_ids(kind: str, *, enabled_only: bool = False) -> list[str]:
    return [str(row["id"]) for row in providers(kind, enabled_only=enabled_only)]


def get(kind: str, provider_id: str | None) -> dict | None:
    if not provider_id:
        return None
    return next((row for row in providers(kind) if row.get("id") == provider_id), None)


def flag_values(kind: str, *, enabled_only: bool = True) -> list[str]:
    """The tokens a command line may carry for this kind (``--tts``/``--model``)."""
    out: list[str] = []
    for row in providers(kind, enabled_only=enabled_only):
        flag = str((row.get("handler") or {}).get("cli_flag_value") or row.get("id") or "")
        if flag and flag not in out:
            out.append(flag)
    return out


def spec_for_flag(kind: str, flag: str | None) -> dict | None:
    """Resolve a command-line token to its spec, honouring declared aliases.

    ``"qwen"`` -> the qwen3 spec is a *declared* alias, not an ``or`` in core code,
    so a retired flag stops working by editing one manifest row.
    """
    if not flag:
        return None
    for row in providers(kind):
        handler = row.get("handler") or {}
        names = {str(row.get("id") or ""), str(handler.get("cli_flag_value") or "")}
        names.update(str(item) for item in handler.get("cli_flag_aliases") or [])
        if flag in names:
            return row
    return None


def labels(kind: str) -> dict[str, str]:
    return {
        str(row["id"]): str(row.get("display_name_zh") or row.get("display_name") or row["id"])
        for row in providers(kind)
    }


def capabilities_of(kind: str, provider_id: str | None) -> dict[str, Any]:
    spec = get(kind, provider_id)
    return dict((spec or {}).get("capabilities") or {})


def ui_schema(kind: str) -> list[dict[str, Any]]:
    """The whole payload a frontend needs to render any provider, current or future."""
    aliases = alias_map(kind)
    payload = load_manifest(kind)
    rows: list[dict[str, Any]] = []
    for spec in providers(kind):
        rows.append({
            "id": spec["id"],
            "label_zh": spec.get("display_name_zh") or spec.get("display_name"),
            "enabled": bool(spec.get("enabled")),
            "ui_visible": bool(spec.get("ui_visible", True)),
            "controls": cap.ui_controls(spec, aliases),
            "runtime_controls": cap.runtime_control_specs(spec),
            "note_zh": spec.get("panel_note_zh", ""),
            "health_scheme": (spec.get("health") or {}).get("scheme"),
            "version": spec.get("version"),
        })
    return rows


def state_schema(kind: str) -> dict[str, Any]:
    payload = load_manifest(kind)
    return {
        "selection_source": SELECTION_SOURCES.get(kind.upper()),
        "default": default_selection(kind),
        "invariants": payload.get("invariants") or {},
        "field_aliases": payload.get("field_aliases") or {},
    }


def selection_file(kind: str) -> Path | None:
    relative = SELECTION_SOURCES.get(kind.upper())
    return PROJECT_ROOT / relative if relative else None


def active_id(kind: str) -> tuple[str | None, str | None]:
    """Return ``(provider_id, error)``.

    A missing selection file means "fresh install" -> the documented default.  A
    present-but-unknown value is an *error*: silently falling back would hide a
    broken lifecycle, which is exactly the failure mode the brief forbids.
    """
    path = selection_file(kind)
    ids = set(provider_ids(kind))
    if path is None:
        # Nothing on disk names the active provider, so the manifest's declared
        # default *is* the answer.  The old code took "first enabled row" instead,
        # which read as correct while stt.json happened to hold exactly one row and
        # becomes a silent coin-flip the moment a second recognizer is registered.
        default = default_selection(kind)
        if default:
            return (default if default in ids else None,
                    None if default in ids else "SELECTION_DEFAULT_UNKNOWN")
        rows = providers(kind, enabled_only=True)
        return (str(rows[0]["id"]) if len(rows) == 1 else None, None if rows else "NO_PROVIDER")
    if not path.exists():
        default = default_selection(kind)
        return (default if default in ids else None, None if default in ids else "SELECTION_DEFAULT_UNKNOWN")
    try:
        raw = path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        return (None, f"SELECTION_UNREADABLE:{type(exc).__name__}")
    value = raw
    if raw.startswith("{"):
        try:
            value = str(json.loads(raw).get("active_model", "")).strip()
        except ValueError:
            return (None, "SELECTION_NOT_JSON")
    if value not in ids:
        return (None, "ACTIVE_PROVIDER_INVALID")
    return (value, None)


def health_plan(kind: str, provider_id: str | None) -> dict[str, Any]:
    return dict((get(kind, provider_id) or {}).get("health") or {})


def worker_plan(kind: str, provider_id: str | None) -> dict[str, Any]:
    return dict((get(kind, provider_id) or {}).get("worker") or {})


def launch_args(kind: str, provider_id: str | None, context: dict[str, str] | None = None) -> dict[str, Any]:
    """Substituted CLI fragments for the lifecycle scripts.

    ``context`` supplies whatever a placeholder needs (``QWEN3_MODEL_DIR`` …).  An
    unsatisfied placeholder is reported, never guessed at.
    """
    spec = get(kind, provider_id) or {}
    launch = dict(spec.get("launch") or {})
    ctx = {k: str(v) for k, v in (context or {}).items()}
    missing: list[str] = []

    def expand(items: list[str]) -> list[str]:
        out: list[str] = []
        for item in items:
            text = str(item)
            whole = text.startswith("{") and text.endswith("}") and text[1:-1].isidentifier()
            key = text[1:-1] if whole else ""
            if whole and key not in ctx:
                missing.append(key)
                out.append(text)
                continue
            if whole:
                out.append(ctx[key])
                continue
            for name in sorted(ctx, key=len, reverse=True):
                text = text.replace("{" + name + "}", ctx[name])
            for token in _PLACEHOLDER_RE.findall(text):
                missing.append(token)
            out.append(text)
        return out

    env_declared = {str(k): str(v) for k, v in (launch.get("env") or {}).items()}
    env_expanded = dict(zip(env_declared, expand(list(env_declared.values()))))

    return {
        "provider": provider_id,
        "args": expand(list(launch.get("cli_args") or [])),
        "worker_args": expand(list(launch.get("worker_cli_args") or [])),
        "requires": expand(list(launch.get("requires") or [])),
        # the mirror of scripts/Runtime-Common.ps1:Expand-ProviderLaunch: a switch the
        # worker reads at import time (AVATAR_SEGMENT_GATE) belongs in the manifest,
        # not in a lifecycle script's `$env:` line.
        "env": env_expanded,
        "missing_placeholders": sorted(set(missing)),
    }


_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z")


def launch_context(kind: str, provider_id: str | None) -> dict[str, str]:
    """The placeholder context a provider's own row implies -- in Python.

    Exact twin of ``Get-ProviderLaunchContext`` in ``scripts/Runtime-Common.ps1``,
    which is the only reason it can be a twin at all: a declared placeholder is a
    path on this machine when it resolves to one, a bare single token when it is a
    provider-internal identifier, and *nothing* otherwise (so
    :func:`launch_args` reports it under ``missing_placeholders`` and the caller
    fails loudly instead of handing a prose note to a model as an argument).

    Needed by :mod:`providers.stt_factory`, which builds a handler inside the
    process and therefore has to resolve ``{STT_MODEL_DIR}`` the same way the
    lifecycle script does -- two answers for "where are the weights" is how a
    pipeline ends up loading a different model than the one the panel reports.
    """
    spec = get(kind, provider_id) or {}
    declared = dict((spec.get("launch") or {}).get("placeholders") or {})
    context: dict[str, str] = {}
    for name, value in declared.items():
        text = str(value or "").strip()
        if not text:
            continue
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        candidate = Path(os.path.abspath(str(candidate)))
        if candidate.exists():
            context[str(name)] = str(candidate)
        elif _SAFE_TOKEN_RE.match(text):
            context[str(name)] = text
    return context


def model_setup_kwargs(kind: str, provider_id: str | None) -> dict[str, Any]:
    """``{setup_kwarg: value}`` from the row's ``handler.model_path`` block, else ``{}``.

    The block ties three already-declared facts together: the placeholder that names
    the model directory, the CLI flag that carries it, and the keyword the handler's
    ``setup()`` finally receives (the wheel's ``rename_args`` strips the flag's prefix,
    so ``--faster_whisper_stt_model_name`` becomes ``model_name``).  Resolving it here
    keeps "which weights does the recognizer load" a manifest read for the in-process
    factory too, instead of a path the launch script alone knows.
    """
    spec = get(kind, provider_id) or {}
    block = (spec.get("handler") or {}).get("model_path") or {}
    if not isinstance(block, dict):
        return {}
    field = str(block.get("setup_kwarg") or "")
    placeholder = str(block.get("placeholder") or "")
    if not field or not placeholder:
        return {}
    value = launch_context(kind, provider_id).get(placeholder)
    return {field: str(value)} if value else {}


def validate_selection(kind: str, provider_id: str | None, raw: Any) -> tuple[dict[str, Any], list[str]]:
    spec = get(kind, provider_id)
    if spec is None:
        return {}, [f"UNKNOWN_PROVIDER:{provider_id}"]
    return cap.validate_selection(spec, raw, alias_map(kind))


def normalize_selection(kind: str, provider_id: str | None, raw: Any) -> dict[str, Any]:
    clean, _dropped = validate_selection(kind, provider_id, raw)
    return clean


def runtime_control_value(kind: str, provider_id: str | None, name: str, default: Any = None) -> Any:
    """Read a runtime control's state file through the manifest-declared path.

    Both the admin writer and the adapter reader go through here, which is what
    keeps the two from disagreeing (the bug that made the emotion picker dead).
    """
    spec = get(kind, provider_id) or {}
    entry = (spec.get("runtime_controls") or {}).get(name)
    if not isinstance(entry, dict):
        return default
    relative = entry.get("state_file")
    if not relative:
        return default
    path = PROJECT_ROOT / str(relative)
    candidates = [path] + [PROJECT_ROOT / str(old) for old in entry.get("legacy_state_files") or []]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        value = payload.get(name) if name in payload else payload.get(_legacy_control_key(name))
        allowed = {str(item.get("id")) for item in entry.get("allowed_values") or [] if isinstance(item, dict)}
        if isinstance(value, str) and (not allowed or value in allowed):
            return value
    return default


def write_runtime_control(kind: str, provider_id: str | None, name: str, value: str) -> dict[str, Any]:
    spec = get(kind, provider_id) or {}
    entry = (spec.get("runtime_controls") or {}).get(name)
    if not isinstance(entry, dict):
        return {"ok": False, "reason": "NO_SUCH_RUNTIME_CONTROL"}
    allowed = {str(item.get("id")) for item in entry.get("allowed_values") or [] if isinstance(item, dict)}
    if allowed and value not in allowed:
        return {"ok": False, "reason": "INVALID_VALUE"}
    relative = entry.get("state_file")
    if not relative:
        return {"ok": False, "reason": "NO_STATE_FILE"}
    path = PROJECT_ROOT / str(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The temp name must vary per write.  A fixed `<state>.tmp` partner is enough for
    # one lingering handle -- an antivirus scan, a sibling that died still holding it --
    # to make every later write fail for good, and this file is exactly the kind of
    # state the UI reads back as "what is live now".  Same rule the BFF writer and
    # tool_layer/fs_tools.py already follow.
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        tmp.write_text(json.dumps({name: value}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True, "value": value, "state_file": str(relative)}


def _legacy_control_key(name: str) -> str:
    return {"delivery_preset": "emotion"}.get(name, name)


def summary() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kind in KINDS:
        if not _manifest_path(kind).exists():
            continue
        active, error = active_id(kind)
        out[kind] = {
            "providers": provider_ids(kind),
            "active": active,
            "active_error": error,
            "labels": labels(kind),
        }
    return out
