"""WP-4 AC-21/AC-22: exact Prusa pairing QR (qr_pairing).

Host-only and stdlib-only. Uses the synthetic, non-working fixture and injected
fake decoders only: no ``zbarimg``, no subprocess image decode, no network, no
``/data`` access. Verifies the captured schema, the bounds, redaction, the
scan rate limiter, and secret hygiene under hostile inputs.
"""
import ast
import json
import logging
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import qr_pairing  # noqa: E402

FIXTURE_PATH = Path(__file__).resolve().parent / 'fixtures' / 'prusa_qr_synthetic.json'
FIXTURE_TEXT = FIXTURE_PATH.read_text(encoding='utf-8').strip()

SSID = 'SyntheticNet'
PWD = 'not-a-real-password'
TOKEN = 'SYNTHETIC-NOT-A-REAL-TOKEN'
SECRET = 'SYNTHETIC-SECRET-VALUE-DO-NOT-LOG'

VALID_PAYLOAD = json.dumps({'ssid': SSID, 'pwd': PWD, 'token': TOKEN})


class LogCapture(logging.Handler):
    """Collect formatted log records for secret-leak assertions."""

    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(self.format(record))


class ParsePayloadTests(unittest.TestCase):
    def test_valid_synthetic_fixture_parses(self):
        result = qr_pairing.parse_payload(FIXTURE_TEXT)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.reason, '')
        self.assertEqual(result.fields, {'ssid': SSID, 'pwd': PWD, 'token': TOKEN})

    def test_valid_payload_parses_and_keys_are_exact(self):
        result = qr_pairing.parse_payload(VALID_PAYLOAD)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(tuple(result.fields.keys()), qr_pairing.QR_KEYS)

    def test_parse_accepts_utf8_bytes(self):
        result = qr_pairing.parse_payload(VALID_PAYLOAD.encode('utf-8'))
        self.assertTrue(result.ok, result.reason)

    def test_non_json_rejected(self):
        result = qr_pairing.parse_payload('this is not json')
        self.assertFalse(result.ok)
        self.assertIsNone(result.fields)

    def test_json_array_rejected(self):
        self.assertFalse(qr_pairing.parse_payload('["a", "b", "c"]').ok)

    def test_json_string_rejected(self):
        self.assertFalse(qr_pairing.parse_payload('"just a string"').ok)

    def test_json_number_rejected(self):
        self.assertFalse(qr_pairing.parse_payload('12345').ok)

    def test_missing_key_rejected(self):
        payload = json.dumps({'ssid': SSID, 'pwd': PWD})
        self.assertFalse(qr_pairing.parse_payload(payload).ok)

    def test_unknown_extra_key_rejected(self):
        payload = json.dumps({'ssid': SSID, 'pwd': PWD, 'token': TOKEN, 'extra': 'x'})
        self.assertFalse(qr_pairing.parse_payload(payload).ok)

    def test_non_string_value_rejected(self):
        payload = json.dumps({'ssid': SSID, 'pwd': PWD, 'token': 12345})
        self.assertFalse(qr_pairing.parse_payload(payload).ok)

    def test_empty_required_value_rejected(self):
        for key in qr_pairing.QR_KEYS:
            payload = json.dumps({'ssid': SSID, 'pwd': PWD, 'token': TOKEN, key: ''})
            self.assertFalse(qr_pairing.parse_payload(payload).ok, key)

    def test_overlong_payload_rejected(self):
        blob = b'x' * (qr_pairing.MAX_PAYLOAD_BYTES + 1)
        result = qr_pairing.parse_payload(blob)
        self.assertFalse(result.ok)
        self.assertIn(str(qr_pairing.MAX_PAYLOAD_BYTES), result.reason)

    def test_overlong_string_rejected(self):
        payload = json.dumps({'ssid': SSID, 'pwd': 'x' * 300, 'token': TOKEN})
        self.assertLessEqual(len(payload.encode('utf-8')), qr_pairing.MAX_PAYLOAD_BYTES)
        result = qr_pairing.parse_payload(payload)
        self.assertFalse(result.ok)
        self.assertIn('pwd', result.reason)

    def test_control_characters_rejected(self):
        payload = json.dumps({'ssid': SSID, 'pwd': 'bad\nvalue', 'token': TOKEN})
        result = qr_pairing.parse_payload(payload)
        self.assertFalse(result.ok)
        self.assertIn('pwd', result.reason)

    def test_c1_and_separator_characters_rejected(self):
        for bad in ('a\u0085b', 'a\u2028b', 'a\u2029b', 'a\u009fb'):
            payload = json.dumps({'ssid': bad, 'pwd': PWD, 'token': TOKEN})
            result = qr_pairing.parse_payload(payload)
            self.assertFalse(result.ok, repr(bad))
            self.assertIn('ssid', result.reason)

    def test_lone_surrogate_rejected(self):
        # A lone-surrogate escape parses under json.loads but cannot round-trip
        # to UTF-8, so it must be rejected before the wizard writes TOML.
        payload = '{"ssid": "\\ud800", "pwd": "p", "token": "t"}'
        result = qr_pairing.parse_payload(payload)
        self.assertFalse(result.ok)
        self.assertIn('Unicode', result.reason)
        # The reason itself must be encodable and free of the surrogate.
        result.reason.encode('utf-8')

    def test_invalid_utf8_bytes_rejected(self):
        result = qr_pairing.parse_payload(b'\xff\xfe\x00')
        self.assertFalse(result.ok)

    def test_non_text_input_rejected(self):
        self.assertFalse(qr_pairing.parse_payload(None).ok)
        self.assertFalse(qr_pairing.parse_payload(12345).ok)


