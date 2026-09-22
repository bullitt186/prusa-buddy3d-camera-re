"""One-shot restore of durable settings + timelapse store (GAP-PERSIST-01).

Runs as root from ``pi-persist.service`` before the camera/RTSP units. It:

1. bails out (exit 0) unless ``/data`` is a real mountpoint, so it is harmless
   before the offline repartition creates ``mmcblk0p3``;
2. creates the durable directories and bind-mounts ``/data/sdcard`` onto the
   firmware path ``/mnt/sdcard`` (SMB keeps sharing ``/mnt/sdcard`` unchanged),
   and ``/data/network/system-connections`` onto
   ``/etc/NetworkManager/system-connections`` so the station profile created at
   claim survives the read-only-root reboot (B4);
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
DATA_CONFIG_DIR = DATA_PRUSA_CAM + '/config'
DATA_RELEASES_DIR = DATA_PRUSA_CAM + '/releases'
DATA_BACKUPS_DIR = DATA_PRUSA_CAM + '/backups'
DATA_NETWORK_DIR = '/data/network'
DATA_NETWORK_CONNECTIONS = DATA_NETWORK_DIR + '/system-connections'
SD_MOUNT = '/mnt/sdcard'
#: NetworkManager keyfile store. Bind-mounted from DATA_NETWORK_CONNECTIONS so
#: the station profile created at claim survives the read-only-root reboot (B4).
NM_CONNECTIONS = '/etc/NetworkManager/system-connections'
TIMELAPSE_DIR = DATA_SDCARD + '/timelapse'
FRAME_SUFFIX = '.jpg'

# Free-space floor below which stored frames are pruned (300 MB).
PRUNE_FREE_THRESHOLD_BYTES = 300 * 1024 * 1024

# Dedicated non-login service account that owns every durable directory. The
# account is created by deploy.sh/bootstrap.sh and is the single identity in the
# systemd units; override only for a non-standard install via SERVICE_USER.
DEFAULT_SERVICE_USER = 'prusa-cam'

# Durable directory layout on the PERSIST partition, in creation order, with the
# mode each directory must end up with. ``config``/``backups`` hold secrets and
# migration backups, so they are group-accessible but not world-readable; the
# media directories stay 0755 so the bind-mounted SMB share can traverse them.
DATA_LAYOUT = (
    (DATA_SDCARD, 0o755),
    (TIMELAPSE_DIR, 0o755),
    (DATA_PRUSA_CAM, 0o750),
    (DATA_CONFIG_DIR, 0o750),
    (DATA_RELEASES_DIR, 0o750),
    (DATA_BACKUPS_DIR, 0o750),
    (DATA_NETWORK_DIR, 0o700),
    (DATA_NETWORK_CONNECTIONS, 0o700),
)

#: Layout entries that must stay root-owned. The NetworkManager keyfile store is
#: bind-mounted onto ``/etc/NetworkManager/system-connections`` (see
#: :data:`NM_CONNECTIONS`), which root consumes and inotify-reloads; giving the
#: unprivileged service account write access there would both weaken the
#: privilege boundary and let NM reject the profiles. These entries are created
#: (as root, since ``pi-persist.service`` runs as root) but never chowned to the
#: service user.
ROOT_ONLY_DIRS = frozenset({DATA_NETWORK_DIR, DATA_NETWORK_CONNECTIONS})


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


def ensure_durable_layout(service_user):
    """Create, mode, and chown the durable ``/data`` layout.

    Idempotent and best-effort: a directory that cannot be created is logged and
    skipped so one bad path never blocks the settings restore. Returns the list
    of directories that exist (and were chowned) afterwards. Callers must check
    :func:`settings_store.available` first; this helper does not verify that
    ``/data`` is a real mountpoint.
    """
    created = []
    for directory, mode in DATA_LAYOUT:
        try:
            os.makedirs(directory, exist_ok=True)
            os.chmod(directory, mode)
        except OSError as e:
            log.warning(f'persist: could not create {directory}: {e}')
            continue
        # The NetworkManager keyfile store stays root-owned (see ROOT_ONLY_DIRS);
        # pi-persist.service runs as root, so a freshly created directory is
        # already owned correctly and must not be handed to the service account.
        if directory not in ROOT_ONLY_DIRS:
            _chown(directory, service_user)
        created.append(directory)
    return created


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
    # The service user (not root) must write state.json, configuration, releases,
    # backups, and frames, so hand over every durable directory the app touches.
    ensure_durable_layout(service_user)

    _bind_mount(DATA_SDCARD, SD_MOUNT)
    # B4: the station profile lives under /etc, which is volatile on the
    # read-only-root appliance. Bind the durable copy in before NetworkManager
    # starts (pi-persist runs Before=data-ready.target; NM is After it), so the
    # profile created at claim survives reboot.
    _bind_mount(DATA_NETWORK_CONNECTIONS, NM_CONNECTIONS)
    _restore_settings()
    _prune_timelapse()
    return 0


if __name__ == '__main__':
    sys.exit(main())
