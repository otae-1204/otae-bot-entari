"""Event-loop lag watchdog: report blocking calls that stall every plugin."""

from __future__ import annotations

import asyncio
from datetime import datetime
from time import perf_counter

from loguru import logger


DEFAULT_INTERVAL_SECONDS = 0.5
DEFAULT_WARN_SECONDS = 0.2
DEFAULT_ERROR_SECONDS = 1.0

_task: asyncio.Task[None] | None = None


def _timestamp() -> str:
    """Local time in the same seconds/milliseconds shape as the other logs."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _lag_level(
    lag_seconds: float,
    warn_seconds: float,
    error_seconds: float,
) -> str | None:
    """Return the level a measured lag deserves, or None when acceptable."""
    if lag_seconds >= error_seconds:
        return "ERROR"
    if lag_seconds >= warn_seconds:
        return "WARNING"
    return None


async def watch_loop(
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    warn_seconds: float = DEFAULT_WARN_SECONDS,
    error_seconds: float = DEFAULT_ERROR_SECONDS,
) -> None:
    """Sleep in a loop and log how much longer than requested each sleep took.

    A sleep only overruns when the event loop was busy, so the excess is the
    stall a blocking call caused; the wall-clock stamp lets one line be matched
    against the logs of the work that stalled the loop.
    """
    while True:
        started = perf_counter()
        await asyncio.sleep(interval_seconds)
        lag = perf_counter() - started - interval_seconds
        level = _lag_level(lag, warn_seconds, error_seconds)
        if level == "ERROR":
            logger.error(f"[watchdog] event loop blocked {lag:.3f}s at {_timestamp()}")
        elif level == "WARNING":
            logger.warning(f"[watchdog] event loop lag {lag:.3f}s at {_timestamp()}")


async def start_loop_watchdog(
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    warn_seconds: float = DEFAULT_WARN_SECONDS,
    error_seconds: float = DEFAULT_ERROR_SECONDS,
) -> asyncio.Task[None]:
    """Start the shared watchdog; later calls return the running task."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(
            watch_loop(interval_seconds, warn_seconds, error_seconds),
            name="loop-watchdog",
        )
    return _task


async def close_loop_watchdog() -> None:
    """Cancel the shared watchdog and allow a fresh one to be started."""
    global _task
    task, _task = _task, None
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
