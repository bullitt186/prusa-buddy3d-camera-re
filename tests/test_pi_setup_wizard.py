"""WP-3c AC-17: first-boot captive-portal wizard core (setup_wizard).

Host-only. Every path is a ``tempfile`` path, ``/data`` availability is patched,
the hotspot is a fake that records call order, and the camera target is an
injected callback. No real file outside the temp directory, no subprocess, no
network, no root.
"""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import admin_auth  # noqa: E402
import config_schema  # noqa: E402
import hotspot  # noqa: E402
import privileged  # noqa: E402
import provisioning  # noqa: E402
import settings_store  # noqa: E402
import setup_wizard  # noqa: E402

SSID = 'HomeNet'
PSK = 'sup3rsecret'
TOKEN = 'tok-abc-123'
PASSWORD = 'correct-horse'


class FakeHotspot:
    """Records ``stop``/``start`` calls in a shared event list (never runs nmcli)."""

    def __init__(self, events, ok=True, reason='', start_ok=None):
        self.events = events
        self.ok = ok
        self.start_ok = ok if start_ok is None else start_ok
        self.reason = reason
        self.ifnames = []
        self.start_ssids = []

    def stop(self, ifname=None):
        self.events.append('hotspot_stop')
        self.ifnames.append(ifname)
        return hotspot.HotspotResult(self.ok, self.reason, active=False, ifname=ifname or '')

    def start(self, ssid, password=None, ifname=None, **kwargs):
        self.events.append('hotspot_start')
        self.start_ssids.append(ssid)
        return hotspot.HotspotResult(
            self.start_ok, self.reason, active=self.start_ok, ssid=ssid,
            ifname=ifname or '',
        )


class WizardTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.device_path = os.path.join(self.root, 'device.toml')
        self.secrets_path = os.path.join(self.root, 'secrets.toml')
        self.provisioning_path = os.path.join(self.root, 'provisioning.json')

        available = patch.object(settings_store, 'available', return_value=True)
        available.start()
        self.addCleanup(available.stop)

        self.events = []
        self.hotspot = FakeHotspot(self.events)
        self.start_camera = lambda: self.events.append('camera_start')
        self.probe = SimpleNamespace(ok=True, reason='', sensors=1)

    def make_session(self, **kwargs):
        defaults = dict(
            device_id='AA:BB:CC:DD:EE:FF',
            device_path=self.device_path,
            secrets_path=self.secrets_path,
            provisioning_path=self.provisioning_path,
            storage_ready=True,
            probe_result=self.probe,
            hotspot_controller=self.hotspot,
            start_camera=self.start_camera,
        )
        defaults.update(kwargs)
        return setup_wizard.WizardSession(**defaults)

    def walk(self, session, ssid=SSID, psk=PSK, token=TOKEN, password=PASSWORD,
             fingerprint='', mqtt=None):
        """Submit steps 1-8 with valid data; returns the list of results."""
        if mqtt is None:
            mqtt = {'enabled': False}
        return [
            session.submit('status', {}),
            session.submit('imager_prefill', {'wifi_ssid': ssid}),
            session.submit('wifi', {'ssid': ssid, 'psk': psk}),
            session.submit('prusa_token', {'token': token}),
            session.submit('fingerprint', {'fingerprint': fingerprint}),
            session.submit('admin_password', {'password': password, 'confirm': password}),
            session.submit('mqtt', mqtt),
            session.submit('summary', {}),
        ]

    def persist(self, session):
        return session.submit('persist', {})

    def read_text(self, path):
        with open(path, encoding='utf-8') as f:
            return f.read()


class StepOrderTests(WizardTestBase):
    def test_step_order_matches_ac17(self):
        self.assertEqual(
            setup_wizard.STEP_ORDER,
            ('status', 'imager_prefill', 'wifi', 'prusa_token', 'fingerprint',
             'admin_password', 'mqtt', 'summary', 'persist', 'finish'),
        )
        self.assertEqual(len(setup_wizard.STEP_ORDER), 10)
        for step in setup_wizard.STEP_ORDER:
            self.assertIn(step, setup_wizard.STEP_TITLES)

    def test_unknown_step_is_rejected(self):
        session = self.make_session()
        result = session.set_step('nope')
        self.assertFalse(result.ok)
        self.assertEqual(session.step, 'status')
        result = session.submit('nope', {})
        self.assertFalse(result.ok)
        self.assertEqual(session.step, 'status')

    def test_non_mapping_step_data_is_rejected(self):
        session = self.make_session()
        for data in (None, 'x', 1, []):
            with self.subTest(data=data):
                result = session.submit('status', data)
                self.assertFalse(result.ok)
                self.assertNotIn('status', session.completed)


