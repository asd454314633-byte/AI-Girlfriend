###############################################################################
#  Copyright (C) 2024 LiveTalking@lipku https://github.com/lipku/LiveTalking
#  email: lipku@foxmail.com
# 
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  
#       http://www.apache.org/licenses/LICENSE-2.0
# 
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
###############################################################################

# server.py
from flask import Flask, render_template,send_from_directory,request, jsonify
#from flask_sockets import Sockets
import base64
import json
#import gevent
#from gevent import pywsgi
#from geventwebsocket.handler import WebSocketHandler
import re
import os
import sys
import numpy as np
from threading import Thread,Event
#import multiprocessing
import torch.multiprocessing as mp

from aiohttp import web
import aiohttp
import aiohttp_cors
from aiortc import RTCPeerConnection, RTCSessionDescription,RTCIceServer,RTCConfiguration
from aiortc.rtcrtpsender import RTCRtpSender
from server.webrtc import HumanPlayer
from avatars.base_avatar import BaseAvatar
from llm import llm_response
import registry
from server.routes import setup_routes
from server.rtc_manager import RTCManager
from server.session_manager import session_manager
from server.session_lifecycle import session_lifecycle

import argparse
import random
import shutil
import asyncio
import signal
import atexit
import torch
from io import BytesIO
from typing import Dict
from utils.logger import logger
import copy
import gc
from dotenv import load_dotenv


app = Flask(__name__)
#sockets = Sockets(app)
opt = None
model = None
global_avatars = {} # avatar_id: payload
        

#####webrtc###############################
# rtc_manager replaces the old pcs set and duplicate offer handlers.
rtc_manager = None

def randN(N)->int:
    '''生成长度为 N的随机数 '''
    min = pow(10, N - 1)
    max = pow(10, N)
    return random.randint(min, max - 1)

def build_avatar_session(sessionid:str, params:dict)->BaseAvatar:
    opt_this = copy.deepcopy(opt)
    opt_this.sessionid = sessionid

    avatar_id = params.get('avatar',opt.avatar_id) 
    opt_this.avatar_id = avatar_id
    ref_audio = params.get('refaudio','') #音色
    ref_text = params.get('reftext','')
    if (avatar_id and avatar_id != opt.avatar_id):
        # Avoid reloading if already cached globally
        if avatar_id not in global_avatars:
            global_avatars[avatar_id] = load_avatar(avatar_id)
        avatar_this = global_avatars[avatar_id]
    else:
        # Default avatar loaded at startup
        avatar_this = global_avatars.get(opt.avatar_id)
    if ref_audio: #请求参数配置了参考音频
        opt_this.REF_FILE = ref_audio
        opt_this.REF_TEXT = ref_text
    custom_config=params.get('custom_config','') #动作编排配置
    if custom_config:
        opt_this.customopt = json.loads(custom_config)

    avatar_session = registry.create("avatar", opt.model, opt=opt_this, model=model, avatar=avatar_this)
    return avatar_session

async def offer(request):
    return await rtc_manager.handle_offer(request)

async def whep(request):
    return await rtc_manager.handle_whep(request)

async def on_shutdown(app):
    await rtc_manager.shutdown()


# ---------------------------------------------------------------------------
# death witnesses -- so "the person vanished" is a recorded fact, not a guess
# ---------------------------------------------------------------------------
# Why two of them, and why they are cheap: an unelevated Windows box cannot answer
# "who ended this process". Security 4688/4689 are not audited here and WMI's
# Win32_ProcessStopTrace is access-denied, so there is no event to read. What the
# service CAN do is make each of the three ways it can disappear leave a signature:
#   * a console/session teardown (hidden console closed, sign-out, shutdown) is
#     delivered as a CTRL_* event that CPython surfaces NOWHERE -- it only maps
#     CTRL_C to SIGINT and CTRL_BREAK to SIGBREAK, and CTRL_CLOSE_EVENT just runs the
#     default handler and ExitProcess after five seconds. A registered handler turns
#     that silent class into a logged line.
#   * a self-initiated stop already logs 'received SIG…' / 'avatar runtime stopped'.
#   * TerminateProcess is untrappable, so the third case is identified by elimination:
#     the Supervisor compares this heartbeat's last stamp against the instant the
#     process vanished. No console line, no graceful line, fresh heartbeat, exit code 0
#     means somebody else ended it -- and says so in exit_reason instead of CHILD_EXIT.
_console_witness_ref = {}


