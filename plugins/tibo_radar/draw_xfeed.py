"""Tibo radar X-feed card renderer — dark "X timeline" style reply images.

A dedicated renderer for the ``/tibo 动态`` command.  Instead of generic
section panels, the card is laid out like the X (Twitter) dark-mode timeline:

- near-black background with a pure-black header band and the X glyph emblem
- a profile strip (avatar monogram, name + verified seal, @handle, stats)
- one feed card per post with a numbered timeline rail on the left
- each post shows the English original, the Chinese translation, the model
  interpretation and (when present) key-phrase chips plus the original link

The module intentionally owns its own small helper set (fonts, wrapping,
chips, LEDs) so it can be tuned without touching ``draw.py`` / ``draw_v2.py``.
It also exports the classic ``AMBER`` / ``CYAN`` / ``GREEN`` constants so a
caller can switch a card accent to a feed accent by importing this module.
"""

from __future__ import annotations

import re
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from utils.image_executor import run_image_render

from .client import TIBO_HANDLE
from .models import TiboPost

CANVAS_W = 1080
PAD_X = 40
PANEL_GAP = 20
PANEL_PAD_X = 30
HEADER_H = 216
FOOTER_H = 84
PROFILE_H = 150
MAX_HEIGHT = 3900

RAIL_X = PAD_X + 26
CARD_X = PAD_X + 58
CARD_W = CANVAS_W - PAD_X - CARD_X

# --- Palette: X dark mode ----------------------------------------------------
BG_TOP = (8, 10, 14)
BG_BOT = (14, 18, 26)
HEADER_TOP = (0, 0, 0)
HEADER_BOT = (10, 14, 20)
PANEL = (22, 28, 39)
PANEL_EDGE = (56, 68, 90)
PANEL_ALT = (17, 22, 31)
TEXT = (231, 236, 244)
SUBTLE = (196, 206, 220)
MUTED = (150, 162, 178)
FAINT = (104, 116, 134)
LINE = (255, 255, 255)

X_BLUE = (29, 155, 240)
X_BLUE_DEEP = (13, 108, 196)
GREEN = (43, 213, 118)
AMBER = (245, 166, 35)
RED = (240, 96, 109)
GRAY = (128, 140, 158)
CYAN = (86, 200, 226)

# Compatibility constants (used by callers that switch accents).
BLUE = X_BLUE
BG = BG_TOP
PANEL_LEGACY = PANEL
TEXT_LEGACY = TEXT
MUTED_LEGACY = MUTED
LINE_LEGACY = LINE

_RELEVANCE_COLORS = {"direct": GREEN, "indirect": AMBER, "none": GRAY}
_CHIP_ALPHA = 30
_CHIP_EDGE_ALPHA = 150

TIBO_DISPLAY_NAME = "Tibo"
TIBO_AVATAR_PATH = Path(__file__).resolve().parents[2] / "assets" / "image" / "tibo" / "tibo-x-avatar.jpg"
# Downloaded from X's official brand toolkit (x-logo.zip).
TIBO_X_LOGO_PATH = Path(__file__).resolve().parents[2] / "assets" / "image" / "tibo" / "x-logo-white.png"


# ---------------------------------------------------------------------------
# Fonts and shared text helpers.
# ---------------------------------------------------------------------------

