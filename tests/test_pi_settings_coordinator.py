"""WP-1 AC-4/AC-5: shared settings coordinator contract.

Pins the coordinator as the single mutation path: validation, serialization,
live apply, persistence, error reporting, and authoritative-state publication.
Uses fakes only — no systemd, no ``/data``, no ``/etc`` writes, and ``main.py``
is inspected statically (never imported, since it pulls aiohttp/socketio/gi).
"""
import ast
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import quality_control  # noqa: E402
import rtsp_control  # noqa: E402
import settings_store  # noqa: E402
from settings_coordinator import SettingsCoordinator, SettingsResult  # noqa: E402
from state import CameraState, RAW_TO_ENUM  # noqa: E402


MAIN_PY = PI_DIR / 'main.py'


class Recorder:
    """Minimal callable double that records calls and returns a fixed value."""

    def __init__(self, return_value=None):
        self.return_value = return_value
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.return_value


class LiveQuality:
    """Fake live-apply: records raw bytes, updates state only on success."""

    def __init__(self, state, return_value=True, update=True):
        self.state = state
        self.return_value = return_value
        self.update = update
        self.calls = []

    def __call__(self, raw):
        self.calls.append(raw)
        if self.return_value and self.update and raw in RAW_TO_ENUM:
            self.state.quality = RAW_TO_ENUM[raw]
        return self.return_value


class CoordinatorTestCase(unittest.TestCase):
    """Builds a coordinator with recording fakes and no filesystem effects."""

    def setUp(self):
        # rtsp_control.apply_mode persists the mode file; keep it off the host.
        self._write_mode = patch.object(rtsp_control, 'write_mode', return_value=True)
        self._write_mode.start()
        self.addCleanup(self._write_mode.stop)

    def _make(self, state=None, live=None, **overrides):
        state = state or CameraState()
        persist = Recorder(True)
        publish = Recorder(None)
        qpersist = Recorder(None)
        if live is None:
            live = LiveQuality(state)
        kwargs = dict(
            persist=persist,
            publish=publish,
            quality_apply=live,
            quality_persist=qpersist,
            rtsp_start=Recorder(None),
            rtsp_stop=Recorder(None),
            webrtc_start=Recorder(None),
            webrtc_stop=Recorder(None),
        )
        kwargs.update(overrides)
        coordinator = SettingsCoordinator(state, **kwargs)
        return state, coordinator, persist, publish, qpersist, live


class QualityTests(CoordinatorTestCase):
    """AC-5: scoped quality lock and failed applies change nothing."""

    def test_success_applies_live_persists_and_publishes(self):
        state, coordinator, persist, publish, qpersist, live = self._make()

        result = coordinator.set_quality(5)

        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.changed, ['quality_tier'])
        self.assertEqual(live.calls, [5])
        self.assertEqual(len(persist.calls), 1)
        self.assertEqual(len(publish.calls), 1)
        self.assertEqual(qpersist.calls, [((1,), {})])
        self.assertEqual(state.quality, 1)
        self.assertEqual(result.state['quality_tier'], 1)

    def test_turn_lock_rejects_without_persist_or_restart(self):
        state = CameraState(quality=1)
        state.turn_online = True
        state, coordinator, persist, publish, qpersist, live = self._make(state=state)
        before = state.persistable_state()

        result = coordinator.set_quality(7)  # raise while a TURN client is online

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, quality_control.TURN_QUALITY_LOCK_LOG)
        self.assertEqual(live.calls, [])
        self.assertEqual(persist.calls, [])
        self.assertEqual(publish.calls, [])
        self.assertEqual(qpersist.calls, [])
        self.assertEqual(state.persistable_state(), before)
        self.assertEqual(result.state, before)

    def test_unknown_raw_byte_rejected_with_no_side_effects(self):
        state, coordinator, persist, publish, qpersist, live = self._make()
        before = state.persistable_state()

        result = coordinator.set_quality(99)

        self.assertFalse(result.ok)
        self.assertEqual(live.calls, [])
        self.assertEqual(persist.calls, [])
        self.assertEqual(publish.calls, [])
        self.assertEqual(state.persistable_state(), before)
        self.assertEqual(result.state, before)

    def test_live_apply_failure_rejected_without_persist(self):
        state = CameraState()
        _, coordinator, persist, publish, qpersist, live = self._make(
            state=state, live=LiveQuality(state, return_value=False)
        )
        before = state.persistable_state()

        result = coordinator.set_quality(5)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'live apply failed')
        self.assertEqual(live.calls, [5])
        self.assertEqual(persist.calls, [])
        self.assertEqual(publish.calls, [])
        self.assertEqual(state.persistable_state(), before)

    def test_live_only_path_republishes_without_state_json(self):
        state, coordinator, persist, publish, qpersist, live = self._make()

        result = coordinator.set_quality(5, persist=False)

        self.assertTrue(result.ok, result.reason)
        self.assertEqual(live.calls, [5])
        self.assertEqual(persist.calls, [])      # no durable state.json write
        self.assertEqual(len(publish.calls), 1)  # authoritative republish still happens
        self.assertEqual(qpersist.calls, [])
        self.assertEqual(state.quality, 1)


