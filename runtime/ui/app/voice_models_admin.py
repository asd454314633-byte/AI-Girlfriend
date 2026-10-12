"""Windows-native voice mode control for the local girlfriend page (the :7860 BFF).

Provider-neutral by construction.  Every fact reported here — which backends exist,
how each one is reached, which process may own it, what its preset vocabulary is, and
what "ready" even means — is read out of ``runtime/core/providers/manifests/*.json``
through ``providers.registry``.  Registering a new voice provider is one manifest row
plus its own adapter: this file does not change, because it never names one.

The two authorities are deliberately the same ones the registry uses:

* "who is selected right now" -> the manifest's ``selection_source``, via
  ``registry.active_id`` (never a fallback guessed in Python), and
* "what a provider can do / how to prove it is alive" -> that provider's own row:
  ``resident``, ``worker``, ``health``, ``runtime_controls``, ``invariants``.

"Ready" is therefore an *aggregate over rows*, not a truth table: each row's own
residency model decides its verdict, and the manifest's
``invariants.max_resident_provider_instances`` bounds how many instances may be live at
once — counted over rows that *prove* their instance exists, never over rows that merely
have nothing contradicting them.  Every conjunct that failed is reported individually in
``health_detail`` — collapsing them into a bare ``ready: false`` is what used to produce
the misleading "the switcher said OK but nothing passed" message at the end of a switch.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = APP_DIR.parents[1]
CORE_DIR = RUNTIME_DIR / 'core'

# ``providers`` is importable as a package only because a .pth file adds
# ``runtime/core`` to the *runtime venv's* ``sys.path``.  This BFF can be served by a
# different interpreter, where that .pth never ran, so make the package reachable
# explicitly.  Guarded: a no-op when the venv already put it there.
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from providers import registry  # noqa: E402  (the path bootstrap above must come first)
import pid_liveness  # noqa: E402  (same directory: read-only-handle liveness, see its header)

KIND = 'TTS'
KINDS = registry.KINDS

NATIVE_ROOT = RUNTIME_DIR / 'windows'
PID_FILE = NATIVE_ROOT / 'service_pids.json'
SWITCH_STATE_FILE = NATIVE_ROOT / 'tts_switch_state.json'
SCRIPT = NATIVE_ROOT / 'Switch-Girlfriend-Voice.ps1'
POWERSHELL = str(Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft' / 'WindowsApps' / 'pwsh.exe')

# Fixed *product* topology, identical for every backend, and therefore not provider
# coupling: this is the realtime pipeline process that hosts in-process handlers and
# the pool endpoint that says whether a turn is in flight.
PIPELINE_BASE = os.environ.get('S2S_POOL_BASE', 'http://127.0.0.1:8765').strip().rstrip('/')
PIPELINE_POOL_URL = PIPELINE_BASE + '/v1/pool'
PIPELINE_PID_KEY = 's2s'
PIPELINE_EXE_SUFFIX = str(CORE_DIR / 'venv' / 'scripts' / 'speech-to-speech.exe')
PIPELINE_POOL_SIZE = 1
PROBE_TIMEOUT_S = 2.0

# The one runtime control the page exposes as "emotion".  Its id vocabulary, its
# state file and its legacy shadows all live in the manifest row that declares it.
PRESET_CONTROL = 'delivery_preset'
_PRESET_LABEL_FALLBACK = '语音模型'

_lock = threading.Lock()
_state: dict[str, Any] = {'busy': False, 'target': '', 'result': '', 'detail': '', 'phase': '', 'message': ''}
_runner: threading.Thread | None = None

# ------------------------------------------------------- switch transaction (TSSCP01)
# TASK_ID = AI_GIRLFRIEND_TTS_SWITCH_CONTROL_PLANE_FINAL_REPAIR_01.  "Is a switch in
# flight" used to have TWO independent authorities — this module's in-process
# ``_state['busy']`` and the ``tts_switch_state.json`` file, written by both this
# process and the lifecycle script.  A wedged runner thread (its stdout pipe held
# open forever by executor-spawned grandchildren) made the in-memory flag immortal,
# and the page refused every switch with SWITCH_ALREADY_RUNNING from that moment on.
# The only authority now is the journal below: one writer (this controller), phases
# instead of a boolean, and every non-terminal phase bound to a *provable* child.
# ``busy`` survives only as a derived projection for the page (§5: busy = phase is
# non-terminal) — it is never stored and never read back as a fact.

PHASE_IDLE = 'IDLE'
PHASE_STARTING = 'STARTING'
PHASE_RUNNING = 'RUNNING'
PHASE_VERIFYING = 'VERIFYING'
PHASE_COMMITTED = 'COMMITTED'
PHASE_ROLLED_BACK = 'ROLLED_BACK'
PHASE_FAILED = 'FAILED'
PHASE_ABORTED_STALE = 'ABORTED_STALE'
ACTIVE_PHASES = (PHASE_STARTING, PHASE_RUNNING, PHASE_VERIFYING)
TERMINAL_PHASES = (PHASE_COMMITTED, PHASE_ROLLED_BACK, PHASE_FAILED, PHASE_ABORTED_STALE)

REASON_RUNTIME_MISMATCH = 'TTS_RUNTIME_STATE_MISMATCH'
REASON_STALE_RECONCILED = 'STALE_TRANSACTION_RECONCILED'

# How long a STARTING claim with no spawnable child yet stays believable.  The claim
# is written under ``_lock`` before the runner thread spawns the executor, so there
# is a real window in which the journal is non-terminal but no child pid exists to
# verify; without the grace period a second request inside that window would
# reconcile a perfectly live transaction away.
STARTING_GRACE_S = 240.0

EXECUTOR_TIMEOUT_S = 900.0

# Legacy dual-authority keys: once written as independent facts, now neither stored
# nor read.  A journal carrying them is normalized on the next write.
_LEGACY_JOURNAL_KEYS = ('busy', 'owner_pid')


class JournalWriteError(RuntimeError):
    """The journal could not be written or failed its read-back verification."""


# -------------------------------------------------------------------------- state I/O

def _write_json_atomic(path: Path, payload: dict) -> None:
    """Persist ``payload`` so a torn write can never masquerade as state.

    A *unique* temporary name in the target directory matters on Windows: a fixed
    ``*.tmp`` name is enough for one lingering handle (an antivirus scan, a crashed
    sibling) to make every later write fail for good, which is how a state file stops
    accepting updates while the UI keeps reporting success.
    """
    tmp = path.with_name(f'{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:12]}.tmp')
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _read_json(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _service_pids() -> dict:
    return _read_json(PID_FILE)


def _read_switch_state() -> dict:
    """The switch transaction journal — the ONLY "is a switch in flight" authority.

    Legacy keys (``busy``, ``owner_pid``) are dropped on read: they were the second
    authority that made a stale flag immortal, and a fact that is not read back
    cannot gate anything (§3/§4).
    """
    raw = _read_json(SWITCH_STATE_FILE)
    record = _blank_journal()
    for key, value in raw.items():
        if key in _LEGACY_JOURNAL_KEYS:
            continue
        record[key] = value
    if record['phase'] not in (PHASE_IDLE,) + ACTIVE_PHASES + TERMINAL_PHASES:
        # Legacy journals carry phase:"" (no transaction concept) — that reads as
        # IDLE, not FAILED.  Only a *non-empty* unknown phase is treated as broken.
        record['phase'] = PHASE_IDLE if not record['phase'] else PHASE_FAILED
    return record


def _blank_journal() -> dict:
    return {'switch_id': '', 'target': '', 'source': '', 'phase': PHASE_IDLE,
            'started_at': '', 'updated_at': '', 'child_pid': 0, 'child_start_time': '',
            'result': '', 'reason': '', 'detail': '', 'message': '', 'previous': ''}


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def _journal_write(**fields: Any) -> dict:
    """Persist journal fields atomically, then prove the file took them (§10).

    Write → temp file with a unique name → ``os.replace`` → read back and verify the
    identity-carrying keys.  A write that raises before the replace leaves the previous
    journal byte-identical on disk (the caller keeps a valid old version); a replace
    that does not stick raises instead of silently pretending.
    """
    merged = _read_switch_state()
    merged.update(fields)
    merged['updated_at'] = _now_iso()
    if not str(merged.get('switch_id') or ''):
        merged['switch_id'] = uuid.uuid4().hex
    _write_json_atomic(SWITCH_STATE_FILE, merged)
    persisted = _read_json(SWITCH_STATE_FILE)
    for key in ('switch_id', 'phase', 'target', 'child_pid'):
        if persisted.get(key) != merged.get(key):
            raise JournalWriteError(f'JOURNAL_VERIFY_FAILED key={key} '
                                    f'expected={merged.get(key)!r} persisted={persisted.get(key)!r}')
    return persisted


def _derived_busy(journal: dict) -> bool:
    """§5: ``busy`` is a projection of the phase, never a stored fact."""
    return journal.get('phase') in ACTIVE_PHASES


def _process_alive(pid: int) -> bool:
    """Best effort "is this pid still somebody's process?".

    Returns False when the question cannot be answered, so a missing ``psutil`` can
    never latch the switch into a permanent BUSY refusal.

    The fallback is ``pid_liveness``, and it is NOT ``os.kill(pid, 0)``.  On Windows
    CPython, ``CTRL_C_EVENT`` is literally 0, so sig=0 first calls
    ``GenerateConsoleCtrlEvent(0, pid)`` and -- whenever the target does not share this
    process' console, which is the normal case for every pid in ``service_pids.json`` --
    falls through to ``OpenProcess(PROCESS_TERMINATE)`` + ``TerminateProcess(handle, 0)``:
    a liveness question that kills whatever it asked about, leaving exit status
    0x00000000, no ``atexit``, no traceback and no new process.  Measured in
    ``runtime/debug/pid_probe/lab3_cross_console_kill0.py``; see also
    ``runtime/ui/app/avatar_admin.py::_pid_alive``.  ``psutil.pid_exists`` above is the
    safe primitive (query handle only) and stays first.
    """
    if pid <= 0:
        return False
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except Exception:
        pass
    return pid_liveness.pid_alive(pid)


# ----------------------------------------------------------------------- selection

def _active_pair() -> tuple[str | None, str | None]:
    """``(provider_id, error)`` straight from the registry — the only reader of the
    selection file in this process, so the page and the adapters can never disagree."""
    return registry.active_id(KIND)


def _active() -> str | None:
    return _active_pair()[0]


def labels() -> dict[str, str]:
    return registry.labels(KIND)


# --------------------------------------------------------------- runtime control/preset

def _preset_rows() -> list[dict]:
    """Rows that declare the preset control — i.e. the providers that can honour it."""
    return [
        row for row in registry.providers(KIND)
        if isinstance((row.get('runtime_controls') or {}).get(PRESET_CONTROL), dict)
    ]


def _preset_owner() -> str | None:
    """Which provider owns the preset control (preferring the one in use).

    This is the fix for the picker that silently did nothing: the writer used to
    target one hand-picked file while ``registry.runtime_control_value`` read a
    *candidate list* in manifest order, so an older legacy file shadowed whatever the
    page wrote.  Writer and readers now resolve the same row, hence the same file.
    """
    rows = _preset_rows()
    active, _error = _active_pair()
    for row in rows:
        if row.get('id') == active:
            return str(row['id'])
    return str(rows[0]['id']) if rows else None


def _preset_entry(owner: str | None) -> dict:
    row = registry.get(KIND, owner) or {}
    entry = (row.get('runtime_controls') or {}).get(PRESET_CONTROL)
    return entry if isinstance(entry, dict) else {}


def preset_values() -> list[str]:
    """The accepted preset ids, from the manifest (never a set copied into code)."""
    return [str(item.get('id')) for item in _preset_entry(_preset_owner()).get('allowed_values') or []
            if isinstance(item, dict)]


def _preset_default(owner: str | None) -> str:
    return str(_preset_entry(owner).get('default') or '')


def emotion() -> str:
    """Current preset as the *adapters* see it, i.e. through the same candidate order."""
    owner = _preset_owner()
    if not owner:
        return ''
    default = _preset_default(owner)
    value = registry.runtime_control_value(KIND, owner, PRESET_CONTROL, default)
    return str(value if value not in (None, '') else default)


def _attach_current(rows: list[dict], kind: str = KIND) -> list[dict]:
    """Stamp each row's runtime controls with the value the adapters will read.

    The browser renders one widget per declared control, so the control's *current*
    value has to travel with its declaration; without it a freshly opened page shows
    the manifest default instead of the live setting, and a user "changes" something
    that was already set.  Read through ``registry.runtime_control_value`` — the same
    candidate order every adapter uses, never a second path guess.
    """
    for row in rows:
        provider_id = str(row.get('id') or '')
        for control in row.get('runtime_controls') or []:
            if not isinstance(control, dict):
                continue
            value = registry.runtime_control_value(kind, provider_id, str(control.get('field') or ''),
                                                  control.get('default', ''))
            control['current'] = str(value if value not in (None, '') else control.get('default', ''))
    return rows


def set_runtime_control(provider_id: str | None, control: str, value: str) -> dict:
    """Write any declared runtime control of any provider — one code path, no per-vendor branch.

    ``/api/voice-models/emotion`` stays as the historical alias for the preset control;
    this is the generic form the capability-driven page posts to, and it validates the
    value against the vocabulary that *that* provider declared.
    """
    name = str(control or '').strip()
    row = registry.get(KIND, str(provider_id or '').strip() or None)
    if row is None:
        return {'ok': False, 'reason': 'UNKNOWN_PROVIDER', 'provider': provider_id}
    entry = (row.get('runtime_controls') or {}).get(name)
    if not isinstance(entry, dict):
        return {'ok': False, 'reason': 'NO_SUCH_RUNTIME_CONTROL', 'provider': row.get('id'), 'control': name}
    receipt = registry.write_runtime_control(KIND, str(row['id']), name, str(value or '').strip())
    if not receipt.get('ok'):
        return {**receipt, 'provider': row.get('id'), 'control': name}
    return {'ok': True, 'provider': row.get('id'), 'control': name,
            'value': str(receipt.get('value', value)), 'state_file': receipt.get('state_file')}


def set_emotion(value: str) -> dict:
    """Write the preset through the registry, validating against its own vocabulary."""
    owner = _preset_owner()
    if not owner:
        return {'ok': False, 'reason': 'NO_PRESET_OWNER'}
    receipt = registry.write_runtime_control(KIND, owner, PRESET_CONTROL, value)
    if not receipt.get('ok'):
        reason = str(receipt.get('reason') or 'WRITE_FAILED')
        return {'ok': False, 'reason': 'UNKNOWN_EMOTION' if reason == 'INVALID_VALUE' else reason,
                'provider': owner}
    return {'ok': True, 'emotion': str(receipt.get('value', value)), 'provider': owner,
            'state_file': receipt.get('state_file')}


# -------------------------------------------------------------------- process probes

def _tracked_process(pid: int, *, executable_suffix: str = '', command_marker: str = '') -> bool:
    """Does ``pid`` still look like the process we started, and not a recycled one?

    The exe/marker pair is what makes a stale pid file harmless: both are supplied by
    the provider's own ``worker`` row, never named here.
    """
    if pid <= 0:
        return False
    try:
        import psutil

        proc = psutil.Process(pid)
        return (
            proc.is_running()
            and (not executable_suffix or proc.exe().lower().endswith(executable_suffix.lower()))
            and (not command_marker or command_marker.lower() in ' '.join(proc.cmdline()).lower())
        )
    except Exception:
        return False


def _worker_running(spec: dict, pids: dict) -> bool:
    """Is this provider's external worker up, per ``worker.{pid_key,executable_suffix,command_marker}``?"""
    worker = dict(spec.get('worker') or {})
    key = str(worker.get('pid_key') or spec.get('id') or '')
    try:
        pid = int(pids.get(key) or 0)
    except (TypeError, ValueError):
        pid = 0
    return _tracked_process(
        pid,
        executable_suffix=str(worker.get('executable_suffix') or ''),
        command_marker=str(worker.get('command_marker') or ''),
    )


