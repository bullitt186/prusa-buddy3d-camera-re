"""Host tests for WP-R5 application release automation (AC-29, AC-34, AC-35).

These tests never build or flash an image, never require root, and never touch
the network. They drive ``image/scripts/make-app-release.sh`` and the new
``--source-tree`` mode of ``image/scripts/scan-secrets.sh`` against small
synthetic trees in a temporary directory and assert the updater's bundle
contract: deterministic tar with uid/gid 0 members, ``main.py`` +
``requirements.lock`` + ``wheels/`` at the top level, a manifest that validates
through ``updater.parse_manifest``, matching hashes/sizes, and clean rejection
of malformed or incomplete releases.
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PI_DIR = REPO_ROOT / "pi-impersonator"
SCRIPTS = REPO_ROOT / "image" / "scripts"
MAKE_APP_RELEASE = SCRIPTS / "make-app-release.sh"
SCAN_SECRETS = SCRIPTS / "scan-secrets.sh"

sys.path.insert(0, str(PI_DIR))
import updater  # noqa: E402

BASH = shutil.which("bash")
TAR = shutil.which("tar")
ZSTD = shutil.which("zstd")
SHA256SUM = shutil.which("sha256sum")
MINISIGN = shutil.which("minisign")
GIT = shutil.which("git")
PYTHON3 = sys.executable

SOURCE_DATE_EPOCH = "1700000000"

# The updater's exact manifest schema.
MANIFEST_FIELDS = {
    "schema_version",
    "version",
    "channel",
    "source_commit",
    "min_image_version",
    "bundle_url",
    "bundle_sha256",
    "bundle_size",
    "release_summary",
    "release_url",
    "reboot_required",
}

HASH = "a" * 64
LOCK_TEXT = (
    "#\n"
    "# synthetic hash-locked requirements\n"
    "#\n"
    "foo==1.0.0 \\\n"
    f"    --hash=sha256:{HASH}\n"
    "bar==2.1.0\n"
)


def run(cmd, env=None, cwd=None):
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, env=merged, cwd=cwd)


class ScanSecretsSourceModeTests(unittest.TestCase):
    """The --source-tree mode gates source without flagging ordinary code."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, *paths, mode=None):
        cmd = [BASH, str(SCAN_SECRETS)]
        if mode:
            cmd.append(mode)
        cmd.extend(str(p) for p in paths)
        return run(cmd)

    @unittest.skipUnless(BASH, "bash not available")
    def test_clean_source_tree_passes(self):
        src = self.dir / "src"
        src.mkdir()
        (src / "main.py").write_text("token = secrets.token_urlsafe(32)\n")
        (src / "wifi.py").write_text("psk = ''\n")
        result = self.scan(src, mode="--source-tree")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("clean", result.stdout)

    @unittest.skipUnless(BASH, "bash not available")
    def test_source_mode_ignores_code_shaped_values(self):
        # The default artifact mode flags these; source mode must not.
        src = self.dir / "src"
        src.mkdir()
        fixture = src / "code.py"
        fixture.write_text("token = cfg['identity']['token']\npsk = ''\n")
        default = self.scan(fixture)
        self.assertNotEqual(default.returncode, 0)
        source = self.scan(src, mode="--source-tree")
        self.assertEqual(source.returncode, 0, source.stdout + source.stderr)

    @unittest.skipUnless(BASH, "bash not available")
    def test_planted_private_key_fails(self):
        src = self.dir / "src"
        src.mkdir()
        (src / "leak.py").write_text(
            "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n"
        )
        result = self.scan(src, mode="--source-tree")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("leak.py:1", result.stdout)

    @unittest.skipUnless(BASH, "bash not available")
    def test_planted_secret_named_file_fails(self):
        src = self.dir / "src"
        src.mkdir()
        (src / "id_rsa").write_text("not obviously secret\n")
        result = self.scan(src, mode="--source-tree")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("id_rsa", result.stdout)

    @unittest.skipUnless(BASH, "bash not available")
    def test_planted_machine_id_fails(self):
        src = self.dir / "src"
        src.mkdir()
        (src / "machine-id").write_text("0123456789abcdef0123456789abcdef\n")
        result = self.scan(src, mode="--source-tree")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("machine-id:1", result.stdout)

    @unittest.skipUnless(BASH, "bash not available")
    @unittest.skipIf(os.geteuid() == 0, "root can read chmod 000 files")
    def test_unreadable_file_is_a_match(self):
        path = self.dir / "locked.txt"
        path.write_text("clean\n", encoding="utf-8")
        os.chmod(path, 0)
        try:
            result = self.scan(path)
        finally:
            os.chmod(path, 0o644)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unreadable path", result.stdout)

    @unittest.skipUnless(BASH, "bash not available")
    @unittest.skipIf(os.geteuid() == 0, "root can read chmod 000 files")
    def test_unreadable_file_in_directory_fails_closed(self):
        tree = self.dir / "tree"
        tree.mkdir()
        (tree / "ok.txt").write_text("clean\n", encoding="utf-8")
        locked = tree / "locked.txt"
        locked.write_text("clean\n", encoding="utf-8")
        os.chmod(locked, 0)
        try:
            result = self.scan(tree)
        finally:
            os.chmod(locked, 0o644)
        self.assertNotEqual(result.returncode, 0)

    @unittest.skipUnless(BASH, "bash not available")
    @unittest.skipIf(os.geteuid() == 0, "root can read chmod 000 dirs")
    def test_unreadable_directory_fails_closed(self):
        locked = self.dir / "locked-dir"
        locked.mkdir()
        os.chmod(locked, 0)
        try:
            result = self.scan(locked)
        finally:
            os.chmod(locked, 0o755)
        self.assertNotEqual(result.returncode, 0)

    @unittest.skipUnless(BASH, "bash not available")
    @unittest.skipIf(os.geteuid() == 0, "root can read chmod 000 dirs")
    def test_unreadable_nested_directory_fails_closed(self):
        parent = self.dir / "parent"
        child = parent / "child"
        child.mkdir(parents=True)
        (parent / "ok.txt").write_text("clean\n", encoding="utf-8")
        os.chmod(child, 0)
        try:
            result = self.scan(parent)
        finally:
            os.chmod(child, 0o755)
        self.assertNotEqual(result.returncode, 0)


