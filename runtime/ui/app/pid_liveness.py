# -*- coding: utf-8 -*-
"""判活探针：只问「这个 pid 还在不在」，并且保证**问**这个动作本身杀不死对方。

为什么专门写这一个模块（2026-09-25，TASK_ID=AI_GIRLFRIEND_RUNTIME_LIFECYCLE_FINAL_REPAIR_01）
------------------------------------------------------------------
本仓库此前用 ``os.kill(pid, 0)`` 回答这个问题 —— 教科书写法，在很多语言/系统上是无害的。
在 Windows + CPython 上它不是。实测见
``runtime/debug/pid_probe/lab3_cross_console_kill0.py``（结果 ``lab3_run2.txt``）：

* ``CTRL_C_EVENT`` 的数值**就是 0**，所以 ``sig=0`` 进入的是 CPython 的 console 分支：
  先 ``GenerateConsoleCtrlEvent(0, pid)``；
* 这一调用失败时（**目标不与控制方共享同一个 console** 就必然失败），CPython 不报错退出，
  而是**掉进** ``OpenProcess(PROCESS_TERMINATE)`` + ``TerminateProcess(handle, 0)``；
* 于是目标以退出码 ``0x00000000`` 当场死亡：不跑 ``atexit``、不给 ``faulthandler`` 留栈、
  没有 WER 事件、也**不产生任何新进程**（凶手是一个早就活着、一直在跑的进程，进程快照里
  看不见它）；控制方那边只会看到一条被 ``except`` 吃掉的异常。
* 对照矩阵（同一台机器、同一解释器，唯一的变量是 console 归属）：
  跨 console：调用方有 console / CREATE_NO_WINDOW / DETACHED 三种拓扑**全部**把目标打成
  ``0x00000000``（R1/R2/R3/R3b）；同 console 的目标则毫发无损、``os.kill`` 自称成功（R4）；
  ``os.kill(pid, SIGTERM)`` 给出 ``0x0000000F``（R5，正向对照 —— 没有它，"状态 0" 什么都
  不是证据）。此前 ``oskill0_selfaim.py`` 得出的「INERT」结论之所以没错却没用：它只能瞄准
  **自己**，而自己永远在自己的 console 里，跨 console 那条分支根本不会被触发。

对本栈的意义：:7860 控制面板与 :8010 数字人服务从来不属于同一个 console，所以
``avatar_admin._pid_alive(listener_pid)`` 每次刷新页面都是在向数字人进程发一发
``TerminateProcess(h, 0)`` —— 这与历史上「:8010 静默消失、状态码 0、什么日志都不留」逐条
吻合。判活这件事不能留给"谁调的、拓扑对不对"的运气，所以这里只用一个**只申请查询权限**的
句柄回答。

契约
----
* 只申请 ``SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION``，绝不申请
  ``PROCESS_TERMINATE`` —— 这是整个模块存在的理由。
* ``OpenProcess`` 以 ``ERROR_ACCESS_DENIED`` 失败意味着**对象存在**、只是我们没权限，
  因此判活为 ``True``；``ERROR_INVALID_PARAMETER`` 才是「没有这个 pid」。
* 回答不了的一律 ``False``（与旧实现一致）：面板宁可把还在的人说成不在，也绝不能把
  不在的人说成在 —— 但这条保守只在**判活**上成立，杀不死人这件事由上面的权限选择保证。
* POSIX 一侧 ``sig=0`` 确实是无副作用的空信号，保留原写法并说明差异。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes as wt

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 0x00000103
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87

# 本模块刻意**不**定义、也**不**使用 PROCESS_TERMINATE。谁想在这里加它，先回去读上面那段。
_QUERY_MASK = SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION

_IS_WINDOWS = os.name == 'nt'
_kernel32 = None


def _win():
    global _kernel32
    if _kernel32 is None:
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = wt.HANDLE
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k32.GetExitCodeProcess.restype = wt.BOOL
        k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
        k32.CloseHandle.restype = wt.BOOL
        k32.CloseHandle.argtypes = [wt.HANDLE]
        _kernel32 = k32
    return _kernel32


def pid_probe(pid: int) -> dict:
    """一个 pid 的存活判决 + 判决是怎么来的（``how`` 是给人看/给日志用的，不做分支）。"""
    out = {'pid': int(pid), 'alive': False, 'how': ''}
    if pid <= 0:
        out['how'] = 'pid<=0'
        return out

    if not _IS_WINDOWS:
        # POSIX：0 号空信号不投递任何东西，这里可以照旧。
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            out['how'] = 'posix:ESRCH'
            return out
        except PermissionError:
            out['alive'] = True
            out['how'] = 'posix:EACCES=exists'
            return out
        except OSError as exc:
            out['how'] = 'posix:errno=%s' % getattr(exc, 'errno', '?')
            return out
        out['alive'] = True
        out['how'] = 'posix:signal_0'
        return out

    k32 = _win()
    ctypes.set_last_error(0)          # 成功的 OpenProcess 不会清掉上一次的错误码
    handle = k32.OpenProcess(_QUERY_MASK, False, int(pid))
    raw = getattr(handle, 'value', handle)
    gle = ctypes.get_last_error()
    if not raw or raw == 0xFFFFFFFFFFFFFFFF:
        if gle == ERROR_ACCESS_DENIED:
            out['alive'] = True       # 对象在，只是不给查 —— "存在"是被证实的
            out['how'] = 'open_denied_but_exists(gle=5)'
        elif gle == ERROR_INVALID_PARAMETER:
            out['how'] = 'no_such_pid(gle=87)'
        else:
            out['how'] = 'open_failed(gle=%d)' % gle
        return out
    try:
        code = wt.DWORD(0)
        ctypes.set_last_error(0)
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            out['how'] = 'get_exit_code_failed(gle=%d)' % ctypes.get_last_error()
            return out
        out['alive'] = (code.value == STILL_ACTIVE)
        out['how'] = 'exit_code=0x%08X' % code.value
        return out
    finally:
        k32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """``os.kill(pid, 0)`` 的替代品：只读权限，问不出活就报活不了，永远不杀进程。"""
    return bool(pid_probe(pid)['alive'])


def explain(pid: int) -> str:
    """一行人类可读的判决来源，供面板/审计直接引用。"""
    row = pid_probe(pid)
    return 'pid=%s alive=%s how=%s' % (row['pid'], row['alive'], row['how'])
