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
from dataclasses import dataclass, replace
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

from .models import (
    EffectView,
    EquipmentCatalogAttributeView,
    EquipmentCatalogGroupView,
    EquipmentCatalogItemView,
    EquipmentCatalogView,
    EquipmentView,
    AttendanceCardView,
    GachaHistoryView,
    LEVEL_COLUMNS,
    LoadoutView,
    OperatorCatalogItemView,
    OperatorCatalogView,
    OperatorView,
    SkillView,
    TermStyleView,
    WeaponSkillView,
    WeaponCatalogItemView,
    WeaponCatalogView,
    WeaponView,
)
from .gacha import (
    FreePullBatch,
    GachaAnalysis,
    PoolAnalysis,
    SixStarEvent,
    SixStarExpectation,
    calculate_six_star_expectation,
    format_timestamp,
)


OPERATOR_CARD_WIDTH = 1600
CARD_WIDTH = OPERATOR_CARD_WIDTH
CARD_MIN_HEIGHT = 780
CARD_MAX_HEIGHT = 6144
GACHA_PAGE_ROW_BUDGETS = (55, 45, 35)
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


async def draw_equipment_card(view: EquipmentView) -> bytes:
    started = perf_counter()
    prepared = await prepare_equipment_card_html(view)
    html_path = _write_temp_html(prepared.html)
    assets_seconds = perf_counter() - started
    try:
        screenshot_started = perf_counter()
        output = await screenshot_web_element(
            html_path.resolve().as_uri(),
            ".equipment-card",
            viewport=(prepared.width, 1),
            timeout_ms=15000,
            max_height=CARD_MAX_HEIGHT,
            device_scale_factor=2.0,
            settle_ms=50,
            resources=prepared.resources,
            wait_for_images=True,
            strict_max_height=True,
            overflow_selectors=(
                ".equipment-left",
                ".equipment-right",
                ".equipment-stat",
                ".equipment-piece",
            ),
        )
        optimize_started = perf_counter()
        optimized = await run_image_render(optimize_png_container, output)
        logger.info(
            f"[endfield] draw kind=equipment assets={assets_seconds:.3f}s "
            f"screenshot={optimize_started - screenshot_started:.3f}s "
            f"png_optimize={perf_counter() - optimize_started:.3f}s "
            f"bytes={len(output)}->{len(optimized)}"
        )
        return optimized
    finally:
        schedule_temp_file_cleanup(html_path, delay_seconds=30)


async def draw_attendance_card(view: AttendanceCardView) -> bytes:
    rows = []
    for role in view.roles:
        rewards = "、".join(f"{esc(item.name)} × {item.count}" for item in role.rewards) or "无奖励明细"
        rows.append(
            f"""
            <section class="attendance-row status-{esc(role.status)}">
              <div class="role-main"><strong>{esc(role.nickname)}</strong><span>{esc(role.server_name or '默认服务器')} · {esc(role.uid)}</span></div>
              <div class="status"><b>{esc(role.message)}</b><span>{rewards}</span></div>
            </section>
            """
        )
    return await _draw_neutral_card(
        "attendance-card",
        f"""
        <header><div><small>ENDFIELD / SKLAND</small><h1>签到结果</h1></div><time>{esc(view.generated_at)}</time></header>
        <main class="attendance-list">{''.join(rows) or '<div class="empty">没有可签到的角色</div>'}</main>
        """,
        extra_css="""
        .attendance-list{display:grid;gap:12px}
        .attendance-row{display:grid;grid-template-columns:minmax(260px,.8fr) minmax(420px,1.2fr);min-height:102px;border:1px solid #8d8d8d;border-left:8px solid #222;background:#fff}
        .attendance-row.status-failed{border-left-width:3px;background:#ededed}
        .role-main,.status{display:flex;flex-direction:column;justify-content:center;padding:18px 22px}
        .role-main{border-right:1px solid #b8b8b8}.role-main strong{font-size:28px}.role-main span,.status span{margin-top:7px;color:#666;font-size:16px}
        .status b{font-size:22px}.status span{line-height:1.45}
        """,
    )


async def draw_gacha_analysis_cards(view: GachaAnalysis, *, uid: str) -> tuple[bytes, ...]:
    try:
        return (await draw_gacha_analysis_card(view, uid=uid),)
    except RuntimeError as exc:
        if not _is_gacha_height_limit_error(exc):
            raise

    character_pools = _recent_gacha_pools(view, "角色")
    weapon_pools = _recent_gacha_pools(view, "武器")
    last_error: RuntimeError | None = None
    for row_budget in GACHA_PAGE_ROW_BUDGETS:
        character_pages = _paginate_gacha_pools(character_pools, row_budget)
        weapon_pages = _paginate_gacha_pools(weapon_pools, row_budget)
        page_count = max(len(character_pages), len(weapon_pages))
        pages: list[bytes] = []
        try:
            for page_index in range(page_count):
                pages.append(await draw_gacha_analysis_card(
                    view,
                    uid=uid,
                    character_pools=(
                        character_pages[page_index] if page_index < len(character_pages) else []
                    ),
                    weapon_pools=(
                        weapon_pages[page_index] if page_index < len(weapon_pages) else []
                    ),
                    page_number=page_index + 1,
                    page_count=page_count,
                    show_summary=page_index == 0,
                ))
        except RuntimeError as exc:
            if not _is_gacha_height_limit_error(exc):
                raise
            last_error = exc
            continue
        logger.info(
            f"[endfield] gacha analysis paginated pages={page_count} row_budget={row_budget}"
        )
        return tuple(pages)
    raise last_error or RuntimeError("抽卡分析分页失败")


async def draw_gacha_analysis_card(
    view: GachaAnalysis,
    *,
    uid: str,
    character_pools: list[PoolAnalysis] | None = None,
    weapon_pools: list[PoolAnalysis] | None = None,
    page_number: int = 1,
    page_count: int = 1,
    show_summary: bool = True,
) -> bytes:
    character_pools = (
        _recent_gacha_pools(view, "角色") if character_pools is None else character_pools
    )
    weapon_pools = _recent_gacha_pools(view, "武器") if weapon_pools is None else weapon_pools
    compact_layout = max(
        _gacha_column_render_rows(character_pools),
        _gacha_column_render_rows(weapon_pools),
    ) > 80
    character_total = sum(item.total for item in view.pools if item.item_type == "角色")
    weapon_total = sum(item.total for item in view.pools if item.item_type == "武器")
    character_paid = sum(_pool_paid_total(item) for item in view.pools if item.item_type == "角色")
    weapon_paid = sum(_pool_paid_total(item) for item in view.pools if item.item_type == "武器")
    character_free = sum(item.free_pull_count for item in view.pools if item.item_type == "角色")
    weapon_free = sum(item.free_pull_count for item in view.pools if item.item_type == "武器")
    free_pull_count = getattr(view, "free_pull_count", 0)
    paid_total = getattr(view, "paid_total", 0) or max(0, view.total - free_pull_count)
    xhh_imported_at = getattr(view, "xhh_imported_at", 0)
    recorded_total = getattr(view, "recorded_total", 0)
    if not xhh_imported_at and not recorded_total:
        recorded_total = view.total
    history_missing_count = getattr(view, "history_missing_count", 0)
    six_star_total = sum(count for rarity, count in view.rarity_counts.items() if rarity >= 6)
    total_detail = f"逐抽明细 {recorded_total}"
    if history_missing_count:
        total_detail += f" · 统计补齐 {history_missing_count}"
    else:
        total_detail = f"计保底 {paid_total} · 免费 {free_pull_count}"
    total_detail += f" · 六星记录 {six_star_total}"
    character_expectation = calculate_six_star_expectation(view.pools, "角色")
    weapon_expectation = calculate_six_star_expectation(view.pools, "武器")
    character_expectation_html = _draw_gacha_expectation_summary(character_expectation, "角色")
    weapon_expectation_html = _draw_gacha_expectation_summary(weapon_expectation, "武器")
    character_column = _draw_gacha_pool_column(
        character_pools, "角色池", character_paid,
        free_total=character_free, paginated=page_count > 1,
    )
    weapon_column = _draw_gacha_pool_column(
        weapon_pools, "武器池", weapon_paid,
        free_total=weapon_free, paginated=page_count > 1,
    )
    completeness = "统计已补齐" if xhh_imported_at and view.complete else ("同步正常" if view.complete else "部分数据")
    page_state = f"{completeness} · {page_number}/{page_count}" if page_count > 1 else completeness
    source_text = (
        f"小黑盒历史统计已补齐 · 导入 {format_timestamp(xhh_imported_at)} · 官方逐抽明细单独保留"
        if xhh_imported_at else "官方接口仅提供近 90 天记录 · 本地同步会持续累积保留"
    )
    compact_css = """
        .pool-stack{gap:7px;padding:7px}.column-head{padding:11px 14px}.pool-head{padding:8px 10px}
        .pity-item{padding:6px 8px}.pull-bars{gap:4px;padding:6px 8px 8px}
        .pull-row{grid-template-columns:38px 136px minmax(0,1fr);gap:6px}
        .gacha-thumb,.current-marker{width:36px;height:36px}.bar-track{height:32px}
        .bar-value{padding:0 8px}.bar-value b{font-size:14px}.pity-hit{padding:2px 5px;font-size:10px}.pity-hit-guarantee{padding:2px 5px;font-size:10px}.pity-hit-miss{padding:2px 7px;font-size:11px}
        .free-row{grid-template-columns:82px minmax(0,1fr) auto;min-height:40px;padding:3px 5px}
        .free-icons{min-height:36px}.free-marker{width:76px;height:32px}
    """ if compact_layout else ""
    summary_html = (
        f'<section class="summary"><div class="total"><span>卡池总数</span><strong>{view.total}</strong><small>{esc(total_detail)}</small></div><div class="metric"><div class="metric-head"><span>角色寻访</span><strong>{character_total}</strong></div><small>计保底 {character_paid}</small>{character_expectation_html}</div><div class="metric"><div class="metric-head"><span>武器申领</span><strong>{weapon_total}</strong></div><small>计保底 {weapon_paid}</small>{weapon_expectation_html}</div></section>'
        if show_summary else ""
    )
    return await _draw_neutral_card(
        "gacha-analysis-card",
        f"""
        <header><div><small>ENDFIELD / GACHA ARCHIVE</small><h1>{esc(view.role.nickname)} · 抽卡分析</h1><p>{esc(view.role.server_name or '默认服务器')} · {esc(uid)}</p></div><div class="sync-state"><b>{esc(page_state)}</b><span>同步 {esc(format_timestamp(view.last_sync_at))}</span></div></header>
        <main>
          {summary_html}
          <section class="two-column">{character_column}{weapon_column}</section>
          {f'<div class="warning">有 {len(view.errors)} 个卡池同步失败，其他成功数据已保留。</div>' if view.errors and show_summary else ''}
          <footer class="gacha-source"><span>{esc(source_text)}</span><span>免费十连单列展示，不计入任何保底{f' · 第 {page_number}/{page_count} 页' if page_count > 1 else ''}</span></footer>
        </main>
        """,
        extra_css=f"""
        header p{{margin:8px 0 0;color:#d0d0d0;font-size:16px}}.sync-state{{text-align:right}}.sync-state b{{display:block;font-size:22px}}.sync-state span{{display:block;margin-top:8px;color:#ccc}}
        .summary{{display:grid;grid-template-columns:.85fr 1.575fr 1.575fr;gap:10px;margin-bottom:16px}}.total,.metric{{min-height:172px;padding:15px 17px;border:1px solid #999;background:#fff}}.total{{display:flex;flex-direction:column;justify-content:center;border:3px solid #222}}.total span,.metric-head span{{color:#666;font-size:15px}}.total strong{{font-size:48px;line-height:1}}.metric-head{{display:flex;align-items:flex-end;justify-content:space-between;gap:12px}}.metric-head strong{{font-size:31px;line-height:1}}.total small,.metric>small{{display:block;margin-top:8px;color:#777;font-size:11px;font-weight:800}}.expectation-summary{{display:grid;gap:5px;margin-top:10px}}.expectation-row{{padding:7px 8px;border-left:4px solid #333;background:#ededed;color:#444;font-size:10px;font-weight:850;line-height:1.35}}.expectation-row b{{color:#111;font-size:12px;white-space:nowrap}}
        .two-column{{display:grid;grid-template-columns:1fr 1fr;align-items:start;gap:16px}}.pool-column{{min-width:0;border:1px solid #777;background:#e4e4e4}}.column-head{{display:flex;justify-content:space-between;align-items:flex-end;padding:15px 17px;border-bottom:4px solid #222;background:#fff}}.column-head h2{{margin:0;font-size:25px}}.column-head p{{margin:0;color:#666;font-size:13px}}.pool-stack{{display:grid;gap:10px;padding:10px}}.pool-card{{min-width:0;border:1px solid #888;background:#fff}}.pool-head{{position:relative;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;align-items:center;overflow:hidden;padding:11px 13px;border-bottom:1px solid #aaa}}.pool-head.has-banner{{min-height:92px}}.pool-head.character-banner-head{{padding-left:118px}}.pool-head.weapon-banner-head{{padding-left:102px}}.pool-head>.pool-title,.pool-head>.pool-total{{position:relative;z-index:2}}.pool-title{{min-width:0}}.pool-title strong{{display:block;font-size:18px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.pool-title span{{display:block;margin-top:3px;color:#777;font-size:11px}}.pool-total{{text-align:right}}.pool-total b{{display:block;font-size:22px}}.pool-total span{{display:block;color:#777;font-size:10px;white-space:nowrap}}.pool-total .pool-history{{margin-top:2px;color:#999;font-size:9px}}.pool-banner{{position:absolute;z-index:0;inset:0 21% 0 32%;display:flex;justify-content:center;align-items:center;overflow:hidden;pointer-events:none}}.pool-banner::after{{content:"";position:absolute;z-index:2;inset:-1px;background:linear-gradient(90deg,#fff 0%,rgba(255,255,255,.36) 18%,rgba(255,255,255,.08) 52%,rgba(255,255,255,.88) 100%)}}.pool-banner.character-pool-banner{{inset:0 auto 0 0;width:124px;justify-content:flex-start}}.pool-banner.character-pool-banner::after,.pool-banner.weapon-pool-banner::after{{background:linear-gradient(90deg,rgba(255,255,255,.04) 0%,rgba(255,255,255,.10) 58%,#fff 100%)}}.pool-banner.weapon-pool-banner{{inset:0 auto 0 0;width:108px;justify-content:center}}.pool-banner img{{position:relative;z-index:1;filter:saturate(.96) contrast(1.08)}}.pool-banner img.character-banner{{width:124px;height:112px;object-fit:cover;object-position:center 42%;-webkit-mask-image:radial-gradient(ellipse 82% 78% at center,#000 42%,rgba(0,0,0,.82) 64%,transparent 100%);mask-image:radial-gradient(ellipse 82% 78% at center,#000 42%,rgba(0,0,0,.82) 64%,transparent 100%)}}.pool-banner.multi-banner img.character-banner{{width:84px;height:108px;margin-left:-42px}}.pool-banner img:first-child{{margin-left:0}}.pool-banner.weapon-pool-banner img.weapon-banner{{width:92px;height:84px;margin:0;object-fit:contain;transform:translateX(-12px);-webkit-mask-image:radial-gradient(ellipse 86% 82% at center,#000 58%,rgba(0,0,0,.82) 72%,transparent 100%);mask-image:radial-gradient(ellipse 86% 82% at center,#000 58%,rgba(0,0,0,.82) 72%,transparent 100%)}}
        .current-tag{{display:inline-block;margin-right:6px;padding:2px 5px;border:1px solid #222;background:#222;color:#fff;font-size:9px;letter-spacing:.1em;vertical-align:2px}}.pity-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border-bottom:1px solid #aaa;background:#ececec}}.pity-grid.pity-two{{grid-template-columns:repeat(2,minmax(0,1fr))}}.pity-item{{min-width:0;padding:8px 10px;border-right:1px solid #bbb}}.pity-item:last-child{{border-right:0}}.pity-item span{{display:block;color:#666;font-size:9px;font-weight:900}}.pity-item b{{display:block;margin-top:2px;font-size:15px;white-space:nowrap}}.pity-item small{{display:block;margin-top:2px;color:#777;font-size:9px}}
        .pull-bars{{display:grid;gap:7px;padding:9px 11px 11px}}.pull-row{{display:grid;grid-template-columns:46px 144px minmax(0,1fr);align-items:center;gap:8px;min-width:0}}.gacha-thumb,.current-marker{{width:44px;height:44px;display:grid;place-items:center;overflow:hidden;border:1px solid #777;background:#eee}}.gacha-thumb{{border:2px solid #222}}.gacha-thumb img{{width:100%;height:100%;object-fit:contain}}.gacha-thumb span{{font-size:11px;font-weight:950}}.current-marker{{color:#555;font-size:11px;font-weight:900}}.pull-copy{{min-width:0}}.pull-copy strong{{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:14px}}.pull-copy time{{display:block;margin-top:3px;color:#777;font-size:9px}}.bar-track{{position:relative;height:36px;border:1px solid #999;background:#ededed}}.bar-fill{{height:100%;min-width:46px;background:#333}}.bar-fill.current{{background:#777}}.bar-value{{position:absolute;inset:0;display:flex;align-items:center;gap:7px;padding:0 10px;overflow:hidden}}.bar-value b{{flex:0 0 auto;color:#fff;white-space:nowrap;font-size:16px}}.pity-hits{{display:flex;align-items:center;gap:5px;min-width:0}}.pity-hit{{padding:3px 7px;border:2px solid #111;background:#fff;color:#111;font-size:11px;font-weight:950;line-height:1;white-space:nowrap;box-shadow:0 0 0 1px #fff}}.pity-hit-guarantee{{padding:3px 7px;font-size:11px;font-weight:950}}.pity-hit-small{{border-color:#3f6078;background:#e5eef4;color:#29485d;box-shadow:0 0 0 1px #f7fbfd}}.pity-hit-large{{border-color:#7a5c2e;background:#f4ead7;color:#5d421d;box-shadow:0 0 0 1px #fffaf0}}.pity-hit-miss{{padding:3px 8px;border:2px solid #f8e9e9;background:#8a3f46;color:#fff;font-size:12px;font-weight:950;letter-spacing:.12em;box-shadow:0 0 0 1px #5a2328}}.pool-empty{{padding:4px 0;color:#777;font-size:11px}}.free-row{{display:grid;grid-template-columns:96px minmax(0,1fr) auto;align-items:center;gap:8px;min-height:48px;padding:5px 7px;border:1px dashed #777;background:#f0f0f0}}.free-icons{{display:flex;align-items:center;min-height:44px}}.free-icons .gacha-thumb{{margin-right:-8px;background:#fff}}.free-marker{{width:88px;height:38px;display:grid;place-items:center;border:2px solid #555;color:#444;font-size:11px;font-weight:950;letter-spacing:.08em}}.free-count{{padding:4px 7px;border:1px solid #777;background:#fff;font-size:11px;font-weight:900;white-space:nowrap}}
        .warning{{margin-top:12px;padding:12px 16px;border:2px dashed #555;background:#f2f2f2}}.gacha-source{{display:flex;justify-content:space-between;gap:20px;margin-top:12px;padding-top:10px;border-top:2px solid #222;color:#777;font-size:12px;font-weight:800}}
        {compact_css}
        """,
    )


