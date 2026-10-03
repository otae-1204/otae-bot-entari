from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from plugins.hyw.config import HywConfig, HywError
from plugins.hyw.handlers import handle_hyw, parts_from, scope_for
from plugins.hyw.history import HistoryStore, SourceBook


def _env(values):
    def read(key, default=None):
        return values.get(key, default)
    return read


class ConfigTests(unittest.TestCase):
    def test_direct_proxy_does_not_inherit_global(self):
        values = {"HYW_PROXY": "direct", "HYW_SEARCH_PROXY": "direct", "HYW_API_KEY": "k"}
        with patch("plugins.hyw.config._env", side_effect=_env(values)), \
             patch("plugins.hyw.config.SYSTEM_PROXY", {"https": "http://127.0.0.1:7890"}):
            config = HywConfig.from_env()
        self.assertEqual(config.proxy, "")
        self.assertEqual(config.search_proxy, "")
        self.assertEqual(config.provider, "openai-compatible")

    def test_service_account_uses_google_provider_and_ignores_relay_model_prefix(self):
        values = {
            "HYW_CREDENTIALS_FILE": __file__,
            "HYW_MODEL": "google/gemini-3.8-flash",
            "HYW_BASE_URL": "https://llm.hyw.mom/v1",
            "HYW_VERTEX_LOCATION": "global",
        }
        with patch("plugins.hyw.config._env", side_effect=_env(values)):
            config = HywConfig.from_env()
        self.assertEqual(config.auth_mode, "service_account")
        self.assertEqual(config.provider, "google")
        self.assertEqual(config.model, "gemini-3.8-flash")
        self.assertNotIn("llm.hyw.mom", config.model)

    def test_missing_credentials_file_is_not_configured(self):
        values = {"HYW_CREDENTIALS_FILE": "data/missing-service-account.json"}
        with patch("plugins.hyw.config._env", side_effect=_env(values)):
            config = HywConfig.from_env()
        self.assertEqual(config.auth_mode, "none")
        self.assertFalse(config.configured)
        self.assertIn("找不到", config.auth_error)

    def test_bad_config_source_raises(self):
        with patch("plugins.hyw.config._env", side_effect=_env({"HYW_CONFIG_SOURCE": "nope"})), \
             self.assertRaises(ValueError):
            HywConfig.from_env()


class HistoryTests(unittest.TestCase):
    def test_sources_are_visible_in_channel_and_cleared_per_sender(self):
        book = SourceBook()
        channel = ("qq", "bot", "1", "1")
        book.put(channel, "alice", "m1", [{"title": "A", "url": "https://example.com/a"}])
        book.put(channel, "bob", "m2", [{"title": "B", "url": "https://example.com/b"}])
        self.assertEqual(book.get(channel, "m1")[0]["title"], "A")
        self.assertEqual(book.latest(channel, "alice")[0]["url"], "https://example.com/a")
        book.clear_sender(channel, "alice")
        self.assertEqual(book.latest(channel, "alice"), [])
        self.assertEqual(book.get(channel, "m2")[0]["title"], "B")

    def test_history_drops_image_blocks(self):
        store = HistoryStore()
        scope = ("qq", "bot", "1", "1", "alice")
        store.put(scope, "m", [{
            "role": "user",
            "content": [{"type": "text", "text": "问题"}, {"type": "image", "data": "abc"}],
        }])
        saved = store.get(scope, "m")
        self.assertIn("问题", saved[0]["content"])
        self.assertNotIn("abc", str(saved))


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_does_not_call_the_model(self):
        session = AsyncMock()
        session.reply = None
        session.send = AsyncMock(return_value=[])
        with patch("plugins.hyw.handlers.run_request", AsyncMock()) as run:
            await handle_hyw(session, type("R", (), {"all_matched_args": {"content": []}})())
        run.assert_not_awaited()
        session.send.assert_awaited()

    def test_parts_keep_text_and_images(self):
        from satori import Image, Text

        text, images = parts_from([Text("你好"), Image(src="https://example.com/a.png")])
        self.assertEqual(text, "你好")
        self.assertEqual(images, ["https://example.com/a.png"])

    def test_scope_includes_sender(self):
        session = type("S", (), {})()
        session.account = type("A", (), {"platform": "qq", "self_id": "bot"})()
        session.event = type("E", (), {
            "guild": type("G", (), {"id": "g"})(),
            "channel": type("C", (), {"id": "c"})(),
            "user": type("U", (), {"id": "u"})(),
        })()
        self.assertEqual(scope_for(session), ("qq", "bot", "g", "c", "u"))


class ExpansionTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_and_xml_cards_keep_public_fields_only(self):
        from plugins.hyw.messages import expand_special

        class Element:
            def __init__(self, tag, attrs):
                self.tag = tag
                self.attrs = attrs
                self.children = []

        payload = json.dumps({
            "token": "secret-token",
            "meta": {"title": "夜航", "desc": "一张单曲", "cover": "https://example.com/cover.jpg"},
        })
        xml = '<msg title="分享" url="https://example.com/a"><image src="https://example.com/b.png"/></msg>'
        text, images, rich = await expand_special(None, [
            Element("json", {"data": payload}),
            Element("xml", {"data": xml}),
        ])
        self.assertTrue(rich)
        self.assertIn("夜航", text)
        self.assertIn("一张单曲", text)
        self.assertNotIn("secret-token", text)
        self.assertIn("https://example.com/cover.jpg", images)
        self.assertIn("https://example.com/a", text)
        self.assertIn("https://example.com/b.png", images)

    async def test_forward_uses_only_the_given_id(self):
        from types import SimpleNamespace

        from plugins.hyw.messages import expand_special

        action = AsyncMock(return_value={"messages": [{
            "sender": {"nickname": "甲", "user_id": "1"},
            "time": 0,
            "message": [
                {"type": "text", "data": {"text": "记录正文"}},
                {"type": "image", "data": {"url": "https://example.com/in.png"}},
            ],
        }]})

        class Element:
            tag = "forward"
            attrs = {"id": "fwd-1"}
            children = []

        session = SimpleNamespace(account=SimpleNamespace(self_id="bot"))
        with patch("otae_bot.adapters.onebot.call_account_action", action):
            text, images, rich = await expand_special(session, [Element()])
        action.assert_awaited_once_with(session.account, "get_forward_msg", id="fwd-1")
        self.assertTrue(rich)
        self.assertIn("记录正文", text)
        self.assertIn("甲", text)
        self.assertEqual(images, ["https://example.com/in.png"])

    async def test_satori_forward_with_children_is_read_in_place(self):
        from satori import Author, Image, Text
        from satori.element import Message, transform
        from satori.parser import parse

        from plugins.hyw.messages import expand_special

        built = Message(forward=True, content=[
            Message(content=[Author("1", "甲"), Text("第一句"), Image(src="https://example.com/a.png")]),
            Message(content=[Author("2", "乙"), Text("第二句")]),
        ])
        parsed = transform(parse(
            '<message forward="true"><message><author id="1" name="甲"/>第一句<img src="https://example.com/a.png"/>'
            '</message><message><author id="2" name="乙"/>第二句</message></message>'
        ))[0]
        session = AsyncMock()
        action = AsyncMock()
        with patch("otae_bot.adapters.onebot.call_account_action", action):
            for element in (built, parsed):
                text, images, rich = await expand_special(session, [element])
                self.assertTrue(rich)
                self.assertIn("【聊天记录 开始", text)
                self.assertLess(text.index("甲（1）"), text.index("第一句"))
                self.assertLess(text.index("第一句"), text.index("乙（2）"))
                self.assertIn("第二句", text)
                self.assertEqual(images, ["https://example.com/a.png"])
        action.assert_not_awaited()

    async def test_satori_forward_without_children_is_fetched_by_id(self):
        from satori.element import Message

        from plugins.hyw.messages import expand_special

        session = AsyncMock()
        action = AsyncMock(return_value={"messages": [{"sender": {"nickname": "丙"}, "content": [
            {"type": "text", "data": {"text": "按 id 取回"}}]}]})
        with patch("otae_bot.adapters.onebot.call_account_action", action):
            text, _, rich = await expand_special(session, [Message("res-1", forward=True)])
        action.assert_awaited_once_with(session.account, "get_forward_msg", id="res-1")
        self.assertTrue(rich)
        self.assertIn("按 id 取回", text)

    def test_xml_entity_declaration_is_not_parsed(self):
        import asyncio
        from plugins.hyw.messages import expand_special

        class Element:
            tag = "xml"
            attrs = {"data": '<!DOCTYPE foo [<!ENTITY x "y">]><msg>&x;</msg>'}
            children = []

        text, images, rich = asyncio.run(expand_special(None, [Element()]))
        self.assertTrue(rich)
        self.assertIn("实体声明", text)
        self.assertEqual(images, [])


