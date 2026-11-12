"""指令统计的卡片面板与文本降级。所有进入 HTML 的文本都过 ``escape``。"""

from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path

from otae_bot.command_usage.store import UsageReport
from otae_bot.groups.features import plugin_label
from otae_bot.infrastructure.rendering.art_card import ArtCard

ASSETS = Path(__file__).parent / "assets"
#: 插件占比条最多分几段，其余合成「其他」。
PLUGIN_SLICES = 5
PLUGIN_COLORS = ("#527fbe", "#7aa0d4", "#a5bcde", "#4f9a6a", "#c58a2c")
OTHER_COLOR = "#b8bcc4"
WEEKDAYS = "一二三四五六日"


def text(value: object) -> str:
    return escape(str(value))


def scope_label(report: UsageReport, days: int) -> str:
    where = "全局" if report.scope.is_global else "本群"
    if report.scope.plugin:
        where += f" · {plugin_label(report.scope.plugin)}"
    if days == 1:
        return f"{where} · 今天（{report.end:%m-%d}）"
    return f"{where} · 近 {days} 天（{report.start:%m-%d} ~ {report.end:%m-%d}）"


def _delta(current: int, previous: int) -> tuple[str, str]:
    if previous == 0:
        return ("新", "up") if current else ("持平", "flat")
    change = round((current - previous) / previous * 100)
    if change == 0:
        return "持平", "flat"
    return (f"+{change}%", "up") if change > 0 else (f"{change}%", "down")


def _strip(report: UsageReport, days: int) -> str:
    sign, _ = _delta(report.total, report.previous_total)
    trend = "上期没有记录" if sign == "新" else f"较上期 {sign}"
    peak_day, peak = max(report.days, key=lambda item: item[1], default=("", 0))
    peak_text = f"峰值 {peak_day[5:]} · {peak:,} 次" if peak else "还没有调用"
    cells = (
        ("总调用", f"{report.total:,}", "次", trend),
        ("活跃用户", f"{report.users:,}", "人", f"新面孔 {report.new_users} 人"),
        ("日均", f"{report.total / max(1, days):,.0f}", "次", peak_text),
        ("成功率", f"{report.success_rate:.1f}", "%", f"失败 {report.failures} 次 · 均耗时 {report.avg_ms / 1000:.1f} 秒"),
    )
    return '<div class="us-strip">' + "".join(
        f"<div><small>{text(label)}</small><b>{text(value)}<i>{text(unit)}</i></b><em>{text(note)}</em></div>"
        for label, value, unit, note in cells
    ) + "</div>"


def _ranking(report: UsageReport) -> str:
    if not report.commands:
        return '<p class="us-empty">这段时间还没有人用过指令。</p>'
    top = report.commands[0].count
    rows = []
    for index, row in enumerate(report.commands, 1):
        sign, kind = _delta(row.count, row.previous)
        share = row.count / report.total * 100 if report.total else 0
        rows.append(
            f'<div class="us-rank{" us-top" if index <= 3 else ""}"><span class="us-no">{index:02d}</span>'
            f'<div class="us-name"><b>{text(row.label)}</b><small>{text(plugin_label(row.plugin))} · 均 {row.avg_ms / 1000:.1f}s</small>'
            f'<span class="us-track"><i style="width:{row.count / top * 100:.0f}%"></i></span></div>'
            f'<span class="us-num">{row.count:,}<small>{share:.1f}%</small></span>'
            f'<span class="us-delta us-{kind}">{text(sign)}</span></div>'
        )
    return "".join(rows)


def _plugin_share(report: UsageReport) -> str:
    total = sum(count for _, count in report.plugins)
    if not total:
        return ""
    slices = [(plugin_label(plugin), count, color) for (plugin, count), color in zip(report.plugins, PLUGIN_COLORS)]
    rest = sum(count for _, count in report.plugins[PLUGIN_SLICES:])
    if rest:
        slices.append(("其他", rest, OTHER_COLOR))
    bar = "".join(f'<i style="width:{count / total * 100:.2f}%;background:{color}"></i>' for _, count, color in slices)
    legend = "".join(
        f'<span><i style="background:{color}"></i>{text(name)}<b>{count / total * 100:.0f}%</b></span>'
        for name, count, color in slices
    )
    return f'<div class="us-stack">{bar}</div><div class="us-legend">{legend}</div>'


