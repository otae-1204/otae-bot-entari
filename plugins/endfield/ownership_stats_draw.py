"""持有率报告展示层:把 OwnershipStatsReport 渲染为工业风长图。

只消费报告对象,不访问账号接口、绑定表或快照表;排序、分母与比例
均由后端给出,这里只负责呈现。空样本段不渲染干员列表,避免把
None 比例画成 0%。
"""

from __future__ import annotations

import math
import time

from .draw import (
    ASSET_DIR,
    PreparedCardHtml,
    _draw_gallery_catalog,
    _local_image_data_url,
    _prepare_assets,
    esc,
    esc_attr,
    is_height_limit_error,
)
from .ownership_stats import (
    OperatorOwnership,
    OwnershipStatsReport,
    OwnershipStatsSegment,
    PotentialBucket,
)


OWNERSHIP_STATS_WIDTH = 1500

# AKEData 精灵图 CDN,头像按 charId 直接拼 URL;观测补录干员没有 source_id,走首字兜底。
AKEDATA_SPRITES_BASE = (
    "https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites"
)

_REGION_LABELS = {"all": "总计", "cn": "国服", "asia": "亚服"}
_REGION_ACCENTS = {"all": "#20262a", "cn": "#286cd6", "asia": "#d59800"}
_SCOPE_LABELS = {"global": "全局统计", "group": "群内统计"}

# (底色, 文字色);potential_5 用主视觉黄,未知用斜纹灰。
_BUCKET_COLORS: dict[str, tuple[str, str]] = {
    "unowned": ("#d9dde0", "#5f6a70"),
    "potential_0": ("#c9d9e6", "#2c373d"),
    "potential_1": ("#a6c4da", "#2c373d"),
    "potential_2": ("#7fa9cc", "#1d2731"),
    "potential_3": ("#5a8cba", "#ffffff"),
    "potential_4": ("#3a6fa5", "#ffffff"),
    "potential_5": ("#ffd000", "#20252a"),
    "unknown": ("#c4cacf", "#5f6a70"),
}
_BUCKET_LEGEND_ORDER = (
    "unowned",
    "potential_0",
    "potential_1",
    "potential_2",
    "potential_3",
    "potential_4",
    "potential_5",
    "unknown",
)
_BUCKET_LEGEND_LABELS = {
    "unowned": "未持有",
    "unknown": "未知",
    # 数据 potentialLevel 0-5:0 为零额外潜能(基础态),1-5 对应 1潜-5潜。
    **{f"potential_{level}": f"{level}潜" for level in range(6)},
}
# 桶内文字只在足够宽时渲染,窄桶靠颜色与图例区分,避免截断出残字。
_BUCKET_LABEL_MIN_RATE = 0.06

_OVERFLOW_SELECTORS = (".op-row", ".summary-row", ".sample-card", ".empty-segment-note")


def operator_avatar_url(operator: OperatorOwnership) -> str:
    source_id = str(operator.source_id or "").strip()
    if not source_id:
        return ""
    return f"{AKEDATA_SPRITES_BASE}/charremoteicon/icon_{source_id}.png"


async def prepare_ownership_stats_html(
    report: OwnershipStatsReport,
    *,
    regions: tuple[str, ...] | None = None,
) -> PreparedCardHtml:
    urls = _avatar_urls(report, regions)
    assets = await _prepare_assets(urls, inline=False)
    return PreparedCardHtml(
        _render_page(report, assets.urls, _select_segments(report, regions)),
        assets.resources,
        OWNERSHIP_STATS_WIDTH,
    )


async def render_ownership_stats_html(
    report: OwnershipStatsReport,
    *,
    icon_map: dict[str, str] | None = None,
    regions: tuple[str, ...] | None = None,
) -> str:
    """完整页面 HTML;icon_map 供离线渲染(测试/预览)传入内联资源。"""
    if icon_map is None:
        assets = await _prepare_assets(_avatar_urls(report, regions), inline=True)
        icon_map = assets.urls
    return _render_page(report, icon_map, _select_segments(report, regions))


