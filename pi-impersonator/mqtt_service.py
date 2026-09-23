"""MQTT runtime service for the public appliance (WP-5b; AC-23/24/25/27/28).

Source of truth: ``docs/public-appliance-distribution-plan.md`` §6.2 (broker
configuration), §6.3 (topics/state/LWT/commands) and §6.4 (discovery + birth +
command rules). The pure builders live in :mod:`mqtt_topics` (topic contract) and
:mod:`mqtt_state` (state JSON + HA discovery); this module is the runtime that
connects them to a broker and to the shared :class:`settings_coordinator.SettingsCoordinator`.

Broker I/O is injectable
------------------------
Every broker operation goes through a tiny :class:`MqttBackend` protocol
(``connect``/``publish``/``subscribe``/``set_will``/``disconnect`` plus
``set_message_callback`` for inbound delivery). The production default wraps
``paho-mqtt``; that import happens *lazily* inside :func:`default_backend` so this
module is import-safe on a host without paho. Tests inject a fake backend and an
injected clock/sleeper/jitter, so no network, broker, or real sleep is used.

There is **no broker auto-discovery and no credential extraction** from Home
Assistant (source §6.2): the URI, username/password, and optional custom CA come
only from the durable device/secrets documents. MQTT stays disabled unless
``mqtt.enabled`` is true *and* the URI is a valid ``mqtt://``/``mqtts://`` URL
without embedded userinfo.

Failure isolation
-----------------
Every backend call and every coordinator call is wrapped: an MQTT/TLS/broker
error is logged through :func:`admin_auth.redact` and never propagates to Prusa
signaling or local media. A disconnect or a hostile command cannot stop the
source, registration, snapshots, RTSP, or WebRTC.

No MQTT camera entity (AC-28)
-----------------------------
ONVIF remains the media interface. This module publishes no ``camera``
component; the two expected Home Assistant entries are ``<camera name>`` (ONVIF,
the camera entity) and ``<camera name> Controls`` (MQTT, settings + diagnostics).

Stdlib only, and no file/network/thread side effects on import.
"""
import json
import logging
import os
import random
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

import admin_auth
import mqtt_state
import mqtt_topics

log = logging.getLogger('prusa-cam.mqtt')

# --------------------------------------------------------------------------- #
# Bounds and constants
# --------------------------------------------------------------------------- #

#: Largest accepted command payload, in bytes (source §6.4 "parse a bounded
#: UTF-8 payload"). A bigger frame is rejected before any coordinator call.
MAX_PAYLOAD_BYTES = 256

#: How long a ``last_command_error`` reason is published before it is cleared
#: even without a later successful command (source §6.4 "or after a short
#: timeout"). The clock is injectable.
COMMAND_ERROR_TTL = 60.0

#: Birth/reconnect republish delay window (source §6.4: randomized 0-5 s).
BIRTH_DELAY_MIN = 0.0
BIRTH_DELAY_MAX = 5.0

#: Reconnect backoff: exponential from ``BASE`` up to ``MAX`` with a bounded
#: positive jitter of at most ``FRACTION`` of the base (source §6.2).
RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 60.0
RECONNECT_JITTER_FRACTION = 0.25

#: MQTT keepalive (seconds) and the QoS used for commands/state/discovery.
DEFAULT_KEEPALIVE = 60
COMMAND_QOS = 1

#: Availability payloads and the HA button press payload.
ONLINE = 'online'
OFFLINE = 'offline'
PRESS = 'press'

#: The literal ``update/install`` payload declared by the HA discovery document
#: (``mqtt_state`` sets ``payload_install: 'install'``, AC-31). Anything else is
#: rejected before the privileged install trigger is called.
INSTALL_PAYLOAD = 'install'

#: State-document metric overrides a caller may supply through
#: ``metrics_provider``. ``application_version``/``last_command_error`` are owned
#: by the service itself and are intentionally excluded.
_STATE_METRIC_KEYS = frozenset({
    'quality', 'snapshot_upload', 'snapshot_interval', 'timelapse_enabled',
    'timelapse_interval', 'timelapse_fps', 'prusa_rtsp', 'webrtc',
    'prusa_connected', 'camera_source', 'storage_free_bytes', 'wifi_rssi_dbm',
    'cpu_temperature_c', 'uptime_seconds',
})

#: Published quality name -> firmware raw event byte accepted by
#: ``SettingsCoordinator.set_quality`` (``state.RAW_TO_ENUM``).
_QUALITY_RAW = {'sd': 5, 'hd': 6, 'fhd': 7}
_TRUE_WORDS = frozenset({'true', '1', 'on', 'yes'})
_FALSE_WORDS = frozenset({'false', '0', 'off', 'no'})


class CommandError(ValueError):
    """A malformed command payload; converted to a bounded rejection reason."""


