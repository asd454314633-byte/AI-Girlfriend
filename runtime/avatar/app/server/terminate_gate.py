"""Deny PROCESS_TERMINATE on our own process object -- and on our launcher's -- for a
bounded window.

WHY THIS EXISTS
---------------
Every measured death of the :8010 service has the same shape and no explanation:
exit status 0x00000000, no Python ``atexit``, no ``faulthandler`` dump, no WER
entry, no console CTRL_* line, and no carrier process in a 60 ms-resolution
process trail.  Two families produce exactly that shape and nothing we had could
tell them apart:

  A. an outside process opened a handle to us and called ``TerminateProcess`` --
     available unelevated, and reachable from PowerShell with NO new process at
     all (``Invoke-CimMethod -MethodName Terminate``, or an ``Add-Type`` P/Invoke
     from a shell that was already running), which is exactly why the killer never
     showed up in a process trail;
  B. this process ended itself through a path Python cannot see -- a native
     ``ExitProcess(0)`` / CRT ``exit(0)`` inside some extension.

WHAT MEASUREMENT CHANGED ABOUT THAT
-----------------------------------
Two results, both from runtime/tests/probes/, and both of them moved this module:

* ``os.kill(pid, 0)`` was recorded here as INERT on all three interpreters this stack runs.
  **That reading is RETRACTED (2026-09-25).**  It came from aiming the call at the calling
  process itself, with a sibling calling ``os.kill(self, SIGTERM)`` as the positive control
  (oskill0_selfaim.py: the control dies with 0x0000000F, each sig=0 case reports its own
  survival, twice).  That was correct for what it measured -- a process is always inside its
  own console -- and precisely for that reason it can never exercise the cross-console
  branch, so it says nothing about the :7860 panel aiming at a :8010 listener pid.
  Re-measured in the production shape (``runtime/debug/pid_probe/lab3_cross_console_kill0.py``,
  re-run 2026-09-25 13:15 -> ``lab3_run3_1315.txt``): sig=0 aimed at a cross-console,
  venv-trampoline-re-exec'd listener pid **is** a kill -- status ``0x00000000``, no atexit,
  no faulthandler stack, no WER entry, no new carrier process.  The same row aimed at a
  shared-console target is inert (R4), and ``os.kill(pid, SIGTERM)`` gives 0x0000000F (R5,
  positive control).  So the :7860 panel's ``_pid_alive`` *was* the killer; it now goes
  through the read-only handle in ``runtime/ui/app/pid_liveness.py``
  (``SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION``), and ``os.kill`` is banned from any
  Windows-reachable liveness path -- machine-checkable by
  ``runtime/debug/scan_oskill_gate_0925.py``.
  Test-author caveat, measured the same afternoon: a canary aimed at its OWN direct
  ``Popen`` child does not reproduce the kill (``test_pid_liveness``'s canary FAILED at
  13:13 while lab3 PASSED minutes later on the same box).  The decisive variable is the pid
  *class* the probe aims at, so any liveness regression test must aim at a
  trampoline-re-exec'd, self-reported pid, not at ``proc.pid``.

* ``launcher_kill_tree_fingerprint.py`` measured what each primitive leaves behind when
  it is aimed at the LAUNCHER (``runtime\\avatar\\venv\\Scripts\\python.exe``) -- the pid
  ``service_pids.json['avatar']`` actually holds -- rather than at the listener.  The venv
  redirector runs the real interpreter inside a kill-on-close job object, so the listener
  dies together with the launcher and its recorded status is **0x00000000 for every
  primitive**: Terminate0, Terminate1, Popen.terminate(), Stop-Process -Force,
  [Process]::Kill(), Kill($true), taskkill /F.  Only ``taskkill /T`` puts a nonzero
  status on the listener.

  That invalidates the reasoning previously used to exclude killers: the old table read
  statuses from the process the primitive was *aimed at*, so the listener's 0x00000000
  carries no information about who killed it.  What still discriminates is the
  *launcher's* status, and the observed pair is listener=0x00000000 / launcher=0x00000000
  -- a status-0 terminate on either pid, or a launcher that exited 0 by itself and closed
  its job.

CONSEQUENCE FOR THIS MODULE
---------------------------
Arming only the listener would have been nearly useless: the handle that matters is very
often aimed at the launcher, which is a *different process object* with its own DACL.
``arm()`` therefore covers this process **and its parent, when that parent is a python
launcher** -- roles ``self`` and ``parent`` appear in every log line.  A parent that is
not a python image (the Supervisor's ``powershell.exe``, a ``conhost``) is *skipped and
said to be skipped*: taking away the Supervisor's ability to stop the owner is a
self-inflicted outage, and it is not where the fingerprint points anyway.

HOW IT SEPARATES A FROM B
-------------------------
Access checks on a process object happen when the handle is OPENED.  A
``DENY PROCESS_TERMINATE`` ACE for Everyone in front of the DACL makes every *fresh*
``OpenProcess(PROCESS_TERMINATE)`` fail with ``ERROR_ACCESS_DENIED`` -- including a
caller that would have killed us a millisecond later.  ``ExitProcess`` and the
current-process pseudo handle do not go through that check, and neither does a job
object closing:

  * teardown completes, both processes alive  -> family A, by fresh handle, on one of
    these two objects (the log line says which)
  * we still vanish with 0x00000000           -> not a fresh handle open on an armed
    object: a native self-exit, a handle opened BEFORE arming, or the launcher's job
    closing for a reason of its own

so a survival is a proof and a death is still information.  Both are recorded.

WHY SDDL AND NOT SetEntriesInAcl
--------------------------------
Both were tried here.  ``SetEntriesInAclW`` + ``SetSecurityDescriptorDacl`` on an
absolute-format descriptor reported success and changed nothing, and an
``AllocateAndInitializeSid`` spelling with zero sub-authorities builds S-1-1, not
Everyone -- an ACE nobody holds, which is also accepted in silence.  A gate that
denies nothing while saying it armed is the single worst outcome available to this
investigation, so the descriptor is written as text and every arm is *verified by
re-opening the object it just protected*.  The lab has since confirmed the whole
contract end to end, three repeats: blocked=True, watchable=True, and after disarming a
terminate returns the very status it was given.

Scope is deliberately narrow: only PROCESS_TERMINATE (0x1) is denied, so the Supervisor
and the resident witness keep their ``SYNCHRONIZE`` /
``PROCESS_QUERY_LIMITED_INFORMATION`` handles working, keep waiting on us, and keep
reading our exit status (measured: ``exit_code_while_armed = 0x00000103``, STILL_ACTIVE).
The original descriptor is restored in a ``finally`` and the restore is *verified* the
same way -- an unrestored gate would be a self-inflicted denial of service, so that
check reports loudly.

This is a diagnostic instrument, not lifecycle policy: it decides nothing about
sessions, and it cannot block a legitimate shutdown, because a legitimate Stop goes
through our own teardown (which disarms it) or through a handle that outlives the
window.
"""

