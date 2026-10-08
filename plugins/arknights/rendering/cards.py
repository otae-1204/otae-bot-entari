"""Attendance card renderer for the Arknights Skland plugin.

Reward icons come only from the approved bundled artwork; no remote URL is
fetched or embedded from API or user input. Every
nickname, channel name and message goes through :func:`esc`/:func:`esc_attr`.

Shares Endfield's rounded attendance layout with an Arknights blue-black header.
"""

from __future__ import annotations

import html
import re
import tempfile
from pathlib import Path

from otae_bot.infrastructure.rendering.browser import screenshot_web_element
from otae_bot.infrastructure.rendering.temp_files import schedule_temp_file_cleanup

from ..models import AttendanceCardView, daily_progress_value
from .icons import reward_icon_url

CARD_WIDTH = 1280
CARD_MAX_HEIGHT = 8192
# Long/abnormal rosters must never be silently truncated into an unreadable image.
MAX_CARD_ROWS = 60

# Defence in depth: even though every card string comes from our own client,
# no URL-like token is allowed to reach the renderer as text.
_REMOTE_URL = re.compile(r"\b(?:https?|data|file|blob)\s*:[^\s\"'<>]*", re.IGNORECASE)
_URL_PLACEHOLDER = "[已省略链接]"


def sanitize_text(value: object) -> str:
    """Strip URL-like substrings so no remote resource can enter the document."""
    return _REMOTE_URL.sub(_URL_PLACEHOLDER, str(value if value is not None else ""))


def esc(value: object) -> str:
    return html.escape(sanitize_text(value), quote=True)


def esc_attr(value: object) -> str:
    return html.escape(sanitize_text(value), quote=True)


def _reward_icon(name: str) -> str:
    url = reward_icon_url(name)
    return f'<img class="attendance-reward-icon" src="{url}" alt="{esc(name)}">' if url else ""


