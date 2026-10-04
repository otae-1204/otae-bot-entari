"""Dispatch message events even when Entari cannot fetch the quoted message.

Entari 0.17.4 ``MessageEvent.gather`` (``arclet/entari/event/base.py`` 322-347) builds the
reply while assembling the event context. A quote without inline children is looked up with
``account.protocol.message_get(channel, quote.id)`` (base.py:329). LLBot answers a quoted
merged forward with ``500 ServerException: 消息为空``; the exception leaves the gather,
letoderea's ``dispatch`` never reaches a subscriber, and the event is lost: no command runs
and record_message does not even log it.

The wrapper leaves all of that to the original gather and only guards the lookup. When
``message_get`` fails, the gather runs again with the quote hidden, which is Entari's own
"no quote" path: no ``ITEM_MESSAGE_REPLY``, ``is_reply_me`` False. ``event.quote`` is put
back afterwards, so plugins still get the quoted element and its id.

letoderea's ``Publisher`` copies ``target.gather`` into ``supplier`` when Entari defines the
event types, so replacing the class attribute alone would not reach those publishers; their
suppliers are re-pointed as well.
"""

from __future__ import annotations

import copy
import functools
import inspect
import re
from collections.abc import Callable, Mapping
from typing import Any

from loguru import logger

_MARK = "__otae_quote_fallback__"
# Names the guarded gather has to use. If an Entari upgrade drops one of them, the
# lookup moved and the guard would no longer sit around it.
_EXPECTED_NAMES = frozenset({"quote", "children", "account", "protocol", "message_get", "Reply", "ITEM_MESSAGE_REPLY"})
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")


class QuoteUnavailable(Exception):
    """Raised by the guarded protocol instead of the adapter's own error."""

    def __init__(self, error: Exception):
        super().__init__(error)
        self.error = error


class _GuardedProtocol:
    def __init__(self, protocol: Any):
        self._protocol = protocol

    async def message_get(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return await self._protocol.message_get(*args, **kwargs)
        except Exception as error:
            raise QuoteUnavailable(error) from error

    def __getattr__(self, name: str) -> Any:
        return getattr(self._protocol, name)


class _GuardedAccount:
    """Stands in for ``event.account`` only while the original gather runs."""

    def __init__(self, account: Any):
        self._account = account
        self.protocol = _GuardedProtocol(account.protocol)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._account, name)


def _safe_message(error: BaseException, account: Any) -> str:
    try:
        text = " ".join(str(error).split())
    except Exception:  # noqa: BLE001 - a broken __str__ must not hide the original failure
        return "<unprintable>"
    token = getattr(getattr(account, "config", None), "token", None)
    if isinstance(token, str) and token:
        text = text.replace(token, "<redacted>")
    return _BEARER.sub("Bearer <redacted>", text)[:300]


def _log_failure(event: Any, quote: Any, account: Any, error: Exception) -> None:
    logger.warning(
        "[entari] quoted message fetch failed, dispatching without reply: channel={} quote={} error={}: {}",
        getattr(getattr(event, "channel", None), "id", None),
        getattr(quote, "id", None),
        type(error).__name__,
        _safe_message(error, account),
    )


def _wrap(original: Callable[..., Any], account_key: str, reply_key: str) -> Callable[..., Any]:
    @functools.wraps(original)
    async def gather(self, context):
        quote = getattr(self, "quote", None)
        account = self.account
        if (
            quote is None
            or not getattr(quote, "id", None)
            or getattr(quote, "children", None)
            or getattr(account, "protocol", None) is None
        ):
            return await original(self, context)
        content = copy.copy(self.content)
        self.account = _GuardedAccount(account)
        try:
            return await original(self, context)
        except QuoteUnavailable as failure:
            error = failure.error
        finally:
            self.account = account
            if account_key in context:
                context[account_key] = account
        _log_failure(self, quote, account, error)
        # Start the retry from the state the failed attempt started from.
        context.pop(reply_key, None)
        self.content = content
        self.quote = None
        try:
            return await original(self, context)
        finally:
            self.quote = quote

    setattr(gather, _MARK, True)
    return gather


def _unexpected_structure(gather: Any) -> str | None:
    if gather is None:
        return "is missing"
    if not inspect.iscoroutinefunction(gather):
        return "is not a coroutine function"
    try:
        params = list(inspect.signature(gather).parameters)
    except (TypeError, ValueError):
        return "has no inspectable signature"
    if params != ["self", "context"]:
        return f"takes {params} instead of ['self', 'context']"
    names = set(getattr(getattr(gather, "__code__", None), "co_names", ()))
    if missing := sorted(_EXPECTED_NAMES - names):
        return f"no longer references {missing}"
    return None


def _entari_version() -> str:
    try:
        import arclet.entari as entari
    except ImportError:
        return "?"
    return str(getattr(entari, "__version__", "?"))


def install_quote_fetch_fallback(event_cls: type | None = None, publishers: Mapping[str, Any] | None = None) -> bool:
    """Guard the quote lookup in ``MessageEvent.gather``; return whether the guard is active.

    Safe to call again: an installed wrapper is detected and only publishers that still
    hold the original gather are re-pointed. If Entari's gather no longer looks like the
    one this guard was written for, nothing is patched and a warning is logged.
    """
    try:
        from arclet.entari.const import ITEM_ACCOUNT, ITEM_MESSAGE_REPLY

        if event_cls is None:
            from arclet.entari.event.base import MessageEvent as event_cls
        if publishers is None:
            from arclet.letoderea.publisher import _publishers as publishers
    except ImportError as error:
        logger.warning("[entari] quote fetch fallback not installed: {}", error)
        return False
    if not isinstance(publishers, Mapping):
        logger.warning("[entari] quote fetch fallback not installed: letoderea publisher registry changed")
        return False
    current = event_cls.__dict__.get("gather")
    if getattr(current, _MARK, False):
        original, wrapper = current.__wrapped__, current
    else:
        if problem := _unexpected_structure(current):
            logger.warning(
                "[entari] quote fetch fallback not installed: {}.gather {} (Entari {})",
                event_cls.__name__, problem, _entari_version(),
            )
            return False
        original, wrapper = current, _wrap(current, ITEM_ACCOUNT, ITEM_MESSAGE_REPLY)
        event_cls.gather = wrapper
    for publisher in list(publishers.values()):
        if getattr(publisher, "supplier", None) is original:
            publisher.supplier = wrapper
    if current is not wrapper:
        users = sum(getattr(publisher, "supplier", None) is wrapper for publisher in publishers.values())
        if users:
            logger.info("[entari] quote fetch fallback installed on {} event publishers", users)
        else:
            logger.warning("[entari] quote fetch fallback installed, but no event publisher uses {}.gather", event_cls.__name__)
    return True