import contextlib
import ctypes
import logging
import os
import re
import threading
from ctypes import wintypes as wt

logger = logging.getLogger('avatar.terminate_gate')

PROCESS_TERMINATE = 0x00000001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000

# winnt.h: the info bits for Get/SetKernelObjectSecurity are NOT the numbering a
# reader expects from the SDDL masks -- the DACL bit is 0x4, and asking for 0x1 reads
# the OWNER, which hands back a descriptor whose DACL is 'not present' (measured).
SECURITY_DACL_SECURITY_INFORMATION = 0x00000004
SDDL_REVISION_1 = 1
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_ACCESS_DENIED = 5

_PSEUDO_CURRENT = 0xFFFFFFFFFFFFFFFF      # what a -1 pseudo handle reads back as

# SDDL: type=deny, no flags, access mask PROCESS_TERMINATE, trustee WD = Everyone.
# ACE field order is type;flags;access-mask;object-type;inherited-object-type;sid.
#
# Read back, this renders as 'D:(D;;CC;;;WD)'.  'CC' is NOT a different mask: the SDDL
# printer names bit 0x1 after the directory-service specific right that occupies it
# (CREATE_CHILD), because a process object has no specific-rights mapping of its own.
# Measured, not assumed -- with this ACE in place a fresh OpenProcess(0x1) fails with
# ERROR_ACCESS_DENIED while OpenProcess(0x100000|0x1000) still succeeds, so the one bit
# that is denied is terminate and the deny is real.  Do not "fix" the 1 to something
# else on the strength of that rendering.
DENY_TERMINATE_ACE = '(D;;1;;;WD)'

