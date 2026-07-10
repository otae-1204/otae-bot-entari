from __future__ import annotations

import asyncio
import textwrap
from datetime import datetime
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from utils.http_client import fetch_bytes
from utils.image_executor import run_image_render

from .models import BiliCard


CANVAS_W = 900
PADDING = 36
PINK = (251, 114, 153)
BLUE = (35, 173, 229)
TEXT = (31, 35, 41)
MUTED = (102, 112, 128)
LIGHT_BG = (246, 248, 251)
CARD_BG = (255, 255, 255)
BORDER = (226, 232, 240)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


FONT_TITLE = _font(34, True)
FONT_SUBTITLE = _font(22, True)
FONT_BODY = _font(21)
FONT_SMALL = _font(17)
FONT_BADGE = _font(18, True)


async def draw_bili_card(card: BiliCard) -> bytes:
    cover_bytes, avatar_bytes = await asyncio.gather(
        _fetch_image_bytes(card.cover_url),
        _fetch_image_bytes(card.avatar_url),
    )
    return await run_image_render(_render_bili_card, card, cover_bytes, avatar_bytes)


def _render_bili_card(
    card: BiliCard,
    cover_bytes: bytes | None,
    avatar_bytes: bytes | None,
) -> bytes:
    cover = _decode_image(cover_bytes, (360, 210))
    avatar = _decode_image(avatar_bytes, (72, 72), circle=True)

    desc = _clip(card.description, 160)
    title_lines = _wrap(card.title or "Bilibili", FONT_TITLE, CANVAS_W - PADDING * 2 - 120, max_lines=2)
    desc_lines = _wrap(desc, FONT_BODY, CANVAS_W - PADDING * 2, max_lines=5)
    height = 330 + len(title_lines) * 42 + len(desc_lines) * 30
    if cover:
        height += 230
    height = max(height, 560)

    img = Image.new("RGB", (CANVAS_W, height), LIGHT_BG)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((18, 18, CANVAS_W - 18, height - 18), radius=8, fill=CARD_BG, outline=BORDER, width=1)

    draw.rounded_rectangle((18, 18, CANVAS_W - 18, 92), radius=8, fill=(255, 241, 246), outline=None)
    draw.rectangle((18, 58, CANVAS_W - 18, 92), fill=(255, 241, 246))
    draw.text((PADDING, 40), "BILIBILI", font=FONT_SUBTITLE, fill=PINK)
    badge = card.badge or card.card_type.upper()
    badge_w = int(_text_size(draw, badge, FONT_BADGE)[0]) + 28
    badge_box = (CANVAS_W - PADDING - badge_w, 36, CANVAS_W - PADDING, 68)
    draw.rounded_rectangle(badge_box, radius=8, fill=BLUE)
    _draw_centered_text(draw, badge_box, badge, FONT_BADGE, (255, 255, 255))

    y = 120
    if avatar:
        img.paste(avatar, (PADDING, y), avatar if avatar.mode == "RGBA" else None)
    else:
        draw.ellipse((PADDING, y, PADDING + 72, y + 72), fill=(233, 241, 248), outline=BORDER)
    author = card.author or "Bilibili"
    draw.text((PADDING + 92, y + 4), author, font=FONT_SUBTITLE, fill=TEXT)
    subtitle = card.subtitle or _format_time(card.published_at)
    draw.text((PADDING + 92, y + 40), subtitle, font=FONT_SMALL, fill=MUTED)

    y += 100
    for line in title_lines:
        draw.text((PADDING, y), line, font=FONT_TITLE, fill=TEXT)
        y += 42

    if cover:
        y += 12
        cover = _fit_cover(cover, CANVAS_W - PADDING * 2, 230)
        img.paste(cover, (PADDING, y))
        y += 250

    if desc_lines:
        for line in desc_lines:
            draw.text((PADDING, y), line, font=FONT_BODY, fill=(52, 58, 67))
            y += 30
        y += 8

    meta = _meta_text(card)
    draw.line((PADDING, height - 92, CANVAS_W - PADDING, height - 92), fill=BORDER, width=1)
    draw.text((PADDING, height - 72), meta, font=FONT_SMALL, fill=MUTED)
    if card.url:
        draw.text((PADDING, height - 46), _clip(card.url, 82), font=FONT_SMALL, fill=BLUE)

    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


async def _fetch_image_bytes(url: str) -> bytes | None:
    if not url:
        return None
    try:
        resource = await fetch_bytes(
            url,
            namespace="bilibilibot-assets",
            timeout_seconds=8.0,
        )
        return resource.content
    except Exception:
        return None


def _decode_image(
    content: bytes | None,
    size: tuple[int, int],
    circle: bool = False,
) -> Image.Image | None:
    if not content:
        return None
    try:
        image = Image.open(BytesIO(content)).convert("RGB")
        image = _fit_cover(image, *size)
        if circle:
            mask = Image.new("L", size, 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size[0], size[1]), fill=255)
            rgba = image.convert("RGBA")
            rgba.putalpha(mask)
            return rgba
        return image
    except Exception:
        return None


def _fit_cover(image: Image.Image, width: int, height: int) -> Image.Image:
    image = image.convert("RGB")
    src_w, src_h = image.size
    scale = max(width / src_w, height / src_h)
    resized = image.resize((int(src_w * scale), int(src_h * scale)))
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def _wrap(text: str, font, max_width: int, max_lines: int) -> list[str]:
    text = " ".join(str(text or "").split())
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if font.getlength(candidate) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = char
            if len(lines) >= max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and font.getlength(lines[-1] + "...") > max_width:
        lines[-1] = lines[-1][:-3] + "..."
    elif len(lines) == max_lines and len("".join(lines)) < len(text):
        lines[-1] += "..."
    return lines


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _centered_text_position(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font,
) -> tuple[float, float]:
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    box_l, box_t, box_r, box_b = box
    x = box_l + ((box_r - box_l) - text_w) / 2 - bbox[0]
    y = box_t + ((box_b - box_t) - text_h) / 2 - bbox[1]
    return x, y


def _draw_centered_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font,
    fill,
) -> None:
    draw.text(_centered_text_position(draw, box, text, font), text, font=font, fill=fill)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _format_time(ts: int) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")


def _meta_text(card: BiliCard) -> str:
    parts = []
    if card.uid:
        parts.append(f"UID {card.uid}")
    if card.room_id:
        parts.append(f"ROOM {card.room_id}")
    if card.item_id:
        parts.append(card.item_id)
    if card.published_at:
        parts.append(_format_time(card.published_at))
    return "  |  ".join(parts) or "Bilibili"
