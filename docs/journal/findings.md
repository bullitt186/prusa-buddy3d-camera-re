# Prusa Buddy3D Camera Firmware — Reverse Engineering Findings

**Firmware version:** 3.1.5  
**Source:** `oem.img` (UBI image, single volume "oem", 148 PEBs, 131072 byte PEB size)  
**Main binary:** `oem_extracted/897730261/oem/usr/sbin/lp_app` (ARM ELF 32-bit, dynamically linked, stripped, uClibc)

---

## 1. Hardware Platform

| Component | Detail |
|-----------|--------|
| SoC | Rockchip RV1103 (ARM Cortex-A7, single core) |
| OS | Linux 5.10.110, BusyBox 1.27.2 |
| RAM | 34 MB total (~7 MB free at runtime) |
| Flash | Winbond W25N01GV 128 MB SPI NAND |
| Sensor | JX-F37P 2MP CMOS (1920x1080) — driver: `jx_f37P.ko` |
| WiFi | Realtek RTL8188FU (USB, 802.11n 2.4 GHz) — driver: `8188fu.ko` |
| NPU | RKNPU (module present but commented out in boot) |
| Video codec | Rockchip MPP (mpp_vcodec.ko, rockit.ko) |
| ISP | Rockchip ISP (video_rkisp.ko) |
| GPIO 53 | USB host mode enable (GPIO1_C5) |
| GPIO 34 | Secondary USB/power control |
| SPI serial | `/sys/class/spi_master/spi2/spi2.0/version` — HW version chip |

---

## 2. Filesystem Layout (oem partition)

```
usr/
├── bin/          # Rockchip test binaries, RkLunch.sh (boot entry), ntpd, sample apps
├── etc/          # Configs (ntp.conf, udhcpd.conf), WAV audio prompts, CA cert bundle
│   └── init.d/   # S10ntp
├── ko/           # Kernel modules + insmod scripts
├── lib/          # Shared libraries
├── sbin/         # lp_app (main), hostapd, wpa_supplicant_new, udhcpd, wifi scripts
└── share/
    ├── iqfiles/  # ISP image quality tuning
    ├── vqefiles/ # Audio VQE config
    └── zoneinfo/ # Timezone data (uclibc)
```

---

## 3. Boot Sequence

`RkLunch.sh` is the entry point, called by the system init:

1. `rcS()` — remount rootfs readonly, run `/oem/usr/etc/init.d/S??*` scripts (only S10ntp exists)
2. `post_chk()`:
   - Wait for `/userdata` mount (up to 3 seconds)
   - Run `insmod_ko.sh` (loads camera/video/wifi kernel modules)
   - Play `upgrade_voice.sh` (success sound if upgrade just completed)
   - `network_init eth0` and `network_init wlan0` (in background)
   - Check for SD card OTA: `/mnt/sdcard/cus_update_ota.tar` or `/mnt/sdcard/fac_update_ota.tar` → reboot recovery
   - Delete core dumps from `/userdata/`
   - Start `lp_app --noshell &` (main application)
3. Override mechanisms:
   - `/mnt/sdcard/lp_stop` — prevents lp_app from starting
   - `/mnt/sdcard/lp_app.sh` — replaces lp_app with custom script
   - `/mnt/sdcard/log2file` — enables `lp_app --noshell --log2file /mnt/sdcard/log`

---

## 4. Kernel Module Loading (`insmod_ko.sh`)

```
rk_dvbm.ko → video_rkcif.ko → video_rkisp.ko
→ phy-rockchip-csi2-dphy-hw.ko → phy-rockchip-csi2-dphy.ko
→ jx_f37P.ko (sensor driver, others commented out)
→ rga3.ko (2D graphics accelerator)
→ mpp_vcodec.ko (video codec)
→ snd-soc-rv1106.ko (audio)
→ rockit.ko (media pipeline, loads MCU firmware hpmcu_wrap.bin)
→ insmod_wifi.sh (background)
```

WiFi init (`insmod_wifi.sh`):
- Sets GPIO53 + GPIO34 high (USB host mode)
- Sets USB OTG mode to host
- Loads: `libsha256.ko → sha256_generic.ko → cfg80211.ko → 8188fu.ko`

---

## 5. Main Application (`lp_app`)

### 5.1 Build Info
- **Vendor SDK:** NSLpTech (XHR/Langpai platform)
- **C++ toolchain:** ARM GCC with uClibc, C++17 features
- **Libraries linked:**
  - nlohmann/json v3.11.3
  - WebSocket++ 0.8.2
  - ASIO (standalone, not Boost)
  - Socket.IO C++ client (sio::client)
  - libdatachannel (WebRTC, by user "miro")
  - libjuice (ICE/STUN/TURN)
  - OpenSSL 1.1.x
  - ZXing (QR code recognition)
  - Nanopb (lightweight protobuf)
  - SimpleWeb SSL client (HTTP)
  - Rockchip MPP/Rockit APIs

### 5.2 Internal Architecture (Classes)

| Class | Role |
|-------|------|
| `PrusaCommunicationService` | Socket.IO client, auth, message dispatch |
| `PrusaWebRTC` | WebRTC peer connection management |
| `PrusaRTSP` | Local RTSP server |
| `PrusaTimelapsService` | Timelapse capture and video generation |
| `NSLpTech::NSLpService::XhrUploadService` | Snapshot/image upload to cloud |
| `NSLpTech::NSLpService::XHRUpgradeService` | OTA firmware updates |
| `NSLpTech::NSLpService::XhrQRRecognizeService` | QR code pairing |
| `NSLpTech::NSLpService::XhrPlatformDeviceService` | Hardware control (motors, IR, fan) |
| `NSLpTech::NSLpModule::XhrWIFIModule` | WiFi STA/AP management |
| `NSLpTech::NSLpModule::XhrInputModule` | Input event handling |
| `NSLpTech::NSLpTools::LpXhrConfig` | Config file management |
| `NSLpTech::NSLpTools::SystemTimeManager` | NTP/time sync |
| `NSLpTech::NSLpTools::Tmi8150b_Motor` | Motor/gimbal control |
| `NS_ZToolKit::EventDispatcher` | Internal event bus |
| `NS_ZToolKit::NS_WorkQueue::WorkQueue` | Thread pool/work queue |
| `SystemDump` | Debug/diagnostics |
| `Stream` | Video stream source |
| `H264CameraSource` | H.264 encoder source for RTSP |

### 5.3 Command Line

```
lp_app [--noshell] [--log2file <path>]
```

- `--noshell` — no AT command shell on serial
- `--log2file` — log to file instead of stdout

---

## 6. Configuration System

### 6.1 Main Config: `/userdata/xhr_config.ini`

