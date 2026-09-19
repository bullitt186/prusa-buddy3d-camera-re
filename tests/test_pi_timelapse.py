"""GAP-TIMELAPSE-01: Pi storage-backed timelapse helpers."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import timelapse  # noqa: E402
from state import CameraState  # noqa: E402


class TimelapseValidationTests(unittest.TestCase):
    def test_valid_interval(self):
        self.assertEqual(timelapse.valid_interval('30'), 30)
        self.assertIsNone(timelapse.valid_interval(0))
        self.assertIsNone(timelapse.valid_interval(3601))
        self.assertIsNone(timelapse.valid_interval('x'))

    def test_valid_fps(self):
        self.assertEqual(timelapse.valid_fps(12), 12)
        self.assertIsNone(timelapse.valid_fps(0))
        self.assertIsNone(timelapse.valid_fps(31))

    def test_apply_enable(self):
        state = CameraState()
        self.assertFalse(state.timelapse_enabled)
        self.assertTrue(timelapse.apply_enable('timelapse_enable', state))
        self.assertTrue(state.timelapse_enabled)
        self.assertTrue(timelapse.apply_enable('timelapse_disable', state))
        self.assertFalse(state.timelapse_enabled)
        self.assertFalse(timelapse.apply_enable('unknown', state))


class TimelapseStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_frame_naming_and_index(self):
        self.assertEqual(timelapse.frame_name(1), 'frame_00001.jpg')
        self.assertEqual(timelapse.next_index(self.dir), 1)
        timelapse.save_frame(b'one', self.dir, 1)
        self.assertEqual(timelapse.next_index(self.dir), 2)

    def test_list_frames_sorted_and_filtered(self):
        for i in (2, 1, 10):
            timelapse.save_frame(b'x', self.dir, i)
        with open(os.path.join(self.dir, 'notes.txt'), 'w') as f:
            f.write('ignore me')
        self.assertEqual(
            timelapse.list_frames(self.dir),
            ['frame_00001.jpg', 'frame_00002.jpg', 'frame_00010.jpg'],
        )

    def test_list_frames_missing_dir_is_empty(self):
        self.assertEqual(timelapse.list_frames(os.path.join(self.dir, 'nope')), [])

    def test_build_mjpeg_concatenates_frames(self):
        timelapse.save_frame(b'AAA', self.dir, 1)
        timelapse.save_frame(b'BBB', self.dir, 2)
        out = os.path.join(self.dir, 'out.mjpeg')
        path = timelapse.build_mjpeg(self.dir, out)
        self.assertEqual(path, out)
        with open(out, 'rb') as f:
            self.assertEqual(f.read(), b'AAABBB')

    def test_build_mjpeg_empty_returns_none(self):
        self.assertIsNone(timelapse.build_mjpeg(self.dir, os.path.join(self.dir, 'out.mjpeg')))


class StorageStatusTests(unittest.TestCase):
    """GAP-TIMELAPSE-01: emulated-SD telemetry for extended_status.4."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_sd_present_true_for_writable_dir(self):
        self.assertTrue(timelapse.sd_present(self.dir))

    def test_sd_present_false_for_missing_path(self):
        self.assertFalse(timelapse.sd_present(os.path.join(self.dir, 'nope')))

    def test_sd_present_true_for_read_only_dir(self):
        # FUN_00071bc0 uses access(path, R_OK); write access is the separate mode
        # string, so a readable-but-not-writable mountpoint is still "present".
        with patch('timelapse.os.access', side_effect=lambda path, mode: mode == os.R_OK):
            self.assertTrue(timelapse.sd_present(self.dir))
            self.assertEqual(timelapse.sd_mode(self.dir), 'RO')

    def test_sd_space_returns_consistent_megabytes(self):
        total, free, used = timelapse.sd_space(self.dir)
        for value in (total, free, used):
            self.assertIsInstance(value, int)
            self.assertGreaterEqual(value, 0)
        # FUN_000745e0 floors each MB value independently, so `used` computed
        # from (f_blocks - f_bfree) can differ from (total - free) by at most
        # 1 MB. Assert the firmware relationship without that rounding artifact.
        self.assertLessEqual(abs(used - (total - free)), 1)

    def test_storage_status_present(self):
        present, total, free, used, mode = timelapse.storage_status(self.dir)
        self.assertEqual(present, 1)
        self.assertEqual(mode, 'RW')
        self.assertEqual(total, timelapse.sd_space(self.dir)[0])
        self.assertLessEqual(abs(used - (total - free)), 1)

    def test_storage_status_absent(self):
        self.assertEqual(
            timelapse.storage_status(os.path.join(self.dir, 'nope')),
            (2, 0, 0, 0, 'UNKNOWN'),
        )

    def test_sd_mode_read_only(self):
        # Patch sd_present directly so the RO branch is reached without relying
        # on os.access side effects inside sd_present.
        with patch('timelapse.sd_present', return_value=True), \
                patch('timelapse.os.access', return_value=False):
            self.assertEqual(timelapse.sd_mode(self.dir), 'RO')


if __name__ == '__main__':
    unittest.main()
