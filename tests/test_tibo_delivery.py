from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.tibo_radar import handlers
from plugins.tibo_radar.delivery import deliver_page, fallback_text
from plugins.tibo_radar.models import TiboPost
from plugins.tibo_radar.store import TiboStore
from otae_bot.adapters.entari import SendDest


PNG = b"\x89PNG\r\n\x1a\nfixture"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def post(index: int) -> TiboPost:
    return TiboPost(
        str(index).zfill(5), f"Public sample {index}", f"https://example.invalid/{index}",
        source_time=START + timedelta(seconds=index),
        first_seen_at=START + timedelta(seconds=index),
        translation="中文样例", analysis="仅为测试", relevance="direct",
    )


def account():
    return SimpleNamespace(
        adapter="test", config=SimpleNamespace(timeout=30), protocol=SimpleNamespace(
            send_message=AsyncMock(return_value=[SimpleNamespace(id="receipt")]),
        ),
    )


@pytest.fixture
def radar(tmp_path, monkeypatch):
    store = TiboStore(tmp_path / "radar.db")
    store.upsert_post(post(0))
    store.subscribe("group", "channel")
    monkeypatch.setattr(handlers, "store", store)
    monkeypatch.setattr(handlers, "render_xfeed", AsyncMock(return_value=PNG))
    monkeypatch.setattr(handlers, "_notification_lock", asyncio.Lock())
    monkeypatch.setattr(handlers, "_refresh_notify_lock", asyncio.Lock())
    monkeypatch.setattr(handlers, "SUBSCRIPTION_BATCH", 20)
    monkeypatch.setattr(handlers, "SUBSCRIPTION_MAX_PAGES", 1)
    yield store
    store.close()


def notify(bot):
    asyncio.run(handlers._notify_subscriptions(bot))


def test_repeated_updates_deliver_without_restart(radar):
    bot = account()
    for index in range(1, 6):
        radar.upsert_post(post(index))
        notify(bot)
        assert radar.subscription("group").last_notified_post_id == post(index).post_id
    assert bot.protocol.send_message.await_count == 5
    notify(bot)
    assert bot.protocol.send_message.await_count == 5


def test_upload_is_fresh_each_cycle_and_uses_bridge_url(radar):
    bot = account()
    bot.protocol.upload_create = AsyncMock(side_effect=[["internal:one"], ["internal:two"]])
    for index in (1, 2):
        radar.upsert_post(post(index))
        notify(bot)
    calls = bot.protocol.send_message.await_args_list
    assert calls[0].args[1][0].src == "internal:one"
    assert calls[1].args[1][0].src == "internal:two"
    assert bot.protocol.upload_create.await_count == 2
    assert bot.protocol.upload_create.await_args.args[0].file == PNG


def test_unsupported_upload_falls_back_to_inline_image(radar):
    bot = account()
    bot.protocol.upload_create = AsyncMock(side_effect=NotImplementedError)
    radar.upsert_post(post(1))
    notify(bot)
    assert bot.protocol.send_message.await_args.args[1][0].src.startswith("data:")
    assert radar.subscription("group").last_delivery_mode == "image"


@pytest.mark.parametrize("failure", ["render", "image_send"])
def test_image_failure_delivers_text_then_advances_cursor(radar, monkeypatch, failure):
    bot = account()
    if failure == "render":
        monkeypatch.setattr(handlers, "render_xfeed", AsyncMock(side_effect=RuntimeError("render failed")))
    else:
        bot.protocol.send_message.side_effect = [RuntimeError("media rejected"), [SimpleNamespace(id="text")]]
    radar.upsert_post(post(1))
    notify(bot)
    saved = radar.subscription("group")
    assert saved.last_notified_post_id == post(1).post_id
    assert saved.last_delivery_mode == "text"
    sent = str(bot.protocol.send_message.await_args.args[1])
    assert "中文样例" in sent and post(1).url in sent and "图片暂不可用" in sent
    count = bot.protocol.send_message.await_count
    notify(bot)
    assert bot.protocol.send_message.await_count == count


def test_no_receipt_keeps_cursor_and_does_not_bypass_send_hook(radar):
    bot = account()
    bot.protocol.send_message.return_value = []
    radar.upsert_post(post(1))
    notify(bot)
    assert radar.subscription("group").last_notified_post_id == post(0).post_id
    assert radar.subscription("group").last_delivery_error == "DeliveryNotAcknowledged"
    assert bot.protocol.send_message.await_count == 1


def test_text_without_receipt_also_keeps_cursor(radar):
    bot = account()
    bot.protocol.send_message.side_effect = [RuntimeError("image failed"), []]
    radar.upsert_post(post(1))
    notify(bot)
    assert radar.subscription("group").last_notified_post_id == post(0).post_id
    assert radar.subscription("group").delivery_failures == 1


