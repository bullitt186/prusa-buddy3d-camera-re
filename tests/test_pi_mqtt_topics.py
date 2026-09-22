"""WP-5a AC-24: stable MQTT topic contract (mqtt_topics).

Host-only and stdlib-only. Every assertion pins an exact §6.3 topic string and
the device-id/prefix validation rules. No broker, network, or hardware is used.
"""
import sys
import unittest
import uuid
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import mqtt_topics  # noqa: E402

DEVICE_SEED = 'appliance-seed-01'
DEVICE_ID = mqtt_topics.device_id(DEVICE_SEED)

#: Exact §6.3 topics for the fixed synthetic device id.
EXPECTED_TOPICS = {
    'availability': f'buddy3d/{DEVICE_ID}/availability',
    'state': f'buddy3d/{DEVICE_ID}/state',
    'quality': f'buddy3d/{DEVICE_ID}/command/quality',
    'snapshot_upload': f'buddy3d/{DEVICE_ID}/command/snapshot_upload',
    'snapshot_interval': f'buddy3d/{DEVICE_ID}/command/snapshot_interval',
    'timelapse_enabled': f'buddy3d/{DEVICE_ID}/command/timelapse_enabled',
    'timelapse_interval': f'buddy3d/{DEVICE_ID}/command/timelapse_interval',
    'timelapse_fps': f'buddy3d/{DEVICE_ID}/command/timelapse_fps',
    'timelapse_build': f'buddy3d/{DEVICE_ID}/command/timelapse_build',
    'prusa_rtsp': f'buddy3d/{DEVICE_ID}/command/prusa_rtsp',
    'webrtc': f'buddy3d/{DEVICE_ID}/command/webrtc',
    'restart': f'buddy3d/{DEVICE_ID}/command/restart',
    'update_state': f'buddy3d/{DEVICE_ID}/update/state',
    'update_install': f'buddy3d/{DEVICE_ID}/update/install',
}


class ContractConstantsTests(unittest.TestCase):
    def test_default_prefixes_and_status_topic(self):
        self.assertEqual(mqtt_topics.DEFAULT_BASE_PREFIX, 'buddy3d')
        self.assertEqual(mqtt_topics.DEFAULT_DISCOVERY_PREFIX, 'homeassistant')
        self.assertEqual(mqtt_topics.HA_STATUS_TOPIC, 'homeassistant/status')

    def test_command_names_are_the_ten_documented_leaves(self):
        self.assertEqual(mqtt_topics.COMMAND_NAMES, (
            'quality',
            'snapshot_upload',
            'snapshot_interval',
            'timelapse_enabled',
            'timelapse_interval',
            'timelapse_fps',
            'timelapse_build',
            'prusa_rtsp',
            'webrtc',
            'restart',
        ))
        self.assertEqual(len(mqtt_topics.COMMAND_NAMES), 10)
        self.assertEqual(len(set(mqtt_topics.COMMAND_NAMES)), 10)


class ExactTopicTests(unittest.TestCase):
    def test_availability_and_state(self):
        self.assertEqual(
            mqtt_topics.availability(DEVICE_ID), EXPECTED_TOPICS['availability'])
        self.assertEqual(mqtt_topics.state(DEVICE_ID), EXPECTED_TOPICS['state'])

    def test_every_command_topic_exact(self):
        builders = {
            'quality': mqtt_topics.quality,
            'snapshot_upload': mqtt_topics.snapshot_upload,
            'snapshot_interval': mqtt_topics.snapshot_interval,
            'timelapse_enabled': mqtt_topics.timelapse_enabled,
            'timelapse_interval': mqtt_topics.timelapse_interval,
            'timelapse_fps': mqtt_topics.timelapse_fps,
            'timelapse_build': mqtt_topics.timelapse_build,
            'prusa_rtsp': mqtt_topics.prusa_rtsp,
            'webrtc': mqtt_topics.webrtc,
            'restart': mqtt_topics.restart,
        }
        self.assertEqual(set(builders), set(mqtt_topics.COMMAND_NAMES))
        for name, builder in builders.items():
            self.assertEqual(builder(DEVICE_ID), EXPECTED_TOPICS[name])
            self.assertEqual(
                mqtt_topics.command(DEVICE_ID, name), EXPECTED_TOPICS[name])

    def test_update_topics_exact(self):
        self.assertEqual(
            mqtt_topics.update_state(DEVICE_ID), EXPECTED_TOPICS['update_state'])
        self.assertEqual(
            mqtt_topics.update_install(DEVICE_ID), EXPECTED_TOPICS['update_install'])

    def test_discovery_topic_exact_and_custom_prefix(self):
        self.assertEqual(
            mqtt_topics.discovery_device_topic(
                mqtt_topics.DEFAULT_DISCOVERY_PREFIX, DEVICE_ID),
            f'homeassistant/device/buddy3d_{DEVICE_ID}/config')
        self.assertEqual(
            mqtt_topics.discovery_device_topic('ha_discovery', DEVICE_ID),
            f'ha_discovery/device/buddy3d_{DEVICE_ID}/config')

    def test_base_prefix_is_configurable(self):
        self.assertEqual(
            mqtt_topics.state(DEVICE_ID, base_prefix='mycams'),
            f'mycams/{DEVICE_ID}/state')
        self.assertEqual(
            mqtt_topics.quality(DEVICE_ID, base_prefix='site-a/cams'),
            f'site-a/cams/{DEVICE_ID}/command/quality')


