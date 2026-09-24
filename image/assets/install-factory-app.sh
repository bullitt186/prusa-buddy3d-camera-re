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
# rsync -a applies the source directory's mode (the checkout is often 0775),
# so pin the factory tree to 0755 explicitly (B3/AC-13).
chmod 0755 "$root$APP_ROOT"

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
         bootlog.service prusa-updater.service prusa-updater.timer \
         prusa-updater-install.service; do
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

# --- camera device access (hardware fix) ------------------------------------
# /dev/dma_heap/* is root:root 0600, so the prusa-cam service account cannot
# allocate camera buffers; grant the video group access (prusa-cam is a member).
install -D -o root -g root -m 0644 "$assets/udev/50-prusa-cam-camera.rules" \
   "$root/etc/udev/rules.d/50-prusa-cam-camera.rules"

# --- stable Wi-Fi identity (hardware fix) -----------------------------------
# Keep the wlan0 MAC stable: NetworkManager's scan-time MAC randomization made
# the MAC-derived fingerprint flap and broke the Prusa Connect token binding.
install -D -o root -g root -m 0644 "$assets/networkmanager/10-prusa-mac.conf" \
   "$root/etc/NetworkManager/conf.d/10-prusa-mac.conf"

# --- runtime launcher + factory fallback (AC-13; WP-R4c) --------------------
# The runtime units exec this launcher, which prefers an installed release
# under DATA and falls back to the immutable factory app when none is valid.
install -D -m 0755 "$assets/launcher.sh" "$root$APP_ROOT/launcher.sh"

# --- hash-locked Python runtime venv (WP-R3 / AC-14) ------------------------
# The venv is built here, not in the bdebstrap customize95-buddy3d-python
# hook: this installer runs from the layer's customize-hooks, which execute
# after the factory app directory exists, so the venv always lands in a
# populated /opt/prusa-cam. That hook is a documented no-op.
#
# requirements.lock is the committed, hash-locked arm64 dependency set. It is
# installed with --require-hashes and --no-cache-dir so no unpinned, tampered,
# or cached wheel can enter the image. Native components (PyGObject, GStreamer,
# libcamera/rpicam, NetworkManager, Samba) stay Debian packages.
install -m 0644 "$repo/image/requirements.lock" "$root$APP_ROOT/requirements.lock"
lock_sha256="$(sha256sum "$root$APP_ROOT/requirements.lock" | awk '{print $1}')"
log "installing hash-locked Python deps (requirements.lock sha256=$lock_sha256)"

# --system-site-packages keeps Debian's PyGObject/GStreamer visible inside the
# venv; only the pure-Python deps come from the lock (AC-14 tail).
# Preflight: `python3 -m venv` needs ensurepip. The package list requests the
# versioned venv package, but apt resolution in the pinned snapshot is brittle
# (trixie's python3-venv metapackage pins an older python3), so repair it in
# place rather than shipping a venv-less image. The diagnostic lines below make
# an apt failure self-explanatory in the build log.
pyver="$(chroot "$root" /usr/bin/python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 3)"
export DEBIAN_FRONTEND=noninteractive
if ! chroot "$root" /usr/bin/python3 -c 'import ensurepip' >/dev/null 2>&1; then
   log "ensurepip is missing in the image; installing python${pyver}-venv"
   chroot "$root" dpkg-query -W -f='${Package} ${Version}\n' 2>/dev/null \
      | grep -E "python3(\.${pyver#3.}-)?-(venv|pip)" \
      | sed 's/^/buddy3d-image: installed: /' || true
   if ! chroot "$root" apt-get install -y --no-install-recommends "python${pyver}-venv" 2>&1 \
           | sed 's/^/buddy3d-image: apt: /'; then
      log "apt install failed; refreshing the package lists and retrying"
      chroot "$root" apt-get update -qq 2>&1 | sed 's/^/buddy3d-image: apt: /' || true
      chroot "$root" apt-get install -y --no-install-recommends "python${pyver}-venv" 2>&1 \
         | sed 's/^/buddy3d-image: apt: /' || true
   fi
fi
if ! chroot "$root" /usr/bin/python3 -c 'import ensurepip' >/dev/null 2>&1; then
   log "ERROR: ensurepip is unavailable in the image; refusing to ship a venv-less image"
   exit 1
