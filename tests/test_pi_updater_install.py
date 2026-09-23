"""WP-R4b (AC-29/AC-30/AC-31): signed update install orchestration.

Stdlib-only and hermetic: the orchestration is exercised entirely with fakes
for the injectable I/O (download, signature verification, extraction, venv
build, preflight, switch, health, restart, record_bad, prune, clock, sleeper).
The only real filesystem operations are inside per-test temporary directories.
No network, no ``minisign``/``zstd``/``systemctl`` binary is ever invoked.
"""
import ast
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PI_DIR))

import updater  # noqa: E402
import updater_install as ui  # noqa: E402

SHA_A = 'a' * 64


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def valid_manifest(**overrides):
    doc = {
        'schema_version': 1,
        'version': '1.1.0',
        'channel': 'stable',
        'source_commit': 'deadbeef' * 5,
        'min_image_version': '1.0.0',
        'bundle_url': 'https://example.com/buddy3d-camera-app-1.1.0.tar.zst',
        'bundle_sha256': SHA_A,
        'bundle_size': 1024,
        'release_summary': 'Fixes a camera bug.',
        'release_url': 'https://example.com/releases/1.1.0',
        'reboot_required': False,
    }
    doc.update(overrides)
    return doc


def make_manifest(**overrides):
    return updater.parse_manifest(valid_manifest(**overrides))


class FakeClock:
    """Injectable clock/sleeper pair; ``sleep`` advances the clock."""

    def __init__(self, start=0.0):
        self.now = float(start)
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += max(0.0, float(seconds))


def make_paths(tmp, *, factory=True):
    root = Path(tmp)
    releases = root / 'releases'
    factory_app = root / 'factory'
    if factory:
        factory_app.mkdir(parents=True, exist_ok=True)
        (factory_app / 'main.py').write_text('# factory\n', encoding='utf-8')
    return ui.InstallPaths(
        releases_dir=str(releases),
        current_link=str(releases / 'current'),
        previous_link=str(releases / 'previous'),
        factory_app=str(factory_app),
        state_dir=str(root),
    )


class Harness:
    """Fake injectable I/O that records the call order.

    ``fail_at`` selects a step that returns a bounded failure; ``health`` is the
    health-check result; ``free`` is the free-space value.
    """

    def __init__(self, paths, *, fail_at=None, health=True, free=10 ** 9,
                 secrets=(), download_size=None):
        self.paths = paths
        self.fail_at = fail_at
        self.health = health
        self.free = free
        self.secrets = secrets
        self.download_size = download_size
        self.calls = []

    # -- steps --------------------------------------------------------------
    def download(self, manifest, staging):
        self.calls.append('download')
        if self.fail_at == 'download':
            return False, 'download failed'
        size = self.download_size
        if size is None:
            size = manifest.bundle_size
        path = os.path.join(staging, ui.BUNDLE_FILENAME)
        with open(path, 'wb') as handle:
            handle.write(b'x' * size)
        return True, path

    def verify_manifest_signature(self, manifest, staging):
        self.calls.append('verify_manifest')
        if self.fail_at == 'manifest_sig':
            return False, 'bad manifest signature'
        return True, ''

    def verify_bundle_signature(self, bundle_path):
        self.calls.append('verify_bundle')
        if self.fail_at == 'bundle_sig':
            return False, 'bad bundle signature'
        return True, ''

    def free_space(self, path):
        return self.free

    def extract(self, bundle_path, dest, *, expected_sha256=None):
        self.calls.append('extract')
        if self.fail_at == 'extract':
            return False, 'extraction failed'
        (Path(dest) / 'main.py').write_text('# release\n', encoding='utf-8')
        return True, ''

    def build_venv(self, staging, manifest):
        self.calls.append('build_venv')
        if self.fail_at == 'venv':
            return False, 'venv failed'
        return True, ''

    def preflight(self, staging, manifest):
        self.calls.append('preflight')
        if self.fail_at == 'preflight':
            return False, 'preflight failed'
        return True, ''

    def switch(self, paths, staging, version):
        self.calls.append('switch')
        if self.fail_at == 'switch':
            return False, 'switch failed'
        return ui.switch_release(paths, staging, version)

    def health_check(self, version):
        self.calls.append('health')
        return self.health

    def restart_services(self, version):
        self.calls.append(('restart', version))
        if self.fail_at == 'restart':
            return False, 'restart failed'
        return True, ''

    def record_bad(self, version, reason):
        self.calls.append(('record_bad', version, reason))

    def prune(self, paths, protected):
        self.calls.append(('prune', tuple(protected)))

    # -- runner -------------------------------------------------------------
    def install(self, manifest, *, clock=None, sleeper=None,
                force_reinstall=False):
        clock = clock or FakeClock()
        return ui.install_update(
            manifest,
            paths=self.paths,
            download=self.download,
            verify_manifest_signature=self.verify_manifest_signature,
            verify_bundle_signature=self.verify_bundle_signature,
            free_space=self.free_space,
            extract=self.extract,
            build_venv=self.build_venv,
            preflight=self.preflight,
            switch=self.switch,
            health_check=self.health_check,
            restart_services=self.restart_services,
            record_bad=self.record_bad,
            prune=self.prune,
            clock=clock,
            sleeper=sleeper or clock.sleep,
            secrets=self.secrets,
            force_reinstall=force_reinstall,
        )


# --------------------------------------------------------------------------- #
# InstallPaths / constants
# --------------------------------------------------------------------------- #

class InstallPathsTests(unittest.TestCase):
    def test_defaults(self):
        paths = ui.InstallPaths()
        self.assertEqual(paths.releases_dir, '/data/prusa-cam/releases')
        self.assertEqual(paths.current_link, '/data/prusa-cam/releases/current')
        self.assertEqual(paths.previous_link, '/data/prusa-cam/releases/previous')
        self.assertEqual(paths.factory_app, '/opt/prusa-cam')
        self.assertEqual(paths.state_dir, '/data/prusa-cam')

    def test_version_dir(self):
        paths = ui.InstallPaths(releases_dir='/tmp/x')
        self.assertEqual(paths.version_dir('1.2.3'), '/tmp/x/1.2.3')


# --------------------------------------------------------------------------- #
# switch_release + rollback helpers (real filesystem, temp dirs)
# --------------------------------------------------------------------------- #

class SwitchReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = make_paths(self.tmp.name)

    def _staging(self, name):
        path = os.path.join(self.paths.releases_dir, name)
        os.makedirs(path, exist_ok=True)
        return path

    def test_first_switch_points_previous_at_factory(self):
        staging = self._staging('.1.1.0.staging.abc')
        ok, reason = ui.switch_release(self.paths, staging, '1.1.0')
        self.assertTrue(ok, reason)
        self.assertEqual(
            os.path.realpath(self.paths.current_link),
            os.path.realpath(self.paths.version_dir('1.1.0')))
        self.assertEqual(
            os.path.realpath(self.paths.previous_link),
            os.path.realpath(self.paths.factory_app))

    def test_second_switch_moves_previous_to_first(self):
        first = self._staging('.1.1.0.staging.a')
        ui.switch_release(self.paths, first, '1.1.0')
        second = self._staging('.1.2.0.staging.b')
        ok, reason = ui.switch_release(self.paths, second, '1.2.0')
        self.assertTrue(ok, reason)
        self.assertEqual(
            os.path.realpath(self.paths.previous_link),
            os.path.realpath(self.paths.version_dir('1.1.0')))
        self.assertEqual(
            os.path.realpath(self.paths.current_link),
            os.path.realpath(self.paths.version_dir('1.2.0')))

    def test_existing_target_is_rejected(self):
        os.makedirs(self.paths.version_dir('1.1.0'))
        staging = self._staging('.1.1.0.staging.abc')
        ok, reason = ui.switch_release(self.paths, staging, '1.1.0')
        self.assertFalse(ok)
        self.assertIn('already exists', reason)

    def test_invalid_version_is_rejected(self):
        staging = self._staging('.x.staging')
        ok, reason = ui.switch_release(self.paths, staging, 'not-semver')
        self.assertFalse(ok)
        self.assertIn('invalid', reason)

    def test_active_version_and_current_target(self):
        self.assertEqual(ui._active_version(self.paths), '')
        self.assertEqual(
            os.path.realpath(ui._current_target(self.paths)),
            os.path.realpath(self.paths.factory_app))
        staging = self._staging('.1.1.0.staging.abc')
        ui.switch_release(self.paths, staging, '1.1.0')
        self.assertEqual(ui._active_version(self.paths), '1.1.0')

    def test_rename_failure_leaves_previous_and_current_untouched(self):
        # Seed an active release and a previous release.
        os.makedirs(self.paths.version_dir('1.0.0'))
        os.makedirs(self.paths.version_dir('0.9.0'))
        os.symlink(self.paths.version_dir('1.0.0'), self.paths.current_link)
        os.symlink(self.paths.version_dir('0.9.0'), self.paths.previous_link)
        staging = self._staging('.1.1.0.staging.abc')
        with patch.object(ui.os, 'rename', side_effect=OSError('no rename')):
            ok, reason = ui.switch_release(self.paths, staging, '1.1.0')
        self.assertFalse(ok)
        self.assertIn('activation failed', reason)
        # Both symlinks are exactly as before; the staging directory survives.
        self.assertEqual(
            os.path.realpath(self.paths.current_link),
            os.path.realpath(self.paths.version_dir('1.0.0')))
        self.assertEqual(
            os.path.realpath(self.paths.previous_link),
            os.path.realpath(self.paths.version_dir('0.9.0')))
        self.assertTrue(os.path.isdir(staging))
        self.assertFalse(os.path.exists(self.paths.version_dir('1.1.0')))


