"""Tests for GAP-DEVICE-01 (reboot) and GAP-DEVICE-02 (hardware truthfulness).

Stdlib-only: imports ``device_control``, ``state``, ``status`` and ``proto`` but
never ``socketio``/``aiohttp``/``gi``/GStreamer, and never touches systemd. The
reboot action is an injected callable, so these tests perform no real reboot.
"""
import ast
import sys
import unittest
from pathlib import Path
from unittest import mock


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

MAIN_PY = PI_DIR / 'main.py'

from proto import decode_message  # noqa: E402
from state import CameraState  # noqa: E402
from status import build_status_message  # noqa: E402
import device_control  # noqa: E402


def nested(raw):
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    return decode_message(raw)


def recording_reboot(calls):
    def reboot():
        calls.append('reboot')
        return True
    return reboot


class RebootGuardTests(unittest.TestCase):
    def test_can_reboot_first_request_and_window_boundary(self):
        interval = device_control.DEFAULT_REBOOT_MIN_INTERVAL_SECONDS
        self.assertTrue(device_control.can_reboot(None, 1000.0, interval))
        self.assertTrue(device_control.can_reboot(1000.0, 1000.0 + interval, interval))
        self.assertFalse(device_control.can_reboot(1000.0, 1000.0 + interval - 0.001, interval))

    def test_backwards_clock_fails_closed(self):
        self.assertFalse(device_control.can_reboot(1000.0, 999.0, 60))

    def test_first_request_reboots_once_and_records_time(self):
        state = CameraState()
        calls = []
        self.assertTrue(
            device_control.request_reboot(state, recording_reboot(calls), now=500.0)
        )
        self.assertEqual(calls, ['reboot'])
        self.assertEqual(state.last_reboot_monotonic, 500.0)

    def test_second_request_inside_window_is_rejected_without_rebooting(self):
        state = CameraState()
        calls = []
        reboot = recording_reboot(calls)
        interval = device_control.DEFAULT_REBOOT_MIN_INTERVAL_SECONDS
        self.assertTrue(device_control.request_reboot(state, reboot, now=500.0))
        self.assertFalse(
            device_control.request_reboot(state, reboot, now=500.0 + interval - 1)
        )
        self.assertEqual(calls, ['reboot'])
        self.assertEqual(state.last_reboot_monotonic, 500.0)

    def test_request_after_window_is_allowed_again(self):
        state = CameraState()
        calls = []
        reboot = recording_reboot(calls)
        interval = device_control.DEFAULT_REBOOT_MIN_INTERVAL_SECONDS
        self.assertTrue(device_control.request_reboot(state, reboot, now=500.0))
        self.assertTrue(
            device_control.request_reboot(state, reboot, now=500.0 + interval)
        )
        self.assertEqual(calls, ['reboot', 'reboot'])

    def test_failing_reboot_fn_is_reported_and_does_not_crash(self):
        state = CameraState()

        def boom():
            raise RuntimeError('systemctl unavailable')

        self.assertFalse(device_control.request_reboot(state, boom, now=10.0))
        # The failed attempt is still recorded so a burst cannot hot-loop it.
        self.assertEqual(state.last_reboot_monotonic, 10.0)

    def test_reboot_fn_returning_false_is_not_success(self):
        state = CameraState()
        self.assertFalse(device_control.request_reboot(state, lambda: False, now=1.0))

    def test_default_now_uses_monotonic(self):
        state = CameraState()
        with mock.patch.object(device_control.time, 'monotonic', return_value=123.0):
            self.assertTrue(device_control.request_reboot(state, lambda: True))
        self.assertEqual(state.last_reboot_monotonic, 123.0)

    def test_guard_state_lives_on_the_shared_object(self):
        first = CameraState()
        second = CameraState()
        self.assertTrue(device_control.request_reboot(first, lambda: True, now=1.0))
        self.assertFalse(device_control.request_reboot(first, lambda: True, now=1.0))
        # A different state object has its own window.
        self.assertTrue(device_control.request_reboot(second, lambda: True, now=1.0))