fi
if ! chroot "$root" /usr/bin/python3 -m venv --system-site-packages "$APP_ROOT/venv"; then
   log "ERROR: failed to create the runtime venv at $APP_ROOT/venv"
   exit 1
fi
if [ ! -x "$root$APP_ROOT/venv/bin/python" ]; then
   log "ERROR: $APP_ROOT/venv/bin/python is missing after venv creation"
   exit 1
fi
if ! chroot "$root" "$APP_ROOT/venv/bin/pip" install \
      --require-hashes --no-cache-dir -r "$APP_ROOT/requirements.lock"; then
   log "ERROR: pip install --require-hashes failed; refusing to ship a venv-less image"
   exit 1
fi
# The venv is part of the immutable factory tree: root:root and never
# group/world-writable (B3).
chown -R root:root "$root$APP_ROOT/venv"
chmod -R go-w "$root$APP_ROOT/venv"

# The reused openssh-server layer runs `uchroot $1 'mkdir -p ${HOME}/.ssh'` as
# user1 (prusa-cam). Since B3 the factory tree is root-owned, so that hook would
# fail with "Permission denied" and abort the build. Pre-create the service
# account's .ssh directory (0700, owned by the account); SSH itself stays
# disabled by default until an operator explicitly enables it (AC-20).
install -d -m 0700 "$root$APP_ROOT/.ssh"
chown "$uid:$gid" "$root$APP_ROOT/.ssh"

# --- enable the runtime graph (source §3.2) ---------------------------------
# Only the pre-runtime gate, pi-persist.service and the boot-mode selector are
# enabled. pi-persist.service must run at boot (even while unclaimed) so it
# bind-mounts the durable NetworkManager system-connections and /mnt/sdcard
# stores before NetworkManager/camera units start (B4). The camera units,
# prusa-admin.service and prusa-camera.target are NOT enabled here: the selector
# starts prusa-camera.target after claim and it pulls them via Wants=
# (AC-12/AC-17), so nothing camera-related runs while the device is unclaimed.
# The units' own [Install] sections are left intact for the dev deploy.sh path.
# prusa-updater.timer (WP-R4b) is enabled here: the daily signed-update check
# runs independently of claim and is isolated from camera startup (AC-27). Its
# oneshot service is triggered by the timer and has no [Install] section, so it
# is installed but not separately enabled.
chroot "$root" systemctl enable \
   data-ready.target prusa-data-ready.service prusa-data-grow.service \
   pi-persist.service bootlog.service prusa-boot-mode.service \
   prusa-updater.timer >/dev/null 2>&1 || true

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
   --python-lock-sha256 "$lock_sha256" \
   --output "$root/usr/share/prusa-buddy3d-camera/build-info.json"

# --- embedded release-signing public key (WP-R4b / AC-29) -------------------
# The updater trusts exactly this committed public key; the matching secret key
# never enters the repository or the image. It is root:root 0644 so the root
# updater service can read it and the service account cannot modify it. The
# image secret scan (validate-image.sh) asserts it is not private key material.
install -D -o root -g root -m 0644 "$repo/image/keys/buddy3d-release.pub" \
   "$root/usr/share/prusa-buddy3d-camera/buddy3d-release.pub"
log "embedded release-signing public key (buddy3d-release.pub)"

# --- root-owned updater configuration (WP-R4b / AC-32) ----------------------
# prusa-updater.service reads only /etc/prusa-updater.conf via EnvironmentFile=.
# It is root:root 0644 — never under /etc/prusa-cam, which the service account
# can write — so the account cannot redirect the updater at an attacker manifest
# or override the trust anchor. The signing public key is fixed in
# updater_install.py and is not configurable here. Unset manifest URL => the
# scheduled check exits 0 ("not configured"), never a usage error.
if [ ! -f "$root/etc/prusa-updater.conf" ]; then
   install -D -o root -g root -m 0644 /dev/null "$root/etc/prusa-updater.conf"
   {
      echo '# Buddy3D signed application updates (WP-R4b). Root-owned, mode 0644.'
      echo '# Set one manifest URL to enable the daily availability check:'
      echo '# PRUSA_UPDATE_MANIFEST_URL=https://example.invalid/update-manifest.json'
   } > "$root/etc/prusa-updater.conf"
   chown root:root "$root/etc/prusa-updater.conf"
   chmod 0644 "$root/etc/prusa-updater.conf"
fi
log "root-owned updater config /etc/prusa-updater.conf"

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
