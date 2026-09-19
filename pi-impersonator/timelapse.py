"""Pi storage-backed timelapse (GAP-TIMELAPSE-01).

Firmware 3.1.6 controls timelapse enable/interval/FPS, stores frames, creates
MJPEG output, indexes files and emits progress/error ``client_trigger`` messages.
The owner decision (2026-09-19) is a Pi storage-backed equivalent rooted at
``/var/lib/prusa-cam/timelapse``.

Stdlib-only and side-effect free on import so the logic is host-testable.
"""
import os
import struct
import time

SD_MOUNT = '/mnt/sdcard'   # emulated SD, the firmware's storage path
TIMELAPSE_DIR = '/mnt/sdcard/timelapse'   # emulated SD (see the SMB share)
DEFAULT_INTERVAL = 10   # seconds between frames
DEFAULT_FPS = 10        # playback rate of the assembled MJPEG AVI
INTERVAL_MIN, INTERVAL_MAX = 1, 3600
FPS_MIN, FPS_MAX = 1, 30

# Firmware 3.1.6 artifact conventions (FUN_000ac2e8 / FUN_000ac5d4 /
# FUN_000ac934 / FUN_000aee3c): timestamped JPEG frames, an MJPEG stream saved
# with an .avi extension, and a hidden CSV index in the same directory.
FRAME_PREFIX = 'timelapse_'
FRAME_SUFFIX = '.jpg'
AVI_SUFFIX = '.avi'
CSV_NAME = '.timelapse_videos.csv'


def sd_present(path=SD_MOUNT):
    """True when the emulated SD is mounted and accessible.

    Pi policy for the firmware's ``isDevicePresent && canAccessMountPoint &&
    /proc/mounts`` check: the Pi has no block device, so ``/mnt/sdcard`` is a
    real directory provisioned by ``deploy.sh``. Presence matches the firmware's
    ``access(path, R_OK)`` (``FUN_00071bc0``); write access is reported
    separately by :func:`sd_mode`. Returns False on ``OSError``.
    """
    try:
        return os.path.isdir(path) and os.access(path, os.R_OK)
    except OSError:
        return False


def sd_space(path=SD_MOUNT):
    """Return ``(total_mb, free_mb, used_mb)`` from ``statvfs64``.

    Matches ``FUN_000745e0``: ``(f_bsize * f_blocks) >> 20`` (total),
    ``(f_bsize * f_bfree) >> 20`` (free), and
    ``(f_bsize * (f_blocks - f_bfree)) >> 20`` (used). Returns ``(0, 0, 0)``
    on ``OSError``.
    """
    try:
        st = os.statvfs(path)
    except OSError:
        return (0, 0, 0)
    total = (st.f_bsize * st.f_blocks) >> 20
    free = (st.f_bsize * st.f_bfree) >> 20
    used = (st.f_bsize * (st.f_blocks - st.f_bfree)) >> 20
    return (total, free, used)


def sd_mode(path=SD_MOUNT):
    """SD mount-mode string (``FUN_00073914``): ``RW``/``RO``/``UNKNOWN``."""
    if not sd_present(path):
        return 'UNKNOWN'
    return 'RW' if os.access(path, os.W_OK) else 'RO'


def storage_status(path=SD_MOUNT):
    """5-tuple shaped for ``extended_status.4`` (descriptor ``0x3f72b0``).

    ``(present, total_mb, free_mb, used_mb, mode)``; absent storage reports the
    firmware mounted-state ``2`` (1 = mounted) with zero space and
    ``'UNKNOWN'`` mode.
    """
    if not sd_present(path):
        return (2, 0, 0, 0, 'UNKNOWN')
    total, free, used = sd_space(path)
    return (1, total, free, used, sd_mode(path))


def valid_interval(value):
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds if INTERVAL_MIN <= seconds <= INTERVAL_MAX else None


