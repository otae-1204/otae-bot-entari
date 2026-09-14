"""AI 智商雷达的纯文本输出层。

**只出 ``list[str]``，不碰网络、不碰文件 IO、不渲染图片**（框架 §2.1）。群聊里等宽不成立，
所以不用 Markdown 表格，一律单行结构：

    1. gpt-6-astra[low] IQ 108.6 · 通过 67.6% · n=136 · $1.92

红线（数据字典 §16 的 13 条，落到代码里的位置都标了）：

- **IQ 与通过率必须一起给**：只给 IQ 会让人以为是绝对能力，只给通过率会丢掉跨频道口径。
- **必须带样本量**（``n=``）、**数据时间**（脚注）、**档位**（``[low]``）。
- **非 ``measured`` 的成本/耗时前缀 ``~`` 并标「估算」**。
- **缺失值统一 ``—``**，绝不用 0 代替（0 是「实测为零」，``—`` 才是「没有数据」）。
- **不跨频道混排**：每条输出的脚注都写明频道与口径。
- **空数据就说空**，不抛异常。
- 每行 ≤ :data:`MAX_LINE_WIDTH` 个显示宽度（中文按 2 计）。
"""

from __future__ import annotations

import unicodedata
from typing import Sequence

from .errors import RadarError
from .models import (
    BenchmarkInfo,
    Comparison,
    ContributorRow,
    DegradationAlert,
    EfficiencyPoint,
    FlagRace,
    FleetPulse,
    ModelConfig,
    ModelProfile,
    ModelRow,
    RadarEvent,
    RadarMeta,
    Recommendation,
    TaskDetail,
    TaskInfo,
    TrendPoint,
    is_estimate,
)

#: 单行最大显示宽度（中文按 2 计）。超长的自由文本由 :func:`_clip` 截断。
MAX_LINE_WIDTH = 40

#: 缺失值占位符。**不是 0** —— 0 是实测结果，``—`` 才是「无数据」。
EMPTY = "—"

#: **数据行**的宽度上限。数据行指必须同时携带「档位 + IQ + 通过率 + 样本量」四项红线
#: 的行（排行榜 / 模型档案 / 性价比 / 题目格子 …）。
#:
#: 为什么不是 :data:`MAX_LINE_WIDTH`：plugin_api §6 自己规定的单行结构
#: 「名次. 模型[档位] IQ 98.5 · 通过 68.2% · n=135 · $1.98」按该节的中文计 2 规则
#: 实测就是 **52 列**，而真实上游最长模型 id ``dsh-deepseek-v4-flash-vision-exp``
#: （36 列）配上档位与三项数据要 **82 列** —— 40 列物理上装不下这些**必须出现**的字段。
#: 按 40 列硬截断的后果是尾部 ``n=`` 被吃掉，直接违反「必须带样本量」这条红线
#: （截断掉必需信息比超出建议宽度严重得多）。
#:
#: 因此：40 列约束的是**自由文本**（标题、备注、帮助说明）；**数据行**与脚注一样
#: 享有更宽的预算，但仍设上限，避免病态长串撑爆整行。
ROW_MAX_WIDTH = 120

#: 脚注的宽度上限。脚注是**唯一**不受 :data:`MAX_LINE_WIDTH` 约束的行：口径 / 数据时间 /
#: 样本量三项必须完整出现（plugin_api §6 的示例本身就是 82 列），40 列装不下。
#: 仍设上限是为了防止 ``note`` 之类的自由文本把整行撑爆。
FOOTER_MAX_WIDTH = 120

_MISSING_HINTS = {"", "none", "null", "nan"}


def display_width(text: str) -> int:
    """显示宽度：东亚宽字符（W/F）按 2 计，其余按 1 计。"""
    total = 0
    for char in str(text):
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


def _clip(text: str, width: int = MAX_LINE_WIDTH) -> str:
    """按显示宽度截断（超出部分用 ``…`` 收尾）。"""
    text = " ".join(str(text).split())
    if display_width(text) <= width:
        return text
    out = ""
    used = 0
    for char in text:
        size = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + size > width - 1:
            break
        out += char
        used += size
    return out + "…"


def _row(text: str) -> str:
    """**数据行**专用截断：按 :data:`ROW_MAX_WIDTH` 而不是 40 列。

    凡是同一行必须携带「档位 / IQ / 通过率 / 样本量 / 成本」这类红线条目的行，
    一律走这里 —— 按 40 列截断会吃掉行尾的 ``n=``，等于违反「必须带样本量」。
    """
    return _clip(text, ROW_MAX_WIDTH)


