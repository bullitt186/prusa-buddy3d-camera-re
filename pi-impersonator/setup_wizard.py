"""First-boot captive-portal wizard core (WP-3c, AC-17).

This module owns the *host-testable* core of the provisioning wizard served at
``http://192.168.4.1`` while the appliance is unclaimed (source plan §4.3). It
has no HTTP/aiohttp dependency: the future portal layer is a thin adapter that
maps form posts onto :meth:`WizardSession.submit`. Everything here is
stdlib-only, import-safe, and injectable (clock, callbacks, paths) so it can be
driven entirely on a workstation.

The wizard steps (source §4.3) in order:

1. ``status``           storage readiness and the camera probe result.
2. ``imager_prefill``   optional Wi-Fi/hostname/SSH values supplied by
                        Raspberry Pi Imager (Imager 2.x "advanced options" /
                        ``firstrun`` customization). They are *prefilled* only;
                        the user may change them and nothing here is required.
3. ``wifi``             Wi-Fi scan list or manual SSID + PSK. The scan is
                        hardware-only and injectable (``wifi_scan``); manual
                        entry always works, including for hidden networks.
4. ``prusa_token``      MANUAL registration-token entry. A clearly-documented
                        hook (``qr_decoder``) exists for the WP-4 exact-QR path;
                        no QR parsing is implemented here (see
                        :data:`QR_PAIRING_HOOK`).
5. ``fingerprint``      optional explicit fingerprint; empty keeps the existing
                        MAC-derived behaviour.
6. ``admin_password``   local administrator password + confirmation. Only the
                        scrypt hash from :func:`admin_auth.hash_password` is
                        stored; the plaintext is never retained.
7. ``mqtt``             optional MQTT settings; validation by default, with an
                        optional injectable live broker test (WP-R2, AC-23 tail)
                        that is never persisted.
8. ``summary``          final REDACTED summary before committing.
9. ``persist``          atomic, validate-before-activate persistence. The device
                        and secrets documents are built, then parsed with
                        :func:`config_schema.parse_device` /
                        :func:`config_schema.parse_secrets` *before* any write.
                        Invalid input is rejected with a non-secret reason and
                        nothing is written.
10. ``finish``          stop the provisioning hotspot, then start the camera
                        target through an injectable callback, guarded by
                        :func:`camera_probe.sensor_handoff_allowed`.

Hotspot lifetime
----------------
The setup hotspot exists only while the device is unclaimed. It is stopped in
step 10 *before* the camera target starts, and the sensor hand-off guard denies
finishing while a live libcamera owner is running (AC-18).

Secret hygiene
--------------
No plaintext secret is stored in the session, the summary, ``repr`` or any log.
:meth:`WizardSession.summary` redacts through :func:`admin_auth.redact`, and the
persisted secrets document contains only the token, PSK, MQTT credentials and
the password *hash*. Rejection reasons name a field or schema error, never a
value.
"""
import dataclasses
import logging
from datetime import datetime, timezone

import admin_auth
import camera_probe
import config_schema
import hotspot
import identity
import mqtt_service
import provisioning
import settings_store

log = logging.getLogger('prusa-cam.setup_wizard')

#: The AC-17 wizard steps, in order. Index + 1 is the UI step number.
STEP_ORDER = (
    'status',
    'imager_prefill',
    'wifi',
    'prusa_token',
    'fingerprint',
    'admin_password',
    'mqtt',
    'summary',
    'persist',
    'finish',
)

#: Human labels for the portal UI.
STEP_TITLES = {
    'status': 'Storage and camera status',
    'imager_prefill': 'Imager customization',
    'wifi': 'Wi-Fi network',
    'prusa_token': 'Prusa registration token',
    'fingerprint': 'Optional fingerprint',
    'admin_password': 'Administrator password',
    'mqtt': 'Optional MQTT',
    'summary': 'Review',
    'persist': 'Save configuration',
    'finish': 'Start camera',
}

