###############################################################################
#  服务器路由 — 统一异常处理的 API 路由
###############################################################################

import json
import asyncio
import os
import time
from aiohttp import web

from utils.logger import logger


# ─── 路由工具函数 ──────────────────────────────────────────────────────────

def json_ok(data=None):
    """返回成功 JSON 响应"""
    body = {"code": 0, "msg": "ok"}
    if data is not None:
        body["data"] = data
    return web.Response(
        content_type="application/json",
        text=json.dumps(body),
    )


def json_error(msg: str, code: int = -1):
    """返回错误 JSON 响应"""
    return web.Response(
        content_type="application/json",
        text=json.dumps({"code": code, "msg": str(msg)}),
    )


from server.session_manager import session_manager
from server.session_lifecycle import session_lifecycle
from server.avatar_routes import setup_avatar_routes
from server.idempotency import UNTAGGED, default_gate as segment_gate
from server import playback

def get_session(request, sessionid: str):
    """从 app 中获取 session 实例

    顺带盖章：能按 sessionid 打到本服务的任何一个请求，都是"这个客户端还在"
    的证据。看门狗靠这个时间戳区分"页面活着但没在说话"和"页面早没了只是没人
    通知服务端"——前者绝不能回收（验收要求单页长时间稳态 active session == 1），
    后者必须回收。
    """
    _touch(sessionid)
    return session_manager.get_session(sessionid)


def _touch(sessionid) -> None:
    """Any HTTP call naming a session is proof the client is still there.

    The reaper's ``never_connected`` deadline is what keeps abandoned tabs from
    burning slots; a tab that *is* talking to us must never be reaped for
    staleness, so every route that resolves a session stamps it first.
    """
    if sessionid:
        session_lifecycle.touch(str(sessionid))


async def lipsync_mode(request):
    """Per-WebRTC-session composition selection; never changes global opt/model."""
    try:
        params = await request.json() if request.method == 'POST' else request.query
        sid = str(params.get('sessionid', ''))
        if not sid or sid == '0':
            return web.json_response({'code': -1, 'msg': 'sessionid required'}, status=400)
        avatar_session = get_session(request, sid)
        if avatar_session is None:
            return web.json_response({'code': -1, 'msg': 'session not found'}, status=404)
        if not hasattr(avatar_session, 'lipsync_mode_status'):
            return web.json_response({'code': -1, 'msg': 'lipsync mode unsupported by this avatar'}, status=409)
        if request.method == 'POST':
            state = avatar_session.request_lipsync_mode(params.get('mode'))
        else:
            state = avatar_session.lipsync_mode_status()
        return web.json_response({'code': 0, 'data': state})
    except (ValueError, TypeError) as exc:
        return web.json_response({'code': -1, 'msg': str(exc)}, status=400)
    except Exception:
        logger.exception('lipsync mode API failure')
        return web.json_response({'code': -1, 'msg': 'lipsync mode API failed'}, status=500)


# ─── 路由处理函数 ──────────────────────────────────────────────────────────

async def human(request):
    """文本输入（echo/chat 模式），支持 voice/emotion 参数"""
    try:
        params: dict = await request.json()

        sessionid: str = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")

        if params.get('interrupt'):
            _cancel_prefetched_responses(sessionid, params)
            # flush_talk() only drains tts.msgqueue + asr.queue; everything past
            # that keeps rendering.  cancel_turn() is the ONE place on this
            # service that decides what "cancel the current response" means: it
            # retires the named response (a barge-in reply that never enqueued
            # anything is still blocked) and raises the gate's watermark, which
            # is what stops the interrupted response's late audio from slipping
            # into the next one (任务 §7 H「旧 response 插入新 response」).
            segment_gate.cancel_turn(
                sessionid,
                response_id=params.get('response_id'),
                reason=params.get('reason') or 'user_interrupt',
            )
            avatar_session.flush_talk()

        datainfo = {}
        if params.get('tts'):  # tts 参数透传（voice, emotion 等）
            datainfo['tts'] = params.get('tts')

        if params['type'] == 'echo':
            # Same key and the same single rule as /humanaudio (``admit_turn``),
            # so a client that adopts (response_id, segment_index) gets one dedupe
            # semantic for both audio-in and text-in.
            verdict, allowed = segment_gate.admit_turn(sessionid, params, kind='human(echo)')
            if not allowed:
                return json_ok(data={'segment_gate': verdict.to_dict(), 'enqueued': False})
            avatar_session.put_msg_txt(params['text'], datainfo)
        elif params['type'] == 'chat':
            llm_response = request.app.get("llm_response")
            if llm_response:
                asyncio.get_event_loop().run_in_executor(
                    None, llm_response, params['text'], avatar_session, datainfo
                )

        return json_ok()
    except Exception as e:
        logger.exception('human route exception:')
        return json_error(str(e))


