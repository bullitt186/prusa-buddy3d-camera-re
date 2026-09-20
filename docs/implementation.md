# Prusa Buddy3D Camera — Linux Impersonator Implementation Guide

Build a Linux application that appears as a genuine Buddy3D camera to Prusa Connect.

---

## Requirements

- Python 3.10+ (or C++ with libdatachannel)
- Any V4L2 camera or video source
- Network access to `camera-signaling.prusa3d.com` and `connect.prusa3d.com`
- A valid pairing token from Prusa Connect

---

## Dependencies (Python)

```
python-socketio[asyncio_client]  # Socket.IO v4 client
aiohttp                          # HTTP for uploads
protobuf                         # Standard protobuf (or just manual encoding)
aiortc                           # WebRTC (optional, for live streaming)
opencv-python                    # Camera capture + JPEG
```

---

## Configuration

```ini
[identity]
token = <paste from Prusa Connect web UI: Camera > Token>

[camera]
firmware_version = 3.1.6
model = Buddy3D-C1
manufacturer = Niceboy

[network]
wifi_mac = AA:BB:CC:DD:EE:FF
wifi_ipv4 = 192.168.1.100
wifi_ssid = MyNetwork

[upload]
interval_seconds = 10
server = connect.prusa3d.com
```

---

## Step 1: Authenticate

Connect Socket.IO and send auth:

```python
import socketio
import struct

sio = socketio.AsyncClient()
await sio.connect(
    'wss://camera-signaling.prusa3d.com',
    transports=['websocket']
)

# Build protobuf: field 1 = token, field 2 = fingerprint (firmware FUN_000a3058)
auth_msg = encode_protobuf({
    1: token,        # string
    2: fingerprint,  # string
})
ack = await sio.emit('camera_authentication', auth_msg, callback=True)
# Success ACK is the bare integer 0
# (1 = not authorized, 2 = error joining session). Nothing is sent post-auth:
# status/features/protobuf_version are trigger-driven (tags 1/2/12).
```

### Protobuf Encoding (minimal, no .proto needed)

```python
def encode_string_field(field_number, value):
    """Encode a protobuf string field (wire type 2)."""
    tag = (field_number << 3) | 2
    encoded_value = value.encode('utf-8')
    return encode_varint(tag) + encode_varint(len(encoded_value)) + encoded_value

def encode_varint(value):
    """Encode an unsigned varint."""
    result = b''
    while value > 127:
        result += bytes([(value & 0x7F) | 0x80])
        value >>= 7
    result += bytes([value & 0x7F])
    return result

def encode_protobuf(fields):
    """Encode a dict of {field_number: string_value} as protobuf."""
    result = b''
    for field_num, value in sorted(fields.items()):
        if value is not None and value != '':
            result += encode_string_field(field_num, value)
    return result
```

---

## Step 2: Send Camera Status (trigger tag 1)

> **Trigger-driven, not post-auth.** Firmware `FUN_000a05e4` sends **nothing** after a
> successful auth ACK; the server drives the camera with `trigger` polls. Steps 2–4 are the
> responses to trigger tags `1` (status), `12` (protobuf_version), and `2` (features)
> respectively — do not emit them unsolicited after auth (the server answers an unsolicited
> `protobuf_version` with ACK `1` + `CameraIsNotSessionMemberError`).