INI-format file managed by `LpXhrConfig` class. Actual path is `/userdata/xhr_config.ini` (not `/data/`). Contains `[config]` section header.

| INI Key | Description | Values |
|---------|-------------|--------|
| `http.token` | Auth token for Prusa Connect | string (same as `/userdata/xhr_http_token.conf`) |
| `http.fingerprint` | Device fingerprint (MD5) | hex string |
| `config.camera_name` | User-assigned camera name | string |
| `config.video_quality` | Video resolution | integer 1-10 (range, default 6) |
| `config.ir_mode` | IR cut filter mode | 0=day, 1=auto, 2=night |
| `config.rtsp_server_mode` | RTSP server state | 0=OFF, 2=ON |
| `config.webrtc_mode` | WebRTC enable/disable | integer |
| `config.volume` | Speaker volume | 5-100 (default 40) |
| `config.snapshot_upload_interval` | Upload frequency | **milliseconds** (default 10000 = 10s) |
| `service.img` | Image upload service enable | bool |
| `service.ota` | OTA service enable | bool |
| `service.socketio` | Socket.IO service enable | bool |
| `service.time` | Time sync service enable | bool |
| `wifi_ap.ssid` | AP mode SSID | string |
| `wifi_ap.pwd` | AP mode password | string |
| `ssid` | WiFi STA SSID | string |
| `pwd` | WiFi STA password | **base64 encoded** (uuencode -m) |
| `token` | Prusa Connect token | string (duplicated from http.token) |

**Note:** The `[config]` section header exists. Keys without a section prefix (like `ssid`, `pwd`, `token`) are at the top level.

### 6.2 Credential Files

| File | Content |
|------|---------|
| `/data/xhr_http_token.conf` | Pairing token (set via QR code or AT+TOKEN) |
| `/data/xhr_http_fingerprint.conf` | MD5 fingerprint (generated from MAC) |
| `/data/wlan0addr.txt` | Persisted WiFi MAC address |
| `/data/eth0addr.txt` | Persisted Ethernet MAC address |

### 6.3 SD Card Overrides

Files on SD card that override default behavior:

| Path | Purpose |
|------|---------|
| `/mnt/sdcard/imgsrv` | Custom image upload server URL (format: `IMAGE_SRV=<url>`) |
| `/mnt/sdcard/siosrv` | Custom Socket.IO server URL (format: `SIO_SRV=<url>`) |
| `/mnt/sdcard/ota` | Custom OTA server URL |
| `/mnt/sdcard/syslog_srv` | Remote syslog server config |
| `/mnt/sdcard/video_cfg` | Video init config (resolution on boot) |
| `/mnt/sdcard/sdcard_cfg.txt` | General SD card configuration |
| `/mnt/sdcard/lp_stop` | Prevent lp_app from starting |
| `/mnt/sdcard/lp_app.sh` | Custom startup script (replaces lp_app) |
| `/mnt/sdcard/log2file` | Enable logging to `/mnt/sdcard/log` |

---

## 7. Device Identity

### 7.1 Model Names Found in Binary

- `Buddy3D`
- `Buddy3D Camera`
- `Buddy3D-C1`
- `Buddy3D-POE`
- `Buddy3D.`
- `Niceboy`

### 7.2 Identity Fields

| Field | Source | Example |
|-------|--------|---------|
| Serial Number (SN) | OTP memory on SPI chip | integer, burned at factory |
| HW Version | SPI version chip `/sys/class/spi_master/spi2/spi2.0/version` | integer |
| FW Version | Compiled into binary | `3.1.5` |
| Model | Hardcoded | `Niceboy` (for /c/info JSON) |
| MAC Address | WiFi adapter, persisted to `/data/wlan0addr.txt` | standard format |
| Fingerprint | MD5 of MAC address | hex string, stored in `/data/xhr_http_fingerprint.conf` |
| Token | Pairing token from QR code or AT command | string, stored in `/data/xhr_http_token.conf` |

### 7.3 Fingerprint Generation

Source: `xhr_tools/Util/lp_fingerprint_generation_tool.cpp`
- Gets MAC address via `iw dev`
- Computes MD5 hash
- Falls back to random string if MAC unavailable
- Stored in `/data/xhr_http_fingerprint.conf`
- Initialized by `init_fingerPrint` / `generateFingerPrint`

---

## 8. Network Services

### 8.1 Cloud Servers (Prusa)

| Service | Production URL | Staging URL |
|---------|---------------|-------------|
| Signaling (Socket.IO) | `camera-signaling.prusa3d.com` | `camera-signaling-stage.prusa3d.com` |
| OTA Updates | `connect-ota.prusa3d.com` | `connect-ota-staging.prusa3d.com` |
| Image Upload | `connect-ota.prusa3d.com` (same) | `dev.connect.prusa3d.com` |
| Timezone | `timezone.prusa3d.com` | — |
| NTP | `prusa3d.pool.ntp.org` (preferred) | 0-3.pool.ntp.org |

### 8.2 Local Services

| Service | Port/Protocol |
|---------|--------------|
| RTSP | Dynamic port, path `/live` |
| WiFi AP | 192.168.42.1, DHCP range .20-.254 |
| DHCP server | udhcpd on wlan0 (AP mode) |

### 8.3 ICE/STUN/TURN (WebRTC)

Default STUN servers (hardcoded):
- `stun:stun.l.google.com:19302`
- `stun:stun1.l.google.com:3478`
- `stun:stun2.l.google.com:5349`

TURN servers: provided dynamically by signaling server per-session.

---

## 9. Socket.IO Protocol

### 9.1 Connection

- URL: `wss://camera-signaling.prusa3d.com/socket.io/?EIO=4&transport=websocket`
- Library: Socket.IO C++ client with WebSocket++ 0.8.2 + ASIO TLS
- Reconnection: automatic with configurable interval (`SetSocketReconnectIntervalMin`)

### 9.2 Camera → Server Events (Emitted)

| Event Name | Payload Type | Purpose |
|------------|--------------|---------|
| `camera_authentication` | Binary (protobuf) | Auth with fingerprint + token |
| `send_sio_info` | Binary/JSON | Camera info (model, firmware, network) |
| `protobuf_version` | Binary | Schema version announcement |
| `client_trigger` | Binary (protobuf) | Feature support, status updates |
| `webrtc_connection_info` | Binary | Report connection type per WebRTC client |

### 9.3 Server → Camera Events (Received)

The camera registers handlers via `PrusaCommunicationService::SetEventHandler`. Three main event types:

**Trigger events** — `"Trigger event received: %s"`
- Get features
- Get status
- Get snapshot
- Set snapshot enable / disable
- Timelapse enable / disable
- Start timelapse video make
- Get timelapse file list
- Start device reboot
- Start FW update
- Start/Stop RTSP stream
- Get protocol information

