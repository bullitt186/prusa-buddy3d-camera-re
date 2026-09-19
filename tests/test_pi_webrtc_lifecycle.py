"""GAP-WEBRTC-03: WebRTC session lifecycle and resume-snapshot wiring.

The pure policy (``webrtc_lifecycle``) is imported directly. ``main.py`` and
``webrtc.py`` depend on aiohttp/socketio and PyGObject/GStreamer respectively, so
their wiring is checked with the AST pattern from
``test_pi_timelapse.MainTimelapseWiringTests``.
"""
import ast
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
MAIN_PY = PI_DIR / 'main.py'
WEBRTC_PY = PI_DIR / 'webrtc.py'
sys.path.insert(0, str(PI_DIR))

import webrtc_lifecycle  # noqa: E402


class EndReasonTests(unittest.TestCase):
    def test_all_states_map_correctly(self):
        self.assertIsNone(webrtc_lifecycle.end_reason(0))   # NEW
        self.assertIsNone(webrtc_lifecycle.end_reason(1))   # CHECKING
        self.assertIsNone(webrtc_lifecycle.end_reason(2))   # CONNECTED
        self.assertIsNone(webrtc_lifecycle.end_reason(3))   # COMPLETED
        self.assertEqual(webrtc_lifecycle.end_reason(4), 'ice-failed')
        self.assertEqual(webrtc_lifecycle.end_reason(5), 'ice-disconnected')
        self.assertEqual(webrtc_lifecycle.end_reason(6), 'ice-closed')

    def test_unknown_state_is_not_terminal(self):
        self.assertIsNone(webrtc_lifecycle.end_reason(99))
        self.assertIsNone(webrtc_lifecycle.end_reason(None))

    def test_connected_and_ended_membership(self):
        self.assertEqual(webrtc_lifecycle.ICE_CONNECTED, frozenset({2, 3}))
        self.assertEqual(webrtc_lifecycle.ICE_ENDED, frozenset({4, 6}))
        self.assertEqual(webrtc_lifecycle.ICE_DISCONNECTED, 5)
        self.assertTrue(webrtc_lifecycle.ICE_CONNECTED.isdisjoint(webrtc_lifecycle.ICE_ENDED))


class _AstHelpers:
    def _function(self, tree, name):
        return next(
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        )

    def _calls(self, node):
        return [n for n in ast.walk(node) if isinstance(n, ast.Call)]

    def _call_named(self, node, name):
        for call in self._calls(node):
            if isinstance(call.func, ast.Name) and call.func.id == name:
                return call
            if isinstance(call.func, ast.Attribute) and call.func.attr == name:
                return call
        return None


class MainWebRtcWiringTests(unittest.TestCase, _AstHelpers):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(MAIN_PY.read_text())

    def test_on_stream_ended_clears_streaming_and_logs_resume(self):
        fn = self._function(self.tree, 'on_stream_ended')
        self.assertIsInstance(fn, ast.AsyncFunctionDef)
        clears = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Attribute) and t.attr == 'streaming'
                for t in n.targets
            )
            and isinstance(n.value, ast.Constant) and n.value.value is False
        ]
        self.assertEqual(len(clears), 1)
        log_texts = []
        for n in self._calls(fn):
            if not (isinstance(n.func, ast.Attribute) and n.func.attr == 'info' and n.args):
                continue
            arg = n.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                log_texts.append(arg.value)
            elif isinstance(arg, ast.JoinedStr):
                log_texts.append(''.join(
                    part.value for part in arg.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                ))
        self.assertTrue(any('Resuming snapshots' in t for t in log_texts))

    def test_prusa_webrtc_receives_on_stream_ended(self):
        constructor = self._call_named(self.tree, 'PrusaWebRTC')
        self.assertIsNotNone(constructor)
        kwargs = {kw.arg: kw.value for kw in constructor.keywords}
        self.assertIn('on_stream_ended', kwargs)
        self.assertIsInstance(kwargs['on_stream_ended'], ast.Name)
        self.assertEqual(kwargs['on_stream_ended'].id, 'on_stream_ended')

    def test_find_candidate_delegates_to_proto_helper(self):
        fn = self._function(self.tree, '_find_candidate')
        delegated = self._call_named(fn, 'find_webrtc_candidate')
        self.assertIsNotNone(delegated)
        statements = [
            n for n in fn.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))
        ]
        self.assertEqual(len(statements), 1)
        self.assertIsInstance(statements[0], ast.Return)


class WebRtcModuleWiringTests(unittest.TestCase, _AstHelpers):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(WEBRTC_PY.read_text())

    def test_connects_ice_connection_state_change(self):
        connects = [
            n for n in self._calls(self.tree)
            if isinstance(n.func, ast.Attribute) and n.func.attr == 'connect'
            and n.args and isinstance(n.args[0], ast.Constant)
            and n.args[0].value == 'on-ice-connection-state-change'
        ]
        self.assertEqual(len(connects), 1)

    def test_ice_state_handler_uses_lifecycle_policy(self):
        fn = self._function(self.tree, '_on_ice_state_change')
        self.assertIsNotNone(self._call_named(fn, 'end_reason'))
        self.assertIsNotNone(self._call_named(fn, '_notify_stream_ended'))
        self.assertTrue(any(
            isinstance(n, ast.Attribute) and n.attr == 'ICE_CONNECTED'
            for n in ast.walk(fn)
        ))

    def test_notify_stream_ended_is_idempotent_and_marshals_to_loop(self):
        fn = self._function(self.tree, '_notify_stream_ended')
        guard = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.Attribute)
            and n.test.attr == '_ended_notified'
        ]
        self.assertEqual(len(guard), 1)
        self.assertIsNotNone(self._call_named(fn, 'call_soon_threadsafe'))

    def test_teardown_cancels_pending_timeouts(self):
        fn = self._function(self.tree, '_teardown')
        cancelled = {
            n.args[0].value
            for n in self._calls(fn)
            if isinstance(n.func, ast.Attribute) and n.func.attr == '_cancel_timeout'
            and n.args and isinstance(n.args[0], ast.Constant)
        }
        self.assertIn('_disconnect_timeout_id', cancelled)
        self.assertIn('_connect_watchdog_id', cancelled)


if __name__ == '__main__':
    unittest.main()
