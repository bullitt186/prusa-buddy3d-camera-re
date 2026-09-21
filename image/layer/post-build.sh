#!/bin/bash
# post-build hook for the Buddy3D image layer.
#
# rpi-image-gen v2.8.0 invokes IMAGE_ASSET/post-build.sh with the target
# filesystem path after bdebstrap completes (bin/runner phase_args post-build).
# Removes every build-time identity so the released image carries none.
set -eu

fs="$1"

# No persistent machine-id: leave it empty and let systemd generate a fresh
# one on first boot (systemd-machine-id-setup).
rm -f "$fs/etc/machine-id" "$fs/var/lib/dbus/machine-id"
install -m 0644 /dev/null "$fs/etc/machine-id"
ln -sf /etc/machine-id "$fs/var/lib/dbus/machine-id"

# No build-time SSH host key. The reused openssh-server layer already removes
# the postinst keys; ssh-hostkeys-generate.service regenerates them on first
# boot only if SSH is later enabled.
rm -f "$fs"/etc/ssh/ssh_host_*

# No Wi-Fi profile or captured connection.
rm -f "$fs"/etc/NetworkManager/system-connections/*.nmconnection 2>/dev/null || true

# The service account is non-login and its home is /opt/prusa-cam; drop any
# home directory created by the generic user-provisioning layer.
rm -rf "$fs/home/prusa-cam"
