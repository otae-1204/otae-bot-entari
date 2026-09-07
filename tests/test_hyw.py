from __future__ import annotations

import asyncio
import base64
import json
import socket
import ssl
import unittest
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from arclet.entari import MessageChain
from PIL import Image as PILImage
from satori import Image, Text

from plugins.hyw import agent, handlers, network_errors, rendering, web
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

        async def complete(client, config, messages):
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

        async def complete(client, config, messages):
            payloads.append(list(messages))
            return CALL if len(payloads) == 1 else '<final_response>搜索不可用，请稍后重试。</final_response>'

        with patch.object(agent, "complete", side_effect=complete), patch.object(agent, "search", AsyncMock(side_effect=HywError("搜索暂时不可用"))):
            result = await agent.ask(None, HywConfig(), "问题")
        self.assertIn("搜索暂时不可用", payloads[1][-1]["content"])
        self.assertEqual(result.sources, [])

    async def test_concurrent_requests_keep_callbacks_and_sources_separate(self):
        async def complete(client, config, messages):
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