class StatusTests(WizardTestBase):
    def test_status_records_storage_and_probe(self):
        session = self.make_session()
        result = session.submit('status', {})
        self.assertTrue(result.ok)
        self.assertTrue(session.status['storage_ready'])
        self.assertTrue(session.status['camera_ok'])
        self.assertEqual(session.status['sensors'], 1)

    def test_failed_probe_is_recorded_not_rejected(self):
        probe = SimpleNamespace(ok=False, reason='no CSI sensor detected', sensors=0)
        session = self.make_session(probe_result=probe)
        result = session.submit('status', {})
        self.assertTrue(result.ok)
        self.assertFalse(session.status['camera_ok'])
        self.assertEqual(session.status['camera_reason'], 'no CSI sensor detected')

    def test_injected_probe_callable_is_used(self):
        calls = []

        def probe():
            calls.append(True)
            return self.probe

        session = self.make_session(probe=probe, probe_result=None)
        self.assertTrue(session.submit('status', {}).ok)
        self.assertEqual(calls, [True])


class ImagerPrefillTests(WizardTestBase):
    def test_prefill_is_optional_and_applied(self):
        session = self.make_session()
        result = session.submit('imager_prefill', {})
        self.assertTrue(result.ok)
        self.assertEqual(session.imager['wifi_ssid'], '')

        session = self.make_session()
        result = session.submit('imager_prefill', {
            'wifi_ssid': 'ImagerNet', 'wifi_psk': 'imagerpsk',
            'hostname': 'imager-host', 'ssh_enabled': True,
        })
        self.assertTrue(result.ok)
        self.assertEqual(session.wifi['ssid'], 'ImagerNet')
        self.assertEqual(session.wifi['psk'], 'imagerpsk')
        self.assertEqual(session.hostname, 'imager-host')
        self.assertTrue(session.imager['ssh_enabled'])

    def test_invalid_types_are_rejected(self):
        session = self.make_session()
        for data in ({'wifi_ssid': 1}, {'hostname': 2}, {'ssh_enabled': 'yes'}):
            with self.subTest(data=data):
                result = session.submit('imager_prefill', data)
                self.assertFalse(result.ok)
                self.assertNotIn('imager_prefill', session.completed)


class WifiTests(WizardTestBase):
    def test_ssid_is_required(self):
        session = self.make_session()
        for ssid in ('', '   ', None, 5):
            with self.subTest(ssid=ssid):
                result = session.submit('wifi', {'ssid': ssid})
                self.assertFalse(result.ok)
                self.assertEqual(session.step, 'status')
                self.assertNotIn('wifi', session.completed)

    def test_psk_bounds(self):
        session = self.make_session()
        self.assertFalse(session.submit('wifi', {'ssid': SSID, 'psk': 'short'}).ok)
        self.assertFalse(session.submit('wifi', {'ssid': SSID, 'psk': 'x' * 64}).ok)
        self.assertFalse(session.submit('wifi', {'ssid': SSID, 'psk': 123}).ok)
        self.assertTrue(session.submit('wifi', {'ssid': SSID, 'psk': PSK}).ok)

    def test_scan_is_injectable(self):
        scans = []
        session = self.make_session(
            wifi_scan=lambda: scans.append(True) or ['HomeNet', 'Guest']
        )
        result = session.submit('wifi', {'scan': True, 'ssid': SSID, 'psk': PSK})
        self.assertTrue(result.ok)
        self.assertEqual(scans, [True])
        self.assertEqual(session.wifi_scan_results, ['HomeNet', 'Guest'])

    def test_scan_unavailable_is_rejected(self):
        session = self.make_session()
        result = session.submit('wifi', {'scan': True, 'ssid': SSID})
        self.assertFalse(result.ok)
        self.assertIn('scan', result.reason)


