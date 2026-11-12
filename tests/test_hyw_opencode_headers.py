from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import patch

import pytest

pytest.importorskip("hyw_frontier")
openai = pytest.importorskip("openai")
httpx2 = pytest.importorskip("httpx2")

from hyw_frontier import model_backend
from hyw_frontier.credentials import ModelConnection

from otae_bot.config import settings
from plugins.hyw.config import HywConfig

OPENCODE = "https://opencode.ai/inference/go/openai/v1"
MODEL = "deepseek-v4.1-flash"
REQUEST = {"command": "complete", "model": MODEL,
           "context": {"systemPrompt": "s", "tools": [], "messages": [{"role": "user", "content": "hi"}]}}
REPLY = {"id": "c1", "object": "chat.completion", "created": 0, "model": MODEL,
         "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


class OpenCodeHeaderTests(unittest.TestCase):
    def test_only_opencode_endpoints_get_headers(self):
        for url in (OPENCODE, "https://api.opencode.ai/v1", "https://relay.example/inference/go/openai/v1"):
            headers = model_backend.opencode_headers(url)
            self.assertEqual(set(headers), {"x-opencode-session", "User-Agent"}, url)
            self.assertEqual(headers["User-Agent"], "hyw-frontier/0.1.0")
        for url in ("https://openrouter.ai/api/v1", "https://api.deepseek.com", "https://notopencode.ai/v1", "", None):
            self.assertEqual(model_backend.opencode_headers(url), {}, url)

    def test_session_is_stable_per_process_and_env_overrides_it(self):
        with patch.dict(os.environ):
            os.environ.pop("HYW_OPENCODE_SESSION", None)
            first = model_backend.opencode_headers(OPENCODE)["x-opencode-session"]
            self.assertEqual(model_backend.opencode_headers(OPENCODE)["x-opencode-session"], first)
            uuid.UUID(first)
        with patch.dict(os.environ, {"HYW_OPENCODE_SESSION": " fixed-session "}):
            self.assertEqual(model_backend.opencode_headers(OPENCODE)["x-opencode-session"], "fixed-session")

    def test_plugin_passes_the_opencode_url_through_unchanged(self):
        values = {"api_key": "k", "base_url": OPENCODE + "/", "model": MODEL}
        with settings.override({"plugins": {"plugins.hyw": values}}):
            config = HywConfig.from_settings()
        self.assertEqual((config.base_url, config.provider, config.api), (OPENCODE, "openai-compatible", "chat"))


class WireHeaderTests(unittest.TestCase):
    """Real AsyncOpenAI + Pydantic AI over a mock transport: assert what actually goes on the wire."""

    def setUp(self):
        self.clients, self.sent = [], []
        real = openai.AsyncOpenAI

        def reply(request):
            self.sent.append(request)
            return httpx2.Response(200, json=REPLY)

        def client(**kwargs):
            self.clients.append(kwargs)
            return real(**kwargs, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(reply)))

        patcher = patch("openai.AsyncOpenAI", side_effect=client)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HYW_OPENCODE_SESSION", None)

    def ask(self, base_url):
        session = model_backend.ModelSession(
            ModelConnection("openai-compatible", "chat", base_url, "sk-test", max_output_tokens=1024), 10)
        try:
            result = session.call(REQUEST)
        finally:
            session.close()
        self.assertEqual(result["content"], [{"type": "text", "text": "ok"}])
        return self.clients[-1], self.sent[-1]

    def test_opencode_requests_carry_one_session_and_our_user_agent(self):
        kwargs, first = self.ask(OPENCODE)
        _, second = self.ask(OPENCODE)
        session = model_backend.opencode_session_id()
        self.assertEqual(kwargs["default_headers"], {"x-opencode-session": session, "User-Agent": "hyw-frontier/0.1.0"})
        self.assertEqual(str(first.url), OPENCODE + "/chat/completions")
        for sent in (first, second):
            self.assertEqual(sent.headers["x-opencode-session"], session)
            self.assertEqual(sent.headers["user-agent"], "hyw-frontier/0.1.0")

    def test_other_endpoints_are_unchanged(self):
        kwargs, sent = self.ask("https://relay.example/v1")
        self.assertIsNone(kwargs["default_headers"])
        self.assertNotIn("x-opencode-session", sent.headers)
        self.assertTrue(sent.headers["user-agent"].startswith("pydantic-ai/"), sent.headers["user-agent"])


if __name__ == "__main__":
    unittest.main()
