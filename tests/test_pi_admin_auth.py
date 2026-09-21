"""WP-3b AC-19: admin authentication and session primitives (admin_auth).

Stdlib-only. No aiohttp/socketio/gi/GStreamer import; time-dependent cases pass
an explicit ``now`` and never sleep.
"""
import json
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import admin_auth  # noqa: E402


class PasswordHashTests(unittest.TestCase):
    def test_hash_is_salted_and_verifies(self):
        first = admin_auth.hash_password('correct horse battery')
        second = admin_auth.hash_password('correct horse battery')
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith('scrypt$'))
        self.assertTrue(admin_auth.verify_password('correct horse battery', first))
        self.assertTrue(admin_auth.verify_password('correct horse battery', second))

    def test_encoded_never_contains_plaintext(self):
        plaintext = 'SuperSecret123'
        encoded = admin_auth.hash_password(plaintext)
        self.assertNotIn(plaintext, encoded)
        self.assertEqual(encoded.split('$')[0], 'scrypt')

    def test_verify_rejects_wrong_password(self):
        encoded = admin_auth.hash_password('the-right-one')
        self.assertFalse(admin_auth.verify_password('the-wrong-one', encoded))

    def test_verify_rejects_empty_or_non_string_password(self):
        encoded = admin_auth.hash_password('the-right-one')
        for password in ('', None, 123, b'the-right-one', []):
            with self.subTest(password=password):
                self.assertFalse(admin_auth.verify_password(password, encoded))

    def test_verify_returns_false_on_malformed_encoding(self):
        malformed = [
            '',
            'not-a-hash',
            None,
            123,
            'scrypt$16384$8$1$onlyfive',
            'scrypt$x$8$1$AAAA$AAAA',
            'scrypt$16384$8$1$!!!!$!!!!',
            'scrypt$16384$8$1$' + 'A' * 200 + '$' + 'A' * 8,
            'scrypt$999999$8$1$AAAA$AAAA',
            'scrypt$16384$0$1$AAAA$AAAA',
            'bcrypt$16384$8$1$AAAA$AAAA',
        ]
        for encoded in malformed:
            with self.subTest(encoded=encoded):
                self.assertFalse(admin_auth.verify_password('anything', encoded))

    def test_hash_password_rejects_empty_and_too_long(self):
        for password in ('', None, 123, 'x' * (admin_auth.MAX_PASSWORD_LENGTH + 1)):
            with self.subTest(password_type=type(password).__name__):
                with self.assertRaises(ValueError):
                    admin_auth.hash_password(password)

    def test_password_strength_ok(self):
        cases = [
            ('correct horse', True),
            ('abcdefgh', True),
            ('abcdefg', False),
            ('', False),
            ('        ', False),
            ('\t\n ', False),
            (None, False),
            (123, False),
        ]
        for password, expected in cases:
            with self.subTest(password=password):
                ok, reason = admin_auth.password_strength_ok(password)
                self.assertIsInstance(ok, bool)
                self.assertIsInstance(reason, str)
                self.assertTrue(reason)
                self.assertEqual(ok, expected)
        ok, reason = admin_auth.password_strength_ok('x' * (admin_auth.MAX_PASSWORD_LENGTH + 1))
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_scrypt_parameters_are_documented(self):
        self.assertEqual(admin_auth.SCRYPT_N, 2 ** 14)
        self.assertEqual(admin_auth.SCRYPT_R, 8)
        self.assertEqual(admin_auth.SCRYPT_P, 1)
        self.assertEqual(admin_auth.SCRYPT_DKLEN, 32)
        self.assertEqual(admin_auth.SCRYPT_SALT_BYTES, 16)