def valid_fps(value):
    try:
        fps = int(value)
    except (TypeError, ValueError):
        return None
    return fps if FPS_MIN <= fps <= FPS_MAX else None


def _millis(now):
    """Millisecond component of a ``time.time()`` value (the ``%03d`` field)."""
    return int((now - int(now)) * 1_000_000) // 1000


def _stamp(tm, ms):
    """``HH-MM-SS-mmm`` exactly as the firmware's ``%02d-%02d-%02d-%03d``."""
    return '%02d-%02d-%02d-%03d' % (tm.tm_hour, tm.tm_min, tm.tm_sec, ms)


def frame_name(now=None):
    """Timestamped frame filename (``FUN_000ac2e8`` + ``FUN_000ac5d4``).

    ``timelapse_<HH-MM-SS-mmm>.jpg`` where the time comes from
    ``time.localtime(sec)`` and the millisecond field is ``usec // 1000``.
    """
    now = time.time() if now is None else now
    return f'{FRAME_PREFIX}{_stamp(time.localtime(int(now)), _millis(now))}{FRAME_SUFFIX}'


def _unique_name(dir, suffix, now):
    """Return a not-yet-existing timestamped path, bumping the ms on collision.

    The firmware never overwrites an artifact, so a same-millisecond capture or
    build gets the next free millisecond value instead.
    """
    tm = time.localtime(int(now))
    ms = _millis(now)
    path = os.path.join(dir, f'{FRAME_PREFIX}{_stamp(tm, ms)}{suffix}')
    while os.path.exists(path):
        ms += 1
        path = os.path.join(dir, f'{FRAME_PREFIX}{_stamp(tm, ms)}{suffix}')
    return path


def save_frame(data, dir=TIMELAPSE_DIR, now=None):
    """Persist one JPEG frame under its timestamped name; returns the path.

    A frame is never silently overwritten: if the computed name already exists,
    the millisecond field is incremented until a free name is found.
    """
    os.makedirs(dir, exist_ok=True)
    now = time.time() if now is None else now
    path = _unique_name(dir, FRAME_SUFFIX, now)
    with open(path, 'wb') as f:
        f.write(data)
    return path


def list_frames(dir=TIMELAPSE_DIR):
    """Return the stored timestamped JPEG frame basenames, sorted by name."""
    try:
        names = os.listdir(dir)
    except OSError:
        return []
    frames = [
        n for n in names
        if n.startswith(FRAME_PREFIX) and n.endswith(FRAME_SUFFIX)
        and os.path.isfile(os.path.join(dir, n))
    ]
    return sorted(frames)


def list_videos(dir=TIMELAPSE_DIR):
    """Return assembled ``.avi`` basenames, sorted (``FUN_000ac934``).

    The firmware's list builder enumerates regular files ending in ``.avi``
    under the timelapse directory, joined by ``;``.
    """
    try:
        names = os.listdir(dir)
    except OSError:
        return []
    videos = [
        n for n in names
        if n.endswith(AVI_SUFFIX) and os.path.isfile(os.path.join(dir, n))
    ]
    return sorted(videos)


def _jpeg_dimensions(data):
    """Return ``(width, height)`` from the first SOF0/SOF1/SOF2 marker, else None."""
    if len(data) < 4 or data[0:2] != b'\xff\xd8':
        return None
    i = 2
    n = len(data)
    while i + 1 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        i += 2
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0xD9 or i + 2 > n:
            break
        seg_len = struct.unpack('>H', data[i:i + 2])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            if i + 7 > n:
                break
            height = struct.unpack('>H', data[i + 3:i + 5])[0]
            width = struct.unpack('>H', data[i + 5:i + 7])[0]
            return (width, height)
        i += seg_len
    return None


def _avi_chunk(fourcc, payload):
    """One RIFF chunk: fourcc + little-endian size + payload padded to even."""
    padding = b'\x00' if len(payload) % 2 else b''
    return fourcc + struct.pack('<I', len(payload)) + payload + padding


