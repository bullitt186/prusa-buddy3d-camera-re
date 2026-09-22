"""Authoritative MQTT state JSON and HA discovery documents (WP-5a, AC-25, AC-26).

Source of truth: ``docs/public-appliance-distribution-plan.md`` §6.3 (state
JSON) and §6.4 (Home Assistant MQTT discovery).

Two pure builders live here, both host-testable and free of hardware, broker,
or file access:

* :func:`build_state` produces exactly the §6.3 state document with native JSON
  values. Callers pass a :class:`state.CameraState` and/or explicit settings and
  metrics; missing metrics become ``null``. It never reads hardware.
* :func:`build_discovery` produces the single retained HA **device discovery**
  document from §6.4: origin (``o``), device (``dev``), availability
  (``avty_t``), and an ``cmps`` mapping keyed by stable unique IDs.

Secret hygiene (AC-25)
----------------------
:func:`build_state` only ever reads a fixed allowlist of state attributes, so a
secret parked in an unexpected mapping key cannot reach the document. String
fields are bounded, control characters are stripped, ``last_command_error`` is
reduced to a non-secret reason (tracebacks and path-like tokens are removed),
and any literal secret substrings passed via ``secrets`` are scrubbed with
:func:`admin_auth.redact`.

No MQTT camera entity (AC-28)
-----------------------------
This module deliberately exposes no ``camera`` component. ONVIF remains the
media interface; MQTT carries only the controls and diagnostics device named
``<camera name> Controls``.
"""
import re

import admin_auth
import identity
import mqtt_topics

#: Schema version published in the state document (source §6.3).
STATE_SCHEMA_VERSION = 1

#: Internal protobuf quality tier -> stable published name (source §3.3/§6.3).
QUALITY_NAMES = {1: 'SD', 2: 'HD', 3: 'FHD'}
_QUALITY_BY_NAME = {name: tier for tier, name in QUALITY_NAMES.items()}
DEFAULT_QUALITY_TIER = 3

#: The exact, ordered §6.3 state keys.
STATE_KEYS = (
    'schema_version',
    'quality',
    'snapshot_upload',
    'snapshot_interval',
    'timelapse_enabled',
    'timelapse_interval',
    'timelapse_fps',
    'prusa_rtsp',
    'webrtc',
    'prusa_connected',
    'camera_source',
    'storage_free_bytes',
    'wifi_rssi_dbm',
    'cpu_temperature_c',
    'uptime_seconds',
    'application_version',
    'last_command_error',
)

#: Recognized ``camera_source`` values; anything else is reported as ``unknown``.
CAMERA_SOURCES = ('running', 'stopped', 'error', 'unknown')
_UNKNOWN_SOURCE = 'unknown'

#: Defaults used only when neither an override nor a state object supplies a
#: value. They mirror :class:`state.CameraState` so a bare builder call is
#: consistent with the live runtime defaults.
_DEFAULT_STATE = {
    'quality': DEFAULT_QUALITY_TIER,
    'snapshot_upload': True,
    'snapshot_interval': 10,
    'timelapse_enabled': False,
    'timelapse_interval': 10,
    'timelapse_fps': 10,
    'prusa_rtsp': False,
    'webrtc': True,
}

_APP_VERSION_MAX = 64
_ERROR_MAX = 200
_NAME_MAX = 128
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]')
_TRACEBACK_RE = re.compile(r'(?i)\btraceback\b|file\s+"|line\s+\d+|exception|stack trace')
_PATH_RE = re.compile(r'(?:(?:[A-Za-z]:)?[\\/][^\s,;]+)')

# --------------------------------------------------------------------------- #
# Discovery constants (source §6.4)
# --------------------------------------------------------------------------- #

ORIGIN_NAME = 'prusa-buddy3d-camera'
MANUFACTURER = 'Prusa Community'
MODEL = 'Buddy3D Raspberry Pi Camera'
DEVICE_NAME_SUFFIX = ' Controls'
DEFAULT_CAMERA_NAME = 'Buddy3D Camera'

