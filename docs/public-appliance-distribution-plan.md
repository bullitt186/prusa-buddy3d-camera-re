# Public Raspberry Pi Camera Appliance — Distribution and Implementation Plan

Status: approved design; not yet implemented.

This document is the implementation handoff for turning the current Raspberry Pi impersonator,
ONVIF facade, persistent data partition, and Home Assistant support into a public DIY appliance.
It is deliberately detailed enough for another coding agent to implement without having to choose
the distribution model, partition strategy, onboarding paths, MQTT surface, or update policy.

The implementation baseline is the current `main` branch. It contains the `/data` persistence and
Home Assistant ONVIF work that are absent from older development worktrees such as `skimmer`.
Rebase a feature branch on `main` before beginning and preserve all newer persistence, TURN-quality
lock, WebRTC, snapshot, bootlog, and ONVIF behavior.

This is primarily product packaging and local interoperability work, not a claim of additional
Buddy3D firmware parity. If implementation changes any emulated firmware behavior or wire fields,
the agent must first follow `AGENTS.md`, work against the applicable named `GAP-*` item, and update
the evidence tracker. Do not reinterpret or close firmware gaps as part of packaging work.

## 1. Decision and Supported Product

Ship the project as an appliance, using three related deliverables:

1. **Canonical source of truth:** a pinned, reproducible `rpi-image-gen` configuration and custom
   layers in this repository.
2. **Supported end-user artifact:** a signed, compressed SD-card image published with every stable
   GitHub release.
3. **Developer/migration path:** retain `pi-impersonator/bootstrap.sh`, but mark it unsupported for
   public appliance installation. It cannot reproduce the partitioning, recovery, identity,
   onboarding, or update model.

Do not distribute a captured clone of the development SD card. A clone risks leaking machine
identity or secrets, is not auditable, and cannot be rebuilt reliably.

### 1.1 Version-one support boundary

- Raspberry Pi Zero 2 W, arm64.
- Raspberry Pi OS/Debian Trixie minbase generated through `rpi-image-gen`.
- One CSI/libcamera camera.
- OV5647 is the first certified sensor because it is the known-good development sensor.
- Other libcamera CSI sensors may proceed when the first-boot probe proves all required capture
  modes. They must be labelled experimental until added to the tested hardware matrix.
- 8 GB minimum SD card; 16 GB or larger recommended for timelapse use.
- Built-in Home Assistant ONVIF and MQTT integrations only. No HACS/custom integration is required.
- Stable release channel only for v1.
- No telemetry.

### 1.2 User-facing installation flow

The documented normal flow is:

1. Download Raspberry Pi Imager 2.x.
2. Open the project-published Imager manifest/repository file, select the Buddy3D camera image,
   optionally provide Wi-Fi/hostname/SSH customization, and flash the card.
3. Connect the CSI camera, insert the card, and power the Pi.
4. Complete setup through the temporary setup hotspot and captive portal. Wi-Fi values already
   provided through Imager are prefilled and do not need to be re-entered.
5. Pair with Prusa Connect by scanning an original Prusa pairing QR or entering the token manually.
6. Optionally configure MQTT. ONVIF works independently of MQTT.
7. Add the discovered ONVIF camera in Home Assistant and allow the MQTT control device to appear
   automatically when MQTT is enabled.

`Use Custom` remains a fallback flashing method, but Raspberry Pi Imager cannot infer the correct
Trixie initialization format from a raw local image. The project manifest is therefore the preferred
path and must declare the selected first-boot format and image hashes.

## 2. Repository and Build Architecture

### 2.1 Proposed repository structure

Use a top-level `image/` subtree; do not mix image generation into `pi-impersonator/deploy.sh`.

```text
image/
  README.md                         build and local test instructions
  rpi-image-gen.lock                pinned upstream commit/release
  config/buddy3d-pi-zero2w.yaml     complete image composition
  layer/                            project-specific layers and hooks
  assets/                           systemd units, defaults, icon, web assets
  scripts/build-image.sh            deterministic build entry point
  scripts/validate-image.sh         offline partition/rootfs validation
  scripts/make-release.sh           compression, hashes, SBOM, manifest
  imager/os-list.template.json      Raspberry Pi Imager manifest template
```

Add a release workflow for validation and artifact assembly. Because `rpi-image-gen` is officially
supported on native arm64 Debian/Raspberry Pi OS, produce public release images on a controlled
native arm64 Trixie runner. Host unit tests may continue on the normal CI runner. Do not silently
claim a QEMU or foreign-architecture build as the supported release path.

### 2.2 Pinning and reproducibility

- Pin the exact `rpi-image-gen` revision in `rpi-image-gen.lock` and verify its checkout before
  building.
- Pin the Debian/Raspberry Pi OS suite and repository definitions.
- Generate a locked Python dependency file with hashes for `aiohttp`, `python-socketio`, the MQTT
  client, web-auth dependencies if any, and their transitive dependencies.
