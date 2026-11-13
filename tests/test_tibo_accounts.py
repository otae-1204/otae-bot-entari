"""A Tibo subscription not yet bound to an account goes out through an account in the group.

The cycle's account comes from get_bot(), the most recent login; with the Ir
panel (native Satori, not QQ) online that is Ir, which must never send to a
QQ group, nor become the subscription's bound account. Real Satori accounts
with the real EntariProtocol; only HTTP is faked.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from otae_bot.accounts import runtime
from plugins.tibo_radar import handlers
from plugins.tibo_radar.models import TiboPost
from plugins.tibo_radar.store import TiboStore
from tests.support.satori_accounts import make_account, pin_event_loop

GROUP = "977295722"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def post(index: int) -> TiboPost:
    return TiboPost(
        str(index).zfill(5), f"Public sample {index}", f"https://example.invalid/{index}",
        source_time=START + timedelta(seconds=index), first_seen_at=START + timedelta(seconds=index),
        translation="中文样例", analysis="仅为测试", relevance="direct",
    )


@pytest.fixture
def radar(tmp_path, monkeypatch):
    runtime.clear_account()
    store = TiboStore(tmp_path / "radar.db")
    store.upsert_post(post(0))
    store.subscribe(GROUP, GROUP)  # Legacy row: no bound account.
    store.upsert_post(post(1))
    monkeypatch.setattr(handlers, "store", store)
    monkeypatch.setattr(handlers, "render_xfeed", AsyncMock(return_value=b"\x89PNG\r\n\x1a\nfixture"))
    monkeypatch.setattr(handlers, "_notification_lock", asyncio.Lock())
    monkeypatch.setattr(handlers, "_delivery_accounts", {})
    yield store
    store.close()
    runtime.clear_account()


def run(scenario):
    async def pinned():
        with pin_event_loop():
            await scenario()

    asyncio.run(pinned())


async def online(*bots):
    for bot in bots:
        runtime.set_account(bot)
    await runtime.refresh_guilds()


def ir_panel():
    return make_account("ir-7f3a", platform="ir_web", guilds=["folder-root"])


def test_an_unbound_subscription_is_sent_by_an_account_in_the_group_not_the_latest_login(radar):
    async def scenario():
        qq, ir = make_account("3898438488", guilds=[GROUP]), ir_panel()
        await online(qq, ir)
        await handlers._notify_subscriptions(ir)  # The cycle's account is the latest login, Ir.
        assert ir.protocol.sent == []
        assert [channel for channel, _ in qq.protocol.sent] == [GROUP]
        assert radar.subscription(GROUP).account_key == "llonebot:3898438488"

    run(scenario)


def test_an_unbound_subscription_waits_when_no_account_is_in_the_group(radar):
    async def scenario():
        ir = ir_panel()
        await online(make_account("1194397508", guilds=["100"]), ir)
        await handlers._notify_subscriptions(ir)
        assert ir.protocol.sent == []
        subscription = radar.subscription(GROUP)
        assert subscription.account_key == "" and subscription.last_notified_post_id == post(0).post_id

    run(scenario)


def test_the_cycle_account_is_kept_when_it_is_in_the_group(radar):
    async def scenario():
        first, second = make_account("1194397508", guilds=[GROUP]), make_account("3898438488", guilds=[GROUP])
        await online(first, second)
        await handlers._notify_subscriptions(first)
        assert len(first.protocol.sent) == 1 and second.protocol.sent == []

    run(scenario)