def _heartbeat_path():
    """Where the liveness stamp lands: the repo's own ``logs/runtime``, found by walking up.

    The launch environment is the provider manifest's business and stays untouched; the
    app only needs a path that is true wherever the repo is checked out.

    'Walking up and taking the first ``logs/runtime`` that exists' turned out not to be
    that simple, and the failure was silent and total.  A diagnostic probe on 2026-09-25
    09:55 accidentally created ``<repo>/runtime/logs/runtime/`` (one level too deep), and
    from that second every generation resolved its heartbeat AND its exit witness into
    that stray directory -- because it is nearer to ``app.py`` than the repo root is.  The
    service was healthy and instrumented; the Supervisor was reading a file that had
    stopped moving hours earlier, and reported 'nothing in the witness'.  So the anchor is
    no longer 'nearest directory that looks like a log dir' but 'a directory that IS the
    repo root', proved by the thing only the repo root has.  A stray ``logs/runtime``
    anywhere below the root can no longer capture the channel.
    """
    override = os.environ.get('AVATAR_HEARTBEAT_FILE')
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if (os.path.isdir(os.path.join(here, 'logs', 'runtime'))
                and os.path.isdir(os.path.join(here, 'runtime', 'avatar', 'app'))):
            return os.path.join(here, 'logs', 'runtime', 'avatar-8010.heartbeat.log')
        here = os.path.dirname(here)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'avatar-8010.heartbeat.log')


def _install_console_witness():
    import ctypes

    names = {0: 'CTRL_C_EVENT', 1: 'CTRL_BREAK_EVENT', 2: 'CTRL_CLOSE_EVENT',
             5: 'CTRL_LOGOFF_EVENT', 6: 'CTRL_SHUTDOWN_EVENT'}
    try:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        @handler_type
        def _witness(ctrl_type):
            # Deliberately not taking any lock and not awaiting anything: this runs on a
            # thread Windows is already tearing down, with a five-second budget. Record
            # and return False so the default teardown still happens.
            try:
                sys.stderr.write('%s avatar witnessed %s pid=%d -- console/session '
                                 'teardown, no graceful drain ran\n'
                                 % (_stamp(), names.get(ctrl_type, 'CTRL_%d' % ctrl_type),
                                    os.getpid()))
                sys.stderr.flush()
            except Exception:  # noqa: BLE001 - a witness must never cause the death
                pass
            return False

        # ctypes frees the trampoline if the only reference is a local.
        _console_witness_ref['fn'] = _witness
        installed = bool(kernel32.SetConsoleCtrlHandler(_witness, True))
        logger.info('console-control witness installed=%s pid=%d', installed, os.getpid())
        return installed
    except Exception as exc:  # noqa: BLE001 - best effort by design
        logger.warning('cannot install console-control witness: %s', exc)
        return False


def _stamp():
    import time

    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


# ---------------------------------------------------------------------------
# exit witnesses -- the two silent deaths the console handler cannot see
# ---------------------------------------------------------------------------
# The dossier in Avatar-Supervisor.ps1 distinguishes deaths by which lines exist.
# Before this, only two of the four ways to end were distinguishable, and the two
# that mattered most were silent:
#   * a native fault inside aiortc / torch / onnxruntime -> WER reported nothing on
#     this box, and the stderr log just stops.  faulthandler dumps every thread's
#     Python stack for SIGSEGV/SIGABRT/SIGFPE/SIGBUS/SIGILL into the witness file.
#   * an exit raised from *inside* the process -- CPython finalizing normally -- vs. a
#     foreign TerminateProcess.  They look identical from outside (code 0, no logs), and
#     the Python atexit hook separates them:
#       python-exit-hook  -> the interpreter ran its exit hooks: it left by itself
#       neither            -> somebody else ended the process
#     A third tier was tried and REMOVED after measurement: a marker registered through
#     ctypes in a CRT's atexit list, meant to catch "a C extension called exit() without
#     Python ever finalizing".  logs/runtime/regression/crt_diff.txt is the differential:
#     the same child exiting cleanly returned 0 in the control arm and
#     0xC000001D STATUS_ILLEGAL_INSTRUCTION in both arms that registered the callback
#     (the callback never ran).  Registering a Python trampoline in a foreign CRT list
#     therefore *caused* the very exit-time crash this witness exists to observe, so it
#     is gone; C-level-exit-without-finalize stays an unclassified gap rather than a
#     mechanism that corrupts the shutdown path.
_exit_witness_refs = {}


