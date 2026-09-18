import sys
import unittest
from pathlib import Path


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

from proto import decode_message  # noqa: E402
from state import CameraState  # noqa: E402
from status import build_status_message  # noqa: E402


def nested(raw):
    """Decode a nested message field.

    proto.decode_message returns a str for length-delimited bytes that happen to
    be valid UTF-8 (common for tiny control submessages), so re-encode before the
    second decode; UTF-8 round-trips exactly.
    """
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    return decode_message(raw)


def known_state():
    state = CameraState()
    state.set_quality(2)              # HD
    state.snapshot_interval = 60
    state.camera_name = 'Test Camera'
    state.webrtc_mode = 0
    state.webrtc_status = 1
    state.rtsp_mode = 2
    state.rtsp_running = True
    return state


class StatusMessageTests(unittest.TestCase):
    def _build(self, state, **kwargs):
        kwargs.setdefault('token', 'token-value')
        kwargs.setdefault('mac', 'AA:BB:CC:DD:EE:FF')
        kwargs.setdefault('ip', '192.0.2.1')
        kwargs.setdefault('ssid', 'TestSSID')
        kwargs.setdefault('signal_quality', 55)
        return build_status_message(state, **kwargs)

    def test_status_reports_shared_state(self):
        state = known_state()
        top = decode_message(self._build(state, request_id=None, sid='sid-initial'))

        self.assertEqual(nested(top[11]), {1: 2})          # video_quality enum
        self.assertEqual(nested(top[3])[4], 60)            # camera_status interval
        extended = nested(top[5])
        self.assertEqual(extended[3], 'Test Camera')       # camera name
        self.assertEqual(nested(extended[11]), {1: 0, 2: 1})  # webrtc mode/status
        rtsp = nested(extended[6])
        self.assertEqual(rtsp[1], 2)                       # rtsp mode
        self.assertEqual(rtsp[2], 1)                       # running -> status 1
        self.assertEqual(top[8], 'token-value')

    def test_status_omits_empty_secondary_network_submessage(self):
        state = known_state()
        top = decode_message(self._build(state, request_id=None, sid='sid-initial'))
        network = nested(top[4])
        self.assertNotIn(2, network)
        primary = nested(network[1])
        self.assertEqual(primary[1], 'TestSSID')
        self.assertEqual(primary[2], 'AA:BB:CC:DD:EE:FF')
        self.assertEqual(primary[3], '192.0.2.1')
        self.assertEqual(primary[5], 55)

    def test_rtsp_not_running_maps_to_status_two(self):
        state = known_state()
        state.rtsp_running = False
        rtsp = nested(nested(decode_message(self._build(state, sid='sid'))[5])[6])
        self.assertEqual(rtsp[2], 2)

    def test_field_ten_prefers_request_id_over_sid(self):
        state = known_state()
        requested = decode_message(self._build(state, request_id='req-12345678', sid='sid-abc'))
        self.assertEqual(requested[10], 'req-12345678')

    def test_field_ten_falls_back_to_sid_for_initial_status(self):
        state = known_state()
        initial = decode_message(self._build(state, request_id=None, sid='sid-abc'))
        self.assertEqual(initial[10], 'sid-abc')


if __name__ == '__main__':
    unittest.main()
