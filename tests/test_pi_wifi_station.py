"""WP-R1 B2: post-claim Wi-Fi station activation (wifi_station).

Host-only: every case injects a fake ``runner``; the import-safety test patches
``subprocess.run`` so no real ``nmcli`` command can run. No interface is touched,
and the PSK is asserted never to appear in argv, logs, or reasons.
"""
import importlib
import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import wifi_station  # noqa: E402

PSK = 'sup3rsecret'


class FakeResult:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_runner(handlers=(), default=None, timeout_match=None, unavailable_match=None,
                captured=None):
    """A fake runner keyed on the command line; records every call + stdin.

    When ``captured`` is given, the passwd-file named by a
    ``nmcli connection up … passwd-file <file>`` call is read (content + mode)
    *while it still exists*, so tests can assert its 0600 mode and exact line.
    """
    calls = []

    def runner(args, timeout, input=None):
        calls.append((list(args), timeout, input))
        if captured is not None and 'passwd-file' in args:
            path = args[args.index('passwd-file') + 1]
            captured['path'] = path
            try:
                captured['content'] = Path(path).read_text(encoding='utf-8')
                captured['mode'] = stat.S_IMODE(os.stat(path).st_mode)
            except OSError:
                captured['content'] = None
                captured['mode'] = None
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
            importlib.reload(wifi_station)
        self.assertTrue(callable(wifi_station.apply))
        self.assertTrue(callable(wifi_station.main))


class ConstantTests(unittest.TestCase):
    def test_station_profile_name_is_deterministic(self):
        self.assertEqual(wifi_station.CONNECTION_NAME, 'buddy3d-station')


class ApplyTests(unittest.TestCase):
    def test_open_network_adds_autoconnect_auto_profile(self):
        runner = make_runner(default=FakeResult(0, ''))
        ok, reason = wifi_station.apply('HomeNet', '', runner=runner)
        self.assertTrue(ok, reason)

        add = runner.calls[0][0]
        self.assertEqual(add[:4], ['nmcli', 'connection', 'add', 'type'])
        self.assertEqual(add[add.index('con-name') + 1], 'buddy3d-station')
        self.assertEqual(add[add.index('ssid') + 1], 'HomeNet')
        self.assertEqual(add[add.index('autoconnect') + 1], 'yes')
        self.assertEqual(add[add.index('ipv4.method') + 1], 'auto')
        self.assertNotIn('wifi-sec.key-mgmt', add)

        # Open network: no credential file and no PSK, but stale security is
        # dropped.
        self.assertNotIn('passwd-file', all_argv(runner))
        self.assertNotIn('--ask', all_argv(runner))
        self.assertIn(
            ['nmcli', 'connection', 'modify', 'buddy3d-station',
             'remove', '802-11-wireless-security'],
            [args for args, _t, _i in runner.calls],
        )
        self.assertEqual(
            runner.calls[-1][0],
            ['nmcli', 'connection', 'up', 'buddy3d-station'],
        )

    def test_wpa_network_uses_0600_passwd_file_not_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            captured = {}
            runner = make_runner(default=FakeResult(0, ''), captured=captured)
            ok, reason = wifi_station.apply(
                'HomeNet', PSK, runner=runner, passwd_dir=tmp
            )
            self.assertTrue(ok, reason)

            # The PSK is never on the command line.
            self.assertNotIn(PSK, all_argv(runner))
            add = runner.calls[0][0]
            self.assertEqual(add[add.index('wifi-sec.key-mgmt') + 1], 'wpa-psk')
            self.assertNotIn('wifi-sec.psk', add)

            # A 0600 passwd-file with the documented single line was used.
            self.assertEqual(captured['mode'], 0o600)
            self.assertEqual(
                captured['content'],
                f'802-11-wireless-security.psk:{PSK}\n',
            )
            self.assertTrue(captured['path'].startswith(tmp))
            self.assertEqual(
                runner.calls[-1][0],
                ['nmcli', 'connection', 'up', 'buddy3d-station',
                 'passwd-file', captured['path']],
            )
            # Removed afterwards.
            self.assertFalse(os.path.exists(captured['path']))

    def test_passwd_file_removed_when_activation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            captured = {}
            runner = make_runner(
                handlers=[('connection up', FakeResult(1, ''))],
                captured=captured,
            )
            ok, reason = wifi_station.apply(
                'HomeNet', PSK, runner=runner, passwd_dir=tmp
            )
            self.assertFalse(ok)
            self.assertIn('activation failed', reason)
            self.assertNotIn(PSK, reason)
            self.assertTrue(captured['path'])
            self.assertFalse(os.path.exists(captured['path']))

    def test_passwd_file_write_failure_does_not_connect(self):
        runner = make_runner(default=FakeResult(0, ''))
        with patch.object(
            wifi_station.tempfile, 'mkstemp', side_effect=OSError('nope')
        ):
            ok, reason = wifi_station.apply('HomeNet', PSK, runner=runner)
        self.assertFalse(ok)
        self.assertIn('credential file', reason)
        self.assertNotIn(PSK, reason)
        # The profile add ran, but activation must not be attempted.
        self.assertFalse(
            any('up' in args for args, _t, _i in runner.calls)
        )

    def test_existing_profile_falls_back_to_modify(self):
        def runner(args, timeout, input=None):
            runner.calls.append((list(args), timeout, input))
            if 'add' in args:
                return FakeResult(1, '')
            return FakeResult(0, '')

        runner.calls = []
        ok, reason = wifi_station.apply('HomeNet', PSK, runner=runner)
        self.assertTrue(ok, reason)
        modify = runner.calls[1][0]
        self.assertEqual(modify[:3], ['nmcli', 'connection', 'modify'])
        self.assertEqual(modify[3], 'buddy3d-station')
        self.assertEqual(modify[modify.index('ipv4.method') + 1], 'auto')

    def test_add_and_modify_failure_is_reported(self):
        runner = make_runner(default=FakeResult(1, ''))
        ok, reason = wifi_station.apply('HomeNet', PSK, runner=runner)
        self.assertFalse(ok)
        self.assertIn('exit 1', reason)
        self.assertNotIn(PSK, reason)

    def test_up_failure_is_reported(self):
        runner = make_runner(handlers=[('connection up', FakeResult(1, ''))])
        ok, reason = wifi_station.apply('HomeNet', PSK, runner=runner)
        self.assertFalse(ok)
        self.assertIn('activation failed', reason)
        self.assertNotIn(PSK, reason)

    def test_timeout_is_reported(self):
        runner = make_runner(timeout_match='connection add')
        ok, reason = wifi_station.apply('HomeNet', PSK, runner=runner)
        self.assertFalse(ok)
        self.assertIn('timed out', reason)

    def test_missing_tool_is_reported(self):
        runner = make_runner(unavailable_match='connection add')
        ok, reason = wifi_station.apply('HomeNet', PSK, runner=runner)
        self.assertFalse(ok)
        self.assertIn('unavailable', reason)

    def test_generic_runner_failure_does_not_leak(self):
        def runner(args, timeout, input=None):
            raise RuntimeError('argv=nmcli password ' + PSK)

        ok, reason = wifi_station.apply('HomeNet', PSK, runner=runner)
        self.assertFalse(ok)
        self.assertNotIn(PSK, reason)

    def test_validation_rejects_bad_input_without_running(self):
        runner = make_runner()
        for ssid in ('', '   ', None, 123):
            with self.subTest(ssid=ssid):
                ok, _reason = wifi_station.apply(ssid, '', runner=runner)
                self.assertFalse(ok)
        ok, reason = wifi_station.apply('HomeNet', 'short', runner=runner)
        self.assertFalse(ok)
        self.assertNotIn('short', reason)
        ok, reason = wifi_station.apply('HomeNet', 'x' * 64, runner=runner)
        self.assertFalse(ok)
        self.assertEqual(runner.calls, [])


