"""Pure RTSP configured-mode / runtime-state control (GAP-RTSP-02).

Firmware ``FW-RTSP-INIT`` loads the configured RTSP mode (``1`` = disabled,
``2`` = enabled), starts/stops the server, and reports the runtime service state
separately. The direct ``set_rtsp_server_mode`` event and the ``configuration``
``rtsp`` field dispatch to the same path. This module holds that logic with the
systemd calls injected, so it can be unit-tested without the Pi.

Persistence
-----------
The configured mode is written to ``/etc/prusa-cam/rtsp.mode`` (override with
``PRUSA_RTSP_MODE_FILE``). On the production Pi the root filesystem is a
read-only overlay, so a runtime write is visible for the running session but is
discarded on the next power cycle unless it reaches the lower filesystem via
``deploy.sh``. ``main`` therefore reads the file at startup and keeps the
in-memory mode authoritative; deploying the file makes the choice durable.
"""
import logging
import os

from proto import decode_message

log = logging.getLogger('prusa-cam.rtsp')

RTSP_DISABLED = 1
RTSP_ENABLED = 2

# The shipped ``prusa-rtsp.service`` is enabled at boot and firmware's shipped
# default mode is not recovered, so default to enabled to preserve the current
# Pi behavior until a config image pins the firmware value.
DEFAULT_RTSP_MODE = RTSP_ENABLED

RTSP_MODE_FILE = os.environ.get('PRUSA_RTSP_MODE_FILE', '/etc/prusa-cam/rtsp.mode')


def mode_from_config(value):
    """Map the ``configuration`` ``rtsp`` value to a mode, or ``None``.

    Accepts the string form (``"on"``/``"off"``) and the numeric form
    (``2``/``1``) so both command shapes reach the same transition.
    """
    if isinstance(value, str):
        return {'on': RTSP_ENABLED, 'off': RTSP_DISABLED}.get(value.strip().lower())
    if type(value) is int and value in (RTSP_DISABLED, RTSP_ENABLED):
        return value
    return None


def decode_mode(data):
    """Decode ``SetRtspServerMode`` field 1 (``uint32 mode`` = 1/2)."""
    if not isinstance(data, (bytes, bytearray)):
        return None
    try:
        fields = decode_message(bytes(data))
    except Exception:
        return None
    value = fields.get(1)
    if type(value) is not int or value not in (RTSP_DISABLED, RTSP_ENABLED):
        return None
    return value


def read_mode(path=None):
    """Read the persisted configured mode; fall back to ``DEFAULT_RTSP_MODE``."""
    path = path or RTSP_MODE_FILE
    try:
        with open(path) as f:
            raw = f.read().strip()
    except OSError:
        return DEFAULT_RTSP_MODE
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log.warning(f'RTSP mode: ignoring malformed persisted value {raw!r}')
        return DEFAULT_RTSP_MODE
    if value not in (RTSP_DISABLED, RTSP_ENABLED):
        log.warning(f'RTSP mode: ignoring out-of-range persisted value {value!r}')
        return DEFAULT_RTSP_MODE
    return value


def write_mode(mode, path=None):
    """Persist the configured mode atomically. Returns True on success."""
    if mode not in (RTSP_DISABLED, RTSP_ENABLED):
        return False
    path = path or RTSP_MODE_FILE
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            f.write(str(mode) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        log.warning(f'RTSP mode: could not persist to {path}: {e}')
        return False
    return True


def runtime_after_command(mode, query_service=None):
    """Resolve ``rtsp_running`` from the queried service, else the commanded mode.

    A query that raises or returns ``None`` (non-Pi, unit absent) falls back to
    the commanded state so a failed probe cannot make status claim the opposite.
    """
    if query_service is not None:
        try:
            active = query_service()
        except Exception as e:
            log.warning(f'RTSP mode: service state query failed: {e}')
            active = None
        if active is not None:
            return bool(active)
    return mode == RTSP_ENABLED


def apply_mode(mode, state, start_service=None, stop_service=None,
               query_service=None, persist=None):
    """Apply a configured RTSP mode through the shared direct/config path.

    Sets ``state.rtsp_mode``, starts/stops ``prusa-rtsp.service``, optionally
    persists the mode, and sets ``state.rtsp_running`` from the actual (or
    commanded) service state.
    """
    if type(mode) is not int or mode not in (RTSP_DISABLED, RTSP_ENABLED):
        log.warning(f'RTSP mode: invalid value {mode!r}')
        return False
    state.rtsp_mode = mode
    action = start_service if mode == RTSP_ENABLED else stop_service
    if action is not None:
        try:
            action()
        except Exception as e:
            log.error(f'RTSP mode: service command failed: {e}')
    if persist is not None:
        try:
            persist(mode)
        except Exception as e:
            log.warning(f'RTSP mode: persist callback failed: {e}')
    state.rtsp_running = runtime_after_command(mode, query_service)
    state.mark_info_dirty()
    return True
