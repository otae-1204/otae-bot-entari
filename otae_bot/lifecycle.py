"""Process lock and shared-resource shutdown, independent of plugin features."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import BinaryIO

from otae_bot.adapters.entari import close_scheduled_jobs
from otae_bot.infrastructure.http.client import close_http_client
from otae_bot.infrastructure.rendering.browser import close_browser
from otae_bot.infrastructure.rendering.executor import close_image_executor


def acquire_run_lock(runtime_dir: Path = Path(".runtime")) -> BinaryIO | None:
    runtime_dir.mkdir(exist_ok=True)
    lock_file = (runtime_dir / "bot.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None

    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(f"pid={os.getpid()}\n".encode("utf-8"))
    lock_file.flush()
    return lock_file


async def close_shared_resources() -> None:
    # Stop producers before closing the resources their jobs may still use.
    await close_scheduled_jobs()
    await asyncio.gather(
        close_http_client(),
        close_image_executor(),
        close_browser(),
    )
