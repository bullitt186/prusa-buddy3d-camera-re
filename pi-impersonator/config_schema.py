"""Versioned TOML configuration schema for the public appliance (WP-1, AC-3).

The Pi root is a read-only overlayfs, so durable configuration lives on the
``PERSIST`` ext4 partition mounted at ``/data``:

    /data/prusa-cam/config/device.toml    non-secret appliance configuration
    /data/prusa-cam/config/secrets.toml   Prusa/MQTT/admin secrets (mode 0600)

This module owns the schema, its strict allowlist, validation, migration
harness, and a bounded deterministic TOML writer. There is no TOML writer in
the standard library, so :func:`dumps_device`/:func:`dumps_secrets` implement
just enough of TOML for this documented schema (scalars, bools, and the fixed
nested tables). They are not a general TOML library.

Security
--------
The device document has a strict allowlist. Any unknown key or table -- in
particular a security-sensitive one such as ``prusa.token`` misplaced into the
non-secret file -- raises :class:`UnknownKeyError`. Secret values are never
included in exception messages. ``[meta]`` is an explicitly allowed table for
forward-compatible string metadata.

Schema versioning
-----------------
``schema_version`` greater than :data:`SCHEMA_VERSION` raises
:class:`SchemaTooNewError` and callers must leave the original file untouched.
A missing ``schema_version`` is treated as ``1`` so hand-written files keep
working. Migrations are pure functions; the version is never silently lowered.

Stdlib only, and no file/network/thread side effects on import.
"""
import logging
import os
import re
import tomllib
from urllib.parse import urlsplit

log = logging.getLogger('prusa-cam.config')

SCHEMA_VERSION = 1

CONFIG_DIR = '/data/prusa-cam/config'
DEVICE_TOML_PATH = CONFIG_DIR + '/device.toml'
SECRETS_TOML_PATH = CONFIG_DIR + '/secrets.toml'

# Documented device schema. Scalars are top-level keys; tables map to their
# allowed child keys. ``meta`` is handled separately because it is open-ended.
_DEVICE_SCALARS = ('schema_version', 'camera_name', 'fingerprint')
_DEVICE_TABLES = {
    'prusa': ('server',),
    'mqtt': ('enabled', 'uri', 'client_id', 'discovery_prefix', 'topic_prefix', 'ca_file'),
    'admin': ('hostname',),
}
_META_TABLE = 'meta'

# Documented secrets schema. ``schema_version`` is accepted as version metadata
# only; it is never returned as part of the normalized secrets dict.
_SECRET_TABLES = {
    'prusa': ('token',),
    'mqtt': ('username', 'password'),
    'wifi': ('psk',),
    'admin': ('password_hash',),
}
_SECRET_ORDER = (
    ('prusa', ('token',)),
    ('mqtt', ('username', 'password')),
    ('wifi', ('psk',)),
    ('admin', ('password_hash',)),
)

_PREFIX_RE = re.compile(r'[A-Za-z0-9_/-]+')
_BARE_KEY_RE = re.compile(r'[A-Za-z0-9_-]+')

# from_version -> callable(cfg) -> cfg. Empty while v1 is the only schema, but
# the harness is exercised by a test that injects a synthetic migration.
MIGRATIONS = {}


class ConfigError(Exception):
    """Base class for configuration schema/validation failures."""


class SchemaTooNewError(ConfigError):
    """The document declares a schema newer than this build supports."""


class UnknownKeyError(ConfigError):
    """The document contains a key outside the strict allowlist."""


class ValidationError(ConfigError):
    """A known key holds a wrong type or an invalid value."""


def default_device():
    """Return a fresh, fully-populated default device document."""
    return {
        'schema_version': SCHEMA_VERSION,
        'camera_name': 'Printer Camera',
        'fingerprint': '',
        'prusa': {'server': 'webcam.connect.prusa3d.com'},
        'mqtt': {
            'enabled': False,
            'uri': 'mqtts://broker.example:8883',
            'client_id': '',
            'discovery_prefix': 'homeassistant',
            'topic_prefix': 'buddy3d',
            'ca_file': '',
        },
        'admin': {'hostname': ''},
    }


# --------------------------------------------------------------------------- #
# Parsing / validation
# --------------------------------------------------------------------------- #

def parse_device(text):
    """Parse and validate a device TOML document into a normalized dict.

    Unknown keys/tables raise :class:`UnknownKeyError`; wrong types and invalid
    values raise :class:`ValidationError`; a newer schema raises
    :class:`SchemaTooNewError`. The returned dict contains every documented key
    with defaults filled in.
    """
    data = _loads(text)
    return _validate_device(data)


