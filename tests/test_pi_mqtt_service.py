"""WP-5b AC-23/24/25/27/28: MQTT runtime service (mqtt_service).

Host-only and stdlib-only. A fake broker backend plus injected clock, sleeper,
and jitter exercise every path with no network, broker, paho, or real sleep:
configuration gating, the connect sequence (retained online/LWT/subscriptions/
state/discovery), command mapping and persistence ordering, rejection state,
bounded payloads, retained-command rejection, HA birth republish, bounded
reconnect backoff, failure isolation, secret hygiene, and import-safety.
"""
import importlib
import json
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import config_schema  # noqa: E402
import mqtt_service  # noqa: E402
import mqtt_topics  # noqa: E402
import state as state_module  # noqa: E402
from state import CameraState  # noqa: E402

DEVICE_SEED = 'appliance-seed-01'
DEVICE_ID = mqtt_topics.device_id(DEVICE_SEED)
BROKER_SECRET = 'MQTT-SECRET-456'


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #

@dataclass
class Result:
    """Minimal stand-in for ``settings_coordinator.SettingsResult``."""

    ok: bool = True
    reason: str = None
    changed: list = field(default_factory=list)
    state: dict = field(default_factory=dict)


class FakeCoordinator:
    """Records setter calls and mirrors the real coordinator's state changes."""

    def __init__(self, state=None, events=None, results=None):
        self.state = state if state is not None else CameraState()
        self.events = events if events is not None else []
        self.results = dict(results or {})
        self.calls = []

    def _invoke(self, name, *args):
        self.calls.append((name, args))
        self.events.append(('coordinator', name, args))
        result = self.results.get(name)
        if callable(result):
            result = result(*args)
        return result if result is not None else Result(True)

    def set_quality(self, raw):
        result = self._invoke('set_quality', raw)
        if result.ok and raw in state_module.RAW_TO_ENUM:
            self.state.quality = state_module.RAW_TO_ENUM[raw]
        return result

    def set_snapshot_upload(self, enabled):
        result = self._invoke('set_snapshot_upload', enabled)
        if result.ok:
            self.state.snapshot_upload_enabled = enabled
        return result

    def set_snapshot_interval(self, seconds):
        result = self._invoke('set_snapshot_interval', seconds)
        if result.ok:
            self.state.snapshot_interval = seconds
        return result

    def set_timelapse_enabled(self, action):
        result = self._invoke('set_timelapse_enabled', action)
        if result.ok:
            self.state.timelapse_enabled = action == 'timelapse_enable'
        return result

    def set_timelapse_interval(self, seconds):
        result = self._invoke('set_timelapse_interval', seconds)
        if result.ok:
            self.state.timelapse_interval = seconds
        return result

    def set_timelapse_fps(self, fps):
        result = self._invoke('set_timelapse_fps', fps)
        if result.ok:
            self.state.timelapse_fps = fps
        return result

    def set_rtsp_mode(self, mode):
        result = self._invoke('set_rtsp_mode', mode)
        if result.ok:
            self.state.rtsp_mode = mode
        return result

    def set_webrtc_mode(self, mode):
        result = self._invoke('set_webrtc_mode', mode)
        if result.ok:
            self.state.webrtc_mode = mode
        return result


class FakeBackend:
    """In-memory :class:`mqtt_service.MqttBackend` double."""

    def __init__(self, events=None, fail_on=()):
        self.events = events if events is not None else []
        self.fail_on = set(fail_on)
        self.calls = []
        self.callback = None
        self.will = None
        self.connected = False

    def _record(self, *call):
        self.calls.append(call)
        self.events.append(('backend',) + call)

    def _maybe_fail(self, name):
        if name in self.fail_on:
            raise RuntimeError(f'backend {name} failed')

    def connect(self):
        self._record('connect')
        self._maybe_fail('connect')
        self.connected = True

    def publish(self, topic, payload, qos=0, retain=False):
        self._record('publish', topic, payload, qos, retain)
        self._maybe_fail('publish')

    def subscribe(self, topic, qos=0):
        self._record('subscribe', topic, qos)
        self._maybe_fail('subscribe')

    def set_will(self, topic, payload, qos=0, retain=False):
        self._record('set_will', topic, payload, qos, retain)
        self.will = (topic, payload, qos, retain)
        self._maybe_fail('set_will')

    def disconnect(self):
        self._record('disconnect')
        self.connected = False
        self._maybe_fail('disconnect')

    def set_message_callback(self, callback):
        self.callback = callback
        self._record('set_message_callback')

    def deliver(self, topic, payload, retain=False):
        self.callback(topic, payload, retain)


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RecordingSleeper:
    def __init__(self):
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)


