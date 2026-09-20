"""Readiness gate for the durable ``/data`` (PERSIST) partition (AC-7).

Runs as root from ``prusa-data-ready.service`` before any camera or application
unit. It must **never** create ``/data`` itself: a look-alike directory on the
volatile overlay root would let the app "succeed" while every write is discarded
on the next reboot. If the real partition is absent, unlabelled, read-only, or
not writable, the gate fails and the units that Require ``data-ready.target`` do
not start.

Checks, in order:

1. ``mount`` exists and is a directory;
2. ``mount`` is a real mountpoint (``os.path.ismount``);
3. the ``PERSIST`` by-label device resolves to the same block device as the mount;
4. the mounted filesystem is not read-only (``statvfs.f_flag & os.ST_RDONLY``);
5. a temp file can be created, fsynced, and removed inside the mount.

The device comparison uses the mounted filesystem's ``st_dev`` against the label
node's ``st_rdev``: on Linux a block-device node's ``st_dev`` is devtmpfs and its
``st_rdev`` is the device number the mount reports as ``st_dev``, so comparing
``st_dev`` to ``st_dev`` would never match.

Stdlib only, no filesystem side effects on import.
"""
import os
import sys
import tempfile

DEFAULT_MOUNT = '/data'
DEFAULT_LABEL_PATH = '/dev/disk/by-label/PERSIST'
WRITE_PROBE_PREFIX = '.prusa-cam-data-ready-'


def _mount_device(mount):
    """Device number of the filesystem mounted at ``mount``."""
    return os.stat(mount).st_dev


def _label_device(label_path):
    """Block-device number the ``PERSIST`` label resolves to."""
    return os.stat(label_path).st_rdev


def _is_read_only(mount):
    """True when ``mount`` is mounted read-only."""
    return bool(os.statvfs(mount).f_flag & os.ST_RDONLY)


def _write_probe(mount):
    """Create, fsync, and remove a temp file inside ``mount``.

    Returns ``(ok, reason)``. Only called after the mountpoint check, so it can
    never create a look-alike ``/data``.
    """
    try:
        fd, path = tempfile.mkstemp(prefix=WRITE_PROBE_PREFIX, dir=mount)
    except OSError as e:
        return False, f'{mount} is not writable: {e}'
    try:
        try:
            os.write(fd, b'prusa-cam data-ready probe\n')
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        _remove_quietly(path)
        return False, f'{mount} write probe failed: {e}'
    _remove_quietly(path)
    return True, f'{mount} is mounted, labelled, writable, and not read-only'


def _remove_quietly(path):
    """Best-effort removal of a probe file (never raises)."""
    try:
        os.remove(path)
    except OSError:
        pass


def check(mount=DEFAULT_MOUNT, label_path=DEFAULT_LABEL_PATH):
    """Return ``(ok, reason)`` for whether ``mount`` is a usable PERSIST.

    Both paths are injectable so tests never touch the real ``/data``. ``ok`` is
    True only when ``mount`` is a directory, a real mountpoint, the same device
    as the ``PERSIST`` label, not read-only, and writable.
    """
    try:
        if not os.path.isdir(mount):
            return False, f'{mount} is not a directory'
        if not os.path.ismount(mount):
            return False, f'{mount} is not a mountpoint'
    except OSError as e:
        return False, f'{mount} cannot be inspected: {e}'

    # The label is mandatory: an unlabelled filesystem mounted at /data must not
    # be accepted even though it looks like durable storage.
    if not os.path.exists(label_path):
        return False, f'{label_path} is missing; the PERSIST partition must be labelled'
    try:
        if _mount_device(mount) != _label_device(label_path):
            return False, f'{mount} is not the PERSIST partition'
    except OSError as e:
        return False, f'{label_path} cannot be resolved: {e}'

    try:
        if _is_read_only(mount):
            return False, f'{mount} is mounted read-only'
    except OSError as e:
        return False, f'{mount} cannot be checked for a read-only mount: {e}'

    return _write_probe(mount)


def main(argv=None):
    """CLI: print the readiness reason; return 0 when ready, 1 otherwise."""
    args = list(sys.argv[1:] if argv is None else argv)
    mount = args[0] if len(args) > 0 else DEFAULT_MOUNT
    label_path = args[1] if len(args) > 1 else DEFAULT_LABEL_PATH
    ok, reason = check(mount, label_path)
    print(reason)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
