from __future__ import annotations

import asyncio
import atexit
import base64
import json
import os
import shutil
import socket
import ssl
import tempfile
import unittest
from dataclasses import replace
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from arclet.entari import MessageChain
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from PIL import Image as PILImage
from satori import Image, Text

from plugins.hyw import agent, google_auth, handlers, network_errors, rendering, web
from plugins.hyw import config as hyw_config
from plugins.hyw.config import HywConfig, HywError
from plugins.hyw.history import HistoryStore

CALL = '<tool_call name="web_search"><query>Entari</query><time_range>w</time_range></tool_call>'
FINAL = '<final_response>这是答案。</final_response>'
SCOPE = ("qq", "bot", "guild", "channel", "user")


def session(user="user", channel="channel", bot="bot", receipt_prefix=""):
    sent = []

    async def send(message, **kwargs):
        sent.append(message)
        return [SimpleNamespace(id=receipt_prefix + str(len(sent)))]

    return SimpleNamespace(
        account=SimpleNamespace(platform="qq", self_id=bot),
        event=SimpleNamespace(user=SimpleNamespace(id=user), channel=SimpleNamespace(id=channel), guild=SimpleNamespace(id="guild")),
        reply=None, send=AsyncMock(side_effect=send), sent=sent,
    )


def command_result(text):
    return SimpleNamespace(all_matched_args={"content": [Text(text)]})


# --- Google 服务账号测试夹具：临时目录中的一次性密钥，测试结束即删除 ---
#
# 这里刻意使用合成标识符（example-project / *.example.com），而不是任何真实项目的
# 项目号、client_email 或 private_key_id：真实凭据只应存在于部署机器的仓库之外，
# 不应因为测试夹具而进入版本库。私钥由本进程即时生成，测试结束随临时目录删除。

SA_DIR = Path(tempfile.mkdtemp(prefix="hyw-sa-"))
atexit.register(shutil.rmtree, SA_DIR, True)
SA_KEY = RSA.generate(2048)
SA_PUBLIC_KEY = SA_KEY.publickey()
SA_PROJECT = "example-project-123456"
SA_EMAIL = "otaebot@example-project-123456.iam.gserviceaccount.com"
SA_KEY_ID = "0123456789abcdef0123456789abcdef01234567"