# Only these parent images are ever armed, and only from inside this repo.  The whole
# point of a parent scope is the venv trampoline; arming a shell would break the
# Supervisor's Stop path for no diagnostic gain.
_LAUNCHER_IMAGES = ('python.exe', 'pythonw.exe')

advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

VP = ctypes.c_void_p

kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.LocalFree.restype = VP
kernel32.LocalFree.argtypes = [VP]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                                ctypes.POINTER(wt.DWORD)]

advapi32.GetKernelObjectSecurity.restype = wt.BOOL
advapi32.GetKernelObjectSecurity.argtypes = [wt.HANDLE, wt.DWORD, VP, wt.DWORD,
                                             ctypes.POINTER(wt.DWORD)]
advapi32.SetKernelObjectSecurity.restype = wt.BOOL
advapi32.SetKernelObjectSecurity.argtypes = [wt.HANDLE, wt.DWORD, VP]
advapi32.GetSecurityDescriptorDacl.restype = wt.BOOL
advapi32.GetSecurityDescriptorDacl.argtypes = [VP, ctypes.POINTER(wt.BOOL),
                                               ctypes.POINTER(VP), ctypes.POINTER(wt.BOOL)]
advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wt.BOOL
advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
    VP, wt.DWORD, wt.DWORD, ctypes.POINTER(VP), ctypes.POINTER(wt.DWORD)]
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wt.BOOL
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wt.LPCWSTR, wt.DWORD, ctypes.POINTER(VP), ctypes.POINTER(wt.DWORD)]

_LOCK = threading.RLock()
_DEPTH = 0
_STATE = None          # armed state: one row per process object covered
_KEEPALIVE = []        # buffers must outlive every later use of their pointer

ENV_NAME = 'AVATAR_TERMINATE_GATE'
ENV_SCOPE = 'AVATAR_TERMINATE_GATE_SCOPE'
ENV_NO_PARENT = 'AVATAR_TERMINATE_GATE_NO_PARENT'


def gate_enabled(default='1'):
    """The gate is on unless someone says otherwise, in this process' environment."""
    raw = os.environ.get(ENV_NAME)
    if raw is None:
        raw = default
    return str(raw).strip().lower() not in ('0', 'off', 'no', 'false', '')


def _try_open(access, pid=None):
    """(handle_or_None, last_error) for our pid or an explicit one.

    The probe that shows whether an ACE took effect.  A successful OpenProcess leaves
    the previous last-error value where it was, so it is cleared first -- otherwise a
    'handle=True, gle=5' row invites a reader to conclude the deny fired when it did not
    (that exact false reading cost one lab run).
    """
    ctypes.set_last_error(0)
    handle = kernel32.OpenProcess(access, False, pid or os.getpid())
    raw = getattr(handle, 'value', handle)
    if raw and raw != _PSEUDO_CURRENT:
        return handle, 0
    return None, ctypes.get_last_error()


