from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from otae_bot.infrastructure.http.client import fetch_bytes
from otae_bot.infrastructure.rendering.executor import run_image_render

from .models import BiliCard

CANVAS_W = 900
PADDING = 48
CONTENT_W = CANVAS_W - PADDING * 2
TEXT = (65, 54, 62)
MUTED = (128, 113, 124)
LIGHT_BG = (255, 248, 251)
CARD_BG = (255, 255, 255)
BORDER = (243, 229, 236)
COVER_BG = (253, 246, 249)
ROSE = (212, 111, 148)


def _font(
    size: int, bold: bool = False
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    # Use the same bundled Chinese fonts on Windows and Linux.
    font_dir = Path(__file__).resolve().parents[2] / "assets/font/steamInfo"
    candidates = [
        str(font_dir / ("MiSans-Bold.ttf" if bold else "MiSans-Regular.ttf")),
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


FONT_STATUS = _font(44, True)
FONT_TITLE = _font(32, True)
FONT_SUBTITLE = _font(27, True)
FONT_BODY = _font(22)
FONT_SMALL = _font(20)
FONT_BADGE = _font(18, True)


@dataclass(frozen=True)
class _CardStyle:
    label: str
    hint: str
    icon: str
    accent: tuple[int, int, int]
    background: tuple[int, int, int]


_STYLES = {
    "video": _CardStyle("视频", "视频预览", "play", ROSE, (255, 241, 247)),
    "live_on": _CardStyle("已开播", "直播进行中", "live", ROSE, (255, 231, 241)),
    "live_off": _CardStyle(
        "已下播", "本场直播已结束", "stop", (143, 131, 149), (247, 243, 249)
    ),
    "live_idle": _CardStyle(
        "未开播", "直播间当前休息中", "idle", (143, 131, 149), (247, 243, 249)
    ),
    "dynamic": _CardStyle("动态", "UP 主的新动态", "dynamic", ROSE, (255, 241, 247)),
}
_DEFAULT_STYLE = _CardStyle("哔哩哔哩", "内容分享", "dynamic", ROSE, (255, 241, 247))


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
    style = _STYLES.get(card.card_type, _DEFAULT_STYLE)
    cover = _decode_image(cover_bytes)
    avatar = _decode_image(avatar_bytes, (64, 64), circle=True)
    cover_panel = _fit_full_cover(cover, CONTENT_W) if cover else None
    # A missing cover still gets an explicit, compact placeholder.
    cover_h = cover_panel.height if cover_panel else 220
    title_lines = _wrap(card.title, FONT_TITLE, CONTENT_W, max_lines=3) or ["Bilibili"]
    description = _clip(card.description, 240)
    if description in {"-", "--", "—"}:
        description = ""
    desc_lines = _wrap(
        description,
        FONT_BODY,
        CONTENT_W,
        max_lines=5 if card.card_type == "dynamic" else 3,
    )
    meta_lines = _wrap(_meta_text(card), FONT_SMALL, CONTENT_W, max_lines=2)
    url_lines = _wrap(card.url, FONT_SMALL, CONTENT_W, max_lines=2)
    cover_y = 264
    title_y = cover_y + cover_h + 28
    desc_y = title_y + len(title_lines) * 44 + (14 if desc_lines else 0)
    divider_y = desc_y + len(desc_lines) * 32 + 26
    height = divider_y + 24 + len(meta_lines) * 28 + len(url_lines) * 28 + 40

    img = Image.new("RGB", (CANVAS_W, height), LIGHT_BG)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (18, 18, CANVAS_W - 18, height - 18), radius=24, fill=CARD_BG, outline=BORDER
    )

    # Status is the first visual anchor, independent of the cover and badge text.
    draw.rounded_rectangle(
        (18, 18, CANVAS_W - 18, 154), radius=24, fill=style.background
    )
    draw.rectangle((18, 110, CANVAS_W - 18, 154), fill=style.background)
    _draw_status_icon(draw, (PADDING, 50, PADDING + 64, 114), style)
    draw.text(
        (PADDING + 88, 43),
        style.label,
        font=FONT_STATUS,
        fill=style.accent,
        anchor="lt",
    )
    draw.text(
        (PADDING + 90, 100),
        _status_hint(card, style),
        font=FONT_SMALL,
        fill=style.accent,
        anchor="lt",
    )
    draw.text(
        (CANVAS_W - PADDING, 58),
        "BILIBILI",
        font=FONT_BADGE,
        fill=style.accent,
        anchor="rt",
    )

    author_y = 177
    if avatar:
        img.paste(avatar, (PADDING, author_y), avatar)
    else:
        draw.ellipse((PADDING, author_y, PADDING + 64, author_y + 64), fill=COVER_BG)
        _draw_centered_text(
            draw,
            (PADDING, author_y, PADDING + 64, author_y + 64),
            (card.author or "B")[:1],
            FONT_SUBTITLE,
            ROSE,
        )
    author_lines = _wrap(card.author, FONT_SUBTITLE, CONTENT_W - 84, max_lines=1)
    author = author_lines[0] if author_lines else "Bilibili"
    draw.text(
        (PADDING + 84, author_y + 3), author, font=FONT_SUBTITLE, fill=TEXT, anchor="lt"
    )
    subtitle = _author_detail(card)
    detail_lines = _wrap(subtitle, FONT_SMALL, CONTENT_W - 84, max_lines=1)
    if detail_lines:
        draw.text(
            (PADDING + 84, author_y + 42),
            detail_lines[0],
            font=FONT_SMALL,
            fill=MUTED,
            anchor="lt",
        )

    draw.rounded_rectangle(
        (PADDING - 6, cover_y - 6, CANVAS_W - PADDING + 6, cover_y + cover_h + 6),
        radius=12,
        fill=COVER_BG,
    )
    if cover_panel:
        # No crop, rounded mask, text or state overlay may hide the source cover.
        img.paste(cover_panel, (PADDING, cover_y))
    else:
        _draw_centered_text(
            draw,
            (PADDING, cover_y, CANVAS_W - PADDING, cover_y + cover_h),
            "封面暂不可用",
            FONT_BODY,
            MUTED,
        )

    for index, line in enumerate(title_lines):
        draw.text(
            (PADDING, title_y + index * 44),
            line,
            font=FONT_TITLE,
            fill=TEXT,
            anchor="lt",
        )
    for index, line in enumerate(desc_lines):
        draw.text(
            (PADDING, desc_y + index * 32),
            line,
            font=FONT_BODY,
            fill=MUTED,
            anchor="lt",
        )

    draw.line((PADDING, divider_y, CANVAS_W - PADDING, divider_y), fill=BORDER)
    y = divider_y + 22
    for line in meta_lines:
        draw.text((PADDING, y), line, font=FONT_SMALL, fill=MUTED, anchor="lt")
        y += 28
    for line in url_lines:
        draw.text((PADDING, y), line, font=FONT_SMALL, fill=ROSE, anchor="lt")
        y += 28

    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _draw_status_icon(
    draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], style: _CardStyle
) -> None:
    left, top, right, bottom = box
    # Shape + Chinese text conveys state even without colour perception.
    draw.rounded_rectangle(box, radius=18, fill=style.accent)
    cx, cy = (left + right) // 2, (top + bottom) // 2
    if style.icon == "play":
        draw.polygon(
            ((cx - 9, cy - 15), (cx - 9, cy + 15), (cx + 16, cy)), fill=CARD_BG
        )
    elif style.icon == "live":
        for offset, bar_h in ((-18, 16), (-6, 32), (6, 42), (18, 24)):
            draw.rounded_rectangle(
                (cx + offset - 3, cy - bar_h // 2, cx + offset + 3, cy + bar_h // 2),
                radius=3,
                fill=CARD_BG,
            )
    elif style.icon == "stop":
        draw.rounded_rectangle(
            (cx - 13, cy - 13, cx + 13, cy + 13), radius=4, fill=CARD_BG
        )
    elif style.icon == "idle":
        for offset in (-9, 9):
            draw.rounded_rectangle(
                (cx + offset - 4, cy - 15, cx + offset + 4, cy + 15),
                radius=3,
                fill=CARD_BG,
            )
    else:
        draw.rounded_rectangle(
            (cx - 17, cy - 14, cx + 17, cy + 12), radius=6, outline=CARD_BG, width=3
        )
        draw.line(
            (cx - 9, cy + 12, cx - 9, cy + 20, cx + 2, cy + 12), fill=CARD_BG, width=3
        )
        draw.line((cx - 9, cy - 4, cx + 9, cy - 4), fill=CARD_BG, width=3)


def _status_hint(card: BiliCard, style: _CardStyle) -> str:
    if card.card_type != "live_off":
        return style.hint
    seconds = card.live_duration_seconds
    if seconds is None or seconds < 0:
        return "本次直播时长未知"
    minutes = seconds // 60
    if minutes == 0:
        return "本次直播不足 1 分钟"
    hours, minutes = divmod(minutes, 60)
    parts = ([f"{hours} 小时"] if hours else []) + (
        [f"{minutes} 分钟"] if minutes else []
    )
    return "本次直播约 " + " ".join(parts)


def _author_detail(card: BiliCard) -> str:
    if card.card_type.startswith("live_"):
        return f"直播间 {card.room_id}" if card.room_id else "哔哩哔哩直播"
    return card.subtitle or (
        f"发布于 {_format_time(card.published_at)}"
        if card.published_at
        else "哔哩哔哩 UP 主"
    )


async def _fetch_image_bytes(url: str) -> bytes | None:
    if not url:
        return None
    try:
        resource = await fetch_bytes(
            url, namespace="bilibilibot-assets", timeout_seconds=8.0
        )
        return resource.content
    except Exception:  # noqa: BLE001 - failed optional assets must not block a notification
        return None


def _decode_image(
    content: bytes | None,
    size: tuple[int, int] | None = None,
    circle: bool = False,
) -> Image.Image | None:
    if not content:
        return None
    try:
        with Image.open(BytesIO(content)) as source:
            oriented = ImageOps.exif_transpose(source).convert("RGBA")
            image = Image.new("RGB", oriented.size, CARD_BG)
            image.paste(oriented, mask=oriented.getchannel("A"))
        if circle:
            size = size or (64, 64)
            image = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
            mask = Image.new("L", size, 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size[0] - 1, size[1] - 1), fill=255)
            image = image.convert("RGBA")
            image.putalpha(mask)
        return image
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None


