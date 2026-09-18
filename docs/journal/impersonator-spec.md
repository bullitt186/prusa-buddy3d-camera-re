# Prusa Buddy3D Camera — Linux Impersonator Specification

## Goal

A Linux application that impersonates a Prusa Buddy3D camera, connecting to Prusa Connect cloud services and streaming video from any V4L2 camera source. It must be indistinguishable from a real Buddy3D camera from the server's perspective.

---

## 1. Identity Configuration

The impersonator needs these values (from a real paired camera or fresh pairing):

```ini
# config.ini
token = <pairing_token>           # From QR pairing or Prusa Connect UI
fingerprint = <md5_hex>           # lowercase MD5 of uppercase colon-separated Wi-Fi MAC
firmware_version = 3.1.5          # Must match known firmware
model = Niceboy
manufacturer = Niceboy            # Inferred, confirm via traffic capture
camera_name = My Camera           # User-assignable
```

### Fingerprint Generation
```
fingerprint = MD5(wifi_mac_address_string)
```
If no real MAC, generate a random 32-char hex string and persist it.

---

## 2. Startup Sequence

```
1. Load config (token, fingerprint, camera_name, video settings)
2. Initialize video capture (V4L2 / GStreamer / FFmpeg)
3. Connect Socket.IO to wss://camera-signaling.prusa3d.com/socket.io/?EIO=4&transport=websocket
4. emit("camera_authentication", protobuf{fingerprint, token}) -> wait ACK "OK"
5. emit("status", protobuf{camera_info_11_fields})
6. emit("protobuf_version", protobuf{"4.4", ...})
7. emit("features", protobuf{feature_list_6_strings})
8. Start snapshot upload loop (PUT /c/snapshot every 10s)
9. Listen for "webrtc" events (offers) -> respond with answers
10. Listen for "client_trigger" events (config changes, commands)
```

---

## 3. Socket.IO Connection

### 3.1 Connect

```
URL: wss://camera-signaling.prusa3d.com/socket.io/?EIO=4&transport=websocket
TLS: Required (use system CA bundle)
Reconnect: Auto, with exponential backoff
```

### 3.2 Authenticate

Emit event: **`camera_authentication`**  
Payload: Raw protobuf binary (2 string fields, sequential tags):
```protobuf
field 1 (tag 0x0a): string fingerprint  // MD5 hex of MAC
field 2 (tag 0x12): string token        // from Prusa Connect UI
```

Wire bytes example: `0a 20 <32_hex_chars> 12 <len> <token_bytes>`

Wait for ACK. Expected: `"ACK: OK"`

### 3.3 Send Camera Info

Emit event: **`status`** (NOT "send_sio_info" — that was an internal function name)  
Payload: Protobuf binary (11 fields, complex submessages)

Fields:
```json
{
  "model": "Niceboy",
  "manufacturer": "...",
  "firmware": "3.1.5",
  "trigger_scheme": "THIRTY_SEC",
  "network_info": {
    "wifi_mac": "<mac>",
    "wifi_ipv4": "<ip>",
    "wifi_ssid": "<ssid>"
  },
  "available_resolutions": [
    {"width": 1920, "height": 1080},
    {"width": 1280, "height": 720},
    {"width": 640, "height": 480}
  ]
}
```

### 3.4 Send Protobuf Schema Version

Emit event: **`protobuf_version`**  
Payload: Protobuf (4 string fields). Field 1 = **"4.4"** (protocol schema version, NOT firmware "3.1.5")

### 3.5 Send Supported Features

Emit event: **`features`** (NOT "client_trigger")  
Payload: Protobuf (6 string fields) listing supported capabilities. Also includes "4.4" version.

**Minimum features to advertise for streaming:**
- `"VideoStream"`
- `"WebRtc"`
- `"VideoQuality"`
- `"GetSnapshot"`
- `"RtspStream"`
- `"FwVer"`
- `"HwVer"`
- `"CameraName"`
- `"SocketCom"`

**Full feature set (for complete impersonation):**
- All 25 features listed in findings.md section 10.3

---

## 4. HTTP Snapshot Upload

### 4.1 Request

```http
PUT /c/snapshot HTTP/1.1
Host: <image-server>
User-Agent: Buddy3D Camera
Token: <token>
Fingerprint: <fingerprint>
Content-Type: image/jpg
Expect: 100-continue

<JPEG binary>
```

NOTE: Headers are just `Token` and `Fingerprint` (NOT `X-Camera-Token`). The `X-Camera-*` prefix is only used for the OTA endpoint.

### 4.2 Image Server URL

Default: `connect-ota.prusa3d.com` (HTTPS)  
Override: configurable (equivalent of `/mnt/sdcard/imgsrv`)

### 4.3 Upload Loop

