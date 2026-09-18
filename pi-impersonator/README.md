# Pi Camera Impersonator

Runs on a Raspberry Pi and registers to Prusa Connect **as a genuine Buddy3D camera**.
It authenticates with a camera registration token, uploads camera-info and snapshots,
speaks the Socket.IO signaling protocol, streams H.264 via local RTSP, and negotiates
WebRTC. The root filesystem is locked read-only with overlayfs so abrupt power cuts
(the Pi powers on/off with the printer) can't corrupt the SD card.

Protocol spec: [`../docs/protocol.md`](../docs/protocol.md) —
what works vs. what's still gated: [`../docs/status.md`](../docs/status.md) —
implementation backlog: [`../docs/firmware-implementation-gap-tracker.md`](../docs/firmware-implementation-gap-tracker.md).

## Hardware

- **Raspberry Pi** — tested on **Pi Zero 2 W** (512 MB RAM). Any Pi with a CSI port works.
- **Camera module** — tested with **OV5647** (Pi Camera v1, 5 MP). Any `libcamera`-supported
  module works; adjust `--rotation` in `systemd/rpicam-source.service` for your physical mount.
- **MicroSD** — 8 GB minimum, 16 GB recommended.

## Quick start — one command

Flash **Raspberry Pi OS Lite 64-bit** via `rpi-imager` (enable SSH + Wi-Fi in settings,
username `pi`). Then from this repo on your machine:

```bash
PI=pi@<PI_IP> pi-impersonator/bootstrap.sh
```

`bootstrap.sh` installs all apt deps, builds the venv, installs and enables the three
systemd units, and deploys the code. When it finishes, drop in your `config.ini`:

```bash
scp pi-impersonator/config.ini.example pi@<PI_IP>:~/prusa-cam/config.ini
ssh pi@<PI_IP> 'nano ~/prusa-cam/config.ini'   # set token
ssh pi@<PI_IP> 'sudo systemctl restart prusa-cam'
```

Then lock the SD read-only (strongly recommended — Pi cuts power with the printer):

```bash
PI=pi@<PI_IP> pi-impersonator/deploy.sh --enable-overlay
```

## Configuration

`config.ini` (copy from `config.ini.example`, **never commit** — it holds a live secret):

| Key | Value |
|---|---|
| `token` | Camera registration token from Prusa Connect (Web UI → Camera → *Token*) |
| `interval` | Snapshot upload interval in seconds, `10`–`600` (default `10`) |

Resolution is not configured here: it follows the persisted video-quality tier
(`/etc/prusa-cam/quality.env`) and the ephemeral live override
(`/etc/prusa-cam/quality.live.env`), so snapshots, RTSP, WebRTC and status always agree.

The fingerprint is generated automatically from `wlan0` exactly like firmware 3.1.6: normalize
the MAC as uppercase colon-separated ASCII and send its lowercase MD5 digest. Connect binds this
fingerprint on a token's first use, so use a fresh token when migrating from an older deployment
that configured a static fingerprint.

## Architecture

Three systemd services, one concern each:

```
rpicam-source.service   rpicam-vid -o - | stream_mux.py → H.264 TCP :8888 (multi-client)
        │                 resolution driven by quality.env (persisted) + quality.live.env (live override)
        │                 --rotation 180  --intra 30  --flush
        ↓
prusa-rtsp.service      rtsp_server.py (GStreamer) → rtsp://<pi>:8554/live
        │
prusa-cam.service       main.py
                          /c/info upload · snapshot loop · Socket.IO signaling · WebRTC
```

`main.py` starts/stops `prusa-rtsp` on command from Prusa and reconfigures the encoder
resolution live on video-quality commands (SD 640×480 / HD 1280×720 / FHD 1920×1080) by
writing the ephemeral `/etc/prusa-cam/quality.live.env` and restarting `rpicam-source`;
a persistence flag (whose event wiring is still being recovered, see `GAP-QUALITY-02`) also
writes `/etc/prusa-cam/quality.env` for the next boot.

## Files

| File | Role |
|---|---|
| `main.py` | Entry point: config, `/c/info`, snapshot loop, signaling, WebRTC, quality handler |
| `signaling.py` | Socket.IO/Engine.IO client → `camera-signaling.prusa3d.com` |
| `webrtc.py` | WebRTC offer/answer via GStreamer `webrtcbin` |
| `upload.py` | HTTP uploads → `webcam.connect.prusa3d.com` |
| `identity.py` | Firmware-faithful MAC normalization and fingerprint derivation |
| `proto.py` | Minimal protobuf encode/decode (nanopb wire format) |
| `camera.py` | JPEG snapshot via `gst-launch-1.0` reading from `stream_mux.py` on port 8888 (avoids fighting `rpicam-vid` for the sensor — libcamera is single-consumer) |
| `rtsp_server.py` | GStreamer `GstRtspServer` → `rtsp://0.0.0.0:8554/live` |
| `local_http.py` | Local HTTP on port 80 |
| `features.py` | Camera feature/capability advertisement |
| `quality.py` | Video-quality tier state (SD/HD/FHD) — atomic writes, crash-safe |
| `stream_mux.py` | TCP broadcast mux: fans the H264 stream from `rpicam-vid` out to multiple clients (RTSP server, snapshot code) on port 8888. libcamera is single-consumer; this replaces the old `--listen` single-client model. |
| `config.ini.example` | Config template |
| `deploy.sh` | Overlay-aware deploy helper (dev: fast rsync; prod: maintenance dance) |
| `bootstrap.sh` | One-command fresh-Pi provisioning |
| `systemd/` | Ready-to-install unit files (`User=pi` templates — `bootstrap.sh` adapts them) |