**WebRTC events** — `"Event received: %s"`
- Binary WebRTC signaling messages (offer/answer/candidate)

**Configuration events** — `"Configuration event received: %s"`
- Rotation direction + angle (RotationX/Z)
- Camera mode (IR: auto/day/night)
- Upload interval
- Volume
- Printing job name
- Camera name
- RTSP server mode
- Timelapse interval + video FPS
- Video quality (FHD/HD/SD)
- WebRTC mode (enable/disable)

### 9.4 ACK Responses

After emitting, the camera receives ACK callbacks:
- `"ACK: OK"` — success
- `"ACK: Not authorized"` — token invalid
- `"ACK: Error joining session"` — session error
- `"ACK: Missing token"` — no token provided
- `"ACK: Error decoding message"` — malformed payload
- `"ACK: Unknown error"`
- `"ACK: received, but no data in the list"` — empty response
- `"ACK: received, but unknown data type: %d"` — unexpected type

### 9.5 Connection Lifecycle

```
1. Socket opens
2. "Socket opened: %s . Start sending authentication message"
3. emit("camera_authentication") with fingerprint + token
4. On ACK OK:
   - emit("send_sio_info") — camera info
   - emit("protobuf_version") — schema version
   - emit("client_trigger") — SendCameraSupportedFeatures
5. Listen for incoming trigger/webrtc/config events
6. On disconnect: "SocketIO disconnected" → auto reconnect
7. On reconnect: re-send auth message
```

---

## 10. Protobuf Encoding

### 10.1 Encoding Functions

| Function | Purpose |
|----------|---------|
| `pb_encode_string_cus` | Encode general messages (triggers, features, config) |
| `pb_decode_string_cus` | Decode general messages from server |
| `pb_encode_string_webrtc` | Encode WebRTC signaling messages |
| `pb_decode_string_webrtc` | Decode WebRTC signaling messages |

### 10.2 Known Protobuf Types (from C++ mangled names)

- `_camera_protocol_ClientTrigger` — camera-to-server trigger message
- `_camera_protocol_WebRtcConnectionType` — connection type enum

### 10.3 Known Message Fields

**CameraInfo** (`send_sio_info` / `/c/info`):
- `model` (string) — "Niceboy"
- `manufacturer` (string)
- `firmware` (string) — "3.1.5"
- `trigger_scheme` (string) — "THIRTY_SEC"
- `network_info` (nested):
  - `wifi_mac` (string)
  - `wifi_ipv4` (string)
  - `wifi_ssid` (string)
- `available_resolutions` (repeated):
  - `width` (int)
  - `height` (int)

**SupportedFeatures** (from `SendCameraSupportedFeatures`):
Advertised capability strings:
- `"CameraName"`, `"CameraReboot"`, `"FanControl"`, `"FwUpdate"`, `"FwVer"`
- `"GetSnapshot"`, `"HwVer"`, `"IrMode"`, `"McuTemp"`, `"MicroSd"`
- `"RotationX"`, `"RotationZ"`, `"RtspStream"`, `"SocketCom"`, `"SpeakerVolume"`
- `"TimelapseEn"`, `"TimelapseFileList"`, `"TimelapseInterval"`, `"TimelapseVideoMake"`
- `"TurnVideoQualityChange"`, `"UploadInterval"`, `"VideoQuality"`, `"VideoStream"`, `"WebRtc"`, `"WiFi"`

**ClientTrigger** messages:
- `SendClientTriggerBase` — generic trigger wrapper
- `SendClientTriggerErrorCode` — error code report
- `SendClientTriggerTimelapseVideoMakeStatus` — timelapse progress
- `SendClientTriggerUpgradeProcess` — OTA progress

**Protobuf Schema Version:**
- Event: `protobuf_version`
- Version value: likely `3.1.5` (matches firmware)

### 10.4 Protobuf Translate Functions

| Function | Purpose |
|----------|---------|
| `TranslateVideoLpRv1106ToProtobuf` | Video quality enum → protobuf value |
| `TranslateVideoProtobufToLpRv1106` | Protobuf value → internal video quality enum |
| `TranslateVideoProtobufToString` | Protobuf value → "FHD"/"HD"/"SD" |
| `TranslateLogLevelToProtobuf` | Log level → protobuf enum |
| `TranslateWebRTCConnectionTypeToProtobufEnum` | Connection type → protobuf enum |
| `SetVideoQualityFromProtobuf` | Apply video quality from protobuf message |
| `translateWebRtcMsgTypeUintToString` | WebRTC msg type uint → "offer"/"answer"/"candidate" |

### 10.5 WebRtcConnectionType Enum Values

- `WEBRTC_CONNECTION_TYPE_HOST` — direct LAN
- `WEBRTC_CONNECTION_TYPE_SERVER_REFLEXIVE` — via STUN
- `WEBRTC_CONNECTION_TYPE_PEER_REFLEXIVE`
- `WEBRTC_CONNECTION_TYPE_RELAYED` — via TURN
- `WEBRTC_CONNECTION_TYPE_UNDEFINED`
- `WEBRTC_CONNECTION_TYPE_UNKNOWN`

### 10.6 Binary Message Envelope

Socket.IO binary messages are JSON-wrapped:
```json
{"bytes": [<byte array>], "subtype": <integer>}
```

### 10.7 Disassembly of Protobuf Encoding (ARM)

**`pb_encode_string_cus`** at VMA `0x0009c294`:
- Source file: `socketio_comm/prusa_socketio_comm.cpp`
- Signature: `bool pb_encode_string_cus(pb_ostream_t* stream, const char* string, const pb_field_t* field)`
- Calls:
  - `0x9a938` — `pb_encode_tag()`: reads field type from descriptor byte at offset +22, field number from uint16 at offset +16
  - `0x9a9c4` — `pb_encode_string()`: writes varint length + string bytes to stream
- Error strings: "Encoding string failed: null pointer!", "Failed to encode tag for field", "Failed to encode string", "Exception during string encoding: %s"

**Tag encoder jump table** (at `0x9a938`):
```
Field type nibble → wire type:
  0,1,2,3  → varint (wire type 0)
  4        → fixed32 (wire type 5)
  5        → fixed64 (wire type 1)
  6,7,8,9  → length-delimited (wire type 2)
  11       → length-delimited (wire type 2)
```

**`SendCameraInfoMessage`** at VMA `0x000a0a00`:
- References `pb_encode_string_cus` as function pointer (literal pool at `0x0a0ce4`)
- References "wlan0" (reads WiFi interface for network_info)
- Uses "SendCameraInfoMessage" as log tag
- Message descriptor table at VMA `0x003f5e98`:
  - 11 fields total
  - Field types: OPTIONAL SUBMSG (nested messages), OPTIONAL UVARINT (uint32), BOOL
  - Sub-message descriptors for network_info and available_resolutions
  - Uses nanopb paired 32-bit word format: `[type_flags | size_offset]`

