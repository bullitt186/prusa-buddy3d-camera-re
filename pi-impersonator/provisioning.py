"""Provisioning state machine for the public appliance (WP-3a, AC-15).

The appliance moves through a small, persisted, forward-only state machine
(source plan §4.1)::

    factory -> storage_ready -> camera_validated -> unclaimed
    unclaimed -> claimed -> configured -> running
    running -> recovery (explicit action or invalid durable configuration)
    recovery -> configured -> running

The state is persisted as JSON on the durable ``/data`` partition. While
``/data`` is not a real mountpoint (:func:`settings_store.available`) every
write is a no-op returning ``False`` and the persisted file is left alone, so
the code is safe to deploy before the PERSIST partition exists.

Claim rule
----------
``claimed`` is **never** inferred from the mere existence of a Prusa token. A
device is claimed only when an administrator password hash is present in
``secrets.toml`` *and* the durable ``device.toml`` validates. See
:func:`is_claimed`.

Secret hygiene
--------------
The persisted payload contains only ``schema_version``, ``state``,
``updated_at`` and a bounded, printable ``reason``. No token, password hash,
PSK or credential is ever written here. Failure reasons are sanitized (control
characters removed, length bounded) so a caller cannot smuggle a secret into
the log or the state file.

Stdlib only, and no file/network side effects on import.
"""
import dataclasses
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import config_schema
import identity
import settings_store

log = logging.getLogger('prusa-cam.provisioning')

PROVISIONING_SCHEMA_VERSION = 1
PROVISIONING_PATH = '/data/prusa-cam/provisioning.json'

#: Durable fallback seed location. The appliance root is a read-only overlay, so
#: /etc is volatile across reboots; the random identity seed must live under the
#: durable PERSIST partition to keep the setup SSID stable (H2).
IDENTITY_FALLBACK_PATH = '/data/prusa-cam/identity.fallback'

#: The documented forward-only chain. ``recovery`` is deliberately kept out of
#: this tuple (it is not a forward milestone) and exposed as :data:`RECOVERY`.
STATES = (
    'factory',
    'storage_ready',
    'camera_validated',
    'unclaimed',
    'claimed',
    'configured',
    'running',
)
RECOVERY = 'recovery'
ALL_STATES = STATES + (RECOVERY,)

#: One-step successor along the documented chain. ``advance`` walks this; an
#: explicit ``transition`` may only take one of these edges (plus recovery).
NEXT_STATE = {
    'factory': 'storage_ready',
    'storage_ready': 'camera_validated',
    'camera_validated': 'unclaimed',
    'unclaimed': 'claimed',
    'claimed': 'configured',
    'configured': 'running',
}

_REASON_MAX = 200
_ALNUM_RE = re.compile(r'[^a-z0-9]+')


# --------------------------------------------------------------------------- #
# Device identity for the setup hotspot / admin hostname
# --------------------------------------------------------------------------- #

def derive_device_id(seed):
    """Return the stable lowercase hex device id for ``seed``.

    Mirrors ``onvif_facade.OnvifContext.create`` exactly: the endpoint UUID is
    ``uuid5(NAMESPACE_URL, 'prusa-camera:<seed>')``. Using the same derivation
    for the setup SSID suffix and the ONVIF endpoint keeps a device's identity
    consistent across the wizard, mDNS name, and ONVIF discovery.

    Raises :class:`ValueError` for an empty/non-string seed so a caller cannot
    silently derive an all-devices-identical id.
    """
    if not isinstance(seed, str) or not seed.strip():
        raise ValueError('device seed must be a non-empty string')
    return uuid.uuid5(uuid.NAMESPACE_URL, f'prusa-camera:{seed}').hex


def _sanitize_device_id(device_id):
    """Lowercase alphanumeric projection of a device id (never raises)."""
    if isinstance(device_id, uuid.UUID):
        text = device_id.hex
    elif device_id is None:
        text = ''
    else:
        text = str(device_id)
    return _ALNUM_RE.sub('', text.lower())


def setup_ssid_suffix(device_id):
    """Return the last six lowercase alphanumeric characters of ``device_id``.

    The device id is sanitized (``:``/``-``/uppercase and any other character
    removed, lowercased) before the last six characters are taken, so a MAC,
    UUID or UUID-hex all yield the same shape. An id shorter than six
    characters (or empty) yields the whole sanitized value, possibly ``''``.
    """
    cleaned = _sanitize_device_id(device_id)
    if not cleaned:
        return ''
    return cleaned[-6:]


