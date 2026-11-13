"""Player-change broadcasts: an account in the group sends, kicked-group pause, error digest to the superuser.

Real Satori accounts with the real EntariProtocol; only HTTP and the server ping are faked.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from unittest.mock import AsyncMock

import pytest

from otae_bot.accounts import runtime
from tests.support.satori_accounts import make_account, pin_event_loop

importlib.import_module("plugins.minecraft_plugin.broadcast")
broadcast = sys.modules["plugins.minecraft_plugin.broadcast"]


@pytest.fixture
def mc(monkeypatch):
    runtime.clear_account()
    monkeypatch.setattr(broadcast, "_bm", None)
    monkeypatch.setattr(broadcast, "__BroadcastInfo", {"100": {"a": {"address": "a", "name": "srv", "players": {}}}})
    monkeypatch.setattr(broadcast, "_kicked_groups", set())
    monkeypatch.setattr(broadcast, "_last_error_digest_at", {})
    monkeypatch.setattr(broadcast, "_is_group_broadcast_due", lambda *_: True)
    monkeypatch.setattr(broadcast, "_mark_group_broadcasted", lambda *_: None)
    monkeypatch.setattr(broadcast, "ping", AsyncMock(return_value={"status": "success", "data": {"players": ["Steve"]}}))
    yield
    runtime.clear_account()


def run(reject=None, times=1):
    """One account in group 100; run `times` broadcasts; return what it sent."""
    async def scenario():
        with pin_event_loop():
            bot = make_account("9", platform="llonebot", guilds=["100"])
            bot.protocol.reject = reject
            runtime.set_account(bot)
            await runtime.refresh_guilds()
            for _ in range(times):
                await broadcast.broadcast()
            private = [(params["channel_id"], params["content"]) for action, params in bot.protocol.calls
                       if action == "message.create" and params["channel_id"].startswith("private:")]
            groups = [action for action, params in bot.protocol.calls
                      if action == "message.create" and params["channel_id"] == "100"]
            return bot.protocol.sent, private, groups

    return asyncio.run(scenario())


def test_changes_go_to_the_group_through_an_account_in_it(mc):
    sent, private, _ = run()
    assert sent == [("100", "[MC_Server] srv：Steve 加入了服务器。")]
    assert private == []


def test_a_kicked_group_is_paused_without_an_error_digest(mc):
    _, private, groups = run(RuntimeError("发送失败：已被移出该群"), times=2)
    assert broadcast._kicked_groups == {"100"}
    assert private == []
    assert len(groups) == 1  # Paused: the second run does not try again.


def test_other_send_errors_reach_the_superuser_with_the_traceback(mc):
    _, private, _ = run(RuntimeError("timeout"))
    assert not broadcast._kicked_groups
    [(channel, message)] = private
    assert channel == "private:" + broadcast.superuser
    assert "群 100 发送消息失败" in message and "RuntimeError: timeout" in message
