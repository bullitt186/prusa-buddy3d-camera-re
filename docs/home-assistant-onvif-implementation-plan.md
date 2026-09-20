# Home Assistant ONVIF integration plan

Status: implemented, host-validated, and deployed to the Pi on 2026-09-20. Camera-side live checks
passed; actual Home Assistant entity setup plus concurrent Prusa WebRTC and quality-change checks
remain open. This document is the implementation and verification handoff for another coding agent.

Host verification on 2026-09-20: all 339 repository tests, `compileall`, shell syntax checks, and
`git diff --check` pass. A disposable `onvif-zeep-async` 4.2.1 / Zeep 4.3.3 client also completed
`update_xaddrs`, Device information/interfaces/time, Media capabilities/profiles, `GetStreamUri`,
and `GetSnapshotUri` against the facade. Home Assistant's exact `WSDiscovery` 2.1.2 dependency also
found the facade with its type/scope filter and parsed the expected XAddr, name, hardware, and MAC
scopes. This is protocol-client evidence, not Pi or Home Assistant live acceptance.

Partial live evidence on 2026-09-20 (deployment of `312b59b`): the overlay-aware deployment
completed, required services were active after reboot, Connect `/c/info` and snapshots returned 200,
and Socket.IO authentication returned ACK 0. The pinned `WSDiscovery` client found exactly one
camera at `http://192.168.0.162:80/onvif/device_service` with the expected name/hardware/MAC scopes.
SOAP device information and the JPEG endpoint worked; ports 8554 and 8555 both exposed H.264
1280×720. During a sustained 25-second connection to port 8555, Connect snapshots continued at
10-second cadence. The first deploy verifier queried ONVIF before the application's 41-second startup
completed; `deploy.sh` now polls for up to 60 seconds. No actual HA config entry was created and no
Prusa WebRTC/quality-change concurrency test was run, so the remaining live criteria stay open.

## Goal

Make the impersonator easy to add through Home Assistant's built-in ONVIF integration while
preserving every existing Prusa Connect, Prusa app, RTSP, WebRTC, quality-control, persistence, and
overlay behavior.

The implementation must satisfy two independent requirements:

1. Home Assistant gets a stable, always-available H.264 RTSP URL and JPEG snapshot URL without
   owning the camera sensor or changing Prusa's stream mode.
2. Home Assistant discovers the camera through standard ONVIF WS-Discovery and can also be
   configured manually when multicast cannot cross the network boundary.

## Constraints and evidence

- Work against `GAP-SNAPSHOT-04` for the firmware-related concurrent snapshot behavior.
- The ONVIF facade is a Pi-only interoperability extension, not a claim about Buddy3D firmware and
  not an ONVIF certification claim.
- `rpicam-source.service` remains the only camera-sensor owner. RTSP, WebRTC, and JPEG capture must
  consume its existing `stream_mux.py` fan-out.
- Prusa's `prusa-rtsp.service` remains controlled exclusively by Prusa's RTSP mode on port 8554.
- The Home Assistant stream must not depend on Prusa's RTSP mode, app viewer state, or cloud
  connectivity.
- The Prusa token and fingerprint must never be returned by ONVIF, discovery, HTTP, logs, or scopes.
- Local edits/tests do not authorize deployment, reboot, token changes, backend changes, or firmware
  flashing. See `.agent/pi-ops.md` only after the user explicitly requests a live run.
- Preserve the newer `/data` persistence, `bootlog.service`, `pi-persist.service`, TURN quality lock,
  gated `webrtc_connection_info`, and no-unsolicited-post-auth behavior.

## Architecture

```text
                              +--> prusa-rtsp.service :8554/live
CSI camera -> rpicam-source -> stream_mux.py          (Prusa mode-controlled)
                              +--> prusa-ha-rtsp.service :8555/live
                              |                        (always on)
                              +--> JPEG capture -> /snapshot.jpg + Connect uploads
                              +--> WebRTC pipeline -> Prusa app/site

LAN client -> UDP 239.255.255.250:3702 -> WS-Discovery reply
Home Assistant -> HTTP :80 /onvif/device_service, /onvif/media_service
               -> RTSP :8555/live
               -> HTTP :80/snapshot.jpg
```

The dedicated port 8555 service is intentional. Reusing port 8554 would make Home Assistant
availability depend on a cloud-controlled mode, and making ONVIF start port 8554 would change
Prusa-visible state. A second `GstRtspServer` consumer is cheap because it reads the existing H.264
fan-out; it does not start a second encoder.

## Implemented file changes

### Independent Home Assistant RTSP service

- `pi-impersonator/rtsp_config.py`
  - Parse `RTSP_PORT`, `RTSP_PATH`, and `RTSP_LABEL`.
  - Preserve defaults `8554`, `/live`, and `Prusa` for the existing service.
  - Reject invalid ports and mount paths before GStreamer starts.
- `pi-impersonator/rtsp_server.py`
  - Consume the validated configuration without changing the existing pipeline/factory semantics.
