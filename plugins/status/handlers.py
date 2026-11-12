"""``/status`` ``/状态``：群里人人可看的公开版；只有超管在私聊里发，才出带内部信息的详细版。

超管在群里发也只出公开版，详细信息不会因为超管在群里发了命令就漏出去。
import 期不读 ``.env``、不连网络、不注册定时任务。
"""

from __future__ import annotations

import asyncio

from arclet.alconna import Alconna, Args, MultiVar
from arclet.entari import MessageChain, Session, command
from loguru import logger
from nepattern import AnyString

from otae_bot.groups.superusers import is_superuser
from otae_bot.infrastructure.rendering.art_card import render_card
from otae_bot.messaging import media
from otae_bot.messaging.inbound import get_group_id

from .collect import collect
from .presentation import build_card, format_text

status_cmd = Alconna(["status", "状态"], Args["rest;?", MultiVar(AnyString)])


def wants_detail(event) -> bool:
    return is_superuser(event) and not get_group_id(event)


@command.on(status_cmd)
async def handle_status(session: Session) -> None:
    detail = wants_detail(session.event)
    snapshot = await collect(detail=detail)
    try:
        png = await render_card(build_card(snapshot, detail=detail))
        await session.send(MessageChain([media.png(png)]))
        return
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - 图片边界必须保住文本
        logger.warning("[status] card delivery fell back to text: {}", type(error).__name__)
    await session.send("\n".join(format_text(snapshot, detail=detail)))


__all__ = ["handle_status", "status_cmd", "wants_detail"]