def image_of(pid):
    """Executable path of a pid via a QUERY_LIMITED handle; '' when unreadable.

    Limited information is deliberate: the decision this feeds -- 'is the parent a python
    launcher, do I get to take away its terminate right' -- must not depend on a right
    that the gate itself touches, and PROCESS_QUERY_INFORMATION can be denied where the
    limited form is not.
    """
    h, _gle = _try_open(PROCESS_QUERY_LIMITED_INFORMATION, pid)
    if not h:
        return ''
    try:
        size = wt.DWORD(4096)
        buf = ctypes.create_unicode_buffer(4096)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ''
        return buf.value or ''
    except Exception:  # noqa: BLE001
        return ''
    finally:
        kernel32.CloseHandle(h)


def launcher_pids():
    """Process objects worth arming besides ourselves, plus why not when they are not.

    Returns ([(pid, 'parent')], [(pid_or_None, 'parent', why_skipped)]).  Everything here
    is decided from the object's own image name: the Supervisor's powershell and any
    conhost are named and skipped rather than silently left out.
    """
    wanted = str(os.environ.get(ENV_SCOPE, 'auto')).strip().lower()
    if wanted in ('self', 'off', 'no'):
        return [], [(None, 'parent', 'scope=%s' % wanted)]
    try:
        ppid = os.getppid()
    except Exception:  # noqa: BLE001
        ppid = None
    if not ppid or ppid <= 0:
        return [], [(None, 'parent', 'no_parent_pid')]
    img = image_of(ppid)
    base = os.path.basename(img).lower() if img else ''
    if base not in _LAUNCHER_IMAGES:
        return [], [(ppid, 'parent', 'not_a_launcher image=%s' % (base or 'unreadable'))]
    if str(os.environ.get(ENV_NO_PARENT, '')).strip().lower() in ('0', 'off', 'no'):
        return [], [(ppid, 'parent', 'parent_scope_disabled')]
    return [(ppid, 'parent')], []


def _read_sd(pid):
    """(handle, buffer, size) for a process' security descriptor.

    Two calls: a size probe with a NULL buffer, then the read.  What comes back is
    self-relative, which is the format ``SetKernelObjectSecurity`` wants for the
    restore, and the buffer is kept in ``_KEEPALIVE`` because that restore happens much
    later -- a collected buffer would be a dangling read.
    """
    handle, gle = _try_open(READ_CONTROL | WRITE_DAC, pid)
    if not handle:
        raise OSError('OPEN_FOR_SECURITY_FAILED pid=%s gle=%d' % (pid, gle))
    try:
        needed = wt.DWORD(0)
        advapi32.GetKernelObjectSecurity(handle, SECURITY_DACL_SECURITY_INFORMATION,
                                         None, 0, ctypes.byref(needed))
        err = ctypes.get_last_error()
        if not needed.value:
            raise OSError('SD_SIZE_UNKNOWN pid=%s gle=%d' % (pid, err))
        buf = ctypes.create_string_buffer(needed.value)
        _KEEPALIVE.append(buf)
        got = wt.DWORD(0)
        if not advapi32.GetKernelObjectSecurity(handle, SECURITY_DACL_SECURITY_INFORMATION,
                                                buf, needed.value, ctypes.byref(got)):
            raise OSError('GET_SD_FAILED pid=%s gle=%d' % (pid, ctypes.get_last_error()))
        return handle, buf, got.value
    except Exception:
        kernel32.CloseHandle(handle)
        raise


def describe_sd(buf):
    """The DACL as SDDL text, for the log.  '' when it cannot be read."""
    try:
        ptr = VP()
        size = wt.DWORD(0)
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                buf, SDDL_REVISION_1, SECURITY_DACL_SECURITY_INFORMATION,
                ctypes.byref(ptr), ctypes.byref(size)):
            return ''
        try:
            return ctypes.wstring_at(ptr.value) or ''
        finally:
            kernel32.LocalFree(ptr)
    except Exception:  # noqa: BLE001
        return ''


