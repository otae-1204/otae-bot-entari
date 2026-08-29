from __future__ import annotations

import html
from collections.abc import Mapping
from time import perf_counter

from loguru import logger

from utils.image_executor import run_image_render
from utils.image_utils import BrowserResource, screenshot_web_element
from utils.temp_files import schedule_temp_file_cleanup

from .draw import (
    _prepare_assets,
    _write_temp_html,
    is_height_limit_error,
    optimize_png_container,
)
from .stage_models import (
    BossRushStageDetails,
    EnergyDepositStageDetails,
    Stage,
    StageBlock,
    StageBlockEntry,
    StageCardView,
    StageCatalogGroup,
    StageCatalogView,
    StageEnemy,
    StageEnemyPoise,
    StageEnemyResistance,
    StageFact,
    StageReward,
    StageRewards,
    StageVariant,
    StageWave,
)


STAGE_CARD_WIDTH = 1500
STAGE_CARD_MAX_HEIGHT = 12000
# Tried in order once a single catalog image overflows; the first budget that fits wins.
STAGE_CATALOG_PAGE_BUDGETS = (120, 80, 50)

# Ether is deliberately not shown; matched on the stable code with the label as a fallback.
HIDDEN_RESIST_ELEMENTS = frozenset({"Ether"})
HIDDEN_RESIST_LABELS = frozenset({"超域"})

_CARD_OVERFLOW_SELECTORS = (
    ".stage-section",
    ".stat-cell",
    ".variant-tab",
    ".boss-card",
    ".mob",
    ".wave-group",
    ".reward",
    ".compare-table",
    ".resist-cell",
    ".resist-table",
    ".poise-cell",
    ".entry",
)
# A block that would dominate the card is trimmed rather than allowed to push it over
# the height limit; the note always states how many rows were left out.
BLOCK_ENTRY_BUDGET = 24
_CATALOG_OVERFLOW_SELECTORS = (".stage-family", ".catalog-item")


async def draw_stage_card(view: StageCardView) -> bytes:
    prepared = await _prepare_assets(_stage_icon_urls(view.stage), inline=False)
    return await _draw_stage_html(
        render_stage_card_html(view, prepared.urls),
        _CARD_OVERFLOW_SELECTORS,
        "stage",
        prepared.resources,
    )


async def draw_stage_catalog_card(view: StageCatalogView) -> bytes:
    return await _draw_stage_html(
        render_stage_catalog_html(view),
        _CATALOG_OVERFLOW_SELECTORS,
        "stage_catalog",
        None,
    )


async def draw_stage_catalog_cards(view: StageCatalogView) -> tuple[bytes, ...]:
    """One image while the catalog fits; otherwise split it across as few images as possible.

    Splitting is a render-side fallback on purpose: the user never asks for a page,
    so a catalog that outgrows one image must not turn into a failed query.
    """
    try:
        return (await draw_stage_catalog_card(view),)
    except RuntimeError as exc:
        if not is_height_limit_error(exc):
            raise
        last_error = exc

    for item_budget in STAGE_CATALOG_PAGE_BUDGETS:
        pages = _paginate_catalog(view, item_budget)
        try:
            rendered = [await draw_stage_catalog_card(page) for page in pages]
        except RuntimeError as exc:
            if not is_height_limit_error(exc):
                raise
            last_error = exc
            continue
        logger.info(
            f"[endfield] stage catalog paginated pages={len(rendered)} item_budget={item_budget}"
        )
        return tuple(rendered)
    raise last_error


def _paginate_catalog(view: StageCatalogView, item_budget: int) -> tuple[StageCatalogView, ...]:
    """Fill each page up to ``item_budget`` entries, splitting a family across pages if needed."""
    pages: list[list[StageCatalogGroup]] = []
    current: list[StageCatalogGroup] = []
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
                StageCatalogGroup(
                    key=group.key,
                    name=f"{group.name}（续）" if continued else group.name,
                    items=tuple(chunk),
                )
            )
            used += len(chunk)
            continued = True
    if current or not pages:
        pages.append(current)
    return tuple(
        StageCatalogView(
            groups=tuple(page),
            source=view.source,
            revision=view.revision,
            updated_at=view.updated_at,
            page_number=index + 1,
            page_count=len(pages),
            catalog_family_count=len(view.groups),
            catalog_queryable_count=view.queryable_count,
            catalog_pending_count=view.pending_count,
        )
        for index, page in enumerate(pages)
    )


def _stage_icon_urls(stage: Stage) -> tuple[str, ...]:
    urls = [_stage_icon_url(stage)]
    for variant in stage.variants:
        urls.extend(enemy.icon_url for enemy in variant.enemies or ())
        urls.extend(wave.enemy.icon_url for wave in variant.waves or ())
    return tuple(dict.fromkeys(url for url in urls if url))


def _stage_icon_url(stage: Stage) -> str:
    if stage.icon_url:
        return stage.icon_url
    if isinstance(stage.extension, BossRushStageDetails):
        return stage.extension.icon_url
    return ""


