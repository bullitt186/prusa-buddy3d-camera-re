"""Firmware OTA check-in classification (GAP-OTA-01).

Recovered 3.1.6 behavior: the camera periodically queries
``connect-ota.prusa3d.com/api/niceboy/v1/camera`` and compares the returned
release metadata (``file``, ``last_version``, ``sha1sum``, ``force_upgrade``)
against its running version, then downloads/verifies/installs and reports
progress through ``client_trigger``.

Policy for this Pi impersonator (owner decision, 2026-09-19): **truthful
decline** — the endpoint is queried and classified, but no firmware is ever
flashed. ``start_fw_update`` returns an explicit unsupported result instead of a
silent no-op. This module is stdlib-only and pure so the decisions are testable.
"""
import hashlib
import json

OTA_ENDPOINT = 'https://connect-ota.prusa3d.com/api/niceboy/v1/camera'
OTA_CHECK_INTERVAL = 6 * 3600  # seconds; firmware polls periodically

# Classifications returned by ``classify``.
UP_TO_DATE = 'up_to_date'
UPDATE_AVAILABLE = 'update_available'
FORCED_UPDATE = 'forced_update'
INVALID = 'invalid'

# Why an update cannot be applied here.
DECLINE_REASON = 'pi_impersonator_does_not_flash_firmware'


def parse_checkin(body):
    """Parse the OTA JSON body into a dict, or ``None`` when unusable."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not data.get('file') or not data.get('last_version'):
        return None
    return data


def _version_key(version):
    """Comparable tuple for dotted numeric versions; non-numeric parts sort low."""
    parts = []
    for chunk in str(version).split('.'):
        try:
            parts.append((0, int(chunk)))
        except ValueError:
            parts.append((1, chunk))
    return tuple(parts)


def is_update_available(current_version, last_version):
    return _version_key(last_version) > _version_key(current_version)


def classify(current_version, body):
    """Return one of UP_TO_DATE / UPDATE_AVAILABLE / FORCED_UPDATE / INVALID."""
    data = parse_checkin(body) if isinstance(body, (str, bytes)) else body
    if not data:
        return INVALID
    if is_update_available(current_version, data['last_version']):
        return UPDATE_AVAILABLE
    # A forced flag on an equal/newer release still counts as forced.
    if data.get('force_upgrade') and is_update_available(current_version, data['last_version']):
        return FORCED_UPDATE
    return UP_TO_DATE


def verify_integrity(data, actual_sha1):
    """True when the release's declared sha1sum matches the downloaded file."""
    expected = (data or {}).get('sha1sum')
    if not expected or not actual_sha1:
        return False
    return expected.lower() == actual_sha1.lower()


def sha1_of(data):
    return hashlib.sha1(data).hexdigest()


def can_apply(classification):
    """The Pi never flashes firmware; every classification is declined."""
    return False


def decline_reason():
    return DECLINE_REASON
