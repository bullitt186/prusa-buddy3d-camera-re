"""Stdlib-only admin/provisioning HTTP core (WP-3e1, AC-17/AC-19/AC-20).

This module owns the *host-testable* request routing and policy layer behind the
appliance's setup captive portal and post-claim admin UI (source plan §4.3-§4.5,
§6.4). It has **no** ``aiohttp``/``socketio``/GStreamer dependency: the real HTTP
transport is a later, thin adapter that turns an incoming socket request into a
:class:`Request` and writes the returned :class:`Response`. Everything here is
stdlib-only, import-safe, and fully injectable, so the whole policy can be driven
from a workstation.

Reused, not duplicated
----------------------
The security primitives already exist and are reused verbatim:

* :mod:`admin_auth` -- :class:`SessionStore`, :class:`LoginRateLimiter`,
  :func:`session_cookie`/:func:`clear_session_cookie`, :func:`reauth_ok`,
  :func:`requires_reauth`, and the :func:`redact*` helpers.
* :mod:`setup_wizard` -- :class:`WizardSession` (steps 1-10).
* :mod:`provisioning` -- :class:`ProvisioningState`.
* :mod:`expert_config` -- validate-before-apply TOML editing.
* :mod:`ssh_control`, :mod:`recovery`, :mod:`factory_reset`, :mod:`hotspot`,
  :mod:`camera_probe` -- the device operations behind the routes.

Routing and policy
------------------
A small route table maps ``(method, path pattern)`` to a handler and one of three
policies:

``public``
    No session required. Covers ``GET /``, ``GET /admin``, ``GET /api/status`` and
    ``POST /api/login``. The ONVIF/snapshot/RTSP surfaces remain unauthenticated
    by v1 product policy but are explicitly labelled *trusted LAN only*.
``public_setup``
    The captive-portal wizard. Available **only** while the device is unclaimed
    and the app is in ``setup`` mode; otherwise a ``GET`` redirects to ``/admin``
    and a ``POST`` returns ``409``.
``authenticated``
    A live server-side session is required. State-changing methods additionally
    require the per-session CSRF token.
``reauth_required``
    A live session **and** a fresh admin-password re-authentication (via
    :func:`admin_auth.reauth_ok`) are required. The request may carry the
    password inline, or the session may be inside a short re-auth window primed
    by ``POST /api/reauth``. Re-auth attempts share the login rate limiter.

Setup -> admin transition
-------------------------
While unclaimed the transport builds ``AdminApp(mode='setup')`` and serves the
public wizard. Once the device is claimed the transport builds
``AdminApp(mode='admin')``; the wizard routes then return ``409``/redirect and
every state change requires session + CSRF + re-auth.

CSRF and the setup portal
-------------------------
Pre-claim the wizard has no session to bind a CSRF token to, so ``public_setup``
POSTs are exempt from session CSRF; they are instead constrained to the
unclaimed state and the captive portal / trusted physical location (source
§4.3). Every post-claim state change (``authenticated``/``reauth_required``) is
CSRF-protected. ``POST /api/login`` is likewise exempt because no session exists
yet.

Secret hygiene
--------------
Every response body and every logged header mapping passes through
:func:`admin_auth.redact`/:func:`admin_auth.redact_headers`. Session identifiers
travel only in the ``Set-Cookie`` header; factory-reset confirmation tokens stay
server-side and are never emitted. No token, PSK, password hash or cookie value
is returned in a body or written to a log.
"""
import dataclasses
import http.cookies
import ipaddress
import json
import logging
import math
import os
import re
import time
import urllib.parse

import admin_auth
import config_schema
import expert_config
import factory_reset as factory_reset_module
import provisioning
import recovery
import setup_wizard
import ssh_control

log = logging.getLogger('prusa-cam.admin_http')

# --------------------------------------------------------------------------- #
# Trusted-LAN labelling (source §4.5)
# --------------------------------------------------------------------------- #

#: RFC1918 private, IPv4/IPv6 link-local, and loopback networks.
_TRUSTED_NETWORKS = (
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
    ipaddress.ip_network('169.254.0.0/16'),
    ipaddress.ip_network('127.0.0.0/8'),
    ipaddress.ip_network('::1/128'),
    ipaddress.ip_network('fe80::/10'),
)

#: The documented "trusted LAN only" notice (source plan §4.5).
TRUSTED_LAN_NOTICE = (
    'Trusted LAN only: ONVIF SOAP, /snapshot.jpg and RTSP ports 8554/8555 are '
    'unauthenticated by v1 product policy. Do not port-forward TCP 80, 8554 or '
    '8555 to the Internet.'
)

#: The unauthenticated interfaces the notice applies to, for the status API.
TRUSTED_LAN_INTERFACES = (
    {'name': 'ONVIF SOAP', 'path': '/onvif/*', 'port': 80, 'authenticated': False},
    {'name': 'Snapshot', 'path': '/snapshot.jpg', 'port': 80, 'authenticated': False},
    {'name': 'Prusa RTSP', 'port': 8554, 'authenticated': False},
    {'name': 'HA RTSP', 'port': 8555, 'authenticated': False},
)


