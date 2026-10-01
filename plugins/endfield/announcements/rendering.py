"""Activity digest PNGs using the existing Endfield calendar rendering stack."""

from __future__ import annotations

import asyncio
import html
import tempfile
from functools import lru_cache
from pathlib import Path

from loguru import logger

from otae_bot.infrastructure.rendering.browser import (
    BrowserResource,
    screenshot_web_element,
)
from otae_bot.infrastructure.rendering.executor import run_image_render
from otae_bot.infrastructure.rendering.temp_files import schedule_temp_file_cleanup
from otae_bot.paths import PROJECT_ROOT

from ..rendering.cards import _prepare_assets, optimize_png_container
from .models import KINDS, AnnouncementDigest, local_time
from .presentation import activity_state, remaining

CARD_WIDTH = 1080
MAX_HEIGHT = 20000
PHASE_LABELS = {
    "news": "新公告",
    "updated": "公告更新",
    "start": "开始提醒",
    "started": "活动开启",
    "end": "结束提醒",
    "maintenance": "维护提醒",
}


@lru_cache(maxsize=1)
def font_resources() -> dict[str, BrowserResource]:
    folder = PROJECT_ROOT / "assets/font/steamInfo"
    return {
        f"https://endfield.local/fonts/{weight}.ttf": BrowserResource(
            (folder / f"MiSans-{weight}.ttf").read_bytes(), "font/ttf"
        )
        for weight in ("Regular", "Bold")
    }


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_digest_html(
    digest: AnnouncementDigest, assets: dict[str, str] | None = None
) -> str:
    if not digest.cards:
        raise ValueError("Cannot render an empty announcement digest")
    assets = assets or {}
    rows = []
    for index, card in enumerate(digest.cards, 1):
        url = assets.get(card.image_url, "")
        art = (
            f'<img src="{_esc(url)}" alt="活动图片">'
            if url
            else '<div class="missing"><b>ENDFIELD</b><span>活动图片暂不可用</span></div>'
        )
        if url:
            art += f'<span class="art-caption">{_esc(card.image_caption)}</span>'
        countdown_label, countdown = remaining(card, digest.generated_at)
        state = activity_state(card, digest.generated_at)
        reasons = " / ".join(PHASE_LABELS.get(phase, phase) for phase in card.phases)
        start = (
            local_time(card.start_at)
            if card.start_at
            else (card.start_hint or "未公布具体时间")
        )
        end = (
            local_time(card.end_at)
            if card.end_at
            else (card.end_hint or "未公布具体时间")
        )
        summary = card.summary or "活动内容详见官方公告。"
        # Character bounds keep tall digests legible without hiding date rows.
        summary = summary if len(summary) <= 150 else summary[:149] + "…"
        rows.append(f"""<article class="activity">
          <div class="activity-heading"><div><span class="number">{index:02d}</span><span class="kind">{_esc(KINDS.get(card.kind, "公告"))}</span><span class="reason">{_esc(reasons)}</span></div><span class="state">{_esc(state)}</span></div>
          <div class="body"><div class="art">{art}</div><div class="copy"><h2>{_esc(card.title)}</h2><p>{_esc(summary)}</p></div></div>
          <div class="times"><div><label>开始时间</label><strong>{_esc(start)}</strong></div><div><label>结束时间</label><strong>{_esc(end)}</strong></div><div class="countdown"><label>{_esc(countdown_label)}</label><strong>{_esc(countdown)}</strong></div></div>
          <div class="source"><span>公告发布 {_esc(local_time(card.published_at))}</span><span>{_esc(card.url)}</span></div>
        </article>""")
    # Many simultaneous activities still form one PNG; two columns keep its
    # height bounded without dropping entries or splitting their receipts.
    columns = len(rows) > 8
    width = 1600 if columns else CARD_WIDTH
    opening = all(card.phases == ("started",) for card in digest.cards)
    title = "终末地 · 活动开启" if opening else "终末地 · 活动公告"
    start_times = {card.start_at for card in digest.cards}
    subtitle = (
        f"{local_time(digest.cards[0].start_at)} 开启 · 北京时间"
        if opening and len(start_times) == 1
        else f"截至 {local_time(digest.generated_at)} · 北京时间"
    )
    total = "同期开启" if opening else "本轮"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data: https://endfield.local; font-src https://endfield.local; style-src 'unsafe-inline'">
