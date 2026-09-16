"""Pure, escaped HTML views over RadarService results; no HTTP or file I/O.

Each page keeps its own metadata and explanation. Charts only encode comparable
quantities. The existing text formatters remain the delivery fallback.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape

from .models import (
    SCORING_CONTINUOUS,
    BenchmarkInfo,
    Comparison,
    DegradationAlert,
    EfficiencyPoint,
    ModelConfig,
    ModelProfile,
    ModelRow,
    RadarMeta,
    Recommendation,
    TrendPoint,
)


@dataclass(frozen=True)
class RadarPage:
    title: str
    subtitle: str
    body: str
    why: str
    section: str
    meta: RadarMeta | None = None
    number: int = 1
    total: int = 1
    layout: str = "report"


class RadarReply(list[str]):
    """Text-compatible reply carrying optional, already prepared card views."""

    def __init__(self, lines: list[str], pages: Sequence[RadarPage]):
        super().__init__(lines)
        self.pages = tuple(pages)


def text(value: object) -> str:
    return escape(str(value)) if value is not None else "—"


def number(value: float | None, digits: int = 1) -> str:
    if value is None or not math.isfinite(value):
        return "—"
    return f"{value:,.{digits}f}"


def percent(value: float | None) -> str:
    return (
        number(value * 100) + "%" if value is not None and math.isfinite(value) else "—"
    )


def money(value: float | None) -> str:
    return (
        f"~${number(value, 2)}" if value is not None and math.isfinite(value) else "—"
    )


def minutes(value: float | None) -> str:
    return f"~{number(value)} 分" if value is not None and math.isfinite(value) else "—"


def stamp(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (
            dt.replace(tzinfo=dt.tzinfo or timezone.utc)
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%d %H:%M UTC")
        )
    except ValueError:
        return str(value)


def identity(model: str, effort: str | None) -> str:
    return f'<span class="model">{text(model)}</span><span class="effort">{text(effort or "跨档位合并")}</span>'


def metric(label: str, value: str, hint: str = "") -> str:
    return f'<div class="metric"><span>{text(label)}</span><strong>{text(value)}</strong><small>{text(hint)}</small></div>'


def notice(message: str, *, warning: bool = False) -> str:
    return f'<div class="notice{" warning" if warning else ""}">{text(message)}</div>'


def empty(
    message: str = "暂无数据", detail: str = "当前查询没有可展示的实测记录。"
) -> str:
    return f'<div class="empty"><span class="empty-mark">＋</span><h2>{text(message)}</h2><p>{text(detail)}</p></div>'


def score_label(meta: RadarMeta | None) -> str:
    return "Macro-F1" if meta and meta.scoring_mode == SCORING_CONTINUOUS else "通过率"


def score(value: float | None, meta: RadarMeta | None) -> str:
    return (
        number(value, 3)
        if meta and meta.scoring_mode == SCORING_CONTINUOUS
        else percent(value)
    )


def iq_label(row: ModelRow) -> str:
    return "频道换算 IQ" if row.iq_derived else "综合 IQ"


def model_table(
    rows: Sequence[ModelRow],
    meta: RadarMeta | None,
    *,
    offset: int = 0,
    ordered: bool = False,
) -> str:
    if not rows:
        return empty()
    body = []
    for index, row in enumerate(rows, offset + 1):
        width = (
            max(0, min(100, row.pass_rate * 100)) if math.isfinite(row.pass_rate) else 0
        )
        body.append(
            f'<tr><td class="rank">{index:02}</td><td><div class="identity">{identity(row.model, row.effort)}</div></td>'
            f'<td class="score"><strong>{text(score(row.pass_rate, meta))}</strong>'
            f'<div class="bar"><i style="width:{width:.2f}%"></i></div></td>'
            f"<td><b>{number(row.iq)}</b><small>{iq_label(row)}</small></td>"
            f"<td><b>{row.graded:,}</b><small>{row.cells:,} 道覆盖题目</small></td></tr>"
        )
    return (
        '<table class="ranking"><colgroup><col class="rank-col"><col class="model-col">'
        '<col class="score-col"><col class="iq-col"><col class="sample-col"></colgroup>'
        f"<thead><tr><th>#</th><th>模型 / 推理档位</th><th>{score_label(meta)}{' ↓' if ordered else ''}</th><th>IQ · 口径</th>"
        "<th>有效运行样本</th></tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


def ranking_pages(rows: Sequence[ModelRow], meta: RadarMeta) -> tuple[RadarPage, ...]:
    # The service is called with by=pass_rate. Keep its selection and ordering.
    pages = []
    total = max(1, math.ceil(len(rows) / 8))
    for page in range(total):
        chunk = rows[page * 8 : (page + 1) * 8]
        hero = ""
        if chunk:
            first = chunk[0]
            hero = (
                '<section class="hero"><div><span class="eyebrow">BENCHMARK LEADERBOARD</span>'
                f"<h2>{'本次查询首位' if page == 0 else '继续查看榜单'}</h2>"
                f'<div class="identity">{identity(first.model, first.effort)}</div></div>'
                f'<div class="hero-score"><strong>{text(score(first.pass_rate, meta))}</strong>'
                f"<span>{score_label(meta)} · 本频道</span></div></section>"
            )
        body = hero + model_table(chunk, meta, offset=page * 8, ordered=True)
        body += notice(
            "按本频道评测得分排序；综合 IQ 为上游加权值，频道换算 IQ = 本频道得分 × 150，两者分别标注。"
        )
        pages.append(
            RadarPage(
                "模型实力榜",
                "同一个频道，看清每一档的实测表现。",
                body,
                "评测得分用于同频道比较；IQ 提供分数参考；档位说明推理投入；样本数与覆盖题目帮助判断证据规模。最高档不等于性价比最高。",
                "榜单",
                meta,
                page + 1,
                total,
            )
        )
    return tuple(pages)


def trend_chart(points: Sequence[TrendPoint]) -> str:
    """Use actual UTC timestamps on X and a fixed 0–150 IQ scale on Y.

    Invalid points break the path, rather than interpolating missing evidence.
    Singletons are visible. SVG titles expose each point's timestamp and n.
    """
    valid: list[tuple[float, TrendPoint] | None] = []
    for point in points:
        try:
            dt = datetime.fromisoformat(point.timestamp.replace("Z", "+00:00"))
            ts = dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
            valid.append(
                (ts, point)
                if math.isfinite(point.iq) and 0 <= point.iq <= 150
                else None
            )
        except (ValueError, TypeError):
            valid.append(None)
    samples = [item for item in valid if item is not None]
    if not samples:
        return empty("暂无趋势数据", "该模型与档位没有匹配的历史序列。")
    start, end = min(item[0] for item in samples), max(item[0] for item in samples)
    svg = [
        '<svg class="chart" viewBox="0 0 920 294" role="img" aria-label="IQ 历史趋势，纵轴 0 至 150，横轴为 UTC 时间">'
    ]
    for iq in (0, 50, 100, 150):
        y = 236 - iq / 150 * 212
        svg.append(
            f'<line class="gridline" x1="52" y1="{y}" x2="884" y2="{y}"/><text x="36" y="{y + 5}" text-anchor="end">{iq}</text>'
        )
    path = []
    dots = []
    connected = False
    for item in valid:
        if item is None:
            connected = False
            continue
        ts, point = item
        x = 52 + (ts - start) / (end - start) * 832 if end > start else 468
        y = 236 - point.iq / 150 * 212
        path.append(f"{'L' if connected else 'M'}{x:.2f},{y:.2f}")
        connected = True
        dots.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3.5"><title>{text(stamp(point.timestamp))} · IQ {number(point.iq)} · n={point.samples}</title></circle>'
        )
    svg.append(f'<path class="trend-line" d="{" ".join(path)}"/>')
    svg.extend(dots)
    for x, ts, anchor in ((52, start, "start"), (884, end, "end")):
        label = datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d %H:%M UTC")
        svg.append(f'<text x="{x}" y="276" text-anchor="{anchor}">{label}</text>')
    svg.append("</svg>")
    return "".join(svg)


def trend_pages(
    points: Sequence[TrendPoint], label: str, meta: RadarMeta
) -> tuple[RadarPage, ...]:
    body = f'<div class="section-heading"><h2>{text(label)}</h2><span>历史 IQ · 0–150</span></div>'
    if points:
        last = points[-1]
        body += '<div class="metrics">' + metric(
            "最新历史 IQ", number(last.iq), "本序列末点"
        )
        body += metric("末点样本", number(last.samples, 0), "每个时刻样本量可能不同")
        body += (
            metric("数据点", number(len(points), 0), "按真实时间间隔绘制") + "</div>"
        )
    body += trend_chart(points)
    if points:
        body += '<div class="endpoints">'
        for label_text, point in (("起点", points[0]), ("末点", points[-1])):
            body += f"<span>{label_text} {text(stamp(point.timestamp))} · IQ {number(point.iq)} · n={point.samples}</span>"
        body += "</div>"
    body += notice(
        "历史 IQ 与综合 IQ 分属不同口径；历史接口未提供同期评测得分，不由 IQ 反推。曲线波动不自动等于降智预警。"
    )
    return (
        RadarPage(
            "IQ 趋势",
            meta.note or "历史序列",
            body,
            "时间曲线用于观察同一模型口径的变化；固定纵轴避免放大轻微波动；起止时间和样本数用于识别旧数据与样本变化。",
            "趋势",
            meta,
        ),
    )


def efficiency_panel(point: EfficiencyPoint, meta: RadarMeta | None) -> str:
    # The endpoint supplies aggregate averages, without measured provenance.
    # Keep these conservative labels until the model carries evidence otherwise.
    body = f'<div class="section-heading"><h2>成本与等待时间</h2><div class="identity">{identity(point.model, point.effort)}</div></div>'
    body += '<div class="metrics">' + metric(
        "平均 API 等价成本", money(point.average_price_usd), "估算 · 每次任务"
    )
    body += metric("平均耗时", minutes(point.average_minutes), "估算 · 分钟")
    body += (
        metric(
            "综合成本指数 ↓",
            number(point.combined_cost_index),
            "原样引用上游，越低越划算",
        )
        + "</div>"
    )
    rate = (
        point.passed / point.total if point.passed is not None and point.total else None
    )
    body += f'<p class="data-note">{score_label(meta)} {text(score(rate, meta))} · 样本 n={number(point.total, 0)} · 累计运行 {point.runs_total:,} · 数据 {text(stamp(point.source_updated_at))}</p>'
    return body


def profile_pages(profile: ModelProfile) -> tuple[RadarPage, ...]:
    pages = []
    meta = profile.meta
    chunks = [
        profile.variants[i : i + 6] for i in range(0, len(profile.variants), 6)
    ] or [()]
    for i, chunk in enumerate(chunks):
        body = f'<div class="section-heading"><h2>{text(profile.model)}</h2><span>档位明细</span></div>'
        body += model_table(chunk, meta, offset=i * 6)
        if i == 0 and profile.insight:
            p = profile.insight
            body += f'<div class="section-heading"><h2>综合视角</h2><div class="identity">{identity(p.model, p.effort)}</div></div>'
            body += '<div class="metrics">' + metric(
                "综合 IQ", number(p.iq), f"上游加权 · n={p.samples}"
            )
            body += (
                metric("软件 IQ", number(p.software_iq), "软件频道")
                + metric("视觉 IQ", number(p.visual_iq), "视觉频道")
                + "</div>"
            )
            body += notice("两个频道难度不同，软件与视觉分数不能用来判断哪项能力更强。")
        if i == 0 and profile.efficiency:
            body += efficiency_panel(profile.efficiency, meta)
        body += notice(
            "综合 IQ 与频道换算 IQ 分别标注；辅助数据标题中的档位，是该组数据实际对应的档位。"
        )
        pages.append(
            RadarPage(
                "模型档案",
                "把模型名拆成可比较的具体档位。",
                body,
                "各档得分展示增加推理投入后的表现；综合三项分数保留上游构成；费用与耗时帮助判断投入。不同档位的数据各自注明身份。",
                "模型",
                meta,
                i + 1,
                len(chunks),
            )
        )
    return tuple(pages)


def comparison_pages(cmp: Comparison) -> tuple[RadarPage, ...]:
    profiles = (cmp.left, cmp.right)
    body = '<div class="comparison">'
    for side, profile in zip(("A", "B"), profiles):
        row = profile.best
        eff = profile.efficiency
        if eff and (not row or eff.key != row.key):
            eff = None
        body += f'<section class="compare-panel"><span class="eyebrow">MODEL {side}</span><div class="identity">{identity(profile.model, profile.effort)}</div>'
        body += metric(
            iq_label(row) if row else "IQ",
            number(row.iq if row else None),
            "保留各自的 IQ 来源",
        )
        body += metric(
            score_label(profile.meta),
            score(row.pass_rate if row else None, profile.meta),
            f"有效样本 n={row.graded if row else '—'}",
        )
        body += metric(
            "平均 API 等价成本",
            money(eff.average_price_usd if eff else None),
            "估算 · 仅展示相同档位",
        )
        body += metric(
            "平均耗时", minutes(eff.average_minutes if eff else None), "估算 · 分钟"
        )
        body += f'<p class="data-note">榜单数据 {text(stamp(profile.meta.source_updated_at if profile.meta else None))}<br>效率数据 {text(stamp(eff.source_updated_at if eff else None))}</p>'
        if profile.meta and profile.meta.stale:
            body += notice("数据可能过期 · 陈旧缓存", warning=True)
        body += "</section>"
    body += "</div>"
    left, right = cmp.left.best, cmp.right.best
    same_benchmark = (
        cmp.left.meta
        and cmp.right.meta
        and cmp.left.meta.benchmark_id == cmp.right.meta.benchmark_id
        and cmp.left.meta.scoring_mode == cmp.right.meta.scoring_mode
    )
    if (
        same_benchmark
        and left
        and right
        and left.iq_derived == right.iq_derived
        and cmp.iq_delta is not None
    ):
        body += notice(
            f"同口径 IQ 差（A − B）：{cmp.iq_delta:+.1f}；正值表示 A 更高，不代表在所有任务上更优。"
        )
    else:
        body += notice("IQ 来源或频道不同，或缺少对照数据，暂不计算 IQ 差。")
    return (
        RadarPage(
            "模型对比",
            "A vs B · 对齐档位，再权衡能力与投入。",
            body,
            "并排呈现得分、样本、成本与耗时，让选择取决于任务预算；只展示相同档位的辅助数据，缺失保留为 —。",
            "对比",
            cmp.meta,
        ),
    )


def recommendation_pages(
    groups: Sequence[Recommendation], meta: RadarMeta
) -> tuple[RadarPage, ...]:
    pages = []
    for group in groups:
        # Bound each card even if upstream increases the number of candidates.
        chunks = [group.items[i : i + 3] for i in range(0, len(group.items), 3)] or [()]
        for chunk in chunks:
            body = f'<div class="section-heading"><h2>{text(group.title or group.key)}</h2><span>上游推荐顺序</span></div>'
            for item in chunk:
                body += f'<section class="candidate"><div class="identity">{identity(item.model, item.effort)}</div><div class="metrics">'
                body += metric("推荐 IQ", number(item.iq), f"样本 n={item.samples}")
                body += metric(
                    "平均 API 等价成本", money(item.average_cost_usd), "估算"
                )
                body += (
                    metric("平均耗时", minutes(item.average_duration_minutes), "估算")
                    + "</div>"
                )
                weighted_label = (
                    "F1 加权和"
                    if meta.scoring_mode == SCORING_CONTINUOUS
                    else "加权通过量"
                )
                body += f'<p class="data-note">{weighted_label} {number(item.weighted_passed, 2)} · 非通过题数 · 综合成本指数 {number(item.combined_cost_index)}</p></section>'
            if not chunk:
                body += empty("该场景暂无推荐")
            body += notice(
                "推荐接口未提供本频道通过率 / F1，保留缺失，不由推荐 IQ 反推。"
            )
            body += f'<div class="rule"><span class="eyebrow">SELECTION RULE / 上游选取规则</span><p>{text(group.rule or "—")}</p></div>'
            pages.append(
                RadarPage(
                    "场景选型",
                    "从要做的事情出发，选择模型与档位。",
                    body,
                    "推荐场景缩小选择范围；IQ、费用、耗时和样本量解释取舍；保留上游规则与顺序，避免把推荐包装成新的全能榜。",
                    "推荐",
                    meta,
                )
            )
    if not pages:
        pages = [
            RadarPage(
                "场景选型",
                "按场景选择模型",
                empty("上游暂无推荐"),
                "推荐只引用上游已有的候选与选取规则。",
                "推荐",
                meta,
            )
        ]
    from dataclasses import replace

    return tuple(
        replace(page, number=i + 1, total=len(pages)) for i, page in enumerate(pages)
    )


def alert_pages(
    alerts: Sequence[DegradationAlert], meta: RadarMeta
) -> tuple[RadarPage, ...]:
    if not alerts:
        body = empty("当前无降智预警", "本次上游快照未返回预警条目。")
        body += notice(
            "这表示当前没有上游预警，不保证所有模型表现稳定；上游规则排除 DeepSeek。"
        )
        return (
            RadarPage(
                "波动观察",
                "只与自身历史比较，关注已有预警。",
                body,
                "预警用于发现值得复查的历史变化；空列表明确写出数据状态，避免将没有告警解读成稳定承诺。",
                "预警",
                meta,
            ),
        )
    pages = []
    for i, alert in enumerate(alerts):
        body = f'<section class="alert-head"><span class="status-dot"></span>上游预警<div class="identity">{identity(alert.model, alert.effort)}</div></section>'
        body += '<div class="metrics">' + metric(
            "当前 IQ", number(alert.current_iq), "上游预警口径"
        )
        body += metric(
            "24h 均值", number(alert.avg_24h), f"上游降幅 {number(alert.delta_24h)}"
        )
        body += (
            metric(
                "48h 均值", number(alert.avg_48h), f"上游降幅 {number(alert.delta_48h)}"
            )
            + "</div>"
        )
        body += trend_chart(alert.trend_48h)
        sample = alert.trend_48h[-1].samples if alert.trend_48h else None
        body += notice(
            f"趋势末点样本 n={number(sample, 0)}；上游未提供独立预警样本数或通过率，均不推算。"
        )
        pages.append(
            RadarPage(
                "波动观察",
                "上游降智预警 · 不在本地新增结论",
                body,
                "当前分数与 24h / 48h 基线展示预警依据；历史曲线与样本帮助复查。没有明确语义的高点与严重度字段不作为结论。",
                "预警",
                meta,
                i + 1,
                len(alerts),
            )
        )
    return tuple(pages)


def value_pages(
    points: Sequence[EfficiencyPoint], meta: RadarMeta
) -> tuple[RadarPage, ...]:
    pages = []
    total = max(1, math.ceil(len(points) / 3))
    for page in range(total):
        body = notice(
            "按上游综合成本指数升序排列 ↓；IQ、费用和耗时均来自同一个效率端点。"
        )
        for i, point in enumerate(points[page * 3 : (page + 1) * 3], page * 3 + 1):
            body += f'<section class="value-entry"><span class="value-rank">{i:02}</span><div><div class="value-iq">频道 IQ <b>{number(point.iq)}</b></div>'
            body += efficiency_panel(point, meta) + "</div></section>"
        if not points:
            body += empty("该频道暂无性价比数据")
        pages.append(
            RadarPage(
                "成本与效率",
                "聪明之外，还要看花费和等待。",
                body,
                "IQ 表示本频道效果，平均费用代表预算，平均耗时代表等待；保留上游成本指数用于排序，并显示样本与各条数据时间。",
                "性价比",
                meta,
                page + 1,
                total,
            )
        )
    return tuple(pages)


def benchmark_pages(items: Sequence[BenchmarkInfo]) -> tuple[RadarPage, ...]:
    pages = []
    total = max(1, math.ceil(len(items) / 4))
    for page in range(total):
        body = ""
        for item in items[page * 4 : (page + 1) * 4]:
            body += f'<section class="benchmark"><span class="eyebrow">{text(item.id)}</span><h2>{text(item.title)}</h2><p>{text(item.description)}</p><div class="metrics">'
            body += metric("评测题目", number(item.task_count, 0), "频道题库规模")
            body += metric("模型档位", number(item.model_config_count, 0), "已配置数量")
            body += (
                metric("滚动窗口", str(item.rolling_window), "最近有效运行") + "</div>"
            )
            body += f'<p class="data-note">{text(item.score_label)} · {text(item.scoring_mode)}</p><code>/radar 榜 @{text(item.id)}</code></section>'
        body = body or empty("暂无评测频道")
        pages.append(
            RadarPage(
                "评测频道",
                "先选要测的能力，再读对应的分数。",
                body,
                "评测内容决定分数含义；题库、档位数量交代覆盖范围；不同频道各自比较，不把不同难度的分数合成新榜。频道接口未提供更新时间。",
                "频道",
                number=page + 1,
                total=total,
            )
        )
    return tuple(pages)


def catalog_pages(
    combos: Sequence[ModelConfig], meta: RadarMeta
) -> tuple[RadarPage, ...]:
    grouped: dict[str, list[str]] = {}
    for combo in combos:
        grouped.setdefault(combo.model, []).append(combo.effort)
    rows = list(grouped.items())
    pages = []
    total = max(1, math.ceil(len(rows) / 9))
    for page in range(total):
        body = '<div class="catalog">'
        for model, efforts in rows[page * 9 : (page + 1) * 9]:
            body += f'<div class="catalog-row"><span class="model">{text(model)}</span><div>{"".join(f"<span class=effort>{text(effort)}</span>" for effort in efforts)}</div></div>'
        body += "</div>"
        if not rows:
            body += empty("该频道暂无模型档位")
        pages.append(
            RadarPage(
                "可用档位",
                "每个模型只展示上游实际配置的档位。",
                body,
                "模型与档位共同确定一次查询的身份。配置清单来自评测目录，不从价格表或其他模型推测支持范围。",
                "档位",
                meta,
                page + 1,
                total,
            )
        )
    return tuple(pages)


def help_pages() -> tuple[RadarPage, ...]:
    commands = (
        ("01", "看实力", "/radar 榜", "同频道得分、IQ、样本与档位"),
        ("02", "看模型", "/radar 模型 astra low", "明确档位的模型档案"),
        ("03", "做对比", "/radar 对比 astra sol max", "能力、成本和耗时并排看"),
        ("04", "选场景", "/radar 推荐", "按上游场景规则选择"),
        ("05", "查波动", "/radar 预警", "已有预警与历史基线"),
        ("06", "看成本", "/radar 性价比", "预算与等待时间的取舍"),
        ("07", "读趋势", "/radar 趋势 astra low", "同档位 IQ 的历史变化"),
        ("08", "选频道", "/radar 频道", "切换评测内容与计分方式"),
        ("09", "查档位", "/radar 档位", "已配置的模型与推理档位"),
    )
    body = '<section class="help-hero"><span class="eyebrow">A FIELD GUIDE TO MODEL INTELLIGENCE</span><h2>让每次选择，<br>都有数据可循。</h2><p>选模型 · 看波动 · 衡量成本</p><div class="scope" aria-hidden="true"><i></i><b></b></div></section><div class="command-list">'
    for index, title, command, hint in commands:
        body += f'<div><span class="rank">{index}</span><strong>{title}</strong><code>{command}</code><span>{hint}</span></div>'
    body += "</div>" + notice(
        "切换频道：在命令末尾添加 @频道，例如 /radar 榜 @pompeii-adjacency。模型与趋势命令建议显式填写档位。"
    )
    return (
        RadarPage(
            "AI 智商雷达",
            "模型观察手册 / FIELD GUIDE",
            body,
            "从要回答的问题选择命令。榜单用于比较，趋势用于观察，推荐用于缩小候选范围；每张数据卡保留来源和口径。",
            "帮助",
        ),
    )
