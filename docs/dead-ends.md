# Dead Ends, Red Herrings & Corrected Assumptions

The mistakes are as valuable as the findings — they stop the next person (or agent) from
re-walking them. Each entry: what we believed → why it was wrong → what's true. Deeper detail
lives in [`journal/findings.md`](journal/findings.md) and [`status.md`](status.md).

## Protocol / CameraInfo struct — the big misreads

| Believed (wrong) | Reality (verified) | How it was caught |
|---|---|---|
| `network_info` is top-level **field 5** | Network data is top-level **field 4** (68-byte block: ssid, MAC/BSSID, IPv4). Field 5 is an *extended device/status* block (11 sub-fields, 152 B). | Ghidra descriptor + accessor tracing (`FUN_00082330`, `FUN_00096cc0("wlan0")`, offsets `0x070`/`0x0b8`) |
| Field **8** = camera name | Field 8 = `http.token` | Descriptor tracing |
| Field **10** = firmware, field **9** = `available_resolutions` | Field 9 = system telemetry; field 10 = a different wrapper | Descriptor tracing |
| `/c/info` upload is a **flat, top-level** JSON payload | Almost everything must be nested under `config`; `features`/`capabilities` must be JSON **arrays**, not CSV strings | Flat version always returned `400`; decompiling `do_update_camera_attr` (`0x00061bbc`) + live trial gave `200` |

> The single most expensive assumption was **"field 5 = network_info."** It propagated into
> `protocol.md` and `implementation.md` and produced a wrong `status` message before descriptor
> tracing corrected it. Both docs are now fixed in place.

## Config path confusion

- **Wrong:** config lives at `/data/xhr_config.ini` (from binary strings).
- **Right:** the real path is `/userdata/xhr_config.ini`; `/data/…` is a symlink/alternate.
  Token is also mirrored to `/userdata/xhr_http_token.conf` as raw content.

## Origin cannot be changed post-registration

- **Hoped:** the `/c/info` HTTP upload could set/override the camera **origin** (`WEB` vs `OTHER`).
- **Found:** origin is fixed at **token creation** by *which* "add camera" UI flow minted the
  token; `/c/info` cannot change it afterward. (This is also captured in the project memory.)

## A confirmed dead-end trace

- Chasing the strings `"Buddy3D-C1"`, `"Buddy3D-POE"`, `"Buddy3D."` through all xrefs looked
  promising for device-model logic. **Dead end:** every reference is a C++ static initializer
  (`_INIT_*`) building a port-number/service-name table (integers like 5100, 7200, 12200…),
  not model-selection logic.

## `origin: LINK` was never the target (2026-07-09)

- **Believed:** `origin: LINK` (from the printer's QR pairing) is what a genuine Buddy3D camera
  registers as, and getting one would unblock WebRTC for our impersonator. This was the leading
  hypothesis in `next-steps.md` Step 1 for a while.
- **Reality:** the official Buddy3D quick-start manual ("Buddy3D Camera for Prusa Core One",
  v1.00) documents the actual pairing flow — Connect web UI → pick printer → **Camera tab → "Add
  WiFi Camera"** → enter Wi-Fi credentials → **"Generate QR Code"** → camera scans that QR with
  its own lens (RESET-button pairing mode, ~50 cm distance) → registers. The OpenAPI spec's own
  description of the `origin` parameter (*"Use OTHER for camera registration via api. WEB is used
  when registering camera via web qr code"*) makes this **`origin: OTHER`** — the same origin our
  impersonator already uses.
- **What `LINK` actually is:** a separate Prusa guide ("Camera setup for PrusaLink / Prusa
  Connect") shows `origin: LINK` belongs to CSI/USB webcams wired directly into a Raspberry Pi
  running PrusaLink, switched on via a "Link camera to Connect" toggle in PrusaLink's own web UI —
  an unrelated product with no mention of WebRTC/Socket.IO anywhere in that guide.
- **How it was caught:** reading the official pairing manual and the PrusaLink camera-setup PDF
  after the user pointed both out; see `status.md`'s Bottom line and `next-steps.md` Step 1.

## `origin: WEB`'s rejection doesn't corroborate `origin: OTHER`'s (2026-07-09)

- **Believed:** since both our `WEB` and `OTHER` tokens got the identical ACK-`5` rejection, that
  was two independent confirmations of the same registry gate.
- **Reality:** `origin: WEB` is Prusa Connect's separate **browser-webcam** feature — a user scans
  a QR with their *phone's normal camera app*, which redirects the *browser* to a page where the
  phone's or laptop's own camera (via `getUserMedia()`/browser-native WebRTC) becomes the video
  feed. It has nothing to do with the Buddy3D firmware protocol, Socket.IO, or the
  `camera_authentication`/`client_authentication` handshake we reverse-engineered. Our `WEB` token
  was probably never going to pass that handshake regardless of any registry gate — its rejection
  is not meaningful corroborating evidence, just an inapplicable test.
- **How it was caught:** reading `camera_registration`'s `#register-camera-by-user` section
  closely at the user's request — the WEB description ("scan the QR code... using a camera on the
  device... redirected to the web page, where he can use the camera") only makes sense as
  browser-camera capture, not a hardware camera pairing flow.
- **Partial correction (2026-07-09, later the same day):** the "WEB tokens can't even run our
  protocol" part of this entry turned out to be wrong. It also turned out our long-running
  impersonator's *actual* deployed token had been `origin: WEB` the whole time (nobody had
  verified this) — and it ran the full Socket.IO/protobuf Buddy3D protocol successfully for the
  entire project up to this point: `camera_authentication` ACK `1`, `/c/info` 200, snapshots all
  worked. So `origin: WEB` does **not** block a non-browser client from using the Buddy3D wire
  protocol — that assumption was too strong. What's still true: a live, controlled test (fresh
  `origin: OTHER` token, same impersonator, `registered: true`) got the *identical* ACK `5` as the
  `WEB` token, so origin is still not the WebRTC gate — just confirmed by direct experiment now
  instead of by this (partly incorrect) inference. See `status.md`'s "origin and network-reputation
  ruled out by live experiment" section for the full writeup.

## Symptom still open

- After all corrections, the Prusa app can still show **"Kamera-Kommunikation fehlgeschlagen"**
  (camera communication failed) in some states even though auth + `/c/info` succeed. The
  remaining gap is in the live WebRTC/signaling handshake, not the info upload. See
  [`status.md`](status.md) and [`next-steps.md`](next-steps.md).
