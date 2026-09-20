import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import quality  # noqa: E402
import quality_control  # noqa: E402
from state import CameraState  # noqa: E402


class QualityControlTestCase(unittest.TestCase):
    """Isolate the live/persisted env files from the host's real /etc paths."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_envs = (quality.QUALITY_ENV, quality.QUALITY_LIVE_ENV)
        quality.QUALITY_ENV = os.path.join(self._tmp.name, 'quality.env')
        quality.QUALITY_LIVE_ENV = os.path.join(self._tmp.name, 'quality.live.env')

    def tearDown(self):
        quality.QUALITY_ENV, quality.QUALITY_LIVE_ENV = self._old_envs
        self._tmp.cleanup()


class LiveApplyFailureTests(QualityControlTestCase):
    """Review fix 1: a failed restart must not leave the failed tier live."""

    def test_failed_restart_removes_new_override_when_none_existed(self):
        state = CameraState()  # FHD
        restart_calls = []

        def failing_restart():
            restart_calls.append(1)
            return 1

        self.assertFalse(quality_control.apply_live_quality(5, state, failing_restart))
        self.assertEqual(state.quality, 3)
        self.assertEqual(restart_calls, [1])
        self.assertFalse(os.path.exists(quality.QUALITY_LIVE_ENV))

    def test_failed_restart_restores_previous_live_override(self):
        quality.write_live(1)  # previous live override = SD
        previous = quality.read_live()
        state = CameraState()  # in-memory FHD

        self.assertFalse(quality_control.apply_live_quality(7, state, lambda: 1))

        self.assertEqual(state.quality, 3)
        self.assertEqual(quality.read_live(), previous)
        self.assertEqual(quality.read_current()[0], 1)

    def test_successful_restart_updates_state_and_keeps_override(self):
        state = CameraState()
        self.assertTrue(quality_control.apply_live_quality(6, state, lambda: 0))
        self.assertEqual(state.quality, 2)
        self.assertEqual(quality.read_current()[0], 2)

    def test_restart_exception_restores_previous_state(self):
        quality.write_live(1)  # previous live override = SD
        previous = quality.read_live()
        state = CameraState()  # in-memory FHD

        def raising_restart():
            raise RuntimeError('systemctl exploded')

        self.assertFalse(quality_control.apply_live_quality(7, state, raising_restart))
        self.assertEqual(state.quality, 3)
        self.assertEqual(quality.read_live(), previous)

    def test_live_write_failure_leaves_no_override_and_does_not_restart(self):
        state = CameraState()
        restart_calls = []
        original = quality.write_live

        def failing_write(_qenum):
            raise OSError('read-only filesystem')

        quality.write_live = failing_write
        try:
            self.assertFalse(
                quality_control.apply_live_quality(5, state, lambda: restart_calls.append(1) or 0)
            )
        finally:
            quality.write_live = original

        self.assertEqual(state.quality, 3)
        self.assertEqual(restart_calls, [])
        self.assertFalse(os.path.exists(quality.QUALITY_LIVE_ENV))

    def test_unknown_raw_byte_does_not_write_or_restart(self):
        state = CameraState()
        restart_calls = []
        self.assertFalse(
            quality_control.apply_live_quality(99, state, lambda: restart_calls.append(1) or 0)
        )
        self.assertEqual(state.quality, 3)
        self.assertEqual(restart_calls, [])
        self.assertFalse(os.path.exists(quality.QUALITY_LIVE_ENV))


class HandleQualityTests(QualityControlTestCase):
    """GAP-QUALITY-02 acceptance: live always; persist only on flag."""

    def _live_apply(self, state, returncode):
        def live(raw):
            return quality_control.apply_live_quality(raw, state, lambda: returncode)
        return live

    def test_persist_flag_zero_applies_live_once_without_persistence(self):
        state = CameraState()
        live_calls = []
        persist_calls = []

        def live(raw):
            live_calls.append(raw)
            return quality_control.apply_live_quality(raw, state, lambda: 0)

        ok = quality_control.handle_quality(5, False, live, persist_calls.append)

        self.assertTrue(ok)
        self.assertEqual(live_calls, [5])
        self.assertEqual(persist_calls, [])
        self.assertEqual(state.quality, 1)                 # SD
        self.assertFalse(os.path.exists(quality.QUALITY_ENV))

    def test_persist_flag_one_applies_live_once_and_persists_once(self):
        state = CameraState()
        live_calls = []
        persist_calls = []

        def live(raw):
            live_calls.append(raw)
            return quality_control.apply_live_quality(raw, state, lambda: 0)

        def persist(qenum):
            persist_calls.append(qenum)
            quality_control.persist_quality(qenum)

        ok = quality_control.handle_quality(6, True, live, persist)

        self.assertTrue(ok)
        self.assertEqual(live_calls, [6])
        self.assertEqual(persist_calls, [2])               # HD enum
        self.assertEqual(state.quality, 2)
        self.assertEqual(quality.read_current()[0], 2)

    def test_live_apply_failure_updates_nothing_and_does_not_persist(self):
        state = CameraState()  # FHD
        persist_calls = []

        ok = quality_control.handle_quality(5, True, self._live_apply(state, 1), persist_calls.append)

        self.assertFalse(ok)
        self.assertEqual(state.quality, 3)                 # unchanged
        self.assertEqual(persist_calls, [])                # no persisted write
        self.assertFalse(os.path.exists(quality.QUALITY_ENV))
        # The failed tier must not survive in the live override either.
        self.assertFalse(os.path.exists(quality.QUALITY_LIVE_ENV))

    def test_unknown_raw_byte_is_rejected_before_any_effect(self):
        state = CameraState()
        live_calls = []
        persist_calls = []

        ok = quality_control.handle_quality(42, True, lambda raw: live_calls.append(raw) or True, persist_calls.append)

        self.assertFalse(ok)
        self.assertEqual(live_calls, [])
        self.assertEqual(persist_calls, [])
        self.assertEqual(state.quality, 3)


class RestartServicesTests(unittest.TestCase):
    @patch('quality_control.subprocess.run')
    def test_restarts_shared_source_and_ha_then_try_restarts_prusa(self, run):
        run.side_effect = [
            type('Result', (), {'returncode': 0})(),
            type('Result', (), {'returncode': 0})(),
        ]

        self.assertEqual(quality_control.restart_services(), 0)

        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], [
            'sudo', 'systemctl', 'restart',
            'rpicam-source.service', 'prusa-ha-rtsp.service',
        ])
        self.assertEqual(run.call_args_list[1].args[0], [
            'sudo', 'systemctl', 'try-restart', 'prusa-rtsp.service',
        ])

    @patch('quality_control.subprocess.run')
    def test_any_restart_failure_is_returned(self, run):
        run.side_effect = [
            type('Result', (), {'returncode': 3})(),
            type('Result', (), {'returncode': 0})(),
        ]
        self.assertEqual(quality_control.restart_services(), 3)

        run.reset_mock()
        run.side_effect = [
            type('Result', (), {'returncode': 0})(),
            type('Result', (), {'returncode': 4})(),
        ]
        self.assertEqual(quality_control.restart_services(), 4)


if __name__ == '__main__':
    unittest.main()
