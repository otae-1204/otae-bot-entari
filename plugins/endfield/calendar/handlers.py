"""终末地 /ef 版本日历出图（由 handlers 的指令派发调用）。"""

from __future__ import annotations

from loguru import logger

from .. import caches
from ..caches import _CALENDAR_CACHE
from ..state import calendar_source, official_calendar_source
from .draw import draw_version_calendar
from .official_draw import draw_official_version_calendar


async def _render_current_version_calendar() -> bytes:
    generation = caches._ASSET_GENERATION
    try:
        calendar = await calendar_source.current_ake_primary()
        return await _CALENDAR_CACHE.get_or_create(
            f"{generation}:akedata:{calendar.version}:{calendar.revision}",
            lambda: draw_version_calendar(calendar),
        )
    except Exception as exc:
        logger.warning("[endfield] AKE calendar coverage unavailable; official fallback ({})", type(exc).__name__)
    try:
        official = await official_calendar_source.current()
        return await _CALENDAR_CACHE.get_or_create(
            f"{generation}:official:{official.revision}",
            lambda: draw_official_version_calendar(official),
        )
    except Exception as exc:
        logger.warning(
            f"[endfield] official calendar unavailable, use AkeData fallback: "
            f"{type(exc).__name__}: {exc}"
        )
    calendar = await calendar_source.current()
    return await _CALENDAR_CACHE.get_or_create(
        f"{generation}:generated:{calendar.version}:{calendar.revision}",
        lambda: draw_version_calendar(calendar),
    )
