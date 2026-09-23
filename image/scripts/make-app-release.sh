#!/usr/bin/env bash
# make-app-release.sh — assemble the signed Buddy3D application release bundle
# consumed by the on-device updater (WP-R5; master AC-29/AC-34, distribution
# plan §7.1).
#
# Usage:
#   make-app-release.sh --version X.Y.Z --out-dir <dir> --wheels <dir>
#       --url-base <https URL>
#       [--source-dir <pi-impersonator>] [--channel stable|alpha]
#       [--key <minisign secret key>] [--source-commit <sha>]
#       [--min-image-version X.Y.Z] [--release-summary <text>]
#       [--release-url <https URL>] [--reboot-required]
#       [--release-date YYYY-MM-DD] [--requirements-lock <file>]
#
# Produces:
#   buddy3d-camera-app-<version>.tar.zst
#   buddy3d-camera-app-<version>.tar.zst.sha256
#   buddy3d-camera-app-<version>.tar.zst.minisig        (only with --key)
#   update-manifest.json
#   update-manifest.json.minisig                        (only with --key)
#
# The bundle's top level contains main.py, requirements.lock and wheels/ so the
# updater can build the release venv offline with:
#   pip install --require-hashes --no-index --find-links wheels -r requirements.lock
# Every archive member is owned uid/gid 0 with a fixed mtime, so the updater's
# strict owner check passes and the tar is reproducible.
#
# The signing key is optional: without --key both the bundle and the manifest
# are left unsigned with a clear warning. With --key, minisign must be installed
# or the script fails loudly. The produced artifacts are passed through
# scan-secrets.sh and the script fails if any secret pattern matches. No secret
# material (including the signing key contents or path) is ever written into an
# artifact or printed.
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$IMAGE_DIR/.." && pwd)"
SCAN_SECRETS="$SCRIPT_DIR/scan-secrets.sh"
DEFAULT_SOURCE_DIR="$REPO_ROOT/pi-impersonator"
DEFAULT_REQUIREMENTS_LOCK="$IMAGE_DIR/requirements.lock"
UPDATER_DIR="$REPO_ROOT/pi-impersonator"

# Documented default archive timestamp when SOURCE_DATE_EPOCH is unset: the
# Unix epoch (1970-01-01T00:00:00Z). Any fixed value works; the point is that
# two runs without an explicit epoch still produce byte-identical archives.
DEFAULT_MTIME_EPOCH=0

die() { echo "ERROR: $*" >&2; exit 1; }
warn() { echo "WARNING: $*" >&2; }

usage() {
   cat <<'USAGE'
Usage:
  make-app-release.sh --version X.Y.Z --out-dir <dir> --wheels <dir>
      --url-base <https URL>
      [--source-dir <pi-impersonator>] [--channel stable|alpha]
      [--key <minisign secret key>] [--source-commit <sha>]
      [--min-image-version X.Y.Z] [--release-summary <text>]
      [--release-url <https URL>] [--reboot-required]
      [--release-date YYYY-MM-DD] [--requirements-lock <file>]

  --version            application version, strict X.Y.Z (required)
  --out-dir            artifact output directory (required)
  --wheels             directory of hash-pinned wheels (required)
  --url-base           https base URL the bundle is published under (required)
  --source-dir         application source tree (default: repo pi-impersonator)
  --channel            release channel: stable (default) or alpha
  --key                minisign secret key; omit to publish unsigned
  --source-commit      source commit recorded in the manifest (default: git HEAD)
  --min-image-version  minimum compatible image version (default: 1.0.0)
  --release-summary    human-readable summary for the manifest
  --release-url        https release page URL (default: --url-base)
  --reboot-required    record reboot_required=true in the manifest
  --release-date       release date, YYYY-MM-DD (validated, informational)
  --requirements-lock  hash-locked requirements file (default: image/requirements.lock)
USAGE
}

VERSION=""
OUT_DIR=""
WHEELS=""
URL_BASE=""
SOURCE_DIR="$DEFAULT_SOURCE_DIR"
CHANNEL="stable"
KEY=""
SOURCE_COMMIT=""
MIN_IMAGE_VERSION="1.0.0"
RELEASE_SUMMARY=""
RELEASE_URL=""
REBOOT_REQUIRED="0"
RELEASE_DATE=""
REQUIREMENTS_LOCK="$DEFAULT_REQUIREMENTS_LOCK"

