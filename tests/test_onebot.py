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


HISTORY = {"status": "ok", "retcode": 0, "data": {"messages": [
    {"message_id": 537779999, "message_seq": 3969610, "message": [{"type": "forward", "data": {"id": "resid-1"}}]},
]}}


def _account(port, self_id, *, path="", platform="qq"):
    """A Satori account as Entari hands it to plugins: real ``ApiInfo``, its own login."""
    from satori.client.account import ApiInfo

    token = {5500: "satori-a", 5550: "satori-b", 5600: "satori-c"}.get(port)
    return SimpleNamespace(config=ApiInfo(host="127.0.0.1", port=port, path=path, token=token),
                           self_id=self_id, platform=platform)


class AccountActionTests(unittest.IsolatedAsyncioTestCase):
    """call_account_action：只用收到事件的那个号——它的 onebot_url，再退回同一连接的 Satori 透传。"""

    def setUp(self):
        self.seen: list[httpx.Request] = []
        self.answers: dict[str, object] = {}

        def handler(request: httpx.Request):
            self.seen.append(request)
            answer = self.answers.get(f"{request.url.host}:{request.url.port}{request.url.path}")
            if answer is None:
                raise httpx.ConnectError("refused", request=request)
            if isinstance(answer, httpx.Response):
                return answer
            return httpx.Response(200, json=answer)

        real = httpx.AsyncClient

        def client(**kwargs):
            return real(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

        for patcher in (mock.patch.object(onebot.httpx, "AsyncClient", client),
                        mock.patch.object(onebot, "ashared_ssl_context", mock.AsyncMock(return_value=None))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _env(self, **values):
        values.setdefault("SATORI_CLIENTS", CLIENTS)
        values.setdefault("ONEBOT_HTTP_URL", "http://127.0.0.1:9999")
        patcher = mock.patch.object(onebot, "_env", side_effect=_env(values))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_own_onebot_url_is_tried_first(self):
        self._env()
        self.answers["127.0.0.1:3000/get_group_msg_history"] = HISTORY
        result = await onebot.call_account_action(
            _account(5500, "111"), "get_group_msg_history", group_id=875241970, message_seq=3969610, count=10)
        self.assertEqual(result, HISTORY)
        self.assertEqual([str(request.url) for request in self.seen], ["http://127.0.0.1:3000/get_group_msg_history"])
        self.assertEqual(self.seen[0].headers["authorization"], "Bearer ob-a")
        self.assertEqual(json.loads(self.seen[0].content), {"group_id": 875241970, "message_seq": 3969610, "count": 10})

    async def test_without_onebot_url_the_satori_passthrough_route_is_used(self):
        self._env()
        self.answers["127.0.0.1:5600/v1/internal/onebot11/get_forward_msg"] = {"status": "ok", "data": {"messages": []}}
        await onebot.call_account_action(_account(5600, "333", platform="llonebot"), "get_forward_msg", id="resid-1")
        # 有两个以上连接时 ONEBOT_HTTP_URL 可能属于别的号，不用
        self.assertEqual([str(request.url) for request in self.seen],
                         ["http://127.0.0.1:5600/v1/internal/onebot11/get_forward_msg"])
        headers = self.seen[0].headers
        self.assertEqual(headers["authorization"], "Bearer satori-c")
        self.assertEqual(headers["satori-user-id"], "333")
        self.assertEqual(headers["satori-platform"], "llonebot")
        self.assertNotIn("x-self-id", headers)
        self.assertNotIn("x-platform", headers)
        self.assertEqual(json.loads(self.seen[0].content), {"id": "resid-1"})

    async def test_passthrough_keeps_the_connection_path(self):
        self._env(SATORI_CLIENTS=[], ONEBOT_HTTP_URL="")
        self.answers["127.0.0.1:5600/satori/v1/internal/onebot11/get_msg"] = {"status": "ok", "data": {}}
        await onebot.call_account_action(_account(5600, "333", path="satori"), "get_msg", message_id=1)
        self.assertEqual([str(request.url) for request in self.seen],
                         ["http://127.0.0.1:5600/satori/v1/internal/onebot11/get_msg"])

    async def test_the_receiving_account_is_the_one_used(self):
        self._env()
        # 5550 的 onebot_url（3001）没开：退回 5550 自己的透传，用的是这个号的 token 与 id
        self.answers["127.0.0.1:5550/v1/internal/onebot11/get_group_msg_history"] = HISTORY
        self.answers["127.0.0.1:3000/get_group_msg_history"] = {"status": "failed", "wording": "wrong account"}
        await onebot.call_account_action(_account(5550, "222"), "get_group_msg_history", group_id=1, message_seq=2, count=10)
        self.assertEqual([str(request.url) for request in self.seen], [
            "http://127.0.0.1:3001/get_group_msg_history",
            "http://127.0.0.1:5550/v1/internal/onebot11/get_group_msg_history",
        ])
        passthrough = self.seen[-1].headers
        self.assertEqual((passthrough["authorization"], passthrough["satori-user-id"]), ("Bearer satori-b", "222"))

    async def test_onebot_http_url_counts_as_the_account_server_with_a_single_connection(self):
        self._env(SATORI_CLIENTS=[{"host": "127.0.0.1", "port": 5600, "token": "satori-c"}], ONEBOT_ACCESS_TOKEN="global")
        self.answers["127.0.0.1:9999/get_msg"] = {"status": "ok", "data": {}}
        await onebot.call_account_action(_account(5600, "333"), "get_msg", message_id=1)
        self.assertEqual([str(request.url) for request in self.seen], ["http://127.0.0.1:9999/get_msg"])
        self.assertEqual(self.seen[0].headers["authorization"], "Bearer global")

    async def test_a_onebot_failure_is_reported_as_answered_without_tokens(self):
        self._env()
        self.answers["127.0.0.1:3000/get_group_msg_history"] = {"status": "failed", "retcode": 1200, "wording": "ob-a 不在群"}
        self.answers["127.0.0.1:5500/v1/internal/onebot11/get_group_msg_history"] = httpx.Response(
            500, json={"message": "Cannot read properties of undefined (reading 'start')"})
        with self.assertRaises(onebot.OneBotUnavailable) as caught:
            await onebot.call_account_action(_account(5500, "111"), "get_group_msg_history", group_id=1, message_seq=2, count=10)
        self.assertTrue(caught.exception.answered)
        message = str(caught.exception)
        self.assertIn("retcode=1200", message)
        self.assertIn("HTTP 500: Cannot read properties of undefined (reading 'start')", message)
        self.assertNotIn("ob-a", message)
        self.assertNotIn("satori-a", message)

    async def test_unreachable_channels_are_not_answered(self):
        self._env()
        with self.assertRaises(onebot.OneBotUnavailable) as caught:
            await onebot.call_account_action(_account(5500, "111"), "get_msg", message_id=1)
        self.assertFalse(caught.exception.answered)
        self.assertIn("ConnectError", str(caught.exception))
        self._env(SATORI_CLIENTS=[], ONEBOT_HTTP_URL="")
        with self.assertRaises(onebot.OneBotUnavailable) as caught:
            await onebot.call_account_action(SimpleNamespace(), "get_msg", message_id=1)
        self.assertFalse(caught.exception.answered)
        self.assertIn("没有可用通道", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
