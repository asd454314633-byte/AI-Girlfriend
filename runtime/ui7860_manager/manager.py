"""Authenticated single-process lifecycle, Windows, no kill fallback."""
from __future__ import annotations

import argparse
import csv
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import importlib
import inspect
import json
import marshal
import os
from pathlib import Path
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
SERVICE = 'AI_GIRLFRIEND_UI_7860_V1'
PROTECTED = {8010, 8090, 8765}
KEY_FIELDS = ('service', 'project', 'mode', 'port', 'run_id', 'pid', 'creation_ticks',
              'executable', 'runner', 'runner_sha256', 'app_dir', 'app', 'argv')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_bytes(canonical(value))
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def signed(value, key):
    return {**value, 'mac': hmac.new(bytes.fromhex(key), canonical(value), 'sha256').hexdigest()}


def verify(value, key):
    if not isinstance(value, dict):
        raise ValueError('INVALID_MESSAGE')
    value = dict(value)
    mac = value.pop('mac', '')
    if not isinstance(mac, str) or not hmac.compare_digest(
            mac, hmac.new(bytes.fromhex(key), canonical(value), 'sha256').hexdigest()):
        raise ValueError('AUTH_FAILED')
    return value


def native():
    if os.name != 'nt':
        raise RuntimeError('WINDOWS_ONLY')
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                           wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    return k


def process_handle(pid):
    k = native()
    handle = k.OpenProcess(0x1000 | 0x100000, False, int(pid))  # query-limited + synchronize
    if not handle:
        raise OSError(ctypes.get_last_error(), 'PROCESS_QUERY_DENIED_OR_GONE')
    return k, handle


