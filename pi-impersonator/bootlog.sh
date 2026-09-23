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
    # Camera-target forensics: after claim the runtime owns the device. Capture
    # the unit states, whether libcamera actually sees a sensor, and the app
    # journal so a "no :80/:443, RTSP up" boot is diagnosable without a console.
    echo "--- camera target units ($(date -Is)) ---"
    systemctl --failed --no-pager 2>/dev/null
    for unit in prusa-camera.target rpicam-source.service prusa-rtsp.service \
                prusa-ha-rtsp.service prusa-cam.service prusa-admin.service; do
        echo "$unit: active=$(systemctl is-active "$unit" 2>/dev/null) enabled=$(systemctl is-enabled "$unit" 2>/dev/null)"
    done
    echo "--- camera detection ---"
    vcgencmd get_camera 2>/dev/null
    { rpicam-still --list-cameras 2>&1 || libcamera-hello --list-cameras 2>&1; } | head -25
    dmesg 2>/dev/null | grep -iE "camera|ov5647|ov5640|imx|unicam|bcm2835-isp|brcm" | tail -25
    echo "--- journal (camera target) ---"
    journalctl -b --no-pager -n 250 -u prusa-camera.target -u rpicam-source \
        -u prusa-cam -u prusa-admin -u prusa-rtsp -u prusa-ha-rtsp 2>/dev/null
    echo
} >> "$LOG" 2>&1 || true

# Keep the small vfat boot partition bounded.
if [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 2000 ]; then
    tail -n 1000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG" 2>/dev/null
fi
