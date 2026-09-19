"""Shared runtime state for the Pi camera impersonator.

Pure stdlib: no file, network, hardware, or thread side effects on import. Every
command handler and every outbound surface (status, ``/c/info``, encoder
dimensions, snapshot cadence) reads and mutates one ``CameraState`` so the wire
values stay consistent (GAP-QUALITY-03, GAP-STATUS-01, GAP-CONTROL-01,
GAP-SNAPSHOT-01/02).

Quality representations recovered from firmware 3.1.6
(``FW-QUALITY-PB`` / ``FW-QUALITY-DIMS``):

    protobuf enum : 1=SD, 2=HD, 3=FHD
    raw event byte: 5=SD, 6=HD, 7=FHD
    resolution    : 640x480, 1280x720, 1920x1080
"""
import asyncio

import timelapse

# Exact firmware raw-event-byte -> protobuf-enum mapping (GAP-QUALITY-01).
RAW_TO_ENUM = {5: 1, 6: 2, 7: 3}
ENUM_TO_RAW = {1: 5, 2: 6, 3: 7}
RESOLUTIONS = {1: (640, 480), 2: (1280, 720), 3: (1920, 1080)}
DEFAULT_QUALITY = 3
SNAPSHOT_INTERVAL_MIN = 10
SNAPSHOT_INTERVAL_MAX = 600


def snapshot_interval_from_config(raw):
    """Parse a config ``upload.interval`` value into a valid interval or None.

    Review fix 3: config values must be integers in the inclusive 10..600 range;
    anything else falls back to the default with a warning at the call site.
    """
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return None
    if not SNAPSHOT_INTERVAL_MIN <= seconds <= SNAPSHOT_INTERVAL_MAX:
        return None
    return seconds


class CameraState:
    """One mutable runtime state object shared by handlers and status encoders."""

    def __init__(self, camera_name='Buddy3D Camera', quality=DEFAULT_QUALITY,
                 snapshot_interval=SNAPSHOT_INTERVAL_MIN,
                 snapshot_upload_enabled=True):
        self.camera_name = camera_name
        self.quality = quality
        self.snapshot_interval = snapshot_interval
        self.snapshot_upload_enabled = snapshot_upload_enabled
        # GAP-RTSP-02 (configured mode vs actual service state) is still open, so
        # these defaults preserve the bytes previously hardcoded in
        # signaling._status_message. Wiring the real systemd state is a separate gap.
        self.rtsp_mode = 1          # 1=disabled / 2=enabled (FW-CONFIG rtsp on/off)
        self.rtsp_running = False   # runtime service state
        self.webrtc_mode = 1        # 0=disabled / 1=enabled
        self.webrtc_status = 1      # 0=stopped / 1=running
        self.streaming = False      # true while a WebRTC peer is active
        self.info_dirty = True
        # GAP-STATUS-04: detected timezone (the /etc/TZ content) reported in
        # status; empty until the web API detection runs at startup.
        self.tz_name = ''
        # GAP-TIMELAPSE-01: Pi storage-backed timelapse state.
        self.timelapse_enabled = False
        self.timelapse_interval = 10
        self.timelapse_fps = 10
        # GAP-DEVICE-02: explicit hardware availability. The Pi has no IR
        # illuminator, speaker, fan, or MicroSD slot, so no control path may
        # imply otherwise or report a fake applied mode. ``ir_mode`` stays None
        # (no mode applied) while ``ir_available`` is False.
        self.ir_available = False
        self.speaker_available = False
        self.fan_available = False
        self.microsd_available = False
        self.ir_mode = None
        # GAP-DEVICE-01: monotonic time of the last accepted reboot request,
        # kept on the shared state so the rate-limit guard survives triggers.
        self.last_reboot_monotonic = None
        # Woken by set_snapshot_interval so snapshot_loop can re-read the cadence
        # without a process restart (GAP-SNAPSHOT-01).
        self.snapshot_interval_changed = asyncio.Event()

    def resolution(self):
        """Return the (width, height) for the current protobuf quality enum."""
        return RESOLUTIONS.get(self.quality, RESOLUTIONS[DEFAULT_QUALITY])

    def set_quality(self, quality):
        """Set the protobuf enum (1..3). Returns True only for a valid tier."""
        if type(quality) is not int or quality not in RESOLUTIONS:
            return False
        self.quality = quality
        return True

    def set_snapshot_interval(self, seconds):
        """Set the periodic cadence; accepts only int seconds in 10..600 inclusive."""
        if type(seconds) is not int:
            return False
        if not SNAPSHOT_INTERVAL_MIN <= seconds <= SNAPSHOT_INTERVAL_MAX:
            return False
        self.snapshot_interval = seconds
        self.snapshot_interval_changed.set()
        # GAP-INFO-01: a cadence change is a published attribute, so the
        # /c/info service loop must republish it.
        self.mark_info_dirty()
        return True

    def set_timelapse_interval(self, seconds):
        """Set the timelapse capture cadence (GAP-CONFIG-01).

        The Socket.IO ``configuration`` top-level field 2 is the firmware's
        ``set_timelaps_interval`` (recovered from dispatcher ``FUN_000a7940``,
        which logs ``"Timelapse interval: %d seconds"``). Accepts int seconds in
        the timelapse module's 1..3600 range; anything else is rejected and the
        previous value kept.
        """
        if type(seconds) is not int:
            return False
        if timelapse.valid_interval(seconds) is None:
            return False
        self.timelapse_interval = seconds
        return True

    def set_camera_name(self, name):
        """Set a non-empty stripped camera name. Returns False for invalid input."""
        if not isinstance(name, str):
            return False
        stripped = name.strip()
        if not stripped:
            return False
        self.camera_name = stripped
        return True

    def mark_info_dirty(self):
        self.info_dirty = True

    def periodic_snapshot_allowed(self, rtsp_active=False):
        """GAP-SNAPSHOT-02 loop predicate for periodic uploads.

        Disabling upload or an active stream (RTSP/WebRTC) pauses the periodic
        loop; immediate get-snapshot requests do not consult this predicate.
        """
        return (self.snapshot_upload_enabled
                and not self.streaming
                and not rtsp_active)
