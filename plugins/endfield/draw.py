from __future__ import annotations

import base64
import colorsys
import html
import hashlib
import mimetypes
import re
import struct
import tempfile
import unicodedata
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Iterable

import cv2
import numpy as np
from loguru import logger
from PIL import Image

from utils.http_client import fetch_many
from utils.image_executor import run_image_render
from utils.image_utils import BrowserResource, screenshot_web_element
from utils.temp_files import schedule_temp_file_cleanup

from .models import EffectView, LEVEL_COLUMNS, OperatorView, SkillView, TermStyleView, WeaponSkillView, WeaponView


OPERATOR_CARD_WIDTH = 1600
CARD_WIDTH = OPERATOR_CARD_WIDTH
CARD_MIN_HEIGHT = 780
CARD_MAX_HEIGHT = 6144
OPERATOR_RAIL_HEIGHT = 880
OPERATOR_ACCENT_LEFT = 440
ASSET_DIR = Path(__file__).resolve().parents[2] / "assets" / "image" / "endfield"
REMOTE_ASSET_NAMESPACE = "endfield-assets"
FALLBACK_TERM_STYLES = {
    "物理伤害": TermStyleView("物理伤害", "#e3c19a", ""),
    "击飞": TermStyleView("击飞", "#e3c19a", "https://static.warfarin.wiki/v4/termicon/icon_term_ba_airborne.webp"),
    "破防": TermStyleView("破防", "#e3c19a", "https://static.warfarin.wiki/v4/termicon/icon_term_ba_noguard.webp"),
    "物理异常": TermStyleView("物理异常", "#e3c19a", ""),
    "猛击": TermStyleView("猛击", "#e3c19a", "https://static.warfarin.wiki/v4/termicon/icon_term_ba_crush.webp"),
    "倒地": TermStyleView("倒地", "#e3c19a", "https://static.warfarin.wiki/v4/termicon/icon_term_ba_knockdown.webp"),
    "物理脆弱": TermStyleView("物理脆弱", "#e3c19a", "https://static.warfarin.wiki/v4/termicon/icon_term_ba_physicalvul.webp"),
    "碎甲": TermStyleView("碎甲", "#e3c19a", "https://static.warfarin.wiki/v4/termicon/icon_term_ba_fracture.webp"),
}
TEXT_ONLY_TERMS = {"消耗", "法术附着"}
PLAIN_TEXT_TERMS = {"法术脆弱"}
NO_ICON_TERMS = {"物理伤害", "法术伤害", "灼热伤害", "电磁伤害", "寒冷伤害", "自然伤害", "超域伤害"}
TERM_BOUNDARY_EXCEPTIONS = {"法术脆弱", "法术附着", "寒冷附着", "灼热附着", "电磁附着", "自然附着"}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_DROPPED_CHUNKS = {b"tEXt", b"zTXt", b"iTXt", b"tIME", b"eXIf"}
_PORTRAIT_LAYOUT_CACHE: OrderedDict[str, "PortraitLayout"] = OrderedDict()
_PORTRAIT_LAYOUT_OVERRIDES = {
    "莱万汀": (50.0, 46.0, 1.12),
    "骏卫": (50.0, 47.0, 1.08),
    "佩丽卡": (52.0, 45.0, 1.10),
    "弭弗": (50.0, 44.0, 1.10),
}


@dataclass(frozen=True, slots=True)
class PreparedCardHtml:
    html: str
    resources: dict[str, BrowserResource]
    width: int


@dataclass(frozen=True, slots=True)
class PortraitLayout:
    x: float = 50.0
    y: float = 45.0
    scale: float = 1.12


@dataclass(frozen=True, slots=True)
class _PreparedAssets:
    urls: dict[str, str]
    resources: dict[str, BrowserResource]
    contents: dict[str, bytes]


async def draw_operator_card(view: OperatorView) -> bytes:
    started = perf_counter()
    prepared = await prepare_operator_card_html(view)
    html_path = _write_temp_html(prepared.html)
    assets_seconds = perf_counter() - started
    try:
        screenshot_started = perf_counter()
        output = await screenshot_web_element(
            html_path.resolve().as_uri(),
            ".endfield-card",
            viewport=(prepared.width, 1),
            timeout_ms=15000,
            max_height=CARD_MAX_HEIGHT,
            device_scale_factor=2.0,
            settle_ms=50,
            resources=prepared.resources,
            wait_for_images=True,
            strict_max_height=True,
            overflow_selectors=(
                ".rail",
                ".info-box",
                ".potential-item",
                ".skill-card",
                ".effect-card",
                ".metric-table",
            ),
        )
        optimize_started = perf_counter()
        optimized = await run_image_render(optimize_png_container, output)
        logger.info(
            f"[endfield] draw kind=operator assets={assets_seconds:.3f}s "
            f"screenshot={optimize_started - screenshot_started:.3f}s "
            f"png_optimize={perf_counter() - optimize_started:.3f}s "
            f"bytes={len(output)}->{len(optimized)}"
        )
        return optimized
    finally:
        schedule_temp_file_cleanup(html_path, delay_seconds=30)


async def draw_weapon_card(view: WeaponView) -> bytes:
    started = perf_counter()
    prepared = await prepare_weapon_card_html(view)
    html_path = _write_temp_html(prepared.html)
    assets_seconds = perf_counter() - started
    try:
        screenshot_started = perf_counter()
        output = await screenshot_web_element(
            html_path.resolve().as_uri(),
            ".weapon-card",
            viewport=(prepared.width, 1),
            timeout_ms=15000,
            max_height=CARD_MAX_HEIGHT,
            device_scale_factor=2.0,
            settle_ms=50,
            resources=prepared.resources,
            wait_for_images=True,
            strict_max_height=True,
            overflow_selectors=(".rail", ".panel", ".skill-card", ".level-row"),
        )
        optimize_started = perf_counter()
        optimized = await run_image_render(optimize_png_container, output)
        logger.info(
            f"[endfield] draw kind=weapon assets={assets_seconds:.3f}s "
            f"screenshot={optimize_started - screenshot_started:.3f}s "
            f"png_optimize={perf_counter() - optimize_started:.3f}s "
            f"bytes={len(output)}->{len(optimized)}"
        )
        return optimized
    finally:
        schedule_temp_file_cleanup(html_path, delay_seconds=30)


async def render_operator_card_html(view: OperatorView) -> str:
    return (await _prepare_operator_card_html(view, inline=True)).html


async def prepare_operator_card_html(view: OperatorView) -> PreparedCardHtml:
    return await _prepare_operator_card_html(view, inline=False)


async def _prepare_operator_card_html(view: OperatorView, *, inline: bool) -> PreparedCardHtml:
    portrait_url = view.portrait_url or view.round_icon_url or view.icon_url
    skill_urls = {skill.icon_id: skill_icon_url(skill.icon_id) for skill in view.skills if skill.icon_id}
    talent_urls = {effect.effect_id: effect.icon_url for effect in view.talents if effect.icon_url}
    term_styles = merged_term_styles(view)
    used_terms = _operator_terms_used(view, term_styles)
    term_urls = {
        term: style.icon_url
        for term, style in term_styles.items()
        if style.icon_url and term in used_terms
    }
    assets = await _prepare_assets(
        [portrait_url, *skill_urls.values(), *talent_urls.values(), *term_urls.values()]
        ,
        inline=inline,
    )
    portrait = assets.urls.get(portrait_url, "")
    skill_icons = {key: assets.urls.get(url, "") for key, url in skill_urls.items()}
    talent_icons = {key: assets.urls.get(url, "") for key, url in talent_urls.items()}
    term_icons = {key: assets.urls.get(url, "") for key, url in term_urls.items()}
    layout = await _portrait_layout(view, assets.contents.get(portrait_url, b""))
    return PreparedCardHtml(
        _render_html(view, portrait, skill_icons, talent_icons, term_styles, term_icons, layout),
        assets.resources,
        OPERATOR_CARD_WIDTH,
    )


async def render_weapon_card_html(view: WeaponView) -> str:
    return (await _prepare_weapon_card_html(view, inline=True)).html


