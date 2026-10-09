"""Arknights Skland binding + attendance regression tests.

Everything here runs against synthetic data: ``httpx.MockTransport`` for the
protocol, temporary SQLite files for storage, and fabricated roles/UIDs for the
renderer.  No real account, credential, or network call is involved.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import importlib.util
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from satori import ChannelType

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "arknights_account_for_test"


def _load(name: str, relative_path: str):
    if "." in name:
        importlib.import_module(name.rpartition(".")[0])
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT / "plugins/arknights")]
    sys.modules[PACKAGE] = package

crypto = _load(f"{PACKAGE}.crypto", "plugins/arknights/crypto.py")
store_module = _load(f"{PACKAGE}.store", "plugins/arknights/store.py")
client_module = _load(f"{PACKAGE}.client", "plugins/arknights/client.py")
commands_module = _load(f"{PACKAGE}.commands", "plugins/arknights/commands.py")
models_module = _load(f"{PACKAGE}.models", "plugins/arknights/models.py")
tasks_module = _load(f"{PACKAGE}.tasks", "plugins/arknights/tasks.py")
attendance_module = _load(f"{PACKAGE}.attendance", "plugins/arknights/attendance.py")
cards_module = _load(f"{PACKAGE}.rendering.cards", "plugins/arknights/rendering/cards.py")


def role(uid: str, game_id: str, nickname: str, *, channel: str = "官服", primary: bool = False, role_id: int = 1):
    return store_module.ArknightsRole(
        id=role_id,
        credential_id=role_id,
        qq_user_id="7",
        uid=uid,
        game_id=game_id,
        channel_name=channel,
        nickname=nickname,
        is_primary=primary,
    )


def binding_payload() -> dict:
    return {
        "code": 0,
        "data": {
            "list": [
                {
                    "appCode": "endfield",
                    "appName": "明日方舟：终末地",
                    "bindingList": [{"uid": "endfield-uid", "roles": []}],
                },
                {
                    "appCode": "arknights",
                    "appName": "明日方舟",
                    "bindingList": [
                        {
                            "uid": "10001234",
                            "channelMasterId": "1",
                            "gameId": 1,
                            "channelName": "官服",
                            "nickName": "博士#1234",
                            "isOfficial": True,
                        },
                        {
                            "uid": "20005678",
                            "channelMasterId": "2",
                            "gameId": 1,
                            "channelName": "B服",
                            "nickName": "小号",
                            "isOfficial": False,
                        },
                    ],
                },
            ]
        },
    }


def attendance_success_payload() -> dict:
    return {
        "code": 0,
        "message": "OK",
        "data": {
            "ts": "1693823939",
            "awards": [
                {
                    "resource": {"id": "4003", "type": "DIAMOND_SHD", "name": "合成玉", "rarity": 4},
                    "count": 500,
                    "type": "first",
                },
                {
                    "resource": {"id": "2002", "type": "CARD_EXP", "name": "初级作战记录", "rarity": 2},
                    "count": 3,
                    "type": "daily",
                },
            ],
        },
    }


# --------------------------------------------------------------------- signing


class ArknightsSigningTests(unittest.IsolatedAsyncioTestCase):
    def test_canonical_string_matches_the_documented_example(self):
        canonical = client_module.sign_canonical_string(
            "/api/v1/game/player/binding", "", "1695184599"
        )
        self.assertEqual(
            canonical,
            "/api/v1/game/player/binding1695184599"
            '{"platform":"1","timestamp":"1695184599",'
            '"dId":"de9759a5afaa634f","vName":"1.45.1"}',
        )
        digest = hmac.new(b"token", canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        sign, headers = client_module.build_signature(
            "token", "/api/v1/game/player/binding", "", "1695184599"
        )
        self.assertEqual(sign, hashlib.md5(digest.encode("utf-8")).hexdigest())
        # Field order is part of the protocol and must never be reordered.
        self.assertEqual(list(headers), ["platform", "timestamp", "dId", "vName"])

    def test_attendance_body_is_compact_and_stable(self):
        self.assertEqual(
            client_module.build_attendance_body("10001234", "1"),
            '{"uid":"10001234","gameId":"1"}',
        )

    def test_business_code_sanitizer_never_forwards_free_text(self):
        self.assertEqual(client_module.safe_business_code("10001"), "10001")
        self.assertEqual(client_module.safe_business_code(404), "404")
        self.assertEqual(client_module.safe_business_code(0), "0")
        self.assertEqual(client_module.safe_business_code(None), "")
        self.assertEqual(client_module.safe_business_code("  "), "")
        # Surrounding whitespace is trimmed, then the digits pass through.
        self.assertEqual(client_module.safe_business_code(" 404\n"), "404")
        # Only plain digits may reach chat/logs: a hex blob, token fragment or
        # any other free text must collapse to a neutral marker.
        for hostile in (
            "token=secret\n第二行",
            "deadbeefcafe",
            "abc123",
            "-1",
            "10001.0",
            "Bearer abc.def",
        ):
            with self.subTest(hostile=hostile):
                self.assertEqual(client_module.safe_business_code(hostile), "unknown")

    async def test_signed_post_bytes_equal_the_signed_body(self):
        captured: list[httpx.Request] = []

        async def handler(request: httpx.Request):
            captured.append(request)
            if request.url.host == "as.hypergryph.com":
                return httpx.Response(200, json={"status": 0, "data": {"code": "oauth-code"}})
            if request.url.path.endswith("/user/auth/generate_cred_by_code"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "timestamp": 1000,
                        "data": {"cred": "cred-value", "token": "sign-token"},
                    },
                )
            if request.url.path == client_module.ATTENDANCE_PATH and request.method == "GET":
                return httpx.Response(200, json={"code": 0, "data": {"records": []}})
            if request.url.path == client_module.ATTENDANCE_PATH and request.method == "POST":
                return httpx.Response(200, json=attendance_success_payload())
            raise AssertionError(str(request.url))

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.ArknightsClient(http)
        with mock.patch.object(client_module.time, "time", return_value=1000):
            result = await client.attendance(
                "account-token", SimpleNamespace(uid="10001234", game_id="1")
            )
        await http.aclose()

        self.assertEqual(result.status, "success")
        self.assertEqual(
            [(item.name, item.count) for item in result.rewards],
            [("合成玉", 500), ("初级作战记录", 3)],
        )

        post = next(
            request
            for request in captured
            if request.method == "POST" and request.url.path == client_module.ATTENDANCE_PATH
        )
        self.assertEqual(post.content, b'{"uid":"10001234","gameId":"1"}')
        expected = client_module.sign_canonical_string(
            client_module.ATTENDANCE_PATH, post.content.decode("utf-8"), "1000"
        )
        digest = hmac.new(b"sign-token", expected.encode("utf-8"), hashlib.sha256).hexdigest()
        self.assertEqual(post.headers["sign"], hashlib.md5(digest.encode()).hexdigest())
        self.assertEqual(post.headers["cred"], "cred-value")
        self.assertEqual(post.headers["platform"], "1")

        status = next(
            request
            for request in captured
            if request.method == "GET" and request.url.path == client_module.ATTENDANCE_PATH
        )
        self.assertEqual(status.url.query, b"uid=10001234&gameId=1")
        self.assertEqual(
            status.headers["sign"],
            client_module.build_signature(
                "sign-token",
                client_module.ATTENDANCE_PATH,
                "uid=10001234&gameId=1",
                "1000",
            )[0],
        )

    async def test_binding_request_signs_an_empty_query(self):
        captured: list[httpx.Request] = []

        async def handler(request: httpx.Request):
            captured.append(request)
            if request.url.host == "as.hypergryph.com":
                return httpx.Response(200, json={"status": 0, "data": {"code": "oauth-code"}})
            if request.url.path.endswith("/user/auth/generate_cred_by_code"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "timestamp": 1000,
                        "data": {"cred": "cred-value", "token": "sign-token"},
                    },
                )
            if request.url.path == client_module.BINDING_PATH:
                return httpx.Response(200, json=binding_payload())
            raise AssertionError(str(request.url))

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.ArknightsClient(http)
        with mock.patch.object(client_module.time, "time", return_value=1000):
            roles = await client.discover_roles("account-token")
        await http.aclose()

        self.assertEqual(
            [(item.uid, item.game_id, item.channel_name) for item in roles],
            [("10001234", "1", "官服"), ("20005678", "1", "B服")],
        )
        binding = next(request for request in captured if request.url.path == client_module.BINDING_PATH)
        self.assertEqual(binding.url.query, b"")
        self.assertEqual(
            binding.headers["sign"],
            client_module.build_signature(
                "sign-token", client_module.BINDING_PATH, "", "1000"
            )[0],
        )


# ------------------------------------------------------------------- parsing


class ArknightsBindingParseTests(unittest.TestCase):
    def test_keeps_only_arknights_channels(self):
        roles = client_module.parse_arknights_bindings(binding_payload())
        self.assertEqual(
            [(item.uid, item.game_id, item.nickname, item.channel_name) for item in roles],
            [
                ("10001234", "1", "博士#1234", "官服"),
                ("20005678", "1", "小号", "B服"),
            ],
        )

    def test_ignores_other_apps_missing_fields_and_duplicates(self):
        payload = {
            "code": 0,
            "data": {
                "list": [
                    {"appCode": "ArKnIgHtS", "bindingList": [
                        {"uid": "1", "channelMasterId": "1", "channelName": "官服"},
                        {"uid": "1", "channelMasterId": "1", "channelName": "官服"},
                        {"uid": "1"},
                        {"channelMasterId": "1"},
                        "not-a-dict",
                    ]},
                    {"appCode": "endfield", "bindingList": [{"uid": "ef", "channelMasterId": "1"}]},
                ]
            },
        }
        roles = client_module.parse_arknights_bindings(payload)
        self.assertEqual([item.uid for item in roles], ["1"])
        # A nickname-less binding still yields a usable label.
        self.assertTrue(roles[0].nickname)

    def test_missing_or_malformed_data_yields_no_roles(self):
        self.assertEqual(client_module.parse_arknights_bindings({}), [])
        self.assertEqual(client_module.parse_arknights_bindings({"data": None}), [])
        self.assertEqual(client_module.parse_arknights_bindings({"data": {"list": "x"}}), [])

    def test_localized_nickname_and_channel(self):
        payload = {
            "code": 0,
            "data": {"list": [{"appCode": "arknights", "bindingList": [{
                "uid": "9", "channelMasterId": "1",
                "channelName": {"zh": "官服", "en": "Official"},
                "nickName": {"zh-CN": "中文博士", "en": "Doctor"},
            }]}]},
        }
        parsed = client_module.parse_arknights_bindings(payload)
        self.assertEqual((parsed[0].nickname, parsed[0].channel_name), ("中文博士", "官服"))

    def test_awards_parse_name_and_count_with_fallbacks(self):
        payload = {
            "code": 0,
            "data": {"awards": [
                {"resource": {"name": "合成玉"}, "count": 500},
                {"resource": {"id": "4003"}, "count": "3"},
                {"resource": {}, "count": None},
                "junk",
            ]},
        }
        self.assertEqual(
            [(item.name, item.count) for item in client_module.parse_attendance_awards(payload)],
            [("合成玉", 500), ("4003", 3), ("签到奖励", 0)],
        )
        self.assertEqual(client_module.parse_attendance_awards({"data": {}}), ())

    def test_reward_count_never_invents_a_quantity(self):
        # A usable quantity is preserved exactly — including an explicit 1.
        self.assertEqual(client_module.reward_count(1), 1)
        self.assertEqual(client_module.reward_count(500), 500)
        self.assertEqual(client_module.reward_count("12"), 12)
        self.assertEqual(client_module.reward_count(0), 0)
        # Malformed input becomes 0, never a fabricated 1.
        for malformed in (None, "", "   ", "abc", "1.5", -5, True, False, [], {}, [1]):
            with self.subTest(malformed=malformed):
                self.assertEqual(client_module.reward_count(malformed), 0)

    def test_missing_resource_keeps_the_award_without_overstating_it(self):
        payload = {
            "data": {"awards": [
                {"count": 1},
                {"resource": {"name": "龙门币"}, "count": 0},
                {"resource": {"name": "龙门币"}, "count": None},
            ]}
        }
        self.assertEqual(
            [(item.name, item.count) for item in client_module.parse_attendance_awards(payload)],
            [("签到奖励", 1), ("龙门币", 0), ("龙门币", 0)],
        )

    def test_extract_account_token_accepts_raw_and_web_json(self):
        self.assertEqual(client_module.extract_account_token("raw-token"), "raw-token")
        self.assertEqual(
            client_module.extract_account_token('{"code":0,"data":{"content":"inner"}}'),
            "inner",
        )
        self.assertEqual(client_module.extract_account_token('{"code":0,"data":{}}'), "")
        self.assertEqual(client_module.extract_account_token(""), "")


# ------------------------------------------------------------- client flows


class ArknightsClientFlowTests(unittest.IsolatedAsyncioTestCase):
    def test_default_client_uses_the_shared_tls_context_and_ignores_proxies(self):
        with mock.patch.object(client_module.httpx, "AsyncClient") as async_client:
            client = client_module.ArknightsClient()

        async_client.assert_called_once_with(
            timeout=25.0,
            follow_redirects=True,
            trust_env=False,
            verify=client_module.shared_ssl_context(trust_env=False),
        )
        self.assertIs(client.http, async_client.return_value)
        self.assertTrue(client._owns_http)

    async def test_credential_falls_back_to_second_route_with_fresh_code(self):
        paths: list[str] = []
        grants = 0

        async def handler(request: httpx.Request):
            nonlocal grants
            paths.append(request.url.path)
            if request.url.host == "as.hypergryph.com":
                grants += 1
                return httpx.Response(200, json={"status": 0, "data": {"code": f"oauth-{grants}"}})
            if request.url.path == "/api/v1/user/auth/generate_cred_by_code":
                return httpx.Response(200, json={"code": 404, "message": "Not Found"})
            if request.url.path == "/web/v1/user/auth/generate_cred_by_code":
                self.assertEqual(json.loads(request.content)["code"], "oauth-2")
                return httpx.Response(200, json={"code": 0, "timestamp": 1000, "data": {"cred": "c", "token": "t"}})
            raise AssertionError(str(request.url))

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.ArknightsClient(http)
        with mock.patch.object(client_module.time, "time", return_value=1000):
            context = await client._context("account-token")
        await http.aclose()

        self.assertEqual((context.cred, context.sign_token), ("c", "t"))
        self.assertEqual(grants, 2)
        self.assertEqual(paths[:3], [
            "/user/oauth2/v2/grant",
            "/api/v1/user/auth/generate_cred_by_code",
            "/user/oauth2/v2/grant",
        ])

    async def test_missing_sign_token_is_refreshed(self):
        async def handler(request: httpx.Request):
            if request.url.host == "as.hypergryph.com":
                return httpx.Response(200, json={"status": 0, "data": {"code": "oauth"}})
            if request.url.path.endswith("/user/auth/generate_cred_by_code"):
                return httpx.Response(200, json={"code": 0, "data": {"cred": "cred-only"}})
            if request.url.path == client_module.REFRESH_PATH:
                self.assertEqual(request.headers["cred"], "cred-only")
                return httpx.Response(
                    200, json={"code": 0, "timestamp": 1000, "data": {"salt": "fresh-salt"}}
                )
            raise AssertionError(str(request.url))

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.ArknightsClient(http)
        with mock.patch.object(client_module.time, "time", return_value=1000):
            context = await client._context("account-token")
        await http.aclose()

        self.assertEqual((context.cred, context.sign_token), ("cred-only", "fresh-salt"))

    async def test_context_is_reused_between_requests(self):
        grants = 0

        async def handler(request: httpx.Request):
            nonlocal grants
            if request.url.host == "as.hypergryph.com":
                grants += 1
                return httpx.Response(200, json={"status": 0, "data": {"code": "oauth"}})
            if request.url.path.endswith("/user/auth/generate_cred_by_code"):
                return httpx.Response(200, json={"code": 0, "data": {"cred": "c", "token": "t"}})
            if request.url.path == client_module.BINDING_PATH:
                return httpx.Response(200, json=binding_payload())
            raise AssertionError(str(request.url))

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.ArknightsClient(http)
        with mock.patch.object(client_module.time, "time", return_value=1000):
            await client.discover_roles("account-token")
            await client.discover_roles("account-token")
        await http.aclose()
        self.assertEqual(grants, 1)

    async def test_account_credential_errors_are_sanitized(self):
        async def handler(_request: httpx.Request):
            return httpx.Response(
                200,
                json={
                    "code": 401,
                    "message": "cred=deadbeef token=super-secret https://internal.example/?t=1",
                },
            )

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.ArknightsClient(http)
        with self.assertRaises(client_module.ArknightsAPIError) as caught:
            await client.discover_roles("account-token")
        await http.aclose()

        text = str(caught.exception)
        self.assertNotIn("super-secret", text)
        self.assertNotIn("deadbeef", text)
        self.assertNotIn("http", text)
        self.assertIn("凭据已失效", text)
        # A credential failure must never look like an already-signed result.
        self.assertFalse(caught.exception.already_signed)

    async def test_account_credential_error_does_not_leak_server_message(self):
        async def handler(_request: httpx.Request):
            return httpx.Response(200, json={"code": 10002, "message": "内部追踪码 ABC-123"})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.ArknightsClient(http)
        with self.assertRaises(client_module.ArknightsAPIError) as caught:
            await client.discover_roles("account-token")
        await http.aclose()
        self.assertNotIn("ABC-123", str(caught.exception))
        self.assertNotIn("内部追踪码", str(caught.exception))
        self.assertIn("10002", str(caught.exception))

    async def test_network_and_malformed_responses_are_reported(self):
        async def boom(_request: httpx.Request):
            raise httpx.ConnectError("offline")

        http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        client = client_module.ArknightsClient(http)
        with self.assertRaises(client_module.ArknightsAPIError) as caught:
            await client._json_request("账号授权", "GET", "https://as.hypergryph.com/x")
        self.assertIn("网络请求失败", str(caught.exception))
        await http.aclose()

        for response, expected in (
            (httpx.Response(200, text="<html>not json</html>"), "无法解析"),
            (httpx.Response(500, text="<html>boom</html>"), "暂时不可用"),
        ):
            async def handler(_request: httpx.Request, response=response):
                return response

            http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            client = client_module.ArknightsClient(http)
            with self.assertRaises(client_module.ArknightsAPIError) as caught:
                await client._json_request("账号授权", "GET", "https://as.hypergryph.com/x")
            self.assertIn(expected, str(caught.exception))
            await http.aclose()

    async def test_already_signed_code_only_applies_to_attendance(self):
        async def handler(request: httpx.Request):
            if request.url.host == "as.hypergryph.com":
                return httpx.Response(200, json={"status": 0, "data": {"code": "oauth"}})
            if request.url.path.endswith("/user/auth/generate_cred_by_code"):
                return httpx.Response(200, json={"code": 0, "data": {"cred": "c", "token": "t"}})
            return httpx.Response(200, json={"code": 10001, "message": "已签到"})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = client_module.ArknightsClient(http)
        with self.assertRaises(client_module.ArknightsAPIError) as caught:
            await client.discover_roles("account-token")
        await http.aclose()
        self.assertFalse(caught.exception.already_signed)
        self.assertNotIn("已签到", str(caught.exception))


class _SklandServer:
    """Fake passport + Skland: each grant issues a new ``cred``; replies are scripted."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.grants = 0
        self.requests: list[tuple[str, str, str, str]] = []

    async def __call__(self, request: httpx.Request):
        if request.url.host == "as.hypergryph.com":
            self.grants += 1
            return httpx.Response(200, json={"status": 0, "data": {"code": f"oauth-{self.grants}"}})
        if request.url.path.endswith("/user/auth/generate_cred_by_code"):
            return httpx.Response(200, json={"code": 0, "data": {"cred": f"cred-{self.grants}", "token": "t"}})
        self.requests.append(
            (request.method, request.url.path, request.headers["cred"], request.content.decode())
        )
        if not self.replies:
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        return self.replies.pop(0)

    def posts(self):
        return [(cred, body) for method, _path, cred, body in self.requests if method == "POST"]


