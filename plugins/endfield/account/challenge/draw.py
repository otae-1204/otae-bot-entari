"""Personal challenge cards and browser rendering.

Models and parsing remain re-exported for callers of the original combined API.
"""

from __future__ import annotations

import re
from time import perf_counter, time
from typing import Any, Sequence

from loguru import logger

from ...paths import IMAGE_DIR, UI_DIR
from ...rendering.cards import (
    _prepare_assets,
    _write_temp_html,
    esc,
    esc_attr,
    optimize_png_container,
)
from otae_bot.infrastructure.rendering.executor import run_image_render
from otae_bot.infrastructure.rendering.browser import screenshot_web_element
from otae_bot.infrastructure.rendering.temp_files import schedule_temp_file_cleanup



from .models import (
    ChallengeAmbiguousError as ChallengeAmbiguousError,
    ChallengeEnemy as ChallengeEnemy,
    ChallengeIdentity as ChallengeIdentity,
    ChallengeMember as ChallengeMember,
    ChallengeRecord as ChallengeRecord,
    ChallengeResolutionError as ChallengeResolutionError,
    MonumentDungeon as MonumentDungeon,
    MonumentGroup as MonumentGroup,
    MonumentPayload as MonumentPayload,
    WarAchievement as WarAchievement,
    WarDungeon as WarDungeon,
    WarEchoPayload as WarEchoPayload,
    WarGroup as WarGroup,
    WarSeason as WarSeason,
    WarWeek as WarWeek,
    _chunks as _chunks,
    _current_item as _current_item,
    _int as _int,
)
from .parsing import (
    _date as _date,
    _dict as _dict,
    _difficulty_label as _difficulty_label,
    _display_stage_name as _display_stage_name,
    _empty_monument as _empty_monument,
    _enemies as _enemies,
    _flag as _flag,
    _format_duration as _format_duration,
    _latest_monument_record as _latest_monument_record,
    _latest_war_record as _latest_war_record,
    _list as _list,
    _localized_dungeon_plain as _localized_dungeon_plain,
    _localized_dungeon_text as _localized_dungeon_text,
    _localized_plain as _localized_plain,
    _localized_text as _localized_text,
    _member as _member,
    _monument_difficulty_label as _monument_difficulty_label,
    _monument_dungeon as _monument_dungeon,
    _multiline as _multiline,
    _normalize as _normalize,
    _period as _period,
    _period_short as _period_short,
    _pick as _pick,
    _pick_or_none as _pick_or_none,
    _plain as _plain,
    _record as _record,
    _score as _score,
    _text as _text,
    _timestamp as _timestamp,
    _unwrap_data as _unwrap_data,
    _war_dungeon as _war_dungeon,
    parse_monument as parse_monument,
    parse_war_echoes as parse_war_echoes,
    resolve_monument_detail as resolve_monument_detail,
    resolve_war_detail as resolve_war_detail,
)

CHALLENGE_CARD_WIDTH = 1920
CHALLENGE_CARD_HEIGHT = 1080
# 卡片高度随内容生长，这里只是防止布局失控时产出超长图的兜底上限。
CHALLENGE_CARD_MAX_HEIGHT = 6000
CHALLENGE_VARIANTS = ("a", "b", "c")
DEFAULT_VARIANT = "b"
# 空态「下一步」的提示。原因取自 docs/skland_endfield_public_query.md 的实测结论：
# code 0 且账号定位正常时，缺的是通关记录或展示开关，两者都可能。
NEXT_STEP_HINT = "如果你确实打过，请在森空岛开启个人数据展示后回来重新查询。"
# 挑战卡主色对齐插件家底色板：影拓=琥珀金（同族于签名黄 #ffd000），回响=钢青蓝（配装页数据蓝的降饱和版）。
MONUMENT_ACCENT = "#ffb300"
MONUMENT_ACCENT_DEEP = "#8a6500"
# 苦难档身份色取官方玩法条红（version_calendar 的 .entry.gameplay 同源，公告红 #d80018 同族）；
# 墨底反相行用提亮版，保证 25px 主字在两种表面上都站得住。
MONUMENT_HARD = "#d64035"
MONUMENT_HARD_LIFT = "#ea5648"
WAR_ACCENT = "#2c63a8"
WAR_ACCENT_DEEP = "#2c63a8"
WAR_ACCENT_LIFT = "#6f9fd8"
# 难度身份色阶梯：基础档=墨，往上依次是该玩法的系统色与危险红。
# 每项是 (浅底脊线, 深底脊线, 浅底文字, 深底文字)。
TIER_IDENTITY = {
    ("monument", "normal"): ("rgba(23,27,31,.34)", "rgba(255,255,255,.34)", "#414c52", "#ffffff"),
    ("monument", "hard"): (MONUMENT_HARD, MONUMENT_HARD, MONUMENT_HARD, MONUMENT_HARD_LIFT),
    ("war", "normal"): ("rgba(23,27,31,.34)", "rgba(255,255,255,.34)", "#414c52", "#ffffff"),
    ("war", "hard"): (WAR_ACCENT, WAR_ACCENT, WAR_ACCENT, WAR_ACCENT_LIFT),
    ("war", "cruel"): (MONUMENT_HARD, MONUMENT_HARD, MONUMENT_HARD, MONUMENT_HARD_LIFT),
}


def _tier_identity(family: str, difficulty: str, on_ink: bool) -> tuple[str, str]:
    """返回这一档的 (脊线色, 文字色)，按所在表面是浅底还是墨底挑值。"""
    bar, bar_dark, text, text_dark = TIER_IDENTITY.get((family, difficulty), TIER_IDENTITY[(family, "normal")])
    return (bar_dark, text_dark) if on_ink else (bar, text)


# 回响赛季评级章（官方评级图 204×96 透明 PNG；C 为 189×96）。
# 按星数与追加目标自动选章，缺图时总览回退星标刻度。
WAR_RATING_ASSET_DIR = IMAGE_DIR / "challenges" / "war_rating"
# 轮换关卡组的官方三星素材：未点亮 / 点亮 / 点亮且追加目标全完成（金色六芒变体）。
WAR_STAR_ASSET_DIR = IMAGE_DIR / "challenges" / "war_star"
WAR_STAR_FILES = {"empty": "empty.png", "lit": "lit.png", "lit_plus": "lit_plus.png"}
# 战绩条底衬：bg_seasontower_degree.png（纯白圆角 + 外阴影）按 Unity Sliced 与
# #1B1B1B×0.851 逐通道乘法染色烘焙而成，见 scripts/bake_endfield_war_plate.py。
WAR_PLATE_ASSET = IMAGE_DIR / "challenges" / "war_plate" / "degree.png"
# 贴片左侧菱形纹章：deco_seasontower_simple_2，Unity 白色 × alpha 0.502。
WAR_DECO_ASSET = WAR_PLATE_ASSET.with_name("deco_left.png")
# 干员潜能差分 icon（与账号卡共用）：potential_0.png … potential_5.png
POTENTIAL_ICON_DIR = UI_DIR
WAR_RATING_FILES = {
    "unrated": "unrated.png",
    "d": "d.png",
    "c": "c.png",
    "b": "b.png",
    "a": "a.png",
    "s": "s.png",
    "s_plus": "s_plus.png",
}


async def draw_monument_overview(identity: ChallengeIdentity, payload: MonumentPayload, *, variant: str = DEFAULT_VARIANT) -> bytes:
    if not payload.has_records:
        return await draw_challenge_empty(identity, "影拓丰碑", query="暂无个人挑战记录", variant=variant)
    group = payload.current()
    assets = await _prepare_assets(_asset_urls_monument(payload, group), inline=False)
    return await _render_challenge(
        _monument_overview_html(identity, payload, group, variant, assets.urls),
        assets.resources,
        kind="monument_overview",
    )


async def draw_monument_detail(
    identity: ChallengeIdentity,
    group: MonumentGroup,
    dungeon: MonumentDungeon,
    *,
    variant: str = DEFAULT_VARIANT,
) -> bytes:
    assets = await _prepare_assets(_asset_urls_monument_detail(group, dungeon), inline=False)
    return await _render_challenge(
        _monument_detail_html(identity, group, dungeon, variant, assets.urls),
        assets.resources,
        kind="monument_detail",
    )


async def draw_monument_history(
    identity: ChallengeIdentity,
    groups: Sequence[MonumentGroup],
    *,
    page: int,
    page_count: int,
    variant: str = DEFAULT_VARIANT,
) -> bytes:
    if not _monument_history_has_records(groups):
        return await draw_challenge_empty(identity, "影拓丰碑", query="暂无历史主题记录", variant=variant)
    assets = await _prepare_challenge_assets(
        _asset_urls_monument_history(groups),
        kind="monument_history",
        page=page,
        page_count=page_count,
    )
    return await _render_challenge(
        _monument_history_html(identity, groups, page, page_count, variant, assets.urls),
        assets.resources,
        kind="monument_history",
        page=page,
        page_count=page_count,
    )


async def draw_monument_history_pages(
    identity: ChallengeIdentity,
    pages: Sequence[Sequence[MonumentGroup]],
    *,
    variant: str = DEFAULT_VARIANT,
) -> tuple[bytes, ...]:
    """Prepare archive assets once and export pages serially on the browser worker."""
    normalized_pages = tuple(tuple(groups) for groups in pages)
    if not normalized_pages:
        return ()
    all_groups = tuple(group for groups in normalized_pages for group in groups)
    assets = await _prepare_challenge_assets(
        _asset_urls_monument_history(all_groups),
        kind="monument_history_all",
        page_count=len(normalized_pages),
    )
    rendered: list[bytes] = []
    for index, groups in enumerate(normalized_pages, 1):
        if _monument_history_has_records(groups):
            document = _monument_history_html(
                identity, groups, index, len(normalized_pages), variant, assets.urls
            )
            resources = assets.resources
        else:
            document = _empty_html(identity, "影拓丰碑", "暂无历史主题记录", variant)
            resources = {}
        rendered.append(
            await _render_challenge(
                document,
                resources,
                kind="monument_history",
                page=index,
                page_count=len(normalized_pages),
            )
        )
    return tuple(rendered)


async def draw_war_overview(identity: ChallengeIdentity, payload: WarEchoPayload, *, variant: str = DEFAULT_VARIANT) -> bytes:
    if not payload.has_records:
        return await draw_challenge_empty(identity, "战争回响", query="暂无个人挑战记录", variant=variant)
    season = payload.current()
    assets = await _prepare_assets(_asset_urls_war(payload, season), inline=False)
    return await _render_challenge(
        _war_overview_html(identity, payload, season, variant, assets.urls),
        assets.resources,
        kind="war_overview",
    )


async def draw_war_detail(
    identity: ChallengeIdentity,
    season: WarSeason,
    week: WarWeek,
    group: WarGroup,
    dungeon: WarDungeon,
    *,
    variant: str = DEFAULT_VARIANT,
) -> bytes:
    assets = await _prepare_assets(_asset_urls_war_detail(season, group, dungeon), inline=False)
    return await _render_challenge(
        _war_detail_html(identity, season, week, group, dungeon, variant, assets.urls),
        assets.resources,
        kind="war_detail",
    )


async def draw_war_history(
    identity: ChallengeIdentity,
    season: WarSeason,
    *,
    page: int,
    page_count: int,
    achievements: Sequence[WarAchievement] = (),
    variant: str = DEFAULT_VARIANT,
) -> bytes:
    if not _war_history_has_records(season, achievements):
        return await draw_challenge_empty(identity, "战争回响", query="暂无历史赛季记录", variant=variant)
    assets = await _prepare_challenge_assets(
        _asset_urls_war_history((season,)),
        kind="war_history",
        page=page,
        page_count=page_count,
    )
    return await _render_challenge(
        _war_history_html(identity, season, page, page_count, achievements, variant, assets.urls),
        assets.resources,
        kind="war_history",
        page=page,
        page_count=page_count,
    )


