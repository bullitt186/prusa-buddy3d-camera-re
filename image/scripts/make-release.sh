#!/usr/bin/env bash
# make-release.sh — assemble the Buddy3D appliance release artifacts.
#
# Usage:
#   make-release.sh --image <file> --version <semver> --out-dir <dir>
#       [--release-date YYYY-MM-DD] [--url-base URL] [--icon URL]
#       [--website URL] [--packages <file>] [--build-info <file>]
#       [--key <minisign.key>]
#
# Produces (AC-14, AC-34, AC-35):
#   buddy3d-camera-pi-zero2w-<version>.img.xz
#   buddy3d-camera-pi-zero2w-<version>.img.xz.sha256
#   buddy3d-camera-pi-zero2w-<version>.img.xz.minisig   (only with --key)
#   buddy3d-camera-pi-zero2w-<version>.spdx.json
#   buddy3d-camera-pi-zero2w-<version>.packages.txt     (only with --packages)
#   buddy3d-camera-os-list.json                         (Raspberry Pi Imager)
#
# The signing key is never required: without --key the artifact is left
# unsigned with a clear warning. With --key, minisign must be installed or the
# script fails loudly. The produced artifacts are passed through
# scan-secrets.sh and the script fails if any secret pattern matches.
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$IMAGE_DIR/imager/os-list.template.json"
SCAN_SECRETS="$SCRIPT_DIR/scan-secrets.sh"

die() { echo "ERROR: $*" >&2; exit 1; }
warn() { echo "WARNING: $*" >&2; }

usage() {
   cat <<'USAGE'
Usage:
  make-release.sh --image <file> --version <semver> --out-dir <dir>
      [--release-date YYYY-MM-DD] [--url-base URL] [--icon URL]
      [--website URL] [--packages <file>] [--build-info <file>]
      [--key <minisign.key>]
USAGE
}

IMAGE=""
VERSION=""
OUT_DIR=""
RELEASE_DATE=""
URL_BASE=""
ICON=""
WEBSITE=""
PACKAGES=""
BUILD_INFO=""
KEY=""

while [ "$#" -gt 0 ]; do
   case "$1" in
      --image) IMAGE="$2"; shift 2 ;;
      --version) VERSION="$2"; shift 2 ;;
      --out-dir) OUT_DIR="$2"; shift 2 ;;
      --release-date) RELEASE_DATE="$2"; shift 2 ;;
      --url-base) URL_BASE="$2"; shift 2 ;;
      --icon) ICON="$2"; shift 2 ;;
      --website) WEBSITE="$2"; shift 2 ;;
      --packages) PACKAGES="$2"; shift 2 ;;
      --build-info) BUILD_INFO="$2"; shift 2 ;;
      --key) KEY="$2"; shift 2 ;;
      -h | --help) usage; exit 0 ;;
      *) die "unknown argument: $1" ;;
   esac
done

[ -n "$IMAGE" ] || { usage; die "--image is required"; }
[ -n "$VERSION" ] || { usage; die "--version is required"; }
[ -n "$OUT_DIR" ] || { usage; die "--out-dir is required"; }
[ -f "$IMAGE" ] || die "image does not exist: $IMAGE"

# --- 1. Validate inputs -----------------------------------------------------
if ! printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$'; then
   die "--version is not valid SemVer: $VERSION"
fi
if [ -n "$PACKAGES" ]; then
   [ -f "$PACKAGES" ] || die "--packages file does not exist: $PACKAGES"
fi
if [ -n "$BUILD_INFO" ]; then
   [ -f "$BUILD_INFO" ] || die "--build-info file does not exist: $BUILD_INFO"
fi
if [ -n "$KEY" ]; then
   command -v minisign >/dev/null 2>&1 || \
      die "--key was given but minisign is not installed; refusing to produce an unsigned release"
fi
[ -f "$TEMPLATE" ] || die "missing Imager template: $TEMPLATE"
[ -f "$SCAN_SECRETS" ] || die "missing secret scanner: $SCAN_SECRETS"

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

