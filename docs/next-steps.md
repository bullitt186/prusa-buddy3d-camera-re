# Next Steps Plan — Prusa Buddy3D Camera Impersonator

**Created:** 2026-07-07. **Revised 2026-09-18** after tracing token and fingerprint provenance
through the complete 3.1.6 decompilation. Earlier revisions incorporated Prusa's official Buddy3D pairing
manual and PrusaLink camera guide — see `status.md`'s Bottom line for the full story.
**Firmware follow-up (2026-09-17):** 3.1.6 was compared directly with 3.1.5 and contains no
cloud-protocol change; see [`firmware-3.1.6.md`](firmware-3.1.6.md).
**Context:** Camera impersonator works (snapshots, Socket.IO auth, `/c/info`, RTSP) but the
mobile app never sends WebRTC offers and shows "Kamera-Kommunikation Fehlgeschlagen".
The immediate software-only lead is an exact MAC/fingerprint consistency retest with a fresh
token. Firmware hashes an uppercase, colon-separated MAC preimage; the 2026-07-09 one-off test did
not preserve its preimage/casing and is not reproducible. The other remaining routes are backend
rollout/enrollment, real Buddy3D hardware for comparison, or SSL-unpinning on a jailbroken client.

---

## CURRENT (2026-09-19) — live WebRTC works; finish SD/timelapse

The sections below are historical. **Superseded:** WebRTC live view now works
(verified live). The old "backend gate / no offer" framing is wrong; it was four
firmware-parity bugs (auth field order + ACK `0`, TURN credentials, offerer
direction + candidate handling, and the H.264 SPS profile). See `status.md`'s
2026-09-19 section.

**Timelapse storage (live-confirmed in Connect 2026-09-19; capture/list round-trip
pending).** The app says *"Time lapse not available, camera storage not detected, insert
SD card"* and reads storage from the `status` message, block `extended_status.4`
(descriptor `0x3f72b0`).

- Firmware (descriptor re-trace, **[confirmed]**): `{1:<mounted 1|2>, 2:totalMB,
  3:freeMB, 4:usedMB, 5:<mode string>}` — tag1 `FUN_000744ac` (1=mounted, 2=absent),
  tags 2-4 `FUN_000745e0` (`statvfs` MB, `>> 20`), tag5 `FUN_00073914`
  (`"RW"`/`"RO"`/`"UNKNOWN"`). The earlier note's `FUN_000abcb0`/`FUN_000abaf4`
  getters belong to `timelapse_status` (field 2), not this block, and `MODEL` was
  never tag 5.
- Ours now: `timelapse.storage_status()` reports the emulated `/mnt/sdcard`
  (`{1:1,2:total,3:free,4:used,5:"RW"}` when present, `{1:2,2:0,3:0,4:0,5:"UNKNOWN"}`
  otherwise) and `signaling` supplies it. Confirm live that Connect stops reporting
  "storage not detected".

Already done for timelapse:
- Emulated SD at `/mnt/sdcard/timelapse` (the firmware path), shared over SMB
  (`\\<pi>\sdcard`, verified); `deploy.sh`/`bootstrap.sh` provision it.
- `MicroSd` re-advertised; `timelapse.py` storage + firmware-named frames
  (`timelapse_<HH-MM-SS-mmm>.jpg`), stdlib MJPEG-in-AVI `build_avi`
  (`timelapse_<HH-MM-SS-mmm>.avi` + `.timelapse_videos.csv`) and `timelapse_loop`;
  trigger tags 5/14/15 dispatched. The `0x3f701c` file-list envelope is annotated
  (event `file_list`; field 1 `"<page>;<total>\n<chunk>"`, field 2 token, field 3
  optional request_id, field 4 unset; empty list sends nothing) and implemented
  locally. Live 2026-09-19: frames record as `timelapse_<HH-MM-SS-mmm>.jpg` and
  `build_avi` on real frames yields a `file(1)`-confirmed MJPEG AVI; this app
  version has no make-video button or file-list view, so the `file_list` sender
  is not app-exercisable.

Also open (smaller):
- Wire the RTSP `configuration` field (`tag3.11`/`tag3.12`) through `rtsp_control`.
- Finish mapping the remaining `configuration` protobuf `tag3` subfields.
- **`file_list` envelope verification pending:** implemented + unit-tested and the make path
  is verified directly on the Pi, but this Connect version has no make-video button or
  file-list view, so the sender cannot be exercised through the app (only a raw Socket.IO
  replay would). See `GAP-TIMELAPSE-01`.
- **Reboot & throttling — investigated 2026-09-19, mitigations deployed.** Throttling is **thermal**
  (81–84 °C continuously, no heatsink/fan; `get_throttled` = `0x60002` → ARM frequency capped **now**
  and hard-throttle latched; **no under-voltage ever**) from the 1080p30 H.264 pipeline on a
  passively cooled Pi Zero 2 W. The ~21:07 reboot cause is **undeterminable** (volatile journal, no
  RTC, RAM overlay upper layer): most likely an external power cut or a 1-minute hardware-watchdog
  reset. Mitigations: `bootlog.service` persists boot reason + `get_rsts`/`get_throttled`/temp/dmesg
  to `/boot/firmware/bootlog.txt` each boot; `rpicam-source` now runs `rpicam-vid -v 0` so its
  ~30 lines/s frame stats stop flooding the volatile journal. **Hardware action needed:** heatsink/fan
  or a lighter stream (720p/15 fps). Also: `systemd-remount-fs` and the swap units fail, leaving
  **zero swap** on a 415 MB device — a plausible watchdog trigger to clean up.