async def draw_war_history_pages(
    identity: ChallengeIdentity,
    seasons: Sequence[WarSeason],
    *,
    achievements: Sequence[WarAchievement] = (),
    variant: str = DEFAULT_VARIANT,
) -> tuple[bytes, ...]:
    """Fetch shared history assets once, then render every season in page order."""
    normalized_seasons = tuple(seasons)
    if not normalized_seasons:
        return ()
    assets = await _prepare_challenge_assets(
        _asset_urls_war_history(normalized_seasons),
        kind="war_history_all",
        page_count=len(normalized_seasons),
    )
    rendered: list[bytes] = []
    for index, season in enumerate(normalized_seasons, 1):
        if _war_history_has_records(season, achievements):
            document = _war_history_html(
                identity,
                season,
                index,
                len(normalized_seasons),
                achievements,
                variant,
                assets.urls,
            )
            resources = assets.resources
        else:
            document = _empty_html(identity, "战争回响", "暂无历史赛季记录", variant)
            resources = {}
        rendered.append(
            await _render_challenge(
                document,
                resources,
                kind="war_history",
                page=index,
                page_count=len(normalized_seasons),
            )
        )
    return tuple(rendered)


async def draw_challenge_empty(
    identity: ChallengeIdentity,
    kind: str,
    *,
    query: str = "",
    variant: str = DEFAULT_VARIANT,
) -> bytes:
    return await _render_challenge(
        _empty_html(identity, kind, query, variant),
        {},
        kind="challenge_empty",
    )


async def _render_challenge(
    document: str,
    resources: dict[str, Any],
    *,
    kind: str,
    page: int = 0,
    page_count: int = 0,
) -> bytes:
    html_path = _write_temp_html(document)
    try:
        screenshot_started = perf_counter()
        try:
            output = await screenshot_web_element(
                html_path.resolve().as_uri(),
                ".challenge-card",
                viewport=(CHALLENGE_CARD_WIDTH, CHALLENGE_CARD_HEIGHT),
                timeout_ms=25000,
                # 高度不封顶：内容多了就往下长。这里只留一个失控兜底值。
                max_height=CHALLENGE_CARD_MAX_HEIGHT,
                device_scale_factor=1.0,
                settle_ms=60,
                resources=resources,
                wait_for_images=True,
                wait_for_fonts=True,
                resource_wait_timeout_ms=5000,
                screenshot_timeout_ms=60000,
                # Playwright 的 page.screenshot 会再次无界等待 document.fonts.ready；
                # 挑战页先做有界等待，再直接使用 Chromium 截图，避免字体状态卡死。
                screenshot_backend="cdp",
                strict_max_height=True,
            )
        except Exception as exc:
            logger.opt(exception=exc).error(
                "[endfield-challenge] render failed "
                f"kind={kind} page={page}/{page_count} stage=screenshot "
                f"resources={len(resources)} error_type={type(exc).__module__}.{type(exc).__name__}"
            )
            raise
        optimize_started = perf_counter()
        try:
            optimized = await run_image_render(optimize_png_container, output)
        except Exception as exc:
            logger.opt(exception=exc).error(
                "[endfield-challenge] render failed "
                f"kind={kind} page={page}/{page_count} stage=png_optimize "
                f"resources={len(resources)} error_type={type(exc).__module__}.{type(exc).__name__}"
            )
            raise
        logger.info(
            "[endfield-challenge] render complete "
            f"kind={kind} page={page}/{page_count} resources={len(resources)} "
            f"screenshot={optimize_started - screenshot_started:.3f}s "
            f"png_optimize={perf_counter() - optimize_started:.3f}s "
            f"bytes={len(output)}->{len(optimized)}"
        )
        return optimized
    finally:
        schedule_temp_file_cleanup(html_path, delay_seconds=30)


async def _prepare_challenge_assets(
    urls: Sequence[str],
    *,
    kind: str,
    page: int = 0,
    page_count: int = 0,
):
    unique_urls = tuple(dict.fromkeys(url for url in urls if url))
    started = perf_counter()
    try:
        assets = await _prepare_assets(unique_urls, inline=False)
    except Exception as exc:
        logger.opt(exception=exc).error(
            "[endfield-challenge] render failed "
            f"kind={kind} page={page}/{page_count} stage=assets "
            f"resources={len(unique_urls)} error_type={type(exc).__module__}.{type(exc).__name__}"
        )
        raise
    logger.info(
        "[endfield-challenge] assets prepared "
        f"kind={kind} page={page}/{page_count} requested={len(unique_urls)} "
        f"resolved={len(assets.resources)} seconds={perf_counter() - started:.3f}"
    )
    return assets


def _monument_history_has_records(groups: Sequence[MonumentGroup]) -> bool:
    return any(
        group.stages
        and any(item.passed or item.record.available for pair in group.stages for item in pair)
        for group in groups
    )


def _war_history_has_records(
    season: WarSeason,
    achievements: Sequence[WarAchievement],
) -> bool:
    return bool(achievements) or any(
        dungeon.passed or dungeon.record.available
        for week in season.weeks
        for group in week.groups
        for dungeon in (group.normal, group.hard, group.cruel)
        if dungeon is not None
    )


def _monument_overview_html(identity, payload, group, variant, assets):
    if group is None:
        return _empty_html(identity, "影拓丰碑", "暂无可见主题", variant)
    normal = sum(1 for pair in group.stages if pair[0].passed)
    hard = sum(1 for pair in group.stages if pair[1].passed)
    total = len(group.stages)
    history = "".join(_monument_group_summary(item, group) for item in payload.groups)
    stages = "".join(_monument_stage_card(pair, assets, index) for index, pair in enumerate(group.stages, 1))
    medal_url = group.medal_plated_icon_url if group.medal_plated else group.medal_icon_url
    medal_src = assets.get(medal_url, "") if medal_url else ""
    medal_img = f'<img class="seal-img" src="{esc_attr(medal_src)}" alt="" />' if medal_src else ""
    passed_pct = round(group.passed_stages * 100 / group.total_stages) if group.total_stages else 0
    # 主题图是纸本水墨海报（960×1200 竖构图），整张供在海报位上，不裁切也不压暗幕。
    poster_src = _asset_src(group.pic_url, assets)
    poster = f'<img src="{esc_attr(poster_src)}" alt="" />' if poster_src else '<span class="poster-empty">主题图未返回</span>'
    banner = f'<div class="slab">{medal_img}<div><span class="slab-label">THEMED MEDAL</span><strong class="slab-title">{esc(group.medal_name or "主题奖章")}</strong><span class="slab-note">奖章等级 {group.medal_level or "--"} · {"已镀层" if group.medal_plated else "记录中"}</span></div></div>'
    meta = (
        f'{esc(group.activity_name or "常驻挑战")} · {esc(_period(group.start_ts, group.end_ts))} · {"活动中" if group.is_active else "历史主题"}'
        f' · {esc(identity.nickname or "未命名账号")} · {esc(identity.server_name or "国服")} · UID {esc(identity.uid or "--")}'
    )
    return _document(
        identity,
        f"影拓丰碑 · {group.name}",
        "",
        MONUMENT_ACCENT,
        _asset_src(group.pic_url, assets),
        variant,
        f"""
        <section class="overview-grid"><aside class="theme-rail"><div class="theme-poster">{poster}</div><div class="theme-figure"><span class="stage-eyebrow">MONUMENT / PROGRESS</span><b>{group.passed_stages}<i>/{group.total_stages}</i></b><div class="progress-track"><div class="progress-fill" style="width:{passed_pct}%"></div></div><ul class="stage-tiers"><li class="tier-normal"><span>普通</span><b>{normal}/{total}</b><small>{esc(_tier_note(group, 0))}</small></li><li class="tier-hard"><span>苦难</span><b>{hard}/{total}</b><small>{esc(_tier_note(group, 1))}</small></li></ul></div></aside><div class="overview-main"><article class="data-sheet stage-board"><div class="sheet-heading"><div><span>STAGE RECORDS</span><h2>关卡记录</h2></div></div><div class="ladder"><div class="ladder-head"><span>#</span><span>关卡</span><span class="head-tier">普通</span><span class="head-tier head-hard">苦难</span></div>{stages}</div></article><article class="index-sheet"><div class="sheet-heading"><div><span>SERIES INDEX</span><h2>历届主题</h2></div><small>{len(payload.groups)} 项</small></div><div class="history-grid">{history}</div></article></div></section>
        """,
        meta=meta,
        banner=banner,
        page="overview",
    )


def _best_cleared_html(tiers, order, label_of) -> tuple[str, str]:
    """题头大字答的是「这个组我最牛打到哪」：按难度从高到低取最高已通关档。

    与对照表当前行不再复读同一对数字——表答逐档明细，题头答代表作。
    """
    best = next(
        (item for difficulty in order for item in tiers if item.difficulty == difficulty and item.passed),
        None,
    )
    if best is None:
        return "--", "尚无通关记录"
    figure = _format_duration(best.record.pass_time) if best.record.pass_time > 0 else "用时未返回"
    stamp = best.record.record_ts or best.first_pass_ts
    tier_label = label_of(best.difficulty)
    note = f"{tier_label} · {_date(stamp)}" if stamp else tier_label
    return figure, note


def _monument_detail_html(identity, group, dungeon, variant, assets):
    """碑面双档详情：题头给代表作，工作台给同关两档记录、关卡机制与敌方情报。

    账号身份留在页眉，主题名留在页眉标题，本页正文只谈这一关；
    普通/苦难两档并置成对照表，当前查看的档位反相强调。
    """
    pair, position, total = _monument_stage_context(group, dungeon)
    difficulty_label = _monument_difficulty_label(dungeon.difficulty)
    best_figure, best_note = _best_cleared_html(pair, ("hard", "normal"), _monument_difficulty_label)
    features = _feature_items(dungeon.feature)
    eyebrow = f"MONUMENT / STAGE {position:02d} OF {total:02d}" if position else "MONUMENT / STAGE DOSSIER"
    diff = _feature_diff(dungeon, pair)
    feature_html = _feature_list_html(features, diff)
    legend = (
        f'<span class="h3-note"><i class="note-swatch" style="background:{_tier_identity("monument", dungeon.difficulty, False)[1]}"></i>{esc(_monument_difficulty_label(dungeon.difficulty))}独有</span>'
        if diff
        else ""
    )
    enemies = "".join(_enemy_html(enemy, assets) for enemy in dungeon.enemies[:6]) or '<span class="muted">暂无敌方情报</span>'
    return _document(
        identity,
        f"影拓丰碑 · {group.name}",
        "",
        MONUMENT_ACCENT,
        _asset_src(group.pic_url, assets),
        variant,
        f"""
        <section class="detail-hero"><div><span class="eyebrow">{eyebrow}</span><h1>{esc(_display_stage_name(dungeon.name))}</h1><p>{difficulty_label}难度{' · 尚未通关' if not dungeon.passed else ''}</p></div><div class="slab"><div><span class="slab-label">BEST CLEARED</span><strong class="slab-figure">{esc(best_figure)}</strong><span class="slab-note">{esc(best_note)}</span></div></div></section>
        <section class="detail-workbench"><article class="duo-sheet data-sheet"><div class="sheet-heading"><div><span>DEPLOYED RECORDS</span><h2>本关双档记录</h2></div><small>主题进度 {group.passed_stages}/{group.total_stages}</small></div><div class="duo-list">{_monument_duo_rows(pair, dungeon, assets)}</div><div class="duo-foot">主题周期 {esc(_period(group.start_ts, group.end_ts))} · {"活动中" if group.is_active else "历史主题"}</div></article><article class="intel-sheet data-sheet"><div class="sheet-heading"><div><span>STAGE DOSSIER</span><h2>关卡档案</h2></div><small>公开关卡资料</small></div><div class="intel-copy"><section><h3>关卡描述</h3><p>{_multiline(dungeon.desc) or '暂无公开描述'}</p></section><section><h3>机制特性{legend}</h3><ul class="feature-list">{feature_html}</ul></section></div></article><aside class="enemy-sheet data-sheet"><div class="sheet-heading"><div><span>HOSTILE UNITS</span><h2>敌方情报</h2></div><small>{len(dungeon.enemies)} 个单位</small></div><div class="enemy-grid">{enemies}</div></aside></section>
        """,
        page="detail",
    )


