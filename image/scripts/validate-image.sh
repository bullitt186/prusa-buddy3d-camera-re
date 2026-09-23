#!/usr/bin/env bash
# Offline validator for the Buddy3D appliance image (AC-13).
#
# Usage:
#   validate-image.sh --image <file> [--mount-root <dir>] [--root-image <root.ext4>]
#                     [--boot-image <boot.vfat>] [--persist-image <persist.ext4>]
#                     [--manifest <os-list.json>] [--compressed-image <file>]
#                     [--disksig <0x...>] [--strict]
#
# Three independently testable groups:
#
#   A. Partition-table checks (no root): `sfdisk --json` reads the image file
#      directly and asserts the documented MBR layout from
#      image/layer/genimage.cfg.in.ext4 / image/config/buddy3d-pi-zero2w.yaml:
#      exactly 3 partitions BOOT (0xC, bootable) + ROOT (0x83) + PERSIST
#      (0x83, last), sizes within an 8 MiB alignment tolerance of
#      512 MiB / 4 GiB / 512 MiB, and (with --disksig) the configured MBR disk
#      signature.
#
#   B. Rootfs checks (no mount): run when --mount-root points at a directory
#      tree that represents the mounted ROOT filesystem. The tests use a
#      synthetic tree so no mount or root is required. Checks cover required
#      units and ordering, absence of personal usernames/home paths,
#      SSH/password login disabled, overlayroot.conf, volatile journald,
#      NetworkManager, the factory app and launcher fallback, build-info.json,
#      the embedded release-signing public key and updater units,
#      forbidden secret/identity artifacts, and that the ROOT /data mount point
#      is empty (no build-time identity).
#
#   B2. Partition payloads (no mount): genimage MOVES the rootfs /boot/firmware
#      contents onto the BOOT (vfat) partition and the /data contents onto the
#      PERSIST (ext4) partition, leaving EMPTY mount-point directories behind in
#      ROOT. Those payloads therefore cannot be read through --mount-root and
#      must be read from the partition images directly:
#        --boot-image <boot.vfat>     read cmdline.txt via mtools (mtype/mdir)
#                                     and assert overlayroot= is set;
#        --persist-image <persist.ext4> list the seeded /data layout via
#                                     debugfs and assert the prusa-cam uid/gid.
#      When either image is absent the corresponding checks SKIP (they are
#      optional inputs; see --strict below).
#
#   C. ROOT utilisation and release-manifest consistency: dumpe2fs for
#      --root-image, a clearly-labelled du estimate for --mount-root, and the
#      Imager manifest hash/size cross-check for --manifest.
#
# --strict means: every check that CAN run with the provided inputs must pass.
# It does NOT fail merely because an optional input (--manifest, --boot-image,
# --persist-image) was not supplied — those checks are reported as SKIPPED and
# do not affect the exit status, even under --strict. Checks skipped because a
# required input is missing, or because a tool needed by a check that could
# otherwise run is unavailable, still count as strict failures. Without
# --strict, skipped checks print a clear "SKIPPED" line and never affect the
# exit status.
#
# This script never mounts anything, never needs root, and never writes to the
# image. Exit status is nonzero if any check fails (or a check is skipped under
# --strict).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REPO_UNITS="$REPO_ROOT/pi-impersonator/systemd"

ROOT_CAPACITY_BYTES=$(( 4 * 1024 * 1024 * 1024 ))
ROOT_UTIL_LIMIT=75
SIZE_TOLERANCE_MIB=8

IMAGE=""
MOUNT_ROOT=""
ROOT_IMAGE=""
BOOT_IMAGE=""
PERSIST_IMAGE=""
MANIFEST=""
COMPRESSED_IMAGE=""
DISKSIG=""
STRICT=0

PASS=0
FAIL=0
SKIP=0
OPTIONAL_SKIP=0
WARN=0

usage() {
   cat <<'EOF'
Offline Buddy3D appliance image validator (AC-13).

Options:
  --image <file>             uncompressed or .xz image to inspect (required)
  --mount-root <dir>         directory tree representing the mounted ROOT
                             filesystem; enables the rootfs checks
  --root-image <root.ext4>   ROOT filesystem image for the authoritative
                             dumpe2fs utilisation check
  --boot-image <boot.vfat>   BOOT (vfat) partition image; reads cmdline.txt via
                             mtools (no mount) and asserts overlayroot=
  --persist-image <persist.ext4>
                             PERSIST (ext4) partition image; lists the seeded
                             /data layout via debugfs (no mount)
  --manifest <os-list.json>  Raspberry Pi Imager manifest to cross-check
  --compressed-image <file>  override the auto-located compressed artifact
  --disksig <0x...>          expected MBR disk signature
  --strict                   fail if any runnable check is skipped (optional
                             inputs that are simply not supplied do not fail)
  -h, --help                 show this help
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }

while [ $# -gt 0 ]; do
   case "$1" in
      --image)            [ $# -ge 2 ] || die "--image needs a value"; IMAGE="$2"; shift 2 ;;
      --mount-root)       [ $# -ge 2 ] || die "--mount-root needs a value"; MOUNT_ROOT="$2"; shift 2 ;;
      --root-image)       [ $# -ge 2 ] || die "--root-image needs a value"; ROOT_IMAGE="$2"; shift 2 ;;
      --boot-image)       [ $# -ge 2 ] || die "--boot-image needs a value"; BOOT_IMAGE="$2"; shift 2 ;;
      --persist-image)    [ $# -ge 2 ] || die "--persist-image needs a value"; PERSIST_IMAGE="$2"; shift 2 ;;
      --manifest)         [ $# -ge 2 ] || die "--manifest needs a value"; MANIFEST="$2"; shift 2 ;;
      --compressed-image) [ $# -ge 2 ] || die "--compressed-image needs a value"; COMPRESSED_IMAGE="$2"; shift 2 ;;
      --disksig)          [ $# -ge 2 ] || die "--disksig needs a value"; DISKSIG="$2"; shift 2 ;;
      --strict)           STRICT=1; shift ;;
      -h|--help)          usage; exit 0 ;;
      *)                  usage >&2; die "unknown argument: $1" ;;
   esac
done

[ -n "$IMAGE" ] || { usage >&2; die "--image is required"; }
[ -f "$IMAGE" ] || die "image not found: $IMAGE"
if [ -n "$MOUNT_ROOT" ]; then
   [ -d "$MOUNT_ROOT" ] || die "mount root not found: $MOUNT_ROOT"
fi
if [ -n "$BOOT_IMAGE" ]; then
   [ -f "$BOOT_IMAGE" ] || die "boot image not found: $BOOT_IMAGE"
fi
if [ -n "$PERSIST_IMAGE" ]; then
   [ -f "$PERSIST_IMAGE" ] || die "persist image not found: $PERSIST_IMAGE"
fi

TMPDIR_VALIDATE="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_VALIDATE"' EXIT

section() { printf '\n== %s ==\n' "$1"; }

report() {
   case "$1" in
      ok)    PASS=$(( PASS + 1 )); printf '[PASS]    %s\n' "$2" ;;
      fail)  FAIL=$(( FAIL + 1 )); printf '[FAIL]    %s\n' "$2" ;;
      skip)  SKIP=$(( SKIP + 1 )); printf '[SKIPPED] %s\n' "$2" ;;
      # An optional input (--manifest/--boot-image/--persist-image) was not
      # supplied. Recorded separately so --strict does not treat the absence of
      # an optional input as a failure; the check simply could not run.
      oskip) SKIP=$(( SKIP + 1 )); OPTIONAL_SKIP=$(( OPTIONAL_SKIP + 1 )); printf '[SKIPPED] %s\n' "$2" ;;
      warn)  WARN=$(( WARN + 1 )); printf '[WARN]    %s\n' "$2" ;;
      *)     printf '[????]    %s\n' "$2" ;;
   esac
}

# Consume "kind|message" lines from a process substitution (kept in the
# current shell so the counters are not lost to a pipeline subshell).
consume() {
   local kind msg
   while IFS='|' read -r kind msg; do
      [ -n "${kind:-}" ] || continue
      report "$kind" "$msg"
   done
}

###############################################################################
# A. Partition table
###############################################################################
section "A. Partition table (AC-10)"

if ! command -v sfdisk >/dev/null 2>&1; then
   report skip "partition-table checks (sfdisk not available)"