def parse_secrets(text):
    """Parse and validate a secrets TOML document into a normalized dict.

    Only the documented secret keys are returned; ``schema_version`` is checked
    for forward compatibility but is not a secret and is dropped from the result.
    """
    return _validate_secrets(_loads(text))


def _validate_secrets(data):
    """Validate a secrets mapping (from TOML or an in-memory dict).

    Shared by :func:`parse_secrets` and :func:`save_secrets` so the allowlist is
    enforced identically on both paths.
    """
    if not isinstance(data, dict):
        raise ValidationError('secrets configuration must be a table')
    if 'schema_version' in data:
        version = data['schema_version']
        if type(version) is not int:
            raise ValidationError('schema_version must be an integer')
        if version > SCHEMA_VERSION:
            raise SchemaTooNewError(
                f'schema_version {version} is newer than supported schema {SCHEMA_VERSION}'
            )
    _check_secrets_keys(data)
    result = {}
    for table, allowed in _SECRET_TABLES.items():
        if table not in data:
            continue
        section = data[table]
        kept = {}
        for sub in allowed:
            if sub not in section:
                continue
            if not isinstance(section[sub], str):
                raise ValidationError(f'{table}.{sub} must be a string')
            kept[sub] = section[sub]
        if kept:
            result[table] = kept
    return result


def _check_secrets_keys(data):
    """Enforce the secrets allowlist, naming the offending key path.

    The single shared key check used by both the parse and save paths so they
    cannot drift.
    """
    for key in data:
        if key == 'schema_version':
            continue
        if key not in _SECRET_TABLES:
            raise UnknownKeyError(f"unknown secrets key '{key}'")
        section = data[key]
        if not isinstance(section, dict):
            raise ValidationError(f'{key} must be a table')
        for sub in section:
            if sub not in _SECRET_TABLES[key]:
                raise UnknownKeyError(f"unknown secrets key '{key}.{sub}'")


def _loads(text):
    if not isinstance(text, str):
        raise ValidationError('configuration document must be text')
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValidationError(f'invalid TOML: {e}')


def _validate_device(data):
    """Validate a device mapping (from TOML or an in-memory dict).

    Shared by :func:`parse_device` and :func:`save_device` so the allowlist is
    enforced identically on both paths.
    """
    if not isinstance(data, dict):
        raise ValidationError('device configuration must be a table')
    _check_device_keys(data)

    version = 1
    if 'schema_version' in data:
        raw = data['schema_version']
        if type(raw) is not int:
            raise ValidationError('schema_version must be an integer')
        if raw > SCHEMA_VERSION:
            raise SchemaTooNewError(
                f'schema_version {raw} is newer than supported schema {SCHEMA_VERSION}'
            )
        version = raw

    cfg = default_device()
    cfg['schema_version'] = version

    if 'camera_name' in data:
        cfg['camera_name'] = _validate_camera_name(data['camera_name'])
    if 'fingerprint' in data:
        cfg['fingerprint'] = _expect_str(data['fingerprint'], 'fingerprint')

    for table in _DEVICE_TABLES:
        if table in data:
            _apply_device_table(cfg, table, data[table])

    if _META_TABLE in data:
        meta = data[_META_TABLE]
        for key, value in meta.items():
            if not isinstance(value, str):
                raise ValidationError(f"meta value for '{key}' must be a string")
        cfg[_META_TABLE] = dict(meta)

    return cfg


def _check_device_keys(data):
    """Enforce the device allowlist, naming the offending key path.

    The single shared key check used by both the parse and save paths so they
    cannot drift. Unknown top-level keys, unknown table subkeys, and non-table
    values for a documented table are all rejected here.
    """
    for key in data:
        if key not in _DEVICE_SCALARS and key not in _DEVICE_TABLES and key != _META_TABLE:
            raise UnknownKeyError(f"unknown device key '{key}'")
        if key in _DEVICE_TABLES:
            section = data[key]
            if not isinstance(section, dict):
                raise ValidationError(f'{key} must be a table')
            for sub in section:
                if sub not in _DEVICE_TABLES[key]:
                    raise UnknownKeyError(f"unknown device key '{key}.{sub}'")
        elif key == _META_TABLE and not isinstance(data[key], dict):
            raise ValidationError('meta must be a table')


