"""Per-role Endfield attendance orchestration.

Kept free of Entari imports so the loop can be exercised directly in tests: one
character failing (expired credential, network error, duplicate request, storage
fault) must never stop the remaining characters from being signed in.  Both
``/ef 签到`` and the unified ``/签到`` entry point call :func:`sign_roles` and
then render the returned view with this game's own card.
"""

from __future__ import annotations

from collections.abc import Sequence
from time import time

from loguru import logger

from .account.client import AttendanceResult, EndfieldAPIError
from .account.crypto import CredentialKeyError
from .account.i18n import server_label
from .catalog.models import (
    AttendanceCardView,
    AttendanceMilestoneView,
    AttendanceRewardView,
    AttendanceRoleView,
)
from .gacha.service import (
    ROLE_TASKS,
    RoleTaskRegistry,
    TaskAlreadyRunning,
    format_timestamp,
)

FAILURE_RETRY_MESSAGE = "签到失败，请稍后重试"


def build_attendance_view(role, result: AttendanceResult) -> AttendanceRoleView:
    """Convert one role plus its API result into a display-only view.

    The UID stays masked here: the same card is rendered in group chats and in
    private chats, and this game never reveals the full role id in an image.
    """
    return AttendanceRoleView(
        nickname=role.nickname,
        uid=role.masked_uid,
        server_name=server_label(role.server_name or role.server_id),
        status=result.status,
        message=result.message,
        rewards=[
            AttendanceRewardView(item.name, item.count, item.icon_url)
            for item in result.rewards
        ],
        monthly_count=result.monthly_count,
        calendar_days=result.calendar_days,
        milestones=[AttendanceMilestoneView(item.day, item.reward.count, item.reward.icon_url)
                    for item in result.milestones],
    )


async def sign_one(
    store,
    client,
    cipher,
    role,
    *,
    registry: RoleTaskRegistry | None = None,
) -> AttendanceRoleView:
    """Sign one stored role in and always return a view (never raise).

    Every failure mode — key/decryption problems, a locked database, a duplicate
    in-flight request, an API error — is contained here so one broken character
    cannot abort the rest of the batch.  ``CancelledError`` is a ``BaseException``
    and therefore still propagates, which releases the claim below.
    """
    active = registry if registry is not None else ROLE_TASKS
    try:
        token = store.decrypt_token(role, cipher)
    except CredentialKeyError as exc:
        return build_attendance_view(role, AttendanceResult("failed", str(exc)))
    except LookupError as exc:
        return build_attendance_view(role, AttendanceResult("failed", str(exc)))
    except Exception as exc:  # noqa: BLE001 - a storage fault must not abort the batch
        logger.error(
            f"[endfield-account] credential read failed: stored_role={getattr(role, 'id', '?')} "
            f"error_type={type(exc).__name__}"
        )
        return build_attendance_view(role, AttendanceResult("failed", FAILURE_RETRY_MESSAGE))

    try:
        async with active.claim(role):
            result = await client.attendance(token, role)
    except TaskAlreadyRunning as exc:
        return build_attendance_view(role, AttendanceResult("failed", str(exc)))
    except EndfieldAPIError as exc:
        return build_attendance_view(role, AttendanceResult("failed", str(exc)))
    except Exception as exc:  # noqa: BLE001 - one role must not abort the batch
        logger.error(
            f"[endfield-account] attendance failed: stored_role={getattr(role, 'id', '?')} "
            f"error_type={type(exc).__name__}"
        )
        return build_attendance_view(role, AttendanceResult("failed", FAILURE_RETRY_MESSAGE))
    return build_attendance_view(role, result)


async def sign_roles(
    store,
    client,
    cipher,
    roles: Sequence,
    *,
    registry: RoleTaskRegistry | None = None,
    generated_at: str | None = None,
) -> AttendanceCardView:
    """Sign every given role and collect the display views in order."""
    views: list[AttendanceRoleView] = []
    for role in roles:
        views.append(await sign_one(store, client, cipher, role, registry=registry))
    return AttendanceCardView(
        roles=views,
        generated_at=generated_at if generated_at is not None else format_timestamp(int(time())),
    )


def format_attendance_report(view: AttendanceCardView) -> str:
    """Full plain-text result used when the card cannot be rendered.

    UIDs are already masked in the view, so the group and private chat forms are
    identical; no extra masking decision is needed here.
    """
    lines = [f"终末地森空岛签到结果（{len(view.roles)} 个角色）"]
    if not view.roles:
        lines.append("没有可签到的角色。")
    for index, role in enumerate(view.roles, 1):
        lines.append(f"{index}. {role.nickname} · {role.server_name} · UID {role.uid}")
        if role.status == "success":
            rewards = "、".join(f"{item.name} × {item.count}" for item in role.rewards)
            lines.append(f"   签到成功{f'：{rewards}' if rewards else '（无奖励明细）'}")
        elif role.status == "already":
            lines.append("   今日已签到，无需重复签到")
        else:
            reason = (role.message or "").strip() or FAILURE_RETRY_MESSAGE
            lines.append(f"   签到失败：{reason}")
        if role.status in {"success", "already"} and role.monthly_count is not None:
            lines.append(f"   本月累签 {role.monthly_count} 天")
    if view.generated_at:
        lines.append(f"生成时间：{view.generated_at}")
    return "\n".join(lines)


__all__ = [
    "FAILURE_RETRY_MESSAGE",
    "build_attendance_view",
    "format_attendance_report",
    "sign_one",
    "sign_roles",
]