def setup_ssid(device_id):
    """Unclaimed setup hotspot name ``Buddy3D-Setup-<last6>`` (source §4.3).

    Returns ``''`` when no suffix can be derived, so the caller treats the
    hotspot name as unavailable rather than emitting an invalid SSID.
    """
    suffix = setup_ssid_suffix(device_id)
    if not suffix:
        return ''
    return f'Buddy3D-Setup-{suffix}'


def admin_hostname(device_id):
    """Post-claim mDNS hostname ``buddy3d-<last6>`` (``https://…local``).

    A valid DNS label (lowercase, alnum, hyphen) and at most 63 characters.
    Returns ``''`` when no suffix can be derived.
    """
    suffix = setup_ssid_suffix(device_id)
    if not suffix:
        return ''
    return f'buddy3d-{suffix}'


def resolve_device_id(device_path=None, mac_path='/sys/class/net/wlan0/address',
                      fallback_path=None):
    """Derive the stable device id before ``device.toml`` exists (WP-R1).

    Precedence mirrors the firmware identity path (:func:`identity.resolve_fingerprint`):

    1. an explicitly configured ``device.toml`` ``fingerprint`` (the value a
       registration token was bound to) wins;
    2. otherwise the raw ``wlan0`` MAC is hashed;
    3. otherwise the persisted random fallback seed, read from
       :data:`IDENTITY_FALLBACK_PATH` on the durable PERSIST partition (``/etc``
       is volatile on the read-only-root appliance, so a seed there would
       regenerate every reboot and change the setup SSID — H2).

    The chosen fingerprint is projected through :func:`derive_device_id`, so the
    setup SSID, the admin hostname and the ONVIF endpoint all share one identity
    even on an unclaimed device. Returns ``''`` when nothing can be derived and
    never raises; every path is injectable for host testing.
    """
    if fallback_path is None:
        fallback_path = IDENTITY_FALLBACK_PATH
    fingerprint = ''
    try:
        device = (config_schema.load_device(device_path)
                  if device_path is not None else config_schema.load_device())
    except Exception:  # noqa: BLE001 - a corrupt document must not stop the UI
        device = None
    if isinstance(device, dict):
        configured = device.get('fingerprint')
        if isinstance(configured, str) and configured.strip():
            fingerprint = configured.strip()

    raw_mac = ''
    try:
        with open(mac_path, encoding='utf-8') as f:
            raw_mac = f.read().strip()
    except (OSError, TypeError, ValueError):
        raw_mac = ''

    try:
        _mac, fingerprint = identity.resolve_fingerprint(
            fingerprint, raw_mac, fallback_path)
    except Exception:  # noqa: BLE001 - identity resolution must never raise
        return ''
    if not fingerprint:
        return ''
    try:
        return derive_device_id(fingerprint)
    except ValueError:
        return ''


# --------------------------------------------------------------------------- #
# Claim predicate
# --------------------------------------------------------------------------- #

def _password_set(secrets):
    """True when ``secrets['admin']['password_hash']`` is a non-empty string."""
    if not isinstance(secrets, dict):
        return False
    admin = secrets.get('admin')
    if not isinstance(admin, dict):
        return False
    value = admin.get('password_hash')
    return isinstance(value, str) and bool(value.strip())


def _token_set(secrets):
    """True when ``secrets['prusa']['token']`` is a non-empty string."""
    if not isinstance(secrets, dict):
        return False
    prusa = secrets.get('prusa')
    if not isinstance(prusa, dict):
        return False
    value = prusa.get('token')
    return isinstance(value, str) and bool(value.strip())


def device_is_valid(device):
    """True when ``device`` is a non-empty mapping that passes the schema.

    ``device`` is expected to be the dict returned by
    :func:`config_schema.load_device` (which already validated the raw TOML
    allowlist). Re-serializing and re-parsing here catches a mutated or
    hand-built mapping before it can satisfy the claim predicate.
    """
    if not isinstance(device, dict) or not device:
        return False
    try:
        config_schema.parse_device(config_schema.dumps_device(device))
    except Exception:  # noqa: BLE001 - never raise from a predicate
        return False
    return True


def is_claimed(secrets, device):
    """The claim predicate: admin password hash present *and* device valid.

    Deliberately ignores ``prusa.token``: a token alone (or a token with no
    admin password) is not a claim. This is the single shared implementation
    reused by :meth:`ProvisioningState.advance` and by the wizard.
    """
    return _password_set(secrets) and device_is_valid(device)