class AdvancingSleeper:
    """Records each sleep and advances the injected clock by that amount."""

    def __init__(self, clock):
        self.clock = clock
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)
        self.clock.advance(seconds)


class RecordingDispatcher:
    """Captures dispatched callables instead of spawning real threads.

    Production uses a daemon-thread dispatcher; tests drive the captured task
    explicitly so the birth/error-clear paths are deterministic.
    """

    def __init__(self):
        self.tasks = []

    def __call__(self, func):
        self.tasks.append(func)

    def run_all(self):
        tasks, self.tasks = self.tasks, []
        for task in tasks:
            task()
        return len(tasks)


class Action:
    def __init__(self, return_value=True):
        self.return_value = return_value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.return_value


def device_doc(enabled=True, uri='mqtts://broker.example:8883', fingerprint=DEVICE_SEED,
               discovery_prefix='homeassistant', topic_prefix='buddy3d', ca_file=''):
    device = config_schema.default_device()
    device['fingerprint'] = fingerprint
    device['mqtt'].update({
        'enabled': enabled,
        'uri': uri,
        'discovery_prefix': discovery_prefix,
        'topic_prefix': topic_prefix,
        'ca_file': ca_file,
    })
    return device


def secrets_doc(username='operator', password=BROKER_SECRET):
    return {'mqtt': {'username': username, 'password': password}}


def valid_config(**kwargs):
    device = kwargs.pop('device', device_doc())
    secrets = kwargs.pop('secrets', secrets_doc())
    return mqtt_service.MqttConfig.from_documents(device, secrets, **kwargs)


class ServiceTestCase(unittest.TestCase):
    """Builds a service with fakes and no broker/network/sleep."""

    def make(self, *, config=None, backend=None, events=None, clock=None,
             sleeper=None, jitter=None, coordinator=None, dispatcher=None, **kwargs):
        events = events if events is not None else []
        clock = clock if clock is not None else FakeClock()
        sleeper = sleeper if sleeper is not None else RecordingSleeper()
        coordinator = coordinator if coordinator is not None else FakeCoordinator(events=events)
        backend = backend if backend is not None else FakeBackend(events=events)
        # Default to a non-running dispatcher: no real thread ever spins on the
        # frozen fake clock. Tests that exercise dispatch pass a RecordingDispatcher.
        if dispatcher is None:
            dispatcher = lambda func: None
        service = mqtt_service.MqttService(
            coordinator, config or valid_config(), backend=backend,
            clock=clock, sleeper=sleeper, jitter=jitter or (lambda low, high: low),
            dispatcher=dispatcher, **kwargs)
        return service, coordinator, backend, clock, sleeper

    @staticmethod
    def publishes(backend):
        return [call for call in backend.calls if call[0] == 'publish']

    @staticmethod
    def subscribes(backend):
        return [call for call in backend.calls if call[0] == 'subscribe']

    def last_state(self, service, backend):
        for call in reversed(backend.calls):
            if call[0] == 'publish' and call[1] == service.state_topic:
                return json.loads(call[2].decode('utf-8'))
        return None

    def last_discovery(self, service, backend, topic=None):
        topic = topic or service.discovery_topic
        for call in reversed(backend.calls):
            if call[0] == 'publish' and call[1] == topic and call[2]:
                return json.loads(call[2].decode('utf-8'))
        return None


# --------------------------------------------------------------------------- #
# Configuration (AC-23)
# --------------------------------------------------------------------------- #

