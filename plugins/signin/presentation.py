"""Combine game results for one chat reply, without repeating attendance."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO

from loguru import logger
from PIL import Image

from otae_bot.attendance_registry import SIGNED, AttendanceOutcome, build_notice
from otae_bot.infrastructure.rendering.executor import run_image_render

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
class SigninDelivery:
    png: bytes | None
    text: str
    fallback_text: str


async def build_delivery(outcomes: Sequence[AttendanceOutcome]) -> SigninDelivery:
    signed = [item for item in outcomes if item.status == SIGNED]
    notice = build_notice(outcomes)
    reports = [item.text or f"{item.label}：签到请求已处理，结果图片不可用。" for item in signed]
    fallback = "\n\n".join(part for part in [*reports, notice] if part)
    text = "\n\n".join(part for part in [
        *(item.text for item in signed if item.png is None), notice,
    ] if part)
    cards = [item.png for item in signed if item.png is not None]
    if not cards:
        return SigninDelivery(None, fallback, fallback)
    try:
        png = await run_image_render(stack_cards, cards)
    except Exception as exc:  # noqa: BLE001 - retain every result even if image composition fails
        logger.warning(f"[signin] composition failed error_type={type(exc).__name__}")
        return SigninDelivery(None, fallback, fallback)
    return SigninDelivery(png, text, fallback)
