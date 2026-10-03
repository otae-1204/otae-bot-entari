"""OneBot HTTP 兜底按账号选地址：两个 LLBot 实例各有自己的 OneBot 端口。"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx

from otae_bot.adapters import onebot

CLIENTS = [
    {"host": "127.0.0.1", "port": 5500, "token": "satori-a", "onebot_url": "http://127.0.0.1:3000/", "onebot_token": "ob-a"},
    {"host": "127.0.0.1", "port": "5550", "token": "satori-b", "onebot_url": "http://127.0.0.1:3001"},
    {"host": "127.0.0.1", "port": 5600, "token": "satori-c"},
]


def _env(values):
    def read(key, default=None):
        return values.get(key, default)
    return read


def _bot(port):
    return SimpleNamespace(config=SimpleNamespace(host="127.0.0.1", port=port))


class EndpointTests(unittest.TestCase):
    def test_each_account_uses_its_own_onebot_endpoint(self):
        values = {"SATORI_CLIENTS": CLIENTS, "ONEBOT_HTTP_URL": "http://127.0.0.1:9999"}
        with mock.patch.object(onebot, "_env", side_effect=_env(values)):
            self.assertEqual(onebot._endpoints(_bot(5500)), [("http://127.0.0.1:3000", "ob-a")])
            # 没写 onebot_token：用全局 ONEBOT_ACCESS_TOKEN，再退回本连接的 token
            self.assertEqual(onebot._endpoints(_bot(5550)), [("http://127.0.0.1:3001", "satori-b")])
        values["ONEBOT_ACCESS_TOKEN"] = "global"
        with mock.patch.object(onebot, "_env", side_effect=_env(values)):
            self.assertEqual(onebot._endpoints(_bot(5550)), [("http://127.0.0.1:3001", "global")])

    def test_accounts_without_onebot_url_keep_the_previous_list(self):
        values = {"SATORI_CLIENTS": CLIENTS, "ONEBOT_HTTP_URL": "http://127.0.0.1:9999"}
        with mock.patch.object(onebot, "_env", side_effect=_env(values)):
            expected = [(url, "satori-a") for url in onebot._base_urls()]
            self.assertEqual(onebot._endpoints(_bot(5600)), expected)
            self.assertEqual(onebot._endpoints(SimpleNamespace()), expected)
        self.assertEqual(expected[0][0], "http://127.0.0.1:9999")


class CallActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_fallback_posts_to_the_account_endpoint_with_its_token(self):
        seen = []

        def handler(request: httpx.Request):
            seen.append((str(request.url), request.headers.get("authorization"), json.loads(request.content)))
            return httpx.Response(200, json={"status": "ok", "data": {"messages": []}})

        real = httpx.AsyncClient

        def client(**kwargs):
            return real(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

        bot = _bot(5550)
        bot.internal = mock.AsyncMock(side_effect=RuntimeError("no internal route"))
        values = {"SATORI_CLIENTS": CLIENTS, "ONEBOT_HTTP_URL": "http://127.0.0.1:9999"}
        with mock.patch.object(onebot, "_env", side_effect=_env(values)), \
             mock.patch.object(onebot.httpx, "AsyncClient", client), \
             mock.patch.object(onebot, "ashared_ssl_context", mock.AsyncMock(return_value=None)):
            result = await onebot.call_onebot_action(bot, "get_forward_msg", message_id="fwd-1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(seen, [("http://127.0.0.1:3001/get_forward_msg", "Bearer satori-b", {"message_id": "fwd-1"})])


if __name__ == "__main__":
    unittest.main()
