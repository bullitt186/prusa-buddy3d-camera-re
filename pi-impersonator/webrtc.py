import asyncio
import logging
import os
import subprocess
import quality
import webrtc_lifecycle
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GstWebRTC, GstSdp, GLib
import threading

Gst.init(None)
log = logging.getLogger('prusa-cam.webrtc')

# GAP-WEBRTC-06: the candidate-type fields' wire form is NOT confirmed. The live
# server rejected a numeric encoding (fields 2/3 as bytes) with the `error` event
# "webrtc_connection_info - Error: Read past limit", so the numeric-vs-string
# question must be settled from a genuine capture (or the C++ message's .proto)
# before we ship it. The sender and event stay implemented but are gated off by
# default; enable with PRUSA_WEBRTC_CONNECTION_INFO=1 for an experiment.
CONNECTION_INFO_ENABLED = os.environ.get('PRUSA_WEBRTC_CONNECTION_INFO', '') == '1'

def _munge_offer(sdp_text):
    """Restructure the GStreamer offer to match the firmware's libdatachannel one.

    Diffing a reference libdatachannel offer (same library the firmware uses)
    against webrtcbin's showed: libdatachannel puts ``a=mid`` first in the
    m-section and includes the session-level ``a=msid-semantic:WMS *`` and
    ``a=group:LS``, while webrtcbin emits ``a=mid`` after the ``a=ssrc`` lines
    and omits those. A strict JSEP answerer rejects that (the Connect viewer
    answered ``m=video 0``).
    """
    lines = [l for l in sdp_text.replace('\r\n', '\n').split('\n') if l]
    try:
        first_media = next(i for i, l in enumerate(lines) if l.startswith('m='))
    except StopIteration:
        return sdp_text
    session, media = lines[:first_media], lines[first_media:]
    mid = next((l for l in media if l.startswith('a=mid:')), '')
    media = [l for l in media if not l.startswith('a=mid:')]

    if mid:
        mid_value = mid.split(':', 1)[1]
        if not any(l.startswith('a=group:LS') for l in session):
            session.append(f'a=group:LS {mid_value}')
        if not any(l.startswith('a=msid-semantic') for l in session):
            session.append('a=msid-semantic:WMS *')

    has_c = any(l.startswith('c=') for l in media)
    out, inserted = [], False
    for line in media:
        out.append(line)
        if mid and not inserted:
            if line.startswith('c=') or (not has_c and line.startswith('m=')):
                out.append(mid)
                inserted = True
    return '\r\n'.join(session + out) + '\r\n'


def _strip_sprop(sdp_text):
    """Drop ``sprop-parameter-sets`` from the H264 fmtp.

    The firmware's libdatachannel offer omits it (the stream carries SPS/PPS
    inline via rpicam-vid ``--inline``). Do NOT rewrite ``profile-level-id``: the
    answerer validates the actual SPS, so overriding the fmtp to ``42e01f`` while
    the stream SPS is ``428029`` makes it stop answering entirely.
    """
    out = []
    for line in sdp_text.replace('\r\n', '\n').split('\n'):
        if line.startswith('a=fmtp:96 ') and 'sprop-parameter-sets' in line:
            params = [p for p in line[len('a=fmtp:96 '):].split(';')
                      if not p.startswith('sprop-parameter-sets=')]
            line = 'a=fmtp:96 ' + ';'.join(params)
        out.append(line)
    return '\r\n'.join(out)


def _structure_items(node):
    """Yield ``(field_name, value)`` for a GstStructure, defensively.

    ``get-stats`` is version-dependent, so a missing/odd member must never raise
    out of the GLib thread; an unusable node simply yields nothing.
    """
    n_fields = getattr(node, 'n_fields', None)
    if not callable(n_fields):
        return
    try:
        count = node.n_fields()
    except Exception:
        return
    for index in range(count):
        try:
            name = node.nth_field_name(index)
            value = node.get_value(name)
        except Exception:
            continue
        yield name, value


