"""Stable MQTT topic contract for the public appliance (WP-5a, AC-24).

Source of truth: ``docs/public-appliance-distribution-plan.md`` §6.3. The
appliance publishes and subscribes on one fixed topic tree rooted at the
configurable base prefix (default ``buddy3d``)::

    <base>/<device-id>/availability
    <base>/<device-id>/state
    <base>/<device-id>/command/<name>
    <base>/<device-id>/update/state
    <base>/<device-id>/update/install

Device identity
---------------
``<device-id>`` is derived from the *persisted* appliance UUID/seed, never from
the changeable camera name or the device IP. The seed is hashed through the same
derivation used for the setup SSID and the ONVIF endpoint
(:func:`provisioning.derive_device_id`, ``uuid5(NAMESPACE_URL,
'prusa-camera:<seed>')``) so one appliance keeps one identity across the wizard,
mDNS, ONVIF discovery, and MQTT. The result is projected onto the safe
lowercase ``[a-z0-9-]`` token used in MQTT topics and HA unique IDs.

Prefixes
--------
Both the base prefix and the HA discovery prefix are configurable (source §6.2)
and validated against a conservative charset. ``#``/``+`` wildcards, NUL,
whitespace, empty segments, and leading/trailing separators are rejected so a
user-supplied prefix can never broaden or escape the topic tree.

This module is pure stdlib and has no file/network/broker side effects; it only
builds strings and is safe to import on a host test runner.
"""
import re
import uuid

import provisioning

#: Default root of the appliance's own topic tree (source §6.3).
DEFAULT_BASE_PREFIX = 'buddy3d'

#: Default Home Assistant discovery prefix (source §6.4).
DEFAULT_DISCOVERY_PREFIX = 'homeassistant'

#: Home Assistant's default MQTT birth/LWT topic (source §6.4).
HA_STATUS_TOPIC = 'homeassistant/status'

#: Object-id prefix for the device-discovery topic (source §6.4).
DISCOVERY_OBJECT_PREFIX = 'buddy3d_'

#: The ten ``command/<name>`` leaves of the §6.3 contract, in table order.
#:
#: ``update/state`` and ``update/install`` are separate topics, not commands,
#: so they are intentionally absent here.
COMMAND_NAMES = (
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
)

#: MQTT-safe prefix: one or more ``[A-Za-z0-9_-]`` segments separated by ``/``.
_PREFIX_RE = re.compile(r'^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$')

#: Safe device-id token accepted in topics and HA unique IDs.
_DEVICE_ID_RE = re.compile(r'^[a-z0-9-]+$')

_DEVICE_ID_MAX = 128
_PREFIX_MAX = 128


def validate_prefix(prefix, name='prefix'):
    """Return ``prefix`` if it is a safe MQTT prefix, else raise ``ValueError``.

    A safe prefix is a non-empty ``/``-separated path of ``[A-Za-z0-9_-]``
    segments. Wildcards (``#``, ``+``), whitespace, control characters, empty
    segments, and leading/trailing separators are rejected.
    """
    if not isinstance(prefix, str) or not prefix:
        raise ValueError(f'{name} must be a non-empty string')
    if len(prefix) > _PREFIX_MAX:
        raise ValueError(f'{name} is too long (max {_PREFIX_MAX} characters)')
    if not _PREFIX_RE.match(prefix):
        raise ValueError(f'{name} contains unsafe characters: {prefix!r}')
    return prefix


def sanitize_device_id(value):
    """Project ``value`` onto a stable lowercase ``[a-z0-9-]`` token.

    Runs of disallowed characters collapse to a single ``-``; leading/trailing
    hyphens are stripped. Accepts a ``str`` or :class:`uuid.UUID` and never
    raises (``None`` and non-string scalars become ``''``).
    """
    if isinstance(value, uuid.UUID):
        text = value.hex
    elif value is None:
        text = ''
    else:
        text = str(value)
    text = text.strip().lower()
    text = re.sub(r'[^a-z0-9]+', '-', text)
    text = re.sub(r'-{2,}', '-', text).strip('-')
    return text


def device_id(seed):
    """Return the stable MQTT ``<device-id>`` for a persisted appliance seed.

    ``seed`` is the durable per-device identity: the persisted appliance UUID
    (or the fingerprint/fallback seed already used for the ONVIF endpoint). It
    is hashed through :func:`provisioning.derive_device_id` so the MQTT id
    matches the setup SSID and ONVIF endpoint, then sanitized to ``[a-z0-9-]``.

    Raises :class:`ValueError` for an empty/non-string seed.
    """
    if isinstance(seed, uuid.UUID):
        seed = seed.hex
    if not isinstance(seed, str) or not seed.strip():
        raise ValueError('device seed must be a non-empty string')
    return sanitize_device_id(provisioning.derive_device_id(seed))


