"""Entari command routing for ``/ak``: Arknights Skland binding and attendance.

Registration happens at import time (one command, one Cleanup listener); the
database and HTTP client are created lazily on first use so importing the
plugin never touches ``data/`` or opens a socket.
"""

from __future__ import annotations

import re
import sys
import tempfile
from typing import Any

from arclet.alconna import Alconna, Args, MultiVar
from arclet.entari import Cleanup, Event, listen
from arclet.letoderea.exceptions import _ExitException
from loguru import logger
from nepattern import AnyString

from otae_bot.adapters.entari import (
    ArgVal,
    ChainMsg,
    event_user_id,
    is_group,
    make_image,
    on_alconna,
    prompt,
)
from otae_bot.attendance_registry import (
    AttendanceCapability,
    AttendanceResult,
    register_attendance_capability,
)
from otae_bot.infrastructure.rendering.temp_files import schedule_temp_file_cleanup

from .attendance import sign_roles
from .client import ArknightsAPIError, ArknightsClient, extract_account_token
from .commands import (
    ACTION_ACCOUNTS,
    ACTION_ATTENDANCE,
    ACTION_BIND,
    ACTION_PRIMARY,
    ACTION_UNBIND,
    PRIVATE_ONLY_NOTICE,
    ROOT_ALIASES,
    UNBIND_NEEDS_SELECTOR,
    format_accounts,
    format_help,
    format_selector_failure,
    parse_command,
    requires_private_chat,
)
from .crypto import ArknightsCipher, CredentialKeyError
from .models import AttendanceCardView, format_attendance_report
from .rendering.cards import draw_attendance_card
from .store import ArknightsStore

CANCEL_WORDS = {"取消", "cancel", "q", "quit"}
PHONE_PATTERN = re.compile(r"1\d{10}")
CODE_PATTERN = re.compile(r"\d{4,8}")

BIND_METHOD_PROMPT = (
    "请选择绑定方式：\n1. Token 绑定\n2. 手机号验证码绑定\n"
    "回复 1 或 2；回复“取消”退出。"
)
TOKEN_GUIDE = (
    "请在浏览器登录森空岛后打开：\nhttps://web-api.skland.com/account/info/hg\n"
    "页面会返回形如 {\"code\":0,\"data\":{\"content\":\"……\"},\"msg\":\"……\"} 的内容。\n"
    "请只复制 data.content 双引号内的 Token，不要复制整段 JSON，也不要复制这里的示例。\n"
    "绑定的角色会取自该鹰角账号下全部明日方舟官服与 B服 角色。\n"
    "请勿在群聊或其他平台公开该内容。"
)
TOKEN_PROMPT = "请发送 data.content 双引号内的 Token；回复“取消”退出。"
PHONE_PROMPT = "请输入用于鹰角账号登录的手机号；回复“取消”退出。"
CODE_PROMPT = "验证码已发送，请输入短信验证码；回复“取消”退出。"
CANCELLED_TEXT = "绑定已取消或等待超时。"
PRIMARY_NEEDS_SELECTOR = "请指定要设为主账号的角色：/ak 主账号 <编号|昵称|UID后四位>。"

_arknights_alconna = on_alconna(
    Alconna(list(ROOT_ALIASES), Args["rest;?", MultiVar(AnyString)]),
    priority=5,
    block=True,
)

_store: ArknightsStore | None = None
_client: ArknightsClient | None = None


def _store_instance() -> ArknightsStore:
    global _store
    if _store is None:
        _store = ArknightsStore()
    return _store


def _client_instance() -> ArknightsClient:
    global _client
    if _client is None:
        _client = ArknightsClient()
    return _client


def _rest(match: Any) -> str:
    if not getattr(match, "available", False):
        return ""
    value = getattr(match, "result", "")
    if isinstance(value, tuple):
        return " ".join(str(item) for item in value).strip()
    return str(value).strip() if value is not None else ""


async def _prompt_text(message: str, *, timeout: int) -> str | None:
    answer = await prompt(message, timeout=timeout)
    if answer is None:
        return None
    text = answer.extract_plain_text() if hasattr(answer, "extract_plain_text") else str(answer or "")
    text = text.strip()
    if not text or text.casefold() in CANCEL_WORDS:
        return None
    return text


@_arknights_alconna.handle()
async def handle_arknights(event: Event, rest: ArgVal) -> None:
    await _dispatch(_arknights_alconna, event, parse_command(_rest(rest)))


