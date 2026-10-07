"""Small helpers shared by the bilibilibot test modules."""

from __future__ import annotations

import asyncio
import functools


def asyncio_test(fn):
    """Run one coroutine test on a fresh loop (this repo has no asyncio plugin)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


async def open_store(bili, tmp_path):
    store = bili.store.BiliStore(tmp_path / "bilibili.db", tmp_path / "missing.db")
    await store.open()
    return store


def live_obs(bili, **kwargs):
    kwargs.setdefault("uid", "123")
    kwargs.setdefault("room_id", "456")
    return bili.models.LiveObservation(**kwargs)
