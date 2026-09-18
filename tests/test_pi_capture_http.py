import ast
import sys
import unittest
from pathlib import Path
from unittest import mock


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import camera  # noqa: E402  (stdlib-only: subprocess/tempfile/os/glob)

UPLOAD_PY = PI_DIR / 'upload.py'


class SnapshotUploadExpect100Tests(unittest.TestCase):
    """GAP-HTTP-01: snapshot PUT must send Expect: 100-continue.

    Inspect upload.py's AST instead of importing it, because upload.py imports
    aiohttp, which the host test environment does not provide.
    """

    def test_upload_snapshot_passes_expect100_true(self):
        tree = ast.parse(UPLOAD_PY.read_text())
        function = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == 'upload_snapshot'
        )
        expect100_calls = [
            call for call in ast.walk(function)
            if isinstance(call, ast.Call)
            and any(
                kw.arg == 'expect100'
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in call.keywords
            )
        ]
        self.assertTrue(expect100_calls, 'upload_snapshot must pass expect100=True (GAP-HTTP-01)')


class SnapshotJpegQualityTests(unittest.TestCase):
    """GAP-SNAPSHOT-03: the capture pipeline encodes JPEG at quality 95."""

    def test_capture_pipeline_uses_jpegenc_quality_95(self):
        captured = {}

        class _Result:
            returncode = 0

        def fake_run(args, **kwargs):
            captured['args'] = list(args)
            location = next(a.split('location=', 1)[1] for a in args if a.startswith('location='))
            with open(location, 'wb') as f:
                f.write(b'\xff' * 200)
            return _Result()

        with mock.patch.object(camera.subprocess, 'run', fake_run):
            data = camera.capture_jpeg()

        self.assertEqual(len(data), 200)
        args = captured['args']
        self.assertIn('jpegenc', args)
        quality_index = args.index('jpegenc') + 1
        self.assertEqual(args[quality_index], 'quality=95')


if __name__ == '__main__':
    unittest.main()
