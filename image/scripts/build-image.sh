#!/usr/bin/env bash
# Deterministic Buddy3D appliance image build entry point (AC-13/AC-14).
#
# Usage:
#   RPI_IMAGE_GEN_DIR=/path/to/rpi-image-gen image/scripts/build-image.sh
#
# Steps:
#   1. Verify the checkout at $RPI_IMAGE_GEN_DIR is exactly the revision pinned
#      in image/rpi-image-gen.lock, and that it advertises the documented
#      capabilities this config relies on (-S source dir, custom image layers).
#   2. Set SOURCE_DATE_EPOCH from the source commit timestamp.
#   3. Run the pinned builder with the composition in
#      image/config/buddy3d-pi-zero2w.yaml.
#   4. Fail if the populated ROOT filesystem exceeds 75% of its 4 GiB capacity,
#      or if the extracted image exceeds an 8 GB card budget.
#
# This script does not sign, compress, or publish anything; that is WP-7.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE_DIR="$REPO_ROOT/image"
LOCK="$IMAGE_DIR/rpi-image-gen.lock"
CONFIG="$IMAGE_DIR/config/buddy3d-pi-zero2w.yaml"

die() { echo "ERROR: $*" >&2; exit 1; }

read_lock() {
   awk -F= -v key="$1" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' "$LOCK"
}

LOCK_URL="$(read_lock url)"
LOCK_TAG="$(read_lock tag)"
LOCK_COMMIT="$(read_lock commit)"
[ -n "$LOCK_COMMIT" ] || die "no commit in $LOCK"

: "${RPI_IMAGE_GEN_DIR:?set RPI_IMAGE_GEN_DIR to a checkout of $LOCK_URL ($LOCK_TAG)}"
[ -d "$RPI_IMAGE_GEN_DIR/.git" ] || die "$RPI_IMAGE_GEN_DIR is not a git checkout"

# --- 1. Pin verification ----------------------------------------------------
ACTUAL_COMMIT="$(git -C "$RPI_IMAGE_GEN_DIR" rev-parse HEAD)"
if [ "$ACTUAL_COMMIT" != "$LOCK_COMMIT" ]; then
   die "rpi-image-gen pin mismatch: expected $LOCK_COMMIT ($LOCK_TAG), found $ACTUAL_COMMIT"
fi

# Fail loudly if the pinned revision lacks the documented mechanisms the
# composition depends on (custom image layers + the -S source directory).
for path in \
   rpi-image-gen \
   bin/runner \
   bin/image2json \
   site/layer_manager.py \
   docs/config/index.adoc \
   docs/execution/index.adoc \
   image/mbr/simple_dual/genimage.cfg.in.ext4; do
   [ -e "$RPI_IMAGE_GEN_DIR/$path" ] || \
      die "pinned rpi-image-gen is missing $path; refusing to build"
done
# -S is a `build` subcommand option, so query that help level (the top-level
# help does not list it).
if ! "$RPI_IMAGE_GEN_DIR/rpi-image-gen" build --help 2>&1 | grep -q -- '-S'; then
   die "pinned rpi-image-gen does not advertise the -S source-directory capability"
fi

# --- 2. Reproducibility inputs ---------------------------------------------
SOURCE_DATE_EPOCH="$(git -C "$REPO_ROOT" log -1 --format=%ct)"
export SOURCE_DATE_EPOCH
SOURCE_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD)"
VERSION="$(git -C "$REPO_ROOT" describe --tags --always --dirty 2>/dev/null || echo 0.0.0+local)"
WORKROOT="${WORKROOT:-$IMAGE_DIR/work}"

echo "builder:   $LOCK_URL @ $LOCK_COMMIT ($LOCK_TAG)"
echo "source:    $SOURCE_COMMIT ($VERSION)"
echo "epoch:     $SOURCE_DATE_EPOCH"
echo "workroot:  $WORKROOT"

# --- 3. Build ---------------------------------------------------------------
"$RPI_IMAGE_GEN_DIR/rpi-image-gen" build \
   -S "$IMAGE_DIR" \
   -c "$CONFIG" \
   -B "$WORKROOT" \
   -- "IGconf_artefact_version=$VERSION" \
      "PRUSA_SOURCE_COMMIT=$SOURCE_COMMIT" \
      "RPI_IMAGE_GEN_REVISION=$LOCK_COMMIT"

# --- 4a. ROOT utilisation must stay below 75% of 4 GiB (AC-13) --------------
root_img="$(find "$WORKROOT" -name 'root.ext4' -print -quit 2>/dev/null || true)"
[ -n "$root_img" ] || die "no root.ext4 under $WORKROOT; cannot verify ROOT utilisation"

read -r blocks free_blocks < <(dumpe2fs -h "$root_img" 2>/dev/null | awk -F: '
   /Block count/ { gsub(/[^0-9]/, "", $2); b = $2 }
   /Free blocks/ { gsub(/[^0-9]/, "", $2); f = $2 }
   END { print b, f }')
if [ -z "${blocks:-}" ] || [ -z "${free_blocks:-}" ]; then
   die "dumpe2fs could not read $root_img (install e2fsprogs)"
fi
used_pct=$(( (blocks - free_blocks) * 100 / blocks ))
if [ "$used_pct" -gt 75 ]; then
   die "ROOT utilisation ${used_pct}% exceeds the 75% threshold (AC-13)"
fi
echo "ROOT utilisation: ${used_pct}% (limit 75%)"

# --- 4b. Extracted image must fit an 8 GB card (AC-13) ----------------------
img="$(find "$WORKROOT" -maxdepth 3 -name '*.img' -print -quit 2>/dev/null || true)"
[ -n "$img" ] || die "no extracted .img under $WORKROOT"
img_bytes="$(stat -c%s "$img")"
card_budget=$(( 8 * 1000 * 1000 * 1000 ))
if [ "$img_bytes" -gt "$card_budget" ]; then
   die "extracted image $img_bytes bytes exceeds the 8 GB card budget $card_budget"
fi
echo "image size: $img_bytes bytes (8 GB budget $card_budget)"
echo "build complete: $img"
