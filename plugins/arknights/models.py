"""Render-facing view models for the Arknights attendance card.

This module is pure data: it never registers events, never touches the network
and never imports the renderer, so the card can be rebuilt from a plain result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .client import AttendanceResult

STATUS_LABELS = {
    "success": "签到成功",
    "already": "今日已签到",
    "failed": "签到失败",
}
STATUS_ORDER = ("success", "already", "failed")


@dataclass(frozen=True, slots=True)
class AttendanceRewardView:
    name: str
    count: int


@dataclass(frozen=True, slots=True)
class AttendanceMilestoneView:
    """One official cumulative sign-in slot (see ``client.AttendanceMilestone``).

    ``done`` wins over ``available`` when labelling the state: a claimed slot is
    "已领取" even if the server also still marks it claimable.
    """

    day: int
    name: str
    count: int
    done: bool = False
    available: bool = False

    @property
    def state_label(self) -> str:
        if self.done:
            return "已领取"
        if self.available:
            return "可领取"
        return "未达成"


@dataclass(frozen=True, slots=True)
class AttendanceRoleView:
    nickname: str
    uid: str
    full_uid: str = ""
    channel_name: str = ""
    status: str = "success"
    message: str = ""
    rewards: tuple[AttendanceRewardView, ...] = ()
    # Days already signed in the server's current month; ``None`` when the
    # server did not report a usable calendar, so the card can hide the row
    # instead of showing a fabricated 0.
    monthly_count: int | None = None
    # Official cumulative 合成玉 slots, already filtered by the client.
    milestones: tuple[AttendanceMilestoneView, ...] = ()
    # Actual daily flags; neither calendar length nor a signed prefix is inferred.
    daily_progress: tuple[bool, ...] = ()

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, "签到失败")

    @property
    def server_label(self) -> str:
        return self.channel_name or "未知渠道"

    def rewards_text(self) -> str:
        if not self.rewards:
            return ""
        return "，".join(f"{item.name} × {item.count}" for item in self.rewards)


@dataclass(frozen=True, slots=True)
class AttendanceCardView:
    roles: tuple[AttendanceRoleView, ...] = ()
    generated_at: str = ""
    subtitle: str = "森空岛 · 明日方舟每日签到"

    def counts(self) -> dict[str, int]:
        counts = {status: 0 for status in STATUS_ORDER}
        for role in self.roles:
            status = role.status if role.status in counts else "failed"
            counts[status] += 1
        counts["total"] = len(self.roles)
        return counts

    def summary_text(self) -> str:
        counts = self.counts()
        return (
            f"共 {counts['total']} 个角色：成功 {counts['success']}，"
            f"已签到 {counts['already']}，失败 {counts['failed']}"
        )


def monthly_count_value(value: Any) -> int | None:
    """Normalize a monthly total to ``int | None``.

    Only integers from 0 through 31 are accepted; other values become ``None`` so the
    card can hide an unknown total rather than claim ``0`` days.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 31 else None


def milestone_views(value: Any) -> tuple[AttendanceMilestoneView, ...]:
    """Normalize the client's milestone tuple for display.

    Items that are not usable (missing day/name, non-positive count) are
    dropped; the client already excluded malformed server nodes.
    """
    if not value:
        return ()
    views: list[AttendanceMilestoneView] = []
    for item in value:
        try:
            day = int(getattr(item, "day", 0))
            count = int(getattr(item, "count", 0))
        except (TypeError, ValueError):
            continue
        name = str(getattr(item, "name", "") or "")
        if day <= 0 or count <= 0 or not name:
            continue
        views.append(
            AttendanceMilestoneView(
                day=day,
                name=name,
                count=count,
                done=bool(getattr(item, "done", False)),
                available=bool(getattr(item, "available", False)),
            )
        )
    return tuple(views)


def daily_progress_value(value: Any) -> tuple[bool, ...]:
    if not isinstance(value, (tuple, list)) or not 1 <= len(value) <= 31:
        return ()
    return tuple(value) if all(isinstance(done, bool) for done in value) else ()


def build_attendance_view(role: Any, result: AttendanceResult) -> AttendanceRoleView:
    """Convert one role plus its API result into a display-only view."""
    status = str(getattr(result, "status", "") or "failed")
    if status not in STATUS_LABELS:
        status = "failed"
    return AttendanceRoleView(
        nickname=str(getattr(role, "nickname", "") or "未命名角色"),
        uid=str(getattr(role, "masked_uid", "") or "****----"),
        full_uid=str(getattr(role, "uid", "") or ""),
        channel_name=str(getattr(role, "channel_name", "") or getattr(role, "server_label", "") or "未知渠道"),
        status=status,
        message=str(getattr(result, "message", "") or ""),
        rewards=tuple(
            AttendanceRewardView(name=str(item.name), count=int(item.count))
            for item in (getattr(result, "rewards", ()) or ())
        ),
        # ``getattr`` keeps fakes/results that predate the field working.
        monthly_count=monthly_count_value(getattr(result, "monthly_count", None)),
        milestones=milestone_views(getattr(result, "milestones", ())),
        daily_progress=daily_progress_value(getattr(result, "daily_progress", ())),
    )


def format_attendance_report(view: AttendanceCardView, *, reveal_uid: bool = False) -> str:
    """Full plain-text result used when the card cannot be rendered.

    ``reveal_uid`` stays ``False`` for group chats so only masked UIDs leave the
    bot there.
    """
    lines = [f"明日方舟森空岛签到结果（{view.summary_text()}）"]
    if not view.roles:
        lines.append("没有可签到的角色。")
    for index, role in enumerate(view.roles, 1):
        uid = role.full_uid if reveal_uid and role.full_uid else role.uid
        lines.append(f"{index}. {role.nickname} · {role.server_label} · UID {uid}")
        if role.status == "success":
            rewards = role.rewards_text()
            lines.append(f"   签到成功{f'：{rewards}' if rewards else '（无奖励明细）'}")
        elif role.status == "already":
            lines.append("   今日已签到，无需重复签到")
        else:
            reason = role.message.strip() or "签到失败，请稍后重试"
            lines.append(f"   签到失败：{reason}")
        if role.status in {"success", "already"}:
            if role.monthly_count is not None:
                lines.append(f"   本月累签 {role.monthly_count} 天")
            for milestone in role.milestones:
                lines.append(
                    f"   累计第 {milestone.day} 天：{milestone.name} × {milestone.count}"
                    f"（{milestone.state_label}）"
                )
    if view.generated_at:
        lines.append(f"生成时间：{view.generated_at}")
    return "\n".join(lines)


def format_local_timestamp(epoch: float | None = None) -> str:
    shanghai = timezone(timedelta(hours=8))
    moment = datetime.fromtimestamp(float(epoch), tz=shanghai) if epoch is not None else datetime.now(shanghai)
    return moment.strftime("%Y-%m-%d %H:%M")