def _recent_gacha_pools(view: GachaAnalysis, item_type: str) -> list[PoolAnalysis]:
    pools = [
        item for item in view.pools
        if item.item_type == item_type and not _is_hidden_analysis_pool(item)
    ]
    return sorted(
        pools,
        key=lambda item: (
            0 if item.sort_order >= 0 else 1,
            item.sort_order if item.sort_order >= 0 else 0,
            -item.latest_ts,
            item.name,
        ),
    )


def _is_hidden_analysis_pool(pool: PoolAnalysis) -> bool:
    identity = f"{pool.pool_id} {pool.name}".casefold()
    return any(marker in identity for marker in ("standard", "基础寻访", "常驻池"))


def _gacha_column_render_rows(pools: list[PoolAnalysis]) -> int:
    return sum(
        (1 if pool.is_current else 0)
        + len(pool.six_stars)
        + len(pool.keepsake_gifts)
        + len(pool.free_batches)
        for pool in pools
    )


def _is_gacha_height_limit_error(exc: RuntimeError) -> bool:
    message = str(exc)
    return "Screenshot element height" in message and "exceeds limit" in message


def _gacha_pool_page_weight(pool: PoolAnalysis) -> int:
    return 2 + (3 if pool.is_current else 0) + _gacha_column_render_rows([pool])


def _paginate_gacha_pools(
    pools: list[PoolAnalysis],
    row_budget: int,
) -> list[list[PoolAnalysis]]:
    pieces = [
        piece
        for pool in pools
        for piece in _split_gacha_pool(pool, row_budget)
    ]
    if not pieces:
        return [[]]
    pages: list[list[PoolAnalysis]] = []
    current: list[PoolAnalysis] = []
    current_weight = 0
    for piece in pieces:
        weight = _gacha_pool_page_weight(piece)
        if current and current_weight + weight > row_budget:
            pages.append(current)
            current = []
            current_weight = 0
        current.append(piece)
        current_weight += weight
    if current:
        pages.append(current)
    return pages


def _split_gacha_pool(pool: PoolAnalysis, row_budget: int) -> list[PoolAnalysis]:
    if _gacha_pool_page_weight(pool) <= row_budget:
        return [pool]
    timeline = [
        (item.pool_position, item.gacha_ts, 0, "six", item)
        for item in pool.six_stars
    ]
    timeline.extend(
        (item.pool_position, item.gacha_ts, 1, "gift", item)
        for item in pool.keepsake_gifts
    )
    timeline.sort(key=lambda item: item[:3], reverse=True)
    content = [(kind, item) for *_sort, kind, item in timeline]
    content.extend(("free", item) for item in pool.free_batches)
    if not content:
        return [pool]
    first_capacity = max(1, row_budget - 6 if pool.is_current else row_budget - 2)
    continuation_capacity = max(1, row_budget - 2)
    chunks: list[list[tuple[str, object]]] = []
    cursor = 0
    capacity = first_capacity
    while cursor < len(content):
        chunks.append(content[cursor:cursor + capacity])
        cursor += capacity
        capacity = continuation_capacity
    result: list[PoolAnalysis] = []
    for index, chunk in enumerate(chunks):
        result.append(replace(
            pool,
            name=pool.name if index == 0 else f"{pool.name}（续 {index + 1}）",
            six_stars=tuple(item for kind, item in chunk if kind == "six"),
            keepsake_gifts=tuple(item for kind, item in chunk if kind == "gift"),
            free_batches=tuple(item for kind, item in chunk if kind == "free"),
            is_current=pool.is_current and index == 0,
        ))
    return result


def _draw_gacha_pool_column(
    pools: list[PoolAnalysis],
    title: str,
    paid_total: int,
    *,
    free_total: int | None = None,
    paginated: bool = False,
) -> str:
    if not pools:
        content = '<div class="empty">暂无此类卡池记录</div>'
    else:
        content = "".join(_draw_gacha_pool(item) for item in pools)
    six_star_total = sum(
        len(item.six_stars) + sum(len(batch.six_stars) for batch in item.free_batches)
        for item in pools
    )
    if free_total is None:
        free_total = sum(item.free_pull_count for item in pools)
    six_star_label = f"本页 {six_star_total} 个六星" if paginated else f"{six_star_total} 个六星"
    return f"""
    <div class="pool-column">
      <div class="column-head"><h2>{esc(title)}</h2><p>计保底 {paid_total} 抽 · 免费 {free_total} 抽 · {six_star_label}</p></div>
      <div class="pool-stack">{content}</div>
    </div>
    """


def _draw_gacha_expectation_summary(expectation: SixStarExpectation, item_type: str) -> str:
    return (
        '<div class="expectation-summary">'
        + _draw_gacha_expectation_row(
            "up", item_type, expectation.up_before, expectation.up_after,
            expectation.actual_up,
        )
        + _draw_gacha_expectation_row(
            "6星", item_type, expectation.before_up, expectation.after_up,
            expectation.actual,
        )
        + "</div>"
    )


def _draw_gacha_expectation_row(
    rarity_label: str,
    item_type: str,
    before: float,
    after: float | None,
    actual: float | None,
) -> str:
    theory = f"{before:.1f}" if after is None else f"{before:.1f} → {after:.1f}"
    actual_text = f"{actual:.1f}" if actual is not None else "暂无"
    return (
        '<div class="expectation-row">'
        f'获取{esc(rarity_label)}{esc(item_type)}的期望抽数为：<b>{esc(theory)}</b>，'
        f'该账号实际抽数为：<b>{esc(actual_text)}</b>'
        '</div>'
    )


def _draw_gacha_pool(pool: PoolAnalysis) -> str:
    scale = 40 if pool.item_type == "武器" else 80
    rows = [_draw_gacha_pull_row(None, pool.since_six_star, scale)] if pool.is_current else []
    timeline = [
        (item.pool_position, item.gacha_ts, 0, _draw_gacha_pull_row(item, item.interval, scale))
        for item in pool.six_stars
    ]
    timeline.extend(
        (item.pool_position, item.gacha_ts, 1, _draw_keepsake_gift_row(item))
        for item in pool.keepsake_gifts
    )
    timeline.sort(key=lambda item: item[:3], reverse=True)
    rows.extend(item[3] for item in timeline)
    rows.extend(_draw_free_pull_row(batch) for batch in pool.free_batches)
    empty = '<div class="pool-empty">本池记录中尚无六星</div>' if not pool.six_stars and not pool.free_batches and not pool.keepsake_gifts else ""
    latest = format_timestamp(pool.latest_ts).split(" ", 1)[0]
    current_tag = (
        '<span class="current-tag">CURRENT</span>'
        if pool.is_current and pool.item_type != "武器" else ""
    )
    banner_items = tuple(item for item in pool.banners if item.image_path)[:2]
    banner_art = _draw_gacha_pool_banner(banner_items)
    banner_class = " has-banner" if banner_art else ""
    if banner_art and any(item.item_type == "角色" for item in banner_items):
        banner_class += " character-banner-head"
    elif banner_art:
        banner_class += " weapon-banner-head"
    paid_total = _pool_paid_total(pool)
    total_caption = f"计保底 {paid_total} · 垫抽 {pool.since_six_star} · 免费 {pool.free_pull_count}"
    history_caption = (
        f'<span class="pool-history">逐抽 {pool.recorded_total} · 统计补齐 {pool.history_missing_count}</span>'
        if pool.history_missing_count else ""
    )
    pity = _draw_pity_grid(pool) if pool.is_current else ""
    return f"""
    <section class="pool-card">
      <div class="pool-head{banner_class}">{banner_art}<div class="pool-title"><strong>{current_tag}{esc(pool.name)}</strong><span>{esc(latest)} · {len(pool.six_stars)} 个计保底六星</span></div><div class="pool-total"><b>{pool.total}</b><span>{esc(total_caption)}</span>{history_caption}</div></div>
      {pity}
      <div class="pull-bars">{''.join(rows)}{empty}</div>
    </section>
    """


def _draw_gacha_pool_banner(items) -> str:
    images = []
    for item in items:
        image_url = _local_image_data_url(Path(item.image_path))
        if not image_url:
            continue
        kind = "weapon-banner" if item.item_type == "武器" else "character-banner"
        images.append(
            f'<img class="{kind}" src="{esc_attr(image_url)}" alt="{esc_attr(item.name)}">'
        )
    multi_class = " multi-banner" if len(images) > 1 else ""
    character_pool = images and any(item.item_type == "角色" for item in items)
    pool_class = " character-pool-banner" if character_pool else " weapon-pool-banner"
    return (
        f'<div class="pool-banner{multi_class}{pool_class}">{"".join(images)}</div>'
        if images else ""
    )


