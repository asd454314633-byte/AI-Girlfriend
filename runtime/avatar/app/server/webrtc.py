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

import asyncio
import json
import logging
import threading
import time
from typing import Tuple, Dict, Optional, Set, Union
import queue
from av.frame import Frame
from av.packet import Packet
from av import AudioFrame
import fractions
import numpy as np

AUDIO_PTIME = 0.020  # 20ms audio packetization
VIDEO_CLOCK_RATE = 90000
VIDEO_PTIME = 0.040 #1 / 25  # 30fps
VIDEO_TIME_BASE = fractions.Fraction(1, VIDEO_CLOCK_RATE)
SAMPLE_RATE = 16000
AUDIO_TIME_BASE = fractions.Fraction(1, SAMPLE_RATE)

#from aiortc.contrib.media import MediaPlayer, MediaRelay
#from aiortc.rtcrtpsender import RTCRtpSender
from aiortc import (
    MediaStreamTrack,
)

logging.basicConfig()
logger = logging.getLogger(__name__)
from utils.logger import logger as mylogger

# The terminate gate is the diagnostic instrument for this process' 0x00000000 deaths:
# it denies PROCESS_TERMINATE on our own process object (and on the venv launcher above
# us) for the duration of one teardown, so "teardown finished and we are still alive"
# becomes evidence rather than a shrug.  See server/terminate_gate.py for exactly what
# it can and cannot prove, and runtime/tests/probes/terminate_gate_lab.py for the
# three-repeat control that showed it arms, stays narrow, and releases.
#
# Imported defensively and never required: it is Windows-only ctypes, and an instrument
# that breaks the import of the service it observes is worse than no instrument.  A
# missing gate degrades to the pre-gate behaviour, it does not stop a teardown.
try:
    from server import terminate_gate as _terminate_gate
except Exception:  # noqa: BLE001
    _terminate_gate = None