# --------------------------------------------------------------------------- #
# Observed facts
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class Facts:
    """Snapshot of the facts ``advance`` derives the next state from."""

    storage_ready: bool = False
    camera_validated: bool = False
    admin_password_set: bool = False
    device_valid: bool = False
    prusa_token_set: bool = False
    camera_running: bool = False
    recovery_reason: str = ''


def observe_facts(device_path=config_schema.DEVICE_TOML_PATH,
                  secrets_path=config_schema.SECRETS_TOML_PATH,
                  camera_validated=False,
                  camera_running=False,
                  recovery_reason=''):
    """Read the on-disk facts for :meth:`ProvisioningState.advance`.

    ``camera_validated`` and ``camera_running`` cannot be read from the durable
    configuration, so the caller (the camera probe / the systemd predicate)
    supplies them. All paths and probes are injectable; the function never
    raises.
    """
    storage_ready = False
    try:
        directory = os.path.dirname(device_path) or '.'
        storage_ready = bool(settings_store.available()) and os.path.isdir(directory)
    except OSError:
        storage_ready = False

    device_valid = False
    try:
        if os.path.isfile(device_path):
            config_schema.load_device(device_path)
            device_valid = True
    except config_schema.ConfigError:
        device_valid = False

    secrets = {}
    try:
        secrets = config_schema.load_secrets(secrets_path)
    except config_schema.ConfigError:
        secrets = {}

    return Facts(
        storage_ready=storage_ready,
        camera_validated=bool(camera_validated),
        admin_password_set=_password_set(secrets),
        device_valid=device_valid,
        prusa_token_set=_token_set(secrets),
        camera_running=bool(camera_running),
        recovery_reason=_sanitize_reason(recovery_reason),
    )


def _coerce_facts(value):
    """Normalize a facts-like input (Facts/dict/None) into :class:`Facts`."""
    if isinstance(value, Facts):
        return value
    if value is None:
        return Facts()
    if isinstance(value, dict):
        allowed = Facts.__dataclass_fields__
        return Facts(**{key: val for key, val in value.items() if key in allowed})
    return Facts()


def _milestone(facts):
    """Furthest chain state justified by ``facts`` (never ``recovery``)."""
    if not facts.storage_ready:
        return 'factory'
    if not facts.camera_validated:
        return 'storage_ready'
    if not (facts.admin_password_set and facts.device_valid):
        return 'unclaimed'
    if not facts.prusa_token_set:
        return 'claimed'
    if not facts.camera_running:
        return 'configured'
    return 'running'


# --------------------------------------------------------------------------- #
# Result object
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class TransitionResult:
    """Outcome of a transition/advance attempt (never carries a secret)."""

    ok: bool
    reason: str
    state: str
    previous: str = ''
    persisted: bool = False


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #

class ProvisioningState:
    """The persisted provisioning state, with safe load/save/transition.

    ``state``/``reason``/``updated_at`` are the persisted fields; ``last_good``
    is the in-session last non-recovery state used by :meth:`recover` (it
    defaults to ``configured`` after a reload, matching the documented
    ``recovery -> configured`` edge). Every public method is exception-free on
    bad input.
    """

    def __init__(self, state='factory', reason='', updated_at='', last_good='', path=None):
        self.state = state if state in ALL_STATES else 'factory'
        self.reason = _sanitize_reason(reason)
        self.updated_at = updated_at if isinstance(updated_at, str) else ''
        self.last_good = last_good if last_good in STATES else ''
        self.path = path or PROVISIONING_PATH

    # -- persistence -------------------------------------------------------- #

    @classmethod
    def load(cls, path=PROVISIONING_PATH):
        """Load the persisted state; a missing/corrupt file yields ``factory``."""
        inst = cls(path=path)
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            return inst
        except (OSError, ValueError):
            inst.reason = 'provisioning state unreadable'
            return inst
        if not isinstance(data, dict):
            inst.reason = 'provisioning state is not an object'
            return inst
        state = data.get('state')
        if state in ALL_STATES:
            inst.state = state
        else:
            inst.reason = 'provisioning state value is invalid'
        reason = data.get('reason')
        if isinstance(reason, str) and reason:
            inst.reason = _sanitize_reason(reason)
        updated_at = data.get('updated_at')
        if isinstance(updated_at, str):
            inst.updated_at = updated_at
        return inst

    def save(self, path=None):
        """Atomically persist the state; ``False`` (no write) when unavailable."""
        target = path or self.path or PROVISIONING_PATH
        if not _available():
            return False
        payload = {
            'schema_version': PROVISIONING_SCHEMA_VERSION,
            'state': self.state,
            'updated_at': self.updated_at,
            'reason': self.reason,
        }
        try:
            text = json.dumps(payload, sort_keys=True) + '\n'
            config_schema.write_atomic(target, text, mode=0o640)
        except (config_schema.ConfigError, OSError, TypeError, ValueError) as e:
            log.warning(f'provisioning: could not persist state: {e}')
            return False
        self.path = target
        return True

    # -- transitions -------------------------------------------------------- #

    def _legal(self, target):
        if target == RECOVERY:
            return self.state in STATES
        if self.state == RECOVERY:
            return target in STATES
        return NEXT_STATE.get(self.state) == target

    def _apply(self, target, reason, now):
        self.state = target
        self.reason = _sanitize_reason(reason)
        self.updated_at = _format_now(now)
        if target != RECOVERY:
            self.last_good = target

    def transition(self, to, reason='', now=None):
        """Attempt ``to``; on an illegal edge leave the persisted state unchanged.

        A legal edge is persisted before it is reported as accepted, so the
        in-memory state can never claim a state that did not reach disk. The
        result never contains a secret.
        """
        target = getattr(to, 'state', to)
        if target not in ALL_STATES:
            return TransitionResult(False, 'unknown provisioning state', self.state)
        if target == self.state:
            return TransitionResult(True, 'already in state', self.state, previous=self.state)
        if not self._legal(target):
            return TransitionResult(
                False, f'illegal transition {self.state} -> {target}', self.state
            )
        snapshot = (self.state, self.reason, self.updated_at, self.last_good)
        self._apply(target, reason, now)
        if not self.save():
            self.state, self.reason, self.updated_at, self.last_good = snapshot
            return TransitionResult(
                False, 'durable storage unavailable; state not persisted', self.state
            )
        return TransitionResult(
            True, self.reason, self.state, previous=snapshot[0], persisted=True
        )

    def advance(self, now=None, facts=None):
        """Derive and apply the next state from observed ``facts``.

        ``facts`` defaults to :func:`observe_facts`. The machine moves at most
        one edge per call, toward the furthest milestone the facts justify, so
        ``camera_validated`` is always visited before ``unclaimed``. A recovery
        reason forces ``recovery``; a valid last-good state is restored by
        :meth:`recover`.
        """
        facts = _coerce_facts(observe_facts() if facts is None else facts)

        if self.state == RECOVERY:
            if facts.recovery_reason:
                return self.transition(RECOVERY, facts.recovery_reason, now)
            return self.recover(now)
        if facts.recovery_reason:
            return self.transition(RECOVERY, facts.recovery_reason, now)

        index = STATES.index(self.state)
        if index >= STATES.index('claimed') and (
                not facts.storage_ready
                or (facts.admin_password_set and not facts.device_valid)):
            return self.transition(
                RECOVERY, 'durable configuration is invalid', now
            )

        target = _milestone(facts)
        target_index = STATES.index(target)
        if target_index == index:
            return TransitionResult(True, 'no change', self.state, previous=self.state)
        step = STATES[index + 1] if target_index > index else STATES[index - 1]
        return self.transition(step, f'advanced to {step}', now)

    def recover(self, now=None):
        """Clear recovery and return to the last known good state."""
        if self.state != RECOVERY:
            return TransitionResult(False, 'not in recovery', self.state)
        target = self.last_good if self.last_good in STATES else 'configured'
        return self.transition(target, 'recovery cleared', now)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _available():
    """Best-effort ``settings_store.available()`` (never raises)."""
    try:
        return bool(settings_store.available())
    except Exception:  # noqa: BLE001 - persistence must never raise
        return False


def _format_now(now):
    """Format ``now`` (datetime/str/None) as an ISO-8601 UTC timestamp."""
    if isinstance(now, str) and now:
        return now
    if isinstance(now, datetime):
        moment = now
    else:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _sanitize_reason(reason):
    """Bound and strip a reason so it can never carry a secret payload."""
    if not isinstance(reason, str):
        return ''
    cleaned = ''.join(
        ch for ch in reason if ch.isprintable() and ch not in '\r\n\t'
    ).strip()
    if len(cleaned) > _REASON_MAX:
        cleaned = cleaned[:_REASON_MAX]
    return cleaned