- Default interval: **10 seconds** (config `snapshot_upload_interval=10000` ms)
- `THIRTY_SEC` is the `trigger_scheme` name reported to server, but actual interval is configurable
- Configurable via server configuration event (value in milliseconds)
- Capture JPEG from video source
- Upload via HTTPS PUT
- Handle `"Upload image BLOCKED by server!"` gracefully (back off)

### 4.4 Camera Info Upload

```http
PUT /c/info HTTP/1.1
Host: <image-server>
User-Agent: Buddy3D Camera
Token: <token>
Fingerprint: <fingerprint>
Content-Type: application/json

{
  "model": "Niceboy",
  "firmware": "3.1.5",
  "manufacturer": "...",
  "trigger_scheme": "THIRTY_SEC",
  "network_info": {"wifi_mac": "...", "wifi_ipv4": "...", "wifi_ssid": "..."},
  "available_resolutions": [
    {"width": 1920, "height": 1080},
    {"width": 1280, "height": 720},
    {"width": 640, "height": 480}
  ]
}
```

---

## 5. WebRTC Streaming

### 5.1 Receiving Offers

Server sends binary WebRTC messages via Socket.IO. Message format:

```
[client_type: u8] [msg_type: u8] [id_len: ?] [id: string] 
[sdp_len: ?] [sdp: string] [transport_policy: u8] [ttl: u32]
[video_cfg: u8] [plan: u8] [quality: string] [fps: u8] [ttl2: u32] [scope: u8]
```

msg_type values:
- `offer` — server/client is offering to receive video
- `answer` — response to our offer (unlikely in camera role)
- `candidate` — ICE candidate trickle

### 5.2 Responding

Camera typically receives an `offer`, creates a `PeerConnection`, sets remote description, creates an `answer`, and sends it back.

Send via: emit to server (exact event name TBD — likely same Socket.IO channel)  
Format: `"Starting to send WebRTC message. RequestID: %s, Type: %s, SDP: %s"`

### 5.3 Media

**Video track:**
- Codec: H.264
- Profile: Constrained Baseline (42e01f), Level 3.1
- Packetization mode: 1
- Resolution: FHD (1920x1080) default, HD/SD if scoped
- RTP payload type: dynamic

**SDP offer template:**
```
m=video 0 RTP/AVP <pt>
a=rtpmap:<pt> H264/90000
a=fmtp:<pt> packetization-mode=1;sprop-parameter-sets=<sps>,<pps>
```

**Audio track (optional):**
- Codec: AAC-HBR (MPEG4-GENERIC)
- Sample rate: 48000
- Channels: 2

### 5.4 ICE

Default STUN servers:
```
stun:stun.l.google.com:19302
stun:stun1.l.google.com:3478
stun:stun2.l.google.com:5349
```

TURN credentials: received from signaling server (dynamic per-session).

