"""Pure construction of the ``CameraInfoMessage`` status payload.

Stdlib-only and free of ``socketio``/``gi``/GStreamer so the field construction
can be unit-tested on the host (``tests/test_pi_messages.py``). ``signaling.py``
supplies live system telemetry; the shared ``CameraState`` supplies the
command-driven values (GAP-STATUS-01).
"""
from features import FIRMWARE_VERSION, MODEL
from proto import Float32, encode_message


def _uptime_string(seconds):
    days, rem = divmod(max(0, seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    return f'{days} days, {hours}:{minutes}:{seconds}'


def _rtsp_status(state):
    # Firmware boolean/service translation: internal 0 -> 2, 1 -> 1
    # (FW-STATUS:183-192,246-265,296-301,365-384). GAP-RTSP-02 now drives
    # state.rtsp_running from the actual service state (rtsp_control).
    return 1 if state.rtsp_running else 2


def build_status_message(state, *, token='', mac='', ip='', ssid='',
                         signal_quality=0, cpu_temperature=0.0, uptime=0,
                         load_average='', memory=None, process_count=0,
                         request_id=None, sid='', tz_name='', storage=None):
    """Encode the firmware-shaped status message from shared state + telemetry.

    GAP-STATUS-02: request-triggered status correlates on ``request_id``; the
    unsolicited initial status falls back to the Socket.IO SID.
    GAP-NETWORK-01: the empty secondary network submessage is not emitted.
    GAP-TIMELAPSE-01: ``storage`` is the 5-tuple produced by
    ``timelapse.storage_status`` for ``extended_status.4``; it defaults to the
    absent state when no live telemetry is supplied.
    """
    if memory is None:
        memory = {'MemTotal': 0, 'MemFree': 0, 'Shmem': 0, 'Buffers': 0}
    if storage is None:
        storage = (2, 0, 0, 0, 'UNKNOWN')
    storage_present, storage_total, storage_free, storage_used, storage_mode = storage

    timelapse_status = encode_message({
        # GAP-TIMELAPSE-01: descriptor 0x3f753c tags 1/2 are the directly-traced
        # enable/interval getters (FUN_000abcdc translation: enabled->1,
        # disabled->2; FUN_000abcb0 interval, default 10). Tags 3-7 remain
        # unannotated (GAP-STATUS-03), so they are left as-is.
        1: 1 if state.timelapse_enabled else 2,
        2: state.timelapse_interval,
        3: 0,
        4: '',
        5: 0,
        # GAP-STATUS-03: descriptor 0x3f753c pins tag 6 = fixed32 (float) and
        # tag 7 = uvarint; the previous build had these two swapped.
        6: Float32(0.0),
        7: 0,
    })

    # GAP-DEVICE-02: fields 5/6 carry the hardware-implying IR-mode/speaker
    # values under the current mapping. The recovered descriptor does not pin
    # the nested tag-to-field mapping yet, so per the tracker rule ("do not
    # invent or guess nested status tags") these bytes are left unchanged even
    # though the Pi has no such hardware (CameraState.*_available is False and
    # device_control.apply_light_control never claims a mode). Changing this
    # encoding requires the descriptor fixture.
    camera_status = encode_message({
        3: 1,
        4: state.snapshot_interval,
        5: 1,
        6: 40,
    })

    # GAP-NETWORK-01: firmware leaves the secondary network block absent, so do
    # not emit the previously hardcoded empty field-2 submessage.
    network_info = encode_message({
        1: encode_message({
            1: ssid,
            2: mac,
            3: ip,
            5: signal_quality,
        }),
    })

    extended_status = encode_message({
        1: FIRMWARE_VERSION,
        2: MODEL,
        3: state.camera_name,
        4: encode_message({
            # GAP-TIMELAPSE-01 / GAP-STATUS-03: extended_status.4 is the SD storage
            # block (descriptor 0x3f72b0, traced 2026-09-19 from FW-STATUS). tag1 =
            # mounted state (1=mounted, 2=absent), tag2/3/4 = total/free/used MB
            # (FUN_000745e0, f_bsize*f_blocks>>20), tag5 = mount-mode string
            # (FUN_00073914 -> "RW"/"RO"/"UNKNOWN"). The previous MODEL-on-tag5 was a
            # prior-session misread of the getters and is removed.
            1: storage_present,
            2: storage_total,
            3: storage_free,
            4: storage_used,
            5: storage_mode,
        }),
        6: encode_message({
            1: state.rtsp_mode,
            2: _rtsp_status(state),
            # GAP-STATUS-03: descriptor 0x3f7278 has tags 1-2 uvarint and tag 3
            # string; the RTSP URL belongs on tag 3 (was tag 4).
            3: f'rtsp://{ip}:8554/live' if ip else '',
        }),
        7: encode_message({
            1: 1,
            2: 0,
        }),
        9: encode_message({
            1: 'webcam.connect.prusa3d.com',
            2: 'camera-signaling.prusa3d.com',
            3: 'connect.prusa3d.com',
        }),
        10: encode_message({
            1: tz_name,
            2: 1,
        }),
        11: encode_message({
            1: state.webrtc_mode,
            2: state.webrtc_status,
        }),
    })

    system_info = encode_message({
        1: Float32(cpu_temperature),
        2: uptime,
        3: _uptime_string(uptime),
        4: load_average,
        5: memory['MemTotal'],
        6: memory['MemFree'],
        7: memory['Shmem'],
        8: memory['Buffers'],
        9: process_count,
    })

    video_quality = encode_message({1: state.quality})

    correlation = request_id if request_id else sid

    fields = {
        2: timelapse_status,
        3: camera_status,
        4: network_info,
        5: extended_status,
        8: token,
        9: system_info,
        10: correlation,
        11: video_quality,
    }
    return encode_message(fields)
