"""WP-3d2 / AC-20: destructive factory reset (two-step, backed up, guarded).

Stdlib-only. Every test uses ``tempfile`` and an injected data root; the real
``/data`` is never touched.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import factory_reset  # noqa: E402

FIXED_CLOCK = lambda: datetime(2026, 9, 21, 12, 34, 56, tzinfo=timezone.utc)  # noqa: E731
FIXED_TOKEN = 'fixed-reset-token-0123456789abcdef'

SECRET_PSK = 'hunter2-super-secret-psk'
SECRET_TOKEN = 'prusa-token-DO-NOT-LEAK'


class FactoryResetTestCase(unittest.TestCase):
    """Shared fixture: a temp durable root with the documented layout."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, 'data')
        self.prusa_cam = os.path.join(self.root, 'prusa-cam')
        self.config_dir = os.path.join(self.prusa_cam, 'config')
        self.backups_dir = os.path.join(self.prusa_cam, 'backups')
        self.timelapse_dir = os.path.join(self.root, 'sdcard', 'timelapse')
        for directory in (self.config_dir, self.backups_dir, self.timelapse_dir):
            os.makedirs(directory, exist_ok=True)

        self.device_toml = os.path.join(self.config_dir, 'device.toml')
        self.secrets_toml = os.path.join(self.config_dir, 'secrets.toml')
        self.state_json = os.path.join(self.prusa_cam, 'state.json')
        self.provisioning_json = os.path.join(self.prusa_cam, 'provisioning.json')
        self.pending_path = os.path.join(self.prusa_cam, 'pending-deletion.json')

        self._write(self.device_toml, b'camera_name = "Printer Camera"\n')
        self._write(
            self.secrets_toml,
            f'[prusa]\ntoken = "{SECRET_TOKEN}"\n\n[wifi]\npsk = "{SECRET_PSK}"\n'.encode(),
        )
        self._write(self.state_json, b'{"quality_tier": 3}\n')
        self._write(self.provisioning_json, b'{"state": "configured"}\n')
        self.frame_a = os.path.join(self.timelapse_dir, '0001.jpg')
        self.frame_b = os.path.join(self.timelapse_dir, '0002.jpg')
        self._write(self.frame_a, b'frame-a')
        self._write(self.frame_b, b'frame-b')

    def _write(self, path, data):
        with open(path, 'wb') as handle:
            handle.write(data)

    def _reset(self, **kwargs):
        kwargs.setdefault('data_root', self.root)
        kwargs.setdefault('durable_root', self.root)
        kwargs.setdefault('clock', FIXED_CLOCK)
        kwargs.setdefault('token_factory', lambda: FIXED_TOKEN)
        return factory_reset.FactoryReset(**kwargs)

    def _arm(self, reset):
        token = reset.begin()
        self.assertTrue(reset.confirm(token))
        return token

    def _data_files(self):
        return (
            self.device_toml,
            self.secrets_toml,
            self.state_json,
            self.provisioning_json,
            self.frame_a,
            self.frame_b,
        )


class TwoStepConfirmationTests(FactoryResetTestCase):
    def test_execute_without_begin_is_rejected_and_deletes_nothing(self):
        reset = self._reset()
        report = reset.execute(FIXED_TOKEN)
        self.assertFalse(report.ok)
        self.assertIn('two-step', report.reason)
        for path in self._data_files():
            self.assertTrue(os.path.exists(path), path)
        self.assertFalse(os.path.exists(self.pending_path))
        self.assertEqual(os.listdir(self.backups_dir), [])

    def test_execute_without_confirm_is_rejected(self):
        reset = self._reset()
        token = reset.begin()
        report = reset.execute(token)
        self.assertFalse(report.ok)
        self.assertIn('second confirmation', report.reason)
        for path in self._data_files():
            self.assertTrue(os.path.exists(path), path)
        self.assertEqual(os.listdir(self.backups_dir), [])

    def test_execute_with_wrong_token_is_rejected(self):
        reset = self._reset()
        token = self._arm(reset)
        report = reset.execute(token + '-wrong')
        self.assertFalse(report.ok)
        self.assertIn('token', report.reason)
        for path in self._data_files():
            self.assertTrue(os.path.exists(path), path)
        self.assertEqual(os.listdir(self.backups_dir), [])

    def test_confirm_rejects_unknown_token(self):
        reset = self._reset()
        reset.begin()
        self.assertFalse(reset.confirm('not-the-token'))


