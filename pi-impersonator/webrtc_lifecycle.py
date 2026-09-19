"""GAP-WEBRTC-03: WebRTC session lifecycle policy (stdlib-only, host-testable).

This module holds the pure state mapping used by ``webrtc.py`` so the teardown
policy can be unit-tested without importing PyGObject/GStreamer.

``GstWebRTC.WebRTCICEConnectionState`` values, in GStreamer's declared order:

    NEW = 0
    CHECKING = 1
    CONNECTED = 2
    COMPLETED = 3
    FAILED = 4
    DISCONNECTED = 5
    CLOSED = 6
"""

ICE_NEW = 0
ICE_CHECKING = 1
ICE_CONNECTED_STATE = 2
ICE_COMPLETED = 3
ICE_FAILED = 4
ICE_DISCONNECTED = 5
ICE_CLOSED = 6

# States that mean media can flow.
ICE_CONNECTED = frozenset({ICE_CONNECTED_STATE, ICE_COMPLETED})
# States that are terminal for the peer connection.
ICE_ENDED = frozenset({ICE_FAILED, ICE_CLOSED})


def end_reason(ice_state):
    """Map an ICE connection state to its terminal reason, else ``None``.

    DISCONNECTED is not terminal on its own — it is a grace period that the
    caller resolves with a delayed re-check — so it maps to its own reason and
    the caller decides whether to schedule or act.
    """
    if ice_state == ICE_FAILED:
        return 'ice-failed'
    if ice_state == ICE_CLOSED:
        return 'ice-closed'
    if ice_state == ICE_DISCONNECTED:
        return 'ice-disconnected'
    return None
