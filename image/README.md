# `image/` — Buddy3D camera appliance image build

Canonical, pinned source of truth for the public Raspberry Pi Zero 2 W SD-card
image. This subtree contains **only host-side scaffolding**: the composition,
the custom image layer, the first-boot growth logic, the runtime unit graph,
and the deterministic build entry point. It contains no secrets and generates
no device identity.

The image itself is built later on a controlled **native arm64 Debian/Raspberry
Pi OS Trixie** runner. `rpi-image-gen` does not support foreign-architecture
builds as a release path, and `scripts/build-image.sh` never attempts one.

## Pinned upstream

`rpi-image-gen.lock` pins:

| Field | Value |
|---|---|
| URL | `https://github.com/raspberrypi/rpi-image-gen` |
| Tag | `v2.8.0` |
| Commit | `262d4df5a9f9d4133370465399a7958a7c22cdc7` |

`scripts/build-image.sh` refuses to build unless the checkout at
`$RPI_IMAGE_GEN_DIR` has exactly that commit and advertises the `-S` source
directory and custom-image-layer mechanisms.

```sh
git clone --branch v2.8.0 --depth 1 \
  https://github.com/raspberrypi/rpi-image-gen /path/to/rpi-image-gen
RPI_IMAGE_GEN_DIR=/path/to/rpi-image-gen image/scripts/build-image.sh
```

## Layout

```text
image/
  README.md                         this file
  rpi-image-gen.lock                pinned upstream revision
  config/buddy3d-pi-zero2w.yaml     complete image composition
  layer/
    buddy3d-suite.yaml              rpios suite with NetworkManager (source §3.2)
    buddy3d-image.yaml              custom MBR 3-partition image layer
    genimage.cfg.in.ext4            genimage layout template
    setup.sh                        per-partition cmdline/fstab/PERSIST seeding
    pre-image.sh                    renders genimage.cfg
    post-build.sh                   strips build-time identity
    mke2fs.conf                     deterministic ext4 parameters
    bdebstrap/customize95-buddy3d-python  WP-2b extension point (stub)
  assets/
    prusa-data-grow.sh              first-boot PERSIST growth (AC-11)
    systemd/prusa-data-grow.service image-only grow unit
    systemd/prusa-camera.target     runtime target (AC-12)
    systemd/data-ready.target.d/10-data-grow.conf
    systemd/NetworkManager.service.d/10-data-ready.conf
    install-factory-app.sh          factory app/unit/layout hook
    build-info.py                   build-info.json generator (stdlib only)
    icon/buddy3d-camera.png         placeholder icon
  scripts/build-image.sh            deterministic build entry point
```

## Disk layout (AC-10)

MBR, exactly three primary partitions, PERSIST last:

| Partition | Size in image | Filesystem | Label | Mount |
|---|---:|---|---|---|
| 1 | 512 MiB | FAT32 | `BOOT` | `/boot/firmware` |
| 2 | 4 GiB | ext4 | `ROOT` | `/` (immutable overlay lower) |
| 3 | 512 MiB | ext4 | `PERSIST` | `/data` (grown to end of device on first boot) |

The MBR disk signature is fixed (`image.disksig`), so the kernel-derived
PARTUUIDs are deterministic: `<signature>-01/02/03`. `cmdline.txt` and
`/etc/fstab` therefore reference PARTUUIDs and never `/dev/mmcblk0pN`. The
signature is a build constant, not device identity.

`ROOT` is mounted read-only and `overlayroot` supplies the tmpfs upper layer
(`overlayroot="tmpfs:recurse=0"` in `/etc/overlayroot.conf`, `overlayroot=tmpfs`
in `cmdline.txt`). `PERSIST` is mounted directly at `/data` and is never part of
the overlay.

## Service ordering (AC-12)

```text
local-fs.target
  -> prusa-data-grow.service
  -> data-ready.target
     -> NetworkManager.service
     -> prusa-camera.target
        -> pi-persist -> rpicam-source -> prusa-rtsp -> prusa-ha-rtsp -> prusa-cam
```

