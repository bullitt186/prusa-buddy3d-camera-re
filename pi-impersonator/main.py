import asyncio
import configparser
import json
import logging
import os
import subprocess
import sys
import time
from camera import capture_jpeg
from upload import make_session, upload_snapshot, upload_info
from signaling import PrusaSignaling
from local_http import start_local_http
from webrtc import PrusaWebRTC
from identity import resolve_fingerprint
from state import CameraState, ENUM_TO_RAW, snapshot_interval_from_config
from http_result import SUCCESS
from info_service import (
    countdown_after_result,
    info_dirty_after_result,
    next_info_action,
)
from scheduling import next_deadline, time_until
from proto import (
    WEBRTC_ANSWER,
    WEBRTC_CANDIDATE,
    WEBRTC_OFFER,
    WEBRTC_REQUEST,
    decode_camera_webrtc_message,
    decode_ice_config,
    decode_message,
    encode_camera_webrtc_message,
    encode_message,
)
import device_control
import quality
import quality_control
import rtsp_control
import trigger
import webrtc_control
import local_http
import timezone
import ota
import timelapse

logging.basicConfig(
    # stdout only → journald (Storage=volatile, RAM). No SD-card log writes: the Pi
    # power-cycles with the printer, so we avoid continuous writes that a cut could corrupt.
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s | %(message)s',
    datefmt='%H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('prusa-cam')

# One shared runtime state object: command handlers mutate it, and status,
# /c/info, snapshots and the encoder all read the same values (GAP-QUALITY-03,
# GAP-STATUS-01, GAP-CONTROL-01, GAP-SNAPSHOT-01/02).
state = CameraState()

# GAP-QUALITY-02: the firmware registration callback that assigns the nonzero
# persistence flag to the indirectly-registered Socket.IO events has NOT been
# recovered. Do not map the flag by event name yet; both default to no-persist so
# an unknown/failed path can never falsely persist. This gap remains partially open.
QUALITY_EVENT_PERSIST = {'change_video_size': False, 'save_video_size': False}


def apply_live_quality(raw_byte):
    """Reconfigure the live encoder for a raw quality byte.

    GAP-QUALITY-01: raw 5/6/7 -> protobuf enum 1/2/3 -> SD/HD/FHD.
    GAP-QUALITY-02: live-only path; state updates only after the live change
    (write + restart) succeeds. Review fix 1: a failed restart restores the
    previous live override so the encoder cannot later run the failed tier.
    """
    return quality_control.apply_live_quality(raw_byte, state, quality_control.restart_services)


def persist_quality(qenum):
    """Persist tier `qenum` for rpicam-source's boot EnvironmentFile (GAP-QUALITY-02)."""
    quality_control.persist_quality(qenum)


def handle_quality(raw_byte, persist):
    """Shared GAP-QUALITY-02 handler: always live-apply; persist only on flag.

    The control flow lives in stdlib-only ``quality_control`` so it is testable
    without ``gi``/``aiohttp``/systemd.
    """
    return quality_control.handle_quality(raw_byte, persist, apply_live_quality, persist_quality)

def rtsp_streaming():
    # ponytail: /proc/net/tcp check — no subprocess, detects active RTSP client
    # port 8888 = 0x22B8; look for ESTABLISHED (01) connections in hex
    try:
        with open('/proc/net/tcp') as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                # local_address field is hex IP:PORT; port is after the colon
                if fields[1].split(':')[1] == '22B8' and fields[3] == '01':
                    return True
    except Exception:
        pass
    return False

def redact_secrets(value, token, fingerprint):
    if isinstance(value, str):
        for secret in (token, fingerprint):
            if secret:
                value = value.replace(secret, f'<redacted:{len(secret)}>')
        return value
    if isinstance(value, dict):
        return {key: redact_secrets(item, token, fingerprint) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item, token, fingerprint) for item in value]
    return value

def summarize_info_response(body, token, fingerprint):
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return redact_secrets(body[:1000], token, fingerprint)

    config = payload.get('config') if isinstance(payload.get('config'), dict) else {}
    summary = {
        'origin': payload.get('origin'),
        'registered': payload.get('registered'),
        'printer_uuid': payload.get('printer_uuid'),
        'team_id': payload.get('team_id'),
        'config.name': config.get('name'),
        'config.model': config.get('model'),
        'features': payload.get('features'),
        'capabilities': payload.get('capabilities'),
    }
    return redact_secrets(summary, token, fingerprint)