def _monument_stage_context(group, dungeon):
    """取回同关的普通/苦难两档，以及它在本主题里的序号。"""
    for index, pair in enumerate(group.stages, 1):
        if any(item.id == dungeon.id or item.name == dungeon.name for item in pair):
            return pair, index, len(group.stages)
    return (dungeon,), 0, len(group.stages)


def _monument_duo_rows(pair, current, assets):
    return "".join(
        _monument_duo_row(item, item.id == current.id or item.name == current.name, assets) for item in pair
    )


def _monument_duo_row(dungeon, is_current, assets):
    """一档一行：难度名做主字（苦难用官方玩法条红做身份色），推荐等级降为副行，
    记录日期与用时叠成右上角一栈（用时为大字），配队按栏宽铺满整行。

    未通关不再画 ✓ 框——没有记录本身就是答案，用时位写「未记录」。
    """
    record = dungeon.record
    duration = _format_duration(record.pass_time) if record.pass_time > 0 else "未记录"
    stamp = record.record_ts or record.first_pass_ts
    when = _date(stamp) if stamp else "暂无时间"
    reco = f'<small class="duo-reco">推荐 Lv.{dungeon.recommend_level}</small>' if dungeon.recommend_level else ""
    bar, text = _tier_identity("monument", dungeon.difficulty, is_current)
    slots = [_duo_member_html(item, assets) for item in record.members[:4]]
    if slots:
        slots += [_duo_member_html(None, assets)] * (4 - len(slots))
        team_html = f'<div class="duo-team">{"".join(slots)}</div>'
    else:
        # 整档没记录时不画四个空框（那看着像渲染故障），直接说明没有配队记录。
        team_html = '<div class="duo-note">未留下配队记录</div>'
    return (
        f'<div class="duo-row{" is-current" if is_current else ""}" style="--tier-bar:{bar};--tier-text:{text}">'
        f'<div class="duo-top"><div class="duo-label"><b class="duo-tier">{_monument_difficulty_label(dungeon.difficulty)}</b>{reco}</div>'
        f'<div class="duo-figure"><small class="duo-when">{esc(when)}</small><b class="duo-time">{esc(duration)}</b></div></div>'
        f'{team_html}</div>'
    )


def _duo_member_html(member, assets):
    if member is None:
        return '<span class="duo-member"><span class="team-avatar is-empty"></span></span>'
    url = _asset_src(member.avatar_url, assets)
    image = f'<img class="avatar-img" src="{esc_attr(url)}" alt="" />' if url else ""
    # 头像没取到时连潜能角标一起留空：空槽里飘个星标会读成坏卡。
    pip = _potential_pip(member.potential) if image else ""
    return (
        f'<span class="duo-member"><span class="team-avatar{" is-empty" if not image else ""}" '
        f'title="Lv.{member.level} 潜能{member.potential}">{image}{pip}</span></span>'
    )


def _feature_items(value, limit: int = 6):
    """官方 feature 是用 " - " 串起来的一整段，拆成条目而不是压成流水账。"""
    text = _plain(value)
    if not text:
        return ()
    parts = re.split(r"(?:^|(?<=[。！？]))\s*[-–]\s*", text)
    items = [part.strip(" 。") for part in parts if part.strip(" 。")]
    if len(items) < 2 and " - " in text:
        items = [part.strip(" 。") for part in text.split(" - ") if part.strip(" 。")]
    return tuple(esc(item[:150]) for item in items[:limit]) or (esc(text[:150]),)


def _feature_list_html(features, diff) -> str:
    """一条列表讲完机制：标记形状全部一致，只有本档独有的条目染成苦难红。"""
    marked = set(diff)
    items = "".join(
        '<li class="is-diff">{}</li>'.format(item) if item in marked else f"<li>{item}</li>" for item in features
    )
    return items or '<li class="muted">暂无公开机制说明</li>'


def _feature_diff(dungeon, tiers):
    """本档独有、其余各档都没有的规则条目。

    战争回响传进来的是三档，所以取「不在任何兄弟档里出现」的并集；
    丰碑传进来只有一对，结果与两档相减一致。
    """
    others = [item for item in tiers if item.id != dungeon.id and item.name != dungeon.name]
    if not others:
        return ()
    mine = _feature_items(dungeon.feature, limit=12)
    theirs: set[str] = set()
    for item in others:
        theirs.update(_feature_items(item.feature, limit=12))
    return tuple(entry for entry in mine if entry not in theirs)


def _monument_history_width() -> int:
    """按内容算卡宽：写死 1920 会让两档配队后面各拖出一条大空隙。

    这些数字与 CSS 里的 .archive-* / .history-record 轨道一一对应，改哪边都要同步。
    """
    avatar, avatar_gap = 52, 6
    cell_gap, rule_offset = 12, 13
    time_track, date_track, name_track = 84, 132, 190
    meta_panel, table_padding, card_padding, border = 268, 16, 54, 2
    team = 4 * avatar + 3 * avatar_gap
    tier = time_track + cell_gap + date_track + cell_gap + team
    table = 2 * table_padding + name_track + cell_gap + tier + cell_gap + tier + rule_offset
    return meta_panel + table + border + 2 * card_padding


def _monument_history_html(identity, groups, page, page_count, variant, assets):
    blocks = "".join(_monument_history_block(group, assets) for group in groups)
    # 分页挪进页眉右侧：那条墨带右边本来就空着，单独占一行 band 太浪费。
    # 只报分页事实，不重复页眉标题，也不写会被读成档案编号的本页条数。
    pager = (
        f'<div class="archive-pager"><span>MONUMENT / ARCHIVE</span>'
        f'<b>{page}<i> / </i>{page_count}<em> 页</em></b>'
        f'<small>本页 {len(groups)} 个主题 · 每页 2 个主题</small></div>'
    )
    return _document(
        identity,
        "影拓丰碑 · 历史",
        "",
        MONUMENT_ACCENT,
        # 历史页管的是多个主题，借第一个主题的 KV 当全页背景讲不通；
        # 页面只留混凝土+蓝图网格这层统一地面，主题身份由每块档案头自己的奖章承担。
        "",
        variant,
        f'''<section class="monument-archive-stack">{blocks or '<div class="data-sheet empty-panel">暂无可见历史记录</div>'}</section>''',
        banner=pager,
        card_width=_monument_history_width(),
        page="history",
    )


def _war_overview_html(identity, payload, season, variant, assets):
    if season is None:
        return _empty_html(identity, "战争回响", "暂无可见赛季", variant)
    week = season.current_week() or (season.weeks[-1] if season.weeks else None)
    groups = week.groups if week else ()
    stages = "".join(_war_stage_card(group, assets, index) for index, group in enumerate(groups, 1))
    achievements = "".join(f'<span class="achievement-chip"><span>{esc(item.name)}</span>{_star_marks(item.star)}</span>' for item in payload.achievements[:8])
    stars = "".join(f'<i class="star{" on" if index < season.stars else ""}">★</i>' for index in range(9))
    banner = _war_rating_html(season) or f'<div class="stars">{stars}</div>'
    meta = (
        f'{esc(week.name if week else "暂无轮换")} · {"赛季进行中" if season.current() else "历史赛季"}'
        f' · 全部追加目标 {"已完成" if season.all_plus_tasks else "未全清"}'
        f' · {esc(identity.nickname or "未命名账号")} · {esc(identity.server_name or "国服")} · UID {esc(identity.uid or "--")}'
    )
    return _document(
        identity,
        f"战争回响 · {season.name}",
        "",
        WAR_ACCENT,
        _asset_src(season.header_url or season.kv_url, assets),
        variant,
        f"""
        <section class="overview-stack"><article class="data-sheet stage-board"><div class="sheet-heading"><div><span>CURRENT ROTATION</span><h2>{esc(week.name if week else '当前轮换')}</h2></div><div class="board-side"><small>仅显示最高通关难度</small><div class="board-stats"><span class="stat"><b>{len(groups)}</b><small>关卡组</small></span><span class="stat"><b>{len(payload.achievements)}</b><small>荣誉</small></span><span class="stat"><b>{len(season.weeks)}<i> 期</i></b><small>轮换周期</small></span></div></div></div><div class="war-stage-grid">{stages or '<div class="muted">暂无轮换记录</div>'}</div></article><article class="data-sheet honors-band"><div class="sheet-heading"><div><span>HONORS INDEX</span><h2>荣誉记录</h2></div><small>{len(payload.achievements)} 项</small></div><div class="achievement-grid honors-cols4">{achievements or '<span class="muted">暂无荣誉记录</span>'}</div></article></section>
        """,
        meta=meta,
        banner=banner,
    )


def _war_detail_html(identity, season, week, group, dungeon, variant, assets):
    """战争回响详情：三档对照。同一关卡组的普通/困难/残酷并置，
    每档带自己的推荐等级、用时、首通、追加目标与配队；星级是关卡组的，留在题头。
    """
    tiers = tuple(item for item in (group.normal, group.hard, group.cruel) if item is not None)
    features = _feature_items(dungeon.feature)
    diff = _feature_diff(dungeon, tiers)
    legend = (
        f'<span class="h3-note"><i class="note-swatch" style="background:{_tier_identity("war", dungeon.difficulty, False)[1]}"></i>'
        f'{esc(_difficulty_label(dungeon.difficulty))}独有</span>'
        if diff
        else ""
    )
    enemies = "".join(_enemy_html(enemy, assets) for enemy in dungeon.enemies[:6]) or '<span class="muted">暂无敌方情报</span>'
    banner = _war_rating_html(season)
    best_figure, best_note = _best_cleared_html(tiers, ("cruel", "hard", "normal"), _difficulty_label)
    position = next((index + 1 for index, item in enumerate(week.groups) if item is group or item.name == group.name), 0)
    eyebrow = f"WAR / GROUP {position:02d} OF {len(week.groups):02d}" if position else "WAR / STAGE DOSSIER"
    meta = (
        f'{esc(season.name)} · {esc(week.name)} · {"赛季进行中" if season.current() else "历史赛季"}'
        f' · {esc(identity.nickname or "未命名账号")} · {esc(identity.server_name or "国服")} · UID {esc(identity.uid or "--")}'
    )
    return _document(
        identity,
        f"战争回响 · {week.name}",
        "",
        WAR_ACCENT,
        _asset_src(season.header_url or season.kv_url, assets),
        variant,
        f"""
        <section class="detail-hero"><div><span class="eyebrow">{eyebrow}</span><h1>{esc(_display_stage_name(group.name))}</h1><p>{_difficulty_label(dungeon.difficulty)}难度 · 星级 {group.star}/3 · 追加目标 {"已完成" if group.plus_task else "未完成"}</p></div><div class="slab"><div><span class="slab-label">BEST CLEARED</span><strong class="slab-figure">{esc(best_figure)}</strong><span class="slab-note">{esc(best_note)}</span></div></div></section>
        <section class="detail-workbench"><article class="duo-sheet data-sheet"><div class="sheet-heading"><div><span>DEPLOYED RECORDS</span><h2>本组三档记录</h2></div><small>轮换 {esc(_period(week.start_ts, week.end_ts))}</small></div><div class="duo-list">{_war_tier_rows(tiers, dungeon, assets)}</div></article><article class="intel-sheet data-sheet"><div class="sheet-heading"><div><span>STAGE DOSSIER</span><h2>关卡档案</h2></div><small>公开关卡资料</small></div><div class="intel-copy"><section><h3>关卡描述</h3><p>{_multiline(dungeon.desc) or '暂无公开描述'}</p></section><section><h3>机制特性{legend}</h3><ul class="feature-list">{_feature_list_html(features, diff)}</ul></section></div></article><aside class="enemy-sheet data-sheet"><div class="sheet-heading"><div><span>HOSTILE UNITS</span><h2>敌方情报</h2></div><small>{len(dungeon.enemies)} 个单位</small></div><div class="enemy-grid">{enemies}</div></aside></section>
        """,
        meta=meta,
        banner=banner,
        page="detail",
    )


