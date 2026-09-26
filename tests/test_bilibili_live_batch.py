"""Live polling: the batch HTTP path plus the state the poller commits.

The transport is a real `BiliApi` over `httpx.MockTransport`, the store is a
real temporary database and delivery is observed through the outbox, so these
tests cover the batch endpoint, the per-room fallback and the live transitions
in `detect_live` end to end (refactor plan, sections 4.1, 4.5 and stage 3).
"""

from __future__ import annotations

import asyncio
import functools
import json
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

from tests.test_core_logic import (
    _bili_root_package,
    _load_bili_new_module,
    _load_bili_subpackage,
    _load_module,
)


NAV = {
    "code": 0,
    "data": {
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
        }
    },
}


def _load_in_package(package: str, name: str):
    """Load one more module inside the synthetic package the loader created."""
    key = f"{package}.{name}"
    if key in sys.modules:
        return sys.modules[key]
    return _load_module(key, f"plugins/bilibilibot/{name}.py")


@pytest.fixture
def bili():
    poller = _load_bili_new_module("poller")
    package = _bili_root_package(poller)
    return SimpleNamespace(
        poller=poller,
        client=_load_bili_subpackage(package, "api"),
        store=sys.modules[package + ".store"],
        models=sys.modules[package + ".models"],
    )


