# Project Status — Prusa Buddy3D Camera Impersonator

Current, thematic status of the project (not a session log). Read this first; it is the
ground truth on what works, what is blocked, and what is open. Wire-level detail lives in
[`protocol.md`](protocol.md), firmware detail in [`journal/findings.md`](journal/findings.md),
and everything we once believed and later disproved in [`dead-ends.md`](dead-ends.md).

Evidence markers: **[confirmed]** = verified live against the real backend or firmware.
**[assumption]** = inferred, not proven.

---

## Bottom line

A Raspberry Pi impersonates the camera end to end: **snapshots, `/c/info`, Socket.IO
auth, settings, RTSP, and — as of 2026-09-19 — the app's live WebRTC video stream
works** (verified live). The earlier "backend gate / no offer relayed" story is
superseded; see the 2026-09-19 section below.

Timelapse storage is **live-confirmed (2026-09-19)** and **persistent across reboots
(2026-09-20, `GAP-PERSIST-01`)**: the app shows timelapse as available with
size/used/free and a configurable interval, and the store now survives a real reboot.
See the 2026-09-20 section below and `firmware-implementation-gap-tracker.md`
(`GAP-TIMELAPSE-01`).

### 2026-09-19 — live WebRTC works; config is protobuf; emulated SD added

The WebRTC blocker was **not** a backend gate. Four firmware-parity bugs were fixed,
each verified live:

1. **Auth field order + ACK value.** `camera_authentication` must be
   `field1 = token, field2 = fingerprint` and the success ACK is **`0`**
   (`FUN_000a3058` / `FUN_000a05e4` / `FUN_0009e53c`). We had them swapped and
   required `1`, so the server ACKed `1` (an error) and closed the session.
2. **ICE config + TURN.** The inbound `webrtc` ICE config (descriptor `0x3f7680`,
   tag8) carries STUN **and TURN** servers with a time-limited username/base64
   credential; `webrtcbin` needs `stun-server` **and** `turn-server`.
3. **The camera is the offerer.** Flow: ICE config → camera **offer** (type 3) →
   viewer **answer** (type 2) → **trickle candidates** (type 4). Viewer candidates
   arrive as `a=candidate:…` with `tag2 = mid` and must be applied
   (`add-ice-candidate`); they were previously dropped.
4. **H.264 SPS profile.** The Connect answerer (a libdatachannel endpoint)
   validates the stream SPS and rejects the v4l2 SPS (`428029`, level 4.1) with
   `m=video 0`. Transcoding overloads the Pi, so `stream_mux` serves an
   **SPS-patched copy on port 8889** (profile/constraint/level → `42 e0 1f`,
   constrained baseline level 3.1, matched by NAL type); the WebRTC branch reads
   8889. After the patch the answer is `m=video 9 …` and the stream plays.
   Requires `gstreamer1.0-nice` (installed by `deploy.sh`/`bootstrap.sh`).

**`configuration` is a nested protobuf, not JSON** (descriptor `0x3f73a4`,
dispatcher `FUN_000a7940`; the earlier `FUN_000a89e0` "handler" was not a function).
Live-mapped: `tag8.1` = video quality (1=SD/2=HD/3=FHD), `tag3.4` = `light_control`
(IR sun/moon/auto), `tag3.5` = `set_snapshot_upload_interval` (10..600), `tag3.10` =
`set_printing_job_name` (active print-job name). **Wave 2:** `tag3.7` is plausibly
`set_volume` (5..100) `[assumption]`; `tag3.11`/`tag3.12` are **uvarints** and the earlier
"RTSP candidate" label is **refuted** (the RTSP mode is a small enum toggle elsewhere). The
JSON parser is only the QR/manual-config path.

**Capabilities pruned:** `IrMode`, `SpeakerVolume`, `FanControl` removed from the
`/c/info` features (no hardware); `MicroSd` kept and backed by an emulated SD at
`/mnt/sdcard` (SMB share `sdcard`, verified accessible).

