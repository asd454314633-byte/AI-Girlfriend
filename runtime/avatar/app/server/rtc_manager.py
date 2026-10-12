###############################################################################
#  WebRTC 连接管理 + RTC 音频/视频接收
#
#  生命周期契约（TASK_ID=AI_GIRLFRIEND_RUNTIME_LIFECYCLE_FINAL_REPAIR_01）
#  ------------------------------------------------------------------
#  本模块**不再**自己保存 PeerConnection 集合，也**不再**自己决定何时释放。
#  它只做三件事：建 pc、把 pc 交给 session_lifecycle、以及在每条退出路径上
#  调用同一个 destroy_session(sessionid, reason)。
#
#  为什么必须这样：旧实现里 ``self.pcs`` 是一个不带 key 的 set，
#  ``session_manager.sessions`` 是另一个容器，二者没有映射关系，所以
#      · _create_pc_and_answer 在 setRemoteDescription 抛错时**无法**回滚
#        （它手里只有 pc，没有"这个 pc 属于哪个 session"的可寻址记录）；
#      · rtcpush 分支的清理漏掉 remove_session，与 /offer 行为不一致；
#      · 36 个 session 泄漏在生产日志里既没报错也没被回收。
#  现在 sessionid 是唯一寻址键，回滚只需要一行。
###############################################################################

import json
import asyncio
from typing import Optional

from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceServer, RTCConfiguration
from aiortc.rtcrtpsender import RTCRtpSender

from utils.logger import logger


from server.session_manager import session_manager
from server.session_manager import MaxSessionError
from server.session_lifecycle import session_lifecycle


