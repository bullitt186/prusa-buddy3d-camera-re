"""Wizard/admin broker connection test (WP-R2; AC-23 tail).

Before MQTT configuration is saved, the wizard (and the post-claim admin UI)
can ask the appliance to verify that the broker is reachable and accepts the
credentials. This module owns that probe. It runs the documented steps **in
order** and stops at the first failure:

1. DNS resolution of the URI host/port (:func:`socket.getaddrinfo`),
2. TCP connect,
3. TLS handshake when the URI uses ``mqtts://`` (system CA, or ``config.ca_file``),
4. authentication (the MQTT CONNECT/CONNACK),
5. publish then subscribe on a temporary **non-retained** topic,
6. disconnect.

Every step is bounded by a timeout and the whole function is exception-free:
``probe`` returns ``(ok, reason)`` with a short, non-secret reason. The password
and any URI userinfo are never echoed; the raw URI is never included (only the
host, which lives in the non-secret device document).

``paho-mqtt`` is imported lazily by the default backend factory, so this module
is import-safe on a host without it. When the client is unavailable the probe
returns a clear ``'mqtt client unavailable'`` failure. There is no broker
auto-discovery and no Home Assistant credential extraction (source §6.2).

The transport is injectable (``backend_factory`` plus the DNS/connect/TLS
hooks) so the whole probe is host-testable with no network. Stdlib only, and no
file/network/thread side effects on import.
"""
import argparse
import logging
import os
import secrets
import socket
import ssl
import threading
import urllib.parse

import mqtt_service

log = logging.getLogger('prusa-cam.mqtt_probe')

#: Default per-step timeout, in seconds; clamped into a sane range.
DEFAULT_TIMEOUT = 10.0
MIN_TIMEOUT = 0.1
MAX_TIMEOUT = 60.0

#: Bound a reason string so a hostile broker/host cannot bloat a response/log.
MAX_REASON_LENGTH = 200

#: QoS used for the test publish/subscribe (non-retained).
PROBE_QOS = 1

#: Payload published to the temporary test topic (non-secret).
PROBE_PAYLOAD = b'buddy3d-probe'


class ProbeStepError(Exception):
    """A probe step failed with a bounded, non-secret reason.

    The default backend raises this so the probe can report the exact failing
    step; an injected fake backend may raise it too. Any other exception is
    mapped to the step's generic reason.
    """

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class MqttClientUnavailable(Exception):
    """The MQTT client library (paho) is not installed."""


# --------------------------------------------------------------------------- #
# Injectable transport defaults
# --------------------------------------------------------------------------- #

def _default_resolve(host, port, timeout):
    """Resolve ``host``/``port`` with a bounded wall-clock timeout.

    ``socket.getaddrinfo`` has no timeout parameter, so it runs on a daemon
    thread and is abandoned after ``timeout``; a stalled resolver can therefore
    never hang the wizard. Raises the resolver's error (or ``TimeoutError``).
    """
    result = {}

    def worker():
        try:
            result['infos'] = socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM)
        except OSError as e:  # noqa: BLE001 - re-raised on the caller's thread
            result['error'] = e

    thread = threading.Thread(target=worker, name='mqtt-probe-dns', daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError('dns timeout')
    if 'error' in result:
        raise result['error']
    return result.get('infos') or []


def _default_connect(addrinfo, timeout):
    """Open and connect a TCP socket for one resolved ``addrinfo`` entry."""
    family, socktype, proto, _canonname, sockaddr = addrinfo
    sock = socket.socket(family, socktype, proto)
    try:
        sock.settimeout(timeout)
        sock.connect(sockaddr)
    except Exception:
        _close(sock)
        raise
    return sock


def _default_tls_handshake(sock, host, ca_file, timeout):
    """Wrap ``sock`` in TLS using the system CA or ``ca_file``."""
    context = ssl.create_default_context(cafile=ca_file or None)
    sock.settimeout(timeout)
    return context.wrap_socket(sock, server_hostname=host)


def default_backend(config, timeout):
    """Build the production probe backend backed by ``paho-mqtt``.

    The import is local so importing this module never requires paho; a missing
    client is reported as :class:`MqttClientUnavailable`.
    """
    try:
        import paho.mqtt.client as mqtt  # noqa: PLC0415 - lazy, import-safe
    except ImportError as e:
        raise MqttClientUnavailable('mqtt client unavailable') from e
    return PahoProbeBackend(config, mqtt, timeout)


def _close(sock):
    """Best-effort close of ``sock``; never raises."""
    try:
        sock.close()
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# URI helpers
# --------------------------------------------------------------------------- #

def _host_port(uri):
    """Return ``(host, port)`` for a broker URI, or ``('', 0)`` when invalid."""
    if not isinstance(uri, str) or not uri:
        return '', 0
    try:
        parts = urllib.parse.urlsplit(uri)
    except ValueError:
        return '', 0
    if parts.scheme not in ('mqtt', 'mqtts'):
        return '', 0
    host = parts.hostname or ''
    if not host:
        return '', 0
    try:
        port = parts.port
    except ValueError:
        return '', 0
    if port is None:
        port = 8883 if parts.scheme == 'mqtts' else 1883
    if not 1 <= port <= 65535:
        return '', 0
    return host, port


def _is_tls(uri):
    try:
        return urllib.parse.urlsplit(uri).scheme == 'mqtts'
    except ValueError:
        return False


def _bound_timeout(timeout):
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    if value != value:  # NaN
        return DEFAULT_TIMEOUT
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, value))


