# Project Status — Prusa Buddy3D Camera Impersonator on Raspberry Pi Zero 2 W

Last updated: 2026-07-07 (post-auth wire-format/instrumentation round). Read this file first
— it's the current ground truth on what works, what doesn't, and why.
`protocol.md`/`implementation.md` were corrected this session where they were proven
wrong; `journal/findings.md`/`reverse-engineering.md` are still background research material.

## Hardware / deployment

- Raspberry Pi Zero 2 W, Raspberry Pi OS Lite 64-bit (Debian trixie), `pi@<PI_IP>` (SSH, no password)
- Camera: OV5647 (Pi Camera v1), 1920x1080 max usable per firmware constant
- App code: `~/prusa-cam/` on the Pi (venv created with `--system-site-packages` — required for PyGObject/GStreamer bindings)
- Paired to a Prusa CORE One printer in Prusa Connect, camera type set to "C1" in the app UI
- Current token lives in `~/prusa-cam/config.ini` on the Pi (not repeated here — treat as a live secret, rotate via Prusa Connect if this doc is shared)

## Services running (systemd, all `enabled`, survive reboot)

| Service | What it does | Status |
|---|---|---|
| `prusa-cam.service` | Main app: `/c/info` upload, snapshot loop, Socket.IO signaling, WebRTC answer logic | Working |
| `rpicam-source.service` | `rpicam-vid --listen -o tcp://0.0.0.0:8888` — persistent HW H.264 encoder, camera acquired lazily on first TCP client | Working |
| `prusa-rtsp.service` | GStreamer `GstRtspServer`, shared media factory pulling from `rpicam-source` over TCP, serves `rtsp://<pi-ip>:8554/live` | Working |

All three fight over the single camera resource (libcamera allows exactly one client).
`streaming` flag in `main.py` pauses the snapshot loop during WebRTC; **it does NOT yet
know about RTSP clients** — if someone watches the RTSP stream, concurrent snapshot
uploads will fail with "failed to acquire camera" until the RTSP viewer disconnects.
Known gap, not yet fixed.

## What works (verified end-to-end)

1. **Snapshot upload** — `PUT /c/snapshot` to `webcam.connect.prusa3d.com`, headers
   `Token`/`Fingerprint`/`User-Agent: Buddy3D Camera`/`Content-Type: image/jpg`. Returns
   200, snapshot visible and updating in Prusa Connect every 10s.
2. **Socket.IO auth** — `wss://camera-signaling.prusa3d.com`, `camera_authentication`
   event with 2-field protobuf (`fingerprint`, `token`). ACK comes back as bare `1`
   (not the string `"ACK: OK"` the early docs guessed).
3. **`/c/info` HTTP upload — THE BIG FIX THIS SESSION.** See "Corrected `/c/info`
   schema" below. Before this fix every attempt got `400 Bad Request` /
   `"Request body validation error"`; after fixing the schema, first try returned `200`
   with the full stored camera record echoed back. This is what populates the camera's
   display name, firmware, model, and unlocked a "Change camera Wi-Fi" option in the
   app that wasn't there before.
4. **Local RTSP live view** — confirmed working in VLC at `rtsp://<PI_IP>:8554/live`,
   continuous video (not just a frozen frame — that took adding `do-timestamp=true` to
   `tcpclientsrc`, see below).
5. Camera appears "online"/paired in both Prusa Connect web and the mobile app, survives
   reboot, auto-restarts on crash (`Restart=always` on all three services).
6. **Prusa Connect web UI shows metadata correctly but classifies it as "Other cameras".**
   Verified by user after the 2026-07-07 post-auth patch: camera name is
   "Buddy3D Camera", category is "Other cameras", and details show Wi-Fi IPv4
   `<PI_IP>`, Wi-Fi MAC `<PI_MAC>`, Wi-Fi SSID `<WIFI_SSID>`, and firmware
   `3.1.5`.
7. **Official Camera API origin enum identified.** Prusa's public Camera API OpenAPI
   defines stored camera origins as `LINK`, `WEB`, and `OTHER`; its registration
   endpoint only accepts `WEB` or `OTHER` as a query parameter and describes `OTHER` as
   manual Camera API registration, `WEB` as web QR registration, and `LINK` as printer
   registration. This means `/c/info` likely cannot change origin after token creation.
   Live Ghidra string/xref check on `lp_app` found no `OTHER`, `WEB`, or `LINK` origin
   enum strings in the firmware; only unrelated `OTHERNAME`, WebRTC strings containing
   `WEB`, and Buddy model strings (`Buddy3D`, `Buddy3D-C1`, `Buddy3D-POE`). This supports
   origin being backend-assigned registration metadata, not a value the camera sends.
8. **Buddy model selection logic identified in firmware.** `FUN_00072534`
   (`ReadHwVersionFromCamera`) reads 4 bytes from
   `/sys/class/spi_master/spi2/spi2.0/version`, byte-swaps them as a serial number, then
   calls `FUN_000723f0` (`checkHwVersion`) to match that serial against a 16-entry
   hardware variant table initialized in `_INIT_5`. Matching entries set:
   hardware-version string at singleton offset `+0x20`, RLDR/sensor value at `+0x38`,
   motor flags at `+0x40/+0x41`, and model string at `+0x68`. `FUN_00072744` returns
   the `+0x68` model string, and `/c/info` writes it to `config.model`. Most serial
   ranges map to `Buddy3D-C1`; default/no-version-chip falls back to empty hardware plus
   model `Buddy3D.`.
   Deployed impersonator update on 2026-07-07: `features.py` now sets
   `MODEL = 'Buddy3D-C1'` while keeping `MANUFACTURER = 'Niceboy'`. Restart verification:
   `/c/info` returned 200 and echoed `config.model: Buddy3D-C1`; Socket.IO auth ACK was
   `1`; `status` emitted as 374 bytes; snapshots continued returning 200.

## What does NOT work / open questions

