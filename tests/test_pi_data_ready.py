"""AC-7: the durable /data readiness gate (data_ready.check/main).

All checks run against tempfile directories or mocks; nothing here touches the
real ``/data`` or ``/dev/disk/by-label``.
"""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import data_ready  # noqa: E402


class DataReadyCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.mount = os.path.join(self.root, 'data')
        os.makedirs(self.mount)
        self.label = os.path.join(self.root, 'PERSIST')
        with open(self.label, 'w') as f:
            f.write('')

    def tearDown(self):
        self._tmp.cleanup()

    def _ready_patches(self, mount_dev=42, label_dev=42):
        return (
            patch.object(data_ready.os.path, 'ismount', return_value=True),
            patch.object(data_ready, '_mount_device', return_value=mount_dev),
            patch.object(data_ready, '_label_device', return_value=label_dev),
        )

    def test_ready_mount_passes_and_cleans_probe(self):
        p1, p2, p3 = self._ready_patches()
        with p1, p2, p3:
            ok, reason = data_ready.check(self.mount, self.label)
        self.assertTrue(ok, reason)
        self.assertEqual(os.listdir(self.mount), [])

    def test_missing_mount_is_rejected_without_creating_it(self):
        missing = os.path.join(self.root, 'absent')
        ok, reason = data_ready.check(missing, self.label)
        self.assertFalse(ok)
        self.assertIn('not a directory', reason)
        self.assertFalse(os.path.exists(missing))

    def test_non_mountpoint_is_rejected(self):
        with patch.object(data_ready.os.path, 'ismount', return_value=False):
            ok, reason = data_ready.check(self.mount, self.label)
        self.assertFalse(ok)
        self.assertIn('not a mountpoint', reason)
        self.assertEqual(os.listdir(self.mount), [])

    def test_missing_label_is_rejected(self):
        with patch.object(data_ready.os.path, 'ismount', return_value=True):
            ok, reason = data_ready.check(self.mount, os.path.join(self.root, 'nope'))
        self.assertFalse(ok)
        self.assertIn('must be labelled', reason)
        self.assertEqual(os.listdir(self.mount), [])

    def test_wrong_device_is_rejected(self):
        p1, p2, p3 = self._ready_patches(mount_dev=1, label_dev=2)
        with p1, p2, p3:
            ok, reason = data_ready.check(self.mount, self.label)
        self.assertFalse(ok)
        self.assertIn('not the PERSIST partition', reason)
        self.assertEqual(os.listdir(self.mount), [])

    def test_read_only_mount_is_rejected(self):
        p1, p2, p3 = self._ready_patches()
        with p1, p2, p3, patch.object(
            data_ready.os, 'statvfs',
            return_value=SimpleNamespace(f_flag=os.ST_RDONLY),
        ):
            ok, reason = data_ready.check(self.mount, self.label)
        self.assertFalse(ok)
        self.assertIn('read-only', reason)
        self.assertEqual(os.listdir(self.mount), [])

    def test_unwritable_mount_is_rejected(self):
        p1, p2, p3 = self._ready_patches()
        with p1, p2, p3, patch.object(
            data_ready.tempfile, 'mkstemp', side_effect=OSError('read-only fs'),
        ):
            ok, reason = data_ready.check(self.mount, self.label)
        self.assertFalse(ok)
        self.assertIn('not writable', reason)
        self.assertEqual(os.listdir(self.mount), [])

    def test_write_probe_creates_fsyncs_and_removes(self):
        ok, reason = data_ready._write_probe(self.mount)
        self.assertTrue(ok, reason)
        self.assertEqual(os.listdir(self.mount), [])


class DataReadyMainTests(unittest.TestCase):
    def test_main_returns_zero_and_prints_reason_when_ready(self):
        out = io.StringIO()
        with patch.object(data_ready, 'check', return_value=(True, 'ready')), \
                patch('sys.stdout', new=out):
            rc = data_ready.main([])
        self.assertEqual(rc, 0)
        self.assertIn('ready', out.getvalue())

    def test_main_returns_one_and_prints_reason_when_not_ready(self):
        out = io.StringIO()
        with patch.object(data_ready, 'check', return_value=(False, 'not ready')), \
                patch('sys.stdout', new=out):
            rc = data_ready.main([])
        self.assertEqual(rc, 1)
        self.assertIn('not ready', out.getvalue())

    def test_main_accepts_injected_paths(self):
        seen = {}

        def fake_check(mount, label_path):
            seen['mount'] = mount
            seen['label'] = label_path
            return True, 'ok'

        with patch.object(data_ready, 'check', side_effect=fake_check), \
                patch('sys.stdout', new=io.StringIO()):
            data_ready.main(['/mnt/alt', '/dev/disk/by-label/OTHER'])
        self.assertEqual(seen, {'mount': '/mnt/alt', 'label': '/dev/disk/by-label/OTHER'})


if __name__ == '__main__':
    unittest.main()