```python
# Event: "status"
# This is the complex 11-field CameraInfoMessage. The deployed impersonator
# sends the real firmware's always-present optional field set:
# - field 2: timelapse_status
# - field 3: camera_status
# - field 4: network_info; nested field 1 carries ssid/mac/ipv4/signal
# - field 5: extended_status
# - field 8: http.token
# - field 9: system_info
# - field 11: video_quality wrapper, field 1 enum (1=SD, 2=HD, 3=FHD)
#
# The simple encoder must support wire type 5 for Float32 values.
# timelapse_status (descriptor 0x3f753c): tags 1-3/5/7 uvarint, tag4 string,
# tag6 fixed32(float). tag1 = 1 enabled / 2 disabled, tag2 = interval seconds.
timelapse_status = encode_protobuf({
    1: 2,
    2: 10,
    3: 0,
    4: "",
    5: 0,
    6: Float32(0.0),
    7: 0,
})
camera_status = encode_protobuf({
    3: 1,   # IR auto
    4: 10,  # upload interval seconds
    5: 1,
    6: 40,
})
network_info = encode_protobuf({
    1: encode_protobuf({
        1: wifi_ssid,
        2: wifi_mac,
        3: wifi_ipv4,
        5: signal_quality,
    }),
})
extended_status = encode_protobuf({
    1: firmware_version,
    2: hardware_name,
    3: camera_name,
    # extended_status.4 (descriptor 0x3f72b0): SD storage block. tag1 = mounted
    # state (1=present, 2=absent), tags 2-4 = total/free/used MB, tag5 = mount
    # mode string ("RW"/"RO"/"UNKNOWN"). See timelapse.storage_status().
    4: encode_protobuf({
        1: sd_present,
        2: sd_total_mb,
        3: sd_free_mb,
        4: sd_used_mb,
        5: sd_mode,
    }),
    # extended_status.6 (descriptor 0x3f7278): tags 1-2 uvarint, tag3 string.
    6: encode_protobuf({1: rtsp_mode, 2: rtsp_status, 3: rtsp_url}),
    7: encode_protobuf({1: 1, 2: 0}),
    9: encode_protobuf({1: image_host, 2: signaling_host, 3: connect_host}),
    10: encode_protobuf({1: timezone_name, 2: 1}),
    11: encode_protobuf({1: 1, 2: 2}),
})
system_info = encode_protobuf({
    1: Float32(cpu_temperature),
    2: uptime_seconds,
    3: uptime_string,
    4: load_average,
    5: mem_total,
    6: mem_free,
    7: mem_shared,
    8: mem_buffers,
    9: process_count,
})
video_quality = encode_protobuf({1: 3})
status_msg = encode_protobuf({
    2: timelapse_status,
    3: camera_status,
    4: network_info,
    5: extended_status,
    8: token,
    9: system_info,
    11: video_quality,
})
await sio.emit('status', status_msg)
```

---

## Step 3: Send Protocol Version (trigger tag 12)

```python
# Event: "protobuf_version"; sent in response to trigger tag 12, never unsolicited.
version_msg = encode_protobuf({
    1: token,        # http.token; firmware uses FUN_00081c18 here
    2: "4.4",        # protocol schema version (MUST be "4.4")
})
await sio.emit('protobuf_version', version_msg)
```

---

## Step 4: Send Supported Features (trigger tag 2)

```python
import hashlib

# Event: "features"; sent in response to trigger tag 2, never unsolicited.
# The features string is comma-separated quoted names.
# GAP-CAP-01/GAP-DEVICE-02: IrMode, SpeakerVolume and FanControl are NOT advertised
# (the Pi has no such hardware). MicroSd IS advertised — the Pi backs it with the
# emulated SD at /mnt/sdcard, so Connect's timelapse UI works.
FEATURES = (
    '"SocketCom","UploadInterval","TimelapseEn","TimelapseInterval",'
    '"TimelapseVideoMake","TimelapseFileList","VideoStream","RtspStream",'
    '"GetSnapshot","WiFi","FwVer","HwVer",'
    '"CameraName","MicroSd","FwUpdate","CameraReboot","McuTemp",'
    '"VideoQuality","WebRtc","TurnVideoQualityChange","trigger_scheme"'
)

features_json = "[" + FEATURES + "]"
features_msg = encode_protobuf({
    2: token,
    3: firmware_version,
    4: hardware_name,
    5: "4.4",
    6: features_json,
    # field 7 is the MD5 of the bracket-wrapped features JSON, NOT the literal "4.4"
    # (firmware hash-building path; `signaling.send_features` computes this).
    7: hashlib.md5(features_json.encode()).hexdigest(),
})
await sio.emit('features', features_msg)
```

---

## Step 5: Upload Snapshots (HTTP)

