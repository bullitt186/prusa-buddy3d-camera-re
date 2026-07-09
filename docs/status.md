# Project Status — Prusa Buddy3D Camera Impersonator

Current, thematic status of the project (not a session log). Read this first; it is the
ground truth on what works, what is blocked, and what is open. Wire-level detail lives in
[`protocol.md`](protocol.md), firmware detail in [`journal/findings.md`](journal/findings.md),
and everything we once believed and later disproved in [`dead-ends.md`](dead-ends.md).

Evidence markers: **[confirmed]** = verified live against the real backend or firmware.
**[assumption]** = inferred, not proven.

---

## Bottom line

A Raspberry Pi can fully impersonate the camera for **snapshots, identity, and metadata**,
and serve a **local RTSP** live view. It **cannot** deliver the app's live WebRTC stream.

The blocker is a hard backend gate, not a protocol bug we can fix: **live streaming requires
the camera to be registered in `camera-service-api.prusa3d.com`, which only happens for
`origin: LINK` tokens created by the printer's QR-code pairing flow.** Our tokens
(`origin: OTHER` and `origin: WEB`) are not in that registry, so the signaling server rejects
every viewer and never relays a WebRTC offer to the camera. **[confirmed]**

Everything protocol-level that we *can* influence from software has been corrected to match a
real camera; none of it changes this outcome.

---

## Status at a glance

| Capability | State | Evidence |
|---|---|---|
| Snapshot upload → Prusa Connect | ✅ Working | `PUT /c/snapshot` → 200, image updates every 10 s **[confirmed]** |
| Camera identity / auth (Socket.IO) | ✅ Working | `camera_authentication` → ACK `1` **[confirmed]** |
| Camera info / metadata (`/c/info`) | ✅ Working | 200; name, firmware, model, Wi-Fi shown in app **[confirmed]** |
| Appears online & paired, survives reboot | ✅ Working | web + mobile app; `Restart=always` **[confirmed]** |
| Local RTSP live view | ✅ Working | `rtsp://<pi>:8554/live` in VLC **[confirmed]** |
| Classified as a genuine Buddy camera | ❌ No | listed under "Other cameras" **[confirmed]** |
| Live WebRTC stream in the app | ❌ Blocked | camera-service-api registration gate (below) **[confirmed]** |
| "Kamera-Kommunikation fehlgeschlagen" warning | ⚠️ Persistent | side-effect of the same gate **[confirmed]** |

---

## What works (confirmed)

- **Snapshots** — `PUT /c/snapshot` to `webcam.connect.prusa3d.com` with headers
  `Token` / `Fingerprint` / `User-Agent: Buddy3D Camera` / `Content-Type: image/jpg`. Visible
  and updating in Prusa Connect.
- **Socket.IO auth** — `camera_authentication` with the 2-field protobuf
  (`fingerprint`, `token`); server ACKs a bare `1`.
- **`/c/info` metadata upload** — the corrected schema (almost everything nested under
  `config`, `features`/`capabilities` as JSON **arrays**) returns 200 and populates the
  camera's name, firmware `3.1.5`, model `Buddy3D-C1`, and Wi-Fi details. Full body in
  [`protocol.md` §8](protocol.md).
- **Local RTSP** — `rpicam-vid` (userspace HW H.264) → TCP → GStreamer `GstRtspServer`.
  Continuous video (needs `do-timestamp=true` on `tcpclientsrc`). Single upstream client;
  see [RTSP notes in `protocol.md` §12](protocol.md).
- **Online/paired state** — camera shows online in web and app, correct metadata, auto-recovers
  on crash/reboot via three `enabled` systemd services.

---

## The core blocker: live WebRTC streaming

### Root cause (confirmed)

