from __future__ import annotations

import os

from arclet.alconna import Alconna, Args, MultiVar
from nepattern import AnyString
from utils.entari_native import listen_message, on_ready, get_plaintext
from arclet.entari import Account as Bot, Event
from loguru import logger
from utils.entari_native import Pred
from utils.entari_native import ArgVal, ChainMsg, on_alconna

from .client import BiliAPIError, BiliClient
from .models import KIND_LIVE
from .service import BiliService, expand_kinds
from .store import BiliStore


store = BiliStore()
client = BiliClient(
    sessdata=os.getenv("BILI_SESSDATA", ""),
    buvid3=os.getenv("BILI_BUVID3", ""),
    dm_img_list=os.getenv("BILI_DM_IMG_LIST", ""),
    dm_img_str=os.getenv("BILI_DM_IMG_STR", ""),
    dm_cover_img_str=os.getenv("BILI_DM_COVER_IMG_STR", ""),
    rsshub_base_urls=[
        item.strip()
        for item in os.getenv("BILI_RSSHUB_BASE_URLS", "https://rsshub.app,https://rss.materium.io").split(",")
        if item.strip()
    ],
)
service = BiliService(store, client)


def _subscriber(event: Event) -> tuple[str, str]:
    guild = getattr(event, "guild", None)
    channel = getattr(event, "channel", None)
    if guild and getattr(guild, "id", None):
        return "group", str(guild.id)
    if channel and getattr(channel, "id", None):
        return "group", str(channel.id)
    return "user", str(getattr(getattr(event, "user", None), "id", ""))


def _rest(match: ArgVal) -> str:
    if not match.available:
        return ""
    val = match.result
    if isinstance(val, tuple):
        return " ".join(str(item) for item in val).strip()
    return str(val or "").strip()


def _parse_args(rest: str) -> list[str]:
    return [part for part in rest.split() if part]


async def _handle_result(title: str, ok: list[str], failed: list[str]):
    lines = [title]
    if ok:
        lines.append("成功:")
        lines.extend(f"- {item}" for item in ok)
    if failed:
        lines.append("失败:")
        lines.extend(f"- {item}" for item in failed)
    await ChainMsg.text("\n".join(lines)).finish()


bili_cmd = on_alconna(
    Alconna(["bili"], Args["rest;?", MultiVar(AnyString)]),
    priority=5,
    block=True,
)


@bili_cmd.handle()
async def handle_bili(event: Event, rest: ArgVal):
    parts = _parse_args(_rest(rest))
    if not parts or parts[0] == "help":
        await bili_cmd.finish(
            "用法:\n"
            "/bili follow <all|live|video|dynamic> <uid或直播间号> [更多id]\n"
            "/bili unfollow <all|live|video|dynamic> <uid或直播间号> [更多id]\n"
            "/bili list [all|live|video|dynamic]\n"
            "/bili refresh <all|live|video|dynamic> <uid或直播间号>"
        )

    action = parts[0].lower()
    subscriber_type, subscriber_id = _subscriber(event)

    if action in {"follow", "unfollow", "refresh"}:
        if len(parts) < 3:
            await bili_cmd.finish("参数不足，请使用 /bili help 查看用法")
        kind_arg = parts[1]
        values = parts[2:]
        try:
            expand_kinds(kind_arg)
        except ValueError:
            await bili_cmd.finish("类型必须是 all/live/video/dynamic")
        if action == "follow":
            ok, failed = await service.follow(kind_arg, values, subscriber_type, subscriber_id)
            await _handle_result("B站订阅结果", ok, failed)
        if action == "unfollow":
            ok, failed = await service.unfollow(kind_arg, values, subscriber_type, subscriber_id)
            await _handle_result("B站取关结果", ok, failed)
        ok, failed = await service.refresh(kind_arg, values)
        await _handle_result("B站刷新结果", ok, failed)

    if action == "list":
        kind_arg = parts[1] if len(parts) > 1 else None
        try:
            lines = service.list_subscriptions(subscriber_type, subscriber_id, kind_arg)
        except ValueError:
            await bili_cmd.finish("类型必须是 all/live/video/dynamic")
        await bili_cmd.finish("\n".join(lines))

    await bili_cmd.finish("未知子命令，请使用 /bili help 查看用法")


async def _has_bili_link(event: Event) -> bool:
    text = get_plaintext(event).strip()
    if not text or text.startswith("/"):
        return False
    return ("bilibili.com" in text or "b23.tv" in text or "BV" in text)


link_preview = listen_message(rule=Pred(_has_bili_link), priority=20, block=False)


@link_preview.handle()
async def handle_link_preview(bot: Bot, event: Event):
    text = get_plaintext(event)
    try:
        parsed = await client.parse_link(text)
        if parsed is None:
            return
        card = await client.card_for_link(parsed)
        await ChainMsg([await service.card_to_segment(card)]).send()
    except Exception as exc:
        logger.debug(f"[bilibilibot] link preview skipped: {exc}")


async def _warmup():
    try:
        await client.ensure_risk_cookies()
        await client.ensure_wbi_keys()
    except Exception as exc:
        logger.warning(f"[bilibilibot] WBI warmup failed: {exc}")


on_ready(_warmup)

from utils.entari_native import timer


timer.add_job(service.check_live, "interval", minutes=1, id="bili_live_check", replace_existing=True, misfire_grace_time=90)
timer.add_job(service.check_video, "interval", minutes=2, id="bili_video_check", replace_existing=True, misfire_grace_time=90)
timer.add_job(service.check_dynamic, "interval", minutes=1, id="bili_dynamic_check", replace_existing=True, misfire_grace_time=90)
timer.add_job(client.refresh_wbi_keys, "interval", hours=1, id="bili_wbi_refresh", replace_existing=True, misfire_grace_time=90)
timer.add_job(client.refresh_risk_cookies, "interval", hours=6, id="bili_risk_cookie_refresh", replace_existing=True, misfire_grace_time=90)