def _expired(code: int = 10000) -> httpx.Response:
    return httpx.Response(200, json={"code": code, "message": "用户未登录 cred=leaked"})


class ArknightsContextRetryTests(unittest.IsolatedAsyncioTestCase):
    """Rejected credentials (10000 / 10003 / 401) get one fresh context, then a rebind hint."""

    BILIBILI_ROLE = SimpleNamespace(uid="20005678", game_id="1", channel_name="B服")

    async def _run(self, server, call):
        http = httpx.AsyncClient(transport=httpx.MockTransport(server))
        client = client_module.ArknightsClient(http)
        try:
            return await call(client)
        finally:
            await http.aclose()

    async def test_a_rejected_attendance_is_retried_once_with_a_fresh_context(self):
        for rejection in (_expired(10000), _expired(10003), httpx.Response(401, text="Unauthorized")):
            with self.subTest(status=rejection.status_code, body=rejection.text[:20]):
                server = _SklandServer([
                    _expired(10000),  # status probe with the stale cred
                    rejection,  # the POST is rejected too: nothing was signed
                    httpx.Response(200, json={"code": 0, "data": {"records": []}}),
                    httpx.Response(200, json=attendance_success_payload()),
                    httpx.Response(200, json={"code": 0, "data": {"records": []}}),
                ])
                result = await self._run(
                    server, lambda client: client.attendance("account-token", self.BILIBILI_ROLE)
                )
                self.assertEqual(result.status, "success")
                self.assertEqual(server.grants, 2)
                body = '{"uid":"20005678","gameId":"1"}'
                self.assertEqual(server.posts(), [("cred-1", body), ("cred-2", body)])

    async def test_a_day_signed_before_the_rejection_is_reported_as_already(self):
        server = _SklandServer([
            _expired(10003),
            _expired(10003),
            httpx.Response(200, json={"code": 0, "data": {"hasToday": True}}),
        ])
        result = await self._run(
            server, lambda client: client.attendance("account-token", self.BILIBILI_ROLE)
        )
        self.assertEqual(result.status, "already")
        self.assertEqual(len(server.posts()), 1)  # only the rejected first attempt

    async def test_a_second_rejection_asks_the_user_to_bind_again(self):
        server = _SklandServer([_expired(10000)] * 4)
        with self.assertRaises(client_module.ArknightsAPIError) as caught:
            await self._run(
                server, lambda client: client.attendance("account-token", self.BILIBILI_ROLE)
            )
        message = str(caught.exception)
        self.assertIn("请重新私聊使用 /ak 绑定", message)
        self.assertNotIn("leaked", message)
        self.assertFalse(caught.exception.already_signed)
        self.assertEqual(server.grants, 2)  # exactly one retry
        self.assertEqual(len(server.posts()), 2)

    async def test_role_discovery_is_retried_after_an_http_401(self):
        server = _SklandServer([
            httpx.Response(401, json={"code": 0}),
            httpx.Response(200, json=binding_payload()),
        ])
        roles = await self._run(server, lambda client: client.discover_roles("account-token"))
        self.assertEqual([role.uid for role in roles], ["10001234", "20005678"])
        self.assertEqual([cred for _m, _p, cred, _b in server.requests], ["cred-1", "cred-2"])

    async def test_other_failures_are_not_retried(self):
        server = _SklandServer([
            httpx.Response(200, json={"code": 0, "data": {"records": []}}),
            httpx.Response(200, json={"code": 10002, "message": "活动未开始"}),
        ])
        with self.assertRaises(client_module.ArknightsAPIError) as caught:
            await self._run(
                server, lambda client: client.attendance("account-token", self.BILIBILI_ROLE)
            )
        self.assertEqual(caught.exception.code, "10002")
        self.assertEqual(server.grants, 1)

    async def test_concurrent_rejections_share_one_fresh_context(self):
        http = httpx.AsyncClient(transport=httpx.MockTransport(_SklandServer([])))
        client = client_module.ArknightsClient(http)
        self.addAsyncCleanup(http.aclose)
        stale = await client._context("account-token")
        fresh = await client._context("account-token", refresh=True, stale=stale)
        self.assertIsNot(fresh, stale)
        # A second request that was rejected with the same stale context reuses
        # the context the first one already exchanged.
        again = await client._context("account-token", refresh=True, stale=stale)
        self.assertIs(again, fresh)


class ArknightsAttendanceTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, handler) -> tuple[client_module.ArknightsClient, httpx.AsyncClient]:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return client_module.ArknightsClient(http), http

    async def test_explicit_status_record_reports_already_without_posting(self):
        attendances: list[str] = []

        async def handler(request: httpx.Request):
            if request.url.path == client_module.ATTENDANCE_PATH:
                attendances.append(request.method)
            if request.url.host == "as.hypergryph.com":
                return httpx.Response(200, json={"status": 0, "data": {"code": "oauth"}})
            if request.url.path.endswith("/user/auth/generate_cred_by_code"):
                return httpx.Response(200, json={"code": 0, "data": {"cred": "c", "token": "t"}})
            if request.method == "GET":
                # 2023-09-04T15:00:00Z == 2023-09-04 23:00 in UTC+8.
                return httpx.Response(200, json={"code": 0, "data": {"records": [{"ts": "1693843200"}]}})
            raise AssertionError("attendance POST must not run when today is already signed")

        client, http = self._client(handler)
        with mock.patch.object(
            client_module, "shanghai_date", return_value=client_module.date(2023, 9, 4)
        ):
            result = await client.attendance("account-token", SimpleNamespace(uid="1", game_id="1"))
        await http.aclose()

        self.assertEqual(result.status, "already")
        self.assertEqual(result.rewards, ())
        self.assertEqual(attendances, ["GET"])

    async def test_post_already_code_is_not_a_failure(self):
        async def handler(request: httpx.Request):
            if request.url.host == "as.hypergryph.com":
                return httpx.Response(200, json={"status": 0, "data": {"code": "oauth"}})
            if request.url.path.endswith("/user/auth/generate_cred_by_code"):
                return httpx.Response(200, json={"code": 0, "data": {"cred": "c", "token": "t"}})
            if request.method == "GET":
                return httpx.Response(200, json={"code": 0, "data": {"records": []}})
            return httpx.Response(403, json={"code": 10001, "message": "今天已经签到过了"})

        client, http = self._client(handler)
        result = await client.attendance("account-token", SimpleNamespace(uid="1", game_id="1"))
        await http.aclose()
        self.assertEqual(result.status, "already")
        self.assertNotIn("已经签到过了", result.message)

    async def test_status_probe_failure_falls_back_to_the_post(self):
        posted = False

        async def handler(request: httpx.Request):
            nonlocal posted
            if request.url.host == "as.hypergryph.com":
                return httpx.Response(200, json={"status": 0, "data": {"code": "oauth"}})
            if request.url.path.endswith("/user/auth/generate_cred_by_code"):
                return httpx.Response(200, json={"code": 0, "data": {"cred": "c", "token": "t"}})
            if request.method == "GET":
                return httpx.Response(503, text="busy")
            posted = True
            return httpx.Response(200, json=attendance_success_payload())

        client, http = self._client(handler)
        result = await client.attendance("account-token", SimpleNamespace(uid="1", game_id="1"))
        await http.aclose()
        self.assertTrue(posted)
        self.assertEqual(result.status, "success")

    async def test_success_without_award_payload_is_an_error(self):
        async def handler(request: httpx.Request):
            if request.url.host == "as.hypergryph.com":
                return httpx.Response(200, json={"status": 0, "data": {"code": "oauth"}})
            if request.url.path.endswith("/user/auth/generate_cred_by_code"):
                return httpx.Response(200, json={"code": 0, "data": {"cred": "c", "token": "t"}})
            if request.method == "GET":
                return httpx.Response(200, json={"code": 0, "data": {"records": []}})
            return httpx.Response(200, json={"code": 0})

        client, http = self._client(handler)
        with self.assertRaises(client_module.ArknightsAPIError) as caught:
            await client.attendance("account-token", SimpleNamespace(uid="1", game_id="1"))
        await http.aclose()
        self.assertIn("奖励明细", str(caught.exception))

    async def test_expired_credential_stays_a_failure(self):
        async def handler(request: httpx.Request):
            if request.url.host == "as.hypergryph.com":
                return httpx.Response(200, json={"status": 0, "data": {"code": "oauth"}})
            if request.url.path.endswith("/user/auth/generate_cred_by_code"):
                return httpx.Response(200, json={"code": 0, "data": {"cred": "c", "token": "t"}})
            return httpx.Response(200, json={"code": 401, "message": "登录失效"})

        client, http = self._client(handler)
        with self.assertRaises(client_module.ArknightsAPIError) as caught:
            await client.attendance("account-token", SimpleNamespace(uid="1", game_id="1"))
        await http.aclose()
        self.assertFalse(caught.exception.already_signed)
        self.assertIn("凭据已失效", str(caught.exception))

    def test_attendance_has_today_only_trusts_records_and_hastoday(self):
        today = client_module.datetime(2023, 9, 4, 12, tzinfo=client_module.timezone.utc)
        signed_ts = int(client_module.datetime(2023, 9, 4, 2, tzinfo=client_module.timezone.utc).timestamp())
        yesterday_ts = int(client_module.datetime(2023, 9, 3, 2, tzinfo=client_module.timezone.utc).timestamp())
        self.assertIsNone(client_module.attendance_has_today({"data": {"upload": []}}, now=today))
        self.assertIsNone(client_module.attendance_has_today({}, now=today))
        self.assertFalse(
            client_module.attendance_has_today({"data": {"records": [{"ts": str(yesterday_ts)}]}}, now=today)
        )
        self.assertTrue(
            client_module.attendance_has_today({"data": {"records": [{"ts": str(signed_ts)}]}}, now=today)
        )
        self.assertTrue(client_module.attendance_has_today({"data": {"hasToday": True}}, now=today))
        self.assertFalse(client_module.attendance_has_today({"data": {"hasToday": False}}, now=today))