- **Browser WebRTC works (live-verified 2026-09-19).** The candidate-extraction fix was the enabler:
  the viewer's trickle candidates are now all applied and ICE completes (`ICE connection state: 1/2/3`)
  with the page's `<video>` playing 1920×1080. The snapshot stall is fixed too (ICE failure/close/
  disconnect + a 30 s watchdog resume snapshots; verified). The intermittent
  `CameraIsNotSessionMemberError` is **fixed** (`ba48dc8`): it was the unsolicited post-auth burst
  (`send_sio_info` + `status` + `protobuf_version` + `features`); post-auth now sends nothing
  (firmware parity) and the session is stable with the answer relayed immediately.

Live access: `.agent/pi-ops.md` (git-ignored). Deployment requires the overlay
maintenance dance (`deploy.sh`); rsync-only edits are lost on reboot.

---


**Verdict (2026-07-09): closed — no unique identifier found on the `CameraInfoMessage` wire.**
P.1 traced the remaining extended-status sub-block; the answer is "a hardware-derived value is
sent, but it's a small, guessable model string, not an unforgeable serial." Full evidence in
`protocol.md`'s `extended_status` section and `status.md`'s "ruled out" table.

**P.2 (MAC/OUI and fingerprint consistency) is again the top priority (corrected 2026-09-18).** `origin:
LINK` was downgraded, then **both `origin` (WEB vs OTHER) and source-IP/network-reputation were
live-tested and conclusively ruled out** — see `status.md`'s "origin and network-reputation ruled
out by live experiment" section. A fresh, fully-correct-protocol `origin: OTHER` token got the
identical ACK `5` from two different networks. The exact 3.1.6 fingerprint algorithm now gives
one narrowly scoped retest reachable purely from software; mitmproxy remains blocked by pinning.

**Why this is a lead:** there is no user setting to enable/disable WebRTC, cameras are not tied
to a user account (they can be resold), and pairing is QR-based — so WebRTC eligibility likely
depends only on what the camera *reports about itself* (serial / MAC / HW identity, or a specific
field combination). The deployed instance still sends a **Pi-OUI MAC** and its previously bound
**static fingerprint**; repository code now derives the exact matching fingerprint but needs a
fresh token before deployment. It also sends an **invented HW string** (`NB.1.1.0` / `Pi Zero 2 W`), and **~90
`CameraInfoMessage` fields are un-mapped**. Provenance breakdown: see the "Current deployment
state" table in [`status.md`](status.md). Field-5 sub-field mechanics overlap with Step 5 below.

### P.1 — Map the remaining `CameraInfoMessage` fields (is a serial / HW-id transmitted?) — DONE 2026-07-09

Cheapest and most decisive — answers "does the camera put its serial on the wire at all?".