def _bounded_reason(reason):
    """Return a bounded, control-free reason string."""
    if not isinstance(reason, str) or not reason:
        return 'mqtt probe failed'
    cleaned = ''.join(ch for ch in reason if ch.isprintable())
    cleaned = cleaned.strip()
    return (cleaned or 'mqtt probe failed')[:MAX_REASON_LENGTH]


def _safe_reason(reason, config):
    """Return a bounded reason with any supplied credential scrubbed out.

    The probe's own reasons never contain a credential; this is defence in
    depth for a reason supplied by an injected backend, so a password or
    username can never be echoed (AC-23 tail).
    """
    text = _bounded_reason(reason)
    for secret in (getattr(config, 'password', ''), getattr(config, 'username', '')):
        if isinstance(secret, str) and secret:
            text = text.replace(secret, '<redacted>')
    return text


def _probe_topic(config):
    """Return a temporary, non-retained test topic unique to this probe."""
    prefix = getattr(config, 'topic_prefix', '') or 'buddy3d'
    if not isinstance(prefix, str) or not prefix:
        prefix = 'buddy3d'
    return f'{prefix}/probe/{secrets.token_hex(8)}'


# --------------------------------------------------------------------------- #
# Probe
# --------------------------------------------------------------------------- #

def probe(config, *, backend_factory=None, timeout=DEFAULT_TIMEOUT,
          resolve=None, connect=None, tls_handshake=None):
    """Run the broker connection test; return ``(ok, reason)`` (never raises).

    ``backend_factory(config, timeout)`` returns an object with
    ``connect``/``publish``/``subscribe``/``disconnect``; the default wraps
    paho. ``resolve``/``connect``/``tls_handshake`` default to the real network
    operations and are injectable so tests run with no network.
    """
    try:
        return _probe(
            config,
            backend_factory=backend_factory,
            timeout=timeout,
            resolve=resolve,
            connect=connect,
            tls_handshake=tls_handshake,
        )
    except Exception as e:  # noqa: BLE001 - the probe must never raise
        log.debug('mqtt_probe: unexpected failure: %s', type(e).__name__)
        return False, 'mqtt probe failed'


def _probe(config, *, backend_factory, timeout, resolve, connect, tls_handshake):
    timeout = _bound_timeout(timeout)
    uri = getattr(config, 'uri', '') or ''
    host, port = _host_port(uri)
    if not host:
        return False, 'mqtt uri is invalid'

    # The client is required for authentication and pub/sub; report its absence
    # clearly before touching the network.
    factory = backend_factory or default_backend
    try:
        backend = factory(config, timeout)
    except (MqttClientUnavailable, ImportError):
        return False, 'mqtt client unavailable'
    except Exception as e:  # noqa: BLE001
        log.debug('mqtt_probe: backend unavailable: %s', type(e).__name__)
        return False, 'mqtt client unavailable'

    resolver = resolve or _default_resolve
    connector = connect or _default_connect
    handshaker = tls_handshake or _default_tls_handshake

    # 1. DNS resolution.
    try:
        infos = resolver(host, port, timeout)
    except Exception:  # noqa: BLE001 - a resolver failure is a probe failure
        return False, _safe_reason(f'could not resolve {host}', config)
    if not infos:
        return False, _safe_reason(f'could not resolve {host}', config)

    # 2. TCP connect, then 3. TLS handshake when mqtts://.
    addrinfo = infos[0]
    try:
        raw = connector(addrinfo, timeout)
    except Exception:  # noqa: BLE001
        return False, _safe_reason(f'could not connect to {host}:{port}', config)
    if _is_tls(uri):
        try:
            tls_sock = handshaker(raw, host, getattr(config, 'ca_file', ''), timeout)
        except Exception:  # noqa: BLE001
            _close(raw)
            return False, 'TLS handshake failed'
        _close(tls_sock)
    else:
        _close(raw)

    # 4. Authentication, then 5. publish/subscribe, then 6. disconnect.
    try:
        try:
            backend.connect()
        except ProbeStepError as e:
            return False, _safe_reason(e.reason, config)
        except Exception as e:  # noqa: BLE001
            log.debug('mqtt_probe: connect failed: %s', type(e).__name__)
            return False, 'authentication failed'

        topic = _probe_topic(config)
        try:
            backend.publish(topic, PROBE_PAYLOAD, qos=PROBE_QOS, retain=False)
        except ProbeStepError as e:
            return False, _safe_reason(e.reason, config)
        except Exception as e:  # noqa: BLE001
            log.debug('mqtt_probe: publish failed: %s', type(e).__name__)
            return False, 'publish failed'

        try:
            backend.subscribe(topic, qos=PROBE_QOS)
        except ProbeStepError as e:
            return False, _safe_reason(e.reason, config)
        except Exception as e:  # noqa: BLE001
            log.debug('mqtt_probe: subscribe failed: %s', type(e).__name__)
            return False, 'subscribe failed'
    finally:
        try:
            backend.disconnect()
        except Exception:  # noqa: BLE001 - disconnect must never mask the result
            pass

    return True, 'connected'


