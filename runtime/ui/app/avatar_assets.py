"""Recoverable asset deletion only. Never start/stop/switch an avatar process."""
from __future__ import annotations
import contextlib
import copy
import ctypes
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse
import httpx
from fastapi import Request
from pydantic import BaseModel


class DeleteRequest(BaseModel):
    编号: str = ""
    确认: bool = False
    校验: str = ""


class Refused(Exception):
    def __init__(self, code, note):
        self.code, self.note = code, note


@contextlib.contextmanager
def lifecycle_lock():
    """Use the Supervisor's existing mutex; its lifecycle code is untouched."""
    if os.name != 'nt':
        yield
        return
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    api.CreateMutexW.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.ReleaseMutex.argtypes = [wintypes.HANDLE]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.CreateMutexW(None, False, 'Global\\Avatar8010Supervisor')
    if not handle:
        handle = api.CreateMutexW(None, False, 'Avatar8010Supervisor')
    if not handle:
        raise Refused('LIFECYCLE_LOCK_UNAVAILABLE', '暂时无法核对运行状态，请稍后再试。')
    held = False
    try:
        held = api.WaitForSingleObject(handle, 0) in (0, 0x80)
        if not held:
            raise Refused('LIFECYCLE_BUSY', '正在准备或切换人物，请稍后再试。')
        yield
    finally:
        if held: api.ReleaseMutex(handle)
        api.CloseHandle(handle)


def strict_json(path, optional=False):
    if optional and not path.exists(): return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict): raise ValueError()
        return data
    except (OSError, ValueError):
        raise Refused('STATE_UNREADABLE', '人物资料或运行状态暂时读不完整，这次没有删除。')


def revision(entry):
    return hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate(admin, data, handle):
    rows = data.get('avatars')
    if not isinstance(rows, list):
        raise Refused('REGISTRY_INVALID', '人物资料暂时读不完整，这次没有删除。')
    found = [e for e in rows if isinstance(e, dict) and e.get('编号') == handle]
    if len(found) != 1:
        raise Refused('ASSET_NOT_FOUND', '没有找到这个人物，请刷新列表。')
    entry = found[0]; ident = str(entry.get('id') or '')
    if entry.get('builtin') is not False:
        raise Refused('BUILTIN_PROTECTED', '默认保留的人物不能删除。')
    if not re.fullmatch(r'[A-Za-z0-9_-]{4,64}', ident):
        raise Refused('UNSAFE_PATH', '素材位置不正确，这次没有删除。')
    if entry.get('state') == admin.STATE_MAKING:
        raise Refused('MAKING_BUSY', '这个人物还在制作，暂时不能删除。')
    pointer = admin._read_pointer()
    if not pointer:
        raise Refused('SELECTION_UNKNOWN', '暂时无法核对正在使用的人物，这次没有删除。')
    runtime = strict_json(admin.AVATAR_RUNTIME_STATE, optional=True)
    switch = strict_json(admin.SWITCH_STATE, optional=True)
    if switch.get('busy') or admin._switch_state.get('busy') or admin._runner_alive():
        raise Refused('SWITCH_BUSY', '正在切换人物，等切换结束后再删除。')
    if str(runtime.get('state', '')).upper() in ('STARTING', 'STOPPING', 'SWITCHING') or runtime.get('desired_avatar_id'):
        raise Refused('LIFECYCLE_BUSY', '正在准备或切换人物，请稍后再试。')
    protected = {pointer, admin._runtime_avatar_id()}
    # Even a stale ledger is conservatively protected; no destructive guessing.
    if str(runtime.get('state', '')).upper() not in ('STOPPED', 'FAILED', ''):
        protected.add(runtime.get('avatar_id'))
    if admin._making_slot is not None:
        raise Refused('MAKING_BUSY', '还有人物正在制作，等它做完后再删除。')
    if admin._port_open():
        try:
            with httpx.Client(trust_env=False, timeout=2) as client:
                live = client.get(admin._avatar_base_url() + '/api/admin/runtime')
                tasks = client.get(admin._avatar_base_url() + '/api/avatar/tasks')
            live.raise_for_status(); tasks.raise_for_status()
            observed, task_payload = live.json(), tasks.json()
            if observed.get('code') != 0 or task_payload.get('code') != 0: raise ValueError()
            actual = observed['data']['avatar_id']
            if not actual: raise ValueError()
            protected.add(actual)
            task_rows = task_payload['data']
            if isinstance(task_rows, dict): task_rows = task_rows.get('tasks')
            if not isinstance(task_rows, list) or not all(isinstance(t, dict) for t in task_rows): raise ValueError()
            if any(t.get('status') in ('pending', 'queued', 'running', 'processing') for t in task_rows):
                raise Refused('MAKING_BUSY', '还有人物正在制作，等它做完后再删除。')
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise Refused('RUNTIME_UNVERIFIED', '暂时无法核对运行中的人物，这次没有删除。')
    if ident in protected:
        raise Refused('ACTIVE_PROTECTED', '这个人物正在使用，请先换成另一个人物。')
    root = admin.AVATAR_DATA_DIR.resolve()
    source = Path(str(entry.get('dir') or ''))
    expected = root / ident
    try:
        if not source.is_absolute() or source.resolve() != expected or expected.parent != root:
            raise ValueError()
        if source.exists():
            info = source.lstat()
            if source.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400 or not source.is_dir():
                raise ValueError()
        for other in rows:
            if other is entry or not isinstance(other, dict): continue
            if other.get('id') == ident: raise ValueError()
            if other.get('dir'):
                shared = Path(other['dir']).resolve()
                if shared.is_relative_to(expected) or expected.is_relative_to(shared): raise ValueError()
    except (OSError, ValueError, TypeError):
        raise Refused('UNSAFE_PATH', '素材位置不正确或与其他人物共用，这次没有删除。')
    return entry, expected