def _pipeline_port() -> int:
    """The TCP port the realtime pipeline answers on, taken from the pool URL itself."""
    match = re.search(r'://[^/:]+:(\d+)', PIPELINE_BASE)
    if match:
        return int(match.group(1))
    return 8765 if PIPELINE_BASE.startswith('http') else 0


def _port_owner_pids(port: int) -> list[int]:
    """Pids holding a LISTEN socket on ``port``.

    Authoritative for "who answers this endpoint right now", which is not the same
    question as "which pid did the launcher write down": ``service_pids.json`` records
    the *launcher* (``speech-to-speech.exe``), while the socket belongs to the python
    grandchild it spawned — and once the launcher exits, its pid is recyclable by an
    unrelated process.  Returns empty when process enumeration is denied, which the
    caller must treat as "no evidence", never as "not running".
    """
    if port <= 0:
        return []
    try:
        import psutil
    except Exception:
        return []
    found: list[int] = []
    try:
        rows = psutil.net_connections(kind='inet')
    except Exception:
        return []
    for row in rows or []:
        try:
            local = getattr(row, 'laddr', None)
            if not local or int(local.port) != port:
                continue
            if str(getattr(row, 'status', '')) not in ('LISTEN', 'TCP_LISTEN'):
                continue
            if getattr(row, 'pid', None):
                found.append(int(row.pid))
        except Exception:
            continue
    return sorted(set(found))