**Environment note:** `mcp__ghidrassist__*` (live Ghidra GUI MCP) was not running/configured
this session (`127.0.0.1:8080` refused, no `.mcp.json`, no Ghidra process). Used the headless
`analyzeHeadless` fallback instead (`docs/reverse-engineering.md`'s documented technique) —
slower per query (~1-2 min vs. seconds) but the on-disk project already carries every
rename/struct-typing from prior GUI sessions, so results are equivalent. Four short scripts
(`/tmp/IdentityFieldTrace{,2,3,4}.java`), full logs in `/tmp/identity_trace*_output.log`.

- [x] **P.1.1** Decompiled the identity getter at VMA `0x72534` (not yet named
  `ReadHwVersionFromCamera` in this project — still `FUN_00072534`). Confirmed: it `fopen`s the
  real `/sys/class/spi_master/spi2/spi2.0/version` SPI chip, reads + byte-swaps a `u32`, and on
  success calls `FUN_000723f0(this, hw_version_int)`, which walks a fixed range table mapping
  the numeric HW-version code to one of a small set of model-name strings (`"Buddy3D-C1"` etc. —
  this *is* the `checkHwVersion`/model-selector logic already referenced in `protocol.md`).
  **No separate factory-serial/OTP getter exists**: exhaustive `.rodata` substring search for
  `"serial"`, `"Serial"`, `"SERIAL"`, `"otp"`, `"OTP"`, `"SN:"`, `"factory"` returned zero hits
  anywhere in the binary. `journal/findings.md` §7.2's "Serial Number (SN) — OTP memory" row has
  no citation and no traced getter backs it up this session — treat as unconfirmed/superseded.
- [x] **P.1.2** On `FUN_000a01dc` (the `CameraInfoMessage` encoder — not struct-typed in this
  headless project, but the same 464-byte stack layout as `research/camera_info_struct.c`,
  confirmed via the `memset(auStack_3e0, 0, 0x1d0)` call), traced offsets `0x0b8`-`0x0cc` (the
  block right before the already-known `5.4` video-mode flag at `0x0d0` — this is what P.1.2 was
  calling the "field 5.2 hardware sub-block"). Actual shape: **not** "2 ints + string" — it's
  three `(mode=const, value)` pairs. Offset `0x0c4`'s value *does* trace back to
  `FUN_00072534` — indirectly, via a shared device-info singleton's `+0x20` string field
  (`FUN_00071ce0()` → 86 call sites across the binary, including the CameraInfoMessage encoder,
  `/c/info` JSON builder, snapshot upload, and the features-list builder — confirming it's the
  general camera-identity object, not something CameraInfoMessage-specific). The other two
  values (`0x0bc` = compile-time literal string, `0x0cc` = a different singleton's `+0x2c`
  field) do **not** trace to any identity getter. Full call chain and offset table now in
  `protocol.md`'s `extended_status` section.
- [x] **P.1.3** Verdict recorded in [`status.md`](status.md) (see the "ruled out" table): **no
  serial anywhere in the struct** — the one hardware-derived value found (`0x0c4`) is a small,
  guessable model-variant string, not a unique per-device identifier. Hardware-ID-in-`status`
  as an *unforgeable* gate is ruled out. Weight shifts to the token-registry gate (Step 1/2,
  already `status.md`'s #1 recommendation) rather than P.2/P.3 below (see the note at the top of
  this Priority section).

### P.2 — MAC / OUI test (does the backend check the MAC or its vendor prefix?) — NEEDS EXACT RETEST

**Result: negative for the OUI tested, inconclusive for the mechanism in general.** No confirmed
genuine Buddy3D MAC was ever found (checked the community GitHub repo and Prusa forums — one user
even reported the camera ships with no MAC label at all). Used the best available proxy instead:
a community teardown confirmed the WiFi chip is a **Realtek RTL8188FU**, and `00:E0:4C` is
Realtek's common default/reference OUI for that chip family. Spoofed `wlan0` to
`00:E0:4C:86:17:ff`, recomputed an `MD5(MAC)` fingerprint,
registered a **fresh token** (reusing the existing token with just a changed fingerprint got
`403` — the server validates fingerprint against what was recorded at first use, a new
previously-undocumented finding), and redeployed. `client_authentication` → ACK `5`,
registry lookup → `404`, identical to every other test. See `status.md`'s "ruled out" table.

**2026-09-18 correction from the complete 3.1.6 trace:** firmware does not hash an arbitrary
Linux MAC representation. It formats the six bytes as uppercase colon-separated ASCII
(`%02X:%02X:%02X:%02X:%02X:%02X`) and then MD5-hashes that exact byte string. The one-off test
command/preimage was not preserved, and the recorded MAC uses mixed case, so the old result does
not prove that the reported MAC and fingerprint were firmware-consistent. Retest with a fresh
token and the exact uppercase preimage before treating MAC/fingerprint consistency as ruled out.
`00:E0:4C` itself also remains an educated guess, not a verified genuine-camera OUI.

- [x] **P.2.1** Find a genuine Niceboy/Prusa camera **OUI** — no confirmed one found; used the
  Realtek RTL8188FU chip's common reference OUI (`00:E0:4C`) as the best available proxy instead.
- [x] **P.2.2** Temporarily spoof the Pi's `wlan0` MAC to that OUI:
  ```bash
  sudo ip link set wlan0 down
  sudo ip link set wlan0 address <NICEBOY_OUI>:XX:XX:XX
  sudo ip link set wlan0 up
  ```
- [ ] **P.2.3** Register a fresh token, deploy the automatic 3.1.6 fingerprint derivation, then
  re-run `/c/info` + the viewer-flow test. Note whether viewer ACK `5` or the
  `/v1/cameras/<token>` 404 changes. The 2026-07-09 run returned no change, but its MD5 preimage
  casing cannot be verified and therefore does not close this test.
  - **Confirmed (not just a caveat anymore):** the registry gate is keyed on the *token*
    (origin fixed at creation) — reusing the existing token with just a changed fingerprint got
    `403 Forbidden` (server validates fingerprint against what it recorded at first use), so a
    **fresh token** was required to test MAC/OUI in isolation. Done — see `status.md`.

### P.3 — Make the fingerprint firmware-faithful — EXACT ALGORITHM RECOVERED; LIVE RETEST OPEN

The repository implementation now derives `fingerprint` from the same MAC it reports. Firmware
3.1.6 obtains `wlan0`'s six MAC bytes with the
`SIOCGIFHWADDR` ioctl, formats them as uppercase colon-separated ASCII, and emits the lowercase
hex MD5 digest of that exact string. If MAC lookup fails, it hashes a random 10-character seed.

- [x] **P.3.1** Compute `md5(mac.upper().encode("ascii")).hexdigest()` over a normalized
  `AA:BB:CC:DD:EE:FF` string instead of reading a static final value. Implemented in
  `pi-impersonator/identity.py` with unit coverage. **Deployment still needs a fresh token:** the
  current token is already bound to its old fingerprint and changing it returns `403`.
- [x] **P.3.2** Confirmed the "careful" concern was real: changing fingerprint against the
  *existing* token broke it (`403`). Worked around by registering a fresh token instead — see P.2.

**Current order:** P.1 is complete. Repeat P.2 + P.3 once with the exact uppercase MAC preimage
and a fresh token; then return to real hardware or SSL-unpinning if the gate remains unchanged.

---

## Step 0 — Fix Pi SSH access (prerequisite for everything else)

- [ ] **0.1** Attempt password auth explicitly:
  ```bash
  ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no pi@<PI_IP>
  ```
  _Note: BUILD\_PLAN.md says `pi@<PI_IP>` with no password; try both users._

- [ ] **0.2** If password fails, attempt with explicit key:
  ```bash
  ssh -i ~/.ssh/id_rsa pi@<PI_IP>
  ssh -i ~/.ssh/id_ed25519 pi@<PI_IP>
  ```

- [ ] **0.3** If both fail, connect a keyboard/monitor and reset `authorized_keys` or
  re-enable password auth in `/etc/ssh/sshd_config` (`PasswordAuthentication yes`), then
  `sudo systemctl restart ssh`.

- [ ] **0.4** Verify SSH works end-to-end:
  ```bash
  ssh pi@<PI_IP> "systemctl status prusa-cam.service | head -20"
  ```
  **Pass:** service is `active (running)` and last log line is recent.

---

## Step 1 — PrusaLink printer API (get `origin: LINK`) — DEPRIORITIZED 2026-07-09

**This is very likely the wrong target — read before spending time here.** The original theory
was "`origin: LINK` is what a genuine Buddy3D camera gets, and that's what unlocks WebRTC." Two
official Prusa documents now contradict the premise:

- The official Buddy3D quick-start manual shows genuine cameras pairing via Connect's **"Add WiFi
  Camera"** wizard (Camera tab → generate a QR with Wi-Fi credentials → camera scans it with its
  own lens). Per the OpenAPI spec's description of the `origin` param (*"Use OTHER for camera
  registration via api. WEB is used when registering camera via web qr code"*), that flow is
  **`origin: OTHER`** — the same origin our impersonator already uses.
