"""GAP-WEBRTC-03/06: WebRTC session lifecycle + connection-info policy.

This module holds the pure state mapping used by ``webrtc.py`` so the teardown
policy and the ``webrtc_connection_info`` payload can be unit-tested without
importing PyGObject/GStreamer.

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


# --- GAP-WEBRTC-06: webrtc_connection_info -----------------------------------
#
# Recovered 3.1.6 candidate-type translator ``FUN_000b5098``: input 1 -> 1 HOST,
# 2 -> 2 SERVER_REFLEXIVE, 3 -> 3 PEER_REFLEXIVE, 4 -> 4 RELAYED, 0 -> 5
# UNDEFINED, anything else -> 0 UNKNOWN. The ICE candidate ``typ`` token maps to
# that same code space. When no candidate pair is selected the sender forces
# both types to 6. All values are wire bytes (field 2/3), not strings.
CANDIDATE_TYPE_UNKNOWN = 0
CANDIDATE_TYPE_HOST = 1
CANDIDATE_TYPE_SERVER_REFLEXIVE = 2
CANDIDATE_TYPE_PEER_REFLEXIVE = 3
CANDIDATE_TYPE_RELAYED = 4
CANDIDATE_TYPE_UNDEFINED = 5
CANDIDATE_TYPE_NO_PAIR = 6

_CANDIDATE_TYPE_CODES = {
    'host': CANDIDATE_TYPE_HOST,
    'srflx': CANDIDATE_TYPE_SERVER_REFLEXIVE,
    'prflx': CANDIDATE_TYPE_PEER_REFLEXIVE,
    'relay': CANDIDATE_TYPE_RELAYED,
    'undefined': CANDIDATE_TYPE_UNDEFINED,
    'unknown': CANDIDATE_TYPE_UNKNOWN,
}


def candidate_type_code(typ_text):
    """Map an ICE candidate ``typ`` token to the firmware wire code.

    ``typ_text`` is the SDP candidate type (``host``/``srflx``/``prflx``/
    ``relay``) or ``None``/an unrecognized value. Unknown and ``None`` both map
    to ``0`` (UNKNOWN); the no-pair case is a separate caller decision
    (``CANDIDATE_TYPE_NO_PAIR`` = 6).
    """
    if typ_text is None:
        return CANDIDATE_TYPE_UNKNOWN
    return _CANDIDATE_TYPE_CODES.get(str(typ_text).strip().lower(),
                                     CANDIDATE_TYPE_UNKNOWN)


def parse_candidate_type(candidate_text):
    """Extract the ``typ`` token from an SDP candidate line or value.

    Accepts the full ``a=candidate:...`` attribute or the bare value and returns
    the lowercased type (``host``/``srflx``/``prflx``/``relay``), or ``None``
    when the line carries no recognizable ``typ`` token.
    """
    if not candidate_text or not isinstance(candidate_text, str):
        return None
    parts = candidate_text.replace('\r', ' ').replace('\n', ' ').split()
    for index, part in enumerate(parts):
        if part.lower() == 'typ' and index + 1 < len(parts):
            token = parts[index + 1].strip().lower()
            return token or None
    return None


def connection_info_payload(client_id, local_code, remote_code):
    """Build the recovered 3.1.6 ``WebRtcConnectionType`` field map.

    Sender ``FUN_000be3f8`` -> encoder ``FUN_000be050``/``FUN_000bdd3c``:
    field 1 = client id string, field 2 = local candidate type byte, field 3 =
    remote candidate type byte. Fields 4/5/6 are never populated by that sender
    and their semantics are ``[assumption]``/unknown, so they are omitted.
    """
    return {
        1: client_id if isinstance(client_id, str) else '',
        2: local_code,
        3: remote_code,
    }
