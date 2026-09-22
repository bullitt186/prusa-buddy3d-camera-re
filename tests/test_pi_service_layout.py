"""AC-1/AC-2/AC-7: dedicated service account, durable layout, DATA gate.

Reads the unit templates and exercises persist_restore's layout creation with
mocks; nothing here touches the real ``/data`` or creates system accounts.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import persist_restore  # noqa: E402

APP_UNITS = (
    'rpicam-source.service',
    'prusa-cam.service',
    'prusa-rtsp.service',
    'prusa-ha-rtsp.service',
)
ALL_UNITS = APP_UNITS + (
    'bootlog.service',
    'pi-persist.service',
    'prusa-data-ready.service',
    'data-ready.target',
)
# Files this contract is allowed to touch that must not name a personal account
# or a home-directory install path.
SERVICE_FILES = (
    'persist_restore.py',
    'quality.py',
    'data_ready.py',
    'deploy.sh',
    'bootstrap.sh',
) + tuple('systemd/' + name for name in ALL_UNITS)


def unit(name):
    return (PI_DIR / 'systemd' / name).read_text()


class ServiceIdentityConstantsTests(unittest.TestCase):
    def test_default_service_user_is_dedicated(self):
        self.assertEqual(persist_restore.DEFAULT_SERVICE_USER, 'prusa-cam')

    def test_durable_layout_constants(self):
        self.assertEqual(persist_restore.DATA_CONFIG_DIR, '/data/prusa-cam/config')
        self.assertEqual(persist_restore.DATA_RELEASES_DIR, '/data/prusa-cam/releases')
        self.assertEqual(persist_restore.DATA_BACKUPS_DIR, '/data/prusa-cam/backups')
        self.assertEqual(persist_restore.DATA_NETWORK_DIR, '/data/network')
        self.assertEqual(
            persist_restore.DATA_NETWORK_CONNECTIONS,
            '/data/network/system-connections',
        )

    def test_layout_covers_all_durable_dirs_with_safe_modes(self):
        modes = dict(persist_restore.DATA_LAYOUT)
        for path in (
            '/data/sdcard',
            '/data/sdcard/timelapse',
            '/data/prusa-cam',
            '/data/prusa-cam/config',
            '/data/prusa-cam/releases',
            '/data/prusa-cam/backups',
            '/data/network',
            '/data/network/system-connections',
        ):
            self.assertIn(path, modes)
        # config/backups hold secrets and migration backups: not world-readable.
        self.assertEqual(modes['/data/prusa-cam/config'], 0o750)
        self.assertEqual(modes['/data/prusa-cam/backups'], 0o750)


class DurableLayoutCreationTests(unittest.TestCase):
    def test_main_creates_and_chowns_every_dir(self):
        calls = {'makedirs': [], 'chmod': [], 'chown': []}
        with patch.object(persist_restore.settings_store, 'available', return_value=True), \
                patch.object(persist_restore.os, 'makedirs',
                             side_effect=lambda p, exist_ok=False: calls['makedirs'].append(p)), \
                patch.object(persist_restore.os, 'chmod',
                             side_effect=lambda p, m: calls['chmod'].append((p, m))), \
                patch.object(persist_restore, '_chown',
                             side_effect=lambda p, u: calls['chown'].append((p, u))), \
                patch.object(persist_restore, '_bind_mount'), \
                patch.object(persist_restore, '_restore_settings'), \
                patch.object(persist_restore, '_prune_timelapse'):
            self.assertEqual(persist_restore.main(), 0)

        expected = [path for path, _mode in persist_restore.DATA_LAYOUT]
        self.assertEqual(calls['makedirs'], expected)
        # The NetworkManager keyfile store stays root-owned (B4): it is created
        # but never chowned to the service account, because it is bind-mounted
        # onto /etc/NetworkManager/system-connections and consumed by root.
        expected_chown = [
            path for path in expected if path not in persist_restore.ROOT_ONLY_DIRS
        ]
        self.assertEqual([p for p, _u in calls['chown']], expected_chown)
        self.assertEqual({u for _p, u in calls['chown']}, {'prusa-cam'})
        chmod = dict(calls['chmod'])
        self.assertEqual(chmod['/data/prusa-cam/config'], 0o750)
        self.assertEqual(chmod['/data/prusa-cam/backups'], 0o750)
        self.assertEqual(chmod['/data/network'], 0o700)
        self.assertEqual(chmod['/data/network/system-connections'], 0o700)

    def test_root_only_dirs_are_never_chowned(self):
        self.assertEqual(
            persist_restore.ROOT_ONLY_DIRS,
            {'/data/network', '/data/network/system-connections'},
        )

    def test_service_user_env_override(self):
        users = []
        with patch.object(persist_restore.settings_store, 'available', return_value=True), \
                patch.object(persist_restore.os, 'makedirs'), \
                patch.object(persist_restore.os, 'chmod'), \
                patch.object(persist_restore, '_chown',
                             side_effect=lambda p, u: users.append(u)), \
                patch.object(persist_restore, '_bind_mount'), \
                patch.object(persist_restore, '_restore_settings'), \
                patch.object(persist_restore, '_prune_timelapse'), \
                patch.dict(os.environ, {'SERVICE_USER': 'custom-svc'}):
            persist_restore.main()
        self.assertTrue(users)
        self.assertEqual(set(users), {'custom-svc'})

    def test_main_inert_when_data_unavailable(self):
        with patch.object(persist_restore.settings_store, 'available', return_value=False), \
                patch.object(persist_restore.os, 'makedirs',
                             side_effect=AssertionError('must not create dirs')):
            self.assertEqual(persist_restore.main(), 0)


class UnitLayoutTests(unittest.TestCase):
    def test_app_units_use_dedicated_account_and_app_root(self):
        for name in APP_UNITS:
            with self.subTest(unit=name):
                text = unit(name)
                self.assertIn('User=prusa-cam', text)
                self.assertIn('/opt/prusa-cam', text)
                self.assertNotIn('/home/', text)

    def test_app_units_are_gated_on_data_ready(self):
        for name in APP_UNITS:
            with self.subTest(unit=name):
                text = unit(name)
                after = [ln for ln in text.splitlines() if ln.startswith('After=')]
                requires = [ln for ln in text.splitlines() if ln.startswith('Requires=')]
                self.assertTrue(
                    any('data-ready.target' in ln for ln in after), text)
                self.assertTrue(
                    any('data-ready.target' in ln for ln in requires), text)

    def test_ha_unit_still_requires_rpicam_source(self):
        self.assertIn('Requires=rpicam-source.service', unit('prusa-ha-rtsp.service'))

    def test_bootlog_and_persist_use_app_root(self):
        self.assertIn('ExecStart=/opt/prusa-cam/bootlog.sh', unit('bootlog.service'))
        persist = unit('pi-persist.service')
        self.assertIn('Environment=SERVICE_USER=prusa-cam', persist)
        self.assertIn('/opt/prusa-cam/persist_restore.py', persist)
        self.assertIn('RequiresMountsFor=/data', persist)
        self.assertIn('ConditionPathIsMountPoint=/data', persist)

    def test_pi_persist_runs_before_gate_and_services(self):
        before = [ln for ln in unit('pi-persist.service').splitlines()
                  if ln.startswith('Before=')][0]
        for name in ('prusa-data-ready.service', 'data-ready.target') + APP_UNITS:
            with self.subTest(name=name):
                self.assertIn(name, before)

    def test_data_ready_gate_units(self):
        service = unit('prusa-data-ready.service')
        self.assertIn('Type=oneshot', service)
        self.assertIn('RemainAfterExit=yes', service)
        self.assertIn('RequiresMountsFor=/data', service)
        self.assertIn('After=local-fs.target pi-persist.service', service)
        self.assertIn('Before=data-ready.target', service)
        self.assertIn('/opt/prusa-cam/data_ready.py', service)
        self.assertIn('WantedBy=data-ready.target', service)

        target = unit('data-ready.target')
        self.assertIn('Requires=prusa-data-ready.service', target)
        self.assertIn('After=prusa-data-ready.service', target)
        self.assertIn('WantedBy=multi-user.target', target)

    def test_no_personal_username_or_home_path(self):
        for rel in SERVICE_FILES:
            with self.subTest(file=rel):
                text = (PI_DIR / rel).read_text()
                self.assertNotIn('bullitt', text)
                self.assertNotIn('/home/pi', text)
                self.assertNotIn('User=pi', text)


if __name__ == '__main__':
    unittest.main()
