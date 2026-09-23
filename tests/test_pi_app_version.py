"""WP-R2 (AC-24): application version resolution (app_version).

Stdlib-only and hermetic: build-info files live in ``tempfile`` and the
environment is injected, so no real image path or process environment is read.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import app_version  # noqa: E402


class ApplicationVersionTests(unittest.TestCase):
    def _write_build_info(self, directory, doc):
        path = os.path.join(directory, 'build-info.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(doc, f)
        return path

    def test_build_info_wins_over_env(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write_build_info(d, {'version': '1.2.3'})
            self.assertEqual(
                app_version.application_version(path, env={'PRUSA_APP_VERSION': '9.9.9'}),
                '1.2.3',
            )

    def test_env_used_when_build_info_has_no_version(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write_build_info(d, {'source_commit': 'abc'})
            self.assertEqual(
                app_version.application_version(path, env={'PRUSA_APP_VERSION': '2.0.0'}),
                '2.0.0',
            )

    def test_default_when_nothing_available(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'missing.json')
            self.assertEqual(
                app_version.application_version(path, env={}),
                app_version.DEFAULT_VERSION,
            )

    def test_missing_file_falls_through_to_env(self):
        self.assertEqual(
            app_version.application_version(
                '/nonexistent/build-info.json', env={'PRUSA_APP_VERSION': '3.1.0'}),
            '3.1.0',
        )

    def test_unreadable_build_info_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'build-info.json')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('{not valid json')
            self.assertEqual(
                app_version.application_version(path, env={}),
                app_version.DEFAULT_VERSION,
            )

    def test_blank_env_falls_through_to_default(self):
        self.assertEqual(
            app_version.application_version('/nonexistent', env={'PRUSA_APP_VERSION': '   '}),
            app_version.DEFAULT_VERSION,
        )

    def test_non_string_version_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write_build_info(d, {'version': 123})
            self.assertEqual(
                app_version.application_version(path, env={}),
                app_version.DEFAULT_VERSION,
            )

    def test_sanitizes_control_characters_and_bounds_length(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write_build_info(d, {'version': '1.0.0\n\x00evil'})
            value = app_version.application_version(path, env={})
        self.assertEqual(value, '1.0.0evil')
        self.assertLessEqual(len(value), app_version.MAX_VERSION_LENGTH)

    def test_env_none_uses_process_environment(self):
        # No crash when env is omitted; the default path is exercised.
        value = app_version.application_version('/nonexistent/build-info.json')
        self.assertIsInstance(value, str)
        self.assertTrue(value)


if __name__ == '__main__':
    unittest.main()
