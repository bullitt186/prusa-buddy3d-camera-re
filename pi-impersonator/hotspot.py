"""First-boot setup hotspot control (WP-3c, AC-17).

While the appliance is *unclaimed* it exposes a temporary Wi-Fi access point so a
headless, unlabelled device can be reached without a display or a pre-shared
credential (source plan §4.3). The captive portal is served at
``http://192.168.4.1``; the SSID is ``Buddy3D-Setup-<last6-device-id>``
(:func:`setup_ssid`, delegated to :mod:`provisioning`).

The hotspot is **only** active while the device is unclaimed. It is stopped
before ``prusa-camera.target`` starts the camera source, so the runtime never
shares the wireless interface with the setup portal.

Host-only / injectable commands
-------------------------------
This module is stdlib-only and import-safe: importing it runs no command and
touches no interface. Every NetworkManager invocation goes through an injectable
``runner(args, timeout)`` callable (the default wraps :func:`subprocess.run`),
so the automated tests replace it with a fake and never run ``nmcli``.

Documented ``nmcli`` invocations
--------------------------------
* start:      ``nmcli device wifi hotspot ifname <ifname> ssid <ssid>``
              plus ``password <psk>`` when a WPA2 password is supplied
              (a unique credential cannot be delivered from a headless DIY
              device, so the hotspot is open by default; source §4.3).
* stop:       ``nmcli device disconnect <ifname>``
* is_active:  ``nmcli -t -f GENERAL.CONNECTION device show <ifname>`` then, when
              a connection is active, ``nmcli -t -f 802-11-wireless.mode
              connection show <name>``; the hotspot is active when the mode is
              ``ap``.
* status:     :func:`is_active`, plus
              ``nmcli -t -f 802-11-wireless.ssid connection show <name>`` for
              the SSID.

Every public function returns a :class:`HotspotResult` and never raises on a
command failure or a timeout. Failure reasons contain only the command shape,
exit status and the tool name — never a PSK.
"""
import dataclasses
import logging
import subprocess

import provisioning

log = logging.getLogger('prusa-cam.hotspot')

#: The documented captive portal address (source plan §4.3).
CAPTIVE_PORTAL_IP = '192.168.4.1'

#: Convenience URL form of :data:`CAPTIVE_PORTAL_IP`.
CAPTIVE_PORTAL_URL = 'http://' + CAPTIVE_PORTAL_IP

#: Default wireless interface used by the setup hotspot.
DEFAULT_IFNAME = 'wlan0'

#: Bounded wall-clock timeout for every NetworkManager command.
COMMAND_TIMEOUT_SECONDS = 20.0

#: WPA2 password bounds accepted by ``nmcli device wifi hotspot``.
MIN_PSK_LENGTH = 8
MAX_PSK_LENGTH = 63

_REASON_MAX = 200


# --------------------------------------------------------------------------- #
# Result object
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class HotspotResult:
    """Outcome of a hotspot operation (never carries a secret).

    ``ok`` is whether the operation completed; ``reason`` is an exact,
    non-secret failure. ``active``/``ssid`` describe the observed hotspot for
    :func:`is_active`/:func:`status`; ``address`` is always the documented
    captive portal address.
    """

    ok: bool
    reason: str = ''
    active: bool = False
    ssid: str = ''
    ifname: str = ''
    address: str = CAPTIVE_PORTAL_IP


# --------------------------------------------------------------------------- #
# Documented constants / identity
# --------------------------------------------------------------------------- #

def captive_portal_address():
    """Return the documented captive portal IP, ``'192.168.4.1'`` (source §4.3)."""
    return CAPTIVE_PORTAL_IP


def setup_ssid(device_id):
    """Return the unclaimed setup SSID, delegated to :func:`provisioning.setup_ssid`."""
    return provisioning.setup_ssid(device_id)


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
        return None, '', 'network manager command timed out'
    except OSError:
        return None, '', 'network manager tool unavailable'
    except Exception as e:  # noqa: BLE001 - hotspot control must never raise
        # Log only the class name: an injected runner's exception text could
        # otherwise carry argv (and a PSK) into the log.
        log.warning(f'hotspot: runner failed: {type(e).__name__}')
        return None, '', 'network manager command failed'
    return _returncode(result), _stdout(result), None


# --------------------------------------------------------------------------- #
# Command shapes
# --------------------------------------------------------------------------- #

def _start_command(ssid, password, ifname):
    """``nmcli device wifi hotspot …`` for an open or WPA2 setup AP."""
    command = ['nmcli', 'device', 'wifi', 'hotspot', 'ifname', ifname, 'ssid', ssid]
    if password:
        command += ['password', password]
    return command


def _disconnect_command(ifname):
    """``nmcli device disconnect <ifname>`` stops the setup AP."""
    return ['nmcli', 'device', 'disconnect', ifname]


def _active_connection_command(ifname):
    """``nmcli -t -f GENERAL.CONNECTION device show <ifname>``."""
    return ['nmcli', '-t', '-f', 'GENERAL.CONNECTION', 'device', 'show', ifname]


def _connection_mode_command(name):
    """``nmcli -t -f 802-11-wireless.mode connection show <name>``."""
    return ['nmcli', '-t', '-f', '802-11-wireless.mode', 'connection', 'show', name]


def _connection_ssid_command(name):
    """``nmcli -t -f 802-11-wireless.ssid connection show <name>``."""
    return ['nmcli', '-t', '-f', '802-11-wireless.ssid', 'connection', 'show', name]


