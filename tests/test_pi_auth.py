import asyncio
import sys
import types
import unittest
from pathlib import Path


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

# GAP-AUTH-01 flow tests exercise signaling.PrusaSignaling._authenticate, whose
# module imports socketio. Inject a minimal in-process double so the real runtime
# dependency is never loaded (tests must not import socketio).
_socketio_stub = types.ModuleType('socketio')
_socketio_stub.AsyncClient = object
sys.modules['socketio'] = _socketio_stub

from auth import auth_ack_is_success  # noqa: E402
import signaling  # noqa: E402


class AuthAckPredicateTests(unittest.TestCase):
    def test_exact_integer_zero_succeeds(self):
        self.assertTrue(auth_ack_is_success(0))

    def test_all_other_values_fail(self):
        # 1/5: other ints; False (== 0) must not pass via bool; '0': string;
        # None; malformed payloads (bytes/list/dict) and non-int numerics.
        for ack in (1, 5, -1, True, False, '0', 'false', None, 0.0, b'\x00', [], {}):
            with self.subTest(ack=ack):
                self.assertFalse(auth_ack_is_success(ack))


class _FakeSio:
    def __init__(self, ack=None, error=None):
        self._ack = ack
        self._error = error
        self.calls = []
        self.disconnects = 0

    async def call(self, event, data, timeout=None):
        self.calls.append((event, data, timeout))
        if self._error is not None:
            raise self._error
        return self._ack

    async def disconnect(self):
        self.disconnects += 1


class _FakeSignaling:
    def __init__(self, sio):
        self.sio = sio
        self.fingerprint = 'fingerprint-value'
        self.token = 'token-value'
        self.post_auth_count = 0

    async def _send_post_auth(self):
        self.post_auth_count += 1

    async def _drop_session(self):
        # Mirrors PrusaSignaling._drop_session so the supervised-retry path can
        # be asserted without importing the socketio runtime.
        await self.sio.disconnect()


class AuthenticateFlowTests(unittest.TestCase):
    def _authenticate(self, sio):
        sig = _FakeSignaling(sio)
        asyncio.run(signaling.PrusaSignaling._authenticate(sig))
        return sig

    def test_ack_zero_proceeds_to_post_auth(self):
        sig = self._authenticate(_FakeSio(ack=0))
        self.assertEqual(sig.post_auth_count, 1)
        self.assertEqual(sig.sio.calls[0][0], 'camera_authentication')
        self.assertEqual(sig.sio.disconnects, 0)

    def test_rejected_acks_emit_no_post_auth(self):
        for ack in (1, 5, True, False, '0', None, b'', [], {}):
            with self.subTest(ack=ack):
                sig = self._authenticate(_FakeSio(ack=ack))
                self.assertEqual(sig.post_auth_count, 0)
                # WP-1 hardening: a rejected ACK drops the session so the
                # supervisor retries with a fresh client + backoff.
                self.assertEqual(sig.sio.disconnects, 1)

    def test_timeout_emits_no_post_auth(self):
        sig = self._authenticate(_FakeSio(error=TimeoutError('auth timeout')))
        self.assertEqual(sig.post_auth_count, 0)
        self.assertEqual(sig.sio.disconnects, 1)

    def test_exception_emits_no_post_auth(self):
        sig = self._authenticate(_FakeSio(error=RuntimeError('boom')))
        self.assertEqual(sig.post_auth_count, 0)
        self.assertEqual(sig.sio.disconnects, 1)


if __name__ == '__main__':
    unittest.main()