def _args(*content):
    return type("R", (), {"all_matched_args": {"content": list(content)}})()


QUOTE_ID = "2|875241970|3969610"
FORWARD = {"status": "ok", "retcode": 0, "data": {"messages": [
    {"sender": {"nickname": "甲", "user_id": "1"}, "time": 0, "content": [
        {"type": "text", "data": {"text": "转发正文"}},
        {"type": "image", "data": {"url": "https://example.com/f.png"}},
    ]},
]}}


def _msg(seq, *segments, message_id=None):
    return {"message_id": message_id or seq * 7, "message_seq": seq, "message": list(segments)}


def _text(value):
    return {"type": "text", "data": {"text": value}}


def _history(*messages, as_list=False):
    """LLBot get_group_msg_history: no real_id/real_seq, matched on message_seq only."""
    return {"status": "ok", "retcode": 0, "data": list(messages) if as_list else {"messages": list(messages)}}


TARGET_FORWARD = _msg(3969610, {"type": "forward", "data": {"id": "resid-1"}}, message_id=537779999)


class SatoriIdTests(unittest.TestCase):
    def test_group_private_and_invalid_ids(self):
        from plugins.hyw.messages import parse_satori_id

        self.assertEqual(parse_satori_id("2|875241970|3969610"), (2, "875241970", 3969610))
        self.assertEqual(parse_satori_id(" 1|u_AbC-123|55 "), (1, "u_AbC-123", 55))
        for bad in (None, "", "fwd-1", "537779999", "2|875241970", "2|875241970|", "2|875241970|abc",
                    "x|1|2", "2||3", "2|1|3|4", "２|875241970|3969610", "2|875 241970|3", "2|1|３"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_satori_id(bad))


