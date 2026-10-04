"""Group pushes go out through an account that is in the group (issue #20).

Two LLBot accounts are online; the latest login used to be the only sender, so
groups that only the other account had joined lost every push after LLBot
answered "getGroupImageUploadInfo ... msgInfo undefined" four times.
"""

from __future__ import annotations

import asyncio
import io
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from loguru import logger
from PIL import Image

from otae_bot.adapters import runtime
from plugins.bilibilibot import handlers
from plugins.bilibilibot.models import BiliCard, BiliEvent, KIND_LIVE, OutboxRow
from plugins.bilibilibot.notifier import Notifier
from plugins.bilibilibot.store import BiliStore

NOW = 1000
GROUP = "977295722"
LIVE_URL = "https://live.bilibili.com/25731103"
_buffer = io.BytesIO()
Image.new("RGB", (1, 1)).save(_buffer, format="PNG")
PNG = _buffer.getvalue()
MSGINFO = "ServerException: getGroupImageUploadInfo failed: Cannot read properties of undefined (reading 'msgInfo')"


def account(self_id, groups=None):
    async def pages():
        if groups is None:
            raise RuntimeError("guild.list unavailable")
        for group in groups:
            yield SimpleNamespace(id=group)

    return SimpleNamespace(self_id=self_id, platform="qq", protocol=SimpleNamespace(guild_list=pages))


@pytest.fixture
def accounts():
    runtime.clear_account()
    yield
    runtime.clear_account()


@pytest.fixture
def transport(monkeypatch):
    """Records (account, recipient, text); `reject` maps an account id to the error it raises."""
    state = SimpleNamespace(messages=[], reject={})

    async def send(chain, dest=None, bot=None):
        error = state.reject.get(bot.self_id)
        if error is not None:
            raise error
        state.messages.append((bot.self_id, dest.id, str(chain)))

    monkeypatch.setattr(handlers.ChainMsg, "send", send)
    return state


@pytest.fixture
def errors():
    lines = []
    sink = logger.add(lambda message: lines.append(message.record["message"]), level="ERROR", format="{message}")
    yield lines
    logger.remove(sink)


def row(subscriber_type="group", subscriber_id=GROUP, card_type="live_on", attempts=0):
    card = BiliCard(card_type, "已开播", uid="123", url=LIVE_URL)
    return OutboxRow(1, "live:123:live_on:1", KIND_LIVE, "123", card_type, subscriber_type, subscriber_id,
                     json.dumps(asdict(card)), NOW, attempts, NOW)


async def online(*bots):
    for bot in bots:
        runtime.set_account(bot)
    await runtime.refresh_guilds()


def test_group_push_uses_the_account_in_that_group(accounts, transport):
    async def scenario():
        bot1, bot2 = account("1194397508", ["100"]), account("3898438488", [GROUP])
        await online(bot2, bot1)  # bot1 logged in last: it is the default.
        await handlers._send(row(), PNG)
        # Image and link both from bot2; bot1 is not in the group and was never tried.
        assert [(sender, recipient) for sender, recipient, _ in transport.messages] == [
            ("3898438488", GROUP), ("3898438488", GROUP),
        ]
        assert transport.messages[1][2] == LIVE_URL
        transport.messages.clear()
        await handlers._send(row(subscriber_id="100"), PNG)
        assert {sender for sender, _, _ in transport.messages} == {"1194397508"}

    asyncio.run(scenario())


def test_a_not_in_group_error_switches_account_within_the_same_attempt(accounts, transport, tmp_path):
    async def scenario():
        # guild.list failed for both, so membership is unknown: try the default first.
        bot1, bot2 = account("1194397508"), account("3898438488")
        await online(bot2, bot1)
        transport.reject["1194397508"] = RuntimeError(MSGINFO)
        store = BiliStore(tmp_path / "bilibili.db", tmp_path / "missing.db")
        await store.open()
        try:
            card = BiliCard("live_on", "已开播", uid="123", url=LIVE_URL)
            await store.apply_poll_result([], [], [BiliEvent(KIND_LIVE, "123", card, event_key="live:123:live_on:1")],
                                          NOW, expand=lambda _event: [("group", GROUP)], event_key=lambda item: item.event_key)

            async def render(_card):
                return PNG

            # A fixed clock: a retry would never come due, so the row would stay.
            notifier = Notifier(store, render=render, send=handlers._send, clock=lambda: NOW)
            await notifier.start()
            try:
                for _ in range(500):
                    if await store.outbox_count() == 0:
                        break
                    await asyncio.sleep(0.01)
            finally:
                await notifier.stop()
            # Delivered by bot2 on the first attempt: no retry was used up.
            assert await store.outbox_count() == 0
            assert [sender for sender, _, _ in transport.messages] == ["3898438488", "3898438488"]
        finally:
            await store.close()

    asyncio.run(scenario())


def test_no_online_account_in_the_group_is_an_explicit_error(accounts, transport, errors):
    async def scenario():
        await online(account("1194397508", ["100"]), account("3898438488", ["200"]))
        with pytest.raises(LookupError):
            await handlers._send(row(), PNG)
        assert transport.messages == []
        assert len(errors) == 1
        assert GROUP in errors[0]
        assert "1194397508" in errors[0] and "3898438488" in errors[0]

    asyncio.run(scenario())


def test_every_account_rejected_is_reported_once_and_counts_as_one_failure(accounts, transport, errors):
    async def scenario():
        bot1, bot2 = account("1194397508", [GROUP]), account("3898438488", [GROUP])
        await online(bot2, bot1)
        transport.reject.update({"1194397508": RuntimeError(MSGINFO), "3898438488": RuntimeError("账号不在该群")})
        with pytest.raises(RuntimeError, match="不在该群"):
            await handlers._send(row(), PNG)
        assert len(errors) == 1
        assert GROUP in errors[0] and "tried 1194397508, 3898438488" in errors[0]

    asyncio.run(scenario())


def test_other_errors_and_private_chats_do_not_switch_account(accounts, transport):
    async def scenario():
        bot1, bot2 = account("1194397508", [GROUP]), account("3898438488", [GROUP])
        await online(bot2, bot1)
        transport.reject["1194397508"] = RuntimeError("send timeout")
        with pytest.raises(RuntimeError, match="send timeout"):
            await handlers._send(row(), PNG)
        assert transport.messages == []
        transport.reject["1194397508"] = RuntimeError(MSGINFO)
        with pytest.raises(RuntimeError, match="msgInfo"):
            await handlers._send(row(subscriber_type="user", subscriber_id="7"), PNG)
        assert transport.messages == []
        transport.reject.clear()
        await handlers._send(row(subscriber_type="user", subscriber_id="7", card_type="video"), PNG)
        assert [(sender, recipient) for sender, recipient, _ in transport.messages] == [("1194397508", "7")]

    asyncio.run(scenario())


def test_not_in_group_errors_are_recognised():
    for text in (MSGINFO, "机器人不在群内", "Bot is not in group 100", "账号不在该群"):
        assert handlers._not_in_group(RuntimeError(text)), text
    for text in ("send timeout", "HTTP 500: 消息为空", "rate limited"):
        assert not handlers._not_in_group(RuntimeError(text)), text