else
   sfdisk_json="$TMPDIR_VALIDATE/sfdisk.json"
   if sfdisk --json "$IMAGE" > "$sfdisk_json" 2>"$TMPDIR_VALIDATE/sfdisk.err" \
      && [ -s "$sfdisk_json" ]; then
      consume < <(python3 - "$sfdisk_json" "$DISKSIG" "$SIZE_TOLERANCE_MIB" <<'PY'
import json
import sys

path, disksig, tolerance_mib = sys.argv[1], sys.argv[2], int(sys.argv[3])
TOL = tolerance_mib * 1024 * 1024
EXPECTED = [
    ("BOOT", 512, "c", True),
    ("ROOT", 4096, "83", False),
    ("PERSIST", 512, "83", False),
]


def out(kind, msg):
    print(f"{kind}|{msg}")


def norm_type(value):
    value = str(value).lower()
    if value.startswith("0x"):
        value = value[2:]
    value = value.lstrip("0")
    return value or "0"


try:
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
except Exception as exc:  # noqa: BLE001 - report, never raise
    out("fail", f"cannot parse sfdisk JSON: {exc}")
    sys.exit(0)

table = doc.get("partitiontable") or {}
label = str(table.get("label", "")).lower()
sectorsize = int(table.get("sectorsize") or 512)
parts = table.get("partitions") or []

if label == "dos":
    out("ok", "partition table is MBR/dos")
else:
    out("fail", f"partition table is MBR/dos (found {label or 'none'!r})")

if len(parts) != 3:
    out("fail", f"exactly 3 partitions, no foreign entries (found {len(parts)})")
    sys.exit(0)
out("ok", "exactly 3 partitions, no foreign entries")

types = [norm_type(p.get("type", "")) for p in parts]
boots = [bool(p.get("bootable", False)) for p in parts]
order_ok = types == ["c", "83", "83"] and boots == [True, False, False]
out(
    "ok" if order_ok else "fail",
    "partition order BOOT(0xC,bootable), ROOT(0x83), PERSIST(0x83,last) "
    f"(found types={types} bootable={boots})",
)

for index, (name, mib, ptype, bootable) in enumerate(EXPECTED):
    part = parts[index]
    nominal = mib * 1024 * 1024
    size_bytes = int(part.get("size") or 0) * sectorsize
    size_ok = abs(size_bytes - nominal) <= TOL
    out(
        "ok" if size_ok else "fail",
        f"{name} size {size_bytes} B within {TOL} B of {nominal} B ({mib} MiB)",
    )
    type_ok = types[index] == ptype
    out(
        "ok" if type_ok else "fail",
        f"{name} partition type 0x{ptype.upper()} (found 0x{types[index].upper()})",
    )
    boot_ok = boots[index] == bootable
    out(
        "ok" if boot_ok else "fail",
        f"{name} bootable={bootable} (found {boots[index]})",
    )

if disksig:
    def normalize(value):
        value = str(value).lower()
        return value[2:] if value.startswith("0x") else value

    found = str(table.get("id", ""))
    sig_ok = normalize(found) == normalize(disksig)
    out("ok" if sig_ok else "fail", f"MBR disk signature {found or 'none'} matches {disksig}")
else:
    out("skip", "MBR disk signature (no --disksig given)")
PY
)
   else
      err="$(tr '\n' ' ' < "$TMPDIR_VALIDATE/sfdisk.err" 2>/dev/null || true)"
      report fail "cannot read an MBR partition table from $IMAGE: ${err:-sfdisk failed}"
   fi
fi

###############################################################################
# B. Rootfs checks
###############################################################################
section "B. Rootfs (AC-13)"

if [ -z "$MOUNT_ROOT" ]; then
   report skip "rootfs checks (not mounted; pass --mount-root)"