class ConfigTests(unittest.TestCase):
    def test_disabled_by_default(self):
        config = mqtt_service.MqttConfig.from_documents(
            config_schema.default_device(), {})
        self.assertFalse(config.enabled)

    def test_disabled_without_uri(self):
        config = mqtt_service.MqttConfig.from_documents(
            device_doc(uri=''), secrets_doc())
        self.assertFalse(config.enabled)

    def test_disabled_with_userinfo_in_uri(self):
        for uri in ('mqtt://user:pass@broker.example:1883',
                    'mqtts://user@broker.example:8883'):
            config = mqtt_service.MqttConfig.from_documents(
                device_doc(uri=uri), secrets_doc())
            self.assertFalse(config.enabled, uri)

    def test_disabled_with_wrong_scheme_or_host(self):
        for uri in ('http://broker.example', 'mqtt://', 'mqtt://:1883', 'not a url'):
            config = mqtt_service.MqttConfig.from_documents(
                device_doc(uri=uri), secrets_doc())
            self.assertFalse(config.enabled, uri)

    def test_disabled_with_path_query_or_fragment(self):
        for uri in ('mqtt://broker.example:1883/foo',
                    'mqtts://broker.example:8883/?x=1',
                    'mqtt://broker.example:1883#frag'):
            config = mqtt_service.MqttConfig.from_documents(
                device_doc(uri=uri), secrets_doc())
            self.assertFalse(config.enabled, uri)

    def test_disabled_without_device_seed(self):
        config = mqtt_service.MqttConfig.from_documents(
            device_doc(fingerprint=''), secrets_doc())
        self.assertFalse(config.enabled)

    def test_enabled_with_valid_uri_and_seed(self):
        config = valid_config()
        self.assertTrue(config.enabled)
        self.assertEqual(config.device_id, DEVICE_ID)
        self.assertEqual(config.uri, 'mqtts://broker.example:8883')
        self.assertEqual(config.username, 'operator')
        self.assertEqual(config.password, BROKER_SECRET)
        self.assertEqual(config.discovery_prefix, 'homeassistant')
        self.assertEqual(config.topic_prefix, 'buddy3d')

    def test_accepts_plain_mqtt_and_ipv6_and_dns(self):
        for uri in ('mqtt://broker.example:1883', 'mqtt://[::1]:1883',
                    'mqtts://192.168.1.10:8883'):
            config = valid_config(device=device_doc(uri=uri))
            self.assertTrue(config.enabled, uri)

    def test_device_seed_override(self):
        config = mqtt_service.MqttConfig.from_documents(
            device_doc(fingerprint=''), secrets_doc(), device_seed='other-seed')
        self.assertTrue(config.enabled)
        self.assertEqual(config.device_id, mqtt_topics.device_id('other-seed'))

    def test_ca_file_validation(self):
        self.assertTrue(valid_config(device=device_doc(ca_file='/data/ca.pem')).enabled)
        for bad in ('relative/ca.pem', '/data/bad\x00.pem', '/data/bad\n.pem'):
            config = valid_config(device=device_doc(ca_file=bad))
            self.assertFalse(config.enabled, bad)

    def test_invalid_prefixes_disable(self):
        for prefix in ('bad/#', 'trailing/', 'a b'):
            config = valid_config(device=device_doc(discovery_prefix=prefix))
            self.assertFalse(config.enabled, prefix)
            config = valid_config(device=device_doc(topic_prefix=prefix))
            self.assertFalse(config.enabled, prefix)

    def test_effective_client_id_default(self):
        config = valid_config()
        self.assertEqual(config.effective_client_id, f'buddy3d-{DEVICE_ID}')


# --------------------------------------------------------------------------- #
# Startup sequence (AC-24/AC-27)
# --------------------------------------------------------------------------- #

