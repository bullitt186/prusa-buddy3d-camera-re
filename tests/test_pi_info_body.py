import sys
import unittest
from pathlib import Path


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import info_body  # noqa: E402
from proto import decode_message  # noqa: E402
from state import RESOLUTIONS, CameraState  # noqa: E402
from status import build_status_message  # noqa: E402


def _nested(decoded, field):
    value = decoded[field]
    if not isinstance(value, bytes):
        value = value.encode('utf-8')
    return decode_message(value)


class InfoBodyTests(unittest.TestCase):
    """GAP-INFO-02: one CameraState drives /c/info and status consistently."""

    def test_body_uses_state_name_and_quality_resolution(self):
        state = CameraState()
        state.set_camera_name('Print Room')
        state.set_quality(2)  # HD
        body = info_body.build_info_body(state, mac='AA:BB:CC:DD:EE:FF',
                                         ip='192.0.2.1', ssid='lab')
        self.assertEqual(body['config']['name'], 'Print Room')
        self.assertEqual(body['config']['resolution'], {'width': 1280, 'height': 720})
        self.assertEqual(body['options']['available_resolutions'],
                         [{'width': 1280, 'height': 720}])
        self.assertEqual(body['config']['network_info']['wifi_ipv4'], '192.0.2.1')

    def test_body_resolution_tracks_each_quality_tier(self):
        state = CameraState()
        for enum, expected in RESOLUTIONS.items():
            with self.subTest(enum=enum):
                state.set_quality(enum)
                body = info_body.build_info_body(state)
                self.assertEqual(
                    (body['config']['resolution']['width'],
                     body['config']['resolution']['height']),
                    expected,
                )

    def test_body_matches_status_name_and_resolution(self):
        state = CameraState()
        state.set_camera_name('Consistent')
        state.set_quality(1)  # SD
        body = info_body.build_info_body(state)
        decoded = decode_message(build_status_message(state))

        status_quality = _nested(decoded, 11)[1]
        status_resolution = RESOLUTIONS[status_quality]
        self.assertEqual(
            (body['config']['resolution']['width'], body['config']['resolution']['height']),
            status_resolution,
        )
        self.assertEqual(body['config']['name'], _nested(decoded, 5)[3])

    def test_rename_is_reflected_in_body(self):
        state = CameraState()
        before = info_body.build_info_body(state)['config']['name']
        state.set_camera_name('Renamed')
        after = info_body.build_info_body(state)['config']['name']
        self.assertNotEqual(before, after)
        self.assertEqual(after, 'Renamed')


if __name__ == '__main__':
    unittest.main()
