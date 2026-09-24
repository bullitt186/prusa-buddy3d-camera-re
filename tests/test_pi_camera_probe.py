"""WP-3a AC-16/AC-18: camera probe and sensor hand-off (camera_probe).

Host-only: every case injects a fake ``runner`` (and, where useful, a fake
``captures`` callable). No subprocess, no camera, no root.
"""
import importlib
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import camera_probe  # noqa: E402

# Realistic ``rpicam-hello --list-cameras`` shapes.
ONE_SENSOR = """Available cameras
-----------------
0 : ov5647 [2592x1944] (/base/soc/i2c0mux/i2c@1/ov5647@36)
    Modes: 'SGBRG10_CSI2P' : 640x480 [58.92 fps - (16, 0)/2560x1920 crop]
                             1920x1080 [32.81 fps - (348, 420)/2592x1080 crop]
"""
TWO_SENSORS = """Available cameras
-----------------
0 : ov5647 [2592x1944] (/base/soc/i2c0mux/i2c@1/ov5647@36)
    Modes: 'SGBRG10_CSI2P' : 640x480 [58.92 fps - (16, 0)/2560x1920 crop]
1 : imx708 [4608x2592] (/base/soc/i2c0mux/i2c@1/imx708@1a)
    Modes: 'SRGGB10_CSI2P' : 2304x1296 [56.03 fps - (0, 0)/4608x2592 crop]
"""
NO_SENSORS = """Available cameras
-----------------
No cameras available!
"""


class FakeResult:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_runner(listing=ONE_SENSOR, list_rc=0, fail_match=None,
                timeout_match=None, unavailable_match=None):
    """A fake runner keyed on the command line; never touches hardware."""

    def runner(args, timeout):
        joined = ' '.join(args)
        if unavailable_match and unavailable_match in joined:
            raise FileNotFoundError(2, 'No such file or directory')
        if timeout_match and timeout_match in joined:
            raise subprocess.TimeoutExpired(args, timeout)
        if args[0] == 'rpicam-hello':
            return FakeResult(list_rc, listing)
        if fail_match and fail_match in joined:
            return FakeResult(1, '')
        return FakeResult(0, 'fake-capture-bytes')

    return runner


class ImportSafetyTests(unittest.TestCase):
    def test_import_does_not_execute_subprocess(self):
        with patch.object(
            subprocess, 'run', side_effect=AssertionError('subprocess on import')
        ):
            importlib.reload(camera_probe)
        self.assertTrue(callable(camera_probe.list_sensors))
        self.assertTrue(callable(camera_probe.probe))

    def test_list_command_is_the_documented_invocation(self):
        self.assertEqual(
            camera_probe.LIST_COMMAND, ('rpicam-hello', '--list-cameras')
        )

    def test_capture_commands_are_bounded_and_typed(self):
        still = camera_probe._capture_command(640, 480, 'still')
        self.assertEqual(still[0], 'rpicam-still')
        self.assertIn('--timeout', still)
        self.assertEqual(still[still.index('--width') + 1], '640')
        self.assertEqual(still[still.index('--height') + 1], '480')

        h264 = camera_probe._capture_command(1920, 1080, 'h264')
        self.assertEqual(h264[0], 'rpicam-vid')
        self.assertIn('--codec', h264)
        self.assertEqual(h264[h264.index('--codec') + 1], 'h264')

        jpeg = camera_probe._capture_command(1920, 1080, 'jpeg')
        self.assertEqual(jpeg[0], 'rpicam-jpeg')


class ParseSensorCountTests(unittest.TestCase):
    def test_counts_indexed_entries(self):
        self.assertEqual(camera_probe.parse_sensor_count(ONE_SENSOR), 1)
        self.assertEqual(camera_probe.parse_sensor_count(TWO_SENSORS), 2)

    def test_zero_for_empty_and_unparsable(self):
        for text in (NO_SENSORS, '', None, 'garbage output\nno entries here'):
            with self.subTest(text=text):
                self.assertEqual(camera_probe.parse_sensor_count(text), 0)


