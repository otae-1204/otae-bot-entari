"""Normalize quote metadata before Entari checks command prefixes."""

from __future__ import annotations

from arclet.entari import MessageChain, Session
from arclet.entari.command import _commands
from arclet.entari.const import ITEM_MESSAGE_CONTENT
from arclet.entari.message import Reply
from arclet.entari.plugin import get_all_subscribers
from arclet.letoderea import Propagator, Subscriber
from arclet.letoderea.scope import global_propagators
from satori import At, Author, Text, select


def strip_reply_mentions(message: MessageChain, reply: Reply | None, self_id: str) -> MessageChain | None:
    if not reply or not reply.origin:
        return None
    author_id = getattr(reply.origin.user, "id", None)
    if not author_id:
        authors = select(reply.quote, Author)
        author_id = next((author.id for author in authors if author.id), None)
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


class QuotedCommandMentions(Propagator):
    def validate(self, subscriber: Subscriber) -> bool:
        return subscriber.callable_target == _commands.execute

    def compose(self):
        async def normalize(message: MessageChain, session: Session):
            cleaned = strip_reply_mentions(message, session.reply, session.account.self_id)
            if cleaned is not None:
                return {ITEM_MESSAGE_CONTENT: cleaned}

        # MessageJudges checks / and other configured prefixes at priority 60.
        yield normalize, True, 50


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
