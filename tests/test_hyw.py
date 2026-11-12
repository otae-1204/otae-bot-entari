from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import pytest

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

    def test_reasoning_mode_is_read_and_invalid_values_fall_back_to_auto(self):
        cases = {None: "auto", "": "auto", "low": "low", " High ": "high", "medium": "medium", "max": "auto"}
        for raw, expected in cases.items():
            values = {} if raw is None else {"HYW_REASONING_MODE": raw}
            with self.subTest(raw=raw), patch("plugins.hyw.config._env", side_effect=_env(values)):
                self.assertEqual(HywConfig.from_env().reasoning_mode, expected)

    def test_render_limits_are_read_with_safe_fallbacks(self):
        cases = {
            ("HYW_RENDER_TIMEOUT", "render_timeout"): {None: 45, "20": 20, "7.5": 7.5, "0": 45, "-1": 45,
                                                       "nan": 45, "inf": 45, "x": 45},
            ("HYW_RENDER_MAX_IMAGES", "render_max_images"): {None: 16, "4": 4, "0": 0, "-1": 16, "x": 16},
            ("HYW_RENDER_MAX_EDGE", "render_max_edge"): {None: 1024, "800": 800, "5000": 1280, "10": 1024, "x": 1024},
        }
        for (key, field), expected in cases.items():
            for raw, value in expected.items():
                values = {} if raw is None else {key: raw}
                with self.subTest(key=key, raw=raw), patch("plugins.hyw.config._env", side_effect=_env(values)):
                    self.assertEqual(getattr(HywConfig.from_env(), field), value)

    def test_max_rounds_is_read_and_falls_back_to_10(self):
        cases = {None: 10, "6": 6, " 1 ": 1, "0": 10, "-3": 10, "x": 10, "2.5": 10}
        for raw, expected in cases.items():
            values = {} if raw is None else {"HYW_MAX_ROUNDS": raw}
            with self.subTest(raw=raw), patch("plugins.hyw.config._env", side_effect=_env(values)):
                self.assertEqual(HywConfig.from_env().max_rounds, expected)

    def test_render_image_cap_does_not_touch_the_tool_image_budget(self):
        values = {"HYW_RENDER_MAX_IMAGES": "3"}
        with patch("plugins.hyw.config._env", side_effect=_env(values)):
            config = HywConfig.from_env()
        self.assertEqual(config.max_tool_images, 600)
        self.assertEqual(config.render_max_images, 3)


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


def _elements(content: str):
    from satori.element import transform
    from satori.parser import parse

    return transform(parse(content))


def _forward(*nodes: str, resid: str = "resid-1") -> str:
    return f'<message id="{resid}" forward="true">' + "".join(f"<message>{node}</message>" for node in nodes) + "</message>"


def _node(uid: str, name: str, body: str) -> str:
    return f'<author id="{uid}" name="{name}"/>{body}'


