import subprocess
import tempfile
import os
import glob

def capture_jpeg(width=1920, height=1080):
    # Grab a frame from the always-running mux stream (port 8888).
    # libcamera is single-consumer; rpicam-source owns the sensor via stream_mux.py.
    # We use 'timeout' to stop the pipeline after a keyframe arrives (--intra 30 = ≤1s).
    with tempfile.TemporaryDirectory() as d:
        pattern = os.path.join(d, 'snap%05d.jpg')
        subprocess.run(
            ['timeout', '5',
             'gst-launch-1.0', '-q',
             'tcpclientsrc', 'host=127.0.0.1', 'port=8888', 'do-timestamp=true',
             '!', 'h264parse',
             '!', 'openh264dec',
             '!', 'videoconvert',
             '!', 'jpegenc', 'quality=95',  # GAP-SNAPSHOT-03: firmware JPEG quality is 95
             '!', 'multifilesink', f'location={pattern}'],
            capture_output=True, timeout=7
        )
        files = sorted(glob.glob(os.path.join(d, 'snap*.jpg')))
        if not files:
            raise RuntimeError('capture_jpeg: no frame captured')
        data = open(files[-1], 'rb').read()
        if len(data) < 100:
            raise RuntimeError('capture_jpeg: empty frame')
        return data