class ListSensorsTests(unittest.TestCase):
    def test_one_sensor_listing_ok(self):
        result = camera_probe.list_sensors(runner=make_runner())
        self.assertTrue(result.ok)
        self.assertEqual(result.sensors, 1)
        self.assertEqual(result.reason, '')

    def test_zero_sensors_listing_still_ok(self):
        result = camera_probe.list_sensors(runner=make_runner(listing=NO_SENSORS))
        self.assertTrue(result.ok)
        self.assertEqual(result.sensors, 0)

    def test_command_failure_is_non_retryable(self):
        result = camera_probe.list_sensors(runner=make_runner(list_rc=1))
        self.assertFalse(result.ok)
        self.assertFalse(result.retryable)
        self.assertIn('failed (exit 1)', result.reason)

    def test_timeout_is_retryable(self):
        result = camera_probe.list_sensors(
            runner=make_runner(timeout_match='rpicam-hello')
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.retryable)
        self.assertEqual(result.reason, 'camera probe timed out')

    def test_missing_tool_is_non_retryable(self):
        result = camera_probe.list_sensors(
            runner=make_runner(unavailable_match='rpicam-hello')
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.retryable)
        self.assertEqual(result.reason, 'camera probe tool unavailable')


class ProbeTests(unittest.TestCase):
    def step(self, result, name):
        for entry in result.steps:
            if entry.name == name:
                return entry
        self.fail(f'no step named {name!r} in {[s.name for s in result.steps]}')

    def test_probe_passes_with_exactly_one_sensor(self):
        result = camera_probe.probe(runner=make_runner())
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.sensors, 1)
        self.assertFalse(result.retryable)
        self.assertEqual(
            [s.name for s in result.steps],
            [
                'list_sensors', 'sensor_count',
                'capture_640x480', 'capture_1280x720', 'capture_1920x1080',
                'h264_pipeline', 'jpeg_capture',
            ],
        )
        self.assertTrue(all(s.ok for s in result.steps))

    def test_zero_sensors_fails_with_distinct_reason(self):
        result = camera_probe.probe(runner=make_runner(listing=NO_SENSORS))
        self.assertFalse(result.ok)
        self.assertEqual(result.sensors, 0)
        self.assertEqual(result.reason, 'no CSI sensor detected')
        self.assertTrue(result.retryable)
        self.assertFalse(self.step(result, 'sensor_count').ok)

    def test_two_sensors_fails_with_distinct_reason(self):
        result = camera_probe.probe(runner=make_runner(listing=TWO_SENSORS))
        self.assertFalse(result.ok)
        self.assertEqual(result.sensors, 2)
        self.assertIn('multiple CSI sensors detected (2)', result.reason)
        self.assertFalse(result.retryable)

    def test_each_capture_resolution_is_validated(self):
        for width, height in camera_probe.CAPTURE_RESOLUTIONS:
            with self.subTest(resolution=f'{width}x{height}'):
                runner = make_runner(fail_match=f'--width {width} --height {height}')
                result = camera_probe.probe(runner=runner)
                self.assertFalse(result.ok)
                step = self.step(result, f'capture_{width}x{height}')
                self.assertFalse(step.ok)
                self.assertIn(f'{width}x{height}', step.reason)

    def test_h264_pipeline_failure_is_reported(self):
        result = camera_probe.probe(runner=make_runner(fail_match='rpicam-vid'))
        self.assertFalse(result.ok)
        self.assertFalse(self.step(result, 'h264_pipeline').ok)
        self.assertIn('h264 capture failed', result.reason)

    def test_jpeg_failure_is_reported(self):
        result = camera_probe.probe(runner=make_runner(fail_match='rpicam-jpeg'))
        self.assertFalse(result.ok)
        self.assertFalse(self.step(result, 'jpeg_capture').ok)
        self.assertIn('jpeg capture failed', result.reason)

    def test_capture_without_output_fails(self):
        def runner(args, timeout):
            if args[0] == 'rpicam-hello':
                return FakeResult(0, ONE_SENSOR)
            return FakeResult(0, '')

        result = camera_probe.probe(runner=runner)
        self.assertFalse(result.ok)
        self.assertIn('produced no output', result.reason)

    def test_list_timeout_propagates_as_retryable(self):
        result = camera_probe.probe(runner=make_runner(timeout_match='rpicam-hello'))
        self.assertFalse(result.ok)
        self.assertTrue(result.retryable)
        self.assertEqual(result.reason, 'camera probe timed out')

    def test_capture_timeout_fails_without_raising(self):
        result = camera_probe.probe(runner=make_runner(timeout_match='rpicam-vid'))
        self.assertFalse(result.ok)
        self.assertIn('timed out', result.reason)

    def test_injected_captures_override_backend(self):
        def captures(width, height, codec, runner):
            return (False, 'synthetic capture failure') if codec == 'jpeg' else (True, '')

        result = camera_probe.probe(runner=make_runner(), captures=captures)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'synthetic capture failure')
        self.assertFalse(self.step(result, 'jpeg_capture').ok)

    def test_raising_captures_never_propagates(self):
        def captures(width, height, codec, runner):
            raise RuntimeError('boom')

        result = camera_probe.probe(runner=make_runner(), captures=captures)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'capture step failed')

    def test_raising_runner_never_propagates(self):
        def runner(args, timeout):
            raise RuntimeError('boom')

        result = camera_probe.probe(runner=runner)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'camera probe failed')


