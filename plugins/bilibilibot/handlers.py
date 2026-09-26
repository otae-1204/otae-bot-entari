from __future__ import annotations

import os

from arclet.alconna import Alconna, Args, MultiVar
from arclet.entari import Cleanup, listen
from nepattern import AnyString
from otae_bot.adapters.entari import close_scheduled_jobs, listen_message, on_ready, get_plaintext
from arclet.entari import Account as Bot, Event
from loguru import logger
from otae_bot.adapters.entari import Pred
from otae_bot.adapters.entari import (
    ArgVal,
    ChainMsg,
    SendDest,
    account_adapter_name,
    get_bot,
    make_image,
    on_alconna,
)

from plugins.bilibilibot.api import BiliApi
from plugins.bilibilibot.draw import draw_bili_card
from plugins.bilibilibot.models import BiliCard
from plugins.bilibilibot.notifier import Notifier, SenderUnavailable
from plugins.bilibilibot.poller import Poller
from plugins.bilibilibot.service import BiliService, expand_kinds
from plugins.bilibilibot.store import BiliStore


store = BiliStore()
client = BiliApi(
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


def _send_dest(subscriber_type: str, subscriber_id: str, bot: Bot) -> SendDest:
    adapter_name = account_adapter_name(bot)
    if subscriber_type == "group":
        return SendDest(subscriber_id, subscriber_id, True, False, "", adapter_name)
    return SendDest(subscriber_id, "", False, True, "", adapter_name)


async def _render(card: BiliCard) -> bytes:
    return await draw_bili_card(card)


async def _send(row, png: bytes) -> None:
    """Deliver one outbox row; raising SenderUnavailable leaves attempts intact."""
    try:
        bot = get_bot()
    except Exception as exc:
        raise SenderUnavailable(str(exc)) from exc
    if bot is None:
        raise SenderUnavailable("bot is not connected")
    destination = _send_dest(row.subscriber_type, row.subscriber_id, bot)
    # Bytes go straight to the adapter: no temporary file to clean up.
    await ChainMsg([make_image(raw=png)]).send(destination, bot)
    card = row.card()
    if card.card_type == "live_on" and card.url.strip():
        # Keep the image and the clickable link together per recipient.
        await ChainMsg.text(card.url.strip()).send(destination, bot)


notifier = Notifier(store, render=_render, send=_send)
poller = Poller(client, store, notifier)


@listen(Cleanup)
async def _close_bilibili():
    await close_scheduled_jobs()
    # Order matters: stop delivery first, then the transport, then the database.
    await notifier.stop()
    await client.aclose()
    await store.close()


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
            "/bili follow <all|live|video|dynamic> <UID> [更多UID]\n"
            "/bili follow <all|live> room:<直播间号>    （或直接贴直播间链接）\n"
            "/bili unfollow <all|live|video|dynamic> <UID 或 room:直播间号>\n"
            "/bili list [all|live|video|dynamic]\n"
            "/bili refresh <all|live|video|dynamic> <UID 或 room:直播间号>\n"
            "提示：纯数字一律按 UID 处理；按直播间号操作请加 room: 前缀。"
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
            lines = await service.list_subscriptions(subscriber_type, subscriber_id, kind_arg)
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
        card = await service.preview_link(text)
        if card is None:
            return
        await ChainMsg([await service.card_to_segment(card)]).send()
    except Exception as exc:
        logger.debug(f"[bilibilibot] link preview skipped: {exc}")


async def _startup():
    try:
        # The store must be open before the poller or notifier touch it.
        await store.open()
    except Exception as exc:
        logger.exception(f"[bilibilibot] store open failed: {exc}")
        return
    try:
        await client.ensure_risk_cookies()
        await client.ensure_wbi_keys()
    except Exception as exc:
        logger.warning(f"[bilibilibot] WBI warmup failed: {exc}")
    # start() first drains whatever the previous run left in the outbox.
    await notifier.start()


on_ready(_startup)

from otae_bot.adapters.entari import timer


timer.add_job(poller.tick_live, "interval", minutes=1, id="bili_live_check", replace_existing=True, misfire_grace_time=90)
timer.add_job(poller.tick_video, "interval", minutes=2, id="bili_video_check", replace_existing=True, misfire_grace_time=90)
timer.add_job(poller.tick_dynamic, "interval", minutes=1, id="bili_dynamic_check", replace_existing=True, misfire_grace_time=90)
timer.add_job(client.refresh_wbi_keys, "interval", hours=1, id="bili_wbi_refresh", replace_existing=True, misfire_grace_time=90)
timer.add_job(client.refresh_risk_cookies, "interval", hours=6, id="bili_risk_cookie_refresh", replace_existing=True, misfire_grace_time=90)
