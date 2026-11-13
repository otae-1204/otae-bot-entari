"""The one path for proactive (non-reply) messages.

``deliver`` picks the sending account, applies the group's feature switch,
sends through the standard Satori calls and reports what happened instead of
raising. Replies inside a handler keep using the session.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from arclet.entari import Account, MessageChain
from arclet.letoderea import ExitState
from loguru import logger

from otae_bot.accounts import runtime
from otae_bot.delivery.target import Target
from otae_bot.pipeline.feature_gate import allow_group_delivery

SENT = "sent"
SUPPRESSED = "suppressed"
NO_ACCOUNT = "no_account"
FAILED = "failed"

# LLBot rejects a group send from an account outside that group, for an image
# with "getGroupImageUploadInfo ... msgInfo undefined".
_NOT_IN_GROUP = re.compile(r"msginfo|不在(该|此|这个)?群|not (a )?member|not in (the |this )?group", re.I)


@dataclass
class DeliveryResult:
    status: str
    account: Any = None
    messages: list[Any] = field(default_factory=list)
    exception: BaseException | None = None

    @property
    def sent(self) -> bool:
        return self.status == SENT

    @property
    def not_in_group(self) -> bool:
        """The send failed because the account is not in the target group."""
        return self.exception is not None and is_not_in_group(self.exception)

    def raise_for_failure(self) -> "DeliveryResult":
        """Re-raise the send's exception, for callers built around one."""
        if self.exception is not None:
            raise self.exception
        return self


def is_not_in_group(exc: BaseException) -> bool:
    return bool(_NOT_IN_GROUP.search(f"{type(exc).__name__}: {exc}"))


def _platform(bot: Any) -> str:
    return str(getattr(bot, "platform", "") or "")


def _same_platform(bot: Any, target: Target) -> bool:
    return not target.platform or _platform(bot) == target.platform


def _online(target: Target) -> Account | None:
    for bot in runtime.get_bots():
        if runtime.account_id(bot) == target.self_id and _same_platform(bot, target):
            return bot
    return None


def _group_account(target: Target) -> Account | None:
    preferred = _online(target) if target.self_id else None
    if preferred is not None and target.guild_id in (runtime.known_guilds(preferred) or ()):
        return preferred
    # Only accounts whose guild.list names the group. The platform is not
    # filtered: a stored target may name a platform an account has since moved off.
    return runtime.get_bot_for_guild(target.guild_id)


def private_candidates(target: Target) -> list[Account]:
    """Accounts that may send to a private ``target``, in the order ``deliver`` tries them.

    The named account when it is online; otherwise the most recent login on the
    target's platform, never one on another platform. With the platform unknown
    every online account qualifies, the most recent login first, and ``deliver``
    tries the next one when a send fails (the user may exist on one platform only).
    """
    if target.self_id and (named := _online(target)) is not None:
        return [named]
    bots = runtime.get_bots()
    if target.platform:
        return [bot for bot in bots if _platform(bot) == target.platform][:1]
    return bots


async def resolve_account(target: Target) -> Account | None:
    """The account ``deliver`` would use (first) for ``target`` when none is given."""
    if target.is_private:
        return next(iter(private_candidates(target)), None)
    await runtime.refresh_guilds(max_age=runtime.GUILD_REFRESH_SECONDS)
    bot = _group_account(target)
    if bot is None:
        # Invited since the last listing, or a listing went stale.
        await runtime.refresh_guilds(max_age=runtime.GUILD_RETRY_REFRESH_SECONDS)
        bot = _group_account(target)
    return bot


async def deliver(
    target: Target,
    message: Any,
    *,
    account: Account | None = None,
    plugin: str | None = None,
) -> DeliveryResult:
    """Send ``message`` to ``target``; never raises for a failed send.

    An explicit ``account`` is used as given (callers that pin a sender rely on
    that). ``plugin`` defaults to the plugin of the running subscriber, whose
    per-group switch decides whether a group delivery is suppressed.
    """
    if account is not None:
        candidates = [account]
    elif target.is_private:
        candidates = private_candidates(target)
    else:
        candidates = [bot] if (bot := await resolve_account(target)) is not None else []
    if not candidates:
        where = f"user {target.user_id}" if target.is_private else f"group {target.guild_id}"
        logger.warning(f"[delivery] no online account can send to {where}")
        return DeliveryResult(NO_ACCOUNT)
    chain = message if isinstance(message, MessageChain) else MessageChain(message)
    result = DeliveryResult(NO_ACCOUNT)
    for bot in candidates:
        result = await _send(bot, target, chain, plugin)
        if result.status != FAILED:
            break
        if bot is not candidates[-1]:
            logger.debug(
                f"[delivery] {_platform(bot)}:{runtime.account_id(bot)} could not send to user "
                f"{target.user_id} ({type(result.exception).__name__}); trying the next account"
            )
    return result


async def _send(bot: Account, target: Target, chain: MessageChain, plugin: str | None) -> DeliveryResult:
    if not target.is_private and not allow_group_delivery(bot, target.guild_id, plugin):
        return DeliveryResult(SUPPRESSED, bot)
    try:
        if target.is_private:
            messages = await bot.protocol.send_private_message(target.user_id, chain)
        else:
            messages = await bot.protocol.send_message(target.channel_id or target.guild_id, chain)
    except (ExitState, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001 - reported to the caller in the result
        if not target.is_private and is_not_in_group(exc):
            # Skip this account for the group until its listing is fetched again.
            runtime.forget_guild(bot, target.guild_id)
            runtime.schedule_guild_refresh(bot)
        return DeliveryResult(FAILED, bot, exception=exc)
    return DeliveryResult(SENT, bot, list(messages or []))
