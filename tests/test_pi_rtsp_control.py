import os
import sys
import tempfile
import unittest
from pathlib import Path


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

from proto import decode_message, encode_message  # noqa: E402
from state import CameraState  # noqa: E402
from status import build_status_message  # noqa: E402
import rtsp_control  # noqa: E402


def nested(raw):
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    return decode_message(raw)


class RtspDecodeTests(unittest.TestCase):
    def test_decodes_field_one(self):
        self.assertEqual(rtsp_control.decode_mode(encode_message({1: 2})), 2)
        self.assertEqual(rtsp_control.decode_mode(encode_message({1: 1})), 1)

    def test_rejects_invalid_values(self):
        for data in (
            b'',
            b'\x08',                      # tag with no value
            None,
            encode_message({1: 0}),
            encode_message({1: 3}),
            encode_message({2: 2}),       # wrong field
            encode_message({1: 'on'}),    # wrong wire type
        ):
            with self.subTest(data=data):
                self.assertIsNone(rtsp_control.decode_mode(data))

    def test_config_values_map_to_modes(self):
        self.assertEqual(rtsp_control.mode_from_config('on'), 2)
        self.assertEqual(rtsp_control.mode_from_config('OFF'), 1)
        self.assertEqual(rtsp_control.mode_from_config(' off '), 1)
        self.assertEqual(rtsp_control.mode_from_config(2), 2)
        self.assertEqual(rtsp_control.mode_from_config(1), 1)
        for invalid in ('rtsp', 'enabled', '', None, 0, 3, True):
            with self.subTest(invalid=invalid):
                self.assertIsNone(rtsp_control.mode_from_config(invalid))


class RtspTransitionTests(unittest.TestCase):
    def _apply(self, mode, state, calls, query=None, persist=None):
        return rtsp_control.apply_mode(
            mode, state,
            start_service=lambda: calls.append('start'),
            stop_service=lambda: calls.append('stop'),
            query_service=query,
            persist=persist,
        )

    def test_direct_and_config_forms_are_identical(self):
        # Direct: set_rtsp_server_mode field 1 = 2 (enabled).
        direct_state = CameraState()
        direct_calls = []
        self.assertTrue(self._apply(
            rtsp_control.decode_mode(encode_message({1: 2})),
            direct_state, direct_calls, query=lambda: True,
        ))
        # Config: rtsp = "on".
        config_state = CameraState()
        config_calls = []
        self.assertTrue(self._apply(
            rtsp_control.mode_from_config('on'),
            config_state, config_calls, query=lambda: True,
        ))
        self.assertEqual(direct_state.rtsp_mode, config_state.rtsp_mode)
        self.assertEqual(direct_state.rtsp_running, config_state.rtsp_running)
        self.assertEqual(direct_calls, config_calls)
        self.assertEqual(direct_calls, ['start'])
        self.assertEqual(direct_state.rtsp_mode, 2)
        self.assertTrue(direct_state.rtsp_running)

    def test_disable_path(self):
        state = CameraState()
        state.rtsp_mode = 2
        state.rtsp_running = True
        calls = []
        self.assertTrue(self._apply(rtsp_control.mode_from_config('off'), state, calls,
                                    query=lambda: False))
        self.assertEqual(calls, ['stop'])
        self.assertEqual(state.rtsp_mode, 1)
        self.assertFalse(state.rtsp_running)

    def test_runtime_follows_queried_state(self):
        state = CameraState()
        # Commanded enable but the unit reports inactive.
        self._apply(2, state, [], query=lambda: False)
        self.assertFalse(state.rtsp_running)
        # Commanded disable but the unit reports active.
        self._apply(1, state, [], query=lambda: True)
        self.assertTrue(state.rtsp_running)

    def test_runtime_falls_back_to_commanded_state(self):
        state = CameraState()
        self._apply(2, state, [], query=lambda: None)
        self.assertTrue(state.rtsp_running)

        state = CameraState()
        self._apply(1, state, [], query=lambda: None)
        self.assertFalse(state.rtsp_running)

        state = CameraState()

        def boom():
            raise RuntimeError('systemctl unavailable')

        self._apply(2, state, [], query=boom)
        self.assertTrue(state.rtsp_running)

        state = CameraState()
        self._apply(2, state, [], query=None)
        self.assertTrue(state.rtsp_running)

    def test_invalid_mode_does_not_change_state_or_call_services(self):
        state = CameraState()
        state.rtsp_mode = 1
        calls = []
        for invalid in (0, 3, 'on', None, True, 2.0):
            with self.subTest(invalid=invalid):
                self.assertFalse(self._apply(invalid, state, calls))
        self.assertEqual(calls, [])
        self.assertEqual(state.rtsp_mode, 1)

    def test_persist_callback_receives_applied_mode(self):
        state = CameraState()
        persisted = []
        self._apply(2, state, [], persist=persisted.append)
        self.assertEqual(persisted, [2])

    def test_mode_change_marks_info_dirty(self):
        state = CameraState()
        state.info_dirty = False
        self._apply(2, state, [])
        self.assertTrue(state.info_dirty)


class RtspPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, 'rtsp.mode')

    def tearDown(self):
        self._tmp.cleanup()

    def test_disable_survives_the_persistence_boundary(self):
        self.assertTrue(rtsp_control.write_mode(1, self.path))
        self.assertEqual(rtsp_control.read_mode(self.path), 1)

    def test_enable_round_trips(self):
        self.assertTrue(rtsp_control.write_mode(2, self.path))
        self.assertEqual(rtsp_control.read_mode(self.path), 2)

    def test_missing_file_uses_documented_default(self):
        self.assertEqual(rtsp_control.read_mode(self.path), rtsp_control.DEFAULT_RTSP_MODE)

    def test_malformed_or_out_of_range_file_uses_default(self):
        for raw in ('', 'abc', '0', '3'):
            with self.subTest(raw=raw):
                with open(self.path, 'w') as f:
                    f.write(raw)
                self.assertEqual(
                    rtsp_control.read_mode(self.path), rtsp_control.DEFAULT_RTSP_MODE
                )

    def test_write_rejects_invalid_mode(self):
        self.assertFalse(rtsp_control.write_mode(0, self.path))
        self.assertFalse(rtsp_control.write_mode(3, self.path))


class RtspStatusPayloadTests(unittest.TestCase):
    def _rtsp(self, state):
        msg = build_status_message(state, token='t', mac='AA:BB:CC:DD:EE:FF', ip='192.0.2.1')
        extended = nested(decode_message(msg)[5])
        return nested(extended[6])

    def test_status_reports_mode_and_actual_running_state(self):
        state = CameraState()
        state.rtsp_mode = 2
        state.rtsp_running = True
        self.assertEqual(self._rtsp(state)[1], 2)
        self.assertEqual(self._rtsp(state)[2], 1)

        state.rtsp_running = False
        self.assertEqual(self._rtsp(state)[1], 2)
        self.assertEqual(self._rtsp(state)[2], 2)

        state.rtsp_mode = 1
        state.rtsp_running = False
        self.assertEqual(self._rtsp(state)[1], 1)
        self.assertEqual(self._rtsp(state)[2], 2)


if __name__ == '__main__':
    unittest.main()
