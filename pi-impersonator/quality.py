"""Video-quality tier state shared by the RTSP source and the WebRTC path.

The real Buddy3D camera switches live-video resolution on the app's VideoQuality
command. We persist the chosen tier to an EnvironmentFile that the rpicam-source
systemd unit reads on (re)start; webrtc.py reads it at spawn time.

Prusa VideoQuality protobuf enum: 1=SD, 2=HD, 3=FHD.
"""
import os

RESOLUTIONS = {1: (640, 480), 2: (1280, 720), 3: (1920, 1080)}
DEFAULT_QUALITY = 3  # FHD

# Absolute (home-dir independent) so the same path works for pi/ and bullitt/ installs.
# Overridable via env for the self-check below.
QUALITY_ENV = os.environ.get('PRUSA_QUALITY_ENV', '/etc/prusa-cam/quality.env')


def write_current(quality):
    """Persist tier `quality` (1/2/3). Returns the (width, height) written."""
    w, h = RESOLUTIONS.get(quality, RESOLUTIONS[DEFAULT_QUALITY])
    tmp = QUALITY_ENV + '.tmp'
    with open(tmp, 'w') as f:
        f.write(f'CAM_WIDTH={w}\nCAM_HEIGHT={h}\n')
        f.flush()
        os.fsync(f.fileno())  # data on disk before the rename (crash-safe vs abrupt power loss)
    os.replace(tmp, QUALITY_ENV)  # atomic swap so a concurrent read never sees a half-write
    dfd = os.open(os.path.dirname(QUALITY_ENV) or '.', os.O_RDONLY)  # fsync dir → rename durable
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return w, h


def read_current():
    """Return (quality_enum, width, height); defaults to FHD if unset/unreadable."""
    try:
        env = {}
        with open(QUALITY_ENV) as f:
            for line in f:
                if '=' in line:
                    k, v = line.strip().split('=', 1)
                    env[k] = v
        w, h = int(env['CAM_WIDTH']), int(env['CAM_HEIGHT'])
        for q, res in RESOLUTIONS.items():
            if res == (w, h):
                return q, w, h
        return DEFAULT_QUALITY, w, h  # non-tier size still honored
    except Exception:
        w, h = RESOLUTIONS[DEFAULT_QUALITY]
        return DEFAULT_QUALITY, w, h


def demo():
    """Self-check: round-trip each tier through the env file."""
    import tempfile
    global QUALITY_ENV
    with tempfile.TemporaryDirectory() as d:
        QUALITY_ENV = os.path.join(d, 'quality.env')
        for q, (w, h) in RESOLUTIONS.items():
            assert write_current(q) == (w, h)
            assert read_current() == (q, w, h)
        # unknown tier falls back to FHD default
        assert write_current(99) == RESOLUTIONS[DEFAULT_QUALITY]
        # missing file → default
        os.remove(QUALITY_ENV)
        assert read_current() == (DEFAULT_QUALITY, *RESOLUTIONS[DEFAULT_QUALITY])
    print('quality.py self-check OK')


if __name__ == '__main__':
    demo()