class PrusaTokenTests(WizardTestBase):
    def test_manual_token_is_required(self):
        session = self.make_session()
        for token in ('', '   ', None, 7):
            with self.subTest(token=token):
                result = session.submit('prusa_token', {'token': token})
                self.assertFalse(result.ok)
                self.assertNotIn('prusa_token', session.completed)
        self.assertTrue(session.submit('prusa_token', {'token': TOKEN}).ok)
        self.assertEqual(session.token, TOKEN)

    def test_qr_hook_is_documented_not_implemented(self):
        session = self.make_session()
        result = session.submit('prusa_token', {'source': 'qr', 'qr': 'raw-qr-payload'})
        self.assertFalse(result.ok)
        self.assertIn(setup_wizard.QR_PAIRING_HOOK, result.reason)
        self.assertNotIn('raw-qr-payload', result.reason)
        self.assertNotIn('prusa_token', session.completed)

    def test_injected_qr_decoder_is_used(self):
        session = self.make_session(qr_decoder=lambda payload: 'decoded-token')
        result = session.submit('prusa_token', {'source': 'qr', 'qr': 'raw'})
        self.assertTrue(result.ok)
        self.assertEqual(session.token, 'decoded-token')

    def test_unknown_source_is_rejected(self):
        session = self.make_session()
        result = session.submit('prusa_token', {'source': 'nfc', 'token': TOKEN})
        self.assertFalse(result.ok)


class FingerprintTests(WizardTestBase):
    def test_fingerprint_is_optional(self):
        session = self.make_session()
        self.assertTrue(session.submit('fingerprint', {}).ok)
        # The host has no wlan0, so no stable fingerprint can be derived; on a
        # device the empty submission persists the MAC-derived value.
        self.assertEqual(session.fingerprint, '')
        self.assertTrue(session.submit('fingerprint', {'fingerprint': 'fp-123'}).ok)
        self.assertEqual(session.fingerprint, 'fp-123')

    def test_empty_submission_persists_the_mac_derived_fingerprint(self):
        import tempfile

        with tempfile.NamedTemporaryFile('w', suffix='.mac') as handle:
            handle.write('d8:3a:dd:32:1c:ac\n')
            handle.flush()
            derived = setup_wizard.WizardSession._derived_fingerprint(
                mac_path=handle.name)
        self.assertTrue(derived)
        self.assertNotEqual(derived, '')
        self.assertRegex(derived, r'^[0-9a-f]{32}$')

    def test_derived_fingerprint_is_empty_without_a_mac(self):
        self.assertEqual(
            setup_wizard.WizardSession._derived_fingerprint(
                mac_path='/nonexistent/wlan0/address'),
            '',
        )

    def test_invalid_type_is_rejected(self):
        session = self.make_session()
        result = session.submit('fingerprint', {'fingerprint': 42})
        self.assertFalse(result.ok)
        self.assertNotIn('fingerprint', session.completed)


class AdminPasswordTests(WizardTestBase):
    def test_weak_password_is_rejected(self):
        session = self.make_session()
        result = session.submit('admin_password', {'password': 'short', 'confirm': 'short'})
        self.assertFalse(result.ok)
        self.assertNotIn('admin_password', session.completed)
        self.assertEqual(session.admin_hash, '')

    def test_confirmation_must_match(self):
        session = self.make_session()
        result = session.submit('admin_password', {
            'password': PASSWORD, 'confirm': 'different-horse',
        })
        self.assertFalse(result.ok)
        self.assertNotIn('different-horse', result.reason)
        self.assertEqual(session.admin_hash, '')

    def test_only_the_hash_is_stored(self):
        session = self.make_session()
        self.assertTrue(session.submit('admin_password', {
            'password': PASSWORD, 'confirm': PASSWORD,
        }).ok)
        self.assertTrue(session.admin_hash.startswith('scrypt$'))
        self.assertNotIn(PASSWORD, session.admin_hash)
        self.assertNotIn(PASSWORD, repr(session))


class MqttTests(WizardTestBase):
    def test_disabled_is_accepted(self):
        session = self.make_session()
        self.assertTrue(session.submit('mqtt', {'enabled': False}).ok)

    def test_uri_required_when_enabled(self):
        session = self.make_session()
        result = session.submit('mqtt', {'enabled': True, 'uri': ''})
        self.assertFalse(result.ok)
        self.assertIn('uri', result.reason)

    def test_invalid_uri_is_rejected(self):
        session = self.make_session()
        result = session.submit('mqtt', {'enabled': True, 'uri': 'http://broker.example'})
        self.assertFalse(result.ok)
        self.assertIn('mqtt', result.reason.lower())

    def test_valid_settings_are_staged(self):
        session = self.make_session()
        result = session.submit('mqtt', {
            'enabled': True, 'uri': 'mqtts://broker.example:8883',
            'username': 'mqttuser', 'password': 'mqttpass',
        })
        self.assertTrue(result.ok)
        self.assertTrue(session.mqtt['enabled'])
        self.assertEqual(session.mqtt['username'], 'mqttuser')


