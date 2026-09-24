See [`CLAUDE.md`](CLAUDE.md) for the complete agent instructions, repository map, evidence
precedence, gap-closing workflow, and validation commands. Its instructions are mandatory.

Before changing the impersonator, read these in order:

1. [`CLAUDE.md`](CLAUDE.md)
2. [`docs/firmware-implementation-gap-tracker.md`](docs/firmware-implementation-gap-tracker.md)
3. The relevant sections of [`docs/protocol.md`](docs/protocol.md) and
   [`docs/dead-ends.md`](docs/dead-ends.md)

Work against a named `GAP-*` item. Direct 3.1.6 decompiler evidence cited by the tracker takes
precedence over conflicting older prose. Do not guess fields marked `descriptor required`, and do
not close a gap without its tests and evidence record.

Local source edits and tests do not authorize deployment, live backend mutation, token rotation,
reboots, or firmware flashing. Perform those only when the user explicitly requests them. Live
Pi/camera access is in the git-ignored `.agent/pi-ops.md` (template:
`.agent/pi-ops.example.md`).

---

## Developing in the loop with real hardware

The appliance is exercised on a real **Pi Zero 2 W + OV5647 CSI camera**. Offline
tests and `validate-image.sh` pass on things that fail live, so iterate on the
device — but keep hardware and repo in sync (below). The full field record is
[`docs/hardware-bring-up-lessons.md`](docs/hardware-bring-up-lessons.md); read it
before touching the image layer, units, camera path, or NetworkManager config.

### The loop (preferred: patch the running device, then commit to the repo)

1. Make the change in the repo (source/units/image assets) with its tests.
2. Push the changed file(s) to the running device and restart the affected unit.
   ROOT is read-only, so remount first:
   ```sh
   mount -o remount,rw /
   cat > /opt/prusa-cam/<file>      # pipe the repo file over SSH
   systemctl restart <unit>         # e.g. prusa-cam prusa-admin prusa-rtsp rpicam-source
   ```
   Config files under `/data/prusa-cam/config/` **must stay `prusa-cam`-owned**
   (`0640` `device.toml`, `0600` `secrets.toml`). A root-side write makes the app
   read an empty document and Prusa Connect rejects everything. After any root
   edit: `chown prusa-cam:prusa-cam /data/prusa-cam/config/*`.
3. Verify live (snapshot, RTSP, admin UI, `journalctl -u <unit>`).
4. Commit the repo change in the same session.

### Keeping hardware and repo in sync (mandatory)

- **Every on-device edit must land in the repo in the same session.** If you patch
  a file on the device, commit the identical change to the repo before finishing.
- Anything the device needs that is not produced by the image — a udev rule, an
  NM conf drop-in, a `config.txt` line, a unit change, a package — is a **repo
  bug**: add it to the image so the next flash persists it.
- Do not rely on the device's remounted-rw root: it reverts to read-only on
  reboot; correctness must come from the image.

### Build + flash (only when the change must persist / be validated on a fresh card)

- Build host: `bullitt@rpi5.stahmer.lan` (native arm64). Sync the repo there, run
  `image/scripts/build-image.sh`, then `image/scripts/validate-image.sh` (run as
  root with a full `PATH` so `dumpe2fs`/`mtools` resolve; `--mount-root` is a
  mounted `root.ext4`).
- Flashing and card moves are a **user action** (SD card in the USB reader); the
  device then re-onboards from scratch.

### Diagnosing without a console

- `bootlog.sh` persists unit states, `nmcli`, camera detection (`vcgencmd
  get_camera`, `rpicam-hello --list-cameras`), `/data` state and the relevant
  journals to `/boot/firmware/bootlog.txt` (FAT — readable on any PC). It is also
  `WantedBy=prusa-camera.target`, so the claim→runtime boot is captured.
- The journal is volatile; capture it live or via `bootlog.txt`.

### Hardware lessons (highlights — full list in the lessons doc)

- Config-file ownership is load-bearing (empty-token → Connect rejects).
- NetworkManager randomizes the wlan0 MAC during scans; any MAC-derived identity
  must disable it (`wifi.scan-rand-mac-address=no`) or persist the value.
- The image installs without recommends: NM's `dnsmasq-base` (setup hotspot) and
  `nftables`/`iptables` (shared NAT) must be added explicitly.
- `overlayroot` did not activate; ROOT stays read-only and volatile state lives on
  tmpfs (`/var`, `/etc/prusa-cam`).
- libcamera is single-consumer: probe only pre-runtime; snapshots come from the
  `stream_mux` TCP fan-out, never a second `rpicam` capture while the source runs.
- MBR PARTUUIDs are zero-padded (`b33dcafe-03`), so compare numerically.
- The **Prusa Connect live view (WebRTC) is gated by Prusa's camera-service
  registry** (`GET camera-service-api.prusa3d.com/v1/cameras/<token>` → 404);
  viewers get `client_authentication` ACK 5 and never answer, so the camera ends
  at `no-ice-connection`. Not fixable from the device; snapshots are not gated.

### Authorization during a hardware session

The rule above still holds: no flashing, reboots, token rotation, or live backend
mutation without the user's explicit request. When authorized, say what you are
about to do before a disruptive action (reboot, stopping the setup AP), and
remember that stopping the AP before the station link is proven drops the device
off the network.
