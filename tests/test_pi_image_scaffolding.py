"""Host tests for the WP-2 ``image/`` scaffolding (AC-9 … AC-14).

These tests never build an image, never touch a device, and never require root
or network access. They read the ``image/`` subtree and the reused
``pi-impersonator/systemd/`` units from the repository and assert the
structural and ordering invariants the build relies on.
"""

import ast
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment normally has PyYAML
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGE = REPO_ROOT / "image"
LOCK = IMAGE / "rpi-image-gen.lock"
REQUIREMENTS_IN = IMAGE / "requirements.in"
REQUIREMENTS_LOCK = IMAGE / "requirements.lock"
CONFIG = IMAGE / "config" / "buddy3d-pi-zero2w.yaml"
LAYER_DIR = IMAGE / "layer"
ASSETS = IMAGE / "assets"
ASSET_SYSTEMD = ASSETS / "systemd"
PRUSA_PRIV = ASSETS / "prusa-priv"
SUDOERS = ASSETS / "sudoers" / "prusa-cam"
REPO_SYSTEMD = REPO_ROOT / "pi-impersonator" / "systemd"

PINNED_COMMIT = "262d4df5a9f9d4133370465399a7958a7c22cdc7"
PINNED_TAG = "v2.8.0"

REUSED_UNITS = [
    "rpicam-source.service",
    "prusa-rtsp.service",
    "prusa-ha-rtsp.service",
    "prusa-cam.service",
    "prusa-admin.service",
    "prusa-provisioning.service",
    "pi-persist.service",
    "prusa-data-ready.service",
    "data-ready.target",
    "bootlog.service",
    "prusa-updater.service",
    "prusa-updater.timer",
    "prusa-updater-install.service",
]

IMAGE_ONLY_UNITS = [
    "prusa-data-grow.service",
    "prusa-camera.target",
    "prusa-boot-mode.service",
]

# Direct Python runtime dependencies (image/requirements.in / AC-14).
DIRECT_PYTHON_DEPS = ("aiohttp", "python-socketio", "paho-mqtt")