Use trickle ICE (send candidates as discovered, don't wait for full gather).

### 5.5 Connection Type Reporting

After WebRTC connects, emit `webrtc_connection_info` with:
- Client ID
- Local connection type (HOST/SERVER_REFLEXIVE/RELAYED)
- Remote connection type

### 5.6 Recommended Implementation

Use **libdatachannel** (same library as the real firmware) for maximum compatibility:
- C++ or Python bindings available
- Handles ICE, DTLS, SRTP internally
- Just feed H.264 NAL units

Alternative: GStreamer webrtcbin (heavier but more flexible pipeline).

---

## 6. RTSP Server (Optional, Local)

For local network access:
- Path: `/live`
- Codec: H.264
- Port: configurable (original uses dynamic)
- Implementation: GStreamer RTSP server or live555

---

## 7. Event Handling (Server → Camera)

### 7.1 Trigger Events

Handle these commands from the server:

| Trigger | Action |
|---------|--------|
| Get features | Re-send `SendCameraSupportedFeatures` |
| Get status | Send current status |
| Get snapshot | Capture and upload immediately |
| Set snapshot enable/disable | Toggle upload loop |
| Start device reboot | Log/ignore (we're not rebooting Linux) |
| Start FW update | Acknowledge but skip |
| Start/Stop RTSP | Toggle RTSP server |
| Get protocol information | Send protocol info |

### 7.2 Configuration Events

| Config | Action |
|--------|--------|
| Upload interval | Adjust snapshot loop timing |
| Volume | Log/ignore (no speaker) |
| Camera name | Update stored name |
| Video quality (FHD/HD/SD) | Change encoder resolution |
| WebRTC mode | Enable/disable WebRTC |
| RTSP server mode | Toggle RTSP |
| IR mode | Log/ignore (no IR hardware) |
| Timelapse interval | Store if implementing timelapse |
| Rotation | Log/ignore (no motors) |
| Printing job name | Store (for timelapse folder naming) |

### 7.3 WebRTC Events

Process incoming offer/candidate messages, respond with answer/candidates.

---

## 8. Scoped Video Quality

When a WebRTC client connects with scoped config:
- Limit video resolution to the requested quality
- Reset to FHD when all scoped clients disconnect
- If via TURN: lock quality changes

---

## 9. Implementation Stack (Recommended)

### Minimal (Python)
```
python-socketio[asyncio]   — Socket.IO v4 client
aiohttp                    — HTTP client for uploads  
aiortc                     — WebRTC (uses libsrtp, OpenSSL)
opencv-python              — Camera capture + JPEG encoding
```

### Full (C++)
```
libdatachannel             — WebRTC (same as firmware)
socket.io-client-cpp       — Socket.IO
OpenSSL                    — TLS
nlohmann/json              — JSON
GStreamer                  — Video pipeline
```

### Hybrid (Python + GStreamer)
```
python-socketio            — Signaling
gi (GStreamer)             — Video capture, encoding, WebRTC
requests                   — HTTP uploads
```

---

## 10. State Machine

```
[INIT] → [CONNECTING] → [AUTHENTICATING] → [AUTHENTICATED] → [STREAMING]
                ↑                                    |
                └────────── [RECONNECTING] ←─────────┘ (on disconnect)
```

### INIT
- Load config, init video

### CONNECTING
- Socket.IO connect to signaling server

### AUTHENTICATING
- Send `camera_authentication`
- Wait for ACK

### AUTHENTICATED
- Send `send_sio_info`
- Send `protobuf_version`
- Send `client_trigger` (features)
- Start snapshot upload loop

### STREAMING
- Handle WebRTC offers → answer with video
- Handle trigger events
- Handle config events
- Continue snapshot uploads

### RECONNECTING
- Auto-reconnect Socket.IO
- Re-authenticate on reconnect
- Resume streaming state

---

## 11. Files Needed

```
impersonator/
├── config.ini              # Token, fingerprint, settings
├── main.py                 # Entry point
├── signaling.py            # Socket.IO client + auth
├── upload.py               # Snapshot HTTP upload
├── webrtc.py               # WebRTC offer/answer handling
├── video.py                # Camera capture + H.264 encoding
├── protocol.py             # Protobuf encode/decode
└── rtsp.py                 # Optional RTSP server
```

---

## 12. Security Notes

- Token is the primary secret — treat like a password
- Fingerprint is a stable device ID (not secret, but unique)
- All connections use TLS (wss:// and https://)
- The real camera uses system CA bundle from `/oem/usr/etc/ca-bundle.pem`
- No client certificate auth (just token + fingerprint headers/messages)

---

## 13. Testing Checklist

1. **Socket.IO connects** — verify WebSocket upgrade succeeds
2. **Auth ACK received** — `"ACK: OK"` after camera_authentication
3. **Snapshot uploads** — 200 response from PUT /c/snapshot
4. **Visible in Prusa Connect** — camera appears in web UI
5. **WebRTC stream works** — video visible in Prusa Connect live view
6. **Reconnection** — survives network interruption
7. **Config changes** — server can change quality/name/etc

---

## 14. Open Questions (Require Traffic Capture)

See findings.md sections 21-22 for full gap analysis. Key unknowns:

1. **Exact protobuf field numbers** — nanopb descriptors use delta-encoding; need traffic capture
2. **Binary message framing** — exact byte layout of WebRTC signaling msg
3. **Socket.IO event names for WebRTC responses** — does camera emit on same event or different?
4. **Protobuf schema version format** — is it just the string "3.1.5" or a structured message?
5. **Auth message exact format** — is it raw protobuf or JSON in binary envelope?
6. **Image server default URL** — likely `connect.prusa3d.com` (confirmed as cloud endpoint)
7. **Subtype field semantics** — what do the numeric subtypes in `{"bytes":[], "subtype": N}` mean?
8. **Video quality 1-10 mapping** — which number = which resolution?
9. **Token format/length** — obtained from Prusa Connect web UI "Camera > Token"

## 15. Confirmed by Community Project

Source: https://github.com/tlchandler/Improved-Buddy3D-Camera-for-Prusa-CORE-One

- Token is user-visible, copied from Prusa Connect web app
- Upload interval is in **milliseconds** (default 10000 = 10s)
- RTSP mode: 0=OFF, 2=ON
- IR mode in config: 0=day, 1=auto, 2=night
- Video quality: integer range 1-10 (default 6)
- Config file: `/userdata/xhr_config.ini` with `[config]` section
- WiFi password stored base64-encoded
- `lp_app` is started with `--noshell --log2file <path>` for logging
- Cloud endpoints: `connect.prusa3d.com`, `camera-signaling.prusa3d.com`, `connect-ota.prusa3d.com`, `timezone.prusa3d.com`
- The `snapshot_grabber` tool shows how to capture from Rockchip VI (NV12 frames from channel 0)