class ExpansionTests(unittest.TestCase):
    def test_json_and_xml_cards_keep_public_fields_only(self):
        from xml.sax.saxutils import quoteattr

        from plugins.hyw.messages import expand_special

        payload = json.dumps({
            "token": "secret-token",
            "meta": {"title": "夜航", "desc": "一张单曲", "cover": "https://example.com/cover.jpg"},
        })
        xml = '<msg title="分享" url="https://example.com/a"><image src="https://example.com/b.png"/></msg>'
        text, images, rich = expand_special(_elements(
            f"<onebot:json data={quoteattr(payload)}/><onebot:xml data={quoteattr(xml)}/>"))
        self.assertTrue(rich)
        self.assertIn("夜航", text)
        self.assertIn("一张单曲", text)
        self.assertNotIn("secret-token", text)
        self.assertIn("https://example.com/cover.jpg", images)
        self.assertIn("https://example.com/a", text)
        self.assertIn("https://example.com/b.png", images)

    def test_llonebot_ark_is_read_like_onebot_json(self):
        from xml.sax.saxutils import quoteattr

        from plugins.hyw.messages import expand_special

        payload = json.dumps({"meta": {"news": {"title": "群分享", "jumpUrl": "https://example.com/n"}}})
        elements = _elements(f"<llonebot:ark data={quoteattr(payload)}/>")
        text, _, rich = expand_special(elements)
        self.assertTrue(rich)
        self.assertIn("群分享", text)
        self.assertIn("https://example.com/n", text)

    def test_messages_without_cards_or_forwards_add_nothing(self):
        from plugins.hyw.messages import expand_special

        self.assertEqual(expand_special(_elements('普通消息<img src="https://example.com/a.png"/><at id="1"/>')),
                         ("", [], False))

    def test_satori_forward_with_children_is_read_in_place(self):
        from satori import Author, Image, Text
        from satori.element import Message

        from plugins.hyw.messages import expand_special

        built = Message(forward=True, content=[
            Message(content=[Author("1", "甲"), Text("第一句"), Image(src="https://example.com/a.png")]),
            Message(content=[Author("2", "乙"), Text("第二句")]),
        ])
        parsed = _elements(_forward(_node("1", "甲", '第一句<img src="https://example.com/a.png"/>'),
                                    _node("2", "乙", "第二句")))[0]
        for element in (built, parsed):
            text, images, rich = expand_special([element])
            self.assertTrue(rich)
            self.assertIn("【聊天记录 开始", text)
            self.assertLess(text.index("甲（1）"), text.index("第一句"))
            self.assertLess(text.index("第一句"), text.index("乙（2）"))
            self.assertIn("第二句", text)
            self.assertEqual(images, ["https://example.com/a.png"])

    def test_nested_forwards_and_their_images_are_read_in_order(self):
        from plugins.hyw.messages import expand_special

        inner = _forward(_node("3", "丙", '里层<img src="https://example.com/inner.png"/>'), resid="resid-2")
        text, images, _ = expand_special(_elements(_forward(
            _node("1", "甲", '外层<img src="https://example.com/outer.png"/>'),
            _node("2", "乙", inner),
        )))
        self.assertEqual(text.count("【聊天记录 开始"), 2)
        self.assertLess(text.index("外层"), text.index("丙（3）"))
        self.assertLess(text.index("丙（3）"), text.index("里层"))
        self.assertEqual(images, ["https://example.com/outer.png", "https://example.com/inner.png"])

    def test_forward_without_children_is_left_unread(self):
        from satori.element import Message

        from plugins.hyw.messages import expand_special

        for element in (Message("res-1", forward=True), _elements('<message id="res-1" forward="true"/>')[0]):
            text, images, rich = expand_special([element])
            self.assertTrue(rich)
            self.assertIn("聊天记录读取失败", text)
            self.assertEqual(images, [])

    def test_limits_apply_to_inline_forwards(self):
        from plugins.hyw import messages

        deep = "最深处"
        for level in range(messages._MAX_DEPTH + 1):
            deep = _forward(_node(str(level), f"层{level}", deep), resid=f"r{level}")
        text, _, _ = messages.expand_special(_elements(deep))
        self.assertIn("嵌套过深", text)
        self.assertNotIn("最深处", text)

        many_images = _forward(*(_node("1", "甲", f'<img src="https://example.com/{i}.png"/>') for i in range(25)))
        text, images, _ = messages.expand_special(_elements(many_images))
        self.assertEqual(len(images), messages._MAX_IMAGES)
        self.assertIn("展开图片达到 20 张上限", text)

        with patch.object(messages, "_MAX_MESSAGES", 3):
            text, _, _ = messages.expand_special(_elements(_forward(*(_node("1", "甲", f"第{i}条") for i in range(5)))))
        self.assertIn("第2条", text)
        self.assertNotIn("第3条", text)
        self.assertIn("展开消息数量达到", text)

        with patch.object(messages, "_MAX_TEXT", 200):
            text, _, _ = messages.expand_special(_elements(_forward(_node("1", "甲", "长" * 500))))
        self.assertIn("展开文本达到上限", text)
        self.assertLess(len(text.encode()), 400)

    def test_xml_entity_declaration_is_not_parsed(self):
        from plugins.hyw.messages import expand_special

        class Element:
            tag = "onebot:xml"
            attrs = {"data": '<!DOCTYPE foo [<!ENTITY x "y">]><msg>&x;</msg>'}
            children = []

        text, images, rich = expand_special([Element()])
        self.assertTrue(rich)
        self.assertIn("实体声明", text)
        self.assertEqual(images, [])