class MqttTesterTests(WizardTestBase):
    """WP-R2 (AC-23 tail): optional live broker test in step 7."""

    MQTT = {
        'enabled': True, 'uri': 'mqtts://broker.example:8883',
        'username': 'mqttuser', 'password': 'mqtt-secret',
    }

    def test_validation_only_is_the_default(self):
        session = self.make_session()
        result = session.submit('mqtt', dict(self.MQTT, test=True))
        self.assertTrue(result.ok, result.reason)

    def test_successful_test_is_called_and_staged(self):
        seen = []

        def tester(config):
            seen.append(config)
            return True, 'connected'

        session = self.make_session(mqtt_tester=tester)
        result = session.submit('mqtt', dict(self.MQTT, test=True))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].uri, self.MQTT['uri'])
        self.assertEqual(seen[0].username, self.MQTT['username'])
        self.assertEqual(session.mqtt['password'], 'mqtt-secret')

    def test_failed_test_is_redacted_and_not_staged(self):
        def tester(config):
            return False, f'bad password mqtt-secret for mqttuser'

        session = self.make_session(mqtt_tester=tester)
        result = session.submit('mqtt', dict(self.MQTT, test=True))
        self.assertFalse(result.ok)
        self.assertNotIn('mqtt-secret', result.reason)
        self.assertNotIn('mqttuser', result.reason)
        self.assertNotIn('mqtt', session.completed)
        self.assertFalse(session.mqtt['enabled'])

    def test_raising_tester_never_leaks(self):
        def tester(config):
            raise RuntimeError('boom mqtt-secret')

        session = self.make_session(mqtt_tester=tester)
        result = session.submit('mqtt', dict(self.MQTT, test=True))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'mqtt connection test failed')
        self.assertNotIn('mqtt-secret', result.reason)

    def test_test_flag_without_enabled_does_not_call_tester(self):
        calls = []
        session = self.make_session(mqtt_tester=lambda config: calls.append(config))
        result = session.submit('mqtt', {'enabled': False, 'test': True})
        self.assertTrue(result.ok)
        self.assertEqual(calls, [])

    def test_test_flag_without_tester_is_validation_only(self):
        session = self.make_session()
        result = session.submit('mqtt', dict(self.MQTT, test=True))
        self.assertTrue(result.ok)


class SummaryTests(WizardTestBase):
    def test_summary_is_redacted(self):
        session = self.make_session()
        for result in self.walk(
            session, mqtt={
                'enabled': True, 'uri': 'mqtts://broker.example:8883',
                'username': 'mqttuser', 'password': 'mqttpass',
            }
        ):
            self.assertTrue(result.ok, result.reason)
        summary = session.summary()
        blob = repr(summary)
        for secret in (PSK, TOKEN, PASSWORD, 'mqttuser', 'mqttpass', session.admin_hash):
            self.assertNotIn(secret, blob)
        self.assertEqual(summary['wifi']['psk'], admin_auth.REDACTED)
        self.assertEqual(summary['prusa']['token'], admin_auth.REDACTED)
        self.assertEqual(summary['admin']['password_hash'], admin_auth.REDACTED)
        self.assertEqual(summary['mqtt']['username'], admin_auth.REDACTED)
        self.assertEqual(summary['mqtt']['password'], admin_auth.REDACTED)
        self.assertEqual(summary['wifi']['ssid'], SSID)

    def test_repr_never_exposes_secrets(self):
        session = self.make_session()
        self.walk(session)
        blob = repr(session)
        for secret in (PSK, TOKEN, PASSWORD, session.admin_hash):
            self.assertNotIn(secret, blob)


