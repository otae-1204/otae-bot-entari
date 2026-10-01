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
