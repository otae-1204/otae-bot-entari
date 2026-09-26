"""Live duration and live state-transition tests.

The timing rules used to live in `service._check_live_target`; the refactor
moved them verbatim into the pure `detect.detect_live` (refactor plan section
5.3, line 398) and made `poller.Poller` the only caller. This file keeps every
assertion of the old suite:

* the parametrised duration and session-refresh cases now call `detect_live`
  directly (same parameters, same expected values);
* one poller integration test drives a real temporary database through
  开播 → 进行中 → 下播 → 新一场 with a recording send, so the "start, poll,
  restart, end, next session" sequence is still covered end to end;
* the API start parsing, the mid-stream subscription routes, the card rendering
  and the "other card types" checks are unchanged.
"""

from __future__ import annotations

import asyncio
import functools
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest
from PIL import ImageDraw

from tests.test_core_logic import (
    _load_bili_new_module,
    _load_bili_subpackage,
    _load_module,
)

START = 1790059295  # 2026-09-22 14:41:35, UTC+8.
NOW = START + 2 * 3600 + 18 * 60


def asyncio_test(fn):
    """Run one coroutine test on a fresh loop (this repo has no asyncio plugin)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@pytest.fixture(scope="module")
def bili():
    """Load every module inside one synthetic package.

    Sharing a single package keeps `models` a single set of classes, so a
    `BiliCard` built here is the same type the poller, the notifier and the
    renderer see.
    """
    poller = _load_bili_new_module("poller")
    package = poller.__package__

    def in_package(name):
        key = f"{package}.{name}"
        if key in sys.modules:
            return sys.modules[key]
        # The api module is a directory package, so it needs a search path.
        if name == "api":
            return _load_bili_subpackage(package, name)
        return _load_module(key, f"plugins/bilibilibot/{name}.py")

    return SimpleNamespace(
        poller=poller,
        detect=in_package("detect"),
        notifier=in_package("notifier"),
        store=sys.modules[package + ".store"],
        models=sys.modules[package + ".models"],
        draw=in_package("draw"),
        api=in_package("api"),
    )


def live_obs(bili, **kwargs):
    kwargs.setdefault("uid", "123")
    kwargs.setdefault("room_id", "456")
    return bili.models.LiveObservation(**kwargs)


class FakeLiveApi:
    """The live half of the transport: one canned observation per round."""

    def __init__(self, observation=None, error=None):
        self.observation = observation
        self.error = error
        self.batch_calls = []
        self.room_calls = []

    async def batch_live_status(self, uids):
        self.batch_calls.append(list(uids))
        if self.error is not None:
            raise self.error
        if self.observation is None:
            return {}
        return {uid: self.observation for uid in uids}

    async def live_observation(self, target):
        self.room_calls.append(target.uid)
        if self.error is not None:
            raise self.error
        return self.observation


async def open_store(bili, tmp_path):
    store = bili.store.BiliStore(tmp_path / "bilibili.db", tmp_path / "missing.db")
    await store.open()
    return store


async def drain_outbox(bili, store, sent, clock):
    """Deliver whatever the poller committed, recording every send."""
    recorded = []

    async def render(card):
        return b"png"

    async def send(row, png):
        recorded.append((row.card_type, row.card()))

    notifier = bili.notifier.Notifier(
        store, render=render, send=send, clock=lambda: clock[0]
    )
    await notifier.start()
    try:
        for _ in range(200):
            if await store.outbox_count() == 0:
                break
            await asyncio.sleep(0.01)
    finally:
        await notifier.stop()
    sent.extend(recorded)


def _async_return(value):
    async def _call(*args, **kwargs):
        return value

    return _call


@pytest.mark.parametrize(
    "status,live_time,expected",
    [
        (1, "2026-09-22 14:41:35", START),
        (1, "2026-09-23 14:41:35", 0),
        (1, "0000-00-00 00:00:00", 0),
        (1, "invalid", 0),
        (1, "", 0),
        (1, None, 0),
        (0, "2026-09-22 14:41:35", 0),
        (2, "2026-09-22 14:41:35", 0),
    ],
)
def test_api_start_uses_beijing_timezone_and_rejects_invalid_values(
    bili, monkeypatch, status, live_time, expected
):
    monkeypatch.setattr(time, "time", lambda: NOW)
    assert (
        bili.api.BiliApi._live_start_timestamp(
            {"live_status": status, "live_time": live_time}
        )
        == expected
    )


@pytest.mark.parametrize("resolve_by_uid", [False, True])
def test_subscribing_mid_stream_keeps_api_start_in_both_resolution_routes(
    bili, monkeypatch, resolve_by_uid
):
    monkeypatch.setattr(time, "time", lambda: NOW)
    client = bili.api.BiliApi()
    room = {
        "uid": 123,
        "room_id": 456,
        "live_status": 1,
        "live_time": "2026-09-22 14:41:35",
    }
    client._live_room = _async_return(room)
    client._live_user = _async_return({"room_id": 456, "info": {"uname": "主播"}})
    target = asyncio.run(
        client.resolve_live_by_uid("123")
        if resolve_by_uid
        else client.resolve_live_by_room("456")
    )
    assert target.live_started_at == START
    assert target.live_last_seen_at == NOW
    assert target.is_live
    client._live_room = _async_return(room)
    latest = asyncio.run(client.latest_live_state(target))
    assert latest.live_started_at == START
    assert latest.live_last_seen_at == NOW


def test_existing_database_migrates_without_losing_subscriptions_and_persists_timing(
    bili, tmp_path
):
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
        INSERT INTO targets(kind, uid, name, is_live) VALUES('live', '123', '主播', 1);
        INSERT INTO targets(kind, uid, latest_ts) VALUES('video', '123', 77);
        INSERT INTO subscriptions(target_kind,target_uid,subscriber_type,subscriber_id,created_at)
            VALUES('live', '123', 'group', '900', 1);
    """)
    connection.close()

    async def scenario():
        store = bili.store.BiliStore(path, tmp_path / "missing.db")
        await store.open()
        try:
            target = await store.get_target("live", "123")
            assert (
                target.name,
                target.is_live,
                target.live_started_at,
                target.live_last_seen_at,
            ) == ("主播", True, 0, 0)
            assert (await store.get_target("video", "123")).latest_ts == 77
            assert len(await store.subscriptions_for_target("live", "123")) == 1
            target.live_started_at, target.live_last_seen_at = START, NOW - 60
            await store.upsert_target(target)
        finally:
            await store.close()
        # Reopening also exercises idempotent migration and restart persistence.
        store = bili.store.BiliStore(path, tmp_path / "missing.db")
        await store.open()
        try:
            target = await store.get_target("live", "123")
            assert (target.live_started_at, target.live_last_seen_at) == (
                START,
                NOW - 60,
            )
            assert len(await store.subscriptions_for_target("live", "123")) == 1
        finally:
            await store.close()

    asyncio.run(scenario())


