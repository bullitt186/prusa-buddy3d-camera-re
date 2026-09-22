"""Exact Prusa pairing-QR parser and bounded scanner (WP-4, AC-21, AC-22).

Captured schema (authoritative)
-------------------------------
A current Prusa Connect "Add WiFi Camera" pairing QR is a UTF-8 JSON object
(70 bytes in the captured sample) with **exactly** these three string keys and
nothing else::

    {"ssid": "<wifi-ssid>", "pwd": "<wifi-password>", "token": "<prusa-token>"}

* ``ssid``  Wi-Fi network name.
* ``pwd``   Wi-Fi password (SECRET).
* ``token`` Prusa camera registration token (SECRET; ~20 chars in the sample).

There is **no version field**, no other key, and no nesting. The exact payload
was decoded locally from a user-provided screenshot; the raw payload and the
image are **not** committed and never logged. The repository records only the
redacted field names/types and a fully synthetic, non-working fixture.

Duplicate JSON keys are tolerated by the underlying decoder with the usual
last-value-wins behaviour; the surviving values are still fully validated.

Provenance
----------
The schema above was captured from a real QR generated through the current
Prusa "Add WiFi Camera" flow and decoded locally on a workstation. It is the
only accepted shape; fields must never be guessed or extended.

Claim-time write semantics
--------------------------
A syntactically valid QR is only ever **staged** in redacted form by the setup
wizard. The ``token``/``pwd`` are written to ``secrets.toml`` **only** when the
wizard commits the claim (source plan §4.4). A rejected or expired token
returns the user to setup **without** overwriting the last-known-good
credentials. The optional explicit fingerprint step is separate: the QR carries
no fingerprint, and fingerprint binding is preserved by the wizard's existing
MAC-derived/explicit-override behaviour.

Bounds and secret hygiene
-------------------------
The parser is bounded (payload bytes, per-string length) and rejects anything
malformed or unrelated. The scanner is bounded by image size, decode duration
and retry frequency. No token or password ever appears in a reason, log line or
``repr``; :func:`redacted` exposes only the SSID.

The module is stdlib-only and import-safe: importing it performs no I/O, no
subprocess and no network access.
"""
import dataclasses
import json
import logging
import math
import shutil
import subprocess
import time

log = logging.getLogger('prusa-cam.qr')

#: The exact, captured key set. Order is the documented field order.
QR_KEYS = ('ssid', 'pwd', 'token')

#: Marker used for every hidden secret value (matches :data:`admin_auth.REDACTED`).
REDACTED = '<redacted>'

#: Maximum accepted QR payload size in bytes.
MAX_PAYLOAD_BYTES = 512

#: Maximum accepted length, in characters, of any single field value.
MAX_STRING_CHARS = 256

#: Maximum accepted encoded image size for one scan.
MAX_IMAGE_BYTES = 2 * 1024 * 1024

#: Default hard timeout, in seconds, for a single image decode.
DEFAULT_DECODE_TIMEOUT = 5

#: Default scan-attempt budget and window for :class:`ScanLimiter`.
MAX_SCAN_ATTEMPTS = 5
SCAN_WINDOW_SECONDS = 60.0

__all__ = [
    'QR_KEYS',
    'REDACTED',
    'MAX_PAYLOAD_BYTES',
    'MAX_STRING_CHARS',
    'MAX_IMAGE_BYTES',
    'DEFAULT_DECODE_TIMEOUT',
    'MAX_SCAN_ATTEMPTS',
    'SCAN_WINDOW_SECONDS',
    'ParseResult',
    'parse_payload',
    'redacted',
    'decode_image',
    'ScanLimiter',
    'decode_and_parse',
]


# --------------------------------------------------------------------------- #
# Result object
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class ParseResult:
    """Outcome of parsing one QR payload.

    ``reason`` is a bounded, non-secret message. ``fields`` is the parsed
    ``{'ssid', 'pwd', 'token'}` mapping on success and ``None`` otherwise. The
    ``repr`` is redacted so a staged result can be logged safely.
    """

    ok: bool
    reason: str = ''
    fields: dict = None

    def redacted(self):
        """Return this result's fields in redacted form (SSID visible only)."""
        return redacted(self.fields)

    def __repr__(self):
        return (
            f'ParseResult(ok={self.ok!r}, reason={self.reason!r}, '
            f'fields={redacted(self.fields)!r})'
        )


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

