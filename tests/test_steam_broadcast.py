from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from otae_bot.adapters import feature_gate
from otae_bot.group_features import GroupFeatureStore, GroupScope
from plugins.steamInfo import handlers


class Disabled:
    def __init__(self):
        self.parents, self.saved = set(), 0

    def is_disabled(self, parent_id):
        return parent_id in self.parents

    def add(self, parent_id):
        self.parents.add(parent_id)

    def save(self):
        self.saved += 1


@pytest.fixture
def steam(monkeypatch):
    bot = SimpleNamespace(self_id="9", platform="qq", protocol=SimpleNamespace(
        send_message=AsyncMock(return_value=[SimpleNamespace(id="m")])))
    disabled = Disabled()
    entry = {"type": "start", "player": {"steamid": "1", "gameextrainfo": "Factorio"}}
    monkeypatch.setattr(handlers, "get_bot", lambda: bot)
    monkeypatch.setattr(handlers, "disable_parent_data", disabled)
    monkeypatch.setattr(handlers, "config", SimpleNamespace(steam_broadcast_type="none"))
    monkeypatch.setattr(handlers, "steam_info_data", SimpleNamespace(compare=lambda *_: [entry]))
    monkeypatch.setattr(handlers, "_display_name", lambda *_: "Alice")
    directory = tempfile.TemporaryDirectory()
    store = GroupFeatureStore(Path(directory.name) / "switches.json")
    monkeypatch.setattr(feature_gate, "feature_store", store)
    yield SimpleNamespace(bot=bot, disabled=disabled, store=store)
    directory.cleanup()


def broadcast():
    return asyncio.run(handlers.broadcast_steam_info("100", {"response": {"players": []}}))


def test_broadcast_goes_to_the_group_through_the_default_account(steam):
    assert broadcast() is True
    channel, message = steam.bot.protocol.send_message.await_args.args
    assert (channel, str(message)) == ("100", "Alice 开始玩 Factorio 了")


def test_switched_off_group_is_skipped_but_counts_as_done(steam):
    steam.store.set_enabled(GroupScope("qq", "9", "100"), "steamInfo", False)
    assert asyncio.run(_as_steam_job()) is True
    steam.bot.protocol.send_message.assert_not_awaited()
    assert not steam.disabled.parents


async def _as_steam_job():
    from arclet.letoderea import Subscriber

    async def job():
        return await handlers.broadcast_steam_info("100", {"response": {"players": []}})

    job.__module__ = "plugins.steamInfo.handlers"
    return await Subscriber(job).handle({})


def test_kicked_group_disables_its_broadcast(steam):
    steam.bot.protocol.send_message.side_effect = RuntimeError("发送失败：已被移出该群")
    assert broadcast() is False
    assert steam.disabled.parents == {"100"} and steam.disabled.saved == 1


def test_other_send_errors_keep_the_broadcast(steam):
    steam.bot.protocol.send_message.side_effect = RuntimeError("timeout")
    assert broadcast() is False
    assert not steam.disabled.parents
