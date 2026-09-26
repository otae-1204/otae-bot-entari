"""图鉴卡渲染：物品、道具、敌人、词条、档案条目，以及四种目录。

截图参数与干员卡同源（`_prepare_assets` + `_write_temp_html` + `screenshot_web_element`）：
缺图走 `_prepare_assets` 的 missing 记录，从而进 `_IncompletePages`，不写成品缓存。

分页预算与 `rendering.cards.ARCHIVE_PAGE_BUDGETS` 同数值但**不 import 那个常量**：
目录分页是渲染侧的自适应回退，改档案预算不该顺手改图鉴。
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from time import perf_counter

from loguru import logger

from otae_bot.infrastructure.rendering.browser import BrowserResource, screenshot_web_element
from otae_bot.infrastructure.rendering.executor import run_image_render
from otae_bot.infrastructure.rendering.temp_files import schedule_temp_file_cleanup

from ..rendering.cards import (
    _prepare_assets,
    _write_temp_html,
    is_height_limit_error,
    optimize_png_container,
)
from .models import (
    ArchiveEntryView,
    CatalogGroup,
    EncyclopediaCatalogView,
    EnemyView,
    ItemView,
    PropView,
    TermView,
)

CARD_WIDTH = 1240
CARD_MAX_HEIGHT = 12000
# 与 ARCHIVE_PAGE_BUDGETS 同数值，故意不 import。
CATALOG_PAGE_BUDGETS: tuple[int, ...] = (24, 18, 12, 6)
CATALOG_COLUMNS = 6

_OVERFLOW_SELECTORS = (".ency-section", ".entry", ".catalog-item", ".ability", ".resist-cell")
_CATALOG_OVERFLOW_SELECTORS = (".catalog-group", ".catalog-item")

# 效果句里的 <@ba.x>…</> 与 <#ba.x>…</>；与 render_weapon_rich_text 同一形状。
# 不直接调那个函数：它要 WeaponView，还会把没替换掉的 {key} 画成强调文本。
_LINK_RE = re.compile(r"&lt;[@#]([A-Za-z0-9_.-]+)&gt;(.*?)&lt;/&gt;", re.DOTALL)
_STYLE_RE = re.compile(r"&lt;#([A-Za-z0-9_.-]+)&gt;(.*?)&lt;/&gt;", re.DOTALL)


async def draw_item_card(view: ItemView) -> bytes:
    prepared = await _prepare_assets([view.icon_url], inline=False)
    return await _render(render_item_html(view, prepared.urls), _OVERFLOW_SELECTORS, "item", prepared.resources)


async def draw_prop_card(view: PropView) -> bytes:
    prepared = await _prepare_assets([view.icon_url], inline=False)
    return await _render(render_prop_html(view, prepared.urls), _OVERFLOW_SELECTORS, "prop", prepared.resources)


async def draw_enemy_card(view: EnemyView) -> bytes:
    prepared = await _prepare_assets([view.icon_url], inline=False)
    return await _render(render_enemy_html(view, prepared.urls), _OVERFLOW_SELECTORS, "enemy", prepared.resources)


async def draw_term_card(view: TermView) -> bytes:
    prepared = await _prepare_assets([view.icon_url], inline=False)
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
            rendered = [await draw_catalog_card(page) for page in pages]
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
    facts = [
        ("类型", view.type_name),
        ("稀有度", str(view.rarity) if view.rarity else ""),
    ]
    sections = [
        _section("物品说明", _paragraph(view.description)),
        _section("获取途径", _chips(view.obtain_ways, empty=view.no_obtain_hint)),
    ]
    return _document(
        "item-card",
        _hero("ITEM", view.name, view.item_id, urls.get(view.icon_url, ""), _fact_strip(facts)),
        "".join(sections),
        view.revision,
    )


def render_prop_html(view: PropView, urls: Mapping[str, str]) -> str:
    facts = [
        ("类型", view.type_name),
        ("分桶", view.bucket),
        ("持续时间", _seconds(view.duration)),
        ("冷却", _seconds(view.cooldown)),
        ("施放", _seconds(view.cast_time)),
        ("装填", _count(view.charge_count)),
        ("回复上限", _count(view.recover_upper_count)),
    ]
    if view.is_persistent:
        facts.append(("持续生效", "是"))
    body = "".join(
        f"<p class='effect'>{_rich(line, view.term_styles)}</p>" for line in view.effect_lines
    )
    return _document(
        "prop-card",
        _hero("PROP", view.name, view.item_id, urls.get(view.icon_url, ""), _fact_strip(facts)),
        _section("使用效果", body or "<p class='muted'>暂无效果文案</p>"),
        view.revision,
    )


def render_enemy_html(view: EnemyView, urls: Mapping[str, str]) -> str:
    facts = [
        ("类型", view.display_type_name),
        ("实例", f"{view.variant_count} 个" if view.variant_count else ""),
    ]
    if view.resistances:
        resist = "<div class='resist-strip'>" + "".join(
            "<div class='resist-cell' style='--el:#{color}'>".format(color=item.color or "8a9296")
            + f"<span>{_escape(item.label)}</span><b>{_percent(item.percent)}</b></div>"
            for item in view.resistances
        ) + "</div>"
    else:
        resist = (
            f"<p class='muted'>有 {view.resistance_template_count} 种属性模板，"
            "无法给出单一抗性。</p>"
        )
    abilities = "".join(
        "<div class='ability'>"
        + (f"<b>{_escape(item.name)}</b>" if item.name else "<b class='unnamed'>无名能力</b>")
        + f"<p>{_multiline(item.description)}</p></div>"
        for item in view.abilities
    )
    sections = [
        _section("抗性", resist),
        _section("能力", abilities or "<p class='muted'>暂无能力说明</p>"),
        _section("出现区域", _chips(view.distributions, empty=f"{view.distribution_count} 个区域")),
        _section("说明", _paragraph(view.description)),
    ]
    return _document(
        "enemy-card",
        _hero("ENEMY", view.name or view.template_id, view.template_id, urls.get(view.icon_url, ""), _fact_strip(facts)),
        "".join(sections),
        view.revision,
    )


def render_term_html(view: TermView, urls: Mapping[str, str]) -> str:
    facts = [("族", view.family)]
    related = "".join(f"<i>{_escape(item)}</i>" for item in view.related)
    sources = "".join(
        "<div class='source'><b>{name}</b><span>{skill}</span></div>".format(
            name=_escape(item.name), skill=_escape(item.skill)
        )
        for item in view.sources
    )
    if view.source_total > len(view.sources):
        sources += f"<div class='source more'>还有 {view.source_total - len(view.sources)} 条来源</div>"
    sections = [
        _section("说明", _paragraph(view.summary)),
        _section("关联词条", f"<div class='chip-grid'>{related}</div>" if related else "<p class='muted'>无</p>"),
        _section("来源", sources or "<p class='muted'>暂无来源</p>"),
    ]
    return _document(
        "term-card",
        _hero("TERM", view.name, view.term_id, urls.get(view.icon_url, ""), _fact_strip(facts), accent=view.color),
        "".join(sections),
        view.revision,
    )


def render_archive_entry_html(view: ArchiveEntryView, urls: Mapping[str, str]) -> str:
    facts = [
        ("页签", view.page_name),
        ("分类", view.category_name),
        ("组", view.group_name),
        ("子组", view.group_sub_name),
        ("版本", view.version),
    ]
    return _document(
        "archive-entry-card",
        _hero("ARCHIVE", view.name or view.item_id, view.item_id, urls.get(view.icon_url, ""), _fact_strip(facts)),
        _section("档案条目", "<p class='muted'>条目正文来自游戏内档案，本卡只列出目录信息。</p>"),
        view.version,
    )


def render_catalog_html(view: EncyclopediaCatalogView) -> str:
    groups = "".join(
        "<section class='catalog-group'>"
        f"<div class='section-head'><h2>{_escape(group.name)}</h2><span>{group.count} 条</span></div>"
        + (
            f"<div class='catalog-grid'>{''.join(_catalog_item(item) for item in group.items)}</div>"
            if group.items
            else "<p class='muted'>发送该类型名可查看图标页。</p>"
        )
        + "</section>"
        for group in view.groups
    )
    subtitle = f"{len(view.groups)} 个分组 · 共 {view.total} 条"
    if view.page_count > 1:
        subtitle += f" · 第 {view.page_number} / {view.page_count} 张"
    return _document(
        "catalog-card",
        f"""
        <header class="hero"><div class="hero-copy"><small>ENDFIELD / CODEX</small>
        <h1>{_escape(view.title)}</h1><p>{_escape(subtitle)}</p></div>
        <div class="hero-counts"><div class="hero-count"><b>{view.total}</b><span>总条目</span></div></div></header>
        """,
        groups or "<p class='muted'>暂无条目</p>",
        view.revision,
    )


# ------------------------------------------------------------------ 渲染基础设施

async def _render(
    document: str,
    overflow_selectors: tuple[str, ...],
    kind: str,
    resources: Mapping[str, BrowserResource] | None,
) -> bytes:
    html_path = _write_temp_html(document)
    started = perf_counter()
    try:
        output = await screenshot_web_element(
            html_path.resolve().as_uri(),
            ".ency-card",
            viewport=(CARD_WIDTH, 1),
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
            f"[endfield] draw kind={kind} total={perf_counter() - started:.3f}s "
            f"bytes={len(output)}->{len(optimized)}"
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

def _document(klass: str, hero: str, body: str, revision: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>
    *{{box-sizing:border-box}}html,body{{margin:0;width:{CARD_WIDTH}px;background:#dfe3e3;color:#171b1f;font-family:'Microsoft YaHei','PingFang SC','Noto Sans SC',Arial,sans-serif}}
    .ency-card{{width:{CARD_WIDTH}px;min-height:520px;padding:28px;background:linear-gradient(90deg,rgba(23,27,31,.06) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(0deg,rgba(23,27,31,.06) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(135deg,#f8f9f6,#e4e8e9)}}
    .hero{{display:flex;justify-content:space-between;align-items:center;gap:24px;min-height:140px;padding:22px 28px;border:1px solid #171b1f;border-bottom:9px solid var(--accent,#f2c500);background:#20262a;color:#fff}}
    .hero-copy{{min-width:0}}.hero small{{color:var(--accent,#f2c500);font-size:13px;font-weight:900;letter-spacing:.22em}}
    .hero h1{{margin:9px 0 0;font-size:46px;line-height:1.05;font-weight:950;overflow-wrap:anywhere}}
    .hero p{{margin:9px 0 0;color:#cfd5d8;font-size:16px;font-weight:850;overflow-wrap:anywhere}}
    .hero-side{{flex:none;display:flex;align-items:center;gap:18px}}
    .hero-icon{{width:104px;height:104px;object-fit:contain;border:1px solid rgba(242,197,0,.5);background:rgba(255,255,255,.06);padding:6px}}
    .hero-counts{{flex:none;display:flex;gap:26px;text-align:right}}.hero-count b{{display:block;font-size:48px;line-height:1;font-weight:950}}
    .hero-count span{{color:#cfd5d8;font-size:14px;font-weight:850}}
    .ency-section,.catalog-group{{margin-top:16px;padding:16px;border:1px solid #9aa2a5;background:rgba(248,249,247,.96)}}
    .section-head{{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;margin-bottom:11px;padding-bottom:8px;border-bottom:4px solid #20262a}}
    .section-head h2{{margin:0;font-size:24px}}.section-head span{{color:#697277;font-size:14px;font-weight:850}}
    p{{margin:0;font-size:17px;line-height:1.55;font-weight:700;overflow-wrap:anywhere}}
    p + p{{margin-top:7px}}.muted{{color:#7b8288;font-size:15px;font-weight:750}}
    .effect{{padding:11px 14px;border-left:6px solid var(--accent,#f2c500);background:#fff}}
    .effect + .effect{{margin-top:8px}}
    .fact-strip{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:9px;margin-top:14px}}
    .fact{{padding:10px 13px;border:1px solid #abb2b5;border-left:5px solid #20262a;background:rgba(255,255,255,.9)}}
    .fact span{{display:block;color:#6b7479;font-size:12px;font-weight:900}}.fact b{{display:block;margin-top:5px;font-size:19px;overflow-wrap:anywhere}}
    .chip-grid{{display:flex;flex-wrap:wrap;gap:7px}}
    .chip-grid i{{padding:7px 11px;border:1px solid #a7afb2;background:#eef0ef;font-style:normal;font-size:14px;font-weight:850}}
    .resist-strip{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}}
    .resist-cell{{padding:9px 11px;border:1px solid #c4c9cb;border-top:5px solid var(--el,#8a9296);background:#f2f4f4}}
    .resist-cell span{{display:block;color:#4f5a60;font-size:13px;font-weight:900}}
    .resist-cell b{{display:block;margin-top:4px;font-size:17px;font-weight:950}}
    .ability{{padding:12px 14px;border:1px solid #abb2b5;border-left:5px solid #20262a;background:#fff}}
    .ability + .ability{{margin-top:8px}}.ability b{{display:block;font-size:18px;font-weight:900;overflow-wrap:anywhere}}
    .ability b.unnamed{{color:#8a9296}}.ability p{{margin:6px 0 0;color:#3f484d;font-size:15px;font-weight:750}}
    .source{{display:flex;justify-content:space-between;gap:14px;padding:10px 13px;border:1px solid #abb2b5;background:#fff}}
    .source + .source{{margin-top:7px}}.source b{{font-size:16px;font-weight:900;overflow-wrap:anywhere}}
    .source span{{flex:none;color:#626c72;font-size:14px;font-weight:850}}.source.more{{border-style:dashed;color:#6f787d;justify-content:center}}
    .catalog-grid{{display:grid;grid-template-columns:repeat(${CATALOG_COLUMNS},minmax(0,1fr));gap:8px}}
    .catalog-item{{min-height:64px;padding:11px 13px;border:1px solid #9ba2a5;border-bottom:5px solid #f2c500;background:#fff}}
    .catalog-item b{{display:block;font-size:16px;font-weight:900;overflow-wrap:anywhere}}
    .catalog-item span{{display:block;margin-top:5px;color:#657076;font-size:12px;font-weight:850}}
    footer{{display:flex;justify-content:space-between;gap:20px;margin-top:16px;padding-top:11px;border-top:3px solid #20262a;color:#697277;font-size:13px;font-weight:850;overflow-wrap:anywhere}}
    </style></head><body><main class="ency-card {klass}">{hero}{body}
    <footer><span>数据来源 AkeData</span><span>{_escape(revision)}</span></footer></main></body></html>"""


