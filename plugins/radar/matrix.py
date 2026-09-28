"""Compact family × effort overview inspired by the user's layout reference.

Only the structure follows the demo: one family panel, aggregate headline and
sparkline, then effort tiles. All text/numeric data comes from RadarService.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import pairwise

from .models import (
    SCORING_CONTINUOUS,
    EfficiencyPoint,
    MatrixModel,
    RadarMatrix,
    RadarMeta,
    TrendPoint,
)
from .presentation import (
    SAMPLE_REMINDER,
    RadarPage,
    number,
    percent,
    stamp,
    text,
    trend_axis_ticks,
    trend_scale,
    trend_value,
)

#: Two model cards per row, each as wide as a single report card's content.
MATRIX_WIDTH = 1568
MATRIX_MAX_HEIGHT = 20000
MATRIX_SAMPLE_REMINDER = SAMPLE_REMINDER
#: Above this many models the single long image is split into one page per
#: region, so each delivered PNG keeps a screen-friendly aspect ratio. This is
#: a delivery budget, not an upstream or statistical limit.
MATRIX_PAGE_MAX_MODELS = 12
# Text-friendly adaptation of ColorBrewer's RdYlGn hue sequence, darkened so
# every value keeps at least 4.5:1 contrast on the sakura panels and tiles.
# Fixed IQ domain, shared by model headlines, effort scores and the legend.
# Source: https://d3js.org/d3-scale-chromatic/diverging#interpolateRdYlGn
IQ_COLOR_STOPS = (
    (0.0, "#aa3034"),
    (15.0, "#aa392e"),
    (30.0, "#a64a27"),
    (45.0, "#9c5a1d"),
    (60.0, "#896616"),
    (75.0, "#7f6811"),
    (90.0, "#697123"),
    (105.0, "#56732e"),
    (120.0, "#3d7641"),
    (135.0, "#2a724c"),
    (150.0, "#18663d"),
)
IQ_MISSING_COLOR = "#8a7a7e"

_NAMES = {
    "gpt-6-astra": ("GPT-6 Astra", "astra"),
    "gpt-6-sol": ("GPT-6 Sol", "sol"),
    "gpt-6-luna": ("GPT-6 Luna", "luna"),
    "gpt-5.6-sol": ("Sol", "sol"),
    "gpt-5.6-terra": ("Terra", "terra"),
    "gpt-5.6-luna": ("Luna", "luna"),
    "gpt-5.5": ("GPT-5.5", "system"),
    "deepseek-v4-flash": ("DeepSeek V4 Flash", "deepseek"),
    "deepseek-v4.1-flash": ("DeepSeek V4.1 Flash", "deepseek"),
    "dsh-deepseek-v4-flash": ("DeepSeek V4 Flash · DSH", "deepseek"),
    "dsh-deepseek-v4.1-flash": ("DeepSeek V4.1 Flash · DSH", "deepseek"),
    "dsh-deepseek-v4-flash-vision-exp": ("DeepSeek V4 Vision · DSH", "deepseek"),
    "grok-4.6": ("Grok 4.6", "grok"),
    "k3": ("Kimi K3", "kimi"),
    "glm-5.3": ("GLM-5.3", "glm"),
    "glm-5.3-flash": ("GLM-5.3 Flash", "glm"),
    "gemini-3.7-flash": ("Gemini 3.7 Flash", "gemini"),
    "gemini-3.8-flash": ("Gemini 3.8 Flash", "gemini"),
    "hy4-preview": ("HY4 Preview", "hunyuan"),
    "claude-sonnet-5": ("Claude Sonnet 5", "claude"),
    "claude-opus-5": ("Claude Opus 5", "claude"),
    "claude-opus-5-5": ("Claude Opus 5.5", "claude"),
}

# Group by the model developer; a harness prefix such as DSH is not a vendor.
_VENDORS = {
    "openai": ("international", "OpenAI", "GPT"),
    "anthropic": ("international", "Anthropic", "Claude"),
    "google": ("international", "Google", "Gemini"),
    "xai": ("international", "xAI", "Grok"),
    "deepseek": ("domestic", "DeepSeek", "DeepSeek"),
    "zhipu": ("domestic", "智谱 · Z.ai", "GLM"),
    "moonshot": ("domestic", "月之暗面 · Moonshot AI", "Kimi"),
    "tencent": ("domestic", "腾讯 · Tencent", "混元"),
    "unknown": ("unclassified", "厂商待确认", ""),
}
_REGIONS = (
    ("international", "国外模型", "INTERNATIONAL"),
    ("domestic", "国内模型", "DOMESTIC"),
    ("unclassified", "待归类模型", "UNCLASSIFIED"),
)
_ICON_VENDORS = {
    **dict.fromkeys(("astra", "sol", "terra", "luna", "system"), "openai"),
    "claude": "anthropic",
    "gemini": "google",
    "grok": "xai",
    "deepseek": "deepseek",
    "glm": "zhipu",
    "kimi": "moonshot",
    "hunyuan": "tencent",
}
_VENDOR_PREFIXES = (
    ("gpt-", "openai"),
    ("claude-", "anthropic"),
    ("gemini-", "google"),
    ("grok-", "xai"),
    ("deepseek-", "deepseek"),
    ("glm-", "zhipu"),
    ("kimi-", "moonshot"),
    ("hunyuan-", "tencent"),
)


@dataclass(frozen=True, slots=True)
class _VendorGroup:
    key: str
    region: str
    label: str
    series: str
    models: tuple[MatrixModel, ...]


def _group_models(models: tuple[MatrixModel, ...]) -> tuple[_VendorGroup, ...]:
    buckets: dict[str, list[MatrixModel]] = {}
    for model in models:
        model_id = model.model.lower()
        known = _NAMES.get(model_id)
        vendor = _ICON_VENDORS.get(known[1]) if known else None
        if vendor is None:
            base_id = model_id.removeprefix("dsh-")
            vendor = next(
                (
                    vendor
                    for prefix, vendor in _VENDOR_PREFIXES
                    if base_id.startswith(prefix)
                ),
                "unknown",
            )
        buckets.setdefault(vendor, []).append(model)
    return tuple(
        _VendorGroup(key, region, label, series, tuple(buckets[key]))
        for key, (region, label, series) in _VENDORS.items()
        if key in buckets
    )


def short_stamp(value: str | None) -> str:
    value = stamp(value)
    return value[5:] if len(value) > 5 and value[4] == "-" else value


def _valid_iq(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and 0 <= value <= 150


def iq_color(value: float | None) -> str:
    """Continuous red → amber → green, using the original unrounded IQ."""
    if not _valid_iq(value):
        return IQ_MISSING_COLOR
    assert value is not None
    for (low, start), (high, end) in pairwise(IQ_COLOR_STOPS):
        if value <= high:
            fraction = (value - low) / (high - low)
            channels = (
                round(
                    int(start[i : i + 2], 16) * (1 - fraction)
                    + int(end[i : i + 2], 16) * fraction
                )
                for i in (1, 3, 5)
            )
            return "#" + "".join(f"{channel:02x}" for channel in channels)
    return IQ_COLOR_STOPS[-1][1]


def _iq_attributes(value: float | None) -> str:
    score = str(value) if _valid_iq(value) else ""
    return f'data-iq="{score}" style="--iq-color:{iq_color(value)}"'


def _iq_legend() -> str:
    gradient = ",".join(
        f"{color} {value / 150 * 100:g}%" for value, color in IQ_COLOR_STOPS
    )
    ticks = "".join(
        f'<span style="left:{value / 150 * 100:g}%">{value:g}</span>'
        for value, _ in IQ_COLOR_STOPS
    )
    return (
        '<div class="matrix-score-key"><div class="key-title"><span>IQ 颜色</span><b>低 → 高 · 0–150 连续取色 · 模型与档位共用</b></div>'
        '<div class="iq-color-scale" role="img" aria-label="IQ 0 至 150，每 15 分一个色阶，共 11 阶；低分红色，高分绿色，阶间连续渐变">'
        f'<div class="iq-color-ramp" style="background:linear-gradient(90deg,{gradient})"></div>'
        f'<div class="iq-color-ticks">{ticks}</div></div></div>'
    )


def _history_points(
    model: MatrixModel,
) -> tuple[tuple[float, TrendPoint] | None, ...]:
    points = []
    for point in model.history:
        try:
            date = datetime.fromisoformat(point.timestamp.replace("Z", "+00:00"))
            ts = date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp()
        except ValueError:
            points.append(None)
            continue
        points.append((ts, point) if _valid_iq(point.iq) else None)
    return tuple(points)


def _sample_warning(samples: float | None) -> str | None:
    if samples is None or not math.isfinite(samples) or samples < 0:
        return "样本量缺失"
    if samples == 0:
        return "暂无有效样本"
    if samples < MATRIX_SAMPLE_REMINDER:
        return "样本偏少"
    return None


def _tier_warnings(point: EfficiencyPoint) -> tuple[str, ...]:
    warnings = []
    if warning := _sample_warning(point.total):
        warnings.append(warning)
    if not _valid_iq(point.iq) and point.total != 0:
        warnings.append("IQ 缺失")
    if (point.passed is None or not math.isfinite(point.passed)) and point.total != 0:
        warnings.append("评测得分缺失")
    return tuple(warnings)


def _model_warnings(
    model: MatrixModel, points: tuple[tuple[float, TrendPoint] | None, ...]
) -> tuple[str, ...]:
    warnings = []
    valid = [point for point in points if point is not None]
    if not points:
        warnings.append("暂无跨档位历史")
    elif not valid:
        warnings.append("历史记录无效，暂不能绘制")
    else:
        if len(valid) == 1:
            warnings.append("仅 1 个有效历史点，暂不能判断趋势")
        elif len({point[0] for point in valid}) == 1:
            warnings.append("历史仅覆盖 1 个时刻，暂不能判断趋势")
        if points[-1] is None:
            warnings.append("最新历史点无效，模型 IQ 暂缺")
        elif warning := _sample_warning(points[-1][1].samples):
            warnings.append(
                f"历史末点{warning}（n={number(points[-1][1].samples, 0)}）"
            )
        if len(valid) < len(points):
            warnings.append(f"{len(points) - len(valid)} 条无效历史记录已略过")
    affected = sum(bool(_tier_warnings(point)) for point in model.tiers)
    if affected:
        warnings.append(f"{affected} 个档位数据不足，见下方标记")
    return tuple(warnings)


def _time_ticks(start: float, end: float) -> tuple[tuple[float, str], ...]:
    """UTC calendar/clock ticks, independent of the sampling cadence."""
    span = end - start
    if span <= 0:
        return (
            (
                start,
                datetime.fromtimestamp(start, timezone.utc).strftime("%m-%d %H:%M"),
            ),
        )
    steps = (
        1,
        5,
        15,
        30,
        60,
        300,
        900,
        1800,
        3600,
        10800,
        21600,
        43200,
        86400,
        172800,
        259200,
        604800,
        1209600,
        2592000,
        31536000,
    )
    step = min(steps, key=lambda value: abs(value - span / 3))
    first = math.ceil(start / step) * step
    format_ = "%m-%d" if span >= 86400 else "%H:%M" if span >= 60 else "%H:%M:%S"
    return tuple(
        (ts, datetime.fromtimestamp(ts, timezone.utc).strftime(format_))
        for ts in range(first, math.floor(end) + 1, step)
    )


def _sparkline(points: tuple[tuple[float, TrendPoint] | None, ...]) -> str:
    valid = [point for point in points if point is not None]
    if not valid:
        return '<div class="spark-empty"><svg class="empty-chart-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3 2"/></svg><div><b>暂无可绘制历史</b><span>收到有效历史记录后显示趋势</span></div></div>'
    start, end = min(point[0] for point in valid), max(point[0] for point in valid)
    scale = trend_scale(point[1].iq for point in valid)
    low, high = scale
    span = high - low or 1.0
    y_labels, grid = [], []
    for tick in trend_axis_ticks(scale):
        top = (high - tick) / span * 100
        y_labels.append(
            f'<span class="spark-y-tick" style="top:{top:.4f}%">{trend_value(tick)}</span>'
        )
        grid.append(f'<span class="spark-grid" style="top:{top:.4f}%"></span>')
    x_labels = []
    for ts, label in _time_ticks(start, end):
        x = (ts - start) / (end - start) * 100 if end > start else 50
        anchor = "start" if x < 10 else "end" if x > 90 else "middle"
        x_labels.append(
            f'<span class="spark-x-tick" data-anchor="{anchor}" style="left:{x:.4f}%" title="{text(stamp(datetime.fromtimestamp(ts, timezone.utc).isoformat()))}">{label}</span>'
        )
    paths, path, markers = [], [], []
    connected = False
    for index, item in enumerate(points):
        if item is None:
            if path:
                paths.append(" ".join(path))
                path = []
            connected = False
            continue
        ts, point = item
        x = (ts - start) / (end - start) * 1000 if end > start else 500
        y = (high - point.iq) / span * 150
        path.append(f"{'L' if connected else 'M'}{x:.2f},{y:.2f}")
        connected = True
        isolated = (index == 0 or points[index - 1] is None) and (
            index == len(points) - 1 or points[index + 1] is None
        )
        markers.append(
            f'<span class="spark-point" data-isolated="{str(isolated).lower()}" style="left:{x / 10:.4f}%;top:{(high - point.iq) / span * 100:.4f}%" title="{text(stamp(point.timestamp))} · IQ {number(point.iq)} · n={point.samples}"></span>'
        )
    if path:
        paths.append(" ".join(path))
    if points[-1] is not None:
        markers.append(
            f'<span class="spark-last" style="left:{x / 10:.4f}%;top:{(high - points[-1][1].iq) / span * 100:.4f}%" title="最新历史点 · IQ {number(points[-1][1].iq)}"></span>'
        )
    range_start = short_stamp(
        datetime.fromtimestamp(start, timezone.utc).isoformat()
    ).removesuffix(" UTC")
    range_end = short_stamp(
        datetime.fromtimestamp(end, timezone.utc).isoformat()
    ).removesuffix(" UTC")
    range_label = range_start if start == end else f"{range_start} — {range_end}"
    axis_label = f"跨档位历史 IQ；纵轴 {trend_value(low)}、{trend_value(high)}；横轴为实际 UTC 时间 {range_label}"
    return (
        f'<div class="history-chart" role="img" aria-label="{text(axis_label)}">'
        + '<div class="chart-y-axis">'
        + "".join(y_labels)
        + "</div>"
        + '<div class="chart-plot">'
        + "".join(grid)
        + '<svg class="spark" viewBox="0 0 1000 150" preserveAspectRatio="none" aria-hidden="true">'
        + "".join(f'<path class="spark-path" d="{segment}"/>' for segment in paths)
        + "</svg>"
        + "".join(markers)
        + '</div><div class="chart-x-axis">'
        + "".join(x_labels)
        + "</div></div>"
        + f'<div class="chart-range">纵轴 {trend_value(low)}–{trend_value(high)} · {range_label} UTC</div>'
    )


def _tile(point: EfficiencyPoint, continuous: bool) -> str:
    warnings = _tier_warnings(point)
    warning = (
        f'<div class="tier-warning"><span class="warning-icon" aria-hidden="true">＊</span><span>{text(" · ".join(warnings))}</span></div>'
        if warnings
        else ""
    )
    iq = point.iq if _valid_iq(point.iq) else None
    rate = (
        point.passed / point.total if point.passed is not None and point.total else None
    )
    result = f"F1 {number(rate, 3)}" if continuous else f"通过 {percent(rate)}"
    price = (
        f"~${number(point.average_price_usd, 2)}"
        if point.average_price_usd is not None
        else "—"
    )
    minutes = (
        f"~{number(point.average_minutes)}"
        if point.average_minutes is not None
        else "—"
    )
    return f'''<section class="tier" data-effort="{text(point.effort)}" data-evidence="{"limited" if warnings else "available"}" title="档位数据 {text(stamp(point.source_updated_at))}">
<div class="tier-performance"><div class="tier-heading"><strong class="tier-name">{text(point.effort)}</strong>
<div class="tier-score"><b {_iq_attributes(iq)} title="IQ {number(iq)}">{number(iq, 0)}</b><span>IQ</span></div></div>
<div class="tier-evidence"><span>{text(result)}</span><span>n={number(point.total, 0)}</span></div></div>
<div class="tier-input"><div class="tier-duration"><b>{minutes}</b><span>分钟</span></div>
<div class="tier-price"><b>{price}</b><span>/ 次</span></div></div>{warning}</section>'''


def _panel(model: MatrixModel, *, continuous: bool) -> str:
    name, icon = _NAMES.get(model.model, (model.model, "generic"))
    points = _history_points(model)
    last = points[-1][1] if points and points[-1] is not None else None
    warnings = _model_warnings(model, points)
    warning = (
        f'<div class="model-warning" role="note"><span class="warning-icon" aria-hidden="true">＊</span><div><b>数据不足</b><p>{text("；".join(warnings))}</p></div></div>'
        if warnings
        else ""
    )
    score = number(last.iq, 0) if last else "—"
    evidence = (
        f"<span>跨档位 IQ · n={number(last.samples, 0)}</span>"
        if last
        else "<span>暂无跨档位数据</span>"
    )
    if model.history_stale:
        evidence += '<span class="matrix-stale">历史缓存可能过期</span>'
    history_time = (
        f" · 截至 {text(short_stamp(last.timestamp))}" if last else " · UTC"
    )
    tiles = "".join(_tile(point, continuous) for point in model.tiers)
    times = [
        stamp(point.source_updated_at)
        for point in model.tiers
        if point.source_updated_at
    ]
    time_label = "—" if not times else short_stamp(min(times))
    if times and min(times) != max(times):
        time_label = time_label.removesuffix(" UTC") + " — " + short_stamp(max(times))
    return f"""<section class="model-panel family-{icon}" data-model="{text(model.model)}">
