"""Application version resolution for the appliance (WP-R2; AC-24).

The version reported in the retained MQTT state/discovery documents (and later
surfaced in the web UI) is resolved from, in order:

1. the optional ``version`` field of the image's ``build-info.json``
   (``/usr/share/prusa-buddy3d-camera/build-info.json``),
2. the ``PRUSA_APP_VERSION`` environment variable,
3. the development default ``0.0.0+dev``.

The build-info file is written by ``image/assets/build-info.py``. This module
never invents a release process (WP-R5 owns versioning): it only reads an
optional field and falls back. Resolution is bounded (the file is read with a
size cap) and sanitized (control characters dropped, length capped), and it
never raises.

Stdlib only, and no file/network/thread side effects on import.
"""
import json
import os

#: Default development version when neither source supplies one.
DEFAULT_VERSION = '0.0.0+dev'

#: Environment override, used when build-info.json has no ``version`` field.
ENV_VAR = 'PRUSA_APP_VERSION'

#: In-image build-info document.
BUILD_INFO_PATH = '/usr/share/prusa-buddy3d-camera/build-info.json'

#: Bound the build-info read so a corrupt/huge file cannot stall startup.
MAX_BUILD_INFO_BYTES = 64 * 1024

#: Bound the sanitized version string.
MAX_VERSION_LENGTH = 128


def _sanitize(value):
    """Return a bounded, printable version string (or ``''``)."""
    if not isinstance(value, str):
        return ''
    cleaned = ''.join(ch for ch in value if ch.isprintable())
    return cleaned.strip()[:MAX_VERSION_LENGTH]


def _build_info_version(path):
    """Read the optional ``version`` field from ``path``; ``''`` on any failure."""
    if not isinstance(path, str) or not path:
        return ''
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read(MAX_BUILD_INFO_BYTES)
    except OSError:
        return ''
    try:
        doc = json.loads(text)
    except ValueError:
        return ''
    if not isinstance(doc, dict):
        return ''
    return _sanitize(doc.get('version'))


def application_version(build_info_path=BUILD_INFO_PATH, env=None):
    """Resolve the application version; never raises.

    ``build_info_path`` and ``env`` are injectable so the precedence is
    host-testable. A missing/unreadable build-info file, an absent environment
    variable, or a blank value falls through to the next source and finally to
    :data:`DEFAULT_VERSION`.
    """
    version = _build_info_version(build_info_path)
    if version:
        return version
    environment = os.environ if env is None else env
    try:
        value = environment.get(ENV_VAR)
    except Exception:  # noqa: BLE001 - a broken env mapping must not raise
        value = None
    version = _sanitize(value)
    if version:
        return version
    return DEFAULT_VERSION


__all__ = [
    'DEFAULT_VERSION',
    'ENV_VAR',
    'BUILD_INFO_PATH',
    'MAX_BUILD_INFO_BYTES',
    'MAX_VERSION_LENGTH',
    'application_version',
]
