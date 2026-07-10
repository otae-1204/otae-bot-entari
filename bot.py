"""otae Bot Entari entrypoint."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from arclet.entari import Cleanup, Entari, WS, listen, load_plugin

import configs.config  # noqa: F401 - load .env before Entari starts
from configs.config import SATORI_CLIENTS
from utils.entari_native import close_scheduled_jobs
from utils.http_client import close_http_client
from utils.image_executor import close_image_executor
from utils.image_utils import close_browser


def _acquire_run_lock():
    runtime_dir = Path(".runtime")
    runtime_dir.mkdir(exist_ok=True)
    lock_path = runtime_dir / "bot.lock"
    lock_file = lock_path.open("a+b")
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


def _network() -> WS:
    client = SATORI_CLIENTS[0] if SATORI_CLIENTS else {}
    return WS(
        host=str(client.get("host", "localhost")),
        port=int(client.get("port", 5500)),
        path=str(client.get("path", "")),
        token=str(client.get("token", "")) or None,
    )


_RUN_LOCK = None
if __name__ == "__main__":
    _RUN_LOCK = _acquire_run_lock()
    if _RUN_LOCK is None:
        print("[ERROR] Another bot-entari instance is already running.")
        print("[ERROR] Run scripts\\stop.bat first if you need to restart it.")
        sys.exit(2)


app = Entari(_network())


@listen(Cleanup)
async def _cleanup_shared_resources():
    await close_scheduled_jobs()
    await asyncio.gather(
        close_http_client(),
        close_image_executor(),
        close_browser(),
    )

# Load migrated plugins that have a real package entrypoint. Directories without
# __init__.py are preserved assets/examples and should not be registered.
for plugin_dir in sorted(Path("plugins").iterdir()):
    if plugin_dir.is_dir() and (plugin_dir / "__init__.py").exists():
        load_plugin(f"plugins.{plugin_dir.name}")


if __name__ == "__main__":
    try:
        app.run()
    finally:
        if _RUN_LOCK is not None:
            _RUN_LOCK.close()
