"""WP-R2 (AC-23 tail): broker connection probe (mqtt_probe).

Stdlib-only and fully offline: a fake backend plus injected DNS/connect/TLS
hooks exercise every step and every failure. No paho, no socket, no network.
"""
import socket
import ssl
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import mqtt_probe  # noqa: E402
import mqtt_service  # noqa: E402

PASSWORD = 'MQTT-PROBE-SECRET'
USERNAME = 'mqttuser'
ADDRINFO = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
            '', ('127.0.0.1', 8883))


def make_config(uri='mqtts://broker.example:8883', **kwargs):
    return mqtt_service.MqttConfig(
        enabled=True, uri=uri, username=kwargs.pop('username', USERNAME),
        password=kwargs.pop('password', PASSWORD), **kwargs)


class FakeSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeBackend:
    def __init__(self, fail_step=None, reason=None):
        self.fail_step = fail_step
        self.reason = reason
        self.events = []
        self.published = []
        self.subscribed = []
        self.disconnected = False

    def connect(self):
        self.events.append('connect')
        if self.fail_step == 'auth':
            raise mqtt_probe.ProbeStepError(self.reason or 'authentication failed')
        if self.fail_step == 'connect_generic':
            raise RuntimeError('boom')

    def publish(self, topic, payload, qos=0, retain=False):
        self.events.append('publish')
        self.published.append((topic, payload, qos, retain))
        if self.fail_step == 'publish':
            raise mqtt_probe.ProbeStepError(self.reason or 'publish failed')

    def subscribe(self, topic, qos=0):
        self.events.append('subscribe')
        self.subscribed.append((topic, qos))
        if self.fail_step == 'subscribe':
            raise mqtt_probe.ProbeStepError(self.reason or 'subscribe failed')

    def disconnect(self):
        self.events.append('disconnect')
        self.disconnected = True


def ok_hooks():
    """Return injected transport hooks that always succeed."""
    socks = []

    def connect(addrinfo, timeout):
        sock = FakeSocket()
        socks.append(sock)
        return sock

    def tls_handshake(sock, host, ca_file, timeout):
        wrapped = FakeSocket()
        socks.append(wrapped)
        return wrapped

    return {
        'resolve': lambda host, port, timeout: [ADDRINFO],
        'connect': connect,
        'tls_handshake': tls_handshake,
        '_socks': socks,
    }


class ProbeSuccessTests(unittest.TestCase):
    def test_success_runs_every_step_in_order(self):
        backend = FakeBackend()
        hooks = ok_hooks()
        ok, reason = mqtt_probe.probe(
            make_config(),
            backend_factory=lambda config, timeout: backend,
            **{k: v for k, v in hooks.items() if not k.startswith('_')},
        )
        self.assertTrue(ok, reason)
        self.assertEqual(reason, 'connected')
        self.assertEqual(backend.events, ['connect', 'publish', 'subscribe', 'disconnect'])
        topic, payload, qos, retain = backend.published[0]
        self.assertIn('probe', topic)
        self.assertEqual(payload, mqtt_probe.PROBE_PAYLOAD)
        self.assertEqual(qos, 1)
        self.assertFalse(retain)
        self.assertEqual(backend.subscribed[0], (topic, 1))

    def test_plaintext_uri_skips_tls(self):
        backend = FakeBackend()
        hooks = ok_hooks()
        handshakes = []

        def handshake(sock, host, ca_file, timeout):
            handshakes.append(host)
            return FakeSocket()

        ok, _ = mqtt_probe.probe(
            make_config(uri='mqtt://broker.example:1883'),
            backend_factory=lambda config, timeout: backend,
            resolve=hooks['resolve'],
            connect=hooks['connect'],
            tls_handshake=handshake,
        )
        self.assertTrue(ok)
        self.assertEqual(handshakes, [])

    def test_disconnect_called_even_after_failure(self):
        backend = FakeBackend(fail_step='publish')
        hooks = ok_hooks()
        ok, _ = mqtt_probe.probe(
            make_config(),
            backend_factory=lambda config, timeout: backend,
            resolve=hooks['resolve'],
            connect=hooks['connect'],
            tls_handshake=hooks['tls_handshake'],
        )
        self.assertFalse(ok)
        self.assertTrue(backend.disconnected)


