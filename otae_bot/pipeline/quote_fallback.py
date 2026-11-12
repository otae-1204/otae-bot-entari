"""Dispatch message events even when the quoted message cannot be fetched.

Entari 0.19.4 ``MessageEvent.gather`` builds the reply while assembling the event
context: a quote without inline children is looked up with
``account.protocol.message_get(channel, quote.id)``. If that raises (LLBot answers a
quoted merged forward with ``500 ServerException: 消息为空``; the boundary adapter emits
a bare ``<quote id/>`` when it could not fetch the quote itself), the gather fails,
no subscriber runs and the event is lost.

Both pieces below use public hooks only:

* ``guard_message_get`` listens to Entari's ``APIRequest`` (posted by
  ``EntariProtocol`` before every non-static API method). For ``message_get`` it makes
  the call itself and answers with an empty ``UnavailableMessage`` when it fails, so
  the gather goes on. It calls the class's own ``message_get``: ``EntariProtocol``
  wraps the instance's methods only, so this does not post ``APIRequest`` again.
* ``drop_unavailable_reply`` is a shared context supplier (it runs after the event's
  gather and after Entari's session supplier). It removes the reply built from that
  placeholder, which is what Entari does when there is no quote at all: no
  ``ITEM_MESSAGE_REPLY``, ``session.reply`` is ``None``, ``is_reply_me`` stays False.
  ``event.quote`` is untouched, so plugins still see the quoted element and its id.
"""

from __future__ import annotations

import re
from typing import Any

from arclet.entari.const import ITEM_MESSAGE_REPLY, ITEM_SESSION
from arclet.entari.event.api import APIRequest
from arclet.letoderea import Contexts
from loguru import logger
from satori import MessageObject

_BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_installed = False


class UnavailableMessage(MessageObject):
    """Placeholder ``message_get`` result for a message the platform did not return."""


def _safe_message(error: BaseException, account: Any) -> str:
    try:
        text = " ".join(str(error).split())
    except Exception:  # noqa: BLE001 - a broken __str__ must not hide the original failure
        return "<unprintable>"
    token = getattr(getattr(account, "config", None), "token", None)
    if isinstance(token, str) and token:
        text = text.replace(token, "<redacted>")
    return _BEARER.sub("Bearer <redacted>", text)[:300]


async def guard_message_get(event: APIRequest):
    if event.name != "message_get":
        return None
    protocol = event.account.protocol
    try:
        return await type(protocol).message_get(protocol, **event.params)
    except Exception as error:  # noqa: BLE001 - any failure: the event must still be dispatched
        logger.warning(
            "[entari] quoted message fetch failed, dispatching without reply: channel={} quote={} error={}: {}",
            event.params.get("channel_id"),
            event.params.get("message_id"),
            type(error).__name__,
            _safe_message(error, event.account),
        )
        return UnavailableMessage.from_elements(str(event.params.get("message_id") or ""), [])


async def drop_unavailable_reply(context: Contexts) -> None:
    reply = context.get(ITEM_MESSAGE_REPLY)
    if reply is None or not isinstance(getattr(reply, "origin", None), UnavailableMessage):
        return
    del context[ITEM_MESSAGE_REPLY]
    session = context.get(ITEM_SESSION)
    if session is not None and session.reply is reply:
        session.reply = None


def install_quote_fetch_fallback() -> None:
    """Register both hooks once (root scope, so plugin reloads keep them)."""
    global _installed
    if _installed:
        return
    from arclet.entari import listen
    from arclet.letoderea import add_shared_supplier

    listen(APIRequest, priority=0)(guard_message_get)
    add_shared_supplier(drop_unavailable_reply)
    _installed = True
