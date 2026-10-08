"""Per-role attendance orchestration.

Kept free of Entari imports so the loop can be exercised directly in tests:
one character failing (expired credential, network error, duplicate request)
must never stop the remaining characters from being signed in.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from loguru import logger

from .client import ArknightsAPIError, AttendanceResult
from .crypto import CredentialKeyError
from .models import (
    AttendanceCardView,
    AttendanceRoleView,
    build_attendance_view,
    format_local_timestamp,
)
from .tasks import ROLE_TASKS, RoleTaskRegistry, TaskAlreadyRunning

FAILURE_RETRY_MESSAGE = "签到失败，请稍后重试"


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
    and therefore still propagates.
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
            f"[arknights] credential read failed: stored_role={getattr(role, 'id', '?')} "
            f"error_type={type(exc).__name__}"
        )
        return build_attendance_view(role, AttendanceResult("failed", FAILURE_RETRY_MESSAGE))

    try:
        async with active.claim(role):
            result = await client.attendance(token, role)
    except TaskAlreadyRunning as exc:
        return build_attendance_view(role, AttendanceResult("failed", str(exc)))
    except ArknightsAPIError as exc:
        return build_attendance_view(role, AttendanceResult("failed", str(exc)))
    except Exception as exc:  # noqa: BLE001 - one role must not abort the batch
        logger.error(
            f"[arknights] attendance failed: stored_role={getattr(role, 'id', '?')} "
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
    views: list[AttendanceRoleView] = []
    for role in roles:
        views.append(await sign_one(store, client, cipher, role, registry=registry))
    return AttendanceCardView(
        roles=tuple(views),
        generated_at=generated_at if generated_at is not None else format_local_timestamp(),
    )


__all__: Iterable[str] = ("sign_one", "sign_roles")