def _descendant_pids(pid: int) -> list[int]:
    if pid <= 0:
        return []
    try:
        import psutil
        return [int(item.pid) for item in psutil.Process(pid).children(recursive=True)]
    except Exception:
        return []


def _pipeline_identity(proc: Any) -> tuple[bool, list[str]]:
    """(is-this-the-pipeline, its command line) for one candidate process.

    Both shapes are accepted: the ``speech-to-speech.exe`` launcher and the python
    interpreter it spawns, whose argv carries that exe path.  Neither name is a
    provider name, so this stays neutral across every backend.
    """
    want = PIPELINE_EXE_SUFFIX.lower()
    try:
        exe = str(proc.exe()).lower()
    except Exception:
        return False, []
    try:
        argv = [str(item) for item in proc.cmdline()]
    except Exception:
        argv = []
    if exe.endswith(want):
        return True, argv
    return exe.endswith('python.exe') and any(want in item.lower() for item in argv), argv


def _pipeline_facts(pids: dict) -> dict:
    """Identity, command line and pool occupancy of the resident pipeline host.

    Evidence is gathered in decreasing order of authority — whoever owns the listening
    socket, then the recorded launcher, then its descendants — and every conjunct
    reports *which* evidence carried it.  Process introspection can legitimately be
    denied (a restricted token, another session, a recycled pid), and when it is, the
    pool endpoint on that same port is the remaining authority: an in-process handler
    is answering.  Tolerant by design, so a diagnosis never looks like a verdict.
    """
    facts: dict[str, Any] = {'exe_ok': False, 'command_line': [], 'pool': {}, 'notes': [],
                            'pid': 0, 'pid_source': 'none', 'process_probe': 'unavailable'}
    try:
        import psutil
    except Exception as exc:
        facts['process_probe'] = f'PSUTIL_UNAVAILABLE:{type(exc).__name__}'
    tracked = 0
    try:
        tracked = int(pids.get(PIPELINE_PID_KEY) or 0)
    except (TypeError, ValueError):
        tracked = 0
    candidates: list[tuple[int, str]] = []
    seen: set[int] = set()
    for pid, source in ([(item, 'port_owner') for item in _port_owner_pids(_pipeline_port())]
                        + ([(tracked, 'tracked')] if tracked else [])
                        + [(item, 'descendant') for item in _descendant_pids(tracked)]):
        if pid and pid not in seen:
            seen.add(pid)
            candidates.append((pid, source))
    if facts['process_probe'] == 'unavailable':
        for pid, source in candidates:
            try:
                proc = psutil.Process(pid)
                matched, argv = _pipeline_identity(proc)
            except Exception as exc:
                facts['notes'].append(f'CANDIDATE_UNREADABLE pid={pid}:{type(exc).__name__}')
                continue
            if matched:
                facts['exe_ok'] = True
                facts['pid'] = pid
                facts['pid_source'] = source
                facts['command_line'] = argv
                break
        if not facts['exe_ok']:
            facts['notes'].append(
                'PIPELINE_PROCESS_UNREADABLE:' + (facts['process_probe'] if candidates else 'NO_CANDIDATE'))
    try:
        body = httpx.get(PIPELINE_POOL_URL, timeout=PROBE_TIMEOUT_S).json()
        if isinstance(body, dict):
            facts['pool'] = body
            facts['notes'].append(f'POOL_OK via {PIPELINE_POOL_URL}')
        else:
            facts['notes'].append('PIPELINE_POOL_NOT_OBJECT')
    except Exception as exc:
        facts['notes'].append(f'PIPELINE_POOL_UNREACHABLE:{type(exc).__name__}')
    # The pool endpoint is the fallback authority when the pid file is stale or the
    # interpreter behind it cannot be introspected from this token.
    if not facts['exe_ok'] and facts['pool']:
        facts['exe_ok'] = True
        facts['pid_source'] = 'pool_endpoint'
        facts['notes'].append('PROCESS_INTROSPECTION_DENIED; POOL_ENDPOINT_AUTHORITATIVE')
    return facts


