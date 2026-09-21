"""Recovery entry points for the public appliance (WP-3d1, AC-20).

This module owns the *host-testable* core of the two recovery entries described
by the source plan §4.6:

* An authenticated "Enter setup mode" action in the web UI or CLI
  (:func:`enter_setup_mode`).
* A documented ``buddy3d-recovery`` sentinel placed on the BOOT partition while
  the device is powered off (:data:`RECOVERY_SENTINEL`,
  :func:`sentinel_present`, :func:`set_sentinel`, :func:`clear_sentinel`).

It also exposes the two small decision helpers the image/systemd and the
provisioning state machine consume: :func:`recovery_required` (should the
appliance enter recovery?) and :func:`boot_action` (which target should the next
boot start?).

Durability
----------
The Pi root filesystem is a read-only overlayfs and every runtime write is lost
on reboot. The BOOT (FAT) partition is the only place a powered-off operator can
reach, so the sentinel is written there and the file *and* its directory are
fsynced before the call reports success.

Secret hygiene
--------------
The sentinel lives on a partition an operator can read and edit, so it must
never carry a credential. :func:`set_sentinel` sanitizes ``reason`` (printable
characters only, bounded length) and callers must pass a non-secret reason. No
token, PSK, password hash or MQTT credential is ever written here. Rejection
reasons are non-secret too.

Stdlib only, and no file/network/thread side effects on import.
"""
import dataclasses
import logging
import os

log = logging.getLogger('prusa-cam.recovery')

#: Documented recovery sentinel on the BOOT (FAT) partition (source §4.6).
#: Its presence makes the next boot start the setup hotspot instead of the
#: camera target. The path is injectable on every function so tests use a
#: ``tempfile`` path and never touch ``/boot``.
RECOVERY_SENTINEL = '/boot/firmware/buddy3d-recovery'

#: Provisioning state that means "already in recovery" (mirrors
#: :data:`provisioning.RECOVERY`; duplicated as a literal so this module stays
#: import-light).
RECOVERY_STATE = 'recovery'

#: The three documented boot dispatch targets.
BOOT_SETUP = 'setup'
BOOT_CAMERA = 'camera'
BOOT_RECOVERY = 'recovery'

_REASON_MAX = 200


# --------------------------------------------------------------------------- #
# Result object
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class RecoveryResult:
    """Outcome of a recovery/sentinel operation (never carries a secret)."""

    ok: bool
    reason: str = ''
    path: str = ''
    action: str = ''


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _sanitize_reason(reason):
    """Bound and strip a reason so it can never carry control characters."""
    if not isinstance(reason, str):
        return ''
    cleaned = ''.join(
        ch for ch in reason if ch.isprintable() and ch not in '\r\n\t'
    ).strip()
    if len(cleaned) > _REASON_MAX:
        cleaned = cleaned[:_REASON_MAX]
    return cleaned


def _state_name(state):
    """Return the provisioning state name for a string or state object."""
    value = getattr(state, 'state', state)
    return value if isinstance(value, str) else ''


def _fsync_dir(directory):
    """Best-effort fsync of ``directory`` so a create/remove survives a power cut."""
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


# --------------------------------------------------------------------------- #
# BOOT sentinel
# --------------------------------------------------------------------------- #

def sentinel_present(path=RECOVERY_SENTINEL):
    """Return True when the BOOT recovery sentinel exists at ``path``.

    Never raises: an unreadable or absent path simply means "not present".
    """
    if not isinstance(path, str) or not path:
        return False
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def set_sentinel(reason='', path=RECOVERY_SENTINEL):
    """Durably create the BOOT recovery sentinel, recording ``reason``.

    ``reason`` must be a **non-secret** operator note; it is sanitized (printable
    characters only, bounded length) before being written. The file and its
    directory are fsynced so the sentinel survives the next power cut. Returns a
    :class:`RecoveryResult` and never raises.
    """
    return _write_sentinel(reason, path, 'set')