def splice_deny_ace(sddl):
    """Insert DENY_TERMINATE_ACE into the 'D:' section of an SDDL string.

    Right after the DACL flag letters, so it is the first ACE the access check
    evaluates.  Parenthesis depth is tracked because ACE text contains ':' too.
    """
    depth = 0
    i = 0
    n = len(sddl)
    while i < n - 1:
        c = sddl[i]
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
        elif depth == 0 and c.isalpha() and sddl[i + 1] == ':':
            if c == 'D':
                j = i + 2
                while j < n and sddl[j].isalpha():
                    j += 1          # flag letters: P, D, AI, AU
                return sddl[:j] + DENY_TERMINATE_ACE + sddl[j:]
            if c in 'SOGO':          # SACL / owner / group / its label: keep looking
                pass
        i += 1
    return None


def _sd_from_sddl(sddl):
    """(pointer_or_None, gle).  The returned descriptor is self-relative; caller frees."""
    ptr = VP()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, SDDL_REVISION_1, ctypes.byref(ptr), None):
        return None, ctypes.get_last_error()
    return ptr, 0


def _arm_one(pid, role):
    """One process object: add the deny ACE, then prove it took and prove it is narrow.

    Never raises -- failing to arm must not take a live session down with it, so every
    path returns a verdict dict and the caller only ever logs it.
    """
    verdict = {'pid': pid, 'role': role, 'armed': False, 'why': '',
               'deny_verified': False, 'watchdog_ok': False,
               'dacl_before': '', 'dacl_after': ''}
    try:
        handle, buf, _needed = _read_sd(pid)
    except Exception as exc:  # noqa: BLE001 - an instrument, not a dependency
        verdict['why'] = 'READ_SD_FAILED:%s' % (exc,)
        return verdict

    def _give_up(why, rollback=False):
        verdict['why'] = why
        if rollback:
            # put back whatever the object had before this attempt, then say so
            if advapi32.SetKernelObjectSecurity(handle, SECURITY_DACL_SECURITY_INFORMATION,
                                                buf):
                verdict['why'] = why + ' rolled_back'
        try:
            kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass
        return verdict

    try:
        present = wt.BOOL(False)
        defaulted = wt.BOOL(False)
        old_acl = VP()
        if not advapi32.GetSecurityDescriptorDacl(buf, ctypes.byref(present),
                                                 ctypes.byref(old_acl),
                                                 ctypes.byref(defaulted)):
            return _give_up('GET_DACL_FAILED gle=%d' % ctypes.get_last_error())
        if not present.value:
            # 'no DACL' means 'everything is allowed to everyone'.  Writing a DACL
            # that holds only our deny entry would revoke every other right the
            # Supervisor and the witness need, so refuse rather than guess.
            return _give_up('NO_DACL_PRESENT refused')

        before = describe_sd(buf)
        verdict['dacl_before'] = before[:220]
        if not before:
            return _give_up('SDDL_READ_FAILED')
        if DENY_TERMINATE_ACE in before:
            # Already armed -- by an outer window of ours, or by a run that died before
            # it could disarm.  Re-splicing would stack a second copy on an object whose
            # original we no longer hold, so adopt this one and let its owner restore it.
            verdict['dacl_after'] = before[:220]
            verdict['armed'] = True
            verdict['why'] = 'ALREADY_ARMED adopted'
            return verdict
        spliced = splice_deny_ace(before)
        if not spliced:
            return _give_up('SDDL_SPLICE_FAILED no_D_section')
        new_sd, gle = _sd_from_sddl(spliced)
        if not new_sd or not new_sd.value:
            return _give_up('SDDL_TO_SD_FAILED gle=%d' % gle)
        try:
            ok = advapi32.SetKernelObjectSecurity(handle, SECURITY_DACL_SECURITY_INFORMATION,
                                                  new_sd)
        finally:
            kernel32.LocalFree(new_sd)
        if not ok:
            return _give_up('SET_KERNEL_OBJECT_SECURITY_FAILED gle=%d'
                            % ctypes.get_last_error())

        # -- the gate has to prove itself before anyone believes a survival ----------
        # 'we called the API' is not 'the ACE works' -- three silent no-ops were caught
        # exactly here, and an unverified gate would turn every later death into a false
        # verdict of 'internal self-exit'.
        h_term, gle_term = _try_open(PROCESS_TERMINATE, pid)
        verdict['dacl_after'] = (_read_dacl_of(pid) or '')[:220]
        if h_term:
            kernel32.CloseHandle(h_term)
            return _give_up('VERIFY_TERMINATE_STILL_ALLOWED', rollback=True)
        if gle_term != ERROR_ACCESS_DENIED:
            return _give_up('VERIFY_UNEXPECTED_gle=%d' % gle_term, rollback=True)
        verdict['deny_verified'] = True

        h_watch, _gw = _try_open(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, pid)
        if h_watch:
            verdict['watchdog_ok'] = True
            kernel32.CloseHandle(h_watch)
        else:
            # Denied more than terminate would break the lifecycle instruments that
            # watch us, which is a different bug than the one being investigated.
            return _give_up('WATCHDOG_OPEN_DENIED gle=%d' % _gw, rollback=True)

        verdict['armed'] = True
        verdict['why'] = ''
        verdict['_handle'] = handle
        verdict['_sd'] = buf
        return verdict
    except Exception as exc:  # noqa: BLE001
        verdict['why'] = 'ARM_EXCEPTION:%s' % (exc,)
        try:
            kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass
        return verdict