def is_trusted_lan(peer_ip):
    """Return True when ``peer_ip`` is loopback, link-local, or RFC1918.

    The socket peer address is used directly; a forwarding header is never
    consulted. A missing, malformed, or public address is not trusted.
    """
    # Strict parse: no surrounding whitespace and no control characters. The
    # previous ``.strip()`` silently accepted e.g. ``' 10.0.0.1 '`` or a value
    # carrying an embedded control byte; both are now rejected outright.
    if not isinstance(peer_ip, str) or not peer_ip:
        return False
    if not peer_ip.isprintable() or peer_ip != peer_ip.strip():
        return False
    try:
        address = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    return any(address in network for network in _TRUSTED_NETWORKS)


def lan_warning():
    """Return the documented trusted-LAN-only / do-not-port-forward text."""
    return TRUSTED_LAN_NOTICE


# --------------------------------------------------------------------------- #
# Request / response model
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class Request:
    """One transport-neutral HTTP request.

    ``body`` may be ``bytes`` or ``str``. ``peer_ip`` is the socket peer address
    (never a header) and is the only key used for rate limiting. ``now`` is an
    optional injected clock value; when omitted the app clock is used.
    """

    method: str
    path: str
    query: dict = dataclasses.field(default_factory=dict)
    headers: dict = dataclasses.field(default_factory=dict)
    body: bytes = b''
    peer_ip: str = ''
    now: float = None


@dataclasses.dataclass
class Response:
    """One transport-neutral HTTP response; ``body`` is always bytes."""

    status: int
    headers: dict = dataclasses.field(default_factory=dict)
    body: bytes = b''

    def __post_init__(self):
        if isinstance(self.body, str):
            self.body = self.body.encode('utf-8')
        if not isinstance(self.body, bytes):
            self.body = b'' if self.body is None else str(self.body).encode('utf-8')


# --------------------------------------------------------------------------- #
# Route definition
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class Route:
    """A route-table entry: HTTP method, compiled path pattern, policy, handler."""

    method: str
    pattern: object
    policy: str
    handler: object


#: Re-auth freshness window after a successful password re-check.
REAUTH_WINDOW_SECONDS = 5 * 60.0

#: Provisioning states in which the public setup wizard is available.
PRE_CLAIM_STATES = frozenset({
    'factory',
    'storage_ready',
    'camera_validated',
    'unclaimed',
})

_PUBLIC = 'public'
_PUBLIC_SETUP = 'public_setup'
_AUTHENTICATED = 'authenticated'
_REAUTH_REQUIRED = 'reauth_required'


@dataclasses.dataclass
class _ProvisioningView:
    """One resolved provisioning snapshot shared by gating and ``/api/status``.

    ``state`` is the effective state name (``None`` when no trustworthy state is
    known), ``source`` records where it came from (``persisted``, ``injected``,
    ``persisted_corrupt`` or ``none``), ``error`` is a short, secret-free
    recovery hint when an existing state file could not be trusted, and
    ``setup_available`` is the final, mode-aware decision. Building both answers
    from one object is what keeps the setup gate and the status payload in
    agreement.
    """

    state: object = None
    source: str = 'none'
    error: str = ''
    setup_available: bool = False


# --------------------------------------------------------------------------- #
# Admin application
# --------------------------------------------------------------------------- #

