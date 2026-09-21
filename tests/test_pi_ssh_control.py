"""WP-3d1 AC-20: SSH enable/disable control (ssh_control).

Host-only: every case injects a fake ``runner``; the import-safety test patches
``subprocess.run`` so no real ``systemctl`` command can run.
"""
import importlib
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import admin_auth  # noqa: E402
import ssh_control  # noqa: E402


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


class ImportSafetyTests(unittest.TestCase):
    def test_import_does_not_execute_subprocess(self):
        with patch.object(
            subprocess, 'run', side_effect=AssertionError('subprocess on import')
        ):
            importlib.reload(ssh_control)
        self.assertTrue(callable(ssh_control.enable_ssh))
        self.assertTrue(callable(ssh_control.disable_ssh))
        self.assertTrue(callable(ssh_control.ssh_enabled))


class ContractPinTests(unittest.TestCase):
    def test_ssh_is_disabled_by_default(self):
        self.assertFalse(ssh_control.SSH_ENABLED_BY_DEFAULT)

    def test_service_and_reauth_action_are_pinned(self):
        self.assertEqual(ssh_control.SSH_SERVICE, 'ssh')
        self.assertEqual(ssh_control.REAUTH_ACTION, 'ssh_enable')

    def test_enable_is_reauth_gated_by_admin_auth(self):
        self.assertTrue(admin_auth.requires_reauth(ssh_control.REAUTH_ACTION))


class EnableTests(unittest.TestCase):
    def test_enable_success_uses_documented_command(self):
        runner = make_runner(default=FakeResult(0, ''))
        result = ssh_control.enable_ssh(runner=runner)
        self.assertTrue(result.ok)
        self.assertTrue(result.enabled)
        self.assertEqual(
            runner.calls[0][0], ['systemctl', 'enable', '--now', 'ssh']
        )

    def test_enable_failure_is_reported(self):
        runner = make_runner(default=FakeResult(1, 'job failed'))
        result = ssh_control.enable_ssh(runner=runner)
        self.assertFalse(result.ok)
        self.assertFalse(result.enabled)
        self.assertIn('exit 1', result.reason)

    def test_enable_timeout_is_reported(self):
        runner = make_runner(timeout_match='enable')
        result = ssh_control.enable_ssh(runner=runner)
        self.assertFalse(result.ok)
        self.assertIn('timed out', result.reason)

    def test_enable_missing_tool_is_reported(self):
        runner = make_runner(unavailable_match='enable')
        result = ssh_control.enable_ssh(runner=runner)
        self.assertFalse(result.ok)
        self.assertIn('unavailable', result.reason)


class DisableTests(unittest.TestCase):
    def test_disable_success_uses_documented_command(self):
        runner = make_runner(default=FakeResult(0, ''))
        result = ssh_control.disable_ssh(runner=runner)
        self.assertTrue(result.ok)
        self.assertFalse(result.enabled)
        self.assertEqual(
            runner.calls[0][0], ['systemctl', 'disable', '--now', 'ssh']
        )

    def test_disable_failure_is_reported(self):
        runner = make_runner(default=FakeResult(1, 'job failed'))
        result = ssh_control.disable_ssh(runner=runner)
        self.assertFalse(result.ok)
        self.assertIn('exit 1', result.reason)

    def test_disable_timeout_is_reported(self):
        runner = make_runner(timeout_match='disable')
        result = ssh_control.disable_ssh(runner=runner)
        self.assertFalse(result.ok)
        self.assertIn('timed out', result.reason)


class SshEnabledTests(unittest.TestCase):
    def test_enabled_reports_true(self):
        runner = make_runner(default=FakeResult(0, 'enabled\n'))
        result = ssh_control.ssh_enabled(runner=runner)
        self.assertTrue(result.ok)
        self.assertTrue(result.enabled)
        self.assertEqual(
            runner.calls[0][0], ['systemctl', 'is-enabled', 'ssh']
        )

    def test_disabled_reports_false_but_ok(self):
        runner = make_runner(default=FakeResult(1, 'disabled\n'))
        result = ssh_control.ssh_enabled(runner=runner)
        self.assertTrue(result.ok)
        self.assertFalse(result.enabled)

    def test_timeout_is_reported_as_failure(self):
        runner = make_runner(timeout_match='is-enabled')
        result = ssh_control.ssh_enabled(runner=runner)
        self.assertFalse(result.ok)
        self.assertIn('timed out', result.reason)

    def test_missing_tool_is_reported_as_failure(self):
        runner = make_runner(unavailable_match='is-enabled')
        result = ssh_control.ssh_enabled(runner=runner)
        self.assertFalse(result.ok)
        self.assertIn('unavailable', result.reason)


if __name__ == '__main__':
    unittest.main()
