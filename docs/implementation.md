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
fingerprint = <MD5 hex of any stable identifier, 32 chars>

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

# Build protobuf: field 1 = fingerprint, field 2 = token
auth_msg = encode_protobuf({
    1: fingerprint,  # string
    2: token,        # string
})
ack = await sio.emit('camera_authentication', auth_msg, callback=True)
# ack[0] should be 1 (bare integer)
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

## Step 2: Send Camera Status

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
timelapse_status = encode_protobuf({
    1: 2,
    2: 0,
    3: 0,
    4: "",
    5: 0,
    6: 0,
    7: Float32(0.0),
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
    2: encode_protobuf({}),
})
extended_status = encode_protobuf({
    1: firmware_version,
    2: hardware_name,
    3: camera_name,
    4: encode_protobuf({1: 2, 2: 0, 3: 0, 4: 0, 6: model}),
    6: encode_protobuf({1: 1, 2: 2, 4: rtsp_url}),
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

## Step 3: Send Protocol Version

```python
# Event: "protobuf_version"
version_msg = encode_protobuf({
    1: token,        # http.token; firmware uses FUN_00081c18 here
    2: "4.4",        # protocol schema version (MUST be "4.4")
})
await sio.emit('protobuf_version', version_msg)
```

---

## Step 4: Send Supported Features

```python
# Event: "features"
# The features string is comma-separated quoted names
FEATURES = (
    '"SocketCom","UploadInterval","TimelapseEn","TimelapseInterval",'
    '"TimelapseVideoMake","TimelapseFileList","VideoStream","RtspStream",'
    '"GetSnapshot","IrMode","SpeakerVolume","WiFi","FwVer","HwVer",'
    '"CameraName","MicroSd","FwUpdate","CameraReboot","McuTemp",'
    '"VideoQuality","WebRtc","TurnVideoQualityChange","trigger_scheme","FanControl"'
)

features_msg = encode_protobuf({
    2: token,
    3: firmware_version,
    4: hardware_name,
    5: "4.4",
    6: "[" + FEATURES + "]",
    7: "4.4",  # protocol_version, confirmed via DAT_000a8440 → 0x3f1f18
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
    "GetSnapshot", "IrMode", "SpeakerVolume", "WiFi", "FwVer", "HwVer",
    "CameraName", "MicroSd", "FwUpdate", "CameraReboot", "McuTemp",
    "VideoQuality", "WebRtc", "TurnVideoQualityChange", "trigger_scheme", "FanControl"
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

The server sends this to enable/disable the WebRTC service on real Buddy cameras.
An impersonator with `origin: OTHER` will not receive it, but a handler is prudent:

```python
@sio.on('set_webrtc_mode')
async def handle_set_webrtc_mode(data):
    msg = decode_protobuf(data)
    enable = msg.get(1, 0)
    print(f"set_webrtc_mode: enable={enable}")
    # Real firmware writes to webrtc_mode (+0x13d) and starts/stops the WebRTC service.
    # For the impersonator: log and ignore, or update internal state if implementing WebRTC.
```

---

## Step 8: WebRTC Streaming (Advanced)

### Enable Gate

The firmware's offer handler (`FUN_000b87b4`) silently drops any inbound offer unless
both singleton bytes are non-zero:
- `+0x13d` (`webrtc_mode`) — written by `set_webrtc_mode`
- `+0x13e` (`webrtc_status`) — set when the WebRTC service starts

The server only sends `set_webrtc_mode` to cameras that registered with an `origin`
other than `OTHER`. An impersonator registered as `OTHER` will never receive that event,
so both bytes stay zero and WebRTC offers are gated out. To enable WebRTC for an
impersonator, self-report both as enabled in the `status` message field `5.11`
(the WebRTC mode/status block). The real firmware encodes `{1: 1, 2: 2}` in that block
(mode enabled, status running).

### Inbound Offer Field Table

Decoded from `parseWebRtcMessage` (VMA `0xa36a4`). The server sends `msg_type = 3`
for offers; all other values cause an error response.

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

### Response Flow

When the server wants live video, it sends a `"webrtc"` event with an SDP offer
(`msg_type = 3`, `sdp` = offer body).

```python
@sio.on('webrtc')
async def handle_webrtc(data):
    msg = decode_protobuf(data)
    request_id = msg[1]
    msg_type = msg[2]
    sdp = msg[4]  # field 4 is the SDP body in inbound offers

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
- **AUTHENTICATING**: Send auth, wait ACK
- **READY**: Send status + version + features, start upload loop
- **STREAMING**: Handle WebRTC + triggers + config events
- **RECONNECTING**: On disconnect, re-auth on reconnect

---

## Minimal Viable Implementation (Snapshot Only)

If you only need the camera to appear in Prusa Connect with periodic snapshots (no live streaming):

```python
import asyncio, aiohttp, hashlib, time
from pathlib import Path

TOKEN = "your-token-from-prusa-connect"
FINGERPRINT = hashlib.md5(b"any-stable-id").hexdigest()
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
├── config.ini          # Token, fingerprint, network info
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
3. **Auth succeeds**: ACK response is `1` (bare integer)
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
