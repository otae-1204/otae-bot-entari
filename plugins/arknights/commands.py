"""Command parsing for ``/ak`` (``/明日方舟``).

The parser is pure text handling so it stays testable without an Entari
session.  Selector resolution lives in ``store.py``; this module only decides
*which* operation runs, whether the operation needs a private chat, and how an
unresolved selector is explained.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .store import (
    AMBIGUOUS,
    BULK_NOT_ALLOWED,
    NO_BINDING,
    NOT_FOUND,
    SelectorResolution,
)

ROOT_ALIASES = ("ak", "明日方舟", "arknights")

HELP_ALIASES = {"帮助", "help", "h", "?"}
BIND_ALIASES = {"绑定", "添加账号", "新增账号", "bind", "add-account", "addaccount"}
ACCOUNT_ALIASES = {"账号", "账户", "account", "accounts", "列表", "list"}
PRIMARY_ALIASES = {"主账号", "主账户", "primary", "main"}
UNBIND_ALIASES = {"解绑", "删除账号", "unbind", "remove"}
ATTENDANCE_ALIASES = {"签到", "打卡", "checkin", "check-in", "attendance", "sign"}

ACTION_HELP = "help"
ACTION_BIND = "bind"
ACTION_ACCOUNTS = "accounts"
ACTION_PRIMARY = "primary"
ACTION_UNBIND = "unbind"
ACTION_ATTENDANCE = "attendance"

# Only binding and unbinding carry credentials or phone numbers.
PRIVATE_ONLY_ACTIONS = frozenset({ACTION_BIND, ACTION_UNBIND})
PRIVATE_ONLY_NOTICE = "绑定和解绑会涉及账号凭据或手机号，仅支持私聊使用。"
UNBIND_NEEDS_SELECTOR = (
    "请指定要解绑的角色：/ak 解绑 <编号|昵称|UID后四位>。\n"
    "为避免误删，解绑不会默认选中主账号。"
)

_WORD_SPLIT = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    action: str = ACTION_HELP
    selector: str = ""
    error: str = ""


def parse_command(rest: str) -> ParsedCommand:
    parts = [part for part in _WORD_SPLIT.split(str(rest or "").strip()) if part]
    if not parts:
        return ParsedCommand(ACTION_HELP)

    head = parts[0].casefold()
    if head in HELP_ALIASES:
        return ParsedCommand(ACTION_HELP)
    if head in BIND_ALIASES:
        return ParsedCommand(ACTION_BIND)
    if head in ACCOUNT_ALIASES:
        return ParsedCommand(ACTION_ACCOUNTS)
    if head in PRIMARY_ALIASES:
        return ParsedCommand(ACTION_PRIMARY, selector=" ".join(parts[1:]).strip())
    if head in UNBIND_ALIASES:
        return ParsedCommand(ACTION_UNBIND, selector=" ".join(parts[1:]).strip())
    if head in ATTENDANCE_ALIASES:
        return ParsedCommand(ACTION_ATTENDANCE, selector=" ".join(parts[1:]).strip())
    return ParsedCommand(
        ACTION_HELP,
        error=f"未识别的子命令“{parts[0]}”。\n{format_help()}",
    )


def requires_private_chat(action: str) -> bool:
    return str(action) in PRIVATE_ONLY_ACTIONS


def format_help() -> str:
    return (
        "明日方舟森空岛签到\n"
        "/ak 帮助 — 显示本说明\n"
        "/ak 绑定 — 私聊绑定鹰角账号（Token 或手机号验证码），自动导入全部官服/B服角色\n"
        "/ak 账号 — 查看已绑定角色与编号\n"
        "/ak 主账号 <选择器> — 设置默认角色\n"
        "/ak 解绑 <选择器> — 私聊删除单个角色\n"
        "/ak 签到 [全部|编号|昵称|UID后四位] — 默认签到全部角色\n"
        "/签到 — 统一入口，同时签到终末地与明日方舟的全部已绑定角色\n"
        "选择器只接受编号、完整 UID、UID 后四位（≥4 位）或完整昵称；不会模糊匹配多个账号。\n"
        "账号按 QQ 隔离，登录凭据加密保存；请勿公开 Token 或验证码。"
    )


def selector_candidates_text(
    resolution: SelectorResolution, *, reveal_uid: bool = False
) -> str:
    lines: list[str] = []
    for role in resolution.candidates:
        uid = role.uid if reveal_uid else role.masked_uid
        marker = " [主账号]" if role.is_primary else ""
        lines.append(f"- {role.nickname}{marker} · {role.server_label} · UID {uid}")
    return "\n".join(lines)


def format_selector_failure(
    resolution: SelectorResolution, *, reveal_uid: bool = False
) -> str:
    """Explain an unresolved selector; never silently pick an account."""
    reason = resolution.reason
    selector = resolution.selector
    if reason == NO_BINDING:
        return "尚未绑定明日方舟账号。请私聊使用 /ak 绑定 开始绑定。"
    if reason == BULK_NOT_ALLOWED:
        return "“全部”只能用于 /ak 签到；主账号和解绑都必须指定单个角色。"
    if reason == AMBIGUOUS:
        head = (
            f"选择器“{selector}”匹配到多个角色，为避免误操作已取消。"
            "请使用 /ak 账号 查看完整列表编号，再指定该编号操作。以下为匹配角色："
        )
        return "\n".join((head, selector_candidates_text(resolution, reveal_uid=reveal_uid)))
    if reason == NOT_FOUND:
        if not selector:
            return "未指定角色。请使用 /ak 账号 查看编号、昵称或 UID 后四位。"
        return (
            f"未找到与“{selector}”匹配的角色。"
            "请使用 /ak 账号 查看编号、昵称或 UID 后四位（至少 4 位）。"
        )
    return "未找到对应账号，请使用 /ak 账号 查看编号。"


def format_accounts(roles, *, reveal_uid: bool = False) -> str:
    if not roles:
        return "尚未绑定明日方舟账号。请私聊使用 /ak 绑定 开始绑定。"
    lines = ["已绑定的明日方舟角色："]
    for index, role in enumerate(roles, 1):
        marker = " [主账号]" if role.is_primary else ""
        uid = role.uid if reveal_uid else role.masked_uid
        lines.append(f"{index}. {role.nickname}{marker} · {role.server_label} · UID {uid}")
    lines.append("使用 /ak 主账号 <编号> 设置默认角色，或用 /ak 解绑 <编号> 私聊删除。")
    return "\n".join(lines)