def validate_device_id(value, name='device_id'):
    """Return ``value`` if it is a safe ``[a-z0-9-]`` device-id token.

    Builders call this so a caller cannot inject a wildcard or a path separator
    through a hand-supplied device id. Raises :class:`ValueError` otherwise.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f'{name} must be a non-empty string')
    if len(value) > _DEVICE_ID_MAX:
        raise ValueError(f'{name} is too long (max {_DEVICE_ID_MAX} characters)')
    if not _DEVICE_ID_RE.match(value):
        raise ValueError(f'{name} contains unsafe characters: {value!r}')
    return value


def _base(base_prefix):
    return validate_prefix(base_prefix, 'base_prefix')


def _device_path(device_id_value, base_prefix):
    return f'{_base(base_prefix)}/{validate_device_id(device_id_value)}'


def availability(device_id_value, base_prefix=DEFAULT_BASE_PREFIX):
    """``<base>/<device-id>/availability`` (retained ``online`` + LWT ``offline``)."""
    return f'{_device_path(device_id_value, base_prefix)}/availability'


def state(device_id_value, base_prefix=DEFAULT_BASE_PREFIX):
    """``<base>/<device-id>/state`` (retained authoritative state JSON)."""
    return f'{_device_path(device_id_value, base_prefix)}/state'


def command(device_id_value, name, base_prefix=DEFAULT_BASE_PREFIX):
    """``<base>/<device-id>/command/<name>`` (QoS 1, never retained)."""
    if name not in COMMAND_NAMES:
        raise ValueError(f'unknown command name: {name!r}')
    return f'{_device_path(device_id_value, base_prefix)}/command/{name}'


def update_state(device_id_value, base_prefix=DEFAULT_BASE_PREFIX):
    """``<base>/<device-id>/update/state`` (HA update JSON schema)."""
    return f'{_device_path(device_id_value, base_prefix)}/update/state'


def update_install(device_id_value, base_prefix=DEFAULT_BASE_PREFIX):
    """``<base>/<device-id>/update/install`` (non-retained install command)."""
    return f'{_device_path(device_id_value, base_prefix)}/update/install'


def discovery_device_topic(discovery_prefix, device_id_value):
    """``<discovery-prefix>/device/buddy3d_<device-id>/config`` (source §6.4).

    The object id keeps the ``buddy3d_`` prefix so the HA discovery identity is
    stable across camera renames and IP changes.
    """
    validate_prefix(discovery_prefix, 'discovery_prefix')
    validate_device_id(device_id_value)
    return f'{discovery_prefix}/device/{DISCOVERY_OBJECT_PREFIX}{device_id_value}/config'


def command_from_topic(topic, device_id_value, base_prefix=DEFAULT_BASE_PREFIX):
    """Return the command name encoded by ``topic`` or raise ``ValueError``.

    Used by the command handler to reject unknown/foreign topics. The topic must
    be exactly ``<base>/<device-id>/command/<known-name>`` for *this* device.
    """
    prefix = f'{_device_path(device_id_value, base_prefix)}/command/'
    if not isinstance(topic, str) or not topic.startswith(prefix):
        raise ValueError(f'not a command topic for this device: {topic!r}')
    name = topic[len(prefix):]
    if name not in COMMAND_NAMES:
        raise ValueError(f'unknown command topic: {topic!r}')
    return name


# --------------------------------------------------------------------------- #
# Explicit per-command builders (source §6.3, table order)
# --------------------------------------------------------------------------- #

def quality(device_id_value, base_prefix=DEFAULT_BASE_PREFIX):
    return command(device_id_value, 'quality', base_prefix)


def snapshot_upload(device_id_value, base_prefix=DEFAULT_BASE_PREFIX):
    return command(device_id_value, 'snapshot_upload', base_prefix)


def snapshot_interval(device_id_value, base_prefix=DEFAULT_BASE_PREFIX):
    return command(device_id_value, 'snapshot_interval', base_prefix)


def timelapse_enabled(device_id_value, base_prefix=DEFAULT_BASE_PREFIX):
    return command(device_id_value, 'timelapse_enabled', base_prefix)


def timelapse_interval(device_id_value, base_prefix=DEFAULT_BASE_PREFIX):
    return command(device_id_value, 'timelapse_interval', base_prefix)


def timelapse_fps(device_id_value, base_prefix=DEFAULT_BASE_PREFIX):
    return command(device_id_value, 'timelapse_fps', base_prefix)


def timelapse_build(device_id_value, base_prefix=DEFAULT_BASE_PREFIX):
    return command(device_id_value, 'timelapse_build', base_prefix)


def prusa_rtsp(device_id_value, base_prefix=DEFAULT_BASE_PREFIX):
    return command(device_id_value, 'prusa_rtsp', base_prefix)


def webrtc(device_id_value, base_prefix=DEFAULT_BASE_PREFIX):
    return command(device_id_value, 'webrtc', base_prefix)


def restart(device_id_value, base_prefix=DEFAULT_BASE_PREFIX):
    return command(device_id_value, 'restart', base_prefix)