class SensorHandoffTests(unittest.TestCase):
    def test_allowed_in_every_pre_runtime_state(self):
        for state in sorted(camera_probe.PRE_RUNTIME_STATES):
            with self.subTest(state=state):
                allowed, reason = camera_probe.sensor_handoff_allowed(state, False)
                self.assertTrue(allowed)
                self.assertIn('allowed', reason)

    def test_denied_while_camera_running(self):
        for state in sorted(camera_probe.PRE_RUNTIME_STATES):
            with self.subTest(state=state):
                allowed, reason = camera_probe.sensor_handoff_allowed(state, True)
                self.assertFalse(allowed)
                self.assertIn('running', reason)

    def test_denied_in_runtime_and_recovery_states(self):
        for state in ('configured', 'running', 'recovery'):
            with self.subTest(state=state):
                allowed, reason = camera_probe.sensor_handoff_allowed(state, False)
                self.assertFalse(allowed)
                self.assertIn(state, reason)

    def test_denied_for_unknown_state(self):
        allowed, reason = camera_probe.sensor_handoff_allowed('bogus', False)
        self.assertFalse(allowed)
        self.assertEqual(reason, 'unknown provisioning state')

    def test_returns_a_bool_and_string(self):
        allowed, reason = camera_probe.sensor_handoff_allowed('unclaimed', False)
        self.assertIsInstance(allowed, bool)
        self.assertIsInstance(reason, str)


class BinaryStdoutTests(unittest.TestCase):
    """The still/JPEG captures stream a binary JPEG to stdout."""

    def test_capture_accepts_binary_stdout(self):
        # Hardware-found regression: the runner decoded stdout as UTF-8, so a
        # JPEG capture raised and every probe failed. Binary must be accepted.
        jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + b'\x00' * 32
        runner = lambda args, timeout: FakeResult(0, jpeg)
        ok, reason = camera_probe._default_capture(640, 480, 'still', runner)
        self.assertTrue(ok, reason)

    def test_default_runner_does_not_decode_as_text(self):
        # Guard the runner: text=True would raise on the binary capture.
        import inspect

        source = inspect.getsource(camera_probe._default_runner)
        self.assertNotIn('text=True', source)

    def test_stdout_handles_bytes(self):
        self.assertEqual(camera_probe._stdout(FakeResult(0, b'abc')), 'abc')


if __name__ == '__main__':
    unittest.main()