def _font(size: int, bold: bool = False, light: bool = False):
    name = "MiSans-Bold.ttf" if bold else "MiSans-Light.ttf" if light else "MiSans-Regular.ttf"
    candidates = [
        Path("assets/font/steamInfo") / name,
        Path("C:/Windows/Fonts/msyhbd.ttc") if bold else Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


FONT_TITLE = _font(44, bold=True)
FONT_KICKER = _font(18, bold=True)
FONT_NAME = _font(30, bold=True)
FONT_SECTION = _font(24, bold=True)
FONT_BODY = _font(23)
FONT_QUOTE = _font(22, light=True)
FONT_SMALL = _font(20)
FONT_SMALL_B = _font(20, bold=True)
FONT_TAG = _font(17, bold=True)
FONT_TINY = _font(17)
FONT_LINK = _font(18)
FONT_AVATAR = _font(30, bold=True)
FONT_AVATAR_MINI = _font(19, bold=True)

_WRAP_TOKEN = re.compile(r"[A-Za-z0-9_\-@#&%/+.:'\",;!?()\[\]{}<>=~*|]+|\s+|.", re.S)
_URL_TOKEN = re.compile(r"(https?://[^\s，。；、]+)")
_PHRASE_SPLIT = re.compile(r"[、，,；;/]+")


def _wrap(text: str, font, max_width: int) -> list[str]:
    """Greedy wrap that keeps latin words intact and hard-splits long URLs."""
    text = str(text or "").replace("\r", "")
    result: list[str] = []
    for raw in text.split("\n"):
        cur = ""
        for token in _WRAP_TOKEN.findall(raw):
            if token.isspace():
                if cur:
                    cur += " "
                continue
            candidate = cur + token
            if cur and font.getlength(candidate) > max_width:
                result.append(cur.rstrip())
                cur = token
            elif not cur and font.getlength(token) > max_width:
                for char in token:
                    if cur and font.getlength(cur + char) > max_width:
                        result.append(cur)
                        cur = char
                    else:
                        cur += char
            else:
                cur = candidate
        if cur or not result:
            result.append(cur.rstrip())
    return result or [""]


def _wrap_clip(text: str, font, max_width: int, max_lines: int) -> tuple[list[str], bool]:
    lines = _wrap(text, font, max_width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + "…"
        return lines, True
    return lines, False


def _clip(text: str, limit: int = 520) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _clip_width(text: str, font, max_width: float) -> str:
    text = str(text or "")
    if font.getlength(text) <= max_width:
        return text
    out = ""
    for char in text:
        if font.getlength(out + char + "…") > max_width:
            break
        out += char
    return out + "…"


def _dtext(draw: ImageDraw.ImageDraw, pos: tuple[float, float], text: str, font, fill, anchor: str = "la") -> None:
    if anchor == "la" or isinstance(font, ImageFont.FreeTypeFont):
        draw.text(pos, text, font=font, fill=fill, anchor=None if anchor == "la" else anchor)
        return
    x, y = pos
    left, top, right, bottom = draw.textbbox((x, y), text, font=font)
    width, height = right - left, bottom - top
    dx = {"l": 0, "m": -width / 2, "r": -width}.get(anchor[0], 0)
    dy = {"t": 0, "m": -height / 2, "a": -height, "b": -height}.get(anchor[1], 0)
    draw.text((x + dx, y + dy), text, font=font, fill=fill)


def _draw_mixed(draw: ImageDraw.ImageDraw, x: float, y: float, text: str, font, color, link_color=X_BLUE) -> None:
    """Draw one line, coloring URLs differently from the base text."""
    pos = x
    for part in _URL_TOKEN.split(text):
        if not part:
            continue
        fill = link_color if part.startswith("http") else color
        _dtext(draw, (pos, y), part, font, fill)
        pos += font.getlength(part)


def _chip(draw: ImageDraw.ImageDraw, x: float, y: float, text: str, color, font=None, *, alpha: int = _CHIP_ALPHA, edge: int = _CHIP_EDGE_ALPHA) -> float:
    font = font or FONT_TAG
    width = font.getlength(text) + 22
    height = getattr(font, "size", 16) + 12
    draw.rounded_rectangle((x, y, x + width, y + height), radius=height / 2, fill=(*color, alpha), outline=(*color, edge), width=1)
    _dtext(draw, (x + 11, y + 5), text, font, color)
    return width


def _led(draw: ImageDraw.ImageDraw, cx: float, cy: float, color, r: float = 7) -> None:
    for radius, alpha in ((r + 9, 22), (r + 4, 48), (r, 235)):
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(*color, alpha))


def _fmt_md(value: datetime | None) -> str:
    if value is None:
        return "时间未知"
    return value.astimezone().strftime("%m-%d %H:%M")


def _fmt_full(value: datetime | None) -> str:
    if value is None:
        return "时间未知"
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Small raster/vector helpers.
# ---------------------------------------------------------------------------

def _load_x_logo() -> Image.Image | None:
    """Load the official X logo asset bundled with the plugin."""

    try:
        with Image.open(TIBO_X_LOGO_PATH) as source:
            return source.convert("RGBA")
    except (FileNotFoundError, OSError):
        return None


def _draw_verified(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float = 11) -> None:
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*X_BLUE, 240))
    draw.line((cx - r * 0.42, cy + 0.2, cx - r * 0.1, cy + r * 0.34), fill=(255, 255, 255, 255), width=max(2, int(r * 0.22)))
    draw.line((cx - r * 0.1, cy + r * 0.34, cx + r * 0.5, cy - r * 0.38), fill=(255, 255, 255, 255), width=max(2, int(r * 0.22)))