@dataclass
class CommandResult:
    """Outcome of one inbound command (never raised to the caller)."""

    ok: bool
    reason: Optional[str] = None
    changed: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Broker backend protocol
# --------------------------------------------------------------------------- #

@runtime_checkable
class MqttBackend(Protocol):
    """The complete broker surface the service depends on.

    Inbound messages are delivered through the callback registered with
    :meth:`set_message_callback` as ``callback(topic, payload, retain)`` where
    ``payload`` is raw bytes. The service bounds and decodes the payload itself.
    """

    def connect(self) -> None:
        """Connect to the broker (and start any background loop)."""

    def publish(self, topic, payload, qos=0, retain=False) -> None:
        """Publish ``payload`` to ``topic``."""

    def subscribe(self, topic, qos=0) -> None:
        """Subscribe to ``topic`` at ``qos``."""

    def set_will(self, topic, payload, qos=0, retain=False) -> None:
        """Register the broker Last Will and Testament."""

    def disconnect(self) -> None:
        """Disconnect and stop any background loop."""

    def set_message_callback(self, callback) -> None:
        """Register the inbound-message callback."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MqttConfig:
    """Validated MQTT runtime configuration (device + secrets documents).

    ``enabled`` is True only when the device document sets ``mqtt.enabled`` and
    every value validates: a credential-free ``mqtt://``/``mqtts://`` URI, safe
    prefixes, an optional absolute CA path, and a non-empty stable device id
    derived from the persisted fingerprint/seed. The username/password come from
    the secrets document; they are never taken from the URI, from Home
    Assistant, or from broker auto-discovery.
    """

    enabled: bool = False
    uri: str = ''
    client_id: str = ''
    discovery_prefix: str = mqtt_topics.DEFAULT_DISCOVERY_PREFIX
    topic_prefix: str = mqtt_topics.DEFAULT_BASE_PREFIX
    ca_file: str = ''
    # Credentials are excluded from ``repr`` so a logged/traced config cannot
    # disclose the broker password or username (AC-25).
    username: str = field(default='', repr=False)
    password: str = field(default='', repr=False)
    device_id: str = ''
    keepalive: int = DEFAULT_KEEPALIVE

    @classmethod
    def from_documents(cls, device, secrets, device_seed=None):
        """Build a config from ``config_schema`` device + secrets documents.

        ``device_seed`` overrides the persisted ``fingerprint`` used to derive
        the stable device id. Anything that fails validation leaves the config
        disabled; the raw values are still returned for diagnostics but are never
        logged with secrets.
        """
        device = device if isinstance(device, dict) else {}
        secrets = secrets if isinstance(secrets, dict) else {}
        mqtt = device.get('mqtt') if isinstance(device.get('mqtt'), dict) else {}
        secret_mqtt = secrets.get('mqtt') if isinstance(secrets.get('mqtt'), dict) else {}

        enabled = mqtt.get('enabled') is True
        uri = mqtt.get('uri') if isinstance(mqtt.get('uri'), str) else ''
        client_id = mqtt.get('client_id') if isinstance(mqtt.get('client_id'), str) else ''
        ca_file = mqtt.get('ca_file') if isinstance(mqtt.get('ca_file'), str) else ''
        discovery_prefix = (
            mqtt.get('discovery_prefix')
            if isinstance(mqtt.get('discovery_prefix'), str)
            and mqtt.get('discovery_prefix')
            else mqtt_topics.DEFAULT_DISCOVERY_PREFIX
        )
        topic_prefix = (
            mqtt.get('topic_prefix')
            if isinstance(mqtt.get('topic_prefix'), str)
            and mqtt.get('topic_prefix')
            else mqtt_topics.DEFAULT_BASE_PREFIX
        )
        raw_user = secret_mqtt.get('username')
        raw_pass = secret_mqtt.get('password')
        username = raw_user if isinstance(raw_user, str) else ''
        password = raw_pass if isinstance(raw_pass, str) else ''

        seed = device_seed
        if not (isinstance(seed, str) and seed.strip()):
            seed = device.get('fingerprint')
        device_id = ''
        if isinstance(seed, str) and seed.strip():
            try:
                device_id = mqtt_topics.device_id(seed)
            except ValueError:
                device_id = ''

        valid = (
            _valid_uri(uri)
            and _valid_ca_file(ca_file)
            and bool(device_id)
            and _valid_prefix(discovery_prefix, 'discovery_prefix')
            and _valid_prefix(topic_prefix, 'topic_prefix')
        )
        return cls(
            enabled=bool(enabled and valid),
            uri=uri,
            client_id=client_id,
            discovery_prefix=discovery_prefix,
            topic_prefix=topic_prefix,
            ca_file=ca_file,
            username=username,
            password=password,
            device_id=device_id,
            keepalive=DEFAULT_KEEPALIVE,
        )

    @property
    def effective_client_id(self):
        """Return the configured client id, or a stable device-derived default."""
        if self.client_id.strip():
            return self.client_id.strip()
        return f'buddy3d-{self.device_id}' if self.device_id else 'buddy3d-camera'


def _valid_uri(uri):
    """True for a credential-free ``mqtt://``/``mqtts://`` URL with a host."""
    if not isinstance(uri, str) or not uri:
        return False
    try:
        parts = urllib.parse.urlsplit(uri)
    except ValueError:
        return False
    if parts.scheme not in ('mqtt', 'mqtts'):
        return False
    if not parts.hostname:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    # A broker URI is host/port only: a path, query, or fragment is not part of
    # the documented contract and is rejected rather than silently ignored.
    if parts.path or parts.query or parts.fragment:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    return True