def clear_sentinel(path=RECOVERY_SENTINEL):
    """Durably remove the BOOT recovery sentinel at ``path``.

    Idempotent: clearing an absent sentinel succeeds. The directory is fsynced
    so the removal survives the next power cut. Never raises.
    """
    if not isinstance(path, str) or not path:
        return RecoveryResult(False, 'recovery sentinel path is invalid', '', 'clear')
    if not sentinel_present(path):
        return RecoveryResult(True, 'no recovery sentinel present', path, 'clear')
    directory = os.path.dirname(path) or '.'
    try:
        os.remove(path)
        _fsync_dir(directory)
    except OSError as e:
        return RecoveryResult(
            False,
            f'could not remove recovery sentinel: {e.strerror or e}',
            path,
            'clear',
        )
    return RecoveryResult(True, '', path, 'clear')


def _write_sentinel(reason, path, action):
    """Shared durable sentinel writer used by :func:`set_sentinel` and setup entry."""
    if not isinstance(path, str) or not path:
        return RecoveryResult(False, 'recovery sentinel path is invalid', '', action)
    text = _sanitize_reason(reason)
    directory = os.path.dirname(path) or '.'
    try:
        os.makedirs(directory, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            with os.fdopen(fd, 'wb') as f:
                fd = -1
                f.write(((text + '\n') if text else '').encode('utf-8'))
                f.flush()
                os.fsync(f.fileno())
        finally:
            if fd >= 0:
                os.close(fd)
        _fsync_dir(directory)
    except OSError as e:
        return RecoveryResult(
            False,
            f'could not write recovery sentinel: {e.strerror or e}',
            path,
            action,
        )
    return RecoveryResult(True, '', path, action)


# --------------------------------------------------------------------------- #
# Authenticated setup entry
# --------------------------------------------------------------------------- #

def enter_setup_mode(reason, authorized, path=RECOVERY_SENTINEL):
    """Enter setup mode by setting the BOOT sentinel, but only when authorized.

    ``authorized`` must be exactly ``True``, and only after the caller has
    performed the re-authentication check for the setup action (the HTTP/CLI
    layer owns that check; it is deliberately not implemented here). The check
    is strict (``authorized is True``) so a truthy-but-not-True value cannot
    accidentally authorize the transition. An unauthorized call is rejected with
    a non-secret reason and writes **nothing**. On success the sentinel is set
    so the next boot starts the setup hotspot instead of the camera target.
    """
    if authorized is not True:
        return RecoveryResult(False, 're-authentication required', path, 'setup')
    return _write_sentinel(reason, path, 'setup')


# --------------------------------------------------------------------------- #
# Decision helpers
# --------------------------------------------------------------------------- #

def recovery_required(state, invalid_config=False, storage_unavailable=False,
                      manual=False):
    """Return ``(required, reason)`` for the documented recovery decision.

    Precedence (first match wins):

    1. ``manual``            an explicit operator/UI recovery request.
    2. ``invalid_config``    the durable configuration is invalid or too new.
    3. ``storage_unavailable`` the durable ``/data`` partition is missing.
    4. ``state``             the provisioning state is already ``recovery``.

    ``state`` may be a :class:`provisioning.ProvisioningState` or a plain state
    name. ``reason`` is non-secret and empty when recovery is not required.
    """
    if manual:
        return True, 'manual recovery requested'
    if invalid_config:
        return True, 'durable configuration is invalid'
    if storage_unavailable:
        return True, 'durable storage is unavailable'
    if _state_name(state) == RECOVERY_STATE:
        return True, 'provisioning state is recovery'
    return False, ''


def boot_action(sentinel_present, state, invalid_config=False,
                storage_unavailable=False):
    """Return the documented boot target for the image/systemd to consume.

    One of ``'setup'``, ``'camera'`` or ``'recovery'``. Precedence:

    1. the BOOT sentinel is present  -> ``'setup'`` (the operator asked for the
       setup hotspot, including the powered-off sentinel path);
    2. :func:`recovery_required`     -> ``'recovery'`` (show the failure and
       retry without starting the camera);
    3. otherwise                     -> ``'camera'`` (start the camera target).

    ``state`` may be a state object or a plain name. Never raises.
    """
    if sentinel_present:
        return BOOT_SETUP
    required, _reason = recovery_required(
        state,
        invalid_config=invalid_config,
        storage_unavailable=storage_unavailable,
    )
    if required:
        return BOOT_RECOVERY
    return BOOT_CAMERA