else
   SYSTEMD_DIR="$MOUNT_ROOT/etc/systemd/system"

   # --- required units installed ------------------------------------------
   required_units=()
   declare -A seen_units=()
   add_unit() {
      [ -n "${seen_units[$1]:-}" ] && return 0
      seen_units[$1]=1
      required_units+=("$1")
   }
   for unit in \
      rpicam-source.service prusa-rtsp.service prusa-ha-rtsp.service \
      prusa-cam.service pi-persist.service prusa-data-ready.service \
      data-ready.target bootlog.service prusa-data-grow.service \
      prusa-camera.target prusa-boot-mode.service \
      prusa-updater.service prusa-updater.timer \
      prusa-updater-install.service; do
      add_unit "$unit"
   done
   if [ -d "$REPO_UNITS" ]; then
      while IFS= read -r found; do
         add_unit "$(basename "$found")"
      done < <(find "$REPO_UNITS" -maxdepth 1 -type f \
         \( -name '*.service' -o -name '*.target' \) 2>/dev/null | sort)
   fi

   missing_units=()
   for unit in "${required_units[@]}"; do
      [ -f "$SYSTEMD_DIR/$unit" ] || missing_units+=("$unit")
   done
   if [ "${#missing_units[@]}" -eq 0 ]; then
      report ok "all required units installed under /etc/systemd/system (${#required_units[@]})"
   else
      report fail "missing required units: ${missing_units[*]}"
   fi

   # --- ordering ----------------------------------------------------------
   unit_values() {
      sed -n "s/^[[:space:]]*${2}[[:space:]]*=[[:space:]]*//p" "$1" 2>/dev/null || true
   }
   unit_has() {
      unit_values "$1" "$2" | tr ' ' '\n' | grep -qxF "$3"
   }

   for unit in rpicam-source.service prusa-cam.service prusa-rtsp.service prusa-ha-rtsp.service; do
      file="$SYSTEMD_DIR/$unit"
      [ -f "$file" ] || continue
      if unit_has "$file" After data-ready.target && unit_has "$file" Requires data-ready.target; then
         report ok "$unit is After= and Requires= data-ready.target"
      else
         report fail "$unit must be After= and Requires= data-ready.target"
      fi
   done

   persist_file="$SYSTEMD_DIR/pi-persist.service"
   if [ -f "$persist_file" ]; then
      persist_ok=1
      for token in data-ready.target rpicam-source.service prusa-cam.service \
                   prusa-rtsp.service prusa-ha-rtsp.service; do
         unit_has "$persist_file" Before "$token" || persist_ok=0
      done
      if [ "$persist_ok" = 1 ]; then
         report ok "pi-persist.service is Before= the data-ready gate and camera units"
      else
         report fail "pi-persist.service must be Before= data-ready.target and the camera units"
      fi
   fi

   grow_file="$SYSTEMD_DIR/prusa-data-grow.service"
   if [ -f "$grow_file" ]; then
      if unit_has "$grow_file" Before data-ready.target; then
         report ok "prusa-data-grow.service is Before= data-ready.target"
      else
         report fail "prusa-data-grow.service must be Before= data-ready.target"
      fi
   fi

    target_file="$SYSTEMD_DIR/prusa-camera.target"
    if [ -f "$target_file" ]; then
       if unit_has "$target_file" After data-ready.target \
          && unit_has "$target_file" Requires data-ready.target; then
          report ok "prusa-camera.target is After= and Requires= data-ready.target"
       else
          report fail "prusa-camera.target must be After= and Requires= data-ready.target"
       fi
    fi

    # --- boot-mode gating: unclaimed must not start the camera (AC-12/AC-17) -
    # prusa-boot-mode.service is the only enabled runtime selector; it starts
    # prusa-provisioning.service while unclaimed and prusa-camera.target after
    # claim. Enabling the camera units or the target directly would start the
    # camera pipeline before the device is claimed.
    boot_unit="$SYSTEMD_DIR/prusa-boot-mode.service"
    if [ -f "$boot_unit" ]; then
       report ok "prusa-boot-mode.service is installed"
       if grep -q 'boot_mode.py' "$boot_unit"; then
          report ok "prusa-boot-mode.service runs boot_mode.py"
       else
          report fail "prusa-boot-mode.service must run boot_mode.py"
       fi
       if unit_has "$boot_unit" After data-ready.target; then
          report ok "prusa-boot-mode.service is After= data-ready.target"
       else
          report fail "prusa-boot-mode.service must be After= data-ready.target"
       fi
    else
       report fail "prusa-boot-mode.service is missing"
    fi

    wants_dir="$SYSTEMD_DIR/multi-user.target.wants"
    camera_wants_dir="$SYSTEMD_DIR/prusa-camera.target.wants"

    if [ -L "$wants_dir/prusa-boot-mode.service" ]; then
       report ok "prusa-boot-mode.service is enabled at multi-user.target"
    else
       report fail "prusa-boot-mode.service must be enabled at multi-user.target"
    fi

    if [ -f "$SYSTEMD_DIR/prusa-provisioning.service" ]; then
       report ok "prusa-provisioning.service is installed"
       if unit_has "$SYSTEMD_DIR/prusa-provisioning.service" Conflicts prusa-camera.target; then
          report ok "prusa-provisioning.service Conflicts= prusa-camera.target"
       else
          report fail "prusa-provisioning.service must Conflicts= prusa-camera.target"
       fi
    else
       report fail "prusa-provisioning.service is missing"
    fi
    if [ -L "$wants_dir/prusa-provisioning.service" ]; then
       report fail "prusa-provisioning.service must NOT be enabled (started by boot_mode)"
    else
       report ok "prusa-provisioning.service is not enabled"
    fi

    if [ -L "$wants_dir/prusa-camera.target" ]; then
       report fail "prusa-camera.target must NOT be enabled (started by boot_mode)"
    else
       report ok "prusa-camera.target is not enabled"
    fi

    # WP-R4b: the updater timer is enabled at multi-user.target; its oneshot
    # service is installed and triggered by the timer (no [Install] section), so
    # it must NOT be separately enabled. The updater must never be Requires=
    # pulled into camera startup (AC-27 isolation).
    if [ -L "$wants_dir/prusa-updater.timer" ]; then
       report ok "prusa-updater.timer is enabled at multi-user.target"
    else
       report fail "prusa-updater.timer must be enabled at multi-user.target"
    fi
    if [ -L "$wants_dir/prusa-updater.service" ]; then
       report fail "prusa-updater.service must not be enabled (triggered by the timer)"
    else
       report ok "prusa-updater.service is not separately enabled"
    fi
    if [ -f "$SYSTEMD_DIR/prusa-updater.service" ]; then
       if grep -q 'updater_install.py' "$SYSTEMD_DIR/prusa-updater.service"; then
          report ok "prusa-updater.service runs updater_install.py"
       else
          report fail "prusa-updater.service must run updater_install.py"
       fi
       if unit_has "$SYSTEMD_DIR/prusa-updater.service" Requires data-ready.target; then
          report ok "prusa-updater.service Requires= data-ready.target"
       else
          report fail "prusa-updater.service must Require= data-ready.target"
       fi
       # AC-32: the trust anchor is fixed and the manifest source must not come
       # from a service-writable path. The updater reads /etc/prusa-updater.conf
       # (root:root 0644); /etc/prusa-cam is writable by prusa-cam and is never a
       # legitimate EnvironmentFile.
       if grep -q 'PRUSA_UPDATE_PUBLIC_KEY' "$SYSTEMD_DIR/prusa-updater.service"; then
          report fail "prusa-updater.service must not allow a public-key env override"
       else
          report ok "prusa-updater.service has no public-key env override"
       fi
       if grep -Eq 'EnvironmentFile=.*(/etc/prusa-cam/|updater\.env)' \
             "$SYSTEMD_DIR/prusa-updater.service"; then
          report fail "prusa-updater.service must not read a service-writable EnvironmentFile"
       else
          report ok "prusa-updater.service has no service-writable EnvironmentFile"
       fi
       if grep -q 'updater_install.py recover' "$SYSTEMD_DIR/prusa-updater.service"; then
          report ok "prusa-updater.service recovers interrupted state before check"
       else
          report fail "prusa-updater.service must run 'updater_install.py recover' before check"
       fi
    else
       report fail "prusa-updater.service is missing"
    fi
    if [ -f "$SYSTEMD_DIR/prusa-updater.timer" ]; then
       if unit_has "$SYSTEMD_DIR/prusa-updater.timer" Unit prusa-updater.service; then
          report ok "prusa-updater.timer triggers prusa-updater.service"
       else
          report fail "prusa-updater.timer must trigger prusa-updater.service"
       fi
    else
       report fail "prusa-updater.timer is missing"
    fi

    # WP-R4c/AC-31: the install oneshot runs the full signed install, is gated on
    # data-ready.target, and is triggered ONLY via the fixed-verb helper
    # (``prusa-priv install-update``). It must never be enabled: no [Install]
    # section and no enable symlink at multi-user.target or prusa-camera.target.
    install_unit="$SYSTEMD_DIR/prusa-updater-install.service"
    if [ -f "$install_unit" ]; then
       report ok "prusa-updater-install.service is installed"
       if grep -q 'updater_install.py install' "$install_unit"; then
          report ok "prusa-updater-install.service runs updater_install.py install"
       else
          report fail "prusa-updater-install.service must run 'updater_install.py install'"
       fi
       if grep -q 'updater_install.py recover' "$install_unit"; then
          report ok "prusa-updater-install.service recovers interrupted state before install"
       else
          report fail "prusa-updater-install.service must run 'updater_install.py recover' before install"
       fi
       if unit_has "$install_unit" Requires data-ready.target \
          && unit_has "$install_unit" After data-ready.target; then
          report ok "prusa-updater-install.service is After= and Requires= data-ready.target"
       else
          report fail "prusa-updater-install.service must be After= and Requires= data-ready.target"
       fi
       if [ -L "$wants_dir/prusa-updater-install.service" ] \
          || [ -L "$camera_wants_dir/prusa-updater-install.service" ]; then
          report fail "prusa-updater-install.service must not be enabled (triggered only via the helper)"
       else
          report ok "prusa-updater-install.service is not enabled"
       fi
       if [ -f "$target_file" ]; then
          if unit_has "$target_file" Wants prusa-updater-install.service \
             || unit_has "$target_file" Requires prusa-updater-install.service; then
             report fail "prusa-camera.target must not pull prusa-updater-install.service"
          else
             report ok "prusa-camera.target does not pull prusa-updater-install.service"
          fi
       fi
       # AC-32, same standard as prusa-updater.service: no public-key env
       # override and no service-writable EnvironmentFile (the root-owned
       # /etc/prusa-updater.conf is the only config source).
       if grep -q 'PRUSA_UPDATE_PUBLIC_KEY' "$install_unit"; then
          report fail "prusa-updater-install.service must not allow a public-key env override"
       else
          report ok "prusa-updater-install.service has no public-key env override"
       fi
       if grep -q 'EnvironmentFile=.*/etc/prusa-cam' "$install_unit"; then
          report fail "prusa-updater-install.service must not read a service-writable EnvironmentFile"
       else
          report ok "prusa-updater-install.service has no service-writable EnvironmentFile"
       fi
    else
       report fail "prusa-updater-install.service is missing"
    fi

    # prusa-admin.service is bound to the camera runtime, never to
    # multi-user.target: it is either enabled under prusa-camera.target.wants or
    # (because the installer does not enable it directly) carries
    # [Install] WantedBy=prusa-camera.target. Either way it starts only after
    # claim, when prusa-camera.target is selected.
    admin_unit="$SYSTEMD_DIR/prusa-admin.service"
    if [ -L "$wants_dir/prusa-admin.service" ]; then
       report fail "prusa-admin.service must not be enabled at multi-user.target"
    elif [ -L "$camera_wants_dir/prusa-admin.service" ] \
         || unit_has "$admin_unit" WantedBy prusa-camera.target; then
       report ok "prusa-admin.service is enabled under prusa-camera.target.wants"
    else
       report fail "prusa-admin.service must be enabled under prusa-camera.target.wants"
    fi

    # H3: the admin UI must be pulled by the camera target directly (not only
    # via its own [Install] section), so it starts exactly when the camera
    # runtime is selected after claim.
    if [ -f "$target_file" ] && unit_has "$target_file" Wants prusa-admin.service; then
       report ok "prusa-camera.target Wants= prusa-admin.service"
    else
       report fail "prusa-camera.target must Wants= prusa-admin.service"
    fi

    # WP-R4b/AC-27: the updater timer is optional and isolated. The camera
    # target Wants= it (available after claim) but must never Requires= it, so
    # an update failure cannot stop camera startup.
    if [ -f "$target_file" ] && unit_has "$target_file" Wants prusa-updater.timer; then
       report ok "prusa-camera.target Wants= prusa-updater.timer"
    else
       report fail "prusa-camera.target must Wants= prusa-updater.timer"
    fi
    if [ -f "$target_file" ] && unit_has "$target_file" Requires prusa-updater.timer; then
       report fail "prusa-camera.target must not Require= prusa-updater.timer"
    else
       report ok "prusa-camera.target does not Require= prusa-updater.timer"
    fi

    # --- privileged helper + sudoers (B3) ----------------------------------
    # A root helper must never execute code the service account can write:
    # /opt/prusa-cam stays root:root, the helper is root-owned and not
    # group/world-writable, and the sudoers rule is root:root 0440.
    passwd_file="$MOUNT_ROOT/etc/passwd"
    root_uid="$(awk -F: '$1=="root"{print $3; exit}' "$passwd_file" 2>/dev/null || true)"
    root_gid="$(awk -F: '$1=="root"{print $4; exit}' "$passwd_file" 2>/dev/null || true)"
    prusa_uid="$(awk -F: '$1=="prusa-cam"{print $3; exit}' "$passwd_file" 2>/dev/null || true)"
    prusa_gid="$(awk -F: '$1=="prusa-cam"{print $4; exit}' "$passwd_file" 2>/dev/null || true)"
    root_uid="${root_uid:-0}"
    root_gid="${root_gid:-0}"

    helper="$MOUNT_ROOT/usr/libexec/prusa-cam/prusa-priv"
    if [ -f "$helper" ]; then
       helper_owner="$(stat -c '%u:%g' "$helper" 2>/dev/null || true)"
       helper_mode="$(stat -c '%a' "$helper" 2>/dev/null || true)"
       if [ -n "$prusa_uid" ] && [ "$prusa_uid" != "$root_uid" ] \
          && [ "$helper_owner" = "$prusa_uid:$prusa_gid" ]; then
          report fail "prusa-priv must not be owned by prusa-cam ($helper_owner)"
       elif [ "$helper_owner" != "$root_uid:$root_gid" ]; then
          report fail "prusa-priv must be root:root (found ${helper_owner:-missing})"
       elif [ -n "$helper_mode" ] && [ $(( 0$helper_mode & 022 )) -ne 0 ]; then
          report fail "prusa-priv must not be group/world-writable (mode $helper_mode)"
       else
          report ok "prusa-priv is root:root and not group/world-writable"
       fi
    else
       report fail "prusa-priv helper is missing at /usr/libexec/prusa-cam/prusa-priv"
    fi

    sudoers="$MOUNT_ROOT/etc/sudoers.d/prusa-cam"
    if [ -f "$sudoers" ]; then
       sudoers_mode="$(stat -c '%a' "$sudoers" 2>/dev/null || true)"
       sudoers_owner="$(stat -c '%u:%g' "$sudoers" 2>/dev/null || true)"
       if [ "$sudoers_mode" = "440" ] && [ "$sudoers_owner" = "$root_uid:$root_gid" ]; then
          report ok "/etc/sudoers.d/prusa-cam is root:root mode 0440"
       else
          report fail "/etc/sudoers.d/prusa-cam must be root:root mode 0440 (found ${sudoers_owner:-?} ${sudoers_mode:-?})"
       fi
       # The privilege boundary must not depend on the base image's global sudo
       # defaults: pin env_reset (blocks PRUSA_CAM_APP_ROOT/PATH injection) and
       # secure_path, and grant only the fixed-verb helper.
       if grep -qE '^[[:space:]]*Defaults:prusa-cam[[:space:]]+env_reset([[:space:]]|$)' "$sudoers" \
          && grep -qE '^[[:space:]]*Defaults:prusa-cam[[:space:]]+secure_path=' "$sudoers" \
          && grep -qF '/usr/libexec/prusa-cam/prusa-priv' "$sudoers"; then
          report ok "sudoers pins env_reset/secure_path and only the fixed-verb helper"
       else
          report fail "sudoers must pin env_reset + secure_path and grant only /usr/libexec/prusa-cam/prusa-priv"
       fi
    else
       report fail "/etc/sudoers.d/prusa-cam is missing"
    fi

    app_root="$MOUNT_ROOT/opt/prusa-cam"
    if [ -d "$app_root" ]; then
       opt_owner="$(stat -c '%u:%g' "$app_root" 2>/dev/null || true)"
       opt_mode="$(stat -c '%a' "$app_root" 2>/dev/null || true)"
       if [ -n "$prusa_uid" ] && [ "$opt_owner" = "$prusa_uid:$prusa_gid" ]; then
          report fail "/opt/prusa-cam must not be owned by prusa-cam ($opt_owner)"
       elif [ -n "$opt_mode" ] && [ $(( 0$opt_mode & 022 )) -ne 0 ]; then
          report fail "/opt/prusa-cam must not be group/world-writable (mode $opt_mode)"
       else
          report ok "/opt/prusa-cam is root-owned and not group/world-writable"
       fi
    fi

    # --- hash-locked Python runtime venv + dependency lock (WP-R3/AC-14) ---
    # The runtime units start through /opt/prusa-cam/launcher.sh (WP-R4c), which
    # resolves the per-release or factory venv; the factory venv is the
    # immutable fallback and is built by install-factory-app.sh from the
    # committed hash-locked requirements.lock. A venv-less image is not
    # runnable, so a missing venv or lock is a hard failure here.
    venv_dir="$app_root/venv"
    venv_python="$venv_dir/bin/python"
    if [ -f "$venv_python" ]; then
       # venv/bin/python is a symlink to the system interpreter; follow it so
       # the symlink's own 0777 mode is not mistaken for a writable interpreter.
       venv_owner="$(stat -Lc '%u:%g' "$venv_python" 2>/dev/null || true)"
       venv_mode="$(stat -Lc '%a' "$venv_python" 2>/dev/null || true)"
       if [ -n "$prusa_uid" ] && [ "$venv_owner" = "$prusa_uid:$prusa_gid" ]; then
          report fail "venv python must not be owned by prusa-cam ($venv_owner)"
       elif [ "$venv_owner" != "$root_uid:$root_gid" ]; then
          report fail "venv python must be root:root (found ${venv_owner:-missing})"
       elif [ -n "$venv_mode" ] && [ $(( 0$venv_mode & 022 )) -ne 0 ]; then
          report fail "venv python must not be group/world-writable (mode $venv_mode)"
       else
          report ok "/opt/prusa-cam/venv/bin/python is root-owned and not group/world-writable"
       fi
    else
       report fail "/opt/prusa-cam/venv/bin/python is missing (runtime venv not built)"
    fi

    lock_file="$app_root/requirements.lock"
    if [ -f "$lock_file" ]; then
       lock_owner="$(stat -c '%u:%g' "$lock_file" 2>/dev/null || true)"
       lock_mode="$(stat -c '%a' "$lock_file" 2>/dev/null || true)"
       if [ "$lock_mode" = "644" ] && [ "$lock_owner" = "$root_uid:$root_gid" ]; then
          report ok "/opt/prusa-cam/requirements.lock is root:root mode 0644"
       else
          report fail "/opt/prusa-cam/requirements.lock must be root:root mode 0644 (found ${lock_owner:-?} ${lock_mode:-?})"
       fi
       consume < <(python3 - "$lock_file" <<'PY'
import sys

path = sys.argv[1]


def out(kind, msg):
    print(f"{kind}|{msg}")


try:
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
except OSError as exc:  # noqa: BLE001 - report, never raise
    out("fail", f"requirements.lock unreadable: {exc}")
    sys.exit(0)

# Drop comment lines, then re-join backslash continuations so a pinned package
# and its --hash lines form one spec (the pip-compile output format).
lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
joined = []
buf = ""
for line in lines:
    if line.rstrip().endswith("\\"):
        buf += line.rstrip()[:-1] + " "
    else:
        buf += line
        joined.append(buf)
        buf = ""
if buf:
    joined.append(buf)

specs = [spec for spec in joined if "==" in spec and not spec.lstrip().startswith("-")]
if not specs:
    out("fail", "requirements.lock pins no packages")
    sys.exit(0)

unhashed = [
    spec.split("==", 1)[0].strip()
    for spec in specs
    if "--hash=sha256:" not in spec
]
if unhashed:
    out(
        "fail",
        "requirements.lock packages without a sha256 hash: "
        + ", ".join(unhashed),
    )
else:
    out(
        "ok",
        f"requirements.lock pins {len(specs)} packages, each with a sha256 hash",
    )

names = {spec.split("==", 1)[0].strip().lower() for spec in specs}
missing = [
    dep
    for dep in ("aiohttp", "python-socketio", "paho-mqtt")
    if dep not in names
]
if missing:
    out(
        "fail",
        "requirements.lock missing direct dependencies: " + ", ".join(missing),
    )
else:
    out("ok", "requirements.lock contains the aiohttp/python-socketio/paho-mqtt deps")
PY
)
    else
       report fail "/opt/prusa-cam/requirements.lock is missing"
    fi

    # The installed packages must actually be present in the venv (not just a
    # venv shell). Python-socketio installs the ``socketio`` module and
    # paho-mqtt the ``paho`` module; aiohttp is its own module.
    if [ -f "$venv_dir/pyvenv.cfg" ]; then
       report ok "/opt/prusa-cam/venv/pyvenv.cfg present"
    else
       report fail "/opt/prusa-cam/venv/pyvenv.cfg missing (not a virtualenv)"
    fi
    missing_modules=()
    for module in aiohttp socketio paho; do
       found=""
       for candidate in "$venv_dir"/lib/python3*/site-packages/"$module"; do
          if [ -e "$candidate" ]; then
             found="$candidate"
             break
          fi
       done
       [ -n "$found" ] || missing_modules+=("$module")
    done
    if [ "${#missing_modules[@]}" -eq 0 ]; then
       report ok "venv site-packages contains aiohttp, socketio and paho"
    else
       report fail "venv is missing installed modules: ${missing_modules[*]}"
    fi

    # --no-cache-dir must leave no wheel cache behind.
    wheels="$(find "$venv_dir" -name '*.whl' 2>/dev/null || true)"
    if [ -n "$wheels" ]; then
       report fail "pip wheel cache inside venv: $(printf '%s ' $wheels)"
    else
       report ok "no pip wheel cache inside venv"
    fi

    # The runtime deps come from the hash-locked requirements.lock only; an
    # unpinned requirements.txt would bypass it.
    req_txt="$app_root/requirements.txt"
    if [ -f "$req_txt" ]; then
       unpinned="$(grep -vE '^[[:space:]]*(#|--|$)' "$req_txt" | grep -vE '==' || true)"
       if [ -n "$unpinned" ]; then
          report fail "requirements.txt contains unpinned dependencies"
       else
          report ok "requirements.txt pins every dependency"
       fi
    else
       report ok "no unpinned requirements.txt"
    fi

    camera_leaks=()
    for unit in rpicam-source.service prusa-rtsp.service prusa-ha-rtsp.service \
                prusa-cam.service; do
       if [ -L "$wants_dir/$unit" ]; then
          camera_leaks+=("$unit")
       fi
    done
    if [ "${#camera_leaks[@]}" -eq 0 ]; then
       report ok "camera units are not enabled at multi-user.target (unclaimed safe)"
    else
       report fail "camera units enabled at multi-user.target: ${camera_leaks[*]}"
    fi

    # --- personal usernames / home paths / secret-looking config -----------
   consume < <(python3 - "$MOUNT_ROOT" <<'PY'
import os
import re
import sys

root = sys.argv[1]

ALLOWED_HOME_USERS = {"prusa-cam"}
ALLOWED_UNIT_USERS = {
    "root", "prusa-cam", "daemon", "bin", "sys", "sync", "games", "man", "lp",
    "mail", "news", "uucp", "proxy", "www-data", "backup", "list", "irc",
    "_apt", "nobody", "systemd-network", "systemd-timesync", "systemd-resolve",
    "messagebus", "sshd", "ntp", "nginx", "redis",
}
HOME_RE = re.compile("/" + "home/" + r"([A-Za-z0-9._-]+)")
USER_RE = re.compile(r"^[ \t]*(User|Group)[ \t]*=[ \t]*(\S+)[ \t]*$", re.MULTILINE)
SECRET_RE = re.compile(
    r"(?i)\b(token|password|passwd|psk|secret|api[_-]?key|private[_-]?key)\b"
    r"\s*[:=]\s*(.+)"
)
# Private-key *content* (a PEM/OpenSSH/OpenPGP armor header). Built from
# concatenated fragments so this detector's own source is not itself flagged by
# the image secret scanner (which forbids the literal armored header).
KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE" + " KEY-----")
CERT_RE = re.compile(r"-----BEGIN CERTIFICATE-----")
# Real private-key filenames. Deliberately narrow: broad *.pem/*.key globs flag
# public CA bundles (pip/_vendor/certifi/cacert.pem) and cert files. Assembled
# from fragments for the same self-scan reason as KEY_RE.
KEY_NAME_RE = re.compile(
    r"^(" + "id_" + r"(rsa|ed25519)|ssh_host_.*_key|minisign\.key)$"
)
# Library test vectors ship real-looking key blocks in their self-test data
# (e.g. dist-packages/Cryptodome/SelfTest/...). Those are fixtures, not
# release material, so they are excluded by path.
TEST_FIXTURE_RE = re.compile(r"(^|/)(dist-packages/[^/]+/SelfTest|tests)(/|$)")
PLACEHOLDER_RE = re.compile(
    r"^(<.*>|\$\{.*\}|\$[A-Za-z_][A-Za-z0-9_]*|\"\"|''|null|none|changeme|"
    r"change_me|replace_?me|placeholder|redacted)$",
    re.IGNORECASE,
)

HOME_SCAN = [
    "etc/systemd/system",
    "etc/prusa-cam",
    "etc/NetworkManager",
    "etc/fstab",
    "etc/overlayroot.conf",
    "etc/hostname",
    "etc/hosts",
    "opt/prusa-cam",
    "usr/share/prusa-buddy3d-camera",
]
SECRET_SCAN = [
    "etc/systemd/system",
    "etc/prusa-cam",
    "etc/NetworkManager",
    "etc/ssh",
    "data",
    "root",
    "home",
    "usr/share/prusa-buddy3d-camera",
]


def walk(relpaths):
    seen = set()
    for rel in relpaths:
        path = os.path.join(root, rel)
        if os.path.isfile(path):
            yield path, rel
        elif os.path.isdir(path):
            for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
                dirnames[:] = [
                    d for d in dirnames if d not in ("venv", "__pycache__", ".git")
                ]
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    if os.path.islink(full):
                        continue
                    try:
                        if os.path.getsize(full) > 1024 * 1024:
                            continue
                    except OSError:
                        continue
                    relpath = os.path.relpath(full, root)
                    if relpath in seen:
                        continue
                    seen.add(relpath)
                    yield full, relpath


def read_text(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (UnicodeDecodeError, OSError):
        return None


def out(kind, msg):
    print(f"{kind}|{msg}")


home_hits = []
user_hits = []
secret_hits = []
key_hits = []

for path, rel in walk(HOME_SCAN):
    text = read_text(path)
    if text is None:
        continue
    for match in HOME_RE.finditer(text):
        if match.group(1) not in ALLOWED_HOME_USERS:
            home_hits.append(f"{rel}:{match.group(0)}")
    for match in USER_RE.finditer(text):
        if match.group(2) not in ALLOWED_UNIT_USERS:
            user_hits.append(f"{rel}:{match.group(0).strip()}")

for path, rel in walk(SECRET_SCAN):
    text = read_text(path)
    if text is None:
        continue
    for match in SECRET_RE.finditer(text):
        value = match.group(2).strip().rstrip(";").strip()
        if value and not PLACEHOLDER_RE.match(value):
            secret_hits.append(f"{rel}:{match.group(1)}")

for path, rel in walk(["."]):
    base = os.path.basename(rel)
    # Library self-test fixtures (Cryptodome etc.) carry genuine-looking key
    # blocks as test vectors; they are not release material.
    if TEST_FIXTURE_RE.search(rel):
        continue
    # Flag by real private-key filename first (cheap), then by key *content*.
    if KEY_NAME_RE.match(base):
        key_hits.append(rel)
        continue
    text = read_text(path)
    if text is None:
        continue
    if KEY_RE.search(text):
        key_hits.append(rel)
    elif CERT_RE.search(text) and base.lower().endswith((".pem", ".crt")):
        # Public certificate/bundle (e.g. cacert.pem, *.crt): a CERTIFICATE
        # block with no PRIVATE KEY block is not private key material.
        continue


def summarize(ok_kind, fail_kind, message, hits):
    if hits:
        sample = ", ".join(hits[:5])
        extra = "" if len(hits) <= 5 else f" (+{len(hits) - 5} more)"
        out(fail_kind, f"{message}: {sample}{extra}")
    else:
        out(ok_kind, message)


summarize("ok", "fail", "no personal home directory references", home_hits)
summarize("ok", "fail", "no non-system User=/Group= accounts in units", user_hits)
summarize("ok", "fail", "no secret-looking values in configs", secret_hits)
summarize("ok", "fail", "no private key material", key_hits)
PY
)

   # --- forbidden identity / secret artifacts -----------------------------
   hostkeys="$(find "$MOUNT_ROOT/etc/ssh" -name 'ssh_host_*' 2>/dev/null || true)"
   if [ -n "$hostkeys" ]; then
      report fail "build-time SSH host key present: $(printf '%s ' $hostkeys)"
   else
      report ok "no build-time SSH host key"
   fi

   machine_id="$MOUNT_ROOT/etc/machine-id"
   if [ -s "$machine_id" ]; then
      report fail "non-empty /etc/machine-id (must be generated on first boot)"
   else
      report ok "no persistent /etc/machine-id"
   fi

   nmconnections="$(find "$MOUNT_ROOT" -name '*.nmconnection' 2>/dev/null || true)"
   if [ -n "$nmconnections" ]; then
      report fail "Wi-Fi/NetworkManager profile present: $(printf '%s ' $nmconnections)"
   else
      report ok "no Wi-Fi connection profile (*.nmconnection)"
   fi

   # Private-key material is detected by content and by real key filenames in
   # the Python scan above. The previous broad *.pem/*.key glob was removed: it
   # false-positived on public CA bundles (e.g. pip/_vendor/certifi/cacert.pem)
   # and library test vectors, which are not release secrets.

   # --- SSH and password login disabled by default ------------------------
   authkeys="$(find "$MOUNT_ROOT/root/.ssh" "$MOUNT_ROOT/home" -name 'authorized_keys' 2>/dev/null || true)"
   if [ -n "$authkeys" ]; then
      report fail "authorized_keys present: $(printf '%s ' $authkeys)"
   else
      report ok "no authorized_keys installed"
   fi

   shadow="$MOUNT_ROOT/etc/shadow"
   if [ -f "$shadow" ]; then
      root_field="$(awk -F: '$1=="root"{print $2; exit}' "$shadow" 2>/dev/null || true)"
      case "$root_field" in
         ''|'!'|'!!'|'*'|'!*') report ok "root account locked (no password set)" ;;
         *) report fail "root account has a password hash set" ;;
      esac
   else
      report skip "root password state (no /etc/shadow in tree)"
   fi

   sshd_config="$MOUNT_ROOT/etc/ssh/sshd_config"
   if [ -f "$sshd_config" ]; then
      password_auth="$(grep -iE '^[[:space:]]*PasswordAuthentication[[:space:]]+' "$sshd_config" 2>/dev/null | tail -1 | awk '{print tolower($2)}' || true)"
      root_login="$(grep -iE '^[[:space:]]*PermitRootLogin[[:space:]]+' "$sshd_config" 2>/dev/null | tail -1 | awk '{print tolower($2)}' || true)"
      if [ "${password_auth:-no}" = "yes" ]; then
         report fail "sshd PasswordAuthentication is enabled"
      else
         report ok "sshd password authentication disabled"
      fi
      if [ "${root_login:-no}" = "yes" ]; then
         report fail "sshd PermitRootLogin is enabled"
      else
         report ok "sshd root login not enabled"
      fi
   else
      report skip "sshd_config absent (password login check)"
   fi

   ssh_enabled=""
   for candidate in \
      "$MOUNT_ROOT/etc/systemd/system/multi-user.target.wants/ssh.service" \
      "$MOUNT_ROOT/etc/systemd/system/multi-user.target.wants/sshd.service" \
      "$MOUNT_ROOT/etc/systemd/system/sysinit.target.wants/ssh.service"; do
      if [ -e "$candidate" ]; then
         ssh_enabled="$candidate"
      fi
   done
   if [ -n "$ssh_enabled" ]; then
      report fail "ssh.service is enabled by default"
   else
      report ok "ssh.service is not enabled by default"
   fi

   # --- overlayroot -------------------------------------------------------
   # The persistent overlayroot config lives in ROOT /etc. The cmdline.txt
   # overlayroot= token is checked from the BOOT partition image in section B2:
   # genimage moves /boot/firmware contents onto the BOOT partition, so it is
   # not readable through --mount-root.
   overlay_conf="$MOUNT_ROOT/etc/overlayroot.conf"
   if [ -f "$overlay_conf" ] && grep -q 'overlayroot' "$overlay_conf"; then
      report ok "/etc/overlayroot.conf configures overlayroot"
   else
      report fail "/etc/overlayroot.conf missing or does not configure overlayroot"
   fi

   # --- volatile, size-limited journald -----------------------------------
   journald_files=()
   if [ -f "$MOUNT_ROOT/etc/systemd/journald.conf" ]; then
      journald_files+=("$MOUNT_ROOT/etc/systemd/journald.conf")
   fi
   while IFS= read -r found; do
      journald_files+=("$found")
   done < <(find "$MOUNT_ROOT/etc/systemd/journald.conf.d" -type f 2>/dev/null | sort)
   if [ "${#journald_files[@]}" -eq 0 ]; then
      report fail "journald configuration absent (expected Storage=volatile)"
   else
      journald_text="$(cat "${journald_files[@]}" 2>/dev/null || true)"
      if printf '%s\n' "$journald_text" | grep -qiE '^[[:space:]]*Storage[[:space:]]*=[[:space:]]*volatile'; then
         report ok "journald Storage=volatile"
      else
         report fail "journald is not configured Storage=volatile"
      fi
      if printf '%s\n' "$journald_text" | grep -qiE '^[[:space:]]*(SystemMaxUse|RuntimeMaxUse)[[:space:]]*='; then
         report ok "journald has a size limit"
      else
         report fail "journald has no size limit (SystemMaxUse/RuntimeMaxUse)"
      fi
   fi

   # --- NetworkManager ----------------------------------------------------
   nm_ok=0
   if [ -f "$MOUNT_ROOT/etc/NetworkManager/NetworkManager.conf" ]; then
      nm_ok=1
   fi
   if find "$MOUNT_ROOT/etc/systemd/system/NetworkManager.service.d" -name '*.conf' \
      -print -quit 2>/dev/null | grep -q .; then
      nm_ok=1
   fi
   if [ -f "$MOUNT_ROOT/usr/lib/systemd/system/NetworkManager.service" ]; then
      nm_ok=1
   fi
   if [ "$nm_ok" = 1 ]; then
      report ok "NetworkManager is present/configured"
   else
      report fail "NetworkManager configuration absent"
   fi

   # --- factory app + launcher fallback -----------------------------------
   if [ -f "$MOUNT_ROOT/opt/prusa-cam/main.py" ]; then
      report ok "factory application present under /opt/prusa-cam"
   else
      report fail "factory application entry point /opt/prusa-cam/main.py missing"
   fi

   launcher=""
   for candidate in launcher.sh run.sh bin/launcher.sh; do
      if [ -x "$MOUNT_ROOT/opt/prusa-cam/$candidate" ]; then
         launcher="$candidate"
         break
      fi
   done
   if [ -n "$launcher" ]; then
      report ok "release launcher fallback present (/opt/prusa-cam/$launcher)"
   else
      report fail "release launcher fallback missing (/opt/prusa-cam/launcher.sh)"
   fi

   # --- runtime units execute through the launcher (WP-R4c) ----------------
   # An installed signed release under DATA is only used if the runtime units
   # start through launcher.sh; a unit that execs the factory venv directly
   # would pin the appliance to the immutable factory code forever. Each runtime
   # unit must exec the launcher with its own entry point.
    declare -A launcher_script=(
       [prusa-cam.service]=main.py
       [prusa-rtsp.service]=rtsp_server.py
       [prusa-ha-rtsp.service]=rtsp_server.py
       [prusa-admin.service]=admin_app.py
    )
    for unit in prusa-cam.service prusa-rtsp.service prusa-ha-rtsp.service \
                prusa-admin.service; do
      file="$SYSTEMD_DIR/$unit"
      [ -f "$file" ] || continue
      want_script="${launcher_script[$unit]}"
      if unit_has "$file" ExecStart /opt/prusa-cam/launcher.sh \
         && unit_has "$file" ExecStart "$want_script"; then
         report ok "$unit execs /opt/prusa-cam/launcher.sh $want_script"
      else
         report fail "$unit must ExecStart=/opt/prusa-cam/launcher.sh $want_script"
      fi
   done

   # The launcher must prefer a complete per-release venv under DATA and fall
   # back to the factory venv; the runtime-unit assertions above depend on it.
   if [ -n "$launcher" ]; then
      launcher_path="$MOUNT_ROOT/opt/prusa-cam/$launcher"
      if grep -q 'current/venv/bin/python' "$launcher_path" \
         && grep -q 'APP_ROOT/venv/bin/python' "$launcher_path"; then
         report ok "launcher prefers the per-release venv with a factory fallback"
      else
         report fail "launcher must prefer the per-release venv and fall back to the factory venv"
      fi
   fi

   # --- build-info.json ---------------------------------------------------
   build_info="$MOUNT_ROOT/usr/share/prusa-buddy3d-camera/build-info.json"
   if [ -f "$build_info" ]; then
      consume < <(python3 - "$build_info" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        doc = json.load(handle)
except Exception as exc:  # noqa: BLE001 - report, never raise
    print(f"fail|build-info.json unreadable: {exc}")
    sys.exit(0)

required = ["source_commit", "builder_revision", "os_suite", "kernel_package", "package_manifest"]
missing = [key for key in required if not doc.get(key)]
if missing:
    print(f"fail|build-info.json missing/empty keys: {missing}")
else:
    print("ok|build-info.json records source commit, builder revision, OS suite, kernel package, package manifest")
PY
)
   else
      report fail "build-info.json missing at /usr/share/prusa-buddy3d-camera/build-info.json"
   fi

   # --- embedded release-signing public key (WP-R4b / AC-29) --------------
   # The updater trusts exactly the committed public key. It must be present,
   # root:root 0644, not service-account-owned, and public (no private key
   # material). The private key must never appear anywhere in the image; the
   # Python secret/key scan above asserts that.
   pubkey="$MOUNT_ROOT/usr/share/prusa-buddy3d-camera/buddy3d-release.pub"
   if [ -f "$pubkey" ]; then
      key_owner="$(stat -c '%u:%g' "$pubkey" 2>/dev/null || true)"
      key_mode="$(stat -c '%a' "$pubkey" 2>/dev/null || true)"
      if [ -n "$prusa_uid" ] && [ "$key_owner" = "$prusa_uid:$prusa_gid" ]; then
         report fail "buddy3d-release.pub must not be owned by prusa-cam ($key_owner)"
      elif [ "$key_owner" != "$root_uid:$root_gid" ]; then
         report fail "buddy3d-release.pub must be root:root (found ${key_owner:-missing})"
      elif [ "$key_mode" != "644" ]; then
         report fail "buddy3d-release.pub must be mode 0644 (found ${key_mode:-?})"
      else
         report ok "buddy3d-release.pub is root:root mode 0644"
      fi
      if grep -q 'PRIVATE KEY' "$pubkey"; then
         report fail "buddy3d-release.pub contains private key material"
      else
         report ok "buddy3d-release.pub contains no private key material"
      fi
   else
      report fail "buddy3d-release.pub is missing at /usr/share/prusa-buddy3d-camera/"
   fi

   # --- ROOT /data is an empty mount point, no build-time identity --------
   # genimage MOVES the seeded /data contents onto the PERSIST partition, so
   # ROOT's /data is an empty mount point that must carry no build-time device
   # identity or secret. The seeded layout itself is asserted from
   # --persist-image in section B2 (it is not readable here).
   data_root="$MOUNT_ROOT/data"
   identity_hits="$(find "$data_root" \( -name 'identity.json' -o -name 'secrets.toml' \) 2>/dev/null || true)"
   if [ -n "$identity_hits" ]; then
      report fail "build-time device identity/secret in /data: $(printf '%s ' $identity_hits)"
   else
      report ok "no build-time device identity in /data (identity.json/secrets.toml)"
   fi
