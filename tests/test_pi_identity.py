import hashlib
import sys
import unittest
from pathlib import Path


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

from identity import fingerprint_from_mac, normalize_wifi_mac  # noqa: E402


class FirmwareIdentityTests(unittest.TestCase):
    def test_normalizes_sysfs_mac_like_firmware_sprintf(self):
        self.assertEqual(
            normalize_wifi_mac('d8:3a:dd:32:1c:ac\n'),
            'D8:3A:DD:32:1C:AC',
        )

    def test_accepts_hyphenated_mac_and_normalizes_separators(self):
        self.assertEqual(
            normalize_wifi_mac('00-e0-4c-86-17-ff'),
            '00:E0:4C:86:17:FF',
        )

    def test_fingerprint_hashes_exact_uppercase_colon_preimage(self):
        expected = hashlib.md5(b'00:E0:4C:86:17:FF').hexdigest()
        self.assertEqual(fingerprint_from_mac('00:e0:4c:86:17:ff'), expected)

    def test_rejects_malformed_mac(self):
        for value in ('', '00:11:22:33:44', 'not-a-mac', '001122334455'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_wifi_mac(value)


if __name__ == '__main__':
    unittest.main()