@unittest.skipUnless(
    BASH and TAR and ZSTD and SHA256SUM and PYTHON3,
    "bash/tar/zstd/sha256sum/python3 required",
)
class MakeAppReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.make_source()
        self.wheels = self.dir / "wheels"
        self.wheels.mkdir()
        (self.wheels / "foo-1.0.0-py3-none-any.whl").write_bytes(b"")
        (self.wheels / "bar-2.1.0-py3-none-any.whl").write_bytes(b"")
        self.lock = self.dir / "requirements.lock"
        self.lock.write_text(LOCK_TEXT, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    # -- fixtures ----------------------------------------------------------
    def make_source(self):
        src = self.dir / "src"
        (src / "sub").mkdir(parents=True)
        (src / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (src / "sub" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        (src / "tests").mkdir()
        (src / "tests" / "test_x.py").write_text("def test(): pass\n")
        (src / "__pycache__").mkdir()
        (src / "__pycache__" / "main.cpython-312.pyc").write_text("x")
        (src / "config.ini").write_text("token = supersecret\n")
        (src / "config.ini.example").write_text("token = <PLACEHOLDER>\n")
        (src / "README.md").write_text("# readme\n")
        (src / "deploy.sh").write_text("#!/bin/sh\n")
        (src / "bootstrap.sh").write_text("#!/bin/sh\n")
        (src / "venv").mkdir()
        (src / "venv" / "lib.py").write_text("x\n")
        (src / "backups").mkdir()
        (src / "backups" / "old.txt").write_text("old\n")
        return src

    def release(self, *extra, version="1.2.3", out=None, env=None):
        out = out or (self.dir / "out")
        cmd = [
            BASH,
            str(MAKE_APP_RELEASE),
            "--source-dir", str(self.src),
            "--version", version,
            "--out-dir", str(out),
            "--wheels", str(self.wheels),
            "--url-base", "https://example.org/releases/v1.2.3",
            "--requirements-lock", str(self.lock),
            *extra,
        ]
        merged = {"SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH}
        if env:
            merged.update(env)
        return run(cmd, env=merged)

    def bundle(self, out=None, version="1.2.3"):
        return (out or (self.dir / "out")) / f"buddy3d-camera-app-{version}.tar.zst"

    def read_manifest(self, out=None):
        path = (out or (self.dir / "out")) / "update-manifest.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def read_tar(self, bundle):
        tar_path = self.dir / "bundle.tar"
        result = run([ZSTD, "-d", "-q", "-f", "-o", str(tar_path), str(bundle)])
        self.assertEqual(result.returncode, 0, result.stderr)
        return tar_path

    def members(self, bundle):
        with tarfile.open(self.read_tar(bundle), "r:") as archive:
            return archive.getmembers()

    @staticmethod
    def components(name):
        return [part for part in name.split("/") if part not in ("", ".")]

    # -- happy path --------------------------------------------------------
    def test_happy_path_produces_all_artifacts(self):
        result = self.release("--source-commit", "deadbeef")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = self.dir / "out"
        for name in (
            "buddy3d-camera-app-1.2.3.tar.zst",
            "buddy3d-camera-app-1.2.3.tar.zst.sha256",
            "update-manifest.json",
        ):
            self.assertTrue((out / name).is_file(), f"missing {name}")
        # No key supplied: signatures are omitted with a clear warning.
        self.assertFalse((out / "buddy3d-camera-app-1.2.3.tar.zst.minisig").exists())
        self.assertFalse((out / "update-manifest.json.minisig").exists())
        self.assertIn("no --key provided", result.stderr)

        bundle = self.bundle()
        manifest = updater.parse_manifest(
            (out / "update-manifest.json").read_bytes()
        )
        self.assertEqual(manifest.version, "1.2.3")
        self.assertEqual(manifest.channel, "stable")
        self.assertEqual(manifest.schema_version, 1)
        self.assertEqual(manifest.source_commit, "deadbeef")
        self.assertEqual(manifest.min_image_version, "1.0.0")
        self.assertEqual(manifest.reboot_required, False)
        self.assertEqual(
            manifest.bundle_url,
            "https://example.org/releases/v1.2.3/buddy3d-camera-app-1.2.3.tar.zst",
        )
        self.assertEqual(
            manifest.bundle_sha256, hashlib.sha256(bundle.read_bytes()).hexdigest()
        )
        self.assertEqual(manifest.bundle_size, bundle.stat().st_size)

        sha_line = (out / "buddy3d-camera-app-1.2.3.tar.zst.sha256").read_text()
        recorded, _, name = sha_line.strip().partition("  ")
        self.assertEqual(recorded, manifest.bundle_sha256)
        self.assertEqual(name, bundle.name)

    def test_manifest_has_exactly_the_updater_fields(self):
        result = self.release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(set(self.read_manifest()), MANIFEST_FIELDS)

    def test_alpha_channel_is_a_channel_not_a_suffix(self):
        result = self.release("--channel", "alpha")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = updater.parse_manifest(
            (self.dir / "out" / "update-manifest.json").read_bytes()
        )
        self.assertEqual(manifest.channel, "alpha")
        self.assertEqual(manifest.version, "1.2.3")

    def test_reboot_required_flag(self):
        result = self.release("--reboot-required")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIs(self.read_manifest()["reboot_required"], True)

    def test_release_url_and_summary_are_recorded(self):
        result = self.release(
            "--release-url", "https://example.org/releases/tag/v1.2.3",
            "--release-summary", "A synthetic release",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = self.read_manifest()
        self.assertEqual(manifest["release_url"], "https://example.org/releases/tag/v1.2.3")
        self.assertEqual(manifest["release_summary"], "A synthetic release")

    # -- bundle contract ---------------------------------------------------
    def test_tar_members_are_normalized_and_expected(self):
        result = self.release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        bundle = self.bundle()
        members = self.members(bundle)

        names = [member.name for member in members]
        for member in members:
            self.assertEqual(member.uid, 0, member.name)
            self.assertEqual(member.gid, 0, member.name)
            self.assertFalse(member.mode & (stat.S_ISUID | stat.S_ISGID), member.name)

        # The updater's own validation pass must accept the archive.
        ok, reason = updater.validate_archive_members(members, str(self.dir))
        self.assertTrue(ok, reason)

        # Included: main.py at the top level, requirements.lock and wheels/.
        leafs = [self.components(n)[-1] for n in names if self.components(n)]
        self.assertIn("main.py", leafs)
        self.assertIn("requirements.lock", leafs)
        wheel_names = [
            self.components(n)[-1] for n in names
            if "wheels" in self.components(n) and self.components(n)
        ]
        self.assertIn("foo-1.0.0-py3-none-any.whl", wheel_names)
        self.assertIn("bar-2.1.0-py3-none-any.whl", wheel_names)
        self.assertIn("sub/mod.py", [n.lstrip("./") for n in names])

        # Excluded: tests, caches, local config, dev scripts, backups.
        forbidden_components = {"tests", "__pycache__", "venv", "backups"}
        forbidden_names = {"config.ini", "README.md", "deploy.sh", "bootstrap.sh"}
        for name in names:
            parts = self.components(name)
            for part in parts:
                self.assertNotIn(part, forbidden_components, name)
            leaf = parts[-1] if parts else ""
            self.assertNotIn(leaf, forbidden_names, name)
            self.assertFalse(leaf.endswith(".pyc"), name)
            self.assertFalse(leaf.endswith(".example"), name)

        # The updater can actually extract it with main.py at the root.
        dest = self.dir / "extracted"
        ok, reason = updater.extract_bundle(
            str(bundle), str(dest), expected_sha256=self.read_manifest()["bundle_sha256"]
        )
        self.assertTrue(ok, reason)
        self.assertTrue((dest / "main.py").is_file())
        self.assertTrue((dest / "requirements.lock").is_file())
        self.assertTrue((dest / "wheels").is_dir())

    def test_bundle_is_byte_identical_across_runs(self):
        out_a = self.dir / "outA"
        out_b = self.dir / "outB"
        first = self.release(out=out_a)
        second = self.release(out=out_b)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(
            self.bundle(out_a).read_bytes(), self.bundle(out_b).read_bytes()
        )

    def test_requirements_lock_contents_are_copied(self):
        result = self.release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tar_path = self.read_tar(self.bundle())
        with tarfile.open(tar_path, "r:") as archive:
            member = archive.getmember("./requirements.lock")
            copied = archive.extractfile(member).read().decode("utf-8")
        self.assertEqual(copied, LOCK_TEXT)

    def test_dashed_requirement_matches_underscored_wheel(self):
        # pip normalizes '-' to '_' in wheel filenames; the checker must too.
        lock = self.dir / "dashed.lock"
        lock.write_text("python-socketio==5.17.0\n", encoding="utf-8")
        (self.wheels / "python_socketio-5.17.0-py3-none-any.whl").write_bytes(b"")
        result = self.release("--requirements-lock", str(lock))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    # -- signing -----------------------------------------------------------
    @unittest.skipUnless(MINISIGN, "minisign not available")
    def test_signs_bundle_and_manifest_when_key_given(self):
        pub = self.dir / "test.pub"
        sec = self.dir / "test.key"
        gen = run([MINISIGN, "-G", "-W", "-f", "-p", str(pub), "-s", str(sec)])
        self.assertEqual(gen.returncode, 0, gen.stderr)

        result = self.release("--key", str(sec))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = self.dir / "out"
        bundle_sig = out / "buddy3d-camera-app-1.2.3.tar.zst.minisig"
        manifest_sig = out / "update-manifest.json.minisig"
        self.assertTrue(bundle_sig.is_file())
        self.assertTrue(manifest_sig.is_file())

        ok, reason = updater.verify_file(str(self.bundle()), public_key_path=str(pub))
        self.assertTrue(ok, reason)
        ok, reason = updater.verify_file(
            str(out / "update-manifest.json"), public_key_path=str(pub)
        )
        self.assertTrue(ok, reason)
        # The key path is not echoed as key material.
        self.assertNotIn(str(sec), result.stdout)

    @unittest.skipIf(MINISIGN is not None, "minisign installed; absence cannot be tested")
    def test_key_without_minisign_fails_loudly(self):
        result = self.release("--key", str(self.dir / "release.key"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("minisign", result.stderr.lower())

    def test_unsigned_run_warns_and_omits_signatures(self):
        result = self.release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("UNSIGNED", result.stderr)
        self.assertFalse((self.dir / "out" / "buddy3d-camera-app-1.2.3.tar.zst.minisig").exists())
        self.assertFalse((self.dir / "out" / "update-manifest.json.minisig").exists())

    # -- rejections --------------------------------------------------------
    def test_bad_semver_is_rejected(self):
        for version in ("1.2", "1.2.3.4", "01.2.3", "1.2.3-alpha", "v1.2.3"):
            with self.subTest(version=version):
                result = self.release(version=version)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("X.Y.Z", result.stderr)

    def test_bad_min_image_version_is_rejected(self):
        result = self.release("--min-image-version", "1.2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("min-image-version", result.stderr)

    def test_bad_channel_is_rejected(self):
        result = self.release("--channel", "beta")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("channel", result.stderr)

    def test_non_https_urls_are_rejected(self):
        for flag, value in (
            ("--url-base", "http://example.org/x"),
            ("--url-base", "ftp://example.org/x"),
            ("--release-url", "http://example.org/x"),
        ):
            with self.subTest(flag=flag, value=value):
                result = self.release(flag, value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("https", result.stderr)

    def test_missing_wheel_for_pinned_dependency_is_rejected(self):
        (self.wheels / "bar-2.1.0-py3-none-any.whl").unlink()
        result = self.release()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bar==2.1.0", result.stderr)

    def test_missing_required_flags_are_rejected(self):
        base = [
            BASH,
            str(MAKE_APP_RELEASE),
            "--source-dir", str(self.src),
            "--version", "1.2.3",
            "--out-dir", str(self.dir / "out"),
            "--wheels", str(self.wheels),
            "--url-base", "https://example.org/x",
        ]
        for flag in ("--version", "--out-dir", "--wheels", "--url-base"):
            with self.subTest(flag=flag):
                cmd = [item for item in base]
                index = cmd.index(flag)
                del cmd[index:index + 2]
                result = run(cmd)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(flag, result.stderr)

    def test_missing_source_dir_is_rejected(self):
        result = run(
            [
                BASH, str(MAKE_APP_RELEASE),
                "--source-dir", str(self.dir / "absent"),
                "--version", "1.2.3",
                "--out-dir", str(self.dir / "out"),
                "--wheels", str(self.wheels),
                "--url-base", "https://example.org/x",
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stderr)

    def test_source_dir_without_main_is_rejected(self):
        empty = self.dir / "empty"
        empty.mkdir()
        result = run(
            [
                BASH, str(MAKE_APP_RELEASE),
                "--source-dir", str(empty),
                "--version", "1.2.3",
                "--out-dir", str(self.dir / "out"),
                "--wheels", str(self.wheels),
                "--url-base", "https://example.org/x",
                "--requirements-lock", str(self.lock),
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("main.py", result.stderr)

    def test_lock_without_pinned_requirements_is_rejected(self):
        empty_lock = self.dir / "empty.lock"
        empty_lock.write_text("# nothing pinned\n", encoding="utf-8")
        result = self.release("--requirements-lock", str(empty_lock))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no pinned requirements", result.stderr)

    # -- secret scan -------------------------------------------------------
    def test_planted_secret_in_source_tree_fails_the_release(self):
        (self.src / "leak.py").write_text(
            "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n", encoding="utf-8"
        )
        result = self.release()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret scan", result.stderr)

    def test_planted_secrets_toml_fails_the_release(self):
        # A config-like non-.py file must not bypass the scan.
        (self.src / "secrets.toml").write_text(
            'psk = "hunter2"\n', encoding="utf-8"
        )
        result = self.release()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret scan", result.stderr)

    def test_staged_default_scan_runs_on_non_code_files(self):
        # Even a non-config-like filename carrying a credential must fail,
        # proving the staged tree is scanned in the default (artifact) mode.
        (self.src / "notes.txt").write_text(
            'psk = "hunter2"\n', encoding="utf-8"
        )
        result = self.release()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret scan", result.stderr)

    def test_unexpected_config_like_file_is_rejected(self):
        # No match, but the filename is not on the allowlist.
        (self.src / "settings.toml").write_text(
            'camera_name = "x"\n', encoding="utf-8"
        )
        result = self.release()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("config-like", result.stderr)

    def test_clean_tree_still_passes(self):
        result = self.release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("scan-secrets: clean", result.stdout)

    # -- produced-archive member validation --------------------------------
    def test_setgid_source_file_fails_the_release(self):
        evil = self.src / "evil.py"
        evil.write_text("x = 1\n", encoding="utf-8")
        os.chmod(evil, 0o2755)
        try:
            result = self.release()
        finally:
            os.chmod(evil, 0o644)
            evil.unlink()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("updater member validation", result.stderr)
        self.assertIn("setuid/setgid", result.stderr)

    def test_absolute_symlink_fails_the_release(self):
        link = self.src / "abs_link.py"
        os.symlink("/etc/passwd", link)
        result = self.release()
        link.unlink()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("updater member validation", result.stderr)

    # -- channel/tag guard -------------------------------------------------
    @unittest.skipUnless(GIT, "git not available")
    def test_alpha_tag_cannot_be_published_as_stable(self):
        repo = self.dir / "tagged-src"
        shutil.copytree(self.src, repo)
        run([GIT, "init", "-q"], cwd=repo)
        run([GIT, "-c", "user.email=t@example.org", "-c", "user.name=T",
             "add", "-A"], cwd=repo)
        run([GIT, "-c", "user.email=t@example.org", "-c", "user.name=T",
             "commit", "-q", "-m", "x"], cwd=repo)
        run([GIT, "tag", "alpha-v1.2.3"], cwd=repo)

        stable = run(
            [
                BASH, str(MAKE_APP_RELEASE),
                "--source-dir", str(repo),
                "--version", "1.2.3",
                "--channel", "stable",
                "--out-dir", str(self.dir / "out-stable"),
                "--wheels", str(self.wheels),
                "--url-base", "https://example.org/x",
                "--requirements-lock", str(self.lock),
            ],
            env={"SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH},
        )
        self.assertNotEqual(stable.returncode, 0)
        self.assertIn("alpha tag", stable.stderr)

        alpha = run(
            [
                BASH, str(MAKE_APP_RELEASE),
                "--source-dir", str(repo),
                "--version", "1.2.3",
                "--channel", "alpha",
                "--out-dir", str(self.dir / "out-alpha"),
                "--wheels", str(self.wheels),
                "--url-base", "https://example.org/x",
                "--requirements-lock", str(self.lock),
            ],
            env={"SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH},
        )
        self.assertEqual(alpha.returncode, 0, alpha.stdout + alpha.stderr)
        # VCS metadata from a git-checkout --source-dir must never be bundled.
        alpha_bundle = self.bundle(out=self.dir / "out-alpha")
        for member in self.members(alpha_bundle):
            self.assertNotIn(".git", self.components(member.name), member.name)

    @unittest.skipUnless(BASH, "bash not available")
    def test_scripts_pass_bash_n(self):
        for path in (MAKE_APP_RELEASE, SCAN_SECRETS):
            result = run([BASH, "-n", str(path)])
            self.assertEqual(result.returncode, 0, f"{path}: {result.stderr}")


if __name__ == "__main__":
    unittest.main()
