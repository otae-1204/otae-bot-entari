"""远程屏幕窥视插件 - 查询远程服务器桌面截图。"""

import tempfile
from pathlib import Path

import httpx
from arclet.alconna import Alconna, Args
from arclet.entari import Event, Image, MessageChain, Session, Text, command
from arclet.entari.command import Match

from otae_bot.infrastructure.http.tls import ashared_ssl_context
from otae_bot.infrastructure.rendering.temp_files import schedule_temp_file_cleanup
from otae_bot.config.settings import Config as GlobalConfig
from otae_bot.messaging.inbound import event_user_id
from plugins.peek.store import PeekState

state = PeekState()
owner = GlobalConfig.SUPERUSERS
white_list = state.data.get("white_list", [])

screen_end = "/screenshot"
status_end = "/status"

# 命令注册

peek = Alconna(["peek", "窥视"], Args["target?", str])
add_peek = Alconna(["add_peek", "添加窥视"], Args["nickname", str]["peek_http_path", str])
add_whitelist = Alconna(["add_whitelist", "添加白名单", "add_whitelist_group"], Args["group_id", str])
del_peek = Alconna(["del_peek", "删除窥视"], Args["nickname", str])
set_def_peek = Alconna(["set_default_peek", "设置默认窥视"], Args["nickname", str])

# 辅助

def _get_group_id(event: Event) -> str:
    """跨协议获取群/频道 ID。"""
    guild = getattr(event, "guild", None)
    channel = getattr(event, "channel", None)
    if guild and guild.id:
        return str(guild.id)
    if channel and channel.id:
        return str(channel.id)
    return ""


def _join_endpoint(base_url: str, endpoint: str) -> str:
    return base_url.rstrip("/") + endpoint


def _image_segment_from_bytes(content: bytes):
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(content)
        f.flush()
        schedule_temp_file_cleanup(f.name)
        return Image.of(path=Path(f.name))


# helpers

@command.on(peek)
async def handle_peek(
    session: Session,
    target: Match[str],
):
    event = session.event
    group_id = _get_group_id(event)
    if not group_id:
        await session.send("该指令仅支持在群聊或频道中使用。")
        return

    if group_id not in white_list:
        return

    command_args = target.result.strip() if target.available else ""

    if command_args:
        peek_list = state.data.get(group_id, {}).get("peek_list", {})
        if command_args in peek_list:
            webpath = peek_list[command_args]
            nickname = command_args
        else:
            await session.send(f"未找到名为「{command_args}」的监控目标。")
            return
    else:
        def_peek_config = state.data.get(group_id, {}).get("def_peek_path", None)
        if def_peek_config is not None:
            webpath = def_peek_config.get("peek_http_path")
            nickname = def_peek_config.get("nickname")
        else:
            await session.send("当前群暂未配置默认 peek 地址。")
            return
        if webpath is None:
            await session.send("当前群未设置默认 peek 地址，请先添加可用地址。")
            return

    if not (webpath.startswith("http://") or webpath.startswith("https://")):
        await session.send("peek 地址格式不正确，须以 http:// 或 https:// 开头。")
        return

    screen_url = _join_endpoint(webpath, screen_end)

    try:
        async with httpx.AsyncClient(timeout=5, verify=await ashared_ssl_context()) as client:
            response = await client.get(screen_url)
            response.raise_for_status()
    except httpx.HTTPError:
        await session.send("获取屏幕截图失败，目标服务可能未启动。")
        return

    if response.status_code == 200:
        await session.send(MessageChain([
            Text(f"Screenshot for {nickname}"),
            _image_segment_from_bytes(response.content),
        ]))
    else:
        await session.send("获取屏幕截图失败，目标服务可能未启动。")


# list

@command.on(add_whitelist)
async def handle_add_whitelist(
    session: Session,
    group_id: Match[str],
):
    event = session.event
    user_id = str(event_user_id(event))
    if user_id not in owner:
        await session.send("权限不足，无法执行该操作。")
        return

    gid = group_id.result.strip() if group_id.available else ""
    if not gid:
        await session.send("请输入待添加的群号。")
        return

    if gid not in white_list:
        white_list.append(gid)
        state.data["white_list"] = white_list
        state.save()
        await session.send(f"群号 {gid} 已加入白名单。")
        return
    else:
        await session.send(f"群号 {gid} 已在白名单列表中。")


