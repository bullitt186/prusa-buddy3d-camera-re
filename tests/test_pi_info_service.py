import sys
import unittest
from pathlib import Path


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import http_result  # noqa: E402
import info_service  # noqa: E402


class NextInfoActionTests(unittest.TestCase):
    """GAP-INFO-01: attempt only when dirty and the countdown reaches zero."""

    def test_not_dirty_never_attempts(self):
        self.assertEqual(info_service.next_info_action(False, 0), (False, 0))
        self.assertEqual(info_service.next_info_action(False, 7), (False, 7))

    def test_dirty_with_countdown_waits_and_decrements(self):
        self.assertEqual(info_service.next_info_action(True, 3), (False, 2))
        self.assertEqual(info_service.next_info_action(True, 1), (False, 0))

    def test_dirty_at_zero_attempts(self):
        self.assertEqual(info_service.next_info_action(True, 0), (True, 0))
        self.assertEqual(info_service.next_info_action(True, -1), (True, 0))

    def test_firmware_countdown_reaches_zero_after_ten_ticks(self):
        # A failed attempt reloads the countdown to 10; the next attempt happens
        # exactly ten one-second ticks later (firmware +0x44 reload).
        countdown = info_service.INFO_RETRY_COUNTDOWN
        waits = 0
        while True:
            attempt, countdown = info_service.next_info_action(True, countdown)
            if attempt:
                break
            waits += 1
        self.assertEqual(waits, info_service.INFO_RETRY_COUNTDOWN)


class DirtyAfterResultTests(unittest.TestCase):
    def test_success_clears_dirty(self):
        self.assertFalse(info_service.info_dirty_after_result(http_result.SUCCESS, 0))

    def test_transient_failure_keeps_dirty(self):
        for result in (http_result.SERVER_ERROR, http_result.TIMEOUT,
                       http_result.CONNECTION_ERROR, http_result.BLOCKED):
            with self.subTest(result=result):
                self.assertTrue(info_service.info_dirty_after_result(result, 0))

    def test_client_error_clears_dirty(self):
        self.assertFalse(info_service.info_dirty_after_result(http_result.CLIENT_ERROR, 0))
        self.assertFalse(info_service.info_dirty_after_result(http_result.REDIRECT, 0))

    def test_bounded_retries_stop_clearing_dirty(self):
        self.assertFalse(info_service.info_dirty_after_result(
            http_result.SERVER_ERROR, http_result.MAX_INFO_RETRIES))


class CountdownAfterResultTests(unittest.TestCase):
    def test_success_and_client_error_have_no_countdown(self):
        self.assertEqual(info_service.countdown_after_result(http_result.SUCCESS, 0), 0)
        self.assertEqual(info_service.countdown_after_result(http_result.CLIENT_ERROR, 0), 0)

    def test_transient_failure_reloads_to_firmware_countdown(self):
        self.assertEqual(
            info_service.countdown_after_result(http_result.SERVER_ERROR, 0),
            info_service.INFO_RETRY_COUNTDOWN,
        )

    def test_backoff_grows_but_is_bounded(self):
        values = [info_service.countdown_after_result(http_result.SERVER_ERROR, n)
                  for n in range(http_result.MAX_INFO_RETRIES)]
        self.assertEqual(values[0], info_service.INFO_RETRY_COUNTDOWN)
        self.assertEqual(values, sorted(values))
        self.assertLessEqual(values[-1], http_result.BACKOFF_CAP_SECONDS)

    def test_exhausted_retries_have_no_countdown(self):
        self.assertEqual(info_service.countdown_after_result(
            http_result.SERVER_ERROR, http_result.MAX_INFO_RETRIES), 0)


class ServiceLoopRecoveryTests(unittest.TestCase):
    """Acceptance: injected failures recover (dirty clears) without a restart."""

    def _drive(self, results, max_ticks=200):
        dirty, countdown, failures = True, 0, 0
        attempts = 0
        for _ in range(max_ticks):
            attempt, countdown = info_service.next_info_action(dirty, countdown)
            if not attempt:
                if not dirty:
                    failures = 0
                continue
            result = results[attempts] if attempts < len(results) else results[-1]
            attempts += 1
            if result == http_result.SUCCESS:
                dirty, countdown, failures = False, 0, 0
                break
            dirty = info_service.info_dirty_after_result(result, failures)
            countdown = info_service.countdown_after_result(result, failures)
            failures += 1
            if not dirty:
                break
        return dirty, attempts

    def test_transient_failures_then_success(self):
        results = [http_result.SERVER_ERROR, http_result.TIMEOUT, http_result.SUCCESS]
        dirty, attempts = self._drive(results)
        self.assertFalse(dirty)
        self.assertEqual(attempts, 3)

    def test_client_error_stops_without_retry(self):
        dirty, attempts = self._drive([http_result.CLIENT_ERROR])
        self.assertFalse(dirty)
        self.assertEqual(attempts, 1)

    def test_persistent_transient_failure_is_bounded(self):
        # One initial attempt plus MAX_INFO_RETRIES retries, then the dirty flag
        # is cleared so the loop cannot retry forever.
        dirty, attempts = self._drive([http_result.SERVER_ERROR], max_ticks=500)
        self.assertFalse(dirty)
        self.assertEqual(attempts, http_result.MAX_INFO_RETRIES + 1)


if __name__ == '__main__':
    unittest.main()