class StartupTests(ServiceTestCase):
    def test_disabled_makes_no_backend_calls(self):
        service, coordinator, backend, _, _ = self.make(
            config=valid_config(device=device_doc(enabled=False)))
        self.assertFalse(service.start())
        self.assertEqual(backend.calls, [])
        self.assertEqual(coordinator.calls, [])
        self.assertFalse(service.stop())

    def test_connect_sequence(self):
        service, _, backend, _, _ = self.make()
        self.assertTrue(service.start())

        # Retained LWT registered before connect, retained online published.
        self.assertEqual(
            backend.will, (service.availability_topic, b'offline', 1, True))
        online = (service.availability_topic, b'online', 1, True)
        self.assertIn(('publish',) + online, backend.calls)
        connect_index = backend.calls.index(('connect',))
        will_index = backend.calls.index(
            ('set_will', service.availability_topic, b'offline', 1, True))
        online_index = backend.calls.index(('publish',) + online)
        self.assertLess(will_index, connect_index)
        self.assertLess(connect_index, online_index)

        # Every command topic + HA status subscribed at QoS 1.
        expected = [(topic, 1) for topic in service.command_topics()]
        expected.append((mqtt_topics.HA_STATUS_TOPIC, 1))
        self.assertEqual([(call[1], call[2]) for call in self.subscribes(backend)], expected)

        # Retained authoritative state + retained discovery.
        state_publishes = [
            call for call in self.publishes(backend) if call[1] == service.state_topic]
        discovery_publishes = [
            call for call in self.publishes(backend) if call[1] == service.discovery_topic]
        self.assertTrue(state_publishes)
        self.assertTrue(discovery_publishes)
        for _, _, _, qos, retain in state_publishes + discovery_publishes:
            self.assertEqual(qos, 1)
            self.assertTrue(retain)

    def test_stop_publishes_offline(self):
        service, _, backend, _, _ = self.make()
        service.start()
        backend.calls.clear()
        self.assertTrue(service.stop())
        self.assertIn(
            ('publish', service.availability_topic, b'offline', 1, True),
            backend.calls)
        self.assertIn(('disconnect',), backend.calls)

    def test_ca_file_on_plain_mqtt_warns_without_secrets(self):
        config = valid_config(device=device_doc(
            uri='mqtt://broker.example:1883', ca_file='/data/ca.pem'))
        service, _, _, _, _ = self.make(config=config)
        with self.assertLogs('prusa-cam.mqtt', level='WARNING') as captured:
            service.start()
        blob = '\n'.join(captured.output)
        self.assertIn('TLS is not in use', blob)
        self.assertNotIn(BROKER_SECRET, blob)

    def test_state_document_has_no_metrics_leak(self):
        service, coordinator, backend, _, _ = self.make()
        coordinator.state.camera_name = 'Printer Camera'
        service.start()
        document = self.last_state(service, backend)
        self.assertEqual(set(document), set(mqtt_service.mqtt_state.STATE_KEYS))
        self.assertEqual(document['schema_version'], 1)
        self.assertEqual(document['camera_source'], 'unknown')


# --------------------------------------------------------------------------- #
# Command handling (AC-24/AC-27)
# --------------------------------------------------------------------------- #

class CommandMappingTests(ServiceTestCase):
    CASES = (
        ('quality', b'HD', 'set_quality', (6,), 'quality', 'HD'),
        ('snapshot_upload', b'false', 'set_snapshot_upload', (False,),
         'snapshot_upload', False),
        ('snapshot_interval', b'30', 'set_snapshot_interval', (30,),
         'snapshot_interval', 30),
        ('timelapse_enabled', b'true', 'set_timelapse_enabled',
         ('timelapse_enable',), 'timelapse_enabled', True),
        ('timelapse_interval', b'120', 'set_timelapse_interval', (120,),
         'timelapse_interval', 120),
        ('timelapse_fps', b'20', 'set_timelapse_fps', (20,),
         'timelapse_fps', 20),
        ('prusa_rtsp', b'true', 'set_rtsp_mode', (2,), 'prusa_rtsp', True),
        ('webrtc', b'false', 'set_webrtc_mode', (0,), 'webrtc', False),
    )

    def test_every_command_maps_to_the_right_setter(self):
        for name, payload, setter, args, key, expected in self.CASES:
            with self.subTest(command=name):
                service, coordinator, backend, _, _ = self.make()
                service.start()
                backend.calls.clear()
                coordinator.events.clear()

                result = service.handle_command(service.command_topic(name), payload)

                self.assertTrue(result.ok, result.reason)
                self.assertEqual(coordinator.calls, [(setter, args)])
                document = self.last_state(service, backend)
                self.assertEqual(document[key], expected)
                # Persist/apply (coordinator) happens before the state publish.
                self.assertEqual(
                    [event[0] for event in coordinator.events],
                    ['coordinator', 'backend'])
                self.assertEqual(coordinator.events[1][1], 'publish')
                self.assertEqual(coordinator.events[1][2], service.state_topic)

    def test_quality_accepts_raw_bytes_and_is_case_insensitive(self):
        for payload, raw in ((b'sd', 5), (b'FHD', 7), (b'6', 6)):
            with self.subTest(payload=payload):
                service, coordinator, _, _, _ = self.make()
                service.start()
                result = service.handle_command(
                    service.command_topic('quality'), payload)
                self.assertTrue(result.ok)
                self.assertEqual(coordinator.calls, [('set_quality', (raw,))])

    def test_unknown_topics_are_ignored(self):
        service, coordinator, backend, _, _ = self.make()
        service.start()
        backend.calls.clear()
        self.assertIsNone(service.handle_command('buddy3d/other/command/quality', b'HD'))
        self.assertIsNone(service.handle_command(service.state_topic, b'x'))
        self.assertEqual(coordinator.calls, [])
        self.assertEqual(backend.calls, [])

    def test_retained_command_is_ignored(self):
        service, coordinator, backend, _, _ = self.make()
        service.start()
        backend.calls.clear()

        backend.deliver(service.command_topic('quality'), b'HD', retain=True)

        self.assertEqual(coordinator.calls, [])
        self.assertEqual(backend.calls, [])

    def test_buttons_invoke_injected_actions(self):
        build = Action()
        restart = Action()
        service, coordinator, backend, _, _ = self.make(
            build_timelapse=build, restart=restart)
        service.start()

        self.assertTrue(service.handle_command(
            service.command_topic('timelapse_build'), b'press').ok)
        self.assertTrue(service.handle_command(
            service.command_topic('restart'), b'press').ok)

        self.assertEqual(build.calls, 1)
        self.assertEqual(restart.calls, 1)
        self.assertEqual(coordinator.calls, [])  # buttons are not settings mutations

    def test_buttons_reject_without_action(self):
        service, _, _, _, _ = self.make()
        service.start()
        result = service.handle_command(service.command_topic('restart'), b'press')
        self.assertFalse(result.ok)
        self.assertEqual(service.last_command_error, 'restart unavailable')

    def test_button_requires_press_payload(self):
        restart = Action()
        service, _, _, _, _ = self.make(restart=restart)
        service.start()
        result = service.handle_command(service.command_topic('restart'), b'on')
        self.assertFalse(result.ok)
        self.assertEqual(restart.calls, 0)


