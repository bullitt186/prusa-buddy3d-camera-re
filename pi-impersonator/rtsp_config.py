"""Host-testable configuration for the GStreamer RTSP server."""

import os


DEFAULT_PORT = 8554
DEFAULT_PATH = '/live'
DEFAULT_LABEL = 'Prusa'


def load_rtsp_config(env=None):
    """Return ``(port, path, label)`` from an environment-style mapping.

    Keep this separate from :mod:`rtsp_server`, which imports PyGObject and is
    intentionally unavailable in the stdlib-only host test environment.
    """
    env = os.environ if env is None else env
    raw_port = env.get('RTSP_PORT', str(DEFAULT_PORT))
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'RTSP_PORT must be an integer, got {raw_port!r}') from exc
    if not 1 <= port <= 65535:
        raise ValueError(f'RTSP_PORT must be in 1..65535, got {port!r}')

    path = env.get('RTSP_PATH', DEFAULT_PATH)
    if not isinstance(path, str) or not path.startswith('/') or path == '/':
        raise ValueError(f'RTSP_PATH must be a non-root absolute path, got {path!r}')
    if any(char.isspace() for char in path):
        raise ValueError(f'RTSP_PATH must not contain whitespace, got {path!r}')

    label = env.get('RTSP_LABEL', DEFAULT_LABEL)
    if not isinstance(label, str) or not label.strip():
        label = DEFAULT_LABEL
    return port, path, label.strip()
