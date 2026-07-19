import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import Gst, GstRtspServer, GLib

Gst.init(None)

server = GstRtspServer.RTSPServer()
server.set_service('8554')

factory = GstRtspServer.RTSPMediaFactory()
factory.set_launch(
    '( tcpclientsrc host=127.0.0.1 port=8888 do-timestamp=true '
    '! h264parse config-interval=-1 ! rtph264pay name=pay0 pt=96 )'
)
factory.set_shared(True)
factory.set_latency(0)  # drop the default 200ms server-side jitter buffer

mounts = server.get_mount_points()
mounts.add_factory('/live', factory)

server.attach(None)

print('RTSP server running at rtsp://0.0.0.0:8554/live')
loop = GLib.MainLoop()
loop.run()