class CommandRejectionTests(ServiceTestCase):
    def test_rejection_publishes_authoritative_state_and_sets_error(self):
        coordinator = FakeCoordinator(results={
            'set_snapshot_interval': Result(False, 'interval rejected')})
        service, coordinator, backend, _, _ = self.make(coordinator=coordinator)
        service.start()
        backend.calls.clear()

        result = service.handle_command(
            service.command_topic('snapshot_interval'), b'5')

        self.assertFalse(result.ok)
        self.assertEqual(coordinator.calls, [('set_snapshot_interval', (5,))])
        document = self.last_state(service, backend)
        self.assertEqual(document['snapshot_interval'], 10)  # unchanged authoritative
        self.assertEqual(document['last_command_error'], 'interval rejected')
        self.assertEqual(service.last_command_error, 'interval rejected')

    def test_error_clears_on_next_success(self):
        attempts = []

        def reject_first(seconds):
            attempts.append(seconds)
            if len(attempts) == 1:
                return Result(False, 'interval rejected')
            return Result(True)

        coordinator = FakeCoordinator(results={'set_snapshot_interval': reject_first})
        service, coordinator, backend, _, _ = self.make(coordinator=coordinator)
        service.start()
        service.handle_command(service.command_topic('snapshot_interval'), b'5')

        result = service.handle_command(
            service.command_topic('snapshot_interval'), b'30')

        self.assertTrue(result.ok)
        self.assertIsNone(service.last_command_error)
        document = self.last_state(service, backend)
        self.assertIsNone(document['last_command_error'])
        self.assertEqual(document['snapshot_interval'], 30)

    def test_error_clears_after_timeout(self):
        clock = FakeClock()
        coordinator = FakeCoordinator(results={
            'set_quality': Result(False, 'quality locked')})
        service, _, backend, _, _ = self.make(coordinator=coordinator, clock=clock)
        service.start()
        service.handle_command(service.command_topic('quality'), b'SD')
        self.assertEqual(service.last_command_error, 'quality locked')

        clock.advance(mqtt_service.COMMAND_ERROR_TTL + 0.1)
        service.publish_state()

        self.assertIsNone(service.last_command_error)
        document = self.last_state(service, backend)
        self.assertIsNone(document['last_command_error'])

    def test_error_autonomously_clears_after_timeout(self):
        clock = FakeClock()
        sleeper = AdvancingSleeper(clock)
        dispatcher = RecordingDispatcher()
        coordinator = FakeCoordinator(results={
            'set_quality': Result(False, 'quality locked')})
        service, _, backend, _, _ = self.make(
            coordinator=coordinator, clock=clock, sleeper=sleeper,
            dispatcher=dispatcher)
        service.start()
        service.handle_command(service.command_topic('quality'), b'SD')
        self.assertEqual(service.last_command_error, 'quality locked')
        self.assertEqual(len(dispatcher.tasks), 1)

        # The dispatched worker sleeps the TTL on the injected sleeper (which
        # advances the injected clock) and then republishes on its own. No
        # external publish_state() call is made.
        backend.calls.clear()
        dispatcher.run_all()

        self.assertEqual(sleeper.delays, [mqtt_service.COMMAND_ERROR_TTL])
        self.assertIsNone(service.last_command_error)
        document = self.last_state(service, backend)
        self.assertIsNotNone(document)
        self.assertIsNone(document['last_command_error'])

    def test_error_reason_is_bounded_and_non_secret(self):
        hostile = ('Traceback (most recent call last): File "/home/operator/x.py", '
                   'line 3 ' + BROKER_SECRET)
        coordinator = FakeCoordinator(results={
            'set_quality': Result(False, hostile)})
        service, _, backend, _, _ = self.make(
            coordinator=coordinator, secrets=(BROKER_SECRET,))
        service.start()
        service.handle_command(service.command_topic('quality'), b'SD')

        document = self.last_state(service, backend)
        self.assertLessEqual(len(document['last_command_error']), 200)
        self.assertNotIn(BROKER_SECRET, document['last_command_error'])
        self.assertNotIn('/home/', document['last_command_error'])


