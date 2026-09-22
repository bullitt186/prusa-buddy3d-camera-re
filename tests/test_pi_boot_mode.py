"""WP-R1 AC-12/AC-17: boot-mode selection and unit start (boot_mode).

Host-only. Every ``systemctl`` invocation uses an injected fake runner, and the
import-safety test patches ``subprocess.run`` so no real unit is started. State
files are ``tempfile`` paths.
"""
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import boot_mode  # noqa: E402
import provisioning  # noqa: E402


class FakeResult:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_runner(returncode=0, raises=None):
    """A fake runner keyed on a fixed return code; records every call."""
    calls = []

    def runner(args, timeout):
        calls.append((list(args), timeout))
        if raises is not None:
            raise raises
        return FakeResult(returncode)

    runner.calls = calls
    return runner


class ImportSafetyTests(unittest.TestCase):
    def test_import_does_not_execute_subprocess(self):
        with patch.object(
            subprocess, 'run', side_effect=AssertionError('subprocess on import')
        ):
            importlib.reload(boot_mode)
        self.assertTrue(callable(boot_mode.start_camera))
        self.assertTrue(callable(boot_mode.start_unit))
        self.assertTrue(callable(boot_mode.resolve_mode))


class ConstantTests(unittest.TestCase):
    def test_pinned_constants(self):
        self.assertEqual(boot_mode.MODE_PROVISIONING, 'provisioning')
        self.assertEqual(boot_mode.MODE_CAMERA, 'camera')
        self.assertEqual(boot_mode.PROVISIONING_UNIT, 'prusa-provisioning.service')
        self.assertEqual(boot_mode.CAMERA_TARGET, 'prusa-camera.target')


class ResolveModeTests(unittest.TestCase):
    def test_post_claim_states_select_camera(self):
        for state in ('claimed', 'configured', 'running'):
            with self.subTest(state=state):
                self.assertEqual(
                    boot_mode.resolve_mode(state), boot_mode.MODE_CAMERA
                )

    def test_pre_claim_recovery_and_unknown_fail_closed(self):
        for state in (
            'factory',
            'storage_ready',
            'camera_validated',
            'unclaimed',
            'recovery',
            'bogus',
            '',
        ):
            with self.subTest(state=state):
                self.assertEqual(
                    boot_mode.resolve_mode(state), boot_mode.MODE_PROVISIONING
                )

    def test_accepts_state_object(self):
        self.assertEqual(
            boot_mode.resolve_mode(provisioning.ProvisioningState(state='claimed')),
            boot_mode.MODE_CAMERA,
        )
        self.assertEqual(
            boot_mode.resolve_mode(provisioning.ProvisioningState(state='unclaimed')),
            boot_mode.MODE_PROVISIONING,
        )

    def _write(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, 'provisioning.json')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(text)
        return path

    def test_missing_state_file_is_provisioning(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, 'missing.json')
        self.assertEqual(
            boot_mode.resolve_mode(provisioning_path=path),
            boot_mode.MODE_PROVISIONING,
        )

    def test_corrupt_state_file_is_provisioning(self):
        path = self._write('{not valid json')
        self.assertEqual(
            boot_mode.resolve_mode(provisioning_path=path),
            boot_mode.MODE_PROVISIONING,
        )

    def test_non_object_state_file_is_provisioning(self):
        path = self._write('[1, 2, 3]')
        self.assertEqual(
            boot_mode.resolve_mode(provisioning_path=path),
            boot_mode.MODE_PROVISIONING,
        )

    def test_unknown_state_value_is_provisioning(self):
        path = self._write(json.dumps({'state': 'nonsense'}))
        self.assertEqual(
            boot_mode.resolve_mode(provisioning_path=path),
            boot_mode.MODE_PROVISIONING,
        )

    def test_claimed_state_file_selects_camera(self):
        path = self._write(json.dumps({'state': 'claimed'}))
        self.assertEqual(
            boot_mode.resolve_mode(provisioning_path=path), boot_mode.MODE_CAMERA
        )