def load_config():
    cfg = configparser.ConfigParser()
    # config.ini sits next to this script — deploy-path independent
    cfg.read(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.ini'))
    return cfg

def get_network_info(configured_fingerprint=None):
    """Return ``(mac, ip, ssid, fingerprint)``.

    Fingerprint precedence: an explicit ``config.ini`` ``[identity] fingerprint``
    wins (the registered token is bound to it), then the firmware-style
    MAC-derived value, then the persisted fallback seed when the ``wlan0`` MAC is
    unreadable (GAP-IDENTITY-01). ``mac`` is reported empty when there is no
    hardware address to report.
    """
    try:
        raw_mac = open('/sys/class/net/wlan0/address').read().strip()
    except OSError as e:
        log.warning(f'wlan0 MAC unreadable ({e}); using configured or fallback identity')
        raw_mac = ''
    mac, fingerprint = resolve_fingerprint(configured_fingerprint, raw_mac)
    if configured_fingerprint:
        log.info('Using fingerprint from config.ini [identity] fingerprint')
    elif not mac:
        log.warning(
            'Using persisted fallback identity: fingerprint derived from a stored '
            'seed, not a hardware MAC'
        )
    # The interface may be absent entirely on alternate/recovery hardware; keep
    # startup alive with empty fields rather than raising after the MAC fallback.
    try:
        ip_out = subprocess.run(['ip', '-4', 'addr', 'show', 'wlan0'], capture_output=True, text=True).stdout
        ip = next((l.split()[1].split('/')[0] for l in ip_out.splitlines() if 'inet ' in l), '')
    except OSError:
        ip = ''
    try:
        ssid_out = subprocess.run(['nmcli', '-t', '-f', 'active,ssid', 'dev', 'wifi'], capture_output=True, text=True).stdout
        ssid = next((l.split(':', 1)[1] for l in ssid_out.splitlines() if l.startswith('yes:')), '')
    except OSError:
        ssid = ''
    return mac, ip, ssid, fingerprint


def reboot_device():
    """Narrowly scoped reboot: only the intended systemd command (GAP-DEVICE-01).

    Returns True only when systemctl reports success; the caller never fakes it.
    """
    result = subprocess.run(['sudo', 'systemctl', 'reboot'], capture_output=True)
    return result.returncode == 0


def rtsp_service_start():
    subprocess.run(['sudo', 'systemctl', 'start', 'prusa-rtsp.service'], capture_output=True)


def rtsp_service_stop():
    subprocess.run(['sudo', 'systemctl', 'stop', 'prusa-rtsp.service'], capture_output=True)


def rtsp_service_active():
    """Return the real unit state, or None when it cannot be probed.

    ``None`` (systemd missing, unit not installed, unknown output) makes the
    caller fall back to the commanded state, which is what non-Pi hosts need.
    """
    try:
        show = subprocess.run(
            ['systemctl', 'show', 'prusa-rtsp.service', '--property=LoadState', '--value'],
            capture_output=True, text=True,
        )
    except OSError:
        return None
    if show.returncode != 0 or show.stdout.strip() in ('', 'not-found'):
        return None
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', 'prusa-rtsp.service'], capture_output=True, text=True
        )
    except OSError:
        return None
    value = result.stdout.strip()
    if value in ('active', 'activating', 'reloading'):
        return True
    if value in ('inactive', 'failed', 'deactivating'):
        return False
    return None

async def wait_for_deadline_or_change(deadline):
    """Sleep until an absolute monotonic deadline, waking early on interval change.

    GAP-SNAPSHOT-01/04: the interval-change event lets a new cadence take effect
    immediately; the deadline is absolute so capture/upload duration does not
    inflate the start-to-start cadence.
    """
    remaining = time_until(deadline)
    if remaining <= 0:
        return
    try:
        await asyncio.wait_for(state.snapshot_interval_changed.wait(), timeout=remaining)
    except asyncio.TimeoutError:
        return
    state.snapshot_interval_changed.clear()


