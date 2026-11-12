"""运行状态卡的面板与文字降级：公开版（群聊）只放常规运行与服务器状态，详细版（超管私聊）加内部信息。"""

from __future__ import annotations

from html import escape
from pathlib import Path

from otae_bot.groups.features import PLUGIN_NAMES, plugin_label
from otae_bot.infrastructure.rendering.art_card import ArtCard

from .collect import ADMIN_PLUGINS, StatusSnapshot

ASSETS = Path(__file__).parent / "assets"
#: 渲染排队超过这么多张算偏忙；事件循环延迟超过这么多秒算偏慢。
BUSY_QUEUE = 3
SLOW_LAG = 0.2


def text(value: object) -> str:
    return escape(str(value))


def _row(name: str, desc: str, badge: str, kind: str = "ok") -> str:
    return (
        f'<div class="st-row"><span class="st-cmd">{text(name)}</span><span class="st-desc">{text(desc)}</span>'
        f'<span class="st-badge st-{kind}">{text(badge)}</span></div>'
    )


def _heading(title: str, note: str = "") -> str:
    note_html = f"<em>{text(note)}</em>" if note else ""
    return f'<h2 class="st-heading">{text(title)}{note_html}</h2>'


def _delta(today: int, yesterday: int) -> str:
    if not yesterday:
        return "昨日同时段没有记录"
    change = round((today - yesterday) / yesterday * 100)
    return f"较昨日同时段 {'+' if change > 0 else ''}{change}%"


def _feature_plugins(snapshot: StatusSnapshot) -> list[tuple[str, bool]]:
    """公开版只列登记过中文名的功能插件；管理插件和废弃的空包（如 eft_helper）不列。"""
    return [(name, loaded) for name, loaded in snapshot.plugins if name in PLUGIN_NAMES and name not in ADMIN_PLUGINS]


def _strip(snapshot: StatusSnapshot) -> str:
    today, timing = snapshot.today, snapshot.render_timing
    features = _feature_plugins(snapshot)
    loaded = sum(1 for _, ok in features if ok)
    rate = (today.total - today.failures) / today.total * 100 if today.total else 100.0
    mean = f"{timing['mean']:.1f}" if timing["mean"] is not None else "—"
    p95 = f"P95 {timing['p95']:.1f} 秒" if timing["p95"] is not None else "今天还没有出图"
    missing = len(features) - loaded
    cells = (
        ("今日指令", f"{today.total:,}", "条", _delta(today.total, today.yesterday)),
        ("平均出图", mean, "秒", p95),
        ("今日成功率", f"{rate:.1f}", "%", f"失败 {today.failures} 次"),
        ("功能插件", str(loaded), f"/ {len(features)}", f"{missing} 个未加载" if missing else "全部已加载"),
    )
    return '<div class="st-strip">' + "".join(
        f"<div><small>{text(a)}</small><b>{text(b)}<i>{text(c)}</i></b><em>{text(d)}</em></div>" for a, b, c, d in cells
    ) + "</div>"


def _render_row(snapshot: StatusSnapshot, detail: bool) -> str:
    render = snapshot.render
    waiting = sum((render.get("waiting") or {}).values())
    desc = f"使用 {render['inflight']} / {render['concurrency']} · 排队 {waiting} · 上下文 {render['contexts']}" if detail else f"排队 {waiting}"
    return _row("图片渲染" if not detail else "渲染浏览器", desc, "偏忙" if waiting >= BUSY_QUEUE else "正常", "warn" if waiting >= BUSY_QUEUE else "ok")


def _lag_row(snapshot: StatusSnapshot) -> str:
    lag = snapshot.loop_lag
    if lag["last"] is None:
        return _row("响应延迟", "启动中", "—", "idle")
    worst = lag["max"] or 0
    return _row(
        "响应延迟",
        f"{lag['last'] * 1000:.0f} ms · 近 1 分钟最高 {worst * 1000:.0f} ms",
        "偏慢" if worst >= SLOW_LAG else "正常",
        "warn" if worst >= SLOW_LAG else "ok",
    )