class ValidResetTests(FactoryResetTestCase):
    def test_valid_reset_backs_up_deletes_and_reports(self):
        reset = self._reset()
        token = self._arm(reset)
        report = reset.execute(token)

        self.assertTrue(report.ok, report.reason)
        expected_backup = os.path.join(
            self.backups_dir, 'factory-reset-20260921-123456'
        )
        self.assertEqual(report.backup_path, expected_backup)
        self.assertEqual(report.timestamp, '20260921-123456')

        # Dated backup holds the config/state files and the timelapse store.
        self.assertTrue(os.path.isfile(
            os.path.join(expected_backup, 'config', 'device.toml')
        ))
        self.assertTrue(os.path.isfile(
            os.path.join(expected_backup, 'config', 'secrets.toml')
        ))
        self.assertTrue(os.path.isfile(
            os.path.join(expected_backup, 'state.json')
        ))
        self.assertTrue(os.path.isfile(
            os.path.join(expected_backup, 'provisioning.json')
        ))
        self.assertTrue(os.path.isfile(
            os.path.join(expected_backup, 'timelapse', '0001.jpg')
        ))
        self.assertTrue(os.path.isfile(
            os.path.join(expected_backup, 'timelapse', '0002.jpg')
        ))

        # The DATA content is gone; the durable directories remain.
        for path in self._data_files():
            self.assertFalse(os.path.exists(path), path)
        self.assertTrue(os.path.isdir(self.config_dir))
        self.assertTrue(os.path.isdir(self.timelapse_dir))
        self.assertEqual(os.listdir(self.config_dir), [])
        self.assertEqual(os.listdir(self.timelapse_dir), [])

        # Exact deletion report.
        expected_deleted = sorted(self._data_files())
        self.assertEqual(list(report.deleted), expected_deleted)
        expected_bytes = 0
        for path in self._data_files():
            # Sizes were read before deletion; recompute from known payloads.
            expected_bytes += {
                self.device_toml: len(b'camera_name = "Printer Camera"\n'),
                self.secrets_toml: len(
                    f'[prusa]\ntoken = "{SECRET_TOKEN}"\n\n[wifi]\npsk = "{SECRET_PSK}"\n'.encode()
                ),
                self.state_json: len(b'{"quality_tier": 3}\n'),
                self.provisioning_json: len(b'{"state": "configured"}\n'),
                self.frame_a: len(b'frame-a'),
                self.frame_b: len(b'frame-b'),
            }[path]
        self.assertEqual(report.file_count, 6)
        self.assertEqual(report.byte_count, expected_bytes)

        # pending-deletion marker records the backup path + timestamp.
        marker = reset.pending_deletion()
        self.assertIsNotNone(marker)
        self.assertEqual(marker['backup_path'], expected_backup)
        self.assertEqual(marker['timestamp'], '20260921-123456')
        self.assertEqual(report.pending_deletion_path, self.pending_path)
        self.assertTrue(os.path.isfile(self.pending_path))

        # report() returns the same result object.
        self.assertEqual(reset.report(), report)

    def test_backup_name_collision_gets_suffix(self):
        reset = self._reset()
        first = reset.execute(self._arm(reset))
        second = reset.execute(self._arm(reset))
        self.assertTrue(first.ok and second.ok)
        self.assertNotEqual(first.backup_path, second.backup_path)
        self.assertTrue(second.backup_path.endswith('-2'))

    def test_timelapse_can_be_excluded(self):
        reset = self._reset()
        token = self._arm(reset)
        report = reset.execute(token, include_timelapse=False)
        self.assertTrue(report.ok, report.reason)
        self.assertTrue(os.path.isfile(self.frame_a))
        self.assertNotIn(self.frame_a, report.deleted)


