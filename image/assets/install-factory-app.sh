#!/bin/bash
# Factory application install/layout hook for the Buddy3D image (AC-9/AC-10).
#
# Invoked from image/layer/buddy3d-image.yaml's mmdebstrap customize-hooks as:
#   install-factory-app.sh <target-root> <repo-root>
#
# It installs the immutable factory application under /opt/prusa-cam, reuses
# the pi-impersonator/systemd units verbatim, adds the image-only units and
# drop-ins from this directory, and writes build-info.json. It creates no
# secret, identity, Wi-Fi profile, or SSH host key.
set -eu

root="${1:?target root required}"
repo="${2:?repository root required}"
assets="$(cd "$(dirname "$0")" && pwd)"

APP_ROOT=/opt/prusa-cam
SERVICE_USER=prusa-cam
SYSTEMD_DST="$root/etc/systemd/system"

log() { echo "buddy3d-image: $*"; }

# --- dedicated non-login service account (AC-1) -----------------------------
if chroot "$root" getent passwd "$SERVICE_USER" >/dev/null 2>&1; then
   chroot "$root" usermod --shell /usr/sbin/nologin --home "$APP_ROOT" "$SERVICE_USER"
else
   chroot "$root" useradd --system --create-home --home-dir "$APP_ROOT" \
      --shell /usr/sbin/nologin "$SERVICE_USER"
fi
chroot "$root" usermod -aG video,render,audio,plugdev,gpio,i2c,spi "$SERVICE_USER" \
   2>/dev/null || true
rm -rf "$root/home/$SERVICE_USER"
uid="$(chroot "$root" id -u "$SERVICE_USER")"
gid="$(chroot "$root" id -g "$SERVICE_USER")"

# --- immutable factory application code -------------------------------------
# /opt/prusa-cam is an immutable factory tree owned by root:root (B3). The
# prusa-cam account may read/execute it but must never be able to write it: a
# root helper (prusa-priv) executes code from here, so service-account-writable
# code would be a privilege-escalation path. Durable data lives under
# /data/prusa-cam and the ephemeral runtime config under /etc/prusa-cam.
install -d -m 0755 "$root$APP_ROOT"
rsync -a --delete \
   --exclude 'venv/' --exclude '__pycache__/' --exclude '*.pyc' \
   --exclude 'config.ini' --exclude '*.example' --exclude 'systemd/' \
   --exclude 'deploy.sh' --exclude 'bootstrap.sh' --exclude 'README.md' \
   --exclude 'backups/' \
   "$repo/pi-impersonator/" "$root$APP_ROOT/"
chown -R root:root "$root$APP_ROOT"

# --- fixed-verb root helper + narrow sudoers rule (B3) ----------------------
install -D -o root -g root -m 0755 "$assets/prusa-priv" \
   "$root/usr/libexec/prusa-cam/prusa-priv"
install -D -o root -g root -m 0440 "$assets/sudoers/prusa-cam" \
   "$root/etc/sudoers.d/prusa-cam"
if command -v visudo >/dev/null 2>&1; then
   if ! visudo -cf "$root/etc/sudoers.d/prusa-cam" >/dev/null; then
      log "ERROR: /etc/sudoers.d/prusa-cam failed visudo -cf"
      exit 1
   fi
else
   log "visudo unavailable; sudoers syntax check skipped"
fi

# --- reused runtime units (never divergent copies) --------------------------
unit_src="$repo/pi-impersonator/systemd"
for u in rpicam-source.service prusa-rtsp.service prusa-ha-rtsp.service \
         prusa-cam.service prusa-admin.service prusa-provisioning.service \
         pi-persist.service prusa-data-ready.service data-ready.target \
         bootlog.service; do
   install -D -m 0644 "$unit_src/$u" "$SYSTEMD_DST/$u"
done

# --- image-only units, helper, and drop-ins ---------------------------------
install -D -m 0755 "$assets/prusa-data-grow.sh" "$root/usr/libexec/prusa-data-grow"
install -D -m 0644 "$assets/systemd/prusa-data-grow.service" \
   "$SYSTEMD_DST/prusa-data-grow.service"
install -D -m 0644 "$assets/systemd/prusa-camera.target" \
   "$SYSTEMD_DST/prusa-camera.target"
