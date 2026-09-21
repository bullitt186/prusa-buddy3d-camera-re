"""Admin authentication and session primitives for the appliance UI (WP-3b, AC-19).

This module owns the *host-testable* security core behind the persistent admin
web UI. It deliberately has no HTTP, Socket.IO, aiohttp, or GStreamer
dependency, so the whole policy can be unit-tested on a workstation and the
later HTTP wizard layer is a thin adapter over these functions.

Covered here (source plan §4.4 / §4.5, §5.2, acceptance AC-19):

* Salted ``hashlib.scrypt`` password hashing and constant-time verification.
* Server-side sessions with *both* idle and absolute expiry, plus a per-session
  CSRF token. No client-side session state exists; the cookie only carries an
  opaque random identifier.
* Hardened ``Set-Cookie`` construction (``HttpOnly``, ``SameSite=Lax``,
  ``Secure``, ``Path=/``, ``Max-Age``).
* A bounded, memory-pruning login rate limiter with escalating lockout.
* The re-authentication policy for destructive / credential-bearing actions.
* Recursive key-based redaction for UI responses and logs.

Security notes
--------------
* The plaintext password is never stored, returned, logged, or embedded in the
  encoded hash string.
* All comparisons of secrets (password digests, CSRF tokens) use
  :func:`hmac.compare_digest`.
* Verification is total: malformed encoded hashes return ``False`` rather than
  raising, so a corrupt secrets file cannot crash the login path.
* Stdlib only, and no file/network/thread side effects on import.
"""
import base64
import hashlib
import hmac
import secrets
import time

# --------------------------------------------------------------------------- #
# Password hashing (scrypt)
# --------------------------------------------------------------------------- #

#: scrypt work factors. n=2**14 with r=8 costs 16 MiB per hash and ~60 ms on the
#: appliance class of CPU: strong enough for a low-traffic admin login, cheap
#: enough not to stall a Pi Zero-class device. p=1 keeps the cost single-lane.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_SALT_BYTES = 16

#: Hard ceilings accepted while *verifying* an untrusted encoded hash, so a
#: tampered secrets file cannot make verification allocate unbounded memory.
_MAX_SCRYPT_N = 2 ** 15
_MAX_SCRYPT_R = 16
_MAX_SCRYPT_P = 4
_MAX_SCRYPT_DIGEST = 64
_MAX_SCRYPT_SALT = 64

#: Sane password bounds. The minimum is enforced by :func:`password_strength_ok`
#: (not by hashing, which stays a pure primitive); the maximum is enforced by
#: both so a hostile request cannot force a huge scrypt input.
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024


def _maxmem(n, r):
    """Return a scrypt ``maxmem`` that comfortably fits the n/r work factors."""
    return 128 * n * r * 2


def _b64encode(raw):
    return base64.b64encode(raw).decode('ascii')


def _valid_password_input(password):
    return isinstance(password, str) and 0 < len(password) <= MAX_PASSWORD_LENGTH


def hash_password(password):
    """Return a self-describing scrypt hash for ``password``.

    The encoding is ``scrypt$<n>$<r>$<p>$<b64 salt>$<b64 digest>``. A fresh
    16-byte salt is generated for every call, so hashing the same password
    twice yields different strings. The plaintext is never part of the result.

    Raises :class:`ValueError` for a non-string, empty, or over-long password.
    """
    if not _valid_password_input(password):
        raise ValueError(
            'password must be a non-empty string of at most '
            f'{MAX_PASSWORD_LENGTH} characters'
        )
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode('utf-8'),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=_maxmem(SCRYPT_N, SCRYPT_R),
    )
    return 'scrypt${}${}${}${}${}'.format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P, _b64encode(salt), _b64encode(digest)
    )


def verify_password(password, encoded):
    """Constant-time check of ``password`` against an encoded scrypt hash.

    Returns ``False`` (never raises) for an empty/non-string password, a
    malformed encoding, out-of-range parameters, or a mismatch.
    """
    if not _valid_password_input(password):
        return False
    if not isinstance(encoded, str):
        return False
    parts = encoded.split('$')
    if len(parts) != 6 or parts[0] != 'scrypt':
        return False
    try:
        n = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = base64.b64decode(parts[4], validate=True)
        expected = base64.b64decode(parts[5], validate=True)
    except (ValueError, TypeError):
        return False
    if not (0 < n <= _MAX_SCRYPT_N and 0 < r <= _MAX_SCRYPT_R and 0 < p <= _MAX_SCRYPT_P):
        return False
    if not (0 < len(salt) <= _MAX_SCRYPT_SALT and 0 < len(expected) <= _MAX_SCRYPT_DIGEST):
        return False
    try:
        actual = hashlib.scrypt(
            password.encode('utf-8'),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=_maxmem(n, r),
        )
    except (ValueError, MemoryError, OverflowError):
        return False
    return hmac.compare_digest(actual, expected)