# ---------------------------------------------------------------------- health probes

_PLACEHOLDER_RE = re.compile(r'\{([A-Za-z0-9_]+)\}')


def _check(name: str, ok: Any, detail: Any = '', *, required: bool = True) -> dict[str, Any]:
    """One conjunct of one verdict.  ``required=False`` = informative only."""
    return {'check': name, 'ok': bool(ok), 'detail': str(detail)[:200], 'required': bool(required)}


def _credential(health: dict, name: str) -> str:
    """Contents of the credential file a row declares, resolved against the project root."""
    declared = health.get('credentials_file')
    if isinstance(declared, dict):
        relative = str(declared.get(name) or declared.get(name.lower()) or '')
    else:
        # One declared credential per provider: any placeholder in an auth template
        # resolves to it, so the file name never has to be repeated in code.
        relative = str(declared or '')
    if not relative:
        return ''
    try:
        return (registry.PROJECT_ROOT / relative).read_text(encoding='utf-8-sig').strip()
    except OSError:
        return ''


def _expand(text: Any, health: dict) -> str:
    return _PLACEHOLDER_RE.sub(lambda match: _credential(health, match.group(1)), str(text or ''))


def _auth_header(health: dict, template: Any) -> dict[str, str]:
    """``"bearer:{KEY_PLACEHOLDER}"`` -> the ``Authorization`` header, value and all."""
    text = _expand(template, health).strip()
    if not text or text.lower() in {'none', 'null'}:
        return {}
    scheme, separator, token = text.partition(':')
    if not separator:
        scheme, token = 'bearer', text
    scheme, token = scheme.strip(), token.strip()
    if not token:
        return {}
    canonical = {'bearer': 'Bearer', 'basic': 'Basic', 'token': 'Token'}
    return {'Authorization': f'{canonical.get(scheme.lower(), scheme.title())} {token}'}


def _base_url(health: dict, override: str | None) -> str:
    base = str(override or health.get('base_url') or '').strip().rstrip('/')
    return _expand(base, health)