class DeviceIdTests(unittest.TestCase):
    def test_device_id_is_stable_for_the_same_seed(self):
        self.assertEqual(mqtt_topics.device_id(DEVICE_SEED), DEVICE_ID)
        self.assertEqual(mqtt_topics.device_id(DEVICE_SEED), DEVICE_ID)

    def test_device_id_differs_for_a_different_seed(self):
        self.assertNotEqual(mqtt_topics.device_id('other-seed'), DEVICE_ID)

    def test_device_id_is_a_safe_lowercase_token(self):
        self.assertRegex(DEVICE_ID, r'^[a-z0-9-]+$')

    def test_device_id_sanitizes_hostile_seeds(self):
        # The derivation hashes first, so even an unsafe seed yields a safe id.
        for seed in ('UPPER seed/with spaces', 'a:b:c', '  trimmed  ', 'ümlaut'):
            derived = mqtt_topics.device_id(seed)
            self.assertRegex(derived, r'^[a-z0-9-]+$')
            self.assertEqual(derived, mqtt_topics.device_id(seed))

    def test_device_id_accepts_uuid_objects(self):
        value = uuid.uuid5(uuid.NAMESPACE_URL, 'prusa-camera:seed')
        self.assertEqual(mqtt_topics.device_id(value), mqtt_topics.device_id(value.hex))

    def test_device_id_rejects_empty_and_non_string(self):
        for bad in ('', '   ', None, 5, b'bytes'):
            with self.assertRaises(ValueError):
                mqtt_topics.device_id(bad)

    def test_sanitize_device_id_projection(self):
        self.assertEqual(
            mqtt_topics.sanitize_device_id('AA:BB:CC:DD:EE:FF'),
            'aa-bb-cc-dd-ee-ff')
        self.assertEqual(mqtt_topics.sanitize_device_id('__a  b__'), 'a-b')
        self.assertEqual(mqtt_topics.sanitize_device_id(''), '')
        self.assertEqual(mqtt_topics.sanitize_device_id(None), '')
        value = uuid.UUID('00112233-4455-6677-8899-aabbccddeeff')
        self.assertEqual(
            mqtt_topics.sanitize_device_id(value), value.hex)


class PrefixValidationTests(unittest.TestCase):
    def test_valid_prefixes(self):
        for prefix in ('buddy3d', 'homeassistant', 'site-a', 'a/b/c', 'A_b-1'):
            self.assertEqual(mqtt_topics.validate_prefix(prefix), prefix)

    def test_unsafe_prefixes_rejected(self):
        for bad in ('', '  ', '#', 'buddy3d/#', '+', 'a b', '/leading',
                    'trailing/', 'a//b', 'null\x00byte', 'semi;colon', None, 5):
            with self.assertRaises(ValueError, msg=f'{bad!r} should be rejected'):
                mqtt_topics.validate_prefix(bad)

    def test_builders_validate_prefix(self):
        with self.assertRaises(ValueError):
            mqtt_topics.state(DEVICE_ID, base_prefix='bad/#')
        with self.assertRaises(ValueError):
            mqtt_topics.discovery_device_topic('bad/#', DEVICE_ID)

    def test_builders_reject_unsafe_device_id(self):
        for bad in ('', 'has/slash', 'has space', 'has#wildcard', 'UPPER'):
            with self.assertRaises(ValueError, msg=f'{bad!r} should be rejected'):
                mqtt_topics.state(bad)


class CommandRoundTripTests(unittest.TestCase):
    def test_every_command_round_trips(self):
        for name in mqtt_topics.COMMAND_NAMES:
            topic = mqtt_topics.command(DEVICE_ID, name)
            self.assertEqual(
                mqtt_topics.command_from_topic(topic, DEVICE_ID), name)

    def test_round_trip_honors_custom_base_prefix(self):
        topic = mqtt_topics.command(DEVICE_ID, 'restart', base_prefix='site-a')
        self.assertEqual(
            mqtt_topics.command_from_topic(topic, DEVICE_ID, base_prefix='site-a'),
            'restart')

    def test_unknown_command_topic_rejected(self):
        unknown = f'buddy3d/{DEVICE_ID}/command/not_a_command'
        with self.assertRaises(ValueError):
            mqtt_topics.command_from_topic(unknown, DEVICE_ID)

    def test_foreign_or_non_command_topics_rejected(self):
        for topic in (
            mqtt_topics.availability(DEVICE_ID),
            mqtt_topics.state(DEVICE_ID),
            mqtt_topics.update_state(DEVICE_ID),
            mqtt_topics.update_install(DEVICE_ID),
            mqtt_topics.command('otherdevice', 'quality'),
            'buddy3d',
            '',
            None,
            5,
        ):
            with self.assertRaises(ValueError, msg=f'{topic!r} should be rejected'):
                mqtt_topics.command_from_topic(topic, DEVICE_ID)

    def test_command_builder_rejects_unknown_name(self):
        with self.assertRaises(ValueError):
            mqtt_topics.command(DEVICE_ID, 'reboot')


if __name__ == '__main__':
    unittest.main()
