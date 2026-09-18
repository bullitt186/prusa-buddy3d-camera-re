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
    """Decode the flat WebRTC envelope used between Connect and the camera.

    This is deliberately different from the nested viewer-side WebRtcSignal
    protobuf used by the web client. The signaling service translates between
    the two schemas before delivering an event to the camera.
    """
    fields = decode_message(data)
    return {
        'request_id': fields.get(1, ''),
        'msg_type': fields.get(2, 0),
        'client_id': fields.get(3, ''),
        'payload': fields.get(4, ''),
        'transport_policy': fields.get(5, 0),
        'ttl': fields.get(6, 0),
        'video_cfg': fields.get(7, 0),
        'plan': fields.get(8, 0),
        'quality': fields.get(9, ''),
        'fps': fields.get(10, 0),
        'scope': fields.get(12, 0),
        'raw': fields,
    }


def encode_camera_webrtc_message(request_id, msg_type, payload):
    """Encode an answer/candidate using the firmware's outbound field layout."""
    if msg_type not in (WEBRTC_ANSWER, WEBRTC_CANDIDATE):
        raise ValueError(f'unsupported outbound WebRTC message type: {msg_type}')
    if not isinstance(request_id, str) or not request_id:
        raise ValueError('request_id must be a non-empty string')
    if not isinstance(payload, str) or not payload:
        raise ValueError('payload must be a non-empty string')
    return encode_message({1: request_id, 2: msg_type, 3: payload})