def lock_package_specs(text):
    """Return the pinned package specs in a pip-compile lock file.

    Comment lines are dropped and backslash continuations are re-joined so a
    pinned package and its ``--hash`` lines form one spec.
    """
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    specs = []
    buf = ""
    for line in lines:
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
        else:
            buf += line
            specs.append(buf)
            buf = ""
    if buf:
        specs.append(buf)
    return [s for s in specs if "==" in s and not s.lstrip().startswith("-")]



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
            REQUIREMENTS_IN,
            REQUIREMENTS_LOCK,
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
            PRUSA_PRIV,
            SUDOERS,
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
        # WP-R3/AC-14: the venv build needs ensurepip (versioned venv package)
        # and pip inside the image. python3-venv alone does not bring ensurepip
        # in the pinned trixie snapshot (see the package-list comment).
        self.assertIn("python3.13-venv", packages)
        self.assertIn("python3-pip", packages)

    @unittest.skipUnless(yaml is not None, "PyYAML not available")
    def test_layer_metadata_declares_layout_variables(self):
        text = read_text(LAYER_DIR / "buddy3d-image.yaml")
        self.assertIn("X-Env-Layer-Name: buddy3d-image", text)
        self.assertIn("X-Env-Var-boot_part_size: 512M", text)
        self.assertIn("X-Env-Var-root_part_size: 4G", text)
        self.assertIn("X-Env-Var-persist_part_size: 512M", text)
        self.assertIn("X-Env-Var-assetsdir: ${DIRECTORY}/../assets", text)
        self.assertIn("X-Env-Layer-Requires: image-base", text)

    @unittest.skipUnless(yaml is not None, "PyYAML not available")
    def test_layer_packages_install_the_appliance_runtime(self):
        # The config's IGconf_packages list is not reliably installed by the
        # built-in customize20-packages hook, so the layer's mmdebstrap.packages
        # list is authoritative for the runtime (WP-R3 build bring-up).
        doc = yaml.safe_load(read_text(LAYER_DIR / "buddy3d-image.yaml"))
        packages = set(doc["mmdebstrap"]["packages"])
        for name in (
            "rpicam-apps",
            "python3.13-venv",
            "python3-pip",
            "python3-gi",
            "gstreamer1.0-tools",
            "gstreamer1.0-nice",
            "gir1.2-gst-rtsp-server-1.0",
            "overlayroot",
            "cloud-guest-utils",
            "samba",
            "sudo",
        ):
            self.assertIn(name, packages, name)

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
            "prusa-admin.service",
        ):
            self.assertIn(name, wants)
            self.assertIn(name, after)
        # The setup runtime is mutually exclusive with the camera runtime.
        self.assertIn("prusa-provisioning.service", section["Conflicts"].split())
        # AC-18: the camera target is ordered after the provisioning process so
        # it cannot start while QR capture/probe may still hold the sensor.
        self.assertIn("prusa-provisioning.service", section["After"].split())
        # WP-R4b: the updater timer is pulled optionally (available after claim)
        # and is never a hard requirement of camera startup.
        self.assertIn("prusa-updater.timer", wants)
        # Optional later units must never be hard requirements.
        for optional in ("prusa-mqtt.service", "prusa-updater.timer"):
            self.assertNotIn(optional, section.get("Requires", "").split())
        # WP-R4c: the install oneshot is triggered only via the root helper and
        # is never part of camera startup.
        self.assertNotIn("prusa-updater-install.service", wants)
        self.assertNotIn("prusa-updater-install.service", after)
        self.assertNotIn(
            "prusa-updater-install.service", section.get("Requires", "").split()
        )

    def test_provisioning_unit_is_conflicts_gated_and_never_enabled(self):
        unit = parse_unit(REPO_SYSTEMD / "prusa-provisioning.service")
        section = unit["Unit"]
        self.assertIn("data-ready.target", section["After"].split())
        self.assertIn("network-online.target", section["Wants"].split())
        self.assertIn("prusa-camera.target", section["Conflicts"].split())
        # AC-7: After= only, so the setup path survives a missing DATA partition.
        self.assertNotIn("Requires", section)
        # Started by the selector, never enabled.
        self.assertNotIn("Install", unit)

        service = unit["Service"]
        self.assertEqual(service["User"], "prusa-cam")
        self.assertIn("ADMIN_MODE=setup", service["Environment"])
        self.assertIn("ADMIN_HOST=192.168.4.1", service["Environment"])
        self.assertIn("hotspot_ctl.py start", service["ExecStartPre"])
        self.assertIn("/opt/prusa-cam/admin_app.py", service["ExecStart"])
        self.assertIn("hotspot_ctl.py stop", service["ExecStopPost"])

    def test_boot_mode_unit_selects_at_multi_user(self):
        unit = parse_unit(ASSET_SYSTEMD / "prusa-boot-mode.service")
        section = unit["Unit"]
        self.assertIn("data-ready.target", section["After"].split())
        self.assertIn("data-ready.target", section["Wants"].split())
        # AC-7: Wants= (not Requires=) so a missing DATA still reaches setup.
        self.assertNotIn("Requires", section)

        service = unit["Service"]
        self.assertEqual(service["Type"], "oneshot")
        self.assertIn("boot_mode.py", service["ExecStart"])
        self.assertIn("--apply", service["ExecStart"])
        self.assertIn("multi-user.target", unit["Install"]["WantedBy"].split())

    def test_installer_enables_only_the_pre_runtime_gate(self):
        text = read_text(ASSETS / "install-factory-app.sh")
        enable_block = text.split("systemctl enable", 1)[1].split("|| true", 1)[0]
        for enabled in (
            "data-ready.target",
            "prusa-data-ready.service",
            "prusa-data-grow.service",
            "pi-persist.service",
            "bootlog.service",
            "prusa-boot-mode.service",
            "prusa-updater.timer",
        ):
            self.assertIn(enabled, enable_block)
        for deferred in (
            "prusa-camera.target",
            "prusa-provisioning.service",
            "prusa-admin.service",
            "rpicam-source.service",
            "prusa-rtsp.service",
            "prusa-ha-rtsp.service",
            "prusa-cam.service",
            "prusa-updater-install.service",
        ):
            self.assertNotIn(deferred, enable_block)

    def test_installer_keeps_opt_root_owned(self):
        text = read_text(ASSETS / "install-factory-app.sh")
        # B3: /opt/prusa-cam must be root:root so the root helper never executes
        # service-account-writable code.
        self.assertIn('chown -R root:root "$root$APP_ROOT"', text)
        self.assertNotIn('chown -R "$uid:$gid" "$root$APP_ROOT"', text)

    def test_runtime_units_exec_the_launcher(self):
        # WP-R4c: the runtime units start through launcher.sh with their own
        # entry point so an installed signed release under DATA runs.
        expected = {
            "prusa-cam.service": "main.py",
            "prusa-rtsp.service": "rtsp_server.py",
            "prusa-ha-rtsp.service": "rtsp_server.py",
            "prusa-admin.service": "admin_app.py",
        }
        for name, script in expected.items():
            with self.subTest(unit=name):
                service = parse_unit(REPO_SYSTEMD / name)["Service"]
                self.assertEqual(
                    service["ExecStart"], f"/opt/prusa-cam/launcher.sh {script}"
                )

    def test_launcher_prefers_release_venv_with_factory_fallback(self):
        text = read_text(ASSETS / "launcher.sh")
        self.assertIn("current/venv/bin/python", text)
        self.assertIn("APP_ROOT/venv/bin/python", text)
        self.assertIn("DEFAULT_SCRIPT=main.py", text)
        self.assertIn("exec ", text)

    def test_installer_installs_runtime_launcher(self):
        text = read_text(ASSETS / "install-factory-app.sh")
        self.assertIn('"$assets/launcher.sh"', text)
        self.assertIn('"$root$APP_ROOT/launcher.sh"', text)
        # The launcher is the shipped runtime entry point, not a WP-6 placeholder.
        self.assertNotIn("WP-6", text)

    def test_installer_installs_helper_and_sudoers(self):
        text = read_text(ASSETS / "install-factory-app.sh")
        self.assertIn('"$assets/prusa-priv"', text)
        self.assertIn("/usr/libexec/prusa-cam/prusa-priv", text)
        self.assertIn('"$assets/sudoers/prusa-cam"', text)
        self.assertIn("/etc/sudoers.d/prusa-cam", text)
        self.assertIn("visudo -cf", text)

    def test_prusa_priv_asset_is_executable_and_valid(self):
        self.assertTrue(PRUSA_PRIV.is_file())
        self.assertTrue(os.access(PRUSA_PRIV, os.X_OK), "prusa-priv must be executable")
        result = subprocess.run(
            ["bash", "-n", str(PRUSA_PRIV)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        text = read_text(PRUSA_PRIV)
        for verb in (
            "start-camera",
            "stop-provisioning",
            "hotspot-start",
            "hotspot-stop",
            "wifi-station-apply",
            "install-update",
        ):
            self.assertIn(verb, text)
        # Unknown verbs must exit 2 before any privileged command.
        unknown = subprocess.run(
            ["bash", str(PRUSA_PRIV), "definitely-not-a-verb"],
            capture_output=True, text=True,
        )
        self.assertEqual(unknown.returncode, 2, unknown.stderr)

    def test_sudoers_asset_is_narrow_and_valid(self):
        text = read_text(SUDOERS)
        self.assertIn(
            "prusa-cam ALL=(root) NOPASSWD: /usr/libexec/prusa-cam/prusa-priv",
            text,
        )
        # Only the fixed-verb helper is granted; no wildcard command.
        self.assertNotIn("ALL", text.split("NOPASSWD:", 1)[1])
        visudo = shutil.which("visudo")
        if visudo is None:
            self.skipTest("visudo not available")
        result = subprocess.run(
            [visudo, "-cf", str(SUDOERS)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
            PRUSA_PRIV,
        ]
        for path in shells:
            result = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, f"{path}: {result.stderr}")

    def test_ssh_is_disabled_after_all_layers(self):
        # The reused openssh-server layer runs `enable-units ssh ...` AFTER our
        # image-layer customize hook, so the disable must live in post-build.sh,
        # which runs after every layer — not in install-factory-app.sh.
        installer = read_text(ASSETS / "install-factory-app.sh")
        self.assertNotIn('systemctl --root="$root" disable', installer)
        self.assertIn("post-build.sh", installer)

        text = read_text(LAYER_DIR / "post-build.sh")
        ssh_block = text[text.index('SSH_UNITS="ssh.service'):]
        for unit in (
            "ssh.service",
            "ssh.socket",
            "ssh-hostkeys-generate.service",
            "sshd.service",
        ):
            self.assertIn(unit, ssh_block, f"{unit} not handled in SSH disable block")

        # Explicit removal of any remaining enablement symlink.
        self.assertRegex(
            ssh_block, r'rm -f "\$fs/etc/systemd/system/"\*\.wants/"\$unit"'
        )

        # Post-check must fail the build loudly, not silently ship SSH enabled.
        self.assertIn("exit 1", ssh_block)
        self.assertIn("SSH enablement symlink survived", ssh_block)

    def test_factory_installer_records_package_manifest(self):
        text = read_text(ASSETS / "install-factory-app.sh")

        # The manifest is generated from dpkg inside the target root.
        self.assertIn("dpkg-query -W -f='${Package} ${Version}\\n'", text)
        self.assertIn("/usr/share/prusa-buddy3d-camera/packages.txt", text)

        # The in-image path is handed to the generator, so package_manifest is
        # a non-empty string rather than null.
        self.assertRegex(text, r'--package-manifest\s+"\$manifest_arg"')
        self.assertRegex(text, r'MANIFEST_IMAGE_PATH=/usr/share/prusa-buddy3d-camera/packages\.txt')

    # --- WP-R3 / AC-14: hash-locked Python venv -----------------------------

    def test_requirements_in_lists_direct_dependencies(self):
        text = read_text(REQUIREMENTS_IN)
        for dep in DIRECT_PYTHON_DEPS:
            self.assertIn(dep, text, f"{dep} missing from requirements.in")

    def test_requirements_lock_pins_every_package_with_hashes(self):
        text = read_text(REQUIREMENTS_LOCK)
        specs = lock_package_specs(text)
        self.assertGreaterEqual(len(specs), len(DIRECT_PYTHON_DEPS))
        names = []
        for spec in specs:
            name = spec.split("==", 1)[0].strip().lower()
            names.append(name)
            self.assertIn(
                "--hash=sha256:", spec, f"{name} has no sha256 hash in the lock"
            )
        for dep in DIRECT_PYTHON_DEPS:
            self.assertIn(dep, names, f"{dep} missing from requirements.lock")
        self.assertIn(
            "--extra-index-url https://www.piwheels.org/simple", text
        )

    def test_installer_copies_lock_and_builds_hashed_venv(self):
        text = read_text(ASSETS / "install-factory-app.sh")
        # The committed lock is copied into the image verbatim, mode 0644.
        self.assertIn(
            'install -m 0644 "$repo/image/requirements.lock" '
            '"$root$APP_ROOT/requirements.lock"',
            text,
        )
        # System site packages keeps Debian's PyGObject/GStreamer visible.
        self.assertIn(
            "/usr/bin/python3 -m venv --system-site-packages", text
        )
        # Hash enforcement and no on-disk wheel cache.
        self.assertIn("--require-hashes", text)
        self.assertIn("--no-cache-dir", text)
        self.assertIn('"$APP_ROOT/requirements.lock"', text)
        # The venv is part of the immutable root-owned factory tree.
        self.assertIn('chown -R root:root "$root$APP_ROOT/venv"', text)
        # A venv/pip failure must abort the build, never ship a venv-less image.
        self.assertIn("refusing to ship a venv-less image", text)
        self.assertIn("exit 1", text)

    def test_installer_records_python_lock_sha256(self):
        text = read_text(ASSETS / "install-factory-app.sh")
        self.assertIn("sha256sum", text)
        self.assertRegex(text, r'--python-lock-sha256\s+"\$lock_sha256"')

    def test_python_hook_is_noop_owned_by_installer(self):
        text = read_text(
            LAYER_DIR / "bdebstrap" / "customize95-buddy3d-python"
        )
        # It must document the ownership boundary and exit cleanly.
        self.assertIn("install-factory-app.sh", text)
        self.assertIn("exit 0", text)
        # It must not build the venv itself (hook ordering is not guaranteed).
        # Comments may name the installer's commands; only executable lines
        # matter here.
        body = "\n".join(code_lines(text))
        self.assertNotIn("python3 -m venv", body)
        self.assertNotIn("--require-hashes", body)
        self.assertNotIn("pip install", body)
        result = subprocess.run(
            ["bash", "-n", str(LAYER_DIR / "bdebstrap" / "customize95-buddy3d-python")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_build_info_generator_records_nonempty_package_manifest(self):
        # Functional guard for AC-14: the generator records the supplied path.
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "build-info.json"
            result = subprocess.run(
                [
                    "python3",
                    str(ASSETS / "build-info.py"),
                    "--source-commit",
                    "0" * 40,
                    "--builder-revision",
                    "unknown",
                    "--os-suite",
                    "trixie",
                    "--kernel-package",
                    "linux-image-rpi-v8",
                    "--package-manifest",
                    "/usr/share/prusa-buddy3d-camera/packages.txt",
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            doc = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                doc["package_manifest"],
                "/usr/share/prusa-buddy3d-camera/packages.txt",
            )

    def _run_build_info(self, output, *extra, env=None):
        cmd = [
            "python3",
            str(ASSETS / "build-info.py"),
            "--source-commit",
            "0" * 40,
            "--builder-revision",
            "unknown",
            "--os-suite",
            "trixie",
            "--kernel-package",
            "linux-image-rpi-v8",
            *extra,
            "--output",
            str(output),
        ]
        return subprocess.run(cmd, capture_output=True, text=True, env=env)

    def test_build_info_generator_records_explicit_version(self):
        # WP-R2: build-info carries the application version app_version.py reads.
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "build-info.json"
            result = self._run_build_info(output, "--version", "9.9.9")
            self.assertEqual(result.returncode, 0, result.stderr)
            doc = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(doc["version"], "9.9.9")

    def test_build_info_generator_version_defaults(self):
        env = {k: v for k, v in os.environ.items() if k != "PRUSA_IMAGE_VERSION"}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "build-info.json"
            result = self._run_build_info(output, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            doc = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(doc["version"], "0.0.0+local")

    def test_build_info_generator_version_from_env(self):
        env = dict(os.environ, PRUSA_IMAGE_VERSION="4.5.6")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "build-info.json"
            result = self._run_build_info(output, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            doc = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(doc["version"], "4.5.6")

    def test_build_info_generator_records_python_lock_sha256(self):
        # WP-R3/AC-14: the lock digest is recorded in build-info.json.
        digest = "a" * 64
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "build-info.json"
            result = self._run_build_info(
                output, "--python-lock-sha256", digest
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            doc = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(doc["python_lock_sha256"], digest)

    def test_build_info_generator_python_lock_defaults_null(self):
        # Existing callers that omit the flag still work: the field is null.
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "build-info.json"
            result = self._run_build_info(output)
            self.assertEqual(result.returncode, 0, result.stderr)
            doc = json.loads(output.read_text(encoding="utf-8"))
            self.assertIsNone(doc.get("python_lock_sha256"))

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

    # --- WP-R4b / AC-29: signed application updates -------------------------

    def test_layer_installs_minisign_and_zstd(self):
        doc = yaml.safe_load(read_text(LAYER_DIR / "buddy3d-image.yaml"))
        packages = set(doc["mmdebstrap"]["packages"])
        for name in ("minisign", "zstd"):
            self.assertIn(name, packages, name)

    def test_installer_embeds_release_public_key(self):
        text = read_text(ASSETS / "install-factory-app.sh")
        # The committed public key is copied root:root 0644 to the documented
        # in-image path the updater trusts (never a private key).
        self.assertIn("image/keys/buddy3d-release.pub", text)
        self.assertIn(
            "/usr/share/prusa-buddy3d-camera/buddy3d-release.pub", text
        )
        self.assertIn("-o root -g root -m 0644", text)
        self.assertNotIn("release-signing.key", text)

    def test_release_public_key_is_public_material(self):
        key = REPO_ROOT / "image" / "keys" / "buddy3d-release.pub"
        self.assertTrue(key.is_file(), f"missing {key}")
        text = read_text(key)
        self.assertIn("minisign public key", text)
        self.assertNotIn("PRIVATE KEY", text)
        # No secret-looking assignment in the committed key file.
        self.assertIsNone(
            re.search(r"(?i)\b(token|password|secret|private[_-]?key)\s*[:=]\s*\S+", text)
        )

    def test_installer_installs_and_enables_updater_units(self):
        text = read_text(ASSETS / "install-factory-app.sh")
        self.assertIn("prusa-updater.service prusa-updater.timer", text)
        enable_block = text.split("systemctl enable", 1)[1].split("|| true", 1)[0]
        self.assertIn("prusa-updater.timer", enable_block)
        # The oneshot service is triggered by the timer, never enabled directly.
        self.assertNotIn("prusa-updater.service", enable_block)
        # WP-R4c: the install oneshot is installed but never enabled (triggered
        # only through the fixed-verb root helper).
        self.assertIn("prusa-updater-install.service", text)
        self.assertNotIn("prusa-updater-install.service", enable_block)

    def test_install_unit_is_oneshot_gated_and_never_enabled(self):
        unit = parse_unit(REPO_SYSTEMD / "prusa-updater-install.service")
        section = unit["Unit"]
        self.assertIn("data-ready.target", section["Requires"].split())
        self.assertIn("data-ready.target", section["After"].split())
        service = unit["Service"]
        self.assertEqual(service["Type"], "oneshot")
        self.assertEqual(service["User"], "root")
        self.assertIn("updater_install.py install", service["ExecStart"])
        self.assertIn("updater_install.py recover", service["ExecStartPre"])
        self.assertIn("PATH=", service["Environment"])
        # Triggered only via ``prusa-priv install-update``: no [Install] section
        # and never enabled at multi-user.target.
        self.assertNotIn("Install", unit)

    def test_helper_routes_install_update_to_the_install_unit(self):
        text = read_text(PRUSA_PRIV)
        self.assertIn("install-update)", text)
        self.assertIn(
            'exec "$SYSTEMCTL" start prusa-updater-install.service', text
        )

    def test_updater_units_are_reused_and_valid(self):
        service = parse_unit(REPO_SYSTEMD / "prusa-updater.service")
        unit_section = service["Unit"]
        self.assertIn("data-ready.target", unit_section["Requires"].split())
        self.assertEqual(service["Service"]["Type"], "oneshot")
        self.assertEqual(service["Service"]["User"], "root")
        self.assertIn("updater_install.py", service["Service"]["ExecStart"])
        self.assertIn(" check", service["Service"]["ExecStart"])
        # A documented fixed PATH so minisign/zstd resolve deterministically.
        self.assertIn("PATH=", service["Service"]["Environment"])
        self.assertNotIn("Install", service)

        timer = parse_unit(REPO_SYSTEMD / "prusa-updater.timer")
        self.assertEqual(timer["Timer"]["Unit"], "prusa-updater.service")
        self.assertIn("24h", timer["Timer"]["OnUnitActiveSec"])
        self.assertIn(
            "multi-user.target", timer["Install"]["WantedBy"].split()
        )

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
