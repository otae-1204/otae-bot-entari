"""Notification delivery tests: the outbox consumer and the production sender.

Phase 4 moved delivery out of `service.broadcast`: the poller writes one outbox
row per recipient, `notifier.Notifier` drains that table, and `handlers._send`
performs the actual send. The assertions of the old `service.broadcast` tests are
kept, now split along that seam:

* the notifier decides who is delivered, in which order, and that one failing
  recipient cannot block the others;
* `handlers._send` decides what a recipient receives: the image, followed by the
  plain-text room link for `live_on` (refactor plan, section 4.1 item 4).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from dataclasses import asdict
from types import SimpleNamespace
from typing import Awaitable, Callable

import pytest
from PIL import Image

from plugins.bilibilibot import handlers
from plugins.bilibilibot.models import BiliCard, BiliEvent, KIND_LIVE, OutboxRow
from plugins.bilibilibot.notifier import Notifier, SenderUnavailable
from plugins.bilibilibot.store import BiliStore


NOW = 1000
LIVE_URL = "https://live.bilibili.com/25731103"


class FakeCards:
    """Deterministic renderer: one identifiable PNG per (uid, card_type)."""

    def __init__(self) -> None:
        self.renders: list[str] = []

    async def render(self, card: BiliCard) -> bytes:
        self.renders.append(self.key(card))
        return self.png(card)

    @staticmethod
    def key(card: BiliCard) -> str:
        return f"{card.uid}:{card.card_type}"

    @staticmethod
    def png(card: BiliCard) -> bytes:
        digest = hashlib.sha256(FakeCards.key(card).encode()).digest()
        buffer = io.BytesIO()
        Image.new("RGB", (1, 1), tuple(digest[:3])).save(buffer, format="PNG")
        return buffer.getvalue()

    def image(self, card: BiliCard) -> str:
        """The exact message text the adapter receives for this card."""
        return str(handlers.make_image(raw=self.png(card)))


@pytest.fixture
def cards() -> FakeCards:
    return FakeCards()


@pytest.fixture
def transport(monkeypatch) -> SimpleNamespace:
    """Stand-in for the adapter transport and the bot lookup.

    Every message the production sender hands to the adapter is recorded as
    (recipient id, channel, private, text); the `fail` hook decides which of
    them the transport rejects.
    """
    state = SimpleNamespace(messages=[], bot=object(), fail=None)

    async def send(chain, dest=None, bot=None) -> None:
        text = str(chain)
        if state.fail is not None and state.fail(dest, text):
            raise RuntimeError("send failed")
        state.messages.append((dest.id, dest.channel, dest.private, text))
        # Yield so a wrongly concurrent implementation would interleave.
        await asyncio.sleep(0)

    monkeypatch.setattr(handlers.ChainMsg, "send", send)
    monkeypatch.setattr(handlers, "get_bot", lambda: state.bot)
    return state


async def _wait_until(
    predicate: Callable[[], Awaitable[bool]], *, timeout: float = 10.0
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition was not met in time")


async def _open_store(tmp_path) -> BiliStore:
    store = BiliStore(tmp_path / "bilibili.db", tmp_path / "missing.db")
    await store.open()
    return store


async def _commit(store, card, recipients, *, event_key: str, now: int = NOW) -> int:
    """One poll round: the event becomes one outbox row per recipient."""
    event = BiliEvent(KIND_LIVE, card.uid, card, event_key=event_key)
    return await store.apply_poll_result(
        [],
        [],
        [event],
        now,
        expand=lambda _event: list(recipients),
        event_key=lambda item: item.event_key,
    )


async def _drained(store) -> bool:
    return await store.outbox_count() == 0


async def _deliver(
    store, cards, *, done, clock=lambda: NOW, concurrency: int = 2
) -> None:
    """Run the real notifier with the real sender until `done` holds."""
    notifier = Notifier(
        store,
        render=cards.render,
        send=handlers._send,
        clock=clock,
        concurrency=concurrency,
    )
    await notifier.start()
    try:
        await _wait_until(done)
    finally:
        await notifier.stop()


# --- image plus link for a live start ---------------------------------------


@pytest.mark.parametrize("subscriber", [("group", "900"), ("user", "900")])
def test_start_sends_separate_image_then_plain_link(
    transport, cards, tmp_path, subscriber
):
    async def scenario() -> None:
        store = await _open_store(tmp_path)
        try:
            card = BiliCard("live_on", "已开播", uid="123", url=LIVE_URL)
            assert (
                await _commit(store, card, [subscriber], event_key="live:123:live_on:1")
                == 1
            )
            await _deliver(store, cards, done=lambda: _drained(store))

            # Group chats are addressed as a channel, private chats as a user.
            channel = subscriber[0] == "group"
            assert transport.messages == [
                ("900", channel, not channel, cards.image(card)),
                ("900", channel, not channel, card.url),
            ]
        finally:
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "card_type,url",
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
    transport, cards, tmp_path, card_type, url
):
    async def scenario() -> None:
        store = await _open_store(tmp_path)
        try:
            card = BiliCard(card_type, "标题", uid="123", url=url)
            await _commit(
                store, card, [("group", "900")], event_key=f"live:123:{card_type}:1"
            )
            await _deliver(store, cards, done=lambda: _drained(store))

            assert transport.messages == [("900", True, False, cards.image(card))]
        finally:
            await store.close()

    asyncio.run(scenario())


def test_concurrent_starts_do_not_interleave_image_link_pairs(
    transport, cards, tmp_path
):
    """Three starts for one recipient arrive at once; pairs stay adjacent.

    The old implementation needed a lock per recipient for this. Delivery now
    serializes per recipient by handing out only that recipient's lowest outbox
    row, so an image and its link can never be split by another event
    (section 5.3).
    """

    async def scenario() -> None:
        store = await _open_store(tmp_path)
        try:
            started = [
                BiliCard(
                    "live_on",
                    "已开播",
                    uid=str(index),
                    url=f"https://live.bilibili.com/{index}",
                )
                for index in range(3)
            ]
            for index, card in enumerate(started):
                await _commit(
                    store,
                    card,
                    [("group", "900")],
                    event_key=f"live:{index}:live_on:{index}",
                )
            await _deliver(store, cards, done=lambda: _drained(store), concurrency=3)

            sent = [text for _id, _channel, _private, text in transport.messages]
            assert len(sent) == 6
            for card in started:
                image = cards.image(card)
                position = sent.index(image)
                assert position % 2 == 0, sent
                assert sent[position + 1] == card.url
        finally:
            await store.close()

    asyncio.run(scenario())


def test_concurrent_starts_are_delivered_to_every_recipient(transport, cards, tmp_path):
    """One start fanned out to three recipients: each gets its own image+link."""

    async def scenario() -> None:
        store = await _open_store(tmp_path)
        try:
            card = BiliCard("live_on", "已开播", uid="123", url=LIVE_URL)
            recipients = [("group", "900"), ("group", "901"), ("user", "7")]
            assert await _commit(
                store, card, recipients, event_key="live:123:live_on:1"
            ) == len(recipients)
            await _deliver(store, cards, done=lambda: _drained(store), concurrency=3)

            assert sorted(transport.messages) == sorted(
                (recipient_id, channel, not channel, text)
                for recipient_id, channel in (
                    ("900", True),
                    ("901", True),
                    ("7", False),
                )
                for text in (cards.image(card), card.url)
            )
            # One event is rendered once, however many recipients it has.
            assert cards.renders == [cards.key(card)]
        finally:
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["image", "link"])
def test_one_recipient_failure_does_not_block_next_recipient(
    transport, cards, tmp_path, failure
):
    async def scenario() -> None:
        store = await _open_store(tmp_path)
        try:
            card = BiliCard("live_on", "已开播", uid="123", url=LIVE_URL)
            await _commit(
                store,
                card,
                [("group", "900"), ("group", "901")],
                event_key="live:123:live_on:1",
            )
            rejected = cards.image(card) if failure == "image" else card.url
            transport.fail = lambda dest, text: dest.id == "900" and text == rejected

            async def settled() -> bool:
                rows = await store.outbox_rows()
                return len(rows) == 1 and rows[0].attempts == 1

            await _deliver(store, cards, done=settled)

            # The other recipient still gets the image and the link, in order.
            assert [item for item in transport.messages if item[0] == "901"] == [
                ("901", True, False, cards.image(card)),
                ("901", True, False, card.url),
            ]
            # The failing recipient keeps whatever it managed to send.
            assert [item for item in transport.messages if item[0] == "900"] == (
                [] if failure == "image" else [("900", True, False, cards.image(card))]
            )
            # A failed send is retried instead of dropped (section 4.5 item 3).
            rows = await store.outbox_rows()
            assert [
                (row.subscriber_id, row.attempts, row.next_attempt_at) for row in rows
            ] == [("900", 1, NOW + 30)]
        finally:
            await store.close()

    asyncio.run(scenario())


def test_recipient_type_maps_to_a_channel_or_a_private_destination(monkeypatch):
    """Section 4.4 item 21: group chats are channels, private chats are users."""
    monkeypatch.setattr(handlers, "account_adapter_name", lambda _bot: "test")
    bot = object()
    group = handlers._send_dest("group", "900", bot)
    assert (group.id, group.parent_id, group.channel, group.private) == (
        "900",
        "900",
        True,
        False,
    )
    private = handlers._send_dest("user", "7", bot)
    assert (private.id, private.parent_id, private.channel, private.private) == (
        "7",
        "",
        False,
        True,
    )


# --- the sender reports an unavailable bot ----------------------------------


def test_sender_reports_an_unavailable_bot(monkeypatch, transport, cards):
    async def scenario() -> None:
        card = BiliCard("live_on", "已开播", uid="123", url=LIVE_URL)
        row = OutboxRow(
            1,
            "live:123:live_on:1",
            KIND_LIVE,
            "123",
            "live_on",
            "group",
            "900",
            json.dumps(asdict(card)),
            NOW,
            0,
            NOW,
        )
        png = cards.png(card)

        def missing():
            raise RuntimeError("No Entari account is ready")

        monkeypatch.setattr(handlers, "get_bot", missing)
        with pytest.raises(SenderUnavailable):
            await handlers._send(row, png)
        monkeypatch.setattr(handlers, "get_bot", lambda: None)
        with pytest.raises(SenderUnavailable):
            await handlers._send(row, png)
        # Nothing reached the adapter, so the row keeps its attempts.
        assert transport.messages == []
        assert row.attempts == 0

    asyncio.run(scenario())
