###############################################################################
#  全局会话管理器 (Session Manager) — 单一生命周期实现（:8010）
#
#  本文件自 TASK_ID=AI_GIRLFRIEND_RUNTIME_LIFECYCLE_FINAL_REPAIR_01 起只做两件事：
#   1) 保持既有对外 API（routes.py / app.py / avatar_routes.py 不必改调用式）；
#   2) 把 create / destroy 全部委派给 server.session_lifecycle。
#
#  为什么不再是事实源：原先 ``sessions`` 这个 dict 就是唯一的会话真相，
#  而 pc / HumanPlayer / 渲染线程 / SegmentGate 台账各自散在别的对象里，
#  于是「任何一条退出路径都没法调用一个完整的释放函数」。现在真相在
#  SessionRecord 里，本模块只是它的读视图。
###############################################################################

import asyncio

from typing import Optional
from utils.logger import logger
from avatars.base_avatar import BaseAvatar

# MaxSessionError 从此只在**一处**定义。历史上它定义在本模块，rtc_manager 直接
# import；改到 session_lifecycle 里是为了让 reserve() 这个唯一的入口能抛它。
# 这里 re-export，保证 `from server.session_manager import MaxSessionError`
# 的既有 importer（rtc_manager、测试）拿到的还是同一个类对象。
from server.session_lifecycle import (  # noqa: F401
    MaxSessionError,
    RESIDENT_KINDS,
    session_lifecycle,
)


def _rand_session_id() -> str:
    """生成 UUID session ID"""
    import uuid
    return str(uuid.uuid4())


class SessionManager:
    """
    全局数字人会话管理器。

    统一管理 avatar_sessions 生命周期，并在脱离 WebRTC 时依然保持服务可用。
    """
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, "initialized"):
            self.build_session_fn = None
            self.initialized = True

    # ── 兼容读视图：``sessions`` 仍是 sessionid -> BaseAvatar ────────────
    @property
    def sessions(self) -> dict:
        """只读视图。写它不再有任何效果——占位 ``None`` 那套已经废掉了。"""
        out = {}
        for rec in list(session_lifecycle._records.values()):
            if rec.state == "destroyed":
                continue
            out[rec.session_id] = rec.avatar_session
        return out

    def set_max_session(self, n: int):
        """设置最大并发会话数"""
        session_lifecycle.set_max_session(n)

    @property
    def max_session(self) -> int:
        return session_lifecycle.max_session

    def init_builder(self, build_session_fn):
        """配置用于构建 avatar_session 的工厂函数"""
        self.build_session_fn = build_session_fn

    def get_session(self, sessionid: str) -> Optional[BaseAvatar]:
        """获取已存活的会话"""
        rec = session_lifecycle.get(sessionid)
        return None if rec is None else rec.avatar_session

    def has_session(self, sessionid: str) -> bool:
        """检查会话是否存在"""
        rec = session_lifecycle.get(sessionid)
        return bool(rec is not None and rec.avatar_session is not None)

    async def create_session(self, params: dict, sessionid: str = None,
                             kind: str = "offer") -> str:
        """
        在异步环境中创建一个新会话。

        与旧实现的两处实质差别（都是本轮契约要求的）：

        * 名额从 ``reserve()`` 那一刻就算占用。旧实现先写 ``sessions[sid]=None``
          占位、再在 ``active_count`` 里 ``if s is not None`` 把它排除掉，于是
          并发 /offer 能把 5 个名额穿到 6 个——生产日志里的 ``(6/5)`` 就是这么来的。
        * 构建失败必定当场回收，且失败原因进 ``reason``。旧实现里
          ``run_in_executor`` 一抛，占位键永久留在 dict 里，既不放名额也不会有人清。
        """
        if self.build_session_fn is None:
            raise Exception("SessionManager builder not initialized")

        if sessionid is None:
            sessionid = _rand_session_id()

        try:
            session_lifecycle.reserve(sessionid, kind=kind)
        except ValueError as exc:
            # 同 id 已在册：/whep 允许客户端自带 sessionid，重复提交应当被明确拒绝
            # 而不是静默复用另一个 pc。
            raise MaxSessionError(str(exc)) from exc

        session_lifecycle.mark_building(sessionid)
        logger.info('Creating sessionid=%s, current session num=%d',
                    sessionid, session_lifecycle.active_count())
        try:
            avatar_session = await asyncio.get_event_loop().run_in_executor(
                None, self.build_session_fn, sessionid, params
            )
        except BaseException as exc:  # noqa: BLE001 - 包括 CancelledError：名额必须还
            sessionid = await self._rollback(sessionid, f"build_failed:{type(exc).__name__}")
            raise
        if not session_lifecycle.activate(sessionid, avatar_session=avatar_session):
            # 慢构建撞上看门狗回收：产物不能再被采用，否则就是一个"在册对象为零、
            # 却真的在渲染并占着显存"的幽灵会话。就地释放。
            logger.error("session %s was reaped while building; releasing the orphan build",
                         sessionid)
            await session_lifecycle.destroy_session(sessionid, "build_raced_watchdog")
            raise MaxSessionError(f"session {sessionid} was reaped during build")
        return sessionid

    async def _rollback(self, sessionid: str, reason: str) -> str:
        await session_lifecycle.destroy_session(sessionid, reason)
        return sessionid

    def add_session(self, sessionid: str, avatar_session: BaseAvatar,
                    kind: str = "resident"):
        """同步添加静态或外部管理的会话（供非服务端入口调用）

        ``kind='resident'`` 让看门狗永久豁免它、且不占 ``max_session`` 名额：
        virtualcam / rtmp 模式在 app.py 里预置 session ``'0'`` 并自持渲染线程，
        那是一条常驻基础设施路径，不是浏览器会话，回收它等于把该模式打死。
        """
        try:
            session_lifecycle.reserve(sessionid, kind=kind,
                                      counted=(kind not in RESIDENT_KINDS))
        except MaxSessionError as exc:
            logger.error("add_session rejected id=%s kind=%s: %s", sessionid, kind, exc)
            raise
        except ValueError:
            # 已在册：幂等更新载荷即可，不重复占名额。
            pass
        session_lifecycle.activate(sessionid, avatar_session=avatar_session)

    def remove_session(self, sessionid: str, reason: str = "remove_session"):
        """销毁会话资源（兼容入口；真正的释放见 session_lifecycle.destroy_session）

        同步签名是历史包袱：它唯一的调用点在 aiortc 的回调里。这里只做「尽力
        异步触发」，并且**绝不**在这里阻塞等 join——在事件循环线程上等待渲染线程
        正是把整个 :8010 冻住的那条路。没有循环可跑时（解释器 teardown）退回
        同步清理台账。
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:  # pragma: no cover
            loop = None
        if loop is not None and loop.is_running():
            task = loop.create_task(session_lifecycle.destroy_session(str(sessionid), reason))
            task.add_done_callback(_consume)
            return
        session_lifecycle.release_all_sync(reason)


def _consume(task) -> None:
    """Retrieve a fire-and-forget task's outcome so asyncio logs nothing."""
    try:
        if not task.cancelled():
            task.exception()
    except Exception:  # pragma: no cover
        logger.debug("remove_session background task outcome retrieval failed", exc_info=True)


# 单例抛出
session_manager = SessionManager()
