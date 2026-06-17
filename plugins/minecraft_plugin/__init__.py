"""Minecraft 服务器 Ping 插件 - Alconna 版本，前缀与跨平台兼容。"""

import asyncio
import base64
import tempfile
from typing import Dict, Optional, Any

from arclet.entari import Account as Bot, Event
from utils.entari_native import (
    ChainMsg, Text, make_image as ChainImage, ArgVal, event_user_id, on_ready,
)
from arclet.alconna import Alconna, Args

from utils.entari_native import cmd as _cmd, get_rest
from utils.temp_files import schedule_temp_file_cleanup

from configs.config import Config
from plugins.minecraft_plugin.data_source import MinecraftDataManager, BroadcastManager
from plugins.minecraft_plugin.draw import draw_server_info, draw_server_list, draw_server_players, draw_player_leaderboard
from plugins.minecraft_plugin.broadcast import (
    init_broadcast, broadcast, safe_ping, _check_player_changes_simple,
    get_broadcast_snapshot, get_player_online_times,
)
from .ping import ping
from utils.entari_native import on_alconna



data_manager = MinecraftDataManager()
broadcast_manager = BroadcastManager(data_manager)

# 广播播报子系统
@on_ready
async def _on_bot_connect():
    await init_broadcast(broadcast_manager)


# helpers

def _get_group_id(event: Event) -> str:
    guild = getattr(event, "guild", None)
    return str(guild.id) if (guild and guild.id) else str(event_user_id(event))


def _to_image(output) -> ChainImage:
    """将 BytesIO 写入临时文件，返回 Satori 兼容的 Image 段。"""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(output.getvalue())
        f.flush()
        schedule_temp_file_cleanup(f.name)
        return ChainImage(path=f.name)


# 命令定义，用户消息仍使用 entari.yml 中配置的 / 前缀



server_ping = _cmd("ping", aliases={"Ping", "PING", "p", "P"}, priority=5, block=True)
add_server = _cmd("addserver", aliases={"Addserver", "add_server", "as", "添加服务器"}, priority=5, block=True)
add_server_nickname = _cmd("addservernickname", aliases={"asnn", "asn", "添加服务器昵称"}, priority=5, block=True)
remove_server = _cmd("removeserver", aliases={"Removeserver", "remove_server", "rs", "delserver", "del_server", "ds", "删除服务器"}, priority=5, block=True)
remove_server_nickname = _cmd("removeservernickname", aliases={"rsnn", "rsn", "删除服务器昵称"}, priority=5, block=True)
update_server_nickname = _cmd("updateservername", aliases={
    "Updateservername", "update_server_name", "usn", "USN",
    "update_name", "updatename", "un", "editname", "edit_name",
    "en", "修改服务器名称",
}, priority=5, block=True)
update_server_address = _cmd("updateserveraddress", aliases={
    "Updateserveraddress", "update_server_address", "usa", "USA",
    "updateaddress", "update_address", "ua", "UA",
    "editaddress", "edit_address", "ea", "修改服务器地址"}, priority=5, block=True)
ping_list = _cmd("pinglist", aliases={"Pinglist", "ping_list", "pl", "PL", "服务器列表"}, priority=5, block=True)
add_broadcast = _cmd("addbroadcast", aliases={"Addbroadcast", "add_broadcast", "ab", "添加播报服务器"}, priority=5, block=True)
remove_broadcast = _cmd("removebroadcast", aliases={"Removebroadcast", "remove_broadcast", "rb", "删除播报服务器"}, priority=5, block=True)
update_broadcast_address = _cmd("updatebroadcastaddress", aliases={
    "Updatebroadcastaddress", "update_broadcast_address", "uba", "UBA",
    "updatebroadcast", "Updatebroadcast", "ub", "UB",
    "editbroadcastaddress", "edit_broadcast_address", "eba", "修改播报服务器地址"}, priority=5, block=True)
update_broadcast_name = _cmd("updatebroadcastname", aliases={
    "Updatebroadcastname", "update_broadcast_name", "ubn", "UBN",
    "updatebroadcastnickname", "ubnn",
    "editbroadcastname", "edit_broadcast_name", "ebn", "修改播报服务器名称",
}, priority=5, block=True)
broadcast_list = _cmd("broadcastlist", aliases={"Broadcastlist", "broadcast_list", "bl", "BL", "播报服务器列表"}, priority=5, block=True)
online_rank = _cmd("online", aliases={"Online", "ol", "在线", "排行榜", "rank"}, priority=5, block=True)



