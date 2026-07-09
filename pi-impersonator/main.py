import asyncio
import configparser
import json
import logging
import subprocess
import sys
import time
from camera import capture_jpeg
from upload import upload_snapshot, upload_info
from signaling import PrusaSignaling
from local_http import start_local_http
from webrtc import PrusaWebRTC
from proto import encode_message, decode_message

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s | %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/var/log/prusa-cam/main.log', mode='a'),
    ]
)
log = logging.getLogger('prusa-cam')

streaming = False
current_quality = 3

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

def extract_request_id(msg):
    for field in (10, 3, 1):
        value = msg.get(field)
        if isinstance(value, str) and len(value) >= 8:
            return value
    for value in msg.values():
        if isinstance(value, bytes):
            nested = decode_message(value)
            nested_id = extract_request_id(nested)
            if nested_id:
                return nested_id
    return None

def load_config():
    cfg = configparser.ConfigParser()
    cfg.read('/home/pi/prusa-cam/config.ini')
    return cfg

def get_network_info():
    mac = open('/sys/class/net/wlan0/address').read().strip()
    ip = subprocess.run(['ip', '-4', 'addr', 'show', 'wlan0'], capture_output=True, text=True).stdout
    ip = [l.split()[1].split('/')[0] for l in ip.splitlines() if 'inet ' in l][0]
    ssid_out = subprocess.run(['nmcli', '-t', '-f', 'active,ssid', 'dev', 'wifi'], capture_output=True, text=True).stdout
    ssid = next((l.split(':', 1)[1] for l in ssid_out.splitlines() if l.startswith('yes:')), '')
    return mac, ip, ssid

async def snapshot_loop(cfg):
    token = cfg['identity']['token']
    fingerprint = cfg['identity']['fingerprint']
    width = cfg.getint('camera', 'width')
    height = cfg.getint('camera', 'height')
    interval = cfg.getint('upload', 'interval')
    server = cfg['upload']['server']

    while True:
        if not streaming and not rtsp_streaming():
            try:
                jpeg = capture_jpeg(width, height)
                t0 = time.monotonic()
                status = await upload_snapshot(jpeg, token, fingerprint, server)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                log.info(f'Snapshot: {status} ({len(jpeg)} bytes, {elapsed_ms}ms)')
            except Exception as e:
                log.error(f'Snapshot error: {e}')
        else:
            log.debug('snapshot loop paused (streaming active)')
        await asyncio.sleep(interval)

async def ota_checkin(token, fingerprint):
    import aiohttp
    from features import FIRMWARE_VERSION
    headers = {
        'User-Agent': 'Buddy3D Camera',
        'X-Camera-Token': token,
        'X-Camera-Fingerprint': fingerprint,
        'X-Camera-FW-Version': FIRMWARE_VERSION,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                'https://connect-ota.prusa3d.com/api/niceboy/v1/camera',
                headers=headers
            ) as resp:
                body = await resp.text()
                log.info(f'OTA check-in: {resp.status} {body[:200]}')
    except Exception as e:
        log.warning(f'OTA check-in failed: {e}')


