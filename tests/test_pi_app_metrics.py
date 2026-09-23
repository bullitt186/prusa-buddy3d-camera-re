"""WP-R2 (AC-24): runtime metric helpers for the MQTT state document.

Stdlib-only. Every helper is exercised against ``tempfile`` paths/mounts: a
present value, a missing file, and malformed content. No real ``/data``,
``/proc``, ``/sys`` read is required and nothing raises.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import app_metrics  # noqa: E402


class StorageFreeBytesTests(unittest.TestCase):
    def test_reads_a_real_mount(self):
        with tempfile.TemporaryDirectory() as d:
            value = app_metrics.storage_free_bytes(d)
            self.assertIsInstance(value, int)
            self.assertGreaterEqual(value, 0)

    def test_missing_mount_is_none(self):
        self.assertIsNone(app_metrics.storage_free_bytes('/nonexistent/mount/xyz'))

    def test_bad_type_is_none(self):
        self.assertIsNone(app_metrics.storage_free_bytes(None))


class WifiRssiTests(unittest.TestCase):
    LINE = ' wlan0: 0000   54.  -56.  -256        0      0      0      0  0  0\n'

    def test_reads_raw_dbm_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'wireless')
            with open(path, 'w') as f:
                f.write('Inter-| sta-|   Quality        |   Discarded packets\n')
                f.write(self.LINE)
            self.assertEqual(app_metrics.wifi_rssi_dbm(path), -56)

    def test_missing_file_is_none(self):
        self.assertIsNone(app_metrics.wifi_rssi_dbm('/nonexistent/wireless'))

    def test_garbage_file_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'wireless')
            with open(path, 'w') as f:
                f.write('no wireless interface here\n')
            self.assertIsNone(app_metrics.wifi_rssi_dbm(path))


class CpuTemperatureTests(unittest.TestCase):
    def test_millidegrees_to_celsius(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'temp')
            with open(path, 'w') as f:
                f.write('45000\n')
            self.assertAlmostEqual(app_metrics.cpu_temperature_c(path), 45.0)

    def test_missing_file_is_none(self):
        self.assertIsNone(app_metrics.cpu_temperature_c('/nonexistent/temp'))

    def test_garbage_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'temp')
            with open(path, 'w') as f:
                f.write('not-a-number\n')
            self.assertIsNone(app_metrics.cpu_temperature_c(path))


class UptimeTests(unittest.TestCase):
    def test_parses_first_field_as_int(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'uptime')
            with open(path, 'w') as f:
                f.write('12345.67 98765.43\n')
            self.assertEqual(app_metrics.uptime_seconds(path), 12345)

    def test_missing_file_is_none(self):
        self.assertIsNone(app_metrics.uptime_seconds('/nonexistent/uptime'))

    def test_garbage_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'uptime')
            with open(path, 'w') as f:
                f.write('\n')
            self.assertIsNone(app_metrics.uptime_seconds(path))


class MetricsProviderTests(unittest.TestCase):
    def test_returns_only_available_keys(self):
        with tempfile.TemporaryDirectory() as d:
            wireless = os.path.join(d, 'wireless')
            with open(wireless, 'w') as f:
                f.write(' wlan0: 0000   54.  -60.  -256 0 0 0 0 0 0\n')
            thermal = os.path.join(d, 'temp')
            with open(thermal, 'w') as f:
                f.write('50000\n')
            uptime = os.path.join(d, 'uptime')
            with open(uptime, 'w') as f:
                f.write('100.0 50.0\n')
            metrics = app_metrics.metrics_provider(
                storage_mount=d,
                wireless_path=wireless,
                thermal_path=thermal,
                uptime_path=uptime,
            )
        self.assertEqual(set(metrics), {
            'storage_free_bytes', 'wifi_rssi_dbm',
            'cpu_temperature_c', 'uptime_seconds',
        })
        self.assertEqual(metrics['wifi_rssi_dbm'], -60)
        self.assertAlmostEqual(metrics['cpu_temperature_c'], 50.0)
        self.assertEqual(metrics['uptime_seconds'], 100)

    def test_omits_unavailable_keys(self):
        metrics = app_metrics.metrics_provider(
            storage_mount='/nonexistent/mount/xyz',
            wireless_path='/nonexistent/wireless',
            thermal_path='/nonexistent/temp',
            uptime_path='/nonexistent/uptime',
        )
        self.assertEqual(metrics, {})


if __name__ == '__main__':
    unittest.main()
