"""GAP-PERSIST-01: durable settings store on /data."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import settings_store  # noqa: E402


class AvailableTests(unittest.TestCase):
    """``available()`` requires /data to be a real mountpoint."""

    def test_requires_isdir_and_ismount(self):
        with patch('settings_store.os.path.isdir', return_value=True), \
                patch('settings_store.os.path.ismount', return_value=True):
            self.assertTrue(settings_store.available())

    def test_not_a_mountpoint_is_unavailable(self):
        with patch('settings_store.os.path.isdir', return_value=True), \
                patch('settings_store.os.path.ismount', return_value=False):
            self.assertFalse(settings_store.available())

    def test_missing_directory_is_unavailable(self):
        with patch('settings_store.os.path.isdir', return_value=False), \
                patch('settings_store.os.path.ismount', return_value=True):
            self.assertFalse(settings_store.available())


class SettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, 'state.json')
        self._available = patch.object(settings_store, 'available', return_value=True)
        self._available.start()
        self.addCleanup(self._available.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip_and_version(self):
        data = {
            'quality_tier': 2,
            'camera_name': 'Print Room',
            'snapshot_interval': 30,
            'snapshot_upload_enabled': False,
            'timelapse_interval': 15,
            'timelapse_enabled': True,
            'timelapse_fps': 12,
            'rtsp_mode': 2,
            'webrtc_mode': 0,
        }
        self.assertTrue(settings_store.save(data, path=self.path))
        loaded = settings_store.load(self.path)
        self.assertEqual(loaded['version'], settings_store.VERSION)
        for key, value in data.items():
            self.assertEqual(loaded[key], value)

    def test_save_adds_version_and_tolerates_unknown_keys(self):
        self.assertTrue(settings_store.save({'bogus': 'keep'}, path=self.path))
        with open(self.path, encoding='utf-8') as f:
            raw = json.load(f)
        self.assertEqual(raw['version'], settings_store.VERSION)
        self.assertEqual(raw['bogus'], 'keep')
        self.assertEqual(settings_store.load(self.path)['bogus'], 'keep')

    def test_missing_file_loads_empty(self):
        self.assertEqual(settings_store.load(self.path), {})

    def test_corrupt_json_is_quarantined_and_empty(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('{not valid json')
        self.assertEqual(settings_store.load(self.path), {})
        self.assertTrue(os.path.exists(self.path + '.bad'))
        self.assertFalse(os.path.exists(self.path))

    def test_non_object_json_is_quarantined(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            f.write('[1, 2, 3]')
        self.assertEqual(settings_store.load(self.path), {})
        self.assertTrue(os.path.exists(self.path + '.bad'))

    def test_save_noop_when_unavailable(self):
        with patch.object(settings_store, 'available', return_value=False):
            self.assertFalse(settings_store.save({'quality_tier': 1}, path=self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_save_atomic_on_replace_failure(self):
        with patch('settings_store.os.replace', side_effect=OSError('boom')):
            self.assertFalse(settings_store.save({'quality_tier': 1}, path=self.path))
        # Neither the final file nor the temp file may be left behind.
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + '.tmp'))


if __name__ == '__main__':
    unittest.main()
