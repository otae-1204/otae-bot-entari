from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import replace
from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from otae_bot.endfield_notifications.classification import classify
from otae_bot.endfield_notifications.receipts import ReceiptStore, equivalent
from tests.test_endfield_announcements import (
    NOW,
    PACKAGE,
    PNG,
    destination,
    fake_bot,
    models,
    payload,
    runtime_module,
    source_module,
    store_module,
)

rules = importlib.import_module(f"{PACKAGE}.announcements.notification_rules")


def article(name="甲", *, cid="1", published=NOW + 1):
    body = f"「{name}」限时活动说明\n活动时间\n2030/01/01 08:01 - 2030/01/03 08:00\n完成任务领取奖励。"
    data = payload(
        "".join(f"<p>{escape(line)}</p>" for line in body.splitlines()),
        title=f"「{name}」限时活动说明",
        cid=cid,
    )
    data["data"]["displayTime"] = published
    return source_module.parse_article(data)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    store = store_module.AnnouncementStore(tmp_path / "announcements.db")
    store.subscribe(
        models.Subscription(
            destination(),
            start_minutes=0,
            end_minutes=0,
            maintenance_minutes=0,
            created_at=NOW,
        )
    )
    store.apply_snapshot([], NOW)
    monkeypatch.setattr(runtime_module.feature_store, "is_enabled", lambda *_: True)
    return store


def acknowledge_bili(receipts, item, *, phase=None, source_at=None):
    event = classify(item.title, item.content_text, item.published_at).units[0].event
    if phase:
        event = replace(event, phase=phase)
    if source_at is not None:
        event = replace(event, source_at=source_at)
    claim = receipts.reserve(destination().key, "bilibili", [event], NOW + 1)
    receipts.finish(claim, NOW + 1)
    return event


def test_bili_first_filters_one_card_but_finishes_whole_website_batch(prepared):
    async def run():
        first, second = article(), article("乙", cid="2")
        render = AsyncMock(return_value=PNG)
        runtime = runtime_module.AnnouncementRuntime(
            prepared,
            SimpleNamespace(fetch=AsyncMock(return_value=[first, second])),
            clock=lambda: NOW + 1,
            renderer=render,
        )
        acknowledge_bili(runtime.receipts, first)
        bot = fake_bot()
        runtime.remember(bot)
        await runtime.service.tick()
        bulletin = render.await_args.args[0]
        assert len(bulletin.cards) == 1 and "乙" in bulletin.cards[0].title
        assert not prepared.due(NOW + 1, None)
        bot.protocol.send_message.assert_awaited_once()
        # A website ACK also suppresses a later Bilibili notification.
        event = (
            classify(second.title, second.content_text, second.published_at)
            .units[0]
            .event
        )
        assert runtime.receipts.reserve(
            destination().key, "bilibili", [event], NOW + 2
        ).duplicates == (0,)
        await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("delta,send_count", [(86399, 0), (86400, 1), (-86400, 1)])
def test_website_keeps_publications_at_least_one_day_apart(prepared, delta, send_count):
    async def run():
        item = article()
        prepared.apply_snapshot([item], NOW + 1)
        renderer = AsyncMock(return_value=PNG)
        runtime = runtime_module.AnnouncementRuntime(
            prepared, clock=lambda: NOW + 1, renderer=renderer
        )
        acknowledge_bili(runtime.receipts, item, source_at=item.published_at + delta)
        bot = fake_bot()
        runtime.remember(bot)
        await runtime.send(destination(), prepared.due(NOW + 1))
        assert bot.protocol.send_message.await_count == send_count
        assert renderer.await_count == send_count
        await runtime.close()

    asyncio.run(run())


def test_all_duplicates_finish_without_render_or_adapter_send(prepared):
    async def run():
        item = article()
        render = AsyncMock(return_value=PNG)
        runtime = runtime_module.AnnouncementRuntime(
            prepared,
            SimpleNamespace(fetch=AsyncMock(return_value=[item])),
            clock=lambda: NOW + 1,
            renderer=render,
        )
        acknowledge_bili(runtime.receipts, item)
        bot = fake_bot()
        runtime.remember(bot)
        await runtime.service.tick()
        assert not prepared.due(NOW + 1)
        render.assert_not_awaited()
        bot.protocol.send_message.assert_not_awaited()
        await runtime.close()

    asyncio.run(run())