class ProbeFailureTests(unittest.TestCase):
    def _probe(self, backend, resolve=None, connect=None, tls_handshake=None,
               config=None):
        return mqtt_probe.probe(
            config or make_config(),
            backend_factory=lambda c, timeout: backend,
            resolve=resolve or (lambda host, port, timeout: [ADDRINFO]),
            connect=connect or (lambda addrinfo, timeout: FakeSocket()),
            tls_handshake=tls_handshake or (lambda sock, host, ca, timeout: FakeSocket()),
        )

    def test_invalid_uri(self):
        ok, reason = self._probe(FakeBackend(), config=make_config(uri=''))
        self.assertFalse(ok)
        self.assertEqual(reason, 'mqtt uri is invalid')

    def test_dns_failure(self):
        def resolve(host, port, timeout):
            raise socket.gaierror('no such host')
        ok, reason = self._probe(FakeBackend(), resolve=resolve)
        self.assertFalse(ok)
        self.assertIn('could not resolve', reason)

    def test_dns_empty(self):
        ok, reason = self._probe(FakeBackend(), resolve=lambda h, p, t: [])
        self.assertFalse(ok)
        self.assertIn('could not resolve', reason)

    def test_tcp_failure(self):
        def connect(addrinfo, timeout):
            raise OSError('connection refused')
        ok, reason = self._probe(FakeBackend(), connect=connect)
        self.assertFalse(ok)
        self.assertIn('could not connect', reason)

    def test_tls_failure(self):
        def handshake(sock, host, ca_file, timeout):
            raise ssl.SSLError('bad certificate')
        ok, reason = self._probe(FakeBackend(), tls_handshake=handshake)
        self.assertFalse(ok)
        self.assertEqual(reason, 'TLS handshake failed')

    def test_auth_failure(self):
        ok, reason = self._probe(FakeBackend(fail_step='auth'))
        self.assertFalse(ok)
        self.assertEqual(reason, 'authentication failed')

    def test_generic_connect_failure_maps_to_authentication(self):
        ok, reason = self._probe(FakeBackend(fail_step='connect_generic'))
        self.assertFalse(ok)
        self.assertEqual(reason, 'authentication failed')

    def test_publish_failure(self):
        ok, reason = self._probe(FakeBackend(fail_step='publish'))
        self.assertFalse(ok)
        self.assertEqual(reason, 'publish failed')

    def test_subscribe_failure(self):
        ok, reason = self._probe(FakeBackend(fail_step='subscribe'))
        self.assertFalse(ok)
        self.assertEqual(reason, 'subscribe failed')


class ProbeClientUnavailableTests(unittest.TestCase):
    def test_missing_paho_is_clear_failure(self):
        def factory(config, timeout):
            raise ImportError('no paho')
        ok, reason = mqtt_probe.probe(make_config(), backend_factory=factory)
        self.assertFalse(ok)
        self.assertEqual(reason, 'mqtt client unavailable')

    def test_mqtt_client_unavailable_exception(self):
        def factory(config, timeout):
            raise mqtt_probe.MqttClientUnavailable('mqtt client unavailable')
        ok, reason = mqtt_probe.probe(make_config(), backend_factory=factory)
        self.assertFalse(ok)
        self.assertEqual(reason, 'mqtt client unavailable')


class ProbeSecretHygieneTests(unittest.TestCase):
    def test_reason_never_echoes_password_or_username(self):
        backend = FakeBackend(
            fail_step='auth', reason=f'bad password {PASSWORD} for {USERNAME}')
        hooks = ok_hooks()
        ok, reason = mqtt_probe.probe(
            make_config(),
            backend_factory=lambda c, timeout: backend,
            resolve=hooks['resolve'],
            connect=hooks['connect'],
            tls_handshake=hooks['tls_handshake'],
        )
        self.assertFalse(ok)
        self.assertNotIn(PASSWORD, reason)
        self.assertNotIn(USERNAME, reason)

    def test_reason_is_bounded(self):
        backend = FakeBackend(fail_step='publish', reason='x' * 10000)
        hooks = ok_hooks()
        _, reason = mqtt_probe.probe(
            make_config(),
            backend_factory=lambda c, timeout: backend,
            resolve=hooks['resolve'],
            connect=hooks['connect'],
            tls_handshake=hooks['tls_handshake'],
        )
        self.assertLessEqual(len(reason), mqtt_probe.MAX_REASON_LENGTH)

    def test_uri_userinfo_is_not_resolved(self):
        # A URI with embedded userinfo must not leak it: the hostname is used,
        # and the credential-free contract rejects such a URI upstream.
        host, port = mqtt_probe._host_port('mqtts://user:pass@broker.example:8883')
        self.assertEqual((host, port), ('broker.example', 8883))

    def test_cli_never_accepts_a_password_on_argv(self):
        # argv is world-readable (/proc/<pid>/cmdline) and shell history; the
        # manual CLI takes the password from a file or PRUSA_MQTT_PASSWORD.
        source = (PI_DIR / 'mqtt_probe.py').read_text(encoding='utf-8')
        self.assertNotIn("add_argument('--password'", source)
        self.assertIn('--password-file', source)
        self.assertIn('PRUSA_MQTT_PASSWORD', source)


class ProbeImportSafetyTests(unittest.TestCase):
    def test_module_import_does_not_require_paho(self):
        source = (PI_DIR / 'mqtt_probe.py').read_text(encoding='utf-8')
        # The paho import must be inside default_backend, not module scope.
        self.assertIn('import paho.mqtt.client', source)
        head = source.split('def default_backend', 1)[0]
        self.assertNotIn('import paho', head)


if __name__ == '__main__':
    unittest.main()
