"""Runtime account registry for Entari scheduled/background tasks.

Every online account is kept by (platform, self_id): the same user id on two
platforms is two accounts. ``get_account()`` is the most recent login that is
still online, on any platform; background senders pick by group or by the
target's platform instead (``otae_bot.delivery``). Which groups each account is in
comes from Satori ``guild.list``; it is fetched on login, refreshed by callers
when older than ``GUILD_REFRESH_SECONDS`` and again after a failed send.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from time import monotonic
from typing import Any

from arclet.entari import Account
from loguru import logger

GUILD_REFRESH_SECONDS = 15 * 60
GUILD_LIST_TIMEOUT_SECONDS = 15
# A forced guild.list after a failed send, at most this often per account.
GUILD_RETRY_REFRESH_SECONDS = 60

AccountKey = tuple[str, str]

_accounts: dict[AccountKey, Account] = {}
_status: Any = None
_updated_at: datetime | None = None
_guilds: dict[AccountKey, frozenset[str]] = {}
_guilds_at: dict[AccountKey, float] = {}
_guild_locks: dict[AccountKey, asyncio.Lock] = {}
_background: set[asyncio.Task] = set()


def account_id(account: Any) -> str:
    """The account's user id, for logs and Satori targets; not unique across platforms."""
    return str(getattr(account, "self_id", "") or getattr(account, "id", "") or "")


def account_key(account: Any) -> AccountKey:
    """What identifies an account here: its platform and its user id."""
    return str(getattr(account, "platform", "") or ""), account_id(account)


def set_account(account: Account, status: Any = None) -> None:
    global _status, _updated_at
    key = account_key(account)
    # Re-inserting moves it last: the latest login is the default account.
    _accounts.pop(key, None)
    _accounts[key] = account
    _status = status
    _updated_at = datetime.now(timezone.utc)


def clear_account(account: Any = None) -> None:
    """Forget one account that went offline (by its platform and id), or every account."""
    global _status, _updated_at
    key = None if account is None else account_key(account)
    for table in (_accounts, _guilds, _guilds_at, _guild_locks):
        if key is None:
            table.clear()
        else:
            table.pop(key, None)
    _status = None
    _updated_at = datetime.now(timezone.utc)


def get_account() -> Account | None:
    return next(reversed(_accounts.values()), None)


def get_bots() -> list[Account]:
    """Every online account, the default one first."""
    return list(reversed(_accounts.values()))


def get_status() -> Any:
    return _status


def get_updated_at() -> datetime | None:
    return _updated_at


def is_online() -> bool:
    return bool(_accounts)


def known_guilds(account: Any) -> frozenset[str] | None:
    """The groups this account is in, or None before guild.list has answered."""
    return _guilds.get(account_key(account))


def get_bots_for_guild(guild_id: str) -> list[Account]:
    """Online accounts known to be in the group, the default one first."""
    return [bot for bot in get_bots() if str(guild_id) in (known_guilds(bot) or ())]


def get_bot_for_guild(guild_id: str) -> Account | None:
    bots = get_bots_for_guild(guild_id)
    return bots[0] if bots else None


def forget_guild(account: Any, guild_id: str) -> None:
    """The account turned out not to be in the group; skip it until the next listing."""
    key = account_key(account)
    if key in _guilds:
        _guilds[key] = _guilds[key] - {str(guild_id)}


async def refresh_guilds(account: Any = None, *, max_age: float = 0.0) -> None:
    """List groups for one account or every online one; skip listings newer than max_age."""
    targets = [account] if account is not None else get_bots()
    await asyncio.gather(*(_refresh_guilds(target, max_age) for target in targets))


def schedule_guild_refresh(account: Any) -> None:
    """Fetch the account's groups in the background, e.g. right after login."""
    task = asyncio.get_running_loop().create_task(refresh_guilds(account, max_age=GUILD_RETRY_REFRESH_SECONDS))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _refresh_guilds(account: Any, max_age: float) -> None:
    key = account_key(account)
    name = f"{key[0]}:{key[1]}"
    requested = monotonic()
    lock = _guild_locks.setdefault(key, asyncio.Lock())
    async with lock:
        # A listing that finished while this call waited for the lock is fresh enough.
        listed_at = _guilds_at.get(key)
        if listed_at is not None and (listed_at >= requested or requested - listed_at < max_age):
            return
        try:
            guilds = await asyncio.wait_for(_list_guilds(account), GUILD_LIST_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - Keep the previous listing; retry after max_age.
            logger.warning(f"[runtime] guild.list failed for account {name}: {type(exc).__name__}: {exc}")
            guilds = None
        if key not in _accounts:
            return  # Went offline meanwhile.
        _guilds_at[key] = monotonic()
        if guilds is not None:
            _guilds[key] = guilds
            logger.info(f"[runtime] account {name} is in {len(guilds)} groups")


async def _list_guilds(account: Any) -> frozenset[str]:
    # Every page; GUILD_LIST_TIMEOUT_SECONDS bounds an adapter that never stops paging.
    return frozenset([str(guild.id) async for guild in account.protocol.guild_list() if getattr(guild, "id", None)])
