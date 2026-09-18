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
    # (FW-STATUS:183-192,246-265,296-301,365-384). GAP-STATUS-01 is partial:
    # GAP-RTSP-02 (real service state) remains open.
    return 1 if state.rtsp_running else 2


def build_status_message(state, *, token='', mac='', ip='', ssid='',
                         signal_quality=0, cpu_temperature=0.0, uptime=0,
                         load_average='', memory=None, process_count=0,
                         request_id=None, sid='', tz_name=''):
    """Encode the firmware-shaped status message from shared state + telemetry.

    GAP-STATUS-02: request-triggered status correlates on ``request_id``; the
    unsolicited initial status falls back to the Socket.IO SID.
    GAP-NETWORK-01: the empty secondary network submessage is not emitted.
    """
    if memory is None:
        memory = {'MemTotal': 0, 'MemFree': 0, 'Shmem': 0, 'Buffers': 0}

    timelapse_status = encode_message({
        1: 2,
        2: 0,
        3: 0,
        4: '',
        5: 0,
        6: 0,
        7: Float32(0.0),
    })

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
            1: 2,
            2: 0,
            3: 0,
            4: 0,
            6: MODEL,
        }),
        6: encode_message({
            1: state.rtsp_mode,
            2: _rtsp_status(state),
            4: f'rtsp://{ip}:8554/live' if ip else '',
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