class SessionStoreTests(unittest.TestCase):
    def test_create_and_validate(self):
        store = admin_auth.SessionStore()
        token = store.create(now=0)
        self.assertIsInstance(token, str)
        self.assertTrue(token)
        self.assertEqual(store.count(), 1)
        self.assertTrue(store.validate(token, now=1))
        self.assertFalse(store.validate('not-a-token', now=1))
        self.assertFalse(store.validate(None, now=1))

    def test_tokens_are_unique(self):
        store = admin_auth.SessionStore()
        self.assertNotEqual(store.create(now=0), store.create(now=0))

    def test_idle_expiry(self):
        store = admin_auth.SessionStore()
        token = store.create(now=0, idle_ttl=100, absolute_ttl=1000)
        self.assertTrue(store.validate(token, now=99))
        # The use at 99 refreshed the idle anchor; 101s of further idleness expires it.
        self.assertFalse(store.validate(token, now=200))
        self.assertEqual(store.count(), 0)

    def test_absolute_expiry(self):
        store = admin_auth.SessionStore()
        token = store.create(now=0, idle_ttl=1000, absolute_ttl=100)
        self.assertTrue(store.validate(token, now=99))
        self.assertFalse(store.validate(token, now=100))
        self.assertEqual(store.count(), 0)

    def test_refresh_extends_idle_but_not_absolute(self):
        store = admin_auth.SessionStore()
        token = store.create(now=0, idle_ttl=100, absolute_ttl=1000)
        self.assertTrue(store.validate(token, now=90))
        # Without the refresh at 90 this would have expired at 180.
        self.assertTrue(store.validate(token, now=180))

        bounded = store.create(now=0, idle_ttl=1000, absolute_ttl=150)
        self.assertTrue(store.validate(bounded, now=140))
        self.assertFalse(store.validate(bounded, now=160))

    def test_revoke(self):
        store = admin_auth.SessionStore()
        token = store.create(now=0)
        self.assertTrue(store.revoke(token))
        self.assertFalse(store.revoke(token))
        self.assertFalse(store.validate(token, now=1))
        self.assertEqual(store.count(), 0)

    def test_revoke_all(self):
        store = admin_auth.SessionStore()
        for _ in range(3):
            store.create(now=0)
        store.revoke_all()
        self.assertEqual(store.count(), 0)

    def test_csrf_per_session_and_constant_time_validate(self):
        store = admin_auth.SessionStore()
        first = store.create(now=0)
        second = store.create(now=0)
        csrf = store.csrf_for(first)
        self.assertIsInstance(csrf, str)
        self.assertTrue(csrf)
        self.assertNotEqual(csrf, store.csrf_for(second))
        self.assertTrue(store.validate_csrf(first, csrf))
        self.assertFalse(store.validate_csrf(first, 'wrong-csrf'))
        self.assertFalse(store.validate_csrf(first, None))
        self.assertFalse(store.validate_csrf('unknown-token', csrf))
        self.assertIsNone(store.csrf_for('unknown-token'))

    def test_store_is_bounded(self):
        store = admin_auth.SessionStore(max_sessions=3)
        tokens = [store.create(now=i) for i in range(5)]
        self.assertEqual(store.count(), 3)
        self.assertFalse(store.validate(tokens[0], now=5))
        self.assertTrue(store.validate(tokens[-1], now=5))


class CookieTests(unittest.TestCase):
    def test_session_cookie_has_required_attributes(self):
        cookie = admin_auth.session_cookie('token-value', max_age=600, secure=True)
        self.assertIn(f'{admin_auth.SESSION_COOKIE_NAME}=token-value', cookie)
        self.assertIn('Path=/', cookie)
        self.assertIn('Max-Age=600', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)
        self.assertIn('Secure', cookie)

    def test_secure_toggle(self):
        self.assertNotIn('Secure', admin_auth.session_cookie('t', max_age=60, secure=False))
        self.assertIn('Secure', admin_auth.session_cookie('t', max_age=60, secure=True))

    def test_clear_session_cookie(self):
        cookie = admin_auth.clear_session_cookie()
        self.assertIn(f'{admin_auth.SESSION_COOKIE_NAME}=', cookie)
        self.assertIn('Max-Age=0', cookie)
        self.assertIn('Path=/', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)
        self.assertIn('Secure', cookie)

    def test_session_cookie_rejects_empty_token(self):
        with self.assertRaises(ValueError):
            admin_auth.session_cookie('')


