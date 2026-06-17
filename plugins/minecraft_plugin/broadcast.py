"""Minecraft 服务器广播子系统 — 定时轮询 + 玩家变化检测 + 消息播报."""

from __future__ import annotations

import asyncio
import logging
import time
import traceback

from utils.entari_native import listen_notice, get_bot
from arclet.entari import Account as Bot, Event
from utils.entari_native import Pred
from utils.entari_native import ChainMsg, SendDest, account_adapter_name
from utils.entari_native import timer

from configs.config import Config, MC_BROADCAST_INTERVAL
from .data_source import PlayerGameTimeManager
from .ping import ping
from .broadcast_utils import (
    build_broadcast_snapshot,
    build_player_change_messages,
    group_errors_by_group,
    should_send_error_digest,
)

logger = logging.getLogger(__name__)

superuser = str(Config.SUPERUSERS[0]) if Config.SUPERUSERS else "2461673400"

# 全局广播缓存: {group_id: {server_name: {"address": str, "players": dict|None}}}
__BroadcastInfo: dict = {}
_broadcast_lock = asyncio.Lock()

# 被踢出的群（暂停广播，保留配置，等 bot 重新入群后恢复）
_kicked_groups: set[str] = set()
_last_error_digest_at: dict[str, int] = {}
ERROR_DIGEST_COOLDOWN = 600


# ── 监听 bot 重新入群 → 清除踢出标记 ──

async def _is_bot_added(event: Event) -> bool:
    if event.__class__.__name__ != "GuildMemberAddedEvent":
        return False
    bot_self_id = get_bot().self_id if get_bot() else ""
    return str(event.user.id) == str(bot_self_id)


_bot_rejoin = listen_notice(rule=Pred(_is_bot_added), priority=5, block=False)


@_bot_rejoin.handle()
async def _clear_kick_mark(event: Event):
    guild_id = str(event.guild.id) if event.guild else ""
    if guild_id:
        _kicked_groups.discard(guild_id)
        logger.info(f"Bot 重新加入群 {guild_id}，恢复广播")
    await _bot_rejoin.finish()


async def safe_ping(server_address: str, server_type: str = "java") -> dict:
    """安全的 ping 包装，统一返回格式."""
    try:
        return await ping(server_address, server_type)
    except Exception as exc:
        return {"status": "error", "data": {}, "error": str(exc)}


def _check_player_changes_simple(
    server_name: str, previous_players: dict, current_players: list[str] | None, group_id: int,
) -> list:
    """对比新旧玩家列表，生成变化消息."""
    player_game_time_manager = PlayerGameTimeManager()
    messages, playtime_deltas = build_player_change_messages(
        server_name, previous_players, current_players, int(time.time())
    )
    for player, duration in playtime_deltas.items():
        player_game_time_manager.add_player_gametime(
            player_name=player, group_id=group_id,
            server_name=server_name, gametime=duration,
        )
    return messages


_bm = None  # BroadcastManager 引用，由 init_broadcast 设置


def _normalize_servers(data) -> dict:
    """兼容 list/dict 两种历史数据格式，统一转为 {name: info}."""
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {s["name"]: s for s in data if isinstance(s, dict) and "name" in s}
    return {}


async def _ping_with_timeout(addr: str, timeout: float = 5) -> dict:
    """带超时的 ping，避免离线服务器卡死初始化."""
    try:
        return await asyncio.wait_for(ping(addr, "java"), timeout=timeout)
    except (asyncio.TimeoutError, Exception):
        return {"status": "error", "data": {}}


async def init_broadcast(bm):
    """Bot 启动时初始化广播缓存."""
    global __BroadcastInfo, _bm
    _bm = bm
    server_list = await bm.get_all_broadcast_servers()
    logger.info("Initializing broadcast server cache")

    tasks = []
    entries: list[tuple[str, str, str, str]] = []  # (group_id, addr, name, address)
    for group_id, servers in server_list.items():
        for addr, server_info in _normalize_servers(servers).items():
            name = server_info.get("name", addr)
            if addr:
                entries.append((group_id, addr, name, addr))
                tasks.append(_ping_with_timeout(addr))

    results = await asyncio.gather(*tasks)
    for (group_id, addr, name, _), result in zip(entries, results):
        group = __BroadcastInfo.setdefault(group_id, {})
        if addr in group:
            # 已初始化过（如 bot 重连），保留玩家加入时间，仅更新元数据
            group[addr]["name"] = name
            group[addr]["address"] = addr
            continue
        if result.get("status") == "success":
            data = result.get("data", {})
            player_list = data.get("players", [])
            players_hidden = bool(data.get("players_hidden"))
            online_players = int(data.get("online_players", 0) or 0)
        else:
            player_list = None
            players_hidden = False
            online_players = 0
        group[addr] = {
            "name": name,
            "address": addr,
            "players": (
                {}
                if players_hidden
                else {p: int(time.time()) for p in player_list}
                if player_list is not None
                else None
            ),
            "players_hidden": players_hidden,
            "online_players": online_players,
        }
    logger.info("Broadcast server cache initialized: %s servers", len(entries))


