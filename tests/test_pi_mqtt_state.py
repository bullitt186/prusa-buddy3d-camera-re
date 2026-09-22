"""WP-5a AC-25/AC-26: MQTT state JSON and HA discovery (mqtt_state).

Host-only and stdlib-only. Verifies the exact §6.3 state schema, the
secret-free guarantee under hostile inputs, and the §6.4 device-discovery
document (origin/device/availability, all components, types, ranges, and stable
unique IDs). No broker, network, or hardware is used.
"""
import json
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import mqtt_state  # noqa: E402
import mqtt_topics  # noqa: E402
import state as state_module  # noqa: E402

DEVICE_SEED = 'appliance-seed-01'
DEVICE_ID = mqtt_topics.device_id(DEVICE_SEED)
SYNTHETIC_MAC = 'aa-bb-cc-dd-ee-ff'
NORMALIZED_MAC = 'AA:BB:CC:DD:EE:FF'

EXPECTED_STATE = {
    'schema_version': 1,
    'quality': 'HD',
    'snapshot_upload': False,
    'snapshot_interval': 30,
    'timelapse_enabled': True,
    'timelapse_interval': 60,
    'timelapse_fps': 15,
    'prusa_rtsp': True,
    'webrtc': False,
    'prusa_connected': True,
    'camera_source': 'running',
    'storage_free_bytes': 123456789,
    'wifi_rssi_dbm': -54,
    'cpu_temperature_c': 48.2,
    'uptime_seconds': 3600,
    'application_version': '1.0.0',
    'last_command_error': None,
}

EXPECTED_PLATFORMS = {
    'quality': 'select',
    'snapshot_upload': 'switch',
    'snapshot_interval': 'number',
    'timelapse_enabled': 'switch',
    'timelapse_interval': 'number',
    'timelapse_fps': 'number',
    'timelapse_build': 'button',
    'prusa_rtsp': 'switch',
    'webrtc': 'switch',
    'restart': 'button',
    'update': 'update',
    'prusa_connected': 'binary_sensor',
    'camera_source': 'binary_sensor',
    'storage_free': 'sensor',
    'wifi_rssi': 'sensor',
    'cpu_temperature': 'sensor',
    'uptime': 'sensor',
    'app_version': 'sensor',
}

NUMBER_RANGES = {
    'snapshot_interval': (10, 600, 1),
    'timelapse_interval': (1, 3600, 1),
    'timelapse_fps': (1, 30, 1),
}


class StateSchemaTests(unittest.TestCase):
    def test_state_keys_exact_and_ordered(self):
        document = mqtt_state.build_state()
        self.assertEqual(tuple(document.keys()), mqtt_state.STATE_KEYS)
        self.assertEqual(len(document), 17)

    def test_state_uses_native_json_values(self):
        document = mqtt_state.build_state(
            quality=2, snapshot_upload=False, snapshot_interval=30,
            timelapse_enabled=True, timelapse_interval=60, timelapse_fps=15,
            prusa_rtsp=True, webrtc=False, prusa_connected=True,
            camera_source='running', storage_free_bytes=123456789,
            wifi_rssi_dbm=-54, cpu_temperature_c=48.2, uptime_seconds=3600,
            application_version='1.0.0', last_command_error=None)
        self.assertEqual(document, EXPECTED_STATE)
        self.assertIsInstance(document['schema_version'], int)
        self.assertIsInstance(document['quality'], str)
        self.assertIsInstance(document['snapshot_upload'], bool)
        self.assertIsInstance(document['snapshot_interval'], int)
        self.assertIsInstance(document['timelapse_enabled'], bool)
        self.assertIsInstance(document['storage_free_bytes'], int)
        self.assertIsInstance(document['wifi_rssi_dbm'], int)
        self.assertIsInstance(document['cpu_temperature_c'], float)
        self.assertIsInstance(document['uptime_seconds'], int)
        self.assertIsInstance(document['application_version'], str)
        self.assertIsNone(document['last_command_error'])
        # Round-trips through JSON with the documented key set.
        self.assertEqual(json.loads(json.dumps(document)), EXPECTED_STATE)

    def test_missing_metrics_become_null(self):
        document = mqtt_state.build_state()
        self.assertIsNone(document['storage_free_bytes'])
        self.assertIsNone(document['wifi_rssi_dbm'])
        self.assertIsNone(document['cpu_temperature_c'])
        self.assertIsNone(document['uptime_seconds'])
        self.assertIsNone(document['last_command_error'])
        self.assertEqual(document['schema_version'], 1)
        self.assertEqual(document['application_version'], '')

    def test_build_state_reads_a_camera_state(self):
        camera = state_module.CameraState(quality=1, snapshot_interval=45,
                                          snapshot_upload_enabled=False)
        camera.timelapse_enabled = True
        camera.timelapse_interval = 120
        camera.timelapse_fps = 20
        camera.rtsp_mode = 2          # enabled
        camera.webrtc_mode = 0        # disabled
        document = mqtt_state.build_state(camera)
        self.assertEqual(document['quality'], 'SD')
        self.assertEqual(document['snapshot_interval'], 45)
        self.assertFalse(document['snapshot_upload'])
        self.assertTrue(document['timelapse_enabled'])
        self.assertEqual(document['timelapse_interval'], 120)
        self.assertEqual(document['timelapse_fps'], 20)
        self.assertTrue(document['prusa_rtsp'])
        self.assertFalse(document['webrtc'])

    def test_explicit_arguments_override_state(self):
        camera = state_module.CameraState(quality=3)
        document = mqtt_state.build_state(camera, quality=1, prusa_rtsp=True)
        self.assertEqual(document['quality'], 'SD')
        self.assertTrue(document['prusa_rtsp'])


