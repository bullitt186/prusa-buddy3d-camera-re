"""WP-R4a (AC-29/AC-30/AC-32): signed update core (updater).

Stdlib-only and fully hermetic: no network, no real ``minisign``/``zstd``
binary, no ``tar`` subprocess. Every external command uses an injected fake
runner; archive safety is exercised with in-memory :mod:`tarfile` members.
"""
import hashlib
import importlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import updater  # noqa: E402

PUBKEY_BODY = (
    'untrusted comment: minisign public key 48680BE111FEB8E3\n'
    'RWTjuP4R4QtoSN543KA74kYYGJ7WkKAz2j5el+ZZC310+TJvrxVAia02\n'
)
SHA_A = 'a' * 64


class FakeResult:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_runner(returncode=0, raises=None):
    """A fake runner returning a fixed result (or raising); records calls."""
    calls = []

    def runner(args, timeout):
        calls.append((list(args), timeout))
        if raises is not None:
            raise raises
        return FakeResult(returncode)

    runner.calls = calls
    return runner


def valid_manifest(**overrides):
    doc = {
        'schema_version': 1,
        'version': '1.2.3',
        'channel': 'stable',
        'source_commit': 'deadbeef' * 5,
        'min_image_version': '1.0.0',
        'bundle_url': 'https://example.com/buddy3d-camera-app-1.2.3.tar.zst',
        'bundle_sha256': SHA_A,
        'bundle_size': 1024,
        'release_summary': 'Fixes a camera bug.',
        'release_url': 'https://example.com/releases/1.2.3',
        'reboot_required': False,
    }
    doc.update(overrides)
    return doc


def tar_info(name, *, kind=tarfile.REGTYPE, size=0, mode=0o644, uid=0, gid=0,
             linkname=''):
    """Build a TarInfo-like member with a normalized (root) owner."""
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = size
    info.mode = mode
    info.uid = uid
    info.gid = gid
    info.linkname = linkname
    return info


