"""Attribute events whose ``login`` is partial to the account that login belongs to.

The Satori spec (``protocol/events.md``, "事件分为登录事件与非登录事件") says the ``login`` of a
non-login event carries only ``sn``, ``user`` and ``platform``; login events carry the full
login. satori-python 1.3.10+ keys accounts by ``Login.identifier``
(``"{platform}:{adapter}_{user.id}#{sn}"``), so an account registered from READY
(``adapter`` present, e.g. ``iridium`` / ``llonebot``) never matches the event's login,
whose ``adapter`` falls back to the model default ``satori``: ``App.post`` logs
"Received event for unknown account" and drops every event of a spec-conforming server
(Iridium, LLBot's native Satori). Up to 1.3.7 the key was ``platform``, ``user.id`` and the
connection, which is why the same servers worked there.

``EventLoginEntari.post`` (the public ``App.post``, overridden) finds that account among
the ones registered *on the same connection*: same platform, user id and ``sn``; failing
that, same platform and user id when exactly one account has them. The event then carries
that account's login, so the stock ``post`` delivers it unchanged. Nothing is guessed across
connections, and events that already match, or that match nothing, are left to the stock
behaviour.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable

from arclet.entari import Entari
from satori import EventType
from satori.model import Event, Login

# A login-added event is how an account gets registered: never redirected.
_RESOLVED_LOGIN_EVENTS = (EventType.LOGIN_UPDATED, EventType.LOGIN_REMOVED)


def matching_login(login: Login, registered: Iterable[Login]) -> Login | None:
    """The registered login ``login`` (possibly partial) stands for, if it is unambiguous."""
    user = login.user
    if user is None:
        return None
    same_user = [item for item in registered if item.platform == login.platform and item.user and item.user.id == user.id]
    same_sn = [item for item in same_user if item.sn == login.sn]
    if len(same_sn) == 1:
        return same_sn[0]
    if not same_sn and len(same_user) == 1:
        return same_user[0]
    return None


def resolved_login(event: Event, account_logins: Iterable[Login]) -> Login | None:
    """The login ``event`` should carry so ``App.post`` finds its account, or None to leave it.

    For an event whose login matches no registered account as is. Non-login events take the
    account's full login (status and features included: the spec leaves them out of such
    events). Login-updated/-removed events keep what they report and only take the identity
    (``adapter``, ``sn``) and, when they report none, the features.
    """
    login = event.login
    if event.type == EventType.LOGIN_ADDED:
        return None
    found = matching_login(login, account_logins)
    if found is None:
        return None
    if event.type in _RESOLVED_LOGIN_EVENTS:
        return replace(login, adapter=found.adapter, sn=found.sn, features=list(login.features or found.features))
    return replace(found, features=list(found.features))


class EventLoginEntari(Entari):
    """Entari whose ``post`` attributes partial event logins to the connection's account."""

    async def post(self, event: Event, conn: Any):
        # Without a user the stock post warns and drops it; nothing to match on either.
        if event.login.user is not None and event.login.identifier not in self.accounts:
            login = resolved_login(event, (account.self_info for account in conn.accounts.values()))
            if login is not None:
                event.login = login
        await super().post(event, conn)
