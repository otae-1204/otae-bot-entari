"""图鉴卡渲染：物品、道具、敌人、词条、档案条目，以及四种目录。

版式语言与干员数据详表同源：浅色方格底、细描边面板、黄色（按 kind 换色）
编号段头、等宽字编号，读数像一张技术档案而不是一张网页。

截图参数与干员卡同源（`_prepare_assets` + `_write_temp_html` + `screenshot_web_element`）：
缺图走 `_prepare_assets` 的 missing 记录，从而进 `_IncompletePages`，不写成品缓存。

分页预算与 `rendering.cards.ARCHIVE_PAGE_BUDGETS` 同数值但**不 import 那个常量**：
目录分页是渲染侧的自适应回退，改档案预算不该顺手改图鉴。
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from functools import partial
from time import perf_counter

from loguru import logger

from otae_bot.infrastructure.rendering.browser import BrowserResource, screenshot_timing, screenshot_web_element
from otae_bot.infrastructure.rendering.executor import run_image_render
from otae_bot.infrastructure.rendering.pages import render_pages
from otae_bot.infrastructure.rendering.temp_files import schedule_temp_file_cleanup

from ..cold_start import REMOTE_ASSET_NAMESPACE, note_remote_assets
from ..rendering.cards import (
    _prepare_assets,
    _write_temp_html,
    is_height_limit_error,
    optimize_png_container,
)
from ..richtext import rich_tag_at, stray_rich_tag_len
from .models import (
    TERM_SOURCE_ROLE_NAMES,
    TERM_SOURCE_ROLES,
    ArchiveEntryView,
    CatalogGroup,
    EncyclopediaCatalogView,
    EnemyView,
    ItemView,
    PropView,
    TermSourceView,
    TermView,
    TermValueRow,
)

# 版心不写死：卡片按内容撑开，只留上下限。
CARD_WIDTH = 1240
CARD_MIN_WIDTH = 640
CARD_MAX_HEIGHT = 12000
# 与 ARCHIVE_PAGE_BUDGETS 同数值，故意不 import。
CATALOG_PAGE_BUDGETS: tuple[int, ...] = (24, 18, 12, 6)
CATALOG_COLUMNS = 6
_PAGE_PADDING = 26

_OVERFLOW_SELECTORS = (".ency-section", ".entry", ".catalog-item", ".ability", ".resist-cell")
_CATALOG_OVERFLOW_SELECTORS = (".catalog-group", ".catalog-item")

# 稀有度配色与 `account/detail/draw.py` 的 `_RARITY_COLORS` 同源。
_RARITY_COLORS = {6: "#ff3c2e", 5: "#e59a18", 4: "#8a56d6", 3: "#2d82c9", 2: "#4f9a52", 1: "#8d9598"}
_RARITY_FALLBACK = "#c9cec8"

# (强调色, 英文名, 中文名)
_THEMES: dict[str, tuple[str, str, str]] = {
    "item": ("#f2c500", "ITEM", "物品"),
    "prop": ("#21c6d0", "PROP", "道具"),
    "enemy": ("#ff623d", "ENEMY", "敌人"),
    "term": ("#8a56d6", "TERM", "词条"),
    "archive_entry": ("#286cd6", "ARCHIVE", "档案"),
    "catalog": ("#f2c500", "NOTES", "目录"),
}

# 效果句里的 <@ba.x>…</> 与 <#ba.x>…</> 交给 rendering.richtext 按深度配对，
# 嵌套文案（外层强调里再包一个词条）不会把内层标记漏成纯文本。
# 不走 render_weapon_rich_text：它要 WeaponView，还会把没替换掉的 {key} 画成强调文本。


async def _noted_assets(urls):
    await note_remote_assets(urls, namespace=REMOTE_ASSET_NAMESPACE)
    return await _prepare_assets(urls, inline=False)


async def draw_item_card(view: ItemView) -> bytes:
    prepared = await _noted_assets([view.icon_url])
    return await _render(render_item_html(view, prepared.urls), _OVERFLOW_SELECTORS, "item", prepared.resources)


async def draw_prop_card(view: PropView) -> bytes:
    prepared = await _noted_assets([view.icon_url])
    return await _render(render_prop_html(view, prepared.urls), _OVERFLOW_SELECTORS, "prop", prepared.resources)


async def draw_enemy_card(view: EnemyView) -> bytes:
    prepared = await _noted_assets([view.icon_url])
    return await _render(render_enemy_html(view, prepared.urls), _OVERFLOW_SELECTORS, "enemy", prepared.resources)


async def draw_term_card(view: TermView) -> bytes:
    prepared = await _noted_assets([view.icon_url])
    return await _render(render_term_html(view, prepared.urls), _OVERFLOW_SELECTORS, "term", prepared.resources)


async def draw_archive_entry_card(view: ArchiveEntryView) -> bytes:
    prepared = await _prepare_assets([view.icon_url], inline=False)
    return await _render(
        render_archive_entry_html(view, prepared.urls), _OVERFLOW_SELECTORS, "archive_entry", prepared.resources
    )


async def draw_catalog_card(view: EncyclopediaCatalogView) -> bytes:
    return await _render(
        render_catalog_html(view), _CATALOG_OVERFLOW_SELECTORS, view.kind + "_catalog", None
    )


async def draw_catalog_cards(view: EncyclopediaCatalogView) -> tuple[bytes, ...]:
    """一张放得下就一张；放不下按预算拆页，用户永远不用自己翻页。"""
    try:
        return (await draw_catalog_card(view),)
    except RuntimeError as exc:
        if not is_height_limit_error(exc):
            raise
        last_error = exc
    for item_budget in CATALOG_PAGE_BUDGETS:
        pages = _paginate(view, item_budget)
        try:
            rendered = await render_pages([partial(draw_catalog_card, page) for page in pages])
        except RuntimeError as exc:
            if not is_height_limit_error(exc):
                raise
            last_error = exc
            continue
        logger.info(
            f"[endfield] encyclopedia catalog paginated kind={view.kind} "
            f"pages={len(rendered)} item_budget={item_budget}"
        )
        return tuple(rendered)
    raise last_error


# ------------------------------------------------------------------ HTML 片段

def render_item_html(view: ItemView, urls: Mapping[str, str]) -> str:
    accent, kind_en, kind_cn = _THEMES["item"]
    facts = _spec(
        [
            ("类型", view.type_name),
            ("稀有度", str(view.rarity) if view.rarity else ""),
            ("获取途径", _count_meta(view.obtain_ways, "条")),
        ]
    )
    description = _rich_paragraph(view.description)
    if str(view.deco_description or "").strip():
        description += f"<p class='deco'>{_rich(view.deco_description, {})}</p>"
    sections = [
        ("物品说明", description),
        ("获取途径", _chips(view.obtain_ways, empty=view.no_obtain_hint), _count_meta(view.obtain_ways, "条")),
    ]
    return _document(
        "item-card",
        accent,
        _hero(
            kind_en=kind_en,
            kind_cn=kind_cn,
            title=view.name,
            subtitle=view.item_id,
            icon_url=urls.get(view.icon_url, ""),
            facts=facts,
        ),
        _sections(sections),
        view.revision,
    )


def render_prop_html(view: PropView, urls: Mapping[str, str]) -> str:
    accent, kind_en, kind_cn = _THEMES["prop"]
    facts = _spec(
        [
            ("类型", view.type_name),
            ("稀有度", str(view.rarity) if view.rarity else ""),
            ("分桶", "" if view.bucket == view.type_name else view.bucket),
            ("效果", _count_meta(view.effect_lines)),
        ]
    )
    params = [
        ("持续时间", _seconds(view.duration)),
        ("冷却", _seconds(view.cooldown)),
        ("施放", _seconds(view.cast_time)),
        ("装填", _count(view.charge_count)),
        ("回复上限", _count(view.recover_upper_count)),
        ("持续生效", "是" if view.is_persistent else ""),
    ]
    params = [(label, value) for label, value in params if value]
    effects = "".join(
        f"<div class='effect'><i class='mark'></i><p>{_rich(line, view.term_styles)}</p></div>"
        for line in view.effect_lines
    )
    sections = [
        ("使用效果", effects or "<p class='muted'>暂无效果文案</p>", _count_meta(view.effect_lines)),
        ("施放参数", _spec_table(params), f"{len(params)} 项"),
    ]
    return _document(
        "prop-card",
        accent,
        _hero(
            kind_en=kind_en,
            kind_cn=kind_cn,
            title=view.name,
            subtitle=view.item_id,
            icon_url=urls.get(view.icon_url, ""),
            facts=facts,
        ),
        _sections(sections),
        view.revision,
    )


def render_enemy_html(view: EnemyView, urls: Mapping[str, str]) -> str:
    accent, kind_en, kind_cn = _THEMES["enemy"]
    facts = _spec(
        [
            ("类型", view.display_type_name),
            ("实例", f"{view.variant_count} 个" if view.variant_count else ""),
            ("别名", view.nickname),
            ("韧性", str(view.poise) if view.poise else ""),
            ("对塔攻击", _number_text(view.tower_attack)),
        ]
    )
    if view.stats:
        rows = "".join(
            "<div class='stat-row'>"
            f"<b>Lv{stat.level}</b>"
            f"<span>{_stat_number(stat.hp)}</span>"
            f"<span>{_stat_number(stat.attack)}</span>"
            f"<span>{_stat_number(stat.defense)}</span></div>"
            for stat in view.stats
        )
        stats_body = (
            "<div class='stat-table'><div class='stat-head'>"
            "<span>等级</span><span>生命</span><span>攻击</span><span>防御</span></div>"
            f"{rows}</div>"
        )
        stats_meta = (
            f"{len(view.stats)} / {view.stat_node_count} 个节点"
            if view.stat_node_count > len(view.stats)
            else f"{view.stat_node_count} 个节点"
        )
    else:
        # 没有唯一属性模板时整段省略：抗性段已经说明了「N 种属性模板」。
        stats_body = ""
        stats_meta = ""
    if view.resistances:
        resist = "<div class='resist-strip'>" + "".join(
            f"<div class='resist-cell' style='--el:#{item.color or '8a9296'}'>"
            f"<div class='bar'></div><div class='cell-body'>"
            f"<span>{_escape(item.label)}</span><b>{_percent(item.percent)}</b></div></div>"
            for item in view.resistances
        ) + "</div>"
        resist_meta = f"{len(view.resistances)} 项"
    else:
        resist = (
            f"<p class='muted'>有 {view.resistance_template_count} 种属性模板，"
            "无法给出单一抗性。</p>"
        )
        resist_meta = ""
    abilities = "".join(
        "<div class='ability'>"
        f"<b class='idx'>{index:02d}</b>"
        + (f"<b class='name'>{_escape(item.name)}</b>" if item.name else "<b class='name unnamed'>无名能力</b>")
        + f"<p>{_rich(item.description, {})}</p></div>"
        for index, item in enumerate(view.abilities, start=1)
    )
    sections = [
        *([("等级成长", stats_body, stats_meta)] if view.stats else []),
        ("抗性", resist, resist_meta),
        ("能力", abilities or "<p class='muted'>暂无能力说明</p>", _count_meta(view.abilities)),
        (
            "出现区域",
            _chips(view.distributions, empty=f"{view.distribution_count} 个区域"),
            _count_meta(view.distributions),
        ),
        ("说明", _rich_paragraph(view.description)),
    ]
    return _document(
        "enemy-card",
        accent,
        _hero(
            kind_en=kind_en,
            kind_cn=kind_cn,
            title=view.name or view.template_id,
            subtitle=view.template_id,
            icon_url=urls.get(view.icon_url, ""),
            facts=facts,
        ),
        _sections(sections),
        view.revision,
    )


def render_term_html(view: TermView, urls: Mapping[str, str]) -> str:
    accent, kind_en, kind_cn = _THEMES["term"]
    if view.color:
        accent = view.color if view.color.startswith("#") else f"#{view.color}"
    facts = _spec([("族", view.family), ("关联", _count_meta(view.related, "个"))])
    labels = view.related_names or view.related
    related = "".join(f"<i>{_escape(item)}</i>" for item in labels)
    sections = [("说明", _rich_paragraph(view.summary))]
    if view.values:
        sections.append(("数值", _term_values(view), "Lv1–4"))
    sections.append(
        (
            "关联词条",
            f"<div class='chip-grid'>{related}</div>" if related else "<p class='muted'>无</p>",
            _count_meta(view.related),
        )
    )
    sections.append(
        (
            "来源",
            _term_sources(view),
            f"{view.source_total} 条" if view.source_total else "",
        )
    )
    return _document(
        "term-card",
        accent,
        _hero(
            kind_en=kind_en,
            kind_cn=kind_cn,
            title=view.name,
            subtitle=view.term_id,
            icon_url=urls.get(view.icon_url, ""),
            facts=facts,
        ),
        _sections(sections),
        view.revision,
    )


def render_archive_entry_html(view: ArchiveEntryView, urls: Mapping[str, str]) -> str:
    accent, kind_en, kind_cn = _THEMES["archive_entry"]
    facts = _spec([("版本", view.version)])
    trail = [view.page_name, view.category_name, view.group_name, view.group_sub_name]
    crumbs = "".join(
        f"<i>{_escape(part)}</i>" for part in trail if str(part or "").strip()
    )
    sections = [
        ("目录位置", f"<div class='trail'>{crumbs}</div>" if crumbs else "<p class='muted'>暂无目录信息</p>"),
        ("档案条目", "<p class='muted'>条目正文来自游戏内档案，本卡只列出目录信息。</p>"),
    ]
    return _document(
        "archive-entry-card",
        accent,
        _hero(
            kind_en=kind_en,
            kind_cn=kind_cn,
            title=view.name or view.item_id,
            subtitle=view.item_id,
            icon_url=urls.get(view.icon_url, ""),
            facts=facts,
        ),
        _sections(sections),
        view.version,
    )


def render_catalog_html(view: EncyclopediaCatalogView) -> str:
    accent, kind_en, kind_cn = _THEMES.get(view.kind, _THEMES["catalog"])
    if view.kind == "catalog":
        kind_en, kind_cn = _THEMES["catalog"][1:]
    subtitle = f"{len(view.groups)} 个分组 · 共 {view.total} 条"
    if view.page_count > 1:
        subtitle += f" · 第 {view.page_number} / {view.page_count} 张"
    hero = f"""
    <header class="hero">
      <div class="hero-main">
        <div class="hero-kicker"><i class="kicker-mark"></i>ENDFIELD · NOTES<span class="kicker-tag">{_escape(kind_cn)}</span></div>
        <h1>{_escape(view.title)}</h1>
        <div class="hero-id">{_escape(subtitle)}</div>
      </div>
      <div class="hero-side">
        <div class="count-block"><b>{view.total}</b><span>总条目</span></div>
        {f'<div class="page-block"><b>{view.page_number}</b><span>/ {view.page_count}</span></div>' if view.page_count > 1 else ""}
      </div>
    </header>
    <div class="hero-rule"></div>
    """
    return _document(
        "catalog-card",
        accent,
        hero,
        _catalog_groups(view),
        view.revision,
    )


# ------------------------------------------------------------------ 渲染基础设施

async def _render(
    document: str,
    overflow_selectors: tuple[str, ...],
    kind: str,
    resources: Mapping[str, BrowserResource] | None,
) -> bytes:
    html_path = await _write_temp_html(document)
    started = perf_counter()
    try:
        with screenshot_timing() as timing:
            output = await screenshot_web_element(
                html_path.resolve().as_uri(),
                ".ency-card",
                viewport=(CARD_MIN_WIDTH, 1),
                timeout_ms=25000,
                max_height=CARD_MAX_HEIGHT,
                device_scale_factor=1.25,
                settle_ms=40,
                resources=resources,
                wait_for_images=True,
                strict_max_height=True,
                overflow_selectors=overflow_selectors,
            )
        optimized = await run_image_render(optimize_png_container, output)
        logger.info(
            f"[endfield] draw kind={kind} queue_wait={timing.queue_wait:.3f}s "
            f"total={perf_counter() - started:.3f}s bytes={len(output)}->{len(optimized)}"
        )
        return optimized
    finally:
        schedule_temp_file_cleanup(html_path, delay_seconds=30)


def _paginate(
    view: EncyclopediaCatalogView, item_budget: int
) -> tuple[EncyclopediaCatalogView, ...]:
    """每组填到 item_budget 条为止，一组放不下就续页。"""
    pages: list[list] = []
    current: list = []
    used = 0
    for group in view.groups:
        if not group.items:
            current.append(group)
            continue
        remaining = list(group.items)
        continued = False
        while remaining:
            if used >= item_budget and current:
                pages.append(current)
                current, used = [], 0
            capacity = max(1, item_budget - used)
            chunk, remaining = remaining[:capacity], remaining[capacity:]
            current.append(
                CatalogGroup(
                    key=group.key,
                    name=f"{group.name}（续）" if continued else group.name,
                    count=group.count,
                    items=tuple(chunk),
                )
            )
            used += len(chunk)
            continued = True
    if current or not pages:
        pages.append(current)
    return tuple(
        EncyclopediaCatalogView(
            kind=view.kind,
            title=view.title,
            groups=tuple(page),
            total=view.total,
            source=view.source,
            revision=view.revision,
            page_number=index + 1,
            page_count=len(pages),
        )
        for index, page in enumerate(pages)
    )


# ------------------------------------------------------------------ HTML 工具

_BASE_CSS = """
*{box-sizing:border-box}
html,body{margin:0;background:#e5e7e1;color:#15191d;
  font-family:'Microsoft YaHei','PingFang SC','Noto Sans SC',Arial,sans-serif}