# helpers

@command.on(add_peek)
async def handle_add_peek(
    session: Session,
    nickname: Match[str],
    peek_http_path: Match[str],
):
    event = session.event
    user_id = str(event_user_id(event))
    if user_id not in owner:
        await session.send("权限不足，无法执行该操作。")
        return

    nick = nickname.result.strip() if nickname.available else ""
    path = peek_http_path.result.strip() if peek_http_path.available else ""

    if not nick or not path:
        await session.send("格式：/add_peek <昵称> <http地址>")
        return

    group_id = _get_group_id(event)
    if not group_id:
        await session.send("该指令仅支持在群聊或频道中使用。")
        return

    if nick in state.data.get(group_id, {}).get("peek_list", {}):
        await session.send(f"目标昵称「{nick}」已存在。")
        return

    if not (path.startswith("http://") or path.startswith("https://")):
        await session.send("请输入有效的 peek 地址。")
        return

    try:
        async with httpx.AsyncClient(timeout=5, verify=await ashared_ssl_context()) as client:
            resp = await client.get(_join_endpoint(path, screen_end))
            resp.raise_for_status()
    except httpx.HTTPError:
        await session.send(f"无法连通目标地址：{path}")
        return

    if group_id not in state.data:
        state.data[group_id] = {"peek_list": {}}
    peek_list = state.data[group_id].get("peek_list", {})

    add_msg = ""
    if state.data.get(group_id, {}).get("def_peek_path") is None:
        state.data[group_id]["def_peek_path"] = {
            "nickname": nick, "peek_http_path": path
        }
        add_msg = "\n已同步设为默认 peek 地址。"

    peek_list[nick] = path
    state.data[group_id]["peek_list"] = peek_list
    state.save()
    await session.send(f"已成功添加 {nick}: {path}" + add_msg)


# helpers

@command.on(del_peek)
async def handle_del_peek(
    session: Session,
    nickname: Match[str],
):
    event = session.event
    user_id = str(event_user_id(event))
    if user_id not in owner:
        await session.send("权限不足，无法执行该操作。")
        return

    nick = nickname.result.strip() if nickname.available else ""
    if not nick:
        await session.send("请输入待删除的目标昵称。")
        return

    group_id = _get_group_id(event)
    if not group_id:
        await session.send("该指令仅支持在群聊或频道中使用。")
        return

    if group_id not in state.data:
        await session.send(f"群组 {group_id} 尚未配置任何目标。")
        return

    peek_list = state.data.get(group_id, {}).get("peek_list", {})
    if nick not in peek_list:
        await session.send(f"目标昵称「{nick}」不存在。")
        return

    del peek_list[nick]
    state.data[group_id]["peek_list"] = peek_list

    def_peek = state.data.get(group_id, {}).get("def_peek_path")
    if def_peek and def_peek.get("nickname") == nick:
        del state.data[group_id]["def_peek_path"]
        state.save()
        await session.send(f"已移除 {nick}（包含默认地址）。")
        return
    else:
        state.save()
        await session.send(f"已移除 {nick}。")


# helpers

@command.on(set_def_peek)
async def handle_set_def_peek(
    session: Session,
    nickname: Match[str],
):
    event = session.event
    user_id = str(event_user_id(event))
    if user_id not in owner:
        await session.send("权限不足，无法执行该操作。")
        return

    nick = nickname.result.strip() if nickname.available else ""
    if not nick:
        await session.send("格式：/set_default_peek <昵称>")
        return

    group_id = _get_group_id(event)
    if not group_id:
        await session.send("该指令仅支持在群聊或频道中使用。")
        return

    peek_list = state.data.get(group_id, {}).get("peek_list", {})
    if nick not in peek_list:
        await session.send(f"目标昵称「{nick}」不存在。")
        return

    state.data[group_id]["def_peek_path"] = {
        "nickname": nick, "peek_http_path": peek_list[nick]
    }
    state.save()
    await session.send(f"已将默认 peek 目标切换为 {nick}。")