The signaling server validates the **viewer's** `client_authentication` against
`camera-service-api.prusa3d.com`. A direct lookup
`GET camera-service-api.prusa3d.com/v1/cameras/<token>` returns **404** for our tokens — the
camera does not exist in that registry. Only `origin: LINK` tokens (minted by the printer's
"Add Buddy camera" QR pairing) are registered there. As a result the server returns viewer
ACK `5` (rejected) and never relays a `webrtc` offer to the camera. The firmware confirms the
matching camera-side gate: `FUN_000b87b4` silently drops any offer unless `webrtc_mode`
(`+0x13d`) and `webrtc_status` (`+0x13e`) are both set, and those are only set when the server
sends `set_webrtc_mode` — which it withholds from unregistered cameras.
See [`protocol.md` §5 (server-side gate) and §10 (enable gate)](protocol.md).

### The evidence chain

1. **No inbound events, ever** — across stable 45 s–6 min connections the camera never received
   a single `webrtc`, `trigger`, or `configuration` event. **[confirmed]**
2. **The phone never touches the Pi** — full `tcpdump` on `wlan0` during a live app-open showed
   zero packets from the phone toward the Pi (no ARP/TCP/UDP). The failing check is therefore
   **server-side**, not a local probe. **[confirmed]**
3. **Viewer-flow test** — replaying the buddy3d-proxy viewer handshake
   (`client_authentication` with a valid account JWT) returns ACK `5` for both `OTHER` and
   `WEB` tokens, camera online or offline. **[confirmed]**
4. **Registry lookup** — `.../v1/cameras/<token>` → 404 for both tokens. **[confirmed]**

### What was ruled out (so nobody re-chases it)

| Hypothesis | Verdict | Why |
|---|---|---|
| App expects a local port/endpoint on the camera | Ruled out | phone sends zero packets to the Pi |
| TLS certificates / mTLS / keypairs gate pairing | Ruled out | none exist anywhere in the pairing flow |
| STUN / ICE hole-punching is the problem | Not reachable | never gets past signaling; no offer is ever made |
| Wrong Socket.IO field encoding | Real but not the cause | all fields corrected to match a real camera; behaviour unchanged |
| A missing serial number on the wire | Ruled out as a lever | no serial is transmitted in any message; gating is by token registration |
| The local check is "RTSP-shaped" | Ruled out | warning identical with local RTSP up or down |

---

## Confirmed protocol knowledge (reference)

All corrected and matched against a real camera / the buddy3d-proxy captures. Full spec in
[`protocol.md`](protocol.md); firmware derivation in [`journal/findings.md`](journal/findings.md).

- **Two auth paths:** `camera_authentication` (camera: fingerprint+token) vs.
  `client_authentication` (viewer: camera_token + `"client"` + account OAuth2 JWT). WebRTC is
  **client-initiated**; the camera only ever answers an offer the server relays.
- **`/c/info` schema** — nested under `config`; `features`/`capabilities` are arrays;
  `manufacturer: Niceboy`, `model: Buddy3D-C1` (model comes from the firmware's HW-variant
  selector, not a free-text value).
- **`CameraInfoMessage` (`status`)** — a 464-byte nanopb struct, reconstructed byte-accurately
  in Ghidra. Confirmed offsets: `ir_mode @0x5c`, `speaker_volume @0x68`, `video_quality @0x1cc`.
  Top-level field 4 = network info (ssid/mac/ipv4), field 9 = system telemetry, field 11 =
  video-quality enum. ~90 remaining fields are located but generically named.
- **Socket.IO events** — 17 outbound camera notifications + inbound handlers
  (`trigger`, `configuration`, `set_rtsp_server_mode`, `set_webrtc_mode`,
  `change_video_size`/`save_video_size`, `timelapse_get_file_list`, `webrtc`). Enumerated in
  [`protocol.md` §3–4](protocol.md).
- **`set_webrtc_mode` gate** — server → camera enable event; both `webrtc_mode` and
  `webrtc_status` bytes must be set before any offer is processed.
- **WebRtcSignal schema** — confirmed field layout (token/session/peer/body/msg_type/direction/
  ice_config) in [`protocol.md` §6](protocol.md).
