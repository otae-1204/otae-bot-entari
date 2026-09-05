"""Bounded, acknowledged delivery for background radar notifications.

No bot session or local temporary-file path is required.  A failed image may
degrade to a readable text digest; neither path advances a cursor without a
message receipt.  Exception bodies are deliberately not logged (bridges may
include entire base64 payloads or request credentials in them).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger
from satori.model import Upload

from otae_bot.adapters.entari import ChainMsg, SendDest, Text, make_image

from .models import TiboPost


class DeliveryNotAcknowledged(RuntimeError):
    """An empty receipt can also mean a send hook intentionally cancelled it."""


async def _send(message: ChainMsg, target: SendDest, bot, timeout: float) -> None:
    receipts = await asyncio.wait_for(message.send(target, bot), timeout)
    if not receipts or not any(getattr(receipt, "id", None) for receipt in receipts):
        raise DeliveryNotAcknowledged("no message receipt; delivery cursor retained")


async def _image(png: bytes, bot, timeout: float):
    # Uploaded URLs belong to the current bridge, not the bot's filesystem.
    # Never cache them across cycles/reconnects: bridge-local URLs may expire.
    upload = getattr(bot.protocol, "upload_create", None)
    if callable(upload):
        try:
            urls = await asyncio.wait_for(
                upload(Upload(png, "image/png", "tibo-radar.png")), timeout
            )
            if isinstance(urls, (list, tuple)) and urls and isinstance(urls[0], str) and urls[0]:
                return make_image(url=urls[0])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[tibo_radar] image upload unavailable error_type={}", type(exc).__name__)
    # Compatibility for bridges without upload.create. Image.of(raw=...) emits
    # an inline data URL and never assumes a shared disk with the QQ client.
    return make_image(raw=png)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…（完整内容见原帖）"


def fallback_text(posts: list[TiboPost], relevance_label: Callable[[str], str]) -> str:
    lines = ["Tibo 新帖订阅 · 图片暂不可用，已转为文字", ""]
    for post in posts:
        lines.append(f"[{post.post_id}] {relevance_label(post.relevance)}")
        lines.append("原文：" + _clip(post.text, 300))
        if post.translation:
            lines.append("翻译：" + _clip(post.translation, 260))
        if post.analysis:
            lines.append("模型解读（非核验结论）：" + _clip(post.analysis, 160))
        if post.url:
            lines.append(post.url)
        lines.append("")
    return "\n".join(lines)


async def deliver_page(
    bot,
    target: SendDest,
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
        stage = "upload"
        segment = await _image(png, bot, timeout)
        stage = "image_send"
        segments = [segment]
        if links:
            segments.append(Text("源帖链接：\n" + "\n".join(links)))
        await _send(ChainMsg(segments), target, bot, timeout)
        return "image"
    except (asyncio.CancelledError, DeliveryNotAcknowledged):
        # Do not bypass an intentional send cancellation with a second message.
        raise
    except Exception as exc:
        logger.warning(
            "[tibo_radar] image delivery degraded group={} stage={} error_type={} image_bytes={}",
            target.parent_id, stage, type(exc).__name__, image_bytes,
        )
    await _send(ChainMsg.text(fallback_text(posts, relevance_label)), target, bot, timeout)
    return "text"