# 鍛戒护澶勭悊

# helpers

@server_ping.handle()
async def handle_ping_command(event: Event, rest: ArgVal[str]):
    command_args = get_rest(rest)
    group_id = _get_group_id(event)

    if not command_args:
        server_list = data_manager.get_group_serverlist(group_id) or []
        if not server_list:
            await server_ping.finish("当前群没有保存的服务器，请使用 /addserver 添加。")

        async def _ping_one(s):
            r = await ping(s.get("address"), "java")
            return {"status": r.get("status"), "address": s.get("address"),
                    "data": r.get("data", {}), "name": s.get("name", "未知"),
                    "nickname": s.get("nickname")}

        responses = await asyncio.gather(*[_ping_one(s) for s in server_list])
        if len(responses) == 1:
            # draw_server_players 会处理离线状态
            output = draw_server_players(responses[0])
            await server_ping.finish(ChainMsg([_to_image(output)]))
            return

        # 多个服务器全部传入，draw_server_info 会处理离线状态
        imgs = [draw_server_info(r) for r in responses]
        output = draw_server_list(imgs, "群聊")
        await server_ping.finish(ChainMsg([_to_image(output)]))
        return

    # 按地址或昵称查找
    server = data_manager.get_server_by_name(group_id, command_args)
    if not server:
        server = data_manager.get_server_by_nickname(group_id, command_args)

    if server:
        r = await ping(server.get("address"), "java")
        info = {"status": r.get("status"), "address": server.get("address"),
                "data": r.get("data", {}), "name": server.get("name", "未知"),
                "nickname": server.get("nickname")}
        output = draw_server_players(info)
        await server_ping.finish(ChainMsg([_to_image(output)]))
        return

    # 鐩存帴鎸夊湴鍧€ ping
    r = await ping(command_args, "java")
    info = {"status": r.get("status"), "address": command_args,
            "data": r.get("data", {}), "name": "Unknown Server", "nickname": None}
    if r.get("status") == "success":
        output = draw_server_players(info)
    else:
        output = draw_server_list([draw_server_info(info)], "Ping")
    await server_ping.finish(ChainMsg([_to_image(output)]))


# ── add_server

@add_server.handle()
async def handle_add_server_command(event: Event, rest: ArgVal[str]):
    command_args = get_rest(rest)
    group_id = _get_group_id(event)

    if not command_args or " " not in command_args:
        await add_server.finish("指令格式错误，请使用 /addserver <服务器昵称> <服务器地址>")

    parts = command_args.split(" ", 1)
    if len(parts) < 2:
        await add_server.finish("指令格式错误，请使用 /addserver <服务器昵称> <服务器地址>")

    server_name, server_address = parts[0], parts[1]
    result = data_manager.add_group_server(group_id, server_name, server_address)

    if result:
        await add_server.finish(f"已添加服务器: {server_name} ({server_address})")
    else:
        await add_server.finish("添加失败: 服务器已存在或数据错误")


# add_server_nickname

@add_server_nickname.handle()
async def handle_add_server_nickname_command(event: Event, rest: ArgVal[str]):
    command_args = get_rest(rest)
    group_id = _get_group_id(event)

    if not command_args or " " not in command_args:
        await add_server_nickname.finish("指令格式错误，请使用 /addservernickname <服务器名称> <服务器昵称>")

    parts = command_args.split(" ", 1)
    result = data_manager.update_server_nickname(group_id, parts[0], parts[1])
    if result:
        await add_server_nickname.finish(f"已为服务器 {parts[0]} 添加昵称: {parts[1]}")
    else:
        await add_server_nickname.finish("添加昵称失败，请检查服务器名称是否正确。")


# remove_server

@remove_server.handle()
async def handle_remove_server_command(event: Event, rest: ArgVal[str]):
    command_args = get_rest(rest)
    group_id = _get_group_id(event)

    if not command_args:
        await remove_server.finish("指令格式错误，请使用 /removeserver <服务器名称>")

    result = data_manager.remove_group_server(group_id, command_args)
    if result:
        await remove_server.finish(f"已删除服务器: {command_args}")
    else:
        await remove_server.finish("删除失败，请检查服务器名称。")