def _unescape(value):
    """Undo ``nmcli -t`` backslash escaping for a terse field value."""
    return value.replace('\\:', ':').replace('\\\\', '\\')


def _parse_field(text, field):
    """Return the value of terse ``<field>:<value>`` output, or ``''``."""
    for line in text.splitlines():
        key, sep, value = line.partition(':')
        if sep and key == field:
            return _unescape(value).strip()
    return ''


def _connection_name(runner, ifname):
    """Return ``(name, failure)`` for the active connection on ``ifname``.

    ``name`` is ``''`` when no connection is active. ``failure`` is a
    non-secret reason on a command failure and ``None`` otherwise.
    """
    returncode, stdout, failure = _run(
        runner, _active_connection_command(ifname), COMMAND_TIMEOUT_SECONDS
    )
    if failure is not None:
        return '', failure
    if returncode != 0:
        return '', _sanitize_reason(
            f'nmcli device show {ifname} failed (exit {returncode})'
        )
    name = _parse_field(stdout, 'GENERAL.CONNECTION')
    if not name or name == '--':
        return '', None
    return name, None


# --------------------------------------------------------------------------- #
# Public operations
# --------------------------------------------------------------------------- #

def start(ssid, password=None, ifname=DEFAULT_IFNAME, runner=None):
    """Start the setup hotspot (``nmcli device wifi hotspot …``).

    ``password`` is optional; when absent the AP is open, as documented for a
    headless first-boot device. Returns a :class:`HotspotResult`; an invalid
    SSID/password or a command failure/timeout is reported, never raised.
    """
    runner = runner or _default_runner
    if not isinstance(ssid, str) or not ssid.strip():
        return HotspotResult(False, 'setup SSID is required', ifname=ifname)
    if password is not None and password != '':
        if not isinstance(password, str):
            return HotspotResult(False, 'hotspot password must be a string', ifname=ifname)
        if not MIN_PSK_LENGTH <= len(password) <= MAX_PSK_LENGTH:
            return HotspotResult(
                False,
                f'hotspot password must be {MIN_PSK_LENGTH}..{MAX_PSK_LENGTH} characters',
                ifname=ifname,
            )
    returncode, _stdout_text, failure = _run(
        runner, _start_command(ssid, password, ifname), COMMAND_TIMEOUT_SECONDS
    )
    if failure is not None:
        return HotspotResult(False, failure, ifname=ifname)
    if returncode != 0:
        return HotspotResult(
            False,
            _sanitize_reason(f'nmcli hotspot start failed (exit {returncode})'),
            ifname=ifname,
        )
    return HotspotResult(True, '', active=True, ssid=ssid, ifname=ifname)


def stop(ifname=DEFAULT_IFNAME, runner=None):
    """Stop the setup hotspot (``nmcli device disconnect <ifname>``).

    Called before the camera target starts. Returns a :class:`HotspotResult`;
    a command failure/timeout is reported, never raised.
    """
    runner = runner or _default_runner
    returncode, _stdout_text, failure = _run(
        runner, _disconnect_command(ifname), COMMAND_TIMEOUT_SECONDS
    )
    if failure is not None:
        return HotspotResult(False, failure, ifname=ifname)
    if returncode != 0:
        return HotspotResult(
            False,
            _sanitize_reason(f'nmcli device disconnect failed (exit {returncode})'),
            ifname=ifname,
        )
    return HotspotResult(True, '', active=False, ifname=ifname)


def is_active(ifname=DEFAULT_IFNAME, runner=None):
    """Return whether the setup hotspot is the active AP on ``ifname``.

    Uses the two documented queries (:data:`GENERAL.CONNECTION` then the
    connection's ``802-11-wireless.mode``); active means mode ``ap``.
    """
    runner = runner or _default_runner
    name, failure = _connection_name(runner, ifname)
    if failure is not None:
        return HotspotResult(False, failure, ifname=ifname)
    if not name:
        return HotspotResult(True, '', active=False, ifname=ifname)
    returncode, stdout, failure = _run(
        runner, _connection_mode_command(name), COMMAND_TIMEOUT_SECONDS
    )
    if failure is not None:
        return HotspotResult(False, failure, ifname=ifname)
    if returncode != 0:
        return HotspotResult(
            False,
            _sanitize_reason(f'nmcli connection show failed (exit {returncode})'),
            ifname=ifname,
        )
    mode = _parse_field(stdout, '802-11-wireless.mode')
    return HotspotResult(True, '', active=(mode == 'ap'), ifname=ifname)


def status(ifname=DEFAULT_IFNAME, runner=None):
    """Return the hotspot state (active, SSID, interface, portal address).

    Combines :func:`is_active` with the documented SSID query. A command
    failure/timeout is reported, never raised.
    """
    runner = runner or _default_runner
    active = is_active(ifname=ifname, runner=runner)
    if not active.ok or not active.active:
        return active
    name, failure = _connection_name(runner, ifname)
    if failure is not None:
        return HotspotResult(False, failure, ifname=ifname)
    returncode, stdout, failure = _run(
        runner, _connection_ssid_command(name), COMMAND_TIMEOUT_SECONDS
    )
    if failure is not None:
        return HotspotResult(False, failure, ifname=ifname)
    if returncode != 0:
        return HotspotResult(
            False,
            _sanitize_reason(f'nmcli connection show failed (exit {returncode})'),
            ifname=ifname,
        )
    ssid = _parse_field(stdout, '802-11-wireless.ssid')
    return HotspotResult(True, '', active=True, ssid=ssid, ifname=ifname)
