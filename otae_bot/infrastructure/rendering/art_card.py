"""Render a card on the shared illustrated frame with a random artwork.

``art_frame`` owns the frame markup, theme and gallery; this module is the browser side
shared by every card on that template except /help (which caches per page on disk):

* the document is offline: fonts and artwork are local ``file://`` paths the browser reads
  directly, and the CSP allows nothing else;
* the artwork is drawn at random like /help. How much of it the standee shows depends on
  the card height, which is only known after rendering, so the first draw is from the whole
  gallery; when the rendered height leaves that artwork below the standee floor, the card
  is rendered once more with a fitting one. The height is remembered per card content, so
  the same card picks from fitting artwork straight away next time.
"""

from __future__ import annotations

import hashlib
import io
import random
from dataclasses import dataclass
from html import escape

from otae_bot.infrastructure.rendering import art_frame
from otae_bot.infrastructure.rendering.browser import screenshot_web_element
from .temp_files import write_temp_html

THEME = art_frame.HelpTheme()
CARD_WIDTH = THEME.page_width
CARD_MAX_HEIGHT = 6000
#: Same as /help.
DEVICE_SCALE_FACTOR = 1
#: /help's minimum height is sized for a full command table; data cards are often shorter.
DEFAULT_MIN_CARD_HEIGHT = 460

#: Fonts and artwork are read from disk by the browser. Routing two 8 MB fonts through
#: ``BrowserResource`` on every render cost about 0.6 s with identical output.
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; font-src file:; "
    "img-src file:; base-uri 'none'; form-action 'none'"
)

_HEIGHT_MEMORY = 256
_heights: dict[str, int] = {}


@dataclass(frozen=True)
class ArtCard:
    """One card. ``title`` / ``subtitle`` are plain text; ``panel_html`` and ``css`` are trusted."""

    title: str
    subtitle: str
    panel_html: str
    css: str = ""
    min_card_height: int = DEFAULT_MIN_CARD_HEIGHT
    document_title: str = ""

    @property
    def key(self) -> str:
        payload = f"{self.title}\0{self.subtitle}\0{self.panel_html}\0{self.css}\0{self.min_card_height}"
        return hashlib.sha1(payload.encode()).hexdigest()


def theme_variables(theme: art_frame.HelpTheme = THEME) -> str:
    """CSS variables for panel content, so cards follow /help's colours."""
    return (
        f":root{{--accent:{theme.accent};--accent-soft:{theme.accent_soft};"
        f"--heading:{theme.heading_color};--body:{theme.body_color}}}"
    )


def card_html(card: ArtCard, art: art_frame.Artwork | None = None, theme: art_frame.HelpTheme = THEME) -> str:
    """A complete offline HTML document; without ``art`` the gallery default is used."""
    art = art or art_frame.shared_gallery().default_artwork()
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
        f"<title>{escape(card.document_title or card.title)}</title><style>\n"
        f"{art_frame.font_face_css(theme)}\n"
        f"{art_frame.frame_css(theme, art, min_card_height=card.min_card_height)}\n"
        f"{theme_variables(theme)}{card.css}</style></head>"
        + art_frame.frame_body(card.title, art_frame.subtitle_html(card.subtitle), art, card.panel_html, theme)
    )


def candidates(height: int | None) -> tuple[art_frame.Artwork, ...]:
    """Artwork whose subject the standee still shows at this height; all when unknown."""
    return art_frame.candidates(height, art_frame.shared_gallery(), THEME)


def _remember(key: str, height: int) -> None:
    if len(_heights) >= _HEIGHT_MEMORY and key not in _heights:
        _heights.clear()
    _heights[key] = height


async def _shoot(card: ArtCard, art: art_frame.Artwork) -> bytes:
    path = await write_temp_html(card_html(card, art))
    try:
        return await screenshot_web_element(
            path.as_uri(),
            ".page",
            viewport=(CARD_WIDTH, 900),
            max_height=CARD_MAX_HEIGHT,
            device_scale_factor=DEVICE_SCALE_FACTOR,
            strict_max_height=True,
            wait_for_images=True,
            wait_for_fonts=True,
            settle_ms=0,
        )
    finally:
        path.unlink(missing_ok=True)


def _png_height(png: bytes) -> int:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        return image.height


async def render_card(card: ArtCard, *, rng: random.Random | None = None) -> bytes:
    """Render to PNG with a random artwork that fits the card's height."""
    rng = rng or random
    art = rng.choice(candidates(_heights.get(card.key)))
    png = await _shoot(card, art)
    height = _png_height(png)
    _remember(card.key, height)
    fitting = candidates(height)
    if art not in fitting:
        png = await _shoot(card, rng.choice(fitting))
    return png


__all__ = [
    "CARD_MAX_HEIGHT",
    "CARD_WIDTH",
    "CSP",
    "DEFAULT_MIN_CARD_HEIGHT",
    "DEVICE_SCALE_FACTOR",
    "THEME",
    "ArtCard",
    "candidates",
    "card_html",
    "render_card",
    "theme_variables",
]
