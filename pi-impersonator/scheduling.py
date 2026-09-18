"""Pure monotonic snapshot scheduling (GAP-SNAPSHOT-04, scheduling half).

The firmware snapshot timer is start-to-start: the cadence is measured from the
start of one upload cycle to the start of the next, so capture/upload duration
does not inflate the interval. ``next_deadline`` returns an absolute monotonic
instant for the next start; a late cycle (long capture, or a shortened interval)
is clamped to ``now`` so the loop fires immediately instead of sleeping a
negative duration or busy-spinning.

The architectural half of GAP-SNAPSHOT-04 (one shared camera source so snapshots
do not pause for RTSP/WebRTC) stays open.
"""
import time


def next_deadline(last_start, interval, now=None):
    """Absolute monotonic start-to-start deadline.

    ``last_start`` is the monotonic time the previous cycle started; ``interval``
    is the current cadence in seconds. Returns ``last_start + interval``, clamped
    up to ``now`` so a missed deadline (long request, shortened interval) is
    serviced immediately and then re-anchored.
    """
    if now is None:
        now = time.monotonic()
    return max(last_start + interval, now)


def time_until(deadline, now=None):
    """Seconds to sleep before ``deadline``; never negative."""
    if now is None:
        now = time.monotonic()
    return max(0.0, deadline - now)
