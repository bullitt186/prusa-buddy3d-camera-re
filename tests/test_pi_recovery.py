"""WP-3d1 AC-20: recovery entry via BOOT sentinel (recovery).

Host-only. Every sentinel path is a ``tempfile`` path; no ``/boot``, ``/data``
or ``/etc`` path is touched.
"""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import recovery  # noqa: E402


class SentinelTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.sentinel = os.path.join(self.root, 'boot', 'buddy3d-recovery')


class ImportSafetyTests(unittest.TestCase):
    def test_import_runs_no_subprocess_or_write(self):
        with patch.object(os, 'open', side_effect=AssertionError('os.open on import')):
            importlib.reload(recovery)
        self.assertTrue(callable(recovery.set_sentinel))
        self.assertTrue(callable(recovery.clear_sentinel))
        self.assertTrue(callable(recovery.enter_setup_mode))
        self.assertTrue(callable(recovery.boot_action))


class ContractPinTests(unittest.TestCase):
    def test_sentinel_path_is_documented(self):
        self.assertEqual(
            recovery.RECOVERY_SENTINEL, '/boot/firmware/buddy3d-recovery'
        )

    def test_boot_targets_are_pinned(self):
        self.assertEqual(recovery.BOOT_SETUP, 'setup')
        self.assertEqual(recovery.BOOT_CAMERA, 'camera')
        self.assertEqual(recovery.BOOT_RECOVERY, 'recovery')


class SentinelTests(SentinelTestBase):
    def test_present_false_when_missing(self):
        self.assertFalse(recovery.sentinel_present(self.sentinel))
        self.assertFalse(recovery.sentinel_present(''))

    def test_set_then_present_then_clear_round_trip(self):
        result = recovery.set_sentinel('manual recovery', path=self.sentinel)
        self.assertTrue(result.ok)
        self.assertTrue(recovery.sentinel_present(self.sentinel))
        with open(self.sentinel, encoding='utf-8') as f:
            self.assertIn('manual recovery', f.read())
        cleared = recovery.clear_sentinel(path=self.sentinel)
        self.assertTrue(cleared.ok)
        self.assertFalse(recovery.sentinel_present(self.sentinel))

    def test_set_creates_missing_parent_directory(self):
        self.assertFalse(os.path.isdir(os.path.dirname(self.sentinel)))
        self.assertTrue(recovery.set_sentinel(path=self.sentinel).ok)
        self.assertTrue(os.path.isfile(self.sentinel))

    def test_clear_absent_is_idempotent(self):
        result = recovery.clear_sentinel(path=self.sentinel)
        self.assertTrue(result.ok)
        self.assertFalse(recovery.sentinel_present(self.sentinel))

    def test_set_sanitizes_reason(self):
        recovery.set_sentinel('line one\nline two', path=self.sentinel)
        with open(self.sentinel, encoding='utf-8') as f:
            content = f.read()
        self.assertNotIn('\nline two', content)

    def test_set_invalid_path_is_rejected_without_raising(self):
        result = recovery.set_sentinel('x', path='')
        self.assertFalse(result.ok)
        self.assertTrue(result.reason)


class EnterSetupModeTests(SentinelTestBase):
    def test_unauthorized_writes_nothing(self):
        result = recovery.enter_setup_mode(
            'token=SUPERSECRET', authorized=False, path=self.sentinel
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.action, 'setup')
        self.assertNotIn('SUPERSECRET', result.reason)
        self.assertFalse(os.path.exists(self.sentinel))
        self.assertFalse(os.path.exists(os.path.dirname(self.sentinel)))

    def test_authorized_sets_sentinel(self):
        result = recovery.enter_setup_mode(
            'operator requested setup', authorized=True, path=self.sentinel
        )
        self.assertTrue(result.ok)
        self.assertTrue(recovery.sentinel_present(self.sentinel))
        with open(self.sentinel, encoding='utf-8') as f:
            self.assertIn('operator requested setup', f.read())

    def test_truthy_but_not_true_authorized_is_rejected(self):
        for value in (1, 'yes', None, 0, False):
            with self.subTest(value=value):
                result = recovery.enter_setup_mode('x', value, path=self.sentinel)
                self.assertFalse(result.ok)
                self.assertFalse(os.path.exists(self.sentinel))


class RecoveryRequiredTests(unittest.TestCase):
    def test_not_required_by_default(self):
        required, reason = recovery.recovery_required('running')
        self.assertFalse(required)
        self.assertEqual(reason, '')

    def test_manual(self):
        self.assertEqual(
            recovery.recovery_required('running', manual=True),
            (True, 'manual recovery requested'),
        )

    def test_invalid_config(self):
        required, reason = recovery.recovery_required('running', invalid_config=True)
        self.assertTrue(required)
        self.assertIn('invalid', reason)

    def test_storage_unavailable(self):
        required, reason = recovery.recovery_required(
            'running', storage_unavailable=True
        )
        self.assertTrue(required)
        self.assertIn('storage', reason)

    def test_state_recovery(self):
        required, reason = recovery.recovery_required('recovery')
        self.assertTrue(required)
        self.assertIn('recovery', reason)

    def test_accepts_state_object(self):
        state = SimpleNamespace(state='recovery')
        required, _reason = recovery.recovery_required(state)
        self.assertTrue(required)

    def test_manual_takes_precedence(self):
        required, reason = recovery.recovery_required(
            'recovery', invalid_config=True, manual=True
        )
        self.assertTrue(required)
        self.assertIn('manual', reason)


class BootActionTests(unittest.TestCase):
    def test_sentinel_forces_setup(self):
        self.assertEqual(
            recovery.boot_action(True, 'recovery'), recovery.BOOT_SETUP
        )

    def test_state_recovery(self):
        self.assertEqual(
            recovery.boot_action(False, 'recovery'), recovery.BOOT_RECOVERY
        )

    def test_invalid_config(self):
        self.assertEqual(
            recovery.boot_action(False, 'running', invalid_config=True),
            recovery.BOOT_RECOVERY,
        )

    def test_storage_unavailable(self):
        self.assertEqual(
            recovery.boot_action(False, 'running', storage_unavailable=True),
            recovery.BOOT_RECOVERY,
        )

    def test_normal_boot_starts_camera(self):
        self.assertEqual(
            recovery.boot_action(False, 'running'), recovery.BOOT_CAMERA
        )

    def test_sentinel_beats_recovery_state(self):
        self.assertEqual(
            recovery.boot_action(True, 'recovery'), recovery.BOOT_SETUP
        )


if __name__ == '__main__':
    unittest.main()
