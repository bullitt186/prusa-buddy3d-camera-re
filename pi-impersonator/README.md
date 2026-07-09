# Pi Camera Impersonator (Python)

Runs on a Raspberry Pi and registers to Prusa Connect **as a genuine Buddy3D camera**.
It authenticates with a camera registration token, uploads camera-info (`/c/info`) and
snapshots, answers Socket.IO signaling, and negotiates WebRTC to stream the Pi's camera.

This is the primary reverse-engineered implementation. Status (what's confirmed vs. still
failing end-to-end) is in [`../docs/status.md`](../docs/status.md). The protocol it speaks is
[`../docs/protocol.md`](../docs/protocol.md).

## Files

| File | Role |
|---|---|
| `main.py` | Entry point: loads `config.ini`, uploads `/c/info`, starts signaling + WebRTC + snapshot loop + local HTTP |
| `signaling.py` | Socket.IO / Engine.IO client to `camera-signaling.prusa3d.com` |
| `webrtc.py` | WebRTC offer/answer + RTP from the local RTSP feed |
| `upload.py` | `/c/info` and snapshot HTTP upload to `webcam.connect.prusa3d.com` |
| `proto.py` | Minimal protobuf encode/decode (nanopb-compatible wire format) |
| `camera.py` | JPEG capture |
| `rtsp_server.py` | GStreamer RTSP server → `rtsp://0.0.0.0:8554/live` |
| `local_http.py` | Local HTTP endpoint (port 80) |
| `features.py` | Camera feature/capability advertisement |

## Architecture (three services)

```
rpicam-source.service   rpicam-vid → H264 over TCP :8888   (raw camera)
        │
prusa-rtsp.service      rtsp_server.py → rtsp://<pi>:8554/live   (GStreamer RTSP)
        │
prusa-cam.service       main.py → Prusa Connect: /c/info, snapshots, signaling, WebRTC
```

`main.py` can start/stop `prusa-rtsp.service` on command from Prusa. Ready-to-install unit
files are in [`systemd/`](systemd/).

## Prerequisites

- Raspberry Pi (tested: **Pi Zero 2 W**) with a camera module, Raspberry Pi OS Lite 64-bit.
- A camera **registration token** from Prusa Connect (Web UI or app → Camera → *Token* / the
  "add camera" QR screen). This is what authenticates the device as the camera.
- System packages for GStreamer + PyGObject + camera:
  ```bash
  sudo apt update
  sudo apt install -y python3-gi python3-gst-1.0 gstreamer1.0-tools \
      gstreamer1.0-plugins-{base,good,bad} gstreamer1.0-rtsp rpicam-apps
  ```

## Install

```bash
# 1. Copy the source to the Pi
scp -r pi-impersonator/ pi@<PI_IP>:~/prusa-cam
cd ~/prusa-cam   # on the Pi

# 2. venv MUST see system GStreamer/PyGObject bindings (gi comes from the system)
python3 -m venv venv --system-site-packages
./venv/bin/pip install aiohttp "python-socketio[client]" python-engineio \
    simple-websocket requests cryptography pycryptodomex

# 3. Configure (real secrets stay out of git)
cp config.ini.example config.ini
# edit config.ini: set token = <your registration token>, pick a random 32-hex fingerprint
```

## Run

Manual:
```bash
./venv/bin/python main.py
```

As services — install all three units from [`systemd/`](systemd/):

```bash
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rpicam-source.service prusa-rtsp.service prusa-cam.service
journalctl -fu prusa-cam.service         # watch it register + upload
```

Because `main.py` toggles `prusa-rtsp.service` via `sudo systemctl`, give the `pi` user a
sudoers rule scoped to just that:

```
pi ALL=(root) NOPASSWD: /bin/systemctl start prusa-rtsp.service, /bin/systemctl stop prusa-rtsp.service
```

## Verify

- `journalctl` shows `/c/info upload: 200`.
- The camera appears **online** in Prusa Connect with name, IP, MAC, SSID, firmware `3.1.5`.
- Local stream: `vlc rtsp://<PI_IP>:8554/live`.

See [`../docs/status.md`](../docs/status.md) for the current end-to-end caveats and
[`../docs/next-steps.md`](../docs/next-steps.md) for open items.
