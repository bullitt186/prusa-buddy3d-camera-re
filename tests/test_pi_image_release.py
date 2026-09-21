"""Host tests for WP-2b release assembly and secret scanning (AC-14, AC-34, AC-35).

These tests never build or flash an image, never require root, and never touch
the network. They drive ``image/scripts/make-release.sh`` and
``image/scripts/scan-secrets.sh`` against small synthetic files in a temporary
directory and assert the artifact contract: hashes/sizes agree across the
compressed image, the extracted image, the Imager OS list, and the SPDX SBOM.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGE = REPO_ROOT / "image"
SCRIPTS = IMAGE / "scripts"
MAKE_RELEASE = SCRIPTS / "make-release.sh"
SCAN_SECRETS = SCRIPTS / "scan-secrets.sh"
TEMPLATE = IMAGE / "imager" / "os-list.template.json"

BASH = shutil.which("bash")
XZ = shutil.which("xz")
SHA256SUM = shutil.which("sha256sum")
MINISIGN = shutil.which("minisign")
PYTHON3 = sys.executable

DEVICE_ID = "pi3-64bit"
SOURCE_DATE_EPOCH = "1700000000"

# Required render tokens documented for the Imager template.
TEMPLATE_TOKENS = [
    "{{IMAGE_URL}}",
    "{{ICON_URL}}",
    "{{WEBSITE}}",
    "{{RELEASE_DATE}}",
    "{{EXTRACT_SIZE}}",
    "{{EXTRACT_SHA256}}",
    "{{DOWNLOAD_SIZE}}",
    "{{DOWNLOAD_SHA256}}",
    "{{VERSION}}",
    "{{DEVICES}}",
    "{{INIT_FORMAT}}",
    "{{ARCH}}",
]


def run(cmd, env=None, cwd=None):
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        cmd, capture_output=True, text=True, env=merged, cwd=cwd
    )


class ScanSecretsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, content, binary=False):
        path = self.dir / name
        if binary:
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def scan(self, *paths, env=None):
        return run([BASH, str(SCAN_SECRETS), *[str(p) for p in paths]], env=env)

    @unittest.skipUnless(BASH, "bash not available")
    def test_scripts_pass_bash_n(self):
        for path in (MAKE_RELEASE, SCAN_SECRETS):
            result = run([BASH, "-n", str(path)])
            self.assertEqual(result.returncode, 0, f"{path}: {result.stderr}")

    @unittest.skipUnless(BASH, "bash not available")
    def test_clean_fixture_passes(self):
        clean = self.write("clean.txt", "hello world\nno secrets here\n")
        result = self.scan(clean)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("clean", result.stdout)

    @unittest.skipUnless(BASH, "bash not available")
    def test_flags_private_key(self):
        secret = self.write(
            "key.pem",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n",
        )
        result = self.scan(secret)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("key.pem:1", result.stdout)

    @unittest.skipUnless(BASH, "bash not available")
    def test_flags_nonempty_machine_id(self):
        machine = self.write("machine-id", "0123456789abcdef0123456789abcdef\n")
        result = self.scan(machine)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("machine-id:1", result.stdout)

        empty = self.write("empty-machine-id", "\n")
        self.assertEqual(self.scan(empty).returncode, 0)

    @unittest.skipUnless(BASH, "bash not available")
    def test_flags_nmconnection(self):
        profile = self.write(
            "wifi.nmconnection",
            "[connection]\nid=test\n\n[wifi-security]\nkey-mgmt=wpa-psk\n",
        )
        result = self.scan(profile)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wifi.nmconnection", result.stdout)

    @unittest.skipUnless(BASH, "bash not available")
    def test_flags_wifi_psk(self):
        config = self.write("wpa.conf", "network={\n\tpsk=supersecretvalue\n}\n")
        result = self.scan(config)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wpa.conf:2", result.stdout)

    @unittest.skipUnless(BASH, "bash not available")
    def test_flags_personal_home_path(self):
        fixture = self.write("notes.txt", "config lives in /home/devperson/app\n")
        result = self.scan(fixture)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("notes.txt:1", result.stdout)

    @unittest.skipUnless(BASH, "bash not available")
    def test_allows_service_account_home(self):
        fixture = self.write("service.txt", "config lives in /home/prusa-cam/app\n")
        self.assertEqual(self.scan(fixture).returncode, 0)

    @unittest.skipUnless(BASH, "bash not available")
    def test_flags_configured_personal_username(self):
        fixture = self.write("who.txt", "operator is devperson\n")
        result = self.scan(
            fixture, env={"SCAN_PERSONAL_USER_PATTERN": "devperson"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("who.txt:1", result.stdout)

    @unittest.skipUnless(BASH, "bash not available")
    def test_directory_scan_and_missing_path(self):
        nested = self.dir / "tree"
        nested.mkdir()
        self.write("tree/plain.txt", "ok\n")
        self.assertEqual(self.scan(nested).returncode, 0)

        (nested / "leak.txt").write_text(
            "0123456789abcdef0123456789abcdef\n", encoding="utf-8"
        )
        result = self.scan(nested)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("leak.txt:1", result.stdout)

        missing = self.scan(self.dir / "does-not-exist")
        self.assertEqual(missing.returncode, 2)

    @unittest.skipUnless(BASH, "bash not available")
    def test_binary_noise_is_ignored(self):
        binary = self.write(
            "firmware.bin",
            b"\x00\x01\x02-----BEGIN OPENSSH PRIVATE KEY-----\x00\x00",
            binary=True,
        )
        self.assertEqual(self.scan(binary).returncode, 0)

    @unittest.skipUnless(BASH, "bash not available")
    def test_flags_prusa_token_json(self):
        fixture = self.write("cfg.json", '{"token": "0123456789abcdef"}\n')
        result = self.scan(fixture)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cfg.json:1", result.stdout)

    @unittest.skipUnless(BASH, "bash not available")
    def test_flags_prusa_token_env(self):
        fixture = self.write("env.txt", "PRUSA_TOKEN=0123456789abcdef\n")
        result = self.scan(fixture)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("env.txt:1", result.stdout)

    @unittest.skipUnless(BASH, "bash not available")
    def test_flags_mqtt_password(self):
        fixture = self.write("mqtt.txt", "MQTT_PASSWORD=hunter2\n")
        result = self.scan(fixture)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mqtt.txt:1", result.stdout)

    @unittest.skipUnless(BASH, "bash not available")
    def test_flags_password_hash(self):
        fixture = self.write("secrets.toml", 'password_hash = "deadbeef"\n')
        result = self.scan(fixture)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secrets.toml:1", result.stdout)

    @unittest.skipUnless(BASH, "bash not available")
    def test_flags_minisign_secret_key_content(self):
        fixture = self.write("note.txt", "minisign encrypted secret key\n")
        result = self.scan(fixture)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("note.txt:1", result.stdout)

    @unittest.skipUnless(BASH, "bash not available")
    def test_flags_secret_bearing_filenames(self):
        for name in ("ssh_host_ed25519_key", "minisign.key", "id_rsa", "id_ed25519"):
            with self.subTest(name=name):
                path = self.write(name, "not obviously secret\n")
                result = self.scan(path)
                self.assertNotEqual(result.returncode, 0, name)
                self.assertIn(name, result.stdout)
                path.unlink()

    @unittest.skipUnless(BASH, "bash not available")
    def test_dash_prefixed_path_is_still_scanned(self):
        # grep needs `--` so a path beginning with '-' is not eaten as an option.
        self.write("-secret", "psk=supersecretvalue\n")
        result = run(
            [BASH, "-c", f'cd "{self.dir}" && "{SCAN_SECRETS}" -secret']
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(BASH, "bash not available")
    def test_line_with_personal_and_service_home_is_flagged(self):
        # Per-match filtering: an allowed /home/prusa-cam on the same line must
        # not mask a personal /home/devperson path.
        fixture = self.write(
            "mixed.txt", "cp /home/devperson/keys /home/prusa-cam/stolen\n"
        )
        result = self.scan(fixture)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mixed.txt:1", result.stdout)


@unittest.skipUnless(
    BASH and XZ and SHA256SUM and PYTHON3,
    "bash/xz/sha256sum/python3 required",
)
class MakeReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.image = self.dir / "fake.img"
        self.image.write_bytes(bytes(range(256)) * 1024)
        self.packages = self.dir / "packages.txt"
        self.packages.write_text(
            "bash\t5.2.15\ncoreutils\t9.1-1\nzlib1g\t1:1.2.13\n",
            encoding="utf-8",
        )
        self.build_info = self.dir / "build-info.json"
        self.build_info.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_commit": "deadbeefcafe",
                    "builder_revision": "262d4df5",
                    "os_suite": "trixie",
                    "kernel_package": "linux-image-rpi-v8",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.out = self.dir / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def make_release(self, *extra, version="1.2.3", **kwargs):
        cmd = [
            BASH,
            str(MAKE_RELEASE),
            "--image",
            str(self.image),
            "--version",
            version,
            "--out-dir",
            str(self.out),
            "--release-date",
            "2026-01-02",
            "--url-base",
            "https://example.org/releases/v1.2.3",
            "--icon",
            "https://example.org/icon.png",
            "--website",
            "https://example.org",
            *extra,
        ]
        env = {"SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH}
        env.update(kwargs.get("env", {}))
        return run(cmd, env=env)

    def base(self, version="1.2.3"):
        return f"buddy3d-camera-pi-zero2w-{version}"

    def read_json(self, name):
        return json.loads((self.out / name).read_text(encoding="utf-8"))

    def test_produces_required_artifacts(self):
        result = self.make_release(
            "--packages",
            str(self.packages),
            "--build-info",
            str(self.build_info),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        base = self.base()
        for name in (
            f"{base}.img.xz",
            f"{base}.img.xz.sha256",
            f"{base}.spdx.json",
            f"{base}.packages.txt",
            "buddy3d-camera-os-list.json",
        ):
            self.assertTrue((self.out / name).is_file(), f"missing {name}")
        # No key supplied: the artifact must remain unsigned, with a warning.
        self.assertFalse((self.out / f"{base}.img.xz.minisig").exists())
        self.assertIn("no --key provided", result.stderr)

    def test_sha256_file_matches_compressed_image(self):
        result = self.make_release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        xz_path = self.out / f"{self.base()}.img.xz"
        expected = hashlib.sha256(xz_path.read_bytes()).hexdigest()
        line = (self.out / f"{self.base()}.img.xz.sha256").read_text(encoding="utf-8")
        recorded, _, name = line.strip().partition("  ")
        self.assertEqual(recorded, expected)
        self.assertEqual(name, xz_path.name)

    def test_os_list_matches_artifacts(self):
        result = self.make_release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        doc = self.read_json("buddy3d-camera-os-list.json")
        self.assertEqual(set(doc), {"os_list"})
        entry = doc["os_list"][0]

        xz_path = self.out / f"{self.base()}.img.xz"
        self.assertEqual(entry["image_download_sha256"], hashlib.sha256(xz_path.read_bytes()).hexdigest())
        self.assertEqual(entry["image_download_size"], xz_path.stat().st_size)
        self.assertEqual(entry["extract_sha256"], hashlib.sha256(self.image.read_bytes()).hexdigest())
        self.assertEqual(entry["extract_size"], self.image.stat().st_size)

        self.assertEqual(entry["url"], f"https://example.org/releases/v1.2.3/{self.base()}.img.xz")
        self.assertEqual(entry["icon"], "https://example.org/icon.png")
        self.assertEqual(entry["website"], "https://example.org")
        self.assertEqual(entry["release_date"], "2026-01-02")
        self.assertEqual(entry["devices"], [DEVICE_ID])
        self.assertEqual(entry["architecture"], "armv8")
        self.assertEqual(entry["init_format"], "systemd")
        self.assertIn("1.2.3", entry["name"])
        # Every token must have been rendered.
        raw = (self.out / "buddy3d-camera-os-list.json").read_text(encoding="utf-8")
        self.assertNotIn("{{", raw)

    def test_spdx_is_valid_and_uses_real_package_data(self):
        result = self.make_release(
            "--packages", str(self.packages), "--build-info", str(self.build_info)
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        doc = self.read_json(f"{self.base()}.spdx.json")

        self.assertEqual(doc["spdxVersion"], "SPDX-2.3")
        self.assertEqual(doc["SPDXID"], "SPDXRef-DOCUMENT")
        self.assertEqual(doc["dataLicense"], "CC0-1.0")
        self.assertTrue(doc["name"])
        self.assertTrue(doc["documentNamespace"])
        self.assertIn("created", doc["creationInfo"])
        self.assertTrue(doc["creationInfo"]["creators"])

        names = {pkg["name"] for pkg in doc["packages"]}
        self.assertIn("bash", names)
        self.assertIn("coreutils", names)
        for pkg in doc["packages"]:
            self.assertTrue(pkg["SPDXID"].startswith("SPDXRef-Package-"))
            self.assertEqual(pkg["downloadLocation"], "NOASSERTION")
            self.assertIs(pkg["filesAnalyzed"], False)
        image_pkg = next(p for p in doc["packages"] if p["name"] == self.base())
        self.assertIn("source_commit=deadbeefcafe", image_pkg["sourceInfo"])

    def test_spdx_without_inputs_has_no_fabricated_packages(self):
        result = self.make_release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        doc = self.read_json(f"{self.base()}.spdx.json")
        self.assertEqual(doc["packages"], [])

    def test_packages_manifest_is_copied(self):
        result = self.make_release("--packages", str(self.packages))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        copied = (self.out / f"{self.base()}.packages.txt").read_text(encoding="utf-8")
        self.assertIn("coreutils", copied)

    def test_invalid_semver_is_rejected(self):
        result = self.make_release(version="1.2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SemVer", result.stderr)

    def test_missing_image_is_rejected(self):
        result = run(
            [
                BASH,
                str(MAKE_RELEASE),
                "--image",
                str(self.dir / "absent.img"),
                "--version",
                "1.2.3",
                "--out-dir",
                str(self.out),
            ],
            env={"SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stderr)

    def test_secret_in_packages_fails_the_release(self):
        bad = self.dir / "bad-packages.txt"
        bad.write_text(
            "bash\t5.2.15\n-----BEGIN OPENSSH PRIVATE KEY-----\n",
            encoding="utf-8",
        )
        result = self.make_release("--packages", str(bad))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("secret scan failed", result.stderr)

    @unittest.skipIf(MINISIGN is not None, "minisign installed; absence cannot be tested")
    def test_key_without_minisign_fails_loudly(self):
        result = self.make_release("--key", str(self.dir / "release.key"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("minisign", result.stderr.lower())
        self.assertFalse((self.out / f"{self.base()}.img.xz.minisig").exists())

    def test_template_has_all_documented_tokens(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        for token in TEMPLATE_TOKENS:
            self.assertIn(token, text, f"template missing {token}")
        self.assertIn("_comment", text)


if __name__ == "__main__":
    unittest.main()