class QualityMappingTests(unittest.TestCase):
    def test_quality_name_enum_mapping(self):
        self.assertEqual(mqtt_state.quality_name(1), 'SD')
        self.assertEqual(mqtt_state.quality_name(2), 'HD')
        self.assertEqual(mqtt_state.quality_name(3), 'FHD')

    def test_quality_name_accepts_published_names(self):
        self.assertEqual(mqtt_state.quality_name('SD'), 'SD')
        self.assertEqual(mqtt_state.quality_name('hd'), 'HD')
        self.assertEqual(mqtt_state.quality_name(' FHD '), 'FHD')

    def test_quality_name_rejects_invalid(self):
        for bad in (0, 4, True, False, None, 'SDX', '', 1.0):
            with self.assertRaises(ValueError, msg=f'{bad!r} should be rejected'):
                mqtt_state.quality_name(bad)

    def test_build_state_falls_back_to_fhd_for_invalid_tier(self):
        self.assertEqual(mqtt_state.build_state(quality=99)['quality'], 'FHD')
        self.assertEqual(mqtt_state.build_state(quality=True)['quality'], 'FHD')


class SecretHygieneTests(unittest.TestCase):
    HOSTILE = {
        'quality_tier': 2,
        'snapshot_upload_enabled': True,
        'token': 'TOKEN-SECRET-123',
        'mqtt_password': 'MQTT-SECRET-456',
        'wifi_psk': 'WIFI-SECRET-789',
        'broker_uri': 'mqtts://operator:hunter2@broker.example:8883',
        'home_path': '/home/bullitt/.config/secrets.toml',
        'exception': 'Traceback (most recent call last): File "/opt/x.py", line 1',
    }
    SECRETS = ('TOKEN-SECRET-123', 'MQTT-SECRET-456', 'WIFI-SECRET-789',
               'hunter2', 'bullitt')

    def test_hostile_inputs_never_leak(self):
        document = mqtt_state.build_state(
            self.HOSTILE,
            last_command_error='Traceback (most recent call last): '
                               'File "/home/bullitt/x.py", line 3',
            camera_source='TOKEN-SECRET-123',
            secrets=self.SECRETS)
        self.assertEqual(set(document), set(mqtt_state.STATE_KEYS))
        blob = json.dumps(document)
        for secret in self.SECRETS:
            self.assertNotIn(secret, blob)
        self.assertNotIn('Traceback', blob)
        self.assertNotIn('/home/', blob)
        self.assertEqual(document['last_command_error'], 'command failed')
        self.assertEqual(document['camera_source'], 'unknown')

    def test_unknown_state_keys_are_ignored(self):
        document = mqtt_state.build_state({'token': 'x', 'wifi_psk': 'y'})
        self.assertNotIn('token', document)
        self.assertNotIn('wifi_psk', document)

    def test_sanitize_command_error(self):
        self.assertIsNone(mqtt_state.sanitize_command_error(None))
        self.assertIsNone(mqtt_state.sanitize_command_error(5))
        self.assertEqual(
            mqtt_state.sanitize_command_error('quality locked by TURN client'),
            'quality locked by TURN client')
        self.assertEqual(
            mqtt_state.sanitize_command_error(
                'Traceback (most recent call last): boom'),
            'command failed')
        self.assertNotIn(
            'bullitt',
            mqtt_state.sanitize_command_error('failed at /home/bullitt/app.py'))
        bounded = mqtt_state.sanitize_command_error('x' * 5000)
        self.assertLessEqual(len(bounded), 200)
        self.assertEqual(
            mqtt_state.sanitize_command_error(
                'leaked MQTT-SECRET-456 here', secrets=('MQTT-SECRET-456',)),
            'leaked <redacted> here')

    def test_sanitize_camera_source_allowlist(self):
        for value in mqtt_state.CAMERA_SOURCES:
            self.assertEqual(mqtt_state.sanitize_camera_source(value), value)
        self.assertEqual(mqtt_state.sanitize_camera_source('RUNNING'), 'running')
        self.assertEqual(mqtt_state.sanitize_camera_source('secret-token'), 'unknown')
        self.assertEqual(mqtt_state.sanitize_camera_source(None), 'unknown')


