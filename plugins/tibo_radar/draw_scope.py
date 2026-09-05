"""Tibo radar card renderer v3 — "listening-post log" design.

A ground-up redesign of the reply images for the tibo command. It deliberately
does NOT mimic X's own UI (that was draw_xfeed); instead the visual identity
comes from the feature's own name — 雷达 — reimagined as a quiet radio
listening-station log sheet:

- deep petroleum-ink background, warm bone text, one phosphor-amber signal
  color plus teal for secondary data
- the header carries the signature element: a live radar SCOPE whose blips
  are the actual rendered posts — brighter blip = fresher post
- every entry is a log line: stacked operator timestamp in the left gutter,
  a 5-bar signal meter encoding relevance (direct = 5/5, indirect = 2/5),
  then 原文 quote, 「译」 translation, 「解读」 model analysis, key-phrase
  tags and the evidence link row
- entries hang off a single trace rail whose brightness decays with age

Public API mirrors draw_xfeed::

    await render_scope(posts, relevance_label, *, title=..., subtitle=..., page=...)

so switching the plugin over is a one-line import change in __init__.py.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from otae_bot.infrastructure.rendering.executor import run_image_render

from .client import TIBO_HANDLE, TIBO_PROFILE_URL
from .models import ResetEvent, TiboPost


CANVAS_W = 1080
PAD_X = 44
HEADER_H = 224
FOOTER_H = 92
ENTRY_GAP = 18
GUTTER_W = 148
PANEL_X = PAD_X + GUTTER_W + 34
PANEL_W = CANVAS_W - PAD_X - PANEL_X
PANEL_PAD_X = 28
INNER_W = PANEL_W - PANEL_PAD_X * 2
MAX_HEIGHT = 4000

RAIL_X = PAD_X + GUTTER_W + 13

# --- palette: "petroleum ink / phosphor amber" -------------------------------
BG_TOP = (11, 19, 26)        # ink
BG_BOT = (14, 25, 33)
PANEL = (18, 30, 39)         # panel surface
PANEL_EDGE = (52, 74, 88)
TEXT = (231, 238, 233)       # bone
SUBTLE = (196, 208, 206)
MUTED = (146, 162, 164)
FAINT = (100, 116, 120)
LINE = (255, 255, 255)

AMBER = (245, 168, 60)       # phosphor signal — direct relevance / sweep
TEAL = (82, 199, 180)        # secondary data — translation / indirect
GREEN = (126, 217, 140)
GRAY = (122, 138, 142)

_RELEVANCE_COLORS = {"direct": AMBER, "indirect": TEAL, "none": GRAY}
_RELEVANCE_BARS = {"direct": 5, "indirect": 2, "none": 0}
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_SCOPE_CX, _SCOPE_CY, _SCOPE_R = PAD_X + 96, HEADER_H // 2 + 6, 88


# ---------------------------------------------------------------------------
# Fonts
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


def _display_font(size: int):
    """Heavy display face for the masthead; falls back gracefully."""
    candidates = [
        Path(".runtime/HarmonyOS-Sans/HarmonyOS Sans/HarmonyOS_Sans_SC/HarmonyOS_Sans_SC_Black.ttf"),
        Path("assets/font/steamInfo/MiSans-Bold.ttf"),
        Path("C:/Windows/Fonts/msyhbd.ttc"),
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


FONT_MAST = _display_font(42)
FONT_KICKER = _font(17, bold=True)
FONT_SUB = _font(19)
FONT_TIME_BIG = _font(31, bold=True)
FONT_TIME_SMALL = _font(16, bold=True)
FONT_BODY = _font(22)
FONT_QUOTE = _font(22, light=True)
FONT_SMALL = _font(19)
FONT_SMALL_B = _font(19, bold=True)
FONT_TAG = _font(16, bold=True)
FONT_TINY = _font(16)
FONT_LINK = _font(17)
FONT_PAGE = _font(15, bold=True)


_WRAP_TOKEN = re.compile(r"[A-Za-z0-9_\-@#&%/+.:'\",;!?()\[\]{}<>=~*|]+|\s+|.", re.S)
_PHRASE_SPLIT = re.compile(r"[、，,；;/]+")


def _wrap(text: str, font, max_width: int) -> list[str]:
    """Greedy wrap that keeps latin words intact and hard-splits long tokens."""
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


def _wrap_clip(text: str, font, max_width: int, max_lines: int) -> list[str]:
    lines = _wrap(text, font, max_width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


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


def _dtext(draw, pos, text: str, font, fill, anchor: str = "la") -> None:
    if anchor == "la":
        draw.text(pos, text, font=font, fill=fill)
        return
    draw.text(pos, text, font=font, fill=fill, anchor=anchor)


def _fmt_md(value: datetime | None) -> str:
    return value.astimezone().strftime("%m-%d") if value else "-- --"


def _fmt_hm(value: datetime | None) -> str:
    return value.astimezone().strftime("%H:%M") if value else "--:--"


def _weekday(value: datetime | None) -> str:
    if value is None:
        return ""
    return _WEEKDAYS[value.astimezone().weekday()]


def _short_id(post_id: str) -> str:
    pid = str(post_id or "")
    return "#" + pid if len(pid) <= 8 else "#…" + pid[-6:]


def _split_phrases(value: str) -> list[str]:
    parts = [part.strip() for part in _PHRASE_SPLIT.split(str(value or "")) if part.strip()]
    return parts[:6]


# ---------------------------------------------------------------------------
# Signature element: the radar scope
# ---------------------------------------------------------------------------

def _draw_scope(draw, posts: list[TiboPost]) -> None:
    cx, cy, r = _SCOPE_CX, _SCOPE_CY, _SCOPE_R

    # dish face
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(15, 26, 34), outline=(*TEAL, 90), width=2)
    for rr in (r - 22, r - 46):
        draw.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=(*TEAL, 34), width=1)
    draw.line((cx - r, cy, cx + r, cy), fill=(*TEAL, 26), width=1)
    draw.line((cx, cy - r, cx, cy + r), fill=(*TEAL, 26), width=1)
    for i in range(12):
        ang = math.radians(i * 30)
        x0, y0 = cx + (r - 5) * math.cos(ang), cy + (r - 5) * math.sin(ang)
        x1, y1 = cx + (r - 1) * math.cos(ang), cy + (r - 1) * math.sin(ang)
        draw.line((x0, y0, x1, y1), fill=(*TEAL, 60), width=1)

    # sweep wedge: bright leading edge, fading tail
    lead_deg = -64.0
    steps = 46
    for i in range(steps):
        deg = lead_deg + i * 1.35
        alpha = int(120 * (1 - i / steps) ** 1.7) + 4
        ang = math.radians(deg)
        x1, y1 = cx + r * math.cos(ang), cy + r * math.sin(ang)
        draw.line((cx, cy, x1, y1), fill=(*AMBER, alpha), width=2)

    # blips: one per rendered post — newest sits on the leading edge, brightest
    ordered = sorted(
        (p for p in posts if p.source_time is not None),
        key=lambda p: p.source_time,
        reverse=True,
    )[:6]
    fallback = [p for p in posts if p.source_time is None][:6]
    slots = [lead_deg + 14, lead_deg - 38, lead_deg - 78, lead_deg + 52, lead_deg - 118, lead_deg + 96]
    radii = (r - 16, r - 32, r - 50, r - 24, r - 44, r - 58)
    for i, post in enumerate([*ordered, *fallback]):
        ang = math.radians(slots[i % len(slots)])
        br = radii[i % len(radii)]
        bx, by = cx + br * math.cos(ang), cy + br * math.sin(ang)
        fade = 1.0 if i == 0 else max(0.28, 0.85 - i * 0.16)
        color = AMBER if post.relevance == "direct" else (TEAL if post.relevance == "indirect" else GRAY)
        core = tuple(int(c * fade + 20 * (1 - fade)) for c in color)
        for radius, alpha in ((11, int(36 * fade)), (7, int(90 * fade)), (4, min(255, int(255 * fade)))):
            draw.ellipse((bx - radius, by - radius, bx + radius, by + radius), fill=(*core, alpha))


# ---------------------------------------------------------------------------
# Entry measurement
# ---------------------------------------------------------------------------

def _layout_tags(items: list[str], font, max_width: float, gap: float = 14) -> list[list[str]]:
    rows: list[list[str]] = []
    current: list[str] = []
    width = 0.0
    for item in items:
        w = font.getlength(item) + 24
        if current and width + gap + w > max_width:
            rows.append(current)
            current, width = [], 0.0
        current.append(item)
        width += w + gap
    if current:
        rows.append(current)
    return rows


def _measure_entry(post: TiboPost, relevance_label) -> dict:
    relevance = post.relevance if post.relevance in _RELEVANCE_COLORS else "none"
    accent = _RELEVANCE_COLORS[relevance]
    label = relevance_label(relevance)

    orig_lines = _wrap_clip(_clip(post.text, 560), FONT_QUOTE, INNER_W - 18, 7)
    trans_lines = _wrap_clip(post.translation or "", FONT_BODY, INNER_W - 48, 5)
    anal_lines = _wrap_clip(post.analysis or "", FONT_BODY, INNER_W - 48, 5)
    phrases = _split_phrases(post.phrases or "")
    tag_rows = _layout_tags(phrases, FONT_TAG, INNER_W - 8) if phrases else []

    h = 22                       # top pad
    h += 30                      # meta row (meter + label + id)
    h += 14 + len(orig_lines) * 33 + 10
    blocks = [b for b in (trans_lines, anal_lines) if b]
    for block in blocks:
        h += 34 + len(block) * 31
    if len(blocks) == 2:
        h += 12
    if tag_rows:
        h += 14 + len(tag_rows) * 28
    h += 16 + 24                 # link row
    h += 22                      # bottom pad
    return {
        "relevance": relevance,
        "accent": accent,
        "label": label,
        "orig_lines": orig_lines,
        "trans_lines": trans_lines,
        "anal_lines": anal_lines,
        "tag_rows": tag_rows,
        "height": h,
        "bars": _RELEVANCE_BARS[relevance],
    }


# ---------------------------------------------------------------------------
# Painting
# ---------------------------------------------------------------------------

def _draw_background(image: Image.Image) -> None:
    w, height = image.size
    grad = Image.new("RGB", (1, height))
    for y in range(height):
        ratio = y / max(1, height - 1)
        grad.putpixel((0, y), (
            int(BG_TOP[0] + (BG_BOT[0] - BG_TOP[0]) * ratio),
            int(BG_TOP[1] + (BG_BOT[1] - BG_TOP[1]) * ratio),
            int(BG_TOP[2] + (BG_BOT[2] - BG_TOP[2]) * ratio),
        ))
    image.paste(grad.resize((w, height)), (0, 0))
    # faint plotting-paper dots below the header
    draw = ImageDraw.Draw(image, "RGBA")
    step = 54
    for gy in range(HEADER_H + 40, height - FOOTER_H, step):
        for gx in range(step // 2, w, step):
            draw.point((gx, gy), fill=(*TEAL, 22))


def _draw_header(draw, title: str, subtitle: str, page: str, count, posts: list[TiboPost], kicker: str = "TIBO RADAR · SIGNAL LOG") -> None:
    band_h = HEADER_H - 14
    for y in range(band_h):
        ratio = y / max(1, band_h - 1)
        dim = 1 - ratio * 0.55
        draw.line((0, y, CANVAS_W, y), fill=(
            int(BG_TOP[0] * dim), int(BG_TOP[1] * dim), int(BG_TOP[2] * dim),
        ))

    _draw_scope(draw, posts)

    mx = _SCOPE_CX + _SCOPE_R + 34

    # kicker — letter-spaced latin
    kick = kicker
    kx = float(mx)
    for ch in kick:
        _dtext(draw, (kx, 34), ch, FONT_KICKER, AMBER)
        kx += FONT_KICKER.getlength(ch) + 2.4
    draw.line((mx, 66, kx - 2.4, 66), fill=(*AMBER, 70), width=1)

    _dtext(draw, (mx - 3, 78), _clip_width(title, FONT_MAST, CANVAS_W - mx - PAD_X + 20), FONT_MAST, TEXT)
    _dtext(draw, (mx, 142), "@" + TIBO_HANDLE + " · " + TIBO_PROFILE_URL, FONT_LINK, TEAL)
    _dtext(draw, (mx, 168), _clip_width(subtitle, FONT_SMALL, CANVAS_W - mx - PAD_X), FONT_SMALL, MUTED)

    # page / count block, top right
    if page:
        label = page.upper()
        w = FONT_PAGE.getlength(label) + 26
        px, py = CANVAS_W - PAD_X - w, 30
        draw.rounded_rectangle((px, py, px + w, py + 28), radius=6, outline=(*FAINT, 130), width=1)
        _dtext(draw, (px + w / 2, py + 5), label, FONT_PAGE, FAINT, anchor="ma")
    if count is not None:
        _dtext(draw, (CANVAS_W - PAD_X, 68), str(count) + " 条动态", FONT_SMALL_B, TEXT, anchor="ra")
        _dtext(draw, (CANVAS_W - PAD_X, 94), "北京时间 · 倒序", FONT_TINY, FAINT, anchor="ra")

    # header rule
    rule_y = HEADER_H - 12
    segment = 520
    for i in range(segment):
        alpha = int(200 * (1 - i / segment) ** 1.4)
        draw.line((PAD_X + i, rule_y, PAD_X + i + 1, rule_y), fill=(*AMBER, alpha), width=2)
    draw.line((PAD_X + segment, rule_y, CANVAS_W - PAD_X, rule_y), fill=(*LINE, 22), width=1)


def _draw_meter(draw, x: float, y: float, filled: int, color) -> float:
    bar_w, bar_h, gap = 15, 17, 6
    for i in range(5):
        bx = x + i * (bar_w + gap)
        if i < filled:
            draw.rounded_rectangle((bx, y, bx + bar_w, y + bar_h), radius=3, fill=(*color, 235))
        else:
            draw.rounded_rectangle((bx, y, bx + bar_w, y + bar_h), radius=3, outline=(*FAINT, 120), width=1)
    return 5 * bar_w + 4 * gap


def _draw_gutter(draw, x: float, y: float, post: TiboPost) -> None:
    _dtext(draw, (x, y + 6), _fmt_md(post.source_time), FONT_TIME_SMALL, MUTED)
    _dtext(draw, (x, y + 30), _fmt_hm(post.source_time), FONT_TIME_BIG, TEXT)
    wd = _weekday(post.source_time)
    if wd:
        _dtext(draw, (x + 2, y + 72), wd, FONT_TINY, FAINT)


def _draw_tag(draw, x: float, y: float, text: str, color) -> float:
    tw = FONT_TAG.getlength(text) + 20
    th = FONT_TAG.size + 12
    draw.rounded_rectangle((x, y, x + tw, y + th), radius=4, fill=(*color, 36), outline=(*color, 170), width=1)
    _dtext(draw, (x + 10, y + 5), text, FONT_TAG, color)
    return tw


def _draw_dotted(draw, x0: float, x1: float, y: float, color, alpha: int = 60) -> None:
    x = x0
    while x < x1:
        draw.line((x, y, min(x + 5, x1), y), fill=(*color, alpha), width=1)
        x += 11


def _draw_entry_panel(draw, x: float, y: float, post: TiboPost, meta: dict, index: int) -> None:
    h = meta["height"]
    accent = meta["accent"]
    draw.rounded_rectangle((x, y, x + PANEL_W, y + h), radius=14, fill=(*PANEL, 250), outline=(*PANEL_EDGE, 150), width=1)

    ix = x + PANEL_PAD_X
    yy = y + 22

    # --- meta row: meter + relevance label + short id ------------------------
    mw = _draw_meter(draw, ix, yy, meta["bars"], accent)
    _dtext(draw, (ix + mw + 14, yy + 1), meta["label"], FONT_SMALL_B, accent)
    _dtext(draw, (ix + INNER_W, yy + 3), _short_id(post.post_id), FONT_TINY, FAINT, anchor="ra")
    yy += 44

    # --- original quote -------------------------------------------------------
    qx = ix + 2
    quote_h = len(meta["orig_lines"]) * 33 - 12
    draw.rounded_rectangle((ix, yy + 2, ix + 3, yy + 2 + max(12, quote_h)), radius=2, fill=(*accent, 190))
    for line in meta["orig_lines"]:
        _dtext(draw, (qx + 14, yy), line, FONT_QUOTE, TEXT)
        yy += 33
    yy += 12

    # --- translation ----------------------------------------------------------
    if meta["trans_lines"]:
        _draw_dotted(draw, ix, ix + INNER_W, yy, LINE, 40)
        yy += 12
        tag_w = _draw_tag(draw, ix, yy, "译", TEAL)
        tx, ty = ix + tag_w + 16, yy - 1
        for line in meta["trans_lines"]:
            _dtext(draw, (tx, ty), line, FONT_BODY, SUBTLE)
            ty += 31
        yy = max(ty, yy + FONT_TAG.size + 16) + 2

    # --- interpretation -------------------------------------------------------
    if meta["anal_lines"]:
        _draw_dotted(draw, ix, ix + INNER_W, yy, LINE, 40)
        yy += 12
        tag_w = _draw_tag(draw, ix, yy, "解读", AMBER)
        ax, ay = ix + tag_w + 16, yy - 1
        for line in meta["anal_lines"]:
            _dtext(draw, (ax, ay), line, FONT_BODY, SUBTLE)
            ay += 31
        yy = max(ay, yy + FONT_TAG.size + 16) + 2

    # --- phrase tags ----------------------------------------------------------
    if meta["tag_rows"]:
        yy += 6
        for row in meta["tag_rows"]:
            px = float(ix)
            for item in row:
                _dtext(draw, (px, yy), "[", FONT_TAG, (*accent, 170))
                _dtext(draw, (px + 9, yy), item, FONT_TAG, TEAL)
                _dtext(draw, (px + 9 + FONT_TAG.getlength(item), yy), "]", FONT_TAG, (*accent, 170))
                px += FONT_TAG.getlength(item) + 26
            yy += 28
        yy += 4

    # --- link row -------------------------------------------------------------
    link_y = y + h - 22 - 22
    draw.line((ix, link_y - 10, ix + INNER_W, link_y - 10), fill=(*LINE, 26), width=1)
    _dtext(draw, (ix, link_y), "↗", FONT_LINK, TEAL)
    url_text = _clip_width((post.url or TIBO_PROFILE_URL).removeprefix("https://"), FONT_LINK, INNER_W - 190)
    _dtext(draw, (ix + 24, link_y), url_text, FONT_LINK, TEAL)
    _dtext(draw, (ix + INNER_W, link_y + 2), "证据链接 · " + str(index + 1).zfill(2), FONT_TINY, FAINT, anchor="ra")


def _draw_footer(draw, y: float) -> None:
    draw.line((PAD_X, y, CANVAS_W - PAD_X, y), fill=(*LINE, 28), width=1)
    _dtext(draw, (PAD_X, y + 16), "● 回波越亮越新 · 信号条 = 相关强度", FONT_TINY, FAINT)
    _dtext(draw, (PAD_X, y + 40), "数据源 codexradar.com · codex-reset.com", FONT_TINY, FAINT)
    _dtext(draw, (CANVAS_W - PAD_X, y + 16), "译文与解读由模型生成 · 不构成任何官方承诺", FONT_TINY, FAINT, anchor="ra")
    _dtext(draw, (CANVAS_W - PAD_X, y + 40), "公开信息整理 · 仅供参考", FONT_TINY, FAINT, anchor="ra")


def _render_scope(posts: list[TiboPost], relevance_label, title: str, subtitle: str, page: str) -> bytes:
    metas = [_measure_entry(post, relevance_label) for post in posts]
    body_h = sum(meta["height"] + ENTRY_GAP for meta in metas)
    height = max(520, min(MAX_HEIGHT, HEADER_H + body_h + FOOTER_H + 26))

    image = Image.new("RGB", (CANVAS_W, height), BG_TOP)
    draw = ImageDraw.Draw(image, "RGBA")
    _draw_background(image)
    _draw_header(draw, title, subtitle, page, len(posts), posts)

    y = float(HEADER_H + 14)
    for index, (post, meta) in enumerate(zip(posts, metas)):
        node_cy = y + 40
        fade = max(0.3, 1.0 - index * 0.22)
        if index < len(posts) - 1:
            next_node_cy = y + meta["height"] + ENTRY_GAP + 40
            draw.line((RAIL_X, node_cy + 12, RAIL_X, next_node_cy - 12), fill=(*AMBER, max(18, int(70 * fade))), width=2)
        color = meta["accent"]
        for radius, alpha in ((10, int(30 * fade)), (6, int(230 * fade))):
            draw.ellipse((RAIL_X - radius, node_cy - radius, RAIL_X + radius, node_cy + radius), fill=(*color, alpha))

        _draw_gutter(draw, PAD_X, y, post)
        _draw_entry_panel(draw, PANEL_X, y, post, meta, index)
        y += meta["height"] + ENTRY_GAP

    _draw_footer(draw, min(y + 2, height - FOOTER_H + 6))
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def render_scope(
    posts: list[TiboPost],
    relevance_label,
    *,
    title: str = "Tibo 最新 X 动态",
    subtitle: str = "最近动态 · 英文原文 + 中文翻译 + 模型解读 · 按北京时间倒序",
    page: str = "",
) -> bytes:


    """Render the listening-log card for the given posts."""
    return await run_image_render(_render_scope, posts, relevance_label, title, subtitle, page)


# ===========================================================================
# Section cards — 总览 / 状态 / 最近 / 历史
# Drop-in replacement for draw_x: same CardSection API, same line grammar
# (kv rows, hero status, stats tiles, hour bars, source health, PT block),
# repainted in the listening-log identity.
# ===========================================================================

ROW_GAP = 12
CARD_HEADER_H = 86
CARD_BOTTOM_PAD = 26
TILE_BG = (13, 21, 28)

# Compatibility palette exports, remapped to the new identity.
CYAN = TEAL
X_BLUE = TEAL
RED = (236, 108, 98)

_FONT_SEC_TITLE = _font(25, bold=True)
FONT_BODY_B = _font(22, bold=True)
FONT_HERO = _font(29, bold=True)
FONT_BIG = _font(38, bold=True)
FONT_PT = _font(28, bold=True)


@dataclass(slots=True)
class CardSection:
    title: str
    lines: list[str]
    accent: tuple[int, int, int] = TEAL


_STATUS_STYLES = {
    "直接相关": AMBER,
    "间接相关": TEAL,
    "无重置信号": GRAY,
    "已确认完成": GREEN,
    "官方重置预告": AMBER,
    "预计时间窗口": TEAL,
    "疑似发生": AMBER,
    "预告未兑现/已否定": RED,
    "未确认": MUTED,
    "官方重置预告窗口进行中": GREEN,
    "预告窗口已过，尚未核验完成": AMBER,
    "疑似重置信号": AMBER,
    "预告未被核验": RED,
    "暂无进行中的重置信号": TEAL,
}
_PHASE_STYLES = {"睡觉": GRAY, "上班": TEAL, "可能在线": GREEN}
_QUOTE_KEYS = {"原文", "翻译", "解读", "摘要", "证据", "证据原文"}
_BOLD_KEYS = {"距今"}
_KEY_COLORS = {"原文": MUTED, "翻译": TEAL, "解读": AMBER, "摘要": TEAL, "证据": TEAL, "证据原文": TEAL}
_KNOWN_KEYS = _QUOTE_KEYS | {"时间", "距今", "确认", "确认状态", "事件", "完成/确认时间", "完成时间", "预告窗口", "窗口", "来源", "状态", "说明"}

_NUM_HEAD = re.compile(r"^#(\d+)\s+(.+?)\s{2,}(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})(?:\s*北京时间)?$")
_KV_LINE = re.compile(r"^([^：:]{1,14})[：:]\s*(.*)$", re.S)
_FEED_HEAD = re.compile(r"^(直接相关|间接相关|无重置信号)\s*·\s*(\d{2}-\d{2}\s+\d{2}:\d{2})\s*·\s*(.+)$")
_URL_TOKEN = re.compile(r"(https?://[^\s，。；、]+)")
_HOURS_RE = re.compile(r"(\d{1,2}):00\((\d+)\)")
_AMOUNT_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*(\S*)$")


def _led(draw, cx: float, cy: float, color, r: float = 7) -> None:
    for radius, alpha in ((r + 9, 22), (r + 4, 48), (r, 235)):
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(*color, alpha))


def _pill(draw, x: float, y: float, text: str, color, *, font=None, fill_a: int = 30, edge_a: int = 150) -> float:
    font = font or FONT_TAG
    tw = font.getlength(text) + 22
    th = font.size + 12
    draw.rounded_rectangle((x, y, x + tw, y + th), radius=th / 2, fill=(*color, fill_a), outline=(*color, edge_a), width=1)
    _dtext(draw, (x + 11, y + 5), text, font, color)
    return tw


def _draw_mixed(draw, x: float, y: float, text: str, font, color, link_color=TEAL) -> None:
    pos = x
    for part in _URL_TOKEN.split(text):
        if not part:
            continue
        fill = link_color if part.startswith("http") else color
        _dtext(draw, (pos, y), part, font, fill)
        pos += font.getlength(part)


def _split_amount(value: str):
    m = _AMOUNT_RE.match(str(value or "").strip())
    return (m.group(1), m.group(2) or "") if m else (None, "")


def _parse_source(line: str) -> dict:
    text = str(line or "").strip()
    name, _, status = text.partition("：")
    if not status:
        name, _, status = text.partition(":")
    name = name.strip() or text or "未知来源"
    status = status.strip() or "状态未知"
    if "从未成功" in status:
        color = RED
    elif "陈旧" in status or "失败" in status:
        color = AMBER
    else:
        color = GREEN
    return {"name": name, "status": status, "color": color}


def _classify_block(lines: list[str], inner_w: int) -> list[dict]:
    classified: list[dict] = []
    indent = 0
    prev_kind = ""
    for raw in lines:
        text = str(raw).strip()
        head = _NUM_HEAD.match(text)
        if head:
            label = head.group(2).strip()
            classified.append({"kind": "tl", "num": head.group(1), "label": label, "time": head.group(3), "color": _STATUS_STYLES.get(label, TEAL), "indent": 0})
            indent = 42
            prev_kind = "tl"
            continue
        feed = _FEED_HEAD.match(text)
        if feed:
            classified.append({"kind": "feed", "label": feed.group(1), "time": feed.group(2), "pid": feed.group(3).strip(), "color": _STATUS_STYLES.get(feed.group(1), TEAL), "indent": 0})
            indent = 28
            prev_kind = "feed"
            continue
        if text.startswith("http") and " " not in text:
            classified.append({"kind": "link", "url": text, "indent": indent})
            prev_kind = "link"
            continue
        kv = _KV_LINE.match(text)
        if kv and kv.group(1) in _KNOWN_KEYS:
            key, value = kv.group(1), kv.group(2).strip()
            kind = "qkv" if key in _QUOTE_KEYS else "kv"
            classified.append({"kind": kind, "key": key, "value": value, "indent": indent})
            prev_kind = kind
            continue
        classified.append({"kind": "text", "value": text, "indent": indent, "subtle": prev_kind in {"tl", "feed"}})
        prev_kind = "text"

    key_w = 0.0
    for row in classified:
        if row["kind"] == "kv":
            key_w = max(key_w, FONT_SMALL.getlength(row["key"]))
        elif row["kind"] == "qkv":
            key_w = max(key_w, FONT_TAG.getlength(row["key"]) + 22)
    key_col = int(min(190, key_w + 18)) if key_w else 0

    rows: list[dict] = []
    for row in classified:
        kind = row["kind"]
        ind = row.get("indent", 0)
        avail = max(160, inner_w - ind)
        if kind == "text":
            font = FONT_QUOTE if row["subtle"] else FONT_BODY
            line_h = 33 if row["subtle"] else 36
            wrapped = _wrap(row["value"], font, avail)
            rows.append({**row, "lines": wrapped, "h": len(wrapped) * line_h + 2})
        elif kind == "link":
            rows.append({**row, "h": 36})
        elif kind in {"kv", "qkv"}:
            font = FONT_QUOTE if kind == "qkv" else FONT_BODY
            line_h = 33 if kind == "qkv" else 36
            wrapped = _wrap(row["value"], font, max(120, avail - key_col))
            rows.append({**row, "lines": wrapped, "key_col": key_col, "h": max(34, len(wrapped) * line_h) + 10})
        else:
            rows.append({**row, "h": 48 if kind == "tl" else 44})
    return rows


def _build_rows(title: str, lines: list[str], inner_w: int):
    rows: list[dict] = []
    hero_color = None

    def add(kind, height, **data):
        rows.append({"kind": kind, "h": height, **data})

    if not lines:
        add("empty", 48)
        return rows, hero_color

    if title.endswith("状态") and lines[0] in _STATUS_STYLES:
        hero_color = _STATUS_STYLES[lines[0]]
        detail = _wrap(lines[1], FONT_SMALL, inner_w - 40) if len(lines) > 1 else []
        add("hero", 48 + len(detail) * 29 + (8 if detail else 0), label=lines[0], detail=detail, color=hero_color)
        rows.extend(_classify_block(lines[2:], inner_w))
        return rows, hero_color

    if "统计" in title:
        tiles = []
        hour_data = None
        rest = []
        for line in lines:
            matched = False
            kv = _KV_LINE.match(str(line).strip())
            if kv:
                key, value = kv.group(1), kv.group(2).strip()
                if key in {"已确认样本", "最近间隔"}:
                    amount, unit = _split_amount(value)
                    if amount:
                        caption = "已确认重置样本" if key == "已确认样本" else "最近一次间隔"
                        tiles.append((caption, amount, unit or ""))
                        matched = True
                elif key == "北京时间小时分布":
                    parsed = {int(h): int(c) for h, c in _HOURS_RE.findall(value)}
                    if parsed:
                        hour_data = parsed
                        matched = True
            if not matched:
                rest.append(str(line))
        if tiles:
            add("tiles", 108, tiles=tiles)
        if hour_data:
            add("bars", 134, data=hour_data)
        if rest:
            rows.extend(_classify_block(rest, inner_w))
        if not rows:
            add("empty", 48)
        return rows, hero_color

    if "来源健康" in title:
        add("sources", len(lines) * 38, entries=[_parse_source(line) for line in lines])
        return rows, hero_color

    if "PT 时区" in title:
        head = str(lines[0] or "")
        time_part, _, phase = head.partition(" · ")
        note = _wrap(lines[1], FONT_TINY, inner_w - 40) if len(lines) > 1 else []
        add("pt", 48 + len(note) * 24 + (12 if note else 0), time=time_part.strip(), phase=phase.strip(), note=note)
        return rows, hero_color

    rows.extend(_classify_block(lines, inner_w))
    return rows, hero_color


def _layout_section(section, inner_w: int) -> dict:
    title = str(section.title or "")
    accent = section.accent if isinstance(section.accent, tuple) and len(section.accent) == 3 else TEAL
    lines = [str(line) for line in (section.lines or [])]
    rows, hero_color = _build_rows(title, lines, inner_w)
    body_h = sum(row["h"] for row in rows) + ROW_GAP * (len(rows) - 1)
    return {"title": title, "accent": accent, "rows": rows, "height": CARD_HEADER_H + body_h + CARD_BOTTOM_PAD, "hero": hero_color}


def _draw_tiles_v3(draw, x: float, y: float, w: float, tiles) -> None:
    gap = 16
    tile_w = (w - gap * (len(tiles) - 1)) / max(1, len(tiles))
    for index, (caption, value, unit) in enumerate(tiles):
        tx = x + index * (tile_w + gap)
        draw.rounded_rectangle((tx, y, tx + tile_w, y + 96), radius=12, fill=(*TILE_BG, 240), outline=(*PANEL_EDGE, 140), width=1)
        draw.rounded_rectangle((tx, y + 16, tx + 3, y + 80), radius=2, fill=(*AMBER, 190))
        _dtext(draw, (tx + 20, y + 14), caption, FONT_TINY, MUTED)
        _dtext(draw, (tx + 20, y + 38), value, FONT_BIG, TEXT)
        if unit:
            _dtext(draw, (tx + 26 + FONT_BIG.getlength(value), y + 58), unit, FONT_TINY, MUTED)


def _draw_bars_v3(draw, x: float, y: float, w: float, data: dict) -> None:
    _dtext(draw, (x + w, y + 2), "北京时间 · 按小时分布", FONT_TINY, FAINT, anchor="ra")
    slots, gap = 24, 6
    bar_w = (w - gap * (slots - 1)) / slots
    max_value = max(data.values()) or 1
    base = y + 100
    for hour in range(slots):
        value = data.get(hour, 0)
        if value <= 0:
            continue
        bar_h = max(4, round(value / max_value * 78))
        bx = x + hour * (bar_w + gap)
        hot = value == max_value
        color = AMBER if hot else TEAL
        draw.rounded_rectangle((bx, base - bar_h, bx + bar_w, base), radius=3, fill=(*color, 240 if hot else 140))
        if hot:
            _dtext(draw, (bx + bar_w / 2, base - bar_h - 22), str(value), FONT_TINY, AMBER, anchor="ma")
    draw.line((x, base + 1, x + w, base + 1), fill=(*LINE, 34), width=1)
    for hour in (0, 6, 12, 18, 23):
        cx = x + hour * (bar_w + gap) + bar_w / 2
        _dtext(draw, (cx, base + 8), f"{hour:02d}:00", FONT_TINY, FAINT, anchor="ma")


def _draw_row_v3(draw, row: dict, x: float, y: float, w: float) -> None:
    kind = row["kind"]
    if kind == "hero":
        _led(draw, x + 13, y + 17, row["color"])
        _dtext(draw, (x + 40, y), row["label"], FONT_HERO, row["color"])
        yy = y + 50
        for line in row["detail"]:
            _dtext(draw, (x + 40, yy), line, FONT_SMALL, MUTED)
            yy += 29
    elif kind == "tiles":
        _draw_tiles_v3(draw, x, y, w, row["tiles"])
    elif kind == "bars":
        _draw_bars_v3(draw, x, y, w, row["data"])
    elif kind == "sources":
        for index, entry in enumerate(row["entries"]):
            cy = y + index * 38 + 19
            _led(draw, x + 7, cy, entry["color"], r=5)
            _dtext(draw, (x + 26, cy - 12), entry["name"], FONT_SMALL_B, TEXT)
            _dtext(draw, (x + 26 + FONT_SMALL_B.getlength(entry["name"]) + 16, cy - 11), entry["status"], FONT_SMALL, MUTED)
    elif kind == "pt":
        _dtext(draw, (x, y), row["time"], FONT_PT, TEXT)
        if row["phase"]:
            phase = row["phase"]
            color = next((c for prefix, c in _PHASE_STYLES.items() if phase.startswith(prefix)), TEAL)
            _pill(draw, x + FONT_PT.getlength(row["time"]) + 18, y - 2, phase, color)
        yy = y + 50
        for line in row["note"]:
            _dtext(draw, (x, yy), line, FONT_TINY, FAINT)
            yy += 24
    elif kind == "tl":
        cy = y + 22
        color = row["color"]
        draw.ellipse((x, cy - 15, x + 30, cy + 15), fill=(*color, 30), outline=(*color, 180), width=1)
        _dtext(draw, (x + 15, cy), row["num"], FONT_TAG, TEXT, anchor="mm")
        _pill(draw, x + 42, cy - 14, row["label"], color)
        _dtext(draw, (x + w, cy), row["time"], FONT_SMALL, FAINT, anchor="rm")
    elif kind == "feed":
        chip_w = _pill(draw, x, y, row["label"], row["color"])
        _dtext(draw, (x + chip_w + 14, y + 6), row["time"], FONT_SMALL, MUTED)
        _dtext(draw, (x + w, y + 6), "#" + row["pid"], FONT_SMALL, FAINT, anchor="ra")
    elif kind == "kv":
        _dtext(draw, (x + row["indent"], y + 5), row["key"], FONT_SMALL, MUTED)
        font = FONT_BODY_B if row["key"] in _BOLD_KEYS else FONT_BODY
        yy = y
        for line in row["lines"]:
            _draw_mixed(draw, x + row["indent"] + row["key_col"], yy, line, font, TEXT)
            yy += 36
    elif kind == "qkv":
        _draw_tag(draw, x + row["indent"], y, row["key"], _KEY_COLORS.get(row["key"], MUTED))
        yy = y + 3
        for line in row["lines"]:
            _draw_mixed(draw, x + row["indent"] + row["key_col"], yy, line, FONT_QUOTE, SUBTLE)
            yy += 33
    elif kind == "link":
        url = row["url"]
        _dtext(draw, (x + row["indent"], y + 2), url, FONT_SMALL, TEAL)
        draw.line((x + row["indent"], y + 30, x + row["indent"] + FONT_SMALL.getlength(url), y + 30), fill=(*TEAL, 110), width=1)
    elif kind == "text":
        font = FONT_QUOTE if row["subtle"] else FONT_BODY
        color = SUBTLE if row["subtle"] else TEXT
        yy = y
        for line in row["lines"]:
            _draw_mixed(draw, x + row["indent"], yy, line, font, color)
            yy += 33 if row["subtle"] else 36
    elif kind == "empty":
        _dtext(draw, (x + w / 2, y + 12), "— 暂无记录 —", FONT_SMALL, FAINT, anchor="mm")


def _draw_panel_v3(draw, x: float, y: float, w: float, panel: dict) -> None:
    height = panel["height"]
    accent = panel["hero"] or panel["accent"]
    draw.rounded_rectangle((x, y, x + w, y + height), radius=14, fill=(*PANEL, 250), outline=(*PANEL_EDGE, 150), width=1)
    if panel["hero"]:
        draw.rounded_rectangle((x, y, x + w, y + height), radius=14, fill=(*panel["hero"], 14), outline=(*panel["hero"], 120), width=1)
    draw.rounded_rectangle((x, y + 14, x + 5, y + height - 14), radius=3, fill=(*accent, 225))

    ix = x + PANEL_PAD_X
    iw = w - PANEL_PAD_X * 2
    draw.rounded_rectangle((ix, y + 26, ix + 5, y + 52), radius=2, fill=(*accent, 235))
    _dtext(draw, (ix + 17, y + 24), _clip_width(panel["title"], _FONT_SEC_TITLE, iw - 30), _FONT_SEC_TITLE, TEXT)
    _draw_dotted(draw, ix, ix + iw, y + 68, LINE, 36)

    yy = float(y + CARD_HEADER_H)
    conn_x = None
    conn_y = 0.0
    for row in panel["rows"]:
        if conn_x is not None:
            draw.line((conn_x, conn_y, conn_x, yy), fill=(*panel["accent"], 50), width=2)
        _draw_row_v3(draw, row, ix, yy, iw)
        if row["kind"] == "tl":
            conn_x, conn_y = ix + 15, yy + 36
        elif conn_x is not None and row["kind"] in {"kv", "qkv", "text", "link"}:
            conn_y = yy + row["h"]
        else:
            conn_x = None
        yy += row["h"] + ROW_GAP


def _render_card(title: str, subtitle: str, sections: list, page: str) -> bytes:
    inner_w = CANVAS_W - PAD_X * 2 - PANEL_PAD_X * 2
    panels = [_layout_section(section, inner_w) for section in sections]
    body_h = sum(panel["height"] + ENTRY_GAP for panel in panels)
    height = max(430, min(MAX_HEIGHT, HEADER_H + body_h + FOOTER_H + 26))
    image = Image.new("RGB", (CANVAS_W, height), BG_TOP)
    draw = ImageDraw.Draw(image, "RGBA")
    _draw_background(image)
    _draw_header(draw, title, subtitle, page, None, [], kicker="TIBO RADAR · STATION LOG")
    y = float(HEADER_H + 14)
    for panel in panels:
        _draw_panel_v3(draw, PAD_X, y, CANVAS_W - PAD_X * 2, panel)
        y += panel["height"] + ENTRY_GAP
    _draw_footer(draw, min(y + 2, height - FOOTER_H + 6))
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


async def render_card(title: str, subtitle: str, sections: list, *, page: str = "") -> bytes:
    """Render a section card (总览/状态/最近/历史) in the listening-log identity."""
    return await run_image_render(_render_card, title, subtitle, sections, page)


def _format_time(value) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M") if value else "时间未知"


def post_sections(posts: list[TiboPost], relevance_label) -> list[CardSection]:
    sections: list[CardSection] = []
    for index, post in enumerate(posts, 1):
        lines = [
            f"#{index}  {relevance_label(post.relevance)}  {_format_time(post.source_time)} 北京时间",
            f"原文：{_clip(post.text)}",
        ]
        if post.translation:
            lines.append(f"翻译：{_clip(post.translation)}")
        if post.analysis:
            lines.append(f"解读：{_clip(post.analysis)}")
        sections.append(CardSection(f"Tibo 动态 · {post.post_id}", lines, AMBER if post.relevance == "direct" else TEAL))
    return sections


def event_sections(events: list[ResetEvent], event_label) -> list[CardSection]:
    sections: list[CardSection] = []
    for index, event in enumerate(events, 1):
        at = event.effective_at or event.announced_at
        summary = event.localized_summary or event.summary
        lines = [
            f"#{index}  {event_label(event.status)}  {_format_time(at)}",
            f"摘要：{_clip(summary)}",
            f"证据：{event.source_label or event.source or '公开来源'} · {'预告窗口' if event.preview else '非预告'}",
        ]
        accent = GREEN if event.status == "confirmed" else RED if event.status == "rejected" else AMBER
        sections.append(CardSection(f"重置事件 · {event.event_id}", lines, accent))
    return sections
