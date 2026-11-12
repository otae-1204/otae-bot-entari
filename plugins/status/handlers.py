"""``/status`` ``/状态``：群里人人可看的公开版；只有超管在私聊里发，才出带内部信息的详细版。

超管在群里发也只出公开版，详细信息不会因为超管在群里发了命令就漏出去。
import 期不读 ``.env``、不连网络、不注册定时任务。
"""

from __future__ import annotations

import asyncio

from arclet.entari import Session
from loguru import logger

from otae_bot.compat.entari import Image, get_group_id, send
from otae_bot.compat.entari import cmd as _cmd
from otae_bot.infrastructure.rendering.art_card import render_card
from otae_bot.permissions import is_superuser

from .collect import collect
from .presentation import build_card, format_text

status_cmd = _cmd("status", aliases={"状态"}, priority=5, block=True)


def wants_detail(event) -> bool:
    return is_superuser(event) and not get_group_id(event)


@status_cmd.handle()
async def handle_status(session: Session) -> None:
    detail = wants_detail(session.event)
    snapshot = await collect(detail=detail)
    try:
        png = await render_card(build_card(snapshot, detail=detail))
        await send(session, [Image(raw=png)])
        return
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - 图片边界必须保住文本
        logger.warning("[status] card delivery fell back to text: {}", type(error).__name__)
    await send(session, "\n".join(format_text(snapshot, detail=detail)))


__all__ = ["handle_status", "status_cmd", "wants_detail"]