class RedactionTests(unittest.TestCase):
    def test_redacted_hides_pwd_and_token_shows_ssid(self):
        fields = {'ssid': SSID, 'pwd': PWD, 'token': TOKEN}
        view = qr_pairing.redacted(fields)
        self.assertEqual(
            view,
            {'ssid': SSID, 'pwd': qr_pairing.REDACTED, 'token': qr_pairing.REDACTED},
        )

    def test_redacted_handles_missing_fields(self):
        view = qr_pairing.redacted(None)
        self.assertEqual(
            view,
            {
                'ssid': qr_pairing.REDACTED,
                'pwd': qr_pairing.REDACTED,
                'token': qr_pairing.REDACTED,
            },
        )

    def test_parse_result_redacted_method(self):
        result = qr_pairing.parse_payload(VALID_PAYLOAD)
        self.assertEqual(result.redacted()['token'], qr_pairing.REDACTED)
        self.assertEqual(result.redacted()['ssid'], SSID)

    def test_repr_never_contains_secret(self):
        result = qr_pairing.parse_payload(VALID_PAYLOAD)
        rendered = repr(result)
        self.assertNotIn(PWD, rendered)
        self.assertNotIn(TOKEN, rendered)
        self.assertIn(SSID, rendered)


class DecodeImageTests(unittest.TestCase):
    def test_injected_decoder_success(self):
        ok, reason, text = qr_pairing.decode_image(
            b'fake-image-bytes', decoder=lambda data, timeout: FIXTURE_TEXT
        )
        self.assertTrue(ok, reason)
        self.assertEqual(text, FIXTURE_TEXT)

    def test_injected_decoder_returns_none(self):
        ok, reason, text = qr_pairing.decode_image(
            b'fake-image-bytes', decoder=lambda data, timeout: None
        )
        self.assertFalse(ok)
        self.assertEqual(text, '')
        self.assertIn('no QR', reason)

    def test_injected_decoder_failure(self):
        def boom(data, timeout):
            raise RuntimeError('decoder exploded')

        ok, reason, text = qr_pairing.decode_image(b'fake-image-bytes', decoder=boom)
        self.assertFalse(ok)
        self.assertEqual(reason, 'image decode failed')
        self.assertEqual(text, '')

    def test_decode_timeout_handled(self):
        def slow(data, timeout):
            raise subprocess.TimeoutExpired(cmd='zbarimg', timeout=timeout)

        ok, reason, _ = qr_pairing.decode_image(b'fake-image-bytes', decoder=slow)
        self.assertFalse(ok)
        self.assertIn('timed out', reason)

    def test_oversized_image_rejected(self):
        ok, reason, _ = qr_pairing.decode_image(b'0123456789A', max_bytes=10)
        self.assertFalse(ok)
        self.assertIn('exceeds', reason)

    def test_non_bytes_and_empty_rejected(self):
        self.assertFalse(qr_pairing.decode_image('a string')[0])
        self.assertFalse(qr_pairing.decode_image(b'')[0])

    def test_default_decoder_unavailable_reason(self):
        with patch.object(qr_pairing.shutil, 'which', return_value=None):
            ok, reason, text = qr_pairing.decode_image(b'\x89PNG-fake')
        self.assertFalse(ok)
        self.assertIn('zbarimg', reason)
        self.assertEqual(text, '')

    def test_non_finite_and_invalid_bounds_rejected(self):
        for kwargs in (
            {'timeout': float('inf')},
            {'timeout': float('nan')},
            {'timeout': '5'},
            {'max_bytes': float('inf')},
            {'max_bytes': float('nan')},
            {'max_bytes': 'big'},
        ):
            ok, _, _ = qr_pairing.decode_image(b'fake-image-bytes', **kwargs)
            self.assertFalse(ok, kwargs)

    def test_default_decoder_pipes_stdin_without_file(self):
        fake = SimpleNamespace(returncode=0, stdout=FIXTURE_TEXT.encode('utf-8'), stderr=b'')
        with patch.object(qr_pairing.shutil, 'which', return_value='/usr/bin/zbarimg'), \
                patch.object(qr_pairing.subprocess, 'run', return_value=fake) as run:
            ok, reason, text = qr_pairing.decode_image(b'fake-image-bytes')
        self.assertTrue(ok, reason)
        self.assertEqual(text, FIXTURE_TEXT)
        command = run.call_args.args[0]
        self.assertIn('-', command)
        self.assertEqual(run.call_args.kwargs['input'], b'fake-image-bytes')

    def test_default_decoder_nonzero_returncode_is_no_qr(self):
        fake = SimpleNamespace(returncode=1, stdout=b'', stderr=b'no symbol')
        with patch.object(qr_pairing.shutil, 'which', return_value='/usr/bin/zbarimg'), \
                patch.object(qr_pairing.subprocess, 'run', return_value=fake):
            ok, reason, text = qr_pairing.decode_image(b'fake-image-bytes')
        self.assertFalse(ok)
        self.assertIn('no QR', reason)
        self.assertEqual(text, '')


