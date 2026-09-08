from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from arclet.entari import MessageChain
from arclet.entari.config import EntariConfig
from arclet.entari.const import ITEM_MESSAGE_CONTENT, ITEM_MESSAGE_REPLY
from arclet.entari.message import Reply
from satori import At, Author, ChannelType, Image, MessageObject, Quote, Text, User

from otae_bot.adapters import command_input
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


class InlineQuoteAuthorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(EntariConfig, "instance", SimpleNamespace(basic=SimpleNamespace(prefix=["/"], nickname=None)), create=True))
        quote = Quote("quoted", content=[Text("原引用正文"), Image(src="https://example.com/inline.png")])
        self.reply = Reply(quote, MessageObject.from_elements(quote.id, quote.children))
        self.lookup = AsyncMock(return_value=MessageObject("quoted", "另一个时间读取的内容", user=User("other")))
        self.session = SimpleNamespace(reply=self.reply, account=SimpleNamespace(self_id="bot", protocol=SimpleNamespace(message_get=self.lookup)),
                                       channel=SimpleNamespace(id="100", type=ChannelType.TEXT))
        self.message = MessageChain([At("other"), Text(" /q 解释"), Image(src="https://example.com/input.png")])

    async def test_missing_inline_author_is_fetched_without_replacing_quoted_content(self):
        before = str(self.message)
        updates = await command_input.normalize_quoted_command(self.message, self.session)
        self.lookup.assert_awaited_once_with("100", "quoted")
        self.assertEqual(str(updates[ITEM_MESSAGE_CONTENT]), '/q 解释<img src="https://example.com/input.png"/>')
        self.assertIs(updates[ITEM_MESSAGE_CONTENT][1], self.message[2])
        self.assertIs(updates[ITEM_MESSAGE_REPLY], self.session.reply)
        self.assertEqual(self.session.reply.origin.user.id, "other")
        self.assertEqual(self.session.reply.origin.content, self.reply.origin.content)
        self.assertIn("inline.png", self.session.reply.origin.content)
        self.assertNotIn("另一个时间", self.session.reply.origin.content)
        self.assertIsNone(self.reply.origin.user)
        self.assertEqual(str(self.message), before)
        self.assertFalse(updates["is_reply_me"])
        self.assertNotIn("is_notice_me", updates)

    async def test_verified_author_mismatch_preserves_intentional_recipient(self):
        self.lookup.return_value = MessageObject("quoted", "", user=User("someone-else"))
        updates = await command_input.normalize_quoted_command(self.message, self.session)
        self.assertNotIn(ITEM_MESSAGE_CONTENT, updates)
        self.assertEqual(self.message[0].id, "other")

    async def test_quoting_bot_restores_reply_flag_without_fabricating_notice(self):
        self.lookup.return_value = MessageObject("quoted", "", user=User("bot"))
        updates = await command_input.normalize_quoted_command(MessageChain("/q 追问"), self.session)
        self.assertTrue(updates["is_reply_me"])
        self.assertNotIn("is_notice_me", updates)

    async def test_complete_authors_no_quote_and_noncommands_do_not_fetch(self):
        for reply, message in (
            (None, self.message),
            (quoted(), self.message),
            (Reply(Quote("quoted", content=[Author("other")]), self.reply.origin), self.message),
            (self.reply, MessageChain([At("other"), Text(" 普通群聊")])),
            (self.reply, MessageChain([At(type="all"), Text(" /q 解释")]))
        ):
            self.session.reply = reply
            await command_input.normalize_quoted_command(message, self.session)
        self.lookup.assert_not_awaited()

    async def test_mismatched_id_or_missing_user_never_authorizes_stripping(self):
        for origin in (MessageObject("wrong-id", "", user=User("other")), MessageObject("quoted", ""), None):
            self.lookup.return_value = origin
            self.assertIsNone(await command_input.normalize_quoted_command(self.message, self.session))
            self.assertIs(self.session.reply, self.reply)

    async def test_lookup_failure_is_redacted_and_preserves_the_original_input(self):
        self.lookup.side_effect = RuntimeError("secret-token raw-message-body")
        with patch.object(command_input.logger, "warning") as warning:
            self.assertIsNone(await command_input.normalize_quoted_command(self.message, self.session))
        self.assertNotIn("secret-token", str(warning.call_args))
        self.assertNotIn("raw-message-body", str(warning.call_args))
        self.assertIn("RuntimeError", str(warning.call_args))
        self.assertIs(self.session.reply, self.reply)

    async def test_author_lookup_timeout_cancels_the_adapter_request(self):
        stopped = asyncio.Event()

        async def wait(*_):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        self.lookup.side_effect = wait
        with patch.object(command_input, "QUOTE_AUTHOR_TIMEOUT", .01):
            self.assertIsNone(await command_input.normalize_quoted_command(self.message, self.session))
        self.assertTrue(stopped.is_set())

    async def test_cancellation_is_not_swallowed(self):
        self.lookup.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await command_input.normalize_quoted_command(self.message, self.session)
