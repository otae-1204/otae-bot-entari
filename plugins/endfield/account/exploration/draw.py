from __future__ import annotations

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
from .artwork import collection_icons
from .models import (
    COLLECTION_COLUMNS,
    CollectionProgress,
    ExplorationRegion,
    ExplorationView,
)
from .thumbnails import MapThumbnail, ThumbnailMap, fetch_exploration_thumbnails

CARD_WIDTH = 1040
PAGE_ROWS = 24


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
                            ".exploration-table",
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
    identity = " · ".join(
        value
        for value in (
            view.nickname,
            view.server_name,
            f"UID {view.uid}" if view.uid else "",
        )
        if value
    )
    table = (
        _table_html(view, thumbnails or {}, assets or {})
        if view.regions
        else '<div class="empty">森空岛暂未返回地区探索数据，可能尚未解锁或未公开。</div>'
    )
    warnings = "".join(
        f'<div class="warning">{esc(warning)}</div>' for warning in view.warnings
    )
    paging = f" · {page_number:02d} / {page_count:02d}" if page_count > 1 else ""
    version = f"V{view.version}" if view.version else "版本未知"
    timestamp = (
        f"档案更新 {view.saved_at}（北京时间）"
        if view.saved_at
        else "档案更新时间未提供"
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>{_css()}</style></head>
<body><main class="exploration-card">
  <header class="exploration-header">
    <div class="eyebrow">ENDFIELD / EXPLORATION RECORD</div>
    <div class="title-line"><h1>地区探索</h1><span class="page-label">COLLECTION INDEX · <span class="game-version">{esc(version)}</span>{paging}</span></div>
    <div class="identity">{esc(identity)}</div>
    <div class="header-bottom"><span>{view.level_count} 个地区{"（本页）" if page_count > 1 else ""}</span><span>已收集 / 总量</span></div>
  </header>
  {table}{warnings}
  <div class="legend"><span><b>—</b> 该地区无此类收集物</span><span><b>?</b> 数据未提供</span><span><b class="mark">!</b> 数量异常</span></div>
  <footer class="exploration-footer"><span>数据来源 森空岛 · 游戏同步可能存在延迟</span><span>{esc(timestamp)}</span></footer>
</main></body></html>"""


def _table_html(
    view: ExplorationView,
    thumbnails: ThumbnailMap,
    assets: dict[str, str],
) -> str:
    icons = collection_icons()
    headers = "".join(
        '<th scope="col">'
        + (
            f'<img class="collection-icon" src="{esc_attr(icons[key])}" alt="">'
            if icons.get(key)
            else ""
        )
        + f'<span class="collection-label">{esc(label)}</span></th>'
        for key, label in COLLECTION_COLUMNS
    )
    rows = "".join(_region_rows(region, thumbnails, assets) for region in view.regions)
    return f"""<table class="exploration-table"><colgroup><col class="area-col">{"<col>" * 6}</colgroup><thead><tr><th scope="col">地区</th>{headers}</tr></thead><tbody>{rows}</tbody></table>"""


def _region_rows(
    region: ExplorationRegion, thumbnails: ThumbnailMap, assets: dict[str, str]
) -> str:
    if not region.levels:
        return f'<tr><td class="empty" colspan="7">{esc(region.name)}暂无地区明细，可能尚未解锁或未公开。</td></tr>'
    return "".join(
        '<tr><th scope="row"><div class="area-name">'
        + _thumbnail_html(thumbnails.get((region.region_id, level.level_id)), assets)
        + f'<span class="area-label"><span>{esc(level.name)}</span>'
        + f'<small class="area-parent">{esc(region.name)}</small></span></div></th>'
        + "".join(_cell(progress) for progress in level.collections)
        + "</tr>"
        for level in region.levels
    )


def _thumbnail_html(thumb: MapThumbnail | None, assets: dict[str, str]) -> str:
    if thumb is None or not all(assets.get(tile.url) for tile in thumb.tiles):
        return '<span class="map-thumb map-missing" title="缩略图暂不可用" aria-hidden="true">◇</span>'
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
    status = (
        "anomaly" if value.inconsistent else "complete" if value.complete else "pending"
    )
    marker = "<sup>!</sup>" if value.inconsistent else ""
    return f'<td class="{status}"><strong>{count}{marker}</strong><small>/{total}</small></td>'


def _css() -> str:
    return f"""
*{{box-sizing:border-box}}html,body{{margin:0;width:{CARD_WIDTH}px;background:#e5e7e8;color:#20252a;font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",Arial,sans-serif}}
.exploration-card{{width:{CARD_WIDTH}px;padding:28px;background:linear-gradient(90deg,rgba(30,36,40,.045) 1px,transparent 1px) 0 0/36px 36px,linear-gradient(0deg,rgba(30,36,40,.045) 1px,transparent 1px) 0 0/36px 36px,#edf0ef}}
.exploration-header{{border-top:8px solid #ff453b;background:#20252a;color:#fafbf8;padding:22px 26px 0}}
.eyebrow{{color:#c2c9ca;font-size:12px;font-weight:700;letter-spacing:2px}}.title-line{{display:flex;align-items:center;justify-content:space-between;gap:20px;margin:12px 0 16px}}
h1{{font-size:38px;line-height:1.2;letter-spacing:3px;margin:0}}.page-label{{font-size:13px;letter-spacing:1px;color:#aeb7bb}}.game-version{{color:#e0e5e4}}
.identity{{font-size:17px;color:#e0e5e4;overflow-wrap:anywhere;line-height:1.6}}.header-bottom{{margin-top:18px;padding:14px 0;border-top:1px solid #4b5358;display:flex;justify-content:space-between;font-size:14px;color:#d8dddd}}
.exploration-table{{margin-top:18px;width:100%;table-layout:fixed;border-collapse:collapse;background:transparent}}col.area-col{{width:31%}}thead{{color:#596368;border-bottom:1px solid #b9c2c0}}
thead th{{height:88px;padding:14px 3px;font-size:20px;font-weight:600;text-align:center}}thead th:first-child{{padding-left:18px;text-align:left;font-size:25px}}
.collection-icon{{display:block;width:30px;height:30px;object-fit:contain;margin:0 auto 4px}}.collection-label{{display:block;line-height:26px;white-space:nowrap}}
tbody th,tbody td{{height:116px;border-bottom:1px solid #cbd2d0;padding:20px 4px}}
tbody th{{font-size:24px;font-weight:600;line-height:1.6;text-align:left;padding-left:16px;overflow-wrap:anywhere}}.area-name{{display:flex;align-items:center;gap:12px}}.area-label{{min-width:0}}.area-parent{{display:block;margin-top:3px;color:#899496;font-size:20px;font-weight:400;line-height:1.5}}
.map-thumb{{display:flex;align-items:center;justify-content:center;width:72px;height:72px;flex-shrink:0;background:#3b4140;border:1px solid #626e68;overflow:hidden;border-radius:3px}}.map-official{{background:transparent;border:0}}.map-grid{{position:relative;display:block}}.map-grid img{{position:absolute;display:block}}.map-missing{{background:#e3e8e5;color:#9aa5a0;font-size:35px;border-color:#c9d0cc}}
td{{text-align:center;font-variant-numeric:tabular-nums}}td strong{{display:block;font-size:31px;font-weight:700;line-height:1.2}}td small{{display:block;font-size:20px;color:#909b9e;margin-top:7px}}
td.absent{{font-size:27px;color:#9ca6a8}}td.pending strong{{color:#d53b32}}td.anomaly strong{{color:#ba4830}}sup{{font-size:20px;margin-left:3px}}.empty{{padding:40px 24px;font-size:19px;color:#727c82;line-height:1.8}}.exploration-table .empty{{font-size:24px}}
.legend{{display:flex;flex-wrap:wrap;gap:22px;margin-top:22px;font-size:13px;color:#6a7478}}.legend b{{font-size:18px;margin-right:5px}}.mark{{color:#ba4830}}
.warning{{margin-top:16px;padding:14px 20px;background:#faece5;color:#93462e;font-size:16px;line-height:1.8}}
.exploration-footer{{display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px;margin-top:22px;padding-top:18px;border-top:1px solid #cbd1d0;font-size:12px;color:#737f84;line-height:1.6}}
"""