def _exit_witness_path():
    return os.path.join(os.path.dirname(_heartbeat_path()),
                        'avatar-8010.exitwitness.log')


def _install_exit_witness():
    path = _exit_witness_path()
    refs = _exit_witness_refs
    refs['installed'] = False

    try:
        handle = open(path, 'a', encoding='utf-8', buffering=1)
        refs['handle'] = handle
    except Exception as exc:  # noqa: BLE001 - no witness must never stop the service
        logger.warning('exit witness cannot open %s: %s', path, exc)
        return False

    try:
        import faulthandler

        faulthandler.enable(file=handle, all_threads=True)
        refs['faulthandler'] = True
    except Exception as exc:  # noqa: BLE001
        refs['faulthandler'] = False
        logger.warning('faulthandler not enabled for exit witness: %s', exc)

    def _python_exit_hook():
        # Runs during Py_FinalizeEx, while Python is still fully alive: this is the
        # "we decided to leave" marker, and the session count at this instant is the
        # answer to "did it quit because the room emptied?".
        try:
            refs['handle'].write('%s python-exit-hook pid=%d live_sessions=%s\n'
                                 % (_stamp(), os.getpid(),
                                    _witness_session_count()))
            refs['handle'].flush()
        except Exception:  # noqa: BLE001
            pass

    atexit.register(_python_exit_hook)
    refs['python_hook'] = _python_exit_hook

    # ── the hard self-exit tripwire ──────────────────────────────────────────
    # The two hooks above cannot see the one way the process can end *itself* that
    # looks exactly like a foreign kill: ``os._exit()``.  On Windows CPython it is
    # ``TerminateProcess(GetCurrentProcess(), status)`` -- no atexit list, no
    # faulthandler dump, no CRT flush, exit status whatever the caller passed.  That made
    # a self-exit with status 0 look, from outside, like an outside kill -- which is why
    # every death on this service has been classified 'TERMINATED_UNDIAGNOSED'.  (An
    # earlier draft of this comment said a status-0 self-exit was indistinguishable from
    # ``Stop-Process -Force``.  That was wrong in a way worth keeping: measured in
    # runtime/tests/probes/launcher_kill_tree_fingerprint.py, ``Stop-Process -Force`` and
    # ``[Process]::Kill()`` leave 0xFFFFFFFF on the process they are aimed at, while a
    # terminate aimed at the *venv launcher* leaves 0x00000000 on the listener beneath it
    # -- the redirector runs the real interpreter in a kill-on-close job object, and the
    # job's close status is what the listener records, whatever status the caller passed.
    # So '0 at the listener' carries no information about the killer, and the pair
    # listener=0x00000000 / launcher=0x00000000 is the shape that survives: a status-0
    # terminate on either object, or a launcher that exited 0 by itself and closed its job.)
    # Wrapping the two ways Python can end the process from inside means that if this
    # process ever does end itself, the witness names the frame that pulled the trigger --
    # and if the witness stays silent, self-exit is *positively excluded* rather than
    # merely unproven.
    def _all_thread_stacks(limit=520):
        import threading
        import traceback
        parts = []
        try:
            names = {t.ident: t.name for t in threading.enumerate()}
            for tid, frame in sorted(sys._current_frames().items()):
                text = ''.join(traceback.format_stack(frame)).strip().replace('\n', ' | ')
                parts.append('[%s:%d] %s' % (names.get(tid, '?'), tid, text[-limit:]))
        except Exception:  # noqa: BLE001
            parts.append('<unreadable>')
        return ' ;; '.join(parts)[:2400]

    def _hard_exit_note(kind, detail):
        try:
            refs['handle'].write('%s %s pid=%d %s live_sessions=%s\n'
                                 '  caller_stack: %s\n'
                                 '  all_threads: %s\n'
                                 % (_stamp(), kind, os.getpid(), detail,
                                    _witness_session_count(),
                                    ''.join(__import__('traceback').format_stack())
                                    .strip().replace('\n', ' | '),
                                    _all_thread_stacks()))
            refs['handle'].flush()
        except Exception:  # noqa: BLE001 - a witness must never cause the death
            pass

    _orig_os_exit = os._exit

    def _traced_os_exit(status):
        _hard_exit_note('SELF_EXIT', 'status=%s' % (status,))
        return _orig_os_exit(status)

    os._exit = _traced_os_exit
    refs['os_exit_wrapped'] = True

    _orig_os_kill = os.kill

    def _traced_os_kill(pid, sig):
        # Anything in this process that reaches for a signal is worth a line in the
        # witness -- including the sig=0 existence probes, because on some CPython
        # builds sig=0 is not a no-op and that is exactly the assumption the :7860
        # panel makes about every pid in the state file.
        _hard_exit_note('SELF_KILL', 'target=%s sig=%s' % (pid, sig))
        return _orig_os_kill(pid, sig)

    os.kill = _traced_os_kill
    refs['os_kill_wrapped'] = True

    try:
        refs['handle'].write('%s exit-witness-hardexit-hook pid=%d os_exit=1 os_kill=1\n'
                             % (_stamp(), os.getpid()))
        refs['handle'].flush()
    except Exception:  # noqa: BLE001
        pass

    # No CRT-level marker: see the header.  The slot stays in the install line so the
    # dossier's existing readers keep a stable shape.
    refs['crt'] = 'disabled'

    try:
        refs['handle'].write('%s witness-installed pid=%d faulthandler=%s crt=%s '
                             'python_hook=1\n' % (_stamp(), os.getpid(),
                                                  bool(refs.get('faulthandler')),
                                                  refs.get('crt', 'none')))
        refs['handle'].flush()
    except Exception:  # noqa: BLE001
        pass
    refs['installed'] = True
    logger.info('exit witnesses installed pid=%d path=%s', os.getpid(), path)
    return True


