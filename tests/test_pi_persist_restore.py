"""GAP-PERSIST-01: persist_restore pure helpers + import safety."""
import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import persist_restore  # noqa: E402
import quality  # noqa: E402


class FramesToPruneTests(unittest.TestCase):
    def test_within_budget_returns_empty(self):
        frames = [('a.jpg', 10, 1.0)]
        self.assertEqual(persist_restore.frames_to_prune(frames, 10, 100), [])
        self.assertEqual(persist_restore.frames_to_prune(frames, 10, 10), [])

    def test_selects_oldest_first(self):
        frames = [
            ('new.jpg', 100, 3.0),
            ('old.jpg', 100, 1.0),
            ('mid.jpg', 100, 2.0),
        ]
        self.assertEqual(
            persist_restore.frames_to_prune(frames, 300, 150),
            [('old.jpg', 100), ('mid.jpg', 100)],
        )

    def test_prunes_all_when_budget_exhausted(self):
        frames = [('b.jpg', 5, 2.0), ('a.jpg', 5, 1.0)]
        self.assertEqual(
            persist_restore.frames_to_prune(frames, 10, 0),
            [('a.jpg', 5), ('b.jpg', 5)],
        )

    def test_empty_frames(self):
        self.assertEqual(persist_restore.frames_to_prune([], 0, 0), [])


class QualityEnvValuesTests(unittest.TestCase):
    def test_tier_mapping(self):
        self.assertEqual(persist_restore.quality_env_values(1), (640, 480))
        self.assertEqual(persist_restore.quality_env_values(2), (1280, 720))
        self.assertEqual(persist_restore.quality_env_values(3), (1920, 1080))

    def test_unknown_tier_falls_back_to_default(self):
        self.assertEqual(
            persist_restore.quality_env_values(99),
            persist_restore.quality_env_values(quality.DEFAULT_QUALITY),
        )


class PruneTimelapseTests(unittest.TestCase):
    """The free-space -> prune-limit conversion and the .avi/CSV safety."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.threshold = persist_restore.PRUNE_FREE_THRESHOLD_BYTES

    def tearDown(self):
        self._tmp.cleanup()

    def _file(self, name, size):
        with open(os.path.join(self.dir, name), 'wb') as f:
            f.write(b'x' * size)

    def test_prunes_oldest_frames_to_reach_threshold(self):
        for i in range(4):
            self._file(f'frame_{i}.jpg', 100)
        # 150 bytes below the threshold -> limit = 400 - 150 = 250 -> drop 2.
        with patch.object(
            persist_restore.shutil, 'disk_usage',
            return_value=SimpleNamespace(free=self.threshold - 150),
        ):
            persist_restore._prune_timelapse(self.dir, mount='/data')
        self.assertEqual(sorted(os.listdir(self.dir)), ['frame_2.jpg', 'frame_3.jpg'])

    def test_never_deletes_avi_or_csv(self):
        self._file('keep.avi', 100)
        self._file('.timelapse_videos.csv', 100)
        self._file('frame_0.jpg', 100)
        with patch.object(
            persist_restore.shutil, 'disk_usage',
            return_value=SimpleNamespace(free=0),
        ):
            persist_restore._prune_timelapse(self.dir, mount='/data')
        self.assertIn('keep.avi', os.listdir(self.dir))
        self.assertIn('.timelapse_videos.csv', os.listdir(self.dir))
        self.assertNotIn('frame_0.jpg', os.listdir(self.dir))

    def test_no_prune_when_above_threshold(self):
        self._file('frame_0.jpg', 100)
        with patch.object(
            persist_restore.shutil, 'disk_usage',
            return_value=SimpleNamespace(free=self.threshold + 1),
        ):
            persist_restore._prune_timelapse(self.dir, mount='/data')
        self.assertEqual(os.listdir(self.dir), ['frame_0.jpg'])


class ImportSafetyTests(unittest.TestCase):
    """Importing must not touch the filesystem (the module runs only via main)."""

    def test_no_module_level_io_calls(self):
        tree = ast.parse(Path(persist_restore.__file__).read_text())
        forbidden = {
            'makedirs', 'mkdir', 'remove', 'unlink', 'chown',
            'write_current', 'write_mode', 'save', 'run',
        }
        offenders = []
        for node in tree.body:
            # Function/class bodies only run when called; skip them.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                name = func.attr if isinstance(func, ast.Attribute) else (
                    func.id if isinstance(func, ast.Name) else ''
                )
                if name in forbidden:
                    offenders.append(name)
        self.assertEqual(offenders, [])

    def test_main_is_inert_when_data_unavailable(self):
        with patch.object(persist_restore.settings_store, 'available', return_value=False):
            self.assertEqual(persist_restore.main(), 0)


if __name__ == '__main__':
    unittest.main()
