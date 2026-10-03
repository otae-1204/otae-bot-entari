from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

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
        from plugins.hyw.messages import expand_special

        class Session:
            def __init__(self):
                self.ids = []

            async def internal(self, action, message_id):
                self.ids.append((action, message_id))
                return {"messages": [{
                    "sender": {"nickname": "甲", "user_id": "1"},
                    "time": 0,
                    "message": [
                        {"type": "text", "data": {"text": "记录正文"}},
                        {"type": "image", "data": {"url": "https://example.com/in.png"}},
                    ],
                }]}

        class Element:
            tag = "forward"
            attrs = {"id": "fwd-1"}
            children = []

        session = Session()
        text, images, rich = await expand_special(session, [Element()])
        self.assertEqual(session.ids, [("get_forward_msg", "fwd-1")])
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
        for element in (built, parsed):
            text, images, rich = await expand_special(session, [element])
            self.assertTrue(rich)
            self.assertIn("【聊天记录 开始", text)
            self.assertLess(text.index("甲（1）"), text.index("第一句"))
            self.assertLess(text.index("第一句"), text.index("乙（2）"))
            self.assertIn("第二句", text)
            self.assertEqual(images, ["https://example.com/a.png"])
        session.internal.assert_not_awaited()

    async def test_satori_forward_without_children_is_fetched_by_id(self):
        from satori.element import Message

        from plugins.hyw.messages import expand_special

        session = AsyncMock()
        session.internal.return_value = {"messages": [{"sender": {"nickname": "丙"}, "content": [
            {"type": "text", "data": {"text": "按 id 取回"}}]}]}
        text, _, rich = await expand_special(session, [Message("res-1", forward=True)])
        session.internal.assert_awaited_once_with("get_forward_msg", message_id="res-1")
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