class MainTests(unittest.TestCase):
    def _run(self, argv, stdin_text):
        captured = {}

        def fake_apply(ssid, psk='', ifname=wifi_station.DEFAULT_IFNAME, runner=None):
            captured['ssid'] = ssid
            captured['psk'] = psk
            return captured.get('result', (True, ''))

        with patch.object(wifi_station, 'apply', side_effect=fake_apply), \
                patch.object(sys, 'stdin', io.StringIO(stdin_text)):
            code = wifi_station.main(argv)
        return code, captured

    def test_reads_psk_from_stdin(self):
        code, captured = self._run(['apply', 'HomeNet'], PSK + '\n')
        self.assertEqual(code, 0)
        self.assertEqual(captured['ssid'], 'HomeNet')
        self.assertEqual(captured['psk'], PSK)

    def test_empty_stdin_is_open_network(self):
        code, captured = self._run(['apply', 'HomeNet'], '')
        self.assertEqual(code, 0)
        self.assertEqual(captured['psk'], '')

    def test_failure_exits_nonzero_without_leaking_psk(self):
        captured = {}

        def fake_apply(ssid, psk='', ifname=wifi_station.DEFAULT_IFNAME, runner=None):
            return False, 'nmcli station activation failed (exit 1)'

        with patch.object(wifi_station, 'apply', side_effect=fake_apply), \
                patch.object(sys, 'stdin', io.StringIO(PSK + '\n')), \
                patch('sys.stderr', new_callable=io.StringIO) as stderr:
            code = wifi_station.main(['apply', 'HomeNet'])
        self.assertEqual(code, 1)
        self.assertNotIn(PSK, stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