# ------------------------------------------------------------------- crypto


class ArknightsCipherTests(unittest.TestCase):
    def test_aes_gcm_roundtrip_and_wrong_key(self):
        cipher = crypto.ArknightsCipher(b"k" * 32)
        encrypted = cipher.encrypt("hypergryph-account-token")
        self.assertNotIn(b"hypergryph-account-token", encrypted.ciphertext)
        self.assertEqual(cipher.decrypt(encrypted), "hypergryph-account-token")
        with self.assertRaises(crypto.CredentialKeyError):
            crypto.ArknightsCipher(b"x" * 32).decrypt(encrypted)

    def test_key_must_decode_to_exactly_32_bytes(self):
        with self.assertRaises(crypto.CredentialKeyError):
            crypto.ArknightsCipher(b"short")
        with mock.patch.dict(
            "os.environ",
            {crypto.KEY_ENV_NAME: base64.b64encode(b"short").decode()},
            clear=False,
        ), self.assertRaises(crypto.CredentialKeyError) as caught:
            crypto.ArknightsCipher.from_env()
        self.assertIn(crypto.KEY_ENV_NAME, str(caught.exception))

    def test_invalid_base64_names_the_offending_variable(self):
        with mock.patch.dict(
            "os.environ",
            {crypto.KEY_ENV_NAME: "not base64!!"},
            clear=False,
        ), self.assertRaises(crypto.CredentialKeyError) as caught:
            crypto.ArknightsCipher.from_env()
        self.assertIn(crypto.KEY_ENV_NAME, str(caught.exception))
        self.assertIn("Base64", str(caught.exception))

    def test_the_endfield_key_is_never_a_fallback(self):
        endfield_key = base64.b64encode(b"f" * 32).decode()
        for arknights_key in ("", "   "):
            with self.subTest(arknights_key=arknights_key), mock.patch.dict(
                "os.environ",
                {crypto.KEY_ENV_NAME: arknights_key, "ENDFIELD_CREDENTIAL_KEY": endfield_key},
                clear=False,
            ), self.assertRaises(crypto.CredentialKeyError) as caught:
                crypto.ArknightsCipher.from_env()
            message = str(caught.exception)
            self.assertIn("ARKNIGHTS_CREDENTIAL_KEY", message)
            self.assertNotIn("ENDFIELD", message)

    def test_missing_key_message_is_actionable(self):
        with mock.patch.dict(
            "os.environ", {crypto.KEY_ENV_NAME: ""}, clear=False
        ), self.assertRaises(crypto.CredentialKeyError) as caught:
            crypto.ArknightsCipher.from_env()
        message = str(caught.exception)
        self.assertIn("未配置", message)
        self.assertIn("ARKNIGHTS_CREDENTIAL_KEY", message)

    def test_only_the_arknights_key_is_read(self):
        source = (ROOT / "plugins/arknights/crypto.py").read_text(encoding="utf-8")
        code = source.split('"""', 2)[2]  # skip the module docstring that explains why
        self.assertNotIn("ENDFIELD_CREDENTIAL_KEY", code)
        self.assertEqual(crypto.KEY_ENV_NAME, "ARKNIGHTS_CREDENTIAL_KEY")


# -------------------------------------------------------------------- store


class ArknightsStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "arknights.db"
        self.store = store_module.ArknightsStore(self.path)
        self.addCleanup(self.store.close)
        self.cipher = crypto.ArknightsCipher(b"k" * 32)

    def _bind(self, qq="7", token="token-a", roles=None):
        return self.store.bind_roles(
            qq,
            token,
            roles
            or [
                store_module.RoleCandidate("10001234", "1", "甲", "官服"),
                store_module.RoleCandidate("20005678", "2", "乙", "B服"),
            ],
            self.cipher,
        )

    def test_binding_is_deduplicated_and_updates_in_place(self):
        first = self._bind()
        self.assertEqual([item.nickname for item in first], ["甲", "乙"])
        second = self._bind(
            roles=[store_module.RoleCandidate("10001234", "1", "甲改名", "官服")]
        )
        self.assertEqual(len(second), 2)
        self.assertEqual(second[0].nickname, "甲改名")
        self.assertEqual(self.store.credential_count(), 1)

    def test_decrypt_token_is_scoped_to_the_role_owner(self):
        bound = self._bind(qq="7")[0]
        foreign = store_module.ArknightsRole(
            id=bound.id,
            credential_id=bound.credential_id,
            qq_user_id="8",  # another user must not be able to read this credential
            uid=bound.uid,
            game_id=bound.game_id,
            channel_name=bound.channel_name,
            nickname=bound.nickname,
            is_primary=bound.is_primary,
        )
        with self.assertRaises(LookupError):
            self.store.decrypt_token(foreign, self.cipher)
        # The rightful owner still reads it.
        self.assertEqual(self.store.decrypt_token(bound, self.cipher), "token-a")

    def test_multiple_login_accounts_keep_separate_credentials(self):
        self._bind(token="token-a")
        roles = self._bind(
            token="token-b",
            roles=[store_module.RoleCandidate("30009999", "1", "丙", "官服")],
        )
        self.assertEqual(len(roles), 3)
        self.assertEqual(self.store.credential_count(), 2)
        self.assertEqual(
            {item.credential_id for item in roles[:2]}.isdisjoint({roles[2].credential_id}),
            True,
        )
        self.assertEqual(self.store.decrypt_token(roles[0], self.cipher), "token-a")
        self.assertEqual(self.store.decrypt_token(roles[2], self.cipher), "token-b")

    def test_credentials_are_not_stored_in_plaintext(self):
        self._bind(token="very-secret-account-token")
        raw = self.path.read_bytes()
        self.assertNotIn(b"very-secret-account-token", raw)

    def test_accounts_are_isolated_per_qq_user(self):
        self._bind(qq="7")
        self._bind(qq="8", token="token-c", roles=[store_module.RoleCandidate("40001111", "1", "丁")])
        self.assertEqual([item.nickname for item in self.store.list_roles("7")], ["甲", "乙"])
        self.assertEqual([item.nickname for item in self.store.list_roles("8")], ["丁"])
        # A selector that exists for another QQ user never resolves here.
        self.assertEqual(self.store.resolve("8", "5678").reason, store_module.NOT_FOUND)
        self.assertIsNone(self.store.resolve("7", "1111").role)

    def test_selector_resolution_is_exact_and_reports_ambiguity(self):
        # Both official and B服 UIDs deliberately share the "1234" suffix.
        self._bind(
            roles=[
                store_module.RoleCandidate("10001234", "1", "同名", "官服"),
                store_module.RoleCandidate("20001234", "2", "同名", "B服"),
                store_module.RoleCandidate("30005678", "1", "独立", "官服"),
            ]
        )
        self.assertEqual(self.store.resolve("7", "主账号").role.uid, "10001234")
        self.assertEqual(self.store.resolve("7", "1").role.uid, "10001234")
        self.assertEqual(self.store.resolve("7", "5678").role.uid, "30005678")
        self.assertEqual(self.store.resolve("7", "30005678").role.uid, "30005678")
        self.assertEqual(self.store.resolve("7", "独立").role.uid, "30005678")
        self.assertEqual(self.store.resolve("7", "名").reason, store_module.NOT_FOUND)
        # A short fragment is not a UID suffix: the ≥4 character rule holds.
        self.assertEqual(self.store.resolve("7", "234").reason, store_module.NOT_FOUND)

        ambiguous = self.store.resolve("7", "同名")
        self.assertEqual(ambiguous.reason, store_module.AMBIGUOUS)
        self.assertIsNone(ambiguous.role)
        self.assertEqual(len(ambiguous.candidates), 2)

        suffix = self.store.resolve("7", "1234")
        self.assertEqual(suffix.reason, store_module.AMBIGUOUS)
        self.assertIsNone(suffix.role)
        self.assertEqual(
            sorted(item.uid for item in suffix.candidates), ["10001234", "20001234"]
        )

    def test_selector_priority_is_exact_uid_then_suffix_then_index_then_nickname(self):
        self._bind(
            roles=[
                store_module.RoleCandidate("10001234", "1", "甲", "官服"),
                store_module.RoleCandidate("20001234", "2", "2", "B服"),
            ]
        )
        # Exact UID wins over every looser interpretation.
        self.assertEqual(self.store.resolve("7", "20001234").role.uid, "20001234")
        # A single digit is an index, which outranks an identical nickname ("2").
        self.assertEqual(self.store.resolve("7", "2").role.uid, "20001234")
        # A ≥4 digit selector that matches no UID suffix is NOT turned into an
        # index, so an out-of-range number can never select the wrong account.
        self.assertEqual(self.store.resolve("7", "5678").reason, store_module.NOT_FOUND)
        self.assertIsNone(self.store.resolve("7", "5678").role)
        # A full nickname still resolves when nothing numeric matches.
        self.assertEqual(self.store.resolve("7", "甲").role.uid, "10001234")

    def test_four_or_more_digits_never_fall_back_to_an_index(self):
        self._bind()  # 10001234 (#1) and 20005678 (#2)
        for selector in ("0001", "0002", "00001", "0000002"):
            with self.subTest(selector=selector):
                resolution = self.store.resolve("7", selector)
                self.assertEqual(resolution.reason, store_module.NOT_FOUND)
                self.assertIsNone(resolution.role)
                roles, _ = self.store.resolve_roles("7", selector)
                self.assertEqual(roles, ())
                self.assertFalse(self.store.set_primary("7", selector).resolved)
                self.assertFalse(self.store.unbind("7", selector).resolved)
        self.assertEqual([item.uid for item in self.store.list_roles("7")], ["10001234", "20005678"])
        self.assertEqual(self.store.list_roles("7")[0].is_primary, True)
        # Short numbers are still indexes, including full-width digits from an IME.
        self.assertEqual(self.store.resolve("7", "2").role.uid, "20005678")
        self.assertEqual(self.store.resolve("7", "02").role.uid, "20005678")
        self.assertEqual(self.store.resolve("7", "２").role.uid, "20005678")
        # Digits that are not decimal never crash the int() conversion.
        self.assertEqual(self.store.resolve("7", "²").reason, store_module.NOT_FOUND)

    def test_four_digits_that_are_a_uid_suffix_select_that_role(self):
        self._bind(
            roles=[
                store_module.RoleCandidate("10001234", "1", "甲", "官服"),
                store_module.RoleCandidate("30000001", "1", "丙", "B服"),
            ]
        )
        # "0001" is the B服 role's suffix, not role #1.
        self.assertEqual(self.store.resolve("7", "0001").role.uid, "30000001")
        removed = self.store.unbind("7", "0001")
        self.assertEqual(removed.role.uid, "30000001")
        self.assertEqual([item.uid for item in self.store.list_roles("7")], ["10001234"])

    def test_bulk_selector_is_refused_for_single_account_operations(self):
        self._bind()
        resolution = self.store.resolve("7", "全部")
        self.assertEqual(resolution.reason, store_module.BULK_NOT_ALLOWED)
        self.assertIsNone(self.store.unbind("7", "全部").role)
        self.assertEqual(len(self.store.list_roles("7")), 2)

    def test_unbind_removes_one_role_and_cleans_orphan_credentials(self):
        self._bind(
            token="token-a",
            roles=[store_module.RoleCandidate("10001234", "1", "甲", "官服")],
        )
        second = self._bind(
            token="token-b",
            roles=[store_module.RoleCandidate("30009999", "1", "丙", "官服")],
        )
        self.assertEqual(self.store.credential_count(), 2)
        removed = self.store.unbind("7", "甲")
        self.assertTrue(removed.resolved)
        self.assertEqual(removed.role.nickname, "甲")
        self.assertEqual([item.nickname for item in self.store.list_roles("7")], ["丙"])
        # token-a lost its only role, so its credential row must be gone.
        self.assertEqual(self.store.credential_count(), 1)
        self.assertEqual(self.store.decrypt_token(second[-1], self.cipher), "token-b")

    def test_unbind_keeps_a_credential_still_referenced_by_another_role(self):
        self._bind(token="token-a")
        self.assertEqual(self.store.credential_count(), 1)
        self.store.unbind("7", "甲")
        self.assertEqual([item.nickname for item in self.store.list_roles("7")], ["乙"])
        self.assertEqual(self.store.credential_count(), 1)

    def test_unbind_not_found_deletes_nothing(self):
        self._bind()
        resolution = self.store.unbind("7", "9999")
        self.assertEqual(resolution.reason, store_module.NOT_FOUND)
        self.assertEqual(len(self.store.list_roles("7")), 2)

    def test_primary_switch_and_replacement_after_unbind(self):
        self._bind()
        switched = self.store.set_primary("7", "2")
        self.assertTrue(switched.resolved)
        self.assertEqual(switched.role.nickname, "乙")
        self.assertTrue(switched.role.is_primary)
        self.store.unbind("7", "乙")
        remaining = self.store.list_roles("7")
        self.assertEqual(len(remaining), 1)
        self.assertTrue(remaining[0].is_primary)

    def test_resolve_roles_expands_all_by_default(self):
        self._bind()
        roles, resolution = self.store.resolve_roles("7", "")
        self.assertEqual(len(roles), 2)
        self.assertEqual(resolution.reason, store_module.ALL)
        single, resolution = self.store.resolve_roles("7", "乙")
        self.assertEqual([item.nickname for item in single], ["乙"])
        self.assertTrue(resolution.resolved)
        none, resolution = self.store.resolve_roles("7", "不存在")
        self.assertEqual(none, ())
        self.assertEqual(resolution.reason, store_module.NOT_FOUND)

    def test_empty_store_reports_no_binding(self):
        self.assertEqual(self.store.resolve("7", "1").reason, store_module.NO_BINDING)
        roles, resolution = self.store.resolve_roles("7", "")
        self.assertEqual((roles, resolution.reason), ((), store_module.NO_BINDING))