def _cancel_prefetched_responses(sessionid, params):
    # Retire uploads already in flight but not yet admitted by the segment gate.
    ids = params.get('response_ids', [])
    if isinstance(ids, list):
        for rid in ids[:64]:
            segment_gate.cancel_turn(sessionid, response_id=rid,
                                     reason=params.get('reason') or 'user_interrupt')


async def interrupt_talk(request):
    """打断当前说话

    ``response_id`` 可选但强烈建议带上：带上它，被打断的那一条回复即使
    还没向 :8010 投过任何一段，也会被点名拉黑（见 idempotency.cancel_turn）。
    """
    try:
        params = await request.json()
        sessionid = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        _cancel_prefetched_responses(sessionid, params)
        decision = segment_gate.cancel_turn(
            sessionid,
            response_id=params.get('response_id'),
            reason=params.get('reason') or 'user_interrupt',
        )
        avatar_session.flush_talk()
        return json_ok(data={'turn_cancel': decision})
    except Exception as e:
        logger.exception('interrupt_talk exception:')
        return json_error(str(e))


async def humanaudio(request):
    """上传音频文件

    幂等闸门（任务 §7 H）：``response_id`` + ``segment_index`` 是本服务唯一的
    "这段音频有没有入过队" 依据。浏览器（``runtime/ui/app/avatar-sync.js``）
    两者都带，``segment_index`` 在同一 ``response_id`` 内单调递增且**重试时不变**，
    所以一次网络重试不会被当成新的一段。两者缺失时闸门判 ``untagged`` 并原样
    放行。拦截与否取决于本进程启动时的 ``AVATAR_SEGMENT_GATE``：本部署由
    ``runtime/core/providers/manifests/avatar.json`` 的 ``launch.env`` 以 ``enforce``
    拉起（``observe`` 只记账、不改任何音频行为）。
    """
    try:
        form = await request.post()
        sessionid = str(form.get('sessionid', ''))
        fileobj = form["file"]
        filebytes = fileobj.file.read()

        datainfo = {}

        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")

        verdict, allowed = segment_gate.admit_turn(sessionid, form, kind='humanaudio')
        if not allowed:
            # A rejected retry still answers code 0: the client's own retry loop
            # (``drainUploads``) must converge, not re-post the same WAV forever.
            return json_ok(data={'segment_gate': verdict.to_dict(), 'enqueued': False})

        datainfo = dict(sessionid=sessionid, response_id=verdict.response_id,
                        segment_index=verdict.segment_index,
                        reading_key=str(form.get('reading_key', ''))[:200])
        playback.record('avatar_accept', datainfo, bytes=len(filebytes))
        avatar_session.put_audio_file(filebytes, datainfo)
        if verdict.decision == UNTAGGED:
            return json_ok()
        return json_ok(data={'segment_gate': verdict.to_dict(), 'enqueued': True})
    except Exception as e:
        logger.exception('humanaudio exception:')
        return json_error(str(e))