def _probe_health(spec: dict, base_url_override: str | None = None) -> tuple[bool, list[dict[str, Any]]]:
    """Ask a provider's own declared endpoint whether it is serviceable.

    Dispatch is entirely on ``health.scheme``:

    * ``none`` / ``in_process_cli_probe`` — nothing is reachable over the network; the
      host process and its command line (checked by the caller) are the evidence, so
      this is a pass with an informational check, never a silent failure.
    * ``tcp`` — open ``health.{host,port}`` within ``health.timeout_s``.
    * ``http``/``https`` — iterate ``health.endpoints[]``, honouring ``method``, the
      ``auth`` template (resolved from ``health.credentials_file``) and ``true_when``
      key lists; a missing or falsy key fails *that* endpoint only.
    """
    health = dict(spec.get('health') or {})
    scheme = str(health.get('scheme') or 'none').strip().lower()
    if scheme in {'none', 'in_process_cli_probe'}:
        return True, [_check(f'health:{scheme}', True, 'no network endpoint declared', required=False)]
    timeout = float(health.get('timeout_s') or PROBE_TIMEOUT_S)
    if scheme == 'tcp':
        host = str(health.get('host') or '127.0.0.1')
        try:
            port = int(health.get('port') or 0)
        except (TypeError, ValueError):
            port = 0
        if not port:
            return False, [_check('health:tcp', False, 'NO_PORT_DECLARED')]
        try:
            with socket.create_connection((host, port), timeout=timeout):
                pass
        except OSError as exc:
            return False, [_check('health:tcp', False, f'{host}:{port} {type(exc).__name__}')]
        return True, [_check('health:tcp', True, f'{host}:{port}')]
    if scheme not in {'http', 'https'}:
        return False, [_check('health:scheme', False, f'UNSUPPORTED_HEALTH_SCHEME:{scheme}')]
    base = _base_url(health, base_url_override)
    if not base:
        return False, [_check('health:base_url', False, 'NO_BASE_URL_DECLARED')]
    endpoints = [row for row in health.get('endpoints') or [] if isinstance(row, dict)]
    if not endpoints:
        return True, [_check('health:endpoints', False, 'NO_ENDPOINTS_DECLARED', required=False)]
    checks: list[dict[str, Any]] = []
    ok = True
    for endpoint in endpoints:
        path = _expand(endpoint.get('path') or '/', health)
        method = str(endpoint.get('method') or 'get').upper()
        url = base + (path if path.startswith('/') else f'/{path}')
        name = f'health:{method.lower()} {path}'
        try:
            response = httpx.request(method, url, headers=_auth_header(health, endpoint.get('auth')), timeout=timeout)
        except Exception as exc:
            checks.append(_check(name, False, f'{type(exc).__name__}'))
            ok = False
            continue
        if response.status_code >= 400:
            checks.append(_check(name, False, f'HTTP_{response.status_code}'))
            ok = False
            continue
        wanted = [str(key) for key in endpoint.get('true_when') or []]
        if not wanted:
            checks.append(_check(name, True, f'HTTP_{response.status_code}'))
            continue
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            checks.append(_check(name, False, 'RESPONSE_NOT_JSON'))
            ok = False
            continue
        missing = [key for key in wanted if not body.get(key)]
        if missing:
            checks.append(_check(name, False, 'FALSY_OR_MISSING:' + ','.join(missing)))
            ok = False
            continue
        checks.append(_check(name, True, f'HTTP_{response.status_code}'))
    return ok, checks


def _argv_expectation(spec: dict) -> tuple[str | None, str | None]:
    """The ``(flag, value)`` pair this provider's command line must carry.

    Declared, not assumed: ``health.command_line_flag``/``expected_value`` first, then
    ``handler.cli_flag_value`` for the value and — for the flag name — the option that
    precedes that value in the row's own ``launch.cli_args``.  So no module here has to
    know which option word the pipeline happens to use.
    """
    health = dict(spec.get('health') or {})
    handler = dict(spec.get('handler') or {})
    value = str(health.get('expected_value') or handler.get('cli_flag_value') or spec.get('id') or '')
    flag = str(health.get('command_line_flag') or '')
    if not flag:
        args = [str(item) for item in ((spec.get('launch') or {}).get('cli_args') or [])]
        for index, token in enumerate(args):
            if token == value and index and str(args[index - 1]).startswith('--'):
                flag = args[index - 1]
                break
    return (flag or None), (value or None)


def _argv_check(spec: dict, command_line: list[str], *, selected: bool) -> dict[str, Any]:
    """Is the resident host actually carrying *this* provider?  Aliases included.

    Every in-process row shares the one pipeline host, so for those rows this command
    line is the *only* per-provider evidence.  That makes two outcomes different in
    kind, and the difference is what keeps "add a provider" safe:

    * nothing declared to look for — a manifest defect, permanent, and the row must
      not be believed to be resident.  Soft-pedalling it here is what let a freshly
      registered provider be counted as a second live instance and knock the whole
      page out of ``ready`` (任务 §0: registering a model may not break the system).
    * declared, but the command line could not be read — an observation failure.  The
      selected provider may still rest on the port-owner / pool evidence, so only the
      other rows are held to it.
    """
    flag, value = _argv_expectation(spec)
    if not flag or not value:
        return _check('argv', False, 'NO_COMMAND_LINE_FLAG_DECLARED')
    if not command_line:
        return _check('argv', False, 'PIPELINE_COMMAND_LINE_UNREADABLE', required=not selected)
    lowered = [str(token).lower() for token in command_line]
    try:
        at = lowered.index(flag.lower())
    except ValueError:
        return _check('argv', False, f'{flag} ABSENT')
    if at + 1 >= len(command_line):
        return _check('argv', False, f'{flag} WITHOUT VALUE')
    carried = str(command_line[at + 1])
    resolved = registry.spec_for_flag(KIND, carried)
    matches = carried == value or (resolved is not None and str(resolved.get('id')) == str(spec.get('id')))
    return _check('argv', matches, f'{flag}={carried}' if matches else f'{flag}={carried}, expected {value}')


# --------------------------------------------------------------------- readiness model

def _visible_rows(active: str | None) -> list[dict]:
    """Every row the page renders, plus the selected one if it happens to be hidden."""
    rows = [row for row in registry.providers(KIND, ui_only=True) if row.get('id')]
    ids = {str(row['id']) for row in rows}
    if active and active not in ids:
        extra = registry.get(KIND, active)
        if extra:
            rows.append(extra)
    return rows


