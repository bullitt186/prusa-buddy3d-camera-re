"""Idempotent importer for pre-appliance installations (WP-1, AC-6).

Migrates the legacy, scattered configuration into the versioned TOML documents
owned by :mod:`config_schema`:

    pi-impersonator/config.ini            identity token/fingerprint, upload server/interval
    /data/prusa-cam/state.json            persisted runtime settings (settings_store)
    /etc/prusa-cam/quality.env            video-quality tier when state.json lacks it
    /etc/prusa-cam/rtsp.mode              configured RTSP mode when state.json lacks it
    /data/sdcard/timelapse                timelapse frames (reported only, never moved)

Safety properties:

* **Validate before activation** -- the converted device and secrets documents
  are validated with :mod:`config_schema` before any file is created. Invalid
  input returns ``{'status': 'rejected', ...}`` with no filesystem changes.
* **Idempotent** -- a completion record under ``backups_root`` recording the
  current target schema short-circuits to ``{'status': 'noop', ...}``.
* **Preserving** -- source files are copied (never deleted) into a dated
  ``migration-<YYYYMMDD-HHMMSS>`` directory, unknown ``state.json`` keys are
  kept, and the completion record never contains a secret value.
* **No-op safe** -- ``settings_store.save`` is inert when ``/data`` is not a
  mountpoint; the importer reports the failure instead of raising.

Stdlib only, and no side effects on import.
"""
import configparser
import json
import logging
import os
import shutil
from datetime import datetime

import config_schema
import quality
import rtsp_control
import settings_store

log = logging.getLogger('prusa-cam.legacy_import')

APP_VERSION = '0.0.0-dev'

SOURCE_SCHEMA = 'legacy-config.ini+state.json'
DEFAULT_BACKUPS_ROOT = '/data/prusa-cam/backups'

# Module-level so tests can redirect them to a temporary directory.
DEVICE_TOML_PATH = config_schema.DEVICE_TOML_PATH
SECRETS_TOML_PATH = config_schema.SECRETS_TOML_PATH
QUALITY_ENV_PATH = quality.QUALITY_ENV
RTSP_MODE_PATH = rtsp_control.RTSP_MODE_FILE
TIMELAPSE_DIRS = ('/data/sdcard/timelapse', '/mnt/sdcard/timelapse')

_INTERVAL_MIN = 10
_INTERVAL_MAX = 600
_COMPLETION_NAME = 'completion.json'


def import_legacy(config_ini_path, settings_path=settings_store.SETTINGS_PATH,
                  backups_root=DEFAULT_BACKUPS_ROOT, app_version=APP_VERSION, now=None):
    """Import a legacy installation into the versioned TOML configuration.

    Returns a result dict whose ``status`` is ``'unavailable'`` (``/data`` is not
    a mountpoint; nothing written), ``'noop'`` (already imported), ``'rejected'``
    (invalid input or a failed write), or ``'imported'``.
    ``now`` is an injectable timestamp for deterministic tests.
    """
    moment = now or datetime.now()

    if not settings_store.available():
        return {
            'status': 'unavailable',
            'backup_dir': None,
            'applied': [],
            'errors': ['/data is not a mountpoint'],
        }

    existing = _find_completion(backups_root)
    if existing is not None:
        return {
            'status': 'noop',
            'backup_dir': existing.get('backup_dir'),
            'applied': [],
            'errors': [],
        }

    parser = _read_ini(config_ini_path)
    settings = _read_state(settings_path)

    device, secrets, applied = _build_documents(parser, settings)

    quality_tier = _derive_quality_tier(settings)
    if quality_tier is not None:
        settings['quality_tier'] = quality_tier
        applied.append('quality_tier')
    rtsp_mode = _derive_rtsp_mode(settings)
    if rtsp_mode is not None:
        settings['rtsp_mode'] = rtsp_mode
        applied.append('rtsp_mode')

    # Validate the complete converted documents before touching the filesystem.
    try:
        device_text = config_schema.dumps_device(device)
        device = config_schema.parse_device(device_text)
        secrets_text = config_schema.dumps_secrets(secrets)
        secrets = config_schema.parse_secrets(secrets_text)
    except config_schema.ConfigError as e:
        log.warning(f'legacy import: rejected: {e}')
        return {'status': 'rejected', 'backup_dir': None, 'applied': [], 'errors': [str(e)]}

    backup_dir = os.path.join(
        backups_root, 'migration-' + moment.strftime('%Y%m%d-%H%M%S')
    )
    try:
        os.makedirs(backup_dir, exist_ok=True)
    except OSError as e:
        return {
            'status': 'rejected',
            'backup_dir': None,
            'applied': [],
            'errors': [f'could not create backup directory: {e}'],
        }

    for source in (config_ini_path, settings_path, QUALITY_ENV_PATH, RTSP_MODE_PATH):
        _copy_if_exists(source, backup_dir)

    errors = []
    if not config_schema.save_device(device, path=DEVICE_TOML_PATH):
        errors.append('could not write device.toml')
    if not config_schema.save_secrets(secrets, path=SECRETS_TOML_PATH):
        errors.append('could not write secrets.toml')
    if not settings_store.save(settings, path=settings_path):
        errors.append('could not write state.json')

    if errors:
        return {
            'status': 'rejected',
            'backup_dir': backup_dir,
            'applied': sorted(applied),
            'errors': errors,
        }

    completion = {
        'source_schema': SOURCE_SCHEMA,
        'target_schema_version': config_schema.SCHEMA_VERSION,
        'timestamp': moment.isoformat(),
        'app_version': app_version,
        'backup_dir': backup_dir,
        'applied': sorted(applied),
        'timelapse': _timelapse_report(),
    }
    try:
        config_schema.write_atomic(
            os.path.join(backup_dir, _COMPLETION_NAME),
            json.dumps(completion, indent=2, sort_keys=True) + '\n',
            mode=0o640,
        )
    except config_schema.ConfigError as e:
        return {
            'status': 'rejected',
            'backup_dir': backup_dir,
            'applied': sorted(applied),
            'errors': [str(e)],
        }

    return {
        'status': 'imported',
        'backup_dir': backup_dir,
        'applied': sorted(applied),
        'errors': [],
    }