def _hero(
    kicker: str,
    title: str,
    subtitle: str,
    icon_url: str,
    facts: str,
    accent: str = "",
) -> str:
    style = f' style="--accent:#{accent}"' if accent else ""
    icon = f'<img class="hero-icon" src="{_attr(icon_url)}" alt="">' if icon_url else ""
    return f"""
    <header class="hero"{style}>
      <div class="hero-copy"><small>{_escape(kicker)}</small><h1>{_escape(title)}</h1>
      <p>{_escape(subtitle)}</p></div>
      <div class="hero-side">{icon}</div>
    </header>{facts}"""


def _fact_strip(facts: Sequence[tuple[str, str]]) -> str:
    cells = "".join(
        f"<div class='fact'><span>{_escape(label)}</span><b>{_escape(value)}</b></div>"
        for label, value in facts
        if value
    )
    return f"<div class='fact-strip'>{cells}</div>" if cells else ""


def _section(title: str, body: str) -> str:
    return f"<section class='ency-section'><div class='section-head'><h2>{_escape(title)}</h2></div>{body}</section>"


def _paragraph(text: str) -> str:
    return f"<p>{_multiline(text)}</p>" if str(text or "").strip() else "<p class='muted'>暂无</p>"


def _chips(values: Sequence[str], empty: str = "") -> str:
    cleaned = [str(value).strip() for value in values if str(value or "").strip()]
    if not cleaned:
        return f"<p class='muted'>{_escape(empty)}</p>" if empty else "<p class='muted'>暂无</p>"
    return "<div class='chip-grid'>" + "".join(f"<i>{_escape(value)}</i>" for value in cleaned) + "</div>"