# ----------------------------------------------------------------- commands


class ArknightsCommandTests(unittest.TestCase):
    def test_parses_every_subcommand(self):
        self.assertEqual(commands_module.parse_command("").action, "help")
        self.assertEqual(commands_module.parse_command("帮助").action, "help")
        self.assertEqual(commands_module.parse_command("绑定").action, "bind")
        self.assertEqual(commands_module.parse_command("添加账号").action, "bind")
        self.assertEqual(commands_module.parse_command("账号").action, "accounts")
        self.assertEqual(commands_module.parse_command("accounts").action, "accounts")
        primary = commands_module.parse_command("主账号 2")
        self.assertEqual((primary.action, primary.selector), ("primary", "2"))
        unbind = commands_module.parse_command("解绑 甲")
        self.assertEqual((unbind.action, unbind.selector), ("unbind", "甲"))
        attendance = commands_module.parse_command("签到")
        self.assertEqual((attendance.action, attendance.selector), ("attendance", ""))
        selected = commands_module.parse_command("签到 1234")
        self.assertEqual((selected.action, selected.selector), ("attendance", "1234"))
        self.assertEqual(commands_module.parse_command("签到 全部").selector, "全部")

    def test_unknown_subcommand_returns_help_and_an_error(self):
        parsed = commands_module.parse_command("随便写点什么")
        self.assertTrue(parsed.error)
        self.assertIn("/ak 签到", parsed.error)

    def test_only_bind_and_unbind_require_a_private_chat(self):
        self.assertTrue(commands_module.requires_private_chat("bind"))
        self.assertTrue(commands_module.requires_private_chat("unbind"))
        for action in ("accounts", "primary", "attendance", "help"):
            self.assertFalse(commands_module.requires_private_chat(action))

    def test_help_documents_the_selector_forms(self):
        text = commands_module.format_help()
        for expected in ("/ak 绑定", "/ak 账号", "/ak 主账号", "/ak 解绑", "/ak 签到", "UID后四位"):
            self.assertIn(expected, text)

    def test_selector_failure_messages(self):
        ambiguous = store_module.SelectorResolution(
            None, store_module.AMBIGUOUS, (role("10001234", "1", "甲", primary=True), role("20001234", "2", "乙")), "1234"
        )
        text = commands_module.format_selector_failure(ambiguous)
        self.assertIn("匹配到多个角色", text)
        self.assertIn("****1234", text)
        self.assertNotIn("10001234", text)
        revealed = commands_module.format_selector_failure(ambiguous, reveal_uid=True)
        self.assertIn("10001234", revealed)

        self.assertIn(
            "尚未绑定",
            commands_module.format_selector_failure(
                store_module.SelectorResolution(None, store_module.NO_BINDING, (), "")
            ),
        )
        self.assertIn(
            "只能用于 /ak 签到",
            commands_module.format_selector_failure(
                store_module.SelectorResolution(None, store_module.BULK_NOT_ALLOWED, (), "全部")
            ),
        )

    def test_accounts_listing_masks_uids_in_group(self):
        roles = [role("10001234", "1", "甲", primary=True), role("20005678", "2", "乙", channel="B服")]
        group_text = commands_module.format_accounts(roles, reveal_uid=False)
        self.assertIn("****1234", group_text)
        self.assertNotIn("10001234", group_text)
        private_text = commands_module.format_accounts(roles, reveal_uid=True)
        self.assertIn("10001234", private_text)
        self.assertIn("[主账号]", private_text)


# ------------------------------------------------------- attendance service


class _FakeStore:
    def __init__(self, tokens: dict[str, str], broken: tuple[str, ...] = ()):
        self.tokens = dict(tokens)
        self.broken = set(broken)

    def decrypt_token(self, item, _cipher):
        if item.uid in self.broken:
            raise RuntimeError("database is locked")
        if item.uid not in self.tokens:
            raise crypto.CredentialKeyError("明日方舟账号凭据解密失败")
        return self.tokens[item.uid]


class _FakeClient:
    def __init__(self, results):
        self.results = results
        self.calls: list[str] = []

    async def attendance(self, token, item):
        self.calls.append(item.uid)
        outcome = self.results[token]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ArknightsAttendanceServiceTests(unittest.IsolatedAsyncioTestCase):
    def _roles(self):
        return [
            role("10001234", "1", "甲", primary=True, role_id=1),
            role("20005678", "2", "乙", channel="B服", role_id=2),
        ]

    async def test_success_already_and_failure_are_independent(self):
        store = _FakeStore({"10001234": "token-a", "20005678": "token-b"})
        client = _FakeClient(
            {
                "token-a": client_module.AttendanceResult(
                    "success", "签到成功", (client_module.AttendanceReward("合成玉", 500),)
                ),
                "token-b": client_module.ArknightsAPIError("网络请求失败，请稍后重试。"),
            }
        )
        view = await attendance_module.sign_roles(
            store, client, None, self._roles(), generated_at="2026-01-01 00:00"
        )
        self.assertEqual([item.status for item in view.roles], ["success", "failed"])
        self.assertEqual(view.counts(), {"success": 1, "already": 0, "failed": 1, "total": 2})
        self.assertEqual(client.calls, ["10001234", "20005678"])

    async def test_expired_credential_does_not_stop_other_roles(self):
        store = _FakeStore({"20005678": "token-b"})
        client = _FakeClient(
            {"token-b": client_module.AttendanceResult("success", "签到成功", ())}
        )
        view = await attendance_module.sign_roles(store, client, None, self._roles())
        self.assertEqual([item.status for item in view.roles], ["failed", "success"])
        self.assertIn("解密失败", view.roles[0].message)
        self.assertEqual(client.calls, ["20005678"])

    async def test_unexpected_error_is_contained(self):
        store = _FakeStore({"10001234": "token-a", "20005678": "token-b"})
        client = _FakeClient(
            {
                "token-a": ValueError("boom"),
                "token-b": client_module.AttendanceResult("already", "今日已签到", ()),
            }
        )
        view = await attendance_module.sign_roles(store, client, None, self._roles())
        self.assertEqual([item.status for item in view.roles], ["failed", "already"])

    async def test_storage_fault_does_not_abort_the_batch(self):
        # A locked DB / broken connection is not a CredentialKeyError, yet the
        # remaining characters must still be signed in.
        store = _FakeStore({"20005678": "token-b"}, broken=("10001234",))
        client = _FakeClient(
            {"token-b": client_module.AttendanceResult("success", "签到成功", ())}
        )
        view = await attendance_module.sign_roles(store, client, None, self._roles())
        self.assertEqual([item.status for item in view.roles], ["failed", "success"])
        self.assertEqual(view.roles[0].message, attendance_module.FAILURE_RETRY_MESSAGE)
        self.assertEqual(client.calls, ["20005678"])

    async def test_duplicate_request_is_a_failure_not_a_silent_skip(self):
        registry = tasks_module.RoleTaskRegistry()
        store = _FakeStore({"10001234": "token-a"})
        target = self._roles()[0]
        async with registry.claim(target):
            view = await attendance_module.sign_one(
                store, _FakeClient({}), None, target, registry=registry
            )
        self.assertEqual(view.status, "failed")
        self.assertIn("正在签到", view.message)

    async def test_same_role_concurrency_is_serialized(self):
        registry = tasks_module.RoleTaskRegistry()
        started = asyncio.Event()
        release = asyncio.Event()

        class _SlowClient:
            def __init__(self):
                self.calls = 0

            async def attendance(self, token, item):
                self.calls += 1
                started.set()
                await release.wait()
                return client_module.AttendanceResult("success", "签到成功", ())

        store = _FakeStore({"10001234": "token-a"})
        client = _SlowClient()
        target = self._roles()[0]
        first = asyncio.create_task(
            attendance_module.sign_one(store, client, None, target, registry=registry)
        )
        await started.wait()
        second = await attendance_module.sign_one(store, client, None, target, registry=registry)
        release.set()
        first_view = await first

        self.assertEqual(first_view.status, "success")
        self.assertEqual(second.status, "failed")
        self.assertEqual(client.calls, 1)
        self.assertEqual(registry.active_keys(), frozenset())