def _witness_session_count():
    try:
        return session_lifecycle.active_count()
    except Exception:  # noqa: BLE001 - the hook runs at teardown, when anything can fail
        return '?'


async def _liveness_beat():
    """Overwrite-a-line-per-15s proof of when the service was last alive.

    Separate from the stderr log on purpose: that one is a launch-truncated traceback
    nobody wants padded with 14k idle lines a day, and this one is a machine read whose
    only job is to be recent. Appended in place (no rename, no delete) so it cannot hit
    the delete-pending window that a rotating temp file would.
    """
    path = _heartbeat_path()
    interval = float(os.environ.get('AVATAR_HEARTBEAT_SEC', '15'))
    beats = 0
    while True:
        await asyncio.sleep(interval)
        beats += 1
        try:
            active = session_lifecycle.active_count()
        except Exception:  # noqa: BLE001 - the beat must survive a bad query
            active = -1
        line = '%s avatar heartbeat pid=%d beats=%d active_sessions=%d max_session=%s\n' % (
            _stamp(), os.getpid(), beats, active, getattr(session_lifecycle, 'max_session', '?'))
        try:
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write(line)
                handle.flush()
                if handle.tell() > 256 * 1024:
                    keep = []
                    try:
                        with open(path, 'r', encoding='utf-8', errors='replace') as reader:
                            keep = reader.readlines()[-40:]
                    except Exception:  # noqa: BLE001
                        pass
                    if keep:
                        with open(path, 'w', encoding='utf-8') as writer:
                            writer.writelines(keep)
                            writer.flush()
        except Exception:  # noqa: BLE001 - never let a witness kill what it watches
            logger.debug('heartbeat write failed path=%s', path)