def _war_tier_rows(tiers, current, assets):
    # 三档的 name 往往完全相同，只能按对象身份 + 难度判定当前档，
    # 否则三行会一起被标成 is-current。
    return "".join(
        _war_tier_row(
            item,
            item is current or (bool(item.id) and item.id == current.id and item.difficulty == current.difficulty),
            assets,
        )
        for item in tiers
    )


def _war_tier_row(dungeon, is_current, assets):
    record = dungeon.record
    bar, text = _tier_identity("war", dungeon.difficulty, is_current)
    reco = f'<small class="duo-reco">推荐 Lv.{dungeon.recommend_level}</small>' if dungeon.recommend_level else ""
    if record.available:
        duration = _format_duration(record.pass_time) if record.pass_time > 0 else ("用时未返回" if dungeon.passed else "未通关")
        stamp = record.record_ts or record.first_pass_ts
    else:
        # 过了但没给快照：说的是「未返回」，不是没打。
        duration = "用时未返回" if dungeon.passed else "未通关"
        stamp = dungeon.first_pass_ts
    when = _date(stamp) if stamp else "--"
    if record.members:
        slots = [_duo_member_html(item, assets) for item in record.members[:4]]
        slots += [_duo_member_html(None, assets)] * (4 - len(slots))
        team_html = f'<div class="duo-team">{"".join(slots)}</div>'
    else:
        team_html = '<div class="duo-note">未留下配队记录</div>'
    return (
        f'<div class="duo-row{" is-current" if is_current else ""}" style="--tier-bar:{bar};--tier-text:{text}">'
        f'<div class="duo-top"><div class="duo-label"><b class="duo-tier">{_difficulty_label(dungeon.difficulty)}</b>{reco}</div>'
        f'<div class="duo-figure"><small class="duo-when">{esc(when)}</small><b class="duo-time">{esc(duration)}</b></div></div>'
        f'{team_html}</div>'
    )


def _war_history_html(identity, season, page, page_count, achievements, variant, assets):
    weeks = "".join(_war_week_block(week, assets) for week in season.weeks)
    medals = "".join(f'<span class="achievement-chip"><span>{esc(item.name)}</span>{_star_marks(item.star)}</span>' for item in achievements if item.name)
    stars = "".join(f'<i class="star{" on" if index < season.stars else ""}">★</i>' for index in range(9))
    banner = _war_rating_html(season) or f'<div class="stars">{stars}</div>'
    meta = (
        f'{"追加目标完成" if season.all_plus_tasks else "追加目标未全清"} · {esc(_period(season.start_ts, season.end_ts))}'
        f' · 第 {page} / {page_count} 页 · {len(season.weeks)} 个轮换周期'
        f' · {esc(identity.nickname or "未命名账号")} · {esc(identity.server_name or "国服")} · UID {esc(identity.uid or "--")}'
    )
    # 画布宽度跟着卡宽走；高度不封顶，轮换多了就往下排。
    honors_cls = "achievement-grid honors-cols4"
    avatar = 104
    groups = max((len(week.groups) for week in season.weeks), default=1)
    card = 4 * avatar + 3 * 8 + 22
    week_panel = groups * card + (groups - 1) * 20 + 30
    width = week_panel + 108
    return _document(
        identity,
        f"战争回响 · {season.name}",
        "",
        WAR_ACCENT,
        _asset_src(season.header_url or season.kv_url, assets),
        variant,
        f'''<section class="overview-stack"><div class="war-week-stack">{weeks or '<div class="data-sheet empty-panel">暂无轮换记录</div>'}</div><article class="data-sheet honors-band"><div class="sheet-heading"><div><span>HONORS INDEX</span><h2>荣誉记录</h2></div><small>{len(achievements)} 项</small></div><div class="{honors_cls}">{medals or '<span class="muted">暂无荣誉记录</span>'}</div></article></section>''',
        meta=meta,
        banner=banner,
        card_width=width,
    )


