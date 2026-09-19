import asyncio
import logging
import subprocess
import quality
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GstWebRTC, GstSdp, GLib
import threading

Gst.init(None)
log = logging.getLogger('prusa-cam.webrtc')

class PrusaWebRTC:
    def __init__(self, on_offer, on_ice_candidate):
        self._on_offer = on_offer
        self._on_ice = on_ice_candidate
        self._pipe = None
        self._webrtc = None
        self._request_id = None
        self._loop = None
        self._glib_loop = None
        self._glib_thread = None
        self._proc = None

    @property
    def is_running(self):
        """True while the GLib main loop owning the WebRTC pipeline is active."""
        return self._glib_loop is not None

    def start(self):
        if self._glib_loop is not None:
            return
        self._glib_loop = GLib.MainLoop()
        self._glib_thread = threading.Thread(target=self._glib_loop.run, daemon=True)
        self._glib_thread.start()

    def stop(self):
        self._teardown()
        if self._glib_loop:
            self._glib_loop.quit()
            self._glib_loop = None

    def _teardown(self):
        if self._pipe:
            self._pipe.set_state(Gst.State.NULL)
            self._pipe = None
            self._webrtc = None
        if self._proc:
            self._proc.terminate()
            self._proc = None

    def create_offer(self, request_id, ice_servers, loop):
        """Camera-side offer (firmware is the offerer; FUN_000b996c).

        Connect first sends the ICE server configuration; the camera then builds
        the peer connection with those servers and emits an offer. The viewer
        returns an answer (``handle_answer``) and ICE is trickled.
        """
        self._request_id = request_id
        self._loop = loop

        self._teardown()

        # Start rpicam-vid subprocess at the current quality tier (parity with the
        # RTSP source). --rotation 180 matches the physical (inverted) camera mount.
        _, w, h = quality.read_current()
        self._proc = subprocess.Popen(
            ['rpicam-vid', '--codec', 'h264', '-t', '0',
             '--width', str(w), '--height', str(h), '--framerate', '30',
             '--rotation', '180', '--intra', '30', '--flush',
             '--inline', '--profile', 'baseline', '--level', '3.1',
             '-o', '-'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )

        fd = self._proc.stdout.fileno()
        stun = ''
        for server in ice_servers or []:
            if server.get('host') and server.get('type') in (1, 3):
                stun = f" stun-server=stun://{server['host']}:{server['port']}"
                break
        pipeline_str = (
            f'fdsrc fd={fd} ! h264parse config-interval=-1 '
            '! rtph264pay config-interval=1 pt=96 '
            '! application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000 '
            f'! webrtcbin name=webrtc bundle-policy=max-bundle{stun}'
        )

        self._pipe = Gst.parse_launch(pipeline_str)
        self._webrtc = self._pipe.get_by_name('webrtc')

        self._webrtc.connect('on-ice-candidate', self._on_ice_candidate_cb)
        self._pipe.set_state(Gst.State.PLAYING)
        log.info(f'Pipeline set to PLAYING (stun={stun.strip() or "none"})')

        promise = Gst.Promise.new_with_change_func(self._on_offer_created)
        self._webrtc.emit('create-offer', None, promise)

    def handle_answer(self, request_id, sdp_text):
        """Apply the viewer's answer to the existing peer connection."""
        self._request_id = request_id
        if self._webrtc is None:
            log.warning('WebRTC answer received with no peer connection')
            return
        res, sdp_msg = GstSdp.SDPMessage.new_from_text(sdp_text)
        if res != GstSdp.SDPResult.OK:
            log.error(f'Failed to parse SDP answer: {res}')
            return
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, sdp_msg
        )
        promise = Gst.Promise.new()
        self._webrtc.emit('set-remote-description', answer, promise)
        promise.wait()
        log.info('Remote answer set')

    def _on_negotiation_needed(self, element):
        pass

    def _on_ice_candidate_cb(self, element, mline_index, candidate):
        if self._loop and self._on_ice:
            self._loop.call_soon_threadsafe(
                asyncio.ensure_future,
                self._on_ice(self._request_id, candidate, mline_index)
            )

    def _on_offer_created(self, promise):
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer') if reply is not None else None
        if offer is None:
            log.error('Offer creation returned no reply')
            return

        promise2 = Gst.Promise.new()
        self._webrtc.emit('set-local-description', offer, promise2)
        promise2.wait()

        sdp_text = offer.sdp.as_text()
        log.info(f'Offer SDP created ({len(sdp_text)} chars)')

        if self._loop and self._on_offer:
            self._loop.call_soon_threadsafe(
                asyncio.ensure_future,
                self._on_offer(self._request_id, sdp_text)
            )

    def add_ice_candidate(self, candidate, sdp_mline_index=0):
        if self._webrtc:
            self._webrtc.emit('add-ice-candidate', sdp_mline_index, candidate)
