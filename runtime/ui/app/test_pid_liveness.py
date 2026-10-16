# -*- coding: utf-8 -*-
"""判活探针的回归测试：问「还在不在」之后，被问的那个进程必须**还原样活着**。

这个文件存在的全部理由，是 2026-09-25 实测到的 Windows + CPython 陷阱（见 ``pid_liveness``
模块开头、两份实验记录 ``runtime/debug/pid_probe/lab3_run2.txt`` 与 ``lab3b_run3.txt``）：
``os.kill(pid, 0)`` 对 :8010 那个 listener 类进程**不是**无害的存在性检查，它会把目标以
退出码 0x00000000 静默打死（atexit 不跑、faulthandler 留不下栈、没有 WER、不产生新进程），
而调用方这边只留下一条待爆的异常 —— 恰好被 ``except Exception: return False`` 吃掉。

⚠ 现行状态（2026-09-25 用户裁决，勿改）：``test_canary_the_old_primitive_really_kills_this_class_of_target``
已被永久标注为 **INVALID_TOPOLOGY_CANARY**，且**禁止作为生产门禁引用它的红或绿** —— 因为它
现在红还是绿，取决于**用哪个解释器跑本文件**（同一份文件、同一个 canary 方法重复跑 5 次：
core python 3.12.3 → 红 5/5；一个 venv trampoline 3.11.9 → 绿 5/5；逐次退出码见
``runtime/debug/recount_canary_interpreter_ab_0925.txt``），而不是取决于生产根因是否成立。
详见该方法的文档字符串。
本套件的其余 6 项与判活实现本身不受影响，根因与修复的证据
在 ``runtime/debug/pid_probe/lab3_cross_console_kill0.py`` → ``lab3_run3_1315.txt``，
验收在 ``runtime/debug/verify_liveness_0925.report.txt`` 与 ``..._CONFIRM2.txt``。

三条不许妥协的设计（每一条都对应一次真实的假干净）：

1. **目标必须是 listener 类**：本栈的真实进程链是「venv 启动器 → re-exec 出来的真正
   解释器」，``Popen`` 给你的是前者，而 ``avatar_runtime_state.json['listener_pid']``、
   也就是面板真正瞄准的那个 pid，是后者。实测两者命运不同（lab3b 的 K1 打 launcher 无害、
   K2/K3/K4 打 listener 全部静默打死），所以**用 ``proc.pid`` 做测试会得到一个永远通过的
   空检查**。这里每个孩子都把自己的 ``os.getpid()`` 落盘，测试只拿那个自报的 pid。
2. **canary 先证明这把枪真的会响**：``test_canary_...`` 直接用旧写法打死自己起的孩子。
   没有它，"新探针没杀人"可能只是因为"根本没瞄到会死的东西"。
   （2026-09-25 修正：本条**意图**成立，但**这个方法做不到** —— 它的孩子不是 listener 类，
   所以它的红绿都不构成"枪会响"的证据；正向证据改引 ``lab3_run3_1315.txt``。）
3. **权限位死锁**：判活掩码里不许出现 ``PROCESS_TERMINATE``；同时用 AST 检查函数体里不许
   再出现 ``os.kill`` —— 用文本匹配会被解释它的中文注释自己触发（第一版就这么红过一次）。

只操作本文件自己启动的子进程；绝不对 :8010/:7860/:8765 的任何真实 pid 做写操作。
Run:  cd runtime/ui/app && python -m unittest test_pid_liveness -v
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pid_liveness  # noqa: E402

CREATE_NO_WINDOW = 0x08000000
PROCESS_TERMINATE = 0x00000001
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# A child that reports the pid of the interpreter that is actually running -- not the pid
# the venv redirector handed back to Popen -- then just sits there.
_CHILD_SRC = textwrap.dedent('''
    import os, sys, time
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
        fh.flush()
    time.sleep(%d)
''')


def spawn_own_console_child(seconds=60):
    """A live **listener-class** process that does not share this test's console.

    Returns ``(proc, self_reported_pid)``, where the second one is the class of pid the
    panel aims at in production (``listener_pid``).  CREATE_NO_WINDOW mirrors the real
    relationship -- the :7860 panel is itself started with ``Start-Process -WindowStyle
    Hidden`` by ``scripts/Runtime-Common.ps1``, so panel and :8010 listener own separate
    consoles and never share one.
    """
    pidfile = os.path.join(tempfile.mkdtemp(prefix='pidliveness_'), 'self.pid')
    proc = subprocess.Popen(
        [sys.executable, '-c', _CHILD_SRC % seconds, pidfile],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW if os.name == 'nt' else 0)
    deadline = time.time() + 20.0
    reported = None
    while time.time() < deadline:
        try:
            with open(pidfile, 'r', encoding='utf-8') as fh:
                text = fh.read().strip()
            if text.isdigit():
                reported = int(text)
                break
        except OSError:
            pass
        if proc.poll() is not None:
            raise AssertionError('child died before reporting its pid rc=%s' % proc.returncode)
        time.sleep(0.05)
    if reported is None:
        kill_child(proc, reported)
        raise AssertionError('child never reported its own pid')
    end = time.time() + 10.0
    while time.time() < end and not pid_liveness.pid_probe(reported)['alive']:
        time.sleep(0.05)
    if not pid_liveness.pid_probe(reported)['alive']:
        kill_child(proc, reported)
        raise AssertionError('reported pid never became observable: %s'
                             % pid_liveness.pid_probe(reported))
    return proc, reported


def kill_child(proc, pid):
    """Terminate a child of *this* test, by the handle route, after it stopped mattering."""
    for target in (pid, proc.pid):
        if not target:
            continue
        h, _gle = _open(target, PROCESS_TERMINATE)
        if h:
            _terminate(h)
            _close(h)
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001  cleanup never masks a verdict
        pass


def _k32():
    global _K32
    if _K32 is None:
        import ctypes
        from ctypes import wintypes as wt
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = wt.HANDLE
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k32.TerminateProcess.restype = wt.BOOL
        k32.TerminateProcess.argtypes = [wt.HANDLE, ctypes.c_uint]
        k32.CloseHandle.restype = wt.BOOL
        k32.CloseHandle.argtypes = [wt.HANDLE]
        _K32 = k32
    return _K32


_K32 = None


def _open(pid, access):
    ctypes = __import__('ctypes')
    k32 = _k32()
    ctypes.set_last_error(0)
    h = k32.OpenProcess(access, False, int(pid))
    raw = getattr(h, 'value', h)
    return (h if raw and raw != 0xFFFFFFFFFFFFFFFF else None), ctypes.get_last_error()


def _terminate(h):
    return _k32().TerminateProcess(h, 7)


def _close(h):
    _k32().CloseHandle(h)


def flush_pending_exception():
    """``os.kill`` on this branch can RETURN A VALUE WITH AN EXCEPTION STILL SET.

    The stale exception then detonates at whatever C call comes next -- which is how the
    first run of ``lab3b_which_branch.py`` died inside ``time.time()`` two statements away
    from the real event.  A test that does not flush it can attribute row N's interpreter
    state to row N+1, so every call to the old primitive here is followed by this.
    """
    try:
        time.time()
        return 'clean'
    except BaseException as exc:  # noqa: BLE001
        try:
            time.time()      # consume it so it cannot leak into the next statement
        except BaseException:  # noqa: BLE001
            pass
        return 'pending: %s: %s' % (type(exc).__name__, str(exc)[:120])


def function_body(source, name):
    """The real code of ``name``, statements only -- comments and docstrings excluded, so
    prose that *mentions* the forbidden primitive cannot make this test pass or fail."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError('function %s not found' % name)