while [ "$#" -gt 0 ]; do
   case "$1" in
      --version) VERSION="$2"; shift 2 ;;
      --out-dir) OUT_DIR="$2"; shift 2 ;;
      --wheels) WHEELS="$2"; shift 2 ;;
      --url-base) URL_BASE="$2"; shift 2 ;;
      --source-dir) SOURCE_DIR="$2"; shift 2 ;;
      --channel) CHANNEL="$2"; shift 2 ;;
      --key) KEY="$2"; shift 2 ;;
      --source-commit) SOURCE_COMMIT="$2"; shift 2 ;;
      --min-image-version) MIN_IMAGE_VERSION="$2"; shift 2 ;;
      --release-summary) RELEASE_SUMMARY="$2"; shift 2 ;;
      --release-url) RELEASE_URL="$2"; shift 2 ;;
      --reboot-required) REBOOT_REQUIRED="1"; shift ;;
      --release-date) RELEASE_DATE="$2"; shift 2 ;;
      --requirements-lock) REQUIREMENTS_LOCK="$2"; shift 2 ;;
      -h | --help) usage; exit 0 ;;
      *) die "unknown argument: $1" ;;
   esac
done

[ -n "$VERSION" ] || { usage; die "--version is required"; }
[ -n "$OUT_DIR" ] || { usage; die "--out-dir is required"; }
[ -n "$WHEELS" ] || { usage; die "--wheels is required"; }
[ -n "$URL_BASE" ] || { usage; die "--url-base is required"; }

# --- 1. Validate inputs -----------------------------------------------------
# Strict SemVer: exactly X.Y.Z with no leading zeroes, matching the updater's
# parse_semver. A pre-release suffix is deliberately invalid: alpha is a release
# channel, not a version suffix (see the alpha/stable convention in
# .opencode/plans/appliance-remaining-wps.md).
SEMVER_RE='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
printf '%s' "$VERSION" | grep -Eq "$SEMVER_RE" || \
   die "--version is not strict X.Y.Z: $VERSION"
printf '%s' "$MIN_IMAGE_VERSION" | grep -Eq "$SEMVER_RE" || \
   die "--min-image-version is not strict X.Y.Z: $MIN_IMAGE_VERSION"

case "$CHANNEL" in
   stable | alpha) ;;
   *) die "--channel must be stable or alpha: $CHANNEL" ;;
esac

printf '%s' "$URL_BASE" | grep -Eq '^https://[^/[:space:]]+' || \
   die "--url-base must be an https URL: $URL_BASE"
URL_BASE="${URL_BASE%/}"
[ -n "$RELEASE_URL" ] || RELEASE_URL="$URL_BASE"
printf '%s' "$RELEASE_URL" | grep -Eq '^https://[^/[:space:]]+' || \
   die "--release-url must be an https URL: $RELEASE_URL"

if [ -n "$RELEASE_DATE" ]; then
   printf '%s' "$RELEASE_DATE" | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' || \
      die "--release-date must be YYYY-MM-DD: $RELEASE_DATE"
fi

[ -d "$SOURCE_DIR" ] || die "--source-dir does not exist: $SOURCE_DIR"
[ -f "$SOURCE_DIR/main.py" ] || die "--source-dir has no main.py: $SOURCE_DIR"
[ -d "$WHEELS" ] || die "--wheels directory does not exist: $WHEELS"
[ -f "$REQUIREMENTS_LOCK" ] || die "requirements lock not found: $REQUIREMENTS_LOCK"
[ -f "$SCAN_SECRETS" ] || die "missing secret scanner: $SCAN_SECRETS"

if [ -z "$SOURCE_COMMIT" ]; then
   SOURCE_COMMIT="$(git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null || true)"
   [ -n "$SOURCE_COMMIT" ] || SOURCE_COMMIT="unknown"
fi