class AdminApp:
    """Route table + security policy over injected appliance dependencies.

    All dependencies are injectable so the app is hermetic in tests: the session
    store, rate limiter, wizard factory, provisioning state, hotspot/probe,
    factory-reset controller, clock, admin password hash/verifier, and the
    filesystem paths used by the wizard, expert config, and recovery sentinel.
    """

    def __init__(
        self,
        mode='setup',
        *,
        sessions=None,
        limiter=None,
        wizard_factory=None,
        provisioning_state=None,
        hotspot=None,
        probe=None,
        probe_result=None,
        factory_reset=None,
        clock=None,
        admin_hash=None,
        verify_admin=None,
        device_path=None,
        secrets_path=None,
        provisioning_path=None,
        recovery_path=None,
        ssh_runner=None,
        device_id='',
        storage_ready=None,
        wifi_scan=None,
        start_camera=None,
        activate_station=None,
    ):
        if mode not in ('setup', 'admin'):
            raise ValueError("mode must be 'setup' or 'admin'")
        self.mode = mode

        self._sessions = sessions if sessions is not None else admin_auth.SessionStore()
        self._limiter = limiter if limiter is not None else admin_auth.LoginRateLimiter()
        self._wizard_factory = wizard_factory
        self._provisioning_state = provisioning_state
        self._hotspot = hotspot
        self._probe = probe
        self._probe_result = probe_result
        self._factory_reset = factory_reset
        self._clock = clock or time.monotonic
        self._admin_hash = admin_hash
        self._verify_admin = verify_admin
        self._ssh_runner = ssh_runner

        self._device_path = (
            device_path if device_path is not None else config_schema.DEVICE_TOML_PATH
        )
        self._secrets_path = (
            secrets_path if secrets_path is not None else config_schema.SECRETS_TOML_PATH
        )
        self._provisioning_path = (
            provisioning_path if provisioning_path is not None
            else provisioning.PROVISIONING_PATH
        )
        self._recovery_path = recovery_path or recovery.RECOVERY_SENTINEL

        self._device_id = device_id or ''
        self._storage_ready = storage_ready
        self._wifi_scan = wifi_scan
        self._start_camera = start_camera
        self._activate_station = activate_station

        # Lazily built wizard session and reset confirmation state.
        self._wizard = None
        self._reauth_at = {}
        self._reset_token = ''
        self._reset_begun = False
        self._reset_confirmed = False

        self._routes = self._build_routes()

    # ------------------------------------------------------------------ #
    # Route table
    # ------------------------------------------------------------------ #

    def _build_routes(self):
        """Build the immutable route table (order-independent, method-exact)."""
        return (
            Route('GET', re.compile(r'^/$'), _PUBLIC, self._handle_root),
            Route('GET', re.compile(r'^/admin$'), _PUBLIC, self._handle_admin),
            Route('GET', re.compile(r'^/setup$'), _PUBLIC_SETUP, self._handle_setup_page),
            Route(
                'POST', re.compile(r'^/setup/step/(?P<n>[^/]+)$'),
                _PUBLIC_SETUP, self._handle_setup_step,
            ),
            Route(
                'POST', re.compile(r'^/setup/finish$'),
                _PUBLIC_SETUP, self._handle_setup_finish,
            ),
            Route('GET', re.compile(r'^/api/status$'), _PUBLIC, self._handle_status),
            Route('POST', re.compile(r'^/api/login$'), _PUBLIC, self._handle_login),
            Route(
                'POST', re.compile(r'^/api/logout$'),
                _AUTHENTICATED, self._handle_logout,
            ),
            Route(
                'POST', re.compile(r'^/api/reauth$'),
                _REAUTH_REQUIRED, self._handle_reauth,
            ),
            Route(
                'GET', re.compile(r'^/api/config$'),
                _AUTHENTICATED, self._handle_config_get,
            ),
            Route(
                'PUT', re.compile(r'^/api/config$'),
                _AUTHENTICATED, self._handle_config_put,
            ),
            Route(
                'POST', re.compile(r'^/api/ssh$'),
                _REAUTH_REQUIRED, self._handle_ssh,
            ),
            Route(
                'POST', re.compile(r'^/api/recovery/enter-setup$'),
                _REAUTH_REQUIRED, self._handle_recovery,
            ),
            Route(
                'POST', re.compile(r'^/api/reset/begin$'),
                _REAUTH_REQUIRED, self._handle_reset_begin,
            ),
            Route(
                'POST', re.compile(r'^/api/reset/confirm$'),
                _REAUTH_REQUIRED, self._handle_reset_confirm,
            ),
            Route(
                'POST', re.compile(r'^/api/reset/execute$'),
                _REAUTH_REQUIRED, self._handle_reset_execute,
            ),
        )

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #

    def handle(self, request):
        """Dispatch ``request`` to the route table and return a :class:`Response`."""
        method = (request.method or 'GET').upper()
        path = request.path or '/'
        now = request.now if request.now is not None else self._clock()
        body_data = _parse_body(request)

        match = self._match(method, path)
        if match is None:
            response = self._error(request, 404, 'not found')
            self._log_request(request, response)
            return response
        route, path_match = match

        if route.policy == _PUBLIC_SETUP and not self._setup_available():
            if method == 'GET':
                response = self._redirect('/admin')
            else:
                response = self._error(request, 409, 'setup is not available')
            self._log_request(request, response)
            return response

        if route.policy in (_AUTHENTICATED, _REAUTH_REQUIRED):
            token, denied = self._authorize(request, route.policy, body_data, now)
            if denied is not None:
                self._log_request(request, denied)
                return denied

        response = route.handler(request, path_match, body_data, now)
        self._log_request(request, response)
        return response

    def _match(self, method, path):
        """Return ``(route, match)`` for an exact method+path hit, else ``None``."""
        for route in self._routes:
            if route.method != method:
                continue
            matched = route.pattern.match(path)
            if matched is not None:
                return route, matched
        return None

    # ------------------------------------------------------------------ #
    # Policy
    # ------------------------------------------------------------------ #

    def _load_persisted_state(self):
        """Read the on-disk provisioning state, distinguishing missing from bad.

        The file is re-read on every check so a long-lived ``setup``-mode app
        observes a claim written by the wizard (or any other writer) without a
        restart. Returns ``(state, error)``:

        * ``(None, '')`` -- no path is configured, or the file is absent. The
          caller falls back to the injected snapshot: a fresh device must be
          able to serve the setup portal.
        * ``(state, '')`` -- the file exists and holds a documented state.
        * ``(None, reason)`` -- the file exists but is unreadable, not JSON, not
          an object, or carries an unknown state. ``reason`` is a fixed,
          secret-free string safe for the status payload.

        Unlike :meth:`provisioning.ProvisioningState.load`, which maps a corrupt
        file to ``factory``, an existing-but-corrupt file is *not* treated as a
        fresh device; the caller fails closed instead of reopening the
        unauthenticated wizard.
        """
        path = self._provisioning_path
        if not path or not os.path.isfile(path):
            return None, ''
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001 - a bad state file must not crash routing
            return None, 'provisioning state unreadable'
        if not isinstance(data, dict):
            return None, 'provisioning state is not an object'
        state = data.get('state')
        if not isinstance(state, str) or state not in provisioning.ALL_STATES:
            return None, 'provisioning state value is invalid'
        return state, ''

    def _load_secrets_safe(self):
        """Best-effort secrets load for the claim predicate (never raises)."""
        try:
            secrets = config_schema.load_secrets(self._secrets_path)
        except Exception:  # noqa: BLE001 - a bad file must not crash routing
            return {}
        return secrets if isinstance(secrets, dict) else {}

    def _load_device_safe(self):
        """Best-effort device load for the claim predicate (never raises)."""
        try:
            device = config_schema.load_device(self._device_path)
        except Exception:  # noqa: BLE001 - a bad file must not crash routing
            return {}
        return device if isinstance(device, dict) else {}

    def _claimable_by_facts(self):
        """True when the authoritative claim predicate says the device is claimed.

        Reuses :func:`provisioning.is_claimed` (admin password hash present *and*
        a valid device document) so the public wizard is refused on facts alone,
        independent of what the state file or injected snapshot says. A missing
        config is not a claim; an unreadable one cannot establish a claim, so it
        is not treated as claimed (the corrupt-state-file gate covers the
        takeover hole).
        """
        try:
            return bool(provisioning.is_claimed(
                self._load_secrets_safe(), self._load_device_safe()
            ))
        except Exception:  # noqa: BLE001 - a predicate must never crash routing
            return False

    def _provisioning_view(self):
        """Resolve the effective provisioning state once for gating and status.

        The persisted file is authoritative when present and valid. A corrupt
        existing file fails closed (setup unavailable, ``error`` set) and the
        injected snapshot is ignored. When no file is present the injected
        snapshot is used, preserving the fresh-device setup flow. The
        authoritative :func:`provisioning.is_claimed` predicate is a second,
        independent gate: a device claimable by facts closes the portal even if
        the state file (or the injected snapshot) says otherwise.
        """
        persisted, error = self._load_persisted_state()
        if persisted is not None:
            state = persisted
            source = 'persisted'
        elif error:
            state = None
            source = 'persisted_corrupt'
        else:
            state = getattr(self._provisioning_state, 'state', None)
            source = 'injected' if state is not None else 'none'

        claimable = self._claimable_by_facts()
        if claimable and (state is None or state in PRE_CLAIM_STATES):
            # Facts outrank a stale/unclaimed state, so /api/status never says
            # "unclaimed" while the portal is closed.
            state = 'claimed'

        available = False
        if self.mode == 'setup' and not error and not claimable:
            available = state is None or state in PRE_CLAIM_STATES

        return _ProvisioningView(
            state=state, source=source, error=error, setup_available=available,
        )

    def _setup_available(self):
        """True only in ``setup`` mode while the device is still unclaimed.

        The persisted provisioning state is authoritative and re-loaded from
        disk on every check, so once the wizard claims the device the portal is
        refused even if this app instance was built with a stale
        ``provisioning_state`` snapshot. A corrupt existing state file fails
        closed, and the authoritative :func:`provisioning.is_claimed` predicate
        is a second gate. The injected object is consulted only when no state
        file is present.
        """
        return self._provisioning_view().setup_available

    def _authorize(self, request, policy, body_data, now):
        """Enforce session, CSRF, and (for re-auth routes) fresh password checks.

        Returns ``(token, None)`` when authorized and ``(token, response)`` when
        denied. The session is validated **before** any re-auth or handler runs.
        """
        token = _session_token(request)
        if not self._sessions.validate(token, now):
            return token, self._error(request, 401, 'authentication required')

        self._prune_reauth(now)

        if request.method.upper() in ('POST', 'PUT', 'PATCH', 'DELETE'):
            csrf = _csrf_token(request, body_data)
            if not self._sessions.validate_csrf(token, csrf):
                return token, self._error(request, 403, 'invalid CSRF token')

        if policy == _REAUTH_REQUIRED:
            if self._reauth_fresh(token, now):
                return token, None
            key = request.peer_ip
            allowed, retry_after = self._limiter.check(key, now)
            if not allowed:
                return token, self._rate_limited(retry_after)
            password = body_data.get('password')
            if isinstance(password, str) and self._reauth_ok(token, password):
                self._limiter.record_success(key)
                self._reauth_at[token] = now
                return token, None
            self._limiter.record_failure(key, now)
            return token, self._error(request, 403, 're-authentication required')

        return token, None

    def _reauth_fresh(self, token, now):
        """True while ``token`` is inside a successful re-auth window."""
        moment = self._reauth_at.get(token)
        if moment is None:
            return False
        if now - moment >= REAUTH_WINDOW_SECONDS:
            self._reauth_at.pop(token, None)
            return False
        return True

    def _prune_reauth(self, now):
        """Drop re-auth windows that are expired or whose session is gone.

        Revoking/expiring a session does not otherwise reach this map, so stale
        entries would accumulate for the life of the process. Removing them
        here bounds growth without changing behavior: an entry whose session no
        longer exists can never authorize a request anyway.
        """
        stale = [
            token for token, moment in self._reauth_at.items()
            if now - moment >= REAUTH_WINDOW_SECONDS
            or self._sessions.csrf_for(token) is None
        ]
        for token in stale:
            self._reauth_at.pop(token, None)

    def _reauth_ok(self, token, password):
        """Verify ``password`` for an already-validated session (never raises)."""
        if not token:
            return False
        if self._verify_admin is not None:
            try:
                return bool(self._verify_admin(password))
            except Exception:  # noqa: BLE001 - a verifier must never crash the app
                return False
        return admin_auth.reauth_ok(token, password, self._encoded_password())

    def _verify_admin_password(self, password):
        """Verify a login password (never raises)."""
        if self._verify_admin is not None:
            try:
                return bool(self._verify_admin(password))
            except Exception:  # noqa: BLE001
                return False
        return admin_auth.verify_password(password, self._encoded_password())

    def _encoded_password(self):
        """Return the stored admin password hash, or ``''`` when unavailable."""
        if isinstance(self._admin_hash, str) and self._admin_hash:
            return self._admin_hash
        try:
            secrets = config_schema.load_secrets(self._secrets_path)
        except Exception:  # noqa: BLE001 - a corrupt file must not crash login
            return ''
        admin = secrets.get('admin') if isinstance(secrets, dict) else None
        if isinstance(admin, dict):
            value = admin.get('password_hash')
            return value if isinstance(value, str) else ''
        return ''

    # ------------------------------------------------------------------ #
    # Public routes
    # ------------------------------------------------------------------ #

    def _handle_root(self, request, match, body_data, now):
        """Redirect ``/`` to the setup wizard or the admin console."""
        if self._setup_available():
            return self._redirect('/setup')
        return self._redirect('/admin')

    def _handle_admin(self, request, match, body_data, now):
        """Minimal admin console shell; all data lives behind the authed API."""
        html = (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<title>Buddy3D Camera</title></head><body>'
            '<h1>Buddy3D Camera administration</h1>'
            '<p>Sign in through the admin API; the session cookie is HttpOnly.</p>'
            '<p>' + lan_warning() + '</p>'
            '</body></html>'
        )
        return self._html(request, 200, html)

    def _handle_status(self, request, match, body_data, now):
        """Public status JSON, including the trusted-LAN labelling.

        ``setup_available`` and ``provisioning_state`` come from the same
        :meth:`_provisioning_view`, so the payload can never claim the device is
        ``unclaimed`` while the portal is closed (or vice versa). When an
        existing state file is corrupt, ``provisioning_error`` carries a short,
        secret-free recovery hint and ``provisioning_source`` is
        ``persisted_corrupt``.
        """
        view = self._provisioning_view()
        payload = {
            'ok': True,
            'mode': self.mode,
            'setup_available': view.setup_available,
            'provisioning_state': view.state,
            'provisioning_source': view.source,
            'trusted_lan': {
                'notice': lan_warning(),
                'interfaces': TRUSTED_LAN_INTERFACES,
            },
        }
        if view.error:
            payload['provisioning_error'] = view.error
        if self._hotspot is not None:
            payload['hotspot'] = _hotspot_view(self._hotspot)
        if self._probe is not None:
            payload['camera'] = _probe_view(self._probe)
        if self._ssh_runner is not None:
            payload['ssh'] = _ssh_view(self._ssh_runner)
        return self._json(request, 200, payload)

    def _handle_login(self, request, match, body_data, now):
        """Rate-limited password login; sets the hardened session cookie."""
        key = request.peer_ip
        allowed, retry_after = self._limiter.check(key, now)
        if not allowed:
            return self._rate_limited(retry_after)

        password = body_data.get('password')
        if self._verify_admin_password(password):
            self._limiter.record_success(key)
            token = self._sessions.create(now)
            response = self._json(request, 200, {
                'ok': True,
                'mode': self.mode,
                'csrf': self._sessions.csrf_for(token),
            })
            response.headers['Set-Cookie'] = admin_auth.session_cookie(
                token, secure=(self.mode == 'admin')
            )
            return response

        self._limiter.record_failure(key, now)
        return self._error(request, 401, 'invalid credentials')

    # ------------------------------------------------------------------ #
    # Authenticated routes
    # ------------------------------------------------------------------ #

    def _handle_logout(self, request, match, body_data, now):
        """Revoke the session and clear the cookie."""
        token = _session_token(request)
        self._sessions.revoke(token)
        self._reauth_at.pop(token, None)
        response = self._json(request, 200, {'ok': True})
        response.headers['Set-Cookie'] = admin_auth.clear_session_cookie(
            secure=(self.mode == 'admin')
        )
        return response

    def _handle_reauth(self, request, match, body_data, now):
        """Prime a short re-auth window; the password check ran in ``_authorize``."""
        return self._json(request, 200, {'ok': True, 'reauth': True})

    def _handle_config_get(self, request, match, body_data, now):
        """Return the live device/secrets documents, redacted."""
        device, secrets = expert_config.current_config(
            self._device_path, self._secrets_path
        )
        return self._json(request, 200, {
            'ok': True, 'device': device, 'secrets': secrets,
        })

    def _handle_config_put(self, request, match, body_data, now):
        """Validate a candidate pair before applying it; invalid never writes."""
        device_text = body_data.get('device')
        secrets_text = body_data.get('secrets')
        if not isinstance(device_text, str):
            return self._error(request, 400, 'device configuration text is required')
        if secrets_text is not None and not isinstance(secrets_text, str):
            return self._error(request, 400, 'secrets configuration text must be a string')

        ok, reason, _device, _secrets = expert_config.validate_candidate(
            device_text, secrets_text
        )
        if not ok:
            return self._error(request, 400, reason or 'invalid configuration')

        result = expert_config.apply_candidate(
            device_text, secrets_text, self._device_path, self._secrets_path
        )
        if not result.ok:
            return self._error(request, 500, result.reason or 'could not apply configuration')
        return self._json(request, 200, {'ok': True, 'wrote': list(result.wrote)})

    def _handle_ssh(self, request, match, body_data, now):
        """Enable or disable SSH (re-auth gated)."""
        enabled = body_data.get('enabled')
        if not isinstance(enabled, bool):
            return self._error(request, 400, 'enabled must be a boolean')
        if enabled:
            result = ssh_control.enable_ssh(self._ssh_runner)
        else:
            result = ssh_control.disable_ssh(self._ssh_runner)
        status = 200 if result.ok else 500
        return self._json(request, status, {
            'ok': result.ok,
            'enabled': result.enabled,
            'reason': result.reason,
        })

    def _handle_recovery(self, request, match, body_data, now):
        """Enter setup mode by writing the BOOT recovery sentinel (re-auth gated)."""
        reason = body_data.get('reason')
        if not isinstance(reason, str):
            reason = ''
        result = recovery.enter_setup_mode(
            reason, authorized=True, path=self._recovery_path
        )
        status = 200 if result.ok else 500
        return self._json(request, status, {
            'ok': result.ok,
            'action': result.action,
            'reason': result.reason,
        })

    # -- factory reset (two-step) -------------------------------------- #

    def _reset_controller(self):
        """Return the injected factory-reset controller, creating a default one."""
        if self._factory_reset is None:
            self._factory_reset = factory_reset_module.FactoryReset()
        return self._factory_reset

    def _handle_reset_begin(self, request, match, body_data, now):
        """First confirmation; the token is kept server-side and never emitted."""
        controller = self._reset_controller()
        reason = body_data.get('reason')
        if not isinstance(reason, str):
            reason = ''
        self._reset_token = controller.begin(reason=reason)
        self._reset_begun = True
        self._reset_confirmed = False
        return self._json(request, 200, {'ok': True, 'confirm_required': True})

    def _handle_reset_confirm(self, request, match, body_data, now):
        """Second, explicit confirmation for the token minted by ``begin``."""
        if not self._reset_begun or not self._reset_token:
            return self._error(request, 409, 'factory reset requires begin() first')
        controller = self._reset_controller()
        # The second confirmation is a second authenticated+CSRF POST. The token
        # minted by ``begin`` stays server-side and is intentionally never
        # emitted, so an empty body (or a missing/blank ``token``) falls back to
        # that server-side value. A caller may still present an explicit token,
        # which must match or the confirmation is rejected.
        provided = body_data.get('token')
        token = provided if isinstance(provided, str) and provided else self._reset_token
        if not controller.confirm(token):
            return self._error(request, 409, 'factory reset confirmation rejected')
        self._reset_confirmed = True
        return self._json(request, 200, {'ok': True, 'armed': True})

    def _handle_reset_execute(self, request, match, body_data, now):
        """Destructive reset; refuses unless begin and confirm both completed."""
        if not self._reset_begun:
            return self._error(
                request, 409, 'factory reset requires two-step confirmation: begin() first'
            )
        if not self._reset_confirmed:
            return self._error(
                request, 409,
                'factory reset requires the second confirmation: confirm() first',
            )
        include_timelapse = body_data.get('include_timelapse', True)
        if not isinstance(include_timelapse, bool):
            include_timelapse = True
        controller = self._reset_controller()
        report = controller.execute(
            token=self._reset_token, include_timelapse=include_timelapse
        )
        self._reset_begun = False
        self._reset_confirmed = False
        self._reset_token = ''
        if not report.ok:
            return self._error(request, 409, report.reason or 'factory reset failed')
        return self._json(request, 200, {'ok': True, 'report': report.to_dict()})

    # ------------------------------------------------------------------ #
    # Setup wizard routes
    # ------------------------------------------------------------------ #

    def _get_wizard(self):
        """Return the lazily built wizard session for this app instance."""
        if self._wizard is None:
            if self._wizard_factory is not None:
                self._wizard = self._wizard_factory()
            else:
                self._wizard = setup_wizard.WizardSession(
                    device_id=self._device_id,
                    device_path=self._device_path,
                    secrets_path=self._secrets_path,
                    provisioning_path=self._provisioning_path,
                    probe=self._probe,
                    probe_result=self._probe_result,
                    storage_ready=self._storage_ready,
                    wifi_scan=self._wifi_scan,
                    start_camera=self._start_camera,
                    activate_station=self._activate_station,
                    hotspot_controller=(
                        self._hotspot
                        if hasattr(self._hotspot, 'stop') and hasattr(self._hotspot, 'start')
                        else None
                    ),
                )
        return self._wizard

    def _handle_setup_page(self, request, match, body_data, now):
        """Minimal setup page showing the current wizard step."""
        wizard = self._get_wizard()
        step = getattr(wizard, 'step', setup_wizard.STEP_ORDER[0])
        index = setup_wizard.STEP_ORDER.index(step) + 1 if step in setup_wizard.STEP_ORDER else 1
        title = setup_wizard.STEP_TITLES.get(step, step)
        total = len(setup_wizard.STEP_ORDER)
        html = (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<title>Buddy3D Setup</title></head><body>'
            f'<h1>Buddy3D Camera setup</h1><p>Step {index} of {total}: {title}</p>'
            '<p>Submit this step through the setup API.</p>'
            '</body></html>'
        )
        return self._html(request, 200, html)

    def _handle_setup_step(self, request, match, body_data, now):
        """Map ``/setup/step/<n>`` onto :meth:`WizardSession.submit`."""
        raw = match.group('n')
        if not raw.isdigit():
            return self._error(request, 400, 'unknown wizard step')
        number = int(raw)
        if not 1 <= number <= len(setup_wizard.STEP_ORDER):
            return self._error(request, 400, 'unknown wizard step')
        step = setup_wizard.STEP_ORDER[number - 1]

        wizard = self._get_wizard()
        try:
            result = wizard.submit(step, body_data)
        except Exception as e:  # noqa: BLE001 - never leak internals/secrets
            log.warning(f'admin_http: setup step {step} failed: {type(e).__name__}')
            return self._error(request, 400, 'step failed')

        if not result.ok:
            return self._json(request, 400, {
                'ok': False, 'step': step, 'reason': result.reason,
            })
        summary = wizard.summary() if hasattr(wizard, 'summary') else {}
        return self._json(request, 200, {
            'ok': True,
            'step': getattr(wizard, 'step', step),
            'summary': summary,
        })

    def _handle_setup_finish(self, request, match, body_data, now):
        """Stop the hotspot and start the camera target (step 10)."""
        wizard = self._get_wizard()
        try:
            result = wizard.finish(camera_running=body_data.get('camera_running'))
        except Exception as e:  # noqa: BLE001
            log.warning(f'admin_http: setup finish failed: {type(e).__name__}')
            return self._error(request, 400, 'finish failed')
        if not result.ok:
            return self._json(request, 400, {'ok': False, 'reason': result.reason})
        return self._json(request, 200, {
            'ok': True, 'state': getattr(wizard, 'provisioning_state', ''),
        })

    # ------------------------------------------------------------------ #
    # Response helpers
    # ------------------------------------------------------------------ #

    def _known_secrets(self):
        """Literal secret values scrubbed from every body/log as defence in depth."""
        values = []
        encoded = self._encoded_password()
        if encoded:
            values.append(encoded)
        wizard = self._wizard
        if wizard is not None:
            for value in (getattr(wizard, 'token', ''), getattr(wizard, 'admin_hash', '')):
                if isinstance(value, str) and value:
                    values.append(value)
            for table_name in ('wifi', 'mqtt'):
                table = getattr(wizard, table_name, None)
                if isinstance(table, dict):
                    for key in ('psk', 'password', 'username'):
                        value = table.get(key)
                        if isinstance(value, str) and value:
                            values.append(value)
        return tuple(values)

    def _json(self, request, status, payload):
        """Serialize ``payload`` as JSON after redaction."""
        safe = admin_auth.redact(payload, self._known_secrets())
        body = json.dumps(safe, sort_keys=True, default=str).encode('utf-8')
        return Response(status, {'Content-Type': 'application/json; charset=utf-8'}, body)

    def _html(self, request, status, text):
        """Return minimal HTML after literal-secret scrubbing."""
        safe = admin_auth.redact(text, self._known_secrets())
        return Response(
            status, {'Content-Type': 'text/html; charset=utf-8'},
            safe.encode('utf-8') if isinstance(safe, str) else safe,
        )

    def _text(self, request, status, text):
        """Return plain text after literal-secret scrubbing."""
        safe = admin_auth.redact(text, self._known_secrets())
        return Response(
            status, {'Content-Type': 'text/plain; charset=utf-8'},
            safe.encode('utf-8') if isinstance(safe, str) else safe,
        )

    def _error(self, request, status, message):
        """Return a JSON error for ``/api/*`` and plain text elsewhere."""
        path = request.path or ''
        if path.startswith('/api/') or path == '/api':
            return self._json(request, status, {'ok': False, 'error': message})
        return self._text(request, status, message)

    def _redirect(self, location, status=302):
        """Return a redirect with an empty body."""
        return Response(status, {'Location': location}, b'')

    def _rate_limited(self, retry_after):
        """Return a 429 with a bounded integer ``Retry-After``."""
        seconds = max(1, int(math.ceil(float(retry_after or 0))))
        return Response(
            429,
            {
                'Content-Type': 'application/json; charset=utf-8',
                'Retry-After': str(seconds),
            },
            json.dumps({'ok': False, 'error': 'too many attempts'}).encode('utf-8'),
        )

    def _log_request(self, request, response):
        """Log one redacted request line; never logs a secret.

        ``X-CSRF-Token`` is dropped before redaction independently of
        :data:`admin_auth.SENSITIVE_HEADERS`, so a future change to that shared
        list cannot start leaking the per-session CSRF token into debug logs.
        """
        try:
            safe_headers = admin_auth.redact_headers(
                _without_csrf_header(request.headers), self._known_secrets()
            )
            log.debug(
                'admin_http: %s %s -> %s headers=%s',
                (request.method or 'GET').upper(),
                request.path,
                response.status,
                safe_headers,
            )
        except Exception:  # noqa: BLE001 - logging must never break a request
            log.debug(
                'admin_http: %s %s -> %s',
                (request.method or 'GET').upper(),
                request.path,
                response.status,
            )