**Timelapse storage (live-confirmed 2026-09-19; capture/list round-trip pending):** the
app reads storage from the `status` message (`extended_status.4` storage block, descriptor
`0x3f72b0`). The descriptor is **tags 1-4 uvarint + tag 5 string**: tag1 = mounted
state (1=mounted, 2=absent; `FUN_000744ac`), tags 2/3/4 = total/free/used MB
(`FUN_000745e0`, `statvfs` MB), tag5 = mount mode (`"RW"`/`"RO"`/`"UNKNOWN"`;
`FUN_00073914`). The earlier getter mapping (`FUN_000abcb0`/`FUN_000abaf4`) was wrong:
those are `timelapse_status` field-2 getters, not this block, and `MODEL` never belonged
on tag 5. `timelapse.storage_status` reports the emulated SD at `/mnt/sdcard` and
`signaling` supplies it. **Confirmed in Connect 2026-09-19:** timelapse is available, the
storage page shows size/used/free, and the interval is configurable. **Live end-to-end test
2026-09-19:** enable/disable works via trigger tag 5 and frames are captured to
`/mnt/sdcard/timelapse`; the app's interval change arrives as `configuration {2: <seconds>}`
(=`set_timelaps_interval`, dispatcher `FUN_000a7940`), and after wiring it the log shows
`Config: timelapse_interval → 30s` with frames exactly 35 s apart. **Limitation:**
`/mnt/sdcard` is on the read-only overlay root, so recordings were lost on reboot — **fixed
2026-09-20 (GAP-PERSIST-01, live-verified):** the SD was repartitioned (p2 → 10.3G, new 4G ext4
`mmcblk0p3` LABEL `PERSIST` mounted at `/data`). `/data/prusa-cam/state.json` now holds the runtime
settings and `/data/sdcard` is bind-mounted onto `/mnt/sdcard`. After a real reboot: `/data` mounted,
`quality.env` restored to `1280x720` (encoder `--width 1280 --height 720`), frames/`.avi`/CSV survived,
the file list still returned the `.avi`, SMB unchanged, and `quality.live.env` stayed ephemeral. **Live 2026-09-19 (deployed `5ad9263`):** the app set 15 s then 10 s
intervals (`configuration {2: …}`) and 8 frames recorded as `timelapse_<HH-MM-SS-mmm>.jpg` (~15 s
apart); running `build_avi` on those frames produced an `.avi` that `file(1)` confirms as
*"AVI, 1920x1080, 10.00 fps, video: Motion JPEG"* with a `<name>:D` `.timelapse_videos.csv` row.
This app version exposes **no make-video button or file-list view**, so the `0x3f701c` `file_list`
envelope is implemented and unit-tested but not app-exercisable (make-video verified directly).

### 2026-09-20 — browser WebRTC, stable signaling session, persistence, thermal

Five follow-ups landed after the 2026-09-19 WebRTC work; live-verified unless noted:

1. **Browser WebRTC works (live-verified).** The remaining blocker was candidate
   extraction: the viewer's trickle candidates (`a=candidate:…`) are now parsed by
   `proto.find_webrtc_candidate` and applied to `webrtcbin`. `webrtcbin` exposes no
   `on-ice-connection-state-change` signal, so ICE state is observed through the
   `notify::ice-connection-state` property; a 30 s watchdog plus the ICE
   `failed`/`closed`/`disconnected` states clear `state.streaming` and resume the
   snapshot loop (verified). WebRTC now plays live in **both** the Prusa app and the
   browser.
2. **Stable signaling session (live-verified).** The intermittent
   `CameraIsNotSessionMemberError` was the unsolicited post-auth burst
   (`send_sio_info` + `status` + `protobuf_version` + `features`). Removing it
   (`ba48dc8`) restores firmware parity — the camera sends **nothing** after a
   successful `camera_authentication` — and the session is now stable: zero errors and
   one connection.
3. **Persistence (`GAP-PERSIST-01`, live-verified across a real reboot).** The SD was
   repartitioned (p2 → 10.3G, new 4G ext4 `mmcblk0p3` LABEL `PERSIST` mounted at
   `/data`). `/data/prusa-cam/state.json` persists the runtime settings, `/data/sdcard`
   is bind-mounted onto `/mnt/sdcard`, and `persist_restore.py` (root,
   `pi-persist.service`) restores them at boot. Verified across a reboot: `/data`
   mounted, quality restored (`1280x720`, encoder really at that size), timelapse
   frames/`.avi`/CSV survived, SMB unchanged, and `quality.live.env` stayed ephemeral.
4. **Boot forensics (`GAP-DEVICE-03`, deployed).** `bootlog.service` appends the boot
   reason, `get_rsts`/`get_throttled`, temperature, and dmesg to
   `/boot/firmware/bootlog.txt` each boot, and `rpicam-source` now runs
   `rpicam-vid -v 0` so its ~30 lines/s frame stats stop flooding the volatile journal.
5. **Thermal throttling (live-observed).** Throttling is **thermal** — 81–84 °C
   continuously on the passively cooled Pi Zero 2 W, `get_throttled = 0x60002` (ARM
   frequency capped now, hard-throttle latched), **no under-voltage ever**. The ~21:07
   reboot cause is **undeterminable** (volatile journal, no RTC, RAM overlay upper
   layer); most likely an external power cut or a hardware-watchdog reset. Hardware
   action still needed: a heatsink/fan or a lighter stream (720p/15 fps).

---


**[confirmed 2026-07-09, revised]** The `origin: LINK` hypothesis (below) is **dropped as the
leading lead** — it was chasing the wrong origin. Two official Prusa documents settle this:

1. The official Buddy3D quick-start manual ("Buddy3D Camera for Prusa Core One", v1.00) describes
   the *actual* pairing flow for a genuine camera: Connect web UI → pick a printer → **Camera tab
   → "Add WiFi Camera"** → enter the target Wi-Fi's credentials → **"Generate QR Code"** → put the
   camera in pairing mode (hold RESET 1 s) → aim the camera's own lens at that QR from ~50 cm. The
   QR carries Wi-Fi credentials plus a registration token; the camera reads it optically and
   registers itself. Per the OpenAPI spec's own wording for the `origin` parameter — *"Use OTHER
   for camera registration via api. WEB is used when registering camera via web qr code"* — this
   flow is `api`-style registration, i.e. **`origin: OTHER`**. That is the same origin our
   impersonator already uses.