class OtherSetterTests(CoordinatorTestCase):
    """Each remaining setter validates, commits once, or rejects cleanly."""

    def test_valid_setters_persist_and_publish_once(self):
        cases = [
            ('snapshot_upload', lambda c: c.set_snapshot_upload(False),
             'snapshot_upload_enabled'),
            ('snapshot_interval', lambda c: c.set_snapshot_interval(30),
             'snapshot_interval'),
            ('timelapse_enabled', lambda c: c.set_timelapse_enabled('timelapse_enable'),
             'timelapse_enabled'),
            ('timelapse_interval', lambda c: c.set_timelapse_interval(30),
             'timelapse_interval'),
            ('timelapse_fps', lambda c: c.set_timelapse_fps(15),
             'timelapse_fps'),
            ('rtsp_mode', lambda c: c.set_rtsp_mode(2),
             'rtsp_mode'),
            ('webrtc_mode', lambda c: c.set_webrtc_mode(0),
             'webrtc_mode'),
            ('camera_name', lambda c: c.set_camera_name('Kitchen'),
             'camera_name'),
        ]
        for name, invoke, key in cases:
            with self.subTest(setter=name):
                state, coordinator, persist, publish, _, _ = self._make()
                result = invoke(coordinator)
                self.assertTrue(result.ok, result.reason)
                self.assertEqual(result.changed, [key])
                self.assertEqual(len(persist.calls), 1)
                self.assertEqual(len(publish.calls), 1)
                self.assertEqual(result.state, state.persistable_state())

    def test_invalid_setters_reject_without_side_effects(self):
        cases = [
            ('snapshot_upload_type', lambda c: c.set_snapshot_upload(1)),
            ('snapshot_interval_range', lambda c: c.set_snapshot_interval(5)),
            ('snapshot_interval_type', lambda c: c.set_snapshot_interval('30')),
            ('timelapse_enabled_action', lambda c: c.set_timelapse_enabled('bogus')),
            ('timelapse_interval_range', lambda c: c.set_timelapse_interval(0)),
            ('timelapse_fps_range', lambda c: c.set_timelapse_fps(99)),
            ('rtsp_mode_range', lambda c: c.set_rtsp_mode(3)),
            ('webrtc_mode_range', lambda c: c.set_webrtc_mode(2)),
            ('camera_name_empty', lambda c: c.set_camera_name('')),
            ('camera_name_type', lambda c: c.set_camera_name(None)),
        ]
        for name, invoke in cases:
            with self.subTest(setter=name):
                state, coordinator, persist, publish, _, _ = self._make()
                before = state.persistable_state()

                result = invoke(coordinator)

                self.assertFalse(result.ok, name)
                self.assertIsInstance(result.reason, str)
                self.assertEqual(persist.calls, [])
                self.assertEqual(publish.calls, [])
                self.assertEqual(state.persistable_state(), before)
                self.assertEqual(result.state, before)

    def test_never_raises_on_bad_input(self):
        state, coordinator, persist, publish, _, _ = self._make()
        before = state.persistable_state()

        # Unhashable/None quality bytes and wrong-typed settings must reject,
        # not raise, and must leave the authoritative state untouched.
        for invoke in (
            lambda: coordinator.set_quality(None),
            lambda: coordinator.set_quality([]),
            lambda: coordinator.set_snapshot_interval(object()),
            lambda: coordinator.set_timelapse_fps(object()),
            lambda: coordinator.set_rtsp_mode(object()),
            lambda: coordinator.set_webrtc_mode(object()),
            lambda: coordinator.set_camera_name(object()),
        ):
            result = invoke()
            self.assertIsInstance(result, SettingsResult)
            self.assertFalse(result.ok)

        self.assertEqual(persist.calls, [])
        self.assertEqual(publish.calls, [])
        self.assertEqual(state.persistable_state(), before)

    def test_every_control_method_returns_result_with_documented_state(self):
        state, coordinator, _, _, _, _ = self._make()
        documented = set(state.persistable_state())

        results = [
            coordinator.set_quality(5),
            coordinator.set_snapshot_upload(False),
            coordinator.set_snapshot_interval(30),
            coordinator.set_timelapse_enabled('timelapse_enable'),
            coordinator.set_timelapse_interval(30),
            coordinator.set_timelapse_fps(15),
            coordinator.set_rtsp_mode(2),
            coordinator.set_webrtc_mode(0),
            coordinator.set_camera_name('Kitchen'),
            coordinator.restore({'quality_tier': 2}),
        ]

        for result in results:
            self.assertIsInstance(result, SettingsResult)
            self.assertEqual(set(result.state), documented)


