"""MC 进服播报由在该群里的账号发出，绝不交给“最近登录”的账号。

生产上同时连着两个 LLBot（QQ）和 Ir 面板（原生 Satori，“群”是文件夹）。
Ir 最后登录后成了默认账号，播报被交给它往 QQ 群发，全部失败且无日志。
真实的 Satori Account 与 EntariProtocol，只伪造 HTTP 调用与服务器 ping。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger
from satori import Event, Guild, Login, User

from otae_bot.accounts import runtime
from otae_bot.accounts.ready import get_bot
from tests.support.satori_accounts import make_account, pin_event_loop

ROOT = Path(__file__).resolve().parents[1]
GROUP = "977295722"
OTHER = "100"
SUPERUSER = "2461673400"
MC = "mc.example:25565"


def _load_broadcast():
    """只加载 broadcast.py，不执行插件包的 handlers（它会在当前目录建数据文件）。"""
    name = "plugins.minecraft_plugin.broadcast_for_account_test"
    previous = sys.modules.get("plugins.minecraft_plugin")
    if previous is None:
        package = types.ModuleType("plugins.minecraft_plugin")
        package.__path__ = [str(ROOT / "plugins/minecraft_plugin")]
        sys.modules["plugins.minecraft_plugin"] = package
    try:
        spec = importlib.util.spec_from_file_location(name, ROOT / "plugins/minecraft_plugin/broadcast.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("plugins.minecraft_plugin", None)


broadcast = _load_broadcast()


def qq(self_id, groups):
    return make_account(self_id, platform="llonebot", guilds=groups)


def ir_panel():
    # Iridium: native Satori, a non-numeric login id, and "guilds" that are folders.
    return make_account("ir-7f3a", platform="ir_web", guilds=["folder-root", "folder-mc"])


class FakeDataManager:
    def __init__(self):
        self.playtimes = []

    def get_group_broadcast_interval(self, group_id, default):
        return default

    def add_player_gametime(self, player_name, group_id, server_name, gametime):
        self.playtimes.append((player_name, str(group_id), server_name, gametime))


class FakeBroadcastManager:
    def __init__(self, servers):
        self.servers = servers
        self.data_manager = FakeDataManager()

    async def get_all_broadcast_servers(self):
        return self.servers


def _reset():
    for table in ("_last_broadcast_at", "_last_error_digest_at", "_kicked_groups",
                  "_send_failures", "_retry_at"):
        if hasattr(broadcast, table):
            getattr(broadcast, table).clear()


@pytest.fixture
def world(monkeypatch):
    """Two configured groups, one server each; `players[address]` is what the next ping answers."""
    runtime.clear_account()
    state = SimpleNamespace(accounts=[], players={}, pinged=[], now=1_000_000)
    manager = FakeBroadcastManager({
        GROUP: {MC: {"name": "生存服"}},
        OTHER: {"other.example:25565": {"name": "创造服"}},
    })
    cache = getattr(broadcast, "__BroadcastInfo")
    cache.clear()
    cache.update({
        GROUP: {MC: {"name": "生存服", "address": MC,
                     "players": {"Alice": 0}, "players_hidden": False, "online_players": 1}},
        OTHER: {"other.example:25565": {"name": "创造服", "address": "other.example:25565",
                                        "players": {}, "players_hidden": False, "online_players": 0}},
    })
    state.players = {MC: ["Alice"], "other.example:25565": []}

    async def ping(address, server_type="java"):
        state.pinged.append(address)
        players = state.players.get(address)
        if players is None:
            return {"status": "error", "data": {}}
        return {"status": "success", "data": {"players": list(players), "online_players": len(players)}}

    monkeypatch.setattr(broadcast, "ping", ping)
    monkeypatch.setattr(broadcast, "time", SimpleNamespace(time=lambda: state.now))
    monkeypatch.setattr(broadcast, "superuser", SUPERUSER)
    monkeypatch.setattr(broadcast, "_bm", manager)
    _reset()
    state.manager = manager
    yield state
    cache.clear()
    _reset()
    runtime.clear_account()


@pytest.fixture
def warnings():
    lines = []
    sink = logger.add(lambda message: lines.append(message.record["message"]), level="WARNING", format="{message}")
    yield lines
    logger.remove(sink)


def run(world, scenario):
    async def pinned():
        with pin_event_loop():
            await scenario()

    asyncio.run(pinned())


async def online(world, *bots):
    for bot in bots:
        runtime.set_account(bot)
        world.accounts.append(bot)
    await runtime.refresh_guilds()


def sent(world, to=GROUP):
    """(sender id, content) of every message.create to channel `to`."""
    return [(bot.self_id, content) for bot in world.accounts for channel, content in bot.protocol.sent if channel == to]


def test_a_change_is_sent_by_the_account_in_that_group_not_the_latest_login(world):
    async def scenario():
        await online(world, qq("1194397508", [OTHER]), qq("3898438488", [GROUP]), ir := ir_panel())
        assert get_bot() is ir  # Ir logged in last: it is the default account.
        world.players[MC] = ["Alice", "Bob"]
        await broadcast.broadcast()
        assert [sender for sender, _ in sent(world)] == ["3898438488"]
        assert "Bob 加入了服务器" in sent(world)[0][1]
        assert ir.protocol.sent == []

    run(world, scenario)


def test_with_no_account_in_the_group_nothing_goes_to_another_account(world, warnings):
    async def scenario():
        await online(world, qq("1194397508", [OTHER]), ir := ir_panel())
        world.players[MC] = ["Alice", "Bob"]
        await broadcast.broadcast()
        assert sent(world) == [] and ir.protocol.sent == []
        assert any(GROUP in line and "没有在线账号在群内" in line for line in warnings)
        # The superuser digest goes out privately through the QQ account serving OTHER.
        assert [sender for sender, _ in sent(world, "private:" + SUPERUSER)] == ["1194397508"]

    run(world, scenario)


def test_an_account_whose_group_listing_failed_is_never_used(world, warnings):
    async def scenario():
        await online(world, make_account("1194397508", guilds=None), ir_panel())  # QQ groups unknown.
        world.players[MC] = ["Alice", "Bob"]
        await broadcast.broadcast()
        assert all(bot.protocol.sent == [] for bot in world.accounts)  # Not even the digest.
        assert any("异常摘要未发送" in line for line in warnings)

    run(world, scenario)


def test_a_kicked_account_hands_over_to_another_in_the_group(world):
    async def scenario():
        first, second = qq("1194397508", [GROUP]), qq("3898438488", [GROUP])
        await online(world, second, first)
        first.protocol.reject = RuntimeError("500: 已被移出该群")
        world.players[MC] = ["Alice", "Bob"]
        await broadcast.broadcast()
        assert [sender for sender, _ in sent(world)] == ["3898438488"]
        assert GROUP not in broadcast._kicked_groups
        assert GROUP not in (runtime.known_guilds(first) or ())

    run(world, scenario)


def test_send_failures_are_logged_with_group_and_error_type(world, warnings):
    async def scenario():
        bot = qq("1194397508", [GROUP, OTHER])
        await online(world, bot)
        bot.protocol.reject = TimeoutError("secret message text")
        world.players[MC] = ["Alice", "Bob"]
        await broadcast.broadcast()
        failure = [line for line in warnings if "播报发送失败" in line]
        assert failure and GROUP in failure[0] and "TimeoutError" in failure[0]
        assert "Bob" not in failure[0]

    run(world, scenario)


def test_rejoin_is_recognised_for_the_account_that_received_the_event(world):
    async def scenario():
        member, ir = qq("3898438488", [GROUP]), ir_panel()
        await online(world, member, ir)  # The default account is Ir; the event came in through member.
        event = Event("guild-member-added", datetime.now(timezone.utc),
                      Login(user=User("3898438488"), platform="llonebot"),
                      guild=Guild(GROUP), user=User("3898438488"))
        assert broadcast._is_bot_added(member, event)
        assert not broadcast._is_bot_added(ir, event)

    run(world, scenario)