The grow unit and target are image-only. The application units are **reused
verbatim** from `pi-impersonator/systemd/` (never copied or diverged). The extra
ordering is added with drop-ins under `assets/systemd/`. Optional future units
(MQTT/admin/updater) must be `Wants=` under `prusa-camera.target`, never
`Requires=`, so their failures stay isolated.

## Local verification (no image build, no root)

```sh
python3 -m unittest tests.test_pi_image_scaffolding -v
bash -n image/scripts/build-image.sh
python3 -m compileall -q image/assets

# Validate the custom layer against the real pinned metadata parser:
RPI_IMAGE_GEN_DIR=/tmp/opencode/rpi-image-gen \
  /tmp/opencode/rpi-image-gen/rpi-image-gen \
  metadata --lint image/layer/buddy3d-image.yaml
RPI_IMAGE_GEN_DIR=/tmp/opencode/rpi-image-gen \
  /tmp/opencode/rpi-image-gen/rpi-image-gen \
  metadata --lint image/layer/buddy3d-suite.yaml
```

## Upstream basis (rpi-image-gen v2.8.0)

The composition and layer follow the documented mechanism of the pinned
revision, not invented syntax:

- `docs/config/index.adoc` — YAML composition, `IGconf_*` sections, includes,
  `device.layer` / `image.layer` / `layer:` selection.
- `docs/layer/index.adoc` — layer metadata (`X-Env-Layer-*`, `X-Env-Var-*`),
  providers, `${DIRECTORY}` placeholder, `assetdir`.
- `docs/execution/index.adoc` — hook phases and `IMAGE_ASSET` hook/overlay
  resolution.
- `layer/base/image-base.yaml`, `layer/base/fs-base.yaml` — image variables.
- `image/mbr/simple_dual/image.yaml`, `genimage.cfg.in.ext4`, `setup.sh`,
  `pre-image.sh`, `mke2fs.conf` — MBR image-layer template and per-partition
  `exec-pre` hook pattern.
- `image/gpt/ab_userdata/pre-image.sh` — machine-id/identity stripping pattern.
- `layer/suite/debian/trixie-minbase.yaml` — base suite composition.
- `layer/net-misc/network-manager.yaml`, `layer/net-misc/openssh-server.yaml` —
  NetworkManager and first-boot SSH host-key handling.
- `layer/rpi/device/boot-firmware.yaml` — `cmdline.txt`/`config.txt` install.
- `bin/runner`, `bin/image2json` — hook resolution and IDP layout validation.

## Documented limitations / unresolved concerns

1. **No local build here.** There is no native arm64 Trixie host in this
   environment, so the image is not built or flashed. The build script is
   syntax-checked and the layer is metadata-linted against the pinned parser.
2. **`overlayroot` package availability.** v2.8.0 has no built-in read-only
   overlay layer, so the image installs the `overlayroot` package and writes its
   configuration. This is the mechanism already proven on the development Pi
   (`pi-impersonator/deploy.sh`), but it must be confirmed resolvable from the
   pinned Trixie/Raspberry Pi repositories at build time.
3. **WP-2b is not implemented.** Hash-locked Python dependencies, the runtime
   venv at `/opt/prusa-cam/venv`, the SBOM, and the installed-package manifest
   are stubbed at `layer/bdebstrap/customize95-buddy3d-python`. The app units
   reference `/opt/prusa-cam/venv/bin/python`, so the image is not runnable
   until WP-2b lands.
4. **WP-2b/WP-7 scripts are out of scope.** `scripts/validate-image.sh`,
   `scripts/make-release.sh`, and `imager/os-list.template.json` are not part of
   this increment.
5. **Fixed disk signature trade-off.** A fixed MBR signature gives static,
   deterministic PARTUUIDs and simple first-boot validation, at the cost of
   identical PARTUUIDs on every unit. They are never present on one system
   simultaneously; this is not device identity.
