"""WP-3e1 AC-17/AC-19/AC-20: admin/provisioning HTTP core (admin_http).

Stdlib-only. No ``aiohttp``/``socketio``/``gi``/GStreamer import; every path is a
``tempfile`` path, the SSH runner and hotspot are fakes, and time is injected via
``Request.now`` so nothing sleeps. No real network, subprocess, or ``/data``.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import admin_auth  # noqa: E402
import admin_http  # noqa: E402
import config_schema  # noqa: E402
import factory_reset  # noqa: E402
import provisioning  # noqa: E402
import setup_wizard  # noqa: E402

ADMIN_PASSWORD = 'correct horse battery staple'
ADMIN_HASH = admin_auth.hash_password(ADMIN_PASSWORD)
SESSION_COOKIE = admin_auth.SESSION_COOKIE_NAME
NOW = 1000.0


def _make_request(method, path, body=None, headers=None, peer_ip='10.0.0.5', now=NOW):
    """Build a transport-neutral :class:`admin_http.Request`."""
    if isinstance(body, (dict, list)):
        raw = json.dumps(body).encode('utf-8')
        merged = {'Content-Type': 'application/json'}
    elif isinstance(body, str):
        raw = body.encode('utf-8')
        merged = {}
    elif isinstance(body, bytes):
        raw = body
        merged = {}
    else:
        raw = b''
        merged = {}
    merged.update(headers or {})
    return admin_http.Request(method, path, {}, merged, raw, peer_ip, now)


def _parse_cookie(response):
    """Return the session cookie value from a response, or ``''``."""
    raw = response.headers.get('Set-Cookie', '')
    for part in raw.split(';'):
        name, _, value = part.strip().partition('=')
        if name == SESSION_COOKIE:
            return value
    return ''


class FakeRunner:
    """Records ``systemctl`` argv; never runs a process."""

    def __init__(self, returncode=0, stdout=''):
        self.returncode = returncode
        self.stdout = stdout
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(
            list(args), self.returncode, stdout=self.stdout, stderr=''
        )


class FakeHotspot:
    """Reports an active hotspot without touching NetworkManager."""

    def status(self):
        return SimpleNamespace(
            ok=True, active=True, ssid='Buddy3D-Setup-abc123',
            address='192.168.4.1', reason='',
        )

    def start(self, *args, **kwargs):
        return SimpleNamespace(ok=True, active=True, reason='')

    def stop(self, *args, **kwargs):
        return SimpleNamespace(ok=True, active=False, reason='')


class AdminHttpTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.sessions = admin_auth.SessionStore()
        self.limiter = admin_auth.LoginRateLimiter(
            max_attempts=3, window=300.0, lockout=60.0
        )
        self.ssh_runner = FakeRunner()
        self.hotspot = FakeHotspot()
        self.app = self._build_app()

    def _build_app(self, **overrides):
        kwargs = dict(
            mode='admin',
            sessions=self.sessions,
            limiter=self.limiter,
            admin_hash=ADMIN_HASH,
            provisioning_state=provisioning.ProvisioningState(state='claimed'),
            device_path=str(self.root / 'device.toml'),
            secrets_path=str(self.root / 'secrets.toml'),
            provisioning_path=str(self.root / 'provisioning.json'),
            recovery_path=str(self.root / 'buddy3d-recovery'),
            ssh_runner=self.ssh_runner,
            clock=lambda: NOW,
        )
        kwargs.update(overrides)
        return admin_http.AdminApp(**kwargs)

    def req(self, method, path, body=None, headers=None, peer_ip='10.0.0.5', now=NOW):
        return _make_request(method, path, body, headers, peer_ip, now)

    def login(self, app=None, peer_ip='10.0.0.5', password=ADMIN_PASSWORD, now=NOW):
        app = app or self.app
        response = app.handle(self.req(
            'POST', '/api/login', body={'password': password},
            peer_ip=peer_ip, now=now,
        ))
        self.assertEqual(response.status, 200, response.body)
        return _parse_cookie(response)

    def auth_headers(self, token, csrf=None, extra=None):
        headers = {'Cookie': f'{SESSION_COOKIE}={token}'}
        if csrf is not None:
            headers['X-CSRF-Token'] = csrf
        headers.update(extra or {})
        return headers


# --------------------------------------------------------------------------- #
# Trusted-LAN labelling
# --------------------------------------------------------------------------- #

class TrustedLanTests(unittest.TestCase):
    def test_private_link_local_loopback_are_trusted(self):
        for peer_ip in (
            '10.0.0.1', '172.16.5.4', '172.31.255.254', '192.168.1.10',
            '169.254.3.4', '127.0.0.1', '::1', 'fe80::1',
        ):
            with self.subTest(peer_ip=peer_ip):
                self.assertTrue(admin_http.is_trusted_lan(peer_ip))

    def test_public_and_invalid_are_not_trusted(self):
        for peer_ip in (
            '8.8.8.8', '172.32.0.1', '192.169.0.1', '2001:db8::1',
            '', '   ', 'not-an-ip', None, 123,
        ):
            with self.subTest(peer_ip=peer_ip):
                self.assertFalse(admin_http.is_trusted_lan(peer_ip))

    def test_surrounding_whitespace_and_control_chars_rejected(self):
        for peer_ip in (
            ' 10.0.0.1', '10.0.0.1 ', '\t10.0.0.1', '10.0.0.1\n',
            '\x0010.0.0.1', '10.0.0.1\x00', '\u200b10.0.0.1',
        ):
            with self.subTest(peer_ip=peer_ip):
                self.assertFalse(admin_http.is_trusted_lan(peer_ip))

    def test_lan_warning_names_the_unauthenticated_interfaces(self):
        text = admin_http.lan_warning()
        self.assertIn('trusted lan', text.lower())
        self.assertIn('port-forward', text.lower())
        for marker in ('ONVIF', '/snapshot.jpg', '8554', '8555'):
            self.assertIn(marker, text)


# --------------------------------------------------------------------------- #
# Routing and mode transition
# --------------------------------------------------------------------------- #

class RoutingTests(AdminHttpTestBase):
    def test_known_routes_dispatch(self):
        self.assertEqual(self.app.handle(self.req('GET', '/api/status')).status, 200)
        self.assertEqual(self.app.handle(self.req('GET', '/admin')).status, 200)
        login = self.app.handle(self.req(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD}
        ))
        self.assertEqual(login.status, 200)

    def test_unknown_route_404(self):
        self.assertEqual(self.app.handle(self.req('GET', '/nope')).status, 404)

    def test_setup_routes_unavailable_in_admin_mode(self):
        self.assertEqual(self.app.handle(self.req('GET', '/setup')).status, 302)
        self.assertEqual(
            self.app.handle(self.req('POST', '/setup/step/1', body={})).status, 409
        )
        self.assertEqual(
            self.app.handle(self.req('POST', '/setup/finish', body={})).status, 409
        )

    def test_root_redirects_by_mode(self):
        admin_root = self.app.handle(self.req('GET', '/'))
        self.assertEqual(admin_root.status, 302)
        self.assertEqual(admin_root.headers['Location'], '/admin')

        setup_app = self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
        )
        setup_root = setup_app.handle(self.req('GET', '/'))
        self.assertEqual(setup_root.status, 302)
        self.assertEqual(setup_root.headers['Location'], '/setup')

    def test_setup_routes_available_while_unclaimed(self):
        setup_app = self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
        )
        self.assertEqual(setup_app.handle(self.req('GET', '/setup')).status, 200)

    def test_persisted_claimed_state_keeps_setup_open_for_finish(self):
        # ``persist`` writes a valid device + admin password and advances to
        # ``claimed`` (camera validated) while the runtime has not started: the
        # finish step must stay reachable to stop the AP and start the camera.
        path = self.root / 'provisioning.json'
        path.write_text(json.dumps({'state': 'claimed'}), encoding='utf-8')
        setup_app = self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
            provisioning_path=str(path),
        )
        self.assertEqual(setup_app.handle(self.req('GET', '/setup')).status, 200)
        self.assertNotEqual(
            setup_app.handle(self.req('POST', '/setup/finish', body={})).status, 409
        )

    def test_persisted_running_state_closes_setup_routes(self):
        path = self.root / 'provisioning.json'
        path.write_text(json.dumps({'state': 'running'}), encoding='utf-8')
        setup_app = self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='running'),
            provisioning_path=str(path),
        )
        redirect = setup_app.handle(self.req('GET', '/setup'))
        self.assertEqual(redirect.status, 302)
        self.assertEqual(redirect.headers['Location'], '/admin')
        self.assertEqual(
            setup_app.handle(self.req('POST', '/setup/step/1', body={})).status, 409
        )
        self.assertEqual(
            setup_app.handle(self.req('POST', '/setup/finish', body={})).status, 409
        )

    def test_missing_provisioning_file_falls_back_to_injected_state(self):
        # No state file exists, so the injected snapshot is authoritative. A
        # running snapshot must therefore close the portal (fail closed).
        setup_app = self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='running'),
            provisioning_path=str(self.root / 'absent-provisioning.json'),
        )
        redirect = setup_app.handle(self.req('GET', '/setup'))
        self.assertEqual(redirect.status, 302)
        self.assertEqual(redirect.headers['Location'], '/admin')
        self.assertEqual(
            setup_app.handle(self.req('POST', '/setup/step/1', body={})).status, 409
        )
        self.assertEqual(
            setup_app.handle(self.req('POST', '/setup/finish', body={})).status, 409
        )

    def test_empty_provisioning_path_falls_back_to_injected_state(self):
        setup_app = self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='running'),
            provisioning_path='',
        )
        self.assertEqual(setup_app.handle(self.req('GET', '/setup')).status, 302)


class MqttTestRouteTests(AdminHttpTestBase):
    """WP-R2 (AC-23 tail): the broker-test route and its secret hygiene."""

    URI = 'mqtts://broker.example:8883'
    MQTT_PASSWORD = 'mqtt-route-secret'

    def _setup_app(self, probe):
        return self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
            mqtt_probe=probe,
        )

    def _body(self):
        return {
            'uri': self.URI,
            'username': 'mqttuser',
            'password': self.MQTT_PASSWORD,
            'ca_file': '/etc/ssl/certs/ca.pem',
        }

    def test_unavailable_when_no_probe_injected(self):
        # Setup mode keeps the route public (the wizard calls it before saving).
        response = self._setup_app(None).handle(self.req(
            'POST', '/api/mqtt/test', body=self._body()))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['reason'], 'mqtt test unavailable')

    def test_setup_mode_public_success(self):
        seen = []

        def probe(config):
            seen.append(config)
            return True, 'connected'

        response = self._setup_app(probe).handle(self.req(
            'POST', '/api/mqtt/test', body=self._body()))
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(response.body)['ok'])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].uri, self.URI)
        self.assertEqual(seen[0].username, 'mqttuser')
        self.assertEqual(seen[0].password, self.MQTT_PASSWORD)

    def test_failure_reason_is_redacted(self):
        def probe(config):
            return False, f'bad credentials {self.MQTT_PASSWORD}'

        response = self._setup_app(probe).handle(self.req(
            'POST', '/api/mqtt/test', body=self._body()))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertFalse(payload['ok'])
        self.assertNotIn(self.MQTT_PASSWORD, payload['reason'])
        self.assertNotIn(self.MQTT_PASSWORD, response.body.decode('utf-8'))

    def test_raising_probe_is_isolated(self):
        def probe(config):
            raise RuntimeError(self.MQTT_PASSWORD)

        response = self._setup_app(probe).handle(self.req(
            'POST', '/api/mqtt/test', body=self._body()))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertFalse(payload['ok'])
        self.assertNotIn(self.MQTT_PASSWORD, response.body.decode('utf-8'))

    def test_missing_uri_is_rejected(self):
        response = self._setup_app(lambda config: (True, 'connected')).handle(
            self.req('POST', '/api/mqtt/test', body={'username': 'u'}))
        self.assertEqual(response.status, 400)

    def test_oversized_body_is_rejected(self):
        body = b'x' * (admin_http.MAX_MQTT_TEST_BODY_BYTES + 1)
        response = self._setup_app(lambda config: (True, 'connected')).handle(
            self.req('POST', '/api/mqtt/test', body=body))
        self.assertEqual(response.status, 413)

    def test_post_claim_requires_authentication(self):
        response = self.app.handle(self.req(
            'POST', '/api/mqtt/test', body=self._body()))
        self.assertEqual(response.status, 401)

    def test_post_claim_authenticated_succeeds(self):
        app = self._build_app(mqtt_probe=lambda config: (True, 'connected'))
        token = self.login(app=app)
        csrf = self.sessions.csrf_for(token)
        response = app.handle(self.req(
            'POST', '/api/mqtt/test', body=self._body(),
            headers=self.auth_headers(token, csrf=csrf)))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertTrue(payload['ok'])


# --------------------------------------------------------------------------- #
# Corrupt provisioning file / authoritative claim gate (fail closed)
# --------------------------------------------------------------------------- #

class SetupFailClosedTests(AdminHttpTestBase):
    """A corrupt state file or a factual claim must close the public wizard.

    Before this change a corrupt ``provisioning.json`` loaded as ``factory``,
    which is a pre-claim state, so disk corruption reopened the unauthenticated
    setup wizard and let it rewrite ``device.toml``/``secrets.toml`` -- a device
    takeover without the admin password. These tests pin the fail-closed fix and
    the second, facts-based :func:`provisioning.is_claimed` gate.
    """

    def _write_state(self, state):
        (self.root / 'provisioning.json').write_text(
            json.dumps({'state': state}), encoding='utf-8'
        )

    def _write_claimable_config(self):
        (self.root / 'device.toml').write_text(
            config_schema.dumps_device(config_schema.default_device()),
            encoding='utf-8',
        )
        (self.root / 'secrets.toml').write_text(
            config_schema.dumps_secrets({'admin': {'password_hash': ADMIN_HASH}}),
            encoding='utf-8',
        )

    def _setup_app(self, injected_state):
        return self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state=injected_state),
        )

    def _status(self, app):
        response = app.handle(self.req('GET', '/api/status'))
        self.assertEqual(response.status, 200)
        return json.loads(response.body.decode('utf-8'))

    def _assert_setup_closed(self, app):
        redirect = app.handle(self.req('GET', '/setup'))
        self.assertEqual(redirect.status, 302)
        self.assertEqual(redirect.headers['Location'], '/admin')
        self.assertEqual(
            app.handle(self.req('POST', '/setup/step/1', body={})).status, 409
        )
        self.assertEqual(
            app.handle(self.req('POST', '/setup/finish', body={})).status, 409
        )

    def test_corrupt_state_file_closes_setup_despite_claimed_injection(self):
        (self.root / 'provisioning.json').write_text('{not json', encoding='utf-8')
        app = self._setup_app('claimed')
        self._assert_setup_closed(app)
        payload = self._status(app)
        self.assertFalse(payload['setup_available'])
        self.assertNotEqual(payload['provisioning_state'], 'unclaimed')
        self.assertEqual(payload['provisioning_source'], 'persisted_corrupt')
        self.assertTrue(payload['provisioning_error'])

    def test_corrupt_state_file_closes_setup_despite_unclaimed_injection(self):
        # An empty (truncated/partial-write) file is corrupt, not a fresh device.
        (self.root / 'provisioning.json').write_text('', encoding='utf-8')
        app = self._setup_app('unclaimed')
        self._assert_setup_closed(app)
        payload = self._status(app)
        self.assertFalse(payload['setup_available'])
        self.assertNotEqual(payload['provisioning_state'], 'unclaimed')

    def test_invalid_state_value_closes_setup(self):
        self._write_state('bogus')
        app = self._setup_app('unclaimed')
        self._assert_setup_closed(app)
        payload = self._status(app)
        self.assertFalse(payload['setup_available'])
        self.assertEqual(payload['provisioning_error'],
                         'provisioning state value is invalid')

    def test_missing_state_file_unclaimed_serves_setup(self):
        app = self._setup_app('unclaimed')
        self.assertEqual(app.handle(self.req('GET', '/setup')).status, 200)
        payload = self._status(app)
        self.assertTrue(payload['setup_available'])
        self.assertEqual(payload['provisioning_state'], 'unclaimed')
        self.assertEqual(payload['provisioning_source'], 'injected')

    def test_missing_state_file_running_refuses_setup(self):
        app = self._setup_app('running')
        self._assert_setup_closed(app)
        payload = self._status(app)
        self.assertFalse(payload['setup_available'])
        self.assertEqual(payload['provisioning_state'], 'running')

    def test_claimable_in_finish_window_keeps_setup_open(self):
        # The bug this pins: persist writes a valid device + admin password, so
        # the device is claimable by facts while the state is still pre-runtime
        # (storage_ready when the camera is not validated). ``finish`` must stay
        # callable, otherwise the hotspot is never stopped and the camera target
        # never starts.
        self._write_claimable_config()
        self._write_state('storage_ready')
        app = self._setup_app('storage_ready')
        self.assertEqual(app.handle(self.req('GET', '/setup')).status, 200)
        self.assertNotEqual(
            app.handle(self.req('POST', '/setup/finish', body={})).status, 409
        )
        payload = self._status(app)
        self.assertTrue(payload['setup_available'])
        self.assertEqual(payload['provisioning_state'], 'claimed')

    def test_claimable_by_facts_refuses_setup_despite_unclaimed_state(self):
        # The state file and the injected snapshot both say unclaimed, but the
        # authoritative predicate (admin hash + valid device) says claimed.
        self._write_claimable_config()
        self._write_state('unclaimed')
        app = self._setup_app('unclaimed')
        self._assert_setup_closed(app)
        payload = self._status(app)
        self.assertFalse(payload['setup_available'])
        self.assertEqual(payload['provisioning_state'], 'claimed')
        self.assertEqual(payload['provisioning_source'], 'persisted')

    def test_status_consistent_after_persisted_flip(self):
        self._write_state('unclaimed')
        app = self._setup_app('unclaimed')
        before = self._status(app)
        self.assertTrue(before['setup_available'])
        self.assertEqual(before['provisioning_state'], 'unclaimed')

        self._write_state('running')
        after = self._status(app)
        self.assertFalse(after['setup_available'])
        self.assertEqual(after['provisioning_state'], 'running')
        # The invariant the shared view exists to guarantee: once the runtime
        # owns the device, setup is closed.
        self.assertFalse(
            after['setup_available'] and after['provisioning_state'] == 'running'
        )


# --------------------------------------------------------------------------- #
# Session policy
# --------------------------------------------------------------------------- #

class AuthPolicyTests(AdminHttpTestBase):
    def test_protected_route_without_session_401(self):
        self.assertEqual(self.app.handle(self.req('GET', '/api/config')).status, 401)

    def test_invalid_session_401(self):
        response = self.app.handle(self.req(
            'GET', '/api/config',
            headers={'Cookie': f'{SESSION_COOKIE}=forged-token'},
        ))
        self.assertEqual(response.status, 401)

    def test_expired_session_401(self):
        token = self.sessions.create(now=0.0, idle_ttl=1.0)
        response = self.app.handle(self.req(
            'GET', '/api/config', headers=self.auth_headers(token), now=10.0,
        ))
        self.assertEqual(response.status, 401)

    def test_valid_session_200(self):
        token = self.login()
        response = self.app.handle(self.req(
            'GET', '/api/config', headers=self.auth_headers(token),
        ))
        self.assertEqual(response.status, 200)


# --------------------------------------------------------------------------- #
# CSRF policy
# --------------------------------------------------------------------------- #

class CsrfTests(AdminHttpTestBase):
    def test_post_without_csrf_403(self):
        token = self.login()
        response = self.app.handle(self.req(
            'POST', '/api/logout', headers=self.auth_headers(token),
        ))
        self.assertEqual(response.status, 403)

    def test_post_with_bad_csrf_403(self):
        token = self.login()
        response = self.app.handle(self.req(
            'POST', '/api/logout', headers=self.auth_headers(token, csrf='not-the-token'),
        ))
        self.assertEqual(response.status, 403)

    def test_post_with_valid_csrf_proceeds(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        response = self.app.handle(self.req(
            'POST', '/api/logout', headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 200)
        self.assertIn('Max-Age=0', response.headers.get('Set-Cookie', ''))

    def test_csrf_token_is_bound_to_its_session(self):
        token_a = self.login(peer_ip='10.0.0.5')
        token_b = self.login(peer_ip='10.0.0.6')
        csrf_a = self.sessions.csrf_for(token_a)
        self.assertTrue(csrf_a)
        response = self.app.handle(self.req(
            'POST', '/api/logout',
            headers=self.auth_headers(token_b, csrf=csrf_a),
        ))
        self.assertEqual(response.status, 403)
        # Session B remains live: the CSRF failure did not revoke it.
        self.assertIsNotNone(self.sessions.csrf_for(token_b))


# --------------------------------------------------------------------------- #
# Session cookie hardening
# --------------------------------------------------------------------------- #

class CookieTests(AdminHttpTestBase):
    def test_admin_mode_cookie_is_secure_httponly_samesite_lax(self):
        response = self.app.handle(self.req(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD},
        ))
        self.assertEqual(response.status, 200)
        cookie = response.headers.get('Set-Cookie', '')
        self.assertIn('Secure', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)

    def test_setup_mode_cookie_omits_secure_but_keeps_httponly(self):
        app = self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
        )
        response = app.handle(self.req(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD},
        ))
        self.assertEqual(response.status, 200)
        cookie = response.headers.get('Set-Cookie', '')
        self.assertNotIn('Secure', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)


# --------------------------------------------------------------------------- #
# Setup-mode login
# --------------------------------------------------------------------------- #

class SetupLoginTests(AdminHttpTestBase):
    def test_login_works_in_setup_mode_with_admin_hash(self):
        app = self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
        )
        response = app.handle(self.req(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD},
        ))
        self.assertEqual(response.status, 200)

    def test_login_fails_closed_without_admin_hash(self):
        app = self._build_app(
            mode='setup',
            admin_hash=None,
            secrets_path=str(self.root / 'missing-secrets.toml'),
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
        )
        response = app.handle(self.req(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD},
        ))
        self.assertEqual(response.status, 401)


# --------------------------------------------------------------------------- #
# Login rate limiting (keyed on the socket peer, never a header)
# --------------------------------------------------------------------------- #

class RateLimitTests(AdminHttpTestBase):
    def test_failures_lock_the_peer_ip(self):
        for _ in range(3):
            response = self.app.handle(self.req(
                'POST', '/api/login', body={'password': 'wrong'},
                peer_ip='10.0.0.5',
            ))
            self.assertEqual(response.status, 401)
        locked = self.app.handle(self.req(
            'POST', '/api/login', body={'password': 'wrong'}, peer_ip='10.0.0.5',
        ))
        self.assertEqual(locked.status, 429)
        self.assertIn('Retry-After', locked.headers)
        self.assertGreaterEqual(int(locked.headers['Retry-After']), 1)

    def test_a_different_peer_is_unaffected(self):
        for _ in range(3):
            self.app.handle(self.req(
                'POST', '/api/login', body={'password': 'wrong'}, peer_ip='10.0.0.5',
            ))
        response = self.app.handle(self.req(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD}, peer_ip='10.0.0.6',
        ))
        self.assertEqual(response.status, 200)

    def test_forwarded_for_header_does_not_change_the_key(self):
        for index in range(3):
            self.app.handle(self.req(
                'POST', '/api/login', body={'password': 'wrong'},
                headers={'X-Forwarded-For': f'1.2.3.{index}'}, peer_ip='10.0.0.5',
            ))
        same_peer = self.app.handle(self.req(
            'POST', '/api/login', body={'password': 'wrong'},
            headers={'X-Forwarded-For': '9.9.9.9'}, peer_ip='10.0.0.5',
        ))
        self.assertEqual(same_peer.status, 429)

        other_peer = self.app.handle(self.req(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD},
            headers={'X-Forwarded-For': '1.2.3.0'}, peer_ip='10.0.0.6',
        ))
        self.assertEqual(other_peer.status, 200)


# --------------------------------------------------------------------------- #
# Re-authentication policy
# --------------------------------------------------------------------------- #

class ReauthTests(AdminHttpTestBase):
    def test_reauth_route_without_password_403(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        response = self.app.handle(self.req(
            'POST', '/api/ssh', body={'enabled': True},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 403)

    def test_reauth_with_correct_password_proceeds(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        response = self.app.handle(self.req(
            'POST', '/api/ssh', body={'enabled': True, 'password': ADMIN_PASSWORD},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 200)
        self.assertTrue(response.headers.get('Content-Type', '').startswith('application/json'))
        self.assertTrue(self.ssh_runner.calls)

    def test_reauth_window_expires(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        primed = self.app.handle(self.req(
            'POST', '/api/ssh', body={'enabled': True, 'password': ADMIN_PASSWORD},
            headers=self.auth_headers(token, csrf=csrf), now=NOW,
        ))
        self.assertEqual(primed.status, 200)

        later = NOW + admin_http.REAUTH_WINDOW_SECONDS + 1.0
        expired = self.app.handle(self.req(
            'POST', '/api/ssh', body={'enabled': False},
            headers=self.auth_headers(token, csrf=csrf), now=later,
        ))
        self.assertEqual(expired.status, 403)

    def test_reauth_is_rate_limited(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        for _ in range(3):
            response = self.app.handle(self.req(
                'POST', '/api/reauth', body={'password': 'wrong'},
                headers=self.auth_headers(token, csrf=csrf),
            ))
            self.assertEqual(response.status, 403)
        locked = self.app.handle(self.req(
            'POST', '/api/reauth', body={'password': 'wrong'},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(locked.status, 429)
        self.assertIn('Retry-After', locked.headers)

    def test_forged_session_cannot_reach_a_reauth_handler(self):
        response = self.app.handle(self.req(
            'POST', '/api/reauth', body={'password': ADMIN_PASSWORD},
            headers={'Cookie': f'{SESSION_COOKIE}=forged-token'},
        ))
        self.assertEqual(response.status, 401)

    def test_recovery_route_requires_reauth(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        response = self.app.handle(self.req(
            'POST', '/api/recovery/enter-setup', body={},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 403)

    def test_recovery_with_reauth_writes_the_sentinel(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        response = self.app.handle(self.req(
            'POST', '/api/recovery/enter-setup',
            body={'password': ADMIN_PASSWORD, 'reason': 'operator'},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 200)
        self.assertTrue((self.root / 'buddy3d-recovery').exists())


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

class RedactionTests(AdminHttpTestBase):
    def test_login_response_contains_no_secret(self):
        response = self.app.handle(self.req(
            'POST', '/api/login', body={'password': ADMIN_PASSWORD},
        ))
        body = response.body.decode('utf-8')
        self.assertNotIn(ADMIN_PASSWORD, body)
        self.assertNotIn(ADMIN_HASH, body)
        token = _parse_cookie(response)
        self.assertTrue(token)
        self.assertNotIn(token, body)

    def test_config_response_redacts_secrets(self):
        (self.root / 'device.toml').write_text(
            config_schema.dumps_device(config_schema.default_device())
        )
        (self.root / 'secrets.toml').write_text(config_schema.dumps_secrets({
            'prusa': {'token': 'tok-abc-123'},
            'wifi': {'psk': 'sup3rsecret'},
            'admin': {'password_hash': ADMIN_HASH},
        }))
        token = self.login()
        response = self.app.handle(self.req(
            'GET', '/api/config', headers=self.auth_headers(token),
        ))
        self.assertEqual(response.status, 200)
        body = response.body.decode('utf-8')
        for secret in ('tok-abc-123', 'sup3rsecret', ADMIN_HASH):
            self.assertNotIn(secret, body)
        self.assertIn(admin_auth.REDACTED, body)

    def test_logs_contain_no_secret(self):
        with self.assertLogs('prusa-cam.admin_http', level='DEBUG') as captured:
            self.app.handle(self.req(
                'GET', '/api/status',
                headers={'Authorization': 'Bearer topsecret-credential'},
            ))
        joined = '\n'.join(captured.output)
        self.assertNotIn('topsecret-credential', joined)

    def test_csrf_header_absent_from_logs(self):
        token = self.login()
        csrf = self.sessions.csrf_for(token)
        self.assertTrue(csrf)
        with self.assertLogs('prusa-cam.admin_http', level='DEBUG') as captured:
            response = self.app.handle(self.req(
                'POST', '/api/logout',
                headers=self.auth_headers(token, csrf=csrf),
            ))
        self.assertEqual(response.status, 200)
        joined = '\n'.join(captured.output)
        self.assertNotIn(csrf, joined)
        self.assertNotIn('csrf-token', joined.lower())


# --------------------------------------------------------------------------- #
# Status / trusted-LAN labelling
# --------------------------------------------------------------------------- #

class StatusTests(AdminHttpTestBase):
    def test_status_includes_the_trusted_lan_warning(self):
        app = self._build_app(hotspot=self.hotspot)
        response = app.handle(self.req('GET', '/api/status'))
        self.assertEqual(response.status, 200)
        payload = json.loads(response.body.decode('utf-8'))
        notice = payload['trusted_lan']['notice']
        self.assertIn('trusted lan', notice.lower())
        self.assertIn('port-forward', notice.lower())
        interfaces = payload['trusted_lan']['interfaces']
        ports = [item.get('port') for item in interfaces]
        self.assertIn(8554, ports)
        self.assertIn(8555, ports)
        names = ' '.join(item.get('name', '') for item in interfaces)
        self.assertIn('ONVIF', names)
        paths = ' '.join(item.get('path', '') for item in interfaces)
        self.assertIn('/snapshot.jpg', paths)

    def test_status_reports_injected_hotspot(self):
        app = self._build_app(hotspot=self.hotspot)
        payload = json.loads(app.handle(self.req('GET', '/api/status')).body.decode('utf-8'))
        self.assertTrue(payload['hotspot']['active'])
        self.assertEqual(payload['hotspot']['address'], '192.168.4.1')

    def test_camera_probe_only_runs_in_setup_mode(self):
        # Post-runtime rpicam-source owns libcamera (single consumer), so probing
        # would fail and misreport a working camera as unavailable. Setup mode
        # probes; admin mode must not.
        calls = []

        class _Probe:
            ok = True
            reason = ''
            sensors = 1

        def probe():
            calls.append(True)
            return _Probe()

        admin = self._build_app(probe=probe)
        admin_payload = json.loads(
            admin.handle(self.req('GET', '/api/status')).body.decode('utf-8'))
        self.assertNotIn('camera', admin_payload)
        self.assertEqual(calls, [])

        setup = self._build_app(mode='setup', probe=probe)
        setup_payload = json.loads(
            setup.handle(self.req('GET', '/api/status')).body.decode('utf-8'))
        self.assertIn('camera', setup_payload)
        self.assertTrue(setup_payload['camera']['ok'])
        self.assertEqual(calls, [True])


# --------------------------------------------------------------------------- #
# Setup wizard
# --------------------------------------------------------------------------- #

class WizardTests(AdminHttpTestBase):
    def _setup_app(self):
        self.wizard = setup_wizard.WizardSession(
            device_id='AA:BB:CC:DD:EE:FF',
            device_path=str(self.root / 'device.toml'),
            secrets_path=str(self.root / 'secrets.toml'),
            provisioning_path=str(self.root / 'provisioning.json'),
            storage_ready=True,
            probe_result=SimpleNamespace(ok=True, reason='', sensors=1),
        )
        return self._build_app(
            mode='setup',
            provisioning_state=provisioning.ProvisioningState(state='unclaimed'),
            wizard_factory=lambda: self.wizard,
        )

    def test_invalid_step_400_and_no_advance(self):
        app = self._setup_app()
        response = app.handle(self.req(
            'POST', '/setup/step/3', body={'ssid': ''},
        ))
        self.assertEqual(response.status, 400)
        self.assertEqual(self.wizard.step, setup_wizard.STEP_ORDER[0])
        self.assertNotIn('wifi', self.wizard.completed)

    def test_valid_step_advances(self):
        app = self._setup_app()
        response = app.handle(self.req(
            'POST', '/setup/step/1', body={'storage_ready': True},
        ))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.wizard.step, 'imager_prefill')
        self.assertIn('status', self.wizard.completed)

    def test_unknown_step_number_400(self):
        app = self._setup_app()
        self.assertEqual(
            app.handle(self.req('POST', '/setup/step/99', body={})).status, 400
        )
        self.assertEqual(
            app.handle(self.req('POST', '/setup/step/abc', body={})).status, 400
        )

    def test_activate_station_is_passed_to_the_wizard(self):
        def activate(ssid, psk):
            return True

        app = self._build_app(activate_station=activate)
        self.assertIs(app._get_wizard().activate_station, activate)

    def test_activate_station_defaults_to_none(self):
        app = self._build_app()
        self.assertIsNone(app._get_wizard().activate_station)


# --------------------------------------------------------------------------- #
# Expert configuration
# --------------------------------------------------------------------------- #

class ExpertConfigTests(AdminHttpTestBase):
    def test_invalid_put_400_and_live_files_unchanged(self):
        device_path = self.root / 'device.toml'
        secrets_path = self.root / 'secrets.toml'
        device_path.write_bytes(b'original-device-bytes')
        secrets_path.write_bytes(b'original-secrets-bytes')

        token = self.login()
        csrf = self.sessions.csrf_for(token)
        response = self.app.handle(self.req(
            'PUT', '/api/config',
            body={'device': '[[[unterminated', 'secrets': 'x = 1'},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 400)
        self.assertEqual(device_path.read_bytes(), b'original-device-bytes')
        self.assertEqual(secrets_path.read_bytes(), b'original-secrets-bytes')


# --------------------------------------------------------------------------- #
# Factory reset two-step flow
# --------------------------------------------------------------------------- #

class FactoryResetTests(AdminHttpTestBase):
    def _reset_app(self):
        controller = factory_reset.FactoryReset(
            data_root=str(self.root), durable_root=str(self.root),
        )
        os.makedirs(controller.config_dir, exist_ok=True)
        marker = Path(controller.config_dir) / 'device.toml'
        marker.write_text('keep-me')
        return self._build_app(factory_reset=controller), marker

    def test_execute_without_reauth_403(self):
        app, marker = self._reset_app()
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        response = app.handle(self.req(
            'POST', '/api/reset/execute', body={},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(response.status, 403)
        self.assertTrue(marker.exists())

    def test_execute_before_begin_or_confirm_deletes_nothing(self):
        app, marker = self._reset_app()
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)

        before_begin = app.handle(self.req(
            'POST', '/api/reset/execute', body={'password': ADMIN_PASSWORD},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(before_begin.status, 409)
        self.assertTrue(marker.exists())

        app.handle(self.req(
            'POST', '/api/reset/begin', body={'password': ADMIN_PASSWORD},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        before_confirm = app.handle(self.req(
            'POST', '/api/reset/execute', body={'password': ADMIN_PASSWORD},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(before_confirm.status, 409)
        self.assertTrue(marker.exists())

    def test_confirm_with_wrong_explicit_token_409(self):
        app, marker = self._reset_app()
        token = self.login(app)
        csrf = self.sessions.csrf_for(token)
        begin = app.handle(self.req(
            'POST', '/api/reset/begin', body={'password': ADMIN_PASSWORD},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(begin.status, 200)

        rejected = app.handle(self.req(
            'POST', '/api/reset/confirm',
            body={'password': ADMIN_PASSWORD, 'token': 'not-the-server-token'},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(rejected.status, 409)

        # The wrong token must not have armed the reset.
        after = app.handle(self.req(
            'POST', '/api/reset/execute', body={'password': ADMIN_PASSWORD},
            headers=self.auth_headers(token, csrf=csrf),
        ))
        self.assertEqual(after.status, 409)
        self.assertTrue(marker.exists())


if __name__ == '__main__':
    unittest.main()