<style>
@font-face{{font-family:Mi;src:url('https://endfield.local/fonts/Regular.ttf')}}
@font-face{{font-family:Mi;src:url('https://endfield.local/fonts/Bold.ttf');font-weight:700}}
*{{box-sizing:border-box}}html,body{{margin:0;padding:0;background:#e5e5e3;color:#17191a;font-family:Mi,'Microsoft YaHei',sans-serif;-webkit-font-smoothing:antialiased}}
.announcement-digest{{width:{width}px;background:repeating-linear-gradient(135deg,rgba(0,0,0,.018) 0 2px,transparent 2px 8px),#f5f5f2}}
.masthead{{position:relative;padding:32px 40px 28px;border-top:12px solid #151719;border-bottom:6px solid #17191a;overflow:hidden}}
.watermark{{position:absolute;right:-5px;top:28px;font:900 110px/1 'Arial Black',sans-serif;letter-spacing:-6px;color:rgba(0,0,0,.055)}}
.kicker{{font-size:14px;letter-spacing:3px;font-weight:700;color:#595d60;position:relative}}.kicker b{{color:#ba1731;margin-right:12px}}
h1{{position:relative;font-size:48px;letter-spacing:3px;line-height:1.35;margin:15px 0 14px;font-weight:700}}
.subline{{position:relative;display:flex;align-items:center;justify-content:space-between;font-size:17px;color:#686c6e}}.total{{padding:4px 12px;background:#ba1731;color:white;font-size:17px}}
.activities{{padding:24px 28px;display:grid;grid-template-columns:{"repeat(2,minmax(0,1fr))" if columns else "1fr"};gap:20px}}
.activity{{min-width:0;background:#fff;border:1px solid #cdd0d0;border-left:5px solid #272a2c;align-self:start}}
.activity-heading{{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:14px 20px 12px;border-bottom:1px solid #e2e3e3}}
.activity-heading>div{{display:flex;align-items:center;gap:12px;min-width:0}}.number{{font:700 21px/1 Arial,sans-serif;color:#b71c32}}.kind{{font-size:15px;padding:3px 8px;background:#eeefed}}.reason{{font-size:15px;color:#727777}}.state{{font-size:16px;font-weight:700;white-space:nowrap}}
.body{{display:grid;grid-template-columns:{"270px" if columns else "360px"} minmax(0,1fr);gap:24px;padding:20px}}
.art{{position:relative;width:100%;aspect-ratio:16/9;background:#eff0ed;display:flex;align-items:center;justify-content:center;overflow:hidden}}.art img{{display:block;width:100%;height:100%;object-fit:contain}}.art-caption{{position:absolute;right:0;bottom:0;padding:3px 7px;background:rgba(15,20,22,.74);color:#fff;font-size:11px}}
.missing{{display:flex;flex-direction:column;align-items:center;gap:12px;color:#888e8e}}.missing b{{font:700 29px Arial,sans-serif;letter-spacing:2px}}.missing span{{font-size:16px}}
.copy{{min-width:0}}h2{{font-size:{24 if columns else 27}px;line-height:1.45;margin:0 0 12px;font-weight:700;overflow-wrap:anywhere}}.copy p{{font-size:{18 if columns else 20}px;line-height:1.65;color:#5a6062;margin:0;overflow-wrap:anywhere}}
.times{{margin:0 20px;display:grid;grid-template-columns:1.15fr 1.15fr 1fr;border-top:1px solid #d9dcdc;border-bottom:1px solid #d9dcdc}}
.times>div{{padding:14px 12px 14px 0;min-width:0}}.times>div+div{{border-left:1px solid #e2e4e4;padding-left:18px}}label{{display:block;font-size:14px;color:#777f80;margin-bottom:6px;letter-spacing:1px}}strong{{display:block;font-size:{18 if columns else 21}px;line-height:1.4;font-weight:700;overflow-wrap:anywhere}}
.countdown{{background:#f8f0f1;padding-right:10px!important}}.countdown label,.countdown strong{{color:#ae2134}}
.source{{padding:12px 20px 15px;display:flex;justify-content:space-between;gap:12px;color:#82888a;font-size:12px;line-height:1.5;overflow-wrap:anywhere}}.source span{{min-width:0}}
footer{{padding:2px 32px 24px;font-size:14px;color:#747a7b;display:flex;justify-content:space-between;gap:20px}}footer b{{color:#303637;letter-spacing:2px;font-size:13px}}
</style></head><body><main class="announcement-digest">
<header class="masthead"><div class="watermark">ENDFIELD</div><div class="kicker"><b>◼</b>ENDFIELD / ACTIVITY BULLETIN</div><h1>{_esc(title)}</h1><div class="subline"><span>{_esc(subtitle)}</span><span class="total">{total} {len(rows)} 项</span></div></header>
<section class="activities">{"".join(rows)}</section><footer><span>日期以官方公告为准 · 未明确的时间不推算倒计时</span><b>OTAE BOT × ENDFIELD</b></footer>
</main></body></html>"""


async def draw_digest(digest: AnnouncementDigest) -> bytes:
    assets, resources = {}, dict(font_resources())
    try:
        prepared = await asyncio.wait_for(
            _prepare_assets((card.image_url for card in digest.cards), inline=False), 35
        )
        assets.update(prepared.urls)
        resources.update(prepared.resources)
    except asyncio.TimeoutError:
        logger.warning(
            "[endfield-announcements] artwork timeout; rendering placeholders"
        )
    rendered = render_digest_html(digest, assets)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".html", encoding="utf-8", delete=False
    ) as file:
        file.write(rendered)
        path = Path(file.name)
    try:
        png = await screenshot_web_element(
            path.as_uri(),
            ".announcement-digest",
            viewport=(1600 if len(digest.cards) > 8 else CARD_WIDTH, 1),
            timeout_ms=20000,
            max_height=MAX_HEIGHT,
            device_scale_factor=1,
            settle_ms=50,
            resources=resources,
            wait_for_images=True,
            wait_for_fonts=True,
            strict_max_height=True,
            overflow_selectors=(".activity", ".copy", ".times", ".source"),
        )
        return await run_image_render(optimize_png_container, png)
    finally:
        schedule_temp_file_cleanup(path, delay_seconds=30)
