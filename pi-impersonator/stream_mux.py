"""
TCP stream multiplexer: reads H264 from rpicam-vid (stdin) and fans it out to any
number of clients on port 8888. Replaces the single-client --listen model.

rpicam-source.service pipes rpicam-vid stdout here:
  rpicam-vid ... -o - | python stream_mux.py

Clients (RTSP server, snapshot code) connect to 127.0.0.1:8888 normally.
"""
import sys, socket, threading, collections

PORT = 8888
BUFSIZE = 65536
MAX_QUEUE = 60  # frames; slow clients get dropped, not backpressured

clients: list = []
lock = threading.Lock()


def broadcast(data):
    with lock:
        dead = []
        for q in clients:
            try:
                q.append(data)
                if len(q) > MAX_QUEUE:
                    q.popleft()  # drop oldest frame for slow clients
            except Exception:
                dead.append(q)
        for q in dead:
            clients.remove(q)


def handle_client(conn):
    q = collections.deque()
    with lock:
        clients.append(q)
    try:
        while True:
            while not q:
                threading.Event().wait(0.005)
            data = q.popleft()
            conn.sendall(data)
    except Exception:
        pass
    finally:
        with lock:
            if q in clients:
                clients.remove(q)
        conn.close()


def serve():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', PORT))
    srv.listen(8)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle_client, args=(conn,), daemon=True).start()


threading.Thread(target=serve, daemon=True).start()

stdin = sys.stdin.buffer
while True:
    data = stdin.read(BUFSIZE)
    if not data:
        break
    broadcast(data)