class HardwareAvailabilityTests(unittest.TestCase):
    def test_camera_state_reports_all_hardware_unavailable(self):
        state = CameraState()
        self.assertFalse(state.ir_available)
        self.assertFalse(state.speaker_available)
        self.assertFalse(state.fan_available)
        self.assertFalse(state.microsd_available)
        self.assertIsNone(state.ir_mode)

    def test_light_control_mode_mapping_matches_fw_config(self):
        self.assertEqual(device_control.light_control_mode('auto'), 1)
        self.assertEqual(device_control.light_control_mode('day'), 2)
        self.assertEqual(device_control.light_control_mode('night'), 3)
        self.assertEqual(device_control.light_control_mode(' NIGHT '), 3)
        # The SIO configuration protobuf sends the mode as an integer.
        self.assertEqual(device_control.light_control_mode(1), 1)
        self.assertEqual(device_control.light_control_mode(2), 2)
        self.assertEqual(device_control.light_control_mode(3), 3)
        for invalid in ('off', '', 0, 9, None, True, b'auto', {'mode': 1}):
            with self.subTest(invalid=invalid):
                self.assertIsNone(device_control.light_control_mode(invalid))

    def test_light_control_never_claims_a_fake_applied_mode(self):
        state = CameraState()
        for value in ('auto', 'night', 'day'):
            with self.subTest(value=value):
                self.assertFalse(device_control.apply_light_control(value, state))
                self.assertIsNone(state.ir_mode)

    def test_light_control_rejects_unrecognized_values(self):
        state = CameraState()
        for value in (None, 1, '', 'flicker', {'mode': 1}):
            with self.subTest(value=value):
                self.assertFalse(device_control.apply_light_control(value, state))
        self.assertIsNone(state.ir_mode)

    def test_light_control_applies_only_when_hardware_reports_available(self):
        state = CameraState()
        state.ir_available = True
        self.assertTrue(device_control.apply_light_control('night', state))
        self.assertEqual(state.ir_mode, 3)

    def test_status_hardware_bytes_unchanged_pending_descriptor(self):
        # GAP-DEVICE-02 limitation: the nested camera-status tag map is not
        # recovered, so the existing IR-mode/speaker-volume bytes are left as-is
        # rather than guessed. This pins the current encoding so a future
        # descriptor-driven change is deliberate, not accidental.
        state = CameraState()
        top = decode_message(build_status_message(state, token='t', mac='m', ip='i'))
        camera_status = nested(top[3])
        self.assertEqual(camera_status[5], 1)
        self.assertEqual(camera_status[6], 40)


class MainWiringTests(unittest.TestCase):
    """AST checks that main.py wires the reboot action to the narrow command.

    main.py imports aiohttp/socketio, so parse its AST instead of importing it.
    """

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(MAIN_PY.read_text())

    def _function(self, name):
        return next(
            node for node in ast.walk(self.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        )

    def test_reboot_device_runs_only_the_narrow_systemd_command(self):
        runs = [
            node for node in ast.walk(self._function('reboot_device'))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'run'
        ]
        self.assertEqual(len(runs), 1)
        command = runs[0].args[0]
        self.assertIsInstance(command, ast.List)
        self.assertEqual(
            [elt.value for elt in command.elts], ['sudo', 'systemctl', 'reboot']
        )

    def test_dispatch_calls_request_reboot_with_the_injected_reboot_fn(self):
        dispatcher = self._function('dispatch_trigger_action')
        calls = [
            node for node in ast.walk(dispatcher)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'request_reboot'
        ]
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(calls[0].func.value, ast.Name)
        self.assertEqual(calls[0].func.value.id, 'device_control')
        self.assertEqual(len(calls[0].args), 2)
        self.assertIsInstance(calls[0].args[1], ast.Name)
        self.assertEqual(calls[0].args[1].id, 'reboot_device')

    def test_reboot_device_is_referenced_only_by_the_trigger_dispatcher(self):
        # It is passed as the injected callable, not called directly anywhere.
        references = [
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.Name) and node.id == 'reboot_device'
        ]
        self.assertEqual(len(references), 1)


if __name__ == '__main__':
    unittest.main()