async def _dispatch(matcher, event: Event, command) -> None:
    try:
        if command.error:
            return await matcher.finish(command.error)
        handled = {
            ACTION_BIND,
            ACTION_ACCOUNTS,
            ACTION_PRIMARY,
            ACTION_UNBIND,
            ACTION_ATTENDANCE,
        }
        if command.action not in handled:
            return await matcher.finish(format_help())

        user_id = str(event_user_id(event))
        group = is_group(event)
        if requires_private_chat(command.action) and group:
            return await matcher.finish(PRIVATE_ONLY_NOTICE)

        if command.action == ACTION_BIND:
            return await _handle_bind(matcher, user_id)
        if command.action == ACTION_ACCOUNTS:
            return await _handle_accounts(matcher, user_id, reveal_uid=not group)
        if command.action == ACTION_PRIMARY:
            return await _handle_primary(matcher, user_id, command.selector, reveal_uid=not group)
        if command.action == ACTION_UNBIND:
            return await _handle_unbind(matcher, user_id, command.selector, reveal_uid=not group)
        return await _handle_attendance(matcher, user_id, command.selector, group=group)
    except CredentialKeyError as exc:
        return await matcher.finish(str(exc))
    except ArknightsAPIError as exc:
        logger.warning(
            f"[arknights] api request failed operation={exc.operation} code={exc.code or 'unknown'}"
        )
        return await matcher.finish(str(exc))
    except _ExitException:
        raise
    except Exception as exc:  # noqa: BLE001 - report instead of crashing the bot
        logger.error(
            f"[arknights] command failed error_type={type(exc).__module__}.{type(exc).__name__}"
        )
        return await matcher.finish("明日方舟森空岛功能暂时不可用，请稍后重试。")


# ------------------------------------------------------------------ binding


async def _handle_bind(matcher, user_id: str) -> None:
    # Resolve the key before prompting: a missing key must not start a dialog.
    cipher = ArknightsCipher.from_env()
    method = await _prompt_text(BIND_METHOD_PROMPT, timeout=90)
    if method is None:
        return await matcher.finish(CANCELLED_TEXT)
    normalized = method.casefold()
    if normalized in {"1", "token", "t"}:
        account_token = await _bind_token(matcher)
    elif normalized in {"2", "短信", "手机", "sms", "phone"}:
        account_token = await _bind_phone(matcher)
    else:
        return await matcher.finish("未识别绑定方式，绑定已取消。")
    if not account_token:
        return None

    roles = await _client_instance().discover_roles(account_token)
    if not roles:
        return await matcher.finish(
            "该鹰角账号下未找到明日方舟角色（支持官服与 B服；请确认已在森空岛绑定角色）。"
        )
    store = _store_instance()
    previous = {(role.uid, role.game_id) for role in store.list_roles(user_id)}
    bound = store.bind_roles(user_id, account_token, roles, cipher)
    selected = {(role.uid, role.game_id) for role in roles}
    added = len(selected - previous)
    updated = len(selected) - added
    summary = f"绑定完成：新增 {added} 个角色"
    if updated:
        summary += f"，更新 {updated} 个角色"
    summary += f"；当前共 {len(bound)} 个角色。"
    lines = [summary]
    lines.extend(
        f"- {role.nickname} · {role.channel_name or f'渠道 {role.game_id}'} · UID {role.uid}"
        for role in roles
    )
    lines.append("可在私聊使用 /ak 签到 立即签到，或 /ak 账号 查看编号。")
    return await matcher.finish("\n".join(lines))


async def _bind_token(matcher) -> str | None:
    await matcher.send(TOKEN_GUIDE)
    raw = await _prompt_text(TOKEN_PROMPT, timeout=150)
    if raw is None:
        await matcher.finish(CANCELLED_TEXT)
        return None
    token = extract_account_token(raw)
    if not token:
        await matcher.finish("未识别到有效 Token，绑定已取消。")
        return None
    return token


async def _bind_phone(matcher) -> str | None:
    phone = await _prompt_text(PHONE_PROMPT, timeout=90)
    if phone is None:
        await matcher.finish(CANCELLED_TEXT)
        return None
    if not PHONE_PATTERN.fullmatch(phone):
        await matcher.finish("手机号格式不正确，绑定已取消。")
        return None
    await _client_instance().send_phone_code(phone)
    code = await _prompt_text(CODE_PROMPT, timeout=120)
    if code is None:
        await matcher.finish(CANCELLED_TEXT)
        return None
    if not CODE_PATTERN.fullmatch(code):
        await matcher.finish("验证码格式不正确，绑定已取消。")
        return None
    return await _client_instance().token_by_phone_code(phone, code)