def _draw_gacha_pull_row(item: SixStarEvent | None, pulls: int, scale: int) -> str:
    width = max(8.0, min(100.0, pulls / scale * 100 if scale else 0))
    if item is None:
        marker = '<div class="current-marker">至今</div>'
        copy = '<div class="pull-copy"><strong>当前累计</strong><time>距最近六星</time></div>'
        current_class = " current"
    else:
        icon_url = _local_image_data_url(Path(item.icon_path)) if item.icon_path else ""
        icon = f'<img src="{esc_attr(icon_url)}" alt="{esc_attr(item.name)}">' if icon_url else '<span>6★</span>'
        marker = f'<div class="gacha-thumb">{icon}</div>'
        position = f"第{item.pool_position}抽 · " if item.pool_position else ""
        pity_hits = "".join(_draw_pity_hit(label) for label in item.pity_labels)
        pity_html = f'<div class="pity-hits">{pity_hits}</div>' if pity_hits else ""
        copy = f'<div class="pull-copy"><strong>{esc(item.name)}</strong><time>{esc(position + format_timestamp(item.gacha_ts).split(" ", 1)[0])}</time></div>'
        current_class = ""
    if item is None:
        pity_html = ""
    return f'<div class="pull-row">{marker}{copy}<div class="bar-track"><div class="bar-fill{current_class}" style="width:{width:.1f}%"></div><div class="bar-value"><b>{pulls} 抽</b>{pity_html}</div></div></div>'


def _draw_pity_hit(label: str) -> str:
    kinds = {
        "小保底": " pity-hit-guarantee pity-hit-small",
        "大保底": " pity-hit-guarantee pity-hit-large",
        "歪": " pity-hit-miss",
    }
    kind = kinds.get(label, "")
    return f'<span class="pity-hit{kind}">{esc(label)}</span>'


def _pool_paid_total(pool: PoolAnalysis) -> int:
    if pool.paid_total or pool.free_pull_count:
        return pool.paid_total
    return pool.total


def _draw_keepsake_gift_row(item) -> str:
    icon_url = _local_image_data_url(Path(item.icon_path)) if item.icon_path else ""
    icon = f'<img src="{esc_attr(icon_url)}" alt="{esc_attr(item.name)}">' if icon_url else '<span>信物</span>'
    return (
        f'<div class="pull-row"><div class="gacha-thumb">{icon}</div>'
        f'<div class="pull-copy"><strong>{esc(item.name)}</strong><time>第{item.pool_position}抽赠送信物</time></div>'
        f'<div class="bar-track"><div class="bar-fill" style="width:100.0%"></div>'
        f'<div class="bar-value"><b>第{item.pool_position}抽</b>'
        f'<div class="pity-hits"><span class="pity-hit">赠送</span></div></div></div></div>'
    )


def _draw_pity_grid(pool: PoolAnalysis) -> str:
    if pool.item_type == "角色":
        small_remaining = max(0, pool.small_pity_limit - pool.small_pity_progress)
        keepsake_remaining = 240 - pool.keepsake_progress if pool.keepsake_progress else 240
        keepsake_note = f"进度 {pool.keepsake_progress}/240"
        if pool.keepsake_claims:
            keepsake_note += f" · 已赠 {pool.keepsake_claims} 次"
        if not pool.large_pity_limit:
            large_pity = ("大保底", "无", "本池无120抽UP保底")
        elif pool.large_pity_consumed:
            up_name = f" {pool.large_pity_up_name}" if pool.large_pity_up_name else ""
            large_pity = (
                "距大保底", "已消耗", f"第{pool.large_pity_consumed_at}抽获得{up_name} · 本期无下次",
            )
        elif pool.large_pity_known:
            large_remaining = max(0, pool.large_pity_limit - pool.large_pity_progress)
            large_pity = (
                "距大保底", f"{large_remaining} 抽", f"进度 {pool.large_pity_progress}/{pool.large_pity_limit} · 本池首次当期UP",
            )
        else:
            large_pity = ("距大保底", "待识别", "未取得当期UP配置")
        items = (
            ("距小保底", f"{small_remaining} 抽", f"进度 {pool.small_pity_progress}/{pool.small_pity_limit} · 跨角色池继承"),
            large_pity,
            ("距下次信物", f"{keepsake_remaining} 抽", keepsake_note),
        )
    else:
        small_remaining = max(0, pool.small_pity_limit - pool.small_pity_progress)
        large_remaining = max(0, pool.large_pity_limit - pool.large_pity_progress)
        large_value = "已触发" if pool.large_pity_limit and not large_remaining else f"{large_remaining} 抽"
        items = (
            ("距小保底", f"{small_remaining} 次十连", f"进度 {pool.small_pity_progress}/{pool.small_pity_limit} · 第4次十连必出六星"),
            ("距大保底", large_value, f"进度 {pool.large_pity_progress}/{pool.large_pity_limit} · 本池当期UP"),
        )
    class_name = "pity-grid" if pool.item_type == "角色" else "pity-grid pity-two"
    return f'<div class="{class_name}">' + "".join(
        f'<div class="pity-item"><span>{esc(label)}</span><b>{esc(value)}</b><small>{esc(note)}</small></div>'
        for label, value, note in items
    ) + '</div>'


def _draw_free_pull_row(batch: FreePullBatch) -> str:
    if batch.six_stars:
        icons = "".join(_draw_gacha_icon(item) for item in batch.six_stars)
        names = "、".join(item.name for item in batch.six_stars)
        marker = f'<div class="free-icons">{icons}</div>'
        title = f"免费十连 · {names}"
        detail = f"出了 {len(batch.six_stars)} 个六星 · 不计保底"
    else:
        marker = '<div class="free-marker">FREE ×10</div>'
        title = "免费十连 · 未出六星"
        detail = "不受且不影响任何保底"
    label = "免费十连" if batch.pull_count == 10 else f"免费 {batch.pull_count} 抽"
    return (
        f'<div class="free-row">{marker}<div class="pull-copy"><strong>{esc(title)}</strong>'
        f'<time>{esc(format_timestamp(batch.gacha_ts).split(" ", 1)[0])} · {esc(detail)}</time>'
        f'</div><span class="free-count">{esc(label)}</span></div>'
    )


def _draw_gacha_icon(item: SixStarEvent) -> str:
    icon_url = _local_image_data_url(Path(item.icon_path)) if item.icon_path else ""
    icon = f'<img src="{esc_attr(icon_url)}" alt="{esc_attr(item.name)}">' if icon_url else '<span>6★</span>'
    return f'<div class="gacha-thumb">{icon}</div>'


async def draw_gacha_history_card(view: GachaHistoryView) -> bytes:
    rows = []
    for item in view.items:
        icon_url = _local_image_data_url(Path(item.icon_path)) if item.icon_path else ""
        icon = (
            f'<img src="{esc_attr(icon_url)}" alt="{esc_attr(item.item_name)}">'
            if icon_url
            else f'<span>{esc(item.rarity)}★</span>'
        )
        rows.append(
            f"""
            <div class="history-row rarity-{item.rarity}">
              <div class="history-icon">{icon}</div><time>{esc(item.time)}</time><div class="pool">{esc(item.pool_name)}</div><div class="item"><strong>{esc(item.item_name)}</strong><span>{esc(item.detail or item.item_type)}</span></div><b>{item.rarity}★</b><em>{esc(item.item_type)}</em>
            </div>
            """
        )
    rows_html = "".join(rows) or '<div class="empty">这一页没有抽卡记录</div>'
    filter_text = f" · 池：{esc(view.pool_filter)}" if view.pool_filter else ""
    return await _draw_neutral_card(
        "gacha-history-card",
        f"""
        <header><div><small>ENDFIELD / GACHA LOG</small><h1>{esc(view.nickname)} · 抽卡记录</h1><p>{esc(view.server_name or '默认服务器')} · {esc(view.uid)}{filter_text}</p></div><div class="page"><b>{view.page} / {view.total_pages}</b><span>共 {view.total} 条</span></div></header>
        <main><div class="history-head"><span>图</span><span>时间</span><span>卡池</span><span>名称</span><span>星级</span><span>类型</span></div><div class="history-list">{rows_html}</div><footer class="gacha-source"><span>星级与图片来源 FZ Wiki</span><span>图片已存入本地缓存</span></footer></main>
        """,
        extra_css="""
        header p{margin:8px 0 0;color:#d0d0d0}.page{text-align:right}.page b{display:block;font-size:24px}.page span{display:block;margin-top:6px;color:#ccc}
        .history-head,.history-row{display:grid;grid-template-columns:68px 160px 205px minmax(280px,1fr) 70px 80px;align-items:center}.history-head{padding:10px 15px;background:#d2d2d2;border:1px solid #999;font-size:13px;font-weight:900}.history-row{min-height:72px;padding:6px 15px;border:1px solid #b8b8b8;border-top:0;background:#fff}.history-row.rarity-6{border-left:8px solid #111;font-weight:900}.history-row.rarity-5{border-left:5px solid #666}.history-icon{width:56px;height:56px;display:grid;place-items:center;overflow:hidden;border:1px solid #888;background:#eee}.history-icon img{width:100%;height:100%;object-fit:contain}.history-icon span{font-size:12px;font-weight:950}.history-row time{color:#666;font-size:13px}.history-row .pool{padding-right:15px}.history-row .item{display:flex;flex-direction:column}.history-row .item span{margin-top:3px;color:#777;font-size:12px}.history-row b{font-size:19px}.history-row em{font-style:normal;color:#555}.gacha-source{display:flex;justify-content:space-between;margin-top:12px;padding-top:10px;border-top:2px solid #222;color:#777;font-size:12px;font-weight:800}
        """,
    )


async def _draw_neutral_card(selector: str, body: str, *, extra_css: str = "") -> bytes:
    width = 1280
    css = f"""
    *{{box-sizing:border-box}}html,body{{margin:0;width:{width}px;background:#d8d8d8;color:#181818;font-family:'Microsoft YaHei','PingFang SC','Noto Sans SC',Arial,sans-serif}}
    .{selector}{{width:{width}px;min-height:420px;padding:28px;background:linear-gradient(90deg,rgba(0,0,0,.055) 1px,transparent 1px) 0 0/32px 32px,linear-gradient(0deg,rgba(0,0,0,.055) 1px,transparent 1px) 0 0/32px 32px,#ededed}}
    header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;padding:22px 25px;background:#292929;color:#fff;border-bottom:5px solid #000}}
    header small{{font-size:13px;letter-spacing:.2em;color:#c7c7c7}}header h1{{margin:5px 0 0;font-size:36px;line-height:1.1}}header time{{color:#d0d0d0}}
    main{{padding:18px;border:1px solid #777;background:#f8f8f8}}.empty{{padding:28px;text-align:center;color:#777;background:#eee;border:1px dashed #888}}
    {extra_css}
    """
    document = f"<!doctype html><html><head><meta charset='utf-8'><style>{css}</style></head><body><div class='{selector}'>{body}</div></body></html>"
    html_path = _write_temp_html(document)
    try:
        output = await screenshot_web_element(
            html_path.resolve().as_uri(), f".{selector}", viewport=(width, 1), timeout_ms=15000,
            max_height=CARD_MAX_HEIGHT, device_scale_factor=2.0, settle_ms=30,
            wait_for_images=True, strict_max_height=True,
        )
        return await run_image_render(optimize_png_container, output)
    finally:
        schedule_temp_file_cleanup(html_path, delay_seconds=30)


async def draw_equipment_catalog_card(view: EquipmentCatalogView) -> bytes:
    started = perf_counter()
    prepared = await prepare_equipment_catalog_card_html(view)
    html_path = _write_temp_html(prepared.html)
    assets_seconds = perf_counter() - started
    try:
        screenshot_started = perf_counter()
        output = await screenshot_web_element(
            html_path.resolve().as_uri(),
            ".equipment-catalog-card",
            viewport=(prepared.width, 1),
            timeout_ms=20000,
            max_height=12000,
            device_scale_factor=1.5,
            settle_ms=50,
            resources=prepared.resources,
            wait_for_images=True,
            strict_max_height=True,
            overflow_selectors=(
                ".equipment-catalog-group",
                ".equipment-catalog-item",
            ),
        )
        optimize_started = perf_counter()
        optimized = await run_image_render(optimize_png_container, output)
        logger.info(
            f"[endfield] draw kind=equipment_catalog assets={assets_seconds:.3f}s "
            f"screenshot={optimize_started - screenshot_started:.3f}s "
            f"png_optimize={perf_counter() - optimize_started:.3f}s "
            f"bytes={len(output)}->{len(optimized)}"
        )
        return optimized
    finally:
        schedule_temp_file_cleanup(html_path, delay_seconds=30)


async def draw_operator_catalog_card(view: OperatorCatalogView) -> bytes:
    return await _draw_gallery_catalog(
        await prepare_operator_catalog_card_html(view),
        ".operator-catalog-card",
        (".operator-element", ".operator-catalog-item"),
        "operator_catalog",
    )


async def draw_weapon_catalog_card(view: WeaponCatalogView) -> bytes:
    return await _draw_gallery_catalog(
        await prepare_weapon_catalog_card_html(view),
        ".weapon-catalog-card",
        (".weapon-catalog-group", ".weapon-catalog-item"),
        "weapon_catalog",
    )


async def draw_loadout_card(view: LoadoutView) -> bytes:
    return await _draw_gallery_catalog(
        await prepare_loadout_card_html(view),
        ".loadout-card",
        (".loadout-panel", ".loadout-stat", ".loadout-effect", ".loadout-item"),
        "loadout",
    )


async def _draw_gallery_catalog(
    prepared: PreparedCardHtml,
    selector: str,
    overflow_selectors: tuple[str, ...],
    kind: str,
) -> bytes:
    html_path = _write_temp_html(prepared.html)
    try:
        screenshot_started = perf_counter()
        output = await screenshot_web_element(
            html_path.resolve().as_uri(),
            selector,
            viewport=(prepared.width, 1),
            timeout_ms=25000,
            max_height=12000,
            device_scale_factor=1.25,
            settle_ms=50,
            resources=prepared.resources,
            wait_for_images=True,
            strict_max_height=True,
            overflow_selectors=overflow_selectors,
        )
        optimize_started = perf_counter()
        optimized = await run_image_render(optimize_png_container, output)
        logger.info(
            f"[endfield] draw kind={kind} screenshot={optimize_started - screenshot_started:.3f}s "
            f"png_optimize={perf_counter() - optimize_started:.3f}s bytes={len(output)}->{len(optimized)}"
        )
        return optimized
    finally:
        schedule_temp_file_cleanup(html_path, delay_seconds=30)


async def render_operator_card_html(view: OperatorView) -> str:
    return (await _prepare_operator_card_html(view, inline=True)).html


