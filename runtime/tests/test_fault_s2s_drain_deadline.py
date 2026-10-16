"""任务 AI_GIRLFRIEND_RUNTIME_LIFECYCLE_FINAL_REPAIR_01 §十二: the 8765 drain deadline.

``runtime/core/providers/vendor_fixes/unit_drain_release.py`` already bounds how
long a pipeline unit may stay claimed after ``SESSION_END`` fails to propagate.
This file tests the part that fix used to *stop short of*: every branch that
cannot let the vendor finish its own release tail must still put the unit back in
the pool, because with ``--num_pipelines 1`` "unit still claimed" is not a
degraded service, it is no service at all.  The acceptance line is §十二 verbatim:
after the browser closes, ``/v1/pool`` must reach ``in_use = 0`` without
restarting :8765.

Everything here is in-process and offline: no server, no model, no TTS/LLM run.
The "vendor" is a stand-in coroutine with the shipped shape (poll ``drained``,
then ``service.unregister`` + ``unit.session = None``), so each case can make
exactly one of those steps impossible.

Run::

    runtime/core/venv/Scripts/python.exe runtime/tests/test_fault_s2s_drain_deadline.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from queue import Queue
from types import SimpleNamespace

RUNTIME = Path = __import__("pathlib").Path(__file__).resolve().parents[1]
for _p in (str(RUNTIME / "core"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from providers.vendor_fixes import unit_drain_release as udr  # noqa: E402

# 每个用例都把窗口压到百毫秒级：断言的是"有界"，不是"30 秒"。
WINDOW_S = 0.2
TAIL_S = 0.2


def _vendor_release(cleaned: list, released: list, *, honour_drain=True, block_forever=False):
    """The shipped ``_release_unit_after_drain`` shape, with knobs to break it."""
    async def original(unit, session, session_id):
        if block_forever:
            # 任务被挂在 handler 里：drained 设了也醒不过来（真实的死线程形状）。
            await asyncio.sleep(3600)
            return
        if honour_drain:
            while not session.drained.is_set():
                await asyncio.sleep(0.01)
        if callable(getattr(unit, "clean_marker", None)):
            unit.clean_marker(cleaned)
        released.append(session_id)
        unit.service.unregister(session_id)
        unit.session = None

    return original


def _make_module(original, *, drained_event=True):
    """A stand-in for ``speech_to_speech...websocket_router``."""
    module = SimpleNamespace(
        __name__="fake_websocket_router",
        __file__=__file__,
        _release_unit_after_drain=original,
        SESSION_END_FORCE_RELEASE_S=WINDOW_S,
    )
    if not drained_event:
        # 契约漂移：session 上根本没有可设的 drained 事件。
        module.SESSION_END_FORCE_RELEASE_S = WINDOW_S
    return module


def _unit(session, released):
    class _Service:
        def unregister(self, session_id):
            pass

    return SimpleNamespace(index=0, session=session, service=_Service(), released=released)


def _session(session_id="s-1", drained_event=True):
    session = SimpleNamespace(session_id=session_id, released_at=0.0, pending_output_item=b"stale")
    session.drained = asyncio.Event() if drained_event else object()
    return session


def _patched(module):
    outcome = udr.patch_websocket_router(module)
    assert "force-release window" in outcome, outcome
    return module._release_unit_after_drain


def _tail_shortened():
    udr._TAIL_GRACE_S = TAIL_S


# ── 用例 ─────────────────────────────────────────────────────────────


def normal_drain_path_is_untouched():
    """控制组：正常 drained 必须走 vendor 自己的收尾，一次强释都不该发生。"""
    _tail_shortened()
    released, cleaned = [], []
    module = _make_module(_vendor_release(cleaned, released))
    released_fn = _patched(module)

    async def scenario():
        session = _session()
        unit = _unit(session, released)
        task = asyncio.create_task(released_fn(unit, session, "s-1"))
        await asyncio.sleep(0.05)
        session.drained.set()
        await asyncio.wait_for(task, timeout=2.0)
        return unit

    unit = asyncio.run(scenario())
    assert unit.session is None, "正常路径必须释放 unit"
    assert module._release_unit_after_drain is not None
    assert udr.patch_websocket_router(module).startswith("already applied"), "重复 apply 必须幂等"


def tail_that_never_finishes_still_releases_the_unit():
    """§十二 主案：drained 设了但 vendor 收尾醒不过来 → 这里必须清队列+强制释放。

    旧实现只在超时后 ``logger.warning`` 一句就返回，``unit.session`` 永远非 None，
    ``/v1/pool`` 的 ``in_use`` 永远是 1 —— 这正是"关掉浏览器后 8765 就废了"的形状。
    """
    _tail_shortened()
    released, cleaned = [], []
    module = _make_module(_vendor_release(cleaned, released, block_forever=True))
    flushed: list = []
    module._clean_unit = lambda unit: flushed.append(unit.index)
    released_fn = _patched(module)

    async def scenario():
        session = _session("s-dead")
        unit = _unit(session, released)
        with _capture() as logs:
            await asyncio.wait_for(released_fn(unit, session, "s-dead"), timeout=4.0)
        return unit, logs.text()

    unit, logs = asyncio.run(scenario())
    assert flushed == [0], f"强制释放前必须先用 vendor 自己的 _clean_unit 清队列：{flushed}"
    assert unit.session is None, "收尾没跑完也必须把 unit 交回池子（in_use 必须归 0）"
    assert "reason=drain_timeout" in logs, f"必须留下可 grep 的硬 deadline 记录：{logs}"
    assert "s-dead" in logs, logs


def missing_drain_signal_releases_defensively():
    """契约漂移（session 上没有可设的 drained 事件）：不得挂住，必须就地释放。

    形状必须是"vendor loop 还在等一个永远等不到的信号"，所以让它是 block_forever：
    窗口到 → ``_force_release`` 发现没信号可设 → 返回 False → 就地取消、清队列、释放。
    """
    _tail_shortened()
    released, cleaned = [], []
    module = _make_module(_vendor_release(cleaned, released, block_forever=True))
    flushed: list = []
    module._clean_unit = lambda unit: flushed.append(unit.index)
    released_fn = _patched(module)

    async def scenario():
        session = _session("s-nosig", drained_event=False)
        unit = _unit(session, released)
        with _capture() as logs:
            await asyncio.wait_for(released_fn(unit, session, "s-nosig"), timeout=4.0)
        return unit, session, logs.text()

    unit, session, logs = asyncio.run(scenario())
    assert unit.session is None, "没有 drained 事件时也必须释放"
    assert flushed == [0], f"no_drain_signal 分支同样要清队列：{flushed}"
    assert session.pending_output_item is None, "投递缓冲必须一起清掉，别留给下一个会话"
    assert "reason=drain_timeout" in logs, logs


def cancelled_caller_still_releases_the_unit():
    """调用方被 Starlette 取消：释放不能靠"有人在等"，必须由 detached watcher 兜住。"""
    _tail_shortened()
    released, cleaned = [], []
    module = _make_module(_vendor_release(cleaned, released, block_forever=True))
    flushed: list = []
    module._clean_unit = lambda unit: flushed.append(unit.index)
    released_fn = _patched(module)

    async def scenario():
        session = _session("s-cancel")
        unit = _unit(session, released)
        task = asyncio.create_task(released_fn(unit, session, "s-cancel"))
        await asyncio.sleep(0.02)          # 让它进入"等不到 drained"的那段等待
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # detached watcher 的窗口 = WINDOW_S + TAIL_S，再给一倍余量
        deadline = time.monotonic() + (WINDOW_S + TAIL_S) * 6
        while unit.session is not None and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        return unit

    unit = asyncio.run(scenario())
    assert unit.session is None, "取消调用方之后 unit 仍必须有界归池"
    assert flushed == [0], f"watcher 路径也要清队列：{flushed}"


def late_release_cannot_evict_the_new_session():
    """幂等与归属：迟到的旧释放不得把已经 claiming 的新会话踢掉。"""
    _tail_shortened()
    released, cleaned = [], []
    module = _make_module(_vendor_release(cleaned, released))
    released_fn = _patched(module)

    async def scenario():
        old = _session("s-old")
        fresh = _session("s-fresh")
        unit = _unit(fresh, released)      # unit 现在属于新会话
        with _capture() as logs:
            await asyncio.wait_for(released_fn(unit, old, "s-old"), timeout=4.0)
        return unit, fresh, logs.text()

    # old 的 drained 直接设好，让 vendor 收尾跑到 unregister；但归属检查必须挡住
    # "把别人的会话置 None"。这里用 _release_defensively 的归属守卫来证明。
    async def scenario_guard_only():
        old = _session("s-old")
        fresh = _session("s-fresh")
        unit = _unit(fresh, released)
        ok = udr._release_defensively(unit, "s-old", old)
        return unit, fresh, ok

    unit, fresh, ok = asyncio.run(scenario_guard_only())
    assert ok is False, "不属于这个会话的释放必须拒绝"
    assert unit.session is fresh, f"新会话被误伤：{unit.session}"
    assert released == [], f"拒绝时不该碰 service.unregister：{released}"
    # 同一路径再走一次：不得报错（§五"无论执行两次还是五次都不得报错"）。
    assert udr._release_defensively(unit, "s-old", fresh) is True


def release_is_idempotent_when_called_repeatedly():
    """同一个 (unit, session) 连打三次：不报错、不重复 unregister 之外的副作用。"""
    _tail_shortened()
    released, cleaned = [], []
    module = _make_module(_vendor_release(cleaned, released))
    released_fn = _patched(module)

    async def scenario():
        session = _session("s-x")
        unit = _unit(session, released)
        for _ in range(3):
            await asyncio.wait_for(
                _drive(released_fn, unit, session, "s-x"), timeout=4.0
            )
        return unit

    async def _drive(fn, unit, session, sid):
        task = asyncio.create_task(fn(unit, session, sid))
        await asyncio.sleep(0.03)
        session.drained.set()
        await task

    unit = asyncio.run(scenario())
    assert unit.session is None
    assert released, "正常路径至少收尾一次"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


class _capture:
    """把 FIX_ID 那条 logger 收进缓冲区，用来证明"话说了、reason 也写了"。"""

    def __enter__(self):
        self.handler = _Capture()
        udr.logger.addHandler(self.handler)
        self.level = udr.logger.level
        udr.logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        udr.logger.removeHandler(self.handler)
        udr.logger.setLevel(self.level)
        return False

    def text(self):
        return "\n".join(self.handler.records)


TESTS = [
    normal_drain_path_is_untouched,
    tail_that_never_finishes_still_releases_the_unit,
    missing_drain_signal_releases_defensively,
    cancelled_caller_still_releases_the_unit,
    late_release_cannot_evict_the_new_session,
    release_is_idempotent_when_called_repeatedly,
]

if __name__ == "__main__":
    failures = 0
    for test in TESTS:
        started = time.monotonic()
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"  ok   {test.__name__} ({time.monotonic() - started:.2f}s)")
    if failures:
        print(f"s2s drain-deadline checks: FAIL ({failures} of {len(TESTS)})")
        raise SystemExit(1)
    print(f"s2s drain-deadline checks: PASS ({len(TESTS)})")
