# Buddy3D 3.1.6 implementation gap tracker

Behavioral differences between the fully decompiled Buddy3D Camera firmware `3.1.6`
and the Raspberry Pi impersonator in [`pi-impersonator/`](../pi-impersonator/).

This document tracks only behavior, state, settings, and parameters visible to Prusa Connect
or affecting a Connect-requested operation. Rockchip-specific implementation details are out of
scope unless they change the wire behavior. Firmware `3.1.6` has no observed cloud-protocol
change from `3.1.5`; the gaps below are implementation gaps, not newly introduced 3.1.6
requirements. **[confirmed]**

## Status legend

| Marker | Meaning |
|---|---|
| `[ ]` | Open |
| `[~]` | Partially implemented or implemented but not verified |
| `[x]` | Matched and verified |
| `[-]` | Deliberately not implemented; wire behavior must still be truthful |
| `BLOCKED` | Cannot be exercised end-to-end until an external prerequisite is resolved |

Priority describes likely Prusa Connect impact:

- **P0** — prevents or is likely to prevent a core Connect operation.
- **P1** — produces incorrect control/state behavior visible to Connect.
- **P2** — observable compatibility or parameter difference with limited core impact.
- **P3** — cosmetic, diagnostic, or defensive parity.

Evidence labels follow the rest of this repository: **[confirmed]** means directly traced in
firmware, observed live, or both; **[assumption]** identifies the narrowest remaining inference.

## Coding-agent workflow

This file is the implementation work queue. `CLAUDE.md` defines the repository-wide rules; the
following is the completion contract for each `GAP-*` item:

1. Name the gap before editing and read its cross-reference row plus every cited decompiled range.
2. Check the ambiguity table. If a required descriptor, capture, hardware observation, or owner
   policy is missing, recover/request it and leave the gap open rather than guessing.
3. Implement the documented firmware behavior and parameters, using one shared runtime state for
   values published through multiple surfaces. Keep Pi-specific mechanisms behind that behavior.
4. Add the acceptance tests named by the gap. Wire changes require decoded or golden byte fixtures;
   state changes require transition tests, including failure branches.
5. Run:

   ```bash
   python3 -m unittest discover -s tests -v
   python3 -m compileall -q pi-impersonator tests
   ```

6. Update the checkbox and append the implementation commit, test names, live-verification date
   when applicable, and final evidence label. Update `protocol.md` and `status.md` in the same
   change if externally visible behavior or a prior claim changed.

Repository work is local-only by default. Do not deploy, mutate Connect state, rotate a token,
reboot a device, or flash firmware unless the user separately authorizes that live operation.
Items offering “implement or stop advertising” are owner decisions, not coding-agent discretion.

## Executive status

| Surface | State | Summary |
|---|---|---|
| Enrollment/token | Matched | Token is opaque Connect input; no firmware token-generation algorithm exists. |
| Fingerprint derivation | Working; live-verified | Precedence: an explicit `config.ini` `[identity] fingerprint` (the token-bound value) wins, else firmware-style MAC derivation, else a persisted fallback seed. Live-verified 2026-09-18: `/c/info` 200 (`origin='OTHER', registered=True`), snapshots 200. Migrating to the MAC-derived value still requires a fresh token. |
| `/c/info` | Core schema matched | Initial upload succeeds; periodic refresh (`info_service_loop`) and dynamic values (name/quality/network) are implemented (GAP-INFO-01/02, live 200s). |
| Snapshot upload | Working (live) | Endpoint and identity headers match; the interval is live-applied (tag3.5), enable/disable triggers work, and capture quality + monotonic scheduling are implemented (GAP-SNAPSHOT-01..04); the concurrent-stream half of GAP-SNAPSHOT-04 remains open. |
| Socket.IO authentication | Working, stable (live) | Auth field order is `(token, fingerprint)` and the success ACK is `0` (`1` = not authorized, `2` = error joining session). **Superseded:** the "server closes the WebSocket in the same tick (`Server sent close packet data 0`)" behaviour was the unsolicited post-auth burst (`send_sio_info` + `status` + `protobuf_version` + `features`); removing it (`ba48dc8`, live-verified 2026-09-20) gave a stable session (0 `CameraIsNotSessionMemberError`, one connection). The client still supervises reconnection with a fresh client per attempt (`signaling.supervise`, `02918b8`). Residual long-run stability is unproven, but no server-side close has been observed since the fix. |
| Initial metadata messages | Matched (live) | Core envelopes work; dynamic status values and request-id correlation are implemented (GAP-STATUS-01/02). |
| Trigger handling | Implemented (policy actions declined) | Recovered descriptor `0x3f6f14` decodes each trigger and dispatches only the requested action; reboot is rate-limited and wired (GAP-DEVICE-01); timelapse enable/disable/make/file-list are implemented (GAP-TIMELAPSE-01); OTA returns an explicit truthful decline (GAP-OTA-01). Only per-action `client_trigger` result codes remain unimplemented. |
| Configuration handling | Mostly wired | Nested protobuf `0x3f73a4` via `FUN_000a7940`: video quality, `set_timelaps_interval`, `light_control`, `set_snapshot_upload_interval` (10..600), camera name, and RTSP/WebRTC mode are applied and persisted; only the `tag3.11/12` RTSP candidate mapping remains unmapped. |
| RTSP | Partial | Local stream works; configuration-form mode changes are applied and persisted (GAP-RTSP-02). The firmware default and some command semantics still need a final evidence check (GAP-RTSP-01). |
| WebRTC | Working (live); connection reporting implemented | Connect ICE/TURN settings are consumed, camera offer/answer + trickle candidates work live (app and browser), lifecycle/teardown clears `state.streaming` and resumes snapshots, and the existing encoder is shared. `webrtc_connection_info` (GAP-WEBRTC-06) is implemented from the confirmed sender/encoder/candidate mapping (live verification pending); the inbound policy fields (GAP-WEBRTC-05) remain open with only RE-backed, Connect-observed fields eligible for enforcement. |
| OTA/timelapse/device controls | Implemented | Reboot wired behind a 60 s rate limit; OTA returns an explicit truthful decline; timelapse enable/disable/make/file-list implemented with a persistent `/mnt/sdcard` store (GAP-PERSIST-01). IR/speaker/fan were removed from the advertised features (no hardware) and controls never fake success; `MicroSd` **is** advertised (emulated SD). |

## How to use the decompiled firmware evidence

The complete 3.1.6 export is expected at:

```text
~/firmware-analysis/decompiled-3.1.6-full/functions/
```

References below use `file:line` from that export. Function names are still Ghidra-generated, so
the VMA in the filename is the stable identifier. The line numbers refer to the checked export from
2026-09-18; if it is regenerated, search for the VMA/function name and the shown branch/constants.

Do not implement a field or enum from an older prose note when it conflicts with this direct 3.1.6
control flow. In particular, the direct quality mapping was rechecked while preparing this tracker
and corrects a stale mapping in older notes.

### Decompiled source index

| Evidence ID | Firmware behavior | 3.1.6 decompiled source |
|---|---|---|
| `FW-ID-MAC` | Read interface MAC via ioctl and format uppercase colon-separated text | `00097e78__FUN_00097e78.c:23-54` |
| `FW-ID-SEED` | Request `wlan0` identity; random ten-character fallback | `00096cd8__FUN_00096cd8.c:17-76` |
| `FW-ID-MD5` | MD5 input and lowercase 32-character hex output | `00097a4c__FUN_00097a4c.c:24-49` |
| `FW-INFO-BUILD` | Build `/c/info`, including current resolution/options/features and HTTP request | `00062d74__FUN_00062d74.c:64-329` |
| `FW-INFO-LOOP` | Dirty flag, retry countdown, one-second service loop | `00063bfc__FUN_00063bfc.c:23-94` |
| `FW-SNAPSHOT` | Capture JPEG, derive fingerprint, construct/upload HTTP request | `0005f42c__FUN_0005f42c.c:31-128` |
| `FW-CONFIG` | Parse and dispatch QR/configuration fields | `0006cf34__FUN_0006cf34.c:49-310` |
| `FW-QUALITY-DIRECT` | Raw change-quality event and optional persistence | `00072f08__FUN_00072f08.c:16-115` |
| `FW-QUALITY-DIMS` | Internal quality value to dimensions | `0007d7c4__FUN_0007d7c4.c:10-28` |
| `FW-QUALITY-PB` | Protobuf quality enum to internal raw value | `000a76c8__FUN_000a76c8.c:16-79` |
| `FW-QUALITY-STRING` | Protobuf quality enum to SD/HD/FHD string | `000a11f4__FUN_000a11f4.c:12-25` |
| `FW-AUTH` | Build and emit two-field camera authentication | `000a3058__FUN_000a3058.c:39-118` |
| `FW-PB-VERSION` | Build version message and conditional request correlation | `000a3570__FUN_000a3570.c:43-130` |
| `FW-STATUS` | Build the complete 0x1d0-byte status struct | `000a1394__FUN_000a1394.c:133-499` |
| `FW-FEATURES` | Build/hash/encode supported-feature message | `000a8ed0__FUN_000a8ed0.c:76-282` |
| `FW-TRIGGER-STRINGS` | Trigger action names referenced by the dispatcher | `~/firmware-analysis/cam-3.1.6/lp_app.strings:11249,11534-11535,11896,13934,14127,15086,15152,15309-15310` |
| `FW-TIMELAPSE-SEND` | Build, fragment, encode, and emit timelapse file-list response | `000a1fa8__FUN_000a1fa8.c:52-192` |
| `FW-TIMELAPSE-REGISTER` | Register timelapse-related event callbacks | `000a5208__FUN_000a5208.c:26-558` |
| `FW-WEBRTC-SEND` | Encode and emit outgoing WebRTC answer/candidate | `000a3e90__FUN_000a3e90.c:5-166` |
| `FW-WEBRTC-TYPE` | Numeric message-type conversion | `000b6d9c__FUN_000b6d9c.c:13-46` |
| `FW-WEBRTC-CANDIDATE` | Translate local ICE candidate and call WebRTC sender | `000b75e0__FUN_000b75e0.c:29-60` |
| `FW-WEBRTC-MODE` | Apply enable/disable, start/stop service, persist mode | `000b94ac__FUN_000b94ac.c:16-52` |
| `FW-WEBRTC-GATE` | Reject disabled offers and enqueue enabled peer work | `000b996c__FUN_000b996c.c:39-107` |
| `FW-RTSP-INIT` | Load RTSP mode and register start/stop/mode callbacks | `000b0834__FUN_000b0834.c:24-125` |

### Exact recovered configuration dispatch

`FUN_0006cf34` is not a generic JSON handler. It constructs allowed-value/action tables and calls
the shared dispatcher `FUN_0006c0c8`; string pointers in its literal pool resolve to the following
rules. This is the minimum behavior the typed impersonator dispatcher must reproduce.

The names in the table are not guesses based on local-variable order: they were obtained by
resolving `DAT_0006d9f4..DAT_0006daac` through the ELF literal pool into `.rodata`, then checking
the resulting strings against `lp_app.strings`. The integer action values are the immediate values
written beside those string pointers in `FW-CONFIG`.

| Incoming field | Incoming value | Internal action/value | Direct evidence |
|---|---|---|---|
| `rtsp` | `on` | `set_rtsp_server_mode(2)` | `FW-CONFIG:75-107` |
| `rtsp` | `off` | `set_rtsp_server_mode(1)` | `FW-CONFIG:92-107` |
| `webrtc` | `on` | `set_webrtc_mode(1)`; also forces RTSP disabled through the paired rule | `FW-CONFIG:108-140` |
| `webrtc` | `off` | `set_webrtc_mode(0)` | `FW-CONFIG:125-140` |
| `video_quality` | `sd` | internal raw quality `5` | `FW-CONFIG:141-173` |
| `video_quality` | `hd` | internal raw quality `6` | `FW-CONFIG:141-173` |
| `video_quality` | `fhd` | internal raw quality `7` | `FW-CONFIG:141-173` |
| `start_fw_update` | `start` | firmware-update action | `FW-CONFIG:174-192` |
| `light_control` | `auto` | light mode `1` | `FW-CONFIG:193-228` |
| `light_control` | `night` | light mode `3` | `FW-CONFIG:193-228` |
| `light_control` | `day` | light mode `2` | `FW-CONFIG:193-228` |
| `camera_name` | non-empty string | call camera-name setter; empty value only logs warning | `FW-CONFIG:237-259` |
| `snapshot_interval` | integer `10..600` | call interval setter with seconds | `FW-CONFIG:260-277` |

The leading `code` handling rejects the special values `"42"` and `"66"` before normal dispatch:
`FW-CONFIG:49-74` and `FW-CONFIG:286-300`. The one-second waits after individual actions are visible
at `FW-CONFIG:102-105`, `135-138`, `168-171`, `187-190`, and `229-233`.

The exact on-wire numeric tags for every configuration field are **not yet documented with the same
confidence as the semantic dispatch table**. The implementer must dump the configuration nanopb
descriptor or use a redacted captured payload before replacing the current guessed tags. Do not
infer tags from the order of the table above.

### Exact recovered MAC/fingerprint path

`FUN_00096cd8` constructs the interface-name string `"wlan0"` and passes it to `FUN_00097e78`
(`FW-ID-SEED:17-23`). `FUN_00097e78` then:

1. Opens `socket(AF_INET=2, SOCK_STREAM=1, 0)` (`FW-ID-MAC:23`).
2. Copies at most 15 bytes of the interface name into `ifreq` (`38`).
3. Calls `ioctl(fd, 0x8927, &ifreq)` (`39`); `0x8927` is Linux `SIOCGIFHWADDR`.
4. Formats the six returned bytes with the literal
   `%02X:%02X:%02X:%02X:%02X:%02X` (`46-47`).
5. Returns an empty string on socket/ioctl failure (`24-44`).

Back in `FUN_00096cd8`, an empty MAC causes `FUN_000997f8(..., 10, 1)` to generate the ten-character
fallback (`FW-ID-SEED:25-56`). `FUN_00097a4c` hashes the complete seed and writes exactly 16 digest
bytes as two lowercase hex characters each: the 16-iteration loop and `snprintf(dst, 3, "%02x", b)`
are at `FW-ID-MD5:24-45` (the `%02x` literal is resolved from `DAT_00097b34`).

### Exact recovered video-quality mapping

There are three distinct representations; mixing them caused the existing handler bug:

| Representation | SD | HD | FHD | Evidence |
|---|---:|---:|---:|---|
| Protobuf quality enum | `1` | `2` | `3` | `FW-QUALITY-PB:16-69` |
| Internal/raw event byte | `5` | `6` | `7` | `FW-QUALITY-PB:22-69`, `FW-CONFIG:141-173` |
| Resolution | `640×480` | `1280×720` | `1920×1080` | `FW-QUALITY-DIMS:12-23` |

`FUN_00072f08` receives the raw byte at line 16, no-ops when it already equals the stored value at
lines 22-26, handles raw 6/7/5 at lines 32-109, calls the live resolution changer, and persists only
when `*param_3 != 0` at lines 46-51, 68-73, and 93-98. It updates the in-memory current value only
after a successful change at lines 54-57, 76-78, and 101-103.

Implementation translation must therefore be:

```text
raw byte 5 -> protobuf enum 1 -> SD  -> 640x480
raw byte 6 -> protobuf enum 2 -> HD  -> 1280x720
raw byte 7 -> protobuf enum 3 -> FHD -> 1920x1080
```

### Exact recovered WebRTC mode and offer gate

`FUN_000b94ac` implements `set_webrtc_mode`:

```text
requested = payload field/value byte
if runtime_status(+0x13e) == 0 and requested != 0:
    persist mode 1
    start WebRTC service
elif runtime_status(+0x13e) != 0 and requested == 0:
    persist mode 0
    stop WebRTC service
mode(+0x13d) = requested
persist mode again
```

Direct lines: `FW-WEBRTC-MODE:16-27`, `35-40`, and `49-52`.

`FUN_000b996c` then gates peer creation. At `FW-WEBRTC-GATE:39-48`, it returns failure when both
`mode(+0x13d)` and `runtime_status(+0x13e)` are zero or when the client/request pointer is null.
At lines 54-101 it copies request/client/session/ICE parameters into a 0x5c-byte work item and
queues it at singleton offset `+0x140`. The impersonator should not replace this with an always-on
status claim; mode, runtime state, and peer work are separate concepts.

### Recovered WebRTC message contract

The incoming camera-side message layout recovered from the parser/descriptor is:

```protobuf
message CameraWebRtcInbound {
  string request_id = 1;
  uint32 msg_type = 2;          // 3=offer, 4=candidate
  string client_id = 3;
  string sdp_or_candidate = 4;
  uint32 transport_policy = 5;
  uint32 ttl = 6;
  uint32 video_cfg = 7;
  uint32 plan = 8;
  string quality = 9;
  uint32 fps = 10;
  uint32 scope_or_ttl2 = 11;    // semantic name still needs final descriptor annotation
  IceConfig ice_config = 12;
}
```

Outgoing answer/candidate layout is different and already implemented correctly:

```protobuf
message CameraWebRtcOutbound {
  string request_id = 1;
  uint32 msg_type = 2;          // 2=answer, 4=candidate
  string payload = 3;
}
```

`FW-WEBRTC-SEND:105-107` encodes into a 3000-byte buffer and lines 120-160 emit the binary event.
`FW-WEBRTC-TYPE:23-44` explicitly accepts numeric types `2`, `4`, and `1`; other nonzero values take
the invalid branch. `FW-WEBRTC-CANDIDATE:29-52` extracts the candidate metadata and routes it through
the same sender.