def password_strength_ok(password):
    """Return ``(ok, reason)`` for the documented password strength rule.

    The rule is intentionally minimal and non-secret: a string of at least
    :data:`MIN_PASSWORD_LENGTH` characters, at most :data:`MAX_PASSWORD_LENGTH`
    characters, and not only whitespace. ``reason`` is safe to show the user.
    """
    if not isinstance(password, str):
        return False, 'password must be a string'
    if password.strip() == '':
        return False, 'password must not be empty or only whitespace'
    if len(password) < MIN_PASSWORD_LENGTH:
        return False, f'password must be at least {MIN_PASSWORD_LENGTH} characters'
    if len(password) > MAX_PASSWORD_LENGTH:
        return False, f'password must be at most {MAX_PASSWORD_LENGTH} characters'
    return True, 'ok'


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #

#: Cookie name and policy. SameSite=Lax is chosen over Strict so a top-level
#: navigation from a bookmark/other origin still presents the session while
#: cross-site POSTs (the CSRF-relevant case) do not.
SESSION_COOKIE_NAME = 'prusa_admin_session'
SESSION_SAMESITE = 'Lax'

#: Defaults: 30 minutes idle, 12 hours absolute.
DEFAULT_IDLE_TTL = 30 * 60.0
DEFAULT_ABSOLUTE_TTL = 12 * 60 * 60.0

#: Bound on concurrently live sessions. The store is server-side and in-memory,
#: so an unauthenticated flood must not grow it without limit; the oldest
#: session is evicted when the cap is reached.
MAX_SESSIONS = 256


def _now(now=None):
    """Return the supplied clock value, or the monotonic host clock.

    Monotonic time is used by default so session lifetime is immune to wall
    clock changes. Tests pass an explicit ``now`` and never sleep.
    """
    return time.monotonic() if now is None else float(now)


class SessionStore:
    """Server-side session table with idle + absolute expiry and CSRF tokens.

    A session is identified by an opaque ``secrets.token_urlsafe(32)`` value.
    The store records the creation time (absolute expiry anchor), the last-seen
    time (idle expiry anchor), and a distinct CSRF token. :meth:`validate`
    refreshes the idle anchor on every successful use but never the absolute
    anchor.
    """

    def __init__(self, idle_ttl=DEFAULT_IDLE_TTL, absolute_ttl=DEFAULT_ABSOLUTE_TTL,
                 max_sessions=MAX_SESSIONS):
        self.idle_ttl = float(idle_ttl)
        self.absolute_ttl = float(absolute_ttl)
        self.max_sessions = int(max_sessions)
        self._sessions = {}

    def create(self, now=None, idle_ttl=None, absolute_ttl=None):
        """Create a session and return its opaque token.

        ``idle_ttl``/``absolute_ttl`` override the store defaults for this
        session only. Expired sessions are pruned and the oldest live session
        is evicted if the store is at :attr:`max_sessions`.
        """
        now = _now(now)
        self._prune(now)
        token = secrets.token_urlsafe(32)
        while token in self._sessions:
            token = secrets.token_urlsafe(32)
        self._sessions[token] = {
            'csrf': secrets.token_urlsafe(32),
            'created': now,
            'last_seen': now,
            'idle_ttl': float(self.idle_ttl if idle_ttl is None else idle_ttl),
            'absolute_ttl': float(self.absolute_ttl if absolute_ttl is None else absolute_ttl),
        }
        if len(self._sessions) > self.max_sessions:
            self._evict_oldest()
        return token

    def validate(self, token, now=None):
        """Return True if ``token`` is live, refreshing its idle timer.

        A session expires when either ``now - created >= absolute_ttl`` or
        ``now - last_seen >= idle_ttl``. An expired session is removed.
        """
        now = _now(now)
        record = self._sessions.get(token) if isinstance(token, str) else None
        if record is None:
            return False
        if now - record['created'] >= record['absolute_ttl']:
            self._sessions.pop(token, None)
            return False
        if now - record['last_seen'] >= record['idle_ttl']:
            self._sessions.pop(token, None)
            return False
        record['last_seen'] = now
        return True

    def revoke(self, token):
        """Drop ``token``; return True if it existed."""
        return self._sessions.pop(token, None) is not None

    def revoke_all(self):
        """Drop every session."""
        self._sessions.clear()

    def count(self):
        """Return the number of sessions currently stored."""
        return len(self._sessions)

    def csrf_for(self, token):
        """Return the CSRF token bound to ``token``, or None if unknown."""
        record = self._sessions.get(token) if isinstance(token, str) else None
        return record['csrf'] if record is not None else None

    def validate_csrf(self, token, csrf):
        """Constant-time check that ``csrf`` matches the session's token."""
        if not isinstance(csrf, str):
            return False
        expected = self.csrf_for(token)
        if expected is None:
            return False
        return hmac.compare_digest(expected, csrf)

    def _prune(self, now):
        expired = [
            token for token, record in self._sessions.items()
            if now - record['created'] >= record['absolute_ttl']
            or now - record['last_seen'] >= record['idle_ttl']
        ]
        for token in expired:
            del self._sessions[token]

    def _evict_oldest(self):
        oldest = min(self._sessions, key=lambda t: self._sessions[t]['last_seen'])
        del self._sessions[oldest]