# Channel/version guard: an alpha tag must never be published to the stable
# channel and a stable tag must never be published to alpha. When HEAD is
# exactly tagged (the release workflow's state), the tag decides the channel.
HEAD_TAG="$(git -C "$SOURCE_DIR" describe --exact-match --tags 2>/dev/null || true)"
if [ -n "$HEAD_TAG" ]; then
   case "$HEAD_TAG" in
      alpha-v*)
         [ "$CHANNEL" = "alpha" ] || \
            die "tag $HEAD_TAG is an alpha tag; refusing to publish it to the stable channel"
         ;;
      v*)
         [ "$CHANNEL" = "stable" ] || \
            die "tag $HEAD_TAG is a stable tag; refusing to publish it to the alpha channel"
         ;;
   esac
fi

if [ -n "$KEY" ]; then
   command -v minisign >/dev/null 2>&1 || \
      die "--key was given but minisign is not installed; refusing to produce an unsigned release"
fi

if [ -z "$RELEASE_SUMMARY" ]; then
   RELEASE_SUMMARY="Buddy3D camera application ${VERSION}"
fi

# Every pinned requirement must have a matching wheel/sdist in --wheels, so a
# bundle that cannot build its offline venv is never published.
export MR_LOCK="$REQUIREMENTS_LOCK"
export MR_WHEELS="$WHEELS"
python3 - <<'PY'
import glob
import os
import re
import sys

lock = os.environ["MR_LOCK"]
wheels = os.environ["MR_WHEELS"]
requirement = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)")


def normalize(name):
    return re.sub(r"[-_.]+", "_", name).lower()


missing = []
seen = set()
with open(lock, encoding="utf-8") as handle:
    for line in handle:
        match = requirement.match(line)
        if match is None:
            continue
        name, version = match.group(1), match.group(2)
        if (name, version) in seen:
            continue
        seen.add((name, version))
        norm = normalize(name)
        candidates = [
            os.path.join(wheels, f"{norm}-{version}-*.whl"),
            os.path.join(wheels, f"{norm}-{version}.tar.gz"),
            os.path.join(wheels, f"{norm}-{version}.zip"),
            os.path.join(wheels, f"{name}-{version}-*.whl"),
            os.path.join(wheels, f"{name}-{version}.tar.gz"),
        ]
        if not any(glob.glob(candidate) for candidate in candidates):
            missing.append(f"{name}=={version}")

if missing:
    print(
        "missing wheel/sdist for pinned requirement(s): " + ", ".join(missing),
        file=sys.stderr,
    )
    sys.exit(1)
if not seen:
    print("no pinned requirements found in " + lock, file=sys.stderr)
    sys.exit(1)
PY

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

# --- 2. Deterministic staging tree ------------------------------------------
# Honour SOURCE_DATE_EPOCH when supplied; otherwise use the documented default
# so two runs still agree byte-for-byte.
if [ -n "${SOURCE_DATE_EPOCH:-}" ]; then
   MTIME_EPOCH="$SOURCE_DATE_EPOCH"
else
   MTIME_EPOCH="$DEFAULT_MTIME_EPOCH"
   export SOURCE_DATE_EPOCH="$MTIME_EPOCH"
fi
printf '%s' "$MTIME_EPOCH" | grep -Eq '^[0-9]+$' || \
   die "SOURCE_DATE_EPOCH must be an integer: $MTIME_EPOCH"

STAGING="$(mktemp -d "${TMPDIR:-/tmp}/buddy3d-app-release.XXXXXX")"
VALIDATE_TAR=""
cleanup() {
   rm -rf "$STAGING"
   if [ -n "$VALIDATE_TAR" ]; then
      rm -f "$VALIDATE_TAR"
   fi
   return 0
}
trap cleanup EXIT

# The exclusion list mirrors install-factory-app.sh's factory tree layout. VCS
# metadata is additionally excluded: a --source-dir pointed at a git checkout
# must never embed history (which can contain secrets) in a published bundle.
# Permission bits are preserved (-p) so the final archive faithfully reflects
# the source; the updater member validation below then rejects setuid/setgid
# instead of silently normalizing it away.
tar -C "$SOURCE_DIR" \
   --exclude='./tests' --exclude='tests' \
   --exclude='./__pycache__' --exclude='__pycache__' \
   --exclude='*.pyc' \
   --exclude='config.ini' \
   --exclude='*.example' \
   --exclude='venv' \
   --exclude='deploy.sh' \
   --exclude='bootstrap.sh' \
   --exclude='README.md' \
   --exclude='backups' \
   --exclude='.git' \
   -cf - . | tar -C "$STAGING" --no-same-owner -xpf -