# --------------------------------------------------------------------------- #
# Default paho backend (lazily imported)
# --------------------------------------------------------------------------- #

class PahoProbeBackend:
    """Minimal ``paho.mqtt.client`` adapter used by :func:`default_backend`.

    ``connect`` performs authentication (CONNECT/CONNACK) and waits for the
    broker's reply; ``subscribe`` waits for the SUBACK; ``publish`` waits for the
    QoS-1 PUBACK. Each wait is bounded by the probe timeout.
    """

    def __init__(self, config, mqtt_module, timeout):
        self._config = config
        self._mqtt = mqtt_module
        self._timeout = timeout
        self._client = None
        self._connected = threading.Event()
        self._subscribed = threading.Event()
        self._connect_rc = None

    def _make_client(self):
        client_id = f'buddy3d-probe-{secrets.token_hex(4)}'
        api = getattr(self._mqtt, 'CallbackAPIVersion', None)
        try:
            if api is not None:
                return self._mqtt.Client(api.VERSION2, client_id=client_id)
            return self._mqtt.Client(client_id=client_id)
        except TypeError:  # older paho without the callback-API enum
            return self._mqtt.Client(client_id=client_id)

    def connect(self):
        client = self._make_client()
        if self._config.username:
            client.username_pw_set(
                self._config.username, self._config.password or None)
        if self._config.uri.startswith('mqtts://'):
            if self._config.ca_file:
                client.tls_set(ca_certs=self._config.ca_file)
            else:
                client.tls_set()
        client.on_connect = self._on_connect
        client.on_subscribe = self._on_subscribe
        host, port = _host_port(self._config.uri)
        client.connect(host, port, self._config.keepalive)
        client.loop_start()
        self._client = client
        if not self._connected.wait(self._timeout):
            raise ProbeStepError('authentication failed')
        if self._connect_rc != 0:
            raise ProbeStepError('authentication failed')

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        self._connect_rc = rc
        self._connected.set()

    def _on_subscribe(self, client, userdata, mid, granted_qos, properties=None):
        self._subscribed.set()

    def publish(self, topic, payload, qos=0, retain=False):
        if self._client is None:
            raise ProbeStepError('publish failed')
        try:
            info = self._client.publish(topic, payload, qos=qos, retain=retain)
            info.wait_for_publish(timeout=self._timeout)
        except Exception as e:  # noqa: BLE001 - any publish failure is bounded
            raise ProbeStepError('publish failed') from e

    def subscribe(self, topic, qos=0):
        if self._client is None:
            raise ProbeStepError('subscribe failed')
        try:
            self._client.subscribe(topic, qos=qos)
        except Exception as e:  # noqa: BLE001
            raise ProbeStepError('subscribe failed') from e
        if not self._subscribed.wait(self._timeout):
            raise ProbeStepError('subscribe failed')

    def disconnect(self):
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            client.loop_stop()
        finally:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# Optional manual CLI
# --------------------------------------------------------------------------- #

def _read_password(source):
    """Return the broker password from ``--password-file`` or an env var.

    The password is deliberately never accepted as a command-line argument:
    argv is world-readable via ``/proc/<pid>/cmdline`` and shell history.
    """
    if source:
        try:
            with open(source, encoding='utf-8') as handle:
                return handle.read().rstrip('\r\n')
        except OSError:
            return ''
    return os.environ.get('PRUSA_MQTT_PASSWORD', '')


def main(argv=None):
    """Manual broker test CLI; prints ``ok``/``failed`` and a bounded reason.

    The password is read from ``--password-file`` (or ``PRUSA_MQTT_PASSWORD``),
    never from argv; only the bounded reason (which never contains a credential)
    is printed.
    """
    parser = argparse.ArgumentParser(
        description='Test an MQTT broker connection (no config is saved)')
    parser.add_argument('uri', help='mqtt:// or mqtts:// broker URI (no credentials)')
    parser.add_argument('--username', default='')
    parser.add_argument(
        '--password-file',
        default='',
        help='file containing the password (or set PRUSA_MQTT_PASSWORD)',
    )
    parser.add_argument('--ca-file', default='')
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    config = mqtt_service.MqttConfig(
        enabled=True,
        uri=args.uri,
        username=args.username,
        password=_read_password(args.password_file),
        ca_file=args.ca_file,
    )
    ok, reason = probe(config, timeout=args.timeout)
    print('ok' if ok else 'failed', reason)
    return 0 if ok else 1


__all__ = [
    'DEFAULT_TIMEOUT',
    'MAX_REASON_LENGTH',
    'PROBE_QOS',
    'PROBE_PAYLOAD',
    'MqttClientUnavailable',
    'ProbeStepError',
    'PahoProbeBackend',
    'default_backend',
    'probe',
    'main',
]


if __name__ == '__main__':
    raise SystemExit(main())
