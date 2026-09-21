"""Host tests for the WP-2 ``image/`` scaffolding (AC-9 … AC-14).

These tests never build an image, never touch a device, and never require root
or network access. They read the ``image/`` subtree and the reused
``pi-impersonator/systemd/`` units from the repository and assert the
structural and ordering invariants the build relies on.
"""

import ast
import re
import subprocess
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment normally has PyYAML
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGE = REPO_ROOT / "image"
LOCK = IMAGE / "rpi-image-gen.lock"
CONFIG = IMAGE / "config" / "buddy3d-pi-zero2w.yaml"
LAYER_DIR = IMAGE / "layer"
ASSETS = IMAGE / "assets"
ASSET_SYSTEMD = ASSETS / "systemd"
REPO_SYSTEMD = REPO_ROOT / "pi-impersonator" / "systemd"

PINNED_COMMIT = "262d4df5a9f9d4133370465399a7958a7c22cdc7"
PINNED_TAG = "v2.8.0"

REUSED_UNITS = [
    "rpicam-source.service",
    "prusa-rtsp.service",
    "prusa-ha-rtsp.service",
    "prusa-cam.service",
    "pi-persist.service",
    "prusa-data-ready.service",
    "data-ready.target",
    "bootlog.service",
]

IMAGE_ONLY_UNITS = [
    "prusa-data-grow.service",
    "prusa-camera.target",
]


def read_text(path):
    return path.read_text(encoding="utf-8")


def parse_unit(path):
    """Minimal systemd unit parser: {section: {key: value}}."""
    sections = {}
    current = None
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, {})
            continue
        if "=" in line and current is not None:
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            # systemd appends repeated list keys (After/Wants/Requires/Before);
            # accumulate them rather than keeping only the last occurrence.
            existing = sections[current].get(key)
            sections[current][key] = value if existing is None else f"{existing} {value}"
    return sections


