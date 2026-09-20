"""Durable runtime-settings store on the ``/data`` partition (GAP-PERSIST-01).

The Pi root is a read-only overlayfs (``overlayroot=tmpfs``), so ``/etc/prusa-cam``
and ``/mnt/sdcard`` are volatile. A separate ext4 partition (label ``PERSIST``,
``mmcblk0p3``) is mounted at ``/data`` to hold state that must survive a reboot:
``state.json`` here, and the bind-mounted timelapse store (see
``persist_restore.py``).

**No-op rule:** while ``/data`` is not a real mountpoint, every operation here is
inert — ``available()`` is False, ``save()`` writes nothing and returns False, and
``load()`` returns ``{}`` for the missing path. This lets the code be deployed
before the offline repartition creates ``mmcblk0p3``.

Persisted keys (``state.json``, all optional; unknown keys are preserved and
ignored by readers):

    version                  int   store schema version (always written)
    quality_tier             int   1=SD, 2=HD, 3=FHD (protobuf enum)
    camera_name              str   non-empty stripped camera name
    snapshot_interval        int   10..600 seconds
    snapshot_upload_enabled  bool  periodic snapshot uploader on/off
    timelapse_interval       int   1..3600 seconds
    timelapse_enabled        bool  timelapse capture on/off
    timelapse_fps            int   1..30 playback FPS for the assembled AVI
    rtsp_mode                int   1=disabled, 2=enabled (configured mode)
    webrtc_mode              int   0=disabled, 1=enabled

Stdlib only, and no file side effects on import.
"""
import json
import logging
import os

log = logging.getLogger('prusa-cam.settings')

SETTINGS_DIR = '/data/prusa-cam'
SETTINGS_PATH = SETTINGS_DIR + '/state.json'
VERSION = 1

# ``/data`` mountpoint, kept as a module constant so tests can patch the two
# ``os.path`` probes that define availability.
DATA_MOUNT = '/data'


def available():
    """True only when ``/data`` exists and is a real mountpoint.

    ``isdir`` alone would also match a plain directory created on the overlay
    root; ``ismount`` is what distinguishes the durable partition.
    """
    try:
        return os.path.isdir(DATA_MOUNT) and os.path.ismount(DATA_MOUNT)
    except OSError:
        return False


def load(path=SETTINGS_PATH):
    """Return the persisted settings dict, or ``{}`` when absent/corrupt.

    A file that is missing, unreadable, or not valid JSON is treated as absent.
    A corrupt file is renamed to ``<path>.bad`` (best-effort) so it is preserved
    for inspection without blocking startup. Unknown keys are tolerated.
    """
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning(f'settings: could not read {path}: {e}')
        _quarantine(path)
        return {}
    if not isinstance(data, dict):
        log.warning(f'settings: {path} does not contain a JSON object; ignoring')
        _quarantine(path)
        return {}
    return data


def save(data, path=SETTINGS_PATH):
    """Atomically persist ``data`` as JSON; returns True on success.

    No-op returning False when :func:`available` is False. On any ``OSError`` the
    temp file is removed, a warning is logged, and False is returned — callers
    must never have to handle an exception from persistence.
    """
    if not available():
        return False
    payload = dict(data)
    payload['version'] = VERSION
    directory = os.path.dirname(path) or '.'
    tmp = path + '.tmp'
    try:
        os.makedirs(directory, exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(directory)
    except OSError as e:
        log.warning(f'settings: could not persist to {path}: {e}')
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    return True


def _quarantine(path):
    """Best-effort rename of an unreadable file to ``<path>.bad``."""
    try:
        os.replace(path, path + '.bad')
    except OSError as e:
        log.warning(f'settings: could not quarantine {path}: {e}')


def _fsync_dir(directory):
    """Best-effort fsync of ``directory`` so a rename survives a power cut.

    The Pi power-cycles without a clean shutdown (CLAUDE.md), so the directory
    entry created by ``os.replace`` must be flushed too, not just the file data.
    """
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