def _text_or_empty(value: object) -> str:
    """空/None/``"null"`` 一律显示为 ``—``。"""
    if value is None:
        return EMPTY
    text = str(value).strip()
    if text.lower() in _MISSING_HINTS:
        return EMPTY
    return text


def _num(value: float | None, digits: int = 1) -> str:
    """数字格式化；``None`` → ``—``（**不是 0**）。"""
    if value is None:
        return EMPTY
    return f"{value:.{digits}f}"


def _int(value: int | None) -> str:
    if value is None:
        return EMPTY
    return str(int(value))


def _pct(value: float | None, digits: int = 1) -> str:
    """比例 → 百分数。上游 ``pass_rate`` 是 0–1 的比例。"""
    if value is None:
        return EMPTY
    return f"{value * 100:.{digits}f}%"


def _money(value: float | None, *, estimate: bool = False, source: str | None = None) -> str:
    """成本格式化。

    ``src != measured`` 时前缀 ``~`` 并标「估算」—— 上游有一半以上的格子成本来自
    ``cross-model-median-shape`` / ``task-level-fallback`` / ``official-shape`` 等回退口径，
    不标注会让人把它当实测账单。

    ``source`` 缺省（``None``）时按**估算**处理：拿不到 ``src`` 就没有证据说是实测，
    宁可多标一次也不能把估算当账单。显式 ``estimate=False`` 且 ``source`` 也是
    ``measured`` 时才输出裸金额。
    """
    if value is None:
        return EMPTY
    flag = estimate or is_estimate(source)
    prefix = "~" if flag else ""
    suffix = " 估算" if flag else ""
    return f"{prefix}${value:.2f}{suffix}"


def _minutes(value: float | None, *, estimate: bool = False) -> str:
    if value is None:
        return EMPTY
    prefix = "~" if estimate else ""
    suffix = " 估算" if estimate else ""
    return f"{prefix}{value:.1f}分{suffix}"


def format_meta_footer(meta: RadarMeta) -> str:
    """统一脚注：``—— DeepSWE · Pass rate 口径 · 最近 3 次有效运行 · 数据 2026-09-13 17:12 · 样本 135``。

    所有展示函数**必须**调用它（plugin_api §6 第 2 条）：没有口径与数据时间的 IQ
    是不可解释的数字。

    构造分两段：**必需段**（频道 / 口径 / 数据时间 / 样本量 / 陈旧标记）先占位，
    **补充段**（``mode``、综合口径、滚动窗口、``note``）按顺序追加，装不下就整段丢弃。
    这样脚注永远不会把「数据时间」或「⚠ 陈旧缓存」这类红线信息截掉一半 —— 尾部截断
    只会砍掉可有可无的说明。

    ``meta.samples`` 为 ``None``（列表类结果，每行自带 ``n=``）时不输出样本段。
    """
    required = [meta.benchmark_id or EMPTY]
    if meta.stale:
        # 陈旧必须显式说，否则用户会把过期数据当成最新结论；放在必需段，永不被裁。
        required.append("⚠ 陈旧缓存")
    if meta.score_label:
        required.append(f"{meta.score_label} 口径")
    if meta.source_updated_at:
        required.append(f"数据 {_stamp(meta.source_updated_at)}")
    if meta.samples is not None:
        required.append(f"样本 {meta.samples}")

    optional = []
    if meta.mode:
        optional.append(f"mode {meta.mode}")
    if meta.recommendation_mode:
        optional.append(f"综合口径 {meta.recommendation_mode}")
    if meta.rolling_window:
        optional.append(f"最近 {meta.rolling_window} 次有效运行")
    if meta.note:
        optional.append(meta.note)

    budget = FOOTER_MAX_WIDTH
    parts = list(required)
    for part in optional:
        if display_width("—— " + " · ".join([*parts, part])) > budget:
            break
        parts.append(part)
    return _clip("—— " + " · ".join(parts), budget)


def _stamp(value: str | None) -> str:
    """把 ISO8601 压成 ``2026-09-13 17:12``（UTC）。解析失败则原样返回。"""
    text = _text_or_empty(value)
    if text == EMPTY:
        return EMPTY
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return _clip(text, 16)