class PersistTests(WizardTestBase):
    def test_persist_requires_prerequisites(self):
        session = self.make_session()
        result = session.submit('persist', {})
        self.assertFalse(result.ok)
        self.assertFalse(session.persisted)
        self.assertFalse(os.path.exists(self.device_path))

    def test_no_files_written_before_persist(self):
        session = self.make_session()
        for result in self.walk(session):
            self.assertTrue(result.ok, result.reason)
        self.assertFalse(os.path.exists(self.device_path))
        self.assertFalse(os.path.exists(self.secrets_path))

    def test_invalid_token_is_rejected_without_writes(self):
        session = self.make_session()
        self.assertTrue(session.submit('status', {}).ok)
        self.assertTrue(session.submit('imager_prefill', {}).ok)
        self.assertTrue(session.submit('wifi', {'ssid': SSID, 'psk': PSK}).ok)
        result = session.submit('prusa_token', {'token': ''})
        self.assertFalse(result.ok)
        self.assertEqual(session.step, 'prusa_token')
        self.assertFalse(os.path.exists(self.device_path))
        self.assertFalse(os.path.exists(self.secrets_path))

    def test_invalid_config_is_rejected_without_writes(self):
        session = self.make_session(camera_name='')
        for result in self.walk(session):
            self.assertTrue(result.ok, result.reason)
        result = session.submit('persist', {})
        self.assertFalse(result.ok)
        self.assertIn('camera_name', result.reason)
        self.assertFalse(session.persisted)
        self.assertFalse(os.path.exists(self.device_path))
        self.assertFalse(os.path.exists(self.secrets_path))

    def test_persist_writes_files_and_advances_to_claimed(self):
        session = self.make_session()
        for result in self.walk(session):
            self.assertTrue(result.ok, result.reason)
        result = self.persist(session)
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(session.persisted)
        self.assertEqual(session.provisioning_state, 'claimed')
        self.assertEqual(session.step, 'finish')

        self.assertTrue(os.path.exists(self.device_path))
        self.assertTrue(os.path.exists(self.secrets_path))
        self.assertEqual(stat.S_IMODE(os.stat(self.device_path).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(self.secrets_path).st_mode), 0o600)

        device = config_schema.load_device(self.device_path)
        secrets = config_schema.load_secrets(self.secrets_path)
        self.assertEqual(device['admin']['hostname'], 'buddy3d-ddeeff')
        self.assertEqual(secrets['prusa']['token'], TOKEN)
        self.assertEqual(secrets['wifi']['psk'], PSK)
        self.assertTrue(secrets['admin']['password_hash'].startswith('scrypt$'))

    def test_password_is_hashed_not_written_plaintext(self):
        session = self.make_session()
        for result in self.walk(session):
            self.assertTrue(result.ok, result.reason)
        self.assertTrue(self.persist(session).ok)
        text = self.read_text(self.secrets_path)
        self.assertNotIn(PASSWORD, text)
        self.assertIn('scrypt$', text)

    def test_persisted_state_file_has_no_secrets(self):
        session = self.make_session()
        for result in self.walk(session):
            self.assertTrue(result.ok, result.reason)
        self.assertTrue(self.persist(session).ok)
        state_text = self.read_text(self.provisioning_path)
        for secret in (PSK, TOKEN, PASSWORD, session.admin_hash):
            self.assertNotIn(secret, state_text)
        self.assertEqual(provisioning.ProvisioningState.load(
            self.provisioning_path).state, 'claimed')

    def test_partial_persist_failure_keeps_device_unclaimable(self):
        session = self.make_session()
        for result in self.walk(session):
            self.assertTrue(result.ok, result.reason)
        with patch.object(config_schema, 'save_secrets', return_value=False):
            result = self.persist(session)
        self.assertFalse(result.ok)
        self.assertIn('secrets', result.reason)
        self.assertFalse(session.persisted)
        self.assertEqual(session.provisioning_state, '')
        # The device document was written first, but without secrets.toml the
        # claim predicate is not satisfied and the state machine never advanced.
        self.assertTrue(os.path.exists(self.device_path))
        self.assertFalse(os.path.exists(self.secrets_path))
        device = config_schema.load_device(self.device_path)
        self.assertFalse(provisioning.is_claimed({}, device))
        self.assertEqual(
            provisioning.ProvisioningState.load(self.provisioning_path).state,
            'factory',
        )


