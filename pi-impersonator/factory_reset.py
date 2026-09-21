"""Destructive factory reset for the public appliance (WP-3d2, AC-20).

Source plan §4.5 (``docs/public-appliance-distribution-plan.md``)::

    Factory reset is separate and destructive. Require a second explicit
    confirmation/sentinel, first create a dated backup on DATA, and delete that
    backup only after the reset system has completed a successful claimed boot.
    Report exactly what is being deleted.

This module owns the durable-data half of that requirement. It deletes only the
durable DATA content that is reset (the appliance configuration, the persisted
state, the provisioning state machine record, and the timelapse store), leaves a
dated backup behind, and records a ``pending-deletion`` marker so the backup is
removed only once a boot has successfully reached the ``claimed`` state.

Durable layout (``data_root`` defaults to ``/data``)::

    <data_root>/prusa-cam/config/device.toml
    <data_root>/prusa-cam/config/secrets.toml
    <data_root>/prusa-cam/state.json
    <data_root>/prusa-cam/provisioning.json
    <data_root>/prusa-cam/backups/factory-reset-<YYYYMMDD-HHMMSS>/
    <data_root>/prusa-cam/pending-deletion.json
    <data_root>/sdcard/timelapse/

Two-step confirmation
---------------------
The UI requires an explicit second confirmation before a factory reset runs.
This module models that as two calls sharing one reset token:

1. :meth:`FactoryReset.begin` — the first confirmation. Returns a reset token.
2. :meth:`FactoryReset.confirm` — the second, explicit confirmation. Accepts the
   token and arms the reset.
3. :meth:`FactoryReset.execute` — performs the reset. It refuses unless both
   steps completed **for the same token**; the token must be passed again so the
   destructive call cannot be triggered by a stale confirmation.

Re-authentication gate
----------------------
A factory reset is one of the actions gated behind a fresh admin password check
(source plan §4.5). Callers **must** verify
``admin_auth.requires_reauth('factory_reset')`` and complete the re-auth before
calling :meth:`execute`. That check is deliberately *not* implemented here so
this module stays a pure data operation; see :data:`admin_auth.REAUTH_ACTIONS`.

Safety
------
* The configured data root must be an absolute path inside the trusted durable
  root (``/data`` by default) and must have the expected durable structure.
  Otherwise every operation refuses with a clear, non-secret reason and makes
  **no** changes.
* Nothing outside the data root is ever deleted. Symlinks are never followed out
  of the root: a symlink is unlinked itself, never its target.
* ``secrets.toml`` bytes are copied but never parsed, logged, or included in a
  report/reason. Reports contain only paths, counts, and timestamps.

Stdlib only, no hardware, no root, and no file/network side effects on import.
"""
import dataclasses
import json
import logging
import os
import secrets
import shutil
import stat
from datetime import datetime, timezone

log = logging.getLogger('prusa-cam.factory-reset')

# --------------------------------------------------------------------------- #
# Durable layout
# --------------------------------------------------------------------------- #

#: Trusted parent the data root must live under. Injectable for tests, but the
#: production default is the real PERSIST mount.
DEFAULT_DURABLE_ROOT = '/data'
DEFAULT_DATA_ROOT = '/data'

PRUSA_CAM_DIRNAME = 'prusa-cam'
CONFIG_DIRNAME = 'config'
BACKUPS_DIRNAME = 'backups'
SDCARD_DIRNAME = 'sdcard'
TIMELAPSE_DIRNAME = 'timelapse'

DEVICE_TOML_NAME = 'device.toml'
SECRETS_TOML_NAME = 'secrets.toml'
STATE_JSON_NAME = 'state.json'
PROVISIONING_JSON_NAME = 'provisioning.json'
PENDING_DELETION_NAME = 'pending-deletion.json'

BACKUP_PREFIX = 'factory-reset-'
TIMESTAMP_FORMAT = '%Y%m%d-%H%M%S'


def _utcnow():
    """Default clock: an aware UTC ``datetime``."""
    return datetime.now(timezone.utc)


def _join(base, *parts):
    """``os.path.join`` that tolerates a missing/invalid base (returns '')."""
    if not isinstance(base, str) or not base:
        return ''
    return os.path.join(base, *parts)


def _within(path, root):
    """True when ``path`` is ``root`` itself or lies underneath it.

    Both arguments are expected to be already normalized/real paths. This is a
    lexical containment check; callers resolve symlinks first.
    """
    if not path or not root:
        return False
    path = os.path.normpath(path)
    root = os.path.normpath(root)
    return path == root or path.startswith(root + os.sep)


