"""Normalize quote metadata before Entari checks command prefixes."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from arclet.entari import MessageChain, Session
from arclet.entari.command import _commands
from arclet.entari.command.provider import _remove_config_prefix
from arclet.entari.const import ITEM_MESSAGE_CONTENT, ITEM_MESSAGE_REPLY
from arclet.entari.message import Reply
from arclet.entari.plugin import get_all_subscribers
from arclet.letoderea import Propagator, Subscriber
from arclet.letoderea.scope import global_propagators
from loguru import logger
from satori import At, Author, ChannelType, MessageObject, Text, select

QUOTE_AUTHOR_TIMEOUT = 5


def reply_author_id(reply: Reply | None) -> str | None:
    if not reply or not reply.origin:
        return None
    author_id = getattr(reply.origin.user, "id", None)
    if not author_id:
        authors = select(reply.quote, Author)
        author_id = next((author.id for author in authors if author.id), None)
    return author_id


async def resolve_reply_author(message: MessageChain, session: Session) -> Reply | None:
    reply = session.reply
    if not reply or not reply.origin or reply_author_id(reply) or not reply.quote.id or not reply.quote.children:
        return reply
    # LLOneBot can inline quote contents without an Author. Entari then skips
    # message.get, leaving origin.user empty. Query metadata only for commands.
    index = 0
    while index < len(message):
        element = message[index]
        if (isinstance(element, Text) and not element.text.strip()) or (isinstance(element, At) and element.id):
            index += 1
        else:
            break
    candidate = MessageChain(message[index:])
    if not _remove_config_prefix(candidate, session.channel.type == ChannelType.DIRECT):
        return reply
    try:
        origin = await asyncio.wait_for(
            session.account.protocol.message_get(session.channel.id, reply.quote.id), QUOTE_AUTHOR_TIMEOUT,
        )
    except Exception as error:  # noqa: BLE001 - Keep unresolved mentions; never log adapter bodies or credentials.
        logger.warning("[commands] quoted author lookup failed: {}", type(error).__name__)
        return reply
    if not isinstance(origin, MessageObject) or origin.id != reply.quote.id or not getattr(origin.user, "id", None):
        logger.warning("[commands] quoted author lookup returned missing or mismatched metadata")
        return reply
    # Retain the event's quoted text and images, even if message.get now returns
    # different content. Only the verified author metadata is supplemented.
    return Reply(reply.quote, replace(reply.origin, user=origin.user))


def strip_reply_mentions(message: MessageChain, reply: Reply | None, self_id: str) -> MessageChain | None:
    author_id = reply_author_id(reply)
    if not author_id:
        return None
    index = 0
    found_author = False
    while index < len(message):
        element = message[index]
        if isinstance(element, Text) and not element.text.strip():
            index += 1
        elif isinstance(element, At) and element.id in {author_id, self_id}:
            found_author |= element.id == author_id
            index += 1
        else:
            break
    # A mention to somebody else is an intentional recipient, not quote metadata.
    # Keep the original event, quoted content and all mentions in the body intact.
    if not found_author:
        return None
    cleaned = MessageChain(message[index:])
    if cleaned and isinstance(cleaned[0], Text):
        cleaned[0] = Text(cleaned[0].text.lstrip())
    return cleaned


async def normalize_quoted_command(message: MessageChain, session: Session):
    resolved = await resolve_reply_author(message, session)
    updates = {}
    if resolved is not session.reply:
        session.reply = resolved
        updates[ITEM_MESSAGE_REPLY] = resolved
        updates["is_reply_me"] = reply_author_id(resolved) == session.account.self_id
    cleaned = strip_reply_mentions(message, resolved, session.account.self_id)
    if cleaned is not None:
        updates[ITEM_MESSAGE_CONTENT] = cleaned
    return updates or None


class QuotedCommandMentions(Propagator):
    def validate(self, subscriber: Subscriber) -> bool:
        return subscriber.callable_target == _commands.execute

    def compose(self):
        # MessageJudges checks / and other configured prefixes at priority 60.
        yield normalize_quoted_command, True, 50


def install_quoted_command_mentions() -> None:
    normalizer = next((item for item in global_propagators if isinstance(item, QuotedCommandMentions)), None)
    if normalizer is None:
        normalizer = QuotedCommandMentions()
        global_propagators.append(normalizer)
    # Rootless .commands normally loads later during startup. The global
    # propagator also covers its reload; attach to an already loaded one too.
    for subscriber in get_all_subscribers():
        if normalizer.validate(subscriber) and not subscriber.get_propagator(QuotedCommandMentions):
            subscriber.propagate(normalizer)