def _load_avatar() -> Image.Image | None:
    """Load the CodexRadar-sourced Tibo portrait, with a safe fallback."""

    try:
        with Image.open(TIBO_AVATAR_PATH) as source:
            return ImageOps.exif_transpose(source).convert("RGBA")
    except (FileNotFoundError, OSError):
        return None


def _draw_avatar(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    radius: float,
    letter: str,
    font,
    letter_size: float,
    avatar: Image.Image | None = None,
) -> None:
    """Draw the real profile portrait, falling back to the monogram."""

    if avatar is not None:
        size = max(2, int(round(radius * 2)))
        cropped = ImageOps.fit(
            avatar,
            (size, size),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.45),
        )
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        image.paste(cropped, (int(round(cx - radius)), int(round(cy - radius))), mask)
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=(*X_BLUE, 180), width=1)
        return

    # Fallback: blue gradient avatar with a monogram letter.
    steps = max(6, int(radius))
    for i in range(steps):
        ratio = i / max(1, steps - 1)
        color = (
            int(X_BLUE[0] + (X_BLUE_DEEP[0] - X_BLUE[0]) * ratio),
            int(X_BLUE[1] + (X_BLUE_DEEP[1] - X_BLUE[1]) * ratio),
            int(X_BLUE[2] + (X_BLUE_DEEP[2] - X_BLUE[2]) * ratio),
        )
        rr = radius - i / steps * radius
        draw.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=color)
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=(*X_BLUE, 110), width=1)
    _dtext(draw, (cx, cy), letter, font, (255, 255, 255), anchor="mm")


# ---------------------------------------------------------------------------
# Layout.
# ---------------------------------------------------------------------------

def _measure_post(post: TiboPost, relevance_label, inner_w: int) -> dict:
    relevance = post.relevance if post.relevance in _RELEVANCE_COLORS else "none"
    accent = _RELEVANCE_COLORS[relevance]
    label = relevance_label(relevance)

    original = _clip(post.text, 560)
    translation = post.translation or ""
    analysis = post.analysis or ""
    phrases = post.phrases or ""

    orig_lines, orig_trunc = _wrap_clip(original, FONT_BODY, inner_w, 7)
    trans_lines, _ = _wrap_clip(translation, FONT_QUOTE, inner_w, 5)
    anal_lines, _ = _wrap_clip(analysis, FONT_QUOTE, inner_w, 5)

    h = 24 + 46  # top pad + header row
    h += len(orig_lines) * 34 + 12
    blocks: list[dict] = []
    if trans_lines:
        blocks.append({"kind": "trans", "lines": trans_lines, "h": 32 + len(trans_lines) * 32})
        h += 32 + len(trans_lines) * 32
    if anal_lines:
        blocks.append({"kind": "anal", "lines": anal_lines, "h": 32 + len(anal_lines) * 32})
        h += 32 + len(anal_lines) * 32
    if blocks:
        h += 14 * (len(blocks) - 1)
        h += 12  # divider gap

    phrase_chips = _split_phrases(phrases)[:6]
    if phrase_chips:
        rows = _layout_chips(phrase_chips, FONT_TAG, inner_w, gap=10)
        h += 14 + len(rows) * 30
    h += 16 + 26  # link row
    h += 24  # bottom pad
    return {
        "relevance": relevance,
        "label": label,
        "accent": accent,
        "orig_lines": orig_lines,
        "trans_lines": trans_lines,
        "anal_lines": anal_lines,
        "phrase_chips": phrase_chips,
        "height": h,
        "url": post.url,
        "post_id": post.post_id,
    }


def _split_phrases(value: str) -> list[str]:
    parts = [part.strip() for part in _PHRASE_SPLIT.split(str(value or "")) if part.strip()]
    return parts or ([str(value).strip()] if str(value or "").strip() else [])