def _valid_ca_file(path):
    """True when ``path`` is empty or a safe absolute CA file location.

    Existence is not required at build time (the broker connect reports a missing
    file), but a NUL/CR/LF or a relative path is rejected outright.
    """
    if not path:
        return True
    if '\x00' in path or '\n' in path or '\r' in path:
        return False
    if not os.path.isabs(path):
        return False
    if os.path.exists(path) and not os.path.isfile(path):
        return False
    return True


def _valid_prefix(prefix, name):
    try:
        mqtt_topics.validate_prefix(prefix, name)
        return True
    except ValueError:
        return False


def _uri_host_port(uri):
    """Return ``(host, port)`` for a validated broker URI."""
    parts = urllib.parse.urlsplit(uri)
    host = parts.hostname or ''
    port = parts.port
    if port is None:
        port = 8883 if parts.scheme == 'mqtts' else 1883
    return host, port


# --------------------------------------------------------------------------- #
# Default paho-mqtt backend (imported lazily)
# --------------------------------------------------------------------------- #

def _spawn_daemon(func):
    """Run ``func`` on a short-lived daemon thread (the production dispatcher)."""
    threading.Thread(target=func, name='mqtt-task', daemon=True).start()


def default_backend(config, on_reconnect=None):
    """Build the production :class:`MqttBackend` backed by ``paho-mqtt``.

    The import is local so importing this module never requires paho. On every
    successful (re)connect, ``on_reconnect`` is dispatched on a short-lived
    daemon thread so the broker network loop is never blocked by the 0-5 s birth
    delay.
    """
    import paho.mqtt.client as mqtt  # noqa: PLC0415 - lazy so the module is import-safe

    return PahoBackend(config, mqtt, on_reconnect=on_reconnect)


class PahoBackend:
    """Thin adapter over ``paho.mqtt.client`` implementing :class:`MqttBackend`.

    Kept deliberately small and untested on the host: the broker surface is
    exercised through the fake backend in the test suite.
    """

    def __init__(self, config, mqtt_module, on_reconnect=None):
        self._config = config
        self._mqtt = mqtt_module
        self._on_reconnect = on_reconnect
        self._client = None
        self._callback = None
        self._will = None

    def set_message_callback(self, callback):
        self._callback = callback

    def set_will(self, topic, payload, qos=0, retain=False):
        self._will = (topic, payload, qos, retain)

    def connect(self):
        client = self._make_client()
        if self._config.username:
            client.username_pw_set(self._config.username, self._config.password or None)
        if self._config.uri.startswith('mqtts://'):
            if self._config.ca_file:
                client.tls_set(ca_certs=self._config.ca_file)
            else:
                client.tls_set()
        if self._will is not None:
            client.will_set(*self._will)
        client.on_message = self._on_message
        client.on_connect = self._on_connect
        host, port = _uri_host_port(self._config.uri)
        client.connect(host, port, self._config.keepalive)
        client.loop_start()
        self._client = client

    def _make_client(self):
        client_id = self._config.effective_client_id
        api = getattr(self._mqtt, 'CallbackAPIVersion', None)
        try:
            if api is not None:
                return self._mqtt.Client(api.VERSION2, client_id=client_id)
            return self._mqtt.Client(client_id=client_id)
        except TypeError:  # older paho without the callback-API enum
            return self._mqtt.Client(client_id=client_id)

    def _on_message(self, client, userdata, message):
        callback = self._callback
        if callback is None:
            return
        callback(message.topic, message.payload, bool(getattr(message, 'retain', False)))

    def _on_connect(self, *args):
        if self._on_reconnect is not None:
            self._on_reconnect()

    def publish(self, topic, payload, qos=0, retain=False):
        if self._client is None:
            raise RuntimeError('mqtt backend is not connected')
        self._client.publish(topic, payload, qos=qos, retain=retain)

    def subscribe(self, topic, qos=0):
        if self._client is None:
            raise RuntimeError('mqtt backend is not connected')
        self._client.subscribe(topic, qos=qos)

    def disconnect(self):
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            client.loop_stop()
        finally:
            client.disconnect()