## Local development

The source-level tests use only the Python standard library and run without Pi/GStreamer runtime
packages:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q pi-impersonator tests
```

Firmware-parity work must name and update a `GAP-*` item in the implementation tracker. Follow the
evidence precedence and completion workflow in [`../CLAUDE.md`](../CLAUDE.md); in particular, do
not invent fields whose descriptor is still marked as required.

For coding agents, repository edits and these tests are local-only. They do not authorize running
the deployment/bootstrap commands below, changing a Connect token, or operating a physical device.
Those actions require an explicit user request and the private `.agent/pi-ops.md` runbook.

## Deployment

The root filesystem runs as **read-only overlayfs** — runtime writes go to RAM and are
discarded on reboot. A bare `rsync` or `nano` on a running Pi is silently lost. Always deploy
via `deploy.sh`:

```bash
# From repo root:
PI=pi@<PI_IP> pi-impersonator/deploy.sh
# dev (overlay OFF):  rsync + restart, no reboot, ~5 s
# prod (overlay ON):  disable → reboot → rsync → re-enable → reboot, ~3 min (automated)
```

Check mode: `ssh pi@<PI_IP> 'findmnt -no FSTYPE /'` → `overlay` = prod, `ext4` = dev.

Maintenance window (apt installs, `/etc` edits):

```bash
PI=pi@<PI_IP> pi-impersonator/deploy.sh --disable-overlay
# … make changes …
PI=pi@<PI_IP> pi-impersonator/deploy.sh --enable-overlay   # verifies initramfs before rebooting
```

**Intentionally ephemeral** (resets on reboot — don't fight it):
- `/etc/prusa-cam/quality.env` — video tier resets to FHD; that's fine
- journald logs — in RAM (`Storage=volatile`); read with `journalctl -u prusa-cam`

## Manual install (without bootstrap.sh)

```bash
# 1. Apt dependencies
sudo apt update && sudo apt install -y \
    python3-gi python3-gst-1.0 \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-rtsp \
    gir1.2-gst-rtsp-server-1.0 gir1.2-gst-plugins-bad-1.0 \
    rpicam-apps python3-venv python3-pip rsync
# Note: gir1.2-gst-rtsp-server-1.0 and gir1.2-gst-plugins-bad-1.0 are required for
# GstRtspServer and openh264dec Python bindings — not pulled by gstreamer1.0-plugins-bad alone.

# 2. Venv — must use --system-site-packages so gi/GStreamer are visible
python3 -m venv ~/prusa-cam/venv --system-site-packages
~/prusa-cam/venv/bin/pip install aiohttp "python-socketio[client]"

# 3. Copy source, configure
cp pi-impersonator/* ~/prusa-cam/
cp ~/prusa-cam/config.ini.example ~/prusa-cam/config.ini
# edit config.ini: token

# 4. Systemd units (adjust User= to your username)
sed "s/^User=pi$/User=$USER/; s|/home/pi/|/home/$USER/|g" \
    pi-impersonator/systemd/rpicam-source.service \
    | sudo tee /etc/systemd/system/rpicam-source.service
# repeat for prusa-rtsp.service and prusa-cam.service
sudo systemctl daemon-reload
sudo systemctl enable --now rpicam-source prusa-rtsp prusa-cam

# 5. /etc/prusa-cam for quality tier state
sudo install -d -o $USER -g $USER /etc/prusa-cam
printf "CAM_WIDTH=1920\nCAM_HEIGHT=1080\n" > /etc/prusa-cam/quality.env

# 6. Sudoers for quality tier-switching (main.py restarts rpicam-source + prusa-rtsp)
#    bootstrap.sh uses NOPASSWD: ALL; for a tighter rule use this instead:
echo "$USER ALL=(ALL) NOPASSWD: /bin/systemctl restart rpicam-source.service prusa-rtsp.service, /bin/systemctl start prusa-rtsp.service, /bin/systemctl stop prusa-rtsp.service" \
    | sudo tee /etc/sudoers.d/prusa-cam
```

## Verify

```bash
ssh pi@<PI_IP> 'journalctl -u prusa-cam -n 30 --no-pager'
# expect lines like:
#   /c/info upload: 200
#   /c/info response: … registered=True …
#   Snapshot: 200 (… bytes, …ms)
```

Local RTSP: `vlc rtsp://<PI_IP>:8554/live`

## Latency notes

Measured end-to-end (from this setup):

- **Server-side first-frame: ~70 ms** (warm, after the first client connect)
- **Cold join: ~2.5 s** (one-time on first boot — `stream_mux.py` needs to accumulate the first IDR bootstrap block before it serves new clients; subsequent connections are instant)
- **What you see in VLC: ~1 s** — this is VLC's `network-caching` default (1000 ms), not the Pi.
  Use `vlc --network-caching=100 rtsp://…` to see the true ~70 ms server latency.

## Recovery

If the Pi won't boot after a power cut (rare before overlay is enabled; impossible after):

1. Reflash the SD card (Raspberry Pi OS Lite 64-bit, same settings as before).
2. Run `PI=pi@<PI_IP> pi-impersonator/bootstrap.sh` to rebuild everything.
3. Restore `config.ini` (token) and restart `prusa-cam`.
4. Re-lock: `PI=pi@<PI_IP> pi-impersonator/deploy.sh --enable-overlay`.