async def set_audiotype(request):
    """设置自定义状态（动作编排）"""
    try:
        params = await request.json()
        sessionid = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        avatar_session.set_custom_state(params['audiotype'])
        return json_ok()
    except Exception as e:
        logger.exception('set_audiotype exception:')
        return json_error(str(e))


async def record(request):
    """录制控制"""
    try:
        params = await request.json()
        sessionid = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        if params['type'] == 'start_record':
            avatar_session.start_recording()
        elif params['type'] == 'end_record':
            avatar_session.stop_recording()
        return json_ok()
    except Exception as e:
        logger.exception('record exception:')
        return json_error(str(e))


async def is_speaking(request):
    """查询是否正在说话"""
    params = await request.json()
    sessionid = params.get('sessionid', '')
    avatar_session = get_session(request, sessionid)
    if avatar_session is None:
        return json_error("session not found")
    return json_ok(data=avatar_session.is_speaking())

async def sse_handler(request):
    """SSE 事件流，推送服务器状态更新到客户端"""
    sessionid = request.query.get('sessionid', '')
    avatar_session = session_manager.get_session(sessionid)
    if avatar_session is None:
        return json_error("session not found")

    response = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*',
        }
    )
    await response.prepare(request)

    import queue
    msgqueue = queue.Queue()
    avatar_session.add_msgqueue(msgqueue)

    try:
        while True:
            try:
                msg = msgqueue.get_nowait()
                await response.write(f"data: {msg}\n\n".encode('utf-8'))
            except queue.Empty:
                await asyncio.sleep(0.01)
    except (asyncio.CancelledError, ConnectionResetError):
        logger.info('SSE connection closed for session: %s', sessionid)
    finally:
        if msgqueue in avatar_session.msgqueues:
            avatar_session.msgqueues.remove(msgqueue)

    return response


async def admin_config(request):
    """Admin: 获取全局配置参数"""
    try:
        opt = request.app.get("opt")
        if opt:
            return json_ok(data={"config": vars(opt)})
        return json_error("Config not found")
    except Exception as e:
        logger.exception('admin_config exception:')
        return json_error(str(e))


async def admin_sessions(request):
    """Admin: 获取活跃的会话及其配置

    旧实现 ``if avatar_session:`` 直接跳过 ``None`` 占位，于是"占位泄漏"和
    "名额被穿透"恰好在这唯一的观测接口上不可见——生产里 6/5 爆表持续了很久
    而这里看着是 5/5。现在逐条会话带 state / age / pc 连接态，占位也报。
    """
    try:
        snap = session_lifecycle.snapshot()
        by_id = {s['sessionid']: s for s in snap['sessions']}
        sessions_info = []
        for sid in session_lifecycle.session_ids():
            rec = session_lifecycle.get(sid)
            if rec is None:
                continue
            avatar_session = rec.avatar_session
            s_opt = getattr(avatar_session, 'opt', None) if avatar_session else None
            s_data = {
                "sessionid": sid,
                "speaking": avatar_session.is_speaking() if (
                    avatar_session and hasattr(avatar_session, 'is_speaking')) else False,
                "recording": getattr(avatar_session, 'recording', False) if avatar_session else False,
                "state": rec.state,
                "kind": rec.kind,
                "age_s": rec.age_s,
                "idle_s": rec.idle_s,
                "created_at": round(rec.created_wall, 3),
                "last_activity": round(rec.last_activity_wall, 3),
                "pc_connection_state": rec.connection_state(),
                "ice_connection_state": rec.ice_connection_state(),
                "media": session_lifecycle._media(rec),
            }
            extra = by_id.get(sid, {})
            s_data['media_ready'] = extra.get('media_ready')
            if s_opt:
                s_data.update({
                    "model": getattr(s_opt, "model", ""),
                    "avatar_id": getattr(s_opt, "avatar_id", ""),
                    "REF_FILE": getattr(s_opt, "REF_FILE", ""),
                    "transport": getattr(s_opt, "transport", ""),
                    "batch_size": getattr(s_opt, "batch_size", 0),
                    "customopt": getattr(s_opt, "customopt", []),
                })
            sessions_info.append(s_data)
        return json_ok(data={
            "sessions": sessions_info,
            "active_sessions": snap['active_sessions'],
            "max_session": snap['max_session'],
            "totals": snap['totals'],
        })
    except Exception as e:
        logger.exception('admin_sessions exception:')
        return json_error(str(e))


