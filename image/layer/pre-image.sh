#!/bin/bash
# pre-image hook for the Buddy3D image layer.
#
# rpi-image-gen v2.8.0 resolves IMAGE_ASSET pre-image hooks as
# `<assetdir>/pre-image.sh` (docs/execution/index.adoc, "single" hooks) and
# invokes them with `<rootfs> <image-output-dir>` (bin/runner phase_args).
# Modelled on image/mbr/simple_dual/pre-image.sh: render the genimage template
# with the configured geometry and fixed MBR disk signature.
set -eu

rootfs=$1
genimg_in=$2

sig="${IGconf_image_disksig:-random}"
if [ "$sig" = "random" ]; then
   sig="0x$(od -An -N4 -tx4 /dev/urandom | tr -d ' ')"
fi

cat genimage.cfg.in.ext4 | sed \
   -e "s|<IMAGE_DIR>|$IGconf_image_outputdir|g" \
   -e "s|<IMAGE_NAME>|$IGconf_image_name|g" \
   -e "s|<IMAGE_SUFFIX>|$IGconf_image_suffix|g" \
   -e "s|<BOOT_SIZE>|$IGconf_image_boot_part_size|g" \
   -e "s|<ROOT_SIZE>|$IGconf_image_root_part_size|g" \
   -e "s|<PERSIST_SIZE>|$IGconf_image_persist_part_size|g" \
   -e "s|<SECTOR_SIZE>|$IGconf_device_sector_size|g" \
   -e "s|<DISK_SIGNATURE>|$sig|g" \
   -e "s|<SETUP>|'$(readlink -ef setup.sh)'|g" \
   -e "s|<MKE2FS_CONF>|'$(readlink -ef mke2fs.conf)'|g" \
   > "${genimg_in}/genimage.cfg"