- A separate guide ("Camera setup for PrusaLink / Prusa Connect") confirms `origin: LINK` belongs
  to a *different, unrelated* product: a CSI/USB webcam wired directly into the Raspberry Pi
  running PrusaLink, switched on via a "Link camera to Connect" toggle in PrusaLink's own web UI.
  Nothing in that guide mentions WebRTC, Socket.IO, or live streaming of any kind — it reads like
  plain snapshot-style camera linking, a different feature entirely from what Buddy3D cameras do.

So even a successful `origin: LINK` token likely wouldn't exercise the Buddy3D Socket.IO/WebRTC
protocol at all. Full writeup in `status.md`'s Bottom line and `dead-ends.md`. **Do Step 2
(mitmproxy) first** — it's now the higher-value move. The steps below are kept for reference in
case PrusaLink turns out to be relevant for an unrelated reason, but are no longer the leading
lead.

**2026-07-09 (earlier note, superseded by the above):** confirmed from the public OpenAPI spec
(`docs/openapi.yaml`, v0.22.0) that the only documented user/app registration endpoint
(`POST /app/printers/{uuid}/camera`) accepts `origin: WEB|OTHER` only — `LINK` cannot come from
there.

- [ ] **1.1** Find the printer's local IP address.
  - Check your router's DHCP table, or
  - Check Prusa Connect web UI → printer detail page (usually shows local IP), or
  - `arp -a | grep -i prusa` / `nmap -sn 192.168.0.0/24 | grep -A1 -i prusa`

- [ ] **1.2** Confirm PrusaLink is accessible:
  ```bash
  curl -s http://<PRINTER_IP>/api/version | python3 -m json.tool
  ```
  **Pass:** JSON response with `api`, `server`, `text` keys.

- [ ] **1.3** Check the API root for a cameras or devices section:
  ```bash
  # PrusaLink Swagger UI (if enabled):
  open http://<PRINTER_IP>/api/swagger-ui/
  # Or enumerate common paths:
  for path in cameras camera devices link; do
    echo -n "$path: "
    curl -s -o /dev/null -w "%{http_code}" http://<PRINTER_IP>/api/v1/$path
    echo
  done
  ```

- [ ] **1.4** If a cameras endpoint exists, read the PrusaLink API key from the printer
  (visible in its web UI under Settings → API or via `curl http://<IP>/api/v1/auth`), then
  try registering the camera token:
  ```bash
  # PrusaLink uses X-Api-Key header
  PRINTER_IP=192.168.0.XXX
  API_KEY=<from printer web UI>
  CAM_TOKEN=<from ~/prusa-cam/config.ini on Pi>

  # Attempt 1: register camera via printer
  curl -s -X POST http://$PRINTER_IP/api/v1/cameras \
    -H "X-Api-Key: $API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"token\": \"$CAM_TOKEN\"}" | python3 -m json.tool

  # Attempt 2: if the endpoint is different
  curl -s -X PUT http://$PRINTER_IP/api/v1/camera \
    -H "X-Api-Key: $API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"token\": \"$CAM_TOKEN\", \"connected\": true}" | python3 -m json.tool
  ```