- Prefer Debian packages for PyGObject, GStreamer, libcamera/rpicam, NetworkManager, Samba, and
  other native components.
- Set `SOURCE_DATE_EPOCH` from the release commit timestamp and normalize ownership and timestamps
  in application archives.
- Produce an SPDX or CycloneDX SBOM plus an installed-package manifest.
- Record the source commit, image-builder revision, OS suite, kernel package, and package manifest
  in `/usr/share/prusa-buddy3d-camera/build-info.json`.
- The signed release artifact is the trust boundary. Reproducible build instructions are required,
  but do not promise byte-identical output unless CI proves it.

### 2.3 Release artifacts

For a tag such as `v1.0.0`, publish:

```text
buddy3d-camera-pi-zero2w-1.0.0.img.xz
buddy3d-camera-pi-zero2w-1.0.0.img.xz.sha256
buddy3d-camera-pi-zero2w-1.0.0.img.xz.minisig
buddy3d-camera-pi-zero2w-1.0.0.spdx.json
buddy3d-camera-pi-zero2w-1.0.0.packages.txt
buddy3d-camera-os-list.json
```

The Imager manifest must include image URL, compressed and extracted sizes, compressed and
extracted SHA-256 values, release date, arm64 architecture, compatible Pi Zero 2 W device
identifier, icon, website, and the selected initialization format. Validate it against Raspberry
Pi Imager's current JSON schema during the release workflow.

Use one offline Ed25519/Minisign release key. Commit only the public key. Store the private key in
the release environment, never in GitHub Actions logs, the repository, image, or developer Pi.

### 2.4 Partition layout

Generate an MBR image with exactly three partitions:

| Partition | Initial size | Filesystem | Label | Purpose |
|---|---:|---|---|---|
| 1 | 512 MiB | FAT32 | `BOOT` | Pi firmware, kernel, initramfs, first-boot input |
| 2 | 4 GiB | ext4 | `ROOT` | Immutable OS and factory application |
| 3 | 512 MiB in image | ext4 | `PERSIST` | Config, state, media, network, releases |

The third partition must be last. On first boot, before NetworkManager and camera services start:

1. Validate that partition 3 has label `PERSIST` and the expected PARTUUID relationship.
2. Grow partition 3 to the end of the block device.
3. Run `resize2fs` through a one-shot systemd unit.
4. Write a durable completion marker on `PERSIST` only after both operations succeed.
5. Re-running after interruption must be safe.

Use PARTUUIDs in the boot command line and `fstab`; never assume `/dev/mmcblk0p3`. ROOT is mounted
as the immutable overlay lower layer and uses a tmpfs upper layer. `PERSIST` mounts directly at
`/data` and is never part of the volatile overlay.

The build fails when the populated ROOT filesystem exceeds 75% of its 4 GiB capacity. Reduce the
image rather than increasing ROOT unless actual package measurements prove 4 GiB impossible. The
extracted image must fit every standards-compliant 8 GB card while leaving useful DATA capacity.

### 2.5 Runtime filesystem

Use these durable locations:

```text
/data/prusa-cam/config/device.toml       non-secret appliance configuration
/data/prusa-cam/config/secrets.toml      Prusa/MQTT/admin secrets
/data/prusa-cam/state.json               current versioned runtime state
/data/prusa-cam/identity.json            generated stable appliance UUID
/data/prusa-cam/releases/<version>/      installed application releases
/data/prusa-cam/releases/current         atomic symlink to active release
/data/prusa-cam/releases/previous        atomic symlink to rollback release
/data/network/system-connections/        durable NetworkManager profiles
/data/sdcard/timelapse/                  existing emulated SD/timelapse store
/data/backups/                           bounded configuration backups
```

Preserve `/mnt/sdcard` as the bind-mounted firmware-facing/SMB path. Replace the hard-coded
`DEFAULT_SERVICE_USER='<operator>'` and all home-directory assumptions with a dedicated non-login
`prusa-cam` system account. The service account owns application configuration, state, releases,
and media. Secrets are mode `0600`; configuration directories are not world-readable.

DATA must mount before NetworkManager and every camera-related unit. If DATA is missing, corrupt,
or read-only, start only recovery/setup services. Never create a look-alike `/data` directory in
the volatile root and continue as if persistence were working.

Keep journald volatile and size-limited. The web UI may expose a redacted, bounded diagnostic log
download assembled from the current boot.

## 3. Service and Application Refactoring

### 3.1 Service ownership

Retain the existing sensor-sharing invariant:

```text
CSI camera
  -> rpicam-source.service        sole libcamera sensor owner
     -> stream_mux.py
        -> prusa-rtsp :8554/live  Prusa-mode controlled
        -> prusa-ha-rtsp :8555/live, always available
        -> snapshots/uploads
        -> WebRTC
```

