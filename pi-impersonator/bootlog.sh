#!/bin/sh
# GAP-DEVICE forensics: persist a boot-reason + health snapshot to the real vfat
# boot partition, which survives reboots. The root filesystem is a read-only
# overlay with a RAM upper layer and the journal is volatile, so an unexpected
# reboot (power cut, watchdog reset, thermal trip) otherwise leaves no trace.
#
# Installed by deploy.sh as /etc/systemd/system/bootlog.service (oneshot).
LOG=/boot/firmware/bootlog.txt
{
    echo "=== boot $(date -Is) uptime=$(cut -d. -f1 /proc/uptime)s ==="
    echo "cmdline:   $(cat /proc/cmdline 2>/dev/null)"
    echo "model:     $(tr -d '\000' < /proc/device-tree/model 2>/dev/null)"
    echo "rpi-rsts:  $(vcgencmd get_rsts 2>/dev/null)"
    echo "throttled: $(vcgencmd get_throttled 2>/dev/null)"
    echo "temp:      $(vcgencmd measure_temp 2>/dev/null)"
    echo "--- dmesg (first 40 lines) ---"
    dmesg -T 2>/dev/null | head -40
    echo
} >> "$LOG" 2>&1 || true

# Keep the small vfat boot partition bounded.
if [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 2000 ]; then
    tail -n 1000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG" 2>/dev/null
fi
