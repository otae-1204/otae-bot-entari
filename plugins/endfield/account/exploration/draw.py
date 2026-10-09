from __future__ import annotations

import math
from dataclasses import replace

from ...cold_start import REMOTE_ASSET_NAMESPACE, note_remote_assets
from ...rendering.cards import (
    PreparedCardHtml,
    _draw_gallery_catalog,
    _prepare_assets,
    esc,
    esc_attr,
    is_height_limit_error,
)
from ..regions import region_theme_color
from .artwork import collection_icons
from .models import (
    COLLECTION_COLUMNS,
    CollectionProgress,
    ExplorationLevel,
    ExplorationRegion,
    ExplorationView,
)
from .thumbnails import MapThumbnail, ThumbnailMap, fetch_exploration_thumbnails

PAGE_ROWS = 24
AREA_COLUMN = 300
# The dark overall tile starts at the area column's left edge; the six category
# tiles share the rest of the summary row, ending with the last data column.
OVERALL_TILE = 220
# Fits the widest column label (icon + 维修灵感点) and a "198 / 198" cell.
DATA_COLUMN = 136
# The card wraps the columns: 2 × 30 card padding, 2 × 16 section padding, 2 × 1 border.
CARD_WIDTH = AREA_COLUMN + 6 * DATA_COLUMN + 94
# How the overall tile shows its percentage:
#   "badge" a thin version-badge yellow track under the label and figure (default);
#   "edge"  a hairline along the tile's top edge;
#   "arc"   a 240° gauge open at the bottom, beside the figure;
#   "chip"  no bar, a group-head style chip beside the figure (已收满 / 未收满);
#   "spine" a vertical yellow spine on the tile's left edge, filled from the bottom.
OVERALL_PROGRESS = "badge"
# The gauge's radius (px, in a 48 px box), sweep and track length.
ARC_RADIUS = 21
ARC_SWEEP = 240
ARC_LENGTH = 2 * math.pi * ARC_RADIUS * ARC_SWEEP / 360


def paginate_exploration(
    view: ExplorationView, row_budget: int = PAGE_ROWS
) -> tuple[ExplorationView, ...]:
    """Keep every row in source order and retain parent IDs for thumbnail matching."""
    if row_budget < 1:
        raise ValueError("row_budget must be positive")
    pages: list[ExplorationView] = []
    regions: list[ExplorationRegion] = []
    used = 0
    for region in view.regions:
        offset = 0
        while offset < max(1, len(region.levels)):
            if used == row_budget:
                pages.append(replace(view, regions=tuple(regions)))
                regions, used = [], 0
            amount = min(row_budget - used, max(1, len(region.levels)) - offset)
            regions.append(
                replace(region, levels=region.levels[offset : offset + amount])
            )
            offset += amount
            used += amount
    if regions or not pages:
        pages.append(replace(view, regions=tuple(regions)))
    return tuple(pages)


async def draw_exploration_cards(view: ExplorationView) -> tuple[bytes, ...]:
    thumbnails = await fetch_exploration_thumbnails(view)
    last_error: RuntimeError | None = None
    for budget in (PAGE_ROWS, 12, 6, 1):
        pages = paginate_exploration(view, budget)
        try:
            images = []
            for index, page in enumerate(pages, 1):
                prepared = await prepare_exploration_html(
                    page,
                    thumbnails=thumbnails,
                    page_number=index,
                    page_count=len(pages),
                )
                images.append(
                    await _draw_gallery_catalog(
                        prepared,
                        ".exploration-card",
                        (
                            ".exploration-header",
                            ".exploration-summary",
                            ".exploration-table",
                            ".legend",
                            ".exploration-footer",
                        ),
                        "account_exploration",
                    )
                )
            return tuple(images)
        except RuntimeError as exc:
            if not is_height_limit_error(exc):
                raise
            last_error = exc
    raise last_error or RuntimeError("地区探索图片分页失败")


