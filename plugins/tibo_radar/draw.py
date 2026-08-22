"""Codex Radar card renderer — mission-control style dashboard cards for QQ.

Redesigned visual language:
- deep-space navy gradient with a radar sweep motif in the header
- rounded section panels instead of flat text blocks
- hero status panel with a glowing LED for the current reset state
- stat tiles + a 24h distribution bar chart for history statistics
- timeline rows with numbered badges and colored status chips
- key/value rows with aligned columns and quote-styled evidence text

The public API (``render_card``, ``CardSection``, ``post_sections``,
``event_sections`` and the legacy color constants) is unchanged, so callers
keep working without modification.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from utils.image_executor import run_image_render

from .models import ResetEvent, TiboPost


CANVAS_W = 1080
PAD_X = 36
PANEL_GAP = 18
PANEL_PAD_X = 26
ROW_GAP = 12
HEADER_H = 206
FOOTER_H = 74

# Legacy palette names kept for compatibility with existing imports.
BG = (8, 16, 31)
PANEL = (17, 30, 53)
PANEL_ALT = (21, 39, 65)
TEXT = (233, 240, 250)
MUTED = (148, 166, 196)
CYAN = (66, 214, 242)
GREEN = (64, 220, 156)
AMBER = (252, 190, 92)
RED = (250, 110, 124)
LINE = (48, 72, 105)

# Extended palette.
BG_TOP = (6, 12, 24)
BG_BOT = (13, 25, 46)
PANEL_BORDER = (47, 69, 104)
SUBTLE = (201, 212, 231)
FAINT = (108, 126, 156)
BLUE = (126, 174, 255)


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


FONT_TITLE = _font(46, bold=True)
FONT_KICKER = _font(19, bold=True)
FONT_SECTION = _font(26, bold=True)
FONT_HERO = _font(31, bold=True)
FONT_BIG = _font(38, bold=True)
FONT_PT = _font(29, bold=True)
FONT_BODY = _font(23)
FONT_BODY_B = _font(23, bold=True)
FONT_QUOTE = _font(22, light=True)
FONT_SMALL = _font(20)
FONT_SMALL_B = _font(20, bold=True)
FONT_TAG = _font(17, bold=True)
FONT_TINY = _font(17)


_STATUS_STYLES = {
    "直接相关": GREEN,
    "间接相关": AMBER,
    "无重置信号": FAINT,
    "已确认完成": GREEN,
    "官方重置预告": BLUE,
    "预计时间窗口": CYAN,
    "疑似发生": AMBER,
    "预告未兑现/已否定": RED,
    "未确认": MUTED,
    "官方重置预告窗口进行中": GREEN,
    "预告窗口已过，尚未核验完成": AMBER,
    "疑似重置信号": AMBER,
    "预告未被核验": RED,
    "暂无进行中的重置信号": CYAN,
}

_PHASE_STYLES = {"睡觉": BLUE, "上班": CYAN, "可能在线": GREEN}
_QUOTE_KEYS = {"原文", "翻译", "解读", "摘要", "证据", "证据原文"}
_BOLD_KEYS = {"距今"}
_KEY_COLORS = {"原文": BLUE, "翻译": CYAN, "解读": AMBER}
_KNOWN_KEYS = _QUOTE_KEYS | {"时间", "距今", "确认", "确认状态", "事件", "完成/确认时间", "完成时间", "预告窗口", "窗口", "来源", "状态", "说明"}

_NUM_HEAD = re.compile(r"^#(\d+)\s+(.+?)\s{2,}(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})(?:\s*北京时间)?$")
_KV_LINE = re.compile(r"^([^：:]{1,14})[：:]\s*(.*)$", re.S)
_FEED_HEAD = re.compile(r"^(直接相关|间接相关|无重置信号)\s*·\s*(\d{2}-\d{2}\s+\d{2}:\d{2})\s*·\s*(.+)$")
_URL_TOKEN = re.compile(r"(https?://[^\s，。；、]+)")
_HOURS = re.compile(r"(\d{1,2}):00\((\d+)\)")
_AMOUNT = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*(\S*)$")
_WRAP_TOKEN = re.compile(r"[A-Za-z0-9_\-@#&%/+.:'\",;!?()\[\]{}<>=~*|]+|\s+|.", re.S)


@dataclass(slots=True)
class CardSection:
    title: str
    lines: list[str]
    accent: tuple[int, int, int] = CYAN


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


def _format_time(value: datetime | None) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M") if value else "时间未知"


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


def _chip(draw: ImageDraw.ImageDraw, x: float, y: float, text: str, color, font=None, *, alpha: int = 30) -> float:
    font = font or FONT_TAG
    width = font.getlength(text) + 22
    height = getattr(font, "size", 16) + 12
    draw.rounded_rectangle((x, y, x + width, y + height), radius=height / 2, fill=(*color, alpha), outline=(*color, 110), width=1)
    _dtext(draw, (x + 11, y + 5), text, font, color)
    return width


def _led(draw: ImageDraw.ImageDraw, cx: float, cy: float, color, r: float = 7) -> None:
    for radius, alpha in ((r + 10, 24), (r + 5, 52), (r, 235)):
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(*color, alpha))


def _split_amount(value: str) -> tuple[str | None, str]:
    match = _AMOUNT.match(str(value or "").strip())
    return (match.group(1), match.group(2) or "") if match else (None, "")


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


def _draw_mixed(draw: ImageDraw.ImageDraw, x: float, y: float, text: str, font, color, link_color=BLUE) -> None:
    """Draw one line, coloring URLs differently from the base text."""
    pos = x
    for part in _URL_TOKEN.split(text):
        if not part:
            continue
        fill = link_color if part.startswith("http") else color
        _dtext(draw, (pos, y), part, font, fill)
        pos += font.getlength(part)


# ---------------------------------------------------------------------------
# Layout: turn section lines into typed rows with measured heights.
# ---------------------------------------------------------------------------

def _classify_block(lines: list[str], inner_w: int) -> list[dict]:
    classified: list[dict] = []
    indent = 0
    prev_kind = ""
    for raw in lines:
        text = str(raw).strip()
        head = _NUM_HEAD.match(text)
        if head:
            label = head.group(2).strip()
            classified.append({"kind": "tl", "num": head.group(1), "label": label, "time": head.group(3), "color": _STATUS_STYLES.get(label, CYAN), "indent": 0})
            indent = 42
            prev_kind = "tl"
            continue
        feed = _FEED_HEAD.match(text)
        if feed:
            classified.append({"kind": "feed", "label": feed.group(1), "time": feed.group(2), "pid": feed.group(3).strip(), "color": _STATUS_STYLES.get(feed.group(1), CYAN), "indent": 0})
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


def _build_rows(title: str, lines: list[str], inner_w: int) -> tuple[list[dict], tuple[int, int, int] | None]:
    rows: list[dict] = []
    hero_color: tuple[int, int, int] | None = None

    def add(kind: str, height: int, **data: object) -> None:
        rows.append({"kind": kind, "h": height, **data})

    if not lines:
        add("empty", 48)
        return rows, hero_color

    # Hero status panel: 当前雷达状态 / 当前状态.
    if title.endswith("状态") and lines[0] in _STATUS_STYLES:
        hero_color = _STATUS_STYLES[lines[0]]
        detail = _wrap(lines[1], FONT_SMALL, inner_w - 40) if len(lines) > 1 else []
        add("hero", 48 + len(detail) * 29 + (8 if detail else 0), label=lines[0], detail=detail, color=hero_color)
        rows.extend(_classify_block(lines[2:], inner_w))
        return rows, hero_color

    # Statistics: stat tiles + 24h distribution chart.
    if "统计" in title:
        tiles: list[tuple[str, str, str]] = []
        hour_data: dict[int, int] | None = None
        rest: list[str] = []
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
                    parsed = {int(hour): int(count) for hour, count in _HOURS.findall(value)}
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

    # Source health rows.
    if "来源健康" in title:
        add("sources", len(lines) * 38, entries=[_parse_source(line) for line in lines])
        return rows, hero_color

    # PT timezone block.
    if "PT 时区" in title:
        head = str(lines[0] or "")
        if " · " in head:
            time_part, phase = head.split(" · ", 1)
        else:
            time_part, phase = head, ""
        note = _wrap(lines[1], FONT_TINY, inner_w - 40) if len(lines) > 1 else []
        add("pt", 48 + len(note) * 24 + (12 if note else 0), time=time_part.strip(), phase=phase.strip(), note=note)
        return rows, hero_color

    rows.extend(_classify_block(lines, inner_w))
    return rows, hero_color


def _layout_section(section: CardSection, inner_w: int) -> dict:
    title = str(section.title or "")
    accent = section.accent if isinstance(section.accent, tuple) and len(section.accent) == 3 else CYAN
    lines = [str(line) for line in (section.lines or [])]
    rows, hero_color = _build_rows(title, lines, inner_w)
    body_h = sum(row["h"] for row in rows) + ROW_GAP * (len(rows) - 1)
    return {"title": title, "accent": accent, "rows": rows, "height": 80 + body_h + 26, "hero": hero_color}


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


def _draw_radar_motif(draw: ImageDraw.ImageDraw) -> None:
    cx, cy = CANVAS_W - 148, 14
    for radius, alpha in ((56, 48), (96, 36), (136, 26)):
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=(*CYAN, alpha), width=2)
    draw.pieslice((cx - 96, cy - 96, cx + 96, cy + 96), start=196, end=242, fill=(*CYAN, 26))
    angle = math.radians(242)
    draw.line((cx, cy, cx + 136 * math.cos(angle), cy + 136 * math.sin(angle)), fill=(*CYAN, 90), width=2)
    draw.line((cx - 136, cy, cx + 136, cy), fill=(*CYAN, 14), width=1)
    draw.line((cx, cy - 136, cx, cy + 136), fill=(*CYAN, 14), width=1)
    for radius, degrees, color in ((96, 152, GREEN), (136, 284, AMBER), (56, 326, CYAN)):
        angle = math.radians(degrees)
        bx, by = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
        draw.ellipse((bx - 8, by - 8, bx + 8, by + 8), fill=(*color, 40))
        draw.ellipse((bx - 3.5, by - 3.5, bx + 3.5, by + 3.5), fill=(*color, 220))


def _draw_tracked(draw: ImageDraw.ImageDraw, x: float, y: float, text: str, font, fill, tracking: float = 3.0) -> float:
    pos = x
    for char in text:
        _dtext(draw, (pos, y), char, font, fill)
        pos += font.getlength(char) + tracking
    return pos - x - tracking


def _draw_header(draw: ImageDraw.ImageDraw, title: str, subtitle: str, page: str) -> None:
    _draw_radar_motif(draw)
    kicker = "CODEX RADAR"
    kicker_w = sum(FONT_KICKER.getlength(ch) + 3 for ch in kicker) - 3 + 28
    draw.rounded_rectangle((PAD_X, 42, PAD_X + kicker_w, 74), radius=16, fill=(*CYAN, 20), outline=(*CYAN, 120), width=1)
    _draw_tracked(draw, PAD_X + 14, 48, kicker, FONT_KICKER, CYAN)
    if page:
        label = f"PAGE {page}"
        width = FONT_TAG.getlength(label) + 24
        _chip(draw, CANVAS_W - PAD_X - width, 46, label, MUTED, FONT_TAG, alpha=18)
    _dtext(draw, (PAD_X, 86), _clip_width(title, FONT_TITLE, 730), FONT_TITLE, TEXT)
    _dtext(draw, (PAD_X, 150), _clip_width(_clip(subtitle, 110), FONT_SMALL, CANVAS_W - PAD_X * 2), FONT_SMALL, MUTED)
    segment = 420
    for i in range(segment):
        alpha = int(200 * (1 - i / segment) ** 1.6)
        draw.line((PAD_X + i, 192, PAD_X + i + 1, 192), fill=(*CYAN, alpha), width=2)
    draw.line((PAD_X + segment, 192, CANVAS_W - PAD_X, 192), fill=(255, 255, 255, 14), width=1)


def _draw_footer(draw: ImageDraw.ImageDraw, y: float) -> None:
    draw.line((PAD_X, y, CANVAS_W - PAD_X, y), fill=(255, 255, 255, 20), width=1)
    _dtext(draw, (PAD_X, y + 16), "公开信息雷达 · 结论不代表实时在线状态或 OpenAI 官方承诺", FONT_TINY, FAINT)
    _dtext(draw, (CANVAS_W - PAD_X, y + 16), "codexradar.com · codex-reset.com", FONT_TINY, FAINT, anchor="ra")


def _draw_tiles(draw: ImageDraw.ImageDraw, x: float, y: float, w: float, tiles: list[tuple[str, str, str]]) -> None:
    gap = 16
    tile_w = (w - gap * (len(tiles) - 1)) / len(tiles)
    for index, (caption, value, unit) in enumerate(tiles):
        tx = x + index * (tile_w + gap)
        draw.rounded_rectangle((tx, y, tx + tile_w, y + 96), radius=14, fill=(255, 255, 255, 10), outline=(150, 178, 214, 40), width=1)
        _dtext(draw, (tx + 20, y + 14), caption, FONT_TINY, MUTED)
        _dtext(draw, (tx + 20, y + 38), value, FONT_BIG, TEXT)
        if unit:
            _dtext(draw, (tx + 26 + FONT_BIG.getlength(value), y + 58), unit, FONT_TINY, MUTED)


def _draw_bars(draw: ImageDraw.ImageDraw, x: float, y: float, w: float, data: dict[int, int]) -> None:
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
        is_max = value == max_value
        color = GREEN if is_max else CYAN
        draw.rounded_rectangle((bx, base - bar_h, bx + bar_w, base), radius=3, fill=(*color, 205 if is_max else 120))
        if is_max:
            _dtext(draw, (bx + bar_w / 2, base - bar_h - 22), str(value), FONT_TINY, GREEN, anchor="ma")
    draw.line((x, base + 1, x + w, base + 1), fill=(255, 255, 255, 30), width=1)
    for hour in (0, 6, 12, 18, 23):
        cx = x + hour * (bar_w + gap) + bar_w / 2
        _dtext(draw, (cx, base + 8), f"{hour:02d}:00", FONT_TINY, FAINT, anchor="ma")


def _draw_row(draw: ImageDraw.ImageDraw, row: dict, x: float, y: float, w: float) -> None:
    kind = row["kind"]
    if kind == "hero":
        _led(draw, x + 13, y + 17, row["color"])
        _dtext(draw, (x + 38, y), row["label"], FONT_HERO, row["color"])
        yy = y + 48
        for line in row["detail"]:
            _dtext(draw, (x + 38, yy), line, FONT_SMALL, MUTED)
            yy += 29
    elif kind == "tiles":
        _draw_tiles(draw, x, y, w, row["tiles"])
    elif kind == "bars":
        _draw_bars(draw, x, y, w, row["data"])
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
            color = next((c for prefix, c in _PHASE_STYLES.items() if phase.startswith(prefix)), CYAN)
            _chip(draw, x + FONT_PT.getlength(row["time"]) + 18, y - 2, phase, color)
        yy = y + 48
        for line in row["note"]:
            _dtext(draw, (x, yy), line, FONT_TINY, FAINT)
            yy += 24
    elif kind == "tl":
        cy = y + 22
        color = row["color"]
        draw.ellipse((x, cy - 15, x + 30, cy + 15), fill=(*color, 30), outline=(*color, 170), width=1)
        _dtext(draw, (x + 15, cy), row["num"], FONT_TAG, TEXT, anchor="mm")
        _chip(draw, x + 42, cy - 14, row["label"], color)
        _dtext(draw, (x + w, cy), row["time"], FONT_SMALL, FAINT, anchor="rm")
    elif kind == "feed":
        chip_w = _chip(draw, x, y, row["label"], row["color"])
        _dtext(draw, (x + chip_w + 14, y + 6), row["time"], FONT_SMALL, MUTED)
        _dtext(draw, (x + w, y + 6), f"#{row['pid']}", FONT_SMALL, FAINT, anchor="ra")
    elif kind == "kv":
        _dtext(draw, (x + row["indent"], y + 5), row["key"], FONT_SMALL, MUTED)
        font = FONT_BODY_B if row["key"] in _BOLD_KEYS else FONT_BODY
        yy = y
        for line in row["lines"]:
            _draw_mixed(draw, x + row["indent"] + row["key_col"], yy, line, font, TEXT)
            yy += 36
    elif kind == "qkv":
        _chip(draw, x + row["indent"], y, row["key"], _KEY_COLORS.get(row["key"], MUTED), FONT_TAG, alpha=24)
        yy = y + 3
        for line in row["lines"]:
            _draw_mixed(draw, x + row["indent"] + row["key_col"], yy, line, FONT_QUOTE, SUBTLE)
            yy += 33
    elif kind == "link":
        url = row["url"]
        _dtext(draw, (x + row["indent"], y + 2), url, FONT_SMALL, BLUE)
        draw.line((x + row["indent"], y + 30, x + row["indent"] + FONT_SMALL.getlength(url), y + 30), fill=(*BLUE, 70), width=1)
    elif kind == "text":
        font = FONT_QUOTE if row["subtle"] else FONT_BODY
        color = SUBTLE if row["subtle"] else TEXT
        yy = y
        for line in row["lines"]:
            _draw_mixed(draw, x + row["indent"], yy, line, font, color)
            yy += 33 if row["subtle"] else 36
    elif kind == "empty":
        _dtext(draw, (x + w / 2, y + 12), "— 暂无记录 —", FONT_SMALL, FAINT, anchor="mm")


def _draw_panel(draw: ImageDraw.ImageDraw, x: float, y: float, w: float, panel: dict) -> None:
    height = panel["height"]
    draw.rounded_rectangle((x, y, x + w, y + height), radius=18, fill=(*PANEL, 236), outline=(*PANEL_BORDER, 150), width=1)
    if panel["hero"]:
        draw.rounded_rectangle((x, y, x + w, y + height), radius=18, fill=(*panel["hero"], 14), outline=(*panel["hero"], 70), width=1)
    inner_x = x + PANEL_PAD_X
    inner_w = w - PANEL_PAD_X * 2
    draw.rounded_rectangle((inner_x, y + 26, inner_x + 5, y + 52), radius=2, fill=(*panel["accent"], 235))
    _dtext(draw, (inner_x + 17, y + 24), _clip_width(panel["title"], FONT_SECTION, inner_w - 30), FONT_SECTION, TEXT)
    draw.line((inner_x, y + 68, inner_x + inner_w, y + 68), fill=(255, 255, 255, 16), width=1)

    yy = y + 82
    conn_x: float | None = None
    conn_y = 0.0
    for row in panel["rows"]:
        if conn_x is not None:
            draw.line((conn_x, conn_y, conn_x, yy), fill=(*panel["accent"], 40), width=2)
        _draw_row(draw, row, inner_x, yy, inner_w)
        if row["kind"] == "tl":
            conn_x, conn_y = inner_x + 15, yy + 36
        elif conn_x is not None and row["kind"] in {"kv", "qkv", "text", "link"}:
            conn_y = yy + row["h"]
        else:
            conn_x = None
        yy += row["h"] + ROW_GAP


async def render_card(title: str, subtitle: str, sections: list[CardSection], *, page: str = "") -> bytes:
    return await run_image_render(_render_card, title, subtitle, sections, page)


def _render_card(title: str, subtitle: str, sections: list[CardSection], page: str) -> bytes:
    inner_w = CANVAS_W - PAD_X * 2 - PANEL_PAD_X * 2
    panels = [_layout_section(section, inner_w) for section in sections]
    body_h = sum(panel["height"] + PANEL_GAP for panel in panels)
    height = max(430, min(3900, HEADER_H + body_h + FOOTER_H + 26))
    image = Image.new("RGB", (CANVAS_W, height), BG_TOP)
    draw = ImageDraw.Draw(image, "RGBA")
    _draw_background(draw, height)
    _draw_header(draw, title, subtitle, page)
    y = float(HEADER_H)
    for panel in panels:
        _draw_panel(draw, PAD_X, y, CANVAS_W - PAD_X * 2, panel)
        y += panel["height"] + PANEL_GAP
    _draw_footer(draw, min(y + 6, height - FOOTER_H + 6))
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


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
        sections.append(CardSection(f"Tibo 动态 · {post.post_id}", lines, GREEN if post.relevance == "direct" else AMBER))
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
        sections.append(CardSection(f"重置事件 · {event.event_id}", lines, GREEN if event.status == "confirmed" else AMBER if event.status != "rejected" else RED))
    return sections