class RTCManager:
    """
    WebRTC 连接管理器。

    管理 WebRTC 信令与轨道收发；会话的**生死**在 session_lifecycle。
    """

    def __init__(self, opt):
        """
        Args:
            opt: 全局配置
        """
        self.opt = opt

    # ── 供旧调用方读的兼容视图（不再是事实源）────────────────────────
    @property
    def pcs(self) -> set:
        return {r.pc for r in map(session_lifecycle.get, session_lifecycle.session_ids())
                if r is not None and r.pc is not None}

    # ────────────────────────────────────────────────────────────────────
    #  建连
    # ────────────────────────────────────────────────────────────────────
    async def _create_pc_and_answer(self, avatar_session, sessionid: str, offer,
                                    kind: str = "offer") -> RTCPeerConnection:
        """创建 PeerConnection、添加轨道、SDP 交换，返回已完成 answer 的 pc

        整段被一个回滚兜住（旧实现没有这个兜）：``pc.getTransceivers()[1]``、
        ``setRemoteDescription``、``createAnswer`` 里任何一步抛——SDP 少一行、
        客户端送来非法 type、编解码协商失败——都必须把刚占住的名额还回去。
        不兜住的话那一次失败的 /offer 就永久占一个槽，5 次之后就再没有人连得上。
        """
        pc: Optional[RTCPeerConnection] = None
        player = None
        try:
            ice_server = RTCIceServer(urls=self.opt.stun)
            pc = RTCPeerConnection(
                configuration=RTCConfiguration(iceServers=[ice_server])
            )

            # The observer only *reports*.  It must not be the reaper: aiortc
            # never emits ``disconnected`` (see rtcpeerconnection's own comment
            # "we do not have a disconnected state"), and empirically it emitted
            # ``failed`` zero times across 71 sessions, so a callback-only design
            # is precisely what let sessions leak.  The watchdog reads
            # pc.connectionState instead.  A fired callback is still a fast path.
            @pc.on("connectionstatechange")
            async def on_connectionstatechange():
                state = pc.connectionState
                session_lifecycle.note_connection_state(sessionid, state)
                logger.info("Connection state is %s (session=%s)", state, sessionid)
                if state in ("failed", "closed"):
                    # Awaited, not fire-and-forget: the release must be ordered
                    # ahead of any reuse of this slot.  destroy_session is
                    # idempotent, so the watchdog arriving here first is a no-op.
                    await session_lifecycle.destroy_session(sessionid, f"pc_{state}")

            from server.webrtc import HumanPlayer
            player = HumanPlayer(avatar_session)
            pc.addTrack(player.audio)
            pc.addTrack(player.video)

            # 设置编解码器偏好
            capabilities = RTCRtpSender.getCapabilities("video")
            preferences = list(filter(lambda x: x.name == "H264", capabilities.codecs))
            preferences += list(filter(lambda x: x.name == "VP8", capabilities.codecs))
            preferences += list(filter(lambda x: x.name == "rtx", capabilities.codecs))
            transceiver = pc.getTransceivers()[1]
            transceiver.setCodecPreferences(preferences)

            # Attach before the SDP round-trip: from here on the record can
            # answer "which pc owns this sessionid", which is what makes the
            # rollback below possible at all.
            session_lifecycle.activate(sessionid, pc=pc, player=player,
                                       avatar_session=avatar_session)

            await pc.setRemoteDescription(offer)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

            return pc
        except BaseException as exc:
            reason = f"answer_failed:{type(exc).__name__}"
            logger.error("session %s WebRTC setup failed (%s); rolling back the slot",
                         sessionid, reason)
            if pc is not None and session_lifecycle.get(sessionid) is not None:
                await session_lifecycle.destroy_session(sessionid, reason)
            elif pc is not None:
                try:
                    await pc.close()
                except Exception:  # pragma: no cover
                    logger.debug("rollback pc.close failed", exc_info=True)
                if player is not None:
                    await asyncio.get_event_loop().run_in_executor(
                        None, player.force_stop, f"rollback:{sessionid}")
            raise

    # ────────────────────────────────────────────────────────────────────
    #  信令入口
    # ────────────────────────────────────────────────────────────────────
    async def handle_offer(self, request):
        """处理 WebRTC offer 信令"""
        try:
            params = await request.json()
            offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
        except (KeyError, ValueError, TypeError) as exc:
            # A malformed body never reserves a slot, so there is nothing to
            # release — but it is still a rejected offer worth counting.
            logger.warning("Rejecting offer with malformed SDP: %s", exc)
            return self._reject(str(exc))

        try:
            sessionid = await session_manager.create_session(params)
        except MaxSessionError as e:
            logger.warning("Rejecting offer: %s", e)
            return self._reject(str(e))

        logger.info('offer sessionid=%s', sessionid)

        try:
            pc = await self._create_pc_and_answer(
                session_manager.get_session(sessionid), sessionid, offer,
            )
        except MaxSessionError as e:
            return self._reject(str(e))
        except Exception as e:
            # _create_pc_and_answer already rolled the slot back; all that is
            # left is to say so on the wire instead of 500-ing, so the browser
            # can release-and-retry rather than sit on an ambiguous response.
            logger.exception("offer handling failed for session %s", sessionid)
            return self._reject(f"webrtc setup failed: {e}")

        return web.Response(
            content_type="application/json",
            text=json.dumps({
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
                "sessionid": sessionid,
            }),
        )

    @staticmethod
    def _reject(msg: str) -> web.Response:
        """The capacity/answer rejection shape the browser already speaks.

        Kept at HTTP 200 + ``code:-1`` because that is :8010's existing external
        contract (``/whep`` is the asymmetric one, answering 503) and
        ``providers/manifests/avatar.json`` describes it.  ``session_lifecycle``
        still counts the rejection, so the operator can tell "5/5 because of a
        leak" from "5/5 because five real tabs are open".
        """
        return web.Response(
            content_type="application/json",
            text=json.dumps({"code": -1, "msg": msg}),
        )

    async def handle_whep(self, request):
        """
        处理 WHEP 信令（WebRTC HTTP Egress Protocol）

        - 请求 body 为裸 SDP offer（Content-Type: application/sdp）
        - 扩展参数通过 query string 传入（avatar, tts, tts_server 等）
        - 返回 SDP answer（Content-Type: application/sdp）
        - sessionid 通过 X-Session-ID 响应头返回
        """
        params = dict(request.query)
        # 客户端可通过 query param 自定义 sessionid，不传则自动生成
        client_sid = params.pop("sessionid", None)

        offer_sdp = await request.text()
        offer = RTCSessionDescription(sdp=offer_sdp, type="offer")

        try:
            sessionid = await session_manager.create_session(params, sessionid=client_sid,
                                                             kind="whep")
        except MaxSessionError as e:
            logger.warning("Rejecting whep: %s", e)
            return web.Response(
                status=503,
                content_type="text/plain",
                text=str(e),
            )
        logger.info("whep sessionid=%s", sessionid)

        try:
            rec = session_lifecycle.get(sessionid)
            pc = await self._create_pc_and_answer(
                rec.avatar_session if rec else None, sessionid, offer, kind="whep")
        except Exception as e:
            logger.exception("whep handling failed for session %s", sessionid)
            return web.Response(status=500, content_type="text/plain", text=str(e))

        return web.Response(
            status=201,
            content_type="application/sdp",
            text=pc.localDescription.sdp,
            headers={"X-Session-ID": sessionid},
        )

    async def handle_rtcpush(self, push_url, sessionid: str):
        """RTCPush 模式：主动推流

        Registered as ``resident``: it is infrastructure this service starts by
        itself at boot, so the reaper must not treat "nobody is watching it" as a
        leak.  A genuine ``failed`` still releases it — and unlike the old code,
        that release now also returns the session, because the offer and push
        paths finally share one destroy function.
        """
        import aiohttp
        await session_manager.create_session({}, sessionid, kind="resident")
        rec = session_lifecycle.get(sessionid)
        avatar_session = rec.avatar_session if rec else None

        pc = RTCPeerConnection()

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logger.info("Connection state is %s (rtcpush session=%s)",
                        pc.connectionState, sessionid)
            if pc.connectionState in ("failed", "closed"):
                await session_lifecycle.destroy_session(
                    sessionid, f"rtcpush_{pc.connectionState}")

        from server.webrtc import HumanPlayer
        player = HumanPlayer(avatar_session)
        pc.addTrack(player.audio)
        pc.addTrack(player.video)
        session_lifecycle.activate(sessionid, pc=pc, player=player,
                                   avatar_session=avatar_session)

        await pc.setLocalDescription(await pc.createOffer())

        async with aiohttp.ClientSession() as session:
            async with session.post(push_url, data=pc.localDescription.sdp) as response:
                answer_sdp = await response.text()

        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer_sdp, type='answer')
        )

    # ────────────────────────────────────────────────────────────────────
    #  关停
    # ────────────────────────────────────────────────────────────────────
    async def shutdown(self, reason: str = "shutdown"):
        """关闭所有会话——含 pc、播放线程与台账。

        旧实现只 ``pc.close()`` 然后 ``pcs.clear()``，从不清
        ``session_manager.sessions``，也就是说优雅退出会留下一桌子"在册"会话。
        现在它直接走同一个 destroy_session，行为与浏览器主动 release 逐字一致。
        """
        await session_lifecycle.shutdown_all(reason=reason)