class PlayerStreamTrack(MediaStreamTrack):
    """
    A video track that returns an animated flag.
    """

    def __init__(self, player, kind):
        super().__init__()  # don't forget this!
        self.kind = kind
        self._player = player
        self._queue = queue.Queue(maxsize=100)
        self.timelist = [] #记录最近包的时间戳
        self.current_frame_count = 0
        if self.kind == 'video':
            self.framecount = 0
            self.lasttime = time.perf_counter()
            self.totaltime = 0
    
    _start: float
    _timestamp: int

    async def next_timestamp(self) -> Tuple[int, fractions.Fraction]:
        if self.readyState != "live":
            raise Exception

        if self.kind == 'video':
            if hasattr(self, "_timestamp"):
                #self._timestamp = (time.time()-self._start) * VIDEO_CLOCK_RATE
                self._timestamp += int(VIDEO_PTIME * VIDEO_CLOCK_RATE)
                self.current_frame_count += 1
                wait = self._start + self.current_frame_count * VIDEO_PTIME - time.monotonic()  # ARMONO
                # wait = self.timelist[0] + len(self.timelist)*VIDEO_PTIME - time.time()               
                if wait>0:
                    if wait > 0.1:   # ARMONO-CLAMP: a media pump must never idle for more than a few frames
                        self._clampn = getattr(self, '_clampn', 0) + 1
                        if wait > 0.25 and self._clampn % 20 == 1:
                            mylogger.info('PACINGCLAMP %s wait=%.3f cnt=%d q=%d n=%d',
                                        self.kind, wait, self.current_frame_count,
                                        self._queue.qsize(), self._clampn)
                        wait = 0.1
                    await asyncio.sleep(wait)
                # if len(self.timelist)>=100:
                #     self.timelist.pop(0)
                # self.timelist.append(time.time())
            else:
                self._start = time.monotonic()   # ARMONO
                self._timestamp = 0
                self.timelist.append(self._start)
                mylogger.info('video start:%f',self._start)
            return self._timestamp, VIDEO_TIME_BASE
        else: #audio
            if hasattr(self, "_timestamp"):
                #self._timestamp = (time.time()-self._start) * SAMPLE_RATE
                self._timestamp += int(AUDIO_PTIME * SAMPLE_RATE)
                self.current_frame_count += 1
                wait = self._start + self.current_frame_count * AUDIO_PTIME - time.monotonic()  # ARMONO
                # wait = self.timelist[0] + len(self.timelist)*AUDIO_PTIME - time.time()
                if wait>0:
                    if wait > 0.1:   # ARMONO-CLAMP: a media pump must never idle for more than a few frames
                        self._clampn = getattr(self, '_clampn', 0) + 1
                        if wait > 0.25 and self._clampn % 20 == 1:
                            mylogger.info('PACINGCLAMP %s wait=%.3f cnt=%d q=%d n=%d',
                                        self.kind, wait, self.current_frame_count,
                                        self._queue.qsize(), self._clampn)
                        wait = 0.1
                    await asyncio.sleep(wait)
                # if len(self.timelist)>=200:
                #     self.timelist.pop(0)
                #     self.timelist.pop(0)
                # self.timelist.append(time.time())
            else:
                self._start = time.monotonic()   # ARMONO
                self._timestamp = 0
                self.timelist.append(self._start)
                mylogger.info('audio start:%f',self._start)
            return self._timestamp, AUDIO_TIME_BASE

    async def recv(self) -> Union[Frame, Packet]:
        # frame = self.frames[self.counter % 30]            
        self._player._start(self)
        # if self.kind == 'video':
        #     frame = await self._queue.get()
        # else: #audio
        #     if hasattr(self, "_timestamp"):
        #         wait = self._start + self._timestamp / SAMPLE_RATE + AUDIO_PTIME - time.time()
        #         if wait>0:
        #             await asyncio.sleep(wait)
        #         if self._queue.qsize()<1:
        #             #frame = AudioFrame(format='s16', layout='mono', samples=320)
        #             audio = np.zeros((1, 320), dtype=np.int16)
        #             frame = AudioFrame.from_ndarray(audio, layout='mono', format='s16')
        #             frame.sample_rate=16000
        #         else:
        #             frame = await self._queue.get()
        #     else:
        while True:
            try:
                frame, eventpoint = self._queue.get_nowait()
                if eventpoint and eventpoint.get('response_id'):
                    from server.idempotency import default_gate as segment_gate
                    if segment_gate.is_cancelled(eventpoint.get('sessionid'), eventpoint['response_id']):
                        if self.kind == 'audio' and isinstance(frame, AudioFrame):
                            # Cancel speech without compressing the audio clock:
                            # its paired video frame is already in the video queue.
                            # Skipping only audio permanently offsets subsequent lips.
                            # Only cancelled frames are muted; normal reading is intact.
                            for plane in frame.planes:
                                plane.update(bytes(plane.buffer_size))
                            eventpoint = None
                        else:
                            continue
                break
            except queue.Empty:
                await asyncio.sleep(0.005)
                
        pts, time_base = await self.next_timestamp()
        frame.pts = pts
        frame.time_base = time_base
        if eventpoint and self._player is not None:
            if eventpoint.get('status'):
                from server.playback import record
                record('output_' + eventpoint['status'], eventpoint, pts=pts,
                       time_base=str(time_base), queue_frames=self._queue.qsize())
            self._player.notify(eventpoint)
        if frame is None:
            self.stop()
            raise Exception
        if self.kind == 'video':
            self.totaltime += (time.perf_counter() - self.lasttime)
            self.framecount += 1
            self.lasttime = time.perf_counter()
            if self.framecount==100:
                mylogger.info(f"------actual avg final fps:{self.framecount/self.totaltime:.4f}")
                self.framecount = 0
                self.totaltime=0
        return frame
    
    def stop(self):
        super().stop()
        # Drain & delete remaining frames
        while not self._queue.empty():
            item = self._queue.get_nowait()
            del item
        if self._player is not None:
            self._player._stop(self)
            self._player = None

def player_worker_thread(
    quit_event,
    container
):
    container.render(quit_event)