def test_sent_announcement_does_not_hide_actual_start(prepared):
    async def run():
        item = article()
        prepared.apply_snapshot([item], NOW + 1)
        prepared.sent_many(prepared.due(NOW + 1))
        prepared.plan(NOW + 60)
        jobs = prepared.due(NOW + 60)
        assert {job.phase for job in jobs} == {"started"}
        runtime = runtime_module.AnnouncementRuntime(
            prepared, clock=lambda: NOW + 60, renderer=AsyncMock(return_value=PNG)
        )
        bot = fake_bot()
        runtime.remember(bot)
        acknowledge_bili(runtime.receipts, item)
        await runtime.send(destination(), jobs)
        bot.protocol.send_message.assert_awaited_once()
        started = (
            classify(item.title.replace("说明", "已开启"), item.content_text, NOW + 60)
            .units[0]
            .event
        )
        assert started.phase == "started"
        assert runtime.receipts.reserve(
            destination().key, "bilibili", [started], NOW + 61
        ).duplicates == (0,)
        await runtime.close()

    asyncio.run(run())


def test_remove_only_duplicate_phase_in_same_card(prepared):
    async def run():
        current = prepared.subscription(destination().key)
        prepared.subscribe(replace(current, start_minutes=60))
        item = article()
        prepared.apply_snapshot([item], NOW + 1)
        prepared.plan(NOW + 1)
        renderer = AsyncMock(return_value=PNG)
        runtime = runtime_module.AnnouncementRuntime(
            prepared, clock=lambda: NOW + 1, renderer=renderer
        )
        runtime.remember(fake_bot())
        acknowledge_bili(runtime.receipts, item)
        await runtime.send(destination(), prepared.due(NOW + 1))
        cards = renderer.await_args.args[0].cards
        assert len(cards) == 1 and cards[0].phases == ("start",)
        await runtime.close()

    asyncio.run(run())


def test_busy_does_not_consume_retry_and_failed_ack_releases_claim(prepared):
    async def run():
        item = article()
        prepared.apply_snapshot([item], NOW + 1)
        runtime = runtime_module.AnnouncementRuntime(
            prepared,
            SimpleNamespace(fetch=AsyncMock(return_value=[item])),
            clock=lambda: NOW + 1,
            renderer=AsyncMock(return_value=PNG),
        )
        bot = fake_bot()
        runtime.remember(bot)
        event = (
            classify(item.title, item.content_text, item.published_at).units[0].event
        )
        peer = runtime.receipts.reserve(destination().key, "bilibili", [event], NOW + 1)
        await runtime.service.tick()
        jobs = [job for job in prepared.due(NOW + 61) if job.phase == "news"]
        assert len(jobs) == 1 and jobs[0].attempts == 0
        runtime.receipts.release(peer)
        runtime.clock = lambda: NOW + 61
        bot.protocol.send_message.return_value = []
        with pytest.raises(RuntimeError, match="acknowledged"):
            await runtime.send(destination(), jobs)
        retry = runtime.receipts.reserve(
            destination().key, "bilibili", [event], NOW + 61
        )
        assert retry.accepted == (0,) and not retry.busy
        runtime.receipts.release(retry)
        bot.protocol.send_message.return_value = [object()]
        await runtime.send(destination(), jobs)
        assert ReceiptStore(runtime.receipts.path).reserve(
            destination().key, "bilibili", [event], NOW + 62
        ).duplicates == (0,)
        await runtime.close()

    asyncio.run(run())


def test_version_overview_does_not_share_child_identity_and_updates_keep_changes():
    item = article()
    overview = replace(item, title="「雪凇幽梦」版本更新说明", category="version")
    child = rules.event_for(overview, overview.windows[0], "started")
    document = rules.event_for(overview, overview.windows[0], "news")
    assert document.kind == "document:version" and child.kind == "activity"
    assert not equivalent(document, child)
    update = rules.event_for(item, item.windows[0], "updated")
    changed = replace(item, content_text=item.content_text + "\n追加奖励：嵌晶玉。")
    assert not equivalent(
        update, rules.event_for(changed, changed.windows[0], "updated")
    )
    assert equivalent(update, rules.event_for(item, item.windows[0], "updated"))


def test_new_category_fields_survive_persistence_and_old_payloads_load():
    item = article()
    assert item.category == "activity" and item.content_text
    assert models.Announcement.loads(item.dumps()) == item
    raw = json.loads(item.dumps())
    for name in ("category", "tags", "content_text"):
        raw.pop(name)
    old = models.Announcement.loads(json.dumps(raw))
    assert old.category == "" and old.tags == () and old.content_text == ""
    # Adding classification alone must not be treated as a source revision.
    assert old.fingerprint == item.fingerprint
