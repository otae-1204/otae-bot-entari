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


def cached_players(group=GROUP, addr=MC):
    return getattr(broadcast, "__BroadcastInfo")[group][addr]["players"]


def test_a_failed_send_keeps_the_change_and_sends_it_once_later(world):
    async def scenario():
        bot = qq("3898438488", [GROUP, OTHER])
        await online(world, bot)
        bot.protocol.reject = RuntimeError("send timeout")
        world.players[MC] = []  # Alice left.
        await broadcast.broadcast()
        assert sent(world) == []
        assert cached_players() == {"Alice": 0}  # Not committed: the change is still pending.
        assert world.manager.data_manager.playtimes == []
        retry_at = broadcast._retry_at[GROUP]
        assert retry_at == world.now + broadcast.MC_BROADCAST_INTERVAL

        bot.protocol.reject = None
        world.now = retry_at - 1
        await broadcast.broadcast()
        assert sent(world) == []  # Still backing off.
        world.now = retry_at
        await broadcast.broadcast()
        assert len(sent(world)) == 1 and "Alice" in sent(world)[0][1]
        assert cached_players() == {}
        assert [entry[0] for entry in world.manager.data_manager.playtimes] == ["Alice"]

        world.now += broadcast.MC_BROADCAST_INTERVAL
        await broadcast.broadcast()
        assert len(sent(world)) == 1  # Nothing is sent twice.
        assert GROUP not in broadcast._send_failures

    run(world, scenario)


def test_without_an_account_in_the_group_the_change_waits_for_one(world):
    async def scenario():
        await online(world, ir_panel())
        world.players[MC] = ["Alice", "Bob"]
        await broadcast.broadcast()
        assert sent(world) == [] and cached_players() == {"Alice": 0}
        await online(world, qq("3898438488", [GROUP]))
        world.now = broadcast._retry_at[GROUP]
        await broadcast.broadcast()
        assert [sender for sender, _ in sent(world)] == ["3898438488"]
        assert set(cached_players()) == {"Alice", "Bob"}

    run(world, scenario)


def test_repeated_failures_back_off_up_to_a_cap(world):
    waits = [broadcast._defer_group(GROUP, 0) for _ in range(8)]
    interval = broadcast.MC_BROADCAST_INTERVAL
    assert waits[:3] == [interval, 2 * interval, 4 * interval]
    assert max(waits) == max(interval, broadcast.RETRY_BACKOFF_MAX_SECONDS)
    broadcast._group_delivered(GROUP)
    assert GROUP not in broadcast._retry_at and GROUP not in broadcast._send_failures


def test_a_delivery_switched_off_for_the_group_is_not_retried(world, monkeypatch, tmp_path):
    from arclet.letoderea import Subscriber

    from otae_bot.groups.features import GroupFeatureStore, GroupScope
    from otae_bot.pipeline import feature_gate

    store = GroupFeatureStore(tmp_path / "switches.json")
    monkeypatch.setattr(feature_gate, "feature_store", store)

    async def scenario():
        bot = qq("3898438488", [GROUP])
        await online(world, bot)
        store.set_enabled(GroupScope("llonebot", "3898438488", GROUP), "minecraft_plugin", False)

        async def job():  # As the scheduler runs it: inside the plugin's subscriber.
            await broadcast.broadcast()

        job.__module__ = "plugins.minecraft_plugin.broadcast"
        world.players[MC] = ["Alice", "Bob"]
        await Subscriber(job).handle({})
        assert bot.protocol.sent == []
        assert set(cached_players()) == {"Alice", "Bob"}
        assert GROUP not in broadcast._retry_at

    run(world, scenario)


@pytest.fixture
def log_lines():
    lines = []
    sink = logger.add(lambda message: lines.append((message.record["level"].name, message.record["message"])),
                      level="DEBUG", format="{message}")
    yield lines
    logger.remove(sink)


def test_a_hung_server_times_out_without_holding_up_the_others(world, monkeypatch):
    async def scenario():
        await online(world, qq("3898438488", [GROUP]))
        hung = "hung.example:25565"
        world.manager.servers[GROUP][hung] = {"name": "挂死服"}
        getattr(broadcast, "__BroadcastInfo")[GROUP][hung] = {
            "name": "挂死服", "address": hung, "players": None, "players_hidden": False, "online_players": 0,
        }
        never = asyncio.Event()
        answered = broadcast.ping

        async def ping(address, server_type="java"):
            if address == hung:
                world.pinged.append(address)
                await never.wait()  # Like a mcstatus thread stuck on a server that never answers.
            return await answered(address, server_type)

        monkeypatch.setattr(broadcast, "ping", ping)
        monkeypatch.setattr(broadcast, "PING_TIMEOUT_SECONDS", 0.05)
        world.players[MC] = ["Alice", "Bob"]
        await asyncio.wait_for(broadcast.broadcast(), 5)
        assert [sender for sender, _ in sent(world)] == ["3898438488"]
        assert getattr(broadcast, "__BroadcastInfo")[GROUP][hung]["players"] is None  # The unreachable path.

        # The stuck ping is reused, not started again, until it finishes.
        world.now += broadcast.MC_BROADCAST_INTERVAL
        await asyncio.wait_for(broadcast.broadcast(), 5)
        assert world.pinged.count(hung) == 1
        never.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert hung not in broadcast._inflight_pings

    run(world, scenario)


def test_a_stalled_tick_is_abandoned_so_later_ticks_run(world, monkeypatch, log_lines):
    async def scenario():
        await online(world, qq("3898438488", [GROUP]))
        stuck = asyncio.Event()

        async def stalled():
            await stuck.wait()

        monkeypatch.setattr(world.manager, "get_all_broadcast_servers", stalled)
        monkeypatch.setattr(broadcast, "BROADCAST_TICK_TIMEOUT_SECONDS", 0.05)
        await asyncio.wait_for(broadcast.broadcast(), 5)  # Returns: the scheduler sees the job finish.
        assert any(level == "WARNING" and "已中止" in text for level, text in log_lines)

    run(world, scenario)


def test_each_tick_logs_one_line_at_debug_when_idle_and_info_when_something_happened(world, log_lines):
    async def scenario():
        await online(world, qq("3898438488", [GROUP, OTHER]))
        await broadcast.broadcast()
        ticks = [(level, text) for level, text in log_lines if "检查完成" in text]
        assert len(ticks) == 1 and ticks[0][0] == "DEBUG" and "到期群 2" in ticks[0][1]
        log_lines.clear()
        world.now += broadcast.MC_BROADCAST_INTERVAL
        world.players[MC] = ["Alice", "Bob"]
        await broadcast.broadcast()
        ticks = [(level, text) for level, text in log_lines if "检查完成" in text]
        assert len(ticks) == 1 and ticks[0][0] == "INFO" and "变化 1 条" in ticks[0][1]
        assert "Bob" not in ticks[0][1]

    run(world, scenario)