def _build_avi_bytes(payloads, fps, width, height):
    """Serialize JPEG frames as an MJPEG-in-AVI stream (RIFF/AVI + ``idx1``).

    Layout: ``RIFF/AVI `` -> ``LIST hdrl`` (``avih``, ``LIST strl`` with
    ``strh``/``strf``) -> ``LIST movi`` (one ``00dc`` chunk per JPEG) ->
    ``idx1`` (one keyframe entry per frame). Every size field is exact.
    """
    frame_count = len(payloads)
    max_frame = max(len(p) for p in payloads)
    total_bytes = sum(len(p) for p in payloads)
    micro_per_frame = max(1, int(1_000_000 / fps))
    max_bytes_per_sec = int(total_bytes * fps / frame_count)

    # The 'movi' bytearray begins with the 'movi' fourcc, so a chunk's offset
    # from that start is exactly the idx1 offset (relative to the movi list).
    movi = bytearray(b'movi')
    idx = bytearray()
    for jpeg in payloads:
        offset = len(movi)
        movi += _avi_chunk(b'00dc', jpeg)
        # idx1 entry: chunk id, AVIIF_KEYFRAME, offset, size (16 bytes).
        idx += b'00dc' + struct.pack('<III', 0x10, offset, len(jpeg))

    avih = struct.pack(
        '<IIIIIIIIII4I',
        micro_per_frame, max_bytes_per_sec, 0, 0x10,  # flags = AVIF_HASINDEX
        frame_count, 0, 1, max_frame, width, height, 0, 0, 0, 0,
    )
    strh = struct.pack(
        '<4s4sIHHIIIIIIIIhhhh',
        b'vids', b'MJPG', 0, 0, 0, 0,
        1, fps, 0, frame_count, max_frame, 0xFFFFFFFF, 0,
        0, 0, width, height,
    )
    strf = struct.pack(
        '<IiiHH4sIiiII',
        40, width, height, 1, 24, b'MJPG', width * height * 3, 0, 0, 0, 0,
    )

    strl_body = b'strl' + _avi_chunk(b'strh', strh) + _avi_chunk(b'strf', strf)
    strl = b'LIST' + struct.pack('<I', len(strl_body)) + strl_body
    hdrl_body = b'hdrl' + _avi_chunk(b'avih', avih) + strl
    hdrl = b'LIST' + struct.pack('<I', len(hdrl_body)) + hdrl_body
    movi_list = b'LIST' + struct.pack('<I', len(movi)) + bytes(movi)
    idx1 = b'idx1' + struct.pack('<I', len(idx)) + bytes(idx)

    body = b'AVI ' + hdrl + movi_list + idx1
    return b'RIFF' + struct.pack('<I', len(body)) + body


# Video status chars (FUN_000aee3c / FUN_000ac134): 0x44 'D' done, 0x45 'E'
# error, 0x50 'P' pending; a name absent from the index defaults to 0x55 'U'.
VIDEO_STATUS_DONE = 'D'
VIDEO_STATUS_ERROR = 'E'
VIDEO_STATUS_PENDING = 'P'
VIDEO_STATUS_UNKNOWN = 'U'


def _append_video_index(dir, filename, status):
    """Append one ``<name>:<status>`` row to ``.timelapse_videos.csv``.

    Firmware parity: ``FUN_000ac134`` writes ``<basename>`` + ``':'`` + a
    one-char status to the hidden index in the timelapse directory; there is no
    header row. ``FUN_000ad7ec`` reads it back for the file list.
    """
    path = os.path.join(dir, CSV_NAME)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(f'{filename}:{status}\n')


