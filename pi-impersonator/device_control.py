"""Host-testable device-control policy for the Pi impersonator.

Covers the two device-policy gaps:

* ``GAP-DEVICE-01`` — rate-limited, narrowly scoped reboot requested through the
  authenticated trigger dispatcher.
* ``GAP-DEVICE-02`` — truthful IR/speaker/fan/MicroSD handling: the Pi has none
  of that hardware, so no control path may claim a mode was applied.

Everything here is pure stdlib and takes its hardware action as an injected
callable, so the policy can be unit-tested without systemd, ``aiohttp``, or a
real reboot.
"""
import logging
import time

log = logging.getLogger('prusa-cam.device')

# GAP-DEVICE-01: minimum spacing between accepted reboot requests. The trigger
# dispatcher is the only caller, but a burst of triggers (or a retrying client)
# must not reboot-loop the Pi. 60 s is long enough for the Pi to drop the
# Socket.IO connection and come back; a request inside the window is rejected
# without invoking systemd.
DEFAULT_REBOOT_MIN_INTERVAL_SECONDS = 60

# Recovered ``configuration.light_control`` values (FW-CONFIG:193-228):
# auto -> mode 1, day -> mode 2, night -> mode 3. The Pi cannot apply any of
# them because it has no IR illuminator.
LIGHT_CONTROL_MODES = {'auto': 1, 'day': 2, 'night': 3}


def can_reboot(last_reboot_monotonic, now_monotonic, min_interval):
    """Pure reboot rate-limit predicate.

    ``last_reboot_monotonic`` is ``None`` before the first accepted request. The
    window boundary is inclusive: exactly ``min_interval`` seconds later is
    allowed. A non-monotonic (backwards) clock fails closed.
    """
    if last_reboot_monotonic is None:
        return True
    if now_monotonic < last_reboot_monotonic:
        return False
    return (now_monotonic - last_reboot_monotonic) >= min_interval


def request_reboot(state, reboot_fn, now=None):
    """Rate-limit and perform one reboot request.

    The accepted time is recorded on the shared ``state`` *before* invoking
    ``reboot_fn`` so a burst cannot pass the guard. ``reboot_fn`` is a
    zero-argument callable returning truthy on success; a false return or a
    raised exception is logged and reported as ``False`` without crashing.
    """
    now = time.monotonic() if now is None else now
    last = getattr(state, 'last_reboot_monotonic', None)
    if not can_reboot(last, now, DEFAULT_REBOOT_MIN_INTERVAL_SECONDS):
        elapsed = now - last
        remaining = max(0.0, DEFAULT_REBOOT_MIN_INTERVAL_SECONDS - elapsed)
        log.warning(
            f'reboot rejected by rate limit: last accepted {elapsed:.1f}s ago, '
            f'{remaining:.1f}s remaining'
        )
        return False
    state.last_reboot_monotonic = now
    try:
        result = reboot_fn()
    except Exception as e:
        log.error(f'reboot command raised: {e}')
        return False
    if not result:
        log.error('reboot command did not report success')
        return False
    log.warning('reboot command accepted; device is rebooting')
    return True


def light_control_mode(value):
    """Map a ``configuration.light_control`` string to its recovered mode, or None."""
    if not isinstance(value, str):
        return None
    return LIGHT_CONTROL_MODES.get(value.strip().lower())


def apply_light_control(value, state):
    """Handle ``configuration.light_control`` truthfully (GAP-DEVICE-02).

    Firmware applies day/auto/night where an IR illuminator exists. The Pi has
    none, so a recognized mode is logged and rejected (returns ``False``) and
    ``state.ir_mode`` is left ``None`` (unavailable): no surface may report a
    mode that was never applied. Unrecognized values are also rejected.
    """
    mode = light_control_mode(value)
    if mode is None:
        log.warning(f'light_control: unrecognized value {value!r}; no IR hardware')
        return False
    if getattr(state, 'ir_available', False):
        # Hardware that does expose IR would apply the recovered mode here.
        state.ir_mode = mode
        return True
    log.warning(
        f'light_control: {value!r} (mode {mode}) not applied; '
        f'Pi has no IR illuminator'
    )
    return False