def _catalog_item(item) -> str:
    return (
        f"<div class='catalog-item'><b>{_escape(item.name)}</b>"
        f"<span>{_escape(item.subtitle)}</span></div>"
    )


def _rich(line: str, styles: Mapping) -> str:
    """效果句里的 <@>、<#> 着色；颜色来自本卡视图的 term style。"""
    escaped = _escape(line)

    def style_tag(match: re.Match[str]) -> str:
        term_id, body = match.group(1), match.group(2)
        style = styles.get(term_id) if styles else None
        color = getattr(style, "color", "") if style else ""
        if color and not color.startswith("#"):
            color = f"#{color}"
        style_attr = f' style="color:{_attr(color)}"' if color else ""
        return f"<strong{style_attr}>{body}</strong>"

    return _STYLE_RE.sub(style_tag, _LINK_RE.sub(style_tag, escaped))


def _seconds(value: float | None) -> str:
    return f"{value:g} 秒" if value else ""


def _count(value: int | None) -> str:
    return f"{value}" if value else ""


def _percent(value: float | None) -> str:
    return f"{value:g}%" if value is not None else "—"


def _multiline(value: str) -> str:
    return _escape(value).replace("\n", "<br>")


def _escape(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _attr(value: object) -> str:
    return html.escape(str(value or ""), quote=True)
