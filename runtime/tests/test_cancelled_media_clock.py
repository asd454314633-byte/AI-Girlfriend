"""Cancelled speech must retain the slot paired with the queued video frame."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import numpy as np
from av import AudioFrame

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'avatar/app'))
from server.webrtc import PlayerStreamTrack


def frame(value):
    f = AudioFrame(format='s16', layout='mono', samples=320)
    f.planes[0].update(np.full(320, value, np.int16).tobytes())
    f.sample_rate = 16000
    return f


class CancelClock(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_slot_is_muted_then_next_slot_keeps_pts(self):
        player = Mock()
        track = PlayerStreamTrack(player, 'audio')
        track._queue.put((frame(1000), {'sessionid': 'fixture', 'response_id': 'old'}))
        track._queue.put((frame(2000), {'sessionid': 'fixture', 'response_id': 'new'}))
        with patch('server.idempotency.default_gate.is_cancelled', side_effect=[True, False]):
            old = await track.recv()
            new = await track.recv()
        self.assertFalse(old.to_ndarray().any())
        self.assertTrue((new.to_ndarray() == 2000).all())
        self.assertEqual([old.pts, new.pts], [0, 320])
        self.assertEqual(track._queue.qsize(), 0)

    async def test_normal_audio_bytes_are_unchanged(self):
        track = PlayerStreamTrack(Mock(), 'audio')
        original = frame(1234)
        track._queue.put((original, None))
        self.assertIs(await track.recv(), original)
        self.assertTrue((original.to_ndarray() == 1234).all())


if __name__ == '__main__':
    unittest.main()