@asyncio_test
async def test_start_poll_restart_end_and_next_session(bili, tmp_path):
    """The old service-driven sequence, now driven by the poller end to end."""
    path, legacy = tmp_path / "bili.db", tmp_path / "missing.db"
    clock = [NOW - 120]
    sent = []

    store = bili.store.BiliStore(path, legacy)
    await store.open()
    try:
        await store.upsert_target(bili.models.TargetInfo("live", "123"))
        await store.add_subscription("live", "123", "group", "900")
        api = FakeLiveApi(
            live_obs(bili, is_live=True, started_at=START, uname="主播", face="f.png")
        )
        poller = bili.poller.Poller(api, store, clock=lambda: clock[0])

        # 开播：推 live_on，时长未知。
        await poller.tick_live()
        await drain_outbox(bili, store, sent, clock)
        assert [card_type for card_type, _ in sent] == ["live_on"]
        assert sent[0][1].live_duration_seconds is None
        saved = await store.get_target("live", "123")
        assert (saved.is_live, saved.live_started_at, saved.live_last_seen_at) == (
            True,
            START,
            NOW - 120,
        )

        # 进行中：API 暂时不给 start 时不能重置本场 session，也不重复推送。
        clock[0] = NOW - 60
        api.observation = live_obs(bili, is_live=True, started_at=0)
        await poller.tick_live()
        saved = await store.get_target("live", "123")
        assert saved.live_started_at == START
        assert saved.live_last_seen_at == NOW - 60
        assert await store.outbox_count() == 0
        assert len(sent) == 1
    finally:
        await store.close()

    # 重启：状态从库里恢复，下播时长按持久化的开播时间算。
    store = bili.store.BiliStore(path, legacy)
    await store.open()
    try:
        api = FakeLiveApi(live_obs(bili, is_live=False))
        poller = bili.poller.Poller(api, store, clock=lambda: clock[0])
        clock[0] = NOW
        await poller.tick_live()
        await drain_outbox(bili, store, sent, clock)
        assert [card_type for card_type, _ in sent] == ["live_on", "live_off"]
        assert sent[-1][1].live_duration_seconds == 8280
        saved = await store.get_target("live", "123")
        assert (saved.is_live, saved.live_started_at, saved.live_last_seen_at) == (
            False,
            0,
            0,
        )

        # 下播后继续轮询不重复推送。
        clock[0] += 60
        await poller.tick_live()
        assert len(sent) == 2

        # 新一场：开播时间用新的 start，时长按新场算。
        clock[0] += 60
        api.observation = live_obs(bili, is_live=True, started_at=NOW + 100)
        await poller.tick_live()
        clock[0] += 60
        api.observation = live_obs(bili, is_live=False)
        await poller.tick_live()
        await drain_outbox(bili, store, sent, clock)
        assert [card_type for card_type, _ in sent] == [
            "live_on",
            "live_off",
            "live_on",
            "live_off",
        ]
        assert sent[-1][1].live_duration_seconds == 80
    finally:
        await store.close()