async def snapshot_loop(token, fingerprint, server, session):
    last_start = time.monotonic()
    while True:
        # Schedule the next start from the previous cycle's start, not from now.
        deadline = next_deadline(last_start, state.snapshot_interval)
        await wait_for_deadline_or_change(deadline)
        last_start = time.monotonic()
        if state.periodic_snapshot_allowed(rtsp_streaming()):
            width, height = state.resolution()
            try:
                jpeg = capture_jpeg(width, height)
                local_http.last_jpeg = jpeg
                t0 = time.monotonic()
                status, result_class = await upload_snapshot(
                    session, jpeg, token, fingerprint, server
                )
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                log.info(f'Snapshot: {status} ({result_class}, {len(jpeg)} bytes, {elapsed_ms}ms)')
            except Exception as e:
                log.error(f'Snapshot error: {redact_secrets(str(e), token, fingerprint)}')
        elif not state.snapshot_upload_enabled:
            log.debug('snapshot loop paused (upload disabled)')
        else:
            log.debug('snapshot loop paused (streaming active)')

async def ota_checkin(token, fingerprint, session):
    """Query the OTA endpoint and classify the release (GAP-OTA-01).

    Policy (owner decision): truthful decline — classify and log, never flash.
    """
    from features import FIRMWARE_VERSION
    headers = {
        'User-Agent': 'Buddy3D Camera',
        'X-Camera-Token': token,
        'X-Camera-Fingerprint': fingerprint,
        'X-Camera-FW-Version': FIRMWARE_VERSION,
    }
    try:
        async with session.get(ota.OTA_ENDPOINT, headers=headers) as resp:
            if resp.status != 200:
                log.warning(f'OTA check-in: HTTP {resp.status}')
                return
            body = await resp.text()
    except Exception as e:
        log.warning(f'OTA check-in failed: {e}')
        return
    decision = ota.classify(FIRMWARE_VERSION, body)
    if decision == ota.UP_TO_DATE:
        log.info(f'OTA: up to date ({FIRMWARE_VERSION})')
    elif decision == ota.UPDATE_AVAILABLE:
        log.info('OTA: update available — declining (no firmware flashing on the Pi)')
    elif decision == ota.FORCED_UPDATE:
        log.warning('OTA: forced update advertised — declining (no firmware flashing on the Pi)')
    else:
        log.warning(f'OTA: unusable response: {redact_secrets(body[:200], token, fingerprint)}')


async def ota_loop(token, fingerprint, session):
    """Periodic OTA check-in (firmware polls the endpoint on an interval)."""
    while True:
        await asyncio.sleep(ota.OTA_CHECK_INTERVAL)
        await ota_checkin(token, fingerprint, session)


def decline_firmware_update(source):
    """GAP-OTA-01: explicit unsupported result for a remote update request."""
    log.warning(f'OTA: {source} requested — declined ({ota.decline_reason()})')


def _find_candidate(msg):
    """Return the ICE candidate text from a decoded WebRTC message, or ''.

    The viewer trickles candidates as ``a=candidate:...`` (SDP attribute form);
    GStreamer's add-ice-candidate wants the value without the ``a=`` prefix.
    """
    def normalize(text):
        if text.startswith('a='):
            text = text[2:]
        return text if text.startswith('candidate:') else ''

    def scan(value):
        if isinstance(value, bytes):
            found = normalize(value.decode('utf-8', 'replace'))
            if found:
                return found
            try:
                inner = decode_message(value)
            except Exception:
                return ''
            if isinstance(inner, dict):
                for v in inner.values():
                    found = scan(v)
                    if found:
                        return found
        elif isinstance(value, str):
            return normalize(value)
        return ''

    for value in msg.get('raw', {}).values():
        found = scan(value)
        if found:
            return found
    return ''


async def timelapse_loop():
    """Capture and store timelapse frames while enabled (GAP-TIMELAPSE-01)."""
    while True:
        if not state.timelapse_enabled:
            await asyncio.sleep(1)
            continue
        try:
            jpeg = capture_jpeg(*state.resolution())
            index = timelapse.next_index(timelapse.TIMELAPSE_DIR)
            path = timelapse.save_frame(jpeg, timelapse.TIMELAPSE_DIR, index)
            log.info(f'Timelapse: stored {os.path.basename(path)}')
        except Exception as e:
            log.warning(f'Timelapse capture failed: {e}')
        await asyncio.sleep(state.timelapse_interval)


def _find_sdp(msg):
    """Return the SDP text from a decoded camera-side WebRTC message, or ''.

    The offer's SDP is a length-delimited field; locate it by its ``v=0`` header
    rather than guessing a tag while the field mapping is still being recovered.
    """
    for value in msg.get('raw', {}).values():
        if isinstance(value, bytes) and b'v=0' in value:
            return value.decode('utf-8', 'replace')
        if isinstance(value, str) and 'v=0' in value:
            return value
    return ''