def process_identity(k, handle):
    times = [wintypes.FILETIME() for _ in range(4)]
    if not k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        raise OSError(ctypes.get_last_error(), 'PROCESS_TIMES_FAILED')
    buf = ctypes.create_unicode_buffer(32768)
    size = wintypes.DWORD(len(buf))
    if not k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
        raise OSError(ctypes.get_last_error(), 'PROCESS_PATH_FAILED')
    return {'creation_ticks': (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
            'executable': str(Path(buf.value).resolve())}


def validate_spec(spec):
    expected_service = 'AI_GIRLFRIEND_LIVE_8781_V1' if spec.get('mode') == 'live' else SERVICE
    if spec.get('service') != expected_service or Path(spec.get('project', '')).resolve() != PROJECT:
        raise ValueError('FOREIGN_PROJECT_OR_SERVICE')
    port = spec.get('port')
    if not isinstance(port, int) or port in PROTECTED:
        raise ValueError('PROTECTED_OR_INVALID_PORT')
    if Path(spec.get('runner', '')).resolve() != Path(__file__).resolve():
        raise ValueError('FOREIGN_RUNNER')
    if spec.get('mode') == 'production':
        if (port != 7860 or spec.get('maintenance_confirmed') is not True or
                Path(spec.get('app_dir', '')).resolve() != PROJECT / 'runtime/ui/app' or
                spec.get('app') != 'server:app'):
            raise ValueError('PRODUCTION_MAINTENANCE_GATE')
    elif spec.get('mode') == 'live':
        if (port != 8781 or Path(spec.get('app_dir', '')).resolve() != PROJECT or
                spec.get('app') != 'runtime.live.service:app'):
            raise ValueError('LIVE_SCOPE_VIOLATION')
    elif spec.get('mode') == 'lab':
        if (not 1024 < port < 65536 or port == 7860 or
                Path(spec.get('app_dir', '')).resolve() != HERE or
                spec.get('app') != 'fixture_app:app'):
            raise ValueError('LAB_SCOPE_VIOLATION')
    else:
        raise ValueError('INVALID_MODE')
    return spec


def secure_directory(path):
    """Create an owner-only directory BEFORE writing any authentication material."""
    path = Path(path).resolve()
    # Never change ACLs outside this isolated candidate tree.
    path.relative_to(HERE)
    path.mkdir(parents=True, exist_ok=True)
    who = subprocess.run(['whoami.exe', '/user', '/fo', 'csv', '/nh'],
                         capture_output=True, text=True, check=True)
    sid = next(csv.reader(who.stdout.splitlines()))[-1]
    if not sid.startswith('S-1-'):
        raise ValueError('OWNER_SID_UNAVAILABLE')
    subprocess.run(['icacls.exe', str(path), '/inheritance:r', '/grant:r',
                    '*' + sid + ':(OI)(CI)F'], capture_output=True, check=True)
    return path


def receive(stream):
    data = b''
    while not data.endswith(b'\n'):
        part = stream.recv(4096)
        if not part:
            raise ValueError('TRUNCATED_MESSAGE')
        data += part
        if len(data) > 32768:
            raise ValueError('MESSAGE_TOO_LARGE')
    return json.loads(data)


def exchange(record, action, expected=None, timeout=3):
    nonce = secrets.token_hex(16)
    request = signed({'action': action, 'expected': expected or record['identity'],
                      'nonce': nonce, 'at': time.time()}, record['key'])
    with socket.create_connection(('127.0.0.1', int(record['control_port'])), timeout=timeout) as conn:
        conn.settimeout(timeout)
        conn.sendall(canonical(request) + b'\n')
        response = verify(receive(conn), record['key'])
    if response.get('nonce') != nonce or response.get('identity') != record['identity']:
        raise ValueError('CONTROL_PEER_IDENTITY_MISMATCH')
    return response


def validate_record(record):
    spec = validate_spec(record['spec'])
    identity = record['identity']
    for key in ('service', 'project', 'mode', 'port', 'run_id', 'runner', 'app_dir', 'app'):
        if identity.get(key) != spec.get(key):
            raise ValueError('RECORD_SPEC_MISMATCH:' + key)
    if identity.get('runner_sha256') != digest(__file__):
        raise ValueError('RUNNER_VERSION_MISMATCH')
    if record.get('control_host') != '127.0.0.1':
        raise ValueError('CONTROL_NOT_LOOPBACK')
    if not isinstance(record.get('control_port'), int) or not 1024 < record['control_port'] < 65536:
        raise ValueError('INVALID_CONTROL_PORT')
    return identity


def control(record_path, action, timeout=15):
    if action not in ('status', 'stop'):
        raise ValueError('INVALID_ACTION')
    record = json.loads(Path(record_path).read_text(encoding='utf-8'))
    identity = validate_record(record)
    k, handle = process_handle(identity['pid'])
    try:
        observed = process_identity(k, handle)
        if any(observed[key] != identity[key] for key in observed):
            raise ValueError('PID_REUSED_OR_EXECUTABLE_CHANGED')
        if k.WaitForSingleObject(handle, 0) == 0:
            raise ValueError('STALE_EXITED_PROCESS')
        # First authenticate the peer; no stop request until both identities agree.
        status = exchange(record, 'status')
        if not status.get('ok'):
            raise ValueError('STATUS_REFUSED')
        if action == 'status':
            return status
        answer = exchange(record, 'stop', timeout=10)
        if not answer.get('ok'):
            return answer
        wait = k.WaitForSingleObject(handle, int(max(0, timeout) * 1000))
        if wait != 0:
            return {'ok': False, 'verdict': 'HOLD', 'reason': 'GRACEFUL_TIMEOUT_NO_FORCE',
                    'identity': identity}
        return {'ok': True, 'verdict': 'STOPPED', 'identity': identity}
    finally:
        k.CloseHandle(handle)


class Admission:
    def __init__(self, app):
        self.app, self.lock = app, threading.Lock()
        self.active, self.draining = 0, False

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        with self.lock:
            refused = self.draining
            if not refused:
                self.active += 1
        if refused:
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1013})
            else:
                await send({'type': 'http.response.start', 'status': 503, 'headers': []})
                await send({'type': 'http.response.body', 'body': b'SERVICE_DRAINING'})
            return
        try:
            await self.app(scope, receive, send)
        finally:
            with self.lock:
                self.active -= 1


