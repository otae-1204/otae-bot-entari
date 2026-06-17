"""Shared Entari utilities for bot plugins."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from arclet.alconna import Alconna, Arparma, Args, Empty, MultiVar
from arclet.entari import (
    Account,
    AccountUpdate,
    At,
    Image as _Image,
    LoginStatus,
    MessageChain,
    MessageCreatedEvent,
    Quote,
    Session,
    Text,
    command,
    listen,
    scheduler as entari_scheduler,
)
from arclet.entari.command import Match as CommandMatch
from nepattern import AnyString
from satori import ChannelType

from utils import runtime


@dataclass
class ArgVal:
    result: Any = None
    available: bool = False

    def __class_getitem__(cls, _item):
        return cls


@dataclass
class SendDest:
    id: str
    parent_id: str = ""
    channel: bool = False
    private: bool = False
    self_id: str = ""
    adapter: str = ""

    def __post_init__(self):
        self.id = str(self.id)
        self.parent_id = str(self.parent_id)
        self.self_id = str(self.self_id)


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

    async def send(self, dest: SendDest | None = None, bot: Account | None = None):
        if dest and bot:
            if dest.private:
                return await bot.protocol.send_private_message(dest.id, self)
            return await bot.protocol.send_message(dest.id, self)
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
                _SESSION_STACK.append(session)
                try:
                    for handler in self._handlers:
                        result = await _call_handler(handler, self, session, account, alc_result)
                        if result is not None:
                            await send(session, result)
                    if self.block:
                        session.stop()
                finally:
                    _SESSION_STACK.pop()

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
        result = self.func(event)
        if _takes_two_args(self.func):
            result = self.func(account, event)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)


class _EventHook:
    def __init__(self, rule: Pred | Callable[..., Any] | None = None, block: bool = True):
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
                _SESSION_STACK.append(session)
                try:
                    result = await _call_event_handler(func, session, account)
                    if result is not None:
                        await send(session, result)
                    if self.block:
                        session.stop()
                finally:
                    _SESSION_STACK.pop()

            _wrapper.__module__ = func.__module__
            listen(MessageCreatedEvent)(_wrapper)
            return func

        return decorator


class _Scheduler:
    def scheduled_job(self, trigger: str, **kwargs):
        seconds = _seconds(trigger, kwargs)

        def decorator(func: Callable[..., Any]):
            async def _job():
                result = func()
                if inspect.isawaitable(result):
                    return await result
                return result

            _job.__module__ = func.__module__
            entari_scheduler.schedule(lambda: timedelta(seconds=seconds))(_job)
            return func

        return decorator

    def add_job(self, func: Callable[..., Any], trigger: str, **kwargs):
        seconds = _seconds(trigger, kwargs)

        async def _job():
            result = func()
            if inspect.isawaitable(result):
                return await result
            return result

        _job.__module__ = _plugin_module(getattr(func, "__module__", __name__))
        entari_scheduler.schedule(lambda: timedelta(seconds=seconds))(_job)
        return _job


_SESSION_STACK: list[Session] = []
timer = _Scheduler()


def _current_session() -> Session:
    if not _SESSION_STACK:
        raise RuntimeError("No active Entari session")
    return _SESSION_STACK[-1]


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


def _seconds(trigger: str, kwargs: dict[str, Any]) -> int:
    if trigger != "interval":
        return 60
    if "seconds" in kwargs:
        return int(kwargs["seconds"])
    if "minutes" in kwargs:
        return int(kwargs["minutes"]) * 60
    if "hours" in kwargs:
        return int(kwargs["hours"]) * 3600
    return 60


def _plugin_module(module: str) -> str:
    parts = module.split(".")
    if len(parts) >= 3 and parts[0] == "plugins":
        return ".".join(parts[:2])
    return module


async def _call_event_handler(func: Callable[..., Any], session: Session, account: Account):
    kwargs: dict[str, Any] = {}
    for name in inspect.signature(func).parameters:
        if name in {"bot", "account"}:
            kwargs[name] = account
        elif name == "event":
            kwargs[name] = session.event
        elif name == "session":
            kwargs[name] = session
        elif name == "dest":
            kwargs[name] = dest_from_event(session.event)
    result = func(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _call_handler(func: Callable[..., Any], matcher: Matcher, session: Session, account: Account, alc_result: Any):
    kwargs: dict[str, Any] = {}
    sig = inspect.signature(func)
    for name, param in sig.parameters.items():
        anno = param.annotation
        if name in {"event"}:
            kwargs[name] = session.event
        elif name in {"bot", "account"}:
            kwargs[name] = account
        elif name in {"matcher"}:
            kwargs[name] = matcher
        elif name in {"dest"}:
            kwargs[name] = dest_from_event(session.event)
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
    return _EventHook(rule=rule, block=block)


def listen_notice(*_, rule=None, block=True, **__) -> _EventHook:
    return _EventHook(rule=rule, block=block)


def on_ready(func: Callable[..., Any]):
    async def _account_update(event: AccountUpdate):
        if event.status in {LoginStatus.OFFLINE, LoginStatus.DISCONNECT}:
            account_id = str(getattr(event.account, "self_id", "") or getattr(event.account, "id", ""))
            runtime.clear_account(account_id or None)
            return None
        if event.status not in {LoginStatus.ONLINE, LoginStatus.CONNECT, LoginStatus.RECONNECT}:
            return None
        runtime.set_account(event.account, event.status)
        result = func(event.account) if _takes_one_arg(func) else func()
        if inspect.isawaitable(result):
            return await result
        return result

    _account_update.__module__ = func.__module__
    listen(AccountUpdate)(_account_update)
    return func


def get_bot() -> Account:
    account = runtime.get_account()
    if account is None:
        raise RuntimeError("No Entari account is ready")
    return account


def account_adapter_name(account: Any) -> str:
    adapter = getattr(account, "adapter", "")
    getter = getattr(adapter, "get_name", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:
            return ""
    if isinstance(adapter, str):
        return adapter
    return str(adapter or "") if adapter else ""


def inject(func: Callable[..., Any]):
    return func


def stop_session() -> None:
    _current_session().stop()


async def prompt(message: str, timeout: int = 60):
    return await _current_session().prompt(message, timeout=timeout)


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


async def private_send(account: Account, user_id: str, message: str | MessageChain | list[Any]):
    msg = MessageChain(message) if isinstance(message, list) else message
    return await account.protocol.send_private_message(str(user_id), msg)


def get_user_id(event: Any) -> str:
    user = getattr(event, "user", None)
    return str(getattr(user, "id", "") or "")


def get_group_id(event: Any) -> str:
    guild = getattr(event, "guild", None)
    if guild and getattr(guild, "id", None):
        return str(guild.id)
    channel = getattr(event, "channel", None)
    if channel and getattr(channel, "type", None) is not ChannelType.DIRECT:
        return str(channel.id)
    return ""


def get_channel_id(event: Any) -> str:
    channel = getattr(event, "channel", None)
    return str(getattr(channel, "id", "") or "")


def is_group(event: Any) -> bool:
    return bool(get_group_id(event))


def dest_from_event(event: Any) -> SendDest:
    group_id = get_group_id(event)
    if group_id:
        return SendDest(get_channel_id(event) or group_id, group_id, True, False)
    return SendDest(get_user_id(event), "", False, True)


def get_plaintext(event: Any) -> str:
    return event_plain_text(event)


def event_user_id(event: Any) -> str:
    user = getattr(event, "user", None)
    return str(getattr(user, "id", "") or "")


def event_chain(event: Any) -> MessageChain:
    content = getattr(event, "content", None)
    if content is not None:
        return MessageChain(content)
    message = getattr(event, "message", None)
    if message is not None:
        return MessageChain(getattr(message, "message", message))
    return MessageChain()


def event_plain_text(event: Any) -> str:
    text_parts: list[str] = []
    for seg in event_chain(event):
        if isinstance(seg, Text):
            text_parts.append(str(getattr(seg, "text", "") or ""))
    if text_parts:
        return "".join(text_parts)
    return str(getattr(event, "content", "") or "")