async def render_loadout_card_html(view: LoadoutView) -> str:
    return (await _prepare_loadout_card_html(view, inline=True)).html


async def prepare_loadout_card_html(view: LoadoutView) -> PreparedCardHtml:
    return await _prepare_loadout_card_html(view, inline=False)


async def _prepare_loadout_card_html(view: LoadoutView, *, inline: bool) -> PreparedCardHtml:
    equipment_urls = [item.icon_url for item in view.equipment if item.icon_url]
    assets = await _prepare_assets(
        [view.operator_icon_url, view.weapon_icon_url, *equipment_urls],
        inline=inline,
    )
    return PreparedCardHtml(
        _render_loadout_html(view, assets.urls),
        assets.resources,
        1500,
    )


async def prepare_operator_card_html(view: OperatorView) -> PreparedCardHtml:
    return await _prepare_operator_card_html(view, inline=False)


async def _prepare_operator_card_html(view: OperatorView, *, inline: bool) -> PreparedCardHtml:
    portrait_candidates = tuple(
        dict.fromkeys(url for url in (view.portrait_url, view.icon_url, view.round_icon_url) if url)
    )
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
        [*portrait_candidates, *skill_urls.values(), *talent_urls.values(), *term_urls.values()]
        ,
        inline=inline,
    )
    portrait_url = next((url for url in portrait_candidates if assets.urls.get(url)), "")
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


async def render_equipment_card_html(view: EquipmentView) -> str:
    return (await _prepare_equipment_card_html(view, inline=True)).html


async def prepare_equipment_card_html(view: EquipmentView) -> PreparedCardHtml:
    return await _prepare_equipment_card_html(view, inline=False)


async def _prepare_equipment_card_html(view: EquipmentView, *, inline: bool) -> PreparedCardHtml:
    piece_urls = [piece.icon_url for piece in view.suit_pieces if piece.icon_url]
    used_text = view.suit_description
    term_urls = {
        key: style.icon_url
        for key, style in view.term_styles.items()
        if style.icon_url and (key in used_text or style.term in used_text)
    }
    assets = await _prepare_assets(
        [view.icon_url, *piece_urls, *term_urls.values()],
        inline=inline,
    )
    equipment_img = assets.urls.get(view.icon_url, "")
    piece_icons = {url: assets.urls.get(url, "") for url in piece_urls}
    term_icons = {key: assets.urls.get(url, "") for key, url in term_urls.items()}
    return PreparedCardHtml(
        _render_equipment_html(view, equipment_img, piece_icons, term_icons),
        assets.resources,
        1500,
    )


async def render_equipment_catalog_card_html(view: EquipmentCatalogView) -> str:
    return (await _prepare_equipment_catalog_card_html(view, inline=True)).html


async def prepare_equipment_catalog_card_html(view: EquipmentCatalogView) -> PreparedCardHtml:
    return await _prepare_equipment_catalog_card_html(view, inline=False)


async def _prepare_equipment_catalog_card_html(
    view: EquipmentCatalogView,
    *,
    inline: bool,
) -> PreparedCardHtml:
    icon_urls = [
        item.icon_url
        for group in view.groups
        for item in group.items
        if item.icon_url
    ]
    assets = await _prepare_assets(icon_urls, inline=inline)
    item_icons = {url: assets.urls.get(url, "") for url in icon_urls}
    card_width, columns = equipment_catalog_layout(view)
    return PreparedCardHtml(
        _render_equipment_catalog_html(view, item_icons, card_width, columns),
        assets.resources,
        card_width,
    )


async def render_operator_catalog_card_html(view: OperatorCatalogView) -> str:
    return (await _prepare_operator_catalog_card_html(view, inline=True)).html


async def prepare_operator_catalog_card_html(view: OperatorCatalogView) -> PreparedCardHtml:
    return await _prepare_operator_catalog_card_html(view, inline=False)


async def _prepare_operator_catalog_card_html(
    view: OperatorCatalogView,
    *,
    inline: bool,
) -> PreparedCardHtml:
    icon_urls = list(dict.fromkeys(
        url
        for element in view.elements
        for profession in element.professions
        for item in profession.items
        for url in (
            item.icon_url,
            item.element_icon_url,
            item.profession_icon_url,
            item.weapon_type_icon_url,
        )
        if url
    ))
    assets = await _prepare_assets(icon_urls, inline=inline)
    icon_map = {url: assets.urls.get(url, "") for url in icon_urls}
    return PreparedCardHtml(_render_operator_catalog_html(view, icon_map), assets.resources, 1900)


async def render_weapon_catalog_card_html(view: WeaponCatalogView) -> str:
    return (await _prepare_weapon_catalog_card_html(view, inline=True)).html


async def prepare_weapon_catalog_card_html(view: WeaponCatalogView) -> PreparedCardHtml:
    return await _prepare_weapon_catalog_card_html(view, inline=False)


async def _prepare_weapon_catalog_card_html(
    view: WeaponCatalogView,
    *,
    inline: bool,
) -> PreparedCardHtml:
    icon_urls = list(dict.fromkeys(
        url
        for group in view.groups
        for url in (group.icon_url, *(item.icon_url for item in group.items))
        if url
    ))
    assets = await _prepare_assets(icon_urls, inline=inline)
    icon_map = {url: assets.urls.get(url, "") for url in icon_urls}
    return PreparedCardHtml(_render_weapon_catalog_html(view, icon_map), assets.resources, 1900)


def _render_loadout_html(view: LoadoutView, asset_urls: dict[str, str]) -> str:
    operator_img = asset_urls.get(view.operator_icon_url, "")
    weapon_img = asset_urls.get(view.weapon_icon_url, "")

    def equipment_stats(item) -> str:
        return "".join(
            f'<div class="loadout-item-stat"><span>{esc(stat.label)}</span><b>{esc(stat.value)}</b></div>'
            for stat in item.stats
        )

    equipment_html = "".join(
        f'''<article class="loadout-item">
          <div class="loadout-item-top"><span class="loadout-slot">{esc(item.slot_type)}</span><span class="loadout-forge">{esc(_loadout_forge_summary(item.enhance_levels))}</span></div>
          <div class="loadout-item-visual">{f'<img src="{esc_attr(asset_urls.get(item.icon_url, ""))}" alt="">' if asset_urls.get(item.icon_url, "") else '<span>EQ</span>'}</div>
          <div class="loadout-item-name">{esc(item.name)}</div>
          <div class="loadout-item-suit">{esc(item.suit_name or "独立装备")}</div>
          <div class="loadout-item-stats">{equipment_stats(item)}</div>
        </article>'''
        for item in view.equipment
    ) or '<div class="loadout-empty">未装备护甲、护手或配件</div>'

    def stat_cards(rows, class_name: str = "") -> str:
        return "".join(
            f'''<div class="loadout-stat {class_name}">
              <div class="loadout-stat-head">{equipment_attribute_icon(row.label, "loadout-stat-icon-img", "loadout-stat-icon-fallback")}<span>{esc(row.label)}</span></div>
              <strong>{esc(row.value)}</strong>{f'<small>{esc(row.detail)}</small>' if row.detail else ''}
            </div>'''
            for row in rows
        )

    active_effects = [effect for effect in view.effects if effect.active]
    triggered_effects = [effect for effect in view.effects if not effect.active]

    def effect_cards(items) -> str:
        return "".join(
            f'<div class="loadout-effect"><b>{esc(item.source)}</b><span>{highlight_terms(item.description, view.term_styles, {})}</span></div>'
            for item in items
        ) or '<div class="loadout-empty">无</div>'

    def status_cards() -> str:
        if not view.status_effects:
            return '<div class="loadout-empty">该干员没有导电、腐蚀或碎甲附带效果</div>'
        cards = []
        for effect in view.status_effects:
            levels = "".join(
                f'''<div class="status-level">
                  <b>LV {level.level}</b><strong>{esc(level.value)}</strong><span>{esc(level.detail)}</span><small>{esc(level.duration)}</small>
                </div>'''
                for level in effect.levels
            )
            cards.append(
                f'''<article class="status-card{' forced' if effect.forced else ''}">
                  <div class="status-card-head"><div><b>{esc(effect.name)}</b><span>{esc(effect.source)}</span></div><em>{'强制' if effect.forced else '常规'}</em></div>
                  <div class="status-levels{' single' if len(effect.levels) == 1 else ''}">{levels}</div>
                  <div class="status-note">{esc(effect.note)}</div>
                </article>'''
            )
        return "".join(cards)

    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}}
