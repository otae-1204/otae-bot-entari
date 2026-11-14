import asyncio
import tempfile
from pathlib import Path


def _write_temp_html_sync(content: str) -> Path:
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as file:
        file.write(content)
        return Path(file.name)


async def write_temp_html(content: str) -> Path:
    """Write a render document to a temporary ``.html`` file off the event loop.

    Card documents inline fonts and images and run to several MB; writing them on
    the loop stalled every other handler for seconds on a loaded host.
    """
    return await asyncio.to_thread(_write_temp_html_sync, content)


async def _unlink_later(path: str | Path, delay_seconds: float):
    await asyncio.sleep(delay_seconds)
    await asyncio.to_thread(Path(path).unlink, missing_ok=True)


def schedule_temp_file_cleanup(path: str | Path, delay_seconds: float = 300):
    """Remove a temporary file later without blocking message sending."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_unlink_later(path, delay_seconds))