- [ ] **1.5** After any successful POST/PUT, check whether `origin` changed:
  ```bash
  # From the Pi (once SSH is fixed):
  curl -s https://webcam.connect.prusa3d.com/c/info \
    -H "Token: $CAM_TOKEN" \
    -H "Fingerprint: $(python3 -c "import hashlib,json; t='$CAM_TOKEN'; print(hashlib.md5(t.encode()).hexdigest()[:16])")" \
    | python3 -m json.tool | grep origin
  ```
  **Pass:** `"origin": "LINK"` (or `"WEB"`). Any change from `"OTHER"` is worth testing.

- [ ] **1.6** If origin changed, restart `prusa-cam.service` on the Pi and open the mobile
  app camera page. Watch Pi logs for incoming Socket.IO events:
  ```bash
  ssh pi@<PI_IP> "journalctl -f -u prusa-cam.service"
  ```
  **Pass:** Logs show an inbound `webrtc` event within 10–15 seconds of opening the app.

---

## Step 2 — mitmproxy on phone (cloud-side interception) — ATTEMPTED 2026-07-09, BLOCKED BY CERT PINNING

**Result: blocked, not just untried.** Set up mitmweb, configured the phone's Wi-Fi proxy,
installed and trusted the certificate (verified working — `sentry.prusa3d.com` and Firebase
traffic decrypted cleanly), then confirmed time spent on the Core One's camera view in the app.
Across the whole capture, **zero requests ever appeared** for `connect.prusa3d.com`,
`camera-service-api.prusa3d.com`, or `camera-signaling.prusa3d.com` — every other domain the app
touched came through fine. That combination (interception working in general, total silence for
exactly the domains that matter) points to **certificate pinning** on the app's core API traffic.
Bypassing that needs jailbreak-level tooling (SSL Kill Switch / Frida / objection on a jailbroken
device) — a much bigger escalation than this step originally assumed. Not pursued further today;
see `status.md`'s "Recommended next steps" for what that leaves open. The steps below are kept for
reference in case jailbreak tooling becomes available later.

