"""HYW 模型请求重试：超时至多再试一次且缩短时限，429/5xx 与连接错误沿用 HYW_RETRY_*。"""
from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("hyw_frontier")

from hyw_frontier import model_backend


class _RateLimited(Exception):
    status_code = 429


class ReadTimeout(Exception):
    """Same class name as httpx's read timeout."""


class APITimeoutError(Exception):
    pass


class ConnectError(Exception):
    pass


class RetryPolicyTests(unittest.TestCase):
    def _session(self, timeout=90.0, **env):
        values = {key: value for key, value in os.environ.items() if not key.startswith("HYW_RETRY_")}
        values.update(env)
        with patch.dict(os.environ, values, clear=True):
            session = model_backend.ModelSession(None, timeout)
        self.addCleanup(session.runner.close)
        session._interruptible_sleep = AsyncMock()
        return session

    def _run(self, session, errors):
        seen = []

        async def request(request, on_event, timeout=None):
            seen.append(timeout)
            error = errors[min(len(seen), len(errors)) - 1]
            if error is None:
                return {"ok": True}
            raise error

        session._request = request
        try:
            return seen, session.runner.run(session._request_retried({}, None))
        except Exception as error:  # noqa: BLE001 - the test inspects what escaped
            return seen, error

    def test_a_timeout_is_retried_once_with_a_shorter_limit(self):
        session = self._session()
        with self.assertLogs("hyw", "WARNING") as logs:
            seen, outcome = self._run(session, [TimeoutError("sk-SECRET-IN-MESSAGE")])
        self.assertIsInstance(outcome, TimeoutError)
        self.assertEqual(seen, [90, 30])
        self.assertIn("error=TimeoutError status=None", logs.output[0])
        self.assertIn("timeout=30s", logs.output[0])
        self.assertIn("attempts=2 error=TimeoutError", logs.output[-1])
        self.assertNotIn("sk-SECRET-IN-MESSAGE", "\n".join(logs.output))

    def test_a_retried_timeout_can_still_succeed(self):
        session = self._session()
        with self.assertLogs("hyw", "WARNING"):
            seen, outcome = self._run(session, [TimeoutError(), None])
        self.assertEqual(outcome, {"ok": True})
        self.assertEqual(seen, [90, 30])

    def test_sdk_timeouts_count_as_timeouts(self):
        wrapped = APITimeoutError("Request timed out.")
        wrapped.__cause__ = ReadTimeout("read timed out")
        for error in (ReadTimeout("x"), wrapped):
            with self.subTest(error=type(error).__name__):
                session = self._session()
                with self.assertLogs("hyw", "WARNING") as logs:
                    seen, _ = self._run(session, [error])
                self.assertEqual(seen, [90, 30])
                self.assertIn(f"error={type(error).__name__}", logs.output[0])

    def test_rate_limits_keep_the_configured_policy(self):
        session = self._session()
        with self.assertLogs("hyw", "WARNING") as logs:
            seen, outcome = self._run(session, [_RateLimited("slow down")])
        self.assertIsInstance(outcome, _RateLimited)
        self.assertEqual(seen, [90, 90, 90])  # HYW_RETRY_ATTEMPTS 默认 3，次次满额超时
        self.assertIn("error=_RateLimited status=429", logs.output[0])

    def test_connection_errors_keep_the_configured_policy(self):
        session = self._session()
        with self.assertLogs("hyw", "WARNING"):
            seen, _ = self._run(session, [ConnectError("refused")])
        self.assertEqual(seen, [90, 90, 90])

    def test_retries_can_still_be_disabled(self):
        session = self._session(HYW_RETRY_ATTEMPTS="1")
        seen, outcome = self._run(session, [TimeoutError()])
        self.assertIsInstance(outcome, TimeoutError)
        self.assertEqual(seen, [90])

    def test_retry_timeout_is_shorter_and_bounded(self):
        for timeout, expected in ((90, 30), (40, 20), (20, 15), (10, 10), (300, 30)):
            with self.subTest(timeout=timeout):
                self.assertEqual(self._session(timeout).retry_timeout(), expected)

    def test_status_errors_are_not_classified_as_pure_timeouts(self):
        class GatewayTimeout(Exception):
            status_code = 504

        self.assertTrue(model_backend.timeout_failure(TimeoutError()))
        self.assertFalse(model_backend.timeout_failure(GatewayTimeout()))
        self.assertFalse(model_backend.timeout_failure(ConnectError()))
