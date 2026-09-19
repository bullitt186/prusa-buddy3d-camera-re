"""Pi storage-backed timelapse (GAP-TIMELAPSE-01).

Firmware 3.1.6 controls timelapse enable/interval/FPS, stores frames, creates
MJPEG output, indexes files and emits progress/error ``client_trigger`` messages.
The owner decision (2026-09-19) is a Pi storage-backed equivalent rooted at
``/var/lib/prusa-cam/timelapse``.

Stdlib-only and side-effect free on import so the logic is host-testable.
"""
import os
import time

SD_MOUNT = '/mnt/sdcard'   # emulated SD, the firmware's storage path
TIMELAPSE_DIR = '/mnt/sdcard/timelapse'   # emulated SD (see the SMB share)
DEFAULT_INTERVAL = 10   # seconds between frames
DEFAULT_FPS = 10        # playback rate of the assembled MJPEG
INTERVAL_MIN, INTERVAL_MAX = 1, 3600
FPS_MIN, FPS_MAX = 1, 30


def sd_present(path=SD_MOUNT):
    """True when the emulated SD is usable.

    Pi policy for the firmware's ``isDevicePresent && canAccessMountPoint &&
    /proc/mounts`` check: the Pi has no block device, so ``/mnt/sdcard`` is a
    real directory provisioned by ``deploy.sh`` and usability is a read+write
    check. Returns False on ``OSError``.
    """
    try:
        return os.path.isdir(path) and os.access(path, os.R_OK | os.W_OK)
    except OSError:
        return False


def sd_space(path=SD_MOUNT):
    """Return ``(total_mb, free_mb, used_mb)`` from ``statvfs64``.

    Matches ``FUN_000745e0``: ``(f_bsize * f_blocks) >> 20`` (total),
    ``(f_bsize * f_bfree) >> 20`` (free), and
    ``(f_bsize * (f_blocks - f_bfree)) >> 20`` (used). Returns ``(0, 0, 0)``
    on ``OSError``.
    """
    try:
        st = os.statvfs(path)
    except OSError:
        return (0, 0, 0)
    total = (st.f_bsize * st.f_blocks) >> 20
    free = (st.f_bsize * st.f_bfree) >> 20
    used = (st.f_bsize * (st.f_blocks - st.f_bfree)) >> 20
    return (total, free, used)


def sd_mode(path=SD_MOUNT):
    """SD mount-mode string (``FUN_00073914``): ``RW``/``RO``/``UNKNOWN``."""
    if not sd_present(path):
        return 'UNKNOWN'
    return 'RW' if os.access(path, os.W_OK) else 'RO'


def storage_status(path=SD_MOUNT):
    """5-tuple shaped for ``extended_status.4`` (descriptor ``0x3f72b0``).

    ``(present, total_mb, free_mb, used_mb, mode)``; absent storage reports the
    firmware mounted-state ``2`` (1 = mounted) with zero space and
    ``'UNKNOWN'`` mode.
    """
    if not sd_present(path):
        return (2, 0, 0, 0, 'UNKNOWN')
    total, free, used = sd_space(path)
    return (1, total, free, used, sd_mode(path))


def valid_interval(value):
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if INTERVAL_MIN <= seconds <= INTERVAL_MAX else None


def valid_fps(value):
    try:
        fps = int(value)
    except (TypeError, ValueError):
        return None
    return fps if FPS_MIN <= fps <= FPS_MAX else None


def frame_name(index):
    return f'frame_{int(index):05d}.jpg'


def frame_path(directory, index):
    return os.path.join(directory, frame_name(index))


def save_frame(data, directory, index):
    """Persist one JPEG frame; returns the path."""
    os.makedirs(directory, exist_ok=True)
    path = frame_path(directory, index)
    with open(path, 'wb') as f:
        f.write(data)
    return path


def list_frames(directory):
    """Return the stored frame filenames in capture order."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    frames = [n for n in names if n.startswith('frame_') and n.endswith('.jpg')]
    return sorted(frames)


def next_index(directory):
    return len(list_frames(directory)) + 1


def build_mjpeg(directory, output_path=None):
    """Concatenate the stored JPEG frames into an MJPEG file.

    Returns the output path, or ``None`` when there are no frames. The MJPEG
    stream format is simply concatenated JPEG images, which is what the firmware
    produces for timelapse playback.
    """
    frames = list_frames(directory)
    if not frames:
        return None
    if output_path is None:
        output_path = os.path.join(directory, video_name())
    with open(output_path, 'wb') as out:
        for name in frames:
            with open(os.path.join(directory, name), 'rb') as f:
                out.write(f.read())
    return output_path


def video_name(when=None):
    return time.strftime('timelapse_%Y%m%d_%H%M%S.mjpeg', time.localtime(when or time.time()))


def apply_enable(action, state):
    """Set ``state.timelapse_enabled`` from a trigger action name."""
    if action == 'timelapse_enable':
        state.timelapse_enabled = True
        return True
    if action == 'timelapse_disable':
        state.timelapse_enabled = False
        return True
    return False
