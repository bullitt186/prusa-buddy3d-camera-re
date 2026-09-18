import asyncio
import sys
import unittest
from pathlib import Path


PI_DIR = Path(__file__).resolve().parents[1] / 'pi-impersonator'
sys.path.insert(0, str(PI_DIR))

import http_result  # noqa: E402


class StatusClassificationTests(unittest.TestCase):
    """GAP-HTTP-02: 2xx/3xx/4xx/5xx and the firmware 403 blocked class."""

    def test_success_is_2xx(self):
        for status in (200, 201, 204, 299):
            with self.subTest(status=status):
                self.assertEqual(http_result.classify_status(status), http_result.SUCCESS)

    def test_redirect_is_3xx(self):
        for status in (300, 301, 302, 307, 308):
            with self.subTest(status=status):
                self.assertEqual(http_result.classify_status(status), http_result.REDIRECT)

    def test_403_is_blocked(self):
        # Direct evidence: FUN_0005c568 compares the response text with "403"
        # and logs "Upload image BLOCKED by server!" (lp_app.strings:8304).
        self.assertEqual(http_result.classify_status(403), http_result.BLOCKED)

    def test_other_4xx_is_client_error(self):
        for status in (400, 401, 404, 409, 422, 429):
            with self.subTest(status=status):
                self.assertEqual(http_result.classify_status(status), http_result.CLIENT_ERROR)

    def test_5xx_is_server_error(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertEqual(http_result.classify_status(status), http_result.SERVER_ERROR)

    def test_unknown_or_malformed_status_is_client_error(self):
        for status in (0, 99, 600, None, 'nope'):
            with self.subTest(status=status):
                self.assertEqual(http_result.classify_status(status), http_result.CLIENT_ERROR)


class ExceptionClassificationTests(unittest.TestCase):
    def test_timeout_exception(self):
        self.assertEqual(http_result.classify_exception(asyncio.TimeoutError()), http_result.TIMEOUT)
        self.assertEqual(http_result.classify_exception(TimeoutError()), http_result.TIMEOUT)

    def test_connection_exception(self):
        self.assertEqual(http_result.classify_exception(OSError('reset')), http_result.CONNECTION_ERROR)
        self.assertEqual(http_result.classify_exception(ValueError('x')), http_result.CONNECTION_ERROR)


class RetryPolicyTests(unittest.TestCase):
    def test_retryable_classes(self):
        for result in (http_result.BLOCKED, http_result.SERVER_ERROR,
                       http_result.TIMEOUT, http_result.CONNECTION_ERROR):
            with self.subTest(result=result):
                self.assertTrue(http_result.is_retryable(result))

    def test_non_retryable_classes(self):
        for result in (http_result.SUCCESS, http_result.REDIRECT, http_result.CLIENT_ERROR):
            with self.subTest(result=result):
                self.assertFalse(http_result.is_retryable(result))

    def test_transient_retries_are_bounded(self):
        self.assertTrue(http_result.should_retry(http_result.SERVER_ERROR, 0))
        self.assertTrue(http_result.should_retry(
            http_result.SERVER_ERROR, http_result.MAX_INFO_RETRIES - 1))
        self.assertFalse(http_result.should_retry(
            http_result.SERVER_ERROR, http_result.MAX_INFO_RETRIES))

    def test_client_error_never_retries(self):
        self.assertFalse(http_result.should_retry(http_result.CLIENT_ERROR, 0))

    def test_backoff_is_monotonic_and_capped(self):
        delays = [http_result.retry_delay(n) for n in range(8)]
        self.assertEqual(delays[0], http_result.BACKOFF_BASE_SECONDS)
        self.assertEqual(delays, sorted(delays))
        self.assertLessEqual(delays[-1], http_result.BACKOFF_CAP_SECONDS)
        self.assertEqual(http_result.retry_delay(100), http_result.BACKOFF_CAP_SECONDS)


if __name__ == '__main__':
    unittest.main()
