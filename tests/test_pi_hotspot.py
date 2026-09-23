"""WP-3c AC-17: first-boot setup hotspot control (hotspot).

Host-only: every case injects a fake ``runner``; the import-safety test patches
``subprocess.run`` so no real ``nmcli`` command can run. No interface is touched.
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
import provisioning  # noqa: E402


class FakeResult:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_runner(handlers=(), default=None, timeout_match=None, unavailable_match=None):
    """A fake runner keyed on the command line; records every call."""
    calls = []

    def runner(args, timeout):
        calls.append((list(args), timeout))
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


ACTIVE_AP = (
    ('GENERAL.CONNECTION', FakeResult(0, 'GENERAL.CONNECTION:Hotspot\n')),
    ('802-11-wireless.mode', FakeResult(0, '802-11-wireless.mode:ap\n')),
    ('802-11-wireless.ssid', FakeResult(0, '802-11-wireless.ssid:Buddy3D-Setup-ddeeff\n')),
)
NO_CONNECTION = (('GENERAL.CONNECTION', FakeResult(0, 'GENERAL.CONNECTION:--\n')),)
STATION_MODE = (
    ('GENERAL.CONNECTION', FakeResult(0, 'GENERAL.CONNECTION:HomeNet\n')),
    ('802-11-wireless.mode', FakeResult(0, '802-11-wireless.mode:infrastructure\n')),
)


class ImportSafetyTests(unittest.TestCase):
    def test_import_does_not_execute_subprocess(self):
        with patch.object(
            subprocess, 'run', side_effect=AssertionError('subprocess on import')
        ):
            importlib.reload(hotspot)
        self.assertTrue(callable(hotspot.start))
        self.assertTrue(callable(hotspot.stop))
        self.assertTrue(callable(hotspot.is_active))
        self.assertTrue(callable(hotspot.status))


class ConstantTests(unittest.TestCase):
    def test_captive_portal_address_is_documented(self):
        self.assertEqual(hotspot.captive_portal_address(), '192.168.4.1')
        self.assertEqual(hotspot.CAPTIVE_PORTAL_IP, '192.168.4.1')
        self.assertEqual(hotspot.CAPTIVE_PORTAL_URL, 'http://192.168.4.1')

    def test_setup_ssid_delegates_to_provisioning(self):
        device_id = 'AA:BB:CC:DD:EE:FF'
        self.assertEqual(
            hotspot.setup_ssid(device_id),
            provisioning.setup_ssid(device_id),
        )
        self.assertEqual(hotspot.setup_ssid(device_id), 'Buddy3D-Setup-ddeeff')
        self.assertEqual(hotspot.setup_ssid(''), '')


class StartTests(unittest.TestCase):
    def test_connection_name_and_address_are_pinned(self):
        self.assertEqual(hotspot.CONNECTION_NAME, 'buddy3d-setup')
        self.assertEqual(hotspot.CAPTIVE_PORTAL_PREFIX, '192.168.4.1/24')

    def test_start_pins_address_and_disconnects_first(self):
        runner = make_runner(default=FakeResult(0, ''))
        result = hotspot.start('Buddy3D-Setup-ddeeff', runner=runner)
        self.assertTrue(result.ok)
        self.assertTrue(result.active)
        self.assertEqual(result.ssid, 'Buddy3D-Setup-ddeeff')

        # B1: a best-effort disconnect runs first so a prefilled station
        # profile cannot keep the AP from starting.
        self.assertEqual(
            runner.calls[0][0], ['nmcli', 'device', 'disconnect', 'wlan0']
        )

        add = runner.calls[1][0]
        self.assertEqual(add[:4], ['nmcli', 'connection', 'add', 'type'])
        self.assertIn('con-name', add)
        self.assertEqual(add[add.index('con-name') + 1], 'buddy3d-setup')
        self.assertEqual(add[add.index('ssid') + 1], 'Buddy3D-Setup-ddeeff')
        self.assertEqual(add[add.index('autoconnect') + 1], 'no')
        self.assertEqual(add[add.index('mode') + 1], 'ap')
        self.assertIn('ipv4.method', add)
        self.assertEqual(add[add.index('ipv4.method') + 1], 'shared')
        self.assertEqual(add[add.index('ipv4.addresses') + 1], '192.168.4.1/24')
        self.assertEqual(add[add.index('ipv6.method') + 1], 'disabled')
        # Open AP by design: no WPA2 material is passed (source §4.3).
        self.assertNotIn('wifi-sec.psk', add)
        self.assertNotIn('password', add)

        self.assertEqual(
            runner.calls[2][0],
            ['nmcli', 'connection', 'up', 'buddy3d-setup'],
        )

    def test_start_with_password_passes_it_on_the_add(self):
        runner = make_runner(default=FakeResult(0, ''))
        result = hotspot.start('Buddy3D-Setup-ddeeff', password='longenough', runner=runner)
        self.assertTrue(result.ok)
        add = runner.calls[1][0]
        self.assertIn('wifi-sec.key-mgmt', add)
        self.assertEqual(add[add.index('wifi-sec.psk') + 1], 'longenough')

    def test_start_uses_modify_when_profile_exists(self):
        # `connection add` fails (profile already exists) -> modify then up.
        def runner(args, timeout):
            runner.calls.append((list(args), timeout))
            if 'add' in args:
                return FakeResult(1, '')
            return FakeResult(0, '')

        runner.calls = []
        result = hotspot.start('Buddy3D-Setup-ddeeff', runner=runner)
        self.assertTrue(result.ok)
        modify = runner.calls[2][0]
        self.assertEqual(modify[:3], ['nmcli', 'connection', 'modify'])
        self.assertEqual(modify[3], 'buddy3d-setup')
        self.assertEqual(modify[modify.index('ipv4.addresses') + 1], '192.168.4.1/24')
        self.assertEqual(modify[modify.index('connection.autoconnect') + 1], 'no')

    def test_start_rejects_empty_ssid_without_running(self):
        runner = make_runner()
        for ssid in ('', '   ', None, 123):
            with self.subTest(ssid=ssid):
                result = hotspot.start(ssid, runner=runner)
                self.assertFalse(result.ok)
        self.assertEqual(runner.calls, [])

    def test_start_rejects_bad_password_length_without_running(self):
        runner = make_runner()
        for password in ('short', 'x' * 64):
            with self.subTest(password=password):
                result = hotspot.start('Buddy3D-Setup-ddeeff', password=password, runner=runner)
                self.assertFalse(result.ok)
                self.assertNotIn(password, result.reason)
        self.assertEqual(runner.calls, [])

    def test_start_command_failure_is_reported(self):
        # add and modify both fail -> reported, never raised.
        runner = make_runner(default=FakeResult(1, ''))
        result = hotspot.start('Buddy3D-Setup-ddeeff', runner=runner)
        self.assertFalse(result.ok)
        self.assertIn('exit 1', result.reason)

    def test_start_timeout_is_reported(self):
        runner = make_runner(timeout_match='connection add')
        result = hotspot.start('Buddy3D-Setup-ddeeff', runner=runner)
        self.assertFalse(result.ok)
        self.assertIn('timed out', result.reason)

    def test_start_missing_tool_is_reported(self):
        runner = make_runner(unavailable_match='connection add')
        result = hotspot.start('Buddy3D-Setup-ddeeff', runner=runner)
        self.assertFalse(result.ok)
        self.assertIn('unavailable', result.reason)

    def test_nmcli_stderr_is_logged_on_failure(self):
        # The real reason ("shared connection requires dnsmasq", "AP mode not
        # supported", ...) only appears on nmcli's stderr; it must reach the
        # journal so a headless first-boot failure is diagnosable.
        failure = FakeResult(10, '', 'Error: shared connection requires dnsmasq')
        runner = make_runner(handlers=[
            ('connection add', failure),
            ('connection modify', failure),
        ])
        with self.assertLogs('prusa-cam.hotspot', level='WARNING') as logs:
            result = hotspot.start('Buddy3D-Setup-ddeeff', runner=runner)
        self.assertFalse(result.ok)
        self.assertTrue(
            any('requires dnsmasq' in message for message in logs.output),
            logs.output,
        )


class StopTests(unittest.TestCase):
    def _down_calls(self, runner):
        return [c[0] for c in runner.calls if 'connection' in c[0] and 'down' in c[0]]

    def test_stop_deactivates_only_the_ap_profile(self):
        # Must NOT disconnect the device: that would also tear down the station
        # connection the wizard's finish step just activated.
        runner = make_runner(handlers=ACTIVE_AP, default=FakeResult(0, ''))
        result = hotspot.stop(runner=runner)
        self.assertTrue(result.ok)
        self.assertFalse(result.active)
        self.assertEqual(
            self._down_calls(runner),
            [['nmcli', 'connection', 'down', 'buddy3d-setup']],
        )
        self.assertNotIn(
            ['nmcli', 'device', 'disconnect', 'wlan0'],
            [c[0] for c in runner.calls],
        )

    def test_stop_when_ap_already_down_is_a_noop(self):
        # Idempotent: the ExecStopPost must not fail after finish took the AP down.
        runner = make_runner(default=FakeResult(0, ''))
        result = hotspot.stop(runner=runner)
        self.assertTrue(result.ok)
        self.assertEqual(self._down_calls(runner), [])

    def test_stop_failure_is_reported(self):
        runner = make_runner(handlers=ACTIVE_AP, default=FakeResult(1, ''))
        result = hotspot.stop(runner=runner)
        self.assertFalse(result.ok)
        self.assertIn('exit 1', result.reason)

    def test_stop_timeout_is_reported(self):
        runner = make_runner(handlers=ACTIVE_AP, timeout_match='connection down')
        result = hotspot.stop(runner=runner)
        self.assertFalse(result.ok)
        self.assertIn('timed out', result.reason)


class IsActiveTests(unittest.TestCase):
    def test_active_when_connection_mode_is_ap(self):
        runner = make_runner(handlers=ACTIVE_AP)
        result = hotspot.is_active(runner=runner)
        self.assertTrue(result.ok)
        self.assertTrue(result.active)

    def test_inactive_when_no_connection(self):
        runner = make_runner(handlers=NO_CONNECTION)
        result = hotspot.is_active(runner=runner)
        self.assertTrue(result.ok)
        self.assertFalse(result.active)
        self.assertEqual(len(runner.calls), 1)

    def test_inactive_when_station_mode(self):
        runner = make_runner(handlers=STATION_MODE)
        result = hotspot.is_active(runner=runner)
        self.assertTrue(result.ok)
        self.assertFalse(result.active)

    def test_command_failure_is_reported(self):
        runner = make_runner(default=FakeResult(1, ''))
        result = hotspot.is_active(runner=runner)
        self.assertFalse(result.ok)
        self.assertIn('exit 1', result.reason)


class StatusTests(unittest.TestCase):
    def test_status_active_reports_ssid(self):
        runner = make_runner(handlers=ACTIVE_AP)
        result = hotspot.status(runner=runner)
        self.assertTrue(result.ok)
        self.assertTrue(result.active)
        self.assertEqual(result.ssid, 'Buddy3D-Setup-ddeeff')
        self.assertEqual(result.address, '192.168.4.1')

    def test_status_inactive_is_ok_without_ssid(self):
        runner = make_runner(handlers=NO_CONNECTION)
        result = hotspot.status(runner=runner)
        self.assertTrue(result.ok)
        self.assertFalse(result.active)
        self.assertEqual(result.ssid, '')

    def test_status_timeout_is_reported(self):
        runner = make_runner(timeout_match='GENERAL.CONNECTION')
        result = hotspot.status(runner=runner)
        self.assertFalse(result.ok)
        self.assertIn('timed out', result.reason)


if __name__ == '__main__':
    unittest.main()