def write_credentials(directory: Path, name: str, *, raw: str | None = None, **overrides) -> Path:
    """写出一份服务账号 JSON（或原始文本），字段可用 overrides 覆盖以构造异常用例。"""
    path = directory / name
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return path
    document = {
        "type": "service_account",
        "project_id": SA_PROJECT,
        "private_key_id": SA_KEY_ID,
        "private_key": SA_KEY.export_key().decode(),
        "client_email": SA_EMAIL,
        "client_id": "111633939774036951322",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    document.update(overrides)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


SA_FILE = write_credentials(SA_DIR, "service-account.json")


class ProtocolTests(unittest.TestCase):
    def test_internal_planning_and_scoring_never_become_reply(self):
        output = '<response_logic><planning>secret</planning><execution_content><final_response><scoring>scores</scoring># 正文\n答案</final_response></execution_content></response_logic>'
        self.assertEqual(agent.parse_response(output), ("# 正文\n答案", [], ""))

    def test_tool_arguments_and_progress(self):
        final, calls, hint = agent.parse_response('<progress_hint>检索中</progress_hint>' + CALL.replace("Entari", "A &amp; B"))
        self.assertEqual(calls, [("web_search", {"query": "A & B", "time_range": "w"})])
        self.assertEqual((final, hint), ("", "检索中"))

    def test_invalid_protocol_cannot_execute(self):
        for content in ("plain text", FINAL + CALL, CALL * 5, CALL.replace("web_search", "shell"),
                        CALL.replace("<time_range>w</time_range>", ""), CALL + "<tool_call bad>",
                        CALL.replace("</query>", "</query><query>duplicate</query>"), "<final_response></final_response>"):
            with self.subTest(content=content), self.assertRaises(HywError):
                agent.parse_response(content)

    def test_aliases_preserve_multimodal_arguments(self):
        for name in ("q", "hyw", "何意味"):
            result = handlers.hyw_command.parse(MessageChain([Text(name + " 解释 "), Image(src="https://example.com/a.png")]))
            self.assertTrue(result.matched)
            self.assertEqual(handlers.parts_from(result.all_matched_args["content"]), ("解释", ["https://example.com/a.png"]))

    def test_complete_config_tuple_selected_without_key_mixing(self):
        values = {"HYW_CONFIG_SOURCE": "llm", "LLM_API_KEY": "general-secret", "LLM_BASE_URL": "https://llm.example/v1/", "LLM_MODEL": "vision-model", "HYW_API_KEY": "different-secret"}
        with patch("plugins.hyw.config._env", side_effect=lambda key, default=None: values.get(key, default)):
            config = HywConfig.from_env()
        self.assertEqual((config.api_key, config.base_url, config.model), ("general-secret", "https://llm.example/v1", "vision-model"))
        self.assertNotIn("secret", repr(config))

    def test_hyw_proxy_can_override_or_bypass_global_proxy(self):
        global_proxy = {"https": "http://global.example:8080", "http": "http://other.example:8080"}
        for value, expected in [("", global_proxy["https"]), ("  ", global_proxy["https"]), (" DIRECT ", ""), ("http://custom.example:7890", "http://custom.example:7890")]:
            with self.subTest(value=value), patch("plugins.hyw.config.SYSTEM_PROXY", global_proxy), patch("plugins.hyw.config._env", side_effect=lambda key, default=None, selected=value: selected if key == "HYW_PROXY" else default):
                self.assertEqual(HywConfig.from_env().proxy, expected)
        self.assertEqual(global_proxy["https"], "http://global.example:8080")

    def test_search_proxy_is_independent_and_blank_preserves_existing_route(self):
        for model, search, expected in [
            ("direct", "http://search.example:7890", ("", "http://search.example:7890")),
            ("http://model.example:7890", "direct", ("http://model.example:7890", "")),
            ("http://model.example:7890", "", ("http://model.example:7890", "http://model.example:7890")),
            ("direct", "", ("", "")),
        ]:
            values = {"HYW_PROXY": model, "HYW_SEARCH_PROXY": search}
            with self.subTest(values=values), patch("plugins.hyw.config._env", side_effect=lambda key, default=None, settings=values: settings.get(key, default)):
                config = HywConfig.from_env()
            self.assertEqual((config.proxy, config.search_proxy), expected)

    def test_render_data_survives_upstream_bootstrap_and_escapes_html(self):
        answer = agent.Answer('# 中文\n<summary>摘要</summary>\n<script>alert(1)</script><img src=x onerror=alert(2)>', [], [], 1)
        document = rendering.prepare_html(answer)
        self.assertNotIn("window.RENDER_DATA = {};", document)
        self.assertIn("\\u003csummary>", document)
        self.assertIn("&lt;script&gt;", document)
        self.assertNotIn("<img src=x", document)
        self.assertIn("default-src 'none'", document)

    def test_render_injection_bundles_icons_and_drops_page_margin(self):
        # Iconify badges and the bottom panel were broken by the strict CSP and the card's
        # page margin; the injection must keep serving icons offline and reset the margin.
        answer = agent.Answer("# 标题", [], [], 1)
        document = rendering.prepare_html(answer)
        self.assertIn("default-src 'none'", document)
        self.assertNotIn("connect-src", document)
        self.assertIn("window.fetch=function(input,init)", document)
        self.assertIn("M5 4h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5", document)  # bundled mdi:table body
        self.assertIn("#app-wrapper>div{margin:0!important}", document)


class HistoryTests(unittest.TestCase):
    def test_history_isolated_for_every_scope_component_and_copied(self):
        store = HistoryStore()
        store.put(SCOPE, "reply", [{"role": "user", "content": "private question"}])
        for index in range(5):
            other = list(SCOPE)
            other[index] += "other"
            self.assertEqual(store.get(tuple(other), "reply"), [])
        history = store.get(SCOPE, "reply")
        history[0]["content"] = "changed"
        self.assertEqual(store.get(SCOPE, "reply")[0]["content"], "private question")

    def test_ttl_eviction_image_removal_and_clear(self):
        store = HistoryStore(ttl=5, max_entries=2)
        image_message = {"role": "user", "content": [{"type": "text", "text": "question"}, {"type": "image_url", "image_url": {"url": "secret-base64"}}]}
        with patch("plugins.hyw.history.monotonic", return_value=1):
            for key in ("1", "2", "3"):
                store.put(SCOPE, key, [image_message])
            self.assertEqual(store.get(SCOPE, "1"), [])
            self.assertNotIn("secret-base64", json.dumps(store.get(SCOPE, "3")))
        with patch("plugins.hyw.history.monotonic", return_value=7):
            self.assertEqual(store.get(SCOPE, "3"), [])
        self.assertEqual(store._bytes, 0)
        store.put(SCOPE, "new", [image_message])
        store.clear(SCOPE)
        self.assertEqual(store._bytes, 0)

    def test_memory_byte_limit(self):
        store = HistoryStore(max_bytes=100)
        for index in range(10):
            store.put(SCOPE, str(index), [{"role": "user", "content": "x" * 50}])
        self.assertLessEqual(store._bytes, 100)
        store.put(SCOPE, "oversized", [{"role": "user", "content": "x" * 101}])
        self.assertEqual(store.get(SCOPE, "oversized"), [])


class NetworkErrorTests(unittest.TestCase):
    def test_wrapped_transport_causes_are_distinguished(self):
        cases = [
            (ssl.SSLCertVerificationError(1, "certificate verify failed"), "tls_certificate"),
            (ssl.SSLError(1, "handshake failed"), "tls_handshake"),
            (socket.gaierror(-2, "host not found"), "dns"),
            (ConnectionRefusedError(111, "connection refused"), "connection_refused"),
            (OSError(10061, "Windows connection refused"), "connection_refused"),
        ]
        for cause, expected in cases:
            with self.subTest(cause=type(cause).__name__):
                wrapper = RuntimeError("transport wrapper")
                wrapper.__cause__ = cause
                error = httpx.ConnectError("connection failed")
                error.__cause__ = wrapper
                self.assertEqual(network_errors.classify_error(error).code, expected)

    def test_protocol_proxy_and_timeout_errors_have_separate_classifications(self):
        cases = [
            (httpx.ProxyError, "proxy"), (httpx.RemoteProtocolError, "connection_interrupted"),
            (httpx.ReadError, "connection_interrupted"), (httpx.LocalProtocolError, "request_protocol"),
            (httpx.ReadTimeout, "timeout"), (httpx.UnsupportedProtocol, "url_protocol"),
            (httpx.DecodingError, "response_encoding"), (httpx.ConnectError, "connection"),
        ]
        for error_type, expected in cases:
            with self.subTest(error=error_type.__name__):
                self.assertEqual(network_errors.classify_error(error_type("private detail")).code, expected)

    def test_wrapped_ssl_marker_and_cyclic_causes(self):
        error = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] sensitive URL")
        self.assertEqual(network_errors.classify_error(error).code, "tls_certificate")
        error = httpx.ConnectError("unknown")
        error.__cause__ = error
        self.assertEqual(network_errors.classify_error(error).code, "connection")

    def test_tls_want_read_during_read_timeout_is_not_a_handshake_failure(self):
        timeout = TimeoutError()
        timeout.__context__ = ssl.SSLWantReadError(2, "read operation did not complete")
        error = httpx.ReadTimeout("private upstream detail")
        error.__cause__ = timeout
        failure = network_errors.classify_error(error)
        self.assertEqual(failure.code, "timeout")
        self.assertIn("等待模型响应超时", failure.message)
        self.assertNotIn("TLS", failure.message)

    def test_transient_ssl_wait_states_do_not_override_transport_errors(self):
        for error_type, expected in ((httpx.ConnectTimeout, "timeout"),
                                     (httpx.WriteTimeout, "timeout"),
                                     (httpx.ReadError, "connection_interrupted")):
            for ssl_type in (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                with self.subTest(error=error_type, cause=ssl_type):
                    error = error_type("private upstream detail")
                    error.__context__ = ssl_type(2, "operation did not complete")
                    self.assertEqual(network_errors.classify_error(error).code, expected)

    def test_logs_and_replies_do_not_expose_exception_payloads(self):
        config = replace(HywConfig(), api_key="private-key", proxy="http://name:private-password@proxy.example")
        error = httpx.LocalProtocolError("Illegal header value b'Bearer private-key' via private-password")
        with patch.object(network_errors.logger, "warning") as warning:
            reply = network_errors.report_error(error, config)
        self.assertIn("LocalProtocolError", reply)
        self.assertIn("HYW_PROXY=direct", reply)
        for value in (reply, repr(warning.call_args)):
            self.assertNotIn("private-key", value)
            self.assertNotIn("private-password", value)
        self.assertEqual(warning.call_args.args[-1], "proxy")


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_and_search_requests_use_separate_clients(self):
        model_requests, web_requests = [], []

        def model_response(request):
            model_requests.append(request)
            output = CALL if len(model_requests) == 1 else '<final_response>结果[1]</final_response>'
            return httpx.Response(200, json={"choices": [{"message": {"content": output}}]})

        def search_response(request):
            web_requests.append(request)
            return httpx.Response(200, content=b'<a class="result-link" href="https://example.com/doc">Documentation</a>')

        async with httpx.AsyncClient(transport=httpx.MockTransport(model_response)) as model_client, httpx.AsyncClient(transport=httpx.MockTransport(search_response)) as search_client:
            with patch.object(web, "validate_url", AsyncMock()):
                answer = await agent.ask(model_client, replace(HywConfig(), api_key="test-key"), "question", tool_client=search_client)
        self.assertEqual(len(model_requests), 2)
        self.assertTrue(all(request.url.host == "openrouter.ai" for request in model_requests))
        self.assertTrue(all(request.headers["authorization"] == "Bearer test-key" for request in model_requests))
        self.assertEqual(web_requests[0].url.host, "lite.duckduckgo.com")
        self.assertNotIn("authorization", web_requests[0].headers)
        self.assertEqual(answer.sources[0]["url"], "https://example.com/doc")

    async def test_http_failure_surfaces_diagnostic_category_and_route(self):
        def disconnect(request):
            raise httpx.RemoteProtocolError("upstream private payload")

        async with httpx.AsyncClient(transport=httpx.MockTransport(disconnect)) as client:
            with self.assertRaises(HywError) as caught:
                await agent.complete(client, HywConfig(), [])
        message = str(caught.exception)
        self.assertIn("RemoteProtocolError / connection_interrupted", message)
        self.assertIn("直连", message)
        self.assertNotIn("private payload", message)

    async def test_continuation_retains_sources_without_leaking_local_metadata(self):
        store = HistoryStore()
        prior = [{"role": "assistant", "content": "资料[1]", "_sources": [{"index": 1, "title": "资料", "url": "https://example.com"}]}]
        store.put(SCOPE, "reply", prior)
        with patch.object(agent, "complete", AsyncMock(return_value='<final_response>继续解释[1]</final_response>')) as complete:
            answer = await agent.ask(None, HywConfig(), "追问", history=store.get(SCOPE, "reply"))
        self.assertEqual(answer.sources[0]["url"], "https://example.com")
        self.assertIn("https://example.com", rendering.source_links(answer))
        self.assertTrue(all(set(message) == {"role", "content"} for message in complete.await_args.args[2]))

    async def test_search_fetch_final_and_citation_order(self):
        outputs = [CALL, '<tool_call name="web_fetch"><url>https://example.com/b</url></tool_call>', '<final_response>第二项[2]，第一项[1]。</final_response>']
        results = {"results": [{"title": "A", "url": "https://example.com/a"}, {"title": "B", "url": "https://example.com/b"}]}
        requests = []

        async def complete(client, config, messages, budget=None):
            requests.append(list(messages))
            return outputs[len(requests) - 1]

        progress = AsyncMock()
        with patch.object(agent, "complete", side_effect=complete), patch.object(agent, "search", AsyncMock(return_value=results)), patch.object(agent, "fetch_page", AsyncMock(return_value={"title": "B", "url": "https://example.com/b", "content": "正文"})):
            answer = await agent.ask(None, HywConfig(), "问题", progress=progress)
        self.assertEqual(answer.text, "第二项[1]，第一项[2]。")
        self.assertEqual([item["url"] for item in answer.sources], ["https://example.com/b", "https://example.com/a"])
        self.assertEqual(len(answer.history), 2)
        self.assertNotIn("Tool Result", json.dumps(answer.history))
        self.assertEqual(progress.await_count, 2)
        self.assertIn('"index": 2', requests[-1][-1]["content"])
        self.assertNotIn("{current_time}", requests[0][0]["content"])
        self.assertTrue(all(set(message) == {"role", "content"} for message in requests[-1]))

    async def test_format_retries_are_bounded_and_recover(self):
        with patch.object(agent, "complete", AsyncMock(side_effect=["invalid", FINAL])) as complete:
            answer = await agent.ask(None, HywConfig(), "问题")
        self.assertEqual(answer.text, "这是答案。")
        self.assertEqual(complete.await_count, 2)
        with patch.object(agent, "complete", AsyncMock(return_value="invalid")) as complete, self.assertRaises(HywError):
            await agent.ask(None, HywConfig(), "问题")
        self.assertEqual(complete.await_count, 3)

    async def test_tool_budget_forces_final_and_never_executes_excess(self):
        with patch.object(agent, "complete", AsyncMock(return_value=CALL)), patch.object(agent, "search", AsyncMock(return_value={"results": []})) as search, self.assertRaises(HywError):
            await agent.ask(None, replace(HywConfig(), max_tools=2), "问题")
        self.assertEqual(search.await_count, 2)

    async def test_search_failure_is_feedback_to_model(self):
        payloads = []

        async def complete(client, config, messages, budget=None):
            payloads.append(list(messages))
            return CALL if len(payloads) == 1 else '<final_response>搜索不可用，请稍后重试。</final_response>'

        with patch.object(agent, "complete", side_effect=complete), patch.object(agent, "search", AsyncMock(side_effect=HywError("搜索暂时不可用"))):
            result = await agent.ask(None, HywConfig(), "问题")
        self.assertIn("搜索暂时不可用", payloads[1][-1]["content"])
        self.assertEqual(result.sources, [])

    async def test_concurrent_requests_keep_callbacks_and_sources_separate(self):
        async def complete(client, config, messages, budget=None):
            if len(messages) == 2:
                return '<progress_hint>' + config.model + '</progress_hint>' + CALL.replace("Entari", config.model)
            return '<final_response>答案[1]</final_response>'

        async def search(client, query, **kwargs):
            await asyncio.sleep(0)
            return {"results": [{"title": query, "url": f"https://example.com/{query}"}]}

        a, b = AsyncMock(), AsyncMock()
        with patch.object(agent, "complete", side_effect=complete), patch.object(agent, "search", side_effect=search):
            answers = await asyncio.gather(agent.ask(None, replace(HywConfig(), model="A"), "A", progress=a), agent.ask(None, replace(HywConfig(), model="B"), "B", progress=b))
        a.assert_awaited_once_with("A")
        b.assert_awaited_once_with("B")
        self.assertEqual([answer.sources[0]["title"] for answer in answers], ["A", "B"])
        self.assertEqual([answer.sources[0]["index"] for answer in answers], [1, 1])

    async def test_chat_http_request_and_safe_error_responses(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": FINAL}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            output = await agent.complete(client, replace(HywConfig(), api_key="test-secret"), [{"role": "user", "content": "问题"}])
        self.assertEqual(output, FINAL)
        self.assertEqual(str(requests[0].url), "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(requests[0].headers["authorization"], "Bearer test-secret")
        self.assertNotIn("tools", json.loads(requests[0].content))
        for status, body in [(401, {"key": "private"}), (429, {"quota": "private"}), (500, {}), (200, {"bad": "private"})]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req, s=status, b=body: httpx.Response(s, json=b))) as client:
                with self.assertRaises(HywError) as error:
                    await agent.complete(client, HywConfig(), [])
            self.assertNotIn("private", str(error.exception))


class ServiceAccountTests(unittest.TestCase):
    """Google 服务账号模式：断言签名、凭据校验与配置优先级。"""

    def setUp(self):
        google_auth._providers.clear()
        hyw_config._warned.clear()

    def test_assertion_is_verifiable_rs256_with_google_claims(self):
        account = google_auth.load_service_account(str(SA_FILE))
        assertion = google_auth.build_assertion(account, now=1_700_000_000)
        header_b64, claims_b64, signature_b64 = assertion.split(".")
        header = json.loads(base64.urlsafe_b64decode(header_b64 + "=="))
        claims = json.loads(base64.urlsafe_b64decode(claims_b64 + "=="))
        self.assertEqual((header["alg"], header["typ"]), ("RS256", "JWT"))
        self.assertEqual(header["kid"], SA_KEY_ID)
        self.assertEqual(claims["iss"], SA_EMAIL)
        self.assertEqual(claims["aud"], "https://oauth2.googleapis.com/token")
        self.assertEqual(claims["scope"], "https://www.googleapis.com/auth/cloud-platform")
        self.assertEqual(claims["exp"] - claims["iat"], 3600)
        self.assertLess(claims["iat"], 1_700_000_000)  # clock skew is subtracted
        pkcs1_15.new(SA_PUBLIC_KEY).verify(
            SHA256.new(f"{header_b64}.{claims_b64}".encode("ascii")),
            base64.urlsafe_b64decode(signature_b64 + "=="),
        )

    def test_credentials_errors_are_fixed_text_without_path_or_key(self):
        cases = {
            "missing": str(SA_DIR / "absent.json"),
            "bad_json": str(write_credentials(SA_DIR, "broken.json", raw="{not json")),
            "wrong_type": str(write_credentials(SA_DIR, "type.json", type="authorized_user")),
            "no_project": str(write_credentials(SA_DIR, "noproject.json", project_id="")),
            "no_key": str(write_credentials(SA_DIR, "nokey.json", private_key="not-a-pem")),
            "bad_pem": str(write_credentials(SA_DIR, "badpem.json", private_key="-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n")),
        }
        for name, path in cases.items():
            with self.subTest(case=name), self.assertRaises(HywError) as error:
                google_auth.load_service_account(path)
            message = str(error.exception)
            self.assertIn("HYW 服务账号凭据不可用", message)
            self.assertNotIn("PRIVATE KEY", message)
            self.assertNotIn(SA_DIR.name, message)
            self.assertNotIn(path, message)

    def test_credentials_path_expands_home_and_resolves_against_cwd(self):
        self.assertEqual(google_auth.resolve_credentials_path("~/x.json"), Path.home() / "x.json")
        original = Path.cwd()
        try:
            os.chdir(SA_DIR)
            self.assertEqual(google_auth.resolve_credentials_path("data/key.json"), SA_DIR / "data" / "key.json")
        finally:
            os.chdir(original)
        self.assertTrue(google_auth.resolve_credentials_path(str(SA_FILE)).is_file())

    def test_service_account_ignores_relay_base_url_and_prefixes_model(self):
        values = {
            "HYW_CREDENTIALS_FILE": str(SA_FILE),
            "HYW_BASE_URL": "https://llm.hyw.mom/v1",
            "HYW_MODEL": "gemini-3.8-flash",
        }
        with patch("plugins.hyw.config._env", side_effect=lambda key, default=None: values.get(key, default)):
            config = HywConfig.from_env()
        # 关键回归：只加 HYW_CREDENTIALS_FILE 时不得把服务账号令牌发往中转。
        self.assertEqual(config.auth_mode, "service_account")
        self.assertEqual(
            config.base_url,
            f"https://aiplatform.googleapis.com/v1/projects/{SA_PROJECT}/locations/global/endpoints/openapi",
        )
        self.assertNotIn("llm.hyw.mom", config.base_url)
        self.assertEqual(config.model, "google/gemini-3.8-flash")
        self.assertTrue(config.configured)
        self.assertNotIn("PRIVATE KEY", repr(config))

    def test_vertex_base_url_and_location_override_and_model_prefix_is_kept(self):
        values = {
            "HYW_CREDENTIALS_FILE": str(SA_FILE),
            "HYW_VERTEX_BASE_URL": "https://vertex.example/v1/",
            "HYW_VERTEX_LOCATION": "us-central1",
            "HYW_MODEL": "google/gemini-2.5-pro",
        }
        with patch("plugins.hyw.config._env", side_effect=lambda key, default=None: values.get(key, default)):
            config = HywConfig.from_env()
        self.assertEqual(config.base_url, "https://vertex.example/v1")
        self.assertEqual(config.model, "google/gemini-2.5-pro")
        self.assertEqual(config.vertex_location, "us-central1")

    def test_unusable_credentials_degrade_to_none_without_raising(self):
        values = {"HYW_CREDENTIALS_FILE": str(SA_DIR / "absent.json"), "HYW_API_KEY": "relay-secret"}
        with patch("plugins.hyw.config._env", side_effect=lambda key, default=None: values.get(key, default)):
            config = HywConfig.from_env()
        self.assertEqual(config.auth_mode, "none")
        self.assertFalse(config.configured)
        self.assertIn("HYW 服务账号凭据不可用", config.auth_error)

    def test_api_key_mode_is_unchanged_by_the_new_fields(self):
        values = {"HYW_API_KEY": "relay-secret", "HYW_BASE_URL": "https://llm.hyw.mom/v1", "HYW_MODEL": "gemini-3.8-flash"}
        with patch("plugins.hyw.config._env", side_effect=lambda key, default=None: values.get(key, default)):
            config = HywConfig.from_env()
        self.assertEqual(config.auth_mode, "api_key")
        self.assertEqual((config.base_url, config.model, config.api_key), ("https://llm.hyw.mom/v1", "gemini-3.8-flash", "relay-secret"))
        self.assertTrue(config.configured)

    def test_no_credentials_anywhere_is_reported_as_none(self):
        with patch("plugins.hyw.config._env", side_effect=lambda key, default=None: default):
            config = HywConfig.from_env()
        self.assertEqual((config.auth_mode, config.configured, config.auth_error), ("none", False, ""))


class AuthErrorTests(unittest.TestCase):
    def test_google_failures_map_to_actionable_codes(self):
        cases = [
            (400, '{"error":"invalid_grant","error_description":"Invalid JWT Signature."}', "sa_signature"),
            (400, '{"error":"invalid_grant","error_description":"Invalid grant: account not found"}', "sa_account"),
            (400, '{"error":"invalid_grant","error_description":"Token must be a short-lived token (60 minutes)"}', "sa_clock"),
            (400, '{"error":"invalid_request","error_description":"Bad Request"}', "sa_assertion"),
            (400, '{"error":{"status":"INVALID_ARGUMENT","message":"Malformed publisher model"}}', "sa_model_prefix"),
            (400, '{"error":{"message":"Provided image is not valid"}}', "sa_image"),
            (401, '{"error":{"status":"UNAUTHENTICATED"}}', "sa_token"),
            (403, '{"error":{"status":"PERMISSION_DENIED"}}', "sa_project"),
            (403, '{"error":{"status":"CONSUMER_INVALID"}}', "sa_project"),
            (404, '{"error":{"status":"NOT_FOUND"}}', "sa_model"),
            (429, "{}", "quota"),
            (500, '{"error":{"message":"boom"}}', "sa_unknown"),
        ]
        for status, payload, code in cases:
            with self.subTest(status=status, code=code):
                failure = network_errors.classify_auth_error(status, payload)
                self.assertEqual(failure.code, code)
                self.assertTrue(failure.message)

    def test_upstream_payload_never_reaches_the_message(self):
        failure = network_errors.classify_auth_error(403, '{"error":{"message":"leaked-project-123456"}}')
        self.assertNotIn("leaked-project-123456", failure.message)


class TokenProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        google_auth._providers.clear()
        self.account = google_auth.load_service_account(str(SA_FILE))
        self.requests: list[httpx.Request] = []

    def token_transport(self, *, status=200, body=None, delay=0.0):
        async def handler(request):
            self.requests.append(request)
            if delay:
                await asyncio.sleep(delay)
            return httpx.Response(status, json=body if body is not None else {"access_token": "sa-token", "expires_in": 3599})

        return httpx.MockTransport(handler)

    async def test_exchange_is_form_encoded_and_carries_no_authorization(self):
        async with httpx.AsyncClient(transport=self.token_transport()) as client:
            token = await google_auth.bearer_for(client, replace(HywConfig(), auth_mode="service_account", credentials_file=str(SA_FILE)))
        self.assertEqual(token.reveal(), "sa-token")
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://oauth2.googleapis.com/token")
        self.assertEqual(request.headers["content-type"], "application/x-www-form-urlencoded")
        self.assertNotIn("authorization", request.headers)
        form = dict(httpx.QueryParams(request.content.decode()))
        self.assertEqual(form["grant_type"], "urn:ietf:params:oauth:grant-type:jwt-bearer")
        self.assertEqual(form["assertion"].count("."), 2)

    async def test_token_is_cached_and_refreshed_only_near_expiry(self):
        clock = [1_700_000_000.0]
        config = replace(HywConfig(), auth_mode="service_account", credentials_file=str(SA_FILE))
        with patch.object(google_auth, "_now", lambda: clock[0]):
            async with httpx.AsyncClient(transport=self.token_transport()) as client:
                first = await google_auth.bearer_for(client, config)
                clock[0] += 3000  # 3599 - 3000 > 300s margin: still valid
                second = await google_auth.bearer_for(client, config)
                clock[0] += 400  # now inside the margin: refresh
                third = await google_auth.bearer_for(client, config)
        self.assertEqual([item.reveal() for item in (first, second, third)], ["sa-token"] * 3)
        self.assertEqual(len(self.requests), 2)

    async def test_forced_refresh_bypasses_a_cached_token(self):
        clock = [1_700_000_000.0]
        config = replace(HywConfig(), auth_mode="service_account", credentials_file=str(SA_FILE))
        with patch.object(google_auth, "_now", lambda: clock[0]):
            async with httpx.AsyncClient(transport=self.token_transport()) as client:
                await google_auth.bearer_for(client, config)
                await google_auth.bearer_for(client, config, force=True)
        self.assertEqual(len(self.requests), 2)

    async def test_concurrent_requests_exchange_only_once(self):
        config = replace(HywConfig(), auth_mode="service_account", credentials_file=str(SA_FILE))
        async with httpx.AsyncClient(transport=self.token_transport(delay=0.05)) as client:
            tokens = await asyncio.gather(*(google_auth.bearer_for(client, config) for _ in range(4)))
        self.assertEqual([item.reveal() for item in tokens], ["sa-token"] * 4)
        self.assertEqual(len(self.requests), 1)

    async def test_implausible_lifetime_falls_back_to_one_hour(self):
        clock = [1_700_000_000.0]
        config = replace(HywConfig(), auth_mode="service_account", credentials_file=str(SA_FILE))
        with patch.object(google_auth, "_now", lambda: clock[0]):
            async with httpx.AsyncClient(transport=self.token_transport(body={"access_token": "sa-token", "expires_in": 5})) as client:
                await google_auth.bearer_for(client, config)
                clock[0] += 300
                await google_auth.bearer_for(client, config)
        self.assertEqual(len(self.requests), 1)

    async def test_exchange_failures_are_chat_safe(self):
        config = replace(HywConfig(), auth_mode="service_account", credentials_file=str(SA_FILE))
        for status, body, expected in [
            (400, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."}, "sa_signature"),
            (401, {"error": {"status": "UNAUTHENTICATED"}}, "sa_token"),
            (403, {"error": {"status": "PERMISSION_DENIED"}}, "sa_project"),
            (200, {"expires_in": 3599}, "凭据交换未返回访问令牌"),
        ]:
            with self.subTest(status=status):
                self.requests.clear()
                async with httpx.AsyncClient(transport=self.token_transport(status=status, body=body)) as client:
                    with self.assertRaises(HywError) as error:
                        await google_auth.bearer_for(client, config, force=True)
                message = str(error.exception)
                self.assertNotIn("PRIVATE KEY", message)
                self.assertNotIn(SA_EMAIL, message)
                self.assertNotIn(str(SA_FILE), message)
                if expected.startswith("sa_"):
                    self.assertIn(expected, message)
                else:
                    self.assertIn(expected, message)

    async def test_api_key_mode_returns_the_configured_key_untouched(self):
        async with httpx.AsyncClient(transport=self.token_transport()) as client:
            token = await google_auth.bearer_for(client, replace(HywConfig(), api_key="relay-secret"))
        self.assertEqual(token.reveal(), "relay-secret")
        self.assertEqual(self.requests, [])

    def test_secrets_never_appear_in_repr(self):
        """entari 以 diagnose=True 安装 loguru 处理器，traceback 会渲染变量值。

        因此任何可能出现在失败路径上的凭据对象，其 repr 都必须是打码的。
        """
        token = google_auth.Secret("sa-token")
        self.assertEqual(repr(token), "<hidden>")
        self.assertNotIn("sa-token", repr(token))
        self.assertEqual(token.bearer_headers()["Authorization"], "Bearer sa-token")
        self.assertNotIn("sa-token", repr(token.bearer_headers()))
        form = google_auth.Secret("assertion-value").form_data()
        self.assertEqual(form["assertion"], "assertion-value")
        self.assertNotIn("assertion-value", repr(form))
        # 凭据对象本身也不得暴露私钥。
        account = google_auth.load_service_account(str(SA_FILE))
        self.assertNotIn("PRIVATE KEY", repr(account))
        self.assertNotIn(SA_KEY_ID, repr(account))

    def test_signing_failure_is_chat_safe_and_keeps_key_out_of_the_frame(self):
        """签名失败时私钥不得出现在异常帧里（RsaKey 的 repr 含 d/p/q）。"""
        account = google_auth.load_service_account(str(SA_FILE))
        with patch.object(google_auth.pkcs1_15, "new", side_effect=ValueError("simulated signer failure")):
            with self.assertRaises(HywError) as error:
                google_auth.build_assertion(account)
        message = str(error.exception)
        self.assertIn("private_key", message)
        self.assertNotIn("PRIVATE KEY", message)
        # 失败发生在 _sign 内部；_sign 吞掉异常，调用行只引用 account/signing_input。
        self.assertIsNone(error.exception.__cause__)


class ServiceAccountModelCallTests(unittest.IsolatedAsyncioTestCase):
    """模型调用路径：令牌注入、401 重试与空正文容错。"""

    def setUp(self):
        google_auth._providers.clear()
        self.calls: list[httpx.Request] = []

    def setUpConfig(self):
        return replace(HywConfig(), auth_mode="service_account", credentials_file=str(SA_FILE), base_url="https://aiplatform.googleapis.com/v1/projects/p/locations/global/endpoints/openapi", model="google/gemini-3.8-flash")

    def transport(self, *, model_status=200, model_body=None):
        async def handler(request):
            self.calls.append(request)
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "sa-token", "expires_in": 3599})
            return httpx.Response(model_status, json=model_body if model_body is not None else {"choices": [{"message": {"content": FINAL}}]})

        return httpx.MockTransport(handler)

    async def test_model_call_uses_exchanged_token_and_derived_endpoint(self):
        config = self.setUpConfig()
        async with httpx.AsyncClient(transport=self.transport()) as client:
            output = await agent.complete(client, config, [{"role": "user", "content": "问题"}])
        self.assertEqual(output, FINAL)
        model_call = self.calls[-1]
        self.assertEqual(str(model_call.url), config.base_url + "/chat/completions")
        self.assertEqual(model_call.headers["authorization"], "Bearer sa-token")
        self.assertNotIn("sa-token", self.calls[0].headers.get("authorization", ""))

    async def test_rejected_token_is_refreshed_once_and_retried(self):
        config = self.setUpConfig()
        attempts = []

        async def handler(request):
            self.calls.append(request)
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": f"token-{len([c for c in self.calls if c.url.host == 'oauth2.googleapis.com'])}", "expires_in": 3599})
            attempts.append(request.headers["authorization"])
            if len(attempts) == 1:
                return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": FINAL}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            output = await agent.complete(client, config, [{"role": "user", "content": "问题"}])
        self.assertEqual(output, FINAL)
        self.assertEqual(attempts, ["Bearer token-1", "Bearer token-2"])

    async def test_persistent_401_does_not_loop_and_stays_chat_safe(self):
        config = self.setUpConfig()
        async with httpx.AsyncClient(transport=self.transport(model_status=401, model_body={"error": {"status": "UNAUTHENTICATED"}})) as client:
            with self.assertRaises(HywError) as error:
                await agent.complete(client, config, [{"role": "user", "content": "问题"}])
        self.assertIn("sa_token", str(error.exception))
        self.assertEqual(len([call for call in self.calls if call.url.host != "oauth2.googleapis.com"]), 2)

    async def test_service_account_failures_map_to_actionable_text(self):
        config = self.setUpConfig()
        for status, body, expected in [
            (400, {"error": {"status": "INVALID_ARGUMENT", "message": "Malformed publisher model"}}, "发布者前缀"),
            (403, {"error": {"status": "PERMISSION_DENIED"}}, "项目"),
            (404, {"error": {"status": "NOT_FOUND"}}, "模型"),
            (429, {}, "稍后重试"),
        ]:
            with self.subTest(status=status):
                self.calls.clear()
                async with httpx.AsyncClient(transport=self.transport(model_status=status, model_body=body)) as client:
                    with self.assertRaises(HywError) as error:
                        await agent.complete(client, config, [{"role": "user", "content": "问题"}])
                self.assertIn(expected, str(error.exception))

    async def test_missing_content_is_tolerated(self):
        config = self.setUpConfig()
        for body in ({"choices": [{"message": {}}]}, {"choices": [{"message": {"content": "   "}}]}, {"choices": []}, {}):
            with self.subTest(body=body):
                async with httpx.AsyncClient(transport=self.transport(model_body=body)) as client:
                    with self.assertRaises(HywError) as error:
                        await agent.complete(client, config, [{"role": "user", "content": "问题"}])
                self.assertNotIn("private", str(error.exception))


