"""GAP-NETWORK-01: firmware RSSI(dBm) -> 0..100 quality conversion."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import network  # noqa: E402


class RssiConversionTests(unittest.TestCase):
    def test_firmware_formula(self):
        cases = {0: 0, -100: 0, -99: 2, -70: 60, -56: 88, -51: 98, -50: 100, -40: 100}
        for rssi, expected in cases.items():
            with self.subTest(rssi=rssi):
                self.assertEqual(network.rssi_to_quality(rssi), expected)

    def test_non_numeric_is_zero(self):
        self.assertEqual(network.rssi_to_quality(None), 0)
        self.assertEqual(network.rssi_to_quality('n/a'), 0)


class WirelessParseTests(unittest.TestCase):
    LINE = ' wlan0: 0000   54.  -56.  -256        0      0      0      0  0  0\n'

    def test_parses_level_not_link(self):
        self.assertEqual(network.parse_wireless_level(self.LINE), -56)

    def test_non_wlan0_is_none(self):
        self.assertIsNone(network.parse_wireless_level(' eth0: 0000 54. -56. -256 0\n'))

    def test_malformed_is_none(self):
        self.assertIsNone(network.parse_wireless_level(' wlan0:\n'))

    def test_quality_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'wireless')
            with open(path, 'w') as f:
                f.write('Inter-| sta-|   Quality        |   Discarded packets\n')
                f.write(self.LINE)
            self.assertEqual(network.signal_quality_from_wireless(path), 88)

    def test_missing_file_is_zero(self):
        self.assertEqual(network.signal_quality_from_wireless('/nonexistent/wireless'), 0)


if __name__ == '__main__':
    unittest.main()