2. A separate, unrelated Prusa guide ("Camera setup for PrusaLink / Prusa Connect") documents
   `origin: LINK`: it's for a CSI/USB webcam wired directly into the Raspberry Pi that runs
   PrusaLink (i3-series MK2.5/MK3/MK3S+, or natively on MK4/MK3.9/XL), switched on via a **"Link
   camera to Connect"** toggle in PrusaLink's own web UI. This is a different product line
   entirely — plain webcam snapshot linking, no Socket.IO/protobuf/WebRTC signaling of any kind is
   mentioned anywhere in that guide.

**Net effect:** a genuine Buddy3D camera and our impersonator register with the *same* origin
(`OTHER`). Origin was never the differentiator, so `origin: LINK` was very likely never going to
unblock WebRTC for a Buddy3D-style camera even if we obtained one. See `dead-ends.md` for the
full correction and `next-steps.md` for the revised priority order.

**[new, unconfirmed]** Prusa's own blog post announcing Buddy3D/Prusa-App camera-control updates
(2025) describes live streaming as a **staged rollout**: the initial release was **local-network
only**, with cloud/remote streaming — quote: *"we'll use the encrypted WebRTC protocol"* —
explicitly announced as landing **later in 2025**. This is a plausible, more mundane alternative
explanation for the same observed symptoms (ACK `5`, 404 in the registry): cloud WebRTC may simply
not have been (or may still not be) broadly enabled server-side, independent of any
spoofing-detection mechanism. This reframes, but doesn't replace, the remaining open leads (MAC/
OUI check, direct `camera-service-api` registration) — see `next-steps.md`.

### 2026-07-09 (later) — origin and network-reputation ruled out by live experiment

Two hypotheses were fully closed today with controlled, live tests (not just documentation
reading):

1. **`origin: WEB` vs `OTHER`, isolated.** The account's actual deployed camera turned out to be
   `origin: WEB` all along (the "Camera > Token" web-UI flow defaults to `WEB` unless `OTHER` is
   explicitly requested — nobody had verified which one was live). Registered a **fresh** camera
   via `POST /app/printers/{uuid}/camera?origin=OTHER`, pointed the impersonator at it (fully
   correct protocol, `registered: true`), and got **identical results**: `client_authentication` →
   ACK `5`, `GET camera-service-api.../v1/cameras/<token>` → `404`. Origin is conclusively not the
   gate — this supersedes the earlier, weaker claim that both origins had been tried (that older
   test predates the 2026-07-06 `/c/info` schema fix and was likely comparing apples to oranges).
   The impersonator now runs the `OTHER`-origin token going forward (more representative of a real
   camera); the old `WEB`-origin config is backed up on the Pi.
2. **Source-IP / network reputation.** Replayed the exact same `client_authentication` handshake
   for the new token from a second machine on a completely different network (different ASN, not
   the home residential connection) — **identical ACK `5`**. Rules out WAF/geo/ASN-reputation
   blocking as an explanation.