class RecoverInterruptedTests(unittest.TestCase):
    """AC-30: interrupted staging/activation recovery must be idempotent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = make_paths(self.tmp.name)

    def _staging(self, name):
        path = os.path.join(self.paths.releases_dir, name)
        os.makedirs(path, exist_ok=True)
        return path

    def test_missing_releases_dir_is_a_noop(self):
        report = ui.recover_interrupted(self.paths)
        self.assertFalse(report.changed)
        self.assertFalse(os.path.exists(self.paths.releases_dir))

    def test_stale_staging_removed(self):
        os.makedirs(self.paths.releases_dir, exist_ok=True)
        stale = self._staging('.1.1.0.staging.abc')
        other = self._staging('.1.1.0.staging.def')
        report = ui.recover_interrupted(self.paths)
        self.assertEqual(
            report.staging_removed,
            ('.1.1.0.staging.abc', '.1.1.0.staging.def'))
        self.assertFalse(os.path.exists(stale))
        self.assertFalse(os.path.exists(other))

    def test_orphan_version_dir_removed_but_referenced_kept(self):
        os.makedirs(self.paths.version_dir('1.0.0'))
        os.makedirs(self.paths.version_dir('1.1.0'))
        os.makedirs(self.paths.version_dir('0.9.0'))
        os.symlink(self.paths.version_dir('1.0.0'), self.paths.current_link)
        os.symlink(self.paths.version_dir('0.9.0'), self.paths.previous_link)
        report = ui.recover_interrupted(self.paths)
        self.assertEqual(report.orphans_removed, ('1.1.0',))
        self.assertTrue(os.path.isdir(self.paths.version_dir('1.0.0')))
        self.assertTrue(os.path.isdir(self.paths.version_dir('0.9.0')))
        self.assertFalse(os.path.exists(self.paths.version_dir('1.1.0')))
        # A hidden marker dir is not a release and must survive.
        os.makedirs(os.path.join(self.paths.releases_dir, '.bad'))
        ui.recover_interrupted(self.paths)
        self.assertTrue(os.path.isdir(os.path.join(self.paths.releases_dir, '.bad')))

    def test_dangling_current_repaired_to_newest_release(self):
        os.makedirs(self.paths.version_dir('1.0.0'))
        os.makedirs(self.paths.version_dir('1.1.0'))
        os.symlink(self.paths.version_dir('1.2.0'), self.paths.current_link)
        report = ui.recover_interrupted(self.paths)
        self.assertTrue(report.current_repaired)
        self.assertEqual(
            report.current_target, self.paths.version_dir('1.1.0'))
        self.assertEqual(
            os.path.realpath(self.paths.current_link),
            os.path.realpath(self.paths.version_dir('1.1.0')))
        # The newest release was kept; the older orphan was pruned.
        self.assertEqual(report.orphans_removed, ('1.0.0',))
        self.assertTrue(os.path.isdir(self.paths.version_dir('1.1.0')))

    def test_dangling_current_falls_back_to_factory(self):
        os.makedirs(self.paths.releases_dir, exist_ok=True)
        os.symlink(self.paths.version_dir('1.2.0'), self.paths.current_link)
        report = ui.recover_interrupted(self.paths)
        self.assertTrue(report.current_repaired)
        self.assertEqual(
            os.path.realpath(self.paths.current_link),
            os.path.realpath(self.paths.factory_app))

    def test_healthy_tree_is_idempotent(self):
        os.makedirs(self.paths.version_dir('1.0.0'))
        os.symlink(self.paths.version_dir('1.0.0'), self.paths.current_link)
        first = ui.recover_interrupted(self.paths)
        self.assertFalse(first.changed)
        second = ui.recover_interrupted(self.paths)
        self.assertFalse(second.changed)
        self.assertEqual(
            os.path.realpath(self.paths.current_link),
            os.path.realpath(self.paths.version_dir('1.0.0')))

    def test_never_raises_when_releases_path_is_a_file(self):
        with open(self.paths.releases_dir, 'w', encoding='utf-8') as handle:
            handle.write('not a directory')
        report = ui.recover_interrupted(self.paths)
        self.assertFalse(report.changed)


# --------------------------------------------------------------------------- #
# install_update happy path
# --------------------------------------------------------------------------- #

class InstallHappyPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = make_paths(self.tmp.name)

    def test_full_order_and_state(self):
        harness = Harness(self.paths)
        result = harness.install(make_manifest(reboot_required=True))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.outcome, ui.INSTALL_INSTALLED)
        self.assertEqual(result.installed_version, '1.1.0')
        self.assertTrue(result.reboot_required)

        steps = [c for c in harness.calls if isinstance(c, str)]
        self.assertEqual(steps[:9], [
            'download', 'verify_manifest', 'verify_bundle', 'extract',
            'build_venv', 'preflight', 'switch', 'health'])
        # prune is called only after a successful health check.
        self.assertIn(('prune', ('1.1.0',)), harness.calls)
        self.assertLess(
            steps.index('health'),
            [i for i, c in enumerate(harness.calls)
             if c == ('prune', ('1.1.0',))][0])
        self.assertFalse(any(
            isinstance(c, tuple) and c[0] == 'record_bad'
            for c in harness.calls))

        self.assertTrue(os.path.isdir(self.paths.version_dir('1.1.0')))
        self.assertEqual(
            os.path.realpath(self.paths.current_link),
            os.path.realpath(self.paths.version_dir('1.1.0')))
        self.assertEqual(
            os.path.realpath(self.paths.previous_link),
            os.path.realpath(self.paths.factory_app))
        # The staging directory was renamed away.
        leftovers = [
            n for n in os.listdir(self.paths.releases_dir)
            if n.startswith('.') and 'staging' in n
        ]
        self.assertEqual(leftovers, [])

    def test_prune_protects_current_and_previous(self):
        # Seed a previous release so the protected set contains both versions.
        old = os.path.join(self.paths.releases_dir, '1.0.0')
        os.makedirs(old)
        os.makedirs(self.paths.releases_dir, exist_ok=True)
        os.symlink(old, self.paths.current_link)
        harness = Harness(self.paths)
        result = harness.install(make_manifest(version='1.1.0'))
        self.assertTrue(result.ok, result.reason)
        prunes = [c for c in harness.calls
                  if isinstance(c, tuple) and c[0] == 'prune']
        self.assertEqual(len(prunes), 1)
        self.assertEqual(set(prunes[0][1]), {'1.1.0', '1.0.0'})


# --------------------------------------------------------------------------- #
# install_update rejection paths
# --------------------------------------------------------------------------- #

class InstallRejectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = make_paths(self.tmp.name)

    def _staging_leftovers(self):
        if not os.path.isdir(self.paths.releases_dir):
            return []
        return [
            n for n in os.listdir(self.paths.releases_dir)
            if n.startswith('.') and 'staging' in n
        ]

    def test_invalid_manifest_no_staging_no_download(self):
        class Broken:
            version = 'not-semver'
            bundle_sha256 = SHA_A
            bundle_size = 1024

        harness = Harness(self.paths)
        result = harness.install(Broken())
        self.assertFalse(result.ok)
        self.assertNotIn('download', harness.calls)
        self.assertEqual(self._staging_leftovers(), [])

    def test_bad_manifest_signature_cleans_staging(self):
        harness = Harness(self.paths, fail_at='manifest_sig')
        result = harness.install(make_manifest())
        self.assertFalse(result.ok)
        self.assertIn('manifest signature', result.reason)
        self.assertEqual(self._staging_leftovers(), [])

    def test_bad_bundle_signature_cleans_staging(self):
        harness = Harness(self.paths, fail_at='bundle_sig')
        result = harness.install(make_manifest())
        self.assertFalse(result.ok)
        self.assertIn('bundle signature', result.reason)
        self.assertEqual(self._staging_leftovers(), [])

    def test_sha256_mismatch_uses_real_extract(self):
        # Real updater.extract_bundle verifies the sha256 before decompression;
        # a mismatch must fail and the staging directory must be removed.
        harness = Harness(self.paths)
        harness.extract = updater.extract_bundle
        result = harness.install(make_manifest(bundle_sha256='0' * 64))
        self.assertFalse(result.ok)
        self.assertIn('sha256', result.reason)
        self.assertEqual(self._staging_leftovers(), [])

    def test_size_mismatch(self):
        harness = Harness(self.paths, download_size=1)
        result = harness.install(make_manifest(bundle_size=1024))
        self.assertFalse(result.ok)
        self.assertIn('size mismatch', result.reason)
        self.assertEqual(self._staging_leftovers(), [])

    def test_downgrade_rejected(self):
        harness = Harness(self.paths)
        result = ui.install_update(
            make_manifest(version='1.1.0'),
            paths=self.paths,
            download=harness.download,
            verify_manifest_signature=harness.verify_manifest_signature,
            verify_bundle_signature=harness.verify_bundle_signature,
            free_space=harness.free_space,
            extract=harness.extract,
            build_venv=harness.build_venv,
            preflight=harness.preflight,
            switch=harness.switch,
            health_check=harness.health_check,
            restart_services=harness.restart_services,
            record_bad=harness.record_bad,
            prune=harness.prune,
            clock=FakeClock(),
            sleeper=FakeClock().sleep,
            current_version='2.0.0',
        )
        self.assertFalse(result.ok)
        self.assertNotIn('download', harness.calls)

    def test_incompatible_image_rejected(self):
        harness = Harness(self.paths)
        result = ui.install_update(
            make_manifest(min_image_version='2.0.0'),
            paths=self.paths,
            download=harness.download,
            verify_manifest_signature=harness.verify_manifest_signature,
            verify_bundle_signature=harness.verify_bundle_signature,
            free_space=harness.free_space,
            extract=harness.extract,
            build_venv=harness.build_venv,
            preflight=harness.preflight,
            switch=harness.switch,
            health_check=harness.health_check,
            restart_services=harness.restart_services,
            record_bad=harness.record_bad,
            prune=harness.prune,
            clock=FakeClock(),
            sleeper=FakeClock().sleep,
            current_image_version='1.0.0',
        )
        self.assertFalse(result.ok)
        self.assertNotIn('download', harness.calls)

    def test_insufficient_space_before_download(self):
        harness = Harness(self.paths, free=1)
        result = harness.install(make_manifest())
        self.assertFalse(result.ok)
        self.assertIn('free space', result.reason)
        self.assertNotIn('download', harness.calls)
        self.assertEqual(self._staging_leftovers(), [])

    def test_headroom_accounts_for_extracted_tree_and_venv(self):
        # Free space that satisfied the old compressed-size-only formula must
        # now be rejected: extraction + venv need more room than the bundle.
        size = 1024
        old_formula = size + ui.FREE_SPACE_HEADROOM_BYTES
        self.assertLess(old_formula, ui.required_free_space(size))
        harness = Harness(self.paths, free=old_formula)
        result = harness.install(make_manifest(bundle_size=size))
        self.assertFalse(result.ok)
        self.assertIn('free space', result.reason)
        self.assertNotIn('download', harness.calls)

    def test_exact_required_space_is_accepted(self):
        size = 1024
        harness = Harness(self.paths, free=ui.required_free_space(size))
        result = harness.install(make_manifest(bundle_size=size))
        self.assertTrue(result.ok, result.reason)
        self.assertIn('download', harness.calls)

    def test_required_free_space_is_documented_and_pure(self):
        self.assertEqual(
            ui.required_free_space(1024),
            1024 * ui.EXTRACTED_EXPANSION_FACTOR
            + ui.VENV_ALLOWANCE_BYTES
            + ui.FREE_SPACE_HEADROOM_BYTES)
        self.assertEqual(ui.required_free_space('x'), 0)
        self.assertEqual(ui.required_free_space(True), 0)

    def _mark_bad(self, version, reason='health checks failed'):
        bad_dir = self.paths.bad_dir()
        os.makedirs(bad_dir, exist_ok=True)
        with open(os.path.join(bad_dir, f'{version}.json'), 'w',
                  encoding='utf-8') as handle:
            json.dump({'version': version, 'reason': reason}, handle)

    def test_bad_release_is_refused_without_force(self):
        self._mark_bad('1.1.0')
        harness = Harness(self.paths)
        result = harness.install(make_manifest(version='1.1.0'))
        self.assertFalse(result.ok)
        self.assertIn('marked bad', result.reason)
        self.assertNotIn('download', harness.calls)
        self.assertEqual(self._staging_leftovers(), [])

    def test_bad_release_installable_with_explicit_force(self):
        self._mark_bad('1.1.0')
        harness = Harness(self.paths)
        result = harness.install(make_manifest(version='1.1.0'),
                                 force_reinstall=True)
        self.assertTrue(result.ok, result.reason)
        self.assertIn('download', harness.calls)

    def test_unrelated_bad_marker_does_not_block(self):
        self._mark_bad('1.0.0')
        harness = Harness(self.paths)
        result = harness.install(make_manifest(version='1.1.0'))
        self.assertTrue(result.ok, result.reason)

    def test_extract_failure_cleans_staging(self):
        harness = Harness(self.paths, fail_at='extract')
        result = harness.install(make_manifest())
        self.assertFalse(result.ok)
        self.assertEqual(self._staging_leftovers(), [])

    def test_venv_and_preflight_failures_clean_staging(self):
        for step in ('venv', 'preflight'):
            with self.subTest(step=step):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                paths = make_paths(tmp.name)
                harness = Harness(paths, fail_at=step)
                result = harness.install(make_manifest())
                self.assertFalse(result.ok)
                leftovers = [
                    n for n in os.listdir(paths.releases_dir)
                    if n.startswith('.') and 'staging' in n
                ]
                self.assertEqual(leftovers, [])

    def test_switch_failure_cleans_staging(self):
        harness = Harness(self.paths, fail_at='switch')
        result = harness.install(make_manifest())
        self.assertFalse(result.ok)
        self.assertEqual(self._staging_leftovers(), [])

    def test_never_raises_with_exploding_callables(self):
        def boom(*args, **kwargs):
            raise RuntimeError('boom')

        result = ui.install_update(
            make_manifest(),
            paths=self.paths,
            download=boom,
            verify_manifest_signature=boom,
            verify_bundle_signature=boom,
            free_space=boom,
            extract=boom,
            build_venv=boom,
            preflight=boom,
            switch=boom,
            health_check=boom,
            restart_services=boom,
            record_bad=boom,
            prune=boom,
            clock=boom,
            sleeper=boom,
        )
        self.assertFalse(result.ok)
        self.assertEqual(self._staging_leftovers(), [])

    def test_secrets_are_redacted_from_reasons(self):
        harness = Harness(self.paths, fail_at='download',
                          secrets=('super-secret-token',))
        harness.download = lambda manifest, staging: (
            False, 'download failed for super-secret-token')
        result = harness.install(make_manifest())
        self.assertFalse(result.ok)
        self.assertNotIn('super-secret-token', result.reason)
        self.assertIn('<redacted>', result.reason)


# --------------------------------------------------------------------------- #
# install_update health / rollback
# --------------------------------------------------------------------------- #

class InstallHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = make_paths(self.tmp.name)

    def _seed_previous(self, version='1.0.0'):
        os.makedirs(self.paths.releases_dir, exist_ok=True)
        previous = os.path.join(self.paths.releases_dir, version)
        os.makedirs(previous, exist_ok=True)
        os.symlink(previous, self.paths.current_link)
        return previous

    def test_health_timeout_restores_previous_and_marks_bad(self):
        previous = self._seed_previous('1.0.0')
        harness = Harness(self.paths, health=False)
        clock = FakeClock(0.0)
        result = harness.install(make_manifest(version='1.1.0'), clock=clock,
                                 sleeper=clock.sleep)
        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, ui.INSTALL_ROLLED_BACK)
        self.assertEqual(result.installed_version, '1.0.0')
        self.assertEqual(result.attempted_version, '1.1.0')
        self.assertEqual(
            os.path.realpath(self.paths.current_link),
            os.path.realpath(previous))
        bad = [c for c in harness.calls
               if isinstance(c, tuple) and c[0] == 'record_bad']
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0][1], '1.1.0')
        # No prune after a failed health check.
        self.assertFalse(any(
            isinstance(c, tuple) and c[0] == 'prune' for c in harness.calls))
        # Health polling respected the 90 s budget.
        self.assertGreaterEqual(clock.now, ui.HEALTH_TIMEOUT_SECONDS)
        # The attempted version directory is left inactive (marked bad).
        self.assertTrue(os.path.isdir(self.paths.version_dir('1.1.0')))

    def test_factory_fallback_when_no_previous_release(self):
        harness = Harness(self.paths, health=False)
        clock = FakeClock(0.0)
        result = harness.install(make_manifest(version='1.1.0'), clock=clock,
                                 sleeper=clock.sleep)
        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, ui.INSTALL_ROLLED_BACK)
        self.assertEqual(result.installed_version, '')
        self.assertEqual(
            os.path.realpath(self.paths.current_link),
            os.path.realpath(self.paths.factory_app))

    def test_restart_failure_rolls_back(self):
        self._seed_previous('1.0.0')
        harness = Harness(self.paths, fail_at='restart')
        result = harness.install(make_manifest(version='1.1.0'))
        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, ui.INSTALL_ROLLED_BACK)
        self.assertEqual(result.installed_version, '1.0.0')

    def test_health_succeeds_after_retries(self):
        # First two probes fail, third succeeds: still installs.
        state = {'n': 0}

        class Flaky(Harness):
            def health_check(self, version):
                self.calls.append('health')
                state['n'] += 1
                return state['n'] >= 3

        harness = Flaky(self.paths)
        clock = FakeClock(0.0)
        result = harness.install(make_manifest(version='1.1.0'), clock=clock,
                                 sleeper=clock.sleep)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.installed_version, '1.1.0')


# --------------------------------------------------------------------------- #
# check_for_update
# --------------------------------------------------------------------------- #

class CheckForUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.last = os.path.join(self.tmp.name, 'last-check.json')

    def _run(self, *, fetch, verify=None, clock=None, rng=None, force=False,
             current_version='', current_image_version='', bad_versions=(),
             force_reinstall=False):
        return ui.check_for_update(
            current_version=current_version,
            current_image_version=current_image_version,
            fetch_manifest=fetch,
            verify=verify,
            clock=clock,
            rng=rng,
            last_check_path=self.last,
            force=force,
            bad_versions=bad_versions,
            force_reinstall=force_reinstall,
        )

    def test_not_due_skips_fetch(self):
        with open(self.last, 'w', encoding='utf-8') as handle:
            json.dump({'last_check': 1000.0}, handle)
        calls = []
        result = self._run(
            fetch=lambda: (calls.append('fetch'), (True, valid_manifest()))[1],
            clock=lambda: 1000.0 + 3600.0,
        )
        self.assertFalse(result.checked)
        self.assertEqual(result.outcome, ui.CHECK_UP_TO_DATE)
        self.assertEqual(calls, [])

    def test_due_fetches_verifies_and_reports_available(self):
        fetch_calls = []
        verify_calls = []
        result = self._run(
            fetch=lambda: (fetch_calls.append(1), (True, valid_manifest()))[1],
            verify=lambda payload: (verify_calls.append(1), (True, ''))[1],
            clock=lambda: 10000.0,
        )
        self.assertTrue(result.checked)
        self.assertEqual(result.outcome, ui.CHECK_AVAILABLE)
        self.assertIsNotNone(result.manifest)
        self.assertEqual(result.manifest.version, '1.1.0')
        self.assertEqual(fetch_calls, [1])
        self.assertEqual(verify_calls, [1])
        # The last-check timestamp was persisted.
        self.assertTrue(os.path.isfile(self.last))

    def test_force_bypasses_interval(self):
        with open(self.last, 'w', encoding='utf-8') as handle:
            json.dump({'last_check': 1000.0}, handle)
        result = self._run(
            fetch=lambda: (True, valid_manifest()),
            verify=lambda payload: (True, ''),
            clock=lambda: 1001.0,
            force=True,
        )
        self.assertTrue(result.checked)
        self.assertEqual(result.outcome, ui.CHECK_AVAILABLE)

    def test_fetch_failure_is_error(self):
        result = self._run(
            fetch=lambda: (False, 'network down'),
            verify=lambda payload: (True, ''),
            clock=lambda: 0.0,
        )
        self.assertTrue(result.checked)
        self.assertEqual(result.outcome, ui.CHECK_ERROR)
        self.assertIn('network down', result.reason)

    def test_verify_failure_is_invalid(self):
        result = self._run(
            fetch=lambda: (True, valid_manifest()),
            verify=lambda payload: (False, 'bad signature'),
            clock=lambda: 0.0,
        )
        self.assertEqual(result.outcome, ui.CHECK_INVALID)
        self.assertIn('bad signature', result.reason)

    def test_parse_failure_is_invalid(self):
        result = self._run(
            fetch=lambda: (True, {'schema_version': 99}),
            verify=lambda payload: (True, ''),
            clock=lambda: 0.0,
        )
        self.assertEqual(result.outcome, ui.CHECK_INVALID)

    def test_up_to_date_classification(self):
        result = self._run(
            fetch=lambda: (True, valid_manifest(version='1.1.0')),
            verify=lambda payload: (True, ''),
            clock=lambda: 0.0,
            current_version='1.1.0',
        )
        self.assertEqual(result.outcome, ui.CHECK_UP_TO_DATE)
        self.assertIsNotNone(result.manifest)

    def test_jitter_bounds(self):
        class Rng:
            def __init__(self, value):
                self.value = value

            def random(self):
                return self.value

        high = self._run(
            fetch=lambda: (True, valid_manifest()),
            verify=lambda payload: (True, ''),
            clock=lambda: 0.0,
            rng=Rng(1.0),
        )
        low = self._run(
            fetch=lambda: (True, valid_manifest()),
            verify=lambda payload: (True, ''),
            clock=lambda: 0.0,
            rng=Rng(0.0),
        )
        self.assertAlmostEqual(high.next_check_epoch,
                               ui.CHECK_INTERVAL_SECONDS + ui.CHECK_JITTER_SECONDS)
        self.assertAlmostEqual(low.next_check_epoch,
                               ui.CHECK_INTERVAL_SECONDS - ui.CHECK_JITTER_SECONDS)

    def test_never_raises_with_exploding_callables(self):
        def boom(*args, **kwargs):
            raise RuntimeError('boom')

        result = ui.check_for_update(
            current_version='',
            current_image_version='',
            fetch_manifest=boom,
            verify=boom,
            clock=boom,
            rng=boom,
            last_check_path=self.last,
        )
        self.assertEqual(result.outcome, ui.CHECK_ERROR)

    def test_report_only_creates_no_release(self):
        result = self._run(
            fetch=lambda: (True, valid_manifest()),
            verify=lambda payload: (True, ''),
            clock=lambda: 0.0,
        )
        self.assertTrue(result.checked)
        # No release directory or staging directory is created by a check.
        releases = os.path.join(self.tmp.name, 'releases')
        self.assertFalse(os.path.exists(releases))

    def test_bad_version_is_not_offered(self):
        result = self._run(
            fetch=lambda: (True, valid_manifest(version='1.1.0')),
            verify=lambda payload: (True, ''),
            clock=lambda: 0.0,
            bad_versions=('1.1.0',),
        )
        self.assertEqual(result.outcome, ui.CHECK_SUPPRESSED)
        self.assertFalse(result.available)
        self.assertIn('marked bad', result.reason)
        # The last-check timestamp is still persisted.
        self.assertTrue(os.path.isfile(self.last))

    def test_bad_version_offered_when_force_reinstall(self):
        result = self._run(
            fetch=lambda: (True, valid_manifest(version='1.1.0')),
            verify=lambda payload: (True, ''),
            clock=lambda: 0.0,
            bad_versions=('1.1.0',),
            force_reinstall=True,
        )
        self.assertEqual(result.outcome, ui.CHECK_AVAILABLE)

    def test_unrelated_bad_version_does_not_suppress(self):
        result = self._run(
            fetch=lambda: (True, valid_manifest(version='1.1.0')),
            verify=lambda payload: (True, ''),
            clock=lambda: 0.0,
            bad_versions=('1.0.0',),
        )
        self.assertEqual(result.outcome, ui.CHECK_AVAILABLE)


# --------------------------------------------------------------------------- #
# update_state_document
# --------------------------------------------------------------------------- #

class UpdateStateDocumentTests(unittest.TestCase):
    def test_schema_and_values(self):
        doc = ui.update_state_document(
            installed_version='1.0.0',
            latest_version='1.1.0',
            release_summary='Fixes a camera bug.',
            release_url='https://example.com/releases/1.1.0',
            in_progress=True,
            update_percentage=42,
        )
        self.assertEqual(set(doc), {
            'installed_version', 'latest_version', 'release_summary',
            'release_url', 'in_progress', 'update_percentage'})
        self.assertEqual(doc['installed_version'], '1.0.0')
        self.assertEqual(doc['latest_version'], '1.1.0')
        self.assertEqual(doc['release_summary'], 'Fixes a camera bug.')
        self.assertEqual(doc['release_url'], 'https://example.com/releases/1.1.0')
        self.assertTrue(doc['in_progress'])
        self.assertEqual(doc['update_percentage'], 42)

    def test_not_in_progress_percentage_is_none(self):
        doc = ui.update_state_document(
            installed_version='1.0.0', latest_version='1.1.0',
            release_summary='', release_url='', in_progress=False,
            update_percentage=50)
        self.assertFalse(doc['in_progress'])
        self.assertIsNone(doc['update_percentage'])

    def test_percentage_is_bounded(self):
        for value, expected in ((150, 100), (-5, 0), ('x', None),
                                (None, None), (True, None), (12.5, 12.5)):
            with self.subTest(value=value):
                doc = ui.update_state_document(
                    installed_version='1.0.0', latest_version='1.1.0',
                    release_summary='', release_url='', in_progress=True,
                    update_percentage=value)
                self.assertEqual(doc['update_percentage'], expected)

    def test_text_fields_are_bounded_and_control_free(self):
        doc = ui.update_state_document(
            installed_version='1.0.0\ninjected',
            latest_version='x' * (updater.MAX_VERSION_LENGTH + 50),
            release_summary='a\x00b',
            release_url='https://example.com/' + 'y' * (updater.MAX_URL_LENGTH + 50),
            in_progress=False, update_percentage=None)
        for key in ('installed_version', 'latest_version', 'release_summary',
                    'release_url'):
            self.assertNotIn('\n', doc[key])
            self.assertNotIn('\x00', doc[key])
        self.assertLessEqual(len(doc['latest_version']),
                             updater.MAX_VERSION_LENGTH)
        self.assertLessEqual(len(doc['release_url']), updater.MAX_URL_LENGTH)


# --------------------------------------------------------------------------- #
# read_update_state / write_update_state (WP-R4c)
# --------------------------------------------------------------------------- #

class UpdateStateFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'update-state.json')

    def test_default_path_is_under_durable_data(self):
        self.assertEqual(ui.DEFAULT_UPDATE_STATE_PATH,
                         '/data/prusa-cam/update-state.json')

    def test_round_trip(self):
        document = ui.update_state_document(
            installed_version='1.0.0', latest_version='1.1.0',
            release_summary='Fixes a camera bug.',
            release_url='https://example.com/releases/1.1.0',
            in_progress=False, update_percentage=None)
        self.assertTrue(ui.write_update_state(document, path=self.path))
        self.assertEqual(ui.read_update_state(self.path), document)

    def test_write_is_mode_0644_so_app_can_read(self):
        self.assertTrue(ui.write_update_state(
            ui.update_state_document(
                installed_version='1.0.0', latest_version='',
                release_summary='', release_url='', in_progress=False,
                update_percentage=None),
            path=self.path))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o644)

    def test_read_missing_or_empty_path_is_none(self):
        self.assertIsNone(ui.read_update_state(self.path))
        self.assertIsNone(ui.read_update_state(''))
        self.assertIsNone(ui.read_update_state(None))

    def test_read_garbage_json_is_none(self):
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write('{not json')
        self.assertIsNone(ui.read_update_state(self.path))

    def test_read_non_object_json_is_none(self):
        for payload in ('[1, 2, 3]', '"version"', '42', 'null'):
            with open(self.path, 'w', encoding='utf-8') as handle:
                handle.write(payload)
            self.assertIsNone(ui.read_update_state(self.path), payload)

    def test_read_oversized_file_is_none(self):
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write('{"installed_version": "' + 'x' * (
                ui.MAX_UPDATE_STATE_BYTES + 1) + '"}')
        self.assertIsNone(ui.read_update_state(self.path))

    def test_read_projects_unknown_keys_and_normalizes(self):
        with open(self.path, 'w', encoding='utf-8') as handle:
            json.dump({
                'installed_version': '1.0.0',
                'latest_version': '1.1.0',
                'in_progress': True,
                'update_percentage': 150,
                'secret_token': 'must-not-survive',
            }, handle)
        document = ui.read_update_state(self.path)
        self.assertEqual(set(document), set(ui.UPDATE_STATE_KEYS))
        self.assertNotIn('secret_token', document)
        self.assertTrue(document['in_progress'])
        self.assertEqual(document['update_percentage'], 100)

    def test_write_rejects_non_dict_and_keeps_existing(self):
        self.assertTrue(ui.write_update_state(
            ui.update_state_document(
                installed_version='1.0.0', latest_version='',
                release_summary='', release_url='', in_progress=False,
                update_percentage=None),
            path=self.path))
        before = ui.read_update_state(self.path)
        self.assertFalse(ui.write_update_state(['not', 'a', 'dict'], path=self.path))
        self.assertFalse(ui.write_update_state(None, path=self.path))
        self.assertEqual(ui.read_update_state(self.path), before)

    def test_write_rejects_oversized_document(self):
        self.assertFalse(ui.write_update_state(
            {'installed_version': 'x' * (ui.MAX_UPDATE_STATE_BYTES + 1)},
            path=self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_write_leaves_no_temp_files(self):
        self.assertTrue(ui.write_update_state(
            ui.update_state_document(
                installed_version='1.0.0', latest_version='',
                release_summary='', release_url='', in_progress=False,
                update_percentage=None),
            path=self.path))
        leftovers = [
            name for name in os.listdir(self.tmp.name)
            if name != os.path.basename(self.path)
        ]
        self.assertEqual(leftovers, [], 'write must clean up its temp file')

    def test_write_failure_never_raises(self):
        directory = os.path.join(self.tmp.name, 'file-not-dir')
        with open(directory, 'w', encoding='utf-8') as handle:
            handle.write('x')
        self.assertFalse(ui.write_update_state(
            {'installed_version': '1.0.0'},
            path=os.path.join(directory, 'nested', 'update-state.json')))



# --------------------------------------------------------------------------- #
# default_prune (real filesystem, temp dirs)
# --------------------------------------------------------------------------- #

class DefaultPruneTests(unittest.TestCase):
    def test_never_removes_current_previous_or_factory(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = make_paths(tmp.name)
        for version in ('1.0.0', '1.1.0', '1.2.0'):
            os.makedirs(paths.version_dir(version))
        os.symlink(paths.version_dir('1.2.0'), paths.current_link)
        os.symlink(paths.version_dir('1.1.0'), paths.previous_link)
        os.makedirs(os.path.join(paths.releases_dir, '.hidden'))

        removed = ui.default_prune(paths, ('1.2.0',))
        self.assertEqual(removed, ['1.0.0'])
        self.assertTrue(os.path.isdir(paths.version_dir('1.1.0')))
        self.assertTrue(os.path.isdir(paths.version_dir('1.2.0')))
        self.assertTrue(os.path.isdir(paths.factory_app))


# --------------------------------------------------------------------------- #
# default_download + updater.verify_file composition (finding: detached sig)
# --------------------------------------------------------------------------- #

class _FakeHTTPResponse:
    """Minimal context-manager stand-in for ``urllib`` responses."""

    def __init__(self, data, url):
        self._data = data
        self._url = url
        self._offset = 0

    def read(self, size=-1):
        if size is None or size < 0:
            chunk = self._data[self._offset:]
        else:
            chunk = self._data[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DefaultDownloadCompositionTests(unittest.TestCase):
    """Real ``default_download`` + real ``updater.verify_file`` composition."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.staging = os.path.join(self.tmp.name, 'staging')
        os.makedirs(self.staging)
        self.pubkey = os.path.join(self.tmp.name, 'buddy3d-release.pub')
        with open(self.pubkey, 'w', encoding='utf-8') as handle:
            handle.write(
                'untrusted comment: minisign public key SYNTHETIC\n'
                'RWTjuP4R4QtoSN543KA74kYYGJ7WkKAz2j5el+ZZC310+TJvrxVAia02\n')

    def test_bundle_and_signature_downloaded_then_verified(self):
        bundle_bytes = b'bundle-bytes'
        sig_bytes = b'untrusted comment: signature\nRWT...\n'
        url = 'https://example.com/buddy3d-camera-app-1.1.0.tar.zst'
        manifest = make_manifest(bundle_size=len(bundle_bytes), bundle_url=url)
        requested = []

        def fake_urlopen(request, timeout=None):
            requested.append(request)
            if request == url:
                return _FakeHTTPResponse(bundle_bytes, url)
            if request == url + ui.BUNDLE_SIGNATURE_SUFFIX:
                return _FakeHTTPResponse(sig_bytes, url + ui.BUNDLE_SIGNATURE_SUFFIX)
            raise AssertionError(f'unexpected url {request}')

        with patch.object(ui.urllib.request, 'urlopen', side_effect=fake_urlopen):
            ok, bundle_path = ui.default_download(manifest, self.staging)
        self.assertTrue(ok, bundle_path)
        self.assertEqual(requested, [url, url + ui.BUNDLE_SIGNATURE_SUFFIX])

        expected_sig = bundle_path + ui.BUNDLE_SIGNATURE_SUFFIX
        self.assertTrue(os.path.isfile(bundle_path))
        self.assertTrue(os.path.isfile(expected_sig))
        with open(expected_sig, 'rb') as handle:
            self.assertEqual(handle.read(), sig_bytes)

        seen = {}

        def fake_runner(args, timeout):
            # minisign -V -m <bundle> -p <pub> -x <sig>; succeeds only when the
            # detached signature exists at the default path.
            message = args[args.index('-m') + 1]
            signature = args[args.index('-x') + 1]
            seen['message'] = message
            seen['signature'] = signature
            exists = os.path.isfile(signature)
            seen['exists'] = exists
            return subprocess.CompletedProcess(
                args, 0 if exists else 1, '', '')

        ok, reason = updater.verify_file(
            bundle_path, public_key_path=self.pubkey, runner=fake_runner)
        self.assertTrue(ok, reason)
        self.assertEqual(seen['message'], bundle_path)
        self.assertEqual(seen['signature'], expected_sig)
        self.assertTrue(seen['exists'])

        # Removing the detached signature makes the same composition fail: the
        # runner (and thus verify_file) depends on the downloaded ``.minisig``.
        os.remove(expected_sig)
        ok, reason = updater.verify_file(
            bundle_path, public_key_path=self.pubkey, runner=fake_runner)
        self.assertFalse(ok)
        self.assertIn('signature', reason)

    def test_signature_download_failure_cleans_both_files(self):
        url = 'https://example.com/bundle.tar.zst'
        manifest = make_manifest(bundle_size=8, bundle_url=url)

        def fake_urlopen(request, timeout=None):
            if request == url:
                return _FakeHTTPResponse(b'12345678', url)
            raise OSError('signature fetch failed')

        with patch.object(ui.urllib.request, 'urlopen', side_effect=fake_urlopen):
            ok, reason = ui.default_download(manifest, self.staging)
        self.assertFalse(ok)
        self.assertIn('download failed', reason)
        self.assertFalse(os.path.exists(os.path.join(self.staging, ui.BUNDLE_FILENAME)))
        self.assertFalse(os.path.exists(
            os.path.join(self.staging, ui.BUNDLE_FILENAME + ui.BUNDLE_SIGNATURE_SUFFIX)))

    def test_non_https_bundle_url_is_rejected(self):
        url = 'http://example.com/bundle.tar.zst'
        # parse_manifest already rejects http, so build the object directly to
        # prove default_download also fails closed on a non-https URL.
        manifest = updater.Manifest(
            schema_version=1, version='1.2.3', channel='stable',
            source_commit='0' * 40, min_image_version='1.0.0',
            bundle_url=url, bundle_sha256='a' * 64, bundle_size=8,
            release_summary='release', release_url='https://example.com/r',
            reboot_required=False)

        def fake_urlopen(request, timeout=None):
            return _FakeHTTPResponse(b'12345678', url)

        with patch.object(ui.urllib.request, 'urlopen', side_effect=fake_urlopen):
            ok, reason = ui.default_download(manifest, self.staging)
        self.assertFalse(ok)
        self.assertIn('download failed', reason)


