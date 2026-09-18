"""Typed inbound trigger dispatcher (GAP-TRIGGER-01, GAP-SNAPSHOT-02).

Firmware 3.1.6 decodes the Socket.IO ``trigger`` payload as a protobuf message in
dispatcher ``FUN_000a963c`` (descriptor ``0x3f6f14``) and then performs *only* the
requested action(s). This module owns the pure decode + action-plan logic; the
Pi-specific execution (Socket.IO sends, camera capture, systemd) stays in
``main.py``. It is stdlib-only (``proto`` only) so it can be unit-tested without
``socketio``/``aiohttp``/``gi``/GStreamer.

Recovered schema (tag, type, meaning)::

    1  uvarint  Get status
    2  uvarint  Get features
    3  uvarint  Get snapshot
    4  uvarint  snapshot upload: 1=enable, 2=disable
    5  uvarint  timelapse: 1=enable, 2=disable
    8  uvarint  start firmware update
    9  uvarint  reboot device
    10 uvarint  RTSP: 1=start, 2=stop
    11 string   request_id / correlation
    12 uvarint  Get protocol information -> send protobuf_version
    13 string   second string (semantics unresolved; decode/log only)
    14 uvarint  timelapse make video: 1=make, 2=unsupported
    15 uvarint  timelapse file list: 1=get, 2=unsupported

Only an exact recovered ``(tag, value)`` pair produces an action; any other
value is ignored and logged rather than acted on. The value checks are direct
firmware evidence: dispatcher ``FUN_000a963c`` tests ``iStack_xx == 1`` (and
``== 2`` for tags 4, 5, 10, 14, 15) per field. The processing *order* for a
multi-field trigger is not defined by the decompile; ascending tag order is an
implementation assumption (each field is checked independently in firmware).
Do not infer additional numeric values.
"""
import logging

from proto import decode_message

log = logging.getLogger('prusa-cam.trigger')

# Action identifiers returned by ``trigger_actions`` and consumed by main.py.
STATUS = 'status'
FEATURES = 'features'
PROTOCOL_INFO = 'protocol_info'
SNAPSHOT = 'snapshot'
SNAPSHOT_ENABLE = 'snapshot_enable'
SNAPSHOT_DISABLE = 'snapshot_disable'
RTSP_START = 'rtsp_start'
RTSP_STOP = 'rtsp_stop'
TIMELAPSE_ENABLE = 'timelapse_enable'
TIMELAPSE_DISABLE = 'timelapse_disable'
FW_UPDATE = 'fw_update'
REBOOT = 'reboot'
TIMELAPSE_MAKE = 'timelapse_make'
TIMELAPSE_FILE_LIST = 'timelapse_file_list'

# Exact recovered (tag, value) -> action mapping. A field present with any other
# value produces no action.
_ACTION_BY_FIELD_VALUE = {
    (1, 1): STATUS,
    (2, 1): FEATURES,
    (3, 1): SNAPSHOT,
    (4, 1): SNAPSHOT_ENABLE,
    (4, 2): SNAPSHOT_DISABLE,
    (5, 1): TIMELAPSE_ENABLE,
    (5, 2): TIMELAPSE_DISABLE,
    (8, 1): FW_UPDATE,
    (9, 1): REBOOT,
    (10, 1): RTSP_START,
    (10, 2): RTSP_STOP,
    (12, 1): PROTOCOL_INFO,
    (14, 1): TIMELAPSE_MAKE,
    (15, 1): TIMELAPSE_FILE_LIST,
}

# Values the firmware documents as the "unsupported" branch of a control field;
# they are recognized (and logged) but deliberately produce no action.
_UNSUPPORTED_BY_FIELD_VALUE = {
    (14, 2): TIMELAPSE_MAKE,
    (15, 2): TIMELAPSE_FILE_LIST,
}

# Tag 11 (request_id) and tag 13 (second string) are string metadata, not actions.
_METADATA_TAGS = {11, 13}
_DOCUMENTED_TAGS = {tag for tag, _ in _ACTION_BY_FIELD_VALUE} | _METADATA_TAGS


def normalize_string(value):
    """Normalize a recovered string field to ``str`` (``''`` when absent).

    ``proto.decode_message`` returns ``str`` for valid UTF-8 and ``bytes`` for a
    non-UTF-8 payload; both forms must reach callers as text. Anything else
    (absent field, wrong type) yields an empty string.
    """
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    if isinstance(value, str):
        return value
    return ''


class TriggerMessage(dict):
    """Decoded trigger payload: raw values keyed by tag, plus accessors.

    Subclasses ``dict`` so callers can use ordinary field access
    (``decoded.get(4)``), while ``request_id``/``secondary_string`` expose the
    two recovered string fields as normalized text.
    """

    @property
    def request_id(self):
        """Tag 11 correlation value as text (``''`` when absent)."""
        return normalize_string(self.get(11))

    @property
    def secondary_string(self):
        """Tag 13 second string as text (semantics unresolved; log only)."""
        return normalize_string(self.get(13))


def decode_trigger(data):
    """Decode an inbound trigger payload into a :class:`TriggerMessage`.

    Never raises on malformed input: an empty mapping is returned so a bad frame
    cannot take down the Socket.IO handler.
    """
    if not isinstance(data, (bytes, bytearray)):
        log.warning(f'trigger: ignoring non-bytes payload ({type(data).__name__})')
        return TriggerMessage()
    try:
        fields = decode_message(bytes(data))
    except Exception as e:  # decode_message is defensive, but never trust input
        log.warning(f'trigger: decode failed: {e}')
        return TriggerMessage()
    return TriggerMessage(fields)


def trigger_actions(decoded):
    """Return the ordered action identifiers for a decoded trigger.

    Only the exact documented ``(tag, value)`` pairs produce an action; a
    present field with any other value (or an undocumented tag) is logged and
    ignored. The plan is ordered by ascending tag number so a multi-field trigger
    has a deterministic order.
    """
    fields = decoded if isinstance(decoded, dict) else {}
    actions = []
    for tag in sorted(fields):
        value = fields[tag]
        action = _ACTION_BY_FIELD_VALUE.get((tag, value))
        if action is not None:
            actions.append(action)
        elif tag in _METADATA_TAGS:
            continue
        elif (tag, value) in _UNSUPPORTED_BY_FIELD_VALUE:
            log.warning(
                f'trigger: {_UNSUPPORTED_BY_FIELD_VALUE[(tag, value)]} is the '
                f'documented unsupported branch (tag={tag} value={value})'
            )
        elif tag in _DOCUMENTED_TAGS:
            log.warning(f'trigger: ignoring undocumented value tag={tag} value={value!r}')
        else:
            log.warning(f'trigger: ignoring unknown field tag={tag} value={value!r}')
    return actions


def apply_snapshot_upload(action, state):
    """Apply a snapshot-upload control action to shared state (GAP-SNAPSHOT-02).

    Returns True only for the two snapshot control actions. The periodic uploader
    reads ``state.snapshot_upload_enabled``; immediate get-snapshot requests do
    not, so this flag never blocks a requested capture.
    """
    if action == SNAPSHOT_ENABLE:
        state.snapshot_upload_enabled = True
        return True
    if action == SNAPSHOT_DISABLE:
        state.snapshot_upload_enabled = False
        return True
    return False
