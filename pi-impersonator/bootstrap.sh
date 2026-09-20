#!/usr/bin/env bash
# One-command fresh-Pi provisioning for the impersonator — use after a reflash or SD recovery.
# Installs apt deps, creates the dedicated service account, builds the venv under $APP_ROOT,
# installs/enables the runtime units verbatim, and deploys the code.
# After it finishes: drop config.ini (the token) and the camera registers. Then optionally lock
# the SD read-only with:  ./deploy.sh $PI --enable-overlay
#
# Usage:  PI=user@host ./bootstrap.sh    (or ./bootstrap.sh user@host)
# Portable — no host hard-coded. Real host lives in .agent/pi-ops.md.
set -euo pipefail

PI="${1:-${PI:-}}"
[ -n "$PI" ] || { echo "usage: PI=user@host $0"; exit 2; }
PI_USER="${PI%@*}"
SERVICE_USER="${SERVICE_USER:-prusa-cam}"
APP_ROOT="${APP_ROOT:-/opt/prusa-cam}"
SRC="$(cd "$(dirname "$0")" && pwd)"
SSH=(ssh -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new "$PI")
log() { printf '\n\033[1m» %s\033[0m\n' "$*"; }

cat <<'EOF'
NOTE: bootstrap.sh is a DEVELOPER / migration convenience only. It is NOT the
supported public appliance installation path — that is the signed SD-card image,
which also provisions the partition table, recovery, identity, and updates.
EOF

log "overlay must be OFF to provision a persistent system (writes must hit the real disk)"
if "${SSH[@]}" 'findmnt -no FSTYPE / | grep -q overlay'; then
  echo "ERROR: overlay is ON. Run:  $SRC/deploy.sh $PI --disable-overlay   then re-run bootstrap."
  exit 1
fi

log "apt: gstreamer + libcamera/rpicam + python-gi + venv tooling"
"${SSH[@]}" 'sudo apt-get update && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3-gi python3-gst-1.0 gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-nice gstreamer1.0-rtsp rpicam-apps \
  gir1.2-gst-rtsp-server-1.0 gir1.2-gst-plugins-bad-1.0 \
  samba \
  python3-venv python3-pip rsync curl'

log "create $SERVICE_USER service account + app root $APP_ROOT"
"${SSH[@]}" "set -e
  id -u $SERVICE_USER >/dev/null 2>&1 || \
    sudo useradd --system --create-home --home-dir $APP_ROOT --shell /usr/sbin/nologin $SERVICE_USER
  sudo usermod -aG video $SERVICE_USER
  sudo install -d -o $SERVICE_USER -g $SERVICE_USER $APP_ROOT"

log "python venv under $APP_ROOT (--system-site-packages so gi/Gst are visible) + pip deps"
"${SSH[@]}" "sudo python3 -m venv --system-site-packages $APP_ROOT/venv && \
  sudo $APP_ROOT/venv/bin/pip install -q --upgrade pip aiohttp python-socketio && \
  sudo chown -R $SERVICE_USER:$SERVICE_USER $APP_ROOT"

log "install + enable systemd units (verbatim: User=prusa-cam / /opt/prusa-cam)"
for u in rpicam-source.service prusa-rtsp.service prusa-ha-rtsp.service \
         prusa-cam.service pi-persist.service prusa-data-ready.service data-ready.target; do
  "${SSH[@]}" "sudo install -m 0644 /dev/stdin /etc/systemd/system/$u" < "$SRC/systemd/$u"
done
"${SSH[@]}" 'sudo systemctl daemon-reload && sudo systemctl enable data-ready.target prusa-data-ready.service rpicam-source prusa-rtsp prusa-ha-rtsp prusa-cam pi-persist'

log "deploy code + provision /etc/prusa-cam + quality.env (reuses deploy.sh)"
"$SRC/deploy.sh" "$PI" || true   # prusa-cam will crash-loop until config.ini exists — that's fine

cat <<EOF

$(printf '\033[1m✓ bootstrap done.\033[0m')  Final manual step — the token secret is not in the repo:
  scp config.ini $PI:/tmp/config.ini
  ssh $PI "sudo install -o $SERVICE_USER -g $SERVICE_USER -m 0600 /tmp/config.ini $APP_ROOT/config.ini && rm -f /tmp/config.ini"
  ssh $PI 'sudo systemctl restart prusa-cam && journalctl -u prusa-cam -n 20 --no-pager'
  # expect: /c/info response … registered=True, and Snapshot: 200

Once it's confirmed registering, lock the SD read-only:
  $SRC/deploy.sh $PI --enable-overlay
EOF