def code_lines(text):
    """Return non-comment, non-blank lines (crude but sufficient here)."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


class ImageScaffoldingTests(unittest.TestCase):
    # --- AC-9: pinned lock --------------------------------------------------

    def test_lock_parses_and_pins_exact_revision(self):
        self.assertTrue(LOCK.is_file(), f"missing {LOCK}")
        pairs = {}
        for raw in read_text(LOCK).splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            pairs[key.strip()] = value.strip()
        self.assertEqual(pairs.get("commit"), PINNED_COMMIT)
        self.assertEqual(pairs.get("tag"), PINNED_TAG)
        self.assertIn("url", pairs)
        self.assertTrue(pairs["url"].startswith("https://github.com/raspberrypi/rpi-image-gen"))

    def test_image_subtree_required_files_exist(self):
        required = [
            IMAGE / "README.md",
            LOCK,
            CONFIG,
            LAYER_DIR / "buddy3d-image.yaml",
            LAYER_DIR / "buddy3d-suite.yaml",
            LAYER_DIR / "genimage.cfg.in.ext4",
            LAYER_DIR / "setup.sh",
            LAYER_DIR / "pre-image.sh",
            LAYER_DIR / "post-build.sh",
            LAYER_DIR / "mke2fs.conf",
            ASSETS / "prusa-data-grow.sh",
            ASSETS / "install-factory-app.sh",
            ASSETS / "build-info.py",
            ASSETS / "icon" / "buddy3d-camera.png",
            IMAGE / "scripts" / "build-image.sh",
        ]
        missing = [str(p.relative_to(REPO_ROOT)) for p in required if not p.is_file()]
        self.assertEqual(missing, [], f"missing image files: {missing}")

    # --- AC-10: config + layout --------------------------------------------

    @unittest.skipUnless(yaml is not None, "PyYAML not available")
    def test_config_is_valid_yaml_and_references_existing_files(self):
        config = yaml.safe_load(read_text(CONFIG))
        self.assertIsInstance(config, dict)

        image = config["image"]
        self.assertEqual(image["layer"], "buddy3d-image")
        self.assertTrue((LAYER_DIR / f"{image['layer']}.yaml").is_file())

        suite = config["layer"]["base"]
        self.assertEqual(suite, "buddy3d-suite")
        self.assertTrue((LAYER_DIR / f"{suite}.yaml").is_file())

        # The project-specific layer resolves its asset directories relative to
        # its own file; assert those targets and the assets they install exist.
        self.assertTrue(ASSETS.is_dir())
        for name in (
            "prusa-data-grow.sh",
            "install-factory-app.sh",
            "build-info.py",
        ):
            self.assertTrue((ASSETS / name).is_file(), f"missing asset {name}")
        for name in IMAGE_ONLY_UNITS:
            self.assertTrue((ASSET_SYSTEMD / name).is_file(), f"missing unit {name}")

        # Geometry is pinned in the config and matches the required layout.
        self.assertEqual(image["boot_part_size"], "512M")
        self.assertEqual(image["root_part_size"], "4G")
        self.assertEqual(image["persist_part_size"], "512M")
        self.assertEqual(image["rootfs_type"], "ext4")
        self.assertEqual(image["disksig"], "0xb33dcafe")

        packages = set(config["packages"])
        self.assertIn("overlayroot", packages)
        self.assertIn("cloud-guest-utils", packages)

    @unittest.skipUnless(yaml is not None, "PyYAML not available")
    def test_layer_metadata_declares_layout_variables(self):
        text = read_text(LAYER_DIR / "buddy3d-image.yaml")
        self.assertIn("X-Env-Layer-Name: buddy3d-image", text)
        self.assertIn("X-Env-Var-boot_part_size: 512M", text)
        self.assertIn("X-Env-Var-root_part_size: 4G", text)
        self.assertIn("X-Env-Var-persist_part_size: 512M", text)
        self.assertIn("X-Env-Var-assetsdir: ${DIRECTORY}/../assets", text)
        self.assertIn("X-Env-Layer-Requires: image-base", text)

    def test_layout_declares_three_ordered_partitions(self):
        text = read_text(LAYER_DIR / "genimage.cfg.in.ext4")

        # The hdimage block must be MBR with a fixed disk signature.
        self.assertIn('partition-table-type = "mbr"', text)
        self.assertIn('disk-signature = "<DISK_SIGNATURE>"', text)

        # Exactly three partitions, in the required order.
        order = re.findall(r"^\s*partition\s+(\w+)\s*\{", text, re.MULTILINE)
        self.assertEqual(order, ["boot", "root", "persist"])

        # BOOT: vfat, label BOOT, size <BOOT_SIZE>, 0xC.
        boot = self._image_block(text, "boot.vfat")
        self.assertIn('label = "BOOT"', boot)
        self.assertIn("size = <BOOT_SIZE>", boot)
        self.assertIn("-F 32", boot)  # FAT32
        self.assertRegex(text, r"partition boot \{[^}]*partition-type = 0xC", re.DOTALL)

        # ROOT: ext4, label ROOT, size <ROOT_SIZE>.
        root = self._image_block(text, "root.ext4")
        self.assertIn('label = "ROOT"', root)
        self.assertIn("size = <ROOT_SIZE>", root)

        # PERSIST: ext4, label PERSIST, size <PERSIST_SIZE>, last, /data.
        persist = self._image_block(text, "persist.ext4")
        self.assertIn('label = "PERSIST"', persist)
        self.assertIn("size = <PERSIST_SIZE>", persist)
        self.assertIn('mountpoint = "/data"', persist)
        self.assertRegex(text, r"partition persist \{[^}]*partition-type = 0x83", re.DOTALL)

        # The partition image blocks appear in boot/root/persist order.
        self.assertLess(text.index("image boot.vfat {"), text.index("image root.ext4 {"))
        self.assertLess(text.index("image root.ext4 {"), text.index("image persist.ext4 {"))

    @staticmethod
    def _image_block(text, name):
        marker = f"image {name} {{"
        start = text.index(marker)
        depth = 0
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        raise AssertionError(f"unterminated image block {name}")

    def test_partuuid_based_boot_and_data_mount(self):
        setup = read_text(LAYER_DIR / "setup.sh")
        # cmdline root and fstab entries are PARTUUID-based.
        self.assertIn("root=PARTUUID=$root_pu", setup)
        self.assertIn("PARTUUID=$root_pu", setup)
        self.assertIn("PARTUUID=$boot_pu", setup)
        self.assertIn("PARTUUID=$persist_pu", setup)
        # PARTUUIDs derive from the fixed disk signature.
        self.assertIn('sig="${IGconf_image_disksig#0x}"', setup)
        # ROOT is the immutable overlay lower with a tmpfs upper.
        self.assertIn("overlayroot=tmpfs", setup)
        self.assertIn('overlayroot="tmpfs:recurse=0"', setup)
        # PERSIST is mounted at /data, never part of the overlay.
        self.assertIn("/data", setup)

        # No device-name assumption in executable lines (comments may mention
        # /dev/mmcblk0pN to explain what is avoided).
        for path in (LAYER_DIR / "setup.sh", ASSETS / "prusa-data-grow.sh"):
            for line in code_lines(read_text(path)):
                self.assertNotIn("/dev/mmcblk0p", line, f"device name in {path}: {line}")

    # --- AC-11: first-boot growth ------------------------------------------

    def test_required_units_exist(self):
        for name in REUSED_UNITS:
            self.assertTrue((REPO_SYSTEMD / name).is_file(), f"missing reused unit {name}")
        for name in IMAGE_ONLY_UNITS:
            self.assertTrue((ASSET_SYSTEMD / name).is_file(), f"missing image-only unit {name}")
        self.assertTrue(
            (ASSET_SYSTEMD / "data-ready.target.d" / "10-data-grow.conf").is_file()
        )
        self.assertTrue(
            (ASSET_SYSTEMD / "NetworkManager.service.d" / "10-data-ready.conf").is_file()
        )

    def test_reused_units_are_not_duplicated_in_image(self):
        # The image must reuse pi-impersonator/systemd/ rather than ship
        # divergent copies. Only image-only units live under assets/systemd/.
        for name in REUSED_UNITS:
            self.assertFalse(
                (ASSET_SYSTEMD / name).exists(),
                f"{name} must be reused from pi-impersonator/systemd/, not duplicated",
            )

    def test_grow_service_ordering(self):
        unit = parse_unit(ASSET_SYSTEMD / "prusa-data-grow.service")
        section = unit["Unit"]
        self.assertIn("local-fs.target", section["After"].split())
        self.assertIn("data-ready.target", section["Before"].split())
        self.assertEqual(section["ConditionPathIsMountPoint"], "/data")
        self.assertEqual(section["RequiresMountsFor"], "/data")
        self.assertEqual(unit["Service"]["ExecStart"], "/usr/libexec/prusa-data-grow")
        self.assertEqual(unit["Service"]["Type"], "oneshot")

        dropin = parse_unit(ASSET_SYSTEMD / "data-ready.target.d" / "10-data-grow.conf")
        self.assertIn("prusa-data-grow.service", dropin["Unit"]["Requires"].split())
        self.assertIn("prusa-data-grow.service", dropin["Unit"]["After"].split())

    def test_camera_target_ordering_and_isolation(self):
        unit = parse_unit(ASSET_SYSTEMD / "prusa-camera.target")
        section = unit["Unit"]
        self.assertIn("data-ready.target", section["Requires"].split())
        self.assertIn("data-ready.target", section["After"].split())
        wants = section["Wants"].split()
        after = section["After"].split()
        for name in (
            "pi-persist.service",
            "rpicam-source.service",
            "prusa-rtsp.service",
            "prusa-ha-rtsp.service",
            "prusa-cam.service",
        ):
            self.assertIn(name, wants)
            self.assertIn(name, after)
        # Optional later units must never be hard requirements.
        for optional in ("prusa-mqtt.service", "prusa-admin.service", "prusa-updater.timer"):
            self.assertNotIn(optional, section.get("Requires", "").split())

    def test_networkmanager_ordered_after_data_ready(self):
        dropin = parse_unit(
            ASSET_SYSTEMD / "NetworkManager.service.d" / "10-data-ready.conf"
        )
        self.assertIn("data-ready.target", dropin["Unit"]["After"].split())
        # Ordering only: provisioning needs NetworkManager even when DATA is not
        # ready, so this must not be a hard Requires.
        self.assertNotIn("Requires", dropin["Unit"])

    def test_grow_script_is_interruption_safe(self):
        text = read_text(ASSETS / "prusa-data-grow.sh")
        self.assertIn("set -eu", text)
        self.assertIn("MARKER=/data/.prusa-data-grow.done", text)
        self.assertIn("EXPECTED_LABEL=PERSIST", text)
        self.assertIn("EXPECTED_PARTNUM=3", text)
        # Validates the label and the expected PARTUUID relationship.
        self.assertIn("blkid -s LABEL", text)
        self.assertIn("blkid -s PARTUUID", text)
        self.assertIn("EXPECTED_LABEL", text)
        self.assertIn('case "$partuuid" in', text)
        # Grows partition 3 and resizes the filesystem.
        self.assertIn("growpart", text)
        self.assertIn("resize2fs", text)
        self.assertIn('"$EXPECTED_PARTNUM"', text)
        # Re-runnable: an existing marker short-circuits before any grow.
        marker_guard = text.index('if [ -f "$MARKER" ]')
        grow = text.index('growpart "$base" "$num"')
        self.assertLess(marker_guard, grow)
        # The durable marker is written only after both grow and resize.
        resize = text.index('resize2fs "$src"')
        marker_write = text.index('install -m 0644 /dev/null "$MARKER"')
        self.assertLess(grow, marker_write)
        self.assertLess(resize, marker_write)
        self.assertIn("sync", text[marker_write:])

    # --- AC-13/AC-14: build script + no secrets ----------------------------

    def test_build_script_verifies_pin_and_syntax(self):
        text = read_text(IMAGE / "scripts" / "build-image.sh")
        self.assertIn("rev-parse HEAD", text)
        self.assertIn("LOCK_COMMIT", text)
        self.assertIn("pin mismatch", text)
        self.assertIn("SOURCE_DATE_EPOCH", text)
        self.assertIn("dumpe2fs", text)
        self.assertIn("75", text)
        self.assertIn("8 * 1000 * 1000 * 1000", text)

        result = subprocess.run(
            ["bash", "-n", str(IMAGE / "scripts" / "build-image.sh")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_all_new_shell_files_pass_bash_n(self):
        shells = [
            IMAGE / "scripts" / "build-image.sh",
            LAYER_DIR / "setup.sh",
            LAYER_DIR / "pre-image.sh",
            LAYER_DIR / "post-build.sh",
            LAYER_DIR / "bdebstrap" / "customize95-buddy3d-python",
            ASSETS / "prusa-data-grow.sh",
            ASSETS / "install-factory-app.sh",
        ]
        for path in shells:
            result = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, f"{path}: {result.stderr}")

    def test_build_info_generator_is_stdlib_only(self):
        source = read_text(ASSETS / "build-info.py")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[0])
        self.assertTrue(
            imported <= {"argparse", "json", "os", "sys"},
            f"non-stdlib imports: {imported}",
        )
        result = subprocess.run(
            ["python3", str(ASSETS / "build-info.py"), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_icon_is_png(self):
        data = (ASSETS / "icon" / "buddy3d-camera.png").read_bytes()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_no_secrets_or_personal_data(self):
        forbidden_substrings = [
            "bullitt",
            "BEGIN OPENSSH PRIVATE KEY",
            "BEGIN RSA PRIVATE KEY",
            "BEGIN EC PRIVATE KEY",
            "PRIVATE KEY-----",
            "wifi-sec.psk",
            "wpa_passphrase",
            "id_rsa",
            "id_ed25519",
        ]
        secret_assignment = re.compile(
            r"(?i)\b(token|password|passwd|psk|secret)\s*=\s*\S+"
        )
        mac = re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")
        machine_id = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
        home_path = re.compile(r"/home/(?!prusa-cam\b|\$|\{)")

        for path in sorted(IMAGE.rglob("*")):
            if not path.is_file() or path.suffix == ".png":
                continue
            rel = path.relative_to(REPO_ROOT)
            try:
                text = read_text(path)
            except UnicodeDecodeError:
                self.fail(f"unexpected binary file in image/: {rel}")
            for needle in forbidden_substrings:
                self.assertNotIn(needle, text, f"{needle!r} in {rel}")
            self.assertIsNone(secret_assignment.search(text), f"secret assignment in {rel}")
            self.assertIsNone(mac.search(text), f"MAC address in {rel}")
            self.assertIsNone(machine_id.search(text), f"machine-id value in {rel}")
            self.assertIsNone(home_path.search(text), f"personal home path in {rel}")

        # No Wi-Fi connection profile or SSH host key may be committed.
        self.assertEqual(list(IMAGE.rglob("*.nmconnection")), [])
        self.assertEqual(list(IMAGE.rglob("ssh_host_*")), [])
        # No private key material.
        self.assertEqual(list(IMAGE.rglob("*.pem")), [])
        self.assertEqual(list(IMAGE.rglob("*.key")), [])

    def test_identity_clearing_hooks_present(self):
        # The image must actively strip build-time identity rather than merely
        # not contain it.
        post = read_text(LAYER_DIR / "post-build.sh")
        self.assertIn("/etc/machine-id", post)
        self.assertIn("ssh_host_*", post)
        self.assertIn("machine-id", post)


if __name__ == "__main__":
    unittest.main()