def _row_verdict(spec: dict, *, workers_up: dict[str, bool], facts: dict,
                 active: str | None) -> dict[str, Any]:
    """One provider's readiness, decided by *its own* residency declaration.

    ``proven`` is the narrower question the aggregate invariant asks: has this row
    positively shown that its runtime instance exists?  An external worker answers it
    with its own pid; an in-process row answers it by appearing in the pipeline's
    command line.  ``ready`` stays the page-facing verdict, so a transient observation
    failure on the *selected* provider cannot take the panel down by itself.
    """
    provider_id = str(spec.get('id'))
    resident = str(spec.get('resident') or '')
    checks: list[dict[str, Any]] = []
    exclusive = resident.startswith('external_worker')
    proven = False
    if exclusive:
        worker = dict(spec.get('worker') or {})
        running = bool(workers_up.get(provider_id, False))
        proven = running
        checks.append(_check('worker', running, f"pid_key={worker.get('pid_key') or provider_id}"))
        if running:
            # Probing a port nobody owns costs the page a poll interval per dead
            # backend, and cannot succeed: the worker process is the precondition.
            checks.extend(_probe_health(spec)[1])
        else:
            checks.append(_check('health', True, 'SKIPPED_WORKER_NOT_RUNNING', required=False))
    elif resident.startswith('in_proc'):
        evidence = f"via={facts.get('pid_source') or 'none'} pid={facts.get('pid') or 0}"
        checks.append(_check('pipeline_process', facts['exe_ok'], f'{evidence}; ' + '; '.join(facts['notes'])))
        argv = _argv_check(spec, facts['command_line'], selected=(provider_id == active))
        proven = argv['ok']
        checks.append(argv)
        size = facts['pool'].get('size')
        checks.append(_check('pipeline_pool', size == PIPELINE_POOL_SIZE,
                             'size=unreadable' if size is None else f'size={size}'))
        checks.extend(_probe_health(spec)[1])
    else:
        checks.append(_check('resident', False, f"UNSUPPORTED_RESIDENT:{resident or 'unset'}"))
    foreign = sorted(other for other, up in workers_up.items()
                     if up and other != provider_id)
    checks.append(_check('sole_worker', not foreign,
                         'FOREIGN_WORKER_UP:' + ','.join(foreign) if foreign else 'one worker'))
    instance_up = all(row['ok'] for row in checks if row['required'] and row['check'] != 'sole_worker')
    return {
        'id': provider_id,
        'resident': resident,
        'instance_up': instance_up,
        'proven': bool(proven and instance_up),
        'ready': all(row['ok'] for row in checks if row['required']),
        'checks': checks,
    }


def _readiness() -> dict[str, Any]:
    """Per-provider verdicts plus the manifest's aggregate invariant.

    Ready means: a selection exists, *its* own checks pass, and the number of live
    resident instances is exactly what ``invariants.max_resident_provider_instances``
    allows — one voice at a time, whichever two providers that happens to be.

    The count is over *proven* instances, not over rows with nothing to contradict
    them: registering a provider must never be able to take the panel down, and rows
    that cannot evidence their own runtime are reported as unproven rather than live.
    """
    active, active_error = _active_pair()
    pids = _service_pids()
    facts = _pipeline_facts(pids)
    rows = _visible_rows(active)
    workers_up = {str(row['id']): _worker_running(row, pids) for row in rows}
    verdicts = {str(row['id']): _row_verdict(row, workers_up=workers_up, facts=facts, active=active)
                for row in rows}
    limit = int(registry.manifest_value(KIND, 'invariants', 'max_resident_provider_instances', default=1) or 1)
    up = sorted(provider_id for provider_id, verdict in verdicts.items() if verdict['proven'])
    aggregate: list[dict[str, Any]] = []
    if not active:
        aggregate.append(_check('selection', False, active_error or 'NO_ACTIVE_PROVIDER'))
    else:
        own = verdicts.get(active)
        if own is None:
            aggregate.append(_check('selection', False, f'{active} HAS_NO_MANIFEST_ROW'))
        else:
            aggregate.append(_check('selected_provider', own['ready'], f"{active}: see its own checks"))
    aggregate.append(_check('invariant', 0 < len(up) <= limit, f'resident={len(up)} limit={limit} up={",".join(up) or "none"}'))
    ready = all(row['ok'] for row in aggregate if row['required'])
    detail = [{'provider': entry['id'], **row} for entry in verdicts.values() for row in entry['checks']]
    detail[:0] = [{'provider': active, **row} for row in aggregate]
    return {
        'active': active,
        'active_error': active_error,
        'ready': bool(ready and active),
        'workers': {provider_id: bool(verdict['instance_up']) for provider_id, verdict in verdicts.items()},
        'residents_up': up,
        'limit': limit,
        'verdicts': verdicts,
        'health_detail': detail,
    }


# ------------------------------------------------------------------------ public shape

def capabilities(kind: str = KIND) -> dict[str, Any]:
    """The whole render contract for one provider kind: rows, controls, state schema.

    The browser draws this.  Nothing in it was written for a particular provider, so a
    new row appears — with its controls, labels, notes and limits — without a frontend
    change.
    """
    resolved = str(kind or '').strip().upper()
    if resolved not in registry.KINDS:
        return {'ok': False, 'reason': 'UNKNOWN_KIND', 'allowed': list(registry.KINDS)}
    return {
        'ok': True,
        'kind': resolved,
        'providers': _attach_current(registry.ui_schema(resolved)),
        'state': registry.state_schema(resolved),
    }


def models() -> list[dict[str, Any]]:
    """The switcher's option list: enabled, visible rows, labelled by the manifest."""
    names = labels()
    return [
        {'key': str(row['id']), 'label': names.get(str(row['id']), str(row['id']))}
        for row in registry.providers(KIND, enabled_only=True, ui_only=True)
    ]


def state() -> dict[str, Any]:
    """Everything the girlfriend page polls.  Same keys as before, plus the schema.

    ``switch`` is now the transaction journal itself: ``busy`` is derived from the
    phase (§5), ``result``/``message``/``detail`` are the terminal receipts, and the
    in-memory ``_state`` only contributes a live message while the journal has nothing
    fresher — it gates nothing any more.
    """
    with _lock:
        mem = dict(_state)
    journal = _read_switch_state()
    switch = {**mem, **journal}
    switch['busy'] = _derived_busy(journal)
    switch['phase'] = journal.get('phase') or PHASE_IDLE
    info = _readiness()
    schema = _attach_current(registry.ui_schema(KIND))
    return {
        'active': info['active'],
        'activeError': info['active_error'],
        'ready': info['ready'],
        'switch': switch,
        'emotion': emotion(),
        'activeMainTtsCount': int(bool(info['ready'] and info['active'])),
        'workers': info['workers'],
        'models': models(),
        'health_detail': info['health_detail'],
        'providers': schema,
        'capabilities': schema,
        'state': registry.state_schema(KIND),
    }


