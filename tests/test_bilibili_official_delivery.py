from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from otae_bot.endfield_notifications.classification import classify
from otae_bot.endfield_notifications.receipts import ReceiptStore, destination_key
from plugins.bilibilibot import handlers
from plugins.bilibilibot.api.mapping import dynamic_item_to_card
from plugins.bilibilibot.models import BiliCard, OutboxRow
from plugins.bilibilibot.notifier import SenderUnavailable
from plugins.bilibilibot.official_delivery import OfficialDelivery
from tests.test_bilibili_endfield_dynamics import sample

NOW = 1790751600
DEST = destination_key("qq", "bot", "group")
BODY = "▼「甲」活动\n活动时间\n2026/10/01 12:00 - 2026/10/15 12:00\n玩法甲\n▼「乙」签到活动\n活动时间\n2026/10/01 12:00 - 2026/10/16 12:00\n玩法乙"


def card():
    return BiliCard(
        "dynamic",
        "活动一览",
        description=BODY,
        uid="1265652806",
        published_at=NOW,
        cover_url="https://example.com/both.jpg",
    )


def website_receipt(store, event):
    claim = store.reserve(DEST, "website", [event], NOW)
    store.finish(claim, NOW)


def test_partial_image_gets_only_unseen_units_and_no_composite_cover(tmp_path):
    async def run():
        receipts = ReceiptStore(tmp_path / "receipts.db")
        website_receipt(receipts, classify(card().title, BODY, NOW).units[0].event)
        send = AsyncMock()
        await OfficialDelivery(receipts, clock=lambda: NOW).deliver(card(), DEST, send)
        selected = send.await_args.args[0]
        assert "乙" in selected.description and "甲" not in selected.description
        assert "乙" in selected.title and not selected.cover_url
        # A fresh instance sees the committed receipt after restart.
        send.reset_mock()
        await OfficialDelivery(ReceiptStore(receipts.path), clock=lambda: NOW).deliver(
            card(), DEST, send
        )
        send.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("delta,sent", [(86399, False), (86400, True), (-86400, True)])
def test_bili_keeps_publications_a_day_apart(tmp_path, delta, sent):
    async def run():
        receipts = ReceiptStore(tmp_path / "receipts.db")
        for unit in classify(card().title, BODY, NOW).units:
            website_receipt(receipts, unit.event)
        send = AsyncMock()
        await OfficialDelivery(receipts, clock=lambda: NOW + 100000).deliver(
            replace(card(), published_at=NOW + delta), DEST, send
        )
        assert bool(send.await_count) is sent

    asyncio.run(run())


def test_busy_failure_and_cancellation_never_suppress_retry(tmp_path):
    async def run():
        receipts = ReceiptStore(tmp_path / "receipts.db")
        delivery = OfficialDelivery(receipts, clock=lambda: NOW)
        peer = receipts.reserve(
            DEST, "website", [classify(card().title, BODY, NOW).units[0].event], NOW
        )
        send = AsyncMock()
        with pytest.raises(SenderUnavailable):
            await delivery.deliver(card(), DEST, send)
        send.assert_not_awaited()
        receipts.release(peer)
        for failure in (RuntimeError("no ACK"), asyncio.CancelledError()):
            with pytest.raises(type(failure)):
                await delivery.deliver(card(), DEST, AsyncMock(side_effect=failure))
        await delivery.deliver(card(), DEST, send)
        send.assert_awaited_once()
        await delivery.deliver(
            card(), destination_key("qq", "other-bot", "group"), send
        )
        assert send.await_count == 2

    asyncio.run(run())


def test_handler_rerenders_filtered_content_and_requires_adapter_ack(
    tmp_path, monkeypatch
):
    async def run():
        receipts = ReceiptStore(tmp_path / "receipts.db")
        website_receipt(receipts, classify(card().title, BODY, NOW).units[0].event)
        monkeypatch.setattr(
            handlers, "official_delivery", OfficialDelivery(receipts, clock=lambda: NOW)
        )
        monkeypatch.setattr(
            handlers, "get_bot", lambda: SimpleNamespace(platform="qq", self_id="bot")
        )
        monkeypatch.setattr(handlers, "account_adapter_name", lambda _: "test")
        monkeypatch.setattr(handlers, "make_image", lambda *, raw: raw.hex())
        render = AsyncMock(return_value=b"filtered-image")
        monkeypatch.setattr(handlers, "_render", render)
        send = AsyncMock(return_value=[])
        monkeypatch.setattr(handlers.ChainMsg, "send", send)
        row = OutboxRow(
            1,
            "event",
            "dynamic",
            card().uid,
            "dynamic",
            "group",
            "group",
            json.dumps(asdict(card())),
            NOW,
        )
        with pytest.raises(RuntimeError, match="acknowledged"):
            await handlers._send(row, b"full-image")
        assert "甲" not in render.await_args.args[0].description
        send.return_value = ["message-id"]
        await handlers._send(row, b"full-image")
        await handlers._send(row, b"full-image")
        assert send.await_count == 2
        assert render.await_count == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    "name,category",
    [
        ("livestream", "preview"),
        ("activity", "activity"),
        ("lottery_start", "other"),
        ("lottery_result", "lottery_result"),
    ],
)
def test_persisted_categories_do_not_replace_lottery_tags(name, category):
    result = dynamic_item_to_card(sample(name), "1265652806")
    assert result.content_category == category
    assert ("lottery" in result.content_tags) == bool(result.lottery_reason)
    restored = BiliCard(**json.loads(json.dumps(asdict(result))))
    assert restored.content_category == category
    assert not BiliCard("dynamic", "old queue").content_tags