def _connection(snapshot: StatusSnapshot, detail: bool) -> str:
    accounts = snapshot.accounts
    if detail:
        rows = "".join(
            _row(f"{platform.upper() or '账号'} {self_id}", f"{groups} 个群" if groups is not None else "群列表未取到", "在线")
            for platform, self_id, groups in accounts
        ) or _row("QQ 账号", "没有在线账号", "离线", "bad")
        head = _heading("连接", "OneBot · " + "、".join(snapshot.endpoints))
    else:
        rows = _row("QQ 连接", f"{len(accounts)} 个账号在线", "正常") if accounts else _row("QQ 连接", "没有在线账号", "离线", "bad")
        head = _heading("运行状态")
    return f"<section>{head}{rows}{_render_row(snapshot, detail)}{_lag_row(snapshot)}</section>"


def _server(snapshot: StatusSnapshot) -> str:
    rows = []
    for meter in snapshot.meters:
        bar = (
            f'<span class="st-track"><i class="{"st-warnbar" if meter.warn else ""}" style="width:{max(2, meter.ratio * 100):.0f}%"></i></span>'
            if meter.ratio is not None
            else '<span class="st-track st-none"></span>'
        )
        rows.append(f'<div class="st-meter"><b>{text(meter.label)}</b>{bar}<span>{text(meter.value)}</span></div>')
    return f'<section>{_heading("服务器", "本机")}{"".join(rows)}</section>'


def _hours(snapshot: StatusSnapshot) -> str:
    today = snapshot.today
    peak = max(today.hours) or 1
    current = snapshot.now.hour
    bars = "".join(
        f'<i class="{"st-now" if hour == current else ""}" style="height:{count / peak * 100:.0f}%"></i>'
        for hour, count in enumerate(today.hours)
    )
    top = "".join(f'<span class="st-cmd">{text(label)}</span><em>{count:,}</em>' for label, count in today.top)
    top_html = f'<div class="st-top">{top}</div>' if top else '<p class="st-empty">今天还没有人用过指令。</p>'
    return (
        f'<section>{_heading("今日指令", f"按小时 · {today.total:,} 条")}<div class="st-hours">{bars}</div>'
        '<div class="st-axis"><span>00</span><span>06</span><span>12</span><span>18</span><span>23</span></div>'
        f"{top_html}</section>"
    )


def _plugins(snapshot: StatusSnapshot, detail: bool) -> str:
    items = snapshot.plugins if detail else _feature_plugins(snapshot)
    loaded = sum(1 for _, ok in items if ok)
    chips = "".join(
        f'<span class="{"st-off" if not ok else "st-admin" if name in ADMIN_PLUGINS else ""}">{text(plugin_label(name))}</span>'
        for name, ok in items
    )
    note = f"已加载 {loaded} / {len(items)}" + (" · 黄色为管理插件" if detail else "")
    return f'<section>{_heading("插件" if detail else "功能插件", note)}<div class="st-chips">{chips}</div></section>'


def _interval(seconds: float) -> str:
    if seconds >= 3600:
        return f"每 {seconds / 3600:g} 小时"
    if seconds >= 60:
        return f"每 {seconds / 60:g} 分钟"
    return f"每 {seconds:g} 秒"


def _ago(snapshot: StatusSnapshot, moment) -> str:
    seconds = int((snapshot.now - moment).total_seconds())
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return moment.strftime("%m-%d %H:%M")