def _days(report: UsageReport, today: str) -> str:
    peak = max((count for _, count in report.days), default=0) or 1
    many = len(report.days) > 10
    bars = "".join(
        f'<div class="{"us-today" if day == today else ""}">{"" if many else f"<b>{count:,}</b>"}'
        f'<i style="height:{count / peak * (100 if many else 78):.0f}%"></i></div>'
        for day, count in report.days
    )
    step = max(1, len(report.days) // 5)
    labels = "".join(
        f"<span>{'今天' if day == today else day[5:] if (index % step == 0 or index == len(report.days) - 1) else ''}</span>"
        for index, (day, _) in enumerate(report.days)
    )
    return f'<div class="us-days{" us-many" if many else ""}">{bars}</div><div class="us-dayaxis">{labels}</div>'


def _heat(report: UsageReport) -> str:
    peak = max((count for row in report.heat for count in row), default=0) or 1
    cells = []
    for weekday, row in enumerate(report.heat):
        cells.append(f"<span>周{WEEKDAYS[weekday]}</span>")
        cells.extend(f'<i style="background:rgba(82,127,190,{0.07 + count / peak * 0.93:.2f})"></i>' for count in row)
    return (
        f'<div class="us-heat">{"".join(cells)}</div>'
        '<div class="us-heatx"><span></span><span>00</span><span>06</span><span>12</span><span>18</span></div>'
    )


def _users(report: UsageReport) -> str:
    if not report.top_users:
        return ""
    title = "使用量最高的用户" if report.scope.is_global else "最常用的群友"
    note = "全部群合计" if report.scope.is_global else "本群"
    rows = "".join(
        f"<div><em>{index}</em><b>{text(user.name)}</b><small>{user.count:,} 次</small></div>"
        for index, user in enumerate(report.top_users, 1)
    )
    return f'<section class="us-wide"><h2 class="us-heading">{title}<em>{note}</em></h2><div class="us-users">{rows}</div></section>'


def panel_html(report: UsageReport, days: int, now: datetime) -> str:
    foot = f'<p class="us-foot">生成于 {now:%m-%d %H:%M}</p>'
    if not report.total:
        return _strip(report, days) + '<p class="us-empty">这段时间还没有人用过指令。</p>' + foot
    plugin_section = (
        ""
        if report.scope.plugin
        else f'<h2 class="us-heading">按插件<em>调用占比</em></h2>{_plugin_share(report)}'
    )
    return (
        _strip(report, days)
        + '<div class="us-cols"><section>'
        + f'<h2 class="us-heading">指令排行<em>前 {len(report.commands)} · 次数与占比 · 较上期</em></h2>{_ranking(report)}'
        + "</section><section>"
        + plugin_section
        + f'<h2 class="us-heading">每日调用<em>{"今天" if days == 1 else f"近 {days} 天"}</em></h2>{_days(report, now.strftime("%Y-%m-%d"))}'
        + f'<h2 class="us-heading">活跃时段<em>星期 × 小时，颜色越深越忙</em></h2>{_heat(report)}'
        + "</section></div>"
        + _users(report)
        + foot
    )


def build_card(report: UsageReport, days: int, now: datetime) -> ArtCard:
    return ArtCard(
        title="指令统计",
        subtitle=scope_label(report, days),
        panel_html=panel_html(report, days, now),
        css=(ASSETS / "card.css").read_text(encoding="utf-8"),
    )


def format_text(report: UsageReport, days: int) -> list[str]:
    """出图失败时的文字版。"""
    lines = [
        f"指令统计 · {scope_label(report, days)}",
        f"总调用 {report.total} 次 · 活跃用户 {report.users} 人 · 成功率 {report.success_rate:.1f}%",
    ]
    if report.commands:
        lines.append("指令排行：")
        lines += [f"{index}. {row.label}  {row.count} 次" for index, row in enumerate(report.commands, 1)]
    else:
        lines.append("这段时间还没有人用过指令。")
    if report.top_users:
        lines.append("使用量最高的用户：" if report.scope.is_global else "最常用的群友：")
        lines += [f"{index}. {user.name}  {user.count} 次" for index, user in enumerate(report.top_users, 1)]
    return lines