# --------------------------------------------------------------------------- #
# Units, installer, and import safety
# --------------------------------------------------------------------------- #

def parse_unit(path):
    sections = {}
    current = None
    for raw in Path(path).read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or line.startswith(';'):
            continue
        if line.startswith('[') and line.endswith(']'):
            current = line[1:-1]
            sections.setdefault(current, {})
            continue
        if '=' in line and current is not None:
            key, value = line.split('=', 1)
            key, value = key.strip(), value.strip()
            existing = sections[current].get(key)
            sections[current][key] = (
                value if existing is None else f'{existing} {value}')
    return sections


class UpdaterUnitTests(unittest.TestCase):
    def test_service_is_root_oneshot_gated_on_data_ready(self):
        unit = parse_unit(PI_DIR / 'systemd' / 'prusa-updater.service')
        section = unit['Unit']
        self.assertIn('data-ready.target', section['Requires'].split())
        self.assertIn('data-ready.target', section['After'].split())
        service = unit['Service']
        self.assertEqual(service['Type'], 'oneshot')
        self.assertEqual(service['User'], 'root')
        self.assertIn('updater_install.py', service['ExecStart'])
        self.assertIn(' check', service['ExecStart'])
        # A documented fixed PATH for the minisign/zstd binaries.
        self.assertIn('PATH=', service['Environment'])
        # The oneshot service is triggered by the timer, not enabled directly.
        self.assertNotIn('Install', unit)

    def test_service_recovers_before_check(self):
        unit = parse_unit(PI_DIR / 'systemd' / 'prusa-updater.service')
        service = unit['Service']
        self.assertIn('updater_install.py recover', service['ExecStartPre'])
        self.assertIn(' check', service['ExecStart'])

    def test_service_config_is_root_owned_not_service_writable(self):
        text = (PI_DIR / 'systemd' / 'prusa-updater.service').read_text(
            encoding='utf-8')
        service = parse_unit(PI_DIR / 'systemd' / 'prusa-updater.service')['Service']
        # AC-32: the manifest URL comes from the root-owned file, never from
        # /etc/prusa-cam (which the unprivileged service account can write).
        self.assertEqual(service.get('EnvironmentFile'), '-/etc/prusa-updater.conf')
        self.assertNotIn('/etc/prusa-cam/', text)
        # The trust anchor cannot be overridden from the environment.
        self.assertNotIn('PRUSA_UPDATE_PUBLIC_KEY', text)
        # The module itself must never read a public-key env override.
        source = (PI_DIR / 'updater_install.py').read_text(encoding='utf-8')
        self.assertNotIn('PRUSA_UPDATE_PUBLIC_KEY', source)
        self.assertNotIn('PUBLIC_KEY_ENV', source)

    def test_timer_triggers_service_and_is_enabled(self):
        unit = parse_unit(PI_DIR / 'systemd' / 'prusa-updater.timer')
        self.assertEqual(unit['Timer']['Unit'], 'prusa-updater.service')
        self.assertIn('24h', unit['Timer']['OnUnitActiveSec'])
        self.assertIn('multi-user.target', unit['Install']['WantedBy'].split())

    def test_camera_target_wants_timer_but_never_requires_it(self):
        unit = parse_unit(
            REPO_ROOT / 'image' / 'assets' / 'systemd' / 'prusa-camera.target')
        section = unit['Unit']
        self.assertIn('prusa-updater.timer', section['Wants'].split())
        self.assertNotIn('prusa-updater.timer', section.get('Requires', '').split())