def _jobs(snapshot: StatusSnapshot) -> str:
    if not snapshot.jobs:
        return f'<section>{_heading("后台任务")}<p class="st-empty">没有注册的定时任务。</p></section>'
    rows = []
    for job in snapshot.jobs[:8]:
        if job.running:
            badge, kind = "运行中", "ok"
        elif job.last_finished is None:
            badge, kind = "还没跑过", "idle"
        else:
            badge, kind = _ago(snapshot, job.last_finished), "ok" if job.last_ok else "bad"
        desc = _interval(job.interval_seconds)
        if job.last_ok is False:
            desc = job.last_error or "上次失败"
        elif job.failures:
            desc += f" · 累计失败 {job.failures} 次"
        rows.append(_row(job.label, desc, badge, kind))
    more = f"共 {len(snapshot.jobs)} 个，失败的排前面" if len(snapshot.jobs) > 8 else f"共 {len(snapshot.jobs)} 个"
    return f'<section>{_heading("后台任务", more)}{"".join(rows)}</section>'


def _alerts(snapshot: StatusSnapshot) -> str:
    if not snapshot.alerts:
        return f'<section>{_heading("最近告警", "WARNING 及以上")}<p class="st-empty">启动以来没有告警。</p></section>'
    rows = "".join(
        f'<p class="st-note{" st-error" if alert.level in {"ERROR", "CRITICAL"} else ""}">'
        f"<time>{alert.at:%m-%d %H:%M}</time>{text(alert.message)}</p>"
        for alert in snapshot.alerts
    )
    return f'<section>{_heading("最近告警", f"WARNING 及以上 · 最近 {len(snapshot.alerts)} 条")}{rows}</section>'


def _runtime(snapshot: StatusSnapshot) -> str:
    rows = [("版本", f"{snapshot.version or '未知'} · 启动于 {snapshot.started_at:%m-%d %H:%M}"), *snapshot.runtime_info]
    body = "".join(f'<div class="st-row"><span class="st-cmd">{text(k)}</span><span class="st-desc">{text(v)}</span></div>' for k, v in rows)
    return f'<section>{_heading("运行环境")}{body}</section>'


def panel_html(snapshot: StatusSnapshot, *, detail: bool) -> str:
    if detail:
        left = _connection(snapshot, True) + _server(snapshot) + _plugins(snapshot, True) + _hours(snapshot)
        right = _jobs(snapshot) + _alerts(snapshot) + _runtime(snapshot)
        foot = f"仅超管私聊可见 · 生成于 {snapshot.now:%m-%d %H:%M}"
    else:
        left = _connection(snapshot, False) + _server(snapshot)
        right = _hours(snapshot) + _plugins(snapshot, False)
        foot = f"生成于 {snapshot.now:%m-%d %H:%M}"
    return (
        _strip(snapshot)
        + f'<div class="st-cols"><div>{left}</div><div>{right}</div></div>'
        + f'<p class="st-foot">{text(foot)}</p>'
    )


def subtitle(snapshot: StatusSnapshot, *, detail: bool) -> str:
    parts = ["超管私聊" if detail else "在线"]
    if snapshot.version:
        parts.append(snapshot.version)
    parts.append(f"已连续运行 {snapshot.uptime}")
    return " · ".join(parts)


def build_card(snapshot: StatusSnapshot, *, detail: bool) -> ArtCard:
    return ArtCard(
        title="otaebot 运行状态 · 详细" if detail else "otaebot 运行状态",
        subtitle=subtitle(snapshot, detail=detail),
        panel_html=panel_html(snapshot, detail=detail),
        css=(ASSETS / "card.css").read_text(encoding="utf-8"),
    )


def format_text(snapshot: StatusSnapshot, *, detail: bool) -> list[str]:
    """出图失败时的文字版。"""
    today = snapshot.today
    lines = [
        f"otaebot 运行状态 · {subtitle(snapshot, detail=detail)}",
        f"今日指令 {today.total} 条，失败 {today.failures} 次",
        f"QQ 连接：{len(snapshot.accounts)} 个账号在线",
        *(f"{meter.label}：{meter.value}" for meter in snapshot.meters),
    ]
    if detail:
        failing = [job for job in snapshot.jobs if job.last_ok is False]
        lines.append(f"后台任务 {len(snapshot.jobs)} 个，上次失败 {len(failing)} 个")
        lines += [f"[{alert.at:%H:%M}] {alert.message}" for alert in snapshot.alerts]
    return lines