# --------------------------------------------------------------------------- #
# Cookies
# --------------------------------------------------------------------------- #

def session_cookie(token, max_age=DEFAULT_IDLE_TTL, secure=True):
    """Return a hardened ``Set-Cookie`` value for an admin session.

    Always sets ``HttpOnly``, ``SameSite=Lax``, and ``Path=/``; ``Secure`` is
    on by default and only disabled for local plain-HTTP development.
    """
    if not isinstance(token, str) or not token:
        raise ValueError('token must be a non-empty string')
    parts = [
        f'{SESSION_COOKIE_NAME}={token}',
        'Path=/',
        f'Max-Age={int(max_age)}',
        'HttpOnly',
        f'SameSite={SESSION_SAMESITE}',
    ]
    if secure:
        parts.append('Secure')
    return '; '.join(parts)


def clear_session_cookie(secure=True):
    """Return a ``Set-Cookie`` value that deletes the admin session cookie."""
    parts = [
        f'{SESSION_COOKIE_NAME}=',
        'Path=/',
        'Max-Age=0',
        'HttpOnly',
        f'SameSite={SESSION_SAMESITE}',
    ]
    if secure:
        parts.append('Secure')
    return '; '.join(parts)


# --------------------------------------------------------------------------- #
# Login rate limiting
# --------------------------------------------------------------------------- #

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_WINDOW = 5 * 60.0
DEFAULT_LOCKOUT = 15 * 60.0
DEFAULT_MAX_LOCKOUT = 60 * 60.0
DEFAULT_MAX_KEYS = 1024


class LoginRateLimiter:
    """Per-key login throttle with a sliding window and escalating lockout.

    ``key`` is an IP address or username string. Failures are counted inside a
    sliding ``window``; once ``max_attempts`` are recorded the key is locked
    for ``lockout`` seconds, doubling on each repeat lockout up to
    ``max_lockout``. State is pruned on every call and capped at ``max_keys``
    entries so memory cannot grow without bound. Nothing is logged.
    """

    def __init__(self, max_attempts=DEFAULT_MAX_ATTEMPTS, window=DEFAULT_WINDOW,
                 lockout=DEFAULT_LOCKOUT, max_lockout=DEFAULT_MAX_LOCKOUT,
                 max_keys=DEFAULT_MAX_KEYS):
        self.max_attempts = int(max_attempts)
        self.window = float(window)
        self.lockout = float(lockout)
        self.max_lockout = float(max_lockout)
        self.max_keys = int(max_keys)
        self._entries = {}

    @staticmethod
    def _key(key):
        return key if isinstance(key, str) else str(key)

    def check(self, key, now=None):
        """Return ``(allowed, retry_after)`` for ``key`` at ``now``.

        ``retry_after`` is the positive remaining lockout in seconds when
        ``allowed`` is False, and ``0`` otherwise.
        """
        now = _now(now)
        self._prune(now)
        entry = self._entries.get(self._key(key))
        if entry is None:
            return True, 0
        if entry['locked_until'] > now:
            return False, entry['locked_until'] - now
        return True, 0

    def record_failure(self, key, now=None):
        """Record a failed attempt, locking the key once the threshold is met."""
        now = _now(now)
        key = self._key(key)
        entry = self._entries.setdefault(
            key, {'failures': [], 'locked_until': 0.0, 'streak': 0}
        )
        entry['failures'].append(now)
        entry['failures'] = [t for t in entry['failures'] if now - t < self.window]
        if len(entry['failures']) >= self.max_attempts:
            entry['streak'] += 1
            backoff = min(self.lockout * (2 ** (entry['streak'] - 1)), self.max_lockout)
            entry['locked_until'] = now + backoff
        self._prune(now)

    def record_success(self, key):
        """Clear all failure/lockout state for ``key`` after a valid login."""
        self._entries.pop(self._key(key), None)

    def count(self):
        """Return the number of keys currently tracked."""
        return len(self._entries)

    def _prune(self, now):
        stale = []
        for key, entry in self._entries.items():
            entry['failures'] = [t for t in entry['failures'] if now - t < self.window]
            if not entry['failures'] and entry['locked_until'] <= now:
                stale.append(key)
        for key in stale:
            del self._entries[key]
        if len(self._entries) > self.max_keys:
            ordered = sorted(self._entries, key=lambda k: self._activity(self._entries[k]))
            for key in ordered[:len(self._entries) - self.max_keys]:
                del self._entries[key]

    @staticmethod
    def _activity(entry):
        last_failure = entry['failures'][-1] if entry['failures'] else 0.0
        return max(last_failure, entry['locked_until'])