class RetryBackoffTests(unittest.IsolatedAsyncioTestCase):
    """瞬时失败（429/5xx/超时/连接中断）的退避重试。

    两种凭据模式共用同一套策略：api_key 中转与 service_account（Vertex）都只按
    HTTP 契约判定，不依赖任何上游响应体形状。
    """

    def setUp(self):
        google_auth._providers.clear()
        hyw_config._warned.clear()
        self.calls: list[httpx.Request] = []

    def config(self, **overrides):
        # 固定小延迟，让断言只关心“是否重试/等待多久”，不依赖真实退避时间。
        base = replace(HywConfig(), api_key="test-key", retry_base_delay=0.01, retry_max_delay=0.02, retry_budget=5.0)
        return replace(base, **overrides)

    def sa_config(self, **overrides):
        return self.config(
            auth_mode="service_account", credentials_file=str(SA_FILE),
            base_url="https://aiplatform.googleapis.com/v1/projects/p/locations/global/endpoints/openapi",
            model="google/gemini-3.8-flash", **overrides,
        )

    def transport(self, outcomes, *, headers=None):
        """Answer with ``outcomes`` in order (int status or exception), then 200 forever."""

        async def handler(request):
            self.calls.append(request)
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "sa-token", "expires_in": 3599})
            index = self.model_calls - 1
            outcome = outcomes[index] if index < len(outcomes) else 200
            if isinstance(outcome, Exception):
                raise outcome
            if outcome == 200:
                return httpx.Response(200, json={"choices": [{"message": {"content": FINAL}}]})
            return httpx.Response(outcome, json={"error": {"status": "RESOURCE_EXHAUSTED"}}, headers=(headers or {}).get(index, {}))

        return httpx.MockTransport(handler)

    @property
    def model_calls(self) -> int:
        return len([call for call in self.calls if call.url.host != "oauth2.googleapis.com"])

    async def test_rate_limited_request_is_retried_then_succeeds(self):
        """429 后重试成功：这正是“经常 429”场景下要恢复的路径。"""
        with patch.object(agent, "sleep", AsyncMock()) as sleep:
            async with httpx.AsyncClient(transport=self.transport([429, 429])) as client:
                output = await agent.complete(client, self.config(retry_attempts=3), [{"role": "user", "content": "问题"}])
        self.assertEqual(output, FINAL)
        self.assertEqual(self.model_calls, 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_service_account_mode_retries_too(self):
        """服务账号模式走同一条重试路径（Vertex 并发争用会返回裸 RESOURCE_EXHAUSTED）。"""
        with patch.object(agent, "sleep", AsyncMock()) as sleep:
            async with httpx.AsyncClient(transport=self.transport([429])) as client:
                output = await agent.complete(client, self.sa_config(retry_attempts=3), [{"role": "user", "content": "问题"}])
        self.assertEqual(output, FINAL)
        self.assertEqual(self.model_calls, 2)
        self.assertEqual(sleep.await_count, 1)

    async def test_server_errors_are_retried(self):
        """5xx 属网关/服务瞬时故障，同样重试。"""
        for status in (500, 502, 503, 504, 408):
            with self.subTest(status=status):
                self.calls.clear()
                with patch.object(agent, "sleep", AsyncMock()) as sleep:
                    async with httpx.AsyncClient(transport=self.transport([status])) as client:
                        output = await agent.complete(client, self.config(), [{"role": "user", "content": "问题"}])
                self.assertEqual(output, FINAL)
                self.assertEqual(self.model_calls, 2)
                self.assertEqual(sleep.await_count, 1)

    async def test_exhausted_retries_keep_the_single_attempt_wording(self):
        """重试耗尽后必须报出与单次尝试完全相同的文案（不泄漏上游响应体）。"""
        async with httpx.AsyncClient(transport=self.transport([429])) as client:
            with self.assertRaises(HywError) as single:
                await agent.complete(client, self.config(retry_attempts=1), [{"role": "user", "content": "问题"}])
        self.calls.clear()
        with patch.object(agent, "sleep", AsyncMock()):
            async with httpx.AsyncClient(transport=self.transport([429] * 3)) as client:
                with self.assertRaises(HywError) as retried:
                    await agent.complete(client, self.config(retry_attempts=3), [{"role": "user", "content": "问题"}])
        self.assertEqual(str(retried.exception), str(single.exception))
        self.assertIn("稍后重试", str(retried.exception))
        self.assertNotIn("RESOURCE_EXHAUSTED", str(retried.exception))
        self.assertNotIn("private", str(retried.exception))
        self.assertEqual(self.model_calls, 3)

    async def test_non_retryable_status_is_not_retried(self):
        """鉴权/参数类错误重试无意义，只发一次。"""
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                self.calls.clear()
                with patch.object(agent, "sleep", AsyncMock()) as sleep:
                    async with httpx.AsyncClient(transport=self.transport([status])) as client:
                        with self.assertRaises(HywError):
                            await agent.complete(client, self.config(), [{"role": "user", "content": "问题"}])
                self.assertEqual(self.model_calls, 1)
                self.assertEqual(sleep.await_count, 0)

    async def test_retry_after_header_wins_over_computed_backoff(self):
        """Retry-After 是上游唯一说明“限流何时解除”的信号，优先采用。"""
        with patch.object(agent, "sleep", AsyncMock()) as sleep:
            async with httpx.AsyncClient(transport=self.transport([429], headers={0: {"retry-after": "3"}})) as client:
                output = await agent.complete(client, self.config(retry_max_delay=8.0, retry_budget=60.0), [{"role": "user", "content": "问题"}])
        self.assertEqual(output, FINAL)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [3.0])

    async def test_retry_after_is_clamped_to_max_delay(self):
        """上游给的值可能不切实际，按 max_delay 截断。"""
        with patch.object(agent, "sleep", AsyncMock()) as sleep:
            async with httpx.AsyncClient(transport=self.transport([429], headers={0: {"retry-after": "600"}})) as client:
                await agent.complete(client, self.config(retry_max_delay=8.0, retry_budget=60.0), [{"role": "user", "content": "问题"}])
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [8.0])

    async def test_computed_backoff_stays_within_its_window(self):
        """无 Retry-After 时用截断指数退避 + 抖动，且带下限（不会立刻重打）。"""
        policy = agent.RetryPolicy(attempts=4, base_delay=0.5, max_delay=8.0, budget=60.0)
        for attempt, window in ((1, 0.5), (2, 1.0), (3, 2.0), (4, 4.0)):
            for _ in range(20):
                delay = agent.delay_for(attempt, policy)
                self.assertGreaterEqual(delay, window / 2)
                self.assertLessEqual(delay, window)

    async def test_total_sleep_budget_stops_retrying(self):
        """一次问答的累计等待有上限，避免退避吃掉 handler 的 120s 超时。"""
        self.calls.clear()
        with patch.object(agent, "sleep", AsyncMock()) as sleep:
            async with httpx.AsyncClient(transport=self.transport([429] * 8, headers={i: {"retry-after": "1"} for i in range(8)})) as client:
                with self.assertRaises(HywError) as error:
                    await agent.complete(
                        client, self.config(retry_attempts=8, retry_max_delay=8.0, retry_budget=2.0),
                        [{"role": "user", "content": "问题"}],
                    )
        self.assertIn("稍后重试", str(error.exception))
        # 预算 2.0s、每次等 1.0s → 只允许两次退避，第三次预算不足即放弃。
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual(self.model_calls, 3)

    async def test_retry_can_be_disabled_by_configuration(self):
        for overrides in ({"retry_attempts": 1}, {"retry_budget": 0.0}):
            with self.subTest(**overrides):
                self.calls.clear()
                with patch.object(agent, "sleep", AsyncMock()) as sleep:
                    async with httpx.AsyncClient(transport=self.transport([429])) as client:
                        with self.assertRaises(HywError):
                            await agent.complete(client, self.config(**overrides), [{"role": "user", "content": "问题"}])
                self.assertEqual(self.model_calls, 1)
                self.assertEqual(sleep.await_count, 0)

    async def test_transient_transport_failure_is_retried(self):
        """读超时与连接中断会自行恢复，值得重试。"""
        for outcome in (httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("dropped")):
            with self.subTest(outcome=type(outcome).__name__):
                self.calls.clear()
                with patch.object(agent, "sleep", AsyncMock()) as sleep:
                    async with httpx.AsyncClient(transport=self.transport([outcome])) as client:
                        output = await agent.complete(client, self.config(), [{"role": "user", "content": "问题"}])
                self.assertEqual(output, FINAL)
                self.assertEqual(self.model_calls, 2)
                self.assertEqual(sleep.await_count, 1)

    async def test_configuration_transport_failure_is_not_retried(self):
        """DNS/证书/代理类属配置或信任故障，重试只会拖慢诊断。"""
        for outcome, expected in ((httpx.ProxyError("bad proxy"), "代理连接失败"), (httpx.UnsupportedProtocol("bad url"), "协议")):
            with self.subTest(outcome=type(outcome).__name__):
                self.calls.clear()
                with patch.object(agent, "sleep", AsyncMock()) as sleep:
                    async with httpx.AsyncClient(transport=self.transport([outcome])) as client:
                        with self.assertRaises(HywError) as error:
                            await agent.complete(client, self.config(), [{"role": "user", "content": "问题"}])
                self.assertIn(expected, str(error.exception))
                self.assertEqual(self.model_calls, 1)
                self.assertEqual(sleep.await_count, 0)

    async def test_exhausted_transport_retries_report_the_transport_diagnostic(self):
        """传输类重试耗尽后仍是原来的可操作诊断，而不是被退避层改写。"""
        with patch.object(agent, "sleep", AsyncMock()):
            async with httpx.AsyncClient(transport=self.transport([httpx.ReadTimeout("slow")] * 3)) as client:
                with self.assertRaises(HywError) as error:
                    await agent.complete(client, self.config(retry_attempts=3), [{"role": "user", "content": "问题"}])
        self.assertIn("timeout", str(error.exception))
        self.assertEqual(self.model_calls, 3)

    def token_transport(self, outcomes, *, headers=None):
        """令牌端点按 ``outcomes`` 依次应答（状态码或异常），其后一直成功；模型端点始终 200。

        取令牌与调模型是两个独立网络跳，所以这里必须能单独让前者失败。
        """

        async def handler(request):
            self.calls.append(request)
            if request.url.host != "oauth2.googleapis.com":
                return httpx.Response(200, json={"choices": [{"message": {"content": FINAL}}]})
            index = self.token_calls - 1
            outcome = outcomes[index] if index < len(outcomes) else 200
            if isinstance(outcome, Exception):
                raise outcome
            if outcome == 200:
                return httpx.Response(200, json={"access_token": "sa-token", "expires_in": 3599})
            return httpx.Response(
                outcome, json={"error": {"status": "RESOURCE_EXHAUSTED"}}, headers=(headers or {}).get(index, {})
            )

        return httpx.MockTransport(handler)

    @property
    def token_calls(self) -> int:
        return len([call for call in self.calls if call.url.host == "oauth2.googleapis.com"])

    async def test_transient_token_exchange_failure_is_retried(self):
        """令牌跳的瞬时传输失败（自身 20s 超时）必须重试，否则整问直接失败。

        实测形态：一次问答在 20.8s 处失败 = TOKEN_TIMEOUT，而不是模型侧 60s 读超时。
        """
        for outcome in (httpx.ReadTimeout("oauth slow"), httpx.RemoteProtocolError("oauth dropped")):
            with self.subTest(outcome=type(outcome).__name__):
                self.calls.clear()
                # 令牌有模块级缓存：不清掉的话第二轮会复用上一轮换到的令牌，根本不再发起交换。
                google_auth._providers.clear()
                with patch.object(agent, "sleep", AsyncMock()) as sleep:
                    async with httpx.AsyncClient(transport=self.token_transport([outcome])) as client:
                        output = await agent.complete(client, self.sa_config(), [{"role": "user", "content": "问题"}])
                self.assertEqual(output, FINAL)
                self.assertEqual(self.token_calls, 2)
                self.assertEqual(self.model_calls, 1)
                self.assertEqual(sleep.await_count, 1)

    async def test_retryable_token_exchange_status_is_retried(self):
        """令牌跳返回 429/5xx 同样重试（OAuth 端点也会限流）。"""
        for status in (429, 500, 503):
            with self.subTest(status=status):
                self.calls.clear()
                google_auth._providers.clear()
                with patch.object(agent, "sleep", AsyncMock()) as sleep:
                    async with httpx.AsyncClient(transport=self.token_transport([status])) as client:
                        output = await agent.complete(client, self.sa_config(), [{"role": "user", "content": "问题"}])
                self.assertEqual(output, FINAL)
                self.assertEqual(self.token_calls, 2)
                self.assertEqual(self.model_calls, 1)
                self.assertEqual(sleep.await_count, 1)

    async def test_token_exchange_honours_retry_after(self):
        """令牌跳的 Retry-After 与模型侧同等对待。"""
        with patch.object(agent, "sleep", AsyncMock()) as sleep:
            async with httpx.AsyncClient(
                transport=self.token_transport([429], headers={0: {"retry-after": "3"}})
            ) as client:
                output = await agent.complete(
                    client, self.sa_config(retry_max_delay=8.0, retry_budget=60.0), [{"role": "user", "content": "问题"}]
                )
        self.assertEqual(output, FINAL)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [3.0])

    async def test_non_retryable_token_exchange_failure_is_not_retried(self):
        """凭据类失败（签名/账号/项目）重试无意义，只换一次令牌。"""
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                self.calls.clear()
                google_auth._providers.clear()
                with patch.object(agent, "sleep", AsyncMock()) as sleep:
                    async with httpx.AsyncClient(transport=self.token_transport([status])) as client:
                        with self.assertRaises(HywError):
                            await agent.complete(client, self.sa_config(), [{"role": "user", "content": "问题"}])
                self.assertEqual(self.token_calls, 1)
                self.assertEqual(self.model_calls, 0)
                self.assertEqual(sleep.await_count, 0)

    async def test_exhausted_token_exchange_retries_stay_chat_safe(self):
        """令牌跳重试耗尽后仍报可操作文案，且不泄漏凭据或响应体。"""
        with patch.object(agent, "sleep", AsyncMock()):
            async with httpx.AsyncClient(
                transport=self.token_transport([httpx.ReadTimeout("oauth slow")] * 3)
            ) as client:
                with self.assertRaises(HywError) as error:
                    await agent.complete(client, self.sa_config(retry_attempts=3), [{"role": "user", "content": "问题"}])
        message = str(error.exception)
        self.assertIn("timeout", message)
        self.assertNotIn("PRIVATE KEY", message)
        self.assertNotIn(SA_EMAIL, message)
        self.assertEqual(self.token_calls, 3)
        self.assertEqual(self.model_calls, 0)

    async def test_retry_log_names_the_upstream_not_only_the_model(self):
        """重试日志同时覆盖两跳，措辞须与文档一致（曾写作 model request 后改正）。

        日志本身是排障依据：只写 model 会让人误判失败发生在模型调用，而实测的 20.8s
        失败其实在令牌跳。
        """
        with patch.object(agent, "sleep", AsyncMock()), patch.object(agent.logger, "warning") as warning:
            async with httpx.AsyncClient(transport=self.transport([429])) as client:
                await agent.complete(client, self.config(), [{"role": "user", "content": "问题"}])
        self.assertEqual(warning.call_count, 1)
        rendered = warning.call_args.args[0]
        self.assertIn("retrying upstream request", rendered)
        self.assertNotIn("model request", rendered)

    async def test_one_question_shares_a_single_retry_budget(self):
        """预算按“一次问答”共享：单次调用各自计数会撑爆 120s 超时。"""
        with patch.object(agent, "complete", AsyncMock(side_effect=[CALL, FINAL])) as complete, patch.object(
            agent, "search", AsyncMock(return_value={"results": []})
        ):
            await agent.ask(None, self.config(), "问题")
        budgets = [call.args[3] for call in complete.await_args_list]
        self.assertEqual(len(budgets), 2)
        self.assertIsInstance(budgets[0], agent.RetryBudget)
        self.assertIs(budgets[0], budgets[1])

    def test_config_reads_retry_knobs_and_falls_back_on_garbage(self):
        """HYW_RETRY_* 由环境变量读取；非法值回落默认，且下限受保护。"""
        for name, value, attribute, expected in [
            ("HYW_RETRY_ATTEMPTS", "5", "retry_attempts", 5),
            ("HYW_RETRY_ATTEMPTS", "0", "retry_attempts", 1),
            ("HYW_RETRY_ATTEMPTS", "abc", "retry_attempts", 3),
            ("HYW_RETRY_BASE_DELAY", "1.5", "retry_base_delay", 1.5),
            ("HYW_RETRY_BASE_DELAY", "-2", "retry_base_delay", 0.0),
            ("HYW_RETRY_BASE_DELAY", "x", "retry_base_delay", 0.5),
            ("HYW_RETRY_MAX_DELAY", "12", "retry_max_delay", 12.0),
            ("HYW_RETRY_MAX_DELAY", "x", "retry_max_delay", 8.0),
            ("HYW_RETRY_BUDGET", "0", "retry_budget", 0.0),
            ("HYW_RETRY_BUDGET", "x", "retry_budget", 20.0),
        ]:
            with self.subTest(name=name, value=value):
                with patch.dict(os.environ, {name: value}, clear=False):
                    self.assertEqual(getattr(HywConfig.from_env(), attribute), expected)

    def test_retry_policy_is_disabled_when_ineffective(self):
        """attempts=1 或 budget=0 都等价于关闭重试。"""
        self.assertFalse(agent.RetryPolicy(attempts=1).enabled)
        self.assertFalse(agent.RetryPolicy(budget=0.0).enabled)
        self.assertTrue(agent.RetryPolicy().enabled)