```python
import aiohttp

async def upload_snapshot(jpeg_bytes, token, fingerprint, server="connect.prusa3d.com"):
    headers = {
        "User-Agent": "Buddy3D Camera",
        "Token": token,
        "Fingerprint": fingerprint,
        "Content-Type": "image/jpg",
        "Expect": "100-continue",
    }
    async with aiohttp.ClientSession() as session:
        async with session.put(
            f"https://{server}/c/snapshot",
            headers=headers,
            data=jpeg_bytes
        ) as resp:
            return resp.status
```

Run every 10 seconds in a loop.

---

## Step 6: Upload Camera Info (HTTP)

**Corrected 2026-07-06** — verified live against the real server. The earlier flat,
top-level version of this payload always got `400 Bad Request`. Almost everything must
be nested under `config`, and `features`/`capabilities` must be JSON arrays, not CSV
strings. See `protocol.md` §8 and `status.md` for how this was found (decompiling
`do_update_camera_attr`, VMA `0x00061bbc`).

```python
import json

FEATURES_LIST = [
    "SocketCom", "UploadInterval", "TimelapseEn", "TimelapseInterval",
    "TimelapseVideoMake", "TimelapseFileList", "VideoStream", "RtspStream",
    "GetSnapshot", "WiFi", "FwVer", "HwVer",
    "CameraName", "MicroSd", "FwUpdate", "CameraReboot", "McuTemp",
    "VideoQuality", "WebRtc", "TurnVideoQualityChange", "trigger_scheme"
]

async def upload_info(token, fingerprint, mac, ip, ssid, server="connect.prusa3d.com",
                       width=1920, height=1080, camera_name="Buddy3D Camera"):
    info = {
        "config": {
            "path": "private",
            "name": camera_name,
            "driver": "private",
            "model": "Buddy3D-C1",
            "firmware": "3.1.6",
            "manufacturer": "Niceboy",
            "trigger_scheme": "THIRTY_SEC",
            "resolution": {"width": width, "height": height},
            "network_info": {
                "wifi_mac": mac,
                "wifi_ipv4": ip,
                "wifi_ssid": ssid
            },
        },
        "options": {
            "available_resolutions": [{"width": width, "height": height}]
        },
        "capabilities": ["trigger_scheme"],
        "features": FEATURES_LIST,
    }
    headers = {
        "User-Agent": "Buddy3D Camera",
        "Token": token,
        "Fingerprint": fingerprint,
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.put(
            f"https://{server}/c/info",
            headers=headers,
            data=json.dumps(info)
        ) as resp:
            return resp.status, await resp.text()
```

---

## Step 7: Handle Incoming Events

```python
@sio.on('*')  # or register specific handlers
async def handle_event(event, data):
    # Incoming events are protobuf binary
    # For MVP: just acknowledge, don't process
    pass
```

For trigger events (get_snapshot, etc.), decode the protobuf and respond appropriately.

### set_webrtc_mode Handler

The server sends this to enable/disable the WebRTC service. **Superseded:** earlier notes
claimed an impersonator with `origin: OTHER` would never receive it. WebRTC now works live
with `origin: OTHER`, and the handler is implemented (`webrtc_control.apply_mode`), decoding
protobuf field 1 and keeping `webrtc_mode` (`+0x13d`) separate from `webrtc_status` (`+0x13e`).

```python
@sio.on('set_webrtc_mode')
async def handle_set_webrtc_mode(data):
    msg = decode_protobuf(data)
    enable = msg.get(1, 0)
    print(f"set_webrtc_mode: enable={enable}")
    # Real firmware writes to webrtc_mode (+0x13d) and starts/stops the WebRTC service.
    # The impersonator mirrors this via webrtc_control.apply_mode (mode != status).
```

---

## Step 8: WebRTC Streaming (Advanced)

### Enable Gate

The firmware's offer gate silently drops any inbound offer unless both singleton bytes are
non-zero:
- `+0x13d` (`webrtc_mode`) — written by `set_webrtc_mode`
- `+0x13e` (`webrtc_status`) — set when the WebRTC service starts