async def _sync_server_list():
    """从 DB 同步服务器列表到缓存（增量更新：新增/移除服务器，不丢失当前玩家状态）."""
    if _bm is None:
        return
    try:
        db_servers = await _bm.get_all_broadcast_servers()
        async with _broadcast_lock:
            # 移除 DB 中已删除的服务器（按 address key 比对）
            for gid in list(__BroadcastInfo.keys()):
                db_servers_normalized = _normalize_servers(db_servers.get(gid, {}))
                for addr in list(__BroadcastInfo[gid].keys()):
                    if addr not in db_servers_normalized:
                        del __BroadcastInfo[gid][addr]
                if not __BroadcastInfo[gid]:
                    del __BroadcastInfo[gid]
            # 添加 DB 中新增的服务器
            for gid, servers in db_servers.items():
                __BroadcastInfo.setdefault(gid, {})
                for addr, si in _normalize_servers(servers).items():
                    if addr not in __BroadcastInfo[gid]:
                        __BroadcastInfo[gid][addr] = {
                            "name": si.get("name", addr),
                            "address": addr,
                            "players": None,
                            "players_hidden": False,
                            "online_players": 0,
                        }
    except Exception:
        pass


@timer.scheduled_job("interval", seconds=MC_BROADCAST_INTERVAL, misfire_grace_time=30, max_instances=1)
async def broadcast():
    """定时轮询所有服务器，检测玩家变化并播报（同时同步 DB 配置）."""
    await _sync_server_list()

    bot: Bot = get_bot()
    errors: list[str] = []
    now = int(time.time())

    async with _broadcast_lock:
        snapshot = {
            gid: {
                sn: {
                    k: (dict(v) if k == "players" and isinstance(v, dict) else v)
                    for k, v in si.items()
                }
                for sn, si in servers.items()
            }
            for gid, servers in __BroadcastInfo.items()
        }

    for group_id, group_servers in snapshot.items():
        if str(group_id) in _kicked_groups:
            continue
        group_messages: list[str] = []
        for addr, server_info in group_servers.items():
            try:
                server_address = server_info.get("address", addr)
                if not server_address:
                    continue
                server_name = server_info.get("name", addr)

                server_status = await ping(server_address, server_type="java")
                current_players = None
                current_players_hidden = False
                current_online_players = 0
                if server_status.get("status") == "success":
                    data = server_status.get("data", {})
                    current_players_hidden = bool(data.get("players_hidden"))
                    current_online_players = int(data.get("online_players", 0) or 0)
                    current_players = data.get("players", [])

                previous_players = server_info.get("players", {})
                previous_players_hidden = bool(server_info.get("players_hidden"))

                if current_players_hidden:
                    async with _broadcast_lock:
                        __BroadcastInfo[group_id][addr]["players"] = {}
                        __BroadcastInfo[group_id][addr]["players_hidden"] = True
                        __BroadcastInfo[group_id][addr]["online_players"] = current_online_players
                    continue

                if previous_players_hidden and current_players is not None:
                    async with _broadcast_lock:
                        __BroadcastInfo[group_id][addr]["players"] = {
                            p: now for p in current_players
                        }
                        __BroadcastInfo[group_id][addr]["players_hidden"] = False
                        __BroadcastInfo[group_id][addr]["online_players"] = current_online_players
                    continue

                if previous_players is not None and current_players is None:
                    for _ in range(3):
                        await asyncio.sleep(1)
                        confirm = await safe_ping(server_address)
                        if confirm.get("status") == "success":
                            data = confirm.get("data", {})
                            if data.get("players_hidden"):
                                current_players_hidden = True
                                current_online_players = int(data.get("online_players", 0) or 0)
                                break
                            current_players = data.get("players", [])
                            current_online_players = int(data.get("online_players", 0) or 0)
                            break

                if current_players_hidden:
                    async with _broadcast_lock:
                        __BroadcastInfo[group_id][addr]["players"] = {}
                        __BroadcastInfo[group_id][addr]["players_hidden"] = True
                        __BroadcastInfo[group_id][addr]["online_players"] = current_online_players
                    continue

                group_messages.extend(
                    _check_player_changes_simple(server_name, previous_players, current_players, group_id)
                )

                async with _broadcast_lock:
                    if current_players is not None:
                        prev = previous_players or {}
                        __BroadcastInfo[group_id][addr]["players"] = {
                            p: prev.get(p, now) for p in current_players
                        }
                        __BroadcastInfo[group_id][addr]["players_hidden"] = False
                        __BroadcastInfo[group_id][addr]["online_players"] = current_online_players
                    else:
                        __BroadcastInfo[group_id][addr]["players"] = None
                        __BroadcastInfo[group_id][addr]["players_hidden"] = False
                        __BroadcastInfo[group_id][addr]["online_players"] = 0

            except Exception:
                errors.append(f"群 {group_id} 服务器 {addr}: {traceback.format_exc()}")

        if group_messages:
            try:
                target = SendDest(str(group_id), str(group_id), True, False, "", account_adapter_name(bot))
                await ChainMsg.text("\n".join(group_messages)).send(target, bot)
            except Exception as exc:
                err_msg = str(exc)
                # 检测 bot 被移出群 / 群解散（含 content bytes 中的中文错误）
                if "已被移出该群" in err_msg or "已被移除" in err_msg or "群已解散" in err_msg:
                    logger.warning(f"Bot 已被移出群 {group_id}，暂停该群广播")
                    _kicked_groups.add(str(group_id))
                elif "500" in err_msg and ("移出" in err_msg or "移除" in err_msg or "解散" in err_msg):
                    logger.warning(f"Bot 已被移出群 {group_id}，暂停该群广播")
                    _kicked_groups.add(str(group_id))
                else:
                    errors.append(f"群 {group_id} 发送消息失败: {traceback.format_exc()}")

    if errors:
        try:
            target = SendDest(superuser, "", False, True, "", account_adapter_name(bot))
            grouped = group_errors_by_group(errors)
            now_ts = int(time.time())
            lines: list[str] = []
            suppressed = 0
            for gid, group_errors in grouped.items():
                if should_send_error_digest(
                    gid, len(group_errors), now_ts, _last_error_digest_at, ERROR_DIGEST_COOLDOWN
                ):
                    lines.extend(group_errors[-3:])
                else:
                    suppressed += len(group_errors)
            if lines:
                suffix = f"\n已节流 {suppressed} 项重复错误" if suppressed else ""
                await ChainMsg.text(
                    f"广播轮次错误汇总 ({len(lines)} 项):\n" + "\n---\n".join(lines[-5:]) + suffix
                ).send(target, bot)
        except Exception:
            pass