def render_attendance_card_html(view: AttendanceCardView) -> str:
    """Build the compact, rounded layout used by the Endfield attendance preview."""
    rows = "".join(_render_row(role) for role in view.roles[:MAX_CARD_ROWS])
    if not rows:
        rows = '<div class="empty">没有可签到的角色</div>'
    elif len(view.roles) > MAX_CARD_ROWS:
        rows += f'<div class="empty">仅显示前 {MAX_CARD_ROWS} 个角色，共 {len(view.roles)} 个，请分批签到。</div>'
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
*{{box-sizing:border-box}}
html,body{{margin:0;width:{CARD_WIDTH}px;background:#ededed;color:#222;font-family:'Microsoft YaHei','PingFang SC','Noto Sans SC',Arial,sans-serif}}
/* Match Endfield's QQ thumbnail ratio: 1280 / 430 stays below 3:1. */
.ak-card{{width:{CARD_WIDTH}px;min-height:430px;padding:24px;background:#ededed}}
header{{display:flex;justify-content:space-between;align-items:center;gap:24px;margin-bottom:16px;padding:22px 24px;background:#142536;color:#fff;border-bottom:5px solid #54b9ef;border-radius:4px}}
header>div{{min-width:0}}header small{{font-size:12px;letter-spacing:.16em;color:#b8cbdc}}header h1{{margin:7px 0 0;font-size:34px;line-height:1.2}}header time{{max-width:38%;text-align:right;color:#c7d5e2;font-size:17px;overflow-wrap:anywhere}}
.empty{{padding:26px;text-align:center;color:#777;background:#f8f8f8;border:1px dashed #ccc;border-radius:8px}}
.attendance-list{{display:grid;gap:12px}}
.ak-row{{display:grid;grid-template-columns:minmax(0,.8fr) minmax(0,1.2fr);min-height:112px;border:1px solid #cecece;border-left:5px solid #787878;border-radius:8px;background:#fff}}
.ak-row.status-failed{{background:#fafafa}}
.role-main,.status{{min-width:0;padding:18px 20px}}.role-main{{display:flex;flex-direction:column;justify-content:center;gap:5px;border-right:1px solid #e0e0e0}}.role-main strong{{font-size:27px;line-height:1.4;color:#252525}}.role-main span{{color:#777;font-size:16px;line-height:1.5}}
.status{{display:flex;flex-wrap:wrap;align-items:center;gap:14px}}.status-copy{{display:flex;flex:1 1 260px;min-width:0;flex-direction:column;justify-content:center}}.status-copy>b{{font-size:21px;line-height:1.5;color:#333;overflow-wrap:anywhere}}.status-already .status-copy>b{{color:#666}}.status-copy>span{{margin-top:7px;min-width:0;color:#888;font-size:16px;line-height:1.5}}
.attendance-rewards{{display:flex;flex-wrap:wrap;align-items:center;gap:8px}}.attendance-reward{{display:inline-flex;align-items:center;gap:8px;max-width:100%;min-width:0;padding:5px 9px;border:1px solid #ddd;border-radius:6px;background:#f5f5f5;color:#444;line-height:1.4}}.attendance-reward b{{margin-left:3px;font-weight:700;color:#252525}}
.attendance-reward-icon{{width:40px;height:40px;flex:none;object-fit:contain}}.milestone-node .attendance-reward-icon{{width:100%;max-width:26px;height:auto;aspect-ratio:1}}.milestone-reward .attendance-reward-icon{{width:26px;height:26px;vertical-align:middle;margin-right:6px}}
.attendance-meta{{flex:0 1 118px;min-width:0;max-width:100%;padding:10px 13px;border:1px solid #d5d5d5;border-radius:6px;background:#eee}}.attendance-meta span,.attendance-meta b{{display:block;margin:0;overflow-wrap:anywhere}}.attendance-meta span{{color:#777;font-size:13px;font-weight:400;line-height:1.5}}.attendance-meta b{{margin-top:3px;color:#333;font-size:24px;line-height:1.3}}
.milestones{{grid-column:1/-1;min-width:0;padding:15px 20px 18px;border-top:1px solid #e0e0e0}}
.milestone-heading{{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:8px 16px;margin-bottom:11px}}
.milestone-heading h2{{margin:0;color:#333;font-size:16px;font-weight:700}}.milestone-heading span{{color:#888;font-size:13px;overflow-wrap:anywhere}}
.milestone-list{{display:flex;flex-wrap:wrap;gap:8px}}
.milestone{{min-width:170px;max-width:100%;flex:1 1 170px;padding:11px 14px;border:1px solid #ddd;border-radius:6px;background:#f5f5f5;overflow-wrap:anywhere}}
.milestone-day{{display:block;color:#777;font-size:13px;line-height:1.5}}.milestone-reward{{display:block;margin-top:5px;color:#333;font-size:17px;font-weight:700;line-height:1.4}}
.milestone-state{{display:inline-block;margin-top:7px;color:#888;font-size:12px;line-height:1.5}}.milestone.done{{background:#eee}}.milestone.done .milestone-state{{color:#666}}.milestone.available{{border-color:#787878}}.milestone.available .milestone-state{{color:#333;font-weight:700}}
.attendance-milestones{{grid-column:1/-1;display:flex;align-items:center;gap:22px;min-width:0;padding:12px 20px 14px;border-top:1px solid #e6e6e6}}
.milestone-summary,.milestone-next{{flex:none;min-width:0}}.milestone-summary{{width:128px}}.milestone-next{{width:176px;text-align:right}}
.milestone-summary span,.milestone-next span{{display:block;color:#777;font-size:13px;line-height:1.5;overflow-wrap:anywhere}}
.milestone-summary b,.milestone-next b{{display:block;margin-top:2px;color:#333;font-size:20px;line-height:1.3;overflow-wrap:anywhere}}
.milestone-summary small{{margin-left:3px;color:#999;font-size:14px;font-weight:400}}
.milestone-track{{flex:1 1 auto;min-width:0;display:grid;grid-template-columns:repeat(var(--days),minmax(0,1fr));grid-template-rows:auto 10px auto;column-gap:3px;row-gap:5px;align-items:end}}
.milestone-node{{grid-row:1;display:flex;flex-direction:column;align-items:center;min-width:0;color:#9a9a9a}}
.milestone-node b{{max-width:100%;font-size:13px;line-height:1.2;overflow-wrap:anywhere}}.milestone-node.done,.milestone-node.available{{color:#252525}}
.milestone-cell{{grid-row:2;height:10px;border-radius:2px;background:#e3e3e3}}.milestone-cell.done{{background:#3a3a3a}}
.milestone-cell.reward-day{{box-shadow:inset 0 0 0 2px #8f8f8f}}.milestone-cell.reward-day.done{{box-shadow:none;background:#1f1f1f}}
.milestone-track-day{{grid-row:3;justify-self:center;color:#8a8a8a;font-size:12px;line-height:1.2;white-space:nowrap}}
/* Keep adversarial/long text inside the same layout, never silently clip it. */
.attendance-reward>span{{min-width:0}}
.ak-name,.ak-detail,.role-main span,.attendance-reward{{overflow-wrap:anywhere}}
</style></head><body><div class="ak-card">
<header><div><small>ARKNIGHTS / SKLAND</small><h1>签到结果</h1></div><time>{esc(view.generated_at or "--")}</time></header>
<main class="attendance-list">{rows}</main>
</div></body></html>"""


def _render_row(role) -> str:
    status = role.status if role.status in {"success", "already", "failed"} else "failed"
    if status == "success":
        chips = "".join(
            f'<span class="attendance-reward ak-chip">{_reward_icon(item.name)}<span>{esc(item.name)} <b>× {int(item.count)}</b></span></span>'
            for item in role.rewards
        )
        detail = f'<span class="attendance-rewards">{chips}</span>' if chips else "无奖励明细"
    elif status == "already":
        detail = "今日已签到，无需重复签到"
    else:
        detail = esc(role.message or "签到失败，请稍后重试")
    count = getattr(role, "monthly_count", None)
    monthly = (
        f'<div class="attendance-meta"><span>当月累签</span><b>{count} 天</b></div>'
        if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 31 else ""
    )
    return f"""<section class="ak-row status-{status}">
<div class="role-main"><strong class="ak-name">{esc(role.nickname)}</strong><span>{esc(role.server_label)} · {esc(role.uid)}</span></div>
<div class="status"><div class="status-copy"><b>{esc(role.status_label)}</b><span class="ak-detail">{detail}</span></div>{monthly}</div>
{_render_milestones(role)}
</section>"""


def _render_milestones(role) -> str:
    """Render only server-provided cumulative daily reward nodes."""
    milestones = tuple(getattr(role, "milestones", ()) or ())
    if not milestones or role.status not in {"success", "already"}:
        return ""
    progress = daily_progress_value(getattr(role, "daily_progress", ()))
    if progress:
        return _render_milestone_track(milestones, progress)
    # Older/incomplete results retain their known reward nodes, without inventing
    # a full calendar length or painting a made-up signed prefix.
    nodes = []
    for item in milestones:
        state, label = ("done", "已领取") if item.done else (
            ("available", "可领取") if item.available else ("pending", "未达成")
        )
        amount = f" × {int(item.count)}" if item.count > 0 else ""
        nodes.append(
            f'<div class="milestone {state}"><span class="milestone-day">累计第 {int(item.day)} 天</span>'
            f'<strong class="milestone-reward">{_reward_icon(item.name)}{esc(item.name)}{amount}</strong>'
            f'<span class="milestone-state">{label}</span></div>'
        )
    summary = ""
    unclaimed = tuple(item for item in milestones if not item.done)
    count = getattr(role, "monthly_count", None)
    if not unclaimed:
        summary = "本月合成玉奖励已全部领取"
    elif any(item.available for item in unclaimed):
        summary = "有合成玉奖励可领取"
    elif isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 31:
        remaining = min(item.day for item in unclaimed) - count
        if remaining > 0:
            summary = f"距离下次奖励还需 {remaining} 次签到"
    return (
        '<div class="milestones"><div class="milestone-heading"><h2>合成玉里程碑</h2>'
        f'<span>{esc(summary)}</span></div><div class="milestone-list">{"".join(nodes)}</div></div>'
    )


def _render_milestone_track(milestones, progress: tuple[bool, ...]) -> str:
    milestones = sorted((item for item in milestones if 1 <= item.day <= len(progress)), key=lambda item: item.day)
    if not milestones:
        return ""
    nodes, labels = [], []
    for item in milestones:
        state = "done" if item.done else "available" if item.available else "pending"
        label = "已领取" if item.done else "可领取" if item.available else "未达成"
        title = esc(f"累计第 {item.day} 天 · {item.name} × {item.count} · {label}")
        nodes.append(
            f'<div class="milestone-node {state}" style="grid-column:{item.day}" '
            f'title="{title}" aria-label="{title}">{_reward_icon(item.name)}<b>{int(item.count)}</b></div>'
        )
        labels.append(f'<span class="milestone-track-day" style="grid-column:{item.day}">{item.day}</span>')
    milestone_days = {item.day for item in milestones}
    cells = "".join(
        f'<i class="milestone-cell{" done" if done else ""}{" reward-day" if day in milestone_days else ""}" '
        f'style="grid-column:{day}"></i>' for day, done in enumerate(progress, 1)
    )
    claimed = sum(item.count for item in milestones if item.done)
    total = sum(item.count for item in milestones)
    unclaimed = [item for item in milestones if not item.done]
    available = next((item for item in unclaimed if item.available), None)
    upcoming = available or next(iter(unclaimed), None)
    if upcoming is None:
        next_copy = '<span>本月合成玉</span><b>已全部领取</b>'
    else:
        remaining = upcoming.day - sum(progress)
        state = "可领取" if available else f"还需签到 {remaining} 天" if remaining > 0 else "领取状态待确认"
        next_copy = f'<span>下一档 第 {upcoming.day} 天 × {upcoming.count}</span><b>{state}</b>'
    return (
        '<div class="attendance-milestones">'
        f'<div class="milestone-summary"><span>合成玉里程碑</span><b>{claimed}<small>/ {total}</small></b></div>'
        f'<div class="milestone-track" style="--days:{len(progress)}">{"".join(nodes)}{cells}{"".join(labels)}</div>'
        f'<div class="milestone-next">{next_copy}</div></div>'
    )


def _write_temp_html(content: str) -> Path:
    with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as file:
        file.write(content)
        return Path(file.name)


async def draw_attendance_card(view: AttendanceCardView) -> bytes:
    """Screenshot the card through the shared browser renderer."""
    if len(view.roles) > MAX_CARD_ROWS:
        raise ValueError("Too many roles for one image; use the complete text report")
    html_path = _write_temp_html(render_attendance_card_html(view))
    try:
        return await screenshot_web_element(
            html_path.resolve().as_uri(),
            ".ak-card",
            viewport=(CARD_WIDTH, 1),
            timeout_ms=15000,
            max_height=CARD_MAX_HEIGHT,
            device_scale_factor=2.0,
            settle_ms=30,
            wait_for_images=True,
            strict_max_height=True,
            overflow_selectors=(".ak-row", ".ak-name", ".ak-detail", ".ak-chip", ".status", ".attendance-meta",
                                ".milestones", ".milestone", ".attendance-milestones", ".milestone-track",
                                ".milestone-summary", ".milestone-next", ".milestone-node"),
        )
    finally:
        schedule_temp_file_cleanup(html_path, delay_seconds=30)
