"""Thin aiohttp transport for the admin/provisioning UI (WP-3e2, AC-17/AC-19/AC-20).

This is the *only* place the admin surface touches a real socket. It is a thin
adapter over the already-accepted stdlib core in :mod:`admin_http`: it builds an
``aiohttp`` application whose routes map 1:1 onto :class:`admin_http.AdminApp`
routes, converts an incoming request into an :class:`admin_http.Request`, calls
:meth:`admin_http.AdminApp.handle`, and writes the returned
:class:`admin_http.Response` back.

Peer identity
-------------
``peer_ip`` is taken from the TCP transport's socket peer
(``request.transport.get_extra_info('peername')``, falling back to
``request.remote``). Forwarding headers are deliberately ignored: ``X-Forwarded-For``
and ``X-Real-IP`` are never read, so a client cannot spoof its address to evade
the login/re-auth rate limiter or the trusted-LAN labelling. This appliance is
not deployed behind a reverse proxy.

Bind port by mode
-----------------
The pre-claim captive portal binds TCP ``80`` in ``setup`` mode
(``http://192.168.4.1``); the post-claim administration UI binds TCP ``443`` in
``admin`` mode and is reached at ``https://buddy3d-<device-id>.local`` with a
device-generated self-signed certificate (a browser warning is acceptable and
documented in the source plan §4.5). The bind host/port and TLS context are all
injectable. TLS is optional: when ``ADMIN_TLS_CERT``/``ADMIN_TLS_KEY`` are unset
the server runs plain HTTP on the selected port.

Secret hygiene
--------------
This module logs no request body and installs no aiohttp access log (which would
print query strings); the core redacts every response body and every logged
header mapping. Configuration is read through the existing modules
(:mod:`provisioning`, :mod:`privileged`); no secret value is read here.

Stdlib-only tests parse this file with :mod:`ast` and never import it, so the
top-level ``aiohttp`` import is intentional and expected.
"""
import argparse
import logging
import os
import ssl

from aiohttp import web

import admin_http
import camera_probe
import privileged
import provisioning

log = logging.getLogger('prusa-cam.admin_app')

#: The captive-portal bind port while the device is unclaimed (setup mode).
DEFAULT_SETUP_PORT = 80

#: The post-claim administration bind port (admin mode, HTTPS).
DEFAULT_ADMIN_PORT = 443

#: Default bind host. The plan requires the unclaimed wizard to stay off the
#: normal LAN interface; the transport is bound to a specific address only when
#: the caller (setup hotspot / systemd) injects one.
DEFAULT_BIND_HOST = '0.0.0.0'

#: Valid application modes.
VALID_MODES = ('setup', 'admin')

#: aiohttp route table: ``(method, path)`` pairs that map 1:1 onto
#: :class:`admin_http.AdminApp` routes (same method and equivalent path shape).
#: ``/setup/step/{n}`` is the aiohttp form of ``admin_http``'s
#: ``^/setup/step/(?P<n>[^/]+)$``. Keep this in lock-step with
#: :meth:`admin_http.AdminApp._build_routes`.
ROUTES = (
    ('GET', '/'),
    ('GET', '/admin'),
    ('GET', '/setup'),
    ('POST', '/setup/step/{n}'),
    ('POST', '/setup/finish'),
    ('GET', '/api/status'),
    ('POST', '/api/login'),
    ('POST', '/api/logout'),
    ('POST', '/api/reauth'),
    ('GET', '/api/config'),
    ('PUT', '/api/config'),
    ('POST', '/api/ssh'),
    ('POST', '/api/recovery/enter-setup'),
    ('POST', '/api/reset/begin'),
    ('POST', '/api/reset/confirm'),
    ('POST', '/api/reset/execute'),
)

#: Provisioning states at/after which the admin UI (not the wizard) is served.
_CLAIMED_STATES = frozenset({'claimed', 'configured', 'running'})


# --------------------------------------------------------------------------- #
# Request / response translation
# --------------------------------------------------------------------------- #

def _peer_ip(request):
    """Return the socket peer address, never a forwarding header.

    The transport's ``peername`` is authoritative. ``request.remote`` is only a
    fallback for a transport without a peer name; it is still aiohttp's own
    socket-derived value, not a client header.
    """
    transport = getattr(request, 'transport', None)
    if transport is not None:
        try:
            peername = transport.get_extra_info('peername')
        except Exception:  # noqa: BLE001 - a closed transport must not crash a request
            peername = None
        if isinstance(peername, (tuple, list)) and peername:
            return str(peername[0])
        if isinstance(peername, str) and peername:
            return peername
    remote = getattr(request, 'remote', None)
    return remote if isinstance(remote, str) else ''