# ------------------------------------------------------------------------- switching

def _proc_facts(pid: int) -> dict:
    """(exists, create_time, cmdline) for a pid — query-only, never ``os.kill(pid, 0)``.

    Existence goes through ``psutil.pid_exists`` first and ``pid_liveness`` (the frozen
    PASS probe: query-rights handles only) as the fallback; identity fields need psutil.
    Unreadable identity is *not* invented here — the caller treats missing proof as
    "unverified", which for a non-terminal transaction means stale (§7).
    """
    facts: dict[str, Any] = {'exists': False, 'create_time': None, 'cmdline': []}
    if pid <= 0:
        return facts
    try:
        import psutil
        proc = psutil.Process(int(pid))
        facts['exists'] = bool(proc.is_running())
    except Exception:
        facts['exists'] = pid_liveness.pid_alive(int(pid))
        return facts
    try:
        facts['create_time'] = float(proc.create_time())
    except Exception:
        facts['create_time'] = None
    try:
        facts['cmdline'] = [str(item) for item in proc.cmdline()]
    except Exception:
        facts['cmdline'] = []
    return facts


def _child_identity_ok(journal: dict) -> bool:
    """§7 four conjuncts about the transaction's recorded child — ALL must be proven.

    PID exists, its CreationTime matches the recorded one (PID reuse detector), the
    command line is this executor, and the ``-Target`` it is running equals the
    transaction's target.  Any single failure — including "cannot be read" — means the
    phase is a corpse: stale, never a permanent refusal.
    """
    try:
        pid = int(journal.get('child_pid') or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return False
    facts = _proc_facts(pid)
    if not facts['exists']:
        return False
    try:
        recorded = float(journal.get('child_start_time') or 0)
    except (TypeError, ValueError):
        recorded = 0.0
    if not recorded or facts['create_time'] is None:
        return False
    if abs(facts['create_time'] - recorded) > 0.05:
        return False
    lowered = [item.lower() for item in facts['cmdline']]
    if str(SCRIPT).lower() not in ' '.join(lowered):
        return False
    try:
        at = lowered.index('-target')
    except ValueError:
        return False
    if at + 1 >= len(lowered):
        return False
    return str(lowered[at + 1]) == str(journal.get('target') or '').lower()


def _starting_claim_fresh(journal: dict) -> bool:
    """A STARTING claim without a child pid yet is believable for a bounded window."""
    if journal.get('phase') != PHASE_STARTING:
        return False
    try:
        started = datetime.fromisoformat(str(journal.get('started_at') or ''))
        age = (datetime.now().astimezone() - started).total_seconds()
    except (TypeError, ValueError):
        return False
    return 0 <= age <= STARTING_GRACE_S


def _committed_backend() -> str:
    """Authority A: the last successfully committed TTS (active_tts_backend.txt)."""
    try:
        return (NATIVE_ROOT / 'active_tts_backend.txt').read_text(encoding='ascii').strip()
    except OSError:
        return ''


def _runtime_backend() -> str | None:
    """What the live :8765 owner actually carries in its ``--tts`` fragment."""
    facts = _pipeline_facts(_service_pids())
    argv = [str(item) for item in facts.get('command_line') or []]
    if not argv:
        return None
    lowered = [item.lower() for item in argv]
    for row in registry.providers(KIND, enabled_only=True):
        flag, _value = _argv_expectation(row)
        if not flag:
            continue
        try:
            at = lowered.index(str(flag).lower())
        except ValueError:
            continue
        if at + 1 >= len(argv):
            return None
        resolved = registry.spec_for_flag(KIND, argv[at + 1])
        return str(resolved['id']) if resolved else argv[at + 1]
    return None


def _pipeline_healthy() -> bool:
    try:
        body = httpx.get(PIPELINE_POOL_URL, timeout=PROBE_TIMEOUT_S).json()
    except Exception:
        return False
    return isinstance(body, dict)


def _reconcile_stale(journal: dict) -> dict:
    """§8: a stale transaction is reconciled against the REAL runtime, never guessed.

    committed == runtime AND the pipeline answers → the transaction is declared
    ABORTED_STALE and the journal returns to IDLE; no service is restarted.  Anything
    else (including "runtime cannot be observed") fails closed with
    TTS_RUNTIME_STATE_MISMATCH — deciding which backend is right by hand is the
    explicit rollback path, not an automation.
    """
    committed = _committed_backend()
    runtime = _runtime_backend()
    healthy = _pipeline_healthy()
    detail = f'committed={committed or "unreadable"} runtime={runtime or "unobservable"} healthy={healthy}'
    if committed and runtime and committed == runtime and healthy:
        _journal_write(phase=PHASE_ABORTED_STALE, reason=REASON_STALE_RECONCILED,
                       result='', detail=detail, message='')
        _journal_write(phase=PHASE_IDLE, reason=REASON_STALE_RECONCILED,
                       result='', detail=detail, message='', child_pid=0, child_start_time='')
        return {'ok': True, 'committed': committed, 'runtime': runtime, 'healthy': healthy}
    return {'ok': False, 'reason': REASON_RUNTIME_MISMATCH,
            'committed': committed, 'runtime': runtime, 'healthy': healthy, 'detail': detail}


def _executor_command(target: str) -> list[str]:
    return [POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', str(SCRIPT), '-Target', target]


def _run(target: str) -> None:
    """Execute one switch child and guarantee a terminal journal phase (§9).

    The child's stdout/stderr go to temporary FILES, never pipes.  The executor
    spawns workers that inherit the handles; with pipes, a grandchild outliving the
    child kept the read side open forever and wedged this runner thread — the exact
    mechanism that used to make ``SWITCH_ALREADY_RUNNING`` permanent.  A file has no
    EOF to wait for, and ``wait(timeout)`` bounds everything else.

    Success/failure/timeout/exception/child-death all land in a terminal phase,
    decided from the return code + the stdout contract + the actual runtime — the
    ``finally`` block never blindly writes FAILED, it persists the verdict computed
    on the way through.
    """
    terminal, verdict, reason, output = PHASE_FAILED, 'SWITCH_FAILED', 'RUNNER_CRASHED', ''
    timed_out = False
    try:
        argv = _executor_command(target)
        with tempfile.TemporaryFile(mode='w+b') as out, tempfile.TemporaryFile(mode='w+b') as err:
            proc = subprocess.Popen(argv, stdout=out, stderr=err, cwd=str(NATIVE_ROOT))
            child_start_time = ''
            with contextlib.suppress(Exception):
                import psutil
                child_start_time = repr(psutil.Process(proc.pid).create_time())
            try:
                _journal_write(phase=PHASE_RUNNING, child_pid=int(proc.pid),
                               child_start_time=child_start_time)
            except (OSError, JournalWriteError):
                # The child exists and will run to its own conclusion; the journal
                # simply still says STARTING, which the stale path reconciles later.
                # Refusing to proceed here would orphan a running executor.
                pass
            try:
                rc = proc.wait(timeout=EXECUTOR_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait(timeout=60)
                timed_out = True
            out.seek(0), err.seek(0)
            output = (out.read() + err.read()).decode('utf-8', 'replace').strip()[-2000:]

        if timed_out:
            reason = 'EXECUTOR_TIMEOUT'
        elif rc == 0 and 'SWITCH_OK' in output:
            _journal_write(phase=PHASE_VERIFYING, message='正在核验运行态…')
            committed, runtime, healthy = (_committed_backend(), _runtime_backend(),
                                           _pipeline_healthy())
            if committed == target and runtime == target and healthy:
                terminal, verdict, reason = PHASE_COMMITTED, 'SWITCH_OK', ''
            else:
                reason = (f'POST_COMMIT_RUNTIME_MISMATCH committed={committed or "unreadable"} '
                          f'runtime={runtime or "unobservable"} healthy={healthy}')
        elif rc == 0 and 'ROLLBACK_OK' in output:
            committed = _committed_backend()
            if committed != target:
                terminal, verdict = PHASE_ROLLED_BACK, 'ROLLBACK_OK'
                reason = output[:200]
            else:
                reason = f'POST_ROLLBACK_STATE_MISMATCH committed={committed}'
        else:
            reason = output[:200] or f'EXECUTOR_RC={rc}'
    except Exception as exc:  # noqa: BLE001 — every crash still terminalizes below
        reason = f'{type(exc).__name__}: {exc}'[:200]
    finally:
        message = ({PHASE_COMMITTED: '切换完成', PHASE_ROLLED_BACK: '已回滚到原来的语音',
                    PHASE_FAILED: '切换失败'}.get(terminal, '切换失败'))
        try:
            _journal_write(phase=terminal, result=verdict, reason=reason, detail=output,
                           message=message)
        except Exception:
            # A journal that refuses updates must not leave a non-terminal phase
            # behind: fall back to a minimal terminal record, and if even the
            # filesystem is gone, say so loudly in the runtime log.
            try:
                _write_json_atomic(SWITCH_STATE_FILE, {**_blank_journal(),
                                                       'phase': PHASE_FAILED,
                                                       'result': verdict, 'reason': reason})
            except Exception:
                pass
        with _lock:
            _state.update(busy=False, result=verdict, detail=output, phase='', message=message)


def start_switch(target: str) -> dict:
    spec = registry.get(KIND, target)
    if spec is None or not spec.get('enabled'):
        return {'ok': False, 'reason': 'UNKNOWN_TARGET'}
    # Explicit UI selection may recover a stopped or invalid runtime state.
    # The lifecycle script remains the authority and refuses foreign listeners.
    try:
        pool = httpx.get(PIPELINE_POOL_URL, timeout=PROBE_TIMEOUT_S).json()
        if isinstance(pool, dict) and pool.get('in_use', 0):
            return {'ok': False, 'reason': 'BUSY'}
    except Exception:
        pass
    global _runner
    runner = threading.Thread(target=_run, args=(str(target),), daemon=True, name='tts-switch')
    with _lock:
        journal = _read_switch_state()
        if journal['phase'] in ACTIVE_PHASES:
            # §7: a non-terminal transaction refuses a second switch ONLY while its
            # recorded child is provably the running executor.  A fresh STARTING
            # claim (child not spawned yet) is believed for a bounded window;
            # everything else is stale and goes through the §8 reconcile, which
            # either unlocks (ABORTED_STALE → IDLE) or fails closed.
            if journal['phase'] == PHASE_STARTING and _starting_claim_fresh(journal):
                return {'ok': False, 'reason': 'SWITCH_ALREADY_RUNNING'}
            if _child_identity_ok(journal):
                return {'ok': False, 'reason': 'SWITCH_ALREADY_RUNNING'}
            receipt = _reconcile_stale(journal)
            if not receipt['ok']:
                return {'ok': False, 'reason': receipt['reason'], 'detail': receipt}
        previous = _active()
        message = (f"正在关闭 {labels().get(previous) or _PRESET_LABEL_FALLBACK}…" if previous
                   else '正在检查语音模型状态…')
        _state.update(
            busy=True, target=target, previous=previous, result='', detail='',
            phase=PHASE_STARTING, message=message,
        )
        _runner = runner
        try:
            _journal_write(switch_id=uuid.uuid4().hex, target=str(target), source='ui',
                           phase=PHASE_STARTING, started_at=_now_iso(),
                           child_pid=0, child_start_time='', result='', reason='',
                           detail='', message=message, previous=str(previous or ''))
        except (OSError, JournalWriteError) as exc:
            _runner = None
            _state.update(busy=False, result='SWITCH_FAILED', phase='', message='',
                          detail=f'STATE_WRITE_FAILED: {type(exc).__name__}'[:300])
            return {'ok': False, 'reason': 'STATE_WRITE_FAILED'}
    runner.start()
    return {'ok': True, 'target': target}