class InstallerWiringTests(unittest.TestCase):
    def setUp(self):
        self.text = (
            REPO_ROOT / 'image' / 'assets' / 'install-factory-app.sh'
        ).read_text(encoding='utf-8')

    def test_embeds_public_key_root_owned_0644(self):
        self.assertIn('image/keys/buddy3d-release.pub', self.text)
        self.assertIn('/usr/share/prusa-buddy3d-camera/buddy3d-release.pub',
                      self.text)
        self.assertIn('-o root -g root -m 0644', self.text)

    def test_installs_and_enables_updater_units(self):
        self.assertIn('prusa-updater.service prusa-updater.timer', self.text)
        enable_block = self.text.split('systemctl enable', 1)[1].split('|| true', 1)[0]
        self.assertIn('prusa-updater.timer', enable_block)
        self.assertNotIn('prusa-updater.service', enable_block)

    def test_layer_installs_minisign_and_zstd(self):
        layer = (
            REPO_ROOT / 'image' / 'layer' / 'buddy3d-image.yaml'
        ).read_text(encoding='utf-8')
        self.assertIsNotNone(re.search(r'^\s*- minisign\s*$', layer, re.MULTILINE))
        self.assertIsNotNone(re.search(r'^\s*- zstd\s*$', layer, re.MULTILINE))

    def test_installs_root_owned_updater_config(self):
        # AC-32: the only configurable value lives in a root-owned file, never
        # in the service-writable /etc/prusa-cam tree.
        self.assertIn('/etc/prusa-updater.conf', self.text)
        self.assertIn('PRUSA_UPDATE_MANIFEST_URL', self.text)
        self.assertIn('chown root:root', self.text)
        self.assertIn('chmod 0644', self.text)
        self.assertNotIn('/etc/prusa-cam/updater.env', self.text)