def redacted(fields):
    """Return a copy of ``fields`` with the password and token hidden.

    The SSID may be shown; ``pwd`` and ``token`` are always :data:`REDACTED`.
    Accepts a mapping or ``None`` and never raises.
    """
    ssid = ''
    if isinstance(fields, dict):
        value = fields.get('ssid')
        if isinstance(value, str) and value:
            ssid = value
    return {'ssid': ssid or REDACTED, 'pwd': REDACTED, 'token': REDACTED}


# --------------------------------------------------------------------------- #
# Payload parsing
# --------------------------------------------------------------------------- #

def _payload_bytes(text):
    """Return ``(raw_bytes, '')`` or ``(None, reason)`` for a payload input."""
    if isinstance(text, str):
        try:
            return text.encode('utf-8'), ''
        except UnicodeEncodeError:
            return None, 'QR payload is not valid UTF-8'
    if isinstance(text, (bytes, bytearray, memoryview)):
        return bytes(text), ''
    return None, 'QR payload must be text or bytes'


def _has_control_char(value):
    """True when ``value`` contains an ASCII/C1 control or a Unicode separator.

    Covers ASCII controls (``< 0x20``), DEL (``0x7F``), the C1 block
    (U+0080-U+009F, including NEL U+0085), and the line/paragraph separators
    U+2028/U+2029.
    """
    for ch in value:
        code = ord(ch)
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F \
                or code in (0x2028, 0x2029):
            return True
    return False


def parse_payload(text):
    """Parse and validate one decoded QR payload.

    Enforces the captured schema exactly: a UTF-8 JSON object whose keys are
    precisely :data:`QR_KEYS`, with non-empty string values bounded by
    :data:`MAX_STRING_CHARS` and free of control characters. The whole payload
    must be at most :data:`MAX_PAYLOAD_BYTES` bytes. Any failure returns
    ``ok=False`` with a non-secret reason; no token or password is ever echoed.
    """
    raw, reason = _payload_bytes(text)
    if raw is None:
        return ParseResult(False, reason, None)
    if len(raw) > MAX_PAYLOAD_BYTES:
        return ParseResult(
            False, f'QR payload exceeds {MAX_PAYLOAD_BYTES} bytes', None
        )
    try:
        decoded = raw.decode('utf-8')
    except UnicodeDecodeError:
        return ParseResult(False, 'QR payload is not valid UTF-8', None)

    try:
        data = json.loads(decoded)
    except (json.JSONDecodeError, ValueError):
        return ParseResult(False, 'QR payload is not valid JSON', None)

    if not isinstance(data, dict):
        return ParseResult(False, 'QR payload must be a JSON object', None)
    if set(data.keys()) != set(QR_KEYS):
        return ParseResult(
            False, 'QR payload must contain exactly ssid, pwd and token', None
        )

    fields = {}
    for key in QR_KEYS:
        value = data[key]
        if not isinstance(value, str):
            return ParseResult(False, f'QR field {key} must be a string', None)
        if not value:
            return ParseResult(False, f'QR field {key} must not be empty', None)
        if len(value) > MAX_STRING_CHARS:
            return ParseResult(
                False,
                f'QR field {key} exceeds {MAX_STRING_CHARS} characters',
                None,
            )
        if _has_control_char(value):
            return ParseResult(
                False, f'QR field {key} contains control characters', None
            )
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            return ParseResult(False, f'QR field {key} is not valid Unicode', None)
        fields[key] = value
    return ParseResult(True, '', fields)


# --------------------------------------------------------------------------- #
# Image decoding
# --------------------------------------------------------------------------- #

class _DecoderUnavailable(Exception):
    """Raised when the default system decoder cannot be used."""