async def prepare_weapon_card_html(view: WeaponView) -> PreparedCardHtml:
    return await _prepare_weapon_card_html(view, inline=False)


async def _prepare_weapon_card_html(view: WeaponView, *, inline: bool) -> PreparedCardHtml:
    icon_urls = [view.icon_url, *_weapon_rich_icon_urls_used(view)]
    assets = await _prepare_assets(icon_urls, inline=inline)
    weapon_img = assets.urls.get(view.icon_url, "")
    rich_icons = {url: assets.urls.get(url, "") for url in icon_urls if url}
    width = weapon_card_width(view)
    return PreparedCardHtml(
        _render_weapon_html(view, weapon_img, rich_icons, width),
        assets.resources,
        width,
    )


def _render_html(
    view: OperatorView,
    portrait: str,
    skill_icons: dict[str, str],
    talent_icons: dict[str, str],
    term_styles: dict[str, TermStyleView],
    term_icons: dict[str, str],
    portrait_layout: PortraitLayout | None = None,
) -> str:
    card_height = estimate_card_height(view)
    portrait_layout = portrait_layout or PortraitLayout()
    level_labels = operator_level_labels(view)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing: border-box; }}
html, body {{
  margin: 0;
  width: {CARD_WIDTH}px;
  min-height: {card_height}px;
  background: #d9dde0;
  font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans SC", Arial, sans-serif;
  color: #171b1f;
}}
.endfield-card {{
  position: relative;
  width: {CARD_WIDTH}px;
  height: {card_height}px;
  overflow: hidden;
  background:
    linear-gradient(90deg, rgba(29,34,39,.08) 1px, transparent 1px) 0 0 / 40px 40px,
    linear-gradient(0deg, rgba(29,34,39,.08) 1px, transparent 1px) 0 0 / 40px 40px,
    linear-gradient(135deg, #f5f6f3 0%, #e6e9eb 58%, #cfd5d9 100%);
}}
.endfield-card::before {{
  content: "";
  position: absolute;
  left: {OPERATOR_ACCENT_LEFT}px;
  top: -80px;
  width: 360px;
  height: calc(100% + 160px);
  background: #f5c900;
  clip-path: polygon(22% 0, 100% 0, 78% 100%, 0 100%);
  opacity: .96;
}}
.endfield-card::after {{
  content: none;
}}
.rail {{
  position: absolute;
  left: 28px;
  top: 28px;
  z-index: 3;
  width: 390px;
  height: {OPERATOR_RAIL_HEIGHT}px;
  padding: 20px 22px;
  display: flex;
  flex-direction: column;
  overflow: visible;
  border: 1px solid rgba(29,34,39,.28);
  background: rgba(247,248,246,.92);
}}
.kicker {{ font-size: 17px; font-weight: 800; color: #6a7278; }}
.name {{ margin-top: 10px; font-size: 54px; line-height: .96; font-weight: 900; letter-spacing: 0; }}
.eng {{ margin-top: 6px; font-size: 17px; color: #5d656b; font-weight: 700; }}
.stars {{ display: flex; align-items: center; gap: 5px; margin-top: 10px; min-height: 34px; color: transparent; font-size: 0; line-height: 1; }}
.rarity-star {{ width: 32px; height: 32px; object-fit: contain; flex: 0 0 auto; filter: drop-shadow(0 1px 0 rgba(255,255,255,.65)) drop-shadow(0 2px 2px rgba(23,27,31,.22)); }}
.info-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 7px; margin-top: 13px; }}
.info-box {{ min-height: 50px; border-left: 5px solid #171b1f; background: rgba(255,255,255,.58); padding: 6px 8px; overflow: visible; }}
.info-label {{ font-size: 12px; color: #697279; font-weight: 800; }}
.info-value {{ margin-top: 3px; font-size: 18px; line-height: 1.08; font-weight: 900; white-space: normal; overflow-wrap: anywhere; }}
.tag-list {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 11px; }}
.tag-list:empty {{ display: none; }}
.tag {{ padding: 3px 8px; border: 1px solid rgba(23,27,31,.3); background: rgba(23,27,31,.08); font-size: 13px; font-weight: 800; }}
.left-title {{ display: flex; align-items: center; gap: 8px; margin: 15px 0 8px; font-size: 19px; font-weight: 900; border-top: 5px solid #171b1f; padding-top: 10px; }}
.left-title::before {{ content: ""; width: 10px; height: 22px; background: #ffd000; display: inline-block; }}
.potential-list {{ display: grid; gap: 7px; align-content: start; min-height: 0; }}
.potential-item {{
  display: grid;
  grid-template-columns: 68px 1fr;
  gap: 8px;
  min-height: 66px;
  padding: 7px 8px;
  align-items: center;
  border: 1px solid rgba(23,27,31,.22);
  background: rgba(255,255,255,.56);
}}
.potential-icon {{ width: 68px; height: 54px; border: 0; border-radius: 0; background: transparent; display: flex; align-items: center; justify-content: center; overflow: visible; color: transparent; font-size: 0; line-height: 0; }}
.potential-star-img {{ width: 58px; height: 58px; object-fit: contain; display: block; filter: drop-shadow(0 2px 2px rgba(23,27,31,.18)); }}
.potential-star-p5 {{ filter: drop-shadow(0 0 3px rgba(255,216,0,.38)) drop-shadow(0 2px 2px rgba(23,27,31,.18)); }}
.potential-title {{ font-size: 16px; line-height: 1.12; font-weight: 900; white-space: normal; overflow-wrap: anywhere; }}
.potential-desc {{ margin-top: 3px; color: #364047; font-size: 13px; line-height: 1.28; font-weight: 700; overflow: visible; white-space: normal; word-break: break-word; overflow-wrap: anywhere; }}
.potential-desc strong, .effect-desc strong {{ color: #11161a; background: #ffd000; padding: 0 3px; }}
.term {{ color: var(--term-color, #e3c19a); font-weight: 900; white-space: nowrap; }}
.term-plain {{ color: inherit; font-weight: 900; text-decoration: underline; text-decoration-thickness: 2px; text-underline-offset: 2px; white-space: nowrap; }}
.term-icon {{ width: 15px; height: 15px; object-fit: contain; vertical-align: -2px; margin: 0 2px 0 1px; }}
.rich-style, .vup, .info-note {{ font-weight: 900; }}
.info-note {{ color: #59636a; }}
.visual {{
  position: absolute;
  z-index: 1;
  left: 370px;
  top: 0;
  width: 520px;
  height: 100%;
  overflow: visible;
}}
.visual-frame {{ position: absolute; inset: 0; border: 1px solid rgba(23,27,31,.25); clip-path: polygon(0 0, 93% 0, 100% 7%, 100% 100%, 7% 100%, 0 93%); }}
.portrait {{
  position: absolute;
  left: -116px;
  top: 26px;
  width: 730px;
  height: calc(100% - 52px);
  border-radius: 0;
  border: 0;
  background: transparent;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}}
.portrait img {{ width: {portrait_layout.scale * 100:.2f}%; height: {portrait_layout.scale * 100:.2f}%; object-fit: contain; object-position: {portrait_layout.x:.2f}% {portrait_layout.y:.2f}%; filter: drop-shadow(0 28px 30px rgba(29,34,39,.30)); }}
.portrait-fallback {{ position: absolute; font-size: 110px; font-weight: 900; color: rgba(23,27,31,.20); }}
.panel {{
  position: absolute;
  z-index: 3;
  right: 24px;
  top: 28px;
  width: 780px;
  background: rgba(247,248,246,.93);
  border: 1px solid rgba(23,27,31,.32);
  padding: 15px 16px 14px;
  box-shadow: -12px 18px 44px rgba(23,27,31,.14);
}}
.panel-title {{ display: grid; grid-template-columns: 1fr auto; align-items: end; border-bottom: 4px solid #171b1f; padding-bottom: 10px; }}
.panel-title h1 {{ margin: 0; font-size: 34px; line-height: 1; font-weight: 900; }}
.columns {{ display: grid; grid-template-columns: 88px repeat(4, 1fr); gap: 0; width: 372px; border: 1px solid rgba(23,27,31,.28); }}
.columns span {{ padding: 6px 6px; background: #171b1f; color: #fff; font-size: 15px; font-weight: 900; text-align: center; border-left: 1px solid rgba(255,255,255,.2); }}
.columns span:first-child {{ color: #ffd000; border-left: 0; }}
.section-title {{ display: flex; align-items: center; gap: 10px; margin: 9px 0 6px; font-size: 19px; font-weight: 900; }}
.section-title::before {{ content: ""; width: 11px; height: 22px; background: #ffd000; display: inline-block; }}
.skill-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px 10px; align-items: start; }}
.skill-card {{ align-self: start; border: 1px solid rgba(23,27,31,.24); background: linear-gradient(180deg, rgba(255,255,255,.78), rgba(234,237,238,.84)); padding: 7px; position: relative; overflow: visible; }}
.skill-card::before {{ content: ""; position: absolute; top: 0; left: 0; width: 8px; height: 100%; background: #ffd000; }}
.skill-head {{ display: grid; grid-template-columns: 40px 1fr; gap: 7px; align-items: center; padding-left: 5px; }}
.round-icon {{ width: 36px; height: 36px; border-radius: 50%; background: #20252a; border: 2px solid #ffd000; overflow: hidden; display: grid; place-items: center; color: #ffd000; font-size: 13px; font-weight: 900; }}
.round-icon img {{ width: 100%; height: 100%; object-fit: cover; }}
.skill-name {{ font-size: 19px; line-height: 1.08; font-weight: 900; white-space: normal; overflow-wrap: anywhere; }}
.skill-cat {{ font-size: 12px; color: #6b7379; font-weight: 900; margin-top: 2px; }}
.skill-desc {{ margin: 7px 0 0 13px; color: #303941; font-size: 13px; line-height: 1.28; font-weight: 700; overflow: visible; }}
.skill-meta {{ margin: 6px 0 0 13px; display: grid; grid-template-columns: repeat(2, 1fr); gap: 4px; }}
.skill-meta:empty {{ display: none; }}
.skill-meta span {{ min-height: 22px; padding: 3px 5px; background: rgba(23,27,31,.08); color: #313940; font-size: 12.5px; font-weight: 900; text-align: center; white-space: normal; overflow-wrap: anywhere; }}
.skill-meta strong {{ color: #11161a; background: #ffd000; padding: 0 3px; }}
.metric-table {{ --metric-label-width: 92px; margin: 6px 0 0 13px; display: grid; grid-template-columns: var(--metric-label-width) repeat(4, minmax(46px, 1fr)); grid-auto-rows: minmax(22px, auto); border-top: 1px solid rgba(23,27,31,.2); border-left: 1px solid rgba(23,27,31,.2); }}
.metric-table div {{ min-height: 22px; padding: 3px 4px; border-right: 1px solid rgba(23,27,31,.2); border-bottom: 1px solid rgba(23,27,31,.2); font-size: 12.5px; font-weight: 800; line-height: 1.1; overflow: visible; }}
.metric-name {{ background: rgba(23,27,31,.08); color: #313940; white-space: normal; overflow-wrap: anywhere; word-break: break-word; display: flex; align-items: center; }}
.metric-name.long {{ font-size: 11.5px; line-height: 1.08; }}
.metric-value {{ background: #f9fbfa; text-align: center; color: #171b1f; display: flex; align-items: center; justify-content: center; white-space: normal; overflow-wrap: anywhere; word-break: break-word; }}
.metric-value.strong {{ background: #171b1f; color: #ffd000; font-weight: 900; }}
.effect-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 7px 10px; align-items: start; }}
.effect-card {{ align-self: start; display: grid; grid-template-columns: 40px 1fr; gap: 8px; border: 1px solid rgba(23,27,31,.24); background: rgba(255,255,255,.62); padding: 7px; }}
.effect-title {{ font-size: 16px; line-height: 1.1; font-weight: 900; white-space: normal; overflow-wrap: anywhere; }}
.effect-desc {{ margin-top: 3px; font-size: 13px; line-height: 1.28; color: #333b41; font-weight: 700; overflow: visible; }}
.footer-line {{ margin-top: 12px; padding-top: 12px; display: flex; justify-content: flex-end; color: #697279; font-size: 13px; line-height: 1.1; font-weight: 900; }}
</style>
</head>
<body>
<div class="endfield-card">
  <section class="rail">
    <div class="kicker">ARKNIGHTS: ENDFIELD</div>
    <div class="name">{esc(view.name)}</div>
    <div class="eng">{esc(view.english_name or view.slug)}</div>
    <div class="stars">{stars(view.rarity)}</div>
    <div class="info-grid">
      {info_box("职业", view.profession)}
      {info_box("属性", view.damage_type)}
      {info_box("武器", view.weapon_type)}
      {info_box(view.species_label or "种族", view.species or "未知")}
    </div>
    {tag_block(view.tags)}
    <div class="left-title">潜能效果</div>
    <div class="potential-list">{potential_items(view.potentials, term_styles, term_icons, operator_keyword_terms(view))}</div>
  </section>
  <section class="visual">
    <div class="visual-frame"></div>
    <div class="portrait">
      <div class="portrait-fallback">{esc((view.name or "?")[:1])}</div>
      {image(portrait, view.name)}
    </div>
  </section>
  <section class="panel">
    <div class="panel-title">
      <h1>干员数据详表</h1>
      <div class="columns"><span>技能等级</span>{''.join(f'<span>{label}</span>' for label in level_labels)}</div>
    </div>
    <div class="section-title">技能效果与倍率</div>
    <div class="skill-grid">{skill_cards(view.skills, skill_icons, term_styles, term_icons)}</div>
    <div class="section-title">天赋效果</div>
    <div class="effect-grid">{effect_cards(view.talents, "T", talent_icons, term_styles, term_icons)}</div>
    <div class="footer-line"><span>数据版本 {esc(view.source_version or "--")}</span></div>
  </section>
</div>
<script>
(function() {{
  const card = document.querySelector('.endfield-card');
  const rail = document.querySelector('.rail');
  const panel = document.querySelector('.panel');
  if (!card || !rail || !panel) return;
  const cardHeight = Math.max(
    {CARD_MIN_HEIGHT},
    {OPERATOR_RAIL_HEIGHT + 56},
    Math.ceil(panel.scrollHeight) + 56
  );
  card.style.height = cardHeight + 'px';
  document.documentElement.style.height = cardHeight + 'px';
  document.body.style.height = cardHeight + 'px';
}})();
</script>
</body>
</html>"""


def _render_weapon_html(view: WeaponView, weapon_img: str, rich_icons: dict[str, str], card_width: int | None = None) -> str:
    card_width = card_width or weapon_card_width(view)
    card_min_height = 720
    rail_width = 400 if card_width == 1360 else 420 if card_width == 1440 else 430 if card_width == 1520 else 440
    panel_left = rail_width + 58
    panel_width = card_width - panel_left - 24
    max_atk = view.max_atk if view.max_atk not in (None, "") else "--"
    operator_names = weapon_operator_names(view.operator_names)
    css = f"""
* {{ box-sizing:border-box; }}
html, body {{ margin:0; width:{card_width}px; min-height:{card_min_height}px; background:#d9dde0; font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",Arial,sans-serif; color:#15191d; }}
.weapon-card {{ position:relative; width:{card_width}px; min-height:{card_min_height}px; overflow:visible; background:linear-gradient(90deg,rgba(29,34,39,.075) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(0deg,rgba(29,34,39,.075) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(135deg,#f7f8f4 0%,#e8ebed 58%,#cfd5d9 100%); }}
.rail {{ position:absolute; z-index:3; left:28px; top:28px; bottom:28px; width:{rail_width}px; padding:20px 20px; overflow:visible; border:1px solid rgba(29,34,39,.30); background:rgba(247,248,246,.93); }}
.kicker {{ font-size:16px; font-weight:950; color:#687177; }} .name {{ margin-top:10px; font-size:56px; line-height:.96; font-weight:950; letter-spacing:-.04em; }} .eng {{ margin-top:6px; font-size:17px; color:#586168; font-weight:850; }}
.stars {{ display:flex; gap:5px; margin-top:14px; }} .rarity-star {{ width:31px; height:31px; object-fit:contain; filter:drop-shadow(0 2px 2px rgba(23,27,31,.25)); }}
.meta-grid {{ display:grid; grid-template-columns:minmax(86px,.9fr) minmax(132px,1.5fr) minmax(88px,1fr); gap:8px; margin-top:18px; width:100%; }} .info-box {{ min-height:56px; border-left:5px solid #171b1f; background:rgba(255,255,255,.58); padding:7px 9px; overflow:visible; }} .info-label {{ font-size:12px; color:#697279; font-weight:950; }} .info-value {{ margin-top:3px; font-size:19px; line-height:1.1; font-weight:950; white-space:normal; overflow-wrap:anywhere; }}
.visual {{ position:absolute; z-index:4; left:54px; top:318px; bottom:55px; width:{rail_width - 50}px; height:auto; pointer-events:none; }} .visual-frame {{ position:absolute; left:0; top:0; width:100%; height:100%; border:0; background:linear-gradient(180deg,rgba(255,255,255,.16),rgba(0,0,0,.015)); clip-path:polygon(0 0,93% 0,100% 7%,100% 100%,7% 100%,0 93%); }} .weapon-orbit {{ position:absolute; left:6%; top:9%; width:88%; height:76%; border:0; transform:skewY(-5deg); background:radial-gradient(circle at 52% 48%,rgba(255,255,255,.78),rgba(255,255,255,0) 58%); }}
.weapon-img {{ position:absolute; left:0; top:0; width:100%; height:100%; object-fit:contain; object-position:center center; filter:drop-shadow(0 32px 20px rgba(23,27,31,.30)); transform:rotate(-7deg); }}
.weapon-img-fallback {{ position:absolute; left:0; right:0; top:42%; text-align:center; color:#9aa2a8; font-size:20px; font-weight:900; }}
.panel {{ position:absolute; z-index:3; left:{panel_left}px; top:28px; width:{panel_width}px; height:auto; display:flex; flex-direction:column; overflow:visible; background:rgba(247,248,246,.95); border:1px solid rgba(23,27,31,.32); padding:14px 18px 14px; box-shadow:-12px 18px 44px rgba(23,27,31,.14); }} .panel-title {{ display:block; border-bottom:4px solid #171b1f; padding-bottom:10px; margin-bottom:10px; }} .panel-title h1 {{ margin:0; font-size:32px; line-height:1; font-weight:950; }}
.skill-stack {{ min-height:0; display:grid; grid-template-columns:minmax(300px,.76fr) minmax(470px,1.24fr); grid-template-rows:auto auto; gap:10px; align-items:start; }} .skill-card {{ --row-font:13px; --label-font:12px; --row-line:1.20; position:relative; display:flex; flex-direction:column; align-self:start; padding:12px 16px 10px 18px; border:1px solid rgba(23,27,31,.18); background:#f4f5f7; overflow:visible; }} .skill-card.s1 {{ grid-column:1; grid-row:1; }} .skill-card.s2 {{ grid-column:1; grid-row:2; }} .skill-card.s3 {{ grid-column:2; grid-row:1 / 3; }} .skill-head {{ display:block; flex:0 0 auto; padding-bottom:5px; }} .skill-name {{ font-size:23px; line-height:1.08; font-weight:950; white-space:normal; overflow-wrap:anywhere; }} .skill-card.s1 .skill-name,.skill-card.s2 .skill-name {{ font-size:24px; }} strong {{ color:#f2b900; background:transparent; padding:0 1px; }} .term {{ color:#8f5928; font-weight:950; border-bottom:1px dotted rgba(143,89,40,.55); }} .term-icon {{ display:inline-block; object-fit:contain; margin:0 1px 0 2px; vertical-align:-2px; filter:none; }} .rich-style,.vup,.info-note {{ display:inline; }} .info-note {{ color:#59636a; font-weight:900; }}
.frontend-level-list {{ clear:both; display:grid; grid-template-rows:repeat(9,auto); flex:0 0 auto; min-height:0; border-top:1px solid rgba(23,27,31,.10); }} .level-row {{ display:grid; grid-template-columns:45px minmax(0,1fr); gap:10px; align-items:center; min-height:30px; padding:3px 0; overflow:visible; border-bottom:1px solid rgba(23,27,31,.08); }} .level-row:last-child {{ border-bottom:0; }} .level-label {{ padding:0; color:#536071; font-size:var(--label-font); line-height:1; font-weight:900; align-self:center; }} .level-desc {{ padding:0; align-self:center; color:#17202a; font-size:max(12px,var(--row-font)); line-height:max(1.20,var(--row-line)); font-weight:850; overflow-wrap:anywhere; }} .frontend-level-list.short .level-row {{ min-height:30px; }} .skill-card.s1 .level-label,.skill-card.s2 .level-label {{ font-size:var(--label-font); }} .skill-card.s1 .level-desc,.skill-card.s2 .level-desc {{ font-size:max(12px,var(--row-font)); line-height:max(1.20,var(--row-line)); }} .frontend-level-list.long .level-row {{ grid-template-columns:42px minmax(0,1fr); min-height:52px; }} .frontend-level-list.long .level-label {{ font-size:var(--label-font); }} .frontend-level-list.long .level-desc {{ font-size:max(12px,var(--row-font)); line-height:max(1.20,var(--row-line)); letter-spacing:-.012em; }}
.footer-line {{ margin-top:8px; padding-top:0; display:flex; justify-content:space-between; align-items:end; color:#697279; font-size:12px; font-weight:900; }}
"""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>{css}</style></head><body>
<div class="weapon-card">
  <section class="rail">
    <div class="kicker">ARKNIGHTS: ENDFIELD · WEAPON</div>
    <div class="name">{esc(view.name)}</div>
    <div class="eng">{esc(view.english_name or view.slug)}</div>
    <div class="stars">{stars(view.rarity)}</div>
    <div class="meta-grid">
      {info_box("类型", view.weapon_type)}
      {info_box("所属干员", operator_names)}
      {info_box("满级攻击", str(max_atk))}
    </div>
  </section>
  <section class="visual">
    <div class="visual-frame"></div><div class="weapon-orbit"></div>
    {image(weapon_img, view.name).replace("<img ", '<img class="weapon-img" ', 1) or f'<div class="weapon-img-fallback">{esc(view.name)}</div>'}
  </section>
  <section class="panel">
    <div class="panel-title"><h1>武器数据详表</h1></div>
    <div class="skill-stack">{weapon_skill_cards(view.skills, view, rich_icons)}</div>
    <div class="footer-line"><span>数据来源 {esc(view.source_name)} · {esc(view.title)}</span><span>更新 {esc(view.source_version or "--")}</span></div>
  </section>
</div>
<script>
(function() {{
  const card = document.querySelector('.weapon-card');
  const panel = document.querySelector('.panel');
  const stack = document.querySelector('.skill-stack');
  const s1 = document.querySelector('.skill-card.s1');
  const s2 = document.querySelector('.skill-card.s2');
  const s3 = document.querySelector('.skill-card.s3');
  if (!card || !panel || !stack || !s1 || !s2 || !s3) return;
  const cards = [s1, s2, s3];
  cards.forEach(c => {{ c.style.height = ''; c.querySelector('.frontend-level-list').style.height = ''; }});
  stack.style.height = ''; panel.style.height = '';
  const panelHeight = Math.ceil(panel.scrollHeight);
  panel.style.height = panelHeight + 'px';
  const rail = document.querySelector('.rail');
  const railNaturalHeight = rail ? Math.ceil(rail.scrollHeight) + 56 : 0;
  const cardHeight = Math.max({card_min_height}, panelHeight + 56, railNaturalHeight);
  card.style.height = cardHeight + 'px';
  document.documentElement.style.height = cardHeight + 'px';
  document.body.style.height = cardHeight + 'px';
}})();
</script>
</body></html>"""


def _write_temp_html(content: str) -> Path:
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as file:
        file.write(content)
        return Path(file.name)


def estimate_card_height(view: OperatorView) -> int:
    skill_cards_height = [_estimated_skill_card_height(skill) for skill in view.skills[:4]]
    if not skill_cards_height:
        skill_area = 120
    else:
        first_row = max(skill_cards_height[:2] or [0])
        second_row = max(skill_cards_height[2:4] or [0])
        skill_area = first_row + second_row + (8 if second_row else 0)
    talent_cards_height = [_estimated_effect_card_height(effect) for effect in view.talents[:4]]
    if not talent_cards_height:
        talent_area = 52
    else:
        first_row = max(talent_cards_height[:2] or [0])
        second_row = max(talent_cards_height[2:4] or [0])
        talent_area = first_row + second_row + (7 if second_row else 0)
    footer_allowance = 56
    panel_height = 15 + 43 + 9 + 22 + 6 + skill_area + 9 + 22 + 6 + talent_area + 12 + 16 + 14 + footer_allowance
    return min(CARD_MAX_HEIGHT, max(CARD_MIN_HEIGHT, int(max(panel_height, OPERATOR_RAIL_HEIGHT) + 56)))


def _estimated_skill_card_height(skill: SkillView) -> int:
    row_count = len(skill_metric_rows(skill)) or 1
    desc_lines = _estimated_lines(skill.description, chars_per_line=29)
    meta_height = 28 if skill.category == "终结技" else 0
    return 7 + 42 + 7 + desc_lines * 17 + meta_height + 6 + row_count * 25 + 10


def _estimated_effect_card_height(effect: EffectView) -> int:
    desc_lines = _estimated_lines(effect.description, chars_per_line=37)
    return 14 + 20 + desc_lines * 17


def _estimated_potential_card_height(effect: EffectView) -> int:
    title_lines = _estimated_lines(effect.title, chars_per_line=17)
    desc_lines = _estimated_lines(effect.description, chars_per_line=23)
    return max(68, 14 + title_lines * 18 + desc_lines * 17)


def _estimated_lines(text: str, *, chars_per_line: int) -> int:
    if not text:
        return 1
    return max(1, (len(text) + chars_per_line - 1) // chars_per_line)


@lru_cache(maxsize=16)
def _local_image_data_url(path: Path) -> str:
    if not path.exists():
        return ""
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


async def _image_data_url(url: str) -> str:
    return (await _image_data_urls([url])).get(url, "")


async def _image_data_urls(urls: Iterable[str]) -> dict[str, str]:
    return (await _prepare_assets(urls, inline=True)).urls


async def _prepare_assets(urls: Iterable[str], *, inline: bool) -> _PreparedAssets:
    unique_urls = tuple(dict.fromkeys(str(url) for url in urls if url))
    direct = {url: url for url in unique_urls if url.startswith("data:")}
    remote_urls = [url for url in unique_urls if not url.startswith("data:")]
    resources = await fetch_many(
        remote_urls,
        namespace=REMOTE_ASSET_NAMESPACE,
        timeout_seconds=10.0,
    )
    output = dict(direct)
    browser_resources: dict[str, BrowserResource] = {}
    contents: dict[str, bytes] = {}
    for url, resource in resources.items():
        if resource is None:
            output[url] = ""
            continue
        mime = resource.content_type
        if not mime or mime == "application/octet-stream":
            mime = mimetypes.guess_type(url)[0] or "image/webp"
        contents[url] = resource.content
        if inline:
            output[url] = f"data:{mime};base64,{base64.b64encode(resource.content).decode('ascii')}"
            continue
        digest = hashlib.sha256(resource.content).hexdigest()
        browser_url = f"https://endfield.local/assets/{digest}"
        output[url] = browser_url
        browser_resources.setdefault(browser_url, BrowserResource(resource.content, mime))
    return _PreparedAssets(output, browser_resources, contents)


async def _portrait_layout(view: OperatorView, content: bytes) -> PortraitLayout:
    override = _PORTRAIT_LAYOUT_OVERRIDES.get(view.name)
    if override:
        return PortraitLayout(*override)
    if not content:
        return PortraitLayout()
    digest = hashlib.sha256(content).hexdigest()
    cached = _PORTRAIT_LAYOUT_CACHE.get(digest)
    if cached is not None:
        _PORTRAIT_LAYOUT_CACHE.move_to_end(digest)
        return cached
    layout = await run_image_render(_analyze_portrait_layout, content)
    _PORTRAIT_LAYOUT_CACHE[digest] = layout
    _PORTRAIT_LAYOUT_CACHE.move_to_end(digest)
    while len(_PORTRAIT_LAYOUT_CACHE) > 64:
        _PORTRAIT_LAYOUT_CACHE.popitem(last=False)
    return layout


def _analyze_portrait_layout(content: bytes) -> PortraitLayout:
    try:
        image = Image.open(BytesIO(content)).convert("RGBA")
    except Exception:
        return PortraitLayout()
    rgba = np.asarray(image)
    height, width = rgba.shape[:2]
    if not width or not height:
        return PortraitLayout()

    alpha = rgba[:, :, 3]
    active = alpha > 12
    if active.any():
        rows, columns = np.where(active)
        x = float((columns.min() + columns.max()) / 2 / width * 100)
        y = float((rows.min() + rows.max()) / 2 / height * 100)
        coverage = max((columns.max() - columns.min() + 1) / width, (rows.max() - rows.min() + 1) / height)
        scale = 1.18 if coverage < 0.68 else 1.12 if coverage < 0.86 else 1.07
    else:
        x, y, scale = 50.0, 45.0, 1.10

    rgb = cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY)
    face_center = _largest_face_center(gray)
    if face_center is not None:
        face_x, face_y = face_center
        x = x * 0.35 + face_x / width * 100 * 0.65
        y = y * 0.25 + face_y / height * 100 * 0.75
    else:
        saliency = _portrait_saliency_center(rgb, active)
        if saliency is not None:
            salient_x, salient_y = saliency
            x = x * 0.55 + salient_x / width * 100 * 0.45
            y = y * 0.55 + salient_y / height * 100 * 0.45

    return PortraitLayout(
        x=max(35.0, min(65.0, x)),
        y=max(30.0, min(58.0, y)),
        scale=max(1.05, min(1.18, scale)),
    )


def _largest_face_center(gray: np.ndarray) -> tuple[float, float] | None:
    try:
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(24, 24))
    except Exception:
        return None
    if len(faces) == 0:
        return None
    face_x, face_y, face_width, face_height = max(faces, key=lambda item: int(item[2]) * int(item[3]))
    return face_x + face_width / 2, face_y + face_height / 2


def _portrait_saliency_center(rgb: np.ndarray, active: np.ndarray) -> tuple[float, float] | None:
    hsv = cv2.cvtColor(rgb, cv2.COLOR_BGR2HSV)
    saturation = hsv[:, :, 1].astype(np.float32) / 255.0
    edges = cv2.Canny(cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY), 60, 150).astype(np.float32) / 255.0
    weights = saturation * 0.45 + cv2.GaussianBlur(edges, (0, 0), 3) * 0.55
    if active.any():
        weights *= active.astype(np.float32)
    total = float(weights.sum())
    if total <= 1e-6:
        return None
    rows, columns = np.indices(weights.shape)
    return float((columns * weights).sum() / total), float((rows * weights).sum() / total)


def weapon_card_width(view: WeaponView) -> int:
    lengths = [
        len(_weapon_level_plain_text(skill.description, level.values))
        for skill in view.skills[:3]
        for level in skill.levels[:9]
    ]
    maximum = max(lengths or [0])
    total = sum(lengths)
    skill_count = len(view.skills[:3])
    if skill_count <= 2 or (maximum <= 40 and total <= 480):
        return 1360
    if maximum <= 75 and total <= 850:
        return 1440
    if maximum <= 105 and total <= 1100:
        return 1520
    return 1600


def _weapon_level_plain_text(template: str, values: dict[str, object]) -> str:
    def repl(match: re.Match[str]) -> str:
        fmt = match.group(2)[1:] if match.group(2) else None
        return format_weapon_value(values.get(match.group(1)), fmt)

    text = re.sub(r"\{([^{}:]+)(:[^{}]+)?\}", repl, template or "")
    text = re.sub(r"<[^>]+>", "", text)
    return " ".join(text.replace("\\n", "\n").split())


def optimize_png_container(content: bytes) -> bytes:
    started = perf_counter()
    if not content.startswith(_PNG_SIGNATURE):
        return content
    chunks: list[tuple[bytes, bytes]] = []
    position = len(_PNG_SIGNATURE)
    try:
        while position + 12 <= len(content):
            length = struct.unpack(">I", content[position:position + 4])[0]
            chunk_type = content[position + 4:position + 8]
            payload_end = position + 8 + length
            crc_end = payload_end + 4
            if crc_end > len(content):
                return content
            chunks.append((chunk_type, content[position + 8:payload_end]))
            position = crc_end
            if chunk_type == b"IEND":
                break
    except (ValueError, struct.error):
        return content

    idat_payload = b"".join(payload for chunk_type, payload in chunks if chunk_type == b"IDAT")
    if not idat_payload:
        return content
    output = bytearray(_PNG_SIGNATURE)
    emitted_idat = False
    for chunk_type, payload in chunks:
        if chunk_type in _PNG_DROPPED_CHUNKS:
            continue
        if chunk_type == b"IDAT":
            if emitted_idat:
                continue
            payload = idat_payload
            emitted_idat = True
        output.extend(struct.pack(">I", len(payload)))
        output.extend(chunk_type)
        output.extend(payload)
        output.extend(struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF))
    optimized = bytes(output)
    if perf_counter() - started > 0.05 or len(optimized) >= len(content):
        return content
    return optimized


def skill_cards(skills: Iterable[SkillView], icons: dict[str, str], term_styles: dict[str, TermStyleView], term_icons: dict[str, str]) -> str:
    cards = [skill_card(skill, index + 1, icons.get(skill.icon_id, ""), term_styles, term_icons) for index, skill in enumerate(skills)]
    if not cards:
        return '<div class="skill-card"><div class="skill-name">暂无技能数据</div></div>'
    return "".join(cards[:4])


def weapon_skill_cards(skills: Iterable[WeaponSkillView], view: WeaponView, rich_icons: dict[str, str]) -> str:
    cards = []
    for index, skill in enumerate(list(skills)[:3], 1):
        cards.append(
            f"""<article class="skill-card s{index}">
  <div class="skill-head"><div class="skill-name">{esc(skill.title)}</div></div>
  {weapon_skill_level_rows(skill, index, view, rich_icons)}
</article>"""
        )
    while len(cards) < 3:
        index = len(cards) + 1
        cards.append(
            f"""<article class="skill-card s{index}">
  <div class="skill-head"><div class="skill-name">暂无数据</div></div>
  <div class="frontend-level-list {'long' if index == 3 else 'short'}"></div>
</article>"""
        )
    return "".join(cards)


def weapon_skill_level_rows(skill: WeaponSkillView, index: int, view: WeaponView, rich_icons: dict[str, str]) -> str:
    rows = []
    for level in skill.levels[:9]:
        desc = render_weapon_level_text(skill.description, level.values, view, rich_icons)
        rows.append(
            f"""<div class="level-row">
  <div class="level-label">Lv{esc(level.level)}</div>
  <div class="level-desc">{desc}</div>
</div>"""
        )
    return f'<div class="frontend-level-list {"long" if index == 3 else "short"}">{"".join(rows)}</div>'


def render_weapon_level_text(template: str, values: dict[str, object], view: WeaponView, rich_icons: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        fmt = match.group(2)[1:] if match.group(2) else None
        return format_weapon_value(values.get(name), fmt)

    text = re.sub(r"\{([^{}:]+)(:[^{}]+)?\}", repl, template or "")
    return render_weapon_rich_text(text, view, rich_icons)


def _weapon_rich_icon_urls_used(view: WeaponView) -> list[str]:
    used_tag_ids: set[str] = set()
    for skill in view.skills:
        used_tag_ids.update(re.findall(r"<#([a-z0-9_.]+)>", skill.description or ""))
    urls: list[str] = []
    seen: set[str] = set()
    for tag_id in used_tag_ids:
        entry = view.rich_text_links.get(tag_id) or {}
        icon_url = str(entry.get("iconPath") or "")
        if icon_url and icon_url not in seen:
            seen.add(icon_url)
            urls.append(icon_url)
    return urls


def format_weapon_value(value: object, fmt: str | None = None) -> str:
    if value is None:
        return "--"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if fmt == "0.0%":
        return f"{number * 100:.1f}%"
    if fmt == "0%":
        return f"{number * 100:.0f}%"
    if fmt == "0":
        return f"{number:.0f}"
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def render_weapon_rich_text(text: str, view: WeaponView, rich_icons: dict[str, str]) -> str:
    rendered = esc(text or "")
    rendered = re.sub(
        r"&lt;@([a-z0-9_.]+)&gt;(.*?)&lt;/&gt;",
        lambda match: _render_weapon_style_tag(match, view),
        rendered,
    )
    rendered = re.sub(
        r"&lt;#([a-z0-9_.]+)&gt;(.*?)&lt;/&gt;",
        lambda match: _render_weapon_link_tag(match, view, rich_icons),
        rendered,
    )
    rendered = re.sub(r"\{([^{}:]+)(:[^{}]+)?\}", lambda match: f"<strong>{esc(match.group(1))}</strong>", rendered)
    return rendered.replace("\n", "<br>")


def _render_weapon_style_tag(match: re.Match[str], view: WeaponView) -> str:
    tag_id = match.group(1)
    inner = match.group(2)
    style = view.rich_text_styles.get(tag_id) or {}
    color = normalize_rich_color(style.get("color"))
    css = f' style="color:{esc_attr(color)}"' if color else ""
    klass = "info-note" if tag_id == "ba.info" else "vup" if tag_id == "ba.vup" else "rich-style"
    return f'<span class="{klass}"{css}>{inner}</span>'


def _render_weapon_link_tag(match: re.Match[str], view: WeaponView, rich_icons: dict[str, str]) -> str:
    tag_id = match.group(1)
    inner = match.group(2)
    link = view.rich_text_links.get(tag_id) or {}
    style = view.rich_text_styles.get(str(link.get("richTextId") or "")) or {}
    color = normalize_rich_color(style.get("color"))
    icon = weapon_term_icon(str(link.get("iconPath") or ""), rich_icons)
    css = f' style="color:{esc_attr(color)}"' if color else ""
    return f'<span class="term"{css}>{icon}{inner}</span>'


def weapon_term_icon(url: str, rich_icons: dict[str, str]) -> str:
    src = rich_icons.get(url, "")
    if not src:
        return ""
    return f'<img class="term-icon" src="{esc_attr(src)}" style="width:11px;height:11px" alt="">'


def skill_card(skill: SkillView, index: int, icon_url: str, term_styles: dict[str, TermStyleView], term_icons: dict[str, str]) -> str:
    return f"""
<article class="skill-card">
  <div class="skill-head">
    <div class="round-icon">{image(icon_url, skill.title) or esc(index)}</div>
    <div><div class="skill-name">{esc(skill.title)}</div><div class="skill-cat">{esc(skill.category)}</div></div>
  </div>
  <div class="skill-desc">{highlight_terms(skill.description, term_styles, term_icons)}</div>
  {skill_meta(skill)}
  {metric_table(skill)}
</article>"""


def metric_table(skill: SkillView) -> str:
    metric_rows = skill_metric_rows(skill)
    label_width = metric_label_width(skill)
    cells: list[str] = []
    for name, values in metric_rows:
        strong_index = _strong_value_index(skill, name, values)
        metric_class = "metric-name long" if len(name) > 10 else "metric-name"
        cells.append(f'<div class="{metric_class}">{esc(skill_metric_label(skill, name))}</div>')
        for index, value in enumerate(values):
            strong = " strong" if index == strong_index and value != "--" else ""
            cells.append(f'<div class="metric-value{strong}">{esc(value or "--")}</div>')
    if not cells:
        cells = ['<div class="metric-name">效果</div>'] + ['<div class="metric-value">--</div>' for _ in skill_level_labels(skill)]
    return f'<div class="metric-table" style="--metric-label-width:{label_width}px">' + "".join(cells) + "</div>"


def metric_label_width(skill: SkillView) -> int:
    labels = [skill_metric_label(skill, name) for name, _ in skill_metric_rows(skill)] or ["效果"]
    display_width = max(_text_display_width(label) for label in labels)
    if display_width <= 8:
        return 92
    if display_width <= 12:
        return 108
    return 124


def _text_display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F", "A"} else 1 for char in str(text or ""))


def potential_items(effects: Iterable[EffectView], term_styles: dict[str, TermStyleView], term_icons: dict[str, str], keyword_terms: set[str]) -> str:
    cards = []
    for effect in effects:
        level_match = re.search(r"^P(\d+)", effect.title)
        badge = level_match.group(1) if level_match else "?"
        potential_icon = potential_star(badge)
        cards.append(
            f"""<article class="potential-item">
  <div class="potential-icon">
    {potential_icon}
  </div>
  <div><div class="potential-title">{esc(effect.title)}</div><div class="potential-desc">{highlight_terms(effect.description, term_styles, term_icons, keyword_terms)}</div></div>
</article>"""
        )
    return "".join(cards) if cards else '<div class="potential-item"><div class="potential-icon">--</div><div class="potential-title">暂无数据</div></div>'


def effect_cards(effects: Iterable[EffectView], fallback: str, icons: dict[str, str], term_styles: dict[str, TermStyleView], term_icons: dict[str, str]) -> str:
    cards = [effect_card(effect, fallback, icons.get(effect.effect_id, ""), term_styles, term_icons) for effect in effects]
    if not cards:
        return '<div class="effect-card"><div class="round-icon">--</div><div><div class="effect-title">暂无数据</div></div></div>'
    return "".join(cards[:4])


def effect_card(effect: EffectView, fallback: str, icon_url: str, term_styles: dict[str, TermStyleView], term_icons: dict[str, str]) -> str:
    return f"""
<article class="effect-card">
  <div class="round-icon">{image(icon_url or effect.icon_url, effect.title) or esc(fallback)}</div>
  <div>
    <div class="effect-title">{esc(effect.title)}</div>
    <div class="effect-desc">{highlight_terms(effect.description, term_styles, term_icons)}</div>
  </div>
</article>"""


def skill_metric_rows(skill: SkillView) -> list[tuple[str, list[str]]]:
    if skill.category == "普攻":
        if not skill.extra_levels:
            rows = _rows_for_metric_names(skill, _skill_metric_names(skill))
            return _prefer_specific_metric_rows(rows)
        level_count = len(skill.levels) or len(LEVEL_COLUMNS)
        rows = {
            "普攻倍率": ["--"] * level_count,
            "处决攻击倍率": ["--"] * level_count,
            "下落攻击倍率": ["--"] * level_count,
        }
        for levels in skill.extra_levels.values():
            for name in rows:
                values = [level.values.get(name, "--") for level in levels]
                if any(value != "--" for value in values):
                    rows[name] = values
        if all(value == "--" for values in rows.values() for value in values):
            rows["普攻倍率"] = [level.values.get("普攻倍率", "--") for level in skill.levels]
        return list(rows.items())
    rows = []
    for name in _skill_metric_names(skill):
        if skill.category == "终结技" and _is_top_ultimate_metric(name):
            continue
        rows.append((name, [level.values.get(name, "--") for level in skill.levels]))
    metric_names = [name for name, _ in rows]
    if skill.category == "连携技" and "冷却" not in metric_names:
        rows.append(("冷却", [level.cooldown or "--" for level in skill.levels]))
    if skill.category == "连携技":
        rows = _prioritize_combo_metric_rows(rows)
    return _prefer_specific_metric_rows(rows)


def _rows_for_metric_names(skill: SkillView, names: list[str]) -> list[tuple[str, list[str]]]:
    return [(name, [level.values.get(name, "--") for level in skill.levels]) for name in names]


def _prefer_specific_metric_rows(rows: list[tuple[str, list[str]]]) -> list[tuple[str, list[str]]]:
    rows = _non_empty_metric_rows(rows)
    specific_names = [name for name, _ in rows if not _is_generic_metric(name)]
    if not specific_names:
        return rows
    return [(name, values) for name, values in rows if not _generic_metric_shadowed(name, specific_names)]


def _non_empty_metric_rows(rows: list[tuple[str, list[str]]]) -> list[tuple[str, list[str]]]:
    return [(name, values) for name, values in rows if any(value != "--" for value in values)]


def _prioritize_combo_metric_rows(rows: list[tuple[str, list[str]]]) -> list[tuple[str, list[str]]]:
    cooldown = [row for row in rows if row[0] == "冷却"]
    rest = [row for row in rows if row[0] != "冷却"]
    return cooldown + rest


def _is_top_ultimate_metric(name: str) -> bool:
    return name in {"所需能量", "所需终结技能量", "冷却"}


def _is_generic_metric(name: str) -> bool:
    return name in {"攻击倍率", "失衡值", "持续时间", "技力"}


def _generic_metric_shadowed(name: str, specific_names: list[str]) -> bool:
    if name == "攻击倍率":
        return any("倍率" in specific for specific in specific_names)
    if name == "失衡值":
        return any("失衡值" in specific for specific in specific_names)
    if name == "持续时间":
        return any(("持续时间" in specific or "时长" in specific) for specific in specific_names)
    if name == "技力":
        return any(("技力" in specific or "终结技能量" in specific) for specific in specific_names)
    return False


def _skill_metric_names(skill: SkillView) -> list[str]:
    names: list[str] = []
    for level in skill.levels:
        for name in level.values:
            if name not in names:
                names.append(name)
    return names


def skill_metric_label(skill: SkillView, name: str) -> str:
    if skill.category == "普攻":
        if name == "普攻倍率":
            return "最后一段倍率"
        if name == "处决攻击倍率":
            return "处决攻击倍率"
        if name == "下落攻击倍率":
            return "下落攻击倍率"
    return name


def highlight_terms(
    text: str,
    term_styles: dict[str, TermStyleView],
    term_icons: dict[str, str],
    keyword_terms: set[str] | None = None,
) -> str:
    if not text:
        return ""
    terms = sorted(
        {term for term in (set(term_styles) | set(keyword_terms or set())) if term},
        key=len,
        reverse=True,
    )
    rendered: list[str] = []
    index = 0
    while index < len(text):
        rich_tag = _operator_rich_tag_at(text, index)
        if rich_tag:
            kind, tag_id, inner, end_index = rich_tag
            inner_html = highlight_terms(inner, {}, {}, set())
            rendered.append(_render_operator_rich_tag(kind, tag_id, inner_html, term_styles, term_icons))
            index = end_index
            continue
        term = _matched_term_at(text, index, terms)
        if term:
            if term in PLAIN_TEXT_TERMS:
                rendered.append(esc(term))
                index += len(term)
                continue
            if term in TEXT_ONLY_TERMS:
                rendered.append(f'<span class="term-plain">{esc(term)}</span>')
                index += len(term)
                continue
            style = term_styles.get(term) or TermStyleView(term, "#ffd000", "")
            icon = term_icons.get(term, "")
            normalized = normalize_rich_color(style.color)
            color_attr = f' style="--term-color: {esc_attr(normalized)}"' if normalized else ""
            rendered.append(
                f'<span class="term"{color_attr}>{term_image(icon, term) if icon else ""}{esc(term)}</span>'
            )
            index += len(term)
            continue
        number = re.match(r"[+-]?\d+(?:\.\d+)?%?|--", text[index:])
        if number:
            rendered.append(f"<strong>{esc(number.group(0))}</strong>")
            index += len(number.group(0))
            continue
        rendered.append(esc(text[index]))
        index += 1
    return "".join(rendered)


def _operator_rich_tag_at(text: str, index: int) -> tuple[str, str, str, int] | None:
    match = re.match(r"<([@#])([A-Za-z0-9_.-]+)>", text[index:])
    if not match:
        return None
    inner_start = index + len(match.group(0))
    close_index = text.find("</>", inner_start)
    if close_index < 0:
        return None
    return match.group(1), match.group(2), text[inner_start:close_index], close_index + 3


def _render_operator_rich_tag(
    kind: str,
    tag_id: str,
    inner_html: str,
    term_styles: dict[str, TermStyleView],
    term_icons: dict[str, str],
) -> str:
    style = term_styles.get(tag_id) or TermStyleView(tag_id, "", "")
    if kind == "#":
        normalized = normalize_rich_color(style.color)
        color_attr = f' style="--term-color: {esc_attr(normalized)}"' if normalized else ""
        icon = term_icons.get(tag_id, "")
        return f'<span class="term"{color_attr}>{term_image(icon, tag_id) if icon else ""}{inner_html}</span>'
    normalized = normalize_rich_color(style.color)
    color_attr = f' style="color: {esc_attr(normalized)}"' if normalized else ""
    klass = "info-note" if tag_id == "ba.info" else "vup" if tag_id == "ba.vup" else "rich-style"
    return f'<span class="{klass}"{color_attr}>{inner_html}</span>'


def _matched_term_at(text: str, index: int, terms: list[str]) -> str:
    for term in terms:
        if text.startswith(term, index) and _term_boundary_ok(text, index, term):
            return term
    return ""


def _term_boundary_ok(text: str, index: int, term: str) -> bool:
    if term in TERM_BOUNDARY_EXCEPTIONS:
        return True
    before = text[index - 1] if index > 0 else ""
    after_index = index + len(term)
    after = text[after_index] if after_index < len(text) else ""
    if before and re.match(r"[\w\u4e00-\u9fff]", before) and before == term[0]:
        return False
    if after and re.match(r"[\w\u4e00-\u9fff]", after) and after == term[-1]:
        return False
    return True


def _strong_value_index(skill: SkillView, name: str, values: list[str]) -> int:
    if not values:
        return 0
    if skill.category == "连携技" and name == "冷却":
        return min(range(len(values)), key=lambda i: _numeric_signal(values[i], missing=999999.0))
    return max(range(len(values)), key=lambda i: _numeric_signal(values[i]))


def _numeric_signal(value: str, missing: float = -1.0) -> float:
    match = re.search(r"-?\d+(?:\.\d+)?", value or "")
    return float(match.group(0)) if match else missing


def merged_term_styles(view: OperatorView) -> dict[str, TermStyleView]:
    result = dict(FALLBACK_TERM_STYLES)
    result.update(view.term_styles)
    for term in NO_ICON_TERMS:
        if term in result:
            style = result[term]
            result[term] = TermStyleView(term=style.term, color=style.color, icon_url="")
    if "法术脆弱" in result:
        style = result["法术脆弱"]
        result["法术脆弱"] = TermStyleView(term=style.term, color=style.color, icon_url="")
    return {
        term: TermStyleView(style.term, normalize_rich_color(style.color), style.icon_url)
        for term, style in result.items()
    }


def normalize_rich_color(color: object, *, minimum_contrast: float = 4.5) -> str:
    text = str(color or "").strip()
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", text)
    if not match:
        return text
    raw = match.group(1)
    red, green, blue = (int(raw[index:index + 2], 16) / 255.0 for index in (0, 2, 4))
    background = (247 / 255.0, 248 / 255.0, 246 / 255.0)
    if _contrast_ratio((red, green, blue), background) >= minimum_contrast:
        return f"#{raw.lower()}"
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    low, high = 0.0, lightness
    best = (red, green, blue)
    for _ in range(20):
        candidate_lightness = (low + high) / 2
        candidate = colorsys.hls_to_rgb(hue, candidate_lightness, saturation)
        if _contrast_ratio(candidate, background) >= minimum_contrast:
            best = candidate
            low = candidate_lightness
        else:
            high = candidate_lightness
    return "#" + "".join(f"{round(channel * 255):02x}" for channel in best)


def _contrast_ratio(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    first_luminance = _relative_luminance(first)
    second_luminance = _relative_luminance(second)
    lighter = max(first_luminance, second_luminance)
    darker = min(first_luminance, second_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def _relative_luminance(color: tuple[float, float, float]) -> float:
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(value) for value in color)
    return red * 0.2126 + green * 0.7152 + blue * 0.0722


def operator_keyword_terms(view: OperatorView) -> set[str]:
    return {
        item.title
        for item in [*view.skills, *view.talents]
        if item.title and len(item.title) >= 2
    }


def _operator_terms_used(view: OperatorView, term_styles: dict[str, TermStyleView]) -> set[str]:
    text = "\n".join(
        [
            *(skill.description for skill in view.skills),
            *(effect.description for effect in view.talents),
            *(effect.description for effect in view.potentials),
        ]
    )
    used = {term for term in term_styles if term and term in text}
    used.update(re.findall(r"<[@#]([A-Za-z0-9_.-]+)>", text))
    return used


def info_box(label: str, value: str) -> str:
    return f'<div class="info-box"><div class="info-label">{esc(label)}</div><div class="info-value">{esc(value)}</div></div>'


def weapon_operator_names(names: list[str]) -> str:
    unique_names = list(dict.fromkeys(name.strip() for name in names if name.strip()))
    if not unique_names:
        return "通用"
    if len(unique_names) <= 3:
        return "、".join(unique_names)
    return f"{'、'.join(unique_names[:2])}等{len(unique_names)}名"


def tags(values: list[str]) -> str:
    if not values:
        return ""
    return "".join(f'<span class="tag">{esc(value)}</span>' for value in values)


def tag_block(values: list[str]) -> str:
    content = tags(values)
    if not content:
        return ""
    return f'<div class="tag-list">{content}</div>'


def stars(rarity: int) -> str:
    count = max(1, min(6, int(rarity or 0)))
    url = _local_image_data_url(ASSET_DIR / "rarity-star.png")
    if not url:
        return "★" * count
    return "".join(f'<img class="rarity-star" src="{esc_attr(url)}" alt="star">' for _ in range(count))


def potential_star(level: str) -> str:
    try:
        number = max(1, min(5, int(level)))
    except (TypeError, ValueError):
        number = 1
    url = _local_image_data_url(ASSET_DIR / "potential" / f"wpn_potential_{number:02d}.png")
    if not url:
        return "--"
    return f'<img class="potential-star-img potential-star-p{number}" src="{esc_attr(url)}" alt="P{number}">'


def skill_meta(skill: SkillView) -> str:
    if skill.category != "终结技":
        return ""
    cost = "--"
    cooldown = "--"
    target = next((level for level in skill.levels if level.label == "Lv9"), None)
    target = target or (skill.levels[-1] if skill.levels else None)
    if target:
        cost = _display_value(target.cost, target.values.get("所需能量"), target.values.get("所需终结技能量"))
        cooldown = _display_value(target.cooldown, target.values.get("冷却"))
    return f'<div class="skill-meta"><span>所需能量 <strong>{esc(cost)}</strong></span><span>冷却 <strong>{esc(cooldown)}</strong></span></div>'


def _display_value(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text and text != "--":
            return text
    return "--"


def image(url: str, alt: str) -> str:
    if not url:
        return ""
    return f'<img src="{esc_attr(url)}" alt="{esc_attr(alt)}">'


def term_image(url: str, alt: str) -> str:
    if not url:
        return ""
    return f'<img class="term-icon" src="{esc_attr(url)}" alt="{esc_attr(alt)}">'


def skill_icon_url(icon_id: str) -> str:
    icon_id = str(icon_id or "").strip()
    if icon_id.startswith(("http://", "https://", "data:")):
        return icon_id
    return f"https://static.warfarin.wiki/v4/skillicon/{icon_id}.webp" if icon_id else ""


def operator_level_labels(view: OperatorView) -> list[str]:
    for skill in view.skills:
        labels = skill_level_labels(skill)
        if labels:
            return labels
    return [label for _, label in LEVEL_COLUMNS]


def skill_level_labels(skill: SkillView) -> list[str]:
    return [level.label for level in skill.levels[:4]] or [label for _, label in LEVEL_COLUMNS]


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def esc_attr(value: object) -> str:
    return html.escape(str(value or ""), quote=True)