def format_error(error: RadarError) -> str:
    """把错误码转成用户可读的一句。**绝不带上游响应体原文**（plugin_api §2）。"""
    return error.message


# ── 频道与档位 ──


def format_benchmark_list(items: Sequence[BenchmarkInfo]) -> list[str]:
    """频道清单。两个频道分数不可比，故每个频道都标出自己的口径。"""
    if not items:
        return ["雷达暂无可用频道。"]
    lines = ["AI 智商雷达 · 频道"]
    for item in items:
        mark = "（默认）" if item.default else ""
        lines.append(
            _row(
                f"· {item.short_title or item.id}{mark} "
                f"{item.task_count}题 · {item.score_label} · {item.model_config_count}档位"
            )
        )
    return lines


def format_model_list(rows: Sequence[ModelRow], meta: RadarMeta) -> list[str]:
    """档位排行。每行必带 IQ + 通过率 + 样本量 + 档位（四条红线一次满足）。"""
    lines = []
    if not rows:
        lines.append("该频道暂无实测数据。")
    for index, row in enumerate(rows, start=1):
        iq = _num(row.iq)
        if row.iq_derived:
            # 兜底换算与上游 insights IQ 是两个口径，必须标出来。
            iq += "*"
        lines.append(
            _row(
                f"{index}. {row.model}[{row.effort}] IQ {iq} · "
                f"通过 {_pct(row.pass_rate)} · n={row.graded} · {row.cells}题"
            )
        )
    if any(row.iq_derived for row in rows):
        # 兜底换算与上游 insights IQ 是两个口径，必须标出来。
        lines.append(_row("* 本地换算 pass_rate×150，非上游综合 IQ"))
    lines.append(format_meta_footer(meta))
    return lines


def format_model_catalog(combos: Sequence[ModelConfig], meta: RadarMeta) -> list[str]:
    """档位清单（来自 ``/table`` 的 ``combos``，不是价格表）。"""
    if not combos:
        return ["该频道暂无档位数据。", format_meta_footer(meta)]
    grouped: dict[str, list[str]] = {}
    for combo in combos:
        grouped.setdefault(combo.model, []).append(combo.effort)
    lines = ["AI 智商雷达 · 档位"]
    for model in sorted(grouped):
        lines.append(_row(f"· {model}：{'/'.join(grouped[model])}"))
    lines.append(format_meta_footer(meta))
    return lines


# ── 模型档案与对比 ──


def format_model_profile(profile: ModelProfile) -> list[str]:
    """单模型档案。各档位一行，另附效率/运行特征/趋势/最近流水。"""
    lines = [f"{profile.model} · 档位明细"]
    for row in profile.variants:
        iq = _num(row.iq)
        if row.iq_derived:
            iq += "*"
        lines.append(
            _row(
                f"· [{row.effort}] IQ {iq} · 通过 {_pct(row.pass_rate)} · "
                f"n={row.graded} · {row.cells}题"
            )
        )
    if profile.insight is not None:
        # 三个 IQ 必须一起给：视觉频道分数更高是频道难度差异，不是「视觉更强」。
        point = profile.insight
        lines.append(
            _row(
                f"综合 IQ {_num(point.iq)}（软件 {_num(point.software_iq)} · "
                f"视觉 {_num(point.visual_iq)}）· n={point.samples}"
            )
        )
    if profile.efficiency is not None:
        point = profile.efficiency
        lines.append(
            _row(
                f"成本 {_money(point.average_price_usd, source='measured')} · "
                f"耗时 {_minutes(point.average_minutes)} · "
                f"性价比指数 {_num(point.combined_cost_index, 0)}"
            )
        )
        lines.append(
            _row(
                f"通过 {_num(point.passed)}/{_num(point.total, 0)} · "
                f"步数 {_num(point.average_agent_steps)} · "
                f"缓存命中 {_pct(point.cache_hit_rate)}"
            )
        )
    if profile.metrics is not None:
        point = profile.metrics
        lines.append(
            _row(
                f"运行特征：平均 {_num(point.average_agent_steps)} 步 · "
                f"{_num(point.average_total_tokens, 0)} tokens · "
                f"24h {point.runs_24h} 次"
            )
        )
    if profile.trend:
        first, last = profile.trend[0], profile.trend[-1]
        lines.append(
            _row(
                f"趋势 {_stamp(first.timestamp)} → {_stamp(last.timestamp)}："
                f"{_num(first.iq)} → {_num(last.iq)}（{len(profile.trend)} 点）"
            )
        )
    else:
        lines.append("趋势 —")
    for event in profile.recent:
        lines.append(
            _row(
                f"· {_stamp(event.graded_at)} {event.task_id} "
                f"{'通过' if event.passed else '未过'} "
                f"{_money(event.cost_usd, estimate=event.cost_is_estimate or event.cost_is_fallback_estimate)}"
            )
        )
    if profile.meta is not None:
        lines.append(format_meta_footer(profile.meta))
    return lines


