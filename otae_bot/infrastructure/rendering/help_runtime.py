"""Render a help page on demand and cache the PNG on disk.

Shipping one PNG per (page, artwork) pair made the repository grow with every new
illustration. Instead the artwork is picked by aspect ratio, rendered the first time it
is drawn, and reused from ``data/cache/help-cards`` afterwards. The pre-rendered
``assets/image/help/<name>.png`` stays as the fallback when no browser is available.

Rendering goes through the shared browser in
``otae_bot.infrastructure.rendering.browser``; the fonts are injected as route
resources so the page never touches the network.
"""

from __future__ import annotations

import asyncio
import os
import random
import tempfile
from functools import lru_cache
from pathlib import Path

from loguru import logger

from otae_bot.infrastructure.rendering import help_cards
from otae_bot.infrastructure.rendering.browser import BrowserResource, screenshot_web_element
from otae_bot.paths import PROJECT_ROOT

#: Runtime cache location; override with ``OTAE_HELP_CACHE_PATH``.
CACHE_PATH_ENV = "OTAE_HELP_CACHE_PATH"
DEFAULT_CACHE_PATH = "data/cache/help-cards"

#: Matches the offline pipeline in ``scripts/render_help_cards.py``.
DEVICE_SCALE_FACTOR = 1
VIEWPORT_HEIGHT = 900

_locks: dict[str, asyncio.Lock] = {}


def cache_dir() -> Path:
    """Directory holding runtime-rendered help PNGs."""
    configured = Path(os.getenv(CACHE_PATH_ENV, DEFAULT_CACHE_PATH))
    return configured if configured.is_absolute() else PROJECT_ROOT / configured


def find_page(
    page_id: str,
    pages: list[help_cards.HelpPage] | None = None,
) -> help_cards.HelpPage | None:
    """Locate a page by its id or by the stem of its shipped PNG."""
    for page in help_cards.load_pages() if pages is None else pages:
        if page.id == page_id or Path(page.file).stem == page_id:
            return page
    return None


def shipped_help_image(page_id: str) -> Path | None:
    """The pre-rendered fallback shipped in the repository, if it exists."""
    path = help_cards.HELP_IMAGE_DIR / f"{page_id}.png"
    return path if path.is_file() else None


async def cached_help_image(
    page_id: str,
    *,
    rng: random.Random | None = None,
    theme: help_cards.HelpTheme | None = None,
    pages: list[help_cards.HelpPage] | None = None,
    gallery: help_cards.Gallery | None = None,
) -> Path | None:
    """Return a PNG for ``page_id``, rendering and caching it when it is not cached yet.

    The artwork is drawn at random from :func:`help_cards.ratio_candidates`, which is the
    whole gallery minus anything the standee window could not show, so repeated calls vary
    across every illustration. Falls back to the shipped PNG when the page is unknown or
    rendering fails.
    """
    theme = theme or help_cards.HelpTheme()
    fallback = shipped_help_image(page_id)
    page = find_page(page_id, pages)
    if page is None:
        return fallback
    candidates = help_cards.ratio_candidates(page, gallery or help_cards.load_gallery(), theme)
    if not candidates:
        return fallback
    art = (rng or random).choice(candidates)
    digest = help_cards.spec_digest(page, art, theme)
    target = cache_dir() / f"{page.id}.{art.id}.png"
    cached = _cached(target, digest)
    if cached is not None:
        return cached
    async with _lock_for(target):
        cached = _cached(target, digest)
        if cached is not None:
            return cached
        try:
            content = await _render(page, art, theme)
        except Exception as exc:  # any browser failure falls back to the shipped PNG
            logger.warning(f"[help] 渲染 {page.id}/{art.id} 失败，改用静态帮助图：{type(exc).__name__}: {exc}")
            return fallback
        _write_atomic(target, content)
    return target


def _cached(path: Path, digest: str) -> Path | None:
    """Return the cached PNG when it was rendered from the current spec."""
    if path.is_file() and help_cards.read_digest(path) == digest:
        return path
    return None


def _lock_for(path: Path) -> asyncio.Lock:
    """Coalesce concurrent renders of the same file; the loop is single-threaded."""
    return _locks.setdefault(str(path), asyncio.Lock())


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("wb", dir=path.parent, suffix=".tmp", delete=False)
    try:
        handle.write(content)
        handle.close()
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


@lru_cache(maxsize=8)
def _font_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def _font_resources(theme: help_cards.HelpTheme) -> dict[str, BrowserResource]:
    resources = {}
    for _, name in theme.font_files:
        path = (theme.font_dir / name).resolve()
        resources[path.as_uri()] = BrowserResource(_font_bytes(str(path)), "font/ttf")
    return resources


async def _render(
    page: help_cards.HelpPage,
    art: help_cards.Artwork,
    theme: help_cards.HelpTheme,
) -> bytes:
    """Render one page/artwork pair to PNG bytes, byte-identical to the offline pipeline."""
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as handle:
        handle.write(help_cards.render_html(page, art, theme))
        html_path = Path(handle.name)
    try:
        png = await screenshot_web_element(
            html_path.as_uri(),
            ".page",
            viewport=(theme.page_width, VIEWPORT_HEIGHT),
            max_height=theme.max_height,
            device_scale_factor=DEVICE_SCALE_FACTOR,
            strict_max_height=True,
            wait_for_images=True,
            wait_for_fonts=True,
            settle_ms=0,
            resources=_font_resources(theme),
        )
    finally:
        html_path.unlink(missing_ok=True)
    return help_cards.finalize_png(png, help_cards.spec_digest(page, art, theme))


__all__ = ["CACHE_PATH_ENV", "cached_help_image", "cache_dir", "find_page", "shipped_help_image"]
