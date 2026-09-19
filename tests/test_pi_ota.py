"""GAP-OTA-01: firmware OTA check-in classification (truthful decline)."""
import json
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import ota  # noqa: E402


def _body(last_version, force=False, file='https://ota/file.tar', sha1='abc'):
    return json.dumps({
        'file': file, 'last_version': last_version, 'sha1sum': sha1,
        'force_upgrade': force,
    })


class OtaClassificationTests(unittest.TestCase):
    def test_up_to_date(self):
        self.assertEqual(ota.classify('3.1.6', _body('3.1.6')), ota.UP_TO_DATE)

    def test_update_available(self):
        self.assertEqual(ota.classify('3.1.6', _body('3.1.7')), ota.UPDATE_AVAILABLE)

    def test_forced_update(self):
        self.assertEqual(ota.classify('3.1.6', _body('3.1.7', force=True)), ota.UPDATE_AVAILABLE)

    def test_older_release_is_up_to_date(self):
        self.assertEqual(ota.classify('3.1.6', _body('3.1.5')), ota.UP_TO_DATE)

    def test_malformed_or_incomplete_is_invalid(self):
        for body in ('not json', '{}', json.dumps({'file': 'x'}), json.dumps([]), ''):
            with self.subTest(body=body):
                self.assertEqual(ota.classify('3.1.6', body), ota.INVALID)

    def test_dotted_numeric_comparison(self):
        self.assertTrue(ota.is_update_available('3.1.6', '3.1.10'))
        self.assertFalse(ota.is_update_available('3.1.10', '3.1.6'))


class OtaIntegrityTests(unittest.TestCase):
    def test_integrity_match_and_mismatch(self):
        data = ota.parse_checkin(_body('3.1.7', sha1='deadbeef'))
        self.assertTrue(ota.verify_integrity(data, 'DEADBEEF'))
        self.assertFalse(ota.verify_integrity(data, 'cafebabe'))
        self.assertFalse(ota.verify_integrity(data, ''))

    def test_sha1_of(self):
        self.assertEqual(ota.sha1_of(b'abc'), 'a9993e364706816aba3e25717850c26c9cd0d89d')


class OtaPolicyTests(unittest.TestCase):
    def test_pi_never_applies_an_update(self):
        for decision in (ota.UP_TO_DATE, ota.UPDATE_AVAILABLE, ota.FORCED_UPDATE, ota.INVALID):
            with self.subTest(decision=decision):
                self.assertFalse(ota.can_apply(decision))

    def test_decline_reason_is_explicit(self):
        self.assertTrue(ota.decline_reason())


if __name__ == '__main__':
    unittest.main()