def format_comparison(cmp: Comparison) -> list[str]:
    """两档位对比。delta 一律 ``左 - 右``，正数 = 左边更高/更贵/更慢。"""
    left = cmp.left
    right = cmp.right
    lines = [_row(f"{left.model}[{left.effort or EMPTY}] vs {right.model}[{right.effort or EMPTY}]")]

    def _delta(value: float | None, digits: int = 1, suffix: str = "") -> str:
        if value is None:
            return EMPTY
        sign = "+" if value > 0 else ""
        return f"{sign}{value:.{digits}f}{suffix}"

    left_iq = left.best.iq if left.best else None
    right_iq = right.best.iq if right.best else None
    lines.append(_row(f"IQ {_num(left_iq)} vs {_num(right_iq)} · 差 {_delta(cmp.iq_delta)}"))
    lines.append(
        _row(
            f"通过 {_pct(left.best.pass_rate if left.best else None)} vs "
            f"{_pct(right.best.pass_rate if right.best else None)} · "
            f"差 {_delta(cmp.pass_rate_delta * 100 if cmp.pass_rate_delta is not None else None, 1, '%')}"
        )
    )
    lines.append(
        _row(
            f"成本 {_money(left.efficiency.average_price_usd if left.efficiency else None)} vs "
            f"{_money(right.efficiency.average_price_usd if right.efficiency else None)} · "
            f"差 {_delta(cmp.cost_delta_usd, 2, ' 美元')}"
        )
    )
    lines.append(
        _row(
            f"耗时 {_minutes(left.efficiency.average_minutes if left.efficiency else None)} vs "
            f"{_minutes(right.efficiency.average_minutes if right.efficiency else None)} · "
            f"差 {_delta(cmp.duration_delta_minutes, 1, '分')}"
        )
    )
    if cmp.meta is not None:
        lines.append(format_meta_footer(cmp.meta))
    return lines


# ── 推荐与预警 ──


def format_recommendations(recs: Sequence[Recommendation], meta: RadarMeta) -> list[str]:
    """上游推荐。**原样转发**，不重排、不重算（``rule`` 是上游自己给的选取规则）。"""
    if not recs:
        return ["上游暂无推荐。", format_meta_footer(meta)]
    lines = ["AI 智商雷达 · 推荐"]
    for group in recs:
        lines.append(_clip(f"【{group.title or group.key}】"))
        if not group.items:
            lines.append("· —")
        for item in group.items:
            lines.append(
                _row(
                    f"· {item.model}[{item.effort}] IQ {_num(item.iq)} · "
                    f"加权通过 {_num(item.weighted_passed)} · n={item.samples}"
                )
            )
            lines.append(
                _row(
                    f"  成本 {_money(item.average_cost_usd)} · "
                    f"耗时 {_minutes(item.average_duration_minutes)} · "
                    f"性价比 {_num(item.combined_cost_index, 0)}"
                )
            )
    lines.append(format_meta_footer(meta))
    return lines


def format_alerts(alerts: Sequence[DegradationAlert], meta: RadarMeta) -> list[str]:
    """降智预警。

    上游规则已排除 DeepSeek、只与自身历史比较，这里**只转发**：本地补算会造出上游
    不承认的结论。
    """
    if not alerts:
        return ["当前无降智预警。", format_meta_footer(meta)]
    lines = ["AI 智商雷达 · 降智预警"]
    for alert in alerts:
        lines.append(
            _row(
                f"· {alert.model}[{alert.effort}] 当前 IQ {_num(alert.current_iq)} · "
                f"24h 均值 {_num(alert.avg_24h)} · 48h 均值 {_num(alert.avg_48h)}"
            )
        )
        lines.append(
            _row(
                f"  相对 24h {_num(alert.delta_24h)} · 48h {_num(alert.delta_48h)} · "
                f"严重度 {_num(alert.severity, 2)}"
            )
        )
    lines.append(format_meta_footer(meta))
    return lines