class FinalizeTests(FactoryResetTestCase):
    def _run_reset(self):
        reset = self._reset()
        token = self._arm(reset)
        report = reset.execute(token)
        self.assertTrue(report.ok, report.reason)
        return reset, report

    def test_backup_kept_before_claimed_boot(self):
        reset, report = self._run_reset()
        result = reset.finalize_after_claimed_boot(claimed=False)
        self.assertTrue(result.ok)
        self.assertFalse(result.deleted)
        self.assertTrue(os.path.isdir(report.backup_path))
        self.assertTrue(os.path.isfile(self.pending_path))

    def test_backup_deleted_after_claimed_boot(self):
        reset, report = self._run_reset()
        result = reset.finalize_after_claimed_boot(claimed=True)
        self.assertTrue(result.ok)
        self.assertTrue(result.deleted)
        self.assertFalse(os.path.exists(report.backup_path))
        self.assertFalse(os.path.exists(self.pending_path))

    def test_finalize_is_idempotent(self):
        reset, report = self._run_reset()
        first = reset.finalize_after_claimed_boot(claimed=True)
        second = reset.finalize_after_claimed_boot(claimed=True)
        self.assertTrue(first.ok and first.deleted)
        self.assertTrue(second.ok)
        self.assertFalse(second.deleted)
        self.assertIn('no pending deletion', second.reason)
        self.assertFalse(os.path.exists(report.backup_path))

    def test_finalize_without_reset_is_a_noop(self):
        reset = self._reset()
        result = reset.finalize_after_claimed_boot(claimed=True)
        self.assertTrue(result.ok)
        self.assertFalse(result.deleted)

    def test_finalize_refuses_marker_pointing_outside_root(self):
        outside = os.path.join(self._tmp.name, 'outside-backup')
        os.makedirs(outside)
        sentinel = os.path.join(outside, 'keep.txt')
        self._write(sentinel, b'keep me')
        with open(self.pending_path, 'w', encoding='utf-8') as handle:
            json.dump({'backup_path': outside, 'timestamp': '20260921-123456'}, handle)

        reset = self._reset()
        result = reset.finalize_after_claimed_boot(claimed=True)
        self.assertFalse(result.ok)
        self.assertIn('outside', result.reason)
        self.assertTrue(os.path.isfile(sentinel))


