"""Player-change broadcasts: default account, kicked-group pause, error digest to the superuser."""

from __future__ import annotations

import asyncio
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

importlib.import_module("plugins.minecraft_plugin.broadcast")
broadcast = sys.modules["plugins.minecraft_plugin.broadcast"]


@pytest.fixture
def mc(monkeypatch):
    bot = SimpleNamespace(self_id="9", platform="qq", protocol=SimpleNamespace(
        send_message=AsyncMock(return_value=[SimpleNamespace(id="m")]),
        send_private_message=AsyncMock(return_value=[SimpleNamespace(id="p")]),
    ))
    monkeypatch.setattr(broadcast, "_bm", None)
    monkeypatch.setattr(broadcast, "get_bot", lambda: bot)
    monkeypatch.setattr(broadcast, "__BroadcastInfo", {"100": {"a": {"address": "a", "name": "srv", "players": {}}}})
    monkeypatch.setattr(broadcast, "_kicked_groups", set())
    monkeypatch.setattr(broadcast, "_last_error_digest_at", {})
    monkeypatch.setattr(broadcast, "_is_group_broadcast_due", lambda *_: True)
    monkeypatch.setattr(broadcast, "_mark_group_broadcasted", lambda *_: None)
    monkeypatch.setattr(broadcast, "ping", AsyncMock(return_value={"status": "success", "data": {"players": ["Steve"]}}))
    monkeypatch.setattr(broadcast, "_check_player_changes_simple", lambda *_: ["Steve 上线了"])
    return bot


def test_changes_go_to_the_group_through_the_default_account(mc):
    asyncio.run(broadcast.broadcast())
    channel, message = mc.protocol.send_message.await_args.args
    assert (channel, str(message)) == ("100", "Steve 上线了")
    mc.protocol.send_private_message.assert_not_awaited()


def test_a_kicked_group_is_paused_without_an_error_digest(mc):
    mc.protocol.send_message.side_effect = RuntimeError("发送失败：已被移出该群")
    asyncio.run(broadcast.broadcast())
    assert broadcast._kicked_groups == {"100"}
    mc.protocol.send_private_message.assert_not_awaited()
    mc.protocol.send_message.reset_mock()
    asyncio.run(broadcast.broadcast())
    mc.protocol.send_message.assert_not_awaited()


def test_other_send_errors_reach_the_superuser_with_the_traceback(mc):
    mc.protocol.send_message.side_effect = RuntimeError("timeout")
    asyncio.run(broadcast.broadcast())
    assert not broadcast._kicked_groups
    user, message = mc.protocol.send_private_message.await_args.args
    assert user == broadcast.superuser
    assert "群 100 发送消息失败" in str(message) and "RuntimeError: timeout" in str(message)
