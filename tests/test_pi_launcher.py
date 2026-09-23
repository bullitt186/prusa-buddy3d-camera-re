"""WP-R4c: hermetic host tests for ``image/assets/launcher.sh``.

The launcher resolves what the runtime units execute: a complete signed release
under ``/data/prusa-cam/releases/current`` (and its per-release venv) when one
is installed, otherwise the immutable factory application. These tests never
touch ``/opt`` or ``/data``: the real script is copied into a temp directory
with its ``APP_ROOT``/``RELEASES`` constants rewritten, and the interpreters are
fake ``/bin/sh`` scripts that record their marker and argv.

Stdlib-only; no root, no network, no secrets.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "image" / "assets" / "launcher.sh"

FACTORY_MARKER = "factory"
RELEASE_MARKER = "release"


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.app_root = self.tmp / "opt" / "prusa-cam"
        self.releases = self.tmp / "data" / "prusa-cam" / "releases"
        self.record = self.tmp / "record.txt"

        self.launcher = self.tmp / "launcher.sh"
        self._install_launcher()

        # Immutable factory application.
        self._write_script(self.app_root / "main.py")
        self._write_script(self.app_root / "rtsp_server.py")
        self._write_script(self.app_root / "admin_app.py")
        self._write_fake_python(self.app_root / "venv" / "bin" / "python",
                                FACTORY_MARKER)

    # --- helpers -----------------------------------------------------------

    def _install_launcher(self):
        text = LAUNCHER.read_text(encoding="utf-8")
        rewritten = text.replace(
            "APP_ROOT=/opt/prusa-cam", f"APP_ROOT={self.app_root}"
        ).replace(
            "RELEASES=/data/prusa-cam/releases", f"RELEASES={self.releases}"
        )
        # Guard against a silent rewrite failure if the constants move.
        self.assertIn(str(self.app_root), rewritten)
        self.assertIn(str(self.releases), rewritten)
        self.launcher.write_text(rewritten, encoding="utf-8")
        self.launcher.chmod(0o755)

    def _write_script(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic entry point\n", encoding="utf-8")

    def _write_fake_python(self, path, marker):
        """A fake interpreter that records its marker then each argv element."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' '{marker}' > '{self.record}'\n"
            "for arg in \"$@\"; do printf '%s\\n' \"$arg\" >> '"
            f"{self.record}"
            "' ; done\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        # /tmp can be mounted noexec; skip clearly instead of a false failure.
        probe = subprocess.run([str(path)], capture_output=True, text=True)
        if probe.returncode != 0:
            self.skipTest("temp filesystem is not executable (noexec)")

    def _make_release(self, scripts=("main.py", "rtsp_server.py", "admin_app.py"),
                      venv=True):
        release_dir = self.releases / "1.2.3"
        for name in scripts:
            self._write_script(release_dir / name)
        if venv:
            self._write_fake_python(release_dir / "venv" / "bin" / "python",
                                    RELEASE_MARKER)
        return release_dir

    def _point_current(self, target):
        self.releases.mkdir(parents=True, exist_ok=True)
        os.symlink(target, self.releases / "current")

    def _run(self, *args):
        return subprocess.run(
            ["bash", str(self.launcher), *args],
            capture_output=True,
            text=True,
        )

    def _recorded(self):
        return self.record.read_text(encoding="utf-8").splitlines()

    # --- tests -------------------------------------------------------------

    def test_release_venv_and_script_preferred(self):
        release = self._make_release()
        self._point_current(release)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self._recorded()
        self.assertEqual(lines[0], RELEASE_MARKER)
        self.assertEqual(lines[1], str(self.releases / "current" / "main.py"))

    def test_default_script_is_main_py(self):
        release = self._make_release()
        self._point_current(release)
        self._run()
        self.assertEqual(self._recorded()[1],
                         str(self.releases / "current" / "main.py"))

    def test_factory_fallback_when_current_absent(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self._recorded()
        self.assertEqual(lines[0], FACTORY_MARKER)
        self.assertEqual(lines[1], str(self.app_root / "main.py"))

    def test_factory_fallback_when_current_dangling(self):
        self._point_current(self.releases / "does-not-exist")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self._recorded()
        self.assertEqual(lines[0], FACTORY_MARKER)
        self.assertEqual(lines[1], str(self.app_root / "main.py"))

    def test_factory_fallback_when_release_venv_missing(self):
        release = self._make_release(venv=False)
        self._point_current(release)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._recorded()[0], FACTORY_MARKER)

    def test_factory_fallback_when_release_script_missing(self):
        # A release with a venv but no admin_app.py is half-present.
        release = self._make_release(scripts=("main.py",))
        self._point_current(release)
        result = self._run("admin_app.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self._recorded()
        self.assertEqual(lines[0], FACTORY_MARKER)
        self.assertEqual(lines[1], str(self.app_root / "admin_app.py"))

    def test_release_venv_not_executable_falls_back(self):
        release = self._make_release()
        (release / "venv" / "bin" / "python").chmod(0o644)
        self._point_current(release)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._recorded()[0], FACTORY_MARKER)

    def test_script_argument_and_remaining_args_pass_through(self):
        release = self._make_release()
        self._point_current(release)
        result = self._run("rtsp_server.py", "--flag", "value")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._recorded(),
            [
                RELEASE_MARKER,
                str(self.releases / "current" / "rtsp_server.py"),
                "--flag",
                "value",
            ],
        )

    def test_factory_fallback_passes_args_through(self):
        result = self._run("admin_app.py", "--mode", "admin")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._recorded(),
            [
                FACTORY_MARKER,
                str(self.app_root / "admin_app.py"),
                "--mode",
                "admin",
            ],
        )

    # --- static invariants -------------------------------------------------

    def test_source_is_bash_with_set_eu(self):
        text = LAUNCHER.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/bash\n"))
        self.assertIn("set -eu", text)
        self.assertIn("exec ", text)

    def test_source_has_no_secret_assignments(self):
        text = LAUNCHER.read_text(encoding="utf-8")
        secret_assignment = re.compile(
            r"(?i)\b(token|password|passwd|psk|secret|api[_-]?key)\s*=\s*\S+"
        )
        self.assertIsNone(secret_assignment.search(text), text)
        self.assertNotIn("/home/", text)


if __name__ == "__main__":
    unittest.main()