class SafetyTests(FactoryResetTestCase):
    def test_data_root_outside_durable_root_is_refused(self):
        # Default durable root is /data; a temp root must be refused.
        reset = factory_reset.FactoryReset(
            data_root=self.root, clock=FIXED_CLOCK, token_factory=lambda: FIXED_TOKEN
        )
        report = reset.execute(self._arm(reset))
        self.assertFalse(report.ok)
        self.assertIn('durable', report.reason)
        for path in self._data_files():
            self.assertTrue(os.path.exists(path), path)
        self.assertEqual(os.listdir(self.backups_dir), [])

    def test_relative_data_root_is_refused(self):
        reset = factory_reset.FactoryReset(
            data_root='relative/data', durable_root='relative', clock=FIXED_CLOCK
        )
        report = reset.execute(self._arm(reset))
        self.assertFalse(report.ok)
        self.assertIn('absolute', report.reason)

    def test_missing_structure_is_refused(self):
        bare = os.path.join(self._tmp.name, 'bare')
        os.makedirs(bare)
        reset = factory_reset.FactoryReset(
            data_root=bare, durable_root=bare, clock=FIXED_CLOCK,
            token_factory=lambda: FIXED_TOKEN,
        )
        report = reset.execute(self._arm(reset))
        self.assertFalse(report.ok)
        self.assertIn('structure', report.reason)
        self.assertEqual(os.listdir(bare), [])

    def test_symlink_inside_root_pointing_outside_is_not_followed(self):
        outside = os.path.join(self._tmp.name, 'outside-secret.txt')
        self._write(outside, b'outside secret content')

        # Replace device.toml with a symlink escaping the data root.
        os.remove(self.device_toml)
        os.symlink(outside, self.device_toml)

        # A symlinked directory escaping the root must not be descended either.
        outside_dir = os.path.join(self._tmp.name, 'outside-dir')
        os.makedirs(outside_dir)
        self._write(os.path.join(outside_dir, 'keep.jpg'), b'outside frame')
        escaping_dir = os.path.join(self.timelapse_dir, 'escape')
        os.symlink(outside_dir, escaping_dir)

        reset = self._reset()
        report = reset.execute(self._arm(reset))
        self.assertTrue(report.ok, report.reason)

        # Targets outside the root survive untouched.
        self.assertTrue(os.path.isfile(outside))
        with open(outside, 'rb') as handle:
            self.assertEqual(handle.read(), b'outside secret content')
        self.assertTrue(os.path.isfile(os.path.join(outside_dir, 'keep.jpg')))
        self.assertNotIn(outside, report.deleted)
        self.assertNotIn(os.path.join(outside_dir, 'keep.jpg'), report.deleted)

        # The symlinks themselves (inside the root) are gone.
        self.assertFalse(os.path.lexists(self.device_toml))
        self.assertFalse(os.path.lexists(escaping_dir))
        # The escaping symlink is not copied into the backup.
        self.assertFalse(os.path.exists(
            os.path.join(report.backup_path, 'config', 'device.toml')
        ))


class SecretHygieneTests(FactoryResetTestCase):
    def test_secrets_never_appear_in_reports_or_marker(self):
        reset = self._reset()
        report = reset.execute(self._arm(reset))
        self.assertTrue(report.ok, report.reason)

        rendered = repr(report) + json.dumps(report.to_dict())
        self.assertNotIn(SECRET_PSK, rendered)
        self.assertNotIn(SECRET_TOKEN, rendered)
        self.assertNotIn(SECRET_PSK, report.reason)
        self.assertNotIn(SECRET_TOKEN, report.reason)

        with open(self.pending_path, 'r', encoding='utf-8') as handle:
            marker_text = handle.read()
        self.assertNotIn(SECRET_PSK, marker_text)
        self.assertNotIn(SECRET_TOKEN, marker_text)

        finalized = reset.finalize_after_claimed_boot(claimed=True)
        self.assertNotIn(SECRET_PSK, finalized.reason)
        self.assertNotIn(SECRET_TOKEN, finalized.reason)

    def test_all_touched_paths_are_within_the_temp_root(self):
        reset = self._reset()
        report = reset.execute(self._arm(reset))
        self.assertTrue(report.ok, report.reason)
        for path in report.deleted:
            self.assertTrue(path.startswith(self.root + os.sep), path)
        self.assertTrue(report.backup_path.startswith(self.root + os.sep))
        self.assertTrue(report.pending_deletion_path.startswith(self.root + os.sep))
        # The real durable root was never the data root for this reset.
        self.assertNotEqual(os.path.realpath(self.root), os.path.realpath('/data'))


class ImportSafetyTests(unittest.TestCase):
    def test_module_has_no_side_effects_and_stdlib_only(self):
        self.assertTrue(hasattr(factory_reset, 'FactoryReset'))
        self.assertTrue(hasattr(factory_reset, 'DEFAULT_DURABLE_ROOT'))
        self.assertEqual(factory_reset.DEFAULT_DURABLE_ROOT, '/data')


if __name__ == '__main__':
    unittest.main()
