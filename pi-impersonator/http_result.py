"""HTTP result classification and bounded retry policy (GAP-HTTP-02).

Pure stdlib so the decision table is host-testable without ``aiohttp``.

Direct Buddy3D 3.1.6 decompiler evidence for the result classes:

* Snapshot response handler ``FUN_0005c568`` compares the first three bytes of
  the response text against ``"200"``, ``"204"`` and ``"403"`` (literal pool at
  ``0x0005c868``/``0x0005c86c``/``0x0005c870``). The ``403`` branch logs
  ``"Upload image BLOCKED by server!"`` (``lp_app.strings:8304``); every other
  non-``200``/``204`` value logs ``"Upload image failed! Status code: %s"``.
* ``/c/info`` builder ``FUN_00062d74`` clears its dirty flag only when the
  response text begins with ``"200"`` (``DAT_00063bdc``); any other result logs
  ``"Upload info message failed!"`` (``DAT_00063bf0``).

Because the firmware special-cases exactly one code, ``BLOCKED_STATUSES`` is
exactly ``{403}``. Other 4xx values (including 429) are ordinary
``client_error`` and are not retried. The firmware shows no redirect following,
so ``redirect`` is classified but never auto-followed (see ``upload.py``).
"""
import asyncio

SUCCESS = 'success'
REDIRECT = 'redirect'
BLOCKED = 'blocked'
CLIENT_ERROR = 'client_error'
SERVER_ERROR = 'server_error'
TIMEOUT = 'timeout'
CONNECTION_ERROR = 'connection_error'

# Firmware-equivalent blocked class: the only code with a dedicated branch.
BLOCKED_STATUSES = frozenset({403})

# Transient failures worth retrying; 4xx client errors are not.
RETRYABLE_RESULTS = frozenset({BLOCKED, SERVER_ERROR, TIMEOUT, CONNECTION_ERROR})

# Firmware retries /c/info on a 10 s countdown until the response is 200. The
# task contract requires a finite bound, so after this many consecutive retries
# (at most MAX_INFO_RETRIES + 1 attempts including the first) the dirty flag is
# cleared until a new change re-marks it.
MAX_INFO_RETRIES = 10

BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0


def classify_status(status):
    """Map an HTTP status code to a result class."""
    try:
        status = int(status)
    except (TypeError, ValueError):
        return CLIENT_ERROR
    if 200 <= status < 300:
        return SUCCESS
    if 300 <= status < 400:
        return REDIRECT
    if status in BLOCKED_STATUSES:
        return BLOCKED
    if 400 <= status < 500:
        return CLIENT_ERROR
    if 500 <= status < 600:
        return SERVER_ERROR
    return CLIENT_ERROR


def classify_exception(exc):
    """Map a transport exception to ``timeout`` or ``connection_error``.

    ``aiohttp`` is deliberately not imported here; the uploader catches
    ``aiohttp.ClientError`` itself and passes the exception through, so any
    non-timeout exception is a connection-class failure.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return TIMEOUT
    return CONNECTION_ERROR


def is_retryable(result_class):
    """True when a result class is transient and may be retried."""
    return result_class in RETRYABLE_RESULTS


def retry_delay(failures):
    """Bounded exponential backoff in seconds after ``failures`` failures.

    ``failures`` is the count of consecutive failures already recorded (0 for
    the first failure). The delay never exceeds ``BACKOFF_CAP_SECONDS``.
    """
    return min(BACKOFF_BASE_SECONDS * (2 ** max(0, failures)), BACKOFF_CAP_SECONDS)


def should_retry(result_class, failures, max_attempts=MAX_INFO_RETRIES):
    """Whether ``failures`` consecutive failures may still be retried.

    Non-retryable classes (success, redirect, ordinary 4xx) never retry; a
    transient class retries while fewer than ``max_attempts`` have failed.
    """
    return is_retryable(result_class) and failures < max_attempts