async def admin_segment_gate(request):
    """Admin（只读）: 幂等闸门 / 统一 cancel 的当前判决.

    Read-only on purpose — it neither enqueues nor flushes, so an operator can
    confirm which mode :8010 is actually running in (``observe`` vs ``enforce``)
    and which responses a cancel retired without touching live audio.
    """
    try:
        sessionid = request.query.get('sessionid', '')
        payload = {'mode': segment_gate.mode, 'env': 'AVATAR_SEGMENT_GATE'}
        if sessionid:
            payload['session'] = segment_gate.snapshot(sessionid)
        return json_ok(data=payload)
    except Exception as e:
        logger.exception('admin_segment_gate exception:')
        return json_error(str(e))


async def admin_playback(request):
    return json_ok(data=playback.snapshot(request.query.get('sessionid', '')))


# ─── 显式 Release 与运行时可观测性（生命周期契约 §6 / §14）─────────────────

async def session_release(request):
    """浏览器/运维主动交还一个会话名额。**幂等，且永不报错。**

    这一条是 session ownership 闭环里"客户端侧加速"的那半：页面 pagehide /
    beforeunload / teardown / 重连前替换旧 pc 时，用 keepalive fetch 打这里，
    服务端不必等 ICE 超时就能把槽位腾出来。

    但服务端仍是最终权威：浏览器崩了、断网了、来不及发，看门狗的
    ``never_connected`` / ``disconnected_timeout`` 照样回收。所以本函数对
    "根本不认识的 sessionid"（来自上个进程、或已被回收）返回
    ``released:false`` 而不是 404 —— 调用方要的是"这个名额不存在了"这个
    结果，不是"我有没有成功把它删掉"这段过程。

    请求体：``{"sessionid": "...", "reason": "pagehide|reload|reconnect|switch|manual"}``
    """
    try:
        try:
            params = await request.json()
        except Exception:
            params = {}
        sessionid = str(params.get('sessionid') or request.query.get('sessionid') or '')
        reason = str(params.get('reason') or request.query.get('reason') or 'client_release')
        if not sessionid or sessionid == '0':
            return json_error("sessionid is required")
        outcome = await session_lifecycle.destroy_session(sessionid, f"client_{reason}")
        return json_ok(data=outcome)
    except Exception as e:
        logger.exception('session_release exception:')
        return json_error(str(e))


async def admin_runtime(request):
    """Admin（只读）: 一个端点回答"现在到底谁占着 8010"。

    存在理由见任务 §14：本轮之前的故障全部靠猜，因为
    ``/api/admin/sessions`` 既不看 pc 状态、也不看时长、还会跳过 ``None`` 占位
    ——而恰恰是那些占位在穿透 ``max_session``。这里把三类数字一次给全：
    进程所有权（来自 Supervisor 落盘的 state 文件）、逐会话状态、累计计数。
    """
    try:
        payload = session_lifecycle.snapshot()
        payload['gate'] = {'mode': segment_gate.mode}
        payload['process'] = _process_ownership()
        opt = request.app.get("opt")
        if opt is not None:
            payload['avatar_id'] = getattr(opt, 'avatar_id', '')
            payload['transport'] = getattr(opt, 'transport', '')
            payload['listenport'] = getattr(opt, 'listenport', 0)
        return json_ok(data=payload)
    except Exception as e:
        logger.exception('admin_runtime exception:')
        return json_error(str(e))


# :8010 是本数字人服务的固定约定端口；状态文件没写 port 时用它兜底。
AVATAR_RUNTIME_DEFAULT_PORT = 8010

