"""Activate the post-claim Wi-Fi station profile (WP-R1, AC-17).

The wizard persists the user's SSID/PSK, but persisting them is not enough: the
claimed device must actually *join* the network. This module creates or updates a
deterministic NetworkManager station profile (``buddy3d-station``) and activates
it, so a claimed device comes up online instead of staying offline on wlan0.

It runs as the unprivileged ``prusa-cam`` service account through the fixed-verb
root helper ``prusa-priv`` (``wifi-station-apply``), which is the only way the
account may reach NetworkManager with write privileges.

Secret hygiene
--------------
The PSK is **never** placed in argv, a log line, or a failure reason. It is
written to a ``0600`` temporary ``nmcli passwd-file`` (root-only ``/run``, or a
``tempfile.mkstemp`` fallback) as the single documented line
``802-11-wireless-security.psk:<psk>`` and passed to
``nmcli connection up <name> passwd-file <file>``; the file is removed in a
``finally``. Only the SSID, which the station must advertise, appears in argv.
``main`` reads the PSK from stdin so the caller never has to pass it on a command
line.

Stdlib only, and importing this module runs no command and touches no interface:
every ``nmcli`` invocation goes through an injectable
``runner(args, timeout, input=None)`` callable (the default wraps
:func:`subprocess.run`), so the tests replace it with a fake.
"""
import argparse
import logging
import os
import subprocess
import sys
import tempfile

log = logging.getLogger('prusa-cam.wifi_station')

#: Deterministic NetworkManager station profile name. A stable name lets
#: ``apply`` update rather than accumulate profiles and lets the durable
#: ``/data/network/system-connections`` bind mount (B4) persist it across reboot.
CONNECTION_NAME = 'buddy3d-station'

#: Default wireless interface for the station profile.
DEFAULT_IFNAME = 'wlan0'

#: Preferred directory for the short-lived ``0600`` nmcli passwd-file. It runs as
#: root through ``prusa-priv``, so ``/run`` (tmpfs, root-only) is available; a
#: writable-directory fallback keeps the helper usable in a test sandbox.
PASSWD_FILE_DIR = '/run'

#: The single setting line nmcli expects in a passwd-file for a WPA2 PSK.
PASSWD_FILE_PREFIX = '802-11-wireless-security.psk:'

#: Bounded wall-clock timeout for every NetworkManager command.
COMMAND_TIMEOUT_SECONDS = 30.0

#: WPA2 PSK bounds (mirrors the wizard / hotspot validation).
MIN_PSK_LENGTH = 8
MAX_PSK_LENGTH = 63

_REASON_MAX = 200


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


def _default_runner(args, timeout, input=None):
    """Run ``args`` with a bounded timeout, capturing text stdout/stderr.

    The single place this module touches :mod:`subprocess`; tests replace it with
    a fake so no hardware or process is involved. The ``input`` parameter is kept
    for interface compatibility with the other runners but is unused: the PSK
    travels in a 0600 passwd-file, never in argv or on stdin.
    """
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        input=input,
    )


def _stdout(result):
    """Best-effort text stdout of a runner result (never raises)."""
    value = getattr(result, 'stdout', '') or ''
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value


def _run(runner, args, timeout, input=None):
    """Invoke ``runner`` and normalize the outcome.

    Returns ``(returncode, stdout, failure_reason)`` where ``failure_reason`` is
    a non-secret string on an exception and ``None`` on a completed command.
    """
    try:
        result = runner(list(args), timeout, input=input)
    except subprocess.TimeoutExpired:
        return None, '', 'network manager command timed out'
    except OSError:
        return None, '', 'network manager tool unavailable'
    except Exception as e:  # noqa: BLE001 - station setup must never raise
        # Log only the class name: an injected runner's exception text could
        # otherwise carry argv into the log.
        log.warning(f'wifi_station: runner failed: {type(e).__name__}')
        return None, '', 'network manager command failed'
    return _returncode(result), _stdout(result), None


def _returncode(result):
    """Best-effort return code of a runner result (missing means failure)."""
    value = getattr(result, 'returncode', 1)
    return value if isinstance(value, int) else 1


# --------------------------------------------------------------------------- #
# Command shapes (no PSK ever appears in these argument lists)
# --------------------------------------------------------------------------- #

def _add_command(ssid, ifname, wpa):
    """``nmcli connection add`` for the station profile (no PSK in argv)."""
    command = [
        'nmcli', 'connection', 'add', 'type', 'wifi',
        'ifname', ifname, 'con-name', CONNECTION_NAME,
        'autoconnect', 'yes', 'ssid', ssid,
        '--',
        'ipv4.method', 'auto',
    ]
    if wpa:
        command += ['wifi-sec.key-mgmt', 'wpa-psk']
    return command


def _modify_command(ssid, ifname, wpa):
    """``nmcli connection modify`` for an existing station profile (no PSK)."""
    command = [
        'nmcli', 'connection', 'modify', CONNECTION_NAME,
        'connection.autoconnect', 'yes',
        '802-11-wireless.ssid', ssid,
        'ipv4.method', 'auto',
    ]
    if wpa:
        command += ['wifi-sec.key-mgmt', 'wpa-psk']
    return command


def _remove_security_command():
    """Drop any stored WPA material when the network is open."""
    return [
        'nmcli', 'connection', 'modify', CONNECTION_NAME,
        'remove', '802-11-wireless-security',
    ]