class FakeLLBot:
    """Stands in for call_account_action: one answer (or exception) per OneBot action."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    async def __call__(self, account, action, **params):
        self.calls.append((account, action, params))
        answer = self.answers.get(action)
        if answer is None:
            from otae_bot.adapters.onebot import OneBotUnavailable
            raise OneBotUnavailable(f"{action} not mocked")
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def actions(self):
        return [(action, params) for _, action, params in self.calls]


class UnfetchedQuoteTests(unittest.IsolatedAsyncioTestCase):
    """Entari 取不到引用（LLBot message.get 500 消息为空）时 reply 为空，event.quote 只剩 Satori id。"""

    HISTORY_CALL = ("get_group_msg_history", {"group_id": 875241970, "message_seq": 3969610, "count": 10})

    def _bot(self, **answers):
        bot = FakeLLBot(**answers)
        patcher = patch("otae_bot.adapters.onebot.call_account_action", bot)
        patcher.start()
        self.addCleanup(patcher.stop)
        return bot

    def _session(self, quote_id=QUOTE_ID, channel="875241970", user="otae"):
        from types import SimpleNamespace

        from satori import Quote

        session = AsyncMock()
        session.reply = None
        session.send = AsyncMock(return_value=[])
        session.account = SimpleNamespace(platform="qq", self_id="bot", config=SimpleNamespace(token="SATORI-SECRET"))
        session.event = SimpleNamespace(
            quote=Quote(quote_id), channel=SimpleNamespace(id=channel),
            guild=SimpleNamespace(id=channel), user=SimpleNamespace(id=user),
        )
        return session

    async def test_history_is_matched_on_message_seq_not_position(self):
        from plugins.hyw.messages import expand_quote

        bot = self._bot(get_group_msg_history=_history(
            _msg(3969601, _text("更早的消息")),
            _msg(3969610, _text("目标消息"), {"type": "image", "data": {"url": "https://example.com/m.png"}}),
            _msg(3969612, _text("更晚的消息")),
        ))
        session = self._session()
        failures = []
        text, images, rich = await expand_quote(session, QUOTE_ID, failures)
        self.assertEqual(text, "目标消息")
        self.assertEqual(images, ["https://example.com/m.png"])
        self.assertTrue(rich)
        self.assertEqual(bot.actions(), [self.HISTORY_CALL])
        self.assertIs(bot.calls[0][0], session.account)
        self.assertEqual(failures, [])

    async def test_history_data_may_be_the_list_itself(self):
        from plugins.hyw.messages import expand_quote

        self._bot(get_group_msg_history=_history(_msg(3969609, _text("前一条")), _msg(3969610, _text("目标")), as_list=True))
        text, images, rich = await expand_quote(self._session(), QUOTE_ID, [])
        self.assertEqual((text, images, rich), ("目标", [], False))

    async def test_no_matching_seq_is_unavailable(self):
        from plugins.hyw.messages import QuoteUnavailable, expand_quote

        bot = self._bot(get_group_msg_history=_history(_msg(3969608, _text("a")), _msg(3969609, _text("b"))))
        with self.assertRaises(QuoteUnavailable) as caught:
            await expand_quote(self._session(), QUOTE_ID, [])
        self.assertEqual(caught.exception.reason, "get_group_msg_history 返回 2 条，没有 message_seq=3969610")
        self.assertEqual(bot.actions(), [self.HISTORY_CALL])

    async def test_a_forward_segment_is_expanded_by_its_resid(self):
        from plugins.hyw.messages import expand_quote

        bot = self._bot(get_group_msg_history=_history(_msg(3969609, _text("前一条")), TARGET_FORWARD), get_forward_msg=FORWARD)
        text, images, rich = await expand_quote(self._session(), QUOTE_ID, [])
        self.assertEqual(bot.actions(), [self.HISTORY_CALL, ("get_forward_msg", {"id": "resid-1"})])
        self.assertIn("【聊天记录 开始", text)
        self.assertIn("【消息 1】\n发送者：甲（1）", text)
        self.assertIn("转发正文", text)
        self.assertNotIn("前一条", text)
        self.assertEqual(images, ["https://example.com/f.png"])
        self.assertTrue(rich)

    async def test_a_message_without_forward_is_used_directly(self):
        from plugins.hyw.messages import expand_quote

        bot = self._bot(get_group_msg_history=_history(_msg(3969610, _text("普通"), _text("消息"))))
        text, images, _ = await expand_quote(self._session(), QUOTE_ID, [])
        self.assertEqual((text, images), ("普通消息", []))
        self.assertEqual(bot.actions(), [self.HISTORY_CALL])

    async def test_a_private_quote_reads_the_friend_history(self):
        from plugins.hyw.messages import expand_quote

        bot = self._bot(get_friend_msg_history=_history(_msg(77, _text("私聊原文"))))
        session = self._session(quote_id="1|u_AbC123|77", channel="private:123456", user="123456")
        text, _, _ = await expand_quote(session, "1|u_AbC123|77", [])
        self.assertEqual(text, "私聊原文")
        self.assertEqual(bot.actions(), [("get_friend_msg_history", {"user_id": 123456, "message_seq": 77, "count": 10})])

    async def test_unusable_ids_are_skipped_without_any_call(self):
        from plugins.hyw.messages import QuoteUnavailable, expand_quote

        bot = self._bot()
        cases = {
            "fwd-1": "引用 id 不是 chatType|peerUid|msgSeq 格式",
            "537779999": "引用 id 不是 chatType|peerUid|msgSeq 格式",
            "100|875241970|1": "引用 id 的会话无法识别：chatType=100",
            "2|u_group|1": "引用 id 的会话无法识别：chatType=2",
            "1|u_AbC123|77": "引用 id 的会话无法识别：chatType=1",  # 私聊但找不到对方 QQ 号
        }
        for quote_id, reason in cases.items():
            with self.subTest(quote_id=quote_id), self.assertRaises(QuoteUnavailable) as caught:
                await expand_quote(self._session(quote_id, channel="c", user="u"), quote_id, [])
            self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(bot.calls, [])

    async def test_q_gets_the_forward_as_context_without_entari_reply(self):
        from satori import Text

        from plugins.hyw import handlers

        self._bot(get_group_msg_history=_history(_msg(3969600, _text("别的")), TARGET_FORWARD), get_forward_msg=FORWARD)
        session = self._session()
        request = AsyncMock()
        with patch.object(handlers.HywConfig, "from_env", return_value=_config()), \
             patch.object(handlers, "run_request", request), patch.object(handlers, "logger") as log:
            await handle_hyw(session, _args(Text("总结一下")))
        request.assert_awaited_once()
        _, _, scope, text, images, prior = request.await_args.args
        self.assertEqual(scope, ("qq", "bot", "875241970", "875241970", "otae"))
        self.assertTrue(text.startswith("总结一下\n\n[引用消息]\n"), text)
        self.assertIn("转发正文", text)
        self.assertEqual(images, ["https://example.com/f.png"])
        self.assertEqual(prior, [])
        self.assertIs(request.await_args.kwargs["rich"], True)
        log.warning.assert_not_called()

    async def test_q_still_runs_when_the_account_is_not_in_the_group(self):
        from satori import Text

        from otae_bot.adapters.onebot import OneBotUnavailable
        from plugins.hyw import handlers

        bot = self._bot(get_group_msg_history=OneBotUnavailable(
            "satori http://127.0.0.1:5500/v1/internal/onebot11/get_group_msg_history: "
            "HTTP 500: Cannot read properties of undefined (reading 'start')", answered=True))
        session = self._session()
        request = AsyncMock()
        with patch.object(handlers.HywConfig, "from_env", return_value=_config()), \
             patch.object(handlers, "run_request", request), patch.object(handlers, "logger") as log:
            await handle_hyw(session, _args(Text("总结一下")))
        request.assert_awaited_once()
        self.assertEqual(request.await_args.args[3:], ("总结一下", [], []))
        self.assertIs(request.await_args.kwargs["rich"], False)
        self.assertEqual(bot.actions(), [self.HISTORY_CALL])
        template, channel, quote_id, reason, failures = log.warning.call_args.args
        self.assertIn("quoted message unavailable", template)
        self.assertEqual((channel, quote_id), ("875241970", QUOTE_ID))
        self.assertEqual(reason, "get_group_msg_history 报错，收到事件的号可能不在群 875241970")
        self.assertEqual(len(failures), 1)
        self.assertIn("Cannot read properties of undefined (reading 'start')", failures[0])
        self.assertNotIn("SATORI-SECRET", str(log.warning.call_args))

    async def test_the_warning_names_why_the_quote_was_not_read(self):
        from satori import Text

        from otae_bot.adapters.onebot import OneBotUnavailable
        from plugins.hyw import handlers

        cases = [
            ("fwd-1", {}, "引用 id 不是 chatType|peerUid|msgSeq 格式"),
            (QUOTE_ID, {"get_group_msg_history": _history(_msg(1, _text("x")))},
             "get_group_msg_history 返回 1 条，没有 message_seq=3969610"),
            (QUOTE_ID, {"get_group_msg_history": _history(TARGET_FORWARD),
                        "get_forward_msg": OneBotUnavailable("retcode=1200: unexpected end of file", answered=True)},
             "合并转发展开失败（get_forward_msg resid-1）"),
            (QUOTE_ID, {"get_group_msg_history": _history(_msg(3969610, {"type": "forward", "data": {}}))},
             "合并转发展开失败（get_forward_msg 缺少 resid）"),
            (QUOTE_ID, {"get_group_msg_history": OneBotUnavailable("没有可用通道：账号没配 onebot_url，Satori 连接也没有地址")},
             "通道不可用，get_group_msg_history 没有送达"),
            (QUOTE_ID, {"get_group_msg_history": _history(_msg(3969610, {"type": "face", "data": {"id": "1"}}))},
             "引用的消息里没有可读的文字、图片或转发"),
            (QUOTE_ID, {"get_group_msg_history": {"status": "ok", "data": None}}, "get_group_msg_history 返回格式无效"),
        ]
        for quote_id, answers, reason in cases:
            with self.subTest(reason=reason):
                bot = FakeLLBot(**answers)
                request = AsyncMock()
                with patch("otae_bot.adapters.onebot.call_account_action", bot), \
                     patch.object(handlers.HywConfig, "from_env", return_value=_config()), \
                     patch.object(handlers, "run_request", request), patch.object(handlers, "logger") as log:
                    await handle_hyw(self._session(quote_id), _args(Text("总结一下")))
                self.assertEqual(request.await_args.args[3], "总结一下")
                self.assertEqual(log.warning.call_args.args[3], reason)

    async def test_an_unexpected_error_while_reading_still_answers(self):
        from satori import Text

        from plugins.hyw import handlers

        request = AsyncMock()
        with patch.object(handlers, "expand_quote", AsyncMock(side_effect=KeyError("broken"))), \
             patch.object(handlers.HywConfig, "from_env", return_value=_config()), \
             patch.object(handlers, "run_request", request), patch.object(handlers, "logger") as log:
            await handle_hyw(self._session(), _args(Text("总结一下")))
        self.assertEqual(request.await_args.args[3], "总结一下")
        self.assertIn("读取出错", log.warning.call_args.args[3])
        self.assertIn("KeyError", log.warning.call_args.args[3])

    async def test_bare_q_on_an_unreadable_quote_asks_for_a_question(self):
        from plugins.hyw import handlers

        self._bot()
        session = self._session()
        request = AsyncMock()
        with patch.object(handlers.HywConfig, "from_env", return_value=_config()), \
             patch.object(handlers, "run_request", request), patch.object(handlers, "logger"):
            await handle_hyw(session, _args())
        request.assert_not_awaited()
        self.assertIn("没能读取引用的消息", str(session.send.await_args.args[0]))

    async def test_follow_up_on_an_own_answer_uses_the_quote_id(self):
        from satori import Text

        from plugins.hyw import handlers

        bot = self._bot()
        session = self._session(quote_id="2|875241970|4000000")
        scope = handlers.scope_for(session)
        history = [{"role": "user", "content": "旧问题"}, {"role": "assistant", "content": "旧回答"}]
        handlers.history_store.put(scope, "2|875241970|4000000", history)
        self.addCleanup(handlers.history_store.clear, scope)
        request = AsyncMock()
        with patch.object(handlers.HywConfig, "from_env", return_value=_config()), \
             patch.object(handlers, "run_request", request):
            await handle_hyw(session, _args(Text("继续")))
        self.assertEqual(request.await_args.args[3], "继续")
        self.assertEqual(request.await_args.args[5], history)
        self.assertEqual(bot.calls, [])

    async def test_link_uses_the_quote_id(self):
        from plugins.hyw import handlers

        session = self._session(quote_id="2|875241970|4000001")
        scope = handlers.scope_for(session)
        handlers.sources.put(handlers.channel_for(scope), scope[4], "2|875241970|4000001",
                             [{"title": "来源", "url": "https://example.com/s"}])
        self.addCleanup(handlers.sources.clear_sender, handlers.channel_for(scope), scope[4])
        await handlers.handle_link(session, _args())
        self.assertIn("https://example.com/s", str(session.send.await_args.args[0]))


class QuoteChannelEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """引用合并转发发 /q：真实的 call_account_action，模拟 LLBot 8.2.1 的 Satori 透传返回。"""

    CLIENTS = [
        {"host": "127.0.0.1", "port": 5500, "token": "satori-a"},
        {"host": "127.0.0.1", "port": 5550, "token": "satori-b"},
    ]

    def setUp(self):
        from otae_bot.adapters import onebot

        self.seen = []
        self.not_in_group = False

        def llbot(request):
            self.seen.append(request)
            body = json.loads(request.content)
            if request.url.path == "/v1/internal/onebot11/get_group_msg_history":
                if self.not_in_group:
                    return httpx.Response(500, json={"message": "Cannot read properties of undefined (reading 'start')"})
                return httpx.Response(200, json=_history(
                    _msg(body["message_seq"] - 1, _text("前一条")), TARGET_FORWARD, _msg(body["message_seq"] + 1, _text("后一条"))))
            if request.url.path == "/v1/internal/onebot11/get_forward_msg":
                if body != {"id": "resid-1"}:
                    return httpx.Response(200, json={"status": "failed", "retcode": 1200, "message": "unexpected end of file"})
                return httpx.Response(200, json=FORWARD)
            return httpx.Response(404)

        real = httpx.AsyncClient

        def client(**kwargs):
            return real(transport=httpx.MockTransport(llbot), timeout=kwargs.get("timeout"))

        values = {"SATORI_CLIENTS": self.CLIENTS, "ONEBOT_HTTP_URL": "http://127.0.0.1:3000"}
        for patcher in (
            patch.object(onebot.httpx, "AsyncClient", client),
            patch.object(onebot, "ashared_ssl_context", AsyncMock(return_value=None)),
            patch.object(onebot, "_env", side_effect=_env(values)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _session(self):
        from types import SimpleNamespace

        from satori import Quote
        from satori.client.account import ApiInfo

        session = AsyncMock()
        session.reply = None
        session.send = AsyncMock(return_value=[])
        # 收到事件的是 5550 上的第二个号
        session.account = SimpleNamespace(
            platform="qq", self_id="222", config=ApiInfo(host="127.0.0.1", port=5550, token="satori-b"))
        session.event = SimpleNamespace(
            quote=Quote(QUOTE_ID), channel=SimpleNamespace(id="875241970"),
            guild=SimpleNamespace(id="875241970"), user=SimpleNamespace(id="otae"),
        )
        return session

    async def _ask(self):
        from satori import Text

        from plugins.hyw import handlers

        request = AsyncMock()
        with patch.object(handlers.HywConfig, "from_env", return_value=_config()), \
             patch.object(handlers, "run_request", request), patch.object(handlers, "logger") as log:
            await handle_hyw(self._session(), _args(Text("总结一下")))
        request.assert_awaited_once()
        return request, log

    async def test_q_on_a_quoted_forward_gets_its_records_through_the_receiving_account(self):
        request, log = await self._ask()
        text, images = request.await_args.args[3:5]
        self.assertTrue(text.startswith("总结一下\n\n[引用消息]\n"), text)
        self.assertIn("发送者：甲（1）", text)
        self.assertIn("转发正文", text)
        self.assertNotIn("前一条", text)
        self.assertEqual(images, ["https://example.com/f.png"])
        self.assertIs(request.await_args.kwargs["rich"], True)
        log.warning.assert_not_called()
        self.assertEqual([str(sent.url) for sent in self.seen], [
            "http://127.0.0.1:5550/v1/internal/onebot11/get_group_msg_history",
            "http://127.0.0.1:5550/v1/internal/onebot11/get_forward_msg",
        ])
        self.assertEqual(json.loads(self.seen[0].content), {"group_id": 875241970, "message_seq": 3969610, "count": 10})
        for sent in self.seen:
            self.assertEqual(sent.headers["authorization"], "Bearer satori-b")
            self.assertEqual(sent.headers["satori-user-id"], "222")
            self.assertEqual(sent.headers["satori-platform"], "qq")
            self.assertNotIn("x-self-id", sent.headers)

    async def test_an_account_outside_the_group_still_gets_an_answer(self):
        self.not_in_group = True
        request, log = await self._ask()
        self.assertEqual(request.await_args.args[3:], ("总结一下", [], []))
        args = log.warning.call_args.args
        self.assertEqual(args[3], "get_group_msg_history 报错，收到事件的号可能不在群 875241970")
        self.assertIn("HTTP 500: Cannot read properties of undefined (reading 'start')", args[4][0])
        self.assertNotIn("satori-b", str(args))
        self.assertEqual(len(self.seen), 1)


class RetryTests(unittest.TestCase):
    def test_rate_limit_is_retryable_and_auth_failure_is_not(self):
        from hyw_frontier.model_backend import http_status, transient_failure
        from hyw_frontier.model_backend import RETRYABLE_STATUSES

        class Rate:
            status_code = 429

        class Denied:
            status_code = 401

        self.assertIn(http_status(Rate()), RETRYABLE_STATUSES)
        self.assertNotIn(http_status(Denied()), RETRYABLE_STATUSES)
        self.assertTrue(transient_failure(TimeoutError()))
        self.assertFalse(transient_failure(Denied()))


class ErrorTests(unittest.TestCase):
    def test_hyw_error_is_an_exception(self):
        self.assertIsInstance(HywError("x"), Exception)


SECRETS = (
    "sk-live-SECRETKEY1234567890",
    "AIzaSyA-SECRET-QUERY-KEY-0000000000",
    "tok-SECRET-BEARER",
    "SECRET-COOKIE-VALUE",
    "cfg-SECRET-API-KEY",
    "proxy-SECRET-PASS",
    "ya29.SECRET-OAUTH",
)


class FrontierError(RuntimeError):
    """Stand-in for hyw_frontier.errors.FrontierError (not installed in the test venv)."""

    def __init__(self, message, *, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class RenderError(FrontierError):
    pass


class APIStatusError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def _model_failure() -> FrontierError:
    """Same shape as hyw_frontier: safe_model_error(...) raised ``from None`` over the SDK error."""
    try:
        try:
            raise APIStatusError(
                "Error code: 401 - Incorrect API key provided: sk-live-SECRETKEY1234567890. "
                "POST https://llm.example.test/v1/chat?key=AIzaSyA-SECRET-QUERY-KEY-0000000000&alt=json "
                "headers={'Authorization': 'Bearer tok-SECRET-BEARER', 'Cookie': 'session=SECRET-COOKIE-VALUE'} "
                "via http://user:proxy-SECRET-PASS@proxy.test:7897 token ya29.SECRET-OAUTH",
                401,
            )
        except APIStatusError:
            raise FrontierError(
                "模型认证失败，请检查 API Key 或服务账号凭据。",
                diagnostics={"code": "http_401", "http_status": 401, "retryable": False},
            ) from None
    except FrontierError as error:
        return error


def _config(**overrides) -> HywConfig:
    values = dict(api_key="cfg-SECRET-API-KEY", proxy="http://user:proxy-SECRET-PASS@proxy.test:7897")
    values.update(overrides)
    return HywConfig(**values)


class FailureLogTests(unittest.IsolatedAsyncioTestCase):
    def assert_redacted(self, text: str):
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_summary_has_code_status_and_hidden_cause_without_secrets(self):
        from plugins.hyw.handlers import failure_summary

        summary = failure_summary(_model_failure(), ("cfg-SECRET-API-KEY",))
        self.assertIn("code=http_401", summary)
        self.assertIn("http_status=401", summary)
        self.assertIn("retryable=False", summary)
        self.assertIn("FrontierError: 模型认证失败", summary)
        # `from None` 只隐藏 traceback，__context__ 里的 SDK 异常才是有用的部分
        self.assertIn("APIStatusError: Error code: 401 - Incorrect API key provided", summary)
        self.assertIn("https://llm.example.test/v1/chat?<redacted>", summary)
        self.assert_redacted(summary)

    def test_status_and_code_are_found_on_plain_exceptions_in_the_chain(self):
        import httpx
        from plugins.hyw.handlers import failure_summary

        request = httpx.Request("GET", "https://api.example.test/v1/models?api_key=SECRET-QUERY")
        response = httpx.Response(429, request=request)
        try:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise RuntimeError("upstream failed") from error
        except RuntimeError as error:
            summary = failure_summary(error)
        self.assertIn("code=- http_status=429", summary)
        self.assertIn("RuntimeError: upstream failed <- HTTPStatusError:", summary)
        self.assertNotIn("SECRET-QUERY", summary)

    async def test_frontier_error_in_run_request_is_logged_before_the_user_message(self):
        import sys
        import types

        from plugins.hyw import handlers

        package = types.ModuleType("hyw_frontier")
        package.answer = AsyncMock(side_effect=_model_failure())
        errors = types.ModuleType("hyw_frontier.errors")
        errors.FrontierError = FrontierError
        rendering = types.ModuleType("hyw_frontier.rendering")
        rendering.RenderError = RenderError
        modules = {"hyw_frontier": package, "hyw_frontier.errors": errors, "hyw_frontier.rendering": rendering}
        with patch.dict(sys.modules, modules), patch.object(handlers, "logger") as log:
            with self.assertRaises(HywError) as raised:
                await handlers.run_request(AsyncMock(), _config(), ("qq", "b", "g", "c", "u"), "问题", [], [])
        self.assertIn("模型认证失败", str(raised.exception))
        template, summary = log.warning.call_args[0]
        self.assertIn("answer failed", template)
        self.assertIn("code=http_401 http_status=401", summary)
        self.assertIn("APIStatusError", summary)
        self.assert_redacted(summary)

    async def test_unexpected_error_at_the_chat_boundary_logs_the_chain(self):
        from satori import Text

        from plugins.hyw import handlers

        session = AsyncMock()
        session.reply = None
        session.send = AsyncMock(return_value=[])

        async def failing(*args, **kwargs):
            try:
                raise ConnectionResetError("reset by peer; Authorization: Bearer tok-SECRET-BEARER")
            except ConnectionResetError as error:
                raise RuntimeError("search backend crashed (api_key=cfg-SECRET-API-KEY)") from error

        with patch.object(handlers.HywConfig, "from_env", return_value=_config()), \
             patch.object(handlers, "run_request", failing), \
             patch.object(handlers, "logger") as log:
            await handle_hyw(session, type("R", (), {"all_matched_args": {"content": [Text("问题")]}})())
        template, summary = log.warning.call_args[0]
        self.assertIn("request failed", template)
        self.assertIn("RuntimeError: search backend crashed", summary)
        self.assertIn("<- ConnectionResetError: reset by peer", summary)
        self.assert_redacted(summary)
        session.send.assert_awaited()
