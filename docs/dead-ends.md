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

## Symptom still open

- After all corrections, the Prusa app can still show **"Kamera-Kommunikation fehlgeschlagen"**
  (camera communication failed) in some states even though auth + `/c/info` succeed. The
  remaining gap is in the live WebRTC/signaling handshake, not the info upload. See
  [`status.md`](status.md) and [`next-steps.md`](next-steps.md).