def _apply_device_table(cfg, table, section):
    if table == 'prusa':
        if 'server' in section:
            cfg['prusa']['server'] = _expect_str(section['server'], 'prusa.server')
    elif table == 'mqtt':
        mqtt = cfg['mqtt']
        if 'enabled' in section:
            if type(section['enabled']) is not bool:
                raise ValidationError('mqtt.enabled must be a boolean')
            mqtt['enabled'] = section['enabled']
        if 'uri' in section:
            mqtt['uri'] = _validate_uri(section['uri'])
        for key in ('client_id', 'ca_file'):
            if key in section:
                mqtt[key] = _expect_str(section[key], f'mqtt.{key}')
        for key in ('discovery_prefix', 'topic_prefix'):
            if key in section:
                mqtt[key] = _validate_prefix(section[key], f'mqtt.{key}')
    elif table == 'admin':
        if 'hostname' in section:
            cfg['admin']['hostname'] = _expect_str(section['hostname'], 'admin.hostname')


def _expect_str(value, path):
    if not isinstance(value, str):
        raise ValidationError(f'{path} must be a string')
    return value


def _validate_camera_name(value):
    name = _expect_str(value, 'camera_name').strip()
    if not name:
        raise ValidationError('camera_name must not be empty')
    if len(name) > 64:
        raise ValidationError('camera_name must be at most 64 characters')
    return name


def _validate_uri(value):
    value = _expect_str(value, 'mqtt.uri')
    parts = urlsplit(value)
    if parts.scheme not in ('mqtt', 'mqtts') or not parts.netloc:
        raise ValidationError("mqtt.uri must use the 'mqtt' or 'mqtts' scheme")
    if parts.username is not None or parts.password is not None:
        raise ValidationError(
            'mqtt.uri must not embed credentials; store the MQTT username/password '
            'in the secrets document'
        )
    try:
        port = parts.port
    except ValueError:
        raise ValidationError('mqtt.uri port must be between 1 and 65535')
    if port is not None and not 1 <= port <= 65535:
        raise ValidationError('mqtt.uri port must be between 1 and 65535')
    return value


def _validate_prefix(value, path):
    value = _expect_str(value, path)
    if not value or not _PREFIX_RE.fullmatch(value):
        raise ValidationError(f'{path} must be non-empty and match [A-Za-z0-9_/-]+')
    return value


# --------------------------------------------------------------------------- #
# Migrations
# --------------------------------------------------------------------------- #

def migrate_device(cfg, from_version):
    """Return ``cfg`` migrated from ``from_version`` up to :data:`SCHEMA_VERSION`.

    Pure: the input is not mutated. Applies consecutive registered migrations.
    Raises :class:`SchemaTooNewError` when ``from_version`` is newer than this
    build supports and :class:`ConfigError` when a migration is missing.
    """
    if from_version > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f'schema_version {from_version} is newer than supported schema {SCHEMA_VERSION}'
        )
    current = dict(cfg)
    version = from_version
    while version < SCHEMA_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise ConfigError(f'no migration registered from schema version {version}')
        current = migration(current)
        version += 1
    current = dict(current)
    current['schema_version'] = SCHEMA_VERSION
    return current


# --------------------------------------------------------------------------- #
# Bounded deterministic writers
# --------------------------------------------------------------------------- #

def dumps_device(cfg):
    """Serialize a device document as TOML for exactly the documented schema."""
    merged = _merge_device_defaults(cfg)
    version = merged['schema_version']
    if type(version) is not int:
        raise ValidationError('schema_version must be an integer')
    lines = [
        f'schema_version = {version}',
        f'camera_name = {_toml_str(merged["camera_name"])}',
        f'fingerprint = {_toml_str(merged["fingerprint"])}',
        '',
        '[prusa]',
        f'server = {_toml_str(merged["prusa"]["server"])}',
        '',
        '[mqtt]',
        f'enabled = {_toml_bool(merged["mqtt"]["enabled"])}',
        f'uri = {_toml_str(merged["mqtt"]["uri"])}',
        f'client_id = {_toml_str(merged["mqtt"]["client_id"])}',
        f'discovery_prefix = {_toml_str(merged["mqtt"]["discovery_prefix"])}',
        f'topic_prefix = {_toml_str(merged["mqtt"]["topic_prefix"])}',
        f'ca_file = {_toml_str(merged["mqtt"]["ca_file"])}',
        '',
        '[admin]',
        f'hostname = {_toml_str(merged["admin"]["hostname"])}',
    ]
    if _META_TABLE in merged:
        lines.append('')
        lines.append('[meta]')
        for key in sorted(merged[_META_TABLE]):
            lines.append(f'{_toml_key(key)} = {_toml_str(merged[_META_TABLE][key])}')
    return '\n'.join(lines) + '\n'