# remove_server_nickname

@remove_server_nickname.handle()
async def handle_remove_server_nickname_command(event: Event, rest: ArgVal[str]):
    command_args = get_rest(rest)
    group_id = _get_group_id(event)

    if not command_args:
        await remove_server_nickname.finish("指令格式错误，请使用 /removeservernickname <服务器名称>")

    result = data_manager.remove_server_nickname(group_id, command_args)
    if result:
        await remove_server_nickname.finish(f"已删除服务器 {command_args} 的昵称")
    else:
        await remove_server_nickname.finish("删除失败。")


# update_server_nickname

@update_server_nickname.handle()
async def handle_update_server_nickname_command(event: Event, rest: ArgVal[str]):
    command_args = get_rest(rest)
    group_id = _get_group_id(event)

    if not command_args or " " not in command_args:
        await update_server_nickname.finish("指令格式错误，请使用 /updateservername <服务器名称> <新名称>")

    parts = command_args.split(" ", 1)
    result = data_manager.update_server_name(group_id, parts[0], parts[1])
    if result:
        await update_server_nickname.finish("已更新服务器名称。")
    else:
        await update_server_nickname.finish("更新失败。")


# update_server_address

@update_server_address.handle()
async def handle_update_server_address_command(event: Event, rest: ArgVal[str]):
    command_args = get_rest(rest)
    group_id = _get_group_id(event)

    if not command_args or " " not in command_args:
        await update_server_address.finish("指令格式错误，请使用 /updateserveraddress <服务器名称> <新地址>")

    parts = command_args.split(" ", 1)
    success = data_manager.update_server_address(group_id, parts[0], parts[1])
    if success:
        await update_server_address.finish("已更新服务器地址。")
    else:
        await update_server_address.finish("更新失败。")


# ping_list

@ping_list.handle()
async def handle_ping_list_command(event: Event, rest: ArgVal[str]):
    group_id = _get_group_id(event)
    server_list = data_manager.get_group_serverlist(group_id) or []

    if not server_list:
        await ping_list.finish("当前群没有保存的服务器。")

    lines = ["当前群保存的服务器:"]
    for s in server_list:
        name = s.get("name", "未知")
        addr = s.get("address", "未知")
        nick = s.get("nickname", "")
        nick_str = f" ({nick})" if nick else ""
        lines.append(f"  {name}{nick_str} - {addr}")
    await ping_list.finish("\n".join(lines))


# add_broadcast

@add_broadcast.handle()
async def handle_add_broadcast_command(event: Event, rest: ArgVal[str]):
    command_args = get_rest(rest)
    group_id = _get_group_id(event)

    if not command_args:
        await add_broadcast.finish("指令格式错误，请使用 /addbroadcast <服务器名称> <服务器地址>\n或 /addbroadcast <已保存的服务器名称>")

    parts = command_args.split(" ")
    server_name = parts[0]
    server_address = " ".join(parts[1:]) if len(parts) > 1 else ""

    # 先查本地已有服务器
    existing = data_manager.get_server_by_name(group_id, server_name)
    if not existing:
        existing = data_manager.get_server_by_nickname(group_id, server_name)

    if existing:
        await broadcast_manager.add_broadcast_server_by_name(group_id, existing["name"], server_name)
        await add_broadcast.finish(f"已将 {existing['name']} 加入播报列表")
    elif server_address:
        await broadcast_manager.add_broadcast_server_by_address(group_id, server_address, server_name)
        await add_broadcast.finish(f"已添加 {server_name} ({server_address}) 到播报列表")
    else:
        await add_broadcast.finish(f"服务器 {server_name} 未在本地找到，请同时提供地址: /addbroadcast <名称> <地址>")


# remove_broadcast

@remove_broadcast.handle()
async def handle_remove_broadcast_command(event: Event, rest: ArgVal[str]):
    command_args = get_rest(rest)
    group_id = _get_group_id(event)

    if not command_args:
        await remove_broadcast.finish("指令格式错误，请使用 /removebroadcast <服务器名称>")

    await broadcast_manager.remove_broadcast_server(group_id, command_args)
    await remove_broadcast.finish(f"已将 {command_args} 从播报列表中移除")


# update_broadcast_address

