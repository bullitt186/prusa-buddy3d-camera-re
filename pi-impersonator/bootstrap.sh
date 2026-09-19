#!/usr/bin/env bash
# One-command fresh-Pi provisioning for the impersonator — use after a reflash or SD recovery.
# Installs apt deps, builds the venv, installs+enables the 3 systemd units, and deploys the code.
# After it finishes: drop config.ini (the token) and the camera registers. Then optionally lock
# the SD read-only with:  ./deploy.sh $PI --enable-overlay
#
# Usage:  PI=user@host ./bootstrap.sh    (or ./bootstrap.sh user@host)
# Portable — no host hard-coded. Real host lives in .agent/pi-ops.md.
set -euo pipefail

PI="${1:-${PI:-}}"
[ -n "$PI" ] || { echo "usage: PI=user@host $0"; exit 2; }
PI_USER="${PI%@*}"
SRC="$(cd "$(dirname "$0")" && pwd)"
SSH=(ssh -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new "$PI")
log() { printf '\n\033[1m» %s\033[0m\n' "$*"; }

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
  python3-venv python3-pip rsync'

log "python venv (--system-site-packages so gi/Gst are visible) + pip deps"
"${SSH[@]}" 'mkdir -p ~/prusa-cam && python3 -m venv --system-site-packages ~/prusa-cam/venv && \
  ~/prusa-cam/venv/bin/pip install -q --upgrade pip aiohttp python-socketio'

log "install + enable systemd units (template User=pi/home/pi → $PI_USER)"
for u in rpicam-source prusa-rtsp prusa-cam; do
  sed -e "s/^User=pi\$/User=$PI_USER/" -e "s#/home/pi/#/home/$PI_USER/#g" "$SRC/systemd/$u.service" \
    | "${SSH[@]}" "sudo tee /etc/systemd/system/$u.service >/dev/null"
done
"${SSH[@]}" 'sudo systemctl daemon-reload && sudo systemctl enable rpicam-source prusa-rtsp prusa-cam'

log "deploy code + provision /etc/prusa-cam + quality.env (reuses deploy.sh)"
"$SRC/deploy.sh" "$PI" || true   # prusa-cam will crash-loop until config.ini exists — that's fine

cat <<EOF

$(printf '\033[1m✓ bootstrap done.\033[0m')  Final manual step — the token secret is not in the repo:
  scp config.ini  $PI:~/prusa-cam/config.ini      # or re-mint a token in Prusa Connect
  ssh $PI 'sudo systemctl restart prusa-cam && journalctl -u prusa-cam -n 20 --no-pager'
  # expect: /c/info response … registered=True, and Snapshot: 200

Once it's confirmed registering, lock the SD read-only:
  $SRC/deploy.sh $PI --enable-overlay
EOF