Provisioning QR capture owns the sensor only before normal runtime starts. It must terminate and
release libcamera before `rpicam-source.service` starts. No new feature may create a second
`rpicam-vid` process while the source service is active.

### 3.2 Unit ordering

Create explicit systemd targets rather than relying on enablement order:

```text
local-fs.target
  -> prusa-data-grow.service
  -> data-ready.target
     -> NetworkManager.service
     -> prusa-provisioning.service OR prusa-camera.target

prusa-camera.target
  -> pi-persist.service
  -> rpicam-source.service
  -> prusa-rtsp.service
  -> prusa-ha-rtsp.service
  -> prusa-cam.service
  -> prusa-mqtt.service (only when configured)
  -> prusa-admin.service
  -> prusa-updater.timer
```

Local integration failures remain isolated. A failure in WS-Discovery, MQTT, administration, or
update checking must not stop the source, Prusa registration/signaling, snapshots, RTSP, or WebRTC.

### 3.3 Shared settings coordinator

Introduce one application-level settings coordinator used by all control sources:

- Prusa signaling commands.
- Local web administration.
- MQTT commands.
- Startup restore/migration.

It owns validation, serialization, live application, persistence, error reporting, and authoritative
state publication. MQTT and the web UI must not duplicate service-control subprocesses or write
`state.json` directly.

Required settings and ranges remain:

| Setting | Values |
|---|---|
| Quality | `SD`, `HD`, `FHD` / existing enum 1, 2, 3 |
| Snapshot upload | boolean |
| Snapshot interval | 10–600 seconds |
| Timelapse enabled | boolean |
| Timelapse interval | 1–3600 seconds |
| Timelapse FPS | 1–30 |
| Prusa RTSP mode | existing disabled/enabled values |
| WebRTC mode | existing disabled/enabled values |
| Camera name | non-empty normalized UTF-8 string with bounded length |

Quality changes must continue honoring the existing TURN/scoped-quality lock. A rejected mutation
does not change persistent state, does not restart services, and returns the authoritative current
state plus a non-secret reason.

## 4. First Boot, Pairing, and Administration

### 4.1 Provisioning states

Persist an explicit state machine in `identity.json`/configuration:

```text
factory -> storage_ready -> camera_validated -> unclaimed
unclaimed -> claimed -> configured -> running
running -> recovery (explicit action or invalid durable configuration)
recovery -> configured -> running
```

Never infer “claimed” solely from the existence of a token. Claimed means an administrator password
has been set and durable configuration has passed validation.

### 4.2 Camera probe

Before presenting a camera as usable:

1. Run `rpicam-hello --list-cameras` with a timeout.
2. Require exactly one selected CSI sensor for v1.
3. Make short captures that exercise output at 640×480, 1280×720, and 1920×1080. Sensor-native
   modes may differ; successful libcamera scaling is sufficient.
4. Validate that the H.264 source pipeline and one JPEG capture complete.
5. Store sensor model, modes, and probe result for diagnostics.

A missing or failed camera keeps the appliance in setup/recovery mode. It must not cause a rapid
systemd restart loop. The wizard shows the exact probe failure and a retry action.

### 4.3 Setup hotspot and web wizard

While unclaimed, expose a temporary Wi-Fi network named `Buddy3D-Setup-<last6-device-id>` and a
captive portal at `http://192.168.4.1`. The hotspot may be open because a unique credential cannot
be delivered from a headless unlabelled DIY device. Document that initial setup should be performed
in a trusted physical location.

The wizard must:

1. Show storage and camera probe status.
2. Import and prefill Wi-Fi/hostname/SSH customization supplied by Raspberry Pi Imager.
3. Scan for Wi-Fi networks and accept manual SSID entry for hidden networks.
4. Accept an original Prusa pairing QR or manual registration token.
5. Allow an optional explicit fingerprint; otherwise retain the existing MAC-derived behavior.
6. Require a local administrator password and confirmation.
7. Offer optional MQTT configuration and a live connection test.
8. Display a final redacted summary before committing.
9. Atomically persist configuration and secrets.
10. Stop provisioning/QR capture, disable the hotspot, activate station networking, then start the
    normal camera target.

Do not bind an unclaimed administration wizard to the normal LAN interface. After claim, serve the
administration UI using a device-generated HTTPS certificate at
`https://buddy3d-<device-id>.local`. A browser warning for the self-signed certificate is acceptable
and must be documented.

### 4.4 Original Prusa QR pairing

Exact QR support cannot be implemented by guessing the payload. Before coding the parser:

1. Generate a new QR through the current Prusa Connect “Add WiFi Camera” flow.
2. Decode it locally without committing or logging the image or payload.
3. Record only field names/types, encoding, version markers, and validation rules with secrets
   replaced by `<PLACEHOLDER>`.
4. Create a fully synthetic test fixture with non-working credentials.
5. Verify whether the registration token is already fingerprint-bound and preserve current
   fingerprint semantics.