<header class="model-heading"><span class="model-artwork"><img class="model-icon" data-model-icon="{icon}" alt="{text(name)} 图标"></span>
<div class="model-title"><h4>{text(name)}</h4><p>{text(model.model)} · {len(model.tiers)} 个档位</p></div>
<div class="family-score"><strong {_iq_attributes(last.iq if last else None)} title="IQ {number(last.iq if last else None)}">{score}<small>IQ</small></strong><div class="family-evidence">{evidence}</div></div></header>
<div class="model-history"><div class="history-label"><span>近期 IQ 走势</span><b>{sum(point is not None for point in points)} 个点{history_time}</b></div>{_sparkline(points)}</div>
{warning}
<div class="tier-section-heading"><span>推理档位</span><span>IQ · 得分 · 耗时 · 费用</span></div>
<div class="tier-grid">{tiles}</div>
<div class="panel-data-time">档位数据更新 {text(time_label)}</div>
</section>"""


def _region_body(
    region: str,
    label: str,
    english: str,
    index: int,
    vendors: tuple[_VendorGroup, ...],
    *,
    continuous: bool,
) -> str:
    model_count = sum(len(vendor.models) for vendor in vendors)
    tier_count = sum(
        len(model.tiers) for vendor in vendors for model in vendor.models
    )
    body = f'<section class="region-section" data-region="{region}"><header class="region-heading"><h2><span class="region-index">{index:02}</span>{label}<small>{english}</small></h2><p>{len(vendors)} 家厂商 · {model_count} 模型 · {tier_count} 档位</p></header>'
    for vendor in vendors:
        size = len(vendor.models)
        body += f'<section class="vendor-group" data-vendor="{vendor.key}"><header class="vendor-heading"><h3>{text(vendor.label)}<small class="vendor-series">{text(vendor.series)}</small></h3><span>{size} 模型 · {sum(len(model.tiers) for model in vendor.models)} 档位</span></header><div class="model-grid">'
        body += "".join(_panel(model, continuous=continuous) for model in vendor.models)
        body += "</div></section>"
    return body + "</section>"


def _toolbar(meta: RadarMeta, models: int, tiers: int, scope: str) -> str:
    return (
        f'<div class="matrix-toolbar"><span class="benchmark-pill">{text(meta.benchmark_id)}</span>'
        f'<span class="matrix-count">{scope} · {models} 模型 · {tiers} 档位</span>'
        '<span class="matrix-scope">按地区 → 研发厂商分组</span></div>'
    )


def _pack(
    vendors: tuple[_VendorGroup, ...], budget: int
) -> list[tuple[_VendorGroup, ...]]:
    pages: list[tuple[_VendorGroup, ...]] = []
    current: list[_VendorGroup] = []
    size = 0
    for vendor in vendors:
        count = len(vendor.models)
        if current and size + count > budget:
            pages.append(tuple(current))
            current, size = [], 0
        current.append(vendor)
        size += count
    if current:
        pages.append(tuple(current))
    return pages


def _split_vendors(
    vendors: tuple[_VendorGroup, ...], budget: int
) -> tuple[tuple[_VendorGroup, ...], ...]:
    """Pack vendors into as few pages as ``budget`` allows, then even them out.

    A vendor group is never broken across pages, so a single oversized vendor
    keeps its own page rather than being cut mid-group. Balancing keeps the page
    count and avoids a near-empty last page (12 + 2 becomes 7 + 7).
    """
    pages = _pack(vendors, budget)
    total = sum(len(vendor.models) for vendor in vendors)
    for target in range(math.ceil(total / max(1, len(pages))), budget):
        balanced = _pack(vendors, target)
        if len(balanced) <= len(pages):
            return tuple(balanced)
    return tuple(pages)


def matrix_pages(snapshot: RadarMatrix) -> tuple[RadarPage, ...]:
    count = len(snapshot.models)
    tiers = sum(len(model.tiers) for model in snapshot.models)
    continuous = snapshot.meta.scoring_mode == SCORING_CONTINUOUS
    groups = _group_models(snapshot.models)
    why = "先按国内外与研发厂商定位模型，再用总分与趋势看整体变化；档位格给出 IQ、评测得分、样本、耗时和费用，方便比较推理投入。"
    if not snapshot.models:
        body = _toolbar(snapshot.meta, count, tiers, "全量总览")
        body += _iq_legend()
        body += '<div class="matrix-empty">该频道暂无模型档位数据</div>'
        return (
            RadarPage(
                title="模型档位总览",
                subtitle="每个模型的整体 IQ、走势，以及每个推理档位的得分、耗时与费用。",
                body=body,
                why=why,
                section="总览",
                meta=snapshot.meta,
                layout="matrix",
            ),
        )
    regions = [
        (index, region, label, english, tuple(g for g in groups if g.region == region))
        for index, (region, label, english) in enumerate(_REGIONS, 1)
    ]
    regions = [item for item in regions if item[4]]
    if count <= MATRIX_PAGE_MAX_MODELS:
        # Fits one screen-friendly image: keep the single-page overview.
        body = _toolbar(snapshot.meta, count, tiers, "全量总览") + _iq_legend()
        for index, region, label, english, vendors in regions:
            body += _region_body(
                region, label, english, index, vendors, continuous=continuous
            )
        return (
            RadarPage(
                title="模型档位总览",
                subtitle="每个模型的整体 IQ、走势，以及每个推理档位的得分、耗时与费用。",
                body=body,
                why=why,
                section="总览",
                meta=snapshot.meta,
                layout="matrix",
            ),
        )
    # Too long for one image: split by region (国内 / 国外), and split a region
    # further by vendor when it alone exceeds the budget.
    planned: list[tuple[int, str, str, str, tuple[_VendorGroup, ...], int, int]] = []
    for index, region, label, english, vendors in regions:
        chunks = _split_vendors(vendors, MATRIX_PAGE_MAX_MODELS)
        for part, chunk in enumerate(chunks, 1):
            planned.append((index, region, label, english, chunk, part, len(chunks)))
    pages = []
    for position, (index, region, label, english, vendors, part, parts) in enumerate(
        planned, 1
    ):
        page_models = sum(len(vendor.models) for vendor in vendors)
        page_tiers = sum(
            len(model.tiers) for vendor in vendors for model in vendor.models
        )
        name = label if parts == 1 else f"{label}（{part}/{parts}）"
        scope = f"本页 {position:02}/{len(planned):02}"
        body = _toolbar(snapshot.meta, page_models, page_tiers, scope) + _iq_legend()
        body += _region_body(
            region, name, english, index, vendors, continuous=continuous
        )
        pages.append(
            RadarPage(
                title="模型档位总览",
                subtitle=f"{name} · 每个模型的整体 IQ、走势，以及各推理档位的得分与投入。",
                body=body,
                why="先按研发厂商定位模型，再用总分与趋势看整体变化；档位格给出 IQ、评测得分、样本、耗时和费用，方便比较推理投入。总览过长时按国内外分页，本页为其中一页。",
                section=label,
                meta=snapshot.meta,
                number=position,
                total=len(planned),
                layout="matrix",
            )
        )
    return tuple(pages)


def matrix_text(snapshot: RadarMatrix) -> list[str]:
    """Compact text fallback with the same identities and provenance."""
    lines = [f"AI 智商雷达 · 模型档位总览 · {snapshot.meta.benchmark_id}"]
    if snapshot.meta.stale:
        lines.append("数据可能过期 · 效率数据为陈旧缓存")
    previous_region = None
    region_labels = {key: label for key, label, _ in _REGIONS}
    for group in _group_models(snapshot.models):
        if group.region != previous_region:
            lines.append(f"【{region_labels[group.region]}】")
            previous_region = group.region
        lines.append(f"〈{group.label}〉")
        for model in group.models:
            lines.extend(_model_text(model, snapshot))
    lines.append(
        f"档位分数来自本频道效率口径 {snapshot.meta.mode or '—'}，~ 为估算；费用为 API 等价成本。"
    )
    lines.append(
        f"样本偏少：0<n<{MATRIX_SAMPLE_REMINDER} 为前端提醒线，非上游统计判定；不提示不代表结果稳定。"
    )
    return lines


def _model_text(model: MatrixModel, snapshot: RadarMatrix) -> list[str]:
    lines = []
    points = _history_points(model)
    last = points[-1][1] if points and points[-1] is not None else None
    lines.append(
        f"{model.model} · 跨档位 IQ {number(last.iq if last else None)} · n={number(last.samples if last else None, 0)} · {stamp(last.timestamp if last else None)}"
    )
    if model.history_stale:
        lines.append("历史数据可能过期")
    if warnings := _model_warnings(model, points):
        lines.append("数据不足：" + "；".join(warnings))
    for point in model.tiers:
        rate = (
            point.passed / point.total
            if point.passed is not None and point.total
            else None
        )
        result = (
            f"F1 {number(rate, 3)}"
            if snapshot.meta.scoring_mode == SCORING_CONTINUOUS
            else f"通过 {percent(rate)}"
        )
        price = (
            f"~${number(point.average_price_usd, 2)}"
            if point.average_price_usd is not None
            else "—"
        )
        duration = (
            f"~{number(point.average_minutes)} 分"
            if point.average_minutes is not None
            else "—"
        )
        lines.append(
            f"  [{point.effort}] IQ {number(point.iq if _valid_iq(point.iq) else None)} · {result} · n={number(point.total, 0)} · {duration} · {price} · {stamp(point.source_updated_at)}"
        )
        if warnings := _tier_warnings(point):
            lines.append("    数据不足：" + " · ".join(warnings))
    return lines