def test_all_sends_fail_and_backoff_survives_restart(radar, monkeypatch):
    bot = account()
    bot.protocol.send_message.side_effect = RuntimeError("offline")
    radar.upsert_post(post(1))
    notify(bot)
    saved = radar.subscription("group")
    assert saved.last_notified_post_id == post(0).post_id
    assert saved.delivery_failures == 1 and saved.retry_after is not None
    assert bot.protocol.send_message.await_count == 2
    reopened = TiboStore(radar.db_path)
    monkeypatch.setattr(handlers, "store", reopened)
    try:
        notify(bot)
        assert bot.protocol.send_message.await_count == 2
        assert reopened.subscription("group").delivery_failures == 1
    finally:
        reopened.close()


def test_retry_recovers_and_clears_failure_state(radar):
    radar.upsert_post(post(1))
    radar.mark_subscription_failed("group", "TimeoutError", now=START)
    notify(account())
    saved = radar.subscription("group")
    assert saved.last_notified_post_id == post(1).post_id
    assert saved.delivery_failures == 0 and saved.retry_after is None
    assert saved.last_delivery_error == ""


def test_backlog_is_bounded_and_unsent_posts_are_not_discarded(radar):
    bot = account()
    for index in range(1, 21):
        radar.upsert_post(post(index))
    notify(bot)
    assert bot.protocol.send_message.await_count == 1
    assert radar.subscription("group").last_notified_post_id == post(3).post_id
    for _ in range(6):
        notify(bot)
    assert bot.protocol.send_message.await_count == 7
    assert radar.subscription("group").last_notified_post_id == post(20).post_id


def test_successful_cursor_survives_restart(radar, monkeypatch):
    bot = account()
    radar.upsert_post(post(1))
    notify(bot)
    reopened = TiboStore(radar.db_path)
    monkeypatch.setattr(handlers, "store", reopened)
    try:
        notify(bot)
        assert bot.protocol.send_message.await_count == 1
    finally:
        reopened.close()


def test_partial_batch_keeps_acknowledged_progress(radar, monkeypatch):
    monkeypatch.setattr(handlers, "SUBSCRIPTION_MAX_PAGES", 2)
    bot = account()
    bot.protocol.send_message.side_effect = [[SimpleNamespace(id="first")], RuntimeError("image"), RuntimeError("text")]
    for index in range(1, 7):
        radar.upsert_post(post(index))
    notify(bot)
    assert radar.subscription("group").last_notified_post_id == post(3).post_id
    assert radar.subscription("group").delivery_failures == 1


def test_overlapping_notifications_send_once(radar):
    bot = account()
    radar.upsert_post(post(1))

    async def run():
        await asyncio.gather(handlers._notify_subscriptions(bot), handlers._notify_subscriptions(bot))

    asyncio.run(run())
    assert bot.protocol.send_message.await_count == 1


@pytest.mark.parametrize("failure", [False, RuntimeError("collection failed")])
def test_pending_delivery_survives_source_outage(radar, monkeypatch, failure):
    bot = account()
    radar.upsert_post(post(1))
    mock = AsyncMock(side_effect=failure) if isinstance(failure, Exception) else AsyncMock(return_value=False)
    monkeypatch.setattr(handlers.service, "refresh", mock)
    monkeypatch.setattr(handlers, "get_bot", lambda: bot)
    assert asyncio.run(handlers._refresh_and_notify()) is False
    assert radar.subscription("group").last_notified_post_id == post(1).post_id


def test_source_outage_does_not_initialize_empty_subscription(tmp_path, monkeypatch):
    store = TiboStore(tmp_path / "empty.db")
    store.subscribe("empty")
    monkeypatch.setattr(handlers, "store", store)
    try:
        asyncio.run(handlers._notify_subscriptions(account(), initialize_baselines=False))
        assert store.subscription("empty").baseline_pending is True
    finally:
        store.close()