def _args(*content):
    return type("R", (), {"all_matched_args": {"content": list(content)}})()


QUOTE_ID = "2|875241970|3969610"
QUOTED_FORWARD = '<author id="9" name="转发人"/>' + _forward(
    _node("1", "甲", '转发正文<img src="https://example.com/f.png"/>'),
    _node("2", "乙", "第二条"),
)


class QuoteTests(unittest.IsolatedAsyncioTestCase):
    """/q 引用：内容随事件送达（边界适配器展开的合并转发也在其中），不再另行查询。"""

    def _session(self, quote_id=QUOTE_ID, *, content=None, reply=True):
        from types import SimpleNamespace

        from satori import MessageObject, Quote

        session = AsyncMock()
        session.send = AsyncMock(return_value=[])
        session.account = SimpleNamespace(platform="qq", self_id="bot")
        session.reply = None
        if content is None:
            quote = Quote(quote_id)
        elif reply:
            quote = Quote(quote_id)
            session.reply = SimpleNamespace(quote=quote, origin=MessageObject(quote_id, content))
        else:  # Entari had no reply although the quote carries content
            quote = Quote(quote_id, content=_elements(content))
        session.event = SimpleNamespace(
            quote=quote, channel=SimpleNamespace(id="875241970"),
            guild=SimpleNamespace(id="875241970"), user=SimpleNamespace(id="otae"),
        )
        return session

    async def _ask(self, session, *content):
        from plugins.hyw import handlers

        request = AsyncMock()
        with patch.object(handlers.HywConfig, "from_env", return_value=_config()), \
             patch.object(handlers, "run_request", request), patch.object(handlers, "logger") as log:
            await handle_hyw(session, _args(*content))
        return request, log

    async def test_q_gets_a_quoted_forward_from_the_event(self):
        from satori import Text

        request, log = await self._ask(self._session(content=QUOTED_FORWARD), Text("总结一下"))
        request.assert_awaited_once()
        _, _, scope, text, images, prior = request.await_args.args
        self.assertEqual(scope, ("qq", "bot", "875241970", "875241970", "otae"))
        self.assertTrue(text.startswith("总结一下\n\n[引用消息]\n"), text)
        self.assertIn("【消息 1】\n发送者：甲（1）", text)
        self.assertLess(text.index("转发正文"), text.index("乙（2）"))
        self.assertEqual(images, ["https://example.com/f.png"])
        self.assertEqual(prior, [])
        self.assertIs(request.await_args.kwargs["rich"], True)
        log.warning.assert_not_called()

    async def test_quote_content_is_read_even_without_an_entari_reply(self):
        from satori import Text

        request, log = await self._ask(self._session(content=QUOTED_FORWARD, reply=False), Text("总结一下"))
        self.assertIn("转发正文", request.await_args.args[3])
        self.assertEqual(request.await_args.args[4], ["https://example.com/f.png"])
        log.warning.assert_not_called()

    async def test_a_quoted_plain_message_is_used_directly(self):
        from satori import Text

        request, _ = await self._ask(
            self._session(content='<author id="9"/>普通消息<img src="https://example.com/m.png"/>'), Text("问"))
        self.assertEqual(request.await_args.args[3:5], ("问\n\n[引用消息]\n普通消息", ["https://example.com/m.png"]))

    async def test_unread_quotes_answer_without_context_and_say_why(self):
        from satori import Text

        cases = [
            (self._session(content='<author id="9"/><message id="resid-1" forward="true"/>'), "引用的合并转发没有随事件展开"),
            (self._session(content='<message id="resid-1" forward="true"/>'), "引用的合并转发没有随事件展开"),
            (self._session(), "引用的消息没有随事件送达"),
            (self._session(content='<author id="9"/><message id="resid-1" forward="true"/>', reply=False),
             "引用的合并转发没有随事件展开"),
        ]
        for session, reason in cases:
            with self.subTest(reason=reason):
                request, log = await self._ask(session, Text("总结一下"))
                request.assert_awaited_once()
                self.assertEqual(request.await_args.args[3:], ("总结一下", [], []))
                self.assertIs(request.await_args.kwargs["rich"], False)
                template, channel, quote_id, logged = log.warning.call_args.args
                self.assertIn("quoted message unavailable", template)
                self.assertEqual((channel, quote_id, logged), ("875241970", QUOTE_ID, reason))

    async def test_an_unexpected_error_while_reading_still_answers(self):
        from satori import Text

        from plugins.hyw import handlers

        # The first call reads the command's own content, the second the quote.
        with patch.object(handlers, "expand_special", side_effect=[("", [], False), KeyError("broken")]):
            request, log = await self._ask(self._session(content=QUOTED_FORWARD), Text("总结一下"))
        self.assertEqual(request.await_args.args[3], "总结一下")
        self.assertIn("读取出错", log.warning.call_args.args[3])
        self.assertIn("KeyError", log.warning.call_args.args[3])

    async def test_bare_q_on_an_unreadable_quote_asks_for_a_question(self):
        for session in (self._session(), self._session(content='<message id="resid-1" forward="true"/>')):
            request, _ = await self._ask(session)
            request.assert_not_awaited()
            self.assertIn("没能读取引用的消息", str(session.send.await_args.args[0]))

    async def test_follow_up_on_an_own_answer_uses_the_quote_id(self):
        from satori import Text

        from plugins.hyw import handlers

        session = self._session(quote_id="2|875241970|4000000")
        scope = handlers.scope_for(session)
        history = [{"role": "user", "content": "旧问题"}, {"role": "assistant", "content": "旧回答"}]
        handlers.history_store.put(scope, "2|875241970|4000000", history)
        self.addCleanup(handlers.history_store.clear, scope)
        request, log = await self._ask(session, Text("继续"))
        self.assertEqual(request.await_args.args[3], "继续")
        self.assertEqual(request.await_args.args[5], history)
        log.warning.assert_not_called()

    async def test_link_uses_the_quote_id(self):
        from plugins.hyw import handlers

        session = self._session(quote_id="2|875241970|4000001")
        scope = handlers.scope_for(session)
        handlers.sources.put(handlers.channel_for(scope), scope[4], "2|875241970|4000001",
                             [{"title": "来源", "url": "https://example.com/s"}])
        self.addCleanup(handlers.sources.clear_sender, handlers.channel_for(scope), scope[4])
        await handlers.handle_link(session, _args())
        self.assertIn("https://example.com/s", str(session.send.await_args.args[0]))