**nanopb Type Encoding** (confirmed from binary):
```
PB_LTYPE (low nibble):
  0x0 = BOOL
  0x1 = VARINT (signed)
  0x2 = UVARINT (unsigned)
  0x4 = FIXED32
  0x5 = FIXED64
  0x7 = STRING
  0x8 = SUBMESSAGE

PB_HTYPE (bits 4-7):
  0x0 = REQUIRED
  0x1 = OPTIONAL
  0x4 = FIXARRAY (fixed-size repeated)
```

### 10.8 Reconstructed Protobuf Schemas (DEFINITIVE — from Ghidra analysis)

All message descriptors have `largest_tag=0`, meaning **field numbers are sequential starting from 1**. Confirmed by Ghidra headless analysis of nanopb descriptor tables in `.rodata`.

**Protocol schema version: "4.4"** (NOT the firmware version "3.1.5")

```protobuf
// DEFINITIVE SCHEMAS (Ghidra-verified, 2026-07-06)
// Auth message (2 string fields, NOT 4)
message CameraAuthentication {  // descriptor 0x3f5c94
    string fingerprint = 1;   // MD5 of MAC
    string token = 2;         // from Prusa Connect UI
}

// Protobuf schema version (4 string fields)
// Event: "protobuf_version", descriptor 0x3f61f8
message ProtobufSchemaVersion {
    string version = 1;       // "4.4" (schema version, NOT firmware version)
    string field2 = 2;        // unknown
    string field3 = 3;        // unknown
    string field4 = 4;        // unknown
}

// WebRTC connection type (4 string fields)
// Event: "webrtc_connection_info"
message WebRtcConnectionType {
    string field1 = 1;  // likely: client_id
    string field2 = 2;  // likely: local_type
    string field3 = 3;  // likely: remote_type
    string field4 = 4;  // likely: ??? (session id?)
}

// WebRTC signaling message (6 string fields)
// Used for sending offer/answer/candidate
message WebRtcMessage {
    string field1 = 1;  // likely: request_id
    string field2 = 2;  // likely: type ("offer"/"answer"/"candidate")
    string field3 = 3;  // likely: sdp
    string field4 = 4;  // likely: client_id
    string field5 = 5;  // likely: ???
    string field6 = 6;  // likely: ???
}

// Camera info message (11 fields, mixed submessages + strings)
// Event: "send_sio_info"
message CameraInfoMessage {
    SubMsg1 field1 = 1;    // submessage, 32 bytes
    SubMsg2 field2 = 2;    // submessage, 32 bytes
    SubMsg3 field3 = 3;    // submessage, 32 bytes
    SubMsg4 field4 = 4;    // submessage, 68 bytes
    SubMsg5 field5 = 5;    // submessage, 152 bytes (likely network_info)
    SubMsg6 field6 = 6;    // submessage, 8 bytes
    string  field7 = 7;    // string (callback)
    string  field8 = 8;    // string (callback)
    repeated SubMsg9 field9 = 9;  // REPEATED submessage, 72 bytes (likely available_resolutions)
    string  field10 = 10;  // string (callback)
    SubMsg11 field11 = 11; // submessage, 4 bytes (likely a single int/enum)
}

// Supported features (6 fields: 3 strings + 3 mixed)
// Event: "client_trigger" (SendCameraSupportedFeatures)
message SupportedFeatures {
    string field1 = 1;  // callback string
    string field2 = 2;  // callback string
    string field3 = 3;  // callback string
    // fields 4-6 appear to be arrays/repeated (0x12 type = UVARINT or STRING in array)
}

// Client trigger (6 fields: 3 strings + 3 mixed)
// Event: "client_trigger" (SendClientTriggerBase)
message ClientTrigger {
    string field1 = 1;  // callback string
    string field2 = 2;  // callback string
    string field3 = 3;  // callback string
    // fields 4-6: array fields
}

// Configuration event response (9 fields: 3 strings + submessages + arrays)
// Table at 0x3f6680
message ConfigurationEvent {
    string field1 = 1;
    string field2 = 2;
    string field3 = 3;
    SubMsg field4 = 4;   // submessage, 16 bytes
    // fields 5-9: mixed arrays and submessages
}
```

**Key insight:** The `0x57` type byte in callback string fields = `0x50 (PB_HTYPE_CALLBACK) | 0x07 (PB_LTYPE_STRING)`. The `pb_encode_string_cus` function IS the nanopb callback encoder for these fields.

**Field assignment confidence:**
- Auth: CONFIRMED 2 fields only: fingerprint=1, token=2 (from log + descriptor count)
- WebRTC: CONFIRMED request_id=1, type=2, sdp=3 (from log + 9-field descriptor)
- CameraInfo: Structure confirmed (11 fields), internal submessage fields TBD

### 10.9 CORRECTED Schemas (Ghidra session 2, with decompilation)

Critical corrections from decompiler analysis:

| Function | Descriptor | Fields | Socket.IO Event | Notes |
|----------|-----------|--------|-----------------|-------|
| SendAuthMessage | 0x3f5c94 | **2** | **`camera_authentication`** | fingerprint + token |
| SendProtobufSchemaVersion | 0x3f61f8 | 4 | **`protobuf_version`** | version="4.4" |
| SendCameraSupportedFeatures | 0x3f5d84 | 6 (all strings) | **`features`** | includes "4.4" |
| SendClientTriggerBase | 0x3f5f58 | 6 (2 str + 4 mixed) | **`client_trigger`** | status/error reports |
| SendCameraInfoMessage | 0x3f5e98 | 11 (submsg+str+int) | **`status`** | camera info |
| SendTimelapseFileList | 0x3f601c | 4 (all strings) | via `client_trigger` | file list |
| SendWebRtcConnectionType | 0x3f65c8 | 6 (3 str + 3 bytes) | **`webrtc_connection_info`** | conn type |
| SendWebRTCMessage | 0x3f6680 | 9 (3 str + submsg + bytes) | **`webrtc`** | offer/answer/candidate |