The scanner captures low-rate JPEGs until it sees a valid supported payload. Bound image size,
decode duration, JSON/string lengths, and retry frequency. Ignore malformed or unrelated QRs. A
valid QR is staged, shown in redacted form in the wizard, and written only when the user completes
the claim. Expired/rejected tokens return the user to setup without overwriting last-known-good
credentials.

### 4.5 Persistent admin security

- Hash the admin password with Python's salted `hashlib.scrypt`; never store the password.
- Use random server-side session identifiers, idle and absolute expiry, secure/HttpOnly/SameSite
  cookies, CSRF tokens, and login rate limiting.
- Redact Prusa tokens, fingerprints where appropriate, MQTT credentials, Wi-Fi PSKs, cookies, and
  Authorization headers from UI responses and logs.
- Require the current admin password for credential changes, update installation, factory reset,
  backup export containing secrets, and SSH enablement.
- Permit a custom TLS certificate/key import, but do not require it.
- ONVIF SOAP, `/snapshot.jpg`, and RTSP ports 8554/8555 remain unauthenticated by the selected v1
  product policy. The UI and documentation must label them **trusted-LAN only** and explicitly warn
  users not to expose TCP 80, 8554, or 8555 to the Internet.

### 4.6 Expert and recovery paths

SSH is disabled by default. Raspberry Pi Imager may enable it and create the operator account. An
expert may edit the durable TOML configuration through SSH, then run a supplied validator and a
single apply command. Invalid files are rejected without replacing live configuration.

Support two recovery entries:

- Authenticated “Enter setup mode” action in the web UI or CLI.
- A documented `buddy3d-recovery` sentinel placed on the BOOT partition while powered off.

Factory reset is separate and destructive. Require a second explicit confirmation/sentinel, first
create a dated backup on DATA, and delete that backup only after the reset system has completed a
successful claimed boot. Report exactly what is being deleted: configuration, network profiles,
MQTT state, application releases, and optionally timelapse media.

## 5. Configuration and Migration

### 5.1 Configuration contract

Define and version a TOML schema. The public non-secret portion includes:

```toml
schema_version = 1
camera_name = "Printer Camera"
fingerprint = ""                    # optional override

[prusa]
server = "webcam.connect.prusa3d.com"

[mqtt]
enabled = false
uri = "mqtts://broker.example:8883"
client_id = ""                      # generated default when empty
discovery_prefix = "homeassistant"
topic_prefix = "buddy3d"
ca_file = ""                        # optional uploaded CA under /data

[admin]
hostname = "buddy3d-abcdef"
```

The separate secrets document holds the Prusa token, MQTT username/password, Wi-Fi secret where
not owned solely by NetworkManager, and admin password hash. Define a strict allowlist of keys and
reject unknown security-sensitive keys while allowing documented forward-compatible metadata.

Provide schema migrations as pure, unit-tested functions. Never silently downgrade a newer schema.
A newer unsupported schema enters recovery with the original file untouched.

### 5.2 Existing installation importer

Implement an idempotent importer for:

- Existing `pi-impersonator/config.ini` identity/upload values.
- `/data/prusa-cam/state.json`.
- `/etc/prusa-cam/quality.env` and RTSP mode where durable state lacks them.
- `/data/sdcard` or `/mnt/sdcard` timelapse files.

Validate the complete converted configuration before activation. Preserve source files under a
dated `/data/backups/migration-*` directory and write a completion record containing source schema,
target schema, timestamp, and application version—never secret values.

A full image flash rewrites the partition table and may destroy existing DATA. Documentation must
require exporting the configuration/timelapse backup before reflashing. Do not claim in-place
preservation across a full-image flash.

## 6. Home Assistant and MQTT Interface

### 6.1 ONVIF remains the media interface

Preserve the existing ONVIF architecture and URLs:

- WS-Discovery on UDP 3702.
- Device/media SOAP and snapshot HTTP on TCP 80.
- One dynamic H.264 profile backed by `rtsp://<device>:8555/live`.
- Prusa-controlled RTSP remains `rtsp://<device>:8554/live`.
- Manual ONVIF addition by IP remains supported when multicast cannot cross a VLAN.

Do not add an MQTT camera entity; it would duplicate the ONVIF camera. Do not misuse ONVIF imaging
operations for project-specific settings. Standard Home Assistant ONVIF currently exposes only a
small set of recognized imaging/auxiliary switches; MQTT is the supported settings interface.

Current Home Assistant registry rules do not guarantee that devices from separate ONVIF and MQTT
config entries merge by MAC. Expect two entries:

- `<camera name>` from ONVIF, containing the camera entity.
- `<camera name> Controls` from MQTT, containing settings and diagnostics.

Use stable identities in both, but do not promise or test cross-integration merging.

### 6.2 MQTT broker configuration