6. **No byte-identical reproducibility claim.** `SOURCE_DATE_EPOCH` is set and
   ownership/timestamps are normalized where the mechanism allows, but ext4
   filesystem UUIDs are generated by `mke2fs` and byte-identical output is not
   promised.

## Release artifact assembly (WP-2b)

`scripts/make-release.sh` turns one built `.img` into the publishable release
set (AC-14, AC-34, AC-35). It never builds or flashes an image and needs no
root or network. This section supersedes limitation 4 above for
`make-release.sh`, `scan-secrets.sh`, and `imager/os-list.template.json`.

```sh
image/scripts/make-release.sh \
  --image <built.img> --version <semver> --out-dir <dir> \
  [--release-date YYYY-MM-DD] [--url-base URL] [--icon URL] [--website URL] \
  [--packages <installed-packages.txt>] [--build-info <build-info.json>] \
  [--key <minisign.key>]
```

It produces, in `--out-dir`:

```text
buddy3d-camera-pi-zero2w-<version>.img.xz          deterministic xz -T1 -9e
buddy3d-camera-pi-zero2w-<version>.img.xz.sha256   sha256 of the compressed image
buddy3d-camera-pi-zero2w-<version>.img.xz.minisig  only when --key is supplied
buddy3d-camera-pi-zero2w-<version>.spdx.json       SPDX 2.3 SBOM
buddy3d-camera-pi-zero2w-<version>.packages.txt    normalized installed-package manifest
buddy3d-camera-os-list.json                        rendered Raspberry Pi Imager manifest
```

The `.sha256` file is `sha256sum`-compatible (`<hash>  <name>`). The extracted
`.img` SHA-256 and size are computed from the input image; the compressed SHA-256
and size are computed from the `.img.xz`. `SOURCE_DATE_EPOCH` is honored when
present (otherwise it defaults to the input image mtime), and `--release-date`
defaults to the UTC date of that epoch.

### Imager manifest template

`imager/os-list.template.json` is a template, not a flashable manifest. It is
not valid JSON until `make-release.sh` substitutes every token, drops top-level
keys beginning with `_` (the `_comment` note), and validates the result against
the required Imager fields. Tokens are replaced with JSON-encoded values, so
string tokens appear unquoted in the template (`"url": {{IMAGE_URL}}`) and
numeric/array tokens are emitted as numbers/arrays:

| Token | Rendered value |
|---|---|
| `{{IMAGE_URL}}` | `--url-base` + `/buddy3d-camera-pi-zero2w-<version>.img.xz` |
| `{{ICON_URL}}` | `--icon` (defaults to `<url-base>/buddy3d-camera.png`) |
| `{{WEBSITE}}` | `--website` |
| `{{RELEASE_DATE}}` | `--release-date` (default: UTC date of `SOURCE_DATE_EPOCH`) |
| `{{EXTRACT_SIZE}}` | uncompressed `.img` size in bytes |
| `{{EXTRACT_SHA256}}` | uncompressed `.img` SHA-256 |
| `{{DOWNLOAD_SIZE}}` | `.img.xz` size in bytes |
| `{{DOWNLOAD_SHA256}}` | `.img.xz` SHA-256 |
| `{{VERSION}}` | `--version` |
| `{{DEVICES}}` | Imager device tags (see below) |
| `{{INIT_FORMAT}}` | Imager initialisation format (`systemd`) |
| `{{ARCH}}` | Imager architecture (`armv8`, i.e. arm64) |

`make-release.sh` fails if the rendered manifest is unparseable, is missing a
required field, or if any hash/size disagrees with the produced artifacts.

### Raspberry Pi Zero 2 W device identifier

