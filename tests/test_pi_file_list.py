"""GAP-TIMELAPSE-01: `file_list` sender envelope (signaling.send_file_list)."""
import asyncio
import sys
import types
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

# signaling imports socketio; inject a minimal in-process double so the runtime
# dependency is never loaded (tests must not import socketio).
_socketio_stub = types.ModuleType('socketio')
_socketio_stub.AsyncClient = object
sys.modules.setdefault('socketio', _socketio_stub)

import signaling  # noqa: E402
from proto import decode_message, encode_message  # noqa: E402


class _FakeSender:
    """Minimal stand-in providing just what ``send_file_list``/``pb_summary`` use."""

    def __init__(self, token, fingerprint='fp-value'):
        self.token = token
        self.fingerprint = fingerprint
        self.sent = []

    async def sio_emit(self, event, data, callback=None):
        self.sent.append((event, data, callback))

    def _log_ack(self, event):
        return ('ack', event)

    def _redact(self, value):
        return signaling.PrusaSignaling._redact(self, value)


class SendFileListTests(unittest.TestCase):
    def _send(self, fragment, request_id=None, token='http-token'):
        sender = _FakeSender(token)
        asyncio.run(
            signaling.PrusaSignaling.send_file_list(sender, fragment, request_id)
        )
        return sender

    def test_small_fragment_envelope(self):
        sender = self._send('1;1\na.avi;b.avi', request_id='req-1')
        self.assertEqual(len(sender.sent), 1)
        event, data, callback = sender.sent[0]
        self.assertEqual(event, 'file_list')
        fields = decode_message(data)
        self.assertEqual(fields[1], '1;1\na.avi;b.avi')
        self.assertEqual(fields[2], 'http-token')
        self.assertEqual(fields[3], 'req-1')
        self.assertIsNotNone(callback)

    def test_request_id_field_omitted_when_absent(self):
        for request_id in (None, ''):
            with self.subTest(request_id=request_id):
                sender = self._send('1;1\na.avi', request_id=request_id)
                fields = decode_message(sender.sent[0][1])
                self.assertNotIn(3, fields)
                self.assertEqual(fields[2], 'http-token')

    def test_token_is_redacted_in_summary(self):
        sender = _FakeSender('super-secret-token')
        summary = signaling.PrusaSignaling.pb_summary(
            sender,
            encode_message({1: '1;1\na.avi', 2: 'super-secret-token'}),
        )
        self.assertNotIn('super-secret-token', summary)
        self.assertIn('<redacted:', summary)


if __name__ == '__main__':
    unittest.main()
