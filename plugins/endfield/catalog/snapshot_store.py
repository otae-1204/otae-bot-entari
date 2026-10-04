"""Atomic publication of the public collection snapshots and their baselines."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from otae_bot.infrastructure.storage.json_store import JsonStore


async def persist_snapshot(store: JsonStore, lock: asyncio.Lock, **changes: Any) -> None:
    async with lock:
        task = asyncio.create_task(asyncio.to_thread(_persist, store, changes))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # to_thread cannot be cancelled. Finish the write before releasing the lock.
            await task
            raise


def _persist(store: JsonStore, changes: dict[str, Any]) -> None:
    data = {**store._data, **changes}
    data.pop("previous", None)  # Obsolete rolling medal baseline.
    payload = json.dumps(data, ensure_ascii=False, indent=4)
    path = store._path
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
        os.replace(temporary, path)
        # Readers see the new current/baseline together, only after a successful save.
        store._data = data
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
