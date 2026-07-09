import subprocess

def capture_jpeg(width=1920, height=1080):
    result = subprocess.run(
        ['rpicam-jpeg', '-o', '-', '-t', '1', '--width', str(width), '--height', str(height)],
        capture_output=True, timeout=10
    )
    if result.returncode != 0:
        raise RuntimeError(f'rpicam-jpeg failed: {result.stderr.decode()}')
    return result.stdout