# --- 2. Deterministic compression (AC-14) -----------------------------------
if [ -n "${SOURCE_DATE_EPOCH:-}" ]; then
   export SOURCE_DATE_EPOCH
else
   SOURCE_DATE_EPOCH="$(stat -c %Y "$IMAGE")"
   export SOURCE_DATE_EPOCH
fi

BASE="buddy3d-camera-pi-zero2w-${VERSION}"
IMG_XZ="$OUT_DIR/${BASE}.img.xz"
SHA_FILE="${IMG_XZ}.sha256"
SPDX_FILE="$OUT_DIR/${BASE}.spdx.json"
PKG_OUT="$OUT_DIR/${BASE}.packages.txt"
OS_LIST="$OUT_DIR/buddy3d-camera-os-list.json"

# xz writes no timestamps; -T1 forces single-threaded compression, which with
# the fixed preset and check type is deterministic for a given xz version
# (auto-threading, -T0, varies with host core count and memory limits). This is
# not a byte-identical reproducibility claim across xz versions.
xz -T1 -9e --check=crc64 -c "$IMAGE" > "$IMG_XZ"
touch -d "@$SOURCE_DATE_EPOCH" "$IMG_XZ" 2>/dev/null || true

DOWNLOAD_SHA256="$(sha256sum "$IMG_XZ" | awk '{print $1}')"
DOWNLOAD_SIZE="$(stat -c %s "$IMG_XZ")"
EXTRACT_SHA256="$(sha256sum "$IMAGE" | awk '{print $1}')"
EXTRACT_SIZE="$(stat -c %s "$IMAGE")"

printf '%s  %s\n' "$DOWNLOAD_SHA256" "$(basename "$IMG_XZ")" > "$SHA_FILE"

# --- 3. Release metadata ----------------------------------------------------
if [ -z "$RELEASE_DATE" ]; then
   RELEASE_DATE="$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y-%m-%d 2>/dev/null || date -u +%Y-%m-%d)"
fi
printf '%s' "$RELEASE_DATE" | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' || \
   die "--release-date must be YYYY-MM-DD: $RELEASE_DATE"
CREATED="$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf '%sT00:00:00Z' "$RELEASE_DATE")"

if [ -z "$URL_BASE" ]; then
   URL_BASE="https://example.invalid/buddy3d-camera/releases/v${VERSION}"
   warn "--url-base not provided; using placeholder $URL_BASE (override for a real release)"
fi
URL_BASE="${URL_BASE%/}"
IMAGE_URL="$URL_BASE/${BASE}.img.xz"
if [ -z "$ICON" ]; then
   ICON="$URL_BASE/buddy3d-camera.png"
   warn "--icon not provided; defaulting to $ICON"
fi
if [ -z "$WEBSITE" ]; then
   WEBSITE="https://example.invalid/buddy3d-camera"
   warn "--website not provided; defaulting to $WEBSITE"
fi

# Raspberry Pi Zero 2 W is matched by the official Imager tag pi3-64bit (the
# 64-bit BCM2710 family tag) in os_list_imagingutility_v4.json; there is no
# dedicated zero2w tag. The image is arm64, so only the 64-bit tag applies.
DEVICES='["pi3-64bit"]'
INIT_FORMAT="${PRUSA_IMAGER_INIT_FORMAT:-systemd}"
ARCH="${PRUSA_IMAGER_ARCH:-armv8}"

# --- 4. Package manifest (when supplied) ------------------------------------
if [ -n "$PACKAGES" ]; then
   sed -e 's/\r$//' -e '/^[[:space:]]*$/d' "$PACKAGES" | sort -u > "$PKG_OUT"
fi

