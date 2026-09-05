from __future__ import annotations

import html
import tempfile
from pathlib import Path
from time import perf_counter

from loguru import logger

from otae_bot.infrastructure.rendering.executor import run_image_render
from otae_bot.infrastructure.rendering.browser import screenshot_web_element
from otae_bot.infrastructure.rendering.temp_files import schedule_temp_file_cleanup

from ..rendering.cards import _prepare_assets, optimize_png_container
from .official import OfficialCalendarDiscoveryError, OfficialVersionCalendar


CALENDAR_WIDTH = 1080
FOOTER_HEIGHT = 30
MAX_CALENDAR_HEIGHT = 4096
TIMELINE_OFFSET_X = -4


async def draw_official_version_calendar(calendar: OfficialVersionCalendar) -> bytes:
    started = perf_counter()
    prepared = await _prepare_assets(
        (asset.url for asset in calendar.assets),
        inline=False,
    )
    for asset in calendar.assets:
        if not prepared.urls.get(asset.url) or len(prepared.contents.get(asset.url, b"")) < 8_000:
            raise OfficialCalendarDiscoveryError(f"官网日历素材不可用：{asset.key}")
    rendered_html = render_official_version_calendar_html(calendar, prepared.urls)
    html_path = _write_temp_html(rendered_html)
    assets_seconds = perf_counter() - started
    target_height = official_calendar_height(calendar)
    try:
        screenshot_started = perf_counter()
        output = await screenshot_web_element(
            html_path.resolve().as_uri(),
            ".official-version-calendar",
            viewport=(CALENDAR_WIDTH, target_height),
            timeout_ms=20000,
            max_height=MAX_CALENDAR_HEIGHT,
            device_scale_factor=1.0,
            settle_ms=50,
            resources=prepared.resources,
            wait_for_images=True,
            strict_max_height=True,
            overflow_selectors=(".official-version-calendar",),
        )
        optimize_started = perf_counter()
        optimized = await run_image_render(optimize_png_container, output)
        logger.info(
            f"[endfield] draw kind=official_version_calendar assets={assets_seconds:.3f}s "
            f"screenshot={optimize_started - screenshot_started:.3f}s "
            f"png_optimize={perf_counter() - optimize_started:.3f}s "
            f"bytes={len(output)}->{len(optimized)} revision={calendar.revision}"
        )
        return optimized
    finally:
        schedule_temp_file_cleanup(html_path, delay_seconds=30)


def official_calendar_height(calendar: OfficialVersionCalendar) -> int:
    image_height = sum(
        round(asset.height * CALENDAR_WIDTH / asset.width)
        for asset in calendar.assets
    )
    return image_height + FOOTER_HEIGHT


def render_official_version_calendar_html(
    calendar: OfficialVersionCalendar,
    asset_urls: dict[str, str] | None = None,
) -> str:
    asset_urls = asset_urls or {}
    canvas_width = max(asset.width for asset in calendar.assets)
    images = []
    for asset in calendar.assets:
        url = asset_urls.get(asset.url, asset.url)
        width = round(asset.width * CALENDAR_WIDTH / canvas_width)
        height = round(asset.height * CALENDAR_WIDTH / asset.width)
        images.append(
            f'<img class="{_esc_attr(asset.key.replace(".", "-"))}" '
            f'src="{_esc_attr(url)}" width="{width}" height="{height}" '
            f'alt="{_esc_attr(asset.key)}">'
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: #050505; }}
.official-version-calendar {{
  width: {CALENDAR_WIDTH}px;
  overflow: hidden;
  background: #f5f5f4;
}}
.official-version-calendar img {{
  display: block;
  object-fit: fill;
}}
.calendar-content {{
  margin-left: auto;
}}
.calendar-timeline {{
  transform: translateX({TIMELINE_OFFSET_X}px);
}}
.official-footer {{
  position: relative;
  height: {FOOTER_HEIGHT}px;
  color: #777;
  background: #050505;
  border-bottom: 4px solid #c80018;
  font: 8px/26px Arial, sans-serif;
  letter-spacing: .4px;
  padding-left: 25px;
}}
.official-footer span {{
  position: absolute;
  right: 24px;
  color: #999;
}}
</style>
</head>
<body>
<main class="official-version-calendar" data-revision="{_esc_attr(calendar.revision)}">
  {''.join(images)}
  <footer class="official-footer">© HYPERGRYPH <span>官方版本日历 · 自动同步</span></footer>
</main>
</body>
</html>"""


def _write_temp_html(content: str) -> Path:
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as file:
        file.write(content)
        return Path(file.name)


def _esc_attr(value: object) -> str:
    return html.escape(str(value or ""), quote=True)
