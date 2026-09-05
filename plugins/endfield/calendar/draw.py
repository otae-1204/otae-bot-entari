from __future__ import annotations

import html
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter

from loguru import logger

from otae_bot.infrastructure.rendering.executor import run_image_render
from otae_bot.infrastructure.rendering.browser import BrowserResource, screenshot_web_element
from otae_bot.infrastructure.rendering.temp_files import schedule_temp_file_cleanup

from ..rendering.cards import _prepare_assets, optimize_png_container
from .akedata import VersionCalendar, VersionCalendarEntry, VersionCalendarSection


CALENDAR_WIDTH = 1080
CALENDAR_HEIGHT = 1632
HEADER_HEIGHT = 232
AXIS_HEIGHT = 48
FOOTER_HEIGHT = 104
SECTION_GAP = 8
SIDEBAR_WIDTH = 112


@dataclass(frozen=True, slots=True)
class PreparedVersionCalendarHtml:
    html: str
    resources: dict[str, BrowserResource]


async def prepare_version_calendar_html(
    calendar: VersionCalendar,
) -> PreparedVersionCalendarHtml:
    prepared = await _prepare_assets(
        (entry.art_url for entry in calendar.entries),
        inline=False,
    )
    return PreparedVersionCalendarHtml(
        html=render_version_calendar_html(calendar, prepared.urls),
        resources=prepared.resources,
    )


async def draw_version_calendar(calendar: VersionCalendar) -> bytes:
    started = perf_counter()
    prepared = await prepare_version_calendar_html(calendar)
    assets_seconds = perf_counter() - started
    html_path = _write_temp_html(prepared.html)
    try:
        screenshot_started = perf_counter()
        output = await screenshot_web_element(
            html_path.resolve().as_uri(),
            ".version-calendar",
            viewport=(CALENDAR_WIDTH, CALENDAR_HEIGHT),
            timeout_ms=20000,
            max_height=CALENDAR_HEIGHT,
            device_scale_factor=1.0,
            settle_ms=80,
            resources=prepared.resources,
            wait_for_images=True,
            strict_max_height=True,
            overflow_selectors=(".calendar-section", ".section-timeline"),
        )
        optimize_started = perf_counter()
        optimized = await run_image_render(optimize_png_container, output)
        logger.info(
            f"[endfield] draw kind=version_calendar assets={assets_seconds:.3f}s "
            f"screenshot={optimize_started - screenshot_started:.3f}s "
            f"png_optimize={perf_counter() - optimize_started:.3f}s "
            f"bytes={len(output)}->{len(optimized)} revision={calendar.revision}"
        )
        return optimized
    finally:
        schedule_temp_file_cleanup(html_path, delay_seconds=30)


def render_version_calendar_html(
    calendar: VersionCalendar,
    asset_urls: dict[str, str] | None = None,
) -> str:
    asset_urls = asset_urls or {}
    start = datetime.fromisoformat(calendar.starts_at)
    end = datetime.fromisoformat(calendar.ends_at)
    total_seconds = max(1.0, (end - start).total_seconds())
    ticks = _date_ticks(start, end)
    month_bands = _month_bands(start, end)
    phase_at = _phase_boundary(calendar, start, end)
    phase_left = (phase_at - start).total_seconds() / total_seconds * 100

    sections = "".join(
        _render_section(
            section,
            tuple(entry for entry in calendar.entries if entry.section == section.id),
            start,
            end,
            total_seconds,
            asset_urls,
        )
        for section in calendar.sections
    )
    rendered_ticks = "".join(
        (
            f'<div class="day-tick{" major" if tick.day in {1, start.day} else ""}" '
            f'style="left:{position:.5f}%">'
            f'<span>{tick.day:02d}</span></div>'
        )
        for tick, position in ticks
    )
    rendered_months = "".join(
        f'<div class="month-band" style="left:{left:.5f}%;width:{width:.5f}%">'
        f'{month:02d}月</div>'
        for month, left, width in month_bands
    )
    version = _esc(calendar.version)
    title = _esc(calendar.title)
    english_title = _esc(calendar.english_title)
    source = _esc_attr(calendar.official_source)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: #d7d7d7; }}
