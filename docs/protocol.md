# Prusa Buddy3D Camera Protocol Specification

Firmware 3.1.5, Protocol Schema 4.4. Reversed from `lp_app` ARM binary via Ghidra.

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
  alphanumeric characters) and `origin`.
- The camera's role starts *after* this: it receives the token out-of-band (typed in or scanned
  via QR) and uses it for `fingerprint`+`token` auth (§2) and the `Token`/`Fingerprint` HTTP
  headers (§7–8). The camera has no way to self-register or change its own `origin`.

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
| `fingerprint` | MD5 hex of WiFi MAC address (32 chars) | `a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4` |
| `token` | Pairing token from Prusa Connect web UI (Camera > Token) | user-provided string |

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

- `1` (bare integer) — auth success (`camera_authentication` only)
- No ACK is sent for `status`, `protobuf_version`, or `features` events.

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

Also dispatched internally as an action name within `configuration`. Single-byte or integer payload: `1` = disabled, `2` = enabled. Confirmed from `FUN_000c1388` and `FUN_000af67c` (VMA `0xaf67c`). On `2`, starts the RTSP server on port 554 (`rtsp://<ip>/live`). On `1`, stops it.

```protobuf
message SetRtspServerMode {
    uint32 mode = 1;  // 1=disabled, 2=enabled
}
```

### `change_video_size`

Single raw byte payload (not a length-delimited protobuf field): quality enum. **Immediately** applies the new resolution to the live encoder via `change_vi_resolution`. Handler VMA `0x71D50`.

| Byte value | Quality | Resolution |
|---|---|---|
| `5` | HD | 1280×720 |
| `6` | FHD | 1920×1080 |
| `7` | SD | 640×360 |

### `save_video_size`

Same single-byte payload as `change_video_size`. **Only persists** the quality preference to `/data/xhr_config.ini` — does NOT change the live encoder. Handler VMA `0x6F954`. Sent after `change_video_size` to make the change survive reboot.

### `configuration`

Protobuf binary payload with string-named fields (looked up by field name string, not by numeric wire tag — confirmed from `FUN_0006bd7c` VMA `0x6BD7C`). Contains a subset of:

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

Commands sent by server. Decoded protobuf contains a trigger type field.

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

Bidirectional. Server sends to request the list; camera responds with the same event name containing the list payload. Entries come from `/mnt/sdcard/timelapse/timelapse_videos.csv`, each with a path and status char (`D`=done, `E`=error, `P`=pending). Registered in `FUN_000A4050` (VMA `0xA4050`).

### `webrtc`

Incoming SDP offers/ICE candidates from server. See Section 10 for full field table.

---

## 5. Connection Flow

### Camera side (our impersonator)

```
1. Connect WebSocket to wss://camera-signaling.prusa3d.com
   - Socket.IO CONNECT with auth={"token": camera_token}
   - Headers: Origin: https://connect.prusa3d.com
2. emit("camera_authentication", CameraAuthentication{fingerprint, token})
3. Wait for auth ACK `1`
4. emit("send_sio_info", {fingerprint, token})  — triggers status re-send internally
5. emit("status", CameraInfoMessage{...})  — field 10 = sio.get_sid() (session id)
6. emit("protobuf_version", ProtobufSchemaVersion{token, "4.4"})
7. emit("features", CameraSupportedFeatures{...})  — field 7 = MD5(features_json)
8. Start periodic snapshot upload (PUT /c/snapshot every 10s)
9. Listen for incoming trigger/config/webrtc events
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
    string firmware = 3;          // "3.1.5"
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

### WebRTCMessage — outbound answer

Outgoing SDP answers only need fields 1, 2, and 3:

```protobuf
message WebRTCMessage {
    string request_id = 1;  // echoed from inbound offer
    uint32 msg_type = 2;    // msg_type; for answer frames use the answer type value
    string sdp = 3;         // SDP answer body
}
```

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

```protobuf
message ClientTrigger {
    string field1 = 1;
    string field2 = 2;
    bytes field3 = 3;
    string field4 = 4;
    bytes field5 = 5;
    bytes field6 = 6;
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
real backend observed `200` instead (see `status.md`). Doesn't affect the impersonator — it only
logs the status code, it doesn't branch on it — but flagged here in case the discrepancy matters
for future debugging.

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
    "firmware": "3.1.5",
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

---

## 9. HTTP OTA Check (different headers)

```http
GET /api/niceboy/v1/camera HTTP/1.1
Host: connect-ota.prusa3d.com
User-Agent: Buddy3D Camera
X-Camera-Token: <token>
X-Camera-Fingerprint: <fingerprint>
X-Camera-FW-Version: 3.1.5
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

Both `webrtc_mode` (+0x13d) and `webrtc_status` (+0x13e) must be non-zero for an
offer to be processed. The server only sends `set_webrtc_mode` to cameras whose
`/c/info` response shows `origin` other than `OTHER`. An impersonator registered
with `origin: OTHER` never receives `set_webrtc_mode`, so these bytes remain zero
unless the impersonator sets them explicitly (e.g. by self-reporting them as enabled
in the `status` message field `5.11`).

---

## 11. Video Quality

| Protobuf Enum | Resolution | Config Value (`change_video_size` byte) | String |
|---------------|------------|-------------|--------|
| 1 | 640x480 | 7 | SD |
| 2 | 1280x720 | 5 | HD |
| 3 | 1920x1080 | 6 | FHD |

Config Value is the single byte carried by the `change_video_size`/`save_video_size` events
(5=HD, 6=FHD, 7=SD) — matches the impersonator's handler in `main.py`. (Earlier revisions of
this table listed 5=SD/6=HD/7=FHD, which was wrong.)

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
