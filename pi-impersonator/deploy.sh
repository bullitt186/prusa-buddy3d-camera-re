#!/usr/bin/env bash
# Overlay-aware deploy for the Pi impersonator.
#
# The Pi runs a read-only overlayfs root (see CLAUDE.md "Making changes to the Pi that survive
# reboot"). An rsync onto a running prod Pi lands in the tmpfs upper layer and is LOST on reboot.
# This script is the ONLY supported way to update the Pi: it detects the overlay state and, in
# prod mode, does the disable→reboot→deploy→enable→reboot dance so changes hit the real disk.
#
# Usage:  ./deploy.sh [user@host] [--enable-overlay|--disable-overlay]   (or set PI=user@host)
#   (no flag)          deploy code (overlay-aware: dev=fast, prod=maintenance dance)
#   --enable-overlay   one-time: turn on read-only overlayfs root, SAFELY (gated so a broken
#                      initramfs can't brick a headless Pi — verifies before it reboots)
#   --disable-overlay  turn overlay off (for a maintenance window), reboot
# Portable on purpose — no host is hard-coded here. Real host lives in .agent/pi-ops.md.
set -euo pipefail

MODE=deploy
PI_ARG=""
for a in "$@"; do case "$a" in
  --enable-overlay)  MODE=enable ;;
  --disable-overlay) MODE=disable ;;
  *) PI_ARG="$a" ;;
esac; done
PI="${PI_ARG:-${PI:-}}"   # positional wins, else the PI env var
[ -n "$PI" ] || { echo "usage: PI=user@host $0 [--enable-overlay|--disable-overlay]"; exit 2; }
PI_USER="${PI%@*}"
# Dedicated non-login service identity and application root (AC-1). The SSH
# transport user above is only used for the maintenance connection; the deployed
# code and units always run as $SERVICE_USER from $APP_ROOT.
SERVICE_USER="${SERVICE_USER:-prusa-cam}"
APP_ROOT="${APP_ROOT:-/opt/prusa-cam}"
SRC="$(cd "$(dirname "$0")" && pwd)"
SSH=(ssh -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new "$PI")

log() { printf '\n\033[1m» %s\033[0m\n' "$*"; }

install_unit() {  # install a repo unit template verbatim (no user/path templating)
  local name="$1"
  "${SSH[@]}" "sudo install -m 0644 /dev/stdin /etc/systemd/system/$name" \
    < "$SRC/systemd/$name"
}

wait_for_ssh() {  # block until the Pi answers again after a reboot (~10 min budget)
  log "waiting for $PI to come back…"
  for _ in $(seq 1 120); do
    "${SSH[@]}" true 2>/dev/null && { echo "  up."; return 0; }
    sleep 5
  done
  echo "  ERROR: $PI did not return"; return 1
}

overlay_on() { "${SSH[@]}" 'findmnt -no FSTYPE / | grep -q overlay'; }

preflight_persistent_ssh() {
  # An authorized_keys file created while overlayroot is active can live only in
  # tmpfs. Disabling the overlay would then boot a healthy Pi that rejects the
  # very key needed to finish the deployment. Compare the live and persistent
  # files through overlayroot-chroot before taking that maintenance reboot.
  log "preflight persistent SSH access"
  "${SSH[@]}" "set -e
    test -s /home/$PI_USER/.ssh/authorized_keys
    sudo overlayroot-chroot test -s /home/$PI_USER/.ssh/authorized_keys
    live=\$(mktemp)
    trap 'rm -f \"\$live\"' EXIT
    sed '/^[[:space:]]*#/d;/^[[:space:]]*\$/d' /home/$PI_USER/.ssh/authorized_keys | sort -u > \"\$live\"
    sudo overlayroot-chroot cat /home/$PI_USER/.ssh/authorized_keys \
      | sed '/^[[:space:]]*#/d;/^[[:space:]]*\$/d' | sort -u \
      | cmp -s \"\$live\" -" \
    || { echo "ERROR: persistent authorized_keys differs from the live overlay; refusing maintenance reboot"; return 1; }
}

