"""Outbox store tests: the single write transaction and the due/retry/expire API.

Phase 5 made every public store method async and moved the connection onto a
dedicated worker thread (refactor plan, section 5.3). The behaviour asserted here
is unchanged: state, seen marks and outbox rows commit or roll back together, a
recipient only ever has its lowest row in flight, and a reopened database still
holds what the previous run did not deliver.
"""

from __future__ import annotations

import asyncio
import functools
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from tests.test_core_logic import _load_bili_new_module


NOW = 1000


@pytest.fixture
def bili():
    service = _load_bili_new_module("service")
    package = service.__package__
    return SimpleNamespace(
        store=sys.modules[package + ".store"],
        models=sys.modules[package + ".models"],
    )


async def open_store(bili, tmp_path):
    store = bili.store.BiliStore(tmp_path / "bilibili.db", tmp_path / "missing.db")
    await store.open()
    return store


def make_event(bili, card_type="live_on", uid="1", key="live:1:live_on:100"):
    card = bili.models.BiliCard(
        card_type, "标题", uid=uid, url="https://live.bilibili.com/1000"
    )
    return bili.models.BiliEvent("live", uid, card, event_key=key)


async def apply(bili, store, events, *, now=NOW, targets=(), seen=()):
    return await store.apply_poll_result(
        targets,
        seen,
        events,
        now,
        expand=lambda event: [("group", "900"), ("user", "7")],
        event_key=lambda event: event.event_key,
    )


