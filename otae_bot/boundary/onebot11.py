"""OneBot 11 forward-WebSocket adapter for LLBot, served in-process as Satori.

Subclasses ``satori-python-adapter-onebot11`` 0.6.0 and replaces only what is wrong
against LLBot 8.3.2:

- ``guild.member.get`` / ``guild.member.list`` carry the OneBot ``role``, with the same
  ``Role`` objects the stock event handlers use;
- ``message.list`` accepts ``next: null`` and LLBot's ``{"messages": [...]}`` history, and
  pages by message_seq;
- the login declares every routed API in ``features``;
- group message events are built from the payload, without the stock handler's two
  extra API calls per message;
- message decoding: a quote whose ``get_msg`` fails stays a bare ``<quote id/>`` instead
  of turning the whole event INTERNAL, and merged forwards become standard
  ``<message forward id=resid>``, expanded only inside a quote;
- ``request.group.add`` (someone asks to join) is a ``guild-member-request``, not the
  stock ``guild-member-added``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger
from satori import Api, EventType
from satori.adapters.onebot11 import OneBot11ForwardAdapter, OneBot11ForwardConfig
from satori.adapters.onebot11 import api as stock_api
from satori.adapters.onebot11 import message as stock_message
from satori.adapters.onebot11.events import message as stock_message_events
from satori.adapters.onebot11.events import request as stock_request_events
from satori.adapters.onebot11.events.base import events as stock_events
from satori.adapters.onebot11.utils import (
    GROUP_AVATAR_URL,
    ROLE_MAPPING,
    USER_AVATAR_URL,
)
from satori.element import Author, Element, Message, Quote
from satori.exception import ActionFailed
from satori.model import (
    Channel,
    ChannelType,
    Event,
    Guild,
    Login,
    Member,
    MessageObject,
    PageDequeResult,
    PageResult,
    User,
)
from satori.parser import escape
from satori.server import Request

__all__ = ["OneBot11ForwardConfig", "OneBotBoundaryAdapter", "decode", "decode_elements"]

FORWARD_MAX_DEPTH = 3
FORWARD_MAX_MESSAGES = 200


# ── message decoding ──


@dataclass
class _Expansion:
    """Messages still allowed in one quoted forward tree, nested levels included."""

    left: int = FORWARD_MAX_MESSAGES


async def decode_elements(
    content: list[dict], net: Any, *, expansion: _Expansion | None = None, depth: int = 0
) -> list[Element]:
    """Decode OneBot segments; ``expansion`` is set only inside a quote.

    Top level: quotes are fetched once, forwards stay id-only (groups forward a lot and
    nobody needs the body unless someone quotes it). Inside a quote: forwards expand,
    nested quotes stay bare so a quote chain costs one get_msg, not one per link.
    """
    result: list[Element] = []
    for seg in content:
        data = seg.get("data") or {}
        match seg.get("type"):
            case "reply":
                if expansion is None:
                    result.append(await _quote(str(data.get("id", "")), net))
                else:
                    result.append(Quote(str(data.get("id", ""))))
            case "forward":
                result.append(await _forward(str(data.get("id") or ""), net, expansion, depth))
            case _:
                # Everything else is stock and makes no API call.
                result.extend(await stock_message._decode([seg], net))  # type: ignore[list-item]
    return result


async def decode(content: list[dict], net: Any) -> str:
    return "".join(str(element) for element in await decode_elements(content, net))


async def _quote(message_id: str, net: Any) -> Quote:
    try:
        msg = await net.call_api("get_msg", {"message_id": message_id})
        if not msg:
            raise ValueError("empty get_msg result")
        sender = msg["sender"]
        author = Author(str(sender["user_id"]), sender.get("nickname"), USER_AVATAR_URL.format(uin=sender["user_id"]))
        body = await decode_elements(msg["message"], net, expansion=_Expansion())
    except Exception as exc:  # noqa: BLE001 - a lost quote must not drop the message carrying it
        logger.warning(f"[boundary] quote {message_id} unavailable: {type(exc).__name__}: {exc}")
        return Quote(message_id)
    return Quote(message_id, content=[author, *body])


def _forward_element(resid: str, children: list[Element] | None = None) -> Message:
    # satori renders a True attribute as a bare ``forward``, and its own parser then fails
    # on the valueless attribute (conv_bool(True)), losing the whole event. Spell it out.
    element = Message(resid or None, True, children)
    element._attrs = {"id": resid, "forward": "true"} if resid else {"forward": "true"}
    return element


async def _forward(resid: str, net: Any, expansion: _Expansion | None, depth: int) -> Message:
    bare = _forward_element(resid)
    if expansion is None or depth >= FORWARD_MAX_DEPTH or expansion.left <= 0 or not resid:
        return bare
    try:
        result = await net.call_api("get_forward_msg", {"id": resid})
        nodes = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(nodes, list):
            raise TypeError("no message list")
        children: list[Element] = []
        for node in nodes:
            if expansion.left <= 0:
                break
            expansion.left -= 1
            sender = node.get("sender") or {}
            body = node.get("content")
            if body is None:
                body = node.get("message")
            author = Author(str(sender.get("user_id", "")), sender.get("nickname"))
            decoded = await decode_elements(
                body if isinstance(body, list) else [], net, expansion=expansion, depth=depth + 1
            )
            children.append(Message(content=[author, *decoded]))
    except Exception as exc:  # noqa: BLE001 - degrade to the id, never fail the message
        logger.warning(f"[boundary] forward {resid[:40]} not expanded: {type(exc).__name__}: {exc}")
        return bare
    return _forward_element(resid, children)


async def _decode_for(content: list, net: Any) -> str:
    if isinstance(net, OneBotBoundaryAdapter):
        return await decode(content, net)
    return await stock_message.decode(content, net)


async def _group_message(login: Login, net: Any, raw: dict) -> Event:
    """Group message event built from the payload alone.

    The stock handler calls get_group_info and get_group_member_info for every message
    (about 0.7 s on LLBot 8.3.2) only to read the group name and the sender's card, both
    of which LLBot already puts in the event.
    """
    sender: dict = raw["sender"]
    uin, group = sender["user_id"], raw["group_id"]
    name = raw.get("group_name")
    if name is None:
        name = (await net.call_api("get_group_info", {"group_id": group}) or {}).get("group_name")
    user = User(str(uin), sender.get("nickname"), avatar=USER_AVATAR_URL.format(uin=uin))
    return Event(
        EventType.MESSAGE_CREATED,
        datetime.now(),
        login=login,
        user=user,
        guild=Guild(str(group), name, avatar=GROUP_AVATAR_URL.format(group=group)),
        channel=Channel(str(group), ChannelType.TEXT, name),
        member=Member(user, sender.get("card"), user.avatar, roles=_roles(sender.get("role"))),
        message=MessageObject(str(raw["message_id"]), await _decode_for(raw["message"], net)),
    )


_GROUP_MESSAGE_EVENTS = ("message.group.normal", "message.group.notice", "message_sent.group.normal")


async def _group_message_for(login: Login, net: Any, raw: dict) -> Event | None:
    if isinstance(net, OneBotBoundaryAdapter):
        return await _group_message(login, net, raw)
    return await stock_message_events.group(login, net, raw)


async def _group_add_request(login: Login, net: Any, raw: dict) -> Event:
    """``request.group.add``: a user asks to join a group the bot manages.

    The stock handler shares one function with ``request.group.invite`` and emits
    GUILD_MEMBER_ADDED for anything but an invite, so the request looks like a join that
    already happened and nobody can approve it. ``message.id`` is the OneBot flag that
    ``guild.member.approve`` hands back to ``set_group_add_request``.
    """
    group_id, user_id = str(raw["group_id"]), str(raw["user_id"])
    # Names only decorate the request; losing them must not lose the request.
    try:
        name = (await net.call_api("get_group_info", {"group_id": raw["group_id"]}) or {}).get("group_name")
    except ActionFailed:
        name = None
    try:
        nickname = (await net.call_api("get_stranger_info", {"user_id": raw["user_id"]}) or {}).get("nickname")
    except ActionFailed:
        nickname = None
    user = User(user_id, nickname, avatar=USER_AVATAR_URL.format(uin=user_id))
    return Event(
        EventType.GUILD_MEMBER_REQUEST,
        datetime.fromtimestamp(raw["time"]),
        login,
        user=user,
        member=Member(user, avatar=user.avatar),
        guild=Guild(group_id, name, avatar=GROUP_AVATAR_URL.format(group=group_id)),
        channel=Channel(group_id, ChannelType.TEXT, name),
        message=MessageObject(str(raw["flag"]), escape(str(raw.get("comment") or ""))),
    )


async def _group_add_request_for(login: Login, net: Any, raw: dict) -> Event:
    if isinstance(net, OneBotBoundaryAdapter):
        return await _group_add_request(login, net, raw)
    return await stock_request_events.request_group_invite(login, net, raw)


def _install_handlers() -> None:
    # Stock event handlers and message.get bind ``decode`` at import; the only hook
    # short of copying them is rebinding that name. Other adapters keep stock decoding.
    for module in (stock_api, stock_message_events):
        module.decode = _decode_for  # type: ignore[attr-defined]
    for name in _GROUP_MESSAGE_EVENTS:
        stock_events[name] = _group_message_for
    stock_events["request.group.add"] = _group_add_request_for


# ── adapter ──


def _roles(role: Any) -> list:
    return [ROLE_MAPPING[role]] if role in ROLE_MAPPING else []


def _time(value: Any) -> datetime | None:
    return datetime.fromtimestamp(value) if value else None


class _FeatureLogins(dict):
    """Stamps the real feature list on each login before the stock code posts it.

    The stock ``_login_update`` hardcodes ``features`` inside ``message_handle``; it
    stores the login here right before posting LOGIN_ADDED/UPDATED.
    """

    def __init__(self, features: list[str]):
        super().__init__()
        self.features = features

    def __setitem__(self, key: str, login: Login) -> None:
        login.features = list(self.features)
        super().__setitem__(key, login)


class OneBotBoundaryAdapter(OneBot11ForwardAdapter):
    # No return annotation: entari-plugin-server builds the config model only when
    # ``config`` is the sole annotation of ``__init__``.
    def __init__(self, config: OneBot11ForwardConfig):
        super().__init__(config)
        _install_handlers()
        self._stock_message_list = self.routes[Api.MESSAGE_LIST.value]
        self.route(Api.GUILD_MEMBER_GET)(self._guild_member_get)
        self.route(Api.GUILD_MEMBER_LIST)(self._guild_member_list)
        self.route(Api.MESSAGE_LIST)(self._message_list)
        self.logins = _FeatureLogins(self.features)

    @property
    def features(self) -> list[str]:
        apis = sorted(path for path in self.routes if not path.startswith("internal/"))
        return [*apis, "guild.plain"]

    def _member(self, info: dict) -> Member:
        uin = info["user_id"]
        user = User(str(uin), info.get("nickname"), avatar=USER_AVATAR_URL.format(uin=uin))
        return Member(user, info.get("card"), user.avatar, _time(info.get("join_time")), roles=_roles(info.get("role")))

    async def _guild_member_get(self, request: Request) -> Member:
        params = request.params
        info = await self.call_api(
            "get_group_member_info", {"group_id": int(params["guild_id"]), "user_id": int(params["user_id"])}
        )
        if not info:
            raise RuntimeError(f"Failed to get member {params['user_id']} in group {params['guild_id']}")
        return self._member(info)

    async def _guild_member_list(self, request: Request) -> PageResult[Member]:
        items = await self.call_api("get_group_member_list", {"group_id": int(request.params["guild_id"])})
        return PageResult([self._member(item) for item in items or []])  # type: ignore[union-attr]

    async def _message_list(self, request: Request) -> PageDequeResult[MessageObject]:
        params = request.params
        if params.get("direction", "before") != "before":
            raise RuntimeError("OneBot adapter only supports direction='before'")
        info = await self.call_api("get_version_info", {})
        if info.get("app_name") != "LLOneBot":
            # Other implementations keep the stock route; it cannot read next=None.
            if params.get("next") is None:
                params.pop("next", None)
            return await self._stock_message_list(request)
        channel_id = str(params["channel_id"])
        query: dict[str, Any] = {}
        if params.get("limit"):
            query["count"] = int(params["limit"])
        if params.get("next"):
            query["message_seq"] = int(params["next"])
        if channel_id.startswith("private:"):
            action, query["user_id"] = "get_friend_msg_history", int(channel_id.removeprefix("private:"))
        else:
            action, query["group_id"] = "get_group_msg_history", int(channel_id)
        result = await self.call_api(action, query)
        raws = (result.get("messages") if isinstance(result, dict) else result) or []
        # LLBot's message_seq is inclusive and pages towards older messages.
        seqs = [int(raw["message_seq"]) for raw in raws if raw.get("message_seq") is not None]
        oldest = min(seqs, default=0)
        page = PageDequeResult(
            [await self._message_object(raw, channel_id) for raw in raws],
            next=str(oldest - 1) if oldest > 1 else None,
        )
        if params.get("order") == "desc":
            page.data.reverse()
        return page

    async def _message_object(self, raw: dict, channel_id: str) -> MessageObject:
        sender: dict = raw.get("sender") or {}
        uin = sender.get("user_id", raw.get("user_id"))
        user = User(str(uin), sender.get("nickname"), avatar=USER_AVATAR_URL.format(uin=uin))
        content = await decode(raw.get("message") or [], self)
        created = _time(raw.get("time"))
        if channel_id.startswith("private:"):
            channel = Channel(channel_id, ChannelType.DIRECT, sender.get("nickname"))
            return MessageObject(str(raw["message_id"]), content, channel=channel, user=user, created_at=created)
        member = Member(user, sender.get("card") or sender.get("nickname"), user.avatar, roles=_roles(sender.get("role")))
        return MessageObject(
            str(raw["message_id"]),
            content,
            channel=Channel(channel_id, ChannelType.TEXT, raw.get("group_name")),
            guild=Guild(channel_id, raw.get("group_name"), GROUP_AVATAR_URL.format(group=channel_id)),
            member=member,
            user=user,
            created_at=created,
        )

