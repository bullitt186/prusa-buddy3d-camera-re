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
    decode_message,
    encode_camera_webrtc_message,
    encode_message,
)


class CameraWebRtcProtocolTests(unittest.TestCase):
    def test_decodes_firmware_316_inbound_offer_layout(self):
        wire = encode_message({
            1: 'request-12345678',
            2: WEBRTC_OFFER,
            3: 'viewer-client-id',
            4: 'v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n',
            5: 1,
            6: 300,
            7: 3,
            8: 1,
            9: 'fhd',
            10: 30,
            12: 2,
        })

        decoded = decode_camera_webrtc_message(wire)

        self.assertEqual(decoded['request_id'], 'request-12345678')
        self.assertEqual(decoded['msg_type'], WEBRTC_OFFER)
        self.assertEqual(decoded['client_id'], 'viewer-client-id')
        self.assertTrue(decoded['payload'].startswith('v=0'))
        self.assertEqual(decoded['ttl'], 300)
        self.assertEqual(decoded['quality'], 'fhd')
        self.assertEqual(decoded['fps'], 30)

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
