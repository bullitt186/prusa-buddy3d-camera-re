"""Host tests for ``image/scripts/validate-image.sh`` (AC-13, WP-2b).

Hermetic: no root, no mount, no loop device, no network. Partition-table tests
build a synthetic 3-partition MBR file with ``truncate`` + ``sfdisk`` in a temp
directory (and are skipped where ``sfdisk`` is absent). Rootfs tests build a
synthetic directory tree that represents the mounted image (ROOT at the top,
BOOT under ``boot/firmware``, PERSIST under ``data/``) so no mount is needed.

All fixtures use synthetic placeholders only: no personal usernames, no real
MACs/SSIDs, no secret material.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "image" / "scripts" / "validate-image.sh"
REPO_SYSTEMD = REPO_ROOT / "pi-impersonator" / "systemd"
ASSET_SYSTEMD = REPO_ROOT / "image" / "assets" / "systemd"

HAS_SFDISK = shutil.which("sfdisk") is not None

DISKSIG = "0xb33dcafe"
ALIGN_SECTORS = 16384  # 8 MiB, matching genimage `align = 8M`
BOOT_SIZE_SECTORS = 1048576  # 512 MiB
ROOT_SIZE_SECTORS = 8388608  # 4 GiB
PERSIST_SIZE_SECTORS = 1048576  # 512 MiB

REQUIRED_DATA_DIRS = [
    "prusa-cam",
    "prusa-cam/config",
    "prusa-cam/releases",
    "prusa-cam/backups",
    "network",
    "network/system-connections",
    "sdcard",
    "sdcard/timelapse",
]


def run_validator(*args):
    return subprocess.run(
        [str(SCRIPT), *[str(a) for a in args]],
        capture_output=True,
        text=True,
    )


def valid_partitions():
    """(start, size, type, bootable) for BOOT/ROOT/PERSIST, 8M-aligned."""
    boot_start = ALIGN_SECTORS
    root_start = boot_start + BOOT_SIZE_SECTORS
    persist_start = root_start + ROOT_SIZE_SECTORS
    return [
        (boot_start, BOOT_SIZE_SECTORS, "c", True),
        (root_start, ROOT_SIZE_SECTORS, "83", False),
        (persist_start, PERSIST_SIZE_SECTORS, "83", False),
    ]


def write_mbr_image(path, entries, disksig=DISKSIG):
    """Create a sparse image file and lay down the given MBR entries."""
    path = Path(path)
    total_bytes = max(start + size for start, size, _, _ in entries) * 512
    truncate = shutil.which("truncate")
    if truncate:
        subprocess.run(
            [truncate, "-s", str(total_bytes), str(path)],
            check=True,
            capture_output=True,
        )
    else:  # pragma: no cover - coreutils truncate is normally present
        with open(path, "wb") as handle:
            handle.truncate(total_bytes)

    lines = ["label: dos", f"label-id: {disksig}", "unit: sectors"]
    for start, size, ptype, bootable in entries:
        line = f"start={start}, size={size}, type={ptype}"
        if bootable:
            line += ", bootable"
        lines.append(line)

    subprocess.run(
        ["sfdisk", "--no-reread", "-q", str(path)],
        input="\n".join(lines) + "\n",
        text=True,
        check=True,
        capture_output=True,
    )
    return path


def make_image(path):
    """A valid synthetic image when sfdisk exists, else a dummy file.

    On hosts without sfdisk the validator skips the partition group, so the
    rootfs tests still exercise everything else.
    """
    if HAS_SFDISK:
        return write_mbr_image(path, valid_partitions())
    path = Path(path)
    path.write_bytes(b"\x00" * 1024)
    return path


def make_rootfs(base):
    """Build a synthetic, defect-free rootfs tree representing the image."""
    root = Path(base)

    systemd = root / "etc" / "systemd" / "system"
    systemd.mkdir(parents=True)

    # Reused application units verbatim, plus the image-only units.
    for src in sorted(REPO_SYSTEMD.glob("*.service")) + sorted(
        REPO_SYSTEMD.glob("*.target")
    ):
        shutil.copy2(src, systemd / src.name)
    for name in ("prusa-data-grow.service", "prusa-camera.target"):
        shutil.copy2(ASSET_SYSTEMD / name, systemd / name)

    # Overlay root: config file and boot cmdline.
    (root / "etc" / "overlayroot.conf").write_text(
        'overlayroot="tmpfs:recurse=0"\n', encoding="utf-8"
    )
    boot = root / "boot" / "firmware"
    boot.mkdir(parents=True)
    (boot / "cmdline.txt").write_text(
        "console=serial0,115200 root=PARTUUID=b33dcafe-02 rootfstype=ext4 "
        "rootwait overlayroot=tmpfs\n",
        encoding="utf-8",
    )

    # Volatile, size-limited journald.
    journald = root / "etc" / "systemd" / "journald.conf.d"
    journald.mkdir(parents=True)
    (journald / "10-volatile.conf").write_text(
        "[Journal]\nStorage=volatile\nRuntimeMaxUse=32M\n", encoding="utf-8"
    )

    # NetworkManager configuration.
    networkmanager = root / "etc" / "NetworkManager"
    networkmanager.mkdir(parents=True)
    (networkmanager / "NetworkManager.conf").write_text(
        "[main]\nplugins=keyfile\n", encoding="utf-8"
    )

    # Factory application + launcher fallback.
    app = root / "opt" / "prusa-cam"
    app.mkdir(parents=True)
    (app / "main.py").write_text("# synthetic factory app\n", encoding="utf-8")
    launcher = app / "launcher.sh"
    launcher.write_text(
        "#!/bin/sh\nexec /opt/prusa-cam/venv/bin/python /opt/prusa-cam/main.py\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    # build-info.json.
    build_info_dir = root / "usr" / "share" / "prusa-buddy3d-camera"
    build_info_dir.mkdir(parents=True)
    (build_info_dir / "build-info.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_commit": "0" * 40,
                "builder_revision": "262d4df5a9f9d4133370465399a7958a7c22cdc7",
                "os_suite": "trixie",
                "kernel_package": "linux-image-rpi-v8",
                "package_manifest": "/usr/share/prusa-buddy3d-camera/packages.txt",
            }
        ),
        encoding="utf-8",
    )

    # SSH disabled by default, root locked, no authorized_keys.
    ssh = root / "etc" / "ssh"
    ssh.mkdir(parents=True)
    (ssh / "sshd_config").write_text(
        "PasswordAuthentication no\nPermitRootLogin prohibit-password\n",
        encoding="utf-8",
    )
    (root / "etc" / "shadow").write_text(
        "root:!:19000:0:99999:7:::\n", encoding="utf-8"
    )
    (root / "etc" / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/bash\n"
        f"prusa-cam:x:{os.getuid()}:{os.getgid()}:Prusa Camera:"
        "/opt/prusa-cam:/usr/sbin/nologin\n",
        encoding="utf-8",
    )

    # No persistent machine-id.
    (root / "etc" / "machine-id").write_text("", encoding="utf-8")

    # Initial PERSIST structure; ownership matches the passwd prusa-cam entry.
    data = root / "data"
    for relative in REQUIRED_DATA_DIRS:
        (data / relative).mkdir(parents=True, exist_ok=True)

    return root


class ValidateImageScriptSyntaxTests(unittest.TestCase):
    def test_script_exists_and_passes_bash_n(self):
        self.assertTrue(SCRIPT.is_file(), f"missing {SCRIPT}")
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_help_lists_documented_flags(self):
        result = run_validator("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in (
            "--image",
            "--mount-root",
            "--root-image",
            "--manifest",
            "--strict",
            "--disksig",
        ):
            self.assertIn(flag, result.stdout)


@unittest.skipUnless(HAS_SFDISK, "sfdisk not available")
class PartitionTableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _image(self, entries, name="image.img", disksig=DISKSIG):
        return write_mbr_image(Path(self.tmp) / name, entries, disksig)

    def test_correct_partition_table_passes(self):
        image = self._image(valid_partitions())
        result = run_validator("--image", image)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT: PASS", result.stdout)
        self.assertIn("exactly 3 partitions", result.stdout)

    def test_wrong_partition_order_fails(self):
        boot, root, persist = valid_partitions()
        entries = [
            (boot[0], boot[1], "83", False),
            (root[0], root[1], "c", True),
            (persist[0], persist[1], "83", False),
        ]
        image = self._image(entries)
        result = run_validator("--image", image)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("[FAIL]", result.stdout)

    def test_missing_partition_fails(self):
        image = self._image(valid_partitions()[:2])
        result = run_validator("--image", image)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly 3 partitions", result.stdout)

    def test_wrong_partition_type_fails(self):
        boot, root, persist = valid_partitions()
        entries = [
            (boot[0], boot[1], "83", True),
            (root[0], root[1], "83", False),
            (persist[0], persist[1], "83", False),
        ]
        image = self._image(entries)
        result = run_validator("--image", image)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BOOT partition type", result.stdout)

    def test_wrong_partition_size_fails(self):
        # BOOT must be ~512 MiB (8 MiB alignment tolerance). 256 MiB and
        # 768 MiB are both far outside it; keep ROOT/PERSIST starts valid so the
        # only defect is the BOOT size.
        for label, boot_size in (("smaller", 524288), ("larger", 1572864)):
            with self.subTest(boot_size=label):
                boot_start = ALIGN_SECTORS
                root_start = boot_start + boot_size
                persist_start = root_start + ROOT_SIZE_SECTORS
                entries = [
                    (boot_start, boot_size, "c", True),
                    (root_start, ROOT_SIZE_SECTORS, "83", False),
                    (persist_start, PERSIST_SIZE_SECTORS, "83", False),
                ]
                image = self._image(entries, name=f"image-{label}.img")
                result = run_validator("--image", image)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("BOOT size", result.stdout)
                self.assertIn("RESULT: FAIL", result.stdout)

    def test_missing_bootable_flag_fails(self):
        boot, root, persist = valid_partitions()
        entries = [
            (boot[0], boot[1], boot[2], False),
            root,
            persist,
        ]
        image = self._image(entries)
        result = run_validator("--image", image)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BOOT bootable", result.stdout)

    def test_matching_disk_signature_passes(self):
        image = self._image(valid_partitions())
        result = run_validator("--image", image, "--disksig", DISKSIG)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MBR disk signature", result.stdout)

    def test_wrong_disk_signature_fails(self):
        image = self._image(valid_partitions())
        result = run_validator("--image", image, "--disksig", "0xdeadbeef")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MBR disk signature", result.stdout)


class RootfsValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.image = make_image(Path(cls.tmp) / "image.img")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _root(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return make_rootfs(tmp)

    def test_good_rootfs_passes(self):
        root = self._root()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("RESULT: PASS", result.stdout)
        self.assertIn("all required units installed", result.stdout)

    def test_unit_referencing_personal_home_fails(self):
        root = self._root()
        unit = root / "etc" / "systemd" / "system" / "prusa-cam.service"
        unit.write_text(
            unit.read_text(encoding="utf-8")
            + "Environment=HOME=/home/alice\n",
            encoding="utf-8",
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("personal home", result.stdout)

    def test_ssh_host_key_present_fails(self):
        root = self._root()
        (root / "etc" / "ssh" / "ssh_host_ed25519_key").write_text(
            "synthetic-not-a-key\n", encoding="utf-8"
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SSH host key", result.stdout)

    def test_nonempty_machine_id_fails(self):
        root = self._root()
        (root / "etc" / "machine-id").write_text("a" * 32 + "\n", encoding="utf-8")
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("machine-id", result.stdout)

    def test_nmconnection_present_fails(self):
        root = self._root()
        profiles = root / "etc" / "NetworkManager" / "system-connections"
        profiles.mkdir(parents=True, exist_ok=True)
        (profiles / "synthetic.nmconnection").write_text(
            "[connection]\nid=synthetic\n", encoding="utf-8"
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("nmconnection", result.stdout)

    def test_unit_missing_data_ready_ordering_fails(self):
        root = self._root()
        unit = root / "etc" / "systemd" / "system" / "rpicam-source.service"
        text = unit.read_text(encoding="utf-8")
        text = text.replace("After=network.target data-ready.target", "After=network.target")
        text = text.replace("Requires=data-ready.target\n", "")
        unit.write_text(text, encoding="utf-8")
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("data-ready.target", result.stdout)

    def test_missing_build_info_fails(self):
        root = self._root()
        (root / "usr" / "share" / "prusa-buddy3d-camera" / "build-info.json").unlink()
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("build-info.json", result.stdout)

    def test_identity_json_in_data_fails(self):
        root = self._root()
        (root / "data" / "prusa-cam" / "identity.json").write_text(
            '{"device_id": "synthetic"}\n', encoding="utf-8"
        )
        result = run_validator("--image", self.image, "--mount-root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("device identity", result.stdout)

    def test_non_strict_without_mount_root_skips_and_passes(self):
        result = run_validator("--image", self.image)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SKIPPED", result.stdout)
        self.assertIn("not mounted", result.stdout)
        self.assertIn("RESULT: PASS", result.stdout)

    def test_strict_without_mount_root_fails(self):
        result = run_validator("--image", self.image, "--strict")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--strict", result.stdout)
        self.assertIn("RESULT: FAIL", result.stdout)


if __name__ == "__main__":
    unittest.main()