class DiscoveryDocumentTests(unittest.TestCase):
    def build(self, **kwargs):
        kwargs.setdefault('camera_name', 'Printer Camera')
        kwargs.setdefault('application_version', '1.0.0')
        kwargs.setdefault('mac', SYNTHETIC_MAC)
        return mqtt_state.build_discovery(DEVICE_ID, **kwargs)

    def uid(self, key):
        return mqtt_state.component_unique_id(DEVICE_ID, key)

    def comp(self, document, key):
        return document['cmps'][self.uid(key)]

    def test_discovery_topic_exact(self):
        self.assertEqual(
            mqtt_topics.discovery_device_topic(
                mqtt_topics.DEFAULT_DISCOVERY_PREFIX, DEVICE_ID),
            f'homeassistant/device/buddy3d_{DEVICE_ID}/config')

    def test_required_origin_device_and_availability(self):
        document = self.build()
        self.assertEqual(document['o']['name'], 'prusa-buddy3d-camera')
        self.assertEqual(document['o']['sw'], '1.0.0')
        device = document['dev']
        self.assertEqual(device['ids'], DEVICE_ID)
        self.assertEqual(device['name'], 'Printer Camera Controls')
        self.assertEqual(device['mf'], 'Prusa Community')
        self.assertEqual(device['mdl'], 'Buddy3D Raspberry Pi Camera')
        self.assertEqual(device['sw'], '1.0.0')
        self.assertEqual(device['sn'], DEVICE_ID)
        self.assertEqual(device['cns'], [['mac', NORMALIZED_MAC]])
        self.assertEqual(
            document['avty_t'],
            mqtt_topics.availability(DEVICE_ID))

    def test_serial_override(self):
        document = self.build(serial='SERIAL-0001')
        self.assertEqual(document['dev']['sn'], 'SERIAL-0001')

    def test_all_components_with_correct_platforms(self):
        document = self.build()
        components = document['cmps']
        self.assertEqual(
            set(components), {self.uid(key) for key in EXPECTED_PLATFORMS})
        self.assertEqual(len(components), 18)
        for key, platform in EXPECTED_PLATFORMS.items():
            self.assertEqual(self.comp(document, key)['p'], platform, key)

    def test_no_camera_entity_is_created(self):
        components = self.build()['cmps']
        self.assertNotIn('camera', {c['p'] for c in components.values()})
        self.assertNotIn('camera', components)

    def test_number_ranges_and_steps(self):
        document = self.build()
        for key, (minimum, maximum, step) in NUMBER_RANGES.items():
            component = self.comp(document, key)
            self.assertEqual(component['min'], minimum, key)
            self.assertEqual(component['max'], maximum, key)
            self.assertEqual(component['step'], step, key)

    def test_select_options(self):
        self.assertEqual(
            self.comp(self.build(), 'quality')['options'], ['SD', 'HD', 'FHD'])

    def test_buttons_press_payload(self):
        document = self.build()
        for key in ('timelapse_build', 'restart'):
            self.assertEqual(self.comp(document, key)['payload_press'], 'press', key)

    def test_update_component(self):
        update = self.comp(self.build(), 'update')
        self.assertEqual(update['payload_install'], 'install')
        self.assertEqual(update['device_class'], 'firmware')
        self.assertEqual(
            update['command_topic'], mqtt_topics.update_install(DEVICE_ID))
        self.assertEqual(
            update['state_topic'], mqtt_topics.update_state(DEVICE_ID))

    def test_diagnostic_and_disabled_by_default(self):
        document = self.build()
        for key in ('storage_free', 'wifi_rssi', 'cpu_temperature', 'uptime',
                    'app_version'):
            self.assertEqual(
                self.comp(document, key)['entity_category'], 'diagnostic', key)
        for key in ('wifi_rssi', 'cpu_temperature', 'uptime', 'app_version'):
            self.assertIs(
                self.comp(document, key)['enabled_by_default'], False, key)
        self.assertNotIn('enabled_by_default', self.comp(document, 'storage_free'))
        self.assertEqual(
            self.comp(document, 'wifi_rssi')['unit_of_measurement'], 'dBm')
        self.assertEqual(
            self.comp(document, 'cpu_temperature')['unit_of_measurement'], '°C')
        self.assertEqual(self.comp(document, 'uptime')['unit_of_measurement'], 's')
        self.assertEqual(self.comp(document, 'storage_free')['unit_of_measurement'], 'B')

    def test_binary_sensor_templates(self):
        document = self.build()
        self.assertIn(
            'value_json.prusa_connected',
            self.comp(document, 'prusa_connected')['value_template'])
        self.assertIn(
            'value_json.camera_source',
            self.comp(document, 'camera_source')['value_template'])

    def test_unique_ids_are_stable_across_rename(self):
        first = mqtt_state.build_discovery(
            DEVICE_ID, 'Name One', '1.0.0', mac=SYNTHETIC_MAC)
        second = mqtt_state.build_discovery(
            DEVICE_ID, 'Name Two', '2.0.0', mac=SYNTHETIC_MAC)
        self.assertEqual(set(first['cmps']), set(second['cmps']))
        for uid_key, component in first['cmps'].items():
            self.assertEqual(component['unique_id'], uid_key)
            self.assertEqual(
                component['unique_id'], second['cmps'][uid_key]['unique_id'])
            self.assertTrue(component['unique_id'].startswith(DEVICE_ID + '_'))
        # Only the display name changed.
        self.assertEqual(self.comp(first, 'quality')['name'],
                         self.comp(second, 'quality')['name'])
        self.assertNotEqual(first['dev']['name'], second['dev']['name'])

    def test_unique_ids_are_unique(self):
        components = self.build()['cmps']
        unique_ids = [c['unique_id'] for c in components.values()]
        self.assertEqual(len(unique_ids), len(set(unique_ids)))
        self.assertEqual(set(unique_ids), set(components))

    def test_component_topics_reference_the_contract(self):
        document = self.build()
        expected_commands = {
            'quality': mqtt_topics.quality(DEVICE_ID),
            'snapshot_upload': mqtt_topics.snapshot_upload(DEVICE_ID),
            'snapshot_interval': mqtt_topics.snapshot_interval(DEVICE_ID),
            'timelapse_enabled': mqtt_topics.timelapse_enabled(DEVICE_ID),
            'timelapse_interval': mqtt_topics.timelapse_interval(DEVICE_ID),
            'timelapse_fps': mqtt_topics.timelapse_fps(DEVICE_ID),
            'timelapse_build': mqtt_topics.timelapse_build(DEVICE_ID),
            'prusa_rtsp': mqtt_topics.prusa_rtsp(DEVICE_ID),
            'webrtc': mqtt_topics.webrtc(DEVICE_ID),
            'restart': mqtt_topics.restart(DEVICE_ID),
        }
        for key, topic in expected_commands.items():
            self.assertEqual(self.comp(document, key)['command_topic'], topic, key)
        state_topic = mqtt_topics.state(DEVICE_ID)
        for key in ('snapshot_upload', 'snapshot_interval', 'timelapse_enabled',
                    'timelapse_interval', 'timelapse_fps', 'prusa_rtsp', 'webrtc',
                    'prusa_connected', 'camera_source', 'storage_free',
                    'wifi_rssi', 'cpu_temperature', 'uptime', 'app_version'):
            self.assertEqual(self.comp(document, key)['state_topic'], state_topic, key)

    def test_custom_prefixes_flow_into_topics(self):
        document = mqtt_state.build_discovery(
            DEVICE_ID, 'Printer Camera', '1.0.0',
            base_prefix='site-a', discovery_prefix='hass', mac=SYNTHETIC_MAC)
        self.assertEqual(document['avty_t'], f'site-a/{DEVICE_ID}/availability')
        self.assertEqual(
            self.comp(document, 'quality')['command_topic'],
            f'site-a/{DEVICE_ID}/command/quality')
        self.assertEqual(
            mqtt_topics.discovery_device_topic('hass', DEVICE_ID),
            f'hass/device/buddy3d_{DEVICE_ID}/config')

    def test_component_unique_id_rejects_unknown_key(self):
        with self.assertRaises(ValueError):
            mqtt_state.component_unique_id(DEVICE_ID, 'not_a_component')

    def test_blank_camera_name_falls_back(self):
        document = mqtt_state.build_discovery(DEVICE_ID, '   ')
        self.assertEqual(document['dev']['name'], 'Buddy3D Camera Controls')

    def test_invalid_mac_rejected(self):
        with self.assertRaises(ValueError):
            mqtt_state.build_discovery(DEVICE_ID, 'Printer', mac='not-a-mac')

    def test_document_is_json_serializable(self):
        document = self.build()
        self.assertEqual(json.loads(json.dumps(document)), document)


if __name__ == '__main__':
    unittest.main()
