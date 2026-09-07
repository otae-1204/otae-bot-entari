"""Authorize only the current sender in the current account's group."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from loguru import logger

from otae_bot.config.settings import Config
from otae_bot.group_features import GroupScope, scope_from_event

ADMIN_ROLES = {"admin", "administrator", "owner", "group_admin", "group_owner", "管理员", "群主"}


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def member_is_admin(member) -> bool:
    roles = _field(member, "roles", ()) or ()
    if isinstance(roles, (str, Mapping)):
        roles = (roles,)
    for role in roles:
        # Prefer stable role IDs; a display name containing 'admin' is not a role.
        value = role if isinstance(role, str) else _field(role, "id", "")
        if str(value).strip().casefold() in ADMIN_ROLES:
            return True
    return False


async def can_manage(account, event, scope: GroupScope) -> bool:
    if scope is None or scope != scope_from_event(account, event):
        return False
    user_id = str(_field(_field(event, "user"), "id", "") or "")
    configured = Config.SUPERUSERS
    superusers = configured if isinstance(configured, (list, tuple, set)) else (configured,)
    if user_id and user_id in {str(value) for value in superusers}:
        return True
    if not user_id:
        return False
    if member_is_admin(_field(event, "member")):
        return True
    try:
        member = await asyncio.wait_for(
            account.guild_member_get(guild_id=scope.group_id, user_id=user_id), 5,
        )
        member_user = _field(_field(member, "user"), "id")
        if (member_user is None or str(member_user) == user_id) and member_is_admin(member):
            return True
    except Exception as exc:  # noqa: BLE001 - adapter failures must deny access
        logger.debug("[group_manager] member lookup failed error_type={}", type(exc).__name__)
    # Some QQ bridges omit roles from Satori members. Query the same account's
    # OneBot action; never fall back to a different configured HTTP endpoint.
    if scope.platform.casefold() not in {"qq", "onebot"}:
        return False
    try:
        result = await asyncio.wait_for(account.internal(
            action="get_group_member_info", group_id=int(scope.group_id),
            user_id=int(user_id), no_cache=True,
        ), 5)
        if not isinstance(result, Mapping) or result.get("status") == "failed":
            return False
        if result.get("retcode", 0) != 0:
            return False
        data = result.get("data", result)
        return (
            str(_field(data, "user_id", "")) == user_id
            and str(_field(data, "group_id", "")) == scope.group_id
            and _field(data, "role") in {"admin", "owner"}
        )
    except Exception as exc:  # noqa: BLE001 - adapter failures must deny access
        logger.debug("[group_manager] permission lookup failed error_type={}", type(exc).__name__)
        return False
