"""Video-quality tier state shared by the RTSP source and the WebRTC path.

The real Buddy3D camera switches live-video resolution on the app's VideoQuality
command. We persist the chosen tier to an EnvironmentFile that the rpicam-source
systemd unit reads on (re)start; webrtc.py reads it at spawn time.

Prusa VideoQuality protobuf enum: 1=SD, 2=HD, 3=FHD.
"""
import os

RESOLUTIONS = {1: (640, 480), 2: (1280, 720), 3: (1920, 1080)}
DEFAULT_QUALITY = 3  # FHD


def quality_change_allowed(current_enum, requested_enum, turn_online):
    """GAP-WEBRTC-05 TURN/scoped-quality lock (pure policy).

    Recovered 3.1.6: while a TURN client is online the config quality path
    (``FUN_000a7940`` -> ``FUN_000b5ad4`` -> ``FUN_000b4f90``, flag at
    ``singleton+0x278``) rejects a quality *raise* and logs ``"TURN client
    ONLINE - WebRTC is active, video quality change is not allowed"``. Lowering
    or keeping the current tier stays allowed. With no TURN client the change is
    always allowed.

    ``current_enum``/``requested_enum`` are the 1/2/3 protobuf quality tiers.
    ``None`` means the tier is unknown, so the caller has no basis to compare and
    the change is not blocked.
    """
    if not turn_online:
        return True
    if current_enum is None or requested_enum is None:
        return True
    return requested_enum <= current_enum


# Absolute (home-dir independent) so the same path works for pi/ and bullitt/ installs.
# Overridable via env for the self-check below.
QUALITY_ENV = os.environ.get('PRUSA_QUALITY_ENV', '/etc/prusa-cam/quality.env')

# Ephemeral live override, read by rpicam-source *after* QUALITY_ENV so a
# GAP-QUALITY-02 live change can move the encoder without writing the persisted
# tier. Cleared on reboot like every other tmpfs/overlay write.
QUALITY_LIVE_ENV = os.environ.get('PRUSA_QUALITY_LIVE_ENV', '/etc/prusa-cam/quality.live.env')


def write_live(quality):
    """Apply tier `quality` for the live encoder without persisting it.

    Returns the (width, height) written. The rpicam-source unit reads this file
    after QUALITY_ENV; persist_quality() is the separate durable write.
    """
    w, h = RESOLUTIONS.get(quality, RESOLUTIONS[DEFAULT_QUALITY])
    os.makedirs(os.path.dirname(QUALITY_LIVE_ENV) or '.', exist_ok=True)
    with open(QUALITY_LIVE_ENV, 'w') as f:
        f.write(f'CAM_WIDTH={w}\nCAM_HEIGHT={h}\n')
    return w, h


def read_live():
    """Return the raw bytes of the live override file, or None if it is absent.

    Used to snapshot the previous live state before a live apply so a failed
    restart can restore it instead of leaving the encoder on the failed tier.
    """
    try:
        with open(QUALITY_LIVE_ENV, 'rb') as f:
            return f.read()
    except FileNotFoundError:
        return None


def restore_live(previous):
    """Restore the live override to `previous` bytes, or remove it when None."""
    if previous is None:
        try:
            os.remove(QUALITY_LIVE_ENV)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(QUALITY_LIVE_ENV) or '.', exist_ok=True)
    with open(QUALITY_LIVE_ENV, 'wb') as f:
        f.write(previous)


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


def _read_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            if '=' in line:
                k, v = line.strip().split('=', 1)
                env[k] = v
    return env


def read_current():
    """Return (quality_enum, width, height); live override first, then persisted.

    Defaults to FHD if both files are unset/unreadable.
    """
    for path in (QUALITY_LIVE_ENV, QUALITY_ENV):
        try:
            env = _read_env(path)
            w, h = int(env['CAM_WIDTH']), int(env['CAM_HEIGHT'])
            for q, res in RESOLUTIONS.items():
                if res == (w, h):
                    return q, w, h
            return DEFAULT_QUALITY, w, h  # non-tier size still honored
        except Exception:
            continue
    w, h = RESOLUTIONS[DEFAULT_QUALITY]
    return DEFAULT_QUALITY, w, h


def demo():
    """Self-check: round-trip each tier through the env file."""
    import tempfile
    global QUALITY_ENV, QUALITY_LIVE_ENV
    with tempfile.TemporaryDirectory() as d:
        QUALITY_ENV = os.path.join(d, 'quality.env')
        QUALITY_LIVE_ENV = os.path.join(d, 'quality.live.env')
        for q, (w, h) in RESOLUTIONS.items():
            assert write_current(q) == (w, h)
            assert read_current() == (q, w, h)
        # a live override wins over the persisted tier and is not a persist write
        assert write_live(1) == RESOLUTIONS[1]
        assert read_current() == (1, *RESOLUTIONS[1])
        os.remove(QUALITY_LIVE_ENV)
        assert read_current() == (DEFAULT_QUALITY, *RESOLUTIONS[DEFAULT_QUALITY])
        # unknown tier falls back to FHD default
        assert write_current(99) == RESOLUTIONS[DEFAULT_QUALITY]
        # missing file → default
        os.remove(QUALITY_ENV)
        assert read_current() == (DEFAULT_QUALITY, *RESOLUTIONS[DEFAULT_QUALITY])
    print('quality.py self-check OK')


if __name__ == '__main__':
    demo()