def _read_dacl_of(pid=None):
    """A process' current DACL as text, read fresh.  Used for before/after logs."""
    try:
        handle, buf, _n = _read_sd(pid or os.getpid())
        try:
            return describe_sd(buf)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return ''


def _read_dacl_now():
    return _read_dacl_of(os.getpid())


def arm(scope='auto'):
    """Add 'DENY PROCESS_TERMINATE to Everyone' to our DACL and, when the parent is a
    python launcher, to its DACL too.

    One aggregate verdict dict, whose ``targets`` hold the per-object rows, so a log line
    can say *which* object carries which state.  Self is always first: a run that could
    not arm the parent still has to read its own survival correctly.
    """
    if str(scope or '').strip().lower() == 'self':
        targets, skipped = [(os.getpid(), 'self')], [(None, 'parent', 'scope=self')]
    else:
        extra, skipped = launcher_pids()
        targets = [(os.getpid(), 'self')] + list(extra)
    rows = [_arm_one(t_pid, role) for (t_pid, role) in targets]
    for spid, srole, swhy in skipped:
        rows.append({'pid': spid, 'role': srole, 'armed': False, 'deny_verified': False,
                     'watchdog_ok': False, 'why': 'SKIPPED %s' % swhy,
                     'dacl_before': '', 'dacl_after': ''})
    self_row = next((r for r in rows if r['role'] == 'self'), None)
    parent_row = next((r for r in rows if r['role'] == 'parent'), None)
    return {
        # 'armed' means the shield we can interpret a survival with: our own object.
        # parent_armed is reported separately, because a death with parent=0 and self=1
        # is information about where the handle was aimed.
        'armed': bool(self_row and self_row.get('armed')),
        'self_armed': bool(self_row and self_row.get('armed')),
        'parent_armed': bool(parent_row and parent_row.get('armed')),
        'deny_verified': bool(self_row and self_row.get('deny_verified')),
        'watchdog_ok': bool(self_row and self_row.get('watchdog_ok')),
        'dacl_before': (self_row or {}).get('dacl_before', ''),
        'dacl_after': (self_row or {}).get('dacl_after', ''),
        'why': ';'.join(['%s:%s' % (r['role'], r['why']) for r in rows if r.get('why')]),
        'targets': rows,
    }


