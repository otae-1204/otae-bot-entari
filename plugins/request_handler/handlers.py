"""请求处理插件 - 自动同意好友申请，群邀请交由超级用户审批。"""

import asyncio
from typing import Dict

from arclet.entari import Account as Bot, Event, Session, listen
from arclet.entari.event.base import FriendRequestEvent, GuildMemberRequestEvent, GuildRequestEvent
from loguru import logger

from otae_bot.adapters.entari import (
    ChainMsg,
    Pred,
    event_chain,
    event_plain_text,
    event_user_id,
    is_group,
    listen_message,
)
from otae_bot.config.settings import Config
from otae_bot.delivery import Target, deliver
from plugins.request_handler.ark import parse_ark_invite_segment
from plugins.request_handler.text import entity_label, parse_decision, select_invite_reply, text_or_empty

superuser = str(Config.SUPERUSERS[0]) if Config.SUPERUSERS else ""

# 待审批的群邀请 / 入群申请
# {key: {guild_name, user_name, event_type, request_id, guild_id, self_id}}
_pending: Dict[str, dict] = {}
_lock = asyncio.Lock()


def _account_id(bot) -> str:
    return str(getattr(bot, "self_id", "") or getattr(bot, "id", "") or "")


def _request_id(event) -> str:
    message = getattr(event, "message", None)
    return str(getattr(message, "id", "") or "") or str(getattr(event, "sn", "") or "")


# 好友申请 - 自动同意

async def handle_friend_request(session: Session, account: Bot):
    event = session.event
    try:
        request_id = _request_id(event)
        await account.friend_approve(request_id=request_id, approve=True, comment="")
        user = getattr(event, "user", None)
        logger.info(f"已自动通过好友申请: {getattr(user, 'name', None) or request_id}")
    except Exception as e:
        logger.error(f"好友申请处理失败: {e}")
    session.stop()


listen(FriendRequestEvent, priority=5)(handle_friend_request)


# 群邀请 / 入群申请（Satori request 事件）- 通知超级用户

async def handle_guild_request(session: Session, account: Bot):
    event = session.event
    if not superuser:
        logger.warning("未配置 SUPERUSERS，无法审批群邀请")
        session.stop()
        return

    guild = getattr(event, "guild", None)
    operator = getattr(event, "operator", None)
    user = getattr(event, "user", None)
    member = getattr(event, "member", None)

    guild_id = text_or_empty(getattr(guild, "id", None))
    guild_name = entity_label(guild) or guild_id or "未知群"
    user_name = (
        entity_label(operator)
        or entity_label(user)
        or entity_label(member)
        or "未知用户"
    )
    request_id = _request_id(event)
    self_id = _account_id(account)

    info = {
        "guild_name": guild_name,
        "user_name": user_name,
        "event_type": "member" if isinstance(event, GuildMemberRequestEvent) else "guild",
        "request_id": request_id,
        "guild_id": guild_id,
        "self_id": self_id,
    }
    key = f"{self_id}:{request_id}"

    async with _lock:
        _pending[key] = info

    await _notify_superuser(account, guild_name, user_name, key, guild_id)
    session.stop()


listen(GuildRequestEvent, priority=5)(handle_guild_request)
listen(GuildMemberRequestEvent, priority=5)(handle_guild_request)


# 群邀请（Ark 卡片消息）- 只通知超级用户

async def _is_ark_group_invite(event: Event) -> bool:
    try:
        return any(parse_ark_invite_segment(seg) for seg in event_chain(event))
    except Exception:
        return False


ark_invite = listen_message(rule=Pred(_is_ark_group_invite), priority=5, block=True)


@ark_invite.handle()
async def handle_ark_group_invite(event: Event, bot: Bot):
    if not superuser:
        await ark_invite.finish()
        return

    for seg in event_chain(event):
        info = parse_ark_invite_segment(seg)
        if not info:
            continue

        guild_name = text_or_empty(info.get("group_name")) or (
            f"群 {info['group_code']}" if info.get("group_code") else "未知群"
        )
        inviter_name = text_or_empty(info.get("inviter_uin")) or "未知用户"

        await _notify_ark_invite(bot, guild_name, inviter_name, info)
        break

    await ark_invite.finish()


# 通用：通知超级用户

async def _notify_superuser(bot: Bot, guild_name: str, user_name: str, key: str, guild_id: str = ""):
    try:
        lines = [
            "收到入群邀请：",
            f"群名：{guild_name}",
        ]
        if guild_id:
            lines.append(f"群号：{guild_id}")
        lines.extend([
            f"邀请人：{user_name}",
            "",
            "请回复「同意」加入群聊，或回复「拒绝」忽略此邀请。",
        ])
        (await deliver(Target.private(superuser), ChainMsg.text("\n".join(lines)), account=bot)).raise_for_failure()
        logger.info(f"群邀请已转发给超级用户: {guild_name}")
    except Exception as e:
        logger.error(f"通知超级用户失败: {e}")
        async with _lock:
            _pending.pop(key, None)


async def _notify_ark_invite(bot: Bot, guild_name: str, user_name: str, info: dict):
    """Ark 群卡片不是可审批请求，只通知超级用户手动处理。"""
    try:
        (await deliver(Target.private(superuser), ChainMsg.text(
            f"收到群邀请卡片消息：\n"
            f"群名：{guild_name}\n"
            f"群号：{text_or_empty(info.get('group_code')) or '未知'}\n"
            f"邀请人：{user_name}\n\n"
            f"该卡片不是 QQ 系统的入群请求，无法自动通过；"
            f"请在 QQ 里使用“邀请 bot 加入群聊”的系统入口邀请。"
        ), account=bot)).raise_for_failure()
        logger.info(f"群邀请卡片已转发给超级用户（不可自动审批）: {guild_name}")
    except Exception as e:
        logger.error(f"通知超级用户失败: {e}")


# 超级用户审批回复

def _is_superuser_reply(account: Bot, event: Event) -> bool:
    return select_invite_reply(
        user_id=str(event_user_id(event)),
        superuser_id=superuser,
        private=not is_group(event),
        self_id=_account_id(account),
        text=event_plain_text(event).strip(),
        pending=_pending,
    ) is not None


approve_handler = listen_message(rule=Pred(_is_superuser_reply), priority=4, block=True)


@approve_handler.handle()
async def handle_approve_reply(event: Event, bot: Bot):
    text = event_plain_text(event).strip()
    decision = parse_decision(text)
    if decision is None:
        return

    async with _lock:
        selected = select_invite_reply(
            user_id=str(event_user_id(event)),
            superuser_id=superuser,
            private=not is_group(event),
            self_id=_account_id(bot),
            text=text,
            pending=_pending,
        )
        if selected is None:
            return
        key, info = selected

        try:
            label = text_or_empty(info.get("guild_name"), info.get("guild_id")) or "该群"
            await _decide(bot, key, info, decision)
            await approve_handler.send(f"已{'同意' if decision else '拒绝'}加入 {label}。")
        except Exception as e:
            await approve_handler.send(f"操作执行失败：{e}")
            logger.error(f"群邀请审批失败: {e}")
        finally:
            del _pending[key]

    await approve_handler.finish()


async def _decide(bot: Bot, key: str, info: dict, approve: bool):
    request_id = info.get("request_id") or key
    if info.get("event_type") == "member":
        await bot.guild_member_approve(request_id=request_id, approve=approve, comment="")
    else:
        await bot.guild_approve(request_id=request_id, approve=approve, comment="")
