"""A loop clock that only moves when a test moves it."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

_sleep = asyncio.sleep


class FakeClock:
    """Patches the running loop's time() and asyncio.sleep for one test.

    asyncio.timeout, wait_for and call_at all follow loop.time(), so a deadline
    fires as soon as `advance` passes it, without real waiting. Each sleep moves
    the clock by its delay and is recorded in `slept`.
    """

    def __init__(self, test):
        loop = asyncio.get_running_loop()
        self.now = loop.time()
        self.slept: list[float] = []
        test.enterContext(patch.object(loop, "time", lambda: self.now))
        # Debug mode would report each advance as a slow callback.
        test.enterContext(patch.object(loop, "slow_callback_duration", float("inf")))
        test.enterContext(patch.object(asyncio, "sleep", self.sleep))

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        # One turn runs the timers now due; the second lets a cancellation they
        # caused land here, before this coroutine returns.
        await _sleep(0)
        await _sleep(0)

    async def sleep(self, seconds: float, result=None):
        self.slept.append(seconds)
        await self.advance(seconds)
        return result
