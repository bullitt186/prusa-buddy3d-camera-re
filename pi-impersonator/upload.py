import aiohttp

async def upload_snapshot(jpeg_bytes, token, fingerprint, server='webcam.connect.prusa3d.com'):
    headers = {
        'User-Agent': 'Buddy3D Camera',
        'Token': token,
        'Fingerprint': fingerprint,
        'Content-Type': 'image/jpg',
    }
    async with aiohttp.ClientSession() as session:
        async with session.put(
            f'https://{server}/c/snapshot',
            headers=headers,
            data=jpeg_bytes
        ) as resp:
            return resp.status

async def upload_info(token, fingerprint, mac, ip, ssid, server='webcam.connect.prusa3d.com',
                       width=1920, height=1080, camera_name='Buddy3D Camera'):
    import json
    from features import FEATURES_LIST, FIRMWARE_VERSION, MODEL, MANUFACTURER, TRIGGER_SCHEME

    info = {
        'config': {
            'path': 'private',
            'name': camera_name,
            'driver': 'private',
            'model': MODEL,
            'firmware': FIRMWARE_VERSION,
            'manufacturer': MANUFACTURER,
            'trigger_scheme': TRIGGER_SCHEME,
            'resolution': {'width': width, 'height': height},
            'network_info': {'wifi_mac': mac, 'wifi_ipv4': ip, 'wifi_ssid': ssid},
        },
        'options': {
            'available_resolutions': [{'width': width, 'height': height}]
        },
        'capabilities': ['trigger_scheme'],
        'features': FEATURES_LIST,
    }
    headers = {
        'User-Agent': 'Buddy3D Camera',
        'Token': token,
        'Fingerprint': fingerprint,
        'Content-Type': 'application/json',
    }
    async with aiohttp.ClientSession() as session:
        async with session.put(
            f'https://{server}/c/info',
            headers=headers,
            data=json.dumps(info)
        ) as resp:
            return resp.status, await resp.text()
