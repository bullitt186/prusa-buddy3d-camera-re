"""Boot-mode selector for the appliance (WP-R1, AC-12/AC-17).

One flashed card must boot into exactly one runtime: while the device is
unclaimed it serves the setup hotspot + captive-portal wizard; once claimed it
starts the camera runtime. This module is the single, explicit decision point
that encodes that ordering (master AC-12)::

    data-ready.target -> prusa-provisioning.service   (unclaimed)
                      -> prusa-camera.target          (claimed)

``prusa-boot-mode.service`` runs ``boot_mode.py --apply`` at
``multi-user.target``; the selected unit is started with ``systemctl --no-block``
so the selector exits immediately and never gates boot.

Fail-closed default
-------------------
Only the three post-claim states ``claimed``/``configured``/``running`` select
the camera runtime. Every other state — ``factory``, ``storage_ready``,
``camera_validated``, ``unclaimed``, ``recovery``, an unreadable or corrupt
state file, or an unknown value — selects the provisioning/setup path, so a
device with no valid claim can never start the camera units (AC-17).

Stdlib only, and no command runs on import: the ``systemctl`` invocation goes
through an injectable ``runner(args, timeout)`` callable (default wraps
:func:`subprocess.run`), mirroring :mod:`hotspot`.
"""
import argparse
import logging
import subprocess
import sys

import provisioning

log = logging.getLogger('prusa-cam.boot_mode')

#: Boot modes.
MODE_PROVISIONING = 'provisioning'
MODE_CAMERA = 'camera'

#: The unit that serves the setup hotspot + wizard while unclaimed.
PROVISIONING_UNIT = 'prusa-provisioning.service'

#: The target that starts the camera runtime after claim.
CAMERA_TARGET = 'prusa-camera.target'

#: Provisioning states at/after which the camera runtime is selected. Deliberately
#: the same set :func:`admin_app.resolve_mode` uses, so the UI surface and the
#: runtime graph agree.
CAMERA_STATES = frozenset({'claimed', 'configured', 'running'})

#: Bounded wall-clock timeout for the ``systemctl`` invocation.
COMMAND_TIMEOUT_SECONDS = 30.0

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


def _default_runner(args, timeout):
    """Run ``args`` with a bounded timeout, capturing text stdout/stderr.

    The single place this module touches :mod:`subprocess`; tests replace it
    with a fake so no real unit is started.
    """
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def resolve_mode(state=None, *, provisioning_path=None):
    """Return the boot mode for ``state`` (fail-closed to ``provisioning``).

    ``state`` may be a :class:`provisioning.ProvisioningState`, a state string,
    or ``None``. When ``None`` the persisted state is loaded from
    ``provisioning_path`` (default :data:`provisioning.PROVISIONING_PATH`); a
    missing/unreadable/corrupt file loads as ``factory`` and therefore selects
    the provisioning path, never the camera runtime.
    """
    if state is None:
        try:
            state = provisioning.ProvisioningState.load(
                provisioning_path or provisioning.PROVISIONING_PATH)
        except Exception:  # noqa: BLE001 - never fail boot on a bad state file
            return MODE_PROVISIONING
    text = getattr(state, 'state', state)
    if isinstance(text, str) and text in CAMERA_STATES:
        return MODE_CAMERA
    return MODE_PROVISIONING


def unit_for(mode):
    """Return the systemd unit that ``mode`` selects.

    An unknown mode fails closed to :data:`PROVISIONING_UNIT`.
    """
    return CAMERA_TARGET if mode == MODE_CAMERA else PROVISIONING_UNIT


def start_unit(unit, runner=None):
    """Start ``unit`` with ``systemctl --no-block start``.

    Returns ``(ok, reason)``. A missing/blank unit name, a non-zero exit, a
    timeout or a missing ``systemctl`` is reported as a bounded, non-secret
    reason and never raised.
    """
    runner = runner or _default_runner
    if not isinstance(unit, str) or not unit.strip():
        return False, 'unit name is required'
    try:
        result = runner(
            ['systemctl', '--no-block', 'start', unit], COMMAND_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        return False, 'systemctl start timed out'
    except OSError:
        return False, 'systemctl unavailable'
    except Exception as e:  # noqa: BLE001 - boot selection must never raise
        log.warning(f'boot_mode: runner failed: {type(e).__name__}')
        return False, 'systemctl start failed'
    returncode = getattr(result, 'returncode', 1)
    if not isinstance(returncode, int):
        returncode = 1
    if returncode != 0:
        return False, _sanitize_reason(
            f'systemctl start {unit} failed (exit {returncode})'
        )
    return True, ''


def start_camera(runner=None):
    """Start :data:`CAMERA_TARGET`; the wizard ``finish`` callback.

    Returns a plain ``bool`` because :class:`setup_wizard.WizardSession` treats a
    falsy result as a failed hand-off and keeps the setup portal alive.
    """
    ok, _reason = start_unit(CAMERA_TARGET, runner=runner)
    return ok


def main(argv=None, runner=None):
    """CLI entry point used by ``prusa-boot-mode.service``.

    Without ``--apply`` the resolved mode and its unit are printed. With
    ``--apply`` the selected unit is started; exit status is 0 on success and 1
    on failure. Diagnostics go to stderr only.
    """
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(description='Buddy3D boot-mode selector')
    parser.add_argument(
        '--apply', action='store_true',
        help='start the selected unit (default: print the decision only)',
    )
    args = parser.parse_args(argv)

    mode = resolve_mode()
    unit = unit_for(mode)
    if not args.apply:
        print(mode)
        print(unit)
        return 0

    ok, reason = start_unit(unit, runner=runner)
    if not ok:
        print(f'boot_mode: failed to start {unit}: {reason}', file=sys.stderr)
        return 1
    log.info('boot_mode: selected %s; started %s', mode, unit)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