def _layout_chips(items: list[str], font, max_width: float, gap: float = 10) -> list[list[str]]:
    rows: list[list[str]] = []
    current: list[str] = []
    width = 0.0
    for item in items:
        w = font.getlength(item) + 22
        if current and width + gap + w > max_width:
            rows.append(current)
            current, width = [], 0.0
        current.append(item)
        width += w + gap
    if current:
        rows.append(current)
    return rows


# ---------------------------------------------------------------------------
# Painting.
# ---------------------------------------------------------------------------

def _draw_background(draw: ImageDraw.ImageDraw, height: int) -> None:
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = (
            int(BG_TOP[0] + (BG_BOT[0] - BG_TOP[0]) * ratio),
            int(BG_TOP[1] + (BG_BOT[1] - BG_TOP[1]) * ratio),
            int(BG_TOP[2] + (BG_BOT[2] - BG_TOP[2]) * ratio),
        )
        draw.line((0, y, CANVAS_W, y), fill=color)


def _draw_header(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    title: str,
    subtitle: str,
    page: str,
    x_logo: Image.Image | None,
) -> None:
    band_h = HEADER_H - 26
    for y in range(band_h):
        ratio = y / max(1, band_h - 1)
        color = (
            int(HEADER_TOP[0] + (HEADER_BOT[0] - HEADER_TOP[0]) * ratio),
            int(HEADER_TOP[1] + (HEADER_BOT[1] - HEADER_TOP[1]) * ratio),
            int(HEADER_TOP[2] + (HEADER_BOT[2] - HEADER_TOP[2]) * ratio),
        )
        draw.line((0, y, CANVAS_W, y), fill=color)

    # Use the official X logo asset; do not approximate the mark with vectors.
    emblem_cx, emblem_cy = CANVAS_W - 128, band_h // 2 + 8
    if x_logo is not None:
        logo = ImageOps.contain(x_logo, (86, 86), method=Image.Resampling.LANCZOS)
        logo_x = int(round(emblem_cx - logo.width / 2))
        logo_y = int(round(emblem_cy - logo.height / 2))
        image.paste(logo, (logo_x, logo_y), logo)

    kicker = "X · PUBLIC FEED"
    kicker_w = sum(FONT_KICKER.getlength(ch) + 3 for ch in kicker) - 3 + 28
    draw.rounded_rectangle((PAD_X, 42, PAD_X + kicker_w, 74), radius=16, fill=(*X_BLUE, 24), outline=(*X_BLUE, 140), width=1)
    pos = PAD_X + 14
    for ch in kicker:
        _dtext(draw, (pos, 48), ch, FONT_KICKER, X_BLUE)
        pos += FONT_KICKER.getlength(ch) + 3

    if page:
        label = f"PAGE {page}"
        width = FONT_TAG.getlength(label) + 24
        _chip(draw, CANVAS_W - PAD_X - width - 190, 46, label, MUTED, FONT_TAG, alpha=18, edge=90)

    _dtext(draw, (PAD_X, 88), _clip_width(title, FONT_TITLE, 660), FONT_TITLE, TEXT)
    _dtext(draw, (PAD_X, 152), _clip_width(_clip(subtitle, 110), FONT_SMALL, CANVAS_W - PAD_X * 2 - 230), FONT_SMALL, MUTED)

    rule_y = band_h - 2
    segment = 460
    for i in range(segment):
        alpha = int(235 * (1 - i / segment) ** 1.5)
        draw.line((PAD_X + i, rule_y, PAD_X + i + 1, rule_y), fill=(*X_BLUE, alpha), width=3)
    draw.line((PAD_X + segment, rule_y + 1, CANVAS_W - PAD_X, rule_y + 1), fill=(*LINE, 26), width=1)