MQTT is optional and disabled until explicitly configured. Support generic brokers rather than
assuming the Home Assistant Mosquitto add-on:

- `mqtt://` and `mqtts://`.
- IPv4, IPv6, and DNS hostname.
- Optional username/password.
- System CA validation and an optional custom CA PEM.
- Configurable discovery prefix, default `homeassistant`.
- Configurable base prefix, default `buddy3d`.
- Keepalive and reconnect backoff with bounded jitter.

Do not implement broker auto-discovery or broker credential extraction from Home Assistant. The web
wizard tests DNS, TCP/TLS, authentication, publish, and subscribe using a temporary non-retained
test topic before saving.

### 6.3 Stable topic contract

Derive `<device-id>` from the persisted appliance UUID, not the changeable name or IP address:

```text
buddy3d/<device-id>/availability
buddy3d/<device-id>/state
buddy3d/<device-id>/command/quality
buddy3d/<device-id>/command/snapshot_upload
buddy3d/<device-id>/command/snapshot_interval
buddy3d/<device-id>/command/timelapse_enabled
buddy3d/<device-id>/command/timelapse_interval
buddy3d/<device-id>/command/timelapse_fps
buddy3d/<device-id>/command/timelapse_build
buddy3d/<device-id>/command/prusa_rtsp
buddy3d/<device-id>/command/webrtc
buddy3d/<device-id>/command/restart
buddy3d/<device-id>/update/state
buddy3d/<device-id>/update/install
```

Publish `availability` as retained `online` with a retained LWT of `offline`. Publish authoritative
state as one retained JSON document after startup and after every successful mutation. Commands use
QoS 1 and are never retained. Ignore retained command messages defensively.

State JSON uses explicit stable names and native JSON values, for example:

```json
{
  "schema_version": 1,
  "quality": "FHD",
  "snapshot_upload": true,
  "snapshot_interval": 10,
  "timelapse_enabled": false,
  "timelapse_interval": 10,
  "timelapse_fps": 10,
  "prusa_rtsp": false,
  "webrtc": true,
  "prusa_connected": true,
  "camera_source": "running",
  "storage_free_bytes": 123456789,
  "wifi_rssi_dbm": -54,
  "cpu_temperature_c": 48.2,
  "uptime_seconds": 3600,
  "application_version": "1.0.0",
  "last_command_error": null
}
```

Never include secrets, token fragments, broker URI credentials, SSID PSKs, local paths containing a
username, or raw exception traces.

### 6.4 MQTT Discovery

Publish one retained Home Assistant MQTT **device discovery** document at:

```text
<discovery-prefix>/device/buddy3d_<device-id>/config
```

It must contain required origin information, stable unique IDs, device name `<camera name> Controls`,
manufacturer `Prusa Community`, model `Buddy3D Raspberry Pi Camera`, application version, serial/
UUID, normalized WLAN MAC connection, availability topic, and these components:

| HA entity | Type | Behavior |
|---|---|---|
| Quality | select | `SD`, `HD`, `FHD` |
| Snapshot uploads | switch | periodic Prusa uploads only |
| Snapshot interval | number | 10–600 s, step 1 |
| Timelapse | switch | enable/disable capture |
| Timelapse interval | number | 1–3600 s, step 1 |
| Timelapse FPS | number | 1–30, step 1 |
| Build timelapse | button | invoke existing video build action |
| Prusa RTSP mode | switch | controls only port 8554 |
| Prusa WebRTC mode | switch | controls remote WebRTC behavior |
| Restart | button | restart device through existing guarded path |
| Application update | update | approved signed app update |
| Prusa connection | binary sensor | connection/authenticated status |
| Camera source | binary sensor | source service/capture health |
| Storage free | sensor | bytes, diagnostic category |
| Wi-Fi RSSI | sensor | dBm, diagnostic, disabled by default |
| CPU temperature | sensor | °C, diagnostic, disabled by default |
| Uptime | sensor | seconds, diagnostic, disabled by default |
| App version | sensor | diagnostic, disabled by default |

Subscribe to Home Assistant's default birth topic `homeassistant/status`. On an `online` birth or
broker reconnect, republish discovery and state after a randomized 0–5 second delay. Retained
discovery provides immediate cold-start recovery. When an entity is removed or its discovery topic
changes, publish an empty retained payload to the old topic so ghost entities do not remain.

Command handling rules:

- Parse a bounded UTF-8 payload and reject malformed values.
- Call the shared settings coordinator.
- Apply and persist before publishing changed state.
- Publish current state even after rejection so Home Assistant returns to authoritative state.
- Set `last_command_error` to a bounded non-secret reason, then clear it after the next successful
  command or after a short timeout.
- MQTT disconnect/reconnect cannot affect Prusa signaling or local media services.

## 7. Signed Application Updates

### 7.1 Scope and trust