def recover_pending(admin, data):
    """A crash between rename and registration leaves a durable journal.

    Roll back uncommitted renames; never move an unverified manifest path.
    A committed tombstone proves a successful removal even if its final
    journal update was interrupted. Runs under the same deletion locks.
    """
    trash = admin.AVATAR_DATA_DIR.parent / '_deleted_avatars'
    if not trash.exists(): return
    if trash.is_symlink() or getattr(trash.lstat(), 'st_file_attributes', 0) & 0x400:
        raise Refused('UNSAFE_PATH', '保留区位置不正确，这次没有删除。')
    entries = {e.get('id'): e for e in data.get('avatars', []) if isinstance(e, dict)}
    deleted = {e.get('id'): e.get('receipt') for e in data.get('deleted_avatars', []) if isinstance(e, dict)}
    for folder in trash.iterdir():
        if not re.fullmatch('[a-f0-9]{32}', folder.name): continue
        if folder.is_symlink() or getattr(folder.lstat(), 'st_file_attributes', 0) & 0x400:
            raise Refused('UNSAFE_PATH', '保留区位置不正确，这次没有删除。')
        path = folder / 'manifest.json'
        if not path.exists(): continue  # An interrupted mkdir moved no assets.
        record = strict_json(path)
        if record.get('state') != 'prepared': continue
        entry = record.get('entry') or {}; ident = str(entry.get('id') or '')
        if not re.fullmatch('[A-Za-z0-9_-]{4,64}', ident):
            raise Refused('RECOVERY_REQUIRED', '保留记录需要核对，这次没有删除。')
        source = admin.AVATAR_DATA_DIR.resolve() / ident
        if deleted.get(ident) == folder.name and ident not in entries:
            record['state'] = 'committed'
        elif ident in entries and Path(entries[ident].get('dir', '')).resolve() == source:
            held = folder / 'assets'
            if held.exists():
                if source.exists() or held.is_symlink() or getattr(held.lstat(), 'st_file_attributes', 0) & 0x400:
                    raise Refused('RECOVERY_REQUIRED', '保留记录需要核对，这次没有删除。')
                os.replace(held, source)
            record['state'] = 'rolled_back'
        else:
            raise Refused('RECOVERY_REQUIRED', '保留记录需要核对，这次没有删除。')
        admin._write_json_atomic(path, record)
        admin._log('avatar-delete.log', f'DELETE_RECOVERED receipt={folder.name} state={record["state"]}')


