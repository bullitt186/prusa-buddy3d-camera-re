"""Pure ``/c/info`` dirty-flag / retry-countdown service decisions (GAP-INFO-01).

Recovered firmware service loop ``FUN_00063bfc``:

* sleeps one second per iteration (``FW-INFO-LOOP:33-39``);
* when the dirty flag at ``+0x0d`` is set, calls ``/c/info`` when the countdown
  at ``+0x44`` reaches zero, then reloads the countdown to 10 if still dirty
  (``FW-INFO-LOOP:52-61``);
* a successful response clears the dirty flag (``FW-INFO-BUILD:292-295``).

This module holds only the decision logic, with no asyncio/network imports, so
the loop transitions can be unit-tested on the host. The bounded retry policy
(``http_result``) limits consecutive transient failures; the firmware itself
retries indefinitely, but the task contract requires a finite bound.
"""
import math

from http_result import SUCCESS, retry_delay, should_retry

# Firmware reloads the retry countdown (struct +0x44) to 10 seconds after a
# failed send.
INFO_RETRY_COUNTDOWN = 10


def next_info_action(dirty, countdown):
    """One firmware service step.

    Returns ``(attempt_now, countdown)``. When dirty and the countdown has
    reached zero the caller should send ``/c/info``; otherwise the countdown is
    decremented by one tick.
    """
    if not dirty:
        return False, countdown
    if countdown <= 0:
        return True, 0
    return False, countdown - 1


def info_dirty_after_result(result_class, failures):
    """Whether the dirty flag stays set after an attempt.

    ``failures`` is the count of consecutive retryable failures already
    recorded. Success and non-retryable results clear dirty; transient failures
    keep it set until the bounded retry limit is reached.
    """
    if result_class == SUCCESS:
        return False
    return should_retry(result_class, failures)


def countdown_after_result(result_class, failures):
    """Countdown to store after an attempt.

    Firmware uses a fixed 10 s reload; we start there and grow the delay with
    bounded exponential backoff once it exceeds the firmware cadence. Success
    and non-retryable results return 0 (nothing left to retry).
    """
    if not should_retry(result_class, failures):
        return 0
    return max(INFO_RETRY_COUNTDOWN, int(math.ceil(retry_delay(failures))))