# --------------------------------------------------------------------------- #
# Legacy readers
# --------------------------------------------------------------------------- #

def _read_ini(path):
    """Return a parsed ``config.ini``, or None when missing/malformed."""
    if not path:
        return None
    parser = configparser.ConfigParser(interpolation=None)
    try:
        found = parser.read(path)
    except (OSError, configparser.Error) as e:
        log.warning(f'legacy import: could not read {path}: {e}')
        return None
    if not found:
        return None
    return parser


def _ini_value(parser, section, key):
    if parser is None or not parser.has_option(section, key):
        return None
    value = parser.get(section, key).strip()
    return value or None


def _ini_int(parser, section, key):
    value = _ini_value(parser, section, key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _read_state(path):
    """Read ``state.json`` without side effects.

    Unlike :func:`settings_store.load`, a corrupt/unreadable/non-object file is
    treated as empty and is **not** quarantined (renamed), so a rejected import
    leaves the user's file byte-identical. The value is only written back
    through :func:`settings_store.save` after validation succeeds.
    """
    if not path:
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_env_file(path):
    env = {}
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                if '=' in line:
                    key, value = line.strip().split('=', 1)
                    env[key] = value
    except OSError:
        return {}
    return env


def _valid_interval(value):
    return type(value) is int and _INTERVAL_MIN <= value <= _INTERVAL_MAX


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #

def _build_documents(parser, settings):
    """Map ``config.ini`` values into device/secrets docs and imported settings.

    Returns ``(device, secrets, applied)``. Mutates ``settings`` in place with a
    validated snapshot interval when the persisted one is absent or invalid.
    """
    device = config_schema.default_device()
    secrets = {}
    applied = []

    token = _ini_value(parser, 'identity', 'token')
    if token:
        secrets.setdefault('prusa', {})['token'] = token
        applied.append('prusa.token')

    fingerprint = _ini_value(parser, 'identity', 'fingerprint')
    if fingerprint:
        device['fingerprint'] = fingerprint
        applied.append('fingerprint')

    server = _ini_value(parser, 'upload', 'server')
    if server:
        device['prusa']['server'] = server
        applied.append('prusa.server')

    interval = _ini_int(parser, 'upload', 'interval')
    if interval is not None and _INTERVAL_MIN <= interval <= _INTERVAL_MAX:
        if not _valid_interval(settings.get('snapshot_interval')):
            settings['snapshot_interval'] = interval
            applied.append('snapshot_interval')

    return device, secrets, applied


def _derive_quality_tier(settings):
    """Return a tier derived from ``quality.env``, or None when not applicable."""
    if type(settings.get('quality_tier')) is int and settings['quality_tier'] in quality.RESOLUTIONS:
        return None
    env = _read_env_file(QUALITY_ENV_PATH)
    try:
        width = int(env['CAM_WIDTH'])
        height = int(env['CAM_HEIGHT'])
    except (KeyError, TypeError, ValueError):
        return None
    for tier, resolution in quality.RESOLUTIONS.items():
        if resolution == (width, height):
            return tier
    return None


def _derive_rtsp_mode(settings):
    """Return a mode derived from ``rtsp.mode``, or None when not applicable."""
    existing = settings.get('rtsp_mode')
    if type(existing) is int and existing in (rtsp_control.RTSP_DISABLED, rtsp_control.RTSP_ENABLED):
        return None
    try:
        with open(RTSP_MODE_PATH, encoding='utf-8') as f:
            raw = f.read().strip()
    except OSError:
        return None
    try:
        mode = int(raw)
    except (TypeError, ValueError):
        return None
    if mode in (rtsp_control.RTSP_DISABLED, rtsp_control.RTSP_ENABLED):
        return mode
    return None


# --------------------------------------------------------------------------- #
# Backup / completion
# --------------------------------------------------------------------------- #

def _copy_if_exists(source, backup_dir):
    if not source:
        return
    try:
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(backup_dir, os.path.basename(source)))
    except OSError as e:
        log.warning(f'legacy import: could not preserve {source}: {e}')


def _find_completion(backups_root):
    """Return an existing completion record for the current target schema, if any."""
    try:
        names = os.listdir(backups_root)
    except OSError:
        return None
    for name in sorted(names, reverse=True):
        if not name.startswith('migration-'):
            continue
        path = os.path.join(backups_root, name, _COMPLETION_NAME)
        try:
            with open(path, encoding='utf-8') as f:
                record = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get('target_schema_version') == config_schema.SCHEMA_VERSION:
            return record
    return None


def _timelapse_report():
    """Report timelapse store presence and ``.jpg`` frame count (no file moves)."""
    for directory in TIMELAPSE_DIRS:
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        frames = sum(1 for name in names if name.endswith('.jpg'))
        return {'path': directory, 'exists': True, 'frame_count': frames}
    return {'path': None, 'exists': False, 'frame_count': 0}
