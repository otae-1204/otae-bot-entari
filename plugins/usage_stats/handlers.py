"""``/指令统计`` ``/统计``：默认看本群近 7 天，私聊默认看全局；``全局`` 参数任何人可用。

出图走 ``art_card``（help 同款外框、随机插画），渲染或发送失败时降级为文字。
import 期不读 ``.env``、不连网络、不注册定时任务。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from arclet.alconna import Alconna, Args, MultiVar
from arclet.entari import MessageChain, Session, command
from arclet.entari.command import Match
from loguru import logger
from nepattern import AnyString

from otae_bot.command_usage import Scope, usage_recorder
from otae_bot.infrastructure.rendering.art_card import render_card
from otae_bot.messaging import media
from otae_bot.messaging.inbound import get_group_id, get_rest
from otae_bot.pipeline.feature_gate import loaded_group_plugins

from .presentation import build_card, format_text
from .query import parse_query

stats_cmd = Alconna(["指令统计", "统计"], Args["rest;?", MultiVar(AnyString)])


@command.on(stats_cmd)
async def handle_usage_stats(rest: Match[tuple[str, ...]], session: Session) -> None:
    query = parse_query(get_rest(rest) or "", loaded_group_plugins())
    if query.error:
        await session.send(query.error)
        return
    group_id = get_group_id(session.event)
    scope = Scope(
        platform="" if query.global_scope or not group_id else str(session.account.platform or ""),
        group_id="" if query.global_scope else group_id,
        plugin=query.plugin,
    )
    # 先把内存里还没落盘的记录写进去，刚发的指令也能算上。
    await usage_recorder.flush()
    now = datetime.now()
    report = await asyncio.to_thread(usage_recorder.store.report, scope, query.days, now)
    try:
        png = await render_card(build_card(report, query.days, now))
        await session.send(MessageChain([media.png(png)]))
        return
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - 图片边界必须保住文本
        logger.warning("[usage_stats] card delivery fell back to text: {}", type(error).__name__)
    await session.send("\n".join(format_text(report, query.days)))


__all__ = ["handle_usage_stats", "stats_cmd"]
