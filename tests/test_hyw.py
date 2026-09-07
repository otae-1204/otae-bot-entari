from __future__ import annotations

import asyncio
import base64
import json
import unittest
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from arclet.entari import MessageChain
from PIL import Image as PILImage
from satori import Image, Text

from plugins.hyw import agent, handlers, rendering, web
from plugins.hyw.config import HywConfig, HywError
from plugins.hyw.history import HistoryStore

CALL = '<tool_call name="web_search"><query>Entari</query><time_range>w</time_range></tool_call>'
FINAL = '<final_response>这是答案。</final_response>'
SCOPE = ("qq", "bot", "guild", "channel", "user")


def session(user="user", channel="channel", bot="bot"):
    sent = []

    async def send(message):
        sent.append(message)
        return [SimpleNamespace(id=str(len(sent)))]

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


class AgentTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(handlers._active, set())
        self.assertIn("模型失败", str(current.sent[0]))
        with patch.object(HywConfig, "from_env", return_value=replace(HywConfig(), api_key="test")), patch.object(handlers, "run_request", AsyncMock(side_effect=asyncio.CancelledError)), self.assertRaises(asyncio.CancelledError):
            await handlers.handle_hyw(current, command_result("问题"))
        self.assertEqual(handlers._active, set())

    async def test_busy_user_cannot_start_or_clear_inflight_history(self):
        handlers._active.add(SCOPE)
        current = session()
        with patch.object(handlers, "run_request", AsyncMock()) as run:
            await handlers.handle_hyw(current, command_result("问题"))
            await handlers.handle_hyw(current, command_result("清空"))
        run.assert_not_awaited()
        self.assertTrue(all("处理中" in str(message) for message in current.sent))

    async def test_card_failure_delivers_text_and_saves_reply_history(self):
        current = session()
        answer = agent.Answer('# 标题\n<summary>摘要</summary>\n来源[1]', [{"index": 1, "title": "来源", "url": "https://example.com"}], [{"role": "user", "content": "question"}], 2)
        with patch.object(handlers, "ask", AsyncMock(return_value=answer)), patch.object(handlers, "render_answer", AsyncMock(side_effect=RuntimeError("browser unavailable"))):
            await handlers.run_request(current, HywConfig(), SCOPE, "question", [], [])
        self.assertIn("标题", str(current.sent[0]))
        self.assertIn("https://example.com", str(current.sent[1]))
        self.assertTrue(self.store.get(SCOPE, "1"))
        self.assertTrue(self.store.get(SCOPE, "2"))

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