class PayloadBoundingTests(ServiceTestCase):
    def _assert_rejected(self, name, payload):
        service, coordinator, backend, _, _ = self.make()
        service.start()
        result = service.handle_command(service.command_topic(name), payload)
        self.assertFalse(result.ok, name)
        self.assertEqual(coordinator.calls, [], name)
        self.assertIsNotNone(service.last_command_error)
        # Authoritative state is still published after the rejection.
        self.assertIsNotNone(self.last_state(service, backend))

    def test_oversized_payload_rejected(self):
        self._assert_rejected('quality', b'x' * (mqtt_service.MAX_PAYLOAD_BYTES + 1))

    def test_invalid_utf8_rejected(self):
        self._assert_rejected('quality', b'\xff\xfe')

    def test_non_text_payload_rejected(self):
        self._assert_rejected('quality', object())

    def test_malformed_values_rejected(self):
        for name, payload in (
            ('quality', b'SDX'),
            ('snapshot_upload', b'maybe'),
            ('snapshot_interval', b'ten'),
            ('snapshot_interval', b'3.5'),
            ('timelapse_enabled', b'2'),
            ('timelapse_interval', b''),
            ('timelapse_fps', b'x'),
            ('prusa_rtsp', b'perhaps'),
            ('webrtc', b''),
        ):
            with self.subTest(command=name, payload=payload):
                self._assert_rejected(name, payload)


# --------------------------------------------------------------------------- #
# HA birth and reconnect (AC-27)
# --------------------------------------------------------------------------- #

class BirthTests(ServiceTestCase):
    def test_online_birth_republishes_after_delay(self):
        dispatcher = RecordingDispatcher()
        service, _, backend, _, sleeper = self.make(
            jitter=lambda low, high: 3.0, dispatcher=dispatcher)
        service.start()
        backend.calls.clear()

        backend.deliver(mqtt_topics.HA_STATUS_TOPIC, b'online', retain=False)

        # The birth path is dispatched off the network thread, not run inline.
        self.assertEqual(len(dispatcher.tasks), 1)
        self.assertEqual(sleeper.delays, [])
        self.assertEqual(backend.calls, [])

        # Drive the dispatched mqtt-birth task and observe the delayed republish.
        dispatcher.run_all()

        self.assertEqual(sleeper.delays, [3.0])
        self.assertTrue(0.0 <= sleeper.delays[0] <= 5.0)
        topics = [call[1] for call in self.publishes(backend)]
        self.assertIn(service.discovery_topic, topics)
        self.assertIn(service.state_topic, topics)

    def test_birth_delay_is_clamped_to_five_seconds(self):
        dispatcher = RecordingDispatcher()
        service, _, backend, _, sleeper = self.make(
            jitter=lambda low, high: 99.0, dispatcher=dispatcher)
        service.start()
        backend.calls.clear()
        backend.deliver(mqtt_topics.HA_STATUS_TOPIC, b'online', retain=False)
        dispatcher.run_all()
        self.assertEqual(sleeper.delays, [5.0])

    def test_non_online_birth_is_ignored(self):
        dispatcher = RecordingDispatcher()
        service, _, backend, _, sleeper = self.make(
            jitter=lambda low, high: 3.0, dispatcher=dispatcher)
        service.start()
        backend.calls.clear()
        backend.deliver(mqtt_topics.HA_STATUS_TOPIC, b'offline', retain=False)
        self.assertEqual(dispatcher.tasks, [])
        self.assertEqual(sleeper.delays, [])
        self.assertEqual(backend.calls, [])

    def test_reconnect_republishes(self):
        service, _, backend, _, sleeper = self.make(jitter=lambda low, high: 2.0)
        service.start()
        backend.calls.clear()
        service.on_reconnect()
        self.assertEqual(sleeper.delays, [2.0])
        topics = [call[1] for call in self.publishes(backend)]
        self.assertIn(service.discovery_topic, topics)
        self.assertIn(service.state_topic, topics)