#: The 18 component unique-id suffixes in §6.4 table order.
COMPONENT_KEYS = (
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
    'update',
    'prusa_connected',
    'camera_source',
    'storage_free',
    'wifi_rssi',
    'cpu_temperature',
    'uptime',
    'app_version',
)


# --------------------------------------------------------------------------- #
# Small coercion helpers
# --------------------------------------------------------------------------- #

def _text(value, limit):
    """Return a bounded, control-character-free, whitespace-collapsed string."""
    if not isinstance(value, str):
        return ''
    cleaned = _CONTROL_RE.sub(' ', value)
    cleaned = ' '.join(cleaned.split())
    return cleaned[:limit]


def _as_int(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_float(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _attr(source, name, default=None):
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _pick(source, attr, override, default):
    if override is not None:
        return override
    value = _attr(source, attr, None)
    return default if value is None else value


def _int_or(value, default):
    coerced = _as_int(value)
    return default if coerced is None else coerced


def _resolve_bool(source, override, *, bool_attr, mode_attr=None, mode_true=2, default=False):
    if override is not None:
        return bool(override)
    value = _attr(source, bool_attr, None)
    if isinstance(value, bool):
        return value
    if mode_attr is not None:
        mode = _attr(source, mode_attr, None)
        if isinstance(mode, int) and not isinstance(mode, bool):
            return mode == mode_true
    return default


# --------------------------------------------------------------------------- #
# State document (AC-25)
# --------------------------------------------------------------------------- #

def quality_name(tier):
    """Map an internal quality tier to ``'SD'``/``'HD'``/``'FHD'``.

    Accepts the protobuf enum ``1``/``2``/``3`` (reused from
    :mod:`state`/:mod:`quality` ``RESOLUTIONS``) or the already-published name
    (case-insensitive). Raises :class:`ValueError` for anything else; ``bool``
    is rejected explicitly because it is an ``int`` subclass.
    """
    if isinstance(tier, bool):
        raise ValueError(f'invalid quality tier: {tier!r}')
    if isinstance(tier, int):
        try:
            return QUALITY_NAMES[tier]
        except KeyError:
            raise ValueError(f'invalid quality tier: {tier!r}') from None
    if isinstance(tier, str):
        name = tier.strip().upper()
        if name in _QUALITY_BY_NAME:
            return name
    raise ValueError(f'invalid quality tier: {tier!r}')


def _safe_quality(tier):
    try:
        return quality_name(tier)
    except ValueError:
        return QUALITY_NAMES[DEFAULT_QUALITY_TIER]


def sanitize_command_error(value, secrets=()):
    """Return a bounded, non-secret ``last_command_error`` string or ``None``.

    Control characters are removed and the text is length-bounded. A
    traceback-like value collapses to the generic ``'command failed'`` and
    path-like tokens are replaced with ``<path>`` so a username-bearing local
    path or raw exception trace can never be published. Literal ``secrets``
    substrings are scrubbed last.
    """
    if not isinstance(value, str):
        return None
    text = _text(value, _ERROR_MAX)
    if not text:
        return None
    if _TRACEBACK_RE.search(text):
        text = 'command failed'
    else:
        text = _PATH_RE.sub('<path>', text)
    if secrets:
        text = admin_auth.redact(text, secrets)
    text = _text(text, _ERROR_MAX)
    return text or None


def sanitize_camera_source(value, secrets=()):
    """Return a recognized ``camera_source`` value, else ``'unknown'``."""
    if isinstance(value, str):
        text = _text(value, 32).lower()
        if text in CAMERA_SOURCES:
            if secrets:
                text = admin_auth.redact(text, secrets)
            return text
    return _UNKNOWN_SOURCE


def build_state(state=None, *, quality=None, snapshot_upload=None,
                snapshot_interval=None, timelapse_enabled=None,
                timelapse_interval=None, timelapse_fps=None, prusa_rtsp=None,
                webrtc=None, prusa_connected=None, camera_source=None,
                storage_free_bytes=None, wifi_rssi_dbm=None,
                cpu_temperature_c=None, uptime_seconds=None,
                application_version='', last_command_error=None, secrets=()):
    """Build the exact §6.3 state document with native JSON values.

    ``state`` is an optional :class:`state.CameraState` (or any object/mapping
    with the same attribute names). Explicit keyword arguments win over the
    object. Only the documented attributes are read, so unrelated keys — in
    particular secrets — are never copied. Runtime metrics that are not supplied
    become ``None`` (JSON ``null``). No hardware, file, or network access.
    """
    quality_value = _safe_quality(
        _pick(state, 'quality', quality, _DEFAULT_STATE['quality']))

    snapshot_upload_value = _resolve_bool(
        state, snapshot_upload, bool_attr='snapshot_upload_enabled',
        default=_DEFAULT_STATE['snapshot_upload'])
    timelapse_enabled_value = _resolve_bool(
        state, timelapse_enabled, bool_attr='timelapse_enabled',
        default=_DEFAULT_STATE['timelapse_enabled'])
    prusa_rtsp_value = _resolve_bool(
        state, prusa_rtsp, bool_attr='prusa_rtsp', mode_attr='rtsp_mode',
        mode_true=2, default=_DEFAULT_STATE['prusa_rtsp'])
    webrtc_value = _resolve_bool(
        state, webrtc, bool_attr='webrtc', mode_attr='webrtc_mode',
        mode_true=1, default=_DEFAULT_STATE['webrtc'])
    prusa_connected_value = bool(prusa_connected) if prusa_connected is not None \
        else bool(_attr(state, 'prusa_connected', False))

    snapshot_interval_value = _int_or(
        _pick(state, 'snapshot_interval', snapshot_interval,
              _DEFAULT_STATE['snapshot_interval']),
        _DEFAULT_STATE['snapshot_interval'])
    timelapse_interval_value = _int_or(
        _pick(state, 'timelapse_interval', timelapse_interval,
              _DEFAULT_STATE['timelapse_interval']),
        _DEFAULT_STATE['timelapse_interval'])
    timelapse_fps_value = _int_or(
        _pick(state, 'timelapse_fps', timelapse_fps, _DEFAULT_STATE['timelapse_fps']),
        _DEFAULT_STATE['timelapse_fps'])

    version_value = _text(
        _pick(state, 'application_version', application_version, ''), _APP_VERSION_MAX)
    if secrets and version_value:
        version_value = admin_auth.redact(version_value, secrets)

    return {
        'schema_version': STATE_SCHEMA_VERSION,
        'quality': quality_value,
        'snapshot_upload': snapshot_upload_value,
        'snapshot_interval': snapshot_interval_value,
        'timelapse_enabled': timelapse_enabled_value,
        'timelapse_interval': timelapse_interval_value,
        'timelapse_fps': timelapse_fps_value,
        'prusa_rtsp': prusa_rtsp_value,
        'webrtc': webrtc_value,
        'prusa_connected': prusa_connected_value,
        'camera_source': sanitize_camera_source(
            _pick(state, 'camera_source', camera_source, _UNKNOWN_SOURCE), secrets),
        'storage_free_bytes': _as_int(
            _pick(state, 'storage_free_bytes', storage_free_bytes, None)),
        'wifi_rssi_dbm': _as_int(
            _pick(state, 'wifi_rssi_dbm', wifi_rssi_dbm, None)),
        'cpu_temperature_c': _as_float(
            _pick(state, 'cpu_temperature_c', cpu_temperature_c, None)),
        'uptime_seconds': _as_int(
            _pick(state, 'uptime_seconds', uptime_seconds, None)),
        'application_version': version_value,
        'last_command_error': sanitize_command_error(
            _pick(state, 'last_command_error', last_command_error, None), secrets),
    }


# --------------------------------------------------------------------------- #
# Home Assistant device discovery (AC-26)
# --------------------------------------------------------------------------- #

def component_unique_id(device_id_value, key):
    """Stable HA unique id for a component: device identity + component key.

    Derived from the persisted device id only, never the camera name, so a
    rename cannot orphan or duplicate an entity. Also used as the ``cmps``
    mapping key, per HA device-discovery best practice.
    """
    mqtt_topics.validate_device_id(device_id_value)
    if key not in COMPONENT_KEYS:
        raise ValueError(f'unknown component key: {key!r}')
    return f'{device_id_value}_{key}'


def build_discovery(device_id_value, camera_name, application_version='', *,
                    base_prefix=mqtt_topics.DEFAULT_BASE_PREFIX,
                    discovery_prefix=mqtt_topics.DEFAULT_DISCOVERY_PREFIX,
                    serial=None, mac=None, origin_name=ORIGIN_NAME,
                    origin_url=''):
    """Build the single retained HA MQTT device discovery document (source §6.4).

    Returns a dict with the required ``o`` (origin), ``dev`` (device),
    ``avty_t`` (availability) and ``cmps`` (component) members. Every component
    carries a stable ``unique_id`` derived from ``device_id_value`` only, so a
    camera rename does not create duplicate HA entities. No ``camera`` component
    is emitted: ONVIF stays the media interface (AC-28).

    ``mac``, when supplied, must be a valid WLAN MAC and is normalized to the
    ``AA:BB:CC:DD:EE:FF`` form for the device-registry connection.
    """
    mqtt_topics.validate_device_id(device_id_value)
    name = _text(camera_name, _NAME_MAX) or DEFAULT_CAMERA_NAME
    version = _text(application_version, _APP_VERSION_MAX)

    availability_topic = mqtt_topics.availability(device_id_value, base_prefix)
    state_topic = mqtt_topics.state(device_id_value, base_prefix)

    device = {
        'ids': device_id_value,
        'name': f'{name}{DEVICE_NAME_SUFFIX}',
        'mf': MANUFACTURER,
        'mdl': MODEL,
        'sw': version,
        'sn': _text(serial, _NAME_MAX) or device_id_value,
    }
    if mac:
        device['cns'] = [['mac', identity.normalize_wifi_mac(mac)]]

    origin = {'name': origin_name, 'sw': version}
    if origin_url:
        origin['url'] = origin_url

    def uid(key):
        return component_unique_id(device_id_value, key)

    def switch(key, label, command_topic):
        return {
            'p': 'switch',
            'name': label,
            'unique_id': uid(key),
            'command_topic': command_topic,
            'state_topic': state_topic,
            'value_template': f'{{{{ value_json.{key} | lower }}}}',
            'state_on': 'true',
            'state_off': 'false',
            'payload_on': 'true',
            'payload_off': 'false',
        }

    def number(key, label, command_topic, minimum, maximum, step, unit):
        component = {
            'p': 'number',
            'name': label,
            'unique_id': uid(key),
            'command_topic': command_topic,
            'state_topic': state_topic,
            'value_template': f'{{{{ value_json.{key} }}}}',
            'min': minimum,
            'max': maximum,
            'step': step,
            'mode': 'box',
        }
        if unit:
            component['unit_of_measurement'] = unit
        return component

    def button(key, label, command_topic):
        return {
            'p': 'button',
            'name': label,
            'unique_id': uid(key),
            'command_topic': command_topic,
            'payload_press': 'press',
        }

    def binary_sensor(key, label, device_class, template):
        component = {
            'p': 'binary_sensor',
            'name': label,
            'unique_id': uid(key),
            'state_topic': state_topic,
            'value_template': template,
        }
        if device_class:
            component['device_class'] = device_class
        return component

    def sensor(key, label, value_template, *, device_class=None, unit=None,
               state_class=None, diagnostic=True, enabled_by_default=True):
        component = {
            'p': 'sensor',
            'name': label,
            'unique_id': uid(key),
            'state_topic': state_topic,
            'value_template': value_template,
        }
        if device_class:
            component['device_class'] = device_class
        if unit:
            component['unit_of_measurement'] = unit
        if state_class:
            component['state_class'] = state_class
        if diagnostic:
            component['entity_category'] = 'diagnostic'
        if not enabled_by_default:
            component['enabled_by_default'] = False
        return component

    keyed_components = {
        'quality': {
            'p': 'select',
            'name': 'Quality',
            'unique_id': uid('quality'),
            'command_topic': mqtt_topics.quality(device_id_value, base_prefix),
            'state_topic': state_topic,
            'value_template': '{{ value_json.quality }}',
            'options': ['SD', 'HD', 'FHD'],
        },
        'snapshot_upload': switch(
            'snapshot_upload', 'Snapshot uploads',
            mqtt_topics.snapshot_upload(device_id_value, base_prefix)),
        'snapshot_interval': number(
            'snapshot_interval', 'Snapshot interval',
            mqtt_topics.snapshot_interval(device_id_value, base_prefix),
            10, 600, 1, 's'),
        'timelapse_enabled': switch(
            'timelapse_enabled', 'Timelapse',
            mqtt_topics.timelapse_enabled(device_id_value, base_prefix)),
        'timelapse_interval': number(
            'timelapse_interval', 'Timelapse interval',
            mqtt_topics.timelapse_interval(device_id_value, base_prefix),
            1, 3600, 1, 's'),
        'timelapse_fps': number(
            'timelapse_fps', 'Timelapse FPS',
            mqtt_topics.timelapse_fps(device_id_value, base_prefix),
            1, 30, 1, 'fps'),
        'timelapse_build': button(
            'timelapse_build', 'Build timelapse',
            mqtt_topics.timelapse_build(device_id_value, base_prefix)),
        'prusa_rtsp': switch(
            'prusa_rtsp', 'Prusa RTSP mode',
            mqtt_topics.prusa_rtsp(device_id_value, base_prefix)),
        'webrtc': switch(
            'webrtc', 'Prusa WebRTC mode',
            mqtt_topics.webrtc(device_id_value, base_prefix)),
        'restart': button(
            'restart', 'Restart',
            mqtt_topics.restart(device_id_value, base_prefix)),
        'update': {
            'p': 'update',
            'name': 'Application update',
            'unique_id': uid('update'),
            'state_topic': mqtt_topics.update_state(device_id_value, base_prefix),
            'command_topic': mqtt_topics.update_install(device_id_value, base_prefix),
            'payload_install': 'install',
            'device_class': 'firmware',
            'value_template': '{{ value_json.installed_version }}',
            'latest_version_topic': mqtt_topics.update_state(device_id_value, base_prefix),
            'latest_version_template': '{{ value_json.latest_version }}',
        },
        'prusa_connected': binary_sensor(
            'prusa_connected', 'Prusa connection', 'connectivity',
            "{{ 'ON' if value_json.prusa_connected else 'OFF' }}"),
        'camera_source': binary_sensor(
            'camera_source', 'Camera source', 'running',
            "{{ 'ON' if value_json.camera_source == 'running' else 'OFF' }}"),
        'storage_free': sensor(
            'storage_free', 'Storage free',
            '{{ value_json.storage_free_bytes }}',
            device_class='data_size', unit='B', state_class='measurement'),
        'wifi_rssi': sensor(
            'wifi_rssi', 'Wi-Fi RSSI',
            '{{ value_json.wifi_rssi_dbm }}',
            device_class='signal_strength', unit='dBm', state_class='measurement',
            enabled_by_default=False),
        'cpu_temperature': sensor(
            'cpu_temperature', 'CPU temperature',
            '{{ value_json.cpu_temperature_c }}',
            device_class='temperature', unit='°C', state_class='measurement',
            enabled_by_default=False),
        'uptime': sensor(
            'uptime', 'Uptime',
            '{{ value_json.uptime_seconds }}',
            device_class='duration', unit='s', state_class='total_increasing',
            enabled_by_default=False),
        'app_version': sensor(
            'app_version', 'App version',
            '{{ value_json.application_version }}',
            enabled_by_default=False),
    }

    # HA device-discovery best practice: key ``cmps`` by the stable unique id so
    # the discovery identity is rename-proof and equals the entity unique id.
    components = {
        uid(key): component for key, component in keyed_components.items()
    }

    return {
        'dev': device,
        'o': origin,
        'avty_t': availability_topic,
        'cmps': components,
    }