def dumps_secrets(cfg):
    """Serialize a secrets document as TOML for exactly the documented schema."""
    blocks = []
    for table, keys in _SECRET_ORDER:
        section = cfg.get(table)
        if not isinstance(section, dict):
            continue
        present = [key for key in keys if key in section]
        if not present:
            continue
        lines = [f'[{table}]']
        for key in present:
            lines.append(f'{key} = {_toml_str(_expect_str(section[key], f"{table}.{key}"))}')
        blocks.append('\n'.join(lines))
    if not blocks:
        return ''
    return '\n\n'.join(blocks) + '\n'


def _merge_device_defaults(cfg):
    merged = default_device()
    if not isinstance(cfg, dict):
        return merged
    if 'schema_version' in cfg:
        merged['schema_version'] = cfg['schema_version']
    for key in ('camera_name', 'fingerprint'):
        if key in cfg:
            merged[key] = cfg[key]
    for table, keys in _DEVICE_TABLES.items():
        section = cfg.get(table)
        if not isinstance(section, dict):
            continue
        for key in keys:
            if key in section:
                merged[table][key] = section[key]
    meta = cfg.get(_META_TABLE)
    if isinstance(meta, dict):
        merged[_META_TABLE] = dict(meta)
    return merged


def _toml_bool(value):
    if type(value) is not bool:
        raise ValidationError('configuration boolean must be a boolean')
    return 'true' if value else 'false'


def _toml_key(key):
    if _BARE_KEY_RE.fullmatch(key):
        return key
    return _toml_str(key)


def _toml_str(value):
    if not isinstance(value, str):
        raise ValidationError('configuration value must be a string')
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == '\\':
            out.append('\\\\')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif code < 0x20 or code == 0x7F:
            out.append('\\u%04x' % code)
        else:
            out.append(ch)
    out.append('"')
    return ''.join(out)


# --------------------------------------------------------------------------- #
# Atomic persistence
# --------------------------------------------------------------------------- #

def write_atomic(path, text, mode=0o600):
    """Atomically write ``text`` to ``path`` with ``mode``.

    The temp file is created with ``mode`` (before any content is written) so a
    secret is never briefly world/group-readable. Content is written as UTF-8
    bytes, flushed and fsynced, renamed over ``path``, then the directory is
    best-effort fsynced. On ``OSError`` or an encoding failure the temp file is
    removed and :class:`ConfigError` is raised.
    """
    directory = os.path.dirname(path) or '.'
    tmp = path + '.tmp'
    try:
        os.makedirs(directory, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            os.chmod(tmp, mode)  # undo any umask masking before writing content
            with os.fdopen(fd, 'wb') as f:
                fd = -1
                f.write(text.encode('utf-8'))
                f.flush()
                os.fsync(f.fileno())
        finally:
            if fd >= 0:
                os.close(fd)
        os.replace(tmp, path)
        _fsync_dir(directory)
    except (OSError, UnicodeError) as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ConfigError(f'could not write {path}: {e}')


def save_device(cfg, path=DEVICE_TOML_PATH, mode=0o640):
    """Validate ``cfg`` and atomically write it; True on success, False on I/O failure.

    The *input* mapping is checked against the device allowlist (via the same
    helper used by :func:`parse_device`) before anything is serialized, so an
    unknown or security-sensitive key raises :class:`UnknownKeyError` instead of
    being silently dropped. Other validation failures raise :class:`ConfigError`.
    """
    validated = _validate_device(cfg)
    text = dumps_device(validated)
    parse_device(text)
    try:
        write_atomic(path, text, mode=mode)
    except ConfigError as e:
        log.warning(f'config: device not saved: {e}')
        return False
    return True


def save_secrets(cfg, path=SECRETS_TOML_PATH, mode=0o600):
    """Validate ``cfg`` and atomically write it; True on success, False on I/O failure.

    The *input* mapping is checked against the secrets allowlist before anything
    is serialized, so an unknown key raises :class:`UnknownKeyError`.
    """
    validated = _validate_secrets(cfg)
    text = dumps_secrets(validated)
    parse_secrets(text)
    try:
        write_atomic(path, text, mode=mode)
    except ConfigError as e:
        log.warning(f'config: secrets not saved: {e}')
        return False
    return True


def load_device(path=DEVICE_TOML_PATH):
    """Load a device document; a missing file returns :func:`default_device`.

    Invalid or too-new files propagate :class:`ConfigError` so the caller can
    enter recovery with the original file untouched.
    """
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except FileNotFoundError:
        return default_device()
    return parse_device(text)


def load_secrets(path=SECRETS_TOML_PATH):
    """Load a secrets document; a missing file returns an empty dict."""
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except FileNotFoundError:
        return {}
    return parse_secrets(text)


def _fsync_dir(directory):
    """Best-effort fsync of ``directory`` so a rename survives a power cut."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
