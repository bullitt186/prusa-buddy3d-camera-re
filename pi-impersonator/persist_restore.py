"""One-shot restore of durable settings + timelapse store (GAP-PERSIST-01).

Runs as root from ``pi-persist.service`` before the camera/RTSP units. It:

1. bails out (exit 0) unless ``/data`` is a real mountpoint, so it is harmless
   before the offline repartition creates ``mmcblk0p3``;
2. creates the durable directories and bind-mounts ``/data/sdcard`` onto the
   firmware path ``/mnt/sdcard`` (SMB keeps sharing ``/mnt/sdcard`` unchanged);
3. restores ``quality.env`` and ``rtsp.mode`` from ``state.json``;
4. prunes the oldest timelapse JPEG frames when ``/data`` free space is low
   (``.avi`` and the CSV index are never deleted).

The top level is side-effect free: importing this module must not touch the
filesystem, so the work lives in :func:`main` behind the ``__main__`` guard.
Stdlib only.
"""
import logging
import os
import shutil
import stat
import subprocess
import sys

import quality
import rtsp_control
import settings_store

log = logging.getLogger('prusa-cam.persist')

DATA_MOUNT = '/data'
DATA_SDCARD = '/data/sdcard'
DATA_PRUSA_CAM = '/data/prusa-cam'
SD_MOUNT = '/mnt/sdcard'
TIMELAPSE_DIR = DATA_SDCARD + '/timelapse'
FRAME_SUFFIX = '.jpg'

# Free-space floor below which stored frames are pruned (300 MB).
PRUNE_FREE_THRESHOLD_BYTES = 300 * 1024 * 1024
DEFAULT_SERVICE_USER = 'bullitt'


def quality_env_values(tier):
    """Return the ``(width, height)`` a persisted quality tier maps to.

    Pure mirror of ``quality.write_current``'s resolution table so the restore
    decision is testable without writing ``/etc/prusa-cam``.
    """
    resolutions = quality.RESOLUTIONS
    return resolutions.get(tier, resolutions[quality.DEFAULT_QUALITY])


def frames_to_prune(frames, total_bytes, limit_bytes):
    """Select the oldest frames to delete to bring stored bytes under a limit.

    ``frames`` is an iterable of ``(name, size, mtime)`` for prunable JPEG
    frames. Selection is oldest-first (ascending ``mtime``, filename as a
    tie-break). Returns ``[(name, size), ...]``; empty when ``total_bytes`` is
    already at or below ``limit_bytes``. Pure: performs no I/O.
    """
    if total_bytes <= limit_bytes:
        return []
    ordered = sorted(frames, key=lambda frame: (frame[2], frame[0]))
    freed = 0
    selected = []
    for name, size, _mtime in ordered:
        if total_bytes - freed <= limit_bytes:
            break
        selected.append((name, size))
        freed += size
    return selected


def _chown(path, user):
    """Best-effort ``chown`` of ``path`` to ``user`` (never raises)."""
    try:
        shutil.chown(path, user=user)
    except (LookupError, OSError, KeyError) as e:
        log.warning(f'persist: could not chown {path} to {user}: {e}')


def _bind_mount(source, target):
    """Bind-mount ``source`` onto ``target`` unless already mounted there."""
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        log.warning(f'persist: could not create {target}: {e}')
        return False
    if os.path.ismount(target):
        log.info(f'persist: {target} already mounted; leaving as-is')
        return True
    try:
        result = subprocess.run(
            ['mount', '--bind', source, target], capture_output=True, text=True
        )
    except OSError as e:
        log.warning(f'persist: bind mount {source} -> {target} failed: {e}')
        return False
    if result.returncode != 0:
        log.warning(
            f'persist: bind mount {source} -> {target} failed '
            f'(rc={result.returncode}): {result.stderr.strip()}'
        )
        return False
    log.info(f'persist: bind-mounted {source} -> {target}')
    return True


def _restore_settings():
    """Materialize quality.env and rtsp.mode from the persisted state.json."""
    data = settings_store.load()
    if not data:
        log.info('persist: no persisted settings to restore')
        return
    tier = data.get('quality_tier')
    if type(tier) is int and tier in quality.RESOLUTIONS:
        try:
            quality.write_current(tier)
            log.info(f'persist: restored quality tier {tier} to {quality.QUALITY_ENV}')
        except OSError as e:
            log.warning(f'persist: could not restore quality tier {tier}: {e}')
    mode = data.get('rtsp_mode')
    if mode in (rtsp_control.RTSP_DISABLED, rtsp_control.RTSP_ENABLED):
        if rtsp_control.write_mode(mode):
            log.info(f'persist: restored rtsp mode {mode} to {rtsp_control.RTSP_MODE_FILE}')
        else:
            log.warning(f'persist: could not restore rtsp mode {mode}')


def _frame_inventory(directory):
    """Return ``(name, size, mtime)`` for regular ``.jpg`` frames in ``directory``."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    frames = []
    for name in names:
        if not name.endswith(FRAME_SUFFIX):
            continue
        path = os.path.join(directory, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        frames.append((name, st.st_size, st.st_mtime))
    return frames


def _prune_timelapse(directory=TIMELAPSE_DIR, mount=DATA_MOUNT):
    """Delete oldest JPEG frames when free space is below the threshold.

    ``.avi`` and the ``.timelapse_videos.csv`` index are never touched.
    """
    try:
        free = shutil.disk_usage(mount).free
    except OSError as e:
        log.warning(f'persist: could not stat free space on {mount}: {e}')
        return
    if free >= PRUNE_FREE_THRESHOLD_BYTES:
        return
    frames = _frame_inventory(directory)
    if not frames:
        return
    total_bytes = sum(size for _name, size, _mtime in frames)
    # Bytes that must be freed so free space reaches the threshold.
    need = PRUNE_FREE_THRESHOLD_BYTES - free
    limit = max(0, total_bytes - need)
    selected = frames_to_prune(frames, total_bytes, limit)
    removed = 0
    for name, _size in selected:
        try:
            os.remove(os.path.join(directory, name))
            removed += 1
        except OSError as e:
            log.warning(f'persist: could not prune {name}: {e}')
    log.info(
        f'persist: free {free // (1024 * 1024)} MB < '
        f'{PRUNE_FREE_THRESHOLD_BYTES // (1024 * 1024)} MB; pruned {removed} frame(s)'
    )


def main():
    """Restore durable state; returns a process exit code (always 0)."""
    if not settings_store.available():
        log.warning('persist: /data is not a mountpoint; nothing to restore')
        return 0

    service_user = os.environ.get('SERVICE_USER', DEFAULT_SERVICE_USER)
    for directory in (DATA_SDCARD, DATA_PRUSA_CAM, TIMELAPSE_DIR):
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as e:
            log.warning(f'persist: could not create {directory}: {e}')
    # The service user (not root) must write frames into timelapse/ and
    # state.json into prusa-cam/, so hand over every directory the app touches.
    for directory in (DATA_SDCARD, DATA_PRUSA_CAM, TIMELAPSE_DIR):
        _chown(directory, service_user)

    _bind_mount(DATA_SDCARD, SD_MOUNT)
    _restore_settings()
    _prune_timelapse()
    return 0


if __name__ == '__main__':
    sys.exit(main())