ICE construction is additionally supported by these firmware strings and paths:

- `No ICE servers received, using default configuration`
- `Adding TURN: %s:%d User: %s (Type: %d)`
- transport policies `RELAY`, `ALL`, and default/all
- default STUN servers `stun.l.google.com:19302`, `stun1.l.google.com:3478`, and
  `stun2.l.google.com:5349`

The nested `IceConfig` descriptor is still a required recovery item. Do not invent its numeric
subfield tags from the log format; dump the descriptor before implementing `GAP-WEBRTC-01`.

### Recovered `/c/info` construction and retry behavior

`FUN_00062d74` performs the following in order:

1. Reads token and network identity; aborts the body-build path if either required string is empty
   (`FW-INFO-BUILD:64-88`).
2. Builds `config` fields and current resolution (`FW-INFO-BUILD:89-173`). Width and height are
   `0x780`/`0x438` = `1920`/`1080` in the shown FHD path (`131-143`).
3. Builds one current-resolution object and inserts it into `options.available_resolutions`
   (`174-204`). This is direct evidence for one entry, not the older three-entry assumption.
4. Builds `capabilities` (`205-212`).
5. Calls the feature-list builder, wraps/parses it as JSON, and inserts `features`
   (`213-227`).
6. Serializes the body (`228-231`), derives the fingerprint (`242-251`), creates the HTTP request
   and headers (`249-271`), and sends it.
7. Clears the dirty/retry flag only when the response begins with the success text checked at
   `292-295`; non-success is logged at `314-318`.

The service loop sleeps one second (`FW-INFO-LOOP:33-39`). When the dirty flag at `+0x0d` is set,
it calls `/c/info` when countdown `+0x44` reaches zero, then reloads the countdown to 10 if still
dirty (`52-61`). A separate snapshot timer is maintained at offsets `+0x04/+0x08` and invokes the
snapshot uploader at `70-75`.

The concrete Connect request target is:

```http
PUT /c/info
User-Agent: Buddy3D Camera
Token: <opaque Connect token>
Fingerprint: <lowercase MD5 hex>
Content-Type: application/json
```

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
    "resolution": {"width": 1920, "height": 1080},
    "network_info": {
      "wifi_mac": "AA:BB:CC:DD:EE:FF",
      "wifi_ipv4": "192.0.2.1",
      "wifi_ssid": "<SSID>"
    }
  },
  "options": {
    "available_resolutions": [
      {"width": 1920, "height": 1080}
    ]
  },
  "capabilities": ["trigger_scheme"],
  "features": ["<ordered feature names>"]
}
```

The resolution values above describe the shown FHD state; they must be sourced from current state,
not hardcoded independently in each message.

### Recovered status construction rules

`FUN_000a1394` zeroes the full `0x1d0`-byte nanopb structure before populating it
(`FW-STATUS:133-138`). This means optional-field presence is deliberate; an empty encoded submessage
is not automatically equivalent to an absent firmware field.

Key directly visible state translations:

| Status source | Firmware translation | Evidence |
|---|---|---|
| Unannotated three-state enum from `FUN_00071a4c` | internal `2→3`, `3→1`, `1→2`, else `0`; do not call this video quality until the nested descriptor is recovered | `FW-STATUS:155-168` |
| Boolean/service states | internal `0→2`, `1→1`, else `0` | `FW-STATUS:183-192`, `246-265`, `296-301`, `365-384` |
| Four-state enum | internal `0..3→1..4`, else `0` | `FW-STATUS:193-210` |
| RTSP mode/status/URL | reads two enum getters and builds URL from network identity | `FW-STATUS:331-362` |
| WebRTC mode/status | reads two WebRTC singleton getters and translates each to `2/1/0` | `FW-STATUS:363-385` |
| Current quality wrapper | uses configured getter or actual encoder channel getter | `FW-STATUS:386-399` |
| Conditional correlation field | populated only when `param_1[0x12] != 0`, value from `param_1[0x11]` | `FW-STATUS:413-418` |

The exact nested tag-to-local-variable mapping beyond the already recovered top-level layout must
come from the nanopb descriptor, not stack-variable order. Until a golden fixture exists, keep
`GAP-STATUS-03` open and do not describe inferred nested values as exact.

The recovered top-level status envelope is:

```protobuf
message CameraInfoMessage {
  // field 1 descriptor exists but SendCameraInfoMessage does not populate it
  TimelapseStatus timelapse_status = 2;
  CameraStatus camera_status = 3;
  NetworkInfo network_info = 4;
  ExtendedStatus extended_status = 5;
  // field 6 descriptor exists but is not populated here
  // field 7 descriptor exists but is not populated here
  string token = 8;
  SystemInfo system_info = 9;
  string correlation = 10;       // conditional; context-dependent SID/request value
  VideoQuality video_quality = 11; // nested field 1: 1=SD, 2=HD, 3=FHD
}
```

Presence flags observed in `FW-STATUS` correspond to top-level fields 2, 3, 4, 5, 9, and 11;
token field 8 is assigned from the HTTP configuration getter, and field 10 is conditional. The
impersonator must omit fields that firmware leaves absent rather than encoding arbitrary empty
submessages.

### Recovered outbound identity/metadata messages

These are the implementation targets established jointly by nanopb descriptors, sender assignment
paths, and the prior redacted real-camera capture:

```protobuf
message CameraAuthentication {
  string fingerprint = 1;
  string token = 2;
}

message ProtobufSchemaVersion {
  string token = 1;
  string version = 2;       // literal "4.4"
  string request_id = 3;    // present only for correlated reply
  // field 4 callback remains null / omitted
}

