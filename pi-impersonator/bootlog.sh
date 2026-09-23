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
    # Appliance bring-up forensics: the journal is volatile and the root is an
    # overlay, so persist the runtime unit state, NetworkManager view, and the
    # provisioning/hotspot logs here. This is what made the first-boot "no
    # setup hotspot" failure diagnosable without a console.
    echo "--- buddy3d services ($(date -Is)) ---"
    systemctl --failed --no-pager 2>/dev/null
    for unit in data-ready.target pi-persist.service prusa-boot-mode.service \
                prusa-provisioning.service NetworkManager.service; do
        echo "$unit: active=$(systemctl is-active "$unit" 2>/dev/null) enabled=$(systemctl is-enabled "$unit" 2>/dev/null)"
    done
    echo "--- systemctl status ---"
    systemctl status --no-pager -l prusa-boot-mode.service prusa-provisioning.service 2>/dev/null | head -60
    echo "--- nmcli general / device ---"
    nmcli general status 2>/dev/null
    nmcli -t device status 2>/dev/null
    echo "--- nmcli connections ---"
    nmcli -t -f NAME,DEVICE,STATE connection show 2>/dev/null
    echo "--- rfkill ---"
    rfkill list 2>/dev/null
    echo "--- provisioning state / data ---"
    cat /data/prusa-cam/provisioning.json 2>/dev/null || echo "(no provisioning.json)"
    ls -la /data/prusa-cam/ 2>/dev/null
    ls -la /data/network/system-connections/ 2>/dev/null
    echo "--- journal (boot-mode/provisioning/NetworkManager) ---"
    journalctl -b --no-pager -n 300 -u prusa-boot-mode \
        -u prusa-provisioning -u NetworkManager -u data-ready.target 2>/dev/null
    echo
} >> "$LOG" 2>&1 || true

# Keep the small vfat boot partition bounded.
if [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 2000 ]; then
    tail -n 1000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG" 2>/dev/null
fi
