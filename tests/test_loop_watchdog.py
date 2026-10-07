"""Event-loop watchdog: lag reporting, escalation and lifecycle."""

from __future__ import annotations

import asyncio
import re
import time
import unittest

from loguru import logger

from otae_bot.infrastructure import loop_watchdog


TIMESTAMP = re.compile(r"\d{2}:\d{2}:\d{2}\.\d{3}")


class LoopWatchdogTests(unittest.IsolatedAsyncioTestCase):
    def capture(self) -> list[tuple[str, str]]:
        """Collect ``(level, message)`` pairs from the shared loguru logger."""
        records: list[tuple[str, str]] = []

        def sink(message):
            records.append((message.record["level"].name, message.record["message"]))

        sink_id = logger.add(sink, level="DEBUG", format="{message}")
        self.addCleanup(logger.remove, sink_id)
        return records

    async def run_watchdog(self, records: list[tuple[str, str]], block_seconds: float, **options) -> None:
        """Block the loop for ``block_seconds`` while a watchdog with the given options runs.

        The measured lag lies between ``block_seconds - interval_seconds`` and
        ``block_seconds``, so the thresholds below are chosen against that window.
        """
        task = asyncio.create_task(loop_watchdog.watch_loop(**options))
        try:
            await asyncio.sleep(0.03)  # let the watchdog enter its sleep
            time.sleep(block_seconds)  # a blocking call on the event loop thread
            await asyncio.sleep(0.03)  # let it measure the overrun and log
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_blocking_call_is_reported_as_a_warning_with_the_time(self):
        records = self.capture()
        # Lag 0.08-0.1 s: past warn (0.05 s), far below the default error threshold (1 s).
        await self.run_watchdog(records, 0.1, interval_seconds=0.02, warn_seconds=0.05)

        # The sink sees the whole shared logger, and asyncio's own slow-callback
        # warning ("Executing <Task ...> took ...") lands first whenever debug
        # mode is on -- which IsolatedAsyncioTestCase always enables, and which
        # reaches this sink as soon as any test has imported the plugin package
        # (that installs a LoguruHandler on the root logger). Select the
        # watchdog's own record instead of relying on arrival order.
        warnings = [
            message
            for level, message in records
            if level == "WARNING" and "[watchdog]" in message
        ]
        self.assertTrue(warnings, records)
        self.assertRegex(warnings[0], TIMESTAMP)
        # 只到 warning 档，不应升级为 error。
        self.assertNotIn("ERROR", [level for level, _ in records])

    async def test_longer_stall_escalates_to_error(self):
        records = self.capture()
        # Lag 0.1-0.12 s: past the 0.08 s error threshold.
        await self.run_watchdog(
            records, 0.12, interval_seconds=0.02, warn_seconds=0.03, error_seconds=0.08
        )

        errors = [
            message
            for level, message in records
            if level == "ERROR" and "[watchdog]" in message
        ]
        self.assertTrue(errors, records)
        self.assertRegex(errors[0], TIMESTAMP)

    def test_lag_levels_follow_the_configured_thresholds(self):
        level = loop_watchdog._lag_level
        self.assertIsNone(level(0.0, 0.2, 1.0))
        self.assertIsNone(level(0.199, 0.2, 1.0))
        self.assertEqual(level(0.2, 0.2, 1.0), "WARNING")
        self.assertEqual(level(0.999, 0.2, 1.0), "WARNING")
        self.assertEqual(level(1.0, 0.2, 1.0), "ERROR")

    async def test_start_is_idempotent_and_close_leaves_no_task(self):
        self.addAsyncCleanup(loop_watchdog.close_loop_watchdog)

        first = await loop_watchdog.start_loop_watchdog(interval_seconds=0.05)
        self.assertFalse(first.done())
        self.assertEqual(first.get_name(), "loop-watchdog")
        self.assertIs(
            await loop_watchdog.start_loop_watchdog(interval_seconds=0.05), first
        )

        await loop_watchdog.close_loop_watchdog()
        self.assertTrue(first.cancelled())
        self.assertIsNone(loop_watchdog._task)

        second = await loop_watchdog.start_loop_watchdog(interval_seconds=0.05)
        self.assertIsNot(second, first)
        self.assertFalse(second.done())

    async def test_close_without_a_running_watchdog_is_a_no_op(self):
        await loop_watchdog.close_loop_watchdog()
        self.assertIsNone(loop_watchdog._task)
