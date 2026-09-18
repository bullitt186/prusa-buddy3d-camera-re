import asyncio

import aiohttp

import info_body
from http_result import classify_exception, classify_status

# GAP-HTTP-03: one session is created for the application lifetime (see
# main.py) and passed into these functions; they never build their own. Bounded
# timeouts prevent a hung request from pinning the service loop.
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
USER_AGENT = 'Buddy3D Camera'


def make_session():
    """Create the application-lifetime HTTP session with bounded timeouts."""
    return aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT)


async def upload_snapshot(session, jpeg_bytes, token, fingerprint,
                          server='webcam.connect.prusa3d.com'):
    """PUT a JPEG snapshot; returns ``(status_or_None, result_class)``.

    GAP-HTTP-02: the status is classified into the firmware result classes.
    Redirects are not auto-followed: firmware ``FUN_0005c568`` only accepts
    ``200``/``204`` and treats ``403`` as blocked, so following a redirect would
    not be firmware-equivalent.
    """
    headers = {
        'User-Agent': USER_AGENT,
        'Token': token,
        'Fingerprint': fingerprint,
        'Content-Type': 'image/jpg',
    }
    try:
        async with session.put(
            f'https://{server}/c/snapshot',
            headers=headers,
            data=jpeg_bytes,
            expect100=True,  # GAP-HTTP-01: firmware sends Expect: 100-continue
            allow_redirects=False,
        ) as resp:
            return resp.status, classify_status(resp.status)
    except asyncio.TimeoutError as e:
        return None, classify_exception(e)
    except aiohttp.ClientError as e:
        return None, classify_exception(e)
    except OSError as e:
        return None, classify_exception(e)


async def upload_info(session, state, token, fingerprint, mac, ip, ssid,
                      server='webcam.connect.prusa3d.com'):
    """PUT the shared-state ``/c/info`` body; returns ``(status, class, body)``.

    GAP-INFO-02: the body is built from ``CameraState`` (name, quality-derived
    resolution, network values, constants) so it stays consistent with status
    and the encoder. The caller supplies the shared session (GAP-HTTP-03).
    """
    body = info_body.build_info_json(state, mac=mac, ip=ip, ssid=ssid)
    headers = {
        'User-Agent': USER_AGENT,
        'Token': token,
        'Fingerprint': fingerprint,
        'Content-Type': 'application/json',
    }
    try:
        async with session.put(
            f'https://{server}/c/info',
            headers=headers,
            data=body,
            allow_redirects=False,
        ) as resp:
            return resp.status, classify_status(resp.status), await resp.text()
    except asyncio.TimeoutError as e:
        return None, classify_exception(e), ''
    except aiohttp.ClientError as e:
        return None, classify_exception(e), ''
    except OSError as e:
        return None, classify_exception(e), ''
