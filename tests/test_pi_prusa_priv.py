"""WP-R1 B3: the fixed-verb root helper and its narrow sudoers rule.

Runs the helper with a stubbed PATH / app root so no privileged command, no
NetworkManager and no service is touched. The sudoers rule is validated with
``visudo -cf`` when available.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRUSA_PRIV = REPO / 'image' / 'assets' / 'prusa-priv'
SUDOERS = REPO / 'image' / 'assets' / 'sudoers' / 'prusa-cam'


def run_helper(args, stdin='', env=None, path_prepend=None):
    environ = dict(os.environ)
    if path_prepend:
        environ['PATH'] = f'{path_prepend}:{environ.get("PATH", "")}'
    environ.update(env or {})
    return subprocess.run(
        ['bash', str(PRUSA_PRIV), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=environ,
    )


class PrusaPrivAssetTests(unittest.TestCase):
    def test_helper_exists_and_is_executable(self):
        self.assertTrue(PRUSA_PRIV.is_file())
        self.assertTrue(os.access(PRUSA_PRIV, os.X_OK))

    def test_helper_passes_bash_n(self):
        result = subprocess.run(
            ['bash', '-n', str(PRUSA_PRIV)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_helper_declares_exactly_the_allowlisted_verbs(self):
        text = PRUSA_PRIV.read_text(encoding='utf-8')
        for verb in (
            'start-camera',
            'stop-provisioning',
            'hotspot-start',
            'hotspot-stop',
            'wifi-station-apply',
            'install-update',
        ):
            self.assertIn(f'{verb})', text)
        # Nothing else is dispatched.
        self.assertIn('*)\n      exit 2', text)

    def test_unknown_verb_exits_two(self):
        result = run_helper(['definitely-not-a-verb'])
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_wifi_station_apply_reads_psk_from_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp) / 'app'
            python = app_root / 'venv' / 'bin' / 'python'
            python.parent.mkdir(parents=True)
            args_out = Path(tmp) / 'args'
            stdin_out = Path(tmp) / 'stdin'
            python.write_text(
                '#!/bin/sh\n'
                f'printf "%s\\n" "$@" > "{args_out}"\n'
                f'cat > "{stdin_out}"\n',
                encoding='utf-8',
            )
            python.chmod(0o755)

            result = run_helper(
                ['wifi-station-apply', 'HomeNet'],
                stdin='sup3rsecret',
                env={'PRUSA_CAM_APP_ROOT': str(app_root)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = args_out.read_text(encoding='utf-8')
            self.assertIn('wifi_station.py', argv)
            self.assertIn('apply', argv)
            self.assertIn('HomeNet', argv)
            self.assertNotIn('sup3rsecret', argv)
            self.assertEqual(stdin_out.read_text(encoding='utf-8'), 'sup3rsecret')

    def test_wifi_station_apply_requires_ssid(self):
        result = run_helper(['wifi-station-apply'])
        self.assertEqual(result.returncode, 2)

    def test_start_camera_uses_absolute_systemctl_and_target(self):
        # Text-level: the helper must not rely on the caller's PATH for a root
        # command, so it pins the absolute systemctl path. (Executing the verb
        # here would run the host's real systemctl; the stub-path approach no
        # longer applies once the path is absolute.)
        text = PRUSA_PRIV.read_text(encoding='utf-8')
        self.assertIn('SYSTEMCTL=/usr/bin/systemctl', text)
        self.assertIn('exec "$SYSTEMCTL" start prusa-camera.target', text)
        self.assertIn('exec "$SYSTEMCTL" stop prusa-provisioning.service', text)
        self.assertIn('exec "$SYSTEMCTL" start prusa-updater-install.service', text)
        self.assertNotIn('\n      exec systemctl', text)


class SudoersAssetTests(unittest.TestCase):
    def test_rule_grants_only_the_helper(self):
        text = SUDOERS.read_text(encoding='utf-8')
        self.assertIn(
            'prusa-cam ALL=(root) NOPASSWD: /usr/libexec/prusa-cam/prusa-priv',
            text,
        )
        grant = text.split('NOPASSWD:', 1)[1]
        self.assertNotIn('ALL', grant)
        self.assertNotIn('*', grant)

    def test_rule_pins_env_reset_and_secure_path(self):
        # The privilege boundary must not depend on the base image's global sudo
        # defaults: env_reset blocks PRUSA_CAM_APP_ROOT/PATH injection.
        text = SUDOERS.read_text(encoding='utf-8')
        self.assertIn('Defaults:prusa-cam env_reset', text)
        self.assertIn('Defaults:prusa-cam secure_path=', text)

    def test_visudo_accepts_the_file(self):
        visudo = shutil.which('visudo')
        if visudo is None:
            self.skipTest('visudo not available')
        result = subprocess.run(
            [visudo, '-cf', str(SUDOERS)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_install_sets_root_owner_and_0440(self):
        installer = (REPO / 'image' / 'assets' / 'install-factory-app.sh').read_text(
            encoding='utf-8'
        )
        self.assertIn('-o root -g root -m 0440', installer)
        self.assertIn('/etc/sudoers.d/prusa-cam', installer)
        self.assertIn('visudo -cf', installer)


if __name__ == '__main__':
    unittest.main()