def read_video_index(dir=TIMELAPSE_DIR):
    """Map ``.avi`` basename -> status char from ``.timelapse_videos.csv``.

    ``FUN_000ad7ec`` keeps the character after the first ``':'`` on each line.
    A missing or unreadable index yields an empty map (callers default to
    ``'U'``).
    """
    statuses = {}
    try:
        with open(os.path.join(dir, CSV_NAME), encoding='utf-8') as f:
            for line in f:
                name, sep, status = line.rstrip('\n').partition(':')
                if sep and name:
                    statuses[name] = status[:1] or VIDEO_STATUS_UNKNOWN
    except OSError:
        return {}
    return statuses


def file_list_entries(dir=TIMELAPSE_DIR):
    """Firmware-shaped file-list body: one ``<name>;<status>\\n`` per ``.avi``.

    ``FUN_000ad7ec`` enumerates the ``.avi`` files (``FUN_000ac934``), looks each
    up in ``.timelapse_videos.csv`` (missing -> ``'U'``), and appends
    ``<name>;<status>`` followed by a newline. No videos -> ``''``, and the
    sender then emits nothing (``No video files found on SD card``).
    """
    videos = list_videos(dir)
    if not videos:
        return ''
    statuses = read_video_index(dir)
    return ''.join(
        f'{name};{statuses.get(name, VIDEO_STATUS_UNKNOWN)}\n'
        for name in videos
    )


def build_avi(dir=TIMELAPSE_DIR, fps=DEFAULT_FPS, width=None, height=None):
    """Assemble the sorted JPEG frames into an MJPEG-in-AVI file.

    Returns the ``.avi`` path, or ``None`` when no frames are stored. Dimensions
    come from the first frame's SOF marker, falling back to the caller-provided
    values. The filename uses the firmware timestamp convention and one row is
    appended to ``.timelapse_videos.csv``.
    """
    frames = list_frames(dir)
    if not frames:
        return None
    payloads = []
    for name in frames:
        with open(os.path.join(dir, name), 'rb') as f:
            payloads.append(f.read())
    dims = _jpeg_dimensions(payloads[0])
    if dims is not None:
        width, height = dims
    width = width or 640
    height = height or 480
    fps = max(1, int(fps))
    output_path = _unique_name(dir, AVI_SUFFIX, time.time())
    try:
        data = _build_avi_bytes(payloads, fps, width, height)
        with open(output_path, 'wb') as f:
            f.write(data)
    except Exception:
        # FUN_000aee3c records 'E' when a build fails.
        _append_video_index(dir, os.path.basename(output_path), VIDEO_STATUS_ERROR)
        raise
    _append_video_index(dir, os.path.basename(output_path), VIDEO_STATUS_DONE)
    return output_path


def format_file_list_fragment(page, total, chunk):
    """One ``file_list`` fragment: exactly ``"<page>;<total>\\n<chunk>"``.

    Recovered from ``FUN_000a1fa8``; ``page`` is one-based.
    """
    return f'{page};{total}\n{chunk}'


def file_list_fragments(listing, chunk_size=1024):
    """Split a ``;``-joined ``.avi`` listing into firmware-shaped fragments.

    ``FUN_000a1fa8``: below ``0x401`` bytes the whole listing is one fragment;
    otherwise ``total = (size >> 10) + 1`` with ``chunk_size`` (1024) chunks.
    Returns ``(page, total, chunk)`` tuples; an empty listing yields none.
    """
    data = listing.encode('utf-8')
    if not data:
        return []
    if len(data) < 0x401:
        return [(1, 1, listing)]
    total = (len(data) >> 10) + 1
    fragments = []
    for page in range(1, total + 1):
        chunk = data[(page - 1) * chunk_size: page * chunk_size]
        fragments.append((page, total, chunk.decode('utf-8', errors='replace')))
    return fragments


def apply_enable(action, state):
    """Set ``state.timelapse_enabled`` from a trigger action name."""
    if action == 'timelapse_enable':
        state.timelapse_enabled = True
        return True
    if action == 'timelapse_disable':
        state.timelapse_enabled = False
        return True
    return False