# ------------------------------------------------------------- account admin


async def _handle_accounts(matcher, user_id: str, *, reveal_uid: bool) -> None:
    roles = _store_instance().list_roles(user_id)
    return await matcher.finish(format_accounts(roles, reveal_uid=reveal_uid))


async def _handle_primary(matcher, user_id: str, selector: str, *, reveal_uid: bool) -> None:
    if not selector.strip():
        return await matcher.finish(PRIMARY_NEEDS_SELECTOR)
    store = _store_instance()
    resolution = store.set_primary(user_id, selector)
    if not resolution.resolved or resolution.role is None:
        return await matcher.finish(format_selector_failure(resolution, reveal_uid=reveal_uid))
    role = resolution.role
    return await matcher.finish(
        f"已将 {role.nickname}（{role.server_label} · UID {role.masked_uid}）设为主账号。"
    )


async def _handle_unbind(matcher, user_id: str, selector: str, *, reveal_uid: bool) -> None:
    if not selector.strip():
        return await matcher.finish(UNBIND_NEEDS_SELECTOR)
    store = _store_instance()
    resolution = store.unbind(user_id, selector)
    if not resolution.resolved or resolution.role is None:
        return await matcher.finish(format_selector_failure(resolution, reveal_uid=reveal_uid))
    role = resolution.role
    return await matcher.finish(
        f"已解绑 {role.nickname}（{role.server_label} · UID {role.masked_uid}）。"
        "如该凭据已无其他角色引用，加密凭据已同步清理。"
    )


# ---------------------------------------------------------------- attendance


async def _handle_attendance(matcher, user_id: str, selector: str, *, group: bool) -> None:
    cipher = ArknightsCipher.from_env()
    store = _store_instance()
    roles, resolution = store.resolve_roles(user_id, selector)
    if not roles:
        return await matcher.finish(format_selector_failure(resolution, reveal_uid=not group))
    view = await sign_roles(store, _client_instance(), cipher, roles)
    await _finish_attendance_view(matcher, view, group=group)


async def _finish_attendance_view(matcher, view: AttendanceCardView, *, group: bool) -> None:
    """Send the card, or the full text result when the renderer is unavailable."""
    png = await _attendance_png(view)
    if png is None:
        return await matcher.finish(format_attendance_report(view, reveal_uid=not group))
    return await _finish_png(matcher, png)


async def _attendance_png(view: AttendanceCardView) -> bytes | None:
    """Render the card, or return ``None`` so the caller sends full text."""
    try:
        return await draw_attendance_card(view)
    except _ExitException:
        raise
    except Exception as exc:  # noqa: BLE001 - the text result must still be delivered
        logger.warning(
            f"[arknights] attendance card render failed error_type={type(exc).__name__}"
        )
        return None


async def _finish_png(matcher, png: bytes) -> None:
    return await matcher.finish(ChainMsg([_png_image(png)]))


def _png_image(png: bytes):
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as file:
        file.write(png)
        file.flush()
        schedule_temp_file_cleanup(file.name)
        return make_image(path=file.name)


# ------------------------------------------------- unified /签到 registration


def _signin_roles(user_id: str) -> list:
    """Every role this user bound, without touching credentials."""
    return _store_instance().list_roles(user_id)


async def _signin_attendance(user_id: str, *, group: bool) -> AttendanceResult:
    """Run the whole Arknights sign-in for the unified ``/签到`` entry point."""
    store = _store_instance()
    roles = store.list_roles(user_id)
    if not roles:
        return AttendanceResult(ok=False, text="尚未绑定明日方舟账号，请私聊使用 /ak 绑定。")
    try:
        # Resolve the key only once the user actually has roles here, so an
        # unbound user never sees a credential configuration error.
        cipher = ArknightsCipher.from_env()
    except CredentialKeyError as exc:
        return AttendanceResult(ok=False, text=str(exc))
    view = await sign_roles(store, _client_instance(), cipher, roles)
    return AttendanceResult(
        png=await _attendance_png(view),
        text=format_attendance_report(view, reveal_uid=not group),
    )


register_attendance_capability(
    AttendanceCapability(
        game="arknights",
        owner=__name__,
        module=sys.modules[__name__],
        roles=_signin_roles,
        sign=_signin_attendance,
    )
)


@listen(Cleanup)
async def _close_arknights_state() -> None:
    """Release the SQLite connection and the owned HTTP client on shutdown."""
    global _store, _client
    store, _store = _store, None
    client, _client = _client, None
    if store is not None:
        store.close()
    if client is not None:
        await client.close()