fi

###############################################################################
# B2. Partition payloads
###############################################################################
section "B2. Partition payloads (AC-13)"

# --- BOOT cmdline.txt -------------------------------------------------------
# Read cmdline.txt from the FAT BOOT partition without mounting or root. mtools
# is preferred (mtype reads file contents). When --boot-image is not supplied
# the check is optional and SKIPPED, never failed.
if [ -z "$BOOT_IMAGE" ]; then
   report oskip "BOOT cmdline overlayroot= (no --boot-image)"
elif command -v mtype >/dev/null 2>&1; then
   if cmdline_text="$(mtype -i "$BOOT_IMAGE" ::/cmdline.txt 2>/dev/null)"; then
      if printf '%s\n' "$cmdline_text" | grep -q 'overlayroot='; then
         report ok "BOOT cmdline sets overlayroot="
      else
         report fail "BOOT cmdline does not set overlayroot="
      fi
   else
      report fail "cannot read cmdline.txt from BOOT image: $BOOT_IMAGE"
   fi
elif command -v mdir >/dev/null 2>&1; then
   # mdir can list the FAT root but cannot read file contents; use it only to
   # confirm cmdline.txt is present, then skip the content assertion clearly.
   if mdir -i "$BOOT_IMAGE" ::/ 2>/dev/null | grep -qi 'cmdline'; then
      report skip "BOOT cmdline overlayroot= (mtype unavailable; install mtools)"
   else
      report fail "BOOT cmdline.txt is absent from $BOOT_IMAGE"
   fi
