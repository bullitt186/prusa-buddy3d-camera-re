import asyncio
import logging
import subprocess
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GstWebRTC, GstSdp, GLib
import threading

Gst.init(None)
log = logging.getLogger('prusa-cam.webrtc')

class PrusaWebRTC:
    def __init__(self, on_answer, on_ice_candidate):
        self._on_answer = on_answer
        self._on_ice = on_ice_candidate
        self._pipe = None
        self._webrtc = None
        self._request_id = None
        self._loop = None
        self._glib_loop = None
        self._glib_thread = None
        self._proc = None

    def start(self):
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

    def handle_offer(self, request_id, sdp_text, loop):
        self._request_id = request_id
        self._loop = loop

        self._teardown()

        # Start rpicam-vid subprocess
        self._proc = subprocess.Popen(
            ['rpicam-vid', '--codec', 'h264', '-t', '0',
             '--width', '1280', '--height', '720', '--framerate', '30',
             '--inline', '--profile', 'baseline', '--level', '3.1',
             '-o', '-'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )

        fd = self._proc.stdout.fileno()
        pipeline_str = (
            f'fdsrc fd={fd} ! h264parse config-interval=-1 '
            '! rtph264pay config-interval=1 pt=96 '
            '! application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000 '
            '! webrtcbin name=webrtc bundle-policy=max-bundle stun-server=stun://stun.l.google.com:19302'
        )

        self._pipe = Gst.parse_launch(pipeline_str)
        self._webrtc = self._pipe.get_by_name('webrtc')

        self._webrtc.connect('on-negotiation-needed', self._on_negotiation_needed)
        self._webrtc.connect('on-ice-candidate', self._on_ice_candidate_cb)

        self._pipe.set_state(Gst.State.PLAYING)
        log.info('Pipeline set to PLAYING')

        # Set remote offer
        res, sdp_msg = GstSdp.SDPMessage.new_from_text(sdp_text)
        if res != GstSdp.SDPResult.OK:
            log.error(f'Failed to parse SDP offer: {res}')
            return
        offer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.OFFER, sdp_msg
        )
        promise = Gst.Promise.new()
        self._webrtc.emit('set-remote-description', offer, promise)
        promise.wait()
        log.info('Remote description set')

        # Create answer
        promise = Gst.Promise.new_with_change_func(self._on_answer_created)
        self._webrtc.emit('create-answer', None, promise)

    def _on_negotiation_needed(self, element):
        pass

    def _on_ice_candidate_cb(self, element, mline_index, candidate):
        if self._loop and self._on_ice:
            self._loop.call_soon_threadsafe(
                asyncio.ensure_future,
                self._on_ice(self._request_id, candidate, mline_index)
            )

    def _on_answer_created(self, promise):
        promise.wait()
        reply = promise.get_reply()
        if reply is None:
            log.error('Answer creation returned no reply')
            return
        answer = reply.get_value('answer')
        if answer is None:
            log.error('Failed to create answer')
            return

        promise2 = Gst.Promise.new()
        self._webrtc.emit('set-local-description', answer, promise2)
        promise2.wait()

        sdp_text = answer.sdp.as_text()
        log.info(f'Answer SDP created ({len(sdp_text)} chars)')

        if self._loop and self._on_answer:
            self._loop.call_soon_threadsafe(
                asyncio.ensure_future,
                self._on_answer(self._request_id, sdp_text)
            )

    def add_ice_candidate(self, candidate, sdp_mline_index=0):
        if self._webrtc:
            self._webrtc.emit('add-ice-candidate', sdp_mline_index, candidate)
