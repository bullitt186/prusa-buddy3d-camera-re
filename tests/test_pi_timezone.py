"""GAP-STATUS-04: firmware timezone detection/conversion (/etc/TZ)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

from timezone import (  # noqa: E402
    convert_timezone,
    parse_timezone_response,
    read_tz_file,
    resolve_tz_name,
    write_tz_file,
)


class ConvertTimezoneTests(unittest.TestCase):
    def test_utc_plus_becomes_utc_minus(self):
        self.assertEqual(convert_timezone('UTC+2'), 'UTC-2')
        self.assertEqual(convert_timezone('UTC+05:30'), 'UTC-05:30')

    def test_utc_minus_becomes_utc_plus(self):
        self.assertEqual(convert_timezone('UTC-5'), 'UTC+5')

    def test_other_values_pass_through(self):
        self.assertEqual(convert_timezone('Europe/Berlin'), 'Europe/Berlin')
        self.assertEqual(convert_timezone('UTC'), 'UTC')


class ParseResponseTests(unittest.TestCase):
    def test_success_extracts_timezone(self):
        body = json.dumps({'status': 'success', 'timezone': 'UTC+2', 'current_time': 1})
        self.assertEqual(parse_timezone_response(body), 'UTC+2')

    def test_non_success_is_none(self):
        self.assertIsNone(parse_timezone_response(json.dumps({'status': 'error', 'timezone': 'UTC+2'})))

    def test_missing_or_empty_timezone_is_none(self):
        self.assertIsNone(parse_timezone_response(json.dumps({'status': 'success'})))
        self.assertIsNone(parse_timezone_response(json.dumps({'status': 'success', 'timezone': ''})))

    def test_malformed_json_is_none(self):
        self.assertIsNone(parse_timezone_response('not json'))


class TzFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, 'TZ')

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_read_round_trips(self):
        self.assertTrue(write_tz_file('UTC-2', self.path))
        self.assertEqual(read_tz_file(self.path), 'UTC-2')

    def test_write_rejects_empty_and_over_long(self):
        self.assertFalse(write_tz_file('', self.path))
        self.assertFalse(write_tz_file('x' * 65, self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_read_missing_file_is_empty(self):
        self.assertEqual(read_tz_file(os.path.join(self._tmp.name, 'nope')), '')

    def test_resolve_converts_and_reports_persisted_value(self):
        reported = resolve_tz_name('UTC+3', self.path)
        self.assertEqual(reported, 'UTC-3')
        self.assertEqual(read_tz_file(self.path), 'UTC-3')


if __name__ == '__main__':
    unittest.main()