# --------------------------------------------------------------------------- #
# Re-authentication policy
# --------------------------------------------------------------------------- #

#: Actions that require the current admin password before proceeding
#: (source plan §4.5).
REAUTH_ACTIONS = frozenset({
    'credential_change',
    'update_install',
    'factory_reset',
    'backup_export_secrets',
    'ssh_enable',
})


def requires_reauth(action):
    """Return True if ``action`` is gated behind re-authentication."""
    return action in REAUTH_ACTIONS


def reauth_ok(session, password, encoded):
    """Verify a re-auth attempt for an authenticated session.

    ``session`` must be a truthy authenticated session identifier; ``password``
    is checked against the stored ``encoded`` hash. Returns False for a missing
    session or a bad password and never raises.
    """
    if not session:
        return False
    return verify_password(password, encoded)


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

REDACTED = '<redacted>'

#: Keys (normalized to lowercase-hyphen form) whose *values* are secrets. The
#: set covers Prusa/MQTT/Wi-Fi/admin credentials and HTTP cookie/auth headers.
REDACT_KEYS = frozenset({
    'token',
    'access-token',
    'refresh-token',
    'registration-token',
    'camera-token',
    'prusa-token',
    'fingerprint',
    'camera-fingerprint',
    'password',
    'password-hash',
    'admin-password',
    'admin-password-hash',
    'mqtt-password',
    'mqtt-pass',
    'mqtt-username',
    'mqtt-user',
    'username',
    'user',
    'psk',
    'wifi-psk',
    'wifi-password',
    'secret',
    'client-secret',
    'api-key',
    'cookie',
    'set-cookie',
    'authorization',
})

#: Header names whose values are secrets regardless of the surrounding schema.
SENSITIVE_HEADERS = frozenset({
    'cookie',
    'set-cookie',
    'authorization',
    'x-camera-token',
    'x-camera-fingerprint',
    'x-api-key',
    'x-auth-token',
})


def _normalize_key(key):
    return key.strip().lower().replace('_', '-') if isinstance(key, str) else key


def _is_sensitive_key(key):
    normalized = _normalize_key(key)
    return normalized in REDACT_KEYS or normalized in SENSITIVE_HEADERS


def _is_sensitive_header(key):
    return _is_sensitive_key(key)


def _secret_values(secrets):
    return tuple(s for s in (secrets or ()) if isinstance(s, str) and s)


def _scrub_string(value, secret_values):
    for secret in secret_values:
        value = value.replace(secret, REDACTED)
    return value


def redact(value, secrets=()):
    """Return a recursively redacted copy of ``value``.

    Any mapping entry whose key is sensitive (tokens, fingerprints, MQTT/Wi-Fi
    credentials, ``password_hash``, ``Cookie``/``Set-Cookie``/``Authorization``)
    becomes :data:`REDACTED`. Lists, tuples, and sets are rebuilt element-wise.
    ``secrets`` optionally supplies literal secret substrings to scrub from
    ordinary string values as well. Safe on arbitrary structures.
    """
    secret_values = _secret_values(secrets)
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_sensitive_key(key) else redact(item, secret_values)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return type(value)(redact(item, secret_values) for item in value)
    if isinstance(value, str):
        return _scrub_string(value, secret_values)
    return value


def redact_dict(mapping, secrets=()):
    """Redact a mapping (convenience wrapper around :func:`redact`)."""
    return redact(mapping, secrets)


def redact_headers(headers, secrets=()):
    """Redact sensitive HTTP header values from a dict or pair sequence.

    Accepts a mapping or a list/tuple of ``(name, value)`` pairs and preserves
    that shape. Header-name matching is case-insensitive.
    """
    secret_values = _secret_values(secrets)
    if isinstance(headers, dict):
        return {
            name: REDACTED if _is_sensitive_header(name) else redact(value, secret_values)
            for name, value in headers.items()
        }
    if isinstance(headers, (list, tuple)):
        out = []
        for item in headers:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                name, value = item
                redacted_value = REDACTED if _is_sensitive_header(name) else redact(value, secret_values)
                out.append(type(item)((name, redacted_value)))
            else:
                out.append(redact(item, secret_values))
        return type(headers)(out)
    return redact(headers, secret_values)