class BackoffTests(ServiceTestCase):
    def test_reconnect_delay_is_bounded(self):
        service, _, _, _, _ = self.make(jitter=lambda low, high: 1.0)
        for attempt in (0, 1, 2, 5, 20, 100):
            delay = service.reconnect_delay(attempt)
            base = min(1.0 * (2 ** attempt), 60.0)
            self.assertGreaterEqual(delay, base, attempt)
            self.assertLessEqual(delay, base * 1.25, attempt)
            self.assertLessEqual(delay, 60.0 * 1.25, attempt)

    def test_reconnect_delay_uses_jitter(self):
        service, _, _, _, _ = self.make(jitter=lambda low, high: low)
        self.assertEqual(service.reconnect_delay(0), 1.0)
        self.assertEqual(service.reconnect_delay(3), 8.0)

    def test_attempt_reconnect_sleeps_within_bounds(self):
        service, _, _, _, sleeper = self.make(jitter=lambda low, high: 1.0)
        delay = service.attempt_reconnect()
        self.assertEqual(sleeper.delays, [delay])
        self.assertTrue(1.0 <= delay <= 1.25)


# --------------------------------------------------------------------------- #
# Failure isolation (AC-27/AC-28)
# --------------------------------------------------------------------------- #

class FailureIsolationTests(ServiceTestCase):
    def test_connect_failure_is_isolated(self):
        service, _, backend, _, _ = self.make(backend=FakeBackend(fail_on={'connect'}))
        self.assertFalse(service.start())
        self.assertIn(('connect',), backend.calls)

    def test_set_will_failure_is_isolated(self):
        service, _, _, _, _ = self.make(backend=FakeBackend(fail_on={'set_will'}))
        self.assertFalse(service.start())

    def test_publish_failure_is_isolated_and_service_stays_usable(self):
        backend = FakeBackend(fail_on={'publish'})
        service, coordinator, _, _, _ = self.make(backend=backend)
        self.assertTrue(service.start())  # connect/subscribe still succeed

        service.publish_state()
        result = service.handle_command(
            service.command_topic('snapshot_interval'), b'30')

        self.assertTrue(result.ok)
        self.assertEqual(coordinator.calls, [('set_snapshot_interval', (30,))])

    def test_subscribe_and_disconnect_failures_are_isolated(self):
        service, _, _, _, _ = self.make(backend=FakeBackend(fail_on={'subscribe'}))
        self.assertTrue(service.start())
        service, _, _, _, _ = self.make(backend=FakeBackend(fail_on={'disconnect'}))
        service.start()
        self.assertTrue(service.stop())

    def test_coordinator_exception_is_isolated(self):
        def explode(_raw):
            raise RuntimeError('coordinator exploded')

        coordinator = FakeCoordinator(results={'set_quality': explode})
        service, _, _, _, _ = self.make(coordinator=coordinator)
        service.start()
        result = service.handle_command(service.command_topic('quality'), b'SD')
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'command failed')

    def test_backend_publish_raising_never_escapes(self):
        class ExplodingBackend(FakeBackend):
            def publish(self, topic, payload, qos=0, retain=False):
                raise RuntimeError('boom')

        service, _, _, _, _ = self.make(backend=ExplodingBackend())
        service.start()
        service.publish_state()
        service.publish_discovery()
        self.assertTrue(service.stop())

    def test_invalid_discovery_input_does_not_break_start(self):
        service, _, backend, _, _ = self.make(mac='not-a-mac')
        self.assertTrue(service.start())
        topics = [call[1] for call in self.publishes(backend)]
        self.assertIn(service.state_topic, topics)
        self.assertNotIn(service.discovery_topic, topics)

    def test_encoding_failure_skips_publish(self):
        service, _, backend, _, _ = self.make()
        service.start()
        backend.calls.clear()
        with patch.object(service, '_build_state_document', return_value=object()):
            self.assertIsNone(service.publish_state())
        self.assertEqual(backend.calls, [])  # never overwrite the last good doc