message CameraSupportedFeatures {
  // field 1 omitted
  string token = 2;
  string firmware = 3;      // "3.1.6"
  string hardware = 4;      // selected hardware-version string, e.g. "NB.1.1.0"
  string protocol = 5;      // "4.4"
  string features_json = 6; // bracket-wrapped ordered JSON list
  string features_md5 = 7;  // lowercase MD5 of exact field-6 bytes
  string request_id = 8;    // correlated reply when present
}
```

`FW-AUTH:46-58` obtains the two identity strings and passes the initialized structure to nanopb.
`FW-PB-VERSION:54-68` assigns the token/version values and conditionally sets the request field at
lines 60-65. `FW-FEATURES:106-123` obtains token/identity and constructs the exact bracket-wrapped
feature bytes; line 119 invokes the hash operation over those bytes. Lines 191-207 assign firmware,
hardware/model/protocol/hash sources before nanopb encoding.

The feature payload bytes whose MD5 is sent are exactly the no-space bracketed form:

```json
["SocketCom","UploadInterval","TimelapseEn","TimelapseInterval","TimelapseVideoMake","TimelapseFileList","VideoStream","RtspStream","GetSnapshot","IrMode","SpeakerVolume","WiFi","FwVer","HwVer","CameraName","MicroSd","FwUpdate","CameraReboot","McuTemp","VideoQuality","WebRtc","TurnVideoQualityChange","trigger_scheme","FanControl"]
```

`RotationX` and `RotationZ` are inserted only when motor hardware is detected; they are absent for
the motorless C1 target.

### Recovered timelapse file-list sending behavior

`FUN_000a1fa8` is the actual sender; `FUN_000a5208` primarily registers timelapse callbacks.
The sender:

1. Adds token and optional request correlation (`FW-TIMELAPSE-SEND:63-71`).
2. Retrieves/builds the CSV-derived file-list string and returns failure if empty (`72-84`).
3. Computes whether the complete response fits in 1024 bytes. One fragment is used below `0x401`;
   otherwise fragment count is `(size >> 10) + 1` (`85-96`).
4. Prefixes each fragment with its one-based fragment number and total, takes a substring, and
   assigns it to the response structure (`97-120`).
5. Encodes each response into a `0x400`-byte nanopb buffer (`120-142`), emits it (`145-179`), then
   waits 50 ms before the next fragment (`184-186`).

The exact four-field descriptor annotations are now recovered: field 1 = the
`"<page>;<total>\n<chunk>"` fragment, field 2 = the HTTP token, field 3 = request_id (set only when
present), field 4 = never populated by this sender. An empty list emits no message at all (there is
no zero-entry encoding to send). **[confirmed]**

### Areas where decompilation still does not remove all ambiguity

These are explicit recovery prerequisites, not permission to guess:

| Area | Known exactly | Still required before implementation |
|---|---|---|
| Trigger dispatcher | **Recovered 2026-09-18:** descriptor `0x3f6f14` (13 fields, tags 1–5/8–15) and the `FUN_000a963c` per-field `== 1`/`== 2` dispatch; `trigger.py` implements it. **2026-09-20:** result event is the `client_trigger` EVENT with descriptor `0x3f6f58` (not an ack) | Tag 13 string semantics; multi-field processing order; which `client_trigger` tag carries code/progress/message |
| Configuration | **Resolved 2026-09-19: nested protobuf** (descriptor `0x3f73a4`, dispatcher `FUN_000a7940`); live-mapped `tag8.1` = video quality and top-level field 2 = `set_timelaps_interval` (`FUN_000a7940` logs `"Timelapse interval: %d seconds"`); the nlohmann JSON parser is only the QR/manual-config path. **2026-09-20:** `tag3` descriptor `0x3f6d2c` (11 fields) recovered | `tag3.11/12` are uvarints, not confirmed RTSP; a redacted golden capture |
| Status | Top-level fields, struct size, many getters/translations, request correlation, and now the nested descriptor tables (dumpable) | Semantic tag-to-field annotation for every claimed nested value |
| ICE config | The inbound `webrtc` message is 9 fields with nested submessages (`0x3f7680`, handler `FUN_000a43e0`), **not** the flat 12-field log string; tag4/tag8/tag9 offsets and sizes recovered | Exact name->tag binding for the 7 log names (`[assumption]`) |
| Timelapse list | **Resolved 2026-09-19:** descriptor `0x3f701c` field 1 = `"<page>;<total>\n<chunk>"`, field 2 = HTTP token, field 3 = request_id (optional), field 4 never set; `FUN_000a1fa8` sender; `FUN_000ad7ec` composes `<name>;<status>\n` entries from the `*.avi` scan (`FUN_000ac934`) + `.timelapse_videos.csv` status (`FUN_000ac134`, default `U`) | None for the envelope; an empty list sends no message |
| `client_trigger` | Dedicated 6-field descriptor `0x3f6f58` (strings 1/2/4, uvarints 3/5/6); emitted as a Socket.IO **event**, ack callbacks are the server receipt ack; nine sender wrappers recovered | Which tag carries code vs progress vs message is `[assumption]`; exact payload fixtures |
| RTSP port | Runtime getter and advertised URL path are present | Confirm default value from config image or genuine status capture before changing 8554 |
| WebRTC audio | Codec implementations exist in the binary | Confirm whether current Connect camera offers request/require an audio m-line |
| Signaling session lifecycle (residual long-run question) | **Superseded 2026-09-20:** the observed "server ACKs `camera_authentication` (ACK `0`) then closes the WebSocket in the same tick (`Server sent close packet data 0`)" was caused by the unsolicited post-auth burst (`send_sio_info` + `status` + `protobuf_version` + `features`); removing it (`ba48dc8`, live-verified) gave a stable session (0 `CameraIsNotSessionMemberError`, one connection). The client still supervises reconnection with a fresh client per attempt and exponential backoff (15s→120s), and drops the session on a rejected ACK/exception so the supervisor retries (`signaling.supervise`/`_drop_session`). | Whether the service can still be closed by a server-side eligibility/session policy over a long run is **not proven**. **Ruled out 2026-09-19:** handshake URL (`param_3` is an empty map; only `&t=` which engineio already sends), token `origin` (re-registered via the Buddy3D flow, `/c/info` `origin` went `OTHER`→`WEB`, close persists), post-auth pacing (immediate send and no-ACK batching both close), headers/transport, and stale-session resume. The post-auth-burst fix removes the known cause; any remaining close would need a fresh capture. |

### Gap-to-firmware cross-reference

Use this table to jump from a tracker item to the recovered implementation evidence. A row marked
`descriptor required` intentionally has no invented field map; recovering that descriptor is part of
closing the gap.

| Gap | Primary firmware evidence | Remaining ambiguity, if any |
|---|---|---|
| `GAP-TRIGGER-01` | `FW-TRIGGER-STRINGS`; recovered descriptor `0x3f6f14` in `trigger.py`; `client_trigger` string at `lp_app.strings:10503` | Per-action result subtype (`client_trigger`) required |
| `GAP-CONFIG-01` | `FW-CONFIG` and the exact dispatch table above; `tag3` descriptor `0x3f6d2c` (11 fields) | `tag3.11/12` are uvarints; the "RTSP candidate" label is `[assumption]` |
| `GAP-WEBRTC-01` | WebRTC contract above; `FW-WEBRTC-GATE:54-101` copies ICE/session data | Nested `IceConfig` descriptor required |
| `GAP-WEBRTC-02` | `FW-SNAPSHOT:62-77`; `FW-WEBRTC-GATE:79-101` | Pi sharing architecture is implementation-specific |
| `GAP-WEBRTC-03` | `FW-WEBRTC-GATE`; peer queue at `101`; mode stop path `FW-WEBRTC-MODE:35-40` | Exact peer TTL worker should be traced while implementing |
| `GAP-WEBRTC-04` | `FW-WEBRTC-MODE`, exact pseudocode above | None for enable/disable behavior |
| `GAP-WEBRTC-05` | Descriptor `0x3f7680` (9 fields; handler `FUN_000a43e0`); `FW-WEBRTC-GATE:54-101`; TURN lock `FUN_000a7940`->`FUN_000b5ad4`/`FUN_000b4f90` (flag at singleton+0x278) | Exact name->tag binding for the 7 log names `[assumption]`; only Connect-observed fields may be enforced |
| `GAP-WEBRTC-06` | Sender `FUN_000be3f8`; encoder `FUN_000be050`/`FUN_000bdd3c`; candidate translator `FUN_000b5098`; descriptor `0x3f65c8` | GStreamer selected-pair extraction is version-specific (best-effort, skips if unavailable) |
| `GAP-STATUS-01` | `FW-STATUS`, translation table above | Some nested tag annotations still require fixture |
| `GAP-STATUS-02` | `FW-STATUS:413-418`; `FW-PB-VERSION:60-65` | Initial SID versus requested correlation needs fixture |
| `GAP-SNAPSHOT-01` | `FW-CONFIG:260-277`; `FW-INFO-LOOP:64-94` | Exact timer scaling constant should be named, not guessed |
| `GAP-SNAPSHOT-02` | `FW-TRIGGER-STRINGS:11249,11534`; snapshot service loop `FW-INFO-LOOP:64-94`; recovered descriptor `0x3f6f14` tags 4/5 | Live cadence verification |
| `GAP-INFO-01` | `FW-INFO-BUILD`; `FW-INFO-LOOP:42-61` | None for retry/dirty behavior |
| `GAP-CAP-01` | `FW-FEATURES`; feature builder call `FW-INFO-BUILD:213-227` | Capability-removal effect needs live Connect test |
| `GAP-QUALITY-01` | `FW-QUALITY-PB`, `FW-QUALITY-DIRECT`, `FW-QUALITY-DIMS` | None; mapping is exact |
| `GAP-QUALITY-02` | Live-change branches `FW-QUALITY-DIRECT:32-109`; persistence flag branches at `46-51,68-73,93-98` | Confirm which indirectly registered Socket.IO callback supplies flag `0` versus `1` |
| `GAP-QUALITY-03` | Current-value reads `FW-QUALITY-DIRECT:22-31`; status getter `FW-STATUS:386-399` | None for shared-state requirement |
| `GAP-RTSP-01` | `FW-RTSP-INIT:24-42`; status URL `FW-STATUS:331-362` | Default port still requires config/live evidence |
| `GAP-RTSP-02` | `FW-RTSP-INIT`; direct/config action table in `FW-CONFIG` | Callback bodies are split by Ghidra and should be retyped |
| `GAP-SNAPSHOT-03` | Snapshot capture path `FW-SNAPSHOT:62-77`; libjpeg quality 95 trace in `journal/findings.md:893-899` | Reconfirm quality argument if snapshot backend is replaced |
| `GAP-SNAPSHOT-04` | Snapshot timer `FW-INFO-LOOP:64-94`; capture `FW-SNAPSHOT:62-77` | Concurrent Rockchip channels do not prescribe Pi architecture |
| `GAP-HTTP-01` | HTTP request/header build `FW-SNAPSHOT:99-116`; `Expect` string in `lp_app.strings:3993` | Exact HTTP-library automatic behavior may differ |
| `GAP-HTTP-02` | Response/service loop `FW-INFO-BUILD:292-318`, `FW-INFO-LOOP`; blocked-upload string `lp_app.strings:8304` | Exact retry policy for every status code needs call-path trace |
| `GAP-INFO-02` | Dynamic getters throughout `FW-INFO-BUILD:89-227` | Nested JSON key names are already fixed in `protocol.md` |
| `GAP-AUTH-01` | `FW-AUTH`; auth event string `lp_app.strings:10355` | ACK callback branch needs explicit decompile annotation |
| `GAP-CONTROL-01` | `FW-CONFIG:237-259`; status name getter in `FW-STATUS:235-244` | Persistence backend is Pi-specific |
| `GAP-OTA-01` | `FW-CONFIG:174-192`; `start_fw_update` at `lp_app.strings:15084`; OTA endpoint/response keys in `journal/findings.md:1173-1181` | Full OTA state machine still needs focused call-path annotation |
| `GAP-TIMELAPSE-01` | `FW-TIMELAPSE-SEND`, `FW-TIMELAPSE-REGISTER`; action strings `lp_app.strings:15309-15310` | None for the list envelope; make-video/list not yet exercised by the app |
| `GAP-DEVICE-01` | `reboot_device` at `lp_app.strings:14127`; trigger dispatcher recovery item | Trigger enum/result response required |
| `GAP-DEVICE-02` | `camera_status` descriptor `0x3f6cd0` (6 fields); `FW-CONFIG:193-228`; advertised list from `FW-FEATURES` | Tag->hardware binding needs sender `FUN_000a1394`; hardware absence is intentional |
| `GAP-WEBRTC-07` | Codec/SDP strings summarized in `journal/findings.md:1040-1057` | Whether Connect requests audio needs a current offer |
| `GAP-IDENTITY-01` | `FW-ID-MAC`, `FW-ID-SEED`, `FW-ID-MD5` | None for algorithm; persistence policy is Pi-specific |
| `GAP-IDENTITY-02` | `FUN_00096cd8` (generateFingerPrint), `FW-ID-MAC`/`FW-ID-SEED`/`FW-ID-MD5`; `/c/info` use at `FW-INFO-BUILD:242-251` | **Resolved 2026-09-20:** already `wlan0`-MAC-derived with the token bound (live ACK `0`, `/c/info` 200); the runbook in `next-steps.md` applies only if the fingerprint is ever changed |
| `GAP-IDENTITY-03` | MAC source from `FUN_00096cd8`/`FW-ID-MAC`; model table in `firmware-3.1.6.md` | **Closed 2026-09-20:** firmware does not inspect the OUI; the Pi-vendor OUI is a documented per-device difference and the registry-gate theory is superseded |
| `GAP-STATUS-03` | Entire `FW-STATUS`; zero-init/presence rule at `133-138` | Nested descriptor fixture required |
| `GAP-STATUS-04` | Time/status getter block `FW-STATUS:271-301` | Exact reported string needs getter rename/fixture |
| `GAP-NETWORK-01` | Network getter block `FW-STATUS:214-234`; `/c/info` network getters `FW-INFO-BUILD:145-173` | Signal conversion helper needs focused trace |
| `GAP-HTTP-03` | Long-running service loop `FW-INFO-LOOP`; HTTP build/send paths in `FW-INFO-BUILD` and `FW-SNAPSHOT` | Connection reuse is partly inside the bundled HTTP library |
| `GAP-SIO-01` | `client_trigger` at `lp_app.strings:10503`; sender variants listed in `journal/findings.md:376-381` | Six-field descriptor subtype enums require annotation |

## P0 — core operation gaps

### GAP-TRIGGER-01 — Decode and dispatch the requested trigger

- [~] **P0 · Implemented (9291968): recovered descriptor 0x3f6f14; policy actions (fw_update/timelapse) and client_trigger results still open**
- **RE 2026-09-20 (Wave 1) [confirmed]:** the per-action result message is the 6-field
  `client_trigger` descriptor **`0x3f6f58`** (strings 1/2/4, uvarints 3/5/6). The **emit mechanism
  is a Socket.IO EVENT (`client_trigger`), not an ack** — the ack callbacks are the server's
  receipt ack. Nine sender wrappers were recovered (error codes: SD not mounted / SD RO / low
  space / timelapse SD not mounted / RTSP start failed; FW upgrade progress 1..6;
  timelapse-video-make status 1 IN_PROGRESS / 2 FINISHED), but **which tag carries code vs
  progress vs message is unresolved** `[assumption]` — do not implement a wire format yet.
- **Firmware behavior:** decodes the trigger type and performs only the requested action: get
  features, get status, get protocol information, get snapshot, enable/disable snapshot upload,
  enable/disable timelapse, make timelapse video, list timelapse files, reboot, start firmware
  update, or start/stop RTSP. **[confirmed]**
- **Current behavior:** extracts a probable request ID, sends `status`, `protobuf_version`, and
  `features`, then uploads a snapshot for every trigger.
- **Connect impact:** incorrect responses and side effects; settings and control actions may appear
  accepted while doing nothing.
- **Implementation:** recover the exact trigger message descriptor and enum values; introduce a
  typed dispatcher; invoke only the requested action; emit the corresponding firmware-style
  result/error message.
- **Acceptance:** fixture tests for every recovered trigger prove that only its intended action is
  called and the correct response event/payload is emitted.
- **Code:** [`main.py`](../pi-impersonator/main.py#L234-L250),
  [`trigger.py`](../pi-impersonator/trigger.py)
- **Implementation (staged, commit pending):** [`trigger.py`](../pi-impersonator/trigger.py)
  decodes the recovered descriptor `0x3f6f14` (`decode_trigger`, returning a tag-keyed
  `TriggerMessage` with a normalized tag-11 `request_id`) and produces an ordered action plan
  (`trigger_actions`) that fires only the exact documented `(tag, value)` pairs. `main.py`'s
  trigger handler now performs only the planned actions: `status`/`features`/`protocol_info`
  (correlated on tag 11), immediate `snapshot`, snapshot upload enable/disable, and RTSP
  start/stop through `rtsp_control.apply_mode` with persistence. The policy actions `fw_update`,
  `reboot`, and `timelapse_enable/disable/make/file_list` are recognized and logged as not
  implemented; they no longer cause an unrelated response or a fake success. Tag 13 is decoded
  and logged only. Tests: `test_pi_trigger.py`. Remaining: per-action `client_trigger`
  result/error codes (GAP-SIO-01) and the policy decisions for OTA/reboot/timelapse
  (GAP-OTA-01/GAP-DEVICE-01/GAP-TIMELAPSE-01). Trigger result acks are not sent because the
  installed `python-socketio` trigger handler signature carries no ack callback.

### GAP-CONFIG-01 — Replace guessed configuration decoding with the recovered schema

- [~] **P0 · Corrected 2026-09-19: the SIO `configuration` event is a nested protobuf, NOT JSON; video_quality mapped, others in progress**
- **Correction (live-verified):** the earlier "JSON" conclusion was wrong for the Socket.IO path. Live Connect setting changes arrive as a **nested protobuf** (descriptor `0x3f73a4`, 9 fields; handler `FUN_000a7940` decodes it and dispatches by name — `'video_quality'`, `'light_control'`, `'motor_controll'`, `'set_snapshot_upload_interval'`, `'set_timelaps_interval'`). `FUN_000a89e0` is **not** a function; Ghidra had not defined the real dispatcher, which was recovered by forcing a function at its ARM prologue. The JSON parser (`nlohmann`, `FUN_0006f9dc`) is only the QR/manual-config path. The JSON handler rejected every live setting change ("not valid JSON").
- **Mapped (live):** `tag8.1` = video quality enum (1=SD/2=HD/3=FHD) — implemented in `main.py` via `handle_quality`. Verified: the encoder actually changes tier; the WebRTC viewer must stop/start the stream to pick up the new resolution (the peer connection is built with the resolution at offer time) — acceptable.
- **Mapped (live 2026-09-19):** `tag3.5` = `set_snapshot_upload_interval`. Connect's cameras page **"Displayed Frame Update Interval"** slider sends `configuration {3: {5: <seconds>}}`; the Pi previously logged and ignored it. Direct evidence: `FUN_000a7940` reads tag3.5, logs `"Upload interval: %d seconds"` / `"Setting upload interval: %d seconds"`, rejects outside **10..600** (`"Invalid upload interval: %d"`), and dispatches the name `set_snapshot_upload_interval`. Wired to `state.set_snapshot_interval`; **verified live**: slider → `{3: {5: 20}}` → `Config: snapshot_upload_interval (tag3.5) → 20s` and the snapshot cadence settled to exactly 20 s. Note: the runtime interval is not persisted (reverts to `config.ini` on reboot) — persistence is a follow-up.
- **In progress:** `tag3` carries the remaining settings (subfields 4/11/12 observed live).
- **RE 2026-09-20 (Wave 1, direct 3.1.6 decompiler/ELF) [confirmed]:** the `tag3` submessage
  descriptor is **`0x3f6d2c`** (11 fields): 1 submsg @0x04, 2 uvarint @0x0c, 3 submsg @0x14,
  **4 uvarint @0x1c = `light_control`**, **5 uvarint @0x20 = `set_snapshot_upload_interval`
  (10..600)**, 7 uvarint @0x24, 8 uvarint @0x28, 9 uvarint @0x2c, **10 string @0x30 =
  `set_printing_job_name`**, **11 uvarint @0x38**, **12 uvarint @0x3c**. Because tags 11/12 are
  **uvarints**, the tracker's earlier "RTSP candidate" label for them is **unconfirmed**
  `[assumption]`; do not implement an RTSP mapping from them without a genuine payload. The
  dispatch-name pool also includes `set_volume` (5..100) + `play_voice`, `set_camera_name`,
  `set_rtsp_server_mode` (AUTO/ON/OFF), `set_webrtc_mode`, `set_timelaps_interval`,
  `set_timelaps_video_fps`, and the quality+TURN lock.
- **Recovered (2026-09-19, direct decompile):** the config dispatcher is `FUN_000a7940` (`0xa7940`-`0xa885b`; Ghidra had not defined it as a function — recovered by forcing a function at the ARM prologue `push {r4-r9,sl,fp,lr}` and decompiling). It decodes descriptor `0x3f73a4` and dispatches by name: **top-level field 2 (struct offset `0x14`) = `set_timelaps_interval`** (dispatcher logs `"Timelapse interval: %d seconds"`), field 8 = video quality (`Video quality: %d`), the field-9 region = `set_timelaps_video_fps` (`"Timelapse video FPS: %d"`), and field 5 = `set_webrtc_mode` / `set_rtsp_server_mode`. Live Connect sends `configuration {2: <seconds>}` when the timelapse interval changes; `main.py` now maps it to `state.set_timelapse_interval` (1..3600).
- **Implementation:** `main.py`'s `configuration` handler now parses JSON only (the generic protobuf fallback and all numeric-tag lookups are removed) and dispatches the recovered table exactly: `rtsp` on/off, `webrtc` on/off (with the paired `webrtc on → RTSP disabled` rule), `video_quality` sd/hd/fhd, `light_control` auto/night/day, `camera_name`, `snapshot_interval` 10..600, `start_fw_update` start, and the leading `code` rejecting `"42"`/`"66"`.
- **Still open:** a redacted golden JSON capture from a genuine 3.1.6 device to pin exact value casing; `start_fw_update` remains a truthful no-op (owner decision).
- **Firmware behavior:** decodes a protobuf configuration message and dispatches named settings
  including `rtsp`, `webrtc`, `video_quality`, `start_fw_update`, `light_control`, `camera_name`,
  and `snapshot_interval`. **[confirmed]**
- **Current behavior (implemented):** decodes the nested protobuf `0x3f73a4` and dispatches the
  recovered table: top-level field 2 (`set_timelaps_interval`), `tag8.1` video quality,
  `tag3.4` `light_control` (truthful unavailable — no IR hardware), `tag3.5`
  `set_snapshot_upload_interval` (10..600), and the JSON name-keyed fields (`camera_name`,
  `rtsp`, `webrtc` incl. the paired `webrtc on → RTSP disabled` rule, `video_quality`,
  `start_fw_update` decline, `code` guard). `tag3.11/12` are logged as an unmapped candidate;
  they are **uvarints** (`0x3f6d2c`), so the earlier "RTSP candidate" reading is only
  `[assumption]`.
- **Connect impact (resolved):** live setting changes are applied and persisted rather than
  misidentified; only the `tag3.11/12` mapping is unknown (logged, not guessed).
- **Before (superseded):** first attempted JSON decoding, then applied a generic flat protobuf
  decoder with guessed numeric tags, no presence tracking, enum types, signed integer handling,
  or nested-message schema; a valid Connect configuration payload could be misidentified,
  ignored, or interpreted as a different setting.
- **Implementation:** define the exact configuration schema and typed decoder from the nanopb
  descriptor/callback paths; preserve optional-field presence; reject malformed values exactly
  where firmware does.
- **Acceptance:** captured or constructed firmware-compatible payloads for every setting decode to
  the expected typed action; malformed/easter-egg guard payloads reproduce firmware rejection.
- **Code:** [`main.py`](../pi-impersonator/main.py#L251-L280),
  [`proto.py`](../pi-impersonator/proto.py#L63-L90)

### GAP-WEBRTC-01 — Consume Connect-provided ICE server configuration

- [~] **P0 · Implemented (ICE config consumed); live media negotiation verified**
- **Recovered 2026-09-19 (live):** the `webrtc` ICE config is tag8 = `{1: <blob>}` with a repeated list of `{id, host, port, type}` (1=STUN/2=TURN/3=TURNS) plus a TURN block carrying a time-limited username and base64 credential (`FUN_000bc0ec`). `proto.decode_ice_config` parses it; `webrtc.create_offer` configures `webrtcbin`'s `stun-server` and `turn-server` (escaped credentials) with it. Verified live: 12 servers + `coturn.prusa3d.com:3478` + credentials applied.
- **Firmware behavior:** parses the incoming WebRTC ICE submessage, configures every supplied STUN
  and TURN server, including hostname, port, username, credential, and server type; falls back to
  its defaults only when no servers were supplied. **[confirmed]**
- **Current behavior (implemented):** the nested `tag8` ICE config is decoded and all supplied
  STUN/TURN servers and credentials are mapped into `webrtcbin`, with firmware-equivalent fallback
  servers. Media negotiation is live-verified (browser and app).
- **Before (superseded):** field 12 was not decoded as `ice_config`; GStreamer was always configured
  with only `stun://stun.l.google.com:19302`, so remote/cloud viewing would commonly fail when
  direct ICE was unavailable and TURN relay was required.
- **Implementation:** recover and implement the ICE submessage; map all supplied URLs and TURN
  credentials into `webrtcbin`; retain firmware-equivalent fallback servers.
- **Acceptance:** unit fixtures recover the complete ICE list; an integration test forces relay and
  establishes a `RELAYED` connection using Connect-supplied credentials.
