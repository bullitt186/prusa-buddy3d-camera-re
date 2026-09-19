"""
TCP stream multiplexer: reads H264 Annex-B from rpicam-vid (stdin) and fans it out to
any number of TCP clients.

Two ports:
  8888  the stream verbatim (RTSP, snapshots)
  8889  the same stream with the H264 SPS profile/level patched to constrained
        baseline level 3.1 (42 e0 1f) for the WebRTC branch only

Why 8889: the Connect WebRTC answerer (a libdatachannel endpoint) rejects the
camera's v4l2 SPS (428029 = baseline level 4.1) and only accepts the firmware's
42e01f class (constrained baseline level 3.1). Transcoding is too heavy for the
Pi (verified), so the WebRTC branch gets a byte-patched SPS instead. Only the
three informational header bytes change; the SPS body (resolution/VUI) is
untouched.

New clients wait for the next IDR boundary before receiving data, so they always start
on a clean keyframe (no garbage vertical-stripe artifacts).

rpicam-source.service pipes rpicam-vid stdout here:
  rpicam-vid ... --inline -o - | python stream_mux.py

--inline on rpicam-vid re-emits SPS+PPS before every IDR, so each IDR boundary
carries the full decoder init data a fresh client needs.
"""
import sys, socket, threading, collections

PORT = 8888
PORT_PATCHED = 8889
BUFSIZE = 65536
MAX_QUEUE = 120  # ~4 s at 30 fps; slow clients are dropped, not backpressured

START = b'\x00\x00\x00\x01'  # H264 Annex-B start code

# Constrained baseline, level 3.1 (matches the firmware's libdatachannel offer).
SPS_PROFILE_IDC = 0x42
SPS_CONSTRAINT_FLAGS = 0xE0
SPS_LEVEL_IDC = 0x1F


def patch_sps(data: bytes) -> bytes:
    """Return ``data`` with every SPS NAL's profile/constraint/level bytes patched.

    The NAL header byte is matched by type (``byte & 0x1F == 7``), not a fixed
    0x67 — rpicam-vid emits it as 0x27 (nal_ref_idc=1) in this stream.
    """
    out = bytearray(data)
    i = 0
    while True:
        j = out.find(b'\x00\x00\x01', i)
        if j < 0:
            break
        k = j + 3
        if k + 3 < len(out) and (out[k] & 0x1F) == 7:
            out[k + 1] = SPS_PROFILE_IDC
            out[k + 2] = SPS_CONSTRAINT_FLAGS
            out[k + 3] = SPS_LEVEL_IDC
        i = j + 3
    return bytes(out)


class Channel:
    """One TCP fan-out: clients, bootstrap keyframe, and optional SPS patch."""

    def __init__(self, port, patch=False):
        self.port = port
        self.patch = patch
        self.clients = []
        self.keyframe = b''
        self._lock = threading.Lock()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('0.0.0.0', self.port))
        srv.listen(8)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket):
        q = collections.deque()
        with self._lock:
            bootstrap = self.keyframe
        if bootstrap:
            try:
                conn.sendall(bootstrap)
            except Exception:
                conn.close()
                return
        with self._lock:
            self.clients.append(q)
        try:
            while True:
                if not q:
                    threading.Event().wait(0.005)
                    continue
                conn.sendall(q.popleft())
        except Exception:
            pass
        finally:
            with self._lock:
                try:
                    self.clients.remove(q)
                except ValueError:
                    pass
            conn.close()

    def broadcast(self, data: bytes):
        if self.patch:
            data = patch_sps(data)
        with self._lock:
            dead = []
            for q in self.clients:
                if q is None:
                    continue
                try:
                    q.append(data)
                    while len(q) > MAX_QUEUE:
                        q.popleft()
                except Exception:
                    dead.append(q)
            for q in dead:
                try:
                    self.clients.remove(q)
                except ValueError:
                    pass

    def set_keyframe(self, data: bytes):
        with self._lock:
            self.keyframe = data


def _is_keyframe_start(data: bytes) -> bool:
    """True if data begins with (or contains) a SPS or IDR NAL unit start."""
    # rpicam-vid --inline emits: [start] SPS [start] PPS [start] IDR ...
    # NAL type is in the byte after the 4-byte start code: 0x67=SPS, 0x65=IDR
    i = data.find(START)
    while i != -1 and i + 4 < len(data):
        nal_type = data[i + 4] & 0x1F
        if nal_type in (5, 7):  # IDR or SPS
            return True
        i = data.find(START, i + 4)
    return False


channels = [Channel(PORT), Channel(PORT_PATCHED, patch=True)]

# ponytail: accumulate a rolling keyframe buffer (SPS+PPS+IDR) so new clients
# get a clean start. Cleared after each IDR so it only holds the latest one.
_kf_buf = b''

stdin = sys.stdin.buffer
while True:
    chunk = stdin.read(BUFSIZE)
    if not chunk:
        break

    if _is_keyframe_start(chunk):
        # Trim to the first SPS/IDR start code so we never send leading slice data
        sps_pos = len(chunk)
        for sc_offset in range(len(chunk) - 4):
            if chunk[sc_offset:sc_offset + 4] == START:
                nal_type = chunk[sc_offset + 4] & 0x1F
                if nal_type in (5, 7):
                    sps_pos = sc_offset
                    break
        _kf_buf = chunk[sps_pos:]
    elif _kf_buf:
        # Continuation of the current keyframe sequence
        _kf_buf += chunk
        # Promote once we have SPS+PPS+IDR plus at least one full P-frame worth (> 32 KB)
        if len(_kf_buf) > 32768:
            for ch in channels:
                ch.set_keyframe(ch.patch and patch_sps(_kf_buf) or _kf_buf)
            _kf_buf = b''

    for ch in channels:
        ch.broadcast(chunk)
