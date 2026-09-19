import struct

# Camera-side WebRTC message enum recovered from lp_app 3.1.6
# (translateWebRtcMsgTypeUintToString / SendWebRTCMessage).
WEBRTC_REQUEST = 1
WEBRTC_ANSWER = 2
WEBRTC_OFFER = 3
WEBRTC_CANDIDATE = 4

class Float32:
    def __init__(self, value):
        self.value = float(value)

def encode_varint(value):
    result = b''
    while value > 127:
        result += bytes([(value & 0x7F) | 0x80])
        value >>= 7
    result += bytes([value & 0x7F])
    return result

def decode_varint(data, offset=0):
    result = 0
    shift = 0
    while offset < len(data):
        b = data[offset]
        result |= (b & 0x7F) << shift
        offset += 1
        if not (b & 0x80):
            break
        shift += 7
    return result, offset

def encode_string_field(field_number, value):
    tag = (field_number << 3) | 2
    encoded = value.encode('utf-8')
    return encode_varint(tag) + encode_varint(len(encoded)) + encoded

def encode_uint_field(field_number, value):
    tag = (field_number << 3) | 0
    return encode_varint(tag) + encode_varint(value)

def encode_float_field(field_number, value):
    tag = (field_number << 3) | 5
    return encode_varint(tag) + struct.pack('<f', value)

def encode_message(fields):
    result = b''
    for field_num, value in sorted(fields.items()):
        if value is None:
            continue
        if isinstance(value, int):
            result += encode_uint_field(field_num, value)
        elif isinstance(value, Float32):
            result += encode_float_field(field_num, value.value)
        elif isinstance(value, str):
            result += encode_string_field(field_num, value)
        elif isinstance(value, bytes):
            tag = (field_num << 3) | 2
            result += encode_varint(tag) + encode_varint(len(value)) + value
    return result

def decode_message(data):
    """Decode protobuf into {field_number: value}. Strings and bytes as wire type 2."""
    fields = {}
    offset = 0
    while offset < len(data):
        tag, offset = decode_varint(data, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:  # varint
            value, offset = decode_varint(data, offset)
            fields[field_number] = value
        elif wire_type == 2:  # length-delimited
            length, offset = decode_varint(data, offset)
            raw = data[offset:offset + length]
            try:
                fields[field_number] = raw.decode('utf-8')
            except UnicodeDecodeError:
                fields[field_number] = raw
            offset += length
        elif wire_type == 5:  # 32-bit
            fields[field_number] = data[offset:offset + 4]
            offset += 4
        elif wire_type == 1:  # 64-bit
            fields[field_number] = data[offset:offset + 8]
            offset += 8
        else:
            break
    return fields


def decode_camera_webrtc_message(data):
    """Decode the recovered 9-field camera-side WebRTC message.

    Descriptor 0x3f7680 (handler FUN_000a4a78), confirmed against a live Connect
    message:
        tag1 string  request_id / token
        tag2 string  client_id
        tag3 string  session_id
        tag4 submsg  (2 strings)
        tag5 uvarint
        tag6 uvarint
        tag7 uvarint
        tag8 submsg  ICE server configuration (repeated {id, host, port, type})
        tag9 submsg  (5 uvarints)
    """
    fields = decode_message(data)
    return {
        'request_id': fields.get(1, ''),
        'client_id': fields.get(2, ''),
        'session_id': fields.get(3, ''),
        'field4': fields.get(4, ''),
        'field5': fields.get(5, 0),
        'field6': fields.get(6, 0),
        'field7': fields.get(7, 0),
        'ice_config': fields.get(8, b''),
        'field9': fields.get(9, b''),
        'raw': fields,
    }


def _iter_fields(data):
    """Yield (field, wire, value) for a protobuf byte string.

    ``value`` is bytes for wire type 2 and an int for wire type 0. Needed because
    ``decode_message`` collapses repeated fields (last wins), which loses the
    ICE server list.
    """
    offset = 0
    while offset < len(data):
        tag, offset = decode_varint(data, offset)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            value, offset = decode_varint(data, offset)
            yield field, wire, value
        elif wire == 2:
            length, offset = decode_varint(data, offset)
            yield field, wire, data[offset:offset + length]
            offset += length
        elif wire == 5:
            yield field, wire, data[offset:offset + 4]
            offset += 4
        elif wire == 1:
            yield field, wire, data[offset:offset + 8]
            offset += 8
        else:
            return


def decode_ice_config(data):
    """Parse the tag8 ICE-config blob into (servers, username, credential).

    Live structure (FUN_000bc0ec): tag8 = {1: <blob>}; the blob is a repeated
    ``0x0a <len>`` sequence whose elements are either a plain ICE server
    ``{1: id, 2: host, 3: port, 4: type}`` or a TURN block
    ``{1: <repeated servers>, 2: username, 3: credential, ...}``.
    type: 1 = STUN, 2 = TURN, 3 = TURNS.
    """
    servers = []
    username = ''
    credential = ''

    def walk(blob):
        nonlocal username, credential
        for _field, wire, value in _iter_fields(blob):
            if wire != 2:
                continue
            text = None
            try:
                text = value.decode('utf-8')
            except Exception:
                pass
            # TURN time-limited username "timestamp:user" or base64 credential.
            if text and ':' in text and text.replace(':', '').isdigit():
                username = text
                continue
            if text and len(text) >= 16 and text.endswith('=') and ' ' not in text:
                credential = text
                continue
            try:
                inner = decode_message(value)
            except Exception:
                inner = None
            if (isinstance(inner, dict) and isinstance(inner.get(2), str)
                    and isinstance(inner.get(3), int) and isinstance(inner.get(4), int)):
                servers.append({
                    'id': inner.get(1, 0), 'host': inner[2],
                    'port': inner[3], 'type': inner[4],
                })
            else:
                walk(value)

    walk(bytes(data) if isinstance(data, (bytes, bytearray)) else data)
    return servers, username, credential


def decode_ice_servers(data):
    """Return just the ICE server list from the tag8 blob (compat helper)."""
    return decode_ice_config(data)[0]


def encode_camera_webrtc_message(token, request_id, fingerprint, msg_type,
                                 sdp='', candidate='', mid=''):
    """Encode a camera-side WebRTC message (recovered 9-field schema 0x3f7680).

    Recovered from ``FUN_000a3e90``: tag1 = token, tag2 = request_id,
    tag3 = fingerprint, tag4 = {tag4.1, tag4.2}, tag5 = type
    (1 request / 2 answer / 3 offer / 4 candidate), tag7 = 1.

    For an offer/answer tag4.1 = SDP. For a candidate the firmware's
    ``FUN_000b75e0`` logs "generated local candidate: %s, mid: %s", so
    tag4.1 = candidate and tag4.2 = mid (m-line id).
    """
    if msg_type not in (WEBRTC_REQUEST, WEBRTC_ANSWER, WEBRTC_OFFER, WEBRTC_CANDIDATE):
        raise ValueError(f'unsupported outbound WebRTC message type: {msg_type}')
    if not isinstance(request_id, str) or not request_id:
        raise ValueError('request_id must be a non-empty string')
    fields = {1: token, 2: request_id, 3: fingerprint, 5: msg_type, 7: 1}
    if sdp:
        fields[4] = encode_message({1: sdp, 2: ''})
    elif candidate:
        fields[4] = encode_message({1: candidate, 2: mid})
    return encode_message(fields)
