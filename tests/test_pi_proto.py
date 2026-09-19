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
        wire = encode_camera_webrtc_message(
            'tok', 'request-12345678', 'fp', WEBRTC_CANDIDATE,
            candidate='candidate:1 1 UDP 1 10.0.0.1 9 typ host',
        )
        fields = decode_message(wire)
        self.assertEqual(fields[5], WEBRTC_CANDIDATE)
        nested = fields[4].encode('utf-8') if isinstance(fields[4], str) else fields[4]
        self.assertIn('candidate:1', decode_message(nested)[2])

    def test_rejects_non_firmware_outbound_type(self):
        with self.assertRaises(ValueError):
            encode_camera_webrtc_message('tok', 'request-12345678', 'fp', 99, sdp='v=0')


if __name__ == '__main__':
    unittest.main()
