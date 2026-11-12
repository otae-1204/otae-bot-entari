"""Shared Entari utilities for bot plugins."""

from __future__ import annotations

import contextvars
import inspect
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from arclet.alconna import Alconna, Arparma, Args, Empty, MultiVar
from arclet.entari import (
    Account,
    At,
    Image as _Image,
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
from nepattern import AnyString

from otae_bot.accounts.ready import (  # noqa: F401 - re-exported until callers migrate
    get_bot,
    get_bot_for_guild,
    get_bots,
    on_ready,
)
from otae_bot.scheduling.jobs import (  # noqa: F401 - re-exported until callers migrate
    _ManagedJob,
    _plugin_module,
    _Scheduler,
    close_scheduled_jobs,
    timer,
)
from otae_bot.messaging import prompts as _prompts
from otae_bot.messaging.forward import make_forward  # noqa: F401 - re-exported until callers migrate
from otae_bot.messaging.forward import send_forward as _send_forward
from otae_bot.messaging.inbound import (  # noqa: F401 - re-exported until callers migrate
    event_chain,
    event_plain_text,
    event_user_id,
    get_channel_id,
    get_group_id,
    get_rest,
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


def inject(func: Callable[..., Any]):
    return _Injected(func)


def stop_session() -> None:
    _current_session().stop()


async def send_forward(nodes, *, name=None, uin=None, session: Session | None = None):
    return await _send_forward(session or _current_session(), nodes, name=name, uin=uin)


async def prompt(message: str, timeout: int = 60):
    return await _prompts.prompt(_current_session(), message, timeout)


async def prompt_silently(message: str, timeout: int = 60):
    return await _prompts.prompt_silently(_current_session(), message, timeout)


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