body {{
  font-family: "Microsoft YaHei", "Noto Sans CJK SC", Arial, sans-serif;
  color: #101010;
  -webkit-font-smoothing: antialiased;
}}
.version-calendar {{
  width: {CALENDAR_WIDTH}px;
  height: {CALENDAR_HEIGHT}px;
  overflow: hidden;
  position: relative;
  background:
    repeating-linear-gradient(135deg, rgba(0,0,0,.017) 0 2px, transparent 2px 8px),
    #f7f7f5;
}}
.header {{
  height: {HEADER_HEIGHT}px;
  position: relative;
  overflow: hidden;
  background:
    linear-gradient(90deg, rgba(0,0,0,.07), transparent 23%, transparent 72%, rgba(0,0,0,.11)),
    repeating-linear-gradient(0deg, transparent 0 12px, rgba(0,0,0,.035) 13px, transparent 14px),
    #f8f8f7;
  border-bottom: 8px solid #0b0b0b;
}}
.header::before {{
  content: "ENDFIELD";
  position: absolute;
  left: 79px;
  top: 34px;
  font-family: Impact, "Arial Black", sans-serif;
  font-size: 152px;
  letter-spacing: -8px;
  color: rgba(0,0,0,.115);
  transform: scaleX(1.08);
  transform-origin: left center;
}}
.header::after {{
  content: "";
  position: absolute;
  left: 0; right: 0; bottom: 22px; height: 20px;
  background:
    linear-gradient(90deg, #111 0 56%, transparent 56%),
    repeating-linear-gradient(90deg, #bfc0c2 0 48px, #efefef 48px 55px);
  opacity: .68;
  clip-path: polygon(0 34%, 55% 34%, 55% 0, 100% 0, 100% 100%, 0 100%);
}}
.header-scratches {{
  position: absolute;
  inset: 0;
  opacity: .28;
  background:
    radial-gradient(circle at 8% 23%, #111 0 1px, transparent 2px),
    radial-gradient(circle at 31% 38%, #d00018 0 2px, transparent 3px),
    linear-gradient(112deg, transparent 0 19%, rgba(0,0,0,.34) 19.2%, transparent 19.6%),
    repeating-linear-gradient(94deg, transparent 0 31px, rgba(0,0,0,.12) 32px, transparent 33px);
}}
.homecoming {{
  position: absolute;
  left: 23px;
  top: 74px;
  height: 47px;
  padding: 17px 13px 0 62px;
  border-left: 5px solid #c70018;
  border-top: 1px solid #111;
  border-bottom: 1px solid #111;
  font: 8px/1 Arial, sans-serif;
  letter-spacing: 1.2px;
  color: #444;
  background: rgba(255,255,255,.74);
}}
.main-title {{
  position: absolute;
  left: 72px;
  top: 79px;
  display: flex;
  align-items: baseline;
  gap: 22px;
  z-index: 2;
}}
.main-title .version-name {{
  font-family: "SimSun", "Songti SC", serif;
  font-weight: 900;
  font-size: 55px;
  letter-spacing: -5px;
  padding: 0 15px 4px 10px;
  background: rgba(255,255,255,.87);
  box-shadow: 7px 0 0 rgba(197,0,20,.16);
}}
.main-title .calendar-name {{
  font-family: "SimSun", "Songti SC", serif;
  font-weight: 500;
  font-size: 51px;
  letter-spacing: -3px;
  white-space: nowrap;
}}
.main-title .calendar-name::before {{
  content: "";
  display: inline-block;
  width: 5px; height: 48px; margin-right: 17px;
  background: #111;
  transform: skewX(-12deg) translateY(8px);
}}
.version-kicker {{
  position: absolute;
  left: 30px;
  top: 31px;
  font: 700 14px/1 "Arial Narrow", Arial, sans-serif;
  letter-spacing: 8px;
  color: rgba(20,20,20,.72);
}}
.version-kicker span {{ color: #c90016; }}
.brand {{
  position: absolute;
  right: 0; top: 0;
  z-index: 3;
  width: 128px;
  height: 196px;
  color: white;
  background: #080808;
  border-bottom: 5px solid #c90016;
  text-align: center;
}}
.brand .cn {{
  margin-top: 39px;
  font: 900 43px/.78 "SimHei", sans-serif;
  letter-spacing: -7px;
}}
.brand .en {{
  margin: 12px 12px 0;
  font: 700 7px/1 Arial, sans-serif;
  letter-spacing: .1px;
}}
.brand .marks {{
  margin-top: 19px;
  color: #d6001c;
  font: 900 16px/1 Arial, sans-serif;
  letter-spacing: 8px;
}}
.axis {{
  height: {AXIS_HEIGHT}px;
  position: relative;
  border-bottom: 1px solid #a7a7a7;
  background: linear-gradient(#f6f6f5, #e6e6e4 54%, #fafafa 55%);
}}
.axis::before {{
  content: "";
  position: absolute;
  left: {SIDEBAR_WIDTH}px; top: 0; bottom: 0; width: 1px;
  background: #aaa;
}}
.date-range {{
  position: absolute;
  left: {SIDEBAR_WIDTH}px;
  right: 0;
  top: 4px;
  height: 25px;
  z-index: 4;
  pointer-events: none;
}}
.date-range b {{
  position: absolute;
  top: 0;
  padding: 2px 9px 1px;
  color: white;
  background: #181818;
  font: 700 20px/1 "Arial Narrow", Arial, sans-serif;
  box-shadow: -4px 0 0 #c80018;
}}
.date-range b:first-child {{ left: 13px; }}
.timeline-axis {{
  position: absolute;
  left: {SIDEBAR_WIDTH}px; right: 0; top: 0; bottom: 0;
}}
.month-band {{
  position: absolute;
  top: 0;
  height: 18px;
  text-align: center;
  color: #777;
  font: 700 11px/18px Arial, sans-serif;
  border-left: 1px solid #bbb;
}}
.day-tick {{
  position: absolute;
  top: 20px;
  bottom: 0;
  width: 1px;
  border-left: 1px solid rgba(0,0,0,.12);
}}
.day-tick span {{
  position: absolute;
  left: 2px; top: 1px;
  color: #a4a4a4;
  font: 700 8px/1 Arial, sans-serif;
}}
.day-tick.major {{ border-left-color: rgba(185,0,20,.55); }}
.day-tick.major span {{ color: #a90016; }}
.calendar-section {{
  position: relative;
  width: 100%;
  height: var(--section-height);
  border-bottom: 1px solid #b5b5b5;
  background: rgba(255,255,255,.6);
}}
.section-label {{
  position: absolute;
  left: 40px; top: 0; bottom: 0;
  width: {SIDEBAR_WIDTH - 40}px;
  z-index: 4;
  color: white;
  overflow: hidden;
  background:
    linear-gradient(105deg, rgba(255,255,255,.14), transparent 28%),
    linear-gradient(90deg, #272727, #111);
  border-right: 5px solid #d0d0d0;
}}
.section-label::before {{
  content: "";
  position: absolute;
  left: -19px; top: -9px; bottom: -9px;
  width: 39px;
  background: var(--accent);
  transform: skewX(-8deg);
}}
.section-label::after {{
  content: "";
  position: absolute;
  right: 4px; top: 9px; bottom: 9px; width: 2px;
  background: repeating-linear-gradient(#cf001b 0 4px, transparent 4px 9px);
}}
.section-symbol {{
  position: relative;
  z-index: 2;
  margin: 9px 0 3px 10px;
  font: 900 28px/1 Arial, sans-serif;
}}
.section-cn {{
  position: relative;
  z-index: 2;
  margin-left: 11px;
  font: 700 15px/1 "Microsoft YaHei", sans-serif;
}}
.section-en {{
  position: relative;
  z-index: 2;
  margin: 5px 0 0 11px;
  font: 700 6px/1 Arial, sans-serif;
  letter-spacing: .6px;
  color: #aaa;
  writing-mode: vertical-rl;
}}
.section-ornament {{
  position: absolute;
  left: 0; top: 0; bottom: 0;
  width: 40px;
  background:
    linear-gradient(98deg, #050505 0 45%, #c80018 46% 61%, #131313 62%),
    #111;
  border-right: 1px solid #000;
}}
.section-timeline {{
  position: absolute;
  left: {SIDEBAR_WIDTH}px; right: 0; top: 0; bottom: 0;
  overflow: hidden;
  background:
    repeating-linear-gradient(135deg, rgba(0,0,0,.024) 0 2px, transparent 2px 7px),
    repeating-linear-gradient(90deg, transparent 0 calc(100% / 48 - 1px), rgba(0,0,0,.075) calc(100% / 48 - 1px) calc(100% / 48)),
    #fafafa;
}}
.section-timeline::after {{
  content: "";
  position: absolute;
  inset: 0;
  pointer-events: none;
  background: repeating-linear-gradient(0deg, transparent 0 calc(var(--row-height) - 1px), rgba(0,0,0,.08) calc(var(--row-height) - 1px) var(--row-height));
}}
.entry {{
  position: absolute;
  top: var(--top);
  left: var(--left);
  width: var(--width);
  height: var(--entry-height);
  min-width: 28px;
  overflow: visible;
  z-index: 2;
  color: #111;
  border-left: 5px solid var(--entry-accent, #c90016);
  background:
    linear-gradient(90deg, rgba(255,255,255,.97), rgba(225,229,229,.88) 50%, rgba(187,196,199,.55));
  box-shadow: 0 1px 0 rgba(0,0,0,.22), 0 -1px 0 rgba(255,255,255,.68);
  isolation: isolate;
}}
.entry::before {{
  content: "";
  position: absolute;
  inset: 0;
  z-index: -1;
  background:
    linear-gradient(110deg, rgba(255,255,255,.45), transparent 45%),
    repeating-linear-gradient(0deg, transparent 0 6px, rgba(0,0,0,.03) 7px);
}}
.entry-art {{
  position: absolute;
  right: 0; top: 0;
  width: 58%; height: 100%;
  object-fit: contain;
  object-position: 83% 50%;
  z-index: -1;
  filter: saturate(.9) contrast(1.03);
}}
.entry-art-shade {{
  position: absolute;
  inset: 0;
  z-index: -1;
  background: linear-gradient(90deg, rgba(255,255,255,.97) 8%, rgba(255,255,255,.68) 44%, transparent 72%);
}}
.entry-date {{
  position: absolute;
  top: -9px; left: -5px;
  height: 14px;
  padding: 1px 6px 0;
  color: white;
  background: #8f0011;
  font: 800 10px/13px "Arial Narrow", Arial, sans-serif;
  box-shadow: 2px 0 0 rgba(0,0,0,.26);
  z-index: 5;
}}
.entry-text {{
  position: absolute;
  left: 13px; right: 6px; top: 7px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  z-index: 3;
}}
.entry-title {{
  display: inline;
  font-weight: 800;
  font-size: 19px;
  line-height: 1;
  letter-spacing: -.8px;
}}
.entry-subtitle {{
  display: inline;
  margin-left: 8px;
  padding: 1px 4px;
  font-size: 10px;
  line-height: 1;
  background: rgba(255,255,255,.8);
}}
.entry.hero {{
  border-left-width: 7px;
  background: linear-gradient(90deg, #141b1c, #d8f1ee 44%, #43a7a4);
}}
.entry.hero .entry-text {{ top: 22px; left: 20px; color: white; text-shadow: 0 1px 2px #000; }}
.entry.hero .entry-title {{ font-size: 30px; font-family: "SimSun", serif; }}
.entry.hero .entry-subtitle {{ display: block; width: fit-content; margin: 8px 0 0; color: #111; text-shadow: none; }}
.entry.hero .entry-art {{ width: 64%; object-position: 86% 43%; }}
.entry.hero .entry-art-shade {{ background: linear-gradient(90deg, rgba(8,14,15,.92) 2%, rgba(13,37,38,.58) 39%, transparent 74%); }}
.entry.violet {{ --entry-accent: #7b23b6; }}
.entry.hero.violet {{ background: linear-gradient(90deg, #23162d, #e1caf0 46%, #b84ee8); }}
.entry.hero.violet .entry-art-shade {{ background: linear-gradient(90deg, rgba(26,8,35,.92), rgba(64,19,78,.5) 43%, transparent 75%); }}
.entry.steel {{ background: linear-gradient(90deg, #f7faf8, #c8d1d2 58%, #7d898c); }}
.entry.dark {{ color: #fff; background: linear-gradient(90deg, #131719, #343b3d 68%, #181c1d); }}
.entry.dark .entry-subtitle, .entry.black .entry-subtitle {{ color: #111; }}
.entry.yellow {{ --entry-accent: #d7e900; }}
.entry.region {{ background: linear-gradient(90deg, #f0eee4, #aa9b83 40%, #dbd4c4); }}
.entry.region.slate {{ background: linear-gradient(90deg, #e2e5e4, #87949a 48%, #d6d9d9); color: #111; }}
.entry.region.violet {{ background: linear-gradient(90deg, #d9d8e8, #777ad1 54%, #d1d4ef); color: #111; }}
.entry.region.cyan {{ background: linear-gradient(90deg, #d9eef1, #7bbdcc 64%, #dceef2); }}
.entry.event.black, .entry.gameplay.black {{
  color: white;
  background: linear-gradient(90deg, #101010, #2a2a2a 72%, #101010);
}}
.entry.event.yellow {{
  background: linear-gradient(90deg, #ecf100, #f4f8b2 54%, #9d9f00);
}}
.entry.story {{ background: linear-gradient(90deg, #191220, #8e49ac 58%, #d6b5e3); color: white; }}
.entry.gift {{ --entry-accent: #d9e800; background: linear-gradient(90deg, #f7f7ef, #e5e69b 64%, #eff0dd); }}
.entry.orange {{ --entry-accent: #f14d1a; background: linear-gradient(90deg, #fff2e8, #e7652c 55%, #fff0e4); }}
.entry.red {{ --entry-accent: #d80018; }}
.entry.gold {{ --entry-accent: #dba200; background: linear-gradient(90deg, #101010, #b98218 60%, #e5c06b); color: white; }}
.entry.supply {{ --entry-accent: #cddb00; background: linear-gradient(90deg, #171717, #8f941a 68%, #d8db65); color: white; }}
.entry.puzzle {{ --entry-accent: #65abd8; background: linear-gradient(90deg, #1e2d36, #3f789b 62%, #c2dcea); color: white; }}
.entry.gameplay {{ background: linear-gradient(90deg, #ece8e5, #d64035 58%, #f0b1a5); color: #111; }}
.entry.gameplay.orange {{ background: linear-gradient(90deg, #e8aa9c, #ce5741 55%, #f0c8bd); color: #3c0905; }}
.entry.gameplay.pale {{ opacity: .78; }}
.entry.thin .entry-title {{ font-size: 16px; }}
.entry.thin .entry-text {{ top: 4px; }}
.footer {{
  height: {FOOTER_HEIGHT}px;
  position: relative;
  overflow: hidden;
  color: white;
  background: #060606;
  border-top: 6px solid #c90016;
}}
.footer::before {{
  content: "ENDFIELD";
  position: absolute;
  left: 114px; top: -54px;
  font: 900 118px/1 Impact, sans-serif;
  letter-spacing: -4px;
  color: rgba(255,255,255,.09);
}}
.footer .source {{
  position: absolute;
  right: 23px; bottom: 16px;
  font: 9px/1.5 Arial, "Microsoft YaHei", sans-serif;
  color: #a8a8a8;
  text-align: right;
}}
.footer .copyright {{
  position: absolute;
  left: 25px; bottom: 17px;
  font: 8px/1 Arial, sans-serif;
  color: #777;
  letter-spacing: .5px;
}}
</style>
</head>
<body>
<main class="version-calendar" data-version="{version}" data-revision="{_esc_attr(calendar.revision)}">
  <header class="header">
    <div class="header-scratches"></div>
    <div class="version-kicker">ARKNIGHTS <span>/</span> ENDFIELD</div>
    <div class="homecoming">{english_title}</div>
    <div class="main-title"><span class="version-name">{title}</span><span class="calendar-name">版本日历</span></div>
    <div class="brand"><div class="cn">终<br>末<br>地</div><div class="en">ARKNIGHTS<br>ENDFIELD</div><div class="marks">≡≡≡</div></div>
  </header>
  <div class="axis">
    <div class="date-range"><b>{start:%m.%d}</b><b style="left:{phase_left:.5f}%">{phase_at:%m.%d}</b></div>
    <div class="timeline-axis">{rendered_months}{rendered_ticks}</div>
  </div>
  {sections}
  <footer class="footer">
    <div class="copyright">© HYPERGRYPH · DATA-DRIVEN CALENDAR</div>
    <div class="source">数据：AkeData / 游戏内配置<br>实际开放时间与内容请以游戏内公告为准 · <span data-source="{source}">v{version}</span></div>
  </footer>
</main>
</body>
</html>"""


def _render_section(
    section: VersionCalendarSection,
    entries: tuple[VersionCalendarEntry, ...],
    calendar_start: datetime,
    calendar_end: datetime,
    total_seconds: float,
    asset_urls: dict[str, str],
) -> str:
    section_height = section.rows * section.row_height + SECTION_GAP
    rendered_entries = "".join(
        _render_entry(
            entry,
            calendar_start,
            calendar_end,
            total_seconds,
            section.row_height,
            asset_urls,
        )
        for entry in entries
    )
    return f"""<section class="calendar-section" data-section="{_esc_attr(section.id)}"
  style="--section-height:{section_height}px;--row-height:{section.row_height}px;--accent:{_esc_attr(section.accent)}">
  <div class="section-ornament"></div>
  <aside class="section-label">
    <div class="section-symbol">{_esc(section.symbol)}</div>
    <div class="section-cn">{_esc(section.label)}</div>
    <div class="section-en">{_esc(section.english)}</div>
  </aside>
  <div class="section-timeline">{rendered_entries}</div>
</section>"""


def _render_entry(
    entry: VersionCalendarEntry,
    calendar_start: datetime,
    calendar_end: datetime,
    total_seconds: float,
    row_height: int,
    asset_urls: dict[str, str],
) -> str:
    starts_at = max(calendar_start, datetime.fromisoformat(entry.start_at))
    raw_end = datetime.fromisoformat(entry.end_at) if entry.end_at else calendar_end
    ends_at = min(calendar_end, raw_end)
    left = max(0.0, min(100.0, (starts_at - calendar_start).total_seconds() / total_seconds * 100))
    right = max(left, min(100.0, (ends_at - calendar_start).total_seconds() / total_seconds * 100))
    width = max(0.4, right - left)
    top = entry.lane * row_height + 4
    height = row_height - 8
    classes = " ".join(_class_token(token) for token in entry.style.split() if token)
    accent = entry.accent or "#c90016"
    resolved_art = asset_urls.get(entry.art_url, entry.art_url)
    art = (
        f'<img class="entry-art" src="{_esc_attr(resolved_art)}" alt="">'
        if resolved_art
        else ""
    )
    return f"""<article class="entry {classes}" data-source-kind="{_esc_attr(entry.source_kind)}"
  data-source-id="{_esc_attr(entry.source_id)}"
  style="--left:{left:.5f}%;--width:{width:.5f}%;--top:{top}px;--entry-height:{height}px;--entry-accent:{_esc_attr(accent)}">
  {art}<div class="entry-art-shade"></div>
  <div class="entry-date">{starts_at:%m.%d}</div>
  <div class="entry-text"><span class="entry-title">{_esc(entry.title)}</span><span class="entry-subtitle">{_esc(entry.subtitle)}</span></div>
</article>"""


def _date_ticks(start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    ticks: list[tuple[datetime, float]] = []
    cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
    if cursor < start:
        cursor += timedelta(days=1)
    total = max(1.0, (end - start).total_seconds())
    while cursor <= end:
        ticks.append((cursor, (cursor - start).total_seconds() / total * 100))
        cursor += timedelta(days=1)
    return ticks


def _month_bands(start: datetime, end: datetime) -> list[tuple[int, float, float]]:
    total = max(1.0, (end - start).total_seconds())
    cursor = start
    result: list[tuple[int, float, float]] = []
    while cursor < end:
        if cursor.month == 12:
            next_month = cursor.replace(year=cursor.year + 1, month=1, day=1)
        else:
            next_month = cursor.replace(month=cursor.month + 1, day=1)
        band_end = min(end, next_month)
        left = (cursor - start).total_seconds() / total * 100
        width = (band_end - cursor).total_seconds() / total * 100
        result.append((cursor.month, left, width))
        cursor = band_end
    return result


def _phase_boundary(
    calendar: VersionCalendar,
    start: datetime,
    end: datetime,
) -> datetime:
    gacha_starts = sorted(
        {
            datetime.fromisoformat(entry.start_at)
            for entry in calendar.entries
            if entry.section == "gacha" and start < datetime.fromisoformat(entry.start_at) < end
        }
    )
    if gacha_starts:
        return gacha_starts[-1]
    return start + (end - start) / 2


def _class_token(value: str) -> str:
    return "".join(char for char in value if char.isalnum() or char in {"-", "_"})


def _write_temp_html(content: str) -> Path:
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as file:
        file.write(content)
        return Path(file.name)


def _esc(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def _esc_attr(value: object) -> str:
    return html.escape(str(value or ""), quote=True)