class RetryTests(unittest.TestCase):
    def test_rate_limit_is_retryable_and_auth_failure_is_not(self):
        pytest.importorskip("hyw_frontier")
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


def _fake_frontier(answer) -> dict:
    import types

    package = types.ModuleType("hyw_frontier")
    package.answer = answer
    errors = types.ModuleType("hyw_frontier.errors")
    errors.FrontierError = FrontierError
    rendering = types.ModuleType("hyw_frontier.rendering")
    rendering.RenderError = RenderError
    titles = types.ModuleType("hyw_frontier.source_titles")
    titles.source_key = lambda url: url
    titles.source_titles = lambda messages: {}
    return {
        "hyw_frontier": package, "hyw_frontier.errors": errors,
        "hyw_frontier.rendering": rendering, "hyw_frontier.source_titles": titles,
    }


class AnswerCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_reasoning_mode_and_request_timeout_reach_answer(self):
        import sys
        from types import SimpleNamespace

        from plugins.hyw import handlers

        result = SimpleNamespace(messages=[], links=[], kind="text", png=None, display_text="答", text="答")
        answer = AsyncMock(return_value=result)
        session = AsyncMock()
        session.send = AsyncMock(return_value=[])
        config = _config(reasoning_mode="low", request_timeout=42)
        with patch.dict(sys.modules, _fake_frontier(answer)):
            await handlers.run_request(session, config, ("qq", "b", "g", "c", "u"), "问题", [], [])
        kwargs = answer.await_args.kwargs
        self.assertEqual(kwargs["reasoning_mode"], "low")
        self.assertEqual(kwargs["timeout"], 42)

    async def test_render_settings_and_event_logger_reach_answer(self):
        import inspect
        import sys
        from types import SimpleNamespace

        from plugins.hyw import handlers

        result = SimpleNamespace(messages=[], links=[], kind="text", png=None, display_text="答", text="答")
        answer = AsyncMock(return_value=result)
        session = AsyncMock()
        session.send = AsyncMock(return_value=[])
        config = _config(render_timeout=33, render_max_images=5, render_max_edge=800)
        with patch.dict(sys.modules, _fake_frontier(answer)):
            await handlers.run_request(session, config, ("qq", "b", "g", "c", "u"), "问题", [], [])
        kwargs = answer.await_args.kwargs
        self.assertEqual(kwargs["render_timeout"], 33)
        self.assertEqual(kwargs["render_max_images"], 5)
        self.assertEqual(kwargs["render_max_edge"], 800)
        self.assertEqual(kwargs["max_tool_images"], 600)
        self.assertTrue(callable(kwargs["on_event"]))
        self.assertFalse(inspect.iscoroutinefunction(kwargs["on_event"]))

    async def test_max_rounds_reaches_answer(self):
        import sys
        from types import SimpleNamespace

        from plugins.hyw import handlers

        result = SimpleNamespace(messages=[], links=[], kind="text", png=None, display_text="答", text="答")
        answer = AsyncMock(return_value=result)
        session = AsyncMock()
        session.send = AsyncMock(return_value=[])
        with patch("plugins.hyw.config._env", side_effect=_env({"HYW_API_KEY": "k"})):
            default = HywConfig.from_env()
        for config, expected in ((default, 10), (_config(max_rounds=4), 4)):
            with self.subTest(expected=expected), patch.dict(sys.modules, _fake_frontier(answer)):
                await handlers.run_request(session, config, ("qq", "b", "g", "c", "u"), "问题", [], [])
                self.assertEqual(answer.await_args.kwargs["max_rounds"], expected)


