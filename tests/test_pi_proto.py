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

    def test_encodes_firmware_answer_as_numeric_type_and_field_three_payload(self):
        wire = encode_camera_webrtc_message(
            'request-12345678', WEBRTC_ANSWER, 'v=0\r\na=recvonly\r\n'
        )

        self.assertEqual(
            decode_message(wire),
            {1: 'request-12345678', 2: WEBRTC_ANSWER, 3: 'v=0\r\na=recvonly\r\n'},
        )

    def test_encodes_candidate_with_numeric_type(self):
        wire = encode_camera_webrtc_message(
            'request-12345678', WEBRTC_CANDIDATE, 'candidate:1 1 UDP 1 10.0.0.1 9 typ host'
        )
        self.assertEqual(decode_message(wire)[2], WEBRTC_CANDIDATE)

    def test_rejects_non_firmware_outbound_type(self):
        with self.assertRaises(ValueError):
            encode_camera_webrtc_message('request-12345678', WEBRTC_OFFER, 'v=0')


if __name__ == '__main__':
    unittest.main()