# --------------------------------------------------------------------------- #
# Secret hygiene (AC-25)
# --------------------------------------------------------------------------- #

class SecretHygieneTests(ServiceTestCase):
    def test_config_repr_hides_credentials(self):
        config = valid_config()
        representation = repr(config)
        self.assertNotIn(BROKER_SECRET, representation)
        self.assertNotIn('operator', representation)

    def test_published_state_never_contains_broker_credentials(self):
        service, coordinator, backend, _, _ = self.make()
        coordinator.state.camera_name = 'Printer Camera'
        service.start()
        document = self.last_state(service, backend)
        blob = json.dumps(document)
        self.assertNotIn(BROKER_SECRET, blob)
        self.assertNotIn('operator', blob)

    def test_rejection_reason_secret_is_redacted(self):
        coordinator = FakeCoordinator(results={
            'set_quality': Result(False, f'leaked {BROKER_SECRET}')})
        service, _, backend, _, _ = self.make(coordinator=coordinator)
        service.start()
        service.handle_command(service.command_topic('quality'), b'SD')
        document = self.last_state(service, backend)
        self.assertNotIn(BROKER_SECRET, json.dumps(document))
        self.assertEqual(document['last_command_error'], 'leaked <redacted>')

    def test_discovery_name_secret_is_redacted(self):
        service, coordinator, backend, _, _ = self.make()
        coordinator.state.camera_name = f'{BROKER_SECRET} Cam'
        service.start()
        document = self.last_discovery(service, backend)
        self.assertNotIn(BROKER_SECRET, json.dumps(document))

    def test_logs_never_contain_secrets(self):
        class LeakyBackend(FakeBackend):
            def publish(self, topic, payload, qos=0, retain=False):
                raise RuntimeError(f'broker rejected {BROKER_SECRET}')

        service, _, _, _, _ = self.make(backend=LeakyBackend())
        with self.assertLogs('prusa-cam.mqtt', level='WARNING') as captured:
            service.publish_state()
        blob = '\n'.join(captured.output)
        self.assertNotIn(BROKER_SECRET, blob)


# --------------------------------------------------------------------------- #
# Discovery lifecycle (AC-27)
# --------------------------------------------------------------------------- #

class DiscoveryLifecycleTests(ServiceTestCase):
    def test_clear_discovery_publishes_empty_retained(self):
        service, _, backend, _, _ = self.make()
        service.start()
        backend.calls.clear()
        service.clear_discovery()
        self.assertIn(
            ('publish', service.discovery_topic, b'', 1, True), backend.calls)

    def test_discovery_prefix_change_clears_old_topic(self):
        service, _, backend, _, _ = self.make()
        service.start()
        old_topic = service.discovery_topic
        backend.calls.clear()

        self.assertTrue(service.set_discovery_prefix('hass'))

        self.assertIn(('publish', old_topic, b'', 1, True), backend.calls)
        new_topic = f'hass/device/{mqtt_topics.DISCOVERY_OBJECT_PREFIX}{DEVICE_ID}/config'
        topics = [call[1] for call in self.publishes(backend)]
        self.assertIn(new_topic, topics)

    def test_invalid_prefix_change_is_rejected(self):
        service, _, backend, _, _ = self.make()
        service.start()
        backend.calls.clear()
        self.assertFalse(service.set_discovery_prefix('bad/#'))
        self.assertEqual(backend.calls, [])

    def test_discovery_creates_no_camera_entity(self):
        service, _, backend, _, _ = self.make()
        service.start()
        document = self.last_discovery(service, backend)
        platforms = {component['p'] for component in document['cmps'].values()}
        self.assertNotIn('camera', platforms)
        self.assertEqual(len(document['cmps']), 18)


# --------------------------------------------------------------------------- #
# Import safety (no paho at module top)
# --------------------------------------------------------------------------- #

class ImportSafetyTests(unittest.TestCase):
    def test_module_imports_without_paho(self):
        saved = {name: sys.modules.get(name)
                 for name in ('paho', 'paho.mqtt', 'paho.mqtt.client')}
        try:
            for name in saved:
                sys.modules[name] = None  # any top-level paho import would now fail
            importlib.reload(mqtt_service)
        finally:
            for name, value in saved.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value
        self.assertTrue(hasattr(mqtt_service, 'MqttService'))
        self.assertTrue(hasattr(mqtt_service, 'default_backend'))


if __name__ == '__main__':
    unittest.main()