def get_player_online_times(group_id: str, server_address: str) -> dict[str, str]:
    """获取指定服务器当前在线玩家的时长（用于排行榜展示）.

    Returns:
        {player_name: "Xh Xm"}  或空 dict
    """
    now = int(time.time())
    result: dict[str, str] = {}
    for _addr, info in _find_broadcast_servers(group_id, server_address):
        if info.get("players_hidden"):
            return result
        players = info.get("players")
        if isinstance(players, dict):
            for pname, join_ts in players.items():
                duration = now - int(join_ts)
                hours, remainder = divmod(duration, 3600)
                minutes = remainder // 60
                if hours > 0:
                    result[pname] = f"{hours}h {minutes}m"
                else:
                    result[pname] = f"{minutes}m"
        return result
    return result


def _find_broadcast_servers(group_id: str, match: str) -> list[tuple[str, dict]]:
    """在广播缓存中查找匹配的服务器.

    match 可以是服务器名称（精确/模糊）或地址。
    Returns [(server_name, server_info), ...]
    """
    gid = str(group_id)
    servers = __BroadcastInfo.get(gid, {})
    results: list[tuple[str, dict]] = []
    match_lower = match.lower() if match else ""
    for key, info in servers.items():
        addr = info.get("address", key)
        name = info.get("name", "")
        if not match or match == addr or match_lower == name.lower() or match_lower in name.lower():
            results.append((key, info))
    if not results and servers:
        first = next(iter(servers.items()))
        results.append(first)
    return results


def get_broadcast_snapshot(
    group_id: str, match: str = ""
) -> dict | None:
    """从广播缓存中取服务器快照（不 ping），供排行榜等展示用.

    Returns:
        {
            "name": str,
            "address": str,
            "status": "success" | "error",
            "data": {
                "players": [str, ...],
                "playtimes": {player_name: "Xh Xm", ...},
                "online_players": int,
            }
        }
        或 None（该群无广播服务器）
    """
    matched = _find_broadcast_servers(group_id, match)
    if not matched:
        return None

    addr, info = matched[0]
    return build_broadcast_snapshot(addr, info)
