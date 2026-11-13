"""Minecraft 服务器广播子系统 — 定时轮询 + 玩家变化检测 + 消息播报."""

from __future__ import annotations

import asyncio
import time
import traceback

from arclet.entari import MessageChain, Session, Text, listen
from arclet.entari.event.base import GuildMemberAddedEvent
from arclet.letoderea import ExitState
from loguru import logger

from otae_bot.accounts import runtime
from otae_bot.config.settings import Config
from otae_bot.delivery import FAILED as DELIVERY_FAILED
from otae_bot.delivery import NO_ACCOUNT as DELIVERY_NO_ACCOUNT
from otae_bot.delivery import SENT as DELIVERY_SENT
from otae_bot.delivery import Target, deliver
from otae_bot.scheduling.jobs import timer
from .config import minecraft_settings
from .data_source import PlayerGameTimeManager
from .ping import ping
from .broadcast_utils import (
    build_broadcast_snapshot,
    build_player_change_messages,
    group_errors_by_group,
    should_send_error_digest,
)

MC_BROADCAST_INTERVAL = minecraft_settings().broadcast_interval

superuser = str(Config.SUPERUSERS[0]) if Config.SUPERUSERS else "2461673400"

# 全局广播缓存: {group_id: {server_name: {"address": str, "players": dict|None}}}
__BroadcastInfo: dict = {}
_broadcast_lock = asyncio.Lock()

# 被踢出的群（暂停广播，保留配置，等 bot 重新入群后恢复）
_kicked_groups: set[str] = set()
_last_error_digest_at: dict[str, int] = {}
ERROR_DIGEST_COOLDOWN = 600
BROADCAST_TICK_SECONDS = 30
_last_broadcast_at: dict[str, int] = {}


# ── 监听 bot 重新入群 → 清除踢出标记 ──

def _is_bot_added(account, event) -> bool:
    # 收到事件的账号自己入群才算，不与“最近登录”的账号比对。
    user_id = str(getattr(getattr(event, "user", None), "id", "") or "")
    return bool(user_id) and user_id == runtime.account_id(account)


async def _clear_kick_mark(session: Session):
    event = session.event
    if not _is_bot_added(session.account, event):
        return
    guild_id = str(event.guild.id) if event.guild else ""
    if guild_id:
        _kicked_groups.discard(guild_id)
        logger.info(f"Bot 重新加入群 {guild_id}，恢复广播")


listen(GuildMemberAddedEvent)(_clear_kick_mark)


def _text(text: str) -> MessageChain:
    return MessageChain([Text(text)])


async def safe_ping(server_address: str, server_type: str = "java") -> dict:
    """安全的 ping 包装，统一返回格式."""
    try:
        return await ping(server_address, server_type)
    except (ExitState, asyncio.CancelledError):
        raise
    except Exception as exc:
        return {"status": "error", "data": {}, "error": str(exc)}


