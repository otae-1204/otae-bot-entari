from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_core_logic import _load_bili_new_module


@pytest.fixture
def notifications(monkeypatch):
    module = _load_bili_new_module("service")
    models = sys.modules[module.__package__ + ".models"]
    bot = object()
    monkeypatch.setattr(module, "get_bot", lambda: bot)
    monkeypatch.setattr(module, "account_adapter_name", lambda _: "test")
    subscriptions = [models.Subscription("live", "123", "group", "900")]
    store = SimpleNamespace(subscriptions_for_target=lambda *_: subscriptions)
    service = module.BiliService(store, SimpleNamespace())
    image = module.make_image(url="https://example.test/cover.png")
    service.card_to_segment = AsyncMock(return_value=image)
    return SimpleNamespace(
        module=module,
        models=models,
        bot=bot,
        subscriptions=subscriptions,
        service=service,
        image=image,
    )


@pytest.mark.parametrize("recipient_type", ["group", "user"])
def test_start_sends_separate_image_then_plain_link(
    notifications, monkeypatch, recipient_type
):
    n = notifications
    n.subscriptions[0].subscriber_type = recipient_type
    sent = []

    async def send(message, destination, bot):
        assert bot is n.bot
        sent.append((destination.id, destination.private, str(message)))

    monkeypatch.setattr(n.module.ChainMsg, "send", send)
    card = n.models.BiliCard(
        "live_on", "已开播", url="https://live.bilibili.com/25731103"
    )
    asyncio.run(n.service.broadcast("live", "123", card))
    private = recipient_type == "user"
    assert sent == [("900", private, str(n.image)), ("900", private, card.url)]
    n.service.card_to_segment.assert_awaited_once_with(card)


@pytest.mark.parametrize(
    "kind,url",
    [
        ("live_off", "https://live.bilibili.com/123"),
        ("live_idle", "https://live.bilibili.com/123"),
        ("video", "https://www.bilibili.com/video/BV1extE6LEKB"),
        ("dynamic", "https://t.bilibili.com/123"),
        ("live_on", ""),
        ("live_on", "   "),
    ],
)
def test_non_start_notifications_and_empty_links_send_only_image(
    notifications, monkeypatch, kind, url
):
    n = notifications
    sent = []

    async def send(message, *_):
        sent.append(str(message))

    monkeypatch.setattr(n.module.ChainMsg, "send", send)
    asyncio.run(
        n.service.broadcast("live", "123", n.models.BiliCard(kind, "标题", url=url))
    )
    assert sent == [str(n.image)]


def test_concurrent_starts_do_not_interleave_image_link_pairs(
    notifications, monkeypatch
):
    n = notifications
    sent = []
    n.service.card_to_segment = AsyncMock(
        side_effect=lambda card: n.module.make_image(
            url=f"https://example.test/{card.uid}.png"
        )
    )

    async def send(message, *_):
        sent.append(str(message))
        await asyncio.sleep(0)

    monkeypatch.setattr(n.module.ChainMsg, "send", send)

    async def run():
        await asyncio.gather(
            *(
                n.service.broadcast(
                    "live",
                    str(index),
                    n.models.BiliCard(
                        "live_on",
                        "已开播",
                        uid=str(index),
                        url=f"https://live.bilibili.com/{index}",
                    ),
                )
                for index in range(3)
            )
        )

    asyncio.run(run())
    assert len(sent) == 6
    for index in range(3):
        image = str(n.module.make_image(url=f"https://example.test/{index}.png"))
        position = sent.index(image)
        assert position % 2 == 0
        assert sent[position + 1] == f"https://live.bilibili.com/{index}"


@pytest.mark.parametrize("failure", ["image", "link"])
def test_one_recipient_failure_does_not_block_next_recipient(
    notifications, monkeypatch, failure
):
    n = notifications
    n.subscriptions.append(n.models.Subscription("live", "123", "group", "901"))
    sent = []
    url = "https://live.bilibili.com/25731103"

    async def send(message, destination, _):
        text = str(message)
        if destination.id == "900" and text == (
            url if failure == "link" else str(n.image)
        ):
            raise RuntimeError("send failed")
        sent.append((destination.id, text))

    monkeypatch.setattr(n.module.ChainMsg, "send", send)
    asyncio.run(
        n.service.broadcast(
            "live", "123", n.models.BiliCard("live_on", "已开播", url=url)
        )
    )
    assert [item for item in sent if item[0] == "901"] == [
        ("901", str(n.image)),
        ("901", url),
    ]