# ── 性价比与趋势 ──


def format_value_picks(points: Sequence[EfficiencyPoint], meta: RadarMeta) -> list[str]:
    """性价比榜：``combined_cost_index`` 越低越划算。"""
    if not points:
        return ["该频道暂无性价比数据。", format_meta_footer(meta)]
    lines = ["AI 智商雷达 · 性价比"]
    for index, point in enumerate(points, start=1):
        lines.append(
            _row(
                f"{index}. {point.model}[{point.effort}] 指数 {_num(point.combined_cost_index, 0)} · "
                f"IQ {_num(point.iq)} · {_money(point.average_price_usd)}"
            )
        )
        lines.append(
            _row(
                f"   通过 {_num(point.passed)}/{_num(point.total, 0)} · "
                f"耗时 {_minutes(point.average_minutes)} · n={point.runs_total}"
            )
        )
    lines.append(format_meta_footer(meta))
    return lines


def format_trend(points: Sequence[TrendPoint], *, label: str) -> list[str]:
    """趋势。``label`` 必须说明用的是哪种口径（裸模型名跨档位 / 单档位 / ``latest:``）。"""
    if not points:
        return [f"{label}：无数据。"]
    first, last = points[0], points[-1]
    peak = max(points, key=lambda point: point.iq)
    low = min(points, key=lambda point: point.iq)
    lines = [
        _row(f"{label} · {len(points)} 点"),
        _row(f"· {_stamp(first.timestamp)} IQ {_num(first.iq)} n={first.samples}"),
        _row(f"· {_stamp(last.timestamp)} IQ {_num(last.iq)} n={last.samples}"),
        _row(f"峰值 {_num(peak.iq)}（{_stamp(peak.timestamp)}）"),
        _row(f"谷值 {_num(low.iq)}（{_stamp(low.timestamp)}）"),
    ]
    return lines


# ── 题目 ──


def format_task_detail(detail: TaskDetail) -> list[str]:
    """题目详情：元信息 + 各档位格子 + 通过者。"""
    task = detail.task
    lines = [
        _row(f"{task.id}"),
        _row(f"{task.title or EMPTY}"),
        _row(f"{task.language or EMPTY} · {task.category or EMPTY} · {task.repo or EMPTY}"),
    ]
    if task.discrimination is not None:
        info = task.discrimination
        lines.append(
            _row(
                f"区分度 {_num(info.score)} · 置信度 {_num(info.confidence, 2)} · "
                f"{info.samples} 样本 / {info.cells} 格"
            )
        )
    else:
        lines.append("区分度 —")
    if not detail.cells:
        lines.append("该题暂无档位数据。")
    for cell in detail.cells:
        lines.append(
            _row(
                f"· {cell.model}[{cell.effort}] {cell.st} "
                f"通过 {cell.p}/{cell.n} · 率 {_num(cell.rate, 3)} · "
                f"{_money(cell.cost, source=cell.cost_src)}"
            )
        )
    solved = {cell.model for cell in detail.solved_by}
    lines.append(_row(f"通过的档位数 {len(detail.solved_by)}/{len(detail.cells)}（{len(solved)} 个模型）"))
    if detail.meta is not None:
        lines.append(format_meta_footer(detail.meta))
    return lines


def format_task_ranking(tasks: Sequence[TaskInfo], meta: RadarMeta) -> list[str]:
    """好题榜：按区分度降序。``score`` 高 = 能拉开模型差距。"""
    if not tasks:
        return ["该频道暂无区分度数据。", format_meta_footer(meta)]
    lines = ["AI 智商雷达 · 好题（按区分度）"]
    for index, task in enumerate(tasks, start=1):
        info = task.discrimination
        score = _num(info.score) if info is not None else EMPTY
        confidence = _num(info.confidence, 2) if info is not None else EMPTY
        lines.append(_row(f"{index}. {task.id} 区分度 {score} · 置信 {confidence}"))
    lines.append(format_meta_footer(meta))
    return lines


# ── 社区 ──


