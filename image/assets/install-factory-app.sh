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
install -d -m 0755 "$root$APP_ROOT"
rsync -a --delete \
   --exclude 'venv/' --exclude '__pycache__/' --exclude '*.pyc' \
   --exclude 'config.ini' --exclude '*.example' --exclude 'systemd/' \
   --exclude 'deploy.sh' --exclude 'bootstrap.sh' --exclude 'README.md' \
   --exclude 'backups/' \
   "$repo/pi-impersonator/" "$root$APP_ROOT/"

# --- reused runtime units (never divergent copies) --------------------------
unit_src="$repo/pi-impersonator/systemd"
for u in rpicam-source.service prusa-rtsp.service prusa-ha-rtsp.service \
         prusa-cam.service pi-persist.service prusa-data-ready.service \
         data-ready.target bootlog.service; do
   install -D -m 0644 "$unit_src/$u" "$SYSTEMD_DST/$u"
done

# --- image-only units, helper, and drop-ins ---------------------------------
install -D -m 0755 "$assets/prusa-data-grow.sh" "$root/usr/libexec/prusa-data-grow"
install -D -m 0644 "$assets/systemd/prusa-data-grow.service" \
   "$SYSTEMD_DST/prusa-data-grow.service"
install -D -m 0644 "$assets/systemd/prusa-camera.target" \
   "$SYSTEMD_DST/prusa-camera.target"
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
chroot "$root" systemctl enable \
   data-ready.target prusa-data-ready.service prusa-data-grow.service \
   pi-persist.service rpicam-source.service prusa-rtsp.service \
   prusa-ha-rtsp.service prusa-cam.service bootlog.service \
   prusa-camera.target >/dev/null 2>&1 || true

# SSH is installed but disabled by default (AC-13/AC-20). Raspberry Pi Imager
# may enable it (and create the operator account) during first-run setup.
chroot "$root" systemctl disable ssh.service >/dev/null 2>&1 || true

# --- build-info.json (AC-14); package manifest is a WP-2b extension point ---
install -d -m 0755 "$root/usr/share/prusa-buddy3d-camera"
python3 "$assets/build-info.py" \
   --source-commit "${PRUSA_SOURCE_COMMIT:-unknown}" \
   --builder-revision "${RPI_IMAGE_GEN_REVISION:-unknown}" \
   --os-suite "${PRUSA_OS_SUITE:-trixie}" \
   --kernel-package "${PRUSA_KERNEL_PACKAGE:-linux-image-rpi-v8}" \
   --package-manifest "${PRUSA_PACKAGE_MANIFEST:-}" \
   --output "$root/usr/share/prusa-buddy3d-camera/build-info.json"

# --- durable configuration directory + ownership ----------------------------
install -d -m 0750 "$root/etc/prusa-cam"
install -d -m 0755 "$root$APP_ROOT/backups"
chown -R "$uid:$gid" "$root$APP_ROOT" "$root/etc/prusa-cam"
chmod 0755 "$root$APP_ROOT/bootlog.sh" 2>/dev/null || true
log "factory app installed at $APP_ROOT (uid=$uid gid=$gid)"