def _default_decoder(data, timeout):
    """Decode ``data`` with ``zbarimg`` over stdin; raise when unavailable.

    The image is piped on stdin, so no temporary file is ever written. A hard
    ``timeout`` bounds the subprocess; a missing ``zbarimg`` raises
    :class:`_DecoderUnavailable` with a clear, non-secret reason.
    """
    path = shutil.which('zbarimg')
    if not path:
        raise _DecoderUnavailable('QR decoder (zbarimg) is not installed')
    try:
        proc = subprocess.run(
            [path, '--quiet', '--raw', '-'],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise
    except OSError:
        raise _DecoderUnavailable('QR decoder (zbarimg) could not be started')
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode('utf-8', 'replace').strip()
    return text or None


def decode_image(data, *, max_bytes=MAX_IMAGE_BYTES, timeout=DEFAULT_DECODE_TIMEOUT,
                 decoder=None):
    """Decode a QR image to text, bounded by size and decode duration.

    Returns ``(ok, reason, text)``. ``decoder`` is injectable with the signature
    ``decoder(data, timeout) -> str | None``; when omitted, ``zbarimg`` is used
    if present and a clear non-secret reason is returned otherwise. No file is
    written and no network is touched.
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return False, 'image data must be bytes', ''
    blob = bytes(data)
    if not blob:
        return False, 'image data is empty', ''
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, (int, float)) \
            or not math.isfinite(max_bytes) or max_bytes <= 0:
        return False, 'image size limit must be a positive number', ''
    if len(blob) > max_bytes:
        return False, f'image exceeds {max_bytes} byte limit', ''
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not math.isfinite(timeout) or timeout <= 0:
        return False, 'decode timeout must be a positive number', ''

    decoder_fn = decoder if decoder is not None else _default_decoder
    try:
        text = decoder_fn(blob, timeout)
    except subprocess.TimeoutExpired:
        return False, 'QR decode timed out', ''
    except _DecoderUnavailable as e:
        return False, str(e), ''
    except Exception:  # noqa: BLE001 - a decoder must never leak internals
        log.warning('qr_pairing: image decode failed')
        return False, 'image decode failed', ''

    if not isinstance(text, str) or not text.strip():
        return False, 'no QR code found', ''
    return True, '', text


# --------------------------------------------------------------------------- #
# Scan rate limiting
# --------------------------------------------------------------------------- #

class ScanLimiter:
    """Bound QR scan attempts to a fixed budget per rolling window.

    ``clock`` is injectable (defaults to :func:`time.monotonic`); :meth:`allow`
    accepts an explicit ``now`` for deterministic tests. The reason string never
    contains payload data.
    """

    def __init__(self, max_attempts=MAX_SCAN_ATTEMPTS,
                 window_seconds=SCAN_WINDOW_SECONDS, clock=None):
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) \
                or max_attempts < 1:
            raise ValueError('max_attempts must be a positive integer')
        if isinstance(window_seconds, bool) \
                or not isinstance(window_seconds, (int, float)) \
                or window_seconds <= 0:
            raise ValueError('window_seconds must be a positive number')
        self.max_attempts = max_attempts
        self.window_seconds = float(window_seconds)
        self._clock = clock or time.monotonic
        self._attempts = []

    def allow(self, now=None):
        """Return ``(allowed, reason)`` and record an allowed attempt."""
        moment = self._clock() if now is None else now
        if isinstance(moment, bool) or not isinstance(moment, (int, float)):
            return False, 'scan clock returned an invalid time'
        cutoff = moment - self.window_seconds
        self._attempts = [t for t in self._attempts if t > cutoff]
        if len(self._attempts) >= self.max_attempts:
            return False, 'too many QR scans; wait before retrying'
        self._attempts.append(moment)
        return True, ''


# --------------------------------------------------------------------------- #
# End-to-end
# --------------------------------------------------------------------------- #

def decode_and_parse(data, *, decoder=None, limiter=None, now=None,
                     max_bytes=MAX_IMAGE_BYTES, timeout=DEFAULT_DECODE_TIMEOUT):
    """Rate-limit, decode and parse one image, returning a :class:`ParseResult`.

    The retry-frequency bound is applied first; the image is then decoded with
    the size/duration bounds (``max_bytes``/``timeout`` are forwarded to
    :func:`decode_image`) and finally parsed with the schema bounds. A rejected
    scan/parse never yields secret material.
    """
    if limiter is not None:
        allowed, reason = limiter.allow(now)
        if not allowed:
            return ParseResult(False, reason, None)
    ok, reason, text = decode_image(data, max_bytes=max_bytes, timeout=timeout,
                                    decoder=decoder)
    if not ok:
        return ParseResult(False, reason, None)
    return parse_payload(text)