Application updates may replace project Python code, static web assets, and pinned pure-Python
wheels. They may not replace the kernel, boot firmware, partition table, systemd base units, Debian
packages, GStreamer/libcamera packages, or signing public key. Changes to those require a new image.

Publish each application release through GitHub Releases as:

```text
buddy3d-camera-app-<version>.tar.zst
buddy3d-camera-app-<version>.tar.zst.sha256
buddy3d-camera-app-<version>.tar.zst.minisig
update-manifest.json
update-manifest.json.minisig
```

The signed manifest contains schema version, semantic version, release channel, source commit,
minimum compatible image version, bundle URL/hash/size, release summary/URL, and whether a reboot is
required. Reject unknown schema versions, incompatible base images, invalid SemVer, downgrades, and
bundles that exceed configured size/free-space limits.

### 7.2 Install algorithm

1. Check for a new stable version once per 24 hours with randomized jitter and on an authenticated
   manual request.
2. Report availability through the web UI and MQTT update entity; never auto-install.
3. On approval, download to a unique staging directory on DATA.
4. Verify manifest signature, bundle signature, SHA-256, size, version, compatibility, archive paths,
   and free space before extracting.
5. Reject absolute paths, `..`, devices, links escaping the release directory, unexpected owners,
   and setuid/setgid content.
6. Extract to `/data/prusa-cam/releases/<version>.staging` and create its venv using only bundled,
   hash-pinned wheels plus required system site packages.
7. Run compile, import, configuration migration dry-run, and local preflight tests.
8. Rename staging to the final version directory, update `previous`, and atomically replace the
   `current` symlink.
9. Restart application services and run local health checks for up to 90 seconds.
10. Mark success and publish final state only when source, local HTTP, ONVIF, RTSP, and application
    health pass.
11. If startup or health checks fail, atomically restore `previous`, restart it, record a redacted
    failure reason, and mark the attempted version bad.

Prusa cloud reachability is not a health requirement because an Internet outage must not trigger
rollback. Preserve current, previous, and immutable factory releases; prune older versions only
after a successful boot. The launcher must fall back to the factory application when DATA contains
no valid active release.

MQTT update state follows Home Assistant's JSON update schema and includes `installed_version`,
`latest_version`, `release_summary`, `release_url`, `in_progress`, and `update_percentage`. Accept
only the literal non-retained install payload declared by discovery.

## 8. Test Strategy

### 8.1 Existing regression gate

