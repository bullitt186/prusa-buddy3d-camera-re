"""Fixed-verb privileged operations for the appliance (WP-R1, AC-17).

The admin/provisioning UI runs as the unprivileged ``prusa-cam`` service account,
but the wizard's finish path must stop the provisioning unit, stop the setup AP,
activate station networking, and start the camera target — all root-only actions.
Rather than granting broad ``sudo`` rights, the image installs a single
fixed-verb root helper (``/usr/libexec/prusa-cam/prusa-priv``) and a sudoers rule
that allows only that helper. This module is the unprivileged client of it.

Security contract
-----------------
* The verb allowlist lives here *and* in the helper; an unknown verb is rejected
  before any process is started.
* The helper is invoked as ``sudo -n /usr/libexec/prusa-cam/prusa-priv <verb>``.
  The sudoers entry lists the helper without arguments, which (per sudoers
  semantics) permits any arguments — so the helper itself validates the verb.
* The Wi-Fi PSK is passed to ``wifi-station-apply`` on **stdin** and never
  appears in argv, a log line, or a failure reason.
* Every wrapper never raises: a timeout, a missing tool, or a non-zero exit is
  reported as a bounded, non-secret reason.

Stdlib only, and importing this module runs no command. The injectable
``runner(args, timeout, input=None)`` callable mirrors :mod:`wifi_station`, so
the tests replace it with a fake.
"""
import dataclasses
import logging
import subprocess

import hotspot

log = logging.getLogger('prusa-cam.privileged')

#: The fixed-verb root helper installed by ``image/assets/install-factory-app.sh``.
PRIVILEGED_HELPER = '/usr/libexec/prusa-cam/prusa-priv'

#: The privilege-escalation command. ``-n`` fails instead of prompting, which is
#: essential for a non-interactive service.
SUDO = 'sudo'

#: Every verb the helper understands. The helper re-validates this list.
VERBS = frozenset({
    'start-camera',
    'stop-provisioning',
    'hotspot-start',
    'hotspot-stop',
    'wifi-station-apply',
})

#: Bounded wall-clock timeout for a privileged invocation.
COMMAND_TIMEOUT_SECONDS = 60.0

#: Longer budget for station activation: ``wifi_station.apply`` may run several
#: ``nmcli`` commands (add/modify/reload/up) that each carry their own bounded
#: timeout, so the outer helper timeout must exceed their worst-case sum. A
#: mismatch would SIGKILL only ``sudo`` and leave the root-side activation
#: running after the wizard had already reported failure.
STATION_TIMEOUT_SECONDS = 180.0

_REASON_MAX = 200


@dataclasses.dataclass
class PrivilegedResult:
    """Outcome of a privileged invocation (never carries a secret).

    Truthy exactly when ``ok`` is true, so :class:`setup_wizard.WizardSession`
    can treat it as the documented "returns falsy on failure" callback.
    """

    ok: bool
    reason: str = ''

    def __bool__(self):
        return bool(self.ok)


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

    The single place this module touches :mod:`subprocess`; tests replace it
    with a fake so no privileged command runs on a workstation.
    """
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        input=input,
    )


def _returncode(result):
    """Best-effort return code of a runner result (missing means failure)."""
    value = getattr(result, 'returncode', 1)
    return value if isinstance(value, int) else 1


def _invoke(verb, *extra, input=None, runner=None, timeout=None):
    """Invoke ``verb`` through the helper and normalize the outcome.

    Returns a :class:`PrivilegedResult`; never raises. ``extra`` are the verb's
    non-secret arguments (currently only the station SSID). ``input`` carries
    stdin (the PSK) and is never logged or included in ``reason``.
    """
    runner = runner or _default_runner
    if verb not in VERBS:
        return PrivilegedResult(False, 'unsupported privileged action')
    if timeout is None:
        timeout = COMMAND_TIMEOUT_SECONDS
    args = [SUDO, '-n', PRIVILEGED_HELPER, verb, *extra]
    try:
        result = runner(args, timeout, input=input)
    except subprocess.TimeoutExpired:
        return PrivilegedResult(False, 'privileged action timed out')
    except OSError:
        return PrivilegedResult(False, 'privileged helper unavailable')
    except Exception as e:  # noqa: BLE001 - privileged control must never raise
        log.warning(f'privileged: runner failed: {type(e).__name__}')
        return PrivilegedResult(False, 'privileged action failed')
    returncode = _returncode(result)
    if returncode != 0:
        return PrivilegedResult(
            False,
            _sanitize_reason(f'{verb} failed (exit {returncode})'),
        )
    return PrivilegedResult(True, '')


# --------------------------------------------------------------------------- #
# Wrappers
# --------------------------------------------------------------------------- #

def start_camera(runner=None):
    """Start ``prusa-camera.target`` as root; returns a plain ``bool``.

    The wizard's finish callback treats a falsy result as a failed hand-off.
    """
    return _invoke('start-camera', runner=runner).ok


def stop_provisioning(runner=None):
    """Stop ``prusa-provisioning.service`` as root."""
    return _invoke('stop-provisioning', runner=runner)


def hotspot_start(runner=None):
    """Start the setup AP as root (``hotspot_ctl.py start``)."""
    return _invoke('hotspot-start', runner=runner)


def hotspot_stop(runner=None):
    """Stop the setup AP as root (``hotspot_ctl.py stop``)."""
    return _invoke('hotspot-stop', runner=runner)


def activate_station(ssid, psk, runner=None):
    """Create/activate the station profile as root; PSK passed on stdin.

    Returns a :class:`PrivilegedResult`. The SSID is the only non-secret
    argument; the PSK is stdin and never appears in argv or ``reason``.
    """
    if not isinstance(ssid, str) or not ssid.strip():
        return PrivilegedResult(False, 'wifi SSID is required')
    if psk is None:
        psk = ''
    if not isinstance(psk, str):
        return PrivilegedResult(False, 'wifi PSK must be a string')
    return _invoke(
        'wifi-station-apply', ssid.strip(), input=psk, runner=runner,
        timeout=STATION_TIMEOUT_SECONDS,
    )


class PrivilegedHotspot:
    """Hotspot controller whose start/stop need root, status stays read-only.

    ``start``/``stop`` route through the privileged helper (the setup AP and its
    pinned ``192.168.4.1/24`` address need NetworkManager write access);
    ``status``/``is_active`` are read-only ``nmcli`` queries and delegate to
    :mod:`hotspot` directly.
    """

    def start(self, ssid, password=None, ifname=hotspot.DEFAULT_IFNAME, runner=None):
        # The open setup AP needs no PSK, so only the interface matters; the
        # helper derives the SSID from the same device identity.
        return hotspot_start(runner=runner)

    def stop(self, ifname=hotspot.DEFAULT_IFNAME, runner=None):
        return hotspot_stop(runner=runner)

    def status(self, ifname=hotspot.DEFAULT_IFNAME, runner=None):
        return hotspot.status(ifname=ifname, runner=runner)

    def is_active(self, ifname=hotspot.DEFAULT_IFNAME, runner=None):
        return hotspot.is_active(ifname=ifname, runner=runner)