class CliTests(unittest.TestCase):
    """The scheduled ``check`` must be inert, not a usage error, when unset."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_check_without_manifest_url_exits_zero(self):
        with patch.dict(os.environ, {}, clear=True):
            code = ui.main(['check', '--data-root', self.tmp.name])
        self.assertEqual(code, 0)

    def test_check_without_manifest_url_writes_state(self):
        with patch.dict(os.environ, {}, clear=True):
            code = ui.main(['check', '--data-root', self.tmp.name])
        self.assertEqual(code, 0)
        document = ui.read_update_state(
            os.path.join(self.tmp.name, 'update-state.json'))
        self.assertIsNotNone(document)
        self.assertEqual(set(document), set(ui.UPDATE_STATE_KEYS))
        self.assertFalse(document['in_progress'])
        self.assertTrue(document['installed_version'])

    def test_install_missing_manifest_writes_state(self):
        code = ui.main([
            'install', os.path.join(self.tmp.name, 'missing.json'),
            '--data-root', self.tmp.name,
        ])
        self.assertEqual(code, 2)
        document = ui.read_update_state(
            os.path.join(self.tmp.name, 'update-state.json'))
        self.assertIsNotNone(document)
        self.assertIn('installed_version', document)

    def test_install_without_manifest_or_url_is_a_usage_failure(self):
        with patch.dict(os.environ, {}, clear=True):
            code = ui.main(['install'])
        self.assertEqual(code, 2)

    def test_recover_subcommand_is_idempotent(self):
        code = ui.main(['recover', '--data-root', self.tmp.name])
        self.assertEqual(code, 0)
        code = ui.main(['recover', '--data-root', self.tmp.name])
        self.assertEqual(code, 0)

    def test_install_parser_accepts_force_reinstall(self):
        # A recognized flag reaches _cli_install (manifest missing => exit 2);
        # an unknown flag would make argparse exit 2 by raising SystemExit.
        code = ui.main(
            ['install', '--force-reinstall',
             os.path.join(self.tmp.name, 'missing.json')])
        self.assertEqual(code, 2)


class ValidatorAssertionsTests(unittest.TestCase):
    """The offline validator must assert the key and updater units."""

    def setUp(self):
        self.text = (
            REPO_ROOT / 'image' / 'scripts' / 'validate-image.sh'
        ).read_text(encoding='utf-8')

    def test_validator_asserts_key_and_units(self):
        self.assertIn('buddy3d-release.pub', self.text)
        self.assertIn('prusa-updater.service', self.text)
        self.assertIn('prusa-updater.timer', self.text)
        self.assertIn('is enabled at multi-user.target', self.text)
        # AC-32: no service-writable EnvironmentFile, no public-key env override.
        self.assertIn('no service-writable EnvironmentFile', self.text)
        self.assertIn('no public-key env override', self.text)
        self.assertIn('updater_install.py recover', self.text)
        # WP-R4c: the install oneshot runs the install and is never enabled.
        self.assertIn('prusa-updater-install.service', self.text)
        self.assertIn('updater_install.py install', self.text)
        self.assertIn(
            'prusa-updater-install.service is not enabled', self.text)


class ImportSafetyTests(unittest.TestCase):
    def test_import_runs_no_subprocess_or_network(self):
        with patch.object(subprocess, 'run',
                          side_effect=AssertionError('subprocess on import')), \
             patch.object(urllib.request, 'urlopen',
                          side_effect=AssertionError('network on import')):
            importlib.reload(ui)
        self.assertTrue(callable(ui.install_update))
        self.assertTrue(callable(ui.check_for_update))
        self.assertTrue(callable(ui.update_state_document))

    def test_source_is_stdlib_plus_local(self):
        source = (PI_DIR / 'updater_install.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split('.')[0])
        allowed_stdlib = {
            'argparse', 'dataclasses', 'json', 'logging', 'os', 'random', 're',
            'shutil', 'socket', 'subprocess', 'sys', 'tempfile', 'time',
            'urllib',
        }
        self.assertTrue(imported <= allowed_stdlib | {'app_version', 'updater'},
                        f'unexpected imports: {imported}')


if __name__ == '__main__':
    unittest.main()
