# Prusa Buddy3D Camera Protocol Specification

Firmware through 3.1.6; Protocol Schema 4.4. Reversed from `lp_app`
ARM binaries via Ghidra. See [`firmware-3.1.6.md`](firmware-3.1.6.md) for the update delta.

REST endpoints below (registration, `/c/snapshot`, `/c/info`) are cross-checked against Prusa's
official Camera API OpenAPI spec (v0.22.0, saved at [`openapi.yaml`](openapi.yaml)) and the
`camera_registration`/`camera_communication` doc pages — see [`sources.md`](sources.md). That
spec does **not** cover Socket.IO or WebRTC (Sections 3–6, 10 below); those remain firmware-only
knowledge, reversed from `lp_app`.

---

## 0. Registration (app-side — the camera never calls this)

Before any of the camera-side flow below can run, a token has to exist. That happens through an
**app/browser** call, not the camera:

```http
POST /app/printers/{printer_uuid}/camera?origin=WEB|OTHER HTTP/1.1
Host: connect.prusa3d.com
Cookie: SESSID=<user session>
```

- `origin` defaults to `WEB` (Connect's separate browser-webcam feature: a phone/laptop's own
  camera becomes the feed via `getUserMedia()`/browser WebRTC — unrelated to the Buddy3D firmware
  protocol) and can be set to `OTHER` ("registration via api" per the spec's own wording). **Both
  genuine Buddy3D cameras and third-party API integrations (ESP32Cam etc.) register as `origin:
  OTHER`** — confirmed 2026-07-09 from the official pairing manual (Connect → Camera tab → "Add
  WiFi Camera" → camera scans a generated QR with its own lens); see `dead-ends.md`.
- **Confirmed from the spec: `WEB`/`OTHER` is the full enum for this endpoint — `LINK` is not a
  selectable value here.** `LINK` is minted by a separate, unrelated flow: PrusaLink's own
  "Link camera to Connect" toggle, for CSI/USB webcams wired directly into a Raspberry Pi running
  PrusaLink — a different product from the Buddy3D camera, with no WebRTC involved. See
  `status.md` and `next-steps.md` Step 1 (deprioritized).
- Response `201` returns a `camera_response` object containing the new `token` (exactly 20
  alphanumeric characters) and `origin`. Prusa's registration documentation explicitly defines
  this as a randomly generated combination of letters and numbers. The OpenAPI operation has no
  request body: the authenticated user/team (from the session cookie), `printer_uuid`, and
  optional `origin` are the complete registration inputs. No MAC, fingerprint, hardware version,
  serial number, model, or firmware version participates in token generation.
- The camera's role starts *after* this: it receives the token out-of-band (typed in or scanned
  via QR) and uses it for `fingerprint`+`token` auth (§2) and the `Token`/`Fingerprint` HTTP
  headers (§7–8). The camera has no way to self-register or change its own `origin`.

### Firmware token provenance (3.1.6)

The complete firmware trace confirms that the token is opaque input, not a device-derived value:

1. `FUN_0006fb94` parses the scanned QR JSON and invokes `FUN_00068a50` (`CheckQrCodeToken`).
2. `FUN_00068a50` reads the top-level `token` key. It rejects a missing token, the sentinel
   `"none"`, or a value longer than 20 bytes; it performs no derivation or cryptographic check.
   The separate serial-console command `AT+TOKEN=` requires exactly 20 bytes.
3. `FUN_00082eec` (`setHttpToken`) persists the received value as `http.token` in
   `/data/xhr_config.ini` and `/data/xhr_http_token.conf`. `FUN_000842d4` reloads that file at
   startup.
4. The same stored value is used unchanged in HTTP `Token` headers and the protobuf
   `camera_authentication`/`send_sio_info` messages.

There is therefore no algorithm for recreating a token from camera data. A valid token must be
minted by Connect and delivered to the camera. On first camera communication, Connect associates
the separately supplied fingerprint with that token; changing the fingerprint later produces
`403`, but that binding happens after token creation and is not encoded in the token.

---

## 1. Transport

### Socket.IO (signaling + control)

```
URL: wss://camera-signaling.prusa3d.com/socket.io/?EIO=4&transport=websocket
Protocol: Engine.IO 4, Socket.IO binary events
TLS: Required
Reconnect: Automatic with backoff
```

### HTTP (snapshots + info)

```
Base URL: https://connect.prusa3d.com (default, overridable)
TLS: Required
```

---

## 2. Identity

| Value | Description | Example |
|-------|-------------|---------|
| `fingerprint` | Lowercase MD5 hex of the uppercase, colon-separated Wi-Fi MAC string (32 chars) | `md5("AA:BB:CC:DD:EE:FF")` |
| `token` | Opaque, server-generated 20-character alphanumeric pairing token | supplied by Connect |

The 3.1.6 fingerprint path is distinct from token creation. `FUN_00096cd8` obtains `wlan0`'s MAC
with `SIOCGIFHWADDR` and formats it as `%02X:%02X:%02X:%02X:%02X:%02X`; if that fails it creates
a random 10-character seed. `FUN_00097a4c` MD5-hashes that seed and emits 16 bytes as lowercase
`%02x` hex for the wire fingerprint.

The impersonator's wire-fingerprint precedence is:

1. an explicit `[identity] fingerprint` in `config.ini` (returned verbatim) — this is the
   fingerprint the registration token is bound to, so it **wins**; using the MAC-derived value
   instead makes Connect reject the camera (`PUT /c/snapshot` → `400 {"detail":"Invalid
   fingerprint"}`; `PUT /c/info` → `403`). Verified live 2026-09-18: with the configured
   fingerprint, `/c/info` → `200` (`origin='OTHER', registered=True`) and snapshots → `200`.
2. otherwise the firmware-style MAC derivation from `wlan0` (normalized uppercase
   colon-separated MAC, lowercase MD5), and
3. otherwise a persisted fallback seed (`GAP-IDENTITY-01`).

The normalized MAC is reported in camera metadata only when it is actually read (it is empty when
the MAC is unreadable).

---

## 3. Socket.IO Events (Camera → Server)

All payloads are raw protobuf binary, sent via `socket.emit(event_name, binary_blob, ack_callback)`.

| Event Name | Message | Fields | Purpose |
|------------|---------|--------|---------|
| `camera_authentication` | CameraAuthentication | 2 | Login |
| `status` | CameraInfoMessage | 11 | Camera info/state |
| `protobuf_version` | ProtobufSchemaVersion | 4 | Declare protocol version |
| `features` | CameraSupportedFeatures | 6 | Declare capabilities |
| `client_trigger` | ClientTrigger | 6 | Status/error reports |
| `webrtc` | WebRTCMessage | 9 | SDP answer/candidate |
| `webrtc_connection_info` | WebRtcConnectionType | 6 | Connection type report |

### ACK Responses (from server)

- `0` (bare integer) — auth success (`camera_authentication` only). `1` = not authorized,
  `2` = error joining session, `3` = missing token, `4` = error decoding (`FUN_0009e53c`).
- Trigger-driven `status`/`protobuf_version`/`features` emits carry an ack callback; live the
  server returns `1001` for `status`/`features`, while an **unsolicited** `protobuf_version`
  returns ACK `1` plus the `error` event `CameraIsNotSessionMemberError`.

---

## 4. Socket.IO Events (Server → Camera)

### `set_webrtc_mode`

Single-byte protobuf payload: `1` = enable WebRTC service, `0` = disable. Confirmed from `FUN_000b82f4` (VMA `0xb82f4`). After receiving `1`, the camera starts the WebRTC service and the inbound offer gate (`+0x13d`/`+0x13e`) becomes passable.

```protobuf
message SetWebRtcMode {
    uint32 mode = 1;  // 1=enable, 0=disable
}
```

### `set_rtsp_server_mode`

Also dispatched internally as an action name within `configuration`. Single-byte or integer
payload: `1` = disabled, `2` = enabled. On `2`, the firmware starts its configured RTSP service;
on `1`, it stops it. Firmware 3.1.6 loads the port through a configuration getter and builds the
advertised URL from runtime state. The shipped default has not yet been pinned from a configuration
image or genuine status capture; older notes naming port 554 are not sufficient evidence.

```protobuf
message SetRtspServerMode {
    uint32 mode = 1;  // 1=disabled, 2=enabled
}
```

### `change_video_size`

Single raw byte payload (not a length-delimited protobuf field). The shared 3.1.6 handler
`FUN_00072f08` applies the corresponding live resolution and updates in-memory current state only
after success. A third callback argument controls whether it also persists the value.

| Byte value | Quality | Resolution |
|---|---|---|
| `5` | SD | 640×480 |
| `6` | HD | 1280×720 |
| `7` | FHD | 1920×1080 |

### `save_video_size`

Same raw-value domain as `change_video_size`. The recovered shared handler always performs the
live change and persists only when its third callback argument is nonzero. Which indirectly
registered Socket.IO event supplies flag `0` versus `1` remains to be recovered; do not infer that
wiring from the event names. See `GAP-QUALITY-02` in the implementation gap tracker.

### `configuration`

**Correction 2026-09-19 (live-verified):** the Socket.IO `configuration` event is a
**nested protobuf**, descriptor `0x3f73a4` (9 fields), decoded by `FUN_000a89e0` and
dispatched by *name* after decoding (`'video_quality'`, `'light_control'`,
`'motor_controll'`, `'set_snapshot_upload_interval'`). The name-keyed table below is the
**QR / manual-config** path (JSON via nlohmann, `FUN_0006fb94`), not the Socket.IO
message. The JSON handler rejected every live setting change as "not valid JSON".

Live-mapped SIO fields:

| Tag | Meaning | Values |
|---|---|---|
| `8.1` | video quality | `1`=SD, `2`=HD, `3`=FHD |
| `3.4` | `light_control` (IR) | `1`=auto, `2`=day, `3`=night |
| `3.11` / `3.12` | RTSP candidate | mapping pending (`{11:2,12:2}` = on observed) |
| `4` | two strings (`0x3f7418`) | empty in observed messages |
| `6` | token | echoed |

QR/manual-config (JSON) field table:

| Field name | Type | Values |
|---|---|---|
| `code` | string | Auth token; `"42"` and `"66"` are easter-egg guards that reject the message |
| `rtsp` | string | `"on"` / `"off"` — dispatches to `set_rtsp_server_mode` |
| `webrtc` | string | `"on"` / `"off"` / `"rtsp"` — dispatches to `set_webrtc_mode` |
| `video_quality` | string | `"sd"` / `"hd"` / `"fhd"` — schedules quality change |
| `start_fw_update` | string | Any non-empty value triggers OTA firmware update |
| `light_control` | string | `"auto"` / `"night"` / `"day"` — IR/day-night mode |
| `camera_name` | string | New camera display name |
| `snapshot_interval` | int32 | Snapshot upload interval in seconds (valid range 10–600) |

Note: `sd`, `hd`, `fhd` are **NOT** standalone Socket.IO event names — they are string values for the `video_quality` field within `configuration`.

### `trigger`

Commands sent by server. Decoded protobuf contains a trigger type field. The recovered 3.1.6
descriptor `0x3f6f14` (dispatcher `FUN_000a963c`) gives the exact field map; firmware performs
**only** the requested action(s), so an absent field produces no response:

| Tag | Type | Meaning |
|---|---|---|
| 1 | uvarint | Get status |
| 2 | uvarint | Get features |
| 3 | uvarint | Get snapshot |
| 4 | uvarint | Snapshot upload: `1`=enable, `2`=disable |
| 5 | uvarint | Timelapse: `1`=enable, `2`=disable |
| 8 | uvarint | Start firmware update |
| 9 | uvarint | Reboot device |
| 10 | uvarint | RTSP: `1`=start, `2`=stop |
| 11 | string | `request_id` / correlation |
| 12 | uvarint | Get protocol information → send `protobuf_version` |
| 13 | string | Second string; semantics unresolved — decode and log only |
| 14 | uvarint | Timelapse make video: `1`=make, `2`=unsupported |
| 15 | uvarint | Timelapse file list: `1`=get, `2`=unsupported |

A request field is acted on only for its documented value; other values are ignored and logged.
`status`, `features`, and `protobuf_version` correlate on tag 11 when present.

| Trigger | Internal Action |
|---------|----------------|
| Get features | Re-send features |
| Get status | Re-send status |
| Get snapshot | Capture and upload snapshot |
| Set snapshot enable/disable | Enable/disable snapshot upload loop |
| Timelapse enable/disable | Enable/disable timelapse |
| Start timelapse video | `timelapse_make_video` |
| Get timelapse file list | Triggers `timelapse_get_file_list` response |
| Start device reboot | `reboot_device` |
| Start FW update | `start_fw_update` |
| Start/Stop RTSP | `start_rtsp_server` / `stop_rtsp_server` |

### `timelapse_get_file_list`

Bidirectional. The server sends this event to request the list; the camera responds on the
**`file_list`** event (not the same name). The response body is composed by `FUN_000ad7ec`: it
enumerates the regular files in `/mnt/sdcard/timelapse/` whose name ends in **`.avi`**
(`FUN_000ac934`, log `"Timelapse files list: %s"`), looks each up in the hidden
**`.timelapse_videos.csv`** index, and appends one **`<name>;<status>`** line (terminated by
`\n`) per video. The index rows are `<name>:<status>` written by `FUN_000ac134`; the status is
`D`=done / `E`=error / `P`=pending (`FUN_000aee3c`), and a name absent from the index defaults
to `'U'` (0x55). With no `.avi` files the sender emits nothing
(`"No video files found on SD card"`). See the `TimelapseFileList` field table in Section 6.
**[confirmed]**

### `webrtc`

Incoming SDP offers/ICE candidates from server. See Section 10 for full field table.

---

## 5. Connection Flow

### Camera side (our impersonator)

```
1. Connect WebSocket to wss://camera-signaling.prusa3d.com
   - Socket.IO CONNECT with auth={"token": camera_token}
   - Headers: Origin: https://connect.prusa3d.com
2. emit("camera_authentication", CameraAuthentication{token, fingerprint})
3. Wait for auth ACK `0` (`0` = success; `1` = not authorized, `2` = error joining session)
4. Send **nothing** post-auth: firmware `FUN_000a05e4` only logs "Authentication successful"
   and resets the connection counters. The server drives the camera with `trigger` polls —
   tag 1 → `status`, tag 2 → `features`, tag 12 → `protobuf_version`. Do **not** emit
   `send_sio_info` (an internal firmware function name, not a Socket.IO event) and do not
   send `protobuf_version` unsolicited — the server answers that with
   `CameraIsNotSessionMemberError` and cycles the session.
5. Start periodic snapshot upload (PUT /c/snapshot every 10s)
6. Listen for incoming trigger/config/webrtc events
10. On "webrtc" offer: emit("webrtc", WebRTCMessage{request_id, "answer", sdp, ...})
```

### Viewer/client side (buddy3d-proxy / mobile app)

```
1. Connect WebSocket to wss://camera-signaling.prusa3d.com
   - Socket.IO CONNECT with auth={"token": camera_token}
   - Headers: Origin: https://connect.prusa3d.com, User-Agent: Chrome/...
2. emit("client_authentication", {token: camera_token, client_kind: "client", access_jwt})
   - access_jwt = OAuth2 PKCE JWT from account.prusa3d.com
   - ACK 0 = success, ACK 5 = rejected (camera not in camera-service registry)
3. emit("trigger", {field1: 1, token})  — subscribe to camera state
4. emit("trigger", {field2: 1, token})  — subscribe to features
5. GET https://camera-service-api.prusa3d.com/v1/camera-webrtc-config (TURN/STUN config)
6. emit("webrtc", WebRtcSignal{token, session_id, peer_id, msg_type:1, direction:2, ice_config})
   - This is the WebRTC kickoff; server relays it to the camera
7. Receive "webrtc" with msg_type:2 (SDP answer from camera)
8. Exchange ICE candidates (msg_type:4, bidirectional)
9. Receive H.264 video via WebRTC
```

### Server-side gate

**Confirmed (tested 2026-07-07):** the viewer's `client_authentication` gets ACK `5`
(rejected) for tokens with origin `OTHER` and `WEB`, and those tokens return 404 from
`GET camera-service-api.prusa3d.com/v1/cameras/<token>` — so viewers cannot connect and the
camera never receives any relayed events.

**Inferred (untested):** that this `camera-service-api` registry is the exact gate. **Revised
2026-07-09:** `origin: LINK` is very likely not the answer — the official pairing manual shows
genuine Buddy3D cameras register as `origin: OTHER` too (same as `WEB`/`OTHER` above), via
Connect's "Add WiFi Camera" QR wizard; `LINK` belongs to an unrelated PrusaLink RPi-webcam
product. What actually gates registry membership (a real-hardware allowlist vs. a staged feature
rollout) is open — see `status.md`'s Bottom line and `next-steps.md` for the current leads.

---

## 6. Protobuf Messages

All field numbers are **sequential starting from 1**. Wire format is standard protobuf.

### CameraAuthentication (2 fields)

```protobuf
message CameraAuthentication {
    string fingerprint = 1;  // MD5 hex of MAC
    string token = 2;        // pairing token
}
```

Wire example: `0a 20 <32_hex_chars_as_bytes> 12 <varint_len> <token_bytes>`

### ProtobufSchemaVersion (4 fields)

```protobuf
message ProtobufSchemaVersion {
    string token = 1;        // http.token, from FUN_00081c18
    string version = 2;      // "4.4" (literal)
    string request_id = 3;   // optional, only when responding to server request
    // field 4: not sent (null callback)
}
```

### CameraSupportedFeatures (7 fields, all strings, field numbers 2–7)

```protobuf
message CameraSupportedFeatures {
    // field 1: not sent
    string token = 2;             // http.token
    string firmware = 3;          // currently "3.1.6"
    string hardware = 4;          // "Pi Zero 2 W" / HW model string
    string protocol_version = 5;  // "4.4"
    string features = 6;          // bracket-wrapped JSON array: ["SocketCom",...]
    string protocol_version2 = 7; // "4.4" again — confirmed via DAT_000a8440 → 0x3f1f18
}
```

The inner features string value: `"SocketCom","UploadInterval","TimelapseEn","TimelapseInterval","TimelapseVideoMake","TimelapseFileList","VideoStream","RtspStream","GetSnapshot","IrMode","SpeakerVolume","WiFi","FwVer","HwVer","CameraName","MicroSd","FwUpdate","CameraReboot","McuTemp","VideoQuality","WebRtc","TurnVideoQualityChange","trigger_scheme","FanControl"`

Note: quotes are part of each name, separator is `,` with no spaces, and the firmware
wraps the whole string in literal `[` and `]` before protobuf encoding.

### CameraInfoMessage (11 fields)

```protobuf
message CameraInfoMessage {
    SubMessage field1 = 1;             // 32 bytes; descriptor confirmed, not populated in SendCameraInfoMessage decompile
    SubMessage timelapse_status = 2;   // 32 bytes; timelapse service interval/enable/name/state/temp-like values
    SubMessage camera_status = 3;      // 32 bytes; IR mode, upload interval/status, speaker volume
    SubMessage network_info = 4;       // 68 bytes; ssid, mac/bssid, ipv4, signal
    SubMessage extended_status = 5;    // 152 bytes; firmware, HW, camera name, RTSP, services, WebRTC
    SubMessage field6 = 6;             // 8 bytes; descriptor confirmed, not populated in SendCameraInfoMessage decompile
    uint32 field7 = 7;                 // descriptor confirmed, not populated in SendCameraInfoMessage decompile
    string token = 8;                  // http.token
    SubMessage system_info = 9;        // 72 bytes; temp, uptime, load, RAM/process telemetry
    string request_id = 10;            // conditional; only set when param_1[0x12] != 0
    SubMessage video_quality = 11;     // wrapper with field 1 enum: 1=SD, 2=HD, 3=FHD
}
```

`network_info` confirmed layout so far:

```protobuf
message NetworkInfo {
    message NetworkBlock {
        string wifi_ssid = 1;
        string wifi_mac_or_bssid = 2;
        string wifi_ipv4 = 3;
        uint32 signal_quality = 5;
    }
    NetworkBlock current = 1;
}
```

Confirmed always-present optional fields in `SendCameraInfoMessage`:

| Flag offset | Wire field | Meaning |
|---|---:|---|
| `0x024` | `2` | `timelapse_status` |
| `0x048` | `3` | `camera_status` |
| `0x06c` | `4` | `network_info` |
| `0x070` | `4.1` | current network block |
| `0x0b4` | `5` | `extended_status` |
| `0x0b8`-`0x0cc` | `5.1`-`5.3` (offset order, exact tags unconfirmed) | 3× `(mode=1, value)` pairs — see below |
| `0x0d0` | `5.4` | video/timelapse mode/storage block |
| `0x0f0` | `5.6` | RTSP mode/status/url block |
| `0x104` | `5.7` | log-level/status block |
| `0x118` | `5.9` | service endpoint/status string block |
| `0x134` | `5.10` | timezone/status block |
| `0x144` | `5.11` | WebRTC mode/status block |
| `0x170` | `9` | system telemetry block |
| `0x1c8` | `11` | video quality wrapper |

Earlier drafts incorrectly placed `network_info` at field 5, treated field 8 as camera
name, field 10 as firmware, and field 9 as `available_resolutions`. Descriptor +
accessor tracing disproves those guesses: field 4 is network info, field 5 is extended
status, field 8 is `http.token`, field 9 is system telemetry, and field 10 is a
conditional request-id-like string.

**`extended_status` offsets `0x0b8`-`0x0cc` (the block immediately before `5.4`) — traced
2026-07-09, headless Ghidra (`FUN_000a01dc` decompile + xref chase, no live GUI session
available this run):**

Three `(mode, value)` pairs, each `mode` = the shared constant `DAT_000a0ce4` (same value
written three times):

| Offset | Source | Value |
|---|---|---|
| `0x0b8` | constant | `DAT_000a0ce4` |
| `0x0bc` | `FUN_0003726c()` | **compile-time literal string** — not identity/hardware-derived at all |
| `0x0c0` | constant | `DAT_000a0ce4` (same as `0x0b8`) |
| `0x0c4` | `FUN_0007272c(FUN_00071ce0())` → reads `+0x20` of the shared device-info singleton | **model-name string** (one of `"Buddy3D-C1"`/`"Buddy3D-POE"`/`"Buddy3D."` — see §7.1 in `journal/findings.md`) |
| `0x0c8` | constant | `DAT_000a0ce4` (same as `0x0b8`) |
| `0x0cc` | `FUN_0009e4a4(FUN_0005b2f8(FUN_0005a8d4()))` → reads `+0x2c` of a *different* singleton | unidentified string; not traced further (low priority — see `status.md`) |

The `0x0c4` model-name string is populated once, lazily, the first time `FUN_00071ce0()`'s
singleton is touched (guard function `FUN_0007460c` → `FUN_00073638` → `FUN_00072534`, which
`fopen`s the real `/sys/class/spi_master/spi2/spi2.0/version` SPI chip, byte-swaps the u32 it
reads, then `FUN_000723f0` walks a fixed range table mapping that numeric HW-version code to
one of a small, fixed set of model-name strings (`checkHwVersion`, already referenced in the
model-selection note above this table). **This confirms the field carries a hardware-derived
value, but it is a small-cardinality *model/variant* string shared by every unit of that
hardware revision — not a unique per-device factory serial.** No distinct factory-serial / OTP
getter or string (`"serial"`, `"Serial"`, `"SERIAL"`, `"otp"`, `"OTP"`, `"SN:"`, `"factory"`)
was found anywhere in `.rodata` this session — an exhaustive substring search came back empty.
See the "hardware-identity hypothesis" verdict in `status.md` and `next-steps.md` P.1 for the
full reasoning and implication (the value is guessable/reproducible without real hardware, so
this alone doesn't explain the registration gate).

The `FUN_00071ce0()` singleton accessor has 86 call sites across the binary — including
`do_update_camera_attr` (`/c/info` JSON builder), the snapshot-upload function, and the
features-list builder — confirming it's the shared camera-identity/device-info object used
throughout, not something CameraInfoMessage-specific.

**`extended_status.4` storage block (descriptor `0x3f72b0`) — traced 2026-09-19 from
firmware 3.1.6 [confirmed]:**

| Tag | Wire type | Semantic | Firmware source |
|---|---|---|---|
| 1 | uvarint | SD mounted state: **1 = mounted/present, 2 = not mounted** (translation `0->2`, `1->1`, else `0`) | `FUN_000744ac` (SD-monitor singleton `FUN_00072e98`) |
| 2 | uvarint | total SD space in MB, `(f_bsize * f_blocks) >> 20` | `FUN_000745e0(obj, 1, 0)`, `statvfs64("/mnt/sdcard")` |
| 3 | uvarint | free SD space in MB, `(f_bsize * f_bfree) >> 20` | `FUN_000745e0(obj, 0)` |
| 4 | uvarint | used SD space in MB, `(f_bsize * (f_blocks - f_bfree)) >> 20` | `FUN_000745e0(obj, 2, 0)` |
| 5 | string | SD mount-mode string: `"UNKNOWN"` (not mounted), `"RW"` (writable), or `"RO"` (read-only) | `FUN_00073914` = SD-monitor `+0x50`, set by `FUN_000744ac`/`FUN_00074218` |

**Corrected mapping:** the earlier note that placed the TimelapseService getters
`FUN_000abcb0`/`FUN_000abaf4` in this block was wrong — they belong to top-level
`timelapse_status` (field 2). Likewise `MODEL` was previously (mis)encoded on tag 5;
tag 5 is the mount-mode string. The Pi impersonator maps this block to
`timelapse.storage_status` over the emulated SD at `/mnt/sdcard`
**[assumption]** — a Pi policy, not firmware: mounted `1` when the directory is
readable (`access(R_OK)`, matching `FUN_00071bc0`), else absent `2` with zero
space and `"UNKNOWN"`; the `"RW"`/`"RO"` mode is the separate write check.

### WebRTCMessage — inbound offer field list

Decoded from `parseWebRtcMessage` (VMA `0xa36a4`). The log format string at
`0x3f51d0` names every field:

| Field | Semantic | Wire type | Notes |
|-------|----------|-----------|-------|
| 1 | `request_id` | string | Echoed back in the SDP answer |
| 2 | `msg_type` | varint | **3 = offer** (only value that triggers processing; others → error response) |
| 3 | `client_id` | string | Identifies the requesting viewer; stored at `*param_1 + 0x44` |
| 4 | `sdp` | string | SDP offer body; max 3000 bytes |
| 5 | `transport_policy` | varint | ICE transport policy (all / relay) |
| 6 | `ttl` | varint | Session TTL |
| 7 | `video_cfg` | varint | Requested video config enum |
| 8 | `plan` | varint | SDP plan (unified-plan / plan-b) |
| 9 | `quality` | string | `"sd"` / `"hd"` / `"fhd"` |
| 10 | `fps` | varint | Requested FPS |
| 11 | `ttl` (scope) | varint | Session scope |
| 12 | `ice_config` | submessage | TURN/STUN server list |

Processing path: `parseWebRtcMessage` → enable gate check (see §10) →
`FUN_000b7a9c` (get WebRTC singleton) → `FUN_000b87b4` (create peer connection,
enqueue to `singleton + 0x140` work queue). SDP answer emitted back on the
`webrtc` Socket.IO event.

**Correction (2026-09-18, direct 3.1.6 descriptor dump):** the table above is the *log
format string* (`0x3f61c6`: `Client type: %d, Msg type: %d, ID len: %d, SDP len: %d,
Transport policy: %d, TTL: %d, VideoCfg: %d, Plan: %d, Quality: %s, FPS: %d, TTL: %d,
Scope: %d`) rendered as if it were the wire schema. The actual inbound message decoded by
the SIO `webrtc` handler (lambda `0xa4a78`, `pb_decode` descriptor `0x3f7680`) has **9
fields**, several of them nested, not 12 flat fields:

| Tag | Type | Notes |
|---|---|---|
| 1 | string | |
| 2 | string | |
| 3 | string | |
| 4 | submessage | 2 string fields |
| 5 | uvarint | |
| 6 | uvarint | |
| 7 | uvarint | |
| 8 | submessage | bytes + 2 uvarint |
| 9 | submessage | 5 uvarint |

The 12 named values in the log therefore come from the nested submessages (e.g. `Quality`
is rendered through `FUN_000a11f4`, the protobuf-quality-to-string helper). The exact
tag-to-log-value mapping is **not** yet recovered, so `GAP-WEBRTC-05` must not be
implemented from the flat 12-field table. Do not guess the nested tags.

### WebRTCMessage — outbound answer

Outgoing SDP answers only need fields 1, 2, and 3:

```protobuf
message WebRTCMessage {
    string request_id = 1;  // echoed from inbound offer
    uint32 msg_type = 2;    // 2=answer; 4=candidate
    string sdp = 3;         // SDP answer body
}
```

The camera-side enum is `1=request`, `2=answer`, `3=offer`, `4=candidate`. Inbound offers use
field 4 for SDP and field 3 for `client_id`; outbound answers/candidates use field 3 for their
SDP/candidate payload. The signaling service translates this flat camera envelope to/from the
different nested `WebRtcSignal` envelope used by viewers.

### WebRtcConnectionType (6 fields)

```protobuf
message WebRtcConnectionType {
    string client_id = 1;
    string local_type = 2;   // "HOST" | "SERVER_REFLEXIVE" | "RELAYED" | etc
    string remote_type = 3;
    bytes field4 = 4;
    bytes field5 = 5;
    bytes field6 = 6;
}
```

### ClientTrigger (6 fields)

Recovered 3.1.6 descriptor `0x3f6f58` (sender `FUN_000a2754`). Types are now known; the
semantic names are not (the sender populates only a subset), so this remains
`descriptor required` for meaning:

```protobuf
message ClientTrigger {
    string field1 = 1;
    string field2 = 2;
    uint32 field3 = 3;
    string field4 = 4;
    uint32 field5 = 5;
    uint32 field6 = 6;
}
```

### TimelapseFileList (4 fields)

Event **`file_list`**. Recovered 3.1.6 descriptor `0x3f701c` (sender `FUN_000a1fa8`). The camera
responds only when at least one assembled `.avi` exists in `/mnt/sdcard/timelapse/`; an empty list
sends **no** message. **[confirmed]**

| Field | Type | Meaning |
|---|---|---|
| 1 | string | One fragment, exactly `"<page>;<total>\n<chunk>"` (page is 1-based, `total` = fragment count). `<chunk>` is one or more `<name>;<status>\n` entries: the `.avi` basenames (`FUN_000ac934`) each paired with its `.timelapse_videos.csv` status (`D`/`E`/`P`, default `U`) by `FUN_000ad7ec`. **[confirmed]** |
| 2 | string | HTTP token (`FUN_00082dd0` reads `/data/xhr_config.ini` key `http.token`). **[confirmed]** |
| 3 | string | Request correlation id; set only when present. **[confirmed]** |
| 4 | string | Never populated by this sender (its callback funcs slot stays NULL). Do not send. **[confirmed]** |

Fragmentation (`FUN_000a1fa8`, confirmed): if the whole response is **< 0x401 bytes** it is one
fragment; otherwise `total = (size >> 10) + 1`, the chunk size is **1024 bytes**, and there is a
**50 ms** pause between fragments (`FUN_00065d80`).

```protobuf
message TimelapseFileList {
    string field1 = 1;   // "<page>;<total>\n<chunk>"
    string field2 = 2;   // HTTP token
    string field3 = 3;   // request_id (optional)
    // field 4: never set by FUN_000a1fa8
}
```

---

## 7. HTTP Snapshot Upload

```http
PUT /c/snapshot HTTP/1.1
Host: connect.prusa3d.com
User-Agent: Buddy3D Camera
Token: <token>
Fingerprint: <fingerprint>
Content-Type: image/jpg
Expect: 100-continue

<JPEG binary data>
```

Headers are `Token` and `Fingerprint` (short names, no prefix).

Default interval: 10 seconds.

Note: the official OpenAPI spec documents success as `204 No Content`; live testing against the
real backend observed `200` instead (see `status.md`). Firmware `FUN_0005c568` accepts both
`"200"` and `"204"` and has a dedicated `"403"` branch that logs `Upload image BLOCKED by server!`
(`lp_app.strings:8304`); any other response is logged as a failed status code. The impersonator
classifies the same way (`pi-impersonator/http_result.py`).

---

## 8. HTTP Camera Info Upload

**CORRECTED 2026-07-06** — the schema below was verified by decompiling
`do_update_camera_attr` (VMA `0x00061bbc`) and confirmed live against the real server
(first attempt with this shape returned `200`, prior top-level-flat attempts all got
`400 Bad Request "Request body validation error"`). See `status.md` for the full story.

```http
PUT /c/info HTTP/1.1
Host: connect.prusa3d.com
User-Agent: Buddy3D Camera
Token: <token>
Fingerprint: <fingerprint>
Content-Type: application/json

<JSON body>
```

### JSON Body

Almost everything lives under `"config"`. `features` and `capabilities` are genuine
JSON **arrays**, not CSV strings (that assumption was the root cause of every earlier
400 error). `available_resolutions` lives under `"options"`, not top-level.

```json
{
  "config": {
    "path": "private",
    "name": "Buddy3D Camera",
    "driver": "private",
    "model": "Buddy3D-C1",
    "firmware": "3.1.6",
    "manufacturer": "Niceboy",
    "trigger_scheme": "THIRTY_SEC",
    "resolution": {
      "width": 1920,
      "height": 1080
    },
    "network_info": {
      "wifi_mac": "AA:BB:CC:DD:EE:FF",
      "wifi_ipv4": "192.168.1.100",
      "wifi_ssid": "MyNetwork"
    }
  },
  "options": {
    "available_resolutions": [
      {"width": 1920, "height": 1080}
    ]
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
- Only one entry in `available_resolutions` in the real firmware's build logic (just
  the current resolution), not three — untested whether more are accepted.
- `capabilities` is specifically `["trigger_scheme"]` per the decompile, not empty. The
  OpenAPI spec's `camera_capabilities` enum also allows `imaging`, `resolution`, `focus` —
  firmware 3.1.5 never sends them (possibly reserved for other camera hardware/future use).
- The decompile shows `"manufacturer": "Niceboy"` and `model` comes from the hardware
  variant selector (`ReadHwVersionFromCamera` / `checkHwVersion`). Most serial ranges
  map to `"Buddy3D-C1"`; no-version/default fallback maps to `"Buddy3D."`.
- Server response echoes back extra server-assigned fields not in the request:
  `id`, `rotation`, `sort_order`, `origin` (seen as `"OTHER"` for this impersonator —
  real hardware may report something else here), `registered`, `team_id`,
  `printer_uuid`. **`rotation` is not in the OpenAPI `camera_response` schema at all** —
  either an undocumented field the live backend added since the spec was last updated, or
  the spec (v0.22.0) is simply incomplete here.
- **`features` (the top-level array in this request body) has no corresponding field in the
  OpenAPI `camera_request` schema either** — that schema only defines `config`, `options`,
  `capabilities`. We send it anyway because it was traced directly out of firmware
  (`do_update_camera_attr`, VMA `0x00061bbc`) and a live request with it returns `200`; the
  spec is the less authoritative source here (it explicitly doesn't cover newer additions like
  WebRTC, so it's plausible `features` postdates it too).

### HTTP result classes and `/c/info` refresh

Firmware distinguishes only the codes it branches on. `FUN_0005c568` (snapshot) accepts `"200"`/
`"204"`, treats `"403"` as blocked, and logs every other code as a failure; `FUN_00062d74`
(`/c/info`) clears its dirty flag only for `"200"`. The recovered one-second service loop
(`FUN_00063bfc`) re-sends `/c/info` on a 10-second countdown while its dirty flag is set. The
impersonator maps these to `success`/`redirect`/`blocked`(403)/`client_error`/`server_error` plus
`timeout`/`connection_error`, retries only transient classes, and does not auto-follow redirects
(firmware shows no redirect handling).

---

## 9. HTTP OTA Check (different headers)

```http
GET /api/niceboy/v1/camera HTTP/1.1
Host: connect-ota.prusa3d.com
User-Agent: Buddy3D Camera
X-Camera-Token: <token>
X-Camera-Fingerprint: <fingerprint>
X-Camera-FW-Version: 3.1.6
```

Note: OTA endpoint uses `X-Camera-*` prefixed headers (different from snapshot/info).

---

## 10. WebRTC

### Stack
- ICE: libjuice
- DTLS: OpenSSL
- SRTP: via libdatachannel
- Trickle ICE (default)

### Default STUN Servers
```
stun:stun.l.google.com:19302
stun:stun1.l.google.com:3478
stun:stun2.l.google.com:5349
```

TURN credentials are provided by the server in the incoming WebRTC protobuf message (fields 4-9).

### SDP Parameters
```
m=video 0 RTP/AVP <pt>
a=rtpmap:<pt> H264/90000
a=fmtp:<pt> packetization-mode=1;sprop-parameter-sets=<sps>,<pps>
```

Profile: Constrained Baseline (`42e01f`), Level 3.1, packetization-mode=1.

### Connection Type Values
- `HOST` — direct LAN
- `SERVER_REFLEXIVE` — via STUN
- `PEER_REFLEXIVE`
- `RELAYED` — via TURN

### Singleton Byte Map (WebRTC state)

| Offset | Name | Description |
|--------|------|-------------|
| `+0x13c` | ready | Set to 1 during init by `FUN_000b3ce8` |
| `+0x13d` | `webrtc_mode` | Written by `SetWebRtcMode`; read by `GetWebRtcMode` (`0xb3d40`) |
| `+0x13e` | `webrtc_status` | Set when WebRTC service starts or stops |

### Enable Gate

`FUN_000b87b4` (VMA `0xb87b4`) processes inbound WebRTC offers. Its first check:

```c
if ((*(char *)(singleton + 0x13d) == '\0') &&   // webrtc_mode == 0
    (*(char *)(singleton + 0x13e) == '\0')) {    // webrtc_status == 0
    log("WebRTC server is disabled in configuration");
    return 0;  // offer silently dropped
}
```

The 3.1.6 gate uses configured `webrtc_mode` (+0x13d) and runtime `webrtc_status` (+0x13e): when
both are zero, an inbound offer is rejected before peer work is queued. `set_webrtc_mode(1)`
persists enabled mode and starts the service; `set_webrtc_mode(0)` persists disabled mode and stops
it. Mode and runtime state must be reported truthfully rather than bypassed by hardcoding status.

Older revisions attributed delivery of `set_webrtc_mode` to the camera registration `origin`.
Controlled `WEB` versus `OTHER` tests disproved `origin` as the registry/viewer-auth gate; do not
use it as an implementation condition. The currently observed block happens earlier: the test
camera is absent from the camera-service registry and viewer authentication returns ACK `5`.

---

## 11. Video Quality

| Protobuf Enum | Resolution | Config Value (`change_video_size` byte) | String |
|---------------|------------|-------------|--------|
| 1 | 640x480 | 5 | SD |
| 2 | 1280x720 | 6 | HD |
| 3 | 1920x1080 | 7 | FHD |

Config Value is the single byte carried by the `change_video_size`/`save_video_size` events
(`5=SD`, `6=HD`, `7=FHD`). This is confirmed independently by the protobuf-to-raw converter,
configuration dispatcher, direct handler, and dimension selector in firmware 3.1.6. The current
impersonator handler still uses the obsolete reversed mapping; track its correction under
`GAP-QUALITY-01`.

---

## 12. RTSP (Local)

```
URL: rtsp://<camera-ip>/live
Codec: H.264
Port: dynamic
```

---

## 13. Cloud Endpoints

| Service | Hostname |
|---------|----------|
| Signaling (Socket.IO) | `camera-signaling.prusa3d.com` |
| Snapshots + Info | `connect.prusa3d.com` |
| OTA Updates | `connect-ota.prusa3d.com` |
| Timezone | `timezone.prusa3d.com` |
| NTP | `prusa3d.pool.ntp.org` |

## WebRTC camera-side protocol (recovered 2026-09-19)

Recovered from the 3.1.6 binary (`FUN_000a3e90`, `FUN_000bf180`, `FUN_000b996c`,
`FUN_000b75e0`, `FUN_000bc0ec`) and confirmed against live Connect traffic.

### Flow
1. The viewer presses play. Connect sends the camera an **ICE config** `webrtc`
   message (type `1`).
2. The camera builds a peer connection from the ICE config and sends an
   **offer** `webrtc` message (type `3`).
3. The viewer sends an **answer** `webrtc` message (type `2`).
4. Both sides **trickle ICE candidates** (`webrtc` messages of type `4`).

The camera is the **offerer** (`"expected HaveLocalOffer"`,
`"Sending initial WebRTC %s (Trickle ICE)"`); `FUN_000bf180` strips
`a=candidate:` / `a=end-of-candidates` from the local SDP (trickle ICE).

### `webrtc` message (9-field descriptor `0x3f7680`)

| tag | type | offer | answer | candidate | ICE config |
|---|---|---|---|---|---|
| 1 | string | token | token | token | token |
| 2 | string | request_id | request_id | **mid** | client_id |
| 3 | string | fingerprint | fingerprint | session | session_id |
| 4 | submsg | `{1: SDP}` | `{1: SDP}` | `{1: candidate}` | — |
| 5 | uvarint | 3 (offer) | 2 (answer) | 4 (candidate) | 1 (request) |
| 7 | uvarint | 1 | 2 | 2 | 2 |
| 8 | submsg | — | — | — | ICE config |
| 9 | submsg | — | — | — | 5 uvarints |

### ICE config (tag8)
`tag8 = {1: <blob>}`; the blob is a repeated `0x0a <len>` sequence of either a
plain ICE server `{1: id, 2: host, 3: port, 4: type}` or a TURN block
`{1: <servers>, 2: username, 3: credential, ...}`. `type`: 1 = STUN, 2 = TURN,
3 = TURNS. Live values: 9 × `stun*.l.google.com`, `coturn.prusa3d.com:3478`
(types 1/2/3), TURN username `1789811200:43202` (time-limited) and base64
credential. `FUN_000bc0ec` logs `"Adding TURN: %s:%d User: %s (Type: %d)"`.

### H264
Codec is H264 (`H264CameraSource`). A reference offer generated with
**libdatachannel** (the firmware's WebRTC library) uses payload 96,
`a=sendonly`, `a=fmtp:96 profile-level-id=42e01f;packetization-mode=1;
level-asymmetry-allowed=1`, `a=mid` first in the m-section, plus
`a=msid-semantic:WMS *` and `a=group:LS`.

**The Connect answerer validates the stream's SPS** (live-verified 2026-09-19):
it rejects the camera's v4l2 SPS (`428029`, baseline level 4.1) with
`m=video 0`, and accepts only the firmware's `42e01f` class (constrained
baseline level 3.1). Transcoding is too heavy for the Pi, so `stream_mux`
serves an SPS-patched copy on **port 8889** (patch `profile_idc`/
`constraint_flags`/`level_idc` to `42 e0 1f`, matched by NAL type — rpicam-vid
emits the SPS header as `0x27`); the WebRTC branch reads 8889 while 8888
(RTSP/snapshots) is unchanged. After the patch the answer is `m=video 9 …` and
the stream plays.

### Live verification findings
- The signaling server ACKs `camera_authentication` then closes unless the auth
  is sent as `field1 = token, field2 = fingerprint` and the ACK is `0`
  (`FUN_000a05e4`/`FUN_0009e53c`).
- The viewer trickles candidates as `a=candidate:...` (SDP attribute form) with
  `tag2 = mid`; the impersonator must strip the `a=` prefix before
  `add-ice-candidate`.
- The Connect answerer is itself a libdatachannel endpoint (`a=setup:active`,
  `a=ice-options:trickle renomination`, `a=recvonly`).