async def draw_ownership_stats(report: OwnershipStatsReport) -> tuple[bytes, ...]:
    """整页一图;超高(如亚服观测目录爆炸)时降级为一段一图。"""
    try:
        return (await _draw_page(report),)
    except RuntimeError as exc:
        if not is_height_limit_error(exc):
            raise
    populated = tuple(
        segment.region for segment in report.segments if segment.valid_sample_count > 0
    )
    if len(populated) <= 1:
        raise
    return tuple(await _draw_page(report, regions=(region,)) for region in populated)


async def _draw_page(
    report: OwnershipStatsReport,
    *,
    regions: tuple[str, ...] | None = None,
) -> bytes:
    prepared = await prepare_ownership_stats_html(report, regions=regions)
    return await _draw_gallery_catalog(
        prepared,
        ".ownership-stats",
        _OVERFLOW_SELECTORS,
        "ownership_stats",
    )


def _avatar_urls(
    report: OwnershipStatsReport, regions: tuple[str, ...] | None
) -> list[str]:
    return [
        operator_avatar_url(operator)
        for segment in _select_segments(report, regions)
        for operator in segment.operators
    ]


def _select_segments(
    report: OwnershipStatsReport, regions: tuple[str, ...] | None
) -> tuple[OwnershipStatsSegment, ...]:
    if regions is None:
        return report.segments
    wanted = set(regions)
    return tuple(segment for segment in report.segments if segment.region in wanted)


def _render_page(
    report: OwnershipStatsReport,
    icon_map: dict[str, str],
    segments: tuple[OwnershipStatsSegment, ...],
) -> str:
    populated = [segment for segment in segments if segment.valid_sample_count > 0]
    regions_shown = [s for s in populated if s.region in ("cn", "asia")]
    # 只有一个区域段有样本时,总计段的样本集与它完全相同,只保留区域段避免重复。
    skip_all_section = bool(regions_shown) and len(regions_shown) < 2
    displayed = [
        segment
        for segment in segments
        if segment.valid_sample_count > 0
        and (segment.region != "all" or not skip_all_section)
    ]
    body_sections = "".join(_segment_section(segment, icon_map) for segment in displayed)
    empty_notes = "".join(
        _empty_segment_note(segment)
        for segment in segments
        if segment.valid_sample_count == 0
    )
    overview = _overview_section(report, segments)
    legend = _potential_legend() if populated else ""
    refresh = _refresh_strip(report.refresh) if report.refresh is not None else ""
    six_star = _six_star_section(report)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>{_css()}</style></head><body>
<div class="ownership-stats">
  {_header(report)}
  {refresh}
  <section class="ownership-section">
    <div class="section-head"><h2>样本概况</h2></div>
    <div class="sample-grid">{overview}</div>
    {legend}
  </section>
  {six_star}
  {body_sections}
  {empty_notes}