html,body{{margin:0;width:1500px;background:#d9dde0;color:#171b1f;font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",Arial,sans-serif}}
.loadout-card{{width:1500px;padding:28px;background:linear-gradient(90deg,rgba(29,34,39,.075) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(0deg,rgba(29,34,39,.075) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(135deg,#f7f8f4 0%,#e7eaeb 62%,#cfd5d9 100%)}}
.loadout-head{{min-height:92px;display:grid;grid-template-columns:1fr auto;align-items:end;gap:24px;padding:18px 22px 16px;background:#171b1f;color:#fff;border-bottom:7px solid #6f7880}}
.loadout-kicker{{color:#9ca5ab;font-size:15px;font-weight:900;letter-spacing:.12em}} .loadout-title{{margin-top:6px;font-size:42px;line-height:1;font-weight:950}}
.loadout-subtitle{{margin-bottom:4px;color:#c8ced2;font-size:16px;font-weight:800;text-align:right}}
.loadout-overview{{display:grid;grid-template-columns:420px 1fr;gap:18px;margin-top:18px}}
.loadout-identity,.loadout-panel{{border:1px solid rgba(23,27,31,.30);background:rgba(247,248,246,.94);box-shadow:-10px 16px 38px rgba(23,27,31,.10)}}
.loadout-identity{{padding:20px}}
.operator-block{{display:grid;grid-template-columns:145px 1fr;gap:17px;align-items:center;padding-bottom:16px;border-bottom:5px solid #171b1f}}
.operator-visual{{width:145px;height:145px;display:grid;place-items:center;overflow:hidden;background:radial-gradient(circle,#fff 0,#e7eaeb 62%,#cdd2d5 100%);border:1px solid rgba(23,27,31,.22)}} .operator-visual img{{width:100%;height:100%;object-fit:contain}} .operator-visual span{{color:#899197;font-size:28px;font-weight:950}}
.operator-level{{color:#687177;font-size:14px;font-weight:900}} .operator-name{{margin-top:4px;font-size:43px;line-height:.98;font-weight:950;overflow-wrap:anywhere}}
.operator-tags{{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}} .operator-tag{{padding:5px 9px;background:#e1e4e5;border-left:5px solid #20252a;font-size:13px;font-weight:900}}
.weapon-block{{display:grid;grid-template-columns:105px 1fr;gap:14px;align-items:center;margin-top:15px;padding:12px;background:#e5e8e9;border:1px solid rgba(23,27,31,.20)}}
.weapon-visual{{width:105px;height:82px;display:grid;place-items:center;background:rgba(255,255,255,.62)}} .weapon-visual img{{width:100%;height:100%;object-fit:contain}} .weapon-visual span{{color:#899197;font-weight:950}}
.weapon-label{{color:#697279;font-size:12px;font-weight:900}} .weapon-name{{margin-top:3px;font-size:25px;line-height:1.05;font-weight:950}} .weapon-meta{{margin-top:5px;color:#4c565d;font-size:13px;font-weight:850}}
.loadout-panel{{padding:18px 20px}}
.section-title{{display:flex;align-items:center;gap:10px;margin:0 0 12px;padding-bottom:9px;border-bottom:4px solid #20252a;font-size:25px;line-height:1;font-weight:950}} .section-title::before{{content:"";width:9px;height:27px;background:#286cd6}}
.core-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}} .ability-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:8px}}
.loadout-stat{{min-height:108px;padding:12px 13px;background:linear-gradient(180deg,rgba(255,255,255,.80),rgba(232,235,236,.88));border:1px solid rgba(23,27,31,.22);border-left:7px solid #20252a}}
.loadout-stat-head{{display:flex;align-items:center;gap:8px;color:#4a555c;font-size:15px;font-weight:900}} .loadout-stat-icon-img{{width:29px;height:29px;object-fit:contain;filter:brightness(0) saturate(100%);opacity:.56}} .loadout-stat-icon-fallback{{width:29px;height:29px;display:grid;place-items:center;background:#d5dadd;color:#59646b;font-size:11px;font-weight:950}}
.loadout-stat strong{{display:block;margin-top:6px;color:#286cd6;font-size:36px;line-height:1;font-weight:950}} .loadout-stat small{{display:block;margin-top:7px;color:#727b81;font-size:12px;line-height:1.32;font-weight:800}}
.loadout-stat.ability{{min-height:78px;padding:10px}} .loadout-stat.ability strong{{font-size:27px}} .loadout-stat.ability .loadout-stat-icon-img,.loadout-stat.ability .loadout-stat-icon-fallback{{width:24px;height:24px}}
.loadout-section{{margin-top:18px;padding:18px 20px;border:1px solid rgba(23,27,31,.30);background:rgba(247,248,246,.94)}}
.loadout-items{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}} .loadout-item{{position:relative;min-height:365px;padding:10px;background:rgba(255,255,255,.68);border:1px solid rgba(23,27,31,.24)}}
.loadout-item-top{{display:flex;justify-content:space-between;align-items:center;gap:8px;min-height:29px}} .loadout-slot{{padding:5px 9px;background:#20252a;color:#fff;font-size:13px;font-weight:950}} .loadout-forge{{color:#536068;font-size:12px;font-weight:900;text-align:right}}
.loadout-item-visual{{height:180px;display:flex;align-items:center;justify-content:center;margin-top:5px;padding:8px;background:radial-gradient(circle,#fff 0,#eceeef 58%,transparent 72%)}} .loadout-item-visual img{{display:block;width:auto;height:auto;max-width:100%;max-height:100%;object-fit:contain;filter:drop-shadow(0 12px 10px rgba(23,27,31,.20))}} .loadout-item-visual span{{color:#92999e;font-size:22px;font-weight:950}}
.loadout-item-name{{margin-top:7px;font-size:20px;line-height:1.1;font-weight:950}} .loadout-item-suit{{margin-top:5px;color:#727b81;font-size:13px;font-weight:850}}
.loadout-item-stats{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:5px;margin-top:9px}} .loadout-item-stat{{display:flex;align-items:center;justify-content:space-between;gap:6px;min-height:29px;padding:5px 7px;background:#e4e8ea;border-left:4px solid #20252a;color:#59646b;font-size:11px;font-weight:850}} .loadout-item-stat span{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}} .loadout-item-stat b{{flex:none;color:#286cd6;font-size:13px;font-weight:950}}
.advanced-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:7px}} .advanced-grid .loadout-stat{{min-height:75px;padding:9px 10px;border-left-width:5px}} .advanced-grid .loadout-stat strong{{font-size:24px}} .advanced-grid .loadout-stat-icon-img,.advanced-grid .loadout-stat-icon-fallback{{width:23px;height:23px}}
.status-summary{{margin:-3px 0 12px;color:#58636a;font-size:13px;font-weight:850}} .status-summary b{{color:#286cd6;font-size:17px}}
.status-grid{{display:grid;gap:10px}} .status-card{{padding:12px;background:rgba(255,255,255,.72);border:1px solid rgba(23,27,31,.22);border-left:7px solid #20252a}} .status-card.forced{{border-left-color:#286cd6}}
.status-card-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:9px}} .status-card-head>div{{display:flex;align-items:baseline;gap:10px}} .status-card-head b{{font-size:23px;font-weight:950}} .status-card-head span{{color:#667078;font-size:13px;font-weight:850}} .status-card-head em{{padding:4px 9px;background:#20252a;color:#fff;font-size:11px;font-style:normal;font-weight:950;letter-spacing:.08em}} .status-card.forced .status-card-head em{{background:#286cd6}}
.status-levels{{display:grid;grid-template-columns:repeat(4,1fr);gap:7px}} .status-levels.single{{grid-template-columns:minmax(260px,1fr)}} .status-level{{min-height:92px;padding:9px 10px;background:linear-gradient(180deg,#eef1f2,#e0e4e6);border:1px solid rgba(23,27,31,.16)}} .status-level b{{display:block;color:#657078;font-size:11px;font-weight:950}} .status-level strong{{display:block;margin-top:4px;color:#286cd6;font-size:19px;line-height:1.1;font-weight:950}} .status-level span{{display:block;margin-top:5px;color:#4a555c;font-size:12px;font-weight:850}} .status-level small{{display:block;margin-top:4px;color:#747e85;font-size:11px;font-weight:850}}
.status-note{{margin-top:8px;color:#667078;font-size:12px;line-height:1.4;font-weight:850}}
.effect-columns{{display:grid;grid-template-columns:1fr 1fr;gap:12px}} .loadout-effect-list{{display:grid;gap:7px;align-content:start}} .loadout-effect{{display:grid;grid-template-columns:170px 1fr;gap:12px;padding:10px 11px;background:rgba(255,255,255,.70);border:1px solid rgba(23,27,31,.18);border-left:6px solid #20252a;line-height:1.42}}
.loadout-effect b{{font-size:13px;font-weight:950}} .loadout-effect span{{color:#3e484f;font-size:13px;font-weight:750}} .loadout-effect .term,.loadout-effect .vup,.loadout-effect .rich-style{{color:#286cd6 !important;font-weight:950}} .loadout-effect .info-note{{color:#5d6870 !important;font-weight:850}} .effect-note{{margin:-4px 0 9px;color:#747d83;font-size:12px;font-weight:850}}
.loadout-empty{{padding:14px;color:#7a8389;background:rgba(23,27,31,.055);font-weight:850}}
.loadout-note{{margin-top:18px;padding:13px 16px;background:#20252a;color:#cbd1d4;font-size:13px;line-height:1.55;font-weight:800}} .loadout-note strong{{color:#fff}}
</style></head><body><main class="loadout-card">
<header class="loadout-head"><div><div class="loadout-kicker">终末地 · 配装模拟器</div><div class="loadout-title">配装面板</div></div><div class="loadout-subtitle">ARKNIGHTS: ENDFIELD<br>数据来源 api.fz.wiki · 更新 {esc(view.source_version or '--')}</div></header>
<section class="loadout-overview"><aside class="loadout-identity"><div class="operator-block"><div class="operator-visual">{f'<img src="{esc_attr(operator_img)}" alt="">' if operator_img else '<span>OP</span>'}</div><div><div class="operator-level">干员 · LEVEL {view.operator_level} · 潜能 {view.operator_potential}</div><div class="operator-name">{esc(view.operator_name)}</div><div class="operator-tags"><span class="operator-tag">主 · {esc(view.main_attribute)}</span><span class="operator-tag">副 · {esc(view.sub_attribute)}</span><span class="operator-tag">{esc(view.weapon_type)}</span></div></div></div><div class="weapon-block"><div class="weapon-visual">{f'<img src="{esc_attr(weapon_img)}" alt="">' if weapon_img else '<span>WP</span>'}</div><div><div class="weapon-label">武器 · LEVEL {view.weapon_level}</div><div class="weapon-name">{esc(view.weapon_name)}</div><div class="weapon-meta">潜能 {view.weapon_potential} · {esc(view.weapon_type)}</div></div></div></aside>
<div class="loadout-panel"><h2 class="section-title">核心面板</h2><div class="core-grid">{stat_cards(view.primary_stats)}</div><div class="ability-grid">{stat_cards(view.ability_stats, 'ability')}</div></div></section>
<section class="loadout-section"><h2 class="section-title">装备配置</h2><div class="loadout-items">{equipment_html}</div></section>
<section class="loadout-section"><h2 class="section-title">进阶面板</h2><div class="advanced-grid">{stat_cards(view.advanced_stats)}</div></section>
<section class="loadout-section"><h2 class="section-title">最终异常效果</h2><div class="status-summary">源石技艺附带效果增益 <b>+{view.status_effect_bonus * 100:.1f}%</b>　公式 2 × 源石技艺强度 ÷（源石技艺强度 + 300）</div><div class="status-grid">{status_cards()}</div></section>
<section class="loadout-section"><h2 class="section-title">效果明细</h2><div class="effect-columns"><div><div class="effect-note">常驻 / 无触发条件效果</div><div class="loadout-effect-list">{effect_cards(active_effects)}</div></div><div><div class="effect-note">条件 / 触发效果</div><div class="loadout-effect-list">{effect_cards(triggered_effects)}</div></div></div></section>
<footer class="loadout-note"><strong>计算说明</strong>　攻击力按配装公式计算，能力值使用四维属性整数部分；生命值计入 5 × 力量，敏捷 / 智识 / 意志换算对应派生属性。显示结果按游戏规则向下取整。</footer>
</main></body></html>'''


def _loadout_forge_summary(levels: tuple[int, ...]) -> str:
    if not levels:
        return "无可锻造词条"
    if len(set(levels)) == 1:
        return f"全部 {levels[0]} 锻"
    return " / ".join(f"词条{index} {level}锻" for index, level in enumerate(levels, 1))


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
  min-height: {OPERATOR_RAIL_HEIGHT}px;
  height: auto;
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
.stars {{ display: flex; align-items: center; gap: 6px; margin-top: 10px; min-height: 42px; color: transparent; font-size: 0; line-height: 1; }}
.rarity-star {{ width: 40px; height: 40px; object-fit: contain; flex: 0 0 auto; filter: drop-shadow(0 1px 0 rgba(255,255,255,.65)) drop-shadow(0 2px 2px rgba(23,27,31,.22)); }}
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
.skill-desc:empty {{ display: none; }}
.skill-form-list {{ margin: 6px 0 0 13px; display: grid; gap: 5px; }}
.skill-form-desc {{ padding: 5px 7px; border-left: 4px solid #ffd000; background: rgba(23,27,31,.055); }}
.skill-form-name {{ color: #171b1f; font-size: 13px; line-height: 1.15; font-weight: 900; }}
.skill-form-text {{ margin-top: 2px; color: #364047; font-size: 12.5px; line-height: 1.25; font-weight: 700; }}
.skill-meta {{ margin: 6px 0 0 13px; display: grid; grid-template-columns: repeat(2, 1fr); gap: 4px; }}
.skill-meta:empty {{ display: none; }}
.skill-meta span {{ min-height: 22px; padding: 3px 5px; background: rgba(23,27,31,.08); color: #313940; font-size: 12.5px; font-weight: 900; text-align: center; white-space: normal; overflow-wrap: anywhere; }}
.skill-meta strong {{ color: #11161a; background: #ffd000; padding: 0 3px; }}
.metric-table {{ --metric-label-width: 92px; margin: 6px 0 0 13px; display: grid; grid-template-columns: var(--metric-label-width) repeat(4, minmax(46px, 1fr)); grid-auto-rows: minmax(22px, auto); border-top: 1px solid rgba(23,27,31,.2); border-left: 1px solid rgba(23,27,31,.2); }}
.metric-table div {{ min-height: 22px; padding: 3px 4px; border-right: 1px solid rgba(23,27,31,.2); border-bottom: 1px solid rgba(23,27,31,.2); font-size: 12.5px; font-weight: 800; line-height: 1.1; overflow: visible; }}
.metric-group {{ grid-column: 1 / -1; min-height: 27px !important; padding: 5px 8px !important; display: flex; align-items: center; justify-content: space-between; gap: 10px; background: #20252a; color: #ffd000; border-right: 0 !important; font-size: 14px !important; font-weight: 900 !important; }}
.metric-group::before {{ content: ""; width: 5px; height: 16px; flex: 0 0 auto; background: #ffd000; }}
.metric-group-name {{ margin-right: auto; }}
.metric-group-note {{ color: #e1e5e7; font-size: 11px; font-weight: 800; }}
.metric-name {{ background: rgba(23,27,31,.08); color: #313940; white-space: normal; overflow-wrap: anywhere; word-break: break-word; display: flex; align-items: center; }}
.metric-name.long {{ font-size: 11.5px; line-height: 1.08; }}
.metric-value {{ background: #f9fbfa; text-align: center; color: #171b1f; display: flex; align-items: center; justify-content: center; white-space: normal; overflow-wrap: anywhere; word-break: break-word; }}
.metric-value.strong {{ background: #f9fbfa; color: #a86500; font-weight: 900; }}
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
    Math.ceil(rail.scrollHeight) + 56,
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
.stars {{ display:flex; align-items:center; gap:6px; min-height:42px; margin-top:14px; }} .rarity-star {{ width:40px; height:40px; object-fit:contain; flex:0 0 auto; filter:drop-shadow(0 2px 2px rgba(23,27,31,.25)); }}
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


def _render_equipment_html(
    view: EquipmentView,
    equipment_img: str,
    piece_icons: dict[str, str],
    term_icons: dict[str, str],
) -> str:
    card_width = 1500
    card_min_height = 920
    equipment_image = image(equipment_img, view.name)
    if equipment_image:
        equipment_image = equipment_image.replace("<img ", '<img class="equipment-image" ', 1)
    else:
        equipment_image = f'<div class="equipment-image-fallback">{esc(view.name)}</div>'
    suit_description = highlight_terms(
        view.suit_description,
        view.term_styles,
        term_icons,
    )
    description = esc(view.description).replace("\n", "<br>")
    flavor = esc(view.flavor).replace("\n", "<br>")
    css = f"""
* {{ box-sizing:border-box; }}
html,body {{ margin:0; width:{card_width}px; min-height:{card_min_height}px; background:#d9dde0; font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",Arial,sans-serif; color:#171b1f; }}
.equipment-card {{ position:relative; width:{card_width}px; min-height:{card_min_height}px; overflow:visible; background:linear-gradient(90deg,rgba(29,34,39,.07) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(0deg,rgba(29,34,39,.07) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(135deg,#f7f8f4 0%,#e7eaeb 62%,#cfd5d9 100%); }}
.equipment-left,.equipment-right {{ position:absolute; z-index:2; top:28px; height:auto; overflow:visible; border:1px solid rgba(23,27,31,.28); background:rgba(248,249,247,.94); }}
    .equipment-left {{ left:28px; width:650px; min-height:864px; padding:22px 24px 20px; }}
    .equipment-right {{ left:700px; width:772px; min-height:864px; padding:22px 24px 18px; display:flex; flex-direction:column; box-shadow:-12px 18px 44px rgba(23,27,31,.12); }}
.equipment-name {{ font-size:50px; line-height:1; font-weight:950; letter-spacing:-.035em; overflow-wrap:anywhere; }}
.equipment-group {{ margin-top:6px; color:#687177; font-size:19px; font-weight:900; }}
.equipment-meta {{ display:flex; align-items:center; gap:12px; margin-top:13px; padding-bottom:10px; border-bottom:4px solid #171b1f; }}
.equipment-slot {{ padding:7px 13px; border-left:6px solid #ffd000; background:#20252a; color:#fff; font-size:18px; font-weight:950; }}
.equipment-copy {{ margin-top:12px; color:#3b444b; font-size:17px; line-height:1.42; font-weight:750; }}
.equipment-flavor {{ margin-top:7px; padding-left:13px; border-left:4px solid #c7ccd0; color:#71797e; font-size:15px; line-height:1.36; font-weight:750; }}
.equipment-stage {{ position:relative; height:485px; margin-top:8px; overflow:hidden; background:radial-gradient(circle at 50% 48%,rgba(255,255,255,.98) 0,rgba(255,255,255,.62) 29%,rgba(236,239,239,.12) 67%,transparent 68%); }}
.equipment-stage::before,.equipment-stage::after {{ content:""; position:absolute; left:50%; top:50%; border:1px solid rgba(82,93,100,.11); border-radius:50%; transform:translate(-50%,-50%); }}
.equipment-stage::before {{ width:430px; height:430px; box-shadow:0 0 0 28px rgba(82,93,100,.025),0 0 0 76px rgba(82,93,100,.018); }}
.equipment-stage::after {{ width:275px; height:275px; border-style:dashed; }}
.equipment-image {{ position:absolute; z-index:2; left:50%; top:50%; width:470px; height:420px; object-fit:contain; transform:translate(-50%,-50%); filter:drop-shadow(0 30px 22px rgba(23,27,31,.28)); }}
.equipment-image-fallback {{ position:absolute; z-index:2; left:0; right:0; top:45%; text-align:center; color:#899197; font-size:26px; font-weight:950; }}
.equipment-section {{ margin-top:15px; }}
.equipment-section:first-child {{ margin-top:0; }}
    .equipment-section-title {{ display:flex; align-items:center; gap:10px; padding:0 0 10px; border-bottom:4px solid #20252a; font-size:29px; line-height:1; font-weight:950; }}
    .equipment-section-title::before {{ content:""; width:9px; height:30px; background:#ffd000; }}
    .equipment-level {{ display:flex; align-items:end; gap:10px; padding:14px 10px 8px; }}
    .equipment-level-number {{ font-size:60px; line-height:.9; font-weight:950; }}
    .equipment-level-label {{ margin-bottom:7px; padding:2px 8px; background:#aeb4b7; color:#fff; font-size:12px; font-weight:900; text-transform:uppercase; }}
    .equipment-stats {{ display:grid; gap:5px; }}
    .equipment-forge-head,.equipment-stat {{ display:grid; grid-template-columns:minmax(230px,1fr) repeat(4,108px); align-items:stretch; }}
    .equipment-forge-head {{ min-height:38px; background:#20252a; color:#fff; }}
    .equipment-forge-head span {{ display:grid; place-items:center; border-left:1px solid rgba(255,255,255,.14); font-size:14px; font-weight:900; }}
.equipment-forge-head span:first-child {{ justify-content:start; padding-left:12px; border-left:0; color:#ffd000; }}
    .equipment-stat {{ min-height:72px; background:rgba(23,27,31,.075); }}
    .equipment-stat-main {{ display:flex; align-items:center; gap:11px; padding:9px 12px; border-left:7px solid #20252a; }}
    .equipment-stat-icon {{ width:41px; height:41px; flex:0 0 auto; display:grid; place-items:center; color:#687177; }}
    .equipment-stat-icon svg,.equipment-stat-icon-img {{ width:40px; height:40px; display:block; object-fit:contain; }}
.equipment-stat-icon-img {{ filter:brightness(0) saturate(100%); opacity:.54; }}
    .equipment-stat-name {{ color:#3b444b; font-size:23px; line-height:1.12; font-weight:900; }}
    .equipment-stat-value {{ display:grid; place-items:center; min-width:0; padding:7px 3px; border-left:1px solid rgba(23,27,31,.15); color:#171b1f; font-size:25px; font-weight:950; }}
.equipment-stat-value.strong {{ color:#a86500; }}
    .suit-bar {{ display:flex; align-items:center; justify-content:space-between; gap:14px; margin-top:12px; padding:9px 14px; border-radius:22px; background:#d9dcde; font-size:24px; font-weight:950; }}
    .suit-count {{ color:#697279; font-size:16px; }}
    .suit-description {{ margin-top:11px; color:#263038; font-size:20px; line-height:1.42; font-weight:800; }}
.suit-description strong {{ color:#286cd6; background:transparent; padding:0 1px; }}
.term {{ color:var(--term-color,#286cd6); font-weight:950; white-space:nowrap; }}
.term-plain {{ color:inherit; font-weight:950; text-decoration:underline; text-underline-offset:2px; }}
.term-icon {{ width:17px; height:17px; object-fit:contain; vertical-align:-3px; margin:0 2px 0 1px; }}
.rich-style,.vup,.info-note {{ font-weight:950; }}
    .equipment-pieces {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:7px; margin-top:12px; }}
    .equipment-piece {{ min-height:76px; display:grid; grid-template-columns:52px minmax(0,1fr); align-items:center; gap:7px; padding:7px; border:1px solid rgba(23,27,31,.18); background:rgba(255,255,255,.72); }}
    .equipment-piece-icon {{ width:50px; height:50px; display:grid; place-items:center; background:radial-gradient(circle,#fff,#e8ebec); }}
    .equipment-piece-icon img {{ width:48px; height:48px; object-fit:contain; }}
    .equipment-piece-name {{ font-size:14px; line-height:1.14; font-weight:900; overflow-wrap:anywhere; }}
    .equipment-piece-slot {{ margin-top:4px; color:#778087; font-size:12px; font-weight:850; }}
    .piece-summary {{ margin-top:8px; color:#7c848a; font-size:13px; text-align:right; font-weight:850; }}
    .equipment-footer {{ margin-top:auto; padding-top:14px; display:flex; justify-content:space-between; color:#727a80; font-size:13px; font-weight:850; }}
"""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>{css}</style></head><body>
<div class="equipment-card">
  <section class="equipment-left">
    <div class="equipment-name">{esc(view.name)}</div>
    <div class="equipment-group">{esc(view.group_name or view.suit_name or "独立装备")}</div>
    <div class="equipment-meta"><div class="equipment-slot">{esc(view.slot_type)}</div></div>
    <div class="equipment-copy">{description or "暂无装备简介。"}</div>
    <div class="equipment-flavor">{flavor or "暂无档案记录。"}</div>
    <div class="equipment-stage">{equipment_image}</div>
  </section>
  <section class="equipment-right">
    <div class="equipment-section">
      <div class="equipment-section-title">装备属性</div>
      <div class="equipment-level"><div class="equipment-level-number">{esc(view.max_level or "--")}</div><div class="equipment-level-label">LEVEL</div></div>
      <div class="equipment-stats"><div class="equipment-forge-head"><span>锻造等级</span><span>0锻</span><span>1锻</span><span>2锻</span><span>3锻</span></div>{equipment_stat_rows(view)}</div>
    </div>
    <div class="equipment-section">
      <div class="equipment-section-title">装备套组效果</div>
      <div class="suit-bar"><span>{esc(view.suit_name or "无套装")}</span><span class="suit-count">{esc(view.suit_required_count or "--")}件套</span></div>
      <div class="suit-description">{suit_description or "该装备没有套装效果。"}</div>
      {equipment_piece_cards(view, piece_icons)}
    </div>
    <div class="equipment-footer"><span>数据来源 api.fz.wiki</span><span>更新 {esc(view.source_version or "--")}</span></div>
  </section>
</div>
<script>
(function() {{
  const card=document.querySelector('.equipment-card');
  const left=document.querySelector('.equipment-left');
  const right=document.querySelector('.equipment-right');
  if(!card||!left||!right)return;
  const height=Math.max({card_min_height},Math.ceil(left.scrollHeight)+56,Math.ceil(right.scrollHeight)+56);
  card.style.height=height+'px';
  document.documentElement.style.height=height+'px';
  document.body.style.height=height+'px';
}})();
</script>
</body></html>"""


def equipment_stat_rows(view: EquipmentView) -> str:
    if not view.stats:
        return '<div class="equipment-stat"><div class="equipment-stat-main"><span class="equipment-stat-name">暂无属性</span></div>' + '<strong class="equipment-stat-value">--</strong>' * 4 + '</div>'
    rows = []
    for stat in view.stats:
        values = stat.values[:4] or [stat.value]
        while len(values) < 4:
            values.append(values[-1] if values else "--")
        strong_index = max(
            range(len(values)),
            key=lambda index: (_numeric_signal(values[index]), index),
        )
        rendered_values = "".join(
            f'<strong class="equipment-stat-value{" strong" if index == strong_index else ""}">'
            f'{esc(_equipment_signed_value(value))}</strong>'
            for index, value in enumerate(values)
        )
        rows.append(
            '<div class="equipment-stat">'
            '<div class="equipment-stat-main">'
            f'<span class="equipment-stat-icon">{equipment_stat_icon(stat.icon_key, stat.label)}</span>'
            f'<span class="equipment-stat-name">{esc(stat.label)}</span></div>'
            f'{rendered_values}'
            '</div>'
        )
    return "".join(rows)


def _equipment_signed_value(value: str) -> str:
    value = str(value or "--")
    if value not in {"--", "0", "0%"} and not value.startswith(("+", "-")):
        return f"+{value}"
    return value


def equipment_stat_icon(icon_key: str, label: str) -> str:
    normalized = f"{icon_key} {label}".lower()
    if "def" in normalized or "防御" in normalized:
        filename = "icon_attribute_def.png"
        body = '<path d="M12 2.5 20 5.8v5.8c0 5-3.2 8.2-8 10.4-4.8-2.2-8-5.4-8-10.4V5.8L12 2.5Z"/><path d="M12 6.2v11.7"/>'
    elif "will" in normalized or "意志" in normalized:
        filename = "icon_attribute_will.png"
        body = '<path d="M12 3.2c2.2 2.4 3.2 4.4 3.2 6.1 0 1.8-1.4 3.2-3.2 3.2s-3.2-1.4-3.2-3.2c0-1.7 1-3.7 3.2-6.1Z"/><path d="M4 11.5c3.2.2 5.1 1 6 2.4.9 1.5.4 3.4-1.1 4.3-1.5.9-3.4.4-4.3-1.1-.9-1.4-1.1-3.3-.6-5.6Zm16 0c-3.2.2-5.1 1-6 2.4-.9 1.5-.4 3.4 1.1 4.3 1.5.9 3.4.4 4.3-1.1.9-1.4 1.1-3.3.6-5.6Z"/>'
    elif "ultimate" in normalized or "终结技" in normalized or "充能" in normalized:
        filename = "icon_ultimate_sp_gain_scalar.png"
        body = '<path d="M6 3h12M6 21h12M8 4c0 4 1.5 5.3 4 8-2.5 2.7-4 4-4 8m8-16c0 4-1.5 5.3-4 8 2.5 2.7 4 4 4 8"/>'
    else:
        filename = ""
        body = '<path d="m12 3 8 9-8 9-8-9 8-9Z"/><path d="M8 12h8M12 8v8"/>'
    url = _local_image_data_url(ASSET_DIR / "equipment" / filename) if filename else ""
    if url:
        return f'<img class="equipment-stat-icon-img" src="{esc_attr(url)}" alt="">'
    return f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{body}</svg>'


def equipment_piece_cards(view: EquipmentView, piece_icons: dict[str, str]) -> str:
    pieces = view.suit_pieces[:4]
    if not pieces:
        return ""
    cards = []
    for piece in pieces:
        icon_url = piece_icons.get(piece.icon_url, "")
        icon = image(icon_url, piece.name) or "--"
        cards.append(
            '<div class="equipment-piece">'
            f'<div class="equipment-piece-icon">{icon}</div>'
            f'<div><div class="equipment-piece-name">{esc(piece.name)}</div>'
            f'<div class="equipment-piece-slot">{esc(piece.slot_type)}</div></div>'
            '</div>'
        )
    summary = f'<div class="piece-summary">同套装另有 {len(view.suit_pieces)} 件装备</div>'
    return '<div class="equipment-pieces">' + "".join(cards) + "</div>" + summary


def _render_equipment_catalog_html(
    view: EquipmentCatalogView,
    item_icons: dict[str, str],
    card_width: int,
    columns: int,
) -> str:
    rarity_labels = {
        "gold": "金色装备",
        "purple": "紫色装备",
        "blue": "蓝色装备",
        "all": "全部稀有度",
    }
    accent_colors = {
        "gold": "#c88a00",
        "purple": "#7446bc",
        "blue": "#2874b8",
        "all": "#20252a",
    }
    rarity_label = rarity_labels.get(view.rarity_filter, "金色装备")
    accent = accent_colors.get(view.rarity_filter, "#c88a00")
    css = f"""
* {{ box-sizing:border-box; }}
    html,body {{ margin:0; width:{card_width}px; min-height:520px; background:#d9dde0; font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",Arial,sans-serif; color:#171b1f; }}
    .equipment-catalog-card {{ --catalog-accent:{accent}; width:{card_width}px; min-height:520px; padding:28px; overflow:visible; background:linear-gradient(90deg,rgba(29,34,39,.065) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(0deg,rgba(29,34,39,.065) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(135deg,#f8f9f6,#e6eaeb); }}
.catalog-header {{ padding:22px 25px 18px; border:1px solid rgba(23,27,31,.28); background:rgba(249,250,248,.96); }}
.catalog-title-row {{ display:flex; align-items:end; justify-content:space-between; gap:20px; }}
.catalog-title {{ font-size:48px; line-height:1; font-weight:950; letter-spacing:-.035em; }}
.catalog-filter {{ padding:7px 13px; border-left:6px solid var(--catalog-accent); background:#20252a; color:#fff; font-size:16px; font-weight:950; }}
.catalog-subtitle {{ margin-top:8px; color:#667077; font-size:15px; font-weight:850; }}
.catalog-legend {{ display:flex; flex-wrap:wrap; gap:5px 8px; margin-top:14px; padding-top:12px; border-top:3px solid #20252a; }}
.catalog-legend-item {{ height:28px; display:flex; align-items:center; gap:4px; padding:3px 7px; background:#eceeed; color:#4d575e; font-size:11px; font-weight:850; }}
.catalog-legend-icon,.catalog-attr-icon {{ display:block; object-fit:contain; filter:brightness(0) saturate(100%); opacity:.58; }}
.catalog-legend-icon {{ width:20px; height:20px; }}
.catalog-attr-icon {{ width:23px; height:23px; }}
.catalog-legend-fallback,.catalog-attr-fallback {{ display:grid; place-items:center; border:1px solid currentColor; color:#687177; font-weight:950; }}
.catalog-legend-fallback {{ width:20px; height:20px; font-size:9px; }}
.catalog-attr-fallback {{ width:23px; height:23px; font-size:10px; }}
.equipment-catalog-group {{ margin-top:12px; padding:11px; border:1px solid rgba(23,27,31,.25); background:rgba(249,250,248,.95); overflow:visible; }}
.catalog-group-header {{ min-height:36px; display:flex; align-items:center; justify-content:space-between; gap:16px; padding:6px 10px; border-left:8px solid var(--catalog-accent); border-bottom:3px solid #20252a; }}
.catalog-group-name {{ font-size:21px; font-weight:950; }}
.catalog-group-meta {{ color:#727b81; font-size:12px; font-weight:850; }}
.catalog-suit-effect {{ display:grid; grid-template-columns:auto minmax(0,1fr); gap:10px; align-items:start; margin-top:8px; padding:9px 11px; border:1px solid rgba(23,27,31,.17); border-left:6px solid #286cd6; background:#e9edef; }}
.catalog-suit-badge {{ padding:4px 8px; background:#20252a; color:#fff; font-size:11px; font-weight:950; white-space:nowrap; }}
.catalog-suit-copy {{ color:#3f4a51; font-size:12px; line-height:1.5; font-weight:800; }}
.catalog-suit-copy .vup,.catalog-suit-copy .term,.catalog-suit-copy .rich-style,.catalog-suit-copy strong {{ color:#286cd6 !important; font-weight:950; }}
    .catalog-items {{ display:grid; grid-template-columns:repeat({columns},minmax(0,1fr)); gap:7px; margin-top:8px; }}
.equipment-catalog-item {{ position:relative; min-height:104px; display:grid; grid-template-columns:70px minmax(0,1fr); gap:7px; padding:8px; overflow:visible; border:1px solid rgba(23,27,31,.17); background:#f4f6f5; }}
.equipment-catalog-item.rarity-5 {{ border-top:4px solid #c88a00; }}
.equipment-catalog-item.rarity-4 {{ border-top:4px solid #7446bc; }}
.equipment-catalog-item.rarity-3 {{ border-top:4px solid #2874b8; }}
.catalog-item-image {{ width:68px; height:68px; display:grid; place-items:center; align-self:start; background:radial-gradient(circle,#fff,#e5e8e9); }}
.catalog-item-image img {{ width:66px; height:66px; object-fit:contain; }}
.catalog-item-image-fallback {{ color:#9aa1a6; font-size:11px; font-weight:900; }}
.catalog-item-main {{ min-width:0; display:flex; flex-direction:column; }}
.catalog-item-name {{ min-height:32px; font-size:13px; line-height:1.16; font-weight:950; overflow-wrap:anywhere; }}
.catalog-item-meta {{ margin-top:2px; display:flex; align-items:center; justify-content:space-between; gap:5px; color:#778087; font-size:10px; font-weight:850; }}
.catalog-item-slot {{ padding:2px 5px; background:#dfe3e4; color:#465158; }}
.catalog-attributes {{ display:flex; flex-wrap:wrap; gap:3px; margin-top:auto; padding-top:5px; }}
.catalog-attribute {{ width:27px; height:27px; display:grid; place-items:center; background:#e5e8e8; }}
.catalog-footer {{ margin-top:12px; padding:10px 12px; display:flex; justify-content:space-between; border-top:3px solid #20252a; color:#6c757b; font-size:12px; font-weight:850; }}
"""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>{css}</style></head><body>
<div class="equipment-catalog-card">
  <header class="catalog-header">
    <div class="catalog-title-row"><div class="catalog-title">{esc(view.title)}</div><div class="catalog-filter">{esc(rarity_label)}</div></div>
    <div class="catalog-subtitle">{len(view.groups)} 个套组 · {view.total_count} 件装备 · 词条按装备数据顺序显示</div>
    {equipment_catalog_legend(view)}
  </header>
  {equipment_catalog_groups(view, item_icons)}
  <footer class="catalog-footer"><span>数据来源 api.fz.wiki</span><span>更新 {esc(view.source_version or "--")}</span></footer>
</div>
</body></html>"""


def equipment_catalog_groups(view: EquipmentCatalogView, item_icons: dict[str, str]) -> str:
    groups = []
    for group in view.groups:
        slot_counts: dict[str, int] = {}
        for item in group.items:
            slot_counts[item.slot_type] = slot_counts.get(item.slot_type, 0) + 1
        meta = " · ".join(f"{slot}{count}" for slot, count in slot_counts.items())
        items = "".join(equipment_catalog_item(item, item_icons) for item in group.items)
        suit_effect = equipment_catalog_suit_effect(group)
        groups.append(
            '<section class="equipment-catalog-group">'
            '<div class="catalog-group-header">'
            f'<div class="catalog-group-name">{esc(group.name)}</div>'
            f'<div class="catalog-group-meta">共 {len(group.items)} 件 · {esc(meta)}</div>'
            '</div>'
            f'{suit_effect}'
            f'<div class="catalog-items">{items}</div>'
            '</section>'
        )
    return "".join(groups)


def equipment_catalog_suit_effect(group: EquipmentCatalogGroupView) -> str:
    if not group.suit_effect_description:
        return ""
    required = f"{group.suit_required_count}件套" if group.suit_required_count else "套组效果"
    description = highlight_terms(group.suit_effect_description, {}, {}).replace("\n", "<br>")
    return (
        '<div class="catalog-suit-effect">'
        f'<span class="catalog-suit-badge">{esc(required)}</span>'
        f'<div class="catalog-suit-copy">{description}</div>'
        '</div>'
    )


def equipment_catalog_item(
    item: EquipmentCatalogItemView,
    item_icons: dict[str, str],
) -> str:
    icon_url = item_icons.get(item.icon_url, "")
    icon = image(icon_url, item.name)
    if not icon:
        icon = '<span class="catalog-item-image-fallback">暂无图标</span>'
    attributes = "".join(
        '<span class="catalog-attribute" '
        f'title="{esc_attr(attribute.label)} {esc_attr(attribute.value)}">'
        f'{equipment_catalog_attribute_icon(attribute)}</span>'
        for attribute in item.attributes
        if equipment_catalog_attribute_visible(attribute.label)
    )
    return (
        f'<article class="equipment-catalog-item rarity-{item.rarity}">'
        f'<div class="catalog-item-image">{icon}</div>'
        '<div class="catalog-item-main">'
        f'<div class="catalog-item-name">{esc(item.name)}</div>'
        f'<div class="catalog-item-meta"><span class="catalog-item-slot">{esc(item.slot_type)}</span><span>Lv{esc(item.level or "--")}</span></div>'
        f'<div class="catalog-attributes">{attributes}</div>'
        '</div></article>'
    )


def equipment_catalog_legend(view: EquipmentCatalogView) -> str:
    labels = list(dict.fromkeys(
        attribute.label
        for group in view.groups
        for item in group.items
        for attribute in item.attributes
        if equipment_catalog_attribute_visible(attribute.label)
    ))
    items = "".join(
        '<span class="catalog-legend-item">'
        f'{equipment_attribute_icon(label, "catalog-legend-icon", "catalog-legend-fallback")}'
        f'<span>{esc(label)}</span></span>'
        for label in labels
    )
    return f'<div class="catalog-legend">{items}</div>' if items else ""


def equipment_catalog_layout(view: EquipmentCatalogView) -> tuple[int, int]:
    is_specific_group = len(view.groups) == 1 and view.title == view.groups[0].name
    if not is_specific_group:
        return 1900, 8
    columns = 5 if len(view.groups[0].items) >= 5 else 4
    return (1260 if columns == 5 else 1040), columns


def equipment_catalog_attribute_visible(label: str) -> bool:
    normalized = "".join(str(label or "").split())
    return normalized not in {"防御", "防御力"}


def _render_operator_catalog_html(view: OperatorCatalogView, icon_map: dict[str, str]) -> str:
    css = """
* { box-sizing:border-box; }
html,body { margin:0; width:1900px; min-height:680px; background:#d9dde0; font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",Arial,sans-serif; color:#171b1f; }
.operator-catalog-card { width:1900px; min-height:680px; padding:30px; overflow:visible; background:linear-gradient(90deg,rgba(29,34,39,.065) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(0deg,rgba(29,34,39,.065) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(135deg,#f8f9f6,#e6eaeb); }
.gallery-header { padding:24px 28px 20px; border:1px solid rgba(23,27,31,.28); background:rgba(249,250,248,.96); box-shadow:0 12px 32px rgba(23,27,31,.10); }
.gallery-title-row { display:flex; align-items:end; justify-content:space-between; gap:20px; }
.gallery-title { font-size:50px; line-height:1; font-weight:950; letter-spacing:-.035em; }
.gallery-count { padding:8px 14px; border-left:6px solid #ffd000; background:#20252a; color:#fff; font-size:17px; font-weight:950; }
.gallery-subtitle { margin-top:9px; color:#667077; font-size:16px; font-weight:800; }
.operator-element { --element-color:#888; margin-top:18px; padding:14px; border:1px solid rgba(23,27,31,.25); background:rgba(249,250,248,.95); overflow:visible; }
.operator-element-header { min-height:52px; display:flex; align-items:center; justify-content:space-between; gap:16px; padding:8px 14px; border-left:10px solid var(--element-color); border-bottom:3px solid #20252a; background:#f4f6f5; }
.operator-element-name { display:flex; align-items:center; gap:10px; font-size:27px; font-weight:950; }
.operator-element-icon,.operator-profession-icon { width:30px; height:30px; object-fit:contain; filter:brightness(0) saturate(100%); opacity:.62; }
.operator-element-meta { color:#727b81; font-size:14px; font-weight:850; }
.operator-profession-summary { display:flex; flex-wrap:wrap; gap:7px; margin-top:10px; padding:9px 10px; border:1px solid rgba(23,27,31,.16); background:#eef0ef; }
.operator-profession-chip { min-height:31px; display:flex; align-items:center; gap:6px; padding:4px 8px; border-left:4px solid var(--element-color); background:#dfe3e4; color:#465158; font-size:13px; font-weight:900; }
.operator-profession-icon { width:22px; height:22px; }
.operator-grid { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:11px; margin-top:10px; }
.operator-catalog-item { position:relative; min-height:370px; overflow:hidden; border:1px solid rgba(23,27,31,.22); border-bottom:8px solid var(--element-color); background:#f4f6f5; }
.operator-catalog-item.rarity-6 { box-shadow:inset 0 4px 0 #ff3c2e; }
.operator-catalog-item.rarity-5 { box-shadow:inset 0 4px 0 #e59a18; }
.operator-catalog-item.rarity-4 { box-shadow:inset 0 4px 0 #8a56d6; }
.operator-portrait { position:relative; height:272px; overflow:hidden; background:radial-gradient(circle at 50% 42%,#fff 0,#edf0ef 58%,#e1e5e5 100%); }
.operator-portrait img { width:100%; height:100%; object-fit:contain; object-position:center center; display:block; }
.operator-image-fallback { height:100%; display:grid; place-items:center; color:#667177; font-size:15px; font-weight:900; }
.operator-card-body { position:relative; padding:10px 11px 9px; }
.operator-name-row { display:flex; align-items:center; justify-content:space-between; gap:8px; }
.operator-name { min-width:0; font-size:22px; line-height:1.08; font-weight:950; overflow-wrap:anywhere; }
.rarity-chip { flex:0 0 auto; padding:3px 6px; background:#20252a; color:#ffd55a; font-size:12px; font-weight:950; }
.operator-english { margin-top:5px; color:#6e787e; font-size:12px; line-height:1.1; font-weight:800; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.operator-meta-icons { display:flex; align-items:center; gap:6px; margin-top:9px; }
.operator-meta-icon { width:27px; height:27px; padding:2px; object-fit:contain; background:transparent; filter:brightness(0) saturate(100%); opacity:.62; }
.operator-meta-text { margin-left:auto; color:#727b81; font-size:12px; font-weight:850; }
.operator-profession-badge { color:#3e484e; font-size:12px; font-weight:950; }
.gallery-footer { margin-top:18px; padding:12px 14px; display:flex; justify-content:space-between; border-top:3px solid #20252a; color:#6c757b; font-size:13px; font-weight:850; }
"""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>{css}</style></head><body>
<div class="operator-catalog-card">
  <header class="gallery-header">
    <div class="gallery-title-row"><div class="gallery-title">{esc(view.title)}</div><div class="gallery-count">{view.total_count} 位干员</div></div>
    <div class="gallery-subtitle">默认按元素分类，并在元素内按职业划分子类</div>
  </header>
  {_operator_catalog_elements(view, icon_map)}
  <footer class="gallery-footer"><span>数据来源 api.fz.wiki</span><span>更新 {esc(view.source_version or "--")}</span></footer>
</div></body></html>"""


def _operator_catalog_elements(view: OperatorCatalogView, icon_map: dict[str, str]) -> str:
    sections = []
    for element in view.elements:
        count = sum(len(profession.items) for profession in element.professions)
        element_icon = _gallery_icon(icon_map, element.icon_url, "operator-element-icon", element.name)
        profession_chips = "".join(
            '<span class="operator-profession-chip">'
            f'{_gallery_icon(icon_map, profession.icon_url, "operator-profession-icon", profession.name)}'
            f'<span>{esc(profession.name)} · {len(profession.items)}</span></span>'
            for profession in element.professions
        )
        sorted_items = sorted(
            (item for profession in element.professions for item in profession.items),
            key=lambda item: (-item.rarity, item.profession, item.name),
        )
        items = "".join(_operator_catalog_item(item, element.color, icon_map) for item in sorted_items)
        sections.append(
            f'<section class="operator-element" style="--element-color:{esc_attr(normalize_rich_color(element.color))}">'
            '<div class="operator-element-header">'
            f'<div class="operator-element-name">{element_icon}<span>{esc(element.name)}</span></div>'
            f'<div class="operator-element-meta">{count} 位 · {len(element.professions)} 个职业</div>'
            '</div>'
            f'<div class="operator-profession-summary">{profession_chips}</div>'
            f'<div class="operator-grid">{items}</div></section>'
        )
    return "".join(sections)


def _operator_catalog_item(
    item: OperatorCatalogItemView,
    element_color: str,
    icon_map: dict[str, str],
) -> str:
    portrait = _gallery_image(icon_map, item.icon_url, "", item.name)
    if not portrait:
        portrait = '<div class="operator-image-fallback">暂无头像</div>'
    icons = "".join(
        _gallery_icon(icon_map, url, "operator-meta-icon", label)
        for url, label in (
            (item.profession_icon_url, item.profession),
            (item.weapon_type_icon_url, item.weapon_type),
            (item.element_icon_url, item.element),
        )
        if url
    )
    return (
        f'<article class="operator-catalog-item rarity-{item.rarity}" style="--element-color:{esc_attr(normalize_rich_color(element_color))}">'
        f'<div class="operator-portrait">{portrait}</div>'
        '<div class="operator-card-body">'
        f'<div class="operator-name-row"><div class="operator-name">{esc(item.name)}</div><span class="rarity-chip">{item.rarity}★</span></div>'
        f'<div class="operator-english">// {esc(item.english_name or item.operator_id or "--")}</div>'
        f'<div class="operator-meta-icons">{icons}<span class="operator-profession-badge">{esc(item.profession)}</span><span class="operator-meta-text">{esc(item.weapon_type)}</span></div>'
        '</div></article>'
    )


def _render_weapon_catalog_html(view: WeaponCatalogView, icon_map: dict[str, str]) -> str:
    css = """
* { box-sizing:border-box; }
html,body { margin:0; width:1900px; min-height:680px; background:#d9dde0; font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",Arial,sans-serif; color:#171b1f; }
.weapon-catalog-card { width:1900px; min-height:680px; padding:30px; overflow:visible; background:linear-gradient(90deg,rgba(29,34,39,.065) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(0deg,rgba(29,34,39,.065) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(135deg,#f8f9f6,#e6eaeb); }
.gallery-header { padding:24px 28px 20px; border:1px solid rgba(23,27,31,.28); background:rgba(249,250,248,.96); box-shadow:0 12px 32px rgba(23,27,31,.10); }
.gallery-title-row { display:flex; align-items:end; justify-content:space-between; gap:20px; }
.gallery-title { font-size:50px; line-height:1; font-weight:950; letter-spacing:-.035em; }
.gallery-count { padding:8px 14px; border-left:6px solid #ffd000; background:#20252a; color:#fff; font-size:17px; font-weight:950; }
.gallery-subtitle { margin-top:9px; color:#667077; font-size:16px; font-weight:800; }
.weapon-catalog-group { margin-top:18px; padding:14px; border:1px solid rgba(23,27,31,.25); background:rgba(249,250,248,.95); overflow:visible; }
.weapon-group-header { min-height:52px; display:flex; align-items:center; justify-content:space-between; gap:16px; padding:8px 14px; border-left:10px solid #d49400; border-bottom:3px solid #20252a; background:#f4f6f5; }
.weapon-group-name { display:flex; align-items:center; gap:10px; font-size:27px; font-weight:950; }
.weapon-group-icon { width:32px; height:32px; object-fit:contain; filter:brightness(0) saturate(100%); opacity:.62; }
.weapon-group-meta { color:#727b81; font-size:14px; font-weight:850; }
.weapon-grid { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:11px; margin-top:11px; }
.weapon-catalog-item { position:relative; min-height:322px; overflow:hidden; border:1px solid rgba(23,27,31,.22); border-bottom:8px solid #7b8489; background:#f4f6f5; }
.weapon-catalog-item.rarity-6 { border-bottom-color:#ff3c2e; box-shadow:inset 0 4px 0 #ff3c2e; }
.weapon-catalog-item.rarity-5 { border-bottom-color:#e59a18; box-shadow:inset 0 4px 0 #e59a18; }
.weapon-catalog-item.rarity-4 { border-bottom-color:#8a56d6; box-shadow:inset 0 4px 0 #8a56d6; }
.weapon-catalog-item.rarity-3 { border-bottom-color:#2d82c9; box-shadow:inset 0 4px 0 #2d82c9; }
.weapon-image { position:relative; height:216px; padding:12px; overflow:hidden; background:radial-gradient(circle at 50% 45%,#fff 0,#edf0ef 58%,#e1e5e5 100%); }
.weapon-image img { width:100%; height:100%; object-fit:contain; display:block; filter:drop-shadow(0 18px 12px rgba(23,27,31,.28)); }
.weapon-image-fallback { height:100%; display:grid; place-items:center; color:#667177; font-size:15px; font-weight:900; }
.weapon-card-body { padding:10px 11px 9px; }
.weapon-name-row { display:flex; align-items:center; justify-content:space-between; gap:8px; }
.weapon-name { min-width:0; font-size:21px; line-height:1.08; font-weight:950; overflow-wrap:anywhere; }
.rarity-chip { flex:0 0 auto; padding:3px 6px; background:#20252a; color:#ffd55a; font-size:12px; font-weight:950; }
.weapon-english { margin-top:5px; color:#6e787e; font-size:12px; line-height:1.1; font-weight:800; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.weapon-meta { display:flex; align-items:center; gap:6px; margin-top:8px; }
.weapon-atk { margin-left:auto; color:#273138; font-size:13px; font-weight:950; }
.weapon-terms { display:flex; gap:4px; margin-top:7px; min-height:22px; overflow:hidden; }
.weapon-term { max-width:31%; padding:3px 5px; background:#dfe3e4; color:#4d575e; font-size:10px; line-height:1.2; font-weight:850; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.gallery-footer { margin-top:18px; padding:12px 14px; display:flex; justify-content:space-between; border-top:3px solid #20252a; color:#6c757b; font-size:13px; font-weight:850; }
"""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>{css}</style></head><body>
<div class="weapon-catalog-card">
  <header class="gallery-header">
    <div class="gallery-title-row"><div class="gallery-title">{esc(view.title)}</div><div class="gallery-count">{view.total_count} 件武器</div></div>
    <div class="gallery-subtitle">按武器类型分类，展示星级、攻击力与核心词条</div>
  </header>
  {_weapon_catalog_groups(view, icon_map)}
  <footer class="gallery-footer"><span>数据来源 api.fz.wiki</span><span>更新 {esc(view.source_version or "--")}</span></footer>
</div></body></html>"""


def _weapon_catalog_groups(view: WeaponCatalogView, icon_map: dict[str, str]) -> str:
    sections = []
    for group in view.groups:
        group_icon = _gallery_icon(icon_map, group.icon_url, "weapon-group-icon", group.name)
        items = "".join(_weapon_catalog_item(item, icon_map) for item in group.items)
        sections.append(
            '<section class="weapon-catalog-group">'
            '<div class="weapon-group-header">'
            f'<div class="weapon-group-name">{group_icon}<span>{esc(group.name)}</span></div>'
            f'<div class="weapon-group-meta">共 {len(group.items)} 件</div>'
            '</div>'
            f'<div class="weapon-grid">{items}</div></section>'
        )
    return "".join(sections)


def _weapon_catalog_item(item: WeaponCatalogItemView, icon_map: dict[str, str]) -> str:
    weapon_image = _gallery_image(icon_map, item.icon_url, "", item.name)
    if not weapon_image:
        weapon_image = '<div class="weapon-image-fallback">暂无武器图</div>'
    terms = [*item.terms_main[:1], *item.terms_sub[:1], *item.terms_skill[:1]]
    term_html = "".join(f'<span class="weapon-term" title="{esc_attr(term)}">{esc(term)}</span>' for term in terms)
    return (
        f'<article class="weapon-catalog-item rarity-{item.rarity}">'
        f'<div class="weapon-image">{weapon_image}</div>'
        '<div class="weapon-card-body">'
        f'<div class="weapon-name-row"><div class="weapon-name">{esc(item.name)}</div><span class="rarity-chip">{item.rarity}★</span></div>'
        f'<div class="weapon-english">// {esc(item.english_name or item.weapon_id or "--")}</div>'
        f'<div class="weapon-meta"><span class="weapon-atk">ATK {esc(item.max_atk)}</span></div>'
        f'<div class="weapon-terms">{term_html}</div>'
        '</div></article>'
    )


def _gallery_image(icon_map: dict[str, str], source_url: str, class_name: str, alt: str) -> str:
    rendered = image(icon_map.get(source_url, ""), alt)
    if rendered and class_name:
        return rendered.replace("<img ", f'<img class="{class_name}" ', 1)
    return rendered


def _gallery_icon(icon_map: dict[str, str], source_url: str, class_name: str, alt: str) -> str:
    return _gallery_image(icon_map, source_url, class_name, alt)


def equipment_catalog_attribute_icon(attribute: EquipmentCatalogAttributeView) -> str:
    return equipment_attribute_icon(
        attribute.label,
        "catalog-attr-icon",
        "catalog-attr-fallback",
    )


def equipment_attribute_icon(label: str, image_class: str, fallback_class: str) -> str:
    filename = _equipment_attribute_icon_filename(label)
    url = _local_image_data_url(ASSET_DIR / "equipment" / filename) if filename else ""
    if url:
        return f'<img class="{image_class}" src="{esc_attr(url)}" alt="">'
    fallback = clean_attribute_label(label)
    return f'<span class="{fallback_class}">{esc(fallback)}</span>'


def clean_attribute_label(label: str) -> str:
    for token in ("伤害加成", "效率加成", "加成", "效率", "能力"):
        label = label.replace(token, "")
    return label[:2] or "属"


def _equipment_attribute_icon_filename(label: str) -> str:
    exact = {
        "防御力": "icon_attribute_def.png",
        "力量": "icon_attribute_str.png",
        "敏捷": "icon_attribute_agi.png",
        "智识": "icon_attribute_wisd.png",
        "意志": "icon_attribute_will.png",
        "攻击力": "icon_attribute_atk.png",
        "攻击力加成": "icon_attribute_atk.png",
        "生命值": "icon_attribute_maxHp.png",
        "生命值加成": "icon_attribute_maxHp.png",
        "源石技艺强度": "icon_originium_arts.png",
        "终结技充能效率": "icon_ultimate_sp_gain_scalar.png",
        "普通攻击伤害加成": "icon_normal_atk_efficiency.png",
        "治疗效率加成": "icon_heal_output_increase.png",
        "终结技伤害加成": "icon_ultimate_skill_efficiency.png",
        "战技伤害加成": "icon_normal_skill_efficiency.png",
        "物理伤害加成": "icon_physical_damage_increase.png",
        "连携技伤害加成": "icon_combo_skill_efficiency.png",
        "所有技能伤害加成": "icon_normal_skill_efficiency.png",
        "寒冷和电磁伤害加成": "icon_cryst_damage_increase.png",
        "寒冷伤害加成": "icon_cryst_damage_increase.png",
        "暴击率": "icon_attribute_criticalRate.png",
        "暴击伤害": "icon_attribute_criticalDamageIncrease.png",
        "灼热和自然伤害加成": "icon_fire_damage_increase.png",
        "灼热伤害加成": "icon_fire_damage_increase.png",
        "对失衡目标伤害加成": "icon_attr_damage_to_broken_unit_increase.png",
        "全伤害减免": "icon_attribute_def.png",
        "法术伤害加成": "icon_originium_arts.png",
        "物理抗性": "icon_attribute_physicalDamageTakenScalar.png",
        "灼热抗性": "icon_attribute_fireDamageTakenScalar.png",
        "电磁抗性": "icon_attribute_pulseDamageTakenScalar.png",
        "寒冷抗性": "icon_attribute_crystDamageTakenScalar.png",
        "自然抗性": "icon_attribute_natural_damage_taken_scalar.png",
        "超域抗性": "icon_ether_damage_taken_scalar.png",
        "受治疗效率加成": "icon_heal_taken_increase.png",
        "连携技冷却缩减": "icon_comboskill_cooldown_scalar.png",
        "失衡效率加成": "icon_poise_efficiency.png",
        "电磁伤害加成": "icon_pulse_damage_increase.png",
        "自然伤害加成": "icon_natural_damage_increase.png",
        "超域伤害加成": "icon_ether_damage_taken_scalar.png",
    }
    return exact.get(label, "")


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
  {skill_form_descriptions(skill, term_styles, term_icons)}
  {skill_meta(skill)}
  {metric_table(skill)}
</article>"""


def metric_table(skill: SkillView) -> str:
    common_rows, form_groups = skill_metric_row_groups(skill)
    label_width = metric_label_width(skill)
    cells: list[str] = []

    def append_row(name: str, values: list[str], label: str | None = None) -> None:
        strong_index = _strong_value_index(skill, name, values)
        rendered_label = label or skill_metric_label(skill, name)
        metric_class = "metric-name long" if len(rendered_label) > 10 else "metric-name"
        cells.append(f'<div class="{metric_class}">{esc(rendered_label)}</div>')
        for index, value in enumerate(values):
            strong = " strong" if index == strong_index and value != "--" else ""
            cells.append(f'<div class="metric-value{strong}">{esc(value or "--")}</div>')

    for name, values in common_rows:
        append_row(name, values)
    for group_name, note, rows in form_groups:
        cells.append(
            f'<div class="metric-group"><span class="metric-group-name">{esc(group_name)}</span>'
            f'<span class="metric-group-note">{esc(note)}</span></div>'
        )
        for name, label, values in rows:
            append_row(name, values, label)
    if not cells:
        cells = ['<div class="metric-name">效果</div>'] + ['<div class="metric-value">--</div>' for _ in skill_level_labels(skill)]
    return f'<div class="metric-table" style="--metric-label-width:{label_width}px">' + "".join(cells) + "</div>"


def metric_label_width(skill: SkillView) -> int:
    common_rows, form_groups = skill_metric_row_groups(skill)
    labels = [skill_metric_label(skill, name) for name, _ in common_rows]
    labels.extend(label for _, _, rows in form_groups for _, label, _ in rows)
    labels = labels or ["效果"]
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


def skill_form_descriptions(
    skill: SkillView,
    term_styles: dict[str, TermStyleView],
    term_icons: dict[str, str],
) -> str:
    if not skill.form_descriptions:
        return ""
    items = [
        '<div class="skill-form-desc">'
        f'<div class="skill-form-name">{esc(name)}</div>'
        f'<div class="skill-form-text">{highlight_terms(description, term_styles, term_icons)}</div>'
        '</div>'
        for name, description in skill.form_descriptions
    ]
    return '<div class="skill-form-list">' + "".join(items) + "</div>"


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


def skill_metric_row_groups(
    skill: SkillView,
) -> tuple[list[tuple[str, list[str]]], list[tuple[str, str, list[tuple[str, str, list[str]]]]]]:
    rows = skill_metric_rows(skill)
    form_definitions = (
        ("阵诀·智", "智识值 ≥ 意志值"),
        ("阵诀·意", "意志值 > 智识值"),
    )
    grouped: dict[str, list[tuple[str, str, list[str]]]] = {name: [] for name, _ in form_definitions}
    common: list[tuple[str, list[str]]] = []
    for name, values in rows:
        group_name = next((prefix for prefix, _ in form_definitions if name.startswith(prefix)), "")
        if not group_name:
            common.append((name, values))
            continue
        label = name[len(group_name):].strip(" ·：:") or "效果"
        grouped[group_name].append((name, label, values))
    if not all(grouped[name] for name, _ in form_definitions):
        return rows, []
    groups = [
        (name, note, grouped[name])
        for name, note in form_definitions
    ]
    return common, groups


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
        number = max(0, min(5, int(level)))
    except (TypeError, ValueError):
        number = 0
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