class FinishTests(WizardTestBase):
    def finish_ready(self, **kwargs):
        session = self.make_session(**kwargs)
        for result in self.walk(session):
            self.assertTrue(result.ok, result.reason)
        self.assertTrue(self.persist(session).ok)
        return session

    def test_finish_requires_persist(self):
        session = self.make_session()
        result = session.submit('finish', {})
        self.assertFalse(result.ok)
        self.assertEqual(self.events, [])

    def test_finish_stops_hotspot_before_camera(self):
        session = self.finish_ready()
        result = session.finish()
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(self.events, ['hotspot_stop', 'camera_start'])
        self.assertEqual(self.hotspot.ifnames, ['wlan0'])
        self.assertTrue(session.finished)

    def test_guard_denies_when_camera_running(self):
        session = self.finish_ready()
        result = session.finish(camera_running=True)
        self.assertFalse(result.ok)
        self.assertIn('camera source is running', result.reason)
        self.assertEqual(self.events, [])
        self.assertFalse(session.finished)

    def test_hotspot_stop_failure_aborts_handoff(self):
        failing = FakeHotspot(self.events, ok=False, reason='nmcli failed')
        session = self.finish_ready(hotspot_controller=failing)
        result = session.finish()
        self.assertFalse(result.ok)
        self.assertIn('hotspot', result.reason)
        self.assertEqual(self.events, ['hotspot_stop'])
        self.assertFalse(session.finished)

    def test_missing_camera_callback_is_rejected_without_stopping_hotspot(self):
        session = self.finish_ready(start_camera=None)
        result = session.finish()
        self.assertFalse(result.ok)
        self.assertIn('callback', result.reason)
        self.assertEqual(self.events, [])
        self.assertEqual(self.hotspot.ifnames, [])
        self.assertFalse(session.finished)

    def test_start_camera_failure_restarts_hotspot(self):
        def boom():
            raise RuntimeError('argv=nmcli password sup3rsecret')

        for failing_camera in (lambda: False, boom):
            with self.subTest(camera=failing_camera):
                events = []
                controller = FakeHotspot(events)
                session = self.finish_ready(
                    hotspot_controller=controller, start_camera=failing_camera
                )
                result = session.finish()
                self.assertFalse(result.ok)
                self.assertIn('stayed in setup', result.reason)
                self.assertEqual(events, ['hotspot_stop', 'hotspot_start'])
                self.assertEqual(controller.start_ssids, ['Buddy3D-Setup-ddeeff'])
                self.assertNotIn(PSK, result.reason)
                self.assertNotIn(TOKEN, result.reason)
                self.assertNotIn('sup3rsecret', result.reason)
                self.assertFalse(session.finished)

    def test_start_camera_failure_with_failed_restart_reports_it(self):
        failing = FakeHotspot(self.events, start_ok=False, reason='nmcli failed')
        session = self.finish_ready(
            hotspot_controller=failing, start_camera=lambda: False
        )
        result = session.finish()
        self.assertFalse(result.ok)
        self.assertIn('stayed in setup', result.reason)
        self.assertIn('hotspot restart failed', result.reason)
        self.assertEqual(self.events, ['hotspot_stop', 'hotspot_start'])

    def test_finish_activates_station_before_camera(self):
        seen = []

        def activate(ssid, psk):
            seen.append((ssid, psk))
            self.events.append('station_activate')
            return True

        session = self.finish_ready(activate_station=activate)
        result = session.finish()
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(
            self.events, ['hotspot_stop', 'station_activate', 'camera_start']
        )
        self.assertEqual(seen, [(SSID, PSK)])
        self.assertTrue(session.finished)

    def test_station_activation_failure_restarts_hotspot(self):
        def activate(ssid, psk):
            self.events.append('station_activate')
            return False

        session = self.finish_ready(activate_station=activate)
        result = session.finish()
        self.assertFalse(result.ok)
        self.assertIn('station activation failed', result.reason)
        self.assertIn('stayed in setup', result.reason)
        self.assertEqual(
            self.events, ['hotspot_stop', 'station_activate', 'hotspot_start']
        )
        self.assertFalse(session.finished)

    def test_station_activation_result_reason_is_surfaced(self):
        def activate(ssid, psk):
            self.events.append('station_activate')
            return privileged.PrivilegedResult(
                False, 'wifi-station-apply failed (exit 1)'
            )

        session = self.finish_ready(activate_station=activate)
        result = session.finish()
        self.assertFalse(result.ok)
        self.assertIn('wifi-station-apply failed', result.reason)
        self.assertNotIn(PSK, result.reason)
        self.assertFalse(session.finished)

    def test_station_activation_exception_does_not_leak(self):
        def activate(ssid, psk):
            self.events.append('station_activate')
            raise RuntimeError('argv=nmcli password ' + PSK)

        session = self.finish_ready(activate_station=activate)
        result = session.finish()
        self.assertFalse(result.ok)
        self.assertIn('stayed in setup', result.reason)
        self.assertNotIn(PSK, result.reason)
        self.assertEqual(
            self.events, ['hotspot_stop', 'station_activate', 'hotspot_start']
        )
        self.assertFalse(session.finished)


if __name__ == '__main__':
    unittest.main()
