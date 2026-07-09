import asyncio
import binascii
import os
import socketio
import logging
import hashlib
import time
from proto import Float32, encode_message, decode_message
from features import PROTOCOL_VERSION, FEATURES, FIRMWARE_VERSION, MODEL, MANUFACTURER, HW_VERSION

log = logging.getLogger('prusa-cam.signaling')

class PrusaSignaling:
    def __init__(self, fingerprint, token, mac='', ip='', ssid=''):
        self.fingerprint = fingerprint
        self.token = token
        self.mac = mac
        self.ip = ip
        self.ssid = ssid
        self.sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=0,
            reconnection_delay=5,
        )
        self._event_handler = None
        self._setup_handlers()

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
            log.info(f'Auth ACK: {ack!r}')
        except Exception as e:
            log.error(f'Auth failed: {e}')
            return
        await self._send_post_auth()

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

    def _uptime_string(self, seconds):
        days, rem = divmod(max(0, seconds), 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        return f'{days} days, {hours}:{minutes}:{seconds}'

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
        signal_quality = self._signal_quality()
        uptime = self._uptime_seconds()
        memory = self._memory_stats()

        timelapse_status = encode_message({
            1: 2,
            2: 0,
            3: 0,
            4: '',
            5: 0,
            6: 0,
            7: Float32(0.0),
        })

        camera_status = encode_message({
            3: 1,
            4: 10,
            5: 1,
            6: 40,
        })

        network_info = encode_message({
            1: encode_message({
                1: self.ssid,
                2: self.mac,
                3: self.ip,
                5: signal_quality,
            }),
            2: encode_message({}),
        })

        extended_status = encode_message({
            1: FIRMWARE_VERSION,
            2: MODEL,
            3: 'Buddy3D Camera',
            4: encode_message({
                1: 2,
                2: 0,
                3: 0,
                4: 0,
                6: MODEL,
            }),
            6: encode_message({
                1: 1,
                2: 2,
                4: f'rtsp://{self.ip}:8554/live' if self.ip else '',
            }),
            7: encode_message({
                1: 1,
                2: 0,
            }),
            9: encode_message({
                1: 'webcam.connect.prusa3d.com',
                2: 'camera-signaling.prusa3d.com',
                3: 'connect.prusa3d.com',
            }),
            10: encode_message({
                1: time.tzname[0] if time.tzname else '',
                2: 1,
            }),
            11: encode_message({
                1: 1,   # webrtc_mode: enabled (RE confirmed +0x13d=1 after set_webrtc_mode)
                2: 1,   # webrtc_status: running (RE confirmed +0x13e=1 after service starts)
            }),
        })

        system_info = encode_message({
            1: Float32(self._cpu_temperature()),
            2: uptime,
            3: self._uptime_string(uptime),
            4: self._load_average(),
            5: memory['MemTotal'],
            6: memory['MemFree'],
            7: memory['Shmem'],
            8: memory['Buffers'],
            9: self._process_count(),
        })

        video_quality = encode_message({1: 3})
        fields = {
            2: timelapse_status,
            3: camera_status,
            4: network_info,
            5: extended_status,
            8: self.token,
            9: system_info,
            10: self.sio.get_sid() or self.sio.sid or '',
            11: video_quality,
        }
        return encode_message(fields)

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
        await asyncio.sleep(0.3)
        info_msg = encode_message({1: self.fingerprint, 2: self.token})
        await self.sio_emit('send_sio_info', info_msg)
        log.info(f'Sent send_sio_info ({len(info_msg)} bytes)')

        await asyncio.sleep(0.2)
        await self.send_status()

        await asyncio.sleep(0.2)
        await self.send_protobuf_version()

        await asyncio.sleep(0.2)
        await self.send_features()

    async def connect(self):
        await self.sio.connect(
            'https://camera-signaling.prusa3d.com',
            auth={'token': self.token},
            headers={
                'Origin': 'https://connect.prusa3d.com',
                'User-Agent': 'Mozilla/5.0 (Linux; rv1106) Buddy3D/3.1.5',
            },
            transports=['websocket'],
            wait_timeout=10,
        )

    async def wait(self):
        await self.sio.wait()

    def on_trigger(self, handler):
        self._event_handler = handler