class ScanLimiterTests(unittest.TestCase):
    def test_bounds_attempts_per_window(self):
        limiter = qr_pairing.ScanLimiter(max_attempts=2, window_seconds=10)
        self.assertTrue(limiter.allow(0)[0])
        self.assertTrue(limiter.allow(0)[0])
        allowed, reason = limiter.allow(0)
        self.assertFalse(allowed)
        self.assertIn('too many', reason)

    def test_window_rolls_over(self):
        limiter = qr_pairing.ScanLimiter(max_attempts=1, window_seconds=10)
        self.assertTrue(limiter.allow(0)[0])
        self.assertFalse(limiter.allow(5)[0])
        self.assertTrue(limiter.allow(11)[0])

    def test_injected_clock_used_when_now_omitted(self):
        ticks = iter([0, 0, 0])
        limiter = qr_pairing.ScanLimiter(
            max_attempts=1, window_seconds=10, clock=lambda: next(ticks)
        )
        self.assertTrue(limiter.allow()[0])
        self.assertFalse(limiter.allow()[0])

    def test_invalid_configuration_rejected(self):
        with self.assertRaises(ValueError):
            qr_pairing.ScanLimiter(max_attempts=0)
        with self.assertRaises(ValueError):
            qr_pairing.ScanLimiter(window_seconds=0)


