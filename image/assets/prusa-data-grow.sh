#!/bin/bash
# First-boot growth of the PERSIST partition and filesystem (AC-11).
#
# Installed to /usr/libexec/prusa-data-grow and run once per boot by
# prusa-data-grow.service, ordered local-fs.target -> prusa-data-grow.service
# -> data-ready.target (source §3.2). It never assumes a device name: the
# parent device and partition number come from the mounted /data source and
# sysfs, and it validates the PERSIST label and the expected PARTUUID
# relationship before touching the partition table.
#
# Interruption safety: the durable marker is written on PERSIST only after both
# growpart and resize2fs succeed. An interruption before that leaves the marker
# absent, so the next boot re-runs the unit; growpart reports NOCHANGE and
# resize2fs is a no-op on an already-grown filesystem.
set -eu

MARKER=/data/.prusa-data-grow.done
EXPECTED_LABEL=PERSIST
EXPECTED_PARTNUM=3

log() { echo "prusa-data-grow: $*"; }
die() { log "ERROR: $*"; exit 1; }

if [ -f "$MARKER" ]; then
   log "completion marker present; already grown"
   exit 0
fi

# Resolve the mounted /data source (PARTUUID/label symlinks -> real node).
src="$(findmnt -no SOURCE --target /data 2>/dev/null || true)"
[ -n "$src" ] || die "/data is not a mount point"
src="$(readlink -f "$src")"
[ -b "$src" ] || die "/data source $src is not a block device"

# Parent device and partition number straight from sysfs (works for mmcblk0p3,
# sda3, nvme0n1p3, ...).
part="$(basename "$src")"
[ -r "/sys/class/block/$part/partition" ] || die "cannot read partition number for $src"
num="$(cat "/sys/class/block/$part/partition")"
[ "$num" = "$EXPECTED_PARTNUM" ] || die "PERSIST is partition $num, expected $EXPECTED_PARTNUM"
pk="$(lsblk -no PKNAME "$src" 2>/dev/null | tr -d ' ')"
[ -n "$pk" ] || die "cannot determine the parent device of $src"
base="/dev/$pk"

# Validate the filesystem label.
label="$(blkid -s LABEL -o value "$src" 2>/dev/null || true)"
[ "$label" = "$EXPECTED_LABEL" ] || die "$src label '$label' != $EXPECTED_LABEL"

# Validate the expected PARTUUID relationship: PARTUUID is <disk-signature>-NN
# and the signature matches the first partition on the same device.
partuuid="$(blkid -s PARTUUID -o value "$src" 2>/dev/null || true)"
[ -n "$partuuid" ] || die "no PARTUUID for $src"
case "$partuuid" in
   *-"$EXPECTED_PARTNUM") ;;
   *) die "PARTUUID '$partuuid' does not end in -$EXPECTED_PARTNUM" ;;
esac
disk_sig="${partuuid%-*}"
first_pu="$(lsblk -no PARTUUID "$base" 2>/dev/null | awk 'NF { print; exit }')"
[ -n "$first_pu" ] || die "no PARTUUID found on $base"
[ "${first_pu%-*}" = "$disk_sig" ] || die "PARTUUID prefix '${first_pu%-*}' != '$disk_sig'"

# Grow the partition to the end of the device. growpart is idempotent: it
# prints NOCHANGE and exits 1 when the partition already fills the device.
command -v growpart >/dev/null 2>&1 || die "growpart(8) not found (install cloud-guest-utils)"
set +e
out="$(growpart "$base" "$num" 2>&1)"
rc=$?
set -e
case "$out" in
   *NOCHANGE*) log "partition already spans the device" ;;
   *) [ "$rc" -eq 0 ] || die "growpart failed (rc=$rc): $out" ;;
esac

# Resize the filesystem online (ext4 supports growing a mounted filesystem).
resize2fs "$src" || die "resize2fs failed on $src"

# Durable completion marker, written only after BOTH operations succeeded.
install -m 0644 /dev/null "$MARKER"
sync
log "PERSIST grown and resized; marker written to $MARKER"