@pytest.mark.parametrize(
    "start,last_seen,expected",
    [
        (START, NOW - 60, 8280),
        (START, NOW - 180, 8280),
        (START, NOW - 181, None),
        (START, NOW - 86400, None),
        (START, 0, None),
        (0, NOW - 60, None),
        (START, NOW + 1, None),
        (NOW + 1, NOW - 60, None),
    ],
)
def test_end_estimate_requires_recent_valid_observation(
    bili, start, last_seen, expected
):
    prev = bili.models.TargetInfo(
        "live", "123", is_live=True, live_started_at=start, live_last_seen_at=last_seen
    )
    updated, events = bili.detect.detect_live(prev, live_obs(bili, is_live=False), NOW)
    assert [event.card.live_duration_seconds for event in events] == [expected]
    # 下播后状态重置。
    assert (updated.live_started_at, updated.live_last_seen_at) == (0, 0)
    assert updated.is_live is False


@pytest.mark.parametrize(
    "gap,new_start,expected",
    [
        (60, 0, START),
        (181, 0, 0),
        (3600, NOW - 30, NOW - 30),
        (60, NOW - 30, NOW - 30),
        (60, NOW + 1, START),
    ],
)
def test_live_refresh_uses_new_session_start_and_limits_fallback(
    bili, gap, new_start, expected
):
    prev = bili.models.TargetInfo(
        "live", "123", is_live=True, live_started_at=START, live_last_seen_at=NOW - gap
    )
    updated, events = bili.detect.detect_live(
        prev, live_obs(bili, is_live=True, started_at=new_start), NOW
    )
    assert updated.live_started_at == expected
    assert updated.live_last_seen_at == NOW
    # 刷新 session start 本身不是状态变化，不推送。
    assert events == []


@asyncio_test
async def test_failed_poll_does_not_advance_last_seen_or_send_end(bili, tmp_path):
    # 纯函数层：没有观测结果就原样返回，不推进、不发下播。
    prev = bili.models.TargetInfo(
        "live", "123", is_live=True, live_started_at=START, live_last_seen_at=NOW - 60
    )
    updated, events = bili.detect.detect_live(prev, None, NOW)
    assert updated is prev
    assert events == []

    store = await open_store(bili, tmp_path)
    try:
        await store.upsert_target(prev)
        await store.add_subscription("live", "123", "group", "900")
        # 批量请求失败，退回的逐个房间查询也失败。
        api = FakeLiveApi(error=RuntimeError("timeout"))
        poller = bili.poller.Poller(api, store, clock=lambda: NOW)
        await poller.tick_live()
        assert api.batch_calls == [["123"]]
        assert api.room_calls == ["123"]
        saved = await store.get_target("live", "123")
        assert (saved.live_started_at, saved.live_last_seen_at) == (START, NOW - 60)
        assert saved.is_live is True
        assert await store.outbox_count() == 0
    finally:
        await store.close()


@pytest.mark.parametrize(
    "seconds,text",
    [
        (None, "本次直播时长未知"),
        (-1, "本次直播时长未知"),
        (0, "本次直播不足 1 分钟"),
        (59, "本次直播不足 1 分钟"),
        (60, "本次直播约 1 分钟"),
        (3599, "本次直播约 59 分钟"),
        (3600, "本次直播约 1 小时"),
        (8280, "本次直播约 2 小时 18 分钟"),
        (90061, "本次直播约 25 小时 1 分钟"),
    ],
)
def test_duration_is_rendered_in_header_without_overflow(
    bili, monkeypatch, seconds, text
):
    rendered = []
    original = ImageDraw.ImageDraw.text

    def capture(draw, xy, content, **kwargs):
        if content == text:
            rendered.append(
                draw.textbbox(
                    xy, content, font=kwargs["font"], anchor=kwargs.get("anchor")
                )
            )
        return original(draw, xy, content, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    card = bili.models.BiliCard("live_off", "直播结束", live_duration_seconds=seconds)
    bili.draw._render_bili_card(card, None, None)
    assert len(rendered) == 1
    left, top, right, bottom = rendered[0]
    assert 48 <= left < right <= 852
    assert 90 <= top < bottom < 154


@pytest.mark.parametrize("kind", ["video", "live_on", "live_idle", "dynamic"])
def test_other_card_types_do_not_show_previous_duration(bili, kind):
    card = bili.models.BiliCard(kind, "标题", live_duration_seconds=8280)
    assert "时长" not in bili.draw._status_hint(card, bili.draw._STYLES[kind])
    assert "小时" not in bili.draw._status_hint(card, bili.draw._STYLES[kind])