- **Field semantics fixed from buddy3d-proxy captures:** `status` field 10 = Socket.IO sid;
  `features` field 7 = MD5 of the features JSON (used as WebRTC `peer_id`); Socket.IO CONNECT
  must carry `auth={token}` and `Origin: https://connect.prusa3d.com`.
- **No security to forge** — auth is `fingerprint = MD5(MAC)` + `token`; no certs; MAC OUI is
  not validated server-side.

---

## Current deployment state

**Hardware:** Raspberry Pi Zero 2 W, Debian 13 (trixie), OV5647 (Pi Cam v1, 1920×1080). App in
`~/prusa-cam/` (venv `--system-site-packages`). Paired to a Prusa CORE One. Live token in
`~/prusa-cam/config.ini` (secret; not in repo).

**Services (systemd, `enabled`, survive reboot):**

| Service | Role |
|---|---|
| `prusa-cam.service` | `main.py` — `/c/info`, snapshot loop, Socket.IO signaling, WebRTC answer logic |
| `rpicam-source.service` | `rpicam-vid --listen` → H.264 over TCP :8888 (single client) |
| `prusa-rtsp.service` | GStreamer RTSP → `rtsp://<pi>:8554/live`, pulls from rpicam-source |

**What the impersonator currently sends (latest valid state):** corrected `/c/info`;
`camera_authentication`; `status` with fields 2/3/4/5/8/9/11 (real network + telemetry, defaults
elsewhere); `protobuf_version` (token in field 1, `"4.4"` in field 7); `features` (bracket-wrapped
array, field 7 = MD5 hash); inter-emit `sleep(0.2–0.3)` (required — the server disconnects
without them). Inbound `configuration`/`trigger`/`set_rtsp_server_mode`/`change_video_size` are
handled; `set_webrtc_mode` self-enables `webrtc_mode/status`; `webrtc` is wired to `webrtc.py`.

**Single-camera contention:** libcamera allows one client. The snapshot loop now gates on active
RTSP clients (via `/proc/net/tcp` on :8888) and on the WebRTC flag, so local RTSP viewing no
longer races snapshot uploads.

**Note on ACKs:** only `camera_authentication` is ever ACKed; `status`/`features`/
`protobuf_version` are not (appears normal). ACKs therefore can't confirm field correctness —
the `/c/info` HTTP channel is what actually populates the UI. **[assumption]**

---

## Open / unresolved

- **Byte-perfect `CameraInfoMessage` map** — the 464-byte struct is reconstructed and offsets
  are computable, but ~90 fields remain generically named. Fast path: GhidrAssistMCP
  `get_code` on `0xa01dc`, then `struct field_xrefs`/`rename_field` per offset.
  Helpers in [`../research/`](../research/).
- **`webrtc.py` end-to-end** — offer/answer/ICE + `rpicam-vid` H.264 pipeline is implemented but
  **never exercised against a real offer** (no offer ever arrives). Treat as unverified.
- **Direct registration** — whether `camera-service-api.prusa3d.com` exposes a registration
  endpoint (e.g. `POST /v1/cameras` with a bearer JWT) that would place our camera in the
  registry. Unexplored.
- **LINK-origin token / QR format** — the printer's QR pairing produces the only registered
  token type. Format/parameters unknown (firmware `AT+TOKEN=<token>` suggests the QR is just a
  token string).

Corrected/superseded understanding (field-5 vs field-4, flat `/c/info`, `Niceboy` model, etc.)
is catalogued in [`dead-ends.md`](dead-ends.md) — consult it before trusting older notes.

---

## Recommended next steps (by likely payoff)

1. **Get an `origin: LINK` token** via the printer's "Add Buddy camera" QR flow, and test the
   impersonator with it — this is the only known way past the registration gate.
2. **Probe `camera-service-api` for a direct registration path** (JWT-authenticated POST) that
   could register our camera without the QR flow.
3. **MITM the app ↔ Prusa cloud** to confirm exactly what the backend checks before the error —
   the mechanism is already known, so this is confirmation, not discovery.
4. **If a real `webrtc` offer ever arrives**, verify `webrtc.py` end-to-end (currently untested).