class UnfetchedQuoteTests(unittest.IsolatedAsyncioTestCase):
    """Entari 取不到引用（LLBot message.get 500 消息为空）时 reply 为空，但 event.quote 还在。"""

    FORWARD = {"status": "ok", "retcode": 0, "data": {"messages": [
        {"sender": {"nickname": "甲", "user_id": "1"}, "time": 0, "content": [
            {"type": "text", "data": {"text": "转发正文"}},
            {"type": "image", "data": {"url": "https://example.com/f.png"}},
        ]},
    ]}}

    def setUp(self):
        self.http = AsyncMock(side_effect=RuntimeError("未配置 OneBot HTTP 地址"))
        patcher = patch("otae_bot.adapters.onebot.call_onebot_action", self.http)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _session(self, internal=None, quote_id="fwd-1"):
        from types import SimpleNamespace

        from satori import Quote

        session = AsyncMock()
        session.reply = None
        session.send = AsyncMock(return_value=[])
        session.internal = internal or AsyncMock(side_effect=RuntimeError("internal route missing"))
        session.account = SimpleNamespace(platform="qq", self_id="bot")
        session.event = SimpleNamespace(
            quote=Quote(quote_id), channel=SimpleNamespace(id="100"),
            guild=SimpleNamespace(id="100"), user=SimpleNamespace(id="otae"),
        )
        return session

    async def test_forward_is_read_by_the_quoted_message_id(self):
        from plugins.hyw.messages import expand_quote

        session = self._session(AsyncMock(return_value=self.FORWARD))
        failures = []
        text, images, rich = await expand_quote(session, "fwd-1", failures)
        session.internal.assert_awaited_once_with("get_forward_msg", message_id="fwd-1")
        self.assertIn("【消息 1】\n发送者：甲（1）", text)
        self.assertIn("转发正文", text)
        self.assertEqual(images, ["https://example.com/f.png"])
        self.assertTrue(rich)
        self.assertEqual(failures, [])

    async def test_onebot_http_is_used_when_the_satori_internal_route_fails(self):
        from plugins.hyw.messages import expand_quote

        session = self._session()
        self.http.side_effect = None
        self.http.return_value = self.FORWARD
        failures = []
        text, _, _ = await expand_quote(session, "fwd-1", failures)
        self.http.assert_awaited_once_with(session.account, "get_forward_msg", message_id="fwd-1")
        self.assertIn("转发正文", text)
        self.assertEqual(failures, ["internal get_forward_msg: RuntimeError: internal route missing"])

    async def test_a_failed_onebot_status_from_the_internal_route_also_falls_back(self):
        from plugins.hyw.messages import expand_quote

        session = self._session(AsyncMock(return_value={"status": "failed", "retcode": 1200, "wording": "msg not found"}))
        self.http.side_effect = None
        self.http.return_value = self.FORWARD
        failures = []
        text, _, _ = await expand_quote(session, "fwd-1", failures)
        self.assertIn("转发正文", text)
        self.assertEqual(failures, ["internal get_forward_msg: RuntimeError: msg not found"])

    async def test_other_quotes_fall_back_to_get_msg(self):
        from plugins.hyw.messages import expand_quote

        async def internal(action, **params):
            if action == "get_msg":
                return {"data": {"message_id": params["message_id"], "message": [
                    {"type": "text", "data": {"text": "原消息"}},
                    {"type": "image", "data": {"url": "https://example.com/m.png"}},
                    {"type": "forward", "data": {"id": "inner"}},
                ]}}
            if params == {"message_id": "inner"}:
                return self.FORWARD
            raise RuntimeError("消息为空")

        session = self._session(AsyncMock(side_effect=internal))
        text, images, rich = await expand_quote(session, "plain-1", [])
        self.assertIn("原消息", text)
        self.assertIn("转发正文", text)
        self.assertEqual(images, ["https://example.com/m.png", "https://example.com/f.png"])
        self.assertTrue(rich)

    async def test_nothing_readable_is_empty_with_every_step_recorded(self):
        from plugins.hyw.messages import expand_quote

        failures = []
        self.assertEqual(await expand_quote(self._session(), "fwd-1", failures), ("", [], False))
        self.assertEqual([item.split(":")[0] for item in failures], [
            "internal get_forward_msg", "onebot get_forward_msg", "internal get_msg", "onebot get_msg",
        ])

    async def test_q_gets_the_forward_as_context_without_entari_reply(self):
        from satori import Text

        from plugins.hyw import handlers

        session = self._session(AsyncMock(return_value=self.FORWARD))
        request = AsyncMock()
        with patch.object(handlers.HywConfig, "from_env", return_value=_config()), \
             patch.object(handlers, "run_request", request):
            await handle_hyw(session, _args(Text("总结一下")))
        request.assert_awaited_once()
        _, _, scope, text, images, prior = request.await_args.args
        self.assertEqual(scope, ("qq", "bot", "100", "100", "otae"))
        self.assertTrue(text.startswith("总结一下\n\n[引用消息]\n"), text)
        self.assertIn("转发正文", text)
        self.assertEqual(images, ["https://example.com/f.png"])
        self.assertEqual(prior, [])
        self.assertIs(request.await_args.kwargs["rich"], True)

    async def test_q_still_runs_when_the_forward_cannot_be_read(self):
        from satori import Text

        from plugins.hyw import handlers

        session = self._session()
        request = AsyncMock()
        with patch.object(handlers.HywConfig, "from_env", return_value=_config()), \
             patch.object(handlers, "run_request", request), patch.object(handlers, "logger") as log:
            await handle_hyw(session, _args(Text("总结一下")))
        request.assert_awaited_once()
        self.assertEqual(request.await_args.args[3:], ("总结一下", [], []))
        self.assertIs(request.await_args.kwargs["rich"], False)
        template, channel, quote_id, failures = log.warning.call_args.args
        self.assertIn("quoted message unavailable", template)
        self.assertEqual((channel, quote_id, len(failures)), ("100", "fwd-1", 4))

    async def test_bare_q_on_an_unreadable_quote_asks_for_a_question(self):
        from plugins.hyw import handlers

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

        session = self._session(quote_id="answer-1")
        scope = handlers.scope_for(session)
        history = [{"role": "user", "content": "旧问题"}, {"role": "assistant", "content": "旧回答"}]
        handlers.history_store.put(scope, "answer-1", history)
        self.addCleanup(handlers.history_store.clear, scope)
        request = AsyncMock()
        with patch.object(handlers.HywConfig, "from_env", return_value=_config()), \
             patch.object(handlers, "run_request", request):
            await handle_hyw(session, _args(Text("继续")))
        self.assertEqual(request.await_args.args[3], "继续")
        self.assertEqual(request.await_args.args[5], history)
        session.internal.assert_not_awaited()

    async def test_link_uses_the_quote_id(self):
        from plugins.hyw import handlers

        session = self._session(quote_id="answer-2")
        scope = handlers.scope_for(session)
        handlers.sources.put(handlers.channel_for(scope), scope[4], "answer-2", [{"title": "来源", "url": "https://example.com/s"}])
        self.addCleanup(handlers.sources.clear_sender, handlers.channel_for(scope), scope[4])
        await handlers.handle_link(session, _args())
        self.assertIn("https://example.com/s", str(session.send.await_args.args[0]))


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