The rendered `devices` value is `["pi3-64bit"]`. This is **verified, not
guessed**: in the official Imager OS list
(`https://downloads.raspberrypi.com/os_list_imagingutility_v4.json`, schema
`doc/json-schema/os-list-schema.json` in `raspberrypi/rpi-imager`) the
`Raspberry Pi Zero 2 W` device entry carries the tags `pi3-64bit` and
`pi3-32bit`, because it shares the BCM2710 family with the Pi 3. There is no
dedicated `zero2w` tag. This image is arm64, so only the 64-bit tag applies;
official 64-bit entries that support the Zero 2 W (for example
`Raspberry Pi OS (64-bit)`) list `pi3-64bit`.

`architecture` is `armv8`, which is Imager's identifier for a 64-bit ARM image.
`init_format` is `systemd` (see the unresolved concern below).

**Trade-off:** because the official tag set has no Zero 2 W-only tag, the
`pi3-64bit` entry also matches Raspberry Pi 3 (Pi 4 uses `pi4-64bit` and Pi 5
uses `pi5-64bit`), so Imager may offer this image for a Pi 3 too. The image
is validated for the Zero 2 W only; the manifest cannot express a stricter
device restriction with the current official tags.

### SBOM and package manifest

`--packages` is copied to the `.packages.txt` artifact and parsed into SPDX
package entries (one per manifest line, with its version). `--build-info`
contributes a single package entry for the image carrying the real
`source_commit`, `builder_revision`, `os_suite`, and `kernel_package` values.
No package data is fabricated: with neither input the SBOM has an empty
`packages` array. The `.spdx.json` is a minimal SPDX 2.3 document
(`spdxVersion`, `dataLicense`, `SPDXID`, `name`, `documentNamespace`,
`creationInfo`).

### Signing

Signing is optional. Without `--key`, `make-release.sh` prints a warning and
leaves the artifact unsigned. With `--key`, it requires `minisign` and the key
file and fails loudly if either is missing. The private key must never be
committed, logged, or baked into the image; only the public key belongs in the
repository. No signing key is present in this checkout, so no signature is
produced here.

### Secret scan

`scripts/scan-secrets.sh <path>...` is run by `make-release.sh` over every
produced artifact, and fails the release if anything matches. It flags private
keys, minisign secret keys, SSH host private keys, a non-empty machine-id,
Wi-Fi PSK values and `.nmconnection` profiles, Prusa tokens, MQTT passwords,
`password_hash` values, and personal usernames/home paths (service-account
homes such as `prusa-cam` are allowed). Matches are printed as `file:line`;
binary files are skipped for content patterns but secret-bearing filenames are
still flagged. Override the personal-username pattern with
`SCAN_PERSONAL_USER_PATTERN` when scanning a different contributor's tree.

**Self-match waiver (AC-35).** A repo-wide run will match the scanner's own
pattern source (`password_hash`, `minisign encrypted secret key`) and the
intentional synthetic-secret fixtures in `tests/`. Those are non-working
placeholders, not real credentials. Scan release artifacts and the uncompressed
image, not the scanner source; when a repo-wide sweep is required, exclude
`image/scripts/scan-secrets.sh` and the secret-scan test fixtures explicitly and
record the exclusion in the release-candidate report.

### Unresolved concerns (WP-2b)

1. **`init_format: systemd` and the read-only overlay root.** Imager's
   `systemd` customisation writes a first-boot script and expects it to persist,
   but this image's ROOT is a read-only overlay with a volatile tmpfs upper
   layer. Bridging Imager-supplied Wi-Fi/hostname/SSH customization into the
   durable DATA partition is a provisioning (WP-3) task. The exact
   `init_format` must be reconfirmed against the released image before publish;
   `PRUSA_IMAGER_INIT_FORMAT` overrides the rendered value.
2. **No release signing key in this checkout (D2).** Signing is implemented but
   untested end-to-end here; `minisign` is not installed and no key exists.
3. **No native arm64 Trixie runner here (D1).** Release images are produced on
   the controlled arm64 runner; `make-release.sh` is host-testable and does not
   claim a foreign-architecture build.
4. **No byte-identical reproducibility claim.** `xz -T1 -9e` with pinned check
   types produces a stable stream for a given xz version, but reproducibility is
   not proven across xz versions.