def operation(admin, req, commit=False):
    # Same-process switches/creation and the original cross-process lifecycle
    # mutex are held only for this short metadata+rename transaction.
    try:
        with admin._switch_lock, admin._making_lock, admin._tasks_lock, lifecycle_lock(), admin._registry_lock:
            data = strict_json(admin.AVATARS_JSON)
            recover_pending(admin, data)
            entry, source = validate(admin, data, req.编号)
            check = revision(entry)
            if not commit:
                return admin._done({'名称': str(entry.get('display_name') or '人物'), '校验': check,
                                    '说明': '删除后会从人物列表移除，生成素材移到保留区，原视频保留。'})
            if req.确认 is not True:
                raise Refused('CONFIRM_REQUIRED', '请先确认要删除这个人物。')
            if req.校验 != check:
                raise Refused('ASSET_CHANGED', '这个人物的资料已变化，请刷新后重新确认。')
            # Outside the scan root, same volume, no recursive removal/copy.
            trash = admin.AVATAR_DATA_DIR.parent / '_deleted_avatars'
            if trash.is_symlink() or (trash.exists() and getattr(trash.lstat(), 'st_file_attributes', 0) & 0x400):
                raise Refused('UNSAFE_PATH', '保留区位置不正确，这次没有删除。')
            trash.mkdir(exist_ok=True)
            receipt = uuid.uuid4().hex
            folder = trash / receipt; folder.mkdir()
            manifest = folder / 'manifest.json'
            record = {'version':1, 'receipt':receipt, 'deleted_at':admin._now_iso(),
                      'state':'prepared', 'entry':copy.deepcopy(entry), 'source':str(source), 'had_assets':source.exists()}
            admin._write_json_atomic(manifest, record)
            changed = copy.deepcopy(data)
            changed['avatars'] = [e for e in changed['avatars'] if e.get('id') != entry['id']]
            changed.setdefault('deleted_avatars', []).append({'id':entry['id'], 'receipt':receipt, 'deleted_at':record['deleted_at']})
            changed['updated_at'] = admin._now_iso()
            moved = False
            try:
                if record['had_assets']:
                    os.replace(source, folder / 'assets'); moved = True
                admin._write_json_atomic(admin.AVATARS_JSON, changed)
            except OSError:
                if moved:
                    try: os.replace(folder / 'assets', source)
                    except OSError:
                        admin._log('avatar-delete.log', f'DELETE_RECOVERY_REQUIRED receipt={receipt}')
                        raise Refused('RECOVERY_REQUIRED', '这次没有完成，素材仍在保留区，请查看恢复记录。')
                record['state'] = 'rolled_back'
                with contextlib.suppress(OSError): admin._write_json_atomic(manifest, record)
                raise Refused('DELETE_FAILED', '这次没有完成删除，人物资料保留，请稍后再试。')
            admin._registry_cache = changed
            record['state'] = 'committed'
            with contextlib.suppress(OSError): admin._write_json_atomic(manifest, record)
            admin._log('avatar-delete.log', f'DELETE_COMMITTED id={entry["id"]} receipt={receipt}')
            return admin._done({'恢复编号':receipt, '说明':'已经从人物列表移除，素材已保留，原视频未动。'})
    except Refused as exc:
        admin._log('avatar-delete.log', f'DELETE_REFUSED reason={exc.code}')
        return admin._fail(exc.code, exc.note)
    except OSError as exc:
        admin._log('avatar-delete.log', f'DELETE_FAILED error={type(exc).__name__}')
        return admin._fail('DELETE_FAILED', '这次没有完成删除，请稍后再试。')


def register(admin):
    def allowed(request):
        origin = request.headers.get('origin')
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1','localhost') or parsed.port != 7860:
                return False
        return request.client is not None and request.client.host in ('127.0.0.1', '::1')

    @admin.router.post('/api/avatar/delete/preview')
    def preview(request: Request, req: DeleteRequest):
        if not allowed(request): return admin._fail('ORIGIN_DENIED', '这个操作只能在本机管理页面完成。')
        return operation(admin, req)

    @admin.router.post('/api/avatar/delete')
    def delete(request: Request, req: DeleteRequest):
        if not allowed(request): return admin._fail('ORIGIN_DENIED', '这个操作只能在本机管理页面完成。')
        return operation(admin, req, commit=True)