class RestoreAndPersistenceTests(CoordinatorTestCase):
    def test_restore_applies_values_and_does_not_persist(self):
        state = CameraState()
        persist = Recorder(True)
        publish = Recorder(None)
        coordinator = SettingsCoordinator(state, persist=persist, publish=publish)

        result = coordinator.restore({'quality_tier': 2, 'camera_name': 'Kitchen'})

        self.assertTrue(result.ok)
        self.assertEqual(result.changed, ['quality_tier', 'camera_name'])
        self.assertEqual(state.quality, 2)
        self.assertEqual(state.camera_name, 'Kitchen')
        self.assertEqual(persist.calls, [])          # restore must not rewrite the file
        self.assertEqual(len(publish.calls), 1)
        self.assertEqual(result.state['quality_tier'], 2)

    def test_default_persist_is_noop_when_data_unavailable(self):
        state = CameraState()
        coordinator = SettingsCoordinator(state)

        with patch.object(settings_store, 'available', return_value=False), \
                patch.object(settings_store, 'save') as save:
            result = coordinator.set_camera_name('Kitchen')

        self.assertTrue(result.ok, result.reason)
        save.assert_not_called()


class MainWiringStaticTests(unittest.TestCase):
    """main.py is not importable on the host; inspect it as text/AST."""

    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_PY.read_text()
        cls.tree = ast.parse(cls.source)

    def _functions(self, name):
        return [
            node for node in ast.walk(self.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ]

    def test_references_settings_coordinator(self):
        self.assertIn('SettingsCoordinator', self.source)
        self.assertIn('SettingsCoordinator(', self.source)

    def test_no_direct_save_persisted_state_calls(self):
        calls = [
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == '_save_persisted_state'
        ]
        self.assertEqual(calls, [])

    def test_command_paths_route_through_coordinator(self):
        for name in ('handle_event', 'dispatch_trigger_action'):
            functions = self._functions(name)
            self.assertTrue(functions, name)
            for function in functions:
                calls = [n for n in ast.walk(function) if isinstance(n, ast.Call)]
                self.assertTrue(
                    any(
                        isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == 'coordinator'
                        for call in calls
                    ),
                    f'{name} does not route through the coordinator',
                )

    def test_main_does_not_write_settings_store(self):
        calls = [
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'save'
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == 'settings_store'
        ]
        self.assertEqual(calls, [])


class SettingsStoreWriterTests(unittest.TestCase):
    """AC-4: only the coordinator (plus importer/restore) writes state.json."""

    def test_only_coordinator_writes_settings_store(self):
        allowed = {
            'settings_store.py',
            'legacy_import.py',
            'persist_restore.py',
            'settings_coordinator.py',
        }
        callers = set()
        for path in sorted(PI_DIR.glob('*.py')):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (isinstance(func, ast.Attribute) and func.attr == 'save'
                        and isinstance(func.value, ast.Name)
                        and func.value.id == 'settings_store'):
                    callers.add(path.name)

        self.assertTrue(callers <= allowed, f'unexpected writers: {sorted(callers - allowed)}')
        self.assertIn('settings_coordinator.py', callers)


if __name__ == '__main__':
    unittest.main()