**Critical corrections from decompilation:**
- Auth is 2-field (not 4). Only fingerprint + token.
- Schema version is "4.4", not "3.1.5" (that's the firmware version).
- Features event is `"features"` (NOT `"client_trigger"` as previously assumed).
- Camera info event is `"status"` (NOT `"send_sio_info"`).
- WebRTC signaling event is `"webrtc"`.
- WebRTC message types include `"request"` in addition to offer/answer/candidate.

**Socket.IO emit mechanism:** Raw protobuf bytes are passed directly to `sio_client->emit(event_name, binary_blob, ack_callback)`. There is NO application-level wrapper or "subtype" field — Socket.IO's binary event framing handles the envelope automatically.

### 10.10 CameraInfo Sub-Message Structure (from Ghidra sub-descriptor analysis)

The 11-field CameraInfoMessage has 8 sub-descriptors:

| Sub | Fields | Size | Likely Content |
|-----|--------|------|----------------|
| [0] | 4 (2 submsg + 2 arrays) | 32B | model/manufacturer nested |
| [1] | 7 (3 arrays + 1 string + 2 mixed) | 32B | firmware/version info |
| [2] | 6 (1 array + 1 submsg + 4 arrays) | 32B | capabilities? |
| [3] | 2 (2 submsg) | 68B | nested config (36B + 24B subs) |
| [4] | 11 (3 strings + 8 submsg) | 152B | **network_info** (wifi_mac, ipv4, ssid + many sub-fields) |
| [5] | 2 (1 fixed32 + 1 array) | 8B | simple pair |
| [6] | 9 (1 fixed32 + 2 strings + 6 arrays) | — | **trigger/config** (repeated fields with 0x80 flag) |
| [7] | 1 (1 array) | 4B | single repeated field |

Sub-descriptor[4] with 11 fields (3 strings + 8 submessages) matches network_info perfectly — it has string fields for wifi_mac, wifi_ipv4, wifi_ssid plus nested sub-messages for additional network details.

Sub-descriptor[6] (used for field 9 = repeated) has 9 fields starting with a fixed32, confirming it's the **available_resolutions** entry (width/height as integers + metadata).

---

## 21. Remaining Gaps & Recommended Next Steps

### Critical (blocking implementation):

1. **Exact protobuf field numbers for all message types**
   - Need: traffic capture (mitmproxy on WiFi, or serial console logging)
   - Alternative: check if Prusa has published a `.proto` file in any open-source repo
   
2. **Socket.IO event name for WebRTC responses (camera → server)**
   - Is it the same event as received, or a different event name?
   - Need: traffic capture or test with Socket.IO server

3. **Auth message format**
   - Is `camera_authentication` a protobuf binary or JSON?
   - The log "Fingerprint: %s, Token: %s" suggests JSON, but the binary envelope suggests protobuf
   
4. **Image server default URL**
   - Is it `connect-ota.prusa3d.com` or a separate host?
   - The string `"Use default image server address %s"` implies it's dynamic
   - `dev.connect.prusa3d.com` appears only as a staging reference

### Important (quality of impersonation):

5. **Subtype field semantics in `{"bytes":[], "subtype": N}`**
   - What numeric values correspond to which message types?
   
6. **Protobuf schema version value**
   - Is it the string "3.1.5", a numeric version, or a structured message?

7. **WebRTC signaling binary message exact byte layout**
   - The format string gives field names but not byte-level encoding
   - Are lengths varint-encoded or fixed-width?

8. **TURN server credential format**
   - How does the signaling server provide TURN credentials?
   - Part of the WebRTC offer message or separate event?

### Nice to have:

9. **OTA version comparison logic**
   - How does "Image server Stable version" vs "Unstable version" work?
   
10. **Timelapse file list protobuf format**
    - For full feature parity

### 10.11 Final Extraction Results (Ghidra Session 3)

#### Incoming Events (Server -> Camera)

The camera subscribes to 3 Socket.IO event types (all protobuf-encoded):

| Server Event | Handler | Decoded Fields |
|-------------|---------|----------------|
| (WebRTC event) | "Event received: %s" | WebRTC binary protobuf (same format as outgoing) |
| (Trigger event) | "Trigger event received: %s" | Command protobuf with trigger type |
| (Config event) | "Configuration event received: %s" | Settings protobuf |

**Trigger commands** (field in decoded protobuf):
- Get features, Get status, Get snapshot (`get_snapshot`)
- Set snapshot enable/disable (`enable_snapshot_upload`, `disable_snapshot_upload`)
- Timelapse enable/disable (`enable_timelaps`)
- Start timelapse video make (`timelapse_make_video`)
- Get timelapse file list (`timelapse_get_file_list`)
- Start device reboot (`reboot_device`)
- Start FW update (`start_fw_update`)
- Start/Stop RTSP (`start_rtsp_server`, `stop_rtsp_server`)
- Get protocol information

**Configuration fields** (each decoded from protobuf):
- Rotation direction + angle (`motor_controll`)
- Camera mode / IR (`light_control`)
- Upload interval in SECONDS (`set_snapshot_upload_interval`)
- Volume (`set_volume`) + plays `volume_changed.wav`
- Printing job name (`set_printing_job_name`)
- Camera name (`set_camera_name`)
- RTSP server mode: 0=AUTO, 1=ON, 2=OFF (`set_rtsp_server_mode`)
- WebRTC mode: ENABLE/DISABLE (`set_webrtc_mode`)
- Timelapse interval in seconds (`set_timelaps_interval`)
- Timelapse video FPS (`set_timelaps_video_fps`)
- Video quality: protobuf enum value (`SetVideoQualityFromProtobuf`)

#### HTTP Upload Headers (CORRECTED)

**Snapshot upload (`PUT /c/snapshot`):**
```
User-Agent: Buddy3D Camera
Token: <token>
Fingerprint: <fingerprint>
Content-Type: image/jpg
Expect: 100-continue
```

**Camera info (`PUT /c/info`):**
```
User-Agent: Buddy3D Camera
Token: <token>
Fingerprint: <fingerprint>
Content-Type: application/json
```

**OTA check (`GET /api/niceboy/v1/camera`):**
```
User-Agent: Buddy3D Camera
X-Camera-Token: <token>
X-Camera-Fingerprint: <fingerprint>
X-Camera-FW-Version: 3.1.5
```

NOTE: Snapshot/info use SHORT header names (`Token`, `Fingerprint`). OTA uses PREFIXED names (`X-Camera-*`).

#### Camera Info JSON (exact field names from binary)

```json
{
  "model": "Niceboy",
  "firmware": "3.1.5",
  "manufacturer": "...",
  "trigger_scheme": "THIRTY_SEC",
  "network_info": {
    "wifi_mac": "...",
    "wifi_ipv4": "...",
    "wifi_ssid": "..."
  },
  "available_resolutions": [
    {"width": 1920, "height": 1080},
    {"width": 1280, "height": 720},
    {"width": 640, "height": 480}
  ],
  "options": {...},
  "capabilities": {...},
  "features": {...}
}
```

Additional JSON keys found near `/c/info` builder: `"resolution"`, `"name"`, `"driver"`, `"path"`, `"config"`, `"private"`.

#### Video Quality Protobuf Enum (DEFINITIVE)

| Protobuf Value | Resolution | Internal Config Value | String |
|----------------|------------|----------------------|--------|
| 1 | 640x480 | 5 | "SD" |
| 2 | 1280x720 | 6 | "HD" |
| 3 | 1920x1080 | 7 | "FHD" |
| 0 | invalid | — | "UNKNOWN" |

TranslateVideoProtobufToString: `1->"SD"`, `2->"HD"`, `3->"FHD"`, else `"UNKNOWN: " + str(val)`

SetVideoQualityFromProtobuf switch: `case 1->SD(5)`, `case 2->HD(6)`, `case 3->FHD(7)`, `default->invalid`

#### Upload Interval

- Protocol transmits interval in **SECONDS** (log: "Setting upload interval: %d seconds")
- Config file stores as **milliseconds** (community project confirmed: `snapshot_upload_interval=10000`)
- Validated against min/max range: "Snapshot interval out of range (%d seconds, valid: %d-%d)"
- Default: 10 seconds (10000ms in config)

#### ICE Server Configuration

Source: `webrtc/prusa_webrtc.cpp`, function `BuildConfiguration()`
- If no servers received: "No ICE servers received, using default configuration"
- STUN URLs prefixed with `"stun:"`
- TURN: `"Adding TURN: %s:%d User: %s (Type: %d)"` — hostname, port, username, type
- Transport policies: "RELAY", "ALL", "DEFAULT (All)"
- ICE servers come FROM the protobuf-decoded incoming WebRTC message (the submessage fields 4-9)

#### Incoming WebRTC Messages

**Critical finding:** Server sends WebRTC messages as **protobuf** on the same `"webrtc"` event channel (NOT raw binary). The camera decodes them with the same nanopb `pb_decode_string_webrtc` callback.

Decoded struct size: 0x65 = 101 bytes (passed to nanopb decode). Fields after decode:
- request_id (string)
- msg_type (uint: maps to offer/answer/candidate/request)
- client_id (string)
- sdp (string)
- transport_policy (uint)
- ttl (uint)
- video_cfg_present (bool/byte)
- plan (uint)
- quality (string, built from video_cfg)
- fps (byte)
- ttl2 (uint)
- scope (uint)

This matches the 9-field WebRTCMessage descriptor at `0x3f6680`.

### Recommended approach (UPDATED):

With the protocol now fully documented, implementation can proceed directly. No traffic capture needed.

### 10.12 Definitive Features String (from Ghidra decompilation of buildFeatureList)

The features list is a **comma-separated string of double-quoted names**, built by concatenation:

```
"SocketCom","UploadInterval","TimelapseEn","TimelapseInterval","TimelapseVideoMake","TimelapseFileList","VideoStream","RtspStream","GetSnapshot","RotationX","RotationZ","IrMode","SpeakerVolume","WiFi","FwVer","HwVer","CameraName","MicroSd","FwUpdate","CameraReboot","McuTemp","VideoQuality","WebRtc","TurnVideoQualityChange","trigger_scheme","FanControl"
```

- `"RotationX"` and `"RotationZ"` are **conditional** (only included if motor hardware is detected via `FUN_00070a50()`)
- Quotes are PART of the string values (each name is wrapped in `"`)
- Separator is `,` (no spaces)
- This string is placed in the JSON `"features"` field as a raw string value
- Same string goes into the protobuf `"features"` event message

### 10.13 ProtobufSchemaVersion Fields (DEFINITIVE)

```protobuf
// Event: "protobuf_version", descriptor 0x3f61f8
message ProtobufSchemaVersion {
    string fingerprint = 1;     // device fingerprint (always present)
    string version = 2;         // "4.4" (protocol schema version, always)
    string request_id = 3;      // optional (only when responding to server request)
    string field4 = 4;          // NOT SENT (callback=NULL, nanopb skips)
}
```

Field 1 = fingerprint (from `FUN_000816b4`/`FUN_00081c18`), same as in auth.
Field 2 = `"4.4"` literal (from `DAT_000a27a8`).
Field 3 = conditionally set from `param_1[0x11]` if `param_1[0x12] != 0`.
Field 4 = never sent (callback pointer is null).

### 10.14 Complete /c/info JSON Structure (from do_update_camera_attr decompilation)

```json
{
  "config": {
    "path": "...",
    "name": "...",
    "driver": "..."
  },
  "model": "Niceboy",
  "firmware": "3.1.5",
  "manufacturer": "...",
  "trigger_scheme": "THIRTY_SEC",
  "resolution": {
    "width": 1920,
    "height": 1080
  },
  "network_info": {
    "wifi_mac": "AA:BB:CC:DD:EE:FF",
    "wifi_ipv4": "192.168.1.100",
    "wifi_ssid": "MyNetwork"
  },
  "options": {...},
  "available_resolutions": [
    {"width": 1920, "height": 1080},
    {"width": 1280, "height": 720},
    {"width": 640, "height": 480}
  ],
  "capabilities": ["..."],
  "features": "\"SocketCom\",\"UploadInterval\",\"TimelapseEn\",...,\"FanControl\""
}
```

Notes:
- `"features"` value is a raw CSV string with double-quoted names (NOT a JSON array of strings)
- `"capabilities"` uses `[` and `]` brackets (JSON array)
- `"config"` section has `"private"` key usage (possibly for internal config identification)
- `"options"` is built separately (content TBD but non-blocking)

---

## 22. Insights from Community Project (tlchandler/Improved-Buddy3D-Camera)

Source: https://github.com/tlchandler/Improved-Buddy3D-Camera-for-Prusa-CORE-One

This project runs shell scripts from SD card (using the `/mnt/sdcard/lp_app.sh` override mechanism) and then launches `lp_app` normally. Key confirmations and new details:

### 22.1 Confirmed Config Details

- **Config path:** `/userdata/xhr_config.ini` (NOT `/data/` — our earlier assumption was wrong based on binary strings using `/data/xhr_config.ini` which is a symlink or alternate path)
- **Token storage:** Both in `xhr_config.ini` as `token=` AND in `/userdata/xhr_http_token.conf` as raw content
- **Token source:** Obtained from "PrusaConnect app > Camera > Token" (user-visible in Prusa Connect web UI)
- **WiFi password encoding:** base64 (uuencode -m) in `xhr_config.ini`'s `pwd=` field
- **Upload interval:** In **milliseconds** (default 10000ms = 10 seconds), not seconds
- **RTSP mode values:** 0=OFF, 2=ON (not 0/1 as assumed)
- **IR mode values:** 0=day, 1=auto, 2=night (AT command uses 1/2/3 but config uses 0/1/2)
- **Video quality:** Integer range 1-10 (default 6), not just FHD/HD/SD enum
- **Factory backup:** `/userdata/xhr_config.ini.factory`

### 22.2 Cloud Endpoints Confirmed

Blocked hosts when cloud is disabled:
```
connect.prusa3d.com
camera-signaling.prusa3d.com
timezone.prusa3d.com
prusa3d.pool.ntp.org
connect-ota.prusa3d.com (separate OTA toggle)
```

### 22.3 Snapshot Capture (Hardware Access)

The `snapshot_grabber.c` source shows how to capture frames:
- Uses Rockchip MPI: `rk_mpi_vi.h` (Video Input channel 0)
- Grabs NV12 raw frame from VI pipe 0, channel 0
- Converts to JPEG via libjpeg-turbo (quality 95)
- VI channel is SHARED with lp_app (owned by it)
- No VENC channel needed (only 2 exist, both used by lp_app)
- Timeout: 3000ms for frame grab

### 22.4 Print Timelapse (Printer → Camera Protocol)

The Core One printer can stream metrics via UDP:
- G-code: `M334 <camera_ip> 8514 13514` — set metrics destination
- G-code: `M331 is_printing` — enable print state metric
- G-code: `M331 pos_z` — enable Z-position metric
- Port 8514: metrics data (UDP)
- Port 13514: logs (UDP)
- `is_printing` sends 1/0 every ~5 seconds
- `pos_z` sends current height every ~11ms

### 22.5 Hardware Notes

- CPU serial accessible: `grep Serial /proc/cpuinfo`
- Only ~7 MB free RAM at runtime (confirmed)
- FAT32 required on SD card (no exFAT/NTFS support)
- No execute bit on FAT32: binaries must be copied to `/tmp` and chmod'd
- BusyBox 1.27.2 (very old, limited shell features)
- Camera app binary is at `/oem/usr/sbin/lp_app` (confirmed path)

---

## 11. HTTP API

### 11.1 Headers (All Requests)

| Header | Value |
|--------|-------|
| `User-Agent` | `Buddy3D Camera` |
| `X-Camera-Token` | Token from config |
| `X-Camera-Fingerprint` | MD5 fingerprint |
| `X-Camera-FW-Version` | `3.1.5` |

### 11.2 Snapshot Upload

```
PUT /c/snapshot
Host: <image-server>
Content-Type: image/jpg
Expect: 100-continue
X-Camera-Token: <token>
X-Camera-Fingerprint: <fingerprint>
X-Camera-FW-Version: 3.1.5
User-Agent: Buddy3D Camera

<JPEG binary data>
```

Server can respond with:
- `"Upload image BLOCKED by server!"` — throttled
- Success (implied 200)

### 11.3 Camera Info Upload

```
PUT /c/info
Host: <image-server>
Content-Type: application/json
X-Camera-Token: <token>
X-Camera-Fingerprint: <fingerprint>
X-Camera-FW-Version: 3.1.5
User-Agent: Buddy3D Camera

{
  "model": "Niceboy",
  "manufacturer": "...",
  "firmware": "3.1.5",
  "trigger_scheme": "THIRTY_SEC",
  "network_info": { "wifi_mac": "...", "wifi_ipv4": "...", "wifi_ssid": "..." },
  "available_resolutions": [{"width": 1920, "height": 1080}, ...]
}
```

### 11.4 OTA Check

```
GET /api/niceboy/v1/camera
Host: connect-ota.prusa3d.com
X-Camera-Token: <token>
X-Camera-Fingerprint: <fingerprint>
X-Camera-FW-Version: 3.1.5
User-Agent: Buddy3D Camera

Response: {"last_version": "...", "sha1sum": "...", "last_release_type": "..."}
```

Redirect handling: `"redirect host: %s path: %s"`

---

## 12. WebRTC Protocol

### 12.1 Stack

- **ICE:** libjuice (lightweight, not libnice)
- **DTLS:** OpenSSL
- **SRTP:** libsrtp (via libdatachannel)
- **Signaling:** Custom binary over Socket.IO
- **Library:** libdatachannel (built from `/home/miro/webrtc/webrtc-example/libdatachannel_orig/`)

### 12.2 Signaling Message Format

Parsed from log string:
```
"Client type: %d, Msg type: %d, ID len: %d, SDP len: %d, 
 Transport policy: %d, TTL: %d, VideoCfg: %d, Plan: %d, 
 Quality: %s, FPS: %d, TTL: %d, Scope: %d"
```

Binary layout (incoming from server):
```c
struct WebRtcSignalingMsg {
    uint8_t  client_type;       // client type identifier
    uint8_t  msg_type;          // 0=offer, 1=answer, 2=candidate (speculation)
    // id_len + id string (request/client ID)
    // sdp_len + sdp string
    uint8_t  transport_policy;  // 0=ALL/DEFAULT, 1=RELAY
    uint32_t ttl;               // connection time-to-live
    uint8_t  video_cfg;         // video config present flag
    uint8_t  plan;              // reserved/unused
    // If video_cfg:
    //   quality string ("FHD"/"HD"/"SD")
    //   fps (uint8)
    //   ttl2 (uint32)
    //   scope (uint8)
};
```

Camera response:
```
"Starting to send WebRTC message. RequestID: %s, Type: %s, SDP: %s"
```
Types sent: `offer`, `answer`, `candidate`

### 12.3 SDP Parameters

**Video:**
```
m=video 0 RTP/AVP <pt>
a=rtpmap:<pt> H264/90000
a=fmtp:<pt> packetization-mode=1;sprop-parameter-sets=<sps>,<pps>
```

Profile: `42e01f` (Constrained Baseline, Level 3.1)  
Full fmtp: `profile-level-id=42e01f;packetization-mode=1;level-asymmetry-allowed=1`

**Audio (AAC):**
```
m=audio 0 RTP/AVP <pt>
a=rtpmap:<pt> MPEG4-GENERIC/48000/2
a=fmtp:<pt> profile-level-id=1;mode=AAC-hbr;sizelength=13;indexlength=3;indexdeltalength=3;config=<hex>
```

Also supported: G.726, PCMA, PCMU

### 12.4 ICE Configuration

- Uses trickle ICE by default: `"WebRTC full SDP send is disabled. Using trickle ICE/candidates"`
- Transport policies: ALL (default), RELAY
- No ICE-TCP support: `"ICE-TCP is not supported with libjuice"`
- If no ICE servers received from signaling: `"No ICE servers received, using default configuration"`

### 12.5 Scoped Video Quality

Server can limit video quality per-client via the WebRTC signaling message:
```
"SCOPED video cfg: video=%d, fps=%d, prio=%d, found=%d"
"MATCH! Client %s applying scoped video config. S:%s - C:%s, APPLYING LIMITATION"
"Changing video resolution to %s due to SCOPED usage"
```

When TURN relay is active:
```
"TURN client ONLINE - WebRTC is active, video quality change is not allowed"
"WebRTC TURN client is set, getting video mode camera channel H264"
```

### 12.6 Connection Type Reporting

After WebRTC connection established, camera reports via `webrtc_connection_info`:
```
"WebRTC connection info received for client %s: local=%d, remote=%d"
"Sending connection type event for client %s: %d <-> %d"
```

---

## 13. RTSP Server

- Path: `rtsp://<ip>/live`
- Codec: H.264 (via `H264CameraSource` class)
- Port: dynamic (`"rtsp server demo starting on port %d"`)
- Modes: AUTO, ON, OFF, TRIGGER
- Client tracking: `"RTSP client connected. clients: %d"` / `"RTSP client disconnected. clients: %d"`
- RTP over UDP with port negotiation
- H.265 support present in code but likely unused

---

## 14. Video Pipeline

### 14.1 Resolutions

| Name | Width | Height |
|------|-------|--------|
| FHD | 1920 | 1080 |
| HD | 1280 | 720 |
| SD | 640 | 480 |

### 14.2 Encoder

- Rockchip MPP VENC: `"venc_init %d width: %d height: %d, type: %d"`
- Dynamic resolution change: `"change vi %d resolution success to %d*%d"`
- Channel attribute update: `"RK_MPI_VENC_SetChnAttr %d x %d success"`
- Config stored in `/mnt/sdcard/video_cfg` and `config.video_quality`

### 14.3 Video Quality Change

- `"Changing video quality to FHD/HD/SD"`
- `"Save video quality FHD/HD/SD to config file: %d"`
- `"Video quality is already %d = %d, no change needed"`
- Blocked during TURN: `"TURN client ONLINE - WebRTC is active, video quality change is not allowed"`

---

## 15. AT Command Interface

Serial debug interface (disabled with `--noshell`):

| Command | Description |
|---------|-------------|
| `AT+CLAC` | List all commands |
| `AT+TOKEN=<token>` | Set auth token |
| `AT+VERSION?` | Query firmware version |
| `AT+STATUS` | Get device status |
| `AT+RTSP=<0\|1>` | RTSP off/on |
| `AT+WEBRTC=<0\|1>` | WebRTC off/on |
| `AT+IR=<1\|2\|3>` | IR mode: auto/day/night |
| `AT+REBOOT` | Reboot device |
| `AT+UPGRADE` | Start OTA upgrade |
| `AT+WIFISSID=<ssid>` | Set WiFi SSID |
| `AT+WIFIPWD=<password>` | Set WiFi password |
| `AT+WIFICONNECT` | Connect to WiFi |
| `AT+PHOTOINT=<seconds>` | Set snapshot interval |
| `AT+TAKEPHOTO` | Take snapshot now |

---

## 16. Image Upload Service

### 16.1 Flow

```
1. takeSnapshot() — capture JPEG from encoder
2. PUT /c/snapshot with headers
3. Server may respond "Upload image BLOCKED by server!" (throttled)
4. Interval configurable: config.snapshot_upload_interval
5. Disable/enable: disable_snapshot_upload / enable_snapshot_upload
```

### 16.2 Timelapse

- Photos stored: `/mnt/sdcard/timelapse/<folder>/`
- CSV index: `.timelapse_videos.csv`
- Video generation: in-camera, reports progress via `SendClientTriggerTimelapseVideoMakeStatus`
- File list: `SendTimelapseFileList` (fragmented if too large)
- Subfolder rotation on max photo count exceeded

---

## 17. OTA Update

- Check endpoint: `GET /api/niceboy/v1/camera`
- Response fields: `last_version`, `sha1sum`, `last_release_type`
- SD card package: `cus_update_ota.tar` (custom) or `fac_update_ota.tar` (factory)
- On-device: `/userdata/update_ota.tar`
- Process reported via `SendClientTriggerUpgradeProcess`
- Daytime upgrade interval enforcement: `"Within/Outside daytime upgrade interval"`
- Success sound: `/oem/usr/etc/upgrade_successful.wav`

---

## 18. WiFi

### 18.1 Station Mode
- WPA Supplicant (new version): `wpa_supplicant_new`
- WPA3 (SAE) support: `"key_mgmt=WPA-PSK SAE"`, `"ieee80211w=1"`
- Hidden network support: `"scan_ssid=1"`
- Driver: `nl80211`

### 18.2 AP Mode
- Hostapd with WPA2/WPA3
- IP: 192.168.42.1
- DHCP: 192.168.42.20 - 192.168.42.254
- Channel: configurable (1-13)
- Used for initial pairing/setup

### 18.3 Module: RTL8188FU
- USB WiFi dongle
- 802.11n 2.4GHz only
- Driver: `8188fu.ko`

---

## 19. Audio

- Playback: `simple_ao` utility with volume control
- WAV files in `/oem/usr/etc/`:
  - `upgrade_successful.wav`, `upgrading.wav`
  - `wifi_success.wav`, `wifi_failed.wav`
  - `pairing_successful.wav`, `pairing_error.wav`
  - `start_scanning.wav`, `stop_scanning.wav`
  - `video_quality_fhd.wav`, `video_quality_hd.wav`, `video_quality_sd.wav`
  - `rtsp_enable.wav`, `rtsp_disable.wav`
  - `webrtc_enable.wav`, `webrtc_disable.wav`
  - `day_mode.wav`, `night_mode.wav`, `auto_night_mode.wav`
  - `factory_reset.wav`
  - `service_enabled.wav`, `service_disabled.wav`
  - `volume_changed.wav`, `config.wav`
  - `invalid_qr_code.wav`, `application_exit.wav`
  - `fw_update.wav`

---

## 20. Shared Libraries

| Library | Purpose |
|---------|---------|
| `libcrypto.so.1.1` | OpenSSL crypto |
| `libssl.so.1.1` | OpenSSL TLS |
| `librga.so` | Rockchip 2D graphics |
| `librkaiq.so` | Rockchip auto image quality (ISP) |
| `librkaudio_detect.so` | Audio detection |
| `librknnmrt.so` | RKNN neural network runtime |
| `librksysutils.so` | Rockchip system utilities |
| `librockchip_mpp.so.0` | Media processing platform |
| `librockit_tiny.so` | Lightweight media pipeline |
| `librockit.so` | Full media pipeline |
| `librockiva.so` | Rockchip IVA (intelligent video) |
| `librve.so` | Rockchip video enhancement |
| `libsmartIr.so` | Smart IR control |
| `libZXing.so.1` | ZXing barcode/QR reader |
| `libaec_bf_process.so` | Audio echo cancellation |
| `libivs.so` | Intelligent video surveillance |
| `libnl-3.so.200` | Netlink library |
| `libnl-genl-3.so.200` | Generic netlink |
| `libnl-route-3.so.200` | Routing netlink |