class UnitForTests(unittest.TestCase):
    def test_unit_for_each_mode(self):
        self.assertEqual(
            boot_mode.unit_for(boot_mode.MODE_CAMERA), boot_mode.CAMERA_TARGET
        )
        self.assertEqual(
            boot_mode.unit_for(boot_mode.MODE_PROVISIONING),
            boot_mode.PROVISIONING_UNIT,
        )

    def test_unknown_mode_fails_closed_to_provisioning(self):
        self.assertEqual(boot_mode.unit_for('nonsense'), boot_mode.PROVISIONING_UNIT)
        self.assertEqual(boot_mode.unit_for(None), boot_mode.PROVISIONING_UNIT)


class StartUnitTests(unittest.TestCase):
    def test_start_unit_uses_no_block_start(self):
        runner = make_runner()
        ok, reason = boot_mode.start_unit(boot_mode.CAMERA_TARGET, runner=runner)
        self.assertTrue(ok)
        self.assertEqual(reason, '')
        self.assertEqual(
            runner.calls[0][0],
            ['systemctl', '--no-block', 'start', 'prusa-camera.target'],
        )

    def test_start_unit_requires_a_name(self):
        runner = make_runner()
        for unit in ('', '   ', None, 123):
            with self.subTest(unit=unit):
                ok, _reason = boot_mode.start_unit(unit, runner=runner)
                self.assertFalse(ok)
        self.assertEqual(runner.calls, [])

    def test_start_unit_nonzero_exit_is_reported(self):
        runner = make_runner(returncode=3)
        ok, reason = boot_mode.start_unit(boot_mode.CAMERA_TARGET, runner=runner)
        self.assertFalse(ok)
        self.assertIn('exit 3', reason)

    def test_start_unit_timeout_is_reported(self):
        runner = make_runner(raises=subprocess.TimeoutExpired('systemctl', 30))
        ok, reason = boot_mode.start_unit(boot_mode.CAMERA_TARGET, runner=runner)
        self.assertFalse(ok)
        self.assertIn('timed out', reason)

    def test_start_unit_missing_tool_is_reported(self):
        runner = make_runner(raises=FileNotFoundError(2, 'No such file'))
        ok, reason = boot_mode.start_unit(boot_mode.CAMERA_TARGET, runner=runner)
        self.assertFalse(ok)
        self.assertIn('unavailable', reason)

    def test_start_unit_generic_failure_does_not_leak_message(self):
        runner = make_runner(raises=RuntimeError('secret-value-xyz'))
        ok, reason = boot_mode.start_unit(boot_mode.CAMERA_TARGET, runner=runner)
        self.assertFalse(ok)
        self.assertNotIn('secret-value-xyz', reason)

    def test_start_camera_returns_bool(self):
        runner = make_runner()
        self.assertTrue(boot_mode.start_camera(runner=runner))
        self.assertEqual(runner.calls[0][0][-1], boot_mode.CAMERA_TARGET)
        self.assertFalse(boot_mode.start_camera(runner=make_runner(returncode=1)))


class MainTests(unittest.TestCase):
    def test_default_prints_mode_and_unit(self):
        with patch.object(
            boot_mode, 'resolve_mode', return_value=boot_mode.MODE_CAMERA
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                status = boot_mode.main([])
        self.assertEqual(status, 0)
        self.assertEqual(
            out.getvalue().split(), [boot_mode.MODE_CAMERA, boot_mode.CAMERA_TARGET]
        )

    def test_apply_starts_camera_target(self):
        runner = make_runner()
        with patch.object(
            boot_mode, 'resolve_mode', return_value=boot_mode.MODE_CAMERA
        ):
            status = boot_mode.main(['--apply'], runner=runner)
        self.assertEqual(status, 0)
        self.assertEqual(runner.calls[0][0][-1], boot_mode.CAMERA_TARGET)

    def test_apply_starts_provisioning_unit(self):
        runner = make_runner()
        with patch.object(
            boot_mode, 'resolve_mode', return_value=boot_mode.MODE_PROVISIONING
        ):
            status = boot_mode.main(['--apply'], runner=runner)
        self.assertEqual(status, 0)
        self.assertEqual(runner.calls[0][0][-1], boot_mode.PROVISIONING_UNIT)

    def test_apply_failure_returns_nonzero(self):
        runner = make_runner(returncode=1)
        with patch.object(
            boot_mode, 'resolve_mode', return_value=boot_mode.MODE_CAMERA
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                status = boot_mode.main(['--apply'], runner=runner)
        self.assertEqual(status, 1)
        self.assertIn(boot_mode.CAMERA_TARGET, err.getvalue())


if __name__ == '__main__':
    unittest.main()