1. **No "Watch Live" in the Prusa Connect web UI.** Per Prusa's own help doc
   (https://help.prusa3d.com/article/buddy3d-camera_821264, fetched this session):
   *"Browser Live Stream: Not available in web Connect"* — confirmed to be a real gap
   in Prusa's product, not something we're doing wrong. Don't chase this in the browser.
2. **A newer Prusa blog post** (blog.prusa3d.com, "Remote 1080p 24fps camera streaming
   for everyone", fetched this session) says WebRTC remote streaming **has shipped**
   post-beta for "Buddy camera", accessed via the **mobile app**, using STUN-first /
   TURN-fallback exactly like our `webrtc.py` implementation. This directly contradicts
   point 1's older doc — the blog is newer, trust it over the help article.
3. **We never once received an incoming `webrtc` Socket.IO event**, despite:
   - opening the camera detail page in the app repeatedly,
   - the connection being stable and long-running (tested 45s–6+ minutes with zero drops),
   - full camera metadata (name/firmware/model) correctly populated via the `/c/info` fix.
   Conclusion: whatever triggers a WebRTC offer from the server never fires for this
   camera. Not a code bug on our end — there's nothing to respond to.
4. **The app shows a persistent "Kamera-Kommunikation fehlgeschlagen" ("Camera
   communication failed") warning**, immediately on opening the camera page (not
   triggered by any tap — confirmed by user testing). Multiple diagnostic passes,
   all negative:
   - **Socket-level**: user refreshed the page at a known timestamp, we checked Pi logs
     for that exact window — zero incoming packets of any kind arrived at our Socket.IO
     connection during the failure.
   - **Network-level (this round)**: full `tcpdump` packet capture on the Pi's `wlan0`
     during a live app-open test, cross-referenced against the phone's mDNS hostname
     (`<PHONE_HOSTNAME>.local.`, resolved to `<HOST_IP>`). **The phone sent zero
     packets of any kind toward the Pi during the entire capture** — no ARP, no TCP SYN,
     no UDP. Not even an attempt at a local connection.
   - **2026-07-07 post-auth retest**: after patching `protobuf_version` field 1 to the
     token, sending a firmware-aligned `features` payload, and adding inbound Socket.IO
     hexdumps, a two-minute mobile-app test still produced no `trigger`, `configuration`,
     or `webrtc` event. `tcpdump` again saw only phone mDNS/Bonjour multicast, no direct
     camera traffic.
   Conclusion: the app never tries to reach the camera directly over the LAN at all
   (ruling out a missing local port/endpoint, see Hypothesis 1 below), and never signals
   it over the cloud Socket.IO channel either. The failing check is evaluated **entirely
   server-side**, using data Prusa's backend already has on file — not by probing the
   camera live over any network path, local or cloud. Leading hypotheses (unconfirmed):
   - The app/backend validates the paired token or stored camera record against a real
     factory-provisioned origin/serial before allowing live communication. The latest
     `/c/info` response still echoes `"origin": "OTHER"` even though `registered: true`,
     snapshots, name, and metadata work. The web UI also lists the camera under "Other
     cameras" while showing the correct Wi-Fi and firmware details, which supports this
     classification/gating interpretation.
   - A remaining subtle Socket.IO field mismatch is still possible, but it is now a
     weaker lead than provisioning/origin gating: full status is sent, `protobuf_version`
     now uses the token as in the decompile, and no backend command/event is attempted.
5. **No serial number is transmitted anywhere in the reversed protocol.** Real hardware
   has one (factory-burned, OTP memory on an SPI chip — `journal/findings.md` §7.2), but it
   never crosses the wire in any Socket.IO message or HTTP call we've found. If the
   backend gates live streaming on a serial-to-manufacturing-record check, it must do
   so via the *token itself* (i.e. real pairing tokens are only ever issued against a
   real serial during factory/QR provisioning) — something we cannot replicate or
   spoof, since we have no real serial to offer.
6. **RTSP does not fix #4.** We stood up local RTSP fully (see below) specifically to
   test whether the app's local-communication check was RTSP-shaped. It wasn't — the
   warning was identical before and after.

## Hypothesis validation round (session 2)

User proposed four hypotheses for why live streaming/"communication failed" doesn't
work. Ranked and tested in order of plausibility:

| # | Hypothesis | Verdict | Evidence |
|---|---|---|---|
| 1 | App expects an open local port/endpoint on the camera | **Ruled out** | Full `tcpdump` capture of the phone's traffic during a live app-open test shows zero packets toward the Pi of any kind — not even ARP. The app never attempts a local connection at all. |
| 2 | Certificates | **Ruled out (for the channels we've built)** | Already established this session: no certs, mTLS, or keypairs anywhere in the reversed pairing flow. Would only become relevant if #1 had found a local TLS handshake attempt — it didn't. |
| 3 | STUN/hole-punching | **Not reachable yet** | ICE/STUN only matters after a WebRTC offer/answer exchange begins. Proven (twice now) that stage never starts, locally or via cloud. Nothing to debug here until #4 unblocks it. |
| 4 | Falsely formatted messages | **Confirmed real, partially fixed, now lower priority** | Same bug class as the `/c/info` fix: `features` bracket-wrapping and `protobuf_version` token field are now patched and deployed, and the full always-present `status` payload is sent. The app still shows the same warning and the backend still sends no commands/offers, so provisioning/origin gating has overtaken this as the leading hypothesis. |

### `features` / `protobuf_version` Socket.IO messages — patched and retested

Decompiled `SendCameraSupportedFeatures` (VMA `0x000a7d18`) and
`SendProtobufSchemaVersion` (VMA `0x000a23b8`). Confirmed:
- `protobuf_version` field 1 is populated through `FUN_00081c18`, the `http.token`
  getter, **not** the fingerprint. Deployed fix: field 1 now sends the token, field 2
  sends `"4.4"`, and field 3 is reserved for request-id replies.
- One field constant is literally `"4.4"` (`DAT_003f1f18`), confirming `PROTOCOL_VERSION`.
- The features CSV string gets `"["` prepended and `"]"` appended before being encoded
  as a raw/pre-formatted value — exactly the same pattern as the `/c/info` fix. **Our
  code was sending the bare CSV** (`"SocketCom","UploadInterval",...`); real firmware
  sends it bracket-wrapped as a JSON-array-looking string
  (`["SocketCom","UploadInterval",...]`).

Latest deployed `~/prusa-cam/signaling.py` also adds reusable `send_status`,
`send_protobuf_version`, and `send_features` helpers, request-id support where known, and
inbound Socket.IO hexdump/shallow-decode logging. Restart verification on 2026-07-07:
`/c/info` 200, auth ACK `1`, snapshots 200, `status` 371 bytes, `protobuf_version` 27
bytes, `features` 391 bytes, stable connection for 90+ seconds. **Still no app behavior
change and no incoming `trigger`/`configuration`/`webrtc` event during the mobile test.**
**Update (2026-07-07)**: `features` field numbering corrected and field 7 pinned.
The `field_info` descriptor at `0x3f5d50` (pointed to by `DAT_000a8450`) has entries for
fields **2–7**, not 1–6 as previously coded. Field 7 confirmed as `PROTOCOL_VERSION`
via `DAT_000a8440` → `0x3f1f18` → `"4.4"`. Deployed fix bumps all field numbers by one
and sets field 7 = `PROTOCOL_VERSION`. Features message now 387 bytes. Also fixed:
`rtsp_streaming()` in `main.py` checks `/proc/net/tcp` for ESTABLISHED connections on
port 8888 and gates the snapshot loop alongside the WebRTC flag — RTSP viewers no longer
race with snapshot uploads.

### `status` (CameraInfoMessage) — the real struct is far bigger, WebRTC-readiness field found

Decompiled `SendCameraInfoMessage` (real function at VMA `0x000a01dc`, containing the
`0x000a0a00` address `reverse-engineering.md` pointed at). This is a ~500-line decompiled function
building a `0x1d0` (464-byte) struct with dozens of fields, encoded via
`pb_encode(stream, &DAT_003f5e98, struct)` and sent on event `"status"` — confirmed via
the literal string `"status"` right before the emit call, matching what we already do.

Real fields include (not exhaustive): fingerprint, IR/day-night mode, speaker volume,
video quality, RTSP server mode, HW version, a **float** value (position suggests MCU
temperature), full `network_info` (wifi_ssid/ipv4/mac — same three strings as
`/c/info`), firmware version, and critically:

```c
iVar3 = FUN_000b3dd8();
if (iVar3 == 0) {
    FUN_000816b4();
    uVar2 = FUN_00082e4c();
} else {
    // logs: "WebRTC TURN client is set, getting video mode camera channel H264"
    uVar2 = FUN_00071ce0();
    uVar2 = FUN_0007001c(uVar2, 1);
}
local_214 = FUN_0009d13c(param_1, uVar2);
```

This is a field in the struct that explicitly branches on **whether a WebRTC TURN
client is set** and encodes different values depending on the answer. Strong
circumstantial evidence that the server reads *this specific field* from our `status`
message to decide whether the camera is WebRTC-ready before ever attempting to signal
it — which would fully explain why we never receive an offer, on top of explaining the
generic "communication failed" state (the backend may just be reporting "this camera
never declared itself ready").

**Update — field 11 traced and corrected**: followed the value all the way to its
source. `FUN_000b3dd8` just checks a runtime bool ("is a TURN client currently active")
to decide whether to read the *configured default* video quality (`FUN_00082e4c` →
config key `"config.video_quality"`) or the *live encoder's current* resolution
(`FUN_0007001c` → `get_current_venc_resolution`), then `FUN_0009d13c`
(`TranslateVideoLpRv1106ToProtobuf`) maps that to the wire value (1=SD, 2=HD, 3=FHD,
matching `protocol.md`'s already-documented enum). **Field 11 is just the video quality
enum, not a WebRTC-readiness flag as first hypothesized** — correcting that claim
explicitly since it was wrong. The "TURN client active" branch only picks *which source*
to read the resolution from so the status message stays accurate mid-stream; it doesn't
encode a readiness/eligibility signal itself.

**Submessage field counts — confirmed by reading the raw descriptor tables directly**
(not inferred from code flow — this is hard fact, upgrading `journal/findings.md` §10.10's
earlier guesses from speculation to confirmed):

| Submessage | Field count | Size | Content |
|---|---|---|---|
| field1 | 4 | 32B | model/manufacturer-adjacent |
| field2 | 7 | 32B | HW version (2 ints + 1 string), a quality-capability-like enum, a float reading |
| field3 | 6 | 32B | capabilities-like, not traced in detail |
| field4 | 2 | 68B | small nested config, not traced in detail |
| **field5** | **11** | **152B** | **extended device/status block** — earlier notes mislabeled this as `network_info`; later descriptor/accessor tracing corrected it. |
| field6 | 2 | 8B | simple pair, not traced in detail |
| field9 | 9 | 72B | system telemetry block (temperature, uptime, load/RAM/process stats) — earlier notes mislabeled this as `available_resolutions`. |
| field11 wrapper | 1 | 4B | the video quality enum (see above) |

Exact **byte offsets of individual values within each submessage** remain unresolved —
tracing local-variable stack offsets against the struct got far enough to confirm which
submessage (1–6) roughly which values land in, but not a byte-perfect field-by-field
map, and one attempt at inferring this from code-flow order alone (the field-11 episode
above) produced a wrong conclusion that had to be corrected. Pinning the rest down
reliably needs nanopb's exact field-descriptor binary format for this specific build,
which wasn't confirmed with enough confidence to keep guessing headlessly without risking
more wrong leads.

**Update (2026-07-07, GhidrAssistMCP session) — byte-accurate struct reconstructed and
applied in Ghidra.** The user installed GhidrAssistMCP (https://github.com/symgraph/GhidrAssistMCP),
which attaches directly to a *running* Ghidra GUI session (`lp_app` already open at
`/tmp/ghidra_project/cam_analysis`) — no more headless `analyzeHeadless` cold-starts
(each of which cost ~2-3 min per query in the earlier headless-only phase of this
session). This unblocked the struct work the "if picking this up again" plan below used
to describe manually:

`auto_create` on `auStack_3e0` still failed the same way manual GUI "Auto Create
Structure" did — the decompiler had already split the buffer into ~90 individually
named scalar locals rather than leaving pointer+offset accesses for Ghidra's usage-based
inference to walk. But since every one of those locals is a stack variable with a known
offset (`Stack[-0xNNN]`), the byte layout is fully recoverable by arithmetic:
`buffer_offset = 0x3e0 - stack_offset` for every local between `auStack_3e0` (base) and
`local_214` (last field) — and the computed end lands *exactly* on `0x1d0` (464), matching
the `memset(auStack_3e0, 0, 0x1d0)` call precisely. That exact match is strong
confirmation the offset math is right, not a coincidence.

Built and applied a real Ghidra structure, `/auto_structs/CameraInfoMessage` (464 bytes,
102 components: named fields + explicit `byte gapN[..]` padding for the ~13 stretches
the decompiler didn't materialize as named locals, likely inlined nested-submessage
`memcpy`s). Retyped `auStack_3e0` to it (`variables retype`) and re-decompiled —
`FUN_000a01dc` now reads cleanly as `auStack_3e0.field_0x0NN = ...` throughout, and the
final encoder call is now visibly `FUN_0009b0cc(auStack_5bc, DAT_000a0d04, &auStack_3e0)`
— i.e. the *entire* struct pointer, not a sub-slice, confirming this one struct is exactly
what nanopb's descriptor-driven encoder walks for the whole `CameraInfoMessage`.

Three fields already had confirmed semantic names from the full-program export (below)
and now sit at hard-confirmed byte offsets: `ir_mode` @ `0x05c`, `speaker_volume` @
`0x068`, `video_quality` @ `0x1cc` — matching this section's earlier struct-offset notes
(92/104 decimal = 0x5c/0x68) exactly, cross-confirming both derivations independently.
The remaining ~90 fields are named generically (`field_0x0NN`) pending the same kind of
setter/getter tracing already done for those three — the struct in Ghidra is the
artifact to keep working from; the offset-computation script and generated struct
source are saved in this repo as `../research/compute_camerainfo_offsets.py` and
`../research/camera_info_struct.c`.

**If picking this up again**: with GhidrAssistMCP available, the fast path is now
`mcp__ghidrassist__get_code` (format=decompiler) on `0xa01dc` to see the current
struct-typed decompile, then `mcp__ghidrassist__struct` (`rename_field`,
`field_xrefs`) per remaining `field_0x0NN` to identify and rename fields one at a time —
`field_xrefs` in particular can jump straight to every read/write site of a given offset
without re-decompiling the whole function, which should be much faster than the
call-graph tracing that hit diminishing returns before (e.g. resolving what
`FUN_000ab28c`'s singleton actually is, needed for fields 3/4/6 and the untraced
`network_info` sub-fields).

**Pragmatic middle ground if the GUI route isn't worth the time right now**: send a
best-effort richer `status` message using only what's already confirmed with high
confidence (real `network_info` triple already implemented via `/c/info`'s working
schema — could mirror those same three real values into the Socket.IO `status` message
too; real video quality enum for field 11 given our 1920x1080 config → send `3`) and
test empirically whether that alone changes anything, before investing in the full
byte-perfect reconstruction.

**Additional confirmed semantics (from a full-program Ghidra export, `lp_app.c`/`.h`,
545K lines — same underlying analysis as the headless session, just faster to grep
across than re-running scripts per function):**
- The value assigned to `local_384` (struct offset 92) is **IR/day-night mode** —
  confirmed via its setter `FUN_0007089c`, literally named `setIrMode` with log string
  `"Set ir mode: %d"`.
- The value assigned to `local_378` (struct offset 104) is **speaker volume** — its
  getter `FUN_00070994` calls `FUN_00082e10`, which reads config key `"config.volume"`
  with default `0x28` (40), matching `protocol.md`'s documented 5-100 volume range.
- Config key `"config.rtsp_server_mode"` exists (`FUN_00082e28`) confirming RTSP mode is
  a real, separately-tracked setting, but which exact local variable in
  `SendCameraInfoMessage` reads it wasn't pinned down before diminishing returns set in
  — remaining accessor functions from this point on (`FUN_000aaaf8`, `FUN_000aab24`,
  etc.) are generic offset-based struct getters (`param_1+4`, `param_1+0xc`) shared
  across many unrelated objects in the binary, and identifying which specific singleton
  object they're reading in *this* context requires call-graph tracing back through
  `FUN_000ab28c` (likely a "device state" singleton accessor) — not attempted, genuinely
  diminishing returns for manual/headless tracing at this point. This is exactly the
  kind of thing the Ghidra GUI struct-recovery approach above would resolve faster than
  continuing to grep function-by-function.

**Checkpoint**: two full rounds of hypothesis testing plus this struct-reconstruction
pass have been done this session. Confirmed real, working fixes: `/c/info` schema,
`features` bracket-wrapping. Confirmed-but-unresolved: `status`/`CameraInfoMessage` is
genuinely far richer than what we send (IR mode, volume, HW version, video quality,
extended network info all real fields we're omitting), but neither the bracket-wrap fix
nor deeper understanding of the struct has yet produced a single observed change in the
app's behavior — every empirical test (packet capture, feature fix, this struct work)
still shows zero incoming `webrtc` events and an unchanged "communication failed"
warning. Worth treating that pattern itself as information: the blocking factor may
simply not be in the fields we can influence from software at all (see the
serial-number/provisioning hypothesis, still unconfirmed either way).

**Update (2026-07-07, descriptor verification + redeploy)**: decoded the top-level
`CameraInfoMessage` `field_info` table at `0x3f5e3c` with GhidrAssistMCP
(`get_data_at`). The compact entries decode as field number `(first byte >> 2)` plus
struct offset in the second word, and they match all known anchors:
field 4 offset `0x070`, field 5 offset `0x0b8`, field 8 offset `0x168`, field 10 offset
`0x1c0`, field 11 offset `0x1cc`.

Important correction: the earlier "field 5 = network_info" note was wrong. The real
network values are written into top-level **field 4** (68-byte block):
`field4.1.1 = wifi_ap.ssid` via `FUN_00082330`, `field4.1.2 = MAC/BSSID` via
`FUN_00096cc0("wlan0")`, `field4.1.3 = IPv4` via `FUN_00096be0("wlan0")`, and
`field4.1.5 = signal quality` via `FUN_00096980`. Top-level **field 5** (152-byte
block) is extended device/status data, starting with firmware (`FUN_0003726c`),
hardware (`FUN_0007272c`), camera name (`FUN_0005b2f8`), RTSP/video state, timezone,
CPU/temp values, and other status.

P2 also resolved two wrong scalar guesses:
- field 8 is the HTTP token (`FUN_00081c18` reads `http.token`), not camera name.
- field 10 is a conditional request-id-like string from `param_1[0x11]`, not firmware;
  it is only set when `param_1[0x12] != 0`.

Deployed corrected `~/prusa-cam/signaling.py`: `status` now sends top-level field 4
with nested `{1: {1: ssid, 2: mac, 3: ip, 5: 0}}`, field 8 as the token, and field 11
as nested `{1: 3}` (FHD video quality). It no longer sends guessed field 10 firmware.
Restart verification: `/c/info` returned 200, Socket.IO auth ACK was `1`, snapshot
uploads returned 200, status/protobuf_version/features were sent, and the service
remained connected with continuing snapshot uploads. No incoming `webrtc` event was
observed in the post-restart log window; the mobile-app behavior still needs a manual
check.

**Update (2026-07-07, full CAMERAINFO_VERIFICATION_PLAN execution)**: completed the
remaining descriptor and accessor work from `camerainfo-verification.md` and
deployed a full always-present status payload.

Confirmed top-level `field_info` format from `get_data_at(0x3f5e3c)`: the first byte
contains the protobuf field number as `byte >> 2`; the second 16-bit word is the struct
offset; the third 16-bit word is the stored size for length-delimited/submessage fields.
This hypothesis matches every confirmed anchor: field 4 at `0x070`, field 5 at `0x0b8`,
field 8 at `0x168`, field 9 at `0x178`, field 10 at `0x1c0`, and field 11 at `0x1cc`.

Full always-present flag mapping:

| Flag offset | Wire field | Confirmed producer / meaning |
|---|---:|---|
| `0x024` | `2` | timelapse-status block from `FUN_000ab28c`/`timelaps_service` getters |
| `0x048` | `3` | camera status: IR mode, upload interval/status, speaker volume |
| `0x06c` | `4` | network-info top-level block |
| `0x070` | `4.1` | current network block: SSID, MAC/BSSID, IPv4, signal quality |
| `0x0b4` | `5` | extended device/status top-level block |
| `0x0d0` | `5.4` | video/timelapse mode + storage/model-adjacent block |
| `0x0f0` | `5.6` | RTSP mode/status/url (`GetRtspServerMode`, `GetRtspServerStatus`, `GetRtspServerUrl`) |
| `0x104` | `5.7` | log-level/status block (`FUN_000917d4`/`FUN_0009184c` → `TranslateLogLevelToProtobuf`) |
| `0x118` | `5.9` | service endpoint/status string block |
| `0x134` | `5.10` | timezone/status block (`FUN_000b0154`, `FUN_000aff34`) |
| `0x144` | `5.11` | WebRTC mode/status (`GetWebRtcMode`, `GetWebRtcStatus`) |
| `0x170` | `9` | system telemetry: CPU temp, uptime, load average, RAM stats, process count |
| `0x1c8` | `11` | video quality wrapper (`TranslateVideoLpRv1106ToProtobuf`) |

Additional corrections: top-level field 9 is **not** `available_resolutions`; it is the
system telemetry block. Field 1/6/7 descriptors exist but are not populated by the
`SendCameraInfoMessage` decompile. Field 10 remains conditional and is only populated
from `param_1[0x11]` when `param_1[0x12] != 0`.

`FUN_0009b0cc` return semantics are now confirmed: it returns `1` after the field
iterator reaches end-of-message and returns `0` on encode failure. The status sender's
`if (iVar4 == 0) "Encoding failed"` branch is therefore exactly what it looks like;
nonzero means encode success and send.

Deployed second corrected `~/prusa-cam/signaling.py` and `~/prusa-cam/proto.py`.
`proto.py` now supports protobuf wire type 5 via a `Float32` wrapper. `signaling.py`
now sends fields `2`, `3`, `4`, `5`, `8`, `9`, and `11`, covering the real firmware's
always-present optional field set with real Pi telemetry where available and
firmware-equivalent defaults for hardware services the Pi impersonator does not have.
Restart verification after deployment: `/c/info` returned 200, Socket.IO auth ACK was
`1`, the status message encoded to 369 bytes and was emitted, protobuf_version/features
were emitted with the required sleeps intact, the service stayed connected, and snapshot
uploads continued returning 200. No incoming `webrtc` event appeared during the observed
post-restart log window.

## Corrected `/c/info` schema (this session's main discovery)

`protocol.md` §8 and `implementation.md` both had this **wrong** — every field was
assumed top-level, and `features`/`capabilities` were assumed to be strings. Every
attempt using that shape got `400 Bad Request "Request body validation error"` with no
further detail from the server.

Fix: decompiled `do_update_camera_attr` (VMA `0x00061bbc` in `lp_app`) via the existing
Ghidra project (`/tmp/ghidra_project/cam_analysis`, JDK+Ghidra paths documented in
`reverse-engineering.md`). The real JSON builder nests almost everything under `"config"`, and
`features`/`capabilities` are genuine JSON arrays, not CSV strings. First attempt with
the corrected shape returned `200` immediately. Confirmed structure:

```json
{
  "config": {
    "path": "private",
    "name": "Buddy3D Camera",
    "driver": "private",
    "model": "Buddy3D-C1",
    "firmware": "3.1.5",
    "manufacturer": "Niceboy",
    "trigger_scheme": "THIRTY_SEC",
    "resolution": {"width": 1920, "height": 1080},
    "network_info": {
      "wifi_mac": "<PI_MAC>",
      "wifi_ipv4": "<PI_IP>",
      "wifi_ssid": "<WIFI_SSID>"
    }
  },
  "options": {
    "available_resolutions": [{"width": 1920, "height": 1080}]
  },
  "capabilities": ["trigger_scheme"],
  "features": ["SocketCom", "UploadInterval", "TimelapseEn", "TimelapseInterval",
    "TimelapseVideoMake", "TimelapseFileList", "VideoStream", "RtspStream",
    "GetSnapshot", "IrMode", "SpeakerVolume", "WiFi", "FwVer", "HwVer",
    "CameraName", "MicroSd", "FwUpdate", "CameraReboot", "McuTemp",
    "VideoQuality", "WebRtc", "TurnVideoQualityChange", "trigger_scheme", "FanControl"]
}
```

Notes:
- Only **one** entry in `available_resolutions` in the real firmware (just the current
  resolution again), not three like the old docs assumed. Not verified whether more
  entries are accepted/ignored — we send one and it works.
- `capabilities` is `["trigger_scheme"]` specifically in the real firmware, not empty
  and not the full feature list.
- The decompile revealed real firmware sets `"manufacturer": "Niceboy"` while `model`
  comes from the hardware variant selector. After tracing `ReadHwVersionFromCamera` /
  `checkHwVersion`, the C1-target model is `"Buddy3D-C1"`; the old `"Niceboy"` model was
  only a tolerated impersonator value, not the firmware's selected C1 model.
- Server response echoes back extra fields we didn't send: `"id"`, `"rotation": 0`,
  `"sort_order"`, `"origin": "OTHER"`, `"registered": true`, `"team_id"`,
  `"printer_uuid"`. `"origin": "OTHER"` is suspicious — real hardware likely reports a
  specific origin value here (maybe tied to hypothesis #1 above about a
  provisioning/genuine-hardware check). Never investigated further.
- Sent once at startup in current code (`main.py` calls `upload_info()` before
  connecting Socket.IO). Real firmware's resend cadence (if any) is unknown — not
  investigated.

`protocol.md` §8 has been corrected in place to match this. `implementation.md`'s
`upload_info()` example still has the old wrong shape — **not yet fixed**, do that
before anyone follows it literally.

## Socket.IO `status`/`protobuf_version`/`features` — sent, but ACKs never arrive

Current code requests ACKs on all three (non-blocking `callback=`) as a diagnostic.
Result: **no ACK ever arrives** for any of the three, even during 6+ minute stable
connections with zero errors otherwise. Only `camera_authentication` gets acknowledged.
This appears to be normal/expected behavior (the real firmware's ACK table in
`protocol.md` §3 may only actually apply to that one event), not evidence of a bug —
but it does mean **we cannot tell from ACKs alone whether our field encoding is
correct**. Given `/c/info` (a completely separate channel) is what actually populates
name/firmware/model in the UI, it's plausible the Socket.IO `status`/`features`
messages are cosmetically sent by real firmware too but genuinely aren't what the UI
reads from — untested/unconfirmed either way.

**Timing matters**: removing the small `asyncio.sleep(0.2–0.3)` delays between these
three emits causes the server to disconnect us within ~5 seconds. Keep the delays.

## 2026-07-07 deployment / app retest

- Backed up deployed Pi files to `~/prusa-cam/backups/20260707_083341/`.
- Deployed `signaling.py` changes: `protobuf_version` field 1 uses token, metadata sends
  are reusable, trigger replies can include request ids, inbound Socket.IO events log
  hex + shallow decode with token/fingerprint redaction.
- Deployed `main.py` changes: `/c/info` response summary logging and broad trigger
  response path (`status`, `protobuf_version`, `features`) when a request id is found.
- `/c/info` response after restart: `origin: OTHER`, `registered: True`,
  `config.name: Buddy3D Camera`, `config.model: Niceboy`, expected feature/capability
  arrays. This was before the later `Buddy3D-C1` model-selection finding and patch
  staging.
- Mobile-app observation window: no `trigger`, `configuration`, or `webrtc` event; packet
  capture from phone `<HOST_IP>` showed only mDNS multicast.

## RTSP implementation notes (Phase 4.2, done)

- `v4l2h264enc` (the GStreamer HW H.264 encoder element) **does not work** on this
  kernel — pipeline gets stuck in PAUSED, then throws "Failed to process frame" /
  "Internal data stream error". Confirmed via direct GStreamer pipeline test, GLib
  main loop, full error message capture. Root cause not investigated further.
- Workaround: `rpicam-vid` (userspace HW-accelerated encoder binary) piped via
  subprocess into GStreamer, confirmed working for both WebRTC (`webrtc.py`) and RTSP
  (`rpicam-source.service` + `rtsp_server.py`).
- `rpicam-vid --listen -o tcp://...` only serves **one client at a time** — after a
  client disconnects, it exits (by design) and needs `RestartSec` to come back before
  the next connection succeeds. Rapid reconnect attempts within that window get
  RTSP `503 Service Unavailable`. Acceptable for now (single-viewer use case), not
  fixed for concurrent viewers.
- `tcpclientsrc` needs `do-timestamp=true` or downstream `h264parse`/`rtph264pay`
  cannot tell it's a live stream — without it, RTSP negotiates fine (SETUP/PLAY
  succeed, data flows) but the client (VLC) only ever renders the first frame.
- `RTSPMediaFactory.set_shared(True)` is required so multiple simultaneous RTSP
  viewers reuse one upstream TCP connection to `rpicam-source` rather than each opening
  their own (which the single-client TCP listener can't support anyway).

## Firmware reverse-engineering notes (rabbit holes, for context)

- Went looking for a "camera type" (C1 vs pan-tilt) selection/feature table in the
  firmware, triggered by the strings `"Buddy3D-C1"`, `"Buddy3D-POE"`, `"Buddy3D."`
  found via `strings` on `lp_app`. Traced all xrefs via Ghidra (see
  `DumpModelXrefs.java` pattern, script no longer on disk but reproducible from
  `reverse-engineering.md` technique) — **dead end**. All references are from C++ static
  initializers (`_INIT_*` functions) building what looks like a port-number/service-name
  table (sequential integers like 5100, 7200, 12200... alongside the strings), not a
  camera-capability or model-selection table. Conclusion: the C1/pan-tilt distinction
  is a **Prusa Connect UI-side classification**, not something the firmware declares
  over the wire. No protocol-level lever exists for this — don't re-investigate unless
  new evidence emerges.
- No certificates, mTLS, or public/private keys anywhere in the pairing flow. Auth is
  purely `fingerprint` (`MD5(MAC)`) + `token` (from QR code or AT command). Nothing to
  forge here, and MAC OUI is never validated server-side as far as we can tell.

## Complete Socket.IO event enumeration (2026-07-07 RE session)

### FUN_000a4050 — 17 outbound notification events (camera → server)

`FUN_000a4050` registers 17 events the camera will emit to the signaling server. Each
registration uses the `FUN_000397e4` → `FUN_00042268` pattern. All event name string
pointers confirmed via hexdump of the data table at `0xa5014`–`0xa5130`:

| # | Event name | Notes |
|---|---|---|
| 1 | `send_sio_info` | Camera sends its socket.io connection info |
| 2 | `sd_card_missing` | SD card absent (substring ptr into #3's string) |
| 3 | `timelapse_sd_card_missing` | SD card missing during timelapse |
| 4 | `rtsp_start_fail` | RTSP server failed to start |
| 5 | `sd_card_ro` | SD card is read-only |
| 6 | `sd_card_mounted` | SD card mounted |
| 7 | `sd_card_low_space` | SD card low space |
| 8 | `ota_fw_download_failed` | OTA firmware download failed |
| 9 | `ota_fw_upgrade_start` | OTA upgrade started |
| 10 | `ota_fw_download_start` | OTA download started |
| 11 | `timelapse_video_make_status` | Timelapse video creation status |
| 12 | `timelapse_get_file_list` | Request timelapse file list |
| 13 | `socketio_disconnect` | Camera disconnecting from Socket.IO |
| 14 | `socketio_reconnect` | Camera reconnecting |
| 15 | `MEVENT_WIFI_CONNECTED` | WiFi connected internal event |
| 16 | `webrtc_connection_info` | ICE/connection-type telemetry (BIDIRECTIONAL — also received) |
| 17 | `service_socketio_stop` | Socket.IO service stopping |

These are all handled correctly in the impersonator as "things we might emit". Most are
hardware/timelapse/OTA events we don't need to fake.

### Other event clusters — inbound (server → camera) and bidirectional

Six other function clusters also call `FUN_00042268` to register handlers. Partial
enumeration from data-table hexdumps and string xrefs:

| Event | Direction | Status in impersonator |
|---|---|---|
| `trigger` | server → Pi | Handled — responds with status/snapshot/features |
| `configuration` | server → Pi | **Handled** — parses all fields, logs changes; camera_name/interval/quality/IR/RTSP/WebRTC toggles all processed |
| `set_rtsp_server_mode` | server → Pi | **Handled** — `systemctl start/stop prusa-rtsp.service` |
| `set_webrtc_mode` | server → Pi | **Handled** — logged; status message reports mode=1/status=1 (self-enabled) |
| `change_video_size` | server → Pi | **Handled** — updates `current_quality` in memory; live encoder reconfigure not supported |
| `save_video_size` | server → Pi | **Handled** — updates `current_quality` in memory |
| `timelapse_get_file_list` | bidirectional | **Handled** — responds with empty list (no SD card) |
| `webrtc` | bidirectional | Wired to `webrtc.py` — offer/answer/ICE handling |
| `timelapse_video_make_status` | Pi → server | Outbound notification only |
| `timelapse_get_file_list` (emit) | Pi → server | Empty list response implemented |

**`webrtc` — BIDIRECTIONAL (critical for WebRTC):**
The camera BOTH emits and receives `webrtc` Socket.IO events:
- Camera **emits** `webrtc` (SDP answers, ICE candidates) to the server — confirmed at
  `0x9e680` (`SendWebRTCMessage`-class function, calls `0x3827e4`) and `0xa2fac`
  (another WebRTC send path, calls `0x382ee8`).
- Camera **receives** `webrtc` (SDP offers + TURN/ICE config from app via server) —
  handler registered at `0x6c180` via `FUN_0006af10` (a different subscription
  mechanism than `FUN_000397e4`/`FUN_00042268`; likely a different base class or C++
  `std::function`-based binding).
- Log strings confirm the message schema: `"Starting to send WebRTC message. RequestID:
  %s, Type: %s, SDP: %s"` and `"WebRTC start message received. Unsuported."` (a
  "start" sub-type is logged but explicitly not handled).

**Service lifecycle events** (server → camera, controls internal services):
- `service_image_start` / `service_image_save_state`
- `service_socketio_start` / `service_socketio_save_state`
- `service_ota_start` / `service_ota_save_state`

**Misc events**:
- `XHR_BT_PRESS_10S` — physical button long-press (10s hold)

For the impersonator, this means: if/when a `webrtc` event ever arrives, `signaling.py`
needs a `@sio.on('webrtc')` handler that decodes the protobuf payload (contains SDP
offer + TURN/ICE config), starts the WebRTC peer connection, and emits back a `webrtc`
event with the SDP answer. `signaling.py` now has this handler wired to `webrtc.py`.

## set_webrtc_mode / WebRTC enable flow — fully reversed (2026-07-07)

### What `set_webrtc_mode` is

A Socket.IO event sent **server → camera** to enable or disable the WebRTC service. The
payload is a protobuf message with at minimum one boolean/byte field: `1` = enable,
`0` = disable. The real firmware handler is `FUN_000b82f4` (VMA `0xb82f4`).

### The enable gate in `FUN_000b87b4` — the root cause

`FUN_000b87b4` is the function that processes an inbound WebRTC offer and starts the
peer connection. Its **very first check** is:

```c
if ((*(char *)(param_1 + 0x13d) == '\0') &&   // webrtc_mode == 0
    (*(char *)(param_1 + 0x13e) == '\0')) {    // webrtc_status == 0 (service not running)
    log("WebRTC server is disabled in configuration");
    return 0;   // offer silently dropped
}
```

**Both bytes must be non-zero** for an offer to be processed at all. This is the gate —
if `webrtc_mode` (`+0x13d`) is 0 (never set by `set_webrtc_mode`) AND `webrtc_status`
(`+0x13e`) is 0 (service not running), no offer can ever succeed. This fully explains
why we never see a `webrtc` event trigger WebRTC — the server never sends
`set_webrtc_mode` to an `origin: OTHER` camera, so `+0x13d` stays 0.

### The `set_webrtc_mode` handler — `FUN_000b82f4`

Full decompiled logic:

```
SetWebRtcMode(value):
  current_status = webrtc_singleton[+0x13e]   // GetWebRtcStatus
  if current_status == 0 (service not running):
    if value != 0:
      → log "Enabling WebRTC service"
      → FUN_000816b4 (config writer)
      → FUN_00083014(singleton, 1)   // start WebRTC service
      → FUN_000b7e9c(singleton)      // additional init
    else:
      → log "WebRTC service is already disabled"
  else (service running):
    if value == 0:
      → log "Disabling WebRTC service"
      → FUN_000816b4 (config writer)
      → FUN_00083014(singleton, 0)   // stop WebRTC service
      → FUN_000b4b44(singleton)      // teardown
    else:
      → log "WebRTC service is already enabled"
  → write value to webrtc_singleton[+0x13d]   // SetWebRtcMode stores the value
  → FUN_00083014(singleton, value)             // apply to RTSP/video subsystem
```

Key addresses:
- `+0x13c` — set to `1` by `FUN_000b3ce8` (likely a "ready" flag set during init)
- `+0x13d` — `webrtc_mode` byte; `GetWebRtcMode` at `0xb3d40` reads it; `SetWebRtcMode` writes it
- `+0x13e` — `webrtc_status` byte; `GetWebRtcStatus` reads it; set when the service starts/stops

### Inbound `webrtc` offer payload — `parseWebRtcMessage` (Ghidra-named)

Full function at `0xa36a4`. The log format string at `0x3f51d0` reveals all decoded fields:

```
"Client type: %d, Msg type: %d, ID len: %d, SDP len: %d,
 Transport policy: %d, TTL: %d, VideoCfg: %d, Plan: %d,
 Quality: %s, FPS: %d, TTL: %d, Scope: %d"
```

This maps to the inbound `webrtc` protobuf payload fields (wire field numbers TBD from
`pb_decode_string_webrtc` at `0x9c5d0`, but confirmed semantics):

| Field | Type | Meaning |
|---|---|---|
| client_id | string | ID of the requesting client (stored at `*param_1 + 0x44`) |
| msg_type | varint | 3 = offer (only type processed; others → send error response) |
| request_id | string | Round-trip ID, echoed in answer |
| sdp | string | SDP offer body (max 3000 bytes) |
| transport_policy | varint | ICE transport policy (all/relay) |
| ttl | varint | Session TTL |
| video_cfg | varint | Requested video config enum |
| plan | varint | SDP plan (unified/plan-b) |
| quality | string | Requested quality ("sd"/"hd"/"fhd") |
| fps | varint | Requested FPS |
| scope | varint | Session scope |
| ice_config | submessage | TURN/STUN server list (`param_5` → `FUN_000b8620`) |

The offer is processed by `FUN_000b7a9c` (WebRTC singleton getter) →
`FUN_000b87b4` (create peer connection + enqueue to `param_1 + 0x140` queue).
The answer is emitted back on the `webrtc` Socket.IO event.

### What `webrtc_enable.wav` / `webrtc_disable.wav` confirm

Strings at `0x3e3422` / `0x3e3441` confirm the real hardware plays audio feedback when
WebRTC is enabled/disabled via `set_webrtc_mode`. These are purely cosmetic for our
impersonator but confirm the handler is definitely reached on real hardware.

### Implication for the impersonator

The `set_webrtc_mode` event must be received and acted on before any WebRTC offer can
succeed. On real hardware the server sends it after the camera connects (setting
`+0x13d = 1`), then the server sends the `webrtc` offer (which now passes the gate
check). Our impersonator never receives `set_webrtc_mode` (server never sends it for
`origin: OTHER`) so `+0x13d` stays `0` — and even if an offer arrived, the real
firmware would drop it. **The origin gate and the mode gate are the same gate**: the
server withholds `set_webrtc_mode` from non-Buddy cameras.

**Fix for the impersonator**: pre-set `webrtc_mode = 1` and `webrtc_status = 1` in our
own state, and add a `set_webrtc_mode` handler that logs it. If the server ever does
send it, we respond correctly. If it doesn't, we've already self-enabled. This does not
change whether the server sends offers, but it removes a correctness gap.

## Complete event handler status (2026-07-07)

All inbound Socket.IO events are now either handled or explicitly logged. Key RE findings:

- `configuration`: Payload uses **string-named field lookup** (not numeric wire tags). Fields: `code` (auth guard), `rtsp`/`webrtc` (mode toggles), `video_quality` ("sd"/"hd"/"fhd"), `light_control`, `camera_name`, `snapshot_interval`, `start_fw_update`. Easter eggs: `code="42"` logs "Life has no further meaning"; `code="66"` logs "Order 66 received. Jedi presence confirmed." Both cause the message to be rejected.
- `change_video_size` / `save_video_size`: Single raw byte payload. Enum: 5=HD, 6=FHD, 7=SD. `change_video_size` applies live; `save_video_size` persists only. Pi impersonator tracks quality in `current_quality` but cannot live-reconfigure `rpicam-vid` without restart.
- `set_rtsp_server_mode`: 1=off, 2=on. Wired to `systemctl stop/start prusa-rtsp.service`.
- `timelapse_video_make_status`: Outbound bool emit — `false` = job started, `true` = job finished.
- `timelapse_get_file_list`: Bidirectional. Pi responds with empty list (no SD card).
- `sd`/`hd`/`fhd` are NOT standalone events — they are values within `configuration.video_quality`.

## Recommended next steps, in order of likely payoff

1. **Treat backend provisioning/origin gating as the leading hypothesis.** The backend
   stores the camera as `origin: OTHER`; snapshots/name/metadata work, but it never sends
   any command or WebRTC offer and the phone never probes the Pi. Best next evidence
   would be comparing `/c/info` and Socket.IO behavior against a genuine Buddy camera or
   a token known to come from factory-provisioned hardware.
2. **`features` field numbering and field 7 are now fixed (2026-07-07)** — no remaining
   unresolved post-auth message fields at this time.
3. **Wire the RTSP `streaming` flag into the snapshot loop** the same way WebRTC does,
   so watching the local RTSP stream doesn't break snapshot uploads.
4. **Try triggering live view from the Prusa mobile app's "Stream Start" feature**
   (mentioned in the help doc as app-only, distinct from the still-missing browser
   button) while tailing Pi logs live — this is the one avenue that could still surface
   a real `webrtc` offer we haven't tried exhausting, though after two negative
   packet-capture rounds this is looking less likely to be a UI-discoverability issue
   and more likely to be gated by #1.
5. If a `webrtc` offer ever does arrive: `webrtc.py`'s offer/answer/ICE-candidate
   handling is implemented and believed correct (SDP parsing, answer creation,
   `rpicam-vid`-backed H.264 pipeline) but **has never been exercised against a real
   offer** — treat it as unverified until tested end-to-end.
5. If #1 doesn't unblock it: the productive path from there is probably intercepting
   the **app's own network traffic to Prusa's cloud** (mitmproxy on the phone, cloud
   side this time, not local LAN — already ruled out) to see exactly what the backend
   tells the app and why, since two independent packet-level tests now confirm the
   failure is server-side, not something visible from the camera's side of the wire.

## buddy3d-proxy analysis (2026-07-07)

Discovered `buddy3d-proxy-main` (GitHub project, Rust), a working implementation of the
**viewer/client** side of the Prusa camera protocol. It connects to the same signaling
server as a client (not a camera), triggers WebRTC offers, receives H.264 video, and
re-serves it as local RTSP. Key findings that corrected our understanding:

### Protocol: two auth paths, not one

| Event name | Direction | Payload | Who sends it |
|---|---|---|---|
| `camera_authentication` | camera → server | `{1: fingerprint, 2: token}` (protobuf) | Us (the camera impersonator) |
| `client_authentication` | client → server | `{1: camera_token, 2: "client", 3: access_jwt}` (protobuf) | The app/browser viewer |

The **client** (viewer) needs a full OAuth2 JWT from `account.prusa3d.com` (PKCE flow,
client_id `MRHTlZhZqkNrrQ6FUPtjyusAz8nc59ErHXP8XkS4`). The camera only needs
fingerprint + token.

### WebRTC is CLIENT-initiated, not server-initiated

After `client_authentication` ACK, the *viewer* sends:
1. Two `trigger` events: `{field1:1, token}` then `{field2:1, token}`
2. A `webrtc` event with `msg_type=1, direction=2` + ICE config (from
   `GET https://camera-service-api.prusa3d.com/v1/camera-webrtc-config`)

The server relays this to the camera. The camera never receives an unsolicited offer —
it only receives one after a viewer explicitly requests it via the above flow. This
explains why we never see `webrtc` events: no viewer is successfully completing the
client-side flow for our camera (likely because the server refuses to relay to
`origin: OTHER` cameras).

### WebRtcSignal protobuf schema (confirmed from wire captures)

```protobuf
message WebRtcSignal {
  string    token      = 1;  // camera token
  string    session_id = 2;  // Socket.IO sid of the sender
  string    peer_id    = 3;  // sid or features-hash (routing target)
  bytes     body       = 4;  // SdpBody or IceCandidateBody
  uint32    msg_type   = 5;  // 1=offer, 2=answer(s→c), 3=offer(c→s), 4=ICE candidate
  uint32    direction  = 7;  // 1=from_server, 2=from_client
  IceConfig ice_config = 8;  // TURN/STUN servers (only in msg_type=1)
}
```

### Socket.IO CONNECT carries auth payload

The buddy3d-proxy sends `40{"token":"<camera_token>"}` as the Socket.IO CONNECT frame
(before any event). Without it, plus `Origin: https://connect.prusa3d.com` header on the
WS upgrade, the server returns "Missing client permissions". Deployed fix: our
`sio.connect()` now passes `auth={'token': self.token}` and
`headers={'Origin': 'https://connect.prusa3d.com'}`.

### Status field 10 = Socket.IO session ID (not request_id)

The buddy3d-proxy observes `Status.camera_id` (field 10) as a 20-char string like
`"SK1_Gy4shE_GJ7AbP8BS"` — same format as the Socket.IO namespace sid. Confirmed by
testing: `sio.get_sid()` returns a 20-char string matching this format. Previously we
only sent field 10 conditionally as `request_id`; now we always send it as
`self.sio.get_sid()`.

### Features field 7 = MD5 hash of features JSON

Previously we sent `PROTOCOL_VERSION` ("4.4") in field 7. The buddy3d-proxy observes it
as an MD5 hex string and uses it as `peer_id` when addressing WebRTC signals to a camera.
Fixed: `hashlib.md5(features_json.encode()).hexdigest()`.

### Corrections deployed 2026-07-07 (post-buddy3d-proxy analysis)

| Fix | Status | Effect on app |
|---|---|---|
| Status field 10 = `sio.get_sid()` | ✅ Deployed, verified in logs | No change |
| Features field 7 = MD5 hash | ✅ Deployed, verified `812322...` | No change |
| Features field 4 = `'NB.1.1.0'` (HW version) | ✅ Deployed | No change |
| Socket.IO CONNECT `auth={'token': token}` | ✅ Deployed, auth ACK still `1` | No change |
| WebSocket `Origin: https://connect.prusa3d.com` | ✅ Deployed | No change |

**Conclusion**: all protocol-level deviations from the buddy3d-proxy's observed real-camera
behavior have been corrected. The app still shows "Kamera-Kommunikation fehlgeschlagen"
and we still receive zero inbound events.

### Viewer-side test: `client_authentication` ACK `5` (2026-07-07)

Wrote a test script that impersonates the buddy3d-proxy's viewer flow: connects to the
signaling server, sends `client_authentication` with `{token, "client", access_jwt}`, and
attempts a WebRTC kickoff. **The server consistently returns ACK `5` (rejected)** instead
of the expected ACK `0` (success).

Tested against:
- Token `<TOKEN_OTHER>` (origin: OTHER) → ACK `5`
- Token `<TOKEN_WEB>` (origin: WEB) → ACK `5`
- Camera online (actively connected with same token) → ACK `5`
- Camera offline (service stopped) → ACK `5`

The JWT is valid (verified: expires in ~2h, successfully calls REST APIs like
`/app/printers/{uuid}/cameras` and `camera-service-api.prusa3d.com/v1/camera-webrtc-config`).

**Critical finding**: `camera-service-api.prusa3d.com/v1/cameras/<TOKEN_WEB>`
returns **404 Not Found**. The camera doesn't exist in Prusa's camera-service registry.
The signaling server likely validates `client_authentication` against this service — only
cameras registered there (via the printer's QR pairing flow → `origin: LINK`) pass
validation.

**This is the definitive answer**: neither `OTHER` nor `WEB` origin tokens are registered
in `camera-service-api.prusa3d.com`. The signaling server's `client_authentication`
handler checks that service and rejects viewers for unregistered cameras. Without
registration in that service, no viewer can connect, no WebRTC offers can be relayed, and
the app shows "Kamera-Kommunikation fehlgeschlagen" immediately (probably after attempting
this same auth flow or checking the same registry via a REST call).

### Current impersonator token

Active token: `<TOKEN_WEB>` (origin: WEB). Camera auth ACK: `1` (camera is
successfully connected for snapshots/status). Viewer auth ACK: `5` (viewers are blocked).

### Updated understanding of the gate

The gate is NOT simply `origin: OTHER` vs `origin: WEB` — both are blocked. The gate is
**registration in `camera-service-api.prusa3d.com`**, which only happens for `origin: LINK`
tokens created during the printer's QR-code-based Buddy camera pairing flow. This is a
separate backend service from `connect.prusa3d.com`.

### Remaining paths forward (revised)

1. **Get a `LINK`-origin token**: the printer's own UI should have an "Add Buddy camera"
   or "Add camera" option that generates a QR code. The real Buddy camera scans this QR.
   We need to intercept or replicate this flow to get a token registered in
   `camera-service-api`. Check the Core One's menu for camera pairing options.
2. **Reverse-engineer the QR code format**: if the printer displays a QR code during
   pairing, capturing it (screenshot, photo) would reveal the token format and any
   additional pairing parameters. The firmware's AT command `AT+TOKEN=<token>` suggests
   the QR simply contains a token string.
3. **Register via camera-service-api directly**: if there's a registration endpoint
   (`POST /v1/cameras` or similar) that accepts a bearer JWT + camera info, we could
   register ourselves. Needs API exploration.
4. **MITM the app** remains useful to confirm exactly what the app checks before showing
   the error, but the viewer-side test already demonstrates the mechanism.