body{padding:0;
  background-image:linear-gradient(90deg,rgba(21,25,29,.045) 1px,transparent 1px),
    linear-gradient(0deg,rgba(21,25,29,.045) 1px,transparent 1px);
  background-size:40px 40px}
.ency-card{width:max-content;min-width:__minw__;max-width:__maxw__;min-height:520px;padding:__pad__}
.sheet{position:relative;overflow:hidden;background:#f7f8f4;
  border:1px solid rgba(23,27,31,.28);box-shadow:0 2px 6px rgba(18,22,26,.07)}
.hero{position:relative;display:flex;align-items:flex-start;justify-content:space-between;gap:26px;
  padding:26px 30px 24px 34px;color:#fff;background:#171b1f}
.hero::before{content:'';position:absolute;left:0;top:0;bottom:0;width:10px;background:var(--accent)}
.hero-main{position:relative;flex:1;min-width:0}
.hero-kicker{display:flex;align-items:center;gap:10px;font-size:11px;font-weight:900;
  letter-spacing:.26em;color:rgba(255,255,255,.6)}
.kicker-mark{width:16px;height:4px;background:var(--accent);display:block;flex:none}
.kicker-tag{margin-left:2px;padding:3px 8px;background:var(--accent);color:#15191d;font-size:10.5px;
  font-weight:900;letter-spacing:.16em}
.hero h1{margin:13px 0 0;font-size:46px;line-height:1.04;font-weight:950;letter-spacing:-.01em;
  color:#fff;overflow-wrap:anywhere}
.hero-id{margin-top:11px;font:700 13px/1.2 Consolas,'SFMono-Regular',monospace;
  color:rgba(255,255,255,.52);letter-spacing:.05em;overflow-wrap:anywhere}
.hero-side{position:relative;display:flex;align-items:flex-start;gap:18px;flex:none}
.hero-figure{display:flex;flex-direction:column;align-items:center;gap:10px}
.hero-icon{width:112px;height:112px;object-fit:contain;padding:9px;background:rgba(255,255,255,.07);
  border:1px solid rgba(255,255,255,.2);box-shadow:inset 0 0 0 4px rgba(255,255,255,.06)}
.hero-icon.placeholder{display:flex;align-items:center;justify-content:center;
  font:950 40px/1 'Arial Black',Arial;color:var(--accent);opacity:.9}
.count-block,.page-block{display:flex;flex-direction:column;align-items:flex-end;gap:4px;
  padding:10px 16px;border:1px solid rgba(255,255,255,.22);background:rgba(255,255,255,.07)}
.count-block b{font:950 40px/1 'Arial Black',Arial;letter-spacing:-.02em;color:#fff}
.count-block span,.page-block span{font-size:10.5px;font-weight:900;letter-spacing:.16em;
  color:rgba(255,255,255,.6)}
.page-block{background:var(--accent);border-color:var(--accent);color:#15191d}
.page-block b{font:950 26px/1 'Arial Black',Arial}
.page-block span{color:rgba(21,25,29,.6)}
.spec{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(180px,1fr);
  gap:8px;padding:16px 30px;background:rgba(255,255,255,.5);
  border-bottom:1px solid rgba(23,27,31,.14)}
.fact{padding:11px 15px;background:linear-gradient(180deg,rgba(255,255,255,.9),rgba(232,235,236,.9));
  border:1px solid rgba(23,27,31,.22);border-left:6px solid var(--accent)}
.fact span{display:block;font-size:12px;font-weight:900;letter-spacing:.12em;color:#4a555c}
.fact b{display:block;margin-top:6px;font-size:24px;font-weight:950;overflow-wrap:anywhere}
.ency-section{padding:19px 30px 0}
.section-head{display:flex;align-items:center;gap:11px;padding-bottom:10px;border-bottom:4px solid #20252a}
.section-head::before{content:'';flex:none;width:9px;height:22px;background:var(--accent)}
.section-head h2{margin:0;font-size:24px;line-height:1;font-weight:950;letter-spacing:.01em}
.section-meta{margin-left:auto;font-size:12px;font-weight:850;letter-spacing:.1em;color:#727b81}
.section-body{padding-top:14px}
p{margin:0;max-width:620px;font-size:15.5px;line-height:1.7;font-weight:700;color:#232a30;overflow-wrap:anywhere}
p+p{margin-top:8px}
p.deco{margin-top:12px;padding:11px 14px;border-left:3px solid #d3d7d1;background:#f5f6f1;
  font-size:15px;color:#4a545b}
.muted{color:#828a85;font-size:15px;font-weight:800}
.chip-grid{display:flex;flex-wrap:wrap;gap:8px}
.chip-grid i{display:inline-flex;align-items:center;padding:7px 12px;background:#e1e4e5;
  border-left:5px solid #20252a;font-style:normal;font-size:13.5px;font-weight:900}
.trail{display:flex;flex-wrap:wrap;align-items:center;gap:10px}
.trail i{font-style:normal;font-size:14px;font-weight:900;padding:7px 12px;background:#e1e4e5;
  border-left:5px solid #20252a}
.trail i+i::before{content:'›';margin-right:10px;color:#a7aeaa;font-weight:900}
.effect{display:flex;gap:12px;padding:13px 16px;background:rgba(255,255,255,.78);
  border:1px solid rgba(23,27,31,.18);border-left:6px solid var(--accent)}
.effect+.effect{margin-top:8px}
.effect .mark{flex:none;width:8px;height:8px;margin-top:8px;background:var(--accent);transform:rotate(45deg)}
.effect p{font-size:15.5px;line-height:1.6}
.effect strong{font-weight:950}
.spec-table{border:1px solid rgba(23,27,31,.22);background:rgba(255,255,255,.78)}
.spec-row{display:flex;justify-content:space-between;align-items:baseline;gap:16px;padding:11px 16px;
  border-bottom:1px solid rgba(23,27,31,.1)}
.spec-row:last-child{border-bottom:none}
.spec-row span{font-size:13px;font-weight:900;letter-spacing:.08em;color:#4a555c}
.spec-row b{font:950 17px/1.2 Consolas,'SFMono-Regular',monospace;color:#171b1f}
.stat-table{border:1px solid rgba(23,27,31,.22);background:rgba(255,255,255,.78)}
.stat-head,.stat-row{display:grid;grid-template-columns:96px 1fr 1fr 1fr;align-items:baseline;
  column-gap:16px;padding:10px 16px}
.stat-head{background:#e4e8ea;border-bottom:1px solid rgba(23,27,31,.22);font-size:12px;font-weight:900;
  letter-spacing:.12em;color:#4a555c}
.stat-head span:not(:first-child){text-align:right}
.stat-row{border-bottom:1px solid rgba(23,27,31,.1)}
.stat-row:last-child{border-bottom:none}
.stat-row b{font:950 15px/1.3 Consolas,'SFMono-Regular',monospace;letter-spacing:.02em;color:#4a555c}
.stat-row span{font:950 19px/1.3 Consolas,'SFMono-Regular',monospace;text-align:right;color:#171b1f}
.resist-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:9px}
.resist-cell{border:1px solid rgba(23,27,31,.22);border-left:6px solid var(--el,#8a9296);
  background:linear-gradient(180deg,rgba(255,255,255,.9),rgba(232,235,236,.9))}
.resist-cell .bar{display:none}
.resist-cell .cell-body{padding:11px 15px 13px}
.resist-cell span{display:block;font-size:12px;font-weight:900;letter-spacing:.12em;color:#4a555c}
.resist-cell b{display:block;margin-top:6px;font:950 28px/1 'Arial Black',Arial;letter-spacing:-.01em}
.ability{position:relative;padding:13px 17px 13px 60px;background:rgba(255,255,255,.78);
  border:1px solid rgba(23,27,31,.18);border-left:6px solid #20252a}
.ability+.ability{margin-top:8px}
.ability b.idx{position:absolute;left:0;top:0;bottom:0;width:44px;display:flex;align-items:center;
  justify-content:center;border-right:1px solid rgba(23,27,31,.12);background:#e9ecec;
  font:900 14px/1 Consolas,'SFMono-Regular',monospace;color:#8b938e}
.ability b.name{display:block;font-size:17px;font-weight:950;overflow-wrap:anywhere}
.ability b.name.unnamed{color:#9aa19c;font-weight:850}
.ability p{margin-top:6px;font-size:14.5px;line-height:1.6;color:#3e484f}
.entry{display:grid;grid-template-columns:32px 1fr auto;align-items:center;gap:12px;padding:11px 14px;
  background:rgba(255,255,255,.78);border:1px solid rgba(23,27,31,.16);border-left:5px solid #20252a}
.entry+.entry{margin-top:7px}
.entry b.idx{font:900 12px/1 Consolas,'SFMono-Regular',monospace;color:#9aa19c}
.entry b{font-size:15.5px;font-weight:900;overflow-wrap:anywhere}
.entry span{font-size:13px;font-weight:850;color:#6f7773}
.entry-more{margin-top:8px;padding:11px 14px;background:rgba(23,27,31,.055);text-align:center;
  font-size:13px;font-weight:850;color:#6f7773}
/* 词条卡：说明铺满卡宽（620px 的阅读宽度会让尾句掉成孤字），数值区与来源分组 */
.term-card .section-body p{max-width:none}
.val-table{border:1px solid rgba(23,27,31,.22);background:rgba(255,255,255,.78)}
.val-head,.val-row{display:grid;grid-template-columns:minmax(196px,1fr) repeat(4,96px);
  align-items:center;padding:11px 16px}
.val-head{background:#20252a;color:#e8ebe6;font-size:12px;font-weight:900;letter-spacing:.12em}
.val-head b{text-align:center;font-weight:900}
.val-row{border-bottom:1px solid rgba(23,27,31,.1)}
.val-row:last-child{border-bottom:none}
.val-row:nth-child(even){background:rgba(23,27,31,.025)}
.val-label{font-size:14.5px;font-weight:900;color:#232a30;overflow-wrap:anywhere}
.val-label em{margin-left:8px;font-style:normal;font-size:11.5px;font-weight:850;
  letter-spacing:.06em;color:#8d948f}
.val-row b{text-align:center;font:950 17px/1.2 Consolas,'SFMono-Regular',monospace;
  color:#171b1f;font-variant-numeric:tabular-nums}
.val-row b em{margin-left:2px;font-style:normal;font-size:12px;font-weight:850;color:#6f7773}
.val-row-fixed b{grid-column:2 / span 4}
.val-note,.src-note{margin-top:10px;font-size:12.5px;line-height:1.6;font-weight:850;color:#727b81}
.src-group+.src-group{margin-top:13px}
.src-head{display:flex;align-items:center;gap:9px;margin-bottom:7px}
.src-tag{padding:4px 10px;color:#fff;font-size:12px;font-weight:900;letter-spacing:.08em}
.src-count{font-size:12px;font-weight:850;letter-spacing:.08em;color:#727b81}
.src-apply .src-tag{background:#b83f24}
.src-enhance .src-tag{background:#1f7a6b}
.src-trigger .src-tag{background:#5d6873}
.src-consume .src-tag{background:#9a8530}
.src-apply .entry{border-left-color:#b83f24}
.src-enhance .entry{border-left-color:#1f7a6b}
.src-trigger .entry{border-left-color:#5d6873}
.src-consume .entry{border-left-color:#9a8530}
.catalog-group{display:block}
.group-head{display:flex;align-items:center;gap:11px;padding-bottom:10px;border-bottom:4px solid #20252a}
.group-head::before{content:'';flex:none;width:9px;height:20px;background:var(--accent)}
.group-head h3{margin:0;font-size:21px;font-weight:950;overflow-wrap:anywhere}
.group-meta{margin-left:auto;font-size:12px;font-weight:850;letter-spacing:.1em;color:#727b81}
.group-count{display:flex;align-items:baseline;gap:5px}
.group-count b{font:950 22px/1 'Arial Black',Arial}
.group-count span{font-size:11.5px;font-weight:900;letter-spacing:.12em;color:#727b81}
.group-bar{height:4px;margin:12px 0 12px;background:#e4e8ea}
.group-bar i{display:block;height:100%;background:var(--accent)}
.hint{padding:13px 16px;background:rgba(23,27,31,.055);font-size:14px;font-weight:850;color:#5d6870}
.catalog-grid{display:grid;grid-template-columns:repeat(var(--cols,6),minmax(176px,1fr));gap:9px}
.catalog-item{position:relative;min-height:68px;padding:11px 13px 11px 15px;background:rgba(255,255,255,.78);
  border:1px solid rgba(23,27,31,.18)}
.catalog-item::before{content:'';position:absolute;left:0;top:0;bottom:0;width:4px;
  background:var(--rarity,#c9cec8)}
.catalog-item .num{position:absolute;right:9px;top:8px;font:900 10.5px/1 Consolas,'SFMono-Regular',monospace;
  color:#b6bdb7;font-style:normal}
.catalog-item b{display:block;padding-right:20px;font-size:15px;font-weight:950;line-height:1.28;
  overflow-wrap:anywhere}
.catalog-item span{display:block;margin-top:7px;font-size:12px;font-weight:850;color:#7c8480;
  overflow-wrap:anywhere}
footer{display:flex;justify-content:space-between;gap:20px;margin:24px 30px 24px;padding:13px 20px;
  background:#20252a;font-size:11px;font-weight:900;letter-spacing:.08em;color:#aeb6ba}
footer .mono{font-family:Consolas,'SFMono-Regular',monospace;letter-spacing:.04em}
"""


def _css() -> str:
    return (
        _BASE_CSS.replace("__minw__", f"{CARD_MIN_WIDTH}px")
        .replace("__maxw__", f"{CARD_WIDTH}px")
        .replace("__pad__", f"{_PAGE_PADDING}px")
    )


def _document(klass: str, accent: str, hero: str, body: str, revision: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>{_css()}</style></head>
<body><main class="ency-card {klass}" style="--accent:{_attr(accent)}"><div class="sheet">{hero}{body}
<footer><span>ENDFIELD · NOTES · 数据来源 AkeData</span><span class="mono">{_escape(str(revision).split("|", 1)[0])}</span></footer>
</div></main></body></html>"""


def _hero(
    *,
    kind_en: str,
    kind_cn: str,
    title: str,
    subtitle: str,
    icon_url: str,
    facts: str = "",
) -> str:
    icon = (
        f'<img class="hero-icon" src="{_attr(icon_url)}" alt="">'
        if icon_url
        else f'<div class="hero-icon placeholder">{_escape(kind_cn[:1])}</div>'
    )
    return f"""
    <header class="hero">
      <div class="hero-main">
        <div class="hero-kicker"><i class="kicker-mark"></i>ENDFIELD · NOTES<span class="kicker-tag">{_escape(kind_cn)}</span></div>
        <h1>{_escape(title)}</h1>
        <div class="hero-id">{_escape(subtitle)}</div>
      </div>
      <div class="hero-side">
        <div class="hero-figure">{icon}</div>
      </div>
    </header>
{facts}"""


def _spec(facts: Sequence[tuple[str, str]]) -> str:
    cells = "".join(
        f"<div class='fact'><span>{_escape(label)}</span><b>{_escape(value)}</b></div>"
        for label, value in facts
        if value
    )
    return f"<div class='spec'>{cells}</div>" if cells else ""


def _spec_table(rows: Sequence[tuple[str, str]]) -> str:
    if not rows:
        return "<p class='muted'>暂无参数</p>"
    cells = "".join(
        f"<div class='spec-row'><span>{_escape(label)}</span><b>{_escape(value)}</b></div>"
        for label, value in rows
    )
    return f"<div class='spec-table'>{cells}</div>"


def _section(title: str, body: str, meta: str = "") -> str:
    meta_html = f'<span class="section-meta">{_escape(meta)}</span>' if meta else ""
    return (
        '<section class="ency-section">'
        f'<div class="section-head"><h2>{_escape(title)}</h2>{meta_html}</div>'
        f'<div class="section-body">{body}</div></section>'
    )


def _sections(items: Sequence[tuple]) -> str:
    return "".join(
        _section(item[0], item[1], item[2] if len(item) > 2 else "") for item in items
    )


def _term_values(view: TermView) -> str:
    """数值区：四档异常等级一列一格；固定值（持续时间、爆发倍率）跨列居中。"""
    head = "<div class='val-head'><span>配置</span>" + "".join(
        f"<b>Lv{level}</b>" for level in range(1, 5)
    ) + "</div>"
    rows = "".join(_term_value_row(row) for row in view.values)
    note = f"<p class='val-note'>{_escape(view.values_note)}</p>" if view.values_note else ""
    return f"<div class='val-table'>{head}{rows}</div>{note}"


def _term_value_row(row: TermValueRow) -> str:
    calc = f"<em>{_escape(row.calc)}</em>" if row.calc else ""
    label = f"<span class='val-label'>{_escape(row.label)}{calc}</span>"
    if len(row.values) == 1:
        return f"<div class='val-row val-row-fixed'>{label}<b>{_val_cell(row.values[0])}</b></div>"
    cells = "".join(f"<b>{_val_cell(value)}</b>" for value in row.values)
    return f"<div class='val-row'>{label}{cells}</div>"


def _val_cell(value: str) -> str:
    """数值与单位分开：`12%` / `12 秒` 里的单位用小字，数字保持等宽对齐。"""
    match = re.match(r"^(-?[0-9.]+)\s*(.*)$", str(value))
    if match is None:
        return _escape(value)
    number, unit = match.groups()
    unit_html = f"<em>{_escape(unit)}</em>" if unit else ""
    return f"{_escape(number)}{unit_html}"


def _term_sources(view: TermView) -> str:
    """来源按「施加 / 增强 / 触发 / 消耗」分组；只被提及的引用不进来。"""
    if not view.sources:
        body = "<p class='muted'>暂无直接来源</p>"
        if view.mention_total:
            body += (
                f"<p class='src-note'>另有 {view.mention_total} 处技能文本提及该词条，"
                "但未施加、增强或消耗它</p>"
            )
        return body
    blocks: list[str] = []
    for role in TERM_SOURCE_ROLES:
        group = [item for item in view.sources if item.role == role]
        if not group:
            continue
        rows = "".join(_source_row(index, item) for index, item in enumerate(group, start=1))
        blocks.append(
            f"<div class='src-group src-{role}'>"
            f"<div class='src-head'><span class='src-tag'>{_escape(TERM_SOURCE_ROLE_NAMES[role])}</span>"
            f"<span class='src-count'>{len(group)} 条</span></div>{rows}</div>"
        )
    rest = view.source_total - len(view.sources)
    if rest > 0:
        blocks.append(f"<div class='entry-more'>还有 {rest} 条来源</div>")
    return "".join(blocks)


def _source_row(index: int, item: TermSourceView) -> str:
    detail = " · ".join(part for part in (item.kind, item.skill) if part)
    return (
        "<div class='entry'>"
        f"<b class='idx'>{index:02d}</b>"
        f"<b>{_escape(item.name)}</b><span>{_escape(detail)}</span></div>"
    )


def _catalog_groups(view: EncyclopediaCatalogView) -> str:
    """一组一段；多组才画占比条，整页没有图标时不逐组重复同一句提示。"""
    total = max(1, view.total)
    show_share = len(view.groups) > 1
    # 列数跟着最大组走，条目少的目录不会被 6 列撑到满宽。
    columns = min(CATALOG_COLUMNS, max((len(g.items) for g in view.groups), default=0) or 1)
    counter = 0
    blocks: list[str] = []
    for group in view.groups:
        share = round(group.count * 100 / total)
        body = ""
        if group.items:
            tiles = []
            for item in group.items:
                counter += 1
                tiles.append(_catalog_item(item, counter))
            body = (
                f'<div class="catalog-grid" style="--cols:{columns}">'
                f"{''.join(tiles)}</div>"
            )
        meta = f'<span class="group-meta">{share}%</span>' if show_share else ""
        bar = f'<div class="group-bar"><i style="width:{share}%"></i></div>' if show_share else ""
        blocks.append(
            '<section class="ency-section catalog-group">'
            f'<div class="group-head"><h3>{_escape(group.name)}</h3>{meta}'
            f'<div class="group-count"><b>{group.count}</b><span>条</span></div></div>'
            f"{bar}{f'<div class=\"section-body\">{body}</div>' if body else ''}</section>"
        )
    if not any(group.items for group in view.groups):
        blocks.append(
            "<section class='ency-section'><div class='hint'>"
            "发送分组名可查看该类型的图标页。</div></section>"
        )
    return "".join(blocks) or "<section class='ency-section'><p class='muted'>暂无条目</p></section>"


def _paragraph(text: str) -> str:
    return f"<p>{_multiline(text)}</p>" if str(text or "").strip() else "<p class='muted'>暂无</p>"


def _rich_paragraph(text: str) -> str:
    """正文同样过一遍富文本：把 <@ba.x>…</> 收成强调文本，不让标记直接露在卡上。"""
    return f"<p>{_rich(text, {})}</p>" if str(text or "").strip() else "<p class='muted'>暂无</p>"


def _chips(values: Sequence[str], empty: str = "") -> str:
    cleaned = [str(value).strip() for value in values if str(value or "").strip()]
    if not cleaned:
        return f"<p class='muted'>{_escape(empty)}</p>" if empty else "<p class='muted'>暂无</p>"
    return "<div class='chip-grid'>" + "".join(f"<i>{_escape(value)}</i>" for value in cleaned) + "</div>"


def _catalog_item(item, number: int) -> str:
    color = _RARITY_COLORS.get(item.rarity, _RARITY_FALLBACK) if item.rarity else _RARITY_FALLBACK
    subtitle = f"<span>{_escape(item.subtitle)}</span>" if item.subtitle else ""
    return (
        f'<div class="catalog-item" style="--rarity:{color}">'
        f'<i class="num">{number:02d}</i><b>{_escape(item.name)}</b>{subtitle}</div>'
    )


def _rich(line: str, styles: Mapping) -> str:
    """效果句里的 <@>、<#> 着色；颜色来自本卡视图的 term style。

    闭合按深度配对（文案允许嵌套），配不上的标记直接吞掉，不落到卡上。
    """

    def render(text: str) -> str:
        parts: list[str] = []
        index = 0
        while index < len(text):
            tag = rich_tag_at(text, index)
            if tag is not None:
                term_id, body, end_index = tag[1], tag[2], tag[3]
                style = styles.get(term_id) if styles else None
                color = getattr(style, "color", "") if style else ""
                if color and not color.startswith("#"):
                    color = f"#{color}"
                style_attr = f' style="color:{_attr(color)}"' if color else ""
                parts.append(f"<strong{style_attr}>{render(body)}</strong>")
                index = end_index
                continue
            stray = stray_rich_tag_len(text, index)
            if stray:
                index += stray
                continue
            parts.append(_escape(text[index]))
            index += 1
        return "".join(parts)

    return render(line).replace("\n", "<br>")


def _count_meta(values: Sequence, unit: str = "项") -> str:
    return f"{len(values)} {unit}" if values else ""


def _seconds(value: float | None) -> str:
    return f"{value:g} 秒" if value else ""


def _count(value: int | None) -> str:
    return f"{value}" if value else ""


def _percent(value: float | None) -> str:
    return f"{value:g}%" if value is not None else "—"


def _stat_number(value: int | None) -> str:
    return f"{value:,}" if value is not None else "—"


def _number_text(value: float | None) -> str:
    return f"{value:g}" if value else ""


def _multiline(value: str) -> str:
    return _escape(value).replace("\n", "<br>")


def _escape(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _attr(value: object) -> str:
    return html.escape(str(value or ""), quote=True)