else
   report skip "BOOT cmdline overlayroot= (mtools not available; install mtools)"
fi

# --- PERSIST seeded layout --------------------------------------------------
# List the ext4 PERSIST partition with debugfs (no mount, no root) and assert
# the seeded /data layout from image/layer/setup.sh plus its prusa-cam uid/gid.
# Optional input: SKIPPED when --persist-image is absent.
if [ -z "$PERSIST_IMAGE" ]; then
   report oskip "PERSIST seeded layout (no --persist-image)"
elif ! command -v debugfs >/dev/null 2>&1; then
   report skip "PERSIST seeded layout (debugfs not available; install e2fsprogs)"
else
   consume < <(python3 - "$PERSIST_IMAGE" "$MOUNT_ROOT" <<'PY'
import os
import subprocess
import sys

persist_image, mount_root = sys.argv[1], sys.argv[2]

# (parent directory inside the image, child name) for every seeded path.
REQUIRED = [
    ("/", "prusa-cam"),
    ("/prusa-cam", "config"),
    ("/prusa-cam", "releases"),
    ("/prusa-cam", "backups"),
    ("/", "network"),
    ("/network", "system-connections"),
    ("/", "sdcard"),
    ("/sdcard", "timelapse"),
]


def out(kind, msg):
    print(f"{kind}|{msg}")


def list_dir(directory):
    """Return {name: (uid, gid, is_dir)} for a directory, or None on error."""
    result = subprocess.run(
        ["debugfs", "-R", f"ls -l {directory}", persist_image],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    entries = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        # inode mode links uid gid size date time name
        if len(parts) < 9:
            continue
        mode, uid, gid, name = parts[1], parts[3], parts[4], parts[8]
        if name in (".", ".."):
            continue
        try:
            is_dir = (int(mode, 8) & 0o170000) == 0o040000
        except ValueError:
            is_dir = False
        entries[name] = (uid, gid, is_dir)
    return entries


cache = {}
missing = []
owners = []
# Durable directories the service account must NOT own: the root updater owns
# the releases tree and NetworkManager owns the keyfile store (WP-R4b).
ROOT_OWNED = {
    "/prusa-cam/releases",
    "/network",
    "/network/system-connections",
}
for parent, name in REQUIRED:
    if parent not in cache:
        cache[parent] = list_dir(parent)
    entries = cache[parent]
    path = "/" + name if parent == "/" else parent + "/" + name
    if entries is None:
        out("fail", f"cannot read PERSIST directory {parent} from {persist_image}")
        sys.exit(0)
    entry = entries.get(name)
    if entry is None or not entry[2]:
        missing.append(path)
    else:
        owners.append((path, entry[0], entry[1]))

if missing:
    out("fail", f"PERSIST missing seeded directories: {', '.join(missing)}")
    sys.exit(0)
out("ok", "PERSIST has the seeded /data layout")

# Expected prusa-cam uid/gid comes from the same source setup.sh used: the ROOT
# /etc/passwd, available through --mount-root. Without it, fall back to
# asserting every seeded directory shares one consistent non-root owner.
expected = None
if mount_root:
    try:
        with open(os.path.join(mount_root, "etc", "passwd"), encoding="utf-8") as fh:
            for line in fh:
                fields = line.rstrip("\n").split(":")
                if len(fields) >= 4 and fields[0] == "prusa-cam":
                    expected = (fields[2], fields[3])
                    break
    except OSError:
        expected = None

if expected is not None:
    wrong = []
    for p, u, g in owners:
        want = ("0", "0") if p in ROOT_OWNED else expected
        if (u, g) != want:
            wrong.append(f"{p}({u}:{g})")
    if wrong:
        out(
            "fail",
            "PERSIST ownership mismatch (expected prusa-cam "
            f"{expected[0]}:{expected[1]} and root:root for root-only dirs): "
            f"{', '.join(wrong)}",
        )
    else:
        out(
            "ok",
            "PERSIST directories owned correctly (prusa-cam "
            f"{expected[0]}:{expected[1]}; root-only dirs 0:0)",
        )
else:
    root_wrong = [
        f"{p}({u}:{g})" for p, u, g in owners
        if p in ROOT_OWNED and (u, g) != ("0", "0")
    ]
    others = {(u, g) for p, u, g in owners if p not in ROOT_OWNED}
    if root_wrong:
        out(
            "fail",
            "PERSIST root-only directories are not root-owned: "
            f"{', '.join(root_wrong)}",
        )
    elif len(others) == 1 and ("0", "0") not in others:
        owner = next(iter(others))
        out(
            "ok",
            "PERSIST directories consistently owned by "
            f"{owner[0]}:{owner[1]} with root-only dirs (pass --mount-root to "
            "verify the prusa-cam id)",
        )
    else:
        out(
            "fail",
            "PERSIST directories are not owned by a single non-root account: "
            f"{sorted(others)}",
        )
PY
)
fi

###############################################################################
# C. ROOT utilisation and release manifest
###############################################################################
section "C. ROOT utilisation and release manifest"

if [ -n "$ROOT_IMAGE" ]; then
   if [ ! -f "$ROOT_IMAGE" ]; then
      report fail "root image not found: $ROOT_IMAGE"
   else
      dump="$(dumpe2fs -h "$ROOT_IMAGE" 2>/dev/null || true)"
      blocks="$(printf '%s\n' "$dump" | awk -F: '/Block count/{gsub(/[^0-9]/, "", $2); print $2; exit}')"
      free_blocks="$(printf '%s\n' "$dump" | awk -F: '/Free blocks/{gsub(/[^0-9]/, "", $2); print $2; exit}')"
      if [ -z "$blocks" ] || [ -z "$free_blocks" ]; then
         report fail "dumpe2fs could not read $ROOT_IMAGE (install e2fsprogs)"
      else
         used_pct=$(( (blocks - free_blocks) * 100 / blocks ))
         if [ "$used_pct" -gt "$ROOT_UTIL_LIMIT" ]; then
            report fail "ROOT utilisation ${used_pct}% exceeds the ${ROOT_UTIL_LIMIT}% threshold"
         else
            report ok "ROOT utilisation ${used_pct}% is within the ${ROOT_UTIL_LIMIT}% threshold"
         fi
      fi
   fi
elif [ -n "$MOUNT_ROOT" ]; then
   used_bytes="$(du -sb "$MOUNT_ROOT" 2>/dev/null | awk '{print $1}' || true)"
   used_bytes="${used_bytes:-0}"
   estimate_pct=$(( used_bytes * 100 / ROOT_CAPACITY_BYTES ))
   if [ "$estimate_pct" -gt "$ROOT_UTIL_LIMIT" ]; then
      report warn "ROOT utilisation ~${estimate_pct}% by du exceeds ${ROOT_UTIL_LIMIT}% (estimate only; not authoritative — use --root-image)"
   else
      report warn "ROOT utilisation ~${estimate_pct}% by du (estimate only; not authoritative — use --root-image)"
   fi
else
   report skip "ROOT utilisation (no --root-image or --mount-root)"
fi

if [ -z "$MANIFEST" ]; then
   report oskip "Imager manifest consistency (no --manifest)"
elif [ ! -f "$MANIFEST" ]; then
   report fail "manifest not found: $MANIFEST"
else
   if [[ "$IMAGE" == *.xz ]]; then
      compressed_default="$IMAGE"
      uncompressed_default="${IMAGE%.xz}"
   else
      uncompressed_default="$IMAGE"
      compressed_default="$IMAGE.xz"
   fi
   compressed_artifact="${COMPRESSED_IMAGE:-$compressed_default}"
   consume < <(python3 - "$MANIFEST" "$uncompressed_default" "$compressed_artifact" <<'PY'
import hashlib
import json
import os
import sys

manifest, uncompressed, compressed = sys.argv[1], sys.argv[2], sys.argv[3]


def out(kind, msg):
    print(f"{kind}|{msg}")


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


try:
    with open(manifest, encoding="utf-8") as handle:
        doc = json.load(handle)
except Exception as exc:  # noqa: BLE001 - report, never raise
    out("fail", f"manifest unreadable: {exc}")
    sys.exit(0)

entries = doc.get("os_list") or []
if not entries:
    out("fail", "manifest has no os_list entries")
    sys.exit(0)

entry = None
if len(entries) == 1:
    entry = entries[0]
else:
    base = os.path.basename(compressed)
    for candidate in entries:
        if os.path.basename(str(candidate.get("url", ""))) == base:
            entry = candidate
            break
    if entry is None:
        out("fail", f"no manifest entry matches {base}")
        sys.exit(0)


def check_artifact(path, size_key, hash_key, label):
    if not os.path.isfile(path):
        out("skip", f"{label} artifact not found: {path}")
        return
    size = os.path.getsize(path)
    expected_size = entry.get(size_key)
    size_ok = expected_size is not None and int(expected_size) == size
    out(
        "ok" if size_ok else "fail",
        f"manifest {size_key}={expected_size} matches {label} size {size}",
    )
    expected_hash = str(entry.get(hash_key, "")).lower()
    actual_hash = sha256(path)
    hash_ok = expected_hash == actual_hash
    out(
        "ok" if hash_ok else "fail",
        f"manifest {hash_key} matches {label} {os.path.basename(path)}",
    )


check_artifact(uncompressed, "extract_size", "extract_sha256", "uncompressed")
check_artifact(compressed, "image_download_size", "image_download_sha256", "compressed")
PY
)
fi

###############################################################################
# Summary
###############################################################################
section "Summary"
printf 'checks: %d passed, %d failed, %d skipped, %d warnings\n' "$PASS" "$FAIL" "$SKIP" "$WARN"

# --strict fails only for checks that COULD have run but did not. Skips caused
# solely by an absent optional input (--manifest/--boot-image/--persist-image,
# counted in OPTIONAL_SKIP) are not failures. Required-input problems
# (--image) are rejected up front and never reach this point.
strict_skips=$(( SKIP - OPTIONAL_SKIP ))
if [ "$STRICT" = 1 ] && [ "$strict_skips" -gt 0 ]; then
   report fail "--strict: $strict_skips check(s) were skipped"
elif [ "$STRICT" = 1 ] && [ "$OPTIONAL_SKIP" -gt 0 ]; then
   printf '[INFO]    --strict: %d optional-input check(s) skipped (not failures)\n' "$OPTIONAL_SKIP"
fi

if [ "$FAIL" -gt 0 ]; then
   printf 'RESULT: FAIL\n'
   exit 1
fi
printf 'RESULT: PASS\n'
exit 0