def asyncio_test(fn):
    """Run one coroutine test on a fresh loop (this repo has no asyncio plugin)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@asyncio_test
async def test_apply_poll_result_writes_state_seen_and_outbox_together(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        target = bili.models.TargetInfo("live", "1", is_live=True, live_started_at=500)
        rows = await apply(
            bili,
            store,
            [make_event(bili)],
            targets=[target],
            seen=[bili.models.SeenItem("live", "1", "100", 100)],
        )
        # One row per recipient: expansion happens at write time.
        assert rows == 2
        assert (await store.get_target("live", "1")).is_live
        assert await store.seen_ids("live", "1", ["100", "101"]) == {"100"}
        assert await store.outbox_count() == 2
        assert {
            (row.subscriber_type, row.subscriber_id)
            for row in await store.outbox_rows()
        } == {("group", "900"), ("user", "7")}
    finally:
        await store.close()


@asyncio_test
async def test_outbox_write_failure_rolls_back_state_and_seen(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        target = bili.models.TargetInfo("live", "1", is_live=True, live_started_at=500)

        def explode(event):
            raise RuntimeError("outbox unavailable")

        with pytest.raises(RuntimeError, match="outbox unavailable"):
            await store.apply_poll_result(
                [target],
                [bili.models.SeenItem("live", "1", "100", 100)],
                [make_event(bili)],
                NOW,
                expand=lambda event: [("group", "900")],
                event_key=explode,
            )
        # Nothing may survive a failed transaction.
        assert await store.get_target("live", "1") is None
        assert await store.seen_ids("live", "1", ["100"]) == set()
        assert await store.outbox_count() == 0
    finally:
        await store.close()


@asyncio_test
async def test_duplicate_event_key_cannot_be_written_twice(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        event = make_event(bili)
        await apply(bili, store, [event])
        await apply(bili, store, [event])
        assert await store.outbox_count() == 2  # two recipients, one row each
    finally:
        await store.close()


@asyncio_test
async def test_due_outbox_returns_only_the_oldest_row_per_recipient(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        first = make_event(bili, "live_on", key="live:1:live_on:100")
        second = make_event(bili, "live_off", key="live:1:live_off:200")
        await apply(bili, store, [first, second])
        due = await store.due_outbox(NOW)
        assert len(due) == 2
        assert {row.subscriber_id for row in due} == {"900", "7"}
        assert all(row.card_type == "live_on" for row in due)
        # While the head row is waiting for a retry, the later one must not jump ahead.
        await store.outbox_retry(
            due[0].id, attempts=1, next_attempt_at=NOW + 30, error="boom"
        )
        again = await store.due_outbox(NOW)
        assert [row.subscriber_id for row in again] == ["7"]
    finally:
        await store.close()


@asyncio_test
async def test_outbox_retry_drop_and_count(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        await apply(bili, store, [make_event(bili)])
        row = (await store.due_outbox(NOW))[0]
        await store.outbox_retry(
            row.id, attempts=1, next_attempt_at=NOW + 30, error="boom"
        )
        assert all(item.id != row.id for item in await store.due_outbox(NOW))
        assert await store.due_outbox(NOW + 30)
        await store.outbox_drop(row.id)
        assert await store.outbox_count() == 1
    finally:
        await store.close()


@asyncio_test
async def test_outbox_expire_uses_per_card_type_ages(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        now = 100_000
        old_live = make_event(bili, "live_on", key="live:1:live_on:1")
        old_video = bili.models.BiliEvent(
            "video", "1", bili.models.BiliCard("video", "视频"), event_key="video:1:BV1"
        )
        await apply(bili, store, [old_live, old_video], now=now - 3 * 3600)
        removed = await store.outbox_expire(
            now,
            {
                "live_on": 2 * 3600,
                "live_off": 2 * 3600,
                "video": 24 * 3600,
                "dynamic": 24 * 3600,
            },
        )
        assert removed == 2  # the live rows, not the video rows
        assert await store.outbox_count() == 2
        assert {row.card_type for row in await store.outbox_rows()} == {"video"}
    finally:
        await store.close()


@asyncio_test
async def test_outbox_survives_close_and_reopen(bili, tmp_path):
    store = await open_store(bili, tmp_path)
    try:
        await apply(bili, store, [make_event(bili)])
    finally:
        await store.close()
    store = await open_store(bili, tmp_path)
    try:
        rows = await store.due_outbox(NOW)
        assert len(rows) == 2
        assert rows[0].card().card_type == "live_on"
        assert json.loads(rows[0].card_json)["title"] == "标题"
    finally:
        await store.close()


@asyncio_test
async def test_legacy_database_gains_the_outbox_table(bili, tmp_path):
    path = tmp_path / "existing.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, uid TEXT NOT NULL,
            room_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
            avatar_url TEXT NOT NULL DEFAULT '', latest_id TEXT NOT NULL DEFAULT '',
            latest_ts INTEGER NOT NULL DEFAULT 0, is_live INTEGER NOT NULL DEFAULT 0,
            last_title TEXT NOT NULL DEFAULT '', last_cover TEXT NOT NULL DEFAULT '',
            last_desc TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL DEFAULT 0,
            UNIQUE(kind, uid)
        );
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, target_kind TEXT NOT NULL, target_uid TEXT NOT NULL,
            subscriber_type TEXT NOT NULL, subscriber_id TEXT NOT NULL, created_at INTEGER NOT NULL,
            UNIQUE(target_kind, target_uid, subscriber_type, subscriber_id)
        );
        INSERT INTO targets(kind, uid, name) VALUES('live', '123', '主播');
        INSERT INTO subscriptions(target_kind,target_uid,subscriber_type,subscriber_id,created_at)
            VALUES('live', '123', 'group', '900', 1);
    """)
    connection.close()
    store = bili.store.BiliStore(path, tmp_path / "missing.db")
    await store.open()
    try:
        assert await store.outbox_count() == 0
        assert len(await store.subscriptions_for_target("live", "123")) == 1
        await apply(
            bili, store, [make_event(bili, uid="123", key="live:123:live_on:5")]
        )
        assert await store.outbox_count() == 2
    finally:
        await store.close()


@asyncio_test
async def test_database_work_stays_on_the_worker_thread(bili, tmp_path):
    """The connection is created and used by the store's own thread only."""
    import threading

    store = await open_store(bili, tmp_path)
    try:
        creator = store._run(threading.current_thread)
        assert (await creator).name.startswith("bili-sqlite")
        with pytest.raises(sqlite3.ProgrammingError):
            store.conn.execute("SELECT 1")
    finally:
        await store.close()