def request_stop(admission, server, idle_check=None):
    # Freeze admission atomically; a new upload cannot enter between idle check
    # and should_exit. If a check fails, reopen admission and leave server alive.
    with admission.lock:
        if not server.started:
            return False, 'NOT_READY'
        if admission.draining:
            return False, 'ALREADY_DRAINING'
        if admission.active:
            return False, 'BUSY'
        admission.draining = True
    if idle_check is not None:
        try:
            idle = idle_check()
        except Exception:
            idle = False
        if not idle:
            with admission.lock:
                admission.draining = False
            return False, 'EXTERNAL_ACTIVITY_UNCONFIRMED'
    server.should_exit = True
    return True, ''


def production_idle():
    """Fail closed; the runner census also covers uploads, ASR, preview and WS."""
    import urllib.request
    import models_admin
    journal = json.loads((PROJECT / 'runtime/windows/tts_switch_state.json').read_text(encoding='utf-8'))
    if (models_admin._state_snapshot().get('busy') is not False or
            journal.get('phase') not in ('IDLE', 'COMMITTED', 'ROLLED_BACK')):
        return False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8765/v1/pool', timeout=3) as r:
        pool = json.load(r)
    with opener.open('http://127.0.0.1:8765/v1/speak/status', timeout=3) as r:
        speech = json.load(r)
    return (pool.get('in_use') == 0 and pool.get('size', 0) > 0 and
            all(x.get('state') == 'idle' for x in pool.get('units', [])) and
            speech.get('pending_queue_items') == 0 and
            speech.get('response_open') in (False, None) and speech.get('unit_attached') is False)