class _ResetError(Exception):
    """Internal failure that aborts a reset without leaking detail."""


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #

@dataclasses.dataclass(frozen=True)
class FactoryResetReport:
    """Outcome of :meth:`FactoryReset.execute`.

    ``deleted`` is the exact list of removed paths (files, symlinks, and emptied
    directories); ``file_count``/``byte_count`` count the removed files/symlinks
    and their sizes. No file contents (and therefore no secrets) are included.
    """

    ok: bool
    reason: str = ''
    backup_path: str = ''
    deleted: tuple = ()
    deleted_dirs: tuple = ()
    file_count: int = 0
    byte_count: int = 0
    timestamp: str = ''
    pending_deletion_path: str = ''

    def to_dict(self):
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class FinalizeReport:
    """Outcome of :meth:`FactoryReset.finalize_after_claimed_boot`."""

    ok: bool
    deleted: bool = False
    reason: str = ''
    backup_path: str = ''
    timestamp: str = ''

    def to_dict(self):
        return dataclasses.asdict(self)


# --------------------------------------------------------------------------- #
# Factory reset
# --------------------------------------------------------------------------- #

class FactoryReset:
    """Two-step, backed-up, safety-checked factory reset of durable DATA.

    All paths and the clock are injectable so tests never touch the real
    ``/data``. The production defaults mirror :mod:`persist_restore`.
    """

    def __init__(
        self,
        data_root=DEFAULT_DATA_ROOT,
        durable_root=DEFAULT_DURABLE_ROOT,
        prusa_cam_dir=None,
        config_dir=None,
        backups_dir=None,
        timelapse_dir=None,
        clock=None,
        token_factory=None,
    ):
        self.data_root = data_root
        self.durable_root = durable_root
        self.prusa_cam_dir = prusa_cam_dir or _join(data_root, PRUSA_CAM_DIRNAME)
        self.config_dir = config_dir or _join(self.prusa_cam_dir, CONFIG_DIRNAME)
        self.backups_dir = backups_dir or _join(self.prusa_cam_dir, BACKUPS_DIRNAME)
        self.timelapse_dir = timelapse_dir or _join(
            data_root, SDCARD_DIRNAME, TIMELAPSE_DIRNAME
        )
        self.device_toml = _join(self.config_dir, DEVICE_TOML_NAME)
        self.secrets_toml = _join(self.config_dir, SECRETS_TOML_NAME)
        self.state_json = _join(self.prusa_cam_dir, STATE_JSON_NAME)
        self.provisioning_json = _join(self.prusa_cam_dir, PROVISIONING_JSON_NAME)
        self.pending_deletion_path = _join(self.prusa_cam_dir, PENDING_DELETION_NAME)

        self._clock = clock or _utcnow
        self._token_factory = token_factory or (lambda: secrets.token_hex(16))
        self._token = ''
        self._begun = False
        self._confirmed = False
        self._reason = ''
        self._last_report = None

    # ------------------------------------------------------------------ #
    # Two-step confirmation
    # ------------------------------------------------------------------ #

    def begin(self, reason=''):
        """First confirmation: mint and remember a reset token.

        Returns the token the caller must pass to :meth:`confirm` and later to
        :meth:`execute`. Performs no I/O.
        """
        self._token = self._token_factory()
        self._begun = True
        self._confirmed = False
        self._reason = reason if isinstance(reason, str) else ''
        return self._token

    def confirm(self, token):
        """Second, explicit confirmation: arm the reset for ``token``.

        Returns True only when :meth:`begin` was called and ``token`` matches.
        Performs no I/O.
        """
        if not self._begun or not isinstance(token, str) or not token:
            return False
        if token != self._token:
            return False
        self._confirmed = True
        return True

    # ------------------------------------------------------------------ #
    # Execute
    # ------------------------------------------------------------------ #

    def execute(self, token=None, include_timelapse=True):
        """Perform the factory reset and return a :class:`FactoryResetReport`.

        Refuses (and deletes nothing) unless :meth:`begin` and :meth:`confirm`
        both completed for ``token`` and the data root passes the safety check.
        On success: dated backup, delete the DATA content, write the
        ``pending-deletion`` marker, and report the exact deletions.
        """
        if not self._begun:
            return self._store(self._refuse(
                'factory reset requires two-step confirmation: begin() first'
            ))
        if not self._confirmed:
            return self._store(self._refuse(
                'factory reset requires the second confirmation: confirm(token) first'
            ))
        if not isinstance(token, str) or token != self._token:
            return self._store(self._refuse(
                'reset token does not match the confirmed token'
            ))

        # Consume the confirmation so the same token cannot be replayed.
        self._begun = False
        self._confirmed = False
        self._token = ''

        safety = self._safety_reason()
        if safety:
            return self._store(self._refuse(safety))

        timestamp = self._clock().strftime(TIMESTAMP_FORMAT)

        try:
            backup_path = self._create_backup(timestamp, include_timelapse)
        except _ResetError as exc:
            return self._store(self._refuse(str(exc), timestamp=timestamp))

        try:
            deleted, deleted_dirs, file_count, byte_count = self._delete_targets(
                include_timelapse
            )
        except _ResetError as exc:
            return self._store(FactoryResetReport(
                ok=False,
                reason=str(exc),
                backup_path=backup_path,
                timestamp=timestamp,
            ))

        marker_ok, marker_reason = self._write_pending_deletion(backup_path, timestamp)
        if not marker_ok:
            return self._store(FactoryResetReport(
                ok=False,
                reason=marker_reason,
                backup_path=backup_path,
                deleted=tuple(deleted),
                deleted_dirs=tuple(deleted_dirs),
                file_count=file_count,
                byte_count=byte_count,
                timestamp=timestamp,
            ))

        return self._store(FactoryResetReport(
            ok=True,
            reason='factory reset complete; backup retained until a claimed boot',
            backup_path=backup_path,
            deleted=tuple(deleted),
            deleted_dirs=tuple(deleted_dirs),
            file_count=file_count,
            byte_count=byte_count,
            timestamp=timestamp,
            pending_deletion_path=self.pending_deletion_path,
        ))

    # ------------------------------------------------------------------ #
    # Finalize
    # ------------------------------------------------------------------ #

    def finalize_after_claimed_boot(self, claimed=False):
        """Delete the backup only after a successful ``claimed`` boot.

        ``claimed`` must be True only when provisioning confirms the device
        reached the ``claimed`` state on a boot after the reset. When False the
        backup is kept. Idempotent: with no marker (or after a successful
        finalize) it is a no-op success.
        """
        safety = self._safety_reason()
        if safety:
            return FinalizeReport(ok=False, reason=safety)

        marker = self.pending_deletion()
        if marker is None:
            return FinalizeReport(ok=True, deleted=False, reason='no pending deletion')

        backup_path = marker.get('backup_path')
        timestamp = marker.get('timestamp', '')
        if not isinstance(backup_path, str) or not backup_path:
            return FinalizeReport(
                ok=False, reason='pending deletion marker is malformed', timestamp=timestamp
            )

        if os.path.islink(backup_path):
            return FinalizeReport(
                ok=False,
                reason='refusing to delete a backup that is a symbolic link',
                backup_path=backup_path,
                timestamp=timestamp,
            )
        real_backup = os.path.realpath(backup_path)
        real_root = os.path.realpath(self.data_root)
        real_backups = os.path.realpath(self.backups_dir)
        if not _within(real_backup, real_root) or not _within(real_backup, real_backups):
            return FinalizeReport(
                ok=False,
                reason='refusing to delete a backup outside the data root',
                backup_path=backup_path,
                timestamp=timestamp,
            )

        if not claimed:
            return FinalizeReport(
                ok=True,
                deleted=False,
                reason='backup retained until a successful claimed boot',
                backup_path=backup_path,
                timestamp=timestamp,
            )

        if os.path.exists(backup_path):
            try:
                shutil.rmtree(backup_path)
            except OSError as exc:
                return FinalizeReport(
                    ok=False,
                    reason=f'backup could not be deleted: {exc.strerror or "error"}',
                    backup_path=backup_path,
                    timestamp=timestamp,
                )
        self._remove_marker()
        return FinalizeReport(
            ok=True,
            deleted=True,
            reason='backup deleted after a successful claimed boot',
            backup_path=backup_path,
            timestamp=timestamp,
        )

    # ------------------------------------------------------------------ #
    # Accessors
    # ------------------------------------------------------------------ #

    def report(self):
        """Return the most recent :class:`FactoryResetReport` (or None)."""
        return self._last_report

    def pending_deletion(self):
        """Return the parsed ``pending-deletion`` marker, or None.

        Contains only the backup path and timestamps — never secrets.
        """
        if not isinstance(self.pending_deletion_path, str):
            return None
        if not os.path.isfile(self.pending_deletion_path):
            return None
        try:
            with open(self.pending_deletion_path, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------ #
    # Safety
    # ------------------------------------------------------------------ #

    def _safety_reason(self):
        """Return '' when the data root is safe to operate on, else a reason."""
        if not isinstance(self.data_root, str) or not self.data_root:
            return 'data root is not configured'
        if not os.path.isabs(self.data_root):
            return 'data root must be an absolute path'
        if not isinstance(self.durable_root, str) or not self.durable_root:
            return 'durable root is not configured'
        if not os.path.isabs(self.durable_root):
            return 'durable root must be an absolute path'
        try:
            real_root = os.path.realpath(self.data_root)
            real_durable = os.path.realpath(self.durable_root)
        except OSError:
            return 'data root cannot be resolved'
        if not _within(real_root, real_durable):
            return 'data root is outside the durable PERSIST root'
        if not os.path.isdir(self.data_root):
            return 'data root is not a directory'
        if not isinstance(self.prusa_cam_dir, str) or not os.path.isdir(self.prusa_cam_dir):
            return 'durable structure is missing'
        if not _within(os.path.realpath(self.prusa_cam_dir), real_root):
            return 'durable structure escapes the data root'
        return ''

    # ------------------------------------------------------------------ #
    # Backup
    # ------------------------------------------------------------------ #

    def _unique_backup_path(self, timestamp):
        base = _join(self.backups_dir, BACKUP_PREFIX + timestamp)
        candidate = base
        suffix = 1
        while os.path.exists(candidate):
            suffix += 1
            candidate = f'{base}-{suffix}'
        return candidate

    def _create_backup(self, timestamp, include_timelapse):
        backup_path = self._unique_backup_path(timestamp)
        backup_config = _join(backup_path, CONFIG_DIRNAME)
        try:
            os.makedirs(backup_config, exist_ok=True)
        except OSError as exc:
            self._cleanup_backup(backup_path)
            raise _ResetError(
                f'could not create the backup directory: {exc.strerror or "error"}'
            )
        try:
            for source, destination in (
                (self.device_toml, _join(backup_config, DEVICE_TOML_NAME)),
                (self.secrets_toml, _join(backup_config, SECRETS_TOML_NAME)),
                (self.state_json, _join(backup_path, STATE_JSON_NAME)),
                (self.provisioning_json, _join(backup_path, PROVISIONING_JSON_NAME)),
            ):
                self._copy_file_safely(source, destination)
            if include_timelapse:
                self._copy_tree_safely(
                    self.timelapse_dir, _join(backup_path, TIMELAPSE_DIRNAME)
                )
        except _ResetError:
            self._cleanup_backup(backup_path)
            raise
        return backup_path

    def _cleanup_backup(self, backup_path):
        """Best-effort removal of a partially written backup (never raises)."""
        try:
            shutil.rmtree(backup_path, ignore_errors=True)
        except OSError:
            pass

    def _copy_file_safely(self, source, destination):
        """Copy one regular file if it exists and is inside the data root.

        Symlinks are never followed (a symlink is skipped entirely), so a link
        inside the root cannot cause content from outside the root to be read.
        """
        try:
            info = os.lstat(source)
        except OSError:
            return  # missing is not an error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return
        try:
            real = os.path.realpath(source)
        except OSError:
            return
        if not _within(real, os.path.realpath(self.data_root)):
            return
        try:
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copyfile(source, destination)
        except OSError as exc:
            raise _ResetError(
                f'could not back up durable data: {exc.strerror or "error"}'
            )

    def _copy_tree_safely(self, source_dir, destination_dir):
        """Recursively copy regular files, never descending into symlinks."""
        if not isinstance(source_dir, str) or not os.path.isdir(source_dir):
            return
        if os.path.islink(source_dir):
            return
        real_root = os.path.realpath(self.data_root)
        try:
            for dirpath, dirnames, filenames in os.walk(source_dir, followlinks=False):
                if not _within(os.path.realpath(dirpath), real_root):
                    dirnames[:] = []
                    continue
                relative = os.path.relpath(dirpath, source_dir)
                target_dir = (
                    destination_dir if relative == '.'
                    else _join(destination_dir, relative)
                )
                os.makedirs(target_dir, exist_ok=True)
                dirnames[:] = [
                    name for name in dirnames
                    if not os.path.islink(os.path.join(dirpath, name))
                ]
                for name in filenames:
                    source = os.path.join(dirpath, name)
                    if os.path.islink(source):
                        continue
                    if not _within(os.path.realpath(source), real_root):
                        continue
                    shutil.copyfile(source, os.path.join(target_dir, name))
        except OSError as exc:
            raise _ResetError(
                f'could not back up the timelapse store: {exc.strerror or "error"}'
            )

    # ------------------------------------------------------------------ #
    # Deletion
    # ------------------------------------------------------------------ #

    def _delete_targets(self, include_timelapse):
        """Delete the DATA content and return ``(files, dirs, count, bytes)``."""
        files = list(self._iter_files(self.config_dir))
        for path in (self.state_json, self.provisioning_json):
            if isinstance(path, str) and os.path.lexists(path):
                files.append(path)
        if include_timelapse:
            files.extend(self._iter_files(self.timelapse_dir))

        deleted = []
        deleted_dirs = []
        file_count = 0
        byte_count = 0
        for path in sorted(set(files)):
            size = self._delete_file(path)
            if size is None:
                continue
            deleted.append(path)
            file_count += 1
            byte_count += size

        for root in (self.config_dir, self.timelapse_dir if include_timelapse else None):
            if not isinstance(root, str) or not root:
                continue
            for path in self._iter_dirs_bottom_up(root):
                if self._remove_empty_dir(path):
                    deleted_dirs.append(path)

        return sorted(deleted), sorted(deleted_dirs), file_count, byte_count

    def _iter_files(self, root):
        """List files/symlinks under ``root`` without following escaping links."""
        found = []
        if not isinstance(root, str) or not os.path.isdir(root) or os.path.islink(root):
            return found
        real_root = os.path.realpath(self.data_root)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if not _within(os.path.realpath(dirpath), real_root):
                dirnames[:] = []
                continue
            keep = []
            for name in dirnames:
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    found.append(full)  # unlink the link itself, not its target
                else:
                    keep.append(name)
            dirnames[:] = keep
            for name in filenames:
                found.append(os.path.join(dirpath, name))
        return found

    def _iter_dirs_bottom_up(self, root):
        """List directories under ``root`` (excluding ``root``), deepest first."""
        found = []
        if not isinstance(root, str) or not os.path.isdir(root) or os.path.islink(root):
            return found
        real_root = os.path.realpath(self.data_root)
        root_norm = os.path.normpath(root)
        for dirpath, dirnames, _filenames in os.walk(root, followlinks=False):
            if not _within(os.path.realpath(dirpath), real_root):
                dirnames[:] = []
                continue
            dirnames[:] = [
                name for name in dirnames
                if not os.path.islink(os.path.join(dirpath, name))
            ]
            if os.path.normpath(dirpath) != root_norm:
                found.append(dirpath)
        found.sort(key=lambda path: path.count(os.sep), reverse=True)
        return found

    def _delete_file(self, path):
        """Delete one file/symlink inside the root; return its size or None."""
        if not isinstance(path, str) or not os.path.lexists(path):
            return None
        real_root = os.path.realpath(self.data_root)
        if os.path.islink(path):
            if not _within(os.path.realpath(os.path.dirname(path)), real_root):
                raise _ResetError('refusing to delete outside the data root')
        elif not _within(os.path.realpath(path), real_root):
            raise _ResetError('refusing to delete outside the data root')
        try:
            size = os.lstat(path).st_size
            os.remove(path)
        except OSError as exc:
            raise _ResetError(
                f'could not delete durable data: {exc.strerror or "error"}'
            )
        return size

    def _remove_empty_dir(self, path):
        """Remove ``path`` when empty and inside the root; return True if done."""
        if not _within(os.path.realpath(path), os.path.realpath(self.data_root)):
            return False
        try:
            os.rmdir(path)
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------ #
    # Pending-deletion marker
    # ------------------------------------------------------------------ #

    def _write_pending_deletion(self, backup_path, timestamp):
        """Atomically write the marker; return ``(ok, reason)``."""
        payload = {
            'backup_path': backup_path,
            'created_at': self._clock().isoformat(),
            'timestamp': timestamp,
        }
        if not isinstance(self.pending_deletion_path, str) or not self.pending_deletion_path:
            return False, 'pending deletion marker path is not configured'
        temporary = self.pending_deletion_path + '.tmp'
        try:
            with open(temporary, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write('\n')
            os.replace(temporary, self.pending_deletion_path)
        except OSError as exc:
            try:
                os.remove(temporary)
            except OSError:
                pass
            return False, f'pending deletion marker could not be written: {exc.strerror or "error"}'
        return True, ''

    def _remove_marker(self):
        """Best-effort removal of the marker (never raises)."""
        try:
            os.remove(self.pending_deletion_path)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _refuse(self, reason, timestamp=''):
        return FactoryResetReport(ok=False, reason=reason, timestamp=timestamp)

    def _store(self, report):
        self._last_report = report
        return report