# --- 5. SPDX 2.3 SBOM -------------------------------------------------------
# Only real data goes in: package entries come from the supplied manifest and
# build-info. No package is fabricated when no input is supplied.
export MR_SPDX_FILE="$SPDX_FILE"
export MR_SPDX_NAME="$BASE"
export MR_SPDX_NAMESPACE="$URL_BASE/spdx/${BASE}-${EXTRACT_SHA256}"
export MR_SPDX_CREATED="$CREATED"
export MR_SPDX_VERSION="$VERSION"
export MR_PACKAGES_FILE="$PKG_OUT"
export MR_BUILD_INFO_FILE="$BUILD_INFO"

python3 - <<'PY'
import json
import os
import re


def spdx_id(name):
    return "SPDXRef-Package-" + re.sub(r"[^A-Za-z0-9.-]", "-", name)


packages = []
seen = set()


def add_package(name, version=None, source_info=None):
    key = (name, version)
    if key in seen or not name:
        return
    seen.add(key)
    pkg = {
        "SPDXID": spdx_id(name),
        "name": name,
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
    }
    if version:
        pkg["versionInfo"] = version
    if source_info:
        pkg["sourceInfo"] = source_info
    packages.append(pkg)


packages_file = os.environ.get("MR_PACKAGES_FILE", "")
if packages_file and os.path.exists(packages_file):
    with open(packages_file, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            name = parts[0]
            version = parts[1].strip() if len(parts) > 1 else ""
            if not version and "=" in name:
                name, version = name.split("=", 1)
            add_package(name, version or None)

build_info_file = os.environ.get("MR_BUILD_INFO_FILE", "")
if build_info_file and os.path.exists(build_info_file):
    with open(build_info_file, encoding="utf-8") as handle:
        build_info = json.load(handle)
    fields = ("source_commit", "builder_revision", "os_suite", "kernel_package")
    source_info = ", ".join(
        "%s=%s" % (key, build_info[key]) for key in fields if build_info.get(key)
    )
    add_package(
        os.environ["MR_SPDX_NAME"],
        os.environ.get("MR_SPDX_VERSION"),
        source_info or None,
    )

document = {
    "spdxVersion": "SPDX-2.3",
    "dataLicense": "CC0-1.0",
    "SPDXID": "SPDXRef-DOCUMENT",
    "name": os.environ["MR_SPDX_NAME"],
    "documentNamespace": os.environ["MR_SPDX_NAMESPACE"],
    "creationInfo": {
        "created": os.environ["MR_SPDX_CREATED"],
        "creators": ["Tool: make-release.sh"],
    },
    "packages": packages,
}

with open(os.environ["MR_SPDX_FILE"], "w", encoding="utf-8") as handle:
    json.dump(document, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

# --- 6. Render and validate the Raspberry Pi Imager OS list -----------------
export MR_TEMPLATE="$TEMPLATE"
export MR_OS_LIST="$OS_LIST"
export MR_IMAGE_URL="$IMAGE_URL"
export MR_ICON="$ICON"
export MR_WEBSITE="$WEBSITE"
export MR_RELEASE_DATE="$RELEASE_DATE"
export MR_EXTRACT_SIZE="$EXTRACT_SIZE"
export MR_EXTRACT_SHA256="$EXTRACT_SHA256"
export MR_DOWNLOAD_SIZE="$DOWNLOAD_SIZE"
export MR_DOWNLOAD_SHA256="$DOWNLOAD_SHA256"
export MR_VERSION="$VERSION"
export MR_DEVICES="$DEVICES"
export MR_INIT_FORMAT="$INIT_FORMAT"
export MR_ARCH="$ARCH"

python3 - <<'PY'
import json
import os


def env(name):
    return os.environ[name]


def escaped(value):
    return json.dumps(value)[1:-1]


with open(env("MR_TEMPLATE"), encoding="utf-8") as handle:
    text = handle.read()

replacements = {
    "{{IMAGE_URL}}": escaped(env("MR_IMAGE_URL")),
    "{{ICON_URL}}": escaped(env("MR_ICON")),
    "{{WEBSITE}}": escaped(env("MR_WEBSITE")),
    "{{RELEASE_DATE}}": escaped(env("MR_RELEASE_DATE")),
    "{{EXTRACT_SIZE}}": str(int(env("MR_EXTRACT_SIZE"))),
    "{{EXTRACT_SHA256}}": escaped(env("MR_EXTRACT_SHA256")),
    "{{DOWNLOAD_SIZE}}": str(int(env("MR_DOWNLOAD_SIZE"))),
    "{{DOWNLOAD_SHA256}}": escaped(env("MR_DOWNLOAD_SHA256")),
    "{{VERSION}}": escaped(env("MR_VERSION")),
    "{{DEVICES}}": env("MR_DEVICES"),
    "{{INIT_FORMAT}}": escaped(env("MR_INIT_FORMAT")),
    "{{ARCH}}": escaped(env("MR_ARCH")),
}
for token, value in replacements.items():
    text = text.replace(token, value)

document = json.loads(text)
for key in list(document):
    if key.startswith("_"):
        del document[key]

if set(document) - {"imager", "os_list"}:
    raise SystemExit("rendered os-list has unexpected top-level keys: %s" % sorted(document))
if not document.get("os_list"):
    raise SystemExit("rendered os-list has no entries")

entry = document["os_list"][0]
required = [
    "name", "description", "icon", "url", "extract_size", "extract_sha256",
    "image_download_size", "image_download_sha256", "release_date", "devices",
    "init_format", "architecture",
]
missing = [field for field in required if field not in entry]
if missing:
    raise SystemExit("rendered os-list is missing required fields: %s" % missing)

checks = {
    "url": env("MR_IMAGE_URL"),
    "icon": env("MR_ICON"),
    "website": env("MR_WEBSITE"),
    "release_date": env("MR_RELEASE_DATE"),
    "extract_sha256": env("MR_EXTRACT_SHA256"),
    "image_download_sha256": env("MR_DOWNLOAD_SHA256"),
    "init_format": env("MR_INIT_FORMAT"),
    "architecture": env("MR_ARCH"),
}
for field, expected in checks.items():
    if entry.get(field) != expected:
        raise SystemExit("os-list %s=%r does not match %r" % (field, entry.get(field), expected))

if int(entry["extract_size"]) != int(env("MR_EXTRACT_SIZE")):
    raise SystemExit("os-list extract_size does not match the extracted image")
if int(entry["image_download_size"]) != int(env("MR_DOWNLOAD_SIZE")):
    raise SystemExit("os-list image_download_size does not match the compressed image")
if entry["devices"] != json.loads(env("MR_DEVICES")):
    raise SystemExit("os-list devices does not match the expected identifier")
if env("MR_VERSION") not in entry["name"]:
    raise SystemExit("os-list name does not contain the version")

with open(env("MR_OS_LIST"), "w", encoding="utf-8") as handle:
    json.dump(document, handle, indent=2)
    handle.write("\n")
PY

# --- 7. Optional signing ----------------------------------------------------
if [ -n "$KEY" ]; then
   [ -f "$KEY" ] || die "signing key not found: $KEY"
   minisign -S -s "$KEY" -m "$IMG_XZ" -x "${IMG_XZ}.minisig"
   echo "signed: ${IMG_XZ}.minisig"
else
   warn "no --key provided; skipping minisign signature (artifact is unsigned)"
fi

# --- 8. Secret scan of the produced artifacts (AC-35) -----------------------
scan_targets=("$IMG_XZ" "$SHA_FILE" "$SPDX_FILE" "$OS_LIST")
[ -f "$PKG_OUT" ] && scan_targets+=("$PKG_OUT")
[ -f "${IMG_XZ}.minisig" ] && scan_targets+=("${IMG_XZ}.minisig")
bash "$SCAN_SECRETS" "${scan_targets[@]}" || die "secret scan failed; refusing to publish artifacts"

echo "release artifacts in $OUT_DIR:"
echo "  image:   $(basename "$IMG_XZ")  ($DOWNLOAD_SIZE bytes, sha256 $DOWNLOAD_SHA256)"
echo "  sbom:    $(basename "$SPDX_FILE")"
echo "  imager:  $(basename "$OS_LIST")"
