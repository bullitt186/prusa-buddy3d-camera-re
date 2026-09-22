"""Setup-hotspot CLI for the provisioning service (WP-R1, AC-17).

``prusa-provisioning.service`` starts the setup AP as an ``ExecStartPre`` and
stops it as an ``ExecStopPost``::

    ExecStartPre=+.../hotspot_ctl.py start
    ExecStopPost=+.../hotspot_ctl.py stop

``start`` is idempotent: it is safe to re-run on a service restart, when the AP
may already be active. The AP is **open** (no PSK, source §4.3) — a unique
credential cannot be delivered from a headless DIY device.

The controller defaults to :mod:`hotspot` and is injectable for host testing, so
importing this module runs no ``nmcli`` command and touches no interface.
Failure reasons never contain a secret; ``start`` fails cleanly when no SSID can
be derived from the device identity rather than exposing a shared AP.
"""
import argparse
import logging
import sys

import hotspot
import provisioning

log = logging.getLogger('prusa-cam.hotspot_ctl')


def _status(controller, ifname, runner):
    """Best-effort ``controller.status``; ``None`` when it fails or is absent."""
    try:
        return controller.status(ifname=ifname, runner=runner)
    except Exception as e:  # noqa: BLE001 - an optional probe must never raise
        log.warning(f'hotspot_ctl: status failed: {type(e).__name__}')
        return None


def start(controller=None, *, device_id=None, ifname=hotspot.DEFAULT_IFNAME,
          runner=None):
    """Start the setup AP, idempotently.

    Returns a :class:`hotspot.HotspotResult`. When the AP is already active with
    the derived SSID the operation is reported as ok without touching the
    interface, which makes the unit's ``ExecStartPre`` safe across restarts.
    Fails (with a non-secret reason) when no SSID can be derived.
    """
    controller = controller or hotspot
    if device_id is None:
        device_id = provisioning.resolve_device_id()
    ssid = provisioning.setup_ssid(device_id)
    if not ssid:
        return hotspot.HotspotResult(
            False, 'setup SSID unavailable (no device id)', ifname=ifname
        )

    existing = _status(controller, ifname, runner)
    if existing is not None and getattr(existing, 'ok', False) \
            and getattr(existing, 'active', False):
        if getattr(existing, 'ssid', '') == ssid:
            return hotspot.HotspotResult(
                True, '', active=True, ssid=ssid, ifname=ifname
            )
        # A different AP is active (e.g. a stale profile): take it down first.
        try:
            controller.stop(ifname=ifname, runner=runner)
        except Exception as e:  # noqa: BLE001 - proceed to start regardless
            log.warning(f'hotspot_ctl: stale AP stop failed: {type(e).__name__}')

    try:
        # The setup AP is open by design — no PSK is passed (source §4.3).
        result = controller.start(ssid, ifname=ifname, runner=runner)
    except Exception as e:  # noqa: BLE001 - the CLI must never raise
        log.warning(f'hotspot_ctl: start failed: {type(e).__name__}')
        return hotspot.HotspotResult(False, 'hotspot start failed', ifname=ifname)
    if result is None:
        return hotspot.HotspotResult(False, 'hotspot start failed', ifname=ifname)
    return result


def stop(controller=None, *, ifname=hotspot.DEFAULT_IFNAME, runner=None):
    """Stop the setup AP (``hotspot.stop``); a failure is reported, never raised."""
    controller = controller or hotspot
    try:
        result = controller.stop(ifname=ifname, runner=runner)
    except Exception as e:  # noqa: BLE001 - the CLI must never raise
        log.warning(f'hotspot_ctl: stop failed: {type(e).__name__}')
        return hotspot.HotspotResult(False, 'hotspot stop failed', ifname=ifname)
    if result is None:
        return hotspot.HotspotResult(False, 'hotspot stop failed', ifname=ifname)
    return result


def main(argv=None):
    """CLI entry point: ``start`` or ``stop``; exit non-zero on failure."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(description='Buddy3D setup hotspot control')
    parser.add_argument('action', choices=('start', 'stop'))
    args = parser.parse_args(argv)

    result = start() if args.action == 'start' else stop()
    if not getattr(result, 'ok', False):
        reason = getattr(result, 'reason', '') or 'unknown'
        print(f'hotspot_ctl: {args.action} failed: {reason}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