def format_contributors(rows: Sequence[ContributorRow], *, scope: str) -> list[str]:
    """贡献者榜。``usd`` 用上游口径（``folded_usd + deepseek_api_usd``），插件不自行加总。"""
    if not rows:
        return [f"暂无贡献者数据（{scope}）。"]
    title = "月度" if scope == "month" else "总"
    lines = [f"AI 智商雷达 · {title}榜贡献者"]
    for index, row in enumerate(rows, start=1):
        points = row.month_points if scope == "month" else row.points
        tokens = row.tokens
        lines.append(
            _row(
                f"{index}. {row.display_name or EMPTY} {_num(points, 1)} 分 · "
                f"{row.graded} 次 · {tokens} tokens"
            )
        )
        lines.append(_row(f"   美元 {_money(row.usd)} · 提交 {row.submissions}"))
    return lines


def format_events(events: Sequence[RadarEvent], meta: RadarMeta) -> list[str]:
    """判分流水。三个成本布尔必须透传（``估算``/``回退估算``/``API 等价``）。"""
    if not events:
        return ["暂无判分流水。", format_meta_footer(meta)]
    lines = ["AI 智商雷达 · 最近判分"]
    for event in events:
        flags = []
        if event.cost_is_estimate:
            flags.append("估算")
        if event.cost_is_fallback_estimate:
            flags.append("回退估算")
        if event.cost_is_api_equivalent:
            flags.append("API 等价")
        suffix = f"（{'/'.join(flags)}）" if flags else ""
        lines.append(
            _row(
                f"· {_stamp(event.graded_at)} {event.task_id} "
                f"{'通过' if event.passed else '未过'}"
            )
        )
        lines.append(
            _row(
                f"  {event.model}[{event.effort}] {event.harness or EMPTY} "
                f"{_money(event.cost_usd, estimate=bool(flags))}{suffix}"
            )
        )
    lines.append(format_meta_footer(meta))
    return lines


def format_pulse(pulse: FleetPulse | None, race: FlagRace | None, meta: RadarMeta) -> list[str]:
    """实时面板：全队提交吞吐 + 旗赛。

    ``pedal_speed`` 的 ``scope`` 是**全队提交**口径（不是单模型），展示时必须说清楚，
    否则会被读成「这个模型每小时烧 27 美元」。
    """
    lines = ["AI 智商雷达 · 实时"]
    if pulse is None:
        lines.append("吞吐 —")
    else:
        lines.append(
            _row(
                f"全队提交（{pulse.window_minutes} 分钟窗口）："
                f"{pulse.submitted_runs} 次 · {pulse.tokens_per_hour or 0} tokens/时"
            )
        )
        lines.append(
            _row(
                f"成本 {_money(pulse.usd_per_hour)}/时（API 等价 "
                f"{_money(pulse.api_equivalent_usd_per_hour)}）· "
                f"缓存命中 {_pct(pulse.cache_hit_ratio)}"
            )
        )
    if race is None:
        lines.append("旗赛 —")
    else:
        lines.append(
            _row(
                f"旗赛 {race.status} · 进度 {_money(race.current_usd)}/"
                f"{_money(race.target_usd)}"
            )
        )
        if race.winner_name or race.winning_task_id:
            lines.append(
                _row(
                    f"获胜者 {race.winner_name or EMPTY} · {race.winning_task_id or EMPTY} "
                    f"{race.winning_model or EMPTY}[{race.winning_effort or EMPTY}]"
                )
            )
    lines.append(format_meta_footer(meta))
    return lines


def format_help() -> list[str]:
    """帮助文本。"""
    return [
        "AI 智商雷达 · 命令",
        "· /radar 榜 [频道] — 档位排行",
        "· /radar 模型 <名> [档位] — 模型档案",
        "· /radar 对比 <A> <B> [档位] — 对比",
        "· /radar 推荐 [频道] — 上游推荐",
        "· /radar 预警 [频道] — 降智预警",
        "· /radar 性价比 [频道] — 性价比榜",
        "· /radar 趋势 <名> [档位] — IQ 趋势",
        "· /radar 频道 — 频道清单",
        "· /radar 档位 [频道] — 模型档位清单",
        "数据来源 api.codexradar.com（只读）。",
    ]


__all__ = [
    "EMPTY",
    "FOOTER_MAX_WIDTH",
    "MAX_LINE_WIDTH",
    "display_width",
    "format_alerts",
    "format_benchmark_list",
    "format_comparison",
    "format_contributors",
    "format_error",
    "format_events",
    "format_help",
    "format_meta_footer",
    "format_model_catalog",
    "format_model_list",
    "format_model_profile",
    "format_pulse",
    "format_recommendations",
    "format_task_detail",
    "format_task_ranking",
    "format_trend",
    "format_value_picks",
]