def _draw_profile(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    w: float,
    count: int,
    latest: datetime | None,
    avatar: Image.Image | None,
) -> None:
    draw.rounded_rectangle((x, y, x + w, y + PROFILE_H), radius=18, fill=(*PANEL, 246), outline=(*PANEL_EDGE, 170), width=1)
    draw.rounded_rectangle((x, y + 16, x + 6, y + PROFILE_H - 16), radius=3, fill=(*X_BLUE, 235))

    avatar_cx, avatar_cy = x + 78, y + PROFILE_H / 2
    _draw_avatar(image, draw, avatar_cx, avatar_cy, 34, "T", FONT_AVATAR, 30, avatar)

    name_x = x + 132
    _dtext(draw, (name_x, y + 32), TIBO_DISPLAY_NAME, FONT_NAME, TEXT)
    _draw_verified(draw, name_x + FONT_NAME.getlength(TIBO_DISPLAY_NAME) + 20, y + 32 + 21, r=12)
    _dtext(draw, (name_x, y + 78), f"@{TIBO_HANDLE}", FONT_SMALL, MUTED)
    _dtext(draw, (name_x, y + 108), f"https://x.com/{TIBO_HANDLE}", FONT_TINY, X_BLUE)

    _dtext(draw, (x + w - 30, y + 34), f"最近 {count} 条动态", FONT_SMALL_B, TEXT, anchor="ra")
    _dtext(draw, (x + w - 30, y + 66), f"最新发布于 {_fmt_full(latest)}", FONT_TINY, FAINT, anchor="ra")
    _dtext(draw, (x + w - 30, y + 94), "北京时间 · 按来源时间倒序", FONT_TINY, FAINT, anchor="ra")


def _draw_post_card(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    w: float,
    post: TiboPost,
    meta: dict,
    time_text: str,
    avatar: Image.Image | None,
) -> None:
    h = meta["height"]
    accent = meta["accent"]
    draw.rounded_rectangle((x, y, x + w, y + h), radius=18, fill=(*PANEL, 246), outline=(*PANEL_EDGE, 170), width=1)
    draw.rounded_rectangle((x, y + 16, x + 5, y + h - 16), radius=3, fill=(*accent, 225))

    inner_x = x + PANEL_PAD_X
    inner_w = w - PANEL_PAD_X * 2
    yy = y + 24

    # --- header row: mini avatar, name, handle, time, chip, id ---------------
    mini_cx, mini_cy = inner_x + 19, yy + 19
    _draw_avatar(image, draw, mini_cx, mini_cy, 19, "T", FONT_AVATAR_MINI, 19, avatar)
    name_x = inner_x + 52
    _dtext(draw, (name_x, yy + 3), TIBO_DISPLAY_NAME, FONT_SMALL_B, TEXT)
    _draw_verified(draw, name_x + FONT_SMALL_B.getlength(TIBO_DISPLAY_NAME) + 16, yy + 13, r=8)
    hx = name_x + FONT_SMALL_B.getlength(TIBO_DISPLAY_NAME) + 34
    _dtext(draw, (hx, yy + 6), f"@{TIBO_HANDLE}", FONT_TINY, FAINT)
    hx += FONT_TINY.getlength(f"@{TIBO_HANDLE}") + 12
    _dtext(draw, (hx, yy + 6), "·", FONT_TINY, FAINT)
    hx += 14
    _dtext(draw, (hx, yy + 6), time_text, FONT_TINY, FAINT)

    id_text = f"#{meta['post_id']}"
    id_w = FONT_TAG.getlength(id_text)
    chip_w = FONT_TAG.getlength(meta["label"]) + 22
    _chip(draw, inner_x + inner_w - id_w - 20 - chip_w, yy + 2, meta["label"], accent)
    _dtext(draw, (inner_x + inner_w, yy + 5), id_text, FONT_TINY, FAINT, anchor="ra")

    # --- original text --------------------------------------------------------
    ty = yy + 46
    for line in meta["orig_lines"]:
        _draw_mixed(draw, inner_x, ty, line, FONT_BODY, TEXT)
        ty += 34
    ty += 12

    # --- translation / interpretation blocks ---------------------------------
    if meta["trans_lines"] or meta["anal_lines"]:
        draw.line((inner_x, ty - 6, inner_x + inner_w, ty - 6), fill=(*LINE, 34), width=1)
    if meta["trans_lines"]:
        _chip(draw, inner_x, ty, "中文翻译", X_BLUE)
        by = ty + 32
        for line in meta["trans_lines"]:
            _draw_mixed(draw, inner_x, by, line, FONT_QUOTE, SUBTLE)
            by += 32
        ty = by + 14
    if meta["anal_lines"]:
        _chip(draw, inner_x, ty, "模型解读", AMBER)
        by = ty + 32
        for line in meta["anal_lines"]:
            _draw_mixed(draw, inner_x, by, line, FONT_QUOTE, SUBTLE)
            by += 32
        ty = by + 14

    # --- key phrases ----------------------------------------------------------
    if meta["phrase_chips"]:
        rows = _layout_chips(meta["phrase_chips"], FONT_TAG, inner_w, gap=10)
        px = inner_x
        for row in rows:
            for item in row:
                _chip(draw, px, ty, item, X_BLUE, FONT_TAG, alpha=16, edge=90)
                px += FONT_TAG.getlength(item) + 22 + 10
            ty += 30
            px = inner_x
        ty += 4

    # --- link row --------------------------------------------------------------
    link_y = y + h - 24 - 26
    draw.line((inner_x, link_y + 26, inner_x + inner_w, link_y + 26), fill=(*LINE, 26), width=1)
    _dtext(draw, (inner_x, link_y + 4), "原帖链接", FONT_TINY, FAINT)
    url = post.url or "https://x.com/thsottiaux"
    url_text = _clip_width(url, FONT_LINK, inner_w - 120)
    _dtext(draw, (inner_x + FONT_TINY.getlength("原帖链接") + 14, link_y + 3), url_text, FONT_LINK, X_BLUE)
    draw.line((inner_x + FONT_TINY.getlength("原帖链接") + 14, link_y + 25, inner_x + FONT_TINY.getlength("原帖链接") + 14 + FONT_LINK.getlength(url_text), link_y + 25), fill=(*X_BLUE, 120), width=1)
    _dtext(draw, (inner_x + inner_w, link_y + 5), f"#{meta['post_id']}", FONT_TINY, FAINT, anchor="ra")