def _query_dict(request):
    """Return the parsed query string as a plain ``dict``."""
    try:
        return dict(request.query)
    except Exception:  # noqa: BLE001 - a malformed query must not crash a request
        return {}


def _header_dict(request):
    """Return the request headers as a plain ``dict`` (core does its own lookup)."""
    try:
        return dict(request.headers)
    except Exception:  # noqa: BLE001
        return {}


def _to_request(request, body):
    """Translate an aiohttp request into an :class:`admin_http.Request`."""
    return admin_http.Request(
        method=request.method,
        path=request.path,
        query=_query_dict(request),
        headers=_header_dict(request),
        body=body,
        peer_ip=_peer_ip(request),
    )


def _to_response(response: admin_http.Response) -> web.Response:
    """Translate an :class:`admin_http.Response` into an aiohttp response.

    ``Set-Cookie`` (and every other core header) is passed through verbatim, so
    the session cookie minted by the core is preserved unchanged.
    """
    return web.Response(
        status=response.status,
        headers=dict(response.headers),
        body=response.body,
    )


async def _handle(request):
    """Dispatch one aiohttp request through the stdlib admin core."""
    app = request.app['admin_app']
    body = await request.read()
    core_response = app.handle(_to_request(request, body))
    return _to_response(core_response)


# --------------------------------------------------------------------------- #
# Application factory
# --------------------------------------------------------------------------- #

def create_app(admin_app: admin_http.AdminApp) -> web.Application:
    """Build the aiohttp application bound to an :class:`admin_http.AdminApp`."""
    app = web.Application()
    app['admin_app'] = admin_app
    for method, path in ROUTES:
        app.router.add_route(method, path, _handle)
    # Anything not registered above still goes through the core, so unknown
    # paths/methods get the core's own 404 (and its redaction) instead of an
    # aiohttp-generated page.
    app.router.add_route('*', '/{tail:.*}', _handle)
    return app


# --------------------------------------------------------------------------- #
# Mode / port / TLS selection
# --------------------------------------------------------------------------- #

def default_port(mode):
    """Return the documented bind port for ``mode`` (80 setup, 443 admin)."""
    return DEFAULT_SETUP_PORT if mode == 'setup' else DEFAULT_ADMIN_PORT


def resolve_mode(requested=None):
    """Resolve the application mode from an explicit value or provisioning state.

    An explicit valid mode always wins. Otherwise the device is ``admin`` once
    the persisted provisioning state is at/after ``claimed`` and ``setup``
    before that, so one entry point serves the correct surface across the claim
    transition.
    """
    if requested in VALID_MODES:
        return requested
    if requested not in (None, ''):
        log.warning('admin_app: ignoring invalid mode %r', requested)
    try:
        state = provisioning.ProvisioningState.load()
    except Exception:  # noqa: BLE001 - never fail startup on a bad state file
        return 'setup'
    return 'admin' if getattr(state, 'state', '') in _CLAIMED_STATES else 'setup'


def _configured_device_id(device_path=None):
    """Derive the stable device id for the setup SSID / admin hostname, or ``''``.

    Delegates to :func:`provisioning.resolve_device_id`, which prefers the
    configured ``device.toml`` fingerprint and otherwise falls back to the raw
    ``wlan0`` MAC or the persisted random seed. That fallback is what gives an
    unclaimed device (no ``device.toml`` yet) a stable identity for the setup
    hotspot. A missing/unreadable document yields ``''`` so the wizard degrades
    gracefully rather than crashing startup.
    """
    return provisioning.resolve_device_id(device_path)