- `pi-impersonator/systemd/prusa-ha-rtsp.service`
  - Run the same server on `8555/live`.
  - Require and start after `rpicam-source.service`.
  - Enable at boot and restart on failure.
  - Do not make it part of `rtsp_control.apply_mode()`.
- `pi-impersonator/quality_control.py`
  - Restart the shared source and HA RTSP endpoint after an accepted resolution change.
  - Use `systemctl try-restart prusa-rtsp.service`, so a disabled Prusa endpoint is not accidentally
    started.
  - Keep the existing TURN/scoped-quality lock before any restart or state write.
- `bootstrap.sh` and `deploy.sh`
  - Install, enable, restart, and verify the HA RTSP unit.
  - Retain overlay-aware and persistence logic.

### ONVIF device/media facade and discovery

- `pi-impersonator/onvif_facade.py`
  - Build and parse XML using only the Python standard library.
  - Expose one dynamic H.264 media profile (`profile_1`) using the shared state's camera name and
    resolution.
  - Implement the Device operations needed by Home Assistant: `GetServices`, `GetCapabilities`,
    `GetSystemDateAndTime`, `GetDeviceInformation`, `GetNetworkInterfaces`, and `GetScopes`.
  - Implement the Media operations needed by Home Assistant: `GetServiceCapabilities`,
    `GetProfiles`, `GetVideoSources`, `GetStreamUri`, and `GetSnapshotUri`.
  - Return SOAP faults for malformed, unsupported, or unknown-profile requests.
  - Advertise no PTZ, events, imaging, audio, or multicast capability.
  - Derive a stable endpoint UUID and serial from the normalized MAC. If the MAC is unavailable,
    use a UUID/pseudo-MAC derived from a stable seed, but never expose that seed.
  - Return `rtsp://<current-ip>:8555/live` and `http://<current-ip>/snapshot.jpg`.
  - Parse ONVIF WS-Discovery Probe/Resolve datagrams and return matching responses.
  - Use WS-Addressing 2004/08 with WS-Discovery 2005/04, declare the namespace used by the
    `dn:NetworkVideoTransmitter` QName, and include `AppSequence`; Home Assistant's pinned
    `WSDiscovery` parser requires those wire details.
  - Keep the discovery MAC scope byte-for-byte equal to `GetNetworkInterfaces().Info.HwAddress`, so
    automatic and manual setup resolve to the same Home Assistant unique ID.
- `pi-impersonator/onvif_discovery.py`
  - Join `239.255.255.250:3702` on the camera's current IPv4 interface.
  - Reply unicast to matching Probe/Resolve senders.
  - Drop malformed/unrelated traffic safely.
- `pi-impersonator/local_http.py`
  - Preserve `/` and `/snapshot.jpg`.
  - Add POST endpoints `/onvif/device_service` and `/onvif/media_service`.
- `pi-impersonator/main.py`
  - Construct ONVIF identity from the restored shared state and actual IP/MAC.
  - Start HTTP and WS-Discovery after normal state/config initialization.
  - Treat HTTP bind and WS-Discovery failures as local-feature failures: log them and continue
    Prusa Connect/App operation.
  - Close transports/runners during shutdown.

### Concurrent snapshots (`GAP-SNAPSHOT-04`)

- `CameraState.periodic_snapshot_allowed()` now depends only on the snapshot-upload enable flag.
- Periodic and explicit snapshots no longer check RTSP/WebRTC activity.
- WebRTC lifecycle still tracks/clears `state.streaming`; snapshot cadence is simply independent of
  that state.
- The existing monotonic start-to-start scheduling remains unchanged.

## Test concept

### Host unit tests (required on every change)

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q pi-impersonator tests
bash -n pi-impersonator/bootstrap.sh pi-impersonator/deploy.sh
git diff --check
```

Coverage expectations:

- `test_pi_rtsp_config.py`
  - legacy defaults remain `8554/live`;
  - HA environment produces `8555/live`;
  - invalid ports and paths fail closed;
  - HA systemd unit is independent and ordered after the source.
- `test_pi_onvif.py`
  - endpoint identity is stable and MAC-normalized;
  - fallback seed/credential-like input does not appear in rendered identity/scopes;
  - all supported Device and Media operations return parseable SOAP XML;
  - capabilities include only implemented functionality;
  - one H.264 profile reflects SD, HD, and FHD shared state;
  - stream/snapshot URIs use ports 8555/80;
  - malformed requests, unsupported actions, and bad profile tokens return faults;
  - matching Probe and Resolve requests work; unrelated/malformed discovery is ignored;
  - main wiring uses restored shared state and explicit snapshots have no streaming guard.
- `test_pi_state.py` and `test_pi_scheduling.py`
  - RTSP/WebRTC activity does not suppress periodic snapshots;
  - disabling snapshot uploads still suppresses them;
  - monotonic cadence behavior is unchanged.
- `test_pi_quality_control.py`
  - accepted changes restart source + HA RTSP and only `try-restart` Prusa RTSP;
  - failures propagate;
  - existing TURN-quality and persistence tests remain green.
- Existing signaling, WebRTC, RTSP control, persistence, identity, HTTP upload, and protocol tests
  are regression gates, not optional tests.

### Local protocol smoke tests (no device required)

- POST SOAP fixtures to a locally instantiated aiohttp app or call `soap_response()` directly.
- Parse every returned document with `xml.etree.ElementTree`.
- Confirm no configured token/fingerprint appears in any ONVIF/discovery response.
- Send Probe/Resolve fixtures with and without ONVIF types/scopes.

### Pi integration tests (requires explicit authorization)

1. Before deployment, record Prusa registration, snapshot cadence, RTSP mode, WebRTC mode, quality,
   and service states.
2. Deploy through `deploy.sh`; never bypass the overlay workflow.
3. Confirm `rpicam-source`, `prusa-ha-rtsp`, and `prusa-cam` are active. `prusa-rtsp` may correctly
   be inactive when its Prusa mode is disabled.
4. Verify `ffprobe` or VLC can open `rtsp://<pi>:8555/live` for at least five minutes.
5. Toggle Prusa RTSP off/on and verify port 8555 remains available while port 8554 follows Prusa.
6. Open Prusa app/site WebRTC while HA RTSP and Connect snapshots remain active. Record snapshot
   timestamps and confirm the configured cadence does not stop.
