"""WP-R1 B2: fixed-verb privileged client (privileged).

Host-only: every case injects a fake ``runner``; the import-safety test patches
``subprocess.run`` so no real ``sudo``/helper runs. The PSK is asserted never to
appear in argv or a failure reason.
"""
import importlib
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import hotspot  # noqa: E402
import privileged  # noqa: E402

PSK = 'sup3rsecret'


class FakeResult:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_runner(handlers=(), default=None, timeout_match=None, unavailable_match=None):
    calls = []

    def runner(args, timeout, input=None):
        calls.append((list(args), timeout, input))
        joined = ' '.join(args)
        if unavailable_match and unavailable_match in joined:
            raise FileNotFoundError(2, 'No such file or directory')
        if timeout_match and timeout_match in joined:
            raise subprocess.TimeoutExpired(args, timeout)
        for needle, result in handlers:
            if needle in joined:
                return result
        return default if default is not None else FakeResult(0, '')

    runner.calls = calls
    return runner


def all_argv(runner):
    return [arg for args, _timeout, _stdin in runner.calls for arg in args]


class ImportSafetyTests(unittest.TestCase):
    def test_import_does_not_execute_subprocess(self):
        with patch.object(
            subprocess, 'run', side_effect=AssertionError('subprocess on import')
        ):
            importlib.reload(privileged)
        self.assertTrue(callable(privileged.start_camera))
        self.assertTrue(callable(privileged.activate_station))
        self.assertTrue(callable(privileged.install_update))


class AllowlistTests(unittest.TestCase):
    def test_verbs_are_exactly_the_helper_verbs(self):
        self.assertEqual(
            privileged.VERBS,
            frozenset({
                'start-camera', 'stop-provisioning', 'hotspot-start',
                'hotspot-stop', 'wifi-station-apply', 'install-update',
                'rtsp-start', 'rtsp-stop',
            }),
        )

    def test_rtsp_verbs_match_the_helper(self):
        # Every Python verb must be a case label in the root helper.
        helper = (
            Path(__file__).resolve().parent.parent
            / 'image'
            / 'assets'
            / 'prusa-priv'
        ).read_text(encoding='utf-8')
        for verb in ('rtsp-start', 'rtsp-stop'):
            self.assertIn(f'{verb})', helper)

    def test_unknown_verb_is_rejected_without_running(self):
        runner = make_runner()
        result = privileged._invoke('rm-rf', runner=runner)
        self.assertFalse(result)
        self.assertEqual(runner.calls, [])


class WrapperTests(unittest.TestCase):
    def test_start_camera_uses_sudo_no_prompt_helper(self):
        runner = make_runner()
        self.assertTrue(privileged.start_camera(runner=runner))
        self.assertEqual(
            runner.calls[0][0],
            ['sudo', '-n', '/usr/libexec/prusa-cam/prusa-priv', 'start-camera'],
        )

    def test_stop_provisioning_and_hotspot_verbs(self):
        runner = make_runner()
        self.assertTrue(privileged.stop_provisioning(runner=runner))
        self.assertTrue(privileged.hotspot_start(runner=runner))
        self.assertTrue(privileged.hotspot_stop(runner=runner))
        verbs = [args[-1] for args, _t, _i in runner.calls]
        self.assertEqual(
            verbs, ['stop-provisioning', 'hotspot-start', 'hotspot-stop']
        )

    def test_install_update_uses_sudo_no_prompt_helper(self):
        runner = make_runner()
        result = privileged.install_update(runner=runner)
        self.assertTrue(result)
        self.assertEqual(
            runner.calls[0][0],
            ['sudo', '-n', '/usr/libexec/prusa-cam/prusa-priv', 'install-update'],
        )

    def test_install_update_failure_is_bounded(self):
        runner = make_runner(default=FakeResult(1, ''))
        result = privileged.install_update(runner=runner)
        self.assertFalse(result)
        self.assertIn('install-update failed', result.reason)

    def test_start_camera_failure_returns_false(self):
        runner = make_runner(default=FakeResult(1, ''))
        self.assertFalse(privileged.start_camera(runner=runner))

    def test_activate_station_psk_is_stdin_only(self):
        runner = make_runner()
        result = privileged.activate_station('HomeNet', PSK, runner=runner)
        self.assertTrue(result)
        args = runner.calls[0][0]
        self.assertEqual(
            args[:4],
            ['sudo', '-n', '/usr/libexec/prusa-cam/prusa-priv',
             'wifi-station-apply'],
        )
        self.assertEqual(args[4], 'HomeNet')
        self.assertNotIn(PSK, all_argv(runner))
        self.assertEqual(runner.calls[0][2], PSK)

    def test_activate_station_failure_reason_never_carries_psk(self):
        runner = make_runner(default=FakeResult(1, ''))
        result = privileged.activate_station('HomeNet', PSK, runner=runner)
        self.assertFalse(result)
        self.assertNotIn(PSK, result.reason)
        self.assertIn('wifi-station-apply failed', result.reason)

    def test_activate_station_validates_input_without_running(self):
        runner = make_runner()
        self.assertFalse(privileged.activate_station('', PSK, runner=runner))
        self.assertFalse(privileged.activate_station('HomeNet', 123, runner=runner))
        self.assertEqual(runner.calls, [])

    def test_timeout_is_reported(self):
        runner = make_runner(timeout_match='start-camera')
        result = privileged.start_camera(runner=runner)
        self.assertFalse(result)

    def test_missing_tool_is_reported(self):
        runner = make_runner(unavailable_match='start-camera')
        result = privileged._invoke('start-camera', runner=runner)
        self.assertFalse(result)
        self.assertIn('unavailable', result.reason)

    def test_generic_failure_does_not_leak(self):
        def runner(args, timeout, input=None):
            raise RuntimeError('argv=helper wifi-station-apply ' + PSK)

        result = privileged.activate_station('HomeNet', PSK, runner=runner)
        self.assertFalse(result)
        self.assertNotIn(PSK, result.reason)

    def test_privileged_result_is_falsy_on_failure(self):
        self.assertTrue(privileged.PrivilegedResult(True, ''))
        self.assertFalse(privileged.PrivilegedResult(False, 'x'))


class PrivilegedHotspotTests(unittest.TestCase):
    def test_start_and_stop_route_through_privileged(self):
        calls = []

        def fake_start(runner=None):
            calls.append('start')
            return privileged.PrivilegedResult(True, '')

        def fake_stop(runner=None):
            calls.append('stop')
            return privileged.PrivilegedResult(True, '')

        controller = privileged.PrivilegedHotspot()
        with patch.object(privileged, 'hotspot_start', side_effect=fake_start), \
                patch.object(privileged, 'hotspot_stop', side_effect=fake_stop):
            self.assertTrue(controller.start('Buddy3D-Setup-abc123'))
            self.assertTrue(controller.stop())
        self.assertEqual(calls, ['start', 'stop'])

    def test_status_and_is_active_delegate_to_hotspot(self):
        marker = hotspot.HotspotResult(True, '', active=True)
        with patch.object(privileged.hotspot, 'status', return_value=marker) as status, \
                patch.object(privileged.hotspot, 'is_active', return_value=marker) as is_active:
            controller = privileged.PrivilegedHotspot()
            self.assertIs(controller.status(), marker)
            self.assertIs(controller.is_active(), marker)
        status.assert_called_once()
        is_active.assert_called_once()


if __name__ == '__main__':
    unittest.main()