def build_admin_app(mode, *, device_path=None, secrets_path=None,
                    provisioning_path=None, hotspot_controller=None, probe=None,
                    start_camera=None, activate_station=None):
    """Build the stdlib :class:`admin_http.AdminApp` with real dependencies.

    Paths default to the durable ``/data`` locations through the core's own
    defaults; the admin password hash is read lazily by the core from
    ``secrets.toml`` (never here, so no secret is handled in the transport).

    The wizard's finish path needs root-only actions, so the defaults route
    through the fixed-verb privileged helper (WP-R1/B2):

    * ``start_camera`` defaults to :func:`privileged.start_camera`, which starts
      ``prusa-camera.target`` through ``prusa-priv start-camera``; the
      ``Conflicts=`` edges then stop ``prusa-provisioning.service`` and the
      camera target pulls ``prusa-admin.service`` (AC-12).
    * ``activate_station`` defaults to :func:`privileged.activate_station`, which
      creates/activates the Wi-Fi station profile before the camera starts so a
      claimed device comes up online.
    * the default hotspot controller is
      :class:`privileged.PrivilegedHotspot`: its ``start``/``stop`` need root,
      while ``status``/``is_active`` stay read-only :mod:`hotspot` queries.
    """
    try:
        state = provisioning.ProvisioningState.load(
            provisioning_path or provisioning.PROVISIONING_PATH)
    except Exception:  # noqa: BLE001
        state = None
    return admin_http.AdminApp(
        mode=mode,
        provisioning_state=state,
        device_id=_configured_device_id(device_path),
        hotspot=(
            hotspot_controller if hotspot_controller is not None
            else privileged.PrivilegedHotspot()
        ),
        probe=probe if probe is not None else camera_probe.probe,
        start_camera=(
            start_camera if start_camera is not None else privileged.start_camera
        ),
        activate_station=(
            activate_station if activate_station is not None
            else privileged.activate_station
        ),
        device_path=device_path,
        secrets_path=secrets_path,
        provisioning_path=provisioning_path,
    )


def _ssl_context_from_env(env=None):
    """Return a server SSL context from ``ADMIN_TLS_CERT``/``ADMIN_TLS_KEY``.

    Returns ``None`` (plain HTTP) when either path is unset. A certificate and
    key are never logged.
    """
    env = os.environ if env is None else env
    cert = env.get('ADMIN_TLS_CERT')
    key = env.get('ADMIN_TLS_KEY')
    if not cert or not key:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return context


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def run(mode=None, *, host=None, port=None, ssl_context=None, admin_app=None):
    """Serve the admin/provisioning UI; blocks until shutdown.

    ``mode``/``host``/``port``/``ssl_context`` are all injectable for embedding
    and hardware bring-up. When omitted, mode is resolved from the persisted
    provisioning state, host from ``ADMIN_HOST`` (default :data:`DEFAULT_BIND_HOST`),
    port from ``ADMIN_PORT`` (default :func:`default_port`), and TLS from
    ``ADMIN_TLS_CERT``/``ADMIN_TLS_KEY``.
    """
    resolved = resolve_mode(mode)
    if admin_app is None:
        admin_app = build_admin_app(resolved)
    if host is None:
        host = os.environ.get('ADMIN_HOST') or DEFAULT_BIND_HOST
    if port is None:
        port = _port_from_env(resolved)
    if ssl_context is None:
        ssl_context = _ssl_context_from_env()
    scheme = 'https' if ssl_context is not None else 'http'
    if resolved == 'admin' and ssl_context is None:
        log.warning(
            'admin_app: admin mode serving WITHOUT TLS; the Secure session cookie '
            'will not work over http — configure ADMIN_TLS_CERT/ADMIN_TLS_KEY')
    log.info('admin_app: %s UI listening on %s://%s:%s', resolved, scheme, host, port)
    # No aiohttp access log: the core already logs a redacted request line, and
    # the default access log would print query strings.
    web.run_app(
        create_app(admin_app),
        host=host,
        port=port,
        ssl_context=ssl_context,
        access_log=None,
        print=None,
    )
    return 0


def _port_from_env(mode, env=None):
    """Return the configured bind port, or the mode default."""
    env = os.environ if env is None else env
    raw = env.get('ADMIN_PORT')
    if raw:
        try:
            port = int(raw)
        except (TypeError, ValueError):
            log.warning('admin_app: ignoring invalid ADMIN_PORT')
        else:
            if 0 < port < 65536:
                return port
            log.warning('admin_app: ignoring out-of-range ADMIN_PORT')
    return default_port(mode)


def main(argv=None):
    """Command-line entry point used by ``prusa-admin.service``.

    ``ADMIN_MODE`` (or ``--mode``) selects ``setup``/``admin``; when neither is
    given the mode is resolved from the persisted provisioning state. This keeps
    one unit correct across the claim transition.
    """
    logging.basicConfig(
        level=os.environ.get('ADMIN_LOG_LEVEL', 'INFO').upper(),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    parser = argparse.ArgumentParser(description='Buddy3D admin/provisioning UI')
    parser.add_argument('--mode', choices=VALID_MODES, default=None)
    parser.add_argument('--host', default=None)
    parser.add_argument('--port', type=int, default=None)
    args = parser.parse_args(argv)
    mode = args.mode or os.environ.get('ADMIN_MODE')
    return run(mode=mode, host=args.host, port=args.port)


if __name__ == '__main__':
    raise SystemExit(main())
