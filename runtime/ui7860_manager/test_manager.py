"""Own visible lab consoles only; never import or control production services."""
import asyncio
import copy
import json
from pathlib import Path
import socket
import sys
import threading
import time
import urllib.request
from types import SimpleNamespace

import manager as m

RESULTS = []
OUT = m.PROJECT / 'logs/isolated_manager_tests'
OUT.mkdir(parents=True, exist_ok=True)


def check(name, condition):
    RESULTS.append({'test': name, 'pass': bool(condition)})
    print(('PASS ' if condition else 'FAIL ') + name, flush=True)


def reject(name, function, reason):
    try:
        function()
    except Exception as exc:
        check(name, reason in str(exc))
    else:
        check(name, False)


def fetch(port, path, data=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{port}{path}', data=data, timeout=5) as r:
        return json.load(r)


def wait_idle(record_path):
    for _ in range(40):
        state = m.control(record_path, 'status')
        if state['active'] == 0:
            return state
        time.sleep(.05)
    raise RuntimeError('LAB_NOT_IDLE')


def main():
    key = 'ab' * 32
    good = m.signed({'a': 1}, key)
    check('HMAC roundtrip', m.verify(good, key) == {'a': 1})
    reject('wrong auth rejected', lambda: m.verify(good, 'cd' * 32), 'AUTH_FAILED')
    reject('tampered message rejected', lambda: m.verify({**good, 'a': 2}, key), 'AUTH_FAILED')
    reject('unknown controller action never sends stop', lambda: m.control('unused', 'restart'), 'INVALID_ACTION')
    admission = m.Admission(None)
    server = SimpleNamespace(started=False, should_exit=False)
    check('not ready refuses stop', m.request_stop(admission, server)[1] == 'NOT_READY')
    server.started = True
    check('external busy leaves service open', m.request_stop(admission, server, lambda: False)[1] ==
          'EXTERNAL_ACTIVITY_UNCONFIRMED' and not admission.draining and not server.should_exit)
    def broken_probe():
        raise OSError('isolated probe failure')
    check('external probe error fails closed', m.request_stop(admission, server, broken_probe)[1] ==
          'EXTERNAL_ACTIVITY_UNCONFIRMED' and not admission.draining and not server.should_exit)
    entered, release = threading.Event(), threading.Event()
    def probe_wait():
        entered.set()
        release.wait(2)
        return True
    decision = []
    worker = threading.Thread(target=lambda: decision.append(m.request_stop(admission, server, probe_wait)))
    worker.start()
    entered.wait(2)
    messages = []
    async def send(msg):
        messages.append(msg)
    asyncio.run(admission({'type': 'http'}, None, send))
    check('new request denied during external idle check', messages[0]['status'] == 503 and admission.active == 0)
    check('concurrent stop refused while draining', m.request_stop(admission, server)[1] == 'ALREADY_DRAINING')
    release.set()
    worker.join(3)
    check('idle confirmation requests only server graceful exit', decision == [(True, '')] and server.should_exit)
    source = (m.HERE / 'manager.py').read_text(encoding='utf-8')
    check('no process terminate or console broadcast code', all(term not in source for term in (
        'TerminateProcess', 'GenerateConsoleCtrlEvent', 'AttachConsole', '.kill(', '.terminate(',
        'taskkill', 'CTRL_BREAK_EVENT', 'CTRL_C_EVENT')))

    # Single test port, distinct from all production ports.
    port = 17860
    state_dir = m.HERE / 'lab_state' / ('run_' + m.uuid.uuid4().hex)
    started = m.start('lab', port, state_dir)
    record_path = state_dir / 'record.json'
    record = json.loads(record_path.read_text(encoding='utf-8'))
    public = {k: v for k, v in record.items() if k != 'key'}
    m.atomic_json(OUT / 'lab_registration_redacted.json', public)
    check('visible-console lab ready', started.get('ok') and started.get('ready'))
    check('health serves inert fixture', fetch(port, '/health').get('fixture') is True)
    check('runtime import fingerprint recorded', 'fixture_app' in started.get('loaded', {}))
    check('actual loaded function code recorded', bool(started['loaded']['fixture_app']['loaded_function_code'].get('health')))
    check('own interpreter identity present', bool(record['identity']['executable']) and
          bool(record['identity']['creation_ticks']))
    print('LAB_READY_FOR_WINDOW_AUDIT', flush=True)
    # Main agent reads windows while this isolated test is awaiting a file marker.
    audit_gate = OUT / 'window_audit_done'
    for _ in range(200):
        if audit_gate.exists():
            break
        time.sleep(.1)
    try:
        reject('duplicate listener refused without console',
               lambda: m.start('lab', port, m.HERE / 'lab_state' / 'duplicate'), '10048')
        spec = record['spec']
        for protected in (7860, 8010, 8090, 8765):
            bad = {**spec, 'port': protected}
            reject(f'lab refuses production port {protected}', lambda b=bad: m.validate_spec(b),
                   'LAB_SCOPE' if protected == 7860 else 'PROTECTED')
        reject('foreign project refused', lambda: m.validate_spec({**spec, 'project': 'D:/elsewhere'}),
               'FOREIGN_PROJECT')
        reject('foreign runner refused', lambda: m.validate_spec({**spec, 'runner': 'D:/foreign.py'}),
               'FOREIGN_RUNNER')
        prod = {**spec, 'mode': 'production', 'port': 7860,
                'app_dir': str(m.PROJECT / 'runtime/ui/app'), 'app': 'server:app'}
        reject('production requires explicit maintenance flag', lambda: m.validate_spec(prod),
               'PRODUCTION_MAINTENANCE_GATE')
        for field, value in (('creation_ticks', 1), ('executable', 'D:/foreign.exe')):
            bad_record = copy.deepcopy(record)
            bad_record['identity'][field] = value
            fake = state_dir / ('bad_' + field + '.json')
            m.atomic_json(fake, bad_record)
            reject('PID reuse guard:' + field, lambda f=fake: m.control(f, 'stop'), 'PID_REUSED')
        bad_record = copy.deepcopy(record)
        bad_record['identity']['run_id'] = 'stale-run'
        fake = state_dir / 'bad_run.json'
        m.atomic_json(fake, bad_record)
        reject('stale run rejected locally', lambda: m.control(fake, 'stop'), 'RECORD_SPEC_MISMATCH')
        reject('wrong expected peer rejected', lambda: m.exchange(record, 'stop',
               {**record['identity'], 'run_id': 'old'}), 'TRUNCATED_MESSAGE')
        check('identity failures leave own lab alive', m.control(record_path, 'status')['ready'])

        # Auth failure and signed replay do not change server state.
        def raw(payload):
            with socket.create_connection(('127.0.0.1', record['control_port']), timeout=2) as s:
                s.settimeout(2)
                s.sendall(m.canonical(payload) + b'\n')
                return m.receive(s)
        request = m.signed({'action': 'status', 'expected': record['identity'],
                            'nonce': 'ef' * 16, 'at': time.time()}, record['key'])
        check('authenticated request accepted', m.verify(raw(request), record['key'])['ok'])
        reject('replay rejected', lambda: raw(request), 'TRUNCATED_MESSAGE')
        invalid = {**request, 'mac': '00' * 32}
        reject('invalid MAC rejected', lambda: raw(invalid), 'TRUNCATED_MESSAGE')
        check('bad auth never starts drain', not m.control(record_path, 'status')['draining'])

        # Occupied upload/CPU request: stop must refuse, not cancel it.
        upload_result = []
        thread = threading.Thread(target=lambda: upload_result.append(fetch(port, '/slow-upload', b'audio-fixture')))
        thread.start()
        for _ in range(40):
            if m.control(record_path, 'status')['active']:
                break
            time.sleep(.025)
        refused = m.control(record_path, 'stop', .2)
        check('active upload blocks stop', refused.get('reason') == 'BUSY' and not refused['draining'])
        thread.join(5)
        check('refused stop preserves in-flight request', upload_result == [{'ok': True}])

        from websockets.sync.client import connect
        with connect(f'ws://127.0.0.1:{port}/ws', proxy=None) as ws:
            refused = m.control(record_path, 'stop', .2)
            check('active websocket blocks stop', refused.get('reason') == 'BUSY')
            ws.send('done')
        wait_idle(record_path)

        # Drain timeout: no force, process and control channel remain alive.
        hold = m.HERE / 'hold_shutdown'
        hold.write_text('isolated lifecycle delay', encoding='utf-8')
        timed = m.control(record_path, 'stop', .2)
        check('shutdown timeout returns HOLD without force', timed.get('reason') == 'GRACEFUL_TIMEOUT_NO_FORCE')
        status = m.control(record_path, 'status')
        check('timeout leaves authenticated process alive', status['draining'] and status['active'] == 0)
        hold.unlink()
        k, handle = m.process_handle(record['identity']['pid'])
        try:
            check('released shutdown exits naturally', k.WaitForSingleObject(handle, 5000) == 0)
        finally:
            k.CloseHandle(handle)
        check('registration ends STOPPED', json.loads(record_path.read_text())['phase'] == 'STOPPED')
        reject('stale registration cannot stop reused target', lambda: m.control(record_path, 'stop'),
               'PROCESS_QUERY_DENIED_OR_GONE')

        # One normal lifecycle proves acknowledged stop and process exit.
        second = m.HERE / 'lab_state' / ('run_' + m.uuid.uuid4().hex)
        fresh = m.start('lab', port, second)
        check('next run gets different UUID', fresh['identity']['run_id'] != record['identity']['run_id'])
        check('normal stop reports actual process exit', m.control(second / 'record.json', 'stop', 5).get('verdict') == 'STOPPED')
        check('lab port released', socket.socket().connect_ex(('127.0.0.1', port)) != 0)
    finally:
        (m.HERE / 'hold_shutdown').unlink(missing_ok=True)
        # Cleanup only our authenticated registrations, only the same graceful channel.
        for rp in (m.HERE / 'lab_state').glob('run_*/record.json'):
            try:
                m.control(rp, 'stop', 5)
            except Exception:
                pass
        m.atomic_json(OUT / 'manager_test_results.json', RESULTS)
    print('RESULT', sum(r['pass'] for r in RESULTS), '/', len(RESULTS), flush=True)
    return 0 if all(r['pass'] for r in RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())