# 「服务归属」响应里必须**永远存在**的键 -> 状态文件缺键时的默认值（任务 §14）。
# 这张表只兜底键的存在性：状态文件的标量字段是整体透传的（见 _process_ownership），
# 所以 Supervisor 以后新增标量字段不必改这里，也不会再出现「落盘有、接口没有」。
# 键名逐个沿用车主侧已在读的写法（Avatar-Supervisor.ps1 的 Get-AvatarRuntimeEndpoint
# 与前端），禁止重命名。
_OWNERSHIP_DEFAULTS = {
    'state': 'UNOWNED',
    'schema': '',
    'avatar_id': '',
    'avatar_id_source': '',
    'generation': None,
    'owner_pid': None,
    'owner_mode': '',
    'supervisor_pid': None,
    'launcher_pid': None,
    'listener_pid': None,
    'started_at': '',
    'exit_code': None,
    'exit_reason': '',
    'last_error': '',
    'task_name': '',
    'desired_avatar_id': '',
    'request_pid': None,
    'port': AVATAR_RUNTIME_DEFAULT_PORT,
    'updated_at': '',
    'supervisor': None,
}


def _normalize_ownership(info: dict) -> dict:
    """把状态文件快照收口成固定形状的归属响应。

    调用方（前端 / 运维脚本 / Supervisor 回读）按「键一定在、类型一定对」来读，
    所以这里只补默认值与类型，不改任何既有键的名字或取值语义。
    """
    for key, default in _OWNERSHIP_DEFAULTS.items():
        if info.get(key) is None:
            info[key] = default
    # listener_pids 必须是列表：状态文件里可能是 None、空数组，或被 PowerShell
    # 退化成裸标量（单元素时常见），三种都要收成列表而不是让前端判空炸掉。
    pids = info.get('listener_pids')
    if isinstance(pids, (list, tuple)):
        info['listener_pids'] = [p for p in pids if p not in (None, '')]
    elif pids is None or pids == '':
        info['listener_pids'] = []
    else:
        info['listener_pids'] = [pids]
    # port 缺失/为 0/写成字符串都回落 8010。
    try:
        info['port'] = int(info.get('port') or AVATAR_RUNTIME_DEFAULT_PORT)
    except (TypeError, ValueError):
        info['port'] = AVATAR_RUNTIME_DEFAULT_PORT
    return info


def _process_ownership() -> dict:
    """Read the Supervisor's own record of who owns this process.

    Best-effort by design: :8010 must keep answering even when the state file is
    mid-rotation or the service was started by hand.  A missing file is reported
    as ``state='UNOWNED'`` — which is itself the answer to "is there exactly one
    formal owner?", the question PROCESS OWNERSHIP exists to make checkable.
    """
    global _OWNERSHIP_CACHE
    now = time.monotonic()
    if _OWNERSHIP_CACHE is not None and now - _OWNERSHIP_CACHE[0] < 2.0:
        return _OWNERSHIP_CACHE[1]
    info = {'state': 'UNOWNED', 'state_file': AVATAR_RUNTIME_STATE_FILE}
    try:
        if os.path.isfile(AVATAR_RUNTIME_STATE_FILE):
            with open(AVATAR_RUNTIME_STATE_FILE, 'r', encoding='utf-8') as fh:
                raw = json.load(fh)
            # 整体透传状态文件的标量字段：Supervisor 落盘什么归属信息，接口就回答什么，
            # 新增字段不会再漏（env / log_paths 这类嵌套结构不在归属契约内，不透传）。
            # 逐字段白名单曾经就是这样漏掉 owner_mode / listener_pids / port 的。
            info = {k: v for k, v in raw.items()
                    if not isinstance(v, (dict, list, tuple))}
            # supervisor 是嵌套结构里唯一属于既有契约的键（name/pid/task/actions）。
            info['supervisor'] = raw.get('supervisor')
            info['listener_pids'] = raw.get('listener_pids')
            info['state_file'] = AVATAR_RUNTIME_STATE_FILE
    except Exception as exc:  # pragma: no cover - a torn read must not break status
        info = {'state': 'STATE_UNREADABLE', 'error': str(exc),
                'state_file': AVATAR_RUNTIME_STATE_FILE}
    info = _normalize_ownership(info)
    _OWNERSHIP_CACHE = (now, info)
    return info


