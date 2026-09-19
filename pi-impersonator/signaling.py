import asyncio
import binascii
import os
import socketio
import logging
import hashlib
import time
from auth import auth_ack_is_success
from proto import encode_message, decode_message
from status import build_status_message
from features import PROTOCOL_VERSION, FEATURES, FIRMWARE_VERSION, MODEL, MANUFACTURER, HW_VERSION

log = logging.getLogger('prusa-cam.signaling')

class PrusaSignaling:
    def __init__(self, fingerprint, token, state, mac='', ip='', ssid=''):
        self.fingerprint = fingerprint
        self.token = token
        self.state = state
        self.mac = mac
        self.ip = ip
        self.ssid = ssid
        self._event_handler = None
        self.sio = self._new_client()
        self._setup_handlers()

    def _new_client(self):
        """A fresh Socket.IO client (no stale engineio session state).

        Firmware parity: `CheckSocketServerConnection` owns reconnection and the
        camera clears its stored session id before reconnecting (`FUN_0038a420`:
        `*(param_1+0xb74)=0; **(param_1+0xb70)=0`). python-engineio instead keeps
        the previous sid and reuses it on reconnect, so every library-managed
        retry resumes a dead session and the server answers "Server sent close
        packet data 0". We therefore disable library reconnection and create a new
        client per attempt from the supervisor below.
        """
        return socketio.AsyncClient(
            reconnection=False,
            # Diagnostic only: PRUSA_SIO_DEBUG=1 surfaces engineio close reasons.
            logger=os.environ.get('PRUSA_SIO_DEBUG') == '1',
            engineio_logger=os.environ.get('PRUSA_SIO_DEBUG') == '1',
        )

    def _setup_handlers(self):
        @self.sio.event
        async def connect():
            log.info('Socket.IO connected, authenticating...')
            await self._authenticate()

        @self.sio.event
        async def disconnect():
            log.warning('Socket.IO disconnected')

        @self.sio.event
        async def connect_error(data):
            log.error(f'Socket.IO connect error: {data}')

        @self.sio.on('trigger')
        async def on_trigger(data):
            self._log_inbound_event('trigger', data)
            # Ack limitation (GAP-TRIGGER-01): this handler signature carries
            # only the payload, not a Socket.IO ack callback, so the
            # firmware-style trigger result code cannot be returned here. main's
            # dispatcher logs each dispatched action instead; wire an ack only
            # after confirming the installed python-socketio passes a callback.
            log.debug('trigger: no ack callback available; result code not returned')
            if self._event_handler:
                await self._event_handler('trigger', data)

        @self.sio.on('webrtc')
        async def on_webrtc(data):
            self._log_inbound_event('webrtc', data)
            if self._event_handler:
                await self._event_handler('webrtc', data)

        @self.sio.on('configuration')
        async def on_configuration(data):
            self._log_inbound_event('configuration', data)
            if self._event_handler:
                await self._event_handler('configuration', data)

        @self.sio.on('set_webrtc_mode')
        async def on_set_webrtc_mode(data):
            self._log_inbound_event('set_webrtc_mode', data)
            # RE confirmed: field 1 = enable(1)/disable(0) byte.
            # Log it; the status message already reports mode=1/status=1 (self-enabled).
            if self._event_handler:
                await self._event_handler('set_webrtc_mode', data)

        @self.sio.on('set_rtsp_server_mode')
        async def on_set_rtsp_server_mode(data):
            self._log_inbound_event('set_rtsp_server_mode', data)
            if self._event_handler:
                await self._event_handler('set_rtsp_server_mode', data)

        @self.sio.on('change_video_size')
        async def on_change_video_size(data):
            self._log_inbound_event('change_video_size', data)
            if self._event_handler:
                await self._event_handler('change_video_size', data)

        @self.sio.on('save_video_size')
        async def on_save_video_size(data):
            self._log_inbound_event('save_video_size', data)
            if self._event_handler:
                await self._event_handler('save_video_size', data)

        @self.sio.on('timelapse_get_file_list')
        async def on_timelapse_get_file_list(data):
            self._log_inbound_event('timelapse_get_file_list', data)
            if self._event_handler:
                await self._event_handler('timelapse_get_file_list', data)

        @self.sio.on('*')
        async def catch_all(event, data):
            self._log_inbound_event(event, data, prefix='Unknown event')

    async def _authenticate(self):
        auth_msg = encode_message({1: self.fingerprint, 2: self.token})
        try:
            ack = await self.sio.call('camera_authentication', auth_msg, timeout=10)
        except Exception as e:
            log.error(f'Auth failed: {e}; dropping session for supervised retry')
            await self._drop_session()
            return
        log.info(f'Auth ACK: {ack!r}')
        # GAP-AUTH-01: only the exact integer 1 is a successful ACK; anything else
        # (0, 5, malformed, bool) must not emit post-auth messages. Drop the
        # session so the supervisor retries with a fresh client + backoff.
        if not auth_ack_is_success(ack):
            log.warning(f'Auth not accepted (ACK={ack!r}); dropping session for supervised retry')
            await self._drop_session()
            return
        await self._send_post_auth()

    async def _drop_session(self):
        """Close the current client so ``supervise`` reconnects on its backoff."""
        try:
            await self.sio.disconnect()
        except Exception:
            pass

    def _log_ack(self, event):
        def cb(*args):
            log.info(f'{event} ACK (async): {args!r}')
        return cb

    def _redact(self, value):
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            redacted = value
            for secret in (self.token, self.fingerprint):
                if secret:
                    redacted = redacted.replace(secret, f'<redacted:{len(secret)}>')
            return redacted
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self._redact(item) for item in value)
        return value

    def pb_summary(self, data: bytes) -> str:
        try:
            decoded = decode_message(data)
            parts = []
            for k, v in decoded.items():
                if isinstance(v, bytes):
                    parts.append(f'{k}=<bytes:{len(v)}>')
                elif isinstance(v, str) and len(v) > 40:
                    parts.append(f'{k}={v[:40]!r}...')
                else:
                    parts.append(f'{k}={v!r}')
            return '{' + ', '.join(parts) + '}'
        except Exception as exc:
            return f'<decode_error:{exc}>'

    def _log_inbound_event(self, event, data, prefix='Inbound event'):
        if isinstance(data, bytes):
            preview = binascii.hexlify(data[:256]).decode('ascii')
            summary = self.pb_summary(data)
            log.info(f'{prefix}: {event} bytes len={len(data)} summary={summary} hex256={preview}')
        else:
            log.info(f'{prefix}: {event} type={type(data).__name__} value={self._redact(data)!r}')

    async def sio_emit(self, event, data, callback=None):
        if isinstance(data, bytes):
            log.info(f'SIO OUT {event} bytes len={len(data)} summary={self.pb_summary(data)}')
        else:
            log.info(f'SIO OUT {event} type={type(data).__name__} value={self._redact(data)!r}')
        await self.sio.emit(event, data, callback=callback)

    def _signal_quality(self):
        try:
            with open('/proc/net/wireless') as f:
                for line in f:
                    if line.strip().startswith('wlan0:'):
                        fields = line.split()
                        quality = float(fields[2].strip('.'))
                        return max(0, min(100, round(quality * 100 / 70)))
        except Exception:
            pass
        return 0

    def _cpu_temperature(self):
        try:
            with open('/sys/class/thermal/thermal_zone0/temp') as f:
                return round(int(f.read().strip()) / 1000.0, 2)
        except Exception:
            return 0.0

    def _uptime_seconds(self):
        try:
            with open('/proc/uptime') as f:
                return int(float(f.read().split()[0]))
        except Exception:
            return 0

    def _load_average(self):
        try:
            with open('/proc/loadavg') as f:
                one, five, fifteen = f.read().split()[:3]
            return f'{one} {five} {fifteen}'
        except Exception:
            return ''

    def _memory_stats(self):
        stats = {'MemTotal': 0, 'MemFree': 0, 'Shmem': 0, 'Buffers': 0}
        try:
            with open('/proc/meminfo') as f:
                for line in f:
                    key, value = line.split(':', 1)
                    if key in stats:
                        stats[key] = int(value.split()[0]) * 1024
        except Exception:
            pass
        return stats

    def _process_count(self):
        try:
            return sum(name.isdigit() for name in os.listdir('/proc'))
        except Exception:
            return 0

    def _status_message(self, request_id=None):
        # Field construction is pure and lives in status.py so it can be tested
        # without socketio (GAP-STATUS-01/02, GAP-NETWORK-01).
        return build_status_message(
            self.state,
            token=self.token,
            mac=self.mac,
            ip=self.ip,
            ssid=self.ssid,
            signal_quality=self._signal_quality(),
            cpu_temperature=self._cpu_temperature(),
            uptime=self._uptime_seconds(),
            load_average=self._load_average(),
            memory=self._memory_stats(),
            process_count=self._process_count(),
            request_id=request_id,
            sid=self.sio.get_sid() or self.sio.sid or '',
            # GAP-STATUS-04: report the detected /etc/TZ content (firmware reads
            # it back); fall back to the process abbreviation only if undetected.
            tz_name=state.tz_name or (time.tzname[0] if time.tzname else ''),
        )

    async def send_status(self, request_id=None):
        status_msg = self._status_message(request_id=request_id)
        await self.sio_emit('status', status_msg, callback=self._log_ack('status'))
        suffix = f', request_id={request_id[:16]}...' if request_id else ''
        log.info(f'Sent status ({len(status_msg)} bytes, full always-present field set{suffix})')

    async def send_protobuf_version(self, request_id=None):
        fields = {1: self.token, 2: PROTOCOL_VERSION}
        if request_id:
            fields[3] = request_id
        version_msg = encode_message(fields)
        await self.sio_emit('protobuf_version', version_msg, callback=self._log_ack('protobuf_version'))
        suffix = f', request_id={request_id[:16]}...' if request_id else ''
        log.info(f'Sent protobuf_version ({len(version_msg)} bytes{suffix})')

    async def send_features(self, request_id=None):
        features_json = '[' + FEATURES + ']'
        features_hash = hashlib.md5(features_json.encode()).hexdigest()
        fields = {
            2: self.token,
            3: FIRMWARE_VERSION,
            4: HW_VERSION,
            5: PROTOCOL_VERSION,
            6: features_json,
            7: features_hash,
        }
        if request_id:
            fields[8] = request_id
        features_msg = encode_message(fields)
        await self.sio_emit('features', features_msg, callback=self._log_ack('features'))
        suffix = f', request_id={request_id[:16]}...' if request_id else ''
        log.info(f'Sent features ({len(features_msg)} bytes{suffix})')

    async def _send_post_auth(self):
        # Firmware parity: emit the post-auth sequence immediately after the
        # auth ACK. The server closes a session that stays silent after
        # camera_authentication, so the old 0.3s/0.2s pacing lost the session
        # before send_sio_info went out.
        if not self.sio.connected:
            log.warning('post-auth aborted: session closed before send_sio_info')
            return
        info_msg = encode_message({1: self.fingerprint, 2: self.token})
        await self.sio_emit('send_sio_info', info_msg)
        log.info(f'Sent send_sio_info ({len(info_msg)} bytes)')

        if not self.sio.connected:
            log.warning('post-auth aborted: session closed before status')
            return
        await self.send_status()

        if not self.sio.connected:
            return
        await self.send_protobuf_version()

        if not self.sio.connected:
            return
        await self.send_features()

    async def connect(self):
        try:
            await self._connect_once()
        except Exception as e:
            # Let the supervisor retry with a fresh client rather than aborting
            # startup (firmware CheckSocketServerConnection behaviour).
            log.warning(f'initial signaling connect failed: {e}; supervisor will retry')

    async def _connect_once(self):
        await self.sio.connect(
            'https://camera-signaling.prusa3d.com',
            auth={'token': self.token},
            headers={
                'Origin': 'https://connect.prusa3d.com',
                'User-Agent': f'Mozilla/5.0 (Linux; rv1106) Buddy3D/{FIRMWARE_VERSION}',
            },
            transports=['websocket'],
            wait_timeout=10,
        )

    async def supervise(self):
        """Own the reconnect loop (firmware ``CheckSocketServerConnection``).

        The server can close the signaling WebSocket right after
        ``camera_authentication``. When that happens, drop the whole client and
        open a new one so the next attempt cannot resume the dead session. The
        retry interval backs off exponentially (15s -> 120s cap) so a server-side
        rejection is not hammered while it is in effect.

        A half-open connection needs no extra probe: engineio's read loop times
        out after ``ping_interval + ping_timeout`` (25s + 20s) and resets the
        state, which this loop then observes. The body is guarded so an
        unexpected error can never kill reconnection.
        """
        delay = 15
        failures = 0
        while True:
            try:
                await asyncio.sleep(delay)
                eio_state = getattr(self.sio.eio, 'state', '?')
                alive = bool(self.sio.connected) and eio_state == 'connected'
                if alive:
                    if failures:
                        log.info(f'signaling link recovered after {failures} failed attempt(s)')
                    failures = 0
                    delay = 15
                    continue
                failures += 1
                log.warning(
                    f'signaling link down (sio.connected={self.sio.connected}, '
                    f'eio={eio_state}, attempt {failures}); reconnecting with a fresh '
                    f'session (next retry in {delay}s)'
                )
                try:
                    await self.sio.disconnect()
                except Exception:
                    pass
                self.sio = self._new_client()
                self._setup_handlers()
                try:
                    await self._connect_once()
                except Exception as e:
                    log.warning(f'signaling reconnect failed: {e}')
                delay = min(delay * 2, 120)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Never let an unexpected error end reconnection.
                log.error(f'signaling supervisor error: {e}; continuing')
                await asyncio.sleep(5)

    async def wait(self):
        await self.sio.wait()

    def on_trigger(self, handler):
        self._event_handler = handler
