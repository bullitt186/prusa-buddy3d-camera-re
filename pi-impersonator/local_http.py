import logging
from aiohttp import web
from camera import capture_jpeg

log = logging.getLogger('prusa-cam.http')

async def handle_snapshot(request):
    try:
        jpeg = capture_jpeg(1920, 1080)
        return web.Response(body=jpeg, content_type='image/jpeg')
    except Exception as e:
        log.error(f'local /snapshot.jpg: {e}')
        return web.Response(status=503, text='Camera unavailable')

async def handle_root(request):
    return web.Response(text='Buddy3D Camera', content_type='text/plain')

async def start_local_http():
    app = web.Application()
    app.router.add_get('/', handle_root)
    app.router.add_get('/snapshot.jpg', handle_snapshot)
    runner = web.AppRunner(app, access_log=log)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 80)
    await site.start()
    log.info('Local HTTP server started on port 80')