def _document(identity, title, kicker, accent, hero_url, variant, body, *, meta="", banner="", card_width: int = 0, page: str = ""):
    variant = variant if variant in CHALLENGE_VARIANTS else DEFAULT_VARIANT
    kind = "kind-war" if str(accent).casefold() == WAR_ACCENT else "kind-monument"
    hero = esc_attr(hero_url or "")
    size = f' style="--card-width:{int(card_width)}px"' if card_width else ""
    page_class = f" page-{page}" if page else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>{_css(variant, accent, hero)}</style></head><body><main class="challenge-card variant-{variant} {kind}{page_class}"{size}>{_header(identity, title, kicker, meta=meta, banner=banner)}<div class="hero-image" aria-hidden="true"></div><div class="challenge-content">{body}</div><footer class="challenge-footer"><span>森空岛 · 个人挑战档案</span><span>更新时间 {esc(identity.updated_at or _date(int(time())))}</span></footer></main></body></html>"""


def _header(identity, title, kicker="", meta="", banner=""):
    uid = identity.uid or "--"
    kicker_html = f'<span class="header-kicker">{esc(kicker)}</span>' if kicker else ""
    info = meta or f'{esc(identity.nickname or "未命名账号")} · {esc(identity.server_name or "国服")} · UID {esc(uid)}'
    # 有右侧横幅（奖章条 / 评级章）时页眉升高一档，否则 150px 会把横幅挤扁。
    header_class = "challenge-header has-banner" if banner else "challenge-header"
    return f'<header class="{header_class}"><div><span class="brand-mark">ENDFIELD / ACCOUNT DATA</span>{kicker_html}<h2>{esc(title)}</h2><p>{info}</p></div>{banner}</header>'


def _css(variant: str, accent: str, hero: str) -> str:
    """Fixed-canvas challenge card stylesheet.

    Palette follows the plugin house style: concrete gradient + blueprint grid,
    ink panels (#20252a) and one accent per mode (amber monument / steel-blue
    war echoes). The accent also carries a text-safe deep companion used for
    kickers on light surfaces.
    """
    variant = variant if variant in CHALLENGE_VARIANTS else DEFAULT_VARIANT
    settings = {
        "a": ("20%", ".30"),
        "b": ("34%", ".42"),
        "c": ("55%", ".58"),
    }.get(variant, ("34%", ".42"))
    accent_deep = {MONUMENT_ACCENT: MONUMENT_ACCENT_DEEP, WAR_ACCENT: WAR_ACCENT_DEEP}.get(accent, accent)
    css = """
    *{box-sizing:border-box}html,body{margin:0;width:var(--card-width,1920px);height:auto}
    body{font-family:'Microsoft YaHei','PingFang SC','Noto Sans SC',Arial,sans-serif;color:#171b1f;background:#d9dde0}
    .challenge-card{--accent:__ACCENT__;--accent-deep:__ACCENT_DEEP__;--hard:__HARD__;--kv:__HERO__;--hero-width:__HERO_WIDTH__;--hero-opacity:__HERO_OPACITY__;position:relative;width:var(--card-width,1920px);min-height:640px;padding:42px 54px 46px;overflow:hidden;background:linear-gradient(90deg,rgba(23,27,31,.07) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(0deg,rgba(23,27,31,.07) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(135deg,#f7f8f4 0%,#e7eaeb 58%,#cfd5d9 100%)}
    .challenge-header{position:relative;z-index:3;height:150px;display:flex;justify-content:space-between;align-items:center;padding:0 18px;background:linear-gradient(90deg,rgba(28,33,38,.975) 0%,rgba(28,33,38,.94) 58%,rgba(28,33,38,.80) 100%);border-bottom:6px solid var(--accent)}
    .challenge-header.has-banner{height:180px}
    .challenge-header>div:first-child{min-width:0}
    .brand-mark{display:block;color:#8d989e;font:900 12px/1.2 Consolas,'Microsoft YaHei',monospace;letter-spacing:.16em}
    .header-kicker{display:block;margin-top:9px;color:var(--accent);font:900 12px/1.2 Consolas,'Microsoft YaHei',monospace;letter-spacing:.13em;overflow-wrap:anywhere}
    .challenge-header h2{margin:7px 0 3px;font-size:37px;line-height:1.05;font-weight:900;letter-spacing:.01em;color:#fff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .challenge-header.has-banner h2{margin:10px 0 8px;font-size:60px}
    .challenge-header p{margin:0;color:#aeb6ba;font-size:16px;font-weight:800}
    .challenge-header.has-banner p{color:#9aa4a9;font-size:17px}
    .hero-image{position:absolute;z-index:1;right:0;top:0;bottom:0;height:auto;width:var(--hero-width);background-image:linear-gradient(0deg,rgba(23,29,32,.14),rgba(23,29,32,0) 46%),__HERO__;background-position:center top;background-size:cover;opacity:var(--hero-opacity);filter:saturate(.86) contrast(1.06);-webkit-mask-image:linear-gradient(90deg,transparent 0%,rgba(0,0,0,.30) 30%,#000 66%,#000 100%);mask-image:linear-gradient(90deg,transparent 0%,rgba(0,0,0,.30) 30%,#000 66%,#000 100%)}
    .challenge-content{position:relative;z-index:3;padding-top:16px;display:flex;flex-direction:column}
    .challenge-footer{position:absolute;z-index:3;left:54px;right:54px;bottom:11px;display:flex;justify-content:space-between;color:#7d888d;font:900 11px/1 Consolas,monospace;letter-spacing:.06em}
    .slab{flex:0 0 auto;display:flex;align-items:center;gap:18px;min-width:340px;max-width:560px;padding:14px 22px;background:#2a3136;border:1px solid rgba(255,255,255,.12);border-bottom:6px solid var(--accent);box-shadow:0 14px 30px rgba(23,27,31,.20)}
    .slab-label{display:block;color:var(--accent);font:900 11px/1 Consolas,monospace;letter-spacing:.16em}
    .slab-figure{display:block;margin-top:7px;font:900 44px/1 Consolas,monospace;color:#fff}
    .slab-figure i{font-style:normal;font-size:.48em;color:#9aa4a9}
    .slab-title{display:block;margin-top:7px;font-size:22px;line-height:1.15;font-weight:900;color:#fff;overflow-wrap:anywhere}
    .slab-note{display:block;margin-top:6px;color:#aab3b7;font-size:12px;line-height:1.35;font-weight:800}
    .seal-img{width:72px;height:72px;flex:0 0 auto;object-fit:contain;filter:drop-shadow(0 6px 8px rgba(0,0,0,.35))}
    .seal-rating{height:72px;width:auto;flex:0 0 auto;object-fit:contain;filter:drop-shadow(0 6px 10px rgba(0,0,0,.35))}
    .stars{display:flex;gap:5px;margin-top:6px}
    .star{width:26px;height:26px;display:grid;place-items:center;font-style:normal;font-size:22px;line-height:1;color:#454f55}
    .star.on{color:var(--accent)}

    /* 影拓丰碑总览：左栏海报位整张供主题图，墨色台座压进度 */
    .page-overview .hero-image{display:none}
    .theme-rail{min-width:0;display:grid;grid-template-rows:auto auto;gap:14px;align-content:start}
    .theme-poster{position:relative;aspect-ratio:960/1200;display:grid;place-items:center;overflow:hidden;background:#f1f2ee;border:1px solid rgba(23,27,31,.25);box-shadow:inset 0 5px 0 #20252a}
    .theme-poster img{width:100%;height:100%;object-fit:cover}
    .poster-empty{color:#879297;font-size:13px;font-weight:900}
    .theme-figure{position:relative;padding:16px 18px 18px;color:#fff;background:#20252a;border:1px solid rgba(23,27,31,.25);box-shadow:inset 0 5px 0 var(--accent)}
    .stage-eyebrow{display:block;color:var(--accent);font:900 11px/1 Consolas,monospace;letter-spacing:.16em}
    .theme-figure b{display:block;margin-top:10px;font:900 76px/.9 Consolas,monospace;letter-spacing:-.03em;color:#fff}
    .theme-figure b i{font-size:.42em;font-style:normal;color:rgba(255,255,255,.55)}
    .theme-figure .progress-track{margin-top:12px;height:11px;background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.26)}
    .theme-figure .progress-fill{height:100%;background:var(--accent)}
    .stage-tiers{margin:14px 0 0;padding:0;list-style:none;display:flex;flex-direction:column;gap:6px}
    .stage-tiers li{display:flex;align-items:baseline;gap:10px;padding:7px 10px;background:rgba(255,255,255,.055);border-left:4px solid rgba(255,255,255,.30)}
    .stage-tiers .tier-hard{border-left-color:var(--hard)}
    .stage-tiers span{flex:0 0 auto;color:#fff;font-size:15px;font-weight:900;letter-spacing:.05em}
    .stage-tiers .tier-hard span{color:#ff8d7f}
    .stage-tiers b{font:900 22px/1 Consolas,monospace;color:#fff}
    .stage-tiers small{margin-left:auto;color:rgba(255,255,255,.6);font-size:12px;font-weight:850;letter-spacing:.02em}
    .progress-track{margin-top:9px;height:13px;background:#dde2e2;border:1px solid rgba(23,27,31,.20)}
    .progress-fill{height:100%;background:var(--accent)}

    /* overview board + aside */
    .overview-grid{flex:1;min-height:0;display:grid;grid-template-columns:420px minmax(0,1fr);gap:14px}
    .data-sheet,.index-sheet{min-width:0;min-height:0;border:1px solid rgba(23,27,31,.25);background:rgba(249,250,248,.955);box-shadow:inset 0 5px 0 #20252a;overflow:hidden}
    .stage-board{display:grid;grid-template-rows:auto minmax(0,1fr);padding:13px 18px 12px}
    .overview-main{min-width:0;min-height:0;display:grid;grid-template-rows:minmax(0,1fr) auto;gap:14px}
    .index-sheet{display:grid;grid-template-rows:auto minmax(0,1fr);padding:13px 16px 11px}
    .sheet-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;padding-bottom:9px;border-bottom:2px solid rgba(23,27,31,.55)}
    .sheet-heading span{display:block;color:#7b868b;font:900 11px/1 Consolas,'Microsoft YaHei',monospace;letter-spacing:.13em}
    .sheet-heading h2{margin:4px 0 0;font-size:23px;line-height:1;font-weight:900}
    .sheet-heading small{color:#7b868b;font-size:12px;font-weight:900;text-align:right}
    .board-side{display:flex;align-items:flex-end;gap:22px}
    .board-stats{display:flex;align-items:baseline}
    .board-stats .stat{display:flex;align-items:baseline;gap:7px;padding:0 18px;border-left:1px solid rgba(23,27,31,.18)}
    .board-stats .stat:first-child{border-left:0}
    .board-stats b{font:900 30px/1 Consolas,monospace;color:#20272b}
    .board-stats b i{font-size:.5em;font-style:normal;color:#7d888d}
    .board-stats small{color:#707c82;font-size:12px;font-weight:900}

    /* monument ladder */
    .ladder{min-height:0;display:flex;flex-direction:column}
    .ladder-head{flex:0 0 34px;display:grid;grid-template-columns:44px minmax(120px,206px) minmax(0,1fr) minmax(0,1fr);gap:12px;align-items:center;color:#6d787d;font-size:16px;font-weight:900;letter-spacing:.06em}
    /* 用时单独占一轨，列头压在这一轨的正中——对齐的是速度数字，不是整组数据；
       第二列要额外让出 13px，那是 .ladder-cell+.ladder-cell 的 1px 分隔线 + 12px 内缩，
       不让的话列头会偏左半个轨宽（实测 14px）。字距归零，否则尾随空格会让光学中心再偏 1px。 */
    .ladder-head .head-tier{width:116px;justify-self:start;text-align:center;letter-spacing:0}
    .ladder-head .head-tier+.head-tier{width:129px;padding-left:13px}
    .ladder-head .head-hard{color:__HARD__}
    .ladder-row{flex:1 1 auto;min-height:64px;max-height:150px;display:grid;grid-template-columns:44px minmax(120px,206px) minmax(0,1fr) minmax(0,1fr);gap:12px;align-items:center;border-top:1px solid rgba(23,27,31,.13)}
    .ladder-row>span:first-child{color:#8a949a;font:900 17px/1 Consolas,monospace}
    .ladder-name{min-width:0;font-size:20px;font-weight:900;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .ladder-name small{display:block;margin-top:3px;color:#8a959b;font-size:13px;font-weight:800}
    .ladder-cell{min-width:0;align-self:stretch;height:100%;display:flex;align-items:center;justify-content:flex-start;gap:18px}
    .ladder-cell.is-blank{color:#98a2a6;font-size:14px;font-weight:900;letter-spacing:.02em}
    .ladder-cell.is-blank span{flex:0 0 116px;text-align:center}
    .ladder-cell+.ladder-cell{border-left:1px dashed rgba(23,27,31,.20);padding-left:12px}
    .ladder-time{flex:0 0 116px;text-align:center;font:900 22px/1 Consolas,monospace;color:#20272b}
    strong.missing-duration{color:#8a959b;font:900 13px/1.3 'Microsoft YaHei',sans-serif}

    /* war rotation dossiers */
    .war-stage-grid{min-height:0;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;padding-top:10px}
    .stage-dossier{min-width:0;min-height:0;display:grid;grid-template-rows:auto minmax(0,1fr);border:1px solid rgba(23,27,31,.20);background:#f1f3f2;overflow:hidden}
    .stage-dossier header{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:86px;padding:10px 12px;background:#20252a;color:#fff;border-bottom:5px solid var(--accent)}
    .head-left{min-width:0;display:flex;flex-direction:column;gap:9px}
    .group-tag{display:block;color:var(--accent);font:900 13px/1 Consolas,monospace;letter-spacing:.14em}
    .stage-dossier h3{margin:0;min-width:0;font-size:32px;line-height:1.02;font-weight:900;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .head-right{flex:0 0 auto;display:flex;flex-direction:column;align-items:flex-end;gap:8px}
    .head-right .best-time{font:900 26px/1 Consolas,monospace;color:#fff;letter-spacing:-.01em}
    .dossier-body{min-height:0;display:flex;flex:1;align-items:center;justify-content:center;padding:12px 14px}
    .dossier-body .team{gap:12px}
    .dossier-body .team-avatar{width:124px;height:124px}
    .star-trio{display:inline-flex;align-items:center;gap:0}
    .star-trio img{width:34px;height:34px;object-fit:contain;display:block;margin:0 -2px}
    .star-trio-fallback{font:900 15px/1 Arial,sans-serif;letter-spacing:2px;color:#ffcb2d}
    .stage-dossier .star-trio img{width:38px;height:38px;margin:0 -3px}
    .stage-dossier .star-marks{font-size:14px}
    .stage-dossier .star-marks i{color:#5a646a}
    .best-time{font:900 26px/1 Consolas,monospace;color:#20272b}
    .best-team{min-width:0;display:flex}
    .best-team .team{display:flex;gap:12px}
    .best-team .team-avatar{width:124px;height:124px}

    /* atoms */
    .team{display:flex;gap:8px;min-width:0}
    .team-avatar{position:relative;width:32px;height:32px;display:grid;place-items:center;background:#dce2e2;border:2px solid #fff;box-shadow:0 0 0 1px rgba(23,27,31,.35);color:#677278;font-size:9px;font-weight:900}
    .team-avatar .avatar-img{width:100%;height:100%;object-fit:cover}
    .potential-pip{position:absolute;left:2px;bottom:2px;width:34%;height:34%;display:grid;place-items:center}
    .potential-pip:before{content:"";position:absolute;left:50%;top:50%;width:112%;height:112%;transform:translate(-50%,-50%);background:radial-gradient(circle,rgba(255,200,40,.34) 0%,rgba(255,200,40,0) 56%)}
    .potential-pip.p5:before{width:132%;height:132%;background:radial-gradient(circle,rgba(255,205,58,.55) 0%,rgba(255,186,28,.16) 40%,rgba(255,200,40,0) 60%)}
    .potential-pip img{position:relative;width:100%;height:100%;object-fit:contain;transform:translate(0,-3.9%);filter:drop-shadow(0 1px 1px rgba(0,0,0,.80))}
    /* 0-4 潜是五潜图形的逐步点亮，锚点与五潜一致，不做逐档居中 */
    .record-team{min-width:0;height:100%;display:flex;align-items:center;justify-content:flex-end}
    .record-team .team{margin:0;height:100%}
    /* 头像跟着行高走：关少行高就放大，关多行矮就收缩；
       上限同时受格宽约束——用时轨 116 + 间隙 + 四格必须留在列内（列宽约 529px），
       96px 会顶到 542px 溢出，所以收在 88px。 */
    .ladder-cell .team-avatar{width:auto;height:min(76%,88px);aspect-ratio:1/1}
    .missing-record{color:#879196;font-size:12px;font-weight:900;white-space:nowrap}

    /* index chips (series / honors) */
    .history-grid{min-height:0;overflow:hidden;display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:9px;align-content:start;padding-top:9px}
    .achievement-grid{min-height:0;overflow:hidden;display:flex;flex-direction:column}
    .achievement-chip{flex:1;min-height:36px;display:flex;align-items:center;justify-content:space-between;gap:9px;padding:6px 6px;border-bottom:1px solid rgba(23,27,31,.13);color:#4d595f;font-size:13px;font-weight:900}
    .achievement-chip:last-child{border-bottom:0}
    .achievement-chip:before{content:"";width:5px;height:16px;background:var(--accent);flex:0 0 auto}
    .achievement-chip>span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .history-chip{flex:none;min-height:66px;display:flex;flex-direction:column;align-items:flex-start;gap:6px;padding:8px 10px;border:1px solid rgba(23,27,31,.16);background:rgba(255,255,255,.62);color:#4d595f;font-size:14px;font-weight:900}
    .history-chip:before{content:"";width:100%;height:4px;background:rgba(23,27,31,.26);flex:0 0 auto}
    .history-chip>span{width:100%;font-size:14px}
    .chip-tally{display:flex;align-items:baseline;gap:11px;font:900 12px/1.2 Consolas,'Microsoft YaHei',monospace;color:#5d686d}
    .chip-tally em{font-style:normal}
    .chip-tally .t-hard{color:var(--hard)}
    .history-chip.is-current{background:rgba(255,179,0,.16);color:#20272b}
    .history-chip.is-current:before{background:var(--accent)}
    .history-chip.is-current>span{font-weight:950}
    .overview-stack{flex:0 0 auto;display:flex;flex-direction:column;gap:22px}
    .overview-stack .stage-board{flex:0 0 auto}
    .honors-band{flex:0 0 auto;display:grid;grid-template-rows:auto minmax(0,1fr);padding:14px 18px 12px}
    .honors-cols4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:2px 18px;align-content:space-evenly}
    .honors-cols4 .achievement-chip{flex:none;min-height:44px;font-size:14px}
    .honors-cols8{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:2px 10px;align-content:space-evenly}
    .honors-cols8 .achievement-chip{flex:none;flex-direction:column;align-items:flex-start;gap:3px;min-height:36px;padding:4px 2px;font-size:11px}
    .honors-cols8 .achievement-chip b{margin-left:0}
    .star-marks{margin-left:auto;font:900 18px/1 Arial,'Microsoft YaHei',sans-serif;letter-spacing:3px;color:#e8a200;white-space:nowrap}
    .star-marks i{font-style:normal;color:#c6cdd2}

    /* detail */
    .detail-hero{position:relative;flex:0 0 auto;display:flex;justify-content:space-between;align-items:stretch;gap:22px;padding:4px 0 4px 18px;border-left:7px solid var(--accent);margin-bottom:13px}
    .detail-hero>div:first-child{min-width:0;align-self:center;padding:4px 0}
    .detail-hero .eyebrow{display:block;color:var(--accent-deep);font:900 12px/1.2 Consolas,'Microsoft YaHei',monospace;letter-spacing:.13em;overflow-wrap:anywhere}
    .detail-hero h1{margin:6px 0 5px;font-size:46px;line-height:1.03;font-weight:900;letter-spacing:.01em;overflow-wrap:anywhere}
    .detail-hero p{margin:0;color:#68747a;font-size:16px;font-weight:800}
    .detail-workbench{flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,1.04fr) minmax(380px,.96fr);grid-template-rows:minmax(188px,auto) minmax(0,1fr);gap:14px}
    .intel-sheet,.enemy-sheet{display:grid;grid-template-rows:auto minmax(0,1fr);padding:13px 18px}
    .intel-copy{min-height:0;overflow:hidden}
    .intel-copy section{padding:11px 2px;border-bottom:1px solid rgba(23,27,31,.14)}
    .intel-copy section:last-child{border-bottom:0}
    .intel-copy h3{margin:0 0 7px;font-size:15px;font-weight:900}
    .intel-copy p{margin:0;color:#536168;font-size:14px;line-height:1.7;font-weight:750;overflow:hidden;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:5}
    .enemy-grid{min-height:0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));grid-auto-rows:minmax(0,1fr);gap:8px;overflow:hidden}
    .enemy-card{min-width:0;min-height:0;display:grid;grid-template-columns:44px minmax(0,1fr);gap:10px;align-items:center;padding:7px 9px;border:1px solid rgba(23,27,31,.16);background:rgba(255,255,255,.88);overflow:hidden}
    .enemy-card img{width:44px;height:44px;object-fit:contain}
    .enemy-fallback{width:44px;height:44px;display:grid;place-items:center;background:#e2e7e6;color:#8a949a;font-weight:900}
    .enemy-card b{display:block;font-size:13px;line-height:1.2;font-weight:900;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .enemy-card small{display:block;margin-top:3px;color:#5f6b71;font-size:12px;line-height:1.4;font-weight:800;overflow:hidden;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2}

    /* 影拓丰碑详情（page-detail）：碑面双档 + 三栏工作台 */
    .page-detail .detail-hero{align-items:center;padding-left:20px}
    .page-detail .detail-hero .eyebrow{font-size:12px;letter-spacing:.18em}
    .page-detail .detail-hero h1{margin:5px 0 6px;font-size:58px;line-height:1.02;letter-spacing:-.012em}
    .page-detail .detail-hero p{font-size:17px;color:#5d686e}
    .page-detail .detail-hero .slab{align-self:center;min-width:372px}
    .page-detail .detail-hero .slab-figure{font-size:56px}
    .page-detail .detail-workbench{grid-template-columns:minmax(432px,.88fr) minmax(0,1fr) minmax(0,1fr);grid-template-rows:minmax(0,1fr)}
    .duo-sheet{display:grid;grid-template-rows:auto minmax(0,1fr) auto;padding:13px 16px 12px}
    .duo-list{min-height:0;display:flex;flex-direction:column;gap:12px;padding-top:11px}
    .duo-row{flex:0 0 auto;display:flex;flex-direction:column;justify-content:center;gap:11px;padding:14px 15px;background:rgba(255,255,255,.74);border:1px solid rgba(23,27,31,.16);border-left:6px solid var(--tier-bar,rgba(23,27,31,.34))}
    .duo-row.is-current{background:#20252a;box-shadow:0 12px 26px rgba(23,27,31,.24)}
    .duo-top{display:flex;align-items:center;gap:12px}
    .duo-label{display:flex;flex-direction:column;gap:4px;min-width:0}
    .duo-tier{color:var(--tier-text,#414c52);font-size:25px;line-height:1;font-weight:900;letter-spacing:.05em}
    .duo-row.is-current .duo-tier{font-size:27px}
    .duo-reco{color:#7c878c;font-size:12px;font-weight:900;letter-spacing:.02em}
    .duo-row.is-current .duo-reco{color:#a9b2b6}
    .h3-note{display:inline-flex;align-items:center;gap:6px;margin-left:12px;color:#8a949a;font-size:12px;font-weight:850;letter-spacing:.02em}
    .note-swatch{width:7px;height:7px;background:__HARD__}
    .duo-figure{flex:0 0 auto;min-width:112px;margin-left:auto;display:flex;flex-direction:column;align-items:flex-end;gap:5px}
    .duo-when{color:#7c878c;font:900 12px/1.2 Consolas,monospace;white-space:nowrap}
    .duo-row.is-current .duo-when{color:#a9b2b6}
    .duo-time{text-align:right;font:900 32px/1 Consolas,monospace;color:#20272b;letter-spacing:-.01em}
    .duo-row.is-current .duo-time{font-size:38px;color:#fff}
    .duo-team{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;min-width:0}
    .duo-note{display:flex;align-items:center;min-height:52px;padding:0 2px;color:#879297;font-size:13px;font-weight:900;letter-spacing:.02em;border-top:1px dashed rgba(23,27,31,.22)}
    .duo-row.is-current .duo-note{color:#98a2a7;border-top-color:rgba(255,255,255,.20)}
    .duo-member{min-width:0}
    .duo-member .team-avatar{width:100%;height:auto;aspect-ratio:1/1}
    .duo-member .team-avatar.is-empty{background:rgba(23,27,31,.05);border:1px dashed rgba(23,27,31,.20);box-shadow:none}
    .duo-row.is-current .duo-member .team-avatar.is-empty{background:rgba(255,255,255,.055);border-color:rgba(255,255,255,.17)}
    .feature-list{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:9px}
    .feature-list li{position:relative;padding-left:17px;color:#4d595f;font-size:15px;line-height:1.6;font-weight:750}
    .feature-list li:before{content:"";position:absolute;left:0;top:9px;width:7px;height:7px;background:var(--accent-deep)}
    .feature-list li.muted{color:#879297;font-size:13px}
    .feature-list li.is-diff:before{background:__HARD__}
    .page-detail .missing-record{font-size:12px}
    .page-detail .intel-copy{display:flex;flex-direction:column;justify-content:flex-start;gap:16px}
    .page-detail .intel-copy section{padding:12px 2px}
    .duo-foot{margin-top:11px;padding-top:10px;border-top:1px solid rgba(23,27,31,.16);color:#78838a;font:900 12px/1.4 Consolas,monospace;letter-spacing:.02em}
    .page-detail .enemy-grid{grid-template-columns:minmax(0,1fr);grid-auto-rows:minmax(58px,auto);align-content:flex-start;gap:9px;padding-top:2px}
    .page-detail .enemy-card{grid-template-columns:52px minmax(0,1fr);gap:12px;padding:9px 11px;background:rgba(255,255,255,.9)}
    .page-detail .enemy-card img,.page-detail .enemy-fallback{width:52px;height:52px}
    .page-detail .enemy-card b{font-size:15px}
    .page-detail .intel-copy p{-webkit-line-clamp:6;font-size:15px}
    .page-detail .intel-copy h3{font-size:16px}

    /* archive / history */
    /* 分页器住在页眉右端：靠右成栈，页码做大字，与 60px 的页面标题形成主次 */
    .archive-pager{flex:0 0 auto;display:flex;flex-direction:column;align-items:flex-end;gap:6px;padding-right:4px}
    .archive-pager span{color:var(--accent);font:900 11px/1 Consolas,monospace;letter-spacing:.16em}
    .archive-pager b{font:900 40px/1 Consolas,monospace;color:#fff;letter-spacing:-.01em}
    .archive-pager b i{font-style:normal;color:#7f8a90}
    .archive-pager b em{margin-left:7px;color:#a9b2b6;font-size:.42em;font-style:normal;letter-spacing:.02em}
    .archive-pager small{color:#aeb6ba;font-size:12px;font-weight:800}
    /* 两块主题各按自己的关卡数定高：等分会让 4 关的主题空出一大截 */
    .monument-archive-stack{flex:1;min-height:0;display:grid;grid-auto-rows:auto;gap:12px;align-content:start}
    .archive-group{min-height:0;display:grid;grid-template-columns:268px minmax(0,1fr);border:1px solid rgba(23,27,31,.25);background:rgba(249,250,248,.955);box-shadow:inset 0 5px 0 #20252a;overflow:hidden}
    .archive-meta{min-width:0;display:flex;flex-direction:column;justify-content:center;gap:6px;padding:16px 20px;background:#20252a;color:#fff}
    /* 深色章压在墨底上会糊成一团，用一圈极淡的光晕把它抬起来，比黑影管用 */
    .archive-seal{width:56px;height:56px;flex:0 0 auto;object-fit:contain;filter:drop-shadow(0 0 7px rgba(255,255,255,.22))}
    /* 纹章 + 铭牌：黄色标签挪到主题名上方，两行的高度刚好接住 56px 的章，
       比一行字硬跟章居中对齐稳。 */
    .archive-title{display:flex;align-items:center;gap:14px;min-width:0}
    .archive-nameplate{min-width:0;display:flex;flex-direction:column;gap:5px}
    .archive-nameplate span{color:var(--accent);font:900 11px/1 Consolas,monospace;letter-spacing:.14em}
    .archive-nameplate h3{margin:0;font-size:26px;line-height:1.05;font-weight:900;overflow-wrap:anywhere}
    .archive-meta p{margin:2px 0 0;color:#aeb6ba;font-size:13px;line-height:1.4;font-weight:800;white-space:nowrap}
    .archive-meta strong{display:block;margin-top:12px;font:900 34px/1 Consolas,monospace;color:#fff}
    .archive-meta strong i{font-size:.52em;font-style:normal;color:#8f9aa0}
    .archive-table{min-width:0;min-height:0;display:flex;flex-direction:column;padding:12px 16px 10px}
    .stage-table{min-height:0;display:flex;flex-direction:column}
    .archive-group .table-head,.archive-group .stage-row{grid-template-columns:minmax(110px,190px) minmax(0,1fr) minmax(0,1fr)}
    .table-head{flex:0 0 30px;display:grid;gap:12px;align-items:center;color:#6d787d;font-size:15px;font-weight:900;letter-spacing:.06em}
    /* 列头压在用时轨的正中；第二轨同样让出 13px（1px 分隔线 + 12px 内缩） */
    .table-head .head-tier{width:84px;justify-self:start;text-align:center;letter-spacing:0}
    .table-head .head-tier+.head-tier{width:97px;padding-left:13px}
    .table-head .head-hard{color:__HARD__}
    .stage-row{flex:0 0 auto;min-height:56px;display:grid;gap:12px;align-items:center;border-top:1px solid rgba(23,27,31,.13)}
    .history-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:16px;font-weight:900}
    .history-record{min-width:0;display:grid;grid-template-columns:84px minmax(0,132px) minmax(0,1fr);gap:12px;align-items:center}
    .history-record+.history-record{border-left:1px dashed rgba(23,27,31,.20);padding-left:13px}
    .history-record b{font:900 19px/1 Consolas,monospace;color:#20272b;text-align:center}
    .history-record b.history-none{color:#98a2a6;font:900 13px/1.3 'Microsoft YaHei',sans-serif}
    .history-record.is-blank{grid-template-columns:84px minmax(0,1fr)}
    .history-record small{color:#6f7b81;font:900 12px/1.2 Consolas,monospace;white-space:nowrap}
    .history-team{display:flex;gap:6px;min-width:0;overflow:hidden;justify-content:flex-start}
    .history-team .team-avatar{width:52px;height:52px;border-width:2px}
    .war-week-stack{min-height:0;display:grid;grid-auto-rows:auto;gap:14px}
    .week-card{min-height:0;padding:0 14px 13px;border:1px solid rgba(23,27,31,.25);background:rgba(249,250,248,.94);box-shadow:inset 0 5px 0 #20252a;overflow:hidden}
    .week-head{display:flex;align-items:baseline;gap:14px;margin:0 -14px 12px;padding:11px 16px;background:#20252a;color:#fff;border-bottom:5px solid var(--accent)}
    .week-head h3{margin:0;font-size:19px;line-height:1;font-weight:900}
    .week-head p{margin:0;color:#aeb6ba;font-size:11px;font-weight:800}
    .war-group-flow{min-height:0;display:flex;gap:20px}
    .war-group-card{width:fit-content;min-width:0;display:flex;flex-direction:column;gap:10px;padding:10px;background:rgba(255,255,255,.88);border:1px solid rgba(23,27,31,.16)}
    .wgc-team{min-width:0;display:flex;gap:8px}
    .wgc-team .team-avatar{width:var(--wgc-avatar,104px);height:var(--wgc-avatar,104px);border-width:2px}
    .wgc-plate{display:flex;align-items:center;gap:12px;padding:8px 14px 8px 6px;background-color:#1b1b1b;background-image:__PLATE__;background-repeat:no-repeat;background-position:center;background-size:100% 100%;box-shadow:0 6px 16px rgba(18,20,22,.28)}
    .wgc-deco{flex:0 0 auto;width:84px;height:56px;object-fit:contain;margin-left:-4px}
    .wgc-info{display:flex;flex-direction:column;align-items:flex-start;gap:4px;min-width:0;color:#9aa4aa}
    .wgc-info .tier{color:var(--accent);font-size:12px;font-weight:900;letter-spacing:.04em}
    .wgc-info>b{font:900 24px/1 Consolas,monospace;color:#fff}
    .wgc-info small{color:#8f989d;font:900 10px/1 Consolas,monospace}
    .wgc-name{margin-left:auto;flex:0 0 auto;align-self:stretch;display:flex;flex-direction:column;align-items:flex-end;justify-content:flex-end;gap:1px}
    .wgc-name h4{margin:0;font-size:21px;line-height:1.05;font-weight:900;color:#fff;white-space:nowrap}
    .wgc-name .star-trio img{width:var(--wgc-star,27px);height:var(--wgc-star,27px);margin:0 -3px}
    .team-avatar.is-empty{background:repeating-linear-gradient(135deg,#e7eaea 0 6px,#dfe3e3 6px 12px);border-style:dashed;border-color:rgba(23,27,31,.28);box-shadow:none}
    .challenge-header .seal-rating{height:76px;filter:drop-shadow(0 6px 12px rgba(0,0,0,.45))}

    .muted{color:#879297;font-size:13px;font-weight:800}
    .empty-panel{display:grid;place-items:center;padding:20px}

    /* 空态：一张查询回执。左联存查询上下文，右联给结论、两种原因与下一步 */
    .empty-state{position:relative;flex:1;min-height:380px;display:grid;grid-template-columns:320px minmax(0,1fr);align-items:stretch;border:1px solid rgba(23,27,31,.25);background:rgba(249,250,248,.95);box-shadow:inset 0 7px 0 #20252a;overflow:hidden}
    .empty-receipt{align-self:stretch;display:flex;flex-direction:column;justify-content:center;gap:14px;padding:30px 28px;background:#20252a;color:#fff;border-bottom:8px solid var(--accent)}
    .empty-receipt>span{color:var(--accent);font:900 11px/1 Consolas,monospace;letter-spacing:.16em}
    .empty-receipt dl{margin:0;display:flex;flex-direction:column;gap:11px}
    .empty-receipt dt{color:#8f9aa0;font-size:12px;font-weight:900;letter-spacing:.04em}
    .empty-receipt dd{margin:3px 0 0;font-size:17px;line-height:1.25;font-weight:900;overflow-wrap:anywhere}
    /* 盖在回执上的「无记录」章：档案该有的动作，不是装饰，也不转那套歪斜角度 */
    .empty-stamp{margin-top:6px;align-self:flex-start;padding:10px 16px;border:2px solid color-mix(in srgb,var(--accent) 55%,transparent);color:var(--accent)}
    .empty-stamp span{display:block;font:900 10px/1 Consolas,monospace;letter-spacing:.18em;color:color-mix(in srgb,var(--accent) 72%,#fff)}
    .empty-stamp b{display:block;margin-top:6px;font-size:26px;line-height:1;font-weight:900;letter-spacing:.08em}
    .empty-copy{display:flex;flex-direction:column;justify-content:center;gap:14px;padding:38px 44px}
    .empty-copy h1{margin:0;font-size:40px;line-height:1.08;font-weight:900;letter-spacing:-.01em}
    .empty-lead{margin:0;max-width:640px;color:#5d686e;font-size:16px;line-height:1.6;font-weight:800}
    /* 三条条目同构：同一列栅格、同一个间距。首列放序号或标签并居中，
       正文一律左对齐——所以「下一步」和 01/02 的标记中心对齐，而不是首字对齐。 */
    .empty-rows{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:10px}
    .empty-rows li{display:grid;grid-template-columns:56px minmax(0,1fr);align-items:center;gap:0;padding:10px 14px;background:rgba(255,255,255,.72);border:1px solid rgba(23,27,31,.16);border-left:4px solid rgba(23,27,31,.30);color:#3d474c;font-size:15px;line-height:1.35;font-weight:850}
    .empty-rows .row-mark{text-align:center;font-style:normal;color:#7d888d;font:900 13px/1 Consolas,monospace;letter-spacing:.06em}
    .empty-rows li.is-next{background:color-mix(in srgb,var(--accent) 13%,#fff);border-color:transparent;border-left-color:var(--accent)}
    /* 中文标签走雅黑、数字标签走 Consolas，两套字体的墨迹在 em 框里分布不同：
       中文偏低 1.5px，做一次光学下移（实测三行标记与正文的竖直差 ≤0.5px）。 */
    .empty-rows li.is-next .row-mark{color:var(--accent-deep);font-size:11px;letter-spacing:.14em;transform:translate(0,1.5px)}
    """
    return (css
        .replace("__ACCENT__", accent)
        .replace("__ACCENT_DEEP__", accent_deep)
        .replace("__HARD__", MONUMENT_HARD)
        .replace("__HARD_LIFT__", MONUMENT_HARD_LIFT)
        .replace("__HERO_WIDTH__", settings[0])
        .replace("__HERO_OPACITY__", settings[1])
        .replace("__HERO__", f"url('{hero}')" if hero else "none")
        .replace("__PLATE__", f"url('{WAR_PLATE_ASSET.as_uri()}')" if WAR_PLATE_ASSET.is_file() else "none"))

def _asset_urls_monument(payload: MonumentPayload, current: MonumentGroup | None) -> list[str]:
    urls: list[str] = []
    groups = (current,) if current is not None else payload.groups
    for group in groups:
        urls.extend((group.pic_url, group.medal_icon_url, group.medal_plated_icon_url))
        for pair in group.stages:
            for dungeon in pair:
                urls.extend(_record_urls(dungeon.record))
                urls.extend(enemy.image_url for enemy in dungeon.enemies)
    return [url for url in urls if url]


def _asset_urls_monument_history(groups: Sequence[MonumentGroup]) -> list[str]:
    """Theme medal seals and the displayed difficulty teams.

    主题 KV 不在这里取：历史页不再拿它当全页背景。
    """
    urls: list[str] = []
    for group in groups:
        urls.extend((group.medal_icon_url, group.medal_plated_icon_url))
        for pair in group.stages:
            for dungeon in pair:
                urls.extend(_record_urls(dungeon.record))
    return [url for url in urls if url]


def _asset_urls_monument_detail(group: MonumentGroup, dungeon: MonumentDungeon) -> list[str]:
    # 双档对照会把另一档的配队也画出来，所以两档的队伍快照都要取图。
    urls: list[str] = [group.pic_url, group.medal_icon_url, group.medal_plated_icon_url]
    for item in _monument_stage_context(group, dungeon)[0]:
        urls.extend(_record_urls(item.record))
    urls.extend(enemy.image_url for enemy in dungeon.enemies)
    return [url for url in urls if url]


def _asset_urls_war(payload: WarEchoPayload, season: WarSeason | None) -> list[str]:
    urls: list[str] = []
    seasons = (season,) if season is not None else payload.seasons
    for item in seasons:
        urls.extend((item.kv_url, item.header_url))
        for week in item.weeks:
            for group in week.groups:
                for dungeon in (group.normal, group.hard, group.cruel):
                    if dungeon:
                        urls.extend(_record_urls(dungeon.record))
                        urls.extend(enemy.image_url for enemy in dungeon.enemies)
    return [url for url in urls if url]


def _asset_urls_war_history(seasons: Sequence[WarSeason]) -> list[str]:
    """Match the archive UI: hero art plus the highest cleared tier per group."""
    urls: list[str] = []
    for season in seasons:
        urls.extend((season.kv_url, season.header_url))
        for week in season.weeks:
            for group in week.groups:
                dungeon = _best_war_dungeon(group)
                if dungeon is not None:
                    urls.extend(_record_urls(dungeon.record))
    return [url for url in urls if url]


def _asset_urls_war_detail(season: WarSeason, group: WarGroup, dungeon: WarDungeon) -> list[str]:
    """三档对照会把没在看的档位配队也画出来，所以三档的队伍快照都要取图。"""
    urls = [season.kv_url, season.header_url]
    for item in (group.normal, group.hard, group.cruel):
        if item is not None:
            urls.extend(_record_urls(item.record))
    urls.extend(enemy.image_url for enemy in dungeon.enemies)
    return [url for url in urls if url]


def _record_urls(record: ChallengeRecord) -> list[str]:
    return [item.avatar_url for item in record.members if item.avatar_url]


def _asset_src(url: str, assets: dict[str, str]) -> str:
    return assets.get(url, "") if url else ""


def _compact_team_html(record: ChallengeRecord, assets: dict[str, str]) -> str:
    if not record.available or not record.members:
        return '<span class="muted">--</span>'
    return "".join(_member_html(member, assets) for member in record.members[:4])


def _potential_pip(potential) -> str:
    level = max(0, min(5, _int(potential)))
    path = POTENTIAL_ICON_DIR / f"potential_{level}.png"
    if not path.is_file():
        return ""
    return f'<span class="potential-pip p{level}"><img src="{path.as_uri()}" alt="" /></span>'


def _member_html(member: ChallengeMember, assets: dict[str, str]) -> str:
    url = _asset_src(member.avatar_url, assets)
    image = f'<img class="avatar-img" src="{esc_attr(url)}" alt="" />' if url else ""
    pip = _potential_pip(member.potential) if image else ""
    return f'<span class="team-avatar{" is-empty" if not image else ""}" title="Lv.{member.level} 潜能{member.potential}">{image}{pip}</span>'


def _enemy_html(enemy: ChallengeEnemy, assets: dict[str, str]) -> str:
    url = assets.get(enemy.image_url, "") if enemy.image_url else ""
    image = f'<img src="{esc_attr(url)}" alt="" />' if url else '<span class="enemy-fallback">--</span>'
    return f'<article class="enemy-card">{image}<div><b>{esc(enemy.name or "未命名敌人")}</b><small>Lv.{enemy.level or "--"} · {esc(_enemy_note(enemy))}</small></div></article>'


def _enemy_note(enemy: ChallengeEnemy, limit: int = 62) -> str:
    """取官方能力说明的首句；宁可少说，也不在句中截断成半句话。"""
    text = _plain(enemy.ability or enemy.desc)
    if not text:
        return "无公开说明"
    sentence = re.split(r"(?<=[。！？])", text)[0].strip() or text
    if len(sentence) <= limit:
        return sentence
    cut = sentence[:limit]
    for separator in ("，", "、", "；", " ", ","):
        index = cut.rfind(separator)
        if index >= int(limit * 0.55):
            return cut[:index].rstrip("，、； ,") + "…"
    return cut.rstrip("，、； ,") + "…"


def _monument_stage_card(pair, assets: dict[str, str], index: int) -> str:
    normal, hard = pair

    def cell(dungeon):
        # 难度由列头表达，通关与否不画勾：有用时本身就是通关的证据，
        # 没过的档由文字回答，不用一列符号重复说一遍。
        record = dungeon.record
        if not record.available:
            blank = "未通关" if not dungeon.passed else "无记录返回"
            return f'<div class="ladder-cell is-blank"><span>{blank}</span></div>'
        if record.pass_time > 0:
            duration = f'<strong class="ladder-time">{esc(_format_duration(record.pass_time))}</strong>'
        elif dungeon.passed:
            duration = '<strong class="ladder-time missing-duration">用时未返回</strong>'
        else:
            # 有快照但没过：说的是「没通关」，不是接口没给数，两者不能混。
            duration = '<strong class="ladder-time missing-duration">未通关</strong>'
        team = _compact_team_html(record, assets) if record.members else '<span class="missing-record">队伍未返回</span>'
        return f'<div class="ladder-cell">{duration}<span class="record-team">{team}</span></div>'

    recommend = hard.recommend_level or normal.recommend_level
    name_html = esc(_display_stage_name(normal.name or hard.name))
    if recommend:
        name_html += f'<small>推荐等级 {recommend}</small>'
    return f'<div class="ladder-row"><span>{index:02d}</span><span class="ladder-name">{name_html}</span>{cell(normal)}{cell(hard)}</div>'


def _best_war_dungeon(group: WarGroup) -> WarDungeon | None:
    """残酷 > 困难 > 普通：返回最高已通关档；全部未通关时退回有记录的最高档。"""
    dungeons = (group.cruel, group.hard, group.normal)
    for dungeon in dungeons:
        if dungeon is not None and dungeon.passed:
            return dungeon
    for dungeon in dungeons:
        if dungeon is not None and dungeon.record.available:
            return dungeon
    return next((item for item in dungeons if item is not None), None)


def _war_rating_key(season: WarSeason) -> str:
    """官方评级规则：9 星看追加目标（S+/S），其余按星数分 D–A，0 星未评级。"""
    if season.stars >= 9:
        return "s_plus" if season.all_plus_tasks else "s"
    if season.stars >= 7:
        return "a"
    if season.stars >= 5:
        return "b"
    if season.stars >= 3:
        return "c"
    if season.stars >= 1:
        return "d"
    return "unrated"


def _war_rating_html(season: WarSeason) -> str:
    path = WAR_RATING_ASSET_DIR / WAR_RATING_FILES.get(_war_rating_key(season), "")
    if path.is_file():
        return f'<img class="seal-rating" src="{path.as_uri()}" alt="" />'
    return ""


def _war_star(kind: str) -> str:
    path = WAR_STAR_ASSET_DIR / WAR_STAR_FILES.get(kind, "")
    return path.as_uri() if path.is_file() else ""


def _star_trio(star, *, plus: bool = False) -> str:
    """轮换关卡组的官方三星：点亮数取 group.star，追加目标全完成切换金色变体。"""
    value = max(0, min(3, _int(star)))
    lit = _war_star("lit_plus" if plus else "lit")
    empty = _war_star("empty")
    if not lit or not empty:
        return f'<span class="star-trio-fallback">{"★" * value}</span>'
    marks = "".join(f'<img src="{lit if index < value else empty}" alt="" />' for index in range(3))
    return f'<span class="star-trio{" is-plus" if plus else ""}">{marks}</span>'


def _star_marks(star, total: int = 3) -> str:
    value = max(0, min(total, _int(star)))
    return f'<b class="star-marks">{"★" * value}<i>{"★" * (total - value)}</i></b>'


def _war_stage_card(group: WarGroup, assets: dict[str, str], index: int) -> str:
    """难度并入标题行，用时压在三星下方，卡体只留配队。"""
    star_marks = _star_trio(group.star, plus=group.plus_task)
    dungeon = _best_war_dungeon(group)
    tier_html, time_html = "", '<b class="best-time">--</b>'
    members = ()
    if dungeon is not None:
        tier_html = f'<span class="tier">· {_difficulty_label(dungeon.difficulty)}</span>'
        record = dungeon.record
        duration = _format_duration(record.pass_time) if record.pass_time > 0 else "--"
        time_html = f'<b class="best-time">{esc(duration)}</b>'
        members = record.members[:4]
    team = "".join(_member_html(item, assets) for item in members) or '<span class="missing-record">队伍未返回</span>'
    return (
        f'<article class="stage-dossier"><header>'
        f'<div class="head-left"><span class="group-tag">GROUP {index:02d}</span>'
        f'<h3>{esc(group.name)} {tier_html}</h3></div>'
        f'<div class="head-right">{star_marks}{time_html}</div></header>'
        f'<div class="dossier-body"><div class="team">{team}</div></div></article>'
    )


def _duration_text(dungeon) -> str:
    """用时的三种真实状态分开说：有数、过了但没给数、没过。"""
    record = dungeon.record
    if record.pass_time > 0:
        return _format_duration(record.pass_time)
    return "用时未返回" if dungeon.passed else "未通关"


def _history_record_cell(dungeon, assets: dict[str, str]) -> str:
    """一档一格：用时轨 + 记录时间 + 队伍。不画通关勾——有用时本身就是通关的证据。

    整档没有任何记录时只留一句「未通关」，不铺「-- / 队伍未返回」三条空话。
    """
    record = dungeon.record
    if not record.available:
        blank = "未通关" if not dungeon.passed else "无记录返回"
        return f'<div class="history-record is-blank"><b class="history-none">{blank}</b></div>'
    has_time = record.pass_time > 0
    duration = f'<b>{esc(_duration_text(dungeon))}</b>' if has_time else f'<b class="history-none">{esc(_duration_text(dungeon))}</b>'
    stamp = record.record_ts or record.first_pass_ts
    when = _date(stamp) if stamp else "--"
    team = _compact_team_html(record, assets) if record.members else '<span class="missing-record">队伍未返回</span>'
    return f'<div class="history-record">{duration}<small>{esc(when)}</small><span class="history-team">{team}</span></div>'


def _monument_stage_row(pair, assets=None) -> str:
    """历史页一行：关卡名 + 两档各自的最佳用时、记录时间与队伍。"""
    normal, hard = pair
    stage_name = esc(_display_stage_name(normal.name or hard.name))
    return (
        f'<div class="stage-row history-stage-row"><span class="history-name">{stage_name}</span>'
        f'{_history_record_cell(normal, assets or {})}{_history_record_cell(hard, assets or {})}</div>'
    )


def _war_group_card(group: WarGroup, assets, index: int = 1) -> str:
    """Official group card: team on top, one bottom plate holding deco, stats, name and stars."""
    trio = _star_trio(group.star, plus=group.plus_task)
    dungeon = _best_war_dungeon(group)
    tier, duration = "暂无记录", "--"
    members = ()
    if dungeon is not None:
        record = dungeon.record
        tier = _difficulty_label(dungeon.difficulty)
        duration = _format_duration(record.pass_time) if record.pass_time > 0 else "未返回"
        members = record.members[:4]
    # 接口偶发少给成员：补齐到 4 格空框，保证贴片与队伍行等宽。
    cells = "".join(_member_html(member, assets or {}) for member in members)
    cells += '<span class="team-avatar is-empty"></span>' * (4 - len(members))
    deco = f'<img class="wgc-deco" src="{WAR_DECO_ASSET.as_uri()}" alt="" />' if WAR_DECO_ASSET.is_file() else ""
    return (
        f'<article class="war-group-card"><div class="wgc-team">{cells}</div>'
        f'<div class="wgc-plate">{deco}'
        f'<div class="wgc-info"><span class="tier">{esc(tier)}</span><b>{esc(duration)}</b></div>'
        f'<div class="wgc-name"><h4>{esc(group.name)}</h4>{trio}</div></div></article>'
    )


def _war_week_block(week: WarWeek, assets) -> str:
    cards = "".join(_war_group_card(group, assets, index) for index, group in enumerate(week.groups, 1))
    flow = cards or '<div class="muted">暂无轮换记录</div>'
    return (
        '<article class="week-card"><div class="week-head">'
        f'<h3>{esc(week.name)}</h3><p>{esc(_period(week.start_ts, week.end_ts))}</p></div>'
        f'<div class="war-group-flow">{flow}</div></article>'
    )


def _war_target(group: WarGroup) -> str:
    for dungeon in (group.normal, group.hard, group.cruel):
        if dungeon and dungeon.additional_target:
            return dungeon.additional_target
    return "已完成" if group.plus_task else "未设置"


def _war_group_record(group: WarGroup) -> ChallengeRecord:
    return max(
        (dungeon.record for dungeon in (group.normal, group.hard, group.cruel) if dungeon and dungeon.record.available),
        key=lambda item: (item.record_ts, item.first_pass_ts),
        default=ChallengeRecord(),
    )


def _monument_group_summary(group: MonumentGroup, current: MonumentGroup | None = None) -> str:
    total = len(group.stages)
    normal = sum(1 for pair in group.stages if pair[0].passed)
    hard = sum(1 for pair in group.stages if pair[1].passed)
    is_current = current is not None and (group.id == current.id or group.name == current.name)
    # 两档分开标出来：光写「4/4 · 4/4」没人知道哪个是哪个。
    tally = f'<span class="chip-tally"><em>普通 {normal}/{total}</em><em class="t-hard">苦难 {hard}/{total}</em></span>'
    return (
        f'<span class="history-chip{" is-current" if is_current else ""}"><span>{esc(group.name)}</span>{tally}</span>'
    )


def _tier_note(group: MonumentGroup, index: int) -> str:
    """把该档所有已返回的用时聚合成一句真话：最快与合计，没有就直说没有。"""
    times = [pair[index].record.pass_time for pair in group.stages if pair[index].record.pass_time > 0]
    if not times:
        return "无用时记录"
    if len(times) == 1:
        return f"用时 {_format_duration(times[0])}"
    return f"最快 {_format_duration(min(times))} · 合计 {_format_duration(sum(times))}"


def _monument_history_block(group: MonumentGroup, assets) -> str:
    rows = "".join(_monument_stage_row(pair, assets) for pair in group.stages)
    cleared = sum(1 for pair in group.stages for dungeon in pair if dungeon.passed)
    total = len(group.stages) * 2
    medal_url = group.medal_plated_icon_url if group.medal_plated else group.medal_icon_url
    medal_src = _asset_src(medal_url, assets)
    seal = f'<img class="archive-seal" src="{esc_attr(medal_src)}" alt="" />' if medal_src else ""
    # 章是这个主题的纹章，跟着身份行走；镀层用的是另一张章图，
    # 所以「· 已镀层」那行字本来就是多余的。
    return (
        f'<article class="archive-group"><div class="archive-meta">'
        f'<div class="archive-title">{seal}<div class="archive-nameplate"><span>THEME ARCHIVE</span>'
        f'<h3>{esc(group.name)}</h3></div></div>'
        f'<p>{esc(_period_short(group.start_ts, group.end_ts))}</p>'
        f'<strong>{cleared}<i>/{total}</i></strong></div>'
        f'<div class="archive-table"><div class="stage-table">'
        f'<div class="table-head"><span>关卡</span><span class="head-tier">普通</span><span class="head-tier head-hard">苦难</span></div>'
        f'{rows}</div></div></article>'
    )


def _empty_html(identity, kind, query, variant):
    """空态是一张查询回执：左联存查询上下文，右联给结论、两种真实原因与下一步。

    原因取自实测结论（docs/skland_endfield_public_query.md）：接口返回成功、账号
    定位正常时，缺的是「通关记录」或「展示开关」，两者都可能，所以并列而不武断。
    """
    kind_text = str(kind)
    # 账号 / 服务器 / UID 页眉已经写过，存根只留页眉没有的信息。
    rows = (
        ("玩法", kind_text),
        ("接口结果", "成功（code 0）· 无可见记录"),
        ("查询时间", identity.updated_at or _date(int(time()))),
    )
    receipt = "".join(f"<div><dt>{esc(label)}</dt><dd>{esc(value)}</dd></div>" for label, value in rows)
    lead = "接口返回成功、账号定位正常，只是这个玩法没有返回可见数据。"
    if query:
        lead = f"{lead}（{query}）"
    reasons = ("这个账号还没有该玩法的通关记录", "森空岛未开启终末地个人数据的展示开关")
    rows = "".join(
        f'<li><i class="row-mark">{index + 1:02d}</i><span>{esc(text)}</span></li>'
        for index, text in enumerate(reasons)
    )
    rows += f'<li class="is-next"><i class="row-mark">下一步</i><span>{esc(NEXT_STEP_HINT)}</span></li>'
    return _document(
        identity,
        f"{kind_text} · 查询回执",
        "",
        MONUMENT_ACCENT if "影拓" in kind_text else WAR_ACCENT,
        "",
        variant,
        f'''<section class="empty-state"><div class="empty-receipt"><span>QUERY RECEIPT</span><dl>{receipt}</dl><div class="empty-stamp"><span>NO RECORD</span><b>无记录</b></div></div><div class="empty-copy"><h1>没有可展示的记录</h1><p class="empty-lead">{esc(lead)}</p><ul class="empty-rows">{rows}</ul></div></section>''',
        card_width=1320,
        page="empty",
    )