**Also investigated and ruled out as a comparison point:** the account has a second, genuine Prusa
camera — an ESP32-Cam running Prusa's own open-source
[`Prusa-Firmware-ESP32-Cam`](https://github.com/prusa3d/Prusa-Firmware-ESP32-Cam) firmware,
`origin: OTHER`, on a different printer. Initially looked like a huge lead (a *working* camera on
the same account to diff against), but its live view turned out to be periodic snapshots only, not
WebRTC — confirmed from its own README, that firmware never implements Socket.IO/WebRTC signaling
at all (`features: []`, `capabilities: []` in `/c/info`). So it doesn't test the WebRTC gate one
way or the other; it's simply a different, snapshot-only integration tier of the same public
Camera API. **Confirmed 2026-07-09:** this ESP32Cam is *also* absent from the
`camera-service-api` registry (`404`, same as ours) — consistent with that registry being scoped
to WebRTC-capable enrollment specifically, not "every camera on the account."

### 2026-07-09 (later still) — `camera-service-api` surface probed directly, no hidden path found

Probed `camera-service-api.prusa3d.com` beyond the one known endpoint: root (`GET /`, no auth) →
`200 {"app":"camera-service-api","version":"0.6.7","healthy":true}` — the only endpoint that
responds with anything other than a uniform 404. Every other guess (`/v1/cameras` list, `/v1/camera`
singular, `POST /v1/cameras`, `POST /v1/cameras/<token>/register`, `/health`, `/v1/health`,
`/openapi.json`, `/docs`, `/v1/`) returned the identical `404 Resource not found` body regardless
of auth style (account Bearer token vs. device Token/Fingerprint headers) or HTTP method.
`OPTIONS /v1/cameras/<token>` → `204` (the route pattern exists, as expected — this is just CORS
preflight, not new information). No hidden registration path, no informative error, no version- or
feature-flag hint anywhere in the responses. This was the cheapest remaining software-only probe
and it came back clean/negative like everything else today.

**Net effect:** origin, source-network reputation, hidden registry endpoints, and firmware 3.1.6
protocol changes are ruled out. One software-controlled variable needs an exact retest:
MAC/fingerprint consistency using the OEM uppercase preimage. After that, the remaining
explanations are a genuine-hardware allowlist or a backend feature-rollout gate — see
`next-steps.md` for the current priority order.

The framing and message schemas now match firmware; device-identity consistency is the remaining
software-controlled variable.

### 2026-09-18 — 3.1.6 exhaustive audit and live gate retest

The initial sampled 3.1.5→3.1.6 comparison was expanded to every Ghidra-defined function:
10,548 functions in 3.1.5 and 10,552 in 3.1.6, with zero decompiler failures. Exact
relocation-insensitive Function-ID and call-target hashes match for `/c/info`, nanopb encoding,
WebRTC dispatch/gating, WebRTC answer/candidate encoding, numeric message-type translation, and
ICE candidate emission. The only relevant OEM logic delta remains the expanded hardware-version
range table; 3.1.6 contains no streaming enrollment or signaling change. **[confirmed]**

A fresh read-only test using the logged-in Connect session returned the deployed camera as
`origin: OTHER`, `registered: true`, firmware `3.1.6`, with its full WebRTC feature list. The same
camera token still returns `404` from `camera-service-api /v1/cameras/<token>`, while the account
successfully receives current TURN/STUN configuration. A fresh Socket.IO viewer probe connected
but `client_authentication` again returned ACK `5`. **[confirmed after deployment and reboot]**
The rejection therefore still happens before any offer can reach camera code.

The audit did expose a separate latent impersonator bug: it treated camera-side message types as
strings and read inbound SDP from field 3. Firmware uses numeric values
`1=request, 2=answer, 3=offer, 4=candidate`, inbound SDP in field 4, and outbound payload in field
3. The implementation now uses the recovered flat camera schema and has wire-format unit tests.
This is necessary for an offer to work after enrollment is unblocked, but cannot change ACK `5`.

---

## Status at a glance

| Capability | State | Evidence |
|---|---|---|
| Snapshot upload → Prusa Connect | ✅ Working | `PUT /c/snapshot` → 200, image updates every 10 s **[confirmed]** |
| Camera identity / auth (Socket.IO) | ✅ Working | `camera_authentication` → ACK `0` **[confirmed]** |
| Camera info / metadata (`/c/info`) | ✅ Working | 200; name, firmware, model, Wi-Fi shown in app **[confirmed]** |
| Appears online & paired, survives reboot | ✅ Working | web + mobile app; `Restart=always` **[confirmed]** |
| Local RTSP live view | ✅ Working | `rtsp://<pi>:8554/live` in VLC, 1080p, `--rotation 180` **[confirmed]**; the firmware default is `554` (`FUN_000b04d4`), so `8554` is a documented privileged-port Pi exception that Connect consumes (GAP-RTSP-01 closed) |
| Dynamic video-quality tier-switching | ✅ Working (live-verified) | raw-byte mapping `{5:1,6:2,7:3}`; the app set HD and the encoder ran `1280x720` (GAP-QUALITY-01 closed); the persisted tier survives reboot (GAP-QUALITY-03); the TURN/scoped-quality lock is implemented (`state.turn_online` + `quality_change_allowed`). `GAP-QUALITY-02`'s persist flag is a **per-payload** flag for `change_video_size` (`FUN_00072f08`); the `save_video_size` handler is in an unexported gap `[assumption]`. |
| Classified as a genuine Buddy camera | ❌ No | listed under "Other cameras" **[confirmed]** — a UI classification only. It was never the WebRTC blocker; the superseded "registry-membership gate" theory is in `dead-ends.md`, and genuine cameras are `origin: OTHER` too. |
| Live WebRTC stream (app + browser) | ✅ Working | offer/answer/ICE completes and video plays live in both the Prusa app and the browser **[confirmed 2026-09-19/20]**; the video-only offer is accepted (audio optional, GAP-WEBRTC-07 closed) and the inbound field names are confirmed (GAP-WEBRTC-05) |

---

## What works (confirmed)

- **Snapshots** — `PUT /c/snapshot` to `webcam.connect.prusa3d.com` with headers
  `Token` / `Fingerprint` / `User-Agent: Buddy3D Camera` / `Content-Type: image/jpg`. Visible
  and updating in Prusa Connect.
- **Socket.IO auth** — `camera_authentication` with the 2-field protobuf
  (`token`, `fingerprint`); server ACKs a bare `0` (success; `1`/`2` are errors).
- **`/c/info` metadata upload** — the corrected schema (almost everything nested under
  `config`, `features`/`capabilities` as JSON **arrays**) returns 200 and populates the
  camera's name, firmware, model `Buddy3D-C1`, and Wi-Fi details. Firmware `3.1.6` was deployed
  and live-verified after reboot on 2026-09-18. Full body in [`protocol.md` §8](protocol.md).
- **Local RTSP** — `rpicam-vid` (userspace HW H.264) → TCP → GStreamer `GstRtspServer`.
  Continuous video (needs `do-timestamp=true` on `tcpclientsrc`). Single upstream client;
  see [RTSP notes in `protocol.md` §12](protocol.md). Default **1080p @ 30 fps**, `--rotation 180`
  (camera mounted inverted).
- **Dynamic video-quality plumbing (partial)** — configuration strings (`sd`/`hd`/`fhd`) can
  reconfigure the encoder, and `main.py handle_quality()` writes the tier to
  `/etc/prusa-cam/quality.env` before restarting the source. Resolutions in `quality.py` are
  correct: SD 640×480 / HD 1280×720 / FHD 1920×1080. The raw
  `change_video_size`/`save_video_size` handler now maps bytes correctly (`{5:1,6:2,7:3}`,
  implemented + unit-tested). Firmware 3.1.6 uses `5=SD`, `6=HD`, `7=FHD`, and its shared
  handler persists only when a **per-payload** callback flag is nonzero (Wave 2). The
  TURN/scoped-quality lock is implemented (`state.turn_online` + `quality_change_allowed`).
  Until `GAP-QUALITY-01` live verification and `GAP-QUALITY-02` flag wiring close, raw-event
  parity is not confirmed.

### Streaming latency (measured 2026-07-14, [confirmed])

Headless measurement (ffmpeg from a LAN host, time-to-first-frame over RTSP):

- **Warm join ≈ 70 ms** — server-side delivery is fast; `rpicam-vid` emits a keyframe on each
  new client connect (`--inline`), so first-frame time is ~70 ms whether or not a viewer was
  just connected.
- **Cold join ≈ 2.5 s (one-time)** — when the camera is idle (no viewers), the first connection
  waits on OV5647 acquisition; `rpicam-vid --listen` only grabs the sensor when a client arrives.
- **Tuning applied** (kept — safe, negligible cost): `--intra 30 --flush` on the encoder,
  `factory.set_latency(0)` and a bounded `queue max-size-buffers=1 leaky=downstream` in
  `rtsp_server.py`. These reduce steady-state jitter accumulation and mid-stream loss-recovery
  latency; they did **not** move the warm-join metric (rpicam keyframes on connect gate it, not GOP).
- **The >1 s a viewer perceives is client-side buffering**, not the Pi. VLC's default
  `network-caching` is 1000 ms — set it low (e.g. `vlc --network-caching=100 rtsp://<pi>:8554/live`)
  to see the true ~70 ms server latency. This is not a server bug to keep chasing.
- **Online/paired state** — camera shows online in web and app, correct metadata, auto-recovers
  on crash/reboot via three `enabled` systemd services.

---

## The core blocker: live WebRTC streaming

> **[superseded 2026-09-19/20]** Retained as history. **WebRTC works live** in both the
> app and the browser (see the 2026-09-19 and 2026-09-20 sections above); the
> "backend gate / no offer relayed" story below was wrong — it was four firmware-parity
> bugs (auth order/ACK, ICE+TURN, camera-is-offerer + candidate handling, SPS patch).
> By this repo's convention, superseded material belongs in
> [`dead-ends.md`](dead-ends.md); this passage is kept here pending a move.

### Root cause

**Confirmed by direct test:** the viewer handshake `client_authentication` is rejected with
ACK `5` for both our tokens (`OTHER` and `WEB`), and a direct lookup
`GET camera-service-api.prusa3d.com/v1/cameras/<token>` returns **404** — the camera is not in
that registry. So the server never relays a `webrtc` offer. The firmware shows the matching
camera-side gate: `FUN_000b996c` silently drops any offer unless `webrtc_mode` (`+0x13d`) and
`webrtc_status` (`+0x13e`) are both set, and those are only set when the server sends
`set_webrtc_mode`.

**Inferred, not verified:** that `camera-service-api` registration is the precise gate. What
gates entry into that registry is now the open question — **not** `origin`, since genuine Buddy3D
cameras register as `origin: OTHER` too (confirmed from the official pairing manual — see Bottom
line). Two live leads: (a) a real-hardware allowlist keyed on something we haven't identified
(MAC/OUI is the cheapest untested candidate — `next-steps.md` P.2), or (b) cloud WebRTC streaming
is a staged rollout not yet broadly enabled server-side, unrelated to any per-camera check (Prusa's
own 2025 blog post on the Buddy3D/App update describes exactly this staged local-then-cloud
rollout). See [`protocol.md` §5 (server-side gate) and §10 (enable gate)](protocol.md), and
[`next-steps.md`](next-steps.md) for the revised priority order.

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
| A missing **unique factory serial** on the wire | **Ruled out (2026-07-09)** | Traced the `CameraInfoMessage` extended-status sub-block at offsets `0x0b8`-`0x0cc` (headless Ghidra, `FUN_000a01dc` + xref chase — see `protocol.md`'s `extended_status` section). One offset (`0x0c4`) *is* hardware-derived — a model-name string read from the real SPI HW-version chip (`/sys/class/spi_master/spi2/spi2.0/version`) via a small fixed range table — but it's a small-cardinality **model/variant** string (`"Buddy3D-C1"` etc.), not a unique per-device value. An exhaustive `.rodata` substring search for `"serial"`/`"Serial"`/`"SERIAL"`/`"otp"`/`"OTP"`/`"SN:"`/`"factory"` found **zero hits** anywhere in the binary — no distinct factory-serial/OTP getter exists. `journal/findings.md` §7.2's "Serial Number (SN) — OTP memory" row is unconfirmed community/inference, not a traced fact; treat it as superseded pending a citation. |
| The local check is "RTSP-shaped" | Ruled out | warning identical with local RTSP up or down |
| `origin: LINK` is the WebRTC unblock for a Buddy3D-style camera | **Downgraded (2026-07-09)** | The official pairing manual shows genuine Buddy3D cameras register as `origin: OTHER` (same as us) via Connect's "Add WiFi Camera" QR wizard. `LINK` belongs to a separate, unrelated product — CSI/USB webcams wired into a Raspberry Pi running PrusaLink, toggled on via a "Link camera to Connect" button, no WebRTC/Socket.IO involved at all. See `dead-ends.md`. |
| `origin` (`WEB` vs `OTHER`) is the WebRTC gate | **Ruled out — confirmed by live experiment (2026-07-09)** | Registered a fresh `origin: OTHER` token, redeployed the fully-correct-protocol impersonator against it (`registered: true`) — identical ACK `5` + registry `404` as the pre-existing `origin: WEB` token. Also corrects an earlier, weaker claim: `origin: WEB` is **not** restricted to the browser-webcam client — our impersonator ran the full Socket.IO/protobuf protocol successfully on a `WEB`-origin token for the whole project up to this point (auth ACK `0`, `/c/info` 200, snapshots all worked). `origin` appears to be account-side metadata, not a protocol-level access restriction. |
| Source-IP / network reputation (WAF, geo, ASN, residential-IP blocking) is the WebRTC gate | **Ruled out — confirmed by live experiment (2026-07-09)** | Replayed the identical `client_authentication` handshake from a second machine on a completely different network (non-residential, different ASN) — same ACK `5`. |
| MAC/OUI or MAC/fingerprint consistency is the WebRTC gate | **Inconclusive; exact retest needed** | The 2026-07-09 guessed-Realtek-OUI run produced identical ACK `5` + `404`, but the one-off MD5 preimage was not preserved. The full 3.1.6 trace now proves the firmware hashes the exact uppercase colon-separated MAC string; the recorded test MAC was mixed-case, so firmware-consistent pairing cannot be established retrospectively. No confirmed genuine Buddy3D OUI is available. |
| mitmproxy would show the real app's `connect.prusa3d.com`/`camera-service-api` traffic | **Blocked, not just untried (2026-07-09)** | Proxy + cert trust confirmed working (other domains decrypted cleanly), but zero requests to any Prusa Camera API domain appeared despite confirmed time on the camera view — consistent with certificate pinning on that traffic specifically. |

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
- **No device certificate or asymmetric key to forge** — auth is `fingerprint = MD5(MAC)` plus
  the server-minted token credential; no certs or keypairs appear in the pairing flow. The token
  is a random 20-character value minted by Connect from a bodyless registration request
  (`printer_uuid` + `origin`); firmware only reads it from the QR/serial command and persists it.
  The wire fingerprint is lowercase MD5 of the firmware's uppercase colon-separated MAC string
  (or a random fallback seed). See [`protocol.md` §0–2](protocol.md) for the complete 3.1.6 trace.
  The repository implementation now derives this value automatically from `wlan0`; the deployed
  camera still uses its previously bound static value until a fresh token is issued for migration.

---

## Current deployment state

**Hardware:** Raspberry Pi Zero 2 W, Debian 13 (trixie), OV5647 (Pi Cam v1, 1920×1080, mounted
inverted → `--rotation 180` on all capture paths). App in
`~/prusa-cam/` (venv `--system-site-packages`). Paired to a Prusa CORE One. Live token in
`~/prusa-cam/config.ini` (secret; not in repo). **2026-07-09:** switched to a freshly-registered
`origin: OTHER` camera (currently id `577960`, see the origin-ruled-out experiment above) — the prior
`origin: WEB` camera (id `572286`) is deregistered from active use but still exists on the
account; its config is backed up on the Pi as `config.ini.bak.<timestamp>`.

**Services (systemd, `enabled`, survive reboot):**

| Service | Role |
|---|---|
| `prusa-cam.service` | `main.py` — `/c/info`, snapshot loop, Socket.IO signaling, WebRTC answer logic |
| `rpicam-source.service` | `rpicam-vid --listen` → H.264 over TCP :8888 (single client); resolution from `EnvironmentFile=/etc/prusa-cam/quality.env` (tier-switchable), `--rotation 180 --intra 30 --flush` |
| `prusa-rtsp.service` | GStreamer RTSP → `rtsp://<pi>:8554/live`, pulls from rpicam-source |

The current local worktree additionally contains an always-on `prusa-ha-rtsp.service`
(`rtsp://<pi>:8555/live`) and an ONVIF/WS-Discovery facade for Home Assistant. These additions are
unit-tested but are not included in the live-deployment claims above until explicitly deployed and
verified. They share the existing encoder/mux and leave Prusa's mode-controlled `:8554` endpoint,
Socket.IO, and WebRTC signaling paths unchanged.

**What the impersonator currently sends (latest valid state):** corrected `/c/info` and
`camera_authentication` (`token`, `fingerprint`). After a successful auth the camera sends
**nothing** (firmware parity, `FUN_000a05e4`); there is **no `send_sio_info` Socket.IO event**.
`status` (fields 2/3/4/5/8/9/11 — real network + telemetry, defaults elsewhere),
`protobuf_version` (token in field 1, `"4.4"` in field 7), and `features` (bracket-wrapped
array, field 7 = MD5 hash) are **trigger-driven** — sent in response to trigger tags 1/2/12
respectively, not on connect. Inbound `configuration`/`trigger`/`set_rtsp_server_mode`/
`set_webrtc_mode`/`change_video_size` are handled; `set_webrtc_mode` applies the firmware
enable/disable gate (configured `webrtc_mode` and runtime `webrtc_status` tracked separately,
offers rejected only when both are zero); RTSP keeps configured `rtsp_mode` and actual service
state separate, with the direct event and the `configuration` `rtsp` field sharing one start/stop
path; `webrtc` is wired to `webrtc.py`. Trigger tag 9 (reboot) dispatches through a rate-limited
(60 s), narrowly scoped `systemctl reboot` path in `device_control.py`; a second request inside
the window or a failed command is logged and never reported as success. IR/speaker/fan/MicroSD
are represented as unavailable on `CameraState`, and `configuration.light_control` is logged and
rejected instead of being reported as applied. **GAP-DEVICE-02 closed (Wave 2):** the truthful
`MicroSd` status is the `extended_status.4` storage block (already emitted correctly), and the
remaining `camera_status` fields are pruned (`ir_mode`/`speaker_volume`) or moot, so the pinned
hardware bytes are harmless. **TURN/scoped-quality lock implemented:** while a relay viewer
candidate is online, `state.turn_online` locks the global quality tier against raises via
`quality.quality_change_allowed`.

**Single-camera contention:** libcamera allows one client. The snapshot loop now gates on active
RTSP clients (via `/proc/net/tcp` on :8888) and on the WebRTC flag, so local RTSP viewing no
longer races snapshot uploads.

**Note on ACKs:** only `camera_authentication` returns a meaningful ACK (`0` = success; `1`/`2`
are errors); the trigger-driven `status`/`features`/`protobuf_version` sends are not ACKed
(appears normal). ACKs therefore can't confirm field correctness — the `/c/info` HTTP channel is
what actually populates the UI. **[assumption]**

### Power-loss robustness (the Pi power-cycles with the printer, no clean shutdown)

Design to make an abrupt cut a non-event, layered:

1. **No continuous SD writes** — `main.py` logs stdout-only at INFO (dropped the
   `/var/log/prusa-cam/main.log` `FileHandler`); journald `Storage=volatile` (RAM). **[done]**
2. **Crash-safe rare writes** — `quality.py write_current()` fsyncs the temp file + directory
   before/after the atomic rename; `read_current()` already falls back to FHD on a corrupt file.
   **[done]**
3. **FS/boot hardening** — ext4 `fsck.repair=yes`, `noatime`, zram swap (already); `/boot/firmware`
   → `ro`. **[partial]**
4. **Read-only overlayfs root** — writes → tmpfs, discarded on reboot, so the SD can't be
   corrupted at runtime. Uses `overlayroot`+`initramfs-tools`+`auto_initramfs=1` on this minimal
   Debian. Deploy is overlay-aware via `pi-impersonator/deploy.sh`; 3.1.6 deployment and a full
   reboot were verified on 2026-09-18. **[done]**
5. Hardware UPS/GPIO clean-shutdown — optional, documented only.

⚠️ **2026-07-14 incident:** an abrupt-shutdown *test* via `sysrq b` (unsynced reset) corrupted
the rootfs and left the Pi unbootable (no initramfs → bad root mount halts boot before Wi-Fi);
recovery = SD fsck or reflash (see `.agent/pi-ops.md`). Lesson recorded there: **never
hard-reset this headless Pi to test** — verify overlay/robustness structurally instead. The
incident is itself the argument for step 4. Layers 1–2 are committed; steps 3–4 resume once the
Pi is recovered.

---

## Open / unresolved

- **Byte-perfect `CameraInfoMessage` map** — the 464-byte struct is reconstructed and offsets
  are computable, but most fields remain generically named. `0x0b8`-`0x0cc` (the "field 5.2"
  sub-block) was traced 2026-07-09 — see `protocol.md`'s `extended_status` section. Fast path
  for the rest: GhidrAssistMCP `get_code` on `0xa01dc` if the GUI/MCP session is running (it
  wasn't this session — see `next-steps.md` P.1 for the headless fallback that was used
  instead), then `struct field_xrefs`/`rename_field` per offset. Helpers in
  [`../research/`](../research/).
- **Direct registration** — whether `camera-service-api.prusa3d.com` exposes a registration
  endpoint (e.g. `POST /v1/cameras` with a bearer JWT) that would place our camera in the
  registry. Unexplored.
- **Real Buddy3D pairing QR format** — the official manual confirms the QR generated by Connect's
  "Add WiFi Camera" wizard carries Wi-Fi credentials plus a registration token (both scanned
  optically by the camera itself); exact encoding/format is still unconfirmed (firmware
  `AT+TOKEN=<token>` suggests at least the token portion is a plain string). Low priority — we
  already have a working token-acquisition path via the plain "Camera > Token" screen.

Corrected/superseded understanding (field-5 vs field-4, flat `/c/info`, `Niceboy` model, etc.)
is catalogued in [`dead-ends.md`](dead-ends.md) — consult it before trusting older notes.

---

## Recommended next steps (by likely payoff)

> **[superseded 2026-09-19/20]** Retained as history. These leads (MAC/OUI fingerprint
> retest, mitmproxy/SSL-unpinning, real-hardware comparison) were all chasing the
> "backend gate" that turned out not to exist; WebRTC works live. See the 2026-09-19 and
> 2026-09-20 sections above.

**2026-07-09 update:** the hardware-identity hypothesis (P.1 in `next-steps.md`) is now closed —
no unique, un-forgeable identifier was found on the `CameraInfoMessage` wire (see the "ruled
out" table above and `protocol.md`). One concrete, cheap correctness fix fell out of the
investigation: send the real firmware's model-name string (`"Buddy3D-C1"`) at the newly-traced
`extended_status` offset `0x0c4` instead of the impersonator's invented `"Pi Zero 2 W"`.
**Applied 2026-07-09** (`pi-impersonator/signaling.py`: `2: 'Pi Zero 2 W'` → `2: MODEL`),
deployed to the Pi, verified on the wire (`status` event's field 5 now shows `Buddy3D-C1`) and
end-to-end (`/c/info` 200, auth ACK `0`, stable connection). This alone did not (and wasn't
expected to) trigger a `webrtc` event.

**2026-07-09 update #2:** the `origin: LINK` token-registry hypothesis is downgraded — genuine
Buddy3D cameras also register as `origin: OTHER` (see Bottom line), so getting a LINK token was
very likely never going to help. The PrusaLink/Step 1 plan in `next-steps.md` is kept only as a
low-priority, separate curiosity, not the leading lead.

**2026-07-09 update #3:** live-tested and ruled out both `origin` and source-IP/network-reputation
as the gate (see "origin and network-reputation ruled out by live experiment" above).

**2026-07-09 update #4 (fingerprint caveat added 2026-09-18):** MAC/OUI test executed live and
mitmproxy turned out to be **blocked entirely**, not just untried:

- **MAC/OUI (P.2):** spoofed the Pi's `wlan0` to a guessed Realtek reference OUI (`00:E0:4C`,
  matching the RTL8188FU chip a community teardown found inside a real Buddy3D unit — no confirmed
  real-device MAC was found anywhere to test against directly), recomputed the fingerprint
  (`MD5(MAC)`, per P.3), registered a fresh token so it bound cleanly to the new identity, and
  redeployed. Identical ACK `5` + registry `404`. **2026-09-18 caveat:** firmware hashes the exact
  uppercase colon-separated MAC string, but the test's one-off MD5 preimage/casing was not saved.
  The recorded MAC was mixed-case, so this does not reproducibly establish firmware-consistent
  MAC/fingerprint pairing and must be repeated before ruling it out. No confirmed genuine MAC was
  available either. The run did confirm that the server binds fingerprint at a token's first use:
  reusing the existing token with a changed fingerprint returned `403`.
- **mitmproxy (Step 2):** phone proxy + cert trust both confirmed working (`sentry.prusa3d.com`
  and Firebase traffic decrypted cleanly), but across the whole session — including confirmed time
  spent on the Core One's camera view — **zero requests to `connect.prusa3d.com`,
  `camera-service-api.prusa3d.com`, or `camera-signaling.prusa3d.com`** were ever captured. That
  combination (working interception elsewhere, total silence for exactly the domains that matter)
  points to **certificate pinning** on the app's core API traffic. Seeing it would need
  jailbreak-level tooling (SSL Kill Switch / Frida), a much bigger escalation than attempted today.

**Net effect (revised 2026-09-18):** origin and source-IP reputation are ruled out. One precise
software-only test has reopened: a fresh token bound on first use to the exact firmware-derived
fingerprint for the same reported MAC. Mitmproxy remains blocked by pinning. After that retest,
what remains needs real Buddy3D hardware or significantly more invasive phone tooling:

1. **Repeat MAC/fingerprint consistency with a fresh token** using
   `md5("AA:BB:CC:DD:EE:FF").hexdigest()` over the exact uppercase reported MAC.
2. ~~**Probe `camera-service-api` for a direct registration path**~~ — **done 2026-07-09, negative.**
   No hidden endpoint, no informative error; see "camera-service-api surface probed directly"
   above. Also confirmed the genuine ESP32Cam is absent from this registry too.
3. ~~**Check the next firmware for a protocol/rollout change**~~ — **done 2026-09-17.** A direct
   3.1.5→3.1.6 binary diff found no signaling, WebRTC-gate, protobuf, feature, endpoint, or
   `/c/info` change. The app delta is hardware-version classification; see
   [`firmware-3.1.6.md`](firmware-3.1.6.md). This rules out a client-side 3.1.6 protocol fix, but
   cannot rule out a server-side staged rollout.
4. **SSL-unpinning on a jailbroken device**, if one becomes available — the only way left to see
   the real app's actual `connect.prusa3d.com`/`camera-service-api` traffic.
5. **Get real Buddy3D hardware** to compare directly (the ESP32Cam on the account doesn't count —
   it never implements WebRTC at all, see above).
6. ~~**If a real `webrtc` offer ever arrives**, verify `webrtc.py` end-to-end~~ — **done
   2026-09-19/20:** WebRTC is exercised live in the app and the browser.
