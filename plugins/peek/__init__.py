"""远程屏幕窥视插件 - 查询远程服务器桌面截图。"""

import io
import tempfile

import httpx
from arclet.alconna import Args
from utils.entari_native import (
    ArgVal, ChainMsg, Text, event_user_id, make_image,
)
from arclet.entari import Account as Bot, Event

from utils.entari_native import cmd_with_args as _cmd
from utils.temp_files import schedule_temp_file_cleanup
from configs.config import Plugin_Config, Config as GlobalConfig

config = Plugin_Config("peek")
owner = GlobalConfig.SUPERUSERS
white_list = config.plugin_content.get("white_list", [])

screen_end = "/screenshot"
status_end = "/status"

# 命令注册

peek = _cmd("peek", args=Args["target?", str], aliases={"窥视"}, priority=5, block=True)
add_peek = _cmd("add_peek", args=Args["nickname", str]["peek_http_path", str], aliases={"添加窥视"}, priority=5, block=True)
add_whitelist = _cmd("add_whitelist", args=Args["group_id", str], aliases={"添加白名单", "add_whitelist_group"}, priority=5, block=True)
del_peek = _cmd("del_peek", args=Args["nickname", str], aliases={"删除窥视"}, priority=5, block=True)
set_def_peek = _cmd("set_default_peek", args=Args["nickname", str], aliases={"设置默认窥视"}, priority=5, block=True)

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
        return make_image(path=f.name)


# helpers

@peek.handle()
async def handle_peek(
    event: Event,
    bot: Bot,
    target: ArgVal[str],
):
    group_id = _get_group_id(event)
    if not group_id:
        await peek.finish("该命令仅在群聊/频道中可用")

    if group_id not in white_list:
        return

    command_args = target.result.strip() if target.available else ""

    if command_args:
        peek_list = config.plugin_content.get(group_id, {}).get("peek_list", {})
        if command_args in peek_list:
            webpath = peek_list[command_args]
            nickname = command_args
        else:
            await peek.finish(f"未找到名为 '{command_args}' 的目标")
    else:
        def_peek_config = config.plugin_content.get(group_id, {}).get("def_peek_path", None)
        if def_peek_config is not None:
            webpath = def_peek_config.get("peek_http_path")
            nickname = def_peek_config.get("nickname")
        else:
            await peek.finish("当前群组没有设置默认 peek 地址")
        if webpath is None:
            await peek.finish("当前群组没有设置默认 peek 地址，请添加新的 peek 地址")

    if not (webpath.startswith("http://") or webpath.startswith("https://")):
        await peek.finish("请提供正确的 peek 地址，必须以 http:// 或 https:// 开头")

    screen_url = _join_endpoint(webpath, screen_end)

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(screen_url)
            response.raise_for_status()
    except httpx.HTTPError:
        await peek.finish("获取图片失败，可能是目标没有启动服务")

    if response.status_code == 200:
        await ChainMsg([
            Text(f"Screenshot for {nickname}"),
            _image_segment_from_bytes(response.content),
        ]).finish()
    else:
        await peek.finish("获取图片失败，可能是目标没有启动服务")


# list

@add_whitelist.handle()
async def handle_add_whitelist(
    event: Event,
    group_id: ArgVal[str],
):
    user_id = str(event_user_id(event))
    if user_id not in owner:
        await add_whitelist.finish("你没有权限执行此操作")

    gid = group_id.result.strip() if group_id.available else ""
    if not gid:
        await add_whitelist.finish("请提供要添加的群号")

    if gid not in white_list:
        white_list.append(gid)
        config.plugin_content["white_list"] = white_list
        config.update()
        await add_whitelist.finish(f"群号 {gid} 已添加到白名单")
    else:
        await add_whitelist.finish(f"群号 {gid} 已在白名单中")


# helpers

@add_peek.handle()
async def handle_add_peek(
    event: Event,
    nickname: ArgVal[str],
    peek_http_path: ArgVal[str],
):
    user_id = str(event_user_id(event))
    if user_id not in owner:
        await add_peek.finish("你没有权限执行此操作")

    nick = nickname.result.strip() if nickname.available else ""
    path = peek_http_path.result.strip() if peek_http_path.available else ""

    if not nick or not path:
        await add_peek.finish("用法: /add_peek <昵称> <http地址>")

    group_id = _get_group_id(event)
    if not group_id:
        await add_peek.finish("该命令仅在群聊/频道中可用")

    if nick in config.plugin_content.get(group_id, {}).get("peek_list", {}):
        await add_peek.finish(f"昵称 {nick} 已存在")

    if not (path.startswith("http://") or path.startswith("https://")):
        await add_peek.finish("请提供正确的 peek 地址")

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(_join_endpoint(path, screen_end))
            resp.raise_for_status()
    except httpx.HTTPError:
        await add_peek.finish(f"无法访问: {path}")

    if group_id not in config.plugin_content:
        config.plugin_content[group_id] = {"peek_list": {}}
    peek_list = config.plugin_content[group_id].get("peek_list", {})

    add_msg = ""
    if config.plugin_content.get(group_id, {}).get("def_peek_path") is None:
        config.plugin_content[group_id]["def_peek_path"] = {
            "nickname": nick, "peek_http_path": path
        }
        add_msg = "\n已设为默认 peek 地址"

    peek_list[nick] = path
    config.plugin_content[group_id]["peek_list"] = peek_list
    config.update()
    await add_peek.finish(f"已添加 {nick}: {path}" + add_msg)


# helpers

@del_peek.handle()
async def handle_del_peek(
    event: Event,
    nickname: ArgVal[str],
):
    user_id = str(event_user_id(event))
    if user_id not in owner:
        await del_peek.finish("你没有权限执行此操作")

    nick = nickname.result.strip() if nickname.available else ""
    if not nick:
        await del_peek.finish("请提供要删除的昵称")

    group_id = _get_group_id(event)
    if not group_id:
        await del_peek.finish("该命令仅在群聊/频道中可用")

    if group_id not in config.plugin_content:
        await del_peek.finish(f"群组 {group_id} 没有配置")

    peek_list = config.plugin_content.get(group_id, {}).get("peek_list", {})
    if nick not in peek_list:
        await del_peek.finish(f"昵称 {nick} 不存在")

    del peek_list[nick]
    config.plugin_content[group_id]["peek_list"] = peek_list

    def_peek = config.plugin_content.get(group_id, {}).get("def_peek_path")
    if def_peek and def_peek.get("nickname") == nick:
        del config.plugin_content[group_id]["def_peek_path"]
        config.update()
        await del_peek.finish(f"已删除 {nick}（含默认地址）")
    else:
        config.update()
        await del_peek.finish(f"已删除 {nick}")


# helpers

@set_def_peek.handle()
async def handle_set_def_peek(
    event: Event,
    nickname: ArgVal[str],
):
    user_id = str(event_user_id(event))
    if user_id not in owner:
        await set_def_peek.finish("你没有权限执行此操作")

    nick = nickname.result.strip() if nickname.available else ""
    if not nick:
        await set_def_peek.finish("用法: /set_default_peek <昵称>")

    group_id = _get_group_id(event)
    if not group_id:
        await set_def_peek.finish("该命令仅在群聊/频道中可用")

    peek_list = config.plugin_content.get(group_id, {}).get("peek_list", {})
    if nick not in peek_list:
        await set_def_peek.finish(f"昵称 {nick} 不存在")

    config.plugin_content[group_id]["def_peek_path"] = {
        "nickname": nick, "peek_http_path": peek_list[nick]
    }
    config.update()
    await set_def_peek.finish(f"已将默认 peek 设置为 {nick}")