</div></body></html>"""


def _header(report: OwnershipStatsReport) -> str:
    scope = _SCOPE_LABELS.get(report.scope, report.scope)
    if report.snapshot_updated_at is not None:
        updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(report.snapshot_updated_at))
        snapshot_text = f"快照更新 {updated}"
    else:
        snapshot_text = "暂无有效快照"
    meta_parts = [
        snapshot_text,
        f"AKEData 目录 {report.catalog_version}" if report.catalog_version else "AKEData 目录不可用",
    ]
    return (
        '<header class="ownership-header">'
        '<div class="ownership-title-row"><div>'
        "<h1>干员持有率统计</h1>"
        "</div>"
        f'<div class="ownership-scope-badge">{esc(scope)}</div></div>'
        f'<div class="ownership-meta">{" · ".join(esc(part) for part in meta_parts)}</div>'
        "</header>"
    )


def _refresh_strip(refresh) -> str:
    duration = max(0, int(refresh.finished_at) - int(refresh.started_at))
    duration_text = f"{duration} 秒" if duration else "<1 秒"
    catalog_text = "目录已更新" if refresh.catalog_updated else "目录无变化"
    items = (
        ("尝试", refresh.attempted),
        ("成功", refresh.succeeded),
        ("失败", refresh.failed),
        ("跳过", refresh.skipped),
    )
    stats = " · ".join(f"{esc(label)} <b>{value}</b>" for label, value in items)
    return (
        '<section class="ownership-refresh">'
        f"<span>刷新批次</span>{stats}"
        f"<span>{esc(catalog_text)}</span><span>耗时 <b>{esc(duration_text)}</b></span>"
        "</section>"
    )


def _overview_section(report, segments) -> str:
    return "".join(_sample_card(segment) for segment in segments)


def _sample_card(segment: OwnershipStatsSegment) -> str:
    region = _REGION_LABELS.get(segment.region, segment.region)
    accent = _REGION_ACCENTS.get(segment.region, "#20262a")
    return (
        f'<article class="sample-card" style="border-left-color:{accent}">'
        f'<span class="sample-region" style="color:{accent}">{esc(region)}</span>'
        f"<b>{segment.valid_sample_count:,}</b>"
        f'<i>合格 {segment.eligible_sample_count:,} · 已排除 {segment.excluded_sample_count:,}</i>'
        "</article>"
    )


# 6 星常驻干员名单(展示层静态配置):收集概况里除名单外的 6 星均计为非常驻。
# 游戏新增常驻 6 星时需同步维护此名单。
_PERMANENT_SIX_STAR = frozenset({"别礼", "骏卫", "黎风", "艾尔黛拉", "余烬"})
# 管理员(男女两名同名条目)人人必得,不参与收集概况四图,
# 仅保留在干员持有率列表中作数据展示。
_ADMIN_SIX_STAR = frozenset({"管理员"})


def _six_star_groups(operators):
    six = [
        operator
        for operator in operators
        if operator.rarity >= 6 and operator.name not in _ADMIN_SIX_STAR
    ]
    limited = [
        operator
        for operator in six
        if operator.name not in _PERMANENT_SIX_STAR
    ]
    return six, limited


def _donut_html(
    stops: list[str],
    center_value: str,
    center_caption: str,
    *,
    empty: bool = False,
    inner: str = "",
) -> str:
    css_class = "donut donut-empty" if empty else "donut"
    background = "" if empty else f' style="background:conic-gradient({", ".join(stops)})"'
    return (
        f'<div class="{css_class}"{background}><div class="donut-center">'
        f"<b>{esc(center_value)}</b><i>{esc(center_caption)}</i>"
        f"</div>{inner}</div>"
    )


def _collection_donut_card(title: str, operators, sample_count: int) -> str:
    """收集率环形图:金色为已持有槽位,灰色为未持有,中心为收集率。"""
    total_slots = sample_count * len(operators)
    rate = sum(operator.owned_count for operator in operators) / total_slots if total_slots else None
    if rate is None:
        donut = _donut_html([], "--", "收集率", empty=True)
    else:
        edge = rate * 360.0
        stops: list[str] = []
        if rate > 0:
            stops.append(f"#ffd000 0.00deg {edge:.2f}deg")
        if rate < 1:
            stops.append(f"#d9dde0 {edge:.2f}deg 360.00deg")
        donut = _donut_html(stops, _percent(rate), "收集率")
    return _donut_card(title, donut)


def _potential_donut_card(title: str, operators) -> str:
    """潜能分布环形图:分母为该组已持有槽位(不含未持有),中心为满潜(5潜)占比。

    每个扇区的"潜能名 + 占比"统一放到环外,用引线连回扇区。
    """
    owned_slots = sum(operator.owned_count for operator in operators)
    if owned_slots <= 0:
        donut = _donut_html([], "--", "满潜率", empty=True)
    else:
        sums: dict[str, int] = {
            key: 0 for key in _BUCKET_LEGEND_ORDER if key != "unowned"
        }
        for operator in operators:
            for bucket in operator.potential_buckets:
                if bucket.key in sums:
                    sums[bucket.key] += bucket.count
        slices: list[tuple[str, float, float, float]] = []
        stops: list[str] = []
        cursor = 0.0
        for key in _BUCKET_LEGEND_ORDER:
            if key == "unowned" or sums.get(key, 0) <= 0:
                continue
            share = sums[key] / owned_slots
            start, end = cursor, min(1.0, cursor + share)
            slices.append((key, share, start * 360.0, end * 360.0))
            stops.append(f"{_BUCKET_COLORS[key][0]} {start * 360:.2f}deg {end * 360:.2f}deg")
            cursor = end
        max_rate = sums.get("potential_5", 0) / owned_slots
        donut = _donut_html(
            stops,
            _percent(max_rate),
            "满潜率",
            inner=_donut_slice_labels(slices),
        )
    return _donut_card(title, donut)


_DONUT_SIZE = 132.0
# 标注统一放在环外,引线从扇区外缘连到标注。
_DONUT_LABEL_RADIUS = _DONUT_SIZE / 2 + 24.0
_DONUT_LABEL_MIN_GAP = 14.0
_DONUT_LABEL_MAX_Y = _DONUT_SIZE + 26.0


def _donut_slice_labels(slices: list[tuple[str, float, float, float]]) -> str:
    """所有扇区的"潜能名 + 占比"标注放到环外,并用引线连回扇区。

    同侧(左/右)标注按纵坐标做最小间距排布,避免薄扇区聚集时互相压字;
    引线从扇区外缘中点连到标注锚点。
    """
    center = _DONUT_SIZE / 2
    items: list[dict[str, object]] = []
    for key, share, start_deg, end_deg in slices:
        mid_deg = (start_deg + end_deg) / 2
        rad = math.radians(mid_deg)
        sin_, cos_ = math.sin(rad), math.cos(rad)
        items.append(
            {
                "text": f"{_BUCKET_LEGEND_LABELS.get(key, key)} {share * 100:.0f}%",
                "edge_x": center + 68.0 * sin_,
                "edge_y": center - 68.0 * cos_,
                "ax": center + _DONUT_LABEL_RADIUS * sin_,
                "ay": center - _DONUT_LABEL_RADIUS * cos_,
                "right": sin_ >= 0,
            }
        )
    for side in (True, False):
        group = sorted(
            (item for item in items if item["right"] is side), key=lambda item: item["ay"]
        )
        for index, item in enumerate(group):
            if index:
                item["ay"] = max(
                    item["ay"], group[index - 1]["ay"] + _DONUT_LABEL_MIN_GAP
                )
            item["ay"] = min(item["ay"], _DONUT_LABEL_MAX_Y)
    parts: list[str] = []
    for item in items:
        gap = 4.0 if item["right"] else -4.0
        parts.append(
            _leader_line(item["edge_x"], item["edge_y"], item["ax"] + gap, item["ay"])
        )
        css_class = (
            "donut-slice-label" if item["right"] else "donut-slice-label donut-slice-left"
        )
        parts.append(
            f'<span class="{css_class}" '
            f'style="left:{item["ax"] + gap:.1f}px;top:{item["ay"]:.1f}px;color:#39444a">'
            f"{esc(item['text'])}</span>"
        )
    return "".join(parts)


def _leader_line(x1: float, y1: float, x2: float, y2: float) -> str:
    length = math.hypot(x2 - x1, y2 - y1)
    angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
    return (
        f'<i class="donut-leader" style="left:{x1:.1f}px;top:{y1:.1f}px;'
        f'width:{length:.1f}px;transform:rotate({angle:.2f}deg)"></i>'
    )


def _donut_card(title: str, donut: str) -> str:
    return (
        '<article class="overview-donut">'
        f"<h3>{esc(title)}</h3>"
        f'<div class="donut-slot">{donut}</div>'
        "</article>"
    )


def _six_star_section(report: OwnershipStatsReport) -> str:
    segment = report.segment("all")
    if segment is None or segment.valid_sample_count <= 0:
        segment = next((item for item in report.segments if item.valid_sample_count > 0), None)
    if segment is None:
        sample_count = 0
        six: list = []
        limited: list = []
    else:
        sample_count = segment.valid_sample_count
        six, limited = _six_star_groups(segment.operators)
    cards = (
        _collection_donut_card("6星干员收集率", six, sample_count),
        _collection_donut_card("6星非常驻干员收集率", limited, sample_count),
        _potential_donut_card("6星干员潜能分布", six),
        _potential_donut_card("6星非常驻干员潜能分布", limited),
    )
    return (
        '<section class="ownership-section">'
        '<div class="section-head"><h2>6星干员收集概况</h2></div>'
        f'<div class="overview-donut-grid">{"".join(cards)}</div>'
        "</section>"
    )


def _potential_legend() -> str:
    chips = "".join(
        _legend_chip(key)
        for key in _BUCKET_LEGEND_ORDER
    )
    return (
        '<div class="potential-legend"><span class="legend-title">潜能分布图例</span>'
        f"{chips}"
        '<span class="legend-note">每名干员的横条以本段全部有效样本为分母,灰色即未持有比例</span></div>'
    )


def _legend_chip(key: str) -> str:
    bg, fg = _BUCKET_COLORS.get(key, ("#c4cacf", "#5f6a70"))
    stripes = " bucket-unknown" if key == "unknown" else ""
    return (
        f'<span class="legend-chip"><em class="legend-swatch{stripes}" '
        f'style="background:{bg}"></em>{esc(_BUCKET_LEGEND_LABELS.get(key, key))}</span>'
    )


def _segment_section(segment: OwnershipStatsSegment, icon_map: dict[str, str]) -> str:
    region = _REGION_LABELS.get(segment.region, segment.region)
    accent = _REGION_ACCENTS.get(segment.region, "#20262a")
    rows: list[str] = []
    last_rarity: int | None = None
    for index, operator in enumerate(segment.operators, start=1):
        if operator.rarity != last_rarity:
            last_rarity = operator.rarity
            rows.append(_op_group_label(operator.rarity, segment.operators))
        rows.append(_operator_row(operator, icon_map, index))
    return (
        '<section class="ownership-section segment-section">'
        f'<div class="section-head">'
        f'<h2 style="color:{accent}">{esc(region)}干员持有率</h2>'
        "</div>"
        f'<div class="op-list">{"".join(rows)}</div>'
        f"{_summaries_html(segment)}"
        "</section>"
    )


def _op_group_label(rarity: int, operators) -> str:
    # 稀有度分块的视觉分隔;顺序仍完全遵循后端排序,仅作分组展示。
    count = sum(1 for operator in operators if operator.rarity == rarity)
    if rarity >= 6:
        color = "#c99700"
    elif rarity >= 1:
        color = "#286cd6"
    else:
        color = "#6f7d86"
        return (
            f'<div class="op-group"><i style="background:{color}"></i>'
            f"观测补录干员 · {count} 名</div>"
        )
    return (
        f'<div class="op-group"><i style="background:{color}"></i>'
        f"{rarity}星干员 · {count} 名</div>"
    )


def _operator_row(
    operator: OperatorOwnership, icon_map: dict[str, str], rank: int
) -> str:
    rarity_class = f" rarity-{max(0, min(operator.rarity, 9))}"
    rate = _percent(operator.ownership_rate)
    owned_note = f"{operator.owned_count:,} 人持有" if operator.sample_count else "无有效样本"
    return (
        f'<article class="op-row{rarity_class}">'
        f'<span class="op-rank">{rank}</span>'
        f'<span class="op-avatar-frame">{_avatar_img(icon_map, operator)}</span>'
        '<div class="op-identity">'
        f'<b><span class="op-name">{esc(operator.name)}</span>{_stars(operator.rarity)}</b>'
        f'<span class="op-profession">{esc(operator.profession)}</span>'
        "</div>"
        f'<div class="op-rate"><b>{esc(rate)}</b><i>{esc(owned_note)}</i></div>'
        f'<div class="op-bar">{_bucket_bar(operator.potential_buckets)}</div>'
        "</article>"
    )


def _avatar_img(icon_map: dict[str, str], operator: OperatorOwnership) -> str:
    url = operator_avatar_url(operator)
    resolved = icon_map.get(url, "") if url else ""
    if resolved:
        return (
            f'<img class="op-avatar" src="{esc_attr(resolved)}" alt="{esc_attr(operator.name)}">'
        )
    initial = esc(operator.name[:1]) if operator.name else "?"
    return f'<span class="op-avatar-fallback">{initial}</span>'


def _stars(rarity: int) -> str:
    if rarity <= 0:
        return ""
    star = _local_image_data_url(ASSET_DIR / "rarity-star.png")
    if star:
        img = f'<img class="op-star" src="{esc_attr(star)}" alt="★">'
        return f'<span class="op-stars">{img * rarity}</span>'
    return f'<span class="op-stars op-stars-text">{"★" * rarity}</span>'


def _bucket_bar(buckets: tuple[PotentialBucket, ...]) -> str:
    cells = "".join(_bucket_cell(bucket) for bucket in buckets)
    return f'<div class="op-bar-track">{cells}</div>'


def _bucket_cell(bucket: PotentialBucket) -> str:
    bg, fg = _BUCKET_COLORS.get(bucket.key, ("#c4cacf", "#5f6a70"))
    stripes = " bucket-unknown" if bucket.key == "unknown" else ""
    grows = max(0, bucket.count)
    min_width = "min-width:2px;" if grows else ""
    label = ""
    if bucket.rate is not None and bucket.rate >= _BUCKET_LABEL_MIN_RATE and grows:
        label = (
            f'<span style="color:{fg}">{esc(_BUCKET_LEGEND_LABELS.get(bucket.key, bucket.key))} '
            f"{bucket.rate * 100:.0f}%</span>"
        )
    return (
        f'<div class="op-bucket{stripes}" style="background:{bg};flex-grow:{grows};{min_width}">'
        f"{label}</div>"
    )


def _summaries_html(segment: OwnershipStatsSegment) -> str:
    profession_rows = "".join(_summary_row(item) for item in segment.professions)
    rarity_rows = "".join(_summary_row(item) for item in segment.rarities)
    if not profession_rows and not rarity_rows:
        return ""
    return (
        '<div class="summary-cols">'
        f'<div class="summary-col"><h3>按职业平均收集率</h3>{profession_rows or _summary_empty()}</div>'
        f'<div class="summary-col"><h3>按稀有度平均收集率</h3>{rarity_rows or _summary_empty()}</div>'
        "</div>"
    )


def _summary_row(item) -> str:
    label = f"{item.label}★" if item.kind == "rarity" else item.label
    rate = _percent(item.collection_rate)
    fill = 0 if item.collection_rate is None else max(2, min(100, round(item.collection_rate * 100)))
    # 稀有度列用金色条,与该列标题的强调色呼应;职业列保持蓝色。
    color = "#d59800" if item.kind == "rarity" else "#286cd6"
    return (
        '<div class="summary-row">'
        f"<b>{esc(label)}</b>"
        f'<div class="summary-bar"><em style="width:{fill}%;background:{color}"></em></div>'
        f'<span>{item.operator_count} 干员 · {item.owned_slots:,}/{item.possible_slots:,} 格</span>'
        f"<strong>{esc(rate)}</strong>"
        "</div>"
    )


def _summary_empty() -> str:
    return '<div class="empty-segment-note">暂无汇总数据</div>'


def _empty_segment_note(segment: OwnershipStatsSegment) -> str:
    region = _REGION_LABELS.get(segment.region, segment.region)
    if segment.eligible_sample_count:
        detail = f"合格 {segment.eligible_sample_count:,} 个角色 · 已排除 {segment.excluded_sample_count:,} 个(过期或无完整快照)"
    else:
        detail = "当前范围内没有仍绑定的游戏角色"
    return (
        '<section class="ownership-section">'
        f'<div class="empty-segment-note">{esc(region)} · 暂无有效样本,{detail}。</div>'
        "</section>"
    )


def _percent(rate: float | None) -> str:
    return "--" if rate is None else f"{rate * 100:.1f}%"


def _css() -> str:
    width = OWNERSHIP_STATS_WIDTH
    return f"""