7. Trigger a snapshot explicitly during both RTSP and WebRTC playback.
8. Change SD/HD/FHD quality when not TURN-locked; verify HA reconnects and reports the new profile
   resolution. Confirm Prusa RTSP is not started if it was inactive.
9. During the TURN lock, request a quality change and verify the change/restarts remain blocked.
10. Stop or obstruct local HTTP/WS-Discovery and prove cloud registration, snapshots, signaling,
    and WebRTC continue.
11. Reboot with overlay enabled and confirm service enablement/configuration survives.

### Home Assistant acceptance tests (requires explicit authorization)

1. On the same multicast domain, Home Assistant discovers exactly one ONVIF device with a stable
   identity/name.
2. Adding it with blank credentials creates a camera entity and loads live video/still images.
3. Restart Home Assistant and the Pi independently; the same device/entity is reused instead of a
   duplicate being created.
4. On a routed/VLAN setup where multicast is unavailable, manual ONVIF configuration by Pi IP and
   port 80 succeeds.
5. Dashboard viewing for at least 15 minutes does not stop Connect snapshots or Prusa app/site live
   view.
6. Home Assistant logs show no repeated unsupported-operation loop. If a missing read-only ONVIF
   operation is observed, capture the exact SOAP action and add only the minimal truthful response
   plus fixtures.

## Acceptance criteria

Source-level acceptance:

- All commands under “Host unit tests” pass.
- Existing default `prusa-rtsp.service` behavior and URL are unchanged.
- HA uses the independent `:8555` endpoint.
- No additional `rpicam-vid`/libcamera process is introduced.
- No cloud credential or fingerprint is exposed over LAN interfaces.
- Local discovery/HTTP startup failure cannot terminate or reconnect the Prusa signaling client.
- Snapshot enable/disable remains authoritative; RTSP/WebRTC activity does not alter it.
- Quality-change restart behavior never starts inactive Prusa RTSP and remains blocked under the
  existing TURN lock.

Live acceptance (do not mark complete without evidence):

- Home Assistant discovery/manual setup, stable entity identity, live stream, and still image all
  work on the target installation.
- Prusa Connect snapshots continue at configured cadence during HA RTSP and Prusa WebRTC viewing.
- Prusa app and website behavior remains unchanged with HA viewing continuously.
- Prusa's RTSP mode still controls only port 8554; port 8555 remains available.
- Resolution changes, reboot persistence, and overlay deployment pass the integration checks.
- Evidence (commands, timestamps, relevant logs, HA result, date/commit) is added to
  `docs/firmware-implementation-gap-tracker.md`; only then may `GAP-SNAPSHOT-04` be closed.

## Security and network notes

- The ONVIF facade currently has no authentication because the target is easy trusted-LAN setup.
- Do not expose TCP 80, TCP 8555, or UDP 3702 through an Internet-facing firewall/NAT rule.
- Required HA-to-camera connectivity: TCP 80 and TCP 8555. Same-subnet discovery additionally
  needs UDP multicast `239.255.255.250:3702`.
- A routed network may require an mDNS/SSDP/WS-Discovery relay depending on infrastructure; manual
  ONVIF entry is the supported fallback.

## Rollback

For a code rollback, revert the ONVIF/HA-specific files and changes listed above, restore the old
snapshot streaming guards, and run the complete regression suite. For an authorized live rollback,
disable/remove only `prusa-ha-rtsp.service`, deploy the prior known-good revision using the overlay
workflow, and verify `rpicam-source`, `prusa-cam`, optional `prusa-rtsp`, Connect snapshots, and
WebRTC. Do not modify the token or persistent data as part of this rollback.