def calls_in(func):
    out = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                base = f.value.id if isinstance(f.value, ast.Name) else '?'
                out.append('%s.%s' % (base, f.attr))
            elif isinstance(f, ast.Name):
                out.append(f.id)
    return out


class TestReadOnlyLiveness(unittest.TestCase):
    def test_self_is_alive_and_still_here(self):
        self.assertTrue(pid_liveness.pid_alive(os.getpid()))

    def test_probe_of_cross_console_listener_class_child_leaves_it_running(self):
        proc, pid = spawn_own_console_child()
        try:
            row = pid_liveness.pid_probe(pid)
            self.assertTrue(row['alive'], row)
            self.assertIsNone(proc.poll(),
                              'the liveness probe killed the child it asked about: %s' % row)
            self.assertTrue(pid_liveness.pid_alive(pid), 'second answer says dead')
            self.assertTrue(pid_liveness.pid_alive(proc.pid), 'launcher branch says dead')
            self.assertIsNone(proc.poll(), 'second probe killed the child')
        finally:
            kill_child(proc, pid)

    def test_dead_pid_is_false(self):
        proc, pid = spawn_own_console_child(seconds=1)
        proc.wait(timeout=30)
        end = time.time() + 8.0
        while time.time() < end and pid_liveness.pid_probe(pid)['alive']:
            time.sleep(0.1)
        self.assertFalse(pid_liveness.pid_alive(pid), 'a reaped child still reads alive')

    def test_nonpositive_pid_is_false(self):
        for pid in (0, -1, -9999):
            self.assertFalse(pid_liveness.pid_alive(pid))

    def test_mask_never_requests_terminate(self):
        # tripwire: the day somebody ORs PROCESS_TERMINATE into the probe mask, this stack
        # is pointing a gun at :8010 again
        self.assertEqual(pid_liveness._QUERY_MASK,
                         SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION)
        self.assertEqual(pid_liveness._QUERY_MASK & PROCESS_TERMINATE, 0)

    def test_panel_and_voice_funtions_call_only_the_read_only_probe(self):
        for filename, func in (('avatar_admin.py', '_pid_alive'),
                               ('voice_models_admin.py', '_process_alive')):
            with open(os.path.join(HERE, filename), 'r', encoding='utf-8') as fh:
                tree_src = fh.read()
            body = function_body(tree_src, func)
            called = calls_in(body)
            self.assertNotIn('os.kill', called,
                             '%s::%s went back to the killing primitive (calls=%s)'
                             % (filename, func, called))
            self.assertTrue(any(c.endswith('pid_alive') for c in called),
                            '%s::%s no longer delegates to pid_liveness (calls=%s)'
                            % (filename, func, called))

    @unittest.skipUnless(os.name == 'nt', 'Windows-only mechanism proof')
    def test_canary_the_old_primitive_really_kills_this_class_of_target(self):
        """canary_class = **INVALID_TOPOLOGY_CANARY** —— 永久标注，禁止当生产门禁。

        本应证的东西：证明"这套拓扑 + 这一类目标"真的会被 ``os.kill(pid, 0)`` 打死，
        否则"新探针没杀人"可能只是因为"根本没瞄到会死的东西"。这个设计意图依然正确。

        但它**没有**复刻生产拓扑，所以它现在的红灯**不构成**对根因或修复的反证：

          * 这里的孩子 = ``Popen([sys.executable, '-c', …], CREATE_NO_WINDOW)`` 且自报
            ``os.getpid()``。这是一条**单层**链：``Popen.pid`` 与自报 pid 是同一个进程，
            中间既没有 venv 启动器（trampoline），也没有 re-exec，也没有那层
            kill-on-close job object。
          * 生产里被杀的是 ``runtime\\avatar\\venv\\Scripts\\python.exe`` re-exec 出来的真
            解释器 —— ``avatar_runtime_state.json['listener_pid']`` 那一类。同一台机器、
            同一解释器下，后者被 sig=0 稳定打成 ``0x00000000``
            （``runtime/debug/pid_probe/lab3_cross_console_kill0.py`` →
            ``lab3_run3_1315.txt``：R1/R2/R3/R3b 跨 console 全杀，R4 同 console 才无害，
            R5 ``SIGTERM`` 给 ``0x0000000F`` 作正向对照）。
          * ⇒ 差异变量是"目标是否经 trampoline re-exec 而来"，不是"sig=0 是否致命"。
            这条 canary 只能判"我的探针瞄对了进程类别没有"，判不了生产结论。
          * **而且它的红/绿取决于用哪个解释器跑本文件**（2026-09-25 同文件 A/B。
            下面分数 = **同一个 canary 方法重复跑的次数**，不是"套件 7 项里绿了几项"；
            逐次退出码见 ``runtime/debug/recount_canary_interpreter_ab_0925.txt``）：
            用 ``runtime\\core\\python\\cpython-3.12.3-windows-x86_64-none\\python.exe``
            （``in_venv=False``，真解释器，孩子也是单层）→ ``test_canary`` **红 5/5 次**
            （不复现击杀）；用 ``D:\\AI\\hermes_home\\hermes-agent\\
            venv\\Scripts\\python.exe``（``in_venv=True`` 的 venv trampoline 3.11.9，
            孩子经启动器 re-exec）→ **绿 5/5 次**（复现击杀）。
            单跑一次的形状分别是 ``Ran 7 tests / FAILED (failures=1)`` 与
            ``Ran 7 tests / OK`` —— 变的只有解释器谱系，代码逐字相同。
            所以本方法**红也不推翻生产结论，绿也不证明根因**，两头都不能当门禁。

        因此，明令禁止（用户 2026-09-25 裁决 + 本轮实测）：
          1. **不许**为了让套件变绿而删掉本方法、翻掉断言、或改成 ``expectedFailure`` /
             ``skip`` —— 那等于把"探针瞄错类别"这个已付过学费的事实抹掉；
          2. **不许**把本方法的任何结果（红或绿）写进验收矩阵、门禁脚本或交接结论；
             需要"旧原语确实致命"的正向证据时，引用 ``lab3_run3_1315.txt``；
          3. 若要把它修成**拓扑同构**的 canary：孩子必须由 venv 启动器（trampoline）
             ``runtime\\avatar\\venv\\Scripts\\python.exe`` 起，并自报 pid；改完本标注作废，
             且必须重跑两键验收（``runtime/debug/_verify_liveness_0925.ps1``）而不是只看
             这里变绿。在此之前，本方法**红或绿都是预期内的**（见上面的同文件 A/B），
             登记于 ``AI_GIRLFRIEND_FUNCTIONAL_REPLICA_MASTER_STATE.json`` 的 ``open_items`` #25。
        """
        proc, pid = spawn_own_console_child()
        raised = None
        try:
            os.kill(pid, 0)
        except BaseException as exc:  # noqa: BLE001
            raised = '%s: %s' % (type(exc).__name__, str(exc)[:120])
        poison = flush_pending_exception()
        end = time.time() + 8.0
        while time.time() < end and pid_liveness.pid_probe(pid)['alive']:
            time.sleep(0.1)
        gone = not pid_liveness.pid_probe(pid)['alive']
        kill_child(proc, pid)
        self.assertTrue(gone,
                        'canary failed to reproduce the kill: os.kill(pid, 0) left a '
                        'cross-console listener-class child alive (raised=%s poison=%s) -- '
                        're-measure before this repo keeps calling it the root cause'
                        % (raised, poison))


if __name__ == '__main__':
    unittest.main(verbosity=2)
