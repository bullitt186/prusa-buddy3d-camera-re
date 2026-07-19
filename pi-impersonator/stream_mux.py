"""
TCP stream multiplexer: reads H264 Annex-B from rpicam-vid (stdin) and fans it out to
any number of TCP clients on port 8888.

New clients wait for the next IDR boundary before receiving data, so they always start
on a clean keyframe (no garbage vertical-stripe artifacts).

rpicam-source.service pipes rpicam-vid stdout here:
  rpicam-vid ... --inline -o - | python stream_mux.py

--inline on rpicam-vid re-emits SPS+PPS before every IDR, so each IDR boundary
carries the full decoder init data a fresh client needs.
"""
import sys, socket, threading, collections

PORT = 8888
BUFSIZE = 65536
MAX_QUEUE = 120  # ~4 s at 30 fps; slow clients are dropped, not backpressured

_lock = threading.Lock()
_clients: list = []      # list of deques, one per connected client
_keyframe: bytes = b''   # latest SPS+PPS+IDR chunk to bootstrap new clients

START = b'\x00\x00\x00\x01'  # H264 Annex-B start code


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


def _broadcast(data: bytes):
    with _lock:
        dead = []
        for q in _clients:
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
                _clients.remove(q)
            except ValueError:
                pass


def _handle_client(conn: socket.socket):
    q = collections.deque()
    # Don't add to broadcast list yet — wait for the next keyframe snapshot
    # to be available so the client starts clean.
    with _lock:
        bootstrap = _keyframe

    if bootstrap:
        try:
            conn.sendall(bootstrap)
        except Exception:
            conn.close()
            return

    with _lock:
        _clients.append(q)
    try:
        while True:
            if not q:
                threading.Event().wait(0.005)
                continue
            conn.sendall(q.popleft())
    except Exception:
        pass
    finally:
        with _lock:
            try:
                _clients.remove(q)
            except ValueError:
                pass
        conn.close()


def _serve():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', PORT))
    srv.listen(8)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=_handle_client, args=(conn,), daemon=True).start()


threading.Thread(target=_serve, daemon=True).start()

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
            if chunk[sc_offset:sc_offset+4] == START:
                nal_type = chunk[sc_offset+4] & 0x1F
                if nal_type in (5, 7):
                    sps_pos = sc_offset
                    break
        _kf_buf = chunk[sps_pos:]
    elif _kf_buf:
        # Continuation of the current keyframe sequence
        _kf_buf += chunk
        # Promote once we have SPS+PPS+IDR plus at least one full P-frame worth (> 32 KB)
        if len(_kf_buf) > 32768:
            with _lock:
                _keyframe = _kf_buf
            _kf_buf = b''

    _broadcast(chunk)