_OWNERSHIP_CACHE = None

_AVATAR_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# scripts/Avatar-Runtime-Supervisor.ps1 落盘的三元组记录（owner/launcher/listener）。
AVATAR_RUNTIME_STATE_FILE = os.environ.get(
    "AVATAR_RUNTIME_STATE_FILE",
    os.path.abspath(os.path.join(_AVATAR_ROOT, '..', '..', 'windows',
                                 'avatar_runtime_state.json')),
)


async def media_ready(request):
    """Admin（只读）: 「数字人真的在出画」的服务端那一半判据。

    浏览器只能看到 ``pc.connectionState``，而 aiortc 说 connected 只证明信令
    通了。真正的判据是本服务的媒体泵还在往编码器里推帧——``video_age_s``
    就是"上一帧距今多久"。前端把它和自己的 ``videoWidth>0`` /
    ``currentTime`` 增长合成一个 MEDIA_READY。
    """
    try:
        sessionid = request.query.get('sessionid', '')
        if not sessionid:
            return json_error("sessionid is required")
        window = float(request.query.get('window_s', '1.5'))
        return json_ok(data=session_lifecycle.media_ready(sessionid, window_s=window))
    except Exception as e:
        logger.exception('media_ready exception:')
        return json_error(str(e))


# ─── 路由注册 ──────────────────────────────────────────────────────────────

async def index(request):
    """默认首页重定向"""
    opt = request.app.get("opt")
    pagename = 'index.html'
    if opt and opt.transport == 'rtmp':
        pagename = 'rtmpapi.html'
    elif opt and opt.transport == 'rtcpush':
        pagename = 'rtcpushapi.html'
    raise web.HTTPFound(f'/{pagename}')


def setup_routes(app):
    """注册所有路由到 aiohttp app"""
    app.router.add_get("/api/lipsync/mode", lipsync_mode)
    app.router.add_post("/api/lipsync/mode", lipsync_mode)
    app.router.add_get("/", index)
    app.router.add_post("/human", human)
    app.router.add_post("/humanaudio", humanaudio)
    app.router.add_post("/set_audiotype", set_audiotype)
    app.router.add_post("/record", record)
    app.router.add_post("/interrupt_talk", interrupt_talk)
    app.router.add_post("/is_speaking", is_speaking)
    # Explicit session release — the browser-side half of the reclamation loop.
    app.router.add_post("/api/session/release", session_release)
    app.router.add_get("/api/session/release", session_release)
    app.router.add_get("/api/admin/runtime", admin_runtime)
    app.router.add_get("/api/media_ready", media_ready)
    app.router.add_get("/api/admin/config", admin_config)
    app.router.add_get("/api/admin/sessions", admin_sessions)
    app.router.add_get("/api/admin/segment_gate", admin_segment_gate)
    app.router.add_get("/api/admin/playback", admin_playback)
    app.router.add_get('/sse', sse_handler)

    # ── Local ASR endpoint (SenseVoice/FunASR) ── Issue #604 ──
    try:
        from server.asr_server import asr_websocket_handler, is_funasr_available
        if is_funasr_available():
            app.router.add_get("/api/asr", asr_websocket_handler)
            logger.info("[ASR] Local SenseVoice ASR endpoint enabled at /api/asr")
        else:
            logger.info("[ASR] funasr not installed — local ASR endpoint disabled "
                        "(pip install funasr modelscope)")
    except Exception as e:
        logger.warning(f"[ASR] Failed to register ASR endpoint: {e}")

    # 注册 avatar 生成相关的路由
    setup_avatar_routes(app)

    app.router.add_static('/', path='web')
