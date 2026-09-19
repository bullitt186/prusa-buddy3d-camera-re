"""GAP-STATUS-03: pin the status nested wire types/tags to the 3.1.6 descriptor.

Recovered with the ELF descriptor dumper (docs/protocol.md):
- CameraInfoMessage @ 0x3f6e98
- timelapse_status   @ 0x3f753c: tag6 = fixed32 (float), tag7 = uvarint
- extended_status    @ 0x3f748c: tag4 submsg @ 0x3f72b0 has tags 1-4 uvarint + tag5 string
                                  tag6 submsg @ 0x3f7278 has tags 1-2 uvarint + tag3 string
"""
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

from proto import decode_message  # noqa: E402
from state import CameraState  # noqa: E402
from status import build_status_message  # noqa: E402


def _nested(fields, key):
    """Return the raw bytes of a length-delimited field from decode_message."""
    value = fields[key]
    if isinstance(value, bytes):
        return value
    return value.encode('utf-8')  # utf-8 decode/encode round-trips


class StatusSchemaTests(unittest.TestCase):
    def setUp(self):
        self.status = build_status_message(
            CameraState(), token='tok', mac='AA:BB:CC:DD:EE:FF', ip='192.168.0.10',
            ssid='net', signal_quality=50, cpu_temperature=30.0, uptime=100,
            load_average='0 0 0', process_count=1, sid='sid', tz_name='Europe/Berlin',
        )
        self.top = decode_message(self.status)

    def test_timelapse_tag6_is_fixed32_and_tag7_is_uvarint(self):
        tl = decode_message(_nested(self.top, 2))
        self.assertIsInstance(tl[6], bytes, 'tag6 must be fixed32 (float)')
        self.assertEqual(len(tl[6]), 4)
        self.assertIsInstance(tl[7], int, 'tag7 must be uvarint')

    def test_timelapse_enable_and_interval(self):
        tl = decode_message(_nested(self.top, 2))
        self.assertEqual(tl[1], 2, 'disabled timelapse maps to 2 (FUN_000abcdc)')
        self.assertEqual(tl[2], 10, 'default interval is 10 (FUN_000abcb0)')

    def test_extended_status_storage_block_defaults(self):
        ext = decode_message(_nested(self.top, 5))
        storage = decode_message(_nested(ext, 4))
        self.assertEqual(storage[1], 2, 'absent storage mounted-state is 2')
        self.assertEqual(storage[2], 0)
        self.assertEqual(storage[3], 0)
        self.assertEqual(storage[4], 0)
        self.assertEqual(storage[5], 'UNKNOWN', 'tag5 is the SD mount-mode string')
        self.assertNotIn(6, storage, 'tag6 must not exist in descriptor 0x3f72b0')

    def test_extended_status_storage_block_explicit(self):
        status = build_status_message(CameraState(), storage=(1, 100, 40, 60, 'RW'))
        ext = decode_message(_nested(decode_message(status), 5))
        storage = decode_message(_nested(ext, 4))
        self.assertEqual(storage, {1: 1, 2: 100, 3: 40, 4: 60, 5: 'RW'})

    def test_extended_status_rtsp_url_is_tag3_string(self):
        ext = decode_message(_nested(self.top, 5))
        rtsp = decode_message(_nested(ext, 6))
        self.assertIn(3, rtsp, 'RTSP URL must be extended_status.6.3')
        self.assertNotIn(4, rtsp, 'tag4 must not exist in descriptor 0x3f7278')


if __name__ == '__main__':
    unittest.main()