#: Documented marker for the not-yet-implemented exact-QR parser (source §4.4).
#: WP-3c ships the manual token path plus an injectable ``qr_decoder`` hook; the
#: real parser must not be guessed and is delivered by WP-4.
QR_PAIRING_HOOK = 'WP-4'

#: Steps that must be complete before the configuration may be persisted.
PERSIST_PREREQUISITES = frozenset({'wifi', 'prusa_token', 'admin_password'})

#: MQTT defaults reused when the user enables MQTT without overriding them.
DEFAULT_MQTT_URI = 'mqtts://broker.example:8883'


# --------------------------------------------------------------------------- #
# Result object
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class StepResult:
    """Outcome of a step submission (never carries a secret)."""

    ok: bool
    reason: str = ''
    step: str = ''
    state: str = ''


# --------------------------------------------------------------------------- #
# Wizard session
# --------------------------------------------------------------------------- #

class WizardSession:
    """One first-boot wizard session over the persisted device/secrets files.

    All side effects are injectable so the class is fully host-testable:

    * ``device_path``/``secrets_path``/``provisioning_path`` default to the
      durable ``/data`` locations and are redirected to ``tempfile`` paths in
      tests.
    * ``probe``/``probe_result`` supply the camera status.
    * ``wifi_scan`` performs the hardware-only scan.
    * ``qr_decoder`` is the WP-4 QR hook (not implemented here).
    * ``hotspot_controller`` performs the real ``nmcli`` stop; tests inject a
      fake that records call order.
    * ``mqtt_tester`` optionally runs a live broker connection test for step 7
      (WP-R2); it receives a ``mqtt_service.MqttConfig`` and returns
      ``(ok, reason)``. When ``None`` step 7 stays validation-only.
    * ``start_camera`` starts ``prusa-camera.target``.
    * ``activate_station`` creates/activates the Wi-Fi station profile after the
      hotspot is stopped and before the camera target starts; when it fails the
      hotspot is restarted and the device stays in setup (B2). It is optional:
      when ``None`` the finish path is unchanged.
    * ``clock`` supplies timestamps to the provisioning state machine.
    """

    def __init__(
        self,
        device_id=None,
        *,
        device_path=config_schema.DEVICE_TOML_PATH,
        secrets_path=config_schema.SECRETS_TOML_PATH,
        provisioning_path=provisioning.PROVISIONING_PATH,
        ifname=hotspot.DEFAULT_IFNAME,
        hotspot_controller=None,
        probe=None,
        probe_result=None,
        storage_ready=None,
        wifi_scan=None,
        qr_decoder=None,
        mqtt_tester=None,
        start_camera=None,
        activate_station=None,
        camera_running=False,
        imager_values=None,
        camera_name='Printer Camera',
        prusa_server=None,
        clock=None,
    ):
        self.device_id = device_id or ''
        self.device_path = device_path
        self.secrets_path = secrets_path
        self.provisioning_path = provisioning_path
        self.ifname = ifname
        self.hotspot_controller = hotspot_controller
        self.probe = probe
        self.probe_result = probe_result
        self.wifi_scan = wifi_scan
        self.qr_decoder = qr_decoder
        self.mqtt_tester = mqtt_tester
        self.start_camera = start_camera
        self.activate_station = activate_station
        self.camera_running = bool(camera_running)
        self.camera_name = camera_name
        self.prusa_server = prusa_server
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        if storage_ready is None:
            try:
                storage_ready = bool(settings_store.available())
            except Exception:  # noqa: BLE001 - status must never raise
                storage_ready = False
        self.storage_ready = bool(storage_ready)

        # Current UI position and completed steps.
        self.step = STEP_ORDER[0]
        self.completed = set()

        # Staged, non-secret state.
        self.status = {}
        self.imager = {}
        self.wifi = {}
        self.fingerprint = ''
        self.hostname = ''
        self.mqtt = {'enabled': False, 'uri': '', 'username': '', 'password': ''}
        self.wifi_scan_results = []
        self.imager_values = dict(imager_values or {})

        # Staged secrets (never exposed by repr/summary).
        self.token = ''
        self.admin_hash = ''

        # Persisted results.
        self.device = {}
        self.secrets = {}
        self.persisted = False
        self.finished = False
        self.provisioning_state = ''
        self._camera_validated = False

    # -- safe representation ------------------------------------------------- #

    def __repr__(self):
        """Never expose a token, PSK, MQTT credential or password hash."""
        return (
            f'<WizardSession step={self.step!r} completed={len(self.completed)} '
            f'persisted={self.persisted} finished={self.finished}>'
        )

    # -- navigation ---------------------------------------------------------- #

    def set_step(self, step):
        """Select ``step`` for display; reject an unknown step name."""
        if step not in STEP_ORDER:
            return StepResult(False, 'unknown wizard step', step)
        self.step = step
        return StepResult(True, '', step)

    def submit(self, step, data):
        """Validate and apply one step's ``data``.

        A rejected submission returns ``ok=False`` with a non-secret reason,
        does **not** mark the step complete, and does not advance
        :attr:`step`. On success the next step becomes current.
        """
        if step not in STEP_ORDER:
            return StepResult(False, 'unknown wizard step', step)
        if not isinstance(data, dict):
            return StepResult(False, 'step data must be a mapping', step)
        if step == 'persist' and not PERSIST_PREREQUISITES <= self.completed:
            return StepResult(
                False,
                'complete Wi-Fi, Prusa token and admin password steps first',
                step,
            )
        if step == 'finish' and not self.persisted:
            return StepResult(False, 'configuration has not been persisted', step)

        handler = getattr(self, '_step_' + step)
        try:
            ok, reason = handler(data)
        except Exception as e:  # noqa: BLE001 - never leak internals/secrets
            log.warning(f'setup_wizard: step {step} failed: {e}')
            ok, reason = False, 'step failed'
        if not ok:
            return StepResult(False, reason, step)

        self.completed.add(step)
        index = STEP_ORDER.index(step)
        if index + 1 < len(STEP_ORDER):
            self.step = STEP_ORDER[index + 1]
        return StepResult(True, '', step, self.provisioning_state)

    # -- step handlers ------------------------------------------------------- #

    def _step_status(self, data):
        """Step 1: record storage readiness and the camera probe result."""
        storage_ready = data.get('storage_ready', self.storage_ready)
        probe_result = data.get('probe_result', self.probe_result)
        if probe_result is None and self.probe is not None:
            try:
                probe_result = self.probe()
            except Exception as e:  # noqa: BLE001 - a probe must never raise
                log.warning(f'setup_wizard: camera probe failed: {e}')
                probe_result = None
        self._camera_validated = bool(getattr(probe_result, 'ok', False))
        self.status = {
            'storage_ready': bool(storage_ready),
            'camera_ok': self._camera_validated,
            'camera_reason': getattr(probe_result, 'reason', '') if probe_result else '',
            'sensors': getattr(probe_result, 'sensors', 0) if probe_result else 0,
        }
        return True, ''

    def _step_imager_prefill(self, data):
        """Step 2: accept optional Raspberry Pi Imager customization values."""
        expected = {
            'wifi_ssid': str,
            'wifi_psk': str,
            'hostname': str,
            'ssh_enabled': bool,
        }
        for key, kind in expected.items():
            value = data.get(key)
            if value is None:
                continue
            if kind is bool and not isinstance(value, bool):
                return False, f'{key} has an invalid type'
            if kind is str and not isinstance(value, str):
                return False, f'{key} has an invalid type'
        self.imager = {
            'wifi_ssid': data.get('wifi_ssid') or '',
            'wifi_psk': data.get('wifi_psk') or '',
            'hostname': data.get('hostname') or '',
            'ssh_enabled': bool(data.get('ssh_enabled', False)),
        }
        if self.imager['wifi_ssid']:
            self.wifi.setdefault('ssid', self.imager['wifi_ssid'])
        if self.imager['wifi_psk']:
            self.wifi.setdefault('psk', self.imager['wifi_psk'])
        if self.imager['hostname']:
            self.hostname = self.imager['hostname']
        return True, ''

    def scan_networks(self):
        """Run the injectable hardware scan; return ``(ok, reason)``.

        This is the only part of step 3 that needs hardware. It never touches
        the network itself: ``wifi_scan`` is supplied by the caller.
        """
        if self.wifi_scan is None:
            return False, 'wifi scanning is not available'
        try:
            results = self.wifi_scan()
        except Exception as e:  # noqa: BLE001 - a scan must never raise
            log.warning(f'setup_wizard: wifi scan failed: {e}')
            return False, 'wifi scan failed'
        self.wifi_scan_results = list(results or [])
        return True, ''

    def _step_wifi(self, data):
        """Step 3: scan (optional) and record a manual SSID + PSK."""
        if data.get('scan'):
            ok, reason = self.scan_networks()
            if not ok:
                return False, reason
        ssid = data.get('ssid')
        if not isinstance(ssid, str) or not ssid.strip():
            return False, 'wifi SSID is required'
        psk = data.get('psk', '')
        if psk is None:
            psk = ''
        if not isinstance(psk, str):
            return False, 'wifi PSK must be a string'
        if psk and not 8 <= len(psk) <= 63:
            return False, 'wifi PSK must be 8..63 characters'
        self.wifi = {'ssid': ssid.strip(), 'psk': psk}
        return True, ''

    def _step_prusa_token(self, data):
        """Step 4: manual registration token, plus the documented WP-4 QR hook."""
        source = data.get('source', 'manual')
        if source == 'qr':
            if self.qr_decoder is None:
                return (
                    False,
                    f'QR pairing is not implemented yet ({QR_PAIRING_HOOK} hook); '
                    'enter the token manually',
                )
            try:
                token = self.qr_decoder(data.get('qr'))
            except Exception as e:  # noqa: BLE001 - decoder must never raise
                log.warning(f'setup_wizard: QR decoder failed: {e}')
                return False, 'QR pairing failed'
            if not isinstance(token, str) or not token.strip():
                return False, 'QR payload did not contain a registration token'
        elif source == 'manual':
            token = data.get('token')
        else:
            return False, 'unknown token source'
        if not isinstance(token, str) or not token.strip():
            return False, 'Prusa registration token is required'
        self.token = token.strip()
        return True, ''

    def _step_fingerprint(self, data):
        """Step 5: explicit fingerprint, else persist the stable MAC-derived one.

        Leaving it empty derived the fingerprint at runtime from the wlan0 MAC,
        which NetworkManager randomizes during scans, so the identity (and the
        Prusa Connect token binding) was not stable -- hardware-found as
        "Invalid fingerprint", snapshot 400, and signaling ACK=1 after a rescan.
        """
        fingerprint = data.get('fingerprint', '')
        if fingerprint is None:
            fingerprint = ''
        if not isinstance(fingerprint, str):
            return False, 'fingerprint must be a string'
        fingerprint = fingerprint.strip()
        if not fingerprint:
            fingerprint = self._derived_fingerprint()
        self.fingerprint = fingerprint
        return True, ''

    @classmethod
    def _derived_fingerprint(cls, mac_path='/sys/class/net/wlan0/address'):
        """The stable MAC-derived fingerprint, or '' when no MAC is readable.

        Deliberately does NOT fall back to a stored seed: the fingerprint must
        be a real hardware identity at onboarding, and an invented one would not
        match what the runtime derives.
        """
        try:
            raw_mac = open(mac_path).read().strip()
        except OSError:
            raw_mac = ''
        if not raw_mac:
            return ''
        try:
            _mac, derived = identity.resolve_fingerprint(None, raw_mac)
        except Exception:  # noqa: BLE001 - a step must never raise
            return ''
        return derived or ''

    def _step_admin_password(self, data):
        """Step 6: strength-check, confirm, then store only the scrypt hash."""
        password = data.get('password')
        confirm = data.get('confirm')
        if not isinstance(password, str):
            return False, 'password must be a string'
        ok, reason = admin_auth.password_strength_ok(password)
        if not ok:
            return False, reason
        if confirm != password:
            return False, 'password confirmation does not match'
        try:
            self.admin_hash = admin_auth.hash_password(password)
        except ValueError as e:
            return False, str(e)
        return True, ''

    def _step_mqtt(self, data):
        """Step 7: validate optional MQTT settings, with an optional live test.

        Validation-only is the default (no connection). When MQTT is enabled,
        the caller requests a test (``test`` truthy), and an injectable
        ``mqtt_tester`` is configured, the broker is probed before anything is
        staged; a failure is surfaced redacted and the step is not completed, so
        nothing is persisted (WP-R2, AC-23 tail).
        """
        enabled = data.get('enabled', False)
        if not isinstance(enabled, bool):
            return False, 'mqtt enabled must be a boolean'
        uri = data.get('uri', '')
        username = data.get('username', '')
        password = data.get('password', '')
        for name, value in (('uri', uri), ('username', username), ('password', password)):
            if value is None:
                value = ''
            if not isinstance(value, str):
                return False, f'mqtt {name} must be a string'
        if enabled and not uri:
            return False, 'mqtt uri is required when MQTT is enabled'
        if enabled:
            candidate = config_schema.default_device()
            candidate['mqtt']['enabled'] = True
            candidate['mqtt']['uri'] = uri
            try:
                config_schema.parse_device(config_schema.dumps_device(candidate))
            except config_schema.ConfigError as e:
                return False, str(e)
        if enabled and data.get('test') and self.mqtt_tester is not None:
            ok, reason = self._test_mqtt(uri, username, password)
            if not ok:
                return False, reason
        self.mqtt = {
            'enabled': enabled,
            'uri': uri or DEFAULT_MQTT_URI,
            'username': username or '',
            'password': password or '',
        }
        return True, ''

    def _test_mqtt(self, uri, username, password):
        """Run the injected broker test; return a redacted ``(ok, reason)``.

        Never raises and never echoes a credential: the reason is scrubbed
        against the supplied username/password/URI before it is returned.
        """
        try:
            config = mqtt_service.MqttConfig(
                enabled=True, uri=uri, username=username, password=password,
            )
            result = self.mqtt_tester(config)
        except Exception as e:  # noqa: BLE001 - a tester must never raise
            log.warning(f'setup_wizard: mqtt broker test failed: {type(e).__name__}')
            return False, 'mqtt connection test failed'
        if isinstance(result, (tuple, list)) and len(result) == 2:
            ok, reason = result
        else:
            ok, reason = bool(result), ''
        if ok:
            return True, ''
        safe = admin_auth.redact(str(reason or ''), (password, username, uri))
        safe = ''.join(ch for ch in safe if ch.isprintable()).strip()
        return False, (safe[:200] or 'mqtt connection test failed')

    def _step_summary(self, data):
        """Step 8: build the redacted summary (no secret is retained)."""
        return True, ''

    def _step_persist(self, data):
        """Step 9: validate the documents, then write and advance the state."""
        device, secrets = self.build_documents()
        try:
            config_schema.parse_device(config_schema.dumps_device(device))
            config_schema.parse_secrets(config_schema.dumps_secrets(secrets))
        except config_schema.ConfigError as e:
            return False, str(e)
        if not config_schema.save_device(device, path=self.device_path):
            return False, 'could not write device configuration'
        if not config_schema.save_secrets(secrets, path=self.secrets_path):
            return False, 'could not write secrets configuration'
        self.device, self.secrets = device, secrets
        self.persisted = True
        self.provisioning_state = self._advance_provisioning()
        return True, ''

    def _step_finish(self, data):
        """Step 10: stop the hotspot then start the camera target."""
        result = self.finish(
            camera_running=data.get('camera_running'),
            now=data.get('now'),
        )
        return result.ok, result.reason

    # -- document construction ----------------------------------------------- #

    def build_documents(self):
        """Return the ``(device, secrets)`` documents implied by the session.

        Validation is *not* performed here; :meth:`_step_persist` parses both
        documents before writing anything.
        """
        device = config_schema.default_device()
        device['camera_name'] = self.camera_name
        device['fingerprint'] = self.fingerprint
        device['admin']['hostname'] = (
            self.hostname or provisioning.admin_hostname(self.device_id)
        )
        if self.prusa_server:
            device['prusa']['server'] = self.prusa_server
        mqtt = device['mqtt']
        mqtt['enabled'] = bool(self.mqtt.get('enabled'))
        if self.mqtt.get('enabled') and self.mqtt.get('uri'):
            mqtt['uri'] = self.mqtt['uri']

        secrets = {}
        if self.token:
            secrets.setdefault('prusa', {})['token'] = self.token
        if self.wifi.get('psk'):
            secrets.setdefault('wifi', {})['psk'] = self.wifi['psk']
        if self.mqtt.get('enabled'):
            mqtt_secrets = {}
            if self.mqtt.get('username'):
                mqtt_secrets['username'] = self.mqtt['username']
            if self.mqtt.get('password'):
                mqtt_secrets['password'] = self.mqtt['password']
            if mqtt_secrets:
                secrets['mqtt'] = mqtt_secrets
        if self.admin_hash:
            secrets.setdefault('admin', {})['password_hash'] = self.admin_hash
        return device, secrets

    # -- provisioning -------------------------------------------------------- #

    def _advance_provisioning(self):
        """Advance the persisted state to the furthest pre-runtime milestone.

        The wizard stops at ``claimed`` (or earlier, as the facts allow) because
        the runtime states are reached by the camera target after step 10, once
        the setup hotspot has been stopped. Each edge is persisted by
        :meth:`provisioning.ProvisioningState.advance`.
        """
        state = provisioning.ProvisioningState.load(self.provisioning_path)
        facts = provisioning.Facts(
            storage_ready=True,
            camera_validated=self._camera_validated,
            admin_password_set=True,
            device_valid=True,
            prusa_token_set=bool(self.token),
            camera_running=False,
        )
        for _ in range(len(provisioning.STATES)):
            if state.state == 'claimed':
                break
            previous = state.state
            result = state.advance(now=self._clock(), facts=facts)
            if not result.ok or state.state == previous:
                break
        return state.state

    # -- summary / finish ---------------------------------------------------- #

    def summary(self):
        """Return a REDACTED view of everything staged for review.

        Sensitive keys are replaced with :data:`admin_auth.REDACTED`, and the
        literal secrets are additionally scrubbed from any free-form string, so
        no token, PSK, MQTT credential, fingerprint or password hash survives.
        """
        raw = {
            'step': self.step,
            'completed': sorted(self.completed),
            'status': self.status,
            'imager': {
                'wifi_ssid': self.imager.get('wifi_ssid', ''),
                'hostname': self.imager.get('hostname', ''),
                'ssh_enabled': self.imager.get('ssh_enabled', False),
            },
            'wifi': {
                'ssid': self.wifi.get('ssid', ''),
                'psk': self.wifi.get('psk', ''),
            },
            'prusa': {
                'server': self.prusa_server or '',
                'token': self.token,
            },
            'fingerprint': self.fingerprint,
            'admin': {
                'hostname': self.hostname or provisioning.admin_hostname(self.device_id),
                'password_hash': self.admin_hash,
            },
            'mqtt': {
                'enabled': bool(self.mqtt.get('enabled')),
                'uri': self.mqtt.get('uri', ''),
                'username': self.mqtt.get('username', ''),
                'password': self.mqtt.get('password', ''),
            },
            'provisioning_state': self.provisioning_state,
        }
        literal_secrets = (
            self.token,
            self.wifi.get('psk', ''),
            self.mqtt.get('username', ''),
            self.mqtt.get('password', ''),
            self.fingerprint,
            self.admin_hash,
        )
        return admin_auth.redact(raw, literal_secrets)

    def finish(self, camera_running=None, now=None):
        """Stop the hotspot, activate station networking, then start the camera.

        The camera-start callback is checked *before* the AP is taken down, so a
        misconfigured caller can never strand a headless device with neither a
        captive portal nor a running camera. The hotspot is stopped *before*
        ``activate_station``/``start_camera`` run, so the setup portal and the
        station runtime never overlap. Finishing is denied while a live
        libcamera owner is running.

        ``activate_station`` is optional (backward compatible): when configured
        it runs after the hotspot is stopped and before ``start_camera``; if it
        returns falsy the hotspot is restarted best-effort and the device stays
        in setup rather than coming up offline. If the camera target fails to
        start, the hotspot is restarted best-effort and the failure is reported
        rather than swallowed.
        """
        if not self.persisted:
            return StepResult(False, 'configuration has not been persisted', 'finish')
        if self.start_camera is None:
            return StepResult(
                False, 'camera start callback is not configured', 'finish',
                self.provisioning_state,
            )
        running = self.camera_running if camera_running is None else bool(camera_running)
        state = self.provisioning_state
        allowed, reason = camera_probe.sensor_handoff_allowed(state, running)
        if not allowed:
            return StepResult(False, reason, 'finish', state)

        controller = self.hotspot_controller or hotspot
        stop_result = controller.stop(ifname=self.ifname)
        if not stop_result.ok:
            return StepResult(
                False,
                'could not stop provisioning hotspot: ' + (stop_result.reason or 'unknown'),
                'finish',
                state,
            )
        if self.activate_station is not None:
            outcome = self._activate_station()
            if not outcome:
                restart_note = self._restart_hotspot(controller)
                detail = getattr(outcome, 'reason', '')
                reason = 'station activation failed'
                if isinstance(detail, str) and detail:
                    reason += f' ({detail})'
                reason += '; device stayed in setup'
                if restart_note:
                    reason += f' ({restart_note})'
                return StepResult(False, reason, 'finish', state)
        try:
            started = self.start_camera()
        except Exception as e:  # noqa: BLE001 - never leak internals
            log.warning(
                f'setup_wizard: camera target failed to start: {type(e).__name__}'
            )
            started = False
        if started is False:
            restart_note = self._restart_hotspot(controller)
            reason = 'camera target failed to start; device stayed in setup'
            if restart_note:
                reason += f' ({restart_note})'
            return StepResult(False, reason, 'finish', state)
        self.finished = True
        return StepResult(True, '', 'finish', state)

    def _activate_station(self):
        """Call the injected station activation callable with SSID + PSK.

        Returns the callable's outcome (truthy on success) or ``False`` when the
        callable raises. Never raises itself.
        """
        ssid = self.wifi.get('ssid', '')
        psk = self.wifi.get('psk', '')
        try:
            return self.activate_station(ssid, psk)
        except Exception as e:  # noqa: BLE001 - activation must never raise
            log.warning(
                f'setup_wizard: station activation failed: {type(e).__name__}'
            )
            return False

    def _restart_hotspot(self, controller):
        """Best-effort restart of the setup AP; returns a non-secret note or ``''``.

        Called when the camera target fails to start so a headless device keeps
        its captive portal. The SSID is derived from the device id; no secret is
        involved. Never raises.
        """
        try:
            result = controller.start(
                provisioning.setup_ssid(self.device_id), ifname=self.ifname
            )
        except Exception as e:  # noqa: BLE001 - restart must never raise
            log.warning(f'setup_wizard: hotspot restart failed: {type(e).__name__}')
            return 'hotspot restart failed'
        if not result.ok:
            return 'hotspot restart failed'
        return ''