> **Function-name resolved (2026-09-20):** earlier revisions cited `FUN_000b87b4` for this gate.
> VMA `0xb87b4` is **not** a function entry — it lies 0x18 bytes inside the unrelated
> `FUN_000b879c`. The authoritative gate (and the `singleton + 0x140` enqueue) is
> `FUN_000b996c`, matching the gap tracker's `FW-WEBRTC-GATE` row and `webrtc_control.py`.
> `protocol.md` §10 has been corrected.

**Superseded:** earlier notes claimed the server only sends `set_webrtc_mode` to cameras
registered with an `origin` other than `OTHER`, that an `origin: OTHER` impersonator would
therefore never receive it, and that WebRTC had to be unblocked by self-reporting both bytes
enabled in `status` field `5.11`. Controlled `WEB`/`OTHER` tests disproved `origin` as the
gate, and WebRTC now works live with `origin: OTHER`; the gate is driven by the actual
`set_webrtc_mode` state, not a hardcoded status claim.

### Inbound Offer Field Table

**Superseded — viewer-side/log-format schema.** The flat 12-field table below is the
`parseWebRtcMessage` (VMA `0xa36a4`) log format string (`0x3f61c6`), rendered as if it were
the wire schema; it is **not** the camera-side inbound message. The camera-side inbound
message is the 9-field descriptor `0x3f7680` with nested submessages (see the table after
this one). Do not implement from the flat table (`GAP-WEBRTC-05`).

| Field | Semantic | Wire type | Notes |
|-------|----------|-----------|-------|
| 1 | `request_id` | string | Echoed back in the SDP answer |
| 2 | `msg_type` | varint | **3 = offer** |
| 3 | `client_id` | string | Viewer identifier |
| 4 | `sdp` | string | SDP offer body; max 3000 bytes |
| 5 | `transport_policy` | varint | ICE transport policy (all / relay) |
| 6 | `ttl` | varint | Session TTL |
| 7 | `video_cfg` | varint | Requested video config enum |
| 8 | `plan` | varint | SDP plan (unified-plan / plan-b) |
| 9 | `quality` | string | `"sd"` / `"hd"` / `"fhd"` |
| 10 | `fps` | varint | Requested FPS |
| 11 | `scope` | varint | Session scope |
| 12 | `ice_config` | submessage | TURN/STUN server list |

**Camera-side inbound (descriptor `0x3f7680`, 9 fields):** `msg_type` values are `1=request`,
`2=answer`, `3=offer`, `4=candidate`.

| Tag | Type | Notes |
|---|---|---|
| 1 | string | token |
| 2 | string | `request_id` (offer/answer) / `mid` (candidate) / `client_id` (ICE config) |
| 3 | string | fingerprint / `session` (candidate) / `session_id` (ICE config) |
| 4 | submessage | `{1: SDP}` for offer/answer; `{1: candidate}` for candidate |
| 5 | uvarint | msg type (`1=request`, `2=answer`, `3=offer`, `4=candidate`) |
| 7 | uvarint | 1 (offer) / 2 (answer/candidate/ICE config) |
| 8 | submessage | ICE config (ICE-config message only) |
| 9 | submessage | 5 uvarints (ICE-config message only) |

### Response Flow

When the server wants live video, it sends a `"webrtc"` event with an SDP offer
(`msg_type = 3`, `sdp` = offer body).

```python
@sio.on('webrtc')
async def handle_webrtc(data):
    msg = decode_protobuf(data)
    request_id = msg[1]
    msg_type = msg[2]
    # field 4 carries the SDP for offers (nested {1: SDP} per descriptor 0x3f7680)
    sdp = msg[4]

    if msg_type == 3:  # offer
        # Create PeerConnection, set remote description, create answer
        answer_sdp = create_webrtc_answer(sdp)
        
        response = encode_protobuf({
            1: request_id,
            2: 2,  # numeric camera-side enum: answer
            3: answer_sdp,
        })
        await sio.emit('webrtc', response)
```

Do not reuse the viewer-side nested `WebRtcSignal` schema here. Connect translates it into the
firmware's flat camera envelope. Camera-side values are `1=request`, `2=answer`, `3=offer`, and
`4=candidate`; inbound SDP is field 4, while outbound answer/candidate payloads are field 3.