class LoginRateLimiterTests(unittest.TestCase):
    def test_allowed_then_blocked_after_threshold(self):
        limiter = admin_auth.LoginRateLimiter(
            max_attempts=3, window=60, lockout=120, max_lockout=3600, max_keys=16
        )
        key = '203.0.113.9'
        self.assertEqual(limiter.check(key, now=0), (True, 0))
        for _ in range(2):
            limiter.record_failure(key, now=0)
        self.assertEqual(limiter.check(key, now=0), (True, 0))
        limiter.record_failure(key, now=0)
        allowed, retry_after = limiter.check(key, now=0)
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)

    def test_lockout_expires(self):
        limiter = admin_auth.LoginRateLimiter(
            max_attempts=2, window=60, lockout=120, max_lockout=3600, max_keys=16
        )
        limiter.record_failure('user', now=0)
        limiter.record_failure('user', now=0)
        self.assertFalse(limiter.check('user', now=0)[0])
        self.assertTrue(limiter.check('user', now=121)[0])

    def test_success_resets(self):
        limiter = admin_auth.LoginRateLimiter(
            max_attempts=2, window=60, lockout=120, max_lockout=3600, max_keys=16
        )
        limiter.record_failure('user', now=0)
        limiter.record_failure('user', now=0)
        limiter.record_success('user')
        self.assertEqual(limiter.check('user', now=0), (True, 0))
        self.assertEqual(limiter.count(), 0)

    def test_prunes_old_entries(self):
        limiter = admin_auth.LoginRateLimiter(
            max_attempts=5, window=60, lockout=120, max_lockout=3600, max_keys=16
        )
        for i in range(10):
            limiter.record_failure(f'host-{i}', now=0)
        limiter.check('probe', now=1000)
        self.assertEqual(limiter.count(), 0)

    def test_memory_is_bounded(self):
        limiter = admin_auth.LoginRateLimiter(
            max_attempts=5, window=60, lockout=120, max_lockout=3600, max_keys=2
        )
        for name in ('a', 'b', 'c', 'd'):
            limiter.record_failure(name, now=0)
        self.assertLessEqual(limiter.count(), 2)


class ReauthTests(unittest.TestCase):
    def test_requires_reauth_for_listed_actions(self):
        for action in ('credential_change', 'update_install', 'factory_reset',
                       'backup_export_secrets', 'ssh_enable'):
            with self.subTest(action=action):
                self.assertTrue(admin_auth.requires_reauth(action))
        for action in ('login', 'view_status', '', None):
            with self.subTest(action=action):
                self.assertFalse(admin_auth.requires_reauth(action))
        self.assertEqual(
            admin_auth.REAUTH_ACTIONS,
            frozenset({'credential_change', 'update_install', 'factory_reset',
                       'backup_export_secrets', 'ssh_enable'}),
        )

    def test_reauth_ok(self):
        encoded = admin_auth.hash_password('current-admin-password')
        self.assertTrue(admin_auth.reauth_ok('session-token', 'current-admin-password', encoded))
        self.assertFalse(admin_auth.reauth_ok('session-token', 'wrong-password', encoded))
        self.assertFalse(admin_auth.reauth_ok('', 'current-admin-password', encoded))
        self.assertFalse(admin_auth.reauth_ok(None, 'current-admin-password', encoded))