@update_broadcast_address.handle()
async def handle_update_broadcast_address_command(event: Event, rest: ArgVal[str]):
    command_args = get_rest(rest)
    group_id = _get_group_id(event)

    if not command_args or " " not in command_args:
        await update_broadcast_address.finish("指令格式错误，请使用 /updatebroadcastaddress <服务器名称> <新地址>")

    parts = command_args.split(" ", 1)
    await broadcast_manager.update_broadcast_server_address(group_id, parts[0], parts[1])
    await update_broadcast_address.finish(f"已更新播报服务器 {parts[0]} 的地址。")


# update_broadcast_name

@update_broadcast_name.handle()
async def handle_update_broadcast_name_command(event: Event, rest: ArgVal[str]):
    command_args = get_rest(rest)
    group_id = _get_group_id(event)

    if not command_args or " " not in command_args:
        await update_broadcast_name.finish("指令格式错误，请使用 /updatebroadcastname <服务器名称> <新的播报名称>")

    parts = command_args.split(" ", 1)
    await broadcast_manager.update_broadcast_server_name(group_id, parts[0], parts[1])
    await update_broadcast_name.finish("已更新播报服务器名称。")


# broadcast_list

@broadcast_list.handle()
async def handle_broadcast_list_command(event: Event, rest: ArgVal[str]):
    group_id = _get_group_id(event)
    servers = await broadcast_manager.get_group_broadcast_servers(group_id) or {}

    if not servers:
        await broadcast_list.finish("当前群没有播报服务器。")

    lines = ["当前群的播报服务器:"]
    for name, info in servers.items():
        addr = info.get("address", "未知") if isinstance(info, dict) else str(info)
        lines.append(f"  {name} - {addr}")
    await broadcast_list.finish("\n".join(lines))


# online 排行榜

@online_rank.handle()
async def handle_online_command(event: Event, rest: ArgVal[str]):
    """Show historical online-time leaderboard."""
    command_args = get_rest(rest)
    group_id = _get_group_id(event)
    gid_int = int(group_id)

    # 解析 -a 参数
    show_all = False
    if command_args.endswith(" -a"):
        show_all = True
        command_args = command_args[:-3].strip()
    limit = 0 if show_all else 10

    if command_args:
        server = data_manager.get_server_by_identifier(group_id, command_args)
        if not server:
            await online_rank.finish(f"未找到服务器: {command_args}")
            return
        top_players = data_manager.get_top_players(gid_int, server["address"], limit=limit)
        total_count = len(data_manager.get_server_player_gametimes(group_id, server["address"]))
        server_name = server["name"]
        addr = server["address"]
        # 尝试 ping 获取 favicon/version，离线也不影响历史排行榜
        try:
            r = await ping(addr, "java")
            _rdata = r.get("data", {})
            if not isinstance(_rdata, dict):
                _rdata = {}
            favicon = _rdata.get("favicon")
            version = _rdata.get("game_version", "")
            max_players = _rdata.get("max_players", 0)
        except Exception:
            favicon, version, max_players = None, "", 0
    else:
        top_players = data_manager.get_top_players_all_servers(gid_int, limit=limit)
        total_count = len(data_manager.get_all_servers_player_gametimes(group_id))
        guild = getattr(event, 'guild', None)
        server_name = "全部服务器"
        addr = ""
        version = ""
        max_players = 0
        favicon = None
        if guild:
            try:
                bot = get_bot()
                full_guild = await bot.guild_get(guild_id=str(guild.id))
                server_name = full_guild.name or server_name
                avatar_url = full_guild.avatar
                if avatar_url:
                    data = await bot.download(avatar_url)
                    favicon = base64.b64encode(data).decode()
            except Exception:
                server_name = guild.name or server_name
        if len(server_name) > 20:
            server_name = server_name[:19] + "..."

    if not top_players:
        await online_rank.finish("暂无玩家游戏时长数据。\n玩家数据会在离开服务器时自动记录。")
        return

    player_names = [p["player_id"] for p in top_players]
    playtimes = {p["player_id"]: p["formatted_time"] for p in top_players}

    server_info = {
        "name": server_name,
        "address": addr,
        "status": "success",
        "data": {
            "players": player_names,
            "playtimes": playtimes,
            "online_players": total_count,
            "max_players": max_players,
            "favicon": favicon,
            "game_version": version,
            "latency": 0,
        },
    }

    output = draw_player_leaderboard(server_info)
    await online_rank.finish(ChainMsg([_to_image(output)]))

