"""React to accounts coming online, and pick an online account for background work."""

from __future__ import annotations

import inspect
from typing import Any, Callable

from arclet.entari import Account, AccountUpdate, LoginStatus, listen

from otae_bot.accounts import runtime


def on_ready(func: Callable[..., Any]):
    async def _account_update(event: AccountUpdate):
        if event.status in {LoginStatus.OFFLINE, LoginStatus.DISCONNECT}:
            # An account without an id cannot be told apart: forget them all, as before.
            runtime.clear_account(event.account if runtime.account_id(event.account) else None)
            return None
        if event.status not in {LoginStatus.ONLINE, LoginStatus.CONNECT, LoginStatus.RECONNECT}:
            return None
        runtime.set_account(event.account, event.status)
        # Several plugins register on_ready; the registry lists each login's groups once.
        runtime.schedule_guild_refresh(event.account)
        result = func(event.account) if _takes_one_arg(func) else func()
        if inspect.isawaitable(result):
            return await result
        return result

    _account_update.__module__ = func.__module__
    listen(AccountUpdate)(_account_update)
    return func


def get_bot() -> Account:
    """The most recent login that is still online, on any platform.

    With several platforms online (QQ through LLBot, the Ir panel) this is not
    a sender for any particular chat: replies use the session's account,
    proactive messages ``otae_bot.delivery.deliver`` (by group, or by the
    target's platform), lookups for a command's group ``session.account``.
    Only "some online account" callers remain: Tibo's cycle account (a
    preference ``deliver`` checks against the group) and bilibili's group
    ordering (members of the group first).
    """
    account = runtime.get_account()
    if account is None:
        raise RuntimeError("No Entari account is ready")
    return account


def get_bots() -> list[Account]:
    """Every online account, the default one first."""
    return runtime.get_bots()


def get_bot_for_guild(guild_id: str) -> Account | None:
    """An online account known to be in the group (the default one if it is), else None."""
    return runtime.get_bot_for_guild(guild_id)


def _takes_one_arg(func: Callable[..., Any]) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return bool(params)