# --------------------------------------------------------------------------- #
# Payload helpers
# --------------------------------------------------------------------------- #

def _decode_payload(payload):
    """Return the bounded UTF-8 text of ``payload`` or raise :class:`CommandError`."""
    if isinstance(payload, str):
        raw = payload.encode('utf-8')
    elif isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
    else:
        raise CommandError('payload must be text')
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise CommandError('payload too large')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        raise CommandError('payload is not valid UTF-8') from None


def _payload_text(payload):
    """Best-effort bounded text for non-command topics; ``None`` when malformed."""
    try:
        return _decode_payload(payload)
    except CommandError:
        return None


def _parse_bool(text):
    value = text.strip().lower()
    if value in _TRUE_WORDS:
        return True
    if value in _FALSE_WORDS:
        return False
    return None


def _parse_int(text):
    value = text.strip()
    if not value or not value.lstrip('-').isdigit():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_quality(text):
    value = text.strip().lower()
    if value in _QUALITY_RAW:
        return _QUALITY_RAW[value]
    if value in ('5', '6', '7'):
        return int(value)
    return None


def _normalize_action_result(result):
    """Normalize a button/install action result to ``(ok, reason)``.

    Accepts the documented forms: a ``(ok, reason)`` tuple, a result object with
    ``ok``/``reason`` attributes (for example ``privileged.PrivilegedResult``), or
    a truthy/falsy scalar. ``reason`` is only returned when it is a string; it is
    bounded/redacted later by :meth:`MqttService._set_command_error`.
    """
    if isinstance(result, tuple) and len(result) == 2:
        reason = result[1] if isinstance(result[1], str) else ''
        return bool(result[0]), reason
    if hasattr(result, 'ok'):
        reason = getattr(result, 'reason', '')
        return bool(getattr(result, 'ok', False)), reason if isinstance(reason, str) else ''
    return bool(result), ''


# --------------------------------------------------------------------------- #
# The runtime service
# --------------------------------------------------------------------------- #