def _install_lifecycle(appasync, runner, loop):
    """Give :8010 a graceful exit and a reaper.

    Two absences, one cause.  There was no signal handling anywhere in the
    service — ``on_shutdown`` is only awaited by ``runner.cleanup()``, and
    ``run_server`` blocked on ``run_forever()`` with no try/finally, so even
    Ctrl+C skipped ``rtc_manager.shutdown()``.  And there was nothing sweeping
    sessions that aiortc never reports on.  Both are fixed here, and both are
    the same contract: the process must leave no live session behind, whatever
    made it stop.
    """
    stopping = {'done': False}

    async def _drain_and_stop(signame):
        if stopping['done']:
            return
        stopping['done'] = True
        logger.info('received %s — draining all avatar sessions', signame)
        try:
            await rtc_manager.shutdown(reason=f'signal_{signame}')
        except Exception:  # noqa: BLE001
            logger.exception('session drain on %s failed; closing listener anyway', signame)
        finally:
            loop.stop()

    def _on_signal(signame):
        # SIGINT/SIGTERM arrive on the OS thread; marshal them onto the loop so
        # the drain can await pc.close() instead of racing the interpreter dying.
        loop.call_soon_threadsafe(
            lambda: loop.ensure_future(_drain_and_stop(signame)))

    for signame in ('SIGINT', 'SIGTERM', 'SIGBREAK'):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _on_signal, signame)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
            try:
                signal.signal(sig, lambda *_a: loop.call_soon_threadsafe(
                    lambda: loop.ensure_future(_drain_and_stop(signame))))
            except (ValueError, OSError, RuntimeError):
                logger.debug('cannot install %s handler', signame)

    @atexit.register
    def _atexit_release():
        """Last resort: an abrupt interpreter exit must not *report* live sessions.

        Threads die with the process, so the GPU work stops regardless; this
        clears the bookkeeping so a supervisor reading state sees STOPPED, not a
        table of sessions nobody can release any more.
        """
        try:
            session_lifecycle.release_all_sync('atexit')
        except Exception:  # pragma: no cover
            pass

    loop.create_task(_startup_watchdog())
    if sys.platform == 'win32':
        # Only meaningful with a console to be torn down; on a headless launch the call
        # just fails and logs once.
        _install_console_witness()
        _install_exit_witness()
    loop.create_task(_liveness_beat())


async def _startup_watchdog():
    """Start the session reaper and announce the identity the Supervisor needs."""
    session_lifecycle.start_watchdog()
    logger.info(
        'avatar session lifecycle ready pid=%d listener=%s max_session=%s',
        os.getpid(), _listener_pids(), session_lifecycle.max_session,
    )


def _listener_pids():
    """PIDs actually holding our listening socket, as seen from inside.

    The Supervisor needs to tell three PIDs apart — the owner that holds our
    handle, the venv launcher Python, and the process that owns the socket.
    Reporting our own PID and port from here gives it a second, independent
    read to cross-check its CIM walk against, rather than one number to trust.
    """
    try:
        import psutil
        mine = psutil.Process(os.getpid())
        # connections() is deprecated in this psutil and prints a DeprecationWarning
        # into the service log on every status read; net_connections() is its
        # replacement.  Keep the old name as a fallback for an older venv.
        conns = (mine.net_connections() if hasattr(mine, 'net_connections')
                 else mine.connections())
        return sorted({c.pid for c in conns
                       if c.status == psutil.CONN_LISTEN})
    except Exception:
        return [os.getpid()]

async def download_record(request):
    sessionid = request.match_info.get('sessionid')
    if not sessionid:
        return web.Response(status=400, text="sessionid is required")
    
    record_file = os.path.join('data', 'record', f"{sessionid}.mp4")
    
    if os.path.exists(record_file):
        return web.FileResponse(record_file)
    else:
        return web.Response(status=404, text="Record not found")


