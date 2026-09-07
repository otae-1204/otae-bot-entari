from __future__ import annotations

import unittest
from types import SimpleNamespace

from arclet.entari import MessageChain
from arclet.entari.message import Reply
from satori import At, Author, Image, MessageObject, Quote, Text, User

from otae_bot.adapters.command_input import strip_reply_mentions


def quoted(author="other"):
    return Reply(Quote("quoted"), MessageObject("quoted", "引用正文", user=User(author) if author else None))


class QuotedCommandInputTests(unittest.TestCase):
    def test_automatic_author_mention_and_whitespace_are_removed(self):
        for alias in ("q", "hyw", "何意味"):
            with self.subTest(alias=alias):
                body = Text("/" + alias + " 解释一下")
                message = MessageChain([Text(" \n"), At("other"), Text("\u2005\n"), body])
                original = str(message)
                result = strip_reply_mentions(message, quoted(), "bot")
                self.assertEqual(result, MessageChain([body]))
                self.assertEqual(str(message), original)

    def test_bot_and_quote_author_mentions_can_both_precede_command(self):
        for mentions in ([At("bot"), At("other")], [At("other"), Text(" "), At("bot")]):
            message = MessageChain([*mentions, Text(" /q 解释")])
            self.assertEqual(str(strip_reply_mentions(message, quoted(), "bot")), "/q 解释")

    def test_preserves_images_body_mentions_and_quote_content(self):
        reply = quoted()
        reply.origin.message = [Text("引用正文"), Image(src="https://example.com/quoted.png")]
        body = MessageChain([Text("/q 解释 "), At("someone"), Image(src="https://example.com/current.png")])
        result = strip_reply_mentions(MessageChain([At("other"), *body]), reply, "bot")
        self.assertEqual(result, body)
        self.assertEqual(reply.origin.content, '引用正文<img src="https://example.com/quoted.png"/>')

    def test_inline_quote_author_is_supported_without_message_user(self):
        reply = Reply(Quote("quoted", content=[Author("other"), Text("引用正文")]),
                      MessageObject("quoted", "引用正文"))
        self.assertEqual(str(strip_reply_mentions(MessageChain([At("other"), Text("/q 解释")]), reply, "bot")), "/q 解释")

    def test_unrelated_mentions_missing_quote_and_plain_commands_are_not_rewritten(self):
        for message, reply in (
            (MessageChain([At("other"), Text(" /q 解释")]), None),
            (MessageChain([At("other"), Text(" /q 解释")]), SimpleNamespace(origin=None)),
            (MessageChain([At("other"), Text(" /q 解释")]), quoted(author=None)),
            (MessageChain([At("someone"), Text(" /q 解释")]), quoted()),
            (MessageChain([Text("/q 解释"), At("other")]), quoted()),
            (MessageChain([Text("/q 解释")]), quoted()),
            (MessageChain([At(type="all"), Text(" /q 解释")]), quoted()),
        ):
            with self.subTest(message=message):
                self.assertIsNone(strip_reply_mentions(message, reply, "bot"))

    def test_additional_recipient_still_blocks_prefix_matching(self):
        message = MessageChain([At("other"), Text(" "), At("someone"), Text(" /q 解释")])
        result = strip_reply_mentions(message, quoted(), "bot")
        self.assertIsInstance(result[0], At)
        self.assertEqual(result[0].id, "someone")
