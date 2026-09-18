import sys
import unittest
from pathlib import Path


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

from scheduling import next_deadline, time_until  # noqa: E402


class NextDeadlineTests(unittest.TestCase):
    """GAP-SNAPSHOT-04 (scheduling half): start-to-start monotonic deadline."""

    def test_returns_last_start_plus_interval(self):
        self.assertEqual(next_deadline(100.0, 10.0, now=100.0), 110.0)
        self.assertEqual(next_deadline(100.0, 60.0, now=100.0), 160.0)

    def test_missed_deadline_clamps_to_now(self):
        # A long capture (or shortened interval) must not yield a negative sleep.
        self.assertEqual(next_deadline(100.0, 10.0, now=130.0), 130.0)

    def test_interval_change_catches_up(self):
        # Old 60 s cadence scheduled 160; at t=130 the interval drops to 10 s,
        # so the already-passed 110 deadline clamps to now and fires immediately.
        old_deadline = next_deadline(100.0, 60.0, now=100.0)
        self.assertEqual(old_deadline, 160.0)
        new_deadline = next_deadline(100.0, 10.0, now=130.0)
        self.assertEqual(new_deadline, 130.0)
        self.assertEqual(time_until(new_deadline, now=130.0), 0.0)

    def test_upload_duration_does_not_shift_cadence(self):
        # last_start is anchored before the upload; the next deadline is still
        # last_start + interval regardless of how long the upload took.
        self.assertEqual(next_deadline(100.0, 10.0, now=107.5), 110.0)

    def test_time_until_never_negative(self):
        self.assertEqual(time_until(110.0, now=100.0), 10.0)
        self.assertEqual(time_until(110.0, now=120.0), 0.0)


if __name__ == '__main__':
    unittest.main()
