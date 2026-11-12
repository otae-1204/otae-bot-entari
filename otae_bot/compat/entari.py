"""Shared Entari utilities for bot plugins."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
from collections.abc import Iterable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

import aiohttp
from arclet.alconna import Alconna, Arparma, Args, Empty, MultiVar
from arclet.entari import (
    Account,
    AccountUpdate,
    At,
    Author,
    Image as _Image,
    LoginStatus,
    Message as _MessageElement,
    MessageChain,
    MessageCreatedEvent,
    Quote,
    Session,
    Text,
    command,
    listen,
)
from arclet.entari.command import Match as CommandMatch
from arclet.entari.event.base import NoticeEvent
from arclet.letoderea import defer, step_out
from loguru import logger
from nepattern import AnyString
from satori import ChannelType

from otae_bot.accounts import runtime
from otae_bot.scheduling.jobs import (  # noqa: F401 - re-exported until callers migrate
    _ManagedJob,
    _plugin_module,
    _Scheduler,
    close_scheduled_jobs,
    timer,
)
from otae_bot.messaging.inbound import (  # noqa: F401 - re-exported until callers migrate
    event_chain,
    event_plain_text,
    event_user_id,
    get_channel_id,
    get_group_id,
    is_group,
)


@dataclass
class ArgVal:
    result: Any = None
    available: bool = False

    def __class_getitem__(cls, _item):
        return cls


@dataclass(frozen=True)
class _Injected:
    provider: Callable[..., Any]


def make_image(*, path: str | Path | None = None, url: str | None = None, raw: bytes | BytesIO | None = None, **kwargs):
    if path:
        return _Image.of(path=Path(path))
    if url:
        return _Image(src=url)
    if raw is not None:
        return _Image.of(raw=raw)
    return _Image(**kwargs)


Image = make_image
Reply = Quote
ReplySeg = Quote


def make_forward(
    nodes: Iterable[Any],
    *,
    name: str | None = None,
    uin: str | int | None = None,
) -> _MessageElement:
    """Build a Satori merged-forward element (``<message forward>``).

    Each node becomes one child ``<message>``; a node may be a single element, a
    string, or a sequence of elements.  ``name``/``uin`` are emitted as an
    ``<author>`` inside every node.  Callers pass one author for the whole
    forward; per-node senders are not relied on.  This is the only way plugins
    send a merged forward; there is no protocol-specific fallback.
    """
    children: list[_MessageElement] = []
    for node in nodes:
        if isinstance(node, str):
            content: list[Any] = [Text(node)]
        elif isinstance(node, (list, tuple, MessageChain)):
            content = [Text(item) if isinstance(item, str) else item for item in node]
        else:
            content = [node]
        if uin:
            content.insert(0, Author(str(uin), name or None))
        children.append(_MessageElement(content=content))
    return _MessageElement(forward=True, content=children)


async def send_forward(
    nodes: Iterable[Any],
    *,
    name: str | None = None,
    uin: str | int | None = None,
    session: Session | None = None,
):
    """Send one merged-forward message through the current Satori session."""
    target = session or _current_session()
    return await target.send(MessageChain([make_forward(nodes, name=name, uin=uin)]))


class ChainMsg(MessageChain):
    @classmethod
    def text(cls, text: str) -> "ChainMsg":
        return cls([Text(str(text))])

    @classmethod
    async def generate(cls, *_, event=None, message=None, **__) -> "ChainMsg":
        if message is not None:
            return cls(message)
        if event is not None:
            msg = getattr(event, "message", None)
            if msg is not None:
                return cls(getattr(msg, "message", msg))
            content = getattr(event, "content", None)
            if content is not None:
                return cls(content)
        return cls()

    async def send(self):
        """Reply in the current session; proactive messages use otae_bot.delivery.deliver."""
        return await _current_session().send(self)

    async def finish(self):
        await self.send()
        _current_session().stop()


class Matcher:
    def __init__(self, alconna: Alconna, *, block: bool = True):
        self.alconna = alconna
        self.block = block
        self._handlers: list[Callable[..., Any]] = []

    def handle(self):
        def decorator(func: Callable[..., Any]):
            self._handlers.append(func)

            async def _wrapper(session: Session, account: Account, alc_result: Any):
                session_token = _CURRENT_SESSION.set(session)
                try:
                    for handler in self._handlers:
                        result = await _call_handler(handler, self, session, account, alc_result)
                        if result is not None:
                            await send(session, result)
                    if self.block:
                        session.stop()
                finally:
                    _CURRENT_SESSION.reset(session_token)

            _wrapper.__module__ = func.__module__
            command.on(self.alconna)(_wrapper)
            return func

        return decorator

    async def send(self, message: Any = None, *_, **__):
        if message is not None:
            await send(_current_session(), message)

    async def finish(self, message: Any = None, *_, **__):
        if message is not None:
            await send(_current_session(), message)
        _current_session().stop()


class Pred:
    def __init__(self, func: Callable[..., Any]):
        self.func = func

    async def __call__(self, account: Account, event: Any) -> bool:
        if _takes_two_args(self.func):
            result = self.func(account, event)
        else:
            result = self.func(event)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)


class _EventHook:
    def __init__(
        self,
        event_type: type = MessageCreatedEvent,
        rule: Pred | Callable[..., Any] | None = None,
        block: bool = True,
    ):
        self.event_type = event_type
        self.rule = rule
        self.block = block

    def handle(self):
        def decorator(func: Callable[..., Any]):
            async def _wrapper(session: Session, account: Account):
                event = session.event
                if self.rule:
                    predicate = self.rule if isinstance(self.rule, Pred) else Pred(self.rule)
                    if not await predicate(account, event):
                        return
                session_token = _CURRENT_SESSION.set(session)
                try:
                    result = await _call_event_handler(func, session, account)
                    if result is not None:
                        await send(session, result)
                    if self.block:
                        session.stop()
                finally:
                    _CURRENT_SESSION.reset(session_token)

            _wrapper.__module__ = func.__module__
            listen(self.event_type)(_wrapper)
            return func

        return decorator

    async def send(self, message: Any = None, *_, **__):
        if message is not None:
            await send(_current_session(), message)

    async def finish(self, message: Any = None, *_, **__):
        if message is not None:
            await send(_current_session(), message)
        _current_session().stop()


_CURRENT_SESSION: contextvars.ContextVar[Session | None] = contextvars.ContextVar(
    "entari_current_session",
    default=None,
)


def _current_session() -> Session:
    session = _CURRENT_SESSION.get()
    if session is None:
        raise RuntimeError("No active Entari session")
    return session


def _takes_two_args(func: Callable[..., Any]) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    positional = [
        p
        for p in params.values()
        if p.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    return len(positional) >= 2


async def _call_event_handler(func: Callable[..., Any], session: Session, account: Account):
    kwargs: dict[str, Any] = {}
    for name in inspect.signature(func).parameters:
        if name in {"bot", "account"}:
            kwargs[name] = account
        elif name == "event":
            kwargs[name] = session.event
        elif name == "session":
            kwargs[name] = session
    result = func(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _call_handler(func: Callable[..., Any], matcher: Matcher, session: Session, account: Account, alc_result: Any):
    kwargs: dict[str, Any] = {}
    sig = inspect.signature(func)
    for name, param in sig.parameters.items():
        anno = param.annotation
        if isinstance(param.default, _Injected):
            kwargs[name] = await _call_event_handler(
                param.default.provider, session, account
            )
        elif name in {"event"}:
            kwargs[name] = session.event
        elif name in {"bot", "account"}:
            kwargs[name] = account
        elif name in {"matcher"}:
            kwargs[name] = matcher
        elif name in {"session"}:
            kwargs[name] = session
        elif anno is ArgVal or name in {"rest", "content", "target_match", "steam_id", "nickname", "group_id", "peek_http_path"}:
            kwargs[name] = _get_arg(alc_result, name)
        elif anno is CommandMatch:
            kwargs[name] = _get_arg(alc_result, name)
    result = func(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _unwrap_alconna_result(alc_result: Any) -> Arparma | Any:
    inner = getattr(alc_result, "result", None)
    if inner is not None and hasattr(inner, "all_matched_args"):
        return inner
    return alc_result


def _get_arg(alc_result: Any, name: str) -> ArgVal:
    arparma = _unwrap_alconna_result(alc_result)
    value = getattr(arparma, "all_matched_args", {}).get(name, Empty)
    return ArgVal(None if value is Empty else value, value is not Empty)


def on_alconna(alconna: Alconna, **kwargs) -> Matcher:
    return Matcher(alconna, block=kwargs.get("block", True))


def listen_message(*_, rule=None, block=True, **__) -> _EventHook:
    return _EventHook(MessageCreatedEvent, rule=rule, block=block)


def listen_notice(*_, rule=None, block=True, **__) -> _EventHook:
    return _EventHook(NoticeEvent, rule=rule, block=block)


def on_ready(func: Callable[..., Any]):
    async def _account_update(event: AccountUpdate):
        if event.status in {LoginStatus.OFFLINE, LoginStatus.DISCONNECT}:
            account_id = str(getattr(event.account, "self_id", "") or getattr(event.account, "id", ""))
            runtime.clear_account(account_id or None)
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
    """The default account: the most recent login that is still online."""
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


def inject(func: Callable[..., Any]):
    return _Injected(func)


def stop_session() -> None:
    _current_session().stop()


async def prompt(message: str, timeout: int = 60):
    session = _current_session()
    started = perf_counter()
    try:
        return await session.prompt(message, timeout=timeout)
    except aiohttp.ClientConnectionError:
        if perf_counter() - started >= 5:
            raise
        logger.warning("[entari] prompt message connection interrupted; retrying once")
        await asyncio.sleep(0.25)
        return await session.prompt(message, timeout=timeout)


async def prompt_silently(message: str, timeout: int = 60):
    session = _current_session()
    sent_messages = await session.send(message)
    message_ids = {
        str(item.id)
        for item in sent_messages or ()
        if getattr(item, "id", None) is not None
    }

    async def waiter(content: MessageChain, candidate_session: Session):
        quote_id = str(getattr(getattr(candidate_session.event, "quote", None), "id", "") or "")
        if not quote_id or quote_id not in message_ids:
            return None
        if session.event.user and session.event.user.id == candidate_session.event.user.id:
            if candidate_session.event.channel.type == ChannelType.DIRECT:
                return content
            if session.event.channel and candidate_session.event.channel.id == session.event.channel.id:
                return content

    waiter.__annotations__ = {"content": MessageChain, "candidate_session": Session}
    step = step_out(MessageCreatedEvent, waiter, block=False, priority=15)
    defer(step.dispose)
    return await step.wait(timeout=timeout)


def _takes_one_arg(func: Callable[..., Any]) -> bool:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return bool(params)


def cmd(name: str, aliases: set[str] | None = None, **kwargs):
    names = [name.lstrip("/")]
    if aliases:
        names.extend(str(alias).lstrip("/") for alias in aliases)
    return on_alconna(Alconna(names, Args["rest;?", MultiVar(AnyString)]), **kwargs)


def cmd_with_args(name: str, args: Args, aliases: set[str] | None = None, **kwargs):
    names = [name.lstrip("/")]
    if aliases:
        names.extend(str(alias).lstrip("/") for alias in aliases)
    return on_alconna(Alconna(names, args), **kwargs)


def get_rest(rest_match: Match | Any) -> str:
    if not getattr(rest_match, "available", False):
        return ""
    val = getattr(rest_match, "result", "")
    if isinstance(val, tuple):
        return " ".join(str(item) for item in val).strip()
    return str(val).strip() if val is not None else ""


def image_from_path(path: str | Path):
    return make_image(path=Path(path))


def image_from_raw(raw: bytes | BytesIO):
    return make_image(raw=raw)


async def send(session: Session, message: str | MessageChain | list[Any] | tuple[Any, ...]):
    if isinstance(message, MessageChain):
        return await session.send(message)
    if isinstance(message, (list, tuple)):
        return await session.send(MessageChain(message))
    return await session.send(str(message))


async def finish(session: Session, message: str | MessageChain | list[Any] | None = None):
    if message is not None:
        await send(session, message)
    session.stop()


def get_user_id(event: Any) -> str:
    user = getattr(event, "user", None)
    return str(getattr(user, "id", "") or "")


def get_plaintext(event: Any) -> str:
    return event_plain_text(event)
