"""Turn attendance results into one chat reply, without repeating attendance.

``/ak 签到`` and ``/ef 签到`` sign the requested game first and then, through
:mod:`otae_bot.attendance_registry`, the user's bound roles in the other game.
This module stacks the resulting cards into one image and sends it together
with any notices.  When sending the image fails, the complete text report is
sent once instead; the attendance requests themselves are never repeated.
"""

from __future__ import annotations

import tempfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from arclet.letoderea.exceptions import _ExitException
from loguru import logger
from PIL import Image

from otae_bot.adapters.entari import ChainMsg, make_image
from otae_bot.attendance_registry import SIGNED, AttendanceOutcome, build_notice
from otae_bot.infrastructure.rendering.executor import run_image_render
from otae_bot.infrastructure.rendering.temp_files import schedule_temp_file_cleanup

MAX_IMAGE_PIXELS = 32_000_000
MAX_IMAGE_HEIGHT = 16384


def stack_cards(cards: Sequence[bytes]) -> bytes:
    """Stack at the smallest source width, without upscaling or cutting rows.

    Excessively tall results use the caller's complete text report instead of
    shrinking all text into an unreadable thumbnail or allocating huge images.
    """
    if not cards:
        raise ValueError("No cards to combine")
    if len(cards) == 1:
        return cards[0]
    sizes = []
    for content in cards:
        with Image.open(BytesIO(content)) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise ValueError("Card exceeds the pixel budget")
            sizes.append(source.size)
    width = min(size[0] for size in sizes)
    heights = [max(1, round(h * width / w)) for w, h in sizes]
    height = sum(heights)
    if height > MAX_IMAGE_HEIGHT or width * height > MAX_IMAGE_PIXELS:
        raise ValueError("Combined card is too large; retain the text report")
    with Image.new("RGB", (width, height), "#ededed") as canvas:
        top = 0
        for content, h in zip(cards, heights):
            with Image.open(BytesIO(content)) as source:
                rgba = source.convert("RGBA")
                if rgba.size != (width, h):
                    resized = rgba.resize((width, h), Image.Resampling.LANCZOS)
                    rgba.close()
                    rgba = resized
                canvas.paste(rgba, (0, top), rgba)
                rgba.close()
            top += h
        output = BytesIO()
        canvas.save(output, format="PNG")
        return output.getvalue()


@dataclass(frozen=True)
class AttendanceDelivery:
    """What to send: one image (or none), the text beside it, and the text retry.

    ``fallback_text`` always carries every game's complete report, so a failed
    image send still tells the user what was signed.
    """

    png: bytes | None
    text: str
    fallback_text: str


async def build_delivery(outcomes: Sequence[AttendanceOutcome]) -> AttendanceDelivery:
    """Combine the games' cards top to bottom, in the order of ``outcomes``."""
    signed = [item for item in outcomes if item.status == SIGNED]
    notice = build_notice(outcomes)
    reports = [item.text or f"{item.label}：签到请求已处理，结果图片不可用。" for item in signed]
    fallback = "\n\n".join(part for part in [*reports, notice] if part)
    text = "\n\n".join(part for part in [
        *(item.text for item in signed if item.png is None), notice,
    ] if part)
    cards = [item.png for item in signed if item.png is not None]
    if not cards:
        return AttendanceDelivery(None, fallback, fallback)
    if len(cards) == 1:
        return AttendanceDelivery(cards[0], text, fallback)
    try:
        png = await run_image_render(stack_cards, cards)
    except Exception as exc:  # noqa: BLE001 - retain every result even if image composition fails
        logger.warning(f"[attendance] composition failed error_type={type(exc).__name__}")
        return AttendanceDelivery(None, fallback, fallback)
    return AttendanceDelivery(png, text, fallback)


async def deliver(send: Callable[[Any], Awaitable[Any]], delivery: AttendanceDelivery) -> None:
    """Send the image and its notices together; retry only the delivery, as text.

    ``send`` is the matcher's ``send``.  Entari's ``_ExitException`` still
    propagates; any other send failure of the image is answered with the full
    text report, and a failure of that retry is only logged.
    """
    try:
        if delivery.png is None:
            if delivery.text:
                await send(delivery.text)
            return
        parts = [png_image(delivery.png)]
        if delivery.text:
            parts.append("\n" + delivery.text)
        await send(ChainMsg(parts))
    except _ExitException:
        raise
    except Exception as exc:  # noqa: BLE001 - preserve results when image delivery fails
        logger.warning(f"[attendance] delivery failed error_type={type(exc).__name__}")
        if delivery.png is None or not delivery.fallback_text:
            return
        try:
            await send(delivery.fallback_text)
        except _ExitException:
            raise
        except Exception as retry_exc:  # noqa: BLE001 - never repeat the attendance request
            logger.warning(f"[attendance] text delivery failed error_type={type(retry_exc).__name__}")


def png_image(png: bytes):
    """Persist one PNG to a temp file and wrap it as an image element.

    LLOneBot resolves ``file://`` locally, so a temp file avoids inlining a
    base64 data URI for every card, matching the two game plugins.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as file:
        file.write(png)
        file.flush()
        schedule_temp_file_cleanup(file.name)
        return make_image(path=file.name)


__all__ = [
    "MAX_IMAGE_HEIGHT",
    "MAX_IMAGE_PIXELS",
    "AttendanceDelivery",
    "build_delivery",
    "deliver",
    "png_image",
    "stack_cards",
]