class MqttService:
    """Publisher/subscription wiring and command handling for one appliance.

    ``coordinator`` is the sole settings mutation path; its ``state`` supplies
    the authoritative settings and its setters persist before this service
    publishes the updated retained state. All hardware/broker access is
    injectable and every failure is isolated.
    """

    def __init__(self, coordinator, config, backend=None, *, clock=None,
                 sleeper=None, jitter=None, secrets=(), application_version='',
                 serial=None, mac=None, build_timelapse=None, restart=None,
                 metrics_provider=None, dispatcher=None,
                 update_state_provider=None, update_install=None):
        self._coordinator = coordinator
        self._config = config
        self._backend = backend
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._jitter = jitter or random.uniform
        self._dispatch = dispatcher or _spawn_daemon
        self._application_version = application_version or ''
        self._serial = serial
        self._mac = mac
        self._build_timelapse = build_timelapse
        self._restart = restart
        self._metrics_provider = metrics_provider
        # AC-31: the HA update entity's state comes from a provider (typically
        # ``updater_install.read_update_state``) and the install command from a
        # privileged trigger (``privileged.install_update``). Both are optional:
        # without them the update topics stay inert and the rest of the service
        # is unaffected.
        self._update_state_provider = update_state_provider
        self._update_install = update_install
        self._last_command_error = None
        self._command_error_at = None
        self._error_lock = threading.Lock()
        self._error_worker_running = False
        self._error_pending = False
        self._started = False
        self._reconnect_attempt = 0
        self._discovery_prefix = config.discovery_prefix
        # Broker credentials are secrets too: never echo them in a log or payload.
        self._secrets = self._collect_secrets(
            tuple(secrets or ()) + (config.username, config.password))

    # -- identity / topics -------------------------------------------------

    @staticmethod
    def _collect_secrets(secrets):
        values = []
        for value in secrets or ():
            if isinstance(value, str) and value:
                values.append(value)
        return tuple(dict.fromkeys(values))

    @property
    def config(self):
        return self._config

    @property
    def device_id(self):
        return self._config.device_id

    @property
    def availability_topic(self):
        return mqtt_topics.availability(self.device_id, self._config.topic_prefix)

    @property
    def state_topic(self):
        return mqtt_topics.state(self.device_id, self._config.topic_prefix)

    @property
    def discovery_topic(self):
        return mqtt_topics.discovery_device_topic(
            self._discovery_prefix, self.device_id)

    @property
    def update_state_topic(self):
        """``update/state``: retained HA update-state JSON (AC-31)."""
        return mqtt_topics.update_state(self.device_id, self._config.topic_prefix)

    @property
    def update_install_topic(self):
        """``update/install``: non-retained install command (AC-31)."""
        return mqtt_topics.update_install(self.device_id, self._config.topic_prefix)

    @property
    def last_command_error(self):
        return self._last_command_error

    def command_topic(self, name):
        """Return the exact ``command/<name>`` topic for this device."""
        return mqtt_topics.command(self.device_id, name, self._config.topic_prefix)

    def command_topics(self):
        """Return every ``command/<name>`` topic, in ``COMMAND_NAMES`` order."""
        return [self.command_topic(name) for name in mqtt_topics.COMMAND_NAMES]

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        """Connect and publish/subscribe; False (and no I/O) when disabled.

        Order matters: the retained LWT is registered *before* connect, then the
        retained ``online`` availability, the QoS-1 command/HA-status
        subscriptions, and finally the retained authoritative state and
        discovery documents.
        """
        if not self._config.enabled:
            log.info('mqtt: disabled or unconfigured; not connecting')
            return False
        if self._config.ca_file and self._config.uri.startswith('mqtt://'):
            # Non-secret warning: a custom CA has no effect on a plaintext link.
            log.warning(
                'mqtt: ca_file is configured but the broker URI uses plain '
                'mqtt://; TLS is not in use')
        if self._backend is None:
            try:
                self._backend = default_backend(
                    self._config, on_reconnect=self._reconnect_in_thread)
            except Exception as e:  # paho missing / construction failure
                self._log('mqtt: default backend unavailable: %s', e)
                return False
        try:
            self._backend.set_message_callback(self._on_message)
            self._backend.set_will(
                self.availability_topic, OFFLINE.encode('utf-8'),
                qos=COMMAND_QOS, retain=True)
            self._backend.connect()
        except Exception as e:
            self._log('mqtt: connect failed: %s', e)
            self._started = False
            return False

        self._started = True
        self._reconnect_attempt = 0
        self._publish_availability(ONLINE)
        self._subscribe_topics()
        self.publish_state()
        self.publish_discovery()
        self.publish_update_state()
        return True

    def stop(self):
        """Publish a clean retained ``offline`` and disconnect; never raises."""
        if not self._config.enabled or self._backend is None:
            return False
        if self._started:
            self._publish_availability(OFFLINE)
        self._guard(self._backend.disconnect)
        self._started = False
        return True

    def _subscribe_topics(self):
        for topic in self.command_topics():
            self._guard(self._backend.subscribe, topic, qos=COMMAND_QOS)
        self._guard(
            self._backend.subscribe, mqtt_topics.HA_STATUS_TOPIC, qos=COMMAND_QOS)
        # AC-31: the update install command is a separate topic, not a
        # ``command/<name>`` leaf, so it is subscribed explicitly here.
        self._guard(
            self._backend.subscribe, self.update_install_topic, qos=COMMAND_QOS)

    def _publish_availability(self, value):
        self._publish(
            self.availability_topic, value.encode('utf-8'),
            qos=COMMAND_QOS, retain=True)

    def _reconnect_in_thread(self):
        """Dispatch the birth republish off the broker's network thread.

        Used for the HA ``online`` birth and by the backend on reconnect, so a
        0-5 s birth delay never blocks paho's network loop.
        """
        self._dispatch(self.on_reconnect)

    def on_reconnect(self):
        """Republish discovery + state after a randomized 0-5 s delay.

        Invoked on the HA ``online`` birth and by the backend on reconnect
        (source §6.4). The delay uses the injected ``sleeper``/``jitter``.
        """
        self._schedule_birth()

    def _schedule_birth(self):
        delay = self._bounded_jitter(BIRTH_DELAY_MIN, BIRTH_DELAY_MAX)
        if delay > 0:
            self._guard(self._sleeper, delay)
        self.publish_discovery()
        self.publish_state()
        self.publish_update_state()
        self._reconnect_attempt = 0

    # -- publishing --------------------------------------------------------

    def publish_state(self):
        """Publish the retained authoritative state document; returns the dict."""
        self._expire_command_error()
        try:
            document = self._build_state_document()
        except Exception as e:  # a builder failure must not escape to Prusa/media
            self._log('mqtt: could not build state document: %s', e)
            return None
        payload = self._encode(document)
        if payload is None:
            self._log('mqtt: could not encode state document; keeping last retained')
            return None
        self._publish(self.state_topic, payload, qos=COMMAND_QOS, retain=True)
        return document

    def publish_update_state(self):
        """Publish the retained HA update-state document; returns the dict.

        The document is supplied by ``update_state_provider`` (AC-31). On a
        missing provider, a provider failure, a non-dict/empty result, or an
        encoding failure the last retained document is kept: this method never
        publishes ``{}`` over good state and never raises.
        """
        if self._update_state_provider is None:
            return None
        try:
            document = self._update_state_provider()
        except Exception as e:  # a provider failure must not escape (AC-27)
            self._log('mqtt: update state provider failed: %s', e)
            return None
        if not isinstance(document, dict) or not document:
            # Absent/failed provider: keep the last retained good document.
            return None
        payload = self._encode(document)
        if payload is None:
            self._log('mqtt: could not encode update state; keeping last retained')
            return None
        self._publish(self.update_state_topic, payload, qos=COMMAND_QOS, retain=True)
        return document

    def publish_discovery(self):
        """Publish the retained HA discovery document; returns the dict."""
        try:
            document = self._build_discovery_document()
        except Exception as e:
            self._log('mqtt: could not build discovery document: %s', e)
            return None
        payload = self._encode(document)
        if payload is None:
            self._log('mqtt: could not encode discovery document; keeping last retained')
            return None
        self._publish(self.discovery_topic, payload, qos=COMMAND_QOS, retain=True)
        return document

    def clear_discovery(self):
        """Publish an empty retained payload to the current discovery topic."""
        self._publish(self.discovery_topic, b'', qos=COMMAND_QOS, retain=True)

    def clear_topic(self, topic):
        """Publish an empty retained payload to ``topic`` (ghost cleanup)."""
        self._publish(topic, b'', qos=COMMAND_QOS, retain=True)

    def set_discovery_prefix(self, new_prefix):
        """Clear the old discovery topic and republish under ``new_prefix``."""
        try:
            mqtt_topics.validate_prefix(new_prefix, 'discovery_prefix')
        except ValueError as e:
            self._log('mqtt: invalid discovery prefix: %s', e)
            return False
        old_topic = self.discovery_topic
        if new_prefix != self._discovery_prefix:
            self._publish(old_topic, b'', qos=COMMAND_QOS, retain=True)
        self._discovery_prefix = new_prefix
        self.publish_discovery()
        return True

    def _build_state_document(self):
        metrics = {}
        if self._metrics_provider is not None:
            try:
                supplied = self._metrics_provider()
                if isinstance(supplied, dict):
                    metrics = {
                        key: value for key, value in supplied.items()
                        if key in _STATE_METRIC_KEYS
                    }
            except Exception as e:
                self._log('mqtt: metrics provider failed: %s', e)
        return mqtt_state.build_state(
            getattr(self._coordinator, 'state', None),
            application_version=self._application_version,
            last_command_error=self._last_command_error,
            secrets=self._secrets,
            **metrics)

    def _build_discovery_document(self):
        state = getattr(self._coordinator, 'state', None)
        name = getattr(state, 'camera_name', '') if state is not None else ''
        return mqtt_state.build_discovery(
            self.device_id,
            admin_auth.redact(str(name), self._secrets),
            admin_auth.redact(self._application_version, self._secrets),
            base_prefix=self._config.topic_prefix,
            discovery_prefix=self._discovery_prefix,
            serial=admin_auth.redact(str(self._serial), self._secrets)
            if self._serial is not None else None,
            mac=self._mac)

    def _encode(self, document):
        """Return UTF-8 JSON bytes, or ``None`` when the document cannot encode.

        A failure must never publish ``{}`` over the last good retained document.
        """
        try:
            return json.dumps(document, separators=(',', ':')).encode('utf-8')
        except (TypeError, ValueError):
            return None

    # -- inbound messages --------------------------------------------------

    def _on_message(self, topic, payload, retain=False):
        """Backend callback: route one inbound message; never raises."""
        try:
            if topic == mqtt_topics.HA_STATUS_TOPIC:
                if _payload_text(payload) == ONLINE:
                    # Dispatch off the network thread: the 0-5 s birth delay
                    # must never block paho's loop.
                    self._reconnect_in_thread()
                return
            if retain:
                # Source §6.3/§6.4: ignore retained command messages defensively.
                log.debug('mqtt: ignoring retained command on %s', topic)
                return
            if topic == self.update_install_topic:
                # AC-31: the update install topic is not a ``command/<name>``
                # leaf, so it is routed to its own handler, never through
                # ``command_from_topic``.
                self.handle_update_install(payload)
                return
            self.handle_command(topic, payload)
        except Exception as e:  # absolute isolation from Prusa/media
            self._log('mqtt: inbound message handling failed: %s', e)

    def handle_command(self, topic, payload):
        """Apply one command topic; unknown topics are ignored (returns None)."""
        try:
            name = mqtt_topics.command_from_topic(
                topic, self.device_id, base_prefix=self._config.topic_prefix)
        except ValueError:
            return None
        result = self._apply_command(name, payload)
        if result.ok:
            self._clear_command_error()
        else:
            self._set_command_error(result.reason)
        # Persist/apply already happened inside the coordinator; publish the
        # authoritative state even after a rejection.
        self.publish_state()
        return result

    def handle_update_install(self, payload):
        """Handle one ``update/install`` command; never raises.

        Accepts only the literal discovery payload (:data:`INSTALL_PAYLOAD`).
        The injected ``update_install`` callable is normalized like the other
        command actions (``bool`` or ``(ok, reason)``). A bounded, redacted
        reason is surfaced through ``last_command_error`` and the retained state
        and update-state documents are republished. An MQTT/update failure here
        cannot disturb the Prusa or local media paths (AC-27).
        """
        result = self._apply_update_install(payload)
        if result.ok:
            self._clear_command_error()
        else:
            self._set_command_error(result.reason)
        # Publish both the authoritative settings state (so ``last_command_error``
        # is visible) and the update state (so HA reflects the install outcome).
        self.publish_state()
        self.publish_update_state()
        return result

    def _apply_update_install(self, payload):
        try:
            text = _decode_payload(payload)
        except CommandError as e:
            return CommandResult(False, str(e))
        if text.strip() != INSTALL_PAYLOAD:
            return CommandResult(False, 'expected install payload')
        if self._update_install is None:
            return CommandResult(False, 'update install unavailable')
        try:
            raw = self._update_install()
        except Exception as e:  # a privileged trigger failure must not escape
            self._log('mqtt: update install trigger failed: %s', e)
            return CommandResult(False, 'update install failed')
        ok, reason = _normalize_action_result(raw)
        if not ok:
            return CommandResult(False, reason or 'update install failed')
        return CommandResult(True)

    def _apply_command(self, name, payload):
        try:
            if name in ('timelapse_build', 'restart'):
                return self._apply_button(name, payload)
            text = _decode_payload(payload)
            handler = self._handlers().get(name)
            if handler is None:
                return CommandResult(False, 'unsupported command')
            return handler(text)
        except CommandError as e:
            return CommandResult(False, str(e))
        except Exception as e:
            self._log('mqtt: command %s failed: %s', name, e)
            return CommandResult(False, 'command failed')

    def _handlers(self):
        return {
            'quality': self._set_quality,
            'snapshot_upload': self._set_snapshot_upload,
            'snapshot_interval': self._set_snapshot_interval,
            'timelapse_enabled': self._set_timelapse_enabled,
            'timelapse_interval': self._set_timelapse_interval,
            'timelapse_fps': self._set_timelapse_fps,
            'prusa_rtsp': self._set_prusa_rtsp,
            'webrtc': self._set_webrtc,
        }

    @staticmethod
    def _from_result(result):
        return CommandResult(
            bool(getattr(result, 'ok', False)),
            getattr(result, 'reason', None),
            list(getattr(result, 'changed', []) or []))

    def _set_quality(self, text):
        raw = _parse_quality(text)
        if raw is None:
            raise CommandError('unknown quality value')
        return self._from_result(self._coordinator.set_quality(raw))

    def _set_snapshot_upload(self, text):
        enabled = _parse_bool(text)
        if enabled is None:
            raise CommandError('snapshot upload must be a boolean')
        return self._from_result(self._coordinator.set_snapshot_upload(enabled))

    def _set_snapshot_interval(self, text):
        seconds = _parse_int(text)
        if seconds is None:
            raise CommandError('snapshot interval must be an integer')
        return self._from_result(self._coordinator.set_snapshot_interval(seconds))

    def _set_timelapse_enabled(self, text):
        enabled = _parse_bool(text)
        if enabled is None:
            raise CommandError('timelapse flag must be a boolean')
        action = 'timelapse_enable' if enabled else 'timelapse_disable'
        return self._from_result(self._coordinator.set_timelapse_enabled(action))

    def _set_timelapse_interval(self, text):
        seconds = _parse_int(text)
        if seconds is None:
            raise CommandError('timelapse interval must be an integer')
        return self._from_result(self._coordinator.set_timelapse_interval(seconds))

    def _set_timelapse_fps(self, text):
        fps = _parse_int(text)
        if fps is None:
            raise CommandError('timelapse fps must be an integer')
        return self._from_result(self._coordinator.set_timelapse_fps(fps))

    def _set_prusa_rtsp(self, text):
        enabled = _parse_bool(text)
        if enabled is None:
            raise CommandError('prusa rtsp must be a boolean')
        return self._from_result(self._coordinator.set_rtsp_mode(2 if enabled else 1))

    def _set_webrtc(self, text):
        enabled = _parse_bool(text)
        if enabled is None:
            raise CommandError('webrtc must be a boolean')
        return self._from_result(self._coordinator.set_webrtc_mode(1 if enabled else 0))

    def _apply_button(self, name, payload):
        text = _decode_payload(payload)
        if text.strip().lower() != PRESS:
            raise CommandError('expected press payload')
        action = self._build_timelapse if name == 'timelapse_build' else self._restart
        if action is None:
            return CommandResult(False, f'{name} unavailable')
        try:
            ok = action()
        except Exception as e:
            self._log('mqtt: %s action failed: %s', name, e)
            return CommandResult(False, 'action failed')
        if not ok:
            return CommandResult(False, 'action failed')
        return CommandResult(True)

    # -- command error lifecycle ------------------------------------------

    def _set_command_error(self, reason):
        sanitized = mqtt_state.sanitize_command_error(reason, self._secrets)
        self._last_command_error = sanitized or 'command rejected'
        self._command_error_at = self._clock()
        self._schedule_error_clear()

    def _clear_command_error(self):
        self._last_command_error = None
        self._command_error_at = None

    def _expire_command_error(self):
        if self._last_command_error is None or self._command_error_at is None:
            return
        if self._clock() - self._command_error_at >= COMMAND_ERROR_TTL:
            self._clear_command_error()

    def _schedule_error_clear(self):
        """Start the single autonomous worker that clears a stale error.

        The worker sleeps on the injected ``sleeper`` until the injected clock
        passes :data:`COMMAND_ERROR_TTL`, then clears the error and republishes
        the retained state. It is dispatched (daemon thread by default) so the
        network loop is never blocked, and exits as soon as the error is cleared
        or a newer error reschedules it. Only one worker runs at a time.
        """
        with self._error_lock:
            if self._error_worker_running:
                # A worker is already waiting; it will re-read the newer error.
                self._error_pending = True
                return
            self._error_worker_running = True
            self._error_pending = False
        self._dispatch(self._error_clear_worker)

    def _error_clear_worker(self):
        try:
            while True:
                error = self._last_command_error
                at = self._command_error_at
                if error is None or at is None:
                    return
                remaining = (at + COMMAND_ERROR_TTL) - self._clock()
                if remaining <= 0:
                    # Re-check under the lock: a success may have cleared it.
                    if self._last_command_error is not None:
                        self._clear_command_error()
                        self.publish_state()
                    return
                if not self._guard(self._sleeper, remaining):
                    return  # a failing sleeper must not spin the worker
        finally:
            with self._error_lock:
                self._error_worker_running = False
                pending = self._error_pending
                self._error_pending = False
            if pending and self._last_command_error is not None:
                # An error arrived as this worker was exiting; reschedule once.
                self._schedule_error_clear()

    # -- reconnect backoff -------------------------------------------------

    def reconnect_delay(self, attempt):
        """Return a bounded exponential backoff delay for ``attempt``.

        The delay is the capped base plus a positive jitter of at most
        :data:`RECONNECT_JITTER_FRACTION` of that base, so it always lies in
        ``[base, base * (1 + FRACTION)]`` and never exceeds the capped maximum.
        """
        try:
            attempt = max(0, int(attempt))
        except (TypeError, ValueError):
            attempt = 0
        attempt = min(attempt, 30)  # cap the exponent; the delay is capped anyway
        base = min(RECONNECT_BASE_DELAY * (2 ** attempt), RECONNECT_MAX_DELAY)
        extra = self._bounded_jitter(0.0, base * RECONNECT_JITTER_FRACTION)
        ceiling = RECONNECT_MAX_DELAY * (1.0 + RECONNECT_JITTER_FRACTION)
        return min(base + extra, ceiling)

    def attempt_reconnect(self):
        """Sleep one jittered backoff interval and return the delay used."""
        delay = self.reconnect_delay(self._reconnect_attempt)
        self._reconnect_attempt += 1
        self._guard(self._sleeper, delay)
        return delay

    def _bounded_jitter(self, low, high):
        try:
            value = float(self._jitter(low, high))
        except Exception:
            return low
        if value != value:  # NaN
            return low
        return max(low, min(high, value))

    # -- low-level guarded I/O --------------------------------------------

    def _publish(self, topic, payload, qos=0, retain=False):
        if self._backend is None:
            return False
        try:
            self._backend.publish(topic, payload, qos=qos, retain=retain)
            return True
        except Exception as e:
            self._log('mqtt: publish to %s failed: %s', topic, e)
            return False

    def _guard(self, func, *args, **kwargs):
        try:
            func(*args, **kwargs)
            return True
        except Exception as e:
            self._log('mqtt: backend call failed: %s', e)
            return False

    def _log(self, fmt, *args):
        """Log a bounded, secret-redacted warning; never raises."""
        try:
            message = fmt % args if args else fmt
        except Exception:
            message = fmt
        try:
            message = admin_auth.redact(str(message), self._secrets)
        except Exception:
            message = 'mqtt: error'
        log.warning(message[:512])