# ------------------------------------------------------------------ renderer


class ArknightsRendererTests(unittest.TestCase):
    def _view(self):
        return models_module.AttendanceCardView(
            roles=(
                models_module.AttendanceRoleView(
                    nickname="<script>alert('x')</script>",
                    uid="****1234",
                    full_uid="10001234",
                    channel_name="官服",
                    status="success",
                    rewards=(models_module.AttendanceRewardView("合成玉", 500),),
                ),
                models_module.AttendanceRoleView(
                    nickname="乙 & <b>",
                    uid="****5678",
                    full_uid="20005678",
                    channel_name="B服",
                    status="already",
                ),
                models_module.AttendanceRoleView(
                    nickname="丙",
                    uid="****9999",
                    status="failed",
                    message='<img src="https://evil.example/x.png"> 崩了',
                ),
            ),
            generated_at="2026-01-01 00:00",
        )

    def test_html_escapes_all_user_controlled_text(self):
        html = cards_module.render_attendance_card_html(self._view())
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&amp; &lt;b&gt;", html)
        self.assertIn("&lt;img src=", html)
        self.assertIn("&quot;", html)

    def test_html_contains_no_remote_resources(self):
        html = cards_module.render_attendance_card_html(self._view())
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("evil.example", html)
        from lxml import html as lxml_html
        images = lxml_html.fromstring(html).findall('.//img')
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].get('src'), cards_module.reward_icon_url("合成玉"))
        self.assertIn(cards_module._URL_PLACEHOLDER, html)

    def test_html_renders_statuses_and_rewards(self):
        html = cards_module.render_attendance_card_html(self._view())
        self.assertIn("status-success", html)
        self.assertIn("status-already", html)
        self.assertIn("status-failed", html)
        self.assertIn("合成玉", html)
        self.assertIn("× 500", html)
        self.assertIn("今日已签到，无需重复签到", html)

    def test_empty_roster_renders_a_placeholder(self):
        html = cards_module.render_attendance_card_html(
            models_module.AttendanceCardView(roles=(), generated_at="2026-01-01 00:00")
        )
        self.assertIn("没有可签到的角色", html)

    def test_long_roster_is_capped_with_a_notice(self):
        roles = tuple(
            models_module.AttendanceRoleView(nickname=f"角色{index}", uid="****0000")
            for index in range(cards_module.MAX_CARD_ROWS + 5)
        )
        html = cards_module.render_attendance_card_html(
            models_module.AttendanceCardView(roles=roles, generated_at="x")
        )
        self.assertIn(f"仅显示前 {cards_module.MAX_CARD_ROWS} 个角色", html)

    def test_text_report_is_the_full_fallback_and_masks_uids(self):
        text = models_module.format_attendance_report(self._view(), reveal_uid=False)
        self.assertIn("成功 1", text)
        self.assertIn("签到成功：合成玉 × 500", text)
        self.assertIn("今日已签到，无需重复签到", text)
        self.assertIn("签到失败：", text)
        self.assertIn("****1234", text)
        self.assertNotIn("10001234", text)

        revealed = models_module.format_attendance_report(self._view(), reveal_uid=True)
        self.assertIn("10001234", revealed)


# ------------------------------------------------------- handler integration


class _FakeMatcher:
    """``messages`` keeps everything shown to the user, ``sent`` only ``send()``."""

    def __init__(self, *, fail_sends: int = 0):
        self.messages: list = []
        self.sent: list = []
        self.fail_sends = fail_sends

    async def finish(self, message=None):
        if message is not None:
            self.messages.append(message)

    async def send(self, message=None):
        if self.fail_sends > 0:
            self.fail_sends -= 1
            raise RuntimeError("message connection interrupted")
        if message is not None:
            self.sent.append(message)
            self.messages.append(message)

    def last_text(self) -> str:
        return str(self.messages[-1]) if self.messages else ""


def _event(*, private: bool, group_id: str = "100", user_id: str = "7"):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild=None if private else SimpleNamespace(id=group_id),
        channel=SimpleNamespace(
            id=group_id if not private else user_id,
            type=ChannelType.DIRECT if private else ChannelType.TEXT,
        ),
    )


class ArknightsHandlerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        from plugins.arknights import handlers

        cls.handlers = handlers

    def test_storage_and_http_client_are_created_lazily(self):
        self.assertIsNone(self.handlers._store)
        self.assertIsNone(self.handlers._client)

    async def test_the_first_client_builds_its_tls_context_off_the_event_loop(self):
        from otae_bot.infrastructure.http import tls

        loop_thread = threading.get_ident()
        built_on: list[int] = []
        context = object()

        def create_ssl_context(**_kwargs):
            built_on.append(threading.get_ident())
            return context

        constructed: list[object] = []

        def client_factory():
            # By the time the client asks for the context it must be cached.
            constructed.append(tls._contexts.get(tls._key(False)))
            return mock.Mock(close=mock.AsyncMock())

        with (
            mock.patch.dict(tls._contexts, clear=True),
            mock.patch.object(tls.httpx, "create_ssl_context", side_effect=create_ssl_context),
            mock.patch.object(self.handlers, "ArknightsClient", side_effect=client_factory),
            mock.patch.object(self.handlers, "_client", None),
        ):
            first = await self.handlers._client_instance()
            second = await self.handlers._client_instance()

        self.assertIs(first, second)
        self.assertEqual(len(built_on), 1)
        self.assertNotEqual(built_on[0], loop_thread)
        self.assertEqual(constructed, [context])

    async def _bind(self, *answers, phone_code=None):
        """Run /ak 绑定 with scripted replies, recording sensitivity per prompt."""
        from otae_bot.adapters import message_log

        answers = list(answers)
        prompts: list[tuple[str, bool, str]] = []

        async def prompt(message, timeout):
            reply = answers.pop(0)
            # What Entari's [message] logger would print for this private reply.
            record = {"message": f"[QQ] 博士(7) -> {reply!r}"}
            message_log.redact_record(record)
            prompts.append((message, message_log.is_sensitive("7"), record["message"]))
            return SimpleNamespace(extract_plain_text=lambda: reply)

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = store_module.ArknightsStore(Path(directory.name) / "ak.db")
        self.addCleanup(store.close)
        client = mock.Mock(
            send_phone_code=mock.AsyncMock(),
            token_by_phone_code=mock.AsyncMock(return_value=phone_code),
            discover_roles=mock.AsyncMock(
                return_value=[store_module.RoleCandidate("10001234", "1", "甲", "官服")]
            ),
        )
        cipher = crypto.ArknightsCipher(b"k" * 32)
        matcher = _FakeMatcher()
        with (
            mock.patch.object(self.handlers, "prompt", prompt),
            mock.patch.object(self.handlers, "_store_instance", lambda: store),
            mock.patch.object(self.handlers, "_client_instance", mock.AsyncMock(return_value=client)),
            mock.patch.object(self.handlers.ArknightsCipher, "from_env", return_value=cipher),
        ):
            await self.handlers._dispatch(matcher, _event(private=True), commands_module.parse_command("绑定"))
        self.assertEqual(answers, [])
        self.assertFalse(message_log.is_sensitive("7"))
        return matcher, prompts, client

    async def test_token_binding_keeps_the_token_out_of_the_message_log(self):
        token = "hgAccountToken0123456789abcdefXYZ"
        matcher, prompts, client = await self._bind("1", f'{{"code":0,"data":{{"content":"{token}"}}}}')
        client.discover_roles.assert_awaited_once_with(token)
        self.assertIn("绑定完成", matcher.last_text())
        self.assertTrue(all(sensitive for _message, sensitive, _log in prompts))
        for _message, _sensitive, logged in prompts:
            self.assertNotIn(token, logged)

    async def test_sms_binding_keeps_the_phone_and_code_out_of_the_message_log(self):
        phone, code, token = "13812345678", "654321", "sms-account-token-secret-value"
        matcher, prompts, client = await self._bind("2", phone, code, phone_code=token)
        client.send_phone_code.assert_awaited_once_with(phone)
        client.token_by_phone_code.assert_awaited_once_with(phone, code)
        self.assertIn("绑定完成", matcher.last_text())
        self.assertEqual(len(prompts), 3)
        self.assertTrue(all(sensitive for _message, sensitive, _log in prompts))
        logged = "\n".join(line for _message, _sensitive, line in prompts)
        for secret in (phone, code, phone[3:7]):
            self.assertNotIn(secret, logged)

    async def test_a_failed_binding_still_ends_the_sensitive_dialog(self):
        from otae_bot.adapters import message_log

        async def prompt(message, timeout):
            raise RuntimeError("prompt broke")

        with mock.patch.object(self.handlers, "prompt", prompt), mock.patch.object(
            self.handlers.ArknightsCipher, "from_env", return_value=crypto.ArknightsCipher(b"k" * 32)
        ):
            matcher = _FakeMatcher()
            await self.handlers._dispatch(matcher, _event(private=True), commands_module.parse_command("绑定"))
        self.assertIn("暂时不可用", matcher.last_text())
        self.assertFalse(message_log.is_sensitive("7"))

    async def test_binding_is_refused_in_a_group_chat(self):
        matcher = _FakeMatcher()
        await self.handlers._dispatch(
            matcher, _event(private=False), commands_module.parse_command("绑定")
        )
        self.assertEqual(len(matcher.messages), 1)
        self.assertIn("仅支持私聊", matcher.last_text())

        matcher = _FakeMatcher()
        await self.handlers._dispatch(
            matcher, _event(private=False), commands_module.parse_command("解绑 甲")
        )
        self.assertIn("仅支持私聊", matcher.last_text())

    async def test_unbind_without_a_selector_is_refused(self):
        matcher = _FakeMatcher()
        await self.handlers._dispatch(
            matcher, _event(private=True), commands_module.parse_command("解绑")
        )
        self.assertIn("请指定要解绑的角色", matcher.last_text())

    async def test_help_is_answered_without_storage(self):
        matcher = _FakeMatcher()
        await self.handlers._dispatch(
            matcher, _event(private=True), commands_module.parse_command("")
        )
        self.assertIn("/ak 签到", matcher.last_text())

    def _bound_store(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = store_module.ArknightsStore(Path(directory.name) / "ak.db")
        self.addCleanup(store.close)
        cipher = crypto.ArknightsCipher(b"k" * 32)
        store.bind_roles(
            "7",
            "token-a",
            [
                store_module.RoleCandidate("10001234", "1", "甲", "官服"),
                store_module.RoleCandidate("20005678", "2", "乙", "B服"),
            ],
            cipher,
        )
        return store, cipher

    def _patch_runtime(self, store, cipher, client, *, card=None):
        class _StubCipher:
            @classmethod
            def from_env(cls):
                return cipher

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(self.handlers, "_store_instance", lambda: store))
        stack.enter_context(
            mock.patch.object(self.handlers, "_client_instance", mock.AsyncMock(return_value=client))
        )
        stack.enter_context(mock.patch.object(self.handlers, "ArknightsCipher", _StubCipher))
        stack.enter_context(
            mock.patch.object(
                self.handlers, "draw_attendance_card", mock.AsyncMock(return_value=card)
            )
        )
        return stack

    async def test_default_attendance_signs_every_role(self):
        store, cipher = self._bound_store()
        client = _FakeClient(
            {
                "token-a": client_module.AttendanceResult(
                    "success", "签到成功", (client_module.AttendanceReward("合成玉", 500),)
                ),
                "token-b": client_module.AttendanceResult("already", "今日已签到", ()),
            }
        )
        matcher = _FakeMatcher()
        with self._patch_runtime(store, cipher, client):
            await self.handlers._dispatch(
                matcher, _event(private=True), commands_module.parse_command("签到")
            )
        self.assertEqual(client.calls, ["10001234", "20005678"])
        text = matcher.last_text()
        self.assertIn("甲", text)
        self.assertIn("乙", text)
        self.assertIn("10001234", text)  # private chat may reveal the full UID

    async def test_group_attendance_masks_uids_and_survives_one_failure(self):
        store, cipher = self._bound_store()
        client = _FakeClient(
            {
                "token-a": client_module.ArknightsAPIError("网络请求失败，请稍后重试。"),
                "token-b": client_module.AttendanceResult("success", "签到成功", ()),
            }
        )
        matcher = _FakeMatcher()
        with self._patch_runtime(store, cipher, client):
            await self.handlers._dispatch(
                matcher, _event(private=False), commands_module.parse_command("签到")
            )
        text = matcher.last_text()
        self.assertIn("签到失败", text)
        self.assertIn("****1234", text)
        self.assertNotIn("10001234", text)
        self.assertNotIn("20005678", text)
        self.assertEqual(client.calls, ["10001234", "20005678"])

    async def test_ambiguous_selector_signs_nothing(self):
        store, cipher = self._bound_store()
        client = _FakeClient({})
        matcher = _FakeMatcher()
        with self._patch_runtime(store, cipher, client):
            await self.handlers._dispatch(
                matcher, _event(private=True), commands_module.parse_command("签到 甲 乙")
            )
        self.assertIn("未找到", matcher.last_text())
        self.assertEqual(client.calls, [])

    async def test_render_failure_falls_back_to_full_text(self):
        store, cipher = self._bound_store()
        client = _FakeClient(
            {"token-a": client_module.AttendanceResult("already", "今日已签到", ())}
        )
        matcher = _FakeMatcher()
        with self._patch_runtime(store, cipher, client, card=None):
            # draw_attendance_card returns None -> text fallback.
            await self.handlers._dispatch(
                matcher, _event(private=True), commands_module.parse_command("签到 1234")
            )
        text = matcher.last_text()
        self.assertIn("明日方舟森空岛签到结果", text)
        self.assertIn("10001234", text)  # private chat may reveal the full UID
        self.assertIn("今日已签到", text)
        self.assertEqual(client.calls, ["10001234"])

    async def test_rendered_card_is_sent_when_available(self):
        store, cipher = self._bound_store()
        client = _FakeClient(
            {"token-a": client_module.AttendanceResult("already", "今日已签到", ())}
        )
        matcher = _FakeMatcher()
        delivered: list[bytes] = []

        async def _capture(_matcher, png):
            delivered.append(png)

        with self._patch_runtime(store, cipher, client, card=b"\x89PNG-card"), mock.patch.object(
            self.handlers, "_finish_png", _capture
        ):
            await self.handlers._dispatch(
                matcher, _event(private=True), commands_module.parse_command("签到 1234")
            )
        self.assertEqual(delivered, [b"\x89PNG-card"])
        self.assertEqual(matcher.messages, [])

    async def test_long_numbers_that_are_no_uid_suffix_select_nothing(self):
        store, cipher = self._bound_store()
        client = _FakeClient({})
        for text in ("签到 0001", "签到 0002", "解绑 0001", "主账号 0002"):
            with self.subTest(command=text):
                matcher = _FakeMatcher()
                with self._patch_runtime(store, cipher, client):
                    await self.handlers._dispatch(
                        matcher, _event(private=True), commands_module.parse_command(text)
                    )
                self.assertIn("未找到", matcher.last_text())
        self.assertEqual(client.calls, [])
        roles = store.list_roles("7")
        self.assertEqual([item.uid for item in roles], ["10001234", "20005678"])
        self.assertTrue(roles[0].is_primary)

    async def test_attendance_png_returns_none_when_the_renderer_raises(self):
        view = models_module.AttendanceCardView(roles=(), generated_at="x")
        with mock.patch.object(
            self.handlers, "draw_attendance_card", mock.AsyncMock(side_effect=RuntimeError("no browser"))
        ):
            self.assertIsNone(await self.handlers._attendance_png(view))
        with mock.patch.object(
            self.handlers, "draw_attendance_card", mock.AsyncMock(return_value=b"png")
        ):
            self.assertEqual(await self.handlers._attendance_png(view), b"png")

    async def test_missing_key_message_reaches_the_user(self):
        endfield_key = base64.b64encode(b"f" * 32).decode()
        for text in ("绑定", "签到"):
            with self.subTest(command=text), mock.patch.dict(
                "os.environ",
                {crypto.KEY_ENV_NAME: "", "ENDFIELD_CREDENTIAL_KEY": endfield_key},
                clear=False,
            ):
                matcher = _FakeMatcher()
                await self.handlers._dispatch(
                    matcher, _event(private=True), commands_module.parse_command(text)
                )
            self.assertIn("未配置", matcher.last_text())
            self.assertIn("ARKNIGHTS_CREDENTIAL_KEY", matcher.last_text())


class ArknightsCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_closes_and_resets_state(self):
        from plugins.arknights import handlers

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = store_module.ArknightsStore(Path(directory.name) / "ak.db")
        client = mock.AsyncMock()
        handlers._store, handlers._client = store, client
        try:
            await handlers._close_arknights_state()
        finally:
            handlers._store, handlers._client = None, None

        self.assertIsNone(store.conn)
        client.close.assert_awaited_once()
        self.assertIsNone(handlers._store)
        self.assertIsNone(handlers._client)


if __name__ == "__main__":
    unittest.main()
