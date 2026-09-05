"""Shared bounded executor for CPU-heavy image rendering."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from threading import RLock
from typing import Any, Callable, TypeVar


T = TypeVar("T")
IMAGE_RENDER_CONCURRENCY = 2

_executor: ThreadPoolExecutor | None = None
_executor_lock = RLock()
_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=IMAGE_RENDER_CONCURRENCY,
                thread_name_prefix="image-render",
            )
        return _executor


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore, _semaphore_loop
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not loop:
        _semaphore_loop = loop
        _semaphore = asyncio.Semaphore(IMAGE_RENDER_CONCURRENCY)
    return _semaphore


async def run_image_render(
    renderer: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Run a synchronous image renderer without blocking the event loop."""
    async with _get_semaphore():
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            _get_executor(),
            partial(renderer, *args, **kwargs),
        )


async def close_image_executor() -> None:
    """Stop the shared image executor and allow lazy recreation."""
    global _executor, _semaphore, _semaphore_loop
    with _executor_lock:
        executor = _executor
        _executor = None
    _semaphore = None
    _semaphore_loop = None
    if executor is not None:
        await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