async def prepare_exploration_html(
    view: ExplorationView,
    *,
    inline: bool = False,
    thumbnails: ThumbnailMap | None = None,
    page_number: int = 1,
    page_count: int = 1,
) -> PreparedCardHtml:
    if thumbnails is None:
        thumbnails = await fetch_exploration_thumbnails(view)
    selected = {
        (region.region_id, level.level_id): thumbnails[region.region_id, level.level_id]
        for region in view.regions
        for level in region.levels
        if (region.region_id, level.level_id) in thumbnails
    }
    urls = [tile.url for thumb in selected.values() for tile in thumb.tiles]
    await note_remote_assets(
        (url for url in urls if not url.startswith("data:")),
        namespace=REMOTE_ASSET_NAMESPACE,
    )
    assets = await _prepare_assets(urls, inline=inline)
    return PreparedCardHtml(
        render_exploration_html(
            view,
            thumbnails=selected,
            assets=assets.urls,
            page_number=page_number,
            page_count=page_count,
        ),
        assets.resources,
        CARD_WIDTH,
    )


def render_exploration_html(
    view: ExplorationView,
    *,
    page_number: int = 1,
    page_count: int = 1,
    thumbnails: ThumbnailMap | None = None,
    assets: dict[str, str] | None = None,
) -> str:
    """Each parent region heads its rows with a header row carrying the column labels."""
    identity = " · ".join(
        value
        for value in (
            view.nickname,
            view.server_name,
            f"UID {view.uid}" if view.uid else "",
        )
        if value
    )
    paginated = page_count > 1
    if view.regions:
        summary, flagged = _summary_html(view, paginated)
        body = (
            '<section class="exploration-section">'
            + summary
            + '<div class="section-head detail-head"><h2>地区明细</h2></div>'
            + _table_html(view, thumbnails or {}, assets or {}, paginated)
            + _legend_html(flagged)
            + "</section>"
        )
    else:
        body = '<div class="empty">森空岛暂未返回地区探索数据，可能尚未解锁或未公开。</div>'
    warnings = "".join(
        f'<div class="warning">{esc(warning)}</div>' for warning in view.warnings
    )
    # The header badge carries the game version; paged cards add the page number.
    version = (
        f'游戏版本 <span class="game-version">V{esc(view.version)}</span>'
        if view.version
        else '<span class="game-version">版本未知</span>'
    )
    page = (
        f'<span class="page-label">{page_number:02d} / {page_count:02d}</span>'
        if paginated
        else ""
    )
    badge_html = (
        f'<div class="header-bottom"><span class="version-label">{version}</span>'
        f"{page}</div>"
    )
    timestamp = (
        f"档案更新 {view.saved_at}（北京时间）"
        if view.saved_at
        else "档案更新时间未提供"
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>{_css()}</style></head>
<body><main class="exploration-card">
  <header class="exploration-header">
    <div class="header-main">
      <div class="exploration-kicker">ENDFIELD / EXPLORATION RECORD</div>
      <h1>地区探索</h1>
      <div class="identity">{esc(identity)}</div>
    </div>
    {badge_html}
  </header>
  {body}{warnings}
  <footer class="exploration-footer"><span>数据来源 森空岛 · 游戏同步可能存在延迟</span><span>{esc(timestamp)}</span></footer>
</main></body></html>"""


def _summary_html(view: ExplorationView, paginated: bool) -> tuple[str, bool]:
    """Return the tiles and whether any sum leaves cells out (the legend explains it)."""
    levels = [level for region in view.regions for level in region.levels]
    if not levels:
        return "", False
    cells: list[str] = []
    icons = collection_icons()
    flagged = False
    for index, (key, label) in enumerate(COLLECTION_COLUMNS):
        present = [
            level.collections[index]
            for level in levels
            if not level.collections[index].absent
        ]
        # Only cells with a known, consistent count and total are summed.
        known = [(value.count, value.total) for value in present if _known(value)]
        excluded = len(present) - len(known)
        flagged = flagged or excluded > 0
        flag = f'<em class="flag">?{excluded}</em>' if excluded else ""
        mode = ""
        if not present:
            value_html = '<b class="stat-value muted"><span>—</span></b>'
        elif not known:
            value_html = f'<b class="stat-value muted"><span>--</span>{flag}</b>'
        else:
            count = sum(count for count, _ in known)
            total = sum(total for _, total in known)
            # Seven or more digits (count, total and flag) shrink the figures; from
            # nine, a flag no longer fits beside them and takes a line of its own.
            digits = len(f"{count}{total}{excluded or ''}")
            stacked = bool(excluded) and digits >= 9
            mode = (" compact" if digits >= 7 else "") + (" stacked" if stacked else "")
            value_html = (
                f'<b class="stat-value"><span>{count}</span>'
                f"<small>/ {total}</small>{'' if stacked else flag}</b>"
                + (f'<span class="stat-flags">{flag}</span>' if stacked else "")
            )
        icon = (
            f'<img class="collection-icon" src="{esc_attr(icons[key])}" alt="">'
            if icons.get(key)
            else ""
        )
        cells.append(
            f'<div class="stat-cell{mode}"><span class="stat-label">{icon}{esc(label)}</span>'
            f"{value_html}</div>"
        )
    # One row: the overall tile, then six category tiles spread over the data columns.
    head, unsure = _overall_html(levels, "本页收集度" if paginated else "收集度")
    return (
        '<div class="exploration-summary">'
        + head
        + "".join(cells)
        + "</div>",
        flagged or unsure,
    )


def _known(value: CollectionProgress) -> bool:
    return (
        value.count is not None and value.total is not None and not value.inconsistent
    )


def _overall_html(levels: list[ExplorationLevel], label: str) -> tuple[str, bool]:
    """Dark overall tile: label over the percentage, plus a progress mark.

    Only areas whose every cell is known count towards the percentage.
    """
    sure = [
        level
        for level in levels
        if all(value.absent or _known(value) for value in level.collections)
    ]
    cells = [
        value for level in sure for value in level.collections if not value.absent
    ]
    count = sum(value.count or 0 for value in cells)
    total = sum(value.total or 0 for value in cells)
    unsure = len(levels) - len(sure)
    flag = f'<em class="flag">?{unsure}</em>' if unsure else ""
    ratio = count * 100 // total if total else 0
    percent = f"<span>{ratio}</span><small>%</small>" if total else "<span>--</span>"
    mark = _progress_html(ratio if total else None, unsure)
    # The chip sits on the figure's line; the other marks follow the text.
    inline = OVERALL_PROGRESS == "chip"
    return (
        f'<div class="overall-cell {OVERALL_PROGRESS}-progress">'
        f'<span class="overall-label">{label}{flag}</span>'
        f'<b class="overall-value{"" if total else " muted"}">{percent}'
        f'{mark if inline else ""}</b>{"" if inline else mark}</div>',
        unsure > 0,
    )


def _progress_html(ratio: int | None, unsure: int) -> str:
    """The overall tile's progress mark; an unknown ratio leaves only the track."""
    if OVERALL_PROGRESS == "chip":
        # A full sum with areas left out cannot be called complete.
        if ratio is None or (ratio == 100 and unsure):
            state, text = "unknown", "数据不全"
        else:
            state, text = ("full", "已收满") if ratio == 100 else ("short", "未收满")
        return f'<em class="overall-chip {state}">{text}</em>'
    if OVERALL_PROGRESS == "arc":
        return _arc_html(ratio or 0)
    # "spine" fills upwards; "edge" and "badge" fill from the left.
    side = "height" if OVERALL_PROGRESS == "spine" else "width"
    fill = f'<i style="{side}:{ratio}%"></i>' if ratio else ""
    return f'<span class="overall-{OVERALL_PROGRESS}" aria-hidden="true">{fill}</span>'


def _arc_html(ratio: int) -> str:
    """A gauge open at the bottom: faint track, yellow arc for the ratio."""
    circle = f'cx="24" cy="24" r="{ARC_RADIUS}" stroke-dasharray'
    fill = (
        f'<circle class="on" {circle}="{ARC_LENGTH * ratio / 100:.2f} 200"/>'
        if ratio
        else ""
    )
    # The stroke starts at three o'clock; turning it centres the gap at the bottom.
    start = 90 + (360 - ARC_SWEEP) / 2
    return (
        '<svg class="overall-arc" viewBox="0 0 48 48" aria-hidden="true">'
        f'<g transform="rotate({start:g} 24 24)"><circle {circle}="{ARC_LENGTH:.2f} 200"/>'
        f"{fill}</g></svg>"
    )


def _legend_html(flagged: bool) -> str:
    items = [
        '<span><b class="absent">—</b>无此类收集物</span>',
        "<span><b>?</b>数据未提供</span>",
        '<span><b class="mark">!</b>数量异常（已收集超过总量）</span>',
    ]
    if flagged:
        items.append(
            '<span><em class="flag">?N</em>概况合计未计入 N 个数据不全的地区</span>'
        )
    return '<div class="legend">' + "".join(items) + "</div>"


def _table_html(
    view: ExplorationView,
    thumbnails: ThumbnailMap,
    assets: dict[str, str],
    paginated: bool,
) -> str:
    icons = collection_icons()
    labels = "".join(
        '<span class="group-col">'
        + (
            f'<img class="collection-icon" src="{esc_attr(icons[key])}" alt="">'
            if icons.get(key)
            else ""
        )
        + f"{esc(label)}</span>"
        for key, label in COLLECTION_COLUMNS
    )
    groups = "".join(
        _region_rows(region, labels, thumbnails, assets, paginated)
        for region in view.regions
    )
    return f"""<table class="exploration-table"><colgroup><col class="area-col">{"<col>" * 6}</colgroup>{groups}</table>"""


def _region_rows(
    region: ExplorationRegion,
    labels: str,
    thumbnails: ThumbnailMap,
    assets: dict[str, str],
    paginated: bool,
) -> str:
    """One tbody per parent region, headed by its name and the column labels."""
    count = len(region.levels)
    scope = f"本页 {count} 个地区" if paginated else f"{count} 个地区"
    tag = f"<em>{scope}</em>" if count else ""
    # A single spanning cell keeps the ink band seamless; its grid matches the columns.
    head = (
        f'<tbody style="--region-color:{esc_attr(region_theme_color(region.region_id, region.name))}">'
        '<tr class="group"><th colspan="7" scope="rowgroup"><div class="group-head">'
        f'<span class="group-name"><b>{esc(region.name)}</b>{tag}</span>{labels}</div></th></tr>'
    )
    if not region.levels:
        return (
            head
            + f'<tr><td class="empty" colspan="7">{esc(region.name)}暂无地区明细，可能尚未解锁或未公开。</td></tr></tbody>'
        )
    # Every second row of a group is tinted; counting restarts under each head.
    return head + "".join(
        ('<tr class="alt">' if index % 2 else "<tr>")
        + '<th scope="row"><div class="area-name">'
        + _thumbnail_html(thumbnails.get((region.region_id, level.level_id)), assets)
        + f'<span class="area-label"><span>{esc(level.name)}</span></span></div></th>'
        + "".join(_cell(progress) for progress in level.collections)
        + "</tr>"
        for index, level in enumerate(region.levels)
    ) + "</tbody>"


def _thumbnail_html(thumb: MapThumbnail | None, assets: dict[str, str]) -> str:
    if thumb is None or not all(assets.get(tile.url) for tile in thumb.tiles):
        return '<span class="map-thumb map-missing" title="缩略图暂不可用" aria-hidden="true"></span>'
    # The source grid uses increasing Y from bottom to top. Fit the entire grid.
    ratio = max(thumb.columns, thumb.rows)
    tiles = "".join(
        f'<img src="{esc_attr(assets[tile.url])}" alt="" '
        f'style="left:{tile.column / thumb.columns * 100}%;top:{tile.row / thumb.rows * 100}%;'
        f'width:{100 / thumb.columns}%;height:{100 / thumb.rows}%">'
        for tile in thumb.tiles
    )
    return (
        f'<span class="map-thumb{" map-official" if thumb.official else ""}" aria-hidden="true"><span class="map-grid" '
        f'style="width:{thumb.columns / ratio * 100}%;height:{thumb.rows / ratio * 100}%">'
        f"{tiles}</span></span>"
    )


def _cell(value: CollectionProgress) -> str:
    if value.absent:
        return '<td class="absent">—</td>'
    count = str(value.count) if value.count is not None else "?"
    total = str(value.total) if value.total is not None else "?"
    if value.inconsistent:
        status = "anomaly"
    elif value.count is None or value.total is None:
        status = "unknown"
    elif value.complete:
        status = "complete"
    else:
        status = "pending"
    marker = "<sup>!</sup>" if value.inconsistent else ""
    return (
        f'<td class="{status}"><strong>{count}{marker}</strong>'
        f"<small>/ {total}</small></td>"
    )


def _css() -> str:
    # Only 700 and 500: CJK fonts on the render hosts ship just Regular/Bold.
    return f"""
*{{box-sizing:border-box}}
html,body{{margin:0;width:{CARD_WIDTH}px;background:#d9dde0;color:#171b1f;font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",Arial,sans-serif;font-weight:500}}
.exploration-card{{width:{CARD_WIDTH}px;padding:30px;overflow:visible;background:linear-gradient(90deg,rgba(29,34,39,.065) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(0deg,rgba(29,34,39,.065) 1px,transparent 1px) 0 0/40px 40px,linear-gradient(135deg,#f7f8f4 0%,#e7eaeb 60%,#cfd5d9 100%)}}
.exploration-header{{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:end;gap:32px;padding:24px 30px 22px;background:#171b1f;color:#fff;border-bottom:7px solid #ffd000}}
.header-main{{min-width:0}}
.exploration-kicker{{color:#9ca5ab;font-size:14px;line-height:1.3;font-weight:500;letter-spacing:.14em}}
.exploration-header h1{{margin:10px 0 12px;font-size:46px;line-height:1.1;font-weight:700;letter-spacing:0}}
.identity{{color:#d4d9dc;font-size:17px;line-height:1.4;font-weight:500;overflow-wrap:anywhere}}
.header-bottom{{display:flex;gap:10px;margin-bottom:4px}}
.page-label,.version-label{{display:inline-block;padding:8px 14px;border-left:6px solid #ffd000;background:#2a3136;color:#fff;font-size:17px;line-height:1.3;font-weight:700;white-space:nowrap;font-variant-numeric:tabular-nums}}
.version-label{{color:#d4d9dc;font-weight:500}}
.version-label .game-version{{color:#fff;font-weight:700}}
.exploration-section{{margin-top:18px;padding:16px;border:1px solid rgba(23,27,31,.25);background:rgba(249,250,248,.95)}}
.section-head{{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;padding-bottom:9px;border-bottom:4px solid #20262a}}
.detail-head{{margin-top:20px}}
.section-head h2{{position:relative;margin:0;padding-left:18px;font-size:24px;line-height:1.2;font-weight:700}}
.section-head h2::before{{content:"";position:absolute;left:0;top:2px;bottom:2px;width:8px;background:#ffd000}}
.exploration-summary{{display:grid;grid-template-columns:{OVERALL_TILE}px repeat(6,minmax(0,1fr));column-gap:8px}}
/* A quiet 80 px strip: one dark summary tile, then six light category tiles. */
.overall-cell{{position:relative;display:flex;flex-direction:column;justify-content:center;min-width:0;padding:0 22px;background:#20252a;white-space:nowrap}}
.overall-label{{display:flex;align-items:center;height:20px;color:#9aa2a5;font-size:13px;line-height:20px;font-weight:700}}
.overall-label .flag{{margin-left:6px}}
.overall-value{{display:flex;align-items:baseline;height:32px;margin-top:2px;color:#fff;font-size:32px;line-height:32px;font-weight:700;font-variant-numeric:tabular-nums;overflow-y:clip}}
.overall-value small{{margin-left:2px;font-size:16px;line-height:1;font-weight:700}}
.overall-value.muted{{color:#9aa2a5}}
/* "arc": a 48 px gauge at the right, centred on the tile. */
.overall-arc{{position:absolute;top:50%;right:20px;width:48px;height:48px;margin-top:-24px}}
.overall-arc circle{{fill:none;stroke:rgba(255,255,255,.15);stroke-width:5}}
.overall-arc circle.on{{stroke:#ffd000}}
/* "edge": a full-width 2 px hairline on the tile's top edge. */
.overall-edge{{position:absolute;top:0;right:0;left:0;height:2px;background:rgba(255,255,255,.12)}}
.overall-edge i{{display:block;height:100%;background:#ffd000}}
.overall-cell.edge-progress{{padding-top:2px}}
/* "chip": the group-head chip on the figure's line; yellow only once complete. */
.overall-chip{{align-self:center;display:inline-flex;align-items:center;height:22px;margin-left:10px;padding:0 8px;border:1px solid rgba(255,255,255,.38);color:#d4d9dc;font-size:14px;line-height:1;font-style:normal;font-weight:500;white-space:nowrap}}
.overall-chip.full{{border-color:rgba(255,208,0,.6);color:#ffd000}}
.overall-chip.unknown{{color:#9aa2a5}}
/* "spine": the version badge's 6 px yellow spine on the tile's left edge, filled upwards. */
.overall-spine{{position:absolute;top:0;bottom:0;left:0;width:6px;background:#3a4248}}
.overall-spine i{{position:absolute;right:0;bottom:0;left:0;background:#ffd000}}
/* "badge": a 3 px version-badge yellow track spanning the text column, 10 px under
   the figure; label, figure and track are centred together as one quiet stack. */
.overall-badge{{display:block;height:3px;margin-top:10px;overflow:hidden;border-radius:1.5px;background:#4a5258}}
.overall-badge i{{display:block;height:100%;border-radius:1.5px;background:#ffd000}}
.stat-cell{{display:flex;flex-direction:column;justify-content:center;min-width:0;padding:11px 6px 11px 8px;white-space:nowrap;border:1px solid #abb2b5;background:rgba(255,255,255,.88)}}
.stat-label{{display:flex;align-items:center;gap:6px;height:21px;color:#5f6a70;font-size:15px;line-height:21px;font-weight:500}}
.collection-icon{{flex:none;width:20px;height:20px;object-fit:contain;filter:brightness(0) saturate(100%);opacity:.58}}
.stat-value{{display:flex;align-items:baseline;gap:5px;height:32px;margin-top:3px;color:#171b1f;font-size:26px;line-height:32px;font-weight:700;font-variant-numeric:tabular-nums}}
.stat-value small{{color:#7d878c;font-size:16px;line-height:1;font-weight:500}}
.stat-value .flag{{margin-left:0}}
.stat-value.muted{{color:#9aa2a5}}
/* Long sums shrink the figures; a flag that still doesn't fit takes a third line. */
.stat-cell.compact .stat-value{{gap:3px;font-size:22px}}
.stat-cell.compact .stat-value small{{font-size:14px}}
.stat-cell.compact .flag{{padding:0 4px;font-size:12px}}
.stat-cell.stacked{{padding-top:3px;padding-bottom:3px}}
.stat-cell.stacked .stat-value{{height:28px;margin-top:2px;line-height:28px}}
.stat-flags{{display:flex;justify-content:flex-end;height:20px;margin-top:1px}}
.flag{{align-self:center;display:inline-flex;align-items:center;height:20px;margin-left:3px;padding:0 6px;border:1px solid rgba(23,27,31,.3);background:#edf0f0;color:#4d575e;font-size:13px;line-height:1;font-style:normal;font-weight:700;font-variant-numeric:tabular-nums}}
.exploration-table{{width:100%;margin-top:12px;table-layout:fixed;border-collapse:separate;border-spacing:0;border-bottom:2px solid #20252a}}
col.area-col{{width:{AREA_COLUMN}px}}
tbody tr{{background:#fff}}
tbody tr.alt{{background:#fcf6df}}
tbody th,tbody td{{height:80px}}
tbody th{{padding:8px 12px 8px 10px;text-align:left;font-weight:700}}
.area-name{{display:flex;align-items:center;gap:14px}}
.area-label{{min-width:0;font-size:21px;line-height:1.2;overflow-wrap:anywhere}}
.area-label>span{{display:block;text-wrap:balance}}
.map-thumb{{flex:none;width:60px;height:60px;display:flex;align-items:center;justify-content:center;overflow:hidden;background:#20252a}}
.map-official{{background:transparent}}
.map-grid{{position:relative;display:block}}.map-grid img{{position:absolute;display:block}}
.map-missing{{background:#edf0f0;border:1px dashed #9aa2a5}}
td{{padding:8px 6px;text-align:center;font-variant-numeric:tabular-nums;white-space:nowrap}}
td strong{{font-size:26px;line-height:1.2;font-weight:700;color:#171b1f}}
td small{{margin-left:5px;color:#7d878c;font-size:15px;font-weight:500}}
td.unknown strong{{color:#7d878c}}
td.anomaly strong,td.anomaly sup{{color:#d64035}}
sup{{margin-left:2px;font-size:15px;font-weight:700;vertical-align:11px;line-height:0}}
td.absent{{color:#858e93;font-size:18px;font-weight:700}}
tbody tr.group{{background:none}}
tbody tr.group th{{height:auto;padding:0;background:#20252a padding-box;color:#fff}}
/* Groups close with an ink rule: the next head draws it atop its gap, the table border ends the last. */
tbody+tbody tr.group th{{border-top:14px solid transparent;background:linear-gradient(#20252a,#20252a) top/100% 2px no-repeat border-box,#20252a padding-box}}
.group-head{{display:grid;grid-template-columns:{AREA_COLUMN}px repeat(6,minmax(0,1fr));min-height:52px}}
.group-name{{display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px;padding:6px 12px 6px 22px;box-shadow:inset 8px 0 var(--region-color,#ffd000)}}
.group-col{{display:flex;align-items:center;justify-content:center;gap:8px;padding:0 6px;font-size:16px;line-height:1.2;font-weight:700;white-space:nowrap}}
tr.group .collection-icon{{width:22px;height:22px;filter:brightness(0) invert(1);opacity:.9}}
tr.group b{{font-size:20px;line-height:1.2;font-weight:700}}
tr.group em{{display:inline-flex;align-items:center;height:22px;padding:0 8px;border:1px solid color-mix(in srgb,var(--region-color,#fff) 60%,transparent);font-size:14px;line-height:1;font-style:normal;font-weight:500;font-variant-numeric:tabular-nums;white-space:nowrap}}
.empty{{padding:24px;border:1px dashed #9aa2a5;background:#edf0f0;color:#697279;font-size:17px;line-height:1.5;font-weight:500;white-space:normal}}
.exploration-card>.empty{{margin-top:18px}}
.exploration-table td.empty{{height:80px;border:0;padding:0 24px;text-align:left}}
.legend{{display:flex;flex-wrap:wrap;align-items:center;gap:8px 24px;margin-top:12px;padding:9px 14px;border:1px dashed #9aa2a5;background:#edf0f0;color:#5f6a70;font-size:13px;line-height:1.4;font-weight:500}}
.legend>span{{display:inline-flex;align-items:center;gap:8px}}
.legend b{{width:26px;height:22px;display:inline-grid;place-items:center;border:1px solid rgba(23,27,31,.28);background:#fff;color:#6b7479;font-size:14px;line-height:1;font-weight:700}}
.legend b.mark{{color:#d64035}}
.legend b.absent{{color:#858e93}}
.legend .flag{{margin-left:0;background:#fff}}
.warning{{margin-top:16px;padding:13px 18px;border-left:8px solid #ffd000;background:#fff;color:#303941;font-size:15px;line-height:1.5;font-weight:500;overflow-wrap:anywhere}}
.exploration-footer{{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px 20px;margin-top:16px;padding-top:12px;border-top:3px solid #20262a;color:#697279;font-size:13px;line-height:1.4;font-weight:500}}
"""