def _draw_footer(draw: ImageDraw.ImageDraw, y: float) -> None:
    draw.line((PAD_X, y, CANVAS_W - PAD_X, y), fill=(*LINE, 30), width=1)
    _dtext(draw, (PAD_X, y + 18), "公开信息雷达 · 翻译与解读由模型生成，结论不代表实时在线状态或 OpenAI 官方承诺", FONT_TINY, FAINT)
    _dtext(draw, (CANVAS_W - PAD_X, y + 18), "codexradar.com · codex-reset.com", FONT_TINY, FAINT, anchor="ra")


def _render_xfeed(posts: list[TiboPost], relevance_label, title: str, subtitle: str, page: str) -> bytes:
    inner_w = CARD_W - PANEL_PAD_X * 2
    metas = [_measure_post(post, relevance_label, inner_w) for post in posts]
    body_h = PROFILE_H + PANEL_GAP + sum(meta["height"] + PANEL_GAP for meta in metas)
    height = max(430, min(MAX_HEIGHT, HEADER_H + body_h + FOOTER_H + 24))
    image = Image.new("RGB", (CANVAS_W, height), BG_TOP)
    draw = ImageDraw.Draw(image, "RGBA")
    avatar = _load_avatar()
    x_logo = _load_x_logo()
    _draw_background(draw, height)
    _draw_header(image, draw, title, subtitle, page, x_logo)

    latest = max((post.source_time for post in posts if post.source_time), default=None)
    _draw_profile(image, draw, PAD_X, float(HEADER_H), CANVAS_W - PAD_X * 2, len(posts), latest, avatar)

    y = float(HEADER_H + PROFILE_H + PANEL_GAP)
    for index, (post, meta) in enumerate(zip(posts, metas)):
        dot_cy = y + 26
        _led(draw, RAIL_X, dot_cy, meta["accent"], r=7)
        if index < len(posts) - 1:
            next_y = y + meta["height"] + PANEL_GAP + 26
            draw.line((RAIL_X, dot_cy + 10, RAIL_X, next_y), fill=(*meta["accent"], 46), width=2)
        _draw_post_card(image, draw, CARD_X, y, CARD_W, post, meta, _fmt_md(post.source_time), avatar)
        y += meta["height"] + PANEL_GAP

    _draw_footer(draw, min(y + 4, height - FOOTER_H + 4))
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def render_xfeed(
    posts: list[TiboPost],
    relevance_label,
    *,
    title: str = "Tibo 最新 X 动态",
    subtitle: str = "最近动态 · 含中文翻译与模型解读 · 按北京时间倒序",
    page: str = "",
) -> bytes:
    """Render the X-timeline style reply card for the given posts."""
    return await run_image_render(_render_xfeed, posts, relevance_label, title, subtitle, page)
