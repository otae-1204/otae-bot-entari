"""Illustrated card frame shared by /help and every card built on the same template.

One place owns what the cards have in common, so they can never drift apart:

* the theme (``HelpTheme``: width, colours, blur, fonts);
* the art gallery in ``assets/image/help/art/gallery.json`` (``shared_gallery`` reloads it
  when the file changes, so every card sees an added or removed artwork at the same time);
* the artwork choice (``candidates``: the standee window is the only gate);
* the frame itself: blue banner, blurred backdrop, frosted panel and sharp standee
  (``frame_css`` + ``frame_body``).

A card only supplies its panel content and the CSS for it. ``help_cards`` builds the help
pages on top of this module; ``plugins/changelog`` is the second user.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any

from otae_bot.infrastructure.rendering.help_images import cover_visibility, image_ratio

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ART_DIR = PROJECT_ROOT / "assets/image/help/art"
GALLERY_PATH = ART_DIR / "gallery.json"
FONT_FAMILY = "OtaeHelpSans"


class HelpSpecError(ValueError):
    pass


@dataclass(frozen=True)
class HelpTheme:
    page_width: int = 1325
    min_card_height: int = 661
    window_width: int = 240
    max_height: int = 4000
    accent: str = "#527fbe"
    accent_soft: str = "#a5bcde"
    heading_color: str = "#434343"
    body_color: str = "#5b5b5b"
    border_color: str = "#251e19"
    panel_alpha: float = 0.78
    backdrop_blur: int = 8
    font_dir: Path = PROJECT_ROOT / "plugins/endfield/assets/fonts"
    font_files: tuple[tuple[int, str], ...] = (
        (500, "HarmonyOS_Sans_SC_Medium.ttf"),
        (700, "HarmonyOS_Sans_SC_Bold.ttf"),
    )

    def digest_fields(self) -> dict[str, Any]:
        values = dataclasses.asdict(self)
        values.pop("font_dir")
        return values


@dataclass(frozen=True)
class Artwork:
    id: str
    path: Path
    focus: tuple[float, float] = (0.5, 0.3)
    backdrop_focus: tuple[float, float] | None = None
    credit: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def sha256(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Gallery:
    artworks: dict[str, Artwork]
    default: tuple[str, ...]

    def resolve(self, page: Any) -> tuple[Artwork, ...]:
        """Artwork a help page pins (``page.art``), else the gallery default."""
        ids = page.art or self.default
        missing = [art_id for art_id in ids if art_id not in self.artworks]
        if missing:
            raise HelpSpecError(f"page {page.id!r} uses unknown art {missing}; add them to gallery.json")
        if not ids:
            raise HelpSpecError(f"page {page.id!r} has no art and gallery.json has no default")
        return tuple(self.artworks[art_id] for art_id in ids)

    def default_artwork(self) -> Artwork:
        """The first default artwork, or any artwork when no default is set."""
        for art_id in self.default:
            if art_id in self.artworks:
                return self.artworks[art_id]
        return next(iter(self.artworks.values()))


# ---------------------------------------------------------------------------
# Gallery


def _text(value: Any) -> str:
    return str(value or "").strip()


def _focus(value: Any, fallback: tuple[float, float] | None) -> tuple[float, float] | None:
    if value is None:
        return fallback
    x, y = (float(part) for part in value)
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise HelpSpecError(f"art focus must be fractions between 0 and 1: {value!r}")
    return x, y


def load_gallery(path: Path = GALLERY_PATH) -> Gallery:
    data = json.loads(path.read_text(encoding="utf-8"))
    artworks: dict[str, Artwork] = {}
    for raw in data.get("art") or ():
        art_id = _text(raw.get("id"))
        if not art_id or not re.fullmatch(r"[A-Za-z0-9_-]+", art_id):
            raise HelpSpecError(f"art id must be letters, digits, '-' or '_': {raw!r}")
        if art_id in artworks:
            raise HelpSpecError(f"duplicate art id: {art_id!r}")
        art_path = path.parent / _text(raw.get("file"))
        if not art_path.is_file():
            raise HelpSpecError(f"art {art_id!r} file not found: {art_path}")
        artworks[art_id] = Artwork(
            id=art_id,
            path=art_path,
            focus=_focus(raw.get("focus"), (0.5, 0.3)),
            backdrop_focus=_focus(raw.get("backdrop_focus"), None),
            credit=_text(raw.get("credit")),
            raw=raw,
        )
    default = data.get("default") or ()
    return Gallery(artworks, (default,) if isinstance(default, str) else tuple(default))


_shared: tuple[Path, int, Gallery] | None = None


def shared_gallery(path: Path = GALLERY_PATH) -> Gallery:
    """The gallery every illustrated card draws from, reloaded when ``gallery.json`` changes."""
    global _shared
    stamp = path.stat().st_mtime_ns
    if _shared is None or _shared[0] != path or _shared[1] != stamp:
        _shared = (path, stamp, load_gallery(path))
    return _shared[2]


# ---------------------------------------------------------------------------
# Artwork choice by aspect ratio

#: The standee window is the sharp part of the card, so it is the only gate: an artwork
#: stays a candidate while at least this much of it survives into the window. The blurred
#: backdrop sits behind a frosted panel and tolerates heavy cropping, so it never filters -
#: it only orders the pool, best match first.
WINDOW_VISIBILITY_FLOOR = 0.15

#: Card metrics from ``frame_css``: `.card` is inset from `.page`, and `.panel` is inset
#: from `.card` again. Keep in sync with the CSS.
CARD_WIDTH_INSET = 30
CARD_HEIGHT_INSET = 100
PANEL_HEIGHT_INSET = 72


def backdrop_ratio(height: int, theme: HelpTheme = HelpTheme()) -> float:
    """Aspect ratio of the blurred backdrop box."""
    return (theme.page_width - CARD_WIDTH_INSET) / max(1, height - CARD_HEIGHT_INSET)


def window_ratio(height: int, theme: HelpTheme = HelpTheme()) -> float:
    """Aspect ratio of the sharp standee window."""
    return theme.window_width / max(1, height - CARD_HEIGHT_INSET - PANEL_HEIGHT_INSET)


def candidates(
    height: int | None,
    gallery: Gallery,
    theme: HelpTheme = HelpTheme(),
) -> tuple[Artwork, ...]:
    """Every artwork a card of this rendered height may use, closest backdrop match first.

    `WINDOW_VISIBILITY_FLOOR` is the single gate - it drops only artwork that would leave
    the standee showing almost nothing, and falls back to the whole gallery when nothing
    clears it, so a send can always produce an image. With an unknown height (a card whose
    height is only known after rendering) the whole gallery is eligible.

    The order is by backdrop match so the result is deterministic; callers draw uniformly.
    """
    artworks = list(gallery.artworks.values())
    if height is None:
        return tuple(sorted(artworks, key=lambda art: art.id))
    backdrop = backdrop_ratio(height, theme)
    window = window_ratio(height, theme)
    scored = [
        (
            cover_visibility(image_ratio(str(art.path)), backdrop),
            cover_visibility(image_ratio(str(art.path)), window),
            art,
        )
        for art in artworks
    ]
    scored.sort(key=lambda row: (-row[0], row[2].id))
    usable = [row for row in scored if row[1] >= WINDOW_VISIBILITY_FLOOR] or scored
    return tuple(row[2] for row in usable)


# ---------------------------------------------------------------------------
# Frame HTML / CSS


def _object_position(focus: tuple[float, float]) -> str:
    return f"{focus[0] * 100:.1f}% {focus[1] * 100:.1f}%"


def _dots(color: str) -> str:
    circles = "".join(
        f'<circle cx="{column * 16 + 3 + (3 - row) * 8}" cy="{row * 12 + 3}" r="3"/>'
        for row in range(4) for column in range(10)
    )
    return f'<svg class="dots" width="174" height="42" viewBox="0 0 174 42" fill="{color}">{circles}</svg>'


def font_face_css(theme: HelpTheme) -> str:
    return "".join(
        f'@font-face{{font-family:"{FONT_FAMILY}";font-weight:{weight};'
        f'src:url("{(theme.font_dir / name).resolve().as_uri()}") format("truetype")}}'
        for weight, name in theme.font_files
    )


def frame_css(theme: HelpTheme, art: Artwork, *, min_card_height: int | None = None) -> str:
    """Stylesheet for the frame: page, banner, card, blurred backdrop, frosted panel, standee.

    ``min_card_height`` defaults to the theme's; cards with short content may lower it.
    The panel padding and insets match ``CARD_HEIGHT_INSET`` / ``PANEL_HEIGHT_INSET``.
    """
    t = theme
    card_height = t.min_card_height if min_card_height is None else min_card_height
    return f"""*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{background:#fff}}
body{{font-family:"{FONT_FAMILY}",sans-serif;font-synthesis:none}}
.page{{width:{t.page_width}px;padding:17px 13px 16px 17px;background:#fff}}
.banner{{position:relative;height:52px;border-radius:21px;background:{t.accent}}}
.titles{{position:absolute;left:32px;top:0;height:52px;display:flex;align-items:baseline;gap:16px;
  padding-top:9px;white-space:nowrap}}
.titles h1{{font-size:30px;line-height:34px;font-weight:700;letter-spacing:.5px;color:#fff}}
.subtitle{{font-size:18px;font-weight:500;color:rgba(255,255,255,.86)}}
.dots{{position:absolute;right:166px;top:5px}}
.pill{{position:absolute;right:23px;top:7px;width:143px;height:36px;border-radius:18px;background:#fff}}
.card{{position:relative;margin-top:15px;width:{t.page_width - CARD_WIDTH_INSET}px;min-height:{card_height}px;display:flex;gap:22px;
  border:2px solid {t.border_color};border-radius:24px;overflow:hidden;background:#b9c3c0;padding:38px 34px 30px 37px}}
.backdrop{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;
  object-position:{_object_position(art.backdrop_focus or art.focus)};
  filter:blur({t.backdrop_blur}px);transform:scale(1.06)}}
.panel{{position:relative;flex:1;min-width:0;display:flex;flex-direction:column;min-height:{card_height - PANEL_HEIGHT_INSET}px;
  border-radius:24px;background:rgba(255,255,255,{t.panel_alpha});padding:34px 32px 18px 36px}}
.window{{position:relative;flex:0 0 {t.window_width}px;border-radius:24px;overflow:hidden;
  border:3px solid rgba(255,255,255,.9);box-shadow:0 6px 18px rgba(37,30,25,.28)}}
.window img{{display:block;width:100%;height:100%;object-fit:cover;object-position:{_object_position(art.focus)}}}"""


def frame_body(title: str, subtitle_html: str, art: Artwork, panel_html: str, theme: HelpTheme) -> str:
    """The frame markup around ``panel_html``. ``title`` is escaped here; the rest is HTML."""
    art_uri = art.path.resolve().as_uri()
    return f"""<body><div class="page">
<div class="banner"><div class="titles"><h1 data-fit>{escape(title)}</h1>{subtitle_html}</div>{_dots(theme.accent_soft)}<div class="pill"></div></div>
<div class="card"><img class="backdrop" alt="backdrop" src="{art_uri}">
<div class="panel">{panel_html}</div>
<div class="window"><img alt="{escape(art.id)}" src="{art_uri}"></div></div>
</div></body></html>"""


def subtitle_html(subtitle: str) -> str:
    return f'<span class="subtitle" data-fit>{escape(subtitle)}</span>' if subtitle else ""


__all__ = [
    "ART_DIR",
    "CARD_HEIGHT_INSET",
    "CARD_WIDTH_INSET",
    "FONT_FAMILY",
    "GALLERY_PATH",
    "PANEL_HEIGHT_INSET",
    "WINDOW_VISIBILITY_FLOOR",
    "Artwork",
    "Gallery",
    "HelpSpecError",
    "HelpTheme",
    "backdrop_ratio",
    "candidates",
    "font_face_css",
    "frame_body",
    "frame_css",
    "load_gallery",
    "shared_gallery",
    "subtitle_html",
    "window_ratio",
]