def make_tar(entries):
    """Build uncompressed tar bytes from ``(TarInfo, data)`` entries."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for info, data in entries:
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def fake_zstd_runner(calls=None, *, returncode=0, create=True):
    """A fake zstd runner that copies the input to the ``-o`` output path."""
    def runner(args, timeout):
        args = list(args)
        if calls is not None:
            calls.append((args, timeout))
        if returncode != 0:
            return FakeResult(returncode)
        if create:
            out_index = args.index('-o')
            source, output = args[-1], args[out_index + 1]
            with open(source, 'rb') as handle:
                data = handle.read()
            with open(output, 'wb') as handle:
                handle.write(data)
        return FakeResult(0)

    return runner


# --------------------------------------------------------------------------- #
# SemVer
# --------------------------------------------------------------------------- #

class SemVerTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(updater.parse_semver('0.0.0'), (0, 0, 0))
        self.assertEqual(updater.parse_semver('1.2.3'), (1, 2, 3))
        self.assertEqual(updater.parse_semver('10.20.30'), (10, 20, 30))

    def test_invalid(self):
        for text in ('v1.2.3', '1.2', '1.2.3.4', '1.2.x', '01.2.3', '1.02.3',
                     '1.2.03', '-1.2.3', '1.2.3 ', ' 1.2.3', '1.2.3-rc1',
                     '1.2.3+build', '', 'abc', None, 123, True):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    updater.parse_semver(text)

    def test_too_long_is_rejected(self):
        with self.assertRaises(ValueError):
            updater.parse_semver('1.' + '2' * updater.MAX_VERSION_LENGTH + '.3')


class IsNewerTests(unittest.TestCase):
    def test_comparisons(self):
        self.assertTrue(updater.is_newer('1.2.4', '1.2.3'))
        self.assertTrue(updater.is_newer('2.0.0', '1.9.9'))
        self.assertFalse(updater.is_newer('1.2.3', '1.2.3'))
        self.assertFalse(updater.is_newer('1.2.2', '1.2.3'))

    def test_invalid_never_raises(self):
        for candidate, current in (('bad', '1.0.0'), ('1.0.0', 'bad'),
                                   (None, '1.0.0'), ('1.0.0', None)):
            with self.subTest(candidate=candidate, current=current):
                self.assertFalse(updater.is_newer(candidate, current))


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #

class ManifestHappyPathTests(unittest.TestCase):
    def test_dict_payload(self):
        manifest = updater.parse_manifest(valid_manifest())
        self.assertIsInstance(manifest, updater.Manifest)
        self.assertEqual(manifest.schema_version, 1)
        self.assertEqual(manifest.version, '1.2.3')
        self.assertEqual(manifest.channel, 'stable')
        self.assertEqual(manifest.bundle_sha256, SHA_A)
        self.assertFalse(manifest.reboot_required)

    def test_json_str_and_bytes(self):
        payload = json.dumps(valid_manifest(reboot_required=True))
        for candidate in (payload, payload.encode('utf-8')):
            with self.subTest(kind=type(candidate).__name__):
                manifest = updater.parse_manifest(candidate)
                self.assertTrue(manifest.reboot_required)

    def test_sha256_is_normalized_to_lowercase(self):
        manifest = updater.parse_manifest(valid_manifest(bundle_sha256='A' * 64))
        self.assertEqual(manifest.bundle_sha256, 'a' * 64)

    def test_unknown_extra_fields_are_allowed(self):
        manifest = updater.parse_manifest(valid_manifest(extra='metadata'))
        self.assertEqual(manifest.version, '1.2.3')

    def test_empty_current_versions_skip_comparisons(self):
        manifest = updater.parse_manifest(valid_manifest(version='0.0.1'))
        self.assertEqual(manifest.version, '0.0.1')

    def test_alpha_channel_accepted(self):
        manifest = updater.parse_manifest(valid_manifest(channel='alpha'))
        self.assertEqual(manifest.channel, 'alpha')


class ManifestRejectionTests(unittest.TestCase):
    def assertRejected(self, doc, *, current_version='', current_image_version=''):
        with self.assertRaises(updater.ManifestError):
            updater.parse_manifest(
                doc, current_version=current_version,
                current_image_version=current_image_version)

    def test_manifest_error_is_value_error(self):
        self.assertTrue(issubclass(updater.ManifestError, ValueError))

    def test_invalid_current_version_is_rejected(self):
        self.assertRejected(valid_manifest(version='1.2.4'),
                            current_version='not-semver')

    def test_invalid_current_image_version_is_rejected(self):
        self.assertRejected(valid_manifest(), current_image_version='x')

    def test_unknown_schema(self):
        self.assertRejected(valid_manifest(schema_version=2))

    def test_schema_bool_or_missing(self):
        self.assertRejected(valid_manifest(schema_version=True))
        doc = valid_manifest()
        del doc['schema_version']
        self.assertRejected(doc)

    def test_invalid_semver(self):
        self.assertRejected(valid_manifest(version='1.2'))
        self.assertRejected(valid_manifest(version='v1.2.3'))
        self.assertRejected(valid_manifest(min_image_version='nope'))

    def test_unknown_channel(self):
        self.assertRejected(valid_manifest(channel='beta'))
        self.assertRejected(valid_manifest(channel=''))

    def test_downgrade_and_equal(self):
        self.assertRejected(valid_manifest(version='1.0.0'), current_version='1.2.3')
        self.assertRejected(valid_manifest(version='1.2.3'), current_version='1.2.3')

    def test_incompatible_image(self):
        self.assertRejected(
            valid_manifest(min_image_version='3.0.0'),
            current_image_version='2.0.0')
        # An equal-or-older required image is accepted.
        manifest = updater.parse_manifest(
            valid_manifest(min_image_version='2.0.0'),
            current_image_version='2.0.0')
        self.assertEqual(manifest.min_image_version, '2.0.0')

    def test_oversize_bundle(self):
        self.assertRejected(
            valid_manifest(bundle_size=updater.MAX_BUNDLE_SIZE + 1))
        self.assertRejected(valid_manifest(bundle_size=0))
        self.assertRejected(valid_manifest(bundle_size=-1))
        self.assertRejected(valid_manifest(bundle_size='1024'))

    def test_bundle_url_must_be_https(self):
        self.assertRejected(
            valid_manifest(bundle_url='http://example.com/app.tar.zst'))
        self.assertRejected(valid_manifest(bundle_url='ftp://example.com/app'))
        self.assertRejected(valid_manifest(bundle_url='https://'))
        self.assertRejected(valid_manifest(bundle_url='not a url'))

    def test_release_url_must_be_https(self):
        self.assertRejected(valid_manifest(release_url='http://example.com/r'))
        self.assertRejected(valid_manifest(release_url=''))

    def test_malformed_sha256(self):
        self.assertRejected(valid_manifest(bundle_sha256='a' * 63))
        self.assertRejected(valid_manifest(bundle_sha256='g' * 64))
        self.assertRejected(valid_manifest(bundle_sha256=1234))

    def test_reboot_required_must_be_bool(self):
        self.assertRejected(valid_manifest(reboot_required=1))
        self.assertRejected(valid_manifest(reboot_required='false'))
        self.assertRejected(valid_manifest(reboot_required=None))

    def test_missing_required_fields(self):
        for field in ('version', 'channel', 'source_commit', 'min_image_version',
                      'bundle_url', 'bundle_sha256', 'bundle_size',
                      'release_summary', 'release_url', 'reboot_required'):
            with self.subTest(field=field):
                doc = valid_manifest()
                del doc[field]
                self.assertRejected(doc)

    def test_release_summary_bounds(self):
        self.assertRejected(valid_manifest(release_summary=''))
        self.assertRejected(valid_manifest(release_summary='   '))
        self.assertRejected(
            valid_manifest(release_summary='x' * (updater.MAX_SUMMARY_LENGTH + 1)))

    def test_release_url_too_long(self):
        self.assertRejected(
            valid_manifest(release_url='https://e.com/' + 'a' * updater.MAX_URL_LENGTH))

    def test_source_commit_invalid(self):
        self.assertRejected(valid_manifest(source_commit=''))
        self.assertRejected(valid_manifest(source_commit='bad\ncommit'))

    def test_payload_shape(self):
        for payload in ('not json', '[]', '"text"', b'\xff\xfe', None, 42):
            with self.subTest(payload=payload):
                with self.assertRaises(updater.ManifestError):
                    updater.parse_manifest(payload)

    def test_manifest_payload_too_large(self):
        blob = 'x' * (updater.MAX_MANIFEST_BYTES + 1)
        with self.assertRaises(updater.ManifestError):
            updater.parse_manifest(blob)


class ClassifyManifestTests(unittest.TestCase):
    def manifest(self, **overrides):
        return updater.parse_manifest(valid_manifest(**overrides))

    def test_available(self):
        outcome = updater.classify_manifest(
            self.manifest(), current_version='1.0.0',
            current_image_version='2.0.0')
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.outcome, updater.OUTCOME_AVAILABLE)

    def test_up_to_date(self):
        outcome = updater.classify_manifest(
            self.manifest(), current_version='1.2.3')
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.outcome, updater.OUTCOME_UP_TO_DATE)

    def test_available_without_installed_versions(self):
        outcome = updater.classify_manifest(self.manifest())
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.outcome, updater.OUTCOME_AVAILABLE)

    def test_downgrade(self):
        outcome = updater.classify_manifest(
            self.manifest(), current_version='2.0.0')
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.outcome, updater.OUTCOME_DOWNGRADE)

    def test_incompatible_image(self):
        outcome = updater.classify_manifest(
            self.manifest(min_image_version='3.0.0'),
            current_image_version='2.0.0')
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.outcome, updater.OUTCOME_INCOMPATIBLE_IMAGE)

    def test_invalid_manifest_object(self):
        class Broken:
            version = 'not-semver'
            min_image_version = '1.0.0'

        outcome = updater.classify_manifest(Broken())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.outcome, updater.OUTCOME_INVALID)

    def test_invalid_installed_version_fails_closed(self):
        # A corrupted current version must not silently skip the downgrade check.
        outcome = updater.classify_manifest(
            self.manifest(), current_version='not-semver')
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.outcome, updater.OUTCOME_INVALID)

    def test_invalid_installed_image_version_fails_closed(self):
        outcome = updater.classify_manifest(
            self.manifest(), current_image_version='not-semver')
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.outcome, updater.OUTCOME_INVALID)

    def test_outcome_is_tuple_like(self):
        outcome = updater.classify_manifest(
            self.manifest(), current_version='1.0.0')
        ok, name, reason = outcome
        self.assertTrue(ok)
        self.assertEqual(name, updater.OUTCOME_AVAILABLE)
        self.assertEqual(reason, '')


# --------------------------------------------------------------------------- #
# Minisign verification
# --------------------------------------------------------------------------- #

class VerifyFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.bundle = os.path.join(self.dir, 'app.tar.zst')
        self.sig = self.bundle + '.minisig'
        self.pubkey = os.path.join(self.dir, 'buddy3d-release.pub')
        for path, data in ((self.bundle, b'bundle'),
                           (self.sig, b'signature'),
                           (self.pubkey, PUBKEY_BODY.encode())):
            with open(path, 'wb') as handle:
                handle.write(data)

    def test_success_and_command_shape(self):
        runner = make_runner(0)
        ok, reason = updater.verify_file(
            self.bundle, public_key_path=self.pubkey, runner=runner)
        self.assertTrue(ok)
        self.assertEqual(reason, '')
        args, timeout = runner.calls[0]
        self.assertEqual(args, [
            'minisign', '-V', '-m', self.bundle,
            '-p', self.pubkey, '-x', self.sig])
        self.assertEqual(timeout, updater.MINISIGN_TIMEOUT_SECONDS)

    def test_default_signature_suffix(self):
        runner = make_runner(0)
        ok, _ = updater.verify_file(
            self.bundle, public_key_path=self.pubkey, runner=runner)
        self.assertTrue(ok)
        self.assertIn(self.bundle + '.minisig', runner.calls[0][0])

    def test_nonzero_exit_is_failure(self):
        ok, reason = updater.verify_file(
            self.bundle, public_key_path=self.pubkey, runner=make_runner(1))
        self.assertFalse(ok)
        self.assertIn('exit 1', reason)

    def test_runner_exceptions_are_failures(self):
        cases = {
            'timeout': subprocess.TimeoutExpired('minisign', 1),
            'missing': FileNotFoundError(2, 'No such file'),
            'oserror': OSError(13, 'denied'),
            'generic': RuntimeError('boom'),
        }
        for label, exc in cases.items():
            with self.subTest(label=label):
                ok, reason = updater.verify_file(
                    self.bundle, public_key_path=self.pubkey,
                    runner=make_runner(raises=exc))
                self.assertFalse(ok)
                self.assertTrue(reason)

    def test_missing_signature(self):
        os.remove(self.sig)
        ok, reason = updater.verify_file(
            self.bundle, public_key_path=self.pubkey, runner=make_runner(0))
        self.assertFalse(ok)
        self.assertEqual(reason, 'signature file is missing')

    def test_missing_public_key(self):
        ok, reason = updater.verify_file(
            self.bundle, public_key_path=os.path.join(self.dir, 'nope.pub'),
            runner=make_runner(0))
        self.assertFalse(ok)
        self.assertEqual(reason, 'public key is missing')

    def test_missing_file(self):
        ok, reason = updater.verify_file(
            os.path.join(self.dir, 'nope.tar.zst'),
            public_key_path=self.pubkey, runner=make_runner(0))
        self.assertFalse(ok)
        self.assertEqual(reason, 'file is missing')

    def test_invalid_arguments(self):
        self.assertEqual(
            updater.verify_file('', public_key_path=self.pubkey)[0], False)
        ok, reason = updater.verify_file(
            self.bundle, public_key_path='', runner=make_runner(0))
        self.assertFalse(ok)
        self.assertEqual(reason, 'public key path is required')

    def test_never_raises_with_broken_runner(self):
        class Exploding:
            def __call__(self, args, timeout):
                raise ValueError('unexpected')

        ok, reason = updater.verify_file(
            self.bundle, public_key_path=self.pubkey, runner=Exploding())
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_default_public_key_path_constant(self):
        self.assertEqual(
            updater.DEFAULT_PUBLIC_KEY_PATH,
            '/usr/share/prusa-buddy3d-camera/buddy3d-release.pub')


# --------------------------------------------------------------------------- #
# Archive member safety
# --------------------------------------------------------------------------- #

class ValidateArchiveMembersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = self.tmp.name

    def validate(self, members):
        return updater.validate_archive_members(members, self.dest)

    def test_happy_path(self):
        members = [
            tar_info('app/', kind=tarfile.DIRTYPE),
            tar_info('app/main.py', size=10),
            tar_info('app/link.py', kind=tarfile.SYMTYPE, linkname='main.py'),
            tar_info('app/hard.py', kind=tarfile.LNKTYPE, linkname='app/main.py'),
        ]
        self.assertEqual(self.validate(members), (True, ''))

    def test_absolute_path(self):
        ok, reason = self.validate([tar_info('/etc/passwd')])
        self.assertFalse(ok)
        self.assertIn('escapes', reason)

    def test_parent_traversal(self):
        for name in ('../evil', 'app/../../evil', '..', './../evil'):
            with self.subTest(name=name):
                ok, _ = self.validate([tar_info(name)])
                self.assertFalse(ok)

    def test_device_and_fifo(self):
        for kind in (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                ok, reason = self.validate([tar_info('dev/x', kind=kind)])
                self.assertFalse(ok)
                self.assertIn('device', reason)

    def test_escaping_symlink(self):
        for linkname in ('../../etc/passwd', '/etc/passwd', '..'):
            with self.subTest(linkname=linkname):
                ok, _ = self.validate([
                    tar_info('app/link', kind=tarfile.SYMTYPE, linkname=linkname)])
                self.assertFalse(ok)

    def test_escaping_hardlink(self):
        ok, _ = self.validate([
            tar_info('link', kind=tarfile.LNKTYPE, linkname='../../etc/passwd')])
        self.assertFalse(ok)

    def test_empty_linkname(self):
        ok, _ = self.validate([
            tar_info('link', kind=tarfile.SYMTYPE, linkname='')])
        self.assertFalse(ok)

    def test_setuid_setgid(self):
        for mode in (0o4755, 0o2755, 0o6755):
            with self.subTest(mode=oct(mode)):
                ok, reason = self.validate([tar_info('app/x', mode=mode)])
                self.assertFalse(ok)
                self.assertIn('setuid', reason)

    def test_foreign_owner(self):
        ok, _ = self.validate([tar_info('app/x', uid=1000)])
        self.assertFalse(ok)
        ok, _ = self.validate([tar_info('app/x', gid=1000)])
        self.assertFalse(ok)

    def test_oversize_member(self):
        ok, reason = self.validate([
            tar_info('app/big', size=updater.MAX_MEMBER_SIZE + 1)])
        self.assertFalse(ok)
        self.assertIn('size limit', reason)

    def test_oversize_total(self):
        members = [
            tar_info(f'app/f{i}', size=updater.MAX_MEMBER_SIZE) for i in range(9)
        ]
        ok, reason = self.validate(members)
        self.assertFalse(ok)
        self.assertIn('total size', reason)

    def test_invalid_dest(self):
        self.assertEqual(
            updater.validate_archive_members([], ''),
            (False, 'destination is required'))

    def test_none_members(self):
        ok, _ = updater.validate_archive_members(None, self.dest)
        self.assertFalse(ok)

    def test_nameless_member(self):
        member = tar_info('x')
        member.name = ''
        ok, _ = self.validate([member])
        self.assertFalse(ok)

    def test_never_raises(self):
        class Exploding:
            @property
            def name(self):
                raise RuntimeError('boom')

        ok, reason = updater.validate_archive_members([Exploding()], self.dest)
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_members_may_be_an_iterator(self):
        members = iter([tar_info('app/main.py', size=1)])
        self.assertEqual(self.validate(members), (True, ''))


# --------------------------------------------------------------------------- #
# Bundle extraction
# --------------------------------------------------------------------------- #

class ExtractBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.archive = os.path.join(self.dir, 'app.tar.zst')
        self.dest = os.path.join(self.dir, 'release')

    def write_archive(self, data):
        with open(self.archive, 'wb') as handle:
            handle.write(data)
        return hashlib.sha256(data).hexdigest()

    def test_sha256_mismatch(self):
        data = make_tar([(tar_info('app/main.py', size=3), b'abc')])
        self.write_archive(data)
        ok, reason = updater.extract_bundle(
            self.archive, self.dest, expected_sha256='0' * 64,
            runner=fake_zstd_runner())
        self.assertFalse(ok)
        self.assertEqual(reason, 'bundle sha256 mismatch')
        self.assertFalse(os.path.exists(self.dest))

    def test_malformed_expected_sha256(self):
        self.write_archive(b'data')
        ok, reason = updater.extract_bundle(
            self.archive, self.dest, expected_sha256='xyz',
            runner=fake_zstd_runner())
        self.assertFalse(ok)
        self.assertEqual(reason, 'bundle sha256 is malformed')

    def test_missing_archive(self):
        ok, reason = updater.extract_bundle(
            os.path.join(self.dir, 'nope.tar.zst'), self.dest,
            runner=fake_zstd_runner())
        self.assertFalse(ok)
        self.assertEqual(reason, 'bundle file is missing')

    def test_symlink_destination_is_rejected(self):
        # A pre-planted symlink at the staging path would defeat the realpath
        # checks and the data filter; refuse it outright.
        data = make_tar([(tar_info('app/main.py', size=1), b'x')])
        expected = self.write_archive(data)
        outside = os.path.join(self.dir, 'outside')
        os.makedirs(outside)
        os.symlink(outside, self.dest)
        ok, reason = updater.extract_bundle(
            self.archive, self.dest, expected_sha256=expected,
            runner=fake_zstd_runner())
        self.assertFalse(ok)
        self.assertEqual(reason, 'destination must not be a symlink')
        self.assertEqual(os.listdir(outside), [])

    def test_happy_extraction(self):
        payload = b'print("hi")\n'
        data = make_tar([
            (tar_info('app/', kind=tarfile.DIRTYPE), b''),
            (tar_info('app/main.py', size=len(payload)), payload),
        ])
        expected = self.write_archive(data)
        calls = []
        ok, reason = updater.extract_bundle(
            self.archive, self.dest, expected_sha256=expected,
            runner=fake_zstd_runner(calls))
        self.assertTrue(ok, reason)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, 'app', 'main.py')))
        with open(os.path.join(self.dest, 'app', 'main.py'), 'rb') as handle:
            self.assertEqual(handle.read(), payload)
        # Exact zstd CLI contract.
        args, timeout = calls[0]
        self.assertEqual(args[0], 'zstd')
        self.assertIn('-d', args)
        self.assertIn('-o', args)
        self.assertEqual(args[-1], self.archive)
        self.assertEqual(timeout, updater.ZSTD_TIMEOUT_SECONDS)

    def test_expected_sha256_is_optional(self):
        data = make_tar([(tar_info('app/main.py', size=3), b'abc')])
        self.write_archive(data)
        ok, reason = updater.extract_bundle(
            self.archive, self.dest, runner=fake_zstd_runner())
        self.assertTrue(ok, reason)

    def test_uppercase_expected_sha256_matches(self):
        data = make_tar([(tar_info('app/main.py', size=3), b'abc')])
        expected = self.write_archive(data)
        ok, reason = updater.extract_bundle(
            self.archive, self.dest, expected_sha256=expected.upper(),
            runner=fake_zstd_runner())
        self.assertTrue(ok, reason)

    def test_zstd_failure(self):
        self.write_archive(b'data')
        ok, reason = updater.extract_bundle(
            self.archive, self.dest, runner=fake_zstd_runner(returncode=1))
        self.assertFalse(ok)
        self.assertIn('zstd decompression failed', reason)

    def test_zstd_timeout(self):
        self.write_archive(b'data')
        ok, reason = updater.extract_bundle(
            self.archive, self.dest,
            runner=make_runner(raises=subprocess.TimeoutExpired('zstd', 1)))
        self.assertFalse(ok)
        self.assertEqual(reason, 'zstd decompression timed out')

    def test_zstd_missing_binary(self):
        self.write_archive(b'data')
        ok, reason = updater.extract_bundle(
            self.archive, self.dest,
            runner=make_runner(raises=FileNotFoundError(2, 'No such file')))
        self.assertFalse(ok)
        self.assertEqual(reason, 'zstd is not installed')

    def test_zstd_produced_no_archive(self):
        self.write_archive(b'data')
        ok, reason = updater.extract_bundle(
            self.archive, self.dest,
            runner=fake_zstd_runner(create=False))
        self.assertFalse(ok)
        self.assertEqual(reason, 'bundle decompression produced no archive')

    def test_traversal_member_is_rejected_before_extraction(self):
        data = make_tar([(tar_info('../evil.txt', size=4), b'evil')])
        expected = self.write_archive(data)
        ok, reason = updater.extract_bundle(
            self.archive, self.dest, expected_sha256=expected,
            runner=fake_zstd_runner())
        self.assertFalse(ok)
        self.assertIn('escapes', reason)
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'evil.txt')))

    def test_escaping_symlink_is_rejected_before_extraction(self):
        data = make_tar([
            (tar_info('app/', kind=tarfile.DIRTYPE), b''),
            (tar_info('app/link', kind=tarfile.SYMTYPE, linkname='../../etc/passwd'), b''),
        ])
        self.write_archive(data)
        ok, reason = updater.extract_bundle(
            self.archive, self.dest, runner=fake_zstd_runner())
        self.assertFalse(ok)
        self.assertIn('link', reason)

    def test_setuid_member_is_rejected(self):
        data = make_tar([(tar_info('app/x', size=1, mode=0o4755), b'x')])
        self.write_archive(data)
        ok, reason = updater.extract_bundle(
            self.archive, self.dest, runner=fake_zstd_runner())
        self.assertFalse(ok)
        self.assertIn('setuid', reason)

    def test_invalid_destination(self):
        self.write_archive(b'data')
        ok, reason = updater.extract_bundle(
            self.archive, '', runner=fake_zstd_runner())
        self.assertFalse(ok)
        self.assertEqual(reason, 'destination is required')

    def test_temp_dir_is_cleaned_up(self):
        before = set(os.listdir(tempfile.gettempdir()))
        data = make_tar([(tar_info('app/main.py', size=3), b'abc')])
        self.write_archive(data)
        updater.extract_bundle(
            self.archive, self.dest, runner=fake_zstd_runner())
        after = set(os.listdir(tempfile.gettempdir()))
        leaked = [name for name in after - before
                  if name.startswith('buddy3d-update-')]
        self.assertEqual(leaked, [])

    def test_never_raises_with_broken_runner(self):
        self.write_archive(b'data')

        class Exploding:
            def __call__(self, args, timeout):
                raise RuntimeError('boom')

        ok, reason = updater.extract_bundle(
            self.archive, self.dest, runner=Exploding())
        self.assertFalse(ok)
        self.assertTrue(reason)


class ImportSafetyTests(unittest.TestCase):
    def test_import_runs_no_subprocess(self):
        with patch.object(
            subprocess, 'run',
            side_effect=AssertionError('subprocess on import')
        ):
            importlib.reload(updater)
        self.assertTrue(callable(updater.parse_manifest))
        self.assertTrue(callable(updater.verify_file))
        self.assertTrue(callable(updater.extract_bundle))

    def test_supported_schema_and_channels(self):
        self.assertEqual(updater.SUPPORTED_SCHEMA_VERSIONS, frozenset({1}))
        self.assertEqual(updater.CHANNELS, ('stable', 'alpha'))


if __name__ == '__main__':
    unittest.main()
