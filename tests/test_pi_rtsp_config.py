import sys
import unittest
from pathlib import Path


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import rtsp_config  # noqa: E402


class RtspConfigTests(unittest.TestCase):
    def test_defaults_preserve_prusa_endpoint(self):
        self.assertEqual(rtsp_config.load_rtsp_config({}), (8554, '/live', 'Prusa'))

    def test_home_assistant_endpoint(self):
        self.assertEqual(
            rtsp_config.load_rtsp_config({
                'RTSP_PORT': '8555',
                'RTSP_PATH': '/live',
                'RTSP_LABEL': 'Home Assistant',
            }),
            (8555, '/live', 'Home Assistant'),
        )

    def test_rejects_invalid_ports(self):
        for value in ('', 'abc', '0', '-1', '65536', None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    rtsp_config.load_rtsp_config({'RTSP_PORT': value})

    def test_rejects_invalid_paths(self):
        for value in ('', '/', 'live', '/with space'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    rtsp_config.load_rtsp_config({'RTSP_PATH': value})


class HomeAssistantUnitTests(unittest.TestCase):
    def test_unit_is_independent_and_always_on(self):
        unit = (PI_DIR / 'systemd' / 'prusa-ha-rtsp.service').read_text()
        self.assertIn('RTSP_PORT=8555', unit)
        self.assertIn('RTSP_PATH=/live', unit)
        self.assertIn('Requires=rpicam-source.service', unit)
        self.assertIn('WantedBy=multi-user.target', unit)
        self.assertNotIn('rtsp.mode', unit)


if __name__ == '__main__':
    unittest.main()