async def _draw_stage_html(
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
            ".stage-card",
            viewport=(STAGE_CARD_WIDTH, 1),
            timeout_ms=25000,
            max_height=STAGE_CARD_MAX_HEIGHT,
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


# ---------------------------------------------------------------- catalog card


def render_stage_catalog_html(view: StageCatalogView) -> str:
    groups = "".join(
        f"""
        <section class="stage-family">
          <div class="section-head"><h2>{_escape(group.name)}</h2><span>{_catalog_group_meta(group)}</span></div>
          <div class="catalog-grid">{''.join(_catalog_item(item) for item in group.items)}</div>
        </section>
        """
        for group in view.groups
    )
    subtitle = f"{view.family_count} 个玩法 · 按玩法分组"
    if view.page_count > 1:
        subtitle += f" · 第 {view.page_number} / {view.page_count} 张"
    body = f"""
    <header class="hero">
      <div class="hero-copy"><small>ENDFIELD / STAGE ARCHIVE</small><h1>关卡资料目录</h1><p>{_escape(subtitle)}</p></div>
      <div class="hero-counts">
        <div class="hero-count"><b>{view.queryable_count}</b><span>可查询</span></div>
        {f'<div class="hero-count muted"><b>{view.pending_count}</b><span>待完善</span></div>' if view.pending_count else ''}
      </div>
    </header>
    <div class="notice">发送 <b>/ef 副本 关卡名</b> 查询详情，默认展示排序最高的变体；<b>/ef 副本 关卡名 总览</b> 对比全部变体。</div>
    {groups or '<div class="empty">数据源暂未提供关卡资料目录。</div>'}
    <footer><span>来源 · {_escape(view.source)}</span><span>目录版本 · {_escape(view.revision)}{_updated_suffix(view.updated_at)}</span></footer>
    """
    return _document(body)


def _catalog_group_meta(group) -> str:
    pending = sum(not item.queryable for item in group.items)
    text = f"{len(group.items)} 个关卡"
    return f"{text} · {pending} 个资料待完善" if pending else text


def _catalog_item(item) -> str:
    if not item.queryable:
        return f'<div class="catalog-item pending"><b>{_escape(item.name)}</b><span>资料待完善</span></div>'
    hints = []
    if item.recommended_level is not None:
        hints.append(f"Lv.{item.recommended_level}")
    if item.region:
        hints.append(item.region)
    meta = " · ".join(hints)
    return (
        f'<div class="catalog-item"><b>{_escape(item.name)}</b>'
        f'{f"<span>{_escape(meta)}</span>" if meta else ""}</div>'
    )


# ------------------------------------------------------------------ stage card


def render_stage_card_html(view: StageCardView, icons: Mapping[str, str] | None = None) -> str:
    icons = icons or {}
    stage = view.stage
    variants = _ordered(stage.variants)
    overview = view.mode == "overview"
    selected = None if overview else view.selected_variant
    if not overview and selected is None:
        raise ValueError("详情卡缺少选中的关卡变体")

    unreachable = len(view.unreachable_enemies)
    if overview:
        content = _overview_content(stage, variants, icons, unreachable)
        subtitle = f"{stage.family_name} · 全部 {len(variants)} 个变体总览"
    else:
        content = _detail_content(stage, selected, variants, icons, unreachable)
        subtitle = f"{stage.family_name} · {selected.label}"
        if len(variants) > 1:
            subtitle += f"（共 {len(variants)} 个变体）"

    body = f"""
    <header class="hero">
      <div class="hero-copy"><small>ENDFIELD / STAGE ARCHIVE</small><h1>{_escape(stage.name)}</h1><p>{_escape(subtitle)}</p></div>
      {_hero_side(stage, icons)}
    </header>
    {_variant_rail(variants, selected)}
    {_stat_strip(stage, selected, variants)}
    {_summary_block(stage.summary)}
    {content}
    <footer><span>来源 · {_escape(stage.source.source)} / {_escape(stage.source.article_title)}</span><span>revision · {_escape(stage.source.revision)}{_updated_suffix(stage.source.updated_at)}</span></footer>
    """
    return _document(body)


def _hero_side(stage: Stage, icons: Mapping[str, str]) -> str:
    icon = icons.get(_stage_icon_url(stage), "")
    if icon:
        return f'<div class="hero-side"><img class="hero-icon" src="{_attr(icon)}" alt="{_attr(stage.name)}"></div>'
    return '<div class="hero-side"><div class="hero-mark">STAGE</div></div>'


def _variant_rail(variants: tuple[StageVariant, ...], selected: StageVariant | None) -> str:
    if len(variants) < 2:
        return ""
    tabs = "".join(
        f'<div class="variant-tab{" active" if selected is not None and item.id == selected.id else ""}">'
        f"<b>{_escape(item.label)}</b><span>{_level_text(item.recommended_level)}</span></div>"
        for item in variants
    )
    return f'<div class="variant-rail">{tabs}</div>'


def _stat_strip(stage: Stage, selected: StageVariant | None, variants: tuple[StageVariant, ...]) -> str:
    energy = _energy_details(selected) or (
        stage.extension if isinstance(stage.extension, EnergyDepositStageDetails) else None
    )
    cells: list[tuple[str, str]] = [("所属玩法", stage.family_name)]
    if selected is not None:
        cells.append(("推荐等级", _level_text(selected.recommended_level)))
        if selected.stamina_cost is not None:
            cells.append(("理智消耗", str(selected.stamina_cost)))
    else:
        levels = [item.recommended_level for item in variants if item.recommended_level is not None]
        if levels:
            cells.append(("推荐等级", f"{min(levels)} – {max(levels)}" if min(levels) != max(levels) else str(levels[0])))
    cells.append(("地区", stage.location or (energy.region if energy else "")))
    if energy and energy.intensity:
        cells.append(("强度", energy.intensity))
    cells.append(("开放条件", stage.unlock_condition))
    # Adapter facts carry whatever their gameplay considers headline data.
    cells.extend((fact.label, fact.value) for fact in _facts(stage, selected))
    enemies = _enemy_subjects(selected) if selected is not None else ()
    if len(enemies) > 1:
        total = sum(enemy.count for enemy in enemies if enemy.count is not None)
        kinds = f"{len(enemies)} 种"
        cells.append(("敌人", f"{kinds} · 共 {total} 只" if total else kinds))
    if selected is not None and selected.rewards:
        cells.append(("掉落", f"{len(selected.rewards)} 项"))
    seen: dict[str, str] = {}
    for label, value in cells:
        if value and label not in seen:
            seen[label] = value
    return f'<div class="stat-strip">{"".join(_stat_cell(label, value) for label, value in seen.items())}</div>'


def _facts(stage: Stage, selected: StageVariant | None) -> tuple[StageFact, ...]:
    variant_facts = selected.facts if selected is not None else ()
    return (*stage.facts, *variant_facts)


def _energy_details(variant: StageVariant | None) -> EnergyDepositStageDetails | None:
    if variant is None or not isinstance(variant.extension, EnergyDepositStageDetails):
        return None
    return variant.extension


def _detail_content(
    stage: Stage,
    variant: StageVariant,
    variants: tuple[StageVariant, ...],
    icons: Mapping[str, str],
    unreachable: int = 0,
) -> str:
    missing: list[str] = []
    if variant.mechanics is None:
        missing.append("关卡机制")
    blocks = [
        _variant_note(variant, stage.summary),
        _block_sections(variant.blocks),
        _enemy_block(variant, icons, missing),
        _resist_block(variant, missing, unreachable=unreachable),
        _poise_block(variant),
        _reward_block(variant, missing),
        _related_weapons(variant),
        _compare_block(variants, variant),
        _block_sections(stage.blocks),
        _missing_note(missing),
    ]
    return "".join(block for block in blocks if block)


def _overview_content(
    stage: Stage,
    variants: tuple[StageVariant, ...],
    icons: Mapping[str, str],
    unreachable: int = 0,
) -> str:
    compare = _compare_block(variants, None)
    notes = "".join(
        f'<article class="variant-card">'
        f'<div class="variant-top"><b>{_escape(item.label)}</b><span>{_escape(_level_label(item.recommended_level))}</span></div>'
        f"{_variant_body(item, stage.summary)}"
        f"</article>"
        for item in variants
    )
    blocks = [compare, _section("各变体说明", f'<div class="variant-grid">{notes}</div>')]
    # Without a compare table the enemy card is shown, and it already carries the boss strip.
    enemy_card_shown = not compare and bool(variants)
    if enemy_card_shown:
        blocks.append(_section("敌人资料", _enemy_body(variants[-1], icons)))
    if variants:
        blocks.append(
            _resist_block(
                variants[-1], [], boss_card_shown=enemy_card_shown, unreachable=unreachable
            )
        )
        if not enemy_card_shown:
            blocks.append(_poise_section(variants[-1]))
        blocks.append(_poise_block(variants[-1]))
    return "".join(block for block in blocks if block)


def _poise_section(variant: StageVariant) -> str:
    """Overview has no boss card to hang the strip on, so a lone boss gets its own section."""
    enemies = _resist_subjects(variant)
    if not _is_solo_boss(enemies) or enemies[0].poise is None:
        return ""
    return _section("敌人失衡", _poise_detail(enemies[0].poise), note=enemies[0].name)


def _variant_body(variant: StageVariant, summary: str) -> str:
    unique = _unique_text(variant, summary)
    if unique:
        return f'<p class="summary">{_multiline(unique)}</p>'
    if variant.mechanics is None:
        return '<p class="muted">数据源暂未提供关卡机制。</p>'
    return '<p class="muted">与关卡说明一致。</p>'


def _variant_note(variant: StageVariant, summary: str) -> str:
    unique = _unique_text(variant, summary)
    if not unique:
        return ""
    return f'<div class="variant-note"><span>{_escape(variant.label)} · 本级说明</span><p>{_multiline(unique)}</p></div>'


def _unique_text(variant: StageVariant, summary: str) -> str:
    """Drop the description the variant shares with the stage so it is not printed twice."""
    text = "\n".join(item for item in (variant.mechanics or ()) if item).strip()
    shared = str(summary or "").strip()
    if shared and text.startswith(shared):
        return text[len(shared) :].strip()
    return text


# --------------------------------------------------------------------- enemies


def _variant_waves(variant: StageVariant) -> tuple[StageWave, ...] | None:
    energy = _energy_details(variant)
    if energy is not None and energy.waves is not None:
        return energy.waves
    return variant.waves


def _enemy_block(variant: StageVariant, icons: Mapping[str, str], missing: list[str]) -> str:
    waves = _variant_waves(variant)
    if waves is not None:
        if not waves:
            return _section("敌人与波次", '<div class="empty">来源明确标注暂无出怪信息。</div>')
        body = _wave_body(waves)
        # Wave rows carry no stats, so a stat-bearing enemy list is still worth showing.
        detailed = [enemy for enemy in variant.enemies or () if _has_metrics(enemy)]
        if detailed:
            body += _enemy_body(variant, icons)
        return _section("敌人与波次", body)
    if variant.enemies is None:
        missing.append("敌人资料")
        return ""
    if not variant.enemies:
        return _section("敌人资料", '<div class="empty">来源明确标注暂无敌人。</div>')
    return _section("敌人资料", _enemy_body(variant, icons))


def _enemy_body(variant: StageVariant, icons: Mapping[str, str]) -> str:
    enemies = variant.enemies or ()
    detailed = [enemy for enemy in enemies if _has_metrics(enemy)]
    plain = [enemy for enemy in enemies if not _has_metrics(enemy)]
    # Only a lone boss keeps its resistances inline; anything else compares them in the matrix.
    inline = _is_solo_boss(tuple(enemies))
    blocks = [_boss_card(enemy, icons, show_resistances=inline) for enemy in detailed]
    if plain:
        blocks.append(f'<div class="mob-grid">{"".join(_mob_cell(enemy) for enemy in plain)}</div>')
    return "".join(blocks)


def _is_solo_boss(enemies: tuple[StageEnemy, ...]) -> bool:
    return len(enemies) == 1 and _has_metrics(enemies[0])


def _has_metrics(enemy: StageEnemy) -> bool:
    return enemy.hp is not None or enemy.attack is not None or enemy.defense is not None


def _boss_card(enemy: StageEnemy, icons: Mapping[str, str], *, show_resistances: bool = True) -> str:
    icon = icons.get(enemy.icon_url, "")
    portrait = (
        f'<div class="boss-icon"><img src="{_attr(icon)}" alt="{_attr(enemy.name)}"></div>'
        if icon
        else '<div class="boss-icon fallback">敌</div>'
    )
    metrics = [("生命", _amount(enemy.hp)), ("攻击", _amount(enemy.attack)), ("防御", _amount(enemy.defense))]
    cells = "".join(
        f'<div class="metric"><span>{label}</span><b>{value}</b></div>' for label, value in metrics if value
    )
    tags = []
    if enemy.level is not None:
        tags.append(f"Lv.{enemy.level}")
    if enemy.count is not None:
        tags.append(f"{enemy.count} 只")
    return f"""
    <div class="boss-card">
      {portrait}
      <div class="boss-main">
        <div class="boss-top"><b>{_escape(enemy.name)}</b><span>{_escape(" · ".join(tags))}</span></div>
        <div class="metric-grid">{cells}</div>
        {_resist_strip(enemy.resistances) if show_resistances else ""}
        {_poise_detail(enemy.poise) if show_resistances else ""}
      </div>
    </div>
    """


def _visible_resistances(
    rows: tuple[StageEnemyResistance, ...] | None,
) -> tuple[StageEnemyResistance, ...] | None:
    """Keeps None (never answered) distinct from () while dropping hidden elements."""
    if rows is None:
        return None
    return tuple(row for row in rows if not _is_hidden_element(row))


def _is_hidden_element(row: StageEnemyResistance) -> bool:
    return row.element in HIDDEN_RESIST_ELEMENTS or row.label in HIDDEN_RESIST_LABELS


def _resist_strip(raw: tuple[StageEnemyResistance, ...] | None) -> str:
    """FZ publishes `percent` as damage TAKEN, so 80% means 20% resistance, 100% means none."""
    if raw is None:
        return ""
    rows = _visible_resistances(raw)
    if not rows:
        return '<div class="empty">来源明确标注暂无元素抗性。</div>'
    cells = "".join(
        f'<div class="resist-cell{"" if not row.is_standard else " standard"}" style="--el:{_hex_color(row.color)}">'
        f"<span>{_escape(row.label)}</span><b>{_escape(_resist_headline(row))}</b>"
        f"<em>{_escape(_taken_text(row))}</em></div>"
        for row in rows
    )
    return f'<div class="resist-strip">{cells}</div>'


def _resist_headline(row: StageEnemyResistance) -> str:
    reduction = row.reduction
    if reduction is None:
        return "未提供"
    if abs(reduction) < 1e-9:
        return "标准"
    return f"抗 {_percent(reduction)}" if reduction > 0 else f"易伤 {_percent(-reduction)}"


def _taken_text(row: StageEnemyResistance) -> str:
    return f"受伤 {_percent(row.percent)}" if row.percent is not None else "受伤未提供"


def _percent(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:g}%"


def _hex_color(value: str) -> str:
    """Only ever emit a literal hex colour from the source into the style attribute."""
    text = str(value or "").lstrip("#")
    if len(text) in (3, 6) and all(char in "0123456789abcdefABCDEF" for char in text):
        return f"#{text}"
    return "#8a9296"


def _mob_cell(enemy: StageEnemy) -> str:
    tags = []
    if enemy.count is not None:
        tags.append(f"×{enemy.count}")
    if enemy.level is not None:
        tags.append(f"Lv.{enemy.level}")
    return f'<div class="mob"><b>{_escape(enemy.name)}</b><span>{_escape(" · ".join(tags))}</span></div>'


def _wave_body(waves: tuple[StageWave, ...]) -> str:
    groups: dict[tuple[object, str], list[StageWave]] = {}
    for wave in waves:
        groups.setdefault((wave.wave, wave.condition), []).append(wave)
    rows = []
    for (number, condition), items in groups.items():
        title = f"第 {number} 波" if number is not None else "波次未提供"
        note = condition or "触发条件未提供"
        total = sum(item.enemy.count for item in items if item.enemy.count is not None)
        count = f"{len(items)} 种 · 共 {total} 只" if total else f"{len(items)} 种"
        chips = "".join(_wave_chip(item) for item in items)
        rows.append(
            f'<div class="wave-group"><div class="wave-head"><b>{_escape(title)}</b>'
            f"<span>{_escape(note)}</span><em>{_escape(count)}</em></div>"
            f'<div class="wave-enemies">{chips}</div></div>'
        )
    return f'<div class="wave-list">{"".join(rows)}</div>'


def _wave_chip(wave: StageWave) -> str:
    tags = []
    if wave.enemy.count is not None:
        tags.append(f"×{wave.enemy.count}")
    if wave.enemy.level is not None:
        tags.append(f"Lv.{wave.enemy.level}")
    if wave.time is not None:
        tags.append(f"{wave.time:g}s")
    return f'<div class="wave-chip"><b>{_escape(wave.enemy.name)}</b><span>{_escape(" · ".join(tags))}</span></div>'


# ------------------------------------------------------------------ resistances


def _resist_block(
    variant: StageVariant,
    missing: list[str],
    *,
    boss_card_shown: bool = True,
    unreachable: int = 0,
) -> str:
    """A matrix for stages with several enemies; a single boss already shows a strip on its own card."""
    enemies = _resist_subjects(variant)
    if not enemies:
        return ""
    provided = [enemy for enemy in enemies if enemy.resistances is not None]
    known = [enemy for enemy in enemies if _visible_resistances(enemy.resistances)]
    if not provided:
        # Never claim the source withheld the field when the fetch is simply what failed.
        if unreachable:
            return _section("敌人元素抗性", f'<div class="missing">{_unreachable_text(unreachable)}</div>')
        missing.append("敌人元素抗性")
        return ""
    if _is_solo_boss(enemies):
        if boss_card_shown:
            return ""
        return _section(
            "敌人元素抗性",
            _resist_strip(enemies[0].resistances),
            note=f"{enemies[0].name} · 数值为受到该元素伤害的比例",
        )
    if not known:
        return _section("敌人元素抗性", '<div class="empty">来源明确标注暂无元素抗性。</div>')
    elements = _resist_elements(known)
    head = "".join(
        f'<th style="--el:{_hex_color(color)}">{_escape(label)}</th>' for _, label, color in elements
    )
    body = ""
    for enemy in enemies:
        lookup = {_element_key(row): row for row in _visible_resistances(enemy.resistances) or ()}
        cells = "".join(_resist_matrix_cell(lookup.get(key)) for key, _, _ in elements)
        body += f'<tr><th scope="row">{_escape(enemy.name)}</th>{cells}</tr>'
    return _section(
        "敌人元素抗性",
        f'<table class="resist-table"><thead><tr><th scope="col">敌人</th>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table>",
        note=_resist_note(enemies, provided, unreachable),
    )


def _resist_note(
    enemies: tuple[StageEnemy, ...], provided: list[StageEnemy], unreachable: int
) -> str:
    """Report the three states separately so provenance is never misstated."""
    empty = sum(1 for enemy in provided if enemy.resistances == ())
    absent = len(enemies) - len(provided)
    parts = ["数值为受到该元素伤害的比例，越低越抗"]
    if empty:
        parts.append(f"{empty} 个敌人来源标注暂无抗性")
    if unreachable:
        parts.append(_unreachable_text(min(unreachable, absent) or unreachable))
    elif absent:
        parts.append(f"{absent} 个敌人数据源暂未提供")
    return "；".join(parts)


def _unreachable_text(count: int) -> str:
    return f"{count} 个敌人的资料本次未能取得，稍后重试"


def _resist_subjects(variant: StageVariant) -> tuple[StageEnemy, ...]:
    return _enemy_subjects(variant)


def _enemy_subjects(variant: StageVariant) -> tuple[StageEnemy, ...]:
    """Whichever list actually names this variant's enemies — the waves or the flat roster."""
    seen: dict[tuple[str, str], StageEnemy] = {}
    waves = _variant_waves(variant)
    subjects = tuple(wave.enemy for wave in waves) if waves is not None else variant.enemies or ()
    for enemy in subjects:
        key = (enemy.enemy_id, enemy.name)
        current = seen.get(key)
        if current is None or _enemy_detail_score(enemy) > _enemy_detail_score(current):
            seen[key] = enemy
    return tuple(seen.values())


def _enemy_detail_score(enemy: StageEnemy) -> int:
    return sum(
        value is not None
        for value in (
            enemy.hp,
            enemy.attack,
            enemy.defense,
            enemy.resistances,
            enemy.poise,
        )
    )


def _resist_elements(enemies: list[StageEnemy]) -> tuple[tuple[str, str, str], ...]:
    """Join on the stable element code so a label difference cannot split one element into two columns."""
    ordered: dict[str, tuple[str, str]] = {}
    for enemy in enemies:
        for row in _visible_resistances(enemy.resistances) or ():
            ordered.setdefault(_element_key(row), (row.label, row.color))
    return tuple((key, label, color) for key, (label, color) in ordered.items())


def _element_key(row: StageEnemyResistance) -> str:
    return row.element or row.label


def _resist_matrix_cell(row: StageEnemyResistance | None) -> str:
    if row is None or row.percent is None:
        return '<td class="resist-none">--</td>'
    if row.is_standard:
        return f'<td class="resist-std">{_escape(_percent(row.percent))}</td>'
    kind = "resist-lo" if row.percent < 100 else "resist-hi"
    return f'<td class="{kind}" style="--el:{_hex_color(row.color)}">{_escape(_percent(row.percent))}</td>'


# ------------------------------------------------------------------------ poise


def _poise_detail(poise: StageEnemyPoise | None) -> str:
    """Poise ceiling, execution scalar, recovery timing, recovery scalar, and bar knots."""
    if poise is None:
        return ""
    items = [
        ("失衡值上限", _plain(poise.max_value)),
        ("处决承伤系数", _coefficient(poise.damage_scalar)),
        ("失衡恢复时间", f"{_plain(poise.recover_seconds)}s" if poise.recover_seconds is not None else ""),
        ("失衡恢复时间系数", _coefficient(poise.recover_scalar)),
        ("失衡节点", _knot_text(poise.knots)),
    ]
    cells = "".join(
        f'<div class="poise-cell"><span>{_escape(label)}</span><b>{_escape(value)}</b></div>'
        for label, value in items
        if value
    )
    return f'<div class="poise-strip">{cells}</div>' if cells else ""


def _knot_text(knots: tuple[float, ...] | None) -> str:
    if knots is None:
        return ""
    if not knots:
        return "无"
    return " · ".join(_percent(value * 100) for value in knots)


def _poise_block(variant: StageVariant) -> str:
    """A table when several enemies have poise data; a lone boss shows it on its own card."""
    enemies = _resist_subjects(variant)
    withpoise = [enemy for enemy in enemies if enemy.poise is not None]
    if not withpoise or _is_solo_boss(enemies):
        return ""
    columns = [
        ("失衡值上限", lambda poise: _plain(poise.max_value)),
        ("处决承伤系数", lambda poise: _coefficient(poise.damage_scalar)),
        ("失衡恢复时间", lambda poise: f"{_plain(poise.recover_seconds)}s" if poise.recover_seconds is not None else ""),
        ("失衡恢复时间系数", lambda poise: _coefficient(poise.recover_scalar)),
        ("失衡节点", lambda poise: _knot_text(poise.knots)),
    ]
    active = [
        column for column in columns if any(column[1](enemy.poise) for enemy in withpoise)
    ]
    if not active:
        return ""
    head = "".join(f"<th>{_escape(label)}</th>" for label, _ in active)
    body = ""
    for enemy in enemies:
        if enemy.poise is None:
            cells = "".join('<td class="resist-none">--</td>' for _ in active)
        else:
            cells = "".join(f"<td>{_escape(getter(enemy.poise)) or '--'}</td>" for _, getter in active)
        body += f'<tr><th scope="row">{_escape(enemy.name)}</th>{cells}</tr>'
    unknown = len(enemies) - len(withpoise)
    note = "节点为失衡条上的刻度位置" if any(label == "失衡节点" for label, _ in active) else ""
    if unknown:
        note += ("；" if note else "") + f"{unknown} 个敌人数据源暂未提供"
    return _section(
        "敌人失衡",
        f'<table class="resist-table poise-table"><thead><tr><th scope="col">敌人</th>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table>",
        note=note,
    )


def _plain(value: float | None) -> str:
    return "" if value is None else f"{value:g}"


def _coefficient(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


# --------------------------------------------------------------------- rewards


def _reward_block(variant: StageVariant, missing: list[str]) -> str:
    if variant.reward_sets is not None:
        return _reward_sets_block(variant.reward_sets)
    if variant.rewards is None:
        missing.append("奖励与掉落")
        return ""
    if not variant.rewards:
        return _section("奖励与掉落", '<div class="empty">来源明确标注暂无奖励。</div>')
    cells = "".join(_reward_cell(item) for item in variant.rewards)
    return _section("奖励与掉落", f'<div class="reward-grid">{cells}</div>')


def _reward_sets_block(rewards: StageRewards) -> str:
    """A stage that lets the player pick between reward groups must not read as getting both."""
    title = rewards.title or "奖励与掉落"
    if not rewards.is_choice:
        cells = "".join(_reward_cell(item) for item in rewards.items)
        return _section(title, f'<div class="reward-grid">{cells}</div>')
    groups = "".join(
        f'<div class="reward-group"><div class="reward-group-head">{_escape(group.label or f"奖励组 {index}")}</div>'
        f'<div class="reward-grid">{"".join(_reward_cell(item) for item in group.items)}</div></div>'
        for index, group in enumerate(rewards.groups, 1)
    )
    note = f"结算时 {len(rewards.groups)} 选 {rewards.select_count}"
    return _section(title, f'<div class="reward-groups">{groups}</div>', note=note)


# ---------------------------------------------------------------- generic blocks


def _block_sections(blocks: tuple[StageBlock, ...]) -> str:
    return "".join(_block_section(block) for block in blocks if not block.is_empty)


def _block_section(block: StageBlock) -> str:
    """Renders an adapter-supplied list without knowing what gameplay produced it."""
    parts = []
    if block.facts:
        parts.append(
            f'<div class="stat-strip inner">'
            f'{"".join(_stat_cell(fact.label, fact.value) for fact in block.facts)}</div>'
        )
    shown = block.entries[:BLOCK_ENTRY_BUDGET]
    if shown:
        parts.append(f'<div class="entry-grid">{"".join(_entry_cell(item) for item in shown)}</div>')
    hidden = len(block.entries) - len(shown)
    note = block.note
    if hidden:
        note = f"{note}；另有 {hidden} 项未展示" if note else f"共 {len(block.entries)} 项，另有 {hidden} 项未展示"
    return _section(block.title, "".join(parts), note=note)


def _entry_cell(entry: StageBlockEntry) -> str:
    head = f"<b>{_escape(entry.name)}</b>" if entry.name else ""
    meta = f"<span>{_escape(entry.meta)}</span>" if entry.meta else ""
    parts = [f'<div class="entry-top">{head}{meta}</div>']
    if entry.badges:
        chips = "".join(f"<i>{_escape(badge)}</i>" for badge in entry.badges)
        parts.append(f'<div class="entry-badges">{chips}</div>')
    if entry.desc:
        parts.append(f"<p>{_multiline(entry.desc)}</p>")
    if entry.rewards:
        rewards = "".join(f"<em>{_escape(_reward_label(item))}</em>" for item in entry.rewards)
        parts.append(f'<div class="entry-rewards">{rewards}</div>')
    return f'<div class="entry">{"".join(parts)}</div>'


def _reward_label(item: StageReward) -> str:
    return f"{item.name} {item.quantity_text}".strip()


def _reward_cell(item: StageReward) -> str:
    rarity = f" rarity-{item.rarity}" if item.rarity else ""
    tags = [f"{item.rarity}★" if item.rarity else "", item.quantity_text]
    meta = " · ".join(tag for tag in tags if tag)
    return (
        f'<div class="reward{rarity}"><b>{_escape(item.name)}</b>'
        f'{f"<span>{_escape(meta)}</span>" if meta else ""}</div>'
    )


def _related_weapons(variant: StageVariant) -> str:
    energy = variant.extension if isinstance(variant.extension, EnergyDepositStageDetails) else None
    weapons = energy.weapon_references if energy else None
    if not weapons:
        return ""
    preview = weapons[:36]
    chips = "".join(f'<span class="chip">{_escape(name)}</span>' for name in preview)
    more = f'<span class="chip more">其余 {len(weapons) - len(preview)} 把</span>' if len(weapons) > len(preview) else ""
    return _section(
        "相关武器",
        f'<div class="chip-grid">{chips}{more}</div>',
        note=f"共 {len(weapons)} 把",
    )


# -------------------------------------------------------------- variant compare


def _compare_block(variants: tuple[StageVariant, ...], selected: StageVariant | None) -> str:
    if len(variants) < 2:
        return ""
    rows = [(variant, _headline_enemy(variant)) for variant in variants]
    if not any(enemy is not None and _has_metrics(enemy) for _, enemy in rows):
        return ""
    columns = [("推荐等级", lambda variant, enemy: _level_text(variant.recommended_level))]
    if not _levels_agree(rows):
        columns.append(("敌人等级", lambda variant, enemy: f"Lv.{enemy.level}" if enemy and enemy.level is not None else ""))
    columns.extend(
        [
            ("生命", lambda variant, enemy: _amount(enemy.hp if enemy else None)),
            ("攻击", lambda variant, enemy: _amount(enemy.attack if enemy else None)),
            ("防御", lambda variant, enemy: _amount(enemy.defense if enemy else None)),
        ]
    )
    active = [column for column in columns if any(column[1](variant, enemy) for variant, enemy in rows)]
    head = "".join(f"<th>{_escape(label)}</th>" for label, _ in active)
    body = ""
    for variant, enemy in rows:
        current = selected is not None and variant.id == selected.id
        cells = "".join(f"<td>{_escape(getter(variant, enemy)) or '--'}</td>" for _, getter in active)
        body += f'<tr class="{"is-current" if current else ""}"><th scope="row">{_escape(variant.label)}</th>{cells}</tr>'
    note = "当前变体已高亮" if selected is not None else f"{len(variants)} 个变体"
    return _section(
        "变体对比",
        f'<table class="compare-table"><thead><tr><th scope="col">变体</th>{head}</tr></thead><tbody>{body}</tbody></table>',
        note=note,
    )


def _levels_agree(rows: list[tuple[StageVariant, StageEnemy | None]]) -> bool:
    """Enemy level usually mirrors the recommended level; drop the duplicate column when it does."""
    return all(enemy is not None and enemy.level == variant.recommended_level for variant, enemy in rows)


def _headline_enemy(variant: StageVariant) -> StageEnemy | None:
    enemies = variant.enemies or ()
    detailed = [enemy for enemy in enemies if _has_metrics(enemy)]
    if detailed:
        return max(detailed, key=lambda enemy: enemy.hp or enemy.attack or 0)
    return enemies[0] if enemies else None


# --------------------------------------------------------------------- helpers


def _summary_block(summary: str) -> str:
    return _section("关卡说明", f'<p class="summary">{_multiline(summary)}</p>') if summary else ""


def _stat_cell(label: str, value: str) -> str:
    return f'<div class="stat-cell"><span>{_escape(label)}</span><b>{_escape(value)}</b></div>'


def _section(title: str, content: str, *, note: str = "") -> str:
    head = f'<div class="section-head"><h2>{_escape(title)}</h2>{f"<span>{_escape(note)}</span>" if note else ""}</div>'
    return f'<section class="stage-section">{head}{content}</section>'


def _missing_note(titles: list[str]) -> str:
    """One compact line instead of an empty section per unavailable field."""
    if not titles:
        return ""
    return f'<div class="missing">数据源暂未提供该项资料：{_escape("、".join(titles))}</div>'


def _ordered(variants: tuple[StageVariant, ...]) -> tuple[StageVariant, ...]:
    return tuple(sorted(variants, key=lambda item: item.sort_order))


def _level_text(level: int | None) -> str:
    return str(level) if level is not None else "未提供"


def _level_label(level: int | None) -> str:
    return f"推荐等级 {level}" if level is not None else "推荐等级未提供"


def _amount(value: int | float | None) -> str:
    return f"{value:,}" if value is not None else ""


def _short_date(value: str) -> str:
    text = str(value or "")
    return text[5:10].replace("-", "/") if len(text) >= 10 else ""


def _updated_suffix(value: str) -> str:
    date = _short_date(value)
    return f" · 更新 {date}" if date else ""


def _document(body: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>
    *{{box-sizing:border-box}}html,body{{margin:0;width:{STAGE_CARD_WIDTH}px;background:#dfe3e3;color:#171b1f;font-family:'Microsoft YaHei','PingFang SC','Noto Sans SC',Arial,sans-serif}}
    .stage-card{{width:{STAGE_CARD_WIDTH}px;min-height:560px;padding:30px;background:linear-gradient(90deg,rgba(23,27,31,.065) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(0deg,rgba(23,27,31,.065) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(135deg,#f8f9f6,#e4e8e9)}}
    .hero{{display:flex;justify-content:space-between;align-items:center;gap:24px;min-height:150px;padding:24px 30px;border:1px solid #171b1f;border-bottom:9px solid #f2c500;background:#20262a;color:#fff}}
    .hero-copy{{min-width:0}}.hero small{{color:#f2c500;font-size:14px;font-weight:900;letter-spacing:.22em}}.hero h1{{margin:9px 0 0;font-size:50px;line-height:1;font-weight:950;overflow-wrap:anywhere}}.hero p{{margin:10px 0 0;color:#cfd5d8;font-size:19px;font-weight:850}}
    .hero-side{{flex:none;display:flex;align-items:center;gap:18px}}.hero-mark{{font-size:22px;font-weight:950;letter-spacing:.28em;color:#f2c500}}
    .hero-icon{{width:110px;height:110px;object-fit:contain;border:1px solid rgba(242,197,0,.55);background:rgba(255,255,255,.06);padding:6px}}
    .hero-counts{{flex:none;display:flex;gap:26px;text-align:right}}.hero-count b{{display:block;font-size:52px;line-height:1;font-weight:950}}.hero-count span{{color:#cfd5d8;font-size:15px;font-weight:850}}.hero-count.muted b{{color:#98a2a8}}
    .notice{{margin-top:16px;padding:14px 18px;border-left:8px solid #f2c500;background:#fff;font-size:17px;font-weight:700}}.notice b{{color:#8b6d00}}
    .variant-rail{{display:grid;grid-template-columns:repeat(auto-fill,minmax(212px,1fr));gap:10px;margin-top:16px}}
    .variant-tab{{padding:10px 16px;border:1px solid #abb2b5;border-bottom:5px solid #b9c0c2;background:rgba(255,255,255,.9)}}
    .variant-tab b{{display:block;font-size:21px;font-weight:950}}.variant-tab span{{display:block;margin-top:3px;color:#6b7479;font-size:13px;font-weight:850}}
    .variant-tab.active{{border-color:#20262a;border-bottom-color:#f2c500;background:#20262a;color:#fff}}.variant-tab.active span{{color:#f2c500}}
    .stat-strip{{display:grid;grid-template-columns:repeat(auto-fill,minmax(212px,1fr));gap:10px;margin-top:16px}}
    .stat-cell{{min-height:78px;padding:13px 15px;border:1px solid #abb2b5;border-left:6px solid #20262a;background:rgba(255,255,255,.88)}}
    .stat-cell span{{display:block;color:#6b7479;font-size:13px;font-weight:900}}.stat-cell b{{display:block;margin-top:6px;font-size:21px;overflow-wrap:anywhere}}
    .variant-note{{margin-top:16px;padding:14px 18px;border:1px solid #abb2b5;border-left:8px solid #f2c500;background:#fff}}
    .variant-note span{{display:block;color:#8b6d00;font-size:13px;font-weight:900;letter-spacing:.05em}}.variant-note p{{margin:7px 0 0;font-size:17px;line-height:1.5;font-weight:750}}
    .stage-section,.stage-family{{margin-top:16px;padding:17px;border:1px solid #9aa2a5;background:rgba(248,249,247,.96)}}
    .section-head{{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;margin-bottom:12px;padding-bottom:9px;border-bottom:4px solid #20262a}}
    .section-head h2{{margin:0;font-size:25px}}.section-head span{{color:#697277;font-size:14px;font-weight:850}}
    .summary{{margin:0;font-size:17px;line-height:1.55;font-weight:700;overflow-wrap:anywhere}}.muted{{margin:0;color:#7b8288;font-size:15px;font-weight:750}}
    .missing,.empty{{padding:16px;border:1px dashed #9aa2a5;background:#edf0f0;color:#71797d;font-size:16px;font-weight:750}}
    .missing{{margin-top:16px}}
    .boss-card{{display:grid;grid-template-columns:120px minmax(0,1fr);gap:16px;padding:14px;border:1px solid #abb2b5;border-left:7px solid #f2c500;background:#fff}}
    .boss-card + .boss-card{{margin-top:9px}}
    .boss-icon{{width:120px;height:120px;display:grid;place-items:center;background:radial-gradient(circle,#fff,#e3e7e8);border:1px solid #c8ced0}}
    .boss-icon img{{width:112px;height:112px;object-fit:contain}}.boss-icon.fallback{{color:#9aa1a6;font-size:20px;font-weight:950}}
    .boss-main{{min-width:0;display:flex;flex-direction:column;justify-content:center;gap:12px}}
    .boss-top{{display:flex;align-items:baseline;gap:14px}}.boss-top b{{font-size:26px;font-weight:950}}.boss-top span{{color:#626c72;font-size:15px;font-weight:850}}
    .metric-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:9px}}
    .metric{{padding:10px 13px;border:1px solid #c4c9cb;background:#f2f4f4}}
    .metric span{{display:block;color:#6b7479;font-size:12px;font-weight:900}}.metric b{{display:block;margin-top:4px;font-size:22px;font-weight:950}}
    .resist-strip{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}}
    .resist-cell{{padding:9px 11px;border:1px solid #c4c9cb;border-top:5px solid var(--el,#8a9296);background:#f2f4f4}}
    .resist-cell span{{display:block;color:#4f5a60;font-size:13px;font-weight:900;overflow-wrap:anywhere}}
    .resist-cell b{{display:block;margin-top:4px;font-size:17px;font-weight:950;color:#20262a}}
    .resist-cell em{{display:block;margin-top:2px;color:#727b81;font-size:12px;font-style:normal;font-weight:850}}
    .resist-cell.standard{{background:#eceeee}}.resist-cell.standard b{{color:#7b8288;font-weight:900}}
    .resist-table{{width:100%;border-collapse:collapse;font-size:16px}}
    .resist-table th,.resist-table td{{padding:9px 12px;border:1px solid #bcc2c4;text-align:center;font-weight:850}}
    .resist-table thead th{{background:#20262a;color:#fff;font-size:14px;font-weight:900;border-bottom:5px solid var(--el,#f2c500)}}
    .resist-table thead th:first-child{{text-align:left;border-bottom-color:#f2c500}}
    .resist-table tbody th{{background:#eceeee;text-align:left;font-size:16px;font-weight:950;overflow-wrap:anywhere}}
    .resist-table tbody td{{background:#fff;font-variant-numeric:tabular-nums}}
    .resist-table .resist-std{{color:#98a0a5}}
    .resist-table .resist-lo{{color:#20262a;font-weight:950;background:#fdf6dc;box-shadow:inset 4px 0 0 var(--el,#8a9296)}}
    .resist-table .resist-hi{{color:#20262a;font-weight:950;background:#eaf4ea;box-shadow:inset 4px 0 0 var(--el,#8a9296)}}
    .resist-table .resist-none{{color:#b3babd}}
    .poise-strip{{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:8px}}
    .poise-cell{{padding:9px 12px;border:1px solid #c4c9cb;border-left:5px solid #20262a;background:#f2f4f4}}
    .poise-cell span{{display:block;color:#6b7479;font-size:12px;font-weight:900}}
    .poise-cell b{{display:block;margin-top:4px;font-size:18px;font-weight:950;overflow-wrap:anywhere}}
    .poise-table tbody td{{font-variant-numeric:tabular-nums}}
    .mob-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(228px,1fr));gap:8px}}
    .mob{{min-height:64px;padding:11px 13px;border:1px solid #abb2b5;border-left:5px solid #20262a;background:#fff}}
    .mob b{{display:block;font-size:17px;font-weight:900;overflow-wrap:anywhere}}.mob span{{display:block;margin-top:4px;color:#626c72;font-size:13px;font-weight:850}}
    .wave-list{{display:grid;gap:9px}}
    .wave-group{{padding:12px 14px;border:1px solid #abb2b5;background:#fff}}
    .wave-head{{display:flex;align-items:baseline;gap:12px;padding-bottom:9px;border-bottom:2px solid #d3d8d9}}
    .wave-head b{{padding:3px 10px;background:#20262a;color:#f2c500;font-size:15px;font-weight:950}}
    .wave-head span{{color:#4f5a60;font-size:15px;font-weight:850}}.wave-head em{{margin-left:auto;color:#7b8288;font-size:13px;font-style:normal;font-weight:850}}
    .wave-enemies{{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:7px;margin-top:9px}}
    .wave-chip{{padding:9px 12px;border:1px solid #c9cfd1;background:#f2f4f4}}
    .wave-chip b{{display:block;font-size:16px;font-weight:900;overflow-wrap:anywhere}}.wave-chip span{{display:block;margin-top:3px;color:#687278;font-size:12px;font-weight:850}}
    .reward-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(216px,1fr));gap:8px}}
    .reward{{min-height:62px;padding:11px 13px;border:1px solid #abb2b5;border-left:5px solid #8a9296;background:#fff}}
    .reward.rarity-5{{border-left-color:#c88a00}}.reward.rarity-4{{border-left-color:#7446bc}}.reward.rarity-3{{border-left-color:#2874b8}}
    .reward b{{display:block;font-size:16px;font-weight:900;overflow-wrap:anywhere}}.reward span{{display:block;margin-top:4px;color:#687278;font-size:13px;font-weight:850}}
    .reward-groups{{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:10px}}
    .reward-group{{padding:12px;border:1px solid #abb2b5;border-top:5px solid #f2c500;background:#fbfcfb}}
    .reward-group-head{{margin-bottom:9px;font-size:16px;font-weight:950;color:#8b6d00}}
    .stat-strip.inner{{margin-top:0;margin-bottom:10px}}
    .entry-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:8px}}
    .entry{{padding:12px 14px;border:1px solid #abb2b5;border-left:5px solid #20262a;background:#fff}}
    .entry-top{{display:flex;justify-content:space-between;align-items:baseline;gap:12px}}
    .entry-top b{{font-size:17px;font-weight:900;overflow-wrap:anywhere}}
    .entry-top span{{flex:none;color:#8b6d00;font-size:13px;font-weight:900}}
    .entry p{{margin:7px 0 0;color:#3f484d;font-size:14px;line-height:1.5;font-weight:750;overflow-wrap:anywhere}}
    .entry-badges{{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}}
    .entry-badges i{{padding:3px 8px;border:1px solid #c2c8ca;background:#eef0ef;font-style:normal;font-size:12px;font-weight:900;color:#5d666b}}
    .entry-rewards{{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px;padding-top:8px;border-top:1px dashed #cfd4d5}}
    .entry-rewards em{{padding:3px 9px;background:#fdf6dc;border:1px solid #e4d091;font-style:normal;font-size:12px;font-weight:900;color:#6d5600}}
    .chip-grid{{display:flex;flex-wrap:wrap;gap:7px}}.chip{{padding:8px 11px;border:1px solid #a7afb2;background:#eef0ef;font-size:14px;font-weight:850}}.chip.more{{border-style:dashed;color:#6f787d}}
    .compare-table{{width:100%;border-collapse:collapse;font-size:17px}}
    .compare-table th,.compare-table td{{padding:11px 14px;border:1px solid #bcc2c4;text-align:right;font-weight:850}}
    .compare-table thead th{{background:#20262a;color:#fff;font-size:14px;font-weight:900;letter-spacing:.04em}}
    .compare-table thead th:first-child,.compare-table tbody th{{text-align:left}}
    .compare-table tbody th{{background:#eceeee;font-size:18px;font-weight:950}}
    .compare-table tbody td{{background:#fff;font-variant-numeric:tabular-nums}}
    .compare-table tr.is-current th{{background:#20262a;color:#f2c500}}.compare-table tr.is-current td{{background:#fdf6dc}}
    .variant-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:10px}}
    .variant-card{{padding:15px;border:1px solid #9ba2a5;border-top:6px solid #f2c500;background:#fff}}
    .variant-top{{display:flex;justify-content:space-between;gap:16px;align-items:baseline;margin-bottom:9px}}
    .variant-top b{{font-size:23px;font-weight:950}}.variant-top span{{color:#626c72;font-size:14px;font-weight:850}}
    .catalog-grid{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px}}
    .catalog-item{{min-height:66px;padding:11px 13px;border:1px solid #9ba2a5;border-bottom:5px solid #f2c500;background:#fff}}
    .catalog-item b{{display:block;font-size:17px;font-weight:900;overflow-wrap:anywhere}}
    .catalog-item span{{display:block;margin-top:5px;color:#657076;font-size:12px;font-weight:850}}
    .catalog-item.pending{{border-bottom-color:#8a9296;background:#eceeee}}
    footer{{display:flex;justify-content:space-between;gap:20px;margin-top:16px;padding-top:12px;border-top:3px solid #20262a;color:#697277;font-size:13px;font-weight:850;overflow-wrap:anywhere}}
    </style></head><body><main class="stage-card">{body}</main></body></html>"""


def _multiline(value: str) -> str:
    return _escape(value).replace("\n", "<br>")


def _escape(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _attr(value: object) -> str:
    return html.escape(str(value or ""), quote=True)