async def detect_timezone(session):
    """GAP-STATUS-04: detect the timezone from the web API and persist ``/etc/TZ``.

    Firmware ``FUN_000b1dc8`` GETs ``timezone.prusa3d.com/`` and reads the JSON
    ``timezone`` field; ``FUN_000b1620`` swaps the ``UTC+``/``UTC-`` prefix into
    the POSIX form and ``FUN_000b170c`` writes ``/etc/TZ``. The status message
    reports that content (``FUN_000b130c``).
    """
    try:
        async with session.get(timezone.TIMEZONE_URL) as resp:
            if resp.status != 200:
                log.warning(f'timezone API: HTTP {resp.status}')
                return
            body = await resp.text()
    except Exception as e:
        log.warning(f'timezone API error: {e}')
        return
    raw = timezone.parse_timezone_response(body)
    if not raw:
        log.warning('timezone API: no usable timezone in response')
        return
    state.tz_name = timezone.resolve_tz_name(raw)
    log.info(f'timezone: API {raw!r} -> reported {state.tz_name!r}')


async def info_service_loop(token, fingerprint, server, session, mac, ip, ssid):
    """One-second ``/c/info`` dirty/retry service loop (GAP-INFO-01).

    Mirrors firmware ``FW-INFO-LOOP``: attempt when dirty and the countdown hits
    zero, clear dirty on success, otherwise keep dirty and reload the countdown.
    ``http_result`` bounds consecutive transient retries and stops on ordinary
    4xx so the loop can never hot-spin or retry a permanent client error.
    """
    countdown = 0
    failures = 0
    while True:
        await asyncio.sleep(1)
        attempt, countdown = next_info_action(state.info_dirty, countdown)
        if not attempt:
            if not state.info_dirty:
                failures = 0
            continue
        try:
            status, result_class, _ = await upload_info(
                session, state, token, fingerprint, mac, ip, ssid, server
            )
        except Exception as e:
            # Never let an unexpected error kill the service task and permanently
            # disable /c/info refresh; treat it as a transient failure.
            log.error(f'/c/info service refresh raised: {redact_secrets(str(e), token, fingerprint)}')
            state.info_dirty = info_dirty_after_result('connection_error', failures)
            countdown = countdown_after_result('connection_error', failures)
            failures += 1
            continue
        if result_class == SUCCESS:
            state.info_dirty = False
            countdown = 0
            failures = 0
            log.info(f'/c/info service refresh: {status}')
            continue
        state.info_dirty = info_dirty_after_result(result_class, failures)
        countdown = countdown_after_result(result_class, failures)
        failures += 1
        log.warning(
            f'/c/info service refresh failed: status={status} class={result_class} '
            f'dirty={state.info_dirty} retry_in={countdown}s'
        )