def run(spec_path):
    import asyncio
    import uvicorn
    spec = validate_spec(json.loads(Path(spec_path).read_text(encoding='utf-8')))
    state_dir = Path(spec_path).resolve().parent
    state_dir.relative_to(HERE)
    # Reserve the port before importing any app. No shared production server is touched.
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    listener.bind(('127.0.0.1', spec['port']))
    listener.listen(128)
    os.chdir(spec['app_dir'])
    sys.path.insert(0, spec['app_dir'])
    if spec['mode'] == 'production':
        os.environ['SPEECH_TO_SPEECH_URL'] = 'http://127.0.0.1:8765'
        os.environ['S2S_SPEAK_BASE'] = 'http://127.0.0.1:8765'
    module, attr = spec['app'].split(':')
    imported = importlib.import_module(module)
    admission = Admission(getattr(imported, attr))
    server = uvicorn.Server(uvicorn.Config(admission, host='127.0.0.1', port=spec['port'],
                            log_level='info', timeout_graceful_shutdown=None))
    k, handle = process_handle(os.getpid())
    try:
        native_id = process_identity(k, handle)
    finally:
        k.CloseHandle(handle)
    identity = {key: spec[key] for key in ('service', 'project', 'mode', 'port', 'run_id',
                                          'runner', 'app_dir', 'app')}
    identity.update(native_id, pid=os.getpid(), runner_sha256=digest(__file__), argv=sys.argv)
    key = secrets.token_hex(32)
    def module_receipt(m):
        return {'file': str(Path(m.__file__).resolve()), 'sha256_at_import': digest(m.__file__),
                'loaded_function_code': {
                    name: hashlib.sha256(marshal.dumps(inspect.unwrap(value).__code__)).hexdigest()
                    for name, value in vars(m).items()
                    if inspect.isfunction(value) and value.__module__ == m.__name__}}
    loaded = {module: module_receipt(imported)}
    for name in ('reading_admin', 'voice_library_admin'):
        if name in sys.modules:
            m = sys.modules[name]
            loaded[name] = module_receipt(m)
    title = f"AI Girlfriend {spec['port']} http://127.0.0.1:{spec['port']} [{spec['mode']}]"
    k.SetConsoleTitleW.argtypes = [wintypes.LPCWSTR]
    k.SetConsoleTitleW(title)
    print(title, '\nRun:', spec['run_id'], '\nPID:', os.getpid(), flush=True)
    print('Live stdout/stderr. Graceful stop uses authenticated control only.', flush=True)
    seen = set()
    record_path = state_dir / 'record.json'

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            try:
                request = verify(receive(self.request), key)
                nonce = request.get('nonce')
                if not isinstance(nonce, str) or len(nonce) != 32:
                    raise ValueError('INVALID_NONCE')
                if abs(time.time() - float(request.get('at', 0))) > 10:
                    raise ValueError('EXPIRED_REQUEST')
                with admission.lock:
                    if nonce in seen:
                        raise ValueError('REPLAY_REQUEST')
                    if len(seen) > 4096:
                        seen.clear()
                    seen.add(nonce)
                if request.get('expected') != identity:
                    raise ValueError('EXPECTED_IDENTITY_MISMATCH')
                action = request.get('action')
                if action not in ('status', 'stop'):
                    raise ValueError('INVALID_ACTION')
                ok, reason = True, ''
                if action == 'stop':
                    idle_check = production_idle if spec['mode'] == 'production' else None
                    if spec['mode'] == 'live':
                        idle_check = lambda: not imported.router.enabled and not imported.router.inflight
                    ok, reason = request_stop(admission, server, idle_check)
                with admission.lock:
                    response = {'ok': ok, 'reason': reason, 'identity': identity,
                                'nonce': nonce, 'active': admission.active, 'draining': admission.draining,
                                'ready': server.started, 'loaded': loaded, 'title': title}
                self.request.sendall(canonical(signed(response, key)) + b'\n')
            except Exception as exc:
                # No unauthenticated state or key disclosure.
                print('Control refused:', type(exc).__name__, str(exc), flush=True)

    class ControlServer(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = False

    control_server = ControlServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=control_server.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
    thread.start()
    record = {'spec': spec, 'identity': identity, 'key': key, 'control_host': '127.0.0.1',
              'control_port': control_server.server_address[1], 'phase': 'STARTING', 'loaded': loaded}
    atomic_json(record_path, record)
    try:
        async def serve_managed():
            task = asyncio.create_task(server.serve(sockets=[listener]))
            while not server.started and not task.done():
                await asyncio.sleep(.05)
            if server.started:
                record['phase'] = 'RUNNING'
                atomic_json(record_path, record)
            await task
        asyncio.run(serve_managed())
        record['phase'] = 'STOPPED' if server.started else 'START_FAILED'
    finally:
        control_server.shutdown()
        control_server.server_close()
        listener.close()
        atomic_json(record_path, record)
    print('Server finished:', record['phase'], 'run=', spec['run_id'], flush=True)
    if record['phase'] != 'STOPPED':
        raise RuntimeError('START_FAILED')


def start(mode, port, state, maintenance=False):
    spec = {'service': 'AI_GIRLFRIEND_LIVE_8781_V1' if mode == 'live' else SERVICE,
            'project': str(PROJECT), 'mode': mode, 'port': port,
            'run_id': uuid.uuid4().hex, 'runner': str(Path(__file__).resolve()),
            'maintenance_confirmed': maintenance,
            'app_dir': str(PROJECT / 'runtime/ui/app' if mode == 'production' else PROJECT if mode == 'live' else HERE),
            'app': 'server:app' if mode == 'production' else 'runtime.live.service:app' if mode == 'live' else 'fixture_app:app'}
    validate_spec(spec)
    # Refuse existing listener before creating a console or importing an app.
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(('127.0.0.1', port))
    state_dir = secure_directory(state)
    if (state_dir / 'record.json').exists() or (state_dir / 'spec.json').exists():
        raise ValueError('STATE_EXISTS_USE_NEW_RUN_DIRECTORY')
    spec_path = state_dir / 'spec.json'
    # Exclusive creation also arbitrates concurrent controllers using this directory.
    with spec_path.open('x', encoding='utf-8') as f:
        json.dump(spec, f)
    powershell = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
    # Optional single-console mode: when runtime/windows/v3_console_mode.json
    # declares {"mode": "background"}, the console host runs hidden and its
    # stdout/stderr stream to rotated log files instead of a visible window.
    # Missing/unreadable file keeps the historical visible-console behavior.
    mode_path = PROJECT / 'runtime/windows/v3_console_mode.json'
    try:
        background = json.loads(mode_path.read_text(encoding='utf-8')).get('mode') == 'background'
    except FileNotFoundError:
        background = False
    except Exception as exc:
        raise RuntimeError(f'V3_CONSOLE_MODE_CONFIG_INVALID: {exc}') from exc
    args = [powershell, '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', str(HERE / 'Console.ps1'), '-Python', sys.executable,
            '-Runner', str(Path(__file__).resolve()), '-Spec', str(spec_path)]
    if background:
        log_dir = PROJECT / 'logs/runtime'
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path, stderr_path = log_dir / 'ui7860.stdout.log', log_dir / 'ui7860.stderr.log'
        for path in (stdout_path, stderr_path):
            if path.exists():
                stamp = time.strftime('%Y%m%d-%H%M%S')
                path.replace(path.with_name(f'{path.name}.{stamp}.log'))
                archives = sorted(path.parent.glob(path.name + '.*.log'), key=lambda p: p.stat().st_mtime, reverse=True)
                for stale in archives[10:]:
                    stale.unlink(missing_ok=True)
        args.append('-Background')
        # Redirect directly to files, never to a pipe this controller would have
        # to drain; the UI server stays independent of the console host.
        with stdout_path.open('ab', buffering=0) as stdout, stderr_path.open('ab', buffering=0) as stderr:
            subprocess.Popen(args, cwd=str(HERE), creationflags=subprocess.CREATE_NO_WINDOW,
                             stdout=stdout, stderr=stderr, close_fds=True)
    else:
        subprocess.Popen(args, cwd=str(HERE), creationflags=subprocess.CREATE_NEW_CONSOLE)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        record_path = state_dir / 'record.json'
        if record_path.exists():
            result = control(record_path, 'status')
            if result.get('ready'):
                return {'ok': True, 'record': str(record_path), **result}
        time.sleep(.1)
    return {'ok': False, 'verdict': 'HOLD', 'reason': 'START_TIMEOUT_NO_FORCE', 'state': str(state_dir)}


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='action', required=True)
    s = sub.add_parser('start')
    s.add_argument('--mode', choices=['lab', 'production', 'live'], default='lab')
    s.add_argument('--port', type=int, default=17860)
    s.add_argument('--state', type=Path, required=True)
    s.add_argument('--maintenance-confirmed', action='store_true')
    r = sub.add_parser('run')
    r.add_argument('--spec', required=True)
    for name in ('status', 'stop'):
        c = sub.add_parser(name)
        c.add_argument('--record', required=True)
        c.add_argument('--timeout', type=float, default=15)
    args = p.parse_args()
    try:
        if args.action == 'run':
            run(args.spec)
            return
        if args.action == 'start':
            answer = start(args.mode, args.port, args.state, args.maintenance_confirmed)
        else:
            answer = control(args.record, args.action, args.timeout)
        print(json.dumps(answer, ensure_ascii=False))
        if not answer.get('ok'):
            raise SystemExit(2)
    except Exception as exc:
        print(json.dumps({'ok': False, 'verdict': 'HOLD', 'reason': str(exc)}, ensure_ascii=False))
        raise SystemExit(2)


if __name__ == '__main__':
    main()
