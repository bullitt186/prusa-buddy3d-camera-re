"""SSH enable/disable control for the public appliance (WP-3d1, AC-20).

Source plan §4.6: **SSH is disabled by default.** Raspberry Pi Imager may enable
it independently and create the operator account, so this module never assumes
the service is absent or off; it only reports and toggles the live state.

Re-authentication
-----------------
Enabling SSH is a re-auth-gated action: it is listed in
:data:`admin_auth.REAUTH_ACTIONS` as ``ssh_enable``, so a caller must first run
:func:`admin_auth.requires_reauth` and verify the current admin password via
:func:`admin_auth.reauth_ok`. **That check is deliberately not implemented
here** -- :func:`enable_ssh` must only be called after it has passed.

Host-only / injectable commands
-------------------------------
This module is stdlib-only and import-safe: importing it runs no command and
touches no service. Every ``systemctl`` invocation goes through an injectable
``runner(args, timeout)`` callable (the default wraps :func:`subprocess.run`),
so the automated tests replace it with a fake and never run ``systemctl``.

Documented invocations
----------------------
* enable:      ``systemctl enable --now ssh``
* disable:     ``systemctl disable --now ssh``
* is-enabled:  ``systemctl is-enabled ssh``

Every public function returns an :class:`SshResult` and never raises on a
command failure or a timeout. Failure reasons contain only the command shape,
exit status and tool name -- never a credential.
"""
import dataclasses
import logging
import subprocess

log = logging.getLogger('prusa-cam.ssh_control')

#: The documented systemd unit controlled here.
SSH_SERVICE = 'ssh'

#: Bounded wall-clock timeout for every ``systemctl`` command.
COMMAND_TIMEOUT_SECONDS = 20.0

#: AC-20: SSH is disabled by default (Raspberry Pi Imager may enable it).
SSH_ENABLED_BY_DEFAULT = False

#: The :data:`admin_auth.REAUTH_ACTIONS` entry that gates :func:`enable_ssh`.
REAUTH_ACTION = 'ssh_enable'

_REASON_MAX = 200


# --------------------------------------------------------------------------- #
# Result object
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class SshResult:
    """Outcome of an SSH operation (never carries a secret)."""

    ok: bool
    reason: str = ''
    enabled: bool = False
    service: str = SSH_SERVICE


# --------------------------------------------------------------------------- #
# Runner plumbing
# --------------------------------------------------------------------------- #

def _default_runner(args, timeout):
    """Run ``args`` with a bounded timeout, capturing text stdout/stderr.

    The single place this module touches :mod:`subprocess`; tests replace it
    with a fake so no hardware or process is involved.
    """
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _stdout(result):
    """Best-effort text stdout of a runner result (never raises)."""
    value = getattr(result, 'stdout', '') or ''
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value


def _returncode(result):
    """Best-effort return code of a runner result (missing means failure)."""
    value = getattr(result, 'returncode', 1)
    return value if isinstance(value, int) else 1


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


def _run(runner, args, timeout):
    """Invoke ``runner`` and normalize the outcome.

    Returns ``(returncode, stdout, failure_reason)`` where ``failure_reason`` is
    a non-secret string on an exception and ``None`` on a completed command.
    """
    try:
        result = runner(list(args), timeout)
    except subprocess.TimeoutExpired:
        return None, '', 'systemctl command timed out'
    except OSError:
        return None, '', 'systemctl unavailable'
    except Exception as e:  # noqa: BLE001 - SSH control must never raise
        # Log only the class name: an injected runner's exception text could
        # otherwise carry argv into the log.
        log.warning(f'ssh_control: runner failed: {type(e).__name__}')
        return None, '', 'systemctl command failed'
    return _returncode(result), _stdout(result), None


# --------------------------------------------------------------------------- #
# Command shapes
# --------------------------------------------------------------------------- #

def _enable_command():
    """``systemctl enable --now ssh``."""
    return ['systemctl', 'enable', '--now', SSH_SERVICE]


def _disable_command():
    """``systemctl disable --now ssh``."""
    return ['systemctl', 'disable', '--now', SSH_SERVICE]


def _is_enabled_command():
    """``systemctl is-enabled ssh``."""
    return ['systemctl', 'is-enabled', SSH_SERVICE]


# --------------------------------------------------------------------------- #
# Public operations
# --------------------------------------------------------------------------- #

def enable_ssh(runner=None):
    """Enable and start SSH (``systemctl enable --now ssh``).

    **Re-auth gated:** call this only after
    ``admin_auth.requires_reauth('ssh_enable')`` (see
    :data:`admin_auth.REAUTH_ACTIONS`) and the password re-check have passed.
    This function does not implement that check. Returns an :class:`SshResult`;
    a command failure or timeout is reported, never raised.
    """
    runner = runner or _default_runner
    returncode, _out, failure = _run(runner, _enable_command(), COMMAND_TIMEOUT_SECONDS)
    if failure is not None:
        return SshResult(False, failure)
    if returncode != 0:
        return SshResult(
            False,
            _sanitize_reason(f'systemctl enable --now ssh failed (exit {returncode})'),
        )
    return SshResult(True, '', enabled=True)


def disable_ssh(runner=None):
    """Disable and stop SSH (``systemctl disable --now ssh``).

    Returns an :class:`SshResult`; a command failure or timeout is reported,
    never raised.
    """
    runner = runner or _default_runner
    returncode, _out, failure = _run(runner, _disable_command(), COMMAND_TIMEOUT_SECONDS)
    if failure is not None:
        return SshResult(False, failure)
    if returncode != 0:
        return SshResult(
            False,
            _sanitize_reason(f'systemctl disable --now ssh failed (exit {returncode})'),
        )
    return SshResult(True, '', enabled=False)


def ssh_enabled(runner=None):
    """Return whether SSH is currently enabled (``systemctl is-enabled ssh``).

    A zero exit means enabled. A non-zero exit is the normal "not enabled"
    answer (``disabled``/``masked``/``not-found``) and is reported as
    ``ok=True, enabled=False``; only an exception or a timeout is a failure
    (``ok=False``). Never raises.
    """
    runner = runner or _default_runner
    returncode, _out, failure = _run(runner, _is_enabled_command(), COMMAND_TIMEOUT_SECONDS)
    if failure is not None:
        return SshResult(False, failure)
    return SshResult(True, '', enabled=(returncode == 0))
