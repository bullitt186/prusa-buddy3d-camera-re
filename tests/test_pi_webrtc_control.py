import sys
import unittest
from pathlib import Path


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

from proto import decode_message, encode_message  # noqa: E402
from state import CameraState  # noqa: E402
from status import build_status_message  # noqa: E402
import webrtc_control  # noqa: E402


def nested(raw):
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    return decode_message(raw)


class WebRtcModeDecodeTests(unittest.TestCase):
    def test_decodes_field_one_not_the_tag_byte(self):
        # b'\x08\x01': data[0] is the tag byte 0x08, field 1 is the value 1.
        payload = encode_message({1: 1})
        self.assertEqual(payload[0], 0x08)
        self.assertEqual(webrtc_control.decode_mode(payload), 1)
        self.assertEqual(webrtc_control.decode_mode(encode_message({1: 0})), 0)

    def test_rejects_missing_non_int_and_out_of_range(self):
        for data in (
            b'',
            None,
            'not-bytes',
            encode_message({2: 1}),        # wrong field
            encode_message({1: 2}),        # out of range
            encode_message({1: 'x'}),      # wrong wire type
        ):
            with self.subTest(data=data):
                self.assertIsNone(webrtc_control.decode_mode(data))


class WebRtcModeTransitionTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def _start(self):
        self.calls.append('start')

    def _stop(self):
        self.calls.append('stop')

    def _apply(self, requested, state):
        return webrtc_control.apply_mode(
            requested, state, start_service=self._start, stop_service=self._stop
        )

    def test_disable_then_enable_matches_firmware_flow(self):
        state = CameraState()                 # default mode=1, status=1
        self.assertTrue(self._apply(0, state))
        self.assertEqual(state.webrtc_mode, 0)
        self.assertEqual(state.webrtc_status, 0)
        self.assertEqual(self.calls, ['stop'])

        # A second disable is a no-op: service already stopped.
        self.assertTrue(self._apply(0, state))
        self.assertEqual(self.calls, ['stop'])

        self.assertTrue(self._apply(1, state))
        self.assertEqual(state.webrtc_mode, 1)
        self.assertEqual(state.webrtc_status, 1)
        self.assertEqual(self.calls, ['stop', 'start'])

        # A second enable is a no-op: service already running.
        self.assertTrue(self._apply(1, state))
        self.assertEqual(self.calls, ['stop', 'start'])

    def test_enable_from_stopped_state_starts_service(self):
        state = CameraState()
        state.webrtc_mode = 0
        state.webrtc_status = 0
        self.assertTrue(self._apply(1, state))
        self.assertEqual(self.calls, ['start'])
        self.assertEqual(state.webrtc_status, 1)

    def test_invalid_requested_value_is_ignored(self):
        state = CameraState()
        for invalid in (2, -1, '1', None, True, 1.0):
            with self.subTest(invalid=invalid):
                self.assertFalse(self._apply(invalid, state))
        self.assertEqual(self.calls, [])
        self.assertEqual(state.webrtc_mode, 1)

    def test_failed_service_call_does_not_update_state(self):
        state = CameraState()
        state.webrtc_status = 0
        state.webrtc_mode = 0

        def boom():
            raise RuntimeError('start failed')

        self.assertFalse(webrtc_control.apply_mode(1, state, start_service=boom))
        self.assertEqual(state.webrtc_status, 0)
        self.assertEqual(state.webrtc_mode, 0)

    def test_mode_change_marks_info_dirty(self):
        state = CameraState()
        state.info_dirty = False
        self._apply(0, state)
        self.assertTrue(state.info_dirty)


class WebRtcGateTests(unittest.TestCase):
    def test_rejects_only_when_both_zero(self):
        state = CameraState()
        cases = [
            (0, 0, False),
            (0, 1, True),
            (1, 0, True),
            (1, 1, True),
        ]
        for mode, status, expected in cases:
            with self.subTest(mode=mode, status=status):
                state.webrtc_mode = mode
                state.webrtc_status = status
                self.assertEqual(webrtc_control.offer_allowed(state), expected)


class WebRtcStatusPayloadTests(unittest.TestCase):
    def _extended(self, state):
        msg = build_status_message(state, token='t', mac='AA:BB:CC:DD:EE:FF', ip='192.0.2.1')
        return nested(decode_message(msg)[5])

    def test_status_reports_actual_mode_and_status(self):
        state = CameraState()
        state.webrtc_mode = 0
        state.webrtc_status = 0
        self.assertEqual(nested(self._extended(state)[11]), {1: 0, 2: 0})

        state.webrtc_mode = 1
        state.webrtc_status = 1
        self.assertEqual(nested(self._extended(state)[11]), {1: 1, 2: 1})

        state.webrtc_mode = 1
        state.webrtc_status = 0
        self.assertEqual(nested(self._extended(state)[11]), {1: 1, 2: 0})


if __name__ == '__main__':
    unittest.main()