install -D -m 0644 "$assets/systemd/prusa-boot-mode.service" \
   "$SYSTEMD_DST/prusa-boot-mode.service"
install -D -m 0644 "$assets/systemd/data-ready.target.d/10-data-grow.conf" \
   "$SYSTEMD_DST/data-ready.target.d/10-data-grow.conf"
install -D -m 0644 "$assets/systemd/NetworkManager.service.d/10-data-ready.conf" \
   "$SYSTEMD_DST/NetworkManager.service.d/10-data-ready.conf"

# --- volatile, size-limited journald (AC-13) --------------------------------
install -D -m 0644 "$assets/systemd/journald-volatile.conf" \
   "$root/etc/systemd/journald.conf.d/99-buddy3d-volatile.conf"

# --- factory fallback launcher (AC-13; units wired through it in WP-6) ------
install -D -m 0755 "$assets/launcher.sh" "$root$APP_ROOT/launcher.sh"

# --- enable the runtime graph (source §3.2) ---------------------------------
# Only the pre-runtime gate, pi-persist.service and the boot-mode selector are
# enabled. pi-persist.service must run at boot (even while unclaimed) so it
# bind-mounts the durable NetworkManager system-connections and /mnt/sdcard
# stores before NetworkManager/camera units start (B4). The camera units,
# prusa-admin.service and prusa-camera.target are NOT enabled here: the selector
# starts prusa-camera.target after claim and it pulls them via Wants=
# (AC-12/AC-17), so nothing camera-related runs while the device is unclaimed.
# The units' own [Install] sections are left intact for the dev deploy.sh path.
chroot "$root" systemctl enable \
   data-ready.target prusa-data-ready.service prusa-data-grow.service \
   pi-persist.service bootlog.service prusa-boot-mode.service >/dev/null 2>&1 || true

# SSH is installed but disabled by default (AC-13/AC-20). The disable runs in
# image/layer/post-build.sh, which executes after every layer — the reused
# openssh-server layer re-enables ssh.service after this customize hook, so
# disabling here would be undone. Raspberry Pi Imager may enable SSH later.

# --- build-info.json (AC-14) ------------------------------------------------
install -d -m 0755 "$root/usr/share/prusa-buddy3d-camera"

# Record the installed-package manifest (AC-14). The manifest is generated
# inside the target root; build-info.json records the in-image path so the
# image never embeds a host build path. If the chroot has no dpkg the file is
# left absent and build-info records package_manifest as null.
MANIFEST_IMAGE_PATH=/usr/share/prusa-buddy3d-camera/packages.txt
manifest="$root$MANIFEST_IMAGE_PATH"
if chroot "$root" dpkg-query -W -f='${Package} ${Version}\n' > "$manifest" 2>/dev/null; then
   log "recorded installed-package manifest ($(wc -l < "$manifest") packages)"
else
   rm -f "$manifest"
   log "dpkg-query unavailable in chroot; package manifest not recorded"
fi
manifest_arg=""
if [ -s "$manifest" ]; then
   manifest_arg="$MANIFEST_IMAGE_PATH"
fi

python3 "$assets/build-info.py" \
   --source-commit "${PRUSA_SOURCE_COMMIT:-unknown}" \
   --builder-revision "${RPI_IMAGE_GEN_REVISION:-unknown}" \
   --os-suite "${PRUSA_OS_SUITE:-trixie}" \
   --kernel-package "${PRUSA_KERNEL_PACKAGE:-linux-image-rpi-v8}" \
   --package-manifest "$manifest_arg" \
   --output "$root/usr/share/prusa-buddy3d-camera/build-info.json"

# --- durable configuration directory + ownership ----------------------------
# /opt/prusa-cam stays root:root 0755 (B3): the factory app is immutable and is
# never writable by the service account. Only the ephemeral runtime config
# directory (quality.env/rtsp.mode are written at runtime) and the durable
# /data/prusa-cam layout (created by persist_restore.py) are prusa-cam-owned.
install -d -m 0750 "$root/etc/prusa-cam"
install -d -m 0755 "$root$APP_ROOT/backups"
chown -R "$uid:$gid" "$root/etc/prusa-cam"
chmod 0755 "$root$APP_ROOT/bootlog.sh" 2>/dev/null || true
log "factory app installed at $APP_ROOT (root:root; service uid=$uid gid=$gid)"