# --------------------------------------------------------------------------- #
# Request parsing helpers
# --------------------------------------------------------------------------- #

def _header(headers, name):
    """Case-insensitive header lookup returning the first match or ``None``."""
    if not isinstance(headers, dict) or not isinstance(name, str):
        return None
    lowered = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == lowered:
            return value
    return None


def _without_csrf_header(headers):
    """Return ``headers`` with any ``X-CSRF-Token`` entry removed.

    The CSRF token is a per-session secret, so it is excluded from logs by name
    rather than relying on :data:`admin_auth.SENSITIVE_HEADERS`. The shape of
    ``headers`` (mapping or ``(name, value)`` pair sequence) is preserved.
    """
    if isinstance(headers, dict):
        return {
            name: value for name, value in headers.items()
            if not (isinstance(name, str) and name.lower() == 'x-csrf-token')
        }
    if isinstance(headers, (list, tuple)):
        return type(headers)(
            item for item in headers
            if not (
                isinstance(item, (list, tuple)) and len(item) == 2
                and isinstance(item[0], str) and item[0].lower() == 'x-csrf-token'
            )
        )
    return headers


def _session_token(request):
    """Return the session token from the Cookie header, or ``''``."""
    raw = _header(request.headers, 'Cookie')
    if not isinstance(raw, str) or not raw:
        return ''
    cookie = http.cookies.SimpleCookie()
    try:
        cookie.load(raw)
    except http.cookies.CookieError:
        return ''
    morsel = cookie.get(admin_auth.SESSION_COOKIE_NAME)
    return morsel.value if morsel is not None else ''


