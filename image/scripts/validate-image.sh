#!/usr/bin/env bash
# Offline validator for the Buddy3D appliance image (AC-13).
#
# Usage:
#   validate-image.sh --image <file> [--mount-root <dir>] [--root-image <root.ext4>]
#                     [--manifest <os-list.json>] [--compressed-image <file>]
#                     [--disksig <0x...>] [--strict]
#
# Two independently testable groups:
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
#      tree that represents the mounted image (ROOT at the top, BOOT under
#      boot/firmware, PERSIST under data/). The tests use a synthetic tree so
#      no mount or root is required. Checks cover required units and ordering,
#      absence of personal usernames/home paths, SSH/password login disabled,
#      overlayroot, volatile journald, NetworkManager, the factory app and
#      launcher fallback, build-info.json, forbidden secret/identity artifacts,
#      and the initial /data structure.
#
#   C. ROOT utilisation and release-manifest consistency: dumpe2fs for
#      --root-image, a clearly-labelled du estimate for --mount-root, and the
#      Imager manifest hash/size cross-check for --manifest.
#
# --strict makes every skipped check fatal. Without it, skipped checks print a
# clear "SKIPPED" line and do not affect the exit status.
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
MANIFEST=""
COMPRESSED_IMAGE=""
DISKSIG=""
STRICT=0

PASS=0
FAIL=0
SKIP=0
WARN=0

usage() {
   cat <<'EOF'
Offline Buddy3D appliance image validator (AC-13).

Options:
  --image <file>             uncompressed or .xz image to inspect (required)
  --mount-root <dir>         directory tree representing the mounted image
                             (ROOT at top, BOOT under boot/firmware, PERSIST
                             under data/); enables the rootfs checks
  --root-image <root.ext4>   ROOT filesystem image for the authoritative
                             dumpe2fs utilisation check
  --manifest <os-list.json>  Raspberry Pi Imager manifest to cross-check
  --compressed-image <file>  override the auto-located compressed artifact
  --disksig <0x...>          expected MBR disk signature
  --strict                   make skipped checks fatal
  -h, --help                 show this help
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }

while [ $# -gt 0 ]; do
   case "$1" in
      --image)            [ $# -ge 2 ] || die "--image needs a value"; IMAGE="$2"; shift 2 ;;
      --mount-root)       [ $# -ge 2 ] || die "--mount-root needs a value"; MOUNT_ROOT="$2"; shift 2 ;;
      --root-image)       [ $# -ge 2 ] || die "--root-image needs a value"; ROOT_IMAGE="$2"; shift 2 ;;
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

TMPDIR_VALIDATE="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_VALIDATE"' EXIT

section() { printf '\n== %s ==\n' "$1"; }

report() {
   case "$1" in
      ok)   PASS=$(( PASS + 1 )); printf '[PASS]    %s\n' "$2" ;;
      fail) FAIL=$(( FAIL + 1 )); printf '[FAIL]    %s\n' "$2" ;;
      skip) SKIP=$(( SKIP + 1 )); printf '[SKIPPED] %s\n' "$2" ;;
      warn) WARN=$(( WARN + 1 )); printf '[WARN]    %s\n' "$2" ;;
      *)    printf '[????]    %s\n' "$2" ;;
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
      prusa-camera.target; do
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
KEY_RE = re.compile(r"BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY")
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
    text = read_text(path)
    if text is not None and KEY_RE.search(text):
        key_hits.append(rel)


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

   keyfiles="$(find "$MOUNT_ROOT" -type f \( -name '*.pem' -o -name '*.key' \) 2>/dev/null || true)"
   if [ -n "$keyfiles" ]; then
      report fail "private key/certificate file present: $(printf '%s ' $keyfiles)"
   else
      report ok "no *.pem/*.key release private key material"
   fi

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
   overlay_conf="$MOUNT_ROOT/etc/overlayroot.conf"
   if [ -f "$overlay_conf" ] && grep -q 'overlayroot' "$overlay_conf"; then
      report ok "/etc/overlayroot.conf configures overlayroot"
   else
      report fail "/etc/overlayroot.conf missing or does not configure overlayroot"
   fi

   cmdline=""
   for candidate in "$MOUNT_ROOT/boot/firmware/cmdline.txt" "$MOUNT_ROOT/boot/cmdline.txt"; do
      if [ -f "$candidate" ]; then
         cmdline="$candidate"
         break
      fi
   done
   if [ -n "$cmdline" ]; then
      if grep -q 'overlayroot=' "$cmdline"; then
         report ok "boot cmdline sets overlayroot="
      else
         report fail "boot cmdline does not set overlayroot="
      fi
   else
      report skip "boot cmdline overlayroot= (no cmdline.txt in tree)"
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

   # --- PERSIST initial structure / ownership / no build-time identity ----
   data_root="$MOUNT_ROOT/data"
   required_data_dirs=(
      prusa-cam prusa-cam/config prusa-cam/releases prusa-cam/backups
      network network/system-connections sdcard sdcard/timelapse
   )
   missing_data=()
   for relative in "${required_data_dirs[@]}"; do
      [ -d "$data_root/$relative" ] || missing_data+=("$relative")
   done
   if [ "${#missing_data[@]}" -eq 0 ]; then
      report ok "PERSIST has the initial /data structure"
   else
      report fail "PERSIST is missing initial directories: ${missing_data[*]}"
   fi

   passwd_file="$MOUNT_ROOT/etc/passwd"
   if [ -f "$passwd_file" ]; then
      service_uid="$(awk -F: '$1=="prusa-cam"{print $3; exit}' "$passwd_file" 2>/dev/null || true)"
      service_gid="$(awk -F: '$1=="prusa-cam"{print $4; exit}' "$passwd_file" 2>/dev/null || true)"
      if [ -n "$service_uid" ] && [ -n "$service_gid" ]; then
         wrong_owner=""
         for relative in "${required_data_dirs[@]}"; do
            if [ -d "$data_root/$relative" ]; then
               owner="$(stat -c '%u:%g' "$data_root/$relative" 2>/dev/null || true)"
               [ "$owner" = "$service_uid:$service_gid" ] || wrong_owner="$wrong_owner $relative($owner)"
            fi
         done
         if [ -z "$wrong_owner" ]; then
            report ok "PERSIST /data directories owned by prusa-cam ($service_uid:$service_gid)"
         else
            report fail "PERSIST /data ownership mismatch:$wrong_owner"
         fi
      else
         report skip "PERSIST ownership (prusa-cam not in /etc/passwd)"
      fi
   else
      report skip "PERSIST ownership (no /etc/passwd in tree)"
   fi

   identity_hits="$(find "$data_root" \( -name 'identity.json' -o -name 'secrets.toml' \) 2>/dev/null || true)"
   if [ -n "$identity_hits" ]; then
      report fail "build-time device identity/secret in /data: $(printf '%s ' $identity_hits)"
   else
      report ok "no build-time device identity in /data (identity.json/secrets.toml)"
   fi
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
   report skip "Imager manifest consistency (no --manifest)"
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

if [ "$STRICT" = 1 ] && [ "$SKIP" -gt 0 ]; then
   report fail "--strict: $SKIP check(s) were skipped"
fi

if [ "$FAIL" -gt 0 ]; then
   printf 'RESULT: FAIL\n'
   exit 1
fi
printf 'RESULT: PASS\n'
exit 0