def _disarm_one(row):
    out = {'pid': row.get('pid'), 'role': row.get('role'), 'restored': False,
           'terminate_allowed_after': None, 'dacl_after_restore': '', 'why': ''}
    handle = row.get('_handle')
    buf = row.get('_sd')
    if not handle or buf is None:
        # An 'adopted' row carries no original descriptor: whoever armed it owns the
        # restore, and pretending otherwise would write back a DACL we never read.
        out['why'] = str(row.get('why') or 'NOT_ARMED_BY_US')
        out['dacl_after_restore'] = (_read_dacl_of(row.get('pid')) or '')[:220]
        return out
    try:
        ok = advapi32.SetKernelObjectSecurity(handle, SECURITY_DACL_SECURITY_INFORMATION, buf)
        out['restored'] = bool(ok)
        if not ok:
            out['why'] = 'SET_BACK_FAILED gle=%d' % ctypes.get_last_error()
        out['dacl_after_restore'] = (_read_dacl_of(row.get('pid')) or '')[:220]
        h_term, gle_term = _try_open(PROCESS_TERMINATE, row.get('pid'))
        if h_term:
            kernel32.CloseHandle(h_term)
            out['terminate_allowed_after'] = True
        else:
            out['terminate_allowed_after'] = False
            out['why'] = (out['why'] + ' ' if out['why'] else '') + \
                         'STILL_DENIED gle=%d' % gle_term
        return out
    except Exception as exc:  # noqa: BLE001
        out['why'] = 'DISARM_EXCEPTION:%s' % (exc,)
        return out
    finally:
        try:
            kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass


def disarm(state):
    """Put every saved descriptor back, and prove each object is terminatable again.

    ``terminate_allowed_after`` false with ``restore_called`` true means the ACE is
    still on our process: nothing outside can stop it any more.  That is a
    self-inflicted outage waiting for a reboot, so it gets its own event rather than
    being folded into 'restored'.
    """
    out = {'restore_called': False, 'restored': False, 'terminate_allowed_after': None,
           'dacl_after_restore': '', 'why': '', 'targets': []}
    rows = list((state or {}).get('targets') or [])
    ours = [r for r in rows if r.get('_handle')]
    if not ours:
        out['why'] = 'NO_ARMED_STATE'
        return out
    out['restore_called'] = True
    for row in ours:
        out['targets'].append(_disarm_one(row))
    out['restored'] = all(bool(t.get('restored')) for t in out['targets'])
    # An 'adopted' row was armed by someone else's window (or a run that died before it
    # could restore) and carries no original descriptor, so we must not write one back.
    # Saying nothing about it would let a stuck ACE read as 'nothing to restore', and a
    # stuck deny ACE on the launcher is how a diagnostic instrument turns into an outage.
    for row in rows:
        if not row.get('_handle') and 'ALREADY_ARMED' in str(row.get('why') or ''):
            out['targets'].append({'pid': row.get('pid'), 'role': row.get('role'),
                                   'restored': False, 'terminate_allowed_after': None,
                                   'dacl_after_restore': (_read_dacl_of(row.get('pid'))
                                                          or '')[:220],
                                   'why': 'NOT_RESTORED adopted, another owner holds it'})
    # The aggregate keys keep the shape the readers and the lab already know: self first.
    primary = next((t for t in out['targets'] if t.get('role') == 'self'),
                   out['targets'][0] if out['targets'] else {})
    out['terminate_allowed_after'] = primary.get('terminate_allowed_after')
    out['dacl_after_restore'] = primary.get('dacl_after_restore', '')
    # 'released_*' rather than reusing 'armed': an arm-shaped field that reads False on a
    # disarm line is indistinguishable from a gate that fell off mid-teardown, and that
    # ambiguity is the one thing this instrument exists to remove.  None (not False) means
    # 'this run never held that object', so it cannot be misread as 'still shielded'.
    by_role = {t.get('role'): t for t in out['targets']}

    def _released(role):
        row = by_role.get(role)
        return None if row is None else bool(row.get('terminate_allowed_after'))

    out['released_self'] = _released('self')
    out['released_parent'] = _released('parent')
    out['why'] = ';'.join(['%s:%s' % (t.get('role'), t.get('why'))
                           for t in out['targets'] if t.get('why')])
    return out