def _csrf_token(request, body_data):
    """Return the CSRF token from ``X-CSRF-Token`` or the parsed body."""
    value = _header(request.headers, 'X-CSRF-Token')
    if isinstance(value, str) and value:
        return value
    value = body_data.get('csrf') if isinstance(body_data, dict) else None
    return value if isinstance(value, str) else ''


def _parse_body(request):
    """Parse a request body as JSON or form-encoded data; always a dict."""
    body = request.body
    if isinstance(body, bytes):
        try:
            text = body.decode('utf-8')
        except UnicodeDecodeError:
            text = body.decode('utf-8', 'replace')
    elif isinstance(body, str):
        text = body
    else:
        return {}
    if not text.strip():
        return {}

    content_type = _header(request.headers, 'Content-Type') or ''
    stripped = text.lstrip()
    if 'json' in content_type.lower() or stripped[:1] in ('{', '['):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return data
        if data is not None:
            return {}
    try:
        parsed = urllib.parse.parse_qs(text, keep_blank_values=True)
    except ValueError:
        return {}
    return {key: (values[0] if len(values) == 1 else values) for key, values in parsed.items()}


# --------------------------------------------------------------------------- #
# Status sub-views (defensive; injected callables must never crash a request)
# --------------------------------------------------------------------------- #

