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
    from features import FIRMWARE_VERSION
    headers = {
        'User-Agent': 'Buddy3D Camera',
        'X-Camera-Token': token,
        'X-Camera-Fingerprint': fingerprint,
        'X-Camera-FW-Version': FIRMWARE_VERSION,
    }
    try:
        async with session.get(
            'https://connect-ota.prusa3d.com/api/niceboy/v1/camera',
            headers=headers
        ) as resp:
            body = await resp.text()
            log.info(f'OTA check-in: {resp.status} {redact_secrets(body[:200], token, fingerprint)}')
    except Exception as e:
        log.warning(f'OTA check-in failed: {e}')


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

    async def on_webrtc_answer(request_id, sdp_text):
        msg = encode_camera_webrtc_message(request_id, WEBRTC_ANSWER, sdp_text)
        await sig.sio_emit('webrtc', msg)
        log.info(f'Sent WebRTC answer for {request_id[:16]}...')

    async def on_ice_candidate(request_id, candidate, mline_index):
        msg = encode_camera_webrtc_message(request_id, WEBRTC_CANDIDATE, candidate)
        await sig.sio_emit('webrtc', msg)

    webrtc = PrusaWebRTC(on_answer=on_webrtc_answer, on_ice_candidate=on_ice_candidate)
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
        ``fw_update``/``timelapse_*`` are recognized but intentionally
        unimplemented and must not perform an unrelated action or fake success.
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
        else:
            log.warning(
                f'Trigger action {action!r} recognized but not implemented on the '
                f'Pi impersonator; no action performed'
            )

    async def handle_event(event, data):
        if event == 'webrtc' and isinstance(data, bytes):
            msg = decode_camera_webrtc_message(data)
            request_id = msg['request_id']
            msg_type = msg['msg_type']
            payload = msg['payload']
            log.info(
                f'WebRTC event: type={msg_type} id={request_id[:16]}... '
                f'client={msg["client_id"][:16]}... payload_len={len(payload)}'
            )
            if msg_type == WEBRTC_OFFER:
                if not webrtc_control.offer_allowed(state):
                    log.warning(
                        'WebRTC offer rejected: service disabled '
                        f'(mode={state.webrtc_mode}, status={state.webrtc_status})'
                    )
                    return
                if not request_id or not payload:
                    log.error('Ignoring malformed WebRTC offer without request ID or SDP')
                    return
                state.streaming = True
                log.info('Pausing snapshots for WebRTC stream')
                await asyncio.sleep(1)
                webrtc.handle_offer(request_id, payload, loop)
            elif msg_type == WEBRTC_CANDIDATE:
                # GStreamer's add-ice-candidate expects the attribute value,
                # while some server messages include the SDP "a=" prefix.
                candidate = payload[2:] if payload.startswith('a=') else payload
                webrtc.add_ice_candidate(candidate)
            elif msg_type == WEBRTC_REQUEST:
                # lp_app 3.1.6 logs this as unsupported; it is not a teardown.
                log.warning('Ignoring unsupported WebRTC request/start message')
            else:
                log.warning(f'Ignoring unknown camera-side WebRTC message type {msg_type}')
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
            try:
                msg = json.loads(data)
            except Exception:
                msg = decode_message(data)
            log.info(f'Configuration: {redact_secrets(msg, token, fingerprint)!r}')
            name = msg.get('camera_name') or msg.get(7)
            if name:
                if state.set_camera_name(name):
                    # GAP-CONTROL-01/GAP-INFO-01: the service loop republishes
                    # /c/info with the new name.
                    state.mark_info_dirty()
                    log.info(f'Config: camera_name → {state.camera_name!r}')
                else:
                    log.warning(f'Config: camera_name {name!r} rejected (empty)')
            interval_val = msg.get('snapshot_interval') or msg.get(8)
            if interval_val is not None:
                if state.set_snapshot_interval(interval_val):
                    log.info(f'Config: snapshot_interval → {interval_val}s (live)')
                else:
                    log.warning(f'Config: snapshot_interval {interval_val!r} rejected (10..600)')
            vq = msg.get('video_quality') or msg.get(4)
            if vq and str(vq).lower() in ('sd', 'hd', 'fhd'):
                qenum = {'sd': 1, 'hd': 2, 'fhd': 3}[str(vq).lower()]
                raw = ENUM_TO_RAW.get(qenum)
                # ASSUMPTION (GAP-CONFIG-01/GAP-QUALITY-02): the dispatch table
                # confirms sd/hd/fhd -> raw 5/6/7, but it does NOT confirm that the
                # configuration-form video_quality path persists. persist=True here
                # is an inference, not recovered evidence.
                if raw is not None and handle_quality(raw, persist=True):
                    # GAP-INFO-01/02: republish the quality-derived resolution.
                    state.mark_info_dirty()
                    log.info(f'Config: video_quality → {vq} (enum {qenum}, {state.resolution()})')
            lc = msg.get('light_control') or msg.get(6)
            if lc:
                # GAP-DEVICE-02: no IR illuminator on the Pi; the policy logs the
                # request and rejects it, leaving state.ir_mode unavailable. Never
                # report a mode that was not applied.
                applied = device_control.apply_light_control(lc, state)
                log.info(f'Config: light_control {lc!r} applied={applied}')
            rtsp = msg.get('rtsp') or msg.get(2)
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
            wrtc = msg.get('webrtc') or msg.get(3)
            if wrtc is not None:
                # GAP-WEBRTC-04: the recovered dispatch table maps webrtc on/off
                # to set_webrtc_mode(1/0). The paired rule that `on` also forces
                # RTSP disabled is not implemented here.
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
            fw = msg.get('start_fw_update') or msg.get(5)
            if fw:
                log.warning('Config: start_fw_update requested — not supported on Pi impersonator')
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
            log.info('timelapse_get_file_list: no SD card on Pi, responding with empty list')
            empty = encode_message({})
            await sig.sio_emit('timelapse_get_file_list', empty)

    sig.on_trigger(handle_event)
    asyncio.create_task(snapshot_loop(token, fingerprint, server, session))
    asyncio.create_task(info_service_loop(token, fingerprint, server, session, mac, ip, ssid))
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
