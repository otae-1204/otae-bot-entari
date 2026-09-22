from __future__ import annotations

import asyncio
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import ImageDraw

from tests.test_core_logic import _load_bili_new_module

START = 1790059295  # 2026-09-22 14:41:35, UTC+8.
NOW = START + 2 * 3600 + 18 * 60


@pytest.fixture(scope="module")
def bili():
    modules = {
        name: _load_bili_new_module(name)
        for name in ("client", "service", "store", "draw")
    }
    modules["models"] = sys.modules[modules["service"].__package__ + ".models"]
    return SimpleNamespace(**modules)


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
    monkeypatch.setattr(bili.client.time, "time", lambda: NOW)
    assert (
        bili.client.BiliClient._live_start_timestamp(
            {"live_status": status, "live_time": live_time}
        )
        == expected
    )


@pytest.mark.parametrize("resolve_by_uid", [False, True])
def test_subscribing_mid_stream_keeps_api_start_in_both_resolution_routes(
    bili, monkeypatch, resolve_by_uid
):
    monkeypatch.setattr(bili.client.time, "time", lambda: NOW)
    client = bili.client.BiliClient()
    room = {
        "uid": 123,
        "room_id": 456,
        "live_status": 1,
        "live_time": "2026-09-22 14:41:35",
    }
    client._live_room = AsyncMock(side_effect=[{}, room] if resolve_by_uid else [room])
    client._live_user = AsyncMock(
        return_value={"room_id": 456, "info": {"uname": "主播"}}
    )
    target = asyncio.run(client.resolve_live_target("123" if resolve_by_uid else "456"))
    assert target.live_started_at == START
    assert target.live_last_seen_at == NOW
    assert target.is_live
    client._live_room = AsyncMock(return_value=room)
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
    store = bili.store.BiliStore(path, tmp_path / "missing.db")
    try:
        target = store.get_target("live", "123")
        assert (
            target.name,
            target.is_live,
            target.live_started_at,
            target.live_last_seen_at,
        ) == ("主播", True, 0, 0)
        assert store.get_target("video", "123").latest_ts == 77
        assert len(store.subscriptions_for_target("live", "123")) == 1
        target.live_started_at, target.live_last_seen_at = START, NOW - 60
        store.upsert_target(target)
    finally:
        store.close()
    # Reopening also exercises idempotent migration and restart persistence.
    store = bili.store.BiliStore(path, tmp_path / "missing.db")
    try:
        target = store.get_target("live", "123")
        assert (target.live_started_at, target.live_last_seen_at) == (START, NOW - 60)
        assert len(store.subscriptions_for_target("live", "123")) == 1
    finally:
        store.close()


def test_start_poll_restart_end_and_next_session(bili, monkeypatch, tmp_path):
    clock = [NOW - 120]
    monkeypatch.setattr(bili.service.time, "time", lambda: clock[0])
    path, legacy = tmp_path / "bili.db", tmp_path / "missing.db"
    store = bili.store.BiliStore(path, legacy)
    client = SimpleNamespace(latest_live_state=AsyncMock())
    service = bili.service.BiliService(store, client)
    service.broadcast = AsyncMock()

    def poll(live, start=0):
        client.latest_live_state.return_value = bili.models.TargetInfo(
            "live", "123", is_live=live, live_started_at=start
        )
        asyncio.run(service._check_live_target(store.get_target("live", "123")))

    try:
        store.upsert_target(bili.models.TargetInfo("live", "123"))
        poll(True, START)
        assert service.broadcast.await_args.args[2].card_type == "live_on"
        assert service.broadcast.await_args.args[2].live_duration_seconds is None
        # A temporarily absent API start must not reset this ongoing session.
        clock[0] = NOW - 60
        poll(True)
        assert store.get_target("live", "123").live_started_at == START
        assert service.broadcast.await_count == 1
        store.close()
        store = bili.store.BiliStore(path, legacy)
        service = bili.service.BiliService(store, client)
        service.broadcast = AsyncMock()
        clock[0] = NOW
        poll(False)
        ended = service.broadcast.await_args.args[2]
        assert (ended.card_type, ended.live_duration_seconds) == ("live_off", 8280)
        saved = store.get_target("live", "123")
        assert (saved.is_live, saved.live_started_at, saved.live_last_seen_at) == (
            False,
            0,
            0,
        )
        clock[0] += 60
        poll(False)
        assert service.broadcast.await_count == 1
        clock[0] += 60
        poll(True, NOW + 100)
        clock[0] += 60
        poll(False)
        assert service.broadcast.await_args.args[2].live_duration_seconds == 80
        assert service.broadcast.await_count == 3
    finally:
        store.close()


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
    bili, monkeypatch, start, last_seen, expected
):
    monkeypatch.setattr(bili.service.time, "time", lambda: NOW)
    target = bili.models.TargetInfo(
        "live", "123", is_live=True, live_started_at=start, live_last_seen_at=last_seen
    )
    latest = bili.models.TargetInfo("live", "123")
    client = SimpleNamespace(latest_live_state=AsyncMock(return_value=latest))
    store = SimpleNamespace(upsert_target=Mock())
    service = bili.service.BiliService(store, client)
    service.broadcast = AsyncMock()
    asyncio.run(service._check_live_target(target))
    assert service.broadcast.await_args.args[2].live_duration_seconds == expected
    client.latest_live_state.assert_awaited_once_with(target)
    store.upsert_target.assert_called_once_with(latest)
    assert latest.live_started_at == latest.live_last_seen_at == 0


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
    bili, monkeypatch, gap, new_start, expected
):
    monkeypatch.setattr(bili.service.time, "time", lambda: NOW)
    target = bili.models.TargetInfo(
        "live", "123", is_live=True, live_started_at=START, live_last_seen_at=NOW - gap
    )
    latest = bili.models.TargetInfo(
        "live", "123", is_live=True, live_started_at=new_start
    )
    service = bili.service.BiliService(
        SimpleNamespace(upsert_target=Mock()),
        SimpleNamespace(latest_live_state=AsyncMock(return_value=latest)),
    )
    service.broadcast = AsyncMock()
    asyncio.run(service._check_live_target(target))
    assert latest.live_started_at == expected
    assert latest.live_last_seen_at == NOW
    service.broadcast.assert_not_awaited()


def test_failed_poll_does_not_advance_last_seen_or_send_end(bili):
    target = bili.models.TargetInfo(
        "live", "123", is_live=True, live_started_at=START, live_last_seen_at=NOW - 60
    )
    store = SimpleNamespace(upsert_target=Mock())
    service = bili.service.BiliService(
        store,
        SimpleNamespace(
            latest_live_state=AsyncMock(side_effect=RuntimeError("timeout"))
        ),
    )
    service.broadcast = AsyncMock()
    with pytest.raises(RuntimeError, match="timeout"):
        asyncio.run(service._check_live_target(target))
    assert (target.live_started_at, target.live_last_seen_at) == (START, NOW - 60)
    store.upsert_target.assert_not_called()
    service.broadcast.assert_not_awaited()


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