def test_real_renderer_and_entari_protocol_across_three_cycles(radar, monkeypatch):
    from arclet.entari.session import EntariProtocol
    from plugins.tibo_radar.draw_xfeed import render_xfeed

    bot = account()
    sent = []

    async def api(action, params, **kwargs):
        if kwargs.get("multipart"):
            payload = next(iter(params.values()))
            assert payload["value"].startswith(b"\x89PNG\r\n\x1a\n")
            return {"file": f"internal:tibo/{len(sent)}.png"}
        assert 'src="internal:tibo/' in params["content"]
        sent.append(params["content"])
        return [{"id": str(len(sent)), "content": params["content"]}]

    monkeypatch.setattr(handlers, "render_xfeed", render_xfeed)

    async def run():
        from arclet.letoderea import core as events

        # Entari binds its event bus at import time; this isolated test owns a
        # fresh asyncio.run loop instead of the application's normal loop.
        monkeypatch.setattr(events._EventSystem, "loop", asyncio.get_running_loop())
        bot.protocol = EntariProtocol(bot)
        bot.protocol.call_api = api
        try:
            for index in range(1, 4):
                radar.upsert_post(post(index))
                await handlers._notify_subscriptions(bot)
                assert radar.subscription("group").last_notified_post_id == post(index).post_id
        finally:
            await bot.protocol.session.close()

    asyncio.run(run())
    assert len(sent) == 3


def test_cycle_lock_covers_startup_and_scheduler(radar, monkeypatch):
    bot = account()
    radar.upsert_post(post(1))

    async def refresh():
        await asyncio.sleep(0)
        return True

    mock = AsyncMock(side_effect=refresh)
    monkeypatch.setattr(handlers.service, "refresh", mock)
    monkeypatch.setattr(handlers, "get_bot", lambda: bot)

    async def run():
        await asyncio.gather(handlers._refresh_and_notify(bot), handlers._refresh_and_notify())

    asyncio.run(run())
    assert mock.await_count == 1
    assert bot.protocol.send_message.await_count == 1


def test_account_is_resolved_after_collection(radar, monkeypatch):
    old_bot, new_bot = account(), account()
    active = [old_bot]

    async def refresh():
        active[0] = new_bot
        return True

    monkeypatch.setattr(handlers.service, "refresh", refresh)
    monkeypatch.setattr(handlers, "get_bot", lambda: active[0])
    radar.upsert_post(post(1))
    asyncio.run(handlers._refresh_and_notify(old_bot))
    assert old_bot.protocol.send_message.await_count == 0
    assert new_bot.protocol.send_message.await_count == 1


def test_failed_group_does_not_block_other_groups(radar):
    radar.subscribe("other", "other-channel")
    radar.upsert_post(post(1))
    bot = account()

    async def send(channel, message):
        if channel == "channel":
            raise RuntimeError("group unavailable")
        return [SimpleNamespace(id="ok")]

    bot.protocol.send_message.side_effect = send
    notify(bot)
    assert radar.subscription("group").delivery_failures == 1
    assert radar.subscription("other").last_notified_post_id == post(1).post_id


def test_cancellation_never_falls_back_or_advances(radar, monkeypatch):
    radar.upsert_post(post(1))
    bot = account()
    monkeypatch.setattr(handlers, "render_xfeed", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        notify(bot)
    assert bot.protocol.send_message.await_count == 0
    assert radar.subscription("group").last_notified_post_id == post(0).post_id
    assert radar.subscription("group").delivery_failures == 0


def test_render_timeout_falls_back_to_text():
    bot = account()

    async def render():
        await asyncio.sleep(1)
        return PNG

    result = asyncio.run(deliver_page(bot, SendDest("channel"), [post(1)], render, [], str, timeout=0.01))
    assert result == "text"


def test_fallback_text_is_bounded_and_keeps_each_source():
    posts = [post(index) for index in range(3)]
    for item in posts:
        item.text = item.translation = item.analysis = "长文本" * 2000
    text = fallback_text(posts, str)
    assert len(text) < 3000
    assert all(item.url in text for item in posts)


def test_cursor_cannot_move_backwards(radar):
    radar.mark_subscription_delivered("group", (START + timedelta(seconds=3)).isoformat(), post(3).post_id)
    radar.mark_subscription_delivered("group", (START + timedelta(seconds=1)).isoformat(), post(1).post_id)
    assert radar.subscription("group").last_notified_post_id == post(3).post_id


def test_schema_migration_preserves_subscription_cursor(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE tibo_subscriptions (group_id TEXT PRIMARY KEY,channel_id TEXT NOT NULL,enabled INTEGER NOT NULL,last_notified_at TEXT NOT NULL,last_notified_post_id TEXT NOT NULL,subscribed_at TEXT NOT NULL,updated_at TEXT NOT NULL)")
        connection.execute("INSERT INTO tibo_subscriptions VALUES ('g','c',1,?,'00001',?,'')", (START.isoformat(), START.isoformat()))
    store = TiboStore(path)
    try:
        saved = store.subscription("g")
        assert saved.last_notified_post_id == "00001"
        assert saved.last_notified_at == START
        assert saved.delivery_failures == 0 and saved.retry_after is None
    finally:
        store.close()