def _sent_texts(session) -> list[str]:
    return ["".join(getattr(part, "text", "") for part in call.args[0]) for call in session.send.await_args_list]


class RenderFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, result=None, *, error=None, config=None):
        import sys

        from plugins.hyw import handlers

        answer = AsyncMock(return_value=result, side_effect=error)
        session = AsyncMock()
        session.send = AsyncMock(return_value=[])
        with patch.dict(sys.modules, _fake_frontier(answer)), patch.object(handlers, "logger") as log:
            await handlers.run_request(session, config or _config(), ("qq", "b", "g", "c", "u"), "问题", [], [])
        return session, log

    def _result(self, **overrides):
        from types import SimpleNamespace

        values = dict(messages=[], links=["https://example.com/a"], kind="text", png=None,
                      display_text="# 标题\n\n完整答案正文", text="<final_response>完整答案正文</final_response>",
                      render_error="", answer_ms=1200, render_ms=45_100)
        values.update(overrides)
        return SimpleNamespace(**values)

    async def test_a_failed_card_still_delivers_the_answer_text(self):
        for code in ("render_timeout", "render_formula_limit"):
            with self.subTest(code=code):
                session, log = await self._run(self._result(render_error=code))
                sent = _sent_texts(session)
                self.assertEqual(sent, ["卡片生成失败\n\n# 标题\n\n完整答案正文"])
                self.assertNotIn("请稍后重试", "".join(sent))
                warning = " ".join(str(call) for call in log.warning.call_args_list)
                self.assertIn(code, warning)
                self.assertNotIn("完整答案正文", warning)

    async def test_plain_text_answers_get_no_failure_note(self):
        session, _ = await self._run(self._result())
        self.assertEqual(_sent_texts(session), ["# 标题\n\n完整答案正文"])

    async def test_render_disabled_sends_text_without_a_failure_note(self):
        session, _ = await self._run(self._result(render_error="render_timeout"), config=_config(render=False))
        self.assertEqual(_sent_texts(session), ["# 标题\n\n完整答案正文"])

    async def test_a_raised_render_error_carrying_the_text_is_still_delivered(self):
        error = RenderError("图片渲染超时。", diagnostics={"code": "render_timeout"})
        error.display_text = "完整答案正文"
        session, _ = await self._run(error=error)
        self.assertEqual(_sent_texts(session), ["卡片生成失败\n\n完整答案正文"])

    async def test_a_raised_render_error_without_text_says_so(self):
        with self.assertRaises(HywError) as raised:
            await self._run(error=RenderError("图片渲染超时。", diagnostics={"code": "render_timeout"}))
        self.assertIn("卡片生成失败", str(raised.exception))
        self.assertNotIn("文本回答已生成完毕", str(raised.exception))


