import logging
from aiohttp import web
from onvif_facade import soap_response

log = logging.getLogger('prusa-cam.http')

# Populated by main.py's snapshot loop; served instantly to avoid slow on-demand capture.
last_jpeg: bytes = b''

async def handle_snapshot(request):
    jpeg = last_jpeg
    if not jpeg:
        try:
            from camera import capture_jpeg
            from quality import read_current
            # Review fix 5: one state drives snapshots — use the shared quality
            # resolution instead of a hardcoded 1920x1080.
            _, width, height = read_current()
            jpeg = capture_jpeg(width, height)
        except Exception as e:
            log.error(f'local /snapshot.jpg cold-start capture: {e}')
            return web.Response(status=503, text='Camera unavailable')
    return web.Response(body=jpeg, content_type='image/jpeg')

async def handle_root(request):
    return web.Response(text='Buddy3D Camera', content_type='text/plain')

async def handle_onvif(request):
    context = request.app['onvif_context']
    service = request.match_info['service']
    payload = await request.read()
    status, body = soap_response(service, context, payload)
    return web.Response(
        status=status,
        body=body,
        content_type='application/soap+xml',
        charset='utf-8',
    )


async def start_local_http(onvif_context=None):
    app = web.Application()
    app.router.add_get('/', handle_root)
    app.router.add_get('/snapshot.jpg', handle_snapshot)
    if onvif_context is not None:
        app['onvif_context'] = onvif_context
        app.router.add_post('/onvif/{service:device|media}_service', handle_onvif)
    runner = web.AppRunner(app, access_log=log)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 80)
    await site.start()
    log.info('Local HTTP/ONVIF server started on port 80')
    return runner