def _scan_candidate_types(node, side, found):
    """Recursively collect one local and one remote candidate ``typ``.

    The side is inferred from field names (``ice-local-candidates`` /
    ``local-candidate`` vs. the remote equivalents) and propagated into the
    nested candidate structures. Best-effort: an unrecognized stats shape finds
    nothing and the caller skips the event.
    """
    if len(found) >= 2:
        return
    for name, value in _structure_items(node):
        lowered = name.lower()
        child_side = side
        if 'local' in lowered:
            child_side = 'local'
        elif 'remote' in lowered:
            child_side = 'remote'
        if child_side and isinstance(value, str):
            typ = webrtc_lifecycle.parse_candidate_type(value)
            if typ and child_side not in found:
                found[child_side] = typ
        _scan_candidate_types(value, child_side, found)
    if not isinstance(node, (str, bytes)) and hasattr(node, '__iter__'):
        try:
            for item in node:
                _scan_candidate_types(item, side, found)
        except Exception:
            pass


class PrusaWebRTC:
    def __init__(self, on_offer, on_ice_candidate, on_stream_ended=None,
                 on_connection_info=None, on_teardown=None):
        self._on_offer = on_offer
        self._on_ice = on_ice_candidate
        self._on_stream_ended = on_stream_ended
        self._on_connection_info = on_connection_info
        self._on_teardown = on_teardown
        self._pipe = None
        self._webrtc = None
        self._request_id = None
        self._loop = None
        self._glib_loop = None
        self._glib_thread = None
        self._proc = None
        self._ice_connected = False
        self._ended_notified = False
        self._connection_info_sent = False
        self._disconnect_timeout_id = None
        self._connect_watchdog_id = None

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
        self._cancel_timeout('_disconnect_timeout_id')
        self._cancel_timeout('_connect_watchdog_id')
        self._ice_connected = False
        self._ended_notified = False
        self._connection_info_sent = False
        # GAP-WEBRTC-05: notify the owner that this peer is gone so its
        # TURN-client/quality-lock flag is cleared (stream-end also clears it).
        self._notify_teardown()
        # Disconnect the old pipeline's ICE-state handler BEFORE NULL so a stale
        # CLOSED signal cannot set _ended_notified and suppress the next session's
        # genuine end notification (GAP-WEBRTC-03 review note).
        if self._webrtc is not None:
            try:
                self._webrtc.disconnect_by_func(self._on_notify_ice_state)
            except Exception:
                pass
        if self._pipe:
            self._pipe.set_state(Gst.State.NULL)
            self._pipe = None
            self._webrtc = None
        if self._proc:
            self._proc.terminate()
            self._proc = None

    def create_offer(self, request_id, ice_servers, loop, username='', credential=''):
        """Camera-side offer (firmware is the offerer; FUN_000b996c).

        Connect first sends the ICE server configuration; the camera then builds
        the peer connection with those servers (STUN + TURN, including the
        time-limited TURN credentials) and emits an offer. The viewer returns an
        answer (``handle_answer``) and ICE is trickled.
        """
        self._request_id = request_id
        self._loop = loop

        self._teardown()

        # GAP-WEBRTC-02: reuse the always-running mux stream (port 8888) instead
        # of opening libcamera a second time — rpicam-source owns the sensor, so a
        # second rpicam-vid cannot capture and webrtcbin would have no media.
        stun_url = ''
        turn_host = ''
        for server in ice_servers or []:
            host = server.get('host')
            port = server.get('port')
            if not host or not port:
                continue
            if server.get('type') in (1, 3) and not stun_url:
                stun_url = f'stun://{host}:{port}'
            elif server.get('type') == 2 and not turn_host:
                turn_host = f'{host}:{port}'
        pipeline_str = (
            # 8889 = the SPS-patched mux stream (constrained baseline level 3.1),
            # which the Connect answerer requires. 8888 (RTSP/snapshots) is
            # untouched. See stream_mux.patch_sps.
            'tcpclientsrc host=127.0.0.1 port=8889 do-timestamp=true '
            '! h264parse config-interval=-1 '
            '! rtph264pay config-interval=1 pt=96 '
            '! application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000 '
            '! webrtcbin name=webrtc bundle-policy=max-bundle'
        )

        self._pipe = Gst.parse_launch(pipeline_str)
        self._webrtc = self._pipe.get_by_name('webrtc')

        # Configure the Connect-provided ICE servers. The TURN username is the
        # time-limited "timestamp:username" form, so escape ':' (and the base64
        # credential) as webrtcbin's turn-server property requires.
        if stun_url:
            self._webrtc.set_property('stun-server', stun_url)
        if turn_host and username and credential:
            from urllib.parse import quote
            turn_url = (
                f'turn://{quote(username, safe="")}:{quote(credential, safe="")}'
                f'@{turn_host}'
            )
            self._webrtc.set_property('turn-server', turn_url)
        log.info(
            f'ICE configured: stun={stun_url or "none"} turn={turn_host or "none"} '
            f'user={"set" if username else "none"} cred={"set" if credential else "none"}'
        )

        # Arm the lifecycle watchdog before any signal wiring: if a signal name
        # is wrong the offer flow must still fail safe instead of leaving
        # snapshots paused with no recovery path (GAP-WEBRTC-03).
        self._offer_started = False
        self._ice_connected = False
        self._ended_notified = False
        self._connection_info_sent = False
        self._disconnect_timeout_id = None
        self._connect_watchdog_id = GLib.timeout_add_seconds(
            30, self._connect_watchdog
        )

        def connect(signal, handler):
            try:
                self._webrtc.connect(signal, handler)
            except Exception as e:
                log.warning(f'could not connect {signal}: {e}')

        connect('on-ice-candidate', self._on_ice_candidate_cb)
        # webrtcbin exposes ice-connection-state as a readable property and has
        # no on-ice-connection-state-change signal (confirmed via gst-inspect),
        # so use the GObject property notify.
        connect('notify::ice-connection-state', self._on_notify_ice_state)
        connect('on-negotiation-needed', self._on_negotiation_needed)

        self._pipe.set_state(Gst.State.PLAYING)
        log.info('Pipeline set to PLAYING')
        # Standard webrtcbin flow: the offer is created from the
        # on-negotiation-needed signal. Keep a fallback in case it does not fire.
        GLib.timeout_add(3000, self._create_offer_timeout)

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
        log.info('WebRTC negotiation needed; creating offer')
        self._create_offer()

    def _create_offer_timeout(self):
        if not getattr(self, '_offer_started', False):
            log.info('WebRTC offer fallback timeout; creating offer')
            self._create_offer()
        return False

    def _create_offer(self):
        if self._webrtc is None or getattr(self, '_offer_started', False):
            return
        self._offer_started = True
        # The camera is a pure sender; offer sendonly rather than sendrecv.
        try:
            trans = self._webrtc.emit('get-transceiver', 0)
            if trans is not None:
                trans.set_property(
                    'direction', GstWebRTC.WebRTCRTPTransceiverDirection.SENDONLY
                )
                log.info('Transceiver direction set to sendonly')
        except Exception as e:
            log.warning(f'could not set transceiver direction: {e}')
        promise = Gst.Promise.new_with_change_func(self._on_offer_created)
        self._webrtc.emit('create-offer', None, promise)

    def _on_ice_candidate_cb(self, element, mline_index, candidate):
        if self._loop and self._on_ice:
            self._loop.call_soon_threadsafe(
                asyncio.ensure_future,
                self._on_ice(self._request_id, candidate, mline_index)
            )

    def _cancel_timeout(self, attr):
        timeout_id = getattr(self, attr, None)
        if timeout_id is not None:
            try:
                GLib.source_remove(timeout_id)
            except Exception as e:
                log.warning(f'could not remove timeout {attr}={timeout_id}: {e}')
            setattr(self, attr, None)

    def _on_notify_ice_state(self, element, pspec):
        """GObject notify::ice-connection-state handler (GLib thread)."""
        try:
            state = int(element.get_property('ice-connection-state'))
        except Exception as e:
            log.warning(f'could not read ice-connection-state: {e}')
            return
        self._on_ice_state_change(element, state)

    def _on_ice_state_change(self, element, state):
        """Handle an ICE connection-state change (GLib thread).

        CONNECTED/COMPLETED means media can flow; DISCONNECTED gets a grace
        period because ICE may recover, while FAILED/CLOSED are terminal.
        """
        try:
            state_value = int(state)
        except (TypeError, ValueError):
            state_value = state
        log.info(f'ICE connection state: {state_value}')

        if state_value in webrtc_lifecycle.ICE_CONNECTED:
            self._ice_connected = True
            self._cancel_timeout('_disconnect_timeout_id')
            self._cancel_timeout('_connect_watchdog_id')
            # GAP-WEBRTC-06: report the selected candidate pair once ICE is up.
            self._emit_connection_info()
            return

        reason = webrtc_lifecycle.end_reason(state_value)
        if reason == 'ice-disconnected':
            if self._disconnect_timeout_id is None:
                self._disconnect_timeout_id = GLib.timeout_add_seconds(
                    15, self._check_disconnected
                )
        elif reason in ('ice-failed', 'ice-closed'):
            self._notify_stream_ended(reason)

    def _emit_connection_info(self):
        """Request the selected candidate pair once per session (GLib thread).

        Recovered 3.1.6 ``FUN_000be3f8`` runs after candidate-pair selection and
        forces both types to 6 when no pair exists. ``get-stats`` resolves its
        promise on the GLib main loop, so it is requested with a change callback
        (never ``promise.wait()`` on this thread, which would deadlock); an
        unusable reply logs and skips rather than guessing.
        """
        if self._connection_info_sent or self._webrtc is None:
            return
        if not CONNECTION_INFO_ENABLED:
            # Wire form unconfirmed (live server rejected the numeric encoding);
            # see the module constant. Do not send a guessed message.
            log.debug('WebRTC connection info disabled (PRUSA_WEBRTC_CONNECTION_INFO != 1)')
            return
        self._connection_info_sent = True
        try:
            promise = Gst.Promise.new_with_change_func(self._on_stats_ready)
            self._webrtc.emit('get-stats', None, promise)
        except Exception as e:
            # Allow a later CONNECTED/COMPLETED notification to retry.
            self._connection_info_sent = False
            log.warning(f'WebRTC get-stats request failed: {e}; skipping connection info')

    def _on_stats_ready(self, promise):
        """Handle the async ``get-stats`` reply (GLib thread)."""
        try:
            reply = promise.get_reply()
        except Exception as e:
            log.warning(f'WebRTC get-stats reply failed: {e}')
            return
        stats = None
        if reply is not None:
            try:
                stats = reply.get_value('stats') if reply.has_field('stats') else reply
            except Exception:
                stats = reply
        if stats is None:
            log.warning('WebRTC connection info: stats unavailable; skipping event')
            return
        types = self._selected_candidate_types_from(stats)
        if not isinstance(types, tuple):
            # We cannot reliably distinguish "no selected pair" from an unknown
            # stats shape, so skip rather than misreport 6/6. (The firmware's 6 is
            # its own internal no-pair detection, which we do not have.)
            log.warning(
                'WebRTC connection info: selected pair not extractable; skipping event'
            )
            return
        local_typ, remote_typ = types
        local_code = webrtc_lifecycle.candidate_type_code(local_typ)
        remote_code = webrtc_lifecycle.candidate_type_code(remote_typ)
        log.info(
            f'WebRTC connection info: local={local_typ}->{local_code} '
            f'remote={remote_typ}->{remote_code}'
        )
        client_id = self._request_id
        if self._loop and self._on_connection_info:
            self._loop.call_soon_threadsafe(
                asyncio.ensure_future,
                self._on_connection_info(client_id, local_code, remote_code),
            )

    @staticmethod
    def _selected_candidate_types_from(stats):
        """Extract ``(local_typ, remote_typ)`` or ``'unparseable'``.

        Returns the tuple only when both candidate types were read; otherwise a
        non-tuple sentinel, and the caller skips the event (a version-dependent
        stats shape must never be misreported as "no pair").
        """
        found = {}
        for name, value in _structure_items(stats):
            lowered = name.lower()
            if 'selected' in lowered and 'pair' in lowered:
                _scan_candidate_types(value, None, found)
                if 'local' in found and 'remote' in found:
                    return found['local'], found['remote']
                return 'unparseable'
        _scan_candidate_types(stats, None, found)
        if 'local' in found and 'remote' in found:
            return found['local'], found['remote']
        return 'unparseable'

    def _check_disconnected(self):
        """Resolve a DISCONNECTED grace period after the timeout (GLib thread)."""
        self._disconnect_timeout_id = None
        state = None
        if self._webrtc is not None:
            try:
                state = int(self._webrtc.get_property('ice-connection-state'))
            except Exception as e:
                log.warning(f'could not read ICE connection state: {e}')
        if (not self._ice_connected
                or state in webrtc_lifecycle.ICE_ENDED
                or state == webrtc_lifecycle.ICE_DISCONNECTED):
            self._notify_stream_ended('ice-disconnected')
        return False

    def _connect_watchdog(self):
        """Fail the session if ICE never connected (GLib thread)."""
        self._connect_watchdog_id = None
        if not self._ice_connected:
            self._notify_stream_ended('no-ice-connection')
        return False

    def _notify_teardown(self):
        """Notify the owner that the peer was torn down (GAP-WEBRTC-05).

        Runs on the caller's thread (create_offer/stop); the callback only clears
        a boolean on the shared state, so no asyncio marshalling is required.
        """
        if self._on_teardown is None:
            return
        try:
            self._on_teardown()
        except Exception as e:
            log.warning(f'on_teardown callback failed: {e}')

    def _notify_stream_ended(self, reason):
        """Report the end of the session once, marshalled to the asyncio loop."""
        if self._ended_notified:
            return
        self._ended_notified = True
        log.info(f'WebRTC stream ended ({reason})')
        if self._loop and self._on_stream_ended:
            self._loop.call_soon_threadsafe(
                asyncio.ensure_future, self._on_stream_ended(reason)
            )

    def _on_offer_created(self, promise):
        # Runs on the GLib main-loop thread; never block here with wait().
        reply = promise.get_reply()
        offer = reply.get_value('offer') if reply is not None else None
        log.info(f'Offer promise completed (reply={reply is not None}, offer={offer is not None})')
        if offer is None:
            log.error('Offer creation returned no reply')
            return
        sdp_text = offer.sdp.as_text()
        mlines = [line for line in sdp_text.splitlines() if line.startswith('m=')]
        log.info(f'Offer SDP text ready ({len(sdp_text)} chars, m-lines={mlines})')
        self._webrtc.emit('set-local-description', offer, None)
        log.info('Local description set')

        # Match libdatachannel's H264 fmtp (no sprop-parameter-sets).
        sdp_text = _strip_sprop(sdp_text)

        if self._loop and self._on_offer:
            self._loop.call_soon_threadsafe(
                asyncio.ensure_future,
                self._on_offer(self._request_id, sdp_text)
            )

    def add_ice_candidate(self, candidate, sdp_mline_index=0):
        if self._webrtc:
            self._webrtc.emit('add-ice-candidate', sdp_mline_index, candidate)