cp "$REQUIREMENTS_LOCK" "$STAGING/requirements.lock"
mkdir -p "$STAGING/wheels"
cp -a "$WHEELS/." "$STAGING/wheels/"

# --- 3. Deterministic bundle ------------------------------------------------
BASE="buddy3d-camera-app-${VERSION}"
BUNDLE="$OUT_DIR/${BASE}.tar.zst"
SHA_FILE="${BUNDLE}.sha256"
MANIFEST="$OUT_DIR/update-manifest.json"
MANIFEST_SIG="${MANIFEST}.minisig"
BUNDLE_SIG="${BUNDLE}.minisig"

# --sort=name + fixed mtime + numeric owner 0 + GNU tar format make the archive
# reproducible for a given tar version; zstd -T1 removes the host-core-count
# variation (auto-threading, -T0, is not deterministic).
tar --sort=name --format=gnu --mtime="@$MTIME_EPOCH" \
   --owner=0 --group=0 --numeric-owner \
   -C "$STAGING" -cf - . | zstd -19 -T1 -q -f -o "$BUNDLE"

# --- 3b. Validate the produced archive through the real updater -------------
# The tar preserves source modes and links, so a setuid/setgid bit or an
# absolute/escaping link would make the device reject the bundle. Validate the
# exact members the updater will see before anything is hashed or signed.
VALIDATE_TAR="$(mktemp "${TMPDIR:-/tmp}/buddy3d-bundle-validate.XXXXXX")"
zstd -dc "$BUNDLE" > "$VALIDATE_TAR"
export MR_BUNDLE_TAR="$VALIDATE_TAR"
export MR_UPDATER_DIR="$UPDATER_DIR"
python3 - <<'PY'
import os
import sys
import tarfile

sys.path.insert(0, os.environ["MR_UPDATER_DIR"])
import updater  # noqa: E402

with tarfile.open(os.environ["MR_BUNDLE_TAR"], "r:") as archive:
    members = archive.getmembers()
ok, reason = updater.validate_archive_members(
    members, os.environ.get("TMPDIR") or "/tmp")
if not ok:
    print(
        "bundle rejected by updater member validation: " + reason,
        file=sys.stderr,
    )
    sys.exit(1)
PY

BUNDLE_SHA256="$(sha256sum "$BUNDLE" | awk '{print $1}')"
BUNDLE_SIZE="$(stat -c %s "$BUNDLE")"
printf '%s  %s\n' "$BUNDLE_SHA256" "$(basename "$BUNDLE")" > "$SHA_FILE"

# --- 4. Signed update manifest ----------------------------------------------
BUNDLE_URL="${URL_BASE}/${BASE}.tar.zst"

export MR_MANIFEST="$MANIFEST"
export MR_VERSION="$VERSION"
export MR_CHANNEL="$CHANNEL"
export MR_COMMIT="$SOURCE_COMMIT"
export MR_MIN_IMAGE="$MIN_IMAGE_VERSION"
export MR_BUNDLE_URL="$BUNDLE_URL"
export MR_BUNDLE_SHA="$BUNDLE_SHA256"
export MR_BUNDLE_SIZE="$BUNDLE_SIZE"
export MR_SUMMARY="$RELEASE_SUMMARY"
export MR_RELEASE_URL="$RELEASE_URL"
export MR_REBOOT="$REBOOT_REQUIRED"
export MR_UPDATER_DIR="$UPDATER_DIR"

# The manifest is validated through the real updater.parse_manifest before it is
# written, so a release that the device would reject can never be published.
python3 - <<'PY'
import json
import os
import sys

sys.path.insert(0, os.environ["MR_UPDATER_DIR"])
import updater  # noqa: E402

