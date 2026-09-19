"""GAP-TIMELAPSE-01: Pi storage-backed timelapse helpers."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == '__main__':
    unittest.main()