def _hotspot_view(controller):
    """Best-effort hotspot status view (never raises, never carries a secret)."""
    try:
        result = controller.status()
    except Exception:  # noqa: BLE001
        return {'ok': False, 'reason': 'hotspot status unavailable'}
    return {
        'ok': bool(getattr(result, 'ok', False)),
        'active': bool(getattr(result, 'active', False)),
        'ssid': getattr(result, 'ssid', ''),
        'address': getattr(result, 'address', ''),
        'reason': getattr(result, 'reason', ''),
    }


def _probe_view(probe):
    """Best-effort camera probe view (never raises)."""
    try:
        result = probe()
    except Exception:  # noqa: BLE001
        return {'ok': False, 'reason': 'camera probe unavailable'}
    return {
        'ok': bool(getattr(result, 'ok', False)),
        'reason': getattr(result, 'reason', ''),
        'sensors': getattr(result, 'sensors', 0),
    }


def _ssh_view(runner):
    """Best-effort SSH enabled view (never raises)."""
    try:
        result = ssh_control.ssh_enabled(runner)
    except Exception:  # noqa: BLE001
        return {'ok': False, 'enabled': None}
    return {'ok': bool(getattr(result, 'ok', False)), 'enabled': bool(getattr(result, 'enabled', False))}


__all__ = [
    'AdminApp',
    'Request',
    'Response',
    'Route',
    'TRUSTED_LAN_INTERFACES',
    'TRUSTED_LAN_NOTICE',
    'REAUTH_WINDOW_SECONDS',
    'is_trusted_lan',
    'lan_warning',
]
