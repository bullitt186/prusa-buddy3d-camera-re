"""WP-R1 AC-17: setup-hotspot CLI (hotspot_ctl).

Host-only. Every case injects a fake controller; the import-safety test patches
``subprocess.run`` so no real ``nmcli`` command can run.
"""
import importlib
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import hotspot  # noqa: E402
import hotspot_ctl  # noqa: E402
import provisioning  # noqa: E402

DEVICE_ID = 'AA:BB:CC:DD:EE:FF'


class FakeController:
    """Records calls and returns canned :class:`hotspot.HotspotResult` objects."""

    def __init__(self, *, active=False, ssid='', start_ok=True, stop_ok=True,
                 raise_on=None):
        self.active = active
        self.ssid = ssid
        self.start_ok = start_ok
        self.stop_ok = stop_ok
        self.raise_on = raise_on
        self.calls = []

    def status(self, ifname=hotspot.DEFAULT_IFNAME, runner=None):
        self.calls.append(('status', ifname))
        if self.raise_on == 'status':
            raise RuntimeError('boom')
        return hotspot.HotspotResult(
            True, '', active=self.active, ssid=self.ssid, ifname=ifname
        )

    def start(self, ssid, ifname=hotspot.DEFAULT_IFNAME, runner=None):
        self.calls.append(('start', ssid, ifname, runner))
        if self.raise_on == 'start':
            raise RuntimeError('boom')
        return hotspot.HotspotResult(
            self.start_ok,
            '' if self.start_ok else 'nmcli hotspot start failed (exit 1)',
            active=self.start_ok,
            ssid=ssid,
            ifname=ifname,
        )

    def stop(self, ifname=hotspot.DEFAULT_IFNAME, runner=None):
        self.calls.append(('stop', ifname))
        if self.raise_on == 'stop':
            raise RuntimeError('boom')
        return hotspot.HotspotResult(
            self.stop_ok,
            '' if self.stop_ok else 'nmcli device disconnect failed (exit 1)',
            ifname=ifname,
        )


class ImportSafetyTests(unittest.TestCase):
    def test_import_does_not_execute_subprocess(self):
        with patch.object(
            subprocess, 'run', side_effect=AssertionError('subprocess on import')
        ):
            importlib.reload(hotspot_ctl)
        self.assertTrue(callable(hotspot_ctl.start))
        self.assertTrue(callable(hotspot_ctl.stop))


class StartTests(unittest.TestCase):
    def test_start_open_hotspot_without_psk(self):
        controller = FakeController(active=False)
        result = hotspot_ctl.start(
            controller, device_id=DEVICE_ID, runner=None
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.ssid, provisioning.setup_ssid(DEVICE_ID))
        # No password/PSK is ever passed (open AP, source §4.3).
        start_calls = [call for call in controller.calls if call[0] == 'start']
        self.assertEqual(len(start_calls), 1)
        self.assertEqual(start_calls[0][1], provisioning.setup_ssid(DEVICE_ID))

    def test_start_is_idempotent_when_ap_already_active(self):
        ssid = provisioning.setup_ssid(DEVICE_ID)
        controller = FakeController(active=True, ssid=ssid)
        result = hotspot_ctl.start(controller, device_id=DEVICE_ID)
        self.assertTrue(result.ok)
        self.assertTrue(result.active)
        self.assertEqual(result.ssid, ssid)
        self.assertNotIn('start', [call[0] for call in controller.calls])

    def test_start_replaces_a_different_active_ap(self):
        controller = FakeController(active=True, ssid='SomeOtherAP')
        result = hotspot_ctl.start(controller, device_id=DEVICE_ID)
        self.assertTrue(result.ok)
        kinds = [call[0] for call in controller.calls]
        self.assertIn('stop', kinds)
        self.assertIn('start', kinds)
        self.assertLess(kinds.index('stop'), kinds.index('start'))

    def test_start_without_device_id_fails_without_touching_the_ap(self):
        controller = FakeController()
        result = hotspot_ctl.start(controller, device_id='')
        self.assertFalse(result.ok)
        self.assertEqual(controller.calls, [])

    def test_start_uses_resolved_device_id_by_default(self):
        controller = FakeController()
        with patch.object(
            provisioning, 'resolve_device_id', return_value=DEVICE_ID
        ):
            result = hotspot_ctl.start(controller)
        self.assertTrue(result.ok)
        self.assertEqual(result.ssid, provisioning.setup_ssid(DEVICE_ID))

    def test_start_failure_is_reported(self):
        controller = FakeController(start_ok=False)
        result = hotspot_ctl.start(controller, device_id=DEVICE_ID)
        self.assertFalse(result.ok)

    def test_start_controller_exception_is_reported(self):
        controller = FakeController(raise_on='start')
        result = hotspot_ctl.start(controller, device_id=DEVICE_ID)
        self.assertFalse(result.ok)

    def test_start_status_failure_still_attempts_start(self):
        controller = FakeController(raise_on='status')
        result = hotspot_ctl.start(controller, device_id=DEVICE_ID)
        self.assertTrue(result.ok)
        self.assertIn('start', [call[0] for call in controller.calls])


class StopTests(unittest.TestCase):
    def test_stop_calls_controller_stop(self):
        controller = FakeController()
        result = hotspot_ctl.stop(controller)
        self.assertTrue(result.ok)
        self.assertEqual(controller.calls, [('stop', hotspot.DEFAULT_IFNAME)])

    def test_stop_failure_is_reported(self):
        controller = FakeController(stop_ok=False)
        result = hotspot_ctl.stop(controller)
        self.assertFalse(result.ok)
        self.assertIn('exit 1', result.reason)

    def test_stop_controller_exception_is_reported(self):
        controller = FakeController(raise_on='stop')
        result = hotspot_ctl.stop(controller)
        self.assertFalse(result.ok)


class MainTests(unittest.TestCase):
    def test_main_start_success(self):
        with patch.object(
            hotspot_ctl, 'start', return_value=hotspot.HotspotResult(True)
        ):
            self.assertEqual(hotspot_ctl.main(['start']), 0)

    def test_main_stop_success(self):
        with patch.object(
            hotspot_ctl, 'stop', return_value=hotspot.HotspotResult(True)
        ):
            self.assertEqual(hotspot_ctl.main(['stop']), 0)

    def test_main_start_failure_exits_nonzero(self):
        with patch.object(
            hotspot_ctl, 'start',
            return_value=hotspot.HotspotResult(False, 'setup SSID unavailable'),
        ):
            self.assertEqual(hotspot_ctl.main(['start']), 1)

    def test_main_stop_failure_exits_nonzero(self):
        with patch.object(
            hotspot_ctl, 'stop',
            return_value=hotspot.HotspotResult(False, 'stop failed'),
        ):
            self.assertEqual(hotspot_ctl.main(['stop']), 1)


if __name__ == '__main__':
    unittest.main()
