"""Firmware timezone handling (GAP-STATUS-04).

Recovered from lp_app 3.1.6:

- ``FUN_000b1dc8`` GETs ``timezone.prusa3d.com/`` and reads the JSON
  ``timezone`` field (``status`` must be ``success``); it follows a 301
  ``Location`` redirect.
- ``FUN_000b1620`` converts the API value to the POSIX convention used in
  ``/etc/TZ`` by swapping the ``UTC+``/``UTC-`` prefix.
- ``FUN_000b170c`` writes the converted value to ``/etc/TZ`` (max 64 chars;
  logs "Timezone string too long" beyond that).
- ``FUN_000b130c`` reads ``/etc/TZ``; the status message reports that content.

Stdlib-only and side-effect free on import so it is host-testable.
"""
import json

TIMEZONE_URL = 'https://timezone.prusa3d.com/'
TZ_FILE = '/etc/TZ'
TZ_MAX_LEN = 64


def convert_timezone(tz):
    """Swap the ``UTC+``/``UTC-`` prefix (firmware ``FUN_000b1620``).

    The API returns the offset in the conventional sign; ``/etc/TZ`` uses the
    POSIX convention where the sign is inverted, hence the swap.
    """
    if tz.startswith('UTC+'):
        return 'UTC-' + tz[4:]
    if tz.startswith('UTC-'):
        return 'UTC+' + tz[4:]
    return tz


def parse_timezone_response(body):
    """Extract the ``timezone`` field; require ``status == 'success'``."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('status') != 'success':
        return None
    tz = data.get('timezone')
    return tz if isinstance(tz, str) and tz else None


def read_tz_file(path=TZ_FILE):
    """Return the trimmed ``/etc/TZ`` content, or ``''`` when unreadable."""
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ''


def write_tz_file(value, path=TZ_FILE):
    """Write ``value`` to ``/etc/TZ``. Rejects empty/over-long values (firmware cap)."""
    if not value or len(value) > TZ_MAX_LEN:
        return False
    try:
        with open(path, 'w') as f:
            f.write(value + '\n')
        return True
    except OSError:
        return False


def resolve_tz_name(raw, path=TZ_FILE):
    """Convert an API value, persist it, and return the value to report.

    The report value is the persisted ``/etc/TZ`` content when the write
    succeeded (firmware reads it back), else the converted value so status still
    carries the detected timezone on a read-only filesystem.
    """
    converted = convert_timezone(raw)
    write_tz_file(converted, path)
    return read_tz_file(path) or converted