class RedactionTests(unittest.TestCase):
    def test_redacts_nested_dict_and_lists(self):
        payload = {
            'token': 'prusa-token-value',
            'fingerprint': 'fingerprint-value',
            'config': {
                'mqtt': {'username': 'mqtt-user', 'password': 'mqtt-pass'},
                'wifi': {'psk': 'wifi-psk-value'},
            },
            'admin': {'password_hash': 'scrypt$encoded$hash'},
            'headers': {'Cookie': 'sid=1', 'Authorization': 'Bearer abc', 'Accept': 'text/html'},
            'items': [{'token': 'nested-token'}, 'plain'],
            'name': 'keep-me',
        }
        redacted = admin_auth.redact(payload)
        self.assertEqual(redacted['token'], admin_auth.REDACTED)
        self.assertEqual(redacted['fingerprint'], admin_auth.REDACTED)
        self.assertEqual(redacted['config']['mqtt']['username'], admin_auth.REDACTED)
        self.assertEqual(redacted['config']['mqtt']['password'], admin_auth.REDACTED)
        self.assertEqual(redacted['config']['wifi']['psk'], admin_auth.REDACTED)
        self.assertEqual(redacted['admin']['password_hash'], admin_auth.REDACTED)
        self.assertEqual(redacted['headers']['Cookie'], admin_auth.REDACTED)
        self.assertEqual(redacted['headers']['Authorization'], admin_auth.REDACTED)
        self.assertEqual(redacted['headers']['Accept'], 'text/html')
        self.assertEqual(redacted['items'][0]['token'], admin_auth.REDACTED)
        self.assertEqual(redacted['items'][1], 'plain')
        self.assertEqual(redacted['name'], 'keep-me')

        serialized = json.dumps(redacted)
        for secret in ('prusa-token-value', 'fingerprint-value', 'mqtt-user',
                       'mqtt-pass', 'wifi-psk-value', 'sid=1', 'Bearer abc'):
            self.assertNotIn(secret, serialized)

    def test_redact_dict_and_headers(self):
        self.assertEqual(
            admin_auth.redact_dict({'password': 'x', 'ok': 'y'}),
            {'password': admin_auth.REDACTED, 'ok': 'y'},
        )
        headers = {
            'Set-Cookie': 'a=b',
            'X-Camera-Token': 'tok',
            'X-Camera-Fingerprint': 'fp',
            'Content-Type': 'application/json',
        }
        redacted = admin_auth.redact_headers(headers)
        self.assertEqual(redacted['Set-Cookie'], admin_auth.REDACTED)
        self.assertEqual(redacted['X-Camera-Token'], admin_auth.REDACTED)
        self.assertEqual(redacted['X-Camera-Fingerprint'], admin_auth.REDACTED)
        self.assertEqual(redacted['Content-Type'], 'application/json')

    def test_redact_header_pairs_preserve_shape(self):
        pairs = [('Cookie', 'sid=secret'), ('Accept', 'application/json')]
        redacted = admin_auth.redact_headers(pairs)
        self.assertIsInstance(redacted, list)
        self.assertIsInstance(redacted[0], tuple)
        self.assertEqual(redacted[0], ('Cookie', admin_auth.REDACTED))
        self.assertEqual(redacted[1], ('Accept', 'application/json'))

    def test_secret_substrings_are_scrubbed(self):
        redacted = admin_auth.redact({'note': 'token=abc123 here'}, secrets=['abc123'])
        self.assertNotIn('abc123', json.dumps(redacted))
        self.assertIn(admin_auth.REDACTED, redacted['note'])

    def test_safe_on_arbitrary_structures(self):
        self.assertEqual(admin_auth.redact(b'bytes'), b'bytes')
        self.assertEqual(admin_auth.redact(7), 7)
        self.assertIsNone(admin_auth.redact(None))
        self.assertEqual(admin_auth.redact((1, {'token': 't'})), (1, {'token': admin_auth.REDACTED}))
        self.assertEqual(admin_auth.redact_headers('nope'), 'nope')

    def test_never_emits_secret(self):
        secret = 'unique-synthetic-secret-value'
        payload = {'token': secret, 'nested': {'password_hash': secret},
                   'list': [secret, {'mqtt_password': secret}]}
        serialized = json.dumps(admin_auth.redact(payload, secrets=[secret]))
        self.assertNotIn(secret, serialized)


if __name__ == '__main__':
    unittest.main()