def asyncio_test(fn):
    """Run one coroutine test on a fresh loop (this repo has no asyncio plugin)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class Backend:
    """Batch endpoint stand-in; get_info answers the per-room fallback."""

    def __init__(self, *, batch=None, batch_code=0, batch_error=None, rooms=None):
        self.batch = batch
        self.batch_code = batch_code
        self.batch_error = batch_error
        self.rooms = dict(rooms or {})
        self.batch_calls: list[list[int]] = []
        self.room_calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/x/web-interface/nav":
            return httpx.Response(200, json=NAV)
        if path == "/room/v1/Room/get_status_info_by_uids":
            self.batch_calls.append(json.loads(request.content.decode())["uids"])
            if self.batch_error is not None:
                raise self.batch_error
            if self.batch_code != 0:
                return httpx.Response(
                    200, json={"code": self.batch_code, "message": "batch down"}
                )
            return httpx.Response(200, json={"code": 0, "data": self.batch or {}})
        if path == "/room/v1/Room/get_info":
            room = str(request.url.params.get("room_id") or "")
            self.room_calls.append(room)
            return httpx.Response(200, json=self.rooms.get(room) or {"code": 1})
        return httpx.Response(200, json={"code": -404, "message": f"unmocked {path}"})


def entry(uid, *, live_status=1, live_time=0, uname="", face="", title="标题"):
    return {
        "uid": int(uid),
        "room_id": int(uid) + 1000,
        "uname": uname,
        "face": face,
        "title": title,
        "live_status": live_status,
        "live_time": live_time,
        "cover_from_user": "cover.jpg",
    }


def live_target(bili, uid, **kwargs):
    return bili.models.TargetInfo("live", uid, **kwargs)


async def build(bili, tmp_path, backend, targets):
    """A real temporary store plus a real client over the mock transport."""
    store = bili.store.BiliStore(tmp_path / "bilibili.db", tmp_path / "missing.db")
    await store.open()
    for target in targets:
        await store.upsert_target(target)
        await store.add_subscription(target.kind, target.uid, "group", "900")
    client = bili.client.BiliApi(transport=httpx.MockTransport(backend), min_interval=0)
    return store, client


@asyncio_test
async def test_batch_request_covers_every_target_in_one_call(bili, tmp_path):
    now = int(time.time())
    backend = Backend(
        batch={
            "1": entry("1", live_time=now - 60, uname="甲"),
            "2": entry("2", live_status=0),
            "3": entry("3", live_status=1, live_time=now - 10, uname="丙"),
        }
    )
    store, client = await build(
        bili,
        tmp_path,
        backend,
        [live_target(bili, "1"), live_target(bili, "2"), live_target(bili, "3")],
    )
    try:
        await bili.poller.Poller(client, store).tick_live()
        assert backend.batch_calls == [[1, 2, 3]]
        assert backend.room_calls == []
        # Two state changes, one outbox row each.
        rows = await store.outbox_rows()
        assert {(row.uid, row.card_type) for row in rows} == {
            ("1", "live_on"),
            ("3", "live_on"),
        }
        assert (await store.get_target("live", "1")).is_live
        assert not (await store.get_target("live", "2")).is_live
        assert (await store.get_target("live", "3")).live_started_at == now - 10
    finally:
        await client.aclose()
        await store.close()


@asyncio_test
async def test_missing_uid_is_not_observed_and_never_ends_a_stream(bili, tmp_path):
    backend = Backend(batch={"2": entry("2", live_status=0)})
    store, client = await build(
        bili,
        tmp_path,
        backend,
        [
            live_target(
                bili, "1", is_live=True, live_started_at=1000, live_last_seen_at=2000
            ),
            live_target(
                bili, "2", is_live=True, live_started_at=1000, live_last_seen_at=2000
            ),
        ],
    )
    try:
        await bili.poller.Poller(client, store).tick_live()
        absent = await store.get_target("live", "1")
        assert (absent.is_live, absent.live_started_at, absent.live_last_seen_at) == (
            True,
            1000,
            2000,
        )
        # A uid missing from a successful batch is not a reason to hit the room API.
        assert backend.room_calls == []
        # Only the observed target may emit an event.
        rows = await store.outbox_rows()
        assert [(row.uid, row.card_type) for row in rows] == [("2", "live_off")]
    finally:
        await client.aclose()
        await store.close()


@asyncio_test
async def test_carousel_status_two_counts_as_not_live(bili, tmp_path):
    now = int(time.time())
    backend = Backend(batch={"1": entry("1", live_status=2, live_time=now - 60)})
    store, client = await build(
        bili,
        tmp_path,
        backend,
        [
            live_target(
                bili,
                "1",
                is_live=True,
                live_started_at=1000,
                live_last_seen_at=now - 60,
            )
        ],
    )
    try:
        await bili.poller.Poller(client, store).tick_live()
        rows = await store.outbox_rows()
        assert [row.card_type for row in rows] == ["live_off"]
        assert not (await store.get_target("live", "1")).is_live
    finally:
        await client.aclose()
        await store.close()


@pytest.mark.parametrize(
    "offset,expected_valid",
    [(0, False), (-300, True), (100000, False)],
)
@asyncio_test
async def test_batch_live_time_is_a_unix_second_within_range(
    bili, tmp_path, offset, expected_valid
):
    now = int(time.time())
    live_time = now + offset if offset else 0
    backend = Backend(batch={"1": entry("1", live_time=live_time)})
    store, client = await build(bili, tmp_path, backend, [live_target(bili, "1")])
    try:
        await bili.poller.Poller(client, store).tick_live()
        stored = await store.get_target("live", "1")
        assert stored.is_live
        assert stored.live_started_at == (live_time if expected_valid else 0)
    finally:
        await client.aclose()
        await store.close()


@asyncio_test
async def test_batch_failure_falls_back_to_per_room_queries(bili, tmp_path):
    now = int(time.time())
    backend = Backend(
        batch_code=-400,
        rooms={
            "1001": {
                "code": 0,
                "data": {
                    "uid": 1,
                    "room_id": 1001,
                    "live_status": 1,
                    "title": "甲",
                    "uname": "甲",
                },
            },
            "1002": {
                "code": 0,
                "data": {
                    "uid": 2,
                    "room_id": 1002,
                    "live_status": 0,
                    "title": "乙",
                    "uname": "乙",
                },
            },
        },
    )
    store, client = await build(
        bili,
        tmp_path,
        backend,
        [
            live_target(bili, "1", room_id="1001"),
            live_target(bili, "2", room_id="1002"),
        ],
    )
    try:
        await bili.poller.Poller(client, store).tick_live()
        assert backend.batch_calls == [[1, 2]]
        assert sorted(backend.room_calls) == ["1001", "1002"]
        rows = await store.outbox_rows()
        assert [(row.uid, row.card_type) for row in rows] == [("1", "live_on")]
        assert (await store.get_target("live", "1")).live_last_seen_at >= now
    finally:
        await client.aclose()
        await store.close()


@asyncio_test
async def test_batch_transport_failure_falls_back_to_per_room_queries(bili, tmp_path):
    """A network failure is a batch failure too, not an excuse to skip the round."""
    backend = Backend(
        batch_error=httpx.ConnectError("batch down"),
        rooms={
            "1001": {
                "code": 0,
                "data": {"uid": 1, "room_id": 1001, "live_status": 1, "uname": "甲"},
            }
        },
    )
    store, client = await build(
        bili, tmp_path, backend, [live_target(bili, "1", room_id="1001")]
    )
    try:
        await bili.poller.Poller(client, store).tick_live()
        assert backend.batch_calls == [[1]]
        assert backend.room_calls == ["1001"]
        rows = await store.outbox_rows()
        assert [(row.uid, row.card_type) for row in rows] == [("1", "live_on")]
    finally:
        await client.aclose()
        await store.close()


@asyncio_test
async def test_batch_env_switch_skips_the_batch_endpoint(bili, tmp_path, monkeypatch):
    """BILI_LIVE_BATCH=0 is the documented rollback path (plan section 6, stage 3).

    With the switch off the poller must never touch the batch endpoint and go
    straight to per-room queries instead.
    """
    monkeypatch.setenv("BILI_LIVE_BATCH", "0")
    backend = Backend(
        batch={"1": entry("1")},
        rooms={
            "1001": {
                "code": 0,
                "data": {
                    "uid": 1,
                    "room_id": 1001,
                    "live_status": 1,
                    "title": "标题",
                    "uname": "甲",
                },
            },
        },
    )
    store, client = await build(
        bili, tmp_path, backend, [live_target(bili, "1", room_id="1001")]
    )
    try:
        await bili.poller.Poller(client, store).tick_live()
        assert backend.batch_calls == []
        assert backend.room_calls == ["1001"]
        rows = await store.outbox_rows()
        assert [(row.uid, row.card_type) for row in rows] == [("1", "live_on")]
    finally:
        await client.aclose()
        await store.close()


@asyncio_test
async def test_batch_endpoint_is_the_default_path(bili, tmp_path, monkeypatch):
    """Without the kill switch the batch endpoint stays the single request."""
    monkeypatch.delenv("BILI_LIVE_BATCH", raising=False)
    backend = Backend(
        batch={"1": entry("1")},
        rooms={
            "1001": {
                "code": 0,
                "data": {
                    "uid": 1,
                    "room_id": 1001,
                    "live_status": 1,
                    "title": "标题",
                    "uname": "甲",
                },
            },
        },
    )
    store, client = await build(
        bili, tmp_path, backend, [live_target(bili, "1", room_id="1001")]
    )
    try:
        await bili.poller.Poller(client, store).tick_live()
        assert backend.batch_calls == [[1]]
        assert backend.room_calls == []
    finally:
        await client.aclose()
        await store.close()


@pytest.mark.parametrize(
    "uname,face,expected",
    [("新名", "new.jpg", ("新名", "new.jpg")), ("", "", ("旧名", "old.jpg"))],
)
@asyncio_test
async def test_batch_syncs_name_and_avatar_but_keeps_old_values_when_blank(
    bili, tmp_path, uname, face, expected
):
    backend = Backend(batch={"1": entry("1", uname=uname, face=face)})
    store, client = await build(
        bili,
        tmp_path,
        backend,
        [live_target(bili, "1", name="旧名", avatar_url="old.jpg")],
    )
    try:
        await bili.poller.Poller(client, store).tick_live()
        stored = await store.get_target("live", "1")
        assert (stored.name, stored.avatar_url) == expected
    finally:
        await client.aclose()
        await store.close()


@asyncio_test
async def test_poll_summary_logs_one_line_per_round(bili, tmp_path):
    """The plan asks for one summary line per round carrying the new fields."""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(
        lambda message: messages.append(message.record["message"]), level="DEBUG"
    )
    backend = Backend(batch={"1": entry("1"), "2": entry("2")})
    store, client = await build(
        bili, tmp_path, backend, [live_target(bili, "1"), live_target(bili, "2")]
    )
    try:
        await bili.poller.Poller(client, store).tick_live()
    finally:
        await client.aclose()
        await store.close()
        logger.remove(sink_id)
    lines = [m for m in messages if "poll kind=live" in m]
    assert len(lines) == 1
    line = lines[0]
    assert "kind=live" in line and "targets=2" in line
    # Both targets were observed in one round, so both succeed and both emit.
    assert "success=2" in line and "failed=0" in line and "events=2" in line
    for field in (
        "due=",
        "success=",
        "failed=",
        "events=",
        "outbox=",
        "backlog_wait=",
        "elapsed=",
    ):
        assert field in line
