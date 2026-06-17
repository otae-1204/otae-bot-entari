import asyncio
from pathlib import Path


async def _unlink_later(path: str | Path, delay_seconds: float):
    await asyncio.sleep(delay_seconds)
    Path(path).unlink(missing_ok=True)


def schedule_temp_file_cleanup(path: str | Path, delay_seconds: float = 300):
    """Remove a temporary file later without blocking message sending."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_unlink_later(path, delay_seconds))
