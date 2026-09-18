import hashlib
import logging
import os
import re
import secrets
import string


log = logging.getLogger('prusa-cam.identity')

_MAC_RE = re.compile(r'^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$')

FALLBACK_SEED_LENGTH = 10

# ASSUMPTION (GAP-IDENTITY-01): the exact alphabet used by firmware
# ``FUN_000997f8(..., 10, 1)`` has not been recovered. Alphanumeric is the
# narrowest documented choice that reproduces the ten-character seed shape.
FALLBACK_SEED_ALPHABET = string.ascii_letters + string.digits

# Stable persistence location, overridable for tests. On the production Pi the
# root filesystem is a read-only overlay, so this file is durable only once it
# reaches the lower filesystem via deploy.sh; until then the seed is stable for
# the running session and a restart regenerates it (with a warning).
IDENTITY_FALLBACK_FILE = os.environ.get(
    'PRUSA_IDENTITY_FALLBACK', '/etc/prusa-cam/identity.fallback'
)


def normalize_wifi_mac(mac):
    """Match lp_app's `%02X:%02X:%02X:%02X:%02X:%02X` formatting."""
    value = mac.strip()
    if not _MAC_RE.fullmatch(value):
        raise ValueError(f'invalid Wi-Fi MAC address: {value!r}')
    return ':'.join(part.upper() for part in value.replace('-', ':').split(':'))


def fingerprint_from_mac(mac):
    """Return the lowercase MD5 wire fingerprint produced by Buddy3D firmware."""
    normalized = normalize_wifi_mac(mac)
    return hashlib.md5(normalized.encode('ascii')).hexdigest()


def fingerprint_from_seed(seed):
    """Return lowercase MD5 hex of the exact seed text (FW-ID-MD5)."""
    return hashlib.md5(seed.encode('utf-8')).hexdigest()


def generate_fallback_seed(length=FALLBACK_SEED_LENGTH):
    """Generate a random ten-character seed (``FUN_000997f8(..., 10, 1)`` shape)."""
    return ''.join(secrets.choice(FALLBACK_SEED_ALPHABET) for _ in range(length))


def _valid_fallback_seed(seed):
    return isinstance(seed, str) and len(seed) == FALLBACK_SEED_LENGTH


def load_or_create_fallback_seed(path=None):
    """Return the persisted fallback seed, creating and persisting it once.

    A valid existing file is reused verbatim so a fingerprint already bound to a
    token cannot silently rotate. When the seed cannot be persisted the generated
    seed is still returned, but a warning states that a restart will change it.
    """
    path = path or IDENTITY_FALLBACK_FILE
    try:
        with open(path) as f:
            seed = f.read().strip()
        if _valid_fallback_seed(seed):
            return seed
        log.warning(f'identity: fallback seed file {path} invalid; regenerating')
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning(f'identity: cannot read fallback seed file {path}: {e}')

    seed = generate_fallback_seed()
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            f.write(seed)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        log.warning(
            f'identity: cannot persist fallback seed to {path}: {e}; '
            'the fallback fingerprint will change on restart'
        )
    return seed


def identity_from_mac_or_fallback(raw_mac, fallback_path=None):
    """Return ``(normalized_mac, fingerprint)``, falling back on a bad MAC.

    Firmware ``FUN_00096cd8`` hashes the formatted ``wlan0`` MAC and, when the
    lookup returns empty, hashes a random ten-character seed instead. ``raw_mac``
    is the sysfs text (or ``''``/``None`` when unreadable); the normal path is
    byte-exact and the fallback returns an empty MAC with a persisted seed.
    """
    try:
        mac = normalize_wifi_mac(raw_mac or '')
    except (TypeError, ValueError, AttributeError):
        seed = load_or_create_fallback_seed(fallback_path)
        return '', fingerprint_from_seed(seed)
    return mac, fingerprint_from_mac(mac)
