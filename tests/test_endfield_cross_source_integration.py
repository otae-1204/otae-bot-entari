"""Run with both feature branches present; never connect to a real account."""

from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests import test_endfield_announcement_dedup as cases
from tests.test_endfield_announcements import (
    NOW,
    PNG,
    destination,
    fake_bot,
    runtime_module,
)

pytestmark = pytest.mark.skipif(
    not (
        Path(__file__).resolve().parents[1] / "plugins/bilibilibot/official_delivery.py"
    ).exists(),
    reason="Cross-source integration requires both feature branches",
)
prepared = cases.prepared


@pytest.mark.parametrize("order", ["website", "bilibili", "concurrent"])
def test_production_senders_share_receipts_in_either_order(
    prepared, monkeypatch, order
):
    async def run():
        handlers = importlib.import_module("plugins.bilibilibot.handlers")
        bili_models = importlib.import_module("plugins.bilibilibot.models")
        official = importlib.import_module("plugins.bilibilibot.official_delivery")
        sender_unavailable = importlib.import_module(
            "plugins.bilibilibot.notifier"
        ).SenderUnavailable
        delivery_deferred = importlib.import_module(
            runtime_module.__package__ + ".service"
        ).DeliveryDeferred
        item = cases.article()
        prepared.apply_snapshot([item], NOW + 1)
        runtime = runtime_module.AnnouncementRuntime(
            prepared, clock=lambda: NOW + 1, renderer=AsyncMock(return_value=PNG)
        )
        bot = fake_bot()
        runtime.remember(bot)
        monkeypatch.setattr(handlers, "get_bot", lambda: bot)
        monkeypatch.setattr(handlers, "account_adapter_name", lambda _: "test")
        monkeypatch.setattr(
            handlers,
            "official_delivery",
            official.OfficialDelivery(runtime.receipts, clock=lambda: NOW + 1),
        )
        card = bili_models.BiliCard(
            "dynamic",
            item.title,
            description=item.content_text,
            uid="1265652806",
            published_at=item.published_at,
        )
        row = bili_models.OutboxRow(
            1,
            "example",
            "dynamic",
            card.uid,
            "dynamic",
            "group",
            "100",
            json.dumps(asdict(card)),
            NOW + 1,
        )
        jobs = prepared.due(NOW + 1)

        async def website():
            await runtime.send(destination(), jobs)

        async def bilibili():
            await handlers._send(row, PNG)

        if order == "concurrent":
            results = await asyncio.gather(
                website(), bilibili(), return_exceptions=True
            )
            assert all(
                result is None
                or isinstance(result, (sender_unavailable, delivery_deferred))
                for result in results
            )
        elif order == "website":
            await website()
            await bilibili()
        else:
            await bilibili()
            await website()
        # The losing source retries after the winner's ACK, including a restart.
        monkeypatch.setattr(
            handlers,
            "official_delivery",
            official.OfficialDelivery(runtime.receipts, clock=lambda: NOW + 2),
        )
        await website()
        await bilibili()
        bot.protocol.send_message.assert_awaited_once()
        await runtime.close()

    asyncio.run(run())