@contextlib.contextmanager
def terminate_gate(reason, log=None, scope='auto'):
    """Hold the deny ACE for one bounded teardown; refcounted, always rolls back."""
    global _DEPTH, _STATE
    say = log or logger
    owned = False
    note = {'armed': False, 'why': 'gate_disabled_by_env', 'targets': []}
    with _LOCK:
        if gate_enabled():
            if _DEPTH == 0:
                _STATE = arm(scope)
            _DEPTH += 1
            owned = True
            note = _STATE or note
    _emit(say, 'arm', reason, note)
    try:
        yield note
    finally:
        if owned:
            res = None
            with _LOCK:
                _DEPTH = max(0, _DEPTH - 1)
                if _DEPTH == 0:
                    res = disarm(_STATE)
                    _STATE = None
            if res is not None:
                _emit(say, 'disarm', reason, res)


def _emit(say, phase, reason, verdict):
    """One line per phase plus one per process object, every conjunct spelled out.

    These lines separate 'the gate never ran' from 'the gate ran and we died anyway' --
    and, now that the launcher is a covered object, from 'the handle was aimed at the
    other process'.  They go to the same statement-level trail as the rest of the
    teardown markers, because that trail is the only thing that has survived a death.

    Each phase names the fields it actually measured: printing 'armed=False' on a disarm
    line would be a zero that means 'not applicable', and a reader of a post-mortem cannot
    be asked to guess which zeros are absences.
    """
    try:
        if phase == 'arm':
            say.info('TERMINATE_GATE arm reason=%s armed=%s self_armed=%s parent_armed=%s '
                     'deny_verified=%s watchdog_ok=%s why=%s dacl=%s',
                     str(reason)[:60],
                     bool(verdict.get('armed')), bool(verdict.get('self_armed')),
                     bool(verdict.get('parent_armed')), bool(verdict.get('deny_verified')),
                     bool(verdict.get('watchdog_ok')),
                     re.sub(r'\s+', ' ', str(verdict.get('why') or ''))[:160],
                     re.sub(r'\s+', ' ', str(verdict.get('dacl_after')
                                             or verdict.get('dacl_before') or ''))[:200])
        else:
            say.info('TERMINATE_GATE disarm reason=%s restore_called=%s restored=%s '
                     'released_self=%s released_parent=%s terminate_allowed_after=%s '
                     'why=%s dacl=%s',
                     str(reason)[:60], bool(verdict.get('restore_called')),
                     bool(verdict.get('restored')), str(verdict.get('released_self')),
                     str(verdict.get('released_parent')),
                     str(verdict.get('terminate_allowed_after')),
                     re.sub(r'\s+', ' ', str(verdict.get('why') or ''))[:160],
                     re.sub(r'\s+', ' ', str(verdict.get('dacl_after_restore') or ''))[:200])
        for row in (verdict.get('targets') or []):
            if phase == 'arm':
                say.info('TERMINATE_GATE arm target role=%s pid=%s armed=%s '
                         'deny_verified=%s watchdog_ok=%s why=%s dacl=%s',
                         row.get('role'), row.get('pid'), bool(row.get('armed')),
                         bool(row.get('deny_verified')), bool(row.get('watchdog_ok')),
                         re.sub(r'\s+', ' ', str(row.get('why') or ''))[:160],
                         re.sub(r'\s+', ' ', str(row.get('dacl_after')
                                                 or row.get('dacl_before') or ''))[:200])
            else:
                say.info('TERMINATE_GATE disarm target role=%s pid=%s restored=%s '
                         'terminate_allowed_after=%s why=%s dacl=%s',
                         row.get('role'), row.get('pid'), bool(row.get('restored')),
                         str(row.get('terminate_allowed_after')),
                         re.sub(r'\s+', ' ', str(row.get('why') or ''))[:160],
                         re.sub(r'\s+', ' ', str(row.get('dacl_after_restore') or ''))[:200])
    except Exception:  # noqa: BLE001 - an instrument must never cause the death
        pass