manifest = {
    "schema_version": 1,
    "version": os.environ["MR_VERSION"],
    "channel": os.environ["MR_CHANNEL"],
    "source_commit": os.environ["MR_COMMIT"],
    "min_image_version": os.environ["MR_MIN_IMAGE"],
    "bundle_url": os.environ["MR_BUNDLE_URL"],
    "bundle_sha256": os.environ["MR_BUNDLE_SHA"],
    "bundle_size": int(os.environ["MR_BUNDLE_SIZE"]),
    "release_summary": os.environ["MR_SUMMARY"],
    "release_url": os.environ["MR_RELEASE_URL"],
    "reboot_required": os.environ["MR_REBOOT"] == "1",
}

updater.parse_manifest(manifest)

with open(os.environ["MR_MANIFEST"], "w", encoding="utf-8") as handle:
    json.dump(manifest, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

# --- 5. Optional signing ----------------------------------------------------
if [ -n "$KEY" ]; then
   [ -f "$KEY" ] || die "the --key signing key file was not found"
   minisign -S -s "$KEY" -m "$BUNDLE" -x "$BUNDLE_SIG"
   minisign -S -s "$KEY" -m "$MANIFEST" -x "$MANIFEST_SIG"
   echo "signed bundle and manifest with minisign"
else
   warn "no --key provided; the bundle and manifest are UNSIGNED and must not be published as stable"
fi

# --- 6. Secret scan of the staged tree and produced artifacts (AC-35) -------
# The compressed bundle is binary, so scan-secrets cannot see inside it. Scan
# the uncompressed staged tree before publishing:
#   * every .py file with the high-signal source-tree mode (ordinary code that
#     merely assigns a runtime token or an empty PSK variable is not flagged),
#     and
#   * every other staged file (minus wheels/) with the default artifact mode,
#     so a bundled secrets.toml/.env/settings file carrying a credential is
#     caught even though it is not Python source.
# Config-like filenames are additionally rejected unless explicitly allowlisted,
# so an unexpected *.toml/*.env/*.ini/secrets* file cannot ride along even when
# it currently contains no match.
ALLOWED_STAGED_CONFIG="requirements.lock"
staged_py=()
staged_other=()
while IFS= read -r -d '' f; do
   rel="${f#"$STAGING"/}"
   case "$rel" in
      wheels/*) continue ;;
      *.py) staged_py+=("$f") ;;
      *) staged_other+=("$f") ;;
   esac
done < <(find "$STAGING" -type f -print0 | sort -z)

if [ "${#staged_py[@]}" -gt 0 ]; then
   bash "$SCAN_SECRETS" --source-tree "${staged_py[@]}" || \
      die "secret scan of the staged application source failed; refusing to publish artifacts"
fi
if [ "${#staged_other[@]}" -gt 0 ]; then
   bash "$SCAN_SECRETS" "${staged_other[@]}" || \
      die "secret scan of the staged application data files failed; refusing to publish artifacts"
   for f in "${staged_other[@]}"; do
      base="${f##*/}"
      [ "$base" = "$ALLOWED_STAGED_CONFIG" ] && continue
      case "$base" in
         *.toml | *.env | .env* | *.ini | *.cfg | *.conf | *.properties | secrets*)
            die "unexpected config-like file in the application tree: ${f#"$STAGING"/}"
            ;;
      esac
   done
fi

scan_targets=("$BUNDLE" "$SHA_FILE" "$MANIFEST")
[ -f "$BUNDLE_SIG" ] && scan_targets+=("$BUNDLE_SIG")
[ -f "$MANIFEST_SIG" ] && scan_targets+=("$MANIFEST_SIG")
bash "$SCAN_SECRETS" "${scan_targets[@]}" || die "secret scan failed; refusing to publish artifacts"

echo "application release artifacts in $OUT_DIR:"
echo "  bundle:   $(basename "$BUNDLE")  ($BUNDLE_SIZE bytes, sha256 $BUNDLE_SHA256)"
echo "  manifest: $(basename "$MANIFEST")  (version $VERSION, channel $CHANNEL)"
if [ -n "$RELEASE_DATE" ]; then
   echo "  date:     $RELEASE_DATE"
fi
exit 0