- **Code:** [`proto.py`](../pi-impersonator/proto.py#L93-L114),
  [`webrtc.py`](../pi-impersonator/webrtc.py#L66-L75)

### GAP-WEBRTC-02 — Share the existing camera encoder instead of opening libcamera twice

- [~] **P0 · Implemented (shared mux source); live-verified**
- **Recovered 2026-09-19:** `webrtc.create_offer` now reads the always-running mux stream (`tcpclientsrc 127.0.0.1:8888`) instead of spawning a second `rpicam-vid` (which died defunct — libcamera is single-consumer). The encoder also runs H264 `--profile baseline --level 3.1` to match the firmware's `H264CameraSource`.
- **Firmware behavior:** WebRTC, RTSP, and snapshots consume coordinated outputs from the existing
  hardware video pipeline. **[confirmed]**
- **Current behavior (implemented):** WebRTC reads the shared `stream_mux` output; the live-verified
  browser and app sessions ran without a second libcamera owner or camera-busy errors.
- **Before (superseded):** `rpicam-source.service` continuously owned the camera, while WebRTC
  started a second independent `rpicam-vid` process, which was likely to fail with a camera-busy
  error once an offer reached the Pi.
- **Implementation:** feed WebRTC from `stream_mux.py` or provide one shared capture/encode service
  with independent RTSP, JPEG, and WebRTC consumers.
- **Acceptance:** RTSP, periodic snapshots, and a WebRTC session can run without a second libcamera
  owner or camera-busy errors.
- **Code:** [`webrtc.py`](../pi-impersonator/webrtc.py#L47-L72),
  [`rpicam-source.service`](../pi-impersonator/systemd/rpicam-source.service)

### GAP-WEBRTC-03 — Implement session lifecycle and teardown

- [x] **P0 · Stream + teardown live-verified (app and browser) 2026-09-19**
- **Live reproduction 2026-09-19 (browser page-open):** a Connect-web cameras page triggered one
  offer (`Sent WebRTC offer … 755 chars`), the viewer answered (`Remote answer set`), and 21
  candidates arrived — **7 added, 14 dropped** as `WebRTC candidate message carried no candidate`.
  The stream never completed and `state.streaming` stayed `True`; two `GET /snapshot.jpg` 13 s
  apart were byte-identical and 40 s of `journalctl -f -u prusa-cam` had zero `Snapshot:` lines —
  **periodic snapshots were permanently paused**, exactly as the "Current behavior" note predicts.
- **Live fix (2026-09-19, `8bd7a45`):** `webrtcbin` has **no** `on-ice-connection-state-change` signal (gst-inspect confirms only `on-ice-candidate`/`on-negotiation-needed`); the first attempt raised in `create_offer` and left snapshots paused again. Fixed by connecting **`notify::ice-connection-state`** (a readable GObject property), arming the connect watchdog *before* signal wiring, wrapping each connect, and resuming snapshots if `create_offer` raises. **Verified live:** a browser "Play live stream" now creates and sends the offer (`m=video 9`, media accepted, 754 chars) with local candidates; when ICE never connected, the 30 s watchdog fired (`WebRTC stream ended (no-ice-connection)`) and snapshots resumed (`Resuming snapshots after WebRTC stream ended`; cadence gap 22:33:19→22:34:00 then every 10 s). Candidate extraction (`proto.find_webrtc_candidate`) also fixed (unit-tested).
- **Browser WebRTC now works (live-verified 2026-09-19):** after the candidate-extraction fix the viewer's trickle candidates are all applied, and the browser stream establishes — Pi log `Remote answer set` → `WebRTC inbound candidate added` → `ICE connection state: 1/2/3`; the page's `<video>` was **1920×1080, readyState 4, playing** (`currentTime` advancing). The candidate fix was the enabler: the earlier run dropped 14/21 candidates and ICE never completed. Teardown also verified: ending the viewer gave `ICE connection state: 4` → `WebRTC stream ended (ice-failed)` → `Resuming snapshots` and snapshots resumed within seconds.
- **`CameraIsNotSessionMemberError` fixed (2026-09-19, `ba48dc8`):** the server error was caused by the **unsolicited post-auth burst** (`send_sio_info` + `status` + `protobuf_version` + `features`) — the server answered `protobuf_version` with ACK `1` + `CameraIsNotSessionMemberError` and cycled the session, dropping the viewer's answer. Firmware `FUN_000a05e4` sends nothing post-auth (only the trigger dispatcher `FUN_000a9638` for tags 1/2/12 does), so the burst was removed. **Live-verified after deploy:** 0 `CameraIsNotSessionMemberError`, one stable connection (Auth ACK `0`, no disconnect), and a browser "Play live stream" relayed its answer immediately (`Remote answer set` → candidates added → `ICE connection state: 1/2/3`, video playing 1920×1080).
- **Recovered 2026-09-19 (live):** the full camera-side flow is implemented — ICE config → camera **offer** (type 3) → viewer **answer** (type 2) → **trickle candidates** (type 4). The viewer's candidates arrive as `a=candidate:...` with `tag2 = mid` and are applied via `add-ice-candidate`.
- **Final fix (live-verified):** the Connect answerer (a libdatachannel endpoint) **validates the H.264 SPS** and rejects our v4l2 SPS (`428029`, baseline level 4.1) with `m=video 0`; it wants the firmware's `42e01f` class (constrained baseline level 3.1). Transcoding was too heavy (`openh264enc` wedged the Pi; `v4l2h264enc` is owned by the camera source), so `stream_mux` now serves the same H264 on **port 8889** with each SPS NAL's profile/constraint/level patched to `42 e0 1f` (matched by NAL type — rpicam-vid emits header `0x27`), and the WebRTC branch reads 8889 (8888/RTSP/snapshots untouched). After the patch the answer is `m=video 9 …` (media **accepted**) and the stream plays. Requires `gstreamer1.0-nice`.
- **Firmware behavior:** tracks clients and connection state, enforces lifetime/scope, tears down
  disconnected/expired peers, and restores scoped video settings. **[confirmed]**
- **Current behavior (historical, fixed 2026-09-19):** the global `streaming` flag used to become true
  on the first offer and was never cleared. ICE/peer failure, close, disconnect and a watchdog now
  clear it and resume snapshots.
- **Connect impact (historical):** snapshots remained permanently paused after the first offer; now
  they resume on any stream end, and stale peer resources are torn down.
- **Implementation:** represent each session explicitly; handle ICE/DTLS/peer state transitions;
  enforce TTL; tear down on failure/disconnect/expiry; clear streaming state when the last client
  ends.
- **Acceptance:** connect/disconnect, failed negotiation, and TTL-expiry tests all clean up the
  pipeline and resume snapshots.
- **Code:** [`main.py`](../pi-impersonator/main.py#L216-L231),
  [`webrtc.py`](../pi-impersonator/webrtc.py#L32-L51)
- **Implementation (done, `1e1e2f2` + `8bd7a45`):** `webrtc.py` connects
  **`notify::ice-connection-state`** (webrtcbin has no `on-ice-connection-state-change` signal) and
  maps states through the stdlib-only
  [`webrtc_lifecycle.py`](../pi-impersonator/webrtc_lifecycle.py): FAILED/CLOSED notify
  immediately, DISCONNECTED is re-checked after a 15 s grace period, and a 30 s connect watchdog
  fires when ICE never connects. `main.py`'s `on_stream_ended` clears `state.streaming` and
  resumes periodic snapshots. Viewer trickle candidates are extracted by the host-testable
  [`proto.find_webrtc_candidate`](../pi-impersonator/proto.py), which also unwraps the
  UTF-8-collapsed tag4 `str` (the live 14-of-21 drop). Tests:
  `test_pi_proto.py::FindWebRtcCandidateTests`, `test_pi_webrtc_lifecycle.py`. The firmware's
  exact peer TTL worker remains untraced, so the watchdog is explicitly Pi-side policy.
  **Live-verified (app and browser, 2026-09-19):** connect, media, and teardown (ICE failed/closed/
  disconnected clears `state.streaming` and resumes snapshots). Only explicit TTL-expiry tests
  remain.

## P1 — Connect-visible control and state gaps

### GAP-WEBRTC-04 — Apply `set_webrtc_mode`

- [~] **P1 · Implemented; live verification pending**
- **Firmware behavior:** protobuf field 1 value `1` starts/enables the WebRTC service; `0` stops and
  disables it. Mode and runtime status are separate values and gate inbound offers. **[confirmed]**
- **Current behavior (implemented):** `webrtc_control.apply_mode` decodes field 1, starts/stops the
  `PrusaWebRTC` loop, tracks `state.webrtc_mode` and `state.webrtc_status` separately, gates
  inbound offers, and reports the real mode/status in `status`.
- **Connect impact (resolved):** Connect can control the service and sees the actual mode/runtime
  state.
- **Before (superseded):** the event was logged but had no state or service effect; status always
  reported mode `1`, status `1`.
- **Implementation:** decode field 1, persist/track mode, start or stop WebRTC resources, reject
  offers while disabled, and report actual mode/runtime status.
- **Acceptance:** enable/disable fixtures alter the gate and subsequent `status` payload exactly as
  expected.
- **Code:** [`signaling.py`](../pi-impersonator/signaling.py#L60-L66),
  [`signaling.py`](../pi-impersonator/signaling.py#L275-L278)
- **Implementation (staged, commit pending):** [`webrtc_control.py`](../pi-impersonator/webrtc_control.py)
  decodes field 1 (not the `0x08` tag byte), applies the `FUN_000b94ac` enable/disable state machine,
  and exposes the `FUN_000b996c` gate (`offer_allowed`). `main.py`'s offer handler consults the gate,
  and `set_webrtc_mode` / `configuration.webrtc` start/stop the `PrusaWebRTC` GLib loop with
  `state.webrtc_mode` and `state.webrtc_status` tracked separately. Tests:
  `test_pi_webrtc_control.py`. **Correction:** the paired rule where `configuration.webrtc=on` also
  forces RTSP disabled **is implemented** (`main.py` `handle_event`, `Config: webrtc on → RTSP
  forced disabled (paired rule)`), consistent with GAP-CONFIG-01; WebRTC mode is persisted via
  `settings_store` / `state.json` (GAP-PERSIST-01), not memory-only.

### GAP-WEBRTC-05 — Honor transport policy, TTL, SDP plan, scoped quality, FPS and scope

- [ ] **P1 · Open; descriptor and enforcement path now confirmed, per-field name binding still `[assumption]`**
- **RE 2026-09-20 (Wave 1, direct 3.1.6 decompiler/ELF) [confirmed]:**
  - Descriptor **`0x3f7680`** (9 fields): 1/2/3 strings (token/client/session); **4 submessage @0x1c
    (16 B, SDP/candidate)**; **5/6/7 uvarints @0x2c/0x30/0x34**; **8 submessage @0x3c (16 B, ICE
    config)**; **9 submessage @0x50 (20 B = the scalar policy group)**.
  - Inbound handler is **`FUN_000a43e0`** (unexported; recovered via Ghidra): it decodes the
    message, reads tag5 (`+0x2c`), tag6 (`+0x30`, checked `==1`), tag7, and tag9's fields, then
    calls the gate/enqueue **`FUN_000b996c`**.
  - Live offers carry keys `[1,2,3,5,7,8,9]` with **tag5 present, tag6 absent, tag7=2, tag9=11 B**
    -> Connect sends only a subset of the descriptor.
  - The **TURN/scoped-quality lock is in the CONFIG path** `[confirmed]`: `FUN_000a7940` quality
    branch calls `FUN_000b5ad4` -> `FUN_000b4f90` reads the "TURN client online" flag at
    **singleton+0x278**; if 0 -> allowed; else a cap check `FUN_000b51e8`; apply `FUN_000b4e98`
    (writes singleton+0x130). Log string `DAT_000a89c4`.
  - The exact name->tag binding for the 7 log names remains **[assumption]** — do not implement
    policy enforcement for unbound fields.
- **Firmware behavior:** consumes inbound fields for transport policy, TTL, video configuration,
  plan, quality, FPS, scope/lifetime, and ICE configuration. It applies per-client quality limits
  and locks incompatible quality changes while a TURN client is active. **[confirmed]**
- **Current behavior (corrected):** these fields are decoded but not enforced. **The stale claim
  that `scope` is read from field 12 is wrong and is removed:** `proto.py` has no `scope` key at
  all, and field 12 of the old flat table is not the camera-side schema (the inbound message is the
  9-field descriptor above, whose field 9 is the scalar policy group). Video is always 30 FPS and
  uses the locally persisted global quality.
- **Connect impact:** relay-only requests, bandwidth limits, session lifetimes, and requested video
  profiles are not honored; only the fields Connect actually sends are candidates for enforcement.
- **Implementation:** correct field mapping, type the fields, enforce transport policy and TTL,
  configure plan/FPS, and add scoped-quality arbitration. **Only RE-backed, Connect-observed fields
  may be enforced**; leave the rest documented as unenforced.
- **Acceptance:** parameterized tests demonstrate distinct ALL/RELAY, TTL, SD/HD/FHD, and FPS
  behavior; TURN sessions block global quality changes like firmware.
- **Code:** [`proto.py`](../pi-impersonator/proto.py#L100-L113),
  [`webrtc.py`](../pi-impersonator/webrtc.py#L56-L72)

### GAP-WEBRTC-06 — Emit `webrtc_connection_info`

- [~] **P1 · Implemented but GATED OFF: live server rejected the numeric encoding (field types unconfirmed)**
- **Live 2026-09-20:** with the numeric encoding (fields 2/3 as bytes) the Pi emitted
  `{1: client_id, 2: 6, 3: 6}` after ICE completed and the server answered the `error` event
  **`webrtc_connection_info - Error: Read past limit`** — so fields 2/3 are most likely
  **strings**, not bytes (the original `protocol.md` note). The numeric-vs-string question must be
  settled from a genuine capture or the C++ message's `.proto`; per the no-guess rule the sender is
  kept but **disabled by default** (`PRUSA_WEBRTC_CONNECTION_INFO=1` gates it). Separately, the
  GStreamer `get-stats` scan did not find the selected pair on the Pi (emitted a misreporting 6/6),
  so the extractor now **skips** rather than misreport when the pair is not extractable.
- **RE 2026-09-20 (Wave 1, direct 3.1.6 decompiler/ELF) [confirmed]:**
  - The sender is **`FUN_000be3f8`** (the older `FUN_000a3998` citation is wrong). It logs
    `"Sending connection type event for client %s: %d <-> %d"` and
    `"Failed to get selected candidate pair for %s"`, runs **after candidate-pair selection**, and
    when no pair exists forces both types to **6**.
  - It calls `FUN_000be270` -> the encoder `FUN_000be050`/`FUN_000bdd3c`: field 1 = client id
    string, field 2 = local type byte, field 3 = remote type byte. Fields 4/5/6 are not populated
    by this path; their semantics are `[assumption]`/unset.
  - Candidate-type translator **`FUN_000b5098`**: input 1 -> **1 HOST**, 2 -> **2
    SERVER_REFLEXIVE**, 3 -> **3 PEER_REFLEXIVE**, 4 -> **4 RELAYED**, 0 -> **5 UNDEFINED**,
    anything else -> **0 UNKNOWN**; forced **6** when there is no pair.
  - **Wire type correction:** fields 2/3 are numeric bytes, not strings. `protocol.md` previously
    rendered them as `string local_type`/`string remote_type`; corrected 2026-09-20.
- **Firmware behavior:** after ICE selection, emits `webrtc_connection_info` containing client ID,
  local candidate type, and remote candidate type (`HOST`, `SERVER_REFLEXIVE`, `PEER_REFLEXIVE`,
  `RELAYED`, etc.). **[confirmed]**
- **Current behavior (implemented):** `webrtc_lifecycle.candidate_type_code` maps the ICE `typ`
  token to the recovered code space; `webrtc.py` reads the selected candidate pair from webrtcbin
  `get-stats` on `notify::ice-connection-state` reaching CONNECTED/COMPLETED and calls
  `signaling.send_webrtc_connection_info` (event `webrtc_connection_info`, fields 1/2/3 only).
  Missing stats log and skip; a present-but-empty pair emits 6/6, matching `FUN_000be3f8`.
- **Connect impact (resolved on the wire):** Connect receives the connection telemetry it expects
  and can distinguish direct from relayed sessions; live verification pending.
- **Implementation:** obtain selected candidate-pair stats from GStreamer, map candidate types to
  the firmware numeric enum, encode the three populated fields, and emit it after selection.
- **Acceptance:** direct and forced-relay tests produce the correct event and candidate types.
- **Implementation (staged, commit pending):** helpers in
  [`webrtc_lifecycle.py`](../pi-impersonator/webrtc_lifecycle.py) (`candidate_type_code`,
  `parse_candidate_type`, `connection_info_payload`), emission in
  [`webrtc.py`](../pi-impersonator/webrtc.py) (`_emit_connection_info`/`_on_stats_ready`),
  sender in
  [`signaling.py`](../pi-impersonator/signaling.py) (`send_webrtc_connection_info`), wired from
  [`main.py`](../pi-impersonator/main.py) (`on_connection_info`). Tests:
  `tests/test_pi_webrtc_connection_info.py`. **Assumption:** the exact GStreamer `get-stats`
  structure shape is version-dependent; the extractor is a best-effort recursive scan and skips
  the event when the selected pair cannot be identified (never guesses).

### GAP-STATUS-01 — Report actual dynamic camera state

- [~] **P1 · Implemented (d1ec311, 9548e95): shared state drives quality/name/interval/WebRTC/RTSP; live verification pending**
- **Firmware behavior:** constructs `CameraInfoMessage` from current snapshot state, upload interval,
  IR mode, speaker volume, RTSP mode/status/URL, WebRTC mode/status, service state, current quality,
  network state, timezone, and system telemetry. **[confirmed]**
- **Current behavior (implemented):** `CameraState` drives status; quality, camera name,
  snapshot/timelapse intervals, WebRTC mode/status, and RTSP mode/running (queried from the
  systemd service) are read from shared runtime state rather than fixed defaults.
- **Connect impact (resolved):** UI state tracks actual camera behavior for the supported settings.
- **Before (superseded):** many values were fixed defaults — WebRTC always enabled/running, video
  quality always FHD, upload interval fixed at 10, and RTSP state not following the service.
- **Implementation:** introduce a single runtime state model shared by command handlers and status
  encoding; read the persisted quality at startup; query actual service states where necessary.
- **Acceptance:** after every supported command, a decoded `status` fixture shows the resulting
  firmware-equivalent state.
- **Code:** [`signaling.py`](../pi-impersonator/signaling.py#L214-L304)

### GAP-STATUS-02 — Correct request correlation in `status`

- [~] **P1 · Implemented and unit-tested (d1ec311); fixture/live comparison pending**
- **Firmware behavior:** conditionally supplies field 10 from the request/correlation value when the
  corresponding presence flag is set. Initial live captures also show the Socket.IO SID in this
  position, so the exact source depends on send context. **[confirmed for conditional firmware
  path; initial-vs-response selection needs a fixture]**
- **Current behavior (implemented):** `build_status_message` sets field 10 from `request_id` for a
  request-triggered status, and falls back to the Socket.IO SID when a trigger carries no
  request-id. (The old "unsolicited initial status" path no longer exists — nothing is sent
  post-auth, GAP-AUTH-01.)
- **Connect impact (resolved):** a requested status response correlates with the request that
  caused it.
- **Before (superseded):** `send_status(request_id=...)` accepted a request ID but the status
  encoder ignored it and always encoded the Socket.IO SID.
- **Implementation:** distinguish unsolicited initial status from request-triggered status and set
  field 10 from the correct context.
- **Acceptance:** initial-status and request-response fixtures encode different expected field 10
  values and match a real-camera capture or descriptor-driven test.
- **Code:** [`signaling.py`](../pi-impersonator/signaling.py#L293-L310)

### GAP-SNAPSHOT-01 — Apply snapshot upload interval changes

- [x] **P1 · Implemented and unit-tested (d1ec311); live cadence verified (20 s, GAP-CONFIG-01)**
- **Firmware behavior:** accepts `snapshot_interval` in seconds, validates the inclusive range
  `10–600`, stores milliseconds in configuration, and changes the active upload cadence.
  **[confirmed]**
- **Current behavior (implemented):** the interval lives in shared mutable state; a valid change
  wakes/reschedules the active loop and updates status. **Live-verified:** the Connect
  "Displayed Frame Update Interval" slider sent `{3:{5:20}}` and the snapshot cadence settled to
  exactly 20 s (GAP-CONFIG-01).
- **Connect impact (resolved):** the UI setting takes effect live.
- **Before (superseded):** the interval was read once at loop creation and later valid changes were
  only logged, so the UI setting had no effect.
- **Implementation:** keep interval in shared mutable state, persist it if desired, wake/reschedule
  the active loop, and update status.
- **Acceptance:** changing 10→60→10 seconds changes measured upload scheduling without restart;
  values outside the firmware range are rejected.
- **Code:** [`main.py`](../pi-impersonator/main.py#L129-L148),
  [`main.py`](../pi-impersonator/main.py#L260-L262)

### GAP-SNAPSHOT-02 — Implement snapshot enable/disable triggers

- [~] **P1 · Implemented and unit-tested (9291968); live verification pending**
- **Firmware behavior:** `enable_snapshot_upload` and `disable_snapshot_upload` control the periodic
  uploader independently of immediate get-snapshot requests. **[confirmed]**
- **Current behavior (implemented):** trigger tags 4/5 values `1`/`2` set the shared
  `state.snapshot_upload_enabled`, which `periodic_snapshot_allowed` reads; immediate
  get-snapshot remains independent of that switch.
- **Connect impact (resolved):** remote snapshot control starts/stops the periodic uploader.
- **Before (superseded):** the periodic loop always ran unless locally paused for RTSP/WebRTC;
  trigger enable/disable was not decoded, so remote snapshot control did nothing.
- **Implementation:** add an explicit upload-enabled state and handle both trigger values; reflect it
  in status while preserving immediate snapshot behavior.
- **Acceptance:** disable stops periodic uploads, get-snapshot still performs its defined action, and
  enable resumes the configured cadence.
- **Implementation (staged, commit pending):** recovered trigger tags 4/5 values `1`/`2` now map to
  `snapshot_enable`/`snapshot_disable` and are applied through
  [`trigger.py`](../pi-impersonator/trigger.py) `apply_snapshot_upload`, which sets the shared
  `state.snapshot_upload_enabled` that `periodic_snapshot_allowed` already reads. Immediate
  get-snapshot is independent of that switch and keeps only the existing WebRTC pause. Status has
  no recovered field for this flag, so none is emitted rather than inventing one. Tests:
  `test_pi_trigger.py` (`SnapshotControlTests`). Live cadence verification on the Pi remains
  pending.

### GAP-INFO-01 — Refresh and retry `/c/info`

- [~] **P1 · Implemented; live verification pending**
- **Firmware behavior:** retries attribute upload and marks it dirty after relevant configuration or
  state changes. The recovered service loop retries on a countdown until successful. **[confirmed]**
- **Current behavior (implemented):** `info_service` runs the firmware-style dirty/countdown loop
  (`next_info_action`, reload to 10 on failure) and marks dirty on camera-name, quality,
  snapshot-interval, and RTSP/WebRTC mode changes, so `/c/info` is republished rather than sent once.
- **Connect impact (resolved):** a transient failure recovers without restart, and changed
  attributes are republished.
- **Before (superseded):** performed one `/c/info` upload during startup and never refreshed it, so a
  transient failure or later attribute change left stale metadata until restart.
- **Implementation:** add bounded retry/backoff and a dirty/update mechanism invoked by relevant
  changes and reconnect/network events.
- **Acceptance:** injected HTTP failures recover without restart; changing a published attribute
  results in a subsequent successful `/c/info` containing the new value.
- **Code:** [`main.py`](../pi-impersonator/main.py#L179-L188),
  [`upload.py`](../pi-impersonator/upload.py#L18-L53)
- **Implementation (staged, commit pending):** pure decisions in
  [`info_service.py`](../pi-impersonator/info_service.py) reproduce the firmware
  dirty/countdown loop (`next_info_action`, reload to 10 on failure); `main.info_service_loop`
  ticks every second and marks dirty on camera-name, quality, snapshot-interval, and RTSP/WebRTC
  mode changes. Retry is bounded by `http_result.MAX_INFO_RETRIES` (the task contract's finite
  bound; firmware itself retries indefinitely). Tests: `test_pi_info_service.py`
  (`NextInfoActionTests`, `DirtyAfterResultTests`, `ServiceLoopRecoveryTests`).

### GAP-CAP-01 — Stop overpromising unsupported features, or implement their wire behavior

- [x] **P1 · Resolved 2026-09-19: prune the hardware-absent features**
- **Decision:** stop advertising features the Pi cannot honor. `/c/info` now advertises only what is implemented: `SocketCom, UploadInterval, TimelapseEn/Interval/VideoMake/FileList, VideoStream, RtspStream, GetSnapshot, WiFi, FwVer, HwVer, CameraName, MicroSd, FwUpdate, CameraReboot, McuTemp, VideoQuality, WebRtc, TurnVideoQualityChange, trigger_scheme`. Removed `IrMode`, `SpeakerVolume`, `FanControl` (no such hardware on the Pi — Connect was showing the IR sun/moon/auto control that could never work). **`MicroSd` is kept** — the Pi backs it with the emulated SD at `/mnt/sdcard`, so Connect's timelapse UI works (GAP-TIMELAPSE-01). `configuration.light_control` still returns a truthful unavailable result if it ever arrives.
- **Firmware behavior:** advertises features it implements: `SocketCom`, `UploadInterval`,
  `TimelapseEn`, `TimelapseInterval`, `TimelapseVideoMake`, `TimelapseFileList`, `VideoStream`,
  `RtspStream`, `GetSnapshot`, `IrMode`, `SpeakerVolume`, `WiFi`, `FwVer`, `HwVer`, `CameraName`,
  `MicroSd`, `FwUpdate`, `CameraReboot`, `McuTemp`, `VideoQuality`, `WebRtc`,
  `TurnVideoQualityChange`, `trigger_scheme`, and `FanControl`; motor features are conditional.
  **[confirmed]**
- **Current behavior:** advertises the same motorless list but does not implement many corresponding
  commands or result messages.
- **Connect impact:** Connect exposes controls that silently fail or receive malformed/no responses.
- **Implementation choice:** either implement each advertised contract or determine, with live
  testing, which capabilities may be removed without losing Buddy classification/WebRTC enrollment.
  Do not claim a feature merely to resemble the string list if its behavior is absent.
- **Acceptance:** every advertised feature has a passing command/status integration test; all
  intentionally unsupported features are absent and the resulting feature hash is updated.
- **Code:** [`features.py`](../pi-impersonator/features.py)

## P2 — parameter and semantic differences

### GAP-QUALITY-01 — Correct the raw quality-byte mapping

- [~] **P2 · Implemented and unit-tested (d1ec311); live verification pending**
- **Firmware parameter:** raw event/internal values are `5=SD`, `6=HD`, `7=FHD`; protobuf enums are
  `1=SD`, `2=HD`, `3=FHD`; dimensions are `640×480`, `1280×720`, `1920×1080`. **[confirmed directly
  from `FW-QUALITY-PB`, `FW-QUALITY-DIRECT`, and `FW-QUALITY-DIMS`]**
- **Current parameter (implemented):** `state.py` `RAW_TO_ENUM = {5: 1, 6: 2, 7: 3}` and the
  dimension table is correct, so raw bytes 5/6/7 select SD/HD/FHD.
- **Connect impact (resolved):** raw-byte commands select the intended tier.
- **Before (superseded):** the direct event handler mapped `5→HD`, `6→FHD`, `7→SD`, so every
  `change_video_size`/`save_video_size` raw-byte command selected the wrong tier.
- **Implementation:** use raw-to-protobuf mapping `{5: 1, 6: 2, 7: 3}` and preserve the existing
  protobuf-enum-to-dimensions table.
- **Acceptance:** raw bytes 5/6/7 yield SD/HD/FHD respectively and status reports enums 1/2/3.
- **Code:** [`quality.py`](../pi-impersonator/quality.py#L11-L12)

### GAP-QUALITY-02 — Reproduce the quality persistence flag and recover event wiring

- [~] **P2 · Partial (d1ec311): live/persist split and failure rollback implemented and tested; event→flag wiring still unrecovered**
- **RE status 2026-09-20 (Wave 1):** not recovered — the Wave-1 RE pass (W6) did not pin the
  `change_video_size`/`save_video_size` registration callbacks or the persist-flag arguments, so
  the flag assignment stays **open** and unassigned by event name.
- **Firmware behavior:** the recovered handler `FUN_00072f08` always attempts the live resolution
  change for raw values `5`, `6`, or `7`. It additionally calls the persistence setter only when
  `*param_3 != 0`, and updates its in-memory current value only after the live changer succeeds.
  **[confirmed]** The exact assignment of flag `0`/`1` to the indirectly registered
  `change_video_size` and `save_video_size` Socket.IO callbacks is **not yet confirmed**.
- **Current behavior:** both events call `apply_quality()`, which persists and restarts the source
  and RTSP service.
- **Connect impact:** persistence occurs even on the non-persisting firmware path, and failed live
  changes can still be recorded as if they succeeded.
- **Implementation:** split `apply_live_quality(raw)` from `persist_quality(raw)`. Make live apply
  return success; update shared current state only on success; invoke persistence only when the
  recovered callback supplies a nonzero flag. Do not assign the flag by event name until the
  registration callback/capture establishes it.
- **Acceptance:** direct handler tests prove that flag `0` performs a successful live change with no
  write, flag `1` performs the same live change plus one write, and live-change failure performs
  neither the current-state update nor persistence. A separate fixture pins each event name to its
  recovered flag.
- **Code:** [`main.py`](../pi-impersonator/main.py#L291-L300)

### GAP-QUALITY-03 — Initialize and publish persisted quality

- [~] **P2 · Implemented and unit-tested (d1ec311); live verification pending**
- **Firmware behavior:** starts from its stored quality and reports the translated current enum.
  **[confirmed]**
- **Current behavior (implemented):** the persisted tier is loaded once into shared state and used
  for the source, WebRTC, `/c/info`, and status (live-verified after reboot: `state.json`
  `{"quality_tier": 2}` → `quality.env` `1280x720` → encoder starts 1280×720; GAP-PERSIST-01).
- **Connect impact (resolved):** state and actual stream resolution agree after restart.
- **Before (superseded):** module state started as FHD and status always encoded FHD even if
  `quality.env` contained HD or SD, so state and stream resolution could disagree after a restart.
- **Implementation:** load `quality.read_current()` once into shared state and use it for source,
  WebRTC, `/c/info`, and status.
- **Acceptance:** starting with each persisted tier produces matching encoder dimensions,
  `/c/info`, and status.

### GAP-RTSP-01 — Align RTSP port and advertised URL

- [~] **P2 · Firmware default needs one final evidence check**
- **RE status 2026-09-20 (Wave 1):** not recovered — the Wave-1 RE pass (W7) did not pin the
  shipped RTSP default port, so it stays **open**; do not change 8554 without config-image or
  genuine-payload evidence.
- **Firmware parameter:** loads the RTSP mode/port through configuration getters, starts the service
  when mode equals `2`, and advertises an RTSP URL assembled from runtime state. The binary logs the
  selected port dynamically. Older notes identify the effective default as `554`, but that literal
  is not established by `FW-RTSP-INIT` alone. **[confirmed for dynamic/configured behavior; default
  port requires config-image or live-payload confirmation]**
- **Current parameter:** listens on `8554` and advertises `rtsp://<ip>:8554/live`.
- **Connect impact:** any consumer assuming the OEM default port sees a different endpoint. Connect
  may accept the explicitly advertised URL; this needs live verification.
- **Implementation:** first recover the default from the shipped config or a genuine status payload.
  If it is 554, prefer that port using service capabilities; otherwise retain the recovered value.
  Document 8554 as an intentional Pi exception only after verifying Connect consumes the advertised
  URL.
- **Acceptance:** selected behavior is consistent between listener, status, documentation, and a
  real Connect/local-client test.
- **Code:** [`rtsp_server.py`](../pi-impersonator/rtsp_server.py),
  [`signaling.py`](../pi-impersonator/signaling.py#L257-L261)

### GAP-RTSP-02 — Track configured mode separately from runtime state

- [~] **P2 · Implemented; live verification pending**
- **Firmware behavior:** handles disabled/enabled modes (`1`/`2` on the recovered direct event),
  starts/stops the server, tracks clients, and reports mode/status/URL dynamically. **[confirmed]**
- **Current behavior (implemented):** direct and configuration-form commands share
  `rtsp_control.apply_mode`, which starts/stops `prusa-rtsp.service`, sets `state.rtsp_mode`, and
  resolves `state.rtsp_running` from `systemctl is-active`; the configured mode persists at
  `/etc/prusa-cam/rtsp.mode` (and in `state.json` via GAP-PERSIST-01).
- **Connect impact (resolved):** reported and actual RTSP state stay consistent.
- **Before (superseded):** direct start/stop called systemd but status remained hardcoded and
  configuration-form RTSP changes were only logged, so reported and actual state could diverge.
- **Implementation:** define persisted/configured mode and actual service state; handle direct and
  configuration-form commands through one path; choose boot behavior from mode.
- **Acceptance:** disable survives the intended persistence boundary, status follows service state,
  and both command forms behave identically.
- **Implementation (staged, commit pending):** [`rtsp_control.py`](../pi-impersonator/rtsp_control.py)
  decodes the direct field-1 mode, maps `configuration.rtsp` `on`/`off` to `2`/`1`, and applies both
  through one `apply_mode` path that starts/stops `prusa-rtsp.service`, sets `state.rtsp_mode`, and
  resolves `state.rtsp_running` from `systemctl is-active` (falling back to the commanded state when
  the unit cannot be probed). The configured mode persists at `/etc/prusa-cam/rtsp.mode`
  (`PRUSA_RTSP_MODE_FILE` override) and is read at startup; on the read-only overlay a runtime write
  is durable only once it reaches the lower filesystem via `deploy.sh`. Tests:
  `test_pi_rtsp_control.py`. Default mode when the file is absent is `2` (enabled), matching the
  shipped unit; the firmware's shipped default remains unrecovered. Client tracking is unchanged
  (`/proc/net/tcp`).

### GAP-SNAPSHOT-03 — Match JPEG encoding quality

- [~] **P2 · Implemented and unit-tested (d1ec311); live verification pending**
- **Firmware parameter:** snapshot JPEG conversion uses quality `95`. **[confirmed]**
- **Current parameter (implemented):** GStreamer `jpegenc quality=95` (`camera.py`).
- **Connect impact (resolved):** JPEG quality and payload size match firmware.
- **Before (superseded):** GStreamer `jpegenc quality=85`.
- **Implementation:** set 95 unless Pi bandwidth/CPU testing justifies and documents a deliberate
  deviation.
- **Acceptance:** encoder configuration and a captured image report quality target 95.
- **Code:** [`camera.py`](../pi-impersonator/camera.py#L12-L20)

### GAP-SNAPSHOT-04 — Match snapshot scheduling and concurrent-stream behavior

- [~] **P2 · Scheduling half implemented; concurrent-stream half open**
- **Firmware behavior:** coordinated hardware channels allow snapshot service state to be controlled
  independently from RTSP/WebRTC. **[confirmed at service/state level]**
- **Current behavior (scheduling half implemented):** the monotonic start-to-start deadline is
  implemented; the RTSP/WebRTC pause is intentionally retained (the mux source is shared, but JPEG
  capture still opens its own consumer), so snapshots are still skipped while an RTSP connection or
  the WebRTC streaming flag is active. WebRTC teardown now clears the flag, so the pause no longer
  persists indefinitely after one offer.
- **Implementation (staged, commit pending):** the scheduling half uses a monotonic start-to-start
  deadline ([`scheduling.py`](../pi-impersonator/scheduling.py) `next_deadline`) so capture/upload
  duration no longer inflates the cadence and an interval change catches up immediately; the
  RTSP/WebRTC pause is intentionally retained until one shared camera source exists. Tests:
  `test_pi_scheduling.py`.
- **Connect impact (partly resolved):** snapshots pause during local/WebRTC viewing as before, but
  the earlier "indefinitely after one offer" symptom is fixed by the WebRTC teardown
  (GAP-WEBRTC-03), which resumes snapshots on stream end.
- **Before (superseded):** periodic snapshots were skipped while RTSP/WebRTC was active and, with the
  then-current WebRTC lifecycle bug, indefinitely after one offer.
- **Implementation:** once all outputs share one source, capture JPEG frames without pausing for
  RTSP/WebRTC; schedule against a monotonic deadline if firmware cadence requires start-to-start
  intervals.
- **Acceptance:** snapshots continue at configured cadence during RTSP and WebRTC without camera
  contention.
- **Code:** [`main.py`](../pi-impersonator/main.py#L129-L148)

### GAP-HTTP-01 — Snapshot `Expect: 100-continue`

- [~] **P2 · Implemented and unit-tested (d1ec311); live handshake verification pending**
- **Firmware parameter:** sends `Expect: 100-continue` for JPEG snapshot uploads. **[confirmed]**
- **Current parameter (implemented):** the snapshot PUT sets aiohttp `expect100=True`, so the
  `Expect: 100-continue` header is sent.
- **Connect impact (resolved):** the handshake matches firmware; live handshake verification still
  pending.
- **Before (superseded):** sent the body immediately without the header.
- **Implementation:** enable aiohttp's `expect100` behavior for snapshot PUT and verify no latency or
  proxy regression.
- **Acceptance:** capture shows the header and successful `100`/final response flow.
- **Code:** [`upload.py`](../pi-impersonator/upload.py#L3-L16)

### GAP-HTTP-02 — Handle HTTP result classes and throttling

- [~] **P2 · Implemented; live verification pending**
- **Firmware behavior:** distinguishes successful, blocked/throttled, redirected, and failed upload
  paths and changes service/retry behavior accordingly. **[confirmed]**
- **Current behavior (implemented):** `http_result` classifies responses into
  `success`/`redirect`/`blocked`(403)/`client_error`/`server_error`/`timeout`/`connection_error`
  and bounds transient retries; one session is reused for the app lifetime; redirects are
  classified but not auto-followed (firmware shows none).
- **Connect impact (resolved):** blocked/failed uploads take the firmware-equivalent path instead of
  being retried blindly.
- **Before (superseded):** returned/logged only the status code for snapshots, used a new session and
  fixed cadence regardless of result, and had no clear handling of server blocking.
- **Implementation:** reuse an HTTP session, classify responses, follow only firmware-equivalent safe
  redirects, and implement bounded retry/backoff/throttle behavior.
- **Acceptance:** mocked 2xx, 3xx, 4xx-blocked, 5xx, timeout, and TLS failures take the documented
  path without leaking token/fingerprint.
- **Implementation (staged, commit pending):** [`http_result.py`](../pi-impersonator/http_result.py)
  classifies `success`/`redirect`/`blocked`/`client_error`/`server_error`/`timeout`/
  `connection_error` and bounds transient retries. Direct evidence resolves the blocked class to
  exactly `403`: snapshot handler `FUN_0005c568` compares the response text to `"200"`, `"204"`,
  `"403"` and logs `Upload image BLOCKED by server!` (lp_app.strings:8304); `/c/info`
  `FUN_00062d74` accepts only `"200"`. Redirects are classified but not auto-followed because
  firmware shows no redirect handling. Tests: `test_pi_http_result.py`. Log paths redact
  token/fingerprint via `main.redact_secrets`.

### GAP-INFO-02 — Keep `/c/info` dynamic values consistent

- [~] **P2 · Implemented; live verification pending**
- **Firmware behavior:** publishes the current configured name, resolution, network values, model,
  firmware, manufacturer, trigger scheme, options, capabilities, and feature list. **[confirmed]**
- **Current behavior (implemented):** `info_body` builds the JSON body from `CameraState`
  (`state.resolution()`/`state.camera_name`), so `/c/info`, status, and the encoder share one state;
  the dirty loop republishes after changes.
- **Connect impact (resolved):** metadata shown in Connect tracks the shared runtime state.
- **Before (superseded):** name and dimensions came from startup config while live quality had
  separate persisted state; later name/quality changes did not update the document.
- **Implementation:** build `/c/info` from the shared runtime/config state used by status and command
  handlers.
- **Acceptance:** one state fixture produces mutually consistent `/c/info`, status, and encoder
  settings.
- **Implementation (staged, commit pending):** [`info_body.py`](../pi-impersonator/info_body.py)
  builds the JSON body from `CameraState` (`state.resolution()`/`state.camera_name`), and
  `upload.upload_info(session, state, ...)` no longer takes independent width/height/name.
  Tests: `test_pi_info_body.py` (body/status name and resolution consistency).

### GAP-AUTH-01 — Require successful authentication ACK

- [x] **P2 · Implemented and unit-tested (d1ec311); live-verified 2026-09-20 (ACK `0`, stable session)**
- **Firmware behavior:** continues its post-authentication flow only on the successful ACK path.
  **[confirmed]**
- **Current behavior:** only the exact success ACK `0` proceeds; no post-auth events are sent
  (the server's `trigger` polls drive `status`/`features`/`protobuf_version`).
- **Connect impact:** invalid/rejected sessions transmit nothing and back off; the bogus
  `send_sio_info` event and the unsolicited `protobuf_version` (which the server answered with
  `CameraIsNotSessionMemberError`) are gone.
- **Implementation:** require the exact success value `0` (`FUN_0009e53c`: `0`=OK, `1`=not
  authorized, `2`=error joining session); log and disconnect/back off otherwise. Post-auth sends
  removed (firmware `FUN_000a05e4` only logs/resets counters).
- **Acceptance:** ACK `0` proceeds; ACK `1`, `2`, `5`, malformed values, and timeout do not send any
  post-auth event.
- **Code:** [`signaling.py`](../pi-impersonator/signaling.py#L96-L105)

### GAP-CONTROL-01 — Apply and publish camera-name changes

- [~] **P2 · Implemented and unit-tested (d1ec311, b6ec1ea); durable persistence implemented (GAP-PERSIST-01)**
- **Firmware behavior:** stores the new camera name and includes it in subsequent status and
  `/c/info`. **[confirmed]**
- **Current behavior (implemented):** `state.set_camera_name` updates shared state, status and
  `/c/info` are republished via the dirty flag, and the name is persisted in `state.json`
  (GAP-PERSIST-01), so it survives reboot.
- **Connect impact (resolved):** rename control has a durable, visible effect.
- **Before (superseded):** logged the value only; all outbound metadata stayed `Buddy3D Camera`.
- **Implementation:** store the configured name in shared state, update status, mark `/c/info` dirty,
  and define safe persistence under the read-only-overlay deployment model.
- **Acceptance:** rename is reflected in both outbound surfaces and survives the intended reboot
  policy.

### GAP-OTA-01 — Implement truthful OTA behavior

- [~] **P2 · Implemented (truthful decline per owner decision)**
- **Implementation:** `ota.py` (stdlib-only) parses the check-in (`file`/`last_version`/`sha1sum`/`force_upgrade`), compares dotted versions and classifies `up_to_date`/`update_available`/`forced_update`/`invalid`, and verifies integrity by SHA-1. `main.py` runs a periodic `ota_loop` (6 h), classifies and logs, and both `start_fw_update` (configuration) and the `fw_update` trigger return an explicit unsupported result (`decline_firmware_update`, reason `pi_impersonator_does_not_flash_firmware`) — never a silent no-op and never a destructive flash. Tests: `tests/test_pi_ota.py` (no update, available, forced, older, malformed, integrity match/mismatch, policy).
- **Acceptance:** staged fixtures cover no update / available / integrity failure / remote-start decline; no unsafe installation path exists.
- **Still open:** a genuine release download+verify+install is intentionally not implemented (owner default); progress `client_trigger` messages remain under `GAP-SIO-01`.
- **Firmware behavior:** periodically queries the OTA endpoint, compares release/version metadata,
  downloads and verifies an update, observes update policy/time windows, installs/reboots, and
  reports progress through `client_trigger`. **[confirmed]**
- **Current behavior (implemented):** a periodic (6 h) `ota_loop` parses and classifies the
  check-in and verifies SHA-1, and both `start_fw_update` and the `fw_update` trigger return an
  explicit unsupported result (`decline_firmware_update`); `FwUpdate` remains advertised.
- **Connect impact (resolved):** Connect receives an explicit decline rather than a silent no-op; no
  unsafe installation path exists. Progress `client_trigger` remains open (GAP-SIO-01).
- **Before (superseded):** made one GET at startup, logged up to 200 response characters, never
  updated, and still advertised `FwUpdate`.
- **Implementation choice:** implement a safe Pi-software update mechanism and OEM-shaped progress
  responses, or remove `FwUpdate` and return an explicit unsupported result if the protocol permits.
- **Acceptance:** staged fixtures cover no update, available update, integrity failure, successful
  update, and remote-start request without unsafe arbitrary firmware installation.
- **Code:** [`main.py`](../pi-impersonator/main.py#L150-L168)

### GAP-TIMELAPSE-01 — Implement or stop advertising timelapse

- [~] **P2 · Firmware-named frames/AVI live-verified; make-video verified directly; file_list not app-exercisable**
- **2026-09-19 (live symptom + descriptor re-trace):** Connect shows *"Time lapse not available, camera storage not detected, insert SD card"*. The app reads storage from the **`status` message**, specifically the `extended_status.4` storage block (descriptor `0x3f72b0`; firmware labels it the "video/timelapse mode/storage block"). The descriptor was recovered exactly as **tags 1-4 uvarint + tag 5 callback string**: tag 1 = SD mounted state (`FUN_000744ac`, translated `0->2`, `1->1`, else `0`; i.e. **1 = mounted/present, 2 = not mounted**), tags 2/3/4 = total/free/used MB (`FUN_000745e0`, `(f_bsize * f_blocks) >> 20` etc. via `statvfs64("/mnt/sdcard")`), tag 5 = mount-mode string (`FUN_00073914` -> `"RW"`/`"RO"`/`"UNKNOWN"`). **Correction:** the earlier note mapping `FUN_000abcb0`/`FUN_000abaf4` into this block was wrong — those are TimelapseService singleton getters that belong to top-level `timelapse_status` (field 2), and the `MODEL` string previously emitted on tag 5 was a misread.
- **Implementation (2026-09-19, local; not live-verified):** [`timelapse.py`](../pi-impersonator/timelapse.py) provides the Pi storage policy — `sd_present` (`os.path.isdir` + `os.access(R_OK)` over `/mnt/sdcard`, matching the firmware's `FUN_00071bc0` accessibility check; the Pi has no block device), `sd_space` (`statvfs` MB with the firmware shift), `sd_mode` (`RW`/`RO`/`UNKNOWN`), and `storage_status` (the 5-tuple). [`status.py`](../pi-impersonator/status.py) encodes it on `extended_status.4` and wires `timelapse_status` tags 1/2 to `state.timelapse_enabled`/`state.timelapse_interval` (`FUN_000abcdc`/`FUN_000abcb0`); [`signaling.py`](../pi-impersonator/signaling.py) supplies live telemetry. `deploy.sh` now chowns the `/mnt/sdcard` mountpoint itself to the service user (not just `/mnt/sdcard/timelapse`) so the mode reports `RW`, and installs samba before writing its config. Tests: `tests/test_pi_timelapse.py` (`StorageStatusTests`), `tests/test_pi_status_schema.py` (storage-block and timelapse enable/interval).
- **Emulated SD (done, verified):** `/mnt/sdcard/timelapse` on the Pi (the exact firmware path), shared read/write over SMB as `\\<pi>\sdcard` (share `sdcard`; `smbd` on 139/445). `deploy.sh` provisions the dir + share + samba; `bootstrap.sh` installs samba. `MicroSd` is re-advertised.
- **Implementation:** `timelapse.py` (stdlib-only) provides interval/FPS validation, timestamped frame naming/storage, ordered listing, a minimal stdlib MJPEG-in-AVI writer, the `<name>:<status>` `.timelapse_videos.csv` index, and the firmware-shaped `file_list` fragmenter under `/mnt/sdcard/timelapse`. `CameraState` carries `timelapse_enabled/interval/fps`; `main.timelapse_loop` captures a frame on the interval while enabled; trigger tags 5 (enable/disable), 14 (make video) and 15 (file list) are wired. `signaling.send_file_list` emits the recovered `0x3f701c` envelope on the `file_list` event (field 1 = `"<page>;<total>\n<chunk>"` where `<chunk>` is one `<name>;<status>\n` per `.avi`, field 2 = HTTP token, field 3 = request_id when present, field 4 omitted); an empty list sends nothing. Tests: `tests/test_pi_timelapse.py`, `tests/test_pi_file_list.py`.
- **Live (Pi-side) 2026-09-19:** deployed via `deploy.sh` (overlay maintenance flow, services `active`); `/mnt/sdcard` is owned by the service user and writable; the deployed `timelapse.storage_status()` returns `(1, <total>, <free>, <used>, 'RW')` (present, RW); `smbd` active; `/c/info` 200 and `status` sent (387 bytes).
- **Live (app-side) confirmed 2026-09-19:** after the deploy, Connect shows timelapse **available**; the storage page displays SD size/used/free and the interval is configurable.
- **Live end-to-end test 2026-09-19:** enable/disable works via trigger tag 5 (`Trigger timelapse_enable`/`timelapse_disable`); frame capture works — 9 `frame_NNNNN.jpg` written to `/mnt/sdcard/timelapse` at the capture cadence (that run used the old `frame_NNNNN.jpg` naming, since superseded by `timelapse_<HH-MM-SS-mmm>.jpg`). Changing the app's interval sent `configuration {2: 30}` (previously ignored); now wired to `state.timelapse_interval` via the recovered `set_timelaps_interval` mapping (GAP-CONFIG-01). **Redeployed and re-verified:** the log shows `Config: timelapse_interval → 30s` and 7 frames landed exactly **35 s apart** (30 s interval + ~5 s capture), proving the interval now takes effect live.
- **Limitation (2026-09-19) [superseded by GAP-PERSIST-01]:** `/mnt/sdcard` was a plain directory on the read-only overlay root, so recordings lived in the tmpfs upper layer and were **lost on reboot** (the maintenance reboot wiped the first test's 9 frames). **Resolved 2026-09-20:** a 4 GB ext4 `PERSIST` partition at `/data` is bind-mounted to `/mnt/sdcard`, and frames + `.avi` + `.timelapse_videos.csv` were live-verified to survive a real reboot (GAP-PERSIST-01). Frames remain retrievable over SMB.
- **Firmware artifact naming (recovered 2026-09-19, decompile):** individual frames are JPEGs written by `FUN_000ac5d4` (`std::ofstream`, log `Saved jpeg frame to file: %s`) as **`timelapse_<HH-MM-SS-mmm>.jpg`** (time format `%02d-%02d-%02d-%03d` from `FUN_000ac2e8`), under `/mnt/sdcard/timelapse/`; the service path component at object `+0x10` (also reported as `timelapse_status` tag 4) is prepended when non-empty (`FUN_000ac4b4`). The assembled video is **`.avi`** with a hidden **`.timelapse_videos.csv`** index (`FUN_000ac134` writes `<name>:<status>` rows — `D`/`E`/`P` from `FUN_000aee3c`); the file-list composer `FUN_000ad7ec` enumerates the **`*.avi`** files (`FUN_000ac934`) and emits one **`<name>;<status>\n`** entry each, defaulting a name absent from the index to `'U'` (0x55; log `Timelapse videos with status: %s`). **Matched locally 2026-09-19:** `timelapse.frame_name`/`save_frame` use the exact timestamped `.jpg` name (collisions bump the millisecond), `build_avi` writes an MJPEG-in-AVI `timelapse_<HH-MM-SS-mmm>.avi` and appends a `<name>:D` (or `:E` on failure) row, and `read_video_index`/`file_list_entries` compose the `<name>;<status>` listing; `FUN_000a1fa8`'s `0x3f701c` list envelope is annotated in `protocol.md` and sent by `signaling.send_file_list`.
- **Live (artifact naming + interval) 2026-09-19:** redeployed (`5ad9263`) and re-tested. The app set the interval to 15 s then 10 s (`configuration {2: 15}` / `{2: 10}` → `Config: timelapse_interval → 15s/10s`), enabled timelapse via trigger tag 5, and 8 frames recorded as **`timelapse_<HH-MM-SS-mmm>.jpg`** (~15 s apart = 10 s interval + ~5 s capture), then disabled. Running `build_avi` on those 8 real frames produced `timelapse_<HH-MM-SS-mmm>.avi` (2.8 MB) that `file(1)` identifies as *"RIFF ... AVI, 1920 x 1080, 10.00 fps, video: Motion JPEG"*, with a `<name>:D` `.timelapse_videos.csv` row and `file_list_entries` = `<name>;D\n`. Also observed: `configuration` `tag3.10` carries the **active print-job name** (e.g. `Voron_Design_Cube_v8_0_4n_0_2mm_PC_COREONE_54m_b`; `unknown_timelapse` when idle).
- **Live status:** this Connect app version exposes **no make-video button and no file-list/recordings view**, so `tag 14`/`tag 15` and the `file_list` envelope **cannot be exercised through the app UI**. They are implemented and unit-tested (the make path is verified directly on the Pi via `build_avi`/`file(1)`); the `file_list` sender remains not app-exercised. Progress `client_trigger` remains under `GAP-SIO-01`.
- **Acceptance:** every advertised timelapse action has a schema fixture and either a working result (enable/disable, make, file list) or an explicit unsupported response.
- **Remaining:** progress `client_trigger` (under `GAP-SIO-01`); the `file_list` envelope has no app UI to exercise it (only a Socket.IO client replay could).
- **Firmware behavior:** controls enable/interval/FPS, stores frames, creates MJPEG output, indexes
  files, returns file list/status, and emits progress/error `client_trigger` messages. **[confirmed]**
- **Current behavior (historical):** previously advertised all four timelapse features and answered
  file-list requests with an untyped empty protobuf message; now implemented locally (see above).
- **Connect impact (historical):** exposed list/progress flows did not work; the list flow is now
  implemented locally and pending live verification, while progress `client_trigger` remains open.
- **Implementation choice:** implement a Pi storage-backed equivalent (chosen); an empty list sends
  no message, and the exact firmware `0x3f701c` list envelope is now encoded rather than an untyped
  empty message.
- **Acceptance:** every advertised timelapse action has a schema fixture and either a working result
  or an explicit firmware-shaped unsupported/error response.
- **Code:** [`main.py`](../pi-impersonator/main.py#L301-L304)

### GAP-PERSIST-01 — Persist settings + timelapse storage on /data

- [x] **P2 · Live-verified 2026-09-20: settings + timelapse store survive a reboot**
- **Problem:** the Pi root is a read-only overlay (`overlayroot=tmpfs`), so `/etc/prusa-cam/*`
  (quality/rtsp/identity) and `/mnt/sdcard` (timelapse frames, `.avi`,
  `.timelapse_videos.csv`) live in the tmpfs upper layer and are discarded on every reboot.
  `CameraState` settings (quality tier, camera name, snapshot/timelapse intervals and enables,
  RTSP/WebRTC modes) were memory-only.
- **Design:** a new 4 GB ext4 partition (`mmcblk0p3`, label `PERSIST`, PARTUUID `46f0d7c3-03`)
  mounted at `/data`; `/data/prusa-cam/state.json` holds the runtime settings and `/data/sdcard`
  is bind-mounted onto `/mnt/sdcard` (SMB keeps sharing `/mnt/sdcard` unchanged).
  `quality.live.env` stays ephemeral (GAP-QUALITY-02).
- **No-op rule (safe pre-deploy):** `settings_store.available()` is true only when `/data` exists
  and `os.path.ismount('/data')`; otherwise `save()` returns False without writing, `load()`
  returns `{}` for the missing file, `main._save_persisted_state` logs at debug, and
  `persist_restore.main` logs and exits 0. Nothing is created under a missing `/data`.
  `pi-persist.service` also carries `ConditionPathIsMountPoint=/data`, so before the partition
  exists the unit is *skipped* rather than reported failed.
- **Settings schema:** `version`, `quality_tier` (1/2/3), `camera_name`, `snapshot_interval`
  (10..600), `snapshot_upload_enabled`, `timelapse_interval` (1..3600), `timelapse_enabled`,
  `timelapse_fps` (1..30), `rtsp_mode` (1/2), `webrtc_mode` (0/1). `save` is atomic (same-dir
  temp + `os.replace`) and corrupt files are quarantined to `<path>.bad`.
- **Wiring:** `main.py` loads `state.json` at startup and `state.apply_persisted` applies only
  present/valid keys (reusing the setters where they exist); every successful mutation in
  `handle_event` (camera name, snapshot/timelapse intervals and enables, quality persist path,
  RTSP/WebRTC modes) calls `_save_persisted_state`.
- **Restore:** `persist_restore.py` (root, `pi-persist.service`, `RequiresMountsFor=/data`,
  `Before=` the three camera units) ensures `/data/{sdcard,prusa-cam}`, chowns to `SERVICE_USER`,
  bind-mounts the store, re-materializes `quality.env`/`rtsp.mode` from `state.json`, and prunes
  the oldest `/data/sdcard/timelapse/*.jpg` frames when `/data` free space is below 300 MB
  (`.avi`/CSV never deleted). `deploy.sh` installs+enables the unit and activates the fstab entry
  only when `/dev/mmcblk0p3` exists (derives the real PARTUUID, skips when already present).
- **Tests:** `tests/test_pi_settings_store.py` (round-trip, version, missing/corrupt→`.bad`,
  unavailable no-op, atomic-on-replace-failure), `tests/test_pi_state.py::PersistedStateTests`
  (contents + valid/invalid apply), `tests/test_pi_timelapse.py::MainTimelapseWiringTests`
  (AST: `_save_persisted_state` defined and called ≥6× in `handle_event`; startup
  loads/applies), `tests/test_pi_persist_restore.py` (`frames_to_prune` oldest-first,
  `quality_env_values`, import safety).
- **Live verification 2026-09-20:** the SD was repartitioned offline (p2 → 10.3G, `mmcblk0p3` 4G
  ext4 LABEL `PERSIST` PARTUUID `46f0d7c3-03`), then a deploy added
  `PARTUUID=46f0d7c3-03 /data ext4 defaults,noatime 0 2`. Verified after a real reboot:
  `findmnt /data` = `/dev/mmcblk0p3`; `/mnt/sdcard` = `/dev/mmcblk0p3[/sdcard]` (bind);
  `pi-persist.service` `Result=success`; `state.json` (`{"quality_tier": 2}`) restored → `quality.env`
  = `1280x720` → `rpicam-vid … --width 1280 --height 720` (the encoder really starts at the
  persisted resolution), and the app logged `Loaded persisted settings: quality_tier`; frames +
  `.avi` + `.timelapse_videos.csv` written under `/mnt/sdcard/timelapse` survived the reboot and
  `file_list_entries()` still returned the `.avi`; `smbd` active with `[sdcard]` `force user =
  bullitt`; `quality.live.env` was absent after the reboot (still ephemeral, GAP-QUALITY-02).
- **Remaining:** none for the persistence scope. Optional follow-ups: an in-app live UI check of
  the save wiring (only the AST tests cover the mutation call sites today), and a `dosfsck`/fsck
  note for the new partition (ext4, journaled, fsck order 2 in fstab).

### GAP-DEVICE-01 — Reboot command behavior

- [~] **P2 · Implemented; live reboot unverified**
- **Firmware behavior:** remote reboot trigger reboots the device and reports the appropriate result
  before disconnect. **[confirmed]**
- **Current behavior:** advertises `CameraReboot` but does not dispatch the trigger.
- **Connect impact:** the Connect reboot control silently fails.
- **Implementation choice:** either authorize a narrowly scoped systemd reboot path with rate
  limiting and acknowledgment, or remove the advertised capability. Owner chose implement.
- **Acceptance:** command is authenticated, rate-limited, acknowledged, and invokes only the intended
  reboot action in an integration harness.
- **Implementation (staged, commit pending):** [`device_control.py`](../pi-impersonator/device_control.py)
  holds a pure predicate `can_reboot(last, now, min_interval)` and `request_reboot(state, reboot_fn,
  now=None)`, which records the accepted monotonic time on the shared `CameraState`
  (`state.last_reboot_monotonic`) before invoking the injected `reboot_fn`. The minimum spacing is
  `DEFAULT_REBOOT_MIN_INTERVAL_SECONDS = 60` (long enough for the Pi to drop the Socket.IO
  connection and come back). `main.py`'s dispatcher sends `trigger.REBOOT` through
  `request_reboot` with a narrowly scoped `reboot_device()` that runs only
  `['sudo','systemctl','reboot']`; the outcome is logged and success is never faked. A second
  request inside the window, a `False` return, and a raised exception all return `False` without
  rebooting. Tests: `test_pi_device_control.py` (`RebootGuardTests`, `MainWiringTests`).
  **Remaining:** the live
  reboot is obviously unverified, and the firmware-style per-action `client_trigger` result code
  (GAP-SIO-01) is still not sent.

### GAP-DEVICE-02 — IR, speaker, fan and MicroSD feature truthfulness

- [~] **P2 · Partially implemented; `camera_status` descriptor now confirmed, tag binding unresolved**
- **RE 2026-09-20 (Wave 1, direct 3.1.6 decompiler/ELF) [confirmed]:** the `camera_status`
  descriptor is **`0x3f6cd0`** (6 fields): 1 uvarint @0x00, 2 submsg @0x08, 3 uvarint @0x10,
  4 uvarint @0x14, 5 uvarint @0x18, 6 uvarint @0x1c. The **tag -> hardware binding**
  (IR/speaker/fan/MicroSD) still needs the sender `FUN_000a1394` and remains **unresolved**;
  keep the hardcoded IR/speaker bytes pinned rather than guessed.
- **Firmware behavior:** applies IR/day-night mode, speaker volume, fan control, and MicroSD status
  where hardware supports them. **[confirmed]**
- **Current behavior:** advertises these capabilities; IR is logged and ignored, and the others have
  no control path.
- **Connect impact:** controls can be displayed but never work; status values may imply nonexistent
  hardware.
- **Implementation:** remove unsupported hardware capabilities unless Buddy classification requires
  them; otherwise return truthful unavailable state rather than fake successful application.
- **Acceptance:** Connect UI and status expose only supportable operations, with no silent success.
- **Implementation (staged, commit pending):** `CameraState` now carries explicit
  `ir_available`/`speaker_available`/`fan_available`/`microsd_available = False` and
  `ir_mode = None`. [`device_control.py`](../pi-impersonator/device_control.py)
  `apply_light_control` maps the recovered `configuration.light_control` values
  (`auto`/`day`/`night` -> 1/2/3, `FW-CONFIG:193-228`), logs that the Pi has no IR illuminator,
  returns `False`, and leaves `ir_mode` unavailable instead of claiming the mode was applied;
  `main.py`'s configuration handler routes `light_control` through it. Tests:
  `test_pi_device_control.py` (`HardwareAvailabilityTests`). **Limitation:** the nested
  `camera_status` tag-to-field mapping remains `descriptor required`, so the hardcoded IR/speaker
  bytes in [`status.py`](../pi-impersonator/status.py) are left unchanged rather than guessed
  (pinned by `test_status_hardware_bytes_unchanged_pending_descriptor`). **Correction:** capability
  removal under GAP-CAP-01 **is** done — `IrMode`/`SpeakerVolume`/`FanControl` were pruned from the
  advertised features (while `MicroSd` is kept for the emulated SD), so Connect no longer exposes
  those controls even though the status bytes remain pinned.

### GAP-DEVICE-03 — Host stability: unexpected reboot + thermal throttling

- [~] **P2 · Investigated 2026-09-19; forensics + journal-flood mitigations deployed, hardware action open**
- **Reboot (~21:07 2026-09-19):** cause **undeterminable** — the reboot happened with the read-only
  overlay active, so journald (volatile), `/tmp`, wtmp and cloud-init all wrote to the RAM upper
  layer and were discarded; there is no RTC (boot clocks jumped +51/+88 min on NTP sync), and no
  `pstore`/ramoops. Current-boot `dmesg` shows no panic/oops, no under-voltage, no OOM, no mmc/ext4
  errors. Most likely an **external power cut** (the Pi is printer-powered by design per
  `CLAUDE.md`) or a **1-minute hardware-watchdog reset** (`RuntimeWatchdogUSec=1min`, `get_rsts=20`,
  non-default but not authoritatively decodable).
- **Throttling (high confidence, thermal):** `get_throttled` = `0x60002` → bit1 ARM frequency capped
  **now**, bit17 freq-cap latched, bit18 hard-throttle latched; **bits 0/16 (under-voltage) clear**.
  Temperature 81–84 °C continuously with **no cooling device**, vs a 85 °C `temp_limit`; real ARM
  clock 672–941 MHz against a 1 GHz request. Cause: 1080p30 H.264 (`rpicam-source`) + `main.py` +
  `stream_mux` on a passively cooled Pi Zero 2 W. Not destructive, but degrades streaming jitter.
- **Aggravators:** `systemd-remount-fs.service` fails in a loop (`overlay: No changes allowed in
  reconfigure`), which also fails the zram/swapfile units → **zero swap** on a 415 MB device.
- **Mitigations deployed (`bootlog.sh` + `systemd/bootlog.service`, `rpicam-source.service`):**
  a oneshot writes `date`/uptime/`get_rsts`/`get_throttled`/temp/dmesg to the real vfat
  `/boot/firmware/bootlog.txt` each boot (survives reboots); `rpicam-vid -v 0` stops the ~30 lines/s
  frame stats that evicted `prusa-cam` logs from the volatile journal within minutes.
- **Open (hardware/ops):** fit a heatsink/fan or reduce the stream (720p/15 fps); repair the failing
  swap/remount-fs units; keep an off-box logger (netconsole/serial) if a reboot must be captured
  precisely. Confirm the next reboot from `/boot/firmware/bootlog.txt`.

### GAP-WEBRTC-07 — Decide audio-track compatibility

- [ ] **P2 · Open; optional-path verification required**
- **Firmware behavior:** contains WebRTC audio support including AAC-HBR/MPEG4-GENERIC at 48 kHz
  stereo, with G.726, PCMA, and PCMU paths also present. The recovered primary video profile remains
  H.264 constrained baseline. **[confirmed in firmware; whether current Connect offers require audio
  is not yet verified]**
- **Current behavior:** publishes a video-only WebRTC pipeline and cannot accept or answer an audio
  media section with a microphone track.
- **Connect impact:** none if Connect deliberately negotiates video-only; otherwise SDP negotiation or
  an expected listen/talk feature may be incomplete.
- **Implementation choice:** capture and inspect a genuine current Connect offer before adding audio.
  If audio is optional, document video-only as intentional; if required, add the negotiated firmware
  codec and a truthful hardware capability path.
- **Acceptance:** a real or canonical offer negotiates successfully with the same accepted/rejected
  media sections as firmware.
- **Code:** [`webrtc.py`](../pi-impersonator/webrtc.py#L66-L98)

## P3 — parity and diagnostics

### GAP-IDENTITY-01 — Firmware fallback when `wlan0` MAC retrieval fails

- [~] **P3 · Implemented and live-verified (config precedence)**
- **Firmware behavior:** formats `wlan0` MAC as uppercase colon-separated text and hashes it with MD5;
  if MAC retrieval fails, it generates a random ten-character seed and hashes that. **[confirmed]**
- **Current behavior:** exact normal MAC path is implemented, but a missing/invalid `wlan0` MAC raises
  and prevents startup.
- **Connect impact:** no effect on the target Pi while `wlan0` exists; affects recovery or alternate
  hardware. A newly generated fallback also requires a newly paired token or stable persistence.
- **Implementation:** decide whether to reproduce and persist a fallback identity or fail closed with
  a clear diagnostic; never silently rotate a fingerprint bound to an existing token.
- **Acceptance:** normal-path vectors remain exact; failure behavior is deterministic and documented.
- **Code:** [`identity.py`](../pi-impersonator/identity.py),
  [`main.py`](../pi-impersonator/main.py#L120-L127)
- **Implementation (staged, commit pending):** [`identity.py`](../pi-impersonator/identity.py) adds
  `fingerprint_from_seed` (lowercase MD5 of the exact seed text), `generate_fallback_seed`, and
  `load_or_create_fallback_seed`, which persists the seed at `/etc/prusa-cam/identity.fallback`
  (`PRUSA_IDENTITY_FALLBACK` override) and reuses a valid existing seed verbatim so a bound token's
  fingerprint is never silently rotated. `main.get_network_info` now resolves the fingerprint via
  `identity.resolve_fingerprint`: an explicit `config.ini` `[identity] fingerprint` wins (the value
  the registered token is bound to; live-verified 2026-09-18), then the MAC derivation, then this
  persisted seed. It reports an empty MAC when none is read. **Assumption:** the exact firmware alphabet
  of `FUN_000997f8(..., 10, 1)` is unrecovered; alphanumeric is used. On the read-only overlay the
  seed survives only after deployment, so a restart without the file regenerates it (with a warning).
  Tests: `test_pi_identity.py` (`FallbackSeedTests`).

### GAP-IDENTITY-02 — Deploy exact fingerprint only with a fresh token

- [x] **P3 · Resolved 2026-09-20: already using the `wlan0`-MAC-derived fingerprint (token bound; live ACK 0)**
- **Live state 2026-09-20 (verified):** the deployed `config.ini` has **no `[identity] fingerprint`**
  line and there is **no fallback-seed file**, so `identity.resolve_fingerprint` derives from `wlan0`
  (`d8:3a:dd:32:1c:ac`) → `md5("D8:3A:DD:32:1C:AC")` = `142486ddfee8889f2eb8de723411221d`. Auth
  succeeds (`camera_authentication` ACK `0`) and `/c/info` is `200` — i.e. **the token is already
  bound to the MAC-derived fingerprint**, so the firmware-equivalent identity is in place and **no
  migration is needed**. The runbook below applies only if the fingerprint is ever changed.
- **RE 2026-09-20 (Wave 1, direct 3.1.6 decompiler/ELF) [confirmed]:** `FUN_00096cd8` is
  `generateFingerPrint`; it reads **`wlan0`** via `SIOCGIFHWADDR` (`FUN_00097e78`), formats
  `"%02X:%02X:%02X:%02X:%02X:%02X"` (uppercase, colon-separated, 17 chars), and hashes with MD5
  lowercase hex (`FUN_00097a4c`); the fallback is a **random 10-char seed** when `wlan0` is
  unavailable. The fingerprint is **bound to the token server-side on first contact**; changing it
  later yields `400 {"detail":"Invalid fingerprint"}` / `403` — recovery requires a fresh token.
- **Firmware/source behavior:** lowercase MD5 of exact uppercase `AA:BB:CC:DD:EE:FF` text.
  **[confirmed]**
- **Deployment state:** the deployed token is bound to the **static fingerprint in
  `config.ini`**; the source now honors that value (`identity.resolve_fingerprint`) so the
   registered identity keeps working. **Live-verified 2026-09-18:** `/c/info` → `200`
   (`origin='OTHER', registered=True`), snapshots → `200`, `camera_authentication` ACK `0`
   (success; the earlier note recording `1` was a misread — `1` is "not authorized"). A
  deploy that switched to the MAC-derived fingerprint instead caused
  `400 {"detail":"Invalid fingerprint"}` / `403`; the configured value was restored.
- **Implementation/operation (optional):** the step-by-step migration runbook is in
  [`next-steps.md`](next-steps.md) ("Identity migration runbook"): read
  `/sys/class/net/wlan0/address`; mint a fresh Connect token via the Buddy3D add-camera flow
  (`origin: OTHER`); remove `[identity] fingerprint` from `config.ini` and set the fresh token;
  deploy overlay-aware (`PI=user@host ./deploy.sh`); verify `/c/info` 200 (`origin=OTHER`,
  `registered=True`), `PUT /c/snapshot` 200, `camera_authentication` ACK `0`, and the
  `X-Camera-Fingerprint` header = `md5("<UPPERCASE:COLON:MAC>")`.
- **Acceptance:** `/c/info`, snapshot, and `camera_authentication` all succeed using the derived
  fingerprint under the fresh token; then repeat the viewer registry/auth checks.

### GAP-IDENTITY-03 — Track the physical Wi-Fi MAC/OUI difference

- [x] **P3 · Closed 2026-09-20: documented per-device difference, not a defect; OUI not inspected and the registry-gate theory is superseded**
- **Resolution 2026-09-20:** the Pi reports and hashes its real `wlan0` MAC (`d8:3a:dd:32:1c:ac`), so
  it exposes a Raspberry-Pi OUI — the same algorithm the firmware uses, on different hardware. The
  firmware never inspects the OUI (it hashes whatever `SIOCGIFHWADDR` returns), and the only place it
  could conceivably have mattered — the Connect camera-service registry gate — is **superseded**
  (WebRTC works live in the app and browser, GAP-WEBRTC-03). **No action.** Keep the experiment note
  below only if a genuine Buddy3D MAC/OUI ever becomes available from reliable evidence.
- **RE 2026-09-20 (Wave 1, direct 3.1.6 decompiler/ELF) [confirmed]:** the firmware does **not**
  inspect the OUI. It formats and hashes whatever `wlan0` reports, so the Pi-vendor OUI is an
  expected per-device difference, **not a defect**. The `smsc95xx` cmdline MAC is the `eth0`
  placeholder and is irrelevant to this path.
- **Firmware behavior:** reports and hashes the genuine camera's `wlan0` MAC. Buddy3D hardware uses a
  Realtek Wi-Fi chipset, but no authoritative genuine-camera OUI has been recovered. **[confirmed for
  MAC source/chipset; OUI unknown]**
- **Current behavior:** reports and hashes the Raspberry Pi's real `wlan0` MAC, therefore exposing a
  Pi-vendor OUI even though the derivation algorithm now matches firmware.
- **Connect impact:** normally a per-device MAC difference is expected. It remains a narrowly scoped
  enrollment hypothesis only because the camera-service registry gate is unexplained; the only
  untested place it could matter is the Connect registry gate, which stays open pending a
  genuine-OUI source.
- **Implementation/experiment:** first test the exact Pi MAC-derived fingerprint with a fresh token.
  Only test a spoofed OUI if a genuine Buddy3D MAC/OUI is obtained from reliable evidence; keep MAC,
  reported metadata, fingerprint preimage, and fresh token mutually consistent.
- **Acceptance:** record the exact redacted test preimage/OUI class, fresh-token binding, `/c/info`
  result, camera registry lookup, and viewer-auth ACK so the experiment is reproducible.

### GAP-STATUS-03 — Verify remaining status subfields against fixtures

- [~] **P3 · Nested schema recovered from the descriptor and three mismatches fixed**
- **Implementation (WP-8):** dumped `CameraInfoMessage` @ `0x3f6e98` and its submessages and fixed three wire-type/tag mismatches in `status.py`: `timelapse_status` (descriptor `0x3f753c`) tag6 = fixed32/float, tag7 = uvarint (were swapped); `extended_status.4` (descriptor `0x3f72b0`) tags 1-4 uvarint + tag 5 string — the block is the SD storage block, not a model string (the earlier "model on tag 5" reading was corrected in GAP-TIMELAPSE-01); `extended_status.6` (descriptor `0x3f7278`) RTSP URL on tag 3 (was 4). Tests: `tests/test_pi_status_schema.py`. Live-verified: `/c/info` 200, snapshots 200, status sent with no errors.
- **Still open:** a golden fixture captured from a genuine 3.1.6 status (not available offline) and the semantic annotation of every remaining nested tag.
- **Firmware behavior:** sends the recovered top-level fields 2, 3, 4, 5, 8, 9, 10 conditionally,
  and 11, with many nested values obtained from actual services/configuration. **[confirmed]**
- **Current behavior:** top-level structure is substantially reconstructed, but several nested values
  are inferred/defaulted, an empty second network submessage is emitted, and endpoint/timezone fields
  have not been compared byte-for-byte with a current real-camera payload.
- **Connect impact:** probably secondary because authentication and `/c/info` already work, but hidden
  classification or UI logic could inspect these values.
- **Implementation:** create canonical decoded fixtures from the decompiled assignment paths and, if
  obtainable, a redacted real-camera status capture; document every intentionally different hardware
  telemetry value.
- **Acceptance:** schema/field-presence comparison has no unexplained field, type, or semantic
  differences.
- **Code:** [`signaling.py`](../pi-impersonator/signaling.py#L214-L304)

### GAP-STATUS-04 — Timezone representation

- [~] **P3 · Implemented and live-verified**
- **Firmware behavior (WP-8):** `FUN_000b1dc8` detects the timezone from the web API (`timezone.prusa3d.com`, JSON `timezone` when `status == success`, follows a 301 `Location`), `FUN_000b1620` swaps the `UTC+`/`UTC-` prefix into the POSIX form, `FUN_000b170c` writes `/etc/TZ` (64-char cap), and `FUN_000b130c` reads it back for status tag 5.10.1.
- **Implementation:** `timezone.py` (detect/parse/convert/read/write), `CameraState.tz_name`, `main.detect_timezone` (runs at startup over the shared aiohttp session), `status` reports the detected value; `write_tz_file` falls back to the unit's passwordless `sudo tee` so `/etc/TZ` is actually written. Tests: `tests/test_pi_timezone.py`.
- **Live-verified 2026-09-19:** `timezone: API 'UTC+2' -> reported 'UTC-2'`, `/etc/TZ` = `UTC-2`, status sent, `/c/info` 200.
- **Still open:** golden capture from a genuine 3.1.6 device (not available offline); non-UTC± (IANA) values pass through unchanged.
- **Firmware behavior:** detects timezone through its configured/web timezone service and reports
  firmware state. **[confirmed at service level; exact status string format needs fixture]**
- **Current behavior (implemented, live-verified 2026-09-19):** detects the timezone from the web
  API, converts it, writes `/etc/TZ`, and reports the detected value (`UTC+2` → `UTC-2`).
- **Connect impact (resolved):** representation matches the service result.
- **Before (superseded):** sent `time.tzname[0]`, commonly an abbreviation such as `CET`/`CEST`,
  plus a fixed status value.
- **Implementation:** confirm whether firmware reports an IANA name, abbreviation, or service result
  before changing the field.
- **Acceptance:** representation matches a real-camera or assignment-path fixture for the same zone.

### GAP-NETWORK-01 — Verify Wi-Fi signal conversion and secondary network block

- [~] **P3 · Signal conversion fixed to the firmware formula**
- **WP-8/network:** recovered `FUN_00097b38`: the input is the **RSSI (dBm)** `level` column of `/proc/net/wireless`, converted as `rssi==0 || rssi<-99 -> 0`, `rssi>-51 -> 100`, else `(rssi+100)*2`. `network.py` implements `rssi_to_quality`/`parse_wireless_level`/`signal_quality_from_wireless`, and `signaling._signal_quality` now uses it instead of the previous linear 0–70 link-quality mapping. Tests: `tests/test_pi_network.py`.
- **Still open:** a golden live status capture to confirm the exact value against a genuine camera; the empty secondary network submessage stays omitted (fixed earlier).
- **Firmware behavior:** reports current WLAN identity/address/signal and has descriptor space for
  additional network state. **[confirmed]**
- **Current behavior (implemented):** `network.signal_quality_from_wireless` uses the recovered
  firmware formula on the RSSI (dBm) level column, and the empty secondary network submessage is
  omitted.
- **Connect impact (resolved):** signal telemetry matches the firmware conversion; no spurious empty
  submessage.
- **Before (superseded):** mapped `/proc/net/wireless` quality linearly from 0–70 to 0–100 and emitted
  an empty field-2 network submessage.
- **Implementation:** trace the firmware signal conversion and the field-2 presence condition; omit
  the empty optional submessage if firmware omits it.
- **Acceptance:** field presence and signal values match a controlled RSSI/quality fixture.

### GAP-HTTP-03 — Reuse HTTP connections

- [~] **P3 · Implemented; live verification pending**
- **Firmware behavior:** long-running services reuse their HTTP/curl context and maintain service
  state. **[confirmed at architecture level]**
- **Current behavior (implemented):** `upload.make_session()` builds one bounded-timeout
  `aiohttp.ClientSession` that `main` creates once, passes to every request path, and closes in a
  `finally`; the request functions never build their own.
- **Connect impact (resolved):** connections are reused; live reuse/close-recovery still needs the
  Pi.
- **Before (superseded):** created a new `aiohttp.ClientSession` for each info and snapshot request,
  causing extra TLS handshakes and connection churn.
- **Implementation:** own one session for the application lifetime with bounded timeouts and clean
  shutdown.
- **Acceptance:** repeated uploads reuse connections and recover after server-side close.
- **Implementation (staged, commit pending):** `upload.make_session()` builds the single
  `aiohttp.ClientSession` with bounded `ClientTimeout`; `main` creates it once and passes it to
  `upload_snapshot`, `upload_info`, the info service loop, snapshots, and OTA, closing it in a
  `finally`. `upload_snapshot`/`upload_info` never construct a session. Tests:
  `test_pi_capture_http.py::SessionReuseTests` (AST assertion). Live connection-reuse/close
  recovery still requires the Pi.

### GAP-SIO-01 — Firmware-style error and progress messages

- [~] **P3 · Sender, descriptor, and emit mechanism confirmed; tag semantics still unresolved**
- **WP-6 partial (offline):** the generic sender is `FUN_000a2754`, confirmed by the emitted event name `client_trigger` and the 6-field descriptor `0x3f6f58`. It populates only a subset of the message: a constant (`DAT_000a2ab4`), the result of `FUN_0008286c` (token-shaped), and the result of `FUN_0009f69c(param_1)` (request-id-shaped); the ack callback is `DAT_000a2ae0/ae4`. Which tag carries the result/error/progress code is not yet mapped — do not guess.
- **RE 2026-09-20 (Wave 1, direct 3.1.6 decompiler/ELF) [confirmed]:** descriptor `0x3f6f58`
  = 6 fields (strings 1/2/4, uvarints 3/5/6). **The emit mechanism is a Socket.IO EVENT
  (`client_trigger`), not an ack**; the ack callbacks are the server's receipt ack. Nine sender
  wrappers were recovered (error codes: SD not mounted / SD RO / low space / timelapse SD not
  mounted / RTSP start failed; FW upgrade progress 1..6; timelapse-video-make status 1
  IN_PROGRESS / 2 FINISHED). **Which tag carries code vs progress vs message is unresolved**
  `[assumption]`; do not implement a wire format yet.
- **Firmware behavior:** uses `client_trigger` variants for generic result/error codes, OTA progress,
  and timelapse-video progress. **[confirmed]**
- **Current behavior:** never emits `client_trigger`.
- **Connect impact:** Connect receives no structured outcome for failed or asynchronous commands.
- **Implementation:** recover subtype fields/enums and centralize success/error/progress emission.
- **Acceptance:** each implemented asynchronous/control command emits the expected lifecycle.

## Confirmed matches — do not re-open without contrary evidence

- [x] Connect, not firmware, creates the random 20-character alphanumeric pairing token. Firmware
  consumes and persists it unchanged.
- [x] Camera authentication fields: fingerprint field 1, token field 2.
- [x] Fingerprint normal path: lowercase MD5 hex of uppercase colon-separated `wlan0` MAC text.
- [x] Snapshot and info paths: `PUT /c/snapshot` and `PUT /c/info`.
- [x] Snapshot/info identity headers: short `Token` and `Fingerprint` names.
- [x] OTA path and prefixed `X-Camera-*` header names.
- [x] `/c/info` nesting: camera attributes under `config`, current resolution under
  `options.available_resolutions`, `capabilities` and `features` as arrays.
- [x] `/c/info` currently sends one available-resolution entry, matching the recovered firmware build
  path rather than the older three-entry assumption.
- [x] Model/manufacturer/firmware/protocol constants: `Buddy3D-C1`, `Niceboy`, `3.1.6`, `4.4`.
- [x] Motorless feature list contents and ordering. `RotationX`/`RotationZ` are conditional in
  firmware and correctly absent for the impersonated C1.
- [x] `protobuf_version`: token field 1, version `4.4` field 2, optional request ID field 3.
- [x] `features` field 7 is the MD5 of the bracket-wrapped feature JSON, as confirmed by the firmware
  hash-building path and prior real-camera capture. It is **not** a second protocol-version field;
  stale documentation saying otherwise must not drive implementation.
- [x] Camera-side WebRTC numeric enum: `1=request`, `2=answer`, `3=offer`, `4=candidate`.
- [x] Inbound WebRTC offer uses client ID field 3 and SDP field 4.
- [x] Outbound WebRTC answer/candidate uses request ID field 1, numeric type field 2, payload field 3.
- [x] Quality dimensions: protobuf enum `1=640×480`, `2=1280×720`, `3=1920×1080`.
- [x] Raw quality command mapping: firmware uses `5=SD`, `6=HD`, `7=FHD`; implemented in `state.py`/`quality_control.py` (`d1ec311`).
- [x] Trigger message descriptor `0x3f6f14` (13 fields) and the `FUN_000a963c` per-field dispatch, recovered 2026-09-18 and implemented in `trigger.py` (`9291968`).
- [x] `ClientTrigger` descriptor `0x3f6f58` types (strings 1/2/4, uvarints 3/5/6); semantics still unresolved.
- [x] Timelapse file-list descriptor `0x3f701c`: event `file_list`, field 1 = `"<page>;<total>\n<chunk>"`, field 2 = HTTP token, field 3 = request_id (optional), field 4 never set; an empty list sends nothing.
- [x] H.264 intent: constrained baseline, level 3.1, packetization mode 1.
- [x] Default snapshot interval is 10 seconds.
- [x] `set_rtsp_server_mode` direct values handled as `1=disabled`, `2=enabled`.

## Verification work required

The existing automated tests cover identity derivation and the basic camera-side WebRTC envelope
only. Add these suites as gaps are closed:

- [ ] Canonical protobuf fixtures for authentication, status, version, features, trigger,
  configuration, `client_trigger`, WebRTC ICE config, and connection info.
- [ ] Golden `/c/info` fixture and consistency checks against shared runtime state.
- [ ] HTTP mock tests for success, blocking/throttling, redirects, timeout, and retry.
- [ ] Stateful command tests covering trigger dispatch, interval, snapshot enable, RTSP, WebRTC,
  quality change/save, and unsupported-feature responses.
- [ ] WebRTC integration test using a local signaling/ICE harness, including forced TURN relay.
- [ ] Single-camera-owner test with concurrent snapshot, RTSP, and WebRTC consumers.
- [ ] Redacted live comparison against a genuine 3.1.6 camera if access becomes available.
- [ ] Fresh-token live test of the exact MAC-derived fingerprint and subsequent registry/viewer gate.

## Suggested implementation order

1. Recover typed trigger and configuration descriptors; add golden fixtures.
2. Introduce one shared runtime/configuration state and make status truthful.
3. Implement trigger actions for snapshot control, status/features/version, RTSP, and quality.
4. Feed WebRTC from the shared H.264 source and implement reliable teardown.
5. Parse/apply ICE/TURN, relay policy, TTL, FPS, plan, scope, and scoped quality.
6. Emit WebRTC connection information and `client_trigger` result/error messages.
7. Decide capability policy for OTA, timelapse, reboot, IR, speaker, fan, and MicroSD.
8. Align remaining parameters: raw quality mapping/persistence, RTSP port, JPEG quality, HTTP
   `Expect`, timezone, and network telemetry.
9. Migrate the live deployment to the firmware-derived fingerprint with a fresh token and repeat the
   camera registry/viewer-auth test.

## Firmware evidence anchors

The binary and decompilation remain outside this repository for copyright reasons. Reproduction and
address notes live in [`reverse-engineering.md`](reverse-engineering.md), with the 3.1.6 delta in
[`firmware-3.1.6.md`](firmware-3.1.6.md). Key recovered paths used by this tracker include:

| Behavior | Firmware 3.1.6 anchor |
|---|---|
| Camera status construction | `FW-STATUS` — `000a1394__FUN_000a1394.c:133-499` |
| Supported-feature construction/hash | `FW-FEATURES` — `000a8ed0__FUN_000a8ed0.c:76-282` |
| `/c/info` JSON/HTTP construction | `FW-INFO-BUILD` — `00062d74__FUN_00062d74.c:64-329` |
| `/c/info` dirty/retry loop | `FW-INFO-LOOP` — `00063bfc__FUN_00063bfc.c:23-94` |
| Quality apply/persistence flag | `FW-QUALITY-DIRECT` — `00072f08__FUN_00072f08.c:16-115` |
| Quality enum/raw conversion | `FW-QUALITY-PB` — `000a76c8__FUN_000a76c8.c:16-79` |
| Quality dimensions | `FW-QUALITY-DIMS` — `0007d7c4__FUN_0007d7c4.c:10-28` |
| Fingerprint seed/fallback | `FW-ID-SEED` — `00096cd8__FUN_00096cd8.c:17-76` |
| MAC retrieval/formatting | `FW-ID-MAC` — `00097e78__FUN_00097e78.c:23-54` |
| Fingerprint MD5/hex conversion | `FW-ID-MD5` — `00097a4c__FUN_00097a4c.c:24-49` |
| WebRTC answer/candidate sender | `FW-WEBRTC-SEND` — `000a3e90__FUN_000a3e90.c:5-166` |
| WebRTC offer gate/session enqueue | `FW-WEBRTC-GATE` — `000b996c__FUN_000b996c.c:39-107` |
| WebRTC enable/disable behavior | `FW-WEBRTC-MODE` — `000b94ac__FUN_000b94ac.c:16-52` |

When closing an item, record the implementing commit, tests, live verification date if applicable,
and whether the conclusion is **confirmed** or remains an **assumption**.
