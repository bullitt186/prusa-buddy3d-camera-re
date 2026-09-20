"""WP-1 AC-6: idempotent legacy importer (legacy_import)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import config_schema  # noqa: E402
import legacy_import  # noqa: E402
import settings_store  # noqa: E402

FIXED_NOW = datetime(2026, 9, 20, 12, 34, 56)
EXPECTED_DIR = 'migration-20260920-123456'


class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.config_ini = os.path.join(self.root, 'config.ini')
        self.state_path = os.path.join(self.root, 'state.json')
        self.backups_root = os.path.join(self.root, 'backups')
        self.device_path = os.path.join(self.root, 'config', 'device.toml')
        self.secrets_path = os.path.join(self.root, 'config', 'secrets.toml')
        self.quality_path = os.path.join(self.root, 'quality.env')
        self.rtsp_path = os.path.join(self.root, 'rtsp.mode')
        self.timelapse_dir = os.path.join(self.root, 'timelapse')

        for target, value in (
            ('legacy_import.DEVICE_TOML_PATH', self.device_path),
            ('legacy_import.SECRETS_TOML_PATH', self.secrets_path),
            ('legacy_import.QUALITY_ENV_PATH', self.quality_path),
            ('legacy_import.RTSP_MODE_PATH', self.rtsp_path),
            ('legacy_import.TIMELAPSE_DIRS', (self.timelapse_dir,)),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        available = patch.object(settings_store, 'available', return_value=True)
        available.start()
        self.addCleanup(available.stop)

    def write_config_ini(self, token='tok-synthetic-123', fingerprint=None,
                         server='cam.example.test', interval=30):
        lines = ['[identity]', f'token = {token}']
        if fingerprint is not None:
            lines.append(f'fingerprint = {fingerprint}')
        lines += ['', '[upload]', f'interval = {interval}', f'server = {server}']
        with open(self.config_ini, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')

    def run_import(self, **overrides):
        kwargs = {
            'config_ini_path': self.config_ini,
            'settings_path': self.state_path,
            'backups_root': self.backups_root,
            'now': FIXED_NOW,
        }
        kwargs.update(overrides)
        return legacy_import.import_legacy(**kwargs)

    def completion_path(self):
        return os.path.join(self.backups_root, EXPECTED_DIR, 'completion.json')

    def read_completion(self):
        with open(self.completion_path(), encoding='utf-8') as f:
            return json.load(f)

    def migration_dirs(self):
        try:
            return [n for n in os.listdir(self.backups_root) if n.startswith('migration-')]
        except OSError:
            return []

    def test_maps_legacy_values_and_writes_documents(self):
        self.write_config_ini(fingerprint='<PLACEHOLDER>')
        result = self.run_import()

        self.assertEqual(result['status'], 'imported')
        self.assertEqual(result['backup_dir'], os.path.join(self.backups_root, EXPECTED_DIR))
        self.assertEqual(result['errors'], [])

        device = config_schema.load_device(self.device_path)
        self.assertEqual(device['fingerprint'], '<PLACEHOLDER>')
        self.assertEqual(device['prusa']['server'], 'cam.example.test')

        secrets = config_schema.load_secrets(self.secrets_path)
        self.assertEqual(secrets['prusa']['token'], 'tok-synthetic-123')

        state = settings_store.load(self.state_path)
        self.assertEqual(state['snapshot_interval'], 30)
        for name in ('prusa.token', 'fingerprint', 'prusa.server', 'snapshot_interval'):
            self.assertIn(name, result['applied'])

    def test_completion_record_has_no_secret_values(self):
        self.write_config_ini()
        self.run_import()
        with open(self.completion_path(), encoding='utf-8') as f:
            raw = f.read()
        self.assertNotIn('tok-synthetic-123', raw)
        record = self.read_completion()
        self.assertEqual(record['source_schema'], legacy_import.SOURCE_SCHEMA)
        self.assertEqual(record['target_schema_version'], config_schema.SCHEMA_VERSION)
        self.assertEqual(record['app_version'], legacy_import.APP_VERSION)
        self.assertEqual(record['timestamp'], FIXED_NOW.isoformat())

    def test_backs_up_source_files(self):
        self.write_config_ini()
        settings_store.save({'quality_tier': 3}, path=self.state_path)
        with open(self.quality_path, 'w', encoding='utf-8') as f:
            f.write('CAM_WIDTH=1920\nCAM_HEIGHT=1080\n')
        with open(self.rtsp_path, 'w', encoding='utf-8') as f:
            f.write('2\n')
        self.run_import()
        backup = os.path.join(self.backups_root, EXPECTED_DIR)
        for name in ('config.ini', 'state.json', 'quality.env', 'rtsp.mode'):
            self.assertTrue(os.path.exists(os.path.join(backup, name)), name)

    def test_second_run_is_noop(self):
        self.write_config_ini()
        self.assertEqual(self.run_import()['status'], 'imported')
        first_dirs = self.migration_dirs()
        result = self.run_import()
        self.assertEqual(result['status'], 'noop')
        self.assertEqual(result['errors'], [])
        self.assertEqual(self.migration_dirs(), first_dirs)

    def test_invalid_input_rejected_with_no_writes(self):
        self.write_config_ini()
        bad = config_schema.default_device()
        bad['mqtt']['uri'] = 'http://not-mqtt.example'
        with patch.object(config_schema, 'default_device', return_value=bad):
            result = self.run_import()
        self.assertEqual(result['status'], 'rejected')
        self.assertTrue(result['errors'])
        self.assertFalse(os.path.exists(self.device_path))
        self.assertFalse(os.path.exists(self.secrets_path))
        self.assertEqual(self.migration_dirs(), [])

    def test_derives_quality_and_rtsp_when_state_lacks_them(self):
        self.write_config_ini()
        with open(self.quality_path, 'w', encoding='utf-8') as f:
            f.write('CAM_WIDTH=1280\nCAM_HEIGHT=720\n')
        with open(self.rtsp_path, 'w', encoding='utf-8') as f:
            f.write('2\n')
        result = self.run_import()
        self.assertEqual(result['status'], 'imported')
        state = settings_store.load(self.state_path)
        self.assertEqual(state['quality_tier'], 2)
        self.assertEqual(state['rtsp_mode'], 2)
        self.assertIn('quality_tier', result['applied'])
        self.assertIn('rtsp_mode', result['applied'])

    def test_preserves_existing_and_unknown_state_keys(self):
        self.write_config_ini(interval=30)
        settings_store.save(
            {'quality_tier': 3, 'snapshot_interval': 45, 'custom_future_key': 'keep'},
            path=self.state_path,
        )
        result = self.run_import()
        state = settings_store.load(self.state_path)
        self.assertEqual(state['snapshot_interval'], 45)
        self.assertEqual(state['quality_tier'], 3)
        self.assertEqual(state['custom_future_key'], 'keep')
        self.assertNotIn('snapshot_interval', result['applied'])

    def test_invalid_persisted_interval_is_replaced(self):
        self.write_config_ini(interval=30)
        settings_store.save({'snapshot_interval': 5}, path=self.state_path)
        self.run_import()
        self.assertEqual(settings_store.load(self.state_path)['snapshot_interval'], 30)

    def test_out_of_range_ini_interval_is_not_applied(self):
        self.write_config_ini(interval=9999)
        result = self.run_import()
        self.assertEqual(result['status'], 'imported')
        self.assertNotIn('snapshot_interval', settings_store.load(self.state_path))
        self.assertNotIn('snapshot_interval', result['applied'])

    def test_timelapse_report_counts_jpg_frames(self):
        self.write_config_ini()
        os.makedirs(self.timelapse_dir)
        for name in ('a.jpg', 'b.jpg', 'c.avi'):
            with open(os.path.join(self.timelapse_dir, name), 'w', encoding='utf-8') as f:
                f.write('x')
        self.run_import()
        report = self.read_completion()['timelapse']
        self.assertEqual(report, {'path': self.timelapse_dir, 'exists': True, 'frame_count': 2})

    def test_missing_timelapse_reports_absent(self):
        self.write_config_ini()
        self.run_import()
        report = self.read_completion()['timelapse']
        self.assertEqual(report, {'path': None, 'exists': False, 'frame_count': 0})

    def test_missing_optional_sources_do_not_fail(self):
        self.write_config_ini(token='', fingerprint=None)
        result = self.run_import()
        self.assertEqual(result['status'], 'imported')
        device = config_schema.load_device(self.device_path)
        self.assertEqual(device['fingerprint'], '')

    def test_missing_config_ini_still_imports_defaults(self):
        result = self.run_import(config_ini_path=os.path.join(self.root, 'absent.ini'))
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(config_schema.load_secrets(self.secrets_path), {})
        self.assertEqual(config_schema.load_device(self.device_path),
                         config_schema.default_device())

    def test_unavailable_data_partition_does_not_crash(self):
        self.write_config_ini()
        with patch.object(settings_store, 'available', return_value=False):
            result = self.run_import()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['errors'], ['/data is not a mountpoint'])
        self.assertFalse(os.path.exists(self.device_path))
        self.assertFalse(os.path.exists(self.secrets_path))
        self.assertFalse(os.path.exists(self.backups_root))

    def test_corrupt_state_rejected_import_leaves_file_untouched(self):
        self.write_config_ini()
        corrupt = '{not valid json'
        with open(self.state_path, 'w', encoding='utf-8') as f:
            f.write(corrupt)
        bad = config_schema.default_device()
        bad['mqtt']['uri'] = 'http://not-mqtt.example'
        with patch.object(config_schema, 'default_device', return_value=bad):
            result = self.run_import()
        self.assertEqual(result['status'], 'rejected')
        with open(self.state_path, encoding='utf-8') as f:
            self.assertEqual(f.read(), corrupt)
        self.assertFalse(os.path.exists(self.state_path + '.bad'))
        self.assertEqual(self.migration_dirs(), [])

    def test_secrets_and_device_file_modes(self):
        self.write_config_ini()
        self.run_import()
        import stat as stat_module
        self.assertEqual(stat_module.S_IMODE(os.stat(self.device_path).st_mode), 0o640)
        self.assertEqual(stat_module.S_IMODE(os.stat(self.secrets_path).st_mode), 0o600)


if __name__ == '__main__':
    unittest.main()