class WebTests(unittest.IsolatedAsyncioTestCase):
    def test_public_address_filter(self):
        for url in ("file:///etc/passwd", "http://localhost/", "http://127.0.0.1", "https://10.0.0.1", "https://[::1]", "https://user:pass@example.com", "https://example.com:22", "http://service.internal"):
            self.assertFalse(web.public_url(url), url)
        self.assertTrue(web.public_url("https://example.com/article"))

    async def test_dns_private_address_is_blocked(self):
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[(2, 1, 6, "", ("10.0.0.1", 443))])), self.assertRaises(HywError):
            await web.validate_url("https://example.com")

    async def test_redirect_is_revalidated_and_download_is_bounded(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/secret"}))) as client:
            with patch.object(web, "validate_url", AsyncMock(side_effect=[None, HywError("private")])) as validate, self.assertRaises(HywError):
                await web.download(client, "https://example.com")
            self.assertEqual(validate.await_args_list[1].args[0], "http://127.0.0.1/secret")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"12345"))) as client:
            with patch.object(web, "validate_url", AsyncMock()), self.assertRaises(HywError):
                await web.download(client, "https://example.com", max_bytes=4)

    async def test_challenge_is_not_treated_as_search_evidence(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(202, content=b"challenge"))) as client:
            with patch.object(web, "validate_url", AsyncMock()), self.assertRaisesRegex(HywError, "搜索服务暂时不可用"):
                await web.search(client, "Entari", time_range="w")

    async def test_search_failures_preserve_reason_without_logging_private_payloads(self):
        for status, content, reason in [(202, b"challenge", "http_202"), (200, b"<form id='challenge-form'>verify</form>", "challenge"), (200, b"<html>unexpected page</html>", "unexpected_page")]:
            with self.subTest(reason=reason):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request, code=status, body=content: httpx.Response(code, content=body))) as client:
                    with patch.object(web, "validate_url", AsyncMock()), self.assertRaises(HywError) as caught:
                        await web.search(client, "private question")
                self.assertIn(reason, str(caught.exception))
                self.assertIn("HYW_SEARCH_PROXY", str(caught.exception))
                self.assertNotIn("private question", str(caught.exception))

        with patch.object(web, "download", AsyncMock(side_effect=httpx.ConnectError("private-password"))), patch.object(web.logger, "warning") as warning, self.assertRaises(HywError) as caught:
            await web.search(None, "private query")
        self.assertIn("connection", str(caught.exception))
        self.assertNotIn("private", repr(warning.call_args_list))
        self.assertNotIn("private", str(caught.exception))

    async def test_search_falls_back_after_challenge_and_distinguishes_no_results(self):
        requests = []

        def respond(request):
            requests.append(request)
            content = b'<form id="challenge-form">verify</form>' if request.url.host.startswith("lite.") else b'<div class="no-results">No results found</div>'
            return httpx.Response(200, content=content)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(web, "validate_url", AsyncMock()):
                result = await web.search(client, "query")
        self.assertEqual([request.url.host for request in requests], ["lite.duckduckgo.com", "html.duckduckgo.com"])
        self.assertEqual(result["results"], [])
        self.assertNotIn("error", result)

    async def test_search_parses_results_and_preserves_time_filter(self):
        content = b'<table><tr><td><a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdoc">Entari docs</a></td></tr><tr><td class="result-snippet">Useful result.</td></tr></table>'
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, content=content)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(web, "validate_url", AsyncMock()):
                result = await web.search(client, "Entari", time_range="w", kl="cn-zh")
        self.assertEqual(result["results"][0], {"title": "Entari docs", "url": "https://example.com/doc", "snippet": "Useful result."})
        self.assertEqual(dict(requests[0].url.params), {"q": "Entari", "df": "w", "kl": "cn-zh"})
        self.assertNotIn("authorization", requests[0].headers)

    async def test_web_fetch_extracts_text_without_scripts(self):
        document = '<html><head><title>标题</title></head><body><nav>导航</nav><main><script>secret()</script><p>正文资料</p></main></body></html>'.encode()
        with patch.object(web, "download", AsyncMock(return_value=(document, "text/html; charset=utf-8", "https://example.com"))):
            result = await web.fetch_page(None, "https://example.com")
        self.assertEqual(result["content"], "正文资料")

    async def test_empty_html_is_reported_as_a_tool_error(self):
        with patch.object(web, "download", AsyncMock(return_value=(b"", "text/html", "https://example.com"))), self.assertRaisesRegex(HywError, "正文"):
            await web.fetch_page(None, "https://example.com")


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = HistoryStore()
        self.history_patch = patch.object(handlers, "history_store", self.store)
        self.history_patch.start()
        self.addCleanup(self.history_patch.stop)
        handlers._active.clear()

    async def test_no_key_still_gives_help_and_configuration_feedback(self):
        current = session()
        with patch.object(HywConfig, "from_env", return_value=HywConfig()), patch.object(handlers, "run_request", AsyncMock()) as run:
            await handlers.handle_hyw(current, command_result("帮助"))
            await handlers.handle_hyw(current, command_result("问题"))
        self.assertIn("/q", str(current.sent[0]))
        self.assertIn("HYW_API_KEY", str(current.sent[1]))
        run.assert_not_awaited()

    async def test_quoted_answer_restores_only_own_history(self):
        self.store.put(SCOPE, "answer", [{"role": "user", "content": "earlier private question"}])
        for user, expected in [("user", True), ("other", False)]:
            current = session(user=user)
            current.reply = SimpleNamespace(origin=SimpleNamespace(id="answer", message="public reply"))
            with patch.object(HywConfig, "from_env", return_value=replace(HywConfig(), api_key="test")), patch.object(handlers, "run_request", AsyncMock()) as run:
                await handlers.handle_hyw(current, command_result("追问"))
            self.assertEqual(bool(run.await_args.args[-1]), expected)
            if not expected:
                self.assertIn("public reply", run.await_args.args[3])

    async def test_failure_and_cancellation_release_busy_slot(self):
        current = session()
        with patch.object(HywConfig, "from_env", return_value=replace(HywConfig(), api_key="test")), patch.object(handlers, "run_request", AsyncMock(side_effect=HywError("模型失败"))):
            await handlers.handle_hyw(current, command_result("问题"))
        self.assertEqual(handlers._active, {})
        self.assertIn("模型失败", str(current.sent[0]))
        with patch.object(HywConfig, "from_env", return_value=replace(HywConfig(), api_key="test")), patch.object(handlers, "run_request", AsyncMock(side_effect=asyncio.CancelledError)), self.assertRaises(asyncio.CancelledError):
            await handlers.handle_hyw(current, command_result("问题"))
        self.assertEqual(handlers._active, {})

    async def test_same_user_parallel_answers_keep_their_own_replies_and_history(self):
        started = {text: asyncio.Event() for text in ("first", "second")}
        release = {text: asyncio.Event() for text in started}
        sessions = {text: session(receipt_prefix=text) for text in started}

        async def answer(client, config, content, **kwargs):
            started[content].set()
            await release[content].wait()
            history = [{"role": "user", "content": content}, {"role": "assistant", "content": "answer " + content}]
            return agent.Answer("answer " + content, [], history, 1)

        config = replace(HywConfig(), api_key="test", render=False)
        with patch.object(HywConfig, "from_env", return_value=config), patch.object(handlers, "ask", side_effect=answer):
            tasks = {text: asyncio.create_task(handlers.handle_hyw(current, command_result(text))) for text, current in sessions.items()}
            try:
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), timeout=2)
                release["second"].set()
                await tasks["second"]
                self.assertEqual(handlers._active, {SCOPE: 1})
                self.assertEqual(self.store.get(SCOPE, "second1")[0]["content"], "second")
                self.assertEqual(self.store.get(SCOPE, "first1"), [])
                release["first"].set()
                await tasks["first"]
            finally:
                for event in release.values():
                    event.set()
                await asyncio.gather(*tasks.values(), return_exceptions=True)
        for text, current in sessions.items():
            self.assertEqual(self.store.get(SCOPE, text + "1")[0]["content"], text)
            self.assertEqual(str(current.sent[0]), "answer " + text)
            self.assertTrue(current.send.await_args.kwargs["reply_to"])
        self.assertEqual(handlers._active, {})

    async def test_parallel_requests_count_towards_global_limit_and_release_individually(self):
        started = asyncio.Queue()
        release = {text: asyncio.Event() for text in ("0", "1", "2", "3", "replacement")}
        self.store.put(SCOPE, "old", [{"role": "user", "content": "old question"}])

        async def run(current, config, scope, text, images, prior):
            started.put_nowait(text)
            await release[text].wait()
            if text == "1":
                raise HywError("模型失败")

        config = replace(HywConfig(), api_key="test")
        with patch.object(HywConfig, "from_env", return_value=config), patch.object(handlers, "run_request", side_effect=run) as request:
            tasks = []
            try:
                for text in ("0", "1", "2", "3"):
                    tasks.append(asyncio.create_task(handlers.handle_hyw(session(), command_result(text))))
                    self.assertEqual(await asyncio.wait_for(started.get(), timeout=2), text)
                for current in (session(), session(user="other")):
                    await handlers.handle_hyw(current, command_result("extra"))
                    self.assertIn("当前较忙", str(current.sent[0]))
                self.assertEqual(request.await_count, 4)

                tasks[0].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await tasks[0]
                tasks.append(asyncio.create_task(handlers.handle_hyw(session(), command_result("replacement"))))
                self.assertEqual(await asyncio.wait_for(started.get(), timeout=2), "replacement")
                release["1"].set()
                await tasks[1]
                self.assertEqual(handlers._active, {SCOPE: 3})
                current = session()
                await handlers.handle_hyw(current, command_result("清空"))
                self.assertIn("全部完成", str(current.sent[0]))
                self.assertTrue(self.store.get(SCOPE, "old"))
            finally:
                for event in release.values():
                    event.set()
                await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(handlers._active, {})
        await handlers.handle_hyw(session(), command_result("清空"))
        self.assertEqual(self.store.get(SCOPE, "old"), [])

    async def test_card_reply_is_attached_to_its_question(self):
        current = session()
        answer = agent.Answer("# answer", [], [], 1)
        buffer = BytesIO()
        PILImage.new("RGB", (1, 1)).save(buffer, "PNG")
        with patch.object(handlers, "ask", AsyncMock(return_value=answer)), patch.object(handlers, "render_answer", AsyncMock(return_value=buffer.getvalue())):
            await handlers.run_request(current, HywConfig(), SCOPE, "question", [], [])
        self.assertTrue(current.send.await_args.kwargs["reply_to"])

    async def test_card_failure_delivers_text_and_saves_reply_history(self):
        current = session()
        answer = agent.Answer('# 标题\n<summary>摘要</summary>\n来源[1]', [{"index": 1, "title": "来源", "url": "https://example.com"}], [{"role": "user", "content": "question"}], 2)
        with patch.object(handlers, "ask", AsyncMock(return_value=answer)), patch.object(handlers, "render_answer", AsyncMock(side_effect=RuntimeError("browser unavailable"))):
            await handlers.run_request(current, HywConfig(), SCOPE, "question", [], [])
        self.assertIn("标题", str(current.sent[0]))
        self.assertIn("https://example.com", str(current.sent[1]))
        self.assertTrue(self.store.get(SCOPE, "1"))
        self.assertTrue(self.store.get(SCOPE, "2"))

    async def test_distinct_tool_client_is_closed_with_model_client(self):
        config = replace(HywConfig(), proxy="", search_proxy="http://search.example:7890")
        answer = agent.Answer("answer", [], [], 1)
        transport = httpx.MockTransport(lambda request: httpx.Response(200))
        with patch.object(handlers.httpx, "AsyncHTTPTransport", return_value=transport) as create_transport, patch.object(handlers, "ask", AsyncMock(return_value=answer)) as ask:
            await handlers.run_request(session(), config, SCOPE, "question", [], [])
        self.assertEqual([call.kwargs["proxy"] for call in create_transport.call_args_list], [None, "http://search.example:7890"])
        model_client, tool_client = ask.await_args.args[0], ask.await_args.kwargs["tool_client"]
        self.assertIsNot(model_client, tool_client)
        self.assertTrue(model_client.is_closed)
        self.assertTrue(tool_client.is_closed)

    async def test_same_routes_reuse_one_client(self):
        answer = agent.Answer("answer", [], [], 1)
        with patch.object(handlers, "ask", AsyncMock(return_value=answer)) as ask:
            await handlers.run_request(session(), HywConfig(), SCOPE, "question", [], [])
        self.assertIs(ask.await_args.args[0], ask.await_args.kwargs["tool_client"])
        self.assertTrue(ask.await_args.args[0].is_closed)

    async def test_images_are_real_multimodal_payloads_and_limited(self):
        buffer = BytesIO()
        PILImage.new("RGB", (2000, 1000), "red").save(buffer, "PNG")
        url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
        content = await handlers.model_content(None, "看图", [url])
        self.assertEqual(content[1]["type"], "image_url")
        jpeg = base64.b64decode(content[1]["image_url"]["url"].split(",", 1)[1])
        with PILImage.open(BytesIO(jpeg)) as image:
            self.assertEqual(image.size, (1280, 640))
        with self.assertRaises(HywError):
            await handlers.model_content(None, "", [url] * 4)
        with self.assertRaises(HywError):
            await handlers.model_content(None, "", ["data:image/png;base64,invalid"])

    async def test_output_text_is_not_interpreted_as_satori_markup(self):
        current = session()
        await handlers.send_text(current, '<img src="https://example.com/secret">')
        self.assertIsInstance(current.sent[0][0], Text)


if __name__ == "__main__":
    unittest.main()
