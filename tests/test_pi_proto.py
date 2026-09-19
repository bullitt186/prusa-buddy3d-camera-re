import sys
import unittest
from pathlib import Path


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

from proto import (  # noqa: E402
    WEBRTC_ANSWER,
    WEBRTC_CANDIDATE,
    WEBRTC_OFFER,
    decode_camera_webrtc_message,
    decode_ice_servers,
    decode_message,
    encode_camera_webrtc_message,
    encode_message,
    find_webrtc_candidate,
)


class CameraWebRtcProtocolTests(unittest.TestCase):
    def test_decodes_recovered_9_field_inbound_layout(self):
        # Recovered 9-field schema (descriptor 0x3f7680), matched to a live
        # Connect message: {1: token, 2: client_id, 3: session_id, 5: 1, 7: 2,
        # 8: <ICE servers>, 9: <uvarints>}.
        entry = encode_message({1: 1, 2: 'stun.l.google.com', 3: 19302, 4: 1})
        blob = encode_message({1: entry})        # tag8.field1 repeated entry
        ice_config = encode_message({1: blob})   # tag8 submessage
        wire = encode_message({
            1: 'request-12345678',
            2: 'viewer-client-id',
            3: 'session-id',
            5: 1,
            7: 2,
            8: ice_config,
            9: encode_message({1: 1}),
        })

        decoded = decode_camera_webrtc_message(wire)

        self.assertEqual(decoded['request_id'], 'request-12345678')
        self.assertEqual(decoded['client_id'], 'viewer-client-id')
        self.assertEqual(decoded['session_id'], 'session-id')
        self.assertEqual(decoded['field5'], 1)
        self.assertEqual(decoded['field7'], 2)
        self.assertEqual(
            decode_ice_servers(decoded['ice_config']),
            [{'id': 1, 'host': 'stun.l.google.com', 'port': 19302, 'type': 1}],
        )

    def test_encodes_offer_with_recovered_9_field_layout(self):
        # Recovered outbound layout (FUN_000a3e90): tag1 token, tag2 request_id,
        # tag3 fingerprint, tag4.1 SDP, tag5 type (3=offer), tag7=1.
        wire = encode_camera_webrtc_message(
            'tok', 'request-12345678', 'fp', WEBRTC_OFFER, sdp='v=0\r\n'
        )
        fields = decode_message(wire)
        self.assertEqual(fields[1], 'tok')
        self.assertEqual(fields[2], 'request-12345678')
        self.assertEqual(fields[3], 'fp')
        self.assertEqual(fields[5], WEBRTC_OFFER)
        self.assertEqual(fields[7], 1)
        nested = fields[4].encode('utf-8') if isinstance(fields[4], str) else fields[4]
        self.assertEqual(decode_message(nested)[1], 'v=0\r\n')

    def test_encodes_candidate_in_tag4(self):
        # Firmware FUN_000b75e0: tag4.1 = candidate, tag4.2 = mid.
        wire = encode_camera_webrtc_message(
            'tok', 'request-12345678', 'fp', WEBRTC_CANDIDATE,
            candidate='candidate:1 1 UDP 1 10.0.0.1 9 typ host', mid='0',
        )
        fields = decode_message(wire)
        self.assertEqual(fields[5], WEBRTC_CANDIDATE)
        nested = fields[4].encode('utf-8') if isinstance(fields[4], str) else fields[4]
        inner = decode_message(nested)
        self.assertIn('candidate:1', inner[1])
        self.assertEqual(inner[2], '0')

    def test_rejects_non_firmware_outbound_type(self):
        with self.assertRaises(ValueError):
            encode_camera_webrtc_message('tok', 'request-12345678', 'fp', 99, sdp='v=0')


CANDIDATE = 'a=candidate:1 1 udp 2130706431 1.2.3.4 5000 typ host'


def _candidate_envelope():
    """Viewer trickle-ICE message: tag4 = {1: candidate, 2: mid} in the 9-field shape."""
    tag4 = encode_message({1: CANDIDATE, 2: 'mid0'})
    return encode_message({
        1: 'token-12345678',
        2: 'viewer-client-id',
        3: 'session-id',
        4: tag4,
        5: WEBRTC_CANDIDATE,
        7: 2,
    })


class FindWebRtcCandidateTests(unittest.TestCase):
    def test_collapsed_tag4_str_still_yields_candidate(self):
        # The tag4 submessage bytes are pure ASCII (leading 0x0a length prefix),
        # so decode_message collapses field 4 to str and the old bytes-only
        # recursion missed it. Assert the regression case first.
        payload = _candidate_envelope()
        decoded = decode_message(payload)
        self.assertIsInstance(decoded[4], str)

        msg = decode_camera_webrtc_message(payload)
        self.assertIsInstance(msg['field4'], str)
        self.assertEqual(msg['field4_fields'][1], CANDIDATE)
        self.assertEqual(msg['field4_fields'][2], 'mid0')
        self.assertEqual(
            find_webrtc_candidate(msg), 'candidate:1 1 udp 2130706431 1.2.3.4 5000 typ host'
        )

    def test_candidate_without_a_prefix_is_returned(self):
        tag4 = encode_message({1: 'candidate:2 1 UDP 1 10.0.0.1 9 typ host', 2: '0'})
        payload = encode_message({1: 'tok', 2: 'cid', 3: 'sid', 4: tag4, 5: 4, 7: 2})
        self.assertEqual(
            find_webrtc_candidate(decode_camera_webrtc_message(payload)),
            'candidate:2 1 UDP 1 10.0.0.1 9 typ host',
        )

    def test_bytes_valued_tag4_still_yields_candidate(self):
        # A non-UTF-8 byte in the submessage keeps decode_message at bytes; the
        # bytes recursion must still find the candidate (pre-existing path).
        tag4 = encode_message(
            {1: b'a=candidate:3 1 UDP 1 10.0.0.2 9 typ host', 2: b'\xff'}
        )
        payload = encode_message({1: 'tok', 2: 'cid', 3: 'sid', 4: tag4, 5: 4, 7: 2})
        self.assertIsInstance(decode_message(payload)[4], bytes)
        self.assertEqual(
            find_webrtc_candidate(decode_camera_webrtc_message(payload)),
            'candidate:3 1 UDP 1 10.0.0.2 9 typ host',
        )

    def test_no_candidate_returns_empty(self):
        tag4 = encode_message({1: 'v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n', 2: ''})
        payload = encode_message({1: 'tok', 2: 'cid', 3: 'sid', 4: tag4, 5: 2, 7: 2})
        msg = decode_camera_webrtc_message(payload)
        self.assertEqual(find_webrtc_candidate(msg), '')

    def test_empty_message_returns_empty(self):
        self.assertEqual(find_webrtc_candidate({}), '')
        self.assertEqual(find_webrtc_candidate(None), '')


if __name__ == '__main__':
    unittest.main()