set_overlay() {  # $1 = enabled|disabled ; toggles via cmdline.txt on the FAT /boot (no RO-root write)
  local want="$1"
  "${SSH[@]}" "sudo mount -o remount,rw /boot/firmware && \
    sudo sed -i 's/ *overlayroot=[^ ]*//' /boot/firmware/cmdline.txt && \
    { [ '$want' = disabled ] && sudo sed -i 's/\$/ overlayroot=disabled/' /boot/firmware/cmdline.txt || true; } && \
    sudo mount -o remount,ro /boot/firmware"
}

reboot_pi() { "${SSH[@]}" 'sudo systemctl reboot' 2>/dev/null || true; sleep 8; wait_for_ssh; }

push_and_restart() {  # the actual deploy — assumes root is writable (dev mode or overlay disabled)
  log "ensure $SERVICE_USER account (dedicated non-login service identity)"
  "${SSH[@]}" "set -e
    id -u $SERVICE_USER >/dev/null 2>&1 || \
      sudo useradd --system --create-home --home-dir $APP_ROOT --shell /usr/sbin/nologin $SERVICE_USER
    sudo usermod -aG video $SERVICE_USER"

  log "backup + rsync sources → $PI:$APP_ROOT/"
  "${SSH[@]}" "d=\$(date +%Y%m%d_%H%M%S); sudo mkdir -p $APP_ROOT/backups/\$d && \
    sudo cp $APP_ROOT/*.py $APP_ROOT/backups/\$d/ 2>/dev/null || true"
  # Run the remote rsync as root so it can write into $APP_ROOT even before the
  # service account owns it; ownership is set explicitly below.
  rsync -az --rsync-path="sudo rsync" --no-owner --no-group \
        --exclude 'config.ini' --exclude 'venv/' --exclude '__pycache__/' \
        --exclude 'backups/' --exclude 'systemd/' --exclude '*.example' --exclude 'README.md' \
        --exclude 'deploy.sh' "$SRC/" "$PI:$APP_ROOT/"
  "${SSH[@]}" "sudo chown -R $SERVICE_USER:$SERVICE_USER $APP_ROOT && \
    sudo chmod +x $APP_ROOT/bootlog.sh"

  log "provision /etc/prusa-cam + quality.env (idempotent)"
  "${SSH[@]}" "sudo install -d -o $SERVICE_USER -g $SERVICE_USER /etc/prusa-cam && \
    { [ -f /etc/prusa-cam/quality.env ] || printf 'CAM_WIDTH=1920\nCAM_HEIGHT=1080\n' | sudo tee /etc/prusa-cam/quality.env >/dev/null; }"

  # WebRTC live view needs webrtcbin's ICE plugin (libgstnice.so). Install it here
  # so it lands on the real disk (this runs with the overlay disabled / in dev),
  # not just the tmpfs upper layer. Non-fatal (apt can be memory-tight on a 512 MB Pi).
  log "ensure gstreamer1.0-nice (WebRTC ICE plugin)"
  "${SSH[@]}" 'command -v gst-inspect-1.0 >/dev/null && gst-inspect-1.0 nice >/dev/null 2>&1 \
    || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y gstreamer1.0-nice >/dev/null 2>&1 || true'

  # Emulated SD card: /mnt/sdcard backs the timelapse feature and is shared over
  # SMB so the recorded clips can be retrieved from a PC. The service user owns
  # the mountpoint itself (not just the timelapse subdir) so the firmware-style
  # RW probe and statvfs report it as writable.
  log "provision emulated SD (/mnt/sdcard) + SMB share"
  "${SSH[@]}" "sudo install -d /mnt/sdcard /mnt/sdcard/timelapse && \
    sudo chown $SERVICE_USER:$SERVICE_USER /mnt/sdcard /mnt/sdcard/timelapse && \
    { [ -x /usr/sbin/smbd ] || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends samba >/dev/null 2>&1; } && \
    sudo install -d /etc/samba && \
    printf '[sdcard]\n   path = /mnt/sdcard\n   browseable = yes\n   read only = no\n   guest ok = yes\n   force user = $SERVICE_USER\n   create mask = 0644\n   directory mask = 0755\n' \
      | sudo tee /etc/samba/smb-sdcard.conf >/dev/null && \
    { grep -q 'include = /etc/samba/smb-sdcard.conf' /etc/samba/smb.conf 2>/dev/null || \
      printf '\ninclude = /etc/samba/smb-sdcard.conf\n' | sudo tee -a /etc/samba/smb.conf >/dev/null; } && \
    sudo systemctl enable --now smbd >/dev/null 2>&1 || true"

  # Units are installed verbatim from the repo templates: they hard-code
  # User=prusa-cam / /opt/prusa-cam, so no per-host sed templating remains.
  log "install systemd units (verbatim) + data-ready gate"
  for u in rpicam-source.service prusa-ha-rtsp.service prusa-rtsp.service \
           prusa-cam.service bootlog.service pi-persist.service \
           prusa-data-ready.service data-ready.target; do
    install_unit "$u"
  done
  "${SSH[@]}" "sudo systemctl daemon-reload && \
    sudo systemctl enable data-ready.target prusa-data-ready.service \
      rpicam-source.service prusa-ha-rtsp.service prusa-rtsp.service \
      prusa-cam.service pi-persist.service bootlog.service >/dev/null 2>&1 || true"
  # Persist a boot-reason/throttle snapshot to the real vfat boot partition: the
  # root overlay + volatile journal otherwise erase all evidence of an unexpected
  # reboot (see the reboot/throttling investigation in the gap tracker).
  "${SSH[@]}" 'sudo systemctl start bootlog.service >/dev/null 2>&1 || true'

  # Guarded activation: only when the /data partition already exists. The fstab
  # entry uses the partition's real PARTUUID and is skipped when already present,
  # so this is idempotent and harmless before the repartition.
  log "check /data partition (mmcblk0p3)"
  "${SSH[@]}" "if [ -b /dev/mmcblk0p3 ]; then
      uuid=\$(sudo blkid -s PARTUUID -o value /dev/mmcblk0p3)
      if [ -n \"\$uuid\" ] && ! grep -q \"\$uuid\" /etc/fstab; then
        printf 'PARTUUID=%s /data ext4 defaults,noatime 0 2\n' \"\$uuid\" | sudo tee -a /etc/fstab >/dev/null
        echo \"added /data fstab entry for \$uuid\"
      fi
      sudo install -d -m 0755 /data/sdcard /data/sdcard/timelapse
      sudo install -d -m 0750 /data/prusa-cam /data/prusa-cam/config /data/prusa-cam/releases /data/prusa-cam/backups /data/network/system-connections
      sudo chown -R $SERVICE_USER:$SERVICE_USER /data/sdcard /data/prusa-cam /data/network
    else
      echo 'no /data partition yet; skipping persistence activation'
    fi"

  log "restart services"
  "${SSH[@]}" 'sudo systemctl restart data-ready.target rpicam-source.service prusa-ha-rtsp.service && \
    sudo systemctl try-restart prusa-rtsp.service && \
    sudo systemctl restart prusa-cam.service'
  sleep 5
}

verify() {
  log "verify"
  "${SSH[@]}" 'set -e
    echo -n "required services: "
    systemctl is-active rpicam-source prusa-ha-rtsp prusa-cam | tr "\n" " "
    echo
    echo -n "Prusa RTSP mode-dependent service: "
    systemctl is-active prusa-rtsp || true
    onvif_ok=
    for _ in $(seq 1 15); do
      if curl -fsS --max-time 5 -H "Content-Type: application/soap+xml" \
        --data "<s:Envelope xmlns:s=\"http://www.w3.org/2003/05/soap-envelope\"><s:Body><tds:GetDeviceInformation xmlns:tds=\"http://www.onvif.org/ver10/device/wsdl\"/></s:Body></s:Envelope>" \
        http://127.0.0.1/onvif/device_service 2>/dev/null | grep -q GetDeviceInformationResponse; then
        onvif_ok=1
        break
      fi
      sleep 4
    done
    [ "$onvif_ok" = 1 ]
    echo "ONVIF: ok"
    journalctl -u prusa-cam -n 20 --no-pager | grep -iE "Snapshot: 200|/c/info response" | tail -1 || echo "  (no snapshot line yet)"'
}

enable_overlay() {
  # SAFELY enable read-only overlayfs. The dangerous failure is rebooting into a broken
  # initramfs on a headless Pi (unrecoverable remotely), so every step is verified BEFORE reboot.
  if overlay_on; then log "overlay already ON — nothing to do"; return 0; fi
  log "installing overlayroot + initramfs-tools"
  "${SSH[@]}" 'sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y overlayroot initramfs-tools' \
    || { echo "ERROR: apt install failed — aborting (overlay NOT enabled)"; exit 1; }

  log "enabling initramfs in bootloader + writing overlayroot.conf"
  "${SSH[@]}" "sudo mount -o remount,rw /boot/firmware && \
    (grep -q '^auto_initramfs=1' /boot/firmware/config.txt || echo auto_initramfs=1 | sudo tee -a /boot/firmware/config.txt) && \
    echo 'overlayroot=\"tmpfs:recurse=0\"' | sudo tee /etc/overlayroot.conf >/dev/null && \
    sudo update-initramfs -u -k all"

  log "GATE: verify the initramfs actually built and is referenced (else refuse to reboot)"
  "${SSH[@]}" 'set -e
    img=$(ls -1 /boot/firmware/initramfs* 2>/dev/null | head -1)
    [ -n "$img" ] && [ -s "$img" ] || { echo "NO initramfs image built"; exit 1; }
    grep -q "^auto_initramfs=1" /boot/firmware/config.txt || { echo "config.txt not referencing initramfs"; exit 1; }
    grep -q "^overlayroot=" /etc/overlayroot.conf || { echo "overlayroot.conf not set"; exit 1; }
    echo "  initramfs OK: $img ($(stat -c%s "$img") bytes)"' \
    || { echo "ERROR: pre-reboot gate FAILED — overlay left OFF, safe to retry. NOT rebooting."; exit 1; }

  log "removing any overlayroot=disabled maintenance override"
  set_overlay enabled

  log "gate passed — rebooting into overlay"
  reboot_pi
  if overlay_on; then
    log "SUCCESS: root is now read-only overlay ($("${SSH[@]}" 'findmnt -no FSTYPE /'))"
    "${SSH[@]}" 'touch /ro-test 2>&1 | head -1 || true; test ! -e /ro-test && echo "  confirmed: write to / is rejected"'
  else
    echo "WARNING: booted but overlay is not active — check /etc/overlayroot.conf and cmdline (overlayroot=disabled?)"
    exit 1
  fi
}

disable_overlay_cmd() {
  overlay_on || { log "overlay already OFF"; return 0; }
  log "disabling overlay for maintenance"; set_overlay disabled; reboot_pi
  overlay_on && { echo "ERROR: overlay still on"; exit 1; }
  log "overlay OFF — root is writable. Re-enable with: $0 $PI --enable-overlay"
}

main() {
  case "$MODE" in
    enable)  enable_overlay; exit 0 ;;
    disable) disable_overlay_cmd; exit 0 ;;
  esac
  if overlay_on; then
    log "overlay is ON (prod). Entering maintenance mode."
    preflight_persistent_ssh
    set_overlay disabled; reboot_pi
    overlay_on && { echo "ERROR: overlay still on after disable"; exit 1; }
    push_and_restart
    log "re-enabling overlay"
    set_overlay enabled; reboot_pi
    overlay_on || { echo "ERROR: overlay did not re-enable"; exit 1; }
    verify
  else
    log "overlay is OFF (dev). Fast deploy, no reboot."
    push_and_restart
    verify
  fi
  log "done."
}
main
