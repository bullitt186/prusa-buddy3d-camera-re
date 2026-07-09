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

## Prerequisites

- Raspberry Pi (tested: **Pi Zero 2 W**) with a camera module, Raspberry Pi OS Lite 64-bit.
- A camera **registration token** from Prusa Connect (Web UI or app → Camera → *Token* / the
  "add camera" QR screen). This is what authenticates the device as the camera.
- System packages for GStreamer + PyGObject:
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

# 2. venv MUST see system GStreamer/PyGObject bindings
python3 -m venv venv --system-site-packages
./venv/bin/pip install aiohttp aioice   # + any other imports your build needs

# 3. Configure (real secrets stay out of git)
cp config.ini.example config.ini
# edit config.ini: set token = <your registration token>, pick a random 32-hex fingerprint
```

## Run

Manual:
```bash
./venv/bin/python main.py
```

As a service (two units — the RTSP server is separate so `main.py` can start/stop it on
command from Prusa):

```ini
# /etc/systemd/system/prusa-cam.service
[Unit]
Description=Prusa Camera Impersonator
After=network-online.target
Wants=network-online.target
[Service]
AmbientCapabilities=CAP_NET_BIND_SERVICE   # bind :80 without root
Type=simple
User=pi
WorkingDirectory=/home/pi/prusa-cam
ExecStart=/home/pi/prusa-cam/venv/bin/python main.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now prusa-cam.service
journalctl -fu prusa-cam.service         # watch it register + upload
```

`main.py` also invokes `sudo systemctl start/stop prusa-rtsp.service` — create a companion unit
that runs `rtsp_server.py` (or fold it into your setup), and grant the `pi` user a sudoers
rule for just those two `systemctl` calls if you keep them separate.

## Verify

- `journalctl` shows `/c/info upload: 200`.
- The camera appears **online** in Prusa Connect with name, IP, MAC, SSID, firmware `3.1.5`.
- Local stream: `vlc rtsp://<PI_IP>:8554/live`.

See [`../docs/status.md`](../docs/status.md) for the current end-to-end caveats and
[`../docs/next-steps.md`](../docs/next-steps.md) for open items.