Original goal: see what `camera-service-api.prusa3d.com` returns when the app tries to initiate
WebRTC for our camera. Origin gating and network-reputation are **ruled out by direct experiment**
(see `status.md`'s "origin and network-reputation ruled out by live experiment") — the two live
hypotheses this was meant to distinguish:

- **Real-hardware allowlist**: response indicates a specific per-camera check failed (e.g. an
  explicit "not a registered device" / "unverified hardware" error, or the request/token itself
  is rejected in a way that implies identity verification).
- **Staged feature rollout**: response indicates the feature/endpoint isn't generally available
  yet (e.g. a version check, a "coming soon"-style message, a flag/entitlement check, or — per
  Prusa's own 2025 blog post about the Buddy3D/App update — evidence that cloud WebRTC access is
  still being rolled out rather than universally on). A real Buddy3D camera on an account showing
  the *same* rejection would be strong evidence for this branch over the allowlist one, if it can
  be arranged.

- [ ] **2.1** Install mitmproxy on Mac:
  ```bash
  brew install mitmproxy
  ```

- [ ] **2.2** Start mitmproxy in transparent/web mode on the Mac:
  ```bash
  mitmweb --listen-host 0.0.0.0 --listen-port 8080
  # Opens browser at http://localhost:8081 (the web UI)
  ```

- [ ] **2.3** Configure iPhone to proxy through Mac:
  - Settings → Wi-Fi → tap your network → Configure Proxy → Manual
  - Server: `<Mac's LAN IP>` (e.g. `192.168.0.X`), Port: `8080`
  - Leave Authentication off

- [ ] **2.4** Install mitmproxy's root CA on the iPhone:
  - On the phone, navigate to `http://mitm.it` (while proxy is running)
  - Download the iOS certificate and install it
  - Settings → General → VPN & Device Management → install the cert
  - Settings → General → About → Certificate Trust Settings → enable full trust for the cert

- [ ] **2.5** On the phone, open the Prusa Connect app and navigate to the camera page.
  In the mitmproxy web UI (`localhost:8081`), filter for `camera-service-api`:
  - Look for requests to `camera-service-api.prusa3d.com`
  - Note the exact endpoint, HTTP method, request body, and **response body**

- [ ] **2.6** Document the exact error/rejection:
  - If the response is `403` / `"origin not allowed"` or similar → confirms origin gating
  - If the response is a different error → copy the full response JSON here for analysis
  - If there is **no** request to `camera-service-api` at all → the gate is earlier
    (perhaps in the app itself reading a field from the camera record)

- [ ] **2.7** Also capture the camera record the app fetches:
  - Look for requests to `connect.prusa3d.com/api/v1/cameras/<uuid>` or
    `connect-api.prusa3d.com/graphql` with the camera query
  - Copy the full response — particularly any `status`, `origin`, `flags`, or
    `capabilities` fields that differ from what we send

- [ ] **2.8** When done, remove the proxy from iPhone Settings and uninstall the CA cert
  (Settings → General → VPN & Device Management → delete).

---

## Step 3 — Add `@sio.on('webrtc')` handler to `signaling.py`

The impersonator silently drops any inbound `webrtc` Socket.IO event. This step adds
the handler, wires it to `webrtc.py`'s existing peer connection logic, and adds the
outbound SDP-answer emit. Required before any end-to-end WebRTC test can succeed.

### 3a — Log-first stub (deploy immediately, before full implementation)

- [ ] **3a.1** SSH to Pi and edit `~/prusa-cam/signaling.py`. Add this handler alongside
  the other `@sio.on` handlers:
  ```python
  @sio.on('webrtc')
  async def on_webrtc(data):
      log.info(f"INBOUND webrtc event, raw bytes: {data.hex() if isinstance(data, (bytes,bytearray)) else repr(data)}")
      # TODO: decode protobuf and start WebRTC session
  ```
  This ensures we at least see and log any offer that arrives, even before full handling.

- [ ] **3a.2** Restart service and verify no errors:
  ```bash
  sudo systemctl restart prusa-cam.service
  journalctl -f -u prusa-cam.service | grep -E "ERROR|webrtc|INBOUND"
  ```

### 3b — Protobuf decode

- [ ] **3b.1** From the RE work, the inbound `webrtc` payload is a protobuf message
  containing at minimum: request_id, message type, SDP string, and ICE/TURN config.
  Add a decoder to `proto.py`:
  ```python
  def decode_webrtc_message(data: bytes) -> dict:
      """Decode inbound 'webrtc' Socket.IO event payload."""
      fields = {}
      i = 0
      while i < len(data):
          tag, i = read_varint(data, i)
          field_num = tag >> 3
          wire_type = tag & 0x07
          value, i = read_field(data, i, wire_type)
          fields[field_num] = value
      return fields
      # Expected fields (to be confirmed empirically once a real offer arrives):
      # 1: request_id (string)
      # 2: message_type (varint: 1=offer, 2=answer, 3=ice_candidate)
      # 3: sdp (string)
      # 4+: ice/turn config (submessage)
  ```

- [ ] **3b.2** Until we receive a real offer, test the decoder against a synthetic payload:
  ```python
  # In proto.py or a test script:
  test = encode_string(1, "test-req-id") + encode_varint(2, 1) + encode_string(3, "v=0\r\no=...")
  assert decode_webrtc_message(test)[1] == b"test-req-id"
  ```

### 3c — Full handler wiring

- [ ] **3c.1** Import and call the existing WebRTC session logic from `webrtc.py`:
  ```python
  @sio.on('webrtc')
  async def on_webrtc(data):
      payload = decode_webrtc_message(data if isinstance(data, bytes) else bytes(data))
      msg_type = payload.get(2, 0)
      request_id = payload.get(1, b'').decode()
      sdp = payload.get(3, b'').decode()

      log.info(f"webrtc event: type={msg_type} request_id={request_id[:8]}... sdp_len={len(sdp)}")

      if msg_type == 1:  # offer
          answer_sdp = await webrtc_session.handle_offer(sdp, payload)
          # Emit answer back on the same 'webrtc' event
          answer_payload = encode_string(1, request_id) + encode_varint(2, 2) + encode_string(3, answer_sdp)
          await sio.emit('webrtc', answer_payload)
          log.info(f"webrtc answer sent for request_id={request_id[:8]}...")
      elif msg_type == 3:  # ICE candidate
          await webrtc_session.add_ice_candidate(sdp, payload)
      else:
          log.warning(f"webrtc: unhandled message type {msg_type}")
  ```

- [ ] **3c.2** Deploy and restart. With the log-first stub already in place, the first
  real offer will be logged in full before the handler runs — no offer is "consumed"
  silently.

- [ ] **3c.3** End-to-end test (once an offer actually arrives):
  - Open the app camera page
  - Watch for `INBOUND webrtc event` in logs
  - Confirm `webrtc answer sent` follows within 2–3 seconds
  - Confirm `webrtc_connection_info` ICE events appear in logs
  - **Pass:** App shows live video feed

---

## Step 4 — Detailed logging

Add structured, timestamped logging across all three Pi services so any future session
can observe what's happening without blind spots.

### 4a — `signaling.py` logging improvements

- [ ] **4a.1** Replace bare `print()` calls with a proper logger:
  ```python
  import logging, sys
  logging.basicConfig(
      level=logging.DEBUG,
      format='%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s | %(message)s',
      datefmt='%H:%M:%S',
      handlers=[
          logging.StreamHandler(sys.stdout),
          logging.FileHandler('/var/log/prusa-cam/signaling.log', mode='a'),
      ]
  )
  log = logging.getLogger('signaling')
  ```

- [ ] **4a.2** Log every inbound Socket.IO event (not just `webrtc`):
  ```python
  @sio.on('*')
  async def on_any(event, data):
      raw = data.hex() if isinstance(data, (bytes, bytearray)) else repr(data)[:200]
      log.debug(f"SIO IN  [{event}] {len(data) if hasattr(data,'__len__') else '?'}B: {raw}")
  ```
  _Note: `'*'` catch-all works in python-socketio's asyncio client._

- [ ] **4a.3** Log every outbound Socket.IO emit:
  ```python
  async def sio_emit(event, data, **kwargs):
      raw = data.hex() if isinstance(data, (bytes, bytearray)) else repr(data)[:200]
      log.debug(f"SIO OUT [{event}] {len(data) if hasattr(data,'__len__') else '?'}B: {raw}")
      await sio.emit(event, data, **kwargs)
  ```
  Replace all `await sio.emit(...)` calls with `await sio_emit(...)`.

- [ ] **4a.4** Log connection lifecycle events with timestamps:
  ```python
  @sio.event
  async def connect():
      log.info(f"Socket.IO connected to {SIGNALING_URL}")

  @sio.event
  async def disconnect():
      log.warning("Socket.IO disconnected — will reconnect")

  @sio.event
  async def connect_error(data):
      log.error(f"Socket.IO connect error: {data}")
  ```

- [ ] **4a.5** Add shallow protobuf decode to inbound event logs:
  ```python
  def pb_summary(data: bytes) -> str:
      """One-line field summary: {1: 'abc...', 2: <varint 42>, 3: <bytes 18B>}"""
      try:
          fields = {}
          i = 0
          while i < len(data) and len(fields) < 8:
              tag, i = read_varint(data, i)
              fn, wt = tag >> 3, tag & 7
              if wt == 0:
                  v, i = read_varint(data, i); fields[fn] = v
              elif wt == 2:
                  n, i = read_varint(data, i)
                  chunk = data[i:i+n]; i += n
                  try: fields[fn] = f'"{chunk.decode()}"'
                  except: fields[fn] = f'<bytes {n}B>'
              else:
                  break
          return str(fields)
      except Exception as e:
          return f'<parse error: {e}>'
  ```
  Use in the catch-all handler: `log.debug(f"SIO IN [{event}] {pb_summary(data)}")`

### 4b — `main.py` logging improvements

- [ ] **4b.1** Add a `log = logging.getLogger('main')` and log all `/c/snapshot` responses:
  ```python
  log.info(f"snapshot → {resp.status_code} ({len(img_bytes)}B, {elapsed_ms:.0f}ms)")
  ```

- [ ] **4b.2** Log when the snapshot loop is paused (WebRTC or RTSP streaming active):
  ```python
  if streaming or rtsp_streaming():
      log.debug("snapshot loop paused (streaming active)")
      continue
  ```

- [ ] **4b.3** Log `/c/info` upload result:
  ```python
  log.info(f"/c/info → {resp.status_code}, origin={resp.json().get('origin','?')}, registered={resp.json().get('registered','?')}")
  ```

### 4c — Log rotation and access — ~~DROPPED~~ (superseded 2026-07-14)

**No longer applicable.** For power-loss robustness the file log was removed entirely:
`main.py` now logs stdout-only at INFO → journald with `Storage=volatile` (RAM). There is no
`/var/log/prusa-cam/*.log` to rotate, so a logrotate config and the log dir are obsolete. Read
logs with `journalctl -u prusa-cam` (in RAM, cleared on reboot; flip journald to `persistent`
via the overlay maintenance flow when you need them to survive a reboot for debugging). See the
power-loss robustness section in `status.md` and `.agent/pi-ops.md`.
  **Pass:** Timestamped lines including `Socket.IO connected`, `SIO OUT [status]`, etc.

- [ ] **4c.4** Quick log monitor alias (add to `~/.bashrc` on Pi):
  ```bash
  alias camlog='tail -f /var/log/prusa-cam/signaling.log | grep -v "snapshot loop paused"'
  alias camlog-all='tail -f /var/log/prusa-cam/signaling.log'
  alias camevents='tail -f /var/log/prusa-cam/signaling.log | grep -E "SIO IN|webrtc|ERROR|WARN"'
  ```

---

## Step 5 — Deeper `status` message tracing (field 5 sub-fields)

Field 5.11 (`GetWebRtcMode` / `GetWebRtcStatus`) at CameraInfoMessage offset `0x144`
is the most interesting untraced field — if the backend reads it to determine WebRTC
eligibility, we need to send a non-zero "enabled/active" value.

- [ ] **5.1** In Ghidra, decompile `GetWebRtcMode` (string at `0x3fcbeb`) and
  `GetWebRtcStatus` (string at `0x3fcc08`) to understand the return values:
  ```
  mcp__ghidrassist__search_strings("GetWebRtcMode")
  → find address, then get_code(address, format=decompiler)
  ```

- [ ] **5.2** Trace what `SendCameraInfoMessage` writes to offsets `0x144`–`0x154`
  (field 5.11 block). Use `struct field_xrefs` on the CameraInfoMessage struct fields
  at those offsets to find every write site.

- [ ] **5.3** Similarly trace field 5.6 (RTSP mode/status/url, offset `0x0f0`):
  - `GetRtspServerMode`, `GetRtspServerStatus`, `GetRtspServerUrl` — find their return
    values and the protobuf field numbers within the 5.6 sub-message.
  - The impersonator currently sends `{}` for 5.6; it should send at least
    `{1: rtsp_mode, 2: rtsp_status}` with the real enum values.

- [ ] **5.4** Update `signaling.py`'s `send_status()` with confirmed field 5 sub-field
  values. Specifically:
  ```python
  # Field 5.11: WebRTC mode + status
  # mode: 1=enabled, 0=disabled (TBD from GetWebRtcMode decompile)
  # status: 1=running, 0=stopped (TBD)
  field_5_11 = encode_varint(1, 1) + encode_varint(2, 1)  # enabled + running

  # Field 5.6: RTSP mode + status + url
  field_5_6 = (encode_varint(1, 1) +           # mode: enabled
               encode_varint(2, 1) +            # status: running
               encode_string(3, RTSP_URL))      # url: rtsp://<PI_IP>:8554/live
  ```

- [ ] **5.5** Deploy, restart, and retest with mobile app:
  ```bash
  sudo systemctl restart prusa-cam.service
  journalctl -f -u prusa-cam.service | grep -E "status|webrtc|INBOUND"
  ```
  Note status message byte count before/after (it should increase).

---

## Step 6 — MQTT topic investigation

The Prusa Connect app subscribes to `wss://mqtt.prusa3d.com:8084/mqtt` for real-time
camera status. Understanding the topic/message structure may reveal status fields the
backend publishes that gate WebRTC in the app.

- [ ] **6.1** Find the MQTT topics by capturing the app's MQTT traffic via mitmproxy
  (same setup as Step 2, but filter for `mqtt.prusa3d.com`).
  WebSocket MQTT traffic: look for `CONNECT` and `SUBSCRIBE` frames in the WS stream.

- [ ] **6.2** Alternatively, try connecting to the broker directly with the user's JWT
  token as the MQTT password (MQTT over WebSocket typically uses username/password from
  the app's auth):
  ```bash
  pip3 install paho-mqtt
  python3 - << 'EOF'
  import paho.mqtt.client as mqtt, ssl

  def on_connect(c, ud, flags, rc):
      print(f"Connected: {rc}")
      c.subscribe("#")  # wildcard — will likely be rejected, but shows error

  def on_message(c, ud, msg):
      print(f"TOPIC: {msg.topic}")
      print(f"PAYLOAD: {msg.payload[:200]}")

  c = mqtt.Client(transport="websockets")
  c.tls_set()
  c.username_pw_set(username="<YOUR_EMAIL>", password="<JWT_TOKEN>")
  c.on_connect = on_connect
  c.on_message = on_message
  c.connect("mqtt.prusa3d.com", 8084)
  c.loop_forever()
  EOF
  ```
  _The JWT token is obtainable from the browser session (check localStorage or the
  Authorization header in any authenticated API call captured by mitmproxy in Step 2)._

- [ ] **6.3** Once connected, subscribe to camera-specific topics:
  ```python
  # Common MQTT topic patterns for IoT camera platforms:
  c.subscribe(f"camera/{CAMERA_UUID}/#")
  c.subscribe(f"device/{CAMERA_UUID}/#")
  c.subscribe(f"v1/devices/{CAMERA_UUID}/#")
  ```

- [ ] **6.4** With the real camera app open, trigger the "camera failed" state and note:
  - What topics receive messages when the state changes
  - What the message payload contains (likely protobuf or JSON)
  - Whether there is a `webrtc_ready`, `status`, or `connection` topic that changes

- [ ] **6.5** If a camera status topic is found, decode its content and compare against
  what the impersonator currently sends in the Socket.IO `status` event. Any discrepancy
  is a candidate for the next fix.

---

## Summary / dependency graph

```
[2026-07-09, done] origin (WEB vs OTHER) ruled out — live experiment
[2026-07-09, done] source-IP/network reputation ruled out — live experiment from 2nd network
[2026-09-18, open] repeat guessed-OUI test with exact uppercase MAC → MD5 fingerprint preimage
[2026-07-09, blocked] Step 2 (mitmproxy) — certificate pinning on the app's core API traffic
[2026-07-09, done] camera-service-api probed directly (root/list/POST/OPTIONS/health/docs) —
                    no hidden endpoint, no informative error, ESP32Cam also absent from registry
                                                    │
                                                    ▼
                              Every software-only lead is now either negative or blocked.
                              What's left needs real hardware or jailbreak-level phone tooling:
        │
        ├─ Firmware/rollout-timing research (item 2 in status.md's next steps)
        ├─ SSL-unpinning on a jailbroken device, if available (item 3)
        └─ Real Buddy3D hardware to compare directly (item 4)

Step 0 (SSH fix) ─── already resolved, kept for reference
Step 1 (PrusaLink / origin: LINK) ─── DEPRIORITIZED, reference only
                                       (see Step 1's 2026-07-09 note)

Step 4 (logging) ─── still useful background hygiene, independent of the above
Step 3 (webrtc handler) ─── needed before any end-to-end WebRTC test
Step 5 (status fields) ──── low priority, no longer expected to reveal the gate
Step 6 (MQTT) ──────────── low priority, no longer expected to reveal the gate
```

**Minimum useful state (as of 2026-07-09):** reached — every cheap, software-only lead has been
tried. From here, further progress needs either real Buddy3D hardware or a jailbroken phone.