def main():
    global rtc_manager, opt, model,load_avatar
    # 解析命令行参数
    from config import parse_args
    opt = parse_args()

    # ─── 加载 avatar 插件（触发 @register 注册）──────────────────────
    _avatar_modules = {
        'musetalk':   'avatars.musetalk_avatar',
        'wav2lip':    'avatars.wav2lip_avatar',
        'ultralight': 'avatars.ultralight_avatar',
    }
    import importlib
    avatar_mod = importlib.import_module(_avatar_modules[opt.model])
    load_model = avatar_mod.load_model
    load_avatar = avatar_mod.load_avatar
    warm_up = avatar_mod.warm_up
    logger.info(opt)

    if opt.model == 'musetalk':
        model = load_model()
        global_avatars[opt.avatar_id] = load_avatar(opt.avatar_id) 
        warm_up(opt.batch_size,model)      
    elif opt.model == 'wav2lip':
        model = load_model("./models/wav2lip.pth")
        global_avatars[opt.avatar_id] = load_avatar(opt.avatar_id)
        warm_up(opt.batch_size,model,256)
    elif opt.model == 'ultralight':
        model = load_model(opt)
        global_avatars[opt.avatar_id] = load_avatar(opt.avatar_id)
        warm_up(opt.batch_size,global_avatars[opt.avatar_id],160)

    # init rtc manager
    session_manager.set_max_session(opt.max_session)
    session_manager.init_builder(build_avatar_session)
    rtc_manager = RTCManager(opt)
    # share avatar_sessions (RTCManager handles it but routes.py expects it)

    # 虚拟摄像头或 RTMP 模式：启动后台渲染线程
    if opt.transport == 'virtualcam' or opt.transport == 'rtmp':
        thread_quit = Event()
        params = {}
        # session 0 for virtualcam
        session_manager.add_session('0', build_avatar_session('0', params))
        rendthrd = Thread(target=session_manager.get_session('0').render, args=(thread_quit,))
        rendthrd.start()
        if opt.transport == 'virtualcam':
            logger.info("[VirtualCam] Virtual camera output enabled - digital human will be rendered to virtual camera")

    #############################################################################
    appasync = web.Application(client_max_size=1024**2*100)
    appasync["llm_response"] = llm_response
    appasync["opt"] = opt
    appasync["rtc_manager"] = rtc_manager

    appasync.on_shutdown.append(on_shutdown)
    appasync.router.add_post("/offer", offer)
    appasync.router.add_get("/record/{sessionid}", download_record)

    # 注册 server/routes.py 中的通用 API 路由
    setup_routes(appasync)

    # Configure default CORS settings.
    cors = aiohttp_cors.setup(appasync, defaults={
            "*": aiohttp_cors.ResourceOptions(
                allow_credentials=True,
                expose_headers="*",
                allow_headers="*",
            )
        })
    # Configure CORS on all routes.
    for route in list(appasync.router.routes()):
        cors.add(route)

    # /whep 注册在 CORS 之后：自行管理 OPTIONS，避免与 aiohttp_cors 冲突
    whep_resource = appasync.router.add_resource('/whep')
    whep_resource.add_route('POST', whep)
    whep_resource.add_route('OPTIONS', lambda _: web.Response(status=200))

    logger.info('start http server; http://<serverip>:'+str(opt.listenport))
    # logger.info('如果使用webrtc，推荐访问webrtc集成前端: http://<serverip>:'+str(opt.listenport)+'/dashboard.html')
    def run_server(runner):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, '0.0.0.0', opt.listenport)
        loop.run_until_complete(site.start())
        if opt.transport=='rtcpush':
            for k in range(opt.max_session):
                push_url = opt.push_url
                if k!=0:
                    push_url = opt.push_url+str(k)
                loop.run_until_complete(rtc_manager.handle_rtcpush(push_url, str(k)))
        _install_lifecycle(appasync, runner, loop)
        try:
            loop.run_forever()
        finally:
            # Whatever ended run_forever() — a signal, a crash inside the loop
            # that asyncio swallows, or loop.stop() from a shutdown path — the
            # sessions and the listener are released here, not by luck.
            loop.run_until_complete(_final_cleanup(runner))
            loop.close()

    async def _final_cleanup(runner):
        session_lifecycle.stop_watchdog()
        try:
            await rtc_manager.shutdown(reason='final_cleanup')
        except Exception:  # noqa: BLE001
            logger.exception('final session drain failed')
        try:
            await runner.cleanup()
        except Exception:  # noqa: BLE001
            logger.exception('runner.cleanup failed')

    #Thread(target=run_server, args=(web.AppRunner(appasync),)).start()
    run_server(web.AppRunner(appasync))
    logger.info('avatar runtime stopped: event loop closed')

    #app.on_shutdown.append(on_shutdown)
    #app.router.add_post("/offer", offer)

    # print('start websocket server')
    # server = pywsgi.WSGIServer(('0.0.0.0', 8000), app, handler_class=WebSocketHandler)
    # server.serve_forever()


# os.environ['MKL_SERVICE_FORCE_INTEL'] = '1'
# os.environ['MULTIPROCESSING_METHOD'] = 'forkserver'                                                    
if __name__ == '__main__':
    mp.set_start_method('spawn')
    load_dotenv()  # Load environment variables from .env file, if it exists
    main()
    
    
    
