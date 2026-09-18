"""Pure WebRTC configured-mode / runtime-status control (GAP-WEBRTC-04).

Firmware ``FUN_000b94ac`` (``set_webrtc_mode``) keeps the configured mode
(``+0x13d``) and the runtime service status (``+0x13e``) as separate bytes, and
``FUN_000b996c`` rejects an inbound offer only when both are zero. This module
holds that state machine with the GLib/GStreamer service calls injected, so it
can be unit-tested without ``gi``/GStreamer.

The recovered control flow (``FW-WEBRTC-MODE``)::

    if runtime_status == 0 and requested != 0: start service
    elif runtime_status != 0 and requested == 0: stop service
    mode = requested
"""
import logging

from proto import decode_message

log = logging.getLogger('prusa-cam.webrtc')

WEBRTC_DISABLED = 0
WEBRTC_ENABLED = 1


def decode_mode(data):
    """Decode ``SetWebRtcMode`` field 1 (``uint32 mode``).

    Returns 0/1 for a valid payload and ``None`` otherwise. The previous
    ``data[0]`` read returned the protobuf tag byte (``0x08``) instead of the
    value, so a disable/enable payload was never recognized.
    """
    if not isinstance(data, (bytes, bytearray)):
        return None
    try:
        fields = decode_message(bytes(data))
    except Exception:
        return None
    value = fields.get(1)
    if type(value) is not int or value not in (WEBRTC_DISABLED, WEBRTC_ENABLED):
        return None
    return value


def offer_allowed(state):
    """Firmware ``FUN_000b996c`` gate: reject only when mode==0 and status==0."""
    return not (state.webrtc_mode == WEBRTC_DISABLED
                and state.webrtc_status == WEBRTC_DISABLED)


def apply_mode(requested, state, start_service=None, stop_service=None):
    """Apply the recovered ``FUN_000b94ac`` enable/disable state machine.

    ``start_service``/``stop_service`` are zero-argument Pi callables (the
    existing ``PrusaWebRTC.start``/``stop``). Returns True when a valid mode was
    applied; a failing service call aborts the transition so the shared state
    never claims a service that failed to move.
    """
    if type(requested) is not int or requested not in (WEBRTC_DISABLED, WEBRTC_ENABLED):
        log.warning(f'WebRTC mode: invalid requested value {requested!r}')
        return False
    if state.webrtc_status == WEBRTC_DISABLED and requested != WEBRTC_DISABLED:
        if start_service is not None:
            try:
                start_service()
            except Exception as e:
                log.error(f'WebRTC mode: start service failed: {e}')
                return False
        state.webrtc_status = WEBRTC_ENABLED
    elif state.webrtc_status != WEBRTC_DISABLED and requested == WEBRTC_DISABLED:
        if stop_service is not None:
            try:
                stop_service()
            except Exception as e:
                log.error(f'WebRTC mode: stop service failed: {e}')
                return False
        state.webrtc_status = WEBRTC_DISABLED
    state.webrtc_mode = requested
    state.mark_info_dirty()
    return True