class EventLogTests(unittest.TestCase):
    def test_timings_and_counts_are_logged_without_text_or_arguments(self):
        from plugins.hyw import handlers

        events = [
            {"type": "model_start", "round": 1, "reasoning_level": "medium", "images_sent": 0, "new_images_sent": 0},
            {"type": "model_first_output", "round": 1, "duration_ms": 812.5},
            {"type": "tool_start", "round": 1, "name": "web_search", "arguments": {"query": "SECRET-QUERY"}},
            {"type": "tool_end", "round": 1, "name": "web_search", "duration_ms": 950.0, "ok": True, "code": None,
             "partial": False, "result": {"content": [{"text": "SECRET-RESULT"}]}, "sources": [{"url": "u"}]},
            {"type": "round_timing", "round": 1, "phase": "model", "complete": False, "model_ms": 1.0},
            {"type": "round_timing", "round": 1, "phase": "finished", "complete": True, "model_ms": 4100.5,
             "tool_ms": 950.0, "media_download_ms": 0, "image_processing_ms": 0, "total_ms": 5100.5},
            {"type": "answer_ready", "text": "SECRET-ANSWER", "display_text": "SECRET-ANSWER"},
            {"type": "text_delta", "delta": "SECRET-ANSWER"},
            {"type": "effective_prompt", "system_prompt": "SECRET-PROMPT"},
            {"type": "render_start", "timeout_s": 45, "max_images": 16, "max_edge": 1024,
             "available_images": 40, "available_icons": 6},
            {"type": "render_end", "duration_ms": 9000, "image": {"bytes": 1},
             "stages_ms": {"selection_ms": 3.0, "queue_ms": 0.1, "prepare_ms": 1500.0, "draw_ms": 7400.0,
                           "worker_ms": 7390.0, "total_ms": 9000.0, "layout_ms": 1.0},
             "selection": {"code": "render_asset_selection", "referenced_images": 20, "selected_images": 16,
                           "dropped_images": 4, "selected_icons": 5},
             "diagnostics": [{"message": "SECRET-DIAG"}]},
            {"type": "render_error", "code": "render_timeout", "stage": "draw", "error_type": "RenderError",
             "duration_ms": 46000, "timings": {"queue_ms": 0.0, "prepare_ms": 900.0, "draw_ms": 45000.0,
                                               "total_ms": 46000.0},
             "selection": {"selected_images": 16, "selected_icons": 3}},
        ]
        observe = handlers.event_logger("abc123")
        with patch.object(handlers, "logger") as log:
            for event in events:
                observe(event)
            observe("not a dict")
        lines = [call.args[0].replace("{}", "%s") % call.args[1:] for call in log.info.call_args_list]
        joined = "\n".join(lines)
        self.assertEqual(len(lines), 7)
        self.assertTrue(all("req=abc123" in line for line in lines))
        self.assertIn("model_start req=abc123 round=1 reasoning_level=medium", joined)
        self.assertIn("tool_end req=abc123 round=1 name=web_search duration_ms=950.0 ok=True", joined)
        self.assertIn("round_timing req=abc123 round=1 model_ms=4100.5 tool_ms=950.0", joined)
        self.assertIn("queue_ms=0.1 prepare_ms=1500.0 draw_ms=7400.0 worker_ms=7390.0 total_ms=9000.0", joined)
        self.assertIn("selected_images=16 dropped_images=4 selected_icons=5", joined)
        self.assertIn("render_error req=abc123 code=render_timeout stage=draw error_type=RenderError", joined)
        for secret in ("SECRET-QUERY", "SECRET-RESULT", "SECRET-ANSWER", "SECRET-PROMPT", "SECRET-DIAG"):
            self.assertNotIn(secret, joined)


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

        from plugins.hyw import handlers

        modules = _fake_frontier(AsyncMock(side_effect=_model_failure()))
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