def _check_player_changes_simple(
    server_name: str,
    previous_players: dict,
    current_players: list[str] | None,
    group_id: int,
    server_identifier: str | None = None,
) -> list:
    """对比新旧玩家列表，生成变化消息."""
    messages, playtime_deltas = build_player_change_messages(
        server_name, previous_players, current_players, int(time.time())
    )
    data_manager = getattr(_bm, "data_manager", None) if _bm is not None else None
    player_game_time_manager = PlayerGameTimeManager(data_manager)
    identifier = server_identifier or server_name
    for player, duration in playtime_deltas.items():
        player_game_time_manager.add_player_gametime(
            player_name=player, group_id=group_id,
            server_name=identifier, gametime=duration,
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


def _build_broadcast_cache_entry(addr: str, name: str, ping_result: dict, now: int) -> dict:
    """Build one in-memory broadcast cache entry from a ping result."""
    if ping_result.get("status") == "success":
        data = ping_result.get("data", {})
        if not isinstance(data, dict):
            data = {}
        player_list = data.get("players", [])
        players_hidden = bool(data.get("players_hidden"))
        online_players = int(data.get("online_players", 0) or 0)
    else:
        player_list = None
        players_hidden = False
        online_players = 0

    return {
        "name": name,
        "address": addr,
        "players": (
            {}
            if players_hidden
            else {p: now for p in player_list}
            if player_list is not None
            else None
        ),
        "players_hidden": players_hidden,
        "online_players": online_players,
    }


def _get_group_broadcast_interval(group_id: str) -> int:
    data_manager = getattr(_bm, "data_manager", None) if _bm is not None else None
    if data_manager is None:
        return MC_BROADCAST_INTERVAL
    return int(data_manager.get_group_broadcast_interval(group_id, MC_BROADCAST_INTERVAL))


def _is_group_broadcast_due(group_id: str, now: int) -> bool:
    gid = str(group_id)
    last = _last_broadcast_at.get(gid)
    if last is None:
        return True
    return now - last >= _get_group_broadcast_interval(gid)


def _mark_group_broadcasted(group_id: str, now: int) -> None:
    _last_broadcast_at[str(group_id)] = now


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
        group[addr] = _build_broadcast_cache_entry(addr, name, result, int(time.time()))
    logger.info(f"Broadcast server cache initialized: {len(entries)} servers")


async def _sync_server_list():
    """从 DB 同步服务器列表到缓存（增量更新：新增/移除服务器，不丢失当前玩家状态）."""
    if _bm is None:
        return
    try:
        db_servers = await _bm.get_all_broadcast_servers()
        new_entries: list[tuple[str, str, str]] = []
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
                        new_entries.append((gid, addr, si.get("name", addr)))
                    else:
                        __BroadcastInfo[gid][addr]["name"] = si.get("name", addr)
                        __BroadcastInfo[gid][addr]["address"] = addr
        if new_entries:
            now = int(time.time())
            results = await asyncio.gather(*[_ping_with_timeout(addr) for _, addr, _ in new_entries])
            async with _broadcast_lock:
                for (gid, addr, name), result in zip(new_entries, results):
                    if gid in __BroadcastInfo and addr not in __BroadcastInfo[gid]:
                        __BroadcastInfo[gid][addr] = _build_broadcast_cache_entry(addr, name, result, now)
    except (ExitState, asyncio.CancelledError):
        raise
    except Exception:
        pass


def _is_kicked_error(exc: BaseException) -> bool:
    """LLBot 报告账号已被移出群 / 群解散（含 content bytes 中的中文错误）."""
    err_msg = str(exc)
    if "已被移出该群" in err_msg or "已被移除" in err_msg or "群已解散" in err_msg:
        return True
    return "500" in err_msg and ("移出" in err_msg or "移除" in err_msg or "解散" in err_msg)


SENT, KICKED, NO_ACCOUNT, FAILED = "sent", "kicked", "no_account", "failed"


async def _send_group(group_id: str, text: str) -> tuple[str, BaseException | None]:
    """经投递服务发出一条播报；返回 (结果, 失败时的异常).

    不指定账号：deliver 只用 guild.list 里列出该群的在线账号（必要时刷新群
    列表），绝不交给“最近登录”的账号（它可能是别的平台，如 Ir）。某账号报告
    已被移出群时从它的群列表里去掉该群，换下一个在群账号；全部被移出才暂停该群。
    群功能被关闭时 deliver 不发，也算处理完毕。
    """
    gid = str(group_id)
    target = Target.group(gid)
    kicked = 0
    for _ in range(len(runtime.get_bots()) + 1):
        result = await deliver(target, _text(text))
        if result.status == DELIVERY_NO_ACCOUNT:
            if kicked:
                break
            online = ", ".join(runtime.account_id(bot) or "?" for bot in runtime.get_bots()) or "none"
            logger.warning(f"[MC_Server] 群 {gid} 没有在线账号在群内，本次播报未发出；在线账号：{online}")
            return NO_ACCOUNT, None
        sender_id = runtime.account_id(result.account)
        if result.status == DELIVERY_FAILED:
            exc = result.exception
            if exc is not None and _is_kicked_error(exc):
                runtime.forget_guild(result.account, gid)
                logger.warning(f"[MC_Server] 账号 {sender_id} 已不在群 {gid}（{type(exc).__name__}）")
                kicked += 1
                continue
            logger.warning(f"[MC_Server] 群 {gid} 播报发送失败：账号 {sender_id}，{type(exc).__name__}")
            return FAILED, exc
        if result.status == DELIVERY_SENT:
            logger.info(f"[MC_Server] 群 {gid} 播报已发送（{text.count(chr(10)) + 1} 行，账号 {sender_id}）")
        return SENT, None
    logger.warning(f"[MC_Server] 所有在线账号都已被移出群 {gid}，暂停该群广播")
    _kicked_groups.add(gid)
    return KICKED, None


def _digest_account(group_ids) -> object | None:
    """超级用户异常摘要的发送账号：按群号排序，第一个有在群账号的配置群所用的账号.

    这样摘要总由能发 QQ 群播报的账号私聊发出，不会落到别的平台的账号上。
    """
    for gid in sorted(str(item) for item in group_ids):
        bots = runtime.get_bots_for_guild(gid)
        if bots:
            return bots[0]
    return None


@timer.scheduled_job("interval", seconds=BROADCAST_TICK_SECONDS, misfire_grace_time=30, max_instances=1)
async def broadcast():
    """定时轮询所有服务器，检测玩家变化并播报（同时同步 DB 配置）."""
    await _sync_server_list()

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
        if not _is_group_broadcast_due(str(group_id), now):
            continue
        _mark_group_broadcasted(str(group_id), now)
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
                    _check_player_changes_simple(
                        server_name, previous_players, current_players, group_id, addr
                    )
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

            except (ExitState, asyncio.CancelledError):
                raise
            except Exception:
                errors.append(f"群 {group_id} 服务器 {addr}: {traceback.format_exc()}")

        if group_messages:
            try:
                outcome, failure = await _send_group(str(group_id), "\n".join(group_messages))
            except (ExitState, asyncio.CancelledError):
                raise
            except Exception as exc:
                logger.warning(f"[MC_Server] 群 {group_id} 播报发送失败：{type(exc).__name__}")
                outcome, failure = FAILED, exc
            if outcome == FAILED:
                detail = "".join(traceback.format_exception(failure)) if failure is not None else ""
                errors.append(f"群 {group_id} 发送消息失败: {detail}")
            elif outcome == NO_ACCOUNT:
                errors.append(f"群 {group_id} 发送消息失败: 没有在线账号在该群内")

    if errors:
        bot = _digest_account(snapshot)
        if bot is None:
            logger.warning(f"[MC_Server] 广播异常摘要未发送：没有可用的 QQ 账号（{len(errors)} 条异常）")
            return
        try:
            # 私聊发给超级用户：用这个账号，平台与它相同，不会落到别的平台。
            target = Target.private(superuser, self_id=runtime.account_id(bot), platform=getattr(bot, "platform", ""))
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
                suffix = f"\n已折叠 {suppressed} 条重复异常" if suppressed else ""
                result = await deliver(target, _text(
                    f"广播异常摘要（共 {len(lines)} 条）：\n" + "\n---\n".join(lines[-5:]) + suffix
                ), account=bot)
                result.raise_for_failure()
        except (ExitState, asyncio.CancelledError):
            raise
        except Exception as exc:
            logger.warning(
                f"[MC_Server] 广播异常摘要发送失败：账号 {runtime.account_id(bot)}，{type(exc).__name__}"
            )


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