Every implementation increment must run the existing project checks:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q pi-impersonator tests
bash -n pi-impersonator/bootstrap.sh pi-impersonator/deploy.sh
git diff --check
```

If the Rust proxy changes, also run `cargo test` in `proxy/`. Existing signaling, snapshots,
quality/TURN lock, RTSP, WebRTC, ONVIF, persistence, timelapse, and protocol tests are mandatory
regression gates.

### 8.2 New host tests

Add isolated tests for:

- TOML parsing, schema validation, secret separation, atomic writes, and every schema migration.
- Legacy `config.ini`/state migration, idempotency, backup creation, and rollback on invalid input.
- Settings-coordinator validation, serialization, live-apply failure, persistence failure, and
  authoritative state restoration.
- QR parser using only synthetic fixtures: valid, wrong version, missing field, invalid encoding,
  expired token, excessive length, unrelated QR, and repeated scan.
- Provisioning state transitions and prevention of normal camera startup while QR capture owns the
  sensor.
- Admin login, scrypt verification, session expiry, CSRF, rate limit, redaction, and protected
  destructive actions.
- MQTT topic construction, device discovery schema, unique-ID stability, retained/LWT flags, HA
  birth republish, stale discovery cleanup, bounded reconnect backoff, TLS validation, invalid
  commands, retained-command rejection, and broker outage isolation.
- Every discovered control invoking the shared coordinator rather than writing files directly.
- Update manifest/bundle signature validation, path traversal prevention, compatibility checks,
  insufficient space, interrupted download/extraction/activation, bad release suppression, health
  timeout, rollback, and factory fallback.
- First-boot grow logic using loopback disk images at several sizes, including interruption between
  partition growth and filesystem growth.

Use a disposable Mosquitto container/process in integration tests and validate the emitted discovery
payload against a current Home Assistant test instance or pinned HA schema. Do not depend on the
user's live broker or Home Assistant instance for automated tests.

### 8.3 Image validation

The image validation script mounts the artifact read-only and asserts:

- Exactly the documented partition types, sizes, labels, and order.
- No secret/config fixture, SSH host key, persistent machine ID, Wi-Fi profile, or release private
  key is present.
- ROOT utilization is below 75%.
- DATA has correct initial structure/ownership but no device identity generated at build time.
- All required units are installed with correct ordering; none references a personal username or
  home directory.
- SSH and password login are disabled by default.
- Overlay, volatile logging, NetworkManager, camera interfaces, and factory fallback are configured.
- The embedded build-info/SBOM/package data matches the release artifact metadata.
- Raspberry Pi Imager manifest hashes and sizes match the final compressed and extracted images.

Run a secret scanner across Git history additions, the uncompressed image, SBOM, logs, test
fixtures, and release artifacts.

### 8.4 Physical SD-card matrix

Test release candidates on 8, 16, 32, and 64 GB cards:

- First boot grows DATA to the end of each card.
- A power interruption before, during, and after growth leaves a recoverable boot.
- ROOT remains immutable; durable configuration survives reboot and hard power loss.
- A full DATA condition invokes pruning/recovery without corrupting settings or starting a restart
  storm.
- OV5647 passes all three quality levels, JPEG, H.264, WebRTC, and timelapse tests.
- Missing, disconnected, and unsupported cameras show actionable setup diagnostics.
- At least one additional available CSI sensor is recorded as experimental or certified based on
  the same matrix; do not generalize from detection alone.

### 8.5 Onboarding matrix

Exercise independently:

- Raspberry Pi Imager Wi-Fi customization plus hotspot claim.
- Hotspot setup with scanned SSID and hidden SSID.
- Exact current Prusa QR pairing.
- Manual token and optional fingerprint entry.
- MQTT disabled.
- Plain MQTT with credentials.
- MQTT over TLS using public CA and custom CA.
- SSH-enabled expert configuration and validation.
- Wrong Wi-Fi password, missing DHCP, DNS failure, captive upstream network, invalid Prusa token,
  unavailable broker, invalid TLS certificate, and interrupted configuration save.
- Authenticated recovery, BOOT-sentinel recovery, and confirmed factory reset.

Verify that no failure overwrites last-known-good configuration and all shown/logged errors are
redacted.

### 8.6 Home Assistant and coexistence tests

On the same multicast domain:

1. HA discovers exactly one stable ONVIF camera.
2. Adding it with blank credentials produces a live camera entity and snapshot.
3. Enabling MQTT creates the separate `<camera name> Controls` device without YAML.
4. All MQTT entities have stable unique IDs across HA, broker, and Pi restarts.

Across a routed/VLAN network:

1. Manual ONVIF setup by IP and port 80 succeeds without WS-Discovery.
2. MQTT Discovery still creates the controls device through the broker.

Coexistence soak:

1. Stream HA RTSP `:8555/live` continuously for at least 15 minutes.
2. During that stream, open and close Prusa app/site WebRTC repeatedly.
3. Confirm Connect snapshots continue at their configured cadence.
4. Trigger explicit snapshots and timelapse captures.
5. Toggle Prusa RTSP and prove it changes only `:8554`, never `:8555`.
6. Change SD/HD/FHD when unlocked and confirm HA reconnects and ONVIF reports the new dimensions.
7. Attempt a quality change during the TURN lock and confirm it is rejected without state drift or
   service restart.
8. Disconnect MQTT, stop WS-Discovery, stop the admin UI, and make update hosting unavailable in
   turn; Prusa registration, snapshots, RTSP, and WebRTC must continue.

## 9. Acceptance Criteria

The public image is ready only when every item below has recorded evidence in a release-candidate
report containing date, source commit, image version, hardware, commands/tests, and result.

### Build and release

- [ ] A clean checkout plus the pinned builder produces a bootable image without personal files.
- [ ] CI host tests, image validation, secret scan, SBOM generation, and Imager schema validation
      pass.
- [ ] The `.img.xz`, checksum, Minisign signature, SBOM, package manifest, and Imager manifest are
      published from one tag and agree on version/hash/size.
- [ ] Verification with the committed public key rejects a modified image or update bundle.
- [ ] The release instructions work on a clean machine using Raspberry Pi Imager 2.x.

### Boot, storage, and recovery

- [ ] Fresh 8, 16, 32, and 64 GB cards boot and DATA expands to the available capacity.
- [ ] ROOT is an immutable overlay lower filesystem and remains below the 75% build threshold.
- [ ] Network, identity, settings, application release, and timelapse data survive normal reboot and
      abrupt power loss.
- [ ] Missing/corrupt DATA starts recovery rather than a falsely successful volatile runtime.
- [ ] Interrupted growth, configuration save, and update activation recover automatically or expose
      a documented recovery path.
- [ ] The immutable factory application starts when no valid DATA release is available.

### Provisioning and security

- [ ] Imager, hotspot, exact Prusa QR, manual token, and SSH expert flows all reach the same valid
      durable configuration.
- [ ] The exact QR parser is based on a current captured schema, while the repository contains only
      redacted documentation and synthetic fixtures.
- [ ] The unclaimed wizard is reachable only through setup mode; the setup hotspot stops after
      claim.
- [ ] The persistent admin UI requires the configured password and passes session, CSRF, rate-limit,
      and secret-redaction tests.
- [ ] No default SSH password, build-time host key, token, Wi-Fi PSK, MQTT credential, admin hash,
      or signing private key exists in the release image.
- [ ] Documentation clearly states that ONVIF, snapshots, and RTSP are unauthenticated trusted-LAN
      services and must not be port-forwarded.

### Camera and Prusa compatibility

- [ ] OV5647 passes SD, HD, FHD, JPEG, H.264, timelapse, RTSP, WebRTC, and power-cycle tests.
- [ ] `rpicam-source.service` remains the only normal-runtime camera sensor owner.
- [ ] Prusa Connect registration/authentication and periodic/explicit snapshots behave as before.
- [ ] Prusa app and website WebRTC work while HA is viewing the camera.
- [ ] Prusa RTSP mode controls only port 8554; HA port 8555 stays available.
- [ ] Existing TURN-quality locking, persistence, reboot, timelapse, signaling, and no-unsolicited-
      post-auth behavior remain covered and passing.

### Home Assistant and MQTT

- [ ] ONVIF discovery and manual IP setup both create a working HA camera.
- [ ] MQTT Discovery creates the complete controls device without HA YAML or a custom integration.
- [ ] No duplicate MQTT camera entity is created; the ONVIF and controls devices are clearly named.
- [ ] Reboots, reconnects, IP changes, and HA/broker restarts do not create duplicate entities.
- [ ] Every control validates, live-applies, persists, and reports authoritative state correctly.
- [ ] MQTT/TLS/broker failures are isolated from local media and Prusa functionality.
- [ ] The 15-minute simultaneous HA/Prusa/WebRTC/snapshot coexistence test passes.

### Updates

- [ ] Update availability appears in both the authenticated web UI and HA update entity.
- [ ] Installation starts only after explicit approval.
- [ ] Invalid signature, hash, archive path, version, compatibility, or free-space checks reject the
      update before activation.
- [ ] Successful activation survives reboot and preserves configuration/media.
- [ ] Failed health checks automatically restore the prior version within the documented timeout.
- [ ] Internet or Prusa backend unavailability alone never causes rollback.
- [ ] Kernel, OS-package, boot, and partition changes cannot be installed through the app updater.

### Documentation and legal

- [ ] README installation, onboarding, HA, MQTT, backup/reflash, recovery, security, update, hardware,
      and troubleshooting instructions match the released image.
- [ ] MIT license and existing trademark/non-affiliation notice are included in the image and
      release page.
- [ ] Documentation states that the project is community-developed, is not ONVIF certified, and
      does not distribute Prusa firmware.

## 10. Implementation Sequence

Implement in this order so each stage is independently testable and does not strand users on an
unrecoverable image:

1. **Foundations:** dedicated service user, durable path cleanup, configuration schemas, shared
   settings coordinator, and legacy migration tests.
2. **Image build:** pinned `rpi-image-gen` config, three-partition image, first-boot growth, overlay,
   service ordering, offline image validation, and initial manual flash tests.
3. **Provisioning:** camera probe, state machine, hotspot, web wizard, admin authentication,
   NetworkManager persistence, manual token, and recovery.
4. **Exact QR:** capture/redact the current format, implement bounded parser/scanner, and complete
   synthetic plus live pairing tests.
5. **MQTT:** generic broker client, shared command coordinator, discovery/state/LWT, HA entity tests,
   and failure isolation.
6. **Updates:** signing infrastructure, release bundle format, staging/verification, atomic switch,
   health checks, rollback, factory fallback, and MQTT update entity.
7. **Release automation:** arm64 build runner, compression/signing, SBOM, Imager manifest, release
   workflow, and documentation.
8. **Acceptance:** full SD-size, onboarding, sensor, HA/Prusa coexistence, power-loss, update, and
   recovery matrices with a recorded release-candidate report.

Do not publish the image as stable before all acceptance sections pass. Alpha artifacts may be
published for explicit testers, but must be marked unsupported and must use a separate pre-release
tag so they are never offered by the stable update channel.

## 11. Reference Documentation

- Raspberry Pi `rpi-image-gen`: <https://github.com/raspberrypi/rpi-image-gen>
- Raspberry Pi Imager customization formats:
  <https://github.com/raspberrypi/rpi-imager/blob/main/doc/os_customisation_formats.md>
- Raspberry Pi Imager OS-list example:
  <https://github.com/raspberrypi/rpi-imager/blob/main/doc/os-sublist-example.json>
- Home Assistant ONVIF integration: <https://www.home-assistant.io/integrations/onvif/>
- Home Assistant MQTT Discovery: <https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery>
- Home Assistant MQTT Update: <https://www.home-assistant.io/integrations/update.mqtt>
- Home Assistant device registry: <https://developers.home-assistant.io/docs/device_registry_index/>
- Existing project ONVIF handoff: `docs/home-assistant-onvif-implementation-plan.md`
- Existing persistence and protocol evidence: `docs/firmware-implementation-gap-tracker.md`