async def main():
    global streaming
    cfg = load_config()
    token = cfg['identity']['token']
    fingerprint = cfg['identity']['fingerprint']
    width = cfg.getint('camera', 'width')
    height = cfg.getint('camera', 'height')
    server = cfg['upload']['server']

    mac, ip, ssid = get_network_info()
    status, body = await upload_info(token, fingerprint, mac, ip, ssid, server, width, height)
    log.info(f'/c/info upload: {status}')
    summary = summarize_info_response(body, token, fingerprint)
    origin = summary.get('origin') if isinstance(summary, dict) else None
    registered = summary.get('registered') if isinstance(summary, dict) else None
    log.info(f'/c/info response: origin={origin!r} registered={registered!r} summary={summary!r}')
    await ota_checkin(token, fingerprint)

    sig = PrusaSignaling(fingerprint, token, mac=mac, ip=ip, ssid=ssid)
    loop = asyncio.get_event_loop()

    async def on_webrtc_answer(request_id, sdp_text):
        msg = encode_message({1: request_id, 2: 'answer', 3: sdp_text})
        await sig.sio.emit('webrtc', msg)
        log.info(f'Sent WebRTC answer for {request_id[:16]}...')

    async def on_ice_candidate(request_id, candidate, mline_index):
        msg = encode_message({1: request_id, 2: 'candidate', 3: candidate})
        await sig.sio.emit('webrtc', msg)

    webrtc = PrusaWebRTC(on_answer=on_webrtc_answer, on_ice_candidate=on_ice_candidate)
    webrtc.start()

    async def handle_event(event, data):
        global streaming
        if event == 'webrtc' and isinstance(data, bytes):
            msg = decode_message(data)
            request_id = msg.get(1, '')
            msg_type = msg.get(2, '')
            sdp = msg.get(3, '')
            log.info(f'WebRTC event: type={msg_type} id={request_id[:16]}...')
            if msg_type == 'offer':
                streaming = True
                log.info('Pausing snapshots for WebRTC stream')
                await asyncio.sleep(1)
                webrtc.handle_offer(request_id, sdp, loop)
            elif msg_type == 'candidate':
                webrtc.add_ice_candidate(sdp)
            elif msg_type == 'request':
                streaming = False
                webrtc._teardown()
                log.info('WebRTC stream ended, resuming snapshots')
        elif event == 'trigger' and isinstance(data, bytes):
            msg = decode_message(data)
            request_id = extract_request_id(msg)
            log.info(f'Trigger received decoded={redact_secrets(msg, token, fingerprint)!r} request_id={request_id[:16] + "..." if request_id else None}')
            if request_id:
                await sig.send_status(request_id=request_id)
                await asyncio.sleep(0.2)
                await sig.send_protobuf_version(request_id=request_id)
                await asyncio.sleep(0.2)
                await sig.send_features(request_id=request_id)
            log.info('Trigger received, uploading snapshot')
            if not streaming:
                try:
                    jpeg = capture_jpeg(width, height)
                    await upload_snapshot(jpeg, token, fingerprint, server)
                except Exception as e:
                    log.error(f'Trigger snapshot error: {e}')
        elif event == 'configuration' and isinstance(data, bytes):
            try:
                msg = json.loads(data)
            except Exception:
                msg = decode_message(data)
            log.info(f'Configuration: {redact_secrets(msg, token, fingerprint)!r}')
            name = msg.get('camera_name') or msg.get(7)
            if name:
                log.info(f'Config: camera_name → {name!r}')
            interval_val = msg.get('snapshot_interval') or msg.get(8)
            if interval_val and isinstance(interval_val, int) and 10 <= interval_val <= 600:
                log.info(f'Config: snapshot_interval → {interval_val}s (live change not implemented)')
            vq = msg.get('video_quality') or msg.get(4)
            if vq and str(vq).lower() in ('sd', 'hd', 'fhd'):
                qmap = {'sd': 1, 'hd': 2, 'fhd': 3}
                current_quality = qmap.get(str(vq).lower(), current_quality)
                log.info(f'Config: video_quality → {vq} (enum {current_quality})')
            lc = msg.get('light_control') or msg.get(6)
            if lc:
                log.info(f'Config: light_control → {lc!r} (Pi has no IR, ignored)')
            rtsp = msg.get('rtsp') or msg.get(2)
            if rtsp:
                log.info(f'Config: rtsp → {rtsp!r}')
            wrtc = msg.get('webrtc') or msg.get(3)
            if wrtc:
                log.info(f'Config: webrtc → {wrtc!r}')
            fw = msg.get('start_fw_update') or msg.get(5)
            if fw:
                log.warning('Config: start_fw_update requested — not supported on Pi impersonator')
        elif event == 'set_rtsp_server_mode':
            val = data[0] if isinstance(data, (bytes, bytearray)) and data else None
            if val == 2:
                log.info('set_rtsp_server_mode: enabling RTSP')
                subprocess.run(['sudo', 'systemctl', 'start', 'prusa-rtsp.service'], capture_output=True)
            elif val == 1:
                log.info('set_rtsp_server_mode: disabling RTSP')
                subprocess.run(['sudo', 'systemctl', 'stop', 'prusa-rtsp.service'], capture_output=True)
            else:
                log.warning(f'set_rtsp_server_mode: unknown value {val!r}')
        elif event in ('change_video_size', 'save_video_size'):
            val = data[0] if isinstance(data, (bytes, bytearray)) and data else None
            qmap = {5: 'HD', 6: 'FHD', 7: 'SD'}
            qenum = {5: 2, 6: 3, 7: 1}
            if val in qmap:
                current_quality = qenum[val]
                log.info(f'{event}: quality → {qmap[val]} (enum {current_quality}; live reconfigure not supported)')
            else:
                log.warning(f'{event}: unknown quality byte {val!r}')
        elif event == 'timelapse_get_file_list':
            log.info('timelapse_get_file_list: no SD card on Pi, responding with empty list')
            empty = encode_message({})
            await sig.sio_emit('timelapse_get_file_list', empty)

    sig.on_trigger(handle_event)
    asyncio.create_task(snapshot_loop(cfg))
    asyncio.create_task(start_local_http())
    await sig.connect()
    await sig.wait()

if __name__ == '__main__':
    asyncio.run(main())