def _fit_full_cover(image: Image.Image, width: int) -> Image.Image:
    """Preserve every edge; constrain extreme aspect ratios with letterboxing."""
    natural_height = round(width * image.height / image.width)
    height = max(240, min(640, natural_height))
    scale = min(width / image.width, height / image.height)
    fitted = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        resample=Image.Resampling.LANCZOS,
    )
    panel = Image.new("RGB", (width, height), COVER_BG)
    panel.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
    return panel


def _wrap(text: str, font, max_width: int, max_lines: int) -> list[str]:
    text = " ".join(str(text or "").split())
    if not text or max_lines <= 0:
        return []
    lines: list[str] = []
    current = ""
    for char in text:
        if font.getlength(current + char) <= max_width:
            current += char
            continue
        if len(lines) == max_lines - 1:
            # Reserve actual glyph width for the ellipsis; never overflow the row.
            ellipsis = "…"
            while current and font.getlength(current + ellipsis) > max_width:
                current = current[:-1]
            if font.getlength(ellipsis) <= max_width:
                lines.append(current + ellipsis)
            return lines
        if current:
            lines.append(current)
        current = char if font.getlength(char) <= max_width else ""
    if current:
        lines.append(current)
    return lines


def _centered_text_position(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font,
) -> tuple[float, float]:
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    box_l, box_t, box_r, box_b = box
    return (
        box_l + ((box_r - box_l) - text_w) / 2 - bbox[0],
        box_t + ((box_b - box_t) - text_h) / 2 - bbox[1],
    )


def _draw_centered_text(draw: ImageDraw.ImageDraw, box, text: str, font, fill) -> None:
    draw.text(
        _centered_text_position(draw, box, text, font), text, font=font, fill=fill
    )


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _format_time(ts: int) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts)).astimezone().strftime("%Y-%m-%d %H:%M")


def _meta_text(card: BiliCard) -> str:
    parts = []
    if card.uid:
        parts.append(f"UID {card.uid}")
    if card.item_id:
        parts.append(card.item_id)
    if card.published_at and card.card_type.startswith("live_"):
        parts.append(f"状态更新于 {_format_time(card.published_at)}")
    return "  ·  ".join(parts) or "哔哩哔哩"