class DecodeAndParseTests(unittest.TestCase):
    def test_end_to_end_with_fake_decoder(self):
        result = qr_pairing.decode_and_parse(
            b'fake-image-bytes', decoder=lambda data, timeout: FIXTURE_TEXT
        )
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.fields['token'], TOKEN)

    def test_limiter_exhausted_skips_decoder(self):
        calls = []

        def decoder(data, timeout):
            calls.append(1)
            return FIXTURE_TEXT

        limiter = qr_pairing.ScanLimiter(max_attempts=1, window_seconds=60)
        self.assertTrue(qr_pairing.decode_and_parse(b'img', decoder=decoder, limiter=limiter).ok)
        result = qr_pairing.decode_and_parse(
            b'img', decoder=decoder, limiter=limiter, now=0
        )
        self.assertFalse(result.ok)
        self.assertIn('too many', result.reason)
        self.assertIsNone(result.fields)
        self.assertEqual(len(calls), 1)

    def test_malformed_payload_reported(self):
        result = qr_pairing.decode_and_parse(
            b'fake-image-bytes', decoder=lambda data, timeout: 'not json'
        )
        self.assertFalse(result.ok)
        self.assertIsNone(result.fields)

    def test_forwards_max_bytes_and_timeout(self):
        seen = {}

        def decoder(data, timeout):
            seen['timeout'] = timeout
            return FIXTURE_TEXT

        result = qr_pairing.decode_and_parse(
            b'fake-image-bytes', decoder=decoder, timeout=3
        )
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(seen['timeout'], 3)

        result = qr_pairing.decode_and_parse(
            b'x' * 20, decoder=decoder, max_bytes=10
        )
        self.assertFalse(result.ok)
        self.assertIn('exceeds', result.reason)


class SecretHygieneTests(unittest.TestCase):
    def setUp(self):
        self.capture = LogCapture()
        self.capture.setFormatter(logging.Formatter('%(message)s'))
        self.logger = qr_pairing.log
        self.old_level = self.logger.level
        self.logger.addHandler(self.capture)
        self.logger.setLevel(logging.DEBUG)

    def tearDown(self):
        self.logger.removeHandler(self.capture)
        self.logger.setLevel(self.old_level)

    def _assert_clean(self, result, secrets=(SECRET, TOKEN, PWD)):
        rendered = repr(result)
        for secret in secrets:
            self.assertNotIn(secret, result.reason)
            self.assertNotIn(secret, rendered)
        for message in self.capture.messages:
            for secret in secrets:
                self.assertNotIn(secret, message)

    def test_hostile_payloads_never_leak(self):
        hostile = [
            json.dumps({'ssid': SSID, 'pwd': SECRET, 'token': SECRET, 'extra': 'x'}),
            json.dumps({'ssid': SSID, 'pwd': SECRET, 'token': 123}),
            json.dumps({'ssid': 'x' * 300, 'pwd': SECRET, 'token': SECRET}),
            json.dumps({'ssid': SSID, 'pwd': SECRET, 'token': SECRET + '\n'}),
            '{"ssid": "' + SSID + '", "pwd": "' + SECRET,
        ]
        for payload in hostile:
            self._assert_clean(qr_pairing.parse_payload(payload))

    def test_decoder_failure_never_leaks(self):
        def boom(data, timeout):
            raise RuntimeError(SECRET)

        ok, reason, text = qr_pairing.decode_image(b'fake', decoder=boom)
        self.assertFalse(ok)
        self.assertNotIn(SECRET, reason)
        for message in self.capture.messages:
            self.assertNotIn(SECRET, message)

    def test_staged_result_repr_redacts_secrets(self):
        result = qr_pairing.parse_payload(VALID_PAYLOAD)
        self._assert_clean(result, secrets=(PWD, TOKEN))


class ImportSafetyTests(unittest.TestCase):
    def test_exact_key_tuple(self):
        self.assertEqual(qr_pairing.QR_KEYS, ('ssid', 'pwd', 'token'))

    def test_module_imports_are_stdlib_only(self):
        source = Path(qr_pairing.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or '').split('.')[0])
        for name in imported:
            self.assertIn(name, sys.stdlib_module_names, name)

    def test_module_imports_without_side_effects(self):
        proc = subprocess.run(
            [sys.executable, '-c', 'import qr_pairing'],
            cwd=str(PI_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == '__main__':
    unittest.main()