### WebRTC Details
- Codec: H.264, Constrained Baseline, Level 3.1
- Packetization mode: 1
- Default STUN: `stun:stun.l.google.com:19302`
- Trickle ICE (send candidates as discovered)
- Library recommendation: `aiortc` (Python) or `libdatachannel` (C++)

---

## State Machine

```
INIT → CONNECTING → AUTHENTICATING → READY → STREAMING
                         ↑                        |
                         └── RECONNECTING ←───────┘
```

- **INIT**: Load config, init camera
- **CONNECTING**: Socket.IO connect
- **AUTHENTICATING**: Send auth (`token`, `fingerprint`), wait for ACK `0`
- **READY**: Send **nothing** post-auth; start the snapshot upload loop and wait for server
  `trigger` polls (tags 1/2/12 drive status/features/protobuf_version)
- **STREAMING**: Handle WebRTC + triggers + config events
- **RECONNECTING**: On disconnect, re-auth on reconnect

---

## Minimal Viable Implementation (Snapshot Only)

If you only need the camera to appear in Prusa Connect with periodic snapshots (no live streaming):

```python
import asyncio, aiohttp, hashlib, time
from pathlib import Path

TOKEN = "your-token-from-prusa-connect"
MAC = "AA:BB:CC:DD:EE:FF"  # normalized exactly as firmware's %02X formatter
FINGERPRINT = hashlib.md5(MAC.encode("ascii")).hexdigest()
SERVER = "connect.prusa3d.com"

async def main():
    while True:
        # Capture JPEG (replace with your camera source)
        jpeg = Path("/tmp/snapshot.jpg").read_bytes()
        
        headers = {
            "User-Agent": "Buddy3D Camera",
            "Token": TOKEN,
            "Fingerprint": FINGERPRINT,
            "Content-Type": "image/jpg",
        }
        async with aiohttp.ClientSession() as session:
            async with session.put(
                f"https://{SERVER}/c/snapshot",
                headers=headers,
                data=jpeg
            ) as resp:
                print(f"Upload: {resp.status}")
        
        await asyncio.sleep(10)

asyncio.run(main())
```

This works without Socket.IO — the HTTP snapshot endpoint is independent.

---

## File Structure

```
impersonator/
├── config.ini          # Token and camera/upload settings
├── main.py             # Entry point + state machine
├── proto.py            # Protobuf encode/decode helpers
├── signaling.py        # Socket.IO connection + auth + events
├── upload.py           # HTTP snapshot + info upload
├── webrtc.py           # WebRTC offer/answer handling (optional)
├── camera.py           # V4L2/OpenCV capture
└── features.py         # Feature list + constants
```

---

## Testing

1. **Snapshot upload works**: PUT /c/snapshot returns 200
2. **Camera appears in Prusa Connect**: visible in web UI after auth
3. **Auth succeeds**: ACK response is `0` (bare integer; `1` = not authorized)
4. **Reconnection**: survives network drop + re-authenticates
5. **WebRTC stream**: "Watch live" button in Prusa Connect shows video

---

## Constants

```python
SIGNALING_URL = "wss://camera-signaling.prusa3d.com"
SNAPSHOT_URL = "https://connect.prusa3d.com/c/snapshot"
INFO_URL = "https://connect.prusa3d.com/c/info"
PROTOCOL_VERSION = "4.4"
FIRMWARE_VERSION = "3.1.6"
MODEL = "Buddy3D-C1"
MANUFACTURER = "Niceboy"
USER_AGENT = "Buddy3D Camera"
TRIGGER_SCHEME = "THIRTY_SEC"
DEFAULT_UPLOAD_INTERVAL = 10  # seconds

VIDEO_QUALITY_SD = 1   # 640x480
VIDEO_QUALITY_HD = 2   # 1280x720
VIDEO_QUALITY_FHD = 3  # 1920x1080

STUN_SERVERS = [
    "stun:stun.l.google.com:19302",
    "stun:stun1.l.google.com:3478",
    "stun:stun2.l.google.com:5349",
]
```