async def main():
    cfg = load_config()
    token = cfg['identity']['token']
    server = cfg['upload']['server']

    # GAP-QUALITY-03: start from the persisted tier and publish it everywhere.
    qenum, _, _ = quality.read_current()
    state.set_quality(qenum)
    width, height = state.resolution()
    log.info(f'Loaded persisted quality enum {qenum} ({width}x{height})')

    # Review fix 3: the old snapshot loop read cfg['upload']['interval']; seed the
    # shared state so a configured interval is honored (validated 10..600).
    interval_raw = cfg.get('upload', 'interval', fallback='10')
    interval = snapshot_interval_from_config(interval_raw)
    if interval is None:
        log.warning(
            f'config upload.interval {interval_raw!r} invalid (must be 10..600); '
            f'using {state.snapshot_interval}s'
        )
    else:
        state.set_snapshot_interval(interval)
        # Startup seed: no snapshot loop is waiting yet, so clear the wake event.
        state.snapshot_interval_changed.clear()
        log.info(f'Loaded snapshot interval {state.snapshot_interval}s from config')

    # GAP-RTSP-02: load the configured mode and make boot behavior follow it.
    # The mode is persisted at /etc/prusa-cam/rtsp.mode (overlay: durable only
    # once deployed); at startup we read it and reconcile the real unit state.
    configured_rtsp_mode = rtsp_control.read_mode()
    rtsp_control.apply_mode(
        configured_rtsp_mode, state,
        start_service=rtsp_service_start,
        stop_service=rtsp_service_stop,
        query_service=rtsp_service_active,
    )
    log.info(
        f'RTSP configured mode={state.rtsp_mode} '
        f'(1=disabled/2=enabled) running={state.rtsp_running}'
    )

    mac, ip, ssid, fingerprint = get_network_info(cfg['identity'].get('fingerprint'))

    # GAP-HTTP-03: one session for the whole application lifetime, reused by the
    # info service loop, snapshots and the OTA check-in; closed on shutdown.
    session = make_session()
    status, result_class, body = await upload_info(
        session, state, token, fingerprint, mac, ip, ssid, server
    )
    log.info(f'/c/info upload: {status} ({result_class})')
    # Same dirty policy as the service loop: success clears, transient keeps
    # dirty for bounded retry, ordinary 4xx stops rather than retrying.
    state.info_dirty = info_dirty_after_result(result_class, 0)
    summary = summarize_info_response(body, token, fingerprint)
    origin = summary.get('origin') if isinstance(summary, dict) else None
    registered = summary.get('registered') if isinstance(summary, dict) else None
    log.info(f'/c/info response: origin={origin!r} registered={registered!r} summary={summary!r}')
    await ota_checkin(token, fingerprint, session)
    await detect_timezone(session)

    sig = PrusaSignaling(fingerprint, token, state, mac=mac, ip=ip, ssid=ssid)
    loop = asyncio.get_event_loop()

    async def on_webrtc_offer(request_id, sdp_text):
        msg = encode_camera_webrtc_message(
            token, request_id, fingerprint, WEBRTC_OFFER, sdp=sdp_text
        )
        await sig.sio_emit('webrtc', msg)
        log.info(f'Sent WebRTC offer for {request_id[:16]}... ({len(sdp_text)} chars)')

    async def on_ice_candidate(request_id, candidate, mline_index):
        msg = encode_camera_webrtc_message(
            token, request_id, fingerprint, WEBRTC_CANDIDATE,
            candidate=candidate, mid=str(mline_index),
        )
        await sig.sio_emit('webrtc', msg)

    webrtc = PrusaWebRTC(on_offer=on_webrtc_offer, on_ice_candidate=on_ice_candidate)
    webrtc.start()

    def start_webrtc_service():
        # GLib loop already running → just report status (GAP-WEBRTC-04).
        if not webrtc.is_running:
            webrtc.start()

    def stop_webrtc_service():
        if webrtc.is_running:
            webrtc.stop()
        # A stopped service has no peer; do not leave snapshots paused forever.
        state.streaming = False

    async def dispatch_trigger_action(action, request_id):
        """Perform exactly one planned trigger action (GAP-TRIGGER-01).

        Actions are selected by ``trigger.trigger_actions`` from the recovered
        descriptor; this function only executes them. ``reboot`` is a
        rate-limited, narrowly scoped systemd reboot (GAP-DEVICE-01).
        ``fw_update`` returns an explicit unsupported result (GAP-OTA-01);
        ``timelapse_*`` are recognized but intentionally unimplemented and must
        not perform an unrelated action or fake success.
        """
        if action == trigger.STATUS:
            await sig.send_status(request_id=request_id)
        elif action == trigger.FEATURES:
            await sig.send_features(request_id=request_id)
        elif action == trigger.PROTOCOL_INFO:
            await sig.send_protobuf_version(request_id=request_id)
        elif action == trigger.SNAPSHOT:
            # Immediate get-snapshot is independent of the periodic
            # snapshot_upload_enabled switch (GAP-SNAPSHOT-02); it keeps the
            # existing WebRTC pause only.
            if state.streaming:
                log.info('Trigger snapshot skipped: WebRTC stream active')
                return
            try:
                jpeg = capture_jpeg(*state.resolution())
                await upload_snapshot(session, jpeg, token, fingerprint, server)
            except Exception as e:
                log.error(
                    f'Trigger snapshot error: {redact_secrets(str(e), token, fingerprint)}'
                )
        elif action in (trigger.SNAPSHOT_ENABLE, trigger.SNAPSHOT_DISABLE):
            trigger.apply_snapshot_upload(action, state)
            log.info(f'Trigger: snapshot_upload_enabled={state.snapshot_upload_enabled}')
        elif action in (trigger.RTSP_START, trigger.RTSP_STOP):
            mode = (rtsp_control.RTSP_ENABLED if action == trigger.RTSP_START
                    else rtsp_control.RTSP_DISABLED)
            rtsp_control.apply_mode(
                mode, state,
                start_service=rtsp_service_start,
                stop_service=rtsp_service_stop,
                query_service=rtsp_service_active,
                persist=rtsp_control.write_mode,
            )
            log.info(f'Trigger {action}: mode={state.rtsp_mode} running={state.rtsp_running}')
        elif action == trigger.REBOOT:
            # GAP-DEVICE-01: the trigger dispatcher is the only path here. The
            # guard rejects a second request inside its window and never fakes
            # success when the systemd command fails.
            accepted = device_control.request_reboot(state, reboot_device)
            log.info(f'Trigger reboot: accepted={accepted}')
        elif action == trigger.FW_UPDATE:
            # GAP-OTA-01: truthful decline; no firmware is flashed on the Pi.
            decline_firmware_update('trigger fw_update')
        elif action in (trigger.TIMELAPSE_ENABLE, trigger.TIMELAPSE_DISABLE):
            if timelapse.apply_enable(action, state):
                log.info(f'Trigger {action}: timelapse_enabled={state.timelapse_enabled}')
        elif action == trigger.TIMELAPSE_MAKE:
            path = timelapse.build_mjpeg(timelapse.TIMELAPSE_DIR)
            if path:
                log.info(f'Trigger timelapse_make: wrote {path}')
            else:
                log.warning('Trigger timelapse_make: no frames stored')
        elif action == trigger.TIMELAPSE_FILE_LIST:
            frames = timelapse.list_frames(timelapse.TIMELAPSE_DIR)
            log.warning(
                f'Trigger timelapse_file_list: {len(frames)} stored frame(s); the '
                'file-list envelope annotations are unresolved, no list sent (GAP-TIMELAPSE-01)'
            )
        else:
            log.warning(
                f'Trigger action {action!r} recognized but not implemented on the '
                f'Pi impersonator; no action performed'
            )

    async def handle_event(event, data):
        if event == 'webrtc' and isinstance(data, bytes):
            # Recovered 9-field camera-side schema (descriptor 0x3f7680). The
            # previous flat 12-field decoder mis-read this and ignored the offer.
            msg = decode_camera_webrtc_message(data)
            log.info(
                f'WebRTC event: request_id={msg["request_id"][:16]!r} '
                f'client={msg["client_id"][:16]!r} session={msg["session_id"][:16]!r} '
                f'f5={msg["field5"]} f6={msg["field6"]} f7={msg["field7"]} '
                f'ice_len={len(msg["ice_config"])} f9_len={len(msg["field9"])} '
                f'keys={sorted(msg["raw"].keys())}'
            )
            if msg['ice_config']:
                # GAP-WEBRTC-01: Connect sends the ICE server config first
                # (tag8 = repeated {id, host, port, type}). The camera is the
                # WebRTC OFFERER (firmware FUN_000b996c): create the peer
                # connection with these servers and send an offer.
                servers, turn_user, turn_cred = decode_ice_config(msg['ice_config'])
                log.info(
                    f'WebRTC ICE servers: {servers} '
                    f'(turn_user={"set" if turn_user else "none"}, '
                    f'cred={"set" if turn_cred else "none"})'
                )
                if not webrtc_control.offer_allowed(state):
                    log.warning(
                        'WebRTC start rejected: service disabled '
                        f'(mode={state.webrtc_mode}, status={state.webrtc_status})'
                    )
                    return
                if not msg['request_id']:
                    log.error('Ignoring WebRTC start without request ID')
                    return
                state.streaming = True
                log.info('Pausing snapshots for WebRTC stream')
                await asyncio.sleep(1)
                # The server routes the offer to the viewer using the inbound
                # client/session id (tag2/tag3), not the camera token.
                webrtc.create_offer(
                    msg['client_id'] or msg['session_id'] or msg['request_id'],
                    servers, loop, turn_user, turn_cred,
                )
            elif msg['field5'] == WEBRTC_CANDIDATE:
                # Viewer trickle-ICE candidate (tag5=4, tag4.1=candidate,
                # tag2=mid). These were previously ignored, so the connection
                # never completed.
                candidate = _find_candidate(msg)
                if candidate:
                    webrtc.add_ice_candidate(candidate)
                    log.info(f'WebRTC inbound candidate added (mid={msg["client_id"]})')
                else:
                    log.warning('WebRTC candidate message carried no candidate')
            elif _find_sdp(msg):
                # The viewer's answer to our offer.
                webrtc.handle_answer(msg['request_id'], _find_sdp(msg))
            else:
                log.info('WebRTC message carried no SDP or candidate (ignored)')
        elif event == 'trigger' and isinstance(data, bytes):
            decoded = trigger.decode_trigger(data)
            request_id = decoded.request_id or None
            log.info(
                f'Trigger received decoded={redact_secrets(dict(decoded), token, fingerprint)!r} '
                f'request_id={request_id[:16] + "..." if request_id else None}'
            )
            # Tag 13 is decoded and logged above but has no recovered semantics;
            # never act on it (GAP-TRIGGER-01).
            actions = trigger.trigger_actions(decoded)
            if not actions:
                log.warning('Trigger: no recognized action; nothing dispatched')
                return
            for action in actions:
                await dispatch_trigger_action(action, request_id)
        elif event == 'configuration' and isinstance(data, bytes):
            # GAP-CONFIG-01: the configuration wire format is JSON, confirmed
            # from the binary (nlohmann::json: ./xhr_tools/nlohmann/json.hpp and
            # the parser FUN_0006f9dc reached via the SIO handler FUN_000c4718 ->
            # FUN_0006fb94 -> FUN_0006cf34). The previous generic protobuf
            # fallback and numeric-tag lookups were guesses; both are removed.
            try:
                msg = json.loads(data)
            except Exception as e:
                log.warning(f'Configuration: not valid JSON ({e}); ignoring')
                return
            if not isinstance(msg, dict):
                log.warning(f'Configuration: expected a JSON object, got {type(msg).__name__}')
                return
            log.info(f'Configuration: {redact_secrets(msg, token, fingerprint)!r}')
            # FW-CONFIG:49-74,286-300: the leading `code` rejects "42"/"66".
            code = msg.get('code')
            if code is not None and str(code) in ('42', '66'):
                log.warning(f'Config: code {code!r} rejected')
                return
            name = msg.get('camera_name')
            if name is not None:
                if state.set_camera_name(name):
                    # GAP-CONTROL-01/GAP-INFO-01: the service loop republishes
                    # /c/info with the new name.
                    state.mark_info_dirty()
                    log.info(f'Config: camera_name → {state.camera_name!r}')
                else:
                    log.warning(f'Config: camera_name {name!r} rejected (empty)')
            interval_val = msg.get('snapshot_interval')
            if interval_val is not None:
                if state.set_snapshot_interval(interval_val):
                    log.info(f'Config: snapshot_interval → {interval_val}s (live)')
                else:
                    log.warning(f'Config: snapshot_interval {interval_val!r} rejected (10..600)')
            vq = msg.get('video_quality')
            if vq is not None:
                qenum = {'sd': 1, 'hd': 2, 'fhd': 3}.get(str(vq).lower())
                if qenum is None:
                    log.warning(f'Config: video_quality {vq!r} not recognized (sd/hd/fhd)')
                else:
                    raw = ENUM_TO_RAW.get(qenum)
                    # ASSUMPTION (GAP-CONFIG-01/GAP-QUALITY-02): the dispatch table
                    # confirms sd/hd/fhd -> raw 5/6/7 but not that this path
                    # persists; persist=True here is an inference, not evidence.
                    if raw is not None and handle_quality(raw, persist=True):
                        state.mark_info_dirty()
                        log.info(f'Config: video_quality → {vq} (enum {qenum}, {state.resolution()})')
            lc = msg.get('light_control')
            if lc is not None:
                # GAP-DEVICE-02: no IR illuminator on the Pi; the policy logs the
                # request and rejects it, leaving state.ir_mode unavailable. Never
                # report a mode that was not applied.
                applied = device_control.apply_light_control(lc, state)
                log.info(f'Config: light_control {lc!r} applied={applied}')
            rtsp = msg.get('rtsp')
            if rtsp is not None:
                # GAP-RTSP-02: configuration form and direct event share one path.
                rtsp_mode = rtsp_control.mode_from_config(rtsp)
                if rtsp_mode is None:
                    log.warning(f'Config: rtsp {rtsp!r} not recognized (expected on/off)')
                else:
                    rtsp_control.apply_mode(
                        rtsp_mode, state,
                        start_service=rtsp_service_start,
                        stop_service=rtsp_service_stop,
                        query_service=rtsp_service_active,
                        persist=rtsp_control.write_mode,
                    )
                    log.info(
                        f'Config: rtsp → mode={state.rtsp_mode} '
                        f'running={state.rtsp_running}'
                    )
            wrtc = msg.get('webrtc')
            if wrtc is not None:
                requested = None
                if isinstance(wrtc, str):
                    requested = {'on': 1, 'off': 0}.get(wrtc.strip().lower())
                if requested is None:
                    log.warning(f'Config: webrtc {wrtc!r} not recognized (expected on/off)')
                elif webrtc_control.apply_mode(
                    requested, state,
                    start_service=start_webrtc_service,
                    stop_service=stop_webrtc_service,
                ):
                    log.info(
                        f'Config: webrtc → mode={state.webrtc_mode} '
                        f'status={state.webrtc_status}'
                    )
                    # FW-CONFIG:108-140: the paired rule — `webrtc on` also
                    # forces RTSP disabled.
                    if requested == 1 and state.rtsp_mode != 1:
                        rtsp_control.apply_mode(
                            1, state,
                            start_service=rtsp_service_start,
                            stop_service=rtsp_service_stop,
                            query_service=rtsp_service_active,
                            persist=rtsp_control.write_mode,
                        )
                        log.info('Config: webrtc on → RTSP forced disabled (paired rule)')
            fw = msg.get('start_fw_update')
            if fw is not None and str(fw).lower() == 'start':
                decline_firmware_update('start_fw_update')
        elif event == 'set_rtsp_server_mode':
            rtsp_mode = rtsp_control.decode_mode(data)
            if rtsp_mode is None:
                log.warning('set_rtsp_server_mode: invalid payload (expected field 1 = 1/2)')
            else:
                rtsp_control.apply_mode(
                    rtsp_mode, state,
                    start_service=rtsp_service_start,
                    stop_service=rtsp_service_stop,
                    query_service=rtsp_service_active,
                    persist=rtsp_control.write_mode,
                )
                log.info(
                    f'set_rtsp_server_mode: mode={state.rtsp_mode} '
                    f'running={state.rtsp_running}'
                )
        elif event == 'set_webrtc_mode':
            requested = webrtc_control.decode_mode(data)
            if requested is None:
                log.warning('set_webrtc_mode: invalid payload (expected field 1 = 0/1)')
            elif webrtc_control.apply_mode(
                requested, state,
                start_service=start_webrtc_service,
                stop_service=stop_webrtc_service,
            ):
                log.info(
                    f'set_webrtc_mode: mode={state.webrtc_mode} '
                    f'status={state.webrtc_status}'
                )
        elif event in ('change_video_size', 'save_video_size'):
            val = data[0] if isinstance(data, (bytes, bytearray)) and data else None
            # GAP-QUALITY-01/02: raw 5/6/7 -> SD/HD/FHD, live apply always, persist
            # only per the (currently unresolved) event flag map.
            persist = QUALITY_EVENT_PERSIST.get(event, False)
            if handle_quality(val, persist):
                # GAP-INFO-01/02: republish the quality-derived resolution.
                state.mark_info_dirty()
                log.info(f'{event}: quality → raw {val} (enum {state.quality}, {state.resolution()})')
            else:
                log.warning(f'{event}: quality byte {val!r} not fully applied (live or persist failed)')
        elif event == 'timelapse_get_file_list':
            # GAP-TIMELAPSE-01: the firmware file-list envelope (4 string fields,
            # descriptor 0x3f701c) has no recovered per-field annotation, so an
            # untyped empty message is NOT sent. Report the explicit unsupported
            # result instead of a malformed list.
            frames = timelapse.list_frames(timelapse.TIMELAPSE_DIR)
            log.warning(
                f'timelapse_get_file_list: {len(frames)} stored frame(s); the file-list '
                'envelope annotations are unresolved, no list emitted (GAP-TIMELAPSE-01)'
            )

    sig.on_trigger(handle_event)
    asyncio.create_task(snapshot_loop(token, fingerprint, server, session))
    asyncio.create_task(info_service_loop(token, fingerprint, server, session, mac, ip, ssid))
    asyncio.create_task(ota_loop(token, fingerprint, session))
    asyncio.create_task(timelapse_loop())
    asyncio.create_task(start_local_http())
    try:
        await sig.connect()
        # WP-1: own the reconnect loop so a server-closed session is replaced
        # with a fresh client (firmware CheckSocketServerConnection parity).
        asyncio.create_task(sig.supervise())
        await asyncio.Event().wait()
    finally:
        # GAP-HTTP-03: release the single long-lived session on shutdown.
        await session.close()

if __name__ == '__main__':
    asyncio.run(main())
