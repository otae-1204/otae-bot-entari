"""Bounded, acknowledged delivery for background radar notifications.

No bot session is required.  The image is handed over as bytes; the outbound
pipeline uploads it through the sending account (``upload.create``) or sends
it inline, fresh for every send.  A failed image may degrade to a readable text
digest; neither path advances a cursor without a message receipt.  Exception
bodies are deliberately not logged (bridges may include entire base64 payloads
or request credentials in them).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from arclet.entari import MessageChain, Text
from loguru import logger

from otae_bot.delivery import Target, deliver
from otae_bot.messaging.media import png as png_image

from .models import TiboPost


class DeliveryNotAcknowledged(RuntimeError):
    """An empty receipt can also mean a send hook intentionally cancelled it."""


async def _send(message: MessageChain, target: Target, bot, timeout: float) -> None:
    result = await asyncio.wait_for(deliver(target, message, account=bot), timeout)
    result.raise_for_failure()
    if not any(getattr(receipt, "id", None) for receipt in result.messages):
        raise DeliveryNotAcknowledged("no message receipt; delivery cursor retained")


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…（完整内容见原帖）"


def fallback_text(posts: list[TiboPost], relevance_label: Callable[[str], str]) -> str:
    lines = ["Tibo 新帖推送（图片渲染降级，已切换为纯文本模式）", ""]
    for post in posts:
        lines.append(f"[{post.post_id}] {relevance_label(post.relevance)}")
        lines.append("原文：" + _clip(post.text, 300))
        if post.translation:
            lines.append("翻译：" + _clip(post.translation, 260))
        if post.analysis:
            lines.append("模型解读（非官方核验结果）：" + _clip(post.analysis, 160))
        if post.url:
            lines.append(post.url)
        lines.append("")
    return "\n".join(lines)


async def deliver_page(
    bot,
    target: Target,
    posts: list[TiboPost],
    render: Callable[[], Awaitable[bytes]],
    links: list[str],
    relevance_label: Callable[[str], str],
    *,
    timeout: float = 30,
) -> str:
    stage = "render"
    image_bytes = 0
    try:
        png = await asyncio.wait_for(render(), timeout)
        image_bytes = len(png)
        stage = "image_send"
        segments = [png_image(png)]
        if links:
            segments.append(Text("源帖链接：\n" + "\n".join(links)))
        await _send(MessageChain(segments), target, bot, timeout)
        return "image"
    except (asyncio.CancelledError, DeliveryNotAcknowledged):
        # Do not bypass an intentional send cancellation with a second message.
        raise
    except Exception as exc:
        logger.warning(
            "[tibo_radar] image delivery degraded group={} stage={} error_type={} image_bytes={}",
            target.guild_id, stage, type(exc).__name__, image_bytes,
        )
    await _send(MessageChain([Text(fallback_text(posts, relevance_label))]), target, bot, timeout)
    return "text"