def _up_command():
    """``nmcli connection up buddy3d-station`` activates the station profile."""
    return ['nmcli', 'connection', 'up', CONNECTION_NAME]


def _up_with_passwd_file_command(passwd_file):
    """``nmcli connection up <name> passwd-file <file>`` for a WPA2 network.

    This is NetworkManager's documented non-interactive way to supply a
    credential; unlike ``nmcli --ask`` it needs no controlling terminal.
    """
    return [
        'nmcli', 'connection', 'up', CONNECTION_NAME,
        'passwd-file', passwd_file,
    ]


# --------------------------------------------------------------------------- #
# Short-lived passwd-file (0600, removed after activation)
# --------------------------------------------------------------------------- #

def _passwd_file_dir(preferred=None):
    """Return the first writable directory among preferred/``/run``/tmp."""
    for candidate in (preferred, PASSWD_FILE_DIR, tempfile.gettempdir()):
        if candidate and os.path.isdir(candidate) and os.access(candidate, os.W_OK):
            return candidate
    return tempfile.gettempdir()


def _write_passwd_file(psk, directory=None):
    """Write the PSK to a ``0600`` nmcli passwd-file.

    Returns ``(path, reason)``; ``path`` is ``''`` and ``reason`` is a bounded,
    non-secret failure when the file cannot be created. The caller removes the
    file (see :func:`_remove_passwd_file`).
    """
    target_dir = _passwd_file_dir(directory)
    try:
        fd, path = tempfile.mkstemp(prefix='buddy3d-psk-', dir=target_dir)
    except OSError:
        return '', 'could not create Wi-Fi credential file'
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(PASSWD_FILE_PREFIX + psk + '\n')
        return path, ''
    except OSError:
        # os.fdopen owns and closes fd once it succeeds; on failure close it
        # best-effort and remove the partial file.
        try:
            os.close(fd)
        except OSError:
            pass
        _remove_passwd_file(path)
        return '', 'could not write Wi-Fi credential file'


def _remove_passwd_file(path):
    """Best-effort removal of the passwd-file (never raises, never logs it)."""
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        log.warning('wifi_station: could not remove Wi-Fi credential file')


# --------------------------------------------------------------------------- #
# Public operation
# --------------------------------------------------------------------------- #

def apply(ssid, psk='', ifname=DEFAULT_IFNAME, runner=None, passwd_dir=None):
    """Create/update and activate the station profile.

    Returns ``(ok, reason)``. ``psk`` may be empty for an open network. The PSK
    is written to a ``0600`` nmcli passwd-file (never argv, a log line, or
    ``reason``) that is removed afterwards; an open network uses plain
    ``nmcli connection up``. ``passwd_dir`` overrides the temporary-file
    directory for tests. Never raises.
    """
    runner = runner or _default_runner
    if not isinstance(ssid, str) or not ssid.strip():
        return False, 'wifi SSID is required'
    if psk is None:
        psk = ''
    if not isinstance(psk, str):
        return False, 'wifi PSK must be a string'
    if psk and not MIN_PSK_LENGTH <= len(psk) <= MAX_PSK_LENGTH:
        return False, f'wifi PSK must be {MIN_PSK_LENGTH}..{MAX_PSK_LENGTH} characters'
    ssid = ssid.strip()
    wpa = bool(psk)

    returncode, _out, failure = _run(
        runner, _add_command(ssid, ifname, wpa), COMMAND_TIMEOUT_SECONDS
    )
    if failure is not None:
        return False, failure
    if returncode != 0:
        # The profile may already exist: update it instead of failing.
        returncode, _out, failure = _run(
            runner, _modify_command(ssid, ifname, wpa), COMMAND_TIMEOUT_SECONDS
        )
        if failure is not None:
            return False, failure
        if returncode != 0:
            return False, _sanitize_reason(
                f'nmcli station profile failed (exit {returncode})'
            )

    if not wpa:
        # Best effort: an open profile must not retain stale WPA material.
        _run(runner, _remove_security_command(), COMMAND_TIMEOUT_SECONDS)

    passwd_file = ''
    try:
        if wpa:
            passwd_file, write_reason = _write_passwd_file(psk, directory=passwd_dir)
            if not passwd_file:
                # Do not attempt the connection without a usable credential file.
                return False, write_reason
        command = (
            _up_with_passwd_file_command(passwd_file) if wpa else _up_command()
        )
        returncode, _out, failure = _run(runner, command, COMMAND_TIMEOUT_SECONDS)
        if failure is not None:
            return False, failure
        if returncode != 0:
            return False, _sanitize_reason(
                f'nmcli station activation failed (exit {returncode})'
            )
        return True, ''
    finally:
        if passwd_file:
            _remove_passwd_file(passwd_file)


def main(argv=None):
    """CLI entry point used by ``prusa-priv wifi-station-apply <ssid>``.

    The SSID is an argument; the PSK is read from **stdin** so it never reaches
    the process list. An empty stdin means an open network. Exit status is 0 on
    success and 1 on failure; diagnostics go to stderr and never carry the PSK.
    """
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(description='Buddy3D Wi-Fi station activation')
    parser.add_argument('action', choices=('apply',))
    parser.add_argument('ssid')
    args = parser.parse_args(argv)

    psk = sys.stdin.readline()
    if psk.endswith('\n'):
        psk = psk[:-1]
    if psk.endswith('\r'):
        psk = psk[:-1]

    ok, reason = apply(args.ssid, psk)
    if not ok:
        print(f'wifi_station: apply failed: {reason}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
