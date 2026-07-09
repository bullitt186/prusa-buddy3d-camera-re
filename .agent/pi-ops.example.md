# Pi & Camera Operations Runbook — TEMPLATE

> Copy to `pi-ops.md` (git-ignored) and fill in your real host/network details.
> The filled-in `pi-ops.md` is never committed.

## The devices

| Device | What | Access |
|---|---|---|
| **Pi** | Raspberry Pi Zero 2 W, Debian 13 (trixie). Runs the impersonator. | `ssh <PI_USER>@<PI_IP>` (SSH **key** auth, no password) |
| **Camera** | Prusa Buddy3D Camera, firmware 3.1.5 (Rockchip, ARM). The RE target. | via Prusa Connect / SD-card overlay |

App lives on the Pi at `~/prusa-cam/` (== `/home/<PI_USER>/prusa-cam/`). Repo source of truth is
`pi-impersonator/` in this checkout.

## SSH quick checks

```bash
PI=<PI_USER>@<PI_IP>   # fill in your Pi
ssh $PI 'systemctl is-active rpicam-source prusa-rtsp prusa-cam'   # expect: active active active
ssh $PI 'journalctl -u prusa-cam -n 50 --no-pager'                 # impersonator log
ssh $PI 'journalctl -fu prusa-cam'                                 # follow live
```

## Deploy code changes (repo → Pi)

The repo code is redacted/portable; `config.ini` (the live secret) stays only on the Pi, so
**exclude it** from any sync.

```bash
PI=<PI_USER>@<PI_IP>   # fill in your Pi
cd ~/Documents/Repositories/prusa-buddy3d-camera-re

# back up what's running, then push the .py files (never config.ini / venv / systemd)
ssh $PI 'mkdir -p ~/prusa-cam/backups/$(date +%Y%m%d_%H%M%S) && cp ~/prusa-cam/*.py "$_"'
rsync -av --exclude 'config.ini' --exclude 'venv/' --exclude '__pycache__/' \
      --exclude 'backups/' --exclude 'systemd/' --exclude '*.example' --exclude 'README.md' \
      pi-impersonator/ $PI:~/prusa-cam/

ssh $PI 'sudo systemctl restart prusa-cam prusa-rtsp'
ssh $PI 'journalctl -u prusa-cam -n 30 --no-pager'    # confirm "/c/info upload: 200"
```

Systemd units currently on the Pi use `User=<PI_USER>` and `/home/<PI_USER>/...` paths (the repo
`pi-impersonator/systemd/*.service` are `pi`/`/home/pi` templates — adjust if you reinstall).
`main.py` now loads `config.ini` next to itself, so deploy path no longer matters.

## Service management

```bash
# three units, in dependency order:
#   rpicam-source.service  -> rpicam-vid H264 to tcp://0.0.0.0:8888
#   prusa-rtsp.service     -> rtsp_server.py, rtsp://<pi>:8554/live  (toggled by main.py)
#   prusa-cam.service      -> main.py (registers to Prusa, uploads, signaling/WebRTC)
ssh $PI 'sudo systemctl restart prusa-cam'
ssh $PI 'sudo systemctl stop prusa-rtsp && sudo systemctl start prusa-rtsp'
vlc rtsp://<PI_IP>:8554/live      # verify local stream
```

## Rotate / change the registration token

Token + fingerprint live in `~/prusa-cam/config.ini` (`[identity]`). To re-register or change
origin, mint a new token in Prusa Connect (which "add camera" flow you use fixes the origin —
see the `prusa-connect-origin-mapping` note), then:

```bash
ssh $PI 'nano ~/prusa-cam/config.ini && sudo systemctl restart prusa-cam'
```

## Flash / reimage the Pi (SD card)

Standard Raspberry Pi OS Lite 64-bit via `rpi-imager` (enable SSH + Wi-Fi in imager settings).
After first boot, recreate the deployment:

```bash
ssh $PI 'sudo apt update && sudo apt install -y python3-gi python3-gst-1.0 gstreamer1.0-tools \
    gstreamer1.0-plugins-{base,good,bad} gstreamer1.0-rtsp rpicam-apps'
# then follow pi-impersonator/README.md "Install" (venv --system-site-packages, pip deps,
# cp config.ini.example config.ini, install systemd/*.service)
```

## Update the CAMERA firmware / OTA  ⚠️ destructive

`research/RK_OTA_update.sh` is the Rockchip on-device updater: for each `/dev/block/by-name/*`
it finds a matching `<name>.img` and does `flash_eraseall` + `nandwrite`, then erases `misc`.
This **overwrites the camera's NAND partitions** — a bad image bricks the camera. Only run on
the camera itself (not the Pi), with known-good images, and a recovery plan. Prefer letting the
camera OTA itself via `connect-ota.prusa3d.com` unless you specifically need a custom image.

## Backups on the Pi

Rolling backups already exist under `~/prusa-cam/backups/` (timestamped dirs + a few
`*_pre_*` snapshots). The deploy step above adds a fresh one each time.