* {{ box-sizing:border-box; }}
html,body {{ margin:0; width:{width}px; background:#d9dde0; font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",Arial,sans-serif; color:#171b1f; }}
.ownership-stats {{ width:{width}px; padding:30px; background:linear-gradient(90deg,rgba(29,34,39,.065) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(0deg,rgba(29,34,39,.065) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(135deg,#f8f9f6,#e6eaeb); }}
.ownership-header {{ padding:24px 28px 20px; border:1px solid rgba(23,27,31,.28); background:rgba(249,250,248,.96); box-shadow:0 12px 32px rgba(23,27,31,.10); }}
.ownership-title-row {{ display:flex; align-items:flex-start; justify-content:space-between; gap:20px; }}
.ownership-title-row h1 {{ margin:0; font-size:36px; line-height:1; font-weight:950; letter-spacing:-.035em; }}
.ownership-scope-badge {{ flex:0 0 auto; padding:9px 14px; border-left:6px solid #ffd000; background:#20252a; color:#fff; font-size:17px; font-weight:950; }}
.ownership-meta {{ margin-top:13px; color:#667077; font-size:14px; font-weight:850; }}
.ownership-refresh {{ margin-top:16px; padding:11px 16px; display:flex; flex-wrap:wrap; align-items:center; gap:6px 16px; border:1px solid rgba(23,27,31,.4); border-left:6px solid #ffd000; background:#20252a; color:#c8cfd4; font-size:13px; font-weight:850; }}
.ownership-refresh b {{ color:#ffd000; font-weight:950; }}
.ownership-section {{ margin-top:16px; padding:14px; border:1px solid rgba(23,27,31,.25); background:rgba(249,250,248,.95); }}
.section-head {{ display:flex; justify-content:space-between; align-items:flex-end; gap:16px; margin-bottom:12px; padding-bottom:9px; border-bottom:4px solid #20262a; }}
.section-head h2 {{ margin:0; font-size:24px; font-weight:950; }}
.sample-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; }}
.sample-card {{ min-height:96px; padding:12px 14px; border:1px solid #abb2b5; background:rgba(255,255,255,.88); }}
.sample-card span,.sample-card i {{ display:block; color:#6b7479; font-size:12px; font-weight:900; font-style:normal; }}
.sample-card b {{ display:block; margin:6px 0; font-size:27px; line-height:1; font-weight:950; }}
.sample-card i {{ color:#8c959a; font-size:11px; }}
.overview-donut-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }}
.overview-donut {{ min-width:0; padding:13px 12px 12px; border:1px solid #abb2b5; background:rgba(255,255,255,.88); text-align:center; }}
.overview-donut h3 {{ margin:0 0 8px; color:#2c373d; font-size:14px; font-weight:950; letter-spacing:.01em; }}
.donut {{ position:relative; width:132px; height:132px; margin:0 auto; border-radius:50%; border:1px solid rgba(23,27,31,.35); }}
.donut-slot {{ padding:30px 0 38px; }}
.donut-leader {{ position:absolute; z-index:1; height:1.5px; background:#8a949a; transform-origin:0 50%; }}
.donut-slice-label {{ position:absolute; z-index:2; font-size:10px; font-weight:950; white-space:nowrap; transform:translateY(-50%); }}
.donut-slice-left {{ transform:translate(-100%,-50%); }}
.donut::before {{ content:""; position:absolute; inset:18px; border-radius:50%; background:#fbfcfb; box-shadow:0 0 0 1px rgba(23,27,31,.12); }}
.donut-center {{ position:absolute; inset:0; z-index:1; display:flex; flex-direction:column; align-items:center; justify-content:center; text-align:center; }}
.donut-center b {{ font-size:19px; line-height:1; font-weight:950; color:#20252a; }}
.donut-center i {{ margin-top:4px; color:#8c959a; font-size:10px; font-weight:900; font-style:normal; letter-spacing:.08em; }}
.donut-empty {{ background:#dfe3e5; }}
.potential-legend {{ margin-top:10px; padding:9px 12px; display:flex; flex-wrap:wrap; align-items:center; gap:7px 14px; border:1px dashed #9aa2a5; background:#eef1f1; color:#5f6a70; font-size:12px; font-weight:850; }}
.legend-title {{ font-weight:950; color:#39444a; }}
.legend-chip {{ display:inline-flex; align-items:center; gap:6px; }}
.legend-swatch {{ width:16px; height:11px; border:1px solid rgba(23,27,31,.4); }}
.legend-note {{ margin-left:auto; color:#8c959a; font-size:11px; }}
.segment-section {{ min-height:220px; }}
.op-list {{ display:flex; flex-direction:column; gap:7px; }}
.op-group {{ display:flex; align-items:center; gap:7px; margin-top:10px; color:#5f6a70; font-size:12px; font-weight:950; }}
.op-group:first-child {{ margin-top:1px; }}
.op-group i {{ width:10px; height:10px; border:1px solid rgba(23,27,31,.35); }}
.op-row {{ display:grid; grid-template-columns:38px 48px minmax(0,1fr) 128px minmax(300px,1.1fr); gap:10px; align-items:center; min-height:60px; padding:7px 12px 7px 8px; border:1px solid rgba(23,27,31,.2); border-left:6px solid #6f7d86; background:#f4f6f5; }}
.op-row.rarity-6 {{ border-left-color:#ffd000; }}
.op-row.rarity-5 {{ border-left-color:#286cd6; }}
.op-rank {{ font-size:14px; font-weight:950; color:#788389; text-align:center; }}
.op-avatar-frame {{ width:48px; height:48px; overflow:hidden; display:grid; place-items:center; background:linear-gradient(160deg,#e8ecee,#cfd6d9); border-bottom:3px solid #9aa2a5; }}
.op-avatar {{ width:100%; height:100%; object-fit:cover; display:block; }}
.op-avatar-fallback {{ font-size:20px; font-weight:950; color:#7d878c; }}
.op-identity {{ min-width:0; }}
.op-identity b {{ display:flex; align-items:center; gap:5px; min-width:0; overflow:hidden; color:#2c373d; font-size:15px; font-weight:950; white-space:nowrap; }}
.op-name {{ overflow:hidden; text-overflow:ellipsis; }}
.op-stars {{ display:inline-flex; flex:0 0 auto; gap:1px; }}
.op-star {{ width:12px; height:12px; object-fit:contain; }}
.op-stars-text {{ color:#c99700; font-size:11px; letter-spacing:1px; }}
.op-profession {{ display:inline-block; margin-top:4px; padding:1px 7px; border:1px solid #abb2b5; background:#fff; color:#5f6a70; font-size:10px; font-weight:900; }}
.op-rate {{ text-align:right; }}
.op-rate b {{ display:block; font-size:20px; line-height:1.05; font-weight:950; color:#20252a; }}
.op-rate i {{ display:block; margin-top:3px; color:#7d878c; font-size:11px; font-weight:850; font-style:normal; }}
.op-bar {{ min-width:0; }}
.op-bar-track {{ display:flex; height:22px; overflow:hidden; border:1px solid rgba(23,27,31,.45); background:#e3e7e9; }}
.op-bucket {{ min-width:0; display:flex; align-items:center; justify-content:center; overflow:hidden; }}
.op-bucket span {{ font-size:10px; font-weight:950; white-space:nowrap; }}
.bucket-unknown {{ background-image:repeating-linear-gradient(45deg,rgba(255,255,255,.55) 0 4px,transparent 4px 8px) !important; }}
.summary-cols {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-top:12px; }}
.summary-col {{ min-width:0; }}
.summary-col h3 {{ margin:0 0 8px; padding-left:9px; border-left:5px solid #286cd6; color:#39444a; font-size:15px; font-weight:950; }}
.summary-col:nth-child(2) h3 {{ border-left-color:#d59800; }}
.summary-row {{ display:grid; grid-template-columns:88px minmax(0,1fr) max-content 72px; gap:12px; align-items:center; min-height:32px; padding:4px 0; border-bottom:1px solid rgba(23,27,31,.12); }}
.summary-row b {{ color:#2c373d; font-size:13px; font-weight:950; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }}
.summary-bar {{ height:10px; background:#dfe3e5; border:1px solid rgba(23,27,31,.25); }}
.summary-bar em {{ display:block; height:100%; background:#286cd6; }}
.summary-row span {{ color:#8c959a; font-size:10px; font-weight:850; white-space:nowrap; }}
.summary-row strong {{ color:#20252a; font-size:15px; font-weight:950; text-align:right; }}
.empty-segment-note {{ padding:14px; border:1px dashed #9aa2a5; background:#edf0f0; color:#71797d; font-size:14px; font-weight:850; }}
"""