class HumanPlayer:

    def __init__(
        self, avatar_session, format=None, options=None, timeout=None, loop=False, decode=True
    ):
        self.__thread: Optional[threading.Thread] = None
        self.__thread_quit: Optional[threading.Event] = None

        # examine streams
        self.__started: Set[PlayerStreamTrack] = set()
        self.__audio: Optional[PlayerStreamTrack] = None
        self.__video: Optional[PlayerStreamTrack] = None

        self.__audio = PlayerStreamTrack(self, kind="audio")
        self.__video = PlayerStreamTrack(self, kind="video")

        self.__container = avatar_session
        if hasattr(self.__container, 'output'):
            self.__container.output._player = self

        # ── MEDIA_READY evidence, and the idempotent teardown latch ─────────
        # Section 11 of the lifecycle contract: ``pc.connectionState ==
        # 'connected'`` proves signalling, not picture.  What proves the picture
        # is that this pump is still handing frames to the encoders, so the
        # counters live here, on the only object that can see them.  They are
        # plain attribute increments — no lock — because a media pump running at
        # 25 fps audio-video plus 50 fps audio must not pay for a mutex, and a
        # torn read of a monitoring counter is harmless.
        self.session_id = str(getattr(
            getattr(avatar_session, 'opt', None), 'sessionid', '') or '')
        self.video_frames = 0
        self.audio_frames = 0
        self.last_video_frame_mono: Optional[float] = None
        self._stopping = False
        self._stop_lock = threading.Lock()
        self._stopped = False

    def push_video(self, frame):
        from av import VideoFrame
        new_frame = VideoFrame.from_ndarray(frame, format="bgr24")
        self.__video._queue.put((new_frame, None))
        self.video_frames += 1
        self.last_video_frame_mono = time.monotonic()

    def push_audio(self, frame, eventpoint=None):
        from av import AudioFrame
        new_frame = AudioFrame(format='s16', layout='mono', samples=frame.shape[0])
        new_frame.planes[0].update(frame.tobytes())
        new_frame.sample_rate = 16000
        if eventpoint and eventpoint.get('status'):
            from server.playback import record
            record('render_' + eventpoint['status'], eventpoint,
                   queue_frames=self.__audio._queue.qsize())
        self.__audio._queue.put((new_frame, eventpoint))
        self.audio_frames += 1

    def media_stats(self) -> dict:
        """Frame泵的读数；``video_age_s`` 为 None 表示从未推过帧。"""
        age = (None if self.last_video_frame_mono is None
               else round(time.monotonic() - self.last_video_frame_mono, 3))
        return {
            'video_frames': self.video_frames,
            'audio_frames': self.audio_frames,
            'video_age_s': age,
            'video_queue': self.__video._queue.qsize(),
            'audio_queue': self.__audio._queue.qsize(),
            'thread_alive': bool(self.__thread is not None and self.__thread.is_alive()),
            'stopped': self._stopped,
        }

    def force_stop(self, reason: str = 'unspecified', join_timeout: float = 10.0) -> dict:
        """Run the teardown inside a bounded 'do not terminate us' window.

        A thin wrapper rather than code inside the body, so the ~110 lines of stop logic
        stay exactly where they were reviewed; ``_force_stop_impl`` is that body unchanged.

        WHY HERE
        --------
        Every measured 0x00000000 death of this process happened while a session was being
        released -- i.e. inside the wrapped call -- and left no handler, no atexit and no
        WER entry behind.  Access checks on a process object happen when a handle is
        OPENED, so holding a ``DENY PROCESS_TERMINATE`` ACE across this window turns any
        fresh handle-based kill into ``ERROR_ACCESS_DENIED`` at the caller, and turns our
        survival into a reading: 'the gate was armed and we still vanished' points at a
        native self-exit or a pre-existing handle, which is a different investigation.

        WHY THIS IS NOT LIFECYCLE POLICY
        ---------------------------------
        The window closes in a ``finally`` and the restore is verified by re-opening for
        terminate, so nothing here can leave the service unkillable (that would be a
        self-inflicted outage, and it is the first thing the lab tests).  A deliberate
        ``Stop`` is unaffected in the end: :8010's own teardown disarms before it exits,
        and the Supervisor's reap loop retries every 500 ms for 25 s, so a kill that
        lands inside our window is logged as denied and succeeds a moment later.
        ``AVATAR_TERMINATE_GATE=0`` removes the wrapper's effect without a code change.

        The no-op fast path skips the gate on purpose: an already-stopping player does no
        teardown, and re-writing a process DACL for a call that returns immediately would
        put noise in the one log line that has to be a clean reading.
        """
        if self._stopped or self._stopping or _terminate_gate is None:
            return self._force_stop_impl(reason=reason, join_timeout=join_timeout)
        with _terminate_gate.terminate_gate('hplayer_force_stop:%s' % (reason or '?'),
                                            log=mylogger):
            return self._force_stop_impl(reason=reason, join_timeout=join_timeout)

    def _force_stop_impl(self, reason: str = 'unspecified', join_timeout: float = 10.0) -> dict:
        """The teardown body -- called only through ``force_stop``, which holds the gate.

        Tear this player down from any thread, exactly once.  Idempotent.

        Why a second stop path is needed
        --------------------------------
        The only stop that upstream LiveTalking has is
        ``PlayerStreamTrack.stop() -> HumanPlayer._stop()``, and that is reached
        *only* when aiortc's ``RTCRtpSender._run_rtp`` exits — which requires the
        sender to have actually started (``sender.__started``).  A session that
        was answered but never negotiated ICE, or one torn down before the first
        RTP packet, therefore leaves ``media-player`` (and with it inference,
        process_frames and process_tts, all non-daemon threads cascading off the
        same quit event) running forever, holding ``avatar_session`` alive by
        reference.  That is the "created but never stopped" half of the leak.

        Semantics
        ---------
        * Repeated calls return immediately; only the first performs the join.
        * Safe to call from a worker thread (``session_lifecycle`` puts it in an
          executor) *and* from the event loop thread's callback context, because
          the caller is expected to be off-loop — see DANGER note 4 in the audit:
          joining on the loop would wedge all of :8010.
        * The join is bounded.  A wedged render thread must not convert a session
          release into an unbounded hang; the leftover thread is reported instead.
        """
        with self._stop_lock:
            if self._stopping:
                return {'already_in_progress': True, 'reason': reason,
                        'thread_alive': bool(self.__thread and self.__thread.is_alive())}
            self._stopping = True

        # ── boundary markers, one per statement, and a stack snapshot ─────────
        # Every measured death of this process happens inside this method and
        # leaves nothing behind: exit status 0x00000000, no Python atexit hook, no
        # faulthandler dump, no WER entry, no console CTRL_* line, and no new
        # process at the death instant.  A method that can be ended mid-flight by
        # something that does not run any of our handlers can only be localised by
        # writing a *statement-level* trail through an fd the kernel flushes
        # immediately.  faulthandler.dump_traceback writes straight to the
        # descriptor (no Python buffering), so the thread inventory below survives
        # whatever is about to happen -- it is the last thing the interpreter can
        # say about itself.  These lines are INFO on purpose: DEBUG reaches only
        # stderr, and the dossier reads both.
        _t_mark = time.perf_counter()

        def _mark(stage, extra=''):
            mylogger.info('HPLAYER_FS %s session=%s reason=%s dt_ms=%.1f %s',
                          stage, self.session_id or '?', reason,
                          (time.perf_counter() - _t_mark) * 1000.0, extra)

        # The gate exists because the dump itself has to be put on trial: the one
        # reproduced death stopped inside the window this block writes, so "the
        # instrument is adjacent to the fault" and "the instrument is the fault"
        # are both live hypotheses until an A/B run separates them.  Off must say
        # so out loud -- silence would let a disabled arm look like a clean result.
        import os as _os
        _inv = str(_os.environ.get('AVATAR_HPLAYER_THREAD_INVENTORY', '1')).strip().lower()
        if _inv in ('0', 'off', 'no', 'false'):
            mylogger.info('HPLAYER_THREADS session=%s reason=%s -- inventory DISABLED '
                          'by AVATAR_HPLAYER_THREAD_INVENTORY=%s',
                          self.session_id or '?', reason, _inv)
        else:
            try:
                import faulthandler
                import sys as _sys
                mylogger.info('HPLAYER_THREADS session=%s reason=%s -- stack inventory',
                              self.session_id or '?', reason)
                faulthandler.dump_traceback(file=_sys.stderr, all_threads=True)
            except Exception:  # noqa: BLE001 - a witness must never cause the death
                logger.debug('HPLAYER_FS thread dump failed', exc_info=True)

        quit_event = self.__thread_quit
        thread = self.__thread
        joined = False
        _mark('enter', 'join_timeout=%.1f thread_alive=%s' % (
            join_timeout, bool(thread and thread.is_alive())))
        if quit_event is not None:
            quit_event.set()
        _mark('quit_set', 'event=%s' % (quit_event is not None))
        if thread is not None:
            try:
                thread.join(timeout=join_timeout)
                joined = not thread.is_alive()
            except Exception:  # noqa: BLE001 - teardown must not raise into the release path
                logger.warning("HumanPlayer.force_stop join failed reason=%s: %s",
                               reason, exc_info=True)
        _mark('join_done', 'joined=%s thread_alive=%s' % (joined, bool(thread and thread.is_alive())))

        # Drop whatever the dead session queued.  Without this, a still-referenced
        # track queue keeps its backlog of frames alive, and a *restarted* pump
        # would replay the previous session's tail.
        drained = 0
        for track in (self.__audio, self.__video):
            if track is None:
                continue
            try:
                while not track._queue.empty():
                    track._queue.get_nowait()
                    drained += 1
            except Exception:  # pragma: no cover - a foreign queue shape is fine
                pass
            if track.readyState == 'live':
                try:
                    track.stop()
                except Exception:  # noqa: BLE001
                    logger.debug("HumanPlayer.force_stop track.stop failed", exc_info=True)
        _mark('drain_done', 'drained=%d' % drained)

        # Break the session <-> player reference cycle, and stop any later
        # /humanaudio from finding a pump through ``output._player``.
        if self.__container is not None:
            try:
                output = getattr(self.__container, 'output', None)
                if output is not None and getattr(output, '_player', None) is self:
                    output._player = None
            except Exception:  # pragma: no cover
                pass
            self.__container = None
        self.__started.clear()
        _mark('unlinked', 'container=%s' % (self.__container is not None))

        with self._stop_lock:
            self._stopped = True
        stats = self.media_stats()
        logger.info(
            "HumanPlayer stopped session=%s reason=%s joined=%s drained=%d "
            "vframes=%d aframes=%d thread_alive=%s",
            self.session_id or '?', reason, joined, drained,
            stats['video_frames'], stats['audio_frames'], stats['thread_alive'],
        )
        if not joined and thread is not None:
            logger.error("HumanPlayer render thread still alive after force_stop "
                         "session=%s reason=%s — reported, not retried",
                         self.session_id or '?', reason)
        _mark('exit', 'joined=%s drained=%d thread_alive=%s' % (
            joined, drained, stats['thread_alive']))
        return {'already_in_progress': False, 'reason': reason, 'joined': joined,
                'drained': drained, 'stats': stats}

    def get_buffer_size(self) -> int:
        return self.__video._queue.qsize()

    def notify(self,eventpoint):
        if self.__container is not None:
            self.__container.notify(eventpoint)

    @property
    def audio(self) -> MediaStreamTrack:
        """
        A :class:`aiortc.MediaStreamTrack` instance if the file contains audio.
        """
        return self.__audio

    @property
    def video(self) -> MediaStreamTrack:
        """
        A :class:`aiortc.MediaStreamTrack` instance if the file contains video.
        """
        return self.__video

    def _start(self, track: PlayerStreamTrack) -> None:
        self.__started.add(track)
        if self.__thread is None:
            self.__log_debug("Starting worker thread")
            self.__thread_quit = threading.Event()
            self.__thread = threading.Thread(
                name="media-player",
                target=player_worker_thread,
                args=(
                    self.__thread_quit,
                    self.__container
                ),
            )
            self.__thread.start()

    def _stop(self, track: PlayerStreamTrack) -> None:
        self.__started.discard(track)

        # ``force_stop`` is the idempotent path and may already have run (it
        # calls ``track.stop()`` itself, which lands here).  Re-entering would
        # join a thread that has already been reported on, so the latch is
        # checked before anything else.
        if self._stopping:
            return

        if not self.__started and self.__thread is not None:
            self.__log_debug("Stopping worker thread")
            self.__thread_quit.set()
            self.__thread.join()
            self.__thread = None

        if not self.__started and self.__container is not None:
            #self.__container.close()
            self.__container = None

    def __log_debug(self, msg: str, *args) -> None:
        mylogger.debug(f"HumanPlayer {msg}", *args)
